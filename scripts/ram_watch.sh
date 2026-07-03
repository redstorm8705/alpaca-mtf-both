#!/bin/bash
# RAM watcher (2026-07-02 incident): alert if available RAM < 80MB during RTH.
# 2026-07-02 format-lock: repeat alerts throttled to one per 30 min — a slow
# leak used to re-fire this every 6-min cron tick, spamming Slack.
cd /home/ubuntu/mtf-bot || exit 0
AVAIL=$(free -m | awk "NR==2{print \$7}")
HR=$(TZ=America/New_York date +%H%M)
STAMP=/tmp/mtf_ram_watch_last_alert
# RTH-ish window 09:00-16:30 ET
if [ "$HR" -ge 0900 ] && [ "$HR" -le 1630 ]; then
  if [ "$AVAIL" -lt 80 ]; then
    NOW=$(date +%s)
    LAST=0
    [ -f "$STAMP" ] && LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
    if [ $((NOW - LAST)) -lt 1800 ]; then
      echo "$(date '+%F %T') RAM ${AVAIL}MB low — alert suppressed (<30min since last)"
      exit 0
    fi
    echo "$NOW" > "$STAMP"
    source .env 2>/dev/null
    RSS=$(ps -eo rss,cmd --sort=-rss | grep "main.py" | grep -v grep | head -1 | awk "{printf \"%.0f\", \$1/1024}")
    MSG=":warning: *RAM WATCH* — ${AVAIL}MB free · bot RSS ${RSS}MB · <80MB during RTH. Next alert suppressed 30 min; watchdog auto-restarts if critical."
    curl -s -X POST -H "Content-type: application/json" --data "{\"text\":\"$MSG\"}" "$SLACK_WEBHOOK_URL" >/dev/null 2>&1
  fi
fi
