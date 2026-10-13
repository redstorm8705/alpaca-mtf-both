#!/usr/bin/env bash
set -euo pipefail

# Idempotently install the broker-fill Slack notifier outside the trading loops.
# The outer flock prevents overlapping one-shot polls if Alpaca or Slack is slow.
readonly CRON_MARKER="# broker-authoritative buy/sell Slack notifications (OpenAI Codex)"
readonly CRON_LINE="* * * * * cd /home/ubuntu/mtf-bot && /usr/bin/flock -n /tmp/mtf-fill-notifications.cron.lock /home/ubuntu/mtf-bot/venv/bin/python3 /home/ubuntu/mtf-bot/run_fill_notifications.py >> /home/ubuntu/mtf-bot/logs/fill_notifications_cron.log 2>&1"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
crontab -l >"$tmp_file" 2>/dev/null || true

if grep -Fqx "$CRON_LINE" "$tmp_file"; then
    echo "fill notification cron already installed"
    exit 0
fi

{
    printf '\n%s\n' "$CRON_MARKER"
    printf '%s\n' "$CRON_LINE"
} >>"$tmp_file"
crontab "$tmp_file"
echo "fill notification cron installed"
