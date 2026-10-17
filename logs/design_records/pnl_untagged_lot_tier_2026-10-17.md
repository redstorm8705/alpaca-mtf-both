# P&L card: which tier gets an untagged lot's profit — design fork (2026-10-17, Claude) — needs Gro/GAI vote

**Trigger:** the 2026-10-16 "Session so far" card booked the owner's manual LLY sale (+$56.86) under Swing, though LLY was a long-term (QHM) hold. Diagnosis from the local session (handoff.md 2026-10-16 evening): LLY had two buys:
- 8/24, QH- tagged: qhm;
- 8/26, untagged (UUID client_order_id).

QHM resynced the 8/26 share into its hold (qty 1→2), and the ledger's QHM overlay counted it as qhm.

`scripts/pnl_snapshot._realized_by_tier` tags each FIFO lot only by its opening order's client_order_id. Untagged defaults to "intraday", which is shown as Swing. The 10/14 sale took the 8/24 QH lot by FIFO. The 10/16 sale took the untagged 8/26 lot, so it was booked to Swing.

## Why there is no one-line fix (read at source)
- `execution/ownership_guard.sync_ledger` (lines 1092-1300) also attributes untagged fills to intraday. It corrects them only at the symbol level, in two ways:
  - an untagged sell where intraday holds nothing goes to the protected tier;
  - the QHM overlay raises qhm to the QHM manager's current claimed count.
- Neither records WHICH lot is protected, and the QHM claim is current-state only. After LLY's sale, the claim is 0.
- The tier-transfer journal (`execution/tier_transfers.py`) is the per-lot hand-over record, but by contract it excludes protected tiers.

## Options
- **A. Extend the ledger's untagged-sell rule to the P&L FIFO.** An untagged close is booked to the protected tier when the symbol has protected lots.
  - This does NOT fix LLY. By 10/16 the only remaining lot was the untagged one.
- **B. Book an untagged-lot close to the tier the ownership ledger holds for that symbol at card time,** when the ledger shows no intraday or daytrade shares there.
  - This fixes the intraday cards on the sale day, because the ledger lags.
  - It is wrong after the ledger heals: the post-close card reads qhm=0 and falls back to Swing. It is not reliable.
- **C. Fix it upstream.** When QHM adopts an untagged buy into its hold (its resync), record a protected-tier adoption entry that names the order id. The P&L FIFO, and the ledger replay, then tag that lot qhm.
  - Exact and per lot.
  - Needs a change in `execution/quarterly_hold_manager.py` (owned by another session) plus a journal-schema decision: extend `tier_transfers` to protected-tier adoptions, or add a separate "protected adoption" journal.
- **D. Do nothing in code.** Untagged buys are rare (legacy or manual). Show "Other (untagged)" instead of Swing for untagged lots, so the card never claims a tier it cannot prove.
  - Cheap and honest, but it does not give QHM its P&L.

## Recommendation
**D now, C as the real fix.**
- D is a display-only change in pnl_snapshot.py. It stops the false "Swing" claim without guessing.
- C closes the root cause: an untagged lot silently becomes protected with no per-lot record.

Neither changes size, frequency or concurrency.

## Open questions for the vote
1. Is D acceptable as the interim?
2. Should a separate "Other" row on the realized card be shown only when material, mirroring the unrealized card's ≥ $0.50 rule?
3. For C: extend `tier_transfers` to protected adoptions, or use a new journal?
