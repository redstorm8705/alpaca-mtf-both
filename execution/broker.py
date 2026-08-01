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


# ── Protective-stop submission sentinels ──────────────────────────────────────
# Returned by submit_gtc_stop_order / submit_day_stop_order ONLY when a caller
# passed allow_cancel_blocking=False AND Alpaca rejected with 40310000
# (held_for_orders). It means: "no order was placed by you, because the qty is
# ALREADY held by a live reducing order — almost certainly a valid protective
# stop." That is a fundamentally different outcome from None (= genuine
# placement failure, position may be unprotected), and conflating the two makes
# a protection-asserting caller page a FALSE 'position UNPROTECTED' every cycle.
#
# Callers that opt out MUST test identity explicitly, FOUR ways:
#     order = submit_gtc_stop_order(..., allow_cancel_blocking=False)
#     if   order is PROTECTION_ALREADY_HELD: ...  # protected by an existing stop — no page
#     elif order is PROTECTION_UNKNOWN:      ...  # can't tell — throttled unknown path
#     elif order is None:                    ...  # genuine failure — PAGE
#     else:                                  ...  # newly placed
#
# ⚠ DO NOT rely on `__bool__` or on an `is not None` check to route this.
# __bool__ is False, which happens to be safe for the 14 legacy callers that use
# `if order:` truthiness — but it does NOT save a caller that tests
# `if order is not None:`, because the sentinel IS not-None. Such a caller would
# take its success branch and then read `getattr(order, "id", "")` -> "" and
# persist an EMPTY order id over a live stop's real one — corrupting exactly the
# protection state this sentinel exists to preserve. The sentinels are __slots__-ed
# with NO `.id` attribute precisely so that misuse as an order object fails loudly
# rather than silently at runtime. Any caller passing allow_cancel_blocking=False
# MUST add the `is PROTECTION_ALREADY_HELD` / `is PROTECTION_UNKNOWN` branches in
# the SAME commit.
class _ProtectionSentinel:
    """Non-order outcome of a protective-stop submit. Deliberately has NO `.id` attribute
    so misuse as an order object fails loudly rather than silently persisting an empty
    order id over a live stop's real one."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name

    def __bool__(self) -> bool:
        return False


# qty is held by a STABLE live reducing order => the position IS protected. Do not page.
PROTECTION_ALREADY_HELD = _ProtectionSentinel("PROTECTION_ALREADY_HELD")

# qty is held, but the order book could not be read => protection status is UNKNOWN. This is
# NOT a claim of protection and NOT a claim of failure. A caller should route this to its own
# throttled unknown-state path — in stop_protection.py that is the dedicated
# _unknown_page_streak (pages once per episode, cleared on any definite outcome), NOT
# _skip_unknown (whose streak is already cleared before the submit) and NOT the
# "placement failed / position UNPROTECTED" path.
# Added 2026-07-20 at GAI's pre-ship request: distinguishing "unknown" from "failed" keeps the
# never-mask-a-loss rule intact (we never assert unverified protection) while avoiding an
# unthrottled false 'UNPROTECTED' page on transient API noise.
PROTECTION_UNKNOWN = _ProtectionSentinel("PROTECTION_UNKNOWN")


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


def _make_idem_id(tier: str, symbol: str, side: str) -> str:
    """Tier-tagged idempotency client_order_id: {TIER2}-{symbol}-{s}-{epoch_ms}-{uuid8}.
    Generated ONCE per order and reused across retries (Alpaca deduplicates double-fires).
    The tier prefix lets fills be attributed to the owning strategy tier
    (execution.ownership_guard.tier_of_coid parses it) for the Phase 0-a ownership layer.
    Today every caller defaults to "intraday" except QHM (passes "qhm"). Fail-safe: an
    unknown tier falls back to intraday rather than crashing order submission."""
    import time as _t
    from execution.ownership_guard import make_coid
    _epoch = int(_t.time() * 1000)
    _uniq = uuid.uuid4().hex[:8]
    try:
        return make_coid(tier, symbol, side, _epoch, _uniq)
    except ValueError:
        logger.warning("[%s] unknown tier %r for client_order_id — tagging intraday",
                       symbol, tier)
        return make_coid("intraday", symbol, side, _epoch, _uniq)


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


_HOLD_STABLE = "stable"
_HOLD_UNSTABLE = "unstable"
_HOLD_NO_COVER = "no_cover"
_HOLD_UNKNOWN = "unknown"
_HOLD_QTY_EPS = 1e-6


def _hold_state(symbol: str, side: str, qty: float) -> str:
    """Classify whether a held_for_orders (40310000) rejection is backed by a live protective
    stop that ACTUALLY COVERS the position. Used only by the allow_cancel_blocking=False
    opt-out paths, whose STABLE answer becomes a POSITIVE claim of protection.

      _HOLD_STABLE   — the book is readable, nothing is mid-cancel, AND live orders on the
                       REDUCING side (`side`) of type *stop* cover at least `qty`. Only this
                       justifies telling a caller "you are protected, do not page."
      _HOLD_UNSTABLE — a blocking order is 'pending_cancel': its held qty is about to
                       release, so the position is about to be UNprotected, not protected.
      _HOLD_NO_COVER — the book is readable but NO reducing-side stop covers the qty. The
                       40310000 is explained by something else (a resting profit limit, an
                       entry order, a wrong-side/partial order). The qty is held, but NOT by
                       protection — so this must NOT read as protected.
      _HOLD_UNKNOWN  — the book could not be read (None / raise). We can neither assert
                       protection nor assert its loss.

    HARDENED 2026-07-20 (PRE-WIRE-BLOCKER-3, found by cold-2nd): the prior version returned
    STABLE for ANY list without a pending_cancel — INCLUDING AN EMPTY LIST — and never checked
    side, type or qty. That let PROTECTION_ALREADY_HELD be returned with zero protecting orders
    on the book: an unverified claim of protection, i.e. exactly the never-mask-a-loss
    violation the sentinel exists to prevent. Protection is now PROVEN, not assumed.

    Never raises.
    """
    try:
        _orders = get_open_orders(symbol)
        if _orders is None:
            logger.warning(
                f"[{symbol}] hold-state probe: get_open_orders unavailable — protection "
                f"status UNKNOWN (not claiming protection, not claiming failure)."
            )
            return _HOLD_UNKNOWN
        if any("pending_cancel" in str(getattr(_o, "status", "")).lower() for _o in _orders):
            return _HOLD_UNSTABLE

        # Prove a reducing-side STOP actually covers the qty. `side` is already the reducing
        # side by this function's contract (submit_*_stop_order is called with "sell" to
        # protect a long, "buy" to protect a short).
        # Match ANY stop-family type on the reducing side: plain "stop", "stop_limit", and
        # "trailing_stop" all contain "stop" and all genuinely defend the position, so counting
        # them TOWARD cover is correct (and safer than requiring exact "stop", which would MISS a
        # real trailing stop and wrongly read a protected position as naked). The one caveat —
        # a stop_limit can gap through its limit and not fill — makes this a mild UNDER-page risk
        # at worst (we'd stay quiet on a stop_limit that later gaps), never an over-claim on an
        # empty/wrong-side book, which is the failure this function exists to prevent.
        _want = str(side).lower()
        _cover = 0.0
        for _o in _orders:
            _os = getattr(_o, "side", "")
            if str(getattr(_os, "value", _os)).lower() != _want:
                continue
            _ot = getattr(_o, "type", "")
            if "stop" not in str(getattr(_ot, "value", _ot)).lower():
                continue
            try:
                _cover += abs(float(getattr(_o, "qty", 0) or 0))
            except (TypeError, ValueError):
                # Reliability seat 2026-07-20: log the dropped order so a malformed protective
                # stop is not silently invisible (a false NO_COVER would otherwise be
                # undebuggable). Fail-closed direction is unchanged — it contributes nothing.
                logger.debug(
                    f"[{symbol}] hold-state probe: dropped order {getattr(_o, 'id', '?')} — "
                    f"unparseable qty {getattr(_o, 'qty', None)!r}; contributes nothing to cover."
                )
                continue   # unparseable qty contributes nothing — cannot count toward proof

        if _cover + _HOLD_QTY_EPS >= float(qty):
            return _HOLD_STABLE

        # Reliability seat 2026-07-20: attach the book we actually saw (id/side/type/qty/status)
        # so a FALSE NO_COVER — a real stop misclassified, e.g. an unexpected type string — is
        # debuggable from logs alone, instead of only surfacing the aggregate shortfall.
        _book = "; ".join(
            f"{getattr(_o, 'id', '?')}:"
            f"{str(getattr(getattr(_o, 'side', ''), 'value', getattr(_o, 'side', ''))).lower()}/"
            f"{str(getattr(getattr(_o, 'type', ''), 'value', getattr(_o, 'type', ''))).lower()}/"
            f"{getattr(_o, 'qty', '?')}/"
            f"{str(getattr(_o, 'status', '')).lower()}"
            for _o in _orders
        ) or "<empty>"
        logger.warning(
            f"[{symbol}] hold-state probe: qty is held (40310000) but live {_want}-side stops "
            f"cover only {_cover:g} of {float(qty):g} share(s) — the hold is NOT protection "
            f"(likely a resting limit/entry order). NOT claiming protection. Book seen: {_book}"
        )
        return _HOLD_NO_COVER
    except Exception as _hse:
        logger.warning(
            f"[{symbol}] hold-state probe raised ({_hse}) — protection status UNKNOWN; "
            f"will not claim protection."
        )
        return _HOLD_UNKNOWN


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


def _parse_insufficient_qty(err: str) -> "tuple[int | None, int | None, int | None]":
    """Parse (held_for_orders, available, existing_qty) from an Alpaca 40310000
    'insufficient qty available' rejection body. Alpaca returns this error for TWO
    distinct causes and the body's fields tell them apart. By Alpaca's own accounting
    `available == existing_qty - held_for_orders`, so:
      - a TRANSIENT held_for_orders reservation (a stale reducing order still holds the
        qty; a cancel releases it): held_for_orders > 0 (=> available < existing_qty);
      - a PERMANENT qty mismatch (the caller asked to protect more shares than the
        position actually holds — tracker qty > broker qty): held_for_orders == 0, which
        FORCES available == existing_qty (nothing is reserved), and available < requested.
    Real body (SMCI 2026-07-29):
        {"available":"1","code":40310000,"existing_qty":"1","held_for_orders":"0",
         "message":"insufficient qty available for order (requested: 2, available: 1)"}.
    existing_qty is returned so the caller can CROSS-CHECK available == existing_qty
    before clamping (belt-and-suspenders: only clamp to `available` when the broker
    confirms the position is exactly that many shares, fully unreserved). Reads the
    JSON-style fields first, falling back to the message text for `available`. Returns
    None for any field that cannot be parsed so callers keep pre-existing behavior when
    the shape is unrecognized. RC-6: field names verified against the live rejection
    body. Never raises."""
    import re as _re2
    _hfo = _avail = _exist = None
    try:
        _m = _re2.search(r'"held_for_orders"\s*:\s*"?(\d+)"?', err)
        if _m:
            _hfo = int(_m.group(1))
        _m = _re2.search(r'"existing_qty"\s*:\s*"?(\d+)"?', err)
        if _m:
            _exist = int(_m.group(1))
        _m = _re2.search(r'"available"\s*:\s*"?(\d+)"?', err)
        if _m:
            _avail = int(_m.group(1))
        if _avail is None:   # fall back to the human-readable message text
            _m = _re2.search(r'available:\s*(\d+)', err)
            if _m:
                _avail = int(_m.group(1))
    except Exception as _pe:
        logger.debug("_parse_insufficient_qty: parse failed (%s) — returning (None, None, None)", _pe)
        return None, None, None
    return _hfo, _avail, _exist


# ── Order submission ──────────────────────────────────────────────────────────

def submit_market_order(
    symbol: str,
    qty: int,
    side: str,          # "buy" or "sell"
    tier: str = "intraday",   # owning strategy tier — tags client_order_id for attribution
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
    # Tier-tagged idempotency key, generated once — reused on every retry so Alpaca
    # deduplicates double-fires caused by network timeouts hitting the retry loop.
    _idem_id   = _make_idem_id(tier, symbol, side)

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
    tier: str = "intraday",   # owning strategy tier — tags client_order_id for attribution
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
    # ambiguous-success scenario, not theoretical. Now tier-tagged.
    _idem_id   = _make_idem_id(tier, symbol, side)

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


def _floor_bound_stop_qty(symbol: str, qty: int, side: str, tier: str) -> int:
    """Prereq #3 (F6 arming; design logs/f6_prereq1_syncgap_design_2026-07-17.md): bound a
    protective STOP's qty by the never-sell floor so a RESTING sell-stop on a protected
    symbol (forever6/qhm) can never fill INTO the floor. This is the hole the close-path
    chokepoint (close_position) does NOT cover: a stop is submitted once and fires later,
    so it must be floor-bounded AT SUBMISSION. DORMANT unless config.OWNERSHIP_GUARD_ENFORCE
    (mirrors close_position / partial_close_position). Returns the qty to submit (>=1), or
    0 to SKIP the stop entirely (fully floor-blocked, or fail-CLOSED on a protected-symbol
    read error). ONLY sell-stops (side=='sell', long-reducing) are gated — a buy-stop (short
    cover) passes through untouched (short stops are live in production; ring-fenced names
    are long-only, so a protected symbol never carries a short).

    NEVER-RAISES CONTRACT (cold board Reliability seat 2026-07-18): the body only reaches
    the ledger/Alpaca reads when enforcement is armed, and a schema-valid but TYPE-corrupt
    ledger (e.g. a tier qty of "abc") slips load_ledger's schema check and makes
    float()/tier_qty raise ValueError PAST the inner `except LedgerError`. This wrapper
    guarantees the "0 = fail closed" contract holds even then: any unexpected raise fails
    CLOSED (skip the sell-stop) for a protected symbol and OPEN (submit requested qty) for
    a non-protected one — it never propagates out and crashes the stop-submission path."""
    import config
    if not getattr(config, "OWNERSHIP_GUARD_ENFORCE", False):
        return qty
    if str(side).lower() != "sell":
        return qty
    try:
        return _floor_bound_stop_qty_impl(symbol, qty, side, tier)
    except Exception as e:
        try:
            from execution import ownership_guard as _og
            _protected = symbol in _og._cached_protected_symbols()
        except Exception:
            _protected = False
        if _protected:
            try:
                _og.page_floor_blind(symbol, tier, "ledger_type_corrupt",
                                     f"stop-submit floor helper raised: {e}")
            except Exception:
                pass
            logger.critical(
                f"[{symbol}] stop-submit({tier}): floor helper raised for a PROTECTED "
                f"symbol — SKIPPING the sell-stop (fail closed): {e}"
            )
            return 0
        logger.warning(
            f"[{symbol}] stop-submit({tier}): floor helper raised, symbol not protected "
            f"— proceeding with requested qty: {e}"
        )
        return qty


def _floor_bound_stop_qty_impl(symbol: str, qty: int, side: str, tier: str) -> int:
    """Body of _floor_bound_stop_qty — MUST be called ONLY through that wrapper, which
    enforces the never-raises fail-closed contract and has already short-circuited the
    enforcement-off and non-sell cases."""
    from execution import ownership_guard as _og
    try:
        _ledger = _og.load_ledger()
    except _og.LedgerError as e:
        if symbol in _og._cached_protected_symbols():
            _og.page_floor_blind(symbol, tier, "ledger_unreadable", f"stop-submit: {e}")
            logger.critical(
                f"[{symbol}] stop-submit({tier}): ledger unreadable for a PROTECTED symbol "
                f"— SKIPPING the sell-stop to preserve the never-sell floor: {e}"
            )
            return 0
        logger.debug(
            f"[{symbol}] stop-submit: ledger unreadable, symbol not protected — "
            f"proceeding with requested qty: {e}"
        )
        return qty
    if _og.protected_floor(_ledger, symbol) <= _og._QTY_EPS:
        return qty
    try:
        _pos = get_open_position(symbol)
    except Exception as e:
        _og.page_floor_blind(symbol, tier, "alpaca_unreadable", f"stop-submit: {e}")
        logger.critical(
            f"[{symbol}] stop-submit({tier}): Alpaca net unreadable for a PROTECTED symbol "
            f"— SKIPPING the sell-stop (fail closed): {e}"
        )
        return 0
    if _pos is None:
        logger.warning(
            f"[{symbol}] stop-submit({tier}): protected symbol has no live position — "
            f"no sell-stop to place."
        )
        return 0
    _net = abs(float(_pos.qty))
    _res = _og.check_never_sell_floor(symbol, tier, int(qty), "sell", alpaca_net_qty=_net)
    if _res.action == "REJECT":
        logger.warning(
            f"[{symbol}] stop-submit({tier}) SKIPPED by never-sell floor: {_res.reason}"
        )
        return 0
    _bq = int(_res.qty)
    if _bq < int(qty):
        logger.warning(
            f"[{symbol}] stop-submit({tier}): never-sell floor bounded the sell-stop "
            f"{int(qty)}→{_bq} share(s) ({_res.reason})."
        )
    return _bq if _bq >= 1 else 0


def submit_gtc_stop_order(
    symbol: str,
    qty: int,
    side: str,          # "sell" for long stops, "buy" for short stops
    stop_price: float,
    tier: str = "intraday",   # owning strategy tier — tags client_order_id for attribution
    *,
    allow_cancel_blocking: bool = True,
) -> object:
    """
    Submit a GTC stop-market order for overnight position protection.
    Submitted at market close. Cancelled at next market open so it does
    not appear on Level II data during RTH.

    If the GTC stop triggers overnight, Alpaca fills it and the bot
    reconciles the tracker at startup (see reconcile_overnight_gtc_stops).

    Returns an order object, or None on failure, or — when
    allow_cancel_blocking=False (see the sentinels at the top of this module) —
    PROTECTION_ALREADY_HELD (a stable live reducing order already holds the qty)
    or PROTECTION_UNKNOWN (the qty is held but the order book was unreadable, so
    protection can be neither confirmed nor denied).

    allow_cancel_blocking (keyword-only, default True = pre-existing behavior):
      On 40310000 (held_for_orders) the DEFAULT recovery cancels ALL open orders
      for the symbol and polls up to 63s for the qty reservation to release
      (MSTR incident 2026-04-21, board vote 26-0). That recovery is correct for a
      caller that KNOWS the blocking order is its own stale stop, and it is
      UNCHANGED.
      It is UNSAFE for a caller that is merely ASSERTING an invariant — e.g.
      execution/stop_protection.py, which re-derives protection from Alpaca each
      cycle. There, a 40310000 most likely means a VALID protective stop already
      holds the qty, and cancelling it would DESTROY live protection (and, if the
      63s poll then exhausts, leave the position genuinely naked). Such callers
      pass False: nothing is cancelled, and the call returns immediately —
      PROTECTION_ALREADY_HELD for a held_for_orders rejection, None for any other
      failure. The caller re-derives protection on its next cycle.
    """
    import re as _re
    if qty <= 0 or stop_price <= 0:
        logger.warning(f"[{symbol}] GTC stop skipped: qty={qty}, stop=${stop_price}")
        return None

    # The qty we were ASKED to protect, captured BEFORE any floor-bounding. _hold_state proves
    # cover against THIS, never the possibly-shrunk qty: floor-bounding only ever REDUCES the
    # submitted qty, so proving cover against the reduced number could report "protected" while
    # part of the position is actually naked. Requiring cover of the full requested qty is the
    # strictly-safer test (never over-claims protection).
    _requested_qty = qty

    # Prereq #3 (F6 arming): floor-bound a RESTING sell-stop so it can never fire INTO the
    # never-sell floor (forever6+qhm). Self-gating (DORMANT unless OWNERSHIP_GUARD_ENFORCE):
    # returns qty unchanged for buy-stops / non-protected symbols, or 0 to skip entirely.
    qty = _floor_bound_stop_qty(symbol, qty, side, tier)
    if qty <= 0:
        return None

    client     = _get_trading_client()
    order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY
    order_data = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        stop_price=round(stop_price, 2),
        time_in_force=TimeInForce.GTC,
        client_order_id=_make_idem_id(tier, symbol, side),
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

            # OPT-OUT GUARD (2026-07-20): a protection-ASSERTING caller must never be able to
            # cancel an order it did not place. Deliberately placed AFTER the pending_cancel
            # guard above: if a blocking order is mid-cancel its held qty is about to release,
            # so the position is NOT reliably protected — that case correctly returns None
            # there (caller pages) rather than reaching the sentinel here. Reaching this point
            # means the qty is held by a STABLE live reducing order, which for such a caller is
            # almost certainly a valid protective stop. Cancel nothing, poll nothing.
            #
            # Discriminate on the ERROR CODE, mirroring submit_day_stop_order: only a literal
            # 40310000 proves held_for_orders. A bare "insufficient qty available" text match
            # without the code is NOT proof the qty is held, so it must NOT claim protection —
            # it returns None so the caller pages (fail loud, never falsely reassure).
            if not allow_cancel_blocking:
                # NOTE the pending_cancel guard above returns None on a READABLE pending_cancel,
                # but NOT when get_open_orders returns None — `if _existing_orders:` treats that
                # as falsy and falls through to here. So this block must handle every case.
                if "40310000" in err:
                    _hs = _hold_state(symbol, side, _requested_qty)
                    if _hs == _HOLD_STABLE:
                        logger.warning(
                            f"[{symbol}] GTC stop not placed (40310000 held_for_orders) and "
                            f"allow_cancel_blocking=False — NOT cancelling blocking orders: "
                            f"the qty is held by a stable live reducing order, most likely a "
                            f"valid protective stop. Returning PROTECTION_ALREADY_HELD."
                        )
                        return PROTECTION_ALREADY_HELD
                    if _hs == _HOLD_UNKNOWN:
                        logger.warning(
                            f"[{symbol}] GTC stop not placed (40310000) and allow_cancel_"
                            f"blocking=False, but the order book is UNREADABLE — protection "
                            f"status UNKNOWN. NOT cancelling, NOT claiming protection. "
                            f"Returning PROTECTION_UNKNOWN; caller should route this to its "
                            f"throttled unknown-state path. Original: {e}"
                        )
                        return PROTECTION_UNKNOWN
                    logger.error(
                        f"[{symbol}] GTC stop FAILED (40310000) and allow_cancel_blocking="
                        f"False, and the hold is NOT protection ({_hs}: a blocking order is "
                        f"mid-cancel, or no reducing-side stop covers the qty) — NOT "
                        f"cancelling, NOT claiming protection. Position may be unprotected. "
                        f"Original: {e}"
                    )
                    return None
                # Bare "insufficient qty available" text WITHOUT a 40310000 code — cannot prove
                # the qty is held at all, so never claim protection.
                logger.error(
                    f"[{symbol}] GTC stop FAILED ('insufficient qty available' WITHOUT a "
                    f"40310000 code) and allow_cancel_blocking=False — NOT cancelling blocking "
                    f"orders, NOT claiming protection. Position may be unprotected. "
                    f"Original: {e}"
                )
                return None

            # QTY-MISMATCH SELF-HEAL (2026-08-01, SMCI naked-stop 2026-07-29): the same
            # 40310000/"insufficient qty available" is returned for a PERMANENT qty mismatch —
            # the caller asked to protect MORE shares than the position actually holds
            # (held_for_orders==0, available<qty). The cancel+63s poll below CANNOT fix that:
            # the missing share does not exist, so every retry re-fails identically and the
            # REAL shares sit naked overnight (SMCI: requested 2, available 1 → 63s poll → naked).
            # Detect it and immediately place a stop for the shares that DO exist. Strictly safer:
            # the available shares get a floor NOW instead of after a guaranteed-fail poll, and we
            # never over-protect a phantom share. held_for_orders>0 is the transient case and
            # correctly falls through to the existing cancel+poll (which polls for the full qty).
            # Cross-check existing_qty: clamp ONLY when the broker confirms the position is
            # exactly `available` shares AND nothing is reserved (held_for_orders==0 =>
            # available==existing_qty by Alpaca's accounting). If existing_qty is present and
            # does NOT equal available, the held_for_orders==0 read is not a clean permanent
            # mismatch (an inconsistent/transient snapshot) — do NOT clamp; fall through to the
            # existing cancel+poll, which resubmits the full qty. If existing_qty is absent,
            # held_for_orders==0 alone already implies available==existing_qty.
            _hfo, _avail, _exist = _parse_insufficient_qty(err)
            if (_hfo == 0 and _avail is not None and 1 <= _avail < qty
                    and (_exist is None or _exist == _avail)):
                logger.warning(
                    f"[{symbol}] GTC stop qty mismatch (held_for_orders=0, existing_qty={_exist}, "
                    f"requested {qty}, available {_avail}) — tracker qty exceeds broker position. "
                    f"Placing a stop for the {_avail} share(s) that exist rather than polling 63s "
                    f"for a phantom {qty - _avail}."
                )
                try:
                    _corrected = StopOrderRequest(
                        symbol=symbol,
                        qty=_avail,
                        side=order_side,
                        stop_price=round(stop_price, 2),
                        time_in_force=TimeInForce.GTC,
                        client_order_id=_make_idem_id(tier, symbol, side),
                    )
                    order = client.submit_order(_corrected)
                    if order:   # submit_order raises on failure; guard mirrors submit_market_order
                        logger.info(
                            f"[{symbol}] GTC STOP submitted (qty-corrected {qty}->{_avail}): "
                            f"{side.upper()} {_avail} @ ${stop_price:.2f} | Order ID: {order.id}"  # type: ignore[union-attr]
                        )
                        return order
                    logger.error(
                        f"[{symbol}] GTC qty-corrected resubmit ({_avail} share(s)) returned no "
                        f"order — falling through to the cancel+poll recovery."
                    )
                except Exception as _qce:
                    logger.error(
                        f"[{symbol}] GTC qty-corrected resubmit ({_avail} share(s)) FAILED: "
                        f"{_qce} — falling through to the cancel+poll recovery."
                    )
                    # fall through to the existing recovery below

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
    tier: str = "intraday",   # owning strategy tier — tags client_order_id for attribution
    *,
    allow_cancel_blocking: bool = True,
) -> object:
    """
    Submit a DAY stop-market order for RTH session protection.
    Expires at 4:00 PM ET — no AH conflict with tonight's GTC submission.
    Used when overnight GTC stops were blocked last AH.
    Tracked in rth_day_stop_order_id; cleared at next pre-market.

    Returns an order object, or None on failure, or — when
    allow_cancel_blocking=False (see the sentinels at the top of this module) —
    PROTECTION_ALREADY_HELD (a stable live reducing order already holds the qty)
    or PROTECTION_UNKNOWN (the qty is held but the order book was unreadable, so
    protection can be neither confirmed nor denied).

    allow_cancel_blocking (keyword-only, default True = pre-existing behavior):
      See submit_gtc_stop_order for the full rationale. Default True leaves the
      cancel-blocking-orders-and-retry recovery below completely UNCHANGED.
      NOTE the asymmetry with the GTC path: this branch also fires on a bare
      "insufficient" match, which catches insufficient BUYING POWER — a genuine
      failure that is NOT held_for_orders. So the opt-out path below discriminates:
      only a real 40310000 yields PROTECTION_ALREADY_HELD; any other "insufficient"
      yields None (a true failure the caller SHOULD page). A 40310000 whose hold
      cannot be verified yields PROTECTION_UNKNOWN. Either way an opted-out caller
      cancels nothing.
    """
    if qty <= 0 or stop_price <= 0:
        logger.warning(f"[{symbol}] DAY stop skipped: qty={qty}, stop=${stop_price}")
        return None

    # The qty we were ASKED to protect, captured BEFORE any floor-bounding. _hold_state proves
    # cover against THIS, never the possibly-shrunk qty: floor-bounding only ever REDUCES the
    # submitted qty, so proving cover against the reduced number could report "protected" while
    # part of the position is actually naked. Requiring cover of the full requested qty is the
    # strictly-safer test (never over-claims protection).
    _requested_qty = qty

    # Prereq #3 (F6 arming): floor-bound a RESTING sell-stop so it can never fire INTO the
    # never-sell floor (forever6+qhm). Self-gating (DORMANT unless OWNERSHIP_GUARD_ENFORCE):
    # returns qty unchanged for buy-stops / non-protected symbols, or 0 to skip entirely.
    qty = _floor_bound_stop_qty(symbol, qty, side, tier)
    if qty <= 0:
        return None

    client     = _get_trading_client()
    order_side = OrderSide.SELL if side == "sell" else OrderSide.BUY

    order_data = StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        stop_price=round(stop_price, 2),
        time_in_force=TimeInForce.DAY,
        client_order_id=_make_idem_id(tier, symbol, side),
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
            # OPT-OUT GUARD (2026-07-20): see submit_gtc_stop_order. A protection-asserting
            # caller cancels NOTHING here, whatever the reason. Discriminate the two errors
            # this branch conflates so the caller pages correctly: a real 40310000 means the
            # qty is already held (protected → sentinel, no page); any other "insufficient"
            # (e.g. buying power) is a genuine failure (→ None, caller SHOULD page).
            if not allow_cancel_blocking:
                # Unlike the GTC path there is no pending_cancel guard upstream here, so this
                # block is the sole discrimination point for the DAY stop.
                if "40310000" in err:
                    _hs = _hold_state(symbol, side, _requested_qty)
                    if _hs == _HOLD_STABLE:
                        logger.warning(
                            f"[{symbol}] DAY stop not placed (40310000 held_for_orders) and "
                            f"allow_cancel_blocking=False — NOT cancelling blocking orders: "
                            f"the qty is held by a stable live reducing order, most likely a "
                            f"valid protective stop. Returning PROTECTION_ALREADY_HELD."
                        )
                        return PROTECTION_ALREADY_HELD
                    if _hs == _HOLD_UNKNOWN:
                        logger.warning(
                            f"[{symbol}] DAY stop not placed (40310000) and allow_cancel_"
                            f"blocking=False, but the order book is UNREADABLE — protection "
                            f"status UNKNOWN. NOT cancelling, NOT claiming protection. "
                            f"Returning PROTECTION_UNKNOWN; caller should route this to its "
                            f"throttled unknown-state path. Original: {e}"
                        )
                        return PROTECTION_UNKNOWN
                    logger.error(
                        f"[{symbol}] DAY stop FAILED (40310000) and allow_cancel_blocking="
                        f"False, and the hold is NOT protection ({_hs}: a blocking order is "
                        f"mid-cancel, or no reducing-side stop covers the qty) — NOT "
                        f"cancelling, NOT claiming protection. Position may be unprotected. "
                        f"Original: {e}"
                    )
                    return None
                logger.error(
                    f"[{symbol}] DAY stop FAILED (insufficient, non-40310000 — e.g. buying "
                    f"power) and allow_cancel_blocking=False — NOT cancelling blocking "
                    f"orders. This is a GENUINE placement failure, position may be "
                    f"unprotected. Original: {e}"
                )
                return None
            # QTY-MISMATCH SELF-HEAL (2026-08-01) — mirror of submit_gtc_stop_order. A DAY stop
            # can hit the same PERMANENT qty mismatch (tracker qty > broker qty). The single
            # cancel+retry below re-fails identically (cancelling frees nothing when
            # held_for_orders==0), leaving the RTH position naked. Place a stop for the shares
            # that exist. A buying-power "insufficient" (no `available` qty field) parses to
            # _avail=None and correctly skips this, falling through to the cancel+retry.
            # Cross-check existing_qty (see submit_gtc_stop_order for the full rationale):
            # clamp only when held_for_orders==0 AND (existing_qty absent OR == available).
            _hfo, _avail, _exist = _parse_insufficient_qty(err)
            if (_hfo == 0 and _avail is not None and 1 <= _avail < qty
                    and (_exist is None or _exist == _avail)):
                logger.warning(
                    f"[{symbol}] DAY stop qty mismatch (held_for_orders=0, existing_qty={_exist}, "
                    f"requested {qty}, available {_avail}) — placing a stop for the {_avail} "
                    f"share(s) that exist rather than retrying for a phantom {qty - _avail}."
                )
                try:
                    _corrected = StopOrderRequest(
                        symbol=symbol,
                        qty=_avail,
                        side=order_side,
                        stop_price=round(stop_price, 2),
                        time_in_force=TimeInForce.DAY,
                        client_order_id=_make_idem_id(tier, symbol, side),
                    )
                    order = client.submit_order(_corrected)
                    if order:   # submit_order raises on failure; guard mirrors submit_market_order
                        logger.info(
                            f"[{symbol}] DAY STOP submitted (qty-corrected {qty}->{_avail}): "
                            f"{side.upper()} {_avail} @ ${stop_price:.2f} | Order ID: {order.id}"  # type: ignore[union-attr]
                        )
                        return order
                    logger.error(
                        f"[{symbol}] DAY qty-corrected resubmit ({_avail} share(s)) returned no "
                        f"order — falling through to the cancel+retry recovery."
                    )
                except Exception as _qce:
                    logger.error(
                        f"[{symbol}] DAY qty-corrected resubmit ({_avail} share(s)) FAILED: "
                        f"{_qce} — falling through to the cancel+retry recovery."
                    )
                    # fall through to the existing recovery below

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

def _floor_bound_partial_qty(symbol: str, qty: int, tier: str) -> int:
    """Floor-bound a PARTIAL-close qty (increment 4a; DORMANT unless OWNERSHIP_GUARD_ENFORCE —
    the caller gates on that + _bypass_floor). Returns: the qty to submit (>=1); 0 to
    REJECT/skip (caller returns False); or -1 = 'position already gone' (caller returns True,
    preserving the pre-existing not-found→True contract). NEVER-RAISES (Reliability seat
    2026-07-18): a type-corrupt ledger fails CLOSED (0) for a cached-protected symbol (page it)
    and OPEN (requested qty) otherwise — never propagates and crashes the exit path. Same
    proven structure as _floor_bound_stop_qty."""
    try:
        return _floor_bound_partial_qty_impl(symbol, qty, tier)
    except Exception as e:
        try:
            from execution import ownership_guard as _og
            _protected = symbol in _og._cached_protected_symbols()
        except Exception:
            _protected = False
        if _protected:
            try:
                _og.page_floor_blind(symbol, tier, "ledger_type_corrupt",
                                     f"partial-close floor helper raised: {e}")
            except Exception:
                pass
            logger.critical(
                f"[{symbol}] partial_close({tier}): floor helper raised for a PROTECTED "
                f"symbol — refusing partial (fail closed): {e}"
            )
            return 0
        logger.warning(
            f"[{symbol}] partial_close({tier}): floor helper raised, symbol not protected "
            f"— proceeding with requested qty: {e}"
        )
        return qty


def _floor_bound_partial_qty_impl(symbol: str, qty: int, tier: str) -> int:
    """Body of _floor_bound_partial_qty — call ONLY through that never-raises wrapper."""
    from execution import ownership_guard as _og
    try:
        _ledger = _og.load_ledger()
    except _og.LedgerError as e:
        if symbol in _og._cached_protected_symbols():
            _og.page_floor_blind(symbol, tier, "ledger_unreadable", f"partial-close: {e}")
            logger.critical(
                f"[{symbol}] partial_close({tier}): ledger unreadable for a PROTECTED "
                f"symbol — refusing partial to preserve the floor: {e}"
            )
            return 0
        logger.debug(
            f"[{symbol}] partial_close: ledger unreadable, symbol not protected — "
            f"proceeding with requested qty: {e}"
        )
        return qty
    if _og.protected_floor(_ledger, symbol) <= _og._QTY_EPS:
        return qty
    try:
        _pos = get_open_position(symbol)
    except Exception as e:
        _og.page_floor_blind(symbol, tier, "alpaca_unreadable", f"partial-close: {e}")
        logger.critical(
            f"[{symbol}] partial_close({tier}): Alpaca net unreadable for a PROTECTED "
            f"symbol — refusing (fail closed): {e}"
        )
        return 0
    if _pos is None:
        return -1   # already gone → caller returns True (preserve not-found→True contract)
    _net = abs(float(_pos.qty))
    _side = "sell" if _pos.side == "long" else "buy"
    _res = _og.check_never_sell_floor(symbol, tier, qty, _side, alpaca_net_qty=_net)
    if _res.action == "REJECT":
        logger.error(
            f"[{symbol}] partial_close({tier}) BLOCKED by never-sell floor: {_res.reason}"
        )
        return 0
    _bq = int(_res.qty)
    if _bq < int(qty):
        logger.warning(
            f"[{symbol}] partial_close({tier}): never-sell floor bounded {int(qty)}→{_bq} "
            f"share(s) ({_res.reason})."
        )
    return _bq if _bq >= 1 else 0


def partial_close_position(symbol: str, qty: int, tier: str = "intraday",
                           *, _bypass_floor: bool = False) -> bool:
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

    # NEVER-SELL-FLOOR bounding (increment 4a). DORMANT unless OWNERSHIP_GUARD_ENFORCE.
    # _bypass_floor=True skips this (the chokepoint close_position already bounded qty).
    # Delegated to the never-raises _floor_bound_partial_qty helper (Reliability seat
    # 2026-07-18) so a type-corrupt ledger fails closed/open, never crashes this exit path.
    import config
    if getattr(config, "OWNERSHIP_GUARD_ENFORCE", False) and not _bypass_floor:
        _fq = _floor_bound_partial_qty(symbol, qty, tier)
        if _fq == -1:
            return True   # position already gone — matches the not-found→True contract below
        if _fq < 1:
            return False
        qty = _fq

    client = _get_trading_client()
    try:
        pos = client.get_open_position(symbol)
        if pos is None:
            logger.warning(f"[{symbol}] No open position for partial close")  # type: ignore[unreachable]
            return False
        side = OrderSide.SELL if pos.side == "long" else OrderSide.BUY  # type: ignore[union-attr]
        _side_str = "sell" if pos.side == "long" else "buy"  # type: ignore[union-attr]
        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=_make_idem_id(tier, symbol, _side_str),
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


def _raw_close_position(symbol: str) -> bool:
    """
    Raw full close of the ENTIRE Alpaca position — NO tier / never-sell-floor
    awareness. This is the unguarded primitive; all normal exits should call
    close_position() (the chokepoint), which delegates here only when the floor
    does not bind. Returns True if successful.

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


def close_position(symbol: str, *, tier: str = "intraday") -> bool:
    """Close an open position — THE never-sell-floor CHOKEPOINT (increment 4a).

    When config.OWNERSHIP_GUARD_ENFORCE is False (default), this is a raw full close
    with ZERO behavior change (delegates straight to _raw_close_position). When True,
    every reducing order routes through execution.ownership_guard.check_never_sell_floor
    so `tier` can NEVER sell shares owned by a protected tier (qhm/forever6):
      - non-protected symbol  → full close (fail OPEN — nothing to protect);
      - protected multi-tier  → bounded to the tier's own shares via a partial;
      - floor-binding case    → REJECT (return False) rather than breach the floor.
    Fails CLOSED for a genuinely-protected symbol whose ledger/Alpaca-net read fails;
    fails OPEN (full close) for a non-protected symbol on a ledger read error. `tier`
    is keyword-only so a legacy positional call can never silently mis-tag.
    """
    import config
    if not getattr(config, "OWNERSHIP_GUARD_ENFORCE", False):
        return _raw_close_position(symbol)   # DORMANT — exact pre-4a behavior
    # Function-boundary never-raises wrapper (Reliability seat 2026-07-18): any unexpected
    # raise in the floor-aware body (e.g. a type-corrupt ledger qty making protected_floor
    # raise) fails CLOSED for a cached-protected symbol (page it) and OPEN (raw close) for a
    # non-protected one (keystone) — never propagates and crashes the exit path.
    try:
        return _close_position_impl(symbol, tier)
    except Exception as e:
        try:
            from execution import ownership_guard as _og
            _protected = symbol in _og._cached_protected_symbols()
        except Exception:
            _protected = False
        if _protected:
            try:
                _og.page_floor_blind(symbol, tier, "ledger_type_corrupt",
                                     f"close_position body raised: {e}")
            except Exception:
                pass
            logger.critical(
                f"[{symbol}] close_position({tier}): body raised for a PROTECTED symbol — "
                f"refusing close (fail closed): {e}"
            )
            return False
        logger.warning(
            f"[{symbol}] close_position({tier}): body raised, symbol not protected — raw "
            f"close (fail open): {e}"
        )
        return _raw_close_position(symbol)


def _close_position_impl(symbol: str, tier: str) -> bool:
    """Body of close_position (increment 4a chokepoint) — call ONLY through close_position's
    never-raises wrapper, which short-circuits the enforce-off case and guarantees no raise
    escapes to the caller."""
    from execution import ownership_guard as _og
    # Protection decision from the ledger (authoritative). Only a genuinely-protected
    # symbol needs the live Alpaca net, so the common non-protected path stays cheap
    # and fails OPEN.
    try:
        _ledger = _og.load_ledger()
    except _og.LedgerError as e:
        if symbol in _og._cached_protected_symbols():
            _og.page_floor_blind(symbol, tier, "ledger_unreadable", f"close_position: {e}")
            logger.critical(
                f"[{symbol}] close_position({tier}): ledger unreadable for a PROTECTED "
                f"symbol — refusing close to preserve the never-sell floor: {e}"
            )
            return False
        return _raw_close_position(symbol)   # not protected → full close (fail open)

    if _og.protected_floor(_ledger, symbol) <= _og._QTY_EPS:
        return _raw_close_position(symbol)   # nothing to protect → full close

    # Protected symbol → the guard needs the live Alpaca net qty.
    try:
        _pos = get_open_position(symbol)
    except Exception as e:
        _og.page_floor_blind(symbol, tier, "alpaca_unreadable", f"close_position: {e}")
        logger.critical(
            f"[{symbol}] close_position({tier}): Alpaca net unreadable for a PROTECTED "
            f"symbol — refusing close (fail closed): {e}"
        )
        return False
    if _pos is None:
        return True                          # already gone (matches raw not-found→True)
    _net = abs(float(_pos.qty))
    _side = "sell" if _pos.side == "long" else "buy"
    _res = _og.check_never_sell_floor(symbol, tier, _net, _side, alpaca_net_qty=_net)
    if _res.action == "REJECT":
        logger.error(
            f"[{symbol}] close_position({tier}) BLOCKED by never-sell floor: {_res.reason}"
        )
        return False
    _q = int(_res.qty)
    if _q >= int(_net):
        return _raw_close_position(symbol)   # full close approved
    logger.warning(
        f"[{symbol}] close_position({tier}): never-sell floor bounded close "
        f"{int(_net)}→{_q} share(s) ({_res.reason}) — preserving qhm/forever6 floor."
    )
    if _q < 1:
        return False
    return partial_close_position(symbol, _q, tier=tier, _bypass_floor=True)


def close_position_for_tier(symbol: str, tier: str = "intraday") -> bool:
    """Backward-compatible alias — the never-sell-floor routing now lives inside
    close_position() itself (the chokepoint, increment 4a). Retained so any existing
    caller keeps working; new code should call close_position(symbol, tier=...)."""
    return close_position(symbol, tier=tier)


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


def get_asset_tradable(symbol: str) -> "bool | None":
    """
    Return the Alpaca-reported tradable status for a symbol, or None if the
    lookup fails or the field is absent — callers must treat None as "unknown",
    never as "not tradable". Used by Build F's halt-observability check (never
    gates order submission directly here — see strategy/run_cycle.py).
    """
    client = _get_trading_client()
    try:
        asset = client.get_asset(symbol)
        tradable = getattr(asset, "tradable", None)
        return bool(tradable) if tradable is not None else None
    except Exception as e:
        logger.warning(f"[{symbol}] get_asset_tradable failed: {e}")
        return None


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
