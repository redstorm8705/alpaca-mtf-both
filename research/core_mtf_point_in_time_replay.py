"""Point-in-time, completed-bar lifecycle evaluator for Core MTF research.

This module is intentionally pure and is never imported by the RTH trading path.
It makes no broker or network call. An external data adapter must supply timestamped
OHLC bars and record its provenance before this evaluator may be used for a decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Iterable, Literal


Direction = Literal["short"]
ExitReason = Literal["stop", "target", "time", "unevaluable"]


@dataclass(frozen=True)
class OHLCBar:
    """A bar timestamped at its *start*, with a known fixed duration."""

    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ShortCandidate:
    decision_time: datetime
    symbol: str
    stop: float
    target: float | None
    max_hold: timedelta
    source: str


def completed_bars(
    bars: Iterable[OHLCBar], *, as_of: datetime, duration: timedelta
) -> list[OHLCBar]:
    """Return bars fully known at ``as_of``; reject naive/malformed observations.

    The strict end-time test is the core anti-look-ahead contract. A bar that starts
    before a decision but ends after it is excluded.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")
    result: list[OHLCBar] = []
    for bar in bars:
        if bar.start.tzinfo is None:
            raise ValueError("bar timestamp must be timezone-aware")
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            raise ValueError("OHLC values must be positive")
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("invalid OHLC range")
        if bar.start + duration <= as_of:
            result.append(bar)
    return sorted(result, key=lambda bar: bar.start)


def first_executable_bar(
    bars: Iterable[OHLCBar], *, after: datetime
) -> OHLCBar | None:
    """First bar opening strictly after a decision; never fill at a known close."""
    candidates = [bar for bar in bars if bar.start > after]
    return min(candidates, key=lambda bar: bar.start) if candidates else None


def evaluate_short(
    candidate: ShortCandidate,
    bars: Iterable[OHLCBar],
    *,
    round_trip_cost_bps: float,
) -> dict:
    """Evaluate a single short with next-bar entry and conservative intrabar exits.

    Stop and target may both trade inside the same OHLC bar. Their order is unknown
    without quote/tick data, so this evaluator assumes the adverse stop fills first.
    A gap through the stop fills at the bar open, again conservatively.
    """
    if candidate.decision_time.tzinfo is None:
        raise ValueError("decision_time must be timezone-aware")
    if candidate.stop <= 0 or candidate.target is not None and candidate.target <= 0:
        raise ValueError("stop and target must be positive")
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be non-negative")
    ordered = sorted(list(bars), key=lambda bar: bar.start)
    entry_bar = first_executable_bar(ordered, after=candidate.decision_time)
    base = {"schema_v": 1, "symbol": candidate.symbol, "direction": "short",
            "decision_time": candidate.decision_time.isoformat(), "source": candidate.source}
    if entry_bar is None:
        return {**base, "status": "unevaluable", "reason": "no_next_executable_bar"}
    entry = entry_bar.open
    if candidate.stop <= entry:
        return {**base, "status": "unevaluable", "reason": "short_stop_not_above_entry"}
    if candidate.target is not None and candidate.target >= entry:
        return {**base, "status": "unevaluable", "reason": "short_target_not_below_entry"}
    deadline = entry_bar.start + candidate.max_hold
    path = [bar for bar in ordered if entry_bar.start <= bar.start <= deadline]
    for bar in path:
        # A price gap already beyond protection has to receive the executable open.
        if bar.open >= candidate.stop:
            return _result(base, entry, bar.open, bar.start, "stop", round_trip_cost_bps)
        if candidate.target is not None and bar.open <= candidate.target:
            return _result(base, entry, bar.open, bar.start, "target", round_trip_cost_bps)
        hit_stop = bar.high >= candidate.stop
        hit_target = candidate.target is not None and bar.low <= candidate.target
        if hit_stop:  # includes ambiguous stop+target bar: adverse ordering.
            return _result(base, entry, candidate.stop, bar.start, "stop", round_trip_cost_bps,
                           ambiguous=bool(hit_target))
        if hit_target:
            return _result(base, entry, candidate.target, bar.start, "target", round_trip_cost_bps)
    if not path:
        return {**base, "status": "unevaluable", "reason": "no_path_after_entry"}
    return _result(base, entry, path[-1].close, path[-1].start, "time", round_trip_cost_bps)


def _result(base: dict, entry: float, exit_price: float, exit_time: datetime,
            reason: ExitReason, cost_bps: float, *, ambiguous: bool = False) -> dict:
    gross = (entry - exit_price) / entry
    return {**base, "status": "complete", "entry_price": entry, "exit_price": exit_price,
            "exit_time": exit_time.isoformat(), "exit_reason": reason,
            "ambiguous_intrabar": ambiguous, "gross_return": gross,
            "net_return": gross - cost_bps / 10_000.0}


def as_record(result: dict) -> dict:
    """Return a JSON-serializable result and reject accidental non-finite fields upstream."""
    return dict(result)
