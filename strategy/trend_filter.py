"""
strategy/trend_filter.py
Daily bias engine — determines whether the bot is allowed to go long, short, or stay flat.
Uses 150 SMA, 200 SMA, 325 SMA on the daily chart.
This is the highest-level filter. If daily says no, no trade fires regardless of lower TF signals.
"""

import pandas as pd
import config
from indicators.moving_averages import add_all_mas, above_sma


class TrendBias:
    STRONG_BULL = "strong_bull"   # Above 150, 200, 325
    BULL        = "bull"          # Above 150 and 200
    NEUTRAL     = "neutral"       # Mixed signals
    BEAR        = "bear"          # Below 150 and 200
    STRONG_BEAR = "strong_bear"   # Below 150, 200, 325


def get_daily_bias(daily_df: pd.DataFrame) -> dict:
    """
    Analyze daily chart and return trend bias + metadata.

    Returns:
        {
            "bias": TrendBias string,
            "long_allowed": bool,
            "short_allowed": bool,
            "above_150": bool,
            "above_200": bool,
            "above_325": bool,
            "ma_stack": str   # describes MA alignment
        }
    """
    df = add_all_mas(daily_df)

    above_150 = above_sma(df, config.SMA_150)
    above_200 = above_sma(df, config.SMA_200)
    above_325 = above_sma(df, config.SMA_325)

    # Determine bias
    if above_150 and above_200 and above_325:
        bias = TrendBias.STRONG_BULL
    elif above_150 and above_200:
        bias = TrendBias.BULL
    elif not above_150 and not above_200 and not above_325:
        bias = TrendBias.STRONG_BEAR
    elif not above_150 and not above_200:
        bias = TrendBias.BEAR
    else:
        bias = TrendBias.NEUTRAL

    # Trading permissions
    long_allowed  = bias in [TrendBias.STRONG_BULL, TrendBias.BULL, TrendBias.NEUTRAL]
    short_allowed = bias in [TrendBias.STRONG_BEAR, TrendBias.BEAR, TrendBias.NEUTRAL]

    # MA stack description
    row = df.iloc[-1]
    ma_150 = row.get(f"sma_{config.SMA_150}")
    ma_200 = row.get(f"sma_{config.SMA_200}")
    ma_325 = row.get(f"sma_{config.SMA_325}")
    price  = row["close"]

    layers = []
    if ma_325 and price > ma_325: layers.append(f"P>{config.SMA_325}sma")
    if ma_200 and price > ma_200: layers.append(f"P>{config.SMA_200}sma")
    if ma_150 and price > ma_150: layers.append(f"P>{config.SMA_150}sma")

    return {
        "bias":          bias,
        "long_allowed":  long_allowed,
        "short_allowed": short_allowed,
        "above_150":     above_150,
        "above_200":     above_200,
        "above_325":     above_325,
        "ma_stack":      " | ".join(layers) if layers else "below all MAs",
        "price":         price,
    }


def swing_bias_score(daily_df: pd.DataFrame) -> int:
    """
    Returns a 0-3 score for swing trade bias strength.
    Used as a bonus requirement for swing signals.
    """
    bias_data = get_daily_bias(daily_df)
    score = 0
    if bias_data["above_150"]: score += 1
    if bias_data["above_200"]: score += 1
    if bias_data["above_325"]: score += 1
    return score
