# -*- coding: utf-8 -*-
"""Функция для Yandex Cloud с таймером каждые 5 минут.

Логика та же, что в локальной версии: вход в ЛК Губкинского, расписание
на день, утренняя сводка (07:30 МСК) и напоминания за 15 минут до пары.
Состояние (что уже отправлено) хранится в файле sent_state.json
GitHub-репозитория — функция в облаке не имеет постоянного диска.

Переменные окружения (задаются в конфигурации функции):
  LK_LOGIN, LK_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
  GH_TOKEN, GH_REPO (например "ran1k1n/gubkin-pairs"),
  TEST_CONNECTIVITY=1 — только проверить доступность lk.gubkin.ru.
"""

import base64
import http.cookiejar
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LKH = "https://lk.gubkin.ru/"
TGH = "https://api.telegram.org/bot%s/sendMessage"
GH_CONTENTS = "https://api.github.com/repos/%s/contents/sent_state.json"
STATE_FILE = "sent_state.json"
STATE_PATH = os.path.dirname(os.path.abspath(__file__)) + "/" + STATE_FILE
TZ = ZoneInfo("Europe/Moscow")
LEAD = 15  # за сколько минут напоминать о паре
HTTP_TIMEOUT = 20

log = logging.getLogger()
log.setLevel(logging.INFO)


def env(name, default=""):
    return os.environ.get(name, default)


def now():
    return datetime.now(TZ)


# ---------------------------------------------------------------- HTTP

class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.opener.addheaders = [
            ("Accept", "application/json"),
            ("Content-Type", "application/json"),
            ("User-Agent", "gubkin-yc/1.0"),
        ]

    def post_json(self, url, payload, headers=None):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST"
        )
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_json(self, url, headers=None):
        req = urllib.request.Request(url, method="GET")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post_form(self, url, form):
        data = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def put_json(self, url, payload, headers=None):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="PUT"
        )
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- ЛК

def lk_login(c):
    login, password = env("LK_LOGIN"), env("LK_PASSWORD")
    if not login or not password:
        log.error("нет LK_LOGIN/LK_PASSWORD")
        return False
    field = int(login) if str(login).isdigit() else login
    try:
        resp = c.post_json(
            LKH + "api/api.php?module=auth&method=login",
            {"login": field, "password": password, "rememberMe": 1},
        )
    except urllib.error.URLError as e:
        log.error("ЛК недоступен: %s", e)
        return False
    if resp.get("success") is not True:
        log.error("вход не удался: %s", resp.get("reason"))
        return False
    log.info("успешный вход в ЛК")
    return True


TIMETABLE_API = env(
    "TIMETABLE_API", "api/api.php?module=study&method=timetable"
)


# ---------------------------------------------------------------- расписание

def parse_hhmm(s):
    m = re.match(r"^(\d{1,2}):(\d{2})", str(s).strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_timetable(raw):
    classes = []

    def first(node, keys, *cands):
        for cand in cands:
            k = keys.get(cand)
            if k and node[k]:
                return str(node[k])
        return None

    def walk(node):
        if isinstance(node, dict):
            keys = {str(k).lower(): str(k) for k in node}
            start = None
            for cand in ("start", "starttime", "begintime", "время", "начало",
                         "time_from", "from", "lessonstart"):
                if cand in keys:
                    start = parse_hhmm(node[keys[cand]])
                    if start:
                        break
            subject = first(node, keys, "subject", "discipline", "name",
                            "title", "предмет", "дисциплина", "lesson",
                            "lessonname")
            if start and subject:
                classes.append({
                    "start": "%02d:%02d" % start,
                    "subject": subject,
                    "room": first(node, keys, "room", "auditory", "place",
                                  "аудитория", "кабинет", "location"),
                    "teacher": first(node, keys, "teacher", "lecturer",
                                     "преподаватель"),
                    "kind": first(node, keys, "kind", "type", "вид",
                                  "lessontype"),
                })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(raw)
    classes.sort(key=lambda c: c["start"])
    return classes


# ---------------------------------------------------------------- уведомления

def send(text):
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.error("нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return False
    body = Client().post_form(TGH % token, {"chat_id": chat, "text": text})
    if body.get("ok") is not True:
        log.error("Telegram ошибка: %s", body)
        return False
    return True


def format_class(c):
    parts = [c["subject"]]
    if c.get("room"):
        parts.append("ауд. %s" % c["room"])
    if c.get("teacher"):
        parts.append(c["teacher"])
    if c.get("kind"):
        parts.append("(%s)" % c["kind"])
    return ", ".join(parts)


# ---------------------------------------------------------------- очередь в GitHub

QUEUE_URL = "https://api.github.com/repos/%s/contents/queue.json"


def gh_load():
    """Читает queue.json: {date, sent: [id...], pending: [{id, text}...]}."""
    url = QUEUE_URL % env("GH_REPO", "ran1k1n/gubkin-pairs")
    headers = {
        "Authorization": "token %s" % env("GH_TOKEN"),
        "Accept": "application/vnd.github+json",
    }
    try:
        data = Client().get_json(url, headers)
        content = base64.b64decode(data["content"]).decode("utf-8")
        q = json.loads(content)
        q["_sha"] = data["sha"]
        return q
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"_sha": None}  # файла ещё нет — создадим
        log.warning("GitHub ответил %s, начинаю с чистой очереди", e.code)
        return {"_sha": None}
    except (urllib.error.URLError, ValueError, KeyError) as e:
        log.warning("не удалось прочитать очередь (%s)", e)
        return {"_sha": None}


def gh_save(q):
    headers = {
        "Authorization": "token %s" % env("GH_TOKEN"),
        "Accept": "application/vnd.github+json",
    }
    q = dict(q)
    sha = q.pop("_sha", None)
    payload = {
        "message": "queue: %s МСК" % now().strftime("%F %H:%M"),
        "content": base64.b64encode(
            json.dumps(q, ensure_ascii=False, indent=1).encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    try:
        Client().put_json(QUEUE_URL % env("GH_REPO", "ran1k1n/gubkin-pairs"),
                          payload, headers)
        return True
    except urllib.error.URLError as e:
        log.error("не удалось сохранить очередь: %s", e)
        return False


def enqueue(q, msg_id, text):
    """Кладёт сообщение в очередь, если оно не отправлялось и не лежит уже."""
    sent = q.setdefault("sent", [])
    pending = q.setdefault("pending", [])
    if msg_id in sent or any(m.get("id") == msg_id for m in pending):
        return False
    pending.append({"id": msg_id, "text": text})
    return True


# ---------------------------------------------------------------- решение

def decide_and_notify(classes, q):
    t = now()
    today = t.date().isoformat()
    if q.get("date") != today:
        q.update({"date": today, "sent": [], "pending": []})
    changed = False

    summary_dt = t.replace(hour=7, minute=30, second=0, microsecond=0)
    if summary_dt <= t < summary_dt + timedelta(minutes=60) and "summary" not in q["sent"]:
        if classes:
            lines = ["Пары на сегодня (%s):" % today]
            lines += ["%s — %s" % (c["start"], format_class(c)) for c in classes]
        else:
            lines = ["Пар сегодня нет — отдыхайте!"]
        if enqueue(q, "summary", "Расписание на день\n" + "\n".join(lines)):
            changed = True

    for c in classes:
        hhmm = parse_hhmm(c["start"])
        if not hhmm:
            continue
        start_dt = t.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
        alert_dt = start_dt - timedelta(minutes=LEAD)
        key = "class:%s:%s" % (today, c["start"])
        if alert_dt <= t < start_dt + timedelta(minutes=5) and key not in q["sent"]:
            if t >= start_dt:
                text = "Скоро пара\nУже началась (%s): %s" % (c["start"], format_class(c))
            else:
                left = int((start_dt - t).total_seconds() // 60)
                text = "Скоро пара\nВ %s (через %d мин): %s" % (
                    c["start"], left, format_class(c))
            if enqueue(q, key, text):
                changed = True

    return changed


# ---------------------------------------------------------------- входная точка

def handler(event, context):
    if env("TEST_CONNECTIVITY"):
        results = []
        for host in ("lk.gubkin.ru", "api.github.com", "api.telegram.org"):
            try:
                req = urllib.request.Request("https://%s/" % host, method="HEAD")
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    results.append("%s: HTTP %s" % (host, resp.status))
            except urllib.error.HTTPError as e:
                results.append("%s: HTTP %s (доступен)" % (host, e.code))
            except urllib.error.URLError as e:
                results.append("%s: НЕДОСТУПЕН (%s)" % (host, e.reason))
        return "; ".join(results)
    if event and str(event.get("test_push", "")).lower() in ("1", "true"):
        q = gh_load()
        changed = enqueue(q, "test:%s" % now().strftime("%s"),
                          "Тест уведомлений (облако)\n"
                          "Цепочка Яндекс → GitHub → Telegram работает.")
        saved = gh_save(q) if changed else False
        return "test_push enqueued: %s, saved: %s" % (changed, saved)

    q = gh_load()
    c = Client()
    if not lk_login(c):
        changed = enqueue(q, "auth_fail:%s" % now().date(),
                          "ЛК Губкина: вход не удался\n"
                          "Проверьте LK_LOGIN/LK_PASSWORD в настройках функции.")
        if changed:
            gh_save(q)
        return "auth failed"

    raw = c.get_json(LKH + TIMETABLE_API)
    classes = parse_timetable(raw)
    if not classes:
        log.info("пары не распознаны, структура: %s",
                 json.dumps(_shape(raw), ensure_ascii=False)[:1500])
        return "no classes parsed"

    log.info("пар сегодня: %d (%s)", len(classes),
             "; ".join(x["start"] for x in classes))
    if decide_and_notify(classes, q):
        if not gh_save(q):
            return "queue save failed"
    return "ok: %d classes" % len(classes)


def _shape(node, depth=0):
    if depth > 6:
        return "..."
    if isinstance(node, dict):
        return {k: _shape(v, depth + 1) for k, v in list(node.items())[:40]}
    if isinstance(node, list):
        return {"list[%d]": _shape(node[0], depth + 1) if node else None}
    if isinstance(node, str):
        return "str(%d)" % len(node)
    return node
