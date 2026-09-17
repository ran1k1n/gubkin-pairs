# -*- coding: utf-8 -*-
"""Санитар системы: сам проверяет здоровье и сообщает админу о проблемах.

Cron каждый час. Проверки:
  1. Сервис gubkin-bot активен (если нет — рестарт и алерт).
  2. Рассыльщик запускался недавно (cron жив).
  3. Кэш каждой подписанной группы свежий (иначе — «университет держит
     блок» алерт, не чаще раза в сутки).
  4. Зависший backoff старше 2 часов сбрасывается (самолечение).
В 21:00 — короткий ежедневный статус (всё ок / список проблем).
"""

import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CACHE_DIR, Sender, load_config, now, users_by_group)

import subprocess

STATE_PATH = CACHE_DIR / "monitor_state.json"
BOT_LOG = CACHE_DIR / "bot.log"
NOTIFY_LOG = CACHE_DIR / "notify.log"


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def mtime_age(path):
    try:
        return time.time() - Path(path).stat().st_mtime
    except OSError:
        return None


def main():
    CFG = load_config()
    sender = Sender(CFG["telegram_bot_token"])
    admin = int(CFG["admin_chat_id"]) if CFG.get("admin_chat_id") else None
    st = load_state()
    t = now()
    problems = []

    # 1) сервис бота
    r = subprocess.run(["systemctl", "is-active", "gubkin-bot"],
                       capture_output=True, timeout=10)
    if r.stdout.decode().strip() != "active":
        subprocess.run(["systemctl", "restart", "gubkin-bot"], timeout=30)
        problems.append("🚑 Сервис бота был выключен — перезапустил автоматически.")
    time.sleep(2)

    # 2) рассыльщик: cron.log свежее 20 минут
    age = mtime_age(CACHE_DIR / "cron.log")
    if age is not None and age > 1200:
        problems.append("⏰ Рассыльщик не запускался %d минут — проверьте cron."
                        % (age // 60))
    elif age is None:
        problems.append("⏰ Лог рассыльщика не найден.")

    # 3) свежесть кэша подписанных групп
    groups = users_by_group(__import__("common").db())
    stale = []
    for gid, info in groups.items():
        cache = None
        try:
            import common
            cache = common.load_sched(gid)
        except Exception:
            pass
        fetched = (cache or {}).get("fetched_at", "")
        try:
            fdt = __import__("datetime").datetime.fromisoformat(fetched)
            age_h = (t - fdt).total_seconds() / 3600
            if age_h > 36:
                stale.append("%s: данные %d ч давности"
                             % (info["name"], int(age_h)))
        except ValueError:
            stale.append("%s: кэш повреждён" % info["name"])
    if stale:
        key = "stale_alert"
        if not st.get(key) or (t - st[key]).total_seconds() > 86400:
            st[key] = t.isoformat()
            problems.append("🗓 Университет держит блокировку, данные "
                            "устарели:\n" + "\n".join("• " + s for s in stale)
                            + "\nДоставлю всё автоматически, как только "
                              "сайт ответит. Ускорить можно только сменой IP.")

    # 4) самолечение зависшего backoff
    try:
        fst = json.loads((CACHE_DIR / "fetch_state.json").read_text(
            encoding="utf-8"))
        nk = fst.get("next_ok")
        if nk:
            fdt = __import__("datetime").datetime.fromisoformat(nk)
            if (t - fdt).total_seconds() > 7200:
                (CACHE_DIR / "fetch_state.json").write_text(
                    '{"next_ok": null, "tries": 0}')
                problems.append("🔧 Сбросил зависшую блокировку ожидания "
                                "(висела больше 2 часов).")
    except (OSError, ValueError):
        pass

    # 5) ежедневный статус в 21:00
    day_key = "digest:%s" % t.date().isoformat()
    if t.hour == 21 and not st.get(day_key):
        st[day_key] = 1
        ok_msg = len(problems) == 0
        text = ("📊 Ежедневный статус бота расписания\n"
                "✅ Всё работает: бот активен, данные обновляются."
                if ok_msg else
                "📊 Ежедневный статус — есть замечания:\n" +
                "\n".join("• " + p for p in problems))
        if admin:
            sender.send(admin, text)
        save_state(st)
        return

    if problems:
        for p in problems:
            if admin:
                sender.send(admin, "🩺 " + p)
    save_state(st)


if __name__ == "__main__":
    main()
