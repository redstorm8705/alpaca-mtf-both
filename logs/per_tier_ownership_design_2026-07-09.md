# Per-Tier Shared-Lot Ownership + Never-Sell Floor — BOARD DESIGN (2026-07-09)

Triggered by Rafael's mandate (2026-07-09): intraday/swing tiers must STILL trade QHM/Forever-6
names on confluence; ring-fence protects a SHARE COUNT, not the symbol. Board: Gro + GAI +
execution/ownership (Katsuyama/Peterffy) + masked-loss (Thorp/Taleb) + P&L-integrity (McKinney/Derman).
Remarkable convergence across all 5. This is the "per-strategy ownership tag + qty-bounded close"
fix the roadmap flagged as THE real fix (its absence retired Movers) — now the FOUNDATION.

## CONVERGED DESIGN (all voices agree)

### 1. Per-tier ownership ledger — `data/state/ownership_ledger.json` (atomic RC-5)
Per symbol: `alpaca_net_qty`, per-tier `{qty, avg_cost, last_fill_id}`, and a `drift` field.
**Ledger is a CLAIM; Alpaca's net position is GROUND TRUTH.** The floor is derived ONLY from the
ledger, never from Alpaca's net (see catastrophic mode #2).

### 2. client_order_id tagging — the sole attribution mechanism
Format `{tier}-{symbol}-{side}-{epoch_ms}-{seq}` (e.g. `IN-NVDA-B-1751999283412-0007`).
Every broker submit wrapper gains a MANDATORY `tier` param (no default → every call site must update,
can't-forget-to-tag). Fill→tier attribution is SOLELY the client_order_id prefix — NEVER inferred
from qty deltas or "which tier looks short" (inference is the ambiguity that sells the wrong share).

### 3. Single floor-guard chokepoint — `execution/ownership_guard.py::check_never_sell_floor()`
The ONLY code permitted to submit a net-long-reducing order. Every SELL/SHORT from every tier routes
through it. FAIL-CLOSED on: no ledger entry, `drift != 0`, Alpaca read fail, ledger unreadable,
unknown tier/side. `protected_floor = forever6_qty + qhm_qty` (from LEDGER only). Re-reads Alpaca net
immediately before submit (race close). Ledger decrements ONLY on confirmed fill, never at submit.

### 4. The four sell/short cases
- (a) **Intraday exit:** qty bounded to intraday's OWN ledger qty AND `net - qty >= floor`.
- (b) **Intraday short on a floor>0 name:** bounded to `net - floor`. On a name F6/QHM hold, this
  means intraday can flatten its own long down to zero but can NEVER open a real net-short (Alpaca
  netting — see DECISION 1). Real short only where `floor == 0`.
- (c) **QHM self-exit / QHM stop:** sells its own qty; floor is DEFINED from qhm_qty, so QHM exiting
  itself is floor-consistent (authorized — 13-wk max hold / weekly-ATR stop is QHM's own mechanism).
- (d) **F6 trim:** the ONLY authorized reduction of F6's own count; ≤25% of F6 qty, gated on
  +1000%/+2000% measured from **F6's OWN cost basis** (never Alpaca blended — see catastrophic mode #3).

### 5. P&L attribution — per-tier FIFO
Per-tier FIFO lot queues keyed `(symbol, tier)`, extending `execution/fifo_pnl.py` one level deeper.
Tier tag captured at fill time from client_order_id. Alpaca blended `avg_entry_price` used ONLY as a
reconciliation sanity check. Untagged external-close fill → HALT auto-attribution + CRITICAL alert +
`unattributed_fills.jsonl` (never silently FIFO against the first tier — that's the Movers path).
Generalize `_get_quarterly_notional_excl` → `_get_other_tier_notional_excl(tier)`. One canonical
`get_combined_symbol_exposure(symbol)` feeds BOTH invariant #10 (beta-correlation) and #11
(overnight budget) so a shared lot counts as ONE symbol, never double-counted.

### 6. Stops on shared lots
Stop qty = the tier's OWN ledger qty, always (never sized off Alpaca net). F6 places NO stops.
SYNCHRONOUS cancel-replace on ANY tier qty change (before the qty change is "complete"); use the
robust GTC-replace retry (3s+poll-60s+CRITICAL) not the weak single-retry; cancel-fail → FREEZE +
alert. Re-verify "stop qty == tier qty" every reconcile cycle.

### 7. Drift reconciliation (every restart AND every cycle)
`drift = alpaca_net - sum(tier qtys)`. Any nonzero drift → `LEDGER_DRIFT` state for that symbol
ONLY: sells BLOCKED (not proportionally adjusted — fully rejected), buys allowed, CRITICAL alert,
human resolves. Uniform handling for all causes (stop fired between polls, manual trade, corporate
action, missed poll, double-count). On restart, `is_fresh()` defaults STALE until first successful
reconcile — no reducing order before it.

### 8. Launch ledger init (run ONCE)
Seed from `get_all_positions()`; ALL existing shares → `intraday`; F6/QHM = 0 for every symbol
(incl. GOOGL/NVDA — F6 starts at 0 by Rafael's instruction); `last_fill_id="SEED-<date>"`; drift=0
by construction; NO orders sent. Refuse to run if the ledger already has any nonzero F6/QHM qty
(re-seeding would erase ownership = self-inflicted catastrophic mode).

### 9. New broker.py primitives
- Mandatory `tier` param on every submit wrapper (breaking change; all call sites updated same patch).
- NEW `qty_bounded_partial_close(symbol, tier, qty)` — guard-first, tag, ledger-update-on-fill.
- Hard-DISALLOW `close_position()` (non-divisible whole-lot close — the Movers root) on any symbol
  with >1 tier holding nonzero qty — assertion IN broker.py, not caller discipline.
- Stop-replace-on-qty-change hook (generalize QHM's PENDING_STOP_REPLACE, parameterized by tier).
- Per-symbol order-submission MUTEX (cross-process) — prevents concurrent oversell race.

## TWO CATASTROPHIC FAILURE MODES (designed against)
1. **Stale resting stop** (masked-loss seat): Alpaca fires a resting stop autonomously; the guard
   isn't in the loop. If a tier's qty shrank after the stop was placed and it wasn't cancel-replaced,
   the stale too-large stop eats a protected share. → synchronous cancel-replace is part of the
   guard's contract, checked every cycle.
2. **Silent floor recalc on drift** (execution seat): if the floor is ever recomputed from Alpaca's
   (drifted) net, it silently shrinks and the guard sells the never-sell book exactly when something's
   already wrong. → floor is LEDGER-derived only; Alpaca net is drift-detection only; any patch that
   makes floor a function of alpaca_net is an auto-reject at review.
3. **Silent F6 trim false-negative** (P&L seat): F6 trim reading Alpaca's blended avg_entry never
   reaches +1000% when a low-basis F6 lot is diluted by a higher-basis intraday lot → trim never
   fires for months. → trim reads F6's own ledger basis; no trim fn may accept an Alpaca Position;
   audit-grep `avg_entry_price`; efficacy-not-presence test.

## ★ RAFAEL DECISIONS — LOCKED 2026-07-09
1. **Shorts on ring-fenced names → OPTIONS (not shares).** The share tiers (intraday/swing) are
   **LONG-ONLY on Forever-6/QHM names** — no short entries on them at all; bearish exposure on those
   names is expressed via PUTS in the separate Options/0DTE program (NOT this build). This SIMPLIFIES
   the guard: floor-case (b) becomes "reject any short on a floor>0 symbol" — there is no net-short
   accounting to maintain on ring-fenced names. Shorts remain fully available on non-ring-fenced names.
2. **Stops → real Alpaca GTC stops, auto-re-issued** (synchronous cancel-replace on every tier
   qty change + freeze-on-fail). Preserves overnight/bot-down coverage (Invariant #3).
3. **Build order → FOUNDATION FIRST, 3 phases.** Phase 0 (ownership layer, also fixes the Movers/
   cross-strategy bug) → Phase 1 (per-tier P&L + synced stops) → Phase 2 (Forever-6 tier). Each phase
   is its own API build + gate. Forever-6 lands LAST, on a proven foundation.

## (Original decision framing — superseded by the locked answers above)
### DECISION 1 — Shorts on ring-fenced names (REVERSES the earlier "allow shorts" answer)
Unanimous (both seats + Gro + GAI): a true net-short on an F6/QHM-held name is MATHEMATICALLY
IMPOSSIBLE under Alpaca's single-net-position model without selling the protected shares. "Allow
shorts, guard the floor" on those names can only mean "intraday flattens its own long to zero, never
negative." Genuine short capacity on those specific names would need a SECOND brokerage account
(true segregation) or options. RECOMMEND: accept intraday-flatten-to-zero on ring-fenced names; real
shorts only on non-ring-fenced names. (No new account; safe.)

### DECISION 2 — Stops on shared-lot names
RECOMMEND: keep Alpaca GTC stops (preserves overnight/bot-down coverage per Invariant #3) BUT with
synchronous cancel-replace on every tier qty change + freeze-on-fail. Alternative (local stop engine)
loses overnight coverage — rejected.

### META — PHASING (this is the biggest architectural change in the project)
This cannot be one API build. Recommended sequence:
- **Phase 0 — Ownership foundation:** ledger + client_order_id tagging + mandatory broker tier param
  + floor-guard chokepoint + drift reconcile + launch init + close_position hard-disallow. Build &
  validate with the EXISTING tiers (intraday + QHM) BEFORE Forever-6 exists. **This alone closes the
  Movers/cross-strategy bug — independently valuable.**
- **Phase 1 — P&L + stops:** per-tier FIFO in fifo_pnl.py + stop-replace-on-qty-change + per-tier sizing.
- **Phase 2 — Forever-6 tier:** the crash-buy engine + reserve/latch + 13-scenario map + trims, on the
  now-solid ownership layer (per forever6_scenario_board + forever6 locked spec).

## RESIDUAL RISK (accept + flag)
Alpaca-side forced liquidation (margin call) can sell the never-sell book outside any local guard.
F6's locked spec already bounds this (cash ≥70%, loan ≤30% MV survives GFC −57%). Detective only:
reconcile detects the drift within one cycle + freezes + alerts. Optionally research per-symbol margin
exclusion. Accept as residual.
