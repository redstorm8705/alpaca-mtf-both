# Post-live audit engine — design (2026-10-11, Claude)

**CEO ask (2026-10-11):** "post live audit" with no plan for how it triggers, what it evaluates, and how the result is
reviewed and turned into bot changes is shallow and static. Mandate behind it: every diff that goes live is audited and
replayed against its first full trading session, and bugs are fixed at once (Rafael 2026-10-08).

## Today (verified at source)
- `.claude/preship/post_live_due.py`: a prompt hook that marks a merged bot-code PR "OVERDUE" once its first full
  regular session has ended. `record_post_live.py` appends a free-text verdict to `logs/post_live_audits.jsonl`.
- The audit itself is a person reading logs by hand. Nothing states in advance what the change should do, nothing
  evaluates it automatically, and nothing feeds back.
- Evidence of the gap: PR #554 was recorded `PASS` with the note "not exercised: no Swing tranche/target exits on
  10/09". A change that never ran was closed as passing.

## Proposed design

### 1. Trigger
- **Expectation record at ship time (mechanism, not a request).** Every bot-code PR adds
  `logs/post_live/expect/<slug>.json`:
  - `paths`: the files changed;
  - `proof_patterns`: log or event patterns that prove the new code ran (file + regex or event name);
  - `metrics`: what should change and in which direction, with rough size (the Rule C replay output);
  - `reversal`: the live result that would prove it wrong;
  - `window_sessions`: how many sessions to watch (default 1, with a re-check at 5).
  The pre-ship gate refuses a bot-code commit with no expectation record (same hash-bound marker pattern as the
  other gates).
- **Evaluation job on OCI.** A cron at 16:30 ET on each trading day evaluates every expectation whose watch window
  includes that session. It reads production logs and Alpaca fills read-only. It places no orders and changes no
  config.

### 2. What it evaluates
- **Ran?** Counts each proof pattern in that session's logs. Zero = `NOT_EXERCISED`, never `PASS`. The record stays
  open and is re-checked each session until it runs or its window ends, then it is flagged `UNEXERCISED_EXPIRED`.
- **Side effects:** errors, tracebacks, pages, broker rejects (wash trade, 40310000) in the changed modules that
  session, compared with the prior 5 sessions.
- **Metrics vs expectation:** each metric computed from its stated source. P&L only from Alpaca fills (never tracker
  math). Verdict per metric: within, better, worse, or reversal criterion hit.
- **Decision replay (where the change made decisions):** a per-change replay script, if the PR provides one,
  re-derives that session's logged decisions from their inputs and reports mismatches.

### 3. Review and feedback
- **Outputs:** `logs/post_live/results/<slug>_<session>.json`, a one-card Slack digest at 16:35 ET (plain English,
  one line per change: ran / did what it said / problem), and a queue file `logs/post_live/queue.jsonl` with every
  finding that needs action.
- **Action:** the prompt hook reads the queue. A finding is worked by the next session: root cause from the logs,
  then a fix or optimisation through the full gate. `NOT_EXERCISED` items stay visible, not closed.
- **Learning:** a finding that could recur becomes an anomaly rule (design record co_audit_and_anomaly_2026-10-09.md
  section 3) and a test. Metric results feed the per-feature kill flags and the weekly connection-optimization audit.

## Open forks for BGG
1. Expectation records: required for every bot-code PR (gate-enforced), or only for risk-path / strategy changes?
2. `NOT_EXERCISED` handling: keep open indefinitely until it runs, or expire after N sessions (what N)?
3. Who acts on a finding: the interactive session only (via the hook), or may the OCI job itself open a GitHub
   issue / Slack thread?
4. Build order: (a) evaluator + Slack digest over the existing records first, then the gate; or (b) the gate first,
   so every new PR carries an expectation from day one.

## BGG consensus (2026-10-11; board Kim/Majors/López de Prado, Gro, GAI — GAI moved to the board on fork 3 after one counter-prompt)
1. **Scope:** every bot-code PR, gate-enforced, two tiers.
   - Tier 1 (all PRs): paths, a UNIQUE structured proof event (a change_id tag the new code emits, checked at ship time
     to match zero times in the 5 sessions before merge), and the reversal criterion.
   - Tier 2 (risk-path and strategy PRs): adds pre-registered metrics with direction and bounded size, plus a replay or
     verification test (GAI).
2. **NOT_EXERCISED:** never PASS. The author declares an expected trigger rate; the window defaults to 5 sessions.
   At expiry, a queued finding that needs a historical-replay proof that the code fires, or a remove / kill-flag
   decision. Add NOT_EXERCISED to record_post_live.py. Reopen #548, #549, #554 (recorded PASS while unexercised).
3. **Who acts:** the OCI job opens or updates GitHub issues deduplicated by (change slug, finding key), and Slack links
   to them. It writes no code, makes no commits, changes no config and writes no tracked files on OCI (a tracked
   queue file would break `git pull --ff-only`; an untracked one is invisible to the Mac hook). The Mac hook reads open
   issues with `gh`. Fixes still go through the interactive session and the full gate.
4. **Build order:** the gate first, with a minimal evaluator in the same increment:
   - proof-event counts;
   - NEW error signatures versus the prior 5 sessions (not count deltas);
   - issue/Slack output.
   Metric evaluation comes second. The expectation is frozen at merge (content hash). Only mechanical metrics get a
   session-1 verdict; P&L metrics read "insufficient sample" until the 5-session recheck and are labelled indicative.
Not risk-path (no size/frequency/concurrency change); the normal gate applies.

## Runbook (increment 1)
- OCI crontab (not tracked in git; recorded here): `15 22 * * 1-5 cd /home/ubuntu/mtf-bot && /home/ubuntu/mtf-bot/venv/bin/python3 scripts/post_live_eval.py >> logs/post_live_eval_cron.log 2>&1`
  (22:15 UTC = 6:15 PM EDT / 5:15 PM EST, after the close on either clock).
- An expectation record: write `logs/post_live/expect/<change>.json`, `git add -f` it, then
  `python3 .claude/preship/record_expectation.py logs/post_live/expect/<change>.json --covers <file> ...`.
  The proof pattern must contain the change slug (the new code logs a unique `[<change>]` tag). A window under 5
  sessions needs `window_reason`. `baseline_exempt_reason` is the audited escape (e.g. a config-only change), and it is
  visible in the PR.
