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
import logging
import urllib.request
import urllib.error
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

# ── DARK FLAG — 1a never gates. Flip to True only after 1b board vote + validation. ──
CATALYST_GATE_ENABLED = False

# Only consider catalysts within this recency window (dynamic: newest wins, not a static score).
_LOOKBACK_HOURS = 72

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
        # ATTRIBUTION: a stock-specific catalyst belongs to ONE company. Benzinga lists the
        # primary symbol first, and the subject is usually named in the headline. Attribute to
        # a watch-symbol NAMED IN THE HEADLINE if any, else the PRIMARY (first) watch-symbol —
        # so a "Rivian offering" article co-tagged [RIVN, TSLA] does not falsely flag TSLA.
        hl = it["headline"].upper()
        named = [s for s in syms if s in watch and s in hl]
        target = named[0] if named else next((s for s in syms if s in watch), None)
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


def has_blocking_catalyst(symbol: str, active: dict[str, dict] | None = None) -> bool:
    """True if `symbol` has an active NEGATIVE stock-specific catalyst. When the gate is DARK
    (CATALYST_GATE_ENABLED=False) this ALWAYS returns False so no entry is ever blocked in 1a."""
    if not CATALYST_GATE_ENABLED:
        return False
    a = active if active is not None else get_active_catalysts([symbol])
    c = a.get(symbol.upper())
    return bool(c and c.get("polarity") == "negative")


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
