# -*- coding: utf-8 -*-
"""Интерактивный бот: /start → выбор факультета и группы, /today /tomorrow
/week /stop. Long-polling, stdlib. Запускается под systemd."""

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from common import (  # noqa: E402
    Backoff, CaptchaNeeded, SchedClient, add_user, db, del_user,
    auto_captcha_allow, day_label, get_user, manual_unlock_allow,
    needs_refresh, classes_on_date, touch_user,
    pending_clear, pending_is,
    pending_set, set_enabled, users_all, week_type_label,
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


def schedule_command(send, conn, chat_id, gid, gname, mode, day=None):
    """Ни одна команда не ждёт сайт: мгновенный ответ из кэша или
    «загружаю…», а получение данных — в фоновом потоке."""
    if needs_refresh(gid, day=day):
        send(chat_id, "⏳ Загружаю свежее расписание с сайта университета…")

    def work():
        wconn = db()
        try:
            week = None
            # терпеливо: сайт иногда "охлаждается" — фон дожимает до 20 минут
            # и сам доставляет расписание, пользователю не нужно переспрашивать
            for attempt in range(20):
                try:
                    week = get_week(gid, day=day, max_age_min=None)
                    break
                except CaptchaNeeded:
                    if auto_captcha_allow():
                        send(chat_id, "🔒 Сайт просит подтверждение — "
                                      "решите капчу:")
                        cmd_unlock(send, chat_id, manual=False)
                    else:
                        send(chat_id, "🔒 Сайт просит капчу. Автопоказ на "
                                      "сегодня исчерпан — /unlock вручную.")
                    return
                except Backoff:
                    last = "сайт недоступен"
                except Exception as e:
                    last = str(e)[:60]
                if attempt < 19:
                    time.sleep(60)
            if week is None:
                send(chat_id, "⏳ Сайт университета не отвечал 20 минут — "
                              "попробуйте позже (напоминания работают "
                              "независимо от этого).")
                return

            if mode == "week":
                days = sorted({l["wd"] for l in week.get("lessons", [])
                               if not l.get("cancelled")})
                wd_by_num = {d["weekDayNumber"]: d["date"]
                             for d in week.get("week_days", [])}
                wt = week_type_label(week)
                out = ["🗓 Неделя%s, группа %s"
                       % (" (%s)" % wt if wt else "", gname)]
                for wd in days:
                    date_str = wd_by_num.get(wd)
                    if not date_str:
                        continue
                    dd = __import__("datetime").datetime.strptime(
                        date_str, "%d-%m-%Y")
                    out.append("")
                    out.append("— %s —" % day_label(dd))
                    for l in classes_for(week, dd):
                        if l.get("cancelled"):
                            out.append("❌ %s отменена (%s)"
                                       % (l["subject"], l["start"]))
                        else:
                            out.append("• "
                                       + __import__("common").format_class(l))
                send(chat_id, "\n".join(out)
                     or "Расписание на неделю пустое.")
                return

            d = now() if mode == "today" else now() + timedelta(days=1)
            wt = week_type_label(week)
            header = "📅 %s, группа %s" % (day_label(d), gname)
            if wt:
                header += " — неделя %s" % wt
            send(chat_id, lessons_text(classes_for(week, d), header))
        finally:
            wconn.close()

    threading.Thread(target=work, daemon=True).start()


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


def _retry_validation(send, chat_id, code, gid):
    """Фоновый авто-повтор проверки кода, когда сайт её придавил (429)."""
    c = SchedClient()
    for _ in range(3):
        time.sleep(45)
        try:
            resp = c.post_json(
                "schedule/api/api.php?act=Captcha&method=validateCaptcha",
                {"key": code})
        except Exception:
            continue
        if resp.get("state") is True:
            pending_clear()
            send(chat_id, "✅ Капча принята! Проверяю доступ к расписанию…")
            try:
                week = get_week(gid, force=True, max_age_min=1)
                send(chat_id, "🎉 Готово — расписание снова читается "
                              "(занятий на этой неделе: %d)."
                     % len(week.get("lessons", [])))
            except Backoff:
                send(chat_id, "Капча принята, но сайт пока ограничивает — "
                              "система повторит сама, ждать не нужно.")
            except Exception as e:
                send(chat_id, "Капча принята, но проверка расписания "
                              "ошиблась: %s. Повторит сама позже." % e)
            return
        send(chat_id, "❌ Код не подошёл — вот новая картинка:")
        cmd_unlock(send, chat_id, manual=False)
        return
    send(chat_id, "Сайт не дал проверить код за 2 минуты. "
                  "Отправьте /unlock чуть позже.")


def check_captcha_answer(send, conn, chat_id, code):
    c = SchedClient()
    try:
        resp = c.post_json(
            "schedule/api/api.php?act=Captcha&method=validateCaptcha",
            {"key": code.strip()})
    except urllib.error.HTTPError as e:
        if e.code == 429:
            send(chat_id, "⏳ Код получен, но сайт проверяет слишком часто — "
                          "повторю автоматически через минуту, "
                          "ничего не делайте.")
            u = get_user(conn, chat_id)
            gid = u[2] if u else 10706
            threading.Thread(target=_retry_validation,
                             args=(send, chat_id, code.strip(), gid),
                             daemon=True).start()
            return
        send(chat_id, "⚠️ Ошибка проверки (HTTP %s). Вот новая капча:"
             % e.code)
        cmd_unlock(send, chat_id, manual=False)
        return
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



def activity_label(last_seen):
    if not last_seen:
        return "не пользовался"
    try:
        dt = __import__("datetime").datetime.fromisoformat(last_seen)
    except ValueError:
        return "не пользовался"
    days = (now().date() - dt.date()).days
    if days <= 0:
        return "активен сегодня"
    if days == 1:
        return "был вчера"
    return "был %d дн. назад" % days


def render_panel(conn):
    rows = users_all(conn)
    on = sum(1 for r in rows if r[4])
    active = sum(1 for r in rows if r[5] and __import__("datetime")
                 .datetime.fromisoformat(r[5]).date() == now().date())
    lines = ["👑 Админ-панель\n",
             "Участников: %d | получают: %d | отключено: %d | "
             "активны сегодня: %d" % (len(rows), on, len(rows) - on, active),
             ""]
    kb = []
    for chat_id, username, gid, gname, enabled, last_seen, notified in rows:
        name = "@" + username if username else str(chat_id)
        lines.append("%s %s — %s" % ("✅" if enabled else "⏸", name, gname))
        lines.append("     %s • уведомлений: %d"
                     % (activity_label(last_seen), notified or 0))
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
    if user:
        touch_user(conn, chat_id)

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
        schedule_command(send, conn, chat_id, user[2], user[3], "today")
    elif text.startswith("/tomorrow") and user:
        schedule_command(send, conn, chat_id, user[2], user[3], "tomorrow",
                         day=now() + timedelta(days=1))
    elif text.startswith("/week") and user:
        schedule_command(send, conn, chat_id, user[2], user[3], "week")
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
    if get_user(conn, chat_id):
        touch_user(conn, chat_id)
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
        if kb and len(kb["inline_keyboard"]) > 1:  # есть группы + кнопка назад
            send(chat_id, "Выберите группу:", kb)
        elif kb:
            send(chat_id, "У этого факультета нет групп в расписании.")
        else:
            send(chat_id, "⏳ Не удалось получить список групп, "
                          "попробуйте позже.")
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
        # карточка-подсказка: старая открепляется, новая закрепляется
        try:
            tg("unpinAllChatMessages", TOKEN, chat_id=chat_id)
        except Exception as e:
            log.warning("unpin не удался: %s", e)
        card = send(chat_id, PIN_CARD)
        if card and card.get("message_id"):
            try:
                tg("pinChatMessage", TOKEN, chat_id=chat_id,
                   message_id=card["message_id"])
            except Exception as e:
                log.warning("pin не удался: %s", e)
        schedule_command(send, conn, chat_id, gid, code, "today")


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

    PENDING_OUT = CACHE_DIR / "pending_sends.json"

    def send(chat_id, text, reply_markup=None):
        params = {"chat_id": chat_id, "text": text}
        if reply_markup:
            params["reply_markup"] = reply_markup
        # одна быстрая попытка; при сбое — в очередь ретранслятора
        # (Mac доставит за ~2 минуты). Никаких 30-секундных висений.
        try:
            return tg("sendMessage", TOKEN, **params)
        except Exception as e:
            log.warning("sendMessage не удался: %s — в очередь", e)
            try:
                q = json.loads(PENDING_OUT.read_text(encoding="utf-8")) \
                    if PENDING_OUT.exists() else []
                q.append({"chat_id": chat_id, "text": text})
                PENDING_OUT.write_text(
                    json.dumps(q[-500:], ensure_ascii=False),
                    encoding="utf-8")
            except Exception as e2:
                log.warning("очередь тоже не удалась: %s", e2)
            return None
        # канал мёртв — сообщение в очередь, её разносит Mac-ретранслятор
        try:
            q = json.loads(PENDING_OUT.read_text(encoding="utf-8")) \
                if PENDING_OUT.exists() else []
            if not isinstance(q, list):
                q = []
            q.append({"chat_id": chat_id, "text": text})
            PENDING_OUT.write_text(
                json.dumps(q[-500:], ensure_ascii=False), encoding="utf-8")
        except Exception as e2:
            log.warning("очередь тоже не удалась: %s", e2)

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

    sem = threading.Semaphore(5)  # не более 5 одновременных обработок

    def process_update(upd):
        """Каждый апдейт — в отдельном потоке со СВОИМ соединением базы:
        медленная операция одного пользователя не задерживает остальных."""
        with sem:
            uconn = db()
            try:
                if "message" in upd:
                    m = upd["message"]
                    chat_id = m.get("chat", {}).get("id")
                    text = (m.get("text") or "").strip()
                    # мгновенная плашка «жив» на каждую команду
                    if (chat_id and text.startswith("/")
                            and get_user(uconn, chat_id)):
                        try:
                            send(chat_id, "⚙️ Принял, работаю…")
                        except Exception:
                            pass
                    handle_message(send, uconn, m)
                elif "callback_query" in upd:
                    handle_callback(send, uconn, upd["callback_query"])
            except Exception:
                log.exception("ошибка обработки апдейта")
            finally:
                uconn.close()

    tg_fail_streak = 0
    probe_counter = 0
    flag_path = CACHE_DIR / "tg_down"
    spool_dir = CACHE_DIR / "updates_in"
    spool_dir.mkdir(exist_ok=True)

    def drain_spool():
        """Обработка обновлений, relay-нутых с Mac (пока прямой канал закрыт)."""
        import glob as _g
        for f in sorted(_g.glob(str(spool_dir / "*.json"))):
            try:
                upds = json.loads(Path(f).read_text(encoding="utf-8"))
            except Exception:
                os.unlink(f)
                continue
            for upd in (upds if isinstance(upds, list) else []):
                try:
                    process_update(upd)
                except Exception:
                    log.exception("spool update")
            os.unlink(f)

    log.info("бот запущен, offset=%d", offset)
    while True:
        try:
            drain_spool()
        except Exception:
            log.exception("drain_spool")

        # при устойчивом сбое канал приёма берёт на себя Mac (флаг tg_down);
        # VPS лишь изредка пробует вернуть канал себе
        if flag_path.exists():
            probe_counter += 1
            if probe_counter % 10:  # 9 циклов из 10 — ждём, 1 — пробуем
                drain_spool()
                time.sleep(2)
                continue

        try:
            res = tg("getUpdates", TOKEN, offset=offset, timeout=25,
                     allowed_updates=["message", "callback_query"])
            tg_fail_streak = 0
            try:
                flag_path.unlink()
            except OSError:
                pass
        except Exception as e:
            log.warning("getUpdates: %s", e)
            tg_fail_streak += 1
            if tg_fail_streak >= 5:
                try:
                    flag_path.write_text(t.isoformat())
                except OSError:
                    pass
            time.sleep(1)
            continue
        for upd in res or []:
            offset = upd["update_id"] + 1
            offset_path.write_text(str(offset))
            threading.Thread(target=process_update, args=(upd,),
                             daemon=True).start()


if __name__ == "__main__":
    main()
