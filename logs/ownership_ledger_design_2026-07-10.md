# Ownership-Ledger Maintenance — Design Decision (2026-07-10)

**Fork:** how does the per-tier share-ownership ledger stay current each cycle?
**Decision: OPTION C (hybrid).** UNANIMOUS — Gro ✓ · GAI ✓ · Board/Reliability (Majors/Kim) ✓ · Board/Execution-risk (Thorp/Taleb) ✓.

## The options
- **A — Full replay each cycle** (re-fetch entire fill history, overwrite ledger). **RULED OUT** — recomputes the floor from a truncatable external source; when an old F6/QHM buy ages out of Alpaca's activities window the protected tier undercounts and the floor **silently shrinks** (drift reads 0 because ledger-sum and Alpaca-net shrank together). Directly violates the locked invariant ("floor is LEDGER-derived ONLY, never recomputed from Alpaca's net — a drift must FREEZE sells, not silently shrink the floor").
- **B — Incremental delta** (seed once via `launch_init`; each cycle apply +/- deltas from NEW fills to the persisted ledger under the fill's tier tag; drift check freezes sells on any total mismatch). Correct normal-case authority.
- **C — Hybrid = B is the per-cycle authority + a full-replay heal tool (`sync_ledger`) that REFUSES to reduce any protected-tier qty vs the current persisted ledger** (a replay that would shrink a protected floor = truncation detected → abort + CRITICAL alert, never write). **CHOSEN.**

## Asymmetry (execution-risk seat, Thorp/Taleb)
Over-freezing = opportunity cost (safe). Under-protecting = a never-sell share gets sold = permanent capital loss + invariant breach. **Design biases toward over-freezing. Any sell that can't be audited must not happen.**

## Load-bearing finding — per-tier misattribution (GAI → execution-risk seat)
The total-mass drift check (`sum(ledger tiers) == Alpaca net`) does NOT catch a **mistagged fill** that shifts qty between tiers while the total stays constant (e.g. an intraday sell mistagged F6 shrinks F6, floor drops, drift=0, guard passes). **Defenses required before the guard gates live sells:**
1. **Immutable fill attribution** — once a fill_id is synced with a tier, LOCK it; the incremental engine only applies NEW fills (id/time newer than the per-tier `last_fill_id`) and never re-attributes an existing fill. (Makes today's vestigial `last_fill_id` field real.)
2. **Level-2 per-tier reconciliation** — track per-tier drift, not just total; a broken tier quarantines the symbol.
3. **Untagged-fill-after-launch = quarantine** — a post-`launch_init` untagged fill is an external/manual trade; freeze that symbol + alert (pre-launch untagged history → intraday is correct).

## Mandatory hardening list (board consolidated — before the guard gates live sells)
| # | Item | Source |
|---|------|--------|
| 1 | Immutable fill_id attribution (never re-attribute a synced fill) | exec-risk + reliability |
| 2 | Per-tier (Level-2) reconciliation + per-tier drift in ledger | GAI + exec-risk |
| 3 | Fill dedup by fill_id (duplicate/restart-mid-cycle double-count) | reliability |
| 4 | Chronological fill sort before any replay/apply | reliability + exec-risk |
| 5 | Aged-out detection: Alpaca net > ledger_sum on a protected tier → possible-data-loss event → global sell freeze + alert | reliability + exec-risk |
| 6 | Corporate actions (split/spin-off change qty, not via fills) → pre-adjust via `get_corporate_action_announcements()`, else manual heal | exec-risk |
| 7 | Untagged-fill-after-launch quarantine + alert | exec-risk |
| 8 | Real-time reconcile every cycle + Slack alert on drift; `ownership_ledger_age_seconds` gauge; drift-timeout escalation (>30 min = SEV-1) | reliability |
| 9 | Alpaca-net snapshot staleness window (reject guard check on a stale read) | exec-risk |
| 10 | F6-trim authorization audit trail (flag alone is insufficient) | both seats |
| 11 | Negative drift (Alpaca < ledger) = CATASTROPHIC (a sell happened outside the system) → global freeze + immediate escalation | exec-risk |
| 12 | File-level lock around ledger load/save (concurrent heal vs cycle) | reliability |

## ⚠️ RC-6 DATA-LAYER CONSTRAINT (verified live 2026-07-10) — attribution needs a JOIN
Alpaca's **FILL activity object does NOT carry `client_order_id`** — only `order_id` (server UUID).
Verified live: FILL keys = `[activity_type, cum_qty, id, leaves_qty, order_id, order_status, price,
qty, side, symbol, transaction_time, type]`. The **ORDER object DOES carry `client_order_id`**, and
`fill.order_id == order.id`. Therefore **tier attribution from the fill feed alone is impossible** —
`tier_of_coid(fill.get("client_order_id"))` is always None on a raw Alpaca fill.

**Required for ALL attribution (steps A–D):** join `fill.order_id → order.client_order_id → tier`.
The engine must `fetch_all_orders()` (paginate `/v2/orders?status=all`), build an
`{order.id: client_order_id}` map, and resolve each fill's tier through it.

**Impact on the SHIPPED heal tool (`sync_ledger`, decbf77):** it reads `fill.get("client_order_id")`
→ always None → attributes 100% to intraday. This is SAFE (untagged→intraday is the documented
fallback; forever6/qhm=0 today) but is a **no-op attribution** — sync_ledger cannot distinguish
tiers until it is given the order_id→coid map. Step-B refinement: add an optional
`coid_by_order_id` param to `sync_ledger` (+ the incremental engine) and resolve tier via
`coid_by_order_id.get(fill["order_id"])`, falling back to `fill.get("client_order_id")`. Also correct
the sync_ledger docstring line "a fill's tier = its client_order_id prefix" → "via the order_id→coid join".
`reporting.pnl_ledger.fetch_all_fills()` is the reusable fill source; add a sibling `fetch_all_orders()`.

## ⚑ MAINTAINER FORK RE-RESOLVED (2026-07-10, after the safe heal tool shipped) → OPTION 1, UNANIMOUS
Gro ✓ · GAI ✓ · cold board Reliability(Kim/Majors) ✓ · cold board Execution-risk(Thorp/Taleb) ✓.
**The incremental-delta engine (the ORIGINAL pick) is ELIMINATED as the per-cycle maintainer.** Its rationale
("full-replay recomputes the floor from a truncatable source") was superseded by `sync_ledger`'s
refuse-to-shrink guard, which makes full-replay SAFE against exactly that. All four voices: Option 2 now buys
ZERO correctness over safe full-replay — only cost — while its cursor failure modes (missed cycle, dup,
out-of-order, restart-mid-cycle, per-tier misattribution at total-drift=0) are a fail-closed VIOLATION the
full-replay + abort-on-shrink does not have. Execution-risk seat proved: a stale-but-never-shrunk floor is the
SAFE direction (guard reads LIVE Alpaca net at decision time; any real breach → drift≠0 → REJECT).

**DECISION — the per-cycle maintainer is `sync_ledger` (full-replay), NOT every cycle but THROTTLED:**
- Cadence: run every **3–6 cycles (~15–30 min)** during RTH + **once at market open** + **event-triggered**
  immediately when the guard REJECTS a sell (LEDGER_DRIFT / unreadable). Removes ~80% of Alpaca API pressure.
  (Reliability seat: full-replay cost is fine now — 370 fills ≈4s — but linear; ~54s@5k, ~108s@10k, untenable
  ~50k. Shares the 175 req/min Alpaca quota with the scanner → throttle, don't every-cycle.)
- Incremental engine = FUTURE Option-3 threshold path (build only at ~10k+ fills), and ONLY ever as a perf
  layer DOWNSTREAM of the guard, never replacing it.

**MANDATORY instrumentation before any prod RTH run (reliability seat):** per-replay wall-time (WARN>10s /
CRIT>30s), healed=False streak (CRIT≥3 consecutive → ledger stale, alert), ledger age vs cadence, drift count,
per-tier ownership sanity (protected_floor per symbol), + a pagination-timeout wrapper on fetch_all_fills/
fetch_all_orders (CRIT on _max_pages hit).

**Guard-gates-live-sells (step D) PRECONDITIONS (execution-risk seat):** per-tier Level-2 reconciliation (#2),
untagged-after-launch quarantine (#7), Alpaca-net staleness window (#9) MUST land before the guard gates a
live sell. The maintainer (below) can stand up first — it only writes the ledger; nothing reads it to gate yet.

## Build sequence (REVISED 2026-07-10 — incremental engine removed)
- **A. Heal tool** — `sync_ledger` refuse-to-shrink. ✅ SHIPPED (decbf77).
- **A2. Join foundation** — `fetch_all_orders` + `build_coid_map`. ✅ SHIPPED (4c0902e).
- **A3. sync_ledger join param** — attribute by order_id→coid. ✅ SHIPPED (d33c10c).
- **C. Maintainer (was "step C")** — a STANDALONE script (`run_ledger_sync.py`, new; NOT inside run_cycle →
  no RTH-hotspot edit, no restart) that: loads env → fetch_all_fills + fetch_all_orders→build_coid_map +
  fetch_positions → sync_ledger(join) → instrument (wall-time/healed/drift/per-tier). Run via cron on the
  throttled cadence + at open. Inert w.r.t. live trading (only writes the ledger). ← NEXT.
- **C2. Hardening before guard-gating** — #2 per-tier Level-2 reconciliation, #7 untagged-after-launch
  quarantine, #9 staleness window (fold into ownership_guard + the maintainer).
- **D. Guard wiring** — only AFTER C2: route reducing-order paths through `check_never_sell_floor`;
  close_position multi-tier disallow; entry_logic tier-tag. Needs a restart + full RTH gate.

### Original (pre-2026-07-10) build sequence — superseded above, kept for history
- **A. Heal tool** — harden `sync_ledger` into the Option-C heal tool: chronological sort + refuse-to-shrink-any-protected-tier vs persisted ledger (abort+alert, never write on shrink). INERT (no caller). ← shipping first.
- **B. Incremental-delta engine** — `apply_new_fills(persisted, new_fills)` with immutable fill_id attribution (#1), dedup (#3), per-tier reconciliation (#2), untagged-after-launch quarantine (#7). INERT until wired.
- **C. Per-cycle wiring** — `launch_init` at startup (once) + incremental engine + `reconcile_drift` each cycle + aged-out/negative-drift freezes (#5, #11) + observability (#8). Ledger now live-maintained but still gates nothing.
- **D. Guard wiring** — only AFTER B/C prove per-tier integrity: route reducing-order paths (exit_logic, broker close/partial) through `check_never_sell_floor`; `close_position` hard-disallow on multi-tier; entry_logic tier-tag. THIS is the increment that needs a restart + full RTH gate.

Corporate actions (#6), F6-trim audit (#10), staleness window (#9), file-lock (#12) fold into B/C/D as each path is built.
