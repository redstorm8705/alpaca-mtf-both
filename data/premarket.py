"""
data/premarket.py
Pre-market mover detection + ATR-based volatility filter.

Two jobs:
  1. scan_premarket_movers()  — find tickers moving ±PREMARKET_MOVER_THRESHOLD_PCT
                                before market open and add them to the scan universe
  2. atr_filter()             — filter any list of tickers by minimum ATR%,
                                ensuring we only trade stocks with enough daily
                                range to be worth the spread
"""

import os
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from data.fetcher import fetch_bars, get_client

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

logger = logging.getLogger("premarket")
ET = ZoneInfo("America/New_York")

# H-2: module-level cache — persists pre-market movers into RTH scan universe
_cached_premarket_movers: list = []

# Rule 1 & 2 detail cache — symbol → signed pct_change (e.g. -4.75 for XOM down 4.75%)
# Populated by scan_premarket_movers() before the list is simplified to ticker strings.
# Used by execute_entries() to apply the Premarket Red Lockout and Gap-Down Settling Window.
_premarket_detail: dict = {}


def cache_premarket_movers(movers: list) -> None:
    """
    Called from main.py during the pre-market phase to persist the mover list.
    build_scan_universe() reads this cache during RTH so movers are not discarded
    at market open.
    Also writes logs/premarket_movers.json so scan_to_html.py can display the
    variable watchlist section with PM MOVER badges.
    """
    global _cached_premarket_movers
    _cached_premarket_movers = list(movers)
    logger.info(f"Pre-market movers cached ({len(_cached_premarket_movers)}): {_cached_premarket_movers}")
    # Persist to disk for scan_to_html.py variable watchlist display
    try:
        import json
        _logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
        os.makedirs(_logs, exist_ok=True)
        _path = os.path.join(_logs, "premarket_movers.json")
        _tmp  = _path + ".tmp"
        with open(_tmp, "w") as _f:
            json.dump({"movers": movers,
                       "core_watchlist": list(config.WATCHLIST),
                       "ts": datetime.now(ET).isoformat()}, _f)
            _f.flush(); os.fsync(_f.fileno())
        os.replace(_tmp, _path)
    except Exception as _e:
        logger.warning(f"premarket_movers.json write failed: {_e}")


# ─── ATR CALCULATION ─────────────────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = None) -> float:
    """
    Calculate Average True Range as a % of current price.
    True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
    Returns ATR as a percentage of the last closing price.
    Returns 0.0 if insufficient data.
    """
    period = period or config.ATR_PERIOD
    if len(df) < period + 1:
        return 0.0

    df = df.copy()
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = df[["high", "low", "prev_close"]].apply(
        lambda row: max(
            row["high"] - row["low"],
            abs(row["high"] - row["prev_close"]) if pd.notna(row["prev_close"]) else 0,
            abs(row["low"]  - row["prev_close"]) if pd.notna(row["prev_close"]) else 0,
        ),
        axis=1,
    )

    atr_value = df["tr"].iloc[-period:].mean()
    last_price = df["close"].iloc[-1]

    if last_price <= 0:
        return 0.0

    return round((atr_value / last_price) * 100, 3)


def atr_filter(symbols: list, min_atr_pct: float = None) -> list:
    """
    Filter a list of symbols, keeping only those whose ATR% >= min_atr_pct.

    Returns:
        List of (symbol, atr_pct) tuples that passed the filter, sorted by
        atr_pct descending (highest expected movers first).
    """
    min_atr = min_atr_pct or config.ATR_MIN_PCT
    passed = []
    failed = []

    for symbol in symbols:
        try:
            df = fetch_bars(symbol, config.TF_DAILY, num_bars=config.ATR_PERIOD + 5)
            if df.empty or len(df) < config.ATR_PERIOD:
                logger.debug(f"[{symbol}] Insufficient daily data for ATR")
                failed.append(symbol)
                continue

            atr_pct = calculate_atr(df, config.ATR_PERIOD)

            if atr_pct >= min_atr:
                label = "HIGH VOL" if atr_pct >= config.ATR_BOOST_PCT else "ok"
                logger.debug(f"[{symbol}] ATR {atr_pct:.2f}% — PASS ({label})")
                passed.append((symbol, atr_pct))
            else:
                logger.debug(f"[{symbol}] ATR {atr_pct:.2f}% < {min_atr}% — FILTERED OUT")
                failed.append(symbol)

        except Exception as e:
            logger.debug(f"[{symbol}] ATR check error: {e}")
            failed.append(symbol)

    if failed:
        logger.info(f"ATR filter removed {len(failed)} low-vol symbols: {failed[:10]}{'...' if len(failed)>10 else ''}")

    passed.sort(key=lambda x: x[1], reverse=True)
    logger.info(f"ATR filter: {len(passed)}/{len(symbols)} symbols passed (min ATR {min_atr}%)")
    return passed


# ─── PRE-MARKET MOVERS ───────────────────────────────────────────────────────

def _check_symbol_premarket(symbol: str, client, threshold: float) -> dict | None:
    """
    Per-symbol premarket check — runs in ThreadPoolExecutor worker thread.
    Returns a mover dict if the symbol moved >= threshold%, else None.
    Thread-safe: no shared mutable state; logger is thread-safe in Python.
    """
    try:
        df = fetch_bars(symbol, config.TF_DAILY, num_bars=3)
        if df.empty or len(df) < 2:
            return None

        prior_close = float(df["close"].iloc[-1])

        quote_req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes    = client.get_stock_latest_quote(quote_req)
        quote     = quotes.get(symbol)
        if quote is None:
            return None

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid <= 0 and ask <= 0:
            return None

        premarket_price = ((bid + ask) / 2) if (bid > 0 and ask > 0) else (ask if ask > 0 else bid)

        if prior_close <= 0:
            return None

        pct_change = ((premarket_price - prior_close) / prior_close) * 100

        if abs(pct_change) > config.PREMARKET_MOVE_MAX_PCT:
            logger.warning(
                f"  [{symbol}] Pre-market move {pct_change:+.1f}% exceeds "
                f"±{config.PREMARKET_MOVE_MAX_PCT:.0f}% sanity cap — treating as bad data, skipping"
            )
            return None

        if abs(pct_change) >= threshold:
            direction = "UP" if pct_change > 0 else "DOWN"
            logger.info(
                f"  PRE-MARKET MOVER: {symbol:6s} {pct_change:+.2f}% "
                f"(${prior_close:.2f} → ${premarket_price:.2f})"
                + (" [LEVERAGED]" if symbol in config.LEVERAGED_TICKERS else "")
            )
            return {
                "symbol":          symbol,
                "pct_change":      round(pct_change, 2),
                "premarket_price": round(premarket_price, 2),
                "prior_close":     round(prior_close, 2),
                "direction":       direction,
                "is_leveraged":    symbol in config.LEVERAGED_TICKERS,
            }
        return None   # moved, but below threshold — not a mover

    except Exception as e:
        logger.debug(f"[{symbol}] Pre-market check failed: {e}")
        return None


def scan_premarket_movers(
    base_universe: list = None,
    threshold_pct: float = None,
) -> list:
    """
    Scan for pre-market movers by comparing the latest quote to prior close.
    Runs best between 4:00 AM – 9:29 AM ET (pre-market session).
    Returns a list of ticker strings that moved ≥ threshold_pct in either direction.

    These get merged into the scan universe on top of WATCHLIST at market open.

    Args:
        base_universe:  list of tickers to check (defaults to config.WATCHLIST
                        + S&P 500 top names). Kept intentionally narrow to avoid
                        rate limit hammering pre-market.
        threshold_pct:  minimum absolute % move to qualify (default: config value)

    Returns:
        List of symbol strings (no duplicates), sorted by abs(pct_change) desc.
    """
    threshold = threshold_pct or config.PREMARKET_MOVER_THRESHOLD_PCT

    # Default universe for pre-market scan: watchlist + extended names
    # Keep this to ~50-75 tickers max to avoid API rate limits pre-market
    if base_universe is None:
        base_universe = list(dict.fromkeys(config.WATCHLIST + [
            # Additional liquid names worth checking pre-market
            "MSFT", "AAPL", "NVDA", "TSLA", "AMZN", "META", "GOOGL",
            "AMD", "NFLX", "QCOM", "INTC", "MU", "AVGO",
            "JPM", "GS", "BAC", "MS",
            "XOM", "CVX",
            "SPY", "QQQ", "IWM", "DIA",
        ]))

    client = get_client()
    movers = []

    # ── Parallel pre-market scan ──────────────────────────────────────────────
    # max_workers=8: 75 symbols / 8 workers ≈ 9 parallel batches.
    # Each worker makes 2 Alpaca API calls (daily bars + latest quote).
    # Previously sequential (150+ blocking calls) — could hang 60+ min on a
    # stalled TCP socket. Parallel execution + socket.setdefaulttimeout(60) in
    # main.py ensures the entire scan completes in < 60s.
    # Alpaca data client (httpx.Client) is thread-safe for concurrent reads.
    logger.info(
        f"Pre-market scan: checking {len(base_universe)} symbols for "
        f"±{threshold}% moves (8 parallel workers)..."
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_check_symbol_premarket, sym, client, threshold): sym
            for sym in base_universe
        }
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                movers.append(result)

    # Sort by absolute move, biggest first
    movers.sort(key=lambda x: abs(x["pct_change"]), reverse=True)

    # Populate detail cache BEFORE simplifying to ticker list.
    # execute_entries() reads this via get_premarket_detail() to apply:
    #   Rule 1 — Premarket Red Lockout (≥75% red → block longs)
    #   Rule 2 — Gap-Down Settling Window (≥3% down → 30-min long block)
    global _premarket_detail
    _premarket_detail = {m["symbol"]: m["pct_change"] for m in movers}

    tickers = [m["symbol"] for m in movers]
    logger.info(
        f"Pre-market scan complete — {len(tickers)} movers ≥ ±{threshold}%: "
        + (", ".join(f"{m['symbol']}({m['pct_change']:+.1f}%)" for m in movers) or "none")
    )
    return tickers


def get_premarket_detail() -> dict:
    """
    Returns {symbol: signed_pct_change} for all premarket movers detected this session.
    Negative values = down (e.g. XOM → -4.75 means XOM was down 4.75% premarket).
    Empty dict if premarket scan has not run yet (no data before 8:45 AM ET).

    Called by execute_entries() in main.py to enforce:
      Rule 1 — Premarket Red Lockout: if ≥75% of tracked movers are red, block longs
      Rule 2 — Gap-Down Settling Window: if symbol down ≥3%, block longs first 30 min RTH
    """
    return dict(_premarket_detail)


# ─── BUILD FINAL SCAN UNIVERSE ───────────────────────────────────────────────

def build_scan_universe(run_atr_filter: bool = True) -> list:
    """
    Builds the final list of tickers the bot will scan each cycle.

    Step 1: Start with WATCHLIST (always included)
    Step 2: Add pre-market movers that qualify (± threshold)
    Step 3: Apply ATR filter to the combined list (remove low-vol names)
    Step 4: Log the final universe with ATR% for each ticker

    Returns:
        Deduplicated list of ticker strings, highest-ATR first.
    """
    global _cached_premarket_movers   # may be written in the disk-reload branch below
    now_et = datetime.now(ET)
    mins_et = now_et.hour * 60 + now_et.minute
    is_premarket  = (4 * 60) <= mins_et < (9 * 60 + 30)
    is_market_open = (9 * 60 + 30) <= mins_et < (16 * 60)

    # Step 1: base watchlist
    universe = list(config.WATCHLIST)

    # During market hours: skip new pre-market scan but KEEP cached movers (H-2).
    # fetch_multi_timeframe gets live intraday bars; pre-market movers add alpha.
    if is_market_open:
        if _cached_premarket_movers:
            new_adds = [s for s in _cached_premarket_movers if s not in universe]
            if new_adds:
                logger.info(
                    f"RTH: injecting {len(new_adds)} cached pre-market mover(s) "
                    f"into scan universe: {new_adds}"
                )
                universe.extend(new_adds)
        else:
            # Try to reload from disk — covers watchdog os.execv() restarts where
            # the module is re-imported fresh and _cached_premarket_movers resets
            # to [] even though logs/premarket_movers.json was written at pre-market.
            try:
                import json as _json
                _pm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "logs", "premarket_movers.json")
                with open(_pm_path) as _pmf:
                    _pm_data = _json.load(_pmf)
                _disk_movers = _pm_data.get("movers", [])
                if _disk_movers:
                    _cached_premarket_movers = _disk_movers
                    new_adds = [s for s in _cached_premarket_movers if s not in universe]
                    if new_adds:
                        logger.info(
                            f"RTH: reloaded {len(new_adds)} pre-market mover(s) from disk "
                            f"(post-restart recovery): {new_adds}"
                        )
                        universe.extend(new_adds)
                    else:
                        logger.info(
                            f"RTH: disk movers {_disk_movers} all already in watchlist — "
                            f"no new adds needed"
                        )
                else:
                    logger.info(f"Market open — watchlist only ({len(universe)} tickers, no cached movers)")
            except Exception:
                logger.info(f"Market open — watchlist only ({len(universe)} tickers, no cached movers)")
        return universe

    # Step 2: pre-market movers (only before 9:30 AM ET)
    premarket_adds = []
    if is_premarket:
        premarket_adds = scan_premarket_movers()
        new_adds = [s for s in premarket_adds if s not in universe]
        if new_adds:
            logger.info(f"Adding {len(new_adds)} pre-market movers to universe: {new_adds}")
            universe.extend(new_adds)

    # Deduplicate while preserving order
    seen = set()
    universe = [s for s in universe if not (s in seen or seen.add(s))]

    if not run_atr_filter:
        logger.info(f"Final universe: {len(universe)} tickers (ATR filter skipped)")
        return universe

    # Step 3: ATR filter (only runs pre-market or after hours)
    logger.info(f"Running ATR filter on {len(universe)} tickers (min {config.ATR_MIN_PCT}%)...")
    filtered = atr_filter(universe, config.ATR_MIN_PCT)

    # Always keep leveraged ETFs and watchlist core even if ATR is low
    # (they behave differently and we explicitly opted them in)
    filtered_symbols = [s for s, _ in filtered]
    forced_in = [s for s in config.WATCHLIST if s not in filtered_symbols]
    if forced_in:
        logger.info(f"Force-including watchlist tickers that failed ATR: {forced_in}")

    final = filtered_symbols + [s for s in forced_in if s not in filtered_symbols]

    # Step 4: log final universe
    logger.info(f"Final scan universe: {len(final)} tickers")
    high_vol = [(s, a) for s, a in filtered if a >= config.ATR_BOOST_PCT]
    if high_vol:
        logger.info(
            "High-volatility tickers (ATR ≥ {:.1f}%): {}".format(
                config.ATR_BOOST_PCT,
                ", ".join(f"{s}({a:.1f}%)" for s, a in high_vol)
            )
        )

    return final


# ─── DYNAMIC UNIVERSE (market-open, FMP screeners) ───────────────────────────

def build_dynamic_universe() -> list:
    """
    Build a dynamic scan universe at market open using FMP screeners (T2).
    Runs once per session at/near market open (9:30 AM ET).

    Steps:
      1. Pull biggest-gainers, biggest-losers, most-actives from FMP T2
      2. Filter: price $5–$300, volume > 1M
      3. Merge with static WATCHLIST (Bucket A + SPY/QQQ always preserved)
      4. Cap total universe at 22 tickers

    Returns:
        List of ticker strings for the day's scan universe.
    """
    MAX_UNIVERSE = 22

    # Anchors: always included regardless of screener results
    anchors    = list(config.BUCKET_A_TICKERS) + ["SPY", "QQQ"]
    anchor_set = set(anchors)

    from data.fmp_client import get_screener_movers
    dynamic = get_screener_movers(price_min=5.0, price_max=300.0, min_volume=1_000_000)

    # Deduplicate dynamic list
    seen    = set()
    dynamic = [s for s in dynamic if not (s in seen or seen.add(s))]

    # Merge: anchors → static watchlist → dynamic movers
    combined = list(dict.fromkeys(anchors + list(config.WATCHLIST) + dynamic))

    # Remove global exclusions
    combined = [s for s in combined if s not in config.EXCLUSIONS]

    # Anchors first, fill remaining slots from combined
    final           = [s for s in combined if s in anchor_set]
    remaining_slots = MAX_UNIVERSE - len(final)
    for s in combined:
        if s not in anchor_set and remaining_slots > 0:
            final.append(s)
            remaining_slots -= 1

    logger.info(
        f"build_dynamic_universe: {len(final)} tickers "
        f"(anchors preserved: {sorted(anchor_set)}, cap={MAX_UNIVERSE})"
    )
    return final
