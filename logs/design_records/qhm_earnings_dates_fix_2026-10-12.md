# QHM holds have no earnings dates — fix design (2026-10-12, Claude)

**CEO (2026-10-12):** "We need to evaluate our T1 target for LLY."

## Findings (verified on OCI 2026-10-12)
- `data/fmp_client.py` `get_cached_earnings_dates` returns [] for LLY, NVDA, GE, GEV, GOOGL: a fresh
  `data/cache/earnings_week.json` answers "no earnings" for any symbol it never scanned (`cache.get("data", {}).get(sym,
  [])`). Writers: `events/earnings_fetcher.fetch_upcoming_earnings` (WATCHLIST + 23 names, 14 days; main.py, run_movers)
  and `earnings_preflight.py` (tracked symbols, 10 days) — non-atomic whole-file overwrites, last writer wins.
- Direct FMP: GE reports 2026-10-20; GOOGL 2026-10-28 (calendar); LLY `/stable/earnings?symbol=LLY` -> HTTP 402 (plan)
  and LLY is not on the calendar for 10/12-11/10. QHM config dates are partly stale (NVDA 08-19, GOOGL 07-28 past;
  GE 10-21 vs FMP 10-20; LLY 10-29 unconfirmed).
- Effects: QHM's pre-earnings trims (`_maybe_earnings_trim`, 21-day window) and earnings-hold gate never fire for these
  holds; the entry earnings gate (`entry_logic.py` ~L1035) does not see them. Stops stay in place.
- **The earnings-hold gate CANCELS the protective stop** 5 days before earnings (`_maybe_enter_earnings_hold`, QHM
  ~L2105-2163) and resubmits it only at startup. Restoring dates would cancel GE's stop ~10/15 and (with a config date)
  LLY's ~10/24 — LLY is 2 shares = ~92% of equity.
- LLY's T1 trim target: no options read (no date) -> fallback 6.25 x 14-week ATR = +41% (T1 $1,723.64 vs price
  $1,180.20, avg $1,222.12): effectively unreachable.

## BGG consensus (board Thorp + McKinney, GAI after a counter-prompt; Gro unavailable — daily cap)
- **D1:** three states per symbol — DATED / NONE_IN_WINDOW / UNKNOWN — with the scan window stored per symbol; a caller
  needing a longer horizon (QHM 21 days) treats a shorter scan as UNKNOWN. HTTP 402/403 -> UNKNOWN, never "none".
- **Cache integrity:** one locked (fcntl), atomic (tmp -> os.replace), per-symbol MERGE write for both writers; failed /
  negative per-symbol lookups cached 24 h (FMP budget 250/day; QHM runs every 5 minutes); a stale cache re-preloads once,
  not a per-symbol fan-out.
- **D2:** QHM + Forever-6 holds added to the scan; lookahead >= 35 days; preflight merges, never narrows.
- **D3:** 402/403 and UNKNOWN for a held symbol: WARNING + one Slack page per symbol per day.
- **D4 (2-1, Thorp + GAI over McKinney):** a QHM config `earnings_exit_before` date is used only if today-or-later,
  tagged source=config, and may drive the entry gate and the trims ONLY. **A config-only date never cancels a stop**;
  UNKNOWN keeps the stop and pages.
- **Risk-path** (default-to-risk-path: restoring dates activates a stop-cancel across a binary event). **The stopless
  earnings window itself needs the CEO's ratification before it can fire;** until then the fix ships with the
  pre-earnings stop-cancel DISABLED (flag), so stops stay on.
- Tests: scanned-empty vs unscanned; short window vs 21-day horizon -> UNKNOWN; preflight keeps main's symbols; atomic
  write; <= 1 live call per symbol per 24 h; 402 -> UNKNOWN + one WARNING; past config date -> UNKNOWN.
- Post-live proof: one INFO line per hold per day `earnings sym=... next=... src=fmp|config|unknown horizon=...`.
- Open, separately (CEO): the T1 fallback (Thorp: ~1x the stock's average absolute earnings-day move over its last 8
  prints instead of 6.25 x weekly ATR) and LLY's concentration into its report (a fractional trim is a CEO call).
