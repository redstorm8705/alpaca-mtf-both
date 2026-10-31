#!/usr/bin/env python3
# ruff: noqa: E402
"""Read-only replay of the F6 crash classifier on completed daily bars.

The replay shares the production classifier.  It never creates a broker and
labels historical closes as reference prices rather than executable fills.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from data.fetcher import fetch_bars_window
from execution.f6_empirical_trigger import evaluate_empirical_starter

ET = "America/New_York"


def _stocks(symbol: str, start: date, end: date) -> pd.DataFrame:
    begin = datetime.combine(start, time.min, tzinfo=timezone.utc)
    finish = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    frame = fetch_bars_window(
        symbol, config.TF_DAILY, begin, finish, feed="sip", adjustment="split"
    )
    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy().sort_index()
    if out.index.tz is None:
        raise RuntimeError(f"{symbol} history lacks timezone")
    out.index = out.index.tz_convert(ET)
    return out


def _volatility(start: date, end: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    import yfinance as yf

    def load(symbol: str) -> pd.DataFrame:
        frame = yf.download(
            symbol,
            start=start - timedelta(days=60),
            end=end + timedelta(days=1),
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
        if frame is None or frame.empty:
            raise RuntimeError(f"{symbol} history unavailable")
        return frame

    return load("^VIX"), load("^VIX3M")


def _forward_returns(spy: pd.DataFrame, position: int) -> dict[str, float | None]:
    close = float(spy["close"].iloc[position])
    result: dict[str, float | None] = {}
    for horizon in (20, 60):
        target = position + horizon
        result[f"spy_forward_{horizon}s_pct"] = (
            round((float(spy["close"].iloc[target]) / close - 1.0) * 100.0, 4)
            if target < len(spy)
            else None
        )
    return result


def replay(*, start: date, end: date, initial_cash: float) -> dict[str, Any]:
    if start >= end or not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("start must precede end and initial_cash must be positive")
    warmup = start - timedelta(days=120)
    spy = _stocks("SPY", warmup, end)
    if spy.empty:
        raise RuntimeError("SPY daily history unavailable")
    vix, vix3m = _volatility(warmup, end)
    prices = {
        symbol: _stocks(symbol, start, end)
        for symbol in config.FOREVER6_UNIVERSE
    }
    missing = sorted(symbol for symbol, frame in prices.items() if frame.empty)
    if missing:
        raise RuntimeError(f"universe history unavailable: {missing}")

    cash = float(initial_cash)
    held: Counter[str] = Counter()
    monthly_counts: Counter[str] = Counter()
    prior_tier_a_fires: list[date] = []
    events: list[dict[str, Any]] = []
    evaluated = 0
    for position in range(20, len(spy)):
        session = spy.index[position].date()
        if session < start or session > end:
            continue
        window = spy.iloc[: position + 1]
        verdict = evaluate_empirical_starter(
            window,
            expected_session_date=session,
            vix_bars=vix.loc[pd.to_datetime(vix.index).date <= session],
            vix3m_bars=vix3m.loc[pd.to_datetime(vix3m.index).date <= session],
        )
        evaluated += 1
        if not verdict.armed:
            continue

        month = session.strftime("%Y-%m")
        session_dates = list(spy.index[: position + 1].date)
        recent = set(session_dates[-6:-1])
        capped = (
            not verdict.bypass_monthly_cap
            and monthly_counts[month]
            >= config.FOREVER6_STARTER_MAX_EVENTS_PER_MONTH
        )
        cooling = verdict.tier == "A" and any(
            day in recent for day in prior_tier_a_fires
        )
        budget = min(
            float(verdict.cash_fraction or 0.0) * cash,
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
        candidates.sort(key=lambda row: (row["held"] > 0, row["price"]))
        remaining = max(0.0, budget)
        plan: list[dict[str, Any]] = []
        if not capped and not cooling:
            name_cap = (
                len(config.FOREVER6_UNIVERSE)
                if verdict.tier == "C" else verdict.max_names
            )
            for candidate in candidates:
                if len(plan) >= min(name_cap, len(config.FOREVER6_UNIVERSE)):
                    break
                price = float(candidate["price"])
                if price <= remaining:
                    plan.append(
                        {
                            "symbol": candidate["symbol"],
                            "reference_close": round(price, 2),
                            "why": "new base" if candidate["held"] == 0 else "add",
                        }
                    )
                    remaining -= price
        spent = sum(float(row["reference_close"]) for row in plan)
        cash_before = cash
        if plan:
            cash -= spent
            held.update(str(row["symbol"]) for row in plan)
            if verdict.tier != "C":
                monthly_counts[month] += 1
            if verdict.tier == "A":
                prior_tier_a_fires.append(session)
        reason = ""
        if capped:
            reason = "A/B monthly event cap"
        elif cooling:
            reason = "Tier A five-session cooldown"
        elif not plan:
            reason = "no affordable reviewed name"
        events.append(
            {
                **verdict.evidence(),
                **_forward_returns(spy, position),
                "executed_reference_plan": bool(plan),
                "reason_not_executed": reason,
                "cash_before": round(cash_before, 2),
                "event_budget": round(max(0.0, budget), 2),
                "plan": plan,
                "reference_spend": round(spent, 2),
                "cash_after": round(cash, 2),
            }
        )

    yearly = Counter(str(row["as_of_date"])[:4] for row in events)
    tiers = Counter(str(row["tier"]) for row in events)
    return {
        "schema_version": 2,
        "method": "production F6 SPY damage plus VIX confirmation, completed bars only",
        "limitations": [
            "Historical catalyst state was unavailable; plans apply "
            "affordability only.",
            "Reference closes are not claims about executable extended-hours fills.",
            "The present-day reviewed universe is survivorship-biased and is not "
            "used to decide trigger dates.",
        ],
        "input": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "spy_start": spy.index[0].date().isoformat(),
            "spy_end": spy.index[-1].date().isoformat(),
            "initial_cash": round(initial_cash, 2),
            "cash_floor": config.FOREVER6_STARTER_CASH_FLOOR,
            "universe": list(config.FOREVER6_UNIVERSE),
        },
        "result": {
            "sessions_evaluated": evaluated,
            "eligible_crash_events": len(events),
            "reference_plans": sum(
                bool(row["executed_reference_plan"]) for row in events
            ),
            "events_by_year": dict(sorted(yearly.items())),
            "events_by_tier": dict(sorted(tiers.items())),
            "calm_years_with_zero_events": [
                year
                for year in range(start.year, end.year + 1)
                if str(year) not in yearly
            ],
            "worst_forward_20s_pct": min(
                (
                    float(row["spy_forward_20s_pct"])
                    for row in events
                    if row["spy_forward_20s_pct"] is not None
                ),
                default=None,
            ),
            "worst_forward_60s_pct": min(
                (
                    float(row["spy_forward_60s_pct"])
                    for row in events
                    if row["spy_forward_60s_pct"] is not None
                ),
                default=None,
            ),
            "ending_cash": round(cash, 2),
            "ending_share_counts": dict(sorted(held.items())),
        },
        "events": events,
    }


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--initial-cash", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(start=args.start, end=args.end, initial_cash=args.initial_cash)
    _atomic_write(args.output, result)
    print(json.dumps(result["result"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
