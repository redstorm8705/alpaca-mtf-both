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

Broker imports: cancel_order, submit_day_stop_order from execution.broker.
Data imports: fetch_bars from data.fetcher; get_latest_trade from data.alpaca_data.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from data.fetcher import fetch_bars
from data.alpaca_data import get_latest_trade
from execution.broker import (
    cancel_order,
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

def apply_mri_breakeven_push(tracker, mri) -> None:
    """T3: Move profitable positions to breakeven when MRI ≥ STRESSED.

    One-time per position (trade["be_pushed_by_mri"] flag prevents repeat).
    Requires ≥ 0.5×ATR profit buffer before pushing (board condition).
    Cancels existing stop orders and resubmits a new DAY stop at breakeven
    (with small random offset to avoid exact round-number fills).
    """
    for symbol, trade in list(tracker.open_trades.items()):
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

        # Stop already at or better than breakeven — just set flag, no resubmit
        if (direction == "long" and current_stop is not None
                and current_stop >= entry_price):
            trade["be_pushed_by_mri"] = True
            logger.info(
                f"[{symbol}] MRI BE push: stop already ≥ breakeven — flag set"
            )
            continue
        if (direction == "short" and current_stop is not None
                and current_stop <= entry_price):
            trade["be_pushed_by_mri"] = True
            logger.info(
                f"[{symbol}] MRI BE push: stop already ≤ breakeven — flag set"
            )
            continue

        # Cancel existing stop orders (same CRITICAL-1 pattern as partial close)
        _cancel_ok = True
        for _skey in ("rth_day_stop_order_id", "gtc_stop_order_id"):
            _soid = trade.get(_skey)
            if _soid:
                if not cancel_order(_soid):
                    logger.error(
                        f"[{symbol}] MRI BE push: cannot cancel "
                        f"{_skey} {_soid} — skipping."
                    )
                    _cancel_ok = False
                    break
                trade[_skey] = None
                logger.info(
                    f"[{symbol}] MRI BE push: {_skey} {_soid} cancelled."
                )
        if not _cancel_ok:
            continue

        trade["stop"] = entry_price
        trade["be_pushed_by_mri"] = True

        import random as _rnd_mri
        _offset  = round(_rnd_mri.uniform(0.01, 0.05), 2)
        _stop_px = (round(entry_price - _offset, 2) if direction == "long"
                    else round(entry_price + _offset, 2))
        _qty = trade.get("qty_remaining") or trade.get("qty", 0)
        if _qty >= 1:
            _new_ord = submit_day_stop_order(
                symbol=symbol, qty=_qty,
                side="sell" if direction == "long" else "buy",
                stop_price=_stop_px,
            )
            if _new_ord:
                trade["rth_day_stop_order_id"] = str(getattr(_new_ord, "id", ""))
                logger.warning(
                    f"[{symbol}] MRI BE push: stop → breakeven "
                    f"${entry_price:.2f} "
                    f"(MRI={mri.level()} score={mri.score()}) "
                    f"| DAY stop {getattr(_new_ord, 'id', 'unknown')}"
                )
                _log_trade_event(
                    "breakeven_push", symbol=symbol, price=current_price,
                    size=_qty, score=trade.get("score", 0),
                    mri_level=mri.level(), data_source="alpaca_data",
                    be_price=entry_price, stop_order=str(getattr(_new_ord, "id", "")),
                )
            else:
                logger.error(
                    f"[{symbol}] MRI BE push: DAY stop resubmit FAILED "
                    f"@ ${_stop_px:.2f} "
                    f"— {_qty} shares unprotected. Set manual stop in Alpaca."
                )
        tracker._save_log()
