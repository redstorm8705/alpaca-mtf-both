# Design Record — BGGN RESILIENCE: never stall or silently skip a gate voice
**Date:** 2026-09-20 · **Status:** DESIGN (awaiting board+Gro+GAI design pass, then gated build) ·
**Trigger:** Rafael directive 2026-09-20 — "Troubleshoot and find a solution whenever BGGN inputs
timeout or don't provide the needed responses. This needs to be a standing rule for all processing
gates where the BGGN is needed." Coined shipping edge-discovery Step 1 (PR #356): Gro's 8k-TPM limit
could not fit two ~400-line diffs and the NVIDIA option-C substitute timed out (120s), forcing a
manual `--waive-gro`.

## OBSERVED FAILURE MODES (verified at source, 2026-09-20)
1. **Gro TPM overflow** — `preship_audit._gro` sends the full prompt to Groq `openai/gpt-oss-120b`
   with `max_completion_tokens=2048` against an 8000 TPM free cap. Input beyond ~5.9k tokens →
   `Request too large ... Requested 8405 > Limit 8000`. A ~400-line diff + context overflows.
2. **Gro option-C substitute (NVIDIA llama-3.2-90b) timed out** (120s) → whole file marked FAIL.
3. **GAI dead model ID** — a pinned `gemini-2.5-flash` now 404s (models churn). Mitigated already by
   the `_GAI_LADDER` (3.1-flash-lite → 3.7-flash → 3-flash-preview → flash-latest) + retries.
4. **GAI transient 503** on one ladder model — handled by advancing the ladder.

## THE STANDING RULE (proposed — goes to CLAUDE.md on approval)
When ANY BGGN input (a board seat, Gro, or GAI) fails to return a usable verdict within its bounded
timeout + capped retries, the gate follows this ladder — it NEVER hangs unbounded, and NEVER silently
drops a voice:

1. **Bounded + laddered per voice, before declaring it unavailable:**
   - **Gro TPM-overflow → AUTO-CHUNK the diff** into sub-limit pieces (each: shared instructions +
     context + one hunk-group, sized so input+2048 < 8000), audit each chunk, **combine
     worst-verdict-wins** (any chunk REJECT ⇒ REJECT; all APPROVE ⇒ APPROVE). This yields a REAL Gro
     verdict on a large diff instead of a skip. Only if chunking itself fails → option-C NVIDIA
     substitute → then unavailable.
   - **GAI dead-model/503/429 → model ladder + capped retries** (already built; keep).
   - **Gro-substitute (NVIDIA) → MODEL LADDER, not one model** (Rafael directive 2026-09-20: "when
     the NVIDIA fallback doesn't work, try another model — it offers more than one; stop settling for
     the first rejection"). Today it hard-coded ONE model (`llama-3.2-90b`) and gave up when it timed
     out. Fix: iterate a ladder of NVIDIA-hosted models (e.g. `meta/llama-3.3-70b-instruct`,
     `meta/llama-3.1-70b-instruct`, `qwen/qwen2.5-72b-instruct`, `nvidia/llama-3.1-nemotron-70b-instruct`
     — exact list validated at build against the live `integrate.api.nvidia.com/v1/models`), each with a
     bounded timeout; a timeout/404/5xx on one advances to the next. Only a whole-ladder failure marks
     the substitute unavailable.
   - **Board seat timeout → one re-spawn**, then treat as non-returning.
   - **GENERAL PRINCIPLE (Rafael 2026-09-20): never settle for the first rejection** — every external
     voice exhausts its full ladder (models + chunking + retries) before it is declared unavailable and
     the fail-safe floor is invoked.
2. **Unavailability is RECORDED, never silent** — the marker carries `gro=WAIVED[reason]` /
   `gai=WAIVED[reason]` and the ship summary to Rafael states which voice was unavailable and why.
3. **FAIL-SAFE FLOOR (never auto-approve on silence):** a ship requires **board majority AND ≥1
   external voice (Gro OR GAI) APPROVE.**
   - **Risk-path diff:** if BOTH external voices are unavailable, OR the one available voice REJECTs
     → **BLOCK + surface to Rafael.** A risk-path diff never ships with zero external review.
   - **Non-risk-path diff:** board majority + one external voice APPROVE suffices (this is Rafael's
     existing "Gro-skip-if-unavailable → board majority + GAI APPROVE suffices", 2026-07-07,
     generalized to either external voice).
4. **Bounded timeouts everywhere** — every external call has a hard timeout + capped retries; no
   unbounded 120s×N hangs. A voice that exhausts its ladder is "unavailable", handled by (2)+(3).
5. **Transparency** — the waive/unavailability + reason is ALWAYS reported to Rafael in the ship
   summary (an honest "Gro was unavailable (TPM); shipped on board+GAI" line), never buried.

## THE ENFORCEMENT (per DOCUMENTATION-IS-NOT-ENFORCEMENT — the rule needs a mechanism)
The recurring, mechanical failure is Gro TPM overflow. The control is **`_gro` auto-chunk**:
- Add a token estimate; if `prompt` would exceed the TPM budget, split the DIFF body (keep the
  system+instruction+context header in every chunk) into K minimal chunks each under budget.
- Call Gro per chunk; combine verdicts worst-wins; concatenate the cited defects.
- If a single hunk is itself over budget (a huge single file), fall back to head/tail clamp of that
  hunk with an explicit "(clamped)" note in the returned rationale (so it is never a false clean).
- Keep option-C NVIDIA as the post-chunk fallback; keep the FAIL-SAFE FLOOR above as the backstop.
- Unit test: a synthetic >8k-token diff produces K>1 Gro calls and a combined verdict; a chunk
  REJECT makes the whole verdict REJECT.

## OPEN DESIGN DECISIONS (for the board design pass)
- D1: chunk-combine semantics — worst-verdict-wins (proposed) vs. require all-APPROVE with a
  re-review of any chunk that abstains. (Proposed: worst-wins; simplest fail-safe.)
- D2: fail-safe floor for a NON-risk-path diff when BOTH external voices are down — board-majority-
  only ship (with loud record), or block? (Proposed: block only risk-path; non-risk ships on
  board-majority + records "both external voices unavailable" — but this is the one spot that could
  weaken review, so it is the key board question.)
- D3: token budget + estimator (chars/4 heuristic vs. a tokenizer) and the per-chunk size.

## SCOPE / SAFETY
Execution-governing tooling change (`preship_audit.py`) + a CLAUDE.md rule addition → both take the
FINAL PRE-SHIP Gro+GAI pass on their own diffs (self-application). It does NOT relax any safety
envelope: the fail-safe floor makes the gate STRICTER (a risk-path diff can never ship with zero
external review), only removing the unbounded-hang / silent-skip / forced-manual-waive failure.
