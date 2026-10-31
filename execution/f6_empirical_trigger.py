"""Completed-bar, broad-market crash trigger for the F6 cash-only tier."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_TIERS = frozenset({"A", "B", "C"})


@dataclass(frozen=True)
class EmpiricalStarterVerdict:
    armed: bool
    reason: str
    as_of_date: str = ""
    tier: str = ""
    close_return_pct: float | None = None
    low_return_pct: float | None = None
    three_session_return_pct: float | None = None
    vix_close: float | None = None
    vix_prior_20d_avg: float | None = None
    vix_to_prior_avg: float | None = None
    vix3m_close: float | None = None
    vix_term_ratio: float | None = None
    price_gate: bool = False
    vix_confirmed: bool = False
    cash_fraction: float | None = None
    max_names: int = 0
    bypass_monthly_cap: bool = False
    history_observations: int = 0
    history_start: str = ""
    history_end: str = ""
    prior_session_dates: tuple[str, ...] = ()
    prior_record_sha256: str = ""

    def evidence(self) -> dict[str, Any]:
        payload = {"schema_version": 2, **asdict(self)}
        payload["prior_session_dates"] = list(self.prior_session_dates)
        return payload


def _fail(reason: str) -> EmpiricalStarterVerdict:
    return EmpiricalStarterVerdict(armed=False, reason=reason)


def fetch_completed_vix_history(expected_session_date: date) -> tuple[Any, Any]:
    """Load VIX/VIX3M daily closes from the permitted T4 source."""
    import yfinance as yf

    start = expected_session_date - timedelta(days=90)
    end = expected_session_date + timedelta(days=1)  # yfinance end is exclusive
    vix = yf.download(
        "^VIX", start=start, end=end, interval="1d", progress=False,
        auto_adjust=False,
    )
    vix3m = yf.download(
        "^VIX3M", start=start, end=end, interval="1d", progress=False,
        auto_adjust=False,
    )
    return vix, vix3m


def _daily_close(frame: Any, label: str) -> Any:
    import pandas as pd

    if frame is None or getattr(frame, "empty", True):
        raise ValueError(f"{label} history unavailable")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(f"{label} history lacks a datetime index")
    if frame.index.has_duplicates:
        raise ValueError(f"{label} history contains duplicate bars")
    out = frame.copy().sort_index()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    close_name = next(
        (name for name in out.columns if str(name).lower() == "close"), None
    )
    if close_name is None:
        raise ValueError(f"{label} history lacks close")
    values = pd.to_numeric(out[close_name], errors="coerce")
    if values.isna().any() or not values.map(
        lambda value: math.isfinite(float(value)) and float(value) > 0
    ).all():
        raise ValueError(f"{label} history contains invalid closes")
    session_dates = [stamp.date() for stamp in out.index]
    if len(session_dates) != len(set(session_dates)):
        raise ValueError(f"{label} history has duplicate session dates")
    return pd.Series(values.to_numpy(dtype=float), index=session_dates, name=label)


def evaluate_empirical_starter(
    bars: Any,
    *,
    expected_session_date: date,
    vix_bars: Any,
    vix3m_bars: Any,
) -> EmpiricalStarterVerdict:
    """Classify completed SPY/VIX/VIX3M observations using the locked crash map."""
    try:
        import pandas as pd

        if bars is None or getattr(bars, "empty", True):
            return _fail("SPY history unavailable")
        required = {"open", "high", "low", "close"}
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
        if frame.isna().any().any():
            return _fail("SPY history contains nonnumeric bars")
        if not all(
            frame[column].map(
                lambda value: math.isfinite(float(value)) and float(value) > 0
            ).all()
            for column in required
        ):
            return _fail("SPY history contains invalid prices")
        if (
            (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()
        ):
            return _fail("SPY history contains internally inconsistent OHLC bars")
        session_dates = list(frame.index.date)
        if len(session_dates) != len(set(session_dates)):
            return _fail("SPY history has duplicate session dates")
        if session_dates[-1] != expected_session_date:
            return _fail("latest SPY bar is not the expected completed session")
        if len(frame) < 21:
            return _fail("SPY history lacks crash lookback")

        vix = _daily_close(vix_bars, "VIX")
        vix3m = _daily_close(vix3m_bars, "VIX3M")
        required_dates = session_dates[-21:]
        if any(day not in vix.index for day in required_dates):
            return _fail("VIX history is not aligned to the last 21 SPY sessions")
        if expected_session_date not in vix3m.index:
            return _fail("VIX3M latest session is missing")

        prior_close = float(frame["close"].iloc[-2])
        close_return = (float(frame["close"].iloc[-1]) / prior_close - 1.0) * 100.0
        low_return = (float(frame["low"].iloc[-1]) / prior_close - 1.0) * 100.0
        three_session_return = (
            float(frame["close"].iloc[-1]) / float(frame["close"].iloc[-4]) - 1.0
        ) * 100.0
        vix_close = float(vix.loc[expected_session_date])
        vix_prior_avg = float(vix.loc[required_dates[:-1]].mean())
        vix3m_close = float(vix3m.loc[expected_session_date])
        if not all(
            math.isfinite(value) and value > 0
            for value in (prior_close, vix_close, vix_prior_avg, vix3m_close)
        ):
            return _fail("crash trigger produced invalid market evidence")
        vix_to_avg = vix_close / vix_prior_avg
        term_ratio = vix_close / vix3m_close

        dynamic_close_floor = -max(2.0, 0.15 * vix_close)
        price_gate = (
            close_return <= dynamic_close_floor
            or low_return <= -3.5
            or three_session_return <= -10.0
        )
        vix_confirmed = vix_to_avg >= 1.3 or term_ratio > 1.0
        depth = min(close_return, low_return)
        tier = ""
        cash_fraction = None
        max_names = 0
        bypass = False
        if close_return < 0 and price_gate and vix_confirmed:
            if (
                (depth <= -7.0 or three_session_return <= -10.0)
                and vix_close > 35.0
            ):
                # Zero means the whole reviewed universe; it avoids a sentinel
                # count that could silently become a cap if the universe grows.
                tier, cash_fraction, max_names, bypass = "C", 0.30, 0, True
            elif -7.0 < depth <= -4.0 and term_ratio > 1.0:
                tier, cash_fraction, max_names = "B", 0.20, 3
            elif -4.0 < depth <= -2.0:
                tier, cash_fraction, max_names = "A", 0.10, 2

        armed = tier in _TIERS
        if armed:
            reason = f"broad-market crash tier {tier} confirmed by SPY and VIX"
        elif close_return >= 0:
            reason = "SPY did not close negative"
        elif not price_gate:
            reason = "SPY price damage did not reach the crash gate"
        elif not vix_confirmed:
            reason = "SPY selloff lacked heightened VIX confirmation"
        else:
            reason = "SPY/VIX evidence did not map to an approved crash tier"

        records = [
            {
                "date": day.isoformat(),
                "spy_close": float(frame["close"].iloc[-21 + offset]),
                "vix_close": float(vix.loc[day]),
            }
            for offset, day in enumerate(required_dates[:-1])
        ]
        records.append(
            {
                "date": expected_session_date.isoformat(),
                "spy_open": float(frame["open"].iloc[-1]),
                "spy_high": float(frame["high"].iloc[-1]),
                "spy_low": float(frame["low"].iloc[-1]),
                "spy_close": float(frame["close"].iloc[-1]),
                "vix_close": vix_close,
                "vix3m_close": vix3m_close,
            }
        )
        digest = hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return EmpiricalStarterVerdict(
            armed=armed,
            reason=reason,
            as_of_date=expected_session_date.isoformat(),
            tier=tier,
            close_return_pct=close_return,
            low_return_pct=low_return,
            three_session_return_pct=three_session_return,
            vix_close=vix_close,
            vix_prior_20d_avg=vix_prior_avg,
            vix_to_prior_avg=vix_to_avg,
            vix3m_close=vix3m_close,
            vix_term_ratio=term_ratio,
            price_gate=price_gate,
            vix_confirmed=vix_confirmed,
            cash_fraction=cash_fraction,
            max_names=max_names,
            bypass_monthly_cap=bypass,
            history_observations=len(required_dates) - 1,
            history_start=required_dates[0].isoformat(),
            history_end=required_dates[-2].isoformat(),
            prior_session_dates=tuple(
                session.isoformat() for session in required_dates[-6:-1]
            ),
            prior_record_sha256=digest,
        )
    except Exception as exc:
        return _fail(f"crash trigger failed: {exc}")
