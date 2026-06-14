"""
events/macro_risk_index.py
Macro Risk Index (MRI) — cross-asset background stress score.

Replaces the binary _war_state_active flag in NewsMonitor (Apr 6 2026 audit).

ARCHITECTURE: MARKET-REACTION-FIRST (unchanged)
  SPY 5-min bar movement is still the SOLE entry gate.
  MRI is a BACKGROUND score that:
    1. Sets a size floor — prevents full-size entries during a stressed backdrop
    2. Sets a MIN_SCORE floor — raises the quality bar before SPY gate fires
  It does NOT block entries directly. SPY/QQQ trigger is required for gating.

SCORING (price-based binary flags — no fitted weights per López de Prado rule):
  Component          Ticker       Max Pts
  ─────────────────────────────────────────
  VIX level          ^VIX          30
  VIX term struct    ^VIX / ^VIX3M 20
  JPY carry unwind   JPY=X         15
  TLT flight safety  TLT           15
  Credit spread      HYG + LQD     10
  Oil shock          USO            8
  Gold flight        GLD            5
  Global pre-open    EWJ or EWG     8
  ─────────────────────────────────────────
  Raw max: ~111 → capped at 100

LEVELS:
  0–20   NORMAL    → 1.00x size, MIN_SCORE base (paper: 10)
  21–40  ELEVATED  → 0.85x size, MIN_SCORE base+1 (paper: 11)
  41–60  STRESSED  → 0.70x size, MIN_SCORE base+2 (paper: 12 — max on 12-pt system)
  61–80  HIGH      → 0.55x size, hard-blocked by BV-5 (run_cycle.py)
  81–100 CRITICAL  → 0.40x size, hard-blocked by BV-5 + 2-scan confirmation

PERSISTENCE:
  Refreshed every 10 minutes (configurable).
  Persisted to logs/mri_state.json on every refresh.
  On bot restart: restores last state, decays 10 pts per hour of downtime.
  After >20 hours downtime: starts fresh from VIX-only estimate.

DATA SOURCE:
  T1  (Alpaca Data) — TLT, SPY, HYG, LQD, USO, GLD, EWJ, EWG via fetcher.py
  T2  (FMP)         — ^VIX and USDJPY via stable/quote (5s requests timeout)
  T4  (yfinance)    — ^VIX3M only (not on Alpaca Data or FMP free tier);
                      wrapped in ThreadPoolExecutor with 8s wall-clock cap.
"""

import concurrent.futures as _cf
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import requests as _req  # type: ignore[import-untyped]
import config
from data.fetcher import fetch_bars

logger = logging.getLogger("macro_risk_index")
ET     = ZoneInfo("America/New_York")

ROOT      = Path(__file__).parent.parent.resolve()
MRI_STATE = ROOT / "logs" / "mri_state.json"

# ── Level thresholds ──────────────────────────────────────────────────────────
_LEVELS = [
    (81, "CRITICAL"),
    (61, "HIGH"),
    (41, "STRESSED"),
    (21, "ELEVATED"),
    (0,  "NORMAL"),
]

# ── Size floors per level (Kelly-derived, board-approved) ─────────────────────
_SIZE_FLOOR = {
    "NORMAL":   1.00,
    "ELEVATED": 0.85,
    "STRESSED": 0.70,
    "HIGH":     0.55,
    "CRITICAL": 0.40,
}

# ── MIN_SCORE floor per level (base = 9 for paper profile) ───────────────────
_SCORE_FLOOR_DELTA = {
    "NORMAL":   0,
    "ELEVATED": 1,
    "STRESSED": 2,
    "HIGH":     3,
    "CRITICAL": 3,   # +3 plus 2-scan confirmation enforced in main.py
}


def _score_to_level(score: int) -> str:
    for threshold, label in _LEVELS:
        if score >= threshold:
            return label
    return "NORMAL"


class MacroRiskIndex:
    """
    Cross-asset macro risk scoring engine.
    Instantiate once at bot startup. Call refresh() every 10 minutes.
    """

    def __init__(self, refresh_mins: int = 10):
        self._refresh_mins                   = refresh_mins
        self._last_refresh: datetime | None  = None
        self._raw_score                      = 0
        self._components: dict[str, Any]     = {}
        self._level                          = "NORMAL"
        self._last_known_good_level          = "NORMAL"
        self._last_known_good_at: datetime | None = None
        self._refresh_failed                          = False
        self._refresh_failed_since: datetime | None   = None   # staleness ceiling clock
        self._ceiling_alert_sent                      = False  # once per failure streak
        self._event_type                              = "TECHNICAL"
        # P2-INJECT-NEWS-LOCK: protects _raw_score/_level in inject_news_state
        self._lock = threading.Lock()
        self._yf_executor = _cf.ThreadPoolExecutor(max_workers=1)

        # Restore from disk if available (handles bot restarts)
        self._restore()

    def __del__(self) -> None:
        """Best-effort executor cleanup on instance destruction.
        NOTE: CPython does not guarantee __del__ is called if this object is
        part of a reference cycle or if the process is killed with SIGTERM
        before GC runs. This is a safety net for future refactors that might
        destroy/recreate MacroRiskIndex mid-session. For guaranteed cleanup,
        add mri._yf_executor.shutdown(wait=True) to the bot's SIGTERM handler
        in main.py (P3 follow-up). In normal bot operation, MacroRiskIndex
        lives for the full process lifetime — OS cleans up threads on exit.
        WARNING: shutdown(wait=True) may block up to 8s if a yfinance request
        is in-flight at destruction time. In production this is not an issue
        (MacroRiskIndex persists for the full process lifetime), but future
        refactors creating/destroying instances mid-session should use async
        shutdown or handle blocking explicitly.
        """
        try:
            self._yf_executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass

    # ── Public interface ──────────────────────────────────────────────────────

    def refresh(self, force: bool = False) -> None:
        """Fetch all cross-asset inputs and recompute score. Throttled."""
        now = datetime.now(ET)
        if not force and self._last_refresh is not None:
            elapsed = (now - self._last_refresh).total_seconds() / 60
            if elapsed < self._refresh_mins:
                return

        try:
            self._compute()
            with self._lock:
                self._refresh_failed       = False
                self._refresh_failed_since = None   # reset staleness clock on success
                self._ceiling_alert_sent   = False  # allow alert on next failure streak
            self._last_refresh = now
            self._persist()
            logger.info(
                f"MRI refreshed: score={self._raw_score} level={self._level} "
                f"event={self._event_type} | {self._components}"
            )
        except Exception as e:
            with self._lock:
                self._refresh_failed = True
                if self._refresh_failed_since is None:  # D1: record first failure time
                    self._refresh_failed_since = datetime.now(ET)
            logger.warning(
                f"MRI refresh failed: {e} — "
                f"returning last-known-good level={self._last_known_good_level} "
                f"(cached at {self._last_known_good_at})"
            )

    def score(self) -> int:
        """Current MRI score 0–100."""
        with self._lock:
            return self._raw_score

    def level(self) -> str:
        """NORMAL / ELEVATED / STRESSED / HIGH / CRITICAL."""
        with self._lock:
            if self._refresh_failed:
                if (
                    self._refresh_failed_since is not None
                    and (datetime.now(ET) - self._refresh_failed_since).total_seconds()
                        >= 6 * 3600
                ):
                    if not self._ceiling_alert_sent:
                        self._ceiling_alert_sent = True
                        _good_at = (
                            str(self._last_known_good_at)
                            if self._last_known_good_at else "never"
                        )
                        logger.critical(
                            "MRI STALENESS CEILING: no successful refresh in 6h+ — "
                            "forcing CRITICAL. Last good level was "
                            f"{self._last_known_good_level} (at {_good_at})."
                        )
                    return "CRITICAL"
                return self._last_known_good_level
            return self._level

    def is_fresh(self) -> bool:
        """False if the last refresh() raised — level() is returning cached data."""
        with self._lock:
            return not self._refresh_failed

    def event_type(self) -> str:
        """
        Dominant risk domain driving the score.
        Returns from the 9-type MRI taxonomy:
          GEO_CONFLICT | GEO_ENERGY | MACRO_MONETARY | MACRO_CREDIT |
          MACRO_FX | MACRO_SYSTEMIC | GLOBAL_ASIA | GLOBAL_EU | TECHNICAL
        Used by main.py hybrid engine to classify price triggers when fired.
        """
        return self._event_type

    def size_floor(self) -> float:
        """Position size floor (0.40–1.00) based on current effective level."""
        return _SIZE_FLOOR.get(self.level(), 1.0)

    def min_score_floor(self, base: int = 9) -> int:
        """
        Minimum MIN_SCORE based on current level.
        Pass config.MIN_LONG_SCORE as base. Paper profile: 10; legacy default=9
        preserved for backward compatibility.
        """
        return base + _SCORE_FLOOR_DELTA.get(self._level, 0)

    def requires_confirmation(self) -> bool:
        """CRITICAL level requires 2-scan confirmation before entry."""
        return self._level == "CRITICAL"

    def price_score(self) -> int:
        """
        Return the cross-asset price-only score (excludes news_alerts component).
        Used by inject_news_state to check whether any price signal is confirming
        before amplifying the news bonus (market-reaction-first gate).
        """
        # MRI-THREAD-4: prevent RuntimeError on concurrent _components rewrite
        with self._lock:
            return sum(
                v.get("pts", 0)
                for k, v in self._components.items()
                if k != "news_alerts" and isinstance(v, dict)
            )

    def inject_news_state(
        self, alert_count: int, price_score: int | None = None
    ) -> None:
        """
        Market-reaction-first gate on news → MRI injection.

        _compute() uses only cross-asset price signals (VIX, TLT, JPY=X, etc.).
        News alerts may lead prices by minutes — this adds a calibrated bonus so
        MRI reflects confirmed macro news backdrop alongside price signals.

        GATE (board fix, Apr 15 2026):
          If price_score == 0 (ALL cross-asset components silent, i.e. the market
          is not reacting in any measurable way), the news bonus is capped at +10
          regardless of alert count.  This prevents Iran headline chatter from
          pushing MRI to ELEVATED/STRESSED on days when VIX is 18, credit is flat,
          TLT is flat, and SPY is hitting new ATHs — the exact false-positive
          pattern observed on Apr 15 2026 (MRI was driven 100% by news_alerts).

          If price_score > 0 (at least one price component confirming stress),
          the full bonus schedule applies — news amplifies confirmed market stress.

        Bonus schedule (full, when price_score > 0):
          1–2 alerts → +10 pts
          3–4 alerts → +20 pts
          5+  alerts → +35 pts

        Bonus schedule (capped, when price_score == 0):
          Any count  → +10 pts max  (mild caution; no market confirmation)
        """
        if alert_count < 0:
            return

        # Resolve price_score BEFORE acquiring lock (price_score() is itself
        # locked — MRI-THREAD-4). _ps is a snapshot at alert-arrival time.
        # A concurrent _compute() could rewrite price components in the TOCTOU
        # window, but gate logic is intentionally snapshot-based.
        _ps = price_score if price_score is not None else self.price_score()

        if alert_count >= 5:
            raw_bonus = 35
        elif alert_count >= 3:
            raw_bonus = 20
        elif alert_count > 0:
            raw_bonus = 10
        else:
            raw_bonus = 0  # alert_count == 0: clears stale bonus below

        # Market-reaction-first gate: cap bonus when price components are silent
        if alert_count > 0 and _ps == 0 and raw_bonus > 10:
            bonus = 10
            logger.info(
                f"MRI news injection GATED: {alert_count} alert(s) would add "
                f"+{raw_bonus}pts but price_score=0 (no market reaction) — "
                f"capped at +10pts. VIX/TLT/JPY/credit all silent."
            )
        else:
            bonus = raw_bonus

        # P2-INJECT-NEWS-LOCK: atomic write of _raw_score and _level
        with self._lock:
            _prior_news = self._components.get("news_alerts", {}).get("pts", 0)
            old_score = self._raw_score

            # MRI-ZERO-ALERT: clear stale news contribution when alerts subside
            if alert_count == 0:
                if _prior_news > 0:
                    self._raw_score = max(self._raw_score - _prior_news, 0)
                    self._level = _score_to_level(self._raw_score)
                    self._components.pop("news_alerts", None)
                    self._last_known_good_level = self._level
                    self._last_known_good_at    = datetime.now(ET)
                    logger.info(
                        f"MRI news bonus cleared ({_prior_news} pts) — "
                        f"alerts subsided ({old_score} → {self._raw_score}, "
                        f"level={self._level})"
                    )
                return

            # MRI-INJECT-OSCILLATION fix: idempotent subtract-prior + add-new.
            # _prior_news = self._components["news_alerts"]["pts"] from last inject.
            # Proof: inject(N) twice → score = (s - prior + bonus); second call:
            #   _prior_news = bonus → score = (s - bonus + bonus) = s. Idempotent.
            self._raw_score  = min(max(self._raw_score - _prior_news + bonus, 0), 100)
            self._level      = _score_to_level(self._raw_score)
            self._components["news_alerts"] = {
                "value": alert_count,
                "pts":   bonus,
                "gated": (_ps == 0 and raw_bonus > bonus),
            }
            self._last_known_good_level = self._level
            self._last_known_good_at    = datetime.now(ET)
            if self._raw_score != old_score:
                logger.info(
                    f"MRI news injection: {alert_count} macro alert(s) → "
                    f"+{bonus} pts ({old_score} → {self._raw_score}, "
                    f"level={self._level}, price_score={_ps})"
                )

    def components(self) -> dict:
        """Per-input point breakdown — for dashboard display."""
        # MRI-THREAD-5: prevent partial read during concurrent _components rewrite
        with self._lock:
            return dict(self._components)

    def summary(self) -> dict:
        """Full state dict for bot_status.json and dashboard."""
        return {
            "mri_score":       self._raw_score,
            "mri_level":       self._level,
            "mri_event":       self._event_type,
            "size_floor":      self.size_floor(),
            "min_score_floor": self.min_score_floor(),
            "components":      self._components,
            "last_refresh": (
                self._last_refresh.isoformat() if self._last_refresh else None
            ),
        }

    # ── Score computation ─────────────────────────────────────────────────────

    def _compute(self) -> None:
        """Fetch cross-asset prices and compute MRI score.

        Data sources:
          T1 (Alpaca Data) — US ETF session % via fetch_bars (TF_DAILY)
          T2 (FMP)         — ^VIX and USDJPY via stable/quote (5s timeout)
          T4 (yfinance)    — ^VIX3M only; 8s wall-clock cap via ThreadPoolExecutor

        Socket timeout: setdefaulttimeout(5.0) applied as defense-in-depth;
        the VIX3M background thread inherits this global 5s socket cap.
        """
        import yfinance as yf
        import pandas as _pd
        import socket as _mri_sock
        _mri_old_to = _mri_sock.getdefaulttimeout()
        _mri_sock.setdefaulttimeout(5.0)
        # P2-MRI-SOCKET: finally restores timeout even if _compute() raises
        try:

            components: dict[str, Any] = {}
            raw = 0

            # ── T1 helpers: US ETF data via Alpaca Data API ───────────────────

            def _alpaca_last_close(symbol: str) -> float | None:
                """Last daily close via fetch_bars (T1 Alpaca Data)."""
                try:
                    df = fetch_bars(symbol, config.TF_DAILY, num_bars=5)
                    if df is None or df.empty:
                        return None
                    return float(df["close"].dropna().iloc[-1])
                except Exception as e:
                    logger.debug(f"[MRI] _alpaca_last_close({symbol}): {e}")
                    return None

            def _alpaca_last_two_closes(symbol: str):
                """(current, prior) daily closes via fetch_bars (T1 Alpaca Data)."""
                try:
                    df = fetch_bars(symbol, config.TF_DAILY, num_bars=5)
                    if df is None or df.empty:
                        return None, None
                    closes = df["close"].dropna()
                    if len(closes) < 2:
                        return None, None
                    return float(closes.iloc[-1]), float(closes.iloc[-2])
                except Exception as e:
                    logger.debug(f"[MRI] _alpaca_last_two_closes({symbol}): {e}")
                    return None, None

            def _alpaca_session_pct(symbol: str) -> float | None:
                """Session % change from prior close (T1 Alpaca Data).

                Uses TF_DAILY num_bars=2: (today_close - yesterday_close) /
                yesterday_close. Daily bars update intraday as price moves.
                Fixes C-3: 15M first_open was unreliable mid-day (35 bars =
                8.75h window could start in pre-market or prior session).
                """
                try:
                    df = fetch_bars(symbol, config.TF_DAILY, num_bars=2)
                    if df is None or df.empty or len(df) < 2:
                        return None
                    prev_close = float(df["close"].dropna().iloc[-2])
                    curr_close = float(df["close"].dropna().iloc[-1])
                    if prev_close > 0:
                        return (curr_close - prev_close) / prev_close * 100
                    return None
                except Exception as e:
                    logger.debug(f"[MRI] _alpaca_session_pct({symbol}): {e}")
                    return None

            # ── T4 helper: ^VIX3M — yfinance only (not on Alpaca or FMP free) ──

            def _yf_last_close(ticker: str) -> float | None:
                try:
                    df = yf.download(
                        ticker, period="5d", interval="1d",
                        progress=False, auto_adjust=True,
                    )
                    if df is None or df.empty:
                        return None
                    if isinstance(getattr(df, "columns", None), _pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    # C-2: yfinance 0.2+ may flatten MultiIndex to ticker symbol
                    _col = "Close" if "Close" in df.columns else df.columns[0]
                    closes = df[_col].dropna()
                    return float(closes.values[-1]) if len(closes) > 0 else None
                except Exception as e:
                    logger.debug(f"[MRI] _yf_last_close({ticker}): {e}")
                    return None

            def _yf_last_close_safe(
                ticker: str, timeout: float = 8.0
            ) -> float | None:
                """8s wall-clock cap on _yf_last_close() via class-level executor.
                Uses self._yf_executor (created once in __init__, reused every cycle).
                Called once per _compute() for ^VIX3M only — the 10-min refresh
                interval ensures any prior timed-out thread has completed before the
                next call, so queue wait is always near-zero and timeout=8.0
                is effectively a wall-clock cap in practice.
                future.cancel() after TimeoutError is a no-op if thread is running;
                kept for intent documentation. Executor is NOT shut down here.
                """
                future = self._yf_executor.submit(_yf_last_close, ticker)
                try:
                    return future.result(timeout=timeout)
                except _cf.TimeoutError:
                    logger.warning(
                        f"MRI yfinance timeout: {ticker} did not respond in "
                        f"{timeout}s — scoring 0 pts for this component"
                    )
                    return None
                except Exception as e:
                    logger.warning(f"MRI yfinance error [{ticker}]: {e}")
                    return None
                finally:
                    future.cancel()
                    # Executor persists across refresh cycles — do NOT shutdown here.

            # ── T2 helpers: ^VIX and USDJPY via FMP stable/quote ─────────────

            def _fmp_quote(symbol: str) -> float | None:
                """FMP stable/quote — current price or None on any failure."""
                _key = os.getenv("FMP_API_KEY", "")
                if not _key:
                    logger.warning(f"FMP_API_KEY not set — {symbol} scoring 0")
                    return None
                try:
                    r = _req.get(
                        "https://financialmodelingprep.com/stable/quote",
                        params={"symbol": symbol, "apikey": _key},
                        timeout=5.0,
                    )
                    if r.status_code != 200:
                        logger.debug(
                            f"FMP quote [{symbol}]: HTTP {r.status_code}"
                        )
                        return None
                    data = r.json()
                    if not isinstance(data, list) or not data:
                        return None
                    return float(data[0]["price"])
                except Exception as e:
                    logger.debug(f"FMP quote [{symbol}] error: {e}")
                    return None

            def _fmp_last_two_closes(
                symbol: str,
            ) -> tuple[float | None, float | None]:
                """FMP stable/quote — (price, previousClose) or (None, None)."""
                _key = os.getenv("FMP_API_KEY", "")
                if not _key:
                    logger.warning(
                        f"FMP_API_KEY not set — {symbol} two-closes unavailable"
                    )
                    return None, None
                try:
                    r = _req.get(
                        "https://financialmodelingprep.com/stable/quote",
                        params={"symbol": symbol, "apikey": _key},
                        timeout=5.0,
                    )
                    if r.status_code != 200:
                        logger.debug(
                            f"FMP quote [{symbol}]: HTTP {r.status_code}"
                        )
                        return None, None
                    data = r.json()
                    if not isinstance(data, list) or not data:
                        return None, None
                    q = data[0]
                    cur = q.get("price")
                    pri = q.get("previousClose")
                    if cur is None or pri is None:
                        return None, None
                    return float(cur), float(pri)
                except Exception as e:
                    logger.debug(f"FMP two-closes [{symbol}] error: {e}")
                    return None, None

            # ── 1. VIX LEVEL (0–30 pts) ───────────────────────────────────────
            vix_val = _fmp_quote("^VIX")   # T2 — FMP stable/quote
            if vix_val is not None:
                if vix_val > 30:
                    vix_pts = 30
                elif vix_val > 25:
                    vix_pts = 20
                elif vix_val > 20:
                    vix_pts = 10
                elif vix_val >= 15:
                    # Linear interpolation: VIX 15→20 maps 0→10 pts.
                    # int(round()) preserves int type contract for score() and
                    # components["pts"]. max/min clamp guards float precision at
                    # boundaries. Board vote: 27-0 YES (2026-04-30)
                    vix_pts = max(
                        0, min(10, int(round((vix_val - 15) / 5 * 10)))
                    )
                else:
                    vix_pts = 0
                components["vix"] = {"value": round(vix_val, 1), "pts": vix_pts}
                raw += vix_pts
            else:
                components["vix"] = {"value": None, "pts": 0}

            # ── 2. VIX TERM STRUCTURE (0–20 pts) ─────────────────────────────
            vix3m_val = _yf_last_close_safe("^VIX3M")   # T4 — 8s timeout wrapper
            if vix_val is not None and vix3m_val is not None and vix3m_val > 0:
                ratio = vix_val / vix3m_val
                if ratio > 1.10:
                    ts_pts = 20   # full inversion — near-term fear > medium-term
                elif ratio > 1.05:
                    ts_pts = 10   # mild inversion
                else:
                    ts_pts = 0
                components["vix_term"] = {"value": round(ratio, 3), "pts": ts_pts}
                raw += ts_pts
            else:
                components["vix_term"] = {"value": None, "pts": 0}

            # ── 3. JPY CARRY UNWIND (0–15 pts) ───────────────────────────────
            # USD/JPY: lower value = JPY strengthening = risk-off carry unwind
            # (Brunnermeier, Nagel & Pedersen 2008 — carry trades as leading
            # indicator of risk appetite)
            usd_jpy_now, usd_jpy_prior = _fmp_last_two_closes("USDJPY")  # T2
            if (
                usd_jpy_now is not None
                and usd_jpy_prior is not None
                and usd_jpy_prior > 0
            ):
                jpy_pct = (usd_jpy_now - usd_jpy_prior) / usd_jpy_prior * 100
                # Negative = USD/JPY falling = JPY strengthening = risk-off
                if jpy_pct < -0.8:
                    jpy_pts = 15
                elif jpy_pct < -0.4:
                    jpy_pts = 10
                else:
                    jpy_pts = 0
                components["jpy"] = {
                    "value": round(jpy_pct, 2),
                    "pts":   jpy_pts,
                    "note":  "USD/JPY daily % chg (negative = JPY strength = risk-off)",
                }
                raw += jpy_pts
            else:
                components["jpy"] = {"value": None, "pts": 0}

            # ── 4. TLT FLIGHT TO SAFETY (0–15 pts) ───────────────────────────
            # TLT rising while equities sell off = classic risk-off flight.
            # Signal requires TLT up AND SPY down — pure TLT rise may be an
            # inflation expectation change (different dynamic).
            tlt_pct  = _alpaca_session_pct("TLT")   # T1 — Alpaca Data API
            spy_pct  = _alpaca_session_pct("SPY")   # T1 — Alpaca Data API
            if tlt_pct is not None:
                if tlt_pct > 1.0 and (spy_pct is None or spy_pct < 0):
                    tlt_pts = 15
                elif tlt_pct > 0.5 and (spy_pct is None or spy_pct < 0):
                    tlt_pts = 10
                else:
                    tlt_pts = 0
                components["tlt"] = {
                    "value": round(tlt_pct, 2),
                    "pts":   tlt_pts,
                    "note":  "TLT session % chg (confirmed with SPY direction)",
                }
                raw += tlt_pts
            else:
                components["tlt"] = {"value": None, "pts": 0}

            # ── 5. CREDIT SPREAD — HYG vs LQD (0–10 pts) ────────────────────
            # High yield underperforming investment grade = credit stress.
            # HYG - LQD divergence: smart money pricing in default risk ahead.
            hyg_pct = _alpaca_session_pct("HYG")   # T1 — Alpaca Data API
            lqd_pct = _alpaca_session_pct("LQD")   # T1 — Alpaca Data API
            if hyg_pct is not None and lqd_pct is not None:
                spread = hyg_pct - lqd_pct
                if spread < -0.5:
                    credit_pts = 10
                elif spread < -0.3:
                    credit_pts = 5
                else:
                    credit_pts = 0
                components["credit"] = {
                    "value": round(spread, 2),
                    "pts":   credit_pts,
                    "note":  "HYG - LQD session pct divergence",
                }
                raw += credit_pts
            else:
                components["credit"] = {"value": None, "pts": 0}

            # ── 6. OIL SHOCK — USO (0–8 pts) ─────────────────────────────────
            # Oil surging while equities fall = supply shock / geopolitical risk.
            # Oil up with equities up = demand recovery — not a stress signal.
            uso_pct = _alpaca_session_pct("USO")   # T1 — Alpaca Data API
            if uso_pct is not None:
                if uso_pct > 4.0 and (spy_pct is None or spy_pct < 0):
                    oil_pts = 8
                elif uso_pct > 2.0 and (spy_pct is None or spy_pct < 0):
                    oil_pts = 5
                else:
                    oil_pts = 0
                components["oil"] = {
                    "value": round(uso_pct, 2),
                    "pts":   oil_pts,
                    "note":  "USO session % chg (confirmed with SPY direction)",
                }
                raw += oil_pts
            else:
                components["oil"] = {"value": None, "pts": 0}

            # ── 7. GOLD FLIGHT (0–5 pts) ──────────────────────────────────────
            # Gold rising with equities falling = tail/geopolitical fear.
            gld_pct = _alpaca_session_pct("GLD")   # T1 — Alpaca Data API
            if gld_pct is not None:
                if gld_pct > 0.5 and (spy_pct is None or spy_pct < 0):
                    gold_pts = 5
                else:
                    gold_pts = 0
                components["gold"] = {
                    "value": round(gld_pct, 2),
                    "pts":   gold_pts,
                    "note":  "GLD session % chg (confirmed with SPY direction)",
                }
                raw += gold_pts
            else:
                components["gold"] = {"value": None, "pts": 0}

            # ── 8. GLOBAL PRE-OPEN / INTRADAY (0–8 pts) ──────────────────────
            # EWJ = iShares Japan ETF  (Nikkei proxy, available during US hours)
            # EWG = iShares Germany ETF (DAX proxy, available during US hours)
            # Pre-market or intraday these reflect prior Asia/EU session.
            ewj_pct = _alpaca_session_pct("EWJ")   # T1 — Alpaca Data API
            ewg_pct = _alpaca_session_pct("EWG")   # T1 — Alpaca Data API
            global_pts = 0
            global_note = []
            if ewj_pct is None and ewg_pct is None:
                logger.warning(
                    "MRI global component: EWJ and EWG both returned None — "
                    "Alpaca Data unavailable for both tickers. "
                    "Global leg scoring 0 pts this refresh."
                )
            for ticker, pct in (("EWJ", ewj_pct), ("EWG", ewg_pct)):
                if pct is not None and pct < -3.0:
                    global_pts = max(global_pts, 8)
                    global_note.append(f"{ticker} {pct:+.1f}%")
                elif pct is not None and pct < -1.5:
                    global_pts = max(global_pts, 5)
                    global_note.append(f"{ticker} {pct:+.1f}%")
            components["global"] = {
                "value": global_note or None,
                "pts":   global_pts,
                "note":  "EWJ/EWG session % chg",
            }
            raw += global_pts

            # ── Final score + level ───────────────────────────────────────────
            if not any(
                isinstance(v, dict) and v.get("value") is not None
                for k, v in components.items()
            ):
                raise ConnectionError(
                    "MRI: all cross-asset feeds returned None — "
                    "network or API outage"
                )

            # MRI-COMPUTE-LOCK: prevent race with inject_news_state()
            with self._lock:
                # MRI-NEWS-CARRY: news_alerts not recomputed by _compute();
                # carry forward from prior state so news elevation persists
                # across 15-min refresh cycles. Bounded by: (a) inject(0)
                # clear, (b) nightly restart.
                _prior_news = self._components.get("news_alerts", {}).get("pts", 0)
                self._raw_score  = min(raw + _prior_news, 100)
                self._level      = _score_to_level(self._raw_score)
                if "news_alerts" in self._components:
                    components["news_alerts"] = self._components["news_alerts"]
                self._components = components
                self._event_type = self._classify_event_type()
                self._last_known_good_level = self._level
                self._last_known_good_at    = datetime.now(ET)
        finally:
            _mri_sock.setdefaulttimeout(_mri_old_to)

    def _classify_event_type(self) -> str:
        """
        Determine dominant risk domain from component contributions.
        Priority: SYSTEMIC > GEO_CONFLICT > GEO_ENERGY > MACRO_CREDIT >
                  MACRO_FX > MACRO_MONETARY > GLOBAL > TECHNICAL
        """
        c = self._components

        # Systemic: multiple components in extreme territory simultaneously
        active = sum(
            1 for v in c.values()
            if isinstance(v, dict) and v.get("pts", 0) >= 8
        )
        if active >= 4:
            return "MACRO_SYSTEMIC"

        # H-1: Score each eligible domain; return highest-scoring driver.
        # Prior first-match logic returned GEO_ENERGY before MACRO_CREDIT even
        # when credit pts > oil pts (Dalio: dominant driver governs labeling).
        scores: dict[str, int] = {}
        if c.get("oil", {}).get("pts", 0) >= 5:
            scores["GEO_ENERGY"] = c["oil"]["pts"]
        if (
            c.get("jpy", {}).get("pts", 0) >= 10
            and c.get("gold", {}).get("pts", 0) >= 5
        ):
            scores["GEO_CONFLICT"] = c["jpy"]["pts"] + c["gold"]["pts"]
        if c.get("credit", {}).get("pts", 0) >= 5:
            scores["MACRO_CREDIT"] = c["credit"]["pts"]
        if c.get("jpy", {}).get("pts", 0) >= 10:
            scores["MACRO_FX"] = c["jpy"]["pts"]
        if c.get("global", {}).get("pts", 0) >= 5:
            _gkey = (
                "GLOBAL_ASIA"
                if c.get("jpy", {}).get("pts", 0) >= 5
                else "GLOBAL_EU"
            )
            scores[_gkey] = c["global"]["pts"]
        if c.get("tlt", {}).get("pts", 0) >= 10:
            scores["MACRO_MONETARY"] = c["tlt"]["pts"]

        if not scores:
            return "TECHNICAL"
        return max(scores, key=lambda k: scores[k])

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        """Write current MRI state to logs/mri_state.json atomically."""
        try:
            # MRI-PERSIST-LOCK: snapshot state atomically before I/O
            with self._lock:
                state = {
                    "score":      self._raw_score,
                    "level":      self._level,
                    "event_type": self._event_type,
                    "components": dict(self._components),
                    "saved_at":   datetime.now(ET).isoformat(),
                }
            # Write OUTSIDE lock — I/O must not block inject_news_state()
            tmp = MRI_STATE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(str(tmp), str(MRI_STATE))
        except Exception as e:
            logger.warning(f"MRI persist failed: {e}")

    def _restore(self) -> None:
        """
        Restore MRI state from disk on startup.
        Applies time-decay: -10 pts per hour since last save (max 20 hours).
        After >20 hours downtime, starts fresh (stale macro state is noise).
        """
        try:
            if not MRI_STATE.exists():
                self._level                 = "CRITICAL"
                self._last_known_good_level = "CRITICAL"
                self._last_known_good_at    = datetime.now(ET)
                return
            with open(MRI_STATE, encoding="utf-8") as f:
                state = json.load(f)

            saved_at_str = state.get("saved_at")
            if not saved_at_str:
                return

            saved_at   = datetime.fromisoformat(saved_at_str)
            now        = datetime.now(ET)
            hours_down = (now - saved_at).total_seconds() / 3600

            if hours_down > 20.0:
                logger.warning(
                    f"MRI state {hours_down:.1f}h stale (>20h) — starting fresh."
                )
                self._level                 = "CRITICAL"
                self._last_known_good_level = "CRITICAL"
                self._last_known_good_at    = datetime.now(ET)
                return

            # C-1: round() not int() — int() truncates, understating decay for
            # fractional hours.
            decay = round(hours_down * 10)
            # MRI-RESTORE-PURGE: strip news bonus from raw_saved BEFORE decay.
            # Decay must apply to the price-only score. Without this subtraction,
            # decay is applied to a news-inclusive total, then news_alerts is
            # purged below, leaving _raw_score > sum(_components).
            # (Board finding: Data Integrity + Quant Logic, 2026-04-29)
            _prior_news_pts = (
                state.get("components", {}).get("news_alerts", {}).get("pts", 0)
            )
            raw_saved   = int(state.get("score", 0)) - _prior_news_pts
            # P5-M2: retain ≥30% — preserves directional signal overnight
            _floor      = int(raw_saved * 0.30)
            raw_decayed = max(_floor, raw_saved - decay)

            self._raw_score  = raw_decayed
            self._level      = _score_to_level(raw_decayed)
            self._last_known_good_level = self._level
            self._last_known_good_at    = saved_at
            # M-3: restore components first, then reclassify event_type from
            # actual post-decay state (prior order set event_type before
            # components existed).
            self._components = state.get("components", {})
            # MRI-RESTORE-PURGE: news is ephemeral — re-injected on startup
            self._components.pop("news_alerts", None)
            self._event_type = self._classify_event_type()

            logger.info(
                f"MRI restored: {raw_saved} → {raw_decayed} "
                f"(decayed {decay}pts over {hours_down:.1f}h) | "
                f"level={self._level}"
            )
        except Exception as e:
            logger.debug(f"MRI restore failed: {e}")
