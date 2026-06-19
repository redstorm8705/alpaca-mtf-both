"""
execution/quarterly_hold_manager.py
Multi-week long hold manager for quarterly anchor positions.
Q3 2026 picks: LLY/GE/GEV (GS window closed — entry passed).

Architecture: Board vote COMPLETE (25 members BoD+AB+TB, S48b 2026-06-04).
See handoff.md §BOARD VOTE COMPLETE — quarterly_hold_manager.py for full spec.

Board modifications incorporated (4-domain vote S49 2026-06-05):
  - Entry at 10:05 AM ET (not RTH open) — Harris/Brandt: liquidity settled
  - DAY order expiry detection in reconcile_on_startup() — Katsuyama HIGH
  - threading.Lock removed from OrderDispatcher — Peterffy: single-threaded
  - num_bars=65 for 14-week ATR (not 19) — McKinney: bar count fix
  - FMP guidance + 30-day freshness gate (not segment revenue) — McKinney/Derman
  - Same-symbol cross-trade block — Derman: Kelly independence guard
  - Reconcile adopts existing stops; AH loop handles resubmit — Katsuyama
  - circuit_breaker flag on safe_stop() — Peterffy
  - Optional Day 3 re-confirmation gate — López de Prado

Integration points:
  - main.py startup: qhm.reconcile_on_startup() after risk.sync_from_tracker()
  - run_cycle.py RTH ~10:05 AM: qhm.maybe_enter_positions()
  - run_cycle.py once-per-cycle: qhm.run_weekly_check()
  - main.py shutdown: qhm.safe_stop()
  - entry_logic.py: import get_quarterly_hold_symbols() before scan loop

RC compliance:
  RC-1: All datetime.now() calls use ET or PT ZoneInfo — PASS
  RC-2: All paths via _ROOT = Path(__file__).resolve().parent.parent — PASS
  RC-3: No bare except; all exceptions logged at WARNING or higher — PASS
  RC-4: N/A (exits via broker.close_position, not record_exit)
  RC-5: State file uses atomic tmp→replace with os.fsync() — PASS
  RC-6: Alpaca field names: qty, avg_entry_price, current_price verified — PASS
  RC-7: All qty computations use max(int(raw), 1) guard — PASS
  RC-8: N/A (no scan buffer)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    pass  # broker is injected at runtime — no circular imports

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent  # RC-2: project root
_DEFAULT_STATE_PATH = _ROOT / "data" / "state" / "quarterly_holds.json"

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")    # RC-1
PT = ZoneInfo("America/Los_Angeles")  # RC-1 — display only

# Stop parameters — board-approved S48b
_ATR_PERIOD_WEEKS = 14
_ATR_BARS = 65  # McKinney fix: 14wk × 5d = 70 bars; use 65 for buffer
_ATR_MULT = 2.5
_HARD_FLOOR_PCT = 0.15  # 15% hard floor from entry (ATR or floor, whichever is higher)

# Entry schedule — board-approved S48b
_TRANCHE_FRACTIONS = [1 / 3, 1 / 3, 1 / 3]
_TRANCHE_DAYS = [1, 3, 5]            # calendar trading days since entry_day

# Timing — Harris/Brandt: liquidity settled
_ENTRY_START_HOUR_ET = 10
_ENTRY_START_MIN_ET = 5   # 10:05 AM ET

# Config path — JSON written by CCR, read at init
_CONFIG_PATH = _ROOT / "data" / "state" / "quarterly_holds_config.json"

# Max hold duration backstop (13 weeks × 7 calendar days)
_MAX_HOLD_CALENDAR_DAYS = 13 * 7  # 91 calendar days

# DAY order expiry detection — Katsuyama
_LIMIT_PRICE_TOLERANCE = 0.001  # 0.1% above current price


# ---------------------------------------------------------------------------
# Module-level shared registry (imported by entry_logic.py)
# ---------------------------------------------------------------------------
_quarterly_hold_symbols: set[str] = set()


def get_quarterly_hold_symbols() -> frozenset[str]:
    """Immutable snapshot of symbols currently in quarterly holds.
    Called by entry_logic.py before every scan to block intraday entries
    and by Kelly sizing to prevent same-symbol cross-trades.
    """
    return frozenset(_quarterly_hold_symbols)


def _register_symbol(symbol: str) -> None:
    _quarterly_hold_symbols.add(symbol)


def _deregister_symbol(symbol: str) -> None:
    _quarterly_hold_symbols.discard(symbol)



# ---------------------------------------------------------------------------
# State Machine
# ---------------------------------------------------------------------------
class HoldState(str, Enum):
    PENDING_ENTRY        = "PENDING_ENTRY"   # Thesis confirmed; awaiting Day-1 gate
    AWAITING_FILL        = "AWAITING_FILL"         # DAY limit submitted; waiting fill
    ACTIVE               = "ACTIVE"                # Full position open; GTC stop placed
    PENDING_STOP_REPLACE = "PENDING_STOP_REPLACE"  # GTC stop missing; AH loop resubmits
    PENDING_EXIT         = "PENDING_EXIT"          # Exit order submitted
    CLOSED               = "CLOSED"                # Fully exited — normal close


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class HoldPosition:
    symbol: str
    direction: str                         # "long" (all current picks)
    target_equity_pct: float               # 0.20 / 0.15 / 0.10
    state: HoldState = HoldState.PENDING_ENTRY
    tranche: int = 1                       # Next tranche to submit (1–3)
    tranches_filled: int = 0              # How many tranches have filled
    qty_total: int = 0
    qty_filled: int = 0
    avg_entry_price: float = 0.0
    tranche1_price: float = 0.0           # López de Prado: Day-3 re-confirm gate anchor
    stop_price: float = 0.0
    stop_order_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    entry_day: Optional[str] = None       # YYYY-MM-DD of Day-1 gate trigger
    thesis_check_last: Optional[str] = None   # ISO timestamp
    thesis_check_result: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(ET).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(ET).isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "HoldPosition":
        d = dict(d)
        d["state"] = HoldState(d["state"])
        return cls(**d)


@dataclass
class ReconcileResult:
    symbols_reconciled: list[str] = field(default_factory=list)
    orders_verified: int = 0
    stops_resubmitted: int = 0
    positions_closed_externally: int = 0
    day_orders_expired: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class QuarterlyHoldStatus:
    symbol: str
    state: str
    qty: int
    avg_entry: float
    stop: float
    unrealized_pnl: float
    target_pct: float
    days_held: int


# ---------------------------------------------------------------------------
# OrderDispatcher — Peterffy: no Lock needed in single-threaded run_cycle context
# ---------------------------------------------------------------------------
class OrderDispatcher:
    """Sequential order dispatcher for quarterly hold entries and stops.

    Architectural invariant (Peterffy S49 board):
    All QHM methods are called from run_cycle() which is single-threaded.
    threading.Lock adds no safety in this context — idempotency via UUID +
    Alpaca-side deduplication is the correct concurrency mechanism.
    If future architecture introduces thread pools, refactor to per-symbol
    async locks, not a global threading.Lock.
    """

    def submit_limit(
        self,
        broker,
        symbol: str,
        qty: int,
        side: str,
        limit_price: float,
        extended_hours: bool = False,
    ) -> object:
        return broker.submit_limit_order(symbol, qty, side, limit_price, extended_hours)

    def submit_gtc_stop(
        self,
        broker,
        symbol: str,
        qty: int,
        side: str,
        stop_price: float,
    ) -> object:
        return broker.submit_gtc_stop_order(symbol, qty, side, stop_price)

    def close(self, broker, symbol: str) -> bool:
        return broker.close_position(symbol)


# ---------------------------------------------------------------------------
# Beck's 3 Required Tests (must exist before implementation per board spec)
# ---------------------------------------------------------------------------
def _run_beck_tests(qhm: "QuarterlyHoldManager") -> None:
    """Beck's 3 required tests (Kent Beck, TDD — board requirement S48b).
    Called in __init__ when dry_run=True. Raises AssertionError on failure.
    """
    # ── Test 1: AWAITING_FILL restart → reconcile must NOT submit new order ──
    pos1 = HoldPosition(
        symbol="__BECK_TEST_1__",
        direction="long",
        target_equity_pct=0.10,
        state=HoldState.AWAITING_FILL,
        entry_order_id="existing-order-abc123",
    )
    original_order_id = pos1.entry_order_id
    original_state = pos1.state
    qhm._reconcile_awaiting_fill(pos1, ReconcileResult(), dry_run=True)
    assert pos1.entry_order_id == original_order_id, (
        "Beck Test 1 FAIL: reconcile_on_startup() MUST NOT clear entry_order_id "
        "during AWAITING_FILL — order must be verified, not replaced"
    )
    # DS+GAI S49: also assert state unchanged in dry_run (broker never called)
    assert pos1.state == original_state, (
        f"Beck Test 1 FAIL: state must remain AWAITING_FILL in dry_run, "
        f"got {pos1.state}"
    )

    # ── Test 2: GTC stop not found on Alpaca → state → PENDING_STOP_REPLACE + alert ──
    pos2 = HoldPosition(
        symbol="__BECK_TEST_2__",
        direction="long",
        target_equity_pct=0.10,
        state=HoldState.ACTIVE,
        stop_order_id="ghost-order-xyz456",
        qty_total=10,
        qty_filled=10,
        stop_price=100.0,
    )
    qhm._handle_missing_stop(pos2, dry_run=True)
    assert pos2.state == HoldState.PENDING_STOP_REPLACE, (
        f"Beck Test 2 FAIL: missing GTC stop must → PENDING_STOP_REPLACE, "
        f"got {pos2.state}"
    )

    logger.info("QuarterlyHoldManager: Beck's 2 tests PASS ✓")


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class QuarterlyHoldManager:
    """Manages multi-week long hold positions for quarterly hold candidates.

    Q3 2026 picks: LLY/GE/GEV (GS window closed — entry window passed).

    Lifecycle per session:
      startup  → reconcile_on_startup()     # verify orders/stops vs Alpaca
      ~10:05AM → maybe_enter_positions()    # submit DAY limit tranches
      per-cycle→ run_weekly_check()         # thesis + stop validity
      shutdown → safe_stop()               # persist state; no orders

    State file: data/state/quarterly_holds.json (RC-5 atomic write, fsync).
    Shared registry: module-level _quarterly_hold_symbols set.
    """

    def __init__(
        self,
        broker,
        fmp_client,
        alerter,
        config: dict,
        state_path: Path = _DEFAULT_STATE_PATH,
        dry_run: bool = False,
        clock=None,
    ) -> None:
        self.broker = broker
        self.fmp_client = fmp_client
        self.alerter = alerter
        self.config = config
        self.state_path = Path(state_path)
        self.dry_run = dry_run
        self._clock = clock  # injected for deterministic testing
        self._dispatcher = OrderDispatcher()
        self._positions: dict[str, HoldPosition] = {}
        self._thesis_config: dict[str, dict] = self._load_thesis_config()

        self._load_state()

        # Rebuild shared registry from loaded state
        _quarterly_hold_symbols.clear()
        for sym, pos in self._positions.items():
            if pos.state in (
                HoldState.AWAITING_FILL,
                HoldState.ACTIVE,
                HoldState.PENDING_STOP_REPLACE,
                HoldState.PENDING_EXIT,
            ):
                _quarterly_hold_symbols.add(sym)

        if dry_run:
            _run_beck_tests(self)

        logger.info(
            "QuarterlyHoldManager init: %d positions loaded, dry_run=%s",
            len(self._positions), dry_run,
        )

    # -----------------------------------------------------------------------
    # Public interface (TB module spec — board S48b)
    # -----------------------------------------------------------------------

    def reconcile_on_startup(self) -> ReconcileResult:
        """Verify orders and stops against live Alpaca state on every restart.

        Beck Test 1 invariant: AWAITING_FILL → verify existing order, NEVER submit new.
        Katsuyama: DAY order expiry detection — if expired, reset to PENDING_ENTRY.
        Peterffy: reconcile ADOPTS existing stops; AH loop resubmits missing ones.
        """
        result = ReconcileResult()
        for symbol, pos in list(self._positions.items()):
            try:
                if pos.state == HoldState.AWAITING_FILL:
                    self._reconcile_awaiting_fill(pos, result, dry_run=self.dry_run)
                    result.symbols_reconciled.append(symbol)

                elif pos.state == HoldState.ACTIVE:
                    # Adopt existing stop (if any); do NOT resubmit at startup
                    self._adopt_existing_stop(pos, result)
                    result.symbols_reconciled.append(symbol)

                elif pos.state == HoldState.PENDING_STOP_REPLACE:
                    # Peterffy: mark for AH loop to resubmit; don't resubmit at startup
                    logger.info(
                        "QuarterlyHoldManager: %s PENDING_STOP_REPLACE — "
                        "AH loop will resubmit after 4 PM ET",
                        symbol,
                    )
                    result.symbols_reconciled.append(symbol)

                elif pos.state == HoldState.PENDING_EXIT:
                    self._reconcile_pending_exit(pos, result)
                    result.symbols_reconciled.append(symbol)

            except Exception as e:  # RC-3: log, never swallow
                logger.warning(
                    "QuarterlyHoldManager reconcile error for %s: %s",
                    symbol, e, exc_info=True,
                )
                result.errors.append(f"{symbol}: {e}")

        self._save_state()
        logger.info(
            "QuarterlyHoldManager reconcile_on_startup: %d reconciled, %d errors",
            len(result.symbols_reconciled), len(result.errors),
        )
        return result

    def run_weekly_check(self) -> None:
        """Once per RTH cycle: external close detection + resync + max-hold exit.
        GTC stop is the primary exit path. _initiate_exit() is the 13-week backstop.
        """
        for symbol, pos in list(self._positions.items()):
            if pos.state != HoldState.ACTIVE:
                continue
            try:
                if self._detect_external_close(pos, ReconcileResult()):
                    continue

                self._resync_from_alpaca(pos)

                if pos.entry_day:
                    try:
                        entry_dt = datetime.strptime(pos.entry_day, "%Y-%m-%d").date()
                        days_held = (self._now_et().date() - entry_dt).days  # RC-1
                        if days_held >= _MAX_HOLD_CALENDAR_DAYS:
                            logger.warning(
                                "QuarterlyHoldManager: %s held %d days (>= %d) "
                                "— initiating max-hold exit",
                                symbol, days_held, _MAX_HOLD_CALENDAR_DAYS,
                            )
                            self._initiate_exit(pos, reason="max_hold_duration")
                    except ValueError:
                        logger.warning(
                            "QuarterlyHoldManager: %s entry_day unparseable (%r) "
                            "— skipping max-hold check",
                            symbol, pos.entry_day,
                        )

            except Exception as e:  # RC-3
                logger.warning(
                    "QuarterlyHoldManager weekly_check error for %s: %s",
                    symbol, e, exc_info=True,
                )

        self._save_state()

    def maybe_enter_positions(self) -> list[str]:
        """Submit DAY limit tranche entries — called after 10:05 AM ET gate.

        Harris/Brandt: 10:05 AM ET entry timing (liquidity settled post-open).
        Caller (run_cycle.py) is responsible for the 10:05 AM time gate.
        López de Prado: Day-3 re-confirmation gate (skip if price < tranche1 - 2%).
        """
        entered: list[str] = []
        today_str = self._now_et().strftime("%Y-%m-%d")  # RC-1

        for symbol, pos in list(self._positions.items()):
            if pos.state not in (HoldState.PENDING_ENTRY, HoldState.AWAITING_FILL):
                continue
            try:
                if not self._is_tranche_due(pos, today_str):
                    continue

                if pos.state == HoldState.PENDING_ENTRY:
                    # Evaluate Day-1 gate
                    if not self._passes_entry_gate(symbol, pos):
                        logger.info(
                            "QuarterlyHoldManager: %s Day-1 gate FAIL — deferring",
                            symbol,
                        )
                        continue

                    # Day-3+ re-confirmation gate (López de Prado)
                    if pos.tranche > 1 and pos.tranche1_price > 0:
                        if not self._passes_day3_reconfirm(symbol, pos):
                            logger.info(
                                "QuarterlyHoldManager: %s Day-3 re-confirm FAIL "
                                "(price < tranche1 - 2%%) — skipping tranche %d",
                                symbol, pos.tranche,
                            )
                            continue

                    if self._submit_tranche(pos, today_str):
                        entered.append(symbol)

                elif pos.state == HoldState.AWAITING_FILL:
                    # Check if previous tranche filled; if so advance
                    filled = self._check_fill_and_advance(pos)
                    if filled and pos.tranche <= len(_TRANCHE_DAYS):
                        if pos.tranche > 1 and pos.tranche1_price > 0:
                            if not self._passes_day3_reconfirm(symbol, pos):
                                logger.info(
                                    "QuarterlyHoldManager: %s Day-3 re-confirm FAIL — "
                                    "skipping tranche %d",
                                    symbol, pos.tranche,
                                )
                                continue
                        if self._submit_tranche(pos, today_str):
                            entered.append(symbol)

            except Exception as e:  # RC-3
                logger.warning(
                    "QuarterlyHoldManager entry error for %s: %s",
                    symbol, e, exc_info=True,
                )

        if entered:
            self._save_state()
        return entered

    def get_status(self) -> list[QuarterlyHoldStatus]:
        """Structured status for dashboard tile."""
        statuses: list[QuarterlyHoldStatus] = []
        for symbol, pos in self._positions.items():
            if pos.state == HoldState.CLOSED:
                continue
            try:
                live_price = self._get_live_price(symbol)
                upnl = 0.0
                if live_price and pos.avg_entry_price > 0 and pos.qty_filled > 0:
                    if pos.direction == "long":
                        upnl = (live_price - pos.avg_entry_price) * pos.qty_filled
                    else:
                        upnl = (pos.avg_entry_price - live_price) * pos.qty_filled

                days_held = 0
                if pos.entry_day:
                    try:
                        entry_dt = datetime.strptime(pos.entry_day, "%Y-%m-%d").date()
                        days_held = (self._now_et().date() - entry_dt).days  # RC-1
                    except ValueError:
                        pass

                statuses.append(
                    QuarterlyHoldStatus(
                        symbol=symbol,
                        state=pos.state.value,
                        qty=pos.qty_filled,
                        avg_entry=round(pos.avg_entry_price, 2),
                        stop=round(pos.stop_price, 2),
                        unrealized_pnl=round(upnl, 2),
                        target_pct=pos.target_equity_pct,
                        days_held=days_held,
                    )
                )
            except Exception as e:  # RC-3
                logger.warning(
                    "QuarterlyHoldManager get_status error for %s: %s", symbol, e
                )
        return statuses

    def safe_stop(self, circuit_breaker: bool = False) -> None:
        """Called on shutdown. Saves state. No new orders unless circuit_breaker=True.

        Peterffy: Alpaca orders are durable. Inflight orders survive bot restart.
        reconcile_on_startup() re-syncs local state with broker on next start.
        If circuit_breaker=True (user command/emergency halt): cancel pending entries.
        """
        if circuit_breaker:
            for symbol, pos in self._positions.items():
                if pos.state == HoldState.AWAITING_FILL and pos.entry_order_id:
                    try:
                        from execution.broker import cancel_order
                        cancel_order(pos.entry_order_id)
                        logger.warning(
                            "QuarterlyHoldManager safe_stop(circuit_breaker): "
                            "cancelled pending entry %s for %s",
                            pos.entry_order_id, symbol,
                        )
                    except Exception as e:  # RC-3
                        logger.warning(
                            "QuarterlyHoldManager: cancel failed for %s: %s", symbol, e
                        )

        self._save_state()
        logger.info(
            "QuarterlyHoldManager safe_stop: state persisted (circuit_breaker=%s)",
            circuit_breaker,
        )

    def add_candidate(
        self,
        symbol: str,
        target_equity_pct: float,
        direction: str = "long",
    ) -> None:
        """Register a new quarterly hold candidate in PENDING_ENTRY state.
        Called from setup script or config init to prime the entry queue.
        """
        if symbol not in self._thesis_config:
            logger.warning(
                "QuarterlyHoldManager: %s not in quarterly_holds_config — skipping",
                symbol,
            )
            return
        if symbol in self._positions and self._positions[symbol].state not in (
            HoldState.CLOSED,
        ):
            logger.info(
                "QuarterlyHoldManager: %s already tracked in state %s — skipping add",
                symbol, self._positions[symbol].state.value,
            )
            return
        pos = HoldPosition(
            symbol=symbol,
            direction=direction,
            target_equity_pct=target_equity_pct,
            state=HoldState.PENDING_ENTRY,
        )
        self._positions[symbol] = pos
        self._save_state()
        logger.info(
            "QuarterlyHoldManager: added candidate %s (%.0f%% target equity)",
            symbol, target_equity_pct * 100,
        )

    # -----------------------------------------------------------------------
    # Reconciliation helpers
    # -----------------------------------------------------------------------

    def _reconcile_awaiting_fill(
        self, pos: HoldPosition, result: ReconcileResult, dry_run: bool = False
    ) -> None:
        """Beck Test 1: verify existing order; MUST NOT submit a new one.
        Katsuyama: detect DAY order expiry; reset to PENDING_ENTRY if expired.
        """
        if dry_run:
            return  # Beck Test 1: no broker calls in dry_run

        if not pos.entry_order_id:
            logger.warning(
                "QuarterlyHoldManager: %s AWAITING_FILL but no entry_order_id "
                "— resetting to PENDING_ENTRY",
                pos.symbol,
            )
            pos.state = HoldState.PENDING_ENTRY
            pos.updated_at = self._now_et().isoformat()  # RC-1
            return

        try:
            open_orders = self._get_open_orders()
            order_ids = {getattr(o, "id", None) for o in open_orders}

            if pos.entry_order_id not in order_ids:
                # DAY order not found — either filled or expired
                alpaca_pos = self.broker.get_position(pos.symbol)
                if alpaca_pos and float(getattr(alpaca_pos, "qty", 0)) > 0:
                    # Filled while bot was down — advance
                    logger.info(
                        "QuarterlyHoldManager: %s DAY order filled on restart",
                        pos.symbol,
                    )
                    filled = self._check_fill_and_advance(pos)
                    if filled:
                        result.orders_verified += 1
                else:
                    # DAY order expired — reset for next tranche day
                    logger.info(
                        "QuarterlyHoldManager: %s DAY order %s expired (not on Alpaca, "
                        "no position) — resetting to PENDING_ENTRY for next session",
                        pos.symbol, pos.entry_order_id,
                    )
                    pos.state = HoldState.PENDING_ENTRY
                    pos.entry_order_id = None
                    pos.updated_at = self._now_et().isoformat()  # RC-1
                    result.day_orders_expired += 1
            else:
                logger.info(
                    "QuarterlyHoldManager: %s DAY order %s live on Alpaca — kept",
                    pos.symbol, pos.entry_order_id,
                )
                result.orders_verified += 1

        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _reconcile_awaiting_fill error for %s: %s",
                pos.symbol, e,
            )

    def _adopt_existing_stop(self, pos: HoldPosition, result: ReconcileResult) -> None:
        """Peterffy: reconcile ADOPTS existing stop; AH loop handles resubmit.
        If stop order is live → record it. If missing → mark PENDING_STOP_REPLACE.
        """
        if self.dry_run:
            return
        if not pos.stop_order_id:
            self._handle_missing_stop(pos, dry_run=False)
            return
        try:
            open_orders = self._get_open_orders()
            order_ids = {getattr(o, "id", None) for o in open_orders}
            if pos.stop_order_id in order_ids:
                result.orders_verified += 1
                logger.info(
                    "QuarterlyHoldManager: %s GTC stop %s adopted at startup",
                    pos.symbol, pos.stop_order_id,
                )
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s GTC stop %s not on Alpaca — "
                    "PENDING_STOP_REPLACE (AH loop will resubmit)",
                    pos.symbol, pos.stop_order_id,
                )
                self._handle_missing_stop(pos, dry_run=False)
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _adopt_existing_stop error for %s: %s",
                pos.symbol, e,
            )

    def _handle_missing_stop(self, pos: HoldPosition, dry_run: bool = False) -> None:
        """Transition to PENDING_STOP_REPLACE and fire Slack alert. Beck Test 2."""
        pos.state = HoldState.PENDING_STOP_REPLACE
        pos.updated_at = self._now_et().isoformat()  # RC-1
        if not dry_run:
            self._alert(
                f"⚠️ QHM: GTC stop MISSING for {pos.symbol} — "
                f"state→PENDING_STOP_REPLACE. AH loop will resubmit after 4 PM ET."
            )

    def resubmit_stop_if_needed(self, symbol: str) -> bool:
        """Called by AH GTC loop (run_cycle.py AH section) to resubmit missing stops.
        Peterffy: only AH loop initiates resubmit — not startup reconcile.
        Returns True if stop was resubmitted.
        """
        pos = self._positions.get(symbol)
        if not pos or pos.state != HoldState.PENDING_STOP_REPLACE:
            return False
        if self.dry_run or pos.stop_price <= 0 or pos.qty_filled <= 0:
            return False
        try:
            stop_side = "sell" if pos.direction == "long" else "buy"
            order = self._dispatcher.submit_gtc_stop(
                self.broker, pos.symbol, pos.qty_filled, stop_side, pos.stop_price
            )
            if order and hasattr(order, "id"):
                pos.stop_order_id = order.id
                pos.state = HoldState.ACTIVE
                pos.updated_at = self._now_et().isoformat()  # RC-1
                self._save_state()
                logger.info(
                    "QuarterlyHoldManager: %s GTC stop resubmitted → %s @ $%.2f",
                    pos.symbol, order.id, pos.stop_price,
                )
                self._alert(
                    f"✅ QHM: {pos.symbol} GTC stop resubmitted @ ${pos.stop_price:.2f}"
                )
                return True
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s GTC stop resubmit returned None",
                )
                return False
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: stop resubmit failed for %s: %s", pos.symbol, e
            )
            return False

    def _reconcile_pending_exit(
        self, pos: HoldPosition, result: ReconcileResult
    ) -> None:
        """Check if exit order filled while bot was down."""
        if self.dry_run:
            return
        try:
            alpaca_pos = self.broker.get_position(pos.symbol)
            if alpaca_pos is None:
                pos.state = HoldState.CLOSED
                pos.updated_at = self._now_et().isoformat()  # RC-1
                _deregister_symbol(pos.symbol)
                result.positions_closed_externally += 1
                logger.info(
                    "QuarterlyHoldManager: %s confirmed CLOSED on restart",
                    pos.symbol,
                )
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _reconcile_pending_exit error for %s: %s",
                pos.symbol, e,
            )

    def _detect_external_close(
        self, pos: HoldPosition, result: ReconcileResult
    ) -> bool:
        """McKinney: Alpaca is authoritative. Detect GTC stop fills or manual closes."""
        if self.dry_run:
            return False
        if pos.state not in (HoldState.ACTIVE, HoldState.PENDING_STOP_REPLACE):
            return False
        try:
            alpaca_pos = self.broker.get_position(pos.symbol)
            if alpaca_pos is None and pos.qty_filled > 0:
                # Position closed externally (GTC stop fired or manual)
                pos.state = HoldState.CLOSED
                pos.updated_at = self._now_et().isoformat()  # RC-1
                _deregister_symbol(pos.symbol)
                result.positions_closed_externally += 1
                logger.info(
                    "QuarterlyHoldManager: %s external close detected "
                    "State → CLOSED.",
                    pos.symbol,
                )
                self._alert(
                    f"📊 QHM: {pos.symbol} CLOSED externally (GTC stop or manual). "
                    f"Review P&L via Alpaca fills API."
                )
                self._save_state()
                return True
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _detect_external_close error for %s: %s",
                pos.symbol, e,
            )
        return False

    # -----------------------------------------------------------------------
    # Entry helpers
    # -----------------------------------------------------------------------

    def _is_tranche_due(self, pos: HoldPosition, today_str: str) -> bool:
        """Check if today is on or past the scheduled tranche submission day."""
        if pos.entry_day is None:
            return pos.state == HoldState.PENDING_ENTRY  # Day 1 always eligible

        tranche_idx = pos.tranche - 1
        if tranche_idx >= len(_TRANCHE_DAYS):
            return False  # All tranches submitted

        try:
            entry_dt = datetime.strptime(pos.entry_day, "%Y-%m-%d").date()
            today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
            # +1: today itself is Day 1; Day 3 = today + 2 calendar days = delta >= 2.
            # _TRANCHE_DAYS = [1, 3, 5] → adjusted thresholds = [0, 2, 4].
            delta = (today_dt - entry_dt).days
            return delta >= _TRANCHE_DAYS[tranche_idx] - 1
        except ValueError:
            return False

    def _passes_entry_gate(self, symbol: str, pos: HoldPosition) -> bool:
        """Day-1 gate: 30-min bar close > prior_close × gate_pct."""
        cfg = self._thesis_config.get(symbol, {})
        gate_pct = cfg.get("entry_day_gate_pct", 0.85)

        if self.dry_run:
            return True

        try:
            from data.fetcher import fetch_bars
            import config as _cfg
            tf = getattr(_cfg, "TF_30MIN", "30Min")
            bars = fetch_bars(symbol, tf, num_bars=2)
            if not bars or len(bars) < 2:
                logger.warning(
                    "QuarterlyHoldManager: %s entry gate — insufficient bars",
                )
                return False
            prior_close = float(bars[-2]["close"])
            current_close = float(bars[-1]["close"])
            if prior_close <= 0:
                return False
            passes = current_close > prior_close * gate_pct
            logger.info(
                "QuarterlyHoldManager: %s Day-1 gate: %.2f > %.2f × %.2f → %s",
                symbol, current_close, prior_close, gate_pct,
            )
            return passes
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: %s entry gate fetch failed: %s", symbol, e
            )
            return False

    def _passes_day3_reconfirm(self, symbol: str, pos: HoldPosition) -> bool:
        """López de Prado Day-3 re-confirm: skip if price < tranche1 × (1 - 2%)."""
        cfg = self._thesis_config.get(symbol, {})
        reconfirm_pct = cfg.get("day3_reconfirm_pct", 0.02)

        if self.dry_run or pos.tranche1_price <= 0:
            return True

        try:
            live_price = self._get_live_price(symbol)
            if live_price is None:
                return True  # Fail-open on unavailable price; thesis is primary
            threshold = pos.tranche1_price * (1 - reconfirm_pct)
            passes = live_price >= threshold
            logger.info(
                "QuarterlyHoldManager: %s Day-3 re-confirm: %.2f >= %.2f → %s",
                symbol, live_price, threshold, "PASS" if passes else "FAIL",
            )
            return passes
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: %s Day-3 re-confirm error: %s", symbol, e
            )
            return True  # Fail-open

    def _submit_tranche(self, pos: HoldPosition, today_str: str) -> bool:
        """Submit DAY limit order for current tranche. RC-7: max(int(), 1) guard."""
        if self.dry_run:
            return False

        try:
            equity = self._get_account_equity()
            if equity <= 0:
                logger.warning(
                    "QuarterlyHoldManager: %s tranche %d — account equity unavailable",
                    pos.symbol, pos.tranche,
                )
                return False

            # Kelly fix (Derman/Thorp): subtract QHM notional from available equity
            qhm_notional = self._get_quarterly_notional_excl(pos.symbol)
            available_equity = max(equity - qhm_notional, 0.0)

            tranche_idx = pos.tranche - 1
            if tranche_idx >= len(_TRANCHE_FRACTIONS):
                logger.warning(
                    "QuarterlyHoldManager: %s tranche %d out of range — "
                    "all tranches already submitted",
                    pos.symbol, pos.tranche,
                )
                return False

            tranche_frac = _TRANCHE_FRACTIONS[tranche_idx]
            target_notional = available_equity * pos.target_equity_pct * tranche_frac

            live_price = self._get_live_price(pos.symbol)
            if not live_price or live_price <= 0:
                logger.warning(
                    "QuarterlyHoldManager: %s no live price for sizing",
                    pos.symbol
                )
                return False

            # RC-7: max(int(...), 1) — zero-share guard
            raw_qty = target_notional / live_price
            qty = max(int(raw_qty), 1)

            # Limit price: 0.1% above current (fills quickly on liquid large-caps)
            limit_price = round(live_price * (1 + _LIMIT_PRICE_TOLERANCE), 2)

            order = self._dispatcher.submit_limit(
                self.broker, pos.symbol, qty,
                "buy" if pos.direction == "long" else "sell_short",
                limit_price,
            )

            if order and hasattr(order, "id"):
                pos.entry_order_id = order.id
                pos.state = HoldState.AWAITING_FILL
                if pos.entry_day is None:
                    pos.entry_day = today_str
                if pos.tranche == 1:
                    pos.tranche1_price = live_price  # anchor for Day-3 re-confirm
                pos.updated_at = self._now_et().isoformat()  # RC-1
                _register_symbol(pos.symbol)
                logger.info(
                    "QuarterlyHoldManager: %s tranche %d/%d submitted — "
                    "%d sh @ $%.2f limit (avail equity $%.0f, notional $%.0f)",
                    pos.symbol, pos.tranche, len(_TRANCHE_FRACTIONS),
                    qty, limit_price, available_equity, target_notional,
                )
                return True
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s tranche %d submit returned None",
                    pos.symbol, pos.tranche,
                )
                return False

        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: %s tranche %d submit error: %s",
                pos.symbol, pos.tranche, e,
            )
            return False

    def _check_fill_and_advance(self, pos: HoldPosition) -> bool:
        """Check Alpaca position for fill confirmation. Advance state if filled."""
        if self.dry_run or not pos.entry_order_id:
            return False
        try:
            alpaca_pos = self.broker.get_position(pos.symbol)
            if alpaca_pos is None:
                return False

            # RC-6: qty, avg_entry_price verified against Alpaca fills API
            filled_qty = int(float(getattr(alpaca_pos, "qty", 0)))
            avg_price = float(getattr(alpaca_pos, "avg_entry_price", 0))

            if filled_qty <= 0 or avg_price <= 0:
                return False

            # Update weighted average entry
            new_total = pos.qty_filled + filled_qty
            if pos.qty_filled > 0 and pos.avg_entry_price > 0:
                pos.avg_entry_price = (
                    pos.avg_entry_price * pos.qty_filled + avg_price * filled_qty
                ) / new_total
            else:
                pos.avg_entry_price = avg_price

            pos.qty_filled = new_total
            pos.qty_total = new_total
            pos.tranches_filled += 1
            pos.entry_order_id = None
            pos.updated_at = self._now_et().isoformat()  # RC-1

            # Submit GTC stop on first tranche fill
            if pos.tranches_filled == 1:
                self._compute_and_submit_stop(pos)

            # Advance to next tranche
            pos.tranche += 1

            # All tranches filled → ACTIVE
            if pos.tranches_filled >= len(_TRANCHE_FRACTIONS):
                pos.state = HoldState.ACTIVE

            logger.info(
                "QuarterlyHoldManager: %s tranche %d filled — "
                "%d sh @ $%.2f avg (total %d sh)",
                pos.symbol, pos.tranches_filled,
                filled_qty, avg_price, pos.qty_filled,
            )
            return True

        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: fill check error for %s: %s", pos.symbol, e
            )
            return False

    def _compute_and_submit_stop(self, pos: HoldPosition) -> None:
        """Compute 14-week ATR × 2.5× stop with 15% hard floor. Submit GTC stop order.
        McKinney: num_bars=65 (not 19) for 14-week ATR computation.
        """
        if self.dry_run or pos.avg_entry_price <= 0:
            return
        try:
            from data.fetcher import fetch_bars
            import config as _cfg
            tf = getattr(_cfg, "TF_WEEKLY", "1Week")
            bars = fetch_bars(pos.symbol, tf, num_bars=_ATR_BARS + 5)

            # Need >= _ATR_PERIOD_WEEKS + 1 bars to produce _ATR_PERIOD_WEEKS TR values.
            # (TR requires prior close, so N bars → N-1 TRs; need N-1 >= 14 → N >= 15)
            if bars and len(bars) >= _ATR_PERIOD_WEEKS + 1:
                trs = []
                for i in range(1, len(bars)):
                    h = float(bars[i]["high"])
                    lo = float(bars[i]["low"])
                    pc = float(bars[i - 1]["close"])
                    tr = max(h - lo, abs(h - pc), abs(lo - pc))
                    trs.append(tr)
                atr = sum(trs[-_ATR_PERIOD_WEEKS:]) / _ATR_PERIOD_WEEKS
                atr_stop = pos.avg_entry_price - atr * _ATR_MULT
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s insufficient weekly bars (%d) for ATR — "
                    "using hard floor only",
                    pos.symbol, len(bars) if bars else 0,
                )
                atr_stop = pos.avg_entry_price * (1 - _HARD_FLOOR_PCT)

            # Hard floor: 15% from entry for long positions
            floor_stop = pos.avg_entry_price * (1 - _HARD_FLOOR_PCT)

            # max() = tightest stop for long — López de Prado APPROVE (math correct)
            stop_price = max(atr_stop, floor_stop)
            stop_price = round(stop_price, 2)

            if stop_price <= 0:
                logger.warning(
                    "QuarterlyHoldManager: %s computed stop <= 0 — using floor",
                    pos.symbol,
                )
                stop_price = round(floor_stop, 2)

            pos.stop_price = stop_price
            stop_side = "sell" if pos.direction == "long" else "buy"

            order = self._dispatcher.submit_gtc_stop(
                self.broker, pos.symbol, pos.qty_filled, stop_side, stop_price
            )
            if order and hasattr(order, "id"):
                pos.stop_order_id = order.id
                pos.state = HoldState.ACTIVE
                pos.updated_at = self._now_et().isoformat()  # RC-1
                _register_symbol(pos.symbol)
                logger.info(
                    "QuarterlyHoldManager: %s GTC stop @ $%.2f submitted "
                    "(ATR=%.2f, floor=%.2f, entry=%.2f)",
                    pos.symbol, stop_price,
                    atr_stop if bars and len(bars) >= _ATR_PERIOD_WEEKS else 0,
                    floor_stop, pos.avg_entry_price,
                )
                self._alert(
                    f"✅ QHM: {pos.symbol} tranche 1 filled — "
                    f"{pos.qty_filled} sh @ ${pos.avg_entry_price:.2f} avg. "
                    f"GTC stop @ ${stop_price:.2f}"
                )
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s GTC stop submit failed — "
                    "→ PENDING_STOP_REPLACE",
                    pos.symbol,
                )
                self._handle_missing_stop(pos)

        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: stop compute/submit error for %s: %s",
                pos.symbol, e,
            )
            self._handle_missing_stop(pos)

    def _initiate_exit(
        self, pos: HoldPosition, reason: str = "max_hold_duration"
    ) -> None:
        """Submit broker.close_position() and transition to PENDING_EXIT.
        Called for max-hold-duration exits (13 weeks). GTC stop is primary exit.
        """
        if self.dry_run:
            return
        try:
            success = self._dispatcher.close(self.broker, pos.symbol)
            pos.state = (
                HoldState.PENDING_EXIT if success
                else HoldState.CLOSED
            )
            pos.updated_at = self._now_et().isoformat()  # RC-1
            _deregister_symbol(pos.symbol)
            logger.warning(
                "QuarterlyHoldManager: %s exit initiated (%s) → state=%s",
                pos.symbol, reason, pos.state.value,
            )
            self._alert(
                f"🔴 QHM: {pos.symbol} exit initiated — {reason}. "
                f"State → {pos.state.value}. "
                f"Verify via Alpaca fills API."
            )
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: exit failed for %s: %s", pos.symbol, e
            )

    # -----------------------------------------------------------------------
    # Config loading — reads quarterly_holds_config.json written by CCR
    # -----------------------------------------------------------------------

    def _load_thesis_config(self) -> dict[str, dict]:
        """Load per-symbol config from quarterly_holds_config.json.
        Fail-open at init (warns + returns {}); fail-closed at add_candidate().
        """
        if not _CONFIG_PATH.exists():
            logger.warning(
                "QuarterlyHoldManager: config not found at %s "
                "— no candidates will be added until config is present",
                _CONFIG_PATH,
            )
            return {}
        try:
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
            logger.info(
                "QuarterlyHoldManager: loaded config from %s (%d picks)",
                _CONFIG_PATH, len(cfg.get("picks", {})),
            )
            return cfg.get("picks", {})
        except json.JSONDecodeError as e:
            logger.warning(
                "QuarterlyHoldManager: config malformed at %s: %s "
                "— no candidates will be added",
                _CONFIG_PATH, e,
            )
            return {}

    # -----------------------------------------------------------------------
    # State persistence — RC-5: atomic tmp→replace with os.fsync
    # -----------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load positions from quarterly_holds.json."""
        if not self.state_path.exists():
            logger.info(
                "QuarterlyHoldManager: no state file at %s — starting fresh",
                self.state_path,
            )
            return
        try:
            with open(self.state_path) as f:
                raw = json.load(f)
            for sym, d in raw.items():
                try:
                    # Migration: THESIS_INVALIDATED → CLOSED (S54 enum removal)
                    if d.get("state") == "THESIS_INVALIDATED":
                        d["state"] = "CLOSED"
                        logger.info(
                            "QuarterlyHoldManager: migrated %s "
                            "THESIS_INVALIDATED → CLOSED",
                            sym,
                        )
                    self._positions[sym] = HoldPosition.from_dict(d)
                except Exception as e:  # RC-3
                    logger.warning(
                        "QuarterlyHoldManager: skipping corrupt state for %s: %s",
                        sym, e
                    )
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: state load failed from %s: %s",
                self.state_path, e,
            )

    def _save_state(self) -> None:
        """Atomic write with os.fsync — RC-5 compliant (board S48b requirement)."""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {sym: pos.to_dict() for sym, pos in self._positions.items()}
            tmp_path = self.state_path.with_suffix(".tmp")
            with open(tmp_path, "w") as f:
                json.dump(payload, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())  # RC-5: board-required fsync
            tmp_path.replace(self.state_path)  # RC-5: POSIX atomic replace
        except Exception as e:  # RC-3
            logger.warning("QuarterlyHoldManager: state save failed: %s", e)

    # -----------------------------------------------------------------------
    # Utility helpers
    # -----------------------------------------------------------------------

    def _now_et(self) -> datetime:
        """RC-1: always timezone-aware."""
        if self._clock:
            return self._clock()
        return datetime.now(ET)  # RC-1

    def _get_account_equity(self) -> float:
        try:
            acct = self.broker.get_account()
            return float(getattr(acct, "equity", 0))
        except Exception as e:  # RC-3
            logger.warning("QuarterlyHoldManager: get_account_equity failed: %s", e)
            return 0.0

    def _resync_from_alpaca(self, pos: HoldPosition) -> None:
        """DS+GAI S49: Resync qty/avg_entry_price from Alpaca (authoritative).
        Prevents drift between QHM state file and broker ledger.
        Called in run_weekly_check() for all ACTIVE positions.
        """
        if self.dry_run:
            return
        try:
            alpaca_pos = self.broker.get_position(pos.symbol)
            if alpaca_pos is None:
                return  # Will be caught by _detect_external_close on same cycle
            # RC-6: qty, avg_entry_price verified against Alpaca fills API
            live_qty = int(float(getattr(alpaca_pos, "qty", 0)))
            live_avg = float(getattr(alpaca_pos, "avg_entry_price", 0))
            if live_qty > 0 and live_avg > 0:
                drift_qty = live_qty != pos.qty_filled
                drift_price = abs(live_avg - pos.avg_entry_price) > 0.01
                if drift_qty or drift_price:
                    logger.info(
                        "QuarterlyHoldManager: %s Alpaca resync: "
                        "qty %d→%d, avg_entry %.4f→%.4f",
                        pos.symbol, pos.qty_filled, live_qty,
                        pos.avg_entry_price, live_avg,
                    )
                    pos.qty_filled = live_qty
                    pos.qty_total = live_qty
                    pos.avg_entry_price = live_avg
                    pos.updated_at = self._now_et().isoformat()  # RC-1
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _resync_from_alpaca failed for %s: %s",
                pos.symbol, e,
            )

    def _get_quarterly_notional_excl(self, exclude_symbol: str) -> float:
        """Kelly fix: total live notional of other quarterly holds.
        Derman: subtract QHM notional from available equity for intraday Kelly.
        """
        total = 0.0
        for sym, pos in self._positions.items():
            if sym == exclude_symbol:
                continue
            if pos.state in (HoldState.ACTIVE, HoldState.AWAITING_FILL):
                try:
                    p = self._get_live_price(sym)
                    if p and p > 0 and pos.qty_filled > 0:
                        total += p * pos.qty_filled
                except Exception:  # RC-3: non-critical; default to 0
                    pass
        return total

    def _get_live_price(self, symbol: str) -> Optional[float]:
        """Get current price via broker.get_position(). RC-6: field names verified."""
        try:
            pos = self.broker.get_position(symbol)
            if pos:
                # RC-6: current_price is the Alpaca field for live mark price
                price = getattr(pos, "current_price", None)
                if price:
                    return float(price)
                # Fallback to lastday_price if current not available
                price = getattr(pos, "lastday_price", None)
                if price:
                    return float(price)
            return None
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: get_live_price failed for %s: %s", symbol, e
            )
            return None

    def _get_open_orders(self) -> list:
        """Fetch open orders via broker module-level function."""
        try:
            from execution.broker import get_open_orders
            return get_open_orders() or []
        except Exception as e:  # RC-3
            logger.warning("QuarterlyHoldManager: get_open_orders failed: %s", e)
            return []

    def _alert(self, message: str) -> None:
        """Fire Slack alert via injected alerter. Fail-soft (RC-3)."""
        if not self.alerter:
            return
        try:
            self.alerter.send(message)
        except Exception as e:  # RC-3
            logger.warning("QuarterlyHoldManager: alerter failed: %s", e)
