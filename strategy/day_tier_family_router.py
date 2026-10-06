"""Fail-closed family-admission router contract for the day tier.

The router consumes explicit evidence admissions.  It cannot infer admission
from scores, P&L, or mechanism tags, and it is not wired to order execution.

Owner/signature: ChatGPT/Codex, 2026-10-06.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REGISTRY_SCHEMA_V = 1
DEFAULT_REGISTRY = Path(__file__).with_name("day_tier_family_admissions.json")


@dataclass(frozen=True)
class RouteDecision:
    family_id: str
    hypothesis_version: str
    allocation: float
    admitted: bool
    reason: str


def load_registry(path: Path = DEFAULT_REGISTRY) -> tuple[dict, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_v") != REGISTRY_SCHEMA_V
        ):
            return {}, "registry schema invalid"
        families = payload.get("families")
        if not isinstance(families, dict):
            return {}, "registry families invalid"
        return families, "ok"
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {}, f"registry unreadable: {type(exc).__name__}"


def _positive_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return score if math.isfinite(score) and score > 0 else 0.0


def _parse_aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def route_families(
    candidates: list[dict],
    registry_path: Path = DEFAULT_REGISTRY,
    *,
    now: datetime | None = None,
) -> list[RouteDecision]:
    """Allocate within the day-tier sleeve; zero is always a valid result."""
    evaluated_at = now or datetime.now(timezone.utc)
    registry, registry_reason = load_registry(registry_path)
    evaluated: list[tuple[dict, dict, float]] = []
    rejected: list[RouteDecision] = []
    if not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is None:
        return [
            RouteDecision("", "", 0.0, False, "evaluation timestamp invalid")
            for _candidate in candidates
        ]
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            rejected.append(
                RouteDecision("", "", 0.0, False, "candidate payload invalid")
            )
            continue
        family_id = str(candidate.get("family_id") or "")
        version = str(candidate.get("hypothesis_version") or "")
        if family_id in seen:
            rejected.append(
                RouteDecision(
                    family_id, version, 0.0, False, "duplicate family candidate"
                )
            )
            continue
        seen.add(family_id)
        admission = registry.get(family_id) if isinstance(registry, dict) else None
        if not isinstance(admission, dict):
            rejected.append(
                RouteDecision(
                    family_id,
                    version,
                    0.0,
                    False,
                    registry_reason if not registry else "family not registered",
                )
            )
            continue
        if admission.get("status") != "ADMITTED_PAPER":
            rejected.append(
                RouteDecision(
                    family_id,
                    version,
                    0.0,
                    False,
                    f"status={admission.get('status', 'UNKNOWN')}",
                )
            )
            continue
        if version != str(admission.get("hypothesis_version") or ""):
            rejected.append(
                RouteDecision(
                    family_id, version, 0.0, False, "hypothesis version mismatch"
                )
            )
            continue
        if admission.get("independent_audit") != "PASS":
            rejected.append(
                RouteDecision(
                    family_id, version, 0.0, False, "independent audit not PASS"
                )
            )
            continue
        cap = _positive_score(admission.get("max_router_share"))
        if not 0 < cap <= 1:
            rejected.append(
                RouteDecision(family_id, version, 0.0, False, "allocation cap invalid")
            )
            continue
        max_age = _positive_score(admission.get("max_score_age_seconds"))
        score_asof = _parse_aware_timestamp(candidate.get("score_asof"))
        if max_age <= 0 or score_asof is None:
            rejected.append(
                RouteDecision(
                    family_id,
                    version,
                    0.0,
                    False,
                    "admitted but route score provenance invalid",
                )
            )
            continue
        age_seconds = (evaluated_at - score_asof).total_seconds()
        if age_seconds < 0 or age_seconds > max_age:
            rejected.append(
                RouteDecision(
                    family_id,
                    version,
                    0.0,
                    False,
                    "admitted but route score stale or future-dated",
                )
            )
            continue
        score = _positive_score(candidate.get("routing_score"))
        if score <= 0:
            rejected.append(
                RouteDecision(
                    family_id,
                    version,
                    0.0,
                    False,
                    "admitted but no positive route score",
                )
            )
            continue
        evaluated.append((candidate, admission, score))

    total_score = sum(item[2] for item in evaluated)
    routed: list[RouteDecision] = []
    for candidate, admission, score in evaluated:
        cap = _positive_score(admission.get("max_router_share"))
        raw = score / total_score if total_score > 0 else 0.0
        allocation = round(min(raw, cap), 6) if cap > 0 else 0.0
        if allocation <= 0:
            routed.append(
                RouteDecision(
                    str(candidate.get("family_id")),
                    str(candidate.get("hypothesis_version")),
                    0.0,
                    False,
                    "normalized router share below output precision",
                )
            )
            continue
        routed.append(
            RouteDecision(
                str(candidate.get("family_id")),
                str(candidate.get("hypothesis_version")),
                allocation,
                True,
                "explicit admission + exact version",
            )
        )
    return routed + rejected
