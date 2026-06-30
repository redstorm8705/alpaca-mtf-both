#!/usr/bin/env python3
"""
compare_logs.py — Stage A baseline capture + Phase 1 decomp regression checker.

Usage:
    # Capture one trading day's metrics to golden snapshot:
    python3 compare_logs.py capture --date 2026-05-12

    # Compare two captured snapshots (exit 0=PASS, 1=FAIL):
    python3 compare_logs.py compare logs/golden_logs/metrics_2026-05-12.json \\
                                     logs/golden_logs/metrics_2026-05-19.json

Metrics captured (per trading day):
    From mtf_bot.log*  : cycles, gate_pass_rate, gate_block_pdt_rate, entry_block_rate
    From trade_events.jsonl : event counts by type, realized_pnl (informational)

Comparison: all rates normalized per-cycle; tolerance ±1%.
P&L is informational only — not gated (market conditions differ across days).
"""

import sys
import json
import re
import argparse
import tempfile
from pathlib import Path

# AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This script
# is read-only (mtf_bot.log*, trade_events.jsonl) and only writes its own
# dedicated golden_logs/metrics_*.json snapshot file -- no write-contention
# risk with the live bot's shared state.

# ─── Paths (anchored to script location — never CWD-relative) ────────────────
_BASE   = Path(__file__).resolve().parent
_LOGS   = _BASE / "logs"
_GOLDEN = _LOGS / "golden_logs"
_MTF_LOGS = [
    _LOGS / "mtf_bot.log",
    _LOGS / "mtf_bot.log.1",
    _LOGS / "mtf_bot.log.2",
    _LOGS / "mtf_bot.log.3",
    _LOGS / "mtf_bot.log.4",
    _LOGS / "mtf_bot.log.5",
]
_EVENTS = _LOGS / "trade_events.jsonl"

TOLERANCE = 0.01  # ±1% per-cycle rate

# ─── Log patterns ─────────────────────────────────────────────────────────────
_CYCLE_PAT       = re.compile(r"─── MTF cycle @")
_GATE_PASS_PAT   = re.compile(r"gate PASS")
_GATE_BLOCK_PDT  = re.compile(r"PDT=3/3 gate BLOCKED")
_ENTRY_BLOCK_PAT = re.compile(r"ENTRY BLOCKED")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _collect_log_lines(date_str: str) -> list[str]:
    """Return all mtf_bot.log* lines whose timestamp prefix matches date_str.

    Deduplicates across rotated log files via a seen-set so that a line
    present in both mtf_bot.log and mtf_bot.log.1 (e.g. during active
    rotation) is counted exactly once.
    """
    lines: list[str] = []
    seen: set[str] = set()
    prefix = date_str + " "
    for log_path in _MTF_LOGS:
        if not log_path.exists():
            continue
        try:
            for line in log_path.read_text(errors="replace").splitlines():
                if line.startswith(prefix) and line not in seen:
                    seen.add(line)
                    lines.append(line)
        except OSError as exc:
            print(f"WARN: could not read {log_path}: {exc}")
    return lines


def capture(date_str: str) -> dict:
    """Extract and return metrics dict for date_str (YYYY-MM-DD, PT calendar date)."""
    log_lines = _collect_log_lines(date_str)
    if not log_lines:
        print(f"INFO: no log lines found for {date_str} — returning empty snapshot.")
        return {"date": date_str, "cycles": 0, "skip": True}

    cycles         = sum(1 for ln in log_lines if _CYCLE_PAT.search(ln))
    gate_pass      = sum(1 for ln in log_lines if _GATE_PASS_PAT.search(ln))
    gate_block_pdt = sum(1 for ln in log_lines if _GATE_BLOCK_PDT.search(ln))
    entry_block    = sum(1 for ln in log_lines if _ENTRY_BLOCK_PAT.search(ln))

    # trade_events.jsonl — filtered by PT date prefix in ts field
    event_counts: dict[str, int] = {}
    realized_pnl = 0.0
    if _EVENTS.exists():
        for raw in _EVENTS.read_text(errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # ts is ISO-8601 with PT offset (e.g. "2026-05-11T07:13:30...-07:00")
            if not ev.get("ts", "").startswith(date_str):
                continue
            evt = ev.get("event", "unknown")
            event_counts[evt] = event_counts.get(evt, 0) + 1
            if evt in ("exit", "partial_exit"):
                realized_pnl += float(ev.get("pnl", 0.0))

    if cycles == 0:
        print(f"WARN: {date_str} has log lines but zero MTF cycles — marked skip.")
        return {"date": date_str, "cycles": 0, "skip": True}

    return {
        "date":                date_str,
        "cycles":              cycles,
        "skip":                False,
        "gate_pass_rate":      round(gate_pass      / cycles, 6),
        "gate_block_pdt_rate": round(gate_block_pdt / cycles, 6),
        "entry_block_rate":    round(entry_block     / cycles, 6),
        "event_counts":        event_counts,
        "realized_pnl":        round(realized_pnl, 4),
    }


def save_snapshot(metrics: dict) -> Path:
    """Atomically write metrics JSON to logs/golden_logs/metrics_YYYY-MM-DD.json."""
    _GOLDEN.mkdir(parents=True, exist_ok=True)
    out = _GOLDEN / f"metrics_{metrics['date']}.json"
    tmp_fd, tmp_path = tempfile.mkstemp(dir=_GOLDEN, prefix=".tmp_metrics_")
    try:
        with open(tmp_fd, "w") as fh:
            json.dump(metrics, fh, indent=2)
        Path(tmp_path).replace(out)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    return out


def _rate_diff(label: str, a: float, b: float) -> tuple[bool, str]:
    """Compare two per-cycle rates. Returns (passed, human-readable message)."""
    if a == 0.0 and b == 0.0:
        return True, f"  {label}: both=0.000000 — SKIP (zero baseline)"
    if a == 0.0:
        return False, (
            f"  {label}: baseline=0.000000 candidate={b:.6f} — "
            f"FAIL (baseline zero, candidate non-zero)"
        )
    delta = abs(b - a) / a
    status = "PASS" if delta <= TOLERANCE else "FAIL"
    return (
        delta <= TOLERANCE,
        f"  {label}: {a:.6f} → {b:.6f} (Δ={delta * 100:.2f}%) — {status}",
    )


def compare(path_a: str, path_b: str) -> bool:
    """Compare two snapshot files. Returns True=PASS, False=FAIL."""
    a = json.loads(Path(path_a).read_text())
    b = json.loads(Path(path_b).read_text())

    print("\n=== compare_logs.py DIFF ===")
    print(f"  Baseline : {a['date']} ({a.get('cycles', 0)} cycles)  [{path_a}]")
    print(f"  Candidate: {b['date']} ({b.get('cycles', 0)} cycles)  [{path_b}]")
    print(f"  Tolerance: ±{TOLERANCE * 100:.0f}%\n")

    if a.get("skip") or b.get("skip"):
        print("  One or both snapshots marked skip (zero cycles) — cannot compare.")
        return False

    all_pass = True

    # ── Rate checks (log-derived, normalized per cycle) ────────────────────────
    rate_keys = ["gate_pass_rate", "gate_block_pdt_rate", "entry_block_rate"]
    for key in rate_keys:
        passed, msg = _rate_diff(key, a.get(key, 0.0), b.get(key, 0.0))
        print(msg)
        all_pass = all_pass and passed

    # ── Event count checks (trade_events.jsonl, normalized per cycle) ──────────
    a_events = a.get("event_counts", {})
    b_events = b.get("event_counts", {})
    for evt in sorted(set(a_events) | set(b_events)):
        va = a_events.get(evt, 0) / a["cycles"]
        vb = b_events.get(evt, 0) / b["cycles"]
        passed, msg = _rate_diff(f"event:{evt}", va, vb)
        print(msg)
        all_pass = all_pass and passed

    # ── P&L informational (not gated — market conditions differ) ──────────────
    pnl_a = a.get("realized_pnl", 0.0)
    pnl_b = b.get("realized_pnl", 0.0)
    print(
        f"\n  realized_pnl: baseline=${pnl_a:.2f}  candidate=${pnl_b:.2f}"
        "  (informational — not gated)"
    )

    verdict = "PASS" if all_pass else "FAIL"
    print(f"\n  VERDICT: {verdict}\n")
    return all_pass


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage A baseline capture + decomp regression checker"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    cap = sub.add_parser("capture", help="Capture one day's metrics to golden_logs/")
    cap.add_argument("--date", required=True, help="Trading date YYYY-MM-DD (PT)")

    cmp = sub.add_parser(
        "compare", help="Compare two snapshot files (exit 0=PASS, 1=FAIL)"
    )
    cmp.add_argument("baseline",  help="Path to baseline metrics JSON")
    cmp.add_argument("candidate", help="Path to candidate metrics JSON")

    args = parser.parse_args()

    if args.cmd == "capture":
        metrics = capture(args.date)
        if not metrics.get("skip"):
            out = save_snapshot(metrics)
            print(f"Captured {args.date}: {metrics['cycles']} cycles → {out}")
            summary = {k: v for k, v in metrics.items() if k != "event_counts"}
            print(json.dumps(summary, indent=2))
        else:
            print(f"Skip: no usable data for {args.date}")

    elif args.cmd == "compare":
        passed = compare(args.baseline, args.candidate)
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
