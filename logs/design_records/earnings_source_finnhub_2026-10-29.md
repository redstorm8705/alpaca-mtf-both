# Earnings dates from Finnhub — EARN-SOURCE-1029 (signed: Claude, 2026-10-29) — DRAFT v4, not approved

Approval-ID: EARN-SOURCE-1029
Covers: data/fmp_client.py, run_earnings_warm.py, tests/test_earnings_source.py, server-ops

Owner, 2026-10-29: "We need full earnings data and scheduled reporting dates. There are a ton of sources available
online. This shouldn't happen." ... "Need something that has the full list."

## Problem (measured at source, logs/research/earnings_sources_2026-10-29.md)
- Every bot consumer of earnings dates goes through `data/fmp_client.py`:
  - QHM `_next_earnings` → pre-earnings trim, stop decision;
  - the main entry path `entry_logic.py:1033`;
  - `earnings_preflight.py`;
  - `scripts/qhm_report.py`.
- On our FMP plan:
  - per-symbol `/stable/earnings` returns HTTP 402 for 19 of 37 tested names (NOW, AVGO, MU, SMCI, SNDK …);
  - `/stable/earnings-calendar` covers only a fixed ~64-symbol sample (11 of our 25 reporters in the next 45 days);
  - the v3 endpoints return 403.
- `get_earnings_info` trusts a fresh batch "none" for any scanned symbol, so for most names the bot reads "no earnings"
  when the batch simply does not cover them.
- Real case: NOW reported 2026-10-28 after the close with no date in the bot. Finnhub per-symbol returns
  `2026-10-28 amc` for NOW (live call, 2026-10-29).

## Measured coverage (37 names; 25 report in the next 45 days)
| Source | Next date found | 45-day reporters found | All of NOW/AVGO/MU/SMCI/SNDK | Matches SEC 8-K, 10-20..10-29 |
|---|---|---|---|---|
| Finnhub per symbol | 37/37 | 25/25 | yes | 17/18 (SMCI's unscheduled 10-20 filing: no source had it) |
| FMP calendar (batch) | 11 | 11 | none | 13/18 |
| FMP per symbol | 18/37 (402 for the rest) | — | none | — |
- Finnhub free plan: 60 calls/min. `FINNHUB_API_KEY` is already in `.env` and used by `events/news_monitor.py`.
- Its date-range mode truncates at 1,500 rows, so only per-symbol mode is used.

## Change — v4 (revised after review rounds 1-3)
### data/fmp_client.py
1. `_symbol_lookup(sym)` asks Finnhub first: GET `https://finnhub.io/api/v1/calendar/earnings` with request params
   `symbol`, `from` = ET date − 120 d, `to` = ET date + 180 d, `token`. Timeout 4 s.
   - **Covered** = HTTP 200 with ≥ 1 row whose `symbol` equals the requested one or its entry in an explicit alias map
     (`{"BRK.B": "BRK.A", "BRK-B": "BRK.A"}`; BRK.B comes back as BRK.A, measured live). Dates come from
     `earningsCalendar[].date`, and `hour` (bmo/amc) is stored. `source = "finnhub_symbol"`.
   - Rows for any other symbol are dropped; if none remain, the answer is not covered. Requested and returned symbols
     are logged (Codex r2 #4).
   - **Not covered** falls back to today's FMP per-symbol call. This covers HTTP 200 with an empty list (ETFs, unknown
     symbols), errors, timeouts, 429s and bad bodies.
   - The FMP fallback gives its own 200 answer (`fmp_symbol`), or `UNKNOWN` on 402/403/error.
   - An empty Finnhub list never becomes `NONE_IN_WINDOW` by itself (Codex #1, McKinney #2).
   - `NONE_IN_WINDOW` only when a covered answer has dates and none is upcoming.
2. Dates are deduped and **sorted nearest-first by distance from the ET date** in every returned answer.
   - Why: Finnhub lists newest first, including two future quarters (live: NOW 2027-04-20, 2027-01-26, 2026-10-28).
   - `entry_logic.py:1037` reads `[:2]`, so without the sort it misses yesterday's report (McKinney #1, Harris #1).
3. TTL:
   - a covered Finnhub answer is kept 24 h;
   - any lookup where Finnhub failed is kept 1 h, whatever FMP said. A transient 429 plus an FMP 402 is never a
     24-hour blind spot (McKinney #3, Codex #2).
4. Cache schema `v: 2`. Entries without it are stale, so pre-deploy FMP-402 entries are re-asked at once (Codex #5,
   McKinney #4). Each entry stores:
   - `provider` and `providers_tried`;
   - the HTTP statuses;
   - the query window;
   - `hour`;
   - `fetched_at_utc`, taken after the request completes.
5. **Precedence** (Codex r2 #1), the same in `get_earnings_info`, `get_symbol_earnings_info` and
   `get_cached_earnings_dates`:
   1. a fresh v2 per-symbol `DATED` answer;
   2. a fresh batch entry that has a date;
   3. a fresh v2 per-symbol `NONE_IN_WINDOW`;
   4. `UNKNOWN` (`[]` for the list function).
   A batch "none" never answers by itself. A per-symbol `UNKNOWN` never hides a dated batch answer.
6. **Network calls in the trading process are bounded** (QHM runs inside `run_cycle`, `strategy/run_cycle.py:1971`;
   today it already makes a live FMP call, 5 s timeout, on a cache miss).
   - `get_cached_earnings_dates` (the main entry path) is cache-only and never calls a provider.
   - `get_earnings_info` / `get_symbol_earnings_info` call a provider only when no v2 entry exists, or the entry is
     older than its TTL.
   - In the main bot process they make at most ONE such live lookup per 5 minutes. The timestamp check-and-reserve
     runs under a process-local `threading.Lock` before any network I/O, with a two-caller barrier test (Codex r3 #2).
   - When the budget is spent they return `UNKNOWN`, never an expired entry (Codex r3 #3).
   - Every returned answer carries:
     - `provider`;
     - `fetched_at_utc`;
     - `fresh` (bool);
     - `record_id` = first 12 hex of the sha256 of the cache entry.
   - Worst case added to one `run_cycle`: one lookup = Finnhub 4 s + FMP 5 s = 9 s, versus 5 s per miss today with
     no cap. The warmer keeps entries fresh, so this path is the exception (Harris #2, McKinney #6, Codex #6, r2 #3).
7. New `warm_symbol_earnings(symbols)` refreshes every stale symbol, in its own process, never in the trading process:
   - Sequential, at most 1 Finnhub call per second (news_monitor shares the key at ~1 call per 2 min).
   - **429 sequence** (Codex r2 #2):
     - the symbol that got the 429 gets its FMP fallback at once and is cached with a 1 h TTL;
     - the warmer then waits `Retry-After` (cap 60 s) once and continues;
     - a second 429 in the same run stops Finnhub calls for that run, and the remaining symbols get the FMP fallback
       only (1 h TTL).
   - Ends with a summary line: `EARNWARM summary attempted=… finnhub=… fmp=… unknown=… not_attempted=… r429=…`.
### run_earnings_warm.py (new; cron via server-ops)
- Symbols:
  - the ownership ledger (finite, non-zero qty);
  - `config.WATCHLIST`;
  - the Day-tier and Swing universes;
  - QHM state + picks;
  - `FOREVER6_UNIVERSE`.
- Logs one line per symbol: `EARNWARM sym=… returned_sym=… provider=… tried=… statuses=… state=… next=… hour=…
  window=… reason=…`.
- Runs weekdays 08:30, 09:15 and 12:30 ET, plus Sunday 17:45 ET (before the 17:55 ET preflight), through
  `scripts/cron_tz_wrapper.py`.
- The Finnhub request:
  - query params only, with the token as a param as `events/news_monitor.py` does;
  - timeout 4 s;
  - HTTP 429 is read from the status code, with `Retry-After` parsed as seconds (default 60, cap 60);
  - any exception counts as not covered.
- Cache read-modify-write stays inside `_earnings_lock()`, as today (`fmp_client.py` L500-503 reads the cache inside
  the lock before the atomic replace).

## Consumer behaviour (traced independently by both board seats)
- **Main entry path** (`entry_logic.py:1033-1088`): within ±3 days (before or after) of a report it trades 0.5× if the
  daily trend agrees, and skips otherwise.
  - Measured: its last EARNINGS SKIP/ALIGNED line on the server is 2026-08-05 (55 such lines in all of `mtf_bot.log*`).
  - October entries are Day tier (33) and Swing (5), and neither uses this path.
  - Trade-count effect today is about zero.
  - If the path is reactivated, the owner decides "halve, never skip" in a separate entry-rule diff.
- **QHM**: real dates for holds FMP plan-blocks.
  - The pre-earnings trim fires only on a gain (L3699).
  - The stop-cancel path is OFF (`QHM_EARNINGS_STOP_CANCEL_ENABLED = False`).
  - Owner direction 10-29: stops are "updated around earnings but not removed". The follow-up QHM-EARN-STOP removes the
    cancel path and labels QHM's `src` by provider (Thorp #3, McKinney #5/#7).
  - Until then QHM's own log line says `src=fmp` for any date from the fmp_client provider chain.
  - The true provider of the date QHM used is recoverable by joining QHM's `QHM earnings sym=… next=…` line on symbol
    and time with:
    - the `EARNWARM` line for that symbol;
    - the v2 cache entry (`provider`, `fetched_at_utc`);
    - a new INFO line fmp_client writes on every live lookup: `EARNLOOKUP sym=… provider=… state=… next=…
      caller_pid=…`;
    - a new INFO line fmp_client writes whenever it SERVES a `DATED` answer to a caller:
      `EARNSERVE sym=… provider=… record=<record_id> fetched=… next=… thread=…`.
      - QHM calls fmp_client and then logs its own line on the same thread, so the EARNSERVE line immediately before a
        QHM decision names the exact cache record it used.
      - This holds even if a later refresh replaces the cache (Codex r3 #1).
  - The QHM label itself changes in QHM-EARN-STOP, the next design, which already owns that file's earnings path.
  - Stop-cancel eligibility cannot be affected meanwhile, because the cancel switch is OFF.
- **Swing / Day tiers**: no earnings input (unchanged).
- **Size / frequency / concurrency**: no increase on any axis.
  - Both seats traced this; Codex counts any sizing/skip/exit change as risk-path regardless of direction.
  - It therefore goes to the owner as an entry/exit-affecting change.

## Reversal criterion and checks
- Weekly, research-only: compare each held name's date with its SEC 8-K item 2.02 filing. Two misses of more than
  1 day in a month → revert to FMP-first and report.
- Daily: UNKNOWN count for held stocks (not ETFs) in the EARNWARM log. Above zero two days running → investigate.

## Not in scope
- Alpha Vantage as a second check: needs the owner's free key signup.
- SEC EDGAR as a live tier.
- Earliest-date-wins across providers.
- The CLAUDE.md data-tier row: its own diff through the FINAL pre-ship pass, shipped with this one.

## Tests and full-path check (before the ship gate)
- Unit tests with HTTP mocked; no broker call exists in this change. Cases:
  - empty Finnhub 200;
  - alias and foreign-symbol rows;
  - 429 then FMP 402;
  - a second 429 in a run;
  - stale v1 cache;
  - expired v2 entry with the budget spent → UNKNOWN;
  - precedence of all four kinds;
  - nearest-first sort, including a yesterday/tomorrow tie (yesterday first);
  - two-caller budget race.
- Read-only probe on OCI before calling it live: run the warmer against the real ledger and universe into a scratch
  cache path, and report coverage and UNKNOWNs. Then a first-session audit of EARNWARM / EARNSERVE against QHM lines.

## Review record
- Round 1: Codex REJECT, GAI REJECT; board seats McKinney APPROVE-WITH-CHANGES, Harris+Thorp APPROVE-WITH-CHANGES.
- Round 2: Codex REJECT, GAI APPROVE-WITH-CHANGES.
- Round 3: Codex APPROVE-WITH-CHANGES, GAI APPROVE-WITH-CHANGES.
- v4 folds in every round-3 change except one. Codex asked to change QHM's own `src=fmp` label in this diff; v4 adds
  the EARNSERVE record link instead and moves the label to QHM-EARN-STOP, the next design, which owns QHM's earnings
  code. Stop-cancel is OFF, so the label drives no action. This is the one open difference.
- GAI's round-3 items describe how the current code differs from the design (precedence order, TTL split, empty list,
  batch "none"). Each is already a stated change in items 1, 3 and 5.

## Required questions (short answers; evidence in the sections above and the review outputs)
- Q1/Q29: real dates for the 14 of 25 reporters FMP misses; it is the date layer every earnings rule needs. High value.
- Q2/Q19: trade count is about zero today (dormant entry path). It changes QHM's trim inputs, so the owner approves.
- Q5/Q6: windows and TTLs are plumbing. The forward pass: a second provider (Alpha Vantage, key needed) plus
  earliest-date and SEC scoring.
- Q11/Q12: no increase in size, frequency or exposure (two seats traced). Data tier: Finnhub added at T2.
  Stops untouched.
- Q13: fails to FMP, then UNKNOWN. Expired never acts.
- Q14: no shadow.
- Q15/Q16: EARNWARM, EARNLOOKUP, EARNSERVE lines.
- Q21: HTTP mocked; no broker calls.
- Q22/Q23: OCI read-only probe and a full-path test before live.
- Q26: T2 Finnhub per symbol → FMP → UNKNOWN.
- Q27: `data/cache/earnings_symbol_cache.json`, locked atomic replace.
- Q28: warmer in its own cron process; QHM in run_cycle with a bounded budget; the entry path is cache-only.
- Q31–Q37: not an edge claim; coverage measured on 37 names and checked against SEC 8-K filings 17/18.
- Q40: protects "a known report is never read as no report". Check: weekly SEC 8-K comparison and the daily UNKNOWN
  count.
- Q44: independent proof is the SEC filings.

## Required questions
(answered in the board / GAI / Codex review rounds; see the scratchpad review outputs)
