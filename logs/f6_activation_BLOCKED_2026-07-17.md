# FOREVER-6 ACTIVATION — BLOCKED (queued for Rafael) — 2026-07-17

## What Rafael directed
"Turn Forever6 on. Open at least one share in all positions." → then chose **Option B**
(partial seed, preserve dry powder) when told all-6 exceeds cash.

## Verdict: ARMING is BLOCKED. Do NOT flip FOREVER6_ENABLED=True yet.
The cold **Execution-risk/ruin board seat REJECTED** arming (Gro, GAI, and the Reliability
seat all APPROVED — they trusted the premise that the never-sell floor is live; it is NOT).
This is the mandatory-cold-board-on-risk-path rule doing exactly its job. Every claim below
is **verified at 100% against source**, not trusted from the agent.

### The force-liquidation path arming would open (does not exist while dark)
1. **The never-sell floor is DORMANT.** `config.py:556 OWNERSHIP_GUARD_ENFORCE = False`.
   `broker.py:759-760 close_position` → `if not OWNERSHIP_GUARD_ENFORCE: return _raw_close_position(symbol)`
   — a raw full close with ZERO floor. `partial_close_position` (broker.py:578) gates its
   `check_never_sell_floor` call the same way. `submit_gtc_stop_order` has NO floor check at all.
2. **The F6 buy never syncs the ownership ledger.** `execute_starter` → `submit_market_order`
   + `_record_event` (writes `forever6_holds.json`, NOT `ownership_ledger.json`). No
   `save_ledger`/`sync_ledger` anywhere in the buy path (broker/forever_hold_manager/main/
   run_cycle). The ledger is only written by `run_ledger_sync.py` — an RTH-only `*/20` cron.
3. **Therefore:** seed runs after RTH close → overnight the ledger still has `forever6.qty=0`
   for the fresh anchor → `_get_forever6_syms()` (keys on ledger forever6 qty>0) can't see it
   → an overnight watchdog restart (`os.execv`; only RTH restarts are barred) →
   `reconcile_positions` adopts the anchor as an **intraday orphan** with a ±5% emergency stop
   → `check_exits` calls `close_position(sym,"intraday")` → floor OFF → **raw close → the
   never-sell anchor is SOLD** (worst-timed: a dip, at a ±5% stop). Ruin asymmetry: trading a
   bounded known downside (the ~$840 spend) for an unbounded-timing downside (conviction book
   force-sold), with the named mitigation inoperative.

## The 3 board-required prerequisites BEFORE any arming (all P0)
1. **Arm the floor:** flip `OWNERSHIP_GUARD_ENFORCE=True` — but only after clearing its own
   blocker #2 (populate `ownership_ledger.json` F6/QHM floors + confirm `protected_symbols.json`
   present on OCI), then **live-verify a rejected sell on a protected symbol.**
2. **Close the sync gap:** the F6 buy must persist `forever6` qty to `ownership_ledger.json`
   (and refresh the protected cache) **synchronously at placement**, or run `run_ledger_sync`
   immediately post-seed — so the anchor is protected before the first possible overnight restart.
3. **GTC/DAY stop path floor check:** add a floor check (or explicit F6-symbol skip) to
   `submit_gtc_stop_order`/day-stop submission so an orphan-adopted anchor can't get a live sell-stop.

Each is its own gated change (full sequence + board + Gro/GAI + preship). This is a
multi-session activation project, NOT a switch-flip. Feature Design Protocol gate applies.

## What DID ship today (DARK / inert / BGG-aligned) — commit 3270a76
- `orphan_manager.py`: `_get_forever6_syms()` + exclude held F6 symbols from the startup orphan
  set (fail-CLOSED to the protected cache per the Reliability seat). Inert today (ledger has 0
  forever6 qty). This is prerequisite-piece #2's *consumer* — necessary but NOT sufficient alone
  (it can't see a fresh anchor until the ledger syncs; that's why prereq #2 exists).
- `forever_hold_manager.py`: durable per-DAY idempotency guard in `maybe_start_accumulation`.
  Inert today (only the FOREVER6_ENABLED AH hook calls it).
- Earlier same session, unrelated: `d883f59` fill-signal None-on-failure refactor (also awaiting
  the next non-RTH OCI restart).

## The seed plan (READY — execute only AFTER all 3 prerequisites clear + a clean restart)
Option B, live prices 2026-07-17: **CRWD $207.54 + AMZN $248.68 + TSLA $384.94 = $841.16**
(×1.01 slippage ≈ $849.57). Settled cash $1,328.78 → after seed ~**$487 dry powder** (> $200
CASH_FLOOR). META $649.25 deferred (priciest). The manual seed deliberately overrides the
20%-per-event auto-cap (~$266) — an authorized Rafael override; the $200 cash floor still binds.
Mechanism: `forever_hold_manager.execute_starter(plan, budget≈$850, settled_cash)` run on OCI
after the restart. Do NOT run until prereqs 1-3 are live and a protected-sell rejection is verified.

## Pending OCI restart (deferred to next non-RTH window)
Two dark/inert commits await the restart: `d883f59` (fill refactor) + `3270a76` (F6 dark subset).
Neither is urgent; both are inert until then. Restart at/after the RTH close.
