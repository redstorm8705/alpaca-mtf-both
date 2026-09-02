# ruff: noqa: E501
"""
run_day_tier_shadow.py — Day-Tier SHADOW LOGGER (READ-ONLY, INERT, no orders).

The front-loaded-validation instrument the day-tier design requires BEFORE the tier is wired to
trade (design_records/day_tier_v2_design_2026-08-29.md §B "rigorous SIMULATION + written HYPOTHESIS
before live", §4 Decision-Explainability, §5.5 tracking, §7 "mover-screen LOGGING now"). It runs the
complete READ-ONLY decision→size pipeline over the day-tier universe and APPENDS each symbol's full
decision-stack to logs/day_tier_shadow.jsonl — so the PROV-tagged thresholds across all six pipeline
modules can be DERIVED from real accumulated outcomes (the "prune-if-no-edge" validation), and so the
day-tier's would-be behavior is fully reverse-engineerable before a single order is ever placed.

INERT: it PLACES NO ORDER. It fetches read-only data (bars, GEX snapshot, equity), calls the six
pipeline compute functions, and writes a log line. It is invoked by its own cron (like the audit
scripts) — it does NOT touch run_cycle or any hotspot. NOTHING it writes drives a trade.

Pipeline per symbol (each module fetches its own T1 data; this only orchestrates + logs):
  compute_day_tier_decision(symbol)                     -> side × whether-to-act × conviction
  compute_entry_trigger(symbol, decision)               -> ENTER/WAIT price-action signal + entry_ref
  compute_day_tier_size(symbol, decision, entry_ref, eq)-> would-be share count (on ENTER)

FAIL-SAFE: one symbol's failure never aborts the sweep; the whole run never raises fatally (main
returns an exit code). All output to logs/ (RC-2 anchored). Timestamps in PT (project rule).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Self-load .env so this runs with T1 auth under cron (matches the standalone-cron pattern:
# nightly_audit / midday_audit / run_ftd all call load_dotenv). GUARDED because dotenv is absent
# in some environments (e.g. the unit-test host); there the caller/env supplies credentials.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PT = ZoneInfo("America/Los_Angeles")
_PROJECT_ROOT = Path(__file__).resolve().parent          # RC-2: file lives at repo root
_SHADOW_LOG = _PROJECT_ROOT / "logs" / "day_tier_shadow.jsonl"

logger = logging.getLogger("day_tier_shadow")


def _fmt_pt(dt: "datetime | None" = None) -> str:
    return (dt or datetime.now(PT)).astimezone(PT).strftime("%Y-%m-%d %I:%M:%S %p PT")


def _universe() -> list:
    """The day-tier Track-A universe (Mag-7 underlyings), from the GEX universe. Never raises."""
    try:
        from data.gex import _DAYTRADE_UNDERLYINGS
        return list(_DAYTRADE_UNDERLYINGS)
    except Exception as e:  # noqa: BLE001
        logger.warning("shadow: universe import failed (%s) — empty run", e)
        return []


def _equity() -> "float | None":
    """Live account equity (read-only), or None on failure (sizing then logs 0 — never blocks)."""
    try:
        from execution.broker import get_portfolio_value
        eq = float(get_portfolio_value())
        return eq if eq > 0 else None
    except Exception as e:  # noqa: BLE001
        logger.warning("shadow: equity fetch failed (%s) — sizing will log 0", e)
        return None


def shadow_scan_symbol(symbol: str, equity: "float | None") -> dict:
    """Run the read-only pipeline for one symbol and return its decision-stack record. Never raises;
    a failure yields a record with error set (so the log always accounts for every symbol)."""
    rec: dict = {"symbol": symbol, "ts": _fmt_pt(), "error": None,
                 "decision": None, "trigger": None, "size": None}
    try:
        from strategy.day_tier_decision import compute_day_tier_decision
        from strategy.day_tier_entry_trigger import compute_entry_trigger
        from strategy.day_tier_sizing import compute_day_tier_size

        decision = compute_day_tier_decision(symbol)
        rec["decision"] = decision
        trigger = compute_entry_trigger(symbol, decision)
        rec["trigger"] = trigger

        # Would-be size only on a live ENTER with a usable entry price + known equity.
        if trigger.get("trigger") == "ENTER" and trigger.get("entry_ref") and equity:
            rec["size"] = compute_day_tier_size(symbol, decision, trigger["entry_ref"], equity, track="A")
        return rec
    except Exception as e:  # noqa: BLE001 — one symbol must never abort the sweep
        rec["error"] = repr(e)
        logger.warning("[%s] shadow scan failed (non-fatal): %s", symbol, e)
        return rec


def _append_log(records: list) -> bool:
    """Append the run's records to the shadow JSONL (one line per record). Best-effort; never raises."""
    try:
        _SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_SHADOW_LOG, "a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("shadow: log append failed (%s) — this run's records are lost", e)
        return False


def run_day_tier_shadow() -> dict:
    """Run the shadow sweep over the universe, log every symbol's decision-stack, return a summary.
    READ-ONLY + INERT — places no order. Never raises."""
    syms = _universe()
    equity = _equity()
    records = [shadow_scan_symbol(s, equity) for s in syms]
    wrote = _append_log(records)
    n_consider = sum(1 for r in records if (r.get("decision") or {}).get("would_consider"))
    n_enter = sum(1 for r in records if (r.get("trigger") or {}).get("trigger") == "ENTER")
    n_err = sum(1 for r in records if r.get("error"))
    summary = {"ts": _fmt_pt(), "symbols": len(syms), "would_consider": n_consider,
               "enter_signals": n_enter, "errors": n_err, "logged": wrote,
               "equity": round(equity, 2) if equity else None}
    logger.info("DAY-TIER SHADOW (INERT): %d symbols | would_consider %d | ENTER %d | err %d | logged=%s | @ %s",
                len(syms), n_consider, n_enter, n_err, wrote, summary["ts"])
    return summary


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    try:
        s = run_day_tier_shadow()
        print(json.dumps(s, indent=2))
        return 0
    except Exception as e:  # noqa: BLE001 — a runner must exit cleanly, never traceback
        logger.error("shadow run failed fatally (unexpected): %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
