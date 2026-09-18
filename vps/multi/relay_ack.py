# -*- coding: utf-8 -*-
"""Управление партиями ретранслятора.
Использование:
  relay_ack.py <outbox_file>            — индексы доставленных приходят в stdin
  relay_ack.py --requeue <outbox_file>  — вернуть партию в очередь целиком
"""
import json
import sys


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def main():
    if len(sys.argv) < 3:
        print('{"error": "usage"}')
        return
    out_file = sys.argv[2]
    out = load(out_file, [])
    if not isinstance(out, list):
        out = []

    if sys.argv[1] == "--requeue":
        pend = load("pending_sends.json", [])
        pend.extend(out)
        save("pending_sends.json", pend)
        print("requeued %d" % len(out), file=sys.stderr)
        return

    try:
        idx = set(json.load(sys.stdin))
    except ValueError:
        idx = set()
    delivered = [out[i] for i in sorted(idx) if i < len(out)]
    undelivered = [m for i, m in enumerate(out) if i not in idx]

    pend = load("pending_sends.json", [])
    pend.extend(undelivered)
    save("pending_sends.json", pend)
    print("delivered %d, returned %d" % (len(delivered), len(undelivered)),
          file=sys.stderr)


if __name__ == "__main__":
    main()
