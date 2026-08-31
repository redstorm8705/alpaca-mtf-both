# Alignment Gate (Self-QA #5) — RESUME BRIEF

**Status:** DESIGNED (BGGN-aligned), one file drafted (`record_alignment_DRAFT.py`), NOT shipped.
Paused mid-build for the weekly usage-limit wrap (Rafael, 2026-08-30). This gate is itself
execution-governing → it runs its OWN Feature Design + full board/Gro/GAI + final preship before it
ships. Nothing is on `main` yet.

## What Rafael asked for (verbatim intent)
1. The **adversarial gate** must NOT check the CLAIMS TEXT for forward-looking buzzwords (that was my
   misread — I built a `_FORWARD_RE` regex, he corrected it, I REVERTED it; board later confirmed it
   was "security theater"). Instead:
2. Every new-capability build's **premise** must pass: *"Can this be more dynamic? Is this build
   accounting for future improvements? How else could this build be better?"*
3. Expanded to the **GATES THEMSELVES**: they must check the **North-Star dogmas** — is this most
   relevant to **$2.5k→$25k**, and is this the **most dynamic build**? "If those questions aren't in
   the gates, we're wasting our time."

## The design (board-consolidated, corrects Gro/GAI false precision)
A NEW dedicated gate, **Self-QA #5**, separate from record_adversarial (Beck: separate concerns).
Requires an **independent COLD critic** (fresh subagent, given ONLY the diff + 3 questions, blind to
author framing) to write `logs/design_records/alignment_<slug>.md` with THREE sections:
- `## Dogma check` — price-action-first · no-static-regimes (cross-ref no_static_scan waivers) ·
  aggressive-growth-in-envelope · anti-silo · decision-explainability
- `## Value rank` — is this the highest-value $2.5k→$25k move or polish? cite a real competing
  roadmap/PENDING/recommendations_backlog item it beat, or state none exists
- `## Dynamism check` — is there a more adaptive/regime-aware form; honest "not yet, because…" = PASS

### The novel enforcement (anti-buzzword — the whole point)
A script CANNOT verdict mission-value (NO-GUESS forbids stating a forecast as a verdict — this is why
**Gro's "$150 ROI" and GAI's "score ≥80" were REJECTED as false precision**). So the gate enforces
**STRUCTURAL compliance only**, mechanically:
- 3 sections present + per-section length floor (screens the empty case)
- **template-reuse detector** (`difflib.SequenceMatcher` vs prior `alignment_*.md`, ratio ≥0.55 →
  BLOCK "name what's different about THIS diff") — checks NOVELTY, which buzzword-templating fails
- **citation-existence** — the critique must name ≥1 symbol that grep-confirms in a covered file's
  STAGED content (engaged with the ACTUAL build, not a generic essay)
- sha-binds each covered file's staged content (a code edit invalidates the marker)

### CRITICAL calibration (do not get this wrong)
- **NEVER blocks on the CONCLUSION.** "not most dynamic" / "this is polish" / "no better idea exists"
  are ALL PASSING. Blocking on them would violate PROFITABLE>PERFECT's own text (Taleb: a gate that
  enforces a dogma by violating a dogma is a bug). Negative findings are surfaced to the Future
  Roadmap Log + `alignment_findings.jsonl`, never suppressed, never block.
- A genuine dogma **VIOLATION** → disagreement protocol → escalate to Rafael, NEVER a script block.
- Fires ONLY on **new-capability builds** (new gated `.py`/`.sh`, same population as design_record_gate).

### MY CORRECTION to the board's design (flag to Rafael when presenting)
Board suggested a **PreToolUse-Write hook** (mirror design_record_gate). **That doesn't fit:** the
critique reviews the DIFF, which doesn't exist at first-write time (chicken-and-egg). So the Alignment
Gate belongs at **SHIP-TIME** — enforcement in `preship_gate._alignment_ok()` at commit/push, bound to
staged content, exactly like cold-2nd/adversarial. **No new PreToolUse hook file.** This is simpler
AND more correct than the 3-file board sketch.

## Remaining build steps (in order)
1. Write `logs/design_records/alignment_gate_2026-08-30.md` (the design record for THIS gate — needed
   first, because design_record_gate will block the new `.claude/preship/*.py` files until a
   design-record marker covers them). Then `record_design.py alignment-gate --doc ... --covers
   .claude/preship/record_alignment.py .claude/preship/...`.
2. Finalize `record_alignment.py` from `record_alignment_DRAFT.py` (drafted, logic complete; re-review).
3. Add `_alignment_ok()` to `.claude/preship/preship_gate.py` — ship-time mirror: a NEW gated .py/.sh
   being committed requires a fresh `align__<slug>.json` marker whose covered staged-sha matches. Wire
   it into the same check-orchestration as `_design_record_ok` / `_adversarial_ok`. GATED_SELF exempt
   (no infinite regress — the gate can't require an alignment critique of itself).
4. (Optional) a lightweight PreToolUse REMINDER hook (not a block) that nudges "new gated file → an
   alignment critique will be required at ship." Decide during build; not essential.
5. Run the FULL gauntlet on the gate's own diff: statics, cold-2nd, board (Beck/Kim/Simons/Thorp/
   Taleb/Schneier), Gro+GAI preship. Ship git-single-channel. Update CLAUDE.md §SELF-QA GATE (#5) +
   handoff.
6. Force-append mechanism: negative alignment findings → `logs/design_records/alignment_findings.jsonl`
   + Future Roadmap Log (so the "how could this be better" answers become the next build queue).

## BGGN inputs preserved in scratchpad
`align_brief.txt` (the neutral prompt sent to Gro+GAI), `align_gro.json`, `align_gai.json`,
`align_gai_out.txt` (their raw responses — Gro over-engineered Docker/ML/ROI; GAI used score≥80; both
corrected by the board).
