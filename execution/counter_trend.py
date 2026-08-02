# ruff: noqa: E501
"""
execution/counter_trend.py

Counter-trend bounce / falling-knife gate (2026-08-02, Rafael + BGG).

Do NOT short a structurally-bearish name that is BOUNCING UP over the last month, and do NOT
long a structurally-bullish name that is CRASHING DOWN over the last month.

WHY (audit finding, verified in strategy/confluence.py + the SMCI trades): the 12-point SHORT
confluence score awards ~5/12 points purely from LONG-TERM downtrend structure —
daily_above_150sma(+2 when BELOW 150d SMA), daily_above_200sma(+1 when BELOW 200d),
momentum_12_1(+2 when 12-month momentum is negative). A crashed former high-flyer (SMCI,
~$1000 -> $24) is PERMANENTLY below both SMAs with hugely negative 12-mo momentum, so it
collects that floor on every scan and scores 10-12 SHORT even while ripping +15% off the bottom.
The bot then shorts the bounce and is stopped repeatedly. The score measures "is this a
structurally broken stock?" (yes, permanently), NOT "is this a good tactical short RIGHT NOW?".
score_long_signal has the exact mirror (structurally-bullish stock crashing = falling knife).

BGG-APPROVED design:
  - SIGNAL = the daily 1-month return, sign vs trade direction. PARAMETER-FREE: it reuses the
    existing config.MOMENTUM_SHORT_LOOKBACK (21) window — which is precisely the short-term
    REVERSAL window that Jegadeesh-Titman's 12-1 momentum deliberately SKIPS. Zero fitted numbers
    (the correct 1-in-10 no-static exception; per-name sample size is ~3, so a tuned threshold
    would be pure overfit). ROADMAP: derive a data-driven magnitude once enough blocks accrue.
  - SCOPE = fire ONLY when the structural score is elevated (the "crashed-and-scoring-high"
    pattern), so a FRESH breakdown on a name not yet below its 150 SMA is NOT gated.
  - SYMMETRY = applies to BOTH directions (momentum crashes are symmetric — Asness/AQR).
  - ROLLOUT = LIVE with daily decision-impact logging (Rafael staged-activation); FAIL-OPEN.

Pure, self-contained, independently testable (anti-silo isolation boundary): inputs are the
trade direction, the signal dict, and the daily bars DataFrame. NEVER RAISES.
"""
import logging

import config

logger = logging.getLogger(__name__)


def one_month_return(daily_df):
    """1-month (config.MOMENTUM_SHORT_LOOKBACK=21 trading days) simple return: close[-1] /
    close[-(N+1)] - 1. Returns None when there are fewer than N+2 bars or the reference price
    is non-positive (caller treats None as fail-open — no block). NEVER RAISES."""
    try:
        _n = int(getattr(config, "MOMENTUM_SHORT_LOOKBACK", 21))
        if daily_df is None or len(daily_df) < _n + 2:
            return None
        _c = daily_df["close"]
        _now = float(_c.iloc[-1])
        _then = float(_c.iloc[-(_n + 1)])
        if _then <= 0:
            return None
        return _now / _then - 1.0
    except Exception as _e:  # pragma: no cover - defensive
        logger.debug("one_month_return: compute failed (%s)", _e)
        return None


def counter_trend_block(direction, sig, daily_df):
    """Return (blocked: bool, ctx: dict).

    blocked=True iff EITHER:
      - direction=="short" AND the candidate is STRUCTURALLY BEARISH (it scored the
        below-150-SMA structural point) AND the 1-month return is POSITIVE (bouncing up) —
        i.e. shorting a bounce; OR
      - direction=="long" AND the candidate is STRUCTURALLY BULLISH (above-150-SMA point) AND
        the 1-month return is NEGATIVE (declining) — i.e. longing a falling knife.

    The SAME condition flag `daily_above_150sma` is the structural proxy for both directions,
    because score_short_signal sets it True when price is BELOW the 150 SMA (bearish) while
    score_long_signal sets it True when ABOVE (bullish) — in both cases True means "the
    structural point scored FOR this direction".

    ctx carries the daily decision-impact audit fields. FAIL-OPEN: returns (False, ...) on any
    error, on a non-structural candidate, or when the 1-month return cannot be computed.
    NEVER RAISES."""
    try:
        _r = one_month_return(daily_df)
        if _r is None:
            return False, {"reason": "no_1m_return"}
        _cond = (sig or {}).get("conditions") or {}
        _structural = bool(_cond.get("daily_above_150sma"))
        _ctx = {
            "direction": direction,
            "mom_1m_pct": round(_r * 100.0, 2),
            "score": int((sig or {}).get("score", 0) or 0),
            "structural": _structural,
        }
        if not _structural:
            return False, {**_ctx, "reason": "not_structural"}
        if direction == "short" and _r > 0.0:
            return True, {**_ctx, "reason": "short_into_1m_bounce"}
        if direction == "long" and _r < 0.0:
            return True, {**_ctx, "reason": "long_into_1m_decline"}
        return False, {**_ctx, "reason": "trend_aligned"}
    except Exception as _e:  # RC-3: logged, never silent; fail-open
        logger.warning("counter_trend_block(%s): error (%s) — FAIL-OPEN (allowing entry).", direction, _e)
        return False, {"reason": "error"}
