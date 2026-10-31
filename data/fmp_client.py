# ruff: noqa: E501
"""
data/fmp_client.py
FMP (Financial Modeling Prep) API client.

Data tier: T2 — fundamentals, earnings calendar, analyst grades.
Earnings dates (EARN-SOURCE-1029): T2 Finnhub per-symbol calendar first, FMP per-symbol as fallback, UNKNOWN when
neither covers the symbol (design logs/design_records/earnings_source_finnhub_2026-10-29.md).
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

import fcntl
import hashlib
import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

def _fetch_symbol_earnings(symbol: str, limit: int = 5) -> "tuple[int, list[date]]":
    """(HTTP status, dates most-recent-first) from GET /stable/earnings?symbol=. Status 0 = no key or a request
    error. Never raises."""
    api_key = _key()
    if not api_key:
        return 0, []
    try:
        resp = requests.get(f"{_BASE_STABLE}/earnings", params={"symbol": symbol, "apikey": api_key},
                            timeout=_TIMEOUT)
        if resp.status_code != 200:
            return int(resp.status_code), []
        dates = []
        for entry in resp.json()[:limit]:
            d_str = entry.get("date")
            if d_str:
                try:
                    dates.append(date.fromisoformat(str(d_str)[:10]))
                except Exception as _date_e:
                    logger.debug("[%s] FMP earnings: date parse failed %r — %s", symbol, d_str, _date_e)
        return 200, dates
    except Exception as e:
        logger.debug(f"[{symbol}] FMP earnings failed: {e}")
        return 0, []


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

        # Persist cache: a locked, atomic, per-symbol MERGE (design record qhm_earnings_dates_fix_2026-10-12.md; two
        # writers — events/earnings_fetcher and earnings_preflight — used to overwrite each other's scope). Each scanned
        # symbol records the window it was scanned through, so "scanned, no earnings" differs from "never scanned".
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            with _earnings_lock():
                cur = _read_json(_EARNINGS_CACHE)
                syms = dict(cur.get("symbols") or {}) if isinstance(cur.get("symbols"), dict) else {}
                for sym in symbol_set:
                    old = syms.get(sym) if isinstance(syms.get(sym), dict) else None
                    new_dates = [d.isoformat() for d in result.get(sym, [])]
                    scanned_to = to_date.isoformat()
                    # A narrower fresh scan never drops a wider fresh one: the new scan answers its own window, the old
                    # entry's dates beyond it are kept, and the entry is re-stamped now (adversarial review: keeping the
                    # old stamp let the whole batch expire mid-session and fan out to per-symbol calls).
                    if (old is not None and _fresh(old.get("fetched_at_utc"), _EARNINGS_TTL_H)
                            and str(old.get("scanned_to") or "") > scanned_to):
                        new_dates = sorted(set(new_dates) | {d for d in (old.get("dates") or []) if str(d) > scanned_to})
                        scanned_to = str(old.get("scanned_to"))
                    syms[sym] = {"dates": new_dates, "scanned_to": scanned_to, "fetched_at_utc": now_iso}
                cache_payload = {
                    "fetched_at_utc": now_iso,
                    "from":           today.isoformat(),
                    "to":             to_date.isoformat(),
                    "symbols":        syms,
                    # legacy readers (earnings_preflight audit line, weekly_postmortem): fresh symbols with dates
                    "data": {k: v["dates"] for k, v in syms.items()
                             if v.get("dates") and _fresh(v.get("fetched_at_utc"), _EARNINGS_TTL_H)},
                }
                _atomic_write_json(_EARNINGS_CACHE, cache_payload)
        except Exception as ce:
            logger.warning(f"earnings_week cache write failed: {ce}")

        n = len(result)
        logger.info(
            f"FMP earnings batch: {n} symbol(s) with earnings in next {lookahead_days}d "
            f"({today} → {to_date}): {sorted(result.keys())}"
        )
        return result

    except Exception as e:
        logger.debug(f"FMP earnings-calendar batch failed: {e}")
        return {}


_EARNINGS_SYMBOL_CACHE = _CACHE_DIR / "earnings_symbol_cache.json"   # per-symbol lookups incl. negatives
_EARNINGS_SYMBOL_TTL_H = 24          # at most one live per-symbol call per symbol per day (FMP free plan: 250/day)
_EARNINGS_LOCK = _CACHE_DIR / ".earnings_cache.lock"

EARNINGS_DATED = "DATED"                      # a scheduled date is known
EARNINGS_NONE_IN_WINDOW = "NONE_IN_WINDOW"    # scanned/looked up: no scheduled date in range
EARNINGS_UNKNOWN = "UNKNOWN"                  # not covered: unscanned for the horizon, plan-blocked, or an error


@contextmanager
def _earnings_lock():
    """Exclusive cross-process lock around an earnings-cache read-modify-write (writes only; readers rely on the
    atomic replace). Milliseconds of hold time; no network call inside."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_EARNINGS_LOCK, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text())
            return d if isinstance(d, dict) else {}
    except Exception as e:
        logger.debug("earnings cache read failed (%s): %s", path.name, e)
    return {}


def _atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def _fresh(ts: object, ttl_h: float) -> bool:
    try:
        age_h = (datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(str(ts)).timestamp()) / 3600
        return 0 <= age_h <= ttl_h
    except Exception:
        return False


# ── Per-symbol earnings: Finnhub first, FMP fallback (EARN-SOURCE-1029) ─────────────────────────────────────────────
# Design: logs/design_records/earnings_source_finnhub_2026-10-29.md (v4). FMP's plan blocks per-symbol earnings for
# about half our names (HTTP 402) and its calendar is a ~64-name sample; Finnhub's per-symbol calendar covered 37/37.
_ET = ZoneInfo("America/New_York")
_EARNINGS_SCHEMA = 2                 # cache entries without v == 2 are stale (pre-Finnhub FMP-402 entries re-asked)
_FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
_FINNHUB_TIMEOUT = 4.0               # request timeout seconds (plumbing)
_FINNHUB_BACK_DAYS = 120             # query window: past reports show a report already happened (plumbing)
_FINNHUB_FWD_DAYS = 180              # query window ahead; longest caller horizon is 35 d (plumbing)
_FAILED_TTL_H = 1.0                  # any lookup where Finnhub failed is re-asked after 1 h (plumbing)
_RETRY_AFTER_CAP_S = 60              # Finnhub 429 Retry-After honoured up to 60 s (plumbing)
_WARM_SPACING_S = 1.0                # warmer: at most 1 Finnhub call per second (free plan 60/min, plumbing)
_LIVE_BUDGET_S = 300.0               # trading process: at most one live lookup per 5 minutes (plumbing)
_SYMBOL_ALIASES = {"BRK.B": "BRK.A", "BRK-B": "BRK.A"}   # Finnhub returns BRK.B rows labelled BRK.A (measured)
_live_lock = threading.Lock()
_last_live_ts = 0.0


def _et_today() -> date:
    return datetime.now(_ET).date()


def _finnhub_key() -> str:
    return os.getenv("FINNHUB_API_KEY", "")


def _fetch_finnhub_earnings(sym: str) -> "tuple[int, list[tuple[date, str]], int]":
    """(HTTP status, [(date, hour)], retry_after_s) from Finnhub's per-symbol calendar. Status 0 = no key or a
    request error. Rows for any symbol other than `sym` or its alias are dropped. Never raises; never logs the
    exception text (the request URL carries the token)."""
    key = _finnhub_key()
    if not key:
        return 0, [], 0
    today = _et_today()
    try:
        resp = requests.get(_FINNHUB_URL, params={
            "symbol": sym,
            "from": (today - timedelta(days=_FINNHUB_BACK_DAYS)).isoformat(),
            "to": (today + timedelta(days=_FINNHUB_FWD_DAYS)).isoformat(),
            "token": key,
        }, timeout=_FINNHUB_TIMEOUT)
        if resp.status_code != 200:
            retry = 0
            if resp.status_code == 429:
                try:
                    retry = int(float(resp.headers.get("Retry-After") or _RETRY_AFTER_CAP_S))
                except (TypeError, ValueError):
                    retry = _RETRY_AFTER_CAP_S
            return int(resp.status_code), [], min(max(retry, 0), _RETRY_AFTER_CAP_S)
        body = resp.json()
        rows = (body.get("earningsCalendar") if isinstance(body, dict) else None) or []
        allowed = {sym, _SYMBOL_ALIASES.get(sym, sym)}
        out: list = []
        for r in rows:
            if not isinstance(r, dict) or str(r.get("symbol") or "").upper() not in allowed:
                continue
            try:
                out.append((date.fromisoformat(str(r.get("date"))[:10]), str(r.get("hour") or "")))
            except ValueError:
                continue
        return 200, out, 0
    except Exception as e:
        logger.debug("[%s] Finnhub earnings request failed: %s", sym, type(e).__name__)
        return 0, [], 0


def _nearest_first(dates: list, today: date) -> list:
    """Deduped dates ordered by distance from `today`; a tie (yesterday vs tomorrow) puts the earlier date first.
    Callers that read only the first two dates (entry_logic) then always see the nearest report."""
    uniq = sorted({d for d in dates if isinstance(d, date)})
    return sorted(uniq, key=lambda d: (abs((d - today).days), d))


def _symbol_fetch(sym: str, finnhub_allowed: bool = True) -> "tuple[dict, int]":
    """Fresh v2 cache entry for `sym` (Finnhub, else FMP, else uncovered) and Finnhub's Retry-After on a 429."""
    today = _et_today()
    tried: list = []
    statuses: dict = {}
    retry_after = 0
    provider, covered, dates, hours = "none", False, [], {}
    if finnhub_allowed:
        st, rows, retry_after = _fetch_finnhub_earnings(sym)
        tried.append("finnhub")
        statuses["finnhub"] = st
        if st == 200 and rows:
            provider, covered = "finnhub", True
            dates = [d for d, _ in rows]
            hours = {d.isoformat(): h for d, h in rows if h}
    if not covered:
        fst, fdates = _fetch_symbol_earnings(sym)
        tried.append("fmp")
        statuses["fmp"] = fst
        if fst == 200:
            provider, covered, dates = "fmp", True, list(fdates)
    finnhub_ok = provider == "finnhub"
    iso_dates = [d.isoformat() for d in _nearest_first(dates, today)]
    entry: dict = {
        "v": _EARNINGS_SCHEMA, "provider": provider, "providers_tried": tried, "statuses": statuses,
        "covered": covered, "dates": iso_dates, "hours": hours,
        "window": [(today - timedelta(days=_FINNHUB_BACK_DAYS)).isoformat(),
                   (today + timedelta(days=_FINNHUB_FWD_DAYS)).isoformat()],
        "ttl_h": _EARNINGS_SYMBOL_TTL_H if finnhub_ok else _FAILED_TTL_H,
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),   # taken after the requests complete
    }
    nxt = next((d for d in iso_dates if d >= today.isoformat()), None)
    logger.info("EARNLOOKUP sym=%s provider=%s tried=%s statuses=%s covered=%s next=%s pid=%s", sym, provider,
                ",".join(tried), statuses, covered, nxt, os.getpid())
    if not covered:
        logger.warning("[%s] earnings date UNKNOWN (Finnhub %s, FMP %s)", sym, statuses.get("finnhub", "skipped"),
                       statuses.get("fmp"))
    return entry, retry_after


def _store_symbol_entry(sym: str, entry: dict) -> None:
    try:
        with _earnings_lock():
            cur = _read_json(_EARNINGS_SYMBOL_CACHE)
            cur[sym] = entry
            _atomic_write_json(_EARNINGS_SYMBOL_CACHE, cur)
    except Exception as we:
        logger.warning("[%s] earnings symbol cache write failed: %s", sym, we)


def _cached_symbol_entry(sym: str) -> "dict | None":
    """The fresh v2 per-symbol entry, or None (missing, pre-v2, or older than its own TTL)."""
    e = _read_json(_EARNINGS_SYMBOL_CACHE).get(sym)
    if not (isinstance(e, dict) and e.get("v") == _EARNINGS_SCHEMA):
        return None
    try:
        ttl = float(e.get("ttl_h") or _FAILED_TTL_H)
    except (TypeError, ValueError):
        ttl = _FAILED_TTL_H
    return e if _fresh(e.get("fetched_at_utc"), ttl) else None


def _expires_at(entry: dict) -> "datetime | None":
    """When a v2 entry stops being fresh (fetched_at_utc + its own TTL); None when unreadable."""
    try:
        ttl = float(entry.get("ttl_h") or _FAILED_TTL_H)
        return datetime.fromisoformat(str(entry.get("fetched_at_utc"))) + timedelta(hours=ttl)
    except (TypeError, ValueError):
        return None


def _in_trading_process() -> bool:
    argv0 = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    return argv0 == "main.py"


def _live_allowed() -> bool:
    """Outside the trading process: always. Inside it (main.py, where QHM runs on run_cycle): at most one live lookup
    per _LIVE_BUDGET_S, reserved under a lock before any network call."""
    global _last_live_ts
    if not _in_trading_process():
        return True
    with _live_lock:
        now = time.monotonic()
        if _last_live_ts and now - _last_live_ts < _LIVE_BUDGET_S:
            return False
        _last_live_ts = now
        return True


def _entry_info(entry: dict) -> dict:
    """Public answer for one v2 entry: state, dates (nearest first), source and provenance."""
    today = _et_today()
    rid = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:12]
    base = {"provider": entry.get("provider"), "fetched_at_utc": entry.get("fetched_at_utc"), "fresh": True,
            "record_id": rid}
    if not entry.get("covered"):
        return {"state": EARNINGS_UNKNOWN, "dates": [], "source": "symbol_uncovered", **base}
    dates = []
    for d in entry.get("dates") or []:
        try:
            dates.append(date.fromisoformat(str(d)[:10]))
        except ValueError:
            continue
    dates = _nearest_first(dates, today)
    upcoming = [d for d in dates if d >= today]
    src = "finnhub_symbol" if entry.get("provider") == "finnhub" else "fmp_symbol"
    return {"state": EARNINGS_DATED if upcoming else EARNINGS_NONE_IN_WINDOW, "dates": dates, "source": src, **base}


def _symbol_lookup(sym: str, allow_live: bool = True) -> dict:
    """Per-symbol answer from the fresh v2 cache, else a live lookup when allowed and within budget, else UNKNOWN
    (an expired entry never acts). Never raises."""
    e = _cached_symbol_entry(sym)
    if e is None:
        if not (allow_live and _live_allowed()):
            return {"state": EARNINGS_UNKNOWN, "dates": [], "source": "cache_miss", "provider": None,
                    "fetched_at_utc": None, "fresh": False, "record_id": None}
        e, _ = _symbol_fetch(sym)
        _store_symbol_entry(sym, e)
    return _entry_info(e)


def _batch_dated(sym: str) -> "dict | None":
    """A fresh batch-calendar entry that HAS dates (the batch is a sample; its "none" never answers)."""
    e = (_read_json(_EARNINGS_CACHE).get("symbols") or {}).get(sym)
    if not (isinstance(e, dict) and _fresh(e.get("fetched_at_utc"), _EARNINGS_TTL_H)):
        return None
    dates = []
    for d in e.get("dates") or []:
        try:
            dates.append(date.fromisoformat(str(d)[:10]))
        except ValueError:
            continue
    today = _et_today()
    if not any(d >= today for d in dates):   # past-only batch dates never answer as DATED (cold-2nd T3)
        return None
    return {"state": EARNINGS_DATED, "dates": _nearest_first(dates, today), "source": "fmp_batch",
            "provider": "fmp", "fetched_at_utc": e.get("fetched_at_utc"), "fresh": True, "record_id": None}


def _resolve(sym: str, allow_live: bool) -> dict:
    """Precedence (design item 5): per-symbol DATED > dated batch > per-symbol NONE_IN_WINDOW > UNKNOWN."""
    per = _symbol_lookup(sym, allow_live=allow_live)
    if per.get("state") == EARNINGS_DATED:
        out = per
    else:
        batch = _batch_dated(sym)
        out = batch if batch is not None else per
    if out.get("state") == EARNINGS_DATED:
        today = _et_today()
        nxt = next((d for d in out["dates"] if d >= today), None)
        logger.info("EARNSERVE sym=%s provider=%s record=%s fetched=%s next=%s thread=%s", sym, out.get("provider"),
                    out.get("record_id"), out.get("fetched_at_utc"), nxt, threading.current_thread().name)
    return out


def get_symbol_earnings_info(symbol: str) -> dict:
    """The authoritative per-symbol answer (Finnhub first, FMP fallback; see _resolve for precedence). A plan block,
    an error or an uncovered symbol is UNKNOWN, never "no earnings". Never raises."""
    try:
        return _resolve(symbol.upper(), allow_live=True)
    except Exception as ex:
        logger.warning("[%s] per-symbol earnings info failed: %s", symbol, ex)
        return {"state": EARNINGS_UNKNOWN, "dates": [], "source": "error"}


def get_earnings_info(symbol: str, min_horizon_days: int = 0) -> dict:
    """{"state": DATED | NONE_IN_WINDOW | UNKNOWN, "dates": [date, ...] nearest first, "source": ..., provenance}.

    Same answer as get_symbol_earnings_info: the per-symbol query window (ET date −120 d .. +180 d) covers every
    caller horizon (QHM 21 d, preflight 35 d), so `min_horizon_days` no longer changes the answer and is kept for
    callers. UNKNOWN is never reported as "no earnings". Never raises."""
    sym = symbol.upper()
    try:
        return _resolve(sym, allow_live=True)
    except Exception as ex:
        logger.warning("[%s] earnings info failed: %s", sym, ex)
        return {"state": EARNINGS_UNKNOWN, "dates": [], "source": "error"}


def get_cached_earnings_dates(symbol: str) -> list[date]:
    """Earnings dates for a symbol (legacy list contract), nearest first: CACHE ONLY — never calls a provider (the
    main entry path runs on the trading thread). [] when nothing fresh is known."""
    try:
        return list(_resolve(symbol.upper(), allow_live=False).get("dates") or [])
    except Exception as ex:
        logger.warning("[%s] cached earnings dates failed: %s", symbol, ex)
        return []


def warm_symbol_earnings(symbols: list, refresh_until: "datetime | None" = None) -> dict:
    """Refresh every symbol whose v2 entry is stale, or would go stale before `refresh_until` (tz-aware; the caller
    passes the next regular-session close so an entry never expires mid-session between warm runs — cold-2nd T1),
    sequentially, in its own process (never the trading thread). The trading process keeps the strict expiry.
    At most 1 provider call per second. A 429: that symbol gets its FMP fallback at once (1 h TTL), the warmer waits
    Retry-After (cap 60 s) once, and a second 429 stops Finnhub calls for the rest of the run (FMP only, 1 h TTL).
    Returns counts by provider/state. Never raises."""
    counts: dict = {"attempted": 0, "finnhub": 0, "fmp": 0, "unknown": 0, "fresh_skipped": 0, "r429": 0}
    finnhub_on = True
    for raw in symbols:
        sym = str(raw or "").upper().strip()
        if not sym:
            continue
        try:
            cached = _cached_symbol_entry(sym)
            if cached is not None:
                exp = _expires_at(cached)
                if refresh_until is None or (exp is not None and exp >= refresh_until):
                    counts["fresh_skipped"] += 1
                    continue
            counts["attempted"] += 1
            entry, retry_after = _symbol_fetch(sym, finnhub_allowed=finnhub_on)
            _store_symbol_entry(sym, entry)
            counts[entry["provider"] if entry.get("covered") else "unknown"] = \
                counts.get(entry["provider"] if entry.get("covered") else "unknown", 0) + 1
            info = _entry_info(entry)
            today = _et_today()
            nxt = next((d for d in info["dates"] if d >= today), None)
            logger.info("EARNWARM sym=%s provider=%s tried=%s statuses=%s state=%s next=%s hour=%s window=%s",
                        sym, entry.get("provider"), ",".join(entry.get("providers_tried") or []),
                        entry.get("statuses"), info["state"], nxt,
                        (entry.get("hours") or {}).get(nxt.isoformat()) if nxt else None, entry.get("window"))
            if finnhub_on and (entry.get("statuses") or {}).get("finnhub") == 429:
                counts["r429"] += 1
                if counts["r429"] >= 2:
                    finnhub_on = False
                    time.sleep(_WARM_SPACING_S)   # spacing before the next (FMP-only) call (Codex preship)
                else:
                    time.sleep(retry_after or _RETRY_AFTER_CAP_S)
            else:   # spacing also applies to FMP-only calls once Finnhub is off (cold-2nd N4)
                time.sleep(_WARM_SPACING_S)
        except Exception as ex:
            counts["unknown"] += 1
            logger.warning("[%s] earnings warm failed: %s", sym, ex)
    logger.info("EARNWARM summary %s", counts)
    return counts


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
