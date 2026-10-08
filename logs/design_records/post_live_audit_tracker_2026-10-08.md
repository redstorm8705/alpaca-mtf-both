# Post-live audit tracker — design record (2026-10-08, Claude)

**Mandate (Rafael, 2026-10-08):** "Moving forward, I want all new diffs to be audited and back tested after they go
live. We need to catch bugs and fix them immediately after a full day of trading."

**Why a mechanism:** the same day's top-to-bottom day-tier audit found a live bug no pre-ship gate could see (Track B
read one empty IEX 5-min bar as a halt and benched AMD on 122 ticks; fixed in PR #532). Per CLAUDE.md
DOCUMENTATION-IS-NOT-ENFORCEMENT the rule is carried by a tracker, not a note.

**Design**
- `.claude/preship/post_live_due.py`: lists PRs merged to origin/main (number >= 531) that changed bot code
  (.py/.sh outside tests/.claude/.github/research/logs/docs, or config). A PR is DUE once one full regular session
  (Mon-Fri 09:30-16:00 ET, holidays not modelled = strict) has started after its merge and ended.
- `.claude/preship/record_post_live.py <pr> --session --verdict PASS|BUGS_FIXED|BUGS_OPEN --notes`: appends to
  `logs/post_live_audits.jsonl` (committed with the session's docs; the tracker reads origin/main + the working file).
- UserPromptSubmit hook (`post_live_due.py --hook`): prints the OVERDUE list on every prompt until each is recorded.
  Read-only, never fetches, never fails the prompt.
- What a post-live audit is: prove the new code executed in production (log lines), replay its decisions (entries,
  skips, exits) against that session's bars/fills, check for errors/regressions in the changed path, fix any bug at
  once through the normal gate, then record the verdict.

**Not built (follow-up option):** blocking new bot-code commits while an audit is overdue (a hard gate in
preship_gate.py). The prompt hook surfaces it every turn; escalate to a block if an audit is ever skipped.

**Data / failure modes:** git log of origin/main only (local ref); unreadable git -> nothing listed (a missed reminder,
never a broken prompt). No market data, no trading path, no RTH impact.
