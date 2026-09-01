#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Напоминания о парах Губкинского — финальная версия для VPS (Aeza).

Каждые 5 минут (cron):
  1. Логинится в lk.gubkin.ru, «посещает» /schedule/ (иначе WAF не пускает),
     забирает расписание группы на сегодня.
  2. В 07:30–08:30 МСК шлёт сводку пар на день (или «пар нет»).
  3. За 15 минут до пары (и до 5 минут после начала) шлёт напоминание.
  4. Дубли отсекает state.json.

Доставка: напрямую в Telegram. Если Telegram временно недоступен с VPS,
сообщение кладётся в очередь queue.json в GitHub-репозитории — оттуда его
доставит workflow sender.yml (GitHub Actions).

Запуск: python3 reader.py [--test-push|--once]
"""

import base64
import http.cookiejar
import io
import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
LOG_PATH = BASE / "reader.log"
LKH = "https://lk.gubkin.ru/"
TZ = ZoneInfo("Europe/Moscow")
HTTP_TIMEOUT = 30
LEAD = 15          # за сколько минут напоминать
SUMMARY = "07:30"
SUMMARY_WINDOW = 60  # минут после 07:30, когда ещё можно прислать сводку

log = logging.getLogger("gubkin")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def now():
    return datetime.now(TZ)


# ---------------------------------------------------------------- HTTP

class LkClient:
    """Клиент ЛК. Важно: /schedule/* отвечает только после GET страницы
    schedule/ той же сессией."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "application/json, text/plain, */*"),
        ]

    def _req(self, path, method="GET", body=None, content_type=None):
        req = urllib.request.Request(LKH + path, method=method)
        if body is not None:
            req.data = body
        if content_type:
            req.add_header("Content-Type", content_type)
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")

    def login(self, login_value, password):
        field = int(login_value) if str(login_value).isdigit() else login_value
        payload = json.dumps(
            {"login": field, "password": password, "rememberMe": 1}
        ).encode("utf-8")
        try:
            resp = json.loads(self._req(
                "api/api.php?module=auth&method=login", "POST", payload,
                "application/json"))
        except urllib.error.HTTPError as e:
            log.error("вход: HTTP %s %s", e.code,
                      e.read().decode("utf-8", "replace")[:200])
            return False
        if resp.get("success") is not True:
            log.error("вход не удался: %s", resp.get("reason"))
            return False
        log.info("вход в ЛК выполнен")
        return True

    def schedule_for(self, day):
        """Пары на день (datetime) по реальному API /schedule/api."""
        # WAF: сначала «визит» на страницу приложения расписания
        try:
            self._req("schedule/")
        except (urllib.error.URLError, io.BlockingIOError, OSError) as e:
            log.warning("визит в /schedule/ не удался: %s", e)
        url = ("schedule/api/api.php?act=schedule&date=%d-%d-%d&groupId=%s"
               % (day.day, day.month, day.year, CFG["group_id"]))
        raw = json.loads(self._req(url))
        rows = raw.get("rows") or {}
        date_str = day.strftime("%d-%m-%Y")
        target = None
        for d in (rows.get("week") or {}).get("weekRussia", {}).get("days", []):
            if d.get("date") == date_str:
                target = d.get("weekDayNumber")
                break
        if target is None:
            return []  # дата вне учебной недели
        classes = []
        for org in rows.get("organizations", []):
            chunks = org.get("lessonsTimeChunks", [])
            for l in org.get("lessons", []):
                if l.get("isCanceled") or l.get("isMoved"):
                    continue
                if l.get("weekDayNumber") != target:
                    continue
                tc = l.get("timeChunks") or []
                if not tc or tc[0] >= len(chunks):
                    continue
                start, end = chunks[tc[0]], chunks[tc[-1]]
                rooms = ", ".join(
                    r.get("number", "") for r in (l.get("rooms") or [])
                    if r.get("number"))
                teachers = ", ".join(
                    t["lastName"] for t in (l.get("teachers") or [])
                    if isinstance(t, dict) and t.get("lastName"))
                classes.append({
                    "start": start.split("-")[0],
                    "end": end.split("-")[-1],
                    "subject": ((l.get("course") or {}).get("name")
                                or l.get("type") or "Занятие"),
                    "room": rooms or None,
                    "teacher": teachers or None,
                    "kind": l.get("type"),
                })
        classes.sort(key=lambda x: x["start"])
        return classes


# ---------------------------------------------------------------- доставка

def tg_send(token, chat, text):
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token, data=data)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = json.loads(resp.read().decode())
    if body.get("ok") is not True:
        raise RuntimeError("telegram: %s" % body)


def gh_enqueue(cfg, msg_id, text):
    """Запасной путь: положить сообщение в queue.json на GitHub."""
    api = "https://api.github.com/repos/%s/contents/queue.json" % cfg["gh_repo"]
    headers = {"Authorization": "token %s" % cfg["gh_token"],
               "Accept": "application/vnd.github+json"}
    q, sha = {"date": "", "sent": [], "pending": []}, None
    try:
        req = urllib.request.Request(api, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        q = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        sha = data["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log.error("GitHub: чтение очереди не удалось (%s)", e.code)
            return False
    except (urllib.error.URLError, ValueError, KeyError) as e:
        log.error("GitHub недоступен: %s", e)
        return False
    sent, pending = q.setdefault("sent", []), q.setdefault("pending", [])
    if msg_id in sent or any(m.get("id") == msg_id for m in pending):
        return True
    pending.append({"id": msg_id, "text": text})
    payload = {
        "message": "queue: %s (из VPS)" % now().strftime("%F %H:%M"),
        "content": base64.b64encode(
            json.dumps(q, ensure_ascii=False, indent=1).encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(
        api, data=json.dumps(payload).encode(), method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            resp.read()
        log.info("сообщение %s отложено в GitHub-очередь", msg_id)
        return True
    except (urllib.error.URLError, OSError) as e:
        log.error("GitHub: запись очереди не удалась: %s", e)
        return False


def deliver(cfg, msg_id, text):
    try:
        tg_send(cfg["telegram_bot_token"], cfg["telegram_chat_id"], text)
        log.info("отправлено в Telegram: %s", msg_id)
        return True
    except (urllib.error.URLError, OSError, RuntimeError, ValueError) as e:
        log.warning("Telegram недоступен (%s) — пробую GitHub-очередь", e)
        return gh_enqueue(cfg, msg_id, text)


# ---------------------------------------------------------------- состояние

def load_state():
    today = now().date().isoformat()
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "sent": []}
    return state


def parse_hhmm(s):
    m = re.match(r"^(\d{1,2}):(\d{2})", str(s).strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def format_class(c):
    parts = [c["subject"]]
    if c.get("kind"):
        parts.append("(%s)" % c["kind"])
    if c.get("room"):
        parts.append("ауд. %s" % c["room"])
    if c.get("teacher"):
        parts.append(c["teacher"])
    return ", ".join(parts)


# ---------------------------------------------------------------- решение

def decide_and_deliver(cfg, classes, state):
    t = now()
    today = t.date().isoformat()
    sent = state.setdefault("sent", [])
    changed = False

    sh, sm = parse_hhmm(cfg.get("summary_time", SUMMARY))
    summary_dt = t.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if (summary_dt <= t < summary_dt + timedelta(
            minutes=cfg.get("summary_window", SUMMARY_WINDOW))
            and "summary" not in sent):
        if classes:
            lines = ["Пары на сегодня (%s):" % today]
            lines += ["%s–%s — %s" % (c["start"], c["end"], format_class(c))
                      for c in classes]
        else:
            lines = ["Пар сегодня нет — отдыхайте!"]
        if deliver(cfg, "summary", "Расписание на день\n" + "\n".join(lines)):
            sent.append("summary")
        changed = True

    lead = cfg.get("minutes_before_class", LEAD)
    for i, c in enumerate(classes):
        hhmm = parse_hhmm(c["start"])
        if not hhmm:
            continue
        start_dt = t.replace(hour=hhmm[0], minute=hhmm[1], second=0,
                             microsecond=0)
        alert_dt = start_dt - timedelta(minutes=lead)
        key = "class:%s:%s" % (today, c["start"])
        if alert_dt <= t < start_dt + timedelta(minutes=5) and key not in sent:
            if t >= start_dt:
                msg = "Уже началась (%s–%s): %s" % (
                    c["start"], c["end"], format_class(c))
            else:
                left = int((start_dt - t).total_seconds() // 60)
                msg = "В %s–%s (через %d мин): %s" % (
                    c["start"], c["end"], left, format_class(c))
            if deliver(cfg, key, "Скоро пара\n" + msg):
                sent.append(key)
            changed = True

    if changed:
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- main

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )


def main():
    global CFG
    setup_logging()
    CFG = load_config()
    args = set(sys.argv[1:])

    if "--test-push" in args:
        ok = deliver(CFG, "test:%d" % now().timestamp(),
                     "Тест уведомлений (VPS)\n"
                     "Система работает независимо от вашего Mac.")
        return 0 if ok else 1

    client = LkClient()
    if not client.login(CFG["lk_login"], CFG["lk_password"]):
        state = load_state()
        if "auth_fail" not in state.get("sent", []):
            deliver(CFG, "auth_fail:%s" % now().date(),
                    "ЛК Губкина: вход не удался\n"
                    "Проверьте логин/пароль в /opt/gubkin/config.json.")
            state.setdefault("sent", []).append("auth_fail")
            STATE_PATH.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return 2

    try:
        classes = client.schedule_for(now())
    except (urllib.error.URLError, ValueError, OSError) as e:
        log.error("расписание не получено: %s", e)
        return 3

    log.info("пар сегодня: %d%s", len(classes),
             ": " + "; ".join("%s %s" % (c["start"], c["subject"])
                              for c in classes) if classes else "")
    decide_and_deliver(CFG, classes, load_state())
    return 0


if __name__ == "__main__":
    sys.exit(main())
