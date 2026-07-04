#!/bin/bash
# S58c: RSS trend sampler — feeds the memory-leak investigation.
# Appends: epoch,iso_pt,bot_rss_kb,writer_rss_kb,avail_mb,swap_used_mb,bot_uptime
OUT=/home/ubuntu/mtf-bot/logs/rss_trend.csv
[ -f "$OUT" ] || echo 'epoch,ts_pt,bot_rss_kb,writer_rss_kb,avail_mb,swap_used_mb,bot_etime' > "$OUT"
BOT_PID=$(systemctl show -p MainPID --value mtf-bot)
WR_PID=$(systemctl show -p MainPID --value mtf-writer)
BOT_RSS=$(ps -o rss= -p "$BOT_PID" 2>/dev/null | tr -d ' ')
WR_RSS=$(ps -o rss= -p "$WR_PID" 2>/dev/null | tr -d ' ')
BOT_ET=$(ps -o etime= -p "$BOT_PID" 2>/dev/null | tr -d ' ')
AVAIL=$(free -m | awk 'NR==2 {print $7}')
SWAP=$(free -m | awk 'NR==3 {print $3}')
echo "$(date +%s),$(TZ=America/Los_Angeles date '+%Y-%m-%d %H:%M'),${BOT_RSS:-0},${WR_RSS:-0},$AVAIL,$SWAP,${BOT_ET:-na}" >> "$OUT"
