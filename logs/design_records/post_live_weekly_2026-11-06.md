# POST-LIVE-WEEKLY-1106 — weekly review of the post-live evaluator (design record)

Approval-ID: POST-LIVE-WEEKLY-1106
Status: rev 7 FINAL DRAFT for owner approval (board, GAI, Codex rounds folded 2026-11-06); rev 1 DRAFT (signed: Claude, 2026-11-06). Owner request 2026-11-06: "We also need a weekly review of the post
live evaluator." Not yet reviewed by board + GAI + Codex.

## Why (measured 2026-11-06, read-only)

- `scripts/post_live_eval.py` runs every weekday at 22:15 UTC (OCI crontab) and writes
  `logs/post_live/results/<session>.json`, a Slack card and one GitHub issue per finding.
- Nothing reviews those outputs as a whole. **27 `post-live` issues are open** (oldest #636, 2026-10-13): 10
  `NEW_ERRORS_<date>` issues and 17 `UNEXERCISED_EXPIRED` issues. None has been closed by a root-cause fix.
- `post_live_due.py` reports **79 PRs OVERDUE** for their post-live audit and **17 done**.
- Last five sessions: 11-02 new error signatures 6, 11-03 0, 11-04 1, 11-05 6, 11-06 8. Each day's issue lists that
  day's new signatures only, so a signature that appears on several days is never seen as one recurring problem.

The daily evaluator finds problems but nothing works them down, so the backlog keeps growing.

## Design

New script `scripts/post_live_weekly.py`, run on OCI by cron on Saturday morning (after Friday's daily run), read-only
on the market and the broker.

Inputs (all already on the server): the week's `logs/post_live/results/*.json`, the expectation records in
`logs/post_live/expect/`, `logs/post_live_audits.jsonl`, the open `post-live` GitHub issues, and the error logs the
daily evaluator reads (`logs/mtf_bot.log`, `logs/day_tier_runner_cron.log`).

The report has five sections, in plain English (owner Slack format rules SLK01-SLK12):
1. **Errors this week, grouped:** every new error signature from the week, merged across days, with the number of
   sessions it appeared in, the first and last day seen, whether it is still occurring, and the change(s) live at the
   time. Ranked by sessions seen, then by line count. The daily `NEW_ERRORS_<date>` issues are linked to the group.
2. **Code that never ran:** every `UNEXERCISED_EXPIRED` item with its age, ranked oldest first; each needs a
   historical replay that proves it fires, or a remove / kill-flag decision.
3. **Quiet issues:** open `NEW_ERRORS` issues whose signatures have not reappeared in any session since. These are
   listed as close candidates with the evidence; the script never closes an issue itself.
4. **Audit backlog trend:** overdue / done counts now vs last week's report, the oldest overdue PR, and the number
   audited this week (burn-down).
5. **Evaluator health:** a results file exists for every session of the week (missing → that daily run failed); every
   proof pattern compiles; proof patterns that have never matched a single line since their change shipped and point
   at a log that does exist (pattern bug vs code that really never ran); error logs present and growing.

Outputs: `logs/post_live/weekly/<week>.json` (untracked, like the daily results), one Slack card, and one GitHub
issue `[post-live weekly] week of <date>` holding the ranked work list. The interactive session works that list from
the top (root cause → fix through the gate → close the issue).

Expectation record: `logs/post_live/expect/post-live-weekly-1106.json` with `window_days` (a weekend job) and a proof
pattern on the script's own dated, tagged summary line.

## Rev 1 answers (superseded by the rev 4 answers below)
Q1: Moves edge evaluation: errors and dead code in live paths distort what the bot's trades say about its edges; this
turns 27 stalled findings into a ranked work list. Not conservative.
Q2: No change to trade count.
Q3: Simpler options considered: rely on the session-start hook (it lists only the first 10 issues and has been
ignored for 3 weeks); extend the daily evaluator (a weekly roll-up needs a week of results). A separate weekly reader
is the simplest.
Q4: Builds on POST-LIVE engine (`logs/design_records/post_live_engine_2026-10-11.md`) and the CO weekly audit
(`logs/design_records/co_audit_and_anomaly_2026-10-09.md`, build item 4, not yet built); this report can become the
CO audit's post-live input. No open PR touches post_live files (to verify at build time).
Q5: No decision thresholds: items are ranked, not cut off; "quiet" means zero occurrences since, with the dates shown.
Q6: Exposed assumption: a daily finding gets worked because it was filed. Next: link each group to the change most
likely to have caused it (first-seen day vs ship dates) and feed it to the anomaly rules.
Q7: Feeds the session-start hook (top of the ranked list instead of the first 10 issues) and the CO audit digest.
Fails safe: any unreadable input is reported as such in section 5, never skipped silently.
Q8: Counts above measured on 2026-11-06 (gh issue list, post_live_due.py, OCI results files).
Q9: Replay before ship: run it on the four completed weeks since 10-12 from the existing results files; expected to
group the 10 NEW_ERRORS issues into fewer recurring signatures and flag the 17 expired items. Wrong if a signature
seen in the daily files is missing from the weekly groups.
Q10-Q11: Not a trading change; no size, frequency or exposure effect.
Q12: Does not touch the safety envelope.
Q13: Missing results file → listed as a missed daily run; GitHub unreachable → report still written and sent to Slack,
issue retried next week.
Q14: Not a shadow: it reports on live code.
Q15-Q16: Its own dated, tagged summary line is the proof; the weekly json keeps every input it read.
Q17: First Saturday run checked on the server.
Q18: This exists because findings were being left open rather than investigated.
Q19: No entry, exit, sizing or routing change.
Q20: To be reviewed by board + GAI + Codex with a neutral prompt.
Q21: No broker calls (calendar read only, as the daily evaluator).
Q22-Q23: Not a fail-closed control; replay on 4 past weeks before ship.
Q24-Q25: No market claims.
Q26: T1 Alpaca calendar only; everything else is the bot's own logs and GitHub.
Q27: `logs/post_live/weekly/<week>.json`, atomic tmp → replace; untracked.
Q28: Cron on OCI, Saturday; separate process, never on the trading thread.
Q29: High: 27 open findings and 79 overdue audits are the backlog that protects every other change.
Q30-Q44: Mostly not applicable to a reporting job; the independent evidence is the replay on past weeks and the first
live Saturday run compared with the daily files by hand.

## Rev 2 — board round folded (2026-11-06, signed: Claude)

Board (cold; Gene Kim, Charity Majors) reviewed rev 1. Points folded; rev 2 supersedes rev 1 where they differ.

**Diff A — daily evaluator fixes (`scripts/post_live_eval.py`, ships first, separate diff — tidy first):**
1. Tracebacks get their own signature from the final exception line, passed through `_signature()` (today every
   traceback is the one signature "Traceback", post_live_eval.py:136-137, so a second, different crash is never new).
2. Signature normalisation: bare upper-case tickers masked (not only bracketed ones); HTTP status codes kept (today
   `\d+ → N` merges 429 and 500); a hash of the full normalised message kept beside the 160-character prefix.
3. The results file stores EVERY signature of the session with its line count (not only the new ones, line 200), and
   is written atomically (tmp → replace; today a plain write_text, line 250).
4. One issue per error signature, `[post-live] error <hash>: <prefix>`, commented on each recurrence, instead of one
   `NEW_ERRORS_<date>` issue per day (the date in the title defeats the dedup — why 10 such issues are open). The 10
   open dated issues are mapped to signature issues once and closed with a link.
5. Every UNEXERCISED_EXPIRED is classified: log missing / log has no dated lines / bad regex / pattern never matches
   anywhere in the log / pattern matched before the ship date (pattern good, code really did not run). Today these
   all look the same (`proof_count` skips a bad regex and a missing log silently; `shipped_at` None skips a record).
6. Findings whose issue could not be filed go to a pending file the next daily run retries.

**Diff B — the weekly review (`scripts/post_live_weekly.py`, Saturday cron):** rev 1 sections, plus:
- **Close the loop (Kim):** the card leads with the week-over-week change in (open findings + overdue audits). For
  each overdue PR it pre-assembles the evidence (its expectation-record verdict, error signatures from modules it
  changed, the suspect PRs from `git log` on that module since the signature's first day) and prints the ready-to-run
  `record_post_live.py` command. `post_live_due.py --hook` shows the top 3 ranked items of the latest weekly report
  (not the first 10 issues by number).
- **Quiet issues (Majors):** "quiet" = no recurrence for longer than that signature's own longest gap between
  appearances so far, and whether a fix PR is linked; not a fixed week. Section 3 says "unavailable" when GitHub is
  unreachable.
- **Health:** a session missing a results file is labelled "calendar unreadable" when the calendar read failed (a
  holiday is not a missed run); counts "issue not filed" / "lookup failed" per run; scans the cron log for
  "post-live eval FAILED"; a corrupt results file is reported, never skipped. Week files named by ISO week.
- **Rotation check [hypothesis — unverified]:** `mtf_bot.log` is rotated (WatchedFileHandler + logrotate). If the
  prior-5-session baseline is missing after a rotation, every signature looks new; the build verifies the OCI
  logrotate config and the evaluator reads rotated files (or the stored per-session signatures from Diff A item 3).

**Owner decision (not built unless he says so):** a "finish before starting" rule — no new feature PR while a top
weekly item is older than its own age ranking allows. It would slow shipping; it is the owner's call.

Corrected answers: Q3 — "ignored for 3 weeks" means: the hook has listed open findings every prompt since 10-13 and
none of the 27 issues was closed (gh, 2026-11-06); whether sessions read it is not measured. Q4 — Diff A is a
separate diff before Diff B. Q5 — "quiet" is per-signature gap-derived. Q13 — retry is the next daily run. Q40 —
machine check: (open findings + overdue audits) week over week, reported as the first line. Q42 — the rawest source is
the per-session signature counts Diff A stores. Q44 — the replay is checked against an independent recount (raw
grep of the logs + `gh issue list`).

## Rev 3 — GAI + Codex round on rev 2 folded (2026-11-06, signed: Claude)

Codex: reject of rev 2; GAI: changes requested. Rev 3 supersedes rev 2 where they differ.

**Diff A (daily evaluator) changes:**
1. **Signature identity (Codex, GAI):** a traceback's signature = exception type + normalised final message + the
   deepest in-repository frame's `module:function`; an ERROR/CRITICAL line keeps its logger module (already in the
   signature) and adds the calling function when the log line carries it. A `ValueError: invalid state` in two modules
   is two signatures. A hash of the full normalised message is kept as a collision check.
2. **Ticker masking only for known symbols (Codex):** a bare upper-case token is masked only if it is in the bot's
   symbol universe (config universes + symbols in the ownership ledger); `BUY`, `SELL`, `GTC`, `IOC`, `HTTP`, `JSON`
   and other words stay. HTTP status codes stay distinct (429 ≠ 500).
3. **Baseline from stored signatures, not rotated logs (Codex, GAI):** the "prior 5 sessions" baseline reads the
   per-session signature sets Diff A now stores; until 5 stored sessions exist it also reads rotated logs
   (`mtf_bot.log.1`, `.gz`). The OCI logrotate config is checked at build time; the 11-05 / 11-06 counts (6, 8) are
   re-run against it to see whether rotation inflated them.
4. **One durable pending-outputs file** for anything not delivered (daily finding issues, the weekly issue, Slack),
   retried by every evaluator run, daily and weekly; pending count and oldest age appear in the health section.
5. **Legacy issues closed by the build (GAI):** the 10 open `NEW_ERRORS_<date>` issues are mapped to signature issues
   and closed through the API with a link; the build verifies each closure.

**Diff B (weekly) changes — honestly scoped as triage plus a disposition loop (Codex):**
6. Every ranked item gets a durable disposition record (`logs/post_live/weekly/dispositions.json`, atomic):
   open / in progress (PR linked) / resolved with evidence (fix PR or audit record) / owner decision. The next report
   flags top items whose disposition did not change and promised actions not done.
7. Outcome metrics are separate, not one net number (Codex): findings resolved with a linked fix, audits completed
   with evidence, new findings, and overdue aging. Closing an issue without a linked fix counts as unresolved.
8. **Incomplete inputs make dependent conclusions incomplete (Codex):** a missing or corrupt results file → counts
   shown as lower bounds ("at least 8; Wednesday unavailable"), and no "quiet" verdict that spans an unknown session.
   The last good trading calendar is cached; with no calendar, session coverage is "unknown", not "holiday".
9. The summary line carries ISO week, sessions expected / read / corrupt, signature count, pending outputs and
   status (complete / degraded).
10. Owner decision (unchanged, GAI endorses): a "finish before starting" rule as a CI check on new feature PRs while
   a top weekly item is older than its own ranking allows. Not built unless the owner says so.

Corrected answers: Q7 — dependent sections become lower-bounded or unavailable, not only reported. Q9 — the replay
on the 4 weeks since 10-12 is checked against an independent recount (raw grep of every session's logs + `gh issue
list`) for BOTH omissions and collisions (two distinct raw failures in one signature); pass = zero of each on the
sampled set. Q15 — the weekly json keeps the normalisation version, signature components, session coverage, input
file identities, the issue-query time and result, the calendar decision, ranking inputs and exclusion reasons.
Q16/Q17 — the summary line above, and the first Saturday run compared with an independent recount by the same pass
rule. Q29 — not ranked numerically against other open items; requested by the owner 11-06; it protects the
post-live mandate behind every other change. Q40 — metrics in item 7.

## Rev 4 — confirmation round on rev 3 folded (2026-11-06, signed: Claude)

Codex and GAI each asked for changes on rev 3; neither rejected it. Rev 4 supersedes earlier revs where they differ.

**Diff A (daily evaluator):**
1. **Canonical signature tuple, versioned (Codex 1-3, GAI):** `(normaliser_version, exception type or logger module,
   deepest in-repo frame module:function, its in-repo caller module:function, canonical message)`; the issue identity
   is the hash of that tuple; the 160-character prefix is display only. The canonical message replaces volatile values
   with typed placeholders: uuids, order / client ids, timestamps, durations, prices, quantities, file paths, URL query
   values, known symbols; raw examples are kept beside the signature, never in its identity. HTTP status codes and
   enum words (BUY, SELL, GTC, IOC) stay.
2. **Stable symbol rule (Codex 2, Q32):** symbols are recognised by the union of every symbol universe snapshot over
   the replayed window (stored with the normaliser version), so a symbol added on Tuesday does not split Monday's
   signature; no hindsight from today's universe alone.
3. A separate related-cluster view groups signatures that share exception + canonical message across call sites, so
   one root cause surfacing through two wrappers is visible as related (issue identity stays call-site specific).
4. **Idempotent delivery (Codex 4):** every pending output carries a deterministic delivery key (in the issue body);
   a retry searches for that key before creating anything; Slack is at-least-once and says so in its footer.
5. **Critical state files:** results, pending outputs and dispositions are written atomically with a schema version;
   an unreadable file is backed up, reported as degraded in the health section, and never silently replaced.
6. **Rotation mid-session (GAI):** a session whose log rotated during the session is flagged `partial_data`.

**Diff B (weekly) — named honestly: triage plus accountability, not a guaranteed burn-down (Codex):**
7. **Dispositions are derived from verified evidence, not trusted JSON (Codex 5, GAI):** "resolved" needs a merged PR
   (checked through the GitHub API) that touches the implicated module, or a `logs/post_live_audits.jsonl` record
   (the existing `record_post_live.py` format) naming the PR / issue and the commit; unverifiable → stays open,
   labelled `evidence_unverified`.
8. **Pre-Diff-A replay weeks (Codex 6):** signatures are rebuilt from whatever raw logs remain; a session whose raw log
   has rotated away is `unavailable` and its conclusions are lower bounds; the zero-omission / zero-collision pass rule
   applies only to sessions with raw logs.
9. The cached calendar carries its as-of, covered date range and freshness; a stale cache gives `unknown` coverage.
10. Not adopted: GAI's fixed "> 10 trading days → REGIME_DEPENDENT" label (a fixed number; the per-signature gap rule
   already covers rare, regime-linked errors). The "finish before starting" CI gate (GAI endorses a hard 14-day gate)
   stays an owner decision.

## Required questions — rev 4 answers (replace all earlier answers)
Q1: Edge evaluation depends on knowing whether shipped code ran and whether new errors appeared; this keeps those
findings from being lost. It measures no edge itself (not claimed).
Q2: No change to trade count.
Q3: A weekly roll-up needs a week of results, so it is a separate reader; it reuses the daily evaluator's parsing and
GitHub/Slack delivery helpers rather than new copies.
Q4: Builds on POST-LIVE engine (post_live_engine_2026-10-11.md) and the CO weekly audit (co_audit_and_anomaly_2026-10-09.md,
build item 4, not built). Open-PR check on post_live files is done at build time (not yet run).
Q5: No fixed decision numbers: ranking, per-signature gaps, rolling evidence.
Q6: Exposed assumption: filing a finding causes action. Next: link each signature to the suspect change and feed the
anomaly rules.
Q7: Feeds the session-start hook and the CO audit; dependent sections become lower-bounded or unavailable on bad input.
Q8: Counts measured 2026-11-06 (gh issue list, post_live_due.py, OCI results files); "findings distort edge evaluation"
is a hypothesis, not measured.
Q9: Replay on the 4 weeks since 10-12 against an independent recount (raw grep + gh) for omissions and collisions on
sessions with raw logs; rotated-away sessions marked unavailable.
Q10: No trading edge is gated.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change.
Q13: Missing / corrupt inputs → lower bounds or unavailable; delivery retried idempotently by the next evaluator run.
Q14: Report-only tooling that depends on a session to act on it; not a trading shadow, but it does not act by itself.
Q15: Weekly json keeps normaliser version, symbol snapshot, signature components, raw examples, session coverage,
input file identities, issue-query time and result, calendar decision, ranking inputs and exclusion reasons.
Q16: The dated, tagged summary line with ISO week, sessions expected / read / corrupt / partial, signature count,
pending outputs and status.
Q17: First Saturday run: the session that builds it compares the output with an independent recount the same day;
a mismatch opens a finding and the run is marked degraded.
Q18: Exists because findings were left open rather than investigated.
Q19: No trading-path change.
Q20: Prompts carried counts and code facts only; "logs fully reviewed" is not claimed beyond the counts cited.
Q21: The Alpaca calendar read IS a broker API call; tests mock it and the broker refuses under test.
Q22: Not a fail-closed control.
Q23: Replay on 4 past weeks before calling it ready.
Q24: No market claim is made; the job only counts the bot's own log lines.
Q25: No market claim is made, so there is no market statistic to measure from bars.
Q26: T1 Alpaca calendar (cached with as-of); everything else is the bot's own logs and GitHub.
Q27: logs/post_live/weekly/<ISO week>.json, pending and disposition files: atomic, schema-versioned, untracked.
Q28: Separate Saturday cron on OCI; never on the trading thread.
Q29: Not ranked numerically against other open items; requested by the owner 11-06; small tooling diff.
Q30: Blind spots covered: state corruption, idempotent delivery, the evaluator's own health, ownership of each item,
alert fatigue (one weekly card, per-signature issues).
Q31: Not an alpha claim; the replay includes holiday, rotation and partial-week cases.
Q32: Symbol normalisation uses the union of historical universes, not today's alone.
Q33: Not applicable because the job places no trades and changes no trading parameter.
Q34: Not applicable because the job sizes nothing and touches no exposure limit.
Q35: Operational regimes tested: holidays, outages, rotation, partial weeks.
Q36: Bounded: issues per signature, not per day; weekly json size grows with signatures, not lines.
Q37: Detection measured deterministically by the replay recount.
Q38: Evaluator-quality decay shows as replay mismatches and health failures.
Q39: Not applicable because the job sizes no position and reads no P&L; Kelly sizing is untouched.
Q40: Findings resolved with verified fixes, audits completed with evidence, new findings, overdue aging — separate.
Q41: An SRE team would also set an owner and a service-level time per item; offered to the owner as the finish-before-
starting decision.
Q42: Rawest data: the raw error lines per session (stored per session by Diff A), not the daily summary.
Q43: Standard practice: error grouping by stack fingerprint with versioned normalisation, idempotent ticket creation,
burn-down tracked by verified closure.
Q44: Independent recount (raw grep + gh) and the first Saturday comparison.

## Rev 5 — confirmation round on rev 4 folded (2026-11-06, signed: Claude)

Codex and GAI each asked for changes on rev 4; neither rejected it.
1. **Mask by role, not by type (Codex 1):** ids, uuids, order / client ids, timestamps, durations and URL query values
   are masked; file paths keep their basename (`live.yaml` ≠ `replay.csv`); numbers that are quantities or prices are
   replaced by their sign class (negative / zero / positive), so `quantity 0 … available -1` and `quantity 500 …
   available 100` stay different. Adversarial merge / split fixtures ship with the normaliser.
2. **Symbols masked only where the log line marks them as symbols (Codex 2):** `[SYM]` brackets, `symbol=` fields and
   the bot's own `(SYM)` patterns; bare words never (fixtures: ALL, ON, IT, ARE, FOR, BUY, SELL).
3. **Owner / next action / due session for every top item (Codex 3):** default owner = the interactive Claude
   session, or the owner when it needs his decision; a missed due session opens a durable escalation issue and leads
   the next Slack card. Still triage plus accountability — the report does not claim the backlog burns down by itself.
4. **Independent oracle (Codex 4):** a separately written recount script that imports none of the evaluator's parsing
   or normalisation code, plus a frozen, reviewed fixture set; its version and the comparison file are recorded.
5. **GitHub down (Codex):** every GitHub-dependent number (open issues, verified resolutions, linked fixes, overdue
   PRs, suspect-PR lookup) is marked unavailable, not only the quiet section.
6. **GAI:** the lower-bound warning names the missing dates in the report text; an audit record counts as evidence
   only if its commit exists on `main`.
7. Not adopted: GAI's mandate of a CI gate after 14 days — GAI has no mandate authority and 14 days is a fixed number;
   the "finish before starting" gate stays the owner's decision.

## Rev 6 — confirmation round on rev 5 folded (2026-11-06, signed: Claude)

Codex and GAI each asked for changes on rev 5; neither rejected it.
1. **Symbols in known message templates (Codex 1):** besides `[SYM]`, `symbol=` and `(SYM)`, a known symbol (frozen
   historical universe) is masked in the bot's own template positions such as `for <SYM>` / `on <SYM>` after a fixed
   phrase; fixture: `No bars returned for AAPL` and `… for MSFT` must merge; `ALL orders rejected` must not.
2. **Quantity relationships kept (Codex 2):** where a message compares two numbers (requested vs available, price vs
   limit), the canonical form keeps the relation (`requested<=available` / `requested>available`) beside the sign
   classes; fixture: `quantity 1, available 100` and `quantity 100, available 1` must not merge.
3. **Caller frame moves out of the issue identity (Codex):** identity = (normaliser version, exception type or logger
   module, deepest in-repo frame, canonical message); the caller frame is a grouping dimension in the related-cluster
   view only.
4. **Oracle location (GAI):** `scripts/post_live_oracle.py` (imports nothing from the evaluator) with frozen fixtures in
   `tests/fixtures/post_live_signatures/`; the weekly json records the oracle's version.
5. GAI's fail-closed rule for a "finish before starting" CI gate is recorded for the owner's decision on that gate; no
   gate is built in this design.

## Rev 7 — final changes from the rev 6 confirmation (2026-11-06, signed: Claude)

Codex and GAI both returned approve-with-changes on rev 6 (no reject). Folded:
1. **"Resolved" needs a result, not a touched file (Codex):** the PR or audit record must name the signature's issue /
   delivery key, AND one machine-evaluated result must hold: a regression fixture for that signature added in the PR
   passes, or the signature has not recurred for longer than its own longest past gap since the merge (the quiet
   rule). Otherwise `open`; GitHub unreachable → `evidence_unverified`.
2. **Audit-record commit check (GAI 1):** `git merge-base --is-ancestor <commit> origin/main` must succeed, else
   `evidence_unverified`.
3. **Oracle / normaliser version (GAI 2):** if the oracle's recorded normaliser version differs from the evaluator's,
   the weekly report is marked degraded and the mismatch is listed; the comparison is not reported as a pass.
