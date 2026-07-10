# Phase 0 — `execution/ownership_guard.py` — CANONICAL IMPLEMENTABLE SPEC (2026-07-09)

Consolidates the board designs (masked-loss + execution/ownership + P&L seats + GAI) into ONE spec,
with Rafael's locked decisions folded in. This is the central new module of Phase 0. The API build
implements THIS (after its own full-read gate). Design source: `logs/per_tier_ownership_design_2026-07-09.md`.

## RAFAEL DECISIONS FOLDED IN
- Ring-fenced names are LONG-ONLY for share tiers → the guard REJECTS any short/net-reducing-below-own-
  long order on a floor>0 symbol (no net-short accounting). Shorts→options (separate program).
- Real Alpaca GTC stops, auto-re-issued on every tier-qty change (§stop-sync).
- Phase 0 tiers: intraday + qhm (forever6 exists in the schema but has 0 qty until Phase 2).

## LEDGER — `data/state/ownership_ledger.json` (atomic tmp→replace + fsync, RC-5)
```json
{ "version": 1, "last_reconciled_utc": "...",
  "positions": { "NVDA": {
    "alpaca_net_qty": 4.0,
    "tiers": { "intraday": {"qty":3.0,"avg_cost":128.40,"last_fill_id":"..."},
               "qhm":{"qty":0,"avg_cost":0,"last_fill_id":null},
               "forever6":{"qty":1.0,"avg_cost":112.10,"last_fill_id":"..."} },
    "drift": 0.0 } } }
```
- `drift = alpaca_net_qty - sum(tier qtys)`. FLOOR IS LEDGER-DERIVED ONLY, never from alpaca_net
  (catastrophic-mode #2). Alpaca net is used ONLY to compute/detect drift.
- Load: atomic; on schema-fail/torn-read → `LedgerReadError` → guard fails closed.

## client_order_id TAGGING — `{TIER2}-{symbol}-{side}-{epoch_ms}-{uuid8}` (IN/QH/F6)
Generated ONCE per order, reused on retries (preserves broker.py idempotency). Fill→tier attribution
reads the prefix ONLY — never inferred from qty deltas. Untagged fill (external/manual/pre-ledger) →
NOT auto-attributed → `unattributed_fills.jsonl` + drift++ + WARNING (P&L seat: never silently FIFO).

## THE CHOKEPOINT — `check_never_sell_floor(symbol, tier, qty, side) -> GuardResult`
The ONLY path allowed to submit a net-long-REDUCING order (broker.py sell/short/close wrappers call
it as a hard precondition; every reducing call site in exit_logic/run_cycle/orphan/QHM routes through it).
```
GuardResult ∈ {APPROVE(qty), QTY_BOUND(qty'), REJECT(reason), RETRY_FROM_TOP}

check_never_sell_floor(symbol, tier, qty, side):   # qty>0 shares; side ∈ {sell, short}
  # STEP 0 — FAIL-CLOSED PRECONDITIONS
  if not ledger.is_fresh(symbol): REJECT("stale ledger")            # incl. STALE-until-first-reconcile after restart
  try: alpaca_net = broker.get_open_position(symbol).qty (0 if None)   # live read
  except: REJECT("alpaca read failed")
  try: f6,qh,intr = ledger.qty(symbol, each tier)
  except: REJECT("ledger unreadable")
  # STEP 1 — DRIFT = AMBIGUITY = FAIL CLOSED
  if (f6+qh+intr) != alpaca_net: FREEZE_SYMBOL(symbol); ALERT_CRITICAL; REJECT("drift")
  # STEP 2 — FLOOR (ledger-derived ONLY)
  floor = f6 + qh
  own = ledger.qty(symbol, tier)
  # STEP 3 — TIER RULES
  if side == "short":
     if floor > 0: REJECT("ring-fenced name is long-only (shorts→options)")   # Rafael decision
     else: pass-through to ordinary short (no protected exposure)              # floor==0 only
  if tier == "intraday" or tier == "qhm":
     qty = min(qty, own)                          # never sell more than the tier owns
     if alpaca_net - qty < floor:
         allowed = alpaca_net - floor
         if allowed <= 0: REJECT("floor binding, 0 sellable")
         qty = min(qty, allowed); LOG_P1("floor-clipped", requested, qty)   # clip = P1 alert, not silent
     # NOTE: a QHM self-exit legitimately reduces the floor (floor is DEFINED from qh); the
     # min(qty,own) bound already prevents QHM selling anyone else's shares, so this is correct.
  elif tier == "forever6":
     if not is_authorized_f6_trim(symbol, qty): REJECT("F6 only reduces via +1000/+2000 trim")
     qty = min(qty, own)                          # trim ≤ F6's own; measured from F6 OWN basis
  else: REJECT("unknown tier")
  # STEP 4 — RACE CLOSE: re-read net right before submit
  if broker net changed since STEP 0: return RETRY_FROM_TOP
  if qty <= 0: REJECT("resolved to 0")
  return APPROVE(qty)   # caller submits tagged; ledger decrements ONLY on confirmed fill
```
Per-symbol MUTEX (cross-process) wraps STEP0→submit so two concurrent reducing orders can't both pass.

## STOP SYNC (catastrophic-mode #1 — stale resting stop)
Invariant: a tier's resting stop qty == that tier's current ledger qty, ALWAYS. On ANY event that
changes a tier's qty (fill, partial exit, reconcile correction), SYNCHRONOUSLY cancel+replace that
tier's stop for that symbol BEFORE the qty change completes; cancel/replace fail → FREEZE + ALERT.
Re-verify the invariant every reconcile cycle. Use the robust GTC-replace retry (3s+poll60s+CRITICAL).

## DRIFT RECONCILE (every restart AND every cycle)
Any nonzero drift → LEDGER_DRIFT(symbol): sells BLOCKED (fully, not proportionally), buys allowed,
CRITICAL alert, human resolves. is_fresh() defaults STALE until first successful post-restart reconcile.

## LAUNCH INIT (run ONCE; refuse if ledger has any nonzero F6/QHM)
Seed from get_all_positions(): all existing shares → intraday, F6/QHM=0 (incl. GOOGL/NVDA),
avg_cost=Alpaca avg_entry, last_fill_id="SEED-<date>", drift=0, NO orders. Log `ledger_seed` per symbol.

## broker.py ENFORCEMENT (from broker scoping)
- Mandatory `tier` param (no default) on submit_market/limit/gtc_stop/day_stop + partial_close.
- NEW `qty_bounded_partial_close(symbol,tier,qty)` = guard-first → tagged submit → ledger-on-fill.
- `close_position()` ASSERTION: hard-disallow when >1 tier nonzero for the symbol (the Movers-root fix).
- `close_all_positions()` (kill-switch/safe_close_all): must skip/guard ring-fenced (floor>0) symbols.

## REMAINING TO SCOPE (fresh context) before the Phase-0 diff:
portfolio_tracker fill-reconcile (add client_order_id→tier attribution + untagged→halt); the ~15
call sites in orphan_manager(5)/run_cycle(4)/main(6)/QHM-OrderDispatcher; then static + cold-2nd +
impact + FINAL Gro+GAI on the exact diff → API build (Phase 0).
