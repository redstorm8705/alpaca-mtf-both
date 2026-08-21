# ruff: noqa: E501 — aligned field/docstring comment columns intentionally exceed 88 for readability
"""reporting/report_figures.py — the SINGLE canonical figures object for every report page.

WHY THIS EXISTS
    Report pages (dashboard / weekly / monthly) previously read P&L from several different
    sources at once — a live Alpaca-FIFO ledger, frozen eod_*.json snapshots, the polluted
    trade_log.json, and a stale lifetime cache. So tiles disagreed: day-cells summed to a
    different number than the headline, a red day could show "100% win rate", and lifetime
    P&L differed page-to-page (+$199.86 vs +$196.80). Every past fix repointed ONE tile and
    silently broke its neighbour's reconciliation — the "plagued since April" whack-a-mole.

THE FIX
    Build ONE version-stamped snapshot per render from reporting.pnl_ledger.build_ledger()
    (the Alpaca-FIFO single source of truth) and derive EVERY $/count/win-rate view from the
    SAME list of realized round-trips. Because a day's $ and its win-rate come from the same
    round-trip list, and the period total is the sum of the day values it displays, the page
    is coherent BY CONSTRUCTION:
      - a red day (Sum pnl < 0) can never show 100% WR (that would require every round-trip
        to be a win, i.e. Sum pnl > 0) — the impossible rows are structurally eliminated;
      - headline == Sum(day-cells) because both are the same rounded per-day values;
      - lifetime is one number, sourced from the same snapshot on every page.

    eod_*.json / trade_log.json remain in use by the pages ONLY for non-P&L metadata
    (score, TQI, loss-driver, MRI regime) — never for $ / count / win-rate.

LAYERS (GAI dual-attribution, 2026-08-20)
    - GRID / accounting layer: day-cells attribute realized P&L by the CLOSING FILL date
      (per_day) so tiles match settled broker cash for that day.
    - PERFORMANCE layer: win-rate / profit-factor / trade-count are ENTRY-LEVEL (partials of
      one position merged), attributed to the period its exits fall in. These are two
      explicitly-separate, individually-correct layers — not the same number.

Data tier: T1 (Alpaca, via pnl_ledger — read-only). Design record:
logs/design_records/report_single_source_of_truth_2026-08-20.md
(board + adversarial + Gro + GAI aligned 2026-08-20; risk-path: NO — reporting only).
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")

# Fallback basis before the ledger reports real net_deposits (paper account start equity).
_INITIAL_PAPER_EQUITY = 2500.0


@dataclass
class ReportFigures:
    """Immutable, version-stamped snapshot of all report P&L figures, derived entirely from
    ONE build_ledger() call. Pass a SINGLE instance to every tile on a page so no tile does
    its own live fetch and every number provably comes from the same data (self.version)."""

    available: bool
    version: str
    ts_pt: str
    round_trips: list = field(default_factory=list)      # each: exit_date/symbol/direction/qty/pnl/entry_time/is_qhm/...
    equity: float | None = None
    net_deposits: float = _INITIAL_PAPER_EQUITY
    unrealized: float = 0.0
    invariant: dict = field(default_factory=dict)         # ledger's equity reconciliation (realized+unrealized vs equity-deposits)
    unmatched_closes: list = field(default_factory=list)  # closes with no prior lot — real cash, $0 in round_trips (surfaced, never fabricated)
    _by_exit_date: dict = field(default_factory=dict)      # exit_date -> [round_trips]  (built once)

    # ── GRID / accounting layer — attributed by the closing FILL date ──────────────────────
    def day_round_trips(self, day: str) -> list:
        """Realized round-trips whose CLOSE landed on this PT calendar day (YYYY-MM-DD)."""
        return self._by_exit_date.get(day, [])

    def has_day(self, day: str) -> bool:
        """True if any realized close happened this day (distinct from an eod file existing
        for metadata but with no closed trade)."""
        return day in self._by_exit_date

    def day_stats(self, day: str) -> dict:
        """The day tile's $ AND win-rate from the SAME round-trip list — so they can never
        disagree. n_trades is round-trip (grid) level; pnl is sum-of-round-trips."""
        rts = self.day_round_trips(day)
        n = len(rts)
        wins = sum(1 for r in rts if float(r.get("pnl", 0.0)) > 0)
        return {
            "pnl": round(sum(float(r.get("pnl", 0.0)) for r in rts), 2),
            "n_trades": n,
            "win_rate": (wins / n * 100.0) if n else 0.0,
        }

    def day_pnl(self, day: str) -> float:
        return self.day_stats(day)["pnl"]

    # ── period views ──────────────────────────────────────────────────────────────────────
    def _period_rts(self, start: str, end: str) -> list:
        return [r for r in self.round_trips if start <= str(r.get("exit_date", "")) <= end]

    def period_days(self, start: str, end: str) -> list:
        return sorted({str(r.get("exit_date", "")) for r in self._period_rts(start, end)})

    def period_pnl(self, start: str, end: str) -> float:
        """Sum of the SAME rounded day values the grid displays (sum-of-rounds, NOT
        round-of-sum) — so headline == Sum(day-cells) exactly, never off by a rounding cent."""
        return round(sum(self.day_pnl(d) for d in self.period_days(start, end)), 2)

    def _entry_level(self, rts: list) -> dict:
        """Merge round-trips into ENTRY-level trades (partials of one position summed).
        Keyed by (symbol, entry_time) — a re-entry at a different time is a distinct trade."""
        by_entry: dict = defaultdict(float)
        for r in rts:
            by_entry[(r.get("symbol"), r.get("entry_time"))] += float(r.get("pnl", 0.0))
        return by_entry

    def period_stats(self, start: str, end: str) -> dict:
        """PERFORMANCE layer: win-rate / profit-factor / count at the ENTRY (lifecycle)
        level, attributed to the period its exits fall in. total_pnl is the GRID value
        (period_pnl) so the headline $ still ties to the day-cells."""
        by_entry = self._entry_level(self._period_rts(start, end))
        total = len(by_entry)
        wins = sum(1 for v in by_entry.values() if v > 0)
        gross_w = sum(v for v in by_entry.values() if v > 0)
        gross_l = sum(-v for v in by_entry.values() if v < 0)
        return {
            "total_pnl": self.period_pnl(start, end),
            "total_trades": total,
            "wins": wins,
            "win_rate": (wins / total * 100.0) if total else 0.0,
            "profit_factor": (gross_w / gross_l) if gross_l > 0 else None,
        }

    # ── lifetime ──────────────────────────────────────────────────────────────────────────
    def lifetime_realized(self) -> float:
        return round(sum(float(r.get("pnl", 0.0)) for r in self.round_trips), 2)

    def lifetime_total_pnl(self) -> float | None:
        """TOTAL account P&L = equity - net_deposits (realized + unrealized, Alpaca ground
        truth). None if equity was unavailable — callers show '—', never a wrong number."""
        if self.equity is None:
            return None
        return round(self.equity - self.net_deposits, 2)

    def lifetime_stats(self) -> dict:
        by_entry = self._entry_level(self.round_trips)
        total = len(by_entry)
        wins = sum(1 for v in by_entry.values() if v > 0)
        return {
            "total_pnl": self.lifetime_total_pnl(),       # equity-based (realized+unrealized)
            "realized_lifetime": self.lifetime_realized(),
            "total_trades": total,
            "win_rate": (wins / total * 100.0) if total else 0.0,
        }


def reconcile(headline_total: float, day_cell_values: list, tol: float = 0.01) -> tuple[bool, float]:
    """Page-coherence invariant: the headline must equal the sum of the day-cells it is built
    from. Returns (ok, drift). A breach means the page is mixing sources again and MUST render
    a visible RECONCILIATION ERROR banner instead of shipping silent, contradictory numbers.
    Compares sum-of-rounded-cells to the (rounded) headline, so honest float accumulation
    never trips a false error."""
    cells_sum = round(sum(float(v) for v in day_cell_values), 2)
    drift = round(abs(cells_sum - round(float(headline_total), 2)), 2)
    return (drift <= tol, drift)


def build_report_figures() -> ReportFigures:
    """Build the single canonical figures snapshot from ONE build_ledger() call. build_ledger
    does a live Alpaca fetch and can raise; on ANY failure this returns
    ReportFigures(available=False) so the caller keeps its last-good page (never blank/partial
    and never a silent fallback to a divergent source)."""
    ts = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    try:
        from reporting.pnl_ledger import build_ledger
        led = build_ledger()
    except Exception as exc:  # RC-3: logged, not swallowed — degrade to unavailable, don't crash the page
        logger.warning("build_report_figures: ledger unavailable (%s) — figures unavailable", exc)
        return ReportFigures(available=False, version="unavailable", ts_pt=ts)

    rts = led.get("round_trips", []) or []
    # net_deposits: fall back ONLY when the key is MISSING (None) — a legitimate 0.0
    # must be kept, never coalesced to the $2,500 fallback (else lifetime P&L = equity - 2500
    # instead of equity - 0). `x or d` would wrongly override a real zero (Gro 2026-08-20).
    _nd = led.get("net_deposits")
    by_exit: dict = defaultdict(list)
    for r in rts:
        by_exit[str(r.get("exit_date", ""))].append(r)

    return ReportFigures(
        available=True,
        version=uuid.uuid4().hex[:12],
        ts_pt=ts,
        round_trips=rts,
        equity=led.get("equity"),
        net_deposits=(_INITIAL_PAPER_EQUITY if _nd is None else float(_nd)),
        unrealized=float(led.get("unrealized") or 0.0),
        invariant=led.get("invariant") or {},
        unmatched_closes=led.get("unmatched_closes") or [],
        _by_exit_date=dict(by_exit),
    )
