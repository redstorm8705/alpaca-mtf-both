# P0 — move stops in place, never cancel-then-replace (2026-09-25)

**Approved by Rafael:** 2026-09-25 ("Approved"), for the proposal to move stops in place, with a retry next cycle and then a page.
**Design review, cold, on `scratchpad/p0_design.txt`:** all four voices returned APPROVE-WITH-CHANGES.
- Harris/Peterffy (execution seat)
- Taleb/Thorp (masked-loss seat)
- Gro (`gpt-oss-120b`)
- GAI

## Verified facts (Alpaca API reference, PATCH /v2/orders/{order_id}; alpaca-py 0.43.3 on OCI)
- A successful replace returns the NEW order with a new id. The old order goes to status `replaced`, and its `replaced_by` field holds the new id.
- An order cannot be replaced while it is `accepted`, `pending_new`, `pending_cancel` or `pending_replace`.
- While an order is `pending_replace`, cancel requests are rejected.
- If the old order fills before the replacement reaches the venue, the replacement is rejected.
- `ReplaceOrderRequest` accepts `qty` together with `stop_price`. Fields confirmed: `qty`, `time_in_force`, `limit_price`, `stop_price`, `trail`, `client_order_id`.

## Consolidated required changes (board + Gro + GAI)

| # | Change | Increment |
|---|---|---|
| 1 | `broker.replace_stop_order(symbol, order_id, stop_price, qty=None)`: one PATCH, no cancel, no sleep. If the call errors, read the old order back; if it is already `replaced`, adopt the order that replaced it. | broker.py (this increment) |
| 2 | `broker.resolve_live_order(order_id)`: follow the `replaced_by` chain to the order in force. | broker.py |
| 3 | `broker.cancel_stop_confirmed(symbol, order_id, max_wait_s=2.0)`: returns True only once the broker confirms the stop is no longer in force. It follows the chain first and waits (bounded) through transitional statuses. HTTP 422 is never read as success. | broker.py |
| 4 | The partial-exit site shrinks the stop to the remaining shares plus the new price BEFORE the partial sell, not a price-only move. | exit_logic increment |
| 5 | Send an explicit quantity on every move, capped at min(qty_remaining, Alpaca position qty). | lifecycle / exit_logic |
| 6 | At most one replace per symbol per cycle. A move skipped for that reason is not a failure. | lifecycle / exit_logic |
| 7 | Set the breakeven / trail flags only after a successful move. On failure, keep the old id and the old level. Save immediately after each successful move. Store `broker_stop_px` from the returned order. | lifecycle / exit_logic |
| 8 | Every stop-move or resubmit site passes `allow_cancel_blocking=False`. On `PROTECTION_ALREADY_HELD`, adopt the live stop and move it. | lifecycle / exit_logic |
| 9 | Every status check (exit_logic ~L422, the reconciler, startup) follows `replaced_by`. | exit_logic / reconciler increment |
| 10 | Two-tier paging (see below). Page text states the broker status it read, never "unprotected" unless checked. | lifecycle / exit_logic |
| 11 | Exits are booked from actual fills only (RC-4). `trade["stop"]` is never an exit-price fallback. | verify in exit_logic |
| 12 | DAY and GTC stop ids both set is an anomaly: page on it and move each live one. | lifecycle / exit_logic |
| 13 | Log every move to `trade_events.jsonl`: old id, new id, old price, new price, outcome. | lifecycle / exit_logic |
| 14 | Increment 2: the reconciler runs every RTH cycle for broker-stop positions, handling `replaced` / `pending_replace` / `replaced_by`. It is the backstop for "move accepted but the new order was rejected". | stop_protection increment |

**Two-tier paging (change 10):**
- **Old stop confirmed live, move failing:** WARNING after 3 consecutive failures, at most every 30 minutes.
- **No live broker stop confirmed:** CRITICAL on the first occurrence.

## Rejected or not adopted, with reasons
- **GAI "orphaned stop after the old one fills"** (the PATCH returns 200, the old order fills, and the new one stays live): contradicted by the API reference, which says the replacement is rejected in that case.
- **GAI "update local state before the API call, then roll back"**: rejected. Writing a stop the broker does not hold is the masked-loss pattern. Local state is updated only after a confirmed move.
- **Gro "Prometheus metric"**: there is no Prometheus exporter in this repo. `trade_events.jsonl` records are used instead (change 13).
- **Fixed sleep between cancel and resubmit** (the original owner idea): rejected 5/5 in the 2026-09-25 BGGN. There is no documented cancel latency, and a sleep blocks the trading thread.

## Tests (required across increments)
1. Replace succeeds.
2. Replace raises, but the order was actually replaced.
3. Old stop fills first, so the replacement is rejected.
4. `pending_replace`, then a cancel or close.
5. A 422 on cancel.
6. A replaced id loaded on restart.
7. No stored id while a live stop exists.
8. Both DAY and GTC ids set.
