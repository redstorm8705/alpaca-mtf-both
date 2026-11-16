# APPROVE-BOX-VALID-1114 — an owner approval that is recorded must also pass the gate

Approval-ID: APPROVE-BOX-VALID-1114
Covers: .claude/preship/owner_approval_capture.py, .claude/preship/test_owner_approval.py, .github/workflows/preship-verify.yml
Not changed: .claude/preship/owner_approval_gate.py, .claude/preship/approval_ready.py, .claude/preship/investigate_propose_gate.py, .claude/preship/test_investigate_propose_gate.py
Status: rev 1 (signed: Claude, 2026-11-16). Design only; nothing written to the covered files.

## Owner direction (verbatim)
2026-11-06: "I'm tired of having to double approve". 2026-11-12: "If it's in the app box, me selected approved should
register". 2026-11-14: "focus on shipping the 4 or 5 queued items that are ready to ship."

## Problem (measured 2026-11-16, read-only run over the 70 stored approval records)
The capture hook writes an approval record; the gate later re-proves that record against the session file
(`approval_valid`). Two cases record and then fail the re-proof, so the owner is asked twice:

1. Box question. When the turn before the owner's reply ended with a question in the app's box, the capture hook
   (`_last_turn`, line 847) stores the hash of "last agent text + box question". The hook usually runs before the
   owner's entry is written, so the record has no message identity and `approval_valid` (line 782) pins the turn by
   that hash, but it rebuilds the agent text through `_read_new` (line 964), which never adds the box question. The
   hashes differ and the record is refused. Measured on INVESTIGATE-GATE-1113 on 11-14 (plain text: no match; text +
   box: match).
2. App-added reminder. In a worktree session the app sometimes stores the owner's message as two text parts: a
   `<system-reminder>` part it added, then the owner's words. `_claude_text` joins both, `_NOT_HUMAN` sees
   `<system-reminder>` and the whole entry is refused as owner text. 15 of 1,717 owner-typed entries in this project's
   session files are of this kind (example: the owner's "Yes, both stand", 2026-11-14 16:54 UTC).

Not fixed here, stated: an approval typed inside a slash command's arguments (23 of 1,717 entries; QHM-OWNERBUY-1113's
11-14 record is one). Its record hash matches no stored form of the message, so it cannot be re-proved. The owner
re-types that one approval as a plain message.

A trial that made `_read_new` always add the box question turned 29 currently valid records invalid (they were stored
with the plain text). The fix therefore accepts either form and never replaces one with the other.

## Change
1. `approval_valid`: where it compares the record's agent hash with the turn's agent text (the no-identity pin and the
   affirmative branch), it accepts the plain text hash OR the hash of the same turn's "agent text + box question", read
   with the existing `iter_turns` (the function the capture hook's rules already mirror). In the affirmative branch the
   request sentence is looked for in the form whose hash matched. Nothing else in `approval_valid` changes: the owner
   message hash, the transcript location check, and the naming rules stay as they are.
2. `_claude_text`: for a list content with more than one text part, a part that is wholly one
   `<system-reminder>...</system-reminder>` block is left out. The remaining text still goes through `_NOT_HUMAN`, and
   `transcript_user_text` still requires `origin.kind == "human"`, a uuid, not meta, not sidechain, not a summary.
   A single-part message that contains a reminder tag is refused exactly as today.
3. CI: `.github/workflows/preship-verify.yml` also runs `.claude/preship/test_investigate_propose_gate.py`.

## Tests (added to test_owner_approval.py; each written to fail on today's code first)
- typed `APPROVE <ID>` after a box-question turn, record without identity: valid;
- the same with a plain-text record (the 29 existing records' shape): still valid;
- record whose agent hash matches neither form: refused;
- affirmative "approved" click after a box question, no identity: valid; with a later agent message replacing the box
  question: refused;
- two-part owner message (reminder + words): accepted as the words; single-part text containing a reminder tag:
  refused; a reminder-only entry: refused; a tool-result entry: refused; a slash-command entry: refused.
- added from the Codex design review (2026-11-16): box asks about ID-B and the owner types `APPROVE ID-A`: ID-B is not
  approved; agent text requests ID-A while the box requests ID-B and the owner clicks "approved": only the form whose
  hash was recorded counts, and only for the one ID it requests; two turns with identical owner text but different box
  questions: a record hashed for one does not validate against the other; several reminder parts before the words;
  a reminder part after the words; a reminder part with surrounding whitespace (accepted as "wholly a reminder" after
  strip); a reminder embedded inside one text part with other words: refused; an unclosed reminder tag: refused;
  nothing left after dropping reminder parts: refused; a tool_result anywhere in the list: refused.
- "the same turn" is enforced by construction: both forms are computed for the one owner entry whose text hash already
  matched, from the agent text and box question that precede that entry; no other turn's text is consulted.
- replay check at build time (record by record, not totals): `approval_valid` over all stored records before and after; the only allowed differences
  are records turning valid.

## Design review (2026-11-16, same prompt to both through bgg_call.py)
- Codex: approve the design, with the added tests listed above (now in Tests).
- GAI: round 1 reject on three objections (an OR across forms could match a superseded box question; reminder removal
  alters owner text; store both hashes instead). Round 2, after the mechanics of each were set out (both forms belong
  to the one owner entry whose text hash matched; only a whole separate reminder part is dropped; storing both hashes
  gives the same acceptance set and cannot repair stored records): approved.
- Nits kept for the build: one shared helper returns the two forms so the recorder and the validator cannot drift
  (Codex); tolerate whitespace around a whole reminder part (both). Not taken: logging which form matched (GAI) —
  `approval_valid` returns only yes/no and nothing reads such a log today.
- No board seat was convened: there is no open fork and no trading path is touched.

## Required questions
Q1: Removes repeat approvals that stall ready builds; no trading change.
Q2: No trade-count change.
Q3: Should exist: the recorder and the gate must agree. Simplest form: accept the two text forms the recorder itself produces.
Q4: Builds on APPROVE-BOX-1112 (PR 967) and APPROVAL-READY-1106; no open PR touches these functions (origin/main 306564e).
Q5: Not applicable because no number or threshold is introduced; the change compares text hashes.
Q6: Forward: have the gate print "recorded but not provable" at the next tool call, so a failed re-proof is visible at once.
Q7: Not applicable because it is approval tooling, not a trading signal.
Q8: Cases: the INVESTIGATE-GATE-1113 record of 11-14, the 16:54 UTC two-part message, the 29 plain-text records.
Q9: Replay over all 70 stored records before and after the change, stated in Tests.
Q10: Not applicable because no trading signal is involved.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change. The approval gate stays: only owner-typed text in an app session file counts.
Q13: Unreadable transcript or record: refused, as today.
Q14: Not a shadow: it changes what the gate accepts.
Q15: Not applicable because no trade decision is made here.
Q16: Proof: a typed approval after a box-question turn lets the covered write through on the first reply.
Q17: The first approval typed after a box question once this is merged.
Q18: The anomaly closed: "recorded" followed by a blocked write.
Q19: Gate file: owner approval required by the approval hook; GAI + Codex review the exact diff before it ships.
Q20: Neutral; tests included.
Q21: No broker contact; tests use temporary session files.
Q22: Not applicable because it is not a trading control.
Q23: Unit tests plus the 70-record replay.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because it uses no market statistic.
Q26: Reads session files only; no market data.
Q27: Writes nothing new; the validity cache is unchanged.
Q28: Runs inside the existing prompt hook, tool-call gate and git hook.
Q29: Owner direction 11-06, 11-12 and 11-14; it unblocks the ready queue.
Q30: Blind spot: the recorder and the validator kept two separate copies of "what the agent showed".
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no market replay.
Q33: Not applicable because there is no trading cost.
Q34: Not applicable because there is no return stream.
Q35: Not applicable because no market regime is involved.
Q36: Not applicable because there is no sizing or liquidity.
Q37: Not applicable because no statistical edge is claimed.
Q38: Reversal: any stored record that is valid today turns invalid, or text the owner did not type is accepted.
Q39: Not applicable because it sizes nothing.
Q40: Outcome: every approval the hook reports as recorded passes the gate; machine check = the new tests in CI.
Q41: Next: the slash-command case, by having the hook refuse to record it and say so.
Q42: The stored approval records and the session files.
Q43: Same pattern as a signature check that accepts each canonical form the signer can emit.
Q44: The first box-question approval after merge, and the 70-record replay output.
