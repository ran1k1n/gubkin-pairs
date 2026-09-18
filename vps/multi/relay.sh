#!/bin/bash
# Ретранслятор Mac→VPS: выдача очереди доставки и подтверждение партии.
# pull  — атомарно выдаёт pending_sends.json (партию) и очищает очередь;
#         зависшие партии старше 30 мин возвращаются в очередь.
# ack   — удаляет доставленные элементы партии (индексы приходят в stdin).
cd /opt/gubkin/multi/cache || exit 1
case "$1" in
  pull)
    for f in relay_out_*.json; do
      [ -e "$f" ] || continue
      age=$(( $(date +%s) - $(stat -c %Y "$f") ))
      if [ "$age" -gt 1800 ]; then
        python3 /opt/gubkin/multi/relay_ack.py --requeue "$f"
        rm -f "$f"
      fi
    done
    if [ -s pending_sends.json ]; then
      TS=$(date +%s%N)
      cp pending_sends.json "relay_out_$TS.json"
      echo "{}" > pending_sends.json
      printf '{"batch": "%s", "items": %s}' "$TS" "$(cat relay_out_$TS.json)"
    else
      printf '{"batch": null, "items": []}'
    fi
    ;;
  ack)
    BATCH=$(echo "$2" | tr -dc '0-9')
    [ -z "$BATCH" ] && { echo '{"error": "no batch"}'; exit 1; }
    python3 /opt/gubkin/multi/relay_ack.py "relay_out_$BATCH.json"
    rm -f "relay_out_$BATCH.json"
    echo ACK_OK
    ;;
  *)
    echo '{"error": "usage: pull | ack <batch>"}'
    ;;
esac
