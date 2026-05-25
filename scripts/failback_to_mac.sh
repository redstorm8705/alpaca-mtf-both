#!/bin/bash
# failback_to_mac.sh — Emergency failback: restore Mac as primary bot runner
#
# USE WHEN: OCI server is unreachable, reclaimed, or permanently down.
# DO NOT RUN while OCI is still running — will cause dual-bot split.
#
# Steps:
#   1. Confirms OCI is actually unreachable (3-ping test)
#   2. Re-enables Mac crontab (removes DISABLED-OCI-MIGRATION comments)
#   3. Starts launch_bots.sh
#   4. Prints manual steps for disabling OCI services

set -e

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$BOT_DIR/logs/failback.log"
PYTHON="/usr/local/bin/python3.10"
OCI_IP="129.153.208.32"

mkdir -p "$BOT_DIR/logs"
echo "$(date): failback_to_mac.sh started" | tee -a "$LOG"

# ── Step 1: Confirm OCI is unreachable ────────────────────────────────────────
echo "Checking OCI reachability..."
if ping -c 3 -W 3 "$OCI_IP" > /dev/null 2>&1; then
    echo ""
    echo "WARNING: OCI server $OCI_IP is REACHABLE."
    echo "Running both Mac and OCI bots simultaneously will cause duplicate trades."
    echo ""
    read -p "Are you SURE OCI services are stopped? Type YES to continue: " confirm
    if [ "$confirm" != "YES" ]; then
        echo "Aborted." | tee -a "$LOG"
        exit 1
    fi
fi
echo "$(date): OCI reachability check passed" | tee -a "$LOG"

# ── Step 2: Re-enable Mac crontab ────────────────────────────────────────────
echo "Re-enabling Mac crontab..."
TMPCT=$(mktemp)
crontab -l 2>/dev/null | sed 's/^#DISABLED-OCI-MIGRATION [0-9-]* //' > "$TMPCT"
crontab "$TMPCT"
rm "$TMPCT"
echo "$(date): Mac crontab re-enabled" | tee -a "$LOG"
echo "Active cron jobs:"
crontab -l | grep -v '^#' | grep -v '^$'

# ── Step 3: Start Mac bot ─────────────────────────────────────────────────────
echo ""
echo "Starting Mac bot via launch_bots.sh..."
cd "$BOT_DIR"
nohup bash launch_bots.sh >> "$LOG" 2>&1 &
LAUNCHER_PID=$!
echo "$(date): launch_bots.sh started (PID $LAUNCHER_PID)" | tee -a "$LOG"
sleep 5

# Confirm main.py started
if pgrep -qf "main\.py"; then
    MAIN_PID=$(pgrep -f "main\.py" | head -1)
    echo "$(date): main.py running (PID $MAIN_PID)" | tee -a "$LOG"
    echo ""
    echo "=== FAILBACK COMPLETE ==="
    echo "Mac bot is running. PID: $MAIN_PID"
else
    echo "$(date): WARNING — main.py not detected after launch. Check logs/bot.log" | tee -a "$LOG"
    echo "WARNING: main.py did not start. Check logs/bot.log manually."
fi

# ── Step 4: Manual OCI cleanup reminder ──────────────────────────────────────
echo ""
echo "=== MANUAL STEP REQUIRED ==="
echo "If OCI is reachable, stop its services to prevent split-brain:"
echo "  ssh -i ~/.ssh/mtf_bot_oracle ubuntu@$OCI_IP \\"
echo "    'sudo systemctl stop mtf-bot mtf-writer mtf-http && sudo systemctl disable mtf-bot mtf-writer mtf-http'"
echo ""
echo "Log: $LOG"
