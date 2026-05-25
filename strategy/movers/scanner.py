# strategy/movers/scanner.py — Top 10 movers with long/short detection

import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from data.fetcher import DataFetcher
from events.calendar import EventCalendar, EventRisk
import logging

PT = ZoneInfo("America/Los_Angeles")

logger = logging.getLogger("movers_scanner")


class MoversScanner:
    """
    Scans the universe for the top N movers by % change.
    Returns both top gainers (long candidates) and top losers (short candidates).
    Applies event calendar filtering before returning.
    """

    def __init__(self, fetcher: DataFetcher, calendar: EventCalendar):
        self.fetcher  = fetcher
        self.calendar = calendar

    def get_daily_movers(self, universe: list, top_n: int = 10) -> dict:
        """
        Compares today's open vs current price to find intraday movers.
        Also checks prior day close for overnight gap movers.

        Returns:
            {
                "long":  [top N gainers, sorted desc],
                "short": [top N losers, sorted asc],
                "scan_time": datetime
            }
        Each entry: {symbol, pct_change, price, volume, event_risk, event_note}
        """
        results = []

        for symbol in universe:
            try:
                # Skip earnings/blackout symbols immediately
                day_risk = self.calendar.get_day_risk(symbol=symbol)
                if day_risk["risk"] == EventRisk.BLACKOUT:
                    continue

                # Fetch last 2 days of daily bars
                df = self.fetcher.get_bars(symbol, "1Day", days_back=3)
                if df is None or len(df) < 2:
                    continue

                prev_close = df["close"].iloc[-2]
                curr_price = df["close"].iloc[-1]

                if prev_close <= 0:
                    continue

                pct_change = ((curr_price - prev_close) / prev_close) * 100
                volume     = df["volume"].iloc[-1]
                avg_volume = df["volume"].mean()
                vol_ratio  = volume / avg_volume if avg_volume > 0 else 1.0

                # Filter out very low volume (< 500k avg) — too illiquid
                if avg_volume < 500_000:
                    continue

                # Filter out penny stocks
                if curr_price < 5.0:
                    continue

                results.append({
                    "symbol":       symbol,
                    "pct_change":   round(pct_change, 2),
                    "price":        round(curr_price, 2),
                    "prev_close":   round(prev_close, 2),
                    "volume":       int(volume),
                    "vol_ratio":    round(vol_ratio, 2),
                    "event_risk":   day_risk["risk"].value,
                    "event_note":   day_risk["note"],
                    "size_mult":    self.calendar.get_size_multiplier(symbol),
                })

            except Exception as e:
                logger.debug(f"Skipping {symbol}: {e}")
                continue

        # Sort and split
        sorted_by_change = sorted(results, key=lambda x: x["pct_change"], reverse=True)

        gainers = [r for r in sorted_by_change if r["pct_change"] > 0][:top_n]
        losers  = [r for r in reversed(sorted_by_change) if r["pct_change"] < 0][:top_n]

        logger.info(
            f"Movers scan complete — {len(gainers)} gainers, {len(losers)} losers "
            f"from {len(results)} eligible symbols"
        )

        return {
            "long":      gainers,
            "short":     losers,
            "scan_time": datetime.now(PT),
        }

    def get_intraday_movers(self, universe: list, top_n: int = 10,
                            lookback_mins: int = 30) -> dict:
        """
        Finds stocks making the biggest moves in the last N minutes.
        Useful for intraday momentum entries.
        """
        results = []

        for symbol in universe:
            try:
                day_risk = self.calendar.get_day_risk(symbol=symbol)
                if day_risk["risk"] == EventRisk.BLACKOUT:
                    continue

                df = self.fetcher.get_bars(symbol, "5Min", days_back=1)
                if df is None or len(df) < (lookback_mins // 5 + 1):
                    continue

                bars_needed = lookback_mins // 5
                recent = df.iloc[-bars_needed:]

                start_price = recent["close"].iloc[0]
                curr_price  = recent["close"].iloc[-1]

                if start_price <= 0 or curr_price < 5.0:
                    continue

                pct_change  = ((curr_price - start_price) / start_price) * 100
                volume      = recent["volume"].sum()

                # Must have volume confirmation
                if volume < 100_000:
                    continue

                results.append({
                    "symbol":       symbol,
                    "pct_change":   round(pct_change, 2),
                    "price":        round(curr_price, 2),
                    "volume_last_n": int(volume),
                    "lookback_mins": lookback_mins,
                    "event_risk":   day_risk["risk"].value,
                    "event_note":   day_risk["note"],
                    "size_mult":    self.calendar.get_size_multiplier(symbol),
                })

            except Exception as e:
                logger.debug(f"Intraday skip {symbol}: {e}")
                continue

        sorted_results = sorted(results, key=lambda x: x["pct_change"], reverse=True)
        gainers = [r for r in sorted_results if r["pct_change"] > 0][:top_n]
        losers  = [r for r in reversed(sorted_results) if r["pct_change"] < 0][:top_n]

        return {
            "long":      gainers,
            "short":     losers,
            "scan_time": datetime.now(PT),
        }

    def add_confluence_score(self, movers: dict, timeframe_data_cache: dict) -> dict:
        """
        Optionally enriches mover results with MTF confluence scores
        from the main strategy engine. Pass in pre-fetched tf_data to avoid
        redundant API calls.
        """
        from strategy.signal_generator import generate_signal

        for direction in ["long", "short"]:
            for mover in movers[direction]:
                sym = mover["symbol"]
                if sym in timeframe_data_cache:
                    sig = generate_signal(sym, timeframe_data_cache[sym], direction=direction)
                    mover["confluence_score"] = sig.get("score", 0)
                    mover["confluence_trend"] = sig.get("trend", {}).get("bias", "neutral")
                else:
                    mover["confluence_score"] = None
                    mover["confluence_trend"] = "unknown"

        return movers

    def print_movers(self, movers: dict):
        """Pretty-print the movers list to console."""
        print(f"\n{'='*55}")
        print(f"  TOP MOVERS — {movers['scan_time'].strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*55}")

        print("\n  📈 TOP GAINERS (LONG CANDIDATES)")
        print(f"  {'Symbol':<8} {'% Chg':>7} {'Price':>8} {'VolRatio':>9} {'Event'}")
        print(f"  {'-'*50}")
        for m in movers["long"]:
            evt = f"⚠ {m['event_note']}" if m["event_risk"] != "clear" else ""
            vr  = m.get("vol_ratio", "-")
            print(f"  {m['symbol']:<8} {m['pct_change']:>+6.2f}% ${m['price']:>7.2f} {str(vr):>8}x  {evt}")

        print("\n  📉 TOP LOSERS (SHORT CANDIDATES)")
        print(f"  {'Symbol':<8} {'% Chg':>7} {'Price':>8} {'VolRatio':>9} {'Event'}")
        print(f"  {'-'*50}")
        for m in movers["short"]:
            evt = f"⚠ {m['event_note']}" if m["event_risk"] != "clear" else ""
            vr  = m.get("vol_ratio", "-")
            print(f"  {m['symbol']:<8} {m['pct_change']:>+6.2f}% ${m['price']:>7.2f} {str(vr):>8}x  {evt}")

        print()
