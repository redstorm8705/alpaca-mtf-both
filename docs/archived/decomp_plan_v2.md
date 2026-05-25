# main.py Decomposition Plan — v2 (REVISED)
**Date:** 2026-04-30 | Incorporates DS + GAI audit feedback. Supersedes v1.

---

## DS vs GAI Conflict Resolution: BotState vs Per-Global Ownership

**DS:** Do NOT create a monolithic BotState — renames the problem without solving it. Decide per-global.
**GAI:** You MUST use a shared context object — Python namespace mechanics make cross-module reassignment silently wrong.
**Resolution (adopted):** Hybrid — per-global ownership decisions:

| Global category | Ownership strategy |
|----------------|--------------------|
| Compute-owned (set each cycle, read downstream) | Module-level in owning module. Other modules import the module, not the variable. |
| Gate state (mutated by multiple callers, reset daily) | `GateState` dataclass in `strategy/scoring.py`. Passed as parameter. |
| Cache/TTL state | Module-level in new cache module (`data/bar_cache.py`). Single writer. |
| Persistence/lifecycle state | `state/persistence.py`. Owns all JSON file writes. |
| Tracker, risk, kelly, mri | Already class instances. No change. |

---

## AST Analysis Results (Phase 0 — automated 2026-04-30)

- **Total lines:** 7,119
- **Total functions:** 38
- **Module-level globals:** 58
- **Functions with `global` declarations:** 10

### Functions with global declarations (critical path):
| Function | Globals declared |
|----------|-----------------|
| `main` | `_entry_confirm_buffer`, `_conviction_streak`, `_kill_switch_alerted`, `_last_daily_reset_date`, `_fill_fallback_count`, `_rth_day_stop_failure_counts`, `_halt_entries_for_session`, `_market_top_score`, `_market_top_zone_tier`, `_macro_regime_label`, `_macro_regime_tier`, `_ftd_combined_state`, `_ftd_min_score`, `_SHORTING_ENABLED`, `_last_cycle_complete` |
| `run_cycle` | `_spy_event_type`, `_kill_switch_alerted`, `_last_vix`, `_spy_200d_ma`, `_spy_200d_ma_date`, `_spy_risk_active`, `_spy_risk_direction`, `_spy_risk_magnitude`, `_spy_risk_scans_left`, `_spy_fetch_failures`, `_spy_last_close`, `_SHORTING_ENABLED`, `_last_breadth`, `_analyst_sentiment`, `_spy_52w_high`, `_last_eod_summary_date`, `_premarket_movers_cache`, `_last_weekly_review_spawn_date` |
| `execute_entries` | `_feed_age_history`, `_systemic_stale_alerted`, `_SHORTING_ENABLED` |
| `_load_hybrid_state` | `_hybrid_state_loaded`, `_spy_risk_active`, `_spy_risk_direction`, `_spy_risk_magnitude`, `_spy_risk_scans_left`, `_spy_event_type` |
| `_cycle_watchdog` | `_last_cycle_complete` |
| `_touch_cycle_ts` | `_last_cycle_complete` |
| `_safe_close_all` | `_halt_entries_for_session` |
| `_record_tqi` | `_tqi_history` |
| `_verify_shorting_live` | `_SHORTING_ENABLED` |
| `_fetch_actual_fill_price` | `_fill_fallback_count` |

### 10 Largest Functions:
| Function | Lines | Size |
|----------|-------|------|
| `run_cycle` | 3766–5134 | 1,368 lines |
| `execute_entries` | 603–1829 | 1,226 lines |
| `check_exits` | 2761–3548 | 787 lines |
| `main` | 6407–7115 | 708 lines |
| `check_partial_exits` | 1832–2317 | 485 lines |
| `_reconcile_positions` | 5488–5921 | 433 lines |
| `_cancel_and_reconcile_gtc_stops` | 5139–5485 | 346 lines |
| `_check_exits_extended_hours` | 5926–6169 | 243 lines |
| `_overnight_entry_check` | 6213–6402 | 189 lines |
| `_submit_rth_day_stops` | 2390–2506 | 116 lines |

---

## Blocker Resolutions

### DS Concern 1: Phase 0 inventory underscoped
Complete AST analysis run 2026-04-30. 58 module-level globals identified, 10 functions with `global` declarations. Full map in `logs/phase0_dependency_map.json`.

### DS Concern 2: "Bot stays live" during extraction
**Resolution (user-approved):** Extractions scheduled for weekend non-market hours only (Saturday 8 PM – Sunday 5 PM ET). Bot restarted after every extraction. Weekday trading continues on current code.

### DS Concern 3: Global strategy contradiction
Resolved by hybrid per-global table above. Decision made per-global in Phase 0.5 before any file moves.

### DS Concern 4: Extraction order
`lifecycle.py` should be extracted BEFORE `run_cycle.py` — adopted by user. Corrected order below.

### DS Concern 5: Full Read Gate undefined for multi-file extraction
**Adopted definition:** Declare pre-extraction read (source file lines) + post-extraction read (new module + reduced source). Both required. New files read in full immediately after writing, before syntax check or rsync.

---

## Revised Module Map

| Module | ~Lines | Owns |
|--------|--------|------|
| `main.py` | ~200 | `main()`, startup, arg parse only |
| `data/bar_cache.py` | ~100 | `_atr_cache`, TTL helpers |
| `state/persistence.py` | ~200 | All atomic JSON writes (confirm_gate, kill_switch_state, tqi_history, hybrid_state) |
| `strategy/scoring.py` | ~500 | `GateState` dataclass, `_rc8_clear_buffers()`, `_get_live_score()` |
| `events/handlers.py` | ~400 | SPY event handler, news halt, MRI callbacks |
| `monitoring/watchdog.py` | ~350 | Cycle watchdog thread, health heartbeat, daily reset |
| `execution/gtc_manager.py` | ~200 | GTC/DAY stop submission, pending_cancel guard, `_submit_rth_day_stops()` |
| `execution/lifecycle.py` | ~900 | `execute_entries()`, `check_exits()`, `check_partial_exits()`, `_apply_mri_breakeven_push()` |
| `execution/orphan_manager.py` | ~450 | `_reconcile_positions()`, `_cancel_and_reconcile_gtc_stops()` |
| `strategy/run_cycle.py` | ~700 | `run_cycle()` orchestrator |

---

## Phase 0 — Complete Dependency Map
**Method:** AST analysis (automated). Full output: `logs/phase0_dependency_map.json`.
**Gate:** DS + GAI sign off on completeness before Phase 1.

## Phase 0.5 — In-Place State Encapsulation (GAI recommendation, adopted)
1. Create `GateState` dataclass in-place in main.py — move `_entry_confirm_buffer`, `_conviction_streak` into it.
2. Convert `_last_vix` to parameter (only 2 readers per DS).
3. Move `_tqi_history` mutations into `KellySizer` as class attribute.
4. Route all JSON writes through helper functions (future `state/persistence.py`).
5. Deploy monolithic main.py with these changes to OCI. Run one full trading day.
6. Only proceed to Phase 2 if no regressions in 24h.

## Phase 1 — Architecture Board Vote
One 27-0 vote on: module map, per-global ownership decisions, extraction order, `GateState` interface.

## Phase 2 — Extraction Order (corrected)
1. `data/bar_cache.py` — leaf node, no dependencies
2. `state/persistence.py` — JSON writes isolated before logic moves
3. `monitoring/watchdog.py` — early per GAI, validates cross-module locking
4. `execution/gtc_manager.py` — depends on broker only
5. `events/handlers.py` — depends on bar_cache only
6. `strategy/scoring.py` — GateState moves here
7. `execution/lifecycle.py` — **BEFORE run_cycle.py** (DS recommendation, user-adopted)
8. `execution/orphan_manager.py` — depends on lifecycle, gtc_manager
9. `strategy/run_cycle.py` — imports all above, extracted last
10. `main.py` slim — ~200 line thin orchestrator

## Board Vote Protocol
- Phase 0 completion → 27-0
- Phase 0.5 completion → 27-0
- Phase 1 architecture → 27-0
- First extraction of each type → 27-0 (5 votes: data, state, monitoring, execution, strategy)
- Subsequent same-type extractions → DS + GAI audit only, no full board

## Circular Import Resolutions
| Risk | Resolution |
|------|-----------|
| `scoring.py` ↔ `lifecycle.py` | `_rc8_clear_buffers` lives in scoring. lifecycle imports scoring and calls it. One direction. |
| `watchdog.py` ↔ `run_cycle.py` | `_last_cycle_complete` moves into `GateState`. Both receive it as parameter. No cross-import. |
| `orphan_manager.py` ↔ `lifecycle.py` | orphan imports lifecycle. lifecycle does NOT import orphan. One direction. |

## Per-Extraction Protocol
Pre: declare line counts → DS+GAI audit → board vote (or delegate) → user approval
Write: new module → post-extraction read (declare lines) → update main.py imports
Validate: py_compile → --once paper run → RTH scan cycle → phase-transition test → state write test → watchdog armed
Deploy: rsync + restart OCI, monitor rest of weekend window

## MVT Before Overnight (DS + GAI)
1. `python3 -m py_compile` passes
2. `python3 main.py --once --profile paper` — no ImportError
3. One RTH scan cycle — no new CRITICAL logs
4. Phase-transition simulation (AH routing, EOD flatten)
5. `trade_events.jsonl`, `hybrid_state.json` write confirmed
6. Watchdog armed log present
