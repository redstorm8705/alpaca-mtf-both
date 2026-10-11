# QHM stop-to-owned-quantity reconciliation — 2026-09-29

**Author/signature:** OpenAI Codex (GPT-6) · risk-path design and implementation

## Defect

`run_weekly_check()` refreshes QHM state from the full Alpaca symbol position but does not prove that the live QH-tagged protective stop covers the QHM-owned shares. Because symbols may be shared across tiers, the full broker position is not a valid QHM ownership quantity. A tranche add, partial fill, manual change, ledger delay, or stale replacement ID can leave the registered stop quantity different from QHM ownership.

## Proposed invariant

For every ACTIVE QHM long, on every RTH manager cycle:

1. Read QHM ownership from the ownership ledger and cross-check it is positive and no greater than the broker net position (`_strict_resync_qty`). Never convert another tier's shares into QHM.
2. Read the broker order book. Unknown ledger, position, or order state blocks mutation and emits a bounded alert; it never guesses.
3. Identify live, reducing, QH-tagged stop orders by symbol, side, type, status, and remaining quantity.
4. Adopt one exact-cover stop before `cancel_stray_sell_orders()` so a correct replacement with a stale local ID is not cancelled as a stray.
5. When exactly one safe QH stop is live with the wrong quantity, resize it atomically with `replace_stop_order(..., qty=qhm_owned_qty)`. Never cancel first. Persist the replacement ID before reporting success.
6. When no QH stop exists, transition durably to `PENDING_STOP_REPLACE`; the existing recovery path submits a stop only after ownership proof.
7. Multiple QH stops are an ambiguous/oversell condition: do not resize blind. The existing registered-ID stray guard removes unregistered sells; reconciliation reruns after cleanup. If there is no trustworthy registered stop, alert and fail closed for operator/recovery handling.
8. A persistence failure after a successful replace retains recovery state and must not claim the state machine is healed. The broker stop remains in force.

## Ordering

Run an adoption/coverage preflight before the stray-sell guard, then rerun coverage after cleanup and ownership refresh in the ACTIVE position loop. This protects a valid replacement ID from accidental cancellation while still cleaning duplicate/unregistered sells.

## Required tests

- exact QH stop + exact QHM ledger quantity: adopt/no broker mutation;
- stale registered ID + exact replacement: adopt before stray cancellation;
- one under/over-sized QH stop: atomic replace and persisted new ID;
- replacement failure: old stop retained, no cancel, recovery remains armed;
- no live QH stop: durable `PENDING_STOP_REPLACE` and existing resubmit path;
- order API, ownership ledger, or broker position unreadable: no mutation;
- QHM ownership less than shared-symbol broker net: stop uses QHM quantity only;
- QHM ownership greater than broker net: reject proof/no mutation;
- multiple QH stops: no blind resize or oversell;
- filled/partially-filled/terminal/wrong-side/untagged orders never count as exact protection;
- state-save failure after replace never reports healed;
- replacement-chain/lost-reply adoption preserves QH ownership tag.

## Scope boundary

This changes protective-stop reconciliation and adds one exact-fill ownership-ledger
write primitive. It does not change entries, strategy exits, allocation targets,
Confluence 2.0 scoring, thresholds, or the ownership ledger schema.

## Board-required revision — round 2

The first design was rejected before implementation. The corrected mechanism is a single-snapshot, two-phase preflight:

1. `run_weekly_check()` obtains one full open-order snapshot. An unreadable snapshot causes no stop adoption, replacement, or stray cancellation.
2. For each ACTIVE QHM hold, `_strict_resync_qty` proves ledger-owned QHM quantity is positive and no greater than the signed live broker net before candidate selection.
3. Candidate validation requires every field: matching symbol, QH ownership tag, reducing `sell` side, stop order type, stable live/non-terminal status, finite positive stop price equal to the stored stop, `0 <= filled_qty <= qty`, and exact positive `remaining_qty = qty - filled_qty`. Unreadable fields are invalid and page.
4. Exactly one valid exact-cover candidate may be adopted. The new ID and ACTIVE state must be durably saved before the stray guard can run. If persistence fails, that symbol is added to a skip set so the same pass cannot cancel the unpersisted stop.
5. Exactly one sole, stable, live, QH-tagged reducing stop may be atomically replaced when its price or remaining quantity differs. The request uses stored stop price and exact QHM-owned quantity. After success or lost-reply recovery, validate the returned live replacement again for tag, side, type, status, price, and exact remaining quantity; rerun `_strict_resync_qty`; only then durably save its ID. Any ambiguity leaves the old stop in force, persists `PENDING_STOP_REPLACE` recovery state, pages, and blocks both duplicate submission and stray mutation for that symbol.
6. Multiple QH reducing stops, partial fills, pending/replaced states, or mixed candidates are ambiguous. Perform no adoption, resize, cancellation, or resubmission for that symbol in this pass; persist `PENDING_STOP_REPLACE`, page, and require a fresh full snapshot on a later pass/operator recovery. Never assume an extra stop cancelled cleanly or did not partially fill.
7. No QH stop transitions durably to `PENDING_STOP_REPLACE`. Existing recovery may run only after a fresh snapshot and a second ownership proof; it cannot submit into an ambiguous book.
8. `cancel_stray_sell_orders()` receives the exact snapshot plus a per-symbol skip set. It preserves only durably registered verified stops and skips every ambiguous/save-failed symbol. It does not re-read the book, so adoption and cleanup cannot disagree about which orders existed.
9. After any adoption or replacement outcome, rerun `_strict_resync_qty`. A shrunk broker net or ledger disagreement blocks success and persists recovery evidence. State-save failure never reports healed.

Additional mandatory tests: same-snapshot ordering; adoption save failure prevents cleanup; exact field validation (wrong side/type/status/tag/price/partial/unreadable); shared-symbol broker net larger than QHM ownership; QHM ownership larger than broker net; replace return and lost-reply result revalidation; wrong replacement price/quantity; multiple QH stops cause zero mutation; partial-fill ambiguity causes zero mutation; cleanup uses the supplied snapshot once; and no duplicate open-order reads.

## Board-required revision — round 3 predicate split

Two predicates govern the sole-stop path:

- `replaceable_sole_qh_stop`: matching symbol; QH ownership tag; reducing `sell` side; stop type; stable live/non-terminal status; finite positive stop price; parseable quantity fields; and `0 <= filled_qty <= qty` with positive remaining quantity. It deliberately does **not** require price or quantity to match desired state. Exactly one such candidate and no other QH reducing-stop candidate may reach atomic in-place replacement.
- `exact_cover_stop`: satisfies every `replaceable_sole_qh_stop` condition **and** has stop price equal to the stored QHM stop price and remaining quantity equal to exact ledger-proven QHM ownership.

Only `exact_cover_stop` may be adopted. A sole replaceable candidate that is not exact is atomically replaced. The returned or lost-reply recovered replacement must satisfy `exact_cover_stop`, followed by a second ownership/broker-net proof and durable state save, before success. Anything outside the replaceable predicate is ambiguous and receives zero mutation in that pass.

## Board-required revision — round 4 recovery idempotency

Every `resubmit_stop_if_needed()` caller supplies or obtains a readable full order-book
snapshot. The recovery function itself rejects multiple or malformed QH reducing stops,
so RTH and after-hours callers cannot bypass the invariant. Ordinary missing-stop
recovery also closes the snapshot-to-submit race as follows:

1. Generate one QH-tagged recovery `client_order_id`, store it on `HoldPosition`, and
   durably save state before sending an order.
2. Look up that exact client ID before each send. If it resolves, validate exact stop
   shape, quantity, and price and adopt it; never submit again.
3. Re-read the full broker book immediately before the first send. An unreadable book or
   any visible QH reducing stop blocks submission and leaves recovery pending.
4. Pass the persisted client ID through `OrderDispatcher` to
   `broker.submit_gtc_stop_order`; broker retries reuse it. A timeout, restart, or failed
   ACTIVE-state save therefore cannot create a second order from the same recovery intent.
5. Clear the recovery client ID only in the same durable save that records the accepted
   stop as ACTIVE. If that save fails, restore the client ID and remain pending so the
   next cycle recovers the exact broker order.

Required race test: an initially empty snapshot followed by a QH stop appearing on the
fresh pre-send read results in zero submission and a pending state. Required cross-path
test: two live QH stops block fill-aware recovery in every caller.

## Post-rebase BGGN corrections — 2026-10-11

The implementation was rebased onto canonical ledger v2 and rejected in its first
post-rebase review. The exact diff now adds these required controls:

1. Broker and ledger quantities must be finite, non-boolean whole shares. Fractional,
   infinite, or malformed evidence is rejected rather than truncated.
2. Startup and RTH preflight failures are isolated per symbol. One corrupt hold is
   blocked while every other hold and the QHM-owned stray guard still run.
3. The stray guard only cancels `QH-` orders. Day, Swing, and Forever-6 protective
   orders on a co-held symbol are outside QHM ownership and are preserved.
4. Every internal GTC retry reuses the caller's durable client order ID.
5. Terminal recovery evidence is status-consistent: `filled` means the submitted
   quantity filled; rejected/expired orders cannot report fills.
6. Exact adoption and atomic replacement clear recovery intent in the same durable
   state save and restore it if that save fails.
7. A registered QHM stop that filled while another tier still owns the symbol is
   resolved by exact order ID and exact filled quantity. A new ledger primitive reduces
   only QHM's claim when the complete ledger total minus that fill equals the fresh
   broker net. It records the exact stop ID as `last_fill_id`, making a retry idempotent.
   If any proof disagrees, the hold remains pending and no replacement stop is sent.

Signed: OpenAI Codex (GPT-6), 2026-10-11.
