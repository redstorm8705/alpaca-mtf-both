# ruff: noqa: E501
"""
indicators/macd.py
MACD with dual settings: Standard (12/26/9) for swings, Fast (3/15/3) for intraday.
Both are calculated and compared — agreement between them strengthens signal.
"""

import pandas as pd
import config


def _calculate_macd(df: pd.DataFrame, fast: int, slow: int, signal: int) -> pd.DataFrame:
    """Core MACD calculation. Returns df with macd, signal, histogram columns."""
    df = df.copy()
    ema_fast   = df["close"].ewm(span=fast,   adjust=False).mean()
    ema_slow   = df["close"].ewm(span=slow,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal_line= macd_line.ewm(span=signal,   adjust=False).mean()
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram


def add_macd(df: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """Add MACD columns to df using provided settings dict."""
    df   = df.copy()
    lbl  = settings["label"]
    macd, sig, hist = _calculate_macd(
        df, settings["fast"], settings["slow"], settings["signal"]
    )
    df[f"{lbl}_line"]      = macd
    df[f"{lbl}_signal"]    = sig
    df[f"{lbl}_histogram"] = hist
    return df


def add_both_macds(df: pd.DataFrame) -> pd.DataFrame:
    """Add both standard and fast MACD to df."""
    df = add_macd(df, config.MACD_STANDARD)
    df = add_macd(df, config.MACD_FAST)
    return df


def macd_bullish_cross(df: pd.DataFrame, settings: dict) -> bool:
    """True if MACD line just crossed above signal line (histogram flipped positive)."""
    if len(df) < 2:
        return False
    lbl  = settings["label"]
    hist = f"{lbl}_histogram"
    return df[hist].iloc[-2] <= 0 and df[hist].iloc[-1] > 0


def macd_bearish_cross(df: pd.DataFrame, settings: dict) -> bool:
    """True if MACD line just crossed below signal line (histogram flipped negative)."""
    if len(df) < 2:
        return False
    lbl  = settings["label"]
    hist = f"{lbl}_histogram"
    return df[hist].iloc[-2] >= 0 and df[hist].iloc[-1] < 0


def macd_histogram_rising(df: pd.DataFrame, settings: dict) -> bool:
    """True if histogram is positive AND growing (momentum building)."""
    # AWP audit fix (2026-06-28): missing the same len(df) < 2 guard that
    # macd_bullish_cross()/macd_bearish_cross() (above) already have --
    # without it, a df with fewer than 2 rows raises IndexError on the
    # iloc[-2] access. Found during the strategy/confluence.py board redo:
    # dual_macd_agreement() calls this unconditionally for both entry_df and
    # confirm_df. Blast radius was already contained (caught by
    # signal_generator.py's _analyze_symbol_full() try/except, skipping
    # only the one symbol for one cycle), but this closes the gap at its
    # actual source instead of relying on exception-based recovery.
    if len(df) < 2:
        return False
    lbl  = settings["label"]
    hist = df[f"{lbl}_histogram"]
    return hist.iloc[-1] > 0 and hist.iloc[-1] > hist.iloc[-2]


def macd_histogram_falling(df: pd.DataFrame, settings: dict) -> bool:
    """True if histogram is negative AND falling (bearish momentum building)."""
    # AWP audit fix (2026-06-28): same missing-guard fix as
    # macd_histogram_rising() above.
    if len(df) < 2:
        return False
    lbl  = settings["label"]
    hist = df[f"{lbl}_histogram"]
    return hist.iloc[-1] < 0 and hist.iloc[-1] < hist.iloc[-2]


def dual_macd_agreement(df: pd.DataFrame, direction: str = "long") -> dict:
    """
    Compare Standard vs Fast MACD signals.
    Returns agreement score and details.
    direction: 'long' or 'short'
    """
    std_cross  = macd_bullish_cross(df, config.MACD_STANDARD) if direction == "long" else macd_bearish_cross(df, config.MACD_STANDARD)
    fast_cross = macd_bullish_cross(df, config.MACD_FAST)     if direction == "long" else macd_bearish_cross(df, config.MACD_FAST)
    std_rising  = macd_histogram_rising(df, config.MACD_STANDARD)  if direction == "long" else macd_histogram_falling(df, config.MACD_STANDARD)
    fast_rising = macd_histogram_rising(df, config.MACD_FAST)      if direction == "long" else macd_histogram_falling(df, config.MACD_FAST)

    agreement_score = sum([std_cross, fast_cross, std_rising, fast_rising])

    return {
        "std_cross":    std_cross,
        "fast_cross":   fast_cross,
        "std_momentum": std_rising,
        "fast_momentum":fast_rising,
        "agreement_score": agreement_score,   # 0-4: higher = stronger signal
        "both_agree":   (std_cross or std_rising) and (fast_cross or fast_rising),
    }


def get_macd_summary(df: pd.DataFrame) -> dict:
    """Quick snapshot of both MACDs on the last bar."""
    row = df.iloc[-1]
    return {
        "std_histogram":  row.get(f"{config.MACD_STANDARD['label']}_histogram"),
        "fast_histogram": row.get(f"{config.MACD_FAST['label']}_histogram"),
        "std_bullish":    (row.get(f"{config.MACD_STANDARD['label']}_histogram", 0) or 0) > 0,
        "fast_bullish":   (row.get(f"{config.MACD_FAST['label']}_histogram", 0) or 0) > 0,
    }
