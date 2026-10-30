# Earnings-date sources: measured comparison (2026-10-29)

Research only. No repo code changed. No orders placed. Scripts and raw responses are in the session scratchpad (`es/*.py`, `es/raw/*.json`). Signed: Claude.

## Plain English

**The problem.** The bot gets earnings dates only from FMP. Our FMP plan does not cap the number of rows. It returns a fixed sample of about 64 popular symbols (AAPL, MSFT, TSLA and similar) and nothing else, even on a single-day query. ServiceNow (NOW), Broadcom (AVGO), Micron (MU), Super Micro (SMCI), SanDisk (SNDK), Palo Alto (PANW), CrowdStrike (CRWD), Eli Lilly (LLY), GE Vernova (GEV) and 17 more of our 37 stocks never appear. The per-symbol FMP endpoint returns HTTP 402 for 19 of our 37 stocks. That is why the bot could not see NOW report after the close on 10-28 and was holding it into the 10-29 gap.

**Recommendation.**
- **Primary: Finnhub `/calendar/earnings?symbol=X` (per-symbol mode).** We already have the key and it is free.
  - It returned a next earnings date for **37 of 37** stocks in the universe and **25 of 25** of the stocks reporting in the next 45 days.
  - It has before-open or after-close timing for 21 of those 25.
  - All five problem names are present (NOW, AVGO, MU, SMCI, SNDK).
  - For all 18 reports between 10-20 and 10-29, it has the same date the company filed its earnings 8-K with the SEC. That includes NOW, GOOGL and MSFT on 10-28 after the close. The one exception is SMCI's unscheduled 10-20 filing, which no source had.
  - It has NVIDIA on 11-17, which NVIDIA confirmed in a 10-28 press release. Nasdaq and Alpha Vantage still show 11-18.
- **Confirmation: Alpha Vantage `EARNINGS_CALENDAR`.**
  - One call returns the whole US market for the next 3 or 12 months as a CSV, with timing included.
  - It covered 25 of 25 in-window reporters.
  - It needs a free key (signup at alphavantage.co, 25 calls a day). The `demo` key works today, but it is meant for demos, not production use.
  - Its dates match Nasdaq's on every name, so the two are probably the same underlying feed (inferred).
- **After-the-fact audit: SEC EDGAR 8-K item 2.02 filings.** These are free and are the true record of when a company actually reported. Use them to score the calendars and to catch unscheduled results. SMCI filed preliminary results on 10-20 that no calendar listed.
- **Where sources disagree, use the earliest date.** For an earnings blackout, the earliest date is the safe side. Today 8 names disagree, all on dates the companies have not yet confirmed (table below).

Not recommended for the bot: Yahoo/yfinance (good data, but our data-tier rules forbid it for trading use, and its terms are unofficial), the Nasdaq JSON (undocumented, and it was stale on NVIDIA), FMP on the current plan, and Alpaca (no earnings calendar).

## Coverage table (measured 2026-10-29, about 16:50 PT)

Universe: 37 common stocks = WATCHLIST + DAY_TIER_UNIVERSE + FOREVER6_UNIVERSE + the QHM picks + the owner's check list. The 10 ETFs (SPY, QQQ, SOXL, SOXS, DRAM, EWY, TSLL, NVDL, TQQQ, SQQQ) are excluded because they have no earnings. The 45-day window is 10-29 to 12-13. Of the 37 stocks, 25 report in that window (the union across all sources).

| Source | Next date for any of 37 | In-window reporters found (of 25) | Timing (BMO/AMC) on in-window | NOW / AVGO / MU / SMCI / SNDK | Back-check 10-20..10-29 vs EDGAR (18 events) | Cost / limits |
|---|---|---|---|---|---|---|
| **Finnhub, per symbol** | **37/37** | **25/25** | 21 | all 5 | 17/18 (only SMCI's unscheduled 10-20 filing missed, which every source missed) | free key (have it); 60 calls/min |
| Finnhub, date-range mode | 22/37 | 22/25 | 19 | NOW, AVGO, SNDK | 17/18 | **silently capped at 1500 rows per call**: the week 11-03..11-09 was truncated, dropping SMCI, TOST and BRK.B. Single-day calls avoid the cap (11-05 = 528 rows) |
| Nasdaq api JSON (per date) | 25/37 (window only) | 25/25 | 16 (past dates lose their timing) | NOW, AVGO, SMCI, SNDK (MU is outside the window) | 17/18 | free, no key, browser User-Agent needed; undocumented |
| Alpha Vantage EARNINGS_CALENDAR (demo key) | 27/37 (horizon ended 2027-01-27) | 25/25 | 16 | AVGO, MU, SMCI, SNDK (NOW's next date is 01-27, at the horizon edge) | forward-only, so it cannot back-check (7/7 of today's 10-29 reporters present) | free key needed (25 calls/day; 1 call = whole market) |
| Yahoo (yfinance) | 37/37 | 25/25 | 20 | all 5 | 17/18 | free, unofficial; **barred by our data-tier rules for trading** |
| FMP stable calendar (any window, even one day) | 11/37 | 11/25 | 0 (no timing field) | none | 13/18 (missing NOW, GEV, LLY, MSTR, SNDK) | current plan: about a 64-symbol sample list |
| FMP stable per-symbol | 18/37 return 200 | n/a | none | 402 for all 5 | n/a | 402 on 19 symbols |
| FMP legacy v3/v4 | 403 | n/a | n/a | n/a | n/a | "legacy users before Aug 31 2025 only" |
| Alpaca corporate actions | none | n/a | n/a | n/a | n/a | returns dividends and splits only, no earnings type |
| Alpaca news (Benzinga) | unstructured | n/a | n/a | only the NVDA scheduling press release | n/a | headline text only; useful as a confirmation signal |
| SEC EDGAR 8-K 2.02 | past only | n/a | filing time gives timing | all 5 (history) | it is the truth set | free; 10 requests/sec; needs a User-Agent with a contact |

### Disagreements (all on dates the companies have not yet confirmed, except NVDA)

| Stock | Finnhub | Nasdaq | Yahoo | Alpha Vantage | Note |
|---|---|---|---|---|---|
| NVDA | 11-17 amc | 11-18 | 11-17 amc | 11-18 | **Company confirmed 11-17, 2 pm PT** (press release 10-28, via Alpaca news). Nasdaq and Alpha Vantage are stale. |
| AVGO | 12-09 amc | 12-10 | 12-09 amc | 12-10 | unconfirmed |
| BRK.B | 11-02 | 11-06 | 11-07 | 11-06 | unconfirmed; Berkshire usually reports on a Saturday |
| CRM | 12-01 amc | 12-02 | 12-02 amc | 12-02 | unconfirmed |
| CRWD | 11-30 | 12-01 | 12-01 amc | 12-01 | unconfirmed |
| SNOW | 12-01 amc | 12-02 | 12-02 amc | 12-02 | unconfirmed |
| MARA | 11-11 | 11-03 | 11-04 amc | 11-03 | unconfirmed (web search found no Q3-2026 release); Finnhub is the outlier and is **later**, which is the unsafe direction |
| PANW | 11-12 | 11-18 | 11-19 amc | 11-18 | unconfirmed; last year's report was 11-19 |
| MU | 12-15 amc | outside window | 12-23 amc | 12-16 | unconfirmed; just outside the 45-day window |

Pattern, inferred from these 8 cases and not proven: for unconfirmed dates, Finnhub's estimate is usually one day *earlier* than the others. The earliest-date rule covers that. MARA is the exception, where Finnhub is later, so a second source is needed.

## What was measured vs inferred

**Measured** (live API calls this session):
- every coverage count;
- the row counts;
- the FMP 402/403 statuses and the about-64-symbol sample list;
- Finnhub's 1500-row truncation;
- the EDGAR 8-K 2.02 dates and times;
- the NVIDIA confirmation text;
- the Alpaca corporate-actions types;
- all disagreements.

**Inferred / not measured:**
- that Nasdaq and Alpha Vantage share an upstream feed;
- the "Finnhub one day early" pattern;
- terms-of-use positions. I did not read each provider's terms in full. Nasdaq's JSON is undocumented, Yahoo is unofficial, and the Alpha Vantage demo key is documented as demo-only.

**Not measurable:** whether each source *listed* NOW before 10-28. The APIs return today's state, not a past snapshot. The back-check proves only that the sources hold the correct historical date now. Finnhub's symbol mode already showed actual EPS for the 10-29 after-close reporters by 16:50 PT the same day.

## Method and raw numbers

- **Finnhub.**
  - Per-symbol mode: `symbol=X&from=2026-10-20&to=2027-03-31`, 37 calls, all HTTP 200.
  - Range mode: weekly chunks, with these row counts.

    | Week | Rows |
    |---|---|
    | 10-20 | 482 |
    | 10-27 | 1031 |
    | 11-03 | **1500 (cap)** |
    | 11-10 | 1103 |
    | 11-17 | 345 |
    | 11-24 | 95 |
    | 12-01 | 117 |
    | 12-08 | 57 |

  - Full-window call: 1500 rows (capped).
  - Headers showed `X-Ratelimit-Limit: 60`.
- **Nasdaq.** `api.nasdaq.com/api/calendar/earnings?date=D`, one call per weekday 10-20..12-11, all HTTP 200. Peak day was 11-05 with 610 rows. Timing values are `time-after-hours`, `time-pre-market` or `time-not-supplied`; past dates revert to "not supplied". The per-symbol endpoint `api/analyst/{sym}/earnings-date` agrees with the date endpoint, including NVDA = 11-18 (stale).
- **FMP.**
  - The stable calendar returned 40 rows over the full 45 days and 27 rows over 2 weeks.
  - Single-day calls returned 0–10 rows, across 64 distinct symbols total. So the limit is a symbol whitelist, not a per-call row cap.
  - The per-symbol endpoint returned 402 for AVGO, BRK.B, CRM, CRWD, DDOG, GEV, LLY, MARA, MSTR, MU, NET, NOW, PANW, SMCI, SNDK, SNOW and TOST.
  - The v3 `earning_calendar`, v3 `historical/earning_calendar` and v4 `earning-calendar-confirmed` endpoints all returned 403 (legacy-only).
- **Yahoo.** yfinance 1.7.0, using `Ticker.calendar` and `get_earnings_dates(limit=6)`. All 37 resolved. A timestamp hour of 15 or later is read as after the close, 9 or earlier as before the open.
- **Alpha Vantage.** `function=EARNINGS_CALENDAR&horizon=3month&apikey=demo` returned HTTP 200 with 3,846 rows covering 2026-10-29 to 2027-01-27 and a `timeOfTheDay` column.
- **EDGAR.** `company_tickers.json` was used to look up each company's CIK, then `data.sec.gov/submissions/CIK##########.json`, filtered to 8-K forms whose items include 2.02. NOW filed at 2026-10-28 20:14Z, GOOGL at 20:01Z and MSFT at 20:04Z, all after the close. SMCI filed on 10-20 (unscheduled preliminary results).
- **Alpaca.** `/v1/corporate-actions` returned the key `cash_dividends` only for NOW, NVDA and AVGO. `/v1beta1/news` with `include_content` returned the NVDA scheduling release ("conference call on November 17, 2026, at 2 p.m. PT").
- **Other sources, not tested and no signup done.**
  - Earnings Whispers, Zacks, Wall Street Horizon, Benzinga direct and Polygon/Massive are paid or need an account. Wall Street Horizon and Benzinga sell confirmed-date feeds; pricing was not checked.
  - Alpha Vantage's free key is the cheapest full-market option found.
