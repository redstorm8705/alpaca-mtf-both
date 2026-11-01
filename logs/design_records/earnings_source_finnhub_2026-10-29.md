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

## Required questions (one line per question, RQ-4)
- Q1: Gives real report dates for the 14 of 25 reporters FMP misses; every earnings rule (QHM trim, Swing, Day) depends on this date layer, so it serves data quality and growth.
- Q2: No change to trade count today; the entry path that reads dates is dormant, and no tier loses trades.
- Q3: It should exist because FMP alone misses most dates; the simplest fix is a second per-symbol provider with FMP as fallback, not a new strategy.
- Q4: Builds on earnings_preflight (PREFLIGHT-LEDGER-1029, PR 801) and fmp_client; no open PR or Codex session covers Finnhub dates; QHM label change moves to QHM-EARN-STOP.
- Q5: No decision thresholds; cache windows and time-to-live values are plumbing, and the dates themselves come from provider data.
- Q6: Exposes the single-provider assumption; forward pass is a third provider plus earliest-date scoring checked against SEC 8-K filings.
- Q7: Feeds QHM earnings trim, Swing earnings handling, Day-tier report mornings and the Sunday preflight warning; each reads the cache and treats missing as UNKNOWN.
- Q8: Coverage gap measured on 37 names in logs/research/earnings_sources_2026-10-29.md; Finnhub dates matched SEC 8-K filings in 17 of 18 checks.
- Q9: Tested against the 10-29 reporting week and 37 names; expected effect is fewer UNKNOWN dates; it is wrong if weekly SEC comparison shows mismatched dates.
- Q10: Not an edge question; this is a data source fix with no calibration being held back.
- Q11: Not risk-path: two board seats traced size, frequency and concurrency as unchanged; dates only feed existing rules.
- Q12: Safety envelope untouched; stops, kill switch, paper flag unchanged; Finnhub is added as a tier-two data source.
- Q13: Finnhub failure falls back to FMP, then to UNKNOWN; an expired cache entry never triggers an action.
- Q14: No shadow component; the warmer and lookups are acted on as soon as they ship.
- Q15: Every lookup logs EARNLOOKUP with provider, date and age; the warmer logs EARNWARM; consumers log EARNSERVE.
- Q16: EARNWARM and EARNSERVE lines on the first trading day prove it ran; post-live audit compares served dates with SEC filings.
- Q17: An OCI read-only probe runs the warmer and a lookup against the real cache before go-live.
- Q18: No anomaly is explained away; FMP missing dates was investigated and measured rather than accepted.
- Q19: It changes QHM trim inputs, so the owner approves this exact design before any commit.
- Q20: Review prompts went through the bias gate; board, GAI and Codex reviewed three rounds with neutral prompts.
- Q21: No broker calls at all; tests mock every HTTP request and the broker refuses under test.
- Q22: Yes, a read-only run on the OCI server against the real cache and providers precedes go-live.
- Q23: A full-path test from warmer through cache to QHM and preflight runs before calling it ready.
- Q24: Provider coverage claims were checked live in the 10-29 probe, including the FMP-blind names.
- Q25: No market statistics are quoted; coverage counts were measured directly from provider responses.
- Q26: Tier two: Finnhub per-symbol calendar first, FMP second, then UNKNOWN; no yfinance involved.
- Q27: Writes data/cache/earnings_symbol_cache.json with a file lock and atomic replace.
- Q28: Warmer runs in its own cron process; QHM reads within run_cycle under a bounded budget; entry path reads cache only, never blocking check_exits.
- Q29: Ranks high because every earnings rule and the QHM-EARN-STOP design depend on correct dates.
- Q30: Standard frameworks keep a verified corporate-events calendar with source reconciliation; our audits underweight silent date gaps, which this closes.
- Q31: Not an edge claim, so no walk-forward test applies; correctness is checked against SEC filings.
- Q32: No replay involved; dates are compared with filings after the fact, so look-ahead does not apply.
- Q33: No trading cost involved; only API call budget, which the warmer spreads across the day.
- Q34: Not a return source, so alpha versus beta does not apply to this data layer.
- Q35: Behaves the same in every regime; a provider outage degrades to UNKNOWN, never a false date.
- Q36: Per-symbol calls scale with the universe size, not account size; free-tier rate limits fit the current universe.
- Q37: Not an edge; accuracy is tracked as matched dates over SEC checks, currently 17 of 18.
- Q38: Decay measured as rising UNKNOWN count or SEC mismatches; then the provider is replaced through a new design.
- Q39: Sizing is not touched; existing sizing keeps using Alpaca fills.
- Q40: Protects that a known report is never read as no report; checked weekly against SEC 8-K filings and the daily UNKNOWN count.
- Q41: An expert would reconcile several providers against official filings automatically, which the forward pass adds.
- Q42: Rawest data is SEC 8-K filings and provider per-symbol calendars; the design uses the per-symbol calendars directly.
- Q43: Professional method is multiple vendors with reconciliation to official filings; this design starts that with two vendors plus SEC checks.
- Q44: Independent evidence is SEC filings, not the author's own checks.
