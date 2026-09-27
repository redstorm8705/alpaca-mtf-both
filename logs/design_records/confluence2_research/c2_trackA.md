# Confluence 2.0 — Track A: Swing-Horizon Entry Signal Evidence Catalog

Frame: paper account, $2.5K→$25K growth phase. Universe: large/mid-cap US equities, swing horizon
2–20 trading days. Data available: Alpaca (OHLCV, option chains), FMP free tier (fundamentals,
earnings calendar, 250 calls/day), yfinance for VIX/VIX3M, TraderMonty breadth CSV.

Every entry below cites a URL actually opened via WebSearch during this research pass.

---

## 1. Cross-sectional momentum (Jegadeesh & Titman 1993)
**Formula:** rank stocks on 3–12 month past return, buy top decile, hold 3–12 months (K/J momentum).
**Predicts:** 3–12 month forward continuation. **Universe/period:** NYSE/AMEX 1965–1989, replicated globally.
**Evidence quality:** Out-of-sample replicated across decades and international markets; the original
"bombshell" anomaly (per Shiller). Post-2000 partial decay documented but persists in most samples.
**Sharpe/return:** ~1% per month raw spread (top-minus-bottom decile) in the original sample.
**Data needed:** daily/monthly OHLCV only. **Long-only:** works but long-short captures full spread;
strong even long-only on large caps (winner leg alone).
**URL:** https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1993.tb04702.x

## 2. Time-series momentum (Moskowitz, Ooi & Pedersen 2012)
**Formula:** sign of trailing 12-month own return predicts next-month sign; scale by inverse vol.
**Predicts:** 1–12 month continuation, partial reversal at longer horizons.
**Universe/period:** 58 futures across equity index/FX/commodity/bond, decades of data (JFE 2012).
**Evidence quality:** Out-of-sample, cross-asset replicated; performs best in extreme/crisis markets.
**Data needed:** daily OHLCV (futures, but the trend-following mechanic transfers to equities via
ETF/SPY-style filters). **Long-only:** yes, works as long/flat filter.
**URL:** https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2089463

## 3. 52-week-high momentum (George & Hwang 2004)
**Formula:** nearness of current price to 52-week high (price / 52wk-high ratio) predicts returns,
dominates and subsumes plain past-return momentum.
**Predicts:** 6–12 month continuation; **does not reverse** long-run (unlike plain momentum).
**Universe/period:** NYSE/AMEX/Nasdaq 1963–2001; replicated in 18 of 20 international markets.
**Evidence quality:** High — published JoF 2004, widely replicated (Liu/Liu/Ma international; Quantpedia).
**Data needed:** daily OHLCV only (rolling 252-day high). **Long-only:** yes; works well on large caps.
**URL:** https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1104491 ; international replication:
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1364566 ; summary: https://quantpedia.com/strategies/52-weeks-high-effect-in-stocks

## 4. Frog-in-the-pan / information discreteness (Da, Gurun & Warachka 2014)
**Formula:** classify formation-period return path as "continuous" (many small same-sign daily moves)
vs "discrete" (few large jumps) using sign-consistency of daily returns; momentum profits are
concentrated in continuous-information stocks (5.94%) vs near-zero/negative for discrete (-2.07%).
**Predicts:** improves momentum stock selection — filters which momentum names actually continue.
**Universe/period:** US equities, published Review of Financial Studies 2014.
**Evidence quality:** Peer-reviewed, high-quality filter on top of #1; low media/analyst coverage
associated with more "continuous" (higher-quality) momentum.
**Data needed:** daily OHLCV (sign-consistency calc) — no extra data source needed.
**Long-only:** yes, is a QUALITY FILTER for momentum entries, ideal cross-use with signal #1.
**URL:** https://academic.oup.com/rfs/article-abstract/27/7/2171/1578455 ; PDF: https://academicweb.nd.edu/~zda/Frog.pdf

## 5. Short-term reversal, 1-week/1-month (Jegadeesh 1990; Lehmann 1990)
**Formula:** sort on trailing 1-week or 1-month return; losers outperform winners going forward.
**Predicts:** 1-week to 1-month reversal (opposite sign to momentum at this horizon).
**Universe/period:** NYSE 1934–1987 (Jegadeesh); replicated.
**Evidence quality:** Robust but largely a LIQUIDITY-PROVISION / microstructure effect; post-cost
profits shrink sharply for large/liquid names (leading explanation = compensation for market-making,
not mispricing) — weak edge on large caps after realistic costs.
**Data needed:** daily OHLCV. **Long-only:** buy-the-dip variant only; long-short version stronger.
**Use case here:** as a SHORT-TERM PULLBACK/ENTRY-TIMING filter within an established uptrend (buy
the 1-week dip inside a 12-month uptrend), not a standalone signal.
**URL:** https://www.newyorkfed.org/medialibrary/media/research/staff_reports/sr513.pdf (decomposition);
background: https://alphaarchitect.com/quantitative-momentum-research-short-term-return-reversal/

## 6. Industry momentum / lead-lag (Moskowitz & Grinblatt 1999; Hou 2007)
**Formula:** rank industries on past 6-month return; buy stocks in top-momentum industries. Hou (2007)
shows the effect is INTRA-industry, large-cap leads small-cap within the same industry.
**Predicts:** 1–6 month industry-level continuation; large-cap news diffuses to small-cap peers with lag.
**Universe/period:** US equities, published JF/RFS era papers, replicated in cross-industry/supply-chain
literature since (geographic, analyst-linked, tech-linked lead-lag).
**Evidence quality:** Well-replicated; industry momentum explains a large share of individual-stock
momentum profits.
**Data needed:** daily OHLCV + sector/industry classification (FMP has sector/industry fields).
**Long-only:** yes — usable as a SECTOR CONFIRMATION filter (only take entries in top-tercile momentum
sectors), directly relevant to the bot's existing sector gate.
**URL:** http://www-stat.wharton.upenn.edu/~steele/Courses/956/Resource/Momentum/MoskowitzGrinblatt99.pdf

## 7. Post-earnings-announcement drift / PEAD (Bernard & Thomas 1989/1990)
**Formula:** standardized unexpected earnings (SUE) = (actual EPS − expected EPS) / std-dev of surprise;
top-SUE-decile stocks drift up for 60+ trading days after the print.
**Predicts:** ~1–3 month post-earnings drift.
**Universe/period:** US equities 1974–1986 original; "granddaddy of underreaction anomalies" (Fama 1998),
replicated across decades/markets. Later studies show 8.76%–43.08% annualized depending on methodology
(some decay expected post-2000 per McLean/Pontiff, see #20).
**Evidence quality:** One of the most replicated anomalies in empirical finance.
**Data needed:** earnings dates + consensus EPS estimates + actual EPS (FMP earnings calendar/estimates
T2) + analyst estimate history (may exceed free FMP 250/day call budget if run broadly — budget
concern).
**Long-only:** yes, strong long leg; large-cap PEAD is smaller but still positive.
**URL:** https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/earnings-autocorrelation-and-the-postearningsannouncement-drift-experimental-evidence/61CD6A2065A4686418A3C47DEF3AC24B ;
review: https://www.sciencedirect.com/science/article/pii/S2214635020303750

## 8. Analyst forecast revisions / earnings momentum
**Formula:** sort on recent analyst EPS estimate revisions (up-revisions minus down-revisions,
normalized); stocks with positive revision momentum outperform.
**Predicts:** 1–6 month drift as analysts/prices slowly incorporate revision information; combines
with price momentum (bivariate sort yields up to 22%/yr gross Carhart alpha per cited study).
**Universe/period:** US equities, multiple studies since 1990s.
**Evidence quality:** Well replicated; some of the "momentum" anomaly is explained by analyst
behavioral bias (per Grinblatt et al. — a caution that isolating pure revision-signal from
correlated price-momentum is imperfect).
**Data needed:** analyst estimate revision history — NOT reliably free on FMP's 250-call/day tier
at scale; DATA GAP for this bot without a paid estimates feed.
**Long-only:** yes.
**URL:** https://anderson-review.ucla.edu/wp-content/uploads/2021/03/Grinblatt_SSRN-id2653666.pdf

## 9. Overnight vs. intraday return decomposition (Lou, Polk & Skouras 2019, "Tug of War")
**Formula:** decompose each day's return into close→open (overnight) and open→close (intraday)
components; momentum profits are earned almost ENTIRELY overnight, several other factors (value,
profitability) earn premia intraday. Persistent cross-period reversal (overnight winners give it
back intraday and vice versa) lasting years.
**Predicts:** informs WHEN within the trading day a signal's edge actually accrues — a
timing/execution-mechanics signal rather than a standalone entry trigger.
**Universe/period:** US equities, published JFE 2019.
**Evidence quality:** Peer-reviewed, robust across 14 strategies tested.
**Data needed:** OHLC (open needed in addition to close) — Alpaca T1 bars have this.
**Cross-use:** directly relevant to the bot's entry_logic — since this bot's momentum-style score
(EMA/MACD/12-1) likely earns its edge overnight, HOLDING OVERNIGHT (already default per Rule) is
consistent with the literature; an intraday-only exit would forfeit most of the edge.
**URL:** https://personal.lse.ac.uk/polk/research/TugOfWar.pdf

## 10. Return seasonality by calendar month (Heston & Sadka 2008)
**Formula:** a stock's own return in a specific past calendar month predicts its return in that same
calendar month in future years (own-firm seasonality, independent of industry/size).
**Predicts:** persists up to 20 years out at the firm level.
**Universe/period:** US equities; robust to size/industry/factor/calendar-month controls.
**Evidence quality:** Peer-reviewed (JFE), replicated; economically significant mainly in advanced
markets (per later international tests), weak/absent in emerging markets.
**Data needed:** long history of monthly returns per name (5+ years) — feasible via Alpaca daily bars
aggregated. **Long-only:** yes, usable as a low-weight seasonal tilt, not a primary trigger given the
odd/non-causal nature of the effect (data-mining risk on any single name).
**URL:** https://www.ssrn.com/abstract=687022

## 11. Volatility-managed portfolios (Moreira & Muir 2017)
**Formula:** scale position size inversely to trailing realized volatility (vol-target) — takes LESS
risk in high-vol regimes even though this seems contrarian to buying dips.
**Predicts:** applies to portfolio-level SIZING, not entry direction; raises Sharpe/alpha across
market, momentum, and other factors.
**Universe/period:** US factor portfolios, published JoF 2017.
**Evidence quality:** Peer-reviewed, but later work (Cejnek/Mair "Understanding...", Barroso et al.)
shows results are conditional/fragile in some specifications — treat as directionally supported,
not bulletproof.
**Data needed:** daily OHLCV only (realized vol calc). **Cross-use:** this is a SIZING multiplier,
NOT a new entry signal — directly maps to the existing VIX-based stop-widening/size-floor machinery;
adding a per-name realized-vol scalar to Kelly sizing is the natural interconnection (Anti-Silo gate).
**URL:** https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2659431

## 12. Idiosyncratic volatility anomaly (Ang, Hodrick, Xing & Zhang 2006)
**Formula:** high idiosyncratic vol (residual vol vs Fama-French 3-factor model) → LOW future returns.
**Predicts:** persists 1 month forward; robust internationally (G7 markets).
**Universe/period:** US equities 1963–2000, JoF 2006; extended internationally 2009.
**Evidence quality:** High — one of the more robust "low-vol" anomalies, though partly explained by
short-term reversal/liquidity in follow-up literature (a known open debate).
**Data needed:** daily OHLCV (residual regression vs market/size/value factors) — factor data would
need to be sourced or proxied (e.g., regress vs SPY/IWM/sector ETF instead of full FF3).
**Long-only:** yes — usable as an EXCLUSION filter (avoid entries in extreme-high-idio-vol names,
i.e., a risk/quality gate), not a standalone buy trigger.
**URL:** https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.2006.00836.x

## 13. Betting-against-beta / low-beta anomaly (Frazzini & Pedersen 2014)
**Formula:** long leveraged low-beta, short high-beta (full BAB); or simply favor lower-beta names
within the buy universe.
**Predicts:** persistent risk-adjusted outperformance from margin-constrained-investor demand for
high-beta "lottery" names.
**Universe/period:** US 1926–2009 (Sharpe 0.75), 20 global equity markets, plus bonds/futures.
**Evidence quality:** Peer-reviewed (JFE 2014), replicated across asset classes — one of the most
cross-validated factors.
**Data needed:** rolling beta to SPY (daily OHLCV only). **Long-only:** the low-beta LONG leg alone
retains most of the edge; well suited to a long-only paper bot as a QUALITY tilt away from high-beta
lottery names (interacts with the bot's leveraged-ETF Bucket A — a natural anti-silo cross-check: BAB
literature suggests leveraged/high-beta names like TQQQ/SQQQ are the exact profile with LOWER
risk-adjusted expected return per unit vol, i.e., size them smaller per unit beta, not just per ATR).
**URL:** https://pages.stern.nyu.edu/~lpederse/papers/BettingAgainstBeta.pdf

## 14. High-volume return premium / abnormal turnover (Gervais, Kaniel & Mingelgrin 2001)
**Formula:** unusually high (low) 1-day or 1-week trading volume relative to trailing norm predicts
positive (negative) return over the FOLLOWING month ("investor recognition"/visibility effect).
**Predicts:** ~1-month forward continuation triggered by a volume SHOCK, not price movement itself.
**Universe/period:** NYSE/AMEX, published JoF 2001; robust across high/normal/low volume subgroups.
**Evidence quality:** Peer-reviewed, replicated, though later literature ("persistence or reversal")
shows the sign can be regime-dependent — evidence is good but not unconditionally one-directional.
**Data needed:** daily OHLCV (volume field) only — already free on Alpaca T1.
**Long-only:** yes; this is the closest academic anchor for the bot's shadowed "volume confirmation"
feature (VOLUME_CONFIRMATION_ENABLED) — directly supports recalibrating that threshold as a
volume-SHOCK percentile (STOD-normalized) rather than a static 1.5x ratio.
**URL:** https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00349

## 15. Short interest / days-to-cover (Asquith, Pathak & Ritter 2005; Hong et al. days-to-cover)
**Formula:** high short-interest-to-float or high days-to-cover ratio predicts underperformance
(short-sale-constrained stocks underperform), effect amplified around negative news days.
**Predicts:** the SHORT side / avoidance signal — a name with extreme short interest + negative news
should be filtered OUT of long entries, or used as a bearish confirmation.
**Universe/period:** US equities 1988–2002 (Asquith et al.); NBER days-to-cover extension.
**Evidence quality:** Peer-reviewed (NBER), but the effect is much larger equally-weighted (−215bp/mo)
than value-weighted (−39bp/mo, insignificant) — i.e., WEAK on large caps, which is this bot's universe.
**Data needed:** short interest data — NOT available on Alpaca/FMP free tier; DATA GAP, would need a
separate source (FINRA bi-monthly short interest is free but low-frequency/stale for swing trading).
**Long-only:** usable only as an avoidance/exclusion filter, and only marginally so on large caps.
**URL:** https://www.nber.org/system/files/working_papers/w10434/w10434.pdf

## 16. Options-implied volatility skew (Xing, Zhang & Zhao 2010)
**Formula:** IV skew = IV(OTM put) − IV(ATM call); steeper skew (informed puts-buying) predicts LOWER
future stock returns.
**Predicts:** ~1-month forward; captures informed trader activity in the options market ahead of
price-relevant news.
**Universe/period:** US optionable equities, JFQA 2010.
**Evidence quality:** Peer-reviewed, one of the most cited options-implied signals; robust across
size/momentum controls in the original and later replications.
**Data needed:** option chain with strikes/IVs — Alpaca get_option_chain provides this (T1, already
approved). **Long-only:** use as an EXCLUSION/avoid-entry filter on names with steep negative skew,
or as confirmation (flat/positive skew = supportive) before a long entry — natural GEX/options-scanner
cross-use per the Anti-Silo mandate.
**URL:** https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1107464

## 17. Implied-minus-realized volatility spread / put-call IV spread (Bali & Hovakimian 2009;
Cremers & Weinbaum 2010)
**Formula:** (a) realized vol − implied vol spread signals jump-risk information; (b) call-IV minus
put-IV spread (put-call parity deviation) predicts returns, strongest where information asymmetry
is high.
**Predicts:** ~1-month forward.
**Universe/period:** US optionable equities, JFQA-era papers, replicated/extended since (incl. recent
machine-learning replications through 2026 per arXiv).
**Evidence quality:** Peer-reviewed, actively still-researched (recent 2026 arXiv extension found),
suggesting the signal has NOT fully decayed.
**Data needed:** option chain IVs across strikes (Alpaca T1 option data). **Long-only:** usable as
confirmation alongside #16; both are natural additions to the options scanner (again, direct Anti-Silo
target — GEX + IV-skew + put/call spread all belong in the same options-informed confluence layer).
**URL:** https://www.sciencedirect.com/science/article/pii/S0304405X25001618 ; recent extension:
https://arxiv.org/html/2608.26115

## 18. Residual / idiosyncratic momentum (Blitz, Huij & Martens 2011)
**Formula:** rank on past 12-month RESIDUAL return (regression residual vs Fama-French factors)
instead of raw return; produces momentum with ~2x the risk-adjusted profit of standard momentum and
much lower factor-timing risk (avoids the momentum "crash" exposure to market-beta/size/value swings).
**Predicts:** 1–12 month continuation, more stable than #1.
**Universe/period:** US/global equities, Journal of Empirical Finance 2011, extended by
idiosyncratic-momentum (2017) and reversal-revisit (2016) follow-ups.
**Evidence quality:** Peer-reviewed, multiply-replicated, addresses the well-known "momentum crash"
weakness of #1.
**Data needed:** requires a factor-residual regression (proxy vs SPY/sector ETF feasible without full
FF3 data feed). **Long-only:** yes, large-cap-friendly.
**URL:** https://www.ssrn.com/abstract=2319883

## 19. Technical-analysis evidence reviews (Park & Irwin 2007; Lo, Mamaysky & Wang 2000)
**Findings:** Park & Irwin's meta-review of 95 modern studies found 56 positive / 20 negative / 19
mixed on technical trading rule profitability, but flagged pervasive data-snooping and cost/risk
estimation problems — a caution against over-trusting single backtests. Lo/Mamaysky/Wang (2000) built
a rigorous kernel-regression pattern-recognition method (head-and-shoulders, double-tops/bottoms) on
1962–1996 US data and found several patterns DO carry statistically significant incremental
information over the unconditional return distribution, though practical profitability after
costs is uncertain.
**Predicts:** short-to-medium horizon; evidence is mixed/moderate, NOT a strong standalone edge.
**Evidence quality:** Meta-review + one rigorous academic implementation; the honest takeaway is
"some information content, weak-to-moderate net-of-cost edge, high data-snooping risk" — this
directly supports NOT over-weighting classic chart patterns in Confluence 2.0 relative to the
momentum/earnings/volume signals above.
**Data needed:** daily OHLCV.
**URL:** https://papers.ssrn.com/sol3/papers.cfm?abstract_id=603481 ;
https://www.nber.org/system/files/working_papers/w7613/w7613.pdf

## 20. Factor decay after publication (McLean & Pontiff 2016)
**Finding:** across 97 published cross-sectional return predictors, out-of-sample returns are 26%
lower than in-sample, and 58% lower post-publication (an additional ~32% attributed to
publication-informed trading/arbitrage, beyond pure data-mining/statistical bias).
**Implication for Confluence 2.0:** every signal above should be assumed to underperform its
published headline number once implemented live, and effects concentrated in high-idiosyncratic-risk/
low-liquidity names decay LESS — i.e., large-cap-only implementation (this bot's universe) should
expect signals to be on the WEAKER end of the post-publication range, reinforcing the need for the
front-loaded simulation + kill-flag discipline already mandated (BUILD DON'T JUST FIX doctrine).
**Evidence quality:** Peer-reviewed (JoF 2016), the standard reference for anomaly decay.
**URL:** https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365

---

## Signals covered but ranked lower / structural notes

- **Frog-in-the-pan (#4)** and **residual momentum (#18)** are best read as REFINEMENTS to signal #1,
  not independent entries — they explain WHICH momentum stocks to keep/discard.
- **Short interest (#15)** and **analyst revisions (#8)** have real DATA GAPS on this bot's current
  free-tier stack (no short-interest feed, no analyst-estimate-revision feed on FMP free tier) —
  flagged, not ranked in the top 20 build list for that reason.
- **Donchian/breakout channels:** searched but found only practitioner/blog-level sources (TrendSpider,
  Medium, AvaTrade) with no peer-reviewed equity-specific evidence located in this pass — NOT included
  in the ranked 20; flag as an unresolved research gap requiring a dedicated Quantpedia/SSRN pass if
  Rafael wants it covered.
- **Trend-following across horizons** is covered structurally by #2 (time-series momentum) — no
  separate academic citation added beyond Moskowitz-Ooi-Pedersen.

---

## RANKED TOP 20 — swing-horizon, large-cap, long-biased, Alpaca+FMP+yfinance+breadth data stack

1. Cross-sectional momentum, 3–12mo (Jegadeesh-Titman) — core signal, zero new data cost
2. 52-week-high proximity (George-Hwang) — dominates raw momentum, zero new data cost, large-cap robust
3. Post-earnings-announcement drift / SUE (Bernard-Thomas) — FMP earnings calendar already integrated
4. Frog-in-the-pan quality filter on momentum (Da-Gurun-Warachka) — free refinement of #1, no new data
5. Residual/idiosyncratic momentum (Blitz-Huij-Martens) — reduces momentum-crash risk, proxy-feasible
6. Industry momentum / sector confirmation (Moskowitz-Grinblatt, Hou) — reuses existing sector gate
7. Options IV skew (Xing-Zhang-Zhao) — Alpaca option chain already available, unused today (silo)
8. Put-call / IV-RV spread (Bali-Hovakimian, Cremers-Weinbaum) — same option-chain data source as #7
9. High-volume return premium (Gervais-Kaniel-Mingelgrin) — direct evidence base for the shadowed
   volume-confirmation feature; recalibrate its static 1.5x threshold off this literature
10. Time-series momentum / trend filter (Moskowitz-Ooi-Pedersen) — reinforces existing SMA/EMA gate
11. Betting-against-beta tilt (Frazzini-Pedersen) — cross-applies to leverage/size decisions (Bucket A)
12. Idiosyncratic volatility exclusion filter (Ang-Hodrick-Xing-Zhang) — risk/quality gate, no new data
13. Volatility-managed sizing (Moreira-Muir) — sizing multiplier, extends existing VIX-scalar machinery
14. Overnight/intraday decomposition (Lou-Polk-Skouras) — validates overnight-hold mechanics already used
15. Analyst revisions / earnings momentum — DATA-GAP flagged; stage only if a revisions feed is added
16. Short-term reversal as entry-timing (Jegadeesh 1990) — pullback-within-uptrend refinement only
17. Return seasonality (Heston-Sadka) — low-weight seasonal tilt, real but small/data-mining-prone
18. Technical pattern recognition (Lo-Mamaysky-Wang) — weak-to-moderate edge, low priority add
19. Short interest / days-to-cover — DATA-GAP (no free feed) + weak on large caps; deprioritized
20. Factor-decay discipline (McLean-Pontiff) — not a signal itself; governs expected-Sharpe haircuts
    and mandates the front-loaded-sim standard for all 19 signals above
