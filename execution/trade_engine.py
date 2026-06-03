# ruff: noqa: E501, E701, E702, B023  — pre-existing long lines / inline semicolons / loop-closure patterns from main.py
"""
execution/trade_engine.py — Phase 2 Extraction 10

Owns all live trading logic extracted from main.py:
  - check_partial_exits()        — trailing stop / partial tranche logic
  - check_exits()                — hard stop / reversal exit logic
  - _check_exits_extended_hours() — AH/PM exit monitoring
  - All coupled helpers: TQI, hybrid state, gate functions

Entry logic extracted to execution/entry_logic.py (Phase 2 Extraction 11):
  - execute_entries()            — signal → order submission
  - _overnight_entry_check()     — overnight swing dry-run scanner
  - FVG helpers: _find_recent_fvgs, _score_fvg_for_signal, _compute_fvg_mult
  - _write_confirm_gate_json

All main.py module-level globals are accessed via lazy ``import main as _main``
inside the function bodies that need them. Functions that access no module globals
are left clean. Phase 3 will complete the decoupling.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from execution.broker import get_order
from execution.risk_manager import RiskManager
from execution.portfolio_tracker import PortfolioTracker

# ─── PHASE 2 DECOMPOSITION — Exit logic extracted to execution/exit_logic.py ──
# Re-exported so external callers (main.py, run_cycle.py) import from trade_engine
# without change. noqa: F401 is intentional — these names are re-exports.
from execution.exit_logic import (  # noqa: F401
    _compute_tqi,
    _record_partial_tqi,
    _record_tqi,
    check_partial_exits,
    _submit_gtc_limit_partial,
    _cancel_open_gtc_orders,
    check_exits,
    _pdt_htf_gate,
    _check_exits_extended_hours,
)

# ─── PHASE 2 DECOMPOSITION — Entry logic extracted to execution/entry_logic.py ──
# Re-exported so external callers (main.py, run_cycle.py) import from trade_engine
# without change. noqa: F401 is intentional — these names are re-exports.
from execution.entry_logic import (  # noqa: F401
    execute_entries,
    _overnight_entry_check,
    _find_recent_fvgs,
    _score_fvg_for_signal,
    _compute_fvg_mult,
    _write_confirm_gate_json,
)

if TYPE_CHECKING:
    pass  # TYPE_CHECKING guard kept for future use

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

# Hybrid engine state persistence — survives bot restarts during active events
_HYBRID_STATE_FILE = Path(__file__).resolve().parent.parent / "logs" / "hybrid_state.json"

def _should_flatten_eod() -> bool:
    """True if at or past 4:00 PM ET — flatten all intraday positions."""
    now_et = datetime.now(ET)
    total_mins = now_et.hour * 60 + now_et.minute
    eod_cutoff = 16 * 60   # 4:00 PM ET
    return total_mins >= eod_cutoff


def _too_early() -> bool:
    """True if before 8:45 AM ET — pre-market scanning starts at 8:45."""
    now_et = datetime.now(ET)
    total_mins = now_et.hour * 60 + now_et.minute
    return total_mins < (8 * 60 + 45)



# _get_tod_phase + _tod_size_multiplier extracted to execution/orphan_manager.py
# (Phase 2 Extraction 8)


# ─── HYBRID ENGINE STATE PERSISTENCE ────────────────────────────────────────
# Survives bot restarts during active EXTREME / BROAD events.
# Writes to logs/hybrid_state.json each time state changes.
# Reloaded once on the first run_cycle() call — ignored if from a prior day.
# (_HYBRID_STATE_FILE defined above in module header with correct parent.parent path)


def _save_hybrid_state() -> None:
    """Atomically persist hybrid engine globals to disk."""
    import main as _main  # lazy: avoids circular import at module load
    try:
        _HYBRID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "active":     _main._spy_risk_active,
            "direction":  _main._spy_risk_direction,
            "magnitude":  _main._spy_risk_magnitude,
            "scans_left": _main._spy_risk_scans_left,
            "event_type": _main._spy_event_type,
            "saved_at":   datetime.now(ET).isoformat(),
        }
        tmp = Path(str(_HYBRID_STATE_FILE) + ".tmp")
        with open(tmp, "w") as _f:
            json.dump(state, _f)
        os.replace(str(tmp), str(_HYBRID_STATE_FILE))
    except Exception as _e:
        logger.debug(f"Hybrid state save failed (non-critical): {_e}")


def _load_hybrid_state() -> None:
    """
    Restore hybrid engine globals from disk on first run_cycle() call.
    Guards:
      - File must exist and parse cleanly.
      - saved_at must be today's date (ET) — no carry-over from prior sessions.
      - scans_left must be > 0 — expired events are not restored.
    """
    import main as _main  # lazy: avoids circular import at module load
    # (globals accessed via _main: _hybrid_state_loaded, _spy_risk_active/direction/magnitude/scans_left, _spy_event_type)
    _main._hybrid_state_loaded = True  # set regardless — only attempt once
    if not _HYBRID_STATE_FILE.exists():
        return
    try:
        with open(_HYBRID_STATE_FILE) as _f:
            state = json.load(_f)
        saved_at   = state.get("saved_at", "")
        today_et   = datetime.now(ET).strftime("%Y-%m-%d")
        if not saved_at.startswith(today_et):
            logger.info("Hybrid state file is from a prior session — not restoring.")
            return
        scans_left = int(state.get("scans_left", 0))
        if scans_left <= 0:
            logger.info("Hybrid state file has scans_left=0 — event already expired, not restoring.")
            return
        _main._spy_risk_active    = bool(state.get("active", False))
        _main._spy_risk_direction = str(state.get("direction", ""))
        _main._spy_risk_magnitude = float(state.get("magnitude", 0.0))
        _main._spy_risk_scans_left = scans_left
        _main._spy_event_type     = str(state.get("event_type", ""))
        logger.warning(
            f"♻️  Hybrid engine state RESTORED from disk: "
            f"{_main._spy_event_type} [{_main._spy_risk_direction.upper()}] "
            f"SPY {_main._spy_risk_magnitude:+.2f}% | {_main._spy_risk_scans_left} scan(s) remaining. "
            f"(Bot restarted mid-event — prior risk state reloaded.)"
        )
    except Exception as _e:
        logger.warning(f"Hybrid state load failed — starting fresh: {_e}")


# ─── FVG CONFLUENCE LAYER — extracted to execution/entry_logic.py (Phase 2 Extraction 11) ───
# _find_recent_fvgs, _score_fvg_for_signal, _compute_fvg_mult, _write_confirm_gate_json
# Re-exported above via the entry_logic import block.


def _submit_rth_day_stops(tracker) -> None:
    """Shim — delegates to execution.gtc_manager (Phase 2 extraction).

    Called by run_cycle.py via `_main._submit_rth_day_stops(tracker)`.
    Must remain in trade_engine.py so the _main proxy resolves correctly.
    """
    from execution.gtc_manager import submit_rth_day_stops
    submit_rth_day_stops(tracker)




# ─── ENTRY EXECUTION — extracted to execution/entry_logic.py (Phase 2 Extraction 11) ───
# execute_entries() re-exported above via the entry_logic import block.



# ─── EXIT CHECKS ─────────────────────────────────────────────────────────────



# _submit_gtc_stop_close extracted to execution/lifecycle.py (Phase 2 Extraction 7).
# Imported at module level as: submit_gtc_stop_close as _submit_gtc_stop_close



# _get_live_score extracted to strategy/scoring.py (Phase 2 B2 fix).
# Imported at module level as: get_live_score as _get_live_score

# _fetch_actual_fill_price extracted to execution/fill_helpers.py (Phase 2).
# Shim kept for the ~20 call sites throughout this file — delegates unchanged.
# _fetch_actual_fill_price imported at module level above.


# _safe_close_all extracted to events/handlers.py (Phase 2).
# Imported at module level as: from events.handlers import safe_close_all as _safe_close_all
# All call sites below are unchanged.




# _apply_mri_breakeven_push extracted to execution/lifecycle.py (Phase 2 Extraction 7).
# Imported at module level as: apply_mri_breakeven_push as _apply_mri_breakeven_push


# ─── MAIN SCAN CYCLE ─────────────────────────────────────────────────────────

# run_cycle() extracted to strategy/run_cycle.py (Phase 2 Extraction 9).
# Imported above: from strategy.run_cycle import run_cycle


# ─── OVERNIGHT ENTRY FEATURE ─────────────────────────────────────────────────
# _overnight_entry_check extracted to execution/entry_logic.py (Phase 2 Extraction 11).
# Re-exported above via the entry_logic import block.

def _reconcile_pending_overnight_orders(tracker: PortfolioTracker, risk: "RiskManager"):
    """
    Check Alpaca for status of any pending_overnight orders.
    Filled  → promote_pending_to_active()
    Cancelled / expired → cancel_pending_entry()
    Called at startup and each RTH cycle (before run_scan).
    """
    pending = tracker.get_pending_overnight_entries()
    if not pending:
        return

    logger.info(f"Reconciling {len(pending)} pending overnight order(s)...")
    for symbol, trade in list(pending):
        order_id = trade.get("order_id")
        if not order_id:
            tracker.cancel_pending_entry(symbol)
            continue
        try:
            order  = get_order(order_id)
            if order is None:
                logger.warning(f"[{symbol}] Overnight order {order_id} not found — removing")
                tracker.cancel_pending_entry(symbol)
                continue
            status = str(getattr(order, "status", "")).lower()
            if "filled" in status:
                fill_price = float(getattr(order, "filled_avg_price", None) or trade["limit_price"])
                filled_qty = int(float(getattr(order, "filled_qty", 0) or 0))
                pdt_count    = tracker.get_rolling_day_trade_count()
                # S47d: Capture pre-promote status so register_open() fires exactly once
                # per genuine pending_overnight→open transition.
                # promote_pending_to_active() is idempotent (status guard): if already
                # promoted on a prior cycle, pre=="open" → gate fails → no double-increment.
                # CALL-ORDER INVARIANT: _reconcile_pending_overnight_orders runs BEFORE
                # sync_from_tracker() at startup (main.py startup sequence) — register_open()
                # increments from the base established by sync_from_tracker on prior startup.
                _pre_status  = trade.get("status", "")
                tracker.promote_pending_to_active(
                    symbol, fill_price,
                    filled_qty=filled_qty,
                    pdt_used=pdt_count,
                )
                _post_status = tracker.open_trades.get(symbol, {}).get("status", "")
                if _pre_status == "pending_overnight" and _post_status == "open":
                    risk.register_open()
                    logger.info(
                        "[%s] Overnight fill promoted — risk.register_open()"
                        " → open_positions=%d",
                        symbol, risk.open_positions,
                    )
                else:
                    if _pre_status == "pending_overnight" and _post_status != "open":
                        logger.warning(
                            "[%s] UNEXPECTED: promote post-status=%s"
                            " — register_open SKIPPED (possible open_positions undercount)",
                            symbol, _post_status,
                        )
                    else:
                        logger.debug(
                            "[%s] promote_pending_to_active no-op (pre=%s post=%s)"
                            " — register_open skipped",
                            symbol, _pre_status, _post_status,
                        )
            elif status in ("cancelled", "expired", "replaced"):
                logger.info(f"[{symbol}] Overnight order {status} — removing from tracker")
                tracker.cancel_pending_entry(symbol)
            else:
                logger.debug(f"[{symbol}] Overnight order still pending (status={status})")
        except Exception as e:
            logger.warning(f"[{symbol}] Could not check overnight order status: {e}")


# ─── MAIN ────────────────────────────────────────────────────────────────────

