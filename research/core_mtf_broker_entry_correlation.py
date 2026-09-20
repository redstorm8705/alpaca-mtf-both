"""Correlate Core MTF ledger parents to broker orders without claiming identity.

Historical ledger events have no broker order id.  This read-only research module
therefore permits only a bounded, same-symbol, same-side, legacy-tier timestamp
correlation.  A unique correlation identifies a nearby legacy sell order, but
its entry intent and parent ownership remain deliberately unknown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.core_mtf_replay_events import (
    CoreMTFShortEvent,
    extract_core_mtf_short_entries,
)


_SCHEMA_V = 1
_LEGACY_CORE_MTF_COID_PREFIX = "IN-"
_MAX_SUBMISSION_LEAD_SECONDS = 60.0


@dataclass(frozen=True)
class BrokerEntryCorrelation:
    """A bounded broker-order correspondence; never an intent or identity assertion."""

    entry_source_line: int
    symbol: str
    decision_time_utc: str
    status: str
    reason: str
    order_id: str | None
    client_order_id: str | None
    submitted_at_utc: str | None
    order_status: str | None
    requested_quantity: float | None
    filled_quantity: float | None
    filled_price: float | None
    fill_completeness: str
    order_type: str | None
    limit_price: float | None
    stop_price: float | None
    entry_intent_proven: bool
    evidence_basis: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def correlate_broker_entries(
    candidates: tuple[CoreMTFShortEvent, ...],
    orders: list[dict[str, Any]],
    *,
    max_submission_lead_seconds: float = _MAX_SUBMISSION_LEAD_SECONDS,
) -> tuple[BrokerEntryCorrelation, ...]:
    """Classify each parent using a narrow pre-decision order time window.

    The event log is written after submission on the observed production path.
    Only an order submitted no more than ``max_submission_lead_seconds`` before
    the log event can correlate. More than one qualifying order is ambiguous;
    a correlation never proves a parent, short-entry intent, or identity.
    """
    if (
        not math.isfinite(max_submission_lead_seconds)
        or max_submission_lead_seconds <= 0
    ):
        raise ValueError("max_submission_lead_seconds must be finite and positive")
    normalized = [_normalize_order(order) for order in orders]
    correlations: list[BrokerEntryCorrelation] = []
    for event in candidates:
        matches = [
            order
            for order in normalized
            if _matches(event, order, max_submission_lead_seconds)
        ]
        if not matches:
            correlations.append(_unmatched(event))
        elif len(matches) > 1:
            correlations.append(_ambiguous(event, len(matches)))
        else:
            correlations.append(_unique(event, matches[0]))
    return tuple(correlations)


def write_correlation_artifact(
    output_path: Path,
    ledger_path: Path,
    order_snapshot_path: Path,
) -> dict[str, Any]:
    """Write source-bound correlation results without live API access."""
    snapshot_raw = order_snapshot_path.read_bytes()
    snapshot = json.loads(snapshot_raw)
    orders = _validated_snapshot_orders(snapshot)
    extracted = extract_core_mtf_short_entries(ledger_path)
    correlations = correlate_broker_entries(extracted.events, orders)
    artifact = {
        "schema_v": _SCHEMA_V,
        "kind": "core_mtf_broker_entry_temporal_correlation",
        "identity_claims_permitted": False,
        "correlation_kind": "bounded_temporal_not_identity",
        "ledger_source": {
            "path": str(ledger_path),
            "sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        },
        "order_snapshot_source": {
            "path": str(order_snapshot_path),
            "artifact_sha256": hashlib.sha256(snapshot_raw).hexdigest(),
            "order_data_sha256": snapshot["order_data_sha256"],
        },
        "window": {
            "submission_must_precede_decision": True,
            "max_submission_lead_seconds": _MAX_SUBMISSION_LEAD_SECONDS,
        },
        "correlations": [item.as_dict() for item in correlations],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    temporary.replace(output_path)
    return artifact


def _validated_snapshot_orders(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        raise TypeError("order snapshot must be an object")
    if snapshot.get("kind") != "alpaca_paper_order_history_snapshot":
        raise ValueError("order snapshot has unexpected kind")
    if snapshot.get("access_mode") != "read_only_https_get":
        raise ValueError("order snapshot is not read-only")
    if snapshot.get("ledger_event_identity_claims_permitted") is not False:
        raise ValueError("order snapshot does not prohibit ledger identity claims")
    orders = snapshot.get("orders")
    digest = snapshot.get("order_data_sha256")
    if not isinstance(orders, list) or not isinstance(digest, str):
        raise ValueError("order snapshot lacks orders or order-data hash")
    raw = json.dumps(orders, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("order snapshot order-data hash mismatch")
    return orders


def _normalize_order(order: dict[str, Any]) -> dict[str, Any]:
    required = ("id", "symbol", "side", "client_order_id", "submitted_at")
    has_required_strings = isinstance(order, dict) and all(
        isinstance(order.get(key), str) for key in required
    )
    if not has_required_strings:
        raise ValueError("order has incomplete correlation fields")
    submitted = _time(order["submitted_at"])
    if submitted is None:
        raise ValueError("order has invalid submitted_at")
    return {**order, "_submitted": submitted}


def _matches(
    event: CoreMTFShortEvent, order: dict[str, Any], max_lead: float
) -> bool:
    if order["symbol"] != event.symbol or order["side"] != "sell":
        return False
    if not order["client_order_id"].startswith(_LEGACY_CORE_MTF_COID_PREFIX):
        return False
    lead_seconds = (event.decision_time - order["_submitted"]).total_seconds()
    return 0.0 <= lead_seconds <= max_lead


def _unmatched(event: CoreMTFShortEvent) -> BrokerEntryCorrelation:
    return BrokerEntryCorrelation(
        entry_source_line=event.source_line,
        symbol=event.symbol,
        decision_time_utc=event.decision_time.isoformat(),
        status="unmatched",
        reason="no_legacy_core_mtf_sell_order_in_predecision_window",
        order_id=None,
        client_order_id=None,
        submitted_at_utc=None,
        order_status=None,
        requested_quantity=None,
        filled_quantity=None,
        filled_price=None,
        fill_completeness="unknown",
        order_type=None,
        limit_price=None,
        stop_price=None,
        entry_intent_proven=False,
        evidence_basis=(),
    )


def _ambiguous(event: CoreMTFShortEvent, count: int) -> BrokerEntryCorrelation:
    return BrokerEntryCorrelation(
        entry_source_line=event.source_line,
        symbol=event.symbol,
        decision_time_utc=event.decision_time.isoformat(),
        status="ambiguous",
        reason=f"{count}_legacy_core_mtf_sell_orders_in_predecision_window",
        order_id=None,
        client_order_id=None,
        submitted_at_utc=None,
        order_status=None,
        requested_quantity=None,
        filled_quantity=None,
        filled_price=None,
        fill_completeness="unknown",
        order_type=None,
        limit_price=None,
        stop_price=None,
        entry_intent_proven=False,
        evidence_basis=(
            "symbol",
            "side",
            "legacy_core_mtf_client_order_id",
            "time_window",
        ),
    )


def _unique(event: CoreMTFShortEvent, order: dict[str, Any]) -> BrokerEntryCorrelation:
    filled_quantity = _positive(order.get("filled_qty"))
    requested_quantity = _positive(order.get("qty"))
    order_status = order.get("status")
    return BrokerEntryCorrelation(
        entry_source_line=event.source_line,
        symbol=event.symbol,
        decision_time_utc=event.decision_time.isoformat(),
        status="unique_temporal_legacy_sell_order",
        reason="unique_bounded_temporal_legacy_sell_order;_intent_unknown",
        order_id=order["id"],
        client_order_id=order["client_order_id"],
        submitted_at_utc=order["_submitted"].isoformat(),
        order_status=order_status if isinstance(order_status, str) else None,
        requested_quantity=requested_quantity,
        filled_quantity=filled_quantity,
        filled_price=_positive(order.get("filled_avg_price")),
        fill_completeness=_fill_completeness(requested_quantity, filled_quantity),
        order_type=order.get("type") if isinstance(order.get("type"), str) else None,
        limit_price=_positive(order.get("limit_price")),
        stop_price=_positive(order.get("stop_price")),
        entry_intent_proven=False,
        evidence_basis=(
            "symbol",
            "sell_side",
            "legacy_core_mtf_client_order_id",
            "predecision_time_window",
        ),
    )


def _positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _fill_completeness(requested: float | None, filled: float | None) -> str:
    if filled is None:
        return "none"
    if requested is None:
        return "unknown"
    return "full" if filled >= requested else "partial"


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--order-snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    artifact = write_correlation_artifact(args.output, args.ledger, args.order_snapshot)
    print(f"wrote {args.output}: {len(artifact['correlations'])} correlations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
