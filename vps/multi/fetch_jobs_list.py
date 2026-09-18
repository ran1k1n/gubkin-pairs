# -*- coding: utf-8 -*-
"""Печатает задания на добычу расписания (по одному JSON в строке)."""
import glob
import json

for f in sorted(glob.glob("/opt/gubkin/multi/cache/fetch_jobs/*.json")):
    try:
        d = json.load(open(f, encoding="utf-8"))
        print(json.dumps({"file": f, "gid": d["gid"], "date": d["date"]}))
    except (OSError, ValueError, KeyError):
        continue
