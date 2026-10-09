"""Strict codecs for ownership-ledger schema v1 and canonical schema v2.

OpenAI Codex (GPT-6), 2026-10-09.  This module is deliberately free of broker
and filesystem I/O so conversion can be proven before durable state changes.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any, Literal, cast

from tier_names import TIER_IDS, canonical_tier, legacy_storage_tier

Schema = Literal[1, 2]
V1_TIERS = tuple(legacy_storage_tier(tier) for tier in TIER_IDS)
V2_TIERS = TIER_IDS


class LedgerSchemaError(ValueError):
    """Raised when ledger bytes or objects are ambiguous or unsafe."""


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise LedgerSchemaError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def loads_strict(raw: bytes | str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_no_duplicate_pairs)
    except LedgerSchemaError:
        raise
    except Exception as exc:
        raise LedgerSchemaError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LedgerSchemaError("ledger root must be an object")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LedgerSchemaError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise LedgerSchemaError(f"{field} must be a finite number")
    return number


def schema_of(ledger: dict[str, Any]) -> Schema:
    version = ledger.get("version")
    if isinstance(version, bool) or type(version) is not int or version not in (1, 2):
        raise LedgerSchemaError("version must be the integer 1 or 2")
    return cast(Schema, version)


def validate(ledger: dict[str, Any]) -> Schema:
    if not isinstance(ledger, dict):
        raise LedgerSchemaError("ledger root must be an object")
    schema = schema_of(ledger)
    if "last_reconciled_utc" not in ledger:
        raise LedgerSchemaError("last_reconciled_utc is required")
    reconciled = ledger["last_reconciled_utc"]
    if reconciled is not None and not isinstance(reconciled, str):
        raise LedgerSchemaError("last_reconciled_utc must be null or a string")
    positions = ledger.get("positions")
    if not isinstance(positions, dict):
        raise LedgerSchemaError("positions must be an object")
    expected = set(V1_TIERS if schema == 1 else V2_TIERS)
    for symbol, entry in positions.items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip()
            or symbol != symbol.upper()
        ):
            raise LedgerSchemaError(
                "position symbol must be a stripped uppercase string"
            )
        if not isinstance(entry, dict):
            raise LedgerSchemaError(f"{symbol}: position must be an object")
        tiers = entry.get("tiers")
        if not isinstance(tiers, dict) or set(tiers) != expected:
            raise LedgerSchemaError(
                f"{symbol}: tier keys must be exactly {sorted(expected)!r}"
            )
        if "alpaca_net_qty" not in entry or "drift" not in entry:
            raise LedgerSchemaError(f"{symbol}: alpaca_net_qty and drift are required")
        _finite_number(entry["alpaca_net_qty"], f"{symbol}.alpaca_net_qty")
        _finite_number(entry["drift"], f"{symbol}.drift")
        for tier, claim in tiers.items():
            if not isinstance(claim, dict):
                raise LedgerSchemaError(f"{symbol}.{tier}: claim must be an object")
            if (
                "qty" not in claim
                or "avg_cost" not in claim
                or "last_fill_id" not in claim
            ):
                raise LedgerSchemaError(
                    f"{symbol}.{tier}: qty, avg_cost and last_fill_id are required"
                )
            _finite_number(claim["qty"], f"{symbol}.{tier}.qty")
            _finite_number(claim["avg_cost"], f"{symbol}.{tier}.avg_cost")
            if claim["last_fill_id"] is not None and not isinstance(
                claim["last_fill_id"], str
            ):
                raise LedgerSchemaError(
                    f"{symbol}.{tier}.last_fill_id must be null or a string"
                )
    return schema


def convert(ledger: dict[str, Any], target: Schema) -> dict[str, Any]:
    source = validate(ledger)
    if isinstance(target, bool) or target not in (1, 2):
        raise LedgerSchemaError("target schema must be 1 or 2")
    result = copy.deepcopy(ledger)
    result["version"] = target
    if source == target:
        return result
    for entry in result["positions"].values():
        old = entry["tiers"]
        new: dict[str, Any] = {}
        for raw_tier, claim in old.items():
            canonical = canonical_tier(raw_tier)
            key = canonical if target == 2 else legacy_storage_tier(canonical)
            if key in new:
                raise LedgerSchemaError(f"duplicate canonical owner {canonical!r}")
            new[key] = claim
        entry["tiers"] = {
            key: new[key] for key in (V2_TIERS if target == 2 else V1_TIERS)
        }
    validate(result)
    return result


def dumps(ledger: dict[str, Any], *, target: Schema, indent: int = 2) -> bytes:
    converted = convert(ledger, target)
    return (json.dumps(converted, indent=indent, allow_nan=False) + "\n").encode()


def ownership_fingerprint(ledger: dict[str, Any]) -> dict[str, Any]:
    """Schema-neutral fields that must survive conversion exactly."""
    canonical = convert(ledger, 2)
    return {
        symbol: {
            "alpaca_net_qty": entry["alpaca_net_qty"],
            "drift": entry["drift"],
            "tiers": {
                tier: {
                    "qty": entry["tiers"][tier]["qty"],
                    "avg_cost": entry["tiers"][tier]["avg_cost"],
                    "last_fill_id": entry["tiers"][tier]["last_fill_id"],
                }
                for tier in V2_TIERS
            },
        }
        for symbol, entry in canonical["positions"].items()
    }
