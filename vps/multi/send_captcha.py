# -*- coding: utf-8 -*-
"""Разослать свежую капчу указанным чатам (ручной запуск на сервере).
Использование: python3 send_captcha.py <chat_id> [chat_id...]"""
import sys

sys.path.insert(0, "/opt/gubkin/multi")
from common import SchedClient, pending_set, tg, load_config

CFG = load_config()
TOKEN = CFG["telegram_bot_token"]

c = SchedClient()
c.visit()
img = c.raw("schedule/api/api.php?act=Captcha&method=generateCaptcha")
c._save()

for chat in sys.argv[1:]:
    try:
        tg("sendPhoto", TOKEN, files={"photo": ("captcha.jpg", img)},
           chat_id=int(chat),
           caption="🔒 Сайт университета просит подтверждение. Ответьте "
                   "кодом с картинки одним сообщением — это вернёт "
                   "напоминания всем.")
        pending_set(int(chat))
        print("отправлено:", chat)
    except Exception as e:
        print("ошибка", chat, ":", str(e)[:120])
