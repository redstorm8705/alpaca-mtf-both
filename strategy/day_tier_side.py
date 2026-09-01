# ruff: noqa: E501
"""
strategy/day_tier_side.py — Day-Tier Layer A: structural SIDE bias (READ-ONLY, INERT).

Rebuilds Layer A of the day-tier meta-label engine per the aligned design record
(logs/design_records/day_tier_v2_design_2026-08-29.md §2 "SIDE"). This is the FIRST
increment of the day-tier engine rebuild after the prior session's validated module was
lost uncommitted (§5b CORRECTION 2026-09-01). Per the hard lesson, this increment is
COMMITTED INERT: it is a pure read-only computation wired to NOTHING (no caller in the live
trade loop), so it cannot affect sizing, entries, or any order. It exists to be unit-tested
and validated before Layers B/C (GEX-whether, entry/size) are built on top.

WHAT IT DOES (design §2): the day-tier SIDE is a per-name, DAILY structural bias — NOT an
intraday trigger (Simons: slow MAs are constant within a month; they set DIRECTION, not
timing). It scores price vs the full project MA stack and returns a LONG / SHORT / TWO_SIDED
(or UNKNOWN on data failure) lean.

MA STACK (design §2 + Rafael §5.6 add-21-EMA + §1.28 quarterly lowest-weight/prune-if-no-edge):
  daily EMAs : 13, 21, 30
  daily SMAs : 20, 150, 200, 325
  10-week SMA (weekly bars)         — mirrors signal_generator._get_weekly_bias' 10wk anchor
  Faber 10-month SMA (monthly bars) — Faber (2007) timing-model anchor
  quarterly SMA (63 trading days)   — NEWEST/least-tested horizon, LOWEST weight, prune first

SCORE: each MA votes sign(close - MA) in {+1 bull, -1 bear}; the quarterly horizon gets the
lowest weight. Weighted sum / total-weight -> normalized score in [-1, +1]. LONG/SHORT/TWO_SIDED
by a documented threshold. Every threshold/weight here is an INITIAL, tunable value on an INERT
signal -- it drives no live trade, so it carries no live-risk (the design's "prune-if-no-edge"
validation happens once this feeds the engine, not now).

FAIL-SAFE: any fetch failure, insufficient bars, or non-finite value degrades that ONE MA to a
non-vote (never a wrong-direction vote); if too few MAs survive, the whole read returns UNKNOWN
(neutral) rather than a low-confidence lean. Never raises to the caller.

Data tier: T1 (Alpaca Data via data.fetcher.fetch_bars) -- the only approved bar source.
"""
from __future__ import annotations

import logging
import math

import config
from data.fetcher import fetch_bars

logger = logging.getLogger(__name__)

# -- MA stack definition ------------------------------------------------------------------
# "ema"/"sma" run on daily closes; the 10-week/10-month run on their own timeframe's closes.
# weight=1.0 for every established horizon; the NEW quarterly horizon gets 0.5 (design 1.28:
# lowest weight, prune-if-no-edge).
_DAILY_EMAS = ((13, "ema13"), (21, "ema21"), (30, "ema30"))
_DAILY_SMAS = ((20, "sma20"), (150, "sma150"), (200, "sma200"), (325, "sma325"))
# DERIVATION PLAN (PROV:daytier-side-thresholds) — the four thresholds below are PROVISIONAL
# initial values on an INERT signal (drives no live trade → no live risk), tagged per the
# no-static-regimes mandate's documented no-data-yet exception: nothing to derive from until the
# signal has run. Once compute_side_bias has logged >= a few hundred scored reads WITH realized
# forward outcomes, DERIVE them from data and retire the PROV tags: the LONG/SHORT cutoffs from the
# outcome-conditioned score-distribution quantiles that best separate forward return; the quarterly
# WEIGHT from its marginal IC (design §1.28 "prune-if-no-edge" → drop to 0 if no edge); the coverage
# floor from the coverage-vs-accuracy curve. This is the design's validation step, executed when the
# signal feeds the engine — not now (no data exists yet).
_QUARTERLY_PERIOD = 63          # ~3 trading months on daily bars
_QUARTERLY_WEIGHT = 0.5         # PROV:daytier-side-thresholds — lowest weight, newest horizon (design §1.28)
_TENWEEK_PERIOD = 10            # weekly bars
_TENMONTH_PERIOD = 10           # monthly bars (Faber)

# Enough daily history for the 325-SMA plus a small buffer (the longest daily lookback).
_DAILY_BARS = 360
_WEEKLY_BARS = 14               # >=10 for the 10-week SMA + buffer
_MONTHLY_BARS = 13              # >=10 for the 10-month SMA + buffer

# Classification thresholds on the normalized [-1, +1] score (see DERIVATION PLAN above).
_LONG_THRESHOLD = 0.34          # PROV:daytier-side-thresholds — >= -> LONG lean
_SHORT_THRESHOLD = -0.34        # PROV:daytier-side-thresholds — <= -> SHORT lean; else TWO_SIDED
# Minimum share of total possible weight that must actually vote, else UNKNOWN (neutral).
_MIN_WEIGHT_COVERAGE = 0.60     # PROV:daytier-side-thresholds — data-sufficiency floor


def _finite_positive(x) -> "float | None":
    """Return float(x) iff it is finite and > 0, else None (a bad close is a non-vote)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def _last_close(df) -> "float | None":
    """Most recent close from a fetch_bars frame, or None if absent/non-finite."""
    if df is None or getattr(df, "empty", True) or "close" not in getattr(df, "columns", []):
        return None
    try:
        return _finite_positive(df["close"].iloc[-1])
    except (IndexError, KeyError):
        return None


def _sma(df, period: int) -> "float | None":
    """Simple MA of the last `period` closes, or None if too few / non-finite."""
    if df is None or getattr(df, "empty", True) or "close" not in getattr(df, "columns", []):
        return None
    closes = df["close"].dropna()
    if len(closes) < period:
        return None
    return _finite_positive(closes.tail(period).mean())


def _ema(df, period: int) -> "float | None":
    """Exponential MA (span=period, adjust=False -- matches indicators/macd.py), last value."""
    if df is None or getattr(df, "empty", True) or "close" not in getattr(df, "columns", []):
        return None
    closes = df["close"].dropna()
    if len(closes) < period:
        return None
    try:
        return _finite_positive(closes.ewm(span=period, adjust=False).mean().iloc[-1])
    except (IndexError, ValueError):
        return None


def _vote(close: float, ma: "float | None", weight: float, label: str, stack: dict) -> tuple:
    """Record one MA's vote. Returns (weighted_vote, weight_used). A missing MA is a NON-VOTE
    (0 weight) -- never a wrong-direction vote (fail-safe). sign(close - ma): + bull, - bear."""
    if ma is None:
        stack[label] = None
        return 0.0, 0.0
    direction = 1 if close > ma else (-1 if close < ma else 0)
    stack[label] = direction
    return direction * weight, weight


def compute_side_bias(symbol: str) -> dict:
    """Per-name structural SIDE bias from the daily/weekly/monthly MA stack (design 2).

    READ-ONLY + INERT: computes and returns a lean; wired to nothing, orders nothing.

    Returns a dict:
      {"symbol", "side": "LONG"|"SHORT"|"TWO_SIDED"|"UNKNOWN", "score": float in [-1,1] or None,
       "stack": {label: +1|-1|0|None}, "weight_coverage": float, "reason": str}
    Never raises.
    """
    result: dict = {"symbol": symbol, "side": "UNKNOWN", "score": None,
                    "stack": {}, "weight_coverage": 0.0, "reason": ""}
    try:
        # -- T1 fetches (fail-safe per timeframe; a missing frame degrades only its MAs) --
        try:
            daily = fetch_bars(symbol, config.TF_DAILY, num_bars=_DAILY_BARS)
        except Exception as _de:
            daily = None
            logger.debug("[%s] day-tier SIDE: daily fetch failed: %s", symbol, _de)
        try:
            weekly = fetch_bars(symbol, config.TF_WEEKLY, num_bars=_WEEKLY_BARS)
        except Exception as _we:
            weekly = None
            logger.debug("[%s] day-tier SIDE: weekly fetch failed: %s", symbol, _we)
        try:
            monthly = fetch_bars(symbol, config.TF_MONTHLY, num_bars=_MONTHLY_BARS)
        except Exception as _me:
            monthly = None
            logger.debug("[%s] day-tier SIDE: monthly fetch failed: %s", symbol, _me)

        # Structural bias is a DAILY read; the current daily close is the reference price.
        # Without it there is no anchor to compare any MA against -> UNKNOWN.
        close = _last_close(daily)
        if close is None:
            result["reason"] = "no usable daily close (T1 fetch failed / empty)"
            logger.warning("[%s] day-tier SIDE: %s -- UNKNOWN (neutral).", symbol, result["reason"])
            return result

        stack: dict = {}
        weighted_sum = 0.0
        weight_used = 0.0
        weight_total = 0.0

        for period, label in _DAILY_EMAS:
            weight_total += 1.0
            wv, wu = _vote(close, _ema(daily, period), 1.0, label, stack)
            weighted_sum += wv
            weight_used += wu
        for period, label in _DAILY_SMAS:
            weight_total += 1.0
            wv, wu = _vote(close, _sma(daily, period), 1.0, label, stack)
            weighted_sum += wv
            weight_used += wu

        # 10-week SMA on WEEKLY closes; 10-month (Faber) on MONTHLY closes.
        weight_total += 1.0
        wv, wu = _vote(close, _sma(weekly, _TENWEEK_PERIOD), 1.0, "sma_10week", stack)
        weighted_sum += wv
        weight_used += wu
        weight_total += 1.0
        wv, wu = _vote(close, _sma(monthly, _TENMONTH_PERIOD), 1.0, "sma_10month", stack)
        weighted_sum += wv
        weight_used += wu

        # Quarterly horizon (newest, LOWEST weight, prune-if-no-edge) -- 63-day daily SMA.
        weight_total += _QUARTERLY_WEIGHT
        wv, wu = _vote(close, _sma(daily, _QUARTERLY_PERIOD), _QUARTERLY_WEIGHT, "sma_quarterly", stack)
        weighted_sum += wv
        weight_used += wu

        result["stack"] = stack
        coverage = (weight_used / weight_total) if weight_total > 0 else 0.0
        result["weight_coverage"] = round(coverage, 3)

        # Too little of the stack voted (data gaps) -> do NOT emit a low-confidence lean.
        if weight_used <= 0.0 or coverage < _MIN_WEIGHT_COVERAGE:
            result["reason"] = (f"insufficient MA coverage {coverage:.0%} < "
                                f"{_MIN_WEIGHT_COVERAGE:.0%} (data gaps) -- neutral")
            logger.warning("[%s] day-tier SIDE: %s -- UNKNOWN.", symbol, result["reason"])
            return result

        # Normalize by the weight that ACTUALLY voted (not the total) so partial coverage is
        # not biased toward zero: score in [-1, +1].
        score = weighted_sum / weight_used
        result["score"] = round(score, 4)
        if score >= _LONG_THRESHOLD:
            result["side"] = "LONG"
        elif score <= _SHORT_THRESHOLD:
            result["side"] = "SHORT"
        else:
            result["side"] = "TWO_SIDED"
        _n_bull = sum(1 for v in stack.values() if v == 1)
        _n_voted = sum(1 for v in stack.values() if v is not None)
        result["reason"] = f"score={score:+.3f} coverage={coverage:.0%} (bull {_n_bull}/{_n_voted} MAs)"
        logger.info("[%s] day-tier SIDE (INERT): %s %s", symbol, result["side"], result["reason"])
        return result
    except Exception as _e:  # read-only signal must NEVER raise into a caller
        result["side"] = "UNKNOWN"
        result["reason"] = f"unexpected error: {_e!r}"
        logger.warning("[%s] day-tier SIDE: unexpected error -- UNKNOWN (neutral): %s", symbol, _e)
        return result
