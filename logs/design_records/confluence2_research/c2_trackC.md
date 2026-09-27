# Research Track C — Professional Discretionary/Systematic Swing Methods
### Confluence 2.0 evidence catalog (daily/weekly timeframe, 4h timing; break-above-meaningful-level trigger)

Each entry: rule definition → evidence quality/results → failure modes → data needs → dynamization path.

---

## 1. Relative Strength Rating (IBD RS Rating / RS line new highs / RS vs sector)

**Rule:** IBD RS Rating ranks a stock's trailing (weighted, recency-tilted) price performance 1–99 vs. all other stocks. RS Line = stock price ÷ benchmark index; a new high in the RS line (while price may not yet be at a new high) signals outperformance. Buy candidates are typically screened for RS ≥ 70–90 combined with a price base/breakout.

**Evidence:** IBD's own internal research (not an independent academic backtest) states the average RS Rating of the best-performing stocks just before their major run-ups, 1950–2008, was 87. Academically, George & Hwang (2004, *Journal of Finance*, "The 52-Week High and Momentum Investing," https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2004.00695.x) found that **nearness to the 52-week high**, not raw trailing return, explains most of momentum's profitability — a closely related but distinct construct from RS Rating (RS Rating = relative return rank; 52-wk-high proximity = absolute anchor). Jegadeesh & Titman (1993, *Journal of Finance*, "Returns to Buying Winners and Selling Losers," https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1993.tb04702.x) is the foundational academic result: 3–12 month winner-minus-loser portfolios earned ~1% per month in the first year, decaying in years 2–3 (this is the peer-reviewed backbone under which IBD's RS Rating sits, since RS Rating is a proprietary implementation of the same trailing-return concept).
**Distinguish anecdote from evidence:** the "87 average RS Rating" claim is IBD marketing research, not peer-reviewed and not disclosed methodology — treat as anecdote-grade. The George/Hwang and Jegadeesh/Titman results ARE peer-reviewed, out-of-sample-replicated evidence for the underlying momentum effect.
**What fails:** momentum famously crashes in market reversals after high-volatility bear markets (see Item on Momentum Crashes below); RS Rating alone is not risk-adjusted, over-weights already-crowded winners near market tops; whole-market regime shifts invalidate cross-sectional rankings (a rank of 90 in a down market ≠ a rank of 90 in an up market).
**Data needs:** daily closes for full universe + benchmark, rolling 12-month (or 3/6/9/12 blended) return calc, cross-sectional percentile rank daily.
**Dynamization:** make the momentum lookback window and RS threshold regime-conditional (shorter lookback / lower threshold in high-dispersion bull regimes, higher threshold and shorter holding period in high-VIX regimes per Momentum Crash literature — see Barroso & Santa-Clara 2015 vol-scaling, referenced under Item 14).

**Sources:**
- https://finance.yahoo.com/news/relative-strength-rating-pinpoints-stocks-214500318.html
- https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2004.00695.x (George & Hwang 2004)
- https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1993.tb04702.x (Jegadeesh & Titman 1993)
- https://alphaarchitect.com/the-secret-to-momentum-is-the-52-week-high/

---

## 2. Mark Minervini — Trend Template + Volatility Contraction Pattern (VCP)

**Rule (Trend Template, 8 criteria):** price > 150-day and 200-day MA; 150-day MA > 200-day MA; 200-day MA trending up ≥1 month; 50-day MA > 150-day MA > 200-day MA; price > 50-day MA; price ≥25% above 52-week low; price within 25% (ideally closer) of 52-week high; RS Rating ≥ 70 (often ≥90 for the best setups).
**VCP entry:** a base with successively tighter pullbacks (e.g., ~20% → ~10% → ~5%) over 2–4 (up to 5–6) contractions, with volume drying up on each contraction; entry trigger = breakout above the final/tightest contraction's high (the "pivot") on volume expansion (often ≥40–50% above average).
**Stop/risk:** stop below the low of the final contraction, max 7–8% loss; 1.25–2.5% of equity risked per trade; pyramid into confirmed winners.
**Evidence:** Primarily practitioner track record (Minervini won the US Investing Championship with an audited +334% in 2021) — this is a single-operator audited return, not a systematic backtest, and is anecdote-grade for generalizability though the audit itself is real. No independent peer-reviewed backtest of VCP specifically was found; it is a discretionary pattern (base tightness, volume dry-up) that resists mechanical, non-discretionary backtesting because "contraction" and "pivot" identification require judgment. The Trend Template's components (price/MA stacking, 52-week-high proximity, RS rank) individually rest on published trend-following and 52-week-high momentum literature (George & Hwang 2004 above; moving-average trend-following literature generally).
**What fails:** discretionary pattern-recognition is not testable/replicable at scale without a rules-engine for "contraction tightness" and "volume dry-up," so most VCP screeners are approximations; false breakouts from bases are common in choppy/low-trend regimes; overfitting risk is high because practitioners cherry-pick clean examples.
**Data needs:** daily OHLCV, 50/150/200-day MAs, 52-week high/low, rolling volume averages, base/pullback detection logic (swing high/low identification).
**Dynamization:** define contraction tightness and pivot breakout thresholds as a function of the stock's own historical volatility (ATR-normalized) rather than fixed %; require the "quality" of base tightening to scale with the current market regime's baseline volatility (tighter contraction required to qualify as VCP during high-VIX regimes).

**Sources:**
- https://trendspider.com/learning-center/volatility-contraction-pattern-vcp/
- https://traderlion.com/technical-analysis/volatility-contraction-pattern/
- https://www.financialtechwiz.com/post/mark-minervini-trading-strategy/

---

## 3. William O'Neil CAN SLIM

**Rule:** 7-factor screen — Current quarterly earnings growth (≥25%), Annual earnings growth (≥25%, 3yr), New (product/management/price high), Supply and demand (low float or high volume on up days), Leader (RS Rating ≥80, top stock in its industry group), Institutional sponsorship (increasing), Market direction (confirm with index trend/follow-through day). Entry at breakout from a proper base ("cup with handle," flat base, etc.) on volume; sell rules include the 7–8% stop and profit-taking rules (e.g., sell into strength at +20–25%, or use trailing technical exits).
**Evidence — mixed, both academic-tested and real-world-refuted:** Lutey et al. (2013, published in *Journal of Accounting and Finance*, "OPBM II: An Interpretation of the CAN SLIM Investment Strategy," http://www.na-businesspress.com/JAF/LuteyM_LWeb14_5_.pdf) backtested a modified CAN SLIM system across 3 independent periods and found backtested average returns of 13.9%–20.2%, beating the S&P 500 benchmark. A separate 2013 academic study (cited via liberatedstocktrader.com, https://www.liberatedstocktrader.com/what-is-canslim/) found CAN SLIM beat the NASDAQ 100 by 0.94%/month, 1999–2013, with better risk-adjusted returns. **However**, real-world implementations diverge sharply: all CAN SLIM/IBD-affiliated mutual funds have underperformed or failed in practice (per liberatedstocktrader.com's review), a strong signal that backtest-to-live implementation gap (execution slippage, discretionary base/quality judgment, survivorship bias in the backtested universe) destroys much of the paper edge.
**What fails:** the "N" (new) and "L" (leader within industry) criteria are qualitative/discretionary and not mechanically backtestable without heavy assumptions; live mutual fund results show the edge does not survive at scale/with real execution; academic backtests use survivorship-biased universes (current index members applied historically) — the same critique leveled at Weinstein Stage 2 tests (Item 4) applies here.
**Data needs:** EPS growth (quarterly + TTM), institutional ownership data (13F-derived), RS Rating, base-pattern detection, index follow-through-day state.
**Dynamization:** replace fixed 25% EPS growth threshold with a percentile rank vs. the current universe/regime; gate new entries on the O'Neil Follow-Through Day state machine (Item 8) rather than a static "market direction" checkbox.

**Sources:**
- http://www.na-businesspress.com/JAF/LuteyM_LWeb14_5_.pdf
- https://www.liberatedstocktrader.com/what-is-canslim/
- https://www.researchgate.net/publication/326548848_OUTPERFORMING_THE_BROAD_MARKET_AN_APPLICATION_OF_CAN_SLIM_STRATEGY

---

## 4. Stan Weinstein Stage Analysis (Stage 2 breakout, 30-week MA)

**Rule:** 4 stages — Stage 1 (basing, flat 30-wk MA), Stage 2 (advancing, price and rising 30-wk MA, entry on breakout above Stage-1 resistance on volume ≥2× average), Stage 3 (topping), Stage 4 (declining — exit/short). Entry = Stage 2 breakout above resistance with the 30-week MA turning up and volume confirmation. Hold while the 30-week MA continues rising; exit at the Stage 2→3 transition (MA flattens/price fails to make new highs) or Stage 3→4 confirmation.
**Evidence — recently, rigorously academically tested and found weaker than claimed:** Damian Roskill, "Does Weinstein Stage Analysis Beat a Moving Average?" (SSRN, 2026, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7429238) is the first identified peer-style rigorous test. Its key finding: **naive practitioner backtests of Stage 2 (using today's index constituents applied backward in time) inflate Stage-2 returns by 6.3%/year** due to survivorship bias, and once corrected, **Weinstein's Stage 2 outperformance is empirically indistinguishable from a simple moving-average trend filter** — i.e., the distinctive Weinstein taxonomy (basing/topping pattern classification beyond the MA slope) adds no measurable incremental forecasting power. This is a significant, credible debunking of the "stage analysis has special edge beyond trend-following" claim.
**What fails:** whipsaws at Stage 1↔2 boundary in choppy/rangebound tape; the 30-week MA is a lagging signal so entries occur well after the initial move; survivorship bias inflates all naive practitioner-reported backtests (this is the single most important methodological finding across this entire catalog).
**Data needs:** weekly OHLCV, 30-week SMA, point-in-time (not current) index membership if doing any cross-sectional universe test.
**Dynamization:** since the SSRN result shows the edge = trend-following, the dynamic version should skip the discretionary stage-labeling entirely and instead use an adaptive-slope MA trend filter (e.g., MA slope z-scored against its own historical distribution) — simpler and evidence-backed rather than the discretionary overlay.

**Sources:**
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7429238 (Roskill, "Does Weinstein Stage Analysis Beat a Moving Average?")
- https://traderlion.com/trading-strategies/stage-analysis/
- https://deepvue.com/indicators/stan-weinstein-stage-analysis-when-to-buy/

---

## 5. Darvas Box

**Rule:** draw a "box" from a stock's recent consolidation high/low; buy on breakout above the box top on volume; stop just below the box bottom (structural stop); trail up new boxes as price advances, only taking longs in rising boxes.
**Evidence:** Purely anecdotal/historical — Nicolas Darvas's own account (turning $10K into $2M in the late 1950s) is a single, non-audited (by modern standards), non-replicable personal track record from a pre-modern-data era. No independent quantitative backtest was found in this search. It is the conceptual ancestor of both Darvas-style box breakout systems and, later, Donchian-channel-style breakout systems (which DO have quant literature behind them — trend-following CTAs' documented use of channel breakouts), but the specific "Darvas box" implementation itself remains untested in modern peer-reviewed or quant-blog form.
**What fails:** entirely discretionary box-drawing (no canonical rule for box width/duration) makes it non-mechanizable without arbitrary parameterization; breakout-and-hold fails in mean-reverting/choppy regimes exactly like every other breakout method here.
**Data needs:** daily OHLCV, consolidation/box detection (swing high/low over N days).
**Dynamization:** replace manual box-drawing with a rules-based consolidation detector (e.g., N-day range where high-low range contracts below a rolling ATR percentile) — converges with VCP (Item 2) and Crabel NR7 (Item 7) as variations on the same "detect low-volatility base, buy the breakout" family.

**Sources:**
- https://corporatefinanceinstitute.com/resources/equities/darvas-box-theory/
- https://trendspider.com/learning-center/darvas-box-theory-trading-strategy/

---

## 6. Qullamaggie-style Episodic Pivots & High Tight Flags

**Rule (Episodic Pivot):** gap up ~≥10% on earnings/major news with volume ≥1 full average day's volume in the first 15–30 min; look for real fundamental support (high double/triple-digit EPS/revenue growth) and a quiet prior base (3–6 months low volatility). Entry on the gap day or the first pullback/reclaim.
**Rule (High Tight Flag):** stock up ~100%+ (often much more) in a short window (weeks), followed by a tight, shallow (typically <25%) consolidation; entry on breakout from the flag.
**Rule (Breakout, the base variant):** stock up 30–100% over 1–3 months, pulls back with higher lows, tightens along the rising 10/20-day MA for 2 weeks–2 months, entry on expansion out of the range — intraday entries are often timed off the opening range (tying directly into Crabel/ORB, Item 7).
**Evidence:** Entirely a living trader's (Kristjan Kullamägi's) documented public track record and educational content; there is no independent academic or peer-reviewed backtest — third-party retail tools ("Qullamaggie Backtester" at sharepredictions.com) exist to mechanically test the setups against historical data but these are unaudited retail tools, not published research. Evidence quality: anecdote/practitioner-grade, though the underlying mechanism (post-earnings gap drift + momentum continuation) is supported by the peer-reviewed PEAD literature (Item 12) and 52-week-high momentum literature (Item 1).
**What fails:** episodic pivots on illiquid/small-cap names are prone to gap-fade and reversal; high tight flags are rare, high-variance setups (survivorship bias in "look at these winners" writeups is severe — the failed flags/pivots are rarely publicized); requires real-time intraday data most retail systems lack.
**Data needs:** intraday (1-min or better) volume/price for gap detection, EPS/revenue growth data, historical volatility for "quiet base" detection, ADR (average daily range) for position sizing (Kullamägi's own stated method).
**Dynamization:** define "gap %" and "tight flag %" thresholds as a function of the stock's own historical ADR/ATR rather than fixed percentages so the setup definitions self-adjust across different volatility regimes and market caps.

**Sources:**
- https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/
- https://www.chartmill.com/documentation/stock-screener/technical-analysis-trading-strategies/494-Mastering-the-Qullamaggie-Episodic-Pivot-Setup-A-Flexible-Stock-Screening-Approach
- https://medium.com/@refikberkol/deep-dive-into-kristjan-kullam%C3%A4gis-swing-trading-strategies-an-in-depth-guide-7872e7f1a0cb

---

## 7. Linda Raschke / Larry Connors short-term setups (RSI(2), pullbacks-in-trends) & Toby Crabel opening-range/NR7

**Rule (Connors RSI(2)):** in an uptrend (price > 200-day MA), buy when RSI(2) drops to an oversold extreme (e.g., <5–10), sell/exit when RSI(2) crosses back above a threshold (e.g., >70) or at a fixed short holding period (2–5 days). Origin: Connors & Alvarez, *Short Term Trading Strategies That Work* (2008).
**Evidence:** This is the single best-documented, most rigorously and repeatedly backtested setup in the catalog. Connors/Alvarez's own mid-1990s–2010 research reported win rates of 70–85%+ on broad indices; independent third-party replications (quantifiedstrategies.com, https://www.quantifiedstrategies.com/rsi-2-strategy/ and https://www.quantifiedstrategies.com/connors-rsi/) report ~75% win rates on Connors RSI variants; a Substack backtest (https://backtest.substack.com/p/the-2-period-rsi-a-simple-system) found the edge persists ("still earns its keep") in more recent out-of-sample data, though with degraded magnitude vs. the original claims — one independent test found a lower ~64.33% win rate (2.33% avg winner / 3.02% avg loser), and crypto-market replications found 62–68% win rates with 1.4–1.8 profit factors on 2–4 day holds. This is a genuinely mean-reversion (NOT breakout) setup — included per the research brief but structurally opposite to the CEO's stated breakout-and-hold preference.
**Rule (Toby Crabel NR7/Opening Range Breakout):** NR7 = the single day with the narrowest high-low range of the past 7 days (NR4 = narrowest of 4); Crabel's pre-1990 futures research (*Day Trading with Short-Term Price Patterns & Opening Range Breakout*, 1990, now out of print) found that volatility contraction (NR7) tends to precede volatility expansion, and that opening-range breakout trades taken specifically after an NR7 setup have improved reliability. Reported win rates in tested setups: 60–76%. This is the direct historical ancestor of both VCP (Item 2) and Darvas boxes (Item 5) — "low volatility precedes high volatility, trade the expansion."
**Modern, rigorous, peer-style validation of the ORB family:** Zarattini, Barbon & Aziz, "A Profitable Day Trading Strategy For The U.S. Equity Market" (SSRN #4729284, 2024, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284) tested opening-range breakout on >7,000 US stocks 2016–2023, restricted to "stocks in play" (high relative volume/news), and found a top-20-names portfolio returned >1,600% cumulative with Sharpe 2.81 and 36% annualized alpha vs. 198% cumulative for buy-and-hold SPY over the same period — a strong, credible, modern, published result for the ORB/NR7 lineage specifically (though this is a day-trading, not swing, implementation — entries and exits same-day). Caution: a third-party replication (https://www.mql5.com/en/blogs/post/776235) reproduced the gross returns on 5 indices but found net-of-realistic-costs performance collapsed to roughly zero — a critical "what fails in practice" data point: the edge is real gross but thin/cost-sensitive.
**What fails:** RSI(2) mean reversion degrades in strong trending/breakout regimes (the opposite regime from where breakout methods thrive) and can produce large single losses if the "buy the dip in an uptrend" filter fails during a trend reversal; ORB/NR7 edges are heavily cost- and slippage-sensitive per the net-zero replication above; both require disciplined, short, fixed holding periods that swing-holding tendencies (the CEO's preference) will violate if discretion is added.
**Data needs:** daily RSI(2) + 200-day MA for Connors; N-day high-low range history + opening 5-15min range for Crabel/ORB; realistic cost/slippage model is essential given the net-zero replication finding.
**Dynamization:** RSI(2) threshold and holding period can be regime-conditioned on ADX/trend strength; NR7 breakout target/stop distance can be ATR-scaled rather than fixed.

**Sources:**
- https://www.quantifiedstrategies.com/rsi-2-strategy/
- https://www.quantifiedstrategies.com/connors-rsi/
- https://backtest.substack.com/p/the-2-period-rsi-a-simple-system
- https://www.quantifiedstrategies.com/nr7-trading-strategy-toby-crabel/
- https://time-price-research-astrofin.blogspot.com/2023/09/nr4-nr7-narrow-range-4-7-id-inside-days.html
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284 (Zarattini/Barbon/Aziz, "A Profitable Day Trading Strategy For The U.S. Equity Market")
- https://www.semanticscholar.org/paper/Can-Day-Trading-Really-Be-Profitable-Evidence-of-in-Zarattini-Aziz/4d55f526cc56f08662cb8976796cd3b719ef6d2b
- https://www.mql5.com/en/blogs/post/776235 (net-of-cost replication — "gross reproduced, net zero")

---

## 8. Anchored VWAP (Brian Shannon) & Volume Profile / Value Area / Pocket Pivots

**Rule (Anchored VWAP):** plot VWAP anchored to a significant event (earnings date, swing high/low, gap, IPO date); price above a rising AVWAP = bullish control, below = bearish; used as dynamic support/resistance for entries, stops, and profit targets. Originated/popularized by Brian Shannon, CMT (*Maximum Trading Gains With Anchored VWAP*).
**Rule (Pocket Pivot — Gil Morales & Chris Kacher, *Trade Like an O'Neil Disciple*, 2010):** an early-entry, inside-the-base volume signal — bar closes up, volume exceeds the single largest down-day volume of the prior 10 sessions, price is above (and not excessively extended from, typically <5–10% per Morales/Kacher) the key moving average (10-day or 50-day). Entry inside a proper base, before the standard new-high breakout — designed to get in ahead of the crowd.
**Evidence:** Both are practitioner-originated, non-peer-reviewed concepts; no independent academic backtest was found for either Anchored VWAP or Pocket Pivots specifically in this search. VWAP itself (unanchored, institutional execution benchmark) has extensive market-microstructure research behind it as an execution quality benchmark, but "anchored VWAP as a swing entry/exit signal" is Shannon's own framework, evidence quality = practitioner/anecdotal. Pocket pivots are a mechanically well-defined rule (unlike VCP or Darvas) and are therefore more readily backtestable, but no published third-party quant test was located.
**What fails:** AVWAP anchor selection is subjective (which earnings date? which swing point?) — different anchors give materially different levels, undermining reproducibility; pocket pivots can fire on low-quality, thin-volume names where the "prior 10-day down volume" bar is trivially low, generating false positives.
**Data needs:** intraday or daily volume + price for VWAP calc from a chosen anchor; daily volume history (10+ days) and MA for pocket pivot detection.
**Dynamization:** since pocket pivot IS mechanically well-defined, it is the most straightforward of this group to backtest rigorously and regime-condition (e.g., require the volume threshold to be a percentile of the stock's own volume distribution rather than a fixed "highest down day" rule, to normalize across liquidity regimes).

**Sources:**
- https://alphatrends.net/anchored-vwap/
- https://www.tradingsim.com/blog/anchored-vwap-strategies
- https://www.luxalgo.com/library/concept/pocket-pivot/
- https://www.tradingsim.com/blog/vdu-and-pocket-pivots

---

## 9. Market Breadth & Sector Rotation Filters (% above 50-day MA, new highs–new lows, advance-decline line)

**Rule:** use % of index members above their 50-day/200-day MA, the cumulative advance-decline line, and new-52-week-highs minus new-52-week-lows as market-health confirmation filters — bullish regime confirmed when these are rising/elevated (e.g., >50% above 50-day MA is considered healthy; ~10–12% of S&P 500 making new highs is typical of a healthy bull phase); used to gate entries (only take breakouts when breadth confirms) or to detect early divergence/weakness (price makes new highs while breadth deteriorates = warning).
**Evidence:** widely used by professional technical analysts (StockCharts, Schwab, AAII) as confirmation/divergence tools; the "combine multiple breadth signals for a stronger signal than any one alone" heuristic is standard practice guidance, not itself a formally backtested quantitative rule in the sources found. Sector rotation specifically: a Fidelity study cited outperformance of sector-rotation-by-relative-strength vs. S&P 500 by ~3.6%/year over 15 years (practitioner-grade citation, methodology not disclosed in the search results); a separate relative-strength-on-Fama-French-sector-data test back to the 1920s found the RS sector rotation approach beat buy-and-hold in ~70% of years across multiple lookback windows (1/3/6/9/12-month) — a much stronger, longer-sample, more credible piece of evidence. Countervailing: an academic paper, "The Myth of Sector Rotation" (Molchanov & Stangl, AUT, https://acfr.aut.ac.nz/__data/assets/pdf_file/0005/294287/The-Myth-of-Sector-Rotation-non-blind.pdf), found little to no evidence that timing sectors to the business cycle produces systematic outperformance — i.e., macro-driven ("this sector leads in this part of the cycle") sector rotation is debunked, whereas pure relative-strength-driven ("buy whichever sectors are outperforming now, regardless of macro narrative") sector rotation has the stronger long-sample evidence above. This is an important distinction: RS-based rotation ≠ business-cycle-based rotation, and only the former holds up.
**What fails:** breadth divergence signals can persist for a long time before "resolving" (early/false warning risk); business-cycle-based sector rotation specifically is empirically weak per the AUT paper; breadth measures using current index constituents applied historically suffer the same survivorship-bias critique as the Weinstein SSRN paper found (Item 4) — must use point-in-time membership.
**Data needs:** full-universe daily closes + 50/200-day MAs, 52-week high/low flags, point-in-time index membership, sector/industry classification (GICS or similar).
**Dynamization:** already naturally regime-descriptive; the dynamic step is using breadth level (not just its sign) as a continuous input to MIN_SCORE/size floor — analogous to how MRI already works in this bot — rather than a binary breadth confirm/deny gate.

**Sources:**
- https://articles.stockcharts.com/article/three-breadth-signals-that-help-confirm-market-trends/
- https://www.schwab.com/learn/story/breadth-check-strength-and-weakness-trend-tracker
- https://www.aaii.com/journal/article/535756-gauging-the-strength-of-market-trends-with-the-ad-line
- https://acfr.aut.ac.nz/__data/assets/pdf_file/0005/294287/The-Myth-of-Sector-Rotation-non-blind.pdf
- https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/fabers-sector-rotation-trading-strategy

---

## 10. Follow-Through Days (William O'Neil market-bottom confirmation)

**Rule:** after a market decline, a "rally attempt" begins on the first up-day off a low; a Follow-Through Day (FTD) = a major index closing up ≥1.25–1.7% on volume higher than the prior session, occurring on day 4 through ~day 12 (sometimes stated day 4–7) of the rally attempt. O'Neil's rule: no major bull market since the early 1900s began without a preceding FTD (his claim). Used as a gate to re-enter aggressive long exposure after a correction/bear phase.
**Evidence:** O'Neil's "no bull market began without an FTD" claim is a historical pattern-match claim by IBD, not an independently audited academic study (anecdote/practitioner-grade as stated, though it is a real, checkable historical pattern). Critically, the base rate the other direction is explicitly weaker than commonly assumed: **about half** of historical FTDs actually lead to a sustained advance (2009, 2020 are cited textbook wins) while the other half fail (2008 is the cited canonical failure) — this 50/50 hit rate is a materially important, honestly-reported limitation (sourced from a Forbes March 2025 "Follow-Through Day Study Update" piece, https://www.forbes.com/sites/randywatts/2025/03/07/follow-through-day-study-update/, and corroborated by quantifiedstrategies.com's dedicated backtest article). So: FTD reliably flags "institutions are trying," not "the rally will succeed" — it's a necessary-but-not-sufficient, high-false-positive-rate signal.
**What fails:** ~50% false positive rate on standalone use; the specific %/volume thresholds are somewhat arbitrary and have been revised by IBD over the decades; doesn't account for which sectors/stocks are leading the FTD (a low-quality FTD led by low-quality stocks is a known IBD refinement not fully covered here).
**Data needs:** daily index (S&P 500/Nasdaq) OHLCV, rally-attempt day counter state machine, volume vs. prior-session comparison.
**Dynamization:** rather than a binary FTD gate, weight it continuously by (a) the underlying %/volume magnitude beyond the minimum threshold and (b) the RS-Rating quality of leading stocks on that day, and feed as a continuous multiplier into MRI/regime rather than a hard reopen/no-reopen switch.

**Sources:**
- https://articles.stockcharts.com/article/a-proven-technique-that-identifies-every-market-bottom/
- https://www.forbes.com/sites/randywatts/2025/03/07/follow-through-day-study-update/
- https://www.quantifiedstrategies.com/follow-through-day/
- https://traderlion.com/trading-strategies/follow-through-day/

---

## 11. Gap-and-Go / Earnings Gaps (Episodic Pivots) & Post-Earnings-Announcement Drift (PEAD)

**Rule (discretionary/practitioner):** buy large earnings/news gaps (see Item 6, Qullamaggie episodic pivots) with fundamental confirmation and volume; hold for continuation.
**Rule (academic, PEAD):** following an earnings surprise (SUE — standardized unexpected earnings), returns continue to "drift" in the direction of the surprise for weeks to months rather than fully adjusting immediately, violating market efficiency.
**Evidence:** PEAD is described in the literature itself as "one of the most solidly documented asset pricing anomalies" in finance (ScienceDirect review, https://www.sciencedirect.com/science/article/pii/S2214635020303750) — decades of replication across markets and periods. Recent SSRN work extends it: a text-based PEAD measure ("PEAD.txt," https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3778798) finds a text-based drift measure remains large even in recent years when the "classic" numeric-surprise-based PEAD has shrunk close to zero — i.e., the raw numeric-surprise version of the anomaly has been substantially arbitraged away, but a richer earnings-call-content-based version still shows a live edge. Another 2024 SSRN paper (https://papers.ssrn.com/sol3/Delivery.cfm/9412a06f-c6aa-4df1-bf29-370fe1bd0399-MECA.pdf?abstractid=4589824) finds investor-attention interacts with the surprise size to affect drift magnitude, and that a strategy exploiting this can generate returns in excess of the market.
**What fails:** the classic (simplest, numeric-only) form of PEAD has decayed toward zero as it's been arbitraged by systematic funds — this is a directly relevant, credible "what fails in practice" data point for any earnings-gap-based swing system relying on the simple surprise number alone; gap-fade risk on illiquid/small names; earnings-gap continuation (episodic pivot style) requires genuine fundamental support (per Kullamägi's stated criteria) or it's prone to reversal.
**Data needs:** EPS/revenue actual vs. consensus (surprise %), ideally earnings-call transcript text/NLP sentiment (to access the still-live "PEAD.txt"-style edge rather than the decayed classic version), historical realized volatility for gap-size normalization.
**Dynamization:** move beyond the numeric-surprise-only version (documented as decayed) toward incorporating qualitative/textual surprise signals, and size/hold-period-scale the position by the magnitude of surprise and investor-attention proxies (e.g., abnormal pre-earnings volume/search interest) per the cited attention research.

**Sources:**
- https://www.sciencedirect.com/science/article/pii/S2214635020303750 (PEAD review)
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3778798 (PEAD.txt)
- https://papers.ssrn.com/sol3/Delivery.cfm/9412a06f-c6aa-4df1-bf29-370fe1bd0399-MECA.pdf?abstractid=4589824
- https://qullamaggie.com/how-to-master-a-setup-episodic-pivots/

---

## 12. Professional Risk Practice for Swing Stops (structure-based, ATR, time stops)

**Rule:** three dominant professional stop-placement paradigms — (a) **structure-based**: stop just beyond a technical invalidation level (prior swing low, box/base bottom, VCP contraction low), often with a small ATR buffer (0.3–0.5× ATR) added past the level to avoid noise wick-outs; (b) **ATR-based**: stop = entry ± (1.5–2.5× ATR), scaling stop distance to the instrument's own realized volatility rather than a fixed %; (c) **time stop**: exit if the trade hasn't worked within N bars/days regardless of price, common in short-term mean-reversion systems (Connors RSI(2), Item 7) and less common in trend/swing systems.
**Evidence:** these are risk-management heuristics with broad professional consensus and industry-guide-level support (Technical Analysis of Stocks & Commodities is cited as having empirically compared multipliers), but the specific "35% reduction in premature stop-outs" and "1.5-2.0x ATR is optimal for 3-15 day swings" figures found in this search are from trading-education blogs (quantstock.org, alphaexcapital.com), not peer-reviewed studies — treat these specific numbers as **[hypothesis — practitioner-grade, unverified against a primary academic source]**. The general principle (volatility-scaled stops outperform fixed-% stops in adapting to regime) is directionally consistent with, and reinforced by, this bot's own existing VIX-adjusted stop-widening implementation (Architecture Invariant #12) and with published vol-scaling/momentum-crash literature (Item 1's George/Hwang and the broader momentum-crash literature — e.g., Barroso & Santa-Clara's vol-managed momentum, referenced generally in momentum-crash discussions), which IS peer-reviewed.
**What fails:** fixed % stops fail to adapt across volatility regimes (widely agreed); ATR stops still fail during volatility regime shifts (an ATR computed over the last 14–20 days lags a sudden vol spike, same class of problem as the bar-staleness issue this bot already guards against); structure stops fail when the "structure" is itself subjective/discretionary (same critique as VCP/Darvas).
**Data needs:** ATR calculation (already implemented per Architecture Invariant #12), swing high/low detection for structure stops.
**Dynamization:** this bot already has the most sophisticated version in this catalog (continuous VIX-curve ATR-multiplier scaling, Architecture Invariant #12) — the incremental improvement suggested by this research is combining structure-based invalidation (the swing low under a VCP/base) WITH the existing ATR volatility buffer, rather than either pure fixed-ATR or pure fixed-structure.

**Sources:**
- https://quantstock.org/blog/atr-stop-loss-strategy-guide
- https://traderssecondbrain.com/guides/stop-loss-placement-methods
- https://www.tradealgo.com/trading-guides/stocks/swing-trading-risk-management-position-sizing-stop-losses-and-portfolio-rules

---

## 13. 52-Week High Momentum, Momentum Crashes, and Vol-Regime Conditioning (cross-cutting academic backbone)

**Rule/finding:** George & Hwang (2004, Item 1) show 52-week-high proximity subsumes and improves on trailing-return momentum as a predictor. A companion literature strand ("Momentum Crashes and the 52-Week High," Marquette repository, https://epublications.marquette.edu/cgi/viewcontent.cgi?article=1168&context=fin_fac) documents that momentum strategies (including 52-week-high-based ones) suffer occasional severe crashes, concentrated in market rebounds after high-volatility down markets (e.g., 2009) — i.e., the exact regime where reflexive "buy the breakout" systems are most dangerous.
**Evidence quality:** strong — both are peer-reviewed, in top finance journals/academic repositories, widely cited and replicated internationally (a ScienceDirect international-markets replication of 52-week-high momentum was also found: https://www.sciencedirect.com/science/article/abs/pii/S0261560610001099).
**What fails:** momentum/breakout systems crash hardest exactly in V-shaped-recovery regimes following high-vol bear markets — this is the single most important "what fails in practice" finding for a break-above-a-level, hold, breakout-style system, and argues strongly for regime-based position-size dampening (not blocking) rather than static sizing during/immediately after high-VIX regimes.
**Dynamization:** this is directly actionable for Confluence 2.0 — condition breakout-and-hold position size (not the entry signal itself, to preserve Profitable > Perfect) on realized/implied vol regime, especially in the weeks following a VIX spike, per the momentum-crash literature. This is a natural extension of the bot's existing VIX-curve stop-widening (Invariant #12) into the sizing dimension specifically for the swing/breakout book.

**Sources:**
- https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2004.00695.x
- https://epublications.marquette.edu/cgi/viewcontent.cgi?article=1168&context=fin_fac
- https://www.sciencedirect.com/science/article/abs/pii/S0261560610001099

---

# ≤600-word Summary (ranked by evidence strength)

**Tier 1 — Peer-reviewed, replicated, strongest evidence:**
1. **Jegadeesh & Titman (1993) cross-sectional momentum** and **George & Hwang (2004) 52-week-high momentum** — the academic bedrock under RS Rating, Trend Template, and every breakout-and-hold method here. Real, replicated, international. Caveat: **momentum crashes** hard in post-high-vol rebounds (Marquette repository) — the single most important risk finding for this whole research track.
2. **Post-Earnings-Announcement Drift (PEAD)** — "one of the most solidly documented anomalies" per its own literature review, decades of replication. Important nuance: the *simple numeric-surprise* version has decayed toward zero as it's been arbitraged; a *text/attention-based* version still shows live edge (2024–2025 SSRN papers). Directly relevant to gap-and-go/episodic-pivot entries.
3. **Zarattini/Barbon/Aziz opening-range-breakout paper (SSRN 2024)** — a genuinely modern, large-sample (7,000+ stocks), rigorously reported result (Sharpe 2.81 gross) validating the Crabel/NR7/ORB lineage. Critical caveat: an independent replication found gross returns reproduce but **net-of-realistic-cost performance collapses to ~zero** — this is the most important single "backtest looks great, real-world execution erases it" data point in the whole catalog.
4. **Sector rotation via relative strength** (not macro/business-cycle rotation) — an 80+ year Fama-French-sector-data backtest found RS-based rotation beats buy-and-hold in ~70% of years. Business-cycle-based rotation, by contrast, is empirically debunked ("The Myth of Sector Rotation," AUT).

**Tier 2 — Well-documented, independently replicated, but with real decay/limitations:**
5. **Connors RSI(2)** — the best-documented *mechanical* short-term system here, multiple independent replications (70–85% down to a more sober ~64% win rate in some out-of-sample tests), still holds up directionally but with degraded magnitude vs. original claims. Note: this is mean-reversion, not the CEO's preferred breakout style.
6. **Follow-Through Days** — real, checkable historical pattern, but honestly only ~50/50 in predicting a sustained rally — necessary, not sufficient.
7. **CAN SLIM** — two independent academic backtests show real outperformance (13.9–20.2% avg returns beating S&P 500; +0.94%/mo vs NASDAQ 100), but **every real-world CAN SLIM/IBD-affiliated mutual fund has underperformed or failed** — a stark backtest-vs-live gap, likely from discretionary "N"/"L" criteria and execution costs.

**Tier 3 — Rigorously *tested and found weaker than the folklore claims*:**
8. **Stan Weinstein Stage Analysis** — a 2026 SSRN paper found naive practitioner backtests inflate Stage-2 returns by 6.3%/yr via survivorship bias, and once corrected, Stage 2's edge is statistically indistinguishable from a plain moving-average trend filter. The stage-labeling taxonomy itself adds nothing measurable. Important methodological warning for every other item in this catalog that hasn't been academically re-tested this way.

**Tier 4 — Practitioner/anecdote-grade, no independent quant backtest found:**
9. Minervini VCP/Trend Template, Darvas boxes, Qullamaggie episodic pivots/high-tight-flags, Anchored VWAP, Pocket Pivots. All are well-specified discretionary or semi-discretionary patterns with strong practitioner track records (audited in Minervini's case) but no peer-reviewed or credible independent quant backtest located. Pocket pivots are the most mechanically rule-based of this group and the easiest candidate to actually backtest rigorously.

**Cross-cutting takeaway for Confluence 2.0:** the strongest, most defensible foundation is (a) 52-week-high/momentum breakout entries, (b) regime-conditioned position sizing that dampens after high-vol regimes (momentum-crash literature) rather than blocking entries, (c) cost/slippage-realistic backtesting given the ORB net-zero replication, and (d) a hard requirement to test any new rule against point-in-time (not current) universe membership, given the Weinstein survivorship-bias finding generalizes to every backtest in this catalog.
