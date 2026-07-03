#!/usr/bin/env python3
"""
scripts/gex_daily_audit.py
Daily GEX impact audit — Rafael mandate 2026-07-03 (compensating control for
activating GEX_ENABLED ahead of the original 30-session shadow review).

Answers, every trading day: what did the GEX gauge read, and how did it change
the bot's decisions?

Data sources (local files only — no API calls, no execution imports):
  logs/gex_history.jsonl  — 15-min GEX snapshots (writer process)
  logs/mtf_bot.log        — "GEX Layer8" reads, dynamic-floor raises ("GEX="),
                            "Kelly [" GEX edge-mult applications
  logs/trade_events.jsonl — today's entries (sizes affected by the Kelly mult)

Outputs:
  logs/gex_daily_audit_YYYY-MM-DD.json  (atomic)
  Slack summary via alerts.send_slack (best-effort — audit file is authoritative)

Cadence: OCI cron weekdays 4:30 PM ET. Idempotent — overwrites today's file.
Failure mode: stderr + exit 1; zero impact on the trading process.
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _PROJECT_ROOT / "logs"

sys.path.insert(0, str(_PROJECT_ROOT))


def _log(msg: str) -> None:
    print(f"[gex_daily_audit] {msg}", file=sys.stderr, flush=True)


def gex_timeline(today_pt: str) -> dict:
    """Per-symbol label timeline + distribution from today's history rows."""
    hist = _LOGS / "gex_history.jsonl"
    timeline: list = []
    labels: dict = {"SPY": Counter(), "QQQ": Counter()}
    if hist.exists():
        with open(hist) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = r.get("ts", "")
                if not ts.startswith(today_pt):
                    continue
                row = {"ts": ts}
                for sym, v in (r.get("symbols") or {}).items():
                    lbl = v.get("label", v.get("error", "?"))
                    row[sym] = {
                        "label": lbl,
                        "raw_gex_m": v.get("raw_gex_m"),
                        "flip": v.get("flip_strike"),
                        "valid": v.get("contract_count"),
                    }
                    if sym in labels:
                        labels[sym][lbl] += 1
                timeline.append(row)
    return {
        "snapshots_today": len(timeline),
        "label_distribution": {k: dict(v) for k, v in labels.items()},
        "timeline": timeline,
    }


def log_impacts(today_pt: str) -> dict:
    """Scan mtf_bot.log for today's GEX decision-impact lines."""
    layer8_reads: list = []
    floor_raises: list = []
    kelly_mults: list = []
    log_path = _LOGS / "mtf_bot.log"
    if log_path.exists():
        with open(log_path, errors="replace") as f:
            for line in f:
                if not line.startswith(today_pt):
                    continue
                if "GEX Layer8:" in line:
                    layer8_reads.append(line.strip()[-160:])
                elif "Dynamic MIN_SCORE" in line and "GEX=" in line:
                    floor_raises.append(line.strip()[-220:])
                elif "Kelly [" in line and "edge_mult" in line:
                    kelly_mults.append(line.strip()[-160:])
    # GEX-attributable floor raises: reason string shows GEX=<label>/<score>;
    # the raise is GEX-attributable when the GEX component >= the effective floor.
    return {
        "layer8_reads": len(layer8_reads),
        "layer8_last": layer8_reads[-3:],
        "floor_raise_lines_mentioning_gex": len(floor_raises),
        "floor_raise_samples": floor_raises[-3:],
        "kelly_gex_mult_applications": len(kelly_mults),
        "kelly_gex_mult_samples": kelly_mults[-5:],
    }


def entries_today(today_pt: str) -> list:
    """Today's entry events (context for which sizes the Kelly mult touched)."""
    out: list = []
    te = _LOGS / "trade_events.jsonl"
    if te.exists():
        with open(te) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _is_entry = e.get("event") == "entry"
                if _is_entry and str(e.get("ts", "")).startswith(today_pt):
                    out.append({
                        "ts": e.get("ts"), "symbol": e.get("symbol"),
                        "direction": e.get("direction"), "size": e.get("size"),
                        "price": e.get("price"), "score": e.get("score"),
                    })
    return out


def main() -> int:
    try:
        today_pt = datetime.now(PT).strftime("%Y-%m-%d")
        report: dict = {
            "date": today_pt,
            "generated": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
            "gex": gex_timeline(today_pt),
            "decision_impacts": log_impacts(today_pt),
            "entries_today": entries_today(today_pt),
            "note": (
                "GEX_ENABLED=True (Rafael 2026-07-03). NEGATIVE SPY GEX raises "
                "MIN_SCORE +1 (Layer 8); Kelly risk x1.30 on NEGATIVE / x1.15 on "
                "POSITIVE for warmed-up signal types (no mult Fridays)."
            ),
        }
        out_path = _LOGS / f"gex_daily_audit_{today_pt}.json"
        tmp = out_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(report, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out_path)
        _log(f"report written → {out_path}")

        # Slack summary — best-effort
        try:
            from alerts import send_slack
            g = report["gex"]["label_distribution"]
            d = report["decision_impacts"]
            _spy_items = (g.get("SPY") or {}).items()
            spy = ", ".join(f"{k} {v}" for k, v in _spy_items) or "no data"
            send_slack(
                f":bar_chart: *GEX daily audit {today_pt}* | "
                f"SPY labels: {spy} | snapshots: {report['gex']['snapshots_today']} | "
                f"Layer8 reads: {d['layer8_reads']} | "
                f"floor-raise lines w/ GEX: {d['floor_raise_lines_mentioning_gex']} | "
                f"Kelly GEX mults applied: {d['kelly_gex_mult_applications']} | "
                f"entries today: {len(report['entries_today'])}"
            )
        except Exception as _se:
            _log(f"Slack summary failed (non-fatal): {_se}")
        return 0
    except Exception as e:
        _log(f"FAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
