"""
indicators/vwap.py
VWAP calculation — resets daily. Used as fair value / entry filter.
"""

import pandas as pd
import numpy as np
import config


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add VWAP to DataFrame. Resets at the start of each trading day.
    Adds 'vwap' column.
    """
    df = df.copy()

    # Typical price
    df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3

    # Group by date for daily reset
    if hasattr(df.index, 'date'):
        dates = pd.Series(df.index.date, index=df.index)
    else:
        dates = pd.Series(df.index, index=df.index)

    vwap_values = []
    for date, group_idx in df.groupby(dates).groups.items():
        group = df.loc[group_idx]
        cumulative_tp_vol = (group["typical_price"] * group["volume"]).cumsum()
        cumulative_vol    = group["volume"].cumsum()
        vwap              = cumulative_tp_vol / cumulative_vol.replace(0, np.nan)
        vwap_values.append(vwap)

    df["vwap"] = pd.concat(vwap_values).sort_index()
    df.drop(columns=["typical_price"], inplace=True)
    return df


def price_near_vwap(df: pd.DataFrame) -> bool:
    """True if current price is within VWAP_PROXIMITY_PCT of VWAP."""
    row = df.iloc[-1]
    if pd.isna(row.get("vwap")):
        return False
    pct_from_vwap = abs(row["close"] - row["vwap"]) / row["vwap"]
    return pct_from_vwap <= config.VWAP_PROXIMITY_PCT


def price_above_vwap(df: pd.DataFrame) -> bool:
    """True if price is above VWAP — bullish intraday bias."""
    row = df.iloc[-1]
    return row["close"] > row.get("vwap", float("inf"))


def price_below_vwap(df: pd.DataFrame) -> bool:
    """True if price is below VWAP — bearish intraday bias."""
    row = df.iloc[-1]
    return row["close"] < row.get("vwap", 0)


def vwap_reclaim(df: pd.DataFrame) -> bool:
    """
    True if price just reclaimed VWAP from below.
    Classic long entry signal after a dip.
    """
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return prev["close"] < prev.get("vwap", float("inf")) and curr["close"] > curr.get("vwap", 0)


def get_vwap_summary(df: pd.DataFrame) -> dict:
    row = df.iloc[-1]
    vwap = row.get("vwap")
    price = row["close"]
    return {
        "vwap": round(float(vwap), 4) if vwap and not pd.isna(vwap) else None,
        "price": price,
        "above_vwap":  price_above_vwap(df),
        "near_vwap":   price_near_vwap(df),
        "vwap_reclaim": vwap_reclaim(df),
        "pct_from_vwap": round(abs(price - vwap) / vwap * 100, 3) if vwap else None,
    }
