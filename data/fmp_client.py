# ruff: noqa: E501
"""
data/fmp_client.py
FMP (Financial Modeling Prep) API client.

Data tier: T2 — fundamentals, earnings calendar, analyst grades.
Approved use cases:
  - Earnings calendar (per-symbol): get_earnings_dates(symbol) → list[datetime.date]
  - Earnings calendar (batch):      preload_earnings_week(symbols) → dict[symbol, list[date]]
  - Analyst sentiment:              get_analyst_sentiment(symbol) → dict
  - Economic calendar: deferred (DATA-4 phase 2)

Free plan limit: 250 calls/day.
  preload_earnings_week: 1 call (replaces 23 per-entry calls)
  get_analyst_sentiment: 1 call per symbol, cached 7 days
  get_earnings_dates:    1 call per symbol (fallback only)

Guardrail 2: No Alpaca SDK instantiation here.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_BASE_STABLE = "https://financialmodelingprep.com/stable"
_TIMEOUT     = 5.0

_CACHE_DIR   = Path(__file__).parent.parent / "data" / "cache"

# Analyst grade strings → sentiment bucket
_BULLISH_GRADES  = {
    "buy", "strong buy", "outperform", "overweight",
    "market outperform", "sector outperform", "positive",
    "accumulate", "add",
}
_BEARISH_GRADES  = {
    "sell", "strong sell", "underperform", "underweight",
    "market underperform", "sector underperform", "negative",
    "reduce", "avoid",
}


def _key() -> str:
    return os.getenv("FMP_API_KEY", "")


_ECON_CACHE    = _CACHE_DIR / "econ_calendar.json"
_ECON_TTL_H    = 6   # refresh every 6h — events don't shift intraday

# FMP event name keywords → (EventType str, EventRisk str)
# T2 source — matched via substring on lowercased FMP event name
_FMP_ECON_KEYWORDS = [
    ("federal funds rate",     "FED_MEETING",  "HIGH_RISK"),
    ("fomc rate decision",     "FED_MEETING",  "HIGH_RISK"),
    ("interest rate decision", "FED_MEETING",  "HIGH_RISK"),
    ("fomc minutes",           "FED_MINUTES",  "HIGH_RISK"),
    ("fed minutes",            "FED_MINUTES",  "HIGH_RISK"),
    ("consumer price index",   "CPI",          "HIGH_RISK"),
    ("cpi",                    "CPI",          "HIGH_RISK"),
    ("producer price index",   "PPI",          "HIGH_RISK"),
    ("ppi",                    "PPI",          "HIGH_RISK"),
    ("nonfarm payrolls",       "JOBS_REPORT",  "HIGH_RISK"),
    ("non farm payrolls",      "JOBS_REPORT",  "HIGH_RISK"),
    ("unemployment rate",      "JOBS_REPORT",  "HIGH_RISK"),
    ("gross domestic product", "GDP",          "HIGH_RISK"),
    ("gdp",                    "GDP",          "HIGH_RISK"),
    ("core pce",               "OTHER",        "HIGH_RISK"),
    ("pce price index",        "OTHER",        "HIGH_RISK"),
    ("personal consumption",   "OTHER",        "HIGH_RISK"),
]


def get_economic_calendar(lookahead_days: int = 45) -> list[dict]:
    """
    Fetch upcoming US high-impact economic events from FMP.  T2 source.

    Endpoint: GET /stable/economic-calendar?from=YYYY-MM-DD&to=YYYY-MM-DD
    Filters:  country=US, impact=High only.
    Returns:  list of {"date", "type_str", "risk_str", "note"} for injection
              into EventCalendar.add_event_dynamic().

    Cache: data/cache/econ_calendar.json, 6h TTL.
    API cost: 1 call per refresh. Free plan: 250 calls/day budget.
    """
    try:
        if _ECON_CACHE.exists():
            with open(_ECON_CACHE) as f:
                cache = json.load(f)
            age_h = (datetime.now(timezone.utc).timestamp() -
                     datetime.fromisoformat(cache["fetched_at_utc"]).timestamp()) / 3600
            if age_h <= _ECON_TTL_H:
                return cache.get("events", [])
    except Exception as _cache_e:
        logger.debug("FMP econ_calendar: cache read failed — %s", _cache_e)

    api_key = _key()
    if not api_key:
        logger.debug("FMP_API_KEY not set — economic calendar unavailable")
        return []

    today   = date.today()
    to_date = today + timedelta(days=lookahead_days)

    try:
        resp = requests.get(
            f"{_BASE_STABLE}/economic-calendar",
            params={"from": today.isoformat(), "to": to_date.isoformat(), "apikey": api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug(f"FMP economic-calendar: HTTP {resp.status_code}")
            return []

        events = []
        for entry in resp.json():
            if entry.get("country", "").upper() != "US":
                continue
            if entry.get("impact", "").lower() != "high":
                continue
            d_str = str(entry.get("date", ""))[:10]
            try:
                date.fromisoformat(d_str)
            except Exception as _date_e:
                logger.debug("FMP econ_calendar: date parse failed %r — %s", d_str, _date_e)
                continue
            name_lower = str(entry.get("event", "")).lower()
            type_str   = "OTHER"
            for keyword, t, _ in _FMP_ECON_KEYWORDS:
                if keyword in name_lower:
                    type_str = t
                    break
            events.append({
                "date":     d_str,
                "type_str": type_str,
                "risk_str": "HIGH_RISK",
                "note":     f"{entry.get('event', 'Economic event')} (FMP T2) — 50% size",
            })

        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_ECON_CACHE, "w") as f:
                json.dump({"fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                           "events": events}, f, indent=2)
        except Exception as ce:
            logger.debug(f"econ_calendar cache write failed: {ce}")

        logger.info(
            f"FMP economic calendar: {len(events)} US high-impact events "
            f"({today} → {to_date})  # T2 source"
        )
        return events

    except Exception as e:
        logger.debug(f"FMP economic-calendar failed: {e}")
        return []


# ── Per-symbol earnings (legacy fallback path) ────────────────────────────────

def get_earnings_dates(symbol: str, limit: int = 5) -> list[date]:
    """
    Return upcoming/recent earnings dates for a symbol.

    Endpoint: GET /stable/earnings?symbol={symbol}&apikey=...
    (Legacy /api/v3/historical/earning_calendar was deprecated Aug 31 2025.)

    Returns: list of datetime.date objects, most-recent first.
             Empty list on any failure or if FMP_API_KEY is not set.

    Replaces yfinance Ticker.calendar (DATA-4).
    NOTE: Prefer get_cached_earnings_dates() — uses the batch pre-load cache
          so the entry gate doesn't burn a live FMP call per symbol.
    """
    api_key = _key()
    if not api_key:
        logger.debug(f"[{symbol}] FMP_API_KEY not set — earnings calendar unavailable")
        return []
    try:
        resp = requests.get(
            f"{_BASE_STABLE}/earnings",
            params={"symbol": symbol, "apikey": api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            dates = []
            for entry in resp.json()[:limit]:
                d_str = entry.get("date")
                if d_str:
                    try:
                        dates.append(date.fromisoformat(str(d_str)[:10]))
                    except Exception as _date_e:
                        logger.debug("[%s] FMP earnings: date parse failed %r — %s", symbol, d_str, _date_e)
            return dates
        elif resp.status_code == 403:
            logger.debug(f"[{symbol}] FMP earnings: 403 — key invalid or plan restriction")
        else:
            logger.debug(f"[{symbol}] FMP earnings: HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"[{symbol}] FMP earnings failed: {e}")
    return []


# ── PEAD: most-recent EPS beat/miss (NEW-2, board-approved 2026-04-20) ───────
# Returns beat_pct and report_date for the most recent confirmed EPS report.
# Cached per-symbol for 24 hours in data/cache/earnings_surprise_{symbol}.json.
# beat_pct = (actual - estimate) / abs(estimate). Positive = beat, negative = miss.

_SURPRISE_TTL_H = 24   # refresh once per day

def get_earnings_surprise(symbol: str) -> dict | None:
    """
    Return most recent EPS surprise for PEAD scoring.

    Returns {"beat_pct": float, "report_date": date} or None on failure/no data.

    Endpoint: GET /stable/earnings?symbol={symbol}&limit=3
    Fields used: eps (actual), epsEstimated (consensus), reportedDate (announcement date).

    Caches to data/cache/earnings_surprise_{symbol}.json (24h TTL).
    API cost: 1 call per symbol per day.
    """
    cache_path = _CACHE_DIR / f"earnings_surprise_{symbol}.json"
    try:
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            ts = cached.get("_ts", 0)
            if (datetime.now(timezone.utc).timestamp() - ts) < _SURPRISE_TTL_H * 3600:
                if cached.get("beat_pct") is not None:
                    return {
                        "beat_pct":    cached["beat_pct"],
                        "report_date": date.fromisoformat(cached["report_date"]),
                    }
                return None   # cached negative (no data) response
    except Exception as _cache_e:
        logger.debug("[%s] FMP earnings_surprise: cache read failed — %s", symbol, _cache_e)

    api_key = _key()
    if not api_key:
        logger.debug(f"[{symbol}] FMP_API_KEY not set — earnings surprise unavailable")
        return None

    result = None
    try:
        resp = requests.get(
            f"{_BASE_STABLE}/earnings",
            params={"symbol": symbol, "apikey": api_key, "limit": 3},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            for entry in resp.json():
                eps_actual = entry.get("eps")
                eps_est    = entry.get("epsEstimated")
                rep_date   = entry.get("reportedDate") or entry.get("date")
                # Require both values present and non-zero estimate
                if eps_actual is not None and eps_est is not None and abs(eps_est) > 0.001:
                    beat_pct = (float(eps_actual) - float(eps_est)) / abs(float(eps_est))
                    result = {
                        "beat_pct":    round(beat_pct, 4),
                        "report_date": str(rep_date)[:10],
                    }
                    break   # most recent report only
        elif resp.status_code == 403:
            logger.debug(f"[{symbol}] FMP earnings surprise: 403 — plan restriction")
        else:
            logger.debug(f"[{symbol}] FMP earnings surprise: HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"[{symbol}] FMP earnings surprise failed: {e}")

    # Write cache (even on None — prevents redundant calls)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {"_ts": datetime.now(timezone.utc).timestamp()}
        if result:
            payload.update(result)
        cache_path.write_text(json.dumps(payload, indent=2))
    except Exception as _cw_e:
        logger.debug("[%s] FMP earnings_surprise: cache write failed — %s", symbol, _cw_e)

    if result:
        return {
            "beat_pct":    result["beat_pct"],
            "report_date": date.fromisoformat(str(result["report_date"])),
        }
    return None


# ── Batch earnings pre-load ───────────────────────────────────────────────────

_EARNINGS_CACHE = _CACHE_DIR / "earnings_week.json"
_EARNINGS_TTL_H = 12   # refresh twice a day (pre-market + noon)


def preload_earnings_week(
    symbols: list[str],
    lookahead_days: int = 10,
) -> dict[str, list[date]]:
    """
    Fetch the next {lookahead_days} days of earnings for ALL watchlist symbols
    in a single FMP call instead of one call per symbol at entry time.

    Endpoint: GET /stable/earnings-calendar?from=YYYY-MM-DD&to=YYYY-MM-DD

    Caches to data/cache/earnings_week.json.
    Returns {symbol: [date, ...]} for symbols with upcoming earnings.
    Empty dict on any failure.

    API cost: 1 call (replaces N per-entry calls).
    """
    api_key = _key()
    if not api_key:
        logger.debug("FMP_API_KEY not set — earnings batch unavailable")
        return {}

    today   = date.today()
    to_date = today + timedelta(days=lookahead_days)

    try:
        resp = requests.get(
            f"{_BASE_STABLE}/earnings-calendar",
            params={
                "from":   today.isoformat(),
                "to":     to_date.isoformat(),
                "apikey": api_key,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug(f"FMP earnings-calendar batch: HTTP {resp.status_code}")
            return {}

        symbol_set = {s.upper() for s in symbols}
        result: dict[str, list[date]] = {}

        for entry in resp.json():
            sym   = (entry.get("symbol") or "").upper()
            d_str = entry.get("date")
            if not sym or not d_str or sym not in symbol_set:
                continue
            try:
                d = date.fromisoformat(str(d_str)[:10])
                result.setdefault(sym, []).append(d)
            except Exception as _date_e:
                logger.debug("FMP earnings_week: date parse failed sym=%s date=%r — %s", sym, d_str, _date_e)

        # Earnings window coverage summary — symbols with no FMP events in this window
        missing = sorted(symbol_set - set(result.keys()))
        if missing:
            logger.info(
                f"FMP earnings batch: scanned={len(symbol_set)} "
                f"with_earnings={len(result)} "
                f"no_events_in_window={len(missing)} "
                f"lookahead_days={lookahead_days}"
            )
            logger.debug(
                f"FMP earnings: no events in {lookahead_days}d window "
                f"(no scheduled dates or not covered by FMP): {missing}"
            )

        # Persist cache
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_payload = {
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                "from":           today.isoformat(),
                "to":             to_date.isoformat(),
                "data": {sym: [d.isoformat() for d in dates]
                         for sym, dates in result.items()},
            }
            with open(_EARNINGS_CACHE, "w") as f:
                json.dump(cache_payload, f, indent=2)
        except Exception as ce:
            logger.debug(f"earnings_week cache write failed: {ce}")

        n = len(result)
        logger.info(
            f"FMP earnings batch: {n} symbol(s) with earnings in next {lookahead_days}d "
            f"({today} → {to_date}): {sorted(result.keys())}"
        )
        return result

    except Exception as e:
        logger.debug(f"FMP earnings-calendar batch failed: {e}")
        return {}


def get_cached_earnings_dates(symbol: str) -> list[date]:
    """
    Return upcoming earnings dates for a symbol from the pre-loaded cache.
    Falls back to live get_earnings_dates() on cache miss.

    Use this in the entry gate instead of get_earnings_dates() — avoids
    a live FMP call per symbol during the scan cycle.
    """
    sym = symbol.upper()
    try:
        if _EARNINGS_CACHE.exists():
            with open(_EARNINGS_CACHE) as f:
                cache = json.load(f)
            # Check cache freshness
            fetched = cache.get("fetched_at_utc")
            if fetched:
                age_h = (datetime.now(timezone.utc).timestamp() -
                         datetime.fromisoformat(fetched).timestamp()) / 3600
                if age_h <= _EARNINGS_TTL_H:
                    raw = cache.get("data", {}).get(sym, [])
                    return [date.fromisoformat(d) for d in raw]
    except Exception as e:
        logger.debug(f"[{sym}] earnings cache read failed: {e}")

    # Cache miss / stale → fall back to live call
    logger.debug(f"[{sym}] earnings cache miss — falling back to live FMP call")
    return get_earnings_dates(sym)


# ── Analyst sentiment (institutional signal proxy) ────────────────────────────

_ANALYST_CACHE = _CACHE_DIR / "analyst_cache.json"
_ANALYST_TTL_H = 168   # 7 days — analyst grades change slowly


def get_analyst_sentiment(
    symbol: str,
    lookback_days: int = 60,
    force_refresh: bool = False,
) -> dict:
    """
    Compute rolling analyst upgrade/downgrade sentiment for a symbol.

    Endpoint: GET /stable/grades?symbol={symbol}&apikey=...
    (Returns analyst ratings: Buy/Sell/Hold from major brokers.)

    Returns:
      {
        "signal":    "BULLISH" | "NEUTRAL" | "BEARISH",
        "net_score": float,     # (upgrades - downgrades) / total, range -1.0 to +1.0
        "upgrades":  int,
        "downgrades": int,
        "total":     int,
        "as_of":     "YYYY-MM-DD",
      }

    Signal thresholds:
      net_score > +0.25  → BULLISH  (≥25% net upgrade sentiment)
      net_score < -0.25  → BEARISH  (≥25% net downgrade sentiment)
      otherwise          → NEUTRAL

    Cache: data/cache/analyst_cache.json, 7-day TTL (quarterly pace).
    API cost: 1 call per symbol per week.

    Note: FMP free plan institutional 13F endpoints return 404.
          This function uses analyst grades as the best available proxy
          for institutional sentiment on the free plan.
    """
    sym     = symbol.upper()
    api_key = _key()

    # ── Cache read ────────────────────────────────────────────────────────────
    if not force_refresh:
        try:
            if _ANALYST_CACHE.exists():
                with open(_ANALYST_CACHE) as f:
                    cache = json.load(f)
                entry = cache.get(sym)
                if entry:
                    age_h = (datetime.now(timezone.utc).timestamp() -
                             datetime.fromisoformat(entry["fetched_at_utc"]).timestamp()) / 3600
                    if age_h <= _ANALYST_TTL_H:
                        logger.debug(
                            f"[{sym}] analyst cache hit: {entry['signal']} "
                            f"(net={entry['net_score']:+.2f} age={age_h:.0f}h)"
                        )
                        return {k: v for k, v in entry.items()
                                if k != "fetched_at_utc"}
        except Exception as e:
            logger.debug(f"[{sym}] analyst cache read failed: {e}")

    # ── Default neutral if no API key ────────────────────────────────────────
    if not api_key:
        logger.debug(f"[{sym}] FMP_API_KEY not set — analyst sentiment unavailable")
        return {"signal": "NEUTRAL", "net_score": 0.0, "upgrades": 0,
                "downgrades": 0, "total": 0, "as_of": "unavailable"}

    # ── Live fetch ────────────────────────────────────────────────────────────
    try:
        resp = requests.get(
            f"{_BASE_STABLE}/grades",
            params={"symbol": sym, "apikey": api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.debug(f"[{sym}] FMP grades: HTTP {resp.status_code}")
            return {"signal": "NEUTRAL", "net_score": 0.0, "upgrades": 0,
                    "downgrades": 0, "total": 0, "as_of": "unavailable"}

        cutoff = date.today() - timedelta(days=lookback_days)
        upgrades = downgrades = 0

        for entry in resp.json():
            d_str = entry.get("date", "")
            try:
                entry_date = date.fromisoformat(str(d_str)[:10])
            except Exception as _date_e:
                logger.debug("[%s] FMP analyst: date parse failed %r — %s", sym, d_str, _date_e)
                continue
            if entry_date < cutoff:
                continue

            grade = str(entry.get("newGrade") or entry.get("grade") or "").lower().strip()
            if grade in _BULLISH_GRADES:
                upgrades += 1
            elif grade in _BEARISH_GRADES:
                downgrades += 1

        total = upgrades + downgrades
        net_score = round((upgrades - downgrades) / max(total, 1), 3)

        if net_score > 0.25:
            signal = "BULLISH"
        elif net_score < -0.25:
            signal = "BEARISH"
        else:
            signal = "NEUTRAL"

        result = {
            "signal":     signal,
            "net_score":  net_score,
            "upgrades":   upgrades,
            "downgrades": downgrades,
            "total":      total,
            "as_of":      date.today().isoformat(),
        }

        # ── Cache write ───────────────────────────────────────────────────────
        try:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache = {}
            if _ANALYST_CACHE.exists():
                with open(_ANALYST_CACHE) as f:
                    cache = json.load(f)
            cache[sym] = {**result, "fetched_at_utc": datetime.now(timezone.utc).isoformat()}
            with open(_ANALYST_CACHE, "w") as f:
                json.dump(cache, f, indent=2)
        except Exception as ce:
            logger.debug(f"[{sym}] analyst cache write failed: {ce}")

        logger.debug(
            f"[{sym}] analyst sentiment: {signal} "
            f"(+{upgrades}/-{downgrades} net={net_score:+.2f} last {lookback_days}d)"
        )
        return result

    except Exception as e:
        logger.debug(f"[{sym}] FMP grades failed: {e}")
        return {"signal": "NEUTRAL", "net_score": 0.0, "upgrades": 0,
                "downgrades": 0, "total": 0, "as_of": "unavailable"}


# ── Dynamic universe screeners (TB-7) ─────────────────────────────────────────

def get_screener_movers(
    price_min: float = 5.0,
    price_max: float = 300.0,
    min_volume: int = 1_000_000,
) -> list[str]:
    """
    Return symbols from FMP biggest-gainers, biggest-losers, most-actives screeners.
    Filters by price and volume. Returns a deduplicated symbol list.
    T2 — 3 FMP calls per invocation.
    """
    api_key = _key()
    if not api_key:
        logger.debug("FMP_API_KEY not set — get_screener_movers unavailable")
        return []

    symbols: list[str] = []
    for endpoint in ("biggest-gainers", "biggest-losers", "most-actives"):
        try:
            resp = requests.get(
                f"{_BASE_STABLE}/{endpoint}",
                params={"apikey": api_key},
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.debug(f"FMP {endpoint}: HTTP {resp.status_code}")
                continue
            for q in resp.json():
                sym   = (q.get("symbol") or "").upper().strip()
                price = float(q.get("price") or 0)
                vol   = float(q.get("volume") or 0)
                if sym and price_min <= price <= price_max and vol >= min_volume:
                    symbols.append(sym)
        except Exception as e:
            logger.warning(f"FMP screener '{endpoint}' failed: {e}")

    result = list(dict.fromkeys(symbols))  # preserves insertion order, removes dupes
    logger.info(f"FMP screener movers: {len(result)} symbol(s) (T2)")
    return result
