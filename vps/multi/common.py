# -*- coding: utf-8 -*-
"""Общий код многопользовательского бота расписания Губкинского.

Расписание группы — публичные данные: логин в ЛК не нужен, достаточно
GET страницы /schedule/ той же сессии перед обращением к API (иначе WAF).

Важно: у Губкина чётные (верхние) и нечётные (нижние) недели различаются,
поэтому кэш хранится РАЗДЕЛЬНО по неделям — get_week(group_id, day) всегда
возвращает данные именно той недели, к которой относится day.
"""

import http.cookiejar
import json
import logging
import re
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
CACHE_DIR = BASE / "cache"
DB_PATH = BASE / "users.db"
CONFIG_PATH = BASE / "config.json"
CACHE_DIR.mkdir(exist_ok=True)

LKH = "https://lk.gubkin.ru/"
TZ = ZoneInfo("Europe/Moscow")
HTTP_TIMEOUT = 40    # telegram/github
SITE_TIMEOUT = 12    # lk.gubkin.ru: в норме 0.1с; tarpit отвалится за 12с
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

log = logging.getLogger("gubkin-multi")


def now():
    return datetime.now(TZ)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- база

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        group_id INTEGER NOT NULL,
        group_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1)""")
    try:
        conn.execute("ALTER TABLE users ADD COLUMN "
                     "enabled INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # колонка уже есть
    return conn


def add_user(conn, chat_id, username, group_id, group_name):
    conn.execute(
        "INSERT OR REPLACE INTO users(chat_id,username,group_id,group_name,"
        "created_at,enabled) VALUES (?,?,?,?,?,1)",
        (chat_id, username, group_id, group_name, now().isoformat()))
    conn.commit()


def set_enabled(conn, chat_id, enabled):
    conn.execute("UPDATE users SET enabled=? WHERE chat_id=?",
                 (1 if enabled else 0, chat_id))
    conn.commit()


def del_user(conn, chat_id):
    conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
    conn.commit()


def get_user(conn, chat_id):
    return conn.execute(
        "SELECT chat_id,username,group_id,group_name FROM users "
        "WHERE chat_id=?", (chat_id,)).fetchone()


def users_all(conn):
    return conn.execute(
        "SELECT chat_id,username,group_id,group_name,enabled FROM users "
        "ORDER BY created_at").fetchall()


def users_by_group(conn):
    """Только включённые пользователи — для рассылки уведомлений."""
    out = {}
    for chat_id, _u, gid, gname in conn.execute(
            "SELECT chat_id,username,group_id,group_name FROM users "
            "WHERE enabled=1"):
        out.setdefault(gid, {"name": gname,
                             "chats": []})["chats"].append(chat_id)
    return out


# ---------------------------------------------------------------- http

class SchedClient:
    """Публичный клиент расписания (без логина). Сессия персистентна
    (cache/session_cookies.txt) и общая для бота и рассыльщика: после
    решения капчи (/unlock) все процессы пользуются разблокированной
    сессией. Визит на /schedule/ нужен один раз на сессию, не на запрос.
    """

    SESSION_PATH = CACHE_DIR / "session_cookies.txt"

    def __init__(self):
        self.jar = http.cookiejar.MozillaCookieJar(str(self.SESSION_PATH))
        try:
            self.jar.load(ignore_discard=True, ignore_expires=True)
        except OSError:
            pass
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", UA),
                                  ("Accept",
                                   "application/json, text/plain, */*")]

    def _has_session(self):
        return any(c.name == "PHPSESSID" for c in self.jar)

    def _save(self):
        try:
            self.jar.save(ignore_discard=True, ignore_expires=True)
        except OSError as e:
            log.warning("не сохранить cookies: %s", e)

    def _get(self, path, timeout=SITE_TIMEOUT):
        req = urllib.request.Request(LKH + path)
        with self.opener.open(req, timeout=timeout) as resp:
            data = resp.read()
        self._save()
        return data

    def visit(self, force=False):
        if not force and self._has_session():
            return
        self._get("schedule/")
        self._save()

    def api(self, path, timeout=SITE_TIMEOUT):
        return json.loads(self._get(path, timeout).decode("utf-8", "replace"))

    def raw(self, path, timeout=SITE_TIMEOUT):
        return self._get(path, timeout)

    def post_json(self, path, payload, timeout=SITE_TIMEOUT):
        req = urllib.request.Request(LKH + path, method="POST")
        req.data = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        with self.opener.open(req, timeout=timeout) as resp:
            data = resp.read()
        self._save()
        return json.loads(data.decode("utf-8", "replace"))


# ------------------------------------------------- state ожидания капчи

PENDING_PATH = CACHE_DIR / "pending_captcha.json"


def pending_load():
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def pending_set(chat_id):
    p = pending_load()
    p[str(chat_id)] = now().isoformat()
    PENDING_PATH.write_text(json.dumps(p), encoding="utf-8")


def pending_clear(chat_id=None):
    if chat_id is None:
        PENDING_PATH.write_text("{}", encoding="utf-8")
        return
    p = pending_load()
    p.pop(str(chat_id), None)
    PENDING_PATH.write_text(json.dumps(p), encoding="utf-8")


def pending_is(chat_id, max_age_min=180):
    p = pending_load()
    ts = p.get(str(chat_id))
    if not ts:
        return False
    try:
        return (now() - datetime.fromisoformat(ts)) <= timedelta(
            minutes=max_age_min)
    except ValueError:
        return False


# --------------------------------------------- лимиты показов капчи

AUTO_CAPTCHA_PATH = CACHE_DIR / "auto_captcha.json"
AUTO_CAPTCHA_LIMIT = 3


def auto_captcha_allow(max_per_day=AUTO_CAPTCHA_LIMIT):
    """True, если авто-показ капчи сегодня не исчерпан (и учитывает показ)."""
    try:
        st = json.loads(AUTO_CAPTCHA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    today = now().date().isoformat()
    if st.get("date") != today:
        st = {"date": today, "count": 0}
    if st["count"] >= max_per_day:
        AUTO_CAPTCHA_PATH.write_text(json.dumps(st), encoding="utf-8")
        return False
    st["count"] += 1
    AUTO_CAPTCHA_PATH.write_text(json.dumps(st), encoding="utf-8")
    return True


MANUAL_UNLOCK_PATH = CACHE_DIR / "manual_unlock.json"
MANUAL_UNLOCK_LIMIT = 15


def manual_unlock_allow(max_per_day=MANUAL_UNLOCK_LIMIT):
    """True, если ручных /unlock сегодня меньше 15 (и учитывает попытку)."""
    try:
        st = json.loads(MANUAL_UNLOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    today = now().date().isoformat()
    if st.get("date") != today:
        st = {"date": today, "count": 0}
    if st["count"] >= max_per_day:
        MANUAL_UNLOCK_PATH.write_text(json.dumps(st), encoding="utf-8")
        return False
    st["count"] += 1
    MANUAL_UNLOCK_PATH.write_text(json.dumps(st), encoding="utf-8")
    return True


# ---------------------------------------------------------------- backoff

FETCH_STATE = CACHE_DIR / "fetch_state.json"


class Backoff(Exception):
    """Сайт недоступен/ошибка сети — повторить позже."""


class CaptchaNeeded(Exception):
    """Сайт ответил 429 «введите капчу» — нужен человек с /unlock."""


def fetch_state():
    try:
        return json.loads(FETCH_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"next_ok": None, "tries": 0}


def fetch_state_save(st):
    FETCH_STATE.write_text(json.dumps(st), encoding="utf-8")


def backoff_active():
    st = fetch_state()
    nk = st.get("next_ok")
    if not nk:
        return False
    try:
        return now() < datetime.fromisoformat(nk)
    except ValueError:
        return False


def backoff_register():
    st = fetch_state()
    st["tries"] = st.get("tries", 0) + 1
    wait = min(30 * 2 ** (st["tries"] - 1), 360)
    st["next_ok"] = (now() + timedelta(minutes=wait)).isoformat()
    fetch_state_save(st)
    return wait


def backoff_clear():
    st = fetch_state()
    if st.get("tries") or st.get("next_ok"):
        fetch_state_save({"next_ok": None, "tries": 0})


# ---------------------------------------------------------------- кэш недель

def week_key(day):
    """Понедельник недели, к которой относится day (datetime/date)."""
    d = day.date() if isinstance(day, datetime) else day
    return (d - timedelta(days=d.weekday())).isoformat()


def sched_cache_path(gid, day=None):
    """Кэш недели: текущая неделя — общий файл, другие — по понедельнику."""
    if day is None:
        return CACHE_DIR / ("sched_%s.json" % gid)
    if week_key(day) == week_key(now()):
        return CACHE_DIR / ("sched_%s.json" % gid)
    return CACHE_DIR / ("sched_%s_%s.json" % (gid, week_key(day)))


def load_sched(gid, day=None):
    try:
        return json.loads(
            sched_cache_path(gid, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_sched(gid, data, day=None):
    sched_cache_path(gid, day).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _norm_week(raw, group_id):
    """Сырой ответ act=schedule -> {week_days, lessons} (нормализовано)."""
    rows = raw.get("rows") or {}
    week_days = (rows.get("week") or {}).get("weekRussia", {}).get("days", [])
    lessons = []
    for org in rows.get("organizations", []):
        chunks = org.get("lessonsTimeChunks", [])
        for l in org.get("lessons", []):
            gids = [g.get("id") for g in (l.get("groups") or [])]
            if group_id not in gids:
                continue
            tc = l.get("timeChunks") or []
            if not tc or tc[0] >= len(chunks):
                continue
            times = (chunks[tc[0]].split("-")[0],
                     chunks[tc[-1]].split("-")[-1])
            rooms = ", ".join(r.get("number", "")
                              for r in (l.get("rooms") or [])
                              if r.get("number"))
            teachers = ", ".join(
                t["lastName"] for t in (l.get("teachers") or [])
                if isinstance(t, dict) and t.get("lastName"))
            lessons.append({
                "wd": l.get("weekDayNumber"),
                "start": times[0], "end": times[1],
                "subject": ((l.get("course") or {}).get("name")
                            or l.get("type") or "Занятие"),
                "kind": l.get("type"),
                "room": rooms or None,
                "teacher": teachers or None,
                "cancelled": bool(l.get("isCanceled")),
                "moved": bool(l.get("isMoved")),
                "changed": bool(l.get("changes")),
            })
    lessons.sort(key=lambda x: (x["wd"], x["start"]))
    return {"week_days": week_days, "lessons": lessons}


def get_week(group_id, day=None, force=False, max_age_min=None):
    """Расписание недели, содержащей day (по умолчанию — сегодня).

    Недели хранятся раздельно: кэш верхней недели никогда не показывается
    в нижнюю и наоборот. Бросает Backoff (сайт недоступен) или
    CaptchaNeeded (429).
    """
    if day is None:
        day = now()
    day = day.date() if isinstance(day, datetime) else day
    date_str = day.strftime("%d-%m-%Y")
    cache = load_sched(group_id, day)
    covers_day = bool(cache and any(
        d.get("date") == date_str for d in cache.get("week_days", [])))
    fresh = False
    if cache:
        try:
            fresh = (now() - datetime.fromisoformat(
                cache.get("fetched_at", ""))) <= timedelta(
                minutes=max_age_min or 0)
        except ValueError:
            pass
    if (cache and covers_day and not force
            and (fresh or cache.get("fetched_at", "")[:10]
                 == now().date().isoformat())):
        return cache
    if backoff_active():
        if covers_day:
            return cache
        raise Backoff()
    c = SchedClient()
    try:
        c.visit()
        raw = c.api(
            "schedule/api/api.php?act=schedule&date=%d-%d-%d&groupId=%s"
            % (day.day, day.month, day.year, group_id))
    except urllib.error.HTTPError as e:
        # 429 — просит капчу; 418/403/5xx — WAF-бан: отдаём кэш мгновенно
        if e.code == 429:
            wait = backoff_register()
            log.warning("429, пауза %d мин", wait)
            if covers_day:
                return cache
            raise CaptchaNeeded()
        log.warning("сайт ответил HTTP %s — считаю недоступным", e.code)
        backoff_register()
        if covers_day:
            return cache
        raise Backoff()
    except (urllib.error.URLError, ValueError, OSError) as e:
        log.warning("сеть: %s", e)
        if covers_day:
            return cache
        raise Backoff()
    backoff_clear()
    prev = cache if covers_day else None
    data = {"fetched_at": now().isoformat(), "group_id": group_id,
            "week_type": ((raw.get("rows") or {}).get("week") or {})
            .get("weekRussia", {}).get("type")}
    data.update(_norm_week(raw, group_id))
    if prev:
        data["prev_lessons"] = prev.get("lessons", [])
    save_sched(group_id, data, day)
    return data


def needs_refresh(gid, day=None, max_age_min=None):
    """True, если следующий get_week пойдёт в сеть (кэша нет/устарел)."""
    if day is None:
        day = now()
    day = day.date() if isinstance(day, datetime) else day
    date_str = day.strftime("%d-%m-%Y")
    cache = load_sched(gid, day)
    if not cache:
        return True
    if not any(d.get("date") == date_str
               for d in cache.get("week_days", [])):
        return True
    try:
        fdt = datetime.fromisoformat(cache.get("fetched_at", ""))
    except ValueError:
        return True
    if max_age_min is not None:
        return (now() - fdt) > timedelta(minutes=max_age_min)
    return fdt.date() != now().date()


def classes_on_date(week, day):
    """Занятия на дату (datetime/date) из нормализованной недели."""
    d = day.date() if isinstance(day, datetime) else day
    date_str = d.strftime("%d-%m-%Y")
    wd = None
    for dd in week.get("week_days", []):
        if dd.get("date") == date_str:
            wd = dd.get("weekDayNumber")
            break
    if wd is None:
        return []
    return [l for l in week.get("lessons", []) if l.get("wd") == wd]


# ---------------------------------------------------------------- telegram

def tg(api_method, token, files=None, **params):
    """Вызов Bot API через curl (Happy Eyeballs обходит зависания
    отдельных IP api.telegram.org). files: {поле: (filename, bytes)}."""
    url = "https://api.telegram.org/bot%s/%s" % (token, api_method)
    long_poll = api_method == "getUpdates"
    cmd = ["curl", "-s", "--connect-timeout", "4",
           "--max-time", "45" if long_poll else "15",
           "--retry", "1", "--retry-all-errors"]
    if files:
        import tempfile
        tmps = []
        try:
            for name, (fname, data) in files.items():
                tmp = tempfile.NamedTemporaryFile(delete=False,
                                                  suffix=".bin")
                tmp.write(data)
                tmp.close()
                tmps.append(tmp.name)
                cmd += ["-F", "%s=@%s;filename=%s" % (name, tmp.name, fname)]
            for k, v in params.items():
                cmd += ["-F", "%s=%s" % (k, v)]
            cmd.append(url)
            r = subprocess.run(cmd, capture_output=True, timeout=60)
        finally:
            import os
            for p in tmps:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    else:
        for k, v in params.items():
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            cmd += ["--data-urlencode", "%s=%s" % (k, v)]
        cmd.append(url)
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    body = r.stdout.decode("utf-8", "replace")
    if not body:
        raise RuntimeError("telegram %s: пустой ответ" % api_method)
    d = json.loads(body)
    if d.get("ok") is not True:
        raise RuntimeError("telegram %s: %s" % (api_method, d))
    return d.get("result")


class Sender:
    """Отправка с локальной очередью повторов (Telegram с VPS бывает
    нестабилен — недоставленное уходит в cache/pending_sends.json)."""

    QUEUE = CACHE_DIR / "pending_sends.json"

    def __init__(self, token):
        self.token = token

    def _flush_queue(self):
        try:
            pending = json.loads(self.QUEUE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = []
        if not pending:
            return
        still = []
        for item in pending[:200]:
            try:
                tg("sendMessage", self.token, chat_id=item["chat_id"],
                   text=item["text"])
            except Exception:
                still.append(item)
        self.QUEUE.write_text(json.dumps(still), encoding="utf-8")

    def send(self, chat_id, text):
        self._flush_queue()
        try:
            tg("sendMessage", self.token, chat_id=chat_id, text=text)
            return True
        except Exception as e:
            log.warning("send %s не удался (%s) — в очередь", chat_id, e)
            try:
                pending = json.loads(
                    self.QUEUE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pending = []
            pending.append({"chat_id": chat_id, "text": text})
            self.QUEUE.write_text(json.dumps(pending[-500:]),
                                  encoding="utf-8")
            return False

    def broadcast(self, chat_ids, text):
        for cid in chat_ids:
            self.send(cid, text)


# ---------------------------------------------------------------- утилиты

def hhmm(s):
    m = re.match(r"^(\d{1,2}):(\d{2})", str(s).strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def day_label(day):
    name = day.strftime("%d.%m (%a)")
    for en, ru in (("Mon", "пн"), ("Tue", "вт"), ("Wed", "ср"),
                   ("Thu", "чт"), ("Fri", "пт"), ("Sat", "сб"),
                   ("Sun", "вс")):
        name = name.replace(en, ru)
    return name


def week_type_label(week):
    t = week.get("week_type")
    return {"upper": "верхняя (нечётная)",
            "lower": "нижняя (чётная)"}.get(t, "")


def format_class(c, with_time=True):
    parts = []
    if with_time:
        parts.append("%s–%s" % (c["start"], c["end"]))
    title = c["subject"] + (" (%s)" % c["kind"] if c.get("kind") else "")
    parts.append(title)
    extra = []
    if c.get("room"):
        extra.append("ауд. " + c["room"])
    if c.get("teacher"):
        extra.append(c["teacher"])
    if extra:
        parts.append(", ".join(extra))
    return " — ".join(parts[:2]) + ((" | " + ", ".join(extra)) if extra else "")


def lessons_text(lessons, header):
    if not lessons:
        return header + "\nЗанятий нет — отдыхайте!"
    live = [l for l in lessons if not l.get("cancelled")]
    cancelled = [l for l in lessons if l.get("cancelled")]
    lines = [header]
    for l in live:
        lines.append("• " + format_class(l))
    for l in cancelled:
        lines.append("❌ ОТМЕНЕНА: %s (%s)" % (l["subject"], l["start"]))
    return "\n".join(lines)
