# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
data/live_price.py — the CURRENT price of a symbol from the plan's real-time feed (T1 Alpaca Data API).

WHY (2026-10-05, design record logs/design_records/main_bot_live_price_feed_2026-10-05.md; board Harris + McKinney
seats + Gro + GAI aligned on this helper): data.fetcher.fetch_bars() sends no feed, and on this account's data plan
the server default is consolidated SIP, which is only served once it is ~15 minutes old. Any caller that reads
fetch_bars()' last close as "the price now" is therefore ~15 minutes behind. The plan's REAL-TIME feed is IEX.

    live_price(symbol) -> LivePrice(price, source, age_s)

Order of sources (first fresh one wins):
  1. "iex_1m"   — close of the newest IEX 1-minute bar (fetch_bars_window(feed="iex")), if that bar is no more than
                  _MAX_BAR_AGE_S old. The newest bar may still be forming: that is intended — this is a PRICE read,
                  not a signal; signal code must keep using completed bars only.
  2. "iex_trade"— the latest IEX trade print (data.alpaca_data.get_latest_trade_with_time), held to the SAME age
                  limit (an IEX print can be much older than the delayed bar on a thin name — never prefer it then).
  3. None       — neither available. The CALLER decides; an exit/stop evaluation must fall back to its previous
                  source (never skip a stop check because the real-time read failed) and log a WARNING.

A short per-symbol cache (_CACHE_TTL_S) bounds the extra API load when several checks read the same symbol in one
cycle. Never raises. Prices are RAW (unadjusted), the same basis as broker fills and the existing price checks.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

_MAX_BAR_AGE_S = 180.0    # PROV:main-bot-live-price-2026-10-05 — newest IEX 1m bar must have STARTED within 3 min
_LOOKBACK_MIN = 10        # PROV:main-bot-live-price-2026-10-05 — window of IEX 1m bars requested
_CACHE_TTL_S = 15.0       # PROV:main-bot-live-price-2026-10-05 — per-symbol reuse window inside one cycle
_CLOCK_SKEW_S = 5.0       # PROV:main-bot-live-price-2026-10-05 — tolerate a slightly-behind local clock (negative age)

_cache: dict = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class LivePrice:
    price: float
    source: str                 # "iex_1m" | "iex_trade"
    age_s: float                # seconds since the source bar STARTED / the trade printed


def _valid(p) -> bool:
    try:
        return math.isfinite(float(p)) and float(p) > 0
    except (TypeError, ValueError):
        return False


def _from_iex_bars(symbol: str, now: datetime) -> "LivePrice | None":
    from data.fetcher import fetch_bars_window
    df = fetch_bars_window(symbol, "1Min", now - timedelta(minutes=_LOOKBACK_MIN), now, feed="iex")
    if df is None or getattr(df, "empty", True) or "close" not in df.columns:
        return None
    ts = df.index[-1]
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if getattr(ts, "tzinfo", None) is None:
        return None                       # a naive timestamp cannot be aged safely
    age = (now - ts).total_seconds()
    px = df["close"].iloc[-1]
    if not (-_CLOCK_SKEW_S <= age <= _MAX_BAR_AGE_S) or not _valid(px):
        return None
    return LivePrice(float(px), "iex_1m", round(max(age, 0.0), 1))


def _from_latest_trade(symbol: str, now: datetime) -> "LivePrice | None":
    from data.alpaca_data import get_latest_trade_with_time
    got = get_latest_trade_with_time(symbol)
    if not got:
        return None
    px, ts = got
    age = (now - ts).total_seconds()
    if not (-_CLOCK_SKEW_S <= age <= _MAX_BAR_AGE_S) or not _valid(px):
        return None                      # an old print is never preferred over the caller's own source
    return LivePrice(float(px), "iex_trade", round(max(age, 0.0), 1))


def live_price(symbol: str, now: "datetime | None" = None) -> "LivePrice | None":
    """The symbol's current price from the real-time feed, or None. Never raises."""
    try:
        use_cache = now is None             # an explicit `now` (replay/test) never reads or writes the live cache
        now = now if now is not None else datetime.now(_ET)
        mono = time.monotonic()
        if use_cache:
            with _lock:
                hit = _cache.get(symbol)
                if hit is not None and mono - hit[1] < _CACHE_TTL_S:
                    return hit[0]
        out = None
        try:
            out = _from_iex_bars(symbol, now)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] live_price: IEX bar read failed: %s", symbol, e)
        if out is None:
            try:
                out = _from_latest_trade(symbol, now)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] live_price: latest-trade read failed: %s", symbol, e)
        if out is not None and use_cache:
            with _lock:
                _cache[symbol] = (out, mono)
        return out
    except Exception as e:  # noqa: BLE001 — a price read must never raise into a trading path
        logger.warning("[%s] live_price error: %s", symbol, e)
        return None


def live_price_or(symbol: str, fallback: "float | None", where: str) -> "tuple[float | None, str]":
    """Real-time price if available, else `fallback` (the caller's previous source) with a WARNING.
    Returns (price, source_label). Use this at exit/stop checks: a failed real-time read must never skip a stop
    evaluation, so the old value is still returned when nothing fresher exists."""
    lp = live_price(symbol)
    if lp is not None:
        return lp.price, lp.source
    logger.warning("[%s] %s: real-time price unavailable — using the delayed fallback %s", symbol, where, fallback)
    return fallback, "delayed_fallback"
