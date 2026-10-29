# Earnings dates from Finnhub — EARN-SOURCE-1029 (signed: Claude, 2026-10-29) — DRAFT, not approved

Approval-ID: EARN-SOURCE-1029
Covers: data/fmp_client.py, tests/test_earnings_source.py

Owner, 2026-10-29: "We need full earnings data and scheduled reporting dates. There are a ton of sources available
online. This shouldn't happen." ... "Need something that has the full list."

## Problem (measured at source, logs/research/earnings_sources_2026-10-29.md)
- Every bot consumer of earnings dates goes through `data/fmp_client.py`:
  - QHM `_next_earnings`, which drives the pre-earnings trim and the stop decision;
  - main entry `entry_logic.py:1033` (`get_cached_earnings_dates`, ±3-day earnings check → 0.5× size or skip);
  - `earnings_preflight.py`;
  - `scripts/qhm_report.py`.
- On our FMP plan:
  - The per-symbol `/stable/earnings` returns HTTP 402 for 19 of 37 tested names (NOW, AVGO, MU, SMCI, SNDK …).
  - The `/stable/earnings-calendar` batch covers only a fixed sample of about 64 popular symbols (11 of our 37 in the
    next 45 days).
  - The legacy v3 endpoints return 403.
- `get_earnings_info` trusts a fresh batch "no date" (`NONE_IN_WINDOW`, source `fmp_batch`) for any scanned symbol, so
  for most names the main entry check reads "no earnings" when the batch simply does not cover them. QHM already
  re-asks per symbol on a batch "none" (L2287); the main entry path does not.
- Real case: NOW reported 2026-10-28 after the close; the bot had no date for it. Finnhub's per-symbol calendar returns
  `('2026-10-28', 'amc')` for NOW today (verified with a live call, 2026-10-29).

## Measured coverage (37 names, next 45 days = 25 reporters)
| Source | Next date found | Reporters in 45 d found | All of NOW/AVGO/MU/SMCI/SNDK | Matches SEC 8-K date, 10-20..10-29 |
|---|---|---|---|---|
| Finnhub, per symbol | 37/37 | 25/25 | yes | 17/18 (SMCI's unscheduled 10-20 filing: no source had it) |
| FMP calendar (batch) | 11 | 11 | none | 13/18 |
| FMP per symbol | 18/37 (402 for the rest) | — | none | — |
Finnhub free plan: 60 calls/minute; the key `FINNHUB_API_KEY` is already in `.env` and used by `events/news_monitor.py`.
Finnhub's date-range mode truncates at 1,500 rows per call (lost SMCI, TOST, BRK.B in the 11-03 week), so only the
per-symbol mode is used.

## Change (data/fmp_client.py only; the public functions and their return contract are unchanged)
1. `_symbol_lookup(sym)` asks Finnhub first: GET `https://finnhub.io/api/v1/calendar/earnings?symbol=SYM&from=<today−120 d>
   &to=<today+180 d>&token=…`, timeout 5 s.
   - HTTP 200 → dates from `earningsCalendar[].date`; `source = "finnhub_symbol"`.
   - Missing key, error, timeout, non-200 or unparseable body → fall back to today's FMP per-symbol call (unchanged).
   - Both unavailable → `UNKNOWN`, as today.
2. Cache: the same 24-hour per-symbol cache (`earnings_symbol_cache.json`), entry gains `"source"`.
   - Finnhub 200 is kept 24 h.
   - A Finnhub failure that falls back to FMP keeps today's TTL rules (error 1 h, answer 24 h).
3. `get_earnings_info`: a batch entry is used only when it HAS a date (`DATED`). A batch "none" for any symbol goes to
   the per-symbol lookup (the batch is a 64-name sample, not the market). This fixes the main entry path the same way
   QHM already does it.
4. Tier note in the module docstring: earnings dates are T2 from Finnhub (per symbol) with FMP as fallback.
   CLAUDE.md's data-tier table gains Finnhub on the T2 row. That is an execution-governing doc edit, so it ships as its
   own diff through the FINAL pre-ship pass.

## Expected effect
- Main entry path: names reporting within ±3 days that FMP missed now get the existing earnings rule (0.5× size if the
  daily trend agrees, else skip). Measured: of the 25 names reporting in the next 45 days, 14 are invisible to FMP
  today.
- QHM: `_next_earnings` returns `fmp`-sourced dates (the source label stays "fmp" for compatibility; the log line gains
  the provider) for every hold. That includes hold names FMP plan-blocks, which today fall to the config date or
  "unknown".
- Weekly preflight: the "earnings date unknown" list should go to about zero.

## Size / frequency / concurrency (Rule B screen)
- Size: no increase. The only size effect is the existing 0.5× earnings rule on more names, which cuts size.
- Frequency: no increase. The existing earnings rule can skip more entries near reports, which cuts entries.
- Concurrency: no change.
- QHM: the pre-earnings trim can fire on more holds; that is a sell, not added exposure.
- Verdict: not risk-path by the Rule E definition (it never raises size, entry rate or exposure). Reviewers must confirm
  independently.

## Reversal criterion
- Any Finnhub date that the company's SEC 8-K item 2.02 filing later contradicts by more than 1 day on two names in a
  month → revert to FMP-first and report.
- A Finnhub 429 storm that leaves holds UNKNOWN (visible in the WARNING lines) → add throttling.

## Failure mode
- Finnhub down → today's FMP behavior exactly.
- Both down → UNKNOWN (never "no earnings").
- No new network call on the trading thread beyond today's per-symbol lookups. The cache keeps it to at most one call
  per symbol per day, plus at most one FMP fallback.

## Not in scope
- Alpha Vantage as a second check: needs a free key signup by the owner.
- SEC EDGAR 8-K history as the after-the-fact check (not an approved tier yet).
- Earliest-date-wins across providers when they disagree (only one provider answers per lookup in this diff).

## Required questions
(answered in the board / GAI / Codex review section below)
