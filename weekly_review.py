"""
weekly_review.py
Reads eod_YYYY-MM-DD.json for Mon-Fri of the current week plus latest backtest
and writes weekly_review.html atomically to the project root.

Usage:
    python3 weekly_review.py                      # data only, current week
    python3 weekly_review.py --week 2026-03-24    # specific Monday
    python3 weekly_review.py --analyze            # include Claude AI summary
"""
# ruff: noqa: E501
import os
import sys
import json
import glob
from ui_tokens import LIVE_CLOCK_HTML  # live-clock rule (2026-07-06)
import argparse
import subprocess
import logging
import statistics
import config as _cfg
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from reporting.metrics import _day_pnl, compute_lifetime_stats, compute_period_stats
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)  # noqa: E501

# Exception handlers use logger (logging module).
# Initialization/bootstrap output (RTH block, CLI progress) uses print(file=sys.stderr).
logger = logging.getLogger(__name__)

# ── Timezones ─────────────────────────────────────────────────────────────────
# RTH block removed 2026-07-02: the project-wide RTH-block removal (Rafael mandate
# 2026-06-30) missed this leftover, which sys.exit(1)'d weekly_review during market
# hours. PDT/_ET retained — used throughout for display + week rollover.
PDT = ZoneInfo("America/Los_Angeles")
_ET  = ZoneInfo("America/New_York")

ROOT     = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(ROOT, "logs")
OUT_HTML = os.path.join(ROOT, "weekly_review.html")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _monday(ref: date) -> date:
    return ref - timedelta(days=ref.weekday())


def _default_monday() -> date:
    """
    Return the Monday for the current week to display.
    Exception: on Sunday at or after 3PM PDT, return the UPCOMING Monday
    (futures market just opened — review the week ahead, not the week past).
    """
    today    = date.today()
    now_pdt  = datetime.now(PDT)
    if today.weekday() == 6 and now_pdt.hour >= 15:
        return today + timedelta(days=1)   # upcoming Monday
    return _monday(today)


def _list_archive_weeks(horizon: int = 12) -> list:
    """
    Return sorted list of Mondays to show in navigation.
    Always includes the last `horizon` Mondays (so the dropdown is always
    populated even before archive files exist), plus any older files already
    written to logs/.
    """
    weeks = set()

    # Last N Mondays from today's anchor
    anchor = date.today()
    while anchor.weekday() != 0:          # walk back to Monday
        anchor -= timedelta(days=1)
    for i in range(horizon):
        weeks.add(anchor - timedelta(weeks=i))

    # Also pick up any older archive files already on disk
    for f in glob.glob(os.path.join(LOGS_DIR, "weekly_????-??-??.html")):
        name = os.path.basename(f)
        try:
            weeks.add(date.fromisoformat(name[7:-5]))
        except (ValueError, AttributeError) as _arch_e:
            # malformed ISO date in archive filename — expected noise, skip silently
            logger.debug(
                "_list_archive_weeks: skipping malformed archive filename %r: %s",
                name, _arch_e,
            )
        except Exception as e:
            print(f"WARN [weekly_review]: unexpected error parsing archive filename {name!r}: {e}", file=sys.stderr)  # noqa: E501

    # M-5: exclude future Mondays beyond +7 days (avoids phantom weeks from pre-generated files)  # noqa: E501
    _cutoff = date.today() + timedelta(days=7)
    return sorted(w for w in weeks if w <= _cutoff)


def _load_eod(d: date) -> "dict | None":
    path = os.path.join(LOGS_DIR, f"eod_{d.isoformat()}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None  # expected — file not generated yet for this date
    except json.JSONDecodeError as e:
        print(f"WARN [weekly_review]: corrupted EOD file {path}: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"WARN [weekly_review]: unexpected error loading EOD file {path}: {e}", file=sys.stderr)  # noqa: E501
        return None


def _latest_eod_total_pnl() -> "float | None":
    """Return all_time_stats.total_pnl from the most recent EOD file. None if unavailable."""  # noqa: E501
    try:
        files = sorted(glob.glob(os.path.join(LOGS_DIR, "eod_*.json")))
        if not files:
            return None
        with open(files[-1]) as f:
            data = json.load(f)
        val = data.get("all_time_stats", {}).get("total_pnl")
        return float(val) if val is not None else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # expected — file missing or corrupt
    except Exception as e:
        print(f"WARN [weekly_review]: unexpected error reading latest EOD P&L: {e}", file=sys.stderr)  # noqa: E501
        return None


def _load_day_trades() -> list:
    """Load logs/day_trades.json. Returns list of records or []."""
    path = os.path.join(LOGS_DIR, "day_trades.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return []  # expected — no trades yet this session
    except json.JSONDecodeError as e:
        print(f"WARN [weekly_review]: corrupted day_trades.json — day trade count will be zero: {e}", file=sys.stderr)  # noqa: E501
        return []
    except Exception as e:
        print(f"WARN [weekly_review]: unexpected error loading day_trades.json: {e}", file=sys.stderr)  # noqa: E501
        return []


def _load_patch_data(monday: date, friday: date) -> dict:
    """Load patch_log.jsonl (week filter) and bug_counter.json for Patch Health section."""  # noqa: E501
    patch_log_path = os.path.join(LOGS_DIR, "patch_log.jsonl")
    week_patches = []
    if os.path.exists(patch_log_path):
        try:
            with open(patch_log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        d = entry.get("session_date", "")[:10]
                        if monday.isoformat() <= d <= friday.isoformat():
                            week_patches.append(entry)
                    except json.JSONDecodeError as e:
                        print(f"WARN [weekly_review]: malformed line in patch_log.jsonl (skipping): {e}", file=sys.stderr)  # noqa: E501
                    except Exception as e:
                        print(f"WARN [weekly_review]: unexpected error parsing patch_log entry: {e}", file=sys.stderr)  # noqa: E501
        except (FileNotFoundError, PermissionError) as e:
            print(f"WARN [weekly_review]: cannot read patch_log.jsonl: {e}", file=sys.stderr)  # noqa: E501
        except Exception as e:
            print(f"WARN [weekly_review]: unexpected error loading patch_log.jsonl: {e}", file=sys.stderr)  # noqa: E501

    bug_counter_path = os.path.join(LOGS_DIR, "bug_counter.json")
    bug_counter = {}
    file_hotspot = {}
    if os.path.exists(bug_counter_path):
        try:
            with open(bug_counter_path) as f:
                raw = json.load(f)
            bug_counter = {k: v for k, v in raw.items() if k.startswith("RC-")}
            file_hotspot = raw.get("file_hotspot", {})
        except Exception as e:
            print(f"WARN [weekly_review]: bug_counter.json parse error: {e}", file=sys.stderr)  # noqa: E501

    return {"week_patches": week_patches, "bug_counter": bug_counter, "file_hotspot": file_hotspot}  # noqa: E501


def _build_patch_health_section(monday: date) -> str:
    """Build the Patch Health card for the weekly review HTML."""
    friday = monday + timedelta(days=4)
    data = _load_patch_data(monday, friday)
    week_patches = data["week_patches"]
    bug_counter  = data["bug_counter"]
    file_hotspot = data["file_hotspot"]

    if not week_patches and not bug_counter:
        return ""

    # ── This week's patches ───────────────────────────────────────────────────
    if week_patches:
        patch_rows = ""
        for p in week_patches:
            rc       = p.get("rc_class", "—")
            rc_col   = "#ff9f0a" if rc.startswith("RC-") else "#b8bdd4"
            fix_type = p.get("fix_type", "—")
            t_col    = "#30d158" if fix_type == "bug_fix" else "#0a84ff"
            fname    = os.path.basename(p.get("file", "—"))
            desc     = p.get("description", "")
            desc     = desc[:58] + "…" if len(desc) > 58 else desc
            patch_rows += (
                f'<tr style="border-bottom:1px solid rgba(42,45,62,.4)">'
                f'<td style="color:#e2e4ee;font-weight:700;padding:5px 10px 5px 0">{p.get("patch_id","—")}</td>'  # noqa: E501
                f'<td style="color:{rc_col};font-weight:700;padding:5px 10px 5px 0">{rc}</td>'  # noqa: E501
                f'<td style="color:#b8bdd4;font-size:11px;padding:5px 10px 5px 0">{fname}</td>'  # noqa: E501
                f'<td style="color:#b8bdd4;font-size:11px;padding:5px 10px 5px 0">{desc}</td>'  # noqa: E501
                f'<td style="color:{t_col};font-size:11px;padding:5px 10px 5px 0">{fix_type}</td>'  # noqa: E501
                f'<td style="color:#636680;font-size:11px;padding:5px 0">{p.get("board_vote","—")}</td>'  # noqa: E501
                f'</tr>'
            )
        n = len(week_patches)
        patches_html = (
            f'<div style="margin-bottom:18px">'
            f'<div style="color:#b8bdd4;font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">'  # noqa: E501
            f'This Week &mdash; {n} patch{"es" if n != 1 else ""}</div>'
            f'<table style="width:100%;border-collapse:collapse">'
            f'<thead><tr style="border-bottom:1px solid #2a2d3e">'
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">ID</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">CLASS</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">FILE</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">DESCRIPTION</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">TYPE</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600">VOTE</th>'  # noqa: E501
            f'</tr></thead>'
            f'<tbody>{patch_rows}</tbody>'
            f'</table>'
            f'</div>'
        )
    else:
        patches_html = '<div style="color:#636680;font-size:12px;margin-bottom:18px">No patches this week.</div>'  # noqa: E501

    # ── RC class counters ─────────────────────────────────────────────────────
    rc_rows = ""
    for rc_key in ["RC-1", "RC-2", "RC-3", "RC-4", "RC-5", "RC-6", "RC-7", "RC-8"]:
        rc = bug_counter.get(rc_key, {})
        count = rc.get("count", 0)
        if count == 0:
            continue
        badge_col = "#ff3b30" if count >= 3 else ("#ffd60a" if count >= 2 else "#30d158")  # noqa: E501
        badge = (f'<span style="background:{badge_col};color:#000;font-size:10px;'
                 f'font-weight:700;padding:1px 7px;border-radius:4px">{count}×</span>')
        last_p = rc.get("last_patched") or "pending"
        lp_col = "#30d158" if last_p != "pending" else "#ffd60a"
        label  = (rc.get("label", ""))[:52]
        rc_rows += (
            f'<tr style="border-bottom:1px solid rgba(42,45,62,.4)">'
            f'<td style="color:#e2e4ee;font-weight:700;padding:5px 12px 5px 0">{rc_key}</td>'  # noqa: E501
            f'<td style="color:#b8bdd4;font-size:11px;padding:5px 12px 5px 0">{label}</td>'  # noqa: E501
            f'<td style="padding:5px 12px 5px 0">{badge}</td>'
            f'<td style="color:{lp_col};font-size:11px;padding:5px 0">{last_p}</td>'
            f'</tr>'
        )

    if rc_rows:
        counter_html = (
            f'<div>'
            f'<div style="color:#b8bdd4;font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">Recurring Bug Counter</div>'  # noqa: E501
            f'<table style="width:100%;border-collapse:collapse">'
            f'<thead><tr style="border-bottom:1px solid #2a2d3e">'
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 12px 6px 0">CLASS</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 12px 6px 0">DESCRIPTION</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 12px 6px 0">COUNT</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600">LAST PATCHED</th>'  # noqa: E501
            f'</tr></thead>'
            f'<tbody>{rc_rows}</tbody>'
            f'</table>'
            f'<div style="color:#636680;font-size:10px;margin-top:8px">'
            f'<span style="color:#ff3b30">&#9632;</span> ≥3 recurrences &nbsp;'
            f'<span style="color:#ffd60a">&#9632;</span> ≥2 &nbsp;'
            f'<span style="color:#30d158">&#9632;</span> 1</div>'
            f'</div>'
        )
    else:
        counter_html = ""

    # ── File hotspot table ────────────────────────────────────────────────────
    hs_rows = ""
    _hs_files = {k: v for k, v in file_hotspot.items() if k != "_meta"}
    for fname, hs in sorted(_hs_files.items(), key=lambda x: x[1].get("patch_count", 0), reverse=True):  # noqa: E501
        pc     = hs.get("patch_count", 0)
        sess   = hs.get("sessions", 0)
        lp     = hs.get("last_patched", "—")
        rating = hs.get("risk_rating", "")
        open_v = hs.get("open_rc_violations", [])
        # colour bar: CRITICAL=red, HIGH=orange, MEDIUM=yellow, LOW=green
        if "CRITICAL" in rating:
            bar_col = "#ff3b30"
        elif "HIGH" in rating:
            bar_col = "#ff9f0a"
        elif "MEDIUM" in rating:
            bar_col = "#ffd60a"
        else:
            bar_col = "#30d158"
        open_v_str = "; ".join(open_v) if open_v else "—"
        open_v_str = open_v_str[:60] + "…" if len(open_v_str) > 60 else open_v_str
        short_name = fname.split("/")[-1]
        hs_rows += (
            f'<tr style="border-bottom:1px solid rgba(42,45,62,.4)">'
            f'<td style="padding:5px 10px 5px 0">'
            f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{bar_col};margin-right:6px"></span>'  # noqa: E501
            f'<span style="color:#e2e4ee;font-size:11px">{short_name}</span></td>'
            f'<td style="color:#e2e4ee;font-weight:700;font-size:12px;padding:5px 10px 5px 0;text-align:center">{pc}</td>'  # noqa: E501
            f'<td style="color:#b8bdd4;font-size:11px;padding:5px 10px 5px 0;text-align:center">{sess}</td>'  # noqa: E501
            f'<td style="color:#636680;font-size:11px;padding:5px 10px 5px 0">{lp}</td>'
            f'<td style="color:#ffd60a;font-size:10px;padding:5px 0">{open_v_str}</td>'
            f'</tr>'
        )

    if hs_rows:
        hotspot_html = (
            f'<div style="margin-top:18px">'
            f'<div style="color:#b8bdd4;font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px">File Hotspots</div>'  # noqa: E501
            f'<table style="width:100%;border-collapse:collapse">'
            f'<thead><tr style="border-bottom:1px solid #2a2d3e">'
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">FILE</th>'  # noqa: E501
            f'<th style="text-align:center;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">PATCHES</th>'  # noqa: E501
            f'<th style="text-align:center;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">SESSIONS</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600;padding:0 10px 5px 0">LAST PATCHED</th>'  # noqa: E501
            f'<th style="text-align:left;color:#636680;font-size:10px;font-weight:600">OPEN VIOLATIONS</th>'  # noqa: E501
            f'</tr></thead>'
            f'<tbody>{hs_rows}</tbody>'
            f'</table>'
            f'<div style="color:#636680;font-size:10px;margin-top:6px">'
            f'<span style="color:#ff3b30">&#9679;</span> CRITICAL &nbsp;'
            f'<span style="color:#ff9f0a">&#9679;</span> HIGH &nbsp;'
            f'<span style="color:#ffd60a">&#9679;</span> MEDIUM &nbsp;'
            f'<span style="color:#30d158">&#9679;</span> LOW</div>'
            f'</div>'
        )
    else:
        hotspot_html = ""

    return (
        f'<div class="card" style="margin-top:20px">'
        f'<div class="card-hdr">Patch Health</div>'
        f'<div class="card-body">{patches_html}{counter_html}{hotspot_html}</div>'
        f'</div>'
    )


def _load_latest_backtest() -> "dict | None":
    pattern = os.path.join(LOGS_DIR, "backtest_12pt_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        return None
    try:
        with open(files[0]) as f:
            return json.load(f)
    except Exception as e:
        print(f"WARN [weekly_review]: backtest load failed ({files[0]}): {e}", file=sys.stderr)  # noqa: E501
        return None


def _load_trade_log() -> dict:
    path = os.path.join(ROOT, "trade_log.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"WARN [weekly_review]: trade_log.json load failed: {e}", file=sys.stderr)
        return {"open": [], "closed": [], "stats": {}}


def _fmt_reason(r: str) -> str:
    """C-3: normalize exit reason display strings — fix 'Detected Ah' → 'Detected AH', etc."""  # noqa: E501
    s = r.replace("_", " ").title()
    for _orig, _fixed in [("Detected Ah", "Detected AH"), ("Gtc", "GTC"),
                           ("Pdt", "PDT"), ("Eod", "EOD"), ("Pm ", "PM "),
                           ("Pm$", "PM"), ("Ah ", "AH "), ("Ah$", "AH")]:
        s = s.replace(_orig, _fixed)
    return s


def _strategy_validation_html(  # noqa: E501
    trade_log: "dict | None",
    lifetime_pnl: "float | None" = None,
) -> str:
    """
    Build the Live Strategy Validation card from real closed trade data.
    Replaces the stale daily-bar backtest reference with a score→outcome matrix
    and exit-reason breakdown sourced from trade_log.json.
    """
    if not trade_log:
        return ""
    closed = trade_log.get("closed", [])
    if not closed:
        return ""

    def _col(v, pos="#30d158", neg="#ff3b30", zero="#b8bdd4"):
        if v is None:
            return zero
        return pos if v > 0 else (neg if v < 0 else zero)
    def _pct_col(wr):
        return "#30d158" if wr >= 60 else ("#ffd60a" if wr >= 40 else "#ff3b30")

    # ── SOURCE: Alpaca-FIFO reconciled cache (logs/fifo_edge.json) when fresh+reconciled, else
    # the bot tracker's pnl. The tracker's pnl is corrupted (phantom exits/estimated fills) so its
    # rows never summed to the account P&L; the FIFO cache is reconciled to Alpaca to the cent, so
    # the score rows sum EXACTLY to realized. (Rafael 2026-07-14 — "the score should match the P/L".)
    _fifo = None
    try:
        _fp = os.path.join(LOGS_DIR, "fifo_edge.json")
        if os.path.exists(_fp):
            with open(_fp) as _fh:
                _fc = json.load(_fh)
            if isinstance(_fc, dict) and _fc.get("by_score") and _fc.get("reconciles"):
                _fifo = _fc
    except Exception:
        _fifo = None

    _n_min = 5   # P1-SCORE-TABLE: min trades for full confidence display

    def _score_row(lbl: str, n: int, wr: float, aw: float, al: float, tpnl: float, has_w: bool, has_l: bool) -> str:
        _row_bg = ("rgba(48,209,88,.07)" if wr >= 60 else "rgba(255,214,10,.07)" if wr >= 40 else "rgba(255,59,48,.07)")
        _n_lbl = f'{n} <span style="font-size:9px;color:#636680">(n={n})</span>' if n < _n_min else str(n)
        return (
            f'<tr style="background:{_row_bg}">'
            f'<td style="color:#e2e4ee;font-weight:700">{lbl}</td>'
            f'<td style="color:#b8bdd4">{_n_lbl}</td>'
            f'<td style="color:{_pct_col(wr)};font-weight:700">{wr:.0f}%</td>'
            f'<td style="color:#30d158">{f"+${aw:.2f}" if has_w else "—"}</td>'
            f'<td style="color:#ff3b30">{f"${al:.2f}" if has_l else "—"}</td>'
            f'<td style="color:{_col(tpnl)};font-weight:700">{("+" if tpnl>=0 else "")}${tpnl:.2f}</td>'
            f'</tr>'
        )

    if _fifo is not None:
        bs = _fifo["by_score"]
        total_trades = int(_fifo.get("n_legs", 0))
        all_wr    = float(_fifo.get("win_pct", 0))
        total_pnl = float(_fifo.get("realized", 0.0))   # rows sum to this (reconciled to Alpaca)
        _wn = sum(b["wins"] for b in bs.values())
        _ln = sum(b["loss_n"] for b in bs.values())
        avg_w = sum(b["avg_win"] * b["wins"] for b in bs.values()) / _wn if _wn else 0
        avg_l = sum(b["avg_loss"] * b["loss_n"] for b in bs.values()) / _ln if _ln else 0
        pf    = abs(avg_w / avg_l) if avg_l != 0 else float("inf")
        score_rows = ""
        for key in sorted(bs.keys(), key=lambda x: (0, int(x)) if x.isdigit() else (1, 0)):
            b = bs[key]
            lbl = f"{key}/12" if key.isdigit() else "pre-score"
            score_rows += _score_row(lbl, b["n"], b["win_pct"], b["avg_win"], b["avg_loss"], b["pnl"], b["wins"] > 0, b["loss_n"] > 0)
    else:
        # Fallback: bot tracker (corrupted pnl — rows will NOT reconcile; used only if cache absent).
        total_trades = len(closed)
        all_wins  = [t for t in closed if (t.get("pnl") or 0) > 0]
        all_losses = [t for t in closed if (t.get("pnl") or 0) <= 0]
        all_wr    = len(all_wins) / total_trades * 100 if total_trades else 0
        avg_w     = sum((t.get("pnl") or 0) for t in all_wins)  / len(all_wins)  if all_wins  else 0
        avg_l     = sum((t.get("pnl") or 0) for t in all_losses) / len(all_losses) if all_losses else 0
        total_pnl = (lifetime_pnl if lifetime_pnl is not None else compute_lifetime_stats().get("total_pnl", 0.0))
        pf        = abs(avg_w / avg_l) if avg_l != 0 else float("inf")
        bands: dict[int | str, list] = {}
        for t in closed:
            score = t.get("score")
            bands.setdefault(int(score) if score is not None else "?", []).append(t)
        score_rows = ""
        for score in sorted(bands.keys(), key=lambda x: (0, x) if isinstance(x, int) else (1, 0)):
            trades = bands[score]
            n = len(trades)
            wins   = [t for t in trades if (t.get("pnl") or 0) > 0]
            losses = [t for t in trades if (t.get("pnl") or 0) <= 0]
            wr     = len(wins) / n * 100 if n else 0
            aw     = sum((t.get("pnl") or 0) for t in wins)   / len(wins)   if wins   else 0
            al     = sum((t.get("pnl") or 0) for t in losses) / len(losses) if losses else 0
            tpnl   = sum(t.get("pnl") or 0 for t in trades)
            lbl    = f"{score}/12" if isinstance(score, int) else "pre-score"
            score_rows += _score_row(lbl, n, wr, aw, al, tpnl, bool(wins), bool(losses))

    # ── Exit reason breakdown — bucket into clean categories ─────────────────
    _REASON_CATS = [
        ("Trail Stop",   "#ff6b35", lambda r: "trail_stop" in r),
        ("Breakeven",    "#ff9f0a", lambda r: "breakeven" in r),
        ("Hard Stop",    "#ff3b30", lambda r: any(x in r for x in ("hard_stop", "stop")) and "trail" not in r and "breakeven" not in r),  # noqa: E501
        ("Target",       "#30d158", lambda r: "target" in r),
        ("Signal Exit",  "#ffd60a", lambda r: "signal" in r or "opposite" in r),
        ("Other",        "#636680", lambda r: True),
    ]
    def _cat_label(raw: str) -> tuple[str, str]:
        r = raw.lower()
        for label, color, test in _REASON_CATS:
            if test(r):
                return label, color
        return "Other", "#636680"

    by_exit: dict[str, list] = {}
    for t in closed:
        raw_r = (t.get("exit_reason") or "unknown").split("|")[0].strip()
        label, _ = _cat_label(raw_r)
        by_exit.setdefault(label, []).append(t)

    exit_rows = ""
    # Render in canonical category order (not by P&L) so the table is consistent
    for _cat_lbl, _cat_col, _ in _REASON_CATS:
        trades = by_exit.get(_cat_lbl, [])
        if not trades:
            continue
        _exit_wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
        tpnl  = sum(t.get("pnl") or 0 for t in trades)
        wr    = _exit_wins / len(trades) * 100 if trades else 0
        exit_rows += (
            f'<tr>'
            f'<td style="color:{_cat_col};font-weight:600">'
            f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
            f'background:{_cat_col};margin-right:6px;vertical-align:middle"></span>'
            f'{_cat_lbl}</td>'
            f'<td style="color:#b8bdd4;text-align:center">{len(trades)}</td>'
            f'<td style="color:{_pct_col(wr)};text-align:center">{wr:.0f}%</td>'
            f'<td style="color:{_col(tpnl)};font-weight:700;text-align:right">{("+" if tpnl>=0 else "")}${tpnl:.2f}</td>'  # noqa: E501
            f'</tr>'
        )

    # ── All-time summary KPIs ─────────────────────────────────────────────────
    pf_str  = f"{pf:.2f}" if pf != float("inf") else "∞"
    wr_col  = _pct_col(all_wr)
    pf_col  = "#30d158" if pf >= 1.5 else ("#ffd60a" if pf >= 1.0 else "#ff3b30")
    pnl_col = _col(total_pnl)

    # Max drawdown (running cumulative P/L peak-to-trough)
    _cum = 0.0
    _peak = 0.0
    _max_dd = 0.0
    for _t in sorted(closed, key=lambda x: x.get("exit_time") or ""):
        _cum += (_t.get("pnl") or 0)
        if _cum > _peak:
            _peak = _cum
        if (_peak - _cum) > _max_dd:
            _max_dd = _peak - _cum
    _max_dd = round(_max_dd, 2)

    # Avg hold time — split intraday (minutes) vs overnight (calendar days)
    _intraday_mins = []
    _overnight_days = []
    for _t in closed:
        try:
            _en = datetime.fromisoformat(_t["entry_time"])
            _ex = datetime.fromisoformat(_t["exit_time"])
            if _t.get("overnight"):
                # Calendar days: meaningful unit for multi-day holds
                _days = max(1, (_ex.date() - _en.date()).days)
                _overnight_days.append(_days)
            else:
                _intraday_mins.append((_ex - _en).total_seconds() / 60)
        except Exception as e:
            print(f"WARN [weekly_review]: hold_time parse skipped for trade: {e}", file=sys.stderr)  # noqa: E501

    # Intraday: format as Xh Ym or Xm
    _avg_intra = round(sum(_intraday_mins) / len(_intraday_mins)) if _intraday_mins else None  # noqa: E501
    if _avg_intra is None:
        _avg_intra_str = "—"
    elif _avg_intra < 60:
        _avg_intra_str = f"{_avg_intra}m"
    else:
        _avg_intra_str = f"{_avg_intra // 60}h {_avg_intra % 60}m"

    # Overnight: format as X.X days
    _avg_ovnt_days = round(sum(_overnight_days) / len(_overnight_days), 1) if _overnight_days else None  # noqa: E501
    _avg_ovnt_str  = f"{_avg_ovnt_days}d" if _avg_ovnt_days is not None else "—"

    # ROI % (based on $2,500 starting capital)
    _STARTING_CAP = 2500.0
    _roi_pct = round(total_pnl / _STARTING_CAP * 100, 1)
    _roi_str = f"{'+' if _roi_pct >= 0 else ''}{_roi_pct:.1f}%"
    _roi_col = "#30d158" if _roi_pct > 0 else ("#ff3b30" if _roi_pct < 0 else "#636680")

    # n<30 disclaimer + data timestamp
    _n30_warn = ('<span style="color:#ffd60a;font-weight:700">⚠ n&lt;30 — results may not be '  # noqa: E501
                 'statistically significant</span> · ') if total_trades < 30 else ''
    _data_ts = datetime.now(PDT).strftime("%b %d %Y %I:%M %p PT")

    th = 'style="font-family:monospace;font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:#636680;padding:6px 12px;text-align:left;border-bottom:1px solid #1a1e30"'  # noqa: E501

    # Reconciliation bridge — realized (edge total) → account total (equity−2500). Prevents the
    # "why don't these match" confusion: they differ by fees + open unrealized, all shown.
    if _fifo is not None:
        _recon_note = (
            '<div style="font-size:10px;color:#8a94ae;margin:0 0 14px;padding:8px 12px;'
            'background:rgba(48,209,88,.05);border-left:2px solid #30d158;border-radius:4px">'
            f'✓ <b style="color:#30d158">Alpaca-FIFO reconciled</b> — closed-trade realized '
            f'<b>{"+" if total_pnl>=0 else ""}${total_pnl:,.2f}</b> (rows below sum to this) '
            f'&minus; fees ${_fifo["fees"]:.2f} {"+" if _fifo["unrealized"]>=0 else "&minus; $"}'
            f'{abs(_fifo["unrealized"]):,.2f} open unrealized = account '
            f'<b>{"+" if _fifo["equity_minus_2500"]>=0 else ""}${_fifo["equity_minus_2500"]:,.2f}</b> '
            f'(equity &minus; $2,500). Residual ${_fifo["residual"]:+.2f}.</div>'
        )
    else:
        _recon_note = ('<div style="font-size:10px;color:#ffd60a;margin:0 0 14px">&#9888; Source: bot '
                       'tracker (unreconciled — FIFO cache unavailable).</div>')

    return f"""
<div class="card">
  <div class="card-hdr">
    Live Strategy Validation
    <span class="badge badge-info" style="margin-left:8px">all-time · {total_trades} trades</span>
  </div>
  <div class="card-body">

    <div style="display:grid;grid-template-columns:repeat(8,1fr);gap:10px;margin-bottom:20px">
      <div class="bt-stat"><div class="bt-lbl">All-Time Win Rate</div>
        <div class="bt-val" style="color:{wr_col}">{all_wr:.1f}%</div></div>
      <div class="bt-stat"><div class="bt-lbl">Profit Factor</div>
        <div class="bt-val" style="color:{pf_col}">{pf_str}</div></div>
      <div class="bt-stat"><div class="bt-lbl">Avg Win / Avg Loss</div>
        <div class="bt-val" style="color:#e2e4ee;font-size:13px">${avg_w:.2f} / ${abs(avg_l):.2f}</div></div>
      <div class="bt-stat"><div class="bt-lbl">Total Realized P&amp;L</div>
        <div class="bt-val" style="color:{pnl_col}">{("+" if total_pnl>=0 else "")}${total_pnl:.2f}</div></div>
      <div class="bt-stat"><div class="bt-lbl">Max Drawdown</div>
        <div class="bt-val" style="color:#ff3b30">${_max_dd:.2f}</div></div>
      <div class="bt-stat"><div class="bt-lbl">Avg Intraday Hold</div>
        <div class="bt-val" style="color:#e2e4ee;font-size:14px">{_avg_intra_str}</div>
        <div style="font-size:9px;color:#4a5070;margin-top:2px">{"no intraday closes" if _avg_intra is None else "same-day only"}</div></div>
      <div class="bt-stat"><div class="bt-lbl">Avg Overnight Hold</div>
        <div class="bt-val" style="color:#e2e4ee;font-size:14px">{_avg_ovnt_str}</div>
        <div style="font-size:9px;color:#4a5070;margin-top:2px">calendar days</div></div>
      <div class="bt-stat"><div class="bt-lbl">ROI % ($2.5K)</div>
        <div class="bt-val" style="color:{_roi_col}">{_roi_str}</div></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">

      <div>
        {_recon_note}
        <div style="font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#636680;margin-bottom:8px">Score → Outcome</div>
        <table style="width:100%;border-collapse:collapse">
          <thead><tr>
            <th {th}>Score</th><th {th}>Trades</th><th {th}>WR</th>
            <th {th}>Avg W</th><th {th}>Avg L</th><th {th}>P&L</th>
          </tr></thead>
          <tbody>{score_rows}</tbody>
        </table>
      </div>

      <div>
        <div style="font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#636680;margin-bottom:8px">Exit Reason → Outcome</div>
        <table style="width:100%;border-collapse:collapse">
          <thead><tr>
            <th {th}>Reason</th><th {th}>N</th><th {th}>WR</th><th {th}>P&L</th>
          </tr></thead>
          <tbody>{exit_rows}</tbody>
        </table>
      </div>

    </div>

    <div style="font-size:10px;color:#636680;margin-top:12px">
      {_n30_warn}Live closed trades only · includes bot exits + manually closed (external_close) + manual_close ·
      Score 0 = pre-scoring era trades · Data as of: {_data_ts}
    </div>

  </div>
</div>"""  # noqa: E501


def _day_driver(eod: dict) -> str:
    """Return one line per trade: 'SYM direction QTY @ $PRICE → ±$PNL'"""
    trades = eod.get("trades") or []
    if not trades:
        return "No trades"
    def _fmt(t):
        sym   = t.get("symbol", "?")
        direc = t.get("direction", "long")
        qty   = t.get("qty", 0)
        entry = t.get("entry_price", 0)
        p     = t.get("pnl", 0)
        sign  = "+" if p >= 0 else "-"
        return f"{sym} {direc} {qty} @ ${entry:.2f} → {sign}${abs(p):.2f}"
    lines = [_fmt(t) for t in trades]
    return "<br>".join(lines) if lines else "No closed P&L"


def _classify_loss_driver(eod: dict):
    """
    Determine the prevailing loss driver for a negative P&L day.

    Returns (label, hex_color) or None if day was flat/positive.

    Classification:
      MECHANICAL — overnight_atr_buffer_exit, safe_close_all, external_close, pm_exit
      STRATEGY   — signal, hard_stop, trail_stop, opposite_signal, stop, target
    Manual override: eod["loss_driver"] = "MECHANICAL" | "STRATEGY"

    Prevailing driver = category with larger total absolute dollar loss.
    Only surfaced when day P&L < 0.
    """
    pnl_day = eod.get("pnl_today", 0)
    if pnl_day >= 0:
        return None  # only badge losing days

    # Manual override takes priority
    override = eod.get("loss_driver", "").upper()
    if override in ("MECHANICAL", "STRATEGY"):
        label = override
        color = "#ffd60a" if label == "MECHANICAL" else "#ff3b30"
        return (label, color)  # M-2: return semantic label; UI layer maps to display string  # noqa: E501

    # Auto-classify from per-trade exit_reason
    MECHANICAL_REASONS = {
        "overnight_atr_buffer_exit", "overnight_breakeven",  # overnight_breakeven = legacy alias pre-2026-05-05  # noqa: E501
        "safe_close_all", "external_close",
        "pm_exit", "overnight_exit", "gtc_cancel", "reconcile",
    }
    STRATEGY_REASONS = {
        "signal", "hard_stop", "trail_stop", "opposite_signal",
        "stop", "target", "partial", "manual",
    }

    mech_loss    = 0.0
    strat_loss   = 0.0

    for t in eod.get("trades") or []:
        raw_pnl    = t.get("pnl", 0)
        if raw_pnl >= 0:
            continue   # only count losers toward driver
        abs_loss   = abs(raw_pnl)
        reason     = (t.get("exit_reason") or t.get("reason") or "").lower().strip()
        if reason in MECHANICAL_REASONS:
            mech_loss  += abs_loss
        elif reason in STRATEGY_REASONS:
            strat_loss += abs_loss
        else:
            # Unknown reason — split 50/50 so neither dominates
            mech_loss  += abs_loss / 2
            strat_loss += abs_loss / 2

    if mech_loss == 0 and strat_loss == 0:
        return None  # no closed losers to classify

    if mech_loss >= strat_loss:
        return ("MECHANICAL", "#ffd60a")   # M-2: semantic label; UI maps → "⚙ BUG IDENTIFIED"  # noqa: E501
    else:
        return ("STRATEGY", "#ff3b30")     # M-2: semantic label; UI maps → "✕ STRATEGY"


def _week_stats(week_eods: dict) -> dict:
    days = [v for v in week_eods.values() if v]
    if not days:
        return {}
    all_trades = [t for d in days for t in (d.get("trades") or []) if t.get("status") == "closed"]  # noqa: E501
    _wk_dates = sorted(week_eods.keys())
    week_pnl  = compute_period_stats(
        date.fromisoformat(_wk_dates[0]),
        date.fromisoformat(_wk_dates[-1]),
    ).get("total_pnl", 0.0)
    total      = len(all_trades)
    wins       = sum(1 for t in all_trades if _day_pnl(t) > 0)
    wr         = round(wins / total * 100) if total else 0
    scores     = [t["score"] for t in all_trades if t.get("score")]
    avg_score  = round(sum(scores) / len(scores), 1) if scores else None
    tqi_scores = [t["tqi_score"] for t in all_trades if t.get("tqi_score") is not None]
    avg_tqi    = round(sum(tqi_scores) / len(tqi_scores)) if tqi_scores else None
    overnight  = sum(len(d.get("overnight_holds") or []) for d in days)
    stops      = sum((d.get("exit_reasons") or {}).get("stop", 0) for d in days)
    targets    = sum((d.get("exit_reasons") or {}).get("target", 0) for d in days)
    # Per-reason exit counts across all days (full breakdown, not just stop/target)
    exit_reasons: dict = {}
    for _d in days:
        for _reason, _cnt in ((_d.get("exit_reasons")) or {}).items():
            exit_reasons[_reason] = exit_reasons.get(_reason, 0) + int(_cnt)
    # Collect unique SPY event types active during entries this week
    spy_events = sorted(set(
        t.get("spy_event_type") for t in all_trades
        if t.get("spy_event_type")
    ))
    return dict(
        week_pnl=week_pnl, total=total, wins=wins, wr=wr,
        avg_score=avg_score, avg_tqi=avg_tqi, overnight=overnight,
        stops=stops, targets=targets, exit_reasons=exit_reasons, spy_events=spy_events,
    )

# ── CSS (same dark theme as scan_results.html) ────────────────────────────────
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f1a;color:#e8ecff;font-family:-apple-system,"SF Pro Text",sans-serif;font-size:13px;line-height:1.4}
.header{background:#13162a;border-bottom:1px solid #252847;padding:13px 24px;
  display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.title{font-size:15px;font-weight:700}
.sub{font-size:11px;color:#8a94ae;margin-top:2px}
.container{max-width:1200px;margin:0 auto;padding:24px}
.card{background:#13162a;border:1px solid #252847;border-radius:8px;margin-bottom:20px;overflow:hidden}
.card-hdr{background:#1e2240;border-bottom:1px solid #252847;padding:11px 20px;
  font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#8a94ae}
.card-body{padding:20px}
.stats-row{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:20px}
.stat{background:#13162a;border:1px solid #252847;border-radius:6px;padding:14px;text-align:center}
.stat-lbl{font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#8a94ae;margin-bottom:6px}
.stat-val{font-size:20px;font-weight:700}
.days-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:20px}
.day{background:#13162a;border:1px solid #252847;border-radius:6px;padding:14px}
.day.active{border-color:#00e5ff}
.day.no-data{opacity:.5}
.day-name{font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#8a94ae;margin-bottom:2px}
.day-date{font-size:11px;color:#8a94ae;margin-bottom:8px}
.day-pnl{font-size:22px;font-weight:700;margin-bottom:4px}
.day-driver{font-size:10px;color:#8a94ae;line-height:1.5}
.day-meta{font-size:10px;color:#8a94ae;margin-top:4px}
.pos{color:#30d158}.neg{color:#ff3b30}.zero{color:#8a94ae}
.bt-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.bt-stat{background:#0d0f1a;border:1px solid #252847;border-radius:5px;padding:12px;text-align:center}
.bt-lbl{font-size:9px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#8a94ae;margin-bottom:4px}
.bt-val{font-size:16px;font-weight:700}
.badge{display:inline-block;font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;
  letter-spacing:.06em;text-transform:uppercase}
.badge-info{background:rgba(0,229,255,.15);color:#00e5ff}
.footer{padding:12px 24px;font-size:10px;color:#8a94ae;border-top:1px solid #252847;text-align:center}
.exec-card{background:#13162a;border:1px solid #252847;border-radius:8px;padding:18px 22px;margin-bottom:20px}
.exec-label{font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#00e5ff;margin-bottom:8px}
.exec-text{font-size:14px;line-height:1.7;color:#e8ecff}
.ai-section{margin-bottom:20px}
.ai-hdr{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:#8a94ae;padding:10px 0 8px;border-bottom:1px solid #252847;margin-bottom:12px}
.ai-body{font-size:13px;line-height:1.8;color:#e8ecff}
.rec-item{background:#0d0f1a;border:1px solid #252847;border-radius:6px;
  padding:14px 16px;margin-bottom:10px}
.rec-title{font-size:13px;font-weight:600;color:#e8ecff;margin-bottom:4px}
.rec-detail{font-size:12px;color:#8a94ae;line-height:1.6}
.rec-change{font-size:11px;color:#ffd60a;margin-top:6px;font-family:monospace}
.conf-high{color:#30d158}.conf-med{color:#ffd60a}.conf-low{color:#ff3b30}
.board-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:4px}
.board-item{background:#0d0f1a;border:1px solid #252847;border-radius:6px;padding:12px 14px}
.board-role{font-size:9px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#bf5af2;margin-bottom:4px}
.board-pov{font-size:12px;color:#8a94ae;line-height:1.5;margin-bottom:4px}
.board-next{font-size:11px;color:#00e5ff}
@media(max-width:768px){
  .stats-row{grid-template-columns:repeat(2,1fr)!important}
  .days-grid{grid-template-columns:1fr!important}
  .bt-grid{grid-template-columns:repeat(2,1fr)!important}
  .board-grid{grid-template-columns:1fr!important}
  .container{padding:10px!important}
  .header{flex-direction:column;align-items:flex-start;gap:8px;padding:12px 14px}
  table{font-size:11px}
  th,td{padding:6px 8px!important}
}
"""  # noqa: E501

# ── AI analysis ───────────────────────────────────────────────────────────────

def _run_analysis(week_eods: dict, stats: dict, backtest: dict) -> "dict | None":
    """
    Call Gemini with week summary. Returns parsed dict or None on failure.
    Uses GEMINI_API_KEY — same key as nightly_audit.py, no separate billing needed.
    """
    from google import genai as _genai
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("  GEMINI_API_KEY not set — skipping AI analysis")
        return None

    # Build compact summary for the prompt
    days_summary = {}
    for d, eod in week_eods.items():
        if not eod:
            continue
        days_summary[d] = {
            "pnl": eod.get("pnl_today", 0),
            "trades": eod.get("trades_today", 0),
            "win_rate": eod.get("win_rate_today", 0),
            "exit_reasons": eod.get("exit_reasons", {}),
            "trade_details": [
                {"symbol": t.get("symbol"), "direction": t.get("direction"),
                 "score": t.get("score"), "pnl": t.get("pnl"),
                 "reason": t.get("reason"), "overnight": t.get("overnight")}
                for t in (eod.get("trades") or [])
            ],
        }

    bt_summary = {}
    if backtest:
        agg = backtest.get("aggregate", {})
        bt_summary = {
            "win_rate": agg.get("win_rate"),
            "sharpe":   agg.get("sharpe_ratio"),
            "max_dd":   agg.get("max_drawdown_pct"),
            "exit_reasons": agg.get("exit_reasons", {}),
        }

    prompt = f"""You are the Advisory Board for "Raf's Trading Bot" — a multi-timeframe confluence algo
on Alpaca paper (~$2.5K). 12-point scoring, min 9/12 to enter. ATR-based stops (1.20x) and targets (2.5x).
Partial exit at 0.8x ATR, trailing stop at 0.5x ATR. Do not reference Pattern Day Trader (PDT) rules — they are no longer enforced.

Weekly performance data:
{json.dumps(days_summary, indent=2)}

Backtest reference (daily-bar proxy, not intraday):
{json.dumps(bt_summary, indent=2)}

Respond ONLY with valid JSON, no markdown, no extra text:
{{
  "exec_summary": "one sentence verdict on the week",
  "what_worked": "one to two sentences max",
  "what_didnt_work": "one to two sentences max",
  "recommendations": [
    {{
      "title": "short title",
      "description": "one sentence",
      "proposed_change": "specific and concrete e.g. raise MIN_SCORE from 9 to 10",
      "confidence": "high|medium|low"
    }}
  ],
  "board_povs": {{
    "quant_research": {{"pov": "one sentence", "next_step": "one sentence"}},
    "risk":           {{"pov": "one sentence", "next_step": "one sentence"}},
    "execution":      {{"pov": "one sentence", "next_step": "one sentence"}},
    "product":        {{"pov": "one sentence", "next_step": "one sentence"}}
  }}
}}"""  # noqa: E501

    _models = ["gemini-2.5-flash", "gemini-2.0-flash-lite"]
    try:
        client = _genai.Client(api_key=api_key)
        last_err = None
        for _model in _models:
            try:
                response = client.models.generate_content(
                    model=_model,
                    contents=prompt,
                )
                raw = (response.text or "").strip()
                # Strip accidental markdown fences
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                return json.loads(raw)
            except Exception as _me:
                print(f"  {_model} failed: {_me}")
                last_err = _me
        print(f"  AI analysis failed: all Gemini models exhausted. Last: {last_err}")
        return None
    except Exception as e:
        print(f"  AI analysis failed: {e}")
        return None


# ── Deterministic executive summary metrics ───────────────────────────────────

def _exec_summary_stats(trade_log: "dict | None", monday: date) -> str:
    """
    4-metric executive summary: Edge Ratio, Score Monotonicity, Early Exit Rate
    Among Losers, MRI P&L split (multi-entry attribution corrected).
    Week = monday through monday+4 days (Friday, inclusive).
    """
    friday = monday + timedelta(days=4)

    # ── Filter closed trades to this week ────────────────────────────────────────────
    week_closed: list = []
    for _t in (trade_log or {}).get("closed", []):
        try:
            _et = datetime.fromisoformat(_t["exit_time"]).astimezone(PDT).date()
            if monday <= _et <= friday:
                week_closed.append(_t)
        except Exception as _e:
            print(f"WARN [exec_stats] bad exit_time: {_e}", file=sys.stderr)

    if not week_closed:
        # Cross-check: do EOD files exist for this week? If so, records are
        # incomplete (external close path) — warn instead of misleading fallback.
        _eod_exists = any(
            os.path.exists(os.path.join(LOGS_DIR, f"eod_{(monday + timedelta(days=_d)).strftime('%Y-%m-%d')}.json"))  # noqa: E501
            for _d in range(5)
        )
        if _eod_exists:
            return (
                '<div style="color:#f5a623;font-style:italic">'
                "Trade records incomplete — some exits may have closed externally. "
                "See stats row for P&amp;L summary."
                "</div>"
            )
        return (
            '<div style="color:#8e92a4;font-style:italic">'
            "No closed trades this week.</div>"
        )

    # ── Load trade_events.jsonl (entry events only — MRI attribution) ─────────────────
    _entry_events: list = []  # (symbol, entry_date, mri_level)
    try:
        with open(os.path.join(LOGS_DIR, "trade_events.jsonl")) as _f:
            for _raw in _f:
                _raw = _raw.strip()
                if not _raw:
                    continue
                try:
                    _ev = json.loads(_raw)
                except Exception:
                    continue
                try:
                    _ts_d = datetime.fromisoformat(_ev["ts"]).astimezone(PDT).date()
                except Exception:
                    continue
                if not (monday <= _ts_d <= friday):
                    continue
                if _ev.get("event") == "entry":
                    _entry_events.append(
                        (_ev.get("symbol", ""), _ts_d, _ev.get("mri_level", "UNKNOWN"))
                    )
    except Exception as _te_e:
        print(f"WARN [exec_stats] trade_events.jsonl: {_te_e}", file=sys.stderr)

    def _entry_mri(symbol: str, exit_dt: date) -> str:
        """MRI of the most recent entry for symbol on or before exit_dt."""
        candidates = [
            (dt, mri) for sym, dt, mri in _entry_events
            if sym == symbol and dt <= exit_dt
        ]
        return max(candidates, key=lambda x: x[0])[1] if candidates else "UNKNOWN"

    # ── 1. Edge Ratio (Thorp — Kelly Criterion; A Man for All Markets) ────────────────
    # Edge = (WR × avg_win − (1−WR) × avg_loss) / avg_loss
    _pnls = [float(_t.get("pnl") or 0.0) for _t in week_closed]
    _win_pnls = [p for p in _pnls if p > 0]
    _loss_pnls = [abs(p) for p in _pnls if p < 0]
    _n_total = len(_pnls)
    if _win_pnls and _loss_pnls:
        _edge_wr = len(_win_pnls) / _n_total
        _avg_win = statistics.mean(_win_pnls)
        _avg_loss = statistics.mean(_loss_pnls)
        _edge = (_edge_wr * _avg_win - (1 - _edge_wr) * _avg_loss) / _avg_loss
        _ec = "#30d158" if _edge > 0 else "#ff453a"
        _edge_str = (
            f'<span style="color:{_ec}">{_edge:+.2f}</span>'
            f' (WR {_edge_wr:.0%} · avg W ${_avg_win:.2f} · avg L ${_avg_loss:.2f})'
        )
    elif _win_pnls:
        _edge_str = (
            '<span style="color:#30d158">100% win rate — no losses this week ✓</span>'
        )
    elif _loss_pnls:
        _edge_str = (
            f'<span style="color:#ff453a">0% win rate ({len(_loss_pnls)} losses)</span>'
        )
    else:
        _edge_str = '<span style="color:#8e92a4">—</span>'

    # ── 2. Score Monotonicity (boundary WRs + verdict) ────────────────────────
    _bkts: dict = {10: [], 11: [], 12: []}
    for _t in week_closed:
        _sc = _t.get("score")
        if _sc in _bkts:
            _bkts[_sc].append(float(_t.get("pnl") or 0.0))

    def _bkt_wr(pnls: list) -> "tuple[float | None, int]":
        if not pnls:
            return None, 0
        return sum(1 for p in pnls if p > 0) / len(pnls), len(pnls)

    _wr12, _n12 = _bkt_wr(_bkts[12])
    _wr11, _n11 = _bkt_wr(_bkts[11])
    _wr10, _n10 = _bkt_wr(_bkts[10])
    _defined = [
        (wr, sc) for wr, sc in [(_wr12, 12), (_wr11, 11), (_wr10, 10)] if wr is not None
    ]
    _is_mono = all(
        _defined[i][0] >= _defined[i + 1][0] for i in range(len(_defined) - 1)
    )
    _mono_badge = (
        '<span style="color:#30d158">✓</span>'
        if _is_mono
        else '<span style="color:#ff453a">⚠ INVERSION</span>'
    )
    if len(_defined) >= 2:
        _hi_wr, _hi_sc = _defined[0]
        _lo_wr, _lo_sc = _defined[-1]
        _mono_str = (
            f'{_hi_sc}/12: <span style="color:#e2e4ee">{_hi_wr:.0%}</span>'
            f' → {_lo_sc}/12: <span style="color:#e2e4ee">{_lo_wr:.0%}</span>'
            f' {_mono_badge}'
        )
    elif _defined:
        _only_wr, _only_sc = _defined[0]
        _only_n = len(_bkts[_only_sc])
        _mono_str = (
            f'{_only_sc}/12: <span style="color:#e2e4ee">{_only_wr:.0%}</span>'
            f' (N={_only_n}) — <span style="color:#8e92a4">single score band</span>'
        )
    else:
        _mono_str = '<span style="color:#8e92a4">— insufficient data</span>'

    # ── 3. Early Exit Rate Among Losers (Douglas — Trading in the Zone) ───────────
    # Early = system-managed exit (trail, breakeven, target, signal) — loss cut short
    # Late  = ran to hard stop — full planned loss taken
    _EARLY = {"trail_stop", "breakeven", "signal_exit", "target", "partial"}
    _loss_trades = [_t for _t in week_closed if float(_t.get("pnl") or 0.0) < 0]
    _early_losses = [
        _t for _t in _loss_trades
        if any(r in (_t.get("exit_reason") or "").lower() for r in _EARLY)
    ]
    if _loss_trades:
        _el_pct = len(_early_losses) / len(_loss_trades)
        _el_c = (
            "#30d158" if _el_pct >= 0.4
            else "#ffd60a" if _el_pct >= 0.2
            else "#ff453a"
        )
        _el_str = (
            f'<span style="color:{_el_c}">{_el_pct:.0%}</span>'
            f' ({len(_early_losses)}/{len(_loss_trades)} losses cut early)'
        )
    else:
        _el_str = '<span style="color:#8e92a4">no losses ✓</span>'

    # ── 4. MRI P&L split (keyed on symbol+date — fixes multi-entry attribution) ──
    _mpnl: dict = {}
    _mn: dict = {}
    for _t in week_closed:
        _sym = _t.get("symbol", "")
        _xdt: date = monday
        try:
            _xdt = datetime.fromisoformat(_t["exit_time"]).astimezone(PDT).date()
        except Exception as _xdt_e:
            logger.warning(
                "[%s] _exec_summary_stats: exit_time parse failed — "
                "defaulting _xdt to monday (%s): %s",
                _sym, monday, _xdt_e,
            )
        _mri = _entry_mri(_sym, _xdt)
        _pnl_v = float(_t.get("pnl") or 0.0)
        _mpnl[_mri] = _mpnl.get(_mri, 0.0) + _pnl_v
        _mn[_mri] = _mn.get(_mri, 0) + 1

    def _mri_cell(label: str, pnl: float, n: int) -> str:
        _c = "#30d158" if pnl >= 0 else "#ff453a"
        _s = f'+${pnl:.2f}' if pnl >= 0 else f'−${abs(pnl):.2f}'
        return f'{label}: <span style="color:{_c}">{_s}</span> (N={n})'

    _norm_pnl = _mpnl.get("NORMAL", 0.0)
    _norm_n = _mn.get("NORMAL", 0)
    _caut_pnl = sum(v for k, v in _mpnl.items() if k not in ("NORMAL", "UNKNOWN"))
    _caut_n = sum(v for k, v in _mn.items() if k not in ("NORMAL", "UNKNOWN"))
    _mri_str = (
        f'{_mri_cell("NORMAL", _norm_pnl, _norm_n)}'
        f' &nbsp;·&nbsp; {_mri_cell("CAUTION+", _caut_pnl, _caut_n)}'
    )

    # ── Assemble HTML ────────────────────────────────────────────────────────
    _L = (
        'style="color:#8e92a4;font-size:10px;font-weight:700;letter-spacing:.08em'
        ';text-transform:uppercase;margin-right:6px;flex-shrink:0"'
    )
    _R = (
        'style="display:flex;flex-wrap:wrap;align-items:baseline;gap:0 4px'
        ';font-size:13px;line-height:1.9;color:#e2e4ee;margin-bottom:2px"'
    )
    return (
        '<div style="display:flex;flex-direction:column;border-bottom:1px solid #2a2e3d'
        ';padding-bottom:12px;margin-bottom:12px">'
        + f'<div {_R}><span {_L}>Edge</span>{_edge_str}</div>'
        + f'<div {_R}><span {_L}>Score</span>{_mono_str}</div>'
        + f'<div {_R}><span {_L}>Loss Quality</span>{_el_str}</div>'
        + f'<div {_R}><span {_L}>MRI Regime</span>{_mri_str}</div>'
        + '</div>'
    )


# ── HTML builder ──────────────────────────────────────────────────────────────

def build_html(  # noqa: E501
    monday: date,
    week_eods: dict,
    backtest: dict,
    analysis: "dict | None" = None,
    day_trades: "list | None" = None,
    archive_weeks: "list | None" = None,
    is_archive: bool = False,
    trade_log: "dict | None" = None,
) -> str:
    day_trades = day_trades or []
    stats   = _week_stats(week_eods)
    today   = date.today()
    gen_ts      = datetime.now(PDT).strftime("%b %d · %I:%M %p PT")
    # C-1: read paper profile values, not module-level live defaults
    _paper_profile = getattr(_cfg, "PROFILES", {}).get("paper", {})
    _stop_mult = _paper_profile.get("INTRADAY_STOP_ATR_MULT",
                 getattr(_cfg, "INTRADAY_STOP_ATR_MULT", 1.25))
    _tgt_mult  = _paper_profile.get("INTRADAY_TARGET_ATR_MULT",
                 getattr(_cfg, "INTRADAY_TARGET_ATR_MULT", 2.5))
    wk_lbl  = f"Week of {monday.strftime('%b %d')} – {(monday + timedelta(days=4)).strftime('%b %d, %Y')}"  # noqa: E501

    # ── Week navigation ───────────────────────────────────────────────────────
    all_weeks  = sorted(set((archive_weeks or []) + [monday]))
    idx        = all_weeks.index(monday) if monday in all_weeks else -1
    prev_m     = all_weeks[idx - 1] if idx > 0 else None
    next_m     = all_weeks[idx + 1] if idx >= 0 and idx < len(all_weeks) - 1 else None

    def _href(m: date) -> str:
        """Relative path to the HTML for a given Monday."""
        fn = f"weekly_{m.isoformat()}.html"
        return fn if is_archive else f"logs/{fn}"

    prev_btn = (f'<a href="{_href(prev_m)}" style="font-size:11px;color:#0a84ff;'
                f'text-decoration:none;padding:3px 8px;border:1px solid #0a84ff;border-radius:4px">'  # noqa: E501
                f'← {prev_m.strftime("%b %d")}</a>' if prev_m else
                '<span style="font-size:11px;color:#2a2f48;padding:3px 8px;border:1px solid #2a2f48;'  # noqa: E501
                'border-radius:4px">← Prev</span>')
    next_btn = (f'<a href="{_href(next_m)}" style="font-size:11px;color:#0a84ff;'
                f'text-decoration:none;padding:3px 8px;border:1px solid #0a84ff;border-radius:4px">'  # noqa: E501
                f'{next_m.strftime("%b %d")} →</a>' if next_m else
                '<span style="font-size:11px;color:#2a2f48;padding:3px 8px;border:1px solid #2a2f48;'  # noqa: E501
                'border-radius:4px">Next →</span>')
    # Week jump dropdown (all archive weeks + current)
    options_html = ""
    for w in reversed(all_weeks):
        lbl      = f"Week of {w.strftime('%b %d')} – {(w + timedelta(days=4)).strftime('%b %d')}"  # noqa: E501
        selected = ' selected' if w == monday else ''
        options_html += f'<option value="{_href(w)}"{selected}>{lbl}</option>'
    nav_html = (
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'{prev_btn}'
        f'<select onchange="window.location=this.value" '
        f'style="background:#161920;border:1px solid #2a2f48;color:#e2e4ee;'
        f'font-size:11px;padding:3px 6px;border-radius:4px;cursor:pointer">'
        f'{options_html}</select>'
        f'{next_btn}'
        f'</div>'
    )
    # "Back to current" link when viewing an archive
    back_link = (
        '<div style="margin-top:4px;font-size:10px">'
        '<a href="../weekly_review.html" style="color:#b8bdd4;text-decoration:none">← Current week</a>'  # noqa: E501
        '</div>'
    ) if is_archive else ""

    # ── Per-day tiles ─────────────────────────────────────────────────────────
    day_tiles = ""
    for i, day_name in enumerate(["Monday","Tuesday","Wednesday","Thursday","Friday"]):
        d    = monday + timedelta(days=i)
        eod  = week_eods.get(d.isoformat())
        ds   = d.strftime("%b %d")
        is_today = (d == today)
        if eod is not None:
            pnl   = eod.get("pnl_today", 0)
            cls   = "pos" if pnl > 0 else ("neg" if pnl < 0 else "zero")
            sign  = "+" if pnl >= 0 else "-"
            tc    = eod.get("trades_today", 0)
            wr    = eod.get("win_rate_today", 0)
            driver = _day_driver(eod)
            tile_cls = "day active" if is_today else "day"
            # Loss driver badge — only on red days
            _driver_result = _classify_loss_driver(eod)
            if _driver_result:
                _driver_label, _driver_color = _driver_result
                # M-2: map semantic label to display string
                _driver_is_mech = (_driver_label == "MECHANICAL")
                _display_label  = "⚙ BUG IDENTIFIED" if _driver_is_mech else "✕ STRATEGY"  # noqa: E501
                _driver_bg      = "rgba(255,214,10,.13)" if _driver_is_mech else "rgba(255,59,48,.13)"  # noqa: E501
                loss_driver_badge = (
                    f'<div style="margin-top:6px">'
                    f'<span style="font-size:9px;font-weight:700;letter-spacing:.07em;'
                    f'text-transform:uppercase;color:{_driver_color};'
                    f'background:{_driver_bg};'
                    f'padding:2px 7px;border-radius:3px;border:1px solid {_driver_color}44">'  # noqa: E501
                    f'{_display_label}</span>'
                    f'</div>'
                )
            else:
                loss_driver_badge = ""
            _session_note = eod.get("session_note", "")
            _note_html = (
                f'<div style="margin-top:6px;font-size:9px;color:#8e8e93;'
                f'font-style:italic;line-height:1.35">{_session_note}</div>'
            ) if _session_note else ""
            day_tiles += f"""
<div class="{tile_cls}">
  <div class="day-name">{day_name}</div>
  <div class="day-date">{ds}</div>
  <div class="day-pnl {cls}">{sign}${abs(pnl):.2f}</div>
  <div class="day-driver">{driver}</div>
  <div class="day-meta">{tc} trade{"s" if tc!=1 else ""} · {wr}% win</div>
  {loss_driver_badge}
  {_note_html}
</div>"""
        else:
            future = d > today
            _future_style = ' style="opacity:0.35;pointer-events:none"' if future else ''  # noqa: E501
            day_tiles += f"""
<div class="day no-data"{_future_style}>
  <div class="day-name">{day_name}</div>
  <div class="day-date">{ds}</div>
  <div class="day-pnl zero">—</div>
  <div class="day-driver">{"Upcoming" if future else "No session data"}</div>
</div>"""

    # ── Stats row ─────────────────────────────────────────────────────────────
    def _sv(val, fmt="{}", cls=""):
        if val is None:
            return '<span class="zero">—</span>'
        text = fmt.format(val)
        return f'<span class="{cls}">{text}</span>' if cls else text

    wp  = stats.get("week_pnl", 0)
    wpc = "pos" if wp > 0 else ("neg" if wp < 0 else "zero")
    _tqi = stats.get("avg_tqi")
    _tqi_col = "#30d158" if (_tqi or 0) >= 65 else ("#ffd60a" if (_tqi or 0) >= 40 else "#ff3b30")  # noqa: E501
    _tqi_val = f'{_tqi}' if _tqi is not None else "—"
    _spy_evts = stats.get("spy_events") or []
    _spy_badge = ""
    if _spy_evts:
        _spy_colors = {"EXTREME": "#ff3b30", "BROAD_WAR_GEO": "#ff6b35",
                       "BROAD_FED_POLICY": "#ffd60a", "BROAD_TECHNICAL": "#ffd60a", "SECTOR": "#0a84ff"}  # noqa: E501
        _spy_badge = " ".join(
            f'<span style="display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;'  # noqa: E501
            f'border-radius:3px;background:rgba(255,255,255,.07);color:{_spy_colors.get(e,"#b8bdd4")};'
            f'margin-left:4px">{e}</span>'
            for e in _spy_evts
        )

    # Lifetime stats — Alpaca-sourced via compute_lifetime_stats() which calls
    # Alpaca /v2/account directly (equity - $2,500 initial). EOD file sums are
    # not used for total_pnl — they had a falsy 0.0 bug that understated P&L.
    _lt       = compute_lifetime_stats()
    _lt_total = _lt["total_trades"]
    _lt_wins  = _lt["wins"]
    _lt_wr    = round(_lt["win_rate"]) if _lt_total else None
    _lt_pnl   = _lt["total_pnl"]
    _lt_wr_col = ("#30d158" if (_lt_wr or 0) >= 60 else "#ffd60a" if (_lt_wr or 0) >= 40 else "#ff3b30") if _lt_wr is not None else "#636680"  # noqa: E501
    _lt_pnl_col = "#30d158" if _lt_pnl > 0 else ("#ff3b30" if _lt_pnl < 0 else "#636680")  # noqa: E501
    _lt_wr_str  = f"{_lt_wr}%" if _lt_wr is not None else "—"
    _lt_pnl_str = (("+" if _lt_pnl >= 0 else "") + f"${_lt_pnl:.2f}") if _lt_total else "—"  # noqa: E501

    # H-3: Build week trades early so we can compute weekly (not lifetime) overnight WR
    _all_week_trades = []
    for _wd, _weod in week_eods.items():
        if _weod:
            for _wt in (_weod.get("trades") or []):
                if _wt.get("pnl") is not None:
                    _all_week_trades.append(_wt)

    # Overnight win rate — weekly scope (H-3: was using lifetime _lt_closed)
    _ovnt_wk_trades = [t for t in _all_week_trades if t.get("overnight")]
    _ovnt_wk_wins   = sum(1 for t in _ovnt_wk_trades if (t.get("pnl") or 0) > 0)
    _ovnt_wr        = round(_ovnt_wk_wins / len(_ovnt_wk_trades) * 100) if _ovnt_wk_trades else None  # noqa: E501
    _ovnt_wr_col    = (("#30d158" if (_ovnt_wr or 0) >= 60 else "#ffd60a" if (_ovnt_wr or 0) >= 40 else "#ff3b30")  # noqa: E501
                       if _ovnt_wr is not None else "#636680")
    _ovnt_wr_str    = f"{_ovnt_wr}%" if _ovnt_wr is not None else "—"

    stats_html = f"""
<div class="stats-row" style="grid-template-columns:repeat(8,1fr)">
  <div class="stat"><div class="stat-lbl">Weekly P&amp;L</div>
    <div class="stat-val {wpc}">{"+" if wp>=0 else "-"}${abs(wp):.2f}</div>
    <div style="font-size:9px;color:#4a5070;margin-top:2px">closed only</div></div>
  <div class="stat"><div class="stat-lbl">Weekly Trades</div>
    <div class="stat-val">{stats.get("total","—")}</div></div>
  <div class="stat"><div class="stat-lbl">Weekly Win Rate</div>
    <div class="stat-val">{_sv(stats.get("wr"), "{}%")}</div></div>
  <div class="stat" style="border:1px solid #30d15830;background:#0d1f12">
    <div class="stat-lbl" style="color:#30d158">Lifetime Win Rate</div>
    <div class="stat-val" style="color:{_lt_wr_col}">{_lt_wr_str}</div>
    <div style="font-size:10px;color:#636680;margin-top:4px">{_lt_total} trades</div>
  </div>
  <div class="stat" style="border:1px solid #30d15830;background:#0d1f12">
    <div class="stat-lbl" style="color:#30d158">Lifetime P&amp;L</div>
    <div class="stat-val" style="color:{_lt_pnl_col};font-size:18px">{_lt_pnl_str}</div>
  </div>
  <div class="stat"><div class="stat-lbl">Avg Score</div>
    <div class="stat-val">{_sv(stats.get("avg_score"), "{}/12")}</div></div>
  <div class="stat"><div class="stat-lbl">Avg TQI (Trade Quality Index)</div>
    <div class="stat-val" style="color:{_tqi_col}">{_tqi_val}</div></div>
  <div class="stat"><div class="stat-lbl">Overnights</div>
    <div class="stat-val">{stats.get("overnight","—")}</div>
    <div style="font-size:10px;color:{_ovnt_wr_col};margin-top:4px">{_ovnt_wr_str} WR</div></div>
</div>
{"<div style='font-size:11px;color:#b8bdd4;margin-top:-12px;margin-bottom:16px;padding:0 2px'>SPY events this week: " + _spy_badge + "</div>" if _spy_badge else ""}"""  # noqa: E501

    # ── Exit reasons — visual pill badges ────────────────────────────────────
    total   = stats.get("total", 0)
    exits_html = ""
    if total:
        # Bucket any raw exit_reason string into 5 clean categories
        _EXIT_CATS = [
            ("Trail Stop",   "#ff6b35", lambda r: "trail_stop" in r),
            ("Breakeven",    "#ff9f0a", lambda r: "breakeven" in r),
            ("Hard Stop",    "#ff3b30", lambda r: any(x in r for x in ("hard_stop", "stop")) and "trail" not in r and "breakeven" not in r),  # noqa: E501
            ("Target",       "#30d158", lambda r: "target" in r),
            ("Signal Exit",  "#ffd60a", lambda r: "signal" in r or "opposite" in r),
            ("Other",        "#636680", lambda r: True),   # catch-all
        ]
        def _bucket(raw: str) -> str:
            r = raw.lower()
            for label, _, test in _EXIT_CATS:
                if test(r):
                    return label
            return "Other"
        _exit_reasons = stats.get("exit_reasons") or {}
        # Aggregate raw reasons into buckets
        _buckets: dict[str, int] = {}
        for _raw, _cnt in _exit_reasons.items():
            _b = _bucket(_raw)
            _buckets[_b] = _buckets.get(_b, 0) + int(_cnt)
        # Build pills in canonical order (only show buckets that appear)
        _pills = []
        for _label, _color, _ in _EXIT_CATS:
            _cnt = _buckets.get(_label, 0)
            if _cnt == 0:
                continue
            _pct = round(_cnt / total * 100)
            _pills.append(
                f'<span style="display:inline-flex;align-items:center;gap:5px;'
                f'background:{_color}18;border:1px solid {_color}55;border-radius:6px;'
                f'padding:4px 10px;font-size:11px;font-weight:600;color:{_color}">'
                f'{_label}'
                f'<span style="background:{_color}33;border-radius:4px;padding:1px 6px;'
                f'font-size:10px;font-weight:700;color:{_color}">{_cnt}</span>'
                f'<span style="font-size:9px;color:{_color}99;font-weight:400">{_pct}%</span>'  # noqa: E501
                f'</span>'
            )
        exits_html = (
            '<div style="margin-top:14px">'
            '<div style="font-size:9px;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.1em;color:#636680;margin-bottom:8px">Exit Breakdown</div>'
            '<div style="display:flex;flex-wrap:wrap;gap:8px">'
            + ("".join(_pills) if _pills else '<span style="color:#4a5070;font-size:12px">—</span>')  # noqa: E501
            + '</div></div>'
        )

    # ── Best / Worst performers this week ─────────────────────────────────────
    # _all_week_trades already built above (H-3 hoist)
    performers_html = ""
    if _all_week_trades:
        _sorted_trades = sorted(_all_week_trades, key=lambda t: t.get("pnl", 0))
        _worst_trades  = _sorted_trades[:3]   # up to 3 worst
        _best_trades   = list(reversed(_sorted_trades[-3:]))  # up to 3 best
        def _perf_row(t, is_best):
            _sym  = t.get("symbol", "?")
            _dir  = "LONG" if t.get("direction","") == "long" else "SHORT"
            _p    = t.get("pnl", 0)
            _col  = "#30d158" if _p >= 0 else "#ff3b30"
            _sign = "+" if _p >= 0 else "-"
            _sc   = t.get("score", 0)
            _rsn  = (t.get("exit_reason") or t.get("reason") or "—").split("|")[0].strip().replace("_", " ")  # noqa: E501
            _tqi  = t.get("tqi_score")
            _tqi_col = "#30d158" if (_tqi or 0) >= 65 else ("#ffd60a" if (_tqi or 0) >= 40 else "#ff3b30")  # noqa: E501
            _tqi_str = f'TQI {_tqi}/100' if _tqi is not None else ""
            _spy  = t.get("spy_event_type", "")
            _spy_str = f' · {_spy}' if _spy else ""
            return (
                f'<div style="display:flex;align-items:center;justify-content:space-between;'  # noqa: E501
                f'padding:6px 0;border-bottom:1px solid #161a28">'
                f'<span style="font-weight:700;color:#e2e4ee;font-size:13px;width:60px">{_sym}</span>'  # noqa: E501
                f'<span style="font-size:11px;color:#b8bdd4;width:50px">{_dir}</span>'
                f'<span style="font-size:11px;color:#b8bdd4;width:40px">{_sc}/12</span>'
                f'<span style="font-size:11px;color:{_tqi_col};width:55px">{_tqi_str}</span>'  # noqa: E501
                f'<span style="font-size:11px;color:#b8bdd4;width:140px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis">{_rsn}{_spy_str}</span>'  # noqa: E501
                f'<span style="font-weight:700;color:{_col};font-size:14px">{_sign}${abs(_p):.2f}</span>'  # noqa: E501
                f'</div>'
            )
        _best_rows   = "".join(_perf_row(t, True)  for t in _best_trades  if t.get("pnl", 0) > 0)  # noqa: E501
        _worst_rows  = "".join(_perf_row(t, False) for t in _worst_trades if t.get("pnl", 0) < 0)  # noqa: E501
        _no_best     = '<div style="font-size:12px;color:#4a5070">No winners this week</div>'  # noqa: E501
        _no_worst    = '<div style="font-size:12px;color:#4a5070">No losers this week</div>'  # noqa: E501
        performers_html = (
            '<div style="margin-top:16px;display:grid;grid-template-columns:1fr 1fr;gap:20px">'  # noqa: E501
            '<div>'
            '<div style="font-size:10px;font-weight:700;color:#30d158;text-transform:uppercase;'  # noqa: E501
            'letter-spacing:.1em;margin-bottom:8px">Biggest Winners</div>'
            + (_best_rows if _best_rows else _no_best) +
            '</div>'
            '<div>'
            '<div style="font-size:10px;font-weight:700;color:#ff3b30;text-transform:uppercase;'  # noqa: E501
            'letter-spacing:.1em;margin-bottom:8px">Biggest Losers</div>'
            + (_worst_rows if _worst_rows else _no_worst) +
            '</div>'
            '</div>'
        )

    # Strategy validation MOVED to the monthly page (Rafael 2026-07-05) — the
    # _strategy_validation_html function stays here (monthly imports it); weekly
    # no longer displays it.

    # ── AI sections ──────────────────────────────────────────────────────────
    _det_stats = _exec_summary_stats(trade_log, monday)
    _gemini_txt = analysis.get("exec_summary", "") if analysis else ""
    # ── Headline: week P&L + one-line verdict (mockup redesign 2026-07-05) ─────
    _wp_col = "#30d158" if wp > 0 else ("#ff3b30" if wp < 0 else "#8a94ae")
    _wp_str = ("+" if wp >= 0 else "") + f"${wp:.2f}"
    _verdict = _gemini_txt or "No AI verdict generated for this week."
    headline_html = (
        '<div style="display:flex;gap:16px;align-items:center;margin:14px 0">'
        '<div><div style="font-size:11px;color:#8a94ae">Week P&amp;L</div>'
        f'<div style="font-size:28px;font-weight:700;color:{_wp_col}">{_wp_str}</div>'
        '</div>'
        '<div style="flex:1;font-size:14px;color:#c8cce4;'
        f'border-left:1px solid #252847;padding-left:16px">{_verdict}</div></div>'
    )
    ai_html    = ""
    if analysis:

        # What worked / didn't
        worked  = analysis.get("what_worked", "")
        didnt   = analysis.get("what_didnt_work", "")
        worked_html = f'<div class="ai-section"><div class="ai-hdr">What Worked</div><div class="ai-body">{worked}</div></div>' if worked else ""  # noqa: E501
        didnt_html  = f'<div class="ai-section"><div class="ai-hdr">What Didn\'t Work</div><div class="ai-body">{didnt}</div></div>' if didnt else ""  # noqa: E501

        # Recommendations
        recs = analysis.get("recommendations") or []
        recs_inner = ""
        for rec in recs:
            conf = rec.get("confidence","medium")
            conf_cls = {"high":"conf-high","medium":"conf-med","low":"conf-low"}.get(conf,"conf-med")  # noqa: E501
            recs_inner += (
                f'<div class="rec-item">'
                f'<div class="rec-title">{rec.get("title","")}'
                f' <span class="{conf_cls}" style="font-size:10px;font-weight:600;margin-left:6px">{conf.upper()}</span>'  # noqa: E501
                f'</div>'
                f'<div class="rec-detail">{rec.get("description","")}</div>'
                f'<div class="rec-change">→ {rec.get("proposed_change","")}</div>'
                f'</div>'
            )
        recs_html = (f'<div class="ai-section"><div class="ai-hdr">Recommendations</div>{recs_inner}</div>'  # noqa: E501
                     if recs_inner else "")

        # Board POVs
        roles = {"quant_research":"Quant Research","risk":"Risk","execution":"Execution","product":"Product"}  # noqa: E501
        board_inner = ""
        for key, label in roles.items():
            p = (analysis.get("board_povs") or {}).get(key)
            if not p:
                continue
            board_inner += (
                f'<div class="board-item">'
                f'<div class="board-role">{label}</div>'
                f'<div class="board-pov">{p.get("pov","")}</div>'
                f'<div class="board-next">→ {p.get("next_step","")}</div>'
                f'</div>'
            )
        board_html = (f'<div class="ai-section"><div class="ai-hdr">Advisory Board</div>'  # noqa: E501
                      f'<div class="board-grid">{board_inner}</div></div>'
                      if board_inner else "")

        ai_html = (
            f'<div class="card"><div class="card-body">'
            f'{worked_html}{didnt_html}{recs_html}{board_html}'
            f'</div></div>'
        )

    # ── No-data notice ────────────────────────────────────────────────────────
    has_data = any(v is not None for v in week_eods.values())
    no_data_html = "" if has_data else (
        '<div style="padding:20px;text-align:center;color:#b8bdd4;font-size:13px">'
        'No EOD session files found for this week. '
        'Bot writes <code>eod_YYYY-MM-DD.json</code> to logs/ at end of each session.'
        '</div>'
    )

    patch_html = _build_patch_health_section(monday)

    # ── Collapsible detail sections — preserve every field, collapsed default ──
    def _collapsible(label: str, inner: str) -> str:
        if not inner:
            return ""
        return ('<details style="margin-top:10px;background:#13162a;'
                'border:1px solid #252847;border-radius:8px">'
                '<summary style="padding:12px 16px;font-size:14px;font-weight:600;'
                'color:#c8cce4;cursor:pointer">'
                f'{label}</summary>'
                f'<div style="padding:0 16px 12px">{inner}</div></details>')
    collapsibles_html = (
        '<div style="font-size:12px;color:#8a94ae;margin-top:18px">'
        'Everything below is preserved — collapsed by default, one click to expand:'
        '</div>'
        + _collapsible("Detail stats &amp; exit breakdown", _det_stats + exits_html)
        + _collapsible("AI review &amp; board POV", ai_html)
        + _collapsible("Patch health", patch_html)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>Weekly Review · {wk_lbl}</title>
<style>{CSS}</style>
</head>
<body>

<div class="header">
  <div>
    <div class="title">Weekly Review</div>
    <div class="sub">{wk_lbl}</div>
  </div>
  <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px">
    {nav_html}
    {back_link}
    <div style="font-size:10px;color:#b8bdd4">Last updated {gen_ts} &nbsp;·&nbsp; {LIVE_CLOCK_HTML}</div>
  </div>
</div>

<div class="container">

{no_data_html}
{headline_html}

{stats_html}

<div class="days-grid" style="margin-top:20px">
{day_tiles}
</div>

{performers_html}

{collapsibles_html}

</div>

<div class="footer">
  Scores: 9/12 min · Stop {_stop_mult}×ATR · Target {_tgt_mult}×ATR ·
  Strategy validation sourced from live closed trades · includes all exit types
</div>

</body>
</html>"""  # noqa: E501


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--week",    help="Monday date YYYY-MM-DD (default: current week)")  # noqa: E501
    parser.add_argument("--analyze", action="store_true", help="Run Claude AI analysis")
    args = parser.parse_args()

    if args.week:
        monday = date.fromisoformat(args.week)
    else:
        monday = _default_monday()

    # Load Mon–Fri EOD files
    week_eods = {}
    for i in range(5):
        d = monday + timedelta(days=i)
        week_eods[d.isoformat()] = _load_eod(d)

    loaded = sum(1 for v in week_eods.values() if v is not None)
    print(f"Weekly Review: {monday.isoformat()} — {loaded}/5 days loaded")

    backtest = _load_latest_backtest()
    if backtest:
        meta = backtest.get("backtest_meta", {})
        print(f"  Backtest: {meta.get('run_date','?')} loaded")
    else:
        print("  Backtest: none found in logs/")

    day_trades = _load_day_trades()

    analysis = None
    if args.analyze:
        if loaded == 0:
            print("  No session data — skipping AI analysis")
        else:
            print("  Running AI analysis...")
            stats    = _week_stats(week_eods)
            analysis = _run_analysis(week_eods, stats, backtest)
            if analysis:
                print("  AI analysis complete")

    # Full 12-week navigation list (always populated, no files required)
    import os as _os
    all_nav_weeks = _list_archive_weeks()

    trade_log = _load_trade_log()
    _tl_count = len(trade_log.get("closed", []))
    print(f"  Trade log: {_tl_count} closed trade(s) loaded for strategy validation panel")  # noqa: E501

    # Write root weekly_review.html (current/requested week, paths relative to root)
    html = build_html(monday, week_eods, backtest, analysis, day_trades,
                      archive_weeks=all_nav_weeks, is_archive=False, trade_log=trade_log)  # noqa: E501

    tmp = OUT_HTML + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    _os.replace(tmp, OUT_HTML)
    print(f"  Written → {OUT_HTML}")

    # Write/update archive for the current week
    arch_path = _os.path.join(LOGS_DIR, f"weekly_{monday.isoformat()}.html")
    html_arch = build_html(monday, week_eods, backtest, analysis, day_trades,
                           archive_weeks=all_nav_weeks, is_archive=True, trade_log=trade_log)  # noqa: E501
    tmp_arch  = arch_path + ".tmp"
    with open(tmp_arch, "w", encoding="utf-8") as f:
        f.write(html_arch)
    _os.replace(tmp_arch, arch_path)
    print(f"  Archive → {arch_path}")

    # Rebuild ALL archive stubs every run so prev/next navigation is always
    # current. Skipping existing files caused stale nav — archives written
    # before newer weeks existed kept next_m=None and rendered "Next" grayed
    # out permanently even after new weeks were added to all_nav_weeks.
    # Skip only the current week's archive (already written above at arch_path).
    stubs_written = 0
    for w in all_nav_weeks:
        w_path = _os.path.join(LOGS_DIR, f"weekly_{w.isoformat()}.html")
        if w == monday:
            continue  # current week's archive already written above
        # Load whatever EOD data exists for that week (may be empty)
        w_eods = {}
        for i in range(5):
            wd = w + timedelta(days=i)
            w_eods[wd.isoformat()] = _load_eod(wd)
        stub_html = build_html(w, w_eods, backtest, None, day_trades,
                               archive_weeks=all_nav_weeks, is_archive=True)
        tmp_stub  = w_path + ".tmp"
        with open(tmp_stub, "w", encoding="utf-8") as f:
            f.write(stub_html)
        _os.replace(tmp_stub, w_path)
        stubs_written += 1
    print(f"  Refreshed {stubs_written} archive(s) with current navigation")
    _r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "monthly_review.py"), "--all-months"],
        check=False, capture_output=True, text=True,
    )
    if _r.returncode != 0:
        print(f"  WARN: monthly_review.py exited {_r.returncode}: {_r.stderr[:200]}", file=sys.stderr)  # noqa: E501
    else:
        print("  Monthly review regenerated")


if __name__ == "__main__":
    main()
