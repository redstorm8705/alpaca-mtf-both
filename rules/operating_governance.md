# OPERATING / GOVERNANCE — full text (canonical definition site for G01–G11)
<!-- Consolidation (Rafael 2026-08-22, Hybrid C+). Always-loaded companion to CORE.md (via OPERATING.md
     pointers). THIS file is authoritative for the full text. -->

## G01 — Rafael is the sole mandate authority
Rafael (Chairman/CEO) decides when work starts, order, what's deferred/blocked, and whether external-audit
recommendations are adopted. Gro (Groq) and GAI (Google AI Studio) and the board are audit VOICES only — they
surface risks/bugs/recommendations with ZERO authority to block, defer, or mandate. Never "Gro mandated X" —
say "Gro flagged/recommended X". No file is read-only from Claude's perspective; the approval gate is the
protection. Proposals come to Rafael only when ALL voices (board, Gro, GAI) are aligned — splits are resolved
by counter-prompting first, never surfaced to him unresolved.
_Source: CLAUDE.md §Authority Rule + §Rafael's Role._

## G02 — PROFITABLE > PERFECT (within the envelope)
Paper-money, aggressive growth phase ($2.5K→$25K): a validated, tradeable-but-imperfect edge is worth more live
than a "perfect" edge benched. Do NOT keep a working signal disabled over calibration perfectionism — ship it
live (paper), measure, refine in flight. SCOPE: this governs the DECISION TO ENABLE a validated signal — it
NEVER relaxes code correctness, the patch sequence, the board/Gro/GAI gate, the cold-2nd, or the safety
controls. "Perfectionism" = calibration/optimization polish ONLY; it NEVER covers a miscalibration that WIDENS
downside risk (that's a safety defect, fully gated). More aggressive WITHIN the envelope, never a wider envelope.
_Source: CLAUDE.md §Profitable > Perfect._

## G03 — BUILD, DON'T JUST FIX (MODE-2 forward pass, separate diff)
Every bug fix / audit item produces THREE things: (1) the fix, (2) the ROOT cause, (3) a FORWARD-IMPROVEMENT
pass (MODE-2: what STATIC assumption did this expose, and what DYNAMIC/adaptive capability should we BUILD?) —
then build it through the gate or log a board-reviewed reason not to. TIDY-FIRST: the build ships as a SEPARATE
diff/commit from the fix (the fix is never blocked by the build; mixing muddies P&L attribution). Front-load
rigor → ship live → iterate; no post-ship shadow-benching of a gate-cleared improvement.
_Source: CLAUDE.md §Build, Don't Just Fix (A/B)._

## G04 — FEATURE DESIGN PROTOCOL (ask first)
Before designing/implementing any new feature/integration/module/data-source/signal, ask the clarifying-
questions gate (≥5, covering: data source & tier & fallback, output location/format/atomic-write, integration
point, failure mode, board-vote-needed?). Never jump to code. Surface unknowns before code, not after. Not
skipped for "simple" features.
_Source: CLAUDE.md §Feature Design Protocol._

## G05 — OPEN QUESTION PROTOCOL (BGG on any fork)
Any patch/config/design decision with ≥2 viable options that can't be resolved from first principles → gather
ALL THREE voices FIRST: board vote (≥2 cold parallel subagents; full board for strategy), Gro (direct API), GAI
(direct API, same prompt). Present as a decision table + the consolidated recommendation WITH the fork; Rafael
is the sole authority. Never a bare menu; never present a fork without the BGG recommendation attached.
_Source: CLAUDE.md §Open Question Protocol + memory feedback_board_rec_with_questions._

## G06 — DURABLE SYNC (every ship AND every BGG-alignment)
Persist aligned state the MOMENT alignment is reached (not at session end) on two triggers: any SHIP, and any
BGG-ALIGNMENT (even zero code). The sweep (same turn): git commit+push the change + updated handoff.md; OCI
(ships → git pull --ff-only + restart; alignment-only → handoff push, no restart); handoff.md "⏩ pick up here"
block rewritten to current state + exact next step; one logs/ line; one Master Brain push. Hard invariant:
handoff.md ALWAYS carries a live "⏩ pick up here" pointer. A cross-account handoff is exactly: git pull → read
handoff.md → query Master Brain. Surgical, not a heavyweight wrap-up.
_Source: CLAUDE.md §Durable Sync Rule._

## G07 — code-review-graph before grep/read
Use the code-review-graph MCP tools FIRST to explore the codebase (semantic_search_nodes / query_graph /
get_impact_radius / detect_changes / get_review_context) — faster, cheaper, structural. Fall back to Grep/Glob/
Read only when the graph doesn't cover the need. (This does NOT relax P01's full-read-before-patch gate.)
_Source: CLAUDE.md §MCP Tools: code-review-graph._

## G08 — Session start (blocking): handoff.md + Master Brain
Two required blocking steps at session start: (1) read handoff.md first — authoritative current bot state (open
items, services, positions, recent changes); any CLAUDE.md metadata conflicting with handoff.md is STALE. (2)
Query the Master Brain notebook (notebooklm) for decisions, preferences, history.
_Source: CLAUDE.md §Session Memory Protocol._

## G09 — Wrap-up is Rafael's call only
Never auto-trigger `/wrap-up` — not at task end, not on high context. Offering once is fine; running it unasked
is not.
_Source: CLAUDE.md §Wrap-Up._

## G10 — Free AI tiers first; paid only when necessary
Cost discipline (only Rafael changes the protocol/budget): Gro (free Groq) + GAI as 2 of 3 voices; cold board on
Sonnet, Opus only for final risk-path sign-off; prompt caching on large files. GAI free key FIRST — on transient
503, retry free with backoff; the PAID GAI key is a last resort for genuine persistent unavailability only, never
an auto-failover. API budget $20/mo to start.
_Source: CLAUDE.md §Interactive-vs-API Cost Protocol; Rafael 2026-08-22 (no paid GAI unless absolutely necessary)._

## G11 — Ship ready items first
A diff that has cleared its full gate (statics + cold-2nd + Gro/GAI preship) is SHIPPED (commit → merge → deploy
→ verify) before starting new work or a tangent. Ready-to-ship > newly-raised. Finish the ship (or the tight
batch of ready ships), then take the next item.
_Source: CLAUDE.md §Ship Ready Items First._
