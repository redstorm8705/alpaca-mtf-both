#!/bin/bash
# Service DOWN watchdog — replaces the inline */5 crontab one-liner.
#
# WHY (2026-07-16): the old crontab one-liner was
#   */5 * * * * systemctl is-active --quiet mtf-bot mtf-writer mtf-http \
#               || curl ... '{"text":"ALERT: MTF bot service DOWN on OCI..."}'
# and had TWO Slack false-alarm/spam sources:
#   (1) NO GRACE — it alerted on the FIRST failed check, so a check landing
#       mid-restart (18 restarts observed in 24h) saw a service briefly inactive
#       and fired a FALSE "bot DOWN".
#   (2) NO THROTTLE — a genuinely-down service re-alerted EVERY 5 MINUTES
#       forever (same defect class as the memory_watchdog.sh <200MB spammer
#       fixed 2026-07-14).
# FIX: (a) CONSECUTIVE-FAIL GRACE — alert only after N consecutive failed checks
#      (default 2 ≈ 10 min down), so a seconds-long restart blip never alerts;
#      (b) THROTTLE — while down, re-alert at most once per 30 min (flock+stamp,
#      mirrors memory_watchdog.sh alert_once);
#      (c) RECOVERY notice once when services come back, so a real outage closes;
#      (d) HONEST DELIVERY — the log never claims "ALERT SENT" unless the POST
#      actually succeeded, and the throttle stamp is written ONLY on a confirmed
#      send so a failed delivery retries next tick (GAI audit 2026-07-16).
# The CHECK still runs every 5 min — only the ALERT is graced/throttled. A real
# outage is reported within ~10 min instead of ~5: a deliberate trade, since this
# is a paper account (a missed 10 min costs a gap in a simulated curve, not money)
# while the false alarms have trained the operator to swipe Slack away — which is
# what makes the REAL alert get missed. GRACE=2 is the MINIMUM N that kills the
# false alarm; every increment past it is pure detection latency (board 2026-07-16).
#
# RTH note: main's monitoring/watchdog.py DOES restart on a stalled cycle during
# RTH, but via os.execv() — the process image is replaced in place, same PID, so
# systemd's MainPID never changes and `is-active` does not flap. If that path is
# ever changed to `systemctl restart`, it WOULD manufacture false DOWNs in market
# hours and this grace window becomes load-bearing (board catch 2026-07-16).
#
# No dependence on /tmp/mtf_planned_restart: the grace window already covers a
# planned restart, and a STALE sentinel was itself a past false-alarm source.

SERVICES="mtf-bot mtf-writer mtf-http"
# The var is SLACK_WEBHOOK_URL (NOT SLACK_WEBHOOK) — verified against the live .env.
# Anchored + first-match + f2-: a commented '#SLACK_WEBHOOK_URL=' line must not match
# (two matches -> multi-line value -> curl garbage), and a URL containing '=' survives.
# NOTE: memory_watchdog.sh:17 greps the UNANCHORED substring 'SLACK_WEBHOOK', which
# happens to match SLACK_WEBHOOK_URL. Anchoring to '^SLACK_WEBHOOK=' (as first drafted
# here) silently matched NOTHING -> empty webhook -> a watchdog that can never alert.
# Caught by live verification before ship; keep the _URL suffix.
WEBHOOK=$(grep -m1 '^SLACK_WEBHOOK_URL=' /home/ubuntu/mtf-bot/.env 2>/dev/null | cut -d= -f2-)
LOG=/home/ubuntu/mtf-bot/logs/watchdog.log
FAILS=/tmp/mtf_svc_fail_count
STAMP=/tmp/mtf_svc_last_alert
GRACE=${SVC_GRACE_CHECKS:-2}   # consecutive failed checks required before alerting
THROTTLE=1800                  # seconds between repeat DOWN alerts (30 min)

# slack <message> — returns 0 only if the POST actually succeeded, else 1.
# Never aborts the watchdog. Logs LOUDLY on a missing webhook or curl failure: a
# watchdog that silently fails to deliver is worse than none, and the caller must
# not claim "ALERT SENT" when nothing was delivered. A watchdog's only product is
# the operator's trust that silence means healthy — a lying log poisons that.
slack() {
    if [ -z "$WEBHOOK" ]; then
        echo "$(TZ=America/Los_Angeles date): ERROR — SLACK_WEBHOOK missing/empty in .env; alert NOT delivered: $1" >> "$LOG"
        return 1
    fi
    curl -s -X POST "$WEBHOOK" -H 'Content-type: application/json' \
        -d "{\"text\":\"$1\"}" >/dev/null 2>&1
    _rc=$?
    if [ "$_rc" -ne 0 ]; then
        echo "$(TZ=America/Los_Angeles date): ERROR — Slack POST failed (curl rc=$_rc); alert NOT delivered: $1" >> "$LOG"
        return 1
    fi
    return 0
}

# Which services are down? (empty => all healthy)
DOWN=""
for s in $SERVICES; do
    systemctl is-active --quiet "$s" || DOWN="$DOWN$s "
done

# flock serializes the read-modify-write on the counter/stamp so two overlapping
# runs can't both slip past the grace/throttle (mirrors memory_watchdog.sh).
(
    flock 200
    now=$(date +%s)

    if [ -n "$DOWN" ]; then
        n=0
        [ -f "$FAILS" ] && n=$(cat "$FAILS" 2>/dev/null || echo 0)
        case "$n" in (*[!0-9]*|"") n=0 ;; esac
        n=$((n + 1))
        echo "$n" > "$FAILS"

        if [ "$n" -lt "$GRACE" ]; then
            echo "$(TZ=America/Los_Angeles date): DOWN: ${DOWN}(check $n/$GRACE — within grace, no alert)" >> "$LOG"
            exit 0
        fi

        last=0
        [ -f "$STAMP" ] && last=$(cat "$STAMP" 2>/dev/null || echo 0)
        case "$last" in (*[!0-9]*|"") last=0 ;; esac
        if [ $((now - last)) -ge "$THROTTLE" ]; then
            # Wording: the counter is shared across services, so state what is
            # actually true — N checks saw A service down — not that THIS service
            # failed N times (board accuracy catch 2026-07-16).
            if slack ":rotating_light: MTF service DOWN on OCI: ${DOWN}— ${n} consecutive checks had a service down (>= $((GRACE * 5)) min). ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32 (throttled 30m)"; then
                echo "$now" > "$STAMP"
                echo "$(TZ=America/Los_Angeles date): DOWN: ${DOWN}(check $n) — ALERT SENT" >> "$LOG"
            else
                echo "$(TZ=America/Los_Angeles date): DOWN: ${DOWN}(check $n) — ALERT SEND FAILED (not stamped; retries next tick)" >> "$LOG"
            fi
        else
            echo "$(TZ=America/Los_Angeles date): DOWN: ${DOWN}(check $n) — alert throttled" >> "$LOG"
        fi
    else
        n=0
        [ -f "$FAILS" ] && n=$(cat "$FAILS" 2>/dev/null || echo 0)
        case "$n" in (*[!0-9]*|"") n=0 ;; esac
        if [ "$n" -ge "$GRACE" ]; then
            # We had alerted — close the loop once, then fully clear.
            if slack ":white_check_mark: MTF services RECOVERED — all up ($SERVICES) after ${n} failed checks."; then
                echo "$(TZ=America/Los_Angeles date): RECOVERED after $n failed checks" >> "$LOG"
            else
                echo "$(TZ=America/Los_Angeles date): RECOVERED after $n failed checks — recovery notice SEND FAILED" >> "$LOG"
            fi
            rm -f "$FAILS" "$STAMP"
        elif [ "$n" -gt 0 ]; then
            # DECAY, don't hard-reset (board catch 2026-07-16): a hard reset meant a
            # FLAPPING service (down/up/down) never accumulated to GRACE and NEVER
            # alerted — the one place this was worse than the old one-liner. Decaying
            # by 1 keeps the mid-restart suppression (one blip then healthy -> 0) while
            # letting a flapper whose downs outnumber its ups still reach the threshold.
            # Residual: a perfect 50/50 alternation still never fires — a rolling-window
            # flap detector is the proper fix (logged follow-up).
            n=$((n - 1))
            if [ "$n" -le 0 ]; then
                rm -f "$FAILS" "$STAMP"
            else
                echo "$n" > "$FAILS"
            fi
            echo "$(TZ=America/Los_Angeles date): healthy — fail counter decayed to $n" >> "$LOG"
        fi
    fi
) 200> "$FAILS.lock"

# DISK-FULL / STATE-WRITE FAIL-SILENT GUARD (board catch 2026-07-16): if /tmp is
# unwritable the `200>` redirect makes bash skip the whole subshell, or the counter
# write fails so n never advances past 1 — either way the alert could NEVER fire, and
# a full disk is CORRELATED with the services dying (mtf-writer writes logs). So the
# most likely real outage would also disarm the watchdog. If we see a service down but
# no counter state on disk, alert UNTHROTTLED (state is broken; throttling needs state).
if [ -n "$DOWN" ] && [ ! -s "$FAILS" ]; then
    slack ":rotating_light: MTF WATCHDOG STATE WRITE FAILED (${FAILS} unwritable — disk full?) while services are DOWN: ${DOWN}— alerting UNTHROTTLED. ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32"
    echo "$(TZ=America/Los_Angeles date): STATE WRITE FAILED while DOWN: ${DOWN}— unthrottled alert attempted" >> "$LOG"
fi

exit 0
