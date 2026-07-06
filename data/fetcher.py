"""
data/fetcher.py
Rate-limited Alpaca bar fetcher.
- Singleton client (one auth for entire session; requests.Session is concurrent-safe)
- GLOBAL rate limiter (_rate_gate) shared by scanner + main bot: aggregate
  throughput capped at config.ALPACA_MAX_REQ_PER_MIN (default 175) regardless of
  caller/thread — replaces the old per-call 0.5s sleep so the two can no longer
  race past the quota (board + Gro + GAI 2026-07-06).
- Shared TTL cache (config.ALPACA_BAR_CACHE_TTL_SECS): dedupes identical
  (symbol, timeframe, n_bars) fetches so scanner + main bot stop double-fetching.
- Retry up to 5x with aggressive backoff on rate limit errors
- Intraday mode: 3 TFs only (15M, 1H, Daily)
"""

import os
import time
import logging
import threading
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
import config
_ET = ZoneInfo("America/New_York")  # tz-aware; prevents naive/aware comparison errors

logger = logging.getLogger("fetcher")

TF_MAP = {
    config.TF_1M:     TimeFrame(1,  TimeFrameUnit.Minute),
    config.TF_5M:     TimeFrame(5,  TimeFrameUnit.Minute),
    config.TF_15M:    TimeFrame(15, TimeFrameUnit.Minute),
    config.TF_30M:    TimeFrame(30, TimeFrameUnit.Minute),
    config.TF_1H:     TimeFrame(1,  TimeFrameUnit.Hour),
    config.TF_4H:     TimeFrame(4,  TimeFrameUnit.Hour),
    config.TF_12H:    TimeFrame(12, TimeFrameUnit.Hour),
    config.TF_DAILY:  TimeFrame(1,  TimeFrameUnit.Day),
    config.TF_WEEKLY: TimeFrame(1,  TimeFrameUnit.Week),
}

# Intraday: only 3 TFs needed — entry timing, trend, daily bias/ATR/momentum
INTRADAY_TFS = [config.TF_15M, config.TF_1H, config.TF_DAILY]
SWING_TFS    = [config.TF_4H, config.TF_12H, config.TF_DAILY, config.TF_WEEKLY]

# Bars per RTH trading day by timeframe — used by DataFetcher.get_bars()
# 1Hour=6: US RTH is 6.5h (9:30-16:00 ET), floor to 6 full bars
_BARS_PER_TRADING_DAY: dict = {
    "1Min": 390, "5Min": 78, "15Min": 26, "30Min": 13,
    "1Hour": 6,  "4Hour": 2, "1Day":   1, "1Week":  1,
}

# Per-symbol fetch error dedup — first occurrence → WARNING,
# repeats → DEBUG until cooldown expires
_fetch_bars_warned: dict = {}        # symbol → last warning time.time()
_FETCH_WARN_COOLDOWN_SECS = 3600.0  # re-escalate to WARNING after 1 hour of silence
_RETRYABLE_ERR_SIGNALS: tuple = (
    "too many", "429", "rate limit",   # Alpaca rate limit (HTTP 429)
    "timeout",  "connection",          # ReadTimeout, ConnectTimeout, ConnectionError
    "reset",    "remote",              # TCP reset, RemoteDisconnected
    "408",                             # HTTP Request Timeout
    "500",      "502", "503",  "504",  # transient server errors
)
_RATE_LIMIT_SIGNALS: tuple = ("too many", "429", "rate limit")

# Singleton — one client for the entire bot session
_client = None

def get_client():
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(
            api_key=os.getenv("ALPACA_API_KEY"),
            secret_key=os.getenv("ALPACA_SECRET_KEY"),
        )
    return _client


# ─── Global rate limiter + shared TTL bar cache (board + Gro + GAI 2026-07-06) ─
# GLOBAL across scanner + main bot: both import this module, so every bar fetch
# passes through ONE rate gate — they can no longer race past the shared Alpaca
# quota (root-cause of the 429 backoff cascades / ~21-min scanner gaps). The TTL
# cache stops double-fetching identical (symbol, timeframe, n_bars) bars.
_rate_lock   = threading.Lock()
_last_req_ts = 0.0                       # monotonic ts of the last request start
_cache_lock  = threading.RLock()
_bar_cache: dict = {}                    # (symbol, tf, n_bars) -> (df, mono_ts)


def _rate_gate() -> None:
    """Reserve the next evenly-spaced request slot and block until it arrives.

    Shared by EVERY bar fetch (scanner + main bot). The slot is RESERVED inside
    the lock (_last_req_ts advanced to the reserved time), then the wait happens
    OUTSIDE the lock — so every request start is guaranteed >= min_interval apart
    across all callers/threads (deterministic spacing, no double-booking), with
    no lock held during the sleep. Aggregate Alpaca throughput therefore stays at
    ALPACA_MAX_REQ_PER_MIN regardless of caller/thread or request duration."""
    global _last_req_ts
    rpm = max(int(getattr(config, "ALPACA_MAX_REQ_PER_MIN", 175)), 1)
    min_interval = 60.0 / rpm
    with _rate_lock:
        slot = max(time.monotonic(), _last_req_ts + min_interval)
        _last_req_ts = slot          # reserve BEFORE releasing the lock
    wait = slot - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _cache_get(key):
    """Return a fresh cached DataFrame for `key`, or None. TTL<=0 disables."""
    ttl = float(getattr(config, "ALPACA_BAR_CACHE_TTL_SECS", 0) or 0)
    if ttl <= 0:
        return None
    with _cache_lock:
        entry = _bar_cache.get(key)
        if entry is not None and (time.monotonic() - entry[1]) < ttl:
            return entry[0]
    return None


def _cache_put(key, df) -> None:
    ttl = float(getattr(config, "ALPACA_BAR_CACHE_TTL_SECS", 0) or 0)
    if ttl <= 0:
        return
    with _cache_lock:
        _bar_cache[key] = (df, time.monotonic())


def get_universe():
    try:
        sp500  = pd.read_html(config.SP500_URL)[0]["Symbol"].tolist()
        ndx100 = pd.read_html(config.NDX100_URL)[4]["Ticker"].tolist()
        combined = list(set(sp500 + ndx100))
        return sorted(
            [t.replace(".", "-") for t in combined if t not in config.EXCLUSIONS]
        )
    except Exception as e:
        logger.error(f"Universe fetch failed: {e}")
        return []


def fetch_bars(
    symbol: str, timeframe: str, num_bars: int | None = None
) -> pd.DataFrame:
    """
    Fetch OHLCV bars with a GLOBAL rate limiter + shared TTL cache.
    - Shared TTL cache (ALPACA_BAR_CACHE_TTL_SECS): a fresh cached (symbol, tf,
      n_bars) is returned as a COPY without an API call or rate-budget cost —
      dedupes scanner/main-bot double-fetches of identical bars.
    - Global rate gate (ALPACA_MAX_REQ_PER_MIN) BEFORE each request replaces the
      old per-call 0.5s sleep, so scanner + main bot share ONE throughput cap and
      can no longer race past the Alpaca quota (root cause of 429 backoff cascades).
    - Up to 5 retries with exponential backoff capped at 20s (3s, 6s, 12s, 20s, 20s)
    - Cap prevents a single thread from sleeping 61s and blocking run_scan()'s
      timeout (90s), which would cause the watchdog to fire.
    """
    n_bars = num_bars or config.BARS_TO_FETCH.get(timeframe, 300)

    # Shared TTL cache — a hit returns a COPY (callers must not mutate cached data)
    # with no API call and no rate-budget cost.
    _ckey = (symbol, timeframe, n_bars)
    _cached = _cache_get(_ckey)
    if _cached is not None:
        return _cached.copy()

    if timeframe in [config.TF_15M, config.TF_30M]:
        days_back = max(30, n_bars // 26)
    elif timeframe == config.TF_1H:
        days_back = max(30, n_bars // 6)
    elif timeframe == config.TF_4H:
        days_back = max(60, n_bars * 2)
    elif timeframe == config.TF_WEEKLY:
        # 8x: 7 cal days/week + holiday buffer.
        # 120-day floor → ~17 weeks ≥ signal_generator 12-bar minimum.
        days_back = max(120, n_bars * 8)
    else:
        days_back = max(30, n_bars * 2)  # floor; prior 400 caused heap leak

    start = datetime.now(_ET) - timedelta(days=days_back)  # C-15: tz-aware

    for attempt in range(5):
        try:
            _rate_gate()  # global min-interval before call (replaces per-call sleep)
            client = get_client()
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TF_MAP[timeframe],
                start=start,
            )
            bars = client.get_stock_bars(request)
            df   = bars.df

            if df.empty:
                return pd.DataFrame()

            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(symbol, level="symbol")
            result = df[["open", "high", "low", "close", "volume"]].tail(n_bars)
            if len(result) < n_bars:
                logger.debug(
                    "[%s/%s] Requested %d bars, got %d",
                    symbol, timeframe, n_bars, len(result),
                )
            _cache_put(_ckey, result)
            return result

        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in _RETRYABLE_ERR_SIGNALS):
                _is_rl = any(x in err for x in _RATE_LIMIT_SIGNALS)
                if _is_rl:
                    wait = min(3 * (2 ** attempt), 20)   # 3s, 6s, 12s, 20s, 20s
                    _reason = "Rate limit"
                else:
                    wait = min(2 * (2 ** attempt), 8)    # 2s, 4s, 8s, 8s, 8s
                    _reason = "Transient error"
                logger.warning(
                    "[%s] %s [%s] — waiting %ss (attempt %d/5)",
                    symbol, _reason, type(e).__name__, wait, attempt + 1,
                )
                time.sleep(wait)
                continue
            _now = time.time()
            if _now - _fetch_bars_warned.get(symbol, 0.0) > _FETCH_WARN_COOLDOWN_SECS:
                _fetch_bars_warned[symbol] = _now
                logger.warning(f"[{symbol}] fetch_bars error [{type(e).__name__}]: {e}")
            else:
                logger.debug(
                    f"[{symbol}] fetch_bars error (repeat) [{type(e).__name__}]: {e}"
                )
            return pd.DataFrame()

    logger.warning(f"[{symbol}/{timeframe}] All retries exhausted")
    return pd.DataFrame()


def fetch_multi_timeframe(symbol: str, mode: str = "intraday") -> dict:
    """
    Fetch the timeframes needed for the given mode.
    intraday → 3 requests per ticker (15M, 1H, Daily)
    swing    → 4 requests per ticker (4H, 12H, Daily, Weekly)
    """
    tfs = INTRADAY_TFS if mode == "intraday" else SWING_TFS
    result = {}
    for tf in tfs:
        df = fetch_bars(symbol, tf)
        if not df.empty:
            result[tf] = df
    return result


class DataFetcher:
    """
    OO wrapper around module-level fetch functions.
    Required by run_movers.py — P5-C2 fix (class didn't exist, ImportError on startup).
    """

    def get_universe(self) -> list:
        return get_universe()

    def get_multi_timeframe(self, symbol: str, timeframes: list) -> dict:
        """Fetch a specific list of timeframes for a symbol."""
        result = {}
        for tf in timeframes:
            df = fetch_bars(symbol, tf)
            if not df.empty:
                result[tf] = df
        return result

    def get_bars(self, symbol: str, timeframe: str, days_back: int = 3) -> pd.DataFrame:
        """
        Fetch OHLCV bars for scanner compatibility. Thin wrapper around fetch_bars().
        Converts days_back to num_bars: (N days x bars/day) + 1 day buffer.
        +1 buffer ensures iloc[-2] is always available for scanner prev_close logic.

        WARNING: Each call incurs 0.5s rate throttle (fetch_bars throttle).
        Do NOT run scanner and main bot concurrently on overlapping universes --
        shared Alpaca 200 req/min quota fills faster and can push main bot past
        the 90s executor watchdog timeout.

        Returns DataFrame with columns [open, high, low, close, volume],
        or empty DataFrame on error (fetch_bars contract).
        """
        if timeframe not in _BARS_PER_TRADING_DAY:
            logger.warning(
                f"[{symbol}] get_bars: unknown timeframe"
                f" '{timeframe}' — returning empty DataFrame"
            )
            return pd.DataFrame()
        if days_back <= 0:
            logger.warning(
                f"[{symbol}] get_bars: days_back={days_back} — using 1"
            )
            days_back = 1
        bpd = _BARS_PER_TRADING_DAY[timeframe]  # key guaranteed by guard above
        num_bars = max(days_back * bpd + bpd, 10)  # +1 day buffer; min 10 bars
        return fetch_bars(symbol, timeframe, num_bars=num_bars)
