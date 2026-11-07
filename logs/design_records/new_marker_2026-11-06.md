# NEW-MARKER-1106 — a Covers entry marked "(new)" covers its file (design record)

Approval-ID: NEW-MARKER-1106
Covers: .claude/preship/owner_approval_gate.py, .claude/preship/test_owner_approval.py

Status: rev 2 FINAL DRAFT for owner approval (board Schneier seat, GAI, Gro folded 2026-11-06; Codex at its usage limit). Rev 1 DRAFT (signed: Claude, 2026-11-06).

## Why (measured 2026-11-06, read-only)

Design records mark files a change will create as `path (new)` in their Covers line (10 current records do). The
approval gate's `covered()` (`.claude/preship/owner_approval_gate.py:256-262`) compares each Covers entry to the path
exactly, after only `strip()` and `rstrip(",")`, so the VIX reader's entry with its marker never equals the bare file name.
Measured with the live store: `covered('.claude/preship/approval_ready.py', approvals)` returns False although
APPROVAL-READY-1106 is approved with that file plus the marker in Covers; `.claude/settings.json`
(no marker) returns True. `covered()` is used by the edit gate, the commit/push hook
(`owner_approval_githook.py:39`) and `behaviour_integrity.py:129/143`.

Effect: an approved design that creates a new trading-behaviour or gate file cannot be built. Today that is
`run_vix_snapshot.py` (REGIME-SIZE-1104) and `.claude/preship/approval_ready.py` (APPROVAL-READY-1106). Removing the
marker from those Covers lines would change their fingerprints and cancel both owner approvals; fixing the gate does
not. (New files under `scripts/` and `tests/` are not trading-behaviour paths, which is why earlier designs such as
LOSS-PM-1104 shipped their new scripts.)

## The fix

In `covered()`, drop a trailing `(new)` marker (any case, surrounding spaces) from each Covers entry before
comparing. Nothing else changes: the marker is the only annotation used on Covers lines; the Covers fingerprint is
computed elsewhere (`owner_approval_capture.covers_sha`) on the raw list and is untouched, so no recorded approval
changes. Tests: a `(new)` entry covers its path; it does not cover a different path; directory (`/`) and prefix
(`*`) entries behave as before.

Order: strip, drop a trailing comma, remove one trailing `(new)` (case-insensitive), strip again. Only one marker is
removed, so an entry carrying the marker twice stays uncovered.

**Rev 2 — review folded (2026-11-06):** Schneier seat approve; Gro approve; GAI approve-with-changes.
1. The marker is removed only when it ends the entry (GAI), with a test that a `(new)` entry covers its own path.
2. An empty prefix never covers (Schneier nit): after normalisation, an entry that is empty, `*` or `/` covers
   nothing, so `* (new)` cannot cover every path. Checked 2026-11-06: no current Covers entry is a bare `*`, `/` or
   empty, so no recorded approval changes. Tests: `(new)` alone and `* (new)` cover nothing.

Not changed: .claude/preship/owner_approval_capture.py, .claude/preship/owner_approval_githook.py, .claude/preship/behaviour_integrity.py, .claude/settings.json

## Required questions

Q1: Unblocks two approved builds (the 2-minute VIX reader and the approval readiness check) without new owner approvals.
Q2: No change to the number of trades; this is approval tooling only.
Q3: Simpler alternative is editing three Covers lines, which cancels two owner approvals; the gate fix is smaller and keeps them.
Q4: Builds on GATES-1017 (owner_approval_gates design); no open PR touches covered().
Q5: No thresholds; a string normalisation only.
Q6: Exposed assumption: Covers entries are bare paths. Next: APPROVAL-READY-1106 normalises the same marker when it checks Covers.
Q7: The same covered() serves the edit gate, the commit hook and the integrity check, so one fix covers all three.
Q8: Measured: covered() returned False for the approved new file and True for a bare entry, from the live approval store.
Q9: Replay before ship: every current design record's Covers entries will be run through the fixed covered(); expected result is that only marked entries change, from not covered to covered.
Q10: Not a trading edge; nothing is held back by calibration polish here.
Q11: No size, frequency or exposure change; it never touches the trading path.
Q12: Does not touch stops, the kill switch, loss reporting, paper mode or data tiers.
Q13: An entry that is only the marker, or empty, covers nothing, as before.
Q14: Not a shadow: it changes gate behaviour from the first commit.
Q15: The gate's existing block messages are unchanged and still name every uncovered path.
Q16: Proof it ran: the first commit of run_vix_snapshot.py or approval_ready.py passes the owner-approval hook.
Q17: Runtime proof: that commit, made through the real git hook, not a test harness.
Q18: Exists because an approved build was found blocked before it was attempted, not explained away.
Q19: No entry, exit, sizing or routing change; it is gate tooling approved by the owner like any gate change.
Q20: Review prompts carry the code lines and measurements only, with no verdict wording.
Q21: Makes no broker calls of any kind; tests use temporary design records only.
Q22: Run read-only against the live approval store before ship, as measured above.
Q23: Full-path check before ready: the test suite plus a dry run of the commit hook on the two blocked files.
Q24: The claim that 10 records use the marker was counted with grep over logs/design_records on 2026-11-06.
Q25: No market statistics are used; the change reads design records only.
Q26: No market data; it reads design records from the worktrees.
Q27: Writes nothing; covered() is a pure function.
Q28: Runs inside the existing PreToolUse gate, git hooks and PostToolUse integrity check; never in the trading process.
Q29: High: two approved builds are blocked without it, and the alternative costs two owner re-approvals.
Q30: Whitespace: the gate had no test for annotated Covers entries; this adds one.
Q31: Not an alpha claim; the replay covers every current record.
Q32: The replay reads the records as they are on disk now; no later state is used.
Q33: No trading costs are involved because the tool places no orders.
Q34: No return is claimed, so there is no market beta to separate out.
Q35: Behaviour is the same in every market regime because it reads only design records.
Q36: Capacity is not affected; it is a string comparison per Covers entry.
Q37: The population is every current Covers entry; all are checked by the replay.
Q38: Decay would show as an approved file blocked by the gate; the block message names it.
Q39: Sizes nothing and reads no profit or loss, so Kelly sizing is untouched.
Q40: Outcome owned: an approved new file can be committed; checked by the commit hook passing on the first build commit.
Q41: An expert would also make the readiness check flag annotations the gate cannot read; APPROVAL-READY-1106 does.
Q42: Rawest data used: the Covers lines in the design records and the live approval store.
Q43: Standard method: normalise annotations at the comparison point and pin it with a regression test.
Q44: Independent evidence: the real commit hook accepting the two blocked files.
