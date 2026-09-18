# -*- coding: utf-8 -*-
"""Приём результата, добытого Mac-ретранслятором, и разбор в кэш.
Использование: fetch_ingest.py <result_file> [<job_file>]
Result: {"gid": int, "date": "DD-MM-YYYY", "state": true, "raw": <ответ сайта>}
При успехе кэш сохраняется, job-файл удаляется. При отказе сайта job
остаётся (повторит Mac в следующем цикле)."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/opt/gubkin/multi")
from common import _norm_week, save_sched

TZ = ZoneInfo("Europe/Moscow")
JOB_DIR = "/opt/gubkin/multi/cache/fetch_jobs"


def main():
    res_file = sys.argv[1]
    with open(res_file, encoding="utf-8") as f:
        d = json.load(f)
    gid = int(d["gid"])
    date_str = d["date"]
    raw = d.get("raw")

    if raw is None or raw.get("state") is not True:
        print("SITE_REFUSED: сайт не отдал данные — job остаётся")
        sys.exit(2)

    day = datetime.strptime(date_str, "%d-%m-%Y").date()
    data = {"fetched_at": datetime.now(TZ).isoformat(), "group_id": gid,
            "week_type": ((raw.get("rows") or {}).get("week") or {})
            .get("weekRussia", {}).get("type")}
    data.update(_norm_week(raw, gid))
    save_sched(gid, data, day)
    if len(sys.argv) > 2:
        try:
            os.remove(sys.argv[2])
            print("job удалён")
        except OSError:
            pass
    print("INGESTED: %s | %s | пар: %d"
          % (gid, data.get("week_type"), len(data.get("lessons", []))))


if __name__ == "__main__":
    main()
