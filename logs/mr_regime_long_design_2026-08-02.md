# Dynamic Mean-Reversion / Regime-Aware LONG layer — DESIGN (item 2, 2026-08-02)

**Status:** front-loaded simulation COMPLETE + validated (Rule C). Build = a sequence of gated diffs.
**Owner directive (Rafael 2026-08-02):** full new long-entry path (not just a gate); build the confirmation-based version.

## The problem this solves
The 12-pt SHORT confluence perma-shorts crashed former high-flyers (SMCI ~$1000→$24): ~5/12 points come purely
from long-term downtrend STRUCTURE (below 150/200 SMA, negative 12-mo momentum), so the name scores 10-12 SHORT
every scan even while ripping +15% off the bottom. The counter_trend gate (PR #55) already BLOCKS shorting the
bounce. This layer builds the other half: LONG the confirmed bounce.

## FRONT-LOADED SIMULATION RESULT (logs/mr_regime_frontload_sim.py; 10 crashed names, ~9 mo, Alpaca T1 daily)
Forward returns of a hypothetical LONG on a structurally-bearish (below-150-SMA) name, by entry signal:

| Entry signal | fwd5 | fwd10 | win(10d) | fwd20 |
|---|---|---|---|---|
| structural-bear only (what bot shorts today) | +0.15% | +0.17% | ~50% | +0.16% |
| **naive MR-long** (oversold + mean-revert, FALLING KNIFE) | **−0.39%** | +0.89% | 54% | +1.66% |
| **CONFIRMED-REVERSAL** (first up-close after RSI<35 oversold) | **+0.71%** | **+1.58%** | 52% | +1.17% |
| **CONFIRMED-REVERSAL + MEAN-REVERT regime** (VR<1 / Hurst<0.5) | **+0.69%** | **+1.78%** | 53% | +1.56% |

**KEY FINDING (the load-bearing design decision):** catching the falling knife (oversold *in-state*) has
NEGATIVE 5-day edge (−0.39%). Waiting for CONFIRMATION (first up-close after oversold) FLIPS it positive
(+0.71% fwd5, +1.78% fwd10). The mean-revert regime filter adds a little at 10d. **The entry MUST be
confirmation-based, never knife-catching.** The naive spec would have shipped a losing signal — the
front-loaded sim killed it before build.

**Honest caveats:** modest edge (~53% win, profit in the right tail → stops/targets matter), modest sample
(205 events), unoptimized first-guess thresholds (RSI<35, first up-close, VR<1). Positive-EV, worth building
live per PROFITABLE>PERFECT; not a slam dunk.

## VALIDATED SPEC
- **Universe/trigger:** structurally-bearish name (close < 150d SMA — where SHORT score sits 10-12).
- **Entry (the edge):** CONFIRMED REVERSAL = RSI(14) was < 35 within the last ~3 bars AND today is the first
  up-close (close > prior close). Secondary confirmer: mean-revert regime (variance-ratio(5) < 1 OR Hurst < 0.5
  on trailing 60 bars).
- **Direction:** LONG (opposite of what the structural short score wants).
- **Expected effect:** ~+1.6–1.8% / trade over ~10 days, ~53% win, right-tailed.
- **Reversal criterion (kill flag):** `MR_LONG_ENABLED` config flag; if live confirmed-reversal longs show
  negative EV over the first ~20 trades, flip off.

## BUILD PLAN (each = its own gated diff, per Rule-A Tidy-First separation)
1. **Detector module** `execution/mr_regime.py` (pure, never-raises, fail-safe, independently testable — the
   counter_trend.py pattern): functions `regime_state(daily_df)` → {variance_ratio, hurst, mean_reverting} and
   `confirmed_reversal(daily_df)` → bool (RSI-oversold-within-3 AND first up-close). No wiring yet. ← BUILD FIRST.
2. **MR confluence score** — a SEPARATE `mean_reversion_confluence` score selected by the regime detector, so
   MR-eligible names are scored by exhaustion/reversal features, NOT the trend-following 12-pt score.
3. **Entry wiring** — a NEW long-entry path in signal_generator/entry_logic that fires on (structural-bear +
   confirmed_reversal + mean_revert), gated by `MR_LONG_ENABLED`. RISK-PATH (new entries = frequency+size) →
   full board + masked-loss seat + cold-2nd + Gro/GAI. Interacts with counter_trend (must not double-fire).
4. **Decision logging (Rule D):** each MR-long decision emits its full stack (regime, RSI, confirmation, score)
   to trade_events.jsonl.

## ANTI-SILO cross-wiring (Rule): the regime detector also confirms/filters — feed `mean_reverting` into the
counter_trend gate (a mean-reverting bearish name is a stronger short-block) and as GEX-regime confirmation.
Each cross-use fail-safe (UNKNOWN→neutral).

## FEATURE DESIGN PROTOCOL answers
1. Data: Alpaca T1 daily bars (fetch_bars, TF_DAILY) — same as counter_trend. 2. Output: signal dict fields +
trade_events.jsonl; detector is pure (no I/O). 3. Integration: new long-entry path, after the structural-short
scoring, gated. 4. Failure mode: detector fail-safe (regime UNKNOWN / insufficient bars → no MR-long, fall back
to trend-only). 5. Board vote: YES — new entry direction + scoring → full board + risk seat.
