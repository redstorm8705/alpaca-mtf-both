# Swing entry rebuild — diagnosis + plan (2026-09-26)
**Frame:** paper account, $2.5K → $25K goal, data collection first, no shadow periods.
**Voices:** Gro, GAI, board seats (López de Prado/Simons; Asness/Jegadeesh-Titman/Brandt). All converge.

## Diagnosis (verified)
- **Mode.** The live "swing" tier runs INTRADAY mode: 36-ticker watchlist plus a dynamic universe, 5-min scan, 15Min/1Hour/1Day bars (`data/fetcher.py:45`).
- **Forming bars.** Indicators read the still-forming bar via `iloc[-1]` (`indicators/moving_averages.py:55-65`).
- **Score.** 12-point total ≥ 8 (paper: `config.py:326-327`; CLAUDE.md's "9/12" is stale). 9 of the 12 points are one trend signal (pairwise correlation 0.6–0.95).
- **52 logged entries** (since 2026-07-29):
  - daily>150SMA, daily>200SMA and RSI-in-range were true 100% of the time; EMA13>EMA30 96%.
  - Score vs 5-day forward return: Spearman 0.02.
  - Entries averaged −0.22 ATR at 5 days, vs +0.38 for all bar-days in the same symbols (underpowered).
  - Win rate 17–20%; median MFE 0.15–0.26R vs targets of 2.1R.
- **Why the filters are dead.** Absolute momentum and SMA checks on a pre-selected uptrending universe are tautologies. Momentum edge is CROSS-SECTIONAL (Jegadeesh-Titman), so the filters must become relative ranks and trend-stage distances.

## Plan (ordered; each step its own gated diff)
- **E0 — closed bars.** Every scoring indicator uses the last COMPLETED bar. This removes the look-ahead mismatch between research and live.
- **E1 — per-candidate / per-trade record.** For every candidate (entered or not), log `trade_id`, setup, continuous features, and decision + reason. Every trade also logs MFE/MAE and its outcome label.
  - Acceptance: an every-entry-has-a-record / every-exit-matches invariant in CI.
  - This is decision telemetry, not a shadow.
- **E2 — offline entry lab** (`research/`, off the trading path).
  - Data: ≥2 years of Alpaca bars on a point-in-time liquid universe (guard against survivorship).
  - Setups:
    1. trend-continuation pullback to VWAP/EMA20 in a top-third relative-strength name;
    2. high-of-day/range breakout with back-test-and-hold (Rafael's preferred logic);
    3. failed-breakout reversal;
    4. breakdown short.
  - Features: relative-strength rank vs SPY and sector (21/63/126 days), ATR-distance to MAs/VWAP, RVOL (time-of-day normalised), range compression, gap, time of day, VIX/term-structure/breadth regime, days to earnings.
  - Labels: triple barrier in ATR, with the stop at the structural invalidation ≤1.5 ATR and a setup-specific target. Uniqueness sample weights.
  - Model: meta-labeling. The setups are the primary signal; an L1/L2 logistic model on ≤8 features predicts P(target first), pooled with setup as a feature until each setup has ~100 samples. Gradient boosting only after ≥300 labels.
  - Validation: purged K-fold with embargo; Deflated Sharpe and PBO (existing `research/deflated_sharpe.py`, `regime_empirical.py`); calibration (Brier/ECE); MDA to prune.
- **E3 — ship live.** Replace the 12-point total with setup + meta-model EV after costs.
  - Gate: out-of-sample EV > 0 after costs across folds and regimes.
  - This is risk-path (entry frequency), so it needs the board gate and Rafael's approval.
  - The model retrains weekly on the historical data plus the live E1 records, with a calibration drift monitor.
  - If E2 finds no out-of-sample edge, that is reported honestly. No ship on in-sample results.

## Realistic target (board factor seat)
- Continuation/breakout setups: 35–45% win rate, 1.8–2.5R payoff, about +0.15–0.30R per trade after costs.
- Reversal setups: 50–60% win rate, 1–1.5R payoff.

## Rejected
- GAI's "3-day shadow mode" and the board's "champion/challenger shadow". Both violate the no-shadow rule. Promotion is decided on the out-of-sample offline result plus the gate.

## Rafael directives (2026-09-26, later) — supersede parts of the plan above
1. **Confluence 2.0 by exhaustive research.** AI agents research free public sources:
   - academic/industry quant work (SSRN, arXiv, AQR, Alpha Architect, Quantpedia, Robeco);
   - open-source ML/algo (Hugging Face, Microsoft Qlib alpha libraries, WorldQuant 101 Alphas, Kaggle, Freqtrade/QuantConnect);
   - professional swing methods (relative-strength ratings, stage analysis, VCP/trend template, anchored VWAP, volume profile, breadth, sector rotation).

   Each candidate is catalogued with its formula, timeframe, data needs, evidence quality (out-of-sample? replicated? post-publication decay?) and citation. The effort is comprehensive, with no shortcuts; a full usage window is acceptable.
2. **Published backtests are leads only.** Every shortlisted candidate is re-tested in our entry lab: point-in-time universe, closed bars, purged walk-forward CV, Deflated Sharpe/PBO. No ship on published or in-sample results.
3. **Dynamic system.** The BGGN designs a system from the evidence, with regime-adaptive weights and features pruned on decay.
4. **Swing moves to HIGHER timeframes.** Daily and weekly, with 4-hour for timing (`SWING_TFS` in `data/fetcher.py:46` is currently unused). The 15-minute layer moves into the DAY-TRADING tier and is optimized there.
5. **Closed bars only, everywhere.**
6. **Retrain DAILY and on weekends** (replaces "weekly").
