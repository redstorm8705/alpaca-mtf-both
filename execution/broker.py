# ruff: noqa: E501
"""
execution/broker.py
Alpaca order execution layer. All buy/sell logic lives here.
Paper trading by default — change paper=True to paper=False in _get_trading_client()
when moving to live. Also update ALPACA_API_KEY / ALPACA_SECRET_KEY in .env.
"""

import os
import time
import uuid
import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopOrderRequest,
    GetOrdersRequest,
    LimitOrderRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

logger = logging.getLogger(__name__)


# ── Singleton client ──────────────────────────────────────────────────────────
# One TradingClient for the entire bot session — same pattern as data/fetcher.py
# Saves 5-10 auth round-trips per scan cycle and reduces failure surface.

_trading_client: "TradingClient | None" = None

# Per-symbol short block cache — populated when Alpaca returns 40310000
# (shorting not enabled for this specific security, not account-wide).
# Persists for the session. Prevents scan-noise and duplicate order attempts.
_short_blocked_symbols: set = set()


def get_short_blocked_symbols() -> set:
    """Return the set of symbols blocked from shorting this session."""
    return _short_blocked_symbols


def _get_trading_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        _trading_client = TradingClient(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
            paper=True,   # Change to paper=False when going live
        )
    return _trading_client


# Keep old name for any external callers
def get_trading_client() -> TradingClient:
    return _get_trading_client()


# ── Account / position queries ────────────────────────────────────────────────

def get_account():
    """Return account object with equity, buying_power, shorting_enabled, etc."""
    client = _get_trading_client()
    return client.get_account()


def get_portfolio_value() -> float:
    """Return current portfolio equity as float."""
    account = get_account()
    return float(account.equity)


def get_open_positions() -> list:
    """Return list of all currently open positions."""
    client = _get_trading_client()
    return client.get_all_positions()  # type: ignore[return-value]


def get_open_position(symbol: str):
    """Return open position for a specific symbol, None if not found, raises on API error."""
    try:
        client = _get_trading_client()
        return client.get_open_position(symbol)
    except Exception as e:
        err = str(e)
        _not_found = ("40410000", "position does not exist", "position not found", "no position")
        if any(s in err.lower() for s in _not_found) or ("404" in err and "position" in err.lower()):
            return None  # genuine 404 — position does not exist
        logger.warning(f"[{symbol}] get_open_position API error (non-404): {e}")
        raise  # activates fail-open logic already written in main.py callers


def get_open_orders(symbol: str | None = None) -> "list | None":
    """
    Return list of open (pending) orders, optionally filtered by symbol.
    Used by position reconciliation to detect pending orders before entry.
    An open order on a symbol means we must not submit another entry.
    """
    client = _get_trading_client()
    try:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = client.get_orders(filter=request)
        if symbol:
            return [o for o in orders if o.symbol == symbol]  # type: ignore[union-attr]
        return orders  # type: ignore[return-value]
    except Exception as e:
        logger.error(f"get_open_orders failed — API error, cannot confirm order state: {e}")
        return None  # None = unknown state; callers must not treat this as empty


def cancel_open_orders_for_symbol(symbol: str) -> int:
    """
    Cancel all open orders for a symbol. Returns count of cancelled orders.
    Used to clear held_for_orders locks when a partial close fails with 40310000.
    """
    orders = get_open_orders(symbol)
    if orders is None:
        logger.error(f"[{symbol}] Cannot cancel orders: get_open_orders returned None (API failure)")
        return 0
    cancelled = 0
    for order in orders:
        if cancel_order(str(order.id)):
            cancelled += 1
            logger.info(f"[{symbol}] Cancelled blocking order {order.id} (type={order.type})")
    return cancelled


def get_order(order_id: str):
    """
    Fetch a specific order by ID.
    Used at startup to check whether an overnight GTC stop was triggered.
    Returns None on failure.
    """
    client = _get_trading_client()
    try:
        return client.get_order_by_id(order_id)
    except Exception as e:
        logger.warning(f"get_order({order_id}) failed: {e}")
        return None


# ── Error classification for retry logic ─────────────────────────────────────

_NON_RETRYABLE = (
    "40310000", "not allowed to short",    # shorting disabled
    "403",                                  # auth failure
    "insufficient", "buying power",         # not enough capital
)
_RETRYABLE = ("429", "too many", "rate", "503", "timeout", "connection")


def _is_retryable(error_str: str) -> bool:
    err = error_str.lower()
    for substr in _NON_RETRYABLE:
        if substr in err:
            return False
    for substr in _RETRYABLE:
        if substr in err:
            return True
    return False


# ── Order submission ──────────────────────────────────────────────────────────

def submit_market_order(
    symbol: str,
    qty: int,
    side: str,          # "buy" or "sell"
) -> object:
    """
    Submit a plain market order with retry (max 3 attempts, 1s / 2s / 4s backoff).
    Distinguishes retryable errors (rate limits, transient network) from
    non-retryable (shorting disabled, insufficient funds).
    Returns order object or None on failure.
    """
    if qty <= 0:
        logger.warning(f"[{symbol}] Skipping order: qty={qty}")
        return None

    client     = _get_trading_client()
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    last_error = None
    # Generate idempotency key once — reused on every retry so Alpaca deduplicates
    # double-fires caused by network timeouts hitting the retry loop.
    # UUID avoids same-second collisions for back-to-back orders of identical symbol/side/qty.
    _idem_id   = f"mtf-{symbol}-{side}-{uuid.uuid4().hex[:12]}"

    for attempt in range(3):
        try:
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                client_order_id=_idem_id,
            )
            order = client.submit_order(order_data)
            logger.info(
                f"[{symbol}] {side.upper()} {qty} shares @ MARKET | Order ID: {order.id}"  # type: ignore[union-attr]
            )
            return order

        except Exception as e:
            err      = str(e)
            last_error = err

            if "40910000" in err or "duplicate client order" in err.lower():
                # Attempt 1 likely filled but response timed out — order may be live.
                # Do not retry; log clearly so operator can verify position in Alpaca.
                logger.warning(
                    f"[{symbol}] Duplicate client_order_id on retry (attempt {attempt + 1}) — "
                    f"order may already be live. Manual verification required. "
                    f"idem_id={_idem_id}"
                )
                return None
            elif "40310000" in err or "not allowed to short" in err:
                # Cache the block — prevents retry on every subsequent scan cycle.
                # 40310000 is issued both for account-level and per-symbol restrictions.
                _short_blocked_symbols.add(symbol)
                logger.warning(
                    f"[{symbol}] Shorting blocked (40310000) — "
                    f"added to session short-block cache. Will not retry this symbol."
                )
                return None
            elif not _is_retryable(err):
                logger.error(f"[{symbol}] Order failed (non-retryable): {e}")
                return None

            wait = 1 * (2 ** attempt)   # 1s, 2s, 4s
            logger.warning(
                f"[{symbol}] Order attempt {attempt + 1}/3 failed (retryable): {e} "
                f"— retrying in {wait}s"
            )
            time.sleep(wait)

    logger.error(f"[{symbol}] Order failed after 3 attempts. Last error: {last_error}")
    return None


def submit_limit_order(
    symbol: str,
    qty: int,
    side: str,              # "buy" or "sell"
    limit_price: float,
    extended_hours: bool = False,
) -> object:
    """
    Submit a DAY limit order. Used for overnight swing entries.
    extended_hours=True only works during active AH/PM sessions (4–8 PM ET, 4–9:30 AM ET).
    After 8 PM ET, submit without extended_hours — order queues for pre-market open.
    Returns order object or None on failure.
    """
    if qty <= 0 or not (0 < limit_price < 99_999):
        logger.warning(f"[{symbol}] Limit order rejected: qty={qty}, price={limit_price}")
        return None

    client     = _get_trading_client()
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    # AWP audit fix (2026-06-28): same idempotency pattern as
    # submit_market_order() — generated once, reused on every retry so
    # Alpaca deduplicates a retry that fires after a timeout/connection
    # error where the original order may have actually been accepted.
    # _is_retryable() (used below) explicitly includes "timeout" and
    # "connection" as retryable, so this is a genuinely reachable
    # ambiguous-success scenario, not theoretical.
    _idem_id   = f"mtf-{symbol}-{side}-{uuid.uuid4().hex[:12]}"

    order_data = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        limit_price=round(limit_price, 2),
        time_in_force=TimeInForce.DAY,
        extended_hours=extended_hours,
        client_order_id=_idem_id,
    )

    for _attempt in range(3):
        try:
            order = client.submit_order(order_data)
            logger.info(
                f"[{symbol}] LIMIT {side.upper()} {qty} @ ${limit_price:.2f} | "
                f"extended_hours={extended_hours} | Order ID: {order.id}"  # type: ignore[union-attr]
            )
            return order
        except Exception as e:
            err = str(e)
            if "40910000" in err or "duplicate client order" in err.lower():
                # AWP audit fix (2026-06-28): mirrors submit_market_order's
                # handling — Alpaca rejecting the idempotency key as a
                # duplicate means attempt 1 likely filled but the response
                # timed out before reaching us. Order may already be live;
                # do not retry. Manual verification required.
                logger.warning(
                    f"[{symbol}] Duplicate client_order_id on limit order retry "
                    f"(attempt {_attempt + 1}) — order may already be live. "
                    f"Manual verification required. idem_id={_idem_id}"
                )
                return None
            if "40310000" in err or "not allowed to short" in err:
                logger.warning(f"[{symbol}] Limit order rejected — shorting not enabled")
                return None
            elif not _is_retryable(err):
                # H-7: non-retryable errors (buying power, auth) must not burn 2s sleep × 3
                logger.error(f"[{symbol}] submit_limit_order failed (non-retryable): {e}")
                return None
            elif _attempt < 2:
                logger.warning(
                    f"[{symbol}] submit_limit_order attempt {_attempt + 1}/3 failed (retryable): {e} — retrying in 1s"
                )
                time.sleep(1)
            else:
                logger.error(f"[{symbol}] submit_limit_order failed after 3 attempts: {e}")
                return None
    return None  # exhausted retries without exception path firing


def submit_gtc_stop_order(
    symbol: str,
    qty: int,
    side: str,          # "sell" for long stops, "buy" for short stops
    stop_price: float,
) -> object:
    """
    Submit a GTC stop-market order for overnight position protection.
    Submitted at market close. Cancelled at next market open so it does
    not appear on Level II data during RTH.

    If the GTC stop triggers overnight, Alpaca fills it and the bot
    reconciles the tracker at startup (see reconcile_overnight_gtc_stops).

    Returns order object or None on failure.
    """
    import re as _re
    if qty <= 0 or stop_price <= 0:
        logger.warning(f"[{symbol}] GTC stop skipped: qty={qty}, stop=${stop_price}")
        return None

    client     = _get_trading_client()
    order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
    order_data = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        stop_price=round(stop_price, 2),
        time_in_force=TimeInForce.GTC,
    )

    try:
        order = client.submit_order(order_data)
        logger.info(
            f"[{symbol}] GTC STOP submitted: {side.upper()} {qty} @ ${stop_price:.2f} | "
            f"Order ID: {order.id}"  # type: ignore[union-attr]
        )
        return order
    except Exception as e:
        err = str(e)
        if "40310000" in err or "insufficient qty available" in err.lower():
            # GTC-RACE: cancel+resubmit timing race — held_for_orders not yet released.
            # Root cause (MSTR incident 2026-04-21): Alpaca paper cancel API returns 200
            # but the qty reservation persists far longer than 5s. Original 10×500ms poll
            # was insufficient — MSTR unprotected overnight 21:07→08:07 ET.
            # Fix (board vote 26-0, 2026-04-27):
            #   1. Cancel all OPEN blocking orders + related_orders from error body.
            #   2. Hard 3s wait — gives Alpaca async cancel time to propagate.
            #   3. Poll 60 × 1s (60s max) — calibrated to actual Alpaca paper release times.
            #   4. On exhaustion: Slack CRITICAL alert so operator can set manual stop.
            logger.warning(
                f"[{symbol}] GTC stop blocked (40310000 held_for_orders) — "
                f"clearing blocking orders and polling for release (max 60s)."
            )
            # PENDING-CANCEL GUARD (GTC-PENDING-CANCEL-FIX 2026-04-30):
            # Confirmed Alpaca SDK only returns "pending_cancel" as the transitional
            # cancel status (PENDING_CANCEL="pending_cancel", CANCELED="canceled").
            # 60s poll will never succeed while an order is in pending_cancel —
            # held_for_orders is not released until the cancel fully propagates.
            # Return None immediately so _cancel_and_reconcile_gtc_stops() preserves
            # the order ID and breaks the held_for_orders death spiral.
            # Board vote: 27-0 YES (2026-04-30).
            try:
                _existing_orders = get_open_orders(symbol)
                if _existing_orders:
                    for _chk_o in _existing_orders:
                        if "pending_cancel" in str(getattr(_chk_o, "status", "")).lower():
                            logger.warning(
                                f"[{symbol}] GTC stop blocked by PENDING_CANCEL order "
                                f"{_chk_o.id} — skipping 60s poll (held_for_orders will not "
                                f"release until cancel propagates). Position deferred to next cycle."
                            )
                            return None
            except Exception as _pcg:
                logger.debug(f"[{symbol}] pending_cancel guard check failed: {_pcg}")

            cancel_open_orders_for_symbol(symbol)
            _related = _re.findall(r'"related_orders"\s*:\s*\[([^\]]*)\]', err)
            if _related:
                for _oid in _re.findall(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', _related[0]):
                    cancel_order(_oid)
                    logger.info(f"[{symbol}] GTC-RACE: force-cancelled stuck related order {_oid}")

            # Hard 3s initial delay — Alpaca cancel is async; reservation rarely clears
            # in under 2s on paper. Eliminates the guaranteed-fail first-attempt pattern.
            time.sleep(3)

            for _poll in range(60):   # 60 × 1s = 60s max (was 10 × 500ms = 5s)
                time.sleep(1)
                try:
                    order = client.submit_order(order_data)
                    logger.info(
                        f"[{symbol}] GTC STOP submitted (after {3 + _poll + 1}s hold-clear): "
                        f"{side.upper()} {qty} @ ${stop_price:.2f} | Order ID: {order.id}"  # type: ignore[union-attr]
                    )
                    return order
                except Exception as _pe:
                    _pe_str = str(_pe)
                    if "40310000" not in _pe_str and "insufficient qty available" not in _pe_str.lower():
                        logger.error(f"[{symbol}] GTC stop poll retry failed (non-40310000): {_pe}")
                        return None
                    # Still held — continue polling

            # 60s exhausted — position is unprotected. Fire Slack CRITICAL.
            try:
                from alerts import alert_gtc_failed
                alert_gtc_failed(symbol, side, stop_price, f"held_for_orders not released after 63s: {e}")
            except Exception as _ae:
                logger.error(f"[{symbol}] alert_gtc_failed send failed: {_ae}")
            logger.error(
                f"[{symbol}] GTC stop FAILED after 63s poll — position UNPROTECTED overnight. "
                f"Set manual stop in Alpaca immediately. Original: {e}"
            )
            return None
        logger.error(f"[{symbol}] GTC stop order failed: {e}")
        return None


def submit_day_stop_order(
    symbol: str,
    qty: int,
    side: str,          # "sell" for long stops, "buy" for short stops
    stop_price: float,
) -> object:
    """
    Submit a DAY stop-market order for RTH session protection.
    Expires at 4:00 PM ET — no AH conflict with tonight's GTC submission.
    Used when overnight GTC stops were blocked last AH.
    Tracked in rth_day_stop_order_id; cleared at next pre-market.

    Returns order object or None on failure.
    """
    if qty <= 0 or stop_price <= 0:
        logger.warning(f"[{symbol}] DAY stop skipped: qty={qty}, stop=${stop_price}")
        return None

    client     = _get_trading_client()
    order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY

    order_data = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        stop_price=round(stop_price, 2),
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.submit_order(order_data)
        logger.info(
            f"[{symbol}] DAY STOP submitted: {side.upper()} {qty} @ ${stop_price:.2f} | "
            f"Order ID: {order.id}"  # type: ignore[union-attr]
        )
        return order
    except Exception as e:
        err = str(e)
        # DAY stop 40310000 fallback (Harris / Peterffy sprint item 1):
        # Paper accounts reserve buying_power against open GTC limit/target orders.
        # When all limits consume the cushion, the DAY stop gets insufficient buying_power.
        # Fix: cancel all blocking orders for the symbol, retry once. If retry succeeds,
        # the position gets stop protection. Caller must resubmit GTC limit after close.
        if "40310000" in err or "insufficient" in err.lower():
            logger.warning(
                f"[{symbol}] DAY stop blocked (40310000 / insufficient buying_power) — "
                f"cancelling blocking orders and retrying once."
            )
            freed = cancel_open_orders_for_symbol(symbol)
            logger.info(f"[{symbol}] Freed {freed} blocking order(s). Retrying DAY stop…")
            try:
                order = client.submit_order(order_data)
                logger.info(
                    f"[{symbol}] DAY STOP submitted (retry): {side.upper()} {qty} @ ${stop_price:.2f} | "
                    f"Order ID: {order.id}"  # type: ignore[union-attr]
                )
                return order
            except Exception as retry_e:
                logger.error(
                    f"[{symbol}] DAY stop retry FAILED after clearing orders: {retry_e} "
                    f"— position unprotected. Set manual stop in Alpaca immediately."
                )
                return None
        # 42210000 = "market is closed" — DAY stop submitted before RTH open (pre-market).
        # _submit_rth_day_stops() already retries each cycle; returning None here lets
        # the retry loop resubmit once the market opens. Not an error condition.
        if "42210000" in err or "market is closed" in err.lower():
            logger.warning(
                f"[{symbol}] DAY stop deferred (42210000 — market not yet open). "
                f"_submit_rth_day_stops() will retry at RTH open."
            )
            return None
        logger.error(f"[{symbol}] DAY stop order failed: {e}")
        return None


def cancel_order(order_id: str) -> bool:
    """
    Cancel a specific order by ID.
    Used to cancel GTC stops at market open so they don't appear on L2 during RTH.
    Returns True if cancelled (or already resolved — not an error).
    """
    client = _get_trading_client()
    try:
        client.cancel_order_by_id(order_id)
        logger.info(f"Order {order_id} cancelled.")
        return True
    except Exception as e:
        err = str(e)
        if "422" in err or "already" in err.lower() or "filled" in err.lower():
            logger.debug(f"Order {order_id} already resolved: {e}")
            return True
        logger.warning(f"Failed to cancel order {order_id}: {e}")
        return False


# ── Position management ───────────────────────────────────────────────────────

def partial_close_position(symbol: str, qty: int) -> bool:
    """
    Close a specific quantity of an open position (partial exit).
    Used for taking first-target profits while letting remainder run.
    Returns True if successful.

    Bug 4 fix (Apr 14 2026): detects 40310000 (held_for_orders) and auto-cancels
    all open blocking orders via cancel_open_orders_for_symbol(), then retries
    once.  Main.py pre-cancels the two tracked order IDs before calling here, but
    unknown blockers (orphaned GTC partials, manually placed orders) were not
    covered — resulting in silent partial-close failures every cycle until manual
    intervention.  cancel_open_orders_for_symbol() catches ALL open orders so the
    retry succeeds even when the blocker is untracked.
    """
    if qty < 1:
        logger.warning(f"[{symbol}] partial_close_position called with qty={qty} < 1 — skipping to prevent zero-share order.")
        return False  # match existing bool return contract; None breaks type-annotated callers
    client = _get_trading_client()
    try:
        pos = client.get_open_position(symbol)
        if pos is None:
            logger.warning(f"[{symbol}] No open position for partial close")  # type: ignore[unreachable]
            return False
        side = OrderSide.SELL if pos.side == "long" else OrderSide.BUY  # type: ignore[union-attr]
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_data)
        logger.info(f"[{symbol}] Partial close: {qty} shares | Order ID: {order.id}")  # type: ignore[union-attr]
        return True
    except Exception as e:
        err = str(e)
        # AWP audit fix (2026-06-28): apply the same not-found-as-success
        # handling close_position() already has (added for the watchdog
        # os.execv() restart race — see that function's docstring). The
        # exact same race applies here: if the bot restarts between this
        # partial close succeeding and record_exit() recording it, the new
        # process reloads the tracker entry as open and retries the partial
        # close, hitting "position not found." Without this check that fell
        # through to `return False`, leaving the tracker permanently stuck
        # believing the position is still open with stale qty.
        _not_found_signals = (
            "40410000",
            "position does not exist",
            "position not found",
            "no open position",
        )
        if any(sig in err.lower() for sig in _not_found_signals) or (
            "404" in err and "position" in err.lower()
        ):
            logger.warning(
                f"[{symbol}] partial_close_position: position not found on Alpaca — "
                f"already closed externally or by concurrent process. "
                f"Returning True so the caller's exit-recording path runs."
            )
            return True
        if "40310000" in err:
            logger.warning(
                f"[{symbol}] Partial close blocked (40310000 held_for_orders) — "
                f"cancelling all open orders and retrying once."
            )
            freed = cancel_open_orders_for_symbol(symbol)
            logger.info(f"[{symbol}] Freed {freed} blocking order(s). Retrying partial close…")
            try:
                order = client.submit_order(order_data)
                logger.info(
                    f"[{symbol}] Partial close retry OK: {qty} shares | Order ID: {order.id}"  # type: ignore[union-attr]
                )
                return True
            except Exception as retry_e:
                logger.error(
                    f"[{symbol}] Partial close retry FAILED after 40310000 clear: {retry_e}"
                )
                return False
        logger.error(f"[{symbol}] Partial close failed: {e}")
        return False


def close_position(symbol: str) -> bool:
    """
    Close an entire open position. Returns True if successful.

    CRITICAL: Also returns True when Alpaca reports the position does not exist
    (404 / 'position does not exist' / 'no open position'). This prevents the
    16x retry loop that fires when a watchdog os.execv() restarts the bot between
    close_position() success and record_exit() — the new process reloads the
    tracker entry as open, tries to close again, and hits 'not found'. Treating
    'not found' as True lets the caller reach record_exit() and clean up state.
    """
    client = _get_trading_client()
    try:
        client.close_position(symbol)
        logger.info(f"[{symbol}] Position closed.")
        return True
    except Exception as e:
        err = str(e)
        _not_found_signals = (
            "40410000",               # Alpaca position-not-found error code
            "position does not exist",
            "position not found",
            "no open position",
        )
        if any(sig in err.lower() for sig in _not_found_signals) or (
            "404" in err and "position" in err.lower()
        ):
            logger.warning(
                f"[{symbol}] close_position: position not found on Alpaca — "
                f"already closed externally or by concurrent process. "
                f"Returning True so record_exit() cleans up tracker."
            )
            return True
        # Bug 4 fix: auto-cancel blocking orders on 40310000 and retry once.
        if "40310000" in err:
            logger.warning(
                f"[{symbol}] close_position blocked (40310000 held_for_orders) — "
                f"cancelling all open orders and retrying once."
            )
            freed = cancel_open_orders_for_symbol(symbol)
            logger.info(f"[{symbol}] Freed {freed} blocking order(s). Retrying close…")
            try:
                client.close_position(symbol)
                logger.info(f"[{symbol}] Position closed (retry after 40310000 clear).")
                return True
            except Exception as retry_e:
                logger.error(
                    f"[{symbol}] close_position retry FAILED after 40310000 clear: {retry_e}"
                )
                return False
        logger.error(f"[{symbol}] Failed to close position: {e}")
        return False


def close_all_positions() -> bool:
    """Close all open positions. Used for kill switch."""
    client = _get_trading_client()
    try:
        client.close_all_positions(cancel_orders=True)
        logger.info("All positions closed.")
        return True
    except Exception as e:
        logger.error(f"Failed to close all positions: {e}")
        return False


def cancel_all_orders() -> bool:
    """Cancel all open orders."""
    client = _get_trading_client()
    try:
        client.cancel_orders()
        logger.info("All orders cancelled.")
        return True
    except Exception as e:
        logger.error(f"Failed to cancel orders: {e}")
        return False


# ── Market status ─────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Check if the market is currently open."""
    client = _get_trading_client()
    clock  = client.get_clock()
    return clock.is_open  # type: ignore[union-attr]


def get_clock() -> dict:
    """Return market clock info."""
    client = _get_trading_client()
    clock  = client.get_clock()
    return {
        "is_open":    clock.is_open,  # type: ignore[union-attr]
        "next_open":  clock.next_open,  # type: ignore[union-attr]
        "next_close": clock.next_close,  # type: ignore[union-attr]
    }


# ── Strategy adapter ──────────────────────────────────────────────────────────

class AlpacaBroker:
    """Thin adapter — wraps module-level functions for MoversStrategy compatibility."""

    def get_account(self):
        return get_account()

    def is_market_open(self) -> bool:
        return is_market_open()

    def buy(self, symbol: str, qty: int, price: float = 0) -> dict:
        """Normalizes submit_market_order()'s order-object-or-None return into
        the {"success": bool, "error": str|None, "order": object|None} shape
        MoversStrategy expects throughout (P0 fix, 2026-06-28 — confirmed via
        cold second-agent review that strategy.py's `result["success"]` /
        `result.get("error")` calls would TypeError on every entry attempt
        against the raw order-object-or-None return that existed before)."""
        order = submit_market_order(symbol, qty, "buy")
        if order is None:
            return {"success": False, "error": "submit_market_order returned None", "order": None}
        return {"success": True, "error": None, "order": order}

    def sell_short(self, symbol: str, qty: int, price: float = 0) -> dict:
        order = submit_market_order(symbol, qty, "sell")
        if order is None:
            return {"success": False, "error": "submit_market_order returned None", "order": None}
        return {"success": True, "error": None, "order": order}

    def place_stop(self, symbol: str, qty: int, stop_price: float, side: str):
        """GTC (not DAY) — Movers positions must stay protected across a
        script restart/crash, since MoversStrategy has no in-memory state
        persistence. 2026-06-28 redesign (Rafael mandate)."""
        return submit_gtc_stop_order(symbol, qty, side, stop_price)

    def cancel_stop_order(self, order_id: str) -> bool:
        return cancel_order(order_id)

    def get_open_positions(self) -> list:
        return get_open_positions()

    def get_open_orders(self, symbol: str | None = None) -> "list | None":
        return get_open_orders(symbol)

    def get_position(self, symbol: str):
        return get_open_position(symbol)

    def close_position(self, symbol: str) -> dict:
        """Normalizes close_position()'s plain-bool return into the
        {"success": bool} shape MoversStrategy expects (same P0 fix as
        buy()/sell_short() above)."""
        ok = close_position(symbol)
        return {"success": ok}
