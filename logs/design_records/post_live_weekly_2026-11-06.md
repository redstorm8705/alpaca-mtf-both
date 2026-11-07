# POST-LIVE-WEEKLY-1106 — weekly review of the post-live evaluator (design record)

Status: rev 2 DRAFT (board round folded 2026-11-06); rev 1 DRAFT (signed: Claude, 2026-11-06). Owner request 2026-11-06: "We also need a weekly review of the post
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

## Required questions
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
