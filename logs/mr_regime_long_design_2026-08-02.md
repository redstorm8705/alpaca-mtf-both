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

---

## DIFF 3 — ENTRY WIRING: FINALIZED DESIGN (board + Gro + GAI aligned 2026-08-02; Rafael chose Option B)

**Architecture: Option B — separate SIGNAL path, but SIZING flows through the SAME clamp/gross/kill (NOT a duplicated sizing routine — "a parallel sizing path is how the invariants get silently bypassed" — masked-loss seat).**

### Resolved (board consensus)
1. **Emit ONCE in `signal_generator.run_scan`** on the 200-bar `_daily_df` (per-symbol Phase-3 loop ~684-844). Attach the MR verdict {eligible, direction, score 0-3, conditions, `strategy="mean_reversion"`} to an MR signal dict matching the base signal shape. **NEVER re-detect in entry_logic** (its ~24-bar window can't compute a 150-SMA — a data landmine). entry_logic only CONSUMES.
2. **Collision = OPTION B (Rafael):** resolve at the signal-LIST level, deterministically (gate-state-independent — NOT coupled to the fail-open runtime counter_trend gate). When MR is eligible on a symbol AND an OPPOSITE tradeable trend signal exists, MR REPLACES the trend signal (one signal per symbol). MR long requires the mean-revert regime (not a coin-flip override). SMCI example: replaces the perma-structural-SHORT with the confirmed-reversal LONG on the bounce.
3. **Sizing:** reduced FIXED 0.5× of the resolved per-trade fraction (`MR_SIZE_MULT=0.5`), staged. NOT MR-score-scaled as primary (the Kelly warmup `(min(12,score)/12)^2` expects the 12-pt score → MR 0-3 collapses to the 0.75% floor). The 0-3 MR score may ride as a secondary nudge only. Reduced size costs ZERO measurement fidelity (edge = size-invariant R-multiples).
4. **DEDICATED Kelly key (MANDATORY):** MR trades book to `long_mr_intraday`/`short_mr_intraday` (add an optional `strategy` dim to kelly `_key`/`record_trade`/`get_risk_pct`, backward-compatible). Without it MR is UNMEASURABLE (pools into trend) AND CORRUPTS the trend Kelly with its right-tailed distribution.
5. **Same NON-NEGOTIABLE envelope as trend:** per-trade Kelly share-clamp, gross-exposure cap, 7% kill, BP pre-flight, min-lot, VOTE-5. MR routes through the SAME `_size` path for these — no bypass.
6. **BYPASS trend-only gates:** counter_trend_block, SPY-direction gate, ORB gate, the 12-pt conviction MIN gate, the 0-12 linear size map.
7. **MANDATORY NEW GUARD — aggregate MR correlation sub-cap (the account-ender the envelope lacks):** total OPEN MR-risk ≤ `MR_AGG_RISK_CAP_PCT` (~0.035 = half the 7% daily kill). In a broad selloff `confirmed_reversal` fires across many correlated crashed names; the gross cap is correlation-blind. Math: 4×(0.5×4.5%)=9% > 7% kill. PLUS a **market-regime gate**: suppress MR LONGS when SPY is in a confirmed downtrend / breadth washout.
8. **Sequential-whipsaw guard:** require mean_reverting on the LONG too (currently a bonus) OR a per-name MR-attempt/day cap — don't perpetually re-catch the same knife.
9. **Stale-bar / edge-consumed guard:** MR detects on a daily close but fires later; skip if the confirmation bar isn't the current session OR price already traveled a set fraction of ATR toward the mean (edge consumed). Tighten the loose >20% price-sanity for the MR subset.
10. **Idempotency:** MR runs as its own pass — check OPEN/PENDING orders (not just filled positions) before submit to avoid a double-fill.
11. **ROLLOUT — LONG-FIRST (decisive):** long-only until ≥30 MR-long trades measure the edge on its own key; THEN short on its own key, INTRADAY-ONLY (short leg has the fatter squeeze/gap tail). `MR_ENABLED` + (for staging) a long-only flag.
12. **Rule-D decision trace (diff 4):** each MR decision logs its full stack (regime, RSI, confirmation, MR score, size mult, which trend signal it replaced) to trade_events.jsonl.

### Build stages (each a separately-gated diff, inert until MR_ENABLED=True)
- **3a** config foundation: MR_ENABLED=False, MR_SIZE_MULT=0.5, MR_MIN_SCORE, MR_AGG_RISK_CAP_PCT=0.035, MR_LONG_ONLY=True, explicit MR_* detector thresholds + kelly dedicated-key support (optional `strategy` dim, backward-compat).
- **3b** signal_generator MR emission + Option-B list-level collision replacement.
- **3c** entry_logic MR consumption pass: reduced sizing through the SAME clamp/gross/kill, bypass trend gates, aggregate MR sub-cap + SPY-regime gate + stale-bar + idempotency guards.
- **3d** Rule-D decision logging for MR entries.
