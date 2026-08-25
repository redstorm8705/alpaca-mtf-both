# MANDATORY PATCH SEQUENCE — full text (canonical definition site for P01–P10)
<!-- Consolidation (Rafael 2026-08-22, Hybrid C+). Every file, every session, every patch, this exact order —
     zero exceptions (urgency/simplicity/familiarity are not exceptions). CORE.md carries the pointers; THIS
     file is authoritative. Much of this is gate-enforced (preship_gate markers), but the AGENT orchestrates it. -->

## P01 — Full-read gate; NO grep as file exploration
No patch is proposed until the WHOLE target file is read. File ≤1000 lines → Read tool in ≤300-line chunks,
every line. File >1000 lines → a dedicated Explore subagent that returns VERBATIM content (every line, no
summaries — a summary is NOT a read). Declare "Full read complete: N lines" before any analysis. grep / awk /
find / sed / search tools are FORBIDDEN as a substitute for reading a file — usable only AFTER a full read to
confirm a line already identified. A patch applied without a full read invalidates the audit; re-read in full
next session before any further change.
_Source: CLAUDE.md §Full-Read Gate + §Explore Subagent._

## P02 — 10-point audit + recurring-bug checks (the 8 RC classes AND the historical recurring bugs)
Before a patch is proposed: run the 10-point per-file audit (statics, trade-path trace, adversarial scenarios,
full read, cross-references, conflicting directions, redundancy, state-persistence, data-tier compliance,
timezone+logging) AND grep+mark PASS/FAIL for the 8 named recurring bug classes (RC-1 naive datetime, RC-2 CWD
paths, RC-3 silent except, RC-4 estimated exit price, RC-5 non-atomic write, RC-6 wrong API field, RC-7
zero-share sizing, RC-8 unbounded scan buffer) **PLUS the oldest / historical recurring bugs from the past 6
months** recorded in logs/bug_counter.json (`fixes` / `sessions`) + logs/tb_audit_log.md. The check is NOT
limited to the 8 named classes (Rafael 2026-08-22) — any bug class that has ever recurred is checked. Record
results in logs/tb_audit_log.md before writing the patch.
**BUG-COUNTER UPKEEP (Rafael mandate 2026-08-22):** logs/bug_counter.json is the running log of every bug found
+ patch applied. It MUST be reviewed, consolidated, and actively UPDATED at the END OF EVERY SESSION that
touches code. It was found ~5-7 weeks STALE on 2026-08-22 (local last-touched 2026-07-05, OCI 2026-07-19) — the
"update every session" rule was silently violated, so this needs a mechanical end-of-session gate, not just the
rule. Consolidate the counter (RC counts + file_hotspot + a de-duplicated `fixes` running log) and keep it live.
_Source: CLAUDE.md §10-Point Audit + §Recurring Bug Classes + §Debug Session Protocol; memory feedback_bug_logging_mandatory._

## P02b — DYNAMIC-FIRST: the is-it-static gate fires WHEN DEVELOPING the diff
When first developing/designing a proposed diff — BEFORE it is proposed — apply the is-it-static gate (B10) to
the diff ITSELF: does the answer it encodes rely on a STATIC value (a hardcoded number / threshold / rule) or a
DYNAMIC one (self-calibrating from data)? A static answer is thrown out ~9/10 — derive the DYNAMIC version and
propose that. The 1-in-10 static exception applies only when no historical data exists to calibrate from,
flagged explicitly + logged as a roadmap item to dynamize later. BGG is convened on the static-vs-dynamic call
on any fork. This is a DESIGN-STAGE gate: a static diff should not reach P05/P06/P07 without that justification.
_Source: rules/behaviour.md B10 (never-static); Rafael reaffirmed at the diff-development stage 2026-08-22._

## P03 — Board vote = cold parallel subagents, never roleplay
The board is independent Explore/general-purpose subagents, one per domain, run COLD and in parallel — never
Claude generating all voices inline (that shares one context + blind spots). Report all findings including
contradictions; never smooth disagreements into false consensus. Board votes on audit findings, not on a patch
already written. Domain mapping + the 28-seat roster live in rules/board_protocol.md.
_Source: CLAUDE.md §Board Execution Model._

## P04 — Gro+GAI external audit — ANTI-BIAS prompt + CITE-WITH-ACTUAL-LINES rejection; counter-prompt, never blind re-roll
Required for ANY new/modified non-read-only code that runs during RTH (import-chain trigger, not file name).
Same comprehensive prompt to both.
**ANTI-BIAS (co-located here, enforced regardless of B11): the prompt carries ONLY facts + the raw diff — NEVER
verdict-leading language ("board approved", "obviously safe", "should approve", "confirmed dormant"). Refused by
`bgg_prompt_bias_gate` / `preship_audit._check_prompt_bias` before any Gro/GAI call.**
**CITE-WITH-ACTUAL-LINES: a REJECT is INVALID unless it quotes the EXACT offending line(s) and gives a CONCRETE
failing input (input → wrong output/crash). Theoretical "could"s go in a NITS section, never a REJECT. A reject
that rests on a false premise (a helper defined outside the diff window) is answered by SHOWING the code, not by
re-rolling.**
On ANY fail/reject from ANY reviewer (cold-2nd, board seat, Gro/GAI, preship) → counter-prompt the rejecting
voice with the SPECIFIC refuting evidence (the cross-file helper body, caller guards, the trace, the unit-test
result). Blindly re-rolling the same prompt is PROHIBITED. Pre-load ground-truth `--context` to pre-empt
false-premise rejects. ≤3 counter-prompt rounds, then escalate to Rafael. Gro/GAI have no mandate authority —
findings only; Rafael decides. (API + personas → rules/reviewer_gates.md.)
_Source: CLAUDE.md §Step 4 + §Disagreement Protocol + §Self-QA #1 (bias gate) + §Reviewer-Context + §No-Leading-Prompts; rules/behaviour.md B11._

## P05 — statics clean (all three)
`python3 -m py_compile`, `python3 -m mypy --warn-unreachable`, and `ruff check --select E,W,F,B` must all pass
clean on the patched file — output shown. No pre-existing carve-out: a file already failing must be fixed as
part of the patch.
_Source: CLAUDE.md §5a + §Pre-Proposal Checklist + RULE C-4._

## P06 — cold-2nd PASS; every revision = fresh review
A cold Explore/general-purpose subagent gets the exact diff + intent and hunts: (1) logic inversion, (2)
off-by-one/boundary, (3) missing conditions, (4) both TRUE and FALSE branches of every new conditional. Binary
PASS/FAIL; a FAIL blocks. Enforced: `preship_gate` requires a fresh `record_cold2 PASS` bound to the sha256 of
the exact staged content. EVERY revision requires a fresh review (later rounds catch defects the prior fix
introduced). State the runtime context in the prompt (threading, what runs synchronously behind it).
_Source: CLAUDE.md §Cold Second-Agent Review._

## P07 — FINAL Gro+GAI pre-ship on the exact diff
After approval and BEFORE apply/ship, Gro AND GAI audit the EXACT final diff (the literal bytes, not the
concept). Both must APPROVE; APPROVE-WITH-CHANGES is not a pass until every change is applied and re-sent clean.
Applies to all artifacts that reach/govern execution (.py, config, patch files, deploy/runbook docs). No "just
docs / small / obvious / concept-already-approved" exceptions. Self-application: a change to this rule takes the
same final pass.
_Source: CLAUDE.md §FINAL PRE-SHIP Gro+GAI Audit._

## P08 — git single-channel deploy; DEPLOY_OK; no rsync
Git is the SOLE deploy channel: Mac commits + pushes → GitHub → CI (ci_audit.py) → merge → OCI
`git pull origin main --ff-only` + `sudo systemctl restart mtf-bot mtf-writer mtf-http`. rsync of tracked files
is PROHIBITED (dirties OCI's git tree → aborts the next ff-only pull). Rollback reverts the CAPTURED patch SHA,
never HEAD. Deploy success requires a literal `DEPLOY_OK` marker from the OCI ssh command; its absence = the
pull failed → do not report success. Deploy is NOT gated on market hours (a live RTH bug is the most urgent to
fix during RTH); prefer landing a restart between 5-min run_cycle scans. Commit form: plain `git commit` (no
-a / pathspec / chained `git add`) so the gate audits STAGED bytes.
_Source: CLAUDE.md §Deploy Mechanism + §Deploy Timing._

## P09 — session boundary resets every gate to zero
Every session, every gate resets: prior-session full reads, audits, board votes, Gro/GAI feedback, statics, and
user approvals EXPIRE at the session boundary (compaction, context reset, new conversation). A compaction
summary is context only — it satisfies ZERO steps, is not an approval, not a completed read, not a board vote.
Resuming in-progress work = restart from Step 1 (there is no checkpoint/save-state). Each file completes its
full sequence independently before the next file's Step 1. "Simple/small/obvious/low-risk" are not modifiers
that skip steps.
_Source: CLAUDE.md §Session Boundary and Compaction Rules (C-1..C-8)._

## P10 — Self-QA: the 4 pre-ship self-checks
(1) "Am I giving BGG lazy/biased prompts?" → ENFORCED git gate (bias gate refuses verdict-leading context). (2)
"Is this a lazy take / am I overclaiming?" → adversarial self-review vs source, ENFORCED (adversarial marker
per bot-code ship). (3) "Am I fully reviewing the logs?" → cite a REAL current log line proving the issue is
real (never a fix for a non-problem). (4) "Am I thinking ahead — what does BGG say?" → BGG design pass on any
new feature/fork before code. Honesty clause: never call code LIVE until it has actually run in production.
_Source: CLAUDE.md §Self-QA Gate._
