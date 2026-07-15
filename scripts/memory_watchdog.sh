#!/bin/bash
# RTH-aware memory watchdog
# During RTH (9:30-16:00 ET Mon-Fri): Slack alert ONLY — never restart
# Outside RTH: alert <200MB free, auto-restart <150MB free
#
# THROTTLE (2026-07-14): this script was the PRIMARY Slack false-alarm source —
# it ran every 30 min via cron and fired an un-throttled Slack alert EVERY tick
# whenever free RAM was low, which the main.py memory leak makes near-constant.
# (The 2026-07-02 throttle fix only covered scripts/ram_watch.sh — the secondary,
# lower-threshold <80MB script — and left THIS one, the <200MB */30 spammer,
# un-throttled. That is why the spam never stopped.) Fix: every Slack alert now
# goes through alert_once(), a per-category 30-min throttle (same pattern as
# ram_watch.sh). The auto-restart ACTION is NEVER throttled — only its Slack
# notification is — so off-hours memory-critical restarts still always fire.

AVAIL=$(free -m | awk 'NR==2 {print $7}')
WEBHOOK=$(grep SLACK_WEBHOOK /home/ubuntu/mtf-bot/.env | cut -d= -f2)
LOG=/home/ubuntu/mtf-bot/logs/watchdog.log
THROTTLE=1800   # 30 min per alert category

# alert_once <category_key> <message>
# Sends the Slack alert only if >THROTTLE seconds have passed since the last send
# for that category. Always returns 0 so callers never break on suppression.
alert_once() {
    local key="$1" msg="$2"
    local stamp="/tmp/mtf_memwatch_${key}"
    local now last
    # flock serializes the read-modify-write on the stamp so two overlapping
    # runs can't both slip past the throttle (GAI preship hardening 2026-07-14).
    (
        flock 200
        now=$(date +%s)
        last=0
        [ -f "$stamp" ] && last=$(cat "$stamp" 2>/dev/null || echo 0)
        if [ $((now - last)) -ge "$THROTTLE" ]; then
            echo "$now" > "$stamp"
            curl -s -X POST "$WEBHOOK" \
              -H 'Content-type: application/json' \
              -d "{\"text\":\"$msg\"}" >/dev/null 2>&1
        fi
    ) 200> "$stamp.lock"
    return 0
}

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
        alert_once "rth_warn" "⚠️ RTH RAM WARNING — ${AVAIL}MB free. Bot still running. NO auto-restart during market hours. Manual action required after 4 PM ET. (throttled 30m)"
        echo "$(TZ=America/Los_Angeles date): RTH RAM WARNING — ${AVAIL}MB free. No restart." >> "$LOG"
    fi
else
    # Outside RTH: alert + auto-restart
    if [ "$AVAIL" -lt 150 ]; then
        # Slack notification is throttled; the RESTART action is NOT.
        alert_once "offhours_crit" ":skull: OCI RAM CRITICAL (off-hours) — ${AVAIL}MB free. Auto-restarting mtf-bot. (throttled 30m; restart always fires)"
        touch /tmp/mtf_planned_restart
        sudo /bin/systemctl restart mtf-bot
        echo "$(TZ=America/Los_Angeles date): AUTO-RESTART — RAM critical at ${AVAIL}MB." >> "$LOG"
    elif [ "$AVAIL" -lt 200 ]; then
        alert_once "offhours_warn" "⚠️ OCI RAM LOW (off-hours) — ${AVAIL}MB free. Watching. (throttled 30m)"
        echo "$(TZ=America/Los_Angeles date): RAM low warning — ${AVAIL}MB free." >> "$LOG"
    fi
fi
