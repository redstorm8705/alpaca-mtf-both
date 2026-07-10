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

## entry_logic.py — EXACT CHANGES (FULL READ DONE via Explore: 1688 lines)
- **REMOVE the registry entry-block: lines 437–440** verbatim:
  `# QHM exclusion: skip intraday entries...` / `if symbol in get_quarterly_hold_symbols():` /
  `_rc8_clear_buffers(symbol, "qhm-hold")` / `continue`. This is what blocks confluence from entering
  ring-fenced names — deleting it un-blocks them (Rafael mandate).
- **Import at L64 STAYS** (`from execution.quarterly_hold_manager import get_quarterly_hold_symbols`):
  L406–410 CYCLE-SYNC-GUARD still calls it to exclude QHM from a tracker-open count. **BUT that
  count-sync must become TIER-AWARE** under coexistence — intraday can now legitimately hold a QHM/F6
  name, so excluding all QHM symbols from the intraday count is now wrong. Review L406–410.
- **4 submit call sites (add tier param):** L676 `submit_market_order(...side=_exit_side)` — a #12c
  opposite-signal EXIT = a REDUCING order → must route through the floor guard, not just be tagged
  (tier=intraday); L1287 `submit_market_order` main intraday entry (tier=intraday); L1360
  `submit_gtc_stop_order` overnight GTC stop (tier=intraday); L1675 `submit_limit_order` overnight DAY
  limit (tier=intraday for Phase 0; swing tier deferred).
- Two entry flows: `execute_entries()` L271 (intraday) + `_overnight_entry_check()` L1495 (swing —
  deferred, tag intraday for Phase 0). Kelly sizing L1078–1136 intact. No `_get_quarterly_notional_excl`
  call in this file (it lives in QHM/risk_manager — Phase 1 generalizes it there).

## fill_helpers.py — (FULL READ DONE: 369 lines)
Recovers fill PRICES (fetch_actual_fill_price, by symbol/side/time-bound) — does NOT read
client_order_id, does NOT attribute to a tier. So **fill→tier attribution is NET-NEW** at the
reconcile layer (portfolio_tracker), keyed on the client_order_id prefix. The `submitted_after=None`
external-close path (L219–279) is exactly the "untagged external close" the P&L seat said must
HALT+ALERT rather than silently attribute. (Also the FILL-UNVERIFIED→$0-P&L root — Build A adjacent.)

## STILL TO SCOPE (remaining Phase 0 reads before the exact diff + final gate)
- FULL READ `execution/exit_logic.py` — **the critical one**: the main reducing-order paths (partial
  exits, stops, full exits) that MUST route through `check_never_sell_floor()`. This is where the floor
  guard actually protects. (entry_logic only had the one #12c exit at L676.)
- FULL READ the fill-reconcile path in `execution/portfolio_tracker.py` — where fills land; add the
  client_order_id→tier attribution + untagged-external-close halt+alert.
- Other submit/close call sites: `strategy/run_cycle.py`, `execution/orphan_manager.py`,
  `execution/quarterly_hold_manager.py` (OrderDispatcher L256, tier=qhm), `main.py` (launch-init hook +
  close_all_positions/kill-switch floor-awareness).
- Spec `ownership_guard.py` pseudocode → concrete (from the masked-loss + execution seat designs).
- Launch-init: seed from get_all_positions(), existing GOOGL(1)/NVDA(1) → intraday, F6/QHM=0, run ONCE.
- Then: static + cold-2nd-agent + impact + FINAL Gro+GAI on the exact Phase 0 diff → API build.

## exit_logic.py — EXACT CHANGES (FULL READ DONE via Explore: 2269 lines) — THE CRITICAL GUARD FILE
3 tier-agnostic top-level fns, all INTRADAY, inject `tier` + thread through every close/stop call:
`check_partial_exits()` L185, `check_exits()` L1061, `_check_exits_extended_hours()` L2051.
**~23 REDUCING-ORDER sites → must route through `check_never_sell_floor(symbol,"intraday",qty,side)`
(qty-bound to intraday's OWN ledger qty AND the floor; reject if it would breach):**
- Full closes (`close_position`): L343 (trail-stop), L1278 (overnight ATR), L1439 (thesis-inval),
  L1547 (hard-stop), L1621 (target), L1878 (reversal).
- Partial closes (`partial_close_position`): L297 (trail phase adv), L715 (tranche) → use new
  `qty_bounded_partial_close(symbol,"intraday",qty)`.
- EH limit closes (`submit_limit_order` reducing): L2190, L2226, L2257.
- Stop re-protect (NOT reducing but need tier tag + correct qty): `submit_gtc_stop_order` L492, L876;
  `submit_day_stop_order` L531, L913, L1487. + `cancel_order` L293/339/377/675/1481 (stop-replace flow).
**QTY-VALUATION BUG ZONE (Phase 0 fixes this):** exits size off tracker `qty_orig` (L286, 637, 638);
Alpaca qty only clamps DOWNWARD (L460, 846); NO per-tier validation → a shared-lot exit sized off
tracker state could exceed intraday's own shares. Guard makes the tier's ledger qty the primary bound.
**P&L path (Phase 1 per-tier attribution):** 23 `record_exit` + 1 `record_partial_exit`; 11
`fetch_actual_fill_price`. Map captured in the Explore output (task a745b80a).

## CALL-SITE CENSUS (broker submit/close/partial refs — each needs tier param; reducing orders also guard-routed)
- `execution/exit_logic.py` — **2268 lines, 30 refs** (the critical file — main partial/stop/full exit
  paths = the reducing orders the floor guard protects). Explore full-read next, with clean context.
- `execution/orphan_manager.py` — 5 refs (external-close/stop-cancel paths; Build B adjacent).
- `strategy/run_cycle.py` — 4 refs.
- `main.py` — 6 refs (incl. kill-switch/close_all + shutdown safe_close_all — floor-awareness).
- `execution/quarterly_hold_manager.py` — via OrderDispatcher (submit_limit/submit_gtc_stop/close), tier=qhm.
Total Phase 0 order-path surface ≈ 45 call sites across 6+ files + 2 new modules (ownership_guard, ledger)
+ launch-init. This is a multi-step build — scope the rest with fresh context, then diff → gate → API.

## SCOPING PROGRESS: broker.py ✅ · entry_logic.py ✅ · fill_helpers.py ✅
## NEXT (fresh context): exit_logic.py (2268L Explore) → portfolio_tracker fill-reconcile →
## orphan/run_cycle/main call sites → spec ownership_guard.py → launch-init → Phase-0 diff → FINAL gate → API build.

## ENFORCEMENT NOTE (Rafael-locked)
Ring-fenced names are LONG-ONLY for share tiers (shorts→options program, separate). So the guard's
short-case on a floor>0 symbol = REJECT (no net-short accounting needed on ring-fenced names).
