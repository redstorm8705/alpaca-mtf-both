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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.broker import get_trading_client

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PT = ZoneInfo("America/Los_Angeles")  # tracker entry_time / overnight_since convention

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
# External-close lower-bound + unverified fallback (2026-07-03 phantom-fill fix)
# ---------------------------------------------------------------------------

def _derive_close_lower_bound(symbol: str, trade: dict) -> "datetime | None":
    """Derive a UTC lower-bound for the external-close CLOSED-orders query.

    A close necessarily occurs at/after entry, so the trade's own entry
    timestamp bounds the query and prevents matching an ancient same-symbol
    fill (the 2026-07-03 phantom bug: unbounded ASC returned a months-old fill
    as today's close — TSLA ~$347 = April entry; PANW -$182.79). Preference:
    entry_time -> overnight_since (both PT ISO strings, tracker convention).

    Returns a tz-aware UTC datetime (60s below the bound for NTP drift), or
    None when neither field is a usable timestamp — in which case the caller
    fails closed to _fill_unverified rather than querying unbounded. NEVER
    raises; NEVER returns a wrong-zone or future bound (reliability guard).
    """
    for _field in ("entry_time", "overnight_since"):
        _raw = trade.get(_field)
        if not _raw or not isinstance(_raw, str):
            continue
        try:
            _dt = datetime.fromisoformat(_raw)
        except (ValueError, TypeError):
            logger.critical(
                "[%s] close lower-bound: %s unparseable (%r) — trying next field.",
                symbol, _field, _raw,
            )
            continue
        # RC-1: never leave a naive datetime — bind to PT (tracker convention).
        if _dt.tzinfo is None:
            logger.warning(
                "[%s] close lower-bound: %s is naive (%r) — assuming PT.",
                symbol, _field, _raw,
            )
            _dt = _dt.replace(tzinfo=_PT)
        else:
            # Reject a wildly-wrong zone (expect PT: -07:00/-08:00, ±15min band).
            _utcoff = _dt.utcoffset()
            _off_h = _utcoff.total_seconds() / 3600.0 if _utcoff is not None else 0.0
            if not (-8.25 <= _off_h <= -6.75):
                logger.critical(
                    "[%s] close lower-bound: %s has non-PT offset %.2fh (%r) — "
                    "skipping field.", symbol, _field, _off_h, _raw,
                )
                continue
        _utc = _dt.astimezone(timezone.utc)
        _now = datetime.now(timezone.utc)
        if _utc > _now + timedelta(seconds=60):
            logger.critical(
                "[%s] close lower-bound: %s is in the future (%r, skew=%s) — "
                "clamping to now.", symbol, _field, _raw, _utc - _now,
            )
            _utc = _now
        return _utc - timedelta(seconds=60)
    return None


def _fill_unverified_fallback(symbol: str, trade: dict) -> float:
    """Fail-closed exit price: entry_price ONLY (never stop/trail_stop).

    Called when no fill can be matched, the recovered fill fails the sanity
    band, or no query lower-bound is derivable. Sets trade['_fill_unverified'],
    increments the ANOMALY-5 counter, and fires a CRITICAL log + Slack alert.
    """
    global _fill_fallback_count
    # An unverified fill must not carry a close-reason label recovered from a
    # rejected match — drop it so callers default to plain external_close.
    trade.pop("_recovered_close_reason", None)
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
        f"[{symbol}] FILL UNVERIFIED: could not recover a verified close fill. "
        f"Using {_fallback_src} as fallback. "
        f"P&L for this trade is unreliable — manual verification required. "
        f"[fallback #{_fill_fallback_count} today]"
    )
    _log_fill_anomaly(symbol, 0.0)
    try:
        from alerts import send_slack
        send_slack(
            f":rotating_light: FILL UNVERIFIED [{symbol}] "
            f"Could not recover a verified close fill. "
            f"P&L recorded using {_fallback_src}. "
            f"MANUAL VERIFICATION REQUIRED. "
            f"[fallback #{_fill_fallback_count} today]"
        )
    except Exception as _se:
        logger.warning(f"[{symbol}] Slack CRITICAL alert failed: {_se}")
    return _fallback


# ---------------------------------------------------------------------------
# Fill-price poller
# ---------------------------------------------------------------------------

def _recover_fill(
    symbol: str,
    trade: dict,
    poll_secs: float = 0.3,
    submitted_after: float | None = None,
    no_retry: bool = False,
) -> "float | None":
    """SF-02 / SF-03: Fetch the real market-order fill price from Alpaca after a close.

    CORE query logic — returns the verified fill price, or None on ANY failure
    (no derivable query bound / no matching fill after the poll budget / a >50%
    sanity-band rejection). Returns None WITHOUT the entry_price fallback or its
    _fill_unverified / CRITICAL / Slack side effects — those belong to the wrappers.
    Single source of truth (2026-07-16, board + Gro + GAI): the two public entry
    points below both call this, so the Alpaca query exists in exactly one place —
    the drift between two copies is precisely what caused Bug A.
    Diagnostic CRITICAL logs (no-bound, >50% deviation) stay here: they are pure
    logging (no state mutation) and are worth surfacing on both wrapper paths.

    Two query modes:
      submitted_after PROVIDED (same-session close-before-reentry, P5-H2):
        ASC by created_at, after=submitted_after — the close order was created
        before any re-entry, so the first filled result is the close. UNCHANGED.
      submitted_after None (external / unexpected close reconciliation):
        Bound the CLOSED-orders query by the trade's own entry timestamp
        (_derive_close_lower_bound: entry_time -> overnight_since) and take the
        MOST RECENT fill (filled_at DESC, limit=20) whose side matches the
        protective close side (sell=long, buy=short). Prevents the historic
        phantom where an unbounded ASC query returned a months-old same-symbol
        fill as today's close. If no lower bound can be derived, fail closed to
        _fill_unverified rather than query unbounded.

    Sanity band: a recovered fill >50% away from entry_price is rejected as
      unverified (universal backstop against any residual wrong-fill match —
      would have caught the TSLA $347 / PANW -$182.79 phantoms regardless).

    Exit-reason: when the matched close order is a stop (type contains 'stop'
      or carries a stop_price), sets trade['_recovered_close_reason'] =
      'gtc_stop_triggered' (else 'external_close') so callers can label the
      exit correctly instead of blanket 'external_close'.

    Budget-capped polling: Attempt 1 sleeps min(poll_secs, 2.5s); Attempt 2
      sleeps min(1.0s, remaining budget) unless no_retry=True. Total blocking
      hard-capped at _MAX_TOTAL_WAIT = 2.5s. Latency -> logs/fill_latency.log.

    Failure (no fill / sanity reject / no bound): returns None. The public
      wrappers below decide — fetch_actual_fill_price applies the entry_price
      fallback; fetch_actual_fill_price_or_none propagates None.
    """
    _MAX_TOTAL_WAIT = 2.5  # hard cap: total blocking per symbol per call
    _RETRY_WAIT = 1.0
    _start = time.monotonic()

    _external = submitted_after is None
    # Protective close side: a long closes with a sell, a short with a buy.
    _expected_side = "sell" if trade.get("direction") == "long" else "buy"

    _ext_lower_bound: "datetime | None" = None
    if _external:
        _ext_lower_bound = _derive_close_lower_bound(symbol, trade)
        if _ext_lower_bound is None:
            logger.critical(
                "[%s] fetch_actual_fill_price: no usable entry_time/overnight_since "
                "to bound external-close query — failing closed to _fill_unverified "
                "rather than risk an ancient-fill match.", symbol,
            )
            return None

    def _query_fills() -> "float | None":
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.common.enums import Sort
            _tc = get_trading_client()
            if _external:
                _req = GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED,
                    symbols=[symbol],
                    limit=20,                # 5 could truncate the real close
                    direction=Sort.DESC,     # most-recent orders first
                    after=_ext_lower_bound,  # never reach pre-entry history
                )
                _ords = _tc.get_orders(filter=_req)
                # Most-recent FILL first: an order created early can fill late,
                # so sort by filled_at (mirrors _fetch_fill_timestamp), NOT
                # created_at. None filled_at -> "" sinks to bottom under DESC.
                for _o in sorted(
                    _ords,
                    key=lambda x: (
                        str(getattr(x, "filled_at", None) or ""),
                        str(getattr(x, "id", "") or ""),
                    ),
                    reverse=True,
                ):
                    _fp = getattr(_o, "filled_avg_price", None)
                    if not _fp:
                        continue
                    # Side filter: skip a same-symbol re-entry leg being mistaken
                    # for the close (a long's close is a sell, a short's a buy).
                    if _expected_side not in str(getattr(_o, "side", "")).lower():
                        continue
                    # Exit-reason detection for correct downstream labeling.
                    _otype = str(
                        getattr(_o, "type", None)
                        or getattr(_o, "order_type", "")
                        or ""
                    ).lower()
                    _ostop = getattr(_o, "stop_price", None)
                    trade["_recovered_close_reason"] = (
                        "gtc_stop_triggered"
                        if ("stop" in _otype or _ostop) else "external_close"
                    )
                    return float(_fp)
                return None
            # submitted_after provided — legacy P5-H2 path, unchanged.
            assert submitted_after is not None  # narrowed: _external is False here
            _req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                limit=5,
                direction=Sort.ASC,  # oldest-first; close created before re-entry
                # 50ms grace: absorbs OCI↔Alpaca NTP drift without pulling in
                # prior same-symbol closes (DS Q5; 2.0s rejected — too wide).
                after=datetime.fromtimestamp(submitted_after - 0.05, tz=timezone.utc),
            )
            _ords = _tc.get_orders(filter=_req)
            for _o in sorted(
                _ords,
                # None created_at -> "9999-12-31" sinks to bottom under ASC sort.
                key=lambda x: (
                    str(getattr(x, "created_at", None) or "9999-12-31"),
                    str(getattr(x, "id", "") or ""),
                ),
                reverse=False,
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

    def _sanity_ok(price: float) -> bool:
        _ep = trade.get("entry_price")
        if _ep and abs(price - float(_ep)) > float(_ep) * 0.50:
            logger.critical(
                "[%s] Recovered fill $%.2f deviates >50%% from entry $%.2f — "
                "rejecting as unverified.", symbol, price, float(_ep),
            )
            return False
        return True

    # Attempt 1
    _wait1 = min(poll_secs, _MAX_TOTAL_WAIT)
    if _wait1 > 0:
        time.sleep(_wait1)
    _price = _query_fills()
    if _price is not None:
        if not _sanity_ok(_price):
            return None
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
            if not _sanity_ok(_price):
                return None
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

    # Both attempts exhausted — no fill recovered.
    return None


def fetch_actual_fill_price(
    symbol: str,
    trade: dict,
    poll_secs: float = 0.3,
    submitted_after: float | None = None,
    no_retry: bool = False,
) -> float:
    """Fetch the real close fill price from Alpaca, or the entry_price fallback.

    Unchanged public contract for the ~17 exit-path callers: on a recovered fill,
    returns it; on ANY failure, returns _fill_unverified_fallback(...) — the
    entry_price fallback, which ALSO sets trade['_fill_unverified'] and fires the
    CRITICAL + Slack. Callers pass the result straight to record_exit, so they need
    a usable price even when the fill is unknown. Behaviour is byte-for-byte what it
    was before the 2026-07-16 refactor — it now just delegates the query to
    _recover_fill and applies the fallback when that returns None.
    """
    _r = _recover_fill(
        symbol, trade, poll_secs=poll_secs,
        submitted_after=submitted_after, no_retry=no_retry,
    )
    return _r if _r is not None else _fill_unverified_fallback(symbol, trade)


def fetch_actual_fill_price_or_none(
    symbol: str,
    trade: dict,
    poll_secs: float = 0.1,
    submitted_after: float | None = None,
    no_retry: bool = True,
) -> "float | None":
    """Like fetch_actual_fill_price but returns None on failure instead of the
    entry_price fallback — and does NOT set trade['_fill_unverified'] or fire the
    CRITICAL + Slack.

    For the RC-4 reconciler (execution/fill_reconciler.py), which must tell "no fill
    found yet" (→ leave the trade pending, retry next cycle) apart from "a real fill
    that happened to be at ~entry" (a genuine scratch → patch it, pnl 0.00 but
    VERIFIED). The old code guessed via `abs(fill - entry) < _MIN_PRICE_DIFF`, which
    skipped a genuine scratch trade FOREVER (never patched → rode to RC-4 EXPIRED as a
    false CRITICAL, re-queued every restart). Board follow-up 2026-07-16 (board + Gro
    + GAI). Defaults mirror the reconciler's call site (poll_secs=0.1, no_retry=True).
    """
    return _recover_fill(
        symbol, trade, poll_secs=poll_secs,
        submitted_after=submitted_after, no_retry=no_retry,
    )
