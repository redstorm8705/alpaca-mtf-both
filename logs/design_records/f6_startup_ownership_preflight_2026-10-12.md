# F6 startup ownership preflight — 2026-10-12 (OpenAI Codex)

## Scope

This is the first prerequisite for the CEO-approved F6 shared-symbol launch. It does
not enable F6 and does not change an order, allocation, entry, or exit. It establishes
one startup rule: F6 cannot buy or trim until broker orders, tagged fills, the ownership
ledger, and the live net position agree.

## Live invariant

At startup, when F6 is enabled, the bot runs the canonical full fill replay. The replay
attributes ownership through exact `F6-` client-order IDs. The preflight then requires:

1. the replay healed successfully;
2. the open-order book was readable;
3. no F6-tagged order remains open or partially unresolved;
4. every positive F6 claim is a whole, non-negative share count no greater than the
   live broker net; and
5. every F6-owned symbol has zero ledger drift.

Any failed condition writes the durable F6 sync-degraded interlock and pages. That
interlock blocks both new F6 starters and discretionary F6 trims. It never blocks an
ordinary Swing or Day exit. A later clean startup preflight clears the interlock.

## Interaction with the remaining launch work

This preflight closes restart ambiguity but does not close the broker-acceptance-to-fill
race. The next diff must reserve an unresolved F6 buy as protected ownership before
submission and make every reducing path honor that reservation. F6 remains disabled
until residual stop coverage, bounded extended-hours entry, fill-confirmed trims,
ETF eligibility, kill-switch attribution, allocator enforcement, simulations, and the
final risk gate are complete.

## Signature

Built by **OpenAI Codex**. Every commit and PR carries `(OpenAI Codex)`.
