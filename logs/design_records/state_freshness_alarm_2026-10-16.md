# State-file freshness alarm — design (2026-10-16, Claude)

**Trigger:** handoff 2026-10-16 pick-up item 5, owner task 2026-10-16. `data/state/dip_zones.json` (QHM dip-add
zones) and `data/state/overnight_k.json` (dynamic overnight cap K) are written by weekday crons and read by live code.
If a cron silently stops, the readers keep the last file (by design: "last good value stays") and nothing pages.

## Facts read at source (not guessed)
- `execution/dip_zones.py` `refresh()` writes `{"session": f"{datetime.now(ET):%Y-%m-%d}", "computed_at": <PT iso>,
  "zones": ...}`; on failure it writes nothing (the old file stays). Reader `zones_for()` never checks the date.
- `execution/overnight_capacity.py` `refresh()` writes `{"session": <ET date>, "computed_at": <PT iso>, "k_dyn", ...}`;
  on failure nothing is written. Reader `overnight_k()` only logs a WARNING (once per day) when `session != today`.
- Crons: `20 12,21 * * 1-5` (dip zones), `15 12,21 * * 1-5` (overnight K), server clock UTC.

## Design (owner-specified; no open fork)
- Script `scripts/state_freshness_check.py`, page-only, read-only. Never writes state, never touches orders.
- **Stale = at least one COMPLETED trading session (close has passed) strictly after the file's `session` date.**
  That is the owner's "older than one completed trading session". Example: the morning check on Thu finds
  `session = Wed` (Thu morning run failed, Wed runs fine) -> not stale; `session = Tue` -> stale (Wed completed
  with no refresh in either Wed run nor Thu morning).
- Calendar: Alpaca trading calendar via `execution.broker.get_trading_client().get_calendar(...)` (read-only, T1,
  holiday + half-day aware; same call as `scripts/post_live_eval.py`). Unreadable -> weekdays strictly before today
  count as completed sessions (a holiday then counts: may page early; today never counts, so for a run BEFORE today's
  close — the installed cron — never late; a fallback run after the close sees today one run later). An empty
  calendar reply is treated as unreadable. No fixed hour thresholds.
- Missing / unreadable / unstamped file -> page.
- Slack via `alerts.send_slack`, one plain-English message listing every stale file, what it controls, what the bot
  is doing meanwhile, and which log to check. Exit 1 when anything is stale.
- Cron `scripts/install_state_freshness_cron.sh`: idempotent, mirrors `install_dip_zones_cron.sh`; weekdays
  `50 12 * * 1-5` UTC (30 min after the later 12:20 refresh, before the 13:30/14:30 UTC open). Not installed by
  this PR.

## Anti-silo note
Signal = "a state input is stale". Other paths that could use it: the readers themselves (`zones_for()` /
`overnight_k()` could fail closed or tighten on stale data). Not wired here: that changes trading behaviour
(risk-path / fork), so it needs board + Gro/GAI. Logged as a proposal in `logs/design_records/co_audit_2026-10-16.md`.

## Gate
Not risk-path (no size / frequency / concurrency change, no order path). Board: cold Claude review in-session.
Gro/GAI: no design fork to vote on; the exact diff gets the server-side Gro+GAI audit in the PR's preship CI.

## Review round 1 (2026-10-16): cold-2nd FAIL + adversarial FAIL, all fixed
- A future-dated `session` (clock error / damaged file) read as fresh forever -> now pages "in the future".
- An empty calendar reply (no rows, no exception) read as "no sessions" -> now uses the weekday fallback (same guard
  as `scripts/post_live_eval.py` `sessions()`).
- "Never late" was overclaimed for the fallback: it holds for a run before today's close (the installed 12:50 UTC
  cron); a fallback run after the close sees today one run later. Docstring corrected.
- Tests added: non-object JSON, string calendar fields, a calendar row on/before the session, the weekend skip in the
  fallback, plural wording, the broker-refused path actually reaching the fallback.
- Known limit (documented, not built): `session` is the run date, not the data date. If only the evening runs
  succeed for days, the data stays one session behind and the check stays quiet. Fixing it needs a data-date field in
  the writers (`execution/dip_zones.py` is owned by another session) — proposal for a later diff.

## Review round 2 (2026-10-16): cold-2nd PASS; adversarial FAIL on this record only (the "never late" sentence above
was unqualified) — corrected. Code unchanged in round 2.
