# Owner-number path in the no-static gate — 2026-10-25 (signed: Claude)

Approval-ID: OWNER-NUMBERS-1025
Covers: .claude/preship/no_static_scan.py, .claude/preship/preship_gate.py, .claude/preship/owner_approval_capture.py, .claude/preship/test_owner_numbers.py, .github/workflows/preship-verify.yml

## Problem
The no-static gate (.claude/preship/no_static_scan.py, local hook and CI) blocks every new or changed number in risk
files that is not derived from data. Tags exempt nothing, and there is no path for a number the owner chose himself.
SWING-OWNER-1025 (owner-approved) names three owner numbers: top 50 names (config SWING_BREAKOUT_TOP_N 10 -> 50), at most
10 lots (SWING_BREAKOUT_MAX_SLOTS 4 -> 10), r = 0.25 x Kelly (new SWING_BREAKOUT_KELLY_FRACTION = 0.25). The gate blocks
exactly those three config lines, so the approved design cannot ship as approved.

## Design
1. A design record may carry one line: `Owner numbers: <file> <NAME>=<value>; <file> <NAME>=<value>; ...`.
2. When the owner's approval is captured (owner_approval_capture.record), the approval also stores the SHA-256 of the full
   design record text and the parsed owner numbers (board Beck + Schneier: today's fingerprint hashes only the Covers
   list, so a record edited after approval would still match).
3. Local gate (preship_gate dynamic check): a flagged literal passes only when ALL hold —
   - the line is one module-level assignment `NAME = <number>` (an optional minus sign); not an expression, a string
     parsed to a number, an augmented assignment or a second assignment of the same name;
   - a registered approval, re-validated with approval_valid() at this commit (never cached; a `reject <ID>` voids it
     at once), lists that exact file, NAME and numerically equal value;
   - the design record at the approval's stored path hashes to the stored record hash, and every other worktree copy of
     that Approval-ID is identical;
   - the approval came from a Claude-session transcript (the transcript-protected path).
   Each pass prints the approval ID, record hash, file, name and value. Anything else still fails; fail closed on any error.
4. CI has no approval store (owner approval is enforced locally only today, for every design). CI waives the same lines
   only when the committed design record's Owner-numbers line lists that file, name and value; the approval check stays
   in the local hook, the same level at which every owner approval is enforced now.
5. Tests (written first): unregistered approval fails; different value / name / file fails; record edited after approval
   fails; an expression, a string-parsed number, an augmented or second assignment fails; a revoked approval fails;
   a matching approved record passes.
Applied first to SWING-OWNER-1025, which the owner re-approves after this ships (so its record hash is captured).

## Review trail
Gro + GAI (same prompt, options A owner path / B derive replacements / C move numbers to a JSON file): GAI A; Gro B, then
A after counter-evidence (its cited backtests did not exist; B needs a new owner approval). Board Beck: A, with the
record-text hash binding and test-first. Board Schneier: A, with the five anti-forgery checks above; C rejected (hidden
statics, repeats the 2026-10-14 failure); B rejected (ships an unapproved design: r 1.48% -> 2%, maxDD -34% -> -39%).

## Required questions (RQ-4)
Q1: Unblocks the owner-approved Swing build (10 -> 50 names, 4 -> 10 lots): more trades and data; not conservative.
Q2: The gate change alone changes no trade; the Swing build it unblocks raises Swing trades about 3x.
Q3: Needed: an owner choice otherwise either stays blocked or hides in an unscanned file; the narrow path is the safe form.
Q4: Builds on GATES-1017 (owner approval store) and the 2026-10-14 scanner rebuild; no open PR touches either.
Q5: The gate keeps every other number guilty by default; owner numbers are explicitly static and owner-chosen.
Q6: Exposes owner numbers with no review date; forward pass: a dynamic replacement for each (lot cap from margin room).
Q7: Gate tooling only; it connects the owner-approval store to the scanner and fails closed.
Q8: The block is measured: the scanner flags exactly the three config lines (run 2026-10-25); the hash gap is at owner_approval_capture.py covers_sha / record().
Q9: Forgery test suite in item 5; reversal: any forged route passing a test.
Q10: No edge held back by perfectionism; this unblocks an approved design.
Q11: The gate change itself changes no size; the Swing numbers it admits are risk-path and passed the board incl. the masked-loss seat.
Q12: Envelope untouched: stops, the 7% kill, never-mask-a-loss, paper=True, data tiers unchanged.
Q13: Missing or unreadable approval, record, hash or parse -> the literal fails (fail closed).
Q14: Not a shadow: it acts on every commit.
Q15: Every owner-number pass prints its approval ID, record hash, file, name and value.
Q16: The pass line in the commit-hook output of the SWING-OWNER-1025 commit.
Q17: Run on the real staged Swing diff with and without a valid approval.
Q18: No anomaly explained away.
Q19: Gate tooling, owner-approved here before any commit.
Q20: Neutral prompts; Gro's false premises were refuted with evidence, not re-rolled.
Q21: The gate makes no broker calls at all.
Q22: n/a — a local/CI gate, not a server control; it is exercised on the real staged diff before use.
Q23: The forgery suite plus one end-to-end commit through the hook.
Q24: No absence claim is made in this design.
Q25: No market statistics are quoted in this design.
Q26: n/a — local files only, no market data.
Q27: Adds two fields to the existing atomic approval-record write; no new output.
Q28: Pre-commit hook and CI; never on the run_cycle thread.
Q29: It is the only blocker on the approved Swing build, the highest-value open item.
Q30: Approval-to-text binding was missing; this closes it for owner numbers.
Q31: Not applicable because this is gate tooling with no trading replay; the Swing replay covers it.
Q32: Not applicable because this is gate tooling with no trading replay; the Swing replay covers it.
Q33: Not applicable because this is gate tooling with no trading replay; the Swing replay covers it.
Q34: Not applicable because this is gate tooling with no trading replay; the Swing replay covers it.
Q35: Not applicable because this is gate tooling with no trading replay; the Swing replay covers it.
Q36: Not applicable because this is gate tooling with no trading replay; the Swing replay covers it.
Q37: Not applicable because this is gate tooling with no trading replay; the Swing replay covers it.
Q38: n/a — gate tooling; each owner number's own design carries its review.
Q39: n/a — the gate does not size; the Swing r it admits is 0.25 x Kelly from fills.
Q40: Protects "only owner-approved numbers ship"; check = the forgery suite in CI.
Q41: Bind every approval to the approved text, not just its file list (done for owner numbers).
Q42: The owner's own transcript message and the parsed file content.
Q43: A signed exception register bound to the content it approves.
Q44: A cold red-team review that tries each forgery route before ship.
