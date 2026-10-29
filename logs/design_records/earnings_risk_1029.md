# EARNINGS-RISK-1029 — one earnings-risk signal for every tier (design record, Claude 2026-10-29)

Status: DESIGN — not built, not approved. Evidence: `logs/research/trade_audit_2026-10-29.md`.

## Frame
Paper account, $2.5K → $25K; data collection and edge evaluation first; the bot trades every day; the 7% daily kill
switch (new entries only) is the only daily brake. Never-skip: edge/risk gates change SIZE, not trade count.

## Problem (2026-10-29)
- Swing NOW 2 sh (bought 10-26 at $157.59) gapped −19.5% at the 10-29 open → −$81.56 (−2.6R vs its $142.07 stop).
  `execution/swing_breakout_manager.py` has no earnings input; FMP's plan returns HTTP 402 for NOW, AVGO, MU, SMCI,
  SNDK, and its calendar endpoint is row-capped (28–48 rows).
- QHM GOOGL 1 sh: date known (FMP 10-28), stop kept by `QHM_EARNINGS_STOP_CANCEL_ENABLED = False` → sold at $285.31
  on the gap.
- Day Track A MSFT 3 sh on MSFT's post-report morning: stop room $10.01 while the first 5-min IEX bar ranged $15.35
  → −$30.57 in 6 minutes. The noise estimate (prior 5 sessions, same time of day) does not see a report morning.
- `earnings_preflight.py` reads only `trade_log.json` → 0 tracked symbols every week.

## Proposed signal: `data/event_risk.py`
For a symbol, ATM straddle implied move (existing `data/gex.get_earnings_implied_move_pct`) at the first three weekly
expiries → variance per trading day of week 1 vs week 2 (`front/next`) and week 2 vs week 3 (`next/after`).
- EVENT_WEEK1 / EVENT_WEEK2 when the ratio exceeds a cut-off DERIVED each refresh from that refresh's cross-section of
  names with an FMP date outside the three-week window (median + 3 × MAD of their ratios); OR an FMP date falls inside.
- CLEAR when both ratios are inside the cut-off and FMP has no date inside the window (or FMP is blind on the name).
- UNKNOWN (no options, stale/failed chain) → treated as an event by every consumer (fail-safe direction = smaller size).
- Cadence: rides the existing GEX refresh (the 10-29 probe hit HTTP 429 on a separate burst). Cached per symbol per day.
- Probe 10-29: report weeks ≥1.52 (front) / ≥2.42 (next); no-report names ≤1.08 / ≤1.47; it flagged SNDK and SMCI,
  which FMP cannot see. One snapshot — the cut-off rule above, not this table, sets the line.
Gap risk for a flagged name: `gap = max(2 × implied move, the stock's largest |open − prior close| / prior close over
the last 252 sessions)` (both measured, no fixed percentage).

## Consumers
1. **Swing entry (size only).** When a report falls inside the lot's maximum hold, size by
   `stop_dist = max(ATR stop distance, gap × price)` through the existing `size_order` / `gap_resize`, never below 1
   share when 1 share fits the risk budget's ceiling; trade count unchanged. NOW 10-26 check: largest |open gap| in the prior 252 sessions = −15.3% (2026-04-23, open 87.25 vs 103.07;
   split-adjusted — the raw 5:1 split print on 2025-12-18 must be excluded) → $24.11 vs the $15.52 ATR stop → the
   existing formula ($39.28 risk ÷ ($24.11 × loss 1.064)) gives 1 share instead of 2: −$40.78 instead of −$81.56.
   The pre-report straddle for NOW was not captured, so the 2× implied-move leg is not checked here. Full replay
   on every Swing lot is owed.
2. **Swing open lots before a report (owner decision).** (a) hold as today; (b) trim to the size rule 1 would allow
   today, at the close before the report; (c) close.
3. **QHM.** The detector supplies dates for FMP-blind holds; the stop-through-earnings switch stays the owner's
   ratification question (pending since ~10-12).
4. **Day tier, report morning.** On the first session after a flagged report, noise = max(5-session time-of-day ATR,
   the same estimator on today's bars so far); same risk budget → fewer shares, never fewer trades.
5. **`earnings_preflight.py`.** Tracked symbols = ownership ledger + QHM state; dates from the detector + FMP.

## Rule C (front-loaded sim) — owed before the package
Cases: every Swing lot since 2026-09-29 and every Day trade since 2026-09-15 that held or traded through a report;
6-year Swing breakout replay with earnings gaps. Expected: smaller losses on report gaps, unchanged count. Reversal:
flagged-lot losses not smaller than unflagged-lot losses over the next 20 flagged lots. Size/frequency/concurrency
delta: size can only DECREASE; frequency 0; concurrency 0 → not risk-path by the Rule E definition (reviewer must
confirm independently).

## Open questions
Q1 snapshot history needed before the cross-sectional cut-off is trusted; Q2 2× implied move vs a measured
earnings-day quantile; Q3 option II (b) trim timing (close before vs morning of); Q4 names without listed weekly
options; Q5 rate budget with GEX refresh.

## Review round 1 (2026-10-29) and Rule C replay — trading consumers WITHDRAWN
Reviews: board Thorp/Taleb APPROVE-WITH-CHANGES (the gap distance must not move the resting stop; a flagged name must
never round to 0 shares; trim option (b)); board Harris/McKinney APPROVE-WITH-CHANGES (forward variance, quote age and
spread checks, 30-session hold vs 3-week horizon, candidate coverage, logging schema); GAI APPROVE-WITH-CHANGES; Codex
REJECT (detector gives a week not a date; FMP-blind ≠ CLEAR; UNKNOWN has no sizing rule; size-down is risk-path;
growth value not shown — "persistent undersizing reduces positive expectancy more than it reduces losses").
Codex's last point was tested first (`swing_earn.py`, 6-year daily SIP, live 55-day breakout rule, 2.5 ATR GTC stop,
20-day-low / 30-session exits, unit risk, 5 bps; FMP report dates; 12 stocks had lots spanning a report):
| Book total R | A hold (today) | B size by gap at entry | C trim before report | D close before report |
|---|---|---|---|---|
| discovery 2020–24 (257 lots, 65 span a report) | **+162.4** | +134.1 | +153.9 | +141.5 |
| test 2024–26 (123 lots, 24 span a report) | **+58.2** | +51.2 | +56.6 | +57.1 |
- Holding from the close before the report to the lot's exit added +0.321R per report (n 65, t 1.49) / +0.046R (n 24,
  t 0.12); worst −2.32R / −1.81R; right-skewed (max +4.88R / +6.90R). Every reduction loses R in both halves.
- Limits: 12 large-cap stocks (survivorship toward names with positive post-report drift); FMP-blind AVGO/MU/SMCI/SNDK
  have no dates and are not in the spanning set; NOW (−2.63R on 10-29) is worse than any replayed report loss.
Day-tier report-morning rule (`ta_today.py`, rebuilt Track A signals, live geometry): noise = max(5-session time-of-day
ATR, today's completed-bar ATR) — all signals −0.048 → −0.035R (disc), −0.016 → −0.029R (test); first ENTER per
name-day −0.045 → −0.018R / +0.022 → +0.026R (t < 0.6); high-noise mornings (today/hist > 1.5) +0.152 → +0.054R (n 13).
No consistent improvement → not proposed.
**Decision record:** consumers 1, 2 and 4 are withdrawn (the replay says holding through reports at full size earns more;
the Day-tier rule shows no gain). The NOW loss is the downside of a positive-expectancy hold, not a defect. Remaining
items are the two bugs (preflight blind B3; ledger floor-shrink page after a tier's own stop fill B4) and the QHM
stop-through-earnings ratification (board Thorp/Taleb seat: keep the stop).
