# ruff: noqa: E501
"""
data/option_bars.py
Alpaca OPTION 5-min bar fetch (T1 — data.alpaca.markets /v1beta1/options/bars).

Isolated from data/fetcher.py (which is equities-only via the SDK). Uses the REST endpoint
via `requests`, and SHARES data.fetcher._rate_gate() so every option request passes through the
SAME global rate limiter as the equity bar fetches (anti-silo: reuse the one gate, don't spawn a
parallel one). Read-only; used by the post-close forward-accuracy evaluator (off the trading
thread). NEVER raises — returns [] on any failure so a bad OCC / dead feed is "unevaluable",
never a false result downstream.

NOTE: the default OPRA feed carries option bars; `feed=indicative` 400s — do NOT pass it.
Verified live 2026-07-27: SPY260727C00739000 5Min bars returned OHLCV {o,h,l,c,v,n,vw}.
"""

import os
import logging
import requests  # type: ignore[import-untyped]
from datetime import datetime
from zoneinfo import ZoneInfo

import data.fetcher as _fetcher   # for the shared global _rate_gate()

logger = logging.getLogger("option_bars")

_BASE = "https://data.alpaca.markets"
_UTC  = ZoneInfo("UTC")
_TIMEOUT = 10.0
_MAX_PAGES = 3   # 5-min bars for one option-day is ~78 rows; 1 page (limit 1000) suffices — cap anyway


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


def _iso_utc(dt: datetime) -> str:
    """Any tz-aware datetime -> Alpaca ISO-8601 Z string (UTC)."""
    return dt.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def fetch_option_bars(occ: str, start: datetime, end: datetime, timeframe: str = "5Min") -> list:
    """Fetch OPTION OHLCV bars for ONE OCC symbol over [start, end] (both tz-aware).

    Returns a list of bar dicts ascending by time (keys per Alpaca: t=ISO-UTC bar START,
    o/h/l/c=OHLC, v=volume, n=trade count, vw=vwap), or [] on failure/empty. Shares the global
    rate gate. Never raises. The caller is responsible for volume-corroboration and window
    clamping (t >= scan_time) — this helper only fetches."""
    if not occ or start is None or end is None:
        return []
    out: list = []
    page_token: str | None = None
    try:
        for _ in range(_MAX_PAGES):
            _fetcher._rate_gate()   # share the ONE global Alpaca limiter
            params: dict = {
                "symbols":   occ,
                "timeframe": timeframe,
                "start":     _iso_utc(start),
                "end":       _iso_utc(end),
                "limit":     1000,
            }
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                f"{_BASE}/v1beta1/options/bars",
                headers=_headers(),
                params=params,
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("option bars [%s]: HTTP %s", occ, resp.status_code)
                return out
            body = resp.json()
            out.extend(body.get("bars", {}).get(occ, []) or [])
            page_token = body.get("next_page_token")
            if not page_token:
                break
    except Exception as e:
        logger.warning("option bars [%s] failed: %s", occ, e)
    return out
