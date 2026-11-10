#!/usr/bin/env python3
# ruff: noqa: E501
"""
events/catalyst_engine.py — per-name catalyst detection + classification (increment 1a, DARK).

Complements events/news_monitor.py (which is MACRO-reaction-first + display-only). THIS is the
per-name ACTIONABLE layer Rafael asked for after RIVN: for each held/candidate symbol, fetch
stock-SPECIFIC news, classify it (negative / positive / neutral) with a rule-based catalog, and
expose has_blocking_catalyst()/get_active_catalysts() for a future entry-gate.

WHY THIS EXISTS: news_monitor queries GLOBAL feeds (category=general, the global 8-K stream) and
never screens a headline against a HELD name. RIVN's dilutive public offering (2026-07-06) was
therefore never flagged against the RIVN position — the bot bought into it. This engine closes
that gap by asking, per name, "is there a stock-specific catalyst that should block/allow a trade?"

INCREMENT 1a = DETECTOR ONLY (read-only): fetch + classify + log. It does NOT gate entries yet —
`CATALYST_GATE_ENABLED = False`. Wiring it into the entry gate (1b) reverses the news-display-only
invariant for stock-specific-on-held names and requires a board vote. Shipping dark first so the
classification can be validated on real data before it ever blocks a live trade.

MACRO stays display-only (news_monitor's job); this engine only ever acts on STOCK-SPECIFIC
catalysts for names in the watch set — so "NVDA stock split" is actionable while "Trump tariffs"
is not even fetched here.

DATA (per-symbol):
  T1  Alpaca News API — https://data.alpaca.markets/v1beta1/news?symbols=X (free, Benzinga feed).
  (1b will add SEC EDGAR per-CIK S-1/S-3/424B/8-K for offerings the newswire lags, + Finnhub backup.)

USAGE:  python3 events/catalyst_engine.py [SYM ...]   # detector report (defaults to held+universe)
"""
from __future__ import annotations

import os
import sys
import json
import math
import time
import logging
import urllib.parse
import urllib.request
import urllib.error
from typing import NamedTuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

logger = logging.getLogger("catalyst_engine")

PT = ZoneInfo("America/Los_Angeles")
_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "state" / "catalyst_state.json"   # atomic snapshot for the dashboard/gate

# ── GATE FLAG — LIVE (Rafael go 2026-07-14). Detector validated on real market-day news
# (classifier accurate, co-tag attribution bug caught+fixed); wiring is entry-branch-only with
# never-mask fault handling (stale/missing cache → block nothing + alert). The ~10-min RTH cron
# refreshes catalyst_state.json before each session's open. Reverses Architecture Invariant #2
# (news display-only) NARROWLY: only stock-specific high-severity NEGATIVE catalysts on watched
# names block NEW entries; macro news stays display-only; exits/never-sell tiers untouched. ──
CATALYST_GATE_ENABLED = True

# Only consider catalysts within this recency window (dynamic: newest wins, not a static score).
_LOOKBACK_HOURS = 72

# ── 1b entry-gate policy (BGG: Gro + GAI + cold seat, 2026-07-13) ──
# Block ONLY these high-severity, unambiguous negative types (add downgrade/leadership/recall
# later after validation). A catalyst older than _BLOCK_MAX_AGE_HOURS is treated as priced-in
# (Simons: act only on signal whose decay you understand). Cache older than _CACHE_STALE_HOURS
# is a FAULT → block NOTHING + alert (never-mask: absence of data is not a trade decision).
_BLOCKING_TYPES: tuple[str, ...] = ("dilution_offering", "guidance_cut", "solvency", "legal_probe")
# Per-type freshness for BLOCKING. Event-like catalysts (an offering, a guidance cut) price in
# within days → 48h. Structural catalysts (bankruptcy/delisting, SEC probe/fraud) persist and
# are NOT priced-in in 48h → 30-day window (GAI 1b review).
_BLOCK_MAX_AGE_HOURS = 48.0
_BLOCK_MAX_AGE_BY_TYPE: dict[str, float] = {"solvency": 720.0, "legal_probe": 720.0}
_CACHE_STALE_HOURS = 1.0

# ── Rule-based classification catalogs (lowercase substring match on headline+summary) ──
# NEGATIVE → a future entry-gate would BLOCK new entries into the name (the RIVN class).
_NEGATIVE: dict[str, tuple[str, ...]] = {
    "dilution_offering": ("public offering", "secondary offering", "registered direct",
                          "at-the-market offering", "atm offering", "common stock offering",
                          "proposed offering", "prices offering", "prices public offering",
                          "dilut", "share offering", "stock offering", "convertible notes",
                          "convertible senior notes", "shelf registration"),
    "guidance_cut":      ("cuts guidance", "lowers guidance", "slashes guidance", "profit warning",
                          "warns on", "cuts forecast", "lowers forecast", "guides below",
                          "withdraws guidance", "cuts outlook"),
    "downgrade":         ("downgrade", "downgraded", "cut to sell", "cut to underperform",
                          "double downgrade"),
    "legal_probe":       ("sec investigation", "sec probe", "subpoena", "securities fraud",
                          "class action", "doj investigation", "accounting probe",
                          "formal investigation"),
    "solvency":          ("going concern", "bankruptcy", "chapter 11", "delisting",
                          "restatement", "restate", "default on"),
    "leadership_shock":  ("ceo resigns", "ceo steps down", "cfo resigns", "cfo steps down",
                          "ceo departs", "abruptly resigns", "unexpected departure"),
    "recall_probe":      ("recall", "safety probe", "nhtsa investigation", "product recall"),
}
# POSITIVE → informational; a future gate could ALLOW a QHM/F6 add (not part of 1a/1b entry-block).
_POSITIVE: dict[str, tuple[str, ...]] = {
    "buyback":       ("buyback", "repurchase program", "accelerated repurchase", "authorizes repurchase"),
    "split":         ("stock split", "forward split", "announces split"),
    "beat_raise":    ("beats estimates", "tops estimates", "raises guidance", "raises forecast",
                      "record revenue", "boosts outlook", "raises outlook"),
    "upgrade":       ("upgrade", "upgraded", "raised to buy", "initiated buy", "price target raised",
                      "raised price target"),
    "m_and_a":       ("to be acquired", "acquisition of", "takeover bid", "buyout offer",
                      "agrees to acquire", "merger agreement"),
}


def _classify(text: str) -> tuple[str, str | None]:
    """Return (polarity, catalyst_type). polarity ∈ {negative, positive, neutral}.
    Negative takes precedence over positive when both match (risk-first)."""
    t = (text or "").lower()
    for ctype, kws in _NEGATIVE.items():
        if any(k in t for k in kws):
            return "negative", ctype
    for ctype, kws in _POSITIVE.items():
        if any(k in t for k in kws):
            return "positive", ctype
    return "neutral", None


def _fetch_alpaca_news(symbols: list[str], limit: int = 25) -> list[dict]:
    """Per-symbol news from the Alpaca News API (T1, free Benzinga feed). Read-only.
    Returns [{symbol, headline, summary, created_at(ISO), url, source}]. Fail-soft to []."""
    key = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not sec or not symbols:
        logger.warning("catalyst: Alpaca creds or symbols missing — no per-name news fetched")
        return []
    start = (datetime.now(timezone.utc) - timedelta(hours=_LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = ("https://data.alpaca.markets/v1beta1/news"
           f"?symbols={','.join(symbols)}&start={start}&limit={min(limit, 50)}&sort=desc")
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec, "Accept": "application/json",
    })
    import ssl
    try:
        import certifi
        _ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _ctx = ssl.create_default_context()
    out: list[dict] = []
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ctx) as resp:
            data = json.loads(resp.read().decode())
        for n in data.get("news", []) or []:
            syms = n.get("symbols") or []
            out.append({
                "symbols":    syms,
                "headline":   n.get("headline", "") or "",
                "summary":    (n.get("summary", "") or "")[:280],
                "created_at": n.get("created_at", "") or "",
                "url":        n.get("url", "") or "",
                "source":     "alpaca_news",  # data-tier tag
            })
    except urllib.error.HTTPError as e:
        logger.warning("catalyst: Alpaca News HTTP %s — %s", e.code, e.reason)
    except Exception as e:
        logger.warning("catalyst: Alpaca News fetch failed: %s", e)
    return out


class NewsFetch(NamedTuple):
    """A complete-or-not read of one symbol's news since a moment (QHM-REENTRY-NEWS-1109)."""
    items: list
    complete: bool
    error: str
    pages: int
    # The end of the window asked for (aware UTC, floored to the second the API was sent); None when no request was
    # made. A caller reading incrementally starts its next read here after a COMPLETE read.
    end: datetime | None = None


# The Alpaca News API's own maximum page size (its documented limit; the cron read above caps at the same value).
_NEWS_PAGE_SIZE = 50
# Wall-clock budget for one fetch_news_since call: it runs on the single trading thread (check_exits cannot run
# while it does). Checked before every page request; when spent the read is incomplete ("time budget").
_NEWS_READ_TIMEOUT_SECONDS = 30


def _news_ts(raw: object) -> datetime | None:
    """An article's created_at as an aware UTC datetime, or None when missing / unparseable / naive."""
    try:
        ts = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return None
    return ts.astimezone(timezone.utc)


def _news_page_cap(oldest_seen: datetime | None, pages_read: int, start: datetime, end: datetime) -> int | None:
    """Pages allowed for a read of start..end, re-derived after EVERY page from all pages read so far (sorted newest
    first): they cover end - the oldest article seen, i.e. (that span / pages read) per page; the window needs about
    (end - start) / that per-page span pages, and the cap allows the same again as headroom. One dense first page
    therefore cannot fix a huge cap. None when the rate cannot be measured (the read is then incomplete)."""
    if oldest_seen is None or pages_read <= 0:
        return None
    span = (end - oldest_seen).total_seconds()
    window = (end - start).total_seconds()
    if span <= 0 or window <= 0:
        return None
    est = math.ceil(window / (span / pages_read))
    return est + est


def fetch_news_since(symbol: str, start_utc: datetime) -> NewsFetch:
    """EVERY Alpaca News (T1) article tagged with `symbol` from start_utc (inclusive) to now, read page by page
    through next_page_token. Read-only. complete=True only when the API ends the listing (an HTTP 200 page whose
    next_page_token field is present and null/empty); any HTTP error, timeout, network error, unreadable JSON, page
    without a news list or without a next_page_token field, unreadable article time, repeated page token, reaching
    the page cap (re-derived after every page) or the _NEWS_READ_TIMEOUT_SECONDS budget returns complete=False
    with the reason; `end` is the window end sent to the API (the next incremental read starts there) —
    the caller must then treat the news as unknown. Duplicate articles (same id) are kept once. Items carry the
    same fields as _fetch_alpaca_news plus id. Used by the QHM re-entry dip release; the cron path is untouched."""
    sym = (symbol or "").strip().upper()
    key = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not sec or not sym:
        return NewsFetch([], False, "credentials or symbol missing", 0)
    if not isinstance(start_utc, datetime) or start_utc.tzinfo is None:
        return NewsFetch([], False, "start time is not timezone-aware", 0)
    start = start_utc.astimezone(timezone.utc)
    # Floored to the second actually sent: an article inside the dropped fraction is read by the next incremental
    # read (which starts at this exact second), never lost between two reads.
    end = datetime.now(timezone.utc).replace(microsecond=0)
    if start >= end:
        return NewsFetch([], False, "start time is not in the past", 0)
    # The API's start is second-resolution: floor it so an article at the exact start second is returned;
    # the exact start is then applied to each article below.
    params = {"symbols": sym, "start": start.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "sort": "desc", "limit": str(_NEWS_PAGE_SIZE)}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec, "Accept": "application/json"}
    import ssl
    try:
        import certifi
        _ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _ctx = ssl.create_default_context()
    items: list[dict] = []
    seen_ids: set = set()
    seen_tokens: set[str] = set()
    token: str | None = None
    pages = 0
    oldest_seen: datetime | None = None
    started = time.monotonic()

    def _done(complete: bool, error: str) -> NewsFetch:
        logger.info("catalyst news_since %s: start=%s pages=%d items=%d complete=%s%s", sym, start.isoformat(),
                    pages, len(items), complete, (" error=%s" % error) if error else "")
        return NewsFetch(items, complete, error, pages, end)

    while True:
        request_timeout_left = _NEWS_READ_TIMEOUT_SECONDS - (time.monotonic() - started)
        if request_timeout_left <= 0:
            return _done(False, "time budget %ss spent after %d page(s)" % (_NEWS_READ_TIMEOUT_SECONDS, pages))
        q = dict(params)
        if token:
            q["page_token"] = token
        req = urllib.request.Request("https://data.alpaca.markets/v1beta1/news?" + urllib.parse.urlencode(q),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=min(10, request_timeout_left), context=_ctx) as resp:
                status = getattr(resp, "status", None)
                if status != 200:
                    return _done(False, "HTTP %s" % status)
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return _done(False, "HTTP %s" % e.code)
        except Exception as e:
            return _done(False, "request failed: %s" % e)
        pages += 1
        news = data.get("news") if isinstance(data, dict) else None
        if not isinstance(news, list):
            return _done(False, "page without a news list")
        for n in news:
            if not isinstance(n, dict):
                return _done(False, "malformed article")
            ts = _news_ts(n.get("created_at"))
            if ts is None:
                return _done(False, "article time unreadable")
            oldest_seen = ts if oldest_seen is None or ts < oldest_seen else oldest_seen
            if ts < start:
                continue
            aid = n.get("id")
            ident = ("id", aid) if aid is not None else ("row", n.get("created_at"), n.get("headline"))
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            items.append({
                "id":         aid,
                "symbols":    n.get("symbols") or [],
                "headline":   n.get("headline", "") or "",
                "summary":    n.get("summary", "") or "",      # whole summary: every word is classified
                "created_at": n.get("created_at", "") or "",
                "url":        n.get("url", "") or "",
                "source":     "alpaca_news",  # data-tier tag
            })
        if "next_page_token" not in data:
            return _done(False, "page without a next_page_token field")
        nxt = data.get("next_page_token")
        if not nxt:
            return _done(True, "")
        if not isinstance(nxt, str):
            return _done(False, "page token unreadable")
        if nxt in seen_tokens:
            return _done(False, "repeated page token")
        seen_tokens.add(nxt)
        cap = _news_page_cap(oldest_seen, pages, start, end)
        if cap is None:
            return _done(False, "page cap not derivable from the pages read")
        if pages >= cap:
            return _done(False, "page cap %d reached" % cap)
        token = nxt


def get_active_catalysts(symbols: list[str]) -> dict[str, dict]:
    """For each symbol, the most-recent NON-neutral stock-specific catalyst within the lookback.
    Returns {symbol: {polarity, type, headline, created_at, url}}. Newest wins (dynamic, not static).
    Read-only — safe to call anywhere."""
    items = _fetch_alpaca_news(symbols)
    watch = {s.upper() for s in symbols}
    result: dict[str, dict] = {}
    # items are sorted newest-first; keep the first non-neutral match per symbol.
    for it in items:
        pol, ctype = _classify(f"{it['headline']} {it['summary']}")
        if pol == "neutral":
            continue
        syms = [str(s).upper() for s in it.get("symbols", [])]
        # ATTRIBUTION: a stock-specific catalyst belongs to ONE company — its SUBJECT. Benzinga
        # lists the subject as the PRIMARY (first) symbol. Rule: attribute to the primary IF it
        # is a watched name; else attribute ONLY to a watched symbol whose TICKER appears in the
        # HEADLINE. NEVER attribute to an incidental co-tag (an index or unrelated ticker that
        # merely appears in the symbols list). This is what stops a RIVN-offering article
        # co-tagged [RIVN,TSLA] from flagging TSLA, or a WBD/PSKY-merger co-tagged SPY from
        # flagging SPY. (Validation catch, 2026-07-14 — co-tag false positives found live.)
        hl = it["headline"].upper()
        primary = syms[0] if syms else None
        target: str | None
        if primary in watch:
            target = primary
        else:
            target = next((s for s in syms if s in watch and s in hl), None)
        if target and target not in result:
            result[target] = {
                "polarity":   pol,
                "type":       ctype,
                "headline":   it["headline"],
                "created_at": it["created_at"],
                "url":        it["url"],
                "source":     it["source"],
            }
    return result


def read_cached_state() -> dict:
    """Read the cron-refreshed catalyst_state.json for the entry gate (fast, no network).
    Validates schema + freshness. Returns {"ok": bool, "active": {sym: catalyst}, "reason": str}.
    On missing / unparseable / STALE (> _CACHE_STALE_HOURS) → ok=False, active={}: the caller must
    NOT block on ok=False (never-mask — absence of data is an infra fault, not a signal) and should
    fire a CRITICAL alert. Only a positively-classified negative catalyst in a FRESH cache blocks."""
    try:
        if not _STATE.exists():
            return {"ok": False, "active": {}, "reason": "catalyst_state.json missing (cron not run?)"}
        payload = json.loads(_STATE.read_text())
        if (not isinstance(payload, dict) or "updated" not in payload
                or not isinstance(payload.get("active"), dict)):
            return {"ok": False, "active": {}, "reason": "catalyst_state.json malformed schema"}
        updated = datetime.fromisoformat(payload["updated"])
        age_h = (datetime.now(PT) - updated).total_seconds() / 3600.0
        if age_h > _CACHE_STALE_HOURS:
            return {"ok": False, "active": {},
                    "reason": f"catalyst cache stale ({age_h:.1f}h > {_CACHE_STALE_HOURS}h)"}
        return {"ok": True, "active": payload["active"], "reason": "fresh"}
    except Exception as e:
        return {"ok": False, "active": {}, "reason": f"catalyst cache read error: {e}"}


def blocking_catalyst_for(symbol: str, active: dict[str, dict]) -> dict | None:
    """Return the catalyst dict if `symbol` has an active NEGATIVE catalyst of a BLOCKING type
    within the age window; else None. Pure filter — no I/O."""
    c = active.get(symbol.upper())
    if not c or c.get("polarity") != "negative":
        return None
    ctype = c.get("type")
    if ctype not in _BLOCKING_TYPES:       # only the 4 high-severity 1b types block
        return None
    max_age = _BLOCK_MAX_AGE_BY_TYPE.get(ctype, _BLOCK_MAX_AGE_HOURS)
    ca = c.get("created_at")
    if not isinstance(ca, str) or not ca.strip():
        # No usable timestamp → cannot CONFIRM the catalyst is within the fresh window → do NOT
        # block. A data issue must never manufacture a trade decision (never-mask); the refresh
        # job + its cache-staleness monitor are what surface a data fault, not the entry gate.
        logger.debug("catalyst for %s has no usable created_at — cannot confirm freshness, not blocking", symbol)
        return None
    try:
        ts = datetime.fromisoformat(ca.replace("Z", "+00:00")).astimezone(timezone.utc)
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        if age_h > max_age:
            return None   # priced-in — no longer blocks (per-type window)
    except (ValueError, TypeError) as _age_e:
        logger.debug("catalyst age unparseable for %s (%s) — cannot confirm freshness, not blocking", symbol, _age_e)
        return None
    return c


def has_blocking_catalyst(symbol: str) -> bool:
    """True iff `symbol` has an active, in-window, high-severity NEGATIVE catalyst in a FRESH cache
    AND the gate is enabled. Fail-safe: gate off OR cache fault → False (never block on absence of
    data — the CALLER must alert on a cache fault). Read-only, fast (cache) — safe in the hot path."""
    if not CATALYST_GATE_ENABLED:
        return False
    st = read_cached_state()
    if not st["ok"]:
        return False   # never-mask: no data ≠ block; caller alerts on st["reason"]
    return blocking_catalyst_for(symbol, st["active"]) is not None


def _write_state(active: dict[str, dict]) -> None:
    """Atomic snapshot for the dashboard / future gate (RC-5 tmp→fsync→replace)."""
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated": datetime.now(PT).isoformat(),
                   "gate_enabled": CATALYST_GATE_ENABLED, "active": active}
        tmp = _STATE.with_suffix(f".json.{os.getpid()}.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(_STATE))
    except Exception as e:
        logger.warning("catalyst: state write failed: %s", e)


def _default_watch() -> list[str]:
    """Held positions + QHM/F6 universe + a small default set. Best-effort, read-only."""
    watch: set[str] = set(["SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "CRWD"])
    try:
        tl = json.loads((_ROOT / "trade_log.json").read_text())
        for t in tl.get("open", []) or []:
            _sym = t.get("symbol") if isinstance(t, dict) else None
            if _sym:
                watch.add(str(_sym).upper())
    except Exception:
        pass
    return sorted(watch)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    syms = [s.upper() for s in sys.argv[1:]] or _default_watch()
    active = get_active_catalysts(syms)
    _write_state(active)
    print("=" * 68)
    print(f"CATALYST ENGINE — per-name detector (DARK, gate_enabled={CATALYST_GATE_ENABLED})")
    print(f"watched: {len(syms)} names · lookback {_LOOKBACK_HOURS}h · Alpaca News (T1)")
    print("-" * 68)
    if not active:
        print("No stock-specific catalysts classified for the watch set.")
    else:
        for sym, c in sorted(active.items()):
            tag = "🔴 BLOCK" if c["polarity"] == "negative" else "🟢 allow"
            print(f"{tag} {sym:6} [{c['type']}] {c['headline'][:90]}")
    print("-" * 68)
    print("1a = detect+classify+log ONLY (no entry gating). 1b wires the entry-block behind a")
    print("board vote (news-display-only invariant reversal). Would-have-caught check: run this")
    print("against a date range covering RIVN's 2026-07-06 offering to confirm it classifies.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
