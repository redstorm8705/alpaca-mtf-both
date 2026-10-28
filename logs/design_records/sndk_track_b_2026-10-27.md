# Add SNDK to the Track B breakout list — SNDK-TRACK-B-1027 (signed: Claude, 2026-10-27) — OWNER-APPROVED 2026-10-27 ("Yes, add it")

Approval-ID: SNDK-TRACK-B-1027
Covers: config.py

Owner question 2026-10-27: "Why hasn't the day tier taken any levered etf trades specifically around the higher priced
stocks like SNDK? It should be tracking SNDK and trigger the etf buy when SNDK breaks out." Owner answer to the
recommendation: "Yes, add it."

## Problem (measured, OCI logs/day_tier_runner_cron.log + config.py, 2026-10-27)
- config.py `DAYTRADE_TRACK_B_UNIVERSE` (the Track B opening-range breakout list, the only Day path that buys a 2x ETF on
  a breakout) has 15 names and no SNDK. SNDK is only in the Track A list, which trades it only on options-wall setups in
  its daily-trend direction (side=SHORT on 10/20-10/27).
- Track A SNDK lines: 10/23 and 10/26 "RIDE: close-through BELOW put_wall is not a fresh cross (bars since the other-side
  close: none since the prior session) — wait" (126 and 179 ticks); 10/27 "gex=STAND_DOWN/NEAR-FLIP" (128 ticks).
- The stock-to-ETF route works when a signal fires: 10/09 "[SNDK] track-A stock fits 0 sh -> ETF SNDQ"; Track B routes
  longs to the 2x bull ETF and shorts to the inverse ETF when the budget buys 0-1 shares (run_day_tier.py Track B block).

## Change
Append "SNDK" to `DAYTRADE_TRACK_B_UNIVERSE`. No logic change: SNDK's Track B longs route to SNXX (2x bull) and shorts to
SNDQ (2x inverse) through the existing leveraged/inverse pivots; every existing gate, size rule, stop and the EOD flatten
apply unchanged.

## Replay (frozen Track B rules, research/day_tier_trackb_watchday_replay.replay_symbol, read-only on OCI, 2025-11-01 ..
2026-10-26, IEX 5m bars, 5 bps cost, priced on the stock)
SNDK: n=85 win 36% mean +0.007R (t +0.05); longs n=44 -0.068R; shorts n=41 +0.086R. About one signal per three sessions.
No edge either way; trading through SNXX/SNDQ adds ETF spread, so expect slightly below break-even.

## Required questions
Q1: Adds about one Day trade every three sessions on SNDK (data collection); no edge claimed.
Q2: Increases the number of trades (more Track B signals); never fewer.
Q3: Simplest form: one list entry; the ETF routes already exist.
Q4: Track B design records (day_tier_track_b_momentum_2026-09-21.md); no conflict; extends the pre-registered list.
Q5: No new numbers; the Track B thresholds are unchanged.
Q6: Static assumption: a hand-kept symbol list per track; forward pass: one shared Day universe for both tracks (owner question 2026-10-27).
Q7: Uses the existing ETF routes, exposure guard (one lot per underlying) and sizing; nothing siloed added.
Q8: Measured from the OCI runner log and the replay above.
Q9: Replay above; expected about break-even per trade; reversal: SNDK Track B mean R over its first 20 trades below -0.3R.
Q10: Not applicable because no validated edge is being held back.
Q11: Frequency rises (one more name screened); size and exposure caps unchanged. Bounded by the daily Day budget and the 7% kill.
Q12: No safety-envelope change; every lot keeps its protective stop.
Q13: Missing SNDK data -> the existing Track B screen returns not-a-mover (fail safe).
Q14: Not a shadow; live.
Q15: Existing Track B decision stack logs every screen, trigger and route.
Q16: Proof: "[SNDK] day-tier TRACK-B SCREEN" lines in logs/day_tier_runner_cron.log on the first session after deploy.
Q17: The runner log line above, in its real process.
Q18: The SNDK no-trade days were traced to the missing list entry, not explained away.
Q19: Changes which stocks are entered; owner approved 2026-10-27.
Q20: Neutral prompts; the replay is the evidence.
Q21: No test reaches the broker; config-only change.
Q22: Not applicable because no new fail-closed control is added.
Q23: Read-only check after deploy that the runner screens SNDK.
Q24: SNXX and SNDQ tradability is recorded in strategy/day_tier_leverage.py (checked 2026-10-09).
Q25: Replay uses Alpaca bars, not quoted statistics.
Q26: T1 Alpaca IEX/SIP bars via data/fetcher.py; existing fallback.
Q27: Logs only.
Q28: Track B loop in run_day_tier.py (the Day runner cron, every 2 minutes).
Q29: Owner-requested; small and immediate.
Q30: Blind spot: names on one track and not the other.
Q31: One symbol added; rules frozen from earlier walk-forward work; no variants tried.
Q32: Replay uses prior-session daily data and the frozen trigger; no look-ahead in the base replay.
Q33: 5 bps per trade in the replay; ETF spread not modelled.
Q34: Not applicable because no factor bet is added.
Q35: Covers 11/2025-10/2026 including the 10/22 high-risk day.
Q36: SNXX 1,056,025 and SNDQ 324,059 IEX shares on 10/08 (strategy/day_tier_leverage.py).
Q37: n=85 replay; CI wide; reversal after 20 live trades.
Q38: Non-working = Q9 reversal; remove SNDK from the list.
Q39: Size from the existing Track B budget and sizing; P&L from fills.
Q40: Protects Day-tier coverage of SNDK moves; check: SNDK Track B screen lines present each session.
Q41: Unify the two tracks' lists (forward pass).
Q42: Raw 5-minute bars and the runner log.
Q43: Opening-range breakout with a liquid leveraged proxy for unaffordable shares.
Q44: Independent evidence: the post-live check of the first SNDK Track B trade against its fills.
