# F6 pending-buy ownership reservation — 2026-10-12 (OpenAI Codex)

## Problem

An F6 starter currently creates a capital-allocator lease before submitting its buy, but the live allocator is disabled and that lease protects capital capacity only. It is not part of the never-sell ownership floor. Between broker acceptance/fill and the next exact fill replay, a reducing path can observe no F6 claim. The existing drift freeze protects many post-fill cases, but it does not create a durable pre-submit ownership fact and cannot close every concurrent submit/read race.

F6 remains disabled. This increment is a launch prerequisite and must stay dormant until both F6 and ownership enforcement pass their final gate.
With F6 disabled and no pre-existing F6 journals, ordinary broker paths do not acquire the new filesystem lock and ledger sync does not create or fsync the new journals. If F6 is disabled after a journal exists, exact reconciliation continues so disabling a flag cannot abandon unresolved ownership protection.

## Design

Add a dedicated durable pending-protected-buy journal. It is separate from the ownership ledger: the ledger records confirmed fills; the journal records unresolved broker intent and must never manufacture a filled share claim.

1. The F6 starter creates its deterministic `F6-` client order ID before either allocator admission or reservation. The exact same ID is passed unchanged to the allocator, reservation journal, broker submit, exact-ID recovery, and fill replay. There is no fallback-generated ID. The reservation also stores the schema-valid, whole-share F6 ledger claim immediately before submission as its immutable baseline.
2. The starter cannot submit if the reservation write fails. A capital lease may be released at this point because no broker call occurred.
3. After submit, a returned or exactly recovered order ID is durably bound to the reservation. A lost response, failed bind, unreadable lookup, open order, or partial fill keeps the reservation unresolved.
   A broker-proven pre-accept rejection is returned through an F6-only identity sentinel and retires both the protected-buy reservation and capital lease. Legacy callers retain the existing `None` contract. A timeout, duplicate client ID, exhausted retry, or absent exact-ID lookup never uses this path and remains unresolved.
4. Reconciliation releases a reservation only after one of two exact outcomes:
   - canceled/expired/rejected with zero broker fill, no exact fill records, and a clean canonical replay proving the F6 claim stayed at its pre-submit baseline; for the first-ever F6 attempt, baseline zero plus an authoritative replay with no symbol row is the exact no-position proof;
   - broker-proven terminal positive fill plus canonical ledger replay showing the F6 claim increased by at least the cumulative fill and broker/ledger drift is zero.
5. Startup reconciles this journal before declaring F6 ready. Any open, missing, malformed, unreadable, or not-yet-accounted reservation leaves the existing durable sync-degraded interlock set. A positive-fill release requires the replayed F6 claim to be at least `baseline + exact cumulative fill`, the replay tier sum to equal the exact broker net, and zero drift.
6. A shared per-symbol order-admission lock serializes both sides of the race. Every reducing broker path holds it from its journal/floor snapshot through submit acknowledgement. F6 holds the same lock from reservation creation through buy submit acknowledgement. The paths re-read the journal and revalidate immediately before submission. The lock is process-reentrant and uses `flock`, so nested broker helpers and separate OCI processes cannot deadlock or race. Before releasing the lock, every reducing path durably records the acknowledged exact order ID; ambiguous outcomes remain as unresolved reducing admissions. A reducer clears only inside `run_ledger_sync`, after the same canonical replay joins its exact terminal order and exact fills and proves zero broker/ledger drift. F6 only reads that durable result and cannot accept a stale pre-sell ownership baseline. This prevents both temporary order-list lag and the broker-terminal/ledger-stale window from crossing a later F6 fill.
   A reducer admission that returns before any broker mutation is explicitly released by the decorator. Every actual submit/replace/close boundary marks the admission attempted first; a failure after that boundary remains ambiguous and blocks. This prevents valid pre-submit refusals, including the pending-F6 guard itself, from leaking an `admitted` row forever.
7. Under that shared lock, the F6 pre-submit reads all live orders for the symbol and fails closed if **any** reducing order exists, regardless of tier or tag. This includes broker-resting Swing/QHM/Day/F6 stops, limits, OCOs, and bracket legs. The starter does not cancel them. This closes the otherwise-uninterceptable case where a previously accepted sell fires after the F6 buy fills.
8. The broker never-sell chokepoint consults the journal before its ordinary ledger early-return. Any unresolved reservation on the symbol rejects every new reducing order, including an authorized F6 trim. This intentionally favors temporary inaction over selling a just-filled F6 share.
9. The journal has its own atomic fsync/replace file lock, nested inside the symbol gate. A present but unreadable journal fails closed. Terminal records remain as an audit trail; only schema-valid `reserved`/`submitted` rows block. Any invalid row, duplicate active client/order ID, illegal status transition, missing ID/timestamp, or unknown version makes startup, F6 submit, and reducing admission fail closed.

## Existing architecture reused

- `tier_capital_allocator` continues to reserve capital when enabled and provides exact-ID recovery. It is not reused as ownership truth because `TIER_CAPITAL_ALLOCATOR_ENABLED=False` and its state is scoped to capital.
- `run_ledger_sync.sync_once` remains the sole confirmed-fill ownership authority. Reconciliation receives the exact fills, order map, and ledger result used by that same successful replay; it does not infer accounting from aggregate claim growth.
- `ownership_guard.check_never_sell_floor` remains the one reducing-order chokepoint. No strategy-specific sell bypass is added.
- Claude's bracket-add paths are untouched.

## Failure matrix

- crash before reservation: no broker call occurred;
- crash after reservation before submit: unresolved record blocks reductions and startup until an exact, identity-valid client-ID lookup proves terminal zero; absence is ambiguous and remains pending;
- accepted order with lost response: exact client-ID recovery binds it; ambiguity retains the reservation;
- exception after reservation: both the capital allocator and protected-buy journal run exact client-ID recovery; a proven terminal-zero order releases capital while an open/filled order binds both records, and an unreadable result retains both;
- explicit broker rejection before acceptance: an F6-only sentinel retires the still-unbound reservation and allocator lease, then stops the remaining starter batch; no reducing order remains blocked by a buy Alpaca proved it rejected;
- partial fill then cancel: reservation remains until the exact order's exact fill IDs and quantities are present in the same successful canonical replay and the symbol has zero drift;
- full fill before ledger replay: reductions block until replay accounts for it;
- terminal zero fill: release only for canceled/expired/rejected when the same clean replay contains no exact fill and the F6 claim remains at baseline; `filled` with zero is contradictory and stays blocked;
- journal corruption or lock/save failure: no new F6 submit; reducing guard fails closed and pages;
- duplicate client ID or second active reservation: reject before broker submission;
- reservation release/save failure: remains protected and blocks, never fails open.
- reducing order acknowledged but absent from a lagging order-list read: its durable admission stays active by exact order ID and blocks F6;
- ambiguous reducing submit or acknowledgement-journal bind failure: the admitted row remains unresolved and blocks F6 until operator reconciliation;
- terminal reducer before ledger replay: remains unresolved; full fills and canceled partial fills clear only after the exact fills appear in a clean canonical replay;

## Exact release proof

Every broker recovery validates the reservation's client ID, bound order ID, symbol, BUY side, positive whole submitted quantity, finite nonnegative filled quantity not exceeding submitted quantity, and a known status. Missing, malformed, duplicated, or conflicting lookup results remain pending. Terminal-zero release requires an exact canceled/expired/rejected order, no exact fill rows, a successful zero-drift canonical replay whose tier sum equals broker net, and an unchanged F6 baseline. A `filled` order must have a positive complete broker quantity. Terminal-positive release additionally requires: every matching fill has the bound order ID; the order map joins that ID to the reservation's exact client ID; each fill is a BUY of the reserved symbol with a unique nonempty fill ID and positive finite quantity; summed fill quantity equals broker cumulative fill; the just-completed canonical replay reports healed; and the symbol's ledger drift is zero. The terminal audit row stores the exact fill IDs and accounted quantity. Aggregate F6 claim growth alone can never release a reservation.

Reducing admissions follow the same standard: no live code path releases a submitted reducer from status alone. Zero-fill cancellations require a clean replay with no exact fills. Positive and canceled-partial reductions require exact positive fills whose sum equals broker cumulative fill, an exact client-order-ID join, and a healed zero-drift tier sum. The account-wide `close_all_positions` endpoint is the explicitly audited exception: it is reserved for emergency kill-switch liquidation, logs a CRITICAL ownership-bypass marker, and deliberately liquidates all tiers after Alpaca cancels resting orders.

## Test and launch boundary

Tests cover all crash windows, exact-ID recovery, partial and full fills, zero-fill release, malformed broker fields, duplicate reservation, journal corruption, concurrent lock serialization, startup interlock, and the broker guard on a ledger-unprotected symbol. The diff ships with F6 and ownership enforcement still off. Bounded extended-hours order entry, fill-confirmed trim resync, allocator/kill-switch/emergency-flatten attribution, ETF eligibility, end-to-end simulations, and the final flag flip remain pending.

## Signature

Built by **OpenAI Codex**. Every commit and PR carries `(OpenAI Codex)`.
