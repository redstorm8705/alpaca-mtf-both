# ruff: noqa: E501 — dense log/page strings run long (project convention, as broker.py)
"""
execution/lifecycle.py
Trade lifecycle helpers — extracted from main.py Phase 2 (Extraction 7).

Owns:
  - B1 state: _SHORTING_ENABLED, _feed_age_history, _systemic_stale_alerted
               _partial_fail_counts — previously main.py module globals
  - Accessors: get_shorting_enabled, set_shorting_enabled,
               get_partial_fail_counts, reset_partial_fail_counts
  - apply_mri_breakeven_push()   T3: push profitable stops to breakeven on MRI≥STRESSED

Pending (next session — require full verbatim read + param-extension surgery):
  - execute_entries()            ~1200 lines, needs 8 extra spy/scoring params
  - check_exits()                ~800 lines, needs 4 spy risk params
  - check_partial_exits()        ~500 lines, needs spy_event_type param

B2 fix: _live_score_cache moved to strategy/scoring.py (Extraction 6).
B1 fix: _SHORTING_ENABLED, _feed_age_history, _systemic_stale_alerted moved here.
        execute_entries/check_exits/check_partial_exits reference these
        via module state. Pending full body move + global decl removal (next session).

Broker imports: replace_stop_order, resolve_live_order, submit_day_stop_order, get_open_position,
get_open_orders from execution.broker.
Data imports: fetch_bars from data.fetcher; get_latest_trade from data.alpaca_data.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from data.fetcher import fetch_bars
from data.alpaca_data import get_latest_trade
from execution.broker import (
    PROTECTION_ALREADY_HELD,
    PROTECTION_UNKNOWN,
    get_open_orders,
    get_open_position,
    replace_stop_order,
    resolve_live_order,
    submit_day_stop_order,
)
from trade_logger import log_event as _log_trade_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# B1 state — previously main.py module globals
# ---------------------------------------------------------------------------

_SHORTING_ENABLED: bool = False        # set at startup + updated per cycle
_feed_age_history: object = None       # deque(maxlen=3) of per-cycle median bar ages
_systemic_stale_alerted: bool = False  # True while systemic feed degradation is active
_partial_fail_counts: dict = {}        # {symbol: int} partial close failures

_ET = ZoneInfo("America/New_York")
_shorts_banned_until: float = 0.0     # Unix ts; 0.0 = no ban; EOD auto-expiry


# Accessors for _SHORTING_ENABLED

def get_shorting_enabled() -> bool:
    """Return current shorting-enabled state."""
    return _SHORTING_ENABLED


def set_shorting_enabled(value: bool) -> None:
    """Update shorting-enabled state. Called from run_cycle() and startup."""
    global _SHORTING_ENABLED
    _SHORTING_ENABLED = value


# Accessors for _partial_fail_counts (read/written by check_partial_exits)

def get_partial_fail_counts() -> dict:
    """Return live reference to partial-fail counter dict."""
    return _partial_fail_counts


def reset_partial_fail_counts() -> None:
    """Clear partial fail counters at daily reset."""
    _partial_fail_counts.clear()


# Accessors for _shorts_banned_until (session short ban — Option A, S13)

def get_shorts_banned() -> bool:
    """Return True if a session short ban is active and not yet expired."""
    global _shorts_banned_until
    if _shorts_banned_until <= 0.0:
        return False
    if datetime.now(_ET).timestamp() >= _shorts_banned_until:
        _shorts_banned_until = 0.0
        logger.info("Session short ban auto-expired (EOD reached).")
        return False
    return True


def set_shorts_banned(until_ts: float, trigger: str) -> None:
    """Activate session short ban until until_ts (Unix timestamp, 23:59:59 ET today)."""
    global _shorts_banned_until
    _shorts_banned_until = until_ts
    _expiry = datetime.fromtimestamp(until_ts, _ET).strftime("%H:%M ET")
    logger.warning(
        f"Session short ban activated: trigger={trigger}, expires={_expiry}"
    )


def clear_shorts_banned() -> None:
    """Clear session short ban (daily reset or manual clear)."""
    global _shorts_banned_until
    if _shorts_banned_until > 0.0:
        logger.info("Session short ban cleared.")
    _shorts_banned_until = 0.0


# ---------------------------------------------------------------------------
# MRI breakeven push
# ---------------------------------------------------------------------------

_STOP_KEYS = ("rth_day_stop_order_id", "gtc_stop_order_id")
_TERMINAL = frozenset(("canceled", "expired", "done_for_day", "rejected"))
# Cannot be replaced right now (Alpaca API reference) — skip this cycle without counting a failure.
_NOT_REPLACEABLE_YET = frozenset(("pending_replace", "pending_cancel", "pending_new", "accepted"))
# The only statuses a live, replaceable stop can be in. Anything else (incl. a "replaced" order at
# the end of a broken replaced_by chain) is UNKNOWN — never paged as "protected" (masked-loss seat).
_REPLACEABLE = frozenset(("new", "held", "partially_filled"))
_BE_WARN_AFTER = 3            # consecutive failed moves while the old stop is live → WARNING
_BE_PAGE_EVERY_S = 1800.0     # re-page at most every 30 min per symbol and tier
_be_fail_counts: dict = {}    # {symbol: consecutive failed breakeven moves}
_be_last_page: dict = {}      # {(symbol, tier): monotonic ts of last page}


def _st(order) -> str:
    _s = getattr(order, "status", "")
    return str(getattr(_s, "value", _s)).lower()


def _be_page(symbol: str, critical: bool, body: str, kind: str = "") -> None:
    """Throttled operator page (Slack + phone), one throttle per symbol + severity + kind.
    Never raises."""
    now = time.monotonic()
    key = (symbol, "critical" if critical else "warning", kind)
    if now - _be_last_page.get(key, -1e18) < _BE_PAGE_EVERY_S:
        return
    _be_last_page[key] = now
    try:
        from alerts import SEV_CRITICAL, SEV_WARNING, _send
        _send(f"{SEV_CRITICAL if critical else SEV_WARNING} — BREAKEVEN STOP MOVE — {symbol}", body,
              priority=5 if critical else 4,
              tags=["rotating_light" if critical else "warning", "stop_sign"],
              emoji=":rotating_light:" if critical else ":warning:")
    except Exception as _pe:
        logger.error(f"[{symbol}] breakeven stop page failed to send: {_pe} | {body}")


def _be_failed(symbol: str, tier: str, detail: str) -> None:
    """Count a failed breakeven move and page by what the broker shows.
    tier "no_stop": the position HAD a broker stop and none is confirmed live now → CRITICAL now.
    tier "old_live": the old stop is confirmed live (still protected) → WARNING after 3 in a row.
    tier "software": software-stop position; the extra broker stop could not be added → WARNING
    after 3 in a row (the position keeps its by-design software stop).
    tier "unknown": the stored stop could not be read → WARNING after 3 in a row (never claims
    protection it has not seen)."""
    n = _be_fail_counts.get(symbol, 0) + 1
    _be_fail_counts[symbol] = n
    logger.error(f"[{symbol}] MRI BE push: move failed ({tier}, {n} in a row) — {detail}")
    try:
        _log_trade_event("breakeven_push_failed", symbol=symbol, outcome=tier,
                         consecutive_failures=n, detail=detail[:300])
    except Exception as _le:
        logger.warning(f"[{symbol}] breakeven_push_failed event not logged: {_le}")
    if tier == "no_stop":
        _be_page(symbol, True, f"NO live broker stop confirmed. {detail} The bot's software stop is "
                               f"checked about every 5 min — set a manual stop in Alpaca if this repeats.")
    elif n >= _BE_WARN_AFTER:
        where = {
            "old_live": "Broker stop is LIVE at the old level — position IS protected.",
            "software": "Position is on its software stop (by design); the breakeven broker stop could not be added.",
        }.get(tier, "Broker stop status could NOT be read — protection unverified; check Alpaca.")
        _be_page(symbol, False, f"Breakeven stop move failing {n}x. {detail} {where}")


def _position_qty(symbol: str, direction: str):
    """("open", qty) · ("gone", 0) · ("unknown", None) from the live Alpaca position. The net
    position on the OPPOSITE side of this trade (another tier on the same symbol flipped the net)
    is "unknown" — the stop is then never re-sized from it (adversarial review)."""
    try:
        pos = get_open_position(symbol)
    except Exception as _e:
        logger.warning(f"[{symbol}] MRI BE push: position read failed ({_e}) — qty not re-checked")
        return "unknown", None
    if pos is None:
        return "gone", 0
    try:
        _q = float(pos.qty)
    except (TypeError, ValueError):
        return "unknown", None
    _side = str(getattr(getattr(pos, "side", ""), "value", getattr(pos, "side", ""))).lower()
    if (_side and _side != direction) or (not _side and ((_q < 0) != (direction == "short"))):
        logger.warning(f"[{symbol}] MRI BE push: net position is {_side or _q} but trade is {direction} "
                       f"— qty not re-sized (another tier on this symbol)")
        return "unknown", None
    return "open", int(abs(_q))


def _adopt_untracked_stop(symbol: str, trade: dict, direction: str, max_qty: int) -> bool:
    """A submit was refused because a live reducing stop already holds the shares (the id was
    lost). Store that stop's id so the NEXT cycle moves it in place. Adopts ONLY a stop tagged to
    this (core/"intraday") tier and no larger than this tier's shares — never another tier's stop
    (day-tier OCO legs are untagged; QHM/F6 share symbols). True if exactly one found."""
    from execution.ownership_guard import tier_of_coid
    orders = get_open_orders(symbol) or []
    want = "sell" if direction == "long" else "buy"

    def _q(o) -> float:
        try:
            return abs(float(getattr(o, "qty", 0) or 0))
        except (TypeError, ValueError):
            return float("inf")
    stops = [o for o in orders
             if str(getattr(getattr(o, "side", ""), "value", getattr(o, "side", ""))).lower() == want
             and "stop" in str(getattr(getattr(o, "type", ""), "value", getattr(o, "type", ""))).lower()
             and tier_of_coid(getattr(o, "client_order_id", None)) == "intraday"
             and _q(o) <= max_qty]
    if len(stops) != 1:
        return False
    tif = str(getattr(getattr(stops[0], "time_in_force", ""), "value",
                      getattr(stops[0], "time_in_force", ""))).lower()
    key = "gtc_stop_order_id" if tif == "gtc" else "rth_day_stop_order_id"
    trade[key] = str(stops[0].id)
    logger.warning(f"[{symbol}] MRI BE push: adopted untracked live stop {stops[0].id} as {key} "
                   f"— will move it in place next cycle.")
    return True


def apply_mri_breakeven_push(tracker, mri) -> None:
    """T3: Move profitable positions to breakeven when MRI ≥ STRESSED.

    One-time per position (trade["be_pushed_by_mri"] flag prevents repeat).
    Requires ≥ 0.5×ATR profit buffer before pushing (board condition).
    Moves the stored broker stop(s) IN PLACE to breakeven (small random 1-5c offset to avoid
    round-number fills) — never cancel-then-resubmit (P0 2026-09-25, UBER 204 min unprotected).
    A position with no stored stop gets a new DAY stop (allow_cancel_blocking=False). The flag and
    trade["stop"] change only after the broker confirms; failures keep the old stop and page.
    """
    for symbol, trade in list(tracker.open_trades.items()):
        try:
            if trade.get("be_pushed_by_mri"):
                continue

            entry_price  = trade.get("entry_price", 0)
            direction    = trade.get("direction", "long")
            atr_value    = trade.get("atr_value") or 0.0
            current_stop = trade.get("trail_stop") or trade.get("stop")

            if atr_value <= 0:
                logger.debug(f"[{symbol}] MRI BE push skipped — no ATR value")
                continue

            current_price = None
            try:
                _df = fetch_bars(symbol, config.TF_15M, num_bars=2)
                if not _df.empty:
                    current_price = float(_df["close"].iloc[-1])
            except Exception as _e:
                logger.warning(f"[{symbol}] MRI BE push: price fetch failed: {_e}")
            if current_price is None:
                continue
            try:
                _live = get_latest_trade(symbol)
                if _live and _live > 0:
                    current_price = _live
            except Exception as _live_e:
                logger.warning(
                    "[%s] MRI breakeven: live price fetch failed — using stale bar close "
                    "(risk of incorrect push): %s",
                    symbol, _live_e
                )

            # Require ≥ 0.5×ATR profit buffer (board condition)
            min_buf = 0.5 * atr_value
            if direction == "long":
                if current_price < entry_price + min_buf:
                    continue
            else:
                if current_price > entry_price - min_buf:
                    continue

            # Stop already at or better than breakeven — just set flag, no resubmit. NOT when an
            # earlier push tightened only the software stop and the broker move is still pending.
            _pending = bool(trade.get("be_broker_pending"))
            if (not _pending and direction == "long" and current_stop is not None
                    and current_stop >= entry_price):
                trade["be_pushed_by_mri"] = True
                logger.info(
                    f"[{symbol}] MRI BE push: stop already ≥ breakeven — flag set"
                )
                continue
            if (not _pending and direction == "short" and current_stop is not None
                    and current_stop <= entry_price):
                trade["be_pushed_by_mri"] = True
                logger.info(
                    f"[{symbol}] MRI BE push: stop already ≤ breakeven — flag set"
                )
                continue

            import random as _rnd_mri
            _offset  = round(_rnd_mri.uniform(0.01, 0.05), 2)
            _stop_px = (round(entry_price - _offset, 2) if direction == "long"
                        else round(entry_price + _offset, 2))

            # P0 2026-09-25 (UBER 2026-09-18, 204 min unprotected): MOVE the stop in place — never
            # cancel-then-resubmit. Nothing is marked done until the broker confirms the move.
            _pstate, _pos_qty = _position_qty(symbol, direction)
            if _pstate == "gone":
                _be_fail_counts.pop(symbol, None)
                logger.info(f"[{symbol}] MRI BE push: no live position — skipped (exit path owns it)")
                continue
            # Tighten the SOFTWARE stop to breakeven now (a tighter software stop never hides a
            # loss — masked-loss seat); the broker move below stays pending until confirmed, and
            # be_pushed_by_mri is set only then, so a failed move is retried next cycle.
            if not _pending:
                trade["stop"] = entry_price
                trade["be_broker_pending"] = True
                tracker._save_log()
            _stored = [(k, str(trade.get(k))) for k in _STOP_KEYS if trade.get(k)]
            if len(_stored) == 2:
                _be_page(symbol, False, "Both a DAY and a GTC stop id are stored (anomaly) — moving each live one.",
                         kind="anomaly")

            _live_stops, _already, _had_stop, _skip = [], [], False, False
            for _key, _oid in _stored:
                _order, _live_id = resolve_live_order(_oid)
                if _order is None:
                    _be_failed(symbol, "unknown", f"{_key} {_oid} unreadable — status unknown")
                    _skip = True
                    break
                _had_stop = True
                _s = _st(_order)
                if _s == "filled":
                    _be_fail_counts.pop(symbol, None)
                    logger.warning(f"[{symbol}] MRI BE push: stop {_live_id} FILLED — position closing; "
                                   f"exit path books it from the actual fill.")
                    _skip = True
                    break
                if _s in _TERMINAL:
                    logger.warning(f"[{symbol}] MRI BE push: stored {_key} {_live_id} is {_s} — not in force.")
                    trade[_key] = None
                    continue
                if _s in _NOT_REPLACEABLE_YET:
                    logger.info(f"[{symbol}] MRI BE push: {_live_id} is {_s} — retry next cycle (not a failure)")
                    _skip = True
                    break
                if _s not in _REPLACEABLE:
                    _be_failed(symbol, "unknown", f"{_key} chain ends at {_live_id} with status {_s!r} — "
                                                  f"no live order confirmed")
                    _skip = True
                    break
                # NEVER LOOSEN: a stop already at/beyond the target (e.g. the trail ratchet moved it
                # above entry, or an earlier retry of this push already moved it) is left alone.
                _cur_raw = getattr(_order, "stop_price", None)
                try:
                    _cur_px = float(_cur_raw) if _cur_raw is not None else None
                except (TypeError, ValueError):
                    _cur_px = None
                trade[_key] = _live_id       # store the order actually in force (chain followed)
                if _cur_px is not None and ((direction == "long" and _cur_px >= _stop_px)
                                            or (direction == "short" and _cur_px <= _stop_px)):
                    _already.append((_key, _live_id, _cur_px))
                    continue
                _live_stops.append((_key, _live_id))
            if _skip:
                continue

            # This tier's shares, capped by the live position — never the whole Alpaca position (a
            # co-held day-tier lot on the same symbol must not be swept into this stop; masked-loss seat).
            try:
                _tier_qty = int(float(trade.get("qty_remaining") or trade.get("qty", 0) or 0))
            except (TypeError, ValueError):
                _tier_qty = 0
            _qty = min(_tier_qty, _pos_qty) if _pstate == "open" and _pos_qty is not None else _tier_qty
            _moved = [(k, i, i, px) for k, i, px in _already]   # (key, old_id, new_id, broker_px)
            if _live_stops:
                for _key, _live_id in _live_stops:
                    _new = replace_stop_order(symbol, _live_id, _stop_px,
                                              qty=_qty if _pstate == "open" and _qty >= 1 else None)
                    if _new is None:
                        break
                    _moved.append((_key, _live_id, str(getattr(_new, "id", "")),
                                   getattr(_new, "stop_price", _stop_px)))
                    trade[_key] = str(getattr(_new, "id", ""))
                _n_replaced = len(_moved) - len(_already)
                if _n_replaced != len(_live_stops):
                    tracker._save_log()   # persist any id that DID move before reporting the failure
                    # Say "protected" only if the broker still shows the unmoved stop live (the old
                    # stop may have filled or changed between the status read and the PATCH).
                    _unmoved = _live_stops[_n_replaced][1]
                    _o, _ = resolve_live_order(_unmoved)
                    _be_failed(symbol, "old_live" if _o is not None and _st(_o) in _REPLACEABLE else "unknown",
                               f"replace refused for {len(_live_stops) - _n_replaced} stop(s); old level kept "
                               f"(target ${_stop_px:.2f}).")
                    continue
            elif _already:
                pass                     # every live stop is already at/beyond breakeven
            elif _qty >= 1:
                _new_ord = submit_day_stop_order(
                    symbol=symbol, qty=int(_qty),
                    side="sell" if direction == "long" else "buy",
                    stop_price=_stop_px, allow_cancel_blocking=False,
                )
                if _new_ord is PROTECTION_ALREADY_HELD:
                    if not _adopt_untracked_stop(symbol, trade, direction, int(_qty)):
                        _be_failed(symbol, "old_live", "shares held by live reducing stop(s) that could not be "
                                                       "identified uniquely")
                    tracker._save_log()
                    continue
                if _new_ord is PROTECTION_UNKNOWN or _new_ord is None:
                    _be_failed(symbol, "no_stop" if _had_stop else "software",
                               f"DAY stop @ ${_stop_px:.2f} not placed "
                               f"({'status unknown' if _new_ord is PROTECTION_UNKNOWN else 'rejected'}).")
                    continue
                _moved.append(("rth_day_stop_order_id", "", str(getattr(_new_ord, "id", "")),
                               getattr(_new_ord, "stop_price", _stop_px)))
                trade["rth_day_stop_order_id"] = str(getattr(_new_ord, "id", ""))
            else:
                logger.warning(f"[{symbol}] MRI BE push: qty {_qty} < 1 — nothing to protect")
                continue

            # Confirmed at the broker — only now mark the push done.
            trade["stop"] = entry_price
            trade["be_pushed_by_mri"] = True
            trade.pop("be_broker_pending", None)
            try:
                trade["broker_stop_px"] = float(_moved[-1][3])
            except (TypeError, ValueError, IndexError):
                trade["broker_stop_px"] = _stop_px
            _be_fail_counts.pop(symbol, None)
            tracker._save_log()
            logger.warning(
                f"[{symbol}] MRI BE push: stop → breakeven ${entry_price:.2f} "
                f"(MRI={mri.level()} score={mri.score()}) | "
                + "; ".join(f"{k}: {o or 'new'} → {n}" for k, o, n, _ in _moved)
            )
            _log_trade_event(
                "breakeven_push", symbol=symbol, price=current_price,
                size=_qty, score=trade.get("score", 0),
                mri_level=mri.level(), data_source="alpaca_data",
                be_price=entry_price, stop_order=_moved[-1][2],
                old_stop_order=_moved[-1][1], broker_stop_px=trade["broker_stop_px"],
                method="replace" if _live_stops else ("already" if _already else "submit"),
            )
        except Exception as _be_err:   # one symbol must never abort the loop (and skip check_exits)
            logger.error(f"[{symbol}] MRI BE push: unexpected error — skipped this cycle: {_be_err}")
