# PENDING APPROVAL — Catalyst `guidance_cut` recall fix (autonomous session 2026-07-14)

**Status:** QUEUED (not shipped). Autonomous session — Rafael's decision required.
**Why queued, not auto-shipped:** Not clean 3-way alignment (GAI standing dissent). Per Rafael's
auto-apply mandate *"aligned→ship, unaligned→queue"*, and the preship_gate needs an honest
Gro+GAI APPROVE marker I will not self-write. Board tie-breaker resolved it APPROVE, but I am
surfacing the GAI dissent to Rafael rather than shipping over it autonomously.

**Patch:** `logs/pending_patch_2026-07-14_catalyst_guidance.patch` (41 lines, applies clean to `ea5a58c`).

---

## PROPOSAL (one sentence)
Expand the LIVE catalyst gate's `guidance_cut` keyword set so it catches common real guidance-cut
headline phrasings it currently misses — improving protection of an already-approved blocking type.

## THE PROBLEM (plain English + example)
The catalyst gate went live 2026-07-14 (Rafael go, commit `2e2561d`): when a watched name has a
fresh negative catalyst (dilution offering, guidance cut, solvency, legal probe), the bot won't
open a NEW position in it — the fix born from buying into RIVN's dilution. I validated the detector
this session: **8/9 correct**, but it MISSED one real case. A headline like *"Acme cuts full-year
**revenue** guidance, lowers outlook"* was classified NEUTRAL — the substring match needs the exact
phrase "cuts guidance", and the word "revenue" in the middle broke it. So today the bot could still
buy into a name that just cut guidance, if the headline is phrased that common way.

## THE FIX (plain English)
Add ~19 more present-tense phrasings of a guidance cut ("lowers outlook", "reduces guidance",
"trims outlook", "cuts revenue guidance", "cuts full-year guidance", etc.). Each is an ACTIVE verb
describing a current company action — so recall goes up with no new false-blocks. Also fixes one
pre-existing mypy type error (no logic change). Validated **11/11** after the change: every real
active cut blocks; every past/referential ("*despite a lowered outlook earlier this year, Company
beats Q3*") and every positive ("*raises guidance*") headline correctly does NOT block.

## BGG RECORD (full)
| Voice | Verdict | Core reasoning |
|-------|---------|----------------|
| **Gro** | APPROVE ×3 | High-precision active verbs; don't appear in positive/neutral headlines; additive keywords are correct for a live gate (co-occurrence rule would over-block "maintains guidance"). |
| **Board seat 1** (LdP/McKinney) | **APPROVE** | GAI's residual objection attacks the approved risk-first premise, not this diff; 11/11 clean substring separation; asymmetric loss function justifies risk-first. |
| **Board seat 2** (Harris/Thorp) | **APPROVE** | False block = one suppressed entry other gates also screen (~0 cost); missed block = re-creating the RIVN naked-short-gamma draw on a $2,745 book. Cheap insurance. |
| **GAI** | REJECT ×3 (evolving) | R1: bare noun "guidance cut" matches past references → **VALID, dropped**. R2: past/passive "lowered outlook" matches history → **VALID, dropped**. R3: even present-tense "cuts revenue guidance" fires in a net-positive headline ("cuts guidance BUT beats EPS") → **board ruled this attacks the approved risk-first premise (a real cut is a real cut), not a defect**. |

**Tally:** Gro APPROVE, Board 2-0 APPROVE, GAI REJECT (premise-level). Rounds 1-2 of GAI's catches
were incorporated into the diff. Round 3 is a genuine disagreement with the *risk-first* design that
was already board-approved when the gate shipped — out of scope for this recall patch.

## YOUR DECISION
- **APPROVE** → apply `logs/pending_patch_2026-07-14_catalyst_guidance.patch`, commit, deploy to OCI,
  restart. (Board + Gro back it; GAI's dissent is against the risk-first premise you already approved.)
- **SIDE WITH GAI** → drop the patch; keep the gate as-is (under-catches split-phrasing guidance cuts).
- **DEFER** → leave queued.

## RISK IF APPROVED
Slightly more aggressive blocking: a name that cuts guidance-in-any-phrasing is skipped for NEW
entries even if the same headline has a positive element (EPS beat). Blocks entries only, never exits.

## RISK IF REJECTED
The live gate keeps missing common guidance-cut phrasings (the "cuts full-year REVENUE guidance"
class) — the bot can still open into a name that just cut guidance, the RIVN-class hole this closes.
