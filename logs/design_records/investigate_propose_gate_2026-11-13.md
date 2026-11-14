# INVESTIGATE-GATE-1113 — every investigation reply must carry a fix, a prevention plan and a tracked bookmark

Approval-ID: INVESTIGATE-GATE-1113
Covers: .claude/preship/investigate_propose_gate.py (new), .claude/preship/test_investigate_propose_gate.py (new), .claude/settings.json
Not changed: .claude/preship/execute_dont_ask_gate.py, .claude/preship/no_guess_gate.py
Status: rev 1 (signed: Claude, 2026-11-13). Draft written and tested in the worktree; not registered, not committed.

## Owner direction (2026-11-13, verbatim)
"Stop just auditing and diagnosing the issue. You need to be dynamic and investigative. You need to bookmark it and
revisit with a dynamic and optimized plan to prevent something like this from happening again. This needs to be the
case whenever I ask you to investigate something. Do not ever just report what happened without a purpose solution
again. Create a mechanical gate or hook to enforce this."

## Problem (measured)
The 11-13 reply on the NVDA CEO exit reported what the logs showed ("the bot did not catch it", the mis-keyed headlines,
no QHM review) and said "I'm starting the fixes now" with no fix named, no prevention plan and no tracked item.
Nothing mechanical checked that.

## Change
A Stop hook beside execute_dont_ask_gate.py and no_guess_gate.py (same once-per-turn pattern, `stop_hook_active`):
- Fires when the owner's latest typed message asks for an investigation (did the bot catch / why did / what happened /
  investigate / look into / post mortem / audit / root cause ...), or the reply itself reports a failure or root cause.
- Then the reply's last 4,000 characters must carry all three, or the stop is blocked with what is missing:
  1. FIX: the proposed solution (fix, design, build, change, proposal, recommend);
  2. PREVENTION: how it is stopped next time (prevent, never again, guard, gate, check, alarm, automatic ...);
  3. BOOKMARK: an Approval-ID (e.g. HOLD-NEWS-REVIEW-1113), a logs/design_records/ path, or a #issue/PR number.
- Unreadable input or transcript never blocks. Tool results and system notices are not read as the owner's words.
- Registered in .claude/settings.json under "Stop", after no_guess_gate.py.

## Tests (8, all pass locally: `cd .claude/preship && python3 -m unittest test_investigate_propose_gate`)
report-only blocks and names all three; full answer passes; missing bookmark only; failure words trigger without an
ask; unrelated turn passes; fires once per turn; unreadable input never blocks; owner text skips tool results/notices.

## Required questions
Q1: Turns every investigation into a fix toward the goal; no trading change.
Q2: No trade-count change.
Q3: Should exist (owner). Simplest: one regex Stop hook on the existing pattern.
Q4: Sits with execute_dont_ask_gate.py (no menus) and no_guess_gate.py (no unverified claims); no overlap.
Q5: The 4,000-character window is a text window (mechanics), not a trading threshold.
Q6: Forward: a gate on the bookmark itself (the named design record must exist on disk).
Q7: Not applicable because it governs replies, not trading signals.
Q8: The 11-13 NVDA reply is the measured case.
Q9: Replayed on that reply in the tests (blocks); a compliant version passes.
Q10: Not applicable because it governs replies, not a trading signal.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change.
Q13: Bad input never blocks (tested).
Q14: Not a shadow: it blocks.
Q15: The block message names what is missing.
Q16: Proof: the block text "INVESTIGATE->PROPOSE GATE" in a session.
Q17: First investigation reply after registration.
Q18: The NVDA reply is the anomaly this closes.
Q19: Not a trading change; owner approval required for gate files by the approval hook.
Q20: Neutral; tests included.
Q21: No broker contact: the hook reads only the session transcript; tests use temp files.
Q22: Not applicable because it is not a trading control.
Q23: Unit tests pass.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because it uses no market statistic.
Q26: Not applicable because it reads no market data; its input is the session transcript.
Q27: It writes nothing; it only exits 2 with a message on stderr.
Q28: Claude Code Stop hook, after execute_dont_ask_gate.py and no_guess_gate.py; once per turn.
Q29: Owner-ordered on 11-13; it closes a repeated reporting failure.
Q30: Blind spot closed: investigation replies had no completeness check.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no replay of market data.
Q33: Not applicable because there is no trading cost.
Q34: Not applicable because there is no return stream.
Q35: Not applicable because no market regime is involved.
Q36: Not applicable because there is no sizing or liquidity.
Q37: Not applicable because no statistical edge is claimed.
Q38: Reversal: an investigation reply passes without a fix, prevention and bookmark, or a routine reply is blocked.
Q39: Not applicable because it sizes nothing.
Q40: Outcome: no investigation reply ends without a fix, prevention and bookmark; machine check = this hook.
Q41: Next, verify the bookmark exists (file on disk / open issue).
Q42: The session transcript.
Q43: A pre-commit-style lint on replies, as the repo already does for menus and unverified claims.
Q44: The first blocked and the first passing investigation replies in a live session.
