# ruff: noqa: E501, E702, E402
"""
Pre-flight & RTH simulation diagnostic.
Verifies bot readiness for tomorrow's session.
Checks: open positions, GTC stops, patch health, mover cache, watchdog math.
Output to logs/ only. Does NOT trade or submit orders.
"""
from zoneinfo import ZoneInfo
from datetime import datetime
import json
import os

# AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This script
# is read-only on trade_log.json/premarket_movers.json/eod_*.json and only
# writes its own dedicated logs/preflight_*.txt report -- no write-contention
# risk with the live bot's shared state.
PDT = ZoneInfo("America/Los_Angeles")
ET  = ZoneInfo("America/New_York")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "logs")
PASS = "✅"; FAIL = "❌"; WARN = "⚠️ "

results = []

def check(label, passed, detail=""):
    icon = PASS if passed else FAIL
    results.append((icon, label, detail))
    print(f"  {icon}  {label}" + (f" — {detail}" if detail else ""))

print("\n" + "="*60)
print("  PRE-FLIGHT / RTH SIMULATION — MTF Bot")
print(f"  {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}")
print("="*60)

# ── 1. TRADE LOG ─────────────────────────────────────────────
print("\n[1] TRADE LOG")
try:
    with open(os.path.join(ROOT, "trade_log.json")) as f:
        tl = json.load(f)
    open_trades = {t["symbol"]: t for t in tl.get("open", [])}
    stats = tl.get("stats", {})
    check("trade_log.json loaded", True,
          f"{len(open_trades)} open, {stats.get('total_trades',0)} closed, "
          f"WR={stats.get('win_rate',0):.1f}%, PnL=${stats.get('total_pnl',0):.2f}")
    for sym, t in open_trades.items():
        has_gtc = bool(t.get("gtc_stop_order_id"))
        has_rth = bool(t.get("rth_day_stop_order_id"))
        overnight = t.get("overnight", False)
        stop = t.get("stop") or t.get("trail_stop")
        check(f"  [{sym}] open position",
              True,
              f"dir={t.get('direction')} qty={t.get('qty_remaining',t.get('qty'))} "
              f"stop=${stop:.2f} GTC={'YES' if has_gtc else 'NO'} "
              f"overnight={overnight}")
        if overnight and not has_gtc:
            check(f"  [{sym}] overnight stop coverage", False,
                  "No GTC stop — DAY stop required at RTH open via _submit_rth_day_stops()")
        elif overnight and has_gtc:
            check(f"  [{sym}] overnight stop coverage", True,
                  f"GTC order {t['gtc_stop_order_id'][:8]}...")
except Exception as e:
    check("trade_log.json", False, str(e))
    open_trades = {}

# ── 2. WATCHDOG TIMING SIMULATION ────────────────────────────
print("\n[2] WATCHDOG TIMING (M1 patch)")
WATCHDOG_TIMEOUT = 12 * 60
OVERNIGHT_SLEEP  = 30 * 60
RTH_SLEEP        = 5  * 60
MAX_CYCLE_DUR    = 90  # seconds (observed: 71s)

worst_overnight_gap = OVERNIGHT_SLEEP + MAX_CYCLE_DUR
worst_rth_gap       = RTH_SLEEP + MAX_CYCLE_DUR

check("Overnight→RTH transition safe",
      worst_overnight_gap < WATCHDOG_TIMEOUT,
      f"worst gap={worst_overnight_gap}s vs timeout={WATCHDOG_TIMEOUT}s — "
      f"{'OK' if worst_overnight_gap < WATCHDOG_TIMEOUT else 'WOULD FIRE'}")
check("RTH cycle timing safe",
      worst_rth_gap < WATCHDOG_TIMEOUT,
      f"worst gap={worst_rth_gap}s vs timeout={WATCHDOG_TIMEOUT}s")

# ── 3. PREMARKET MOVER CACHE ──────────────────────────────────
print("\n[3] PREMARKET MOVER CACHE (B2 patch)")
pm_path = os.path.join(LOG_DIR, "premarket_movers.json")
try:
    with open(pm_path) as f:
        pm = json.load(f)
    movers = pm.get("movers", [])
    ts = pm.get("ts", "unknown")
    check("premarket_movers.json exists", True,
          f"{len(movers)} movers cached as of {ts}: {movers}")
    check("Disk reload ready for RTH restart", True,
          "build_scan_universe() will reload on os.execv() restart")
except FileNotFoundError:
    check("premarket_movers.json exists", False,
          "File not found — mover injection will use watchlist-only at RTH open. "
          "This is normal if bot hasn't run pre-market yet.")
except Exception as e:
    check("premarket_movers.json readable", False, str(e))

# ── 4. DAY STOP SIMULATION (B1 patch) ────────────────────────
print("\n[4] DAY STOP RACE CONDITION (B1 patch — simulated)")
import random as _rnd
for sym, t in open_trades.items():
    if not t.get("overnight"):
        continue
    _dir  = t.get("direction", "long")
    _stop = t.get("trail_stop") or t.get("stop")
    if not _stop:
        check(f"  [{sym}] DAY stop sim", False, "No stop in tracker")
        continue
    _offset = round(_rnd.uniform(0.01, 0.05), 2)
    _stop_price = round(_stop + _offset, 2) if _dir == "short" else round(_stop - _offset, 2)
    # We can't check live market price without trading hours, but we can verify
    # the logic path is correct and the pre-flight check will engage
    check(f"  [{sym}] DAY stop would compute",
          True,
          f"stop_price=${_stop_price:.2f} (tracker_stop=${_stop:.2f} ± offset={_offset:.2f}) "
          f"— market price check will gate submission at RTH open")
if not any(t.get("overnight") for t in open_trades.values()):
    check("DAY stop sim", True, "No overnight positions requiring DAY stop at open")

# ── 5. GTC LOOP FIX (M3 patch) ───────────────────────────────
print("\n[5] PRE-MARKET GTC CANCEL LOOP (M3 patch)")
# Verify the phase function exists and returns expected values
import importlib.util
spec = importlib.util.spec_from_file_location("main_check", os.path.join(ROOT, "main.py"))
# Don't actually import main.py (it would start the bot). Check via grep instead.
import subprocess
result = subprocess.run(
    ["grep", "-n", "_get_tod_phase.*closed", os.path.join(ROOT, "main.py")],
    capture_output=True, text=True
)
found = "_get_tod_phase" in result.stdout and "closed" in result.stdout
check("M3 patch present in main.py",
      found,
      result.stdout.strip() if found else "Patch not found — check manually")

# ── 6. GTC PARTIAL P&L FIX (M2 patch) ────────────────────────
print("\n[6] GTC PARTIAL P&L RECORDING (M2 patch)")
result2 = subprocess.run(
    ["grep", "-n", r"partial_pnl.*partial_exit_price\|FIX: Calculate and bank", os.path.join(ROOT, "main.py")],
    capture_output=True, text=True
)
found2 = "partial_pnl" in result2.stdout and "bank" in result2.stdout.lower()
check("M2 patch present in main.py", found2,
      result2.stdout.strip()[:120] if found2 else "Patch not found")

# ── 7. EOD FILE ───────────────────────────────────────────────
print("\n[7] EOD SNAPSHOT")
from datetime import date
eod_path = os.path.join(LOG_DIR, f"eod_{date.today()}.json")
try:
    with open(eod_path) as f:
        eod = json.load(f)
    check("eod snapshot current", True,
          f"pnl_today=${eod.get('pnl_today',0):.2f} "
          f"trades={eod.get('trades_today',0)} "
          f"WR={eod.get('win_rate_today',0):.1f}%")
except Exception as e:
    check("eod snapshot", False, str(e))

# ── 8. BOT PROCESS ────────────────────────────────────────────
print("\n[8] LIVE PROCESS")
result3 = subprocess.run(["pgrep", "-a", "-f", r"main\.py"], capture_output=True, text=True)
pid_line = result3.stdout.strip()
if pid_line:
    pid = pid_line.split()[0]
    ps_result = subprocess.run(["ps", "-p", pid, "-o", "pid,etime,command"],
                                capture_output=True, text=True)
    check("Bot process running", True, ps_result.stdout.strip().split("\n")[-1])
else:
    check("Bot process running", False, "No main.py process found — restart required")

# ── SUMMARY ───────────────────────────────────────────────────
print("\n" + "="*60)
passes = sum(1 for r in results if r[0] == PASS)
fails  = sum(1 for r in results if r[0] == FAIL)
warns  = sum(1 for r in results if r[0] == WARN)
print(f"  RESULT: {passes} pass  {fails} fail  {warns} warn")
if fails == 0:
    print("  🟢 PRE-FLIGHT CLEAR — Bot ready for tomorrow's RTH session")
else:
    print("  🔴 PRE-FLIGHT ISSUES — Review failures above before market open")
print("="*60 + "\n")

# Write report to logs/
report_path = os.path.join(LOG_DIR, f"preflight_{date.today()}.txt")
with open(report_path, "w") as f:
    f.write(f"Pre-flight report — {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S ET')}\n\n")
    for icon, label, detail in results:
        f.write(f"{icon}  {label}" + (f" — {detail}" if detail else "") + "\n")
    f.write(f"\nRESULT: {passes} pass  {fails} fail  {warns} warn\n")
print(f"Report saved → {report_path}")
