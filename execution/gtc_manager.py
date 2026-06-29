# ruff: noqa: E501
"""
execution/gtc_manager.py
GTC/DAY stop submission and cancel helpers — extracted from main.py Phase 2.

Owns:
  - _rth_day_stops_submitted_dates   once-per-date gate for RTH DAY stops
  - _rth_day_stop_failure_counts     ANOMALY-1: consecutive failures per symbol
  - submit_rth_day_stops()           submit DAY stop-market at RTH open
  - cancel_open_gtc_orders()         cancel all GTC orders before record_exit()
  - reset_daily()                    clear per-day state at midnight ET

Broker imports: cancel_order, get_open_orders, get_open_position,
                submit_day_stop_order all come from execution.broker.
"""

import logging
import random
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

from alerts import send_slack
from execution.broker import (
    cancel_order,
    get_open_orders,
    get_open_position,
    get_order,
    submit_day_stop_order,
)

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Module-level state (previously main.py globals)
# ---------------------------------------------------------------------------

_rth_day_stops_submitted_dates: set = set()   # gates submission once per calendar date
_rth_day_stop_failure_counts:   dict = {}     # ANOMALY-1: {symbol: int} consecutive failures


def reset_daily() -> None:
    """Clear per-day state at midnight ET. Called from main() daily reset block."""
    _rth_day_stop_failure_counts.clear()      # ANOMALY-1: reset per-symbol counts each day
    # _rth_day_stops_submitted_dates intentionally NOT cleared — it uses date strings
    # and auto-expires (today's date never matches tomorrow's check). Clearing would
    # allow a double-submission if reset fires before midnight rolls over on OCI.


def get_failure_counts() -> dict:
    """Return a reference to the failure counts dict (for ANOMALY-1 reporting in run_cycle)."""
    return _rth_day_stop_failure_counts


# ---------------------------------------------------------------------------
# RTH DAY stop submission
# ---------------------------------------------------------------------------

def submit_rth_day_stops(tracker) -> None:
    """Submit DAY stop-market orders at RTH open for overnight positions with no exchange stop.

    DAY orders expire at 4:00 PM ET — no conflict with tonight's AH GTC submission.
    Tracked in trade["rth_day_stop_order_id"]; cleared at next pre-market by
    _cancel_and_reconcile_gtc_stops() in orphan_manager.

    Gated by _rth_day_stops_submitted_dates so it fires exactly once per
    calendar date, even across multiple 5-min run_cycle() calls.
    """
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if today in _rth_day_stops_submitted_dates:
        return
    _rth_day_stops_submitted_dates.add(today)

    needs_stop = [
        (sym, t) for sym, t in tracker.open_trades.items()
        if t.get("overnight")
        and not t.get("gtc_stop_order_id")
        and not t.get("rth_day_stop_order_id")
    ]
    if not needs_stop:
        logger.info(
            "RTH DAY stops: all overnight positions already have exchange-level stop protection."
        )
        return

    logger.warning(
        f"RTH DAY stops: {len(needs_stop)} overnight position(s) have no exchange stop — "
        f"submitting DAY stop-market orders now."
    )
    for sym, t in needs_stop:
        # BUG-B fix: positions loaded from trade_log.json don't go through the orphan
        # adoption path, so existing DAY stops from a prior bot instance are never
        # detected. Check Alpaca for a live DAY stop before submitting a duplicate.
        try:
            _live_ords = get_open_orders(sym)
            if _live_ords is None:
                logger.warning(  # type: ignore[unreachable]
                    f"[{sym}] get_open_orders returned None — DAY stop pre-check skipped "
                    f"(API failure). May submit duplicate stop."
                )
                raise RuntimeError("get_open_orders API failure")
            _prior_day = next(
                (o for o in _live_ords
                 if "stop" in str(getattr(o, "type", "")).lower()
                 and "day" in str(getattr(o, "time_in_force", "")).lower()),
                None,
            )
            if _prior_day:
                t["rth_day_stop_order_id"] = str(_prior_day.id)
                logger.info(
                    f"[{sym}] Prior DAY stop adopted (restart recovery): "
                    f"order {_prior_day.id} @ ${getattr(_prior_day, 'stop_price', '?')}"
                )
                tracker._save_log()
                continue
        except Exception as _day_chk_err:
            logger.debug(f"[{sym}] DAY stop pre-check failed: {_day_chk_err}")

        # AWP audit fix (2026-06-28): the per-calendar-day gate above
        # (_rth_day_stops_submitted_dates.add(today)) is set BEFORE this
        # loop runs, so it fires exactly once per day regardless of how
        # this iteration ends. Without a guard here, an unhandled exception
        # for ONE symbol (malformed trade dict, broker SDK raise) would
        # propagate up and terminate the whole function — abandoning every
        # symbol still queued in `needs_stop` for the rest of THIS pass,
        # with no possibility of retry today since the gate is already
        # set. Wrapping the per-symbol body lets one bad symbol fail loudly
        # without blocking DAY stop protection for every other overnight
        # position. Confirmed reachable by 2 independent board domain
        # agents during the Phase 2 full-board redo.
        try:
            _dir  = t["direction"]
            _stop = t.get("trail_stop") or t.get("stop")
            if not _stop:
                logger.warning(f"[{sym}] RTH DAY stop: no stop price in tracker — skipping.")
                continue
            # use qty_remaining when explicitly set (including 0 after a
            # full partial exit); fall back to the original qty only when
            # the field is absent (None). The old `or` treated
            # qty_remaining=0 as falsy and fell through to the full original
            # size, submitting a stop for more shares than actually remain —
            # the same bug class already fixed elsewhere in this codebase
            # (orphan_manager.py Patch 1, exit_logic.py's trail-ratchet path).
            _qty_rem_raw = t.get("qty_remaining")
            _qty = abs(int(t.get("qty", 0) if _qty_rem_raw is None else _qty_rem_raw))
            if _qty <= 0:
                logger.info(f"[{sym}] RTH DAY stop: qty_remaining=0 — position already flat, skipping.")
                continue
            _offset     = round(random.uniform(0.01, 0.05), 2)
            _stop_price = (round(_stop + _offset, 2) if _dir == "short"
                           else round(_stop - _offset, 2))
            _side = "buy" if _dir == "short" else "sell"

            # Pre-flight: if stop is already above/below market, Alpaca will reject.
            try:
                _pos = get_open_position(sym)
                _mkt = float(_pos.current_price) if _pos else None
            except Exception as _pos_e:
                logger.debug("[%s] RTH DAY stop pre-flight price check failed — %s", sym, _pos_e)
                _mkt = None
            if _mkt is not None:
                if _dir == "long" and _stop_price >= _mkt:
                    logger.warning(
                        f"[{sym}] RTH DAY stop skipped — stop_price ${_stop_price:.2f} "
                        f">= market ${_mkt:.2f} (gap-up open). Set manual stop if needed."
                    )
                    continue
                if _dir == "short" and _stop_price <= _mkt:
                    logger.warning(
                        f"[{sym}] RTH DAY stop skipped — stop_price ${_stop_price:.2f} "
                        f"<= market ${_mkt:.2f} (gap-down open). Set manual stop if needed."
                    )
                    continue

            _order = submit_day_stop_order(symbol=sym, qty=_qty, side=_side,
                                           stop_price=_stop_price)
            if _order:
                t["rth_day_stop_order_id"] = str(getattr(_order, "id", ""))
                _rth_day_stop_failure_counts[sym] = 0   # ANOMALY-1: reset on success
                logger.info(
                    f"[{sym}] RTH DAY stop active: {_side.upper()} {_qty} @ "
                    f"${_stop_price:.2f} (tracker stop={_stop:.2f} + offset={_offset:.2f}) | "
                    f"order {getattr(_order, 'id', 'unknown')}"
                )
            else:
                _rth_day_stop_failure_counts[sym] = (
                    _rth_day_stop_failure_counts.get(sym, 0) + 1
                )  # ANOMALY-1
                logger.error(
                    f"[{sym}] RTH DAY stop FAILED "
                    f"(attempt #{_rth_day_stop_failure_counts[sym]}) — "
                    f"position unprotected. Set manual stop in Alpaca immediately."
                )
        except Exception as _sym_err:
            logger.critical(
                f"[{sym}] RTH DAY stop: unexpected error processing this symbol — "
                f"{_sym_err!r}. Position may be unprotected; skipping to next "
                f"symbol rather than aborting the remaining queue. Set manual "
                f"stop in Alpaca if needed."
            )
            try:
                send_slack(
                    f":rotating_light: [{sym}] RTH DAY stop: unexpected error "
                    f"({_sym_err!r}) — position may be unprotected. Verify "
                    f"manually in Alpaca."
                )
            except Exception as _sl_e:
                logger.debug("[%s] RTH DAY stop unexpected-error Slack alert failed — %s", sym, _sl_e)
    tracker._save_log()


# ---------------------------------------------------------------------------
# Cancel open GTC orders before position close
# ---------------------------------------------------------------------------

def cancel_open_gtc_orders(symbol: str, trade: dict, tracker) -> bool:
    """Cancel all open GTC orders tied to a position before record_exit().

    Called at every full-close site (hard stop, target, reversal, breakeven,
    thesis invalidation) so a GTC partial or GTC stop cannot fill after the
    position is gone and open a reverse trade.

    Cancels:
      - gtc_partial_order_ids  — tranche limit orders (best-effort)
      - _gtc_stop_order_id     — confirmed-stop market order
      - gtc_stop_order_id      — overnight protection stop order

    Returns True if ALL stop orders are confirmed gone (cancelled, terminal, or
    404 not-found). Returns False if any stop order remains live or unverifiable
    (caller should gate the close and retry next cycle).

    On timeout/unverifiable: retains the ID so the next-cycle cancel attempt fires
    again (previously cleared unconditionally, making the caller's gate a no-op).
    rth_day_stop_order_id is excluded: DAY orders auto-expire at 4:00 PM ET.
    """
    _changed = False
    _all_confirmed = True
    _TERMINAL = frozenset(("canceled", "filled", "expired", "done_for_day", "replaced"))

    # GTC partial limit orders — best-effort cancel, blanket wipe.
    # Limit orders require price to return; if one later fills, orphan_manager
    # reconciles the resulting position at next pre-market.
    _partials = trade.get("gtc_partial_order_ids") or {}
    for _tk, _oid in list(_partials.items()):
        if not _oid:
            continue
        try:
            _ok = cancel_order(_oid)
            if _ok:
                logger.info(
                    f"[{symbol}] GTC partial {_tk} order {_oid} cancelled "
                    f"(position closing)."
                )
            else:
                logger.warning(
                    f"[{symbol}] GTC partial {_tk} cancel failed"
                    f" — may still be live on L2."
                )
        except Exception as _ce:
            logger.warning(f"[{symbol}] GTC partial {_tk} cancel error: {_ce}")
    if _partials:
        trade["gtc_partial_order_ids"] = {}
        _changed = True

    # GTC stop orders — stop-market orders that can open a reverse (naked)
    # position if they fill after the position closes. Verify cancellation.
    for _key in ("_gtc_stop_order_id", "gtc_stop_order_id"):
        _oid = trade.get(_key)
        if not _oid:
            continue

        try:
            _ok = cancel_order(_oid)
        except Exception as _ce:
            logger.warning(
                f"[{symbol}] {_key} cancel_order({_oid}) raised: {_ce} — verifying"
            )
            _ok = False

        if _ok:
            trade[_key] = None
            logger.info(f"[{symbol}] {_key} {_oid} cancelled (position closing).")
            _changed = True
            continue

        # Cancel returned False — verify actual order state before clearing ID.
        _old_to = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5.0)
        _ord = _ve = None
        try:
            _ord = get_order(_oid)
        except Exception as _exc:
            _ve = _exc
        finally:
            socket.setdefaulttimeout(_old_to)

        if _ve is not None:
            # 5xx / timeout — unknown state; retain ID so next cycle retries cancel
            logger.critical(
                f"[{symbol}] {_key} {_oid} cancel failed and verify raised {_ve!r}"
                f" — status UNKNOWN. ID retained; will retry next scan."
                f" MANUAL CANCEL REQUIRED in Alpaca if this persists."
            )
            try:
                send_slack(
                    f":rotating_light: [{symbol}] GTC cancel UNVERIFIED"
                    f" | key={_key} oid={_oid} | err={_ve!r}"
                    f" | Cancel manually in Alpaca NOW."
                )
            except Exception as _sl_e:
                logger.debug("[%s] GTC cancel-unverified Slack alert failed — %s", symbol, _sl_e)
            _all_confirmed = False  # unknown state — caller gates on return value
            continue

        if _ord is None:
            # 404 — order not found at Alpaca (confirmed gone)
            logger.warning(
                f"[{symbol}] {_key} {_oid} not found at Alpaca after cancel failure"
                f" (order confirmed gone). Clearing ID."
            )
            trade[_key] = None
            _changed = True
            continue

        _status = str(getattr(_ord, "status", "")).lower()
        if _status in _TERMINAL:
            logger.info(
                f"[{symbol}] {_key} {_oid} already {_status!r} (terminal)."
                f" cancel_order returned False but order is gone. Clearing ID."
            )
            trade[_key] = None
            _changed = True
        else:
            # Order still active — retain ID so next-cycle cancel attempt can retry
            logger.critical(
                f"[{symbol}] {_key} {_oid} still {_status!r} after cancel failure"
                f" — ID retained; caller will gate close and retry next scan."
                f" MANUAL CANCEL REQUIRED in Alpaca if this persists."
            )
            try:
                send_slack(
                    f":rotating_light: [{symbol}] GTC cancel FAILED"
                    f" — order still {_status!r}"
                    f" | key={_key} oid={_oid}"
                    f" | Caller will retry. Cancel manually if bot cannot."
                )
            except Exception as _sl_e:
                logger.debug("[%s] GTC cancel-failed Slack alert failed — %s", symbol, _sl_e)
            _all_confirmed = False  # order still live — caller gates on return value

    if _changed:
        tracker._save_log()
    return _all_confirmed
