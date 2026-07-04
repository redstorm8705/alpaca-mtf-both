#!/usr/bin/env python3
"""
scripts/score16_aggregator.py
Shadow-scoring evidence archiver + aggregate report — 16pt validation pipeline.

Purpose: the per-cycle score_comparison_YYYY-MM-DD.json diagnostics are pruned
after 14 days (signal_generator.py), but the 16pt/TSMOM validation gates need
60-90 days of longitudinal evidence. This script archives each day's final
comparison snapshot into an append-only history BEFORE the prune can delete it,
and rebuilds a rolling aggregate report joining shadow scores to reconciled
trade outcomes.

Data sources (all local files — no API calls, no execution imports):
  logs/score_comparison_*.json — per-cycle 12pt vs 16pt comparison (run_scan)
  logs/eod_*.json              — reconciled closed trades (write_eod_summary)

Outputs (logs/ only, atomic writes, PT timestamps):
  logs/score16_history.jsonl     — one compact record per ticker per day (append-only)
  logs/score16_report.json       — rolling aggregate (overwritten atomically)

Cadence: OCI cron, weekdays 4:20 PM ET (after EOD writes). Idempotent — safe to
re-run; history append is deduped by (date, symbol).

Failure mode: log to stderr + exit 1. Zero impact on the trading process.
"""

import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent   # scripts/ -> project root
_LOGS = _PROJECT_ROOT / "logs"
_HISTORY = _LOGS / "score16_history.jsonl"
_REPORT = _LOGS / "score16_report.json"


def _log(msg: str) -> None:
    print(f"[score16_aggregator] {msg}", file=sys.stderr, flush=True)


def _load_history_keys() -> set:
    """(date, symbol) pairs already archived — dedup for idempotent re-runs."""
    keys = set()
    if _HISTORY.exists():
        with open(_HISTORY) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    keys.add((r.get("date"), r.get("symbol")))
                except json.JSONDecodeError:
                    continue   # tolerate a torn tail line from a prior crash
    return keys


def archive_comparisons() -> int:
    """Append each day's final per-ticker comparison rows to the history file."""
    seen = _load_history_keys()
    added = 0
    with open(_HISTORY, "a") as out:
        for fp in sorted(glob.glob(str(_LOGS / "score_comparison_*.json"))):
            try:
                d = json.load(open(fp))
            except (json.JSONDecodeError, OSError) as e:
                _log(f"skip unreadable {fp}: {e}")
                continue
            day = d.get("date")
            for t in d.get("tickers", []):
                sym = t.get("symbol")
                if (day, sym) in seen:
                    continue
                rec = {
                    "date": day,
                    "scan_time": d.get("scan_time"),
                    "symbol": sym,
                    "long_12pt": t.get("long_12pt"),
                    "short_12pt": t.get("short_12pt"),
                    "long_16pt": t.get("long_16pt"),
                    "short_16pt": t.get("short_16pt"),
                    "long_signal_12": t.get("long_signal_12"),
                    "short_signal_12": t.get("short_signal_12"),
                    "long_signal_16": t.get("long_signal_16"),
                    "short_signal_16": t.get("short_signal_16"),
                    "direction_agreement": t.get("direction_agreement"),
                    "would_differ": t.get("16pt_would_differ"),
                    "mom_rank": t.get("mom_rank"),
                    # Per-factor breakdown for the walk-forward/IC engine
                    # (2026-07-03). .get() -> None on pre-change rows (older
                    # score_comparison files lack these keys) — the IC engine
                    # skips null-component rows for factor analysis.
                    "long_16_components": t.get("long_16_components"),
                    "short_16_components": t.get("short_16_components"),
                }
                out.write(json.dumps(rec) + "\n")
                seen.add((day, sym))
                added += 1
        out.flush()
        os.fsync(out.fileno())
    return added


def build_report() -> dict:
    """Rolling aggregate: shadow agreement stats + outcomes by 16pt bucket."""
    # --- shadow comparison stats from history ---
    days = set()
    n_rows = 0
    agree = 0
    differ = 0
    if _HISTORY.exists():
        with open(_HISTORY) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                n_rows += 1
                days.add(r.get("date"))
                if r.get("direction_agreement"):
                    agree += 1
                if r.get("would_differ"):
                    differ += 1

    # --- trade outcomes joined by recorded score_16pt (eod trades) ---
    seen_trades = set()
    by16: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    by12: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for fp in sorted(glob.glob(str(_LOGS / "eod_*.json"))):
        try:
            d = json.load(open(fp))
        except (json.JSONDecodeError, OSError):
            continue
        for t in d.get("trades", []):
            key = (t.get("symbol"), t.get("entry_time"), t.get("exit_time"))
            if key in seen_trades:
                continue
            seen_trades.add(key)
            pnl = t.get("pnl")
            if pnl is None or t.get("_fill_unverified"):
                continue
            b16 = by16[str(t.get("score_16pt"))]
            b16["n"] += 1
            b16["wins"] += 1 if float(pnl) > 0 else 0
            b16["pnl"] = round(b16["pnl"] + float(pnl), 2)
            b12 = by12[str(t.get("score"))]
            b12["n"] += 1
            b12["wins"] += 1 if float(pnl) > 0 else 0
            b12["pnl"] = round(b12["pnl"] + float(pnl), 2)

    return {
        "generated": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
        "shadow_days_archived": len(days),
        "shadow_rows": n_rows,
        "direction_agreement_rate": round(agree / n_rows, 4) if n_rows else None,
        "would_differ_rate": round(differ / n_rows, 4) if n_rows else None,
        "trades_by_score_16pt": dict(sorted(by16.items())),
        "trades_by_score_12pt": dict(sorted(by12.items())),
        "gate_note": "16pt/TSMOM validation gates need 60-90 days; "
                     "shadow_days_archived is the authoritative clock.",
    }


def main() -> int:
    try:
        added = archive_comparisons()
        report = build_report()
        tmp = _REPORT.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(report, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _REPORT)
        _log(f"archived {added} new rows | days={report['shadow_days_archived']} "
             f"rows={report['shadow_rows']} | report written")
        return 0
    except Exception as e:
        _log(f"FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
