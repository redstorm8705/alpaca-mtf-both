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

## Confluence 2.0 — BGGN design (2026-09-27)
**Inputs:**
- Five research tracks, cited catalogs in `logs/design_records/confluence2_research/c2_track{A..E}.md`: A academic, B open-source ML, C swing methods, D regime/validation, E day tier.
- Four cold board seats: Simons+LdP, Asness+J&T+Weinstein, Thorp+Taleb, McKinney+Harris.
- Gro and GAI, 4 rounds. Prompts and replies are in `confluence2_research/fork_c2*`.

**Facts verified at source (2026-09-27):**
- Alpaca SIP daily bars exist for delisted tickers: TWTR through 2022-10-27; SIVB and FRC for Oct 2022.
- Alpaca's native 4H bars start at 08:00/12:00/16:00 ET, so the first bar mixes in pre-market. RTH 4H bars must be built from 1H bars.
- Every indicator reads `df.iloc[-1]` (moving_averages, macd, rsi, vwap), so closed bars are a convention, not a mechanism.

### Design (aligned: board 4/4, Gro, GAI)
1. **Swing features (daily/weekly, closed bars).** Nine of today's twelve points measure one trend; they collapse into the features below.
   - 52-week-high proximity (George-Hwang).
   - Residual 12-1 momentum vs SPY/sector, skipping the last 21 days (Blitz-Huij-Martens).
   - Frog-in-the-pan continuity, used as a weight multiplier on momentum (Da-Gurun-Warachka).
   - Sector relative strength (confirmation).
   - One weekly trend-structure feature (EMA13>EMA30 on weekly bars).
   - PEAD/SUE from FMP.
   - Option IV skew / put-call, used as confirmation.
   - Kaufman efficiency ratio.
   - RSI-in-range and near-VWAP leave the swing score.
2. **Swing timing.** A break of a meaningful level (52-week high or base top), then a back-test and a hold, on closed RTH 4H bars with volume confirmation.
   - SPY 5-min bar-over-bar becomes a lab-tested FEATURE, not a hard gate. Gro and GAI converged after a counter-prompt round: no evidence it predicts 2–20 day returns.
   - The live swing gate changes only with an amendment to Architecture Invariant #1 (board vote + Rafael).
3. **Day tier, 15-min layer.** It is independent of the swing score (6/6 voices).
   - Fixed ~10 names, ranked daily by time-of-day-normalised RVOL; only the in-play subset trades.
   - ORB with bar-close confirmation.
   - First-30→last-30 minute momentum as confirmation.
   - GEX regime: long gamma favours mean reversion, short gamma favours breakout.
   - Costs are modelled on IEX.
4. **Decision model.** A fixed, sign-constrained feature set weighted by exponentially-weighted rolling IC (floored at 0).
   - A small logistic meta-label model (≤5 inputs) outputs P(win) and feeds Kelly's inputs within the existing caps. It is never a stacked multiplier.
   - GBM/deep models are rejected at a few hundred labels (PBO trap).
   - Label: triple barrier (TP 2.5 ATR, SL = stop, vertical 20 trading days); a vertical exit is signed by its realised return.
5. **Regime.** The HMM filtered probability (SPY returns, realised vol, VIX/VIX3M, breadth) is a continuous weight mixer.
   - Momentum is down-weighted in post-decline, high-vol states (Daniel-Moskowitz). The regime can only dampen size, never amplify it.
6. **Retrain loop.** Runs in an OCI cron, off the scan process.
   - **Daily after the close:** IC weights, an incremental meta-model step, drift monitors (IC trend, PSI/KS, Brier/ECE).
   - **Weekend:** full purged + embargoed walk-forward refit. Purge = 20 days; embargo = 5 days `[hypothesis — check against our vol autocorrelation]`.
   - **Acceptance:** DSR against the configs actually searched, PBO under a pre-set ceiling, and t≥3.0 for any new feature. The DSR/PBO numeric thresholds are set in the lab: Gro proposed 0.8/0.05, GAI 0.95/0.05.
   - **Promotion:** retrain → validate → write a staged artifact → atomic symlink swap for the NEXT session only.
   - **Limits:** a per-day step limit on weight change. On failure or drift, fall back to the last validated model; never a trading halt (GAI's "PSI>0.2 → halt" was rejected).
7. **Lab data.**
   - Alpaca daily bars since 2016, universe S&P 500 + Nasdaq-100, point-in-time membership from free constituent-change tables in a versioned CSV. Delisted names are included (verified available).
   - FMP is cached for multiple days, with the daily calls reserved for the earnings calendar.
8. **Ship order** (lab first; no shadow periods):
   - E0: a closed-bar gate at the fetch layer (`strip_forming_bar`), central, making forming-bar reads impossible.
   - Lab build.
   - Swing features with a percentile floor calibrated to today's trade count. This is a selectivity change; if the replay moves entry rate beyond the bound Rafael sets, it becomes risk-path.
   - IC weighting.
   - Meta-label → Kelly (risk-path, board).
   - Regime dampener.
   - Day-tier layer (board on its initial caps).

**Rejected:**
- Every "shadow mode" / "logging-only first" proposal (GAI round 1, the risk seat, the ML seat draft).
- PSI-triggered trading halts.
- Time-series foundation models (roughly tie a random walk).
- Wholesale Freqtrade strategies.
- Gro's round-1 look-ahead "forward-return" momentum and its inverted VIX rule, both conceded in round 2.

**Needs Rafael:**
- (a) An Architecture Invariant #1 amendment for the swing tier after the lab result.
- (b) Approve free public index-change tables (e.g. Wikipedia) as reference data for the point-in-time universe.
- (c) The entry-rate bound for the "selectivity-only" path (the risk seat suggests ≤20%).

**⏩ Next:** E0 closed-bar gate (full patch sequence).
