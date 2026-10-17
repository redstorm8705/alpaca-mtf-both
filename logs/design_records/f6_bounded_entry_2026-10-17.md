# F6 bounded entry — 2026-10-17

**Owner:** OpenAI Codex
**Status:** implementation under final gate; F6 remains disabled

## Problem

The dormant F6 starter used one-share market buys after the regular close. Its local
cash reservation was an estimate based on the planner price. A queued or delayed
market fill therefore had no hard execution ceiling, and the amount reserved locally
could differ from the broker's eventual spend. The post-buy verifier also assumed an
accepted order should become a ledger-owned share within fourteen seconds. That is
invalid for a resting DAY limit order.

## Board decision

The first bounded-limit proposal was rejected by both independent board seats. The
direction was correct, but the lifecycle was incomplete. The revised contract includes
every required correction:

1. F6 starters submit only from 4:05 PM through 8:00 PM ET on weekdays, rechecked at
   the money-moving boundary. F6 trims retain their separate existing window.
2. The first current-session quote observation arms a symbol but cannot authorize a buy.
   A later scan cycle must observe a strictly newer exchange timestamp for every name in
   the basket. The order boundary then re-reads each quote and rejects a regression,
   malformed/crossed/future quote, or closed session. This proves liveness without
   assuming that two back-to-back REST calls will contain a new print. There is no
   fallback to the planner price.
3. The confirmed ask is rounded upward once to the broker's one-cent stock tick. That exact value
   is used for cash capacity, allocator admission, the durable protected-buy record,
   the broker request, and reporting.
4. Settled cash, the dry-powder floor, the event cap, the name cap, ownership, reducing
   orders, instrument eligibility, and empirical-trigger evidence are rechecked before
   each submission.
5. The same-day event intent is fsynced before the broker call. An ambiguous result
   remains counted and blocks a duplicate event. A broker-proven preaccept rejection
   may release an otherwise empty event.
6. The pending-buy journal stores and re-proves the exact limit, LIMIT order type, DAY
   time in force, extended-hours flag, client ID, order ID, symbol, side, and quantity.
7. Explicit broker rejection is distinct from timeout, duplicate client ID, malformed
   response, and transport uncertainty. Only explicit rejection can retire both
   pre-submit reservations without fill replay.
8. An accepted open order with zero fills is reported as pending. It keeps both durable
   reservations and does not create a false ledger-sync failure. Positive fills still
   require canonical fill/ownership replay; terminal zero-fill orders release only
   through that replay.
9. Accepted orders are reported as submitted and reserved, with actual spend fixed at
   zero until broker fills establish it. The anti-overtrade cap counts a submitted or
   ambiguous event, but the record distinguishes it from a fill.
10. A kernel file lock serializes event-cap recheck and event-intent persistence across
    bot processes. A failure on a later basket name can update only an intent that name
    actually wrote, so it cannot suppress verification of an earlier accepted order.
11. The protected-buy journal migrates an empty schema-1 file to schema 2. A nonempty
    legacy journal remains fail-closed because the missing price/order contract cannot
    be reconstructed safely.

## Quote freshness decision

The boundary requires a current-session quote timestamp to advance across scan cycles.
A frozen 4:05 PM quote therefore cannot authorize a 7:59 PM order, while an ordinary
latest-quote REST endpoint is not expected to manufacture a new print within milliseconds.
The first observation deliberately submits nothing. Every name in the proposed basket
must prove liveness before the first order, preventing a half-built basket from consuming
the daily event. A cached observation from a prior ET date can only seed today's first
observation; it cannot arm an order until today's timestamp advances again. This
watermark is monotonic within the ET date: a regressed timestamp is rejected without
lowering it. Basket watermark advancement is atomic; when one sparse name blocks, other
names keep their earlier watermark so their already-newer print remains eligible on the
next scan. This data-derived proof avoids an invented fixed seconds-old cutoff and
preserves the no-static-threshold rule.

## Failure contract

| Outcome | Capital reservation | Protected-buy reservation | Event record | Sync degraded |
|---|---|---|---|---|
| Accepted, zero fill | retain | retain | submitted | no |
| Partial/complete fill | retain until canonical replay | retain until canonical replay | submitted | only if actual filled ownership cannot be proved |
| Expired/canceled, zero fill | release only after canonical replay | release only after canonical replay | remains distinguishable as submitted | no |
| Explicit preaccept rejection | proven-no-order release | explicit-reject release | release if no other submitted/ambiguous intent | no |
| Timeout/duplicate/unknown | retain | retain | ambiguous | no ownership claim; future entry blocked |

## Scope and launch status

This prerequisite changes dormant F6 entry plumbing only. It does not enable F6, alter
the empirical trigger, change the four-tier allocation, or modify Claude's strategy
work. `FOREVER6_ENABLED` remains `False`. F6 launch still requires trim resync,
allocator/emergency-flatten integration, ETF wind-down detection, the full-path
premarket simulation, and Rafael's one-page approval package.

— OpenAI Codex
