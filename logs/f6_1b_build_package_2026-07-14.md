# Forever-6 STARTER — 1b SHIPPED (3711f03), 1c BUILD PACKAGE (ready to execute)

> **STATUS 2026-07-14:** increment **1b `execute_starter()` SHIPPED DARK** (commit 3711f03, Rafael "ship"; gate: ruin seat + Gro + GAI APPROVE, preship marker 49f8ebd1). The §1b spec below is DONE. Remaining = **§1c** (run_cycle wiring) + the FOREVER6_ENABLED flip (Rafael go).

**Design authority:** `logs/f6_starter_bgg_2026-07-13.md` — BGG-aligned + Rafael-ADOPTED
(cash-only, no margin, 1-3 names, −3% dynamic close trigger, catalyst-screened, segregated budget).
**Current state:** 1a shipped DARK (`execution/forever_hold_manager.py`, 162 lines, log-only, `8be4d4b`);
completely unwired; `config.FOREVER6_ENABLED=False`. Catalyst screen it depends on is now LIVE.

## Why this is a QUEUE package, not an autonomous ship
1b/1c place **real orders** on a live account → risk-path build. Per the design doc it requires a
**mandatory cold masked-loss seat + Gro/GAI on the diff**, and the enable-flip (`FOREVER6_ENABLED=True`)
is an execution-behavior change that needs **Rafael's explicit go** — exactly the pattern the catalyst
gate followed (built dark → Rafael go → flip `2e2561d`). An autonomous midnight session should not flip
live-order execution. Build + gate happens in an interactive session with Rafael present.

## Increment 1b — `execute_starter()` in `forever_hold_manager.py` (self-contained, gate first)
Add a method that turns the 1a PLAN into CASH-ONLY orders. Full mandatory sequence on this file
(already fully read this session — 162 lines). Spec:

```
def execute_starter(self, plan, budget, settled_cash=None) -> dict:
    # Precondition: caller (run_cycle) already checked FOREVER6_ENABLED and RTH/after-close timing.
    # 1. Re-fetch SETTLED CASH at execution (never buying_power — margin is FORBIDDEN for F6).
    # 2. Hard segregation guard: spendable = min(budget, cash - FOREVER6_STARTER_CASH_FLOOR); if <=0 abort.
    # 3. For each planned name (already breadth/affordability-ranked, 1 share each, cash-only):
    #      - if price > (spendable - spent): skip (budget/floor exhausted)
    #      - submit MARKET BUY qty=1 (verify broker API method name via full read of broker.py)
    #      - FAIL-CLOSED: on any exception OR no confirmed order -> log ERROR + BREAK (never continue
    #        placing after an error; never mask a failed fill as success)
    #      - only count a leg as placed on a CONFIRMED order id
    # 4. If any placed: _record_event(placed) -> atomic append to data/state/forever6_holds.json
    #      (RC-5 tmp->fsync->replace) with date for the per-month cap; NEVER writes a sell.
    # 5. Return {"placed":[...], "skipped":[...], "spent": float}.
```
**Masked-loss seat framing:** this is BUY-only accumulation (no exit, no P&L masking), so the seat's
job is to confirm (a) it can NEVER use margin (cash not buying_power), (b) it can NEVER spend below
the cash floor that the deep crash ladder reserves (ammo-cannibalization ruin finding), (c) it
fails-closed on broker errors (no phantom "placed"), (d) the per-month event cap is durably recorded
BEFORE the next trigger can fire. Gro + GAI on the exact diff.

## Increment 1c — run_cycle after-close hook (gated file, needs 2023-line Explore full-read first)
The trigger is a **SPY close move**, so the hook is a once-per-day after-close evaluation, mirroring
the QHM hook pattern. Candidate site: the after-hours block in `strategy/run_cycle.py` (~L490-541,
where `write_eod_summary` runs) — evaluate `spy_day_close_pct` vs `starter_trigger_pct(vix)` once,
behind `if config.FOREVER6_ENABLED:`. Needs: full Explore read of run_cycle.py (>1000 lines →
mandatory), confirm the after-close block runs exactly once, wire `f6 = ForeverHoldManager(broker)` +
`plan = f6.maybe_start_accumulation(...)` → `if plan["plan"]: f6.execute_starter(...)`. Full board +
Gro/GAI on the run_cycle diff. Keep `FOREVER6_ENABLED=False` through 1b+1c; flip is a separate
1-line diff with Rafael's explicit go.

## Order of operations for the interactive session
1. Full-read `execution/broker.py` → confirm the cash-only market-buy method + account/cash accessor.
2. 1b: build `execute_starter` + `_record_event` → statics → board (incl. masked-loss seat) → Gro/GAI
   → ship DARK (unwired, flag off = double-dark, zero live risk).
3. 1c: Explore full-read run_cycle.py → wire the after-close hook behind the flag → board + Gro/GAI
   → ship DARK.
4. Validate on a real/simulated −3% close (log-only path still works) → then Rafael go → flip
   `FOREVER6_ENABLED=True` (1-line, Gro/GAI on the diff) → LIVE.
