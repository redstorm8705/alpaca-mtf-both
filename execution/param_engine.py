"""
execution/param_engine.py — Dynamic Parameter Engine (DPE)
Phase 1: rv_cache + overnight ATR buffer multiplier (H1)

Single source of truth for all dynamic parameters. Replaces static config constant
lookups with function calls that respond to live market primitives (VIX, per-symbol
realized vol, MRI level, breadth).

Market primitives (Phase 1):
  - vix_current  : VIX spot passed in by caller (from macro_risk_index.py / main.py)
  - rv_per_symbol: 20-day annualized realized vol, per-symbol, 1h TTL cache
  - mri_level    : current MRI level string, passed by caller from main.py

Planned primitives (Phase 2+):
  - vix_percentile : VIX vs its own 252-day history
  - breadth_score  : pct of S&P500 above 50-DMA
  - rolling_sharpe : 20-trade rolling Sharpe from kelly.py stats
  - spy_trend_quality: bars above 20-EMA ratio

Refresh schedule:
  - Pre-market (8:45 AM ET): prefetch_rv_batch() for all open symbols
  - Each cycle: caller passes vix_current + mri_level (already computed upstream)
  - 1h TTL: cache expires and re-fetches on the next call naturally
  - param_refresh events to be wired into trade_events.jsonl in H1 trade_engine patch

Data source: T1 (Alpaca Data) via data/fetcher.py — no new external API.

Thread safety: GIL-atomic dict ops. Cache writes are idempotent; race conditions
cause at most duplicate computation, not corruption. Not designed for multiprocessing.
"""

import math
import time
import logging

import config
from data.fetcher import fetch_bars

logger = logging.getLogger("param_engine")

# ─── Realized Vol Cache ────────────────────────────────────────────────────────
# symbol → (rv_annualized: float, fetched_at: float [time.time()])
_RV_CACHE: dict[str, tuple[float, float]] = {}
_RV_CACHE_TTL_SECS: float = 3600.0  # 1h TTL — re-fetch once per session window

# ─── H1: Overnight ATR Buffer Constants ───────────────────────────────────────
_RV_BASELINE: float = 0.20           # 20% ann. vol = "normal" regime baseline
_RV_SCALAR_CAP: float = 2.0          # cap: high-vol symbol never gets > 2× mult
_RV_SCALAR_FLOOR: float = 1.0        # floor: never shrink buffer below VIX-based mult
_OVERNIGHT_BE_MAX_ATR_MULT: float = 0.65  # Taleb cap (board-voted): ceiling = 0.65×ATR

# Number of daily bars to fetch: 21 bars → 20 log returns
_RV_DAILY_BARS: int = 21


# ─── Realized Vol Computation ──────────────────────────────────────────────────

def _compute_rv(symbol: str) -> float | None:
    """
    Fetch 21 daily bars, compute 20 daily log returns, annualize (×√252).

    Returns annualized realized vol as a decimal (e.g. 0.35 = 35% ann. vol).
    Returns _RV_BASELINE when bars are insufficient (sparse data, IPO) — valid result.
    Returns None on exception — caller uses stale cache rather than collapsing to
    baseline.

    Data source: T1 Alpaca Data via data/fetcher.fetch_bars (config.TF_DAILY).
    NaN/zero closes are filtered before log computation to prevent silent propagation.
    """
    try:
        df = fetch_bars(symbol, config.TF_DAILY, num_bars=_RV_DAILY_BARS)
        if df.empty or len(df) < 2:
            logger.warning(
                "[%s] rv_compute: insufficient bars (%d) — using baseline %.2f",
                symbol, len(df), _RV_BASELINE,
            )
            return _RV_BASELINE

        # Filter NaN and non-positive closes — prevents silent nan propagation
        closes_raw = df["close"].values
        closes = [c for c in closes_raw if not math.isnan(float(c)) and c > 0]
        if len(closes) < 2:
            logger.warning(
                "[%s] rv_compute: < 2 valid closes after NaN/zero filter "
                "(raw=%d, valid=%d) — using baseline %.2f",
                symbol, len(closes_raw), len(closes), _RV_BASELINE,
            )
            return _RV_BASELINE

        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
        ]
        n = len(log_returns)
        if n < 2:
            logger.warning(
                "[%s] rv_compute: < 2 log returns (n=%d) — using baseline %.2f",
                symbol, n, _RV_BASELINE,
            )
            return _RV_BASELINE

        mean = sum(log_returns) / n
        variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
        rv_daily = math.sqrt(variance)
        rv_annualized = rv_daily * math.sqrt(252)
        logger.debug(
            "[%s] rv_annualized=%.4f (n=%d log returns)", symbol, rv_annualized, n,
        )
        return rv_annualized

    except Exception as exc:
        logger.warning(
            "[%s] rv_compute failed [%s]: %s",
            symbol, type(exc).__name__, exc,
        )
        return None  # sentinel — caller decides whether to use stale cache


def get_realized_vol(symbol: str, *, force_refresh: bool = False) -> float:
    """
    Return cached 20-day annualized realized vol for symbol.

    Re-fetches if cache is stale (> 1h TTL) or force_refresh=True.
    On fetch failure (None from _compute_rv): returns stale cache value if available,
    else _RV_BASELINE. This prevents a transient API error from collapsing a valid
    cached rv=0.60 to baseline=0.20 and shrinking overnight stops mid-session.
    Always returns a positive float.
    """
    now = time.time()
    cached = _RV_CACHE.get(symbol)
    if cached is not None and not force_refresh:
        rv, fetched_at = cached
        if now - fetched_at < _RV_CACHE_TTL_SECS:
            return rv

    rv_new = _compute_rv(symbol)

    if rv_new is None:
        # Fetch failed — prefer stale cache over collapsing to baseline
        if cached is not None:
            stale_rv, stale_ts = cached
            logger.warning(
                "[%s] rv fetch failed — using stale cache (%.1f min old)",
                symbol, (now - stale_ts) / 60,
            )
            return stale_rv
        logger.warning(
            "[%s] rv fetch failed, no cache — using baseline %.2f",
            symbol, _RV_BASELINE,
        )
        return _RV_BASELINE

    _RV_CACHE[symbol] = (rv_new, now)
    return rv_new


def prefetch_rv_batch(symbols: list[str]) -> None:
    """
    Pre-compute realized vol for all symbols in batch (pre-market warm-up).

    Called by main.py at 8:45 AM ET before RTH opens.
    Each fetch is forced (bypasses TTL) so the cache is fresh at open.
    Ensures no synchronous fetch_bars calls block the RTH check_exits hot loop.
    """
    logger.info("DPE rv_prefetch: computing realized vol for %d symbols", len(symbols))
    for sym in symbols:
        rv = get_realized_vol(sym, force_refresh=True)
        logger.debug("[%s] rv_prefetch complete → rv_annualized=%.4f", sym, rv)
    logger.info("DPE rv_prefetch complete")


# ─── H1: Overnight ATR Buffer Multiplier ──────────────────────────────────────

def get_be_buffer_mult(symbol: str, last_vix: float) -> float:
    """
    Dynamic overnight ATR buffer multiplier for the overnight_atr_buffer_exit gate.

    Formula (board-approved 2026-05-11):
      Step 1 — VIX-based base mult (preserves existing config thresholds):
          0.50  if VIX >= config.VIX_BE_WIDEN_THRESHOLD_2  (default 30)
          0.40  if VIX >= config.VIX_BE_WIDEN_THRESHOLD_1  (default 20)
          0.25  otherwise (normal regime)

      Step 2 — Per-symbol realized vol scalar:
          rv_scalar = clip(rv_annualized / _RV_BASELINE,
                           _RV_SCALAR_FLOOR, _RV_SCALAR_CAP)
                    = clip(rv / 0.20, 1.0, 2.0)
          Floor 1.0: never shrink buffer below VIX-based base mult
          Cap   2.0: prevent runaway buffers on extreme-vol names

      Step 3 — Taleb ceiling (board-voted 0.65×ATR max):
          be_mult_adj = min(be_mult × rv_scalar, _OVERNIGHT_BE_MAX_ATR_MULT)
          Ceiling is correct: this gate is an early-exit trigger that must fire
          before the hard stop (1.25×ATR). min() enforces that invariant.

    Returns: float in [0.25, 0.65]
    Examples:
      NVDA (rv≈0.50, VIX=18):  base=0.25, rv_scalar=2.0, adj=min(0.50, 0.65)=0.50
      SPY  (rv≈0.15, VIX=22):  base=0.40, rv_scalar=1.0, adj=min(0.40, 0.65)=0.40
      TSLA (rv≈0.80, VIX=28):  base=0.40, rv_scalar=2.0, adj=min(0.80, 0.65)=0.65
    """
    # Step 1: VIX-based base mult (config thresholds, unchanged from trade_engine.py)
    be_mult = (
        0.50 if last_vix >= config.VIX_BE_WIDEN_THRESHOLD_2
        else 0.40 if last_vix >= config.VIX_BE_WIDEN_THRESHOLD_1
        else 0.25
    )
    # Step 2: Per-symbol rv scalar
    rv = get_realized_vol(symbol)
    rv_scalar = max(_RV_SCALAR_FLOOR, min(rv / _RV_BASELINE, _RV_SCALAR_CAP))
    # Step 3: Taleb ceiling
    be_mult_adj = min(be_mult * rv_scalar, _OVERNIGHT_BE_MAX_ATR_MULT)

    logger.debug(
        "[%s] get_be_buffer_mult: VIX=%.2f base=%.2f rv=%.4f "
        "rv_scalar=%.3f adj=%.3f",
        symbol, last_vix, be_mult, rv, rv_scalar, be_mult_adj,
    )
    return be_mult_adj


# ─── Cache Introspection (diagnostics only — not called during RTH) ───────────

def rv_cache_summary() -> dict[str, float]:
    """Return {symbol: rv_annualized} for all cached symbols. Diagnostics only."""
    return {sym: rv for sym, (rv, _) in _RV_CACHE.items()}


def rv_cache_age(symbol: str) -> float:
    """Return seconds since last rv fetch for symbol, or inf if not cached."""
    cached = _RV_CACHE.get(symbol)
    if cached is None:
        return float("inf")
    _, fetched_at = cached
    return time.time() - fetched_at


# ─── H2: Intraday Stop ATR Multiplier — Continuous Curve ──────────────────────

def h2_stop_atr_mult(symbol: str, vix: float) -> float | None:
    """
    Pure VIX+RV scalar for the intraday stop ATR multiplier (H2 continuous curve).

    Returns None when VIX is unavailable (vix <= 0.01) so risk_manager keeps full
    native control (existing step-function logic runs unchanged). Caller passes the
    return value as atr_mult_override kwarg to get_stop_and_target().

    Returns a dimensionless scalar — risk_manager multiplies its mode-aware
    stop_mult by this value, preserving intraday vs. swing trade_mode base.

    Formula (board-approved 2026-05-16):
      vix_factor: piecewise linear, preserves regime breakpoints at VIX 25/30
        VIX <= 20:  1.0 (no widening in normal regime)
        VIX 20-30: 1.0 + (vix-20)/20  ->  1.0 -> 1.5
        VIX > 30:  1.5 + min((vix-30)/20, 0.5)  ->  1.5 -> 2.0 (capped at VIX=50)
      rv_factor: min(rv / _RV_BASELINE, 1.5)  —  1.0 -> 1.5 as symbol vol rises
      scalar = min(vix_factor * rv_factor, 2.5)  —  hard cap enforced

    Examples (rv=0.20 baseline, rv_factor=1.0):
      VIX=15: scalar=1.0  |  VIX=25: scalar=1.25  |  VIX=30: scalar=1.5
      VIX=40: scalar=2.0  |  VIX=50+: scalar=2.0 (cap)
    """
    if vix <= 0.01:
        return None
    rv = get_realized_vol(symbol)
    if vix <= 20:
        vix_factor = 1.0
    elif vix <= 30:
        vix_factor = 1.0 + (vix - 20.0) / 20.0
    else:
        vix_factor = 1.5 + min((vix - 30.0) / 20.0, 0.5)
    rv_factor = min(rv / _RV_BASELINE, 1.5)
    scalar = min(vix_factor * rv_factor, 2.5)
    logger.debug(
        "[%s] h2_stop_atr_mult: VIX=%.1f rv=%.3f vix_f=%.3f rv_f=%.3f -> scalar=%.4f",
        symbol, vix, rv, vix_factor, rv_factor, scalar,
    )
    return scalar


# ─── H4: Entry Confirmation Scan Count — VIX-Adjusted ────────────────────────

def h4_entry_confirm_scans(vix: float, is_bucket_a: bool) -> int:
    """
    VIX-adjusted entry confirmation scan count (H4: smooth linear ramp).

    Replaces static `_min_confirm = 3 if is_bucket_a else 2` in execute_entries().
    Harris/Brandt: smooth curve avoids hard step discontinuity at VIX=25/30.
    Thorp: hard cap at base+2 prevents entry paralysis.

    VIX <= 22:  base (no extra scans)
    VIX 22-35:  smooth linear ramp from 0 to 2 extra scans
    VIX >= 35:  base + 2 (maximum)
    vix <= 0.01: base (VIX unavailable — no change)

    Returns: int in [2, 4] for Bucket B, [3, 5] for Bucket A
    """
    base = 3 if is_bucket_a else 2
    if vix <= 0.01:
        return base
    if vix >= 35:
        extra = 2
    elif vix >= 22:
        extra = round((vix - 22) / (35 - 22) * 2)
    else:
        extra = 0
    return min(base + extra, base + 2)
