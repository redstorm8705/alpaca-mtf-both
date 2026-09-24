GROUND-TRUTH FACTS (verified at source by the author) — REVISION 5 of the broker-ground-truth diff.

DESIGN CHANGE IN THIS REVISION: revisions 1-4 let code DOWNGRADE LLM findings that claimed "naked" when broker
data contradicted them. Four cold reviews in a row found inputs where that prose-parsing downgrade hid a real
catastrophic claim. Revision 5 REMOVES all downgrading: nightly_audit._apply_suppressions is byte-identical to
production. The broker ground truth is now used only (a) as prompt context with an explicit rule that stop coverage
is owned by code, and (b) to ADD deterministic card findings and RAISE the card verdict (_escalate_card_verdict:
critical -> FAIL; high -> PASS/UNKNOWN becomes WARN; low changes nothing). It can never remove, hide, or lower an
LLM finding or verdict. Trade-off accepted: if the LLM ignores the prompt rule, a false "naked" claim stays visible.

RUNTIME
- nightly_audit.py runs once per weekday at 16:05 ET via cron, a separate short-lived process on the production
  host (Python 3.10). Not imported by the trading bot; never on the trading thread. Reads logs + Alpaca data, calls
  Gemini, writes logs/gemini_audit_<date>.txt, posts one Slack card.
- reporting/broker_ground_truth.py (new) is read-only: HTTP GETs to the Alpaca paper trading REST API through
  reporting.pnl_ledger._get_json (retries 429/network errors with backoff, raises on other HTTP errors, 30 s timeout
  per call). Its only caller in this diff is nightly_audit.main(), wrapped in try/except.
- mtf_bot.log timestamps are UTC; main.py logs one "[CYCLE] duration=" line per loop cycle; RTH gaps between them
  2026-07-19..09-23: median 6.8 min, p99 10.6, p99.9 14.2, max 43.5. The log is not rotated.
- client_order_id prefixes: "IN-" core intraday, "DT-" day tier, "QH-" quarterly holds; OCO stop legs carry a UUID
  and are nested under "legs" with nested=true. Pre-close sweep DAY stops carry "IN-" (verified in order history).
- Core design: broker GTC stop at entry only for entries >= 15:30 ET; earlier entries are software-stopped each cycle
  until the pre-close sweep places a DAY stop (~15:45-15:52 ET observed); carried core positions get a DAY stop on the
  first cycle after the open (observed 09:34-09:41 ET).
- execution/stop_protection.py "broker-held N" = placements the broker REFUSED because protection already held the qty.

EVIDENCE (production host, py3.10)
- 45 unit tests pass (tests/test_broker_ground_truth.py, tests/test_nightly_audit_ground_truth.py).
- collect() over 15 real sessions 2026-09-02..09-23: alarms only on verified real events (09-18 UBER 3.5h lapse after
  a failed breakeven-push resubmit; 09-15 AAPL/HOOD 7-min exit gap; 09-11 GEV QHM stop gone 64 min + a real 43-min
  bot stall; 09-03 MARA 5 min; 09-02 META day stop at 10:02).

REVIEW STANDARD: quote the exact changed line for every finding; a blocking finding needs a concrete failing input ->
wrong output. Theoretical concerns go in a separate non-blocking NITS list.

REVISION 6 additions (after r5 reviews):
- Prompt: on an UNKNOWN / unavailable ground-truth day the LLM MAY report a naked position the bot's own log states
  (quoted, tagged "broker-unverified") — never silenced (masked-loss seat R5). Bot self-reports of stop failures for
  COVERED / BY-DESIGN symbols go to NEW BUGS as code-path defects. The LLM is told not to repeat NAKED / GAP / LAPSED
  symbols (code posts them).
- classify: a fresh core holding opened at/after 15:30 ET (close − 30 min) has no by-design window (entry_logic
  places a GTC at entry from 15:30).
- Detect-only compliance counter: naked_claims_on_cleared() finds LLM naked claims on COVERED / BY-DESIGN symbols;
  nightly records one line per run to logs/gt_compliance.jsonl and shows "N of last 5 sessions" in the footer + report
  file + log (reversal criterion >= 2). It never changes a finding or verdict.
- card_alarms(): only critical/high ground-truth alarms enter the card's findings (capped at 8 with a "+N more"
  line); lows go to a footer line (render_card collapses > 2 lows into a bare count, which would hide LLM lows).
- collect(): fills are read before and after the positions snapshot; a count change → UNKNOWN.
- 54 tests pass on the production host.

REVISION 7 additions (after r6 cold-2nd):
- Carried-position owner = tiers of same-side stops ALIVE OVERNIGHT (placed at/before the open, still resting within
  the 12h before it); if none, the union of every tier seen on that side in the prior 24h; else the first in-session
  stop's tier; none → "other". A stale expired core stop can no longer relabel a quarterly-hold position.
- classify(..., window_end=None): evaluation stops at window_end (collect passes min(close, now)); the 15:30 and 15:55
  design rules and the "held into the close" lapse key off the REAL session close only.
- _record_gt_compliance skips non-object JSON lines.
- 58 tests pass on the production host; 15-session re-scan identical (alarms only on the verified real events).

REVISION 8 additions (after the adversarial claims review):
- A BROKER-STOP-LAPSED holding with >= 60 lapsed minutes is CRITICAL (forces card FAIL): 2026-09-18 UBER lapsed 204 min
  after the bot logged "2 shares unprotected. Set manual stop in Alpaca" and previously would only have been high/WARN.
- The 15:30 GTC-at-entry rule is the WALL-CLOCK 15:30 ET (as in execution/entry_logic.py `entry_mins >= 15*60+30`),
  capped at the session close (early-close days).
- "F6-" client_order_id prefix → tier "f6" (forever-hold floor; execution/stop_protection.py excludes F6 symbols from
  stop placement by design): an f6-only holding's uncovered minutes are by design, including at the close.
  FOREVER6 is currently disabled in config.
- naked_claims_on_cleared counts only the CATASTROPHIC ALERT section (NEW BUGS is where the prompt routes quoted bot
  self-reports); the footer always shows the violation count when the ground truth is OK.
- CATASTROPHIC definition wording aligned with the UNKNOWN-day rule.
- 60 tests pass on the production host; 15-session re-scan identical (UBER 09-18 now critical).

REVISION 9 additions (after the adversarial r8 review):
- _fetch_orders counts top-level orders PLUS legs toward the 500 page limit (_page_size): a live page returned
  494 orders + 6 OCO legs, which the old len(batch) < 500 test mistook for the last page (history silently stopped at
  2026-08-14). Verified live after the fix: 2,871 orders back to 2026-04-06 (account start).
- render() UNKNOWN text now matches the prompt rule: report UNKNOWN, never assert protected; a naked position may be
  reported only when the bot's own log states it, quoted and tagged broker-unverified. Tested through
  collect() → UNKNOWN → render → _build_prompt.
- 62 tests pass on the production host; 15-session re-scan identical.

REVISION 10 (after cold-2nd r9 PASS with a nit): _fetch_orders now ends ONLY on a page that brings no new order ids
(the overlap page after the last real page, or an empty page); a full page with no new ids still raises (UNKNOWN).
It no longer infers the end from a short page. Non-dict entries are skipped. Live: 2,871 orders back to 2026-04-06.
63 tests pass on the production host.

REVISION 11 (after adversarial r9 found C10 false): Alpaca's `until` filters on submitted_at (verified live: AVGO
stop fa13569f created 2026-07-08 06:00:35.72 / submitted 08:00:45.12 is absent with until=06:00:38.503 and present
with until=08:00:45.200). _fetch_orders now advances its cursor from the page's last order's submitted_at (+1 ms),
falling back to created_at. Live cross-check after the fix: the backward pager's ids ⊇ an independent forward walk's
ids (0 missing; the 6 extra are currently-open orders created after that session's close, merged on purpose).
64 tests pass on the production host.
