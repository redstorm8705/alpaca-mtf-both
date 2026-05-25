"""
indicators/momentum.py
Jegadeesh-Titman momentum factor (1993, validated 2023).

Uses the 12-1 month standard formulation:
  - Look back 12 months (252 trading days)
  - Skip the most recent 1 month (21 days) to avoid short-term reversal
  - Signal: price today vs price 252 days ago, excluding last 21 days

Empirical evidence (peer-reviewed, 30+ years out-of-sample):
  - ~1% monthly excess return in long/short portfolio (Jegadeesh & Titman 1993)
  - Confirmed in 40+ countries (Asness, Moskowitz & Pedersen 2013)
  - Still significant post-2000 (Jegadeesh & Titman 2023)
  - Works across asset classes: equities, bonds, currencies, commodities

For intraday confluence scoring:
  - Long signals: price must be higher than 252 days ago (momentum positive)
  - Short signals: price must be lower than 252 days ago (momentum negative)
  - Scored as +2 in confluence scorer — equal weight to SMA conditions
"""

import pandas as pd
import numpy as np
import config


def calculate_momentum_12_1(df: pd.DataFrame) -> float:
    """
    Calculate 12-1 month momentum return.

    Args:
        df: Daily OHLCV DataFrame, must have at least 273 bars (252 + 21 buffer)

    Returns:
        Float: momentum return as decimal (e.g., 0.15 = +15% momentum)
        None if insufficient data
    """
    if df.empty or len(df) < config.MOMENTUM_LONG_LOOKBACK + config.MOMENTUM_SHORT_LOOKBACK:
        return None

    # Price at the start of the formation period (12 months ago)
    start_idx = -(config.MOMENTUM_LONG_LOOKBACK + config.MOMENTUM_SHORT_LOOKBACK)
    # Price at end of formation period (1 month ago, skipping recent month)
    end_idx   = -config.MOMENTUM_SHORT_LOOKBACK

    price_start = float(df["close"].iloc[start_idx])
    price_end   = float(df["close"].iloc[end_idx])

    if price_start <= 0:
        return None

    return (price_end - price_start) / price_start


def momentum_bullish(df: pd.DataFrame) -> bool:
    """
    Returns True if 12-1 month momentum is positive (long signal condition).
    Standard: price today (skipping last month) > price 12 months ago.
    """
    mom = calculate_momentum_12_1(df)
    if mom is None:
        return False
    return mom > config.MOMENTUM_MIN_PCT


def momentum_bearish(df: pd.DataFrame) -> bool:
    """
    Returns True if 12-1 month momentum is negative (short signal condition).
    """
    mom = calculate_momentum_12_1(df)
    if mom is None:
        return False
    return mom < -config.MOMENTUM_MIN_PCT


def momentum_strength(df: pd.DataFrame) -> str:
    """
    Returns a label for momentum strength (useful for logging).
    """
    mom = calculate_momentum_12_1(df)
    if mom is None:
        return "insufficient_data"
    if mom > 0.30:
        return "strong_bull"
    elif mom > 0.10:
        return "bull"
    elif mom > 0.0:
        return "weak_bull"
    elif mom > -0.10:
        return "weak_bear"
    elif mom > -0.30:
        return "bear"
    else:
        return "strong_bear"


def calculate_ewma_vol_60d(df: pd.DataFrame) -> float:
    """EWMA annualized vol, center-of-mass=60 days (MOP 2012 Eq. 4).
    Superior to simple std for detecting vol-regime changes."""
    try:
        if df is None or len(df) < 62:
            return None
        log_ret = np.log(df["close"] / df["close"].shift(1)).dropna()
        ewma_var = log_ret.ewm(com=60, adjust=True).var()
        return round(float(np.sqrt(ewma_var.iloc[-1] * 261)), 4)
    except Exception:
        return None


def calculate_tsmom(df: pd.DataFrame, lookback_days: int) -> float:
    """Time-series momentum: own past return over lookback_days (MOP 2012).
    No skip period — MOP confirmed results do not depend on excluding last month."""
    try:
        if df is None or len(df) < lookback_days + 1:
            return None
        p_now  = float(df["close"].iloc[-1])
        p_then = float(df["close"].iloc[-lookback_days])
        if p_then <= 0:
            return None
        return round((p_now - p_then) / p_then, 4)
    except Exception:
        return None


def pct_from_52wk_high(df: pd.DataFrame) -> float:
    """
    Distance from 52-week high as a percentage.
    Clenow uses this as a trend strength proxy:
      0% = at the high (strongest trend)
      -15% = meaningfully off the high (weakening)
      -30%+ = broken trend
    """
    try:
        high_252 = float(df["high"].tail(252).max())
        current  = float(df["close"].iloc[-1])
        if high_252 <= 0:
            return None
        return round((current - high_252) / high_252 * 100, 2)
    except Exception:
        return None


def realized_vol_20d(df: pd.DataFrame) -> float:
    """
    20-day realized (historical) volatility, annualized.
    Chan: >40% annualized = high-risk regime, reduce position size.
    Expressed as decimal (0.35 = 35% annualized vol).
    """
    try:
        if len(df) < 22:
            return None
        returns = df["close"].tail(22).pct_change().dropna()
        daily_vol = float(returns.std())
        return round(daily_vol * (252 ** 0.5), 4)
    except Exception:
        return None


def get_momentum_summary(df: pd.DataFrame) -> dict:
    """Returns momentum summary dict for logging and dashboard."""
    mom = calculate_momentum_12_1(df)
    summary = {
        "momentum_12_1":      round(mom * 100, 2) if mom is not None else None,
        "bullish":            momentum_bullish(df),
        "bearish":            momentum_bearish(df),
        "strength":           momentum_strength(df),
        "pct_from_52wk_high": pct_from_52wk_high(df),
        "realized_vol_20d":   realized_vol_20d(df),
        "lookback_long":      config.MOMENTUM_LONG_LOOKBACK,
        "lookback_skip":      config.MOMENTUM_SHORT_LOOKBACK,
    }
    # TSMOM fields (board vote 2026-04-22, 17-0) — logged + used for vol-scaling.
    # Scoring activation gated on 90-day live log + CPCV backtest (Gate 3).
    _ewma_vol   = calculate_ewma_vol_60d(df)
    _tsmom_12m  = calculate_tsmom(df, 252)
    _tsmom_6m   = calculate_tsmom(df, 126)
    _tsmom_mult = None
    if _ewma_vol and _ewma_vol > 0:
        _raw = config.TSMOM_TARGET_VOL / _ewma_vol
        _tsmom_mult = round(
            min(max(_raw, config.TSMOM_VOL_MULT_FLOOR), config.TSMOM_VOL_MULT_CAP), 3
        )
    summary.update({
        "ewma_vol_60d":    _ewma_vol,
        "tsmom_12m":       round(_tsmom_12m * 100, 2) if _tsmom_12m is not None else None,
        "tsmom_6m":        round(_tsmom_6m  * 100, 2) if _tsmom_6m  is not None else None,
        "tsmom_direction": (1 if _tsmom_12m > 0 else -1) if _tsmom_12m is not None else None,
        "tsmom_vol_mult":  _tsmom_mult,
    })
    return summary
