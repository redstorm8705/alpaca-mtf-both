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
    missing_order_joins: list = field(default_factory=list)  # entry fills absent from order history; tier attribution is incomplete
    missing_close_identities: list = field(default_factory=list)  # realized close fills lacking both activity and order IDs
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

    def strategy_edge_stats(self) -> dict:
        """Lifecycle-level strategy evidence from this one Alpaca FIFO snapshot.

        Realized P&L includes every closed leg. Trade count, win rate, payoff, and profit factor
        include only fully closed entry lifecycles, so a profitable partial cannot masquerade as a
        winning completed trade while the residual remains open.
        """
        groups: dict[tuple, dict] = {}
        for r in self.round_trips:
            identity = r.get("entry_order_id") or r.get("entry_time")
            key = (r.get("symbol"), identity, r.get("direction"),
                   r.get("tier", "unattributed"))
            g = groups.setdefault(key, {
                "symbol": r.get("symbol"), "entry_time": r.get("entry_time"),
                "entry_order_id": r.get("entry_order_id", ""),
                "direction": r.get("direction"), "tier": r.get("tier", "unattributed"),
                "pnl": 0.0, "exit_time": "", "complete": True,
            })
            g["pnl"] += float(r.get("pnl", 0.0))
            g["exit_time"] = max(str(g["exit_time"] or ""), str(r.get("exit_time") or ""))
            g["complete"] = bool(g["complete"] and r.get("lifecycle_complete", True))

        completed = [g for g in groups.values() if g["complete"]]
        partial_open = [g for g in groups.values() if not g["complete"]]

        def _stats(rows: list, realized: float) -> dict:
            pnls = [float(x["pnl"]) for x in rows]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            gross_w = sum(wins)
            gross_l = abs(sum(losses))
            return {
                "realized_pnl": round(realized, 2),
                "completed_trades": len(rows),
                "wins": len(wins),
                "win_rate": round(len(wins) / len(rows) * 100.0, 1) if rows else None,
                "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
                "avg_win": round(gross_w / len(wins), 2) if wins else None,
                "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            }

        tiers = ("intraday", "daytrade", "qhm", "forever6", "unattributed")
        by_tier = {}
        for tier in tiers:
            tier_rows = [g for g in completed if g["tier"] == tier]
            tier_realized = sum(float(r.get("pnl", 0.0)) for r in self.round_trips
                                if r.get("tier", "unattributed") == tier)
            by_tier[tier] = _stats(tier_rows, tier_realized)

        # Cash-path drawdown uses atomic broker close fills. One sell can match several
        # FIFO lots; those legs share one execution and must be summed before measuring
        # the cash path, otherwise lot ordering fabricates an intra-fill drawdown.
        by_exit_fill: dict[tuple, float] = defaultdict(float)
        for r in self.round_trips:
            exit_key = r.get("exit_fill_id") or (
                r.get("exit_order_id"), r.get("exit_time"))
            by_exit_fill[exit_key] += float(r.get("pnl", 0.0))
        cash = peak = max_dd = 0.0
        exit_time_by_key = {
            (r.get("exit_fill_id") or (r.get("exit_order_id"), r.get("exit_time"))):
            str(r.get("exit_time") or "") for r in self.round_trips
        }
        for _atomic_key, pnl in sorted(
                by_exit_fill.items(), key=lambda item: exit_time_by_key[item[0]]):
            cash += pnl
            peak = max(peak, cash)
            max_dd = max(max_dd, peak - cash)

        same_day_minutes: list[float] = []
        multi_day_days: list[int] = []
        for g in completed:
            try:
                en = datetime.fromisoformat(str(g["entry_time"]).replace("Z", "+00:00"))
                ex = datetime.fromisoformat(str(g["exit_time"]).replace("Z", "+00:00"))
                if en.date() == ex.date():
                    same_day_minutes.append(max(0.0, (ex - en).total_seconds() / 60.0))
                else:
                    multi_day_days.append(max(1, (ex.date() - en.date()).days))
            except (TypeError, ValueError):
                continue

        overall = _stats(completed, self.lifetime_realized())
        overall.update({
            "partial_open_lifecycles": len(partial_open),
            "partial_open_realized_pnl": round(sum(float(x["pnl"]) for x in partial_open), 2),
            "max_realized_drawdown": round(max_dd, 2),
            "avg_same_day_hold_minutes": (
                round(sum(same_day_minutes) / len(same_day_minutes)) if same_day_minutes else None),
            "avg_multi_day_hold_days": (
                round(sum(multi_day_days) / len(multi_day_days), 1) if multi_day_days else None),
        })
        return {"overall": overall, "by_tier": by_tier, "completed": completed,
                "partial_open": partial_open}


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
        missing_order_joins=led.get("missing_order_joins") or [],
        missing_close_identities=led.get("missing_close_identities") or [],
        _by_exit_date=dict(by_exit),
    )
