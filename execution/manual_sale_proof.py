# ruff: noqa: E501
"""manual_sale_proof.py — prove that a protected-hold sale was placed by the OWNER (Claude, 2026-10-17).

Design record: logs/design_records/qhm_owner_sale_autoheal_2026-10-17.md.

Alpaca stamps every order with `source` in its raw REST JSON. Every order sent with the API key — the bot, every
tier, every script — carries ``source == "access_key"`` (3,092 of 3,092 account orders, measured 2026-10-17). Orders
placed in the Alpaca app/web carry ``source == null`` and a random client_order_id (48 of 48). The bot holds only the
API key, so a bot process can never produce a sale that passes this proof. The alpaca-py SDK model drops `source`,
so the order list is read from the raw REST endpoint here (same pattern as reporting/pnl_ledger.py).

FAIL-CLOSED: any doubt (unreadable list, a missing `source` key, an API-sourced fill, a buy, a tagged order, a qty
mismatch, no proof window) returns NOT proven, and the caller keeps today's operator-confirm path.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_UUID_COID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REQUEST_TIMEOUT_SECONDS = 5.0      # one bounded read, no retry: this runs on the trading thread
_PAGE_SIZE = 500                    # Alpaca's maximum page size for /v2/orders


@dataclass
class SaleProof:
    proven: bool
    reason: str
    sold_qty: int = 0
    order_ids: list = field(default_factory=list)


def _whole(value) -> Optional[int]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not number.is_integer():
        return None
    return int(number)


def _is_bot_identity(client_order_id: str) -> bool:
    """True when the client_order_id carries any bot identity (a tier tag or the legacy mtf- prefix)."""
    coid = str(client_order_id or "")
    if coid.startswith("mtf-"):
        return True
    try:
        from execution.ownership_guard import tier_of_coid
        return tier_of_coid(coid) is not None
    except Exception as exc:  # RC-3: cannot classify -> treat as bot (fail-closed)
        logger.warning("manual_sale_proof: tier_of_coid failed for %r: %s", coid, exc)
        return True


def _parse_ts(value) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo is not None else None


def evaluate_owner_sale(orders: Optional[list], held_qty: int, broker_qty: int, since_iso: str) -> SaleProof:
    """Pure check over raw order dicts for ONE symbol.

    Only orders with a fill AFTER `since_iso` (the last moment the broker provably held `held_qty`) count. Proven
    iff every such order is a SELL with the exact app/web pattern — a `source` key whose value is JSON null and a
    random-UUID client_order_id — and their filled quantities sum to exactly held_qty - broker_qty
    (0 <= broker_qty < held_qty). Any other source value (the API key, a broker liquidation, a third-party app, a
    value never seen before) is NOT proven. Tripwire: any order on the symbol that carries a bot identity yet has a
    null source means the signal can no longer be trusted, so nothing is proven."""
    if orders is None:
        return SaleProof(False, "order history unreadable")
    if held_qty <= 0 or broker_qty < 0 or broker_qty >= held_qty:
        return SaleProof(False, f"no reduction to explain (record {held_qty}, broker {broker_qty})")
    since = _parse_ts(since_iso)
    if since is None:
        return SaleProof(False, "proof window start unreadable")
    sold = 0
    ids: list = []
    for o in orders:
        if not isinstance(o, dict):
            return SaleProof(False, "malformed order row")
        if o.get("_truncated"):
            return SaleProof(False, f"too many orders on this symbol to check ({_PAGE_SIZE}+)")
        oid = o.get("id")
        if "source" in o and o.get("source") is None and _is_bot_identity(o.get("client_order_id") or ""):
            logger.critical("manual_sale_proof TRIPWIRE: bot-tagged order %s has a null source — "
                            "owner-sale proof disabled for this check", oid)
            return SaleProof(False, f"bot order {oid} shows no API source (signal untrusted)")
        filled = _whole(o.get("filled_qty"))
        if filled is None:
            return SaleProof(False, f"unreadable filled quantity on order {oid}")
        if filled <= 0:
            continue
        filled_at = _parse_ts(o.get("filled_at"))
        if filled_at is None:
            return SaleProof(False, f"order {oid} has fills but no fill time")
        if filled_at <= since:
            continue
        side = str(o.get("side") or "").lower()
        if side != "sell":
            return SaleProof(False, f"a {side or 'non-sell'} order filled in the window ({oid})")
        if "source" not in o:
            return SaleProof(False, f"order {oid} has no source field")
        if o.get("source") is not None:
            return SaleProof(False, f"order {oid} came from {o.get('source')!r}, not the Alpaca app")
        if not _UUID_COID.match(str(o.get("client_order_id") or "")):
            return SaleProof(False, f"order {oid} does not have an app-generated order id")
        sold += filled
        ids.append(str(oid))
    # LIVE-SIGNAL CHECK (GAI 2026-10-17): the same response must show Alpaca still stamping the bot's own orders
    # (a bot-tagged order with source "access_key"), so a silent change in how Alpaca fills `source` can never be
    # read as an owner sale. The hold's own entry buy and stops are always in this window.
    if not any(isinstance(o, dict) and o.get("source") == "access_key"
               and _is_bot_identity(o.get("client_order_id") or "") for o in orders):
        return SaleProof(False, "no bot order in the window shows the API source tag (signal not confirmed)")
    expected = held_qty - broker_qty
    if sold != expected:
        return SaleProof(False, f"app sales total {sold} shares, record minus broker is {expected}")
    return SaleProof(True, "sold in the Alpaca app or website", sold, ids)


def fetch_closed_orders_since(symbol: str, after_iso: str) -> Optional[list]:
    """Raw orders (any status) for `symbol` submitted after `after_iso`, or None on any failure. One bounded request."""
    if not symbol or not after_iso:
        return None
    try:
        from reporting.pnl_ledger import _PAPER_BASE, _SSL_CTX, _headers
        query = urllib.parse.urlencode({
            "status": "all", "symbols": symbol, "after": after_iso,
            "limit": _PAGE_SIZE, "direction": "desc", "nested": "true",
        })
        req = urllib.request.Request(f"{_PAPER_BASE}/v2/orders?{query}", headers=_headers())
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
            rows = json.load(resp)
    except Exception as exc:  # RC-3: unreadable -> not proven
        logger.warning("manual_sale_proof: order read failed for %s: %s", symbol, exc)
        return None
    if not isinstance(rows, list):
        logger.warning("manual_sale_proof: non-list order page for %s", symbol)
        return None
    if len(rows) >= _PAGE_SIZE:
        logger.warning("manual_sale_proof: %s order window is a full page — not proving", symbol)
        return [{"_truncated": True}]
    flat: list = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        flat.append(row)
        flat.extend(leg for leg in (row.get("legs") or []) if isinstance(leg, dict))
    return flat


def prove_owner_sale(symbol: str, since_iso: Optional[str], held_qty: int, broker_qty: int,
                     orders_from_iso: Optional[str] = None) -> SaleProof:
    """Fetch + evaluate. `since_iso` = the last moment the broker provably held `held_qty`. Orders are read from
    `orders_from_iso` (the hold's creation time) so an order PLACED before the window but FILLED inside it counts;
    the window itself is applied on each order's fill time."""
    if not since_iso:
        return SaleProof(False, "no proof window (shares not verified since this check was deployed)")
    return evaluate_owner_sale(fetch_closed_orders_since(symbol, orders_from_iso or since_iso),
                               held_qty, broker_qty, since_iso)
