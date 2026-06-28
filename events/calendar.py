# ruff: noqa: E501
# events/calendar.py — Market-moving event tracker
# All risk days trade at 50% capital deployment vs normal days.
# Only true market holidays (BLACKOUT) result in no trading.
#   - BLACKOUT:    no trades — market holidays only
#   - HIGH_RISK:   50% size — FOMC, CPI, NFP, earnings
#   - CAUTION:     50% size — quad witching, early closes
#   - OPPORTUNITY: 50% size — tech conferences, known catalysts
#   - CLEAR:       100% size — no known events

from datetime import date, timedelta
from enum import Enum
from typing import Optional


class EventRisk(Enum):
    BLACKOUT    = "blackout"    # No trades — market holidays, closed sessions only
    HIGH_RISK   = "high_risk"   # Trade at 50% size — FOMC, CPI, earnings, major events
    CAUTION     = "caution"     # Trade at 50% size — quad witching, early closes
    OPPORTUNITY = "opportunity" # Trade at 50% size — tech conferences, known catalysts
    CLEAR       = "clear"       # Full size — no known events


class EventType(Enum):
    FED_MEETING         = "Fed Meeting / FOMC"
    FED_MINUTES         = "Fed Minutes"
    FED_SPEECH          = "Fed Chair Speech"
    TREASURY_AUCTION    = "Treasury Auction"
    CPI                 = "CPI Release"
    PPI                 = "PPI Release"
    JOBS_REPORT         = "Jobs Report (NFP)"
    GDP                 = "GDP Release"
    EARNINGS            = "Earnings"
    NVDA_GTC            = "NVIDIA GTC Conference"
    APPLE_WWDC          = "Apple WWDC"
    APPLE_EVENT         = "Apple Product Event"
    MSFT_BUILD          = "Microsoft Build"
    GOOGLE_IO           = "Google I/O"
    META_CONNECT        = "Meta Connect"
    AMAZON_PRIME        = "Amazon Prime Day"
    OPEX                = "Options Expiration"
    TRIPLE_WITCHING     = "Triple Witching"
    QUAD_WITCHING       = "Quad Witching"
    MARKET_HOLIDAY      = "Market Holiday / Early Close"
    OTHER               = "Other"


# ─── STATIC CALENDAR (update annually) ───────────────────────────────────────
# Format: {"date": "YYYY-MM-DD", "type": EventType, "risk": EventRisk,
#           "symbol": "AAPL" (optional — only block that symbol),
#           "note": "human readable"}

STATIC_EVENTS = [

    # ── FED / MACRO 2025 ──────────────────────────────────────────────────────
    {"date": "2025-01-29", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2025-03-19", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2025-05-07", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2025-06-18", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2025-07-30", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2025-09-17", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2025-10-29", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2025-12-10", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},

    # ── FED / MACRO 2026 ──────────────────────────────────────────────────────
    {"date": "2026-01-28", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2026-03-18", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2026-05-06", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2026-06-17", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2026-07-29", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2026-09-16", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2026-11-04", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},
    {"date": "2026-12-16", "type": EventType.FED_MEETING,      "risk": EventRisk.HIGH_RISK,   "note": "FOMC Rate Decision — 50% size"},

    # ── CPI RELEASES 2026 (source: BLS official schedule) ────────────────────
    # All released at 8:30 AM ET — market typically volatile at open
    {"date": "2026-01-13", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Dec 2025 — 50% size"},
    {"date": "2026-02-11", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Jan 2026 — 50% size"},
    {"date": "2026-03-11", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Feb 2026 — 50% size"},
    {"date": "2026-04-10", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Mar 2026 — 50% size"},
    {"date": "2026-05-12", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Apr 2026 — 50% size"},
    {"date": "2026-06-10", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI May 2026 — 50% size"},
    {"date": "2026-07-14", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Jun 2026 — 50% size"},
    {"date": "2026-08-12", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Jul 2026 — 50% size"},
    {"date": "2026-09-11", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Aug 2026 — 50% size"},
    {"date": "2026-10-14", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Sep 2026 — 50% size"},
    {"date": "2026-11-10", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Oct 2026 — 50% size"},
    {"date": "2026-12-10", "type": EventType.CPI, "risk": EventRisk.HIGH_RISK, "note": "CPI Nov 2026 — 50% size"},

    # ── PPI RELEASES 2026 (source: BLS official schedule) ────────────────────
    # All released at 8:30 AM ET — 1-2 days after CPI, still moves markets
    {"date": "2026-01-14", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Nov 2025 — 50% size"},
    {"date": "2026-01-30", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Dec 2025 — 50% size"},
    {"date": "2026-02-27", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Jan 2026 — 50% size"},
    {"date": "2026-03-12", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Feb 2026 — 50% size"},
    {"date": "2026-04-14", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Mar 2026 — 50% size"},
    {"date": "2026-05-13", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Apr 2026 — 50% size"},
    {"date": "2026-06-11", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI May 2026 — 50% size"},
    {"date": "2026-07-15", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Jun 2026 — 50% size"},
    {"date": "2026-08-13", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Jul 2026 — 50% size"},
    {"date": "2026-09-10", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Aug 2026 — 50% size"},
    {"date": "2026-10-15", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Sep 2026 — 50% size"},
    {"date": "2026-11-13", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Oct 2026 — 50% size"},
    {"date": "2026-12-15", "type": EventType.PPI, "risk": EventRisk.HIGH_RISK, "note": "PPI Nov 2026 — 50% size"},

    # ── NFP / EMPLOYMENT SITUATION 2026 (source: BLS official schedule) ──────
    # All released at 8:30 AM ET on first Friday of the month
    {"date": "2026-01-09", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Dec 2025 — 50% size"},
    {"date": "2026-02-06", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Jan 2026 — 50% size"},
    {"date": "2026-03-06", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Feb 2026 — 50% size"},
    {"date": "2026-04-03", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Mar 2026 — 50% size"},
    {"date": "2026-05-08", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Apr 2026 — 50% size"},
    {"date": "2026-06-05", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP May 2026 — 50% size"},
    {"date": "2026-07-02", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Jun 2026 — 50% size"},
    {"date": "2026-08-07", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Jul 2026 — 50% size"},
    {"date": "2026-09-04", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Aug 2026 — 50% size"},
    {"date": "2026-10-02", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Sep 2026 — 50% size"},
    {"date": "2026-11-06", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Oct 2026 — 50% size"},
    {"date": "2026-12-04", "type": EventType.JOBS_REPORT, "risk": EventRisk.HIGH_RISK, "note": "NFP Nov 2026 — 50% size"},

    # ── QUAD WITCHING 2026 (3rd Friday of Mar/Jun/Sep/Dec) ────────────────────────────
    {"date": "2026-03-20", "type": EventType.QUAD_WITCHING,    "risk": EventRisk.CAUTION,     "note": "Quad Witching Q1 — stock idx futures, stock futures, stock idx options, stock options expire"},
    {"date": "2026-06-18", "type": EventType.QUAD_WITCHING,    "risk": EventRisk.CAUTION,     "note": "Quad Witching Q2 — moved to Thu (Jun 19 = Juneteenth market holiday)"},
    {"date": "2026-06-19", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Juneteenth National Independence Day"},
    {"date": "2026-09-18", "type": EventType.QUAD_WITCHING,    "risk": EventRisk.CAUTION,     "note": "Quad Witching Q3 — all four derivative classes expire simultaneously"},
    {"date": "2026-12-18", "type": EventType.QUAD_WITCHING,    "risk": EventRisk.CAUTION,     "note": "Quad Witching Q4 — all four derivative classes expire simultaneously"},

    # ── QUARTER-END / WINDOW DRESSING 2026 ───────────────────────────────────
    # Last trading day of each quarter — institutional window dressing creates
    # unusual intraday flows (forced rebalancing, index reconstitution, fund
    # managers painting performance). Signals can be misleading near close.
    # CAUTION all day: 50% size, no new entries after 3:00 PM ET.
    {"date": "2026-03-31", "type": EventType.OTHER, "risk": EventRisk.CAUTION,  "note": "Q1 Quarter-End / Window Dressing — 50% size, caution near close"},
    {"date": "2026-06-30", "type": EventType.OTHER, "risk": EventRisk.CAUTION,  "note": "Q2 Quarter-End / Window Dressing — 50% size, caution near close"},
    {"date": "2026-09-30", "type": EventType.OTHER, "risk": EventRisk.CAUTION,  "note": "Q3 Quarter-End / Window Dressing — 50% size, caution near close"},
    {"date": "2026-12-31", "type": EventType.OTHER, "risk": EventRisk.CAUTION,  "note": "Q4 Quarter-End / Window Dressing + Year-End — 50% size, caution near close"},

    # ── TECH CONFERENCES ──────────────────────────────────────────────────────
    {"date": "2026-03-17", "type": EventType.NVDA_GTC,         "risk": EventRisk.OPPORTUNITY, "symbol": "NVDA", "note": "NVIDIA GTC 2026 Keynote — 50% size"},
    {"date": "2026-03-18", "type": EventType.NVDA_GTC,         "risk": EventRisk.OPPORTUNITY, "symbol": "NVDA", "note": "NVIDIA GTC Day 2 — 50% size"},
    {"date": "2026-06-08", "type": EventType.APPLE_WWDC,       "risk": EventRisk.OPPORTUNITY, "symbol": "AAPL", "note": "Apple WWDC 2026 Keynote — 50% size"},
    {"date": "2026-05-19", "type": EventType.MSFT_BUILD,       "risk": EventRisk.OPPORTUNITY, "symbol": "MSFT", "note": "Microsoft Build 2026 — 50% size"},
    {"date": "2026-05-20", "type": EventType.GOOGLE_IO,        "risk": EventRisk.OPPORTUNITY, "symbol": "GOOGL","note": "Google I/O 2026 — 50% size"},

    # ── MARKET HOLIDAYS 2026 ──────────────────────────────────────────────────
    {"date": "2026-01-01", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "New Year's Day"},
    {"date": "2026-01-19", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "MLK Day"},
    {"date": "2026-02-16", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Presidents Day"},
    {"date": "2026-04-03", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Good Friday"},
    {"date": "2026-05-25", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Memorial Day"},
    {"date": "2026-07-03", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Independence Day (observed)"},
    {"date": "2026-09-07", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Labor Day"},
    {"date": "2026-10-12", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.CAUTION,     "note": "Columbus Day — NYSE open, bond market closed, thin liquidity"},
    {"date": "2026-11-11", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.CAUTION,     "note": "Veterans Day — NYSE open, bond market closed, thin liquidity"},
    {"date": "2026-11-26", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Thanksgiving"},
    {"date": "2026-11-27", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.CAUTION,     "note": "Black Friday (early close)"},
    {"date": "2026-12-24", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.CAUTION,     "note": "Christmas Eve (early close)"},
    {"date": "2026-12-25", "type": EventType.MARKET_HOLIDAY,   "risk": EventRisk.BLACKOUT,    "note": "Christmas Day"},
    # ── CORE PCE 2026 (Fed's preferred inflation measure) ────────────────────
    {"date": "2026-01-30", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Dec 2025 — 50% size"},
    {"date": "2026-02-27", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Jan 2026 — 50% size"},
    {"date": "2026-03-27", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Feb 2026 — 50% size"},
    {"date": "2026-04-30", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Mar 2026 — 50% size"},
    {"date": "2026-05-29", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Apr 2026 — 50% size"},
    {"date": "2026-06-26", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE May 2026 — 50% size"},
    {"date": "2026-07-31", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Jun 2026 — 50% size"},
    {"date": "2026-08-28", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Jul 2026 — 50% size"},
    {"date": "2026-09-25", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Aug 2026 — 50% size"},
    {"date": "2026-10-30", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Sep 2026 — 50% size"},
    {"date": "2026-11-25", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Oct 2026 — 50% size"},
    {"date": "2026-12-19", "type": EventType.OTHER, "risk": EventRisk.HIGH_RISK, "note": "Core PCE Nov 2026 — 50% size"},

    # ── FOMC MINUTES 2026 (released ~3 weeks after each meeting) ─────────────
    {"date": "2026-02-18", "type": EventType.FED_MINUTES, "risk": EventRisk.HIGH_RISK, "note": "FOMC Minutes Jan meeting — 50% size"},
    {"date": "2026-04-08", "type": EventType.FED_MINUTES, "risk": EventRisk.HIGH_RISK, "note": "FOMC Minutes Mar meeting — 50% size"},
    {"date": "2026-05-27", "type": EventType.FED_MINUTES, "risk": EventRisk.HIGH_RISK, "note": "FOMC Minutes May meeting — 50% size"},
    {"date": "2026-07-08", "type": EventType.FED_MINUTES, "risk": EventRisk.HIGH_RISK, "note": "FOMC Minutes Jun meeting — 50% size"},
    {"date": "2026-09-09", "type": EventType.FED_MINUTES, "risk": EventRisk.HIGH_RISK, "note": "FOMC Minutes Jul meeting — 50% size"},
    {"date": "2026-10-07", "type": EventType.FED_MINUTES, "risk": EventRisk.HIGH_RISK, "note": "FOMC Minutes Sep meeting — 50% size"},
    {"date": "2026-11-25", "type": EventType.FED_MINUTES, "risk": EventRisk.HIGH_RISK, "note": "FOMC Minutes Nov meeting — 50% size"},

    # ── GDP ADVANCE ESTIMATES 2026 (most market-moving GDP release) ───────────
    {"date": "2026-01-29", "type": EventType.GDP, "risk": EventRisk.HIGH_RISK, "note": "GDP Q3 2025 advance — 50% size"},
    {"date": "2026-04-29", "type": EventType.GDP, "risk": EventRisk.HIGH_RISK, "note": "GDP Q4 2025 advance — 50% size"},
    {"date": "2026-07-29", "type": EventType.GDP, "risk": EventRisk.HIGH_RISK, "note": "GDP Q1 2026 advance — 50% size"},
    {"date": "2026-10-28", "type": EventType.GDP, "risk": EventRisk.HIGH_RISK, "note": "GDP Q2 2026 advance — 50% size"},
]

# ─── PER-SYMBOL EARNINGS BLACKOUTS ───────────────────────────────────────────
# Add known earnings dates here. Bot will not trade that symbol on earnings day.
# Format: ("YYYY-MM-DD", "TICKER")
EARNINGS_BLACKOUTS = [
    ("2026-01-29", "AAPL"),
    ("2026-01-29", "META"),
    ("2026-02-05", "AMZN"),
    ("2026-02-05", "GOOGL"),
    ("2026-02-25", "NVDA"),
    ("2026-04-29", "MSFT"),
    ("2026-04-30", "META"),
    ("2026-05-06", "AMZN"),
    # Add more as earnings dates become known
]


# ── Known release times by event type (all times in ET) ──────────────────────
# Used by get_size_multiplier() for post-release normalization.
# 30 min after these times, HIGH_RISK size steps up from 0.50x → 0.75x.
# Morning releases (8:30 ET) are already normalized when RTH opens at 9:30 ET.
# FOMC (2:00 PM ET) normalizes at 2:30 PM — last ~90 min of trading window.
# Events with no entry here (CAUTION, earnings, speeches) stay at base rate all day.
_RELEASE_TIMES = {
    EventType.CPI:         "08:30",   # BLS — 8:30 AM ET
    EventType.PPI:         "08:30",   # BLS — 8:30 AM ET
    EventType.JOBS_REPORT: "08:30",   # BLS — 8:30 AM ET
    EventType.GDP:         "08:30",   # BEA — 8:30 AM ET
    EventType.OTHER:       "08:30",   # Core PCE (BEA) — 8:30 AM ET
    EventType.FED_MEETING: "14:00",   # FOMC rate decision — 2:00 PM ET
    EventType.FED_MINUTES: "14:00",   # Fed minutes release — 2:00 PM ET
}


_MACRO_EVENT_TYPES = {
    EventType.FED_MEETING,
    EventType.CPI,
    EventType.JOBS_REPORT,
    EventType.GDP,
    EventType.FED_MINUTES,  # 2 PM ET release — same volatility profile as rate decision
    EventType.PPI,
}


def is_macro_event_day(date_str: Optional[str] = None) -> bool:
    """
    Returns True if the given date (YYYY-MM-DD, default today ET) is a FOMC/CPI/NFP day.
    Checks STATIC_EVENTS only — does not require an EventCalendar instance.
    Used by signal_generator for c12 score (NEW-1, board-approved 2026-04-20).
    """
    if date_str is None:
        from datetime import date as _date
        date_str = _date.today().isoformat()
    for event in STATIC_EVENTS:
        if event["date"] == date_str and event.get("type") in _MACRO_EVENT_TYPES:
            return True
    return False


class EventCalendar:
    def __init__(self):
        self._events = STATIC_EVENTS
        self._earnings = EARNINGS_BLACKOUTS

    def get_day_risk(self, check_date: Optional[date] = None, symbol: Optional[str] = None) -> dict:
        """
        Returns the highest risk level for a given date (and optionally symbol).

        Returns:
            {
                "risk":   EventRisk,
                "events": [list of matching events],
                "note":   str summary
            }
        """
        if check_date is None:
            check_date = date.today()

        date_str = check_date.strftime("%Y-%m-%d")
        matching = []
        highest_risk = EventRisk.CLEAR

        risk_order = [EventRisk.CLEAR, EventRisk.OPPORTUNITY, EventRisk.CAUTION, EventRisk.HIGH_RISK, EventRisk.BLACKOUT]

        for event in self._events:
            if event["date"] != date_str:
                continue
            # If event is symbol-specific and symbol doesn't match, skip
            event_symbol = event.get("symbol")
            if event_symbol and symbol and event_symbol != symbol:
                continue
            # If event is symbol-specific and no symbol provided, still flag market-wide events
            if event_symbol and not symbol:
                continue

            matching.append(event)
            if risk_order.index(event["risk"]) > risk_order.index(highest_risk):
                highest_risk = event["risk"]

        # Check earnings blackouts
        for earn_date, earn_sym in self._earnings:
            if earn_date == date_str:
                if symbol and earn_sym == symbol:
                    matching.append({
                        "date": date_str, "type": EventType.EARNINGS,
                        "risk": EventRisk.HIGH_RISK, "symbol": earn_sym,
                        "note": f"{earn_sym} earnings — 50% size"
                    })
                    if risk_order.index(EventRisk.HIGH_RISK) > risk_order.index(highest_risk):
                        highest_risk = EventRisk.HIGH_RISK

        notes = [e["note"] for e in matching]
        return {
            "risk":   highest_risk,
            "events": matching,
            "note":   " | ".join(notes) if notes else "Clear",
        }

    def get_week_ahead(self, symbol: Optional[str] = None) -> list:
        """Returns all events in the next 7 days."""
        today = date.today()
        results = []
        for i in range(7):
            d = today + timedelta(days=i)
            info = self.get_day_risk(d, symbol)
            if info["risk"] != EventRisk.CLEAR:
                results.append({"date": d.isoformat(), **info})
        return results

    def add_earnings(self, date_str: str, symbol: str):
        """Dynamically add an earnings date."""
        self._earnings.append((date_str, symbol))

    def add_event_dynamic(self, date_str: str, event_type, risk: EventRisk, note: str):
        """
        Dynamically add a macro event (used by NewsMonitor / Finnhub calendar).
        Deduplicates by date+type to avoid double-adding the same event.
        """
        existing = {(e["date"], e["type"]) for e in self._events}
        if (date_str, event_type) not in existing:
            self._events.append({
                "date": date_str,
                "type": event_type,
                "risk": risk,
                "note": note,
            })

    def is_blackout(self, symbol: Optional[str] = None) -> bool:
        return self.get_day_risk(symbol=symbol)["risk"] == EventRisk.BLACKOUT

    def get_size_multiplier(self, symbol: Optional[str] = None, now_et=None) -> float:
        """
        Returns position size multiplier based on today's event risk.

        Post-release normalization (HIGH_RISK days only):
          Before release or no release time known → 0.50x
          30+ min after release                   → 0.75x

        Size table:
          CLEAR       → 1.00x
          OPPORTUNITY → 0.80x  (tech conferences — uncertainty, not fear)
          CAUTION     → 0.50x  (quad witching, early close — all day, no release)
          HIGH_RISK   → 0.50x pre-release  |  0.75x post-release (+30 min)
          BLACKOUT    → 0.00x  (market holidays only)

        Pass now_et=datetime.now(ET) from the caller to enable normalization.
        Omitting now_et returns the conservative pre-release rate all day.
        """
        day_info = self.get_day_risk(symbol=symbol)
        risk     = day_info["risk"]

        base = {
            EventRisk.CLEAR:       1.0,
            EventRisk.OPPORTUNITY: 0.8,
            EventRisk.CAUTION:     0.5,
            EventRisk.HIGH_RISK:   0.5,
            EventRisk.BLACKOUT:    0.0,
        }[risk]

        # Post-release normalization — only applies to HIGH_RISK with known release times
        if risk == EventRisk.HIGH_RISK and now_et is not None:
            current_mins = now_et.hour * 60 + now_et.minute
            for event in day_info.get("events", []):
                release_str = _RELEASE_TIMES.get(event.get("type"))
                if not release_str:
                    continue
                rel_h, rel_m  = map(int, release_str.split(":"))
                release_mins  = rel_h * 60 + rel_m
                if current_mins >= release_mins + 30:
                    return 0.75   # Number is out, reaction is known — step up to 0.75x

        return base

    def is_tradeable(self, symbol: Optional[str] = None) -> bool:
        """Returns True if trading is allowed today (any non-holiday day)."""
        return self.get_day_risk(symbol=symbol)["risk"] != EventRisk.BLACKOUT
