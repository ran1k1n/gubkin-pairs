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
import os
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


def info_name(groups, gid):
    return groups.get(gid, {}).get("name", str(gid))


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
    repairs = []

    # 1) сервис бота
    r = subprocess.run(["systemctl", "is-active", "gubkin-bot"],
                       capture_output=True, timeout=10)
    if r.stdout.decode().strip() != "active":
        subprocess.run(["systemctl", "restart", "gubkin-bot"], timeout=30)
        time.sleep(3)
        problems.append("🚑 Сервис бота был выключен — перезапустил автоматически.")
    time.sleep(2)

    # 2) рассыльщик: cron.log свежее 20 минут
    age = mtime_age(Path(str(CACHE_DIR).replace("/cache", "")) / "cron.log")
    if age is not None and age > 1200:
        try:
            subprocess.run(["python3", "/opt/gubkin/multi/notifier.py"],
                           capture_output=True, timeout=300,
                           cwd="/opt/gubkin/multi")
            problems.append("⏰ Рассыльщик молчал %d мин — запустил его "
                            "вручную." % (age // 60))
        except Exception as e:
            problems.append("⏰ Рассыльщик не запускается: %s" % str(e)[:60])
    elif age is None:
        problems.append("⏰ Лог рассыльщика не найден.")

    # повреждённые кэши — удалить (перекачаются автоматически)
    import glob as _g
    for f in _g.glob(str(CACHE_DIR / "sched_*.json")):
        try:
            json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            os.remove(f)
            problems.append("🗑 Повреждённый кэш %s удалён — перекачается."
                            % os.path.basename(f))

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
        # задания Mac-ретранслятору: добыть свежие данные через домашний IP
        job_dir = "/opt/gubkin/multi/cache/fetch_jobs"
        os.makedirs(job_dir, exist_ok=True)
        for gid, info in groups.items():
            job_f = os.path.join(job_dir, "%s.json" % gid)
            if not os.path.exists(job_f):
                with open(job_f, "w", encoding="utf-8") as jf:
                    json.dump({"gid": gid,
                               "date": t.strftime("%d-%m-%Y")}, jf)
        key = "stale_alert"
        prev_alert = st.get(key)
        too_soon = False
        if prev_alert:
            try:
                pdt = __import__("datetime").datetime.fromisoformat(prev_alert)
                too_soon = (t - pdt).total_seconds() < 86400
            except ValueError:
                too_soon = False
        if not too_soon:
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

    # осиротевшие результаты добычи — прогнать через ingestion
    for f in _g.glob(str(CACHE_DIR / "fetch_results" / "*.json")):
        try:
            subprocess.run(["python3", "/opt/gubkin/multi/fetch_ingest.py", f],
                           capture_output=True, timeout=60)
            os.remove(f)
        except Exception:
            pass

    # сверка расписания с сайтом каждые 6 часов (00/06/12/18 + 5 мин):
    # свежая добыча -> сравнение с кэшем -> алерт при расхождении
    if t.hour in (0, 6, 12, 18) and 5 <= t.minute < 25 and groups:
        try:
            import common
            fresh_all = {}
            for gid, info in groups.items():
                try:
                    if common.backoff_active():
                        break
                    fresh_all[gid] = common.get_week(gid, force=True,
                                                     max_age_min=None)
                    time.sleep(4)
                except Exception:
                    pass
            diffs = []
            for gid, fresh in fresh_all.items():
                c = common.load_sched(gid)
                if not c:
                    continue
                a = sorted((l.get("wd"), l.get("start"), l.get("subject"))
                           for l in fresh.get("lessons", [])
                           if not l.get("cancelled"))
                b = sorted((l.get("wd"), l.get("start"), l.get("subject"))
                           for l in c.get("lessons", [])
                           if not l.get("cancelled"))
                if a != b:
                    diffs.append("%s: было %d пар, стало %d"
                                 % (info_name(groups, gid), len(b), len(a)))
            if diffs:
                key = "sched_diff"
                prev = st.get(key)
                today_str = t.date().isoformat()
                if not prev or (t - __import__("datetime").datetime.fromisoformat(prev)).total_seconds() > 21600:
                    st[key] = t.isoformat()
                    problems.append("📋 Сверка расписания: отличия с сайтом —\n"
                                    + "\n".join("• " + d for d in diffs))
        except Exception:
            pass

    # 5) попытка протолкнуть очередь недоставленных сообщений
    try:
        sender._flush_queue()
    except Exception:
        pass

    # 6) если сайт доступен, а кэши групп устарели — обновить их сам
    try:
        import common
        probe = common.SchedClient()
        probe.visit()
        probe.api("schedule/api/api.php?act=meta")
        site_ok = True
    except Exception:
        site_ok = False
    if site_ok and not CFG.get("fetch_via_mac"):
        # в Mac-first режиме сервер не светится перед сайтом:
        # обновления через Mac-задания, прямой заход — не нужен
        fixed = 0
        for gid, info in groups.items():
            try:
                if common.needs_refresh(gid):
                    common.get_week(gid, force=True, max_age_min=None)
                    fixed += 1
                    time.sleep(4)
            except Exception:
                pass
        if fixed:
            entry = "repair %s: обновил %d групп" % (
                now().strftime("%F %H:%M"), fixed)
            repairs.append(entry)

    # 7) ежедневный статус в 21:00
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

    if repairs:
        with open(CACHE_DIR / "selfrepair.log", "a", encoding="utf-8") as f:
            f.write("\n".join(repairs) + "\n")

    # молча: всё, что починено и найдено, уйдёт в вечерний дайджест.
    # Мгновенный алерт — только если бот не поднялся после рестарта.
    if any("Сервис бота" in p for p in problems):
        r2 = subprocess.run(["systemctl", "is-active", "gubkin-bot"],
                            capture_output=True, timeout=10)
        if r2.stdout.decode().strip() != "active" and admin:
            sender.send(admin, "🚨 Бот не поднялся после автоперезапуска — "
                               "смотрите вечерний статус.")
    save_state(st)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()[-600:]
        try:
            with open(CACHE_DIR / "monitor_last_error.log", "w",
                      encoding="utf-8") as f:
                f.write(err)
        except OSError:
            pass
        sys.exit(1)
