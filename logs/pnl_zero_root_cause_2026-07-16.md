# pnl=0.0 EPIDEMIC — ROOT CAUSE (2026-07-16) — from today's nightly VERDICT=FAIL

**Status:** Phase-1 diagnosis COMPLETE, evidence-backed. No patch yet. This is the REAL root of the
`pnl=0.0` + `_fill_unverified` + `_fill_reconcile_expired` pattern that has recurred for weeks
(today's RIVN; 7/7 RIVN + SNOW/HOOD/MS/MSTR/MARA/AVGO). **Bug A (5fb5c4e, fill_reconciler query path)
was NECESSARY BUT NOT SUFFICIENT** — it fixed WHICH query the reconciler runs; this is about the
reconciler never running in time at all.

## TODAY'S CASE (ground truth)
- Alpaca fills: BUY 14 @ $17.42 (7/14 19:17 UTC) → SELL 14 across FOUR fills 7/16 **13:32:51–13:34:15 UTC**
  (17.45 / 17.43 / 17.52 / 17.55). **REAL P&L = +$0.51 (a small WIN).**
- Bot recorded: `exit_price 17.42 (= entry_price fallback)`, **`pnl: 0.0`**, `_fill_unverified: true`,
  `_fill_reconcile_expired: true`, `exit_reason: stop_breach_cover`.

## THE LOG (rotated mtf_bot.log.1)
```
13:30:28,030 CRITICAL gtc_manager   [RIVN] STOP BREACHED at RTH open (gap-up): stop $17.37 vs market $17.35 — covering now
13:30:28,129 INFO     broker        [RIVN] Position closed.              <- order SUBMITTED (not filled)
13:30:30,298 CRITICAL fill_helpers  [RIVN] FILL UNVERIFIED: ... Using entry_price $17.42 as fallback.   <- 2s later!
13:30:30,962 INFO     tracker       [RIVN] Exit recorded: $17.42 | P&L: $0.00 (stop_breach_cover)
13:30:30,973 CRITICAL gtc_manager   [RIVN] Cover-on-breach filled @ $17.42 (P&L $0.00).   <- FALSE: nothing had filled
14:06:30,448 CRITICAL fill_reconciler [RIVN] RC-4 FILL RECONCILIATION EXPIRED: outside 5-min window
```

## ROOT CAUSE (3 compounding defects)
1. **FILL-FETCH BUDGET vs OPEN-AUCTION FILL TIME.** `fetch_actual_fill_price` polls with a HARD 2.5s total
   budget (`_MAX_TOTAL_WAIT`), called ~2s after `close_position`. A market-open cover filled in 4 pieces over
   **2–4 MINUTES**. The fill did not exist yet → `_fill_unverified_fallback()` → `exit_price = entry_price`
   → `pnl = (17.42-17.42) * 14 = 0.00`. (qty was CORRECTLY 14 here — see #3.)
2. **RECONCILER NEVER RUNS IN TIME — the binding defect.**
   a. `strategy/run_cycle.py:951-966` — `if tod_phase == "opening":` runs check_partial_exits + check_exits then
      **`return`** (line ~966). `_run_fill_recon(...)` is at **line 1672** — BELOW that return. So during the
      **9:30–10:00 ET opening window the reconciler NEVER RUNS** — which is EXACTLY when stop-breach covers fire.
   b. `fill_reconciler.py:39` uses `get_unverified_exits(max_age_minutes=5)` — a **5-min** window — but the
      observed cycle cadence is **~5.5–6 min** (13:30:24, 13:36:22, 13:41:53). So even OUTSIDE the opening
      window the reconciler gets **at most ONE** attempt, often ZERO.
   → Combined: first reconciler touch at 14:06:30 (36 min after the 13:30:30 exit) → EXPIRED → **zero recovery
   attempts ever made**. The fill WAS recoverable from 13:34:15 onward.
3. **`_qty_at_close: 0` is a POST-HOC EOD OVERWRITE, not the close-time value.** `reconcile_eod.py:475`
   (`trade["_qty_at_close"] = actual_qty`) overwrites it with the CURRENT Alpaca qty (0 — the position is flat
   by EOD). At `record_exit` qty was correctly 14 (proof: the `qty==0 and not partial_exited` fallback guard at
   portfolio_tracker.py:1557-1573 NEVER logged — it would have fired if qty were 0). **CONSEQUENCE:**
   `patch_exit_pnl` (portfolio_tracker.py:248-251) recomputes P&L from `_qty_at_close` → any FUTURE patch now
   computes `(fill-entry) * 0 = 0`. The EOD overwrite POISONS the repair path.

## WHY IT'S SYSTEMATIC
Stop-breach covers happen AT THE OPEN (a gapped position breaches its stop at 9:30). That is precisely the
window where (a) fills are slowest (open auction, multi-fill) and (b) the reconciler is disabled. So EVERY
open-cover gets a permanently wrong P&L. Matches 7/7 (RIVN covered 09:38 ET + 6 siblings) and today.

## FIX DIRECTIONS (for BGG — NOT yet implemented)
1. **Call `_run_fill_recon` in the opening-window path before the early return** — it is bookkeeping, not a
   trading decision; it must run wherever check_exits ran. (Surgical, run_cycle.)
2. **Widen the RC-4 window** `max_age_minutes` 5 → >> cycle cadence (e.g. 60) so the reconciler gets MULTIPLE
   attempts. 5 min < 6-min cadence makes the current window structurally unable to fire. Also mirrors
   `mark_fill_expired`'s 5-min cutoff (portfolio_tracker.py:359) — must move together or it re-expires.
3. **Stop `reconcile_eod` clobbering `_qty_at_close`** (or have patch_exit_pnl prefer the original qty when
   `_qty_at_close==0 and not partial_exited`) — else the repair is a no-op even when the fill IS recovered.
4. (Honesty) `gtc_manager` logs "Cover-on-breach filled @ $X (P&L $Y)" when nothing filled — same lying-log
   class fixed in the watchdogs. Should say "cover submitted; fill pending reconciliation".

**Impact:** P&L integrity. Today only $0.51, but the SAME mechanism mis-reported RIVN's real −$41 on 7/7 and
7 trades that day. Kill switch is phantom-proof (uses Alpaca equity, not daily_pnl), so this is a REPORTING/
Kelly-feedback corruption, not a live capital-risk path — but it poisons Kelly + TQI + win-rate stats.

## FIX IMPLEMENTED (2026-07-16) — awaiting board seat, then preship + ship
**BGG so far: Gro APPROVE (clean) · GAI APPROVE-WITH-CHANGES (changes = docs + a verification, both
addressed below; 3rd item deferred) · board seat IN FLIGHT.**
1. `strategy/run_cycle.py` — `_run_fill_recon(tracker, kelly=kelly, risk=risk)` now called inside the
   `if tod_phase == "opening":` block, AFTER check_exits and BEFORE `_touch_cycle_ts(); return`
   (verified line-anchored: recon L984 → touch L985 → return L986).
2. `execution/fill_reconciler.py` — `_RC4_WINDOW_MIN = getattr(config, "RC4_RECONCILE_WINDOW_MINUTES", 60)`
   replaces the hard-coded `max_age_minutes=5`; the CRITICAL + Slack expiry messages now report the real
   window instead of a hard-coded "5-min".
**PROOF (self-test against the REAL RIVN timeline — exit 13:30:30, cycles 13:36/13:41/13:47/14:06):**
```
 +  5.9min  OLD(5)=EXPIRED(never tried)  NEW(60)=PENDING(retry)
 + 11.4min  OLD(5)=EXPIRED(never tried)  NEW(60)=PENDING(retry)
 + 36.0min  OLD(5)=EXPIRED(never tried)  NEW(60)=PENDING(retry)
```
Under the OLD window EVERY pass was already expired → recovery was IMPOSSIBLE. Under NEW, the 13:36 pass
recovers (fills existed from 13:34:15). statics: py_compile + ruff + mypy clean.
**GAI change #2 RESOLVED (verified in code):** the opening block takes NO new entries ("exits monitored, no
new entries"), so a Kelly rebuild there cannot affect sizing that cycle. Outside it, `_run_fill_recon` (L1672)
does precede `execute_entries` (L1890) — but it replaces a FABRICATED 0.0 with an Alpaca-verified fill, so
sizing off corrected data is strictly better than sizing off a wrong zero. Non-issue / net benefit.
**DEFERRED (unchanged, logged):** reconcile_eod.py:475 `_qty_at_close` clobber (only poisons a POST-EOD patch;
the live path patches minutes after the exit while _qty_at_close is still true); gtc_manager's false
"Cover-on-breach filled @ $X" log; the 2.5s fill-fetch budget (the reconciler is the correct repair layer —
do not block the trading cycle waiting on fills).
