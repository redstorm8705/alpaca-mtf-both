# CI-CONTEXT-1117 — the CI reviewer sees the code a change touches, not the first 300,000 characters

Approval-ID: CI-CONTEXT-1117
Covers: .github/scripts/ci_audit.py, .github/workflows/preship-verify.yml, .github/scripts/test_ci_audit_context.py
Not changed: .claude/preship/preship_audit.py, .claude/preship/gai_client.py
Status: rev 4 (signed: Claude, 2026-11-17). Rev 4: board seat (Beck) — the reviewer's view is today's head-cut plus the
far-away pieces; budget shares defined; deletion-only hunks and attribute names defined.
Prior: rev 3 (signed: Claude, 2026-11-17). Rev 3: reviewer #2 round 2 — a touched piece is never cut mid-function.
Prior: rev 2 (signed: Claude, 2026-11-17). Design only. Rev 2: reviewer #2 round 1 — an oversized reduction no longer
falls back to the head-cut; pieces are dropped lowest priority first and touched code is never dropped.

## Problem (measured 2026-11-17, PR 985 run on 2026-11-18 02:41 UTC)
- The CI pre-ship step appends the whole post-change body of every changed file to the reviewer prompt
  (`preship-verify.yml`, the `gated_context.txt` loop), then `ci_audit.py:308-310` keeps only the first 300,000
  characters (`context = context[:MAX_CONTEXT_CHARS]`).
- the QHM manager file is 559,981 characters. The reviewer therefore never sees anything after about
  line 5,300 of it. The run printed "full-file context exceeded the size cap and was truncated".
- Result on PR 985: two of three samples rejected on premises the cut-off copy created: "`_now_et` is not defined"
  (it is, at line 6220) and a claim about `max_limit_price` (its use and the order submit are at lines 4050-4061 and
  the owner-buy call at 9409, both visible only in part). Every coming QHM change (KILL-EXIT-JOIN-1117,
  HOLD-NEWS-REVIEW-1113, QHM-OVERWEIGHT-1113) edits the same file and will hit the same wall.

## Change
In `ci_audit.py`, build the context per file instead of cutting the joined text at 300,000. The script reads each
blob itself with `git show <head>:<path>` (no splitting of joined text):
1. Budget: files are taken smallest first; each file that fits in what is left is included whole, as today. The
   remaining budget is split evenly across the files that do not fit.
2. A file that does not fit is reduced, if it is a `.py` file `ast` can parse (CI runs Python 3.11, so `end_lineno` is
   always present), into labelled pieces with file and line range, priority order: (a) the full source of every
   function or method whose lines the diff touches (a deletion-only hunk `+N,0` belongs to the function enclosing
   line N), plus changed lines outside any function; (b) the full source of every function, method or class defined
   in that file whose name the diff's added or removed lines use as an identifier or attribute name (`foo(` and
   `self.foo(` both count; names inside strings do not; names are found by tokenizing the whole HEAD or BASE file and
   mapping the tokens back to the diff's line numbers), one level.
3. The file's budget is filled in this order: (a), then (b), then today's head-cut text from line 1 down with the
   lines already included skipped. So the view always contains today's head-cut up to the point where the budget was
   spent on code further down that the diff itself touches or names. Pieces never get cut mid-function; a piece of (b)
   that does not fit is skipped and named in the log.
4. If (a) alone exceeds that file's budget, the file keeps today's head-cut with today's warning and the reason. A
   non-`.py` file, or one `ast` cannot parse, keeps today's head-cut with the reason ("ast parse error: line N").
5. The log prints, per reduced file, the kept line ranges, the skipped pieces by name, and the budget left.
The workflow passes the base sha and the list of changed line ranges (from `git diff -U0`) to the script.
Tests (`test_ci_audit_context.py`): a small file stays whole beside a large one; a large file keeps a touched method
and a referenced method (called as `self.x(`) defined far below the cut; a caller just below a touched function near
the top is still present (head-cut fill); a name only inside a string, including a hunk that starts inside a
triple-quoted string, pulls nothing; a deletion-only hunk pulls its enclosing function; a referenced piece that does
not fit is skipped and logged; touched pieces larger than the budget fall back to the head-cut; invariant test: for
every file, today's head-cut lines are present except where replaced by (a) or (b) pieces; the total never exceeds
the cap. The parsing into pieces and the budget/fill policy land as separate commits (Tidy First).

## Required questions
Q1: Every QHM change is currently reviewed on a cut-off copy; false rejects block ready trading fixes.
Q2: No trade-count change.
Q3: Simplest form that works: the standard-library parser, one level of references.
Q4: Extends the 2026-07-25 full-file context fix; no open PR touches ci_audit.py.
Q5: Not applicable because no threshold is introduced; the existing 300,000 cap is kept.
Q6: Forward: the same reduction can feed preship_audit.py's own context later.
Q7: Not applicable because it is review tooling, not a trading signal.
Q8: Measured: PR 985 CI log, sample 3 ("`_now_et` ... is not defined") and the truncation warning; `wc -c` 559,981.
Q9: Unit tests on synthetic files plus a dry run on PR 985's real diff showing line 6220 is in the context.
Q10: Not applicable because no trading edge is involved.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change; the CI gate still needs two of three APPROVE samples.
Q13: Parser failure or a non-Python file: today's behaviour, with the reason. Oversize: lowest-priority pieces dropped,
touched code kept.
Q14: Not a shadow: it changes the live CI review input.
Q15: The CI log prints which files were reduced and their kept line ranges.
Q16: Proof: the next QHM PR's log shows "reduced" for the QHM manager file and no truncation warning.
Q17: The KILL-EXIT-JOIN-1117 PR, the next QHM change.
Q18: The anomaly closed: a reviewer rejecting a name that exists because it was cut out of its view.
Q19: Gate file: owner approval; GAI and reviewer #2 review the exact diff before it ships.
Q20: Neutral prompts; tests included.
Q21: Tests make no network call and touch no broker.
Q22: Not applicable because it is not a trading control.
Q23: The tests plus the PR 985 dry run.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because it uses no market statistic.
Q26: Not applicable because it reads no market data.
Q27: Writes nothing; builds prompt text in memory.
Q28: Runs on every pull request CI run, as today.
Q29: Small; it unblocks every large-file change.
Q30: Blind spot: one size cap applied to joined text, so the largest file decides what the reviewer can see.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no market replay; the dry run uses PR 985's real diff.
Q33: Not applicable because it places no trades.
Q34: Not applicable because it has no return.
Q35: Not applicable because it is not market-dependent.
Q36: Not applicable because it trades nothing.
Q37: Not applicable because it is not a trading edge.
Q38: Not applicable because it is not a trading edge; if the reduction misbehaves, the head-cut fallback remains.
Q39: Not applicable because nothing is sized.
Q40: Protects review accuracy: the PR 985 dry-run test asserts line 6220 (`def _now_et`) is in the built context.
Q41: Build the context from the code the change touches and names, as code-review tools do.
Q42: The raw git blobs at the PR head and base, read with git show.
Q43: Syntax-tree slicing of touched and referenced definitions, the standard method for code-review context.
Q44: The CI log on the next QHM PR (no truncation warning, the reduced ranges listed) and the reviewers' verdicts.

## Review record (rev 4)
Board (Beck, cold): APPROVE rev 4 (REJECT on rev 3: caller below a touched function dropped — fixed by the head-cut
fill). GAI: APPROVE. Reviewer #2: Codex at its usage limit, Gro: REJECT on rev 1-2 and on rev 4 (input: touched line at
30,050 in a 420 KB file); counter-evidence (today's view stops near line 5,300, and the diff lines are always sent
separately) -> APPROVE. Build notes from the board (no behaviour change): the invariant test is measured per file
against that file's own head up to its budget; in a multi-file run the step-4 fallback cuts at the file's budget; a
(b) piece over budget is logged with its size; the cap counts labels and headers; a touched inner function pulls its
whole outer function.
