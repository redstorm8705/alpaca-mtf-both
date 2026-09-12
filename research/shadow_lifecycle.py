"""Forward-only score-model shadow lifecycle evaluator.

This is research tooling, not a production-strategy replay.  It evaluates the
12-point and 16-point *score-model* decisions captured in
``score_comparison_events.jsonl`` under one fixed, transparent protocol:

* first qualifying scan per (RTH session, model, symbol),
* first 5-minute bar strictly after that scan as the entry,
* final 5-minute RTH-bar close as the exit, and
* a scalar, round-trip cost deduction from the direction-adjusted return.

The module is never imported by the live loop.  Its optional Alpaca adapter is
only called from the explicit CLI after the evaluated session is complete; it
uses market-data bars and has no trading-client or order API dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from research.deflated_sharpe import deflated_sharpe, sharpe_stats

_ROOT = Path(__file__).resolve().parent.parent
_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_SCHEMA_V = 1
_PROTOCOL_V = "score-model-rth-close-v1"
_VARIANTS = ("12pt", "16pt")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


@dataclass(frozen=True)
class Bar:
    """A completed five-minute OHLC bar used by the offline evaluator."""

    start: datetime
    open: float
    close: float


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(_ET)


def _finite_number(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _rth_session(timestamp: datetime) -> date | None:
    local = timestamp.astimezone(_ET)
    if local.weekday() >= 5 or not (_RTH_OPEN <= local.time() < _RTH_CLOSE):
        return None
    return local.date()


def load_jsonl(path: Path) -> tuple[list[dict], int]:
    """Load valid JSON-object lines, counting malformed rows without repairing them."""
    rows: list[dict] = []
    skipped = 0
    if not path.exists():
        return rows, skipped
    for raw in path.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(item, dict):
            rows.append(item)
        else:
            skipped += 1
    return rows, skipped


def _choose_direction(row: dict, variant: str) -> tuple[str | None, str | None]:
    suffix = "12" if variant == "12pt" else "16"
    choices: list[tuple[str, float]] = []
    for direction in ("long", "short"):
        passed = row.get(f"{direction}_signal_{suffix}")
        score = _finite_number(row.get(f"{direction}_{suffix}pt"))
        if passed is True and score is not None:
            choices.append((direction, score))
    if not choices:
        return None, "no_direction_passed"
    if len(choices) == 1:
        return choices[0][0], None
    if choices[0][1] == choices[1][1]:
        return None, "direction_tie"
    return max(choices, key=lambda pair: pair[1])[0], None


def select_candidates(events: Iterable[dict]) -> tuple[list[dict], dict[str, int]]:
    """Select the first qualifying candidate per (session, variant, symbol).

    The score ledger is append-only, so input ordering is not trusted.  We sort
    valid scan timestamps before choosing the first qualifying decision.
    """
    observations: list[tuple[datetime, dict]] = []
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        if event.get("schema_v") != 1:
            counts["unsupported_event_schema"] += 1
            continue
        scan_time = _parse_time(event.get("scan_time"))
        if scan_time is None:
            counts["invalid_scan_time"] += 1
            continue
        session = _rth_session(scan_time)
        if session is None:
            counts["outside_rth"] += 1
            continue
        tickers = event.get("tickers")
        if not isinstance(tickers, list):
            counts["invalid_tickers"] += 1
            continue
        for row in tickers:
            if isinstance(row, dict):
                observations.append((scan_time, row))
            else:
                counts["invalid_ticker_row"] += 1

    chosen: set[tuple[str, str, str]] = set()
    candidates: list[dict] = []
    for scan_time, row in sorted(observations, key=lambda item: item[0]):
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            counts["invalid_symbol"] += 1
            continue
        session = _rth_session(scan_time)
        assert session is not None  # established while observations were built
        for variant in _VARIANTS:
            direction, reason = _choose_direction(row, variant)
            if direction is None:
                counts[f"{variant}:{reason}"] += 1
                continue
            key = (session.isoformat(), variant, symbol)
            if key in chosen:
                counts[f"{variant}:later_qualifying_scan"] += 1
                continue
            chosen.add(key)
            candidates.append(
                {
                    "session": session.isoformat(),
                    "variant": variant,
                    "symbol": symbol,
                    "direction": direction,
                    "scan_time": scan_time.isoformat(),
                }
            )
    return candidates, dict(counts)


def _as_bar(item: Bar | dict) -> Bar | None:
    if isinstance(item, Bar):
        return item
    if not isinstance(item, dict):
        return None
    stamp = (
        _parse_time(item.get("start"))
        if not isinstance(item.get("start"), datetime)
        else item["start"].astimezone(_ET)
    )
    opening = _finite_number(item.get("open"))
    closing = _finite_number(item.get("close"))
    if (
        stamp is None
        or opening is None
        or closing is None
        or opening <= 0.0
        or closing <= 0.0
    ):
        return None
    return Bar(stamp, opening, closing)


def evaluate_candidate(
    candidate: dict, bars: Iterable[Bar | dict], round_trip_cost_bps: float
) -> dict:
    """Return an immutable lifecycle result or a concrete unevaluable reason."""
    scan_time = _parse_time(candidate.get("scan_time"))
    session = str(candidate.get("session") or "")
    direction = candidate.get("direction")
    if scan_time is None or direction not in ("long", "short"):
        return {**candidate, "status": "unevaluable", "reason": "invalid_candidate"}
    if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps < 0.0:
        raise ValueError("round_trip_cost_bps must be finite and >= 0")
    parsed = [_as_bar(item) for item in bars]
    rth = [
        bar
        for bar in parsed
        if bar is not None
        and _rth_session(bar.start)
        and bar.start.date().isoformat() == session
    ]
    entry = next(
        (
            bar
            for bar in sorted(rth, key=lambda bar: bar.start)
            if bar.start > scan_time
        ),
        None,
    )
    exit_bar = max(rth, key=lambda bar: bar.start, default=None)
    if entry is None:
        return {
            **candidate,
            "status": "unevaluable",
            "reason": "no_strict_after_scan_entry_bar",
        }
    if exit_bar is None:
        return {**candidate, "status": "unevaluable", "reason": "no_rth_exit_bar"}
    gross = (
        ((exit_bar.close - entry.open) / entry.open)
        if direction == "long"
        else ((entry.open - exit_bar.close) / entry.open)
    )
    cost = round_trip_cost_bps / 10_000.0
    result = {
        **candidate,
        "schema_v": _SCHEMA_V,
        "protocol_v": _PROTOCOL_V,
        "status": "complete",
        "entry_time": entry.start.isoformat(),
        "entry_price": entry.open,
        "exit_time": exit_bar.start.isoformat(),
        "exit_price": exit_bar.close,
        "gross_return": gross,
        "cost_return": cost,
        "net_return": gross - cost,
    }
    result["result_id"] = deterministic_result_id(result)
    return result


def deterministic_result_id(result: dict) -> str:
    payload = "|".join(
        str(result.get(key, ""))
        for key in (
            "schema_v",
            "protocol_v",
            "session",
            "variant",
            "symbol",
            "scan_time",
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def append_new_results(path: Path, results: Iterable[dict]) -> int:
    """Append only completed/unevaluable results whose deterministic key is new."""
    prior, _ = load_jsonl(path)
    seen = {
        row.get("result_id") for row in prior if isinstance(row.get("result_id"), str)
    }
    pending: list[dict] = []
    for result in results:
        result = dict(result)
        result.setdefault("schema_v", _SCHEMA_V)
        result.setdefault("protocol_v", _PROTOCOL_V)
        result.setdefault("result_id", deterministic_result_id(result))
        if result["result_id"] not in seen:
            seen.add(result["result_id"])
            pending.append(result)
    if not pending:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for result in pending:
            handle.write(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
        handle.flush()
    return len(pending)


def daily_returns(results: Iterable[dict]) -> dict[str, dict[str, float]]:
    """Equal-weight same-session completed candidates once per score-model variant."""
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for result in results:
        if (
            result.get("schema_v") != _SCHEMA_V
            or result.get("protocol_v") != _PROTOCOL_V
        ):
            continue
        if result.get("status") != "complete" or result.get("variant") not in _VARIANTS:
            continue
        ret = _finite_number(result.get("net_return"))
        session = result.get("session")
        if ret is not None and isinstance(session, str):
            groups[(str(result["variant"]), session)].append(ret)
    output: dict[str, dict[str, float]] = {variant: {} for variant in _VARIANTS}
    for (variant, session), values in groups.items():
        output[variant][session] = float(np.mean(values))
    return output


def session_summary(
    events: Iterable[dict], candidates: Iterable[dict], results: Iterable[dict]
) -> dict[str, dict[str, dict[str, int | str]]]:
    """Expose session counts without treating no-candidate days as zero returns."""
    output: dict[str, dict[str, dict[str, int | str]]] = defaultdict(dict)
    for event in events:
        scan_time = (
            _parse_time(event.get("scan_time")) if event.get("schema_v") == 1 else None
        )
        session = _rth_session(scan_time) if scan_time is not None else None
        if session is not None:
            for variant in _VARIANTS:
                output[session.isoformat()].setdefault(
                    variant,
                    {
                        "candidate_count": 0,
                        "complete_count": 0,
                        "unevaluable_count": 0,
                        "status": "no_observation",
                    },
                )
    for candidate in candidates:
        session, variant = candidate.get("session"), candidate.get("variant")
        if isinstance(session, str) and variant in _VARIANTS:
            row = output[session].setdefault(
                variant,
                {
                    "candidate_count": 0,
                    "complete_count": 0,
                    "unevaluable_count": 0,
                    "status": "pending",
                },
            )
            row["candidate_count"] = int(row["candidate_count"]) + 1
            row["status"] = "pending"
    for result in results:
        session, variant = result.get("session"), result.get("variant")
        if (
            result.get("schema_v") != _SCHEMA_V
            or result.get("protocol_v") != _PROTOCOL_V
            or not isinstance(session, str)
            or variant not in _VARIANTS
        ):
            continue
        row = output[session].setdefault(
            variant,
            {
                "candidate_count": 0,
                "complete_count": 0,
                "unevaluable_count": 0,
                "status": "pending",
            },
        )
        if result.get("status") == "complete":
            row["complete_count"] = int(row["complete_count"]) + 1
            row["status"] = "complete"
        elif result.get("status") == "unevaluable":
            row["unevaluable_count"] = int(row["unevaluable_count"]) + 1
            if row["status"] != "complete":
                row["status"] = "unevaluable"
    return {session: dict(variants) for session, variants in sorted(output.items())}


def load_trial_registry(path: Path) -> tuple[int | None, str | None]:
    """Return the declared all-time trial count only for an attested registry."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, "trial_registry_missing_or_malformed"
    trials = data.get("trials") if isinstance(data, dict) else None
    if (
        data.get("schema_v") != 1
        or data.get("complete") is not True
        or not isinstance(trials, list)
        or not trials
    ):
        return None, "trial_registry_not_attested_complete"
    ids = {
        item.get("id")
        for item in trials
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if len(ids) != len(trials):
        return None, "trial_registry_invalid_ids"
    return len(ids), None


def dsr_report(results: Iterable[dict], registry_path: Path) -> dict:
    """Compute DSR only from matched complete daily observations, or fail loud."""
    n_trials, registry_error = load_trial_registry(registry_path)
    if registry_error:
        return {"available": False, "reason": registry_error}
    by_variant = daily_returns(results)
    available = [variant for variant, rows in by_variant.items() if rows]
    if len(available) < 2:
        return {
            "available": False,
            "reason": "fewer_than_two_measured_variants",
            "n_trials": n_trials,
        }
    common = set.intersection(*(set(by_variant[variant]) for variant in available))
    if len(common) < 30:
        return {
            "available": False,
            "reason": "fewer_than_30_common_sessions",
            "n_trials": n_trials,
            "n_common": len(common),
        }
    ordered = sorted(common)
    series = {
        variant: [by_variant[variant][session] for session in ordered]
        for variant in available
    }
    try:
        sharpes = [sharpe_stats(series[variant])[1] for variant in available]
    except ValueError as exc:
        return {
            "available": False,
            "reason": f"undefined_variant_sharpe:{exc}",
            "n_trials": n_trials,
        }
    dispersion = float(np.std(sharpes, ddof=1))
    if not math.isfinite(dispersion) or dispersion <= 0.0:
        return {
            "available": False,
            "reason": "nonpositive_cross_variant_sharpe_dispersion",
            "n_trials": n_trials,
        }
    return {
        "available": True,
        "n_trials": n_trials,
        "sessions": ordered,
        "variants": {
            variant: deflated_sharpe(
                series[variant], n_trials=n_trials, trials_sr_std=dispersion
            ).as_dict()
            for variant in available
        },
    }


def alpaca_5m_bars(symbol: str, session: str) -> list[Bar]:
    """Explicit market-data adapter; never invoked by imports or production code."""
    from alpaca.data.requests import StockBarsRequest

    import config
    from data import fetcher

    day = date.fromisoformat(session)
    start = datetime.combine(day, _RTH_OPEN, tzinfo=_ET)
    end = datetime.combine(day, _RTH_CLOSE, tzinfo=_ET)
    fetcher._rate_gate()
    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=fetcher.TF_MAP[config.TF_5M],
        start=start.astimezone(_UTC),
        end=end.astimezone(_UTC),
    )
    frame = fetcher.get_client().get_stock_bars(request).df
    if frame is None or frame.empty:
        return []
    if getattr(frame.index, "nlevels", 1) > 1:
        frame = frame.xs(symbol, level="symbol")
    return [
        Bar(index.to_pydatetime().astimezone(_ET), float(row.open), float(row.close))
        for index, row in frame.iterrows()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--events", type=Path, default=_ROOT / "logs/score_comparison_events.jsonl"
    )
    parser.add_argument(
        "--results", type=Path, default=_ROOT / "logs/shadow_lifecycle_events.jsonl"
    )
    parser.add_argument(
        "--registry", type=Path, default=_ROOT / "research/shadow_trial_registry.json"
    )
    parser.add_argument("--round-trip-cost-bps", type=float, default=10.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.round_trip_cost_bps) or args.round_trip_cost_bps < 0.0:
        parser.error("--round-trip-cost-bps must be finite and >= 0")
    events, malformed = load_jsonl(args.events)
    candidates, skipped = select_candidates(events)
    today = datetime.now(_ET).date()
    results: list[dict] = []
    for candidate in candidates:
        session = date.fromisoformat(candidate["session"])
        if session >= today:  # current RTH session is never evaluated before its close
            continue
        try:
            bars = alpaca_5m_bars(candidate["symbol"], candidate["session"])
            results.append(
                evaluate_candidate(candidate, bars, args.round_trip_cost_bps)
            )
        except Exception as exc:  # noqa: BLE001 -- every external market-data failure becomes evidence, never a fabricated return
            results.append(
                {
                    **candidate,
                    "status": "unevaluable",
                    "reason": f"bar_fetch_failed:{type(exc).__name__}",
                }
            )
    appended = append_new_results(args.results, results)
    persisted, persisted_bad = load_jsonl(args.results)
    print(
        json.dumps(
            {
                "protocol_v": _PROTOCOL_V,
                "events": len(events),
                "malformed_events": malformed,
                "candidates": len(candidates),
                "selection_skips": skipped,
                "results_appended": appended,
                "malformed_results": persisted_bad,
                "sessions": session_summary(events, candidates, persisted),
                "dsr": dsr_report(persisted, args.registry),
            },
            default=str,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
