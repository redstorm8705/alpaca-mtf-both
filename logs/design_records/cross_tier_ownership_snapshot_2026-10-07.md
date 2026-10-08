# Cross-Tier Ownership Snapshot — 2026-10-07

**Author/signature:** OpenAI Codex (GPT-6)

**Scope:** shared read-only ownership contract; live day-tier routing integration

**Does not change:** signals, sizing, targets, stops, order submission, or Claude's Confluence 2.0 work

## Verified problem

Alpaca reports one net position per symbol, while the bot has separate day-trade,
swing, QHM, Forever-6, and main swing owners. The interim day-tier routing helper
treated any symbol present in `day_tier_events.jsonl` as fully day-tier-owned. It did
not compare the day-tier quantity with the broker net. For a broker position of three
AAPL shares where the day tier owned one and another tier owned two, the helper would
remove AAPL from the foreign-held set. That is a false exclusive-ownership result.

Claude's 2026-10-07 restart fixes correctly prevent the main tracker from adopting
current day-tier and breakout positions. This build extends that work; it does not
replace it.

## Decision

Add `execution/ownership_snapshot.py` as the shared read-only join between:

1. the signed live broker net for each symbol;
2. signed per-tier quantities in `ownership_ledger.json`; and
3. the day tier's current-session durable lifecycle log, which closes the ledger's
   up-to-20-minute refresh gap.

The lifecycle reader now exposes a checked open-set API. A torn JSON line or a
semantically invalid entry/partial-exit quantity makes the source incomplete; the
snapshot will not use the readable prefix as ownership proof. A complete current-day
log replaces the periodic ledger's day-tier slice in both directions, including
clearing a stale ledger claim after a completed exit.

Exclusive ownership is true only when the tier's signed claim equals the entire
broker net, every other known claim sums to zero, and every ownership source was
read successfully. This handles longs, shorts, partial positions, and same-symbol
co-holds. Broker-read failure raises; it never becomes an empty book. Any ledger or
log read failure makes every broker-held symbol foreign for that snapshot, so a
remaining stale claim cannot manufacture exclusive ownership. Duplicate, missing,
invalid, or non-finite broker rows reject the whole snapshot rather than silently
dropping or overwriting a live position.

The first live consumer is `run_day_tier._held_by_other_tiers()`. Its existing
no-co-hold behavior remains in force, but routing now uses quantity proof rather than
symbol membership. Order-time enforcement in `day_trade_manager.place_entry()` is
unchanged and remains the final guard.

## Why this increment

The allocator, startup reconciliation, stop ownership, and tier reporting currently
derive ownership independently. A small immutable contract gives those paths one
testable input without changing their behavior in the same release. Later increments
can consume the same snapshot and remove duplicated symbol-level exclusions only
after their reducing-order paths are quantity-bounded.

## Mechanical evidence

- New contract tests cover signed long/short ownership, partial co-holds, ledger lag,
  source corruption, stale prior-day log entries, and malformed broker payloads.
- The focused day-tier and ownership suites pass.
- Ruff and mypy pass for the new module and tests.
- The code is deployed-only after exact-diff BGGN and preship gates; it must be called
  **deployed, unexercised** until a production runner tick uses it.
