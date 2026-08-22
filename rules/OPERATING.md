# OPERATING RULES — governance, always-loaded (companion to CORE.md)
<!-- Rafael mandate 2026-08-22 (Hybrid C+). These are governance/operating non-negotiables — always-loaded like
     CORE.md, but separated from the moment-of-action safety/patch/behaviour block. Pointers; full text lives in
     rules/ topic files. SOURCE = audited CLAUDE.md sections cited in (§...). -->

- G01 Rafael is the SOLE mandate authority. Gro/GAI/board are audit voices only — they surface risks/bugs, they never block, defer, or mandate. Never say "Gro mandated X"; say "Gro flagged/recommended X". (§Authority Rule, §Rafael's Role)
- G02 PROFITABLE > PERFECT: this is an aggressive paper-growth phase ($2.5K→$25K) — ship a validated edge live and refine in flight; be MORE aggressive within the safety envelope, NEVER widen it. Scope: relaxes the decision-to-enable, never the code-correctness gate or the safety envelope. (§Profitable>Perfect)
- G03 BUILD, DON'T JUST FIX: every fix/audit item produces (1) the fix, (2) the root cause, (3) a MODE-2 forward-improvement pass — shipped as a SEPARATE diff from the fix (tidy-first). (§Build-Don't-Fix A)
- G04 FEATURE DESIGN PROTOCOL: before designing/implementing any new feature/integration, ask the clarifying-questions gate (≥5: data source, output, integration point, failure mode, board-vote-needed?). Never jump to code. (§Feature Design)
- G05 OPEN QUESTION PROTOCOL: any decision fork with ≥2 viable options → gather board + Gro + GAI FIRST and present their consolidated recommendation WITH the fork. Never a bare menu. (§Open Question)
- G06 DURABLE SYNC: persist aligned state the MOMENT alignment is reached (not at session end) on every SHIP and every BGG-ALIGNMENT — git + OCI + handoff.md "⏩ pick up here" + logs + Master Brain. handoff.md always carries a live pick-up pointer. (§Durable Sync)
- G07 Use the code-review-graph MCP tools (semantic_search / query_graph / get_impact_radius / detect_changes) BEFORE Grep/Glob/Read to explore the codebase. (§MCP Tools)
- G08 SESSION START (blocking): read handoff.md first (authoritative current state), then query the Master Brain (notebooklm) for decisions/preferences/history. (§Session Memory Protocol)
- G09 Wrap-up is Rafael's call only — never auto-trigger `/wrap-up`. (§Wrap-Up)
- G10 INTERACTIVE vs API cost split (only Rafael changes it): interactive handles scoping/diagnosis/design; API handles pre-scoped builds; keep Gro (free) + GAI as 2 of 3 voices; free-tier first, paid GAI only when absolutely necessary. (§Interactive-vs-API)
- G11 SHIP READY ITEMS FIRST: a diff that cleared its full gate is shipped (commit→merge→deploy→verify) before starting new work. (§Ship-Ready-First)
