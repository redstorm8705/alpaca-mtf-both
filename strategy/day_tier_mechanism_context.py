"""Point-in-time mechanism evidence for the day-trade tier.

This module is an observability adapter.  It classifies the existing day-tier
decision and records only values already available to that decision.  It never
fetches data, places orders, changes a signal, or supplies defaults that could
be mistaken for observed market data.

Owner/signature: ChatGPT/Codex, 2026-10-05.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

MECHANISM_SCHEMA_V = 1
PT = ZoneInfo("America/Los_Angeles")

MECHANISMS = (
    "order_flow_imbalance",
    "relative_residual_return",
    "liquidity_implementation_shortfall",
    "vwap_state",
    "catalyst_abnormal_participation",
    "opening_inventory_gap_resolution",
    "volatility_state",
    "structural_breakout_quality",
    "dealer_hedging_pressure",
    "time_of_day_auction_flow",
)


def _plain(value: Any) -> Any:
    """Return a bounded JSON-native representation without inventing values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _plain(item())
        except Exception:  # noqa: BLE001 — foreign scalar adapters are best-effort only
            return str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def _first(
    sources: tuple[Mapping[str, Any], ...], *keys: str
) -> tuple[str, Any] | None:
    for source in sources:
        for key in keys:
            if key in source and source[key] is not None:
                return key, _plain(source[key])
    return None


def _observations(
    sources: tuple[Mapping[str, Any], ...], fields: Mapping[str, tuple[str, ...]]
) -> dict:
    out: dict[str, Any] = {}
    for canonical, aliases in fields.items():
        found = _first(sources, *aliases)
        if found is not None:
            out[canonical] = found[1]
    return out


def _mechanism(
    observations: dict, required: tuple[str, ...] = (), evidence_asof: Any = None
) -> dict:
    if not observations:
        status = "UNKNOWN"
    elif (
        required
        and all(key in observations for key in required)
        and evidence_asof is not None
    ):
        status = "OBSERVED"
    else:
        status = "PARTIAL"
    result = {"status": status, "observations": observations}
    if observations and evidence_asof is not None:
        result["evidence_asof"] = _plain(evidence_asof)
    return result


def _family(decision: Mapping[str, Any], trigger: Mapping[str, Any]) -> tuple[str, str]:
    track_value = _first((decision, trigger), "track", "strategy_track")
    track = str(track_value[1] if track_value else "A").upper()
    mode_value = _first((trigger, decision), "mode", "gex_mode", "signal_mode", "setup")
    mode = str(mode_value[1]).upper() if mode_value else ""
    if track == "B":
        return "orb_break_hold_v1", "daytier-track-b-2026-09-21"
    if mode == "RIDE":
        return "gex_wall_ride_v1", "daytier-track-a-ride-2026-10-04"
    if mode == "FADE":
        return "gex_wall_fade_v1", "daytier-track-a-fade-2026-10-04"
    return "unclassified_v1", "daytier-observation-2026-10-05"


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(
        _plain(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validated_asof(value: Any, captured_at: datetime) -> str | None:
    """Canonicalize an aware, non-future source timestamp; reject all other values."""
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        parsed_utc = parsed.astimezone(timezone.utc)
        captured_utc = captured_at.astimezone(timezone.utc)
        if parsed_utc > captured_utc:
            return None
        return parsed_utc.isoformat().replace("+00:00", "Z")
    except (OverflowError, TypeError, ValueError):
        return None


def build_decision_context(
    symbol: str,
    decision: Mapping[str, Any] | None = None,
    trigger: Mapping[str, Any] | None = None,
    size: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict:
    """Freeze the evidence visible at one decision; fail closed to UNKNOWN fields."""
    try:
        d = decision if isinstance(decision, Mapping) else {}
        t = trigger if isinstance(trigger, Mapping) else {}
        s = size if isinstance(size, Mapping) else {}
        sources = (t, d, s)
        stamp = now or datetime.now(PT)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=PT)
        stamp = stamp.astimezone(PT)
        family_id, hypothesis_version = _family(d, t)
        asof_value = _first(
            sources, "feature_asof", "data_asof", "bar_close_ts", "bar_ts", "asof_ts"
        )
        evidence_asof = _validated_asof(asof_value[1], stamp) if asof_value else None
        def mark(observations: dict, required: tuple[str, ...] = ()) -> dict:
            return _mechanism(observations, required, evidence_asof)

        mechanisms = {
            "order_flow_imbalance": mark(
                _observations(
                    sources,
                    {
                        "ofi": ("ofi", "order_flow_imbalance"),
                        "signed_volume": ("signed_volume", "aggressive_volume_delta"),
                        "price_response": (
                            "price_response",
                            "price_response_per_signed_dollar",
                        ),
                    },
                ),
                ("ofi",),
            ),
            "relative_residual_return": mark(
                _observations(
                    sources,
                    {
                        "residual_return": (
                            "residual_return",
                            "market_residual",
                            "idiosyncratic_return",
                        ),
                        "beta": ("beta", "expanding_beta"),
                        "benchmark_return": (
                            "benchmark_return",
                            "spy_return",
                            "qqq_return",
                        ),
                    },
                ),
                ("residual_return", "beta"),
            ),
            "liquidity_implementation_shortfall": mark(
                _observations(
                    sources,
                    {
                        "bid": ("bid", "bid_price"),
                        "ask": ("ask", "ask_price"),
                        "spread_bps": ("spread_bps",),
                        "quote_age_ms": ("quote_age_ms",),
                        "arrival_mid": ("arrival_mid", "midpoint"),
                    },
                ),
                ("bid", "ask"),
            ),
            "vwap_state": mark(
                _observations(
                    sources,
                    {
                        "vwap": ("vwap", "session_vwap"),
                        "vwap_slope": ("vwap_slope",),
                        "vwap_distance_atr": (
                            "vwap_distance_atr",
                            "distance_from_vwap_atr",
                        ),
                        "vwap_cross_count": ("vwap_cross_count",),
                    },
                ),
                ("vwap",),
            ),
            "catalyst_abnormal_participation": mark(
                _observations(
                    sources,
                    {
                        "catalyst_type": ("catalyst_type", "news_type"),
                        "catalyst_ts": ("catalyst_ts", "news_ts"),
                        "relative_volume": ("relative_volume", "rvol", "rel_volume"),
                        "gap_pct": ("gap_pct", "premarket_gap_pct"),
                        "volume_confirmed": ("vol_confirmed",),
                    },
                )
            ),
            "opening_inventory_gap_resolution": mark(
                _observations(
                    sources,
                    {
                        "prior_close": ("prior_close",),
                        "official_open": ("official_open", "open"),
                        "overnight_high": ("overnight_high", "premarket_high"),
                        "overnight_low": ("overnight_low", "premarket_low"),
                        "premarket_vwap": ("premarket_vwap",),
                        "gap_direction": ("gap_direction",),
                    },
                )
            ),
            "volatility_state": mark(
                _observations(
                    sources,
                    {
                        "atr": ("atr", "atr_5m", "atr14"),
                        "realized_volatility": ("realized_volatility", "realized_vol"),
                        "opening_range_width": ("opening_range_width", "orb_width"),
                        "volatility_regime": ("volatility_regime", "vol_regime"),
                    },
                )
            ),
            "structural_breakout_quality": mark(
                _observations(
                    sources,
                    {
                        "structural_level": (
                            "structural_level",
                            "wall_ref",
                            "wall",
                            "wall_price",
                            "orb_level",
                        ),
                        "break_distance_atr": ("break_distance_atr",),
                        "hold_bars": ("hold_bars", "confirmation_bars"),
                        "retest_depth": ("retest_depth",),
                        "mode": ("mode", "gex_mode", "setup"),
                    },
                ),
                ("structural_level",),
            ),
            "dealer_hedging_pressure": mark(
                _observations(
                    sources,
                    {
                        "gex_sign": ("gex_sign", "gex_label", "gamma_sign"),
                        "gex_action": ("gex_action",),
                        "gex_value": ("gex_value", "gex"),
                        "gex_fresh": ("gex_fresh", "gamma_fresh"),
                        "gex_valid": ("gex_valid", "sign_reliable", "gamma_valid"),
                        "action_admitted": ("act_ok",),
                        "target_pin": ("target_pin", "pin", "pin_price"),
                    },
                ),
                ("gex_sign", "gex_valid", "action_admitted"),
            ),
            "time_of_day_auction_flow": mark(
                {"source_time": evidence_asof} if evidence_asof is not None else {},
                ("source_time",),
            ),
        }
        payload = {
            "mechanism_schema_v": MECHANISM_SCHEMA_V,
            "build_status": "OK",
            "symbol": str(symbol).upper(),
            "captured_ts_pt": stamp.isoformat(),
            "capture_clock_note": "logger wall clock; not evidence source time",
            "family_id": family_id,
            "hypothesis_version": hypothesis_version,
            "mechanisms": mechanisms,
            "risk_delta": {
                "size": 0,
                "frequency": 0,
                "concurrency": 0,
                "effect": "logging_only",
            },
        }
        payload["context_digest"] = _digest(payload)
        return payload
    except Exception as exc:  # noqa: BLE001 — observability must never enter the trade path
        payload = {
            "mechanism_schema_v": MECHANISM_SCHEMA_V,
            "build_status": "ERROR",
            "symbol": str(symbol).upper(),
            "family_id": "unclassified_v1",
            "hypothesis_version": "daytier-observation-2026-10-05",
            "mechanisms": {
                name: {"status": "UNKNOWN", "observations": {}} for name in MECHANISMS
            },
            "risk_delta": {
                "size": 0,
                "frequency": 0,
                "concurrency": 0,
                "effect": "logging_only",
            },
            "error_type": type(exc).__name__,
        }
        payload["context_digest"] = _digest(payload)
        return payload


def compact_tags(context: Mapping[str, Any] | None) -> dict:
    """Return the immutable join fields copied onto fills and exits."""
    c = context if isinstance(context, Mapping) else {}
    try:
        schema_v = int(c.get("mechanism_schema_v", MECHANISM_SCHEMA_V))
    except (TypeError, ValueError):
        schema_v = MECHANISM_SCHEMA_V
    return {
        "mechanism_schema_v": schema_v,
        "family_id": str(c.get("family_id", "unclassified_v1")),
        "hypothesis_version": str(
            c.get("hypothesis_version", "daytier-observation-2026-10-05")
        ),
        "context_digest": str(c.get("context_digest", "")),
    }
