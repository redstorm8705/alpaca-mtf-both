# QHM post-mortem: GOOGL and NOW earnings gaps on 2026-10-29 (signed: Claude)

Research only. No code was changed and no orders were placed.

Labels used below:
- **[measured]**: computed this session from Alpaca bars or option bars, or read in repo code.
- **[sourced]**: from an SEC filing or a news/transcript page, with the URL given.
- **[inferred]**: my own reasoning, not directly measured.

---

## 1. Plain-English summary

**GOOGL (QHM tier, 1 share, −$54.66).**
- Alphabet's business numbers were strong: revenue +25%, Google Cloud +103% [sourced].
- The stock still fell 12.8% [measured] because of three things investors care about more right now:
  1. **Cash.** Free cash flow was negative for the second quarter in a row, −$9.1B against about −$2.7B expected. Capex was $51.4B in one quarter, management said capex will "increase significantly in 2027", and coverage says 2027 could exceed $300B [sourced].
  2. **Search growth slowed.** Search & other grew 12%, down from 17% the quarter before [sourced].
  3. **A headline EPS miss.** EPS was $2.12 against about $3.02 expected. About $1 of that gap is a paper loss on Alphabet's SpaceX stake, not the operating business [sourced].
- The warning signs were visible before the report [measured/sourced]:
  - FCF had already turned negative in Q2 (−$5.9B).
  - Alphabet had sold about $50B of new stock and preferred shares in June, plus more debt and at-the-market share sales in Q3.
  - The stock sat below its 20, 50 and 200-day averages, 20% off its high, RSI 39.
  - On 10-21 Meta reported negative FCF from AI capex. Meta fell 7.1% the next day, and GOOGL fell 3.1% the same day.

**NOW (Swing tier, 2 shares, −$81.56).**
- This was **not a QHM pick.** The Swing tier bought it mechanically on a 55-day closing-high breakout two trading days before the report. The Swing tier has no earnings-date check [measured: no "earning" text in `execution/swing_breakout_manager.py`].
- The 10-29 gap **was earnings.** ServiceNow filed an 8-K Item 2.02 on 2026-10-28 with its Q3 results [sourced].
- The quarter beat on revenue, but there were three problems [sourced]:
  1. Next-quarter guidance for contracted revenue growth (cRPO) was 18.5%, against about 19.8% expected and 20.5% this quarter.
  2. About half of this quarter's cRPO beat came from renewals pulled forward from Q4.
  3. Adjusted EPS was $1.00 against about $1.02 expected.
- In a market already afraid that AI erodes per-seat software, slowing forward growth was punished. NOW opened −19.7% and closed −26.9% [measured].
- NOW had already dropped after 3 of its prior 4 reports (−9.9%, −17.6%, −3.6% next-day closes) [measured].

**Why the inputs said "buy".**
- GOOGL's QHM thesis was a **stale Q3 config entry** written 2026-06-20: "Search AI integration, YouTube growth, cloud margin expansion. Hold through Q2 2026 earnings ~Jul 28."
- It was bought on 9-29 as tranche 1, the day after a cap fix unblocked stale-config picks [inferred link; see §5].
- Nothing re-checked the thesis against the Q2 results, the cash-flow turn, the dilution, or the Meta read-through.
- The QHM research script ranks candidates by how far they have dipped. It never reads results, guidance, transcripts or estimate revisions [measured].
- NOW's buy signal is price only [measured].

**Why we were not short.**
- QHM and Swing are long-only tiers [measured].
- The Day tier can short, but it is flat by 3:58 PM ET [measured], so it can never hold a short through an after-close report.
- Even with a short-capable tier, the evidence before the print was mixed:
  - GOOGL leaned bearish (downtrend, peer read-through, cash burn), but strong Cloud growth argued the other way.
  - NOW's price trend was bullish (breakout, above all averages), while its earnings-reaction history was bearish.
- A directional short would have been roughly a coin flip with a modest bearish lean [inferred]. The defensible action was smaller size or none through the print, not a short.

**What changes.** Every QHM report must:
1. Check the last quarter's results and call.
2. Check estimate direction, the options-implied move and the exact report date.
3. End with a written pre-report action rule: hold full, trim, or exit before the print.

See §7.

---

## 2. GOOGL: evidence

### 2a. What Alphabet reported on 2026-10-28 (Q3 2026)

Source: SEC 8-K exhibit 99.1, https://www.sec.gov/Archives/edgar/data/1652044/000165204426000080/googexhibit991q32026.htm [sourced]

| Item | Q3 2026 | Q3 2025 / note |
|---|---|---|
| Revenue | $127.6B (+25%) | $102.3B |
| Google Services | $96.3B (+11%) | — |
| Search & other | $63.35B (+12%) | Q2 2026 growth was 17% |
| YouTube ads | $11.2B (+9%) | Q2 growth was 13% |
| Google Cloud | $30.7B (+103%); op. income $11.1B | Q2 2026 growth was 82% |
| Operating margin | 31.8% | Q2 2026: 34% |
| Other income | −$8.2B (equity-securities loss of $8.7B) | +$12.8B a year earlier |
| Diluted EPS | $2.12 | $2.87 a year earlier; the equity loss cut EPS by about $1 |
| Capex (PP&E purchases) | $51.4B | $24.0B a year earlier |
| Free cash flow | **−$9.1B** | Q2 2026: −$5.9B; TTM $19.7B |
| Financing in quarter | ATM sales of 12M Class A + 24M Class C shares ($12.4B); $28.7B senior notes | — |

**Estimates and reaction**, per the CNBC article (https://www.cnbc.com/2026/10/28/alphabet-googl-earnings-q3-2026.html; the page returned HTTP 403 to direct fetch, so these figures come via the search summary):
- Adjusted EPS $2.67 vs $3.02 expected.
- Revenue below estimates.
- FCF −$9.07B vs about −$2.70B expected.
- Capex $51.36B vs $55.09B expected.
- Described as the "first top-and-bottom-line miss in nearly four years".
- Treat these estimate figures as single-source.

**Call highlights** (Investing.com transcript, https://www.investing.com/news/transcripts/earnings-call-transcript-alphabet-q3-2026-profit-miss-sparks-10-afterhours-drop-93CH-4981037) [sourced]:
- **Capex.** FY2026 guidance unchanged at $195–205B. 2027 capex expected to "increase significantly"; details on the Q4 call.
- **Cash flow.** FCF was hit by capex, a TPU inventory build (cash goes out before TPU-system revenue comes in) and a $5.2B EC fine payment. Management said TPU inventory and infrastructure will keep pressuring FCF.
- **Margins.** Cloud operating margin was 36.2%, but management flagged near-term Q4 Cloud margin headwinds as it ramps third-party compute. Depreciation and energy costs keep rising.
- **Search.** Management attributed the slowdown to FX (the largest factor), APAC retail, World Cup timing, and lower monetization in AI Mode. Q4 has a larger FX headwind plus a tougher comparison.
- **Analyst questions** focused on the gap between query growth and Search revenue (ads in AI surfaces), TPU margins, and Cloud growth excluding TPU sales.
- Cloud backlog was $561B.

**Why the market sold it** [inferred from the above]:
- The market currently pays for AI growth only if cash comes back.
- Two straight quarters of negative FCF, a bigger 2027 capex bill, ongoing share issuance, and a Search slowdown tied partly to AI Mode monetization outweighed the Cloud beat.
- The "EPS miss" headline is mostly a SpaceX mark-to-market loss, which is not operational.

### 2b. Prior quarter (Q2 2026, reported 2026-07-22): warning signs

Source: https://www.sec.gov/Archives/edgar/data/1652044/000165204426000066/googexhibit991q22026.htm [sourced]
- **Revenue** +24%; Search +17%; Cloud +82%; operating margin 34%.
- **EPS was flattered.** EPS $9.11 included a $99.0B equity-securities gain that added $6.26/share. The repo already noticed this; `logs/tb_audit_log.md` flags "GOOGL Q2-26 EPS 9.11 vs 2.31" as a likely one-time item.
- **FCF turned negative: −$5.9B.** Capex was $44.9B, double the year before.
- **Dilution and leverage.** In June 2026 Alphabet issued Class A, Class C and mandatory-convertible preferred stock for **$49.6B** "including capital expenditures to scale AI infrastructure". It also issued $20.3B of notes.
- **Market reaction to Q2:** next-day open −6.2%, close −7.1% [measured]. The market had already punished the cash turn once.

Verdict [inferred]:
- Clear warning signs before the 9-29 buy: negative FCF, rising capex, heavy equity issuance, and a −7% reaction to Q2.
- The QHM thesis from June ("cloud margin expansion") was half right (Cloud margins did expand). It missed that the market's question had shifted to cash return.

### 2c. Technicals before the report

[measured; Alpaca daily bars. SIP was not authorized for this request, so volumes are the IEX feed only.]

| Metric (close 2026-10-28) | Value |
|---|---|
| Close | 325.37 |
| SMA20 / SMA50 / SMA200 | 339.83 / 341.50 / 341.19 (below all three) |
| RSI(14) | 39.1 |
| 52-week high | 408.57 (−20.4% off) |
| Return 5d / 20d / 60d | −2.8% / −5.4% / −13.8% |
| Volume, 5-day vs 50-day avg (IEX) | 1.30× (rising into the print) |
| Options-implied move, 10-30 expiry ATM straddle | 325 straddle closed $20.28 on 10-28, about **±6.2%** |
| Actual move | open −11.8%, close −12.8%, about 2× the implied move |

- **Implied-move caveat.** These are last-trade daily closes from Alpaca option bars, not mid-quotes. Historical option bars *are* available from Alpaca, so the implied move can be captured going forward.
- The 9-29 entry at 339.97 was near the 50/200-day averages. From 10-13 (353.57) the stock trended down into the report.

**Past earnings reactions** (next-day close vs report-day close) [measured]:

| Report date | Next-day close |
|---|---|
| 2025-10-29 | +2.5% |
| 2026-02-04 | −0.2% |
| 2026-04-29 | +9.9% |
| 2026-07-22 | −7.1% |
| 2026-10-28 | −12.8% |

### 2d. News and positioning in the 2 weeks before

**Meta read-through** [measured + sourced].
- Meta reported Q3 2026 on 2026-10-21 after the close (8-K exhibit 99.1: https://www.sec.gov/Archives/edgar/data/1326801/000162828026066700/meta-09302026xexhibit991.htm).
- Results: revenue +26%, capex $34.9B, free cash flow −$2.12B, 2026 capex narrowed to $135–145B, and a $9.9B legal charge.
- Moves on 10-22: META −7.1% (698.54 → 648.76); GOOGL −3.1% (334.72 → 324.20).
- From 10-14 to 10-28, META fell 17.0% while QQQ fell 0.5% [measured].
- The same thing (negative FCF from AI capex) was punished in a peer one week earlier [inferred].

**Analyst positioning.**
- A Bloomberg-sourced preview framed the week as a test of whether "$800B AI capex pays off" (https://aiweekly.co/alerts/bloomberg-alphabet-microsoft-meta-earnings-this-week-will-judge-whether-800b-ai) [sourced].
- I could not obtain estimate-revision history for GOOGL from a free source this session. **Not checked.**

---

## 3. NOW: evidence

### 3a. Was the 10-29 gap earnings? Yes.

ServiceNow filed an 8-K with Items 2.02 and 9.01 on 2026-10-28 [sourced]:
- Results: https://www.sec.gov/Archives/edgar/data/1373715/000137371526000092/erq3fy26.htm
- An 8-K/A followed: https://www.sec.gov/Archives/edgar/data/0001373715/000137371526000095/erq3fy262.htm

### 3b. What ServiceNow reported (Q3 2026)

| Item | Q3 2026 | Q2 2026 |
|---|---|---|
| Subscription revenue | $3,988M, +21% (20.5% constant currency) | $3,877M, +24.5% (23% cc) |
| Total revenue | $4,106M, +20.5% | $3,987M, +24% |
| cRPO | $13.65B, +20.5% (21% cc) | $13.20B, +21% (21.5% cc) |
| RPO | $29.7B, +22% | $29.0B, +21% |
| **Q4 guide: cRPO growth** | **18.5%** (cc) | — |
| Q4 guide: subscription revenue | $4,245–4,255M (+22.5–23%; 21.5% cc) | — |
| FY26 FCF margin guide | raised to 36% from 35% | — |

**Estimates**, per INDmoney (https://www.indmoney.com/blog/us-stocks/servicenow-stock-fall-q3-earnings-outlook) and TipRanks (https://www.tipranks.com/news/servicenow-stock-plunges-18-despite-another-beat-and-raise-quarter) [sourced]:
- Adjusted EPS $1.00 vs about $1.02.
- Revenue $4.106B vs about $4.095B.
- Q4 cRPO guide 18.5% vs about 19.8% consensus.

**Call** (Investing.com transcript, https://www.investing.com/news/transcripts/earnings-call-transcript-servicenow-q3-2026-revenue-beats-shares-sink-after-hours-93CH-4981127) [sourced]:
- **Why the cRPO guide is low.** About half of the Q3 cRPO beat came from early renewals pulled from Q4. Other drags: a higher on-prem mix from public-sector deals (about 25 bp), deployment-services investment, and about 50 bp of backlog excluded under T4C (termination-for-convenience) provisions.
- **Analyst questions.** Analysts pressed on outcome-based vs seat pricing, when AI momentum shows up in revenue and cRPO, and IT budgets into 2027.
- **Management's answer.** Management said customers are "not asking at all" for outcome pricing.

**Why the market sold it** [inferred]:
- Growth is decelerating: subscription growth slowed from 23% to 20.5% cc, and the cRPO guide fell to 18.5%.
- Part of this quarter's beat was borrowed from next quarter.
- This is a market that already discounts per-seat software for AI risk. The "death of SaaS" theme is documented, for example https://www.salesforceben.com/servicenow-stock-tumbles-50-in-a-year-good-earnings-dont-stop-death-of-saas/.
- A beat that does not accelerate growth was treated as a miss.

### 3c. Prior quarter (Q2 2026, reported 2026-07-22): warning signs

Source: https://www.sec.gov/Archives/edgar/data/1373715/000137371526000072/erq2fy26.htm [sourced]
- Q2 was also a beat-and-raise.
- **Next-day reaction to Q2:** open +4.6%, close −3.6% [measured]; the market faded it.
- **Earnings-reaction history** (next-day close) [measured]:

| Report date | Next-day close |
|---|---|
| 2025-10-29 | +2.4% |
| 2026-01-28 | −9.9% |
| 2026-04-22 | −17.6% |
| 2026-07-22 | −3.6% |
| 2026-10-28 | −26.9% |

  Mean of the four reports before 10-28: −7.2%.
- Note [inferred]: Q2 included acquired businesses (Armis, Veza are named in the releases), so organic growth was likely below the headline.

### 3d. Technicals before the report

[measured; Alpaca daily bars, IEX volume]

| Metric (close 2026-10-28) | Value |
|---|---|
| Close | 149.905 |
| SMA20 / SMA50 / SMA200 | 145.05 / 139.52 / 115.27 (above all three) |
| RSI(14) | 56.2 |
| 52-week high | 192.78 (−22.2% off) |
| Return 5d / 20d / 60d | −2.1% / +11.8% / +26.9% |
| Volume, 5-day vs 50-day (IEX) | 1.73× |
| Options-implied move, 10-30 ATM straddle | 150 straddle $15.97, about **±10.7%** |
| Actual move | open −19.7%, close −26.9% |

- **Entry and stop.** Entry 157.59 on 10-26, near the 10-23 high. Stop 142.07 (about 9.8% below), which is inside the implied move.
- **How the stop filled.** The gap opened about 22% below the stop, so the stop filled at 117.60/116.02, per the fills supplied.

### 3e. Sector into the print

[measured, 10-14 → 10-28 closes]
- Software ETF IGV +4.0%, CRM +2.7%, NOW about +7%: software was rallying into the print.
- On 10-29: IGV −3.5%, CRM −4.8%.

---

## 4. The bot's own inputs: what said "buy" and what was missing

### GOOGL (QHM)

**Where the thesis came from** [measured]:
- The repo copy of `data/state/quarterly_holds_config.json` is the Q3 2026 config dated 2026-06-20.
- GOOGL entry: `"thesis": "Pre-earnings long; Search AI integration, YouTube growth, cloud margin expansion. Hold through Q2 2026 earnings ~Jul 28."`, `earnings_exit_before: 2026-07-28`, `board_score 8.5/10`.
- The box copy was not available, and the user said not to use it.

**How it was bought on 9-29** [measured from design records + inferred link]:
- `logs/design_records/qhm_top3_action_2026-09-27.md` Increment 1 records live logs on 2026-09-28: "NVDA/GOOGL/GE tranche 1 — would breach QHM cap … entry BLOCKED". The fix counts only non-grandfathered holds.
- The next day GOOGL tranche 1 filled at 339.97 (1 share, the RC-7 floor).
- So a buy decision made on a June thesis executed three months later, with no thesis refresh.

**Entry gates are price-only** [measured from config fields]:
- `entry_day_gate_pct 0.85`, `day3_reconfirm_pct 0.02`.

**Earnings handling** [measured, `execution/quarterly_hold_manager.py`]:
- `QHM_EARNINGS_STOP_CANCEL_ENABLED = False` (`config.py:413`), so the stop stays on through earnings. This is by design, per `qhm_earnings_dates_fix_2026-10-12.md`.
- That is why the GTC stop at 288.97 was resting and filled at the gap open (285.31).
- The earnings profit-take (`_maybe_earnings_trim`) only trims a *gain*. GOOGL was below cost, so it could not act.
- Rafael's 2026-09-28 decision: "tranche adds run INTO earnings. No pre-earnings blackout."
- **Net: QHM has no rule that reduces a losing or flat hold before a binary report.**
- The 10-12 design record shows FMP did see GOOGL's 10-28 date, so the date was known.

**What the research pipeline checks** (`scripts/qhm_thesis.py`) [measured]:
- Checks: FMP earnings date; FMP grades-consensus; Alpaca dip metrics (% vs 50-day, % off 20-day high, RSI14); Dataroma 13F holders; a Gro/GAI narrative.
- Candidates are **ranked by dip depth** (`cands.sort(key=lambda c: c["m"].get("off20h", 0.0))`).
- Not checked: last-quarter results, guidance, transcripts, free cash flow, share issuance, estimate revisions, options-implied move, or peer read-through.

**`scripts/qhm_report.py`** [measured]:
- A status report: P&L, stops, earnings-date status.
- No thesis or fundamental content.

**What the GOOGL thesis relied on vs. did not check:**

| Relied on | Not checked |
|---|---|
| Cloud growth | Q2 FCF −$5.9B |
| Search AI integration narrative | $49.6B equity/preferred issuance |
| Analyst grade | Q2 −7.1% reaction |
| Price dip | Search deceleration risk; Meta's 10-21 read-through; implied move ±6.2%; whether to hold 1 losing share through the print |

### NOW (Swing)

**The entry rule is price only** [measured, `execution/swing_breakout_manager.py` docstring]:
- 55-day closing-high breakout above the 200-day SMA, then a 2.5×ATR GTC stop.
- No earnings check exists in the file.
- The user notes FMP's plan cannot see NOW's report date.
- The 10-12 record also shows FMP's batch calendar is sparse on our plan.

**Fallback to cover the gap** [measured this session]:
- SEC EDGAR's submissions feed shows NOW's Item 2.02 8-Ks on 2026-07-22 and 2026-10-28. A 13-week cadence would have predicted the date.

---

## 5. "Why were we not short?"

### Tier capability [measured]

| Tier | Can short? | Detail |
|---|---|---|
| QHM | No | `quarterly_hold_manager.py:3925` "this long-only tier"; adds are long only (`:2453`) |
| Swing | No | Docstring: "WHAT IT TRADES (long only, one lot per symbol)" |
| Day | Yes, but intraday only | Can short, and has inverse ETFs (GGLS for GOOGL, added in PR #544). `DAYTRADE_FORCE_FLAT_MINUTES = 2` means flat by 3:58 PM ET, so it cannot carry a short through a report released after the close |
| Main intraday bot | Not verified | Has short scoring (`MIN_SHORT_SCORE`). I did not verify whether it can hold a short overnight through a print; treat as unverified |

### Was there a short case before the print?

**GOOGL, bearish lean [inferred]:**
- Bearish: below all averages, RSI 39, −20% off high, Meta's FCF punishment on 10-21, and two quarters of negative FCF plus dilution.
- Bullish: Cloud +82% → +103%, a "beat" history, and the options market pricing only ±6%.
- The past four reactions split 2 up / 2 down.
- A short was a defensible bet but not a high-confidence one. Roughly 55–60/40 bearish by judgment, not by a tested model.

**NOW, genuinely mixed [inferred]:**
- The price trend was bullish: breakout, above the 20/50/200-day averages, +27% in 60 days.
- The event history was bearish: 3 of the last 4 reactions negative, mean −7.2%.
- Software was rallying into the print, and the implied move was ±10.7%.
- A short here would have been close to a coin flip.

**The lesson [inferred]:**
- The repeatable edge was not direction. It was size: do not carry a full-size, low-conviction long through a binary event when the stop sits inside the implied move.
- A stop does not protect against a gap; both stops filled far below their levels.

---

## 6. Data sources for the upgraded report

| Need | Source | Cost | In approved tiers? |
|---|---|---|---|
| Last-quarter press release, financials, FCF, capex, share issuance | SEC EDGAR `data.sec.gov/submissions` + 8-K exhibit 99.1 (worked this session) | Free; SEC asks for a contact User-Agent | **No.** Proposed as T2 in the handoff; pending, not in the CLAUDE.md tier table |
| Earnings date | FMP earnings calendar (T2). Fallback: EDGAR Item 2.02 cadence; company IR page | Free tier | FMP yes; EDGAR/IR no |
| Call transcript | Investing.com / Benzinga / Motley Fool pages; FMP transcripts (likely premium) | Free pages via web | **No** |
| Consensus and estimate revisions | FMP analyst-estimates / grades (plan coverage not verified this session) | Free–paid | FMP yes, if the plan allows |
| Options-implied move | Alpaca option bars and snapshots: ATM straddle ÷ price (historical bars worked this session) | Included | Yes (T1) |
| Technicals (averages, RSI, 52-week high, volume) | Alpaca daily bars via `data/fetcher.py` | Included | Yes (T1). SIP volume needs the subscription; IEX only otherwise |
| Past earnings reactions | Alpaca daily bars on Item 2.02 dates | Included | Yes (T1) + EDGAR dates |
| News and peer read-through | Alpaca News API (not tested this session); peer 8-Ks on EDGAR | Included / free | Alpaca yes; EDGAR no |
| 13F / positioning | Dataroma (already used) | Free | Already used by `qhm_thesis.py` |

---

## 7. Proposed QHM report template upgrade

Per pick. Every section is required, and an empty section means NOT READY.

1. **Header.**
   - Ticker, tier, shares, cost, current P&L.
   - Thesis date and age in days. A thesis older than one earnings cycle is flagged STALE and blocks new tranches.
2. **Report date.**
   - Exact date and time (before or after market), with its source (FMP / EDGAR cadence / IR).
   - Days to report.
   - If no source confirms the date: UNKNOWN, which counts as "report within window".
3. **Prior-quarter results.**
   - Revenue and segment growth vs the prior quarter (accelerating or decelerating).
   - Operating margin trend.
   - FCF and capex.
   - Share count, issuance and buybacks.
   - One-time items (for example mark-to-market gains).
   - Source: the 8-K exhibit 99.1 link.
4. **Prior call review.**
   - Guidance given vs delivered.
   - Management's top 3 risks.
   - The analyst-question themes (what the market is worried about).
   - Source: the transcript link.
5. **Estimates.**
   - Consensus EPS and revenue for the coming quarter.
   - Direction of revisions over the last 30 days (up / flat / down).
   - Mark "unavailable" if the plan blocks it. Never invent it.
6. **Technicals.**
   - Price vs 20/50/200-day averages, RSI(14), % off the 52-week high, 5/20/60-day returns, volume trend.
   - Stop level and its distance as a percentage.
7. **Options-implied move.**
   - ATM straddle for the first expiry after the report ÷ price.
   - **Compare it to the stop distance.** A stop inside the implied move means the stop is unlikely to protect against an overnight gap.
8. **Earnings-reaction history.** The last 4 next-day moves (open and close) and their mean.
9. **News and peers.**
   - Company news over the last 14 days.
   - Peer results that already reported this season and how those stocks reacted (for example META 10-21 for GOOGL).
   - Sector ETF move.
10. **Bull/bear checklist.** At least 3 points each, every one tagged measured or sourced.
11. **Pre-report action rule** (written, decided before the print; this is a proposal and needs the owner's approval and the full gate).
    Options: HOLD FULL / TRIM to X% / EXIT before close / ADD.
    Suggested defaults for the board to evaluate:
    - Thesis stale or estimates falling, AND the position is below cost → do not add; reduce before the print.
    - Stop distance smaller than the implied move AND the reaction-history mean below 0 → reduce.
    - Otherwise hold, with the expected loss at a 2× implied-move gap stated in dollars.
12. **Post-report review** (filled in after the print). Actual move vs implied, what the thesis missed, and whether to keep or drop the pick.

**Cross-tier follow-ups (not QHM, for the owner's queue):**
- The Swing tier has no earnings-date check and buys breakouts days before reports (NOW, 10-26).
- An EDGAR-cadence fallback for symbols FMP cannot date (NOW).
- Neither is a QHM change, and both need the owner's approval and the full gate. The 10-29 design record `EARNINGS-RISK-1029` already covers related ground; read it before proposing anything.
