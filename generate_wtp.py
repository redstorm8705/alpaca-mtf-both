#!/usr/bin/env python3
# ruff: noqa: E501  — HTML/CSS template strings exceed 88 chars by design
"""
generate_wtp.py — Weekly Trade Post-mortem (WTP) HTML report.

Usage:
    python3 generate_wtp.py                    # most recent completed week
    python3 generate_wtp.py --week 2026-05-05  # week containing this Monday

Output:
    logs/weekly_wtp_YYYY-MM-DD.html  (Friday date of the target week)

Reads:  logs/trade_events.jsonl
Writes: logs/weekly_wtp_<friday>.html  (atomic tmp→replace)

Design:
    - Stat tiles: Trades | Win Rate | Total P&L | Avg P&L | Avg Score | Avg Missed Move
    - Trade table: Symbol | Dir | P&L | Stage | Missed Move | Exit Reason | Entry → Exit
    - Missed Move = (target − exit_price) × size for longs;
                    (exit_price − target) × size for shorts.
                    Positive = left on table. N/A if no target in entry event.
    - Stage = MRI level at entry (NORMAL / ELEVATED / STRESSED / HIGH / CRISIS)
    - Exit reason icons: ◆ Target  ▲ ATR Buffer  ● AH External  ■ Ext. Close  ✕ Stop
    - Only closed trades whose EXIT date falls within the target Mon–Fri are shown.

RTH Block: 9:30 AM–4:00 PM ET weekdays (per CLAUDE.md §4).
"""

import sys
import json
import argparse
import tempfile
from pathlib import Path
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

# ─── RTH Block ────────────────────────────────────────────────────────────────
_ET = ZoneInfo("America/New_York")
_now_et = datetime.now(_ET)
if _now_et.weekday() < 5:
    _mins = _now_et.hour * 60 + _now_et.minute
    if (9 * 60 + 30) <= _mins < (16 * 60):
        print("BLOCKED: Cannot run during RTH hours (9:30 AM–4:00 PM ET weekdays).")
        sys.exit(1)

# ─── Paths ────────────────────────────────────────────────────────────────────
_BASE   = Path(__file__).resolve().parent
_LOGS   = _BASE / "logs"
_EVENTS = _LOGS / "trade_events.jsonl"

_PT = ZoneInfo("America/Los_Angeles")

# ─── Exit reason → display icon mapping ───────────────────────────────────────
def _exit_icon(reason: str | None) -> tuple[str, str]:
    """Return (icon, label) for a given exit reason string."""
    if not reason:
        return ("■", "Unknown")
    r = reason.lower()
    if "target" in r:
        return ("◆", "Target")
    if "overnight_atr_buffer" in r or "overnight_breakeven" in r:
        return ("▲", "ATR Buffer")
    if "external_close_detected_ah" in r or "ah_external" in r:
        return ("●", "AH External")
    if "external_close" in r:
        return ("■", "Ext. Close")
    if "trail_stop" in r or "trail" in r:
        return ("▲", "Trail Stop")
    if "stop" in r or "stop_hit" in r:
        return ("✕", "Stop")
    if "pm_exit" in r or "pm " in r:
        return ("●", "PM Exit")
    if "manual" in r or "safe_close" in r:
        return ("■", "Manual")
    # Fallback: show first token of reason
    first = reason.split("|")[0].strip()[:20]
    return ("■", first)


# ─── MRI level → display colour class ─────────────────────────────────────────
_MRI_CLASS = {
    "NORMAL":   "mri-normal",
    "ELEVATED": "mri-elevated",
    "STRESSED": "mri-stressed",
    "HIGH":     "mri-high",
    "CRISIS":   "mri-crisis",
}

# ─── Week helpers ──────────────────────────────────────────────────────────────

def _week_bounds(ref: date) -> tuple[date, date]:
    """Return (monday, friday) of the trading week containing ref."""
    mon = ref - timedelta(days=ref.weekday())
    fri = mon + timedelta(days=4)
    return mon, fri


def _most_recent_completed_week() -> date:
    """Return the Monday of the most recent completed trading week."""
    today = datetime.now(_PT).date()
    # 'Completed' means Friday has passed (today is at least Saturday or is Monday of next week)
    # Walk back until we land on or after a Saturday (meaning last Fri has passed)
    days_since_mon = today.weekday()  # 0=Mon … 6=Sun
    if days_since_mon == 0:
        # Today is Monday → last completed week ended last Friday
        last_fri = today - timedelta(days=3)
    elif days_since_mon == 6:
        # Today is Sunday → last completed week ended yesterday (Sat? no — Fri)
        last_fri = today - timedelta(days=2)
    else:
        # Tue–Sat: most recent completed Friday is 0–4 days ago
        last_fri = today - timedelta(days=days_since_mon - 4)
        if last_fri >= today:
            last_fri -= timedelta(weeks=1)
    mon = last_fri - timedelta(days=4)
    return mon


# ─── Trade reconstruction ──────────────────────────────────────────────────────

def _load_events() -> list[dict]:
    """Read all events from trade_events.jsonl, sorted by timestamp."""
    if not _EVENTS.exists():
        return []
    events: list[dict] = []
    for raw in _EVENTS.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Skip non-trade event types (mri_refresh, signal, etc.)
        if ev.get("event") not in ("entry", "exit", "partial_exit", "stop_hit"):
            continue
        events.append(ev)
    events.sort(key=lambda e: e.get("ts", ""))
    return events


def _build_closed_trades(events: list[dict]) -> list[dict]:
    """
    FIFO trade reconstruction from the event stream.

    Returns a list of completed trade dicts:
        entry_ts, exit_ts, symbol, direction,
        entry_price, exit_price, size,
        target, stop, score, mri_level,
        total_pnl, partial_count, exit_reason, exit_event_type,
        trade_mode
    """
    open_trades: dict[str, dict] = {}   # symbol → active trade
    closed: list[dict] = []

    for ev in events:
        sym    = ev.get("symbol", "")
        etype  = ev.get("event", "")
        pnl    = float(ev.get("pnl", 0.0))

        if etype == "entry":
            # If there's already an open trade for this symbol (shouldn't happen,
            # but protect against log gaps), close it as "orphan" before opening new.
            if sym in open_trades:
                pass  # silently discard — log gaps in JSONL
            open_trades[sym] = {
                "entry_ts":     ev.get("ts", ""),
                "symbol":       sym,
                "direction":    ev.get("direction", "long"),
                "entry_price":  float(ev.get("price", 0.0)),
                "size":         int(ev.get("size", 0)),
                "target":       ev.get("target"),   # may be None in older logs
                "stop":         ev.get("stop"),
                "score":        int(ev.get("score", 0)),
                "mri_level":    ev.get("mri_level", "NORMAL"),
                "trade_mode":   ev.get("trade_mode", ""),
                "partial_pnl":  0.0,
                "partial_count": 0,
            }

        elif etype == "partial_exit":
            if sym in open_trades:
                open_trades[sym]["partial_pnl"]    += pnl
                open_trades[sym]["partial_count"]  += 1

        elif etype in ("exit", "stop_hit"):
            if sym not in open_trades:
                # Exit without a recorded entry — skip (pre-existing position or log gap)
                continue
            t = open_trades.pop(sym)
            total_pnl   = t["partial_pnl"] + pnl
            exit_price  = float(ev.get("price", 0.0))
            exit_ts     = ev.get("ts", "")
            exit_reason = ev.get("reason", "")

            # Missed move: distance from exit to target (positive = left on table)
            target = t.get("target")
            if target is not None:
                if t["direction"] == "long":
                    missed_move_per_sh = float(target) - exit_price
                else:
                    missed_move_per_sh = exit_price - float(target)
                missed_move = missed_move_per_sh * t["size"]
            else:
                missed_move = None

            closed.append({
                **t,
                "exit_ts":          exit_ts,
                "exit_price":       exit_price,
                "exit_reason":      exit_reason,
                "exit_event_type":  etype,
                "total_pnl":        round(total_pnl, 4),
                "missed_move":      round(missed_move, 2) if missed_move is not None else None,
            })

    return closed


def _filter_by_week(trades: list[dict], mon: date, fri: date) -> list[dict]:
    """Return trades whose EXIT date falls within Mon–Fri."""
    result = []
    for t in trades:
        exit_ts = t.get("exit_ts", "")
        if not exit_ts:
            continue
        try:
            dt = datetime.fromisoformat(exit_ts).astimezone(_PT)
            exit_date = dt.date()
        except (ValueError, TypeError):
            continue
        if mon <= exit_date <= fri:
            result.append(t)
    return result


# ─── HTML helpers ──────────────────────────────────────────────────────────────

def _fmt_ts(ts: str) -> str:
    """ISO-8601 → 'Mon May 05  9:31 AM PT'"""
    try:
        dt = datetime.fromisoformat(ts).astimezone(_PT)
        return dt.strftime("%-m/%-d %I:%M %p PT").replace(" 0", " ")
    except (ValueError, TypeError):
        return ts[:16] if ts else "—"


def _pnl_html(pnl: float) -> str:
    cls = "pos" if pnl > 0 else ("neg" if pnl < 0 else "zero")
    sign = "+" if pnl > 0 else ""
    return f'<span class="{cls}">{sign}${pnl:.2f}</span>'


def _missed_html(mm: float | None) -> str:
    if mm is None:
        return '<span class="zero">—</span>'
    cls   = "neg" if mm > 0 else ("pos" if mm < 0 else "zero")
    sign  = "+" if mm < 0 else ""   # negative mm = exceeded target (good)
    label = f"{sign}${abs(mm):.2f}"
    title = "Left on table" if mm > 0 else ("Exceeded target" if mm < 0 else "Exact target")
    return f'<span class="{cls}" title="{title}">{label}</span>'


def _mri_badge(level: str) -> str:
    cls   = _MRI_CLASS.get(level.upper(), "mri-normal")
    return f'<span class="mri-badge {cls}">{level}</span>'


def _dir_badge(direction: str) -> str:
    cls = "dir-long" if direction == "long" else "dir-short"
    label = "▲ Long" if direction == "long" else "▼ Short"
    return f'<span class="dir-badge {cls}">{label}</span>'


# ─── HTML generation ───────────────────────────────────────────────────────────

def generate_html(trades: list[dict], mon: date, fri: date) -> str:
    # ── Summary stats ──────────────────────────────────────────────────────────
    n_trades    = len(trades)
    winners     = [t for t in trades if t["total_pnl"] > 0]
    losers      = [t for t in trades if t["total_pnl"] <= 0]
    win_rate    = (len(winners) / n_trades * 100) if n_trades else 0.0
    total_pnl   = sum(t["total_pnl"] for t in trades)
    avg_pnl     = (total_pnl / n_trades) if n_trades else 0.0
    avg_score   = (sum(t["score"] for t in trades) / n_trades) if n_trades else 0.0
    mm_vals     = [t["missed_move"] for t in trades if t["missed_move"] is not None]
    avg_mm      = (sum(mm_vals) / len(mm_vals)) if mm_vals else None

    pnl_cls       = "pos" if total_pnl > 0 else ("neg" if total_pnl < 0 else "zero")
    avg_pnl_cls   = "pos" if avg_pnl > 0 else ("neg" if avg_pnl < 0 else "zero")
    win_rate_cls  = "pos" if win_rate >= 50 else "neg"
    mm_cls        = "neg" if (avg_mm or 0) > 0 else "pos"

    pnl_sign     = "+" if total_pnl > 0 else ""
    avg_pnl_sign = "+" if avg_pnl > 0 else ""
    mm_sign      = "" if avg_mm is None or avg_mm >= 0 else "+"

    week_label = f"Week of {mon.strftime('%b %d')} – {fri.strftime('%b %d, %Y')}"
    gen_time   = datetime.now(_PT).strftime("%-m/%-d/%Y %-I:%M %p PT")

    # ── Trade rows ─────────────────────────────────────────────────────────────
    rows_html = ""
    if not trades:
        rows_html = '<tr><td colspan="7" style="text-align:center;color:#b8bdd4;padding:28px">No closed trades this week</td></tr>'
    else:
        for t in sorted(trades, key=lambda x: x["exit_ts"]):
            icon, icon_label = _exit_icon(t["exit_reason"])
            icon_cls = "icon-target" if icon == "◆" else (
                       "icon-atr"    if icon == "▲" else (
                       "icon-ah"     if icon == "●" else (
                       "icon-stop"   if icon == "✕" else "icon-ext")))
            reason_short = t.get("exit_reason", "") or ""
            reason_trunc = (reason_short[:60] + "…") if len(reason_short) > 60 else reason_short
            partial_note = f' <span class="partial-badge">+{t["partial_count"]}P</span>' \
                           if t["partial_count"] > 0 else ""

            rows_html += f"""
      <tr>
        <td><strong>{t['symbol']}</strong>{partial_note}</td>
        <td>{_dir_badge(t['direction'])}</td>
        <td>{_pnl_html(t['total_pnl'])}</td>
        <td>{_mri_badge(t['mri_level'])}</td>
        <td>{_missed_html(t['missed_move'])}</td>
        <td>
          <span class="exit-icon {icon_cls}" title="{reason_trunc}">{icon}</span>
          <span class="exit-label">{icon_label}</span>
        </td>
        <td class="ts-col">
          <div class="ts-entry">{_fmt_ts(t['entry_ts'])}</div>
          <div class="ts-exit text-muted">→ {_fmt_ts(t['exit_ts'])}</div>
        </td>
      </tr>"""

    # ── Full HTML ──────────────────────────────────────────────────────────────
    avg_mm_display = (
        f'{mm_sign}${abs(avg_mm):.2f}' if avg_mm is not None else "—"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WTP · {week_label}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#111318;color:#e2e4ee;font-family:-apple-system,"SF Pro Text",sans-serif;font-size:13px;line-height:1.4}}
.header{{background:#161920;border-bottom:1px solid #161a28;padding:13px 24px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}}
.title{{font-size:15px;font-weight:700}}
.sub{{font-size:11px;color:#b8bdd4;margin-top:2px}}
.gen-time{{font-size:10px;color:#b8bdd4}}
.container{{max-width:1100px;margin:0 auto;padding:24px}}
.stats-row{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:20px}}
.stat{{background:#161920;border:1px solid #161a28;border-radius:6px;padding:14px;text-align:center}}
.stat-lbl{{font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#b8bdd4;margin-bottom:6px}}
.stat-val{{font-size:20px;font-weight:700}}
.card{{background:#161920;border:1px solid #161a28;border-radius:8px;overflow:hidden}}
.card-hdr{{background:#1a1e28;border-bottom:1px solid #161a28;padding:11px 20px;
  font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#b8bdd4;
  display:flex;align-items:center;justify-content:space-between}}
.card-hdr .n-badge{{background:rgba(10,132,255,.15);color:#0a84ff;font-size:9px;
  padding:2px 8px;border-radius:3px;font-weight:700}}
table{{width:100%;border-collapse:collapse}}
th{{background:#1a1e28;font-size:9px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:#b8bdd4;padding:10px 14px;text-align:left;border-bottom:1px solid #161a28}}
td{{padding:11px 14px;border-bottom:1px solid #111520;vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:rgba(255,255,255,.02)}}
.pos{{color:#30d158}}.neg{{color:#ff3b30}}.zero{{color:#b8bdd4}}
.text-muted{{color:#b8bdd4}}
/* Direction badges */
.dir-badge{{display:inline-block;font-size:9px;font-weight:700;padding:2px 7px;border-radius:3px;letter-spacing:.04em}}
.dir-long{{background:rgba(48,209,88,.12);color:#30d158}}
.dir-short{{background:rgba(255,59,48,.12);color:#ff3b30}}
/* MRI badges */
.mri-badge{{display:inline-block;font-size:9px;font-weight:700;padding:2px 7px;border-radius:3px;letter-spacing:.04em}}
.mri-normal{{background:rgba(48,209,88,.12);color:#30d158}}
.mri-elevated{{background:rgba(255,214,10,.12);color:#ffd60a}}
.mri-stressed{{background:rgba(255,159,10,.12);color:#ff9f0a}}
.mri-high{{background:rgba(255,69,58,.12);color:#ff453a}}
.mri-crisis{{background:rgba(255,55,95,.18);color:#ff375f}}
/* Exit reason icons */
.exit-icon{{font-size:14px;margin-right:5px}}
.exit-label{{font-size:11px;color:#b8bdd4}}
.icon-target{{color:#30d158}}
.icon-atr{{color:#ffd60a}}
.icon-ah{{color:#0a84ff}}
.icon-stop{{color:#ff3b30}}
.icon-ext{{color:#b8bdd4}}
/* Timestamps */
.ts-col{{min-width:160px}}
.ts-entry{{font-size:11px;color:#e2e4ee}}
.ts-exit{{font-size:10px;margin-top:2px}}
/* Partial exit badge */
.partial-badge{{font-size:8px;font-weight:700;background:rgba(10,132,255,.15);color:#0a84ff;
  padding:1px 5px;border-radius:3px;margin-left:4px;vertical-align:middle}}
.footer{{padding:12px 24px;font-size:10px;color:#b8bdd4;border-top:1px solid #161a28;text-align:center;margin-top:24px}}
/* Legend */
.legend{{display:flex;gap:16px;align-items:center;flex-wrap:wrap;font-size:10px;color:#b8bdd4;margin-bottom:20px;padding:10px 14px;
  background:#161920;border:1px solid #161a28;border-radius:6px}}
.legend-item{{display:flex;align-items:center;gap:5px}}
.legend-icon{{font-size:13px}}
/* Responsive */
@media(max-width:768px){{
  .stats-row{{grid-template-columns:repeat(2,1fr)!important}}
  .container{{padding:10px!important}}
  .header{{flex-direction:column;align-items:flex-start;gap:8px;padding:12px 14px}}
  table{{font-size:11px}}
  th,td{{padding:7px 8px!important}}
  .ts-col{{min-width:120px}}
}}
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="title">Weekly Trade Post-mortem (WTP)</div>
    <div class="sub">{week_label}</div>
  </div>
  <div class="gen-time">Generated {gen_time}</div>
</div>

<div class="container">

  <!-- Stat Tiles -->
  <div class="stats-row">
    <div class="stat">
      <div class="stat-lbl">Trades</div>
      <div class="stat-val">{n_trades}</div>
    </div>
    <div class="stat">
      <div class="stat-lbl">Win Rate</div>
      <div class="stat-val" style="font-size:18px">
        <span class="{win_rate_cls}">{win_rate:.0f}%</span>
        <span style="font-size:11px;color:#b8bdd4;display:block;margin-top:2px">{len(winners)}W / {len(losers)}L</span>
      </div>
    </div>
    <div class="stat">
      <div class="stat-lbl">Total P&amp;L</div>
      <div class="stat-val"><span class="{pnl_cls}">{pnl_sign}${total_pnl:.2f}</span></div>
    </div>
    <div class="stat">
      <div class="stat-lbl">Avg P&amp;L</div>
      <div class="stat-val"><span class="{avg_pnl_cls}">{avg_pnl_sign}${avg_pnl:.2f}</span></div>
    </div>
    <div class="stat">
      <div class="stat-lbl">Avg Score</div>
      <div class="stat-val">{avg_score:.1f}<span style="font-size:11px;color:#b8bdd4">/12</span></div>
    </div>
    <div class="stat">
      <div class="stat-lbl">Avg Missed Move</div>
      <div class="stat-val" style="font-size:18px">
        <span class="{mm_cls}">{avg_mm_display}</span>
        <span style="font-size:9px;color:#b8bdd4;display:block;margin-top:2px">vs target</span>
      </div>
    </div>
  </div>

  <!-- Legend -->
  <div class="legend">
    <strong style="color:#e2e4ee;font-size:10px">Exit Reasons:</strong>
    <span class="legend-item"><span class="legend-icon icon-target">◆</span> Target Hit</span>
    <span class="legend-item"><span class="legend-icon icon-atr">▲</span> ATR Buffer / Trail Stop</span>
    <span class="legend-item"><span class="legend-icon icon-ah">●</span> AH External</span>
    <span class="legend-item"><span class="legend-icon icon-ext">■</span> Ext. Close / Manual</span>
    <span class="legend-item"><span class="legend-icon icon-stop">✕</span> Stop Hit</span>
    <span style="margin-left:8px;color:#0a84ff;font-size:9px">+2P = 2 partial exits included</span>
  </div>

  <!-- Trade Table -->
  <div class="card">
    <div class="card-hdr">
      <span>Closed Trades</span>
      <span class="n-badge">{n_trades} trades</span>
    </div>
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Dir</th>
          <th>P&amp;L</th>
          <th>Stage (MRI)</th>
          <th>Missed Move</th>
          <th>Exit Reason</th>
          <th>Entry → Exit (PT)</th>
        </tr>
      </thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
  </div>

</div>

<div class="footer">
  alpaca-mtf-bot · WTP report · {week_label} · generated {gen_time}
  · Missed Move = (target − exit) × size  (negative = exceeded target)
</div>

</body>
</html>
"""


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Weekly Trade Post-mortem (WTP) HTML report"
    )
    parser.add_argument(
        "--week",
        metavar="YYYY-MM-DD",
        help="Any date within the target week (default: most recent completed week)",
    )
    args = parser.parse_args()

    if args.week:
        try:
            ref = date.fromisoformat(args.week)
        except ValueError:
            print(f"ERROR: --week must be YYYY-MM-DD, got: {args.week}")
            sys.exit(1)
    else:
        ref = _most_recent_completed_week()

    mon, fri = _week_bounds(ref)
    out_path  = _LOGS / f"weekly_wtp_{fri.isoformat()}.html"

    print(f"WTP report  : {mon.strftime('%b %d')} – {fri.strftime('%b %d, %Y')}")
    print(f"Reading     : {_EVENTS}")
    print(f"Output      : {out_path}")

    events = _load_events()
    all_trades = _build_closed_trades(events)
    week_trades = _filter_by_week(all_trades, mon, fri)

    print(f"Trades found: {len(all_trades)} total  →  {len(week_trades)} closed this week")

    html = generate_html(week_trades, mon, fri)

    # Atomic write: tmp → replace
    _LOGS.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_LOGS, prefix=".tmp_wtp_", suffix=".html")
    try:
        with open(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(html)
        Path(tmp_path).replace(out_path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    print(f"Written     : {out_path}")

    # Quick summary
    if week_trades:
        winners  = [t for t in week_trades if t["total_pnl"] > 0]
        total_pnl = sum(t["total_pnl"] for t in week_trades)
        pnl_sign  = "+" if total_pnl >= 0 else ""
        print(
            f"Summary     : {len(week_trades)} trades | "
            f"{len(winners)}W/{len(week_trades)-len(winners)}L | "
            f"P&L {pnl_sign}${total_pnl:.2f}"
        )


if __name__ == "__main__":
    main()
