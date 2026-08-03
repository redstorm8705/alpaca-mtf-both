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
