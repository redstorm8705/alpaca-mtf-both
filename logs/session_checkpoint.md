# SESSION CHECKPOINT — 2026-08-23 (cross-account handoff, Rafael near weekly limit)

**Pickup:** `git pull` → read this file → read handoff.md → query Master Brain. Rafael's next-work
directive: **start with items that are DESIGNED but NOT yet shipped** (list in §3).

---

## 1. SHIPPED & LIVE this session (verified)
- **QHM STATUS report** — `scripts/qhm_report.py`, OCI cron **Sundays 9am PT** + **Block Kit** legibility +
  `.env`/401 fix (PRs #165, #166, #167). Live-verified (posts to Slack, heartbeat ok).
- **Slack format spec** — `rules/slack_format.md` (SLK01–SLK15) + `rules/CORE.md` B12. BINDING on all reports
  (incl. the no-truncation rule: full report chunked across ordered messages, never a "see full report" stub).
- **`alerts.py send_slack_blocks`** — Block Kit sender (PR #167).
- *(Earlier in session)* monthly + weekly report single-source (#160/#161/#162); rulebook consolidation
  foundation (#163).

## 2. FULL QUEUE (from the entire session)
**Highest-priority verified bug:** MRI news-noise haircut (GEO_CONFLICT) — source-verified the bot ran at
reduced size **76% of the time, 94% junk-news-driven** (−15% to −30% haircut on most entries). Root fix =
MRI news bonus gated on SPY's OWN move + a rate-driven risk-off signal (bond leg is rate-blind). Risk-path.
See recommendations_backlog.md "MRI ROOT REDESIGN".

Other groups (detail in recommendations_backlog.md + design records):
- QHM research→auto-execution pipeline (big, risk-path) — see §3.
- Slack WS2/WS3 (migrate weekly-postmortem, monthly, gex, score16, meta-audit to Block Kit; SLK lint gate).
- Risk backlog: 1.5% max-daily-loss circuit breaker (board); dynamic overnight MIN_SCORE (#1); dynamic
  overnight ATR / QHM dynamic stop (#2); delay-first-15-min (#5); entry-vol↔overnight-exit study (#3).
- Rafael's 10-point weekly-plan feedback (revisit AFTER critical queue) — reports-by-tag, TSLA IV-aware stop,
  chop defined-range TA, learning loop, +50%-stop-reclaim re-entry, sizing-dials full dynamic audit,
  conviction/vol resize, MRI redesign, Slack UX revisit, weekly dynamic learning.
- Process/infra: auto-capture recs mechanism; Self-QA BUILDING gates (log-evidence, BGG design-record, perf
  before/after); wrap-up must update bug_counter.json; session-start ingestion of postmortem+autonomous_review;
  finish rulebook consolidation; MODE-2 stale-design (April-2026) audit; verify weekly-render perf properly.

## 3. DESIGNED-BUT-NOT-SHIPPED (Rafael: START HERE) — active list
| Item | Design record | Status | Risk-path | Note |
|------|---------------|--------|-----------|------|
| **Observability v4-B** | observability_and_ops_diagnostic_2026-08-18.md | **GATE-CLEARED, ready to ship** | check | The readiest — designed + gated, just needs the ship carry. Verify not already shipped. |
| **QHM research→execution pipeline** | qhm_research_execution_pipeline_2026-08-23.md | design-stage | YES | Weekly research (news/13F/raises/downgrades/debt→BGG→diversified picks) + fully-auto execution + full report to Slack. Needs mandatory cold board + masked-loss on design AND diff. |
| ↳ Directional earnings scaling | (same) | design-stage | YES | Long capped dip-add + PEAD re-add (primary). SHORT side IN per Rafael override (red-flag screen + strict tail cap). |
| ↳ NVDA dip-add refinement | (same) | design-stage | YES | LIFT the 7-day pre-earnings blackout when bullish thesis holds + weekly-engulfing dip + macro-supportive. NVDA earnings 2026-08-26. |
| ↳ NVDA entry-ladder under-fill CHECK | (same) | not started | NO (read-only) | Cheap diagnostic — likely the real cause of "NVDA 1 share vs 20% target". Do FIRST. |
| **PEAD re-add** | qhm_earnings_trim_design_2026-08-10 (SETTLED #9) | designed, NOT shipped | YES | Post-earnings direction-confirmed re-add. Current code only re-arms the stop on the SAME qty — no re-add. |
| **QHM dynamic protective stop** | qhm_stop_noise_filter_2026-08-21.md | designed, awaiting Rafael approval | YES | Profit-state + NAV-concentration aware stop. |
| **QHM earnings-trim ledger auto-confirm** | ..._2026-08-12 + GATE_RESULT_2026-08-16 | designed; **awaiting Rafael FORK decision** | YES | GATE result: do NOT rebuild net-keyed design; needs Rafael's fork pick before build. |
| **Self-QA gate-4 (design-record gate)** | self_qa_gate4_design_record.md | built locally, **UNTRACKED/uncommitted** | tooling | `.claude/preship/design_record_gate.py` + record_design*.py exist locally but not in git — the other account won't have them. Commit needed. |
| **BGG pipeline v2 (multi-agent + DeepSeek via NVIDIA Build)** | bgg_pipeline_v2_multiagent_2026-08-22.md | queued scoping | tooling | Multi-agent BGG personas + async + sandbox. |
| **Research swarm / AI hedge-fund desk** | research_swarm_queue_2026-08-21.md | queued scoping | NO (research layer) | ← This is the "agents doing research" ARTICLE/TWEET Rafael shared (6-bot Grok research desk). Confirmed captured. |
| **MRI root redesign** | recommendations_backlog.md (no formal record yet) | designed direction | YES | The highest-P&L-impact bug (see §2). |
| **Rulebook consolidation** | rule_doctrine_consolidation_2026-08-22.md | foundation shipped, rest designed | NO | Remaining topic files + anti-drift lint + CLAUDE.md→lean index. |

## 4. Claude's recommendation for next
**MRI news-noise root fix** (biggest measured P&L drain; critical-queue; already BUILDING; risk-path → start
with the masked-loss board on the root design). If preferring a designed+gate-cleared quick ship: **observability
v4-B**. Time-sensitive flag: **NVDA earnings 2026-08-26** — the entry-ladder CHECK (read-only) is the safe
pull-forward; any sizing change is risk-path and can't clear the gate before the print.

## 5. Known un-pushed local state to be aware of
`.claude/preship/` design-record gate tooling is untracked (built, not committed). Several working-tree edits
from prior sessions remain uncommitted (news_monitor keyword fix PARKED, CLAUDE.md/settings edits, etc.) — do
NOT assume those are shipped. Verify at source per NO-GUESS.
