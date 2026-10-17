"""Completed-bar empirical trigger for the F6 cash-only starter."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class EmpiricalStarterVerdict:
    armed: bool
    reason: str
    as_of_date: str = ""
    current_return_pct: float | None = None
    return_cutoff_pct: float | None = None
    current_stress_ratio: float | None = None
    stress_cutoff_ratio: float | None = None
    tail_probability: float | None = None
    history_observations: int = 0
    history_start: str = ""
    history_end: str = ""
    prior_record_sha256: str = ""

    def evidence(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}


def _fail(reason: str) -> EmpiricalStarterVerdict:
    return EmpiricalStarterVerdict(armed=False, reason=reason)


def evaluate_empirical_starter(
    bars: Any,
    *,
    expected_session_date: date,
    monthly_event_cap: int,
) -> EmpiricalStarterVerdict:
    """Classify the latest completed SPY bar against strictly prior history.

    The lower-tail probability is derived from the existing monthly event cap
    and the history's own median number of sessions per month.  A distinct
    completed-volume stress series must independently cross the mirrored upper
    tail.
    """
    try:
        import pandas as pd

        if bars is None or getattr(bars, "empty", True):
            return _fail("SPY history unavailable")
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(set(bars.columns)):
            return _fail("SPY history missing OHLC columns")
        if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
            return _fail("SPY history lacks a timezone-aware index")
        frame = bars.loc[:, sorted(required)].copy().sort_index()
        if frame.index.has_duplicates:
            return _fail("SPY history contains duplicate bars")
        frame.index = frame.index.tz_convert(_ET)
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        values = frame.loc[:, sorted(required)]
        if values.isna().any().any():
            return _fail("SPY history contains nonnumeric bars")
        valid = all(
            values[column].map(
                lambda value: math.isfinite(float(value)) and value > 0
            ).all()
            for column in values.columns
        )
        if not valid:
            return _fail("SPY history contains invalid prices")
        if (frame["high"] < frame["low"]).any():
            return _fail("SPY history contains inverted bars")

        session_dates = frame.index.date
        if session_dates[-1] != expected_session_date:
            return _fail("latest SPY bar is not the expected completed session")

        prior_close = frame["close"].shift(1)
        return_fraction = frame["close"] / prior_close - 1.0
        returns = return_fraction * 100.0
        prior_volume_median = frame["volume"].expanding().median().shift(1)
        stress = frame["volume"] / prior_volume_median
        usable = pd.DataFrame({"return_pct": returns, "stress_ratio": stress}).dropna()
        if usable.empty or usable.index[-1] != frame.index[-1]:
            return _fail("latest SPY return/stress is unavailable")
        history = usable.iloc[:-1]
        if history.empty:
            return _fail("SPY prior distribution is empty")

        history_months = set(history.index.month.tolist())
        if history_months != set(range(1, 13)):
            return _fail("SPY prior distribution lacks a full calendar cycle")
        month_keys = history.index.tz_localize(None).to_period("M")
        monthly_counts = history.groupby(month_keys).size()
        median_sessions = float(monthly_counts.median())
        if median_sessions <= 0 or monthly_event_cap <= 0:
            return _fail("monthly frequency evidence is invalid")
        tail_probability = float(monthly_event_cap) / median_sessions
        if not 0.0 < tail_probability < 1.0:
            return _fail("derived tail probability is outside (0, 1)")

        return_cutoff = float(
            history["return_pct"].quantile(tail_probability, interpolation="lower")
        )
        stress_cutoff = float(
            history["stress_ratio"].quantile(
                1.0 - tail_probability, interpolation="higher"
            )
        )
        current_return = float(usable["return_pct"].iloc[-1])
        current_stress = float(usable["stress_ratio"].iloc[-1])
        if not all(
            math.isfinite(value)
            for value in (return_cutoff, stress_cutoff, current_return, current_stress)
        ):
            return _fail("empirical trigger produced nonfinite evidence")

        records = [
            {
                "date": idx.date().isoformat(),
                "return_pct": float(row.return_pct),
                "stress_ratio": float(row.stress_ratio),
            }
            for idx, row in history.iterrows()
        ]
        digest = hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return EmpiricalStarterVerdict(
            armed=current_return <= return_cutoff and current_stress >= stress_cutoff,
            reason=(
                "empirical return and stress tails confirmed"
                if current_return <= return_cutoff and current_stress >= stress_cutoff
                else "empirical return/stress pair not confirmed"
            ),
            as_of_date=expected_session_date.isoformat(),
            current_return_pct=current_return,
            return_cutoff_pct=return_cutoff,
            current_stress_ratio=current_stress,
            stress_cutoff_ratio=stress_cutoff,
            tail_probability=tail_probability,
            history_observations=len(history),
            history_start=history.index[0].date().isoformat(),
            history_end=history.index[-1].date().isoformat(),
            prior_record_sha256=digest,
        )
    except Exception as exc:
        return _fail(f"empirical trigger failed: {exc}")
