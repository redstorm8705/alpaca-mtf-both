"""Build a broker-exact, source-bound Core MTF entry cohort.

This module is research-only.  It accepts no symbol/time/FIFO ownership inference,
makes no execution or outcome claim, and never calls a broker API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


_SCHEMA_V = 1
_COID_PREFIX = "mtf-"
_INTENT_SIDE = {"buy_to_open": "buy", "sell_to_open": "sell"}
_ENTRY_ORDER_TYPES = frozenset({"market", "limit"})
_PARTIAL_STATUSES = frozenset(
    {"partially_filled", "canceled", "expired", "done_for_day", "replaced"}
)


class _ExcludedOrder(ValueError):
    """A valid broker order that is outside the canonical entry cohort."""


@dataclass(frozen=True)
class CanonicalEntry:
    broker_order_id: str
    client_order_id: str
    symbol: str
    direction: str
    position_intent: str
    side: str
    order_type: str
    order_status: str
    submitted_at_utc: str
    filled_at_utc: str
    requested_quantity: str
    filled_quantity: str
    unfilled_quantity: str
    canceled_quantity: str
    average_fill_price: str
    fill_completeness: str
    provenance: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_canonical_intake(
    order_snapshot_path: Path,
    *,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Return a fail-closed accounting artifact from immutable local sources."""
    snapshot_raw = order_snapshot_path.read_bytes()
    snapshot = json.loads(snapshot_raw)
    orders = _validated_snapshot_orders(snapshot)
    submitted_times = [
        _timestamp(order.get("submitted_at"), "invalid_submitted_at")
        for order in orders
    ]
    admitted: list[CanonicalEntry] = []
    exclusions: Counter[str] = Counter()
    mtf_candidates = 0
    for order in orders:
        coid = order.get("client_order_id")
        if not isinstance(coid, str) or not coid.startswith(_COID_PREFIX):
            continue
        mtf_candidates += 1
        try:
            admitted.append(_canonical_entry(order))
        except _ExcludedOrder as exc:
            exclusions[str(exc)] += 1

    entry_ids = [row.broker_order_id for row in admitted]
    if len(set(entry_ids)) != len(entry_ids):
        raise ValueError("duplicate admitted broker order id")

    admitted_by_id = {row.broker_order_id: row for row in admitted}
    ledger_accounting = _ledger_accounting(ledger_path, admitted_by_id)
    ledger_proven_ids = set(ledger_accounting.pop("proven_broker_order_ids"))
    entries = []
    for row in admitted:
        record = row.as_dict()
        if row.broker_order_id in ledger_proven_ids:
            record["provenance"] = ["broker_exact", "ledger_schema_proven"]
        else:
            record["provenance"] = ["broker_exact"]
        entries.append(record)

    return {
        "schema_v": _SCHEMA_V,
        "kind": "core_mtf_canonical_entry_intake",
        "selection_permitted": False,
        "execution_claims_permitted": False,
        "outcome_claims_permitted": False,
        "identity_rule": "exact_broker_order_id_only",
        "inclusion_predicates": [
            "client_order_id starts with mtf-",
            "position_intent is buy_to_open or sell_to_open",
            "side agrees with position_intent",
            "order type is market or limit",
            "submitted_at and filled_at are timezone-aware",
            "filled quantity and average fill price are positive finite numbers",
        ],
        "order_snapshot_source": {
            "path": str(order_snapshot_path),
            "artifact_sha256": hashlib.sha256(snapshot_raw).hexdigest(),
            "order_data_sha256": snapshot["order_data_sha256"],
            "row_count": len(orders),
            "earliest_submitted_at_utc": (
                min(submitted_times).isoformat() if submitted_times else None
            ),
            "latest_submitted_at_utc": (
                max(submitted_times).isoformat() if submitted_times else None
            ),
        },
        "ledger_source": ledger_accounting,
        "accounting": {
            "orders_considered": len(orders),
            "mtf_namespace_candidates": mtf_candidates,
            "broker_exact_entries_admitted": len(entries),
            "ledger_schema_proven_entries": len(ledger_proven_ids),
            "exclusions_by_reason": dict(sorted(exclusions.items())),
        },
        "entries": entries,
    }


def write_artifact(output_path: Path, artifact: dict[str, Any]) -> None:
    """Atomically persist a canonical intake artifact."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    temporary.replace(output_path)


def _validated_snapshot_orders(snapshot: Any) -> list[dict[str, Any]]:
    if not isinstance(snapshot, dict):
        raise TypeError("order snapshot must be an object")
    if snapshot.get("kind") != "alpaca_paper_order_history_snapshot":
        raise ValueError("order snapshot has unexpected kind")
    if snapshot.get("schema_v") != 1:
        raise ValueError("order snapshot has unsupported schema")
    if snapshot.get("access_mode") != "read_only_https_get":
        raise ValueError("order snapshot is not read-only")
    if snapshot.get("ledger_event_identity_claims_permitted") is not False:
        raise ValueError("order snapshot permits ledger identity claims")
    orders = snapshot.get("orders")
    digest = snapshot.get("order_data_sha256")
    if not isinstance(orders, list) or not isinstance(digest, str):
        raise ValueError("order snapshot lacks orders or order-data hash")
    if snapshot.get("order_count") != len(orders):
        raise ValueError("order snapshot row count mismatch")
    canonical = json.dumps(orders, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != digest:
        raise ValueError("order snapshot order-data hash mismatch")
    seen: set[str] = set()
    for order in orders:
        if not isinstance(order, dict):
            raise TypeError("order snapshot contains a non-object order")
        order_id = order.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("order has no immutable broker id")
        if order_id in seen:
            raise ValueError("duplicate broker order id in snapshot")
        seen.add(order_id)
    return orders


def _canonical_entry(order: dict[str, Any]) -> CanonicalEntry:
    order_id = _required_string(order, "id")
    coid = _required_string(order, "client_order_id")
    symbol = _required_string(order, "symbol")
    intent = _required_string(order, "position_intent").lower()
    side = _required_string(order, "side").lower()
    if intent not in _INTENT_SIDE:
        if intent in {"buy_to_close", "sell_to_close"}:
            raise _ExcludedOrder("non_entry_position_intent")
        raise ValueError("invalid_position_intent")
    if side != _INTENT_SIDE[intent]:
        raise ValueError("intent_side_contradiction")
    order_type = _required_string(order, "type").lower()
    if order_type not in _ENTRY_ORDER_TYPES:
        raise _ExcludedOrder("unsupported_entry_order_type")
    submitted = _timestamp(order.get("submitted_at"), "invalid_submitted_at")
    status = _required_string(order, "status").lower()
    if status != "filled" and status not in _PARTIAL_STATUSES:
        raise ValueError("unsupported_filled_order_status")
    requested = _positive_decimal(order.get("qty"), "invalid_requested_quantity")
    filled = _nonnegative_decimal(order.get("filled_qty"), "invalid_filled_quantity")
    if filled == 0:
        if status in {"canceled", "expired", "done_for_day", "replaced"}:
            raise _ExcludedOrder("zero_fill_terminal_entry")
        raise ValueError("zero_fill_status_contradiction")
    filled_at = _timestamp(order.get("filled_at"), "invalid_filled_at")
    if filled_at < submitted:
        raise ValueError("filled_at_precedes_submitted_at")
    fill_price = _positive_decimal(
        order.get("filled_avg_price"), "invalid_average_fill_price"
    )
    if filled > requested:
        raise ValueError("filled_quantity_exceeds_requested")
    if status == "filled" and filled != requested:
        raise ValueError("filled_status_quantity_contradiction")
    if status in _PARTIAL_STATUSES and filled >= requested:
        raise ValueError("partial_status_quantity_contradiction")
    if status != "filled" and status not in _PARTIAL_STATUSES:
        raise ValueError("unsupported_filled_order_status")
    unfilled = requested - filled
    completeness = "full" if filled == requested else "partial"
    return CanonicalEntry(
        broker_order_id=order_id,
        client_order_id=coid,
        symbol=symbol,
        direction="long" if intent == "buy_to_open" else "short",
        position_intent=intent,
        side=side,
        order_type=order_type,
        order_status=status,
        submitted_at_utc=submitted.isoformat(),
        filled_at_utc=filled_at.isoformat(),
        requested_quantity=_decimal_text(requested),
        filled_quantity=_decimal_text(filled),
        unfilled_quantity=_decimal_text(unfilled),
        canceled_quantity=(
            _decimal_text(unfilled)
            if status in {"canceled", "expired", "done_for_day", "replaced"}
            else "0"
        ),
        average_fill_price=_decimal_text(fill_price),
        fill_completeness=completeness,
        provenance=("broker_exact",),
    )


def _ledger_accounting(
    ledger_path: Path | None, admitted_by_id: dict[str, CanonicalEntry]
) -> dict[str, Any]:
    if ledger_path is None:
        return {
            "path": None,
            "sha256": None,
            "row_count": 0,
            "entry_rows_considered": 0,
            "schema_proven_rows": 0,
            "unproven_rows": 0,
            "proof_rule": "explicit Core MTF tier plus exact entry broker order id",
            "proven_broker_order_ids": [],
        }
    raw = ledger_path.read_bytes()
    rows = raw.decode("utf-8").splitlines()
    entry_rows = 0
    unproven = 0
    proven: set[str] = set()
    for line_number, line in enumerate(rows, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ledger row {line_number} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"ledger row {line_number} is not an object")
        if row.get("event") != "entry":
            continue
        entry_rows += 1
        strategy_tier = row.get("strategy_tier")
        tier_alias = row.get("tier")
        if (
            strategy_tier is not None
            and tier_alias is not None
            and strategy_tier != tier_alias
        ):
            raise ValueError(f"ledger row {line_number} has conflicting tier aliases")
        tier = strategy_tier if strategy_tier is not None else tier_alias
        entry_order_id = row.get("entry_order_id")
        broker_order_id = row.get("broker_order_id")
        if (
            entry_order_id is not None
            and broker_order_id is not None
            and entry_order_id != broker_order_id
        ):
            raise ValueError(
                f"ledger row {line_number} has conflicting broker order aliases"
            )
        broker_id = (
            entry_order_id if entry_order_id is not None else broker_order_id
        )
        if tier != "core_mtf" or not isinstance(broker_id, str):
            unproven += 1
            continue
        if broker_id not in admitted_by_id:
            raise ValueError(
                f"ledger row {line_number} names an unknown Core MTF entry order"
            )
        parent = admitted_by_id[broker_id]
        ledger_symbol = row.get("symbol")
        if ledger_symbol is not None and ledger_symbol != parent.symbol:
            raise ValueError(
                f"ledger row {line_number} contradicts broker entry symbol"
            )
        ledger_direction = row.get("direction")
        if ledger_direction is not None and ledger_direction != parent.direction:
            raise ValueError(
                f"ledger row {line_number} contradicts broker entry direction"
            )
        if broker_id in proven:
            raise ValueError("duplicate ledger proof for broker order id")
        proven.add(broker_id)
    return {
        "path": str(ledger_path),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "row_count": len(rows),
        "entry_rows_considered": entry_rows,
        "schema_proven_rows": len(proven),
        "unproven_rows": unproven,
        "proof_rule": "explicit Core MTF tier plus exact entry broker order id",
        "proven_broker_order_ids": sorted(proven),
    }


def _required_string(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing_{field}")
    return value.strip()


def _positive_decimal(value: Any, reason: str) -> Decimal:
    parsed = _nonnegative_decimal(value, reason)
    if parsed <= 0:
        raise ValueError(reason)
    return parsed


def _nonnegative_decimal(value: Any, reason: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(reason) from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(reason)
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _timestamp(value: Any, reason: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(reason) from exc
    if parsed.tzinfo is None:
        raise ValueError(reason)
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-snapshot", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    artifact = build_canonical_intake(
        args.order_snapshot, ledger_path=args.ledger
    )
    write_artifact(args.output, artifact)
    count = artifact["accounting"]["broker_exact_entries_admitted"]
    print(f"wrote {args.output}: {count} broker-exact entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
