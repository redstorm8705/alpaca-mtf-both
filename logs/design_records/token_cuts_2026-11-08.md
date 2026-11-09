# TOKEN-CUTS-1108 — cut fixed per-session and per-message token load (design record)

Approval-ID: TOKEN-CUTS-1108
Covers: .claude/preship/post_live_due.py, .claude/preship/test_post_live_due.py, CLAUDE.md, docs/claude_roadmap_log.md (new)
Not changed: every bot module, every gate's decision logic, every rule in CLAUDE.md

Status: rev 1 for owner approval (signed: Claude, 2026-11-08). Not a trading change: no file the bot runs is touched.

## Why (measured 2026-11-08 from the last 7 days of session logs)

- 19 main sessions re-read about 7.1B cached tokens; the average step re-sent about 550K tokens of context.
- The overdue post-live reminder (`post_live_due.py --hook`) adds about 8.6 KB to every owner message. In the largest
  session that was 580 messages × 8.6 KB = 5M characters kept in context and re-read on every later step.
- CLAUDE.md is 156 KB and loads into every session and every subagent; 29 KB of it is a history log (FUTURE ROADMAP LOG,
  the superseded SHADOW STRATEGY TRACKER, Logged Items, PENDING / DEFERRED), not rules.
- Already done outside the repo (owner-approved 11-08): the SessionStart hook injects only the newest handoff block
  (484 KB → 6 KB per session start and per compaction).

## The change

1. `post_live_due.py --hook`: print counts plus a short sample instead of every item.
   - Findings: "[post-live findings OPEN: N]" + the first 3 issue titles (each cut to 60 characters) + where the full
     list is (`gh issue list --label post-live`).
   - Overdue: "[post-live audit OVERDUE: N PRs]" + the 5 oldest PR numbers + where the full list is
     (`python3 .claude/preship/post_live_due.py`) + the same record command as today.
   - The default report mode (full list) and what counts as due / done are unchanged.
   - Test: the existing hook test keeps passing; a new test checks 70 overdue PRs print as one short line naming 5.
2. CLAUDE.md: move lines "## FUTURE ROADMAP LOG" through the end of "## PENDING / DEFERRED" verbatim into
   `docs/claude_roadmap_log.md`, and leave a 4-line pointer section in CLAUDE.md that keeps the add rule ("log future
   ideas in docs/claude_roadmap_log.md before the session ends") and the review rule. No rule text elsewhere changes.
   The North Star, paper-account frame, gates and architecture invariants all stay in CLAUDE.md.

## Expected effect

- About 2K tokens less added per owner message (cumulative over a session).
- About 7K tokens less at every session start, every compaction and every subagent start.

## Reversal criterion

If a session misses a roadmap item or an overdue audit because it was not in view, restore the moved text or raise
the sample size.

## Size / frequency / concurrency delta

Zero on all three: tooling and documentation only; nothing the bot runs changes.

## Owner action (not in this diff)

`~/.claude/settings.json` can only be edited by the owner. To compact the conversation earlier (the biggest lever,
about 57% of measured usage), add `"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"` inside its `"env"` block. Effect on the
1M-context window is not yet verified; measure after one session.

## Required questions

Q1: Indirectly: sessions last longer before the usage limit, so more build and review work fits per week. No trading change.
Q2: No. The bot's trade count is untouched.
Q3: Yes, it should exist: measured waste. Simplest form: print counts plus a sample; move history text verbatim.
Q4: The 11-08 session-start hook change (outside the repo) covers handoff loading; nothing else covers these two.
Q5: The sample sizes (3 findings, 5 PRs, 60 characters) are display limits for tooling, not trading thresholds.
Q6: Static assumption: every reminder must list every item. Forward pass: earlier compaction (owner settings) and a
    token budget line in the session-start briefing.
Q7: Not a signal; no cross-use.
Q8: Yes: 7-day session-log measurement (11-08): 5M characters from this hook in one session; CLAUDE.md 156 KB, history
    block 29,037 bytes (`awk 'NR>=1623 && NR<=1790' CLAUDE.md | wc -c`).
Q9: Not a strategy. Expected: ~2K tokens less per owner message, ~7K less per session start / compaction / subagent.
    Reversal: a missed roadmap item or overdue audit.
Q10: No validated edge is involved; this is tooling only and enables nothing.
Q11: No: zero on size, frequency and concurrency.
Q12: No: stops, kill switch, paper flag and data tiers are untouched by this tooling change.
Q13: The hook keeps its never-raise wrapper; on any error it prints the existing error line.
Q14: No shadow: the shortened reminder and the moved log are live immediately.
Q15: Not a trading decision; the full list stays one command away (`post_live_due.py` default mode, `gh issue list`).
Q16: The next owner message shows the short "[post-live audit OVERDUE: N PRs]" line.
Q17: Run the hook with `--hook` against the real repo state and check the output size.
Q18: No anomaly is being explained away; the waste was measured and is being removed.
Q19: No entry, exit, sizing or routing change; tooling and documentation only.
Q20: Yes: facts only in the record; the measurement is reproducible from the session logs.
Q21: No: the hook reads git refs and the GitHub issue list only, never the broker.
Q22: Not a fail-closed control.
Q23: The hook run in Q17 is the full path.
Q24: N/A — the design makes no existence or tradability claim.
Q25: N/A — no stock or market statistics are used.
Q26: Local git refs and `gh issue list` (unchanged).
Q27: stdout of the hook (unchanged); `docs/claude_roadmap_log.md` (new, plain markdown).
Q28: UserPromptSubmit hook (unchanged position); CLAUDE.md loads at session start.
Q29: Owner-ranked P0 on 11-08 (sessions burning out too fast).
Q30: Standard practice: keep always-loaded context to rules, store history on demand.
Q31: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q32: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q33: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q34: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q35: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q36: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q37: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q38: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q39: N/A — tooling and documentation change; no strategy, replay or trading result is involved.
Q40: Protects session length; check: the hook output under 1 KB with 70 overdue PRs (test).
Q41: Measure every fixed load (done 11-08) and cut the largest first; the largest (compaction) needs the owner's setting.
Q42: The raw session transcripts' per-step usage fields.
Q43: Context budgeting: fixed load small, history retrieved on demand.
Q44: The next week's session-log measurement with the same script.
