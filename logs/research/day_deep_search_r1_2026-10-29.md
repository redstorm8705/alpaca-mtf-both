# Day-tier deep search, round 1 — 2026-10-29 (signed: Claude)

Owner request 2026-10-29: question the method, then search far wider (indicators, Elliott-style structure, published
intraday anomalies, combinations, machine learning). Read-only research. No bot code changed and no orders placed.
Harness and scripts: session scratchpad `day_search/` (paths at the end).

## Plain-English summary

**Bottom line: none of the ~26,900 same-day rules tested passes a multiple-testing-adjusted significance test. One
rule comes close: it was positive in both the discovery and the test years.** It is not a proven winner. It is the
best next candidate to confirm on new data. (Measured.)

**Closest candidate: buy liquid ETFs after an hourly oversold reading (60-minute RSI(14) below 30).** Long only.
Hold for the next 60-minute bar, out by 15:55. Universe: 20 ETFs (SPY, QQQ, IWM, DIA, 11 sector SPDRs, SMH, SOXL,
SPXL, USO, XBI).

Results on a $2,500 account with 4x intraday buying power (positions sized at 1/15 of buying power each; 15 is the
95th-percentile number of open positions in discovery):

| | Discovery 2020-10..2024-10 | Test 2024-10..2026-10 (one look) |
|---|---|---|
| $ per day at 5 bps | $2.03 | $1.81 |
| $ per day at 2 bps (realistic for these ETFs) | $2.53 | $2.32 |
| Sharpe at 5 bps (annualized) | 1.32 | 1.11 |
| t-stat at 5 bps | 2.64 | 1.55 (one-sided p 0.06) |
| Max drawdown at 5 bps | −$366 | −$287 |
| Positions opened per day | ~2.5 | ~2.5 |

- Cost sensitivity: Sharpe 1.85 / 1.62 before costs, and 0.77 / 0.59 at 10 bps. It survives costs.
- By year ($/day at 5 bps): 2020 +3.0, 2021 +1.2, 2022 +4.9, 2023 +0.7, 2024 −0.2, 2025 +2.2, 2026 +2.9.
- Where the money came from: mostly SPXL and SOXL (the 3x index ETFs). Utilities (XLU) and real estate (XLRE) lost.
- **Not significant after adjustment.** The Bonferroni/Holm-adjusted test p-value is 0.30 across the 5 finalists.
  The deflated Sharpe on discovery is 0.07, against 26,900 trials.
- It is a dip-buying, long-market bet. It lost in 2024.
- In dollars it is small: about $450–580 a year on $2.5K, roughly 18–23%.

**Versus the current Day-tier logic.** The bot's Track B entry, replayed over the same 6 years (7,620 signals), earns
+4.8 bps per trade before costs and −0.2 bps after 5 bps when held to the close with no stop. It earns −0.061R with
its live stop. The current logic has no edge in R or in bps.

**What the methodology audit found (the owner's "is the method the limit?" question):**
1. **R units and tight stops did NOT hide an edge.** The same entries priced with no stop and 30m / 60m / close exits
   in bps also have ~0–5 bps gross edge. Tight stops actually help these entries a little (measured, table below).
2. **A flaw that manufactures false winners.** Splitting capital over the trades that end up firing that day uses
   end-of-day knowledge. In a first pass it turned a losing dip-buying rule into Sharpe 2.0, t 4.0. The harness now
   only allows a fixed capital slot per name, decided before the open. The prior studies' day-mean t-statistic has
   the same weighting (no look-ahead in their mean R, but their t can disagree in sign with the mean).
3. **Cost.** Measured real paper fills on 105 Day-tier orders against the SIP quote midpoint: median 1.2 bps paid per
   side, mean 2.1. So 5 bps round trip is about right for single stocks. It is roughly 2x too high for SPY/QQQ/IWM
   (quoted spreads 0.26–0.41 bps) and about right for TQQQ / SPXL / SOXL (spreads 1.5–3.0 bps).
4. **Universe.** Adding 20 ETFs and 32 large caps (liquid in 2020) did not create edges. Frequent technical signals
   carry ~0 gross edge everywhere.
5. **Where edge exists, it is in rare information events, and it decayed.**
   - FOMC-day long, 09:35→14:00: +48 bps/trade in discovery (t 3.5); +8 bps in test (t 0.5).
   - Earnings-day gap continuation: +76 bps in discovery (t 2.4); −23 bps in test; +13 bps on 32 other names.
   - Cross-sectional machine-learning ranking: real out-of-fold predictive power (rank-IC 0.025 per half hour, t ≈ 3).
     But the top-minus-bottom spread is only 6–9 bps per pair, below the ~10 bps a pair costs.

**Recommended next test:** confirm the RSI-60m ETF rule on data never consulted. Two sources:
- the 3 held-out sessions plus every new day after 2026-10-26 (`compare.py --forward`);
- a 2016–2020 SIP backfill, i.e. older data never loaded.

Also test one pre-registered variant chosen on discovery data only: the same signal on SPY/QQQ, executed in SPXL/TQQQ.

## Method

- **Data (measured).**
  - SIP 5-min RTH bars, split/dividend adjusted, Alpaca T1. Sessions 2020-10-26..2026-10-26; half days dropped.
  - Three universes:
    - **u19**: 18 Day names + SPY.
    - **etf1**: 20 ETFs.
    - **big**: 18 Day names + 32 large caps that were liquid in October 2020, + SPY.
  - The 32 large caps were chosen by their 2020 status. Some fell hard later (ZM, ROKU, SNAP, BABA, SHOP, PYPL, INTC),
    which reduces survivorship bias. Names delisted since 2020 (e.g. TWTR, ATVI) are absent, a mild bias.
- **Timing.**
  - A rule decides at a bar's close and enters at the next 5-min bar's open.
  - Flat at the 15:55 close; never held overnight.
  - Stops are filled at the stop, or at the bar open if price gapped through it.
- **Scoring.**
  - Every rule is measured in return per dollar (bps) at 0/2/5/10 bps round trip.
  - Portfolio view: a causal daily book (fixed slot per name) with Sharpe, t, max drawdown, profit factor and hit rate.
  - R is reported where a stop exists.
- **Split.**
  - Discovery is before 2024-10-26. Test is 2024-10-26..2026-10-26.
  - Test rows were computed into SEALED files and read only for the finalists, once (`finalize.py --reveal`).
  - Sessions after 2026-10-26 were never loaded (final holdout).
- **Finalist rule (fixed before reveal).**
  - At least 200 discovery trades or at least 0.2 round trips/day.
  - Sharpe > 0 at both 5 and 10 bps.
  - Best per family and universe with t ≥ 2.5, plus the top 5.
  - Also the earnings rule (t 2.38), declared before the reveal.
  - Total: 5 finalists.

## Methodology audit — numbers (discovery, Day names)

**Stops vs no stop.** Mean over 14 representative entries:
- Rules: EMA 9/21, MACD, RSI(2), Bollinger, Donchian, Supertrend at 15m and 30m; 15-min opening range; gap > 0.5 ATR.

| Exit | Gross bps/trade | Net 5 bps | Causal t | Causal Sharpe | Look-ahead Sharpe |
|---|---|---|---|---|---|
| 30m, no stop | +0.9 | −4.1 | −4.1 | −2.0 | −1.7 |
| 60m, no stop | +1.0 | −4.0 | −3.0 | −1.5 | −1.1 |
| close, no stop | −0.2 | −5.2 | −2.3 | −1.2 | −0.7 |
| close, 0.25 ATR stop | +2.4 | −2.6 | −1.8 | −0.9 | −0.7 |
| close, 0.5 ATR stop | +1.3 | −3.7 | −2.0 | −1.0 | −0.6 |
| close, 1 ATR stop | +0.2 | −4.8 | −2.2 | −1.1 | −0.6 |

- The look-ahead book adds +0.3 to +1.7 Sharpe to these rules (e.g. EMA 9/21 to close: −0.95 → +0.75).
- In its first form it made "buy 15m 20-bar-low breaks" look like Sharpe 2.0 (t 4.0). Causal: ≈0.

**Current logic (Track B replay, 7,620 signals, discovery, no stop):**

| Exit | Gross | Net 2 bps | Net 5 bps | Hit rate (5 bps) |
|---|---|---|---|---|
| 30m | +1.7 | −0.3 | −3.3 | 47% |
| 60m | +0.3 | −1.8 | −4.8 | 48% |
| close | +4.8 | +2.8 | −0.2 | 51% |

Book t is below 1 in every case.

**Where intraday returns live (Day names, discovery).** Overnight +7.4 bps/session vs intraday +6.5. SPY: overnight
+3.5, intraday +2.3. Half-hour profile: +3.8 bps in 09:30–10:00, +3.0 in 14:30–15:00, near zero or negative otherwise.

**Gross edge vs trade frequency (5,942 event candidates).**
- Rules with more than 10,000 discovery trades: none has gross > 5 bps.
- Rules with 100–300 trades: 42% do.
- Edge large enough to beat costs only appears in rare, event-driven trades.

**Vehicle (discovery).** Executing an index signal in a 3x ETF triples $ per trade and leaves t unchanged. Example:
FOMC long in QQQ is +21 bps (t 2.72); in TQQQ +63 bps (t 2.78). It helps only when the gross edge already beats cost.

## Families tried (discovery; trials = rule variants evaluated)

| Family | Trials (u19 / etf1 / big) | Best discovery t (5 bps) | Gross edge diagnosis |
|---|---|---|---|
| Indicators, 5m-60m: EMA/SMA crosses, MACD, RSI(2)/(14), Bollinger, Stochastic, Donchian, ADX, Supertrend, Ichimoku, VWAP side; follow/fade, L/S/LS, position and event modes | 2,550 / 2,550 / 2,550 + 5,100 with corrected hysteresis | 1.67 / **2.64** / 1.33 | 0–4 bps gross; dies on cost |
| Elliott/swing: causal zigzag, Fibonacci 38-62% / 50-62% / 50-79% pullback continuation, HH/HL structure | 132 ×3 | 0.84 / 0.42 / −0.05 | ~0 gross (0.1–0.2 bps at 30–60m) |
| Published anomalies: Gao intraday momentum (4 predictors), gap buckets, lunch reversal, cross-sectional momentum/reversal at 5 times, half-hour periodicity, first-bar volume shock, volume spikes, calendar, FOMC, SPY trend, prior-day reversal | ~780 ×3 | **3.52** (FOMC) / 2.26 / 2.84 | event days only |
| Regime-conditioned (VIX tercile, VIX/VIX3M, SPY vs 20d, realized vol, Mon/Fri, OPEX week, FOMC, earnings day) × 7 base signals | 560 / 520 / 560 | 2.51 / 1.73 / 1.99 | conditioning finds events, not new edges |
| Earnings reaction day (entry time × direction × gap size × exit × stop) | 456 / – / 504 | 2.38 / – / 2.18 | +50–80 bps gross on Day names; weaker on others |
| 2-condition AND combos (15 features, terciles, 3 horizons) | 2,829 ×3 | 2.08 / 1.86 / 1.39 | long-to-close drift variants |
| ML: gradient boosting, purged quarterly walk-forward + 5-session embargo; raw and market-neutral targets at 30m / 60m / close | 86 / 92 / 94 rules | 1.59 / 1.28 / 1.52 | out-of-fold IC real (neutral to close 0.058 pooled; 0.014–0.030 per half hour) but spread < pair cost |
| Lead-lag catch-up vs SPY | 36 ×3 | < 0 | ~0 gross |
| **Total** | **≈26,900** | | |

## Finalists — discovery vs test (test read once)

| Finalist | Disc. net bps/trade (5 bps) | Disc. t | Deflated Sharpe (disc) | Test net bps/trade | Test Sharpe | Test t | p (one-sided) | Bonferroni / Holm (k=5) |
|---|---|---|---|---|---|---|---|---|
| ETF 60m RSI14<30 long, next 60m bar | (position mode) | 2.64 | 0.07 | (position mode) | 1.10 | 1.55 | 0.060 | 0.30 / 0.30 |
| FOMC-day long 09:35→14:00, Day names | +47.9 | 3.52 | 0.27 | +8.2 | 0.36 | 0.50 | 0.31 | 1.0 / 0.93 |
| FOMC-day long, big universe | +23.9 | 2.84 | 0.09 | +3.8 | 0.32 | 0.46 | 0.32 | 1.0 / 0.93 |
| FOMC day, gap direction 09:35→60m, long | +43.5 | 2.51 | 0.05 | +14.3 | 0.75 | 1.05 | 0.15 | 0.73 / 0.59 |
| Earnings-day gap ≥1 ATR continuation to close | +75.8 | 2.38 | 0.04 | −22.8 | −0.50 | −0.70 | 0.76 | 1.0 / 0.93 |

- Deflated Sharpe uses N = 21,823 trials (the finalist pool, before the 5,100 corrected-hysteresis re-runs).
- It uses a robust cross-trial dispersion of gross daily Sharpe and assumes normal returns.
- The bar is an annualized Sharpe of about 2.1; no finalist clears it.

**Test-year exposure, stated honestly:**
- Test rows were read once for these 5 finalists.
- The always-long intraday benchmark's test number was printed once (not a candidate).
- Earlier studies looked at test years for opening-range, Gao momentum and Track B variants.
- After the reveal, a hysteresis bug was found in the indicator helper (below). The corrected "textbook" versions
  (5,100 trials) are worse on discovery (best t 1.39), and their test rows were not read.

## Diagnosis per family (why it lost, and the formula that would win)

- **Technical indicators and Elliott structure.**
  - Gross 0–4 bps per trade; costs 4–5 bps. Frequent signals on liquid names predict about 1 bp.
  - Would win: the same signal at ≤1 bps all-in cost, or about 3x the gross edge. Neither is available.
- **Intraday momentum (Gao et al.).**
  - Gone in 2020–24 on SPY/QQQ: 0.2 bps gross (t −2.6 net).
- **Event days (FOMC, earnings).**
  - Large discovery gross (+50–80 bps) on 30–200 events, then decayed in test.
  - It is a small-sample, regime-dependent premium.
  - Would win: an event drift that persists; it needs many more events to confirm.
- **Cross-sectional ML.**
  - Real but small: 6–9 bps per long/short pair vs ~10 bps pair cost.
  - Would win: costs ≤3 bps per pair (SPY-hedged single leg in tight names) or a feature set doubling the IC.
- **ETF hourly-oversold long (closest).**
  - Gross ~16 bps per 60m hold. It concentrates in market-wide selloffs, so it is mostly a dip-buying beta bet.
  - It lost in 2024 and is weak in defensive sectors.

## Bug found and fixed during the round

The side-aware hysteresis helper in `fam_ind.py` cleared every state on `long_exit | short_exit`. As a result, the
"reversion" rules (RSI, Bollinger, Stochastic, Donchian) behaved as "hold only while the entry condition is true".

Those rules are still valid, causally evaluated rules, and the finalist is described exactly as tested
(`strategies/etf_rsi60.py`). The helper is now correct, and the corrected variants were added as extra trials.

## Limits

- **Coverage.**
  - 6 years, 51 single stocks + 20 ETFs.
  - Signals run on SIP bars; the bot reads IEX intraday.
  - Short borrow fees are not modeled.
  - ETF slot sizing is coarse; on selloffs many ETFs fire together, so it is effectively one correlated bet.
  - ML used sklearn HistGradientBoosting; LightGBM's OpenMP runtime is not installed.
- **Inputs from outside the price data.**
  - FOMC dates were verified against federalreserve.gov.
  - Earnings dates come from FMP (date only; BMO/AMC inferred from the larger gap).
  - CPI dates were unavailable (FMP calendar is plan-blocked), so CPI-day conditioning was not tested.
- **Holdout.** Only 3 sessions (2026-10-27..29) remain untouched.

## Reusable harness (scratchpad `day_search/`)

**Engine.**
- `core.py`: loader, timing convention, causal book, event and position evaluators, sealed test writer.
- `fetch.py` / `update.py`: SIP bar fetch (research) and forward data.

**Families.** `fam_ind.py`, `fam_swing.py`, `fam_anom.py`, `fam_regime.py`, `fam_earn.py`, `fam_combo.py`,
`fam_leadlag.py`, `ml_meta.py` (with `features.py`).

**Selection.**
- `finalize.py` selects on discovery and reveals sealed test rows once, with deflated Sharpe and Bonferroni/Holm.
- `audit_method.py` runs the methodology audit.
- `cur_logic.py` re-measures Track B in bps.

**Testing one new strategy against the current logic.** Write `strategies/<x>.py` with `NAME`, `UNIVERSE` and
`signal(U)`, then run `python compare.py strategies/<x>.py`:
- It reports discovery $/day at 0/2/5/10 bps.
- It compares the strategy with the current Track B on the same days.
- `--forward` (after `update.py`) runs on sessions after 2026-10-26 only, with an optional live-journal comparison.

Strategies included: `etf_rsi60.py`, `fomc_long.py`, `earn_gap.py`. Cost evidence: `costcheck.py` / `costcheck.csv`
(paper fills vs NBBO) and `spreads.py`.
