#!/usr/bin/env bash
set -euo pipefail

# Idempotently install the daily dynamic overnight-cap refresh (Claude 2026-10-14). Runs weekdays before the open
# (crontab times are the server's UTC clock: 12:15 UTC = 5:15/8:15 AM PT/ET in EDT, 4:15/7:15 in EST), and again
# after the close on the same completed-session data with the closing book (a retry if the morning run failed; it
# can only tighten that session's reading).
readonly CRON_MARKER="# dynamic overnight cap K refresh (Claude)"
readonly CRON_LINE="15 12,21 * * 1-5 cd /home/ubuntu/mtf-bot && /usr/bin/flock -n /tmp/mtf-overnight-k.cron.lock /home/ubuntu/mtf-bot/venv/bin/python3 /home/ubuntu/mtf-bot/run_overnight_k.py >> /home/ubuntu/mtf-bot/logs/overnight_k_cron.log 2>&1"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
crontab -l >"$tmp_file" 2>/dev/null || true

if grep -Fqx "$CRON_LINE" "$tmp_file"; then
    echo "overnight K cron already installed"
    exit 0
fi

{
    printf '\n%s\n' "$CRON_MARKER"
    printf '%s\n' "$CRON_LINE"
} >>"$tmp_file"
crontab "$tmp_file"
echo "overnight K cron installed"
