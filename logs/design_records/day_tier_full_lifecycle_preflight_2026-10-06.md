# Day-tier full-lifecycle preflight — 2026-10-06

**Owner/signature:** ChatGPT/Codex

## Purpose

Prove that the admitted-family router cannot recreate Monday's all-trades lockout and that the existing execution paths still complete their order lifecycle in simulation.

## Scope

No production logic changes. Two stale test modules were reconciled to the already-shipped contracts:

- the fake limit-order function accepts and preserves the required client order ID;
- completed journal fixtures include the timestamp and realized-P&L fields now required by the fail-closed loss reader;
- emergency-stop residual assertions derive from the risk-sized fill rather than an obsolete fixed quantity;
- gross-cap and tier-kill expectations match the current configured risk basis, single-name cap, and 5% tier-kill threshold.

## Result

The combined Track M, family-router, mechanism-context, Swing, Track A, Track B, wire-time cap, and tier-kill suite reports **230 passed with 5 parameterized subtests**. Covered paths include decision admission, entry submission, fill confirmation, stop/OCO placement, invalid-geometry flattening, emergency-stop partial-fill recovery, exit reconciliation, EOD/tier-kill liquidation, and explicit proof that a Track-M router denial does not block Track A or Track B. The tier-kill boundary is pinned on both sides: a 4.2% combined loss must continue, while a 5.2% combined loss must kill at the shipped 5% threshold.

The test corrections make the preflight meaningful again; production code and live risk thresholds are unchanged.
