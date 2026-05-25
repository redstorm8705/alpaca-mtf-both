# events/earnings_fetcher.py — Auto-populate earnings dates
# T2: all data via data/fmp_client.py (FMP earnings-calendar endpoint, 12h cache)

from datetime import date, timedelta
from events.calendar import EventCalendar
import logging

logger = logging.getLogger("earnings_fetcher")


def fetch_upcoming_earnings(calendar: EventCalendar, days_ahead: int = 14):
    """
    Fetch upcoming earnings from FMP T2 and add them to the calendar as blackout dates.
    Uses fmp_client.preload_earnings_week() — single FMP call, 12h cache.
    """
    import config as _cfg
    from data.fmp_client import preload_earnings_week

    symbols = list(dict.fromkeys(list(_cfg.WATCHLIST) + [
        "MSFT", "AAPL", "NVDA", "TSLA", "AMZN", "META", "GOOGL",
        "AMD", "NFLX", "QCOM", "INTC", "MU", "AVGO",
        "JPM", "GS", "BAC", "MS", "XOM", "CVX",
        "SPY", "QQQ", "IWM", "DIA",
    ]))

    result = preload_earnings_week(symbols, lookahead_days=days_ahead)

    added = 0
    for symbol, dates in result.items():
        for d in dates:
            calendar.add_earnings(d.isoformat(), symbol)
            added += 1

    logger.info(f"Earnings: added {added} date(s) from FMP T2 ({len(result)} symbols)")
    return added


def get_earnings_today(calendar: EventCalendar) -> list:
    """Returns list of tickers reporting today."""
    today_str = date.today().isoformat()
    return [sym for d, sym in calendar._earnings if d == today_str]


def get_earnings_this_week(calendar: EventCalendar) -> dict:
    """Returns {date_str: [tickers]} for next 5 trading days."""
    result = {}
    today = date.today()
    for i in range(7):
        d = today + timedelta(days=i)
        d_str = d.isoformat()
        tickers = [sym for dt, sym in calendar._earnings if dt == d_str]
        if tickers:
            result[d_str] = tickers
    return result
