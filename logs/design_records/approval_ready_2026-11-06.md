# APPROVAL-READY-1106 — no approval ask reaches the owner until the approval would record (design record)

Approval-ID: APPROVAL-READY-1106
Covers: .claude/preship/approval_ready.py (new), .claude/preship/test_approval_ready.py (new), .claude/settings.json, scripts/install_managed_gates.sh, CLAUDE.md

Status: rev 3 FINAL DRAFT for owner approval (board, GAI, Gro rounds folded 2026-11-06; Codex at its usage limit). Rev 1 DRAFT (signed: Claude, 2026-11-06). Owner request 2026-11-06: "You need to make sure all requirements
are completed before bringing to me. I'm tired of having to double approve. Confirm that this will change for all
approvals moving forward."

## Why (measured 2026-11-06 from every project transcript, read-only)

34 designs were approved by the owner between 2026-10-17 and 2026-11-06. **9 of them needed the owner to send the
approval again, 12 extra messages in all.** Causes, from the transcripts and the approval hook's own output:

| Cause | Designs (extra messages) |
|---|---|
| The design record was incomplete when brought: required questions unanswered, thin or unreadable answers, Covers list missing or short | DAY-UNIVERSE-1027 (1), CATALYST-DYNAMIC-1104 (1), REGIME-SIZE-1104 (2), POST-LIVE-WEEKLY-1106 (1) — 5 |
| The approval reader did not read the owner's wording ("with changes"; a short sentence after the approval line) — since fixed | CONV-1018 (1), EARN-SOURCE-1029 (1), SWING-OVERNIGHT-1029 (1) — 3 |
| The design changed after approval on the owner's own new direction (a legitimate re-approval) | REGIME-SIZE-1104 (1) — 1 |
| Cause not confirmed from the logs | SWING-OWNER-1025 (2), OWNER-NUMBERS-1025 (1) — 3 |

In 8 more designs the hook printed "NOT recorded" for an incomplete record, and the approval was picked up later
without the owner re-sending (HALT-1019, REPLAY-1019, SLACK-0930, KILL-ATTR-1023, REVIEWER-SWAP-1025,
DAY-UNIVERSE-1027, TRACKB-SIZE-ETF-1028, GAP-EXIT-1029). Every one of those packages was also brought incomplete.

Root cause: the checks that decide whether an approval records (`owner_approval_capture.record`: one unambiguous
design, every worktree copy agreeing, required questions answered) run only AFTER the owner replies. Nothing runs
them BEFORE the ask is sent. And nothing checks that the Covers list names every file the design says it will change
(the 11-06 REGIME miss: 13 of 25 files listed).

## The fix

`.claude/preship/approval_ready.py`, two modes, sharing the capture hook's own functions so the two cannot drift:

1. **CLI, run while preparing a package:** `python3 .claude/preship/approval_ready.py <ID> [<ID> …]` prints READY or
   the exact list of what is missing.
2. **Stop hook (blocking):** when my turn-ending message asks the owner to approve an Approval-ID (the capture hook's
   existing `_ASK_TEMPLATE` / `APPROVE <ID>` patterns), the hook runs the same checks for every ID named and blocks the
   message if any fails, printing what is missing. I must fix the record and only then send the ask. Unlike
   `execute_dont_ask_gate.py` it does NOT let the second attempt through (`stop_hook_active` is ignored): the checks
   are deterministic, so a re-sent unfixed ask fails again. It cannot trap the turn: a message that does not ask for
   approval always passes, so a record I cannot finish this turn is reported as a problem, not as an ask.

Checks (an ID is READY only when all pass):
1. **One design, every copy the same:** `find_design` resolves the ID — exactly one design file declares it and every
   worktree copy has the same Covers list. On failure it names each copy that differs.
2. **Required questions:** `required_questions.unanswered` returns nothing for the record.
3. **Covers present:** a non-empty Covers list.
4. **Covers complete:** every repository path the record names (an existing tracked file, or one marked `(new)`) that
   the ship gate treats as gated (`preship_gate._is_gated`, the same function the commit gate uses) is either in Covers
   or listed on a `Not changed:` line in the record. This is the check that would have caught the 11-06 miss.
5. **Owner choices named in the ask:** a record line `Owner choices: <name> = <option> | <option>` means the ask must
   name each choice and its options, so a pending choice cannot be left out (the VIX < 14 row on 11-06). The capture
   hook already accepts an approval and the choice in one message.
6. **The reply parses:** the exact line the ask tells the owner to send (`APPROVE <ID>`) is run through the capture
   hook's parser and must yield that ID.
7. **No repeat ask for an ID whose approval is on record for these same files:** if the ID is already recorded with the current Covers fingerprint,
   the ask is blocked as unnecessary (asking again is itself a double approval).

## Rev 2 — GAI + Gro round folded (2026-11-06, signed: Claude; Codex at its usage limit, Gro reviewed as reviewer #2)

Both asked for changes; neither rejected.
1. **Fail closed on an internal error (Gro 1):** if a check raises (unreadable record, corrupt approval state), the
   ask is blocked and the error and its stack trace are printed. This cannot trap a turn: a message that does not ask
   for approval always passes, so the error is reported to the owner as a problem instead. Supersedes Q13 rev 1. GAI's
   nit (fail open on corrupt state for check 7) is not adopted for the same reason.
2. **`Not changed:` lines are verified (GAI 2):** a path listed there must have no change in the worktree's diff against
   origin/main; if it does, check 4 fails. (The commit gate separately refuses commits to files outside Covers.)
3. **Tests (Gro 2):** the error path; a gated path listed under `Not changed:` passes; a gated path listed nowhere
   fails; a `Not changed:` path that is modified fails; replay of the 9 past failures blocked and the complete ones
   passed.
4. **Not adopted (GAI 1, the second attempt passing after a fingerprint change):** the block applies to my own
   message, not the owner's; once I fix the record the same checks pass on the next attempt, so a fingerprint
   exception adds nothing, and without the fix a second attempt must stay blocked.

## Rev 3 — board round folded (2026-11-06, signed: Claude; Kent Beck seat, cold, approve-with-changes)

Supersedes the check list above where they differ.
1. **Owner numbers checked (R1):** `parse_owner_numbers` must not return None for the record (record() refuses a
   malformed `Owner numbers:` line, capture:540-542).
2. **Every copy checked (R2):** checks 2 and 1 (owner numbers) run on every worktree copy, not only the one found
   first; a stale copy fails with its path named.
3. **The reply is simulated exactly (R3):** check 6 calls the capture hook's own `resolve(reply, agent_text=<the exact
   ask message>, root)` for both a bare `approved` and the full reply the ask tells the owner to send, and each must
   return exactly the asked IDs. So an ask naming two IDs, an ask sentence the reader drops (e.g. one containing "or"),
   or a choice line the reader rejects all fail before the owner sees them. Rev 1's claim that the capture hook accepts
   a choice line is withdrawn: the check simply refuses any reply format that does not record, and I phrase the ask so
   it does.
4. **Only IDs actually asked (R4):** the IDs checked are those `ids_requested()` returns for the message, with the
   capture hook's `_NEGATED_ASK` exclusion, so a report of a recorded approval or a plan mentioning a future decision is
   not treated as an ask.
5. **`(new)` normalised (R5):** Covers entries are compared without their `(new)` marker.
6. **Check 4 scope (board 4):** a `path:NNN` citation is evidence, not a file the design changes, and is exempt. Replay
   pass criterion: zero check-4 blocks on the past records that recorded on the first message.
7. **Corrected expectations (board 5):** the pre-ask check cannot catch the reader-wording failures (since fixed). The
   replay must flag the 4 incomplete-record designs that needed re-sending plus the 8 that printed NOT recorded, and
   pass the others. Q9, Q23, Q37 and Q40 are corrected below.
8. **Block text** says "remove the approval ask, or fix the record", so the next step is always explicit. Blocks are
   not capped: the checks are deterministic (Rev 2 item 4).
9. **register_designs** is called after a READY result so a later replay of the approval binds the same design.

10. **Confirmation round on Rev 3 (2026-11-06):** Gro approve; GAI approve-with-changes. Folded: a test where a
   design record cites a gated file as `path:NNN` and check 4 passes. Not adopted: GAI's change to the capture hook so a
   re-sent approval of a recorded ID is suppressed; a re-send of an unchanged design already records the same
   fingerprint and costs nothing, and the capture hook is outside this design's Covers.

What stays the same: a Covers change still cancels an earlier approval (an approval binds the exact files); the
capture hook is unchanged; the owner's wording is unchanged.

## Required questions

Q1: Moves data collection indirectly: owner approval time is the gate on every shipped change, and re-sent approvals have delayed builds.
Q2: No change to the number of trades; this is review tooling only.
Q3: Simpler option tried: written rules to run the checks first; they failed 9 times, so the check is now enforced by a hook.
Q4: Builds on owner_approval_capture.py (OWNER-APPROVAL gate), required_questions.py and execute_dont_ask_gate.py; no open PR covers this.
Q5: No decision thresholds: every check is pass or fail against the design record and the capture hook's own logic.
Q6: Exposed static assumption: that the author completes a package before asking. Next: the same pre-check for preship markers.
Q7: Shares functions with the capture hook and the commit gate so all three agree; a failure only blocks a message, never trading.
Q8: The counts come from a read-only scan of every project transcript (scratchpad approval_history2.py / ah3.py, 2026-11-06) and the hook's own NOT recorded lines.
Q9: Replay before ship over every past design record at its first ask: must flag the 4 incomplete designs that needed re-sending and the 8 that printed NOT recorded, and pass the rest with zero check-4 blocks.
Q10: Not a trading edge; nothing is being held back by calibration polish here.
Q11: No change to size, trade frequency or exposure; it never touches the trading path.
Q12: Does not touch stops, the kill switch, loss reporting, paper mode or data tiers.
Q13: If the check itself errors, the ask is blocked and the error printed; a message without an ask always passes, so nothing is trapped.
Q14: Not a shadow: it blocks incomplete approval asks from the first day.
Q15: Every block prints the ID and each failed check in the session, so a refused ask can be traced from the transcript.
Q16: Proof it ran: the hook's READY or NOT READY line in the transcript of the first approval ask after merge, checked by the session that builds it.
Q17: Runtime proof: the first real approval ask after merge must show the READY line, and the next owner approval must record on the first message.
Q18: Exists because repeated re-approvals were treated as one-off slips rather than a missing check.
Q19: Changes no entry, exit, sizing or routing; it is gate tooling, approved by the owner like any gate change.
Q20: Review prompts will carry the counts and the code facts only, with no verdict wording.
Q21: Makes no broker calls of any kind; tests use temporary design records only.
Q22: Fail-closed on errors (rev 2); it is run read-only on the real worktrees and past records before merge.
Q23: Full-path check before ready: the replay above plus test ask messages for each failure class must behave as stated in Q9.
Q24: No claim of the form something does not exist is made; the record paths are checked against git.
Q25: No market statistics are used; the job reads design records and transcripts only.
Q26: No market data: it reads design records, git and the capture hook's state in the git common directory.
Q27: Writes nothing; it only reads files and prints to the hook output.
Q28: Runs as a Stop hook beside execute_dont_ask_gate.py and as a command line tool; never on the trading server process.
Q29: High: owner time is the scarcest input, and this removes the most frequent repeat request he has raised.
Q30: Whitespace: a pre-flight check before asking for sign-off, which release processes normally have and ours lacked.
Q31: Not an alpha claim; the replay over all 34 past records is the whole population, not a sample.
Q32: The replay reads each record at the commit of its first ask, so no later fix is used to judge an earlier package.
Q33: No trading costs are involved because the tool places no orders.
Q34: No return is claimed, so there is no market beta to separate out.
Q35: Behaviour is the same in every market regime because it reads only design records.
Q36: Capacity is not affected because it runs only when an approval is asked.
Q37: The population is all past approvals: 12 incomplete-record cases in scope, the wording and legitimate cases out of scope.
Q38: Decay shows as an approval that fails to record despite a READY line; each such case is logged and fixed.
Q39: Sizes nothing and reads no profit or loss, so Kelly sizing is untouched.
Q40: Outcome owned: every owner approval records on the first message; counted weekly from transcripts, excluding legitimate re-approvals after a design change.
Q41: An expert release manager would also show the owner the readiness result inside the package, which the ask will include.
Q42: Rawest data used: the design records themselves and the capture hook's own parser, not a summary of them.
Q43: Standard method: a pre-merge readiness check that runs the same validation as the merge gate, shared code, fail-fast.
Q44: Independent evidence: the replay over past records, and the weekly count of re-sent approvals going to zero.
