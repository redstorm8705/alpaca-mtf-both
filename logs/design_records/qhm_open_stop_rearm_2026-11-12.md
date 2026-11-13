# QHM-OPENSTOP-1112 — a missing QHM stop is re-placed in pre-market and the opening window too

Approval-ID: QHM-OPENSTOP-1112
Covers: strategy/run_cycle.py, tests/test_qhm_openstop_1112.py
Not changed: execution/broker.py, execution/orphan_manager.py, config.py
Status: rev 1 (signed: Claude, 2026-11-12), for board + GAI review.

## Owner direction (2026-11-12)
"Let's begin these P0 items" — P0 1: QHM stops are not repaired from 8:45 to 10:00 ET; the nightly audit failed on
NVDA's unprotected window ("NVDA is classed NAKED in BROKER GROUND TRUTH (32m exposure)", gemini_audit_2026-11-12.txt).

## Problem (measured)
- strategy/run_cycle.py calls `qhm.resubmit_pending_stops()` only in the market-closed branch (line ~577, added
  2026-08-09 for the weekend NVDA gap) and reaches `qhm.run_weekly_check()` (line ~1835) only after 10:00 ET. The
  pre-market branch (8:45-9:30 ET, line ~392) and the opening-window branch (9:30-10:00 ET, line ~1071) return before
  either, so a QHM hold in PENDING_STOP_REPLACE has no stop re-placed for up to 75 minutes.
- 11-12: NVDA was PENDING_STOP_REPLACE from 06:13 PT (bot restart after the owner's state change); cycles at 06:13,
  06:27 (pre-market), 06:39 and 06:45 (opening window) placed nothing; QHM placed SELL 5 @ $147.48 at 07:02 PT
  (10:02 ET, order aeb32759). mtf_bot.log: "Pre-market window (8:45-9:30 ET)" at 13:56, 14:06, 14:13, 14:27 UTC with no
  rearm line; "Opening noise window active" at 14:40 and 14:45 UTC.

## Change
Call the existing `_main.qhm.resubmit_pending_stops()` (unchanged; rearm-only, one broker-book snapshot, adopts or
resizes a live QH stop before submitting, never blind) in two more places, wrapped exactly like the closed-market
call (never raises into the cycle, one log line when it rearms):
1. the pre-market branch, after the overnight GTC-stop reconcile, before the movers scan;
2. the opening-window branch, after check_exits and the fill reconcile, before its return.
No new number. QHM's trading (entries, dip-adds, trims, the stray-sell guard, the restop pass) still runs only after
10:00 ET, unchanged; only the protective stop repair moves earlier.

## Rev 2 — board (Peterffy) and GAI, both APPROVE-WITH-CHANGES, folded in
1. One helper in strategy/run_cycle.py, `_rearm_qhm_stops(tag)`: calls `_main.qhm.resubmit_pending_stops()`, never
   raises, logs "rearmed N QHM GTC stop(s) (<tag>)" with tag market-closed / pre-market / opening window. All three
   branches call it, the existing market-closed call included, so a future branch cannot omit it (Q6 build, now).
2. Order (tested): pre-market — after `_cancel_and_reconcile_gtc_stops`, before the movers scan; opening window — after
   check_exits and the fill reconcile, and also when either of those raises (its own try).
3. Opening window only: after the rearm, any QHM hold still in PENDING_STOP_REPLACE (read-only scan of
   `_main.qhm._positions`) sends ONE page per symbol per ET day ("ACTION <SYM> (QHM): no stop could be placed in the
   opening window ... place a stop in the Alpaca app"), so a broker refusal (e.g. a stop above the price) is never
   silent for the 30 minutes before QHM's own pass. Pre-market refusals stay log-only (QHM's 10:00 pass and the
   unmanaged-position alarm follow).
4. Verified (both reviewers): `cancel_and_reconcile_gtc_stops` (execution/orphan_manager.py) acts only on the core
   tracker's stored stop ids, keeps every symbol with an active QHM state (PENDING_STOP_REPLACE included), and keeps
   all stops when the QHM state file is unreadable; it never cancels a QH- order. `_recover_or_submit_stop` re-reads
   the book before submitting and never doubles a live QH- sell, so extra callers cannot create a duplicate.
5. A broker refusal repeats at most once per cycle (about 5-6 per window), logged; nothing is cancelled or doubled.

## Interaction checks
- Pre-market branch cancels overnight GTC stops "so they don't appear on L2 during RTH" (`_cancel_and_reconcile_gtc_
  stops`). That function handles the core tracker's GTC stops; it does not cancel QH- stops (verified in review:
  board to confirm). Rearm runs after it, so a QHM stop placed here is not cancelled by it the same cycle.
- A QHM stop at or above the price is refused by Alpaca ("stop price must be less than current price", 11-12 01:08 PT
  log) and the hold stays pending; QHM-RESTOP-1112's reconcile guard never moves a recorded price up through the market.
- Thread: the same trading thread that already runs check_exits in these branches; one order-book read plus at most one
  submit per pending hold (usually zero).

## Required questions
Q1: Protection for QHM shares across the open; no change to trade count or size.
Q2: No change in the number of trades.
Q3: Should exist: the same repair already runs while the market is closed and after 10:00 ET. Simplest form: call it.
Q4: Extends the 2026-08-09 closed-market rearm (run_cycle) and QHM-RESTOP-1112; no open PR touches these branches.
Q5: Not static: the change adds no threshold, multiplier or time number; it reuses the windows run_cycle already
defines (its pre-market and opening phases) and QHM's own stop price.
Q6: Static assumption exposed: "stop repair can wait for the trading window". Forward build: one protective-repair
step shared by every run_cycle branch so a new branch cannot omit it.
Q7: Feeds the unmanaged-position alarm (goes quiet once covered) and the nightly audit's naked-position check.
Q8: mtf_bot.log 11-12 lines above; order aeb32759 at 15:02 UTC; gemini_audit_2026-11-12.txt CATASTROPHIC line.
Q9: Replay of 11-12: NVDA rearmed at the 06:13 PT pre-market cycle instead of 07:02 PT. Wrong if a rearm in these
branches ever places a duplicate QH stop or a stop the broker refuses repeatedly.
Q10: Not applicable because this is a protection repair, not a signal.
Q11: No size, frequency or exposure change.
Q12: Strengthens "a stop on every position"; kill switch and paper flag untouched.
Q13: Order book unreadable -> resubmit_pending_stops submits nothing and alerts (existing behaviour).
Q14: No: it acts from its first cycle after deploy.
Q15: The existing rearm log line plus a branch tag.
Q16: Proof line: "rearmed N QHM GTC stop(s)" with "pre-market" or "opening window" the next time a hold is pending then.
Q17: The next morning a QHM hold is pending before 10:00 ET; until then, unexercised.
Q18: The 11-12 naked window is the anomaly; investigated from the logs.
Q19: Changes when protective stops are placed (exit protection): owner approves this exact design.
Q20: Reviewer prompts carry the code and facts only.
Q21: Tests mock the QHM manager; no broker call.
Q22: Read-only: the function already runs live in the closed-market branch every night.
Q23: Unit tests of both branches.
Q24: Not applicable because no availability claim.
Q25: Not applicable because no market statistic.
Q26: T1 Alpaca orders/positions through QHM's existing broker calls.
Q27: data/state/quarterly_holds.json via QHM's existing atomic save.
Q28: In the two run_cycle branches on the trading thread, after check_exits.
Q29: P0: a stop on every position; the nightly audit failed on it today.
Q30: Blind spot: protective repairs living only in the trading window.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because no returns replay.
Q33: Not applicable because no trades are added.
Q34: Not applicable because no new exposure.
Q35: Not applicable because no new exposure.
Q36: Not applicable because no sizing.
Q37: Not applicable because no statistic.
Q38: Reversal: a duplicate stop or a repeated broker refusal in these windows.
Q39: Not applicable because it sizes nothing.
Q40: Protects "a stop on every QHM share": machine check = the nightly audit's naked-position line stays clear.
Q41: Make protective repair branch-independent (Q6 forward build).
Q42: Alpaca orders and positions.
Q43: Protective orders are maintained continuously, not only in the trading window.
Q44: The nightly Gemini audit and the unmanaged-position alarm, independent of this change.
