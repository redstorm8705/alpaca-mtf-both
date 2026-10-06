# Day-tier execution-cost attribution and family-router contract — 2026-10-06

**Owner/signature:** ChatGPT/Codex

**Status:** BGGN and mechanical gates passed; additive architecture build, not wired to order execution.

## Boundary with Claude

Claude owns mechanism evidence, the fixed admission gate, and the independent mechanism auditor. ChatGPT/Codex owns the shared snapshot, lifecycle attribution, cost measurement, and router contract. The router consumes Claude's explicit admission state; it cannot admit a family from recent P&L, an edge score, or mechanism tags.

## Execution-cost attribution

`strategy/day_tier_execution_costs.py` joins existing decision, entry, partial-exit, and exit records by `trade_id`. It reports direction-aware adverse basis points against the decision reference, explicit arrival midpoint when present, market price at fill, and market price at exit. Missing prices remain `None`. Alpaca US-equity commission is recorded as zero while spread and slippage remain separate measured quantities. The reducer changes neither authoritative P&L nor trade state.

## Router contract

`strategy/day_tier_family_router.py` reads a versioned JSON registry. A family routes only when all of these are true:

1. the family exists in the registry;
2. status is exactly `ADMITTED_PAPER`;
3. the candidate hypothesis version exactly matches the admitted version;
4. the independent audit is exactly `PASS`;
5. the caller supplies a finite positive routing score with a timezone-aware
   `score_asof` inside the family-specific freshness window.

All failures allocate zero. Missing, stale, naive, or future-dated score provenance also allocates zero. Among admitted candidates, positive scores are normalized and clipped by the admission's maximum share of the already-established day-tier router sleeve. A 100% router share means the whole admitted-family sleeve, not 100% of account capital. This module is not imported by the runner or execution manager in this increment.

The initial registry mirrors Claude's 2026-10-05 evidence ledger: Track M is admitted for paper collection; Track-A GEX families are blocked pending valid gamma data/evidence; Track B ORB is rejected. Registry changes are code-reviewed evidence changes, never autonomous writes.

## Adversarial review

The first exact-tree Board review rejected malformed candidate/clock inputs that could raise, invalid or future arrival-mid provenance, and an ambiguous `admitted=True` on zero-allocation results. The second round found that non-datetime `now` values could still raise. Groq then caught normalized shares that could round to zero while remaining admitted. The revision returns an explicit non-routable decision for every malformed input or clock type, validates that arrival-mid evidence is timezone-aware and no later than the decision, and reserves `admitted=True` for candidates with a strictly positive published router allocation.

Final gate result: Board 2/2 PASS, Google AI Studio APPROVE, Groq APPROVE, and mechanical preship PASS. The focused architecture suite reports 25 passing tests, including 12 cost/router tests and 13 mechanism-context regressions. Nvidia was not used because it is the optional backup seat.
