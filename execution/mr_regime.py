# ruff: noqa: E501
"""
execution/mr_regime.py

Mean-reversion / regime DETECTOR (item 2, diff 1 of 4 — 2026-08-02, Rafael + BGG).

PURE, self-contained, never-raises, fail-safe. NO entry wiring in this diff — this module only
COMPUTES the two signals the mean-reversion LONG layer needs; the gated long-entry path that
consumes them is a later, separately-gated diff. Independently testable (anti-silo isolation
boundary): inputs are a daily-bars DataFrame; outputs are plain dict/bool; no I/O, no order calls.

WHY (front-loaded simulation, logs/mr_regime_long_design_2026-08-02.md): on a structurally-bearish
crashed name (below its 150d SMA — exactly where the 12-pt SHORT score sits at 10-12 and the bot
perma-shorts), a LONG has an edge ONLY when the bounce is CONFIRMED. The simulation (10 crashed
names, ~9 months, Alpaca T1) showed catching the falling knife (oversold IN-STATE) has NEGATIVE
5-day return (-0.39%), while waiting for the FIRST UP-CLOSE after oversold FLIPS it POSITIVE
(+0.71% fwd5, +1.78% fwd10, ~53% win). A mean-reverting regime (variance-ratio<1 / Hurst<0.5)
adds a little at the 10-day horizon. Hence the two functions below: `confirmed_reversal` (the
load-bearing entry trigger) and `regime_state` (the secondary confirmer).

Thresholds are the sim-validated FIRST-GUESSES, exposed via config with defaults here so they can
be data-derived later (roadmap) without touching callers — mirroring counter_trend.py's use of
config.MOMENTUM_SHORT_LOOKBACK. NONE are fitted per-name (per-name sample is tiny); they are the
population-level values the sim validated.
"""
import logging

import numpy as np

import config
from indicators.rsi import add_rsi

logger = logging.getLogger(__name__)


def _p(name, default):
    """Read a tunable from config with a sim-validated default; never raises."""
    try:
        return getattr(config, name, default)
    except Exception:  # pragma: no cover - defensive
        return default


def _variance_ratio(logret, q):
    """Lo-MacKinlay VR(q) on a 1-D array of log returns. <1 mean-reverting, >1 trending, ~1 random
    walk. Returns None when there are too few points or variance is degenerate. NEVER RAISES."""
    try:
        x = np.asarray(logret, dtype=float)
        x = x[np.isfinite(x)]
        if len(x) < q * 3:
            return None
        mu = x.mean()
        var1 = np.mean((x - mu) ** 2)
        if var1 <= 0:
            return None
        m = len(x) - (len(x) % q)
        if m < q:
            return None
        xq = x[:m].reshape(-1, q).sum(axis=1)
        varq = np.mean((xq - q * mu) ** 2) / q
        return float(varq / var1)
    except Exception as _e:  # pragma: no cover - defensive
        logger.debug("mr_regime._variance_ratio failed (%s)", _e)
        return None


def _hurst(close, max_lag):
    """Hurst exponent via lagged-difference variance scaling. H<0.5 mean-reverting, >0.5 trending.
    Returns None on insufficient data or a degenerate fit. NEVER RAISES."""
    try:
        c = np.asarray(close, dtype=float)
        c = c[np.isfinite(c) & (c > 0)]
        if len(c) < max_lag * 2:
            return None
        x = np.log(c)
        lags = range(2, max_lag)
        tau = [np.sqrt(np.std(x[lag:] - x[:-lag])) for lag in lags]
        tau = np.asarray(tau)
        if np.any(tau <= 0) or not np.all(np.isfinite(tau)):
            return None
        poly = np.polyfit(np.log(list(lags)), np.log(tau), 1)
        return float(poly[0] * 2.0)
    except Exception as _e:  # pragma: no cover - defensive
        logger.debug("mr_regime._hurst failed (%s)", _e)
        return None


def regime_state(daily_df):
    """Return {"variance_ratio": float|None, "hurst": float|None, "mean_reverting": bool}.

    mean_reverting is True iff EITHER the variance ratio < 1.0 OR the Hurst exponent < 0.5 on the
    trailing window. FAIL-SAFE: if neither can be computed, mean_reverting is False (UNKNOWN ->
    neutral, never a spurious True). NEVER RAISES."""
    try:
        window = int(_p("MR_VR_WINDOW", 60))
        q = int(_p("MR_VR_Q", 5))
        max_lag = int(_p("MR_HURST_MAXLAG", 20))
        if daily_df is None or "close" not in daily_df or len(daily_df) < window + 2:
            return {"variance_ratio": None, "hurst": None, "mean_reverting": False}
        close = daily_df["close"].astype(float)
        logret = np.log(close / close.shift(1)).iloc[-window:]
        vr = _variance_ratio(logret.values, q)
        h = _hurst(close.iloc[-window:].values, max_lag)
        mean_reverting = bool((vr is not None and vr < 1.0) or (h is not None and h < 0.5))
        return {"variance_ratio": vr, "hurst": h, "mean_reverting": mean_reverting}
    except Exception as _e:  # RC-3: logged, never silent; fail-safe
        logger.warning("mr_regime.regime_state error (%s) — returning neutral (not mean-reverting).", _e)
        return {"variance_ratio": None, "hurst": None, "mean_reverting": False}


def confirmed_reversal(daily_df):
    """Return True iff the bounce is CONFIRMED: RSI(14) was OVERSOLD (< MR_RSI_OVERSOLD, default 35)
    within the last MR_REVERSAL_LOOKBACK bars (default 3, EXCLUDING today) AND today closed UP vs the
    prior bar (first up-close after oversold). This is the load-bearing entry trigger — the sim showed
    the naive in-state oversold entry (falling knife) is NEGATIVE-EV while this confirmed version is
    positive. FAIL-SAFE: returns False on insufficient data or any error (no signal, never a spurious
    True). NEVER RAISES."""
    try:
        oversold_th = float(_p("MR_RSI_OVERSOLD", 35.0))
        lookback = int(_p("MR_REVERSAL_LOOKBACK", 3))
        if daily_df is None or "close" not in daily_df or len(daily_df) < max(lookback + 2, 20):
            return False
        df = add_rsi(daily_df.copy())
        if "rsi" not in df:
            return False
        rsi = df["rsi"]
        close = df["close"].astype(float)
        # today is the first up-close: close[-1] > close[-2]
        up_close = float(close.iloc[-1]) > float(close.iloc[-2])
        # was oversold within the last `lookback` bars EXCLUDING today (the knife phase before the bounce)
        recent = rsi.iloc[-(lookback + 1):-1]
        recent_oversold = bool((recent < oversold_th).any())
        return bool(up_close and recent_oversold)
    except Exception as _e:  # RC-3: logged, never silent; fail-safe
        logger.warning("mr_regime.confirmed_reversal error (%s) — returning False (no signal).", _e)
        return False


def confirmed_rollover(daily_df):
    """SHORT-side mirror of confirmed_reversal. Return True iff the top is CONFIRMED: RSI(14) was
    OVERBOUGHT (> MR_RSI_OVERBOUGHT, default 65) within the last MR_REVERSAL_LOOKBACK bars (default 3,
    EXCLUDING today) AND today closed DOWN vs the prior bar (first down-close after overbought). The
    front-loaded sim (logs/mr_regime_long_design_2026-08-02.md) showed this confirmed short-side
    trigger on a structurally-BULLISH (overextended) name has a positive short edge (pnl10 +0.75%,
    win 55%; +0.85%/57% when a MEAN-REVERTING regime is also required — the regime filter matters MORE
    on the short side, as the guard against shorting a rollover in a still-trending-up market). CALLERS
    MUST additionally require regime_state()['mean_reverting'] for the short (stricter than the long).
    FAIL-SAFE: returns False on insufficient data or any error. NEVER RAISES."""
    try:
        overbought_th = float(_p("MR_RSI_OVERBOUGHT", 65.0))
        lookback = int(_p("MR_REVERSAL_LOOKBACK", 3))
        if daily_df is None or "close" not in daily_df or len(daily_df) < max(lookback + 2, 20):
            return False
        df = add_rsi(daily_df.copy())
        if "rsi" not in df:
            return False
        rsi = df["rsi"]
        close = df["close"].astype(float)
        down_close = float(close.iloc[-1]) < float(close.iloc[-2])
        recent = rsi.iloc[-(lookback + 1):-1]
        recent_overbought = bool((recent > overbought_th).any())
        return bool(down_close and recent_overbought)
    except Exception as _e:  # RC-3: logged, never silent; fail-safe
        logger.warning("mr_regime.confirmed_rollover error (%s) — returning False (no signal).", _e)
        return False


def _sma(close, n):
    """Trailing simple moving average of the last n closes; None if too few bars. Never raises."""
    try:
        if close is None or len(close) < n:
            return None
        return float(close.iloc[-n:].mean())
    except Exception:  # pragma: no cover - defensive
        return None


def spy_downtrend_suppress_longs(spy_daily_df):
    """Return True when MR LONGS should be SUPPRESSED because SPY is in an intermediate downtrend
    (SPY close < its MR_SPY_DOWNTREND_SMA=50-day SMA). The masked-loss board seat's regime brake: the
    5-min SPY-direction gate is a micro-gate (single-bar >1%), NOT a slow-bleed brake, and MR bypasses
    counter_trend — so without this MR longs catch the broad-market knife in a selloff (correlated basket).

    FAIL-CLOSED: returns True (suppress) on insufficient data or ANY error — a missed MR long is cheap,
    a caught index-knife basket is the account-ender. Returns False (allow) ONLY when SPY is confirmed
    at/above its SMA. NEVER RAISES."""
    try:
        n = int(_p("MR_SPY_DOWNTREND_SMA", 50))
        if spy_daily_df is None or "close" not in spy_daily_df or len(spy_daily_df) < n + 1:
            return True  # unknown SPY regime → fail-closed (suppress)
        close = spy_daily_df["close"].astype(float)
        sma = _sma(close, n)
        px = float(close.iloc[-1])
        if sma is None or sma <= 0:
            return True  # can't compute → fail-closed
        return bool(px < sma)
    except Exception as _e:  # RC-3: logged, never silent; fail-CLOSED
        logger.warning("mr_regime.spy_downtrend_suppress_longs error (%s) — suppressing MR longs (fail-closed).", _e)
        return True


def mean_reversion_confluence(daily_df, direction):
    """Bidirectional MEAN-REVERSION confluence score (item 2, diff 2). direction: "long"|"short".

    SEPARATE from the trend-following 12-pt confluence: it scores a STRETCHED-and-REVERSING name on
    exhaustion/reversal features, so a crashed name setting up to bounce (long) or an overextended
    rally rolling over (short) is scored on its OWN merits instead of being perma-scored by the
    trend structure. Returns {"eligible": bool, "direction": str, "score": int, "max_score": int,
    "conditions": {...}}. FAIL-SAFE: eligible=False / score=0 on any error or ineligible state.
    NEVER RAISES. PURE (reads bars only; no I/O, no order calls).

    ELIGIBILITY GATE (all required, else eligible=False, score=0):
      - LONG : price BELOW the 150d SMA (structurally bearish) AND confirmed_reversal.
      - SHORT: price ABOVE the 150d SMA (structurally bullish) AND confirmed_rollover AND
               mean_reverting regime — STRICTER than long, because the front-loaded sim showed the
               regime filter is the key guard against shorting a rollover in a still-trending market.
    SCORE (each +1; the confirmed trigger is the eligibility gate, so it is implicit):
      +1 mean_reverting regime  (long: a bonus; short: always present since it is required)
      +1 RSI extremity          (deeply oversold < MR_RSI_EXTREME_LOW / overbought > MR_RSI_EXTREME_HIGH)
      +1 stretched from the SMA (|price/SMA - 1| >= MR_STRETCH_PCT)
    max_score = 3. Thresholds are sim-informed first-guesses via config (data-derivation roadmapped).
    """
    _null = {"eligible": False, "direction": direction, "score": 0, "max_score": 3, "conditions": {}}
    try:
        if direction not in ("long", "short"):
            return _null
        sma_n = int(_p("MR_SMA_PERIOD", 150))
        if daily_df is None or "close" not in daily_df or len(daily_df) < sma_n + 2:
            return _null
        df = add_rsi(daily_df.copy())
        if "rsi" not in df:
            return _null
        close = df["close"].astype(float)
        px = float(close.iloc[-1])
        sma = _sma(close, sma_n)
        if sma is None or sma <= 0:
            return _null
        mean_reverting = bool(regime_state(daily_df).get("mean_reverting"))
        _rsi_val = df["rsi"].iloc[-1]
        rsi_now = float(_rsi_val) if _rsi_val == _rsi_val else None  # NaN-safe
        stretch = abs(px / sma - 1.0)

        if direction == "long":
            structural = px < sma
            trigger = confirmed_reversal(daily_df)
            eligible = bool(structural and trigger)
            rsi_extreme = bool(rsi_now is not None and rsi_now < float(_p("MR_RSI_EXTREME_LOW", 25.0)))
        else:  # short
            structural = px > sma
            trigger = confirmed_rollover(daily_df)
            eligible = bool(structural and trigger and mean_reverting)
            rsi_extreme = bool(rsi_now is not None and rsi_now > float(_p("MR_RSI_EXTREME_HIGH", 75.0)))

        if not eligible:
            return {**_null, "conditions": {"structural": bool(structural), "trigger": bool(trigger),
                                            "mean_reverting": mean_reverting}}

        stretched = bool(stretch >= float(_p("MR_STRETCH_PCT", 0.10)))
        score = int(mean_reverting) + int(rsi_extreme) + int(stretched)
        conds = {"structural": True, "trigger": True, "mean_reverting": mean_reverting,
                 "rsi_extreme": rsi_extreme, "stretched": stretched,
                 "rsi": round(rsi_now, 1) if rsi_now is not None else None,
                 "stretch_pct": round(stretch * 100.0, 1)}
        return {"eligible": True, "direction": direction, "score": int(score), "max_score": 3,
                "conditions": conds}
    except Exception as _e:  # RC-3: logged, never silent; fail-safe
        logger.warning("mr_regime.mean_reversion_confluence(%s) error (%s) — ineligible.", direction, _e)
        return _null
