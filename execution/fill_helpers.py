"""
execution/fill_helpers.py
Fill-price polling helper — extracted from main.py Phase 2.

Owns:
  - _fill_fallback_count      ANOMALY-5: cumulative fallback-price uses today
  - fetch_actual_fill_price() SF-02/SF-03: poll Alpaca closed orders for real fill
  - get_fill_fallback_count() read accessor for ANOMALY-5 check in run_cycle
  - reset_fill_fallback_count() called at daily reset

Broker imports: get_trading_client comes from execution.broker.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from execution.broker import get_trading_client

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# ANOMALY-5: cumulative fill-fallback counter (reset daily by main())
# ---------------------------------------------------------------------------

_fill_fallback_count: int = 0


def get_fill_fallback_count() -> int:
    """Return today's cumulative fill-fallback count (ANOMALY-5 read accessor)."""
    return _fill_fallback_count


def reset_fill_fallback_count() -> None:
    """Reset fallback counter at daily reset. Called from main() daily reset block."""
    global _fill_fallback_count
    _fill_fallback_count = 0


# ---------------------------------------------------------------------------
# Latency / anomaly logging helpers (non-blocking, best-effort)
# ---------------------------------------------------------------------------

def _log_fill_latency(symbol: str, attempt: int, elapsed_ms: float) -> None:
    try:
        _log = _PROJECT_ROOT / "logs" / "fill_latency.log"
        with _log.open("a") as _f:
            _f.write(
                f"{datetime.now(timezone.utc).isoformat()},"
                f"{symbol},{attempt},{elapsed_ms:.1f}\n"
            )
    except Exception as _log_e:
        logger.debug("fill_latency log write failed (non-critical): %s", _log_e)


def _log_fill_anomaly(symbol: str, elapsed_secs: float) -> None:
    try:
        _log = _PROJECT_ROOT / "logs" / "fill_anomalies.log"
        with _log.open("a") as _f:
            _f.write(
                f"{datetime.now(timezone.utc).isoformat()},"
                f"{symbol},{elapsed_secs:.2f}\n"
            )
    except Exception as _log_e:
        logger.debug("fill_anomaly log write failed (non-critical): %s", _log_e)


# ---------------------------------------------------------------------------
# Fill-price poller
# ---------------------------------------------------------------------------

def fetch_actual_fill_price(
    symbol: str,
    trade: dict,
    poll_secs: float = 0.3,
    submitted_after: float | None = None,
    no_retry: bool = False,
) -> float:
    """SF-02 / SF-03: Fetch the real market-order fill price from Alpaca after a close.

    Budget-capped polling (DS Finding 3 fix):
      Attempt 1: sleep min(poll_secs, 2.5s), query Alpaca closed orders.
      Attempt 2: sleep min(1.0s, remaining budget), retry if budget allows.
      no_retry=True skips Attempt 2 entirely — use in reconciliation passes where
      fills are 5+ min old (settled) and blocking >0.1s per symbol is unacceptable.
      Total blocking per call hard-capped at _MAX_TOTAL_WAIT = 2.5s.

    Latency logged to logs/fill_latency.log on every hit (48h empirical validation).
    Anomaly logged to logs/fill_anomalies.log + ANOMALY-5 counter on fallback.

    Fallback (both attempts exhausted):
      Returns entry_price ONLY — never trail_stop or stop.
      Sets trade["_fill_unverified"] = True + CRITICAL log + Slack alert.

    submitted_after: Unix timestamp captured just before close_position() was called.
      Filters closed-order query to orders submitted after that moment — prevents
      stale same-session fill for the same symbol (P5-H2 fill crosstalk).
      Pass None for SF-03 external close where no submission timestamp is available.
    """
    global _fill_fallback_count

    _MAX_TOTAL_WAIT = 2.5  # hard cap: total blocking per symbol per call
    _RETRY_WAIT = 1.0
    _start = time.monotonic()

    def _query_fills() -> float | None:
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            _tc = get_trading_client()
            _req_kwargs: dict = dict(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                limit=5,
            )
            if submitted_after is not None:
                _req_kwargs["after"] = datetime.fromtimestamp(
                    submitted_after, tz=timezone.utc
                )
            _req = GetOrdersRequest(**_req_kwargs)
            _ords = _tc.get_orders(filter=_req)
            for _o in sorted(
                _ords,
                key=lambda x: str(getattr(x, "filled_at", "") or ""),
                reverse=True,
            ):
                _fp = getattr(_o, "filled_avg_price", None)
                if _fp:
                    return float(_fp)
            return None
        except Exception as _e:
            logger.warning(
                f"[{symbol}] fetch_actual_fill_price: API error ({_e})"
            )
            return None

    # Attempt 1
    _wait1 = min(poll_secs, _MAX_TOTAL_WAIT)
    if _wait1 > 0:
        time.sleep(_wait1)
    _price = _query_fills()
    if _price is not None:
        _elapsed = time.monotonic() - _start
        logger.info(
            f"[{symbol}] Actual close fill: ${_price:.2f} "
            f"(attempt 1, {_elapsed * 1000:.0f}ms)"
        )
        _log_fill_latency(symbol, attempt=1, elapsed_ms=_elapsed * 1000)
        return _price

    logger.debug(
        f"[{symbol}] fetch_actual_fill_price: attempt 1 empty — checking retry budget"
    )

    # Attempt 2: only if budget remains
    _elapsed1 = time.monotonic() - _start
    _remaining = _MAX_TOTAL_WAIT - _elapsed1
    if not no_retry and _remaining > 0.05:
        _wait2 = min(_RETRY_WAIT, _remaining)
        time.sleep(_wait2)
        _price = _query_fills()
        if _price is not None:
            _elapsed = time.monotonic() - _start
            logger.info(
                f"[{symbol}] Actual close fill: ${_price:.2f} "
                f"(attempt 2, {_elapsed * 1000:.0f}ms)"
            )
            _log_fill_latency(symbol, attempt=2, elapsed_ms=_elapsed * 1000)
            return _price
    elif no_retry:
        logger.debug(
            f"[{symbol}] fetch_actual_fill_price: attempt 2 skipped (no_retry=True)"
        )
    else:
        logger.debug(
            f"[{symbol}] fetch_actual_fill_price: retry budget exhausted "
            f"({_elapsed1 * 1000:.0f}ms >= {_MAX_TOTAL_WAIT * 1000:.0f}ms)"
        )

    # Both attempts exhausted — entry_price only, never stop/trail_stop
    _total = time.monotonic() - _start
    _entry_px = trade.get("entry_price")
    _fallback = float(_entry_px or 0.0)
    _fallback_src = (
        f"entry_price ${_fallback:.2f}"
        if _entry_px
        else "0.0 (entry_price missing from trade dict)"
    )
    trade["_fill_unverified"] = True
    _fill_fallback_count += 1
    logger.critical(
        f"[{symbol}] FILL UNVERIFIED: Alpaca returned 0 fills after 2 attempts "
        f"({_total:.2f}s). Using {_fallback_src} as fallback. "
        f"P&L for this trade is unreliable — manual verification required. "
        f"[fallback #{_fill_fallback_count} today]"
    )
    _log_fill_anomaly(symbol, _total)
    try:
        from alerts import send_slack
        send_slack(
            f":rotating_light: FILL UNVERIFIED [{symbol}] "
            f"Alpaca returned 0 fills after 2 attempts. "
            f"P&L recorded using {_fallback_src}. "
            f"MANUAL VERIFICATION REQUIRED. "
            f"[fallback #{_fill_fallback_count} today]"
        )
    except Exception as _se:
        logger.warning(f"[{symbol}] Slack CRITICAL alert failed: {_se}")
    return _fallback
