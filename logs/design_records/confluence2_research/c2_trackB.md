# Research Track B — Open-Source ML / Algo-Trading Feature Libraries & Models
Confluence 2.0 swing entry system (2-20 day horizon) + day-trading layer. Paper bot, $2.5K→$25K.

---

## 1. Microsoft Qlib — Alpha158 / Alpha360

**What it is:** Open-source quant research platform (MIT license, GitHub `microsoft/qlib`). Ships two canonical
feature sets used across its own benchmark suite.

- **Alpha158**: 158 *hand-engineered* features built from OHLCV via `qlib/contrib/data/loader.py`. Categories:
  - KBAR (candle shape): `KMID=(close-open)/open`, `KLEN=(high-low)/open`, `KMID2=(close-open)/(high-low+eps)`,
    `KUP=(high-max(open,close))/open`, `KLOW=(min(open,close)-low)/open`, `KSFT=(2*close-high-low)/open`
  - Momentum: `ROC=Ref(close,d)/close`, `MA=Mean(close,d)/close`, `BETA=Slope(close,d)/close`, `RSQR`
  - Volatility: `STD=Std(close,d)/close`, `RESI` (regression residual)
  - Extrema: `MAX/MIN=Max(high,d)/close`, `IMAX/IMIN` (days-since), `QTLU/QTLD` (80th/20th pctile)
  - Relative strength: `RSV=(close-Min(low,d))/(Max(high,d)-Min(low,d))` (stochastic %K), `RANK`,
    `SUMP/SUMN/SUMD` (RSI-like gain/loss ratios)
  - Volume: `VMA`, `VSTD`, `WVMA` (volume-weighted price vol), `VSUMP/VSUMN/VSUMD`
  - Correlation: `CORR=Corr(close, log(volume+1), d)`, `CORD` (price/volume change correlation)
  - Trend counters: `CNTP/CNTN/CNTD` (% up/down days and their difference)
  - Computed at multiple lookback windows `d` (5/10/20/30/60 typical).
- **Alpha360**: 360 raw features = 6 raw fields (open/close/high/low/volume/vwap) × 60 lookback days, no
  hand engineering — meant to feed sequence models (LSTM/GRU/Transformer) that learn their own temporal features.

**Evidence / benchmark quality:** Qlib's own benchmark table (China A-shares, CSI300, daily bars, 20 seeds per
model) shows **Alpha158 (hand-engineered) beats Alpha360 (raw) on IC and annualized return** for classic
GBDT-family models: DoubleEnsemble/Alpha158 IC≈0.052, AnnRet≈11.6%; XGBoost/Alpha158 IC≈0.050, AnnRet≈7.8%;
vs. HIST/Alpha360 IC≈0.052 (comparable) but with a different/heavier deep-learning model needed to extract
value from raw data. **Takeaway: for a small bot without a large ML training pipeline, hand-engineered
features (Alpha158-style) capture most of the signal that a heavy sequence model would otherwise have to
learn from Alpha360 raw data.** This is China A-share evidence, not US equities — treat as directional, not
a guaranteed transfer.

**Feasibility:** Trivial. All Alpha158 formulas are closed-form pandas/numpy expressions on OHLCV bars — no
GPU, no training needed to *compute* them (only the downstream model, e.g. LightGBM, needs training, and
that runs fine CPU-only on an ARM VM). Directly reusable in `indicators/` as new feature columns.

**License:** MIT. Fully reusable.

**Verdict:** High-value, low-cost. Recommend lifting the KBAR candle-shape set (KMID/KLEN/KUP/KLOW/KSFT),
the RSV stochastic, and the CORR price-volume correlation feature into the bot's feature set — all are
missing from a typical MACD/RSI/EMA stack and are cheap to compute per bar.

Sources:
- https://github.com/microsoft/qlib/tree/main/examples/benchmarks
- https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md
- https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py

---

## 2. WorldQuant "101 Formulaic Alphas" (Kakushadze, 2016)

**What it is:** arXiv:1601.00991 / Wilmott Magazine 2016. 101 explicit, code-like formulas for real
production alphas used at WorldQuant, released with permission. Built from daily close-to-close returns,
open/high/low/close, volume, and vwap, plus a handful using industry/sector classification (a minority not
computable from pure OHLCV).

**Key published facts (from the paper's own text):**
- Average holding period across the 101 alphas: **~0.6–6.4 trading days** — squarely inside a 2-20 day swing
  window for the longer-holding-period alphas in the set, and better suited to a day/short-swing layer for
  the short end.
- Average **pairwise correlation across the 101 alphas is low (~15.9%)** — i.e., the set is intentionally
  diversified, not 101 versions of the same momentum signal. Useful validation heuristic: if a new candidate
  feature correlates >50-60% with an existing scoring input, it's probably redundant, not additive.
  (This is the same lens the ANTI-SILO mandate demands — check correlation before wiring in a new signal.)
- The paper explicitly notes turnover has **poor explanatory power for alpha correlation** — i.e., don't
  assume two low-turnover (slow) alphas are related just because they're both slow.
- Well-known, widely-reproduced example formulas (many public re-implementations, e.g. `github.com/lvlh2/alpha101`):
  - **Alpha#101**: `(close - open) / (high - low + 0.001)` — a same-day range-position momentum alpha:
    goes long when the stock closed strong within its own daily range. Directly portable — this is
    conceptually identical to Qlib's `KMID2`.
  - Simple mean-reversion form the paper itself cites: log(today's open / yesterday's close) — an overnight-gap
    mean-reversion signal.
  - The broader family (public re-implementations) relies heavily on cross-sectional `rank()`, `correlation()`,
    `decay_linear()`, `ts_max`/`ts_min`, and `delay()` operators applied to price/volume/vwap — i.e. these
    are RELATIVE (rank-across-universe) alphas, built for a broad tradeable universe, not single-symbol
    absolute signals. That is the single most important adaptation note for this bot: our scanner already
    runs a bounded watchlist, so any 101-alphas formula needs to be re-expressed either as a rank across the
    watchlist that cycle, or as a single-symbol z-score against its own history — the raw formula assumes
    cross-sectional data qlib/Barra-style institutions have and this bot does not (a full liquid-universe
    daily cross-section).

**Evidence / validation quality:** This is a *disclosure* paper, not a walk-forward validation study — it
documents live/production alphas at a real fund with aggregate stats (holding period, correlation, turnover)
but does NOT publish per-alpha backtests, Sharpe, or out-of-sample splits. Treat the formulas as
well-motivated hypotheses to test in-house, not pre-validated edges — this bot's own front-loaded simulation
(BUILD DOCTRINE Rule C) is still required before any of these ship.

**Feasibility:** High — all are closed-form pandas expressions on OHLCV/vwap; no GPU or heavy compute. The
main engineering cost is the cross-sectional rank operator, which requires computing the formula across the
whole scan universe each cycle (bounded — the bot's watchlist is already small), not per-symbol in isolation.

**License:** The formulas as published in the arXiv paper are public/academic; the "alphas" themselves are
described as proprietary to WorldQuant, released "with permission" for the paper — reproducing the *formulas*
for research/education is standard practice (widely done, e.g. multiple GitHub reimplementations), but this
is not a redistributable dataset or executable product from WorldQuant itself.

**Verdict:** Medium-high value as a hypothesis generator for the momentum/mean-reversion signal layer,
especially the intraday-range-position family (Alpha#101-style) which maps cleanly onto the existing
confluence-score OHLCV inputs. Needs adaptation from cross-sectional-rank form to single-symbol/bounded-watchlist
form before use.

Sources:
- https://arxiv.org/pdf/1601.00991
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2701346
- https://hedgefundalpha.com/strategies/101-formulaic-alphas/
- https://github.com/lvlh2/alpha101

---

## 3. Hugging Face — Time-Series Foundation Models (Chronos, TimesFM, Moirai, Lag-Llama)

**What they are:** Pretrained, zero-shot general time-series forecasters — Amazon Chronos (built on a
T5-style encoder-decoder that tokenizes scaled values), Google TimesFM (200M-param decoder-only
transformer, pretrained on ~100B time points from Google Trends/Wikipedia pageviews/synthetic data), Salesforce
Moirai ("any-variate" attention, mixture-distribution output for uncertainty), and Lag-Llama (decoder-only,
lag-conditioned probabilistic forecaster). All are available on Hugging Face with inference-only zero-shot
usage (no fine-tuning required to get a forecast).

**Evidence on financial/equity use — this is the important finding:**
- A 2026 rolling-origin study (Alonso & Franklin) testing pretrained TSFMs on five liquid US equities found
  TSFMs win 8 of 10 task-level comparisons against a naive baseline, but **improvements over a plain
  random-walk benchmark are small and reach statistical significance in only 2 of the model-asset pairs
  tested.** Positive skill showed up more clearly on GOOG/AMZN than other names — no consistent edge.
- A broader financial-domain benchmark ("FinVerse", 116,897 financial series, 43 public foundation models)
  found that **strong generic time-series forecasting rank does NOT translate into useful financial
  forecasts** — i.e. leaderboard performance on general TS benchmarks (weather, traffic, etc.) is not
  predictive of usefulness on price series.
- A practitioner write-up pointing Google's TimesFM directly at stock data found it landed at ~47% directional
  accuracy on a random walk — explicitly noted by the author as "correct behavior, not a defect": a model
  that appeared to beat a random walk would be manufacturing false structure.
- General finding across the TSFM literature reviewed: these models "cannot predict values outside the
  training-set range" (a real constraint for breakout price levels) and the base Chronos architecture has no
  native support for exogenous/covariate inputs (Chronos-2, released 2026, adds multivariate/covariate
  support — worth re-checking later, but adds real complexity).

**Feasibility on an ARM VM, Python 3.10, no GPU:** Marginal-to-poor. Chronos/TimesFM/Moirai/Lag-Llama base
sizes run CPU-inference-capable at small parameter counts (Chronos-small/TimesFM-200M), but per-symbol daily
inference at scan cadence adds real latency and dependency weight (torch/transformers stack) for a benefit
the evidence above does not support for single-stock price forecasting.

**License:** Apache-2.0 (Chronos, TimesFM) / mixed (Moirai typically Apache-2.0, Lag-Llama Apache-2.0) —
licensing is not the blocker; evidence of edge is.

**VERDICT — foundation time-series models do NOT clear the bar for swing entries right now.** The best
available 2026 evidence says they roughly tie a random walk on single-name US equity price forecasting, with
occasional, inconsistent per-symbol exceptions. Given this bot's PROFITABLE > PERFECT and NO-GUESS mandates,
shipping a TSFM into the entry gate today would be adding compute and complexity with no demonstrated edge
over the existing OHLCV-derived confluence signals — this is exactly the kind of foundation-model hype this
project's NO-GUESS rule exists to filter out. Worth a revisit only if a *covariate-aware* model (Chronos-2,
Moirai-2 "less is more" successor) publishes equity-specific validated results — not before.

Sources:
- https://arxiv.org/html/2606.27100 (Pretrained TSFMs for Financial Return Forecasting)
- https://arxiv.org/pdf/2609.04917 (AI in Equity/Crypto Markets: Progress, Profitability Evidence, Limits)
- https://dev.to/michaelhairetis/i-pointed-googles-time-series-foundation-model-at-the-stock-market-2nih
- https://www.amazon.science/blog/introducing-chronos-2-from-univariate-to-universal-forecasting
- https://huggingface.co/amazon/chronos-2
- https://arxiv.org/pdf/2511.11698 (Moirai 2.0)
- https://arxiv.org/pdf/2608.14106 (Forecast Collapse in TS Foundation Models)

---

## 4. Hugging Face — FinBERT & Financial Sentiment Models

**What it is:** ProsusAI/finbert — BERT further pretrained on 1.8M Reuters TRC2 financial news articles,
then fine-tuned for 3-class sentiment (positive/negative/neutral) on the **Financial PhraseBank** dataset
(4,845 analyst-annotated sentences). Multiple community forks exist (ahmedrachid/FinancialBERT,
rajaadil/finbert-finance-sentiment, likith123/SSAF-FinBert, etc.) trained on similar or extended data.

**Evidence / accuracy:** ProsusAI/finbert reports 97% accuracy on the subset of Financial PhraseBank with
100% inter-annotator agreement, dropping to 86% on the full dataset including lower-agreement sentences —
i.e. real-world accuracy on ambiguous financial text is closer to mid-80s%, not high-90s%. Community
fine-tunes report 80-82% on held-out test splits. **Caveat: Financial PhraseBank is analyst-report-style
language, not breaking news headlines or social-media text — a real domain-shift risk if fed live news
headlines rather than analyst prose.**

**Free news/text source to feed it:** This bot already has a T2 FMP economic-calendar/fundamentals feed and
an existing `events/news_monitor.py`. The realistic free options for feeding a sentiment model are (a) the
headlines already flowing through `news_monitor.py`'s RSS/news sources (reuse, don't add a new tier), or
(b) FMP's stock-news / press-release endpoints if available under the existing FMP key (T2, 250 calls/day
free — would need to check whether FMP's plan includes a news endpoint before committing to this without
another API key). No new paid API is justified given the ANTI-SILO mandate's "adds signal / fails safe /
stays testable" gate — reuse the existing news pipeline first.

**Feasibility:** Good — FinBERT (bert-base sized, ~440MB) runs CPU-inference fine on an ARM VM at the news
volume this bot would realistically see (dozens of headlines/day, not high-frequency tick-level text),
unlike the TSFM case above where the negative case is about *edge*, not compute.

**License:** Apache-2.0 (Financial PhraseBank has a research/academic-use license — verify commercial-use
terms before shipping to a live-capital system; fine for the current paper-trading phase).

**Verdict:** Worth a shadow trial — wire FinBERT sentiment as a confluence modifier (never a sole gate,
per Architecture Invariant #2 — CAUTION/MONITOR keywords are display-only) fed from the bot's *existing*
news pipeline, logged for N samples before any board vote to make it scoring-additive. This is exactly the
kind of ANTI-SILO interconnection the mandate calls for: an existing news_monitor signal currently classified
only for macro-event type could also feed a per-symbol sentiment score into the confluence stack, with a
neutral fallback when no fresh news exists (fail-safe requirement).

Sources:
- https://github.com/ProsusAI/finbert
- https://huggingface.co/ProsusAI/finbert
- https://medium.com/prosus-ai-tech-blog/finbert-financial-sentiment-analysis-with-bert-b277a3607101
- https://huggingface.co/peejm/finbert-financial-sentiment

---

## 5. Kaggle Competitions With Public Solution Write-ups

### 5a. Jane Street Market Prediction (2020-21)
High-frequency trade-profitability classification on anonymized features. Public solutions
(scaomath/kaggle-jane-street, abdelghanibelgaid, JLFDataScience) converged on: (1) **autoencoder +
MLP** architectures to denoise/compress anonymized features before a downstream classifier; (2) averaging
the multiple provided response/return horizons into one label to reduce noise rather than optimizing a
single noisy target; (3) PCA/encoder-decoder denoising tested but reported **no significant improvement**
over raw features — a useful negative result: complex denoising didn't beat simple feature use here.
**Transfer lesson:** averaging/aggregating multiple noisy forward-return horizons into one training target
is directly reusable for a swing-horizon label design (e.g. average 5/10/20-day forward returns rather than
picking one arbitrary horizon).

### 5b. Optiver Realized Volatility Prediction (2021)
Predict short-term realized volatility from order-book + trade data. Public write-ups (7th place: 
michaelpoluektov/orvp; ~37th place: fritz-cremer) show winning approaches centered on **windowed order-book
statistics** (WAP — weighted average price — volatility per 10-second bucket, bid/ask spread stats, trade
volume) aggregated into 60×10-second interval features per stock-time_id. The notable 7th-place finding: a
"real stock price reverse-engineering" trick (re-ordering anonymized time_IDs via a Hamiltonian-path
heuristic) scored well on the leaderboard but was explicitly flagged by the author as **not a real-world
transferable technique** — a caution about leaderboard-only tricks vs genuine feature engineering. **Transfer
lesson:** WAP-based short-window volatility features (this bot does not currently compute a intraday WAP
volatility measure) are a legitimate, well-evidenced addition for the day-trading layer specifically.

### 5c. JPX Tokyo Stock Exchange Prediction (2022)
Rank ~2,000 stocks by expected return; scored on spread between top-200 and bottom-200 baskets. Winning
teams (Shoki Sakai, team flaty, team aa — public YouTube "Kaggle Winners Walkthroughs") are documented via
video rather than static write-ups; the competition's core lesson (consistent across quant-ranking
competitions generally) is that **cross-sectional ranking robustness (rank stability across time, not just
raw IC) matters more than raw prediction accuracy** for a long/short-basket-style evaluation — directly
relevant if Confluence 2.0 ever moves toward ranking a watchlist rather than pure per-symbol threshold gating.

**Evidence caveat across all three:** Kaggle leaderboard performance is well-known to reward
competition-metric overfitting (the ORVP time_ID reverse-engineering trick is the clearest example found
here) — treat these as feature-engineering idea sources, not validated trading edges. None of the public
write-ups reviewed report realistic transaction-cost-adjusted, live/paper trading results — they are ML
competition scores, not P&L.

Sources:
- https://github.com/scaomath/kaggle-jane-street
- https://github.com/michaelpoluektov/orvp
- https://www.kaggle.com/competitions/optiver-realized-volatility-prediction/writeups/fritz-cremer-public-37th-solution
- https://github.com/J-Quants/JPXTokyoStockExchangePrediction

---

## 6. Freqtrade / Jesse / QuantConnect Community Strategies

**Freqtrade (crypto-focused, GPL-3.0):** Community strategy sites (FreqST etc.) publish backtests, but the
Freqtrade community's own consensus (confirmed via a widely-cited Reddit test: 42 public strategies tested
across 8 years of BTC data, **33 of 42 lost money** under an independent test config) is that published
community-strategy backtests are **not credible without independent re-verification** — common failure modes
cited: lookahead/future-data leakage, curve-fit parameters, and backtest-vs-dry-run divergence. Freqtrade's
own docs explicitly warn against trusting these leaderboard sites. **Verdict: do not import a Freqtrade
community strategy wholesale; at most, mine indicator *ideas*, and re-validate everything through this bot's
own front-loaded simulation.**

**QuantConnect (Alpaca-adjacent, LEAN engine, Apache-2.0 core):** Has a genuinely more credible mechanism —
the "Strategies" community library **re-backtests every listed strategy daily on true out-of-sample data**
as time passes, and the "Alpha Streams" program requires technical review before a strategy can be licensed
to funds. This is a materially stronger evidence bar than Freqtrade's static, self-reported backtests.
**Feasibility note:** QuantConnect/LEAN is a full separate backtesting engine (C#/Python, its own data
subscription model) — not something to run inside this bot's stack, but its publicly documented
Alpha-Streams-accepted strategy *types* (the common categories LEAN's framework model expects: alpha model →
portfolio construction → execution model, cleanly separated) is a useful architectural reference for keeping
signal-generation, sizing, and execution decoupled — which this bot already does reasonably well via
`entry_logic.py` / `kelly.py` / `broker.py` separation.

**Jesse (crypto-focused, MIT license):** Not surfaced with distinct evidence beyond general community
strategy sharing similar to Freqtrade — same overfitting caveats apply; no additional credible validation
mechanism found beyond what's noted for Freqtrade.

Sources:
- https://www.freqtrade.io/en/stable/strategy-101/
- https://repo-explainer.com/freqtrade/freqtrade-strategies
- https://www.quantconnect.com/docs/v2/cloud-platform/community/strategies
- https://www.quantconnect.com/docs/alpha-streams/common-alpha-features

---

## 7. Indicator Libraries — TA-Lib / pandas-ta / tsfresh

**TA-Lib:** C library (BSD-style license), ~150 classic indicators (SMA/EMA/RSI/MACD/Bollinger/ADX/etc.).
Industry-standard reference implementation — pandas-ta explicitly validates its own output against TA-Lib for
correctness. No new evidence found beyond "these are the textbook indicators" — value here is
implementation-correctness, not edge discovery.

**pandas-ta:** Pure-Python pandas extension (MIT license), 130-150+ indicators, "highly correlated with
TA-Lib." Includes some less-common-but-documented indicators beyond the RSI/MACD/BB core: **Squeeze
(TTM Squeeze, volatility-contraction breakout signal)**, **Aroon/Aroon Oscillator** (time-since-extreme
trend indicator, conceptually close to Qlib's IMAX/IMIN), and Hull Moving Average (HMA, reduced-lag MA).
No independent academic validation found for these specifically in this pass — they are established
technician tools (decades of practitioner use), not academically back-tested, so treat as
hypothesis-generation candidates requiring this bot's own simulation gate, same as the WorldQuant alphas.

**tsfresh (MIT license):** Automated bulk feature extraction (entropy measures, autocorrelation, FFT
coefficients, statistical moments, etc.) — designed for the "extract everything, then statistically filter
by hypothesis test" workflow (feature selection via `tsfresh`'s own significance-filtering, which is its main
methodological contribution vs. hand-picking indicators). No stock-specific validated case study was found
beyond generic examples (documentation shows an Apple-stock feature-extraction *walkthrough*, not a validated
trading result). **Feasibility:** tsfresh is heavier (full statistical battery per window) than the bot's
current per-bar indicator computation — reasonable for an offline research/walk-forward pass (the "Alpha
Decay & Walk-Forward Validation" roadmap item already logged in this project) but not for the live 5-min
scan cycle.

**Additional evidence-bearing indicator surfaced this pass — Kaufman Efficiency Ratio (KER):**
`ER = |close_t - close_(t-n)| / Σ|close_i - close_(i-1)|` over n bars — a 0-to-1 trendiness/choppiness
measure (the basis of Kaufman's KAMA adaptive moving average). Distinguished from the Hurst exponent (also a
trendiness measure but requiring a much larger sample to stabilize, suited to offline/weekly regime studies,
not per-bar signals) by responding within a handful of bars — i.e. **KER is a live-scan-cycle-feasible analog
of the Hurst exponent.** This maps directly onto the ANTI-SILO mandate's "VIX/VIX3M/realized-vol ↔ GEX-regime
confirmation" target: KER is a cheap, per-symbol, closed-form regime/trendiness confirmation signal that
could gate or confirm the existing MRI/regime layer without needing a new data source.

**Licenses:** TA-Lib (BSD-style), pandas-ta (MIT), tsfresh (MIT) — all freely reusable, no license blocker.

Sources:
- https://github.com/xgboosted/pandas-ta-classic
- https://www.pandas-ta.dev/
- https://tsfresh.readthedocs.io/en/latest/text/forecasting.html
- https://www.luxalgo.com/library/concept/market-efficiency-and-regime-persistence-measures/
- https://trendspider.com/learning-center/kaufman-efficiency-ratio/

---

## 8. ML-for-Trading Best-Practice Write-ups

**Stefan Jansen — "Machine Learning for Trading" (3rd ed.), `github.com/stefan-jansen/machine-learning-for-trading` (Apache-2.0-adjacent, open book+code):**
27-chapter, 9-case-study workflow from data sourcing → features → models → backtests → costs → risk →
deployment. Also maintains `zipline-reloaded` (community-maintained fork of Quantopian's Zipline backtester,
Apache-2.0) and `alphalens-reloaded` (factor/alpha analysis tooling, Apache-2.0) — both directly usable
Python libraries, no GPU needed, ARM-compatible in principle (pure Python + pandas/numpy, though zipline has
historically had some C-extension build friction worth testing before committing).

**Hudson & Thames / mlfinlab (based on López de Prado's "Advances in Financial Machine Learning"):**
- **Triple-barrier labeling**: label a trade outcome by whichever of (profit-take barrier, stop-loss barrier,
  max-holding-time vertical barrier) is hit first — directly maps onto this bot's own T1/T2/T3 tranche +
  stop/target structure; the *labeling* technique is valuable even independent of any ML model, as a way to
  define what "a good entry" means for any future walk-forward validation work already logged in this
  project's roadmap.
- **Meta-labeling**: train a secondary binary model to predict whether to *size/take* a primary model's
  already-generated signal (bet size ∈ {0,1} or continuous), rather than trying to improve the primary
  signal's direction call. López de Prado's own framing: this improves precision (fewer false positives) at
  some recall cost, and is the standard technique for combining a directional signal with a "confidence
  filter" — conceptually this bot's own confluence *score* already functions as an ad hoc meta-label (score
  gates whether a directional signal is acted on); formalizing it as a trained meta-model is the natural next
  step once enough labeled trade history exists (same data-threshold-gated approach as this project's
  existing Kelly-warmup / TSMOM-90-day gates).
- **Fractional differencing**: transform a price series with a non-integer differencing order `d` chosen to
  be the *minimum* d that achieves stationarity (via an ADF test) — preserves more memory/predictive
  information than a full first-difference (returns) while still being stationary enough for standard
  statistical/ML methods. This is a real, well-cited (López de Prado, AFML book) technique, not hype, but it
  is a *research/feature-engineering* tool aimed at ML training pipelines, not a live per-bar signal.

**Licensing/feasibility caution:** mlfinlab itself moved from a fully open license to an **open-core /
Patreon-sponsorship gate around 2020** — full-feature access now generally requires payment/sponsorship (some
mirrors of the pre-2020 open version exist on GitHub, e.g. `jmrichardson/mlfinlab`, but treat provenance and
license terms of any such mirror skeptically before using it in a production pipeline). The underlying
*techniques* (triple-barrier, meta-labeling, frac-diff) are fully described in López de Prado's published book
and academic papers, so they can be **reimplemented from the published methodology** without needing the
mlfinlab package itself — recommended path for this project rather than depending on a possibly-unlicensed
mirror.

Sources:
- https://github.com/stefan-jansen/machine-learning-for-trading
- https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/
- https://hudsonthames.org/machine-learning-trading-essentials-part-2-fractionally-differentiated-features-filtering-and-labelling/
- https://github.com/hudson-and-thames/mlfinlab/blob/master/docs/source/additional_information/license.rst
- https://dlcoder.medium.com/what-i-did-when-open-source-library-changed-to-semi-open-source-4d9e7dab2fc9

---

## Overall Verdicts

**Time-series foundation models (Chronos/TimesFM/Moirai/Lag-Llama) for swing entries: DO NOT ADOPT NOW.**
2026 evidence (Alonso & Franklin rolling-origin study; FinVerse benchmark; direct practitioner test) shows
these models roughly tie a random walk on single-equity price forecasting, with only sporadic,
inconsistent per-symbol exceptions (GOOG/AMZN). No evidence clears this project's NO-GUESS bar. Revisit only
if a covariate-aware successor (Chronos-2, Moirai-2) publishes equity-specific validated backtests.

**Financial sentiment models (FinBERT): WORTH A GATED SHADOW TRIAL.** Reasonable accuracy (mid-80s% on
ambiguous real text, ~97% on clean-agreement text), cheap CPU inference, and — critically — can be fed from
the bot's *existing* news pipeline (`events/news_monitor.py`) rather than requiring a new data source,
satisfying the ANTI-SILO fail-safe/testable gate. Must ship as a confluence modifier only, never a sole gate,
with a neutral fallback on missing/stale news (same fail-safe pattern already used for MRI/GEX).

## Top 15 Transferable Features / Techniques (ranked by evidence strength × ease of adoption)

1. **Qlib KBAR candle-shape features** (KMID/KLEN/KUP/KLOW/KSFT) — closed-form, cheap, evidenced via Qlib's
   own IC/return benchmarks; fills a gap versus this bot's current MACD/EMA/RSI-centric feature set.
2. **Qlib RSV (stochastic %K over window)** — `(close-Min(low,d))/(Max(high,d)-Min(low,d))` — same family as
   Alpha#101's range-position idea; cheap confirmation signal.
3. **Qlib CORR (price/log-volume rolling correlation)** — direct volume-confirmation signal, exactly the
   volume-confirmation shadow this project already has queued (VOLUME_CONFIRMATION_ENABLED) — could refine
   its calibration.
4. **Alpha#101-style intraday range-position momentum** `(close-open)/(high-low+eps)` — near-identical to
   Qlib's KMID2; well-known, cheap, single-symbol-usable without cross-sectional rank.
5. **Kaufman Efficiency Ratio (KER)** — cheap, live-scan-feasible trendiness/regime confirmation; a faster
   analog of Hurst exponent; directly slots into the ANTI-SILO "regime confirmation" target.
6. **WorldQuant-style overnight-gap mean-reversion** (log(open_t/close_t-1)) — cheap, well-documented
   overnight-gap signal, complements existing ATH-proximity and gap-handling logic.
7. **Triple-barrier labeling (López de Prado)** — reusable NOW as the labeling scheme for the already-planned
   walk-forward/IC-recalibration engine (roadmap item) — defines "good entry" using this bot's own existing
   T1/T2/T3 + stop/target structure.
8. **Meta-labeling** — formalizes what the confluence score already does informally (filter a directional
   call by a confidence score); natural evolution path once enough trade history accumulates.
9. **Optiver-style WAP (weighted-average-price) short-window volatility** — evidenced day-trading-layer
   feature not currently computed; Kaggle-validated feature engineering (not P&L-validated, but a real
   microstructure signal).
10. **FinBERT sentiment, fed from existing news_monitor pipeline** — gated shadow trial; no new data source
    needed, satisfies ANTI-SILO fail-safe requirement.
11. **Fractional differencing** — research/offline tool for the walk-forward validation engine; preserves
    memory better than raw returns for any future ML-model feature pipeline.
12. **Jane Street "average multiple forward-return horizons into one label"** — directly reusable target-
    design lesson for defining a 2-20 day swing-horizon label robustly instead of picking one arbitrary N.
13. **pandas-ta Squeeze (TTM Squeeze) volatility-contraction indicator** — a well-known technician tool not
    currently in the bot's feature set; candidate for the swing-entry breakout-timing layer.
14. **Cross-sectional ranking robustness (JPX lesson)** — if Confluence 2.0 ever ranks a watchlist rather
    than pure per-symbol thresholding, prioritize rank-stability metrics over raw point-accuracy.
15. **QuantConnect's alpha/portfolio-construction/execution separation discipline** — architectural
    reference only (not a feature), reinforcing the value of this bot's own signal/sizing/execution module
    separation (entry_logic.py / kelly.py / broker.py).

**Explicitly rejected / do-not-adopt-now:** Chronos/TimesFM/Moirai/Lag-Llama zero-shot price forecasting
(no edge over random walk in 2026 evidence); wholesale import of any public Freqtrade community strategy
(33/42 lost money in independent re-test); depending on the mlfinlab *package* directly (license now
gated/paid — reimplement the published techniques instead).
