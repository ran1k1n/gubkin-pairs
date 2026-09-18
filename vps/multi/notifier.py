# -*- coding: utf-8 -*-
"""Рассыльщик уведомлений (cron каждые 5 минут).

Для каждой группы с подписчиками:
  - кэш недели обновляется 1 раз в день + каждые 3 часа днём (отмены);
  - 07:30–08:30 МСК — сводка пар на день всем пользователям группы;
  - за 15 минут до пары (окно до start+5) — напоминание;
  - отмена/перенос пары (по diff кэшей) — разовое сообщение.

Дедупликация по ключам в cache/sent.json, отправка через Sender
(с локальной очередью повторов).
"""

import json
import logging
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    auto_captcha_allow, Backoff, CaptchaNeeded, CACHE_DIR, Sender,
    SchedClient, classes_on_date, db, get_week, hhmm,
    lessons_text, load_config, needs_refresh, now, pending_set, tg,
    users_by_group, day_label, week_key)

SENT_PATH = CACHE_DIR / "sent.json"
REFRESH_MIN = 180  # днём обновлять кэш раз в 3 часа ради отмен
DAY_START, DAY_END = 7, 20  # часы, в которые имеет смысл обновлять кэш

log = logging.getLogger("gubkin-notify")


def load_sent():
    try:
        data = json.loads(SENT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    today = now().date().isoformat()
    return {k: v for k, v in data.items() if k.endswith(today) or ":" not in k}


def save_sent(sent):
    SENT_PATH.write_text(json.dumps(sent), encoding="utf-8")


def lesson_key(l, with_time=True):
    base = "%s:%s" % (l.get("wd"), (l.get("subject") or "").lower()[:40])
    if with_time:
        base += ":%s" % l.get("start")
    return base


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(str(CACHE_DIR / "notify.log"),
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    CFG = load_config()
    sender = Sender(CFG["telegram_bot_token"])
    conn = db()
    groups = users_by_group(conn)
    if not groups:
        return
    t = now()
    today = t.date()
    sent = load_sent()
    today_key = today.isoformat()

    # напоминания об оплате сервера владельцу: за неделю, за день и в день
    rent_day = int(CFG.get("rent_reminder_day", 1))
    if CFG.get("admin_chat_id"):
        import calendar as _cal
        days_in_month = _cal.monthrange(t.year, t.month)[1]
        due = t.date().replace(day=rent_day) if rent_day <= days_in_month else None
        if due:
            left = (due - t.date()).days
            msgs = {
                7: "🗓 Через неделю (%s) — оплата сервера Aeza (800₽). "
                   "Не забудьте пополнить баланс." % due.strftime("%d.%m"),
                1: "⚠️ Завтра оплата сервера Aeza (800₽) — "
                   "пополните баланс уже сегодня.",
                0: "💳 Сегодня пора оплатить сервер Aeza (800₽). "
                   "Пополните баланс, чтобы напоминания о парах не остановились.",
            }
            if left in msgs:
                rkey = "rent%s:%s" % (left, due.isoformat())
                if t.hour >= 10 and rkey not in sent:
                    sent[rkey] = 1
                    sender.send(int(CFG["admin_chat_id"]), msgs[left])

    # сначала та группа, чьи данные самые старые (равный доступ к окнам)
    def cache_age(gid):
        import common
        c = common.load_sched(gid)
        try:
            return __import__("datetime").datetime.fromisoformat(
                (c or {}).get("fetched_at", "")).timestamp()
        except (ValueError, TypeError):
            return 0

    ordered = sorted(groups.items(), key=lambda kv: cache_age(kv[0]))
    first = True
    for gid, info in ordered:
        if not first:
            time.sleep(5)  # щадящий интервал: WAF университета чувствителен
        first = False
        chats = info["chats"]
        gname = info["name"]
        # --- кэш недели
        need_refresh = False
        cache = None
        try:
            import common
            cache = common.load_sched(gid)
            if cache:
                fetched = cache.get("fetched_at", "")
                try:
                    fdt = __import__("datetime").datetime.fromisoformat(fetched)
                    age_min = (t - fdt).total_seconds() / 60
                    same_day = fdt.date() == today
                except ValueError:
                    age_min, same_day = 10**9, False
                # РОВНО 2 захода на сайт в сутки на группу (щадяще для WAF):
                # утро — первое обновление дня, 13:00-13:59 — проверка отмен
                if not same_day:
                    need_refresh = 6 <= t.hour < 20 or t.hour < 2
                elif (13 <= t.hour < 14
                      and cache.get("pm_fetch") != today):
                    need_refresh = True
                else:
                    need_refresh = False
            else:
                need_refresh = 6 <= t.hour < 20 or t.hour < 2
            week = get_week(gid,
                            force=need_refresh,
                            max_age_min=None if need_refresh else REFRESH_MIN)
        except CaptchaNeeded:
            # разослать капчу всем подписчикам: любой введённый код
            # разблокирует общую сессию для всей системы
            ckey = "captcha_sent:%s" % today_key
            if ckey not in sent:
                sent[ckey] = 1
                if not auto_captcha_allow():
                    all_chats = [c for info in groups.values()
                                 for c in info["chats"]]
                    sender.broadcast(
                        all_chats,
                        "🔒 Сайт университета просит капчу — уведомления "
                        "приостановлены. Отправьте /unlock и введите код.")
                    log.warning("лимит автокапчи исчерпан — просим /unlock")
                    continue
                try:
                    c = SchedClient()
                    c.visit()
                    img = c.raw("schedule/api/api.php?act=Captcha"
                                "&method=generateCaptcha")
                    c._save()
                except Exception as e:
                    log.error("капчу получить не удалось: %s", e)
                    continue
                for info in groups.values():
                    for chat in info["chats"]:
                        try:
                            tg("sendPhoto", CFG["telegram_bot_token"],
                               files={"photo": ("captcha.jpg", img)},
                               chat_id=chat,
                               caption="🔒 Сайт университета просит "
                                       "подтверждение. Ответьте кодом с "
                                       "картинки одним сообщением — это "
                                       "вернёт напоминания всем.")
                            pending_set(chat)
                        except Exception as e:
                            log.warning("капча не доставлена %s: %s",
                                        chat, e)
            log.warning("группа %s: нужна капча — разослана", gid)
            continue
        except Backoff:
            # окно может открыться через секунды — одна повторная попытка
            time.sleep(8)
            try:
                week = get_week(gid, force=need_refresh,
                                max_age_min=None if need_refresh
                                else REFRESH_MIN)
            except Exception:
                log.warning("группа %s: сайт недоступен, пропуск", gid)
                continue
        except Exception as e:
            log.error("группа %s: %s", gid, e)
            continue

        if 13 <= t.hour < 14:
            try:
                import common
                c2 = common.load_sched(gid)
                if c2:
                    c2["pm_fetch"] = today
                    common.save_sched(gid, c2)
            except Exception:
                pass

        lessons_today = classes_on_date(week, t)
        today_wd = None
        for d in week.get("week_days", []):
            if d.get("date") == t.strftime("%d-%m-%Y"):
                today_wd = d.get("weekDayNumber")
                break

        # --- сравнение с утренним снимком: отмены, переносы, добавления
        prev = cache.get("prev_lessons") if cache else None
        if prev:
            prev_today = [l for l in prev
                          if l.get("wd") == today_wd]
            prev_map = {lesson_key(l, with_time=False): l
                        for l in prev_today}
            cur_map = {lesson_key(l, with_time=False): l
                       for l in lessons_today}
            for k, old in prev_map.items():
                new = cur_map.get(k)
                if new is None:
                    key = "gone:%s:%s:%s" % (gid, today_key, k)
                    if key not in sent and not old.get("cancelled"):
                        sent[key] = 1
                        sender.broadcast(chats,
                            "➖ Пара исчезла из расписания (%s): %s"
                            % (old.get("start"), format_simple(old)))
                    continue
                if not old.get("cancelled") and new.get("cancelled"):
                    key = "cancel:%s:%s:%s" % (gid, today_key, k)
                    if key not in sent:
                        sent[key] = 1
                        sender.broadcast(chats,
                            "❌ Отменена пара %s\n%s"
                            % (new.get("start"), format_simple(new)))
                elif old.get("start") != new.get("start"):
                    key = "move:%s:%s:%s" % (gid, today_key, k)
                    if key not in sent:
                        sent[key] = 1
                        sender.broadcast(chats,
                            "🔁 Пару перенесли: было %s, стало %s\n%s"
                            % (old.get("start"), new.get("start"),
                               format_simple(new)))
                elif not old.get("changed") and new.get("changed"):
                    key = "chg:%s:%s:%s" % (gid, today_key, k)
                    if key not in sent:
                        sent[key] = 1
                        sender.broadcast(chats,
                            "⚠️ Изменения в паре %s\n%s"
                            % (new.get("start"), format_simple(new)))
            for k, new in cur_map.items():
                if k not in prev_map and not new.get("cancelled"):
                    key = "add:%s:%s:%s" % (gid, today_key, k)
                    if key not in sent:
                        sent[key] = 1
                        sender.broadcast(chats,
                            "➕ Добавлена пара %s\n%s"
                            % (new.get("start"), format_simple(new)))

        # предзагрузка следующей недели: в воскресенье «завтра» уже
        # относится к новой неделе, и /tomorrow должен работать
        tomorrow = t + timedelta(days=1)
        if week_key(tomorrow) != week_key(t) and needs_refresh(gid, day=tomorrow):
            try:
                get_week(gid, day=tomorrow, force=True, max_age_min=None)
            except (Backoff, CaptchaNeeded):
                pass
            except Exception as e:
                log.warning("предзагрузка %s: %s", gid, e)

        # поздняя сводка: если утреннее окно пропущено (блок сайта),
        # а данные за сегодня только что добыты — шлём сразу
        skey_late = "summary:%s:%s" % (gid, today_key)
        if (same_day and skey_late not in sent and skey_late not in sent
                and "summary:%s:%s" % (gid, today_key) not in sent
                and t.hour >= 9):
            sent[skey_late] = 1
            sender.broadcast(chats, lessons_text(
                lessons_today,
                "🌅 Пары на сегодня, группа %s (с задержкой — сайт был "
                "недоступен утром)" % gname))

        live = [l for l in lessons_today if not l.get("cancelled")]

        # --- утренняя сводка
        skey = "summary:%s:%s" % (gid, today_key)
        summary_dt = t.replace(hour=7, minute=30, second=0, microsecond=0)
        if (summary_dt <= t < summary_dt + timedelta(minutes=60)
                and skey not in sent):
            sent[skey] = 1
            sender.broadcast(
                chats,
                lessons_text(lessons_today,
                             "🌅 Пары на сегодня, группа %s" % gname))

        # --- напоминания за 15 минут
        lead = CFG.get("minutes_before_class", 15)
        for i, l in enumerate(live):
            hm = hhmm(l["start"])
            if not hm:
                continue
            start_dt = t.replace(hour=hm[0], minute=hm[1], second=0,
                                 microsecond=0)
            alert_dt = start_dt - timedelta(minutes=lead)
            akey = "alert:%s:%s:%s" % (gid, today_key, l["start"])
            if alert_dt <= t < start_dt + timedelta(minutes=5) \
                    and akey not in sent:
                sent[akey] = 1
                if t >= start_dt:
                    msg = "🔔 Уже началась (%s): %s" % (
                        l["start"], format_simple(l))
                else:
                    left = int((start_dt - t).total_seconds() // 60)
                    msg = "🔔 Через %d мин, в %s: %s" % (
                        left, l["start"], format_simple(l))
                sender.broadcast(chats, msg)

    save_sent(sent)


def format_simple(l):
    parts = [l["subject"]]
    if l.get("kind"):
        parts.append("(%s)" % l["kind"])
    extra = []
    if l.get("room"):
        extra.append("ауд. " + l["room"])
    if l.get("teacher"):
        extra.append(l["teacher"])
    if extra:
        parts.append(", ".join(extra))
    return " ".join(parts)


if __name__ == "__main__":
    main()
