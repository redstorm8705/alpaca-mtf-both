"""
indicators/rsi.py
RSI with overbought/oversold filtering and divergence detection.
"""

import pandas as pd
import numpy as np
import config

def add_rsi(df: pd.DataFrame, period: int = None) -> pd.DataFrame:
    """Add RSI column to DataFrame."""
    df     = df.copy()
    period = period or config.RSI_PERIOD
    delta  = df["close"].diff()
    gain   = delta.clip(lower=0)
    loss   = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = np.where(avg_loss == 0, 100, 100 - (100 / (1 + rs)))
    return df

def rsi_in_range(df: pd.DataFrame, direction: str = "long") -> bool:
    """
    True if RSI is in a 'healthy' range for the given direction.
    Long: RSI between 40-70 (not overbought, has momentum)
    Short: RSI between 30-60 (not oversold, has downside)
    """
    rsi = df["rsi"].iloc[-1]
    if pd.isna(rsi):
        return False
    if direction == "long":
        return config.RSI_NEUTRAL_LOW <= rsi <= config.RSI_OVERBOUGHT
    else:
        return config.RSI_OVERSOLD <= rsi <= config.RSI_NEUTRAL_HIGH

def rsi_oversold(df: pd.DataFrame) -> bool:
    """True if RSI is in oversold territory (potential long reversal)."""
    return df["rsi"].iloc[-1] < config.RSI_OVERSOLD

def rsi_overbought(df: pd.DataFrame) -> bool:
    """True if RSI is in overbought territory (potential short reversal)."""
    return df["rsi"].iloc[-1] > config.RSI_OVERBOUGHT

def rsi_bullish_divergence(df: pd.DataFrame, lookback: int = 20) -> bool:
    """
    Detects bullish divergence: price making lower lows while RSI makes higher lows.
    Classic reversal signal.
    """
    if len(df) < lookback or "rsi" not in df.columns:
        return False

    window = df.tail(lookback)
    price_low1 = window["close"].iloc[-1]
    price_low2 = window["close"].min()

    rsi_at_price_low2 = window.loc[window["close"].idxmin(), "rsi"]
    rsi_now           = window["rsi"].iloc[-1]

    # Price at lower low, RSI at higher low
    return price_low1 <= price_low2 * 1.005 and rsi_now > rsi_at_price_low2

def rsi_bearish_divergence(df: pd.DataFrame, lookback: int = 20) -> bool:
    """
    Detects bearish divergence: price making higher highs while RSI makes lower highs.
    """
    if len(df) < lookback or "rsi" not in df.columns:
        return False

    window = df.tail(lookback)
    price_high1 = window["close"].iloc[-1]
    price_high2 = window["close"].max()

    rsi_at_price_high2 = window.loc[window["close"].idxmax(), "rsi"]
    rsi_now            = window["rsi"].iloc[-1]

    return price_high1 >= price_high2 * 0.995 and rsi_now < rsi_at_price_high2

def get_rsi_summary(df: pd.DataFrame) -> dict:
    rsi = df["rsi"].iloc[-1] if "rsi" in df.columns else None
    return {
        "rsi": round(rsi, 2) if rsi else None,
        "overbought": rsi_overbought(df) if rsi else False,
        "oversold":   rsi_oversold(df)   if rsi else False,
        "healthy_long":  rsi_in_range(df, "long")  if rsi else False,
        "healthy_short": rsi_in_range(df, "short") if rsi else False,
    }
