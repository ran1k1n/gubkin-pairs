# -*- coding: utf-8 -*-
"""Интерактивный бот: /start → выбор факультета и группы, /today /tomorrow
/week /stop. Long-polling, stdlib. Запускается под systemd."""

import json
import logging
import sys
import threading
import time
from datetime import timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Backoff, CaptchaNeeded, SchedClient, add_user, db, del_user,
    auto_captcha_allow, day_label, get_user, manual_unlock_allow,
    needs_refresh, pending_clear,
    pending_is, pending_set,
    set_enabled, users_all,
    get_week, lessons_text, load_config, now, tg, CACHE_DIR)

log = logging.getLogger("gubkin-bot")

# чаты, находящиеся в режиме переписки с поддержкой
SUPPORT_MODE = set()

META_FACULTIES = CACHE_DIR / "meta_faculties.json"
META_TTL = 604800  # 7 дней: справочники почти не меняются


def meta_groups_path(fid):
    return CACHE_DIR / ("meta_groups_%s.json" % fid)


def cached_json(path, allow_stale=False):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if allow_stale or now().timestamp() - data.get("_ts", 0) < META_TTL:
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
    path = meta_groups_path(fid)
    data = cached_json(path)
    if data:
        return data
    try:
        c = SchedClient()
        c.visit()
        raw = c.api("schedule/api/api.php?act=list&method=getFacultyGroups"
                    "&facultyId=%s" % fid)
        data = sorted(((r["id"], r["code"]) for r in raw.get("rows", [])),
                      key=lambda x: x[1])
        store_json(path, data)
    except Exception:
        # сайт недоступен — отдаём устаревший кэш, если он есть
        data = cached_json(path, allow_stale=True)
        if not data:
            raise
    return data
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
    "📌 Бот расписания Губкинского\n\n"
    "📅 Расписание:\n"
    "/today — на сегодня • /tomorrow — на завтра\n"
    "/week — на неделю\n"
    "/start — выбрать группу • /group — сменить\n"
    "/stop — отписаться\n"
    "/support — написать в поддержку\n\n"
    "🔧 Если сайт ограничивает запросы:\n"
    "пришлю капчу автоматически — просто введите код\n"
    "(неверный код — не страшно, пришлю следующую)\n"
    "/unlock — получить капчу вручную\n\n"
    "⏰ Уведомления:\n"
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
    if needs_refresh(group_id):
        send(chat_id, "⏳ Загружаю свежее расписание с сайта университета…")
    try:
        week = get_week(group_id, max_age_min=None)
    except CaptchaNeeded:
        if auto_captcha_allow():
            send(chat_id, "🔒 Сайт университета просит подтверждение — "
                          "решите капчу, это займёт 20 секунд:")
            cmd_unlock(send, chat_id, manual=False)
        else:
            send(chat_id, "🔒 Сайт просит капчу. Автопоказ на сегодня "
                          "исчерпан — отправьте /unlock вручную.")
        return
    except Backoff:
        send(chat_id, "⏳ Сайт университета недоступен — попробуйте позже.")
        return
    t = now()
    today_les = classes_for(week, t)
    send(chat_id, lessons_text(
        today_les, "📅 %s, группа %s" % (day_label(t), group_name)))


def classes_for(week, day):
    from common import classes_on_date
    return classes_on_date(week, day)


def send_photo(chat_id, img_bytes, caption):
    """sendPhoto через curl (multipart делает tg())."""
    try:
        tg("sendPhoto", TOKEN, files={"photo": ("captcha.jpg", img_bytes)},
           chat_id=chat_id, caption=caption)
        return True
    except Exception as e:
        log.warning("sendPhoto: %s", e)
        return False


def cmd_unlock(send, chat_id, manual=True):
    """Показать капчу университета и дождаться кода от пользователя.
    Ручные вызовы ограничены 15 раз в сутки, авто — своим лимитом (3)."""
    if manual and not manual_unlock_allow():
        send(chat_id, "Ручная разблокировка: лимит 15 раз в день исчерпан. "
                      "Попробуйте завтра.")
        return
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
        pending_set(chat_id)
    else:
        send(chat_id, "Не удалось отправить картинку, попробуйте ещё раз: /unlock")


def check_captcha_answer(send, conn, chat_id, code):
    c = SchedClient()
    try:
        resp = c.post_json(
            "schedule/api/api.php?act=Captcha&method=validateCaptcha",
            {"key": code.strip()})
    except Exception as e:
        # сеть дрогнула — капча не потрачена, просто вводим код заново
        send(chat_id, "⚠️ Не удалось проверить код (%s). Введите его ещё раз."
             % str(e)[:80])
        return
    if resp.get("state") is True:
        pending_clear()  # капча снята — всем больше не нужно отвечать
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
        # неверный код: показываем следующую капчу, пока не введёт верно;
        # повторные показы дневной лимит /unlock не тратят
        send(chat_id, "❌ Код не подошёл — попробуйте ещё раз:")
        cmd_unlock(send, chat_id, manual=False)



def render_panel(conn):
    rows = users_all(conn)
    on = sum(1 for r in rows if r[4])
    lines = ["👑 Админ-панель\n",
             "Участников: %d (получают: %d, отключено: %d)" % (len(rows), on,
                                                               len(rows) - on)]
    kb = []
    for chat_id, username, gid, gname, enabled in rows:
        name = "@" + username if username else str(chat_id)
        lines.append("%s %s — %s" % ("✅" if enabled else "⏸", name, gname))
        kb.append([
            {"text": "%s %s" % (name[:20], "выключить" if enabled else "включить"),
             "callback_data": "en:%s" % chat_id},
            {"text": "🗑", "callback_data": "del:%s" % chat_id},
        ])
    return "\n".join(lines), {"inline_keyboard": kb}


def is_admin(chat_id):
    return CFG.get("admin_chat_id") and str(chat_id) == str(CFG["admin_chat_id"])


def handle_callback_admin(send, conn, cb):
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    data = cb.get("data", "")
    if not chat_id or not is_admin(chat_id):
        return
    if data.startswith("en:"):
        target = int(data[3:])
        row = get_user(conn, target)
        if row:
            cur = conn.execute("SELECT enabled FROM users WHERE chat_id=?",
                               (target,)).fetchone()[0]
            set_enabled(conn, target, not cur)
    elif data.startswith("del:"):
        del_user(conn, int(data[4:]))
    else:
        return
    try:
        tg("answerCallbackQuery", TOKEN, callback_query_id=cb["id"])
    except Exception:
        pass
    if message_id:
        text, kb = render_panel(conn)
        try:
            tg("editMessageText", TOKEN, chat_id=chat_id,
               message_id=message_id, text=text, reply_markup=kb)
        except Exception as e:
            log.warning("editMessageText: %s")


def handle_message(send, conn, msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    user = get_user(conn, chat_id)

    # ответ на капчу (если ждём код — любое не-командное сообщение это код)
    if pending_is(chat_id) and not text.startswith("/"):
        check_captcha_answer(send, conn, chat_id, text)
        return

    # режим поддержки: пересылаем админу всё, включая фото и документы
    if chat_id in SUPPORT_MODE:
        if text.startswith("/cancel"):
            SUPPORT_MODE.discard(chat_id)
            send(chat_id, "Режим поддержки закрыт.")
            return
        who = "@" + user[1] if user and user[1] else str(chat_id)
        send(CFG["admin_chat_id"],
             "📨 Сообщение в поддержку от %s (id %s):" % (who, chat_id))
        try:
            tg("forwardMessage", TOKEN, chat_id=CFG["admin_chat_id"],
               from_chat_id=chat_id, message_id=msg["message_id"])
        except Exception as e:
            log.warning("forwardMessage: %s", e)
        send(chat_id, "✅ Передано в поддержку. Ответ придёт сюда.\n"
                      "Выйти из режима: /cancel")
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
        if needs_refresh(user[2]):
            send(chat_id, "⏳ Загружаю свежее расписание с сайта университета…")
        try:
            week = get_week(user[2], max_age_min=None)
        except CaptchaNeeded:
            if auto_captcha_allow():
                send(chat_id, "🔒 Сайт просит подтверждение — решите капчу:")
                cmd_unlock(send, chat_id, manual=False)
            else:
                send(chat_id, "🔒 Сайт просит капчу. Автопоказ на сегодня "
                              "исчерпан — отправьте /unlock вручную.")
            return
        except Backoff:
            send(chat_id, "⏳ Сайт недоступен — попробуйте позже.")
            return
        d = now() + timedelta(days=1)
        send(chat_id, lessons_text(classes_for(week, d),
                                   "📅 Завтра, %s" % day_label(d)))
    elif text.startswith("/week") and user:
        if needs_refresh(user[2]):
            send(chat_id, "⏳ Загружаю свежее расписание с сайта университета…")
        try:
            week = get_week(user[2], max_age_min=None)
        except CaptchaNeeded:
            if auto_captcha_allow():
                send(chat_id, "🔒 Сайт просит подтверждение — решите капчу:")
                cmd_unlock(send, chat_id, manual=False)
            else:
                send(chat_id, "🔒 Сайт просит капчу. Автопоказ на сегодня "
                              "исчерпан — отправьте /unlock вручную.")
            return
        except Backoff:
            send(chat_id, "⏳ Сайт недоступен — попробуйте позже.")
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
    elif text.startswith("/support"):
        if is_admin(chat_id):
            send(chat_id, "Вы и есть поддержка 🙂 Чтобы ответить участнику: "
                          "/reply <id> <текст>")
        else:
            SUPPORT_MODE.add(chat_id)
            send(chat_id, "✉️ Режим поддержки включён: напишите ваше "
                          "сообщение, я передам его администратору, ответ "
                          "придёт сюда.\nВыйти из режима: /cancel")
    elif text.startswith("/reply"):
        if is_admin(chat_id):
            rest = text[len("/reply"):].strip()
            target, _, msg_text = rest.partition(" ")
            if target.isdigit() and msg_text:
                send(int(target), "💬 Ответ поддержки:\n" + msg_text)
                send(chat_id, "Отправлено.")
            else:
                send(chat_id, "Формат: /reply <id> <текст>")
        else:
            send(chat_id, "Эта команда только для владельца бота.")
    elif text.startswith("/admin"):
        if is_admin(chat_id):
            text_panel, kb = render_panel(conn)
            send(chat_id, text_panel, kb)
        else:
            send(chat_id, "Эта команда только для владельца бота.")
    elif text.startswith("/stop"):
        if user:
            del_user(conn, chat_id)
            send(chat_id, "Вы отписались. Вернуться: /start")
            if not is_admin(chat_id):
                send(CFG["admin_chat_id"],
                     "➖ Участник @%s отписался" % (user[1] or user[0]))
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
    if data.startswith(("en:", "del:")):
        handle_callback_admin(send, conn, cb)
        return
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
        if is_admin(chat_id):
            send(chat_id, "Вы владелец этого бота — панель управления: /admin")
        else:
            send(CFG["admin_chat_id"],
                 "➕ Новый участник: @%s — группа %s" % (username or chat_id, code))
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
    global TOKEN, CFG
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

    # фоновый прогрев справочников (факультеты + группы всех факультетов),
    # чтобы нажатия кнопок откликались мгновенно
    def warm_meta():
        try:
            fs = faculties()
        except Exception as e:
            log.warning("прогрев: факультеты недоступны: %s", e)
            return
        for fid, _name in fs:
            try:
                faculty_groups(fid)
            except Exception as e:
                log.warning("прогрев: группы %s: %s", fid, e)
            time.sleep(1.5)
        log.info("справочники прогреты")

    threading.Thread(target=warm_meta, daemon=True).start()

    log.info("бот запущен, offset=%d", offset)
    while True:
        try:
            res = tg("getUpdates", TOKEN, offset=offset, timeout=25,
                     allowed_updates=["message", "callback_query"])
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(1)
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
