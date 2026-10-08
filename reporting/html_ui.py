"""Shared, display-only UI primitives for the bot's generated HTML pages.

The generators keep ownership of their page-specific content. This module provides the
consistent operator shell and tier vocabulary so a page cannot silently rename a
strategy or strand the user without navigation.
"""
from __future__ import annotations

from html import escape
from typing import Any

from tier_names import (
    TIER_IDS,
    canonical_tier,
    tier_label,
)

TIER_LABELS: dict[str, str] = {
    **{tier: tier_label(tier) for tier in TIER_IDS},
    "unattributed": "Unattributed",
}

TIER_COLORS: dict[str, str] = {
    "swing": "#00e5ff",
    "day": "#ff9f0a",
    "qhm": "#bf5af2",
    "forever_6": "#30d158",
    "unattributed": "#8a94ae",
}

PRIMARY_NAV_CSS = """
.global-nav{display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.global-nav a{color:#8a94ae;text-decoration:none;font-size:11px;font-weight:650;
  padding:6px 9px;border:1px solid transparent;border-radius:6px;white-space:nowrap}
.global-nav a:hover{color:#e8ecff;border-color:#5055a0;background:#1e2240}
.global-nav a.active{color:#00e5ff;border-color:#00e5ff55;background:#00e5ff0d}
@media(max-width:860px){.global-nav{width:100%;order:3;overflow-x:auto;flex-wrap:nowrap;
  padding-bottom:2px}.global-nav a{font-size:10px;padding:5px 7px}}
"""

_PAGES = (
    ("dashboard", "Overview", "dashboard.html"),
    ("scanner", "Scanner", "scan_results.html"),
    ("options", "Options", "options.html"),
    ("weekly", "Weekly", "weekly_review.html"),
    ("monthly", "Monthly", "monthly_review.html"),
)


def normalize_tier_mapping(by_tier: object) -> dict[str, Any]:
    """Canonicalize a tier-keyed mapping and reject unknown/alias collisions."""
    if not isinstance(by_tier, dict):
        raise ValueError("tier mapping must be a dict")
    normalized: dict[str, Any] = {}
    for raw_tier, value in by_tier.items():
        tier: str = (
            "unattributed"
            if raw_tier == "unattributed"
            else canonical_tier(raw_tier)
        )
        if tier in normalized:
            raise ValueError(f"duplicate tier aliases for {tier}")
        normalized[tier] = value
    return normalized


def primary_nav(active: str, prefix: str = "") -> str:
    links = []
    for key, label, href in _PAGES:
        cls = ' class="active" aria-current="page"' if key == active else ""
        links.append(f'<a href="{escape(prefix + href)}"{cls}>{escape(label)}</a>')
    return '<nav class="global-nav" aria-label="Primary">' + "".join(links) + "</nav>"


def tier_badges(tiers: list[tuple[str, float]]) -> str:
    """Render exact ownership-ledger tier quantities; never guess a missing tier."""
    if not tiers:
        tiers = [("unattributed", 0.0)]
    out = []
    for raw_tier, qty in tiers:
        try:
            tier_key: str = canonical_tier(raw_tier)
        except ValueError:
            tier_key = "unattributed"
        label = TIER_LABELS[tier_key]
        color = TIER_COLORS[tier_key]
        qty_text = f" {abs(qty):g}" if qty else ""
        out.append(
            f'<span class="tier-badge" style="color:{color};border-color:{color}55;'
            f'background:{color}12">{escape(label)}{qty_text}</span>'
        )
    return "".join(out)


def tier_performance_table(edge: dict) -> str:
    """All-time broker-ledger performance by entry tier."""
    try:
        by_tier = normalize_tier_mapping(edge.get("by_tier") or {})
    except ValueError:
        return (
            '<div class="edge-integrity-error"><b>Tier performance unavailable — '
            "ambiguous tier attribution.</b></div>"
        )
    rows = []
    for tier in (*TIER_IDS, "unattributed"):
        stats = by_tier.get(tier) or {}
        if not isinstance(stats, dict):
            return (
                '<div class="edge-integrity-error"><b>Tier performance unavailable — '
                "invalid tier metrics.</b></div>"
            )
        label = TIER_LABELS[tier]
        color = TIER_COLORS[tier]
        pnl = stats.get("realized_pnl")
        pnl_s = "—" if pnl is None else f"{'+' if pnl >= 0 else ''}${pnl:,.2f}"
        wr = stats.get("win_rate")
        wr_s = "—" if wr is None else f"{wr:.1f}%"
        pf = stats.get("profit_factor")
        pf_s = "—" if pf is None else f"{pf:.2f}×"
        rows.append(
            "<tr>"
            f'<td><span class="tier-dot" style="background:{color}"></span>{label}</td>'
            f'<td>{int(stats.get("completed_trades") or 0)}</td><td>{wr_s}</td>'
            f'<td>{pf_s}</td><td class="tier-pnl">{pnl_s}</td></tr>'
        )
    return (
        '<table class="tier-performance"><thead><tr><th>Entry tier</th>'
        '<th>Completed</th>'
        '<th>Win rate</th><th>Profit factor</th><th>Realized P&amp;L</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )
