#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Напоминания о парах из ЛК РГУ нефти и газа им. Губкина — облачная версия
для GitHub Actions (не зависит от домашнего компьютера).

Отличия от локальной версии:
  - настройки читаются из переменных окружения (GitHub Secrets), не из config.json;
  - окна отправки расширены, потому что GitHub Actions иногда запаздывает
    на несколько минут: сводка — в течение часа после 07:30, напоминание —
    в любой момент между «за 15 минут» и началом пары;
  - состояние (что уже отправлено) лежит в sent_state.json и коммитится
    воркфлоу обратно в репозиторий.

Переменные окружения:
  LK_LOGIN, LK_PASSWORD          — данные входа в lk.gubkin.ru (Secrets)
  TELEGRAM_BOT_TOKEN             — токен бота (Secrets)
  TELEGRAM_CHAT_ID               — chat_id (Secrets)
  TIMETABLE_API                  — необязательно, путь API расписания
  MORNING_SUMMARY_TIME           — необязательно, по умолчанию 07:30
  MINUTES_BEFORE_CLASS           — необязательно, по умолчанию 15
  TZ                             — Europe/Moscow (задаёт воркфлоу)

Запуск вручную: python3 gubkin_notify.py [--test-push|--dry-run|--dump]
"""

import http.cookiejar
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "sent_state.json"
RAW_DUMP_PATH = BASE_DIR / "last_timetable_raw.json"
LK_BASE = "https://lk.gubkin.ru/"
HTTP_TIMEOUT = 20

log = logging.getLogger("gubkin")


def env(name, default=""):
    return os.environ.get(name, default)


def load_state():
    today = date.today().isoformat()
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "sent": []}
    return state


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def parse_hhmm(s, default=None):
    m = re.match(r"^(\d{1,2}):(\d{2})", str(s).strip())
    if not m:
        return default
    return int(m.group(1)), int(m.group(2))


# ---------------------------------------------------------------- клиент ЛК

class LkClient:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        opener.addheaders = [
            ("Accept", "application/json"),
            ("Content-Type", "application/json"),
            ("User-Agent", "gubkin-notify/2.0"),
        ]
        self.opener = opener

    def api_post(self, path, payload):
        url = urllib.parse.urljoin(LK_BASE, path)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def api_get(self, path):
        url = urllib.parse.urljoin(LK_BASE, path)
        req = urllib.request.Request(url, method="GET")
        with self.opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def login(self, login_value, password):
        if not login_value or not password:
            log.error("нет LK_LOGIN/LK_PASSWORD — войти не могу")
            return False
        field = int(login_value) if str(login_value).isdigit() else login_value
        resp = self.api_post(
            "api/api.php?module=auth&method=login",
            {"login": field, "password": password, "rememberMe": 1},
        )
        if resp.get("success") is not True:
            log.error("вход не удался: %s", resp.get("reason"))
            return False
        log.info("успешный вход в ЛК")
        return True

    def timetable(self, path):
        try:
            return self.api_get(path), None
        except urllib.error.URLError as e:
            return None, "сеть/LK недоступны: %s" % e
        except ValueError as e:
            return None, "API вернул не-JSON: %s" % e


# ---------------------------------------------------------------- расписание

def parse_timetable(raw):
    """Сырой ответ API -> список пар (эвристика; уточняется по дампу)."""
    classes = []

    def walk(node):
        if isinstance(node, dict):
            keys = {k.lower(): k for k in node}
            start = None
            for cand in ("start", "starttime", "begintime", "время", "начало",
                         "time_from", "from", "lessonstart"):
                if cand in keys:
                    start = parse_hhmm(node[keys[cand]])
                    if start:
                        break
            subject = None
            for cand in ("subject", "discipline", "name", "title", "предмет",
                         "дисциплина", "lesson", "lessonname"):
                if cand in keys and node[keys[cand]]:
                    subject = str(node[keys[cand]])
                    break
            if start and subject:
                classes.append({
                    "start": "%02d:%02d" % start,
                    "subject": subject,
                    "room": _first(node, keys, "room", "auditory", "place",
                                   "аудитория", "кабинет", "location"),
                    "teacher": _first(node, keys, "teacher", "lecturer",
                                      "преподаватель"),
                    "kind": _first(node, keys, "kind", "type", "вид",
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


def _first(node, keys, *cands):
    for cand in cands:
        if cand in keys and node[keys[cand]]:
            return str(node[keys[cand]])
    return None


# ---------------------------------------------------------------- уведомления

def send_notification(title, message, dry_run=False):
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    if dry_run:
        log.info("[DRY-RUN] %s | %s", title, message.replace("\n", " / "))
        return True
    if not token or not chat_id:
        log.error("нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID")
        return False
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": "%s\n%s" % (title, message)}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("ok") is not True:
        log.error("Telegram ответил ошибкой: %s", body)
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


# ---------------------------------------------------------------- решение

def decide_and_notify(classes, state, dry_run=False):
    now = datetime.now()
    lead = int(env("MINUTES_BEFORE_CLASS", "15"))

    summary_h, summary_m = parse_hhmm(env("MORNING_SUMMARY_TIME", "07:30"), (7, 30))
    summary_dt = now.replace(hour=summary_h, minute=summary_m, second=0,
                             microsecond=0)
    sent = state.setdefault("sent", [])
    changed = False

    # сводка: любое срабатывание в течение часа после планового времени
    if summary_dt <= now < summary_dt + timedelta(minutes=60) and "summary" not in sent:
        if classes:
            lines = ["Пары на сегодня (%s):" % now.date().isoformat()]
            for c in classes:
                lines.append("%s — %s" % (c["start"], format_class(c)))
        else:
            lines = ["Пар сегодня нет — отдыхайте!"]
        send_notification("Расписание на день", "\n".join(lines), dry_run)
        sent.append("summary")
        changed = True

    # напоминание: окно от «за lead минут» до 5 минут после начала пары
    # (терпит задержки GitHub Actions), дубли отсекаются по ключу пары
    for i, c in enumerate(classes):
        sh, sm = parse_hhmm(c["start"])
        if sh is None:
            continue
        start_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        alert_dt = start_dt - timedelta(minutes=lead)
        key = "class:%d" % i
        if alert_dt <= now < start_dt + timedelta(minutes=5) and key not in sent:
            if now >= start_dt:
                msg = "Уже началась (%s): %s" % (c["start"], format_class(c))
            else:
                left = int((start_dt - now).total_seconds() // 60)
                msg = "В %s (через %d мин): %s" % (c["start"], left, format_class(c))
            send_notification("Скоро пара", msg, dry_run)
            sent.append(key)
            changed = True

    if changed:
        save_state(state)


def notify_auth_problem(state, dry_run):
    if "auth_fail" in state["sent"]:
        return
    send_notification(
        "ЛК Губкина: вход не удался",
        "Проверьте Secrets (LK_LOGIN/LK_PASSWORD) — напоминания о парах не работают.",
        dry_run,
    )
    state["sent"].append("auth_fail")
    save_state(state)


# ---------------------------------------------------------------- точка входа

def main():
    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if "--test-push" in args:
        ok = send_notification(
            "Тест уведомлений (облако)",
            "GitHub Actions доставил сообщение — всё работает без вашего Mac.",
            dry_run,
        )
        log.info("тестовое уведомление: %s", "отправлено" if ok else "НЕ отправлено")
        return 0 if ok else 1

    state = load_state()
    client = LkClient()
    if not client.login(env("LK_LOGIN"), env("LK_PASSWORD")):
        notify_auth_problem(state, dry_run)
        return 2

    path = env("TIMETABLE_API", "api/api.php?module=study&method=timetable")
    raw, err = client.timetable(path)
    if err:
        log.error("не удалось получить расписание: %s", err)
        return 3
    RAW_DUMP_PATH.write_text(
        json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    classes = parse_timetable(raw)
    if classes:
        detail = "; ".join(c["start"] + " " + c["subject"] for c in classes) \
            if env("VERBOSE_PERSONAL") else "; ".join(c["start"] for c in classes)
        log.info("пар сегодня: %d (%s)", len(classes), detail)
    else:
        # репозиторий публичный: в лог печатаем только структуру ответа,
        # без персональных значений (они остаются в last_timetable_raw.json
        # на недолговечном раннере)
        log.warning("пары не распознаны; структура ответа: %s",
                    json.dumps(_shape(raw), ensure_ascii=False)[:3000])
        return 4

    decide_and_notify(classes, state, dry_run)
    return 0


def _shape(node, depth=0):
    """Скелет JSON: типы и ключи, строковые значения схлопнуты."""
    if depth > 6:
        return "..."
    if isinstance(node, dict):
        return {k: _shape(v, depth + 1) for k, v in list(node.items())[:40]}
    if isinstance(node, list):
        return {"list[%d]": _shape(node[0], depth + 1) if node else None}
    if isinstance(node, str):
        return "str(%d)" % len(node)
    return node


if __name__ == "__main__":
    sys.exit(main())
