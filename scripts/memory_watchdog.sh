#!/bin/bash
# RTH-aware memory watchdog — SINGLE RAM watchdog (v1 recalibration 2026-07-19,
# board Observability+Reliability + Gro + GAI, all APPROVE-WITH-CHANGES).
#
# WHAT CHANGED & WHY (design: logs/ram_alert_recalibration_design_2026-07-19.md):
# The box (956MB + 4GB swap) runs with 58–95MB *available* as the NORMAL RTH steady
# state (bot RSS 550–600MB). The old thresholds (<200MB here + a separate <80MB
# ram_watch.sh */6) fired on that normal floor → chronic Slack spam on a true-but-
# unactionable condition. Fixes:
#   • ONE watchdog now (ram_watch.sh retired; its */6 cadence moved to THIS cron).
#     Detection cadence (*/6) controls latency; the throttle controls Slack rate —
#     different knobs (board: run detection often, notify rarely).
#   • RTH two-tier, both far below the 58MB floor so normal ticks never fire:
#       CRITICAL <15MB available (throttle 15m)  ·  WARN <30MB available (throttle 30m)
#   • "available" (the value IS `free -m` col $7 = available; the old "free" label
#     misled the operator into reading a healthy-available number as near-death).
#   • Off-hours: the auto-restart ACTION is UNCHANGED (proactive reclaim). Its Slack
#     ping is throttled to 1/day and carries a rolling-24h restart COUNT; an
#     ACCELERATING leak (>=5 restarts/24h) escalates (CRITICAL, 1h throttle). The old
#     off-hours <200MB warn ping is dropped (log-only).
# NOT fixed here (LIVE roadmap, not "refused"): the actual harm — swap thrash →
# cycle-hang watchdog restarts mid-RTH — needs a swap-pressure signal + working-set
# trim / box size-up. Recalibration silences the alarm; it does not fix the wiring.
#
# THROTTLE: alert_once() is a per-category, per-call-configurable throttle (confirmed-
# send: the stamp is written only AFTER a confirmed Slack POST, so a failed delivery
# retries next tick instead of being swallowed). The auto-restart ACTION is NEVER
# throttled — only its notification is.

AVAIL=$(free -m | awk 'NR==2 {print $7}')       # $7 = AVAILABLE memory (not "free")
WEBHOOK=$(grep SLACK_WEBHOOK /home/ubuntu/mtf-bot/.env | cut -d= -f2)
LOG=/home/ubuntu/mtf-bot/logs/watchdog.log
RESTART_LOG=/home/ubuntu/mtf-bot/logs/offhours_restarts.log   # rolling 24h restart ledger

# alert_once <category_key> <message> [throttle_seconds=1800]
# Sends the Slack alert only if >throttle seconds have passed since the last CONFIRMED
# send for that category. Always returns 0 so callers never break on suppression.
alert_once() {
    local key="$1" msg="$2" throttle="${3:-1800}"
    local stamp="/tmp/mtf_memwatch_${key}"
    local now last rc
    # flock serializes the read-modify-write on the stamp so two overlapping runs
    # can't both slip past the throttle.
    (
        flock 200
        now=$(date +%s)
        last=0
        [ -f "$stamp" ] && last=$(cat "$stamp" 2>/dev/null || echo 0)
        case "$last" in (*[!0-9]*|"") last=0 ;; esac
        if [ $((now - last)) -ge "$throttle" ]; then
            if [ -z "$WEBHOOK" ]; then
                echo "$(TZ=America/Los_Angeles date): ERROR — SLACK webhook missing/empty; alert NOT delivered: $msg" >> "$LOG"
            else
                curl -s -X POST "$WEBHOOK" \
                  -H 'Content-type: application/json' \
                  -d "{\"text\":\"$msg\"}" >/dev/null 2>&1
                rc=$?
                if [ "$rc" -eq 0 ]; then
                    echo "$now" > "$stamp"
                else
                    echo "$(TZ=America/Los_Angeles date): ERROR — Slack POST failed (curl rc=$rc); alert NOT delivered, retrying next tick: $msg" >> "$LOG"
                fi
            fi
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

# Guard: if `free -m | awk` ever yields empty (parse failure), AVAIL="" makes every
# `[ "$AVAIL" -lt N ]` throw "integer expression expected" and silently fire nothing.
# Treat a non-numeric reading as a loud log + no-op (never a false restart/alert).
case "$AVAIL" in
    ''|*[!0-9]*)
        echo "$(TZ=America/Los_Angeles date): ERROR — could not read available memory (AVAIL='$AVAIL'); skipping this tick." >> "$LOG"
        exit 0
        ;;
esac

if [ "$IS_RTH" -eq 1 ]; then
    # RTH: alert only — NEVER restart, NEVER SIGKILL. Two tiers, both below the ~58MB
    # normal floor so normal ticks never page.
    if [ "$AVAIL" -lt 15 ]; then
        alert_once "rth_crit" "🚨 RTH RAM CRITICAL — ${AVAIL}MB available (imminent OOM). Bot still running — NO auto-restart during market hours. Manual action after 4 PM ET. (throttled 15m)" 900
        echo "$(TZ=America/Los_Angeles date): RTH RAM CRITICAL — ${AVAIL}MB available. No restart." >> "$LOG"
    elif [ "$AVAIL" -lt 30 ]; then
        alert_once "rth_warn" "⚠️ RTH RAM low — ${AVAIL}MB available. Bot running; NO auto-restart during market hours. Watching. (throttled 30m)" 1800
        echo "$(TZ=America/Los_Angeles date): RTH RAM low — ${AVAIL}MB available. No restart." >> "$LOG"
    fi
else
    # Outside RTH: auto-restart on critical (proactive reclaim). ACTION is UNTHROTTLED;
    # only its notification is throttled/escalated.
    if [ "$AVAIL" -lt 150 ]; then
        # ANTI-THRASH COOLDOWN (cold-2nd 2026-07-19): at the new */6 cadence, restarting on
        # every tick while memory stays tight would loop the bot every 6 min off-hours and
        # starve overnight work (EOD reconcile / nightly audit). Skip a PROACTIVE restart if
        # the last one was <20 min ago. This bounds proactive reclaim to ~1/20min while RTH
        # detection still runs every 6 min. systemd still handles a REAL crash independently
        # (this only gates the watchdog's proactive reclaim, never crash recovery).
        NOW_EPOCH=$(date +%s)
        LAST_RESTART=$(tail -1 "$RESTART_LOG" 2>/dev/null)
        case "$LAST_RESTART" in (''|*[!0-9]*) LAST_RESTART=0 ;; esac
        if [ $((NOW_EPOCH - LAST_RESTART)) -lt 1200 ]; then
            echo "$(TZ=America/Los_Angeles date): off-hours available ${AVAIL}MB but last restart <20m ago — cooldown, skipping proactive restart." >> "$LOG"
        else
            touch /tmp/mtf_planned_restart
            sudo /bin/systemctl restart mtf-bot
            echo "$(TZ=America/Los_Angeles date): AUTO-RESTART — available ${AVAIL}MB." >> "$LOG"
            # Rolling 24h restart ledger (append the just-fired restart, prune, then count).
            echo "$NOW_EPOCH" >> "$RESTART_LOG"
            # Prune entries older than 24h. The just-appended NOW_EPOCH always passes the
            # cutoff, so the pruned file is normally non-empty — but guard the replace on a
            # NON-EMPTY tmp (`-s`) anyway, so a pathological empty awk output (disk-full /
            # concurrent truncate) can NEVER clobber the ledger to empty and reset the count.
            awk -v cutoff=$((NOW_EPOCH - 86400)) '$1 ~ /^[0-9]+$/ && $1 >= cutoff' "$RESTART_LOG" > "$RESTART_LOG.tmp" 2>/dev/null && [ -s "$RESTART_LOG.tmp" ] && mv "$RESTART_LOG.tmp" "$RESTART_LOG"
            N24=$(wc -l < "$RESTART_LOG" 2>/dev/null | tr -d ' '); [ -z "$N24" ] && N24=1
            if [ "$N24" -ge 5 ]; then
                # Accelerating leak — this IS actionable; escalate promptly (1h throttle).
                alert_once "offhours_restart_accel" "🚨 OCI RAM leak ACCELERATING — ${N24} off-hours auto-restarts in 24h (${AVAIL}MB available). Working-set trim / box size needed. (throttled 1h)" 3600
            else
                # Routine nightly reclaim — non-actionable; one low-key notice per day.
                alert_once "offhours_restart" "🔁 OCI RAM reclaim — auto-restarted mtf-bot (${AVAIL}MB available). ${N24} restart(s) in last 24h. (throttled 24h)" 86400
            fi
        fi
    fi
    # (Off-hours <200MB warn ping dropped — was routine noise; log-only if desired.)
fi
