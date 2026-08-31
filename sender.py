#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Отправитель уведомлений для GitHub Actions.

Яндекс-функция (gubkin-notify) складывает сообщения в queue.json —
она видит lk.gubkin.ru, но не может писать в Telegram. Этот скрипт
запускается воркфлоу каждые 5 минут: читает очередь, отправляет всё
ожидающее через Telegram Bot API и помечает отправленное.

Ошибки доставки не смертельны: сообщение остаётся в очереди
и уйдёт со следующим запуском.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

QUEUE = Path(__file__).resolve().parent / "queue.json"
TGH = "https://api.telegram.org/bot%s/sendMessage"
TIMEOUT = 20


def tg_send(token, chat, text):
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text}
    ).encode("utf-8")
    req = urllib.request.Request(TGH % token, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("ok") is not True:
        raise RuntimeError("Telegram ответил: %s" % body)


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]

    if not QUEUE.exists():
        print("queue.json ещё нет — нечего отправлять")
        return 0

    q = json.loads(QUEUE.read_text(encoding="utf-8"))
    sent = q.setdefault("sent", [])
    pending = q.setdefault("pending", [])
    remaining, delivered = [], 0

    for m in pending:
        if m.get("id") in sent:  # уже доставлено раньше
            continue
        try:
            tg_send(token, chat, m["text"])
            sent.append(m["id"])
            delivered += 1
            print("отправлено:", m["id"])
        except Exception as e:  # останется в очереди до следующего раза
            print("ошибка отправки %s: %s" % (m.get("id"), e))
            remaining.append(m)

    if delivered:
        q["pending"] = remaining
        q["sent"] = sent[-500:]  # чтобы файл не рос бесконечно
        QUEUE.write_text(
            json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print("доставлено сообщений:", delivered)
    else:
        print("новых сообщений нет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
