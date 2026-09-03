# DAY-TIER LIVE BUILD — Board-Aligned Design Record (2026-09-02)

**Status:** DESIGN ALIGNED — Board 3 cold seats (Data-integrity / Exec-risk-masked-loss /
Reliability) + Gro + GAI, all five **APPROVE-WITH-CHANGES**, converged. Plus Rafael's binding
amendments (price-path logging; stop-retry-before-flatten; scoped flatten). Build runs the full
mandatory patch sequence + preship gate per increment.
**Parent:** `logs/design_records/day_tier_v2_design_2026-08-29.md` (architecture §1–§7b). This record
is the LIVE-EXECUTION build spec that sits on top of that aligned architecture.
**Owner:** Rafael (Chairman/CEO). North-Star lens: paper $2.5k→$25k growth engine; bounded/survivable
risk OK; safety envelope UNCHANGED (7% kill, never-mask-a-loss, paper=True, data tiers).

---

## 1. SCOPE (Rafael directive)
Flip the day-tier LIVE (paper) for the next session(s). **Track A (GEX-core) ONLY day-1; Track B
(dynamic movers) OFF day-1** — unanimous across all five voices (adding a second strategy day-1
muddies P&L attribution with no upside; Track B's own go-live is gated by §7b.3 on the red-day
audit being built first — which this build IS, so shipping A-first *follows* that gate). Keystone:
**bulletproof durable logging** — every decision/entry/exit/fill/price/size/realized-P&L on disk
immediately + completely (append+flush+fsync), survives restart, no weekend gaps.

## 2. THE FIVE-VOICE VERDICT — GO to build + flip, with named blockers (all must land pre-flip)

**B1 — Flatten is `partial_close_position` ONLY; hard assert-guard against any full-close for
tier="daytrade" (masked-loss C1).** `close_position(symbol)` with `OWNERSHIP_GUARD_ENFORCE=False`
(current default) delegates to a raw Alpaca `DELETE /positions/{symbol}` = closes the ENTIRE
combined net across ALL tiers (broker.py:1450-1451) → would liquidate a co-held QHM/intraday lot.
The day-tier module must NEVER call close_position/close_all_positions; only
`partial_close_position(own_recorded_qty, tier="daytrade")` (broker.py:1223, submits a market order
for EXACTLY qty — verified at source). A runtime assert refuses any full-close under "daytrade".

**B2 — Confirm-stop-then-verify, RETRY ≥2× before flatten, scoped flatten (masked-loss C3 + Gro#1
+ GAI + Rafael amendment).** Entry (`submit_limit_order`, marketable-limit) fills instantly and
returns immediately WITHOUT confirming a fill (broker.py:524-529); the stop is a SEPARATE second
leg (`submit_day_stop_order`, broker.py:926) that today tries ONCE and returns None on failure (no
retry loop — verified at source). Mechanic: place entry → confirm fill (`broker.get_order`) →
place DAY stop → **verify the stop is live (ACK) via get_order** → **if unconfirmed, RETRY placement
≥ DAYTRADE_STOP_RETRIES more times with backoff (Rafael: a transient Alpaca glitch deserves ≥2
retries, not an immediate panic-flatten)** → only if STILL unconfirmed, force-flatten via the SAME
scoped `partial_close_position(own_qty, "daytrade")`. **Rafael amendment — the flatten is
structurally incapable of touching intraday/QHM/F6 shares** (it can only ever sell the day-tier's
own recorded qty; see B1 + the scoped free-shares check). Kill/risk gate evaluated on POST-fill
equity, AFTER fill handling (Gro#4, GAI#4) — never before.

**B3 — Per-symbol per-bar in-flight idempotency, written BEFORE submit (reliability C2 + masked-
loss double-fill).** `_make_idem_id` mints a NEW coid each loop iteration → Alpaca won't dedup →
re-firing the same ENTER before the fill is recorded = DOUBLE position. Before any entry submit,
atomically write `{symbol, bar_id (5m-bar-open-epoch), coid, state}` to `day_tier_state.json` via
tmp+os.replace+fsync; refuse an entry when a record exists for (symbol, current bar_id). Reconcile
the coid against Alpaca on next-run startup. Cancel any unfilled "daytrade" entry order at end-of-
run so no resting entry survives to fill mid-next-bar.

**B4 — Single-writer + tier exclusion (reliability C4 — HARD BLOCKER, the highest-risk item).**
The 2-min runner is a SEPARATE process; it must NOT write the shared `tracker.open_trades`. It gets
its OWN `day_tier_state.json`. **run_cycle must EXCLUDE the "daytrade" tier from check_exits /
partial-exit management**, exactly as QHM/forever6 are already excluded (run_cycle.py:1760). Without
this, run_cycle's check_exits sees the day-tier symbol in the tracker, re-reads a now-absent Alpaca
position, and records a **$0.00 phantom exit** = a masked loss (the RIVN-class bug, config.py:266-
268). The tier-safe order infra isolates ORDERS; it does NOT isolate the shared tracker JSON — B4
closes that gap. Must land WITH the order module, not after.

**B5 — Day-tier owns its force-liquidate at T-DAYTRADE_FORCE_FLAT_MINUTES(20) before the REAL close,
BEFORE the T-15 pre-close sweep window (all five voices).** The #216 pre-close sweep only STOP-
COVERS (naked backstop), does NOT flatten; the AH GTC block skips the day-tier (non-overnight,
run_cycle.py:603); the swing bot never EOD-flattens → the day-tier would ride overnight NAKED.
Fix: the day-tier force-liquidates its OWN lots (`partial_close_position` + marketable/market
close) at T-20, half-day-aware (`get_clock().next_close`), then verifies flat. Running at T-20
(before the T-15 sweep) avoids the mis-tagged-stop deadlock (masked-loss C2: the sweep places an
intraday-tagged DAY stop with no `tier=` arg (stop_protection.py:525→broker.py:931 default
"intraday"→coid "IN"); a day-tier tier-scoped cancel can't cancel an "IN" stop, so a naked day-tier
position covered by the sweep could not be flattened by the day-tier). Enforced by the config check
`DAYTRADE_FORCE_FLAT_MINUTES > PRECLOSE_SWEEP_MINUTES`. GAI: escalate to a MARKET order if not flat
by the hard deadline (prioritize flat-by-close over no-market-order).

**B6 — Cumulative day-tier gross cap at wire-time, reading the live book (masked-loss C4).** The
sizing kernel (`day_tier_sizing.py`) is per-call min()-only with NO cumulative cap → 3 concurrent
Track-A ENTERs each ≤9.75% = 29% of equity vs the 15% alloc. Enforce the account-level gross cap
across concurrent day-tier positions at entry time (≤ DAYTRADE_ALLOC_PCT, Track-A 65% sub-cap) +
the ~$650 maintenance cushion (`DAYTRADE_MAINT_CUSHION_USD`), reading live positions.

**B7 — Durable logging keystone (data-integrity seat + Gro#3 + GAI#3 + masked-loss).** Dedicated
`logs/day_tier_events.jsonl`, append+flush+fsync per write (batch the 30-min samples into ONE
fsync), full schema keyed by trade_id=coid, decision_id join, order_id persisted on both fills.
**Harden `trade_logger.log_event` with flush+fsync** (it appends without fsync today — verified at
source). Write ordering: entry → stop → THEN fsync-log (log-after-stop = never trade-blocking).
Dual-write: canonical minimal lifecycle event to `trade_events.jsonl` (P&L SoT, §4) FIRST,
enrichment/trajectory to `day_tier_events.jsonl` second — both coid-keyed so they join.

**B8 — PRICE-PATH (Rafael's amended keystone).** Per day-trade: (1) `entry_fill` snapshot =
{fill_price, market_price_at_fill}; (2) `price_sample` per 30-min tick for EVERY open trade =
{seq, market_price, unrealized_pnl}; (3) `exit_fill` snapshot = {fill_price, market_price_at_exit,
realized_pnl}. **STATELESS restart-safe sampler** (data-integrity #3): derive the open set every
tick by REPLAYING the log (entry_fill without exit_fill) + reconcile vs live broker positions —
never from in-memory state — so a mid-trade restart never holes the price path. `seq` makes a
missing sample detectable. Torn trailing line tolerated on read (skip+warn, never abort).

**B9 — Track B OFF day-1; architecture = SEPARATE 2-min RTH cron process** (NOT a guarded branch —
the 5-min loop is single-threaded ~11-min real spacing, can't do 2-3 min and would starve
check_exits; NOT a systemd daemon — the repo has no systemd pattern, cron self-heals on the next
tick). flock(LOCK_EX|LOCK_NB) self-overlap guard (reliability C1). Heartbeat file freshness check,
NOT watchdog.py execv (reliability C6). Cron phase-offset off the 5-min boundary + per-run API-call
cap `DAYTRADE_MAX_API_CALLS_PER_RUN` (reliability C5 + ANTI-SILO §7b.2).

## 3. RAFAEL AMENDMENTS (2026-09-02, binding — fold into B1/B2)
1. **Stop retry ≥2× before flatten** (`DAYTRADE_STOP_RETRIES`, tunable/dynamic) — a transient
   Alpaca glitch is not a reason to panic-flatten.
2. **A flatten must be provably unable to sell another tier's shares** — the flatten path is the
   scoped `partial_close_position(own_qty, "daytrade")`; even a drifted own-count is bounded by the
   scoped free-shares check + `check_never_sell_floor` (ownership_guard.py:444-479, verified: bounds
   a sell to `min(qty, own)`, keeps net ≥ floor, REJECTs rather than breach, FREEZES on drift).
3. **Explicit anti-liquidation guarantee for co-held names (e.g. NVDA also held by QHM):** the
   day-tier never calls the whole-symbol close; only ever sells its own recorded qty; records that
   qty from the ACTUAL entry fill (`broker.get_order`) so it can't drift off a partial fill.

## 4. INCREMENT PLAN (each = full patch sequence + preship gate; commit inert)
1. **config constants (INERT, DARK)** — SHIPPED (`64b2996`): `DAYTRADE_ENABLED=False` +
   allocation/kill/cadence/force-flat/universe + fail-closed nesting check in `validate_config`.
2. **durable + price-path logger** — NEW `strategy/day_tier_logger.py` (this record covers it) +
   harden `trade_logger.log_event` fsync. Non-risk-path (logging only).
3. **order-placement module** — NEW `execution/day_trade_manager.py` (rebuild from v2 §5b + B1/B2/
   B3/B6 + Rafael amendments). RISK-PATH → full board + masked-loss seat + Gro/GAI.
4. **run_cycle daytrade-tier EXCLUSION** (B4) — RISK-PATH (touches run_cycle).
5. **2-min stateful runner** — graduate `run_day_tier_shadow.py` → order-placing runner (loop +
   flock + heartbeat + idempotency + force-flat + startup reconcile + API cap).
6. **FLIP** — `DAYTRADE_ENABLED=True` + `*/2` RTH cron (phase-offset) + OCI deploy. Go-live.

## 5. SCHEMA — logs/day_tier_events.jsonl (data-integrity seat)
Every record: `{ts (PT ISO), event, schema_v, trade_id, symbol}`. Events:
- `decision` — decision_id, decision{}+trigger{}+size{} verbatim (log WAITs too).
- `entry_fill` — order_id, decision_id, side, requested_limit, fill_price, fill_qty,
  market_price_at_fill, equity_at_entry, budget, notional.
- `stop_placed` — stop_order_id, stop_price.
- `price_sample` — seq, market_price, unrealized_pnl (one per open trade per 30-min tick).
- `exit_fill` — order_id, exit_reason, fill_price, fill_qty, market_price_at_exit, realized_pnl.
Weekend parser: group by trade_id, order by ts/seq → `entry_fill → [price_sample…] → exit_fill` =
the full price path; decision_id joins the pre-trade stack; order_id reconstructs coid↔order_id.

## 5b. TRACKED FAST-FOLLOWS (LOW, non-blocking — noted, not gating)
- **day_tier_logger.py read_events / open_trades_from_log** — add a defensive `isinstance(ev, dict)`
  skip so a valid-JSON-but-non-object line (corruption/tampering only; this single-owner file only
  ever writes dicts) cannot raise into the live sampler. Cold-2nd LOW nit, pre-existing pattern.
- **day_tier_logger.py `_dir_fsynced`** — the one-time parent-dir fsync is not retried if the first
  attempt fails (best-effort; file-level fsync runs every write, so exposure is negligible).
- **Sizing-constant duplication** — `strategy/day_tier_sizing.py` carries its OWN `_DAYTIER_ALLOC_PCT`
  (0.15) / `_TRACK_A_SHARE` (0.65) / `_TRACK_B_SHARE` (0.35), duplicating the new `config.DAYTRADE_*`
  values (identical today). The order module uses the CONFIG constants for the account-level gross cap
  + cushion; a future tidy-first diff should unify the kernel onto config (single source of truth).

## 6. SAFETY ENVELOPE — UNCHANGED
7% account kill (equity-derived, runs AFTER fill handling), never-mask-a-loss, paper=True,
data-source tiers, tier kill 25% / Track-A 25% / Track-B 20% (nested < account kill). Velocity is
within the envelope, never widening it.
