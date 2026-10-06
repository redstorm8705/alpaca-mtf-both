"""Deterministic implementation-shortfall attribution for day-tier event logs.

This module reads lifecycle dictionaries and produces measurement records.  It
does not fetch prices, alter P&L, size orders, or participate in execution.

Owner/signature: ChatGPT/Codex, 2026-10-06.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

COST_SCHEMA_V = 1


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    )


def _adverse_bps(
    fill: float | None, reference: float | None, side: str
) -> float | None:
    if fill is None or reference is None or fill <= 0 or reference <= 0:
        return None
    direction = 1.0 if side == "long" else -1.0 if side == "short" else None
    if direction is None:
        return None
    return round(direction * (fill - reference) / reference * 10_000.0, 4)


def _exit_adverse_bps(
    fill: float | None, market: float | None, side: str
) -> float | None:
    """Positive means the closing fill was worse than the market snapshot."""
    if fill is None or market is None or fill <= 0 or market <= 0:
        return None
    direction = 1.0 if side == "long" else -1.0 if side == "short" else None
    if direction is None:
        return None
    return round(direction * (market - fill) / market * 10_000.0, 4)


def _arrival_mid(decision: dict) -> float | None:
    context = decision.get("mechanism_context")
    if not isinstance(context, dict):
        return None
    mechanisms = context.get("mechanisms")
    if not isinstance(mechanisms, dict):
        return None
    liquidity = mechanisms.get("liquidity_implementation_shortfall")
    if not isinstance(liquidity, dict):
        return None
    if liquidity.get("status") != "OBSERVED":
        return None
    evidence_asof = _timestamp(liquidity.get("evidence_asof"))
    decision_ts = _timestamp(decision.get("ts"))
    if evidence_asof is None or decision_ts is None or evidence_asof > decision_ts:
        return None
    observations = liquidity.get("observations")
    if not isinstance(observations, dict):
        return None
    return _finite(observations.get("arrival_mid"))


def attribute_trade_costs(events: list[dict], trade_id: str) -> dict:
    """Join one trade lifecycle and measure costs without filling missing data."""
    rows = [row for row in events if str(row.get("trade_id") or "") == trade_id]
    decision = next((row for row in rows if row.get("event") == "decision"), {})
    entry = next((row for row in rows if row.get("event") == "entry_fill"), {})
    exits = [
        row for row in rows if row.get("event") in ("partial_exit_fill", "exit_fill")
    ]

    tags = (
        entry.get("mechanism_tags")
        if isinstance(entry.get("mechanism_tags"), dict)
        else {}
    )
    side_raw = str(entry.get("side") or "").lower()
    side = (
        "long"
        if side_raw in ("buy", "long")
        else "short"
        if side_raw in ("sell", "short")
        else "unknown"
    )
    entry_fill = _finite(entry.get("fill_price"))
    market_at_fill = _finite(entry.get("market_price_at_fill"))
    trigger = (
        decision.get("trigger") if isinstance(decision.get("trigger"), dict) else {}
    )
    decision_reference = _finite(trigger.get("entry_ref"))
    arrival_mid = _arrival_mid(decision)

    decision_ts = _timestamp(decision.get("ts"))
    entry_ts = _timestamp(entry.get("ts"))
    latency_ms = None
    if decision_ts is not None and entry_ts is not None:
        delta = (entry_ts - decision_ts).total_seconds() * 1000.0
        if delta >= 0:
            latency_ms = round(delta, 3)

    exit_rows = []
    for row in exits:
        fill = _finite(row.get("fill_price"))
        market = _finite(row.get("market_price_at_exit"))
        exit_rows.append(
            {
                "event": row.get("event"),
                "ts": row.get("ts"),
                "fill_qty": _finite(row.get("fill_qty")),
                "fill_price": fill,
                "market_price_at_exit": market,
                "exit_shortfall_bps": _exit_adverse_bps(fill, market, side),
                "exit_reason": row.get("exit_reason"),
            }
        )

    entry_components = {
        "decision_reference_bps": _adverse_bps(entry_fill, decision_reference, side),
        "arrival_mid_bps": _adverse_bps(entry_fill, arrival_mid, side),
        "market_at_fill_bps": _adverse_bps(entry_fill, market_at_fill, side),
    }
    measured = [value for value in entry_components.values() if value is not None]
    measured.extend(
        row["exit_shortfall_bps"]
        for row in exit_rows
        if row["exit_shortfall_bps"] is not None
    )
    return {
        "cost_schema_v": COST_SCHEMA_V,
        "trade_id": trade_id,
        "symbol": entry.get("symbol") or decision.get("symbol"),
        "side": side,
        "family_id": tags.get("family_id", "unclassified_v1"),
        "hypothesis_version": tags.get("hypothesis_version", ""),
        "context_digest": tags.get("context_digest", ""),
        "commission_usd": 0.0,
        "commission_note": (
            "Alpaca US equity commission; spread/slippage measured separately"
        ),
        "decision_to_fill_latency_ms": latency_ms,
        "entry": {
            "decision_reference": decision_reference,
            "arrival_mid": arrival_mid,
            "market_price_at_fill": market_at_fill,
            "fill_price": entry_fill,
            **entry_components,
        },
        "exits": exit_rows,
        "measurement_status": "MEASURED" if measured else "INSUFFICIENT_DATA",
    }


def attribute_all_closed_trades(events: list[dict]) -> list[dict]:
    """Return cost records for trade IDs that have at least one terminal exit."""
    closed = {
        str(row.get("trade_id"))
        for row in events
        if row.get("event") == "exit_fill" and row.get("trade_id")
    }
    return [attribute_trade_costs(events, trade_id) for trade_id in sorted(closed)]
