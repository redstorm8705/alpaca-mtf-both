"""
indicators/moving_averages.py
Calculates all MAs used in the strategy: 13EMA, 30EMA, 20SMA, 150SMA, 200SMA, 325SMA.
"""

import pandas as pd
import config


def add_all_mas(df: pd.DataFrame) -> pd.DataFrame:
    """Add all moving averages to a DataFrame. Returns df with new columns."""
    df = df.copy()

    # EMAs
    df[f"ema_{config.EMA_FAST}"]  = df["close"].ewm(span=config.EMA_FAST, adjust=False).mean()
    df[f"ema_{config.EMA_SLOW}"]  = df["close"].ewm(span=config.EMA_SLOW, adjust=False).mean()

    # SMAs
    df[f"sma_{config.SMA_20}"]    = df["close"].rolling(config.SMA_20).mean()
    df[f"sma_{config.SMA_150}"]   = df["close"].rolling(config.SMA_150).mean()
    df[f"sma_{config.SMA_200}"]   = df["close"].rolling(config.SMA_200).mean()
    df[f"sma_{config.SMA_325}"]   = df["close"].rolling(config.SMA_325).mean()

    return df


def ema_bullish_cross(df: pd.DataFrame) -> bool:
    """True if 13 EMA just crossed above 30 EMA (current bar)."""
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    ema_f = f"ema_{config.EMA_FAST}"
    ema_s = f"ema_{config.EMA_SLOW}"
    return prev[ema_f] <= prev[ema_s] and curr[ema_f] > curr[ema_s]


def ema_bearish_cross(df: pd.DataFrame) -> bool:
    """True if 13 EMA just crossed below 30 EMA (current bar)."""
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    ema_f = f"ema_{config.EMA_FAST}"
    ema_s = f"ema_{config.EMA_SLOW}"
    return prev[ema_f] >= prev[ema_s] and curr[ema_f] < curr[ema_s]


def ema_structure_bullish(df: pd.DataFrame) -> bool:
    """True if 13 EMA is currently above 30 EMA (trend is up)."""
    row = df.iloc[-1]
    return row[f"ema_{config.EMA_FAST}"] > row[f"ema_{config.EMA_SLOW}"]


def above_sma(df: pd.DataFrame, period: int) -> bool:
    """True if close is above the given SMA on the last bar."""
    col = f"sma_{period}"
    if col not in df.columns or pd.isna(df.iloc[-1][col]):
        return False
    return df.iloc[-1]["close"] > df.iloc[-1][col]


def get_ma_summary(df: pd.DataFrame) -> dict:
    """Return a snapshot dict of current MA values and price relationships."""
    row = df.iloc[-1]
    price = row["close"]
    return {
        "price": price,
        "ema_13": row.get(f"ema_{config.EMA_FAST}"),
        "ema_30": row.get(f"ema_{config.EMA_SLOW}"),
        "sma_20": row.get(f"sma_{config.SMA_20}"),
        "sma_150": row.get(f"sma_{config.SMA_150}"),
        "sma_200": row.get(f"sma_{config.SMA_200}"),
        "sma_325": row.get(f"sma_{config.SMA_325}"),
        "above_150": above_sma(df, config.SMA_150),
        "above_200": above_sma(df, config.SMA_200),
        "above_325": above_sma(df, config.SMA_325),
        "ema_bullish_structure": ema_structure_bullish(df),
    }
