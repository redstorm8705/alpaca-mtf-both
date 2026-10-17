#!/usr/bin/env python3
# ruff: noqa: E402
"""Front-loaded replay of the F6 empirical starter on completed daily bars.

This is a research-only reader.  It sends no orders and writes only the requested
JSON artifact.  Every decision is recomputed from bars strictly before that date.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from data.fetcher import fetch_bars
from execution.f6_empirical_trigger import evaluate_empirical_starter


def _daily(symbol: str, count: int) -> pd.DataFrame:
    frame = fetch_bars(symbol, config.TF_DAILY, num_bars=count)
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy().sort_index()
    out.index = out.index.tz_convert("America/New_York")
    return out


def replay(*, bars_requested: int, initial_cash: float) -> dict[str, Any]:
    if bars_requested <= 0 or not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("bars_requested and initial_cash must be positive")
    spy = _daily("SPY", bars_requested)
    if spy.empty:
        raise RuntimeError("SPY daily history unavailable")
    prices = {
        symbol: _daily(symbol, bars_requested)
        for symbol in config.FOREVER6_UNIVERSE
    }
    if any(frame.empty for frame in prices.values()):
        missing = sorted(symbol for symbol, frame in prices.items() if frame.empty)
        raise RuntimeError(f"universe history unavailable: {missing}")

    cash = float(initial_cash)
    held: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    fired: list[dict[str, Any]] = []
    evaluated = 0
    eligible = 0
    runtime_bars = config.BARS_TO_FETCH[config.TF_DAILY]
    for position in range(1, len(spy)):
        start = max(0, position + 1 - runtime_bars)
        window = spy.iloc[start : position + 1]
        session = window.index[-1].date()
        verdict = evaluate_empirical_starter(
            window,
            expected_session_date=session,
            monthly_event_cap=config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH,
        )
        if verdict.reason in {
            "SPY prior distribution lacks a full calendar cycle",
            "SPY prior distribution is empty",
        }:
            continue
        evaluated += 1
        if not verdict.armed:
            continue
        eligible += 1
        month = session.strftime("%Y-%m")
        if event_counts[month] >= config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH:
            fired.append({
                **verdict.evidence(),
                "executed": False,
                "reason_not_executed": "monthly event cap",
                "cash_before": round(cash, 2),
                "plan": [],
            })
            continue

        budget = min(
            config.FOREVER6_STARTER_CASH_FRAC_PER_EVENT * cash,
            cash - config.FOREVER6_STARTER_CASH_FLOOR,
        )
        candidates: list[dict[str, Any]] = []
        for symbol, frame in prices.items():
            rows = frame.loc[frame.index.date == session]
            if rows.empty:
                continue
            price = float(rows["close"].iloc[-1])
            if math.isfinite(price) and price > 0:
                candidates.append(
                    {"symbol": symbol, "price": price, "held": held[symbol]}
                )
        candidates.sort(key=lambda item: (item["held"] > 0, item["price"]))

        remaining = max(0.0, budget)
        plan: list[dict[str, Any]] = []
        for candidate in candidates:
            if len(plan) >= config.FOREVER6_STARTER_MAX_NAMES:
                break
            symbol = str(candidate["symbol"])
            price = float(candidate["price"])
            owned = int(candidate["held"])
            if price <= remaining:
                plan.append({
                    "symbol": symbol,
                    "reference_close": round(price, 2),
                    "why": "new base" if owned == 0 else f"add (held {owned})",
                })
                remaining -= price

        spent = sum(float(row["reference_close"]) for row in plan)
        cash_before = cash
        if plan:
            event_counts[month] += 1
            cash -= spent
            held.update(row["symbol"] for row in plan)
        fired.append({
            **verdict.evidence(),
            "executed": bool(plan),
            "reason_not_executed": "" if plan else "no affordable reviewed name",
            "cash_before": round(cash_before, 2),
            "event_budget": round(max(0.0, budget), 2),
            "plan": plan,
            "reference_spend": round(spent, 2),
            "cash_after": round(cash, 2),
        })

    return {
        "schema_version": 1,
        "method": (
            "expanding strictly-prior SPY close-return lower tail paired with an "
            "independent expanding-median volume-stress upper tail"
        ),
        "limitations": [
            "Historical catalyst-screen state was unavailable; plans show the reviewed "
            "universe after affordability only.",
            "Reference closes model selection and cash use; they are not claims "
            "about executable after-hours fills.",
            "Adjusted historical bars can differ from the prices displayed at "
            "the time of a later split.",
        ],
        "input": {
            "spy_start": spy.index[0].date().isoformat(),
            "spy_end": spy.index[-1].date().isoformat(),
            "bars": len(spy),
            "runtime_distribution_bars": runtime_bars,
            "initial_cash": round(initial_cash, 2),
            "cash_floor": config.FOREVER6_STARTER_CASH_FLOOR,
            "cash_fraction_per_event": config.FOREVER6_STARTER_CASH_FRAC_PER_EVENT,
            "monthly_event_cap": config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH,
            "max_names_per_event": config.FOREVER6_STARTER_MAX_NAMES,
            "universe": list(config.FOREVER6_UNIVERSE),
        },
        "result": {
            "sessions_evaluated_after_warmup": evaluated,
            "paired_tail_dates": eligible,
            "events_with_a_plan": sum(bool(row["executed"]) for row in fired),
            "ending_cash": round(cash, 2),
            "ending_share_counts": dict(sorted(held.items())),
            "paired_tail_dates_by_year": dict(
                sorted(
                    Counter(row["as_of_date"].split("-")[0] for row in fired).items()
                )
            ),
        },
        "events": fired,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(bars_requested=args.bars, initial_cash=args.initial_cash)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result["result"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
