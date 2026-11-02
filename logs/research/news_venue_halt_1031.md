# NEWS-HALT-WORDS-1031 + VENUE-CLOSE-1031 — design record (draft, owner approval pending)

Approval-ID: NEWS-VENUE-HALT-1031
Status: draft — investigation complete, gate not yet run

## Problem (from post-live issue #806, session 2026-10-30)
1. **False news halt.** `events/news_monitor.py:448-451` classifies headlines with a plain substring test
   (`k in lower`). On 2026-10-30 a PG&E headline containing "Inter**national Emergency** Management" matched the HALT
   phrase "national emergency", and "origi**nato**r" matched CAUTION "nato". `has_active_halt_keyword()`
   (news_monitor.py:1691-1699) → `strategy/run_cycle.py:1191` → early return at run_cycle.py:1957 blocked all
   QHM / Swing / intraday entries for six cycles (16:18–16:45 UTC). Day tier was unaffected (separate runner). Stops
   and exits ran normally. Trading impact that day: none measured (QHM was cap-blocked anyway), but on a day with room
   it blocks real entries for ~30 minutes on a false read.
2. **False venue halt at the close.** A cycle that started 15:59:50 ET passed the market-open check; its
   `get_clock()` (run_cycle.py:1286) returned at 16:00:25 with `is_open=False`, and `_venue_real_halt =
   (not _venue_is_open) or …` (~L1300) treated the normal close as a halt and fired a CRITICAL `alert_venue_halt`
   (L1342). Same event on 2026-08-07 20:00:16 UTC. No trading impact (market closed).

## Proposed fix
1. Whole-word keyword match in `_classify`: `re.search(r"\b" + re.escape(k) + r"\b", lower)` for HALT, CAUTION and
   MONITOR lists. Multi-word phrases still match as phrases. Replay against the last 30 days of classified headlines
   (logs) before ship: list every headline whose classification changes, and confirm none is a real emergency.
2. Venue verdict: when the clock response says `is_open=False` and its `timestamp` is at or after today's scheduled
   close (`next_close` already passed / calendar close), skip the venue-halt verdict and alert for that cycle.

## Replay (2026-07-19 .. 10-31, 103 days, 19,304 unique logged headlines; /tmp/kwreplay on the server)
- 15 HALT headlines; none was a real US market emergency. 29 RTH cycles in 6 episodes were blocked (07-24, 08-13,
  08-14, 09-02, 09-11, 10-30) — all 29 false positives.
- Whole-word matching alone clears 9 of 29 (PG&E 6, Workday 3). The other 20 come from exact phrases: "trading halt"
  (single-stock halts, e.g. Scorpio Gold 6 cycles) and "systemic risk" matched in DESCRIPTION text (14 cycles).
- 8 of 15 HALTs matched on description text the log does not keep, so they cannot be fully replayed.
- Display-only tiers: ~1,500 CAUTION/MONITOR labels change, mostly noise ("ppi" in shipping/Philippines, "nato" in
  senator, "iran" in Kiran, "election" in selection).
- Plural/inflection losses under whole-word matching are covered by an optional plural suffix `(?:s|es)?` plus explicit
  entries (russian, iranian, crashed, collapsed, congressional, interest rates, rate hikes, ...).

## Revised proposal
1. Whole-word match with optional plural for all three keyword lists, plus the explicit inflected entries.
2. Remove "trading halt" and "systemic risk" from HALT (10 of 15 HALTs, 20 of 29 blocked cycles, zero true positives);
   a real market-wide halt is caught independently by the Alpaca venue-state check.
3. Match HALT keywords against the headline title only.
4. Log the matched text snippet so future replays are complete.
5. Venue-close guard (alert-only), as above.

## Routing (Rule B screen, to be verified independently by the board)
Fix 1 removes false halts, so it can increase entry FREQUENCY on affected cycles → treat as risk-path by default
(board + masked-loss seat + GAI + Codex), plus owner approval (entry behavior change).
Fix 2 is alert-only after the close → not risk-path.

## Reversal criterion
A real emergency headline that the substring match would have caught but the whole-word match misses (replay must
show zero), or any venue halt during RTH that the close guard suppresses (guard only applies at/after scheduled close).
