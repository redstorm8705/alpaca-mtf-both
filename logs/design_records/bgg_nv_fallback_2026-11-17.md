# BGG-NV-1117 — design reviews use the NVIDIA pair when Codex is unavailable (not Gro)

Approval-ID: BGG-NV-1117
Covers: .claude/preship/bgg_call.py, .claude/preship/test_reviewer_swap.py
Not changed: .claude/preship/preship_audit.py, .claude/preship/gai_client.py
Status: rev 1 (signed: Claude, 2026-11-17). Design only.

## Owner direction (2026-11-17, verbatim)
"I didn't think gro was the default backup reviewer anymore. Please confirm. Thought it was and should be the updated
Nvidia api models"

## Problem (measured 2026-11-17)
- Pre-ship audits (`preship_audit.py`) already follow NV-REVIEW-1110 (PR 938, merged 2026-11-11): when Codex is
  unavailable the NVIDIA pair (`_NVIDIA_PAIR` = mistralai/mistral-large-4-1t-a52b + minimaxai/minimax-m3.1-flash,
  `preship_audit.py:728`) reviews, and Gro runs only if both NVIDIA models fail at transport.
- Design reviews (`bgg_call.py:80-100`) were not updated: when Codex is unavailable they call Gro directly
  ("CODEX UNAVAILABLE ... — Gro reviews as reviewer #2"). On 2026-11-17 every design review of KILL-EXIT-JOIN-1117,
  EOD-FRIDAY-1117 and QHM-ROOM-MOC-1117 therefore used Gro, which also hit HTTP 413 (prompt too large) and 429 (rate
  limit) several times.

## Change
`bgg_call.py`: when Codex is unavailable, call `preship_audit._nvidia_pair(prompt, NVIDIA_API_KEY)` (the same function
and models the pre-ship audit uses) and print each model's reply under `=== NVIDIA <model> ===` plus the pair verdict.
Gro runs only when the pair reports `endpoint_down` (both models failed at transport) or no NVIDIA key is set, and the
fallback line says which. `--gro-only` keeps its meaning "reviewer #2 only" (Codex, else NVIDIA pair, else Gro).
Tests (`test_reviewer_swap.py`, BggCallRouting): Codex reply skips both; Codex unavailable -> NVIDIA pair, no Gro call;
pair endpoint down -> Gro; no NVIDIA key -> Gro; `--gai-only` skips all three.

## Required questions
Q1: Reviewer #2 is the quality gate on every design; using the stronger, owner-chosen pair improves design reviews.
Q2: No trade-count change.
Q3: Should exist (owner). Simplest form: call the existing pair function.
Q4: Finishes NV-REVIEW-1110 (PR 938) for design reviews; no open PR touches bgg_call.py.
Q5: Not applicable because no threshold is introduced; model ids come from the existing `_NVIDIA_PAIR`.
Q6: Forward: one reviewer-routing function shared by both tools so they cannot drift again.
Q7: Not applicable because it is review tooling, not a trading signal.
Q8: Measured: the bgg_call outputs of 2026-11-17 show "Gro reviews as reviewer #2"; preship_audit.py:19-20 documents the NVIDIA order.
Q9: Unit tests of the routing with mocked calls.
Q10: Not applicable because no trading edge is involved.
Q11: No size, frequency or exposure change.
Q12: No safety-envelope change.
Q13: NVIDIA down or key missing: Gro, as today; Gro failing prints the error, as today.
Q14: Not a shadow: it changes the live review route.
Q15: The output names which reviewer answered.
Q16: Proof: the next design review prints "=== NVIDIA" when Codex is out.
Q17: The next design review run while Codex is at its limit.
Q18: The anomaly closed: design reviews still on Gro after the owner moved reviewer #2 to the NVIDIA pair.
Q19: Gate file: owner approval; GAI and reviewer #2 review the exact diff before it ships.
Q20: Neutral prompts; tests included.
Q21: Tests mock every network call; nothing reaches a reviewer or the broker.
Q22: Not applicable because it is not a trading control.
Q23: The routing tests plus one real design review run with Codex unavailable, checked for the NVIDIA output block.
Q24: Not applicable because it makes no availability claim.
Q25: Not applicable because it uses no market statistic.
Q26: Not applicable because it reads no market data; reviewer APIs only.
Q27: Writes nothing; prints review text.
Q28: Runs only when Claude calls bgg_call.py for a design review.
Q29: Owner-requested; small; it improves every later design review.
Q30: Blind spot: two tools routing the same reviewer slot separately.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because there is no replay.
Q33: Not applicable because there is no trading cost.
Q34: Not applicable because there is no return stream.
Q35: Not applicable because no market regime is involved.
Q36: Not applicable because there is no sizing or liquidity.
Q37: Not applicable because no edge is claimed.
Q38: Reversal: a design review with Codex out still reaches Gro while NVIDIA is up.
Q39: Not applicable because it sizes nothing.
Q40: Outcome: reviewer #2 on design reviews is the NVIDIA pair whenever Codex is out; machine check = the routing tests in CI.
Q41: Next: share the routing function between both tools.
Q42: The tool output of each review.
Q43: One reviewer policy, applied by every entry point.
Q44: The next real design review's output.
