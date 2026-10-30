# Trend-night QQQ overnight hold — SWING-OVERNIGHT-1029 (signed: Claude, 2026-10-29) — DRAFT v2, not approved

Approval-ID: SWING-OVERNIGHT-1029
Covers: execution/swing_overnight.py, run_swing_overnight.py, config.py, tests/test_swing_overnight.py, server-ops

Owner, 2026-10-29: "You need to find a winning day tier strategy ... and not stop until you do ... think dynamically on
the compositions and combinations."

## What the research found (logs/research/day_deep_search_r1..r4_2026-10-29.md; 30,239 candidates in all)
- **No same-day rule survives** adjusted significance (rounds 1-3, about 30,000 rules).
- **The trend-day drift is overnight.** On days when SPY's prior close is above its 200-day average, the measured
  drift is overnight (prior close → open), not open → close:
  - stocks: +6.7 vs +1.0 bps;
  - ETFs: +8.9 vs −0.7 bps (discovery).
  - Independent re-derivation from raw Alpaca daily bars, 2016-2020: overnight +6.7 bps gross on 30 large caps.
- **Picking the strongest stocks adds nothing significant** once SPY's overnight move is removed (beta-hedged net
  +0.04 / −0.87 / +0.59 bps). The edge is the market's overnight drift on trend days, so the simplest instrument is
  the index.
- **Index variant, the selected candidate.** Pre-declared, 4 instruments, every period read once; 0.5× equity; whole
  shares; $2,555 start, compounding:
  | QQQ | Discovery 2020-24 | Test 2024-26 | Confirmation 2016-20 |
  |---|---|---|---|
  | bps of equity / night | 1.53 (t 1.78) | 1.91 (t 1.63) | 1.44 (t 1.95) |
  | Sharpe | 0.89 | 1.16 | 0.90 |
  | annual % | 3.8 | 4.8 | 3.6 |
  | max drawdown | −9.2% | −4.7% | −6.3% |
  | worst night (% equity) | −2.5 | −1.4 | −1.8 |
  - Trend nights per year: about 184-224.
  - Without the trend filter: worst night down to −5.1% and drawdowns to −18%. The filter mainly cuts the tail.
  - Significance: pooled test + confirmation, Bonferroni over 4 instruments, p = 0.023 (QQQ), TQQQ 0.030,
    SPXL 0.041, SPY 0.089. No single period is significant alone.
  - Consistent sign and size in all three periods.

## Proposed rule (Swing tier, new overnight sub-strategy "trend_night"; Day-tier trades unchanged)
1. **Regime:** SPY's latest settled daily close > its 200-day simple average (T1 Alpaca daily bars). Otherwise no
   entry that night. Long only.
2. **Instrument:** QQQ.
   - Highest bps of the four in discovery and test; TQQQ was higher in confirmation (1.70 vs 1.44).
   - QQQ has a smaller worst night than TQQQ in every period, and there is no leveraged-ETF decay or rebalance risk.
3. **Entry:** buy QQQ at the close. Market-on-close if the broker accepts it; otherwise a market order at the 15:55 ET
   5-minute bar.
4. **Exit:** sell at the next session's open (09:30 ET). Market-on-open if accepted; else a market order on the first
   cycle at or after 09:30.
5. **Stop on every position:**
   - A GTC stop rests from entry until the exit sequence at a level from QQQ's own 99th-percentile overnight loss
     over the last 250 nights (point-in-time).
   - Alpaca stops do not trigger outside regular hours, so in a gap the stop fills at the open, which is the planned
     exit anyway. It is the safety backstop if the exit order fails.
   - The exit sequence cancels the stop, confirms the cancel, then sends the sell. On any failure the stop stays and
     the position is paged.
6. **Size:** shares = floor(0.5 × equity / QQQ price), whole shares only so the GTC stop can rest.
   - Alpaca allows fractional shares only on DAY orders.
   - Measured 10-29: QQQ 707.35 (IEX) and equity ~$2,555 → 1 share = 0.28× equity.
   - 2 shares once equity ≥ ~$2,830; the replay assumed exact 0.5×.
   - The design rule is that the worst historical trend night must cost no more than 3% of equity. At 0.5× it was
     2.5%.
   - Bounded by the K overnight limb (Architecture Invariant #11) and all existing caps. If the cap leaves less than
     one share, that night is skipped and logged.
   - QQQ is not in FOREVER6_UNIVERSE. Day-tier Track M also buys QQQ on Monday gap-downs (same-day). Shared-symbol
     ownership is handled by the ownership ledger with tier tag `swing`. Cross-tier exits are qty-bounded to the
     strategy's own lot.
7. **Earnings / events:** none for the ETF. FOMC/CPI nights are not excluded; the replay includes them.
8. **Logging:** one decision line per session in trade_events.jsonl (tier "swing", strategy "trend_night") with:
   - SPY close, its 200-day average and the regime;
   - size inputs and the K-limb room;
   - the entry and exit order ids and fills;
   - the stop id and level;
   - the reason for any skip.

## Size / frequency / concurrency (Rule B)
- About 200 entries a year, 0.5× equity overnight on trend nights.
- Risk-path on all three axes → board with the masked-loss seat, GAI + Codex, owner approval.

## Expected effect, reversal
- Expected: about +1.5-1.9 bps of equity per trend night ≈ $0.40-0.50 per night at today's equity. That is about
  4% a year at 0.5×, scaling with equity and size.
- Reversal: after 100 live trend nights, a mean below 0 with t < −1 → off. Any night losing more than 3% of equity →
  size review.

## Open questions for the board
1. **Size:** 0.5× (worst night ~2.5%) vs 1.0× (expected ~8%/yr, worst night ~5%). Paper growth goal.
2. **Execution:** market-on-close / market-on-open order support in Alpaca paper vs the 15:55 / 09:30 market-order
   fallback. Fill cost assumed 2 bps.
3. **Interaction:** whether this replaces or adds to the existing Track B 5-session hold. They are different signals;
   Track B is a stock-level trend hold.

## Required questions
(answered in the board / GAI / Codex review)
