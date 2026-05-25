# Decomposition Plan v3 — alpaca-mtf-bot
**Generated:** 2026-05-06 S9 (cron session) | **Status:** AUDIT ONLY — no code changes proposed
**Scope:** All files >800L confirmed via `wc -l` on OCI (May 6 2026)
**Rule:** This document is a planning deliverable. No decomp may begin without user approval in a dedicated session.

---

## Files Audited This Session

| File | Lines | RC Pass/Fail | Decomp Priority | Status |
|------|-------|--------------|-----------------|--------|
| execution/trade_engine.py | 3751 | Partial flags (see below) | PHASE 1–4 staged | ✅ Full audit complete |
| execution/portfolio_tracker.py | 1368 | RC-3 FLAG, RC-4 FLAG | Extract FIFO first | ✅ Full audit complete |
| execution/orphan_manager.py | 1199 | RC-2/3/5/6/7/8 FLAGS | Extract gtc_guards first | ✅ Full audit complete |
| strategy/run_cycle.py | 1536 | ALL PASS | Extract data_writers first | ✅ Full audit complete |

## Files Requiring Future Decomp Audit (>800L, not yet audited)

| File | Est. Lines | Priority | Reason deferred |
|------|------------|----------|-----------------|
| scan_to_html.py | ~2547 | P2 | Second agent diverged — reschedule |
| options_scanner.py | ~1946 | P2 | Second agent diverged — reschedule |
| events/news_monitor.py | ~1807 | P2 | Second agent diverged — reschedule |
| weekly_review.py | ~1434 | P2 | Second agent diverged — reschedule |

---

## execution/trade_engine.py — 3751L

### Audit Findings (RC classes)

| RC | Line(s) | Description | Status |
|----|---------|-------------|--------|
| RC-1 | All datetime calls | Uses `datetime.now(ET)` with timezone throughout | PASS |
| RC-2 | 88 | `Path(__file__).resolve().parent.parent` for hybrid state | PASS |
| RC-3 | 220, 1041, 1719 | Silent exceptions — all have adjacent logging | PASS |
| RC-4 | 1516–1519 | Estimated exit price fallback — logged as fallback, acceptable | FLAGGED (logged) |
| RC-5 | 131–133 | Hybrid state: tmp file + `os.replace()` | PASS |
| RC-6 | 828, 1494, 1827, 3028 | `filled_avg_price` — correct Alpaca field | PASS |
| RC-7 | 1368–1375 | Int truncation with `max(int(raw), 1)` floor guard | PASS |
| RC-8 | 579–582, 1619–1625 | Buffers cleared on all gate blocks | PASS |

### Top 10 Audit Findings

| # | Line(s) | Severity | Finding |
|---|---------|----------|---------|
| 1 | 841 | HIGH | `send_slack_alert()` called but not imported — suppressed with `# noqa: F821`. Slack alert silently fails when fill price fetch errors. |
| 2 | 1318 | MEDIUM | VOTE-4 SPY 200d MA check: `_main._spy_200d_ma > 0` — if global is `None` at module load, runtime TypeError. |
| 3 | 351–363 | MEDIUM | TQI uses `qty` (original) for risk base but line 1696 uses `qty_remaining` in partial close — inconsistent R-multiple attribution on multi-tranche exits. |
| 4 | 1504–1513 | MEDIUM | Material fill gap (>0.05%) triggers re-fetch of bars + stop/target recalculation every cycle if gap persists — expensive bar fetch on hot path. |
| 5 | 2014 | LOW | CRITICAL-1 fix comment references "~line 2126" for GTC cancel but actual cancel is ~1964–2007 — documentation drift from refactoring. |
| 6 | 2394–2395 | MEDIUM | VIX-adjusted overnight breakeven: if `_be_mult=0` AND `ATR=0`, `_be_buf=0` — breakeven check silently skipped. No floor guard. |
| 7 | 2604–2610 | MEDIUM | Hard stop breach counter gate uses `_cur_bar_ts` equality, but `_cur_bar_ts` initialized at line 2311 and may be `None` if bar fetch fails — counter resets every cycle. |
| 8 | 2874–2879 | LOW | RTH reversal scan: `trade.get("entry_price")` accessed without None check — `entry_touched` comparison silently fails on legacy trades. |
| 9 | 3048 | MEDIUM | Overnight fill price fallback passes string `entry_time` to `_fetch_actual_fill_price` which expects Unix timestamp — fills API time filter breaks for legacy trades. |
| 10 | 3339 | HIGH | Extended-hours partial exit calculates `trail_stop` but never updates `trade["qty_remaining"]` in tracker — subsequent AH cycles may double-close same quantity. |

### Proposed Decomposition

| New module | Line range | Purpose | Key exports | Callers to update |
|------------|------------|---------|-------------|------------------|
| execution/state_persistence.py | 88, 117–176, 420–423 | Hybrid engine state save/load; confirm gate JSON I/O | `_save_hybrid_state`, `_load_hybrid_state`, `_write_confirm_gate_json` | `execute_entries` (line 1621) |
| execution/fvg_confluencer.py | 178–326 | FVG detection & multiplier logic | `_find_recent_fvgs`, `_score_fvg_for_signal`, `_compute_fvg_mult` | `execute_entries` (line 1326) |
| execution/trade_quality_index.py | 328–446 | TQI computation & partial TQI logging | `_compute_tqi`, `_record_partial_tqi`, `_record_tqi` | `check_partial_exits` (2149), `check_exits` (2713) |
| execution/entry_gates.py | 538–798 | Rule 1/2/3 (premarket red, gap-down settle, SPY direction), bucket routing, conviction gates, position limits | Gate decision helpers | `execute_entries` (entire function body) |
| execution/entry_sizing.py | 870–1416 | Price fetch, ATR/RVOL cache, earnings check, analyst gate, R:R gate, FVG mult, min-lot guard, Kelly sizing math | `compute_entry_size` | `execute_entries` sizing branch |
| execution/order_submission.py | 1481–1593 | Market order submit, fill price fetch, GTC stop placement, overnight tagging | `submit_entry_order` | `execute_entries` (lines 1484–1592) |
| execution/partial_exits.py | 1663–2190 | Partial tranche logic, trail stop ratchet, PDT-dynamic behavior, GTC limit partials | `check_partial_exits`, `_submit_gtc_limit_partial` | `run_cycle.py` callers |
| execution/exit_gates.py | 2255–3005 | Hard stop, reversal counter, thesis invalidation, target hits, breakeven promotion | `check_exits` core logic | `run_cycle.py` callers |
| strategy/fmp_earnings.py | 1028–1087 | Earnings calendar fetch, HTF evaluation, alignment check | `check_earnings_gate` | `execute_entries` (line 1031) |
| strategy/analyst_sentiment.py | 1089–1105 | Analyst sentiment gate | `check_analyst_gate` | `execute_entries` (line 1093) |

### Risk: What Breaks, What Needs Testing

**High-risk extractions:**
- `entry_gates.py` — 6+ gates × 30 symbols/scan = 180 gate evals/cycle. Behavioral regression risk on every PDT=0/1/2/3 × overnight=True/False permutation.
- `partial_exits.py` — Trail stop ratchet, GTC cancel/resubmit, `qty_remaining` mutation — state explosion risk if helpers don't sync tracker after extraction.
- `exit_gates.py` — Reversal counter decay, hard-out displacement, PDT branching — timing-sensitive multi-scan state machines.

**Circular import risk:** `entry_gates.py` and `exit_gates.py` both reference `_main.GateState`, `strategy.scoring`, and `execution.broker` — import graph must be verified before extraction to prevent circular deps.

**What must be tested after decomposition:**
- All 8+ gate decision paths in `execute_entries` with PDT=0/1/2/3 × overnight=True/False
- Partial tranche T1/T2/T3 closes with PDT-dynamic behavior + trail stop ratchet
- Hard stop 3-scan filter, reversal 6-scan RTH / 10-scan overnight gates, entry-price hard-out
- Fill price fallbacks: Alpaca order → fills API → bar close → entry_price (4-tier)
- Overnight bucket A same-day close block + thesis invalidation branching
- GTC stop resubmission after partial (CRITICAL-1 fix), external close detection (3-source fallback)

### Recommended Order

| Phase | Target | Lines | Risk | Rationale |
|-------|--------|-------|------|-----------|
| 1 | `state_persistence.py` + `fvg_confluencer.py` + `trade_quality_index.py` | 88, 117–446 | LOW | Pure movement — no behavioral change, no shared state with other modules |
| 2 | `entry_sizing.py` | 870–1416 | MEDIUM | Math + ATR cache; test all size multipliers (Kelly, earnings, FVG, vol cap, TQI demotion, TSMOM, overnight cap) |
| 3 | `entry_gates.py` | 538–798 | HIGH | Rule 1/2/3, bucket routing, conviction, PDT gate — end-to-end PDT=0/3 tests required |
| 4 | `partial_exits.py` + `exit_gates.py` | 1663–3005 | HIGHEST | After phases 1–3 stable; both rely on tracker state mutation & multi-scan counters |

---

## execution/portfolio_tracker.py — 1368L

### Audit Findings (RC classes)

| RC | Line(s) | Description | Status |
|----|---------|-------------|--------|
| RC-1 | All datetime calls | ET/PT timezone-aware throughout | PASS |
| RC-2 | All path constructions | `Path(__file__).resolve()` pattern | PASS |
| RC-3 | 83–84, 624–625 | Silent exception blocks — bare `pass` with no log | **FLAG** |
| RC-4 | 894 | `exit_price` not verified as actual fill before `record_exit()` | **FLAG** |
| RC-5 | 962–963 | `manual_audit.jsonl` append — non-atomic | FLAG (low risk) |
| RC-6 | N/A | Alpaca field names verified against API | PASS |
| RC-7 | N/A | No sizing math in this file | PASS |
| RC-8 | N/A | No confirm gate buffers | PASS |

### Top Audit Findings

| # | Line(s) | Severity | Finding |
|---|---------|----------|---------|
| 1 | 83–84 | MEDIUM | Silent `except` block — on FIFO state load failure, falls through with no log and empty FIFO. Bot starts blind to prior positions. |
| 2 | 624–625 | MEDIUM | Silent `except` in trade log flush — write failures are swallowed. Audit trail lost. |
| 3 | 894 | HIGH | `record_exit(exit_price=current_price)` — market price passed directly, not actual fill. RC-4 violation. Board-approved for hotspot review before patching. |
| 4 | 962–963 | LOW | `manual_audit.jsonl` open/append — not atomic. Race condition if two processes write simultaneously (main.py + orphan_manager.py can overlap). |

### Proposed Decomposition

| New module | Line range | Purpose | Key exports | Callers to update |
|------------|------------|---------|-------------|------------------|
| execution/models/trade.py | 1–80 | Trade dataclass / schema | `Trade`, `SIDE_LONG`, `SIDE_SHORT` | All files importing trade schema |
| execution/persistence/fifo.py | 132–209 | FIFO P&L logic, state save/load | `FIFOTracker`, `load_fifo_state`, `save_fifo_state` | `portfolio_tracker.py`, `risk_manager.py` |
| execution/persistence/state_io.py | 210–310 | Atomic JSON read/write helpers | `atomic_write_json`, `read_json_safe` | `portfolio_tracker.py`, `orphan_manager.py` |
| execution/eod/summarizer.py | 700–830 | EOD summary generation, CSV export | `generate_eod_summary` | `main.py` EOD hook |
| execution/stats/pdt_counter.py | 831–900 | PDT slot counting, window management | `PDTCounter`, `count_daytrades` | `main.py`, `risk_manager.py` |
| execution/stats/portfolio_stats.py | 900–1100 | Win rate, avg P&L, streak tracking | `PortfolioStats` | `generate_dashboard.py`, `weekly_review.py` |

### Risk: What Breaks, What Needs Testing

- FIFO extraction: `risk_manager.py` and `portfolio_tracker.py` both reference FIFO state — import path change requires both files to update simultaneously.
- EOD summarizer: `main.py` calls summary generation at 4:00 PM ET — must pass same arguments after extraction.
- PDT counter: `risk_manager.py` and `main.py` both increment PDT — shared state must remain singleton.
- `alerts.py` and `generate_dashboard.py` import tracker stats — all call sites must update imports.

### Recommended Order

1. **First:** Extract `persistence/fifo.py` (lines 132–209) — self-contained FIFO logic, no outbound state dependencies.
2. **Second:** Extract `persistence/state_io.py` — pure I/O helpers, used by FIFO and tracker.
3. **Third:** Extract `stats/pdt_counter.py` — used by both main.py and risk_manager.py; validate singleton behavior.
4. **Fourth:** Extract `eod/summarizer.py` + `stats/portfolio_stats.py` — display-only, lowest blast radius.
5. **Last:** Extract `models/trade.py` — schema change touches every file; defer until other modules are stable.

---

## execution/orphan_manager.py — 1199L

### Audit Findings (RC classes)

| RC | Line(s) | Description | Status |
|----|---------|-------------|--------|
| RC-1 | N/A | Datetime calls use ET timezone | PASS |
| RC-2 | 169, 195, 223–224 | State mutation without atomicity — concurrent access risk | **FLAG** |
| RC-3 | 373–375, 392–393, 520, 533 | Inconsistent save-points — exception handling drops state mid-reconcile | **FLAG** |
| RC-4 | N/A | No direct `record_exit()` calls | PASS |
| RC-5 | 134, 284, 442 | Broker API failures treated as pass-through — no save-point | **FLAG** |
| RC-6 | 167–177, 451–464 | Branching on incomplete state (missing keys treated as "unknown") | **FLAG** |
| RC-7 | 297–301 | `filled_qty` fallback to 1 if falsy — silent minimum | **FLAG** |
| RC-8 | 585–588, 669, 1039 | Implicit type/state assumptions on order objects | **FLAG** |

### Top Audit Findings

| # | Line(s) | Severity | Finding |
|---|---------|----------|---------|
| 1 | 101–556 | HIGH | `cancel_and_reconcile_gtc_stops()` — 455 lines, cyclomatic complexity=14. Single function handles 6 distinct decision trees: TOD phase check, GTF validity, cancel, resubmit, orphan adoption, external close. |
| 2 | 562–1199 | HIGH | `reconcile_positions()` — 637 lines, cyclomatic complexity=18. Handles direction mismatch, qty mismatch, unknown orphan adoption, GTC guard, position state sync in one monolithic function. |
| 3 | 297–301 | MEDIUM | `filled_qty = order.get("filled_qty") or 1` — falsy zero treated as 1. An order with 0 fills silently adopts 1 share, creating phantom position. |
| 4 | 169, 195, 223–224 | MEDIUM | State written mid-reconcile without atomic save-point. If process crashes between broker call and state write, orphan state is desynchronized. |
| 5 | 373–375, 392–393 | MEDIUM | Exception blocks catch broker errors but continue reconciliation loop — partially-reconciled state persists with no rollback. |

### Proposed Decomposition

| New module | Line range | Purpose | Key exports | Callers to update |
|------------|------------|---------|-------------|------------------|
| execution/orphan/gtc_guards.py | Extracted from 101–200 | GTF validity checks, TOD phase gating | `is_gtc_valid`, `check_tod_phase` | `cancel_and_reconcile_gtc_stops()` |
| execution/orphan/gtc_reconciler.py | 200–556 | GTC cancel + resubmit logic | `cancel_gtc_stop`, `resubmit_gtc_stop` | `orphan_manager.py` main function |
| execution/orphan/orphan_adoption.py | 562–700 | Unknown orphan adoption, direction resolution | `adopt_orphan_position` | `reconcile_positions()` |
| execution/orphan/external_close_handler.py | 700–850 | Externally closed position detection + tracker update | `handle_external_close` | `reconcile_positions()` |
| execution/orphan/direction_mismatch_handler.py | 850–970 | Direction mismatch resolution | `resolve_direction_mismatch` | `reconcile_positions()` |
| execution/orphan/qty_mismatch_handler.py | 970–1100 | Qty mismatch detection + partial fill adoption | `resolve_qty_mismatch` | `reconcile_positions()` |
| execution/orphan/position_state_syncer.py | 1100–1199 | Final position state write after reconcile | `sync_position_state` | `reconcile_positions()` |

### Risk: What Breaks, What Needs Testing

- State atomicity gap: if extraction splits state writes across modules, concurrent access by `main.py` and `orphan_manager.py` could corrupt broker sync state. Must introduce explicit lock.
- `filled_qty=1` silent minimum (RC-7): fixing before extraction ensures the bug is not hidden in a new module boundary.
- GTC cancel ordering (P1 DS Finding 5): `_cancel_open_gtc_orders` verification gap in trade_engine.py interacts with orphan_manager.py's cancel logic — both must be patched consistently.
- All 7 new modules import `execution.broker` — circular import risk if broker imports tracker which imports orphan_manager.

### Recommended Order

1. **First:** Extract `gtc_guards.py` (~25L, no external state) — safest start, validates extraction tooling.
2. **Second:** Extract `gtc_reconciler.py` — second simplest, well-bounded cancel/resubmit logic.
3. **Third:** `orphan_adoption.py` + `external_close_handler.py` — both stateless once broker client passed in.
4. **Fourth:** `direction_mismatch_handler.py` + `qty_mismatch_handler.py` — depend on adoption modules.
5. **Last:** `position_state_syncer.py` — touches state persistence; validate atomic write after all others pass tests.

---

## strategy/run_cycle.py — 1536L

### Audit Findings (RC classes)

| RC | Line(s) | Description | Status |
|----|---------|-------------|--------|
| RC-1 | All datetime calls | ET/PT timezone-aware throughout | PASS |
| RC-2 | All path constructions | Absolute path anchors confirmed | PASS |
| RC-3 | All except blocks | All log or re-raise | PASS |
| RC-4 | N/A | No direct `record_exit()` calls | PASS |
| RC-5 | All file writes | Atomic patterns confirmed | PASS |
| RC-6 | N/A | No direct Alpaca API field access | PASS |
| RC-7 | N/A | No sizing math | PASS |
| RC-8 | All confirm gate ops | Cleared on all gate block paths | PASS |

**All 8 RC classes: PASS** — run_cycle.py has the cleanest RC profile of all audited files.

### Top Audit Findings

| # | Line(s) | Severity | Finding |
|---|---------|----------|---------|
| 1 | 119–120, 163–168 | MEDIUM | Function-attribute state tracking (`run_cycle._xxx` globals) — 20+ module-level mutations per cycle. Difficult to test in isolation; state leaks between test runs. |
| 2 | 852–1025 | MEDIUM | Hybrid market engine — 5-level nesting, 174 lines, 7 major gates with 4 early returns. Hardest single block to reason about. |
| 3 | Multiple | LOW | 28 try/except blocks across 1536L — exception handling is comprehensive but makes flow tracing difficult. |
| 4 | 1409–1535 | LOW | Data writers section — 126L of file I/O that has no shared state with cycle logic; cleanest extraction candidate. |

### Proposed Decomposition

| New module | Line range | Purpose | Key exports | Callers to update |
|------------|------------|---------|-------------|------------------|
| strategy/market_gates.py | 538–851 | Pre-entry market condition gates (MRI, VIX, breadth, event calendar) | `check_market_gates` | `run_cycle()` gate section |
| strategy/hybrid_market_engine.py | 852–1025 | Hybrid intraday/swing/overnight state machine | `run_hybrid_engine` | `run_cycle()` engine call |
| strategy/signal_filters.py | 1026–1200 | Signal deduplication, sector filter, correlation gate | `apply_signal_filters` | `run_cycle()` filter section |
| strategy/position_monitors.py | 1200–1408 | Breakeven promotion, trail ratchet check, MRI exit gate | `run_position_monitors` | `run_cycle()` monitor section |
| strategy/data_writers.py | 1409–1535 | Bar write, state flush, dashboard data export | `flush_cycle_data` | `run_cycle()` tail |

### Risk: What Breaks, What Needs Testing

- Global mutation density: `run_cycle._xxx` function-attribute state must be migrated to an explicit `CycleState` dataclass if extracted. This is a prerequisite for all extractions, not an incidental.
- `main.py` imports `run_cycle` and calls it with a dozen arguments — function signature change requires main.py update simultaneously.
- `_lazy_import` pattern (`import main as _main`) used inside several sub-functions — circular import risk if any extracted module tries to import from `main.py`.
- `data_writers.py` — lowest risk extraction, but it writes to `data/state/` — atomic write contract must carry over.

### Recommended Order

1. **First:** Extract `data_writers.py` (lines 1409–1535) — no shared state with cycle logic, pure I/O. Validate atomic write behavior.
2. **Second:** Extract `signal_filters.py` (lines 1026–1200) — reads state but does not mutate it.
3. **Third:** Extract `market_gates.py` (lines 538–851) after introducing `CycleState` dataclass.
4. **Fourth:** Extract `position_monitors.py` — depends on tracker + state; requires `CycleState` first.
5. **Last:** Extract `hybrid_market_engine.py` — highest cyclomatic complexity; needs all other modules stable first.

---

## Cross-File Decomp Sequencing

### Overall Recommended Order (across all 4 files)

The safest global sequencing considers shared state dependencies:

1. **Shared infrastructure first:** `execution/persistence/state_io.py` (from portfolio_tracker) — atomic I/O helpers needed by all other modules.
2. **Isolated logic second:** `execution/persistence/fifo.py`, `execution/orphan/gtc_guards.py`, `strategy/data_writers.py` — no inbound state dependencies.
3. **Computation modules third:** `execution/trade_quality_index.py`, `execution/fvg_confluencer.py`, `strategy/signal_filters.py`.
4. **Gate/decision modules fourth:** `execution/entry_gates.py`, `strategy/market_gates.py`, `execution/orphan/gtc_reconciler.py`.
5. **Stateful execution modules last:** `execution/partial_exits.py`, `execution/exit_gates.py`, `execution/orphan/reconcile_handlers.py`, `strategy/hybrid_market_engine.py`.

### Pre-Decomp Prerequisite (all files)

Before any extraction begins, the following must be completed:
- [ ] **DS Finding 5 fix** (P1): `_cancel_open_gtc_orders` verification — interacts with both `trade_engine.py` and `orphan_manager.py` decomp
- [ ] **RC-3 fix** in `portfolio_tracker.py` (lines 83–84, 624–625) — silent exceptions must be remediated before state_io extraction
- [ ] **RC-4 fix** in `portfolio_tracker.py` (line 894) — actual fill price gate must be in place before FIFO is extracted
- [ ] **RC-7 fix** in `orphan_manager.py` (line 297–301) — `filled_qty=1` phantom must be fixed before orphan_adoption.py is created
- [ ] **Introduce `CycleState` dataclass** — prerequisite for run_cycle.py extractions (prevents function-attribute global mutation pattern from spreading into new modules)

---

## Summary Table

| File | Lines | RC Issues | Top Bug | Decomp Modules | Safe to Start? |
|------|-------|-----------|---------|----------------|----------------|
| trade_engine.py | 3751 | RC-4 (logged) | Line 3339: AH double-close risk | 10 modules, 4 phases | After DS Finding 5 fix |
| portfolio_tracker.py | 1368 | RC-3, RC-4 | Line 894: estimated exit price | 6 modules | After RC-3 + RC-4 fix |
| orphan_manager.py | 1199 | RC-2,3,5,6,7,8 | Lines 297–301: filled_qty=1 phantom | 7 modules | After RC-7 fix |
| run_cycle.py | 1536 | ALL PASS | Hybrid engine complexity (852–1025) | 5 modules | After CycleState introduced |

**Audit complete. No code changes proposed. Decomp requires user approval in a dedicated session.**
