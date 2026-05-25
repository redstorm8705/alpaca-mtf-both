"""
strategy/volatility_regime.py
Volatility regime detection — no options feed required.

Uses SPY 10-day realized volatility (annualized) as a VIX proxy.
Classifies market into three regimes and returns size/stop multipliers.

Regimes:
  LOW_VOL    (<12% annualized) — low-vol grind, trends are clean, size up
  NORMAL     (12–25%)          — baseline behavior
  HIGH_VOL   (>25%)            — panic/expansion, chop increases, size down

Composite regime (display-only, no effect on sizing):
  BULL       — SPY > 50d SMA AND VIX/VIX3M < 1.0 AND 20d rvol < 25%
  BEAR       — SPY < 50d SMA AND VIX/VIX3M > 1.0 AND 20d rvol > 30%
  HIGH_VOL   — VIX/VIX3M > 1.1 (inverted term structure, regardless of direction)
  NEUTRAL    — everything else
  VIX3M failures: single = debug log + default NORMAL; 3+ consecutive = WARNING log.
"""

import logging
import requests
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import config
from data.fetcher import fetch_bars

logger = logging.getLogger("vol_regime")
ET = ZoneInfo("America/New_York")


class VolatilityRegime:
    LOW_VOL = "low_vol"
    NORMAL  = "normal"
    HIGH_VOL = "high_vol"


class RegimeDetector:
    def __init__(self):
        # ── Existing volatility regime state ──────────────────────────────────
        self._regime       = VolatilityRegime.NORMAL
        self._realized_vol = 0.0
        self._last_check   = None
        self._refresh_mins = 15   # MRI audit Apr 6 2026: 30 → 15 min for faster VIX response

        # ── Composite regime state (display-only) ─────────────────────────────
        self._composite_cache      = None
        self._composite_last_check = None
        self._composite_refresh_mins = 30
        self._vix3m_failure_count  = 0   # consecutive VIX3M fetch failures

    def get_regime(self, force_refresh: bool = False) -> dict:
        """
        Returns current volatility regime and associated multipliers.
        Refreshes every 30 minutes automatically.

        Returns:
            {
                "regime":     str (LOW_VOL / NORMAL / HIGH_VOL),
                "realized_vol": float (annualized %),
                "size_mult":  float,
                "stop_mult":  float,
                "note":       str,
            }
        """
        now = datetime.now(ET)
        stale = (
            self._last_check is None or
            (now - self._last_check).total_seconds() > self._refresh_mins * 60
        )

        if stale or force_refresh:
            self._refresh()

        return self._build_result()

    def _fetch_vix(self) -> float:
        """
        Fetch current VIX from Yahoo Finance (free, no API key).
        VIX leads realized vol by days — preferred over SPY proxy.
        Returns VIX level as float, or None on failure.
        """
        try:
            resp = requests.get(config.VIX_API_URL, timeout=5,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return None
            data = resp.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            vix = next((v for v in reversed(closes) if v is not None), None)
            return round(float(vix), 2) if vix else None
        except Exception as e:
            logger.debug(f"VIX fetch failed: {e}")
            return None

    def _fetch_spy_realized_vol(self) -> float:
        """Fallback: SPY 10-day realized vol annualized."""
        try:
            df = fetch_bars("SPY", config.TF_DAILY, num_bars=15)
            if df.empty or len(df) < 11:
                return None
            closes   = df["close"].tail(11)
            log_rets = np.log(closes / closes.shift(1)).dropna()
            return round(log_rets.std() * np.sqrt(252) * 100, 2)
        except Exception:
            return None

    def _refresh(self):
        """
        Refresh volatility regime.
        Tries real VIX first (leads realized vol by days).
        Falls back to SPY 10-day realized vol if VIX fetch fails.
        """
        # PRIMARY: real VIX
        vix = self._fetch_vix()

        if vix is not None:
            self._realized_vol = vix
            self._last_check   = datetime.now(ET)
            source = "VIX"
        else:
            # FALLBACK: SPY realized vol
            spy_vol = self._fetch_spy_realized_vol()
            if spy_vol is None:
                logger.warning("RegimeDetector: both VIX and SPY vol failed, keeping current regime")
                return
            self._realized_vol = spy_vol
            self._last_check   = datetime.now(ET)
            source = "SPY realized vol (VIX fallback)"

        if self._realized_vol < config.REGIME_LOW_VOL_THRESHOLD:
            self._regime = VolatilityRegime.LOW_VOL
        elif self._realized_vol > config.REGIME_HIGH_VOL_THRESHOLD:
            self._regime = VolatilityRegime.HIGH_VOL
        else:
            self._regime = VolatilityRegime.NORMAL

        logger.info(
            f"Volatility regime: {self._regime.upper()} "
            f"({source}: {self._realized_vol:.1f})"
        )

    def _build_result(self) -> dict:
        if self._regime == VolatilityRegime.LOW_VOL:
            return {
                "regime":       VolatilityRegime.LOW_VOL,
                "realized_vol": self._realized_vol,
                "size_mult":    config.REGIME_LOW_VOL_SIZE_MULT,
                "stop_mult":    1.0,
                "note":         f"Low-vol grind ({self._realized_vol:.1f}%) — sizing up {config.REGIME_LOW_VOL_SIZE_MULT}x",
            }
        elif self._regime == VolatilityRegime.HIGH_VOL:
            return {
                "regime":       VolatilityRegime.HIGH_VOL,
                "realized_vol": self._realized_vol,
                "size_mult":    config.REGIME_HIGH_VOL_SIZE_MULT,
                "stop_mult":    config.REGIME_HIGH_VOL_STOP_MULT,
                "note":         f"HIGH VOL ({self._realized_vol:.1f}%) — half size, wider stops",
            }
        else:
            return {
                "regime":       VolatilityRegime.NORMAL,
                "realized_vol": self._realized_vol,
                "size_mult":    config.REGIME_NORMAL_SIZE_MULT,
                "stop_mult":    1.0,
                "note":         f"Normal regime ({self._realized_vol:.1f}%)",
            }

    # ── Composite regime (display-only) ──────────────────────────────────────

    def get_composite_regime(self) -> dict:
        """
        Composite BULL/BEAR/HIGH_VOL/NEUTRAL regime for display.
        Display-only — does not affect size_mult or stop_mult.
        Cached for 30 minutes per instance.
        """
        now = datetime.now(ET)
        stale = (
            self._composite_last_check is None or
            (now - self._composite_last_check).total_seconds() > self._composite_refresh_mins * 60
        )
        if not stale and self._composite_cache is not None:
            return self._composite_cache

        result = self._compute_composite_regime()
        self._composite_cache      = result
        self._composite_last_check = now
        return result

    def _compute_composite_regime(self) -> dict:
        """Fetch all three inputs and classify composite regime."""
        rvol_20d  = self._fetch_spy_rvol_20d()
        ratio, term_label = self._fetch_vix3m_ratio()
        spy_vs_50 = self._fetch_spy_vs_50sma()

        rvol_val    = (rvol_20d / 100.0) if rvol_20d is not None else None
        above_50sma = (spy_vs_50 is not None and spy_vs_50 > 0)

        if ratio > 1.1:
            regime = "HIGH_VOL"
        elif (rvol_val is not None
              and above_50sma
              and ratio < 1.0
              and rvol_val < 0.25):
            regime = "BULL"
        elif (rvol_val is not None
              and not above_50sma
              and ratio > 1.0
              and rvol_val > 0.30):
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

        logger.info(
            f"Composite regime: {regime} | "
            f"VIX/VIX3M={ratio:.3f} ({term_label}) | "
            f"SPY/50SMA={spy_vs_50:+.2f}% | "
            f"20d-rvol={rvol_20d:.1f}%"
            if rvol_20d is not None and spy_vs_50 is not None
            else f"Composite regime: {regime} (partial data)"
        )

        return {
            "composite_regime": regime,
            "vix_term_ratio":   ratio,
            "term_label":       term_label,
            "spy_vs_50sma_pct": spy_vs_50,
            "spy_rvol_20d":     rvol_20d,
        }

    def _fetch_spy_rvol_20d(self) -> float:
        """SPY 20-day realized vol (separate from 10-day used by existing regime)."""
        try:
            df = fetch_bars("SPY", config.TF_DAILY, num_bars=25)
            if df.empty or len(df) < 21:
                return None
            closes   = df["close"].tail(21)
            log_rets = np.log(closes / closes.shift(1)).dropna()
            return round(float(log_rets.std() * np.sqrt(252) * 100), 2)
        except Exception:
            return None

    def _fetch_vix3m_ratio(self) -> tuple:
        """
        Fetch VIX/VIX3M term structure ratio via yfinance.
        Returns (ratio, term_label).
        Consecutive failure counter: single = debug; 3+ = WARNING.
        Falls back to (1.0, "NORMAL") on failure.
        """
        try:
            import yfinance as yf
            vix_df  = yf.download("^VIX",  period="5d", interval="1d",
                                  progress=False, auto_adjust=False)
            vix3m_df = yf.download("^VIX3M", period="5d", interval="1d",
                                   progress=False, auto_adjust=False)

            if vix_df.empty or vix3m_df.empty:
                raise ValueError("Empty VIX/VIX3M data")

            vix_val  = float(vix_df["Close"]["^VIX"].dropna().values[-1])
            vix3m_val = float(vix3m_df["Close"]["^VIX3M"].dropna().values[-1])

            if vix3m_val <= 0:
                raise ValueError(f"Invalid VIX3M value: {vix3m_val}")

            ratio = round(vix_val / vix3m_val, 3)
            self._vix3m_failure_count = 0  # reset on success
            return ratio, ("INVERTED" if ratio > 1.0 else "NORMAL")

        except Exception as e:
            self._vix3m_failure_count += 1
            if self._vix3m_failure_count >= 3:
                logger.warning(
                    f"VIX3M fetch failed {self._vix3m_failure_count} "
                    f"consecutive times: {e}"
                )
            else:
                logger.debug(
                    f"VIX3M fetch failed (attempt {self._vix3m_failure_count}): {e}"
                )
            return 1.0, "NORMAL"

    def _fetch_spy_vs_50sma(self) -> float:
        """Returns SPY close as pct above/below 50d SMA. Positive = above SMA."""
        try:
            df = fetch_bars("SPY", config.TF_DAILY, num_bars=55)
            if df.empty or len(df) < 51:
                return None
            closes  = df["close"]
            sma50   = float(closes.tail(50).mean())
            current = float(closes.iloc[-1])
            return round((current - sma50) / sma50 * 100, 3)
        except Exception:
            return None

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def regime(self) -> str:
        return self._regime

    @property
    def realized_vol(self) -> float:
        return self._realized_vol
