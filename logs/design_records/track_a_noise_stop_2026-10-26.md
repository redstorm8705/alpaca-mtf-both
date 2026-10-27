# Track A stop beyond measured noise + range target — TRACK-A-STOP-1026 (signed: Claude, 2026-10-26) — READY FOR OWNER DECISION

Approval-ID: TRACK-A-STOP-1026
Covers: execution/day_trade_manager.py, execution/earned_sizing.py, config.py, tests/test_track_a_noise_stop.py
Owner numbers: config.py DAYTRADE_TRACK_A_NOISE_ATR_MULT=6.0; config.py DAYTRADE_TRACK_A_NOISE_SESSIONS=5; config.py DAYTRADE_TRACK_A_NOISE_WINDOW_MIN=30; config.py DAYTRADE_TRACK_A_RANGE_TARGET_FRAC=0.35; config.py DAYTRADE_TRACK_A_RANGE_SESSIONS=20

Owner direction 2026-10-26: TSLA Day trade lost money "and there was absolutely no reason for that given our entry.
Audit this." Owner rule 2026-10-24: never cut a trade in profit (an early-exit version was rejected).

## Problem (measured)
- TSLA 10/26: long 4 sh @ 393.05, stop 384.62 (8.43), half at target 409.91 (2R = +16.86, +4.3%), half riding. The
  day's whole range was 13.54. Best move after entry +4.22 (0.50R) at 13:13 ET; flattened 15:58 at 391.31: -$6.91.
- All 30 Track A entries (2026-09-15 .. 10-26, `logs/day_tier_runner_cron.log` PROTECTED lines), replayed on real
  1-minute SIP bars (`research` scripts in the session scratchpad, rerunnable): median best move 0.42R vs a 2R target
  (touched by 8 of 30); 17 of 30 lose the full -1R; logged stop / prior-session 5-minute ATR has median 2.70, 13 of 30
  under 2x — stops sit inside normal 5-minute noise.

## Replay (walk-forward: chosen on the first 15 trades by time, scored on the last 15; R = each rule's own stop, so
every rule risks the same dollars per trade)
| rule | test R | all 30 | wins | full stops |
|---|---|---|---|---|
| today | -6.82 | -11.71 | 6 | 17 |
| target 0.35 x 20-day median daily range | -4.73 | -6.34 | 8 | 16 |
| stop = max(logged, 6 x prior-session 5-min ATR) + range target | -1.94 | -3.00 | 13 | 5 |
| same with 3x / 4x | -1.50 / -1.69 | -3.09 / -3.57 | 12 | 11 / 8 |
Limits: 30 trades, 10 variants tried; no costs beyond SIP prices; stop checked before target inside a minute (conservative).

## Replay v2 (board Harris/Taleb/Thorp changes applied): dollars with integer shares, walk starts the minute after
fill, target only on trade-through, stop filled at the worse of stop and bar open, noise = 5-session median of the
time-of-day 5-minute ATR, 20-day range from prior sessions only (look-ahead removed), dollar risk capped at today's
per-trade budget (one-share lots keep their logged stop).
| rule | test $ (15) | all 30 $ | worst | median | wins |
|---|---|---|---|---|---|
| today | -177.21 | -219.57 | -62.30 | -3.52 | 6 |
| k=4, f=0.35 | -100.00 | -123.15 | -43.09 | -1.39 | 10 |
| k=6, f=0.35 | -72.31 | -91.59 | -28.50 | -1.51 | 10 |
About 30 variants were tried across both replays; test half is 15 trades on ~7-8 independent days. Track A stays
loss-making after the change; this cuts losses, it does not show an edge.

## Owner change 2026-10-26 ("I thought track A was the larger size tier? We need to reduce that? Approve track A stop
change with changes"): Track A stays the larger tier. Its size rule is NOT changed: shares = today's count (the existing
notional caps: 20% of buying power, single-name, thin-name, day gross), trimmed only by the EXISTING per-trade risk
budget (1.5% of start-of-day equity, 2% at 10/10; about $40 / $53 at $2,650) when the wider stop would risk more.
Replay v3 (same fills as v2, shares capped at today's count and at the $40 budget):
| rule | test $ (15) | all 30 $ | worst | median | wins | dollar risk / trade (median, max) |
|---|---|---|---|---|---|---|
| today | -177.21 | -219.57 | -62.30 | -3.52 | 6 | $7.56, $62.30 |
| k=6, fewer shares (v2) | -72.31 | -91.59 | -28.50 | -1.51 | 10 | <= today's |
| k=6, today's shares, existing budget | -48.26 | -73.01 | -28.49 | -1.46 | 13 | $29.00, $39.56 |
| k=6, today's shares, no budget | -174.88 | -205.46 | -123.71 | -1.46 | 13 | $40.28, $265.60 |
The existing budget is what makes keeping the size work: without it one lot loses $124.

## Design (as approved with the owner's change)
1. Track A stop distance = max(today's stop distance, 6 x the 5-session median of the 5-minute ATR in the entry's
   time-of-day window). Fixed k = 6.
2. Shares: the existing Track A sizing, unchanged: never more than today's share count, trimmed only by the existing
   1.5% (2% at 10/10) per-trade risk budget at the wider stop; at least 1 share where the existing floor allows. Dollar
   risk per trade rises from a median of about $8 to about $29, never above the existing budget. No trade skipped.
3. Target: whole lot at 0.35 x the stock's 20-day median daily range (prior sessions only), replacing 2R / half-lot.
4. Fixed settings; no re-fit until 100 Track A trades (then: expanding window, inside the tested grid, one step at a
   time, only past the bootstrap standard error).
5. Fallbacks: ATR or range missing / zero / stale -> today's stop and target. Stops stay stop-market on every lot.
6. Decision stack logs symbol, side, entry, atr5 and its bar count, k, f, the 20 range values' median, both stop
   distances, target distance, budget, shares, change=track-a-stop-1026.
7. Tests: broker mocked; bracket creation, rounding, one-share floor, partial fills, fallbacks. A full-path pre-market
   simulation passes before it is called ready. Track B, Swing, QHM, F6 unchanged.

## Required questions
Q1: Moves the goal by cutting Track A dollar losses about 73% out of sample (v3 replay, today's size kept) while keeping every trade (data collection intact).
Q2: No change to the number of trades; same entries. Holding time can lengthen (fewer stop-outs); one lot per stock and the gross cap still bind.
Q3: Simpler alternative (target-only change) was replayed and recovers less (-$143.69 vs -$73.01 over all 30, v3).
Q4: TA-FOLLOW-1024 (early exit, rejected) and TRACK-A-EDGE-1023 (size cut, not adopted) are different; this widens stops and moves the target, keeping size by dollar risk.
Q5: k=6 and f=0.35 are derived from the walk-forward replay of real trades and scale with each stock's measured noise and range; re-fit only after 100 trades.
Q6: Static assumption exposed: fixed 2R targets and setup-level stops independent of each stock's noise; forward pass: re-fit k/f at 100 trades.
Q7: Connects to Day sizing (via stop distance) only; fails safe to today's stop if ATR is unavailable; unit-tested.
Q8: Every number above comes from the replay of logged trades on SIP minute bars, gathered first.
Q9: Tested on all 30 Track A trades, walk-forward. Expected: about -$7.32 per trade today -> about -$2.43 (v3). Reversal: Track A dollar P&L from fills over the next 20 trades worse than the today-rule counterfactual for the same entries.
Q10: Not applicable because no validated edge is being held back.
Q11: Per-trade dollar risk rises (median about $8 -> $29) but never above the existing 1.5%/2% budget; notional never above today's; entry count unchanged; open time can lengthen, bounded by one lot per stock, the gross cap and the 15:58 flatten. Routed through the risk-path board (done).
Q12: Stops stay on every position; kill switch unchanged.
Q13: Missing ATR or range -> today's stop / today's target.
Q14: No shadow; live after approval and gate.
Q15: The decision stack logs atr5, k, f, both stop distances and the target.
Q16: Proof: day-tier log line with change=track-a-stop-1026 on the first Track A entry.
Q17: Checked on the first live Track A trade against its log line and fills.
Q18: The TSLA loss is treated as a design failure, not "by design".
Q19: Yes, entry stop and exit geometry change; owner approves the exact design.
Q20: Prompts neutral; replay outputs are the evidence.
Q21: Tests mock the broker.
Q22: Not applicable because no new fail-closed control is added.
Q23: A full-path pre-market simulation passes before it is called ready.
Q24: Not applicable because no claim that something does not exist or is not tradable is made.
Q25: Stock statistics (ATR, ranges) are measured from Alpaca SIP bars, not quoted.
Q26: T1 Alpaca bars via data/fetcher.py; fallback to today's stop.
Q27: Logs only; no new files.
Q28: Inside the Day tier's sizing and bracket placement for Track A entries.
Q29: Highest-value open item: Track A is the Day tier's main loss source.
Q30: Blind spot: stops not scaled to instrument noise.
Q31: Walk-forward; about 30 variants tried across both replays; result consistent across the 3-6 band; sample small (15 test trades, ~7-8 days).
Q32: Point-in-time: ATR from the prior 5 sessions' same time-of-day window, range from prior 20 sessions; no look-ahead.
Q33: Slippage not modelled beyond minute-bar prices.
Q34: Not applicable because the change is risk geometry, not a factor bet.
Q35: Across 2026-09-15..10-26 including the 10/22 high-risk day.
Q36: Smaller notional per trade at $25K; liquid megacaps.
Q37: 30 trades; confidence wide; reversal criterion on the next 20.
Q38: Non-working = Q9 reversal; revert to today's geometry.
Q39: Size keeps today's share count, trimmed by the existing per-trade risk budget; P&L from fills.
Q40: Protects Day-tier P&L; check: Track A mean R on the next 20 trades vs today's -0.39R.
Q41: Time of day is already in the noise measure; an expert would also condition the multiple on the VIX regime; queued for the 100-trade re-fit.
Q42: Raw data: logged entries, Alpaca SIP minute bars.
Q43: Volatility-scaled stops with equal dollar risk (standard practice).
Q44: Independent evidence: board, Codex and GAI review of the replay and the design.
