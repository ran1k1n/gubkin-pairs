#!/bin/bash
# Сторож gubkin-bot: при крах-лупе (>=3 перезапусков за 10 минут)
# автоматически откатывает bot.py на последнюю стабильную версию.
CACHE=/opt/gubkin/multi/cache
STABLE="$CACHE/bot.py.stable"
CURRENT=/opt/gubkin/multi/bot.py

STATE=$(systemctl is-active gubkin-bot 2>/dev/null)
if [ "$STATE" != "active" ]; then
  systemctl restart gubkin-bot
  exit 0
fi

RECENT=$(journalctl -u gubkin-bot --since "-10 min" --no-pager 2>/dev/null | grep -c "Started gubkin-bot")
if [ "${RECENT:-0}" -ge 3 ] && [ -f "$STABLE" ]; then
  cp "$STABLE" "$CURRENT"
  systemctl restart gubkin-bot
  echo "$(date '+%F %T') крах-луп: откат bot.py на стабильную версию" >> "$CACHE/selfrepair.log"
fi
