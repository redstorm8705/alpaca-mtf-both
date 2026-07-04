#!/bin/bash
# RTH-aware memory watchdog
# During RTH (9:30-16:00 ET Mon-Fri): Slack alert ONLY — never restart
# Outside RTH: alert <200MB free, auto-restart <150MB free

AVAIL=$(free -m | awk 'NR==2 {print $7}')
WEBHOOK=$(grep SLACK_WEBHOOK /home/ubuntu/mtf-bot/.env | cut -d= -f2)
LOG=/home/ubuntu/mtf-bot/logs/watchdog.log

# Current ET time components
HOUR=$(TZ="America/New_York" date +%H)
MIN=$(TZ="America/New_York" date +%M)
DOW=$(TZ="America/New_York" date +%u)   # 1=Mon ... 7=Sun

MINS_ET=$((10#$HOUR * 60 + 10#$MIN))
RTH_OPEN=$((9 * 60 + 30))    # 570 mins
RTH_CLOSE=$((16 * 60))        # 960 mins

IS_RTH=0
if [ "$DOW" -le 5 ] && [ "$MINS_ET" -ge "$RTH_OPEN" ] && [ "$MINS_ET" -lt "$RTH_CLOSE" ]; then
    IS_RTH=1
fi

if [ "$IS_RTH" -eq 1 ]; then
    # RTH: alert only — NEVER restart, NEVER SIGKILL
    if [ "$AVAIL" -lt 200 ]; then
        curl -s -X POST "$WEBHOOK" \
          -H 'Content-type: application/json' \
          -d "{\"text\":\"⚠️ RTH RAM WARNING — ${AVAIL}MB free. Bot still running. NO auto-restart during market hours. Manual action required after 4 PM ET.\"}"
        echo "$(TZ=America/Los_Angeles date): RTH RAM WARNING — ${AVAIL}MB free. No restart." >> "$LOG"
    fi
else
    # Outside RTH: alert + auto-restart
    if [ "$AVAIL" -lt 150 ]; then
        curl -s -X POST "$WEBHOOK" \
          -H 'Content-type: application/json' \
          -d "{\"text\":\":skull: OCI RAM CRITICAL (off-hours) — ${AVAIL}MB free. Auto-restarting mtf-bot now.\"}"
        touch /tmp/mtf_planned_restart
        sudo /bin/systemctl restart mtf-bot
        echo "$(TZ=America/Los_Angeles date): AUTO-RESTART — RAM critical at ${AVAIL}MB." >> "$LOG"
    elif [ "$AVAIL" -lt 200 ]; then
        curl -s -X POST "$WEBHOOK" \
          -H 'Content-type: application/json' \
          -d "{\"text\":\"⚠️ OCI RAM LOW (off-hours) — ${AVAIL}MB free. Watching.\"}"
        echo "$(TZ=America/Los_Angeles date): RAM low warning — ${AVAIL}MB free." >> "$LOG"
    fi
fi
