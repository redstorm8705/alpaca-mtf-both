"""
data/alpaca_data.py
Real-time quote and trade data via Alpaca Data REST API.

Guardrail 2: StockHistoricalDataClient lives in data/fetcher.py only.
             This module uses requests directly against data.alpaca.markets
             — no SDK instantiation here.
Data tier: T1 (Alpaca Data API).
"""

import os
import time
import logging
import requests

logger = logging.getLogger(__name__)

_BASE    = "https://data.alpaca.markets"
_TIMEOUT = 4.0   # seconds — matches former yfinance socket timeout pattern

# P2-ALPACA-402: set True on 402 subscription-tier error, reset to False on success
_alpaca_402_stale = False


def is_alpaca_data_stale() -> bool:
    """Returns True if the last Alpaca Data call received a 402 subscription-tier error."""
    return _alpaca_402_stale


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


def get_latest_quote(symbol: str) -> dict | None:
    """
    Return the latest NBBO quote (bid/ask) for a symbol from Alpaca Data REST API.

    Endpoint: GET /v2/stocks/{symbol}/quotes/latest
    Returns: dict with keys "bid" and "ask" (floats), or None on failure.
    """
    global _alpaca_402_stale
    try:
        # P2-ALPACA-429: exponential backoff retry for 429 rate-limit (max 3 attempts)
        for _attempt in range(3):
            resp = requests.get(
                f"{_BASE}/v2/stocks/{symbol}/quotes/latest",
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code == 429:
                if _attempt < 2:
                    _wait = 2 ** _attempt  # 1s, 2s
                    logger.warning(
                        f"[{symbol}] get_latest_quote: Alpaca Data 429 rate-limit "
                        f"(attempt {_attempt + 1}/3) — retrying in {_wait}s"
                    )
                    time.sleep(_wait)
                    continue
                else:
                    logger.error(
                        f"[{symbol}] get_latest_quote: Alpaca Data 429 rate-limit "
                        f"— 3 attempts exhausted. Returning None."
                    )
                    return None
            break  # success or non-429 error — exit retry loop

        if resp.status_code == 200:
            # P2-ALPACA-402: reset stale flag on success
            _alpaca_402_stale = False
            q = resp.json().get("quote", {})
            bid = q.get("bp")
            ask = q.get("ap")
            if bid and ask:
                return {"bid": float(bid), "ask": float(ask)}
        elif resp.status_code == 402:
            # P2-ALPACA-402: flag stale state; warn only on first occurrence
            if not _alpaca_402_stale:
                logger.warning(
                    f"[{symbol}] get_latest_quote: 402 — real-time data requires paid plan. "
                    f"Marking alpaca data as stale."
                )
            _alpaca_402_stale = True
        else:
            logger.debug(f"[{symbol}] get_latest_quote: HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"[{symbol}] get_latest_quote failed: {e}")
    return None


def get_latest_trade(symbol: str) -> float | None:
    """
    Return the latest trade price for a symbol from Alpaca Data REST API.
    Replaces yfinance fast_info.last_price (DATA-2).

    Endpoint: GET /v2/stocks/{symbol}/trades/latest
    Returns: float price, or None on any failure.

    Fallback contract: callers must handle None gracefully and fall back
    to the most recent bar close.
    """
    global _alpaca_402_stale
    try:
        # P2-ALPACA-429: exponential backoff retry for 429 rate-limit (max 3 attempts)
        for _attempt in range(3):
            resp = requests.get(
                f"{_BASE}/v2/stocks/{symbol}/trades/latest",
                headers=_headers(),
                timeout=_TIMEOUT,
            )
            if resp.status_code == 429:
                if _attempt < 2:
                    _wait = 2 ** _attempt  # 1s, 2s
                    logger.warning(
                        f"[{symbol}] get_latest_trade: Alpaca Data 429 rate-limit "
                        f"(attempt {_attempt + 1}/3) — retrying in {_wait}s"
                    )
                    time.sleep(_wait)
                    continue
                else:
                    logger.error(
                        f"[{symbol}] get_latest_trade: Alpaca Data 429 rate-limit "
                        f"— 3 attempts exhausted. Returning None."
                    )
                    return None
            break  # success or non-429 error — exit retry loop

        if resp.status_code == 200:
            # P2-ALPACA-402: reset stale flag on success
            _alpaca_402_stale = False
            price = resp.json().get("trade", {}).get("p")
            if price:
                return float(price)
        elif resp.status_code == 402:
            # P2-ALPACA-402: flag stale state; warn only on first occurrence.
            # 402 means the account lacks a paid real-time data plan.
            # The bot will fall back to the bar close price for entries, which can be
            # 1–5 min stale during fast-moving markets.
            if not _alpaca_402_stale:
                logger.warning(
                    f"[{symbol}] get_latest_trade: 402 — real-time data requires paid plan. "
                    f"Falling back to bar close price (may be stale). Marking alpaca data as stale."
                )
            _alpaca_402_stale = True
        else:
            logger.debug(f"[{symbol}] get_latest_trade: HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"[{symbol}] get_latest_trade failed: {e}")
    return None
