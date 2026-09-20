"""Run a source-bound Core MTF simulation without claiming executable fills.

This research-only wrapper validates snapshot integrity, requires an explicit
simulation horizon, and quarantines overlapping same-symbol parent candidates.
It delegates only lifecycle assumptions to the completed-bar kernel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from research.core_mtf_point_in_time_replay import (
    OHLCBar,
    ShortCandidate,
    evaluate_short,
)
from research.core_mtf_replay_events import extract_core_mtf_short_entries


def run_simulation(
    ledger_path: Path,
    bar_snapshot_path: Path,
    *,
    max_hold: timedelta,
    round_trip_cost_bps: float,
) -> dict[str, Any]:
    """Return source-bound simulation results; never broker performance claims."""
    if max_hold <= timedelta(0):
        raise ValueError("max_hold must be positive")
    if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be finite and non-negative")
    snapshot_raw = bar_snapshot_path.read_bytes()
    snapshot = json.loads(snapshot_raw)
    bars_by_symbol = _validated_bars(snapshot)
    extracted = extract_core_mtf_short_entries(ledger_path)
    candidates = extracted.events
    results: list[dict[str, Any]] = []
    for index, event in enumerate(candidates):
        if _has_same_symbol_overlap(candidates, index, max_hold):
            results.append(
                {
                    "source_line": event.source_line,
                    "symbol": event.symbol,
                    "status": "unevaluable",
                    "reason": "same_symbol_parent_within_simulation_horizon",
                }
            )
            continue
        if event.symbol not in bars_by_symbol:
            results.append(
                {
                    "source_line": event.source_line,
                    "symbol": event.symbol,
                    "status": "unevaluable",
                    "reason": "missing_symbol_bars",
                }
            )
            continue
        bars = [_ohlc_bar(row) for row in bars_by_symbol[event.symbol]]
        outcome = evaluate_short(
            ShortCandidate(
                decision_time=event.decision_time,
                symbol=event.symbol,
                stop=event.stop,
                target=event.target,
                max_hold=max_hold,
                source=str(event.source_line),
            ),
            bars,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        outcome["source_line"] = event.source_line
        results.append(outcome)
    return {
        "schema_v": 1,
        "kind": "core_mtf_simulated_ohlcv_replay",
        "execution_claims_permitted": False,
        "execution_evidence": snapshot["execution_evidence"],
        "method": {
            "entry": "next_bar_open_simulation",
            "intrabar": "adverse_stop_first",
            "cost": "fixed_round_trip_bps",
            "max_hold_seconds": max_hold.total_seconds(),
            "round_trip_cost_bps": round_trip_cost_bps,
        },
        "ledger_sha256": hashlib.sha256(ledger_path.read_bytes()).hexdigest(),
        "bar_snapshot_sha256": hashlib.sha256(snapshot_raw).hexdigest(),
        "bar_data_sha256": snapshot["bar_data_sha256"],
        "results": results,
    }


def _validated_bars(snapshot: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(snapshot, dict):
        raise TypeError("bar snapshot must be an object")
    if snapshot.get("kind") != "alpaca_stock_bars_snapshot":
        raise ValueError("unexpected bar snapshot kind")
    if snapshot.get("market_data_kind") != "aggregated_ohlcv_bars":
        raise ValueError("bar snapshot is not aggregated OHLCV")
    if snapshot.get("fill_claims_permitted") is not False:
        raise ValueError("bar snapshot permits unsupported fill claims")
    if snapshot.get("execution_evidence") != "not_bid_ask_or_quote_data":
        raise ValueError("bar snapshot lacks the required execution limitation")
    bars = snapshot.get("bars")
    digest = snapshot.get("bar_data_sha256")
    if not isinstance(bars, dict) or not isinstance(digest, str):
        raise ValueError("bar snapshot lacks bars or hash")
    raw = json.dumps(bars, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("bar snapshot hash mismatch")
    return bars


def _has_same_symbol_overlap(
    candidates: tuple, index: int, max_hold: timedelta
) -> bool:
    candidate = candidates[index]
    for other_index, other in enumerate(candidates):
        if other_index == index or other.symbol != candidate.symbol:
            continue
        earlier, later = sorted((candidate.decision_time, other.decision_time))
        if later < earlier + max_hold:
            return True
    return False


def _ohlc_bar(row: Any) -> OHLCBar:
    if not isinstance(row, dict):
        raise ValueError("bar row must be an object")
    try:
        start = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
        return OHLCBar(
            start=start.astimezone(timezone.utc),
            open=float(row["o"]),
            high=float(row["h"]),
            low=float(row["l"]),
            close=float(row["c"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid OHLCV bar") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--bar-snapshot", required=True, type=Path)
    parser.add_argument("--max-hold-hours", required=True, type=float)
    parser.add_argument("--round-trip-cost-bps", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not math.isfinite(args.round_trip_cost_bps):
        raise ValueError("round_trip_cost_bps must be finite")
    artifact = run_simulation(
        args.ledger,
        args.bar_snapshot,
        max_hold=timedelta(hours=args.max_hold_hours),
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(f"wrote {args.output}: {len(artifact['results'])} results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
