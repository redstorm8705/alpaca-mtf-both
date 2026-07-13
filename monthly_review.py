"""
monthly_review.py
Calendar-format monthly performance page.

Source: logs/eod_YYYY-MM-DD.json (realized P&L only)
Output: logs/monthly_YYYY-MM.html  (archive — always written)
        monthly_review.html         (root — current month only)

Usage:
    python3 monthly_review.py                   # current month
    python3 monthly_review.py --month 2026-04   # specific month (YYYY-MM)
    python3 monthly_review.py --all             # regenerate all months with eod data
"""

from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from reporting.metrics import _day_pnl, compute_lifetime_stats, compute_period_stats
# Strategy Edge Report (All-Time) lives on monthly now (Rafael 2026-07-05) —
# imported from weekly_review, not duplicated.
from weekly_review import _load_trade_log, _strategy_validation_html
from ui_tokens import (
    BG_BASE, BG_PANEL, BG_ELEVATED, BG_TODAY, BG_WEEKEND, BG_LT_BANNER,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, TEXT_DIM,
    BORDER_DEFAULT, BORDER_LIGHT, BORDER_TODAY,
    STATUS_POSITIVE, STATUS_NEGATIVE, STATUS_WARNING,
    FONT_FAMILY,
    FS_DISPLAY, FS_STAT, FS_H1, FS_PNL, FS_BODY, FS_NAV, FS_LABEL, FS_MICRO, FS_TINY,
    RADIUS_SM, RADIUS_MD, RADIUS_LG,
    LIVE_CLOCK_HTML,
)

import logging

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

logger = logging.getLogger(__name__)

# AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This script
# is read-only on logs/eod_*.json and only writes its own dedicated output
# files (monthly_*.html, monthly_review.html) via atomic write -- no
# write-contention risk with the live bot's shared state.

ROOT     = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(ROOT, "logs")
OUT_HTML = os.path.join(ROOT, "monthly_review.html")

_MECH_EXITS = frozenset({
    "overnight_atr_buffer_exit", "safe_close_all", "external_close",
    "pm_exit", "forced_exit", "breakeven_exit",
})


# ── Data helpers ──────────────────────────────────────────────────────────────

def _load_eod(d: date) -> dict | None:
    path = os.path.join(LOGS_DIR, f"eod_{d.isoformat()}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as _load_eod_err:
        logger.warning("_load_eod(%s): JSON parse failed — %s", d, _load_eod_err)
        return None


def _load_lifetime_pnl() -> dict | None:
    """Read lifetime P&L cache written by generate_dashboard.py.
    Returns the cache dict only when the required "total_pnl" key is present.
    Returns None if file missing, parse error, or key mismatch — triggers
    fallback to compute_lifetime_stats() in callers.
    """
    path = os.path.join(LOGS_DIR, "lifetime_pnl_cache.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "total_pnl" not in data:
            logger.warning(
                "_load_lifetime_pnl: cache missing 'total_pnl' key"
                " — falling back to compute_lifetime_stats(). keys=%s",
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )
            return None
        logger.info(
            "_load_lifetime_pnl: cache hit — total_pnl=%.2f ts=%s",
            data.get("total_pnl", 0.0),
            data.get("ts", "?"),
        )
        return data
    except Exception as _lt_pnl_err:
        logger.warning(
            "_load_lifetime_pnl: JSON parse failed parsing %s — %s", path, _lt_pnl_err
        )
        return None


def _month_days(year: int, month: int) -> list[date]:
    n = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, n + 1)]


def _list_months_with_data() -> list[tuple[int, int]]:
    """All (year, month) tuples that have at least one eod file."""
    months: set[tuple[int, int]] = set()
    for f in glob.glob(os.path.join(LOGS_DIR, "eod_????-??-??.json")):
        name = os.path.basename(f)
        try:
            d = date.fromisoformat(name[4:-5])
            months.add((d.year, d.month))
        except Exception as _month_parse_err:
            logger.debug(
                "_list_months_with_data: skipping malformed filename %s — %s",
                name, _month_parse_err,
            )
    return sorted(months)


def _prev_next(
    year: int, month: int
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    avail = _list_months_with_data()
    cur = (year, month)
    if cur not in avail:
        avail = sorted(avail + [cur])
    idx = avail.index(cur)
    prev_m = avail[idx - 1] if idx > 0 else None
    next_m = avail[idx + 1] if idx < len(avail) - 1 else None
    return prev_m, next_m


def _archive_href(y2: int, m2: int, from_archive: bool) -> str:
    """Link to another month's page, relative to the current page."""
    fn = f"monthly_{y2:04d}-{m2:02d}.html"
    return fn if from_archive else f"logs/{fn}"


# ── Metrics ───────────────────────────────────────────────────────────────────

def _compute_metrics(year: int, month: int) -> dict:
    days = _month_days(year, month)
    stats = compute_period_stats(days[0], days[-1])
    stats["wr"] = stats["win_rate"]   # backward compat with _stats_html
    return stats


# ── HTML builders ─────────────────────────────────────────────────────────────

def _pnl_class(pnl: float) -> str:
    return "pos" if pnl > 0 else "neg" if pnl < 0 else "zero"


def _fmt_pnl(pnl: float) -> str:
    sign = "+" if pnl > 0 else ""
    return f"{sign}${pnl:.2f}"


def _wr_color(wr: float) -> str:
    return (
        STATUS_POSITIVE if wr >= 60
        else STATUS_WARNING if wr >= 40
        else STATUS_NEGATIVE
    )


def _tqi_color(v: float) -> str:
    return (
        STATUS_POSITIVE if v >= 65
        else STATUS_WARNING if v >= 40
        else STATUS_NEGATIVE
    )


def _pf_color(v: float) -> str:
    return (
        STATUS_POSITIVE if v >= 1.5
        else STATUS_WARNING if v >= 1.0
        else STATUS_NEGATIVE
    )


def _day_cell(d: date | None) -> str:
    if d is None:
        return '<td class="day empty"></td>'

    is_today   = (d == date.today())
    is_weekend = d.weekday() >= 5
    eod        = _load_eod(d)

    classes = ["day"]
    if is_today:
        classes.append("today")
    if is_weekend:
        classes.append("weekend")
    if not eod:
        classes.append("no-data")
    cls = " ".join(classes)

    day_num = d.day

    if not eod:
        label = "" if is_weekend else "No closed trades"
        return (
            f'<td class="{cls}">'
            f'<div class="cell-date">{day_num}</div>'
            f'<div class="cell-empty">{label}</div>'
            f'</td>'
        )

    trades  = [t for t in (eod.get("trades") or []) if t.get("status") == "closed"]
    # P&L display must use the SAME field the monthly total sums
    # (compute_period_stats: alpaca_pnl if present else pnl_today) — else the
    # "Monthly P&L" header (Σ alpaca_pnl) diverges from the visible daily cells
    # (previously Σ pnl_today). Prefer the Alpaca-reconciled value (Rafael's
    # Alpaca-sourced-P&L mandate); fall back to pnl_today. (Fix 2026-07-12.)
    _ap     = eod.get("alpaca_pnl")
    pnl     = float(_ap if _ap is not None else (eod.get("pnl_today", 0) or 0))
    n       = len(trades)

    if n == 0:
        pnl_cls = _pnl_class(pnl)
        pnl_str = _fmt_pnl(pnl) if pnl != 0.0 else "$0.00"
        label   = "No tracker trades" if pnl != 0.0 else "No closed trades"
        return (
            f'<td class="{cls}">'
            f'<div class="cell-date">{day_num}</div>'
            f'<div class="cell-pnl {pnl_cls}">{pnl_str}</div>'
            f'<div class="cell-empty">{label}</div>'
            f'</td>'
        )

    wins      = sum(1 for t in trades if _day_pnl(t) > 0)
    day_wr    = wins / n * 100
    pnl_cls   = _pnl_class(pnl)
    wr_color  = _wr_color(day_wr)

    # pdt_day/pdt_badge removed S52 — SEC/FINRA rule amendment, board vote S50 28-0.
    loss_driver = (eod.get("loss_driver") or "").lower()
    loss_badge  = ""
    if loss_driver:
        if loss_driver in _MECH_EXITS:
            loss_badge = '<div class="loss-badge mech">⚙ MECHANICAL</div>'
        else:
            loss_badge = '<div class="loss-badge strat">✕ STRATEGY</div>'

    return (
        f'<td class="{cls}">'
        f'<div class="cell-header">'
        f'<span class="cell-date">{day_num}</span></div>'
        f'<div class="cell-pnl {pnl_cls}">{_fmt_pnl(pnl)}</div>'
        f'<div class="cell-meta" style="color:{wr_color}">'
        f'{n} trade{"s" if n != 1 else ""} · {day_wr:.0f}% WR</div>'
        f'{loss_badge}'
        f'</td>'
    )


def _stats_html(m: dict) -> str:
    if m["total_pnl"] > 0:
        pnl_c = STATUS_POSITIVE
    elif m["total_pnl"] < 0:
        pnl_c = STATUS_NEGATIVE
    else:
        pnl_c = TEXT_MUTED
    wr_c  = _wr_color(m["wr"])

    score_str = f"{m['avg_score']:.1f}/12" if m["avg_score"] is not None else "—"
    tqi_str   = f"{m['avg_tqi']:.1f}"      if m["avg_tqi"] is not None   else "—"
    tqi_c     = _tqi_color(m["avg_tqi"])   if m["avg_tqi"] is not None   else TEXT_MUTED
    pf_str    = f"{m['profit_factor']:.2f}×" if m["profit_factor"] is not None else "—"
    pf_c = (
        _pf_color(m["profit_factor"]) if m["profit_factor"] is not None
        else TEXT_MUTED
    )

    def box(label: str, val: str, color: str = TEXT_PRIMARY) -> str:
        return (
            f'<div class="stat-box">'
            f'<div class="stat-label">{label}</div>'
            f'<div class="stat-value" style="color:{color}">{val}</div>'
            f'</div>'
        )

    return (
        '<div class="stats-row">'
        + box("Monthly P&amp;L",  _fmt_pnl(m["total_pnl"]), pnl_c)
        + box("Trades",           str(m["total_trades"]))
        + box("Win Rate",         f"{m['wr']:.1f}%", wr_c)
        + box("Profit Factor",    pf_str, pf_c)
        + box("Avg Score",        score_str)
        + box("Avg TQI",          tqi_str, tqi_c)
        + box("Overnights",       str(m["overnight_count"]))
        + '</div>'
    )


def _cal_html(year: int, month: int) -> str:
    weeks = calendar.monthcalendar(year, month)
    rows  = ""
    for week in weeks:
        cells = "".join(
            _day_cell(date(year, month, dn) if dn else None)
            for dn in week[:5]      # Mon-Fri only (indices 0-4); drop Sat(5)/Sun(6)
        )
        rows += f"<tr>{cells}</tr>"
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    headers = "".join(f"<th>{h}</th>" for h in day_names)
    return (
        '<table class="cal-table">'
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _build_html(year: int, month: int, is_archive: bool) -> str:
    now_pt      = datetime.now(PT)
    metrics     = _compute_metrics(year, month)
    prev_m, next_m = _prev_next(year, month)
    month_label = date(year, month, 1).strftime("%B %Y")
    updated     = now_pt.strftime("%Y-%m-%d %I:%M %p PT")

    # Lifetime P&L banner — Alpaca-sourced via compute_lifetime_stats()
    # (equity - $2,500 initial, same code path as dashboard and weekly review)
    _raw_cache = _load_lifetime_pnl()
    _lt_data   = _raw_cache if _raw_cache is not None else compute_lifetime_stats()
    logger.info("monthly_review._build_html: lifetime_pnl_source=%s",
                "cache" if _raw_cache is not None else "compute_lifetime_stats")
    _lt_pnl    = float(_lt_data.get("total_pnl", 0.0))
    _lt_trades = int(_lt_data.get("total_trades", 0))
    _lt_wr     = float(_lt_data.get("win_rate", 0.0))
    _lt_pnl_c  = (
        STATUS_POSITIVE if _lt_pnl > 0
        else STATUS_NEGATIVE if _lt_pnl < 0
        else TEXT_MUTED
    )
    _lt_sign   = "+" if _lt_pnl >= 0 else ""
    _lt_banner = (
        f'<div class="lt-banner">'
        f'<div><div class="lt-label">Lifetime P&amp;L (All-Time)</div>'
        f'<div class="lt-val" style="color:{_lt_pnl_c}">{_lt_sign}${_lt_pnl:,.2f}</div>'
        f'<div class="lt-sub">From $2,500.00 initial &nbsp;·&nbsp;'
        f' {_lt_trades} closed trades &nbsp;·&nbsp; {_lt_wr:.0f}% win rate</div></div>'
        f'</div>'
    ) if _lt_data else ""

    # Navigation hrefs
    def nav_href(ym: tuple[int, int] | None) -> str:
        if ym is None:
            return "#"
        y2, m2 = ym
        return _archive_href(y2, m2, is_archive)

    prev_href     = nav_href(prev_m)
    next_href     = nav_href(next_m)
    prev_label    = date(*prev_m, 1).strftime("%b %Y") if prev_m else ""
    next_label    = date(*next_m, 1).strftime("%b %Y") if next_m else ""
    prev_dis      = ' class="nav-btn disabled"' if not prev_m else ' class="nav-btn"'
    next_dis      = ' class="nav-btn disabled"' if not next_m else ' class="nav-btn"'

    # Dropdown
    all_months = _list_months_with_data()
    if (year, month) not in all_months:
        all_months = sorted(all_months + [(year, month)])
    opts = ""
    for y2, m2 in reversed(all_months):
        href = _archive_href(y2, m2, is_archive)
        lbl  = date(y2, m2, 1).strftime("%B %Y")
        sel  = " selected" if (y2 == year and m2 == month) else ""
        opts += f'<option value="{href}"{sel}>{lbl}</option>'

    back = (
        '<a href="../monthly_review.html" class="back-link">← Current month</a>'
        if is_archive else ""
    )

    # CSS values sourced from ui_tokens.py (Step-1 token extraction, 2026-07-04).
    # Byte-identical to the prior hardcoded literals — golden-diff gated.
    # rgba() overlays remain literal here (page-specific low-alpha status tints);
    # they are tokenized at the later visual-convergence step, not now.
    css = (
        "* {box-sizing:border-box;margin:0;padding:0}"
        f"body{{background:{BG_BASE};color:{TEXT_SECONDARY};"
        f"font-family:{FONT_FAMILY};"
        f"font-size:{FS_BODY}px;min-height:100vh}}"
        f".header{{background:{BG_PANEL};border-bottom:1px solid {BORDER_DEFAULT};"
        "padding:16px 24px;display:flex;align-items:center;"
        "justify-content:space-between;flex-wrap:wrap;gap:12px}"
        f".header h1{{font-size:{FS_H1}px;font-weight:600;color:{TEXT_PRIMARY}}}"
        f".header-sub{{font-size:{FS_LABEL}px;color:{TEXT_MUTED};margin-top:2px}}"
        ".nav-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}"
        f".nav-btn{{background:{BG_ELEVATED};border:1px solid {BORDER_LIGHT};"
        f"color:{TEXT_SECONDARY};"
        f"padding:6px 14px;border-radius:{RADIUS_MD}px;cursor:pointer;"
        f"text-decoration:none;font-size:{FS_NAV}px}}"
        f".nav-btn:hover{{background:{BORDER_DEFAULT}}}"
        ".nav-btn.disabled{opacity:.3;pointer-events:none}"
        f".nav-select{{background:{BG_ELEVATED};border:1px solid {BORDER_LIGHT};"
        f"color:{TEXT_SECONDARY};padding:6px 10px;"
        f"border-radius:{RADIUS_MD}px;font-size:{FS_NAV}px}}"
        f".back-link{{color:{TEXT_MUTED};font-size:{FS_LABEL}px;text-decoration:none;"
        "display:block;margin-bottom:6px}"
        f".back-link:hover{{color:{TEXT_SECONDARY}}}"
        ".main{padding:20px 24px;max-width:1400px;margin:0 auto}"
        ".stats-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}"
        f".stat-box{{background:{BG_PANEL};border:1px solid {BORDER_DEFAULT};"
        f"border-radius:{RADIUS_LG}px;padding:12px 16px;min-width:100px;flex:1}}"
        f".stat-label{{font-size:{FS_MICRO}px;color:{TEXT_MUTED};text-transform:uppercase;"
        "letter-spacing:.5px;margin-bottom:4px}"
        f".stat-value{{font-size:{FS_STAT}px;font-weight:600}}"
        ".cal-table{width:100%;border-collapse:separate;border-spacing:5px}"
        f".cal-table th{{text-align:center;font-size:{FS_MICRO}px;color:{TEXT_MUTED};"
        "text-transform:uppercase;letter-spacing:.5px;padding-bottom:4px}"
        f"td.day{{background:{BG_PANEL};border:1px solid {BORDER_DEFAULT};"
        f"border-radius:{RADIUS_LG}px;vertical-align:top;padding:8px 10px;"
        "min-height:80px;width:20%}"
        f"td.day.today{{border-color:{BORDER_TODAY};background:{BG_TODAY}}}"
        f"td.day.weekend{{background:{BG_WEEKEND}}}"
        "td.day.no-data{opacity:.45}"
        "td.day.empty{background:transparent;border:none}"
        ".cell-header{display:flex;align-items:center;"
        "justify-content:space-between;margin-bottom:3px}"
        f".cell-date{{font-size:{FS_LABEL}px;color:{TEXT_MUTED};font-weight:500}}"
        f".cell-pnl{{font-size:{FS_PNL}px;font-weight:700;margin-bottom:2px}}"
        f".cell-meta{{font-size:{FS_MICRO}px}}"
        f".cell-empty{{font-size:{FS_MICRO}px;color:{TEXT_DIM};font-style:italic;margin-top:4px}}"
        f".pos{{color:{STATUS_POSITIVE}}}.neg{{color:{STATUS_NEGATIVE}}}.zero{{color:{TEXT_MUTED}}}"
        f".loss-badge{{font-size:{FS_TINY}px;font-weight:600;margin-top:4px;"
        f"padding:2px 5px;border-radius:{RADIUS_SM}px;display:inline-block}}"
        f".loss-badge.mech{{background:rgba(255,214,10,.13);color:{STATUS_WARNING}}}"
        f".loss-badge.strat{{background:rgba(255,59,48,.13);color:{STATUS_NEGATIVE}}}"
        f".footer{{text-align:center;font-size:{FS_MICRO}px;color:{TEXT_DIM};"
        "padding:16px;margin-top:8px}"
        f".lt-banner{{background:{BG_LT_BANNER};border:1px solid rgba(48,209,88,.35);"
        f"border-radius:{RADIUS_LG}px;padding:16px 24px;margin-bottom:16px;"
        "display:flex;align-items:center;gap:32px;flex-wrap:wrap}"
        f".lt-label{{font-size:{FS_MICRO}px;color:{TEXT_MUTED};text-transform:uppercase;"
        "letter-spacing:.5px;margin-bottom:4px}"
        f".lt-val{{font-size:{FS_DISPLAY}px;font-weight:700}}"
        f".lt-sub{{font-size:{FS_MICRO}px;color:{TEXT_MUTED};margin-top:4px}}"
        f".edge-details{{margin-top:20px;border:1px solid {BORDER_DEFAULT};"
        f"border-radius:{RADIUS_LG}px;background:{BG_PANEL};overflow:hidden}}"
        ".edge-details summary{list-style:none;cursor:pointer;padding:14px 18px;"
        "display:flex;align-items:center;gap:14px;flex-wrap:wrap}"
        ".edge-details summary::-webkit-details-marker{display:none}"
        f".edge-details summary:hover{{background:{BG_ELEVATED}}}"
        f".edge-title{{font-size:{FS_MICRO}px;font-weight:600;color:{TEXT_MUTED};"
        "text-transform:uppercase;letter-spacing:.1em}"
        f".edge-sum{{display:flex;align-items:center;gap:8px;font-size:{FS_NAV}px;"
        "font-weight:600;margin-left:auto}"
        f".edge-dot{{color:{TEXT_DIM}}}"
        f".edge-caret{{color:{TEXT_MUTED};font-size:{FS_LABEL}px;"
        "transition:transform .15s}"
        ".edge-details[open] .edge-caret{transform:rotate(180deg)}"
        ".edge-body{padding:0 18px 18px}"
    )

    # Strategy Edge Report (All-Time) — COLLAPSED dropdown (Rafael 2026-07-06,
    # board + Gro + GAI). Summary line shows the four headline metrics
    # (P&L / Win Rate / Profit Factor / Trades); full breakdown behind the expand.
    try:
        _tl = _load_trade_log()
        _closed = _tl.get("closed", [])
        _n_closed = len(_closed)
        _edge_body = _strategy_validation_html(_tl, lifetime_pnl=_lt_pnl)
    except Exception as _edge_err:
        logger.warning("monthly Strategy Edge Report failed: %s", _edge_err)
        _edge_body, _n_closed, _closed = "", 0, []
    if _edge_body:
        # All-time profit factor from closed-trade P&L (matches edge-report math).
        _epnls = [_day_pnl(t) for t in _closed]
        _gw = sum(p for p in _epnls if p > 0)
        _gl = abs(sum(p for p in _epnls if p < 0))
        _pf_all = (_gw / _gl) if _gl > 0 else 0.0
        _pf_all_str = f"{_pf_all:.2f}×" if _gl > 0 else "—"
        _pnl_sum_c = STATUS_POSITIVE if _lt_pnl >= 0 else STATUS_NEGATIVE
        _wr_sum_c = _wr_color(_lt_wr)
        _pf_sum_c = _pf_color(_pf_all) if _gl > 0 else TEXT_MUTED
        _summary = (
            '<span class="edge-sum">'
            f'<span style="color:{_pnl_sum_c}">{_lt_sign}${_lt_pnl:,.2f}</span>'
            '<span class="edge-dot">·</span>'
            f'<span style="color:{_wr_sum_c}">{_lt_wr:.0f}% WR</span>'
            '<span class="edge-dot">·</span>'
            f'<span style="color:{_pf_sum_c}">PF {_pf_all_str}</span>'
            '<span class="edge-dot">·</span>'
            f'<span style="color:{TEXT_SECONDARY}">{_n_closed} trades</span>'
            '</span>'
        )
        _caveat = (
            '<div style="font-size:11px;color:#ffd60a;'
            'background:rgba(255,214,10,.10);border:1px solid rgba(255,214,10,.3);'
            'border-radius:6px;padding:8px 14px;margin-bottom:10px">'
            f'&#9888; n&lt;100 — not yet statistically significant '
            f'(n={_n_closed} closed trades)</div>'
        ) if _n_closed < 100 else ""
        _edge_report = (
            '<details class="edge-details">'
            '<summary class="edge-summary">'
            '<span class="edge-title">Strategy Edge Report — All-Time</span>'
            f'{_summary}<span class="edge-caret">▾</span>'
            '</summary>'
            f'<div class="edge-body">{_caveat}{_edge_body}</div>'
            '</details>'
        )
    else:
        _edge_report = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="60">
  <title>Monthly Review — {month_label}</title>
  <style>{css}</style>
</head>
<body>
<div class="header">
  <div>
    <h1>Monthly Review — {month_label}</h1>
    <div class="header-sub">Updated {updated} &nbsp;·&nbsp; {LIVE_CLOCK_HTML}</div>
  </div>
  <div>
    {back}
    <div class="nav-row">
      <a href="{prev_href}"{prev_dis}>← {prev_label}</a>
      <select class="nav-select" onchange="window.location=this.value">{opts}</select>
      <a href="{next_href}"{next_dis}>{next_label} →</a>
    </div>
  </div>
</div>
<div class="main">
  {_lt_banner}
  {_stats_html(metrics)}
  {_cal_html(year, month)}
  {_edge_report}
</div>
<div class="footer">alpaca-mtf-bot · paper account · auto-refresh 60s</div>
</body>
</html>"""


# ── Write ─────────────────────────────────────────────────────────────────────

def _write_month(year: int, month: int) -> None:
    now_et     = datetime.now(ET)
    is_current = (year == now_et.year and month == now_et.month)

    # Always write the archive file (in logs/)
    archive = os.path.join(LOGS_DIR, f"monthly_{year:04d}-{month:02d}.html")
    _atomic_write(archive, _build_html(year, month, is_archive=True))
    print(f"Written: {archive}")

    # For the current month, also write the root file
    if is_current:
        _atomic_write(OUT_HTML, _build_html(year, month, is_archive=False))
        print(f"Written: {OUT_HTML}")


def _atomic_write(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate monthly review HTML.")
    parser.add_argument("--month", help="YYYY-MM (default: current month)")
    parser.add_argument(
        "--all-months", action="store_true",
        help="Regenerate all months with eod data",
    )
    args = parser.parse_args()

    now_et = datetime.now(ET)

    if args.all_months:
        months = _list_months_with_data()
        cur = (now_et.year, now_et.month)
        if cur not in months:
            months = sorted(months + [cur])
        for y, m in months:
            _write_month(y, m)
    elif args.month:
        try:
            y, m = map(int, args.month.split("-"))
            if not (1 <= m <= 12):
                raise ValueError
        except (ValueError, AttributeError):
            print(f"Invalid --month: {args.month!r}. Use YYYY-MM.")
            sys.exit(1)
        _write_month(y, m)
    else:
        _write_month(now_et.year, now_et.month)


if __name__ == "__main__":
    main()
