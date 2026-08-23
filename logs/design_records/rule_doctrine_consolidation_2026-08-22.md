# RULE / DOCTRINE CONSOLIDATION AUDIT + PLAN (Rafael mandate 2026-08-22)
**Goal (Rafael):** "audit all of the .md and .log files that have rules and doctrines and consolidate."
**Why it matters (tied to the NO-GUESS + DOCUMENTATION-IS-NOT-ENFORCEMENT mandates):** rules scattered across
many files can't be enforced (a gate can't check a rule it can't find) and can't be reliably followed. One
authoritative, de-duplicated rulebook is the prerequisite for enforceability.

## MEASURED INVENTORY (counts, not estimates — 2026-08-22)
- **CLAUDE.md** — 1607 lines, 363 rule-directives. The de-facto authoritative execution rulebook.
- **Memory** — 64 topic files (28 `feedback`, 4 `project`, 3 `reference`, ~29 untyped/older) + MEMORY.md index
  (~20KB, near its 24.4KB read cap → itself needs compaction). Heavy DUPLICATION of CLAUDE.md doctrines:
  "full read" appears in CLAUDE.md + 10 memory files; "grep" in CLAUDE.md + 5; verify-at-source, docs-is-not-
  enforcement, etc. mirrored. Memory's JOB is RECALL (session-load), so it should POINT to CLAUDE.md, not hold
  a second full copy.
- **handoff.md** — 3104 lines, 459 directive-hits. Cross-account STATE, but standing rules are mixed in.
- **logs/tb_audit_log.md** — 9796 lines, 887 hits. The bug/patch LOG (per-file audit findings) — NOT doctrine;
  keep as a log, out of scope for the rulebook.
- **~50 design records / logs** (logs/design_records/*.md = 10; logs/*_design_*.md, build_f_decision, etc.) —
  DECISIONS with rationale; a subset embed STANDING rules that belong in the rulebook (e.g. build_f_decision,
  report_single_source, per_tier_ownership, tier_refactor).
- **docs/archived/** — already-archived superseded docs (correct pattern; extend it).

## THE CONSOLIDATION MODEL (measured recommendation)
1. **CLAUDE.md = THE single authoritative RULEBOOK.** Every binding rule / doctrine / gate lives here, ONCE,
   de-duplicated, under a clean Table of Contents. It is the only place a rule is DEFINED.
2. **Memory = pointer/recall layer, not a second copy.** Each memory file becomes a ONE-LINE pointer to the
   CLAUDE.md rule (§anchor) + only the session-specific nuance that isn't in CLAUDE.md. De-dupe the 64 files
   (merge overlapping, drop stale). Compact MEMORY.md under its read cap.
3. **Design records REFERENCE, don't RESTATE.** A design record keeps its decision + rationale but links to the
   CLAUDE.md rule instead of re-writing it. Extract any standing rule still only in a design record INTO
   CLAUDE.md first.
4. **handoff.md = STATE only.** Move standing rules out to CLAUDE.md; handoff keeps the ⏩ pick-up pointer +
   current state.
5. **tb_audit_log.md / logs = logs.** Not doctrine; leave as-is.
6. **Archive superseded rule docs** into docs/archived/.

## THE ONE STRUCTURAL FORK (for Rafael, BGG-backed)
How should the authoritative rulebook be physically organized?
 (A) **Monolithic CLAUDE.md** — keep everything in one file, de-duplicated + TOC'd. Simplest; but 1607→ lines
     is a lot to load every session.
 (B) **Lean CLAUDE.md INDEX + sectioned rule files** — CLAUDE.md becomes a short index that points to
     topic rule files (e.g. rules/patch_sequence.md, rules/safety_envelope.md, rules/reviewer_gates.md).
     Modular, smaller per-load; more files to keep in sync.
 (C) **CLAUDE.md core + one canonical RULES.md** — CLAUDE.md keeps the project/role context; a single RULES.md
     holds all binding rules. Two files, clean separation.
BGG to weigh; Rafael decides. (Enforceability is equal across all three — the win is de-duplication + a single
DEFINITION site; the fork is only about physical layout + per-session load cost.)

## EXECUTION (once structure is chosen) — incremental, gated
Phase 1: extract standing rules from design records + handoff INTO the chosen rulebook (de-duplicated).
Phase 2: rewrite the 64 memory files as pointers; compact MEMORY.md.
Phase 3: design records + handoff link to the rulebook; archive superseded docs.
Each phase is doc-only (not risk-path); CLAUDE.md/GATED_SELF edits still take the FINAL preship pass.
