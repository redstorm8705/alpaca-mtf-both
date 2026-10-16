#!/usr/bin/env bash
set -euo pipefail

# Idempotently install the state-file freshness check (Claude 2026-10-16). Three runs per weekday (owner 2026-10-16:
# "3x minimum"), server clock UTC:
#   12:50 UTC — after the morning refreshes (overnight K 12:15, dip zones 12:20) and before the open (13:30 UTC in
#               EDT, 14:30 UTC in EST)
#   17:50 UTC — mid-session
#   21:50 UTC — after the close (20:00 UTC in EDT, 21:00 UTC in EST) and after the evening refreshes (21:15 / 21:20):
#               if both of today's refreshes failed, today's completed session already counts, so it pages the
#               same afternoon instead of the next morning.
# Pages Slack when dip_zones.json or overnight_k.json is older than one completed trading session. Read-only.
# Re-running replaces any earlier state_freshness_check line (e.g. the first once-a-day schedule) instead of adding a
# second one.
readonly CRON_MARKER="# state-file freshness check (Claude)"
readonly SCRIPT_PATH="/home/ubuntu/mtf-bot/scripts/state_freshness_check.py"
readonly CRON_LINE="50 12,17,21 * * 1-5 cd /home/ubuntu/mtf-bot && /usr/bin/flock -n /tmp/mtf-state-freshness.cron.lock /home/ubuntu/mtf-bot/venv/bin/python3 ${SCRIPT_PATH} >> /home/ubuntu/mtf-bot/logs/state_freshness_cron.log 2>&1"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
crontab -l >"$tmp_file" 2>/dev/null || true

if grep -Fqx "$CRON_LINE" "$tmp_file" && [ "$(grep -Fc "$SCRIPT_PATH" "$tmp_file")" -eq 1 ]; then
    echo "state freshness cron already installed"
    exit 0
fi

# Drop any earlier freshness-check line and its marker, keep everything else as it was.
grep -Fv "$SCRIPT_PATH" "$tmp_file" | grep -Fvx "$CRON_MARKER" >"${tmp_file}.new" || true
{
    printf '\n%s\n' "$CRON_MARKER"
    printf '%s\n' "$CRON_LINE"
} >>"${tmp_file}.new"
mv "${tmp_file}.new" "$tmp_file"
crontab "$tmp_file"
echo "state freshness cron installed (12:50, 17:50, 21:50 UTC weekdays)"
