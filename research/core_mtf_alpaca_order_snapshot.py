"""Create immutable, read-only Alpaca order-history snapshots for Core MTF research.

The historical event ledger lacks broker order identifiers.  This module therefore
captures the broker's complete order history as a separately hashed input.  It
does *not* assert that a ledger event owns a nearby broker order: callers must
label any later timestamp-based join as correlation, not identity verification.

The module is intentionally outside the live trading path.  It performs HTTPS
GET requests only and has no signal, sizing, execution, or broker-order imports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ORDERS_ENDPOINT = "https://paper-api.alpaca.markets/v2/orders"
_SCHEMA_V = 1
_MAX_PAGES = 400
_PAGE_SIZE = 500
_CURSOR_FIELD = "submitted_at"


@dataclass(frozen=True)
class OrderHistoryResult:
    """Orders plus the evidence required to audit the retrieval contract."""

    orders: list[dict[str, Any]]
    page_count: int
    raw_rows_received: int
    overlap_duplicates_removed: int


def fetch_all_orders(
    *,
    api_key: str,
    secret_key: str,
    request_fn: Callable[[Request], bytes] | None = None,
) -> OrderHistoryResult:
    """Read all account orders with overlap-and-deduplicate pagination.

    Alpaca's ``until`` bound is strictly-before.  Advancing the next request one
    millisecond past the oldest submitted timestamp deliberately re-fetches the boundary
    group, which is then deduplicated by immutable broker order id.  A repeated
    full page with no new ids is surfaced as an error instead of silently
    producing an incomplete research input.
    """
    if not api_key or not secret_key:
        raise ValueError("Alpaca trading credentials are required")
    fetch = request_fn or _read_response
    until: str | None = None
    seen: set[str] = set()
    collected: list[dict[str, Any]] = []
    raw_rows_received = 0
    page_count = 0
    previous_until: datetime | None = None
    for _page in range(_MAX_PAGES):
        params = {
            "status": "all",
            "limit": str(_PAGE_SIZE),
            "direction": "desc",
            "nested": "false",
        }
        if until is not None:
            params["until"] = until
        request = Request(
            f"{_ORDERS_ENDPOINT}?{urlencode(params)}",
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
                "Accept": "application/json",
            },
            method="GET",
        )
        response = json.loads(fetch(request).decode("utf-8"))
        if not isinstance(response, list):
            raise TypeError("Alpaca order response is not a list")
        if not response:
            break
        _validate_page(response)
        page_count += 1
        raw_rows_received += len(response)
        new_rows = [order for order in response if order["id"] not in seen]
        if not new_rows:
            raise ValueError("Alpaca order pagination made no progress")
        for order in new_rows:
            seen.add(order["id"])
        collected.extend(new_rows)
        if len(response) < _PAGE_SIZE:
            break
        next_until = _inclusive_until(response[-1][_CURSOR_FIELD])
        next_until_dt = _parse_timestamp(next_until)
        if previous_until is not None and next_until_dt >= previous_until:
            raise ValueError("Alpaca order pagination cursor did not move backward")
        previous_until = next_until_dt
        until = next_until
    else:
        raise ValueError("Alpaca order history exceeded maximum page count")
    ordered = sorted(collected, key=lambda row: (row[_CURSOR_FIELD], row["id"]))
    return OrderHistoryResult(
        orders=ordered,
        page_count=page_count,
        raw_rows_received=raw_rows_received,
        overlap_duplicates_removed=raw_rows_received - len(ordered),
    )


def write_snapshot(
    output_path: Path,
    result: OrderHistoryResult,
    *,
    fetched_at: datetime,
) -> dict[str, Any]:
    """Atomically write a self-describing, content-hashed order-history input."""
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    _validate_orders(result.orders)
    if result.page_count < 0 or result.raw_rows_received < len(result.orders):
        raise ValueError("invalid order-history retrieval accounting")
    duplicate_count = result.raw_rows_received - len(result.orders)
    if result.overlap_duplicates_removed != duplicate_count:
        raise ValueError("invalid order-history duplicate accounting")
    if result.orders:
        earliest = _parse_timestamp(result.orders[0][_CURSOR_FIELD]).isoformat()
        latest = _parse_timestamp(result.orders[-1][_CURSOR_FIELD]).isoformat()
    else:
        earliest = None
        latest = None
    raw_orders = json.dumps(
        result.orders, sort_keys=True, separators=(",", ":")
    ).encode()
    artifact = {
        "schema_v": _SCHEMA_V,
        "kind": "alpaca_paper_order_history_snapshot",
        "access_mode": "read_only_https_get",
        "identity_scope": "broker_order_id_and_client_order_id_only",
        "ledger_event_identity_claims_permitted": False,
        "fetched_at_utc": fetched_at.astimezone(timezone.utc).isoformat(),
        "retrieval": {
            "endpoint": _ORDERS_ENDPOINT,
            "query": {
                "status": "all",
                "direction": "desc",
                "nested": False,
                "page_size": _PAGE_SIZE,
                "cursor_field": _CURSOR_FIELD,
            },
            "page_count": result.page_count,
            "raw_rows_received": result.raw_rows_received,
            "unique_orders_accepted": len(result.orders),
            "overlap_duplicates_removed": result.overlap_duplicates_removed,
            "earliest_submitted_at_utc": earliest,
            "latest_submitted_at_utc": latest,
        },
        "order_count": len(result.orders),
        "order_data_sha256": hashlib.sha256(raw_orders).hexdigest(),
        "orders": result.orders,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    temporary.replace(output_path)
    return artifact


def _validate_orders(orders: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for order in orders:
        if not isinstance(order, dict):
            raise TypeError("Alpaca order response contains a non-object order")
        order_id = order.get("id")
        created_at = order.get("created_at")
        submitted_at = order.get(_CURSOR_FIELD)
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("Alpaca order has no id")
        if order_id in seen:
            raise ValueError(f"duplicate Alpaca order id: {order_id}")
        seen.add(order_id)
        _parse_timestamp(created_at)
        _parse_timestamp(submitted_at)


def _validate_page(response: list[dict[str, Any]]) -> list[datetime]:
    """Validate one provider-ordered page before its final field becomes a cursor."""
    _validate_orders(response)
    timestamps = [_parse_timestamp(order[_CURSOR_FIELD]) for order in response]
    if any(later > earlier for earlier, later in pairwise(timestamps)):
        raise ValueError(f"Alpaca order page is not descending by {_CURSOR_FIELD}")
    return timestamps


def _inclusive_until(value: str) -> str:
    parsed = _parse_timestamp(value)
    # Use milliseconds because the API accepts RFC3339 Z and its order timestamps
    # can have more than three fractional digits.
    return (parsed + timedelta(milliseconds=1)).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Alpaca order has invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Alpaca order has invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("Alpaca order timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _read_response(request: Request) -> bytes:
    with urlopen(request, timeout=45) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    orders = fetch_all_orders(
        api_key=os.environ.get("ALPACA_API_KEY", ""),
        secret_key=os.environ.get("ALPACA_SECRET_KEY", ""),
    )
    artifact = write_snapshot(
        args.output, orders, fetched_at=datetime.now(timezone.utc)
    )
    print(
        f"wrote {args.output}: {artifact['order_count']} orders "
        f"sha256={artifact['order_data_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
