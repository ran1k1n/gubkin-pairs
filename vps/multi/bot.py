# -*- coding: utf-8 -*-
"""Интерактивный бот: /start → выбор факультета и группы, /today /tomorrow
/week /stop. Long-polling, stdlib. Запускается под systemd."""

import json
import logging
import sys
import time
import urllib.request
from datetime import timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Backoff, SchedClient, add_user, db, del_user, day_label, get_user,
    get_week, lessons_text, load_config, now, tg, CACHE_DIR)

log = logging.getLogger("gubkin-bot")

# chat_id -> True: ждём код капчи следующим сообщением
PENDING_CAPTCHA = {}

META_FACULTIES = CACHE_DIR / "meta_faculties.json"
META_GROUPS = CACHE_DIR / "meta_groups_%s.json"
META_TTL = 86400  # сутки


def cached_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if now().timestamp() - data.get("_ts", 0) < META_TTL:
            return data.get("data")
    except (OSError, ValueError):
        pass
    return None


def store_json(path, data):
    path.write_text(json.dumps({"_ts": now().timestamp(), "data": data}),
                    encoding="utf-8")


def faculties():
    data = cached_json(META_FACULTIES)
    if data:
        return data
    c = SchedClient()
    c.visit()
    raw = c.api("schedule/api/api.php?act=list&method=getFaculties")
    data = sorted(((r["id"], r["name"]) for r in raw.get("rows", [])),
                  key=lambda x: x[1])
    store_json(META_FACULTIES, data)
    return data


def faculty_groups(fid):
    path = META_GROUPS % fid
    data = cached_json(path)
    if data:
        return data
    c = SchedClient()
    c.visit()
    raw = c.api("schedule/api/api.php?act=list&method=getFacultyGroups"
                "&facultyId=%s" % fid)
    data = sorted(((r["id"], r["code"]) for r in raw.get("rows", [])),
                  key=lambda x: x[1])
    store_json(path, data)
    return data


WELCOME = (
    "👋 Это бот расписания занятий Губкинского университета.\n\n"
    "Я пришлю:\n"
    "• утром в 07:30 — пары на день;\n"
    "• за 15 минут до пары — напоминание;\n"
    "• сообщение, если пару отменили.\n\n"
    "Выберите факультет кнопкой ниже 👇"
)

PIN_CARD = (
    "📌 Меню бота расписания Губкинского\n\n"
    "📅 Расписание:\n"
    "/today — пары на сегодня\n"
    "/tomorrow — пары на завтра\n"
    "/week — пары на неделю\n\n"
    "⚙️ Настройки:\n"
    "/group — сменить группу\n"
    "/stop — отписаться\n\n"
    "🔧 Если бот пишет «сайт ограничивает запросы»:\n"
    "/unlock — разблокировка через капчу\n\n"
    "🤔 Как это работает:\n"
    "🌅 в 07:30 — сводка пар на день\n"
    "🔔 за 15 минут до пары — напоминание\n"
    "❌ сообщу об отмене или изменении"
)

BOT_COMMANDS = [
    {"command": "start", "description": "Подключиться и выбрать группу"},
    {"command": "today", "description": "Пары на сегодня"},
    {"command": "tomorrow", "description": "Пары на завтра"},
    {"command": "week", "description": "Расписание на неделю"},
    {"command": "group", "description": "Сменить группу"},
    {"command": "unlock", "description": "Разблокировать расписание (капча)"},
    {"command": "stop", "description": "Отписаться от уведомлений"},
]

HELP = PIN_CARD.replace("📌 ", "") + "\n\nНажмите /start для подключения."


def keyboard(rows_per=2):
    return {"inline_keyboard": [
        [{"text": t, "callback_data": d} for t, d in row]
        for row in rows_per and [] or []
    ]}


def kb_faculties():
    rows, row = [], []
    try:
        data = faculties()
    except Exception as e:
        log.error("faculties: %s", e)
        return None
    for fid, name in data:
        row.append({"text": name[:40], "callback_data": "f:%s" % fid})
        if len(row) == 1:
            rows.append(row)
            row = []
    return {"inline_keyboard": rows}


def kb_groups(fid):
    try:
        data = faculty_groups(fid)
    except Exception as e:
        log.error("groups %s: %s", fid, e)
        return None
    rows, row = [], []
    for gid, code in data:
        row.append({"text": code, "callback_data": "g:%s:%s" % (gid, code)})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ Факультеты", "callback_data": "back"}])
    return {"inline_keyboard": rows}


def send_week_preview(send, chat_id, group_id, group_name):
    try:
        week = get_week(group_id, max_age_min=30)
    except Backoff:
        send(chat_id, "⏳ Сайт университета временно ограничивает запросы — "
                      "попробуйте через полчаса.")
        return
    t = now()
    today_les = classes_for(week, t)
    send(chat_id, lessons_text(
        today_les, "📅 %s, группа %s" % (day_label(t), group_name)))


def classes_for(week, day):
    from common import classes_on_date
    return classes_on_date(week, day)


def send_photo(chat_id, img_bytes, caption):
    """sendPhoto с файлом через multipart (капча — локальный JPEG)."""
    boundary = "----gubkin%d" % now().timestamp()
    body = (
        ("--%s\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n%s\r\n"
         % (boundary, chat_id)).encode()
        + ("--%s\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n%s\r\n"
           % (boundary, caption)).encode()
        + ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; "
           "filename=\"captcha.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
           % boundary).encode()
        + img_bytes
        + ("\r\n--%s--\r\n" % boundary).encode()
    )
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendPhoto" % TOKEN, data=body)
    req.add_header("Content-Type",
                   "multipart/form-data; boundary=%s" % boundary)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.loads(resp.read().decode())
        return True
    except Exception as e:
        log.warning("sendPhoto: %s", e)
        return False


def cmd_unlock(send, chat_id):
    """Показать капчу университета и дождаться кода от пользователя."""
    try:
        c = SchedClient()
        c.visit()
        img = c.raw("schedule/api/api.php?act=Captcha&method=generateCaptcha")
        c._save()
    except Exception as e:
        send(chat_id, "Не удалось получить капчу с сайта: %s" % e)
        return
    if send_photo(chat_id, img,
                  "🔐 Сайт университета просит подтверждение, что вы человек. "
                  "Введите код с картинки одним сообщением (5+ символов):"):
        PENDING_CAPTCHA[chat_id] = True
    else:
        send(chat_id, "Не удалось отправить картинку, попробуйте ещё раз: /unlock")


def check_captcha_answer(send, conn, chat_id, code):
    c = SchedClient()
    try:
        resp = c.post_json(
            "schedule/api/api.php?act=Captcha&method=validateCaptcha",
            {"key": code.strip()})
    except Exception as e:
        send(chat_id, "Ошибка проверки: %s. Попробуйте ещё раз: /unlock" % e)
        PENDING_CAPTCHA.pop(chat_id, None)
        return
    if resp.get("state") is True:
        PENDING_CAPTCHA.pop(chat_id, None)
        send(chat_id, "✅ Капча принята! Проверяю доступ к расписанию…")
        user = get_user(conn, chat_id)
        gid = user[2] if user else 10706
        try:
            week = get_week(gid, force=True, max_age_min=1)
            send(chat_id, "🎉 Готово — расписание снова читается "
                          "(занятий на этой неделе: %d)."
                 % len(week.get("lessons", [])))
        except Backoff:
            send(chat_id, "Капча принята, но сайт пока снова ограничивает. "
                          "Система повторит автоматически, ждать не нужно.")
        except Exception as e:
            send(chat_id, "Капча принята, но при проверке расписания вышла "
                          "ошибка: %s. Она попробует сама позже." % e)
    else:
        send(chat_id, "❌ Код не подошёл. Вот новая картинка:")
        cmd_unlock(send, chat_id)


def handle_message(send, conn, msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    user = get_user(conn, chat_id)

    # ответ на капчу (если ждём код — любое не-командное сообщение это код)
    if PENDING_CAPTCHA.get(chat_id) and not text.startswith("/"):
        check_captcha_answer(send, conn, chat_id, text)
        return

    if text.startswith("/start"):
        if user:
            send(chat_id, "Вы уже подписаны на группу %s.\n\n%s"
                 % (user[3], HELP))
        else:
            kb = kb_faculties()
            if kb:
                send(chat_id, WELCOME, kb)
            else:
                send(chat_id, "⏳ Сайт университета не отвечает, попробуйте "
                              "через пару минут: /start")
    elif text.startswith("/today") and user:
        send_week_preview(send, chat_id, user[2], user[3])
    elif text.startswith("/tomorrow") and user:
        try:
            week = get_week(user[2], max_age_min=30)
        except Backoff:
            send(chat_id, "⏳ Сайт ограничивает запросы — попробуйте позже.")
            return
        d = now() + timedelta(days=1)
        send(chat_id, lessons_text(classes_for(week, d),
                                   "📅 Завтра, %s" % day_label(d)))
    elif text.startswith("/week") and user:
        try:
            week = get_week(user[2], max_age_min=60)
        except Backoff:
            send(chat_id, "⏳ Сайт ограничивает запросы — попробуйте позже.")
            return
        days = sorted({l["wd"] for l in week.get("lessons", [])
                       if not l.get("cancelled")})
        wd_by_num = {d["weekDayNumber"]: d["date"] for d in week.get("week_days", [])}
        out = ["🗓 Неделя, группа %s" % user[3]]
        for wd in days:
            date_str = wd_by_num.get(wd)
            if not date_str:
                continue
            dd = __import__("datetime").datetime.strptime(
                date_str, "%d-%m-%Y")
            les = classes_for(week, dd)
            out.append("")
            out.append("— %s —" % day_label(dd))
            for l in les:
                if l.get("cancelled"):
                    out.append("❌ %s отменена (%s)" % (l["subject"], l["start"]))
                else:
                    out.append("• " + __import__("common").format_class(l))
        send(chat_id, "\n".join(out) or "Расписание на неделю пустое.")
    elif text.startswith("/group"):
        kb = kb_faculties()
        if kb:
            send(chat_id, "Выберите факультет:", kb)
        else:
            send(chat_id, "⏳ Сайт университета не отвечает, попробуйте позже.")
    elif text.startswith("/unlock"):
        cmd_unlock(send, chat_id)
    elif text.startswith("/stop"):
        if user:
            del_user(conn, chat_id)
            send(chat_id, "Вы отписались. Вернуться: /start")
        else:
            send(chat_id, "Вы и не были подписаны 🙂")
    elif text.startswith("/help") or not text.startswith("/"):
        send(chat_id, HELP if user else "Нажмите /start для подключения.")


def handle_callback(send, conn, cb):
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    data = cb.get("data", "")
    if not chat_id:
        return
    try:
        tg("answerCallbackQuery", TOKEN, callback_query_id=cb["id"])
    except Exception:
        pass
    if data == "back":
        kb = kb_faculties()
        if kb:
            send(chat_id, "Выберите факультет:", kb)
        return
    if data.startswith("f:"):
        fid = data[2:]
        kb = kb_groups(fid)
        if kb:
            send(chat_id, "Выберите группу:", kb)
        else:
            send(chat_id, "⏳ Не удалось получить список групп, попробуйте позже.")
        return
    if data.startswith("g:"):
        rest = data[2:]
        gid_str, _, code = rest.partition(":")
        gid = int(gid_str)
        username = (cb.get("from") or {}).get("username") or ""
        add_user(conn, chat_id, username, gid, code)
        log.info("новый пользователь %s -> группа %s (%s)",
                 chat_id, gid, code)
        send(chat_id, "✅ Группа %s сохранена!\n\n"
                      "Утром в 07:30 пришлю пары на день, за 15 минут до "
                      "пары — напомню." % code)
        # карточка-подсказка, закрепляется в чате нового пользователя
        card = send(chat_id, PIN_CARD)
        if card and card.get("message_id"):
            try:
                tg("pinChatMessage", TOKEN, chat_id=chat_id,
                   message_id=card["message_id"])
            except Exception as e:
                log.warning("pin не удался: %s", e)
        send_week_preview(send, chat_id, gid, code)


def main():
    global TOKEN
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(str(CACHE_DIR / "bot.log"),
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    CFG = load_config()
    TOKEN = CFG["telegram_bot_token"]
    conn = db()

    offset_path = CACHE_DIR / "offset.txt"
    try:
        offset = int(offset_path.read_text().strip() or 0)
    except (OSError, ValueError):
        offset = 0

    def send(chat_id, text, reply_markup=None):
        params = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
        for attempt in range(3):
            try:
                return tg("sendMessage", TOKEN, **params)
            except Exception as e:
                log.warning("sendMessage: %s (попытка %d)", e, attempt + 1)
                time.sleep(2)

    # меню команд с подсказками — действует на всех пользователей,
    # текущих и будущих (Telegram показывает его в кнопке «Меню»)
    try:
        tg("setMyCommands", TOKEN, commands=BOT_COMMANDS)
        log.info("меню команд установлено")
    except Exception as e:
        log.warning("setMyCommands: %s", e)

    log.info("бот запущен, offset=%d", offset)
    while True:
        try:
            res = tg("getUpdates", TOKEN, offset=offset, timeout=25,
                     allowed_updates=["message", "callback_query"])
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(5)
            continue
        for upd in res or []:
            offset = upd["update_id"] + 1
            offset_path.write_text(str(offset))
            try:
                if "message" in upd:
                    handle_message(send, conn, upd["message"])
                elif "callback_query" in upd:
                    handle_callback(send, conn, upd["callback_query"])
            except Exception:
                log.exception("ошибка обработки апдейта")


if __name__ == "__main__":
    main()
