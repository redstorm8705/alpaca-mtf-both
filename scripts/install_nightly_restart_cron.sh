#!/usr/bin/env bash
set -euo pipefail

# EOD-FRIDAY-1117 (owner-approved 2026-11-17; logs/design_records/eod_friday_2026-11-17.md).
# The nightly 02:00 ET restart runs the bot's last end-of-day lot pass for the PT session that just ended. Its cron day
# list was 1-5 (UTC Monday-Friday), which covers the Sunday-Thursday PT sessions and skips Friday's. This script
# idempotently changes that one line's day list to 1-6 (adds 02:00 ET Saturday = Friday 23:00 PT). It edits only the
# exact restart line below and refuses if that line is missing or appears more than once.
readonly OLD_LINE="0 6,7 * * 1-5 /home/ubuntu/mtf-bot/venv/bin/python3 /home/ubuntu/mtf-bot/scripts/cron_tz_wrapper.py 02:00 sudo /bin/systemctl restart mtf-bot >> /home/ubuntu/mtf-bot/logs/nightly_restart.log 2>&1"
readonly NEW_LINE="0 6,7 * * 1-6 /home/ubuntu/mtf-bot/venv/bin/python3 /home/ubuntu/mtf-bot/scripts/cron_tz_wrapper.py 02:00 sudo /bin/systemctl restart mtf-bot >> /home/ubuntu/mtf-bot/logs/nightly_restart.log 2>&1"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
crontab -l >"$tmp_file" 2>/dev/null || true

new_count="$(grep -Fxc "$NEW_LINE" "$tmp_file" || true)"
old_count="$(grep -Fxc "$OLD_LINE" "$tmp_file" || true)"

if [ "$new_count" = "1" ] && [ "$old_count" = "0" ]; then
    echo "nightly restart already runs Monday-Saturday (UTC); nothing changed"
    exit 0
fi
if [ "$old_count" != "1" ] || [ "$new_count" != "0" ]; then
    echo "REFUSED: expected exactly one nightly restart line with days 1-5 (found old=$old_count new=$new_count); crontab unchanged" >&2
    exit 1
fi

out_file="$(mktemp)"
trap 'rm -f "$tmp_file" "$out_file"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "$OLD_LINE" ]; then
        printf '%s\n' "$NEW_LINE"
    else
        printf '%s\n' "$line"
    fi
done <"$tmp_file" >"$out_file"
crontab "$out_file"
echo "nightly restart now runs Monday-Saturday (UTC): Friday's session gets its end-of-day pass"
