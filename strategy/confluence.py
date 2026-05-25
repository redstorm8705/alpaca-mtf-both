"""
strategy/confluence.py
Multi-timeframe confluence scorer.
Each condition earns points. Minimum score required to generate a signal.
The key insight: a signal that aligns on 15m, 1H, and daily is far stronger than
one on a single TF.
"""

import logging
import pandas as pd
import config

from indicators.moving_averages import add_all_mas, ema_structure_bullish
from indicators.macd import add_both_macds, dual_macd_agreement
from indicators.rsi import add_rsi, rsi_in_range
from indicators.vwap import add_vwap, price_near_vwap, price_above_vwap, vwap_reclaim
from strategy.trend_filter import get_daily_bias, swing_bias_score
from indicators.momentum import momentum_bullish, momentum_bearish

logger = logging.getLogger("confluence")


def prepare_df(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicators to a DataFrame."""
    df = add_all_mas(df)
    df = add_both_macds(df)
    df = add_rsi(df)
    df = add_vwap(df)
    return df


def score_long_signal(
    symbol: str,
    tf_data: dict,          # {timeframe_str: prepared_df}
    trade_mode: str,        # TradeMode.INTRADAY or TradeMode.SWING
) -> dict:
    """
    Score a potential long entry across multiple timeframes.

    Returns:
        {
            "symbol": str,
            "score": int,
            "max_score": int,
            "signal": bool,
            "conditions": dict,   # which conditions passed
            "bias": str,
            "trade_mode": str,
        }
    """
    conditions = {}
    score = 0
    weights = config.SCORE_WEIGHTS

    # ── 1. DAILY TREND FILTER (highest weight) ───────────────────────────────
    daily_df = tf_data.get(config.TF_DAILY)
    if daily_df is not None and len(daily_df) > config.SMA_200:
        bias = get_daily_bias(daily_df)
        conditions["daily_above_150sma"] = bias["above_150"]
        conditions["daily_above_200sma"] = bias["above_200"]
        if bias["above_150"]:
            score += weights["daily_above_150sma"]
        if bias["above_200"]:
            score += weights["daily_above_200sma"]
        daily_bias_str = bias["bias"]
        long_allowed   = bias["long_allowed"]
    else:
        conditions["daily_above_150sma"] = False
        conditions["daily_above_200sma"] = False
        daily_bias_str = "unknown"
        # fail-closed: no daily data = no entry (board vote Apr 2026)
        long_allowed   = False
        logger.warning(f"[{symbol}] No daily data — long entry blocked (fail-closed)")

    # ── 2. EMA STRUCTURE (entry timeframe) ───────────────────────────────────
    entry_tf = (
        config.TF_15M if trade_mode == config.TradeMode.INTRADAY else config.TF_4H
    )
    entry_df = tf_data.get(entry_tf)
    if entry_df is not None:
        conditions["ema13_above_ema30"] = ema_structure_bullish(entry_df)
        if conditions["ema13_above_ema30"]:
            score += weights["ema13_above_ema30"]
    else:
        conditions["ema13_above_ema30"] = False

    # ── 3. MACD CONFLUENCE ───────────────────────────────────────────────────
    # Check MACD on entry TF + confirmation TF
    confirm_tf = (
        config.TF_1H if trade_mode == config.TradeMode.INTRADAY else config.TF_DAILY
    )
    confirm_df = tf_data.get(confirm_tf)

    macd_score = 0
    if entry_df is not None:
        entry_macd = dual_macd_agreement(entry_df, "long")
        macd_score += entry_macd["agreement_score"]

    if confirm_df is not None:
        confirm_macd = dual_macd_agreement(confirm_df, "long")
        macd_score += confirm_macd["agreement_score"]

    # Map raw MACD agreement to a single point (threshold: at least 2 agreements)
    conditions["macd_bullish_cross"] = macd_score >= 2
    if conditions["macd_bullish_cross"]:
        score += weights["macd_bullish_cross"]

    # ── 4. RSI / VOLUME_CONFIRMED ─────────────────────────────────────────────
    # Shadow (VOLUME_CONFIRMATION_ENABLED=False): RSI scores + volume logged only.
    # Live  (VOLUME_CONFIRMATION_ENABLED=True):   volume_confirmed scores, RSI retired.
    if config.VOLUME_CONFIRMATION_ENABLED:
        # ── LIVE: score volume_confirmed ─────────────────────────────────────
        if symbol in config.BUCKET_A_TICKERS:
            # Bucket A auto-pass: high-velocity leveraged ETFs (daily volume unreliable)
            # C1 TODO: suspend under MRI >= STRESSED+ (requires MRI level threading)
            conditions["volume_confirmed"] = True
            score += weights.get("volume_confirmed", 1)
        elif daily_df is not None:
            # iloc[-21:-1] excludes today's partial bar; ascending by Alpaca T1
            _vol_series = daily_df["volume"].iloc[-21:-1].dropna()
            if len(_vol_series) >= config.VOLUME_MIN_VALID_BARS:
                _avg_vol_20d = float(_vol_series.mean())
                _cur_vol = float(daily_df["volume"].iloc[-1])
                _vol_ratio = (
                    _cur_vol / _avg_vol_20d if _avg_vol_20d > 0.0 else 0.0
                )
                _bar_age_min = None
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    import datetime as _dt
                    _bar_ts = daily_df.index[-1]
                    _now_et = _dt.datetime.now(_ZI("America/New_York"))
                    if getattr(_bar_ts, "tzinfo", None) is not None:
                        _secs = (_now_et - _bar_ts).total_seconds()
                        _bar_age_min = round(_secs / 60.0, 1)
                except Exception as _bae:
                    logger.debug(f"[{symbol}] bar_age_min compute failed: {_bae}")
                conditions["volume_confirmed"] = (
                    _vol_ratio >= config.VOLUME_THRESHOLD
                )
                if conditions["volume_confirmed"]:
                    score += weights.get("volume_confirmed", 1)
                logger.info(
                    f"[{symbol}] VOLCFM long: vol_ratio={_vol_ratio:.2f} "
                    f"avg={_avg_vol_20d:.0f} cur={_cur_vol:.0f} "
                    f"pass={conditions['volume_confirmed']} "
                    f"bar_age_min={_bar_age_min}"
                )
            else:
                conditions["volume_confirmed"] = False
                logger.warning(
                    f"[{symbol}] VOLCFM long: only {len(_vol_series)} valid bars "
                    f"(need {config.VOLUME_MIN_VALID_BARS}) — condition skipped"
                )
        else:
            conditions["volume_confirmed"] = False
            logger.debug(
                f"[{symbol}] VOLCFM long: no daily data — volume gate skipped"
            )
    else:
        # ── SHADOW: RSI scores (zero behavior change), volume logged only ─────
        _score_before_c4 = score   # pre-C4 score for shadow comparison log
        if entry_df is not None:
            conditions["rsi_in_range"] = rsi_in_range(entry_df, "long")
            if conditions["rsi_in_range"]:
                score += weights["rsi_in_range"]
        else:
            conditions["rsi_in_range"] = False
        # Shadow observability (R1 Majors): compute volume, log JSON, zero score impact
        if daily_df is not None and symbol not in config.BUCKET_A_TICKERS:
            try:
                _vol_series = daily_df["volume"].iloc[-21:-1].dropna()
                if len(_vol_series) >= config.VOLUME_MIN_VALID_BARS:
                    _avg_vol_20d = float(_vol_series.mean())
                    _cur_vol = float(daily_df["volume"].iloc[-1])
                    _raw = (
                        _cur_vol / _avg_vol_20d if _avg_vol_20d > 0.0 else 0.0
                    )
                    _vol_ratio = round(_raw, 3)
                    _would_pass = _vol_ratio >= config.VOLUME_THRESHOLD
                    _bar_age_min = None
                    try:
                        from zoneinfo import ZoneInfo as _ZI
                        import datetime as _dt
                        _bar_ts = daily_df.index[-1]
                        _now_et = _dt.datetime.now(_ZI("America/New_York"))
                        if getattr(_bar_ts, "tzinfo", None) is not None:
                            _secs = (_now_et - _bar_ts).total_seconds()
                            _bar_age_min = round(_secs / 60.0, 1)
                    except Exception as _bae:
                        logger.debug(f"[{symbol}] bar_age_min compute failed: {_bae}")
                    logger.info(
                        f"[{symbol}] VOLSHADOW "
                        + __import__("json").dumps({
                            "direction": "long",
                            "vol_ratio": _vol_ratio,
                            "avg_vol_20d": round(_avg_vol_20d, 0),
                            "cur_vol": round(_cur_vol, 0),
                            "would_pass": bool(_would_pass),
                            "bucket": "B",
                            "score_without_vol": _score_before_c4,
                            "rsi_would_score": bool(conditions.get(
                                "rsi_in_range", False
                            )),
                            "bar_age_min": _bar_age_min,
                            "valid_bars": len(_vol_series),
                        })
                    )
            except Exception as _e:
                logger.warning(
                    f"[{symbol}] VOLSHADOW long compute error: {_e}"
                )

    # ── 5. VWAP (intraday only) ───────────────────────────────────────────────
    if trade_mode == config.TradeMode.INTRADAY and entry_df is not None:
        conditions["price_near_vwap"] = (
            price_near_vwap(entry_df) or
            price_above_vwap(entry_df) or
            vwap_reclaim(entry_df)
        )
        if conditions["price_near_vwap"]:
            score += weights["price_near_vwap"]
    else:
        conditions["price_near_vwap"] = True   # Not required for swings
        score += weights["price_near_vwap"]    # Give free point on swings

    # ── 6. MOMENTUM FILTER (Jegadeesh-Titman 12-1 month) ─────────────────────
    # 30+ years peer-reviewed evidence. Uses daily data, +2 weight (equal to SMA)
    # Requires 273 daily bars — only fires when sufficient history available
    _min_mom_bars = config.MOMENTUM_LONG_LOOKBACK + config.MOMENTUM_SHORT_LOOKBACK
    if daily_df is not None and len(daily_df) >= _min_mom_bars:
        conditions["momentum_12_1"] = momentum_bullish(daily_df)
        if conditions["momentum_12_1"]:
            score += weights.get("momentum_12_1", 2)
    else:
        conditions["momentum_12_1"] = False  # insufficient history — skip

    # ── SWING BONUS CHECK ────────────────────────────────────────────────────
    swing_bonus = 0
    if trade_mode == config.TradeMode.SWING and daily_df is not None:
        swing_bonus = swing_bias_score(daily_df)

    # ── FINAL SIGNAL DECISION ────────────────────────────────────────────────
    min_score   = config.MIN_LONG_SCORE
    if trade_mode == config.TradeMode.SWING:
        min_score += config.SWING_SCORE_BONUS_REQUIRED

    max_possible = sum(weights.values())
    signal = score >= min_score and long_allowed

    return {
        "symbol":      symbol,
        "direction":   "long",
        "score":       score,
        "max_score":   max_possible,
        "signal":      signal,
        "conditions":  conditions,
        "bias":        daily_bias_str,
        "swing_bonus": swing_bonus,
        "trade_mode":  trade_mode,
        "macd_agreement": macd_score,
    }


def score_short_signal(
    symbol: str,
    tf_data: dict,
    trade_mode: str,
) -> dict:
    """Mirror of score_long_signal for short entries."""
    conditions = {}
    score = 0
    weights = config.SCORE_WEIGHTS

    daily_df = tf_data.get(config.TF_DAILY)
    if daily_df is not None and len(daily_df) > config.SMA_200:
        bias = get_daily_bias(daily_df)
        conditions["daily_above_150sma"] = not bias["above_150"]
        conditions["daily_above_200sma"] = not bias["above_200"]
        if not bias["above_150"]:
            score += weights["daily_above_150sma"]
        if not bias["above_200"]:
            score += weights["daily_above_200sma"]
        daily_bias_str = bias["bias"]
        short_allowed  = bias["short_allowed"]
    else:
        conditions["daily_above_150sma"] = False
        conditions["daily_above_200sma"] = False
        daily_bias_str = "unknown"
        # fail-closed: no daily data = no entry (board vote Apr 2026)
        short_allowed  = False
        logger.warning(f"[{symbol}] No daily data — short entry blocked (fail-closed)")

    entry_tf = (
        config.TF_15M if trade_mode == config.TradeMode.INTRADAY else config.TF_4H
    )
    entry_df = tf_data.get(entry_tf)

    if entry_df is not None:
        conditions["ema13_above_ema30"] = not ema_structure_bullish(entry_df)
        if conditions["ema13_above_ema30"]:
            score += weights["ema13_above_ema30"]
    else:
        conditions["ema13_above_ema30"] = False

    confirm_tf = (
        config.TF_1H if trade_mode == config.TradeMode.INTRADAY else config.TF_DAILY
    )
    confirm_df = tf_data.get(confirm_tf)
    macd_score = 0
    if entry_df is not None:
        entry_macd = dual_macd_agreement(entry_df, "short")
        macd_score += entry_macd["agreement_score"]
    if confirm_df is not None:
        confirm_macd = dual_macd_agreement(confirm_df, "short")
        macd_score += confirm_macd["agreement_score"]

    conditions["macd_bullish_cross"] = macd_score >= 2
    if conditions["macd_bullish_cross"]:
        score += weights["macd_bullish_cross"]

    # ── 4. RSI / VOLUME_CONFIRMED (short mirror) ──────────────────────────────
    if config.VOLUME_CONFIRMATION_ENABLED:
        if symbol in config.BUCKET_A_TICKERS:
            conditions["volume_confirmed"] = True
            score += weights.get("volume_confirmed", 1)
        elif daily_df is not None:
            _vol_series = daily_df["volume"].iloc[-21:-1].dropna()
            if len(_vol_series) >= config.VOLUME_MIN_VALID_BARS:
                _avg_vol_20d = float(_vol_series.mean())
                _cur_vol = float(daily_df["volume"].iloc[-1])
                _vol_ratio = (
                    _cur_vol / _avg_vol_20d if _avg_vol_20d > 0.0 else 0.0
                )
                _bar_age_min = None
                try:
                    from zoneinfo import ZoneInfo as _ZI
                    import datetime as _dt
                    _bar_ts = daily_df.index[-1]
                    _now_et = _dt.datetime.now(_ZI("America/New_York"))
                    if getattr(_bar_ts, "tzinfo", None) is not None:
                        _secs = (_now_et - _bar_ts).total_seconds()
                        _bar_age_min = round(_secs / 60.0, 1)
                except Exception as _bae:
                    logger.debug(f"[{symbol}] bar_age_min compute failed: {_bae}")
                conditions["volume_confirmed"] = (
                    _vol_ratio >= config.VOLUME_THRESHOLD
                )
                if conditions["volume_confirmed"]:
                    score += weights.get("volume_confirmed", 1)
                logger.info(
                    f"[{symbol}] VOLCFM short: vol_ratio={_vol_ratio:.2f} "
                    f"avg={_avg_vol_20d:.0f} cur={_cur_vol:.0f} "
                    f"pass={conditions['volume_confirmed']} "
                    f"bar_age_min={_bar_age_min}"
                )
            else:
                conditions["volume_confirmed"] = False
                logger.warning(
                    f"[{symbol}] VOLCFM short: only {len(_vol_series)} valid bars "
                    f"(need {config.VOLUME_MIN_VALID_BARS}) — condition skipped"
                )
        else:
            conditions["volume_confirmed"] = False
            logger.debug(
                f"[{symbol}] VOLCFM short: no daily data — volume gate skipped"
            )
    else:
        _score_before_c4 = score
        if entry_df is not None:
            conditions["rsi_in_range"] = rsi_in_range(entry_df, "short")
            if conditions["rsi_in_range"]:
                score += weights["rsi_in_range"]
        else:
            conditions["rsi_in_range"] = False
        if daily_df is not None and symbol not in config.BUCKET_A_TICKERS:
            try:
                _vol_series = daily_df["volume"].iloc[-21:-1].dropna()
                if len(_vol_series) >= config.VOLUME_MIN_VALID_BARS:
                    _avg_vol_20d = float(_vol_series.mean())
                    _cur_vol = float(daily_df["volume"].iloc[-1])
                    _raw = (
                        _cur_vol / _avg_vol_20d if _avg_vol_20d > 0.0 else 0.0
                    )
                    _vol_ratio = round(_raw, 3)
                    _would_pass = _vol_ratio >= config.VOLUME_THRESHOLD
                    _bar_age_min = None
                    try:
                        from zoneinfo import ZoneInfo as _ZI
                        import datetime as _dt
                        _bar_ts = daily_df.index[-1]
                        _now_et = _dt.datetime.now(_ZI("America/New_York"))
                        if getattr(_bar_ts, "tzinfo", None) is not None:
                            _secs = (_now_et - _bar_ts).total_seconds()
                            _bar_age_min = round(_secs / 60.0, 1)
                    except Exception as _bae:
                        logger.debug(f"[{symbol}] bar_age_min compute failed: {_bae}")
                    logger.info(
                        f"[{symbol}] VOLSHADOW "
                        + __import__("json").dumps({
                            "direction": "short",
                            "vol_ratio": _vol_ratio,
                            "avg_vol_20d": round(_avg_vol_20d, 0),
                            "cur_vol": round(_cur_vol, 0),
                            "would_pass": bool(_would_pass),
                            "bucket": "B",
                            "score_without_vol": _score_before_c4,
                            "rsi_would_score": bool(conditions.get(
                                "rsi_in_range", False
                            )),
                            "bar_age_min": _bar_age_min,
                            "valid_bars": len(_vol_series),
                        })
                    )
            except Exception as _e:
                logger.warning(
                    f"[{symbol}] VOLSHADOW short compute error: {_e}"
                )

    if trade_mode == config.TradeMode.INTRADAY and entry_df is not None:
        conditions["price_near_vwap"] = not price_above_vwap(entry_df)
        if conditions["price_near_vwap"]:
            score += weights["price_near_vwap"]
    else:
        conditions["price_near_vwap"] = True
        score += weights["price_near_vwap"]

    # ── MOMENTUM FILTER (short) ──────────────────────────────────────────────
    _min_mom_bars = config.MOMENTUM_LONG_LOOKBACK + config.MOMENTUM_SHORT_LOOKBACK
    if daily_df is not None and len(daily_df) >= _min_mom_bars:
        conditions["momentum_12_1"] = momentum_bearish(daily_df)
        if conditions["momentum_12_1"]:
            score += weights.get("momentum_12_1", 2)
    else:
        conditions["momentum_12_1"] = False

    min_score = config.MIN_SHORT_SCORE
    if trade_mode == config.TradeMode.SWING:
        min_score += config.SWING_SCORE_BONUS_REQUIRED

    max_possible = sum(weights.values())
    signal = score >= min_score and short_allowed

    return {
        "symbol":    symbol,
        "direction": "short",
        "score":     score,
        "max_score": max_possible,
        "signal":    signal,
        "conditions": conditions,
        "bias":       daily_bias_str,
        "trade_mode": trade_mode,
        "macd_agreement": macd_score,
    }
