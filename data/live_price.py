# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
data/live_price.py — the CURRENT price of a symbol from the plan's real-time feed (T1 Alpaca Data API).

WHY (2026-10-05, design record logs/design_records/main_bot_live_price_feed_2026-10-05.md; board Harris + McKinney
seats + Gro + GAI aligned on this helper): data.fetcher.fetch_bars() sends no feed, and on this account's data plan
the server default is consolidated SIP, which is only served once it is ~15 minutes old. Any caller that reads
fetch_bars()' last close as "the price now" is therefore ~15 minutes behind. The plan's REAL-TIME feed is IEX.

    live_price(symbol) -> LivePrice(price, source, age_s)

Sources (BOTH read; the NEWER fresh one wins):
  1. "iex_1m"   — close of the newest IEX 1-minute bar (fetch_bars_window(feed="iex")) within the age limit. The bars
                  API does not return the minute in progress, so this bar normally STARTED 60-120s ago.
  2. "iex_trade"— the latest IEX trade print (data.alpaca_data.get_latest_trade_with_time), held to the SAME
                  caller-set age limit (max_age_s; an IEX print can be older than the delayed bar on a thin name —
                  never prefer it then). Usually seconds old, so it normally wins (cold-2nd rev8).
  3. None       — neither available. The CALLER decides; an exit/stop evaluation must fall back to its previous
                  source (never skip a stop check because the real-time read failed) and log a WARNING.

A short per-symbol cache (_CACHE_TTL_S) of the BAR read bounds the extra API load when several checks read the same
symbol in one cycle; the latest trade is never cached (read fresh every call, as the pre-2026-10-05 exit code did). Never raises. Prices are RAW (unadjusted), the same basis as broker fills and the existing price checks.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

_MAX_BAR_AGE_S = 180.0    # PROV:main-bot-live-price-2026-10-05 — newest IEX 1m bar must have STARTED within 3 min
_LOOKBACK_MIN = 10        # PROV:main-bot-live-price-2026-10-05 — window of IEX 1m bars requested
_CACHE_TTL_S = 15.0       # PROV:main-bot-live-price-2026-10-05 — per-symbol reuse window inside one cycle
_CLOCK_SKEW_S = 5.0       # PROV:main-bot-live-price-2026-10-05 — tolerate a slightly-behind local clock (negative age)
# The delayed default feed (fetch_bars, consolidated SIP) is served only once >= ~15 min old, so ANY IEX price younger
# than this is fresher than the fallback it would replace. Exit/stop callers pass max_age_s=DELAYED_FEED_AGE_S so a
# 3-15 min old IEX print on a thin name is never discarded in favour of an OLDER delayed close (risk seat 2026-10-05).
# fetch_bars() also serves frames from its own TTL cache (config.ALPACA_BAR_CACHE_TTL_SECS), so the fallback close can be
# as old as the SIP delay PLUS that TTL (cold-2nd rev5) — the exit age limit covers both.
_SIP_DELAY_S = 900.0        # PROV:main-bot-live-price-2026-10-05 — SIP delay on this data plan (probe: recent SIP -> 403)
DELAYED_FEED_AGE_S = _SIP_DELAY_S + float(getattr(config, "ALPACA_BAR_CACHE_TTL_SECS", 0) or 0)
# That allowance fits the TRADE source (the pre-2026-10-05 exit code used the latest print with no age limit at all).
# An IEX BAR is a new source: at a longer max age it must have STARTED no more than 900 - 60 = 840s ago, so every print
# in it is NEWER than the SIP cutoff and it is never older than a freshly fetched delayed close (cold-2nd rev6).
_BAR_WIDTH_S = 60.0

# LATENCY BOUND (cold-2nd 2026-10-05): fetch_bars_window retries a 429 for up to ~61s, and this runs on the trading
# thread ahead of check_exits(). (1) Every BAR result, None included, is cached for _CACHE_TTL_S, so the second exit
# site does not repeat the bar read (the latest trade is always re-read). (2) An IEX BAR read slower than _SLOW_READ_S (success or not) pauses BAR reads for
# _BAR_PAUSE_S; the latest-trade read stays available (it costs what the pre-2026-10-05 exit code already paid), so a
# slow bar endpoint never pushes other symbols onto the delayed price, and costs at most ONE slow bar read per pause.
_SLOW_READ_S = 3.0        # PROV:main-bot-live-price-2026-10-05 — a healthy IEX bar read takes well under 1s
_BAR_PAUSE_S = 120.0      # PROV:main-bot-live-price-2026-10-05 — skip IEX bar reads this long after a slow one

_cache: dict = {}
_lock = threading.Lock()
_bar_paused_until = 0.0


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


def _from_iex_bars(symbol: str, now: datetime, max_age_s: float = _MAX_BAR_AGE_S,
                   live: bool = False) -> "LivePrice | None":
    from data.fetcher import fetch_bars_window
    lookback = max(_LOOKBACK_MIN, int(max_age_s // 60) + 2)
    df = fetch_bars_window(symbol, "1Min", now - timedelta(minutes=lookback), now, feed="iex")
    if live:
        now = datetime.now(_ET)           # age against the clock AFTER the (possibly slow) read — cold-2nd rev4
    if df is None or getattr(df, "empty", True) or "close" not in df.columns:
        return None
    ts = df.index[-1]
    ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if getattr(ts, "tzinfo", None) is None:
        return None                       # a naive timestamp cannot be aged safely
    age = (now - ts).total_seconds()
    px = df["close"].iloc[-1]
    if not (-_CLOCK_SKEW_S <= age <= max_age_s) or not _valid(px):
        return None
    return LivePrice(float(px), "iex_1m", round(max(age, 0.0), 1))


class _TradeUnavailable(Exception):
    """The latest-trade request itself failed (timeout / HTTP error / empty body) — as opposed to a print that came
    back but is too old. Trade results are never cached: every exit check re-reads, as the old code did."""


def _from_latest_trade(symbol: str, now: "datetime | None", max_age_s: float = _MAX_BAR_AGE_S) -> "LivePrice | None":
    """now=None (live): age the print against the clock read AFTER the response, so a print made while an earlier
    slow read was running is never rejected as 'from the future' (cold-2nd rev4). Raises _TradeUnavailable when the
    request failed; returns None when a print came back but is too old / invalid."""
    from data.alpaca_data import get_latest_trade_with_time
    got = get_latest_trade_with_time(symbol)
    if not got:
        raise _TradeUnavailable(symbol)
    if now is None:
        now = datetime.now(_ET)
    px, ts = got
    age = (now - ts).total_seconds()
    if age < -_CLOCK_SKEW_S:
        logger.warning("[%s] live_price: latest print is %.1fs in the future — local clock behind? print not used",
                       symbol, -age)
    if not (-_CLOCK_SKEW_S <= age <= max_age_s) or not _valid(px):
        return None                      # an old print is never preferred over the caller's own source
    return LivePrice(float(px), "iex_trade", round(max(age, 0.0), 1))


def _pause_bars_if_slow(symbol: str, elapsed: float) -> None:
    global _bar_paused_until
    if elapsed > _SLOW_READ_S:
        with _lock:
            _bar_paused_until = time.monotonic() + _BAR_PAUSE_S
        logger.warning("[%s] live_price: IEX bar read took %.1fs — bar reads paused %.0fs (latest-trade still read)",
                       symbol, elapsed, _BAR_PAUSE_S)


def live_price(symbol: str, now: "datetime | None" = None,
               max_age_s: float = _MAX_BAR_AGE_S) -> "LivePrice | None":
    """The symbol's current price from the real-time feed, or None. Never raises.
    max_age_s: oldest acceptable bar/print (default 3 min; exit paths pass DELAYED_FEED_AGE_S).
    The bar and the latest trade are both read and the NEWER of the two (smallest age) wins — a 90-s-old bar never
    beats a 2-s-old print (cold-2nd rev8)."""
    try:
        live = now is None                  # an explicit `now` (replay/test) skips the cache and the bar pause
        now = now if now is not None else datetime.now(_ET)
        key = (symbol, max_age_s)
        bar_max = max_age_s if max_age_s <= _MAX_BAR_AGE_S else min(max_age_s, _SIP_DELAY_S - _BAR_WIDTH_S)
        bar = None
        read_bars = True
        if live:
            mono = time.monotonic()
            with _lock:
                hit = _cache.get(key)
                if hit is not None and mono - hit[1] < _CACHE_TTL_S:
                    read_bars = False               # reuse the cached BAR, aged by the time since it was read
                    if hit[0] is not None:
                        aged = hit[0].age_s + (mono - hit[1])
                        if aged <= bar_max:
                            bar = LivePrice(hit[0].price, hit[0].source, round(aged, 1))
                else:
                    read_bars = mono >= _bar_paused_until
        if read_bars:
            t0 = time.monotonic()
            try:
                bar = _from_iex_bars(symbol, now, bar_max, live=live)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] live_price: IEX bar read failed: %s", symbol, e)
            if live:
                done = time.monotonic()
                _pause_bars_if_slow(symbol, done - t0)
                with _lock:
                    _cache[key] = (bar, done)       # bar result only (None included); its age was taken at `done`
        # The latest trade is read on EVERY call, never cached: the bars API never returns the minute in progress, so
        # even the freshest bar STARTED 60-120s ago while the last print is usually seconds old (cold-2nd + risk seat
        # rev8, probe 2026-10-06: AAPL bar 15:40:00Z vs trade 15:41:08Z). One trade read per check is exactly what the
        # pre-2026-10-05 exit code paid. The newer of the two wins; the bar covers a failed/old trade read.
        trade = None
        try:
            trade = _from_latest_trade(symbol, None if live else now, max_age_s)
        except _TradeUnavailable:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] live_price: latest-trade read failed: %s", symbol, e)
        found = [c for c in (bar, trade) if c is not None]
        out = min(found, key=lambda c: c.age_s) if found else None
        return out
    except Exception as e:  # noqa: BLE001 — a price read must never raise into a trading path
        logger.warning("[%s] live_price error: %s", symbol, e)
        return None


def live_price_or(symbol: str, fallback: "float | None", where: str,
                  max_age_s: float = _MAX_BAR_AGE_S) -> "tuple[float | None, str]":
    """Real-time price if available, else `fallback` (the caller's previous source) with a WARNING.
    Returns (price, source_label). Use this at exit/stop checks: a failed real-time read must never skip a stop
    evaluation, so the old value is still returned when nothing fresher exists."""
    lp = live_price(symbol, max_age_s=max_age_s)
    if lp is not None:
        return lp.price, lp.source
    logger.warning("[%s] %s: real-time price unavailable — using the delayed fallback %s", symbol, where, fallback)
    return fallback, "delayed_fallback"
