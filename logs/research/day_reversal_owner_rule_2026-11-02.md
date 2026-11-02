# Day tier — owner's bottom-reversal entry, tested — 2026-11-02 (signed: Claude)

Owner, 2026-11-02: buy the intraday bottom on the shortest timeframe once a candle moves back above the previous
candle, cross-checked against the highest-timeframe trend. Read-only research; no bot code changed.

## Rule tested (5-minute bars, decided at a bar's close, filled at the next bar's open)
- Daily trend (known before the open): prior close above its 50-day average, or 20-day above 50-day. Longs only in an
  uptrend; shorts (the mirror at the session high) only in a downtrend.
- The prior bar printed the session low; this bar holds above it and closes back up:
  - A: above the prior close;
  - B: above the prior high.
- Dip depth below the open: 0, 0.25 or 0.5 × the 14-day ATR. Window: to 11:30 ET, or all day.
- Stop just past the session low (structure), optionally widened to at least 0.25 ATR. Exit at 15:55, or at 2R.
- One trade per name, day and side. Cost: 5 bps round trip (measured live single-stock cost, round 1).

## Data
- SIP 5-min bars, 2020-10-26..2026-10-26, from the round-1 harness cache `big`: 18 Day names, 32 large caps and SPY.
- Discovery years only (before 2024-10-26). The test years were not opened, because nothing passed discovery.

## Result: no version makes money after costs
- 192 variants, plus 64 more with a SPY filter: **0 of 256 have a positive net return per trade.**

| | Gross per trade (median) | Net at 5 bps |
|---|---|---|
| Longs (buy the bottom) | +0.3 bps | −4.7 bps |
| Shorts (sell the top) | −5.4 bps | −10.4 bps |

- Best version: long; SPY also at its own session low (a market-wide dip); close above the prior bar's high; dip
  ≥ 0.5 ATR; 2R target.
  - 2,574 trades: +3.8 bps gross, −1.2 bps net, −0.04R.
  - Its causal daily-book t-stat is −0.34.
- The "stock-specific dip only" filter was worse: long +/−0.4 bps gross.
- Per-day averages look positive on some variants (t up to 2.5) while per-trade returns are negative. Days with only a
  few signals win, and crowded selloff days lose; equal-weighting days overstates the rule. That is the round-1
  weighting artefact, not an edge.

## Why it loses, and what would win
- **Why:** after a 5-minute bottom prints and a candle closes back up, the next move carries no reliable drift. Gross
  edge is about 0 to 4 bps, below the ~5 bps it costs to trade.
- Today's TSLA chart, the first close up after the 9:50 ET low ($397.61), would have entered near $400.31 and run to
  $414. That is one day picked after the fact. Across six years of days like it, the same signal also fires on dips
  that keep falling.
- **What would win:** a version whose gross drift beats cost. Measured round-trip cost is 2.4-4.2 bps on single
  stocks. The best version (+3.8 bps) is close but not there.
- This matches the round-1 to round-4 studies (~27,000 rules, `logs/research/day_deep_search_r1_2026-10-29.md`): fast
  intraday price-pattern entries on these names carry about 0 edge before costs.

## Candidates tried and test-year looks
- 256 candidates, all on discovery data.
- 0 looks at the 2024-10..2026-10 test years.

## Scripts
Session scratchpad `rev/reversal.py` and `rev/spyfilt.py`, on the round-1 harness `day_search/core.py`.
