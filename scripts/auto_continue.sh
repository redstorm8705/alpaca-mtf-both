#!/bin/bash
# Auto-continuation script — fires at usage-limit reset
# Scheduled by Claude Code S29 2026-05-22

PROJECT="/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL"
CLAUDE="/Users/rafaeldeleon/.local/bin/claude"
LOG="$PROJECT/logs/overnight_session_$(date +%Y-%m-%d_%H%M).md"
PROMPT_FILE="$PROJECT/logs/auto_continue_prompt.txt"

cd "$PROJECT" || exit 1

echo "# Overnight Auto-Session — $(date)" > "$LOG"
echo "Started at $(date)" >> "$LOG"
echo "" >> "$LOG"

"$CLAUDE" --print "$(cat "$PROMPT_FILE")" >> "$LOG" 2>&1

echo "" >> "$LOG"
echo "Completed at $(date)" >> "$LOG"

# macOS notification
osascript -e 'display notification "Auto-session complete. Check logs/overnight_session_*.md" with title "alpaca-mtf-bot" sound name "Glass"' 2>/dev/null || true
