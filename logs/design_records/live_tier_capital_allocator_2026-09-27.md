# Live Tier Capital Allocator — Design Record

**Owner/signature:** ChatGPT/Codex
**Date:** 2026-09-27
**Branch:** `feat/live-tier-capital-allocator`
**Status:** ACTIVE BUILD — not yet shipped
**Claude boundary:** Claude owns Confluence 2.0 entry-model work. This ChatGPT/Codex build owns account-level capital admission at the final wire-time entry chokepoints. Confluence may review realized results weekly but must not create a parallel allocator or bypass this gate.

## Rafael mandate

No shadow deployments. After offline tests and the mandatory BGGN/mechanical gates pass, this allocator ships directly into the paper-trading entry paths. User-facing terminology is **Swing**, never “Core MTF.”

## Normal target envelopes

- Day Trade: 40%
- Swing: 20%
- QHM: 30%
- Forever-6: 10%, dynamically expandable after verified crash activation

These are owner gross-exposure envelopes, not four independent cash reservations. QHM and F6 remain settled-cash-only. Day Trade and Swing may use margin within account gross, maintenance, tier-local stop-risk, and kill-switch constraints.

## Live enforcement boundary

The allocator clamps or blocks new entry quantity immediately before broker submission. Existing over-cap positions are grandfathered. Exits, protective orders, stop replacement, reconciliation, and Day Trade forced-flat actions bypass the allocator. Missing broker, ownership, pending-order, price, stop, or account evidence fails closed for new entries only.

QHM's persisted pre-cap share baseline is excluded from its new-money tier envelope and
from the allocator's account-gross admission calculation. The exemption is computed by
QHM from current prices, is accepted only for QHM, cannot exceed allocator-verified QHM
ownership, and ratchets downward after a sale. Those holdings still affect broker cash,
maintenance margin, and every protective-stop control.

Swing and F6 market orders reserve a 1% adverse-execution buffer before submission. This is an admission buffer, not a guaranteed fill ceiling: any fill beyond it is immediately reflected in broker and ownership gross and grandfathered, so it blocks subsequent entries rather than forcing a sale. Unknown or unaccounted fills retain the lease.

After a broker submit returns an order, a failed local bind must retain the durable
pre-submit reservation. Releasing it would treat a possibly live broker order as free
capital and permit double allocation. Reconciliation resolves the reservation by its
deterministic client-order ID. A missing/None response also requires exact-ID recovery;
release is safe only after broker-proven terminal zero fill or equivalent absence proof.

## BGGN state

Initial Board, Google AI Studio, and Groq reviews rejected direct use of the current contradictory knobs and aligned on a single account-wide wire-time gate. Final implementation review is pending exact code. NVIDIA is backup-only per Rafael’s standing direction.
