# P&L TRACKING — FAIL-PROOF SOLUTION (board + Gro + GAI + web + full failure history) 2026-07-09

Rafael mandate: months of P&L bugs, every fix failed, find the 100% fail-proof solution; summarize
what was tried + missed + what's done differently. This doc is the answer. Sources: failure-history
agent (tb_audit_log/bug_counter/ERRORS/git), Gro, GAI, cold reconciliation-architecture board seat,
web research (Alpaca forums + docs).

## PART A — WHAT WAS TRIED, AND WHY EACH FAILED
~13+ patches over ~3 months (2026-04-20 → 07-09). RC-4 declared "CLOSED" THREE times, reopened each
time under a NEW mechanism. THREE parallel P&L engines were built on top of each other.

Timeline (abridged): RC-4 opened 4/20 · $0-from-wrong-query 4/23 · RC-4 patches 5/04–5/15 (×6) ·
fill-crosstalk ASC sort 6/01 (board REJECTED it twice first) · 4dp storage 6/02 · 3 more RC-4 sites
6/08 · Kelly excludes unverified 6/11 · **"RC-4 CLOSED" 6/15 (held ~2.5wk)** · FIFO lot-duplication
6/27 · **PHANTOM-FILL root 7/03** (submitted_after=None → oldest historical fill: TSLA $347, PANW
−$182.79; fixed: entry-time bound + filled_at DESC + side filter + ±50% band + fail-closed) · Kelly
rebuild (4 phantom trades that don't exist in Alpaca; longs under-sized ~75%) 7/03 · false-drop root
(empty get_open_positions → false external_close) 7/04 · phantom-$0 EOD 7/05 · **`reporting/pnl_ledger.py`
built 7/05 — a 3rd, stateless full-history FIFO engine "to structurally eliminate RC-4" — NEVER WIRED
IN** · repeat-run FIFO $0 artifact 7/06 (chose "Option 1 minimal bridge"; OPT-2 event-sourced replay
DEFERRED) · M1 mechanical decomp (fifo_pnl.py) 7/06 (OPT-2 deferred again) · **false −73.86% kill-switch
trip 7/07** (6 of 7 trades booked $0) + RIVN $0/direction corruption · weekly rollup names it #1 root,
still open 7/08.

**THE PATTERN (root, confirmed):** every fix patched the mechanism that RECOVERS or RECONCILES a
LOCALLY-COMPUTED P&L against Alpaca — none of them STOPPED computing P&L locally. The bot recovers a
fill price at exit time (`fetch_actual_fill_price`); fills settle asynchronously, so the real fill often
isn't queryable yet → it falls back to an estimate → wrong/$0 P&L. Three parallel engines
(`record_exit` caller-price → `_fifo_reconstruct` → `reporting/pnl_ledger.py`) each exist because the
one below it diverged. RC-4 isn't one bug; it's the recurring SYMPTOM of one architectural choice:
maintaining a parallel local P&L that must be kept in sync with Alpaca, instead of treating Alpaca as
the sole source of truth for every read. Secondary root: silent-$0 fallback on ambiguity = a masked loss.

## PART B — WHAT WAS MISSED EACH TIME
- 6/15 "CLOSED" was a COMPLIANCE audit (right function called) not a CORRECTNESS audit (right fill
  recovered). The 7/03 phantom bug lived entirely inside the "compliant" function.
- 6/01 fixed one query path (submitted_after provided) but left the SIBLING path (=None) on the same
  broken ASC/created_at design — which 7/03 then exploited.
- Each fix closed the exact reproducing scenario that had just caused an incident (a query param, an
  empty-batch case, a repeat-run window) WITHOUT removing the general mechanism. Whack-a-mole.
- The team RECOGNIZED the root by 7/05 (built pnl_ledger.py as a replacement) but never made it
  authoritative and never deleted the old engines. OPT-2 (the real fix) was deferred 3×.

## PART C — THE FAIL-PROOF SOLUTION (what's done DIFFERENTLY)
**The one thing never done before: REMOVE local P&L computation entirely. Alpaca is the sole source
of truth for every number. The tracker keeps ZERO P&L.**

### Where every number comes from (zero local estimation)
- **Total account P&L** → Alpaca portfolio-history API (read, never compute). Human-facing/advisory.
- **Unrealized / open P&L** → Alpaca position objects `unrealized_pl` (read, never compute).
- **Realized per-trade / per-strategy** → the ONE thing computed: strict FIFO over an append-only,
  immutable, ingested-FILLS ledger — using the fill record's REAL `filled_avg_price`. Never a recovered
  or estimated price.

### The event-sourced fills ledger (this already exists ~90% — `reporting/pnl_ledger.py`)
Ingest EVERY Alpaca fill (WebSocket `trade_updates` stream, + polling fallback for missed/late/bot-down),
append-only, immutable, deduped by Alpaca `activity_id` (NEVER a timestamp-derived key). FIFO is a PURE
function of the ledger (`fifo(fills) -> lots`), replayable from scratch at any instant. Per-partial-fill
row granularity (Alpaca emits one activity per partial), not per-order.

### ★ THE CORRECTED GRAND INVARIANT (board seat fix — this is the load-bearing correction)
Gro/GAI proposed reconciling `SUM(local realized) == portfolio-history realized_pl`. **That is WRONG**
and would have failed again: portfolio-history realized_pl is PER-DAY (vs a base_value snapshot), not
per-TRADE — for ANY overnight-held position (this bot runs GTC + QHM overnight) the two are different
numbers by construction → false BREAK every overnight day → nuisance-freeze or trained-to-ignore. Plus
PT/ET session + base_value-reset ambiguity.
**CORRECT invariant (exact, point-in-time, no day/timezone/base_value ambiguity):**
> Replay the immutable fill ledger from scratch → reconstruct per-symbol (qty, avg_entry_price) →
> compare to Alpaca's LIVE position objects, EVERY cycle, per symbol. PLUS completeness: every fill in
> Alpaca activities is present exactly once in the ledger.
Portfolio-history stays ADVISORY only, never the automated freeze trigger.

### Position-break protocol (institutional; freeze, never auto-correct)
On a confirmed mismatch → FREEZE new trading for that symbol, CRITICAL alert, diagnostic dump, MANUAL
fix. NEVER auto-correct (auto-correct hides the root — that's how it recurred). If the reconciliation
CHECK itself throws → default FREEZE (fail closed), not skip.
- **pending-settlement (mismatch <2 cycles)** = no freeze (absorbs normal async-fill lag).
- **confirmed break (≥2 cycles)** = freeze. First post-restart cycle = READ-ONLY/reconcile-only by hard rule.

### ★ UNIFICATION WITH PHASE 0 (board seat — the same problem)
Per-strategy attribution CANNOT be solved by `client_order_id` alone: Alpaca's FIFO is SYMBOL-level, not
client_order_id-level. If intraday + QHM both hold NVDA, a sell is matched against the aggregate — you
cannot know which strategy's shares sold unless you enforce it at EXECUTION time (never submit a sell
spanning two strategies' inventory). **That is exactly the Phase 0 per-tier ownership ledger +
`qty_bounded_partial_close` we designed today.** So the fail-proof P&L fix and Phase 0 are the SAME
problem — they must ship TOGETHER. Phase 0 gives correct per-strategy attribution; the fills ledger
gives correct broker-authoritative P&L; the state-reconstruction invariant ties them to Alpaca truth.

### Validation (make FIFO provably correct — McKinney)
- Property-based tests (random buy/sell/partial/multi-strategy sequences): realized sum == naive
  (proceeds − cost basis); open-lot qty == Alpaca qty.
- Golden-master regression: replay the REAL historical fills from the bugged months; assert the new
  engine reproduces Alpaca's account-level realized for that period.

### Migration (no flag day)
Wire pnl_ledger.py as authoritative in SHADOW mode → validate weeks (investigate every discrepancy) →
switch per-component (unrealized→positions, total→portfolio-history, realized→ledger) → enable freeze →
DELETE the old parallel engines (record_exit price computation, _fifo_reconstruct). Deleting the old
engines is mandatory — leaving them is how the divergence persisted.

### THE ONE INVARIANT that ends it: reconstructed-position-from-ledger == Alpaca live position object,
every cycle, per symbol; freeze on confirmed break. Most likely residual failure: a fill Alpaca's
activities API never surfaces (permanently missing) → persistent break → needs a written 2am manual-
reconcile runbook (specify before ship).

## WHAT THE TRACKER STILL KEEPS (must preserve — it is load-bearing beyond P&L)
Exit-state machine (ACTIVE/AWAITING_FILL/PENDING_* → gates stop/target/trail), GTC stop lifecycle,
Kelly R-multiple stats (were distorted 75% by phantom losses), the 7% kill-switch input, per-strategy
attribution, dashboard/weekly/monthly surfaces. Remove ONLY the P&L numbers; keep the state.
