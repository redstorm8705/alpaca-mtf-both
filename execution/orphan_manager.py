"""
execution/orphan_manager.py
Position reconciliation and GTC stop lifecycle — extracted from main.py Phase 2
(Extraction 8).

Owns:
  - get_tod_phase()                    Time-of-day phase classifier
  - tod_size_multiplier()              TOD-based position size multiplier
  - cancel_and_reconcile_gtc_stops()   Pre-market GTC stop reconciliation
  - reconcile_positions()              Startup orphan/size/direction mismatch
                                       reconciliation

Broker imports: cancel_order, get_open_position, get_open_orders,
                get_open_positions, get_order, submit_gtc_stop_order
                from execution.broker.
Data imports: fetch_bars from data.fetcher; calculate_atr from data.premarket.
Helper imports: fetch_actual_fill_price from execution.fill_helpers;
               get_live_score from strategy.scoring.
Alert imports: alert_gtc_failed, send_slack from alerts.
"""

import json as _json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import config

if TYPE_CHECKING:
    from execution.portfolio_tracker import PortfolioTracker
    from execution.risk_manager import RiskManager
from alerts import alert_gtc_failed, send_slack
from data.fetcher import fetch_bars
from data.premarket import calculate_atr
from execution.broker import (
    cancel_order,
    get_open_position,
    get_open_orders,
    get_open_positions,
    get_order,
    submit_gtc_stop_order,
)
from execution.fill_helpers import fetch_actual_fill_price
from execution.quarterly_hold_manager import get_quarterly_hold_symbols as _get_qhm_syms
from strategy.scoring import get_live_score

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")  # tracker entry_time convention (PT)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time-of-day phase utilities
# ---------------------------------------------------------------------------

def get_tod_phase(now_et=None) -> str:
    """
    Returns the current time-of-day phase for sizing/filtering decisions.
    Phases: opening | midday | power_hour | closed
    """
    if now_et is None:
        now_et = datetime.now(ET)
    mins = now_et.hour * 60 + now_et.minute

    market_open  = 9 * 60 + 30
    open_buffer  = market_open + config.TOD_MARKET_OPEN_BUFFER_MINS   # 10:00 AM
    midday_start = config.TOD_MIDDAY_START                             # 12:00 PM
    midday_end   = config.TOD_MIDDAY_END                               # 2:00 PM
    power_hour   = config.TOD_POWER_HOUR_START                         # 3:00 PM
    market_close = config.TOD_MARKET_CLOSE                             # 4:00 PM

    premarket_start = 8 * 60 + 45   # 8:45 AM ET

    if mins < premarket_start or mins >= market_close:
        return "closed"
    elif mins < market_open:
        return "premarket"     # 8:45–9:30 — pre-market scan only, no entries
    elif mins < open_buffer:
        return "opening"       # 9:30–10:00 — 30-min no-entry buffer
    elif midday_start <= mins < midday_end:
        return "midday"        # 12:00–2:00 — reduce size
    elif mins >= power_hour:
        return "power_hour"    # 3:00–4:00 — entries allowed to close
    else:
        return "normal"        # 10:00 AM–12:00 PM and 2:00–3:00 PM


def tod_size_multiplier(tod_phase: str) -> float:
    """Returns position size multiplier for the current TOD phase."""
    return {
        "premarket":  0.0,    # no entries pre-market
        "opening":    0.0,    # entry-blocked — defense-in-depth
        "normal":     1.0,
        "midday":     config.TOD_MIDDAY_SIZE_MULT,
        "power_hour": 1.0,
        "closed":     0.0,
    }.get(tod_phase, 1.0)


# ---------------------------------------------------------------------------
# GTC stop reconciliation (pre-market)
# ---------------------------------------------------------------------------

def cancel_and_reconcile_gtc_stops(
    tracker: "PortfolioTracker",
    risk: "Optional[RiskManager]" = None,
):
    """
    Called at startup AND at the start of every premarket cycle.

    For each open position that has a GTC stop order ID stored:
      1. Fetch the order from Alpaca.
      2. If FILLED  → GTC stop was triggered overnight. Close tracker record.
                       C-2: calls risk.register_close() to sync open_positions.
      3. If PENDING → Cancel it so it doesn't appear on L2 during RTH.
      4. If already CANCELLED/not found → Clear the stored ID.

    This ensures the tracker always reflects reality after an overnight gap.
    """
    # TB2: confirmed called at startup AND premarket — docstring accurate
    # Startup call: main() startup reconciliation block (~line 6194)
    # Premarket call: run_cycle() premarket phase (~line 3423)

    # ── QHM protected symbols — read from state file, not module variable ──
    # get_quarterly_hold_symbols() returns empty frozenset before QHM.__init__
    # runs (module-level set, populated only after instantiation). Reading the
    # state file directly avoids startup-order dependency.
    _qhm_protected: frozenset[str] = frozenset()
    _qhm_load_failed = False
    try:
        _qhm_state_path = (
            Path(__file__).resolve().parent.parent
            / "data" / "state" / "quarterly_holds.json"
        )
        if _qhm_state_path.exists():
            _raw = _json.loads(_qhm_state_path.read_text())
            _active_states = {
                "AWAITING_FILL", "ACTIVE", "PENDING_STOP_REPLACE", "PENDING_EXIT",
            }
            _qhm_protected = frozenset(
                sym for sym, pos in _raw.items()
                if isinstance(pos, dict) and pos.get("state") in _active_states
            )
    except Exception as _qhm_e:
        # FAIL-CLOSED (2026-07-01, board + Gro + GAI): the file EXISTS but is
        # unreadable/corrupt — we KNOW there may be QHM holds but cannot enumerate
        # them. Cancelling GTC stops now would strip protection from a QHM position
        # (naked-QHM capital risk >> double-stop transactional risk). Retain ALL
        # stops this cycle and escalate. A simply-ABSENT file does not raise, so this
        # branch fires only on a present-but-corrupt file.
        _qhm_load_failed = True
        logger.critical(
            "QHM state file UNREADABLE (%s) — GTC reconciliation FAIL-CLOSED: "
            "retaining ALL overnight GTC stops this cycle to avoid stripping a "
            "QHM position's protection. Manual review required.", _qhm_e,
        )
        try:
            send_slack(
                f":rotating_light: QHM state file unreadable in GTC reconcile — "
                f"FAIL-CLOSED, all GTC stops retained this cycle ({_qhm_e})."
            )
        except Exception as _ofc_e:
            logger.warning("fail-closed Slack alert failed: %s", _ofc_e)

    gtc_positions = tracker.get_overnight_gtc_positions()
    if not gtc_positions:
        return

    logger.info(
        f"GTC reconciliation: checking {len(gtc_positions)} "
        f"overnight stop order(s)."
    )

    for symbol, trade in gtc_positions:
        order_id = trade.get("gtc_stop_order_id")
        if not order_id:
            continue

        order = get_order(order_id)
        if order is None:
            # Order not found — may have been cancelled externally
            logger.warning(
                f"[{symbol}] GTC stop order {order_id} not found at Alpaca"
                f" — clearing stored ID."
            )
            tracker.clear_gtc_stop_order_id(symbol)
            continue

        status = str(order.status).lower()

        if "filled" in status:
            # GTC stop triggered overnight — close the tracker record
            fill_price = float(
                order.filled_avg_price or trade.get("entry_price", 0)
            )
            if not order.filled_avg_price:
                logger.critical(
                    f"[{symbol}] GTC overnight fill price UNVERIFIED — "
                    f"no filled_avg_price from Alpaca. Using entry_price "
                    f"${float(trade.get('entry_price', 0)):.2f}. "
                    f"Manual verification required."
                )
            logger.warning(
                f"[{symbol}] GTC stop TRIGGERED overnight @ "
                f"${fill_price:.2f} — closing tracker record."
            )
            # SF-01: pending_overnight entries that never had a limit fill
            # have entry_price=None.  record_exit() does (exit - entry) * qty
            # which throws TypeError on None, leaving the position stuck in
            # open_trades forever and risk.open_positions inflated.
            # Guard: patch entry_price to fill_price so record_exit cleans up
            # properly (PnL = $0 — better than a tracker desync).
            _live_trade = tracker.open_trades.get(symbol, {})
            if _live_trade.get("entry_price") is None:
                logger.critical(
                    f"[{symbol}] SF-01 GUARD: GTC stop fired on a PENDING "
                    f"entry (entry_price=None — limit order never filled). "
                    f"Patching entry_price={fill_price:.2f} so tracker "
                    f"closes cleanly. PnL recorded as $0.00 — review fill "
                    f"history manually."
                )
                tracker.open_trades[symbol]["entry_price"] = fill_price
            pnl = tracker.record_gtc_triggered(symbol, fill_price)
            if risk is not None:                                  # C-2
                risk.register_close(pnl or 0.0)

        elif "pending_cancel" in status:
            # GTC-PENDING-CANCEL-FIX (2026-04-30) — board conditions applied:
            # Alpaca SDK: PENDING_CANCEL="pending_cancel", CANCELED="canceled"
            # — only 2 cancel states. "pending_cancel" contains "cancel" so
            # the old elif below would match and incorrectly clear the stored
            # ID → orphan adoption immediately tries new GTC stop →
            # held_for_orders death spiral. Must check pending_cancel FIRST.
            # AB+TB board condition: 1-cycle timeout (not 2). On first
            # detection, immediately clear order ID + fire Slack CRITICAL.
            # Orphan adoption in reconcile_positions() will attempt a fresh
            # GTC stop at the next premarket cycle (submits when ID is None).
            # Schneier persistence risk mitigated: counter reset to 0 at
            # session start (see main()). Board vote: 27-0 YES (2026-04-30).
            _pc_cycles = trade.get("gtc_pending_cancel_cycles", 0) + 1
            tracker.open_trades[symbol]["gtc_pending_cancel_cycles"] = _pc_cycles
            logger.critical(
                f"[{symbol}] GTC stop {order_id} is PENDING_CANCEL "
                f"(detected cycle {_pc_cycles}) — retaining ID, waiting "
                f"for Alpaca cancellation to propagate."
            )
            # GAI-FIX-2026-05-11: Do NOT clear gtc_stop_order_id here.
            # Clearing it evicts this trade from get_overnight_gtc_positions()
            # on the next cycle, making _pc_cycles permanently 1 and cutting
            # off all paths to GTC protection. Patch 1 and orphan adoption
            # both gate on gtc_stop_order_id is None — retaining the ID
            # naturally blocks same-cycle resubmission without any extra flag.
            if _pc_cycles >= 3:
                logger.critical(
                    f"[{symbol}] GTC stop PENDING_CANCEL persisting "
                    f"{_pc_cycles} cycles — manual Alpaca review required."
                )
            _pca_status = (
                "⚠️ ESCALATED: PENDING_CANCEL persisting "
                f"{_pc_cycles} cycles — manual Alpaca review required."
                if _pc_cycles >= 3
                else "Waiting for broker cancellation to propagate."
            )
            tracker._save_log()
            try:
                send_slack(
                    f":rotating_light: *GTC STOP PENDING_CANCEL*"
                    f" — {symbol}\n"
                    f"Order `{order_id}` in PENDING_CANCEL state "
                    f"(cycle {_pc_cycles}).\n"
                    f"{_pca_status}\n"
                    f"Verify position protection in Alpaca immediately."
                )
            except Exception as _pca_e:
                logger.error(
                    f"[{symbol}] pending_cancel Slack alert failed: "
                    f"{_pca_e}"
                )

        elif "cancel" in status or "expired" in status:
            # Fully CANCELED or expired — safe to clear. Also reset counter.
            logger.debug(
                f"[{symbol}] GTC stop already resolved ({status})"
                f" — clearing ID."
            )
            tracker.open_trades[symbol].pop("gtc_pending_cancel_cycles", None)
            tracker.clear_gtc_stop_order_id(symbol)

        else:
            # Order is still live (new/accepted/pending). Behavior is
            # phase-gated (S58 board: Peterffy + Kim):
            #   premarket/RTH → cancel unconditionally. GTC stops hold all
            #     shares in "held_for_orders", blocking partial closes and any
            #     exits that aren't a full market order against the locked qty.
            #   closed (overnight/AH/weekend) → ADOPT the live stop when its
            #     parameters still match tracker intent. The old unconditional
            #     cancel here + Patch 1's emergency resubmit (same pass, phase
            #     "closed") created a cancel/resubmit pair on EVERY overnight
            #     restart — order-log churn with a nonzero protection gap.
            #     Mirrors the Apr-14 fix that closed the identical loop for
            #     the premarket phase.

            # QHM symbols retain their protective GTC stops — do not cancel.
            # FAIL-CLOSED: if the QHM state file was unreadable, we cannot classify
            # any symbol, so retain ALL stops this cycle (board + Gro + GAI 2026-07-01).
            if symbol in _qhm_protected or _qhm_load_failed:
                _retain_reason = (
                    "QHM symbol" if symbol in _qhm_protected
                    else "QHM-load-failed fail-closed"
                )
                logger.info(
                    "[%s] %s — retaining GTC stop %s (not cancelling before RTH).",
                    symbol, _retain_reason, order_id,
                )
                continue

            if get_tod_phase() == "closed":
                # Idempotent adoption: order was fetched BY the stored ID, so
                # identity is established — only check the params are current.
                # Stop comparator matches submission source (trail_stop or
                # stop); qty comparator mirrors Patch 1's qty_remaining
                # semantics (0 after full partial exit is respected, None
                # falls back to original qty). Any parse failure → _matches
                # stays False → conservative cancel + Patch 1 resubmit.
                _adopt_stop = trade.get("trail_stop") or trade.get("stop")
                _qty_rem_raw = trade.get("qty_remaining")
                _adopt_qty = abs(int(
                    trade.get("qty", 0)
                    if _qty_rem_raw is None
                    else _qty_rem_raw
                ))
                # Side check (GAI integrity audit): protective side derived
                # from tracked direction — long protects with sell, short
                # with buy. Mismatch → cancel/resubmit (board 3-0 note: the
                # direction-mismatch handler in reconcile_positions() is the
                # authoritative fix for reversals; this is defense-in-depth).
                _adopt_side = (
                    "sell" if trade.get("direction") == "long" else "buy"
                )
                _matches = False
                try:
                    _matches = (
                        _adopt_stop is not None
                        and abs(
                            float(order.stop_price or 0) - float(_adopt_stop)
                        ) < 0.01
                        and abs(int(float(order.qty or 0))) == _adopt_qty
                        and _adopt_qty > 0
                        and str(getattr(order, "side", "")).lower().endswith(
                            _adopt_side
                        )
                    )
                except (TypeError, ValueError, AttributeError) as _adopt_err:
                    logger.warning(
                        "[%s] GTC adoption param check failed (%s) — "
                        "falling back to cancel/resubmit.",
                        symbol, _adopt_err,
                    )
                if _matches:
                    logger.info(
                        f"[{symbol}] GTC stop {order_id} adopted (idempotent "
                        f"restart): params current "
                        f"(${float(order.stop_price):.2f} x {_adopt_qty}) — "
                        f"no cancel/resubmit."
                    )
                    continue
                logger.info(
                    f"[{symbol}] GTC stop {order_id} params stale "
                    f"(tracker stop={_adopt_stop} qty={_adopt_qty} vs live "
                    f"stop={getattr(order, 'stop_price', '?')} "
                    f"qty={getattr(order, 'qty', '?')}) — cancelling; "
                    f"Patch 1 resubmits at current stop this pass."
                )

            logger.info(
                f"[{symbol}] Cancelling GTC stop {order_id} before RTH "
                f"(status={status})."
            )
            cancelled = cancel_order(order_id)
            if cancelled:
                tracker.clear_gtc_stop_order_id(symbol)
            else:
                logger.warning(
                    f"[{symbol}] Could not cancel GTC stop {order_id} — "
                    f"it may still be active on L2 and holding shares."
                )
                # Fire Slack alert — if the cancel failed, shares remain
                # held_for_orders and the bot cannot execute partial closes
                # or managed exits all session.
                try:
                    _trade_stop = trade.get("stop", 0)
                    _trade_dir  = trade.get("direction", "?")
                    send_slack(
                        f":warning: *GTC STOP CANCEL FAILED* — {symbol}\n"
                        f"Order `{order_id}` could not be cancelled before "
                        f"RTH.\n"
                        f"Direction: {_trade_dir.upper()} | "
                        f"Stop: ${_trade_stop:.2f}\n"
                        f"Shares are likely *held_for_orders* — partial "
                        f"closes will fail.\n"
                        f"*Cancel manually in Alpaca immediately.*"
                    )
                except Exception as _gca:
                    logger.error(
                        f"[{symbol}] GTC cancel-fail alert send failed: "
                        f"{_gca}"
                    )

    # ── Patch 2: Reconcile GTC partial order fills ───────────────────────────
    # For each open trade that has pending GTC partial orders (tranche exits
    # placed via GTC last session — historically deferred under PDT rules,
    # which were abolished S63; GTC tranche exits remain unconditional now),
    # check their fill status:
    #   FILLED    → decrement qty_remaining, advance profit_tranche_level
    #   CANCELLED → clear key (will be re-placed if price is still at level)
    #   LIVE      → cancel before RTH so normal scan manages partials cleanly
    _gtc_partial_changed = False
    for _sym, _trade in list(tracker.open_trades.items()):
        _partials = _trade.get("gtc_partial_order_ids")
        if not _partials:
            continue
        _qty_orig = _trade.get("qty", 0)
        for _tk, _oid in list(_partials.items()):
            if not _oid:
                continue
            _pord = get_order(_oid)
            if _pord is None:
                logger.warning(
                    f"[{_sym}] GTC partial {_tk} order {_oid} not found"
                    f" — clearing."
                )
                del _partials[_tk]
                _gtc_partial_changed = True
                continue
            _pstatus = str(_pord.status).lower()
            if "filled" in _pstatus:
                _fill_px  = float(_pord.filled_avg_price or 0)
                # Explicit None check — preserves 0 as "no fills yet".
                # Falsy `or` would short-circuit to order qty on zero-fill,
                # adopting the full order size as a phantom position.
                _filled_raw = getattr(_pord, "filled_qty", None)
                _fill_qty   = int(float(_filled_raw)) if _filled_raw is not None else 0
                if _fill_qty <= 0:
                    logger.warning(
                        "[%s] GTC partial %s status='%s' but filled_qty=0 — "
                        "data corruption or unexpected Alpaca state. "
                        "Removing stale key.",
                        _sym, _tk, _pstatus,
                    )
                    del _partials[_tk]
                    _gtc_partial_changed = True
                    continue
                _t_num    = int(_tk[1])  # "t1"→1, "t2"→2, "t3"→3

                # Guard: if profit_tranche_level already covers this tranche,
                # the fill was accounted for in a prior session by
                # check_partial_exits(). check_partial_exits() does NOT clear
                # gtc_partial_order_ids, so stale keys survive restarts and
                # would cause Patch 2 to double-bank pnl and double-decrement
                # qty_remaining. Just clean up the key and move on.
                _current_tl = _trade.get("profit_tranche_level", 0)
                if _current_tl >= _t_num:
                    logger.info(
                        f"[{_sym}] GTC partial {_tk} already accounted for"
                        f" (profit_tranche_level={_current_tl} >= t{_t_num})"
                        f" — clearing stale key, skipping pnl/qty update."
                    )
                    del _partials[_tk]
                    _gtc_partial_changed = True
                    continue

                # FIX: Calculate and bank P&L from the overnight partial fill.
                # Without this, partial profits are made in Alpaca but never
                # recorded in the tracker — corrupting Kelly sizing and win
                # rate.
                _entry_px = _trade.get("entry_price", 0)
                _dir      = _trade.get("direction", "long")
                _pnl      = (
                    (_fill_px - _entry_px) * _fill_qty if _dir == "long"
                    else (_entry_px - _fill_px) * _fill_qty
                )
                _trade["partial_pnl"] = round(
                    _trade.get("partial_pnl", 0.0) + _pnl, 2
                )
                _trade["partial_exit_price"] = _fill_px

                _old_rem  = _trade.get("qty_remaining", _qty_orig)
                _trade["qty_remaining"]        = max(0, _old_rem - _fill_qty)
                _trade["profit_tranche_level"] = max(
                    _trade.get("profit_tranche_level", 0), _t_num
                )
                _trade["partial_exited"] = True
                del _partials[_tk]
                _gtc_partial_changed = True
                logger.warning(
                    f"[{_sym}] GTC partial {_tk} FILLED overnight "
                    f"@ ${_fill_px:.2f} | qty {_fill_qty} "
                    f"| qty_remaining → {_trade['qty_remaining']} "
                    f"| profit_tranche_level → "
                    f"{_trade['profit_tranche_level']}"
                )
            elif "cancel" in _pstatus or "expired" in _pstatus:
                logger.debug(
                    f"[{_sym}] GTC partial {_tk} already {_pstatus}"
                    f" — clearing."
                )
                del _partials[_tk]
                _gtc_partial_changed = True
            else:
                # Still live (new/accepted/pending_new) — cancel before RTH
                logger.info(
                    f"[{_sym}] Cancelling GTC partial {_tk} order {_oid} "
                    f"before RTH (status={_pstatus})."
                )
                _p_cancelled = cancel_order(_oid)
                if _p_cancelled:
                    del _partials[_tk]
                    _gtc_partial_changed = True
                else:
                    logger.warning(
                        f"[{_sym}] Could not cancel GTC partial {_tk} "
                        f"order {_oid} — may still be live."
                    )
    if _gtc_partial_changed:
        tracker._save_log()
        logger.info("GTC partial reconciliation: tracker state saved.")

    # ── Patch 3: Clear stale rth_day_stop_order_id ───────────────────────────
    # DAY stop orders submitted at RTH session start cannot survive overnight.
    # Clear the stored ID at pre-market so _submit_rth_day_stops() can
    # resubmit fresh DAY stops for the new session if GTC stops were not
    # placed last AH.
    _rth_day_changed = False
    for _sym, _trade in list(tracker.open_trades.items()):
        if _trade.get("rth_day_stop_order_id"):
            logger.debug(
                f"[{_sym}] Clearing expired rth_day_stop_order_id at "
                f"pre-market (DAY orders expire at 4 PM — cannot be live "
                f"overnight)."
            )
            _trade["rth_day_stop_order_id"] = None
            _rth_day_changed = True
    if _rth_day_changed:
        tracker._save_log()

    # ── Patch 1: Emergency GTC stop for unprotected overnight positions ───────
    # If the bot restarted while market is closed and an overnight position
    # has no gtc_stop_order_id, the position is unprotected until next RTH.
    # Submit a GTC stop now so a gap-down cannot run past the configured stop.
    # FIX: Gate on "closed" phase only — NOT "premarket". During pre-market
    # this function already cancelled all GTC stops (above), so running this
    # block immediately re-submits them, creating an endless cancel/re-submit
    # loop every 10 min that burns rate limits and locks held_for_orders flags.
    if get_tod_phase() == "closed":
        # Historical note (Bug 6 fix, Apr 14 2026): this block originally added
        # a PDT=3/3 + opened_today guard to match the AH GTC loop, avoiding a
        # 40310100 rejection + false-alarm Slack alert for today-opened
        # PDT-limited positions. PDT enforcement was abolished S63 (SEC rule
        # change) and all PDT-counter code removed from the codebase — there
        # is no live PDT check here anymore, this comment is retained only as
        # historical context for why the surrounding structure exists.
        # OM-RACE-1 (2026-05-21 S28): batch-fetch all open orders once before the
        # loop. A just-cancelled GTC stop may still be PENDING_CANCEL on Alpaca's
        # backend for minutes to hours (AH/weekend propagation). Submitting a new
        # GTC while the old one settles causes 40310000 (held_for_orders). One
        # batch call avoids N serial calls inside the loop.
        # Alpaca status="open" includes pending_cancel and pending_new per API docs.
        _p1_open_orders: dict[str, list] = {}
        try:
            _all_open = get_open_orders() or []
            for _o in _all_open:
                _osym = getattr(_o, "symbol", None)
                if _osym:
                    _p1_open_orders.setdefault(_osym, []).append(_o)
        except Exception as _p1_batch_err:
            logger.warning(
                "Patch 1: batch get_open_orders failed (%s) — proceeding without "
                "PENDING_CANCEL guard. GTC submissions may fail with 40310000 if "
                "orders are still settling.",
                _p1_batch_err,
            )

        def _get_blocking_ids(orders: list) -> list[str]:
            """Return IDs of orders blocking new GTC submission.
            Empty list (falsy) means no blocking orders — safe to submit.
            """
            return [
                str(getattr(o, "id", "?")) for o in orders
                if getattr(o, "status", "").lower()
                in ("pending_cancel", "pending_new", "held")
            ]

        for _sym, _trade in list(tracker.open_trades.items()):
            if _trade.get("status") != "open":
                continue
            if _trade.get("gtc_stop_order_id"):
                continue   # already protected
            if _trade.get("internal_hard_stop_active"):
                logger.debug(
                    f"[{_sym}] Patch 1: skipping GTC re-submission — "
                    f"internal_hard_stop_active=True (persisted from prior run)."
                )
                continue
            # OM-RACE-1 guard. (Historical: originally placed after a PDT-defer
            # check that no longer exists post-S63 PDT removal — the ordering
            # rationale is moot, but the guard itself is still needed to avoid
            # incrementing the blocking counter incorrectly.)
            _p1_blocking_ids = _get_blocking_ids(_p1_open_orders.get(_sym, []))
            if _p1_blocking_ids:
                _p1_defer = _trade.get("gtc_p1_defer_cycles", 0) + 1
                _trade["gtc_p1_defer_cycles"] = _p1_defer
                try:
                    tracker._save_log()
                except Exception as _p1_sl_err:
                    logger.debug(
                        "[%s] OM-RACE-1: save_log failed after defer increment: %s",
                        _sym, _p1_sl_err,
                    )
                if _p1_defer >= 5:
                    _p1_alert_side = (
                        "sell" if _trade.get("direction") == "long" else "buy"
                    )
                    logger.critical(
                        "[%s] Patch 1: GTC stop blocked PENDING_CANCEL for %d "
                        "cycles (50+ min) — Alpaca backend may be wedged. "
                        "MANUAL REVIEW REQUIRED. ids=%s",
                        _sym, _p1_defer, _p1_blocking_ids,
                    )
                    alert_gtc_failed(
                        _sym, _p1_alert_side, float(_trade.get("stop", 0) or 0),
                        f"PENDING_CANCEL {_p1_defer} cycles — broker state stuck. "
                        f"Manual review required.",
                    )
                else:
                    logger.warning(
                        "[%s] Patch 1: GTC stop deferred — %d blocking order(s) "
                        "in PENDING_CANCEL/settling (cycle %d/5). Software stop "
                        "active. Retry next premarket cycle. ids=%s",
                        _sym, len(_p1_blocking_ids), _p1_defer, _p1_blocking_ids,
                    )
                continue
            else:
                # No blocking orders — reset stale defer counter if present
                if _trade.pop("gtc_p1_defer_cycles", None) is not None:
                    try:
                        tracker._save_log()
                    except Exception as _p1_sl_err:
                        logger.debug(
                            "[%s] OM-RACE-1: save_log failed after counter reset: %s",
                            _sym, _p1_sl_err,
                        )
            # Fix A (Apr 14 2026): Verify Alpaca holds this position before
            # submitting an emergency GTC stop. Patch 1 fires before
            # reconcile_positions(), so phantom tracker entries (position
            # already closed in Alpaca) would produce a rejected order and a
            # false-alarm Slack alert. Check first; if absent, close the
            # tracker record locally the same way reconcile_positions() would.
            try:
                _p1_alpaca_pos = get_open_position(_sym)
            except Exception as _p1_ap_err:
                _p1_alpaca_pos = True  # fail-open: assume exists if error
                logger.warning(
                    f"[{_sym}] Patch 1: Alpaca position check failed "
                    f"({_p1_ap_err}). Proceeding with GTC stop submission."
                )
            if _p1_alpaca_pos is None:
                logger.warning(
                    f"[{_sym}] Patch 1: position NOT in Alpaca — closing "
                    f"phantom tracker record instead of submitting emergency "
                    f"GTC stop."
                )
                _p1_exit_px = fetch_actual_fill_price(_sym, _trade,
                                                       poll_secs=0)
                _p1_pnl = tracker.record_exit(
                    _sym, _p1_exit_px,
                    reason="external_close_at_startup",
                    mri_level="UNKNOWN",
                )
                if risk is not None:
                    risk.register_close(_p1_pnl or 0.0)
                continue
            _stop_px   = _trade.get("stop")
            _direction = _trade.get("direction", "long")
            # Use qty_remaining when explicitly set (including 0 after a
            # partial exit); fall back to original qty only when the field is
            # absent (None). The old `or` treated qty_remaining=0 as falsy
            # and fell through to qty, causing oversized stops after Patch 2
            # set qty_remaining=0 on a partial fill.
            _qty_remaining_raw = _trade.get("qty_remaining")
            _qty_gtc = abs(int(
                _trade.get("qty", 0)
                if _qty_remaining_raw is None
                else _qty_remaining_raw
            ))
            if not _stop_px or _qty_gtc < 1:
                logger.critical(
                    f"[{_sym}] UNPROTECTED overnight position — no stop "
                    f"price or qty=0. Cannot submit emergency GTC stop. "
                    f"MANUAL REVIEW REQUIRED."
                )
                alert_gtc_failed(
                    _sym, "unknown", 0.0,
                    "No stop price or qty=0 in trade log — manual review "
                    "required",
                )
                continue
            try:
                _gtc_side = "sell" if _direction == "long" else "buy"
                # ── OM-BUG-1: Market-price guard ─────────────────────────────
                # Race window: price can move 50ms between fetch and submission
                # in AH. Guard reduces 42210000 failures ~90% → ~5%; residual
                # failures handled by alert + manual review. Fail-forward on
                # fetch failure.
                _p1_live_px = None
                try:
                    from data.alpaca_data import get_latest_trade as _p1_get_px
                    _p1_live_px = _p1_get_px(_sym)
                except Exception as _p1_px_err:
                    logger.debug(
                        f"[{_sym}] OM-BUG-1 price fetch failed, "
                        f"proceeding with submission: {_p1_px_err}"
                    )
                if _p1_live_px and _p1_live_px > 0:
                    _p1_invalid = (
                        (_gtc_side == "sell" and _stop_px >= _p1_live_px) or
                        (_gtc_side == "buy"  and _stop_px <= _p1_live_px)
                    )
                    if _p1_invalid:
                        logger.critical(
                            f"[{_sym}] OM-BUG-1 GUARD: stop ${_stop_px:.2f} "
                            f"invalid vs live ${_p1_live_px:.2f} ({_gtc_side})."
                            f" Skipping GTC — software stop active. "
                            f"MANUAL REVIEW REQUIRED."
                        )
                        _trade["internal_hard_stop_active"] = True
                        _trade["gtc_rejected_42210000"] = True
                        tracker._save_log()
                        alert_gtc_failed(
                            _sym, _gtc_side, _stop_px,
                            f"OM-BUG-1 price guard: stop=${_stop_px:.2f} "
                            f"invalid vs live=${_p1_live_px:.2f} — "
                            f"software stop active",
                        )
                        continue
                _em_order = submit_gtc_stop_order(
                    _sym, _qty_gtc, _gtc_side, _stop_px
                )
                if _em_order:
                    tracker.set_gtc_stop_order_id(_sym, str(_em_order.id))  # type: ignore[attr-defined]
                    _trade.pop("gtc_p1_defer_cycles", None)  # reset on success
                    tracker._save_log()
                    logger.warning(
                        f"[{_sym}] Emergency GTC stop submitted (Patch 1): "
                        f"{_qty_gtc} shares {_gtc_side} @ stop "
                        f"${_stop_px:.2f} | order {_em_order.id}"  # type: ignore[attr-defined]
                    )
                else:
                    logger.critical(
                        f"[{_sym}] Emergency GTC stop FAILED — position "
                        f"unprotected overnight."
                    )
                    alert_gtc_failed(
                        _sym, _gtc_side, _stop_px,
                        "Order rejected by broker (PDT no longer applicable "
                        "post-S63 — check Alpaca for the actual rejection reason)",
                    )
            except Exception as _gtc_em_err:
                logger.critical(
                    f"[{_sym}] Emergency GTC stop exception: {_gtc_em_err}"
                    f" — position unprotected overnight."
                )
                alert_gtc_failed(
                    _sym, _gtc_side, _stop_px, str(_gtc_em_err)[:120]
                )


def _fetch_fill_timestamp(symbol: str) -> str | None:
    """Recover actual entry fill timestamp from Alpaca closed orders.

    Corrects opened_today() for overnight orphans — historically prevented
    phantom PDT slot consumption on RTH closes of adopted positions (PDT
    removed S63; opened_today() is still used by other same-day-close logic,
    e.g. Bucket A, so the timestamp correction itself remains relevant).
    Returns ISO string in PT
    (matches tracker entry_time convention: portfolio_tracker.py stores
    entry_time as PT throughout). 14-day lookback covers holiday/extended-
    downtime restarts. Returns None on any failure — caller falls back to
    datetime.now(PT). Lazy-imports to avoid circular imports at module load.
    """
    try:
        from datetime import timedelta, timezone
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from execution.broker import get_trading_client
        _tc = get_trading_client()
        _req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[symbol],
            limit=10,
            after=datetime.now(tz=timezone.utc) - timedelta(days=14),
        )
        _ords = _tc.get_orders(filter=_req)
        for _o in sorted(
            _ords,
            key=lambda x: str(getattr(x, "filled_at", "") or ""),
            reverse=True,
        ):
            _fat = getattr(_o, "filled_at", None)
            if _fat is None:
                continue
            try:
                return _fat.astimezone(PT).isoformat()
            except (AttributeError, TypeError, ValueError):
                continue  # naive or non-datetime filled_at — try next order
        return None
    except Exception as _e:
        logger.debug(f"[{symbol}] _fetch_fill_timestamp failed: {_e}")
        return None


# ---------------------------------------------------------------------------
# Startup position reconciliation
# ---------------------------------------------------------------------------

def reconcile_positions(
    tracker: "PortfolioTracker",
    risk: "Optional[RiskManager]" = None,
    trade_mode: Optional[str] = None,
):
    """
    Called once at startup. Compares Alpaca's open positions to the tracker.

    Three discrepancy types:
      Orphaned (Alpaca has it, tracker doesn't):
        → Log CRITICAL. Mark symbol as traded today to block new entries.
          Do NOT auto-add to tracker — no stop/target data available.

      Externally closed (tracker has it, Alpaca doesn't):
        → Log WARNING. Record exit at tracker's entry price (best estimate).
          C-2: calls risk.register_close() to sync open_positions counter.

      Size mismatch (both have it but qty differs):
        → Log WARNING. Update tracker qty to match Alpaca's actual qty.

    P4-1: trade_mode passed so orphan positions can be re-scored via
    get_live_score() on adoption rather than defaulting to score=0.
    """
    _tm = trade_mode or getattr(  # type: ignore[union-attr]
        config, "TradeMode",
        type("_TM", (), {"INTRADAY": "intraday"})
    ).INTRADAY
    try:
        alpaca_positions = {p.symbol: p for p in get_open_positions()}
    except Exception as e:
        logger.warning(
            f"Position reconciliation skipped — could not fetch Alpaca "
            f"positions: {e}"
        )
        return

    tracker_symbols = set(tracker.open_trades.keys())
    alpaca_symbols  = set(alpaca_positions.keys())

    # ── Orphaned positions (in Alpaca, not in tracker) ──────────────────────
    # Adopt into tracker with Alpaca's entry price + direction.
    # No stop/target — reversal counter manages exit.
    # Historical: this comment previously described a PDT=3/3 condition for
    # marking overnight=True; PDT removed S63 — overnight=True is now set
    # unconditionally for every orphan-adopted position below.
    orphans = alpaca_symbols - tracker_symbols
    for sym in orphans:
        pos = alpaca_positions[sym]
        _entry_px  = float(pos.avg_entry_price)
        _raw_qty   = float(pos.qty)
        _qty       = abs(int(_raw_qty))
        _direction = "long" if _raw_qty > 0 else "short"
        # Change C: recover actual fill timestamp so opened_today() is correct
        # for overnight orphans (adoption time != entry time). Falls back to
        # datetime.now(PT) if Alpaca history is unavailable. PT matches the
        # tracker entry_time convention (portfolio_tracker.py lines 953+).
        _fill_ts = _fetch_fill_timestamp(sym)
        if _fill_ts:
            logger.info(f"[{sym}] Orphan entry_time recovered from fill: {_fill_ts}")
            _now_iso = _fill_ts
        else:
            logger.warning(
                f"[{sym}] Orphan entry_time: fill timestamp unavailable — "
                f"using adoption time (opened_today may be inaccurate)."
            )
            _now_iso = datetime.now(PT).isoformat()
        # P1-2: Fetch live ATR for orphan and compute proper stop/target using
        # the same profile multipliers as a normal entry (stop=1.25×ATR,
        # target=2.5×ATR). Falls back to ±5% emergency floor if ATR fetch
        # fails so the exit manager always has a stop to enforce.
        _orph_atr   = None
        _orph_stop  = None
        _orph_tgt   = None
        try:
            _orph_daily = fetch_bars(sym, config.TF_DAILY,
                                     num_bars=config.ATR_PERIOD + 5)
            if _orph_daily is not None and not _orph_daily.empty:
                _orph_atr  = round(
                    (calculate_atr(_orph_daily, config.ATR_PERIOD)
                     / 100) * _entry_px,
                    4,
                )
                _stop_dist = _orph_atr * config.INTRADAY_STOP_ATR_MULT
                _tgt_dist  = _orph_atr * config.INTRADAY_TARGET_ATR_MULT
                if _direction == "long":
                    _orph_stop = round(_entry_px - _stop_dist, 2)
                    _orph_tgt  = round(_entry_px + _tgt_dist, 2)
                else:
                    _orph_stop = round(_entry_px + _stop_dist, 2)
                    _orph_tgt  = round(_entry_px - _tgt_dist, 2)
        except Exception as _orph_atr_err:
            logger.debug(
                f"[{sym}] Orphan ATR fetch failed: {_orph_atr_err}"
            )

        if _orph_stop is None:
            # Fallback: ±5% emergency floor (original behaviour)
            _emg_stop_pct = 0.95 if _direction == "long" else 1.05
            _orph_stop    = round(_entry_px * _emg_stop_pct, 2)

        _stop_src = (
            f"ATR-based (atr=${_orph_atr:.2f}, "
            f"stop={config.INTRADAY_STOP_ATR_MULT}×, "
            f"tgt={config.INTRADAY_TARGET_ATR_MULT}×)"
            if _orph_atr
            else "emergency ±5% floor (ATR unavailable)"
        )
        logger.critical(
            f"[{sym}] ORPHANED POSITION ADOPTED — Alpaca: {pos.qty} shares"
            f" @ ${_entry_px:.2f} ({_direction}). "
            f"Stop=${_orph_stop:.2f} "
            f"target={'$'+str(_orph_tgt) if _orph_tgt else 'None'} "
            f"[{_stop_src}]. overnight=True"
        )
        # P4-1: attempt live re-score before writing dict
        # (fail-open: 0 if unavailable)
        _orph_score = None
        try:
            _orph_score = get_live_score(sym, _direction, _tm)
        except Exception as _sc_err:
            logger.debug(f"[{sym}] Orphan pre-score failed: {_sc_err}")

        tracker.open_trades[sym] = {
            "symbol":                 sym,
            "direction":              _direction,
            "qty":                    _qty,
            "qty_remaining":          _qty,
            "entry_price":            _entry_px,
            "stop":                   _orph_stop,
            "original_stop":          _orph_stop,
            "target":                 _orph_tgt,
            "trail_stop":             None,
            "trade_mode":             (
                "quarterly_hold" if sym in _get_qhm_syms() else "intraday"
            ),
            "score":                  _orph_score if _orph_score is not None
                                      else 0,
            "score_16pt":             None,
            "atr_value":              _orph_atr,
            "partial_exited":         False,
            "entry_time":             _now_iso,
            "status":                 "open",
            "reversal_scan_count":    0,
            "reversal_confirm_count": 0,
            "overnight":              True,
            "overnight_since":        _now_iso,
            "gtc_stop_order_id":      None,
            "stop_breached":          False,
            "stop_breach_price":      None,
            "_adopted_orphan":        True,
        }
        # QHM stop linkage: restore QHM GTC stop_order_id so exit_logic sees it
        if sym in _get_qhm_syms():
            try:
                _qhm_state_path = (
                    Path(__file__).resolve().parent.parent
                    / "data" / "state" / "quarterly_holds.json"
                )
                if _qhm_state_path.exists():
                    _qhm_raw = _json.loads(_qhm_state_path.read_text())
                    _qhm_stop_id = (_qhm_raw.get(sym) or {}).get("stop_order_id")
                    if _qhm_stop_id:
                        tracker.open_trades[sym]["gtc_stop_order_id"] = _qhm_stop_id
                        logger.info(
                            "[%s] QHM stop linked at adoption: %s", sym, _qhm_stop_id
                        )
            except Exception as _qe:
                logger.warning("[%s] QHM stop linkage failed: %s", sym, _qe)
        # P5-H3: adopt any existing Alpaca GTC or DAY stop so restarts don't
        # submit duplicate stops. Prior session's live stop orders are stored
        # here so _submit_rth_day_stops() and the AH loop will skip
        # re-submission.
        # BUG-B fix: previously only GTC stops were detected; DAY stops from a
        # prior instance were invisible → duplicate submission → 40310000.
        try:
            _existing_orders = get_open_orders(sym)
            if _existing_orders is None:
                logger.warning(  # type: ignore[unreachable]
                    f"[{sym}] get_open_orders returned None — orphan order "
                    f"adoption skipped (API failure)."
                )
                raise RuntimeError("get_open_orders API failure")
            for _eord in _existing_orders:
                _etype = str(getattr(_eord, "type", "")).lower()
                _etif  = str(getattr(_eord, "time_in_force", "")).lower()
                if "stop" in _etype:
                    if "gtc" in _etif:
                        tracker.open_trades[sym]["gtc_stop_order_id"] = (
                            str(_eord.id)
                        )
                        logger.info(
                            f"[{sym}] Orphan GTC stop adopted: order "
                            f"{_eord.id} @ "
                            f"${getattr(_eord, 'stop_price', '?')} (P5-H3)"
                        )
                        break
                    elif "day" in _etif:
                        tracker.open_trades[sym][
                            "rth_day_stop_order_id"
                        ] = str(_eord.id)
                        logger.info(
                            f"[{sym}] Orphan DAY stop adopted: order "
                            f"{_eord.id} @ "
                            f"${getattr(_eord, 'stop_price', '?')} "
                            f"(P5-H3 restart recovery)"
                        )
                        break
        except Exception as _gtc_adopt_err:
            logger.debug(
                f"[{sym}] GTC/DAY order adoption scan failed: "
                f"{_gtc_adopt_err}"
            )

        # Fix: if position is overnight AND no existing stop was adopted,
        # submit a new GTC stop. Without this, adopted orphan overnight
        # positions have zero exchange-level protection.
        _orph_trade = tracker.open_trades[sym]
        if (
            _orph_trade.get("gtc_stop_order_id") is None
            and _orph_trade.get("rth_day_stop_order_id") is None
            and _orph_stop is not None
            and not _orph_trade.get("internal_hard_stop_active")
        ):
            _orph_gtc_side = "sell" if _direction == "long" else "buy"
            try:
                # ── OM-BUG-1: Market-price guard (orphan adoption) ────────────
                _o_guard_blocked = False
                _o_live_px = None
                try:
                    from data.alpaca_data import get_latest_trade as _o_get_px
                    _o_live_px = _o_get_px(sym)
                except Exception as _o_px_err:
                    logger.debug(
                        f"[{sym}] OM-BUG-1 orphan price fetch failed, "
                        f"proceeding: {_o_px_err}"
                    )
                if _o_live_px and _o_live_px > 0:
                    _o_invalid = (
                        (_orph_gtc_side == "sell" and _orph_stop >= _o_live_px) or
                        (_orph_gtc_side == "buy"  and _orph_stop <= _o_live_px)
                    )
                    if _o_invalid:
                        logger.critical(
                            f"[{sym}] OM-BUG-1 GUARD (orphan): stop "
                            f"${_orph_stop:.2f} invalid vs live "
                            f"${_o_live_px:.2f} ({_orph_gtc_side}). "
                            f"Skipping GTC — software stop active. "
                            f"MANUAL REVIEW REQUIRED."
                        )
                        _orph_trade["internal_hard_stop_active"] = True
                        _orph_trade["gtc_rejected_42210000"] = True
                        tracker._save_log()
                        try:
                            alert_gtc_failed(
                                sym, _orph_gtc_side, _orph_stop,
                                f"OM-BUG-1 price guard (orphan): "
                                f"stop=${_orph_stop:.2f} invalid vs "
                                f"live=${_o_live_px:.2f} — software stop active",
                            )
                        except Exception as _o_ae:
                            logger.warning(
                                f"[{sym}] alert_gtc_failed call failed: {_o_ae}"
                            )
                        _o_guard_blocked = True
                _orph_gtc_ord = (
                    None if _o_guard_blocked
                    else submit_gtc_stop_order(
                        sym, _qty, _orph_gtc_side, _orph_stop
                    )
                )
                if _orph_gtc_ord:
                    tracker.open_trades[sym]["gtc_stop_order_id"] = str(
                        _orph_gtc_ord.id  # type: ignore[attr-defined]
                    )
                    tracker._save_log()  # OM-BUG-2: persist ID before alert
                    logger.warning(
                        f"[{sym}] Orphan overnight — no existing stop found"
                        f" on Alpaca. New GTC stop submitted @ "
                        f"${_orph_stop:.2f} (ID: {_orph_gtc_ord.id})"  # type: ignore[attr-defined]
                    )
                elif not _o_guard_blocked:
                    logger.critical(
                        f"[{sym}] Orphan overnight GTC stop FAILED — "
                        f"position UNPROTECTED @ stop=${_orph_stop:.2f}. "
                        f"Manual action required in Alpaca app."
                    )
                    try:
                        alert_gtc_failed(
                            sym, _orph_gtc_side, _orph_stop,
                            "submit returned None (orphan adoption)",
                        )
                    except Exception as _ae:
                        logger.warning(f"[{sym}] alert_gtc_failed call failed: {_ae}")
            except Exception as _orph_gtc_err:
                logger.critical(
                    f"[{sym}] Orphan overnight GTC stop exception — "
                    f"position UNPROTECTED: {_orph_gtc_err}"
                )
                try:
                    alert_gtc_failed(
                        sym, _orph_gtc_side, _orph_stop,
                        str(_orph_gtc_err),
                    )
                except Exception as _ae:
                    logger.warning(f"[{sym}] alert_gtc_failed call failed: {_ae}")

        if _orph_score is not None:
            logger.info(f"[{sym}] Orphan re-scored: {_orph_score}/12")
        else:
            logger.info(
                f"[{sym}] Orphan score unavailable at adoption — "
                f"score=0/12 (will update next scan)"
            )
        tracker._save_log()
        tracker.traded_today.add(sym)
        if risk is not None:
            risk.open_positions = max(
                risk.open_positions, len(tracker.open_trades)
            )

    # BUG-3 fix: Slack alert for orphan-adopted positions.
    # Orphan adoption never fired alert_entry (only triggered on live order
    # submission). After a restart, the user had no visibility into which
    # positions the bot inherited.
    if orphans:
        _orphan_lines = []
        for _osym in sorted(orphans):
            _otrade = tracker.open_trades.get(_osym, {})
            _oentry = _otrade.get("entry_price", 0)
            _ostop  = _otrade.get("stop", 0)
            _oqty   = _otrade.get("qty", 0)
            _orphan_lines.append(
                f"• {_osym}: {_oqty}sh @ ${_oentry:.2f}"
                f" | stop ${_ostop:.2f}"
            )
        try:
            send_slack(
                f":recycle: *Bot restarted — {len(orphans)} orphan "
                f"position(s) adopted*\n"
                + "\n".join(_orphan_lines)
            )
        except Exception as _orph_alert_err:
            logger.warning(
                f"Orphan adoption Slack alert failed: {_orph_alert_err}"
            )

    # ── Externally closed positions (in tracker, not in Alpaca) ─────────────
    # Skip symbols we just reconciled via GTC stop — already handled
    for sym in tracker_symbols - alpaca_symbols:
        trade = tracker.open_trades[sym]
        # SF-03: fetch the real fill price from Alpaca's closed-order history
        # instead of recording entry_price (which always produced PnL=$0 and
        # left the kill switch blind to external liquidation losses).
        # poll_secs=0 — position is already gone, no need to wait.
        exit_price = fetch_actual_fill_price(sym, trade, poll_secs=0)
        logger.warning(
            f"[{sym}] Position in tracker but NOT in Alpaca — closed "
            f"externally. Recording exit at ${exit_price:.2f} "
            f"(entry was ${trade.get('entry_price', 0):.2f})."
        )
        pnl = tracker.record_exit(sym, exit_price, reason="external_close",
                                   mri_level="UNKNOWN")
        if risk is not None:                                      # C-2
            risk.register_close(pnl or 0.0)

    # ── Size mismatches ──────────────────────────────────────────────────────
    for sym in tracker_symbols & alpaca_symbols:
        # abs(): short positions are negative in Alpaca
        alpaca_qty  = abs(int(float(alpaca_positions[sym].qty)))
        tracker_qty = tracker.open_trades[sym].get(
            "qty_remaining",
            tracker.open_trades[sym].get("qty", 0),
        )

        # ── Direction mismatch check ─────────────────────────────────────────
        # Mirrors orphan adoption: pos.qty sign is authoritative.
        # pos.qty < 0 in Alpaca → position is short.
        _alpaca_raw_qty    = float(alpaca_positions[sym].qty)
        _alpaca_direction  = "long" if _alpaca_raw_qty > 0 else "short"
        _tracker_direction = tracker.open_trades[sym].get("direction", "long")
        if _alpaca_direction != _tracker_direction:
            _pos          = alpaca_positions[sym]
            _entry_px_alp = float(
                getattr(_pos, "avg_entry_price", 0) or 0
            )
            _entry_px_trk = float(
                tracker.open_trades[sym].get("entry_price", 0) or 0
            )
            _entry_px = (
                _entry_px_alp if _entry_px_alp > 0 else _entry_px_trk
            )
            logger.critical(
                f"[{sym}] DIRECTION MISMATCH — "
                f"Alpaca={_alpaca_direction}, "
                f"tracker={_tracker_direction}. Correcting tracker and "
                f"recomputing stop/target. entry_px=${_entry_px:.2f}"
            )
            # 1. Correct tracker direction and entry price
            tracker.open_trades[sym]["direction"] = _alpaca_direction
            if _entry_px_alp > 0:
                tracker.open_trades[sym]["entry_price"] = _entry_px_alp
            # Reset exit-tracking fields — stale from wrong-direction position
            tracker.open_trades[sym]["trail_stop"]             = None
            tracker.open_trades[sym]["profit_tranche_level"]   = 0
            tracker.open_trades[sym]["partial_exited"]         = False
            tracker.open_trades[sym]["stop_breached"]          = False
            tracker.open_trades[sym]["stop_breach_price"]      = None
            tracker.open_trades[sym]["reversal_scan_count"]    = 0
            tracker.open_trades[sym]["reversal_confirm_count"] = 0
            # Zero stale partial P&L from old direction. partial_pnl computed
            # while direction was wrong is meaningless for the corrected
            # direction — carrying it into record_exit() produces corrupt
            # total P&L (QQQ short case: stale +22.65 flipped a -12.68
            # actual loss to a reported +10.76 gain). Real realized P&L from
            # prior partial closes is already in trade_events.jsonl.
            _stale_partial = tracker.open_trades[sym].get("partial_pnl", 0.0)
            if _stale_partial != 0.0:
                logger.critical(
                    f"[{sym}] Zeroing stale partial_pnl=${_stale_partial:.2f}"
                    f" (accumulated under wrong direction="
                    f"{_tracker_direction}). Value preserved in "
                    f"trade_events.jsonl."
                )
            tracker.open_trades[sym]["partial_pnl"] = 0.0
            # 2. Recompute stop/target — ATR first, ±5% emergency fallback
            _dir_atr  = None
            _dir_stop = None
            _dir_tgt  = None
            try:
                _dir_daily = fetch_bars(sym, config.TF_DAILY,
                                        num_bars=config.ATR_PERIOD + 5)
                if _dir_daily is not None and not _dir_daily.empty:
                    _dir_atr   = round(
                        (calculate_atr(_dir_daily, config.ATR_PERIOD)
                         / 100) * _entry_px,
                        4,
                    )
                    _stop_dist = _dir_atr * config.INTRADAY_STOP_ATR_MULT
                    _tgt_dist  = _dir_atr * config.INTRADAY_TARGET_ATR_MULT
                    if _alpaca_direction == "long":
                        _dir_stop = round(_entry_px - _stop_dist, 2)
                        _dir_tgt  = round(_entry_px + _tgt_dist, 2)
                    else:
                        _dir_stop = round(_entry_px + _stop_dist, 2)
                        _dir_tgt  = round(_entry_px - _tgt_dist, 2)
            except Exception as _dir_atr_err:
                logger.debug(
                    f"[{sym}] Direction-fix ATR fetch failed: {_dir_atr_err}"
                )
            if _dir_stop is None and _entry_px > 0:
                _emg_pct  = (
                    0.95 if _alpaca_direction == "long" else 1.05
                )
                _dir_stop = round(_entry_px * _emg_pct, 2)
            if _dir_stop is not None:
                tracker.open_trades[sym]["stop"]          = _dir_stop
                tracker.open_trades[sym]["original_stop"] = _dir_stop
                tracker.open_trades[sym]["target"]        = _dir_tgt
                logger.critical(
                    f"[{sym}] Stop/target recomputed ({_alpaca_direction}): "
                    f"stop=${_dir_stop:.2f}, "
                    f"target={'$'+str(_dir_tgt) if _dir_tgt else 'None'} "
                    f"(entry=${_entry_px:.2f}, "
                    f"{'ATR-based' if _dir_atr else 'emergency ±5%'})"
                )
            else:
                logger.critical(
                    f"[{sym}] Cannot compute stop/target — entry_price=0. "
                    f"MANUAL REVIEW REQUIRED."
                )
            # 3. Cancel stale GTC stop (wrong direction — must cancel before
            # resubmit)
            for _dkey in ("gtc_stop_order_id", "rth_day_stop_order_id"):
                _doid = tracker.open_trades[sym].get(_dkey)
                if _doid:
                    _cok = cancel_order(_doid)
                    tracker.open_trades[sym][_dkey] = None
                    logger.warning(
                        f"[{sym}] Cancelled stale {_dkey} {_doid} "
                        f"(was {_tracker_direction}, now "
                        f"{_alpaca_direction}) — cancel_ok={_cok}"
                    )
            # 4. Resubmit GTC stop with corrected side and recomputed stop
            _correct_gtc_side = (
                "sell" if _alpaca_direction == "long" else "buy"
            )
            if _dir_stop and alpaca_qty >= 1:
                try:
                    # ── OM-BUG-1: Market-price guard (direction-mismatch) ──────
                    _dm_live_px = None
                    _dm_guard_blocked = False
                    try:
                        from data.alpaca_data import get_latest_trade as _dm_get_px
                        _dm_live_px = _dm_get_px(sym)
                    except Exception as _dm_px_err:
                        logger.debug(
                            f"[{sym}] OM-BUG-1 price fetch failed "
                            f"(dir-mismatch), proceeding: {_dm_px_err}"
                        )
                    if _dm_live_px and _dm_live_px > 0:
                        _dm_invalid = (
                            (_correct_gtc_side == "sell"
                             and _dir_stop >= _dm_live_px) or
                            (_correct_gtc_side == "buy"
                             and _dir_stop <= _dm_live_px)
                        )
                        if _dm_invalid:
                            logger.critical(
                                f"[{sym}] OM-BUG-1 GUARD (dir-mismatch): stop "
                                f"${_dir_stop:.2f} invalid vs live "
                                f"${_dm_live_px:.2f} ({_correct_gtc_side}). "
                                f"Skipping GTC — software stop active. "
                                f"MANUAL REVIEW REQUIRED."
                            )
                            tracker.open_trades[sym][
                                "internal_hard_stop_active"
                            ] = True
                            tracker.open_trades[sym][
                                "gtc_rejected_42210000"
                            ] = True
                            tracker._save_log()
                            try:
                                alert_gtc_failed(
                                    sym, _correct_gtc_side, _dir_stop,
                                    f"OM-BUG-1 price guard (dir-mismatch): "
                                    f"stop=${_dir_stop:.2f} invalid vs "
                                    f"live=${_dm_live_px:.2f} — "
                                    f"software stop active",
                                )
                            except Exception as _dm_ae:
                                logger.warning(
                                    f"[{sym}] alert_gtc_failed failed: {_dm_ae}"
                                )
                            _dm_guard_blocked = True
                    _new_gtc = (
                        None if _dm_guard_blocked
                        else submit_gtc_stop_order(
                            sym, alpaca_qty, _correct_gtc_side, _dir_stop
                        )
                    )
                    if _new_gtc:
                        tracker.set_gtc_stop_order_id(sym, str(_new_gtc.id))  # type: ignore[attr-defined]
                        tracker._save_log()
                        logger.critical(
                            f"[{sym}] Corrected GTC stop submitted: "
                            f"{alpaca_qty}sh {_correct_gtc_side} @ "
                            f"${_dir_stop:.2f} | order {_new_gtc.id}"  # type: ignore[attr-defined]
                        )
                    elif not _dm_guard_blocked:
                        logger.critical(
                            f"[{sym}] Corrected GTC stop submission FAILED"
                            f" — position unprotected. MANUAL REVIEW."
                        )
                except Exception as _gtc_dir_err:
                    logger.critical(
                        f"[{sym}] Corrected GTC stop exception: "
                        f"{_gtc_dir_err} — MANUAL REVIEW REQUIRED."
                    )
            else:
                logger.critical(
                    f"[{sym}] Cannot submit corrected GTC stop — "
                    f"stop_price=None or qty<1. MANUAL REVIEW REQUIRED."
                )
            tracker._save_log()
        # ── End direction mismatch check ─────────────────────────────────────

        if alpaca_qty != tracker_qty:
            logger.warning(
                f"[{sym}] Qty mismatch — Alpaca={alpaca_qty}, "
                f"tracker={tracker_qty}. Updating tracker to match Alpaca."
            )
            # Bug 1 fix: if tracker has MORE shares than Alpaca, some shares
            # were closed externally while the bot was down. Bank the partial
            # P&L for those shares before overwriting qty_remaining —
            # otherwise the gain/loss is permanently lost (TSLA case: qty
            # 3→2 with no partial_pnl recorded).
            if tracker_qty > alpaca_qty:
                _closed_n  = tracker_qty - alpaca_qty
                _fill_px   = fetch_actual_fill_price(
                    sym, tracker.open_trades[sym], poll_secs=0
                )
                _entry_px  = tracker.open_trades[sym].get("entry_price", 0)
                _dir       = tracker.open_trades[sym].get(
                    "direction", "long"
                )
                _rec_pnl   = (
                    (_fill_px - _entry_px) * _closed_n
                    if _dir == "long"
                    else (_entry_px - _fill_px) * _closed_n
                )
                tracker.open_trades[sym]["partial_pnl"] = round(
                    tracker.open_trades[sym].get("partial_pnl", 0.0)
                    + _rec_pnl,
                    2,
                )
                tracker.open_trades[sym]["partial_exited"]     = True
                tracker.open_trades[sym]["partial_exit_price"] = _fill_px
                # board audit fix: mirrors record_partial_exit() so
                # partial_realized_today includes this in EOD report
                tracker.open_trades[sym]["partial_exit_time"]  = (
                    datetime.now(PT).isoformat()  # PT: tracker convention (RC-1 fix)
                )
                # Bug 2 fix: advance profit_tranche_level so
                # check_partial_exits() doesn't re-execute the same tranche,
                # causing double P&L accounting. Each externally-closed share
                # corresponds to at least one tranche (T1).
                # Use max() to never downgrade an already-recorded tranche.
                _prior_tl = tracker.open_trades[sym].get(
                    "profit_tranche_level", 0
                )
                _new_tl   = max(_prior_tl, 1)   # at minimum, T1 is now done
                tracker.open_trades[sym]["profit_tranche_level"] = _new_tl
                # Also clear GTC partial keys for any tranche now accounted
                # for, so Patch 2 doesn't bank the same fill a second time
                # on next restart.
                _qtm_partials = (
                    tracker.open_trades[sym].get("gtc_partial_order_ids")
                    or {}
                )
                for _ti in range(1, _new_tl + 1):
                    _qtm_partials.pop(f"t{_ti}", None)
                logger.warning(
                    f"[{sym}] Qty mismatch: banking {_closed_n} "
                    f"externally-closed share(s) @ ${_fill_px:.2f} "
                    f"| partial_pnl += ${_rec_pnl:.2f} "
                    f"(dir={_dir}, entry=${_entry_px:.2f}) "
                    f"| profit_tranche_level → {_new_tl}"
                )
            tracker.open_trades[sym]["qty_remaining"] = alpaca_qty
            tracker._save_log()

    if not orphans and not (tracker_symbols - alpaca_symbols):
        logger.info(
            f"Position reconciliation: OK — {len(tracker_symbols)} "
            f"position(s) in sync."
        )

    # P2-OPEN-POS-COUNT: reconcile risk.open_positions against tracker truth
    if risk is not None:
        _tracker_count = len(tracker.open_trades)
        if risk.open_positions != _tracker_count:
            logger.critical(
                f"POSITION COUNT DRIFT: "
                f"risk.open_positions={risk.open_positions} "
                f"vs tracker={_tracker_count}. "
                f"Correcting to tracker count."
            )
            risk.open_positions = _tracker_count
        else:
            logger.debug(
                f"Position count OK: {_tracker_count} open position(s) "
                f"in sync."
            )
