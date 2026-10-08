# ruff: noqa: E501  — rationale docstring/comments run long (project convention)
"""The four tier names every person-facing output uses (CEO 2026-10-07): Day, Swing, QHM, F6.

Display only. The internal keys stay exactly as stored — client_order_id prefixes DT- / IN- / QH- / F6-, the
ownership-ledger and trade_mode keys "daytrade" / "intraday" / "qhm" / "forever6" — because attribution, P&L by
tier and the ledger depend on them. Every report, dashboard and Slack message maps a key through tier_label().
tests/test_tier_names.py fails CI if a retired name ("Core MTF", "main bot", "Forever-6", "Day-Trade") reappears in
a person-facing string.
"""
from __future__ import annotations

TIER_NAMES = ("Day", "Swing", "QHM", "F6")

TIER_DISPLAY = {
    "daytrade": "Day",
    "intraday": "Swing",   # internal key of the Swing tier (IN- order tags); "intraday" is a historical name
    "swing": "Swing",
    "qhm": "QHM",
    "forever6": "F6",
}


def tier_label(key: object, default: "str | None" = None) -> str:
    """The display name for an internal tier key; `default` (or the key itself) when unknown. Never raises."""
    k = str(key or "").strip().lower()
    if k in TIER_DISPLAY:
        return TIER_DISPLAY[k]
    return default if default is not None else str(key or "")
