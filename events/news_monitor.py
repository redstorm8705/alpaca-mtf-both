# ruff: noqa: E501
"""
events/news_monitor.py
Real-time news intelligence for the MTF bot.

14 data sources — rebuilt April 2026 after post-mortem showed bot was
completely blind to the Iran war + oil at $110 on April 2.

ARCHITECTURE: MARKET-REACTION-FIRST
  Keywords → MONITOR / CAUTION display only. Zero size impact.
  SPY 5-min bar-over-bar movement is the SOLE sizing trigger (main.py run_cycle).
  HALT reserved for true exchange emergencies only (circuit breaker, nuclear).

SOURCE ROSTER:
 1. Finnhub Economic Calendar  — CPI, PPI, NFP, FOMC live dates (FINNHUB_API_KEY)
 2. CNN Truth Social Archive   — Trump posts, 5-min refresh, no key required
 3. Finnhub Market News        — breaking news + sentiment (FINNHUB_API_KEY)
 4. GNews API                  — business headlines, 100/day free (GNEWS_API_KEY)
 5. Currents API               — financial breaking news, 1000/day (CURRENTS_API_KEY)
 6. Google News RSS (Reuters/AP) — broad market + geo, no key, 3-min poll
 7. War/Geo RSS                — Iran, Hormuz, airstrike, escalation, 2-min poll
 8. Oil/Energy RSS             — crude oil, WTI, Brent, energy prices, 5-min poll
 9. MarketWatch RSS            — top stories, 3-min poll, no key
10. CNBC RSS                   — breaking market news, 3-min poll, no key
11. Federal Reserve RSS        — official press releases, 5-min poll, always CAUTION+
12. BLS RSS                    — CPI/PPI/NFP at 8:30 AM ET, 5-min poll, always CAUTION+
13. SEC EDGAR 8-K              — material event filings, 10-min poll, no key
14. Marketaux API              — financial news + sentiment (MARKETAUX_API_KEY, 100/day)

Dedup: every headline/post is content-hashed before storing. A hash that has
already been seen is silently dropped — no re-fire on repeat poll of same story.

Sizing: get_news_size_multiplier() returns 1.0 always except HALT=0.0.
        All size reduction is performed by the hybrid SPY/QQQ engine in run_cycle().

How it plugs into the bot:
   - NewsMonitor.scan_breaking_news()    → call once per scan cycle
   - NewsMonitor.get_news_size_multiplier() → returns 1.0 or 0.0 (HALT only)
   - NewsMonitor.get_active_event_type() → GEO_CONFLICT | GEO_ENERGY | MACRO_MONETARY |
                                           MACRO_CREDIT | MACRO_FX | MACRO_SYSTEMIC |
                                           GLOBAL_ASIA | GLOBAL_EU | TECHNICAL
   - NewsMonitor.is_macro_risk_active()  → informational macro risk banner only
   - NewsMonitor.is_war_state_active()   → backward-compat alias for is_macro_risk_active()
   - NewsMonitor.refresh_calendar(EventCalendar) → auto-populates macro dates
   - NewsMonitor.get_summary()           → full 14-source status dict
"""

import os
import re
import json
import hashlib
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo
from events.calendar import EventCalendar, EventRisk, EventType

logger = logging.getLogger("news_monitor")
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# Disk path for seen-hash persistence — survives bot restarts / watchdog execv
_SEEN_HASHES_PATH = Path(__file__).parent.parent / "logs" / "news_seen_hashes.json"

# Disk path for macro risk window persistence — survives bot restarts (T3b)
_MACRO_RISK_STATE_PATH = Path(__file__).parent.parent / "data" / "state" / "hybrid_state.json"

# ── Price-confirmation threshold ──────────────────────────────────────────────
# SPY must have moved at least this % since the alert fired for full penalty
# to apply. Below this → softer multiplier (see get_news_size_multiplier).
PRICE_CONFIRM_THRESHOLD = 0.5   # 0.5% SPY move confirms the news risk

# ── Market-moving keyword sets ────────────────────────────────────────────────
# Tiered by expected market impact.
#
# HALT       — extreme; consider pausing entries entirely
# HIGH_RISK  — directly market-moving (trade/Fed/China/NK) → 0.5x size
# CAUTION    — worth noting; geopolitical risk but not always tradeable → 0.75x
# MONITOR    — log only; no size penalty; purely informational

# ── ARCHITECTURE: MARKET-REACTION-FIRST ──────────────────────────────────────
#
# As of April 2026 post-mortem: keyword-based sizing is REMOVED.
#
# Previous architecture inverted causality — it read a headline, assigned a
# risk tier, and reduced position size before knowing whether the market cared.
# During an active war, HIGH_RISK keywords ("iran", "airstrike", "hormuz")
# appear dozens of times per day. Most do nothing to SPY. The ones that do?
# The market tells you immediately via price action.
#
# New architecture:
#   news_monitor = passive surveillance only (MONITOR or CAUTION for display)
#   SPY 5-min bar movement = the SOLE sizing trigger (see main.py run_cycle)
#   HALT = reserved for true exchange-level emergencies only
#
# CAUTION tier: notable items surfaced in dashboard — NOT a size multiplier.
# MONITOR tier: default for all news. Logged, displayed, never gates.
# ─────────────────────────────────────────────────────────────────────────────

KEYWORDS_HALT = {
    # True exchange / systemic emergencies — these and only these pause entries
    "national emergency", "market circuit breaker", "trading halt",
    "federal reserve emergency", "systemic risk",
    "nuclear strike", "nuclear attack", "world war iii",
}

# CAUTION: notable headlines — displayed prominently in dashboard, zero size impact
KEYWORDS_CAUTION = {
    # War / geopolitical — important context, market reaction is what matters
    "iran", "airstrike", "air strike", "bombing", "hormuz",
    "strait of hormuz", "escalation", "retaliation", "invasion",
    "russia", "ukraine", "nato", "north korea",
    # Oil / energy — watch for SPY confirmation
    "crude oil", "oil shock", "oil prices surge", "oil prices fall",
    "wti", "brent crude", "refinery attack", "pipeline attack",
    # Fed / policy
    "federal reserve", "fomc", "rate cut", "rate hike", "powell",
    "interest rate", "basis points",
    # Trade / macro
    "tariff", "tariffs", "trade war", "sanctions", "executive order",
    "congress", "senate",
    # Market events
    "crash", "collapse", "recession", "bankruptcy",
    "inflation", "deflation",
    # Economic data
    "nonfarm payrolls", "cpi", "ppi", "gdp", "unemployment",
}

# MONITOR: everything else — logged, zero display prominence, never gates
KEYWORDS_MONITOR = {
    "military", "troops", "warship", "drone", "missile",
    "election", "vote", "investigation", "indictment",
    "negotiation", "ceasefire", "peace talks", "summit",
    "jobs", "payrolls", "earnings", "guidance",
}

# ── Macro Risk State tracking — informational only, displayed in dashboard ────
# Replaces binary "war state" (Apr 6 2026 audit). No longer gates sizing directly.
# The MacroRiskIndex (events/macro_risk_index.py) owns all sizing decisions.
# This module only counts CAUTION alerts per domain to classify active event type.
#
# Keyword sets map headlines to the 9-type MRI event taxonomy.
# Finding 7 fix: bare "war" removed — too broad, matched "trade war"/"price war".

# GEO_CONFLICT: armed conflict, military strikes, territory, terror, cyber-infra
GEO_CONFLICT_KEYWORDS = {
    "iran", "airstrike", "air strike", "bombing", "hormuz",
    "strait of hormuz", "retaliation", "invasion", "warplane",
    "naval blockade", "military strike", "armed conflict",
    "declared war", "wartime", "terror attack", "terrorist attack",
    "cyber attack", "infrastructure attack", "north korea", "ukraine",
    "russia", "nato",
}

# GEO_ENERGY: supply disruption, oil shock, energy crisis
GEO_ENERGY_KEYWORDS = {
    "oil shock", "crude oil surges", "crude oil jumps", "refinery attack",
    "pipeline attack", "oil supply", "opec cut", "hormuz blockade",
    "energy crisis", "oil embargo",
}

# MACRO_MONETARY: Fed, central bank, rate decisions
MACRO_MONETARY_KEYWORDS = {
    "federal reserve", "fomc", "rate cut", "rate hike", "powell",
    "interest rate", "basis points", "quantitative easing", "quantitative tightening",
    "bank of japan", "boj", "ecb rate", "central bank",
}

# MACRO_CREDIT: credit market stress, bank stress, private credit
MACRO_CREDIT_KEYWORDS = {
    "credit spread", "high yield", "junk bond", "bank failure", "svb",
    "private credit", "leverage loan", "default", "bankruptcy",
    "credit crunch", "bank stress", "financial contagion",
}

# MACRO_FX: currency dislocation
MACRO_FX_KEYWORDS = {
    "yen", "dollar surges", "dollar collapse", "currency crisis",
    "dxy", "yuan devaluation", "carry trade", "fx dislocation",
    "dollar index", "currency war",
}

# MACRO_SYSTEMIC: pandemic, exchange halt, systemic failure
MACRO_SYSTEMIC_KEYWORDS = {
    "pandemic", "circuit breaker", "trading halt", "market halt",
    "exchange closed", "systemic risk", "bank run", "financial system",
    "national emergency",
}

# Combined set for macro risk window counting (any domain)
MACRO_RISK_KEYWORDS = (
    GEO_CONFLICT_KEYWORDS | GEO_ENERGY_KEYWORDS | MACRO_MONETARY_KEYWORDS |
    MACRO_CREDIT_KEYWORDS | MACRO_FX_KEYWORDS | MACRO_SYSTEMIC_KEYWORDS
)

MACRO_RISK_THRESHOLD = 3   # 3+ CAUTION macro alerts in 24h → macro risk state active

# ── Finnhub event type mapping ────────────────────────────────────────────────
FINNHUB_EVENT_MAP = {
    "CPI":                  (EventType.CPI,        EventRisk.HIGH_RISK),
    "Consumer Price Index": (EventType.CPI,        EventRisk.HIGH_RISK),
    "PPI":                  (EventType.PPI,        EventRisk.HIGH_RISK),
    "Producer Price Index": (EventType.PPI,        EventRisk.HIGH_RISK),
    "Nonfarm Payrolls":     (EventType.JOBS_REPORT, EventRisk.HIGH_RISK),
    "Non Farm Payrolls":    (EventType.JOBS_REPORT, EventRisk.HIGH_RISK),
    "NFP":                  (EventType.JOBS_REPORT, EventRisk.HIGH_RISK),
    "FOMC":                 (EventType.FED_MEETING, EventRisk.HIGH_RISK),
    "Fed Rate Decision":    (EventType.FED_MEETING, EventRisk.HIGH_RISK),
    "Federal Reserve":      (EventType.FED_MEETING, EventRisk.HIGH_RISK),
    "GDP":                  (EventType.GDP,         EventRisk.HIGH_RISK),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _content_hash(text: str) -> str:
    """Return a short SHA-256 hex digest of the normalised text."""
    normalised = re.sub(r"\s+", " ", text.lower().strip())
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


class BreakingNewsAlert:
    """Represents a detected breaking news event."""
    def __init__(self, source: str, headline: str, keywords: list,
                 risk_level: str, size_mult: float, timestamp: datetime):
        self.source     = source
        self.headline   = headline[:120]
        self.keywords   = keywords
        self.risk_level = risk_level
        self.size_mult  = size_mult
        self.timestamp  = timestamp

    def __str__(self):
        age_mins = int((datetime.now(ET) - self.timestamp).total_seconds() / 60)
        return (f"[{self.source}] {self.risk_level} | {self.headline} "
                f"| keywords: {self.keywords} | {age_mins}m ago "
                f"| base penalty: {self.size_mult:.0%} (price-confirmation applies before trade)")


class NewsMonitor:
    """
    Monitors six free data sources for market-moving news.
    Designed to be initialized once at bot startup and called each scan cycle.
    """

    def __init__(self):
        self._finnhub_key      = os.getenv("FINNHUB_API_KEY", "")
        self._gnews_key        = os.getenv("GNEWS_API_KEY", "")
        self._currents_key     = os.getenv("CURRENTS_API_KEY", "")
        self._marketaux_key    = os.getenv("MARKETAUX_API_KEY", "")

        # C1: content-hash dedup — maps hash → expiry datetime (24h window)
        # Replaces single _last_truth_id; works across ALL sources.
        # Persisted to logs/news_seen_hashes.json so bot restarts don't re-fire
        # old headlines and falsely trigger MACRO_RISK_ACTIVE state.
        self._hash_ttl_hours = 24
        self._seen_hashes: dict[str, datetime] = self._load_seen_hashes()
        self._last_hash_save: float = 0.0   # unix timestamp; throttles disk writes to 60s
        self._scan_lock = threading.Lock()  # P5-L3: protects _seen_hashes check-then-set across concurrent source threads

        self._last_truth_check      = None
        self._last_news_check       = None
        self._last_gnews_check      = None
        self._last_currents_check   = None
        self._last_rss_check        = None
        self._last_war_rss_check    = None   # war/geo RSS — 2-min poll
        self._last_oil_rss_check    = None   # oil/energy RSS — 5-min poll
        self._last_marketwatch_check = None  # MarketWatch RSS — 3-min poll
        self._last_cnbc_check       = None   # CNBC RSS — 3-min poll
        self._last_fed_check        = None   # Federal Reserve RSS — 5-min poll
        self._last_bls_check        = None   # BLS official releases — 5-min poll
        self._last_sec_check        = None   # SEC EDGAR 8-K filings — 10-min poll
        self._last_marketaux_check  = None   # Marketaux API — 5-min poll

        self._active_alerts    = []        # list of BreakingNewsAlert
        self._alert_duration_mins = 30     # how long an alert stays active

        # ── Macro risk state tracking ─────────────────────────────────────────
        # Replaces _war_state_active (Apr 6 2026 audit — Finding 8: renamed,
        # persisted via MacroRiskIndex).
        # Accumulates timestamps of CAUTION macro-keyword alerts over 24 hours.
        # When MACRO_RISK_THRESHOLD is reached, _macro_risk_active is set True.
        # Sizing decisions are owned by MacroRiskIndex; this flag is informational
        # and used by get_active_event_type() as a classification backstop.
        self._macro_risk_active: bool                = False
        # Dict[content_hash → timestamp] — hash-keyed so the same headline
        # can only count once in the 24h window even across bot restarts.
        # (List-based approach allowed a single repeated headline to inflate
        # the count if _seen_hashes was wiped by a restart.)
        self._macro_risk_window: dict[str, datetime] = {}
        self._restore_macro_risk_window()   # T3b: restore 24h window from disk on restart

        self._truth_url = "https://ix.cnn.io/data/truth-social/truth_archive.json"
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="news")
        self._alerts_lock = threading.Lock()

        if not self._finnhub_key:
            logger.warning(
                "FINNHUB_API_KEY not set — economic calendar / Finnhub news disabled. "
                "Free key at: https://finnhub.io/register"
            )
        if not self._gnews_key:
            logger.debug("GNEWS_API_KEY not set — GNews source disabled. "
                         "Free key at: https://gnews.io/")
        if not self._currents_key:
            logger.debug("CURRENTS_API_KEY not set — Currents source disabled. "
                         "Free key at: https://currentsapi.services/")

    # ── Dedup helpers ─────────────────────────────────────────────────────────

    def _load_seen_hashes(self) -> dict[str, datetime]:
        """Load persisted seen-hashes from disk, discarding already-expired entries."""
        try:
            if not _SEEN_HASHES_PATH.exists():
                return {}
            raw = json.loads(_SEEN_HASHES_PATH.read_text())
            now = datetime.now(ET)
            restored = {}
            for h, exp_str in raw.items():
                try:
                    exp = datetime.fromisoformat(exp_str)
                    # Make timezone-aware if stored as naive (legacy entries)
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=ET)
                    if exp > now:
                        restored[h] = exp
                except Exception:
                    pass
            logger.info(f"news_monitor: restored {len(restored)} seen-hashes from disk "
                        f"(discarded {len(raw) - len(restored)} expired)")
            return restored
        except Exception as _lse:
            logger.debug(f"news_monitor: seen-hash load failed ({_lse}) — starting fresh")
            return {}

    def _save_seen_hashes(self, force: bool = False) -> None:
        """Persist seen-hashes to disk. Throttled to once per 60s unless force=True."""
        import time
        now_ts = time.monotonic()
        if not force and (now_ts - self._last_hash_save) < 60:
            return
        try:
            _SEEN_HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {h: exp.isoformat() for h, exp in self._seen_hashes.items()}
            # NM-4: atomic write — prevents JSON corruption if bot crashes mid-write
            _tmp = _SEEN_HASHES_PATH.with_suffix(".json.tmp")
            _tmp.write_text(json.dumps(payload))
            _tmp.replace(_SEEN_HASHES_PATH)
            self._last_hash_save = now_ts
        except Exception as _sse:
            logger.debug(f"news_monitor: seen-hash save failed: {_sse}")

    def _persist_macro_risk_window(self) -> None:
        """Atomically write macro risk window to data/state/hybrid_state.json. Never raises."""
        try:
            _MACRO_RISK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "window":   {k: v.isoformat() for k, v in self._macro_risk_window.items()},
                "active":   self._macro_risk_active,
                "saved_at": datetime.now(ET).isoformat(),
            }
            tmp = _MACRO_RISK_STATE_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(_MACRO_RISK_STATE_PATH)
        except Exception as e:
            logger.debug(f"Macro risk window persist failed (non-critical): {e}")

    def _restore_macro_risk_window(self) -> None:
        """
        Restore _macro_risk_window from data/state/hybrid_state.json on startup.
        Drops any entries older than 24h — never restores stale window state.
        """
        try:
            if not _MACRO_RISK_STATE_PATH.exists():
                return
            payload = json.loads(_MACRO_RISK_STATE_PATH.read_text())
            now = datetime.now(ET)
            cutoff = now - timedelta(hours=24)
            window = {}
            for h, ts_str in payload.get("window", {}).items():
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=ET)
                if ts > cutoff:
                    window[h] = ts
            self._macro_risk_window = window
            self._macro_risk_active = len(window) >= MACRO_RISK_THRESHOLD
            if window:
                logger.info(
                    f"Macro risk window restored: {len(window)} alert(s) in last 24h "
                    f"(active={self._macro_risk_active})"
                )
        except Exception as e:
            logger.debug(f"Macro risk window restore failed — starting fresh: {e}")

    def _is_seen(self, text: str) -> bool:
        """Return True if this content has already been processed."""
        with self._scan_lock:
            h = _content_hash(text)
            self._purge_expired_hashes()
            return h in self._seen_hashes

    def _mark_seen(self, text: str) -> None:
        """Record content as seen for _hash_ttl_hours and persist to disk (throttled)."""
        with self._scan_lock:
            h = _content_hash(text)
            self._seen_hashes[h] = datetime.now(ET) + timedelta(hours=self._hash_ttl_hours)
        self._save_seen_hashes()

    def _purge_expired_hashes(self) -> None:
        """Remove expired entries to keep the dict from growing unbounded."""
        try:
            now = datetime.now(ET)
            before = len(self._seen_hashes)
            self._seen_hashes = {h: exp for h, exp in self._seen_hashes.items() if exp > now}
            if len(self._seen_hashes) > 10_000:
                _sorted = sorted(self._seen_hashes.items(), key=lambda x: x[1])
                self._seen_hashes = dict(_sorted[5_000:])
                logger.critical(
                    f"news_monitor: _seen_hashes capped — evicted oldest, retained {len(self._seen_hashes)}"
                )
            if len(self._seen_hashes) < before:
                self._save_seen_hashes()
        except Exception as _e:
            logger.error(f"news_monitor: _purge_expired_hashes failed — {_e}")

    # ── Keyword classifier ────────────────────────────────────────────────────

    def _classify(self, text: str) -> tuple[Optional[str], list, float]:
        """
        Returns (risk_level, matched_keywords, size_multiplier).

        Post April-2 rebuild: sizing is REMOVED from keyword classification.
        All multipliers are 1.0. The SPY 5-min market reaction engine in
        main.py run_cycle() is the sole source of position sizing changes.

        risk_level: "HALT" | "CAUTION" | "MONITOR" | None
          HALT    → 0.0  (true exchange emergency only — circuit breaker etc.)
          CAUTION → 1.0  (notable headline — displayed in dashboard, no size impact)
          MONITOR → 1.0  (logged only)
        """
        lower = text.lower()
        found_halt    = [k for k in KEYWORDS_HALT    if k in lower]
        found_caution = [k for k in KEYWORDS_CAUTION if k in lower]
        found_monitor = [k for k in KEYWORDS_MONITOR if k in lower]

        if found_halt:
            return "HALT",    found_halt[:3],    0.0
        if found_caution:
            return "CAUTION", found_caution[:3], 1.0   # display only — no size penalty
        if found_monitor:
            return "MONITOR", found_monitor[:3], 1.0   # log only
        return None, [], 1.0

    # ── 1. FINNHUB ECONOMIC CALENDAR ─────────────────────────────────────────

    def refresh_calendar(self, calendar: EventCalendar, days_ahead: int = 30) -> int:
        """
        Fetch upcoming high-impact economic events from Finnhub
        and add them to the EventCalendar.
        Returns number of new events added.
        """
        if not self._finnhub_key:
            return 0

        try:
            today    = date.today()
            end_date = today + timedelta(days=days_ahead)
            url = (
                f"https://finnhub.io/api/v1/calendar/economic"
                f"?from={today.isoformat()}&to={end_date.isoformat()}"
                f"&token={self._finnhub_key}"
            )
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                if resp.status_code == 403:
                    logger.debug("Finnhub calendar: paid endpoint (403) — static calendar active")
                    return -1
                else:
                    logger.warning(f"Finnhub calendar: HTTP {resp.status_code}")
                return 0

            data   = resp.json()
            events = data.get("economicCalendar", [])
            added  = 0

            for event in events:
                impact = event.get("impact", "").lower()
                if impact not in ("high", "medium"):
                    continue

                event_name = event.get("event", "")
                event_date = event.get("time", "")[:10]
                country    = event.get("country", "")
                if country != "US":
                    continue

                matched = None
                for keyword, (etype, erisk) in FINNHUB_EVENT_MAP.items():
                    if keyword.lower() in event_name.lower():
                        matched = (etype, erisk)
                        break
                if matched is None:
                    matched = (EventType.OTHER,
                               EventRisk.HIGH_RISK if impact == "high" else EventRisk.CAUTION)

                etype, erisk = matched
                actual   = event.get("actual")
                estimate = event.get("estimate")

                note = event_name
                if actual is not None and estimate is not None:
                    note += f" — Actual: {actual} vs Est: {estimate}"
                    if etype in (EventType.CPI, EventType.PPI):
                        try:
                            if float(str(actual).replace("%","")) > float(str(estimate).replace("%","")):
                                erisk = EventRisk.HIGH_RISK
                                note += " ⚠ HOT PRINT"
                        except (ValueError, TypeError):
                            pass

                calendar.add_event_dynamic(event_date, etype, erisk, note)
                added += 1

            logger.info(f"Finnhub calendar: added {added} events for next {days_ahead} days")
            return added

        except Exception as e:
            if "403" in str(e) or "access" in str(e).lower():
                logger.debug("Finnhub calendar: paid endpoint — using hardcoded calendar")
            else:
                logger.warning(f"Finnhub calendar refresh failed: {e}")
            return 0

    # ── 2. CNN TRUTH SOCIAL ARCHIVE ───────────────────────────────────────────

    def check_truth_social(self) -> list:
        """
        Fetch Trump's latest Truth Social posts from CNN's free archive.
        Returns list of new BreakingNewsAlerts not seen before (content-hash dedup).
        """
        try:
            resp = requests.get(self._truth_url, timeout=10,
                                headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                logger.debug(f"Truth Social archive: HTTP {resp.status_code}")
                return []

            posts = resp.json()
            if not posts:
                return []

            posts.sort(key=lambda p: p.get("created_at", ""), reverse=True)
            new_alerts = []

            for post in posts[:10]:
                content = post.get("content", "")
                created = post.get("created_at", "")
                clean   = re.sub(r"<[^>]+>", " ", content).lower().strip()
                if not clean:
                    continue

                # C1: content-hash dedup
                if self._is_seen(clean):
                    continue

                # Age check — only care about posts in the last 6 hours
                try:
                    post_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    post_time = post_time.astimezone(ET)
                    age_hours = (datetime.now(ET) - post_time).total_seconds() / 3600
                    if age_hours > 6:
                        continue
                except Exception:
                    continue

                risk_level, keywords, size_mult = self._classify(clean)
                self._mark_seen(clean)

                if risk_level is None:
                    continue   # no keywords matched

                if risk_level == "MONITOR":
                    logger.info(f"[TruthSocial] MONITOR: {clean[:100]} | keywords: {keywords}")
                    continue   # log only — no alert added

                new_alerts.append(BreakingNewsAlert(
                    source="TruthSocial", headline=clean[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=post_time
                ))

            self._last_truth_check = datetime.now(ET)

            if new_alerts:
                logger.warning(
                    f"⚡ TRUTH SOCIAL ALERT: {len(new_alerts)} market-relevant post(s) detected"
                )
                for alert in new_alerts:
                    logger.warning(f"   {alert}")

            return new_alerts

        except Exception as e:
            logger.debug(f"Truth Social check failed: {e}")
            return []

    # ── 3. FINNHUB MARKET NEWS + SENTIMENT ───────────────────────────────────

    def get_finnhub_news_sentiment(self) -> list:
        """
        Fetch latest general market news from Finnhub with sentiment.
        Returns list of article dicts from the last 2 hours.
        """
        if not self._finnhub_key:
            return []
        try:
            url  = f"https://finnhub.io/api/v1/news?category=general&token={self._finnhub_key}"
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                return []

            articles = resp.json()
            cutoff   = datetime.now(ET).timestamp() - 7200
            results  = []

            for article in articles[:20]:
                if article.get("datetime", 0) < cutoff:
                    continue
                results.append({
                    "headline":  article.get("headline", ""),
                    "sentiment": article.get("sentiment", 0) or 0,
                    "source":    article.get("source", ""),
                    "url":       article.get("url", ""),
                    "timestamp": article.get("datetime", 0),
                })
            return results

        except Exception as e:
            logger.debug(f"Finnhub news fetch failed: {e}")
            return []

    # ── 4. GNEWS API ──────────────────────────────────────────────────────────

    def check_gnews_breaking(self) -> list:
        """
        Fetch top business headlines from GNews API (100 calls/day free).
        Requires GNEWS_API_KEY env var.
        Returns list of new BreakingNewsAlerts.
        """
        if not self._gnews_key:
            return []
        try:
            url = (
                f"https://gnews.io/api/v1/top-headlines"
                f"?topic=business&lang=en&max=10&token={self._gnews_key}"
            )
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                logger.debug(f"GNews: HTTP {resp.status_code}")
                return []

            articles = resp.json().get("articles", [])
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 7200   # 2 hours

            for article in articles:
                headline = article.get("title", "") or ""
                desc     = article.get("description", "") or ""
                text     = f"{headline} {desc}"
                if not text.strip():
                    continue

                # Age check
                pub = article.get("publishedAt", "")
                try:
                    pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(ET)
                    if pub_dt.timestamp() < cutoff_ts:
                        continue
                except Exception:
                    pub_dt = datetime.now(ET)

                # C1: content-hash dedup
                if self._is_seen(text):
                    continue

                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)

                if risk_level is None:
                    continue
                if risk_level == "MONITOR":
                    logger.info(f"[GNews] MONITOR: {headline[:100]} | keywords: {keywords}")
                    continue

                new_alerts.append(BreakingNewsAlert(
                    source="GNews", headline=headline[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_gnews_check = datetime.now(ET)

            if new_alerts:
                logger.warning(f"⚡ GNEWS ALERT: {len(new_alerts)} market-relevant article(s)")
                for alert in new_alerts:
                    logger.warning(f"   {alert}")

            return new_alerts

        except Exception as e:
            logger.debug(f"GNews check failed: {e}")
            return []

    # ── 5. CURRENTS API ───────────────────────────────────────────────────────

    def check_currents_macro(self) -> list:
        """
        Fetch latest macro/financial news from Currents API (1,000 calls/day free).
        Requires CURRENTS_API_KEY env var.
        Returns list of new BreakingNewsAlerts.
        """
        if not self._currents_key:
            return []
        try:
            url = (
                f"https://api.currentsapi.services/v1/latest-news"
                f"?language=en&category=finance,business"
                f"&apiKey={self._currents_key}"
            )
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                logger.debug(f"Currents API: HTTP {resp.status_code}")
                return []

            news     = resp.json().get("news", [])
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 7200

            for item in news[:15]:
                headline = item.get("title", "") or ""
                desc     = item.get("description", "") or ""
                text     = f"{headline} {desc}"
                if not text.strip():
                    continue

                pub = item.get("published", "")
                try:
                    pub_dt = datetime.fromisoformat(pub).astimezone(ET)
                    if pub_dt.timestamp() < cutoff_ts:
                        continue
                except Exception:
                    pub_dt = datetime.now(ET)

                if self._is_seen(text):
                    continue

                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)

                if risk_level is None:
                    continue
                if risk_level == "MONITOR":
                    logger.info(f"[Currents] MONITOR: {headline[:100]} | keywords: {keywords}")
                    continue

                new_alerts.append(BreakingNewsAlert(
                    source="Currents", headline=headline[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_currents_check = datetime.now(ET)

            if new_alerts:
                logger.warning(f"⚡ CURRENTS ALERT: {len(new_alerts)} article(s) detected")
                for alert in new_alerts:
                    logger.warning(f"   {alert}")

            return new_alerts

        except Exception as e:
            logger.debug(f"Currents API check failed: {e}")
            return []

    # ── 6. GOOGLE NEWS RSS (Reuters / AP) ────────────────────────────────────

    def check_reuters_ap_rss(self) -> list:
        """
        Scrape Reuters/AP breaking news from Google News RSS feed via feedparser.
        No API key required — completely free.
        Requires: pip install feedparser
        Returns list of new BreakingNewsAlerts.
        """
        try:
            import feedparser
        except ImportError:
            logger.debug("feedparser not installed — skipping Reuters/AP RSS. "
                         "Run: pip install feedparser")
            return []

        try:
            # Google News RSS: Reuters OR AP — broad financial + macro coverage
            # Updated to include war/geo terms after April 2 post-mortem showed
            # the bot was completely blind to the Iran war driving a 600-pt Dow drop.
            rss_url = (
                "https://news.google.com/rss/search"
                "?q=Reuters+OR+%22Associated+Press%22+"
                "market+OR+economy+OR+%22federal+reserve%22+"
                "OR+Iran+OR+war+OR+oil+OR+Hormuz"
                "&hl=en-US&gl=US&ceid=US:en"
            )
            _rss_resp = requests.get(rss_url, timeout=10)
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            if not feed.entries:
                return []

            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 7200

            for entry in feed.entries[:15]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"{title} {summary}"
                if not text.strip():
                    continue

                # Age check via published_parsed
                pub_dt = None
                try:
                    import time as _time
                    if entry.get("published_parsed"):
                        ts = _time.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                if pub_dt is None:
                    pub_dt = datetime.now(ET)

                if self._is_seen(text):
                    continue

                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)

                if risk_level is None:
                    continue
                if risk_level == "MONITOR":
                    logger.info(f"[Reuters/AP RSS] MONITOR: {title[:100]} | keywords: {keywords}")
                    continue

                new_alerts.append(BreakingNewsAlert(
                    source="Reuters/AP", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_rss_check = datetime.now(ET)

            if new_alerts:
                logger.warning(f"⚡ REUTERS/AP ALERT: {len(new_alerts)} article(s) detected")
                for alert in new_alerts:
                    logger.warning(f"   {alert}")

            return new_alerts

        except Exception as e:
            logger.debug(f"Reuters/AP RSS check failed: {e}")
            return []

    # ── 7. WAR / GEOPOLITICAL RSS ────────────────────────────────────────────

    def check_war_geopolitical_rss(self) -> list:
        """
        Dedicated Google News RSS feed for war/geopolitical breaking news.
        Queries Iran, Hormuz, airstrike, oil shock — the exact content that was
        invisible to the bot on April 2 while driving a 600-pt Dow selloff.
        Polled every 2 minutes (no rate limit). Requires feedparser.
        """
        try:
            import feedparser
        except ImportError:
            logger.debug("feedparser not installed — war/geo RSS disabled. Run: pip install feedparser")
            return []
        try:
            rss_url = (
                "https://news.google.com/rss/search"
                "?q=Iran+OR+%22Strait+of+Hormuz%22+OR+airstrike+OR+%22oil+shock%22"
                "+OR+%22military+strike%22+OR+escalation+OR+retaliation"
                "&hl=en-US&gl=US&ceid=US:en"
            )
            _rss_resp = requests.get(rss_url, timeout=10)
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            if not feed.entries:
                return []

            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 7200  # 2-hour window

            for entry in feed.entries[:15]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"{title} {summary}"
                if not text.strip():
                    continue

                pub_dt = None
                try:
                    import time as _time
                    if entry.get("published_parsed"):
                        ts = _time.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                if pub_dt is None:
                    pub_dt = datetime.now(ET)

                if self._is_seen(text):
                    continue

                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)

                if risk_level is None:
                    continue
                if risk_level == "MONITOR":
                    logger.info(f"[War/Geo RSS] MONITOR: {title[:100]} | keywords: {keywords}")
                    continue

                new_alerts.append(BreakingNewsAlert(
                    source="War/Geo RSS", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_war_rss_check = datetime.now(ET)

            if new_alerts:
                logger.warning(f"⚡ WAR/GEO RSS ALERT: {len(new_alerts)} article(s) detected")
                for alert in new_alerts:
                    logger.warning(f"   {alert}")

            return new_alerts

        except Exception as e:
            logger.debug(f"War/Geo RSS check failed: {e}")
            return []

    # ── 8. OIL / ENERGY RSS ──────────────────────────────────────────────────

    def check_oil_energy_rss(self) -> list:
        """
        Dedicated Google News RSS for crude oil and energy price news.
        Oil at $110 on April 2 was completely invisible to the bot.
        Polled every 5 minutes (no rate limit). Requires feedparser.
        """
        try:
            import feedparser
        except ImportError:
            return []
        try:
            rss_url = (
                "https://news.google.com/rss/search"
                "?q=%22crude+oil%22+OR+%22WTI+crude%22+OR+%22Brent+crude%22"
                "+OR+%22oil+prices%22+OR+%22oil+jumps%22+OR+%22oil+surges%22"
                "+OR+%22oil+plunges%22+OR+%22energy+prices%22"
                "&hl=en-US&gl=US&ceid=US:en"
            )
            _rss_resp = requests.get(rss_url, timeout=10)
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            if not feed.entries:
                return []

            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 7200

            for entry in feed.entries[:10]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"{title} {summary}"
                if not text.strip():
                    continue

                pub_dt = None
                try:
                    import time as _time
                    if entry.get("published_parsed"):
                        ts = _time.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                if pub_dt is None:
                    pub_dt = datetime.now(ET)

                if self._is_seen(text):
                    continue

                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)

                if risk_level is None:
                    continue
                if risk_level == "MONITOR":
                    logger.info(f"[Oil/Energy RSS] MONITOR: {title[:100]} | keywords: {keywords}")
                    continue

                new_alerts.append(BreakingNewsAlert(
                    source="Oil/Energy RSS", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_oil_rss_check = datetime.now(ET)

            if new_alerts:
                logger.warning(f"⚡ OIL/ENERGY RSS ALERT: {len(new_alerts)} article(s) detected")
                for alert in new_alerts:
                    logger.warning(f"   {alert}")

            return new_alerts

        except Exception as e:
            logger.debug(f"Oil/Energy RSS check failed: {e}")
            return []

    # ── 9. MARKETWATCH RSS ───────────────────────────────────────────────────

    def check_marketwatch_rss(self) -> list:
        """
        MarketWatch breaking news RSS — high-quality, real-time financial wire.
        Covers Fed, earnings surprises, market events, economic data.
        No API key. No rate limit. Polled every 3 minutes.
        """
        try:
            import feedparser
        except ImportError:
            return []
        try:
            _rss_resp = requests.get(
                "http://feeds.marketwatch.com/marketwatch/topstories/",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 3600   # 1-hour window

            for entry in (feed.entries or [])[:20]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"{title} {summary}"
                if not text.strip():
                    continue
                pub_dt = None
                try:
                    import time as _t
                    if entry.get("published_parsed"):
                        ts = _t.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                pub_dt = pub_dt or datetime.now(ET)
                if self._is_seen(text):
                    continue
                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)
                if risk_level is None or risk_level == "MONITOR":
                    if risk_level == "MONITOR":
                        logger.debug(f"[MarketWatch] MONITOR: {title[:80]}")
                    continue
                new_alerts.append(BreakingNewsAlert(
                    source="MarketWatch", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_marketwatch_check = datetime.now(ET)
            if new_alerts:
                logger.warning(f"⚡ MARKETWATCH: {len(new_alerts)} notable item(s)")
                for a in new_alerts:
                    logger.warning(f"   {a}")
            return new_alerts
        except Exception as e:
            logger.debug(f"MarketWatch RSS failed: {e}")
            return []

    # ── 10. CNBC RSS ─────────────────────────────────────────────────────────

    def check_cnbc_rss(self) -> list:
        """
        CNBC top news RSS — wire-speed market news, Fed, macro, earnings.
        No API key. No rate limit. Polled every 3 minutes.
        """
        try:
            import feedparser
        except ImportError:
            return []
        try:
            _rss_resp = requests.get(
                "https://www.cnbc.com/id/100003114/device/rss/rss.html",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 3600

            for entry in (feed.entries or [])[:20]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"{title} {summary}"
                if not text.strip():
                    continue
                pub_dt = None
                try:
                    import time as _t
                    if entry.get("published_parsed"):
                        ts = _t.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                pub_dt = pub_dt or datetime.now(ET)
                if self._is_seen(text):
                    continue
                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)
                if risk_level is None or risk_level == "MONITOR":
                    continue
                new_alerts.append(BreakingNewsAlert(
                    source="CNBC", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_cnbc_check = datetime.now(ET)
            if new_alerts:
                logger.warning(f"⚡ CNBC: {len(new_alerts)} notable item(s)")
                for a in new_alerts:
                    logger.warning(f"   {a}")
            return new_alerts
        except Exception as e:
            logger.debug(f"CNBC RSS failed: {e}")
            return []

    # ── 11. FEDERAL RESERVE RSS ───────────────────────────────────────────────

    def check_fed_rss(self) -> list:
        """
        Official Federal Reserve press release RSS.
        Catches FOMC statements, rate decisions, Fed speeches at source.
        No API key. No rate limit. Polled every 5 minutes.
        This should have been in from day one.
        """
        try:
            import feedparser
        except ImportError:
            return []
        try:
            _rss_resp = requests.get(
                "https://www.federalreserve.gov/feeds/press_all.xml",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 7200   # 2-hour window

            for entry in (feed.entries or [])[:10]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"federal reserve {title} {summary}"   # prepend to always hit Fed keywords
                if not text.strip():
                    continue
                pub_dt = None
                try:
                    import time as _t
                    if entry.get("published_parsed"):
                        ts = _t.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                pub_dt = pub_dt or datetime.now(ET)
                if self._is_seen(text):
                    continue
                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)
                if risk_level is None:
                    continue
                # Fed RSS is always at least CAUTION — it's an official statement
                if risk_level == "MONITOR":
                    risk_level = "CAUTION"
                new_alerts.append(BreakingNewsAlert(
                    source="FederalReserve", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_fed_check = datetime.now(ET)
            if new_alerts:
                logger.warning(f"⚡ FEDERAL RESERVE: {len(new_alerts)} official release(s)")
                for a in new_alerts:
                    logger.warning(f"   {a}")
            return new_alerts
        except Exception as e:
            logger.debug(f"Federal Reserve RSS failed: {e}")
            return []

    # ── 12. BLS (Bureau of Labor Statistics) RSS ─────────────────────────────

    def check_bls_rss(self) -> list:
        """
        Official BLS economic data release RSS.
        Catches CPI, PPI, NFP, unemployment releases at source — 8:30 AM ET.
        No API key. No rate limit. Polled every 5 minutes.
        """
        try:
            import feedparser
        except ImportError:
            return []
        try:
            _rss_resp = requests.get(
                "https://www.bls.gov/feed/bls_release.rss",
                timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 7200

            for entry in (feed.entries or [])[:10]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"{title} {summary}"
                if not text.strip():
                    continue
                pub_dt = None
                try:
                    import time as _t
                    if entry.get("published_parsed"):
                        ts = _t.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                pub_dt = pub_dt or datetime.now(ET)
                if self._is_seen(text):
                    continue
                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)
                # BLS releases are always at least CAUTION — official economic data
                if risk_level is None or risk_level == "MONITOR":
                    risk_level, keywords, size_mult = "CAUTION", ["bls", "economic data"], 1.0
                new_alerts.append(BreakingNewsAlert(
                    source="BLS", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_bls_check = datetime.now(ET)
            if new_alerts:
                logger.warning(f"⚡ BLS DATA RELEASE: {len(new_alerts)} release(s)")
                for a in new_alerts:
                    logger.warning(f"   {a}")
            return new_alerts
        except Exception as e:
            logger.debug(f"BLS RSS failed: {e}")
            return []

    # ── 13. SEC EDGAR 8-K RSS ─────────────────────────────────────────────────

    def check_sec_edgar_rss(self) -> list:
        """
        SEC EDGAR 8-K current filings RSS.
        Catches material events, earnings releases, guidance changes filed with SEC.
        No API key. No rate limit. Polled every 10 minutes.
        """
        try:
            import feedparser
        except ImportError:
            return []
        try:
            _rss_resp = requests.get(
                "https://www.sec.gov/cgi-bin/browse-edgar"
                "?action=getcurrent&type=8-K&dateb=&owner=include"
                "&count=20&search_text=&output=atom",
                timeout=10, headers={"User-Agent": "MTFBot/1.0 contact@example.com"}
            )
            feed = feedparser.parse(_rss_resp.content)
            # P2-RSS-403: detect Cloudflare / server-side blocks before iterating entries
            _feed_status = getattr(feed, "status", 0)
            if _feed_status in (403, 401, 429):
                logger.warning(
                    f"[RSS] HTTP {_feed_status} from feed — Cloudflare block or auth required. "
                    f"Skipping {len(getattr(feed, 'entries', []))} apparent entries."
                )
                return []
            if getattr(feed, "bozo", False):
                _bex = getattr(feed, "bozo_exception", None)
                _bex_str = str(_bex)[:120] if _bex else "unknown"
                logger.warning(f"[RSS] Feed parse error (bozo=True): {_bex_str} — may be HTML error page. Skipping.")
                return []
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 3600   # 1-hour window

            for entry in (feed.entries or [])[:20]:
                title   = entry.get("title", "") or ""
                summary = entry.get("summary", "") or ""
                text    = f"{title} {summary}"
                if not text.strip():
                    continue
                pub_dt = None
                try:
                    import time as _t
                    if entry.get("published_parsed"):
                        ts = _t.mktime(entry.published_parsed)
                        if ts < cutoff_ts:
                            continue
                        pub_dt = datetime.fromtimestamp(ts, tz=ET)
                except Exception:
                    pass
                pub_dt = pub_dt or datetime.now(ET)
                if self._is_seen(text):
                    continue
                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)
                if risk_level is None or risk_level == "MONITOR":
                    continue
                new_alerts.append(BreakingNewsAlert(
                    source="SEC-EDGAR", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_sec_check = datetime.now(ET)
            if new_alerts:
                logger.warning(f"⚡ SEC EDGAR 8-K: {len(new_alerts)} material filing(s)")
                for a in new_alerts:
                    logger.warning(f"   {a}")
            return new_alerts
        except Exception as e:
            logger.debug(f"SEC EDGAR RSS failed: {e}")
            return []

    # ── 14. MARKETAUX API ─────────────────────────────────────────────────────

    def check_marketaux_news(self) -> list:
        """
        Marketaux financial news API with sentiment scores.
        API key: MARKETAUX_API_KEY env var (free tier: 100 calls/day).
        Polled every 5 minutes.
        """
        if not self._marketaux_key:
            return []
        try:
            url = (
                f"https://api.marketaux.com/v1/news/all"
                f"?language=en&filter_entities=true&limit=10"
                f"&api_token={self._marketaux_key}"
            )
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                logger.debug(f"Marketaux: HTTP {resp.status_code}")
                return []

            articles = resp.json().get("data", [])
            new_alerts = []
            cutoff_ts  = datetime.now(ET).timestamp() - 3600

            for article in articles:
                title  = article.get("title", "") or ""
                desc   = article.get("description", "") or ""
                text   = f"{title} {desc}"
                if not text.strip():
                    continue
                pub = article.get("published_at", "")
                try:
                    pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(ET)
                    if pub_dt.timestamp() < cutoff_ts:
                        continue
                except Exception:
                    pub_dt = datetime.now(ET)
                if self._is_seen(text):
                    continue
                risk_level, keywords, size_mult = self._classify(text)
                self._mark_seen(text)
                if risk_level is None or risk_level == "MONITOR":
                    continue
                new_alerts.append(BreakingNewsAlert(
                    source="Marketaux", headline=title[:120],
                    keywords=keywords, risk_level=risk_level,
                    size_mult=size_mult, timestamp=pub_dt
                ))

            self._last_marketaux_check = datetime.now(ET)
            if new_alerts:
                logger.warning(f"⚡ MARKETAUX: {len(new_alerts)} article(s)")
                for a in new_alerts:
                    logger.warning(f"   {a}")
            return new_alerts
        except Exception as e:
            logger.debug(f"Marketaux API failed: {e}")
            return []

    # ── 15. BREAKING NEWS SCAN (combines all sources) ─────────────────────────

    def _scan_finnhub_alerts(self, now: datetime) -> list:
        """
        P5-L3: Extracted from scan_breaking_news so it can run in a thread.
        Fetches Finnhub sentiment, filters negative articles, returns BreakingNewsAlerts.
        """
        alerts = []
        try:
            articles = self.get_finnhub_news_sentiment()
            self._last_news_check = now
            for article in articles:
                headline_lower = article["headline"].lower()
                sentiment      = article.get("sentiment", 0) or 0
                if sentiment < -0.5:
                    if self._is_seen(headline_lower):
                        continue
                    risk_level, keywords, size_mult = self._classify(headline_lower)
                    self._mark_seen(headline_lower)
                    if risk_level and risk_level != "MONITOR":
                        try:
                            ts = datetime.fromtimestamp(article["timestamp"], tz=ET)
                        except Exception:
                            ts = now
                        alerts.append(BreakingNewsAlert(
                            source="Finnhub",
                            headline=article["headline"],
                            keywords=keywords,
                            risk_level=risk_level,
                            size_mult=size_mult,
                            timestamp=ts,
                        ))
        except Exception as e:
            logger.debug(f"Finnhub news scan failed: {e}")
        return alerts

    def scan_breaking_news(self) -> list:
        """
        Run all news sources and return list of new BreakingNewsAlerts this cycle.
        P5-L3: Sources run concurrently via ThreadPoolExecutor — reduces worst-case
        blocking from sum(timeouts) ~100s to max(timeout) ~10s.
        """
        now = datetime.now(ET)

        # Build task list — only include sources whose rate-limit window has elapsed
        tasks: list[tuple[str, Callable]] = [
            ("truth_social", self.check_truth_social),
        ]
        if self._last_news_check is None or (now - self._last_news_check).total_seconds() > 300:
            tasks.append(("finnhub",     lambda: self._scan_finnhub_alerts(now)))
        if self._last_gnews_check is None or (now - self._last_gnews_check).total_seconds() > 600:
            tasks.append(("gnews",       self.check_gnews_breaking))
        if self._last_currents_check is None or (now - self._last_currents_check).total_seconds() > 300:
            tasks.append(("currents",    self.check_currents_macro))
        if self._last_rss_check is None or (now - self._last_rss_check).total_seconds() > 180:
            tasks.append(("reuters_ap",  self.check_reuters_ap_rss))
        if self._last_war_rss_check is None or (now - self._last_war_rss_check).total_seconds() > 120:
            tasks.append(("war_geo",     self.check_war_geopolitical_rss))
        if self._last_oil_rss_check is None or (now - self._last_oil_rss_check).total_seconds() > 300:
            tasks.append(("oil_energy",  self.check_oil_energy_rss))
        if self._last_marketwatch_check is None or (now - self._last_marketwatch_check).total_seconds() > 180:
            tasks.append(("marketwatch", self.check_marketwatch_rss))
        if self._last_cnbc_check is None or (now - self._last_cnbc_check).total_seconds() > 180:
            tasks.append(("cnbc",        self.check_cnbc_rss))
        if self._last_fed_check is None or (now - self._last_fed_check).total_seconds() > 300:
            tasks.append(("fed",         self.check_fed_rss))
        if self._last_bls_check is None or (now - self._last_bls_check).total_seconds() > 300:
            tasks.append(("bls",         self.check_bls_rss))
        if self._last_sec_check is None or (now - self._last_sec_check).total_seconds() > 600:
            tasks.append(("sec",         self.check_sec_edgar_rss))
        if self._last_marketaux_check is None or (now - self._last_marketaux_check).total_seconds() > 300:
            tasks.append(("marketaux",   self.check_marketaux_news))

        # Run due tasks concurrently; collect all results before post-processing
        # HANG FIX: Do NOT use `with ThreadPoolExecutor` — its __exit__ calls
        # shutdown(wait=True) which blocks until ALL threads complete. If any
        # feedparser/RSS thread hangs (no socket timeout), run_cycle() freezes
        # indefinitely and the watchdog fires.
        #
        # AWP audit correction (2026-06-28): this comment previously claimed
        # we call shutdown(wait=False, cancel_futures=True) on timeout — that
        # call does NOT exist below, and must not be added against
        # self._executor specifically: it is a persistent, instance-level pool
        # reused every scan_breaking_news() cycle, and shutdown() permanently
        # disables a ThreadPoolExecutor (every subsequent .submit() raises
        # RuntimeError). What actually happens on timeout: we log the skipped
        # source names and move on, leaving the stuck future's thread to
        # finish in the background whenever it finishes. This is safe but not
        # free — a thread that hangs past the 12s as_completed() window
        # without ever returning permanently occupies one of the 6
        # max_workers slots until the bot's next restart. Known latent risk,
        # not fixed here: a periodic executor health check/recreation would
        # close this gap; logged to CLAUDE.md Future Roadmap Log instead of a
        # rushed structural change. Not a capital-risk bug — this module is
        # informational-only (MARKET-REACTION-FIRST architecture, SPY price
        # action is the sole sizing trigger), so a degraded news feed reduces
        # dashboard/HALT-detection coverage over very long uptimes, it does
        # not corrupt P&L or sizing.
        from concurrent.futures import TimeoutError as _FutTimeout
        new_alerts = []
        _pool = self._executor
        future_to_name = {_pool.submit(fn): name for name, fn in tasks}
        try:
            for fut in as_completed(future_to_name, timeout=12):
                name = future_to_name[fut]
                try:
                    result = fut.result()
                    if result:
                        new_alerts.extend(result)
                except Exception as _fe:
                    logger.debug(f"News source '{name}' raised in thread: {_fe}")
        except _FutTimeout:
            _skipped = [future_to_name[f] for f in future_to_name if not f.done()]
            logger.warning(f"scan_breaking_news: source timeout — skipped: {_skipped}")

        # ── Macro risk state detection ────────────────────────────────────────
        # Track sustained CAUTION alerts across all macro risk domains (24h window).
        # When MACRO_RISK_THRESHOLD is reached, _macro_risk_active is set True.
        # Sizing is owned by MacroRiskIndex — this flag is used only for
        # event-type classification and dashboard display.
        # Apr 6 2026: renamed from _war_state_* → _macro_risk_*; keywords
        # expanded to 9-domain taxonomy; bare "war" removed (Finding 7 fix).
        for _alert in new_alerts:
            _ahl = _alert.headline.lower()
            if _alert.risk_level in ("CAUTION", "HALT") and \
               any(_mk in _ahl for _mk in MACRO_RISK_KEYWORDS):
                self._macro_risk_window[_content_hash(_alert.headline)] = now

       # Purge window entries older than 24 hours
        _mr_cutoff = now - timedelta(hours=24)
        self._macro_risk_window = {k: v for k, v in self._macro_risk_window.items() if v > _mr_cutoff}
        self._persist_macro_risk_window()   # T3b: write changes to disk

        if len(self._macro_risk_window) >= MACRO_RISK_THRESHOLD:
            if not self._macro_risk_active:
                logger.critical(
                    f"🚨 MACRO RISK STATE ACTIVATED — {len(self._macro_risk_window)} "
                    f"macro-risk alerts in last 24h across domains. "
                    f"MRI will reflect elevated background. Sizing managed by MacroRiskIndex."
                )
            self._macro_risk_active = True
        elif self._macro_risk_active and len(self._macro_risk_window) == 0:
            logger.info("🟢 MACRO RISK STATE DEACTIVATED — no macro-risk alerts in last 24h.")
            self._macro_risk_active = False

        # Add and prune expired alerts.
        # Only promote alerts to active if they are within the alert window —
        # prevents stale posts (detected as "new" after a bot restart because
        # _seen_hashes is in-memory only) from re-penalising size for 30 min.
        cutoff = now - timedelta(minutes=self._alert_duration_mins)
        fresh_alerts = [
            a for a in new_alerts
            if getattr(a, "timestamp", None) is not None and a.timestamp > cutoff
        ]
        with self._alerts_lock:
            self._active_alerts.extend(fresh_alerts)
            self._active_alerts = [
                a for a in self._active_alerts
                if getattr(a, "timestamp", None) is not None and a.timestamp > cutoff
            ]
            if len(self._active_alerts) > 100:
                self._active_alerts = sorted(
                    self._active_alerts,
                    key=lambda a: getattr(a, "timestamp", datetime.min.replace(tzinfo=ET)),
                    reverse=True,
                )[:100]

        return new_alerts

    # ── SIZE MULTIPLIER (plugs into run_cycle) ────────────────────────────────

    def get_news_size_multiplier(self, price_change_pct: float = 0.0) -> float:
        """
        Returns 0.0 if any active alert is HALT (exchange-level emergency only).
        Returns 1.0 for all other news — sizing is owned by the SPY 5-min bar
        engine in run_cycle(), not by news keywords (April 2026 architecture).

        Args:
            price_change_pct: Unused — kept for call-site compatibility.

        Returns:
            0.0 (HALT active) or 1.0 (all other cases)
        """
        if not self._active_alerts:
            return 1.0

        # NM-3: docstring updated to match actual behavior. Prior docstring described
        # price-confirmation penalties (HIGH_RISK → 0.75, CAUTION → 1.0) that were
        # removed in the April 2026 market-reaction-first architecture switch.
        # All keyword-based sizing is gone. Only HALT (circuit breaker / exchange
        # emergency) is enforced here. Everything else is 1.0.
        for alert in self._active_alerts:
            if alert.risk_level == "HALT":
                return 0.0   # circuit breaker / trading halt — always enforce

        return 1.0   # all other news: zero size impact

    def is_macro_risk_active(self) -> bool:
        """Returns True when sustained macro risk CAUTION state is active (display only).
        Replaces is_war_state_active() — Apr 6 2026 audit rename.
        Sizing decisions are owned by MacroRiskIndex, not this flag.
        """
        return self._macro_risk_active

    # Backward-compat alias — remove after main.py is updated in T1-C
    def is_war_state_active(self) -> bool:
        return self._macro_risk_active

    def get_active_event_type(self) -> str:
        """
        Called by main.py AFTER a SPY+QQQ price trigger to classify the event type.
        Returns one of the 9 MRI event type labels for the hybrid engine.

        Apr 6 2026 fixes:
          Finding 3: Iterates newest-first (reversed) — most recent alert wins
                     classification over stale older alerts in the 30-min window.
          Finding 7: War/geo keywords no longer include bare "war" — expanded to
                     domain-specific sets that don't false-positive on trade war.
          Expanded:  WAR_GEO/FED_POLICY/TECHNICAL → 9-type taxonomy.

        Hybrid engine persistence by type:
          GEO_CONFLICT   7 scans | GEO_ENERGY     5 scans
          MACRO_MONETARY 4 scans | MACRO_CREDIT   6 scans
          MACRO_FX       5 scans | MACRO_SYSTEMIC 8 scans
          GLOBAL_ASIA    3 scans | GLOBAL_EU      3 scans
          TECHNICAL      3 scans

        Never drives sizing directly — SPY price trigger must fire first.
        """
        # Newest-first iteration: most recent alert wins (Finding 3 fix)
        with self._alerts_lock:
            _alerts_snapshot = list(self._active_alerts)
        for alert in reversed(_alerts_snapshot):
            hl = alert.headline.lower()
            if any(kw in hl for kw in MACRO_SYSTEMIC_KEYWORDS):
                return "MACRO_SYSTEMIC"
            if any(kw in hl for kw in GEO_CONFLICT_KEYWORDS):
                return "GEO_CONFLICT"
            if any(kw in hl for kw in GEO_ENERGY_KEYWORDS):
                return "GEO_ENERGY"
            if any(kw in hl for kw in MACRO_CREDIT_KEYWORDS):
                return "MACRO_CREDIT"
            if any(kw in hl for kw in MACRO_FX_KEYWORDS):
                return "MACRO_FX"
            if any(kw in hl for kw in MACRO_MONETARY_KEYWORDS):
                return "MACRO_MONETARY"

        # Fallback: macro risk state active but no matching active alert
        # — sustained background defaults to GEO_CONFLICT (most common backdrop)
        if self._macro_risk_active:
            return "GEO_CONFLICT"

        return "TECHNICAL"

    def get_news_risk_note(self) -> str:
        """Returns human-readable summary of active alerts for logging."""
        if not self._active_alerts:
            return ""
        most_recent = sorted(self._active_alerts, key=lambda a: a.timestamp, reverse=True)[0]
        age_mins = int((datetime.now(ET) - most_recent.timestamp).total_seconds() / 60)
        return (f"{most_recent.source} {most_recent.risk_level}: "
                f"{most_recent.headline[:60]}... ({age_mins}m ago)")

    def get_summary(self) -> dict:
        """Full status summary for logging/dashboard."""
        with self._alerts_lock:
            _alerts_snapshot = list(self._active_alerts)
        items = []
        for alert in _alerts_snapshot[-20:]:
            items.append({
                "time":     alert.timestamp.astimezone(PT).strftime("%I:%M %p PT") if hasattr(alert, "timestamp") else "—",
                "headline": alert.headline if hasattr(alert, "headline") else str(alert),
                "source":   alert.source if hasattr(alert, "source") else "NEWS",
                "risk":     alert.risk_level if hasattr(alert, "risk_level") else "monitor",
            })
        def _iso(dt) -> str | None:
            return dt.isoformat() if dt else None

        return {
            # ── Active alert state ────────────────────────────────────────────
            "active_alerts":          len(_alerts_snapshot),
            "news_size_mult":         self.get_news_size_multiplier(),
            # Renamed Apr 6 2026: war_state_* → macro_risk_*
            # Backward-compat keys kept so dashboard/bot_status reads don't break
            # until T1-C/T1-D updates main.py and generate_dashboard.py.
            "war_state_active":       self._macro_risk_active,   # compat alias
            "macro_risk_active":      self._macro_risk_active,
            "war_state_alert_count":  len(self._macro_risk_window),  # compat alias
            "macro_risk_alert_count": len(self._macro_risk_window),
            "active_event_type":      self.get_active_event_type() if (_alerts_snapshot or self._macro_risk_active) else "",
            # ── Source enable flags ───────────────────────────────────────────
            "finnhub_enabled":        bool(self._finnhub_key),
            "gnews_enabled":          bool(self._gnews_key),
            "currents_enabled":       bool(self._currents_key),
            "marketaux_enabled":      bool(self._marketaux_key),
            "rss_enabled":            True,   # Reuters/AP — no key required
            "war_rss_enabled":        True,
            "oil_rss_enabled":        True,
            "marketwatch_enabled":    True,
            "cnbc_enabled":           True,
            "fed_rss_enabled":        True,
            "bls_rss_enabled":        True,
            "sec_edgar_enabled":      True,
            # ── Last poll timestamps (all 14 sources) ─────────────────────────
            "last_truth_check":       _iso(self._last_truth_check),
            "last_finnhub_check":     _iso(self._last_news_check),
            "last_gnews_check":       _iso(self._last_gnews_check),
            "last_currents_check":    _iso(self._last_currents_check),
            "last_rss_check":         _iso(self._last_rss_check),
            "last_war_rss_check":     _iso(self._last_war_rss_check),
            "last_oil_rss_check":     _iso(self._last_oil_rss_check),
            "last_marketwatch_check": _iso(self._last_marketwatch_check),
            "last_cnbc_check":        _iso(self._last_cnbc_check),
            "last_fed_check":         _iso(self._last_fed_check),
            "last_bls_check":         _iso(self._last_bls_check),
            "last_sec_check":         _iso(self._last_sec_check),
            "last_marketaux_check":   _iso(self._last_marketaux_check),
            # ── Latest alert + item list ──────────────────────────────────────
            "latest_alert":           str(_alerts_snapshot[-1]) if _alerts_snapshot else None,
            "news_items":             items,
        }
