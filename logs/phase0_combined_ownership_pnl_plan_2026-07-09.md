# PHASE 0 (COMBINED) — Per-Tier Ownership + Broker-Authoritative P&L — BUILD PLAN (2026-07-09)

Rafael approved merging the fail-proof P&L fix INTO Phase 0 (they are the same problem — correct
per-strategy P&L requires execution-time ownership). Sources: `logs/phase0_ownership_scoping_2026-07-09.md`,
`logs/phase0_ownership_guard_spec_2026-07-09.md`, `logs/pnl_failproof_solution_2026-07-09.md`.

## WHY ONE BUILD
Alpaca FIFO is SYMBOL-level. Per-strategy realized P&L is impossible unless the bot never sells across
two strategies' inventory — that guarantee IS the Phase 0 ownership ledger + `qty_bounded_partial_close`.
So: ownership layer (correct attribution) + broker-authoritative P&L (correct numbers) + state-
reconstruction invariant (ties both to Alpaca truth) = ONE coherent package.

## CURRENT STATE (verified 2026-07-09)
- `reporting/pnl_ledger.py` (570L) — the stateless full-history FIFO-from-Alpaca-fills engine, ALREADY
  BUILT, ZERO importers (confirmed unwired). This is the ~90%-done authoritative realized-P&L source.
- `execution/fifo_pnl.py` (415L) — the OLD incremental `_fifo_reconstruct` (to be retired).
- `execution/fill_helpers.py` (369L) — `fetch_actual_fill_price` (the estimate-at-exit recovery to DELETE).
- `execution/state_io.py` (111L) — atomic-write primitives (KEEP — reused by the ledger).

## THE COMBINED BUILD — SUB-PHASES (each its own gate + API build)

### P0-a — OWNERSHIP FOUNDATION (already scoped)
Ledger `data/state/ownership_ledger.json` + `client_order_id` tier-tagging + mandatory broker `tier`
param + `check_never_sell_floor()` chokepoint + `qty_bounded_partial_close` + `close_position` hard-
disallow on multi-tier + drift reconcile + launch-init. Files/lines already scoped in the scoping doc
(broker.py, entry_logic.py L437-440, exit_logic.py 23 reducing-order sites, fill_helpers.py). Validated
with intraday+QHM. **Also fixes the Movers/cross-strategy bug.**

### P0-b — BROKER-AUTHORITATIVE P&L (wire-in + deletions)
**WIRE IN (make authoritative):** `reporting/pnl_ledger.py` becomes the SOLE realized-P&L source.
FIRST build step = full-read `reporting/pnl_ledger.py` (570L) to confirm: it ingests ALL Alpaca fills,
pure-function FIFO, per-`client_order_id` attribution (extend to tier prefix from P0-a), dedupe by
`activity_id`. Add the **fill-ingestion** (WebSocket `trade_updates` + polling backstop) into the
append-only immutable ledger.
**READ authoritatively (never compute):** total→portfolio-history (advisory), unrealized→position
objects. Strip these from any local computation.
**DELETE / STRIP (the three parallel engines — leaving them is why it recurred):**
- `fetch_actual_fill_price` / `_fetch_actual_fill_price` — REMOVE the estimate-at-exit path. Call sites
  (~10): fill_reconciler.py, gtc_manager.py, portfolio_tracker.py, orphan_manager.py, trade_engine.py,
  events/handlers.py, exit_logic.py, run_cycle.py, fill_helpers.py(def), preflight_simulation.py.
- `execution/fifo_pnl.py` `_fifo_reconstruct` — RETIRE (superseded by pnl_ledger.py).
- `record_exit` / `record_partial_exit` (~14 callers incl. exit_logic 23 sites, orphan, run_cycle, QHM,
  handlers) — **STRIP the P&L computation; KEEP the exit-STATE transition** (record_exit does double
  duty: state machine + P&L; it must stop returning/booking a P&L number and instead just advance state;
  P&L is derived from the fills ledger on demand). This is the delicate one — do NOT delete record_exit,
  de-P&L it.

### P0-c — STATE-RECONSTRUCTION INVARIANT (the fail-proof lock)
Every cycle: replay the fills ledger → reconstruct per-(symbol,tier) qty + avg_cost → compare to Alpaca
live position objects (qty) AND the ownership ledger (P0-a). Completeness check: every Alpaca activity
fill present exactly once. **Confirmed break (≥2 cycles)** → FREEZE symbol + CRITICAL + manual (NEVER
auto-correct); **pending-settlement (<2 cycles)** → no freeze. Reconciliation-check throws → default
FREEZE. First post-restart cycle = read-only. Portfolio-history = advisory chart only, NEVER the freeze
trigger (board seat: per-day realized_pl ≠ per-trade → false breaks on overnight holds).

### VALIDATION (before flipping authoritative)
Shadow mode weeks; property-based FIFO tests (random buy/sell/partial/multi-strategy → realized sum ==
proceeds−cost; open-lot qty == Alpaca qty); GOLDEN-MASTER: replay the REAL bugged-months fills, assert
pnl_ledger reproduces Alpaca account realized for that period. Only then flip per-component + enable
freeze + DELETE old engines. Write the 2am manual-reconcile runbook for a permanently-missing Alpaca fill.

## WHAT THE TRACKER KEEPS (never remove): exit-state machine (ACTIVE/AWAITING_FILL/PENDING_*), GTC
lifecycle, Kelly R-multiple stats (rebuilt from the authoritative ledger, no phantom losses), kill-switch
input (now from real P&L), per-tier attribution, dashboard/weekly/monthly surfaces. Remove ONLY the P&L
numbers; keep the state.

## SEQUENCE (each = own full-read gate + static + cold-2nd + FINAL Gro+GAI on the diff → API build)
P0-a (ownership: finish scoping portfolio_tracker fill-reconcile + call sites → diff) → P0-b (P&L wire-in
+ deletions; full-read pnl_ledger.py first) → P0-c (invariant + freeze). Then Phase 1 (per-tier P&L
history + synced stops), Phase 2 (Forever-6). The old Build A (safe-mode) + OPT-2 (event-sourced replay)
roadmap items are SUBSUMED by P0-b/P0-c — retire them as separate items.

## NEXT CONCRETE STEP: finish P0-a scoping (portfolio_tracker fill-reconcile path + remaining call
sites) AND full-read `reporting/pnl_ledger.py` (570L) for P0-b — both with fresh context → then assemble
the P0-a diff → gate → API build.
