# Forever-6 — Integration Map (from full-read gate on quarterly_hold_manager.py, 1955L)

Item (c) of the "still owed before API" list. Full read complete 2026-07-09 (4 chunks, every line).
This is the pre-scoped-package skeleton for the API build. NOT code.

## HOW FOREVER-6 PLUGS INTO THE EXISTING SYSTEM

### 1. It is a NEW module, not an extension of the QHM state machine
`execution/forever_hold_manager.py` (new). QHM's `HoldState`/tranche engine is calendar-pick-
specific (Day-1 gate, Day-3 reconfirm, 3 fixed tranches, 13-week max-hold exit, weekly-ATR stops)
— none of that fits Forever-6 (crash-triggered convex ladder, per-rung latch, fixed reserve, NO
stops, +1000/+2000% trims, 13 scenarios). Reuse QHM's *shape* (dataclass position, atomic
tmp→replace+fsync state file, OrderDispatcher, `_now_et`/`_alert`/`_get_live_price` helper style)
but its own state file `data/state/forever_holds.json` and its own config.

### 2. The never-sell registry is THE cross-strategy hub (this is what ring-fences F6)
`quarterly_hold_manager.get_quarterly_hold_symbols()` (L132) is read cross-process by:
- `entry_logic.py` before every scan → BLOCKS intraday entry into those symbols
- Kelly sizing → same-symbol cross-trade guard
- movers/orphan guards → never touch a registered symbol
- `_get_quarterly_notional_excl` (L1886) → subtracts held notional from intraday sizing equity

**Forever-6 symbols MUST union into this registry.** Because registration blocks intraday ENTRY,
it is exactly the mechanism that prevents a shared Alpaca lot from forming (no other tier can open
NVDA/TSLA/etc. once they're registered). Implementation options for the union:
- (A) add a sibling `get_forever_hold_symbols()` in the new module + have `get_quarterly_hold_symbols`
  (or a new `get_never_sell_symbols()`) return the UNION; update the ~4 call sites to the union fn; OR
- (B) have the F6 manager write its symbols INTO the same registry set + a shared state-file read.
Recommend (A): a new `execution/never_sell_registry.py` that unions QHM + F6 state files, with the
existing `get_quarterly_hold_symbols()` kept as a back-compat alias. Cleanest, testable, one source.

### 3. Stray-sell protection — F6 needs its own, STRICTER guard
QHM's HOLE-2 guard (`cancel_stray_sell_orders`, L487) keeps each hold's OWN protective stop and
cancels other sells. F6 has **NO stops** → any resting sell on an F6 name that is NOT a deliberate
trim order = unauthorized → cancel. Mirror the guard in the F6 manager (same fail-OPEN-on-unreadable-
API + LOUD-alert discipline as QHM L544). Run it at startup + once per cycle.

### 4. Wiring into the live loop (mirror QHM's 4 integration points)
- `main.py` startup: `fhm.reconcile_on_startup()` after `qhm.reconcile_on_startup()`
- `strategy/run_cycle.py` per cycle: `fhm.evaluate_crash_triggers(halt_eval, spy/qqq, mri, ...)` —
  driven by Build F's `halt_eval` venue-state event (already live) + per-name depth reads
- `main.py` shutdown: `fhm.safe_stop()` (persist only; F6 EXEMPT from circuit-breaker cancel)
- `entry_logic.py`: already reads the registry → picks up F6 names for free via the union (item 2)

### 5. Sizing / reserve / latch — new, but modeled on `_submit_tranche` (L1477)
QHM sizing: `target_notional = available_equity × target_equity_pct × tranche_frac`; `qty =
max(int(raw),1)`; DAY limit +0.1%. F6 replaces this with the LOCKED engine: fixed-reserve snapshot
at session open, geometric-decay deploy_frac × convex depth_mult (1.0/1.6/2.5×), truncated by
`min(remaining_reserve, CAP_headroom, funds)`; MARKETABLE limit `last×1.01–1.02` (not +0.1% DAY);
per-(symbol,date,RUNG) monotonic latch + ≤3/day ceiling; latch/ceiling decrement ONLY on confirmed
fill. Funding = settled cash ≥ 70% of tranche, margin loan ≤30% of F6 MV (atomic pre-trade check on
borrowed portion, NOT buying_power). All scenario/guard rules from `forever6_scenario_board_2026-07-09.md`.

### 6. Exit — trims only (NO stops, NO max-hold, NO external-close-to-CLOSED for the tier)
QHM's `_initiate_exit`/`_compute_and_submit_stop`/`_detect_external_close` do NOT apply. F6 exit =
trim 25% at +1000% (10x), another 25% at +2000% (20x); shrinking house-money core; never otherwise
sold. No GTC stop is ever placed on an F6 lot.

## ⚠️ KEY FINDING FOR RAFAEL — the pre-existing shared-lot / ring-fence reconciliation
Registration cleanly prevents FUTURE shared lots (other tiers can't enter a registered F6 name).
But an F6 name may ALREADY be held by another tier the day F6 goes live — e.g. **NVDA is in the
live book right now (1 sh)**, and NVDA is an F6 name, while the current QHM picks are LLY/GE/GEV
(so today's NVDA is an intraday/other lot, not QHM's). Alpaca holds ONE lot per symbol with no
ownership tag (the exact root that retired Movers). So on day one, F6 either:
- inherits/absorbs the existing NVDA lot as F6's opening position (simplest; but that lot has an
  intraday cost basis + possibly an intraday GTC stop that must be cancelled so F6 can't be stopped out), OR
- starts F6's NVDA count at 0 and treats the pre-existing 1 sh as a separate non-F6 lot (requires
  qty-bounded accounting to keep them distinct on one Alpaca lot — the unsolved ownership-tag problem).
**This is the one integration decision that needs Rafael + a board pass before the API build.**
Options + board rec below (to be run).

## STILL OWED (unchanged): (d) final board+Gro+GAI on the fully-mapped combined proposal → API build.
Before (d): resolve the shared-lot reconciliation decision above; then assemble the line-scoped
package (new file + the ~4 registry/loop wiring edits + config additions).

## FILE PLAN (pre-scoped for the API build)
- NEW `execution/forever_hold_manager.py` — the tier (buy engine, reserve/latch, trims, own guard, state).
- NEW `execution/never_sell_registry.py` (option A) — unions QHM + F6 symbols; back-comp alias.
- NEW `data/state/forever_holds.json` (+ `forever_holds_config.json` for the curated universe).
- EDIT `strategy/run_cycle.py` — call `fhm.evaluate_crash_triggers(...)` off the existing halt_eval block.
- EDIT `main.py` — startup reconcile + shutdown safe_stop for fhm.
- EDIT `config.py` — FOREVER_6 universe, CAP%, deploy_frac band, depth ladder, funding constants.
- EDIT the ~4 registry call sites (or just entry_logic import) to the union fn.
- Data-quality gate + FMP earnings-origin classifier (FORK B routing) — confirm reuse of existing modules.
