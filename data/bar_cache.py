"""
data/bar_cache.py
Per-symbol daily ATR cache — extracted from main.py Phase 2.

Stores daily_df alongside computed ATR values so FVG, HTF, and ATR-compression
checks all work correctly on cache hits (Finding 7, main.py 2026-04-18).

TTL: ATR_CACHE_TTL seconds (default 4 h). Pruned at daily reset (RAM-DRAIN-1, 2026-04-30).

Usage::

    from data.bar_cache import get_atr, set_atr, prune_atr

    cached = get_atr("AAPL")
    if cached:
        atr_value = cached["atr_pct"] * entry_price
    else:
        set_atr("AAPL", atr_pct=0.012, rvol_20d=0.18, daily_df=df)
"""

import logging
import time

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ATR_CACHE_TTL: int = 4 * 3600  # 4 hours in seconds (RAM-DRAIN-1 board vote 2026-04-30)

# ---------------------------------------------------------------------------
# Internal cache store — single module-level dict, single writer (execute_entries)
# ---------------------------------------------------------------------------

_cache: dict = {}  # symbol → {"atr_pct": float, "rvol_20d": float, "daily_df": df, "ts": float}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_atr(symbol: str) -> dict | None:
    """Return cached ATR entry for symbol if still within TTL, else None.

    Returns the raw dict so callers can access atr_pct, rvol_20d, daily_df directly.
    """
    entry = _cache.get(symbol)
    if entry and (time.time() - entry["ts"]) < ATR_CACHE_TTL:
        return entry
    return None


def set_atr(symbol: str, atr_pct: float, rvol_20d: float, daily_df) -> None:
    """Store ATR cache entry for symbol with current timestamp."""
    _cache[symbol] = {
        "atr_pct":  atr_pct,
        "rvol_20d": rvol_20d,
        "daily_df": daily_df,
        "ts":       time.time(),
    }


def prune_atr(now_ts: float | None = None) -> int:
    """Remove expired cache entries. Returns count pruned.

    Called at daily reset (RAM-DRAIN-1). Accepts optional timestamp for
    deterministic testing.
    """
    if now_ts is None:
        now_ts = time.time()
    expired = [k for k, v in list(_cache.items())
               if (now_ts - v.get("ts", 0)) > ATR_CACHE_TTL]
    for k in expired:
        del _cache[k]
    if expired:
        logger.debug(
            f"ATR cache pruned: {len(expired)} expired entries removed at daily reset"
        )
    return len(expired)


def clear_atr() -> None:
    """Clear all ATR cache entries (e.g., at end of session or test teardown)."""
    _cache.clear()


def cache_size() -> int:
    """Return number of currently cached symbols."""
    return len(_cache)
