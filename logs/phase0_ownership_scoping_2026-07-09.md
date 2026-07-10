# Phase 0 — Ownership Foundation — LINE-SCOPING (in progress, 2026-07-09)

Phase 0 of the per-tier ownership build (design: `logs/per_tier_ownership_design_2026-07-09.md`).
Goal: the ownership ledger + client_order_id tagging + floor-guard chokepoint + drift reconcile +
launch init, validated with intraday + QHM (before Forever-6). Also fixes the Movers/cross-strategy bug.

## NEW MODULES TO BUILD
- `execution/ownership_guard.py` — `check_never_sell_floor(symbol, tier, qty, side) -> GuardResult`
  (the single chokepoint) + ledger load/save (atomic RC-5) + `drift` reconcile + `get_combined_symbol_exposure()`.
- `data/state/ownership_ledger.json` — per-symbol {alpaca_net_qty, tiers{qty,avg_cost,last_fill_id}, drift}.
- Launch-init script (run ONCE, refuses if ledger has any nonzero F6/QHM qty).

## broker.py — EXACT CHANGES (FULL READ DONE: 780 lines)
Current state of client_order_id in the submit wrappers:
- `submit_market_order` (L164) — sets `client_order_id = mtf-{symbol}-{side}-{uuid}` (idempotency, reused on retry).
- `submit_limit_order` (L239) — same idempotency client_order_id.
- `submit_gtc_stop_order` (L317) — **NO client_order_id** on the StopOrderRequest (L340). MUST ADD.
- `submit_day_stop_order` (L435) — **NO client_order_id** (L456). MUST ADD.
- `partial_close_position` (L532) — MarketOrderRequest, no client_order_id (L556). Becomes tier-aware.
- `close_position` (L613) — `client.close_position(symbol)` whole-lot NON-DIVISIBLE = the Movers root.
- `close_all_positions` (L667) — kill-switch/`close_all_positions(cancel_orders=True)`; sells EVERYTHING
  incl. F6/QHM → MUST become floor-aware in a later phase (Phase 0: at least assert/skip ring-fenced).

Changes:
1. Add MANDATORY `tier: Literal["intraday","qhm","forever6"]` param (no default) to the 4 submit
   wrappers + partial_close. Change client_order_id format → `{TIER2}-{symbol}-{side}-{epoch_ms}-{uuid8}`
   generated ONCE per call, reused on retries (preserves the existing idempotency property). Add it to
   BOTH stop requests (currently untagged). Parse-friendly 2-char tier prefix (IN/QH/F6).
2. NEW `qty_bounded_partial_close(symbol, tier, qty)` — calls `check_never_sell_floor()` FIRST, submits
   only the guard-approved (possibly bounded) qty, tier-tagged, ledger-update-on-confirmed-fill.
3. `close_position()` — hard-DISALLOW (assertion) when the ownership ledger shows >1 tier nonzero for
   the symbol; callers must use `qty_bounded_partial_close`. (This is THE Movers-root fix.)
4. Every SELL/SHORT submit path imports + calls `check_never_sell_floor()` as a hard precondition.
5. `AlpacaBroker` adapter (L730, Movers-retired) — thread tier param through or leave inert.

## CALL SITES to update (mandatory tier param = breaking change; Rule C-6 each file own sequence)
submit_* / partial_close / close_position callers live in: `strategy/run_cycle.py`,
`execution/entry_logic.py`, `execution/exit_logic.py`, `execution/quarterly_hold_manager.py`
(via OrderDispatcher L256), `execution/orphan_manager.py`, `main.py`, `run_movers.py` (retired).
Each passes its tier (intraday for run_cycle/entry/exit, qhm for QHM/OrderDispatcher).

## STILL TO SCOPE (remaining Phase 0 reads before the exact diff + final gate)
- FULL READ `execution/entry_logic.py` (1687L → Explore) — REMOVE the registry entry-block (Item 3);
  tag intraday entries. (entry_logic reads get_quarterly_hold_symbols before the scan loop.)
- FULL READ the fill→tier attribution loop (`execution/fill_helpers.py` 369L + the portfolio_tracker
  fill-reconcile path) — attribute fills by client_order_id prefix; untagged→halt+alert.
- Spec `ownership_guard.py` pseudocode → concrete (from the masked-loss + execution seat designs).
- Launch-init: seed from get_all_positions(), existing GOOGL(1)/NVDA(1) → intraday, F6/QHM=0.
- Then: static + cold-2nd-agent + impact + FINAL Gro+GAI on the exact Phase 0 diff → API build.

## ENFORCEMENT NOTE (Rafael-locked)
Ring-fenced names are LONG-ONLY for share tiers (shorts→options program, separate). So the guard's
short-case on a floor>0 symbol = REJECT (no net-short accounting needed on ring-fenced names).
