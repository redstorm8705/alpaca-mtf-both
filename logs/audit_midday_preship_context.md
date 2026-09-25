GROUND-TRUTH FACTS for the midday_audit.py diff (verified at source by the author, 2026-09-24):
- midday_audit.py runs once per weekday at 13:30 ET = 10:30 PT via cron (scripts/cron_tz_wrapper.py 13:30) — MID-session,
  a separate short-lived process on the production host (Python 3.10). Read-only: it reads logs, trade_events.jsonl,
  trade_log.json and Alpaca REST (positions, fills, orders); calls Gemini; writes logs/midday_audit_<date>.json and
  logs/midday_gemini_<date>.txt; posts one Slack card (legacy text fallback). Not imported by the trading bot.
- reporting/broker_ground_truth.py is the shipped module (PR #381/#383): collect(day, now) evaluates the session up to
  `now` (design rules keyed off the real close); card_alarms() returns (critical/high findings, lows text); render(),
  checked_summary(), naked_claims_on_cleared() as in nightly. collect() never raises; UNKNOWN on read failure or a fill
  racing its snapshot.
- Day-tier positions are protected by OCO orders whose STOP is a leg (nested=true). The old midday check read
  /v2/orders?status=open WITHOUT nested=true and posted CRITICAL "NAKED POSITION" for AMZN (09-23) and GOOGL (09-24);
  the ground truth classes both COVERED.
- Core (multi-day swing; the internal tier key is still "intraday") entries before 15:30 ET have no broker stop until the pre-close sweep (~15:45-15:52 ET = 12:45-12:52 PT);
  at 13:30 ET they are live software-stop-only exposure. The core tracker persists each open trade in trade_log.json
  `open` list with fields symbol, direction, stop, trail_stop, stop_breached (verified on the production file).
- mtf_bot.log timestamps are UTC (verified: last line 21:40:15 when UTC was 21:56).
- Design changes requested in review, all applied:
  collect retries, degraded fallback naming uncovered symbols at HIGH, software-stop effectiveness check (self-report →
  CRITICAL; mark beyond tracker stop → HIGH, CRITICAL if stop_breached), midday prompt exception (a live self-reported
  stop/exit failure belongs in CATASTROPHIC), escalation applied to the FINAL card verdict, footer lines, GT block in
  the legacy text path, UTC log window, report schema tag. Resolved-earlier NAKED stays critical.
- 80 tests pass on the production host (midday + module + nightly suites). A full dry run of main() on production data
  for 2026-09-24 (Slack/Gemini disabled): "broker stop check=OK alarms=0", report written, legacy card printed.
REVIEW STANDARD: quote the changed line for every finding; blocking = concrete input → wrong output; nits separate.

REVISION 2:
- R2 measured on production: 5,707 / 5,466 / 5,836 mtf_bot.log lines in the 09:30-13:30 ET window on 09-22/23/24, so the
  2,000-line tail misses most of the session. scan_self_reports() now does its own UNCAPPED pass over mtf_bot.log,
  only for held BY-DESIGN symbols, only lines at/after the holding's entry_time (trade_log.json; else the session open),
  and only WARNING/ERROR/CRITICAL lines carrying a stop/exit/close/protect word AND a fail/unprotected/naked word.
- R1: uncovered_now_findings(): a held position classed COVERED but uncovered at the check moment (within the 2-min
  tolerance) → HIGH "re-check"; CRITICAL if a stop order for it was REJECTED and it is not core-only.
- Compliance ignores symbols with a self-report (the midday prompt permits quoting them); a note when the degraded
  snapshot also fails; the prompt block lists the symbols held right now; footer "checked through HH:MM PT" = the end
  of the evaluated window.
- 85 tests pass on the production host, including a main() test of the legacy text path.

REVISIONS 3-8 (each prompted by a reviewer-found failing input):
- held_symbols(gt, positions) = ground-truth symbols with uncovered_at_close (window ends at its own later snapshot, so
  this means "held with no full resting broker stop at the check") UNION the live positions read. The live read may only
  ADD (it is taken BEFORE the ground truth, which retries ~1 min). Positions read None + GT OK adds a HIGH alarm.
- uncovered_now_findings also covers SOFTWARE-ONLY-BY-DESIGN whose owners are not only core (an earlier closed core
  round trip can class the symbol BY-DESIGN while a later day-tier holding is uncovered).
- Self-report scan floor = max(tracker entry_time, TODAY's 09:30 ET open) — a carried position's prior-evening repaired
  failure (SNOW 2026-08-10, broker stop resting at the check) raised a false CRITICAL before. A self-report is CRITICAL
  when the GT shows uncovered_at_close, else HIGH (never dropped) and the mark check still runs (it is CRITICAL on a
  tracker-recorded breach) — a self-report can never LOWER an alarm.
- Lines matching fail-closed|entry blocked|price sanity are excluded: production grep shows these templates are only
  entry-side ("No daily data — entry blocked (fail-closed)", "PRICE SANITY FAIL … skipping entry"). The real exit-failure
  line "[META] #12c exit order submission failed — skipping entry." is still matched (tested).
- Tests: a module-wide setUpModule guard blocks ma.SLACK_WEBHOOK and audit_slack.post_to_slack (a test had posted a real
  card through the worktree .env webhook). 30 midday tests; 95+ across the three ground-truth suites on the OCI py3.10 venv.
- Replays on production (adversarial reviewer): AMZN 09-23 / GOOGL 09-24 at 13:30 ET → no alarm; SNOW 08-10 → no
  false CRITICAL; META 08-31 #12c → HIGH self-report. Only HTTP GETs to Alpaca; no order placement.
