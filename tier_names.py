# ruff: noqa: E501  — rationale docstring/comments run long (project convention)
"""Canonical identity for the bot's four tiers.

Existing durable state still carries historical keys. Read boundaries translate
those aliases while new domain APIs use day/swing/qhm/forever_6.
"""
from __future__ import annotations

from typing import Literal

TierId = Literal["day", "swing", "qhm", "forever_6"]
TIER_IDS: tuple[TierId, ...] = ("day", "swing", "qhm", "forever_6")
TIER_NAMES = ("Day", "Swing", "QHM", "F6")

TIER_DISPLAY = {
    "day": "Day",
    "daytrade": "Day",
    "intraday": "Swing",   # internal key of the Swing tier (IN- order tags); "intraday" is a historical name
    "swing": "Swing",
    "qhm": "QHM",
    "forever6": "F6",
    "forever_6": "F6",
}

_ALIASES: dict[str, TierId] = {
    "daytrade": "day",
    "intraday": "swing",
    "forever6": "forever_6",
}
_LEGACY_STORAGE: dict[TierId, str] = {
    "day": "daytrade",
    "swing": "intraday",
    "qhm": "qhm",
    "forever_6": "forever6",
}


def canonical_tier(key: object) -> TierId:
    """Strict canonical ID; historical persisted keys are accepted on read."""
    if not isinstance(key, str):
        raise ValueError(f"tier must be a string, got {type(key).__name__}")
    value = key.strip().lower()
    if value in TIER_IDS:
        return value  # type: ignore[return-value]
    try:
        return _ALIASES[value]
    except KeyError as exc:
        raise ValueError(f"unknown tier {key!r}") from exc


def legacy_storage_tier(tier: TierId) -> str:
    """Temporary durable-schema-v1 adapter."""
    return _LEGACY_STORAGE[canonical_tier(tier)]


def tier_label(key: object, default: "str | None" = None) -> str:
    """The display name for an internal tier key; `default` (or the key itself) when unknown. Never raises."""
    k = str(key or "").strip().lower()
    if k in TIER_DISPLAY:
        return TIER_DISPLAY[k]
    return default if default is not None else str(key or "")
