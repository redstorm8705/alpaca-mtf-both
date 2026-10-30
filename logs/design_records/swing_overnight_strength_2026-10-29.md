# Overnight strength hold — SWING-OVERNIGHT-1029 (signed: Claude, 2026-10-29) — DRAFT v1, not approved

Approval-ID: SWING-OVERNIGHT-1029
Covers: execution/swing_overnight.py, run_swing_overnight.py, config.py, tests/test_swing_overnight.py, server-ops

Owner, 2026-10-29: "You need to find a winning day tier strategy ... and not stop until you do ... think dynamically on
the compositions and combinations ... Is R profit the best way to backtest?"

## What the research found (logs/research/day_deep_search_r1..r4_2026-10-29.md)
- 30,235 same-day rules tested; none survives adjusted significance.
- On trend-up days the measured drift is overnight, not intraday (round 3, discovery means per session):
  | Universe | Overnight (prior close → open) | Open → close |
  |---|---|---|
  | 51 stocks | +6.7 bps | +1.0 bps |
  | 20 ETFs | +8.9 bps | −0.7 bps |
- Round 4 finalist (picked on discovery; test read once; 2016-2020 confirmation set never consulted before):
  - Rule: when SPY's prior close is above its 200-day average, take the top quintile of a large-cap universe by
    return from the prior close to 15:25. Buy at the close, sell at the next open. 5 bps cost.
  - Results at 2× overnight buying power, 10 slots:
    | | Discovery 2020-24 | Test 2024-26 | Confirmation 2016-20 |
    |---|---|---|---|
    | net bps / stock-night | +8.6 (t 2.74) | +4.8 (t 0.78) | +10.5 (t 3.86) |
    | $ / night | $3.24 | $2.16 | $2.57 |
    | Sharpe | 1.35 | 0.60 | 1.75 |
    | worst night | −$247 | −$310 | −$128 |
    | beta to SPY overnight | 1.15 | 1.49 | 1.14 |
    | alpha t | 1.01 | −0.60 | 2.71 |
  - Bonferroni p over 5 finalists: test 1.0; confirmation < 0.001; test + confirmation combined 0.001.
  - Deflated Sharpe on discovery alone does not pass (1.35 vs ~2.1 needed for 30k tries); the untouched confirmation
    set carries it.
- Independent re-derivation (Claude, own code, raw Alpaca SIP daily bars, different 30-name list, full-day strength
  proxy), 2016-2020: strong quintile +10.06 bps gross / +5.06 net, t 4.74; all names +6.66 bps gross.
- Most of the return is market drift (all names held overnight on trend-up days: +1.5 / +3.5 / +2.3 bps net). Picking
  the strongest adds about 5-8 bps per night, significant only in 2016-2020.

## Proposed rule (Swing tier, a new overnight sub-strategy; Day-tier trades unchanged)
1. Regime: SPY's prior session close > its 200-day average, from settled Alpaca daily bars. Off otherwise. No shorts.
2. Universe: liquid large caps. Proposed: the tested 50-stock set, minus any name reporting earnings that night or the
   next morning. Earnings come from fmp_client (EARN-SOURCE-1029 when approved); UNKNOWN → excluded.
3. At 15:25 ET rank by return from the prior close (IEX/SIP 5-min bars, T1). Take the top quintile, at most N names.
4. Entry: market-on-close (`time_in_force=cls`) submitted 15:30-15:45 ET.
5. Exit: market-on-open sell (`opg`) the next session.
6. Protection overnight (stop on every position):
   - a GTC stop at the entry minus k × the name's own 99th-percentile overnight loss, measured from its last 250
     nights;
   - cancelled at 09:25 ET before the opg sell is placed, since Alpaca holds shares for both orders.
   - Open question for the board: cancel-then-opg, or skip opg and sell at 09:30 with a market order after cancelling
     the stop.
7. Size:
   - total overnight notional for this strategy so that the worst historical night is ≤ about 3% of equity; at today's
     $2,555 equity that is ~0.5× equity, versus the research's 2×;
   - split equally across the selected names;
   - bounded by the existing K overnight limb (Architecture Invariant #11) and every other cap;
   - fractional shares not used: the 1-share floor applies, so expensive names may not fit and are skipped for that
     night (stated, not hidden).
8. Logging: one decision line per night with the regime, rank table, selected names, sizes, stop levels, and the
   entry and exit fills (trade_events.jsonl, tier "swing", strategy "overnight_strength").

## Size / frequency / concurrency (Rule B)
- New trades: about 5-10 entries per trend-up session.
- New overnight exposure: about 0.5× equity.
- This is a risk-path diff on all three axes: board with the masked-loss seat, GAI + Codex, owner approval.

## Expected effect and reversal criterion
- Expected at 0.5× equity: about $0.50-0.80 per trend-up night at today's equity; it grows with equity.
- It is data collection for an edge that confirmed only on one never-used period.
- Reversal:
  - after 60 live nights, a net mean below 0 bps with t < −1;
  - or any single night losing more than 3% of equity, which triggers a size review.

## Open questions for the board
1. Stop design overnight, and the cancel/exit sequence (Alpaca holds shares for both a resting stop and an exit order).
2. Size: 0.5× equity (worst night ≈ 3%) vs larger for growth.
3. Universe: the 50 tested names vs the current Swing/Day universes.
4. Beta-hedged variant: also short SPY at the close. Not tested yet; would need its own replay.

## Required questions
(to be answered in the board / GAI / Codex review)
