#!/usr/bin/env bash
set -euo pipefail

# Idempotently install the daily state-file freshness check (Claude 2026-10-16). Weekdays at 12:50 UTC (server clock),
# 30 minutes after the later morning refresh (dip zones 12:20 UTC, overnight K 12:15 UTC) and before the open
# (13:30 UTC in EDT, 14:30 UTC in EST). Pages Slack when dip_zones.json or overnight_k.json is older than one
# completed trading session. Read-only.
readonly CRON_MARKER="# state-file freshness check (Claude)"
readonly CRON_LINE="50 12 * * 1-5 cd /home/ubuntu/mtf-bot && /usr/bin/flock -n /tmp/mtf-state-freshness.cron.lock /home/ubuntu/mtf-bot/venv/bin/python3 /home/ubuntu/mtf-bot/scripts/state_freshness_check.py >> /home/ubuntu/mtf-bot/logs/state_freshness_cron.log 2>&1"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
crontab -l >"$tmp_file" 2>/dev/null || true

if grep -Fqx "$CRON_LINE" "$tmp_file"; then
    echo "state freshness cron already installed"
    exit 0
fi

{
    printf '\n%s\n' "$CRON_MARKER"
    printf '%s\n' "$CRON_LINE"
} >>"$tmp_file"
crontab "$tmp_file"
echo "state freshness cron installed"
