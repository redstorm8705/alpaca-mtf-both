#!/usr/bin/env bash
set -euo pipefail

# Idempotently install the daily QHM dip-zone refresh (Claude 2026-10-15). Weekdays before the open (server clock is
# UTC: 12:20 UTC = 8:20 AM ET in EDT, 7:20 AM ET in EST), and again after the close as a retry on the same
# completed-session data.
readonly CRON_MARKER="# QHM dip-zone refresh (Claude)"
readonly CRON_LINE="20 12,21 * * 1-5 cd /home/ubuntu/mtf-bot && /usr/bin/flock -n /tmp/mtf-dip-zones.cron.lock /home/ubuntu/mtf-bot/venv/bin/python3 /home/ubuntu/mtf-bot/run_dip_zones.py >> /home/ubuntu/mtf-bot/logs/dip_zones_cron.log 2>&1"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
crontab -l >"$tmp_file" 2>/dev/null || true

if grep -Fqx "$CRON_LINE" "$tmp_file"; then
    echo "dip zone cron already installed"
    exit 0
fi

{
    printf '\n%s\n' "$CRON_MARKER"
    printf '%s\n' "$CRON_LINE"
} >>"$tmp_file"
crontab "$tmp_file"
echo "dip zone cron installed"
