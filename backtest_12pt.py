#!/usr/bin/env python3
"""
backtest_12pt.py — Standalone daily-bar backtest of the 12-point MTF confluence scoring system.

GUARDRAILS ENFORCED (applies to all plugins and Claude Code skills in this project):
  1. T1 Alpaca Data via data/fetcher.py (fetch_bars) — no yfinance for US equity/ETF OHLCV.
  2. Read-only access to all bot files. Does not modify any bot file.
  3. All output written to logs/ directory only.
  4. Does NOT import execution.broker, execution.risk_manager, or any Alpaca client directly.
  5. RTH-blocked: refuses to run weekdays between 9:30 AM and 4:00 PM ET.

Scoring system replicated faithfully from:
  - strategy/confluence.py  (score_long_signal / score_short_signal)
  - strategy/trend_filter.py (get_daily_bias → long_allowed / short_allowed)
  - indicators/macd.py       (dual_macd_agreement: cross + histogram_rising, 0-4 per TF)
  - config.py                (SCORE_WEIGHTS, paper profile params)

Daily-bar notes:
  - VWAP: not computable on daily bars → gives free 1pt (swing mode behavior, per confluence.py L113)
  - MACD: dual_macd_agreement run on daily bars only (entry+confirm TF collapse to daily)
  - Momentum: Jegadeesh-Titman 252-21 day lookback (unchanged)
  - Min score: 9/12 (paper profile MIN_LONG_SCORE / MIN_SHORT_SCORE)
  - Stop: 1.25x ATR(14), Target: 2.5x ATR(14) (paper profile intraday)

Usage:
  python3 backtest_12pt.py
"""

import sys
import json
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from data.fetcher import fetch_bars  # T1: Alpaca Data — sole approved bar source
import config

# ── Guardrail 5: Block during RTH on weekdays ─────────────────────────────────
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parent
_now = datetime.now(ET)
if _now.weekday() < 5:  # Monday–Friday
    _mins = _now.hour * 60 + _now.minute
    if (9 * 60 + 30) <= _mins < (16 * 60):
        print("BLOCKED: Cannot run during RTH hours (9:30 AM–4:00 PM ET / 6:30 AM–1:00 PM PT weekdays).")
        sys.exit(1)

# ── Guardrail 4: No execution.broker or Alpaca client imports ─────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Config constants mirrored from config.py PROFILES["paper"]
# ─────────────────────────────────────────────────────────────────────────────
STOP_ATR_MULT   = 1.25   # INTRADAY_STOP_ATR_MULT (paper)
TARGET_ATR_MULT = 2.50   # INTRADAY_TARGET_ATR_MULT (paper)
MIN_SCORE       = 9      # MIN_LONG_SCORE / MIN_SHORT_SCORE (paper)
ATR_PERIOD      = 14

# Scoring weights — from config.py SCORE_WEIGHTS
WEIGHTS = {
    "daily_above_150sma": 2,
    "daily_above_200sma": 2,
    "ema13_above_ema30":  2,
    "macd_bullish_cross": 2,
    "rsi_in_range":       1,
    "price_near_vwap":    1,   # free point on daily (swing mode, per confluence.py L113-114)
    "momentum_12_1":      2,
}
MAX_SCORE = sum(WEIGHTS.values())  # 12

# Indicator params — from config.py
EMA_FAST       = 13
EMA_SLOW       = 30
SMA_150        = 150
SMA_200        = 200
SMA_325        = 325
MACD_STD       = (12, 26, 9)   # MACD_STANDARD
MACD_FAST_CFG  = (3,  15, 3)   # MACD_FAST
RSI_PERIOD     = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

# Momentum lookback — from config.py
MOMENTUM_LONG_LB  = 252   # ~12 months
MOMENTUM_SHORT_LB = 21    # ~1 month skip

# Backtest params
TICKERS        = ["NVDA", "TSLA", "AAPL", "AMZN", "META", "SPY", "QQQ"]
FETCH_START    = "2024-01-01"   # enough history for 200 SMA + 273-bar momentum warmup
MAX_HOLD_DAYS  = 20             # max bars before forced exit at close
COOLDOWN_BARS  = 5              # bars to skip after an entry fires


# ─────────────────────────────────────────────────────────────────────────────
# Standalone indicator functions — no bot module imports
# ─────────────────────────────────────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h = df["High"]; l = df["Low"]; c = df["Close"].shift(1)
    tr = pd.concat([(h - l), (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd_components(series: pd.Series, fast: int, slow: int, signal: int):
    line = _ema(series, fast) - _ema(series, slow)
    sig  = _ema(line, signal)
    hist = line - sig
    return line, sig, hist


def _momentum_12_1(close: pd.Series) -> pd.Series:
    """Jegadeesh-Titman 12-1 month momentum (252 lookback, skip last 21 bars)."""
    past_far  = close.shift(MOMENTUM_LONG_LB + MOMENTUM_SHORT_LB)
    past_near = close.shift(MOMENTUM_SHORT_LB)
    return (past_near - past_far) / past_far.replace(0, np.nan)


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["Close"]

    df["sma150"]   = _sma(c, SMA_150)
    df["sma200"]   = _sma(c, SMA_200)
    df["sma325"]   = _sma(c, SMA_325)
    df["ema13"]    = _ema(c, EMA_FAST)
    df["ema30"]    = _ema(c, EMA_SLOW)
    df["atr14"]    = _atr(df, ATR_PERIOD)
    df["rsi14"]    = _rsi(c, RSI_PERIOD)
    df["mom_12_1"] = _momentum_12_1(c)

    ml, ms, mh = _macd_components(c, *MACD_STD)
    df["macd_std_line"] = ml
    df["macd_std_sig"]  = ms
    df["macd_std_hist"] = mh

    fl, fs, fh = _macd_components(c, *MACD_FAST_CFG)
    df["macd_fast_line"] = fl
    df["macd_fast_sig"]  = fs
    df["macd_fast_hist"] = fh

    return df


# ─────────────────────────────────────────────────────────────────────────────
# MACD dual agreement — replicates indicators/macd.py dual_macd_agreement()
# 4 conditions: std_cross, fast_cross, std_rising/falling, fast_rising/falling
# Confluence.py requires total >= 2 across both TFs (entry + confirm)
# On daily bars both TFs collapse to daily → single call, same bar
# ─────────────────────────────────────────────────────────────────────────────

def _macd_agreement_score(row: pd.Series, prev_row: pd.Series, direction: str) -> int:
    score = 0
    sh  = row["macd_std_hist"];  sp = prev_row["macd_std_hist"]
    fh  = row["macd_fast_hist"]; fp = prev_row["macd_fast_hist"]

    if any(pd.isna(v) for v in [sh, sp, fh, fp]):
        return 0

    if direction == "long":
        if sp <= 0 and sh > 0:          score += 1   # std cross bullish
        if fp <= 0 and fh > 0:          score += 1   # fast cross bullish
        if sh > 0 and sh > sp:          score += 1   # std histogram rising
        if fh > 0 and fh > fp:          score += 1   # fast histogram rising
    else:
        if sp >= 0 and sh < 0:          score += 1   # std cross bearish
        if fp >= 0 and fh < 0:          score += 1   # fast cross bearish
        if sh < 0 and sh < sp:          score += 1   # std histogram falling
        if fh < 0 and fh < fp:          score += 1   # fast histogram falling

    return score


# ─────────────────────────────────────────────────────────────────────────────
# Daily bias — replicates strategy/trend_filter.py get_daily_bias()
# ─────────────────────────────────────────────────────────────────────────────

def _daily_bias(row: pd.Series) -> dict:
    above_150 = (not pd.isna(row["sma150"])) and float(row["Close"]) > float(row["sma150"])
    above_200 = (not pd.isna(row["sma200"])) and float(row["Close"]) > float(row["sma200"])
    above_325 = (not pd.isna(row["sma325"])) and float(row["Close"]) > float(row["sma325"])

    if above_150 and above_200 and above_325:
        bias = "strong_bull"
    elif above_150 and above_200:
        bias = "bull"
    elif not above_150 and not above_200 and not above_325:
        bias = "strong_bear"
    elif not above_150 and not above_200:
        bias = "bear"
    else:
        bias = "neutral"

    return {
        "bias":          bias,
        "long_allowed":  bias in ("strong_bull", "bull", "neutral"),
        "short_allowed": bias in ("strong_bear", "bear", "neutral"),
        "above_150":     above_150,
        "above_200":     above_200,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 12-point scoring — replicates strategy/confluence.py
# ─────────────────────────────────────────────────────────────────────────────

def score_long(row: pd.Series, prev_row: pd.Series) -> dict:
    conds = {}
    score = 0
    db    = _daily_bias(row)

    conds["daily_above_150sma"] = db["above_150"]
    conds["daily_above_200sma"] = db["above_200"]
    if db["above_150"]: score += WEIGHTS["daily_above_150sma"]
    if db["above_200"]: score += WEIGHTS["daily_above_200sma"]

    if not (pd.isna(row["ema13"]) or pd.isna(row["ema30"])):
        conds["ema13_above_ema30"] = bool(row["ema13"] > row["ema30"])
    else:
        conds["ema13_above_ema30"] = False
    if conds["ema13_above_ema30"]: score += WEIGHTS["ema13_above_ema30"]

    macd_agree = _macd_agreement_score(row, prev_row, "long")
    conds["macd_bullish_cross"] = macd_agree >= 2
    if conds["macd_bullish_cross"]: score += WEIGHTS["macd_bullish_cross"]

    if not pd.isna(row["rsi14"]):
        conds["rsi_in_range"] = bool(RSI_OVERSOLD < float(row["rsi14"]) < RSI_OVERBOUGHT)
    else:
        conds["rsi_in_range"] = False
    if conds["rsi_in_range"]: score += WEIGHTS["rsi_in_range"]

    # VWAP: free 1pt on daily bars (swing mode per confluence.py L113-114)
    conds["price_near_vwap"] = True
    score += WEIGHTS["price_near_vwap"]

    if not pd.isna(row["mom_12_1"]):
        conds["momentum_12_1"] = bool(float(row["mom_12_1"]) > 0)
    else:
        conds["momentum_12_1"] = False
    if conds["momentum_12_1"]: score += WEIGHTS["momentum_12_1"]

    return {
        "score":        score,
        "signal":       score >= MIN_SCORE and db["long_allowed"],
        "conditions":   conds,
        "bias":         db["bias"],
        "macd_agree":   macd_agree,
        "long_allowed": db["long_allowed"],
    }


def score_short(row: pd.Series, prev_row: pd.Series) -> dict:
    conds = {}
    score = 0
    db    = _daily_bias(row)

    # Short: below SMAs = bearish structure scores points
    conds["daily_above_150sma"] = not db["above_150"]
    conds["daily_above_200sma"] = not db["above_200"]
    if not db["above_150"]: score += WEIGHTS["daily_above_150sma"]
    if not db["above_200"]: score += WEIGHTS["daily_above_200sma"]

    if not (pd.isna(row["ema13"]) or pd.isna(row["ema30"])):
        conds["ema13_above_ema30"] = bool(row["ema13"] < row["ema30"])
    else:
        conds["ema13_above_ema30"] = False
    if conds["ema13_above_ema30"]: score += WEIGHTS["ema13_above_ema30"]

    macd_agree = _macd_agreement_score(row, prev_row, "short")
    conds["macd_bullish_cross"] = macd_agree >= 2
    if conds["macd_bullish_cross"]: score += WEIGHTS["macd_bullish_cross"]

    if not pd.isna(row["rsi14"]):
        conds["rsi_in_range"] = bool(RSI_OVERSOLD < float(row["rsi14"]) < RSI_OVERBOUGHT)
    else:
        conds["rsi_in_range"] = False
    if conds["rsi_in_range"]: score += WEIGHTS["rsi_in_range"]

    conds["price_near_vwap"] = True
    score += WEIGHTS["price_near_vwap"]

    if not pd.isna(row["mom_12_1"]):
        conds["momentum_12_1"] = bool(float(row["mom_12_1"]) < 0)
    else:
        conds["momentum_12_1"] = False
    if conds["momentum_12_1"]: score += WEIGHTS["momentum_12_1"]

    return {
        "score":         score,
        "signal":        score >= MIN_SCORE and db["short_allowed"],
        "conditions":    conds,
        "bias":          db["bias"],
        "macd_agree":    macd_agree,
        "short_allowed": db["short_allowed"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Trade simulation
# Entry at next-bar open. Stop/target checked on each bar's High/Low.
# ─────────────────────────────────────────────────────────────────────────────

def simulate_trade(df: pd.DataFrame, entry_bar_idx: int, direction: str,
                   entry_price: float, atr: float) -> dict:
    r_unit       = STOP_ATR_MULT * atr
    target_dist  = TARGET_ATR_MULT * atr

    if direction == "long":
        stop_price   = entry_price - r_unit
        target_price = entry_price + target_dist
    else:
        stop_price   = entry_price + r_unit
        target_price = entry_price - target_dist

    future      = df.iloc[entry_bar_idx: entry_bar_idx + MAX_HOLD_DAYS]
    exit_price  = None
    exit_reason = "max_hold"
    hold_days   = len(future)

    for i, (_, bar) in enumerate(future.iterrows()):
        h = float(bar["High"])
        l = float(bar["Low"])

        if direction == "long":
            if l <= stop_price:
                exit_price = stop_price; exit_reason = "stop";   hold_days = i + 1; break
            if h >= target_price:
                exit_price = target_price; exit_reason = "target"; hold_days = i + 1; break
        else:
            if h >= stop_price:
                exit_price = stop_price; exit_reason = "stop";   hold_days = i + 1; break
            if l <= target_price:
                exit_price = target_price; exit_reason = "target"; hold_days = i + 1; break

    if exit_price is None:
        exit_price = float(future.iloc[-1]["Close"]) if len(future) > 0 else entry_price

    pnl    = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
    r_mult = pnl / r_unit if r_unit > 0 else 0.0

    return {
        "exit_price":   round(exit_price, 4),
        "exit_reason":  exit_reason,
        "hold_days":    hold_days,
        "pnl":          round(pnl, 4),
        "r_multiple":   round(r_mult, 4),
        "win":          r_mult > 0,
        "stop_price":   round(stop_price, 4),
        "target_price": round(target_price, 4),
        "r_unit":       round(r_unit, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-ticker backtest
# ─────────────────────────────────────────────────────────────────────────────

def backtest_ticker(symbol: str, df_full: pd.DataFrame,
                    backtest_start: pd.Timestamp) -> list:
    df = build_indicators(df_full)

    # Strip timezone from index for clean comparison
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    backtest_mask = df.index >= backtest_start
    backtest_idx  = df.index[backtest_mask].tolist()

    if len(backtest_idx) < 10:
        return []

    trades        = []
    cooldown_left = 0

    for date in backtest_idx:
        global_pos = df.index.get_loc(date)
        if global_pos < 1 or global_pos >= len(df) - 1:
            continue

        row      = df.iloc[global_pos]
        prev_row = df.iloc[global_pos - 1]

        # Require all critical indicators to be warmed up
        needed = ["sma200", "atr14", "mom_12_1", "macd_std_hist", "macd_fast_hist"]
        if any(pd.isna(row.get(c)) for c in needed):
            continue

        atr = float(row["atr14"])
        if atr <= 0:
            continue

        if cooldown_left > 0:
            cooldown_left -= 1
            continue

        long_r  = score_long(row, prev_row)
        short_r = score_short(row, prev_row)

        # Dedup: one direction per bar — pick the stronger signal
        if long_r["signal"] and short_r["signal"]:
            chosen = ("long", long_r) if long_r["score"] >= short_r["score"] else ("short", short_r)
        elif long_r["signal"]:
            chosen = ("long", long_r)
        elif short_r["signal"]:
            chosen = ("short", short_r)
        else:
            continue

        direction, result = chosen
        next_bar    = df.iloc[global_pos + 1]
        entry_price = float(next_bar["Open"])
        entry_date  = df.index[global_pos + 1]

        trade_result = simulate_trade(df, global_pos + 1, direction, entry_price, atr)

        trades.append({
            "symbol":       symbol,
            "direction":    direction,
            "signal_date":  date.strftime("%Y-%m-%d"),
            "entry_date":   entry_date.strftime("%Y-%m-%d"),
            "entry_price":  round(entry_price, 4),
            "atr_at_entry": round(atr, 4),
            "score":        result["score"],
            "max_score":    MAX_SCORE,
            "bias":         result["bias"],
            "macd_agree":   result["macd_agree"],
            "conditions":   result["conditions"],
            **trade_result,
        })

        cooldown_left = COOLDOWN_BARS

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(trades: list) -> dict:
    if not trades:
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate": None, "avg_r": None, "sharpe_ratio": None,
            "max_drawdown_r": None, "max_drawdown_pct": None,
            "signals_per_week": 0, "avg_hold_days": None,
            "exit_reasons": {}, "long_trades": 0, "short_trades": 0,
        }

    r_arr  = np.array([t["r_multiple"] for t in trades])
    wins   = int((r_arr > 0).sum())
    losses = int((r_arr <= 0).sum())

    avg_hold        = float(np.mean([t["hold_days"] for t in trades]))
    trades_per_year = 252 / max(avg_hold, 1)
    sharpe = float((r_arr.mean() / r_arr.std(ddof=1)) * np.sqrt(trades_per_year)) if r_arr.std(ddof=1) > 0 else 0.0

    cum_r     = np.cumsum(r_arr)
    peak      = np.maximum.accumulate(cum_r)
    max_dd_r  = float((cum_r - peak).min())

    avg_r_pct  = float(np.mean([t["r_unit"] / t["entry_price"] for t in trades if t["entry_price"] > 0]))
    max_dd_pct = abs(max_dd_r) * avg_r_pct * 100

    dates = sorted(t["signal_date"] for t in trades)
    if len(dates) >= 2:
        first_dt = datetime.strptime(dates[0], "%Y-%m-%d")
        last_dt  = datetime.strptime(dates[-1], "%Y-%m-%d")
        weeks    = max((last_dt - first_dt).days / 7, 1)
    else:
        weeks = 1
    sig_per_week = len(trades) / weeks

    exit_counts = {}
    for t in trades:
        exit_counts[t["exit_reason"]] = exit_counts.get(t["exit_reason"], 0) + 1

    return {
        "total_trades":     len(trades),
        "wins":             wins,
        "losses":           losses,
        "win_rate":         round(wins / len(trades), 4),
        "avg_r":            round(float(r_arr.mean()), 4),
        "sharpe_ratio":     round(sharpe, 4),
        "max_drawdown_r":   round(max_dd_r, 4),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "signals_per_week": round(sig_per_week, 2),
        "avg_hold_days":    round(avg_hold, 1),
        "exit_reasons":     exit_counts,
        "long_trades":      sum(1 for t in trades if t["direction"] == "long"),
        "short_trades":     sum(1 for t in trades if t["direction"] == "short"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    today_str      = datetime.now(PT).strftime("%Y-%m-%d")
    today_dt       = pd.Timestamp.now(tz="America/Los_Angeles").normalize()
    backtest_start = today_dt - pd.DateOffset(months=6)

    print("=" * 65)
    print("  12-POINT MTF CONFLUENCE BACKTEST")
    print("=" * 65)
    print(f"  Tickers:         {', '.join(TICKERS)}")
    print(f"  Window:          {backtest_start.strftime('%Y-%m-%d')} → {today_str}  (~6 months)")
    print(f"  Paper profile:   stop={STOP_ATR_MULT}x ATR | target={TARGET_ATR_MULT}x ATR | 1:{TARGET_ATR_MULT/STOP_ATR_MULT:.0f} R:R")
    print(f"  Min score:       {MIN_SCORE}/{MAX_SCORE}")
    print()

    # T1 — Alpaca Data via data/fetcher.py (CLAUDE.md Guardrail 1)
    # 650 bars covers ~2.6 years of trading days from today, well past FETCH_START 2024-01-01.
    # Warmup requirement: SMA_200(200) + momentum lookback(252+21) + buffer(10) = 483 bars.
    NUM_BARS_TO_FETCH = 650
    print(f"Fetching daily data ({FETCH_START} → {today_str}) via Alpaca T1 ({NUM_BARS_TO_FETCH} bars/ticker)...")
    raw = {}
    for _sym in TICKERS:
        print(f"  {_sym}...", end=" ", flush=True)
        _df = fetch_bars(_sym, config.TF_DAILY, num_bars=NUM_BARS_TO_FETCH)
        raw[_sym] = _df
        print(f"{len(_df)} bars" if not _df.empty else "NO DATA")
    print()

    all_trades     = []
    ticker_results = {}

    for symbol in TICKERS:
        print(f"  [{symbol}] scoring...", end=" ", flush=True)
        try:
            df_sym = raw.get(symbol, pd.DataFrame())
            if df_sym.empty:
                print(f"no data from Alpaca T1, skipping")
                ticker_results[symbol] = {"error": "no data from Alpaca T1"}
                continue

            # fetch_bars returns lowercase columns; rename for indicator functions
            df_sym = df_sym.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume"
            })

            df_sym.dropna(subset=["Close", "Open", "High", "Low"], inplace=True)
            df_sym.index = pd.to_datetime(df_sym.index)
            if df_sym.index.tz is not None:
                df_sym.index = df_sym.index.tz_localize(None)

            min_bars = SMA_200 + MOMENTUM_LONG_LB + MOMENTUM_SHORT_LB + 10
            if len(df_sym) < min_bars:
                print(f"insufficient data ({len(df_sym)} bars, need {min_bars}), skipping")
                ticker_results[symbol] = {"error": "insufficient data"}
                continue

            trades  = backtest_ticker(symbol, df_sym, backtest_start)
            metrics = compute_metrics(trades)
            ticker_results[symbol] = {"metrics": metrics, "trades": trades}
            all_trades.extend(trades)

            wr  = f"{metrics['win_rate']:.1%}"  if metrics["win_rate"]    is not None else "n/a"
            ar  = f"{metrics['avg_r']:+.2f}R"   if metrics["avg_r"]       is not None else "n/a"
            sh  = f"{metrics['sharpe_ratio']:.2f}" if metrics["sharpe_ratio"] is not None else "n/a"
            print(f"{metrics['total_trades']:>3} trades | WR={wr} | Avg={ar} | Sharpe={sh}")

        except Exception as e:
            import traceback
            print(f"ERROR — {e}")
            traceback.print_exc()
            ticker_results[symbol] = {"error": str(e)}

    agg = compute_metrics(all_trades)

    output = {
        "backtest_meta": {
            "run_at":          datetime.now(PT).isoformat(),
            "tickers":         TICKERS,
            "backtest_start":  backtest_start.strftime("%Y-%m-%d"),
            "backtest_end":    today_str,
            "profile":         "paper",
            "stop_atr_mult":   STOP_ATR_MULT,
            "target_atr_mult": TARGET_ATR_MULT,
            "rr_ratio":        round(TARGET_ATR_MULT / STOP_ATR_MULT, 2),
            "min_score":       MIN_SCORE,
            "max_score":       MAX_SCORE,
            "max_hold_days":   MAX_HOLD_DAYS,
            "cooldown_bars":   COOLDOWN_BARS,
            "scoring_notes": (
                "Daily-bar proxy for the 12-pt system. "
                "VWAP = free 1pt on daily (swing mode, per confluence.py L113-114). "
                "MACD dual_macd_agreement: cross + histogram_rising/falling for both "
                "standard(12,26,9) and fast(3,15,3); score>=2 passes. "
                "Trend filter: long_allowed = not (bear|strong_bear) per trend_filter.py. "
                "Momentum: Jegadeesh-Titman 252-21 day lookback. "
                "Entry on next-bar open. Stop/target checked bar-by-bar on H/L."
            ),
            "guardrails": {
                "1_data_source":          "T1 Alpaca Data via data/fetcher.py (fetch_bars) — no yfinance for equities",
                "2_file_access":          "read-only — no bot files modified",
                "3_output_directory":     "logs/ only",
                "4_no_execution_imports": "execution.broker / risk_manager not imported",
                "5_rth_check":            "blocked weekdays 9:30 AM–4:00 PM ET (standard CLAUDE.md block)",
            },
        },
        "aggregate":  agg,
        "per_ticker": {
            sym: {
                "metrics":     res.get("metrics", {}),
                "trade_count": len(res.get("trades", [])),
            }
            for sym, res in ticker_results.items()
        },
        "trades": all_trades,
    }

    # Guardrail 3: write to logs/ only
    (ROOT / "logs").mkdir(exist_ok=True)
    out_path = ROOT / "logs" / f"backtest_12pt_{today_str}.json"
    _tmp = out_path.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(output, indent=2, default=str))
    _tmp.replace(out_path)

    print()
    print("=" * 65)
    print("  AGGREGATE RESULTS  (all tickers combined)")
    print("=" * 65)
    if agg["total_trades"] > 0:
        print(f"  Total trades:      {agg['total_trades']}  ({agg['long_trades']} long / {agg['short_trades']} short)")
        print(f"  Win rate:          {agg['win_rate']:.1%}  ({agg['wins']}W / {agg['losses']}L)")
        print(f"  Average R:         {agg['avg_r']:+.3f}R")
        print(f"  Sharpe ratio:      {agg['sharpe_ratio']:.2f}  (annualized, per-trade series)")
        print(f"  Max drawdown:      {agg['max_drawdown_pct']:.1f}%  ({agg['max_drawdown_r']:.2f}R)")
        print(f"  Signals/week:      {agg['signals_per_week']:.1f}  (across {len(TICKERS)} tickers)")
        print(f"  Avg hold:          {agg['avg_hold_days']} days")
        print(f"  Exit reasons:      {agg['exit_reasons']}")
    else:
        print("  No trades generated — check indicator warmup or widen MIN_SCORE.")
    print()
    print(f"  Results → {out_path}")
    print("=" * 65)


if __name__ == "__main__":
    main()
