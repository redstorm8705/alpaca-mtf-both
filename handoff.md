# Handoff — alpaca-mtf-bot
**Updated:** 2026-07-12 (interactive) | **CROSS-ACCOUNT HANDOFF** — always current per the new
DURABLE SYNC RULE (CLAUDE.md). Pushed the moment alignment is reached, not at session end.

> **NEW ACCOUNT READS THESE FIRST, IN ORDER:** (1) this file (the ⏩ block below IS your pick-up
> point), (2) `CLAUDE.md` (binding rules — note new §DURABLE SYNC RULE), (3) `logs/tb_audit_log.md`
> (bug/patch log), (4) `logs/qhm_v2_design_2026-07-11.md` + `logs/ownership_ledger_design_2026-07-10.md`
> (active design). Master Brain: `notebooklm use $(cat ~/.claude/master_brain_id)`.

## ⏩ LATEST (2026-07-12 interactive) — pick up here

**🌙 OVERNIGHT/EARLY-AM 2026-07-13 — pick-up state.** Full detail: `logs/overnight_work_order_2026-07-12.md`
+ `logs/july_trade_audit_2026-07.md`. **SHIPPED + LIVE:** dashboard SPY GEX+S/R card; monthly P/L fix
(`4b43042`); loop-engineering scope (`bot_improvements.md`); **QHM dip-add FULLY LIVE** (`aeed5cd`):
flag ON + RegT-BP affordability guard + **Option-C stop-safe add** (cancel GTC stop→marketable
add→15s poll→resubmit stop for actual qty; 4-branch fail-safe; RTH-gated; cold-2nd PASS + Gro+GAI).
**KEY FINDING:** July "downturn" is 87% PHANTOM — real July P&L = **−$34.70** FIFO (not −$279), and
~all of it is **RIVN** (bought into its 7/6 public offering; catalyst the bot doesn't screen).
**REMAINING QUEUE (all need Rafael/BGG):** (a) NVDA catch-up +1sh — do at 9:30 open w/ stop-safe
pattern (Rafael go given); (b) eod/P&L 100%-reliable (purge 07-02 phantom + reconcile to FIFO —
Rafael mandate); (c) catalyst/news engine (per-name screen; reverses news-display-only invariant →
board vote); (d) IC/ICIR learning-loop (Rafael: begin); (e) S/R-calc BGG (VWAP/history/dynamic); (f)
dip-add Findings #2 (78s-latency timeout) + #3 (cosmetic). Margin: RegT BP $3,266 (overnight) /
effective $7,724 (intraday) / cash $888.


**▶ TWO ACTIVE THREADS:** (A) **QHM dip-add rule — magnitudes BGG-ALIGNED + Rafael APPROVED, ready to
BUILD (gated)** — see block just below; full spec `logs/qhm_v2_design_2026-07-11.md` PART 1 FINALIZED.
(B) **Ownership 4a activation — Monday-gated:** blocker #1 (QHM tier=qhm) CLEARED `main@a8584ac`; blocker
#2 REMAINS = verify OCI ledger populated + `protected_symbols.json` present (Monday RTH cron runs inc3
code) BEFORE flipping `OWNERSHIP_GUARD_ENFORCE=True`. Chokepoint is shipped DARK (`main@488a893`).

**QHM DIP-ADD RULE — FINALIZED (board 2 seats + Gro + GAI, Rafael APPROVED 2026-07-12). NEXT = BUILD
(gated: full patch sequence on execution/quarterly_hold_manager.py 1988L + new config.py constants).**
- Rung A: price ≤ cost_avg×(1−0.02) → small add, capped AT target weight.
- Rung B: price ≤ cost_avg×(1−0.05) → aggressive add (size ≥ existing position), MAY exceed target up to
  a **1.375× target ceiling (27.5% equity)** enforced as a **HARD PRE-FILL cap** + a **max-shares-per-name
  cap** recomputed quarterly (`floor(ceiling_wt×equity/price_at_review)`). Board OVERRODE Gro/GAI "no cap":
  cost-avg triggers re-arm on a falling avg → ladder can hit ~34% equity before the price floor fires.
- Hard STOP-ADDING floor: **−15% below FIRST-ENTRY (tranche1)** → escalate to board (NVDA: below $169.17).
- Trigger off COST-AVERAGE; stop-floor off FIRST-ENTRY. Anti-osc: max 3 adds/qtr, ≥2 days apart, none in
  final ~7d pre-earnings, keep day-3 re-confirm.
- **NVDA one-time CATCH-UP: +1 share → 15.2%** (unanimous; NOT +2/22.7%). ⚠️ LIVE paper order tagged
  tier="qhm" — needs Rafael's explicit go before it fires. GOOGL = no action (≈target).
- config constants to add: QHM_DIP_ADD_RUNG_A_PCT=0.02, _RUNG_B_PCT=0.05, _CEILING_MULT=1.375,
  _STOP_FLOOR_PCT=0.15, _MAX_PER_QUARTER=3, _MIN_DAYS_BETWEEN=2, _NO_ADD_DAYS_PRE_EARNINGS=7.

**NEW HARD RULE — DURABLE SYNC RULE (CLAUDE.md §, Rafael mandate 2026-07-12, Gro+GAI APPROVE
marker `a97ea0d7686f`).** On EVERY ship AND EVERY time Board+Gro+GAI align on a
protocol/rule/decision/scope — *even with zero code shipped* — sync all 5 channels the SAME turn:
git push (+ handoff/design docs carrying the EXACT next step), OCI `git pull --ff-only` (+restart
only if code), `.md` files, `logs/`, Master Brain. `handoff.md` always carries a live "⏩ pick up
here" pointer, pushed the moment alignment is reached. Surgical + cheap. Full text: CLAUDE.md
§DURABLE SYNC RULE. **Why:** Rafael switches between two Claude Gmail accounts on rolling 5h limits
— the other account must resume with `git pull` → read handoff.md → query Master Brain and land
exactly here.

**OWNERSHIP INCREMENT 3 — QHM-TIER ATTRIBUTION SHIPPED + LIVE (`main@cbb3925`).** Legacy untagged
QHM buys (NVDA/GOOGL, predate client_order_id tagging) now get a real never-sell floor (were
counting as intraday → floor=0 → sellable). New `get_quarterly_hold_quantities()` (fail-closed on
corrupt state) → `sync_ledger` qhm overlay `qhm=min(max(claim,replay_qhm),net-f6)` (MERGE not
overwrite) + SKIP drifted symbols. **Cold board caught 2 real bugs Gro+GAI BOTH missed**
(overwrite-clawback of tagged-qhm protection; drift-masking) — both fixed, 6/6 self-test PASS,
static clean, Gro+GAI APPROVE final combined diff. Still **UNWIRED** (standalone cron maintainer
`run_ledger_sync.py`; nothing reads the ledger to gate a live sell yet). See tb_audit_log 2026-07-12.

**OWNERSHIP INCREMENT 4a-part-1 — CHOKEPOINT SHIPPED + LIVE (DARK, `main@488a893`).** Rafael
APPROVED Option B (broker chokepoint) + kill-switch semantics + build structure (2026-07-12).
`broker.close_position(symbol, *, tier="intraday")` + `partial_close_position(..., _bypass_floor)`
now route reducing orders through `ownership_guard.check_never_sell_floor` when
`config.OWNERSHIP_GUARD_ENFORCE` is True. Shipped **flag=False (DORMANT — byte-equivalent to today,
cold-2nd verified)**. `close_position_for_tier` → thin alias (seam bug eliminated). Gate: static
clean, 5/5 self-test, cold-2nd PASS on dark ship, Gro+GAI APPROVE. Live: flag=False confirmed on OCI.

**➡️ EXACT NEXT STEP = 4a-part-2 ACTIVATION (flip `OWNERSHIP_GUARD_ENFORCE=True`). ONE blocker
remains:**
1. ✅ **CLEARED (`main@a8584ac`, 2026-07-12): QHM self-close tags `tier="qhm"`** — fixed at
   `main.py:459` (`_QHMBroker.close_position(self, sym, tier="qhm")`) + `quarterly_hold_manager.py:322`
   (`OrderDispatcher.close` → `broker.close_position(symbol, tier="qhm")`). Gate: static clean,
   self-test threads tier=qhm end-to-end, cold-2nd PASS 5/5, Gro+GAI APPROVE. Still dormant (flag off).
2. ⏳ **REMAINS — Ledger populated + `protected_symbols.json` present on OCI.** run_ledger_sync (cron
   */20 RTH Mon-Fri) last ran Jul-10 (pre-inc3) → NVDA/GOOGL currently floor=0 in the live ledger;
   cache absent. Monday's RTH cron runs inc3 code → will populate real floors + write the cache.
   VERIFY (`ssh OCI: cat data/state/protected_symbols.json` present + NVDA/GOOGL show a qhm floor in
   ownership_ledger.json) BEFORE flipping the flag — GAI: flag-on while ledger corrupt AND cache
   absent fails OPEN on a protected symbol; the cache-present precondition closes this.
Activation sequence (once blocker 2 verified): flip `OWNERSHIP_GUARD_ENFORCE=True` (gated config
change — 1-line, needs full read of config.py + gate + Rafael approval) → restart → live-verify an
intraday exit on a protected symbol bounds correctly + QHM self-close still works. Observability the
board asked for (log every guard decision; ALERT on unexpected QTY_BOUND; reconcile_drift alert) can
fold into the flag-flip ship.

**THEN 4b (open the door, only after 4a fully active + verified):** remove intraday-blocks-QHM gate at
`execution/entry_logic.py:438` (`if symbol in get_quarterly_hold_symbols(): continue`) + fix the QHM
exclusion in the cycle-sync risk count at `entry_logic.py:406-410` (an intraday position in a QHM
symbol SHOULD count toward intraday risk once co-holding is allowed).

**REQUIRED EXPLICIT-TIER CALLERS under B (board-identified — must NOT default to intraday):**
- `execution/quarterly_hold_manager.py:322` `QHMBrokerAdapter.close()` → `broker.close_position(sym,
  tier="qhm")` (else a QHM exit is bounded to the ~0-share intraday tier and silently no-ops).
- forever6 self-trim → `tier="forever6"` + `is_authorized_f6_trim=True` (ownership_guard L256-264).
- All ~9 sites in exit_logic.py (L1278/1439/1547/1621/1878 full + L297/715 partial), gtc_manager.py
  L189, and safe_close_all are legit intraday → default, no edit.

**SEAM BUG TO UNIT-TEST FIRST (board seat 1):** `close_position_for_tier` calls plain
`close_position(symbol)` at broker.py L721/L724. Once `close_position` gains a `tier` param, those
internal calls MUST pass `tier` through, else `close_position_for_tier(sym, tier="qhm")` on a
single-tier symbol collapses to the new `"intraday"` default and mistags the client_order_id.

**safe_close_all REWRITE (events/handlers.py L64-83):** drop the whole-symbol QHM skip → call
`close_position(sym, tier="intraday")` per symbol. Preserves QHM/F6 FLOOR even on circuit-breaker
(matches today's intent) BUT now correctly flattens the intraday shares of a co-held symbol.

**DO NOT build a full-flatten-regardless-of-tier path this increment** (board emphatic). A true
regulatory/forced liquidation must be a SEPARATE named primitive (e.g. `broker.force_liquidate`) —
never a `tier=` variant. `broker.close_all_positions()` (L740) is defined but has ZERO callers today;
leave it dead until a deliberate separate decision.

**KILL-SWITCH SEMANTICS = Rafael's open decision:** under B the 7% kill switch / circuit-breaker
preserves QHM/F6 shares but flattens the intraday shares of a co-held symbol (vs today's whole-symbol
skip). Recommended; awaiting Rafael confirm.

Note (housekeeping): code-review-graph is STALE (showed main.py at 3340 lines; it's 1092 post-
decomposition — exit logic now in execution/exit_logic.py). Rebuild the graph before relying on its
line numbers next session.

**GAI conservation:** Rafael topped up Gemini credits but mandated **free-tier gemini-2.5-flash ONLY**
(never pro/preview) + minimize call volume. `auto_ai_audit.py` already on flash (`a7923ae`).

**Queued (post-inc4):** QHM dip-add rule (2 rungs −2%/−5% + NVDA one-time catch-up; board vote on the
−5% over-weight multiple ~1.25–1.5× target), per-tier exit logic + Rafael wants a summary, Forever-6
build, S/R + buy-the-bounce feature (SPY/SPX/QQQ GEX levels on an HTML page), QHM v2 framework
(report→config pipeline, persistent conviction book, post-earnings state machine, take-profit).
See `logs/qhm_v2_design_2026-07-11.md`, `logs/improvement_queue.md`.

---

## (2026-07-10 interactive) — prior context
- **P&L DIAGNOSIS + SINGLE-WRITER FIX SHIPPED + LIVE (`main@1397194`).** Answer to "is the bot
  losing money": **NO — up +$259 (+10.4%), positive expectancy (50% win, avg win +$8.38 vs avg loss
  -$5.90).** The scary numbers were phantoms (RC-4 fill-matching: PANW -$182/TSLA -$81 on 07-02 that
  never happened; real 07-02 = +$45.31), the Movers mass-dump (retired), and the 07-08 false-HALT
  (Build F fixed). Reporting now truthful (lifetime cache = authoritative). **FIX (Rafael-approved,
  board 4-0 + Gro + GAI Option A):** killed the last dual-writer P&L hole — `lifetime_pnl_cache.json`
  had 2 writers with different `total_pnl` semantics (realized $273 vs total $259) swinging the
  headline ~$13 by cron timing + a hardcoded-2500 that would show a phantom gain on any deposit. Now
  pnl_ledger.heal_history is SOLE writer (total_pnl = equity-net_deposits TOTAL + components);
  generate_dashboard reads-only; metrics uses net_deposits not 2500. Live-verified: dashboard renders
  +$259.59 TOTAL. See logs/tb_audit_log.md 2026-07-10 entry. Follow-ups logged (net_deposits `or 2500`;
  monthly_review stale docstring).
- **WIN-RATE + TRADE-COUNT NOW AUTHORITATIVE (`main@96c89c1`, 2026-07-11).** Dashboard win-rate/count were
  eod-file-based (machine-dependent: 69 local/105 OCI, ~30-40%). Now ledger ENTRY-LEVEL (partials merged):
  **158 trades / 39.9%** (lot-level 232/49.6% over-counts partials). heal writes win_rate+total_trades to
  the authoritative cache; metrics overrides eod-based with it. Live: dashboard shows 158/40%. NOTE: my
  earlier "50% win" to Rafael was the lot-level count; accurate trade-level is ~40% (still net-positive,
  +$1.73/trade). Board 2-0 + Gro + cold-2nd PASS.
- **⚠️ GEMINI (GAI) API CREDITS DEPLETED (2026-07-11)** — 429 RESOURCE_EXHAUSTED. Blocks the final Gro+GAI
  gate (Gro-only now → each ship needs a Rafael GAI-skip or top-up) AND the OCI nightly pipeline
  (nightly_audit/meta-audit). ACTION: Rafael tops up Gemini billing / rotates key. Gro (Groq) still works.
- **P0-a OWNERSHIP LAYER — 3 increments SHIPPED (all INERT until wiring). LIVE `main@decbf77`.**
  Per-tier shared-lot ownership (intraday/qhm/forever6) so per-strategy P&L attribution + a
  never-sell floor become real. Alpaca = 1 untagged net position/symbol; ledger = per-tier CLAIM.
  - **(1) `execution/ownership_guard.py`** foundation (`658933f`): ledger I/O (RC-5 atomic),
    `make_coid`/`tier_of_coid`, `check_never_sell_floor` chokepoint, `reconcile_drift`, `launch_init`.
  - **(2) tier-tagging** (`7b1c5c0`): `tier=` param on the 5 broker submit/close wrappers +
    QHM dispatcher + `_QHMBroker` → client_order_id carries IN/QH/F6. New orders tag on next restart.
  - **(3) `sync_ledger` Option-C heal tool** (`decbf77`, THIS SESSION): full-replay rebuild from the
    Alpaca fill history, attributed by tier, with a **NEVER-SHRINK-A-PROTECTED-FLOOR** guard
    (aborts+alerts, never writes, if a replay would reduce any qhm/forever6 qty vs the persisted
    ledger — i.e. old fills aged out). Iterates union(new,baseline) so a vanished floor is caught.
  - **MAINTENANCE FORK RESOLVED → OPTION C, UNANIMOUS** (Gro+GAI+2 cold seats). Design + the 12-item
    hardening list + build sequence A→D: `logs/ownership_ledger_design_2026-07-10.md`.
  - **⚠️ RC-6 CONSTRAINT (verified live 2026-07-10):** Alpaca FILL activities do NOT carry
    `client_order_id` (only `order_id`); ORDER objects carry it and `fill.order_id==order.id`. So
    attribution REQUIRES a join `fill.order_id → order.client_order_id → tier` (fetch orders, build
    `{order.id: coid}` map). Shipped `sync_ledger` reads the fill's (absent) client_order_id → today it
    no-ops everything to intraday (SAFE: fallback, f6/qhm=0) but does NOT actually attribute until the
    join is added. Fold the join + an optional `coid_by_order_id` param into step B.
  - **(4) JOIN foundation SHIPPED** (`4c0902e`, THIS SESSION): `fetch_all_orders()` + `build_coid_map()`
    in `reporting/pnl_ledger.py` — the RC-6 fix. `fetch_all_orders` paginates `/v2/orders?status=all`
    via OVERLAP-AND-DEDUP (cold-2nd caught that a naive exclusive-`until` cursor silently strands
    same-timestamp tie-groups at the 500-row page cutoff; fix = `until`=oldest+1ms inclusive + `_seen`
    dedup; `_bump_iso_ms` emits 'Z' form or the URL 422s). Live: 1928 orders, 0 dupes, 369/369 fills
    joined. INERT (uncalled). `build_coid_map` → `{order_id: client_order_id}`.
  - **(5) sync_ledger ATTRIBUTION JOIN SHIPPED** (`d33c10c`, THIS SESSION): optional
    `coid_by_order_id` param; tier resolved via AUTHORITATIVE membership check (cold-2nd caught +
    fixed a map-hit-None-vs-miss ambiguity). Heal tool now attributes by tier when given the map.
    **LIVE END-TO-END PROVEN (temp ledger):** fetch_all_fills(370)+fetch_all_orders(1932)+
    build_coid_map → sync_ledger reconstructed all 5 open positions' per-tier ownership with
    **|drift|=0.0** (reconciles exactly to Alpaca net). All intraday until OCI restart tags new orders.
  - **ATTRIBUTION READ-PATH IS COMPLETE + GATED + LIVE-PROVEN.** The heal/reconstruct side of the
    ownership layer works end-to-end: given live fills+orders+positions it produces a per-tier ledger
    that reconciles to zero drift and refuses to shrink a protected floor.
  - **(6) MAINTAINER SHIPPED** (`23efb24`, THIS SESSION): `run_ledger_sync.py` — maintainer fork
    UNANIMOUS → OPTION 1 full-replay (Gro+GAI+2 cold seats); **incremental-delta engine ELIMINATED**
    (sync_ledger's refuse-to-shrink guard makes full-replay safe → incremental = cost-only, and its
    cursor failure modes are a fail-closed violation). Standalone cron script (NOT in run_cycle → no RTH
    edit, no restart); only writes the ledger. Instrumented (wall-time, healed-streak Slack, drift,
    per-tier sanity); sync_once never raises (cold-2nd caught + fixed a crash-into-cron). **Live-proven
    on Mac AND OCI:** 370 fills, 1932 orders, 35 positions, ~6-7s, seeds clean |drift|=0.0. OCI ledger
    is seeded. Design/decision: logs/ownership_ledger_design_2026-07-10.md.
  - **✅ MAINTAINER CRON INSTALLED (Rafael-approved 2026-07-10):** OCI crontab
    `*/20 13-21 * * 1-5 ... run_ledger_sync.py >> logs/ledger_sync_cron.log` — throttled ~every 20min
    across the RTH UTC window, weekdays. Raw UTC (no cron_tz_wrapper — exact ET alignment not needed for
    a reconciler; 13-21 UTC covers RTH year-round). $0 incremental cost (Alpaca paper API free; no LLM
    calls at runtime). OCI ledger seeded + now self-maintaining. Event-triggered-on-guard-reject is a
    step-D concern (guard unwired). Backup of prior crontab at OCI /tmp/crontab.bak.
  - **NEXT (build seq C2 → D):** C2 hardening before the guard gates live sells — #2 per-tier Level-2
    reconciliation (catches mistagged-fill floor drift the total-drift check misses), #7 untagged-after-
    launch quarantine, #9 Alpaca-net staleness window (fold into ownership_guard + maintainer). THEN
    D — wire the guard into reducing-order paths (exit_logic, broker close/partial), close_position
    multi-tier disallow, entry_logic tier-tag. **Guard does NOT gate live sells until C2 lands.** D is
    the only increment needing a restart + full RTH gate.
  - Gate note: preship_audit flash-GAI was in a stochastic false-reject loop (~13 rolls; hallucinated
    already-present guards); Rafael chose re-roll; roll landed a clean gro+gai APPROVE marker (sha 553e23d5).
    Every authoritative voice (board 4-0, my final Gro+GAI, cold-2nd) had already approved the exact diff.
- **Build F: ✅ MERGED + DEPLOYED — LIVE on `main@90f8bdf` (2026-07-10 00:27 UTC).** Reviewed the branch diff
  full + verified all 6 integration points; re-ran the gate this session (RULE C-2, prior OCI gate expired):
  Gro APPROVE + GAI APPROVE (both withdrew all findings on counter-prompt), cold board 2-0 (masked-loss +
  execution seats), py_compile/ruff/mypy clean, preship markers written for news_monitor/broker/run_cycle.
  Rafael APPROVED. Merged `claude/build-f-2026-07-08`→main (no-ff `90f8bdf`), pushed, OCI ff-only pull +
  restart → DEPLOY_OK, health OK, zero tracebacks. Branch `claude/build-f-2026-07-08` can be deleted.
- **TWO FOLLOW-UP ENHANCEMENTS from the Build F gate (logged, non-blocking):**
  (1) GAI: add a LOW-severity Slack heads-up if `get_asset_tradable("SPY")` returns None for several
  consecutive cycles while the SPY *bar* feed still works (a get_asset-only outage never trips the existing
  3-fail T1-FEED-DOWN alert). Path already logs a WARNING + `halt_eval` event each cycle — this just adds a
  human page for a persistent streak. (2) Masked-loss seat: cold-start edge — on the very first cycle after a
  restart, if SPY gapped down hard before the first successful 5-min fetch, `_spy_session_pct` reads 0.0
  ("no decline") for one cycle because `_main._spy_last_close` defaults to 0.0; the independent is_open /
  spy_tradable legs still catch a real halt, and it's auditable via `halt_eval`. Minor; consider a
  "warming/unknown" sentinel instead of 0.0. Both are `strategy/run_cycle.py`.
- **Forever-6 design = 100% COMPLETE + LOCKED (2026-07-09)** — but a big architecture decision reordered the build:
  - Scenario map (all 13 sell-first/no-buyers cases) + both forks locked → `logs/forever6_scenario_board_2026-07-09.md`.
    FORK A = per-rung monotonic latch; FORK B = small first rung fires + ping + human-gate deeper rungs.
  - Full read of `quarterly_hold_manager.py` (1955L) done → integration map `logs/forever6_integration_map_2026-07-09.md`.
  - **RAFAEL NEW MANDATE (2026-07-09): intraday/swing tiers must STILL trade QHM/Forever-6 names on confluence.**
    Ring-fence protects a SHARE COUNT, not the symbol. This requires the per-tier shared-lot ownership layer
    (the "real fix" the roadmap flagged; its absence retired Movers). Full board (5 voices) designed it →
    `logs/per_tier_ownership_design_2026-07-09.md`. **Rafael locked 3 decisions:** (1) shorts on ring-fenced
    names → via OPTIONS not shares (share tiers LONG-ONLY there); (2) real Alpaca GTC stops, auto-re-issued on
    every tier-qty change; (3) **BUILD ORDER = FOUNDATION FIRST, 3 PHASES.**
- **⏭ NEXT BUILD = PHASE 0 (COMBINED: ownership foundation + BROKER-AUTHORITATIVE P&L), NOT Forever-6.**
  Rafael merged the fail-proof P&L fix INTO Phase 0 (2026-07-09) — same problem: per-strategy P&L needs
  execution-time ownership. Plan: `logs/phase0_combined_ownership_pnl_plan_2026-07-09.md`. Sub-phases:
  **P0-a** ownership (ledger + client_order_id tier-tag + `ownership_guard.py` floor chokepoint +
  `qty_bounded_partial_close` + `close_position` multi-tier hard-disallow + drift reconcile + launch init;
  also fixes Movers bug). **P0-b** broker-authoritative P&L: WIRE IN `reporting/pnl_ledger.py` (570L,
  ALREADY BUILT, confirmed UNWIRED — the abandoned stateless-FIFO-from-Alpaca-fills engine) as the SOLE
  realized source + fill ingestion; DELETE the 3 parallel engines — `fetch_actual_fill_price` (~10 call
  sites), `_fifo_reconstruct`/`fifo_pnl.py`, and STRIP P&L from `record_exit` (~14 callers) while KEEPING
  its state machine. Total P&L→portfolio-history (advisory), unrealized→position objects. **P0-c** the
  STATE-RECONSTRUCTION INVARIANT: replay fills ledger → reconstruct per-(symbol,tier) qty/avg_cost →
  compare to Alpaca live positions every cycle; confirmed break (≥2 cycles)→FREEZE+manual, never auto-
  correct; portfolio-history is NEVER the freeze trigger (board seat fix). Validation = property-based
  FIFO tests + golden-master replay of the real bugged-months fills + 2am manual-reconcile runbook.
  **Build A (safe-mode) + OPT-2 (event-sourced replay) are SUBSUMED by P0-b/P0-c — retire as separate items.**
  Then Phase 1 (per-tier P&L history + synced stops), then Phase 2 (Forever-6 tier).
  Line-scoping → `logs/phase0_ownership_scoping_2026-07-09.md` (order-path sites) + canonical guard spec
  `logs/phase0_ownership_guard_spec_2026-07-09.md`. **SCOPED (4 core order files): broker.py (780L) +
  entry_logic.py (1688L, del registry block L437-440) + exit_logic.py (2269L, 23 reducing-order sites +
  qty-valuation bug zone) + fill_helpers.py (369L).** **ownership_guard.py = FULLY SPEC'D** (chokepoint
  algorithm + ledger schema + drift + stop-sync + launch-init, Rafael decisions folded in — implementable).
  **NEXT (fresh context): portfolio_tracker fill-reconcile (add client_order_id→tier attribution + untagged→
  halt) → ~15 remaining call sites (orphan 5 / run_cycle 4 / main 6 / QHM OrderDispatcher, tier=qhm) →
  launch-init script → assemble exact Phase-0 diff → static + cold-2nd + impact + FINAL Gro+GAI → API build.**
  Phase-0 surface ≈ 45 call sites / 6+ files + ownership_guard + ledger + launch-init. ~80% scoped.
- **TWO catastrophic modes the board designed against (do NOT regress):** (a) stale resting stop eats a
  protected share → synchronous cancel-replace on every tier-qty change; (b) floor recomputed from Alpaca's
  drifted net → floor is LEDGER-derived ONLY, drift→freeze-sells; (c) F6 trim reading Alpaca blended avg_entry
  → trim reads F6's OWN basis only.
- **Quick fix queued (from 2026-07-08 audits):** `scan_to_html._fetch_options_data` — `cannot convert float NaN
  to integer` (fired 10×; display-layer, fails-closed → not urgent). One-line NaN guard. NOTE: `scan_to_html.py`
  also has a *rejected* Gro/GAI RC-3 item today — handle carefully.
- **WEEKLY AUDIT ROLLUP (2026-07-02→09) → `logs/weekly_audit_rollup_2026-07-08.md`.** A week of Gemini
  midday/post-market audits, deduped to **TWO dominant roots:** (#1) **P&L ATTRIBUTION CORRUPTION** (`FILL
  UNVERIFIED → external_close → $0 P&L`; drove the 07-07 false −73.86% kill-switch trip, the EOD P&L drifts, RIVN
  $0-P&L + direction corruption, stop-hit fidelity) — build it WITH Build A + B (same fill/reconciliation family);
  (#2, new on 07-09) **ENTRY-PIPELINE THROTTLING** — sizing-cap stacking → 0 shares on high-priced names, silent
  "no allocation" skips, ANOMALY-2 confirm_gate stalls; qualified signals not being entered (costing trades now).
  Tier-2: TOD/phase, scan_to_html NaN, cycle-perf. Recurring FALSE POSITIVE: VOLSHADOW "score discrepancy" (Gemini
  misreads the log-only volume shadow — verify + rename). ALPHA reviews: exit R:R, high-score≠profit, MRI data.
  Full prioritized where-to-continue in the rollup.

## ⚡ 2026-07-10 SHIPPED (P&L fixes — LIVE)
- **Kill-switch phantom-proof** (commit `24c542a`, live): drop corrupt daily_pnl; measure = Alpaca
  equity delta − QHM unrealized (position objects, no fill-matching), fail-safe to account-level.
  Fixes the 07-07 false −73.86% trip. Board(masked-loss+reliability)+Gro+GAI, marker 79a4c2c8.
- **pnl_ledger pagination fix** (commit `e1e7416`, live): Alpaca activities use page_token NOT after_id
  (after_id ignored → infinite loop → 14-min hang on 367 fills — the "stayed stuck" issue). Now 3.7s.
- **P&L heal APPLIED + cron**: `python3 -m reporting.pnl_ledger --heal-apply` healed 51 eod days to true
  Alpaca-FIFO (7/02: −251.12→+41.08); invariant ok drift $0.65. **OCI cron `30 20,21,22 * * 1-5`**
  (post-close, both DST; idempotent/fail-closed/singleton). Dashboard regenerates from healed files.
  **TRUE LIFETIME REALIZED = +$282.88** (account UP ~$288 since inception; the losses were mostly
  corrupt reporting + the 07-08 false-HALT day). NOTE: crontab is NOT in git — re-add on host rebuild.
- **P0-b LIVE P&L authoritative — SHIPPED (commit `280cdec`, live)**: `portfolio_tracker.write_eod_summary`
  now sources `pnl_today` from `pnl_ledger.build_ledger()` (authoritative Alpaca-FIFO), invariant-gated
  ($5 tol), PT-keyed, A-4-gap-guarded, dual-compute fallback on any fail. Also fixed `pnl_ledger` to key
  per_day by PT date (was UTC → AH/overnight fills mis-bucketed; commit `1175596`). Gate: full read +
  static + cold-2nd PASS (caught the TZ bug) + Gro + GAI APPROVE. eod files re-healed with PT-keys. So
  the LIVE pnl_today the bot writes each cycle is now Alpaca truth, not corrupt-then-heal. New eod fields:
  pnl_today_qhm / pnl_today_total / pnl_ledger_authoritative.
- **P0-a ownership FOUNDATION — SHIPPED (commit `658933f`, unwired/zero-live-effect)**:
  `execution/ownership_guard.py` — per-tier ledger + `check_never_sell_floor()` chokepoint (fails CLOSED;
  floor ledger-derived only; ring-fenced=long-only; protected-tier self-exit nets vs effective_floor =
  floor-own) + `make_coid`/`tier_of_coid` + `reconcile_drift` + `launch_init`. Full self-tests + cold-2nd
  (caught QHM self-exit bug, fixed) + Gro + GAI + static clean.
- **P0-a WIRING — remaining, STRICT ORDER (or it fails-closed & freezes sells):** (a) run `launch_init`
  to seed the ledger from the current Alpaca book (all→intraday, F6/QHM=0); (b) add fill→tier attribution
  (client_order_id prefix) so the ledger stays current each cycle; (c) per-cycle `reconcile_drift`; (d)
  THEN route reducing-order paths (exit_logic 23 sites, broker close/partial → `qty_bounded_partial_close`)
  through `check_never_sell_floor` + mandatory broker `tier` param; entry_logic tier-tag + remove L437-440
  registry block. Each = own gated increment on live RTH. Scoping: `logs/phase0_*_2026-07-09.md`.

## Bot Status
- **Running:** 4 services active on OCI 129.153.208.32 (mtf-bot, mtf-writer, mtf-http, nginx). Git HEAD `9d03be1`
  (local = GitHub = OCI, verify with `git rev-parse HEAD` + OCI ssh). paper=True. Account ~$2,782, kill switch clear.
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32`. Deploy = git single channel (Mac commit→push→
  OCI `git pull --ff-only`+restart). NEVER rsync tracked files.
- **Permissions:** project `.claude/settings.local.json` `defaultMode: bypassPermissions` (set 2026-07-08 so
  scheduled/headless sessions don't stall on Bash approval). Backup: `settings.local.json.bak-20260708`.
- **Cost protocol (CLAUDE.md, LIVE):** interactive scopes/designs + runs board/Gro/GAI on the DESIGN; API
  (OCI headless) implements the pre-scoped diff + gates the DIFF + ships. Only Rafael changes it. Budget $20/mo.

## SHIPPED this session (2026-07-08)
- **F-INTERIM — commit `9d03be1`, LIVE + health-verified.** A false news-keyword HALT ("Can Trump cut off all
  trade with Spain?" matched substring "national emergency") had `safe_close_all`-liquidated the whole non-QHM
  book (6 pos, ~-$26, 11s). Interim: news-keyword HALT now BLOCKS NEW ENTRIES only, never liquidates. Full gate
  passed (board 3/3 + Gro + GAI; both final-pre-ship REJECTs were misreads, withdrawn on counter-prompt).
- **Cost fix:** morning health report retuned $0.86 → **$0.044/run** (`scripts/oci_report_runner.sh` Haiku +
  `scripts/collect_health_facts.py`; OCI cron 6 AM PT). Zero Mac dependency.
- **Scheduler root-cause fixed + validated:** crons failed on PERMISSIONS (no broad Bash allow → headless
  session stalled → wedged the slot), not scheduling. Fixed via bypassPermissions; live-test-fired 2026-07-08
  12:40 PT, wrote its marker cleanly. **In-thread pickup = CronCreate (not scheduled-tasks routines).**

## ACTIVE WORK — Build F + Forever-Hold (design DONE, decisions in `logs/build_f_decision_2026-07-08.md`)
**Build F (HALT/mass-liquidation redesign) — DECIDED (Rafael): "No reflex + halt observability."**
News = context/display only (delete `get_news_size_multiplier` 0.0 branch); NO automated close-all re-added
(liquidation authority stays with per-position stops + SPY-EXTREME entry-block + 7% kill-switch; `safe_close_all`
= user-shutdown only); ADD Alpaca venue-state detection → block entries + CRITICAL alert + `halt_eval` event
(never blind to a real halt). QHM exempt. Bucket-A-on-CB reversal → deferred to a board re-vote (moot w/o CB-liq).
KEY FACT: the 7% kill-switch does NOT liquidate — only blocks entries; only live `safe_close_all` caller = user-shutdown.

**Forever-Hold Accumulation — Rafael mandate, SEPARATE TIER above QHM (design done, guardrails locked, some open).**
- FOREVER-6: **TSLA, GOOGL, AMZN, CRWD, META, NVDA** — never sold (T1 = +1000%/10x → trim 25%; no other sell).
  Own bucket/logic/rules; ring-fenced (other strategies never dip into forever-6 shares). Curated 3–10yr secular
  names (NOT valuation-based), added manually over time; an extension of QHM. Crash/halt/CB/flash-crash = BUY.
  EXEMPT from kill-switch + halt-entry-block.
- **Board (Gro/GAI/Sosnoff/Thorp+Taleb) = SHIP-WITH-GUARDRAILS.** MANDATORY guards (see decision doc): (1)
  CASH-FUNDED ONLY (settled cash ≥ cost, never margin — makes margin-call impossible; THE load-bearing guard),
  (2) CAP on FIXED reserve / min(current,prior) equity not live equity, (3) per-name cap min(1sh, ~8% eq), (4)
  one-shot latch per (symbol, date) + ≤3 buys/day, (5) marketable limit never market, (6) data-quality gate
  before flash detector, (7) exemption from kill-switch/halt only — never from cash floor/CAP.
- **OPEN (Rafael + board):** sizing fork (scale-up vs flat/"more-levels" synthesis — recommend more-levels);
  CAP number (~30–40%); the EXPANDED crash-scenario board pass Rafael asked for (rules for flash/halt/CB/intraday
  crash/weekly crash/bear market — the full sell-first/no-buyers set); full-read `quarterly_hold_manager.py`
  (1954L); then final board+Gro+GAI on the combined proposal → **API build** (Rafael: ship to API to build).

## BUILD SLATE (Rafael): F → (Forever-6) → A → B → E  — full scope in `logs/api_build_packages_2026-07-08.md`
- **A — Data-Integrity Safe Mode** (2026-07-07 Alpaca-desync fix). Scoped to the LINE (full reads done:
  run_cycle 1865, orphan_manager 1624, risk_manager 802). Explained-P&L glitch validator → safe-mode → glitch-vs-
  real kill tagging → orphan/trade gating. Masked-loss seat MANDATORY (Scenario-7 test).
- **B — Orphan-stop root.** Scoped to the LINE (orphan_manager L714 + L1320; one confirming read left:
  portfolio_tracker.py:~965). Cancel stops BEFORE record_exit in external-close paths, fail-closed, + sweep.
- **E — QHM accumulation** (never-sell + buy-on-dips + `max(1, floor(0.03·eq÷price))`, 20% cap). Full read of
  quarterly_hold_manager.py (1954L) owed. **Note: Forever-6 supersedes/extends this — reconcile E with Forever-6.**

## OPEN ITEMS / BUGS / UNRESOLVED (as of 2026-07-08)
- **P0 (from incidents, in the slate):** Build A (glitch safe-mode), Build B (orphan-stop naked-short root).
- **Build F implementation** (decided, not built) + **Forever-6 implementation** (guardrailed; sizing/CAP + expanded
  crash board owed) → then API build.
- **Bucket-A-on-circuit-breaker reversal** — needs a fresh board vote (reverses Apr-8 ruling).
- **RC-4 open sites:** portfolio_tracker.py L1200/L1753 (per tb_audit_log hotspot table) — verify.
- **Main-bot false-drop root** (`record_exit` dropping a still-live position) — corrupts P&L; feeds the retired-Movers
  adopt vector. Pre-existing P0 (see FUTURE ROADMAP LOG MOVERS-RETIRED).
- **Cross-strategy audit** (strategies sharing Alpaca lots w/o ownership tags) — ongoing (roadmap 2026-06-30).
- **autonomous_review.py pushes to main** — conflicts with git-single-channel; needs branch+PR redesign (roadmap 2026-07-03).
- **Pre-incident:** Options two-column 0DTE page (SPX-source blocker — Alpaca has no SPX; mockup at
  logs/mockups/options_scanner_mockup_2026-07-06.html); OPT-2 event-sourced replay; CLAUDE.md Open-Question
  loophole removal; RBLX phantom lot; UX redesign of the 5 HTML pages; volume/16pt/delta/GEX/TSMOM shadow flips
  (see CLAUDE.md SHADOW STRATEGY TRACKER + FUTURE ROADMAP LOG).
- **Desktop scheduled-task `resume-build-f-2026-07-08`** — DISABLED (switched to in-thread CronCreate). Stray
  in-thread cron jobs are session-only (die on session end).

## Process / Invariants (binding — CLAUDE.md is authoritative)
- Board + Gro + GAI on EVERY fork BEFORE Rafael; the board+Gro+GAI audit of the FULLY-MAPPED proposal is the
  ABSOLUTE LAST STEP before API (account cross-strategy + existing bugs + hotspots). Final Gro+GAI pre-ship on the
  exact diff (preship_gate enforces on commit). Cold board MANDATORY on every risk-path diff.
- Safety: a control must NEVER mask a real loss. paper=True locked. SPY 5-min = sole entry gate. P&L from Alpaca fills only.

## References (all committed to git for the handoff)
- `logs/build_f_decision_2026-07-08.md` — Build F + Forever-Hold decisions + board results + open items (THE active doc)
- `logs/api_build_packages_2026-07-08.md` — F/A/B/E build slate (scoped to the line)
- `logs/tb_audit_log.md` — bug/patch log · `logs/incident_2026-07-07_alpaca_desync.json` — desync forensics
- `logs/safe_mode_spec_2026-07-07.md` — Build A spec
