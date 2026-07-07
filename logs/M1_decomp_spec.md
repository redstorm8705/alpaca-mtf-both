# M1 Decomposition Spec — extract `fifo_pnl.py` from `portfolio_tracker.py`
**Created 2026-07-06 S-FIFO. Council-approved sequencing: A (mechanical-first, then OPT-2 separately), 4-0 unanimous (Beck/Kim + Harris/Peterffy/McKinney + Gro + GAI).**

This is a NEXT-SESSION execution spec. It is NOT yet started in code. Follow the full mandatory patch sequence (full read → 10-pt audit → board + Gro + GAI → static → cold agent → impact → propose → approve → final pre-ship Gro+GAI → ship → verify). Gro was at its Groq daily-token ceiling on 2026-07-06 — confirm Gro is reachable (reset) before starting, or get a Rafael waiver.

## Step 1 — MECHANICAL EXTRACT ONLY (this M1 ship). ZERO logic change.

### Functions to move (all module-level, currently in execution/portfolio_tracker.py), byte-for-byte:
- `_parse_alpaca_ts`            (~L144)
- `_fill_et_date`              (~L160)
- `_fetch_alpaca_fills_for_date`(~L176)
- `_fifo_reconstruct`          (~L252)  — keeps its lazy `from execution.quarterly_hold_manager import get_quarterly_hold_symbols`
- `_load_prior_day_lots`       (~L406)
- `_save_open_lots_state`      (~L453)  — now takes today_pnl/per_trade params (bridge fix 654d507)
- `_load_today_attribution`    (~L485)  — bridge fix + GAI numeric guard

### Shared dependencies these functions need (THE KEY WRINKLE — avoid a circular import):
`_fifo_reconstruct`/`_save_open_lots_state` etc. depend on: `_atomic_write`, `_BotEncoder`, and module constants `_ET`, `_PT`, `_SSL_CTX`, `_ALPACA_PAPER_BASE`, `_LOTS_STATE_FILE`, `_DRIFT_ALERT_FILE`, `logger`.
- `_atomic_write` + `_BotEncoder` are ALSO used by code that STAYS on the class (`_save_log` for TRADE_LOG_FILE, `write_eod_summary` for eod files, drift-alert writes).
- If `fifo_pnl.py` imports `_atomic_write` from `portfolio_tracker` AND `portfolio_tracker` imports the FIFO fns from `fifo_pnl`, that is a CIRCULAR import.
- **Resolution (recommended):** move `_atomic_write` + `_BotEncoder` (and the shared constants `_ET`/`_PT`/`_SSL_CTX`) into a tiny leaf module, e.g. `execution/state_io.py` (or put them at the top of `fifo_pnl.py` and have `portfolio_tracker` import them FROM `fifo_pnl` — one-directional). Either way the dependency graph must be a DAG: `portfolio_tracker → fifo_pnl → (state_io)`, never back.
- `_LOTS_STATE_FILE`/`_DRIFT_ALERT_FILE` are `_ROOT`-anchored paths — recompute `_ROOT` in the new module (`Path(__file__).parent.parent.resolve()`), do NOT import it back from portfolio_tracker.

### What STAYS on the PortfolioTracker class (do NOT move):
`record_entry`, `record_exit`, `record_partial_exit`, `promote_pending_to_active`, `write_eod_summary`, `get_stats`, `_load_log`/`_save_log`, all the `_unverified_exits`/patch_exit_pnl machinery, Phase 2a.5. `write_eod_summary` will now call the moved functions via `from execution.fifo_pnl import _fifo_reconstruct, _load_prior_day_lots, _save_open_lots_state, _load_today_attribution, _fetch_alpaca_fills_for_date`.

### Import swap in portfolio_tracker.py:
Replace the moved function DEFINITIONS with an import at top: `from execution.fifo_pnl import (...)`. Keep every call site identical (same names, same signatures).

## Step 2 — GOLDEN-DIFF PARITY VERIFICATION (the safety net; no test suite exists)
1. BEFORE extracting: capture a baseline by running the current EOD path on a representative day and saving `logs/eod_YYYY-MM-DD.json` + `data/state/open_lots_prior_day.json`.
2. After the mechanical extract: re-run `write_eod_summary()` on the SAME fills/day.
3. Diff: `jq -S . eod_baseline.json > b.json && jq -S . eod_new.json > n.json && diff b.json n.json`
   - EXPECT **zero diff** on: `pnl_today`, `alpaca_per_trade`, `alpaca_pnl`, `all_time_stats.total_pnl`, `trades`.
   - Any diff → extraction bug; fix before proposing.
4. Confirm each fill id appears exactly once in `alpaca_per_trade` on a 2nd same-day EOD call (the bridge-fix invariant must survive the move).

## Step 3 (SEPARATE later ship, NOT part of M1) — OPT-2 event-sourced replay
Only after M1 mechanical extract is live + 1–2 days of zero-drift eod data.
- Convert `_fifo_reconstruct` to recompute per_trade fresh each run by replaying ALL of today's fills against START-OF-DAY lots (decouple attribution from the `processed_fill_ids` lot-dedup).
- **HAZARD the board flagged (must design against):** a mid-day force-restart that replays fills against partially-processed start-of-day lots can reintroduce the 2026-06-27 duplicate-lot bug UNLESS the fill-ID checkpoint is perfect. Keep a fill-ID guard for LOT MUTATION even under replay. Full board + Gro + GAI on the OPT-2 diff.

## Context the next session needs
- The bridge fix (repeat-run FIFO attribution) shipped 2026-07-06 as commit `654d507`, live on OCI, HEALTH_OK. `_alpaca_pnl` is the day-total; `open_lots_prior_day.json` now carries `alpaca_today_pnl`/`alpaca_per_trade`.
- Gro 403 was a Cloudflare-1010 User-Agent ban on urllib — fix: send a browser `User-Agent` header (curl already works). This likely un-stalls the whole autonomous Groq pipeline (queue item 3).
- Preship gate: `.claude/preship/preship_audit.py <file>` (uses curl, sidesteps the UA-403) writes the marker; `preship_gate` blocks commit/push without it. `--waive-gro` needs Rafael's authorization.
- RBLX phantom short-lot in `open_lots_prior_day.json` = pre-existing orphan-seed artifact, queued one-time cleanup (item 2), NOT worsened by the bridge fix.
