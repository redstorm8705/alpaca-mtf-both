#!/bin/bash
# launch_bots.sh — starts MTF confluence bot with pre-flight safety checks
# Logs go to logs/launcher.log

cd "$(dirname "$0")"

PYTHON="/usr/local/bin/python3.10"
LOG="logs/launcher.log"

# ── Never write .pyc bytecode — eliminates stale-cache class of bugs entirely ─
export PYTHONDONTWRITEBYTECODE=1

mkdir -p logs

# ── Kill stale launcher loops from prior sessions ─────────────────────────────
# Each launch_bots.sh spawns a persistent "while true; sleep 30" restart loop.
# Without this guard they accumulate across sessions, each hammering the fcntl
# singleton every 30s and flooding logs with "Another bot instance" spam.
# Exclude our own PID so we don't self-terminate.
_self_pid=$$
for _lpid in $(pgrep -f "launch_bots.sh" 2>/dev/null); do
    if [ "$_lpid" != "$_self_pid" ]; then
        echo "$(date): Killing stale launcher PID $_lpid" >> "$LOG"
        kill "$_lpid" 2>/dev/null
    fi
done
echo "$(date): Using interpreter: $PYTHON ($($PYTHON --version 2>&1))" >> "$LOG"

# ── PRE-FLIGHT CHECKLIST ─────────────────────────────────────────────────────
echo "$(date): Running pre-flight checklist..." >> "$LOG"

# Step 1: Kill any existing bot processes
# Pattern matches any main.py process regardless of arguments — catches both
# launch_bots.sh-started instances (--profile paper) and manual starts (no args).
EXISTING=$(pgrep -f "main\.py" 2>/dev/null)
if [ -n "$EXISTING" ]; then
    echo "$(date): WARNING — killing existing main.py processes: $EXISTING" >> "$LOG"
    pkill -9 -f "main\.py"
    sleep 3
else
    echo "$(date): Pre-flight OK — no existing bot processes." >> "$LOG"
fi

# Step 2: Kill any stale companion processes
pkill -9 -f "live_data_writer.py" 2>/dev/null && echo "$(date): Killed stale live_data_writer.py" >> "$LOG"
pkill -9 -f "run_movers.py"       2>/dev/null && echo "$(date): Killed stale run_movers.py"       >> "$LOG"

# Step 3: Nuke __pycache__ — never rely on stale bytecode
rm -rf __pycache__
echo "$(date): __pycache__ cleared." >> "$LOG"

# Step 4: Verify .env and API keys exist
if [ ! -f ".env" ]; then
    echo "$(date): FATAL — .env missing. Aborting." >> "$LOG"
    exit 1
fi
if ! grep -q "ALPACA_API_KEY" .env; then
    echo "$(date): FATAL — ALPACA_API_KEY missing from .env. Aborting." >> "$LOG"
    exit 1
fi
echo "$(date): Pre-flight complete. Starting bot." >> "$LOG"
# ─────────────────────────────────────────────────────────────────────────────

# Launch live dashboard writer in background (30s refresh, independent of scan cycle)
$PYTHON live_data_writer.py >> logs/live_writer.log 2>&1 &
LIVE_PID=$!
echo "$(date): Live data writer started (PID $LIVE_PID)." >> "$LOG"

# LDW-2: Writer restart loop — mirrors the main.py restart pattern.
# Runs as a background subshell so it doesn't block the main.py loop below.
# fcntl.flock in live_data_writer.py is LOCK_NB: a stale lock from a dead process
# auto-releases, so the new instance acquires it without any cleanup step here.
(while true; do
    sleep 30
    if ! pgrep -qf "live_data_writer\.py" 2>/dev/null; then
        echo "$(date): live_data_writer.py died — restarting." >> "$LOG"
        $PYTHON live_data_writer.py >> logs/live_writer.log 2>&1 &
        echo "$(date): live_data_writer.py restarted (PID $!)." >> "$LOG"
    fi
done) &

# Auto-restart loop — handles silent SIGKILL crashes from macOS memory pressure
# NOTE: do NOT rm the lock file here. fcntl.flock in main.py uses the same inode
# for singleton enforcement. Deleting it before launch creates a new inode and
# bypasses the LOCK_NB check — that was the root cause of zombie accumulation.
while true; do
    # Guard: if lockfile is held by another process (e.g. a manual start without
    # --profile paper), skip this iteration to prevent "Another bot instance" spam.
    if pgrep -qf "main\.py" 2>/dev/null; then
        echo "$(date): main.py already running (pgrep) — skipping launch." >> "$LOG"
        sleep 30
        continue
    fi
    caffeinate -i $PYTHON main.py --profile paper >> logs/bot.log 2>&1
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -eq 1 ]; then
        echo "$(date): Bot exited with code 1 (already running or fatal). Waiting 30s..." >> "$LOG"
    else
        echo "$(date): Bot stopped (exit $EXIT_CODE). Restarting in 30s..." >> "$LOG"
    fi
    sleep 30
done
