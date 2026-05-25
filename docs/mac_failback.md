# Mac Failback Runbook

**Use when:** OCI server (129.153.208.32) is unreachable, down, or unrecoverable.  
**Goal:** Restore all bot functions on Mac within ~5 minutes, zero data loss.  
**Author:** Generated 2026-04-27 from live infrastructure state.

---

## Pre-Checks (do these first)

1. Confirm OCI is actually down — not just SSH timeout:
   ```bash
   ping -c 3 129.153.208.32
   curl -s --max-time 5 http://129.153.208.32:8080/dashboard.html | head -5
   ```
2. Check OCI console at cloud.oracle.com — confirm instance state (Stopped vs Running).
3. If OCI is Running but SSH is unresponsive: try OCI console → Instance → Cloud Shell before failback.

---

## Step 1 — Sync code from OCI to Mac (if OCI is reachable via SCP)

If OCI is reachable but the bot process is broken, pull latest code before starting Mac:

```bash
rsync -az -e 'ssh -i ~/.ssh/mtf_bot_oracle' \
  ubuntu@129.153.208.32:/home/ubuntu/mtf-bot/ \
  /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/ \
  --exclude '__pycache__' --exclude '.env' --exclude 'logs/'
```

**Skip this step if OCI is completely unreachable.** Mac code is the source of truth —
OCI was deployed from Mac via rsync. Any gap will be small.

---

## Step 2 — Kill any stale Mac processes

```bash
pkill -9 -f "main.py"          2>/dev/null
pkill -9 -f "live_data_writer" 2>/dev/null
pkill -9 -f "launch_bots.sh"  2>/dev/null
sleep 2
```

Verify nothing is left:
```bash
pgrep -fl "main.py\|live_data_writer\|launch_bots"
```
Expected: no output.

---

## Step 3 — Re-enable Mac crontab

Open the crontab editor:
```bash
crontab -e
```

Remove the `#DISABLED-OCI-MIGRATION 2026-04-27` prefix from each line.  
After editing, all 7 jobs should be active:

| Job | Mac schedule (PT) |
|-----|-------------------|
| run_market_top.py | 5:30 AM Mon-Fri |
| run_macro_regime.py | 5:00 PM Sun |
| midday_audit.py | 1:15 PM Mon-Fri |
| nightly_audit.py | 1:30 PM Mon-Fri |
| options_scanner.py | Every 15 min, 9-4 PM Mon-Fri |
| run_backtest.sh | 5:05 AM Mon-Fri |

Verify crontab is saved:
```bash
crontab -l | grep -v "^#" | grep -v "^$"
```
Expected: 6+ active lines.

---

## Step 4 — Launch the bot

```bash
cd /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL
bash launch_bots.sh &
```

`launch_bots.sh` handles:
- Pre-flight checks (.env, API keys)
- Killing any lingering processes
- Starting `main.py` with `caffeinate -i` (prevents Mac sleep)
- Starting `live_data_writer.py`
- Auto-restart loops for both

**Keep Mac awake:** plug in power, disable sleep in System Settings → Battery.

---

## Step 5 — Verify bot is running

```bash
sleep 10
pgrep -fl "main.py\|live_data_writer"
tail -20 /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/logs/bot.log
```

Expected: two PIDs, log shows `Bot started` or `run_cycle` entries.

Check dashboard locally:
```bash
open /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/dashboard.html
```

---

## Step 6 — Disable OCI watchdog Slack alerts (prevent noise)

The OCI watchdog cron fires every 5 min and will spam Slack while OCI is down.  
SSH to OCI is unavailable by definition during failback, so suppress on OCI side
is not possible. Instead: mute the Slack channel temporarily, or accept the noise
until OCI recovers.

Optionally, silence in Mac alerts.py by setting env var:
```bash
export MTF_SUPPRESS_WATCHDOG=1
```
(Only effective if alerts.py checks this — add as a future enhancement.)

---

## Step 7 — Adopt open OCI positions

If OCI had open positions with GTC stops when it went down:
- GTC stops are held on **Alpaca's servers** — they remain active regardless of bot state.
- At Mac bot startup, `main.py` reconciles Alpaca positions against `trade_log.json`.
- Any position in Alpaca but not in trade_log will be logged as an orphan — review and
  manually add to trade_log if needed via the orphan adoption path.

Check for orphans in the first log cycle:
```bash
grep -i "orphan\|reconcil" /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/logs/bot.log | tail -20
```

---

## After OCI Recovers — Re-migration Steps

1. Stop Mac bot:
   ```bash
   pkill -9 -f "main.py"
   pkill -9 -f "live_data_writer"
   pkill -9 -f "launch_bots.sh"
   ```

2. Rsync Mac → OCI (one-way, Mac is source of truth during failback):
   ```bash
   rsync -az --exclude '__pycache__' --exclude '.env' --exclude 'logs/' \
     -e 'ssh -i ~/.ssh/mtf_bot_oracle' \
     /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/ \
     ubuntu@129.153.208.32:/home/ubuntu/mtf-bot/
   ```

3. Restart OCI services:
   ```bash
   ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32 \
     "sudo systemctl restart mtf-bot mtf-writer mtf-http && systemctl is-active mtf-bot mtf-writer mtf-http"
   ```

4. Disable Mac crontab:
   ```bash
   crontab -e
   # Re-add #DISABLED-OCI-MIGRATION to all 7 job lines
   ```

5. Verify OCI dashboard is live:
   ```
   http://129.153.208.32:8080/dashboard.html
   ```

---

## Quick Reference

| Item | Value |
|------|-------|
| OCI IP | 129.153.208.32 |
| OCI SSH key | `~/.ssh/mtf_bot_oracle` |
| OCI user | ubuntu |
| OCI bot path | `/home/ubuntu/mtf-bot/` |
| Mac bot path | `/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/` |
| Mac Python | `/usr/local/bin/python3.10` |
| OCI stack (A1.Flex retry) | `ocid1.ormstack.oc1.phx.amaaaaaai3ebloyaw73yf3ibme5ldugobra53vwttklhritd2zy65rl5eoeq` |
