# SLACK MESSAGING OVERHAUL — grounded audit + plan (Rafael mandate 2026-08-19)

**Rafael:** "The slack messages are broken, non-informative and continue to render in text that cut off.
The UX designer and BGG need to fix this fully. Autonomous runs are failing and it's useless as a tool
currently." → Full audit + redesign. UX seat (Luke Wroblewski) leads formatting; BGG on the design.

## AUDIT (done 2026-08-19, verified at source) — from Rafael's pasted Post-Market + Meta-Audit

### Confirmed breakages
1. **FORMATTING — sections render as one wall.** Rafael's paste: "…🔴 CRITICAL: execution.ownership_guard📊
   Today — 2026-08-18Verdict:…" — no line breaks between title/date/verdict/items. Slack newlines/markdown
   lost in the audit scripts' message assembly (NOT in alerts.py — alerts.py sends what it's given).
2. **PER-FIELD TRUNCATION** — descriptions cut mid-word ("account for vola…", "prevents subsequent trade…").
   An in-script `[:N]+"…"` on fields, SEPARATE from the >4000-char message chunking already shipped (#149).
   (Locate: grep the audit scripts for slicing on desc/summary/title before the Slack post.)
3. **DEAD-GRO 404 (autonomous runs failing)** — the "Auto AI Meta-Audit" shows "Groq: ❌ 404". Root:
   TWO live dead-model sites remain on `llama-3.3-70b-versatile`:
   - `auto_ai_audit.py:77` `_GRO_MODEL = "llama-3.3-70b-versatile"` (used L1151/1167/1175); token cap
     `_GRO_MAX_COMPLETION_TOKENS = 3000` (L109) — TOO LOW for gpt-oss (a reasoning model; reasoning eats the
     budget → EMPTY content). FIX: model → `openai/gpt-oss-120b`, add `"reasoning_effort":"low"` to the
     payload (L1150-1158), bump cap to ~6000 (mind Groq TPM). VERIFIED gpt-oss-120b returns non-empty content.
   - `.claude/preship/preship_audit.py:172` `"model":"llama-3.3-70b-versatile"`, `max_tokens:1500` (L176).
     FIX: model → `openai/gpt-oss-120b`, `max_tokens → 4096`, add `"reasoning_effort":"low"`. (This is why
     every ship this week used `--waive-gro` — fixing it restores real Gro in the ship gate.)
   - NOTE: `autonomous_patch_generator.py` is ALREADY on gpt-oss (#148); its remaining llama hit is a stale
     COMMENT (L316) — update the comment only. `autonomous_review.py` fixed in #148.
4. **STALE/NON-INFORMATIVE CONTENT** — the audit paged GEV/qhm sync-refuse ("streak of 20 failed heals")
   which was ALREADY unstuck. Audits report resolved state as CRITICAL → needs current-state checks.
5. **VAGUE VERDICTS** — "2 items to act on" with no clear action. Rafael: useless.

### Slack emitters (~14 — the overhaul surface)
Core: `alerts.py` (chunking shipped #149 — sends what it's given; formatting is upstream in the callers).
Audit/report scripts (own message assembly, where formatting/truncation/staleness live): `auto_ai_audit.py`,
`nightly_audit.py`, `midday_audit.py`, `weekly_perf_audit.py`, `weekly_postmortem.py`, `reconcile_eod.py`,
`run_ledger_sync.py`, `main.py`, `autonomous_review.py`, `autonomous_patch_generator.py`,
`execution/forever_hold_manager.py`, `scripts/sync_reports.py`, `scripts/audit_slack.py`.

## PLAN (workstreams — each its own gated diff; UX+BGG design pass on WS2 before code)
- **WS1 — DEAD-GRO SWEEP (mechanical, HIGH value, do FIRST — fixes "autonomous failing"):** fix the 2 live
  sites (auto_ai_audit.py, preship_audit.py) → gpt-oss-120b + reasoning_effort:low + adequate max_tokens;
  update the stale comment in autonomous_patch_generator.py. TEST each returns non-empty content on a real
  audit-length prompt. Gate (both gated files). Ship.
- **WS2 — FORMATTING REDESIGN (UX seat + BGG design pass FIRST):** a shared Slack-formatting helper (proper
  \n / Slack Block Kit or mrkdwn) so audit messages have clear sections/line-breaks. Migrate the audit
  scripts to it. Readable structure, PT timestamps, no mid-word truncation (summarize-not-cut for long
  fields; chunking already prevents >4000-char loss).
- **WS3 — CURRENT-STATE ACCURACY + ACTIONABILITY:** audits check live state before paging (don't page
  resolved issues); verdicts state a concrete action.

## SEQUENCING RECO: WS1 (quick win, fixes the acute 404) → WS2 (design-led, the bulk) → WS3.
