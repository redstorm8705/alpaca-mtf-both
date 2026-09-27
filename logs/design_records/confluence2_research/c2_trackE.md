# Track E — Intraday (Day-Trading) Signal Evidence Catalog

Frame: paper bot, $2.5K→$25K, day tier trades a ~10-name liquid universe intraday, flat by close.
Goal: evidence-ranked catalog for the 15-min layer moving swing→day tier. Tested evidence vs anecdote
separated per item. All URLs cited.

---

## 1. Opening Range Breakout (ORB) — STRONGEST TESTED EVIDENCE

### 1a. Zarattini & Aziz, "Can Day Trading Really Be Profitable?" (SSRN 4416622, 2023)
- **Rule:** 5-minute opening range on QQQ. Long if price breaks above the 5-min range high (only
  taken if the 5-min bar closed green); short if breaks below the range low (bar closed red).
  Stop = entry ± 1.5×(14-day ATR). Exit = flat at the close, every day (no overnight).
  Position size set so a stop-out loses ~1% of account equity; leverage/3x leveraged-ETF variants
  explored to scale returns.
- **Evidence:** QQQ, 2016–2023 (~8 years). Reported ORB portfolio significantly outperforms QQQ
  buy-and-hold on a risk-adjusted basis; leveraged variants amplify both return and volatility.
  Costs modeled (commissions/slippage) — paper's stated finding is the edge survives realistic
  cost assumptions, though exact after-cost Sharpe/CAGR numbers were not independently confirmed
  in this pass (SSRN abstract page returned 403 to direct fetch; description above is drawn from
  the paper's public abstract/secondary summaries, not the full PDF — **[hypothesis-level detail,
  verify against full PDF before hard-coding parameters]**).
- **What decays:** edge is concentrated in the first-5-minutes range; a widely-published rule is
  a crowding risk (Crabel's original 1990s edge on futures opening ranges is anecdotally described
  as "arbed away" as it became mechanized — no rigorous decay study found in this pass).
- **Data needs:** 5-min OR bar, 14-day ATR, EOD flat.
- **Regime-adaptive path:** width of the OR (range as %ATR) and VIX level are natural conditioning
  variables — a wide OR relative to normal range predicts more false breakouts (mean-reversion
  regime); a narrow OR (Crabel's NR7 concept, below) predicts higher breakout follow-through.
- Sources: [SSRN 4416622](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622), [Semantic Scholar summary](https://www.semanticscholar.org/paper/Can-Day-Trading-Really-Be-Profitable-Evidence-of-in-Zarattini-Aziz/4d55f526cc56f08662cb8976796cd3b719ef6d2b), [ResearchGate](https://www.researchgate.net/publication/370246583_Can_Day_Trading_Really_Be_Profitable_Evidence_of_Sustainable_Long-term_Profits_from_Opening_Range_Breakout_ORB_Day_Trading_Strategy_vs_Benchmark_in_the_US_Stock_Market)

### 1b. Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy for the U.S. Equity Market" / "ORB for Stocks in Play" (SSRN 4729284, 2024)
- **Rule (from QuantConnect's public replication of the same strategy family, since the SSRN PDF
  itself 403'd on direct fetch — treat the exact numbers below as a *replication*, not the primary
  paper's own reported numbers):**
  - Universe ("stocks in play"): top 1,000 most liquid US equities, price > $5, ATR > $0.50; each
    day select the ~20 names with the highest **relative volume in the first 5 minutes** (today's
    first-5-min volume ÷ avg first-5-min volume over the prior 14 days) — this is exactly the
    time-of-day-normalized RVOL concept in item 5 below, used here as a *universe filter*.
  - Opening range = 5 minutes (tested 5–25 min).
  - Entry: long if price breaks the OR high and the bar closed green; short if it breaks the OR
    low and the bar closed red.
  - Stop: entry ∓ 1.5×(14-day ATR).
  - Exit: flat at market close, every name, every day.
  - Sizing: quantity set so a stop-out costs 1% of portfolio value, capped at an equal-weight
    position across the day's basket.
  - Reported replication result: Sharpe ≈ 2.40 vs SPY buy-and-hold Sharpe ≈ 0.84 (2016 sample
    year in the replication); win rate on the individual trade is LOW (~15-20%) — the edge is a
    small-win-rate / large-payoff-ratio profile (big trend days pay for many small stopped-out
    days), consistent with a breakout-following (not mean-reverting) system.
- **What decays:** this is the single most load-bearing design choice for a day tier — the
  volume-based universe filter, not the breakout rule alone, is what the paper credits for
  edge (avoids trading illiquid/low-conviction names). If the day tier's universe stays fixed
  at ~10 static tickers rather than being re-selected daily by RVOL, this specific edge does NOT
  transfer as-is; the fixed-universe version should expect a lower, noisier win rate.
- **Data needs:** first-5-min cumulative volume (today) vs 14-day trailing average at the same
  clock time — this is a T1 Alpaca bar aggregation, no extra data source needed.
- Sources: [SSRN 4729284](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284), [QuantConnect replication — full rule detail](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/), [Concretum Group summary](https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/), [Wealth-Lab PDF mirror](https://www.wealth-lab.com/api/discussion/download/pdf/8007-ssrn-4729284-1-pdf)

### 1c. Crabel, *Day Trading with Short-Term Price Patterns and Opening Range Breakout* (1990, book — anecdotal/practitioner evidence, not academically peer-reviewed)
- **Rule:** ORB is most effective following an NR7 day (narrowest daily range of the prior 7 days)
  — a volatility-contraction precondition before the breakout trade. Originally documented on
  futures, not equities.
- **Evidence class:** ANECDOTAL/proprietary backtest, no public dataset or out-of-sample stats;
  widely cited by practitioners (e.g., Linda Bradford Raschke) but not independently replicated
  in the academic literature found in this search.
- **Regime-adaptive translation:** NR7 (or an ATR-percentile version, e.g., "today's ATR is in the
  bottom 20% of the trailing 20-day distribution") is a cheap, computable volatility-contraction
  filter that could gate which of the day tier's ~10 names get the ORB signal each morning.
- Sources: [Book listing/summary](https://www.goodreads.com/book/show/2306346.Day_Trading_With_Short_Term_Price_Patterns_and_Opening_Range_Breakout), [Oxford Strat NR pattern summary](https://oxfordstrat.com/trading-strategies/toby-crabel-narrow-range-1/)

---

## 2. Intraday Momentum: First Half-Hour Predicts Last Half-Hour — TESTED, ACADEMIC

**Gao, Han, Li & Zhou, "Market Intraday Momentum," *Journal of Financial Economics* 2018** (SSRN 2440866 / published version).
- **Rule:** the S&P 500 (SPY) return in the FIRST 30 minutes since the prior close predicts the
  return in the LAST 30 minutes of the same day, with the same sign (buy at ~3:30pm if the first
  half-hour was up, etc.).
- **Evidence:** SPY high-frequency data 1993–2013 (20 years). Predictive R² of first-half-hour →
  last-half-hour = 1.6%; combining the first half-hour with the 12th half-hour (the interval just
  before the close) raises R² to 2.6% — both exceed typical monthly-frequency predictive R²s in
  the return-predictability literature (implying real, if modest, economic significance).
  Effect is STRONGER on high-volatility days, high-volume days, recession days, and major
  macro-news-release days — i.e., it is itself regime-conditional by the authors' own finding.
  Also present, with varying strength, across 10 other heavily-traded domestic/international ETFs.
- **What decays:** this is a well-known, published anomaly (2014 working paper, 2018 JFE
  publication) — post-publication decay is a real risk (the classic "published anomalies shrink"
  finding, McLean & Pontiff 2016, though this specific factor's post-2018 decay was not directly
  tested in this search pass — **[hypothesis — unverified decay for THIS factor specifically]**).
- **Data needs:** SPY (or the day-tier symbol) opening 30-min bar-over-bar return + closing 30-min
  bar; trivially computable from T1 Alpaca 5-min or 30-min bars.
- **Regime-adaptive path:** condition strength/position size on realized volatility and volume —
  the paper's own finding is a ready-made regime gate (only trade the signal on above-median-vol
  or above-median-volume days).
- Sources: [SSRN delivery PDF](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2585766_code16976.pdf?abstractid=2552752&mirid=1), [ScienceDirect (JFE)](https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351), [Alpha Architect summary](https://alphaarchitect.com/attention-prop-traders-the-first-half-hour-of-trading-predicts-the-last-half-hour/), [paperswithbacktest.com](https://paperswithbacktest.com/strategies/market-intraday-momentum)

---

## 3. VWAP-Based Strategies — MIXED EVIDENCE, PRACTITIONER-HEAVY

- **Mean reversion to VWAP dominates over VWAP-breakout/trend-following in the evidence found**:
  one large-sample screening study (LuxAlgo) reports mean reversion (specifically short-side
  reversion from above VWAP) surviving statistical-significance testing across ~73,000 signal
  instances, with breakout/momentum/trend variants shifted into value-destructive territory.
  **Caveat: this is a vendor/blog-published screening study, not a peer-reviewed paper — treat as
  practitioner-grade evidence, not academic-grade.**
- **Regime-conditioning is not optional per this evidence:** an ADX-based momentum-exhaustion filter
  (a 2025 SSRN FX paper, Bhatti, "Momentum Exhaustion and Fair Value Reversion: An ADX-conditioned
  VWAP Strategy") finds reversion setups fail when ADX shows a live trend — the strategy must skip
  high-ADX days, not just always fade VWAP deviations.
- **Anchored VWAP (AVWAP):** used practitioner-side as a slower multi-day reversion reference
  (anchored to a swing high/low) rather than an intraday-only tool; lower win rate, longer holds —
  a swing-tier tool more than a day-tier one, so likely NOT the right anchor for the 15-min day
  layer (which must be flat by the close).
- **Recommended translation for the day tier:** VWAP reversion (fade extreme deviation from
  session VWAP) gated by an intraday ADX/trend-strength filter, using session VWAP anchored at
  9:30 ET (standard convention) — NOT multi-day anchored VWAP, which is a swing/position concept.
- Sources: [LuxAlgo VWAP regimes](https://www.luxalgo.com/library/indicator/vwap-mean-reversion-vs-trend-regimes/), [Bhatti SSRN 6454659](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6454659), [Crosstrade VWAP reversion](https://crosstrade.io/learn/trading-strategies/vwap-reversion)

---

## 4. Relative Volume (Time-of-Day Normalized) as a Filter — TESTED CONCEPT, NO STANDALONE ACADEMIC BACKTEST FOUND

- **Definition (the correct, non-naive form):** RVOL = today's CUMULATIVE volume up to time T ÷
  the average cumulative volume up to the SAME time-of-day T over the trailing N sessions (commonly
  N=14–20). This time-of-day normalization is essential — raw volume or full-day average volume
  is misleading intraday because volume itself has a strong U-shaped time-of-day seasonal pattern
  (see item 7).
- **Evidence class:** this is the exact universe-selection mechanic embedded in the Zarattini/
  Barbon/Aziz "Stocks in Play" ORB paper (item 1b) — i.e., it has been used as a *filter inside a
  peer-reviewed-adjacent (SSRN) backtest* rather than validated as a standalone factor in isolation.
  No separate academic paper isolating RVOL's own predictive power was found in this search; the
  practitioner heuristic of "RVOL ≥ 2x as a momentum-play minimum, 3-5x ideal" is anecdotal
  (TradingSim/DayTradingToolkit-class sources), not backed by a cited study.
- **Data needs:** cumulative intraday volume by symbol, T1 Alpaca bars; requires storing a rolling
  per-time-of-day historical volume profile per symbol (cheap: one extra time series per name).
- **Regime-adaptive path:** natural gate/confirmation input for the day tier's entry score — use
  it as a confluence add (as the ORB paper does, as a universe/signal-strength filter) rather than
  a standalone signal.
- Sources: [Intraday RVOL Overlay](https://www.tradingview.com/script/fCgl6lLO-Intraday-RVOL-Overlay-Time-of-Day-Relative-Volume/), [TradingSim RVOL guide](https://www.tradingsim.com/blog/relative-volume-rvol), [Örebro working paper on volume-driven time-of-day vol effects](https://www.oru.se/globalassets/oru-sv/institutioner/hh/workingpapers/workingpapers2025/wp-14-2025.pdf)

---

## 5. Gap Fill vs Gap-and-Go — MIXED, MOSTLY PRACTITIONER/BACKTEST-BLOG EVIDENCE

- **Reported same-day fill rates (practitioner backtest sources, not peer-reviewed):** common/
  standard gaps (0.5–0.99% size) fill same-day roughly 70–80% of the time (one cited figure: QQQ
  gap-downs fill same-day 77% of the time in that size band, gap-ups 72%). Exhaustion gaps fill
  75–85% within 5 sessions. **Breakaway gaps (the "gap-and-go" case) fill LESS than 30% of the time
  within a week** — i.e., gap-and-go and gap-fill are two different gap *types*, not two outcomes
  of one generic gap.
- **Key conditioning variable (repeated across sources, still practitioner-level, not a cited
  formal study):** gap on HIGH volume favors gap-and-go (continuation); gap on LOW/light volume
  favors fill (reversion). This directly composes with item 4 (RVOL) as the natural discriminator.
- **Evidence class: mostly ANECDOTAL/backtest-blog.** No peer-reviewed academic paper isolating
  gap-fill statistics for US equities was found in this pass; treat all specific percentages above
  as **[anecdotal — vendor-backtest sourced, not independently verified]**.
- **Regime-adaptive translation:** use gap size (as %ATR) × RVOL as a joint filter — small gap +
  low volume → reversion trade; large gap + high RVOL → continuation/ORB-style trade — rather than
  a single static "gaps fill X% of the time" rule.
- Sources: [QuantifiedStrategies gap types + backtest](https://www.quantifiedstrategies.com/gaps/), [TherobustTrader gap-fill stats](https://therobusttrader.com/do-gaps-always-get-filled/), [TradeZella gap-and-go rules](https://www.tradezella.com/blog/gap-and-go-strategy)

---

## 6. Intraday Mean Reversion in Large Caps — TESTED, ACADEMIC

- **Finding:** short-term (intraday, sub-daily) return reversal is robust and STRONGER, not weaker,
  in large-cap, high-liquidity names — the opposite of the classic (monthly/weekly) reversal
  literature where illiquid small caps show the strongest effect. One cited magnitude: the spread
  between most-liquid and least-liquid portfolios' sequential intraday-return reversal is ~39.9bp/day.
  Overnight returns do NOT show the same reversal — the effect is intraday-specific.
- **Mechanism (per the cited literature):** attributed to liquidity-provision economics (price
  concessions that let liquidity providers absorb order flow) rather than behavioral overreaction —
  meaning it should be thought of as a liquidity-rent signal, not a "sentiment overshoot" signal.
- **Data needs:** 1-minute or finer bar returns; note Alpaca's free/IEX plan (see item 10) only
  reflects ONE venue's prints, understating true intraday reversal magnitude versus full-SIP data.
- **Regime-adaptive path:** effect is stated to strengthen when VIX is elevated — a natural gate to
  size UP a mean-reversion sub-signal specifically in higher-vol regimes (opposite of how a
  breakout/momentum sub-signal should be regime-gated).
- Sources: [ScienceDirect — illiquidity/liquidity oversupply driver](https://www.sciencedirect.com/science/article/abs/pii/S0165188922000185), [NY Fed staff report — decomposing short-term reversal](https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr513.pdf), [Quantpedia — short-term reversal](https://quantpedia.com/strategies/short-term-reversal-in-stocks)

---

## 7. Market-Maker / Dealer Gamma (GEX) Effects on Intraday Volatility and Pinning — TESTED CONCEPT, PRACTITIONER-ORIGIN

- **Mechanism (well-established, cross-cited by academic gamma-fragility work and the original
  practitioner white paper):** when dealers are net LONG gamma (typical in calm/high-open-interest
  regimes), their delta-hedging is counter-trend — sell as price rises, buy as it falls — which
  DAMPENS realized intraday volatility and can "pin" price near heavily-traded strikes into
  expiration. When dealers are net SHORT gamma, hedging flows are WITH the trend and AMPLIFY moves.
- **Origin:** SqueezeMetrics' 2016 (revised 2017) GEX white paper popularized both the concept and
  the acronym; academic support for the gamma-imbalance → realized-vol relationship exists under
  the "gamma fragility" literature (not independently re-verified in this pass — treat the specific
  academic citation as **[inferred from search summaries, not read in full]**).
- **Already staged in this bot:** per CLAUDE.md, GEX is a staged-active (not shadow) signal already
  computed via `data/gex.py` and surfaced; this section is evidence justification for continuing/
  expanding that use, specifically as an intraday-volatility-regime confirmation for the day tier
  (e.g., damp expected ORB follow-through in a strongly long-gamma/pinning regime; favor breakout
  continuation signals in a short-gamma regime) — this is exactly the kind of cross-signal use the
  project's ANTI-SILO MANDATE calls for (GEX↔day-tier entry confirmation is one of the named
  first targets already).
- **Data needs:** requires OI + strikes from an option chain with real (not purely indicative)
  greeks/OI — see item 10 caveat: Alpaca's free/indicative options feed does NOT reliably carry
  OI/greeks (per this project's own RC-6 bug history — `data/gex.py` had to compute gamma locally
  via Black-Scholes because the indicative feed lacked it).
- Sources: [SqueezeMetrics white paper PDF](https://squeezemetrics.com/monitor/download/pdf/white_paper.pdf), [SpotGamma explainer](https://spotgamma.com/gamma-exposure-gex/), [SqueezeMetrics "Implied Order Book"](https://squeezemetrics.com/download/The_Implied_Order_Book.pdf)

---

## 8. Order-Flow Imbalance Proxies from Bars/Quotes — CONCEPT ONLY, NO ACADEMIC BACKTEST OF THE PROXY ITSELF

- **True order-flow imbalance (OFI)** requires tick-level trade/quote data (bid/ask-side volume);
  this is NOT what a free/IEX-tier Alpaca bar feed can reconstruct precisely.
- **OHLCV-only proxies that exist in practitioner literature (not peer-reviewed):** e.g. classifying
  bars as "bid-side" vs "ask-side" using close-vs-rolling-mid-EMA, normalized to a [-1,+1] imbalance
  score; or classic tick-rule-style up/down-volume accumulation (cumulative volume delta, CVD)
  approximated from OHLCV bars.
- **Academic anchor for the general concept (not the OHLCV-proxy specifically):** information-driven
  bars (volume/dollar/imbalance bars) from López de Prado's "Advances in Financial Machine
  Learning" — already a project-familiar reference (cited elsewhere in this repo's board protocol)
  — is the rigorous version of this idea, but it requires trade-level data, not just OHLCV bars.
- **Evidence class: mostly ANECDOTAL for the OHLCV-only proxy.** Treat any OFI-from-bars signal in
  the day tier as a LOW-conviction, confirmation-only input, not a primary signal, until/unless
  tick data becomes available.
- **Data needs:** Alpaca free/IEX plan gives 1 exchange's prints only — inadequate for a faithful
  OFI reconstruction; would need the paid SIP feed (Unlimited plan) at minimum, and ideally
  quote-level (NBBO) data, which is a materially different cost tier than what this project
  currently uses.
- Sources: [Retail Order Flow Imbalances paper (institutional context, not retail-bar proxy)](https://microstructure.exchange/papers/Dog_s_Tail_02212022.pdf), [QuantVPS OFI guide (practitioner)](https://www.quantvps.com/blog/order-flow-imbalance-signals)

---

## 9. Time-of-Day Seasonality (U-Shaped Volume/Volatility) — TESTED, ACADEMIC, FOUNDATIONAL

- **Finding:** intraday volatility AND volume both follow a U-shape across the trading day — high
  near the open, a lunchtime trough, rising again into the close. This is one of the most
  replicated findings in market microstructure (documented across NYSE/NASDAQ, and internationally
  at LSE/Euronext with additional local peaks tied to overlapping-market opens).
  Average returns also tend to be elevated in the first and last half-hours specifically (distinct
  from, but consistent with, the Gao/Han/Li/Zhou intraday-momentum finding in item 2).
- **Implication for a 15-min day-tier layer:** any static, time-invariant threshold (volatility
  filter, minimum-move filter, stop width) will be systematically miscalibrated across the session
  — the same ATR-multiple stop is "tight" at 10am and "loose" at 1pm on a raw-volatility basis.
  A time-of-day-normalized volatility/ATR baseline (analogous to the RVOL normalization in item 4)
  is the correct regime-adaptive fix, not a single all-day constant.
- **Data needs:** none beyond what's already available — this only requires accumulating a
  per-time-of-day historical volatility/volume profile from the bot's own existing T1 bar history.
- Sources: [Bauer/Houston (Heston, Korajczyk, Sadka) — Intra-day Patterns in the Cross-Section of Stock Returns, J. Finance](https://www.bauer.uh.edu/departments/finance/documents/Heston-Korajczyk-Sadka-jf-2010-01-07.pdf), [arXiv version](https://arxiv.org/pdf/1005.3535), [Örebro working paper — volume-driven time-of-day vol effects](https://www.oru.se/globalassets/oru-sv/institutioner/hh/workingpapers/workingpapers2025/wp-14-2025.pdf)

---

## 10. Transaction Costs at Small Size — GENERAL EVIDENCE, NOT DAY-TIER-SPECIFIC

- **General finding (practitioner/backtesting-methodology literature, not a single definitive
  academic paper):** slippage ranges roughly 0.1% in liquid conditions up to >1% when liquidity
  thins (wide spread, high volatility, larger relative order size). Momentum/breakout strategies
  (like ORB) are described as suffering MORE from slippage on average than mean-reversion
  strategies, because a breakout entry is, by construction, chasing price that is already moving
  away from the trader.
- **Implication for THIS bot specifically:** a ~10-name liquid universe at small ($2.5K-$25K paper)
  size should face slippage nearer the low end of that range (liquid large/mid caps, small order
  size relative to ADV) — but the ORB entry mechanic (breaking out on a green/red 5-min bar close)
  is exactly the higher-slippage case per this literature, reinforcing that realistic backtests
  MUST model a wide-spread/adverse-fill assumption at the breakout moment, not a mid-price fill.
- **Evidence class:** this section is general methodology consensus, not a specific tested
  number for this bot's universe/strategy — **[hypothesis — needs this project's own execution-
  quality measurement (already logged as a P0 whitespace item, `execution/execution_quality.py`,
  in CLAUDE.md's Future Roadmap Log) to get a real, cited number rather than a literature range]**.
- Sources: [ResearchGate — impact of transaction costs and slippage on algo trading](https://www.researchgate.net/publication/384458498_The_impact_of_transactions_costs_and_slippage_on_algorithmic_trading_performance), [Hyper-Quant realistic backtesting methodology](https://www.hyper-quant.tech/research/realistic-backtesting-methodology), [BSIC transaction cost modelling](https://bsic.it/backtesting-series-episode-5-transaction-cost-modelling/)

---

## 11. Alpaca Data Plan Implications (IEX vs SIP, Options) — FACTUAL, VERIFIED AGAINST ALPACA DOCS

- **Free/Basic plan (what this bot uses per T1 in CLAUDE.md):** real-time equities data limited to
  **IEX exchange only** — one venue among ~16 US equity venues; NOT the consolidated tape.
  Options data on the free plan is the **indicative feed only** (no reliable OI/greeks — already
  confirmed the hard way in this project's own history, RC-6, `data/gex.py`).
- **Paid Unlimited/Algo Trader Plus plan:** full CTA+UTP SIP feeds (100% of consolidated market
  volume) for equities; more complete options market coverage.
- **Implications for the day-tier signals above:**
  - ORB (item 1), intraday momentum (item 2), RVOL (item 4), and time-of-day seasonality (item 9)
    are all bar-aggregate signals that work reasonably on IEX-only bars, though true breakout
    levels/highs/lows computed from ONE venue's prints will occasionally diverge from the
    NBBO-true high/low — a source of small, systematic noise in exact stop/entry levels.
  - Intraday mean reversion (item 6) and order-flow imbalance (item 8) are the signals MOST
    degraded by IEX-only data — both are magnitude-sensitive to the full tape/NBBO, and OFI in
    particular is not reliably reconstructable without a fuller feed.
  - GEX (item 7) is bottlenecked by the OPTIONS side of the free plan (indicative feed lacking
    reliable OI/greeks), independent of the equities IEX/SIP question — this is a pre-confirmed,
    already-worked-around limitation in this codebase.
- Sources: [Alpaca — About Market Data API](https://docs.alpaca.markets/us/docs/about-market-data-api), [Alpaca — Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq), [Alpaca — data provider support page](https://alpaca.markets/support/data-provider-alpaca)

---

## Evidence-Ranked Summary (also see the ≤500-word chat summary)

**Tier 1 — Tested, academic or near-academic, directly reproducible with existing T1 data:**
1. Intraday momentum (first/last half-hour), Gao/Han/Li/Zhou — item 2
2. Intraday mean reversion in large caps, liquidity-driven — item 6
3. Time-of-day U-shaped volatility/volume seasonality — item 9
4. ORB "Stocks in Play" (Zarattini/Barbon/Aziz) — item 1b (strong but partly unverified full-PDF detail)
5. ORB base rule (Zarattini/Aziz) — item 1a (same caveat)

**Tier 2 — Real mechanism, evidence is a mix of academic-adjacent + heavy practitioner sourcing:**
6. GEX/dealer gamma pinning — item 7
7. VWAP reversion + ADX-conditioning — item 3
8. Relative volume (time-of-day normalized) as filter — item 4

**Tier 3 — Anecdotal / practitioner-blog only, use as confirmation not primary signal:**
9. Gap fill vs gap-and-go statistics — item 5
10. Order-flow imbalance from OHLCV bars — item 8
11. Crabel NR7 precondition — item 1c

**Cross-cutting, not itself a signal but gates everything above:**
12. Transaction costs at small size — item 10
13. Alpaca data-plan limits (IEX vs SIP, indicative options) — item 11
