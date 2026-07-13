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

# ── QHM DIP-ADD RULE (board 2 seats + Gro + GAI, Rafael approved 2026-07-12) ──
# Buy MORE of a conviction hold on weakness below cost average. Spec:
# logs/qhm_v2_design_2026-07-11.md PART 1 FINALIZED. DORMANT until
# _DIP_ADD_ENABLED=True (ships dark; enable after a verification cycle + the
# one-time NVDA catch-up). Two rungs; a hard pre-fill %-equity ceiling + a
# quarterly max-shares cap (board OVERRODE Gro/GAI "no cap": the -5% trigger
# is off a FALLING cost avg, so a grind could stack adds to ~34% of equity
# before the price floor fires); a -15% first-entry stop-adding floor.
_DIP_ADD_ENABLED           = True   # LIVE (Rafael 2026-07-13): adds on dips below avg
_DIP_ADD_RUNG_A_PCT        = 0.02   # <= cost_avg*(1-0.02): small add, capped AT target
_DIP_ADD_RUNG_B_PCT        = 0.05   # <= cost_avg*(1-0.05): aggressive add to ceiling
_DIP_ADD_CEILING_MULT      = 1.375  # Rung B ceiling = 1.375x target (27.5% @ 20%)
_DIP_ADD_STOP_FLOOR_PCT    = 0.15   # STOP adding below first-entry*(1-0.15); escalate
_DIP_ADD_MAX_PER_QUARTER   = 3      # anti-osc: max dip-adds per position per quarter
_DIP_ADD_MIN_DAYS_BETWEEN  = 2      # anti-osc: >= this many days between adds
_DIP_ADD_NO_ADD_DAYS_PRE_EARNINGS = 7  # no adds within this many days of earnings


# ---------------------------------------------------------------------------
# Module-level shared registry (imported by entry_logic.py)
# ---------------------------------------------------------------------------
_quarterly_hold_symbols: set[str] = set()

# Persistent, cross-process source of truth for QHM symbols.
# CROSS-PROCESS INCIDENT (2026-07-02): the in-process registry above is only
# populated in the main-bot process (where QuarterlyHoldManager is instantiated).
# A SEPARATE process — e.g. run_movers.py — never populates it, so
# get_quarterly_hold_symbols() returned EMPTY there, turning every QHM guard in
# that process into a silent no-op. run_movers then flattened the QHM holds
# NVDA/GOOGL at the open. Fix: fall back to the on-disk state file (the same
# authoritative source orphan_manager.cancel_and_reconcile_gtc_stops reads), so
# the guard is effective in ANY process. Fail-safe: any error → empty frozenset
# (never raise into a caller; matches prior behavior).
_QHM_STATE_FILE = _ROOT / "data" / "state" / "quarterly_holds.json"
# MUST match the in-process registration set exactly (the HoldStates that add a
# symbol to _quarterly_hold_symbols — see reconcile/init, incl. PENDING_EARNINGS,
# a fully-open position whose GTC stop is cancelled pre-earnings). Excludes only
# PENDING_ENTRY (no shares yet) and CLOSED (exited). Omitting PENDING_EARNINGS
# would leave an earnings-paused hold unprotected out-of-process (cold-agent catch).
_QHM_ACTIVE_STATES = frozenset({
    "AWAITING_FILL", "ACTIVE", "PENDING_STOP_REPLACE",
    "PENDING_EXIT", "PENDING_EARNINGS",
})

# Last successfully-parsed QHM symbol set (any process). First FAIL-CLOSED fallback
# if the live state file later becomes present-but-corrupt.
_last_good_qhm_symbols: frozenset[str] = frozenset()


def _configured_qhm_symbols() -> frozenset[str]:
    """The configured QHM pick universe from quarterly_holds_config.json. Used as a
    FAIL-CLOSED fallback (deliberately over-protective) when the live state file is
    present but corrupt — better to shield a few extra symbols than to sell a hold."""
    try:
        if _CONFIG_PATH.exists():
            _cfg = json.loads(_CONFIG_PATH.read_text())
            return frozenset((_cfg.get("picks") or {}).keys())
    except Exception:
        pass
    return frozenset()


def get_quarterly_hold_symbols() -> frozenset[str]:
    """Immutable snapshot of symbols currently in quarterly holds.
    Called by entry_logic.py before every scan to block intraday entries,
    by Kelly sizing to prevent same-symbol cross-trades, and by cross-strategy
    guards (movers, orphan reconciliation) to never touch a QHM-held symbol.

    Precedence: in-process registry (main-bot) → persistent quarterly_holds.json
    (any process, incl. run_movers) → FAIL-CLOSED fallback on a corrupt file.
    """
    global _last_good_qhm_symbols
    if _quarterly_hold_symbols:
        return frozenset(_quarterly_hold_symbols)
    try:
        if _QHM_STATE_FILE.exists():
            _raw = json.loads(_QHM_STATE_FILE.read_text())
            _syms = frozenset(
                sym for sym, pos in _raw.items()
                if isinstance(pos, dict) and pos.get("state") in _QHM_ACTIVE_STATES
            )
            _last_good_qhm_symbols = _syms   # cache last-known-good
            return _syms
        # File ABSENT → QHM genuinely holds nothing → empty is correct.
        return frozenset()
    except Exception as _qhm_read_err:
        # File PRESENT but CORRUPT/unreadable. Returning empty here would make EVERY
        # QHM guard a silent no-op — safe_close_all / movers / exit paths would treat
        # QHM as unprotected and could SELL it (the 2026-07-02 flaw Gro+GAI flagged:
        # empty-on-corrupt is a sell-all trigger). FAIL CLOSED: prefer last-known-good,
        # else the configured pick universe (over-protective), else empty as an
        # absolute last resort. Never raise into a guard caller.
        _fallback = _last_good_qhm_symbols or _configured_qhm_symbols()
        logger.critical(
            "get_quarterly_hold_symbols: QHM state file CORRUPT (%s) — FAIL-CLOSED to "
            "%d protected symbol(s) %s (never returning empty, which would let a QHM "
            "hold be sold). Restore data/state/quarterly_holds.json.",
            _qhm_read_err, len(_fallback), sorted(_fallback),
        )
        return _fallback


def get_quarterly_hold_quantities() -> dict[str, int]:
    """{symbol: qty_filled} for ACTIVE quarterly holds — the AUTHORITATIVE source of
    which shares are QHM-tier. Read by the ownership ledger (execution.ownership_guard)
    to attribute the qhm tier's protected floor: legacy QHM buys predate the
    client_order_id tier-tagging, so a fill-based ledger counts them as intraday
    (floor=0, unprotected). This gives the maintainer the true qhm share count so
    NVDA/GOOGL etc. get a real never-sell floor.

    Fail-CLOSED: an existing-but-unreadable/corrupt state file RAISES so the caller (the
    ledger maintainer) leaves the ledger at its last-good state rather than silently
    dropping a quarterly-hold's protected floor to 0. A genuinely-absent file, or a book
    with no active holds, returns {} (there is nothing to attribute)."""
    if not _QHM_STATE_FILE.exists():
        return {}
    try:
        _raw = json.loads(_QHM_STATE_FILE.read_text())
        return {
            sym: int(pos.get("qty_filled", 0) or 0)
            for sym, pos in _raw.items()
            if isinstance(pos, dict)
            and pos.get("state") in _QHM_ACTIVE_STATES
            and int(pos.get("qty_filled", 0) or 0) > 0
        }
    except Exception as _qhm_qty_err:
        logger.critical(
            "get_quarterly_hold_quantities: state file present but unreadable/corrupt "
            "(%s) — raising so the ledger maintainer fails CLOSED (keeps last-good)",
            _qhm_qty_err,
        )
        raise


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
    PENDING_EARNINGS     = "PENDING_EARNINGS"       # GTC stop cancelled pre-earnings


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
    earnings_gate_date: Optional[str] = None  # YYYY-MM-DD expected earnings date
    thesis_check_last: Optional[str] = None   # ISO timestamp
    thesis_check_result: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(ET).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(ET).isoformat())
    # Dip-add anti-oscillation state (2026-07-12). Defaulted so from_dict(cls(**d))
    # on pre-existing persisted holds (which lack these keys) applies the defaults.
    dip_adds_quarter: int = 0                  # dip-adds done in dip_add_quarter_tag
    last_dip_add_date: Optional[str] = None    # YYYY-MM-DD of last dip-add (spacing)
    dip_add_quarter_tag: Optional[str] = None  # "YYYY-Qn" the counter resets on

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
        return broker.submit_limit_order(
            symbol, qty, side, limit_price, extended_hours, tier="qhm")

    def submit_gtc_stop(
        self,
        broker,
        symbol: str,
        qty: int,
        side: str,
        stop_price: float,
    ) -> object:
        return broker.submit_gtc_stop_order(symbol, qty, side, stop_price, tier="qhm")

    def close(self, broker, symbol: str) -> bool:
        # tier="qhm" (matches submit_limit / submit_gtc_stop above): tags the qhm
        # tier so the never-sell-floor chokepoint (broker.close_position, inc 4a)
        # routes this QHM self-exit to the qhm tier's shares, not the "intraday"
        # default (which the guard REJECTs on a QHM-only symbol once the flag is on).
        return broker.close_position(symbol, tier="qhm")


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
        # AWP audit fix (2026-06-28): PENDING_EARNINGS was missing from this
        # list. A position reaches PENDING_EARNINGS with its GTC stop
        # deliberately cancelled (earnings within 5 days) but the position
        # itself is still fully open and alive -- if the bot restarts while
        # in this state, the symbol was NOT re-registered in
        # _quarterly_hold_symbols, meaning entry_logic.py's intraday scan
        # would no longer see this symbol as blocked, allowing the MTF bot
        # to open a conflicting second position in the same name while the
        # quarterly hold is still open. Confirmed by board Quant-logic agent
        # during the Phase 2 full-board redo.
        _quarterly_hold_symbols.clear()
        for sym, pos in self._positions.items():
            if pos.state in (
                HoldState.AWAITING_FILL,
                HoldState.ACTIVE,
                HoldState.PENDING_STOP_REPLACE,
                HoldState.PENDING_EXIT,
                HoldState.PENDING_EARNINGS,
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
        # HOLE-2 guard (2026-07-02): before the per-symbol reconcile and before RTH,
        # kill any stray SELL resting on a QHM-held symbol so an orphaned or
        # cross-process sell cannot fill at the open and liquidate a quarterly hold.
        try:
            _stray = self.cancel_stray_sell_orders()
            if _stray:
                logger.critical(
                    "QuarterlyHoldManager reconcile_on_startup: cancelled %d stray "
                    "sell order(s) at startup", _stray,
                )
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager reconcile_on_startup: stray-sell guard "
                "error: %s", e,
            )
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
                    # External-close check first: a position can be closed (GTC
                    # fired pre-replace, manual close, or external bug) while stuck
                    # here with no stop to detect via Alpaca. Without this check
                    # the position is stranded forever.
                    if self._detect_external_close(pos, result):
                        result.symbols_reconciled.append(symbol)
                        continue
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

                elif pos.state == HoldState.PENDING_EARNINGS:
                    self._reconcile_pending_earnings(pos, result)
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

    def cancel_stray_sell_orders(self) -> int:
        """HOLE-2 guard (2026-07-02, board + Gro + GAI): cancel any resting SELL
        order on a QHM-held symbol that is NOT that hold's own registered protective
        stop.

        Incident context: on 2026-07-02 a separate process (run_movers.py) placed a
        sell on QHM symbols and flattened NVDA/GOOGL at the open. A stray or orphaned
        resting SELL from ANY source (another strategy's process, a manual order, an
        adopted-but-unrecognized order) would fill at the open and liquidate a
        quarterly hold. On a LONG hold the authorized sell is the protective GTC stop
        (pos.stop_order_id): the guard KEEPS any resting sell whose id matches
        pos.stop_order_id and cancels every OTHER sell. Per state: ACTIVE keeps its
        current stop id; PENDING_EARNINGS clears stop_order_id to None (any sell is
        then unauthorized — stop deliberately cancelled for earnings);
        PENDING_STOP_REPLACE RETAINS the last-known stop id (_handle_missing_stop does
        not clear it), so a real stop still resting under that id is preserved while
        genuine strays are cancelled. Buy orders are never touched (QHM entries are
        buys; other strategies' buys are theirs).

        Called at startup (reconcile_on_startup) and once per RTH cycle
        (run_weekly_check), so a stray sell resting at boot OR appearing mid-day is
        cancelled before it can fill. FAILS OPEN: if the order API cannot be read
        (_get_open_orders returns empty on error), nothing is cancelled — the guard
        never risks cancelling a real protective stop on unreadable state. Returns
        the number of stray sell orders cancelled.
        """
        if self.dry_run:
            return 0
        # Symbols QHM currently holds shares in (or is awaiting a fill on). A resting
        # non-stop sell on any of these is unauthorized. PENDING_EXIT is excluded —
        # QHM is deliberately exiting there. PENDING_ENTRY holds no shares yet.
        _guard_states = (
            HoldState.ACTIVE, HoldState.AWAITING_FILL,
            HoldState.PENDING_STOP_REPLACE, HoldState.PENDING_EARNINGS,
        )
        guarded: dict[str, Optional[str]] = {
            sym: (pos.stop_order_id or None)  # normalize "" → None (unknown stop id)
            for sym, pos in self._positions.items()
            if pos.state in _guard_states
        }
        if not guarded:
            return 0
        # Distinguish an API error (None) from a genuinely-empty book ([]). broker's
        # get_open_orders() returns None ONLY on API failure. We must NOT blindly
        # cancel on unreadable state: a fail-CLOSED "cancel every sell on a guarded
        # symbol" would cancel each hold's OWN protective GTC stop (a sell), leaving
        # the hold unprotected — the opposite of this guard's purpose (board+Gro+GAI
        # 2026-07-02). So we fail OPEN on the cancel action, but make the blindness
        # LOUD (CRITICAL + Slack) so a human verifies holds while the API is degraded.
        try:
            from execution.broker import get_open_orders
            open_orders = get_open_orders()
        except Exception as e:  # RC-3
            open_orders = None
            logger.warning(
                "QHM cancel_stray_sell_orders: get_open_orders raised: %s", e
            )
        if open_orders is None:
            logger.critical(
                "QHM cancel_stray_sell_orders: order API UNREADABLE — stray-sell "
                "guard could NOT run; %d QHM hold(s) %s not verified this pass. "
                "Failing OPEN (no blind cancels, which would kill the real stops). "
                "Verify holds manually while the order API is degraded.",
                len(guarded), sorted(guarded),
            )
            self._alert(
                f"🚨 QHM GUARD BLIND: order API unreadable — could not check "
                f"{sorted(guarded)} for stray sells this pass. Manual check advised."
            )
            return 0
        if not open_orders:
            return 0  # genuinely no open orders on the book
        cancelled = 0
        for o in open_orders:
            try:
                o_sym = getattr(o, "symbol", None)
                if o_sym not in guarded:
                    continue
                _side_raw = getattr(o, "side", "")
                o_side = str(getattr(_side_raw, "value", _side_raw)).lower()
                if "sell" not in o_side:
                    continue  # buy — never liquidates a long hold
                o_id = getattr(o, "id", None)
                if o_id is None:
                    continue
                legit_stop = guarded[o_sym]
                if legit_stop is not None and str(o_id) == str(legit_stop):
                    continue  # this IS the hold's own protective stop — keep it
                from execution.broker import cancel_order
                if cancel_order(str(o_id)):
                    cancelled += 1
                    logger.critical(
                        "QHM cancel_stray_sell_orders: CANCELLED unauthorized SELL "
                        "order %s on QHM-held %s (side=%s, legit stop=%s) — a stray "
                        "sell would have liquidated a quarterly hold at the open.",
                        o_id, o_sym, o_side, legit_stop,
                    )
                    self._alert(
                        f"🛑 QHM GUARD: cancelled a stray SELL order on {o_sym} "
                        f"(order {o_id}) that was NOT its protective stop — quarterly "
                        f"hold protected from liquidation."
                    )
                else:
                    logger.critical(
                        "QHM cancel_stray_sell_orders: FAILED to cancel stray SELL "
                        "order %s on QHM-held %s — hold may be at risk.",
                        o_id, o_sym,
                    )
                    self._alert(
                        f"🚨 QHM GUARD: FAILED to cancel a stray SELL order on {o_sym} "
                        f"(order {o_id}). Manual intervention required."
                    )
            except Exception as e:  # RC-3
                logger.warning(
                    "QHM cancel_stray_sell_orders: error handling order on %s: %s",
                    getattr(o, "symbol", "?"), e,
                )
        return cancelled

    def run_weekly_check(self) -> None:
        """Once per RTH cycle: external close detection + resync + max-hold exit.
        GTC stop is the primary exit path. _initiate_exit() is the 13-week backstop.
        """
        # HOLE-2 guard (2026-07-02): kill any stray SELL on a QHM-held symbol that
        # appeared since the last cycle, before it can fill and liquidate a hold.
        try:
            _stray = self.cancel_stray_sell_orders()
            if _stray:
                logger.critical(
                    "QuarterlyHoldManager run_weekly_check: cancelled %d stray sell "
                    "order(s) mid-session", _stray,
                )
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager run_weekly_check: stray-sell guard error: %s", e
            )
        for symbol, pos in list(self._positions.items()):
            _stuck = (HoldState.PENDING_STOP_REPLACE, HoldState.PENDING_EARNINGS)
            if pos.state in _stuck:
                try:
                    if self._detect_external_close(pos, ReconcileResult()):
                        continue
                    # AWP audit fix (2026-06-28): resubmit_stop_if_needed()
                    # and _reconcile_pending_earnings() were previously only
                    # ever called from reconcile_on_startup() — i.e. only on
                    # a bot RESTART. Per this method's own docstring,
                    # run_weekly_check() runs once per RTH cycle (every 5
                    # minutes via run_cycle.py), but never actually attempted
                    # recovery from either stuck state on that cadence — it
                    # only checked for external close and otherwise left the
                    # position stuck. Confirmed via full board audit (2 of 4
                    # domain agents independently): a position that loses its
                    # GTC stop had NO automatic resubmit path short of a bot
                    # restart, which could be hours, days, or (with no
                    # scheduled restart) indefinitely away. Now attempts
                    # recovery every cycle instead of waiting for a restart.
                    if pos.state == HoldState.PENDING_STOP_REPLACE:
                        self.resubmit_stop_if_needed(symbol)
                    elif pos.state == HoldState.PENDING_EARNINGS:
                        self._reconcile_pending_earnings(pos, ReconcileResult())
                except Exception as e:  # RC-3
                    logger.warning(
                        "QuarterlyHoldManager weekly_check external-close/resubmit "
                        "check error for %s: %s", symbol, e, exc_info=True,
                    )
                continue
            if pos.state != HoldState.ACTIVE:
                continue
            try:
                if self._detect_external_close(pos, ReconcileResult()):
                    continue

                self._resync_from_alpaca(pos)

                self._maybe_enter_earnings_hold(pos)
                if pos.state == HoldState.PENDING_EARNINGS:
                    continue  # type: ignore[unreachable]  # state set by side-effect

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

                # DIP-ADD: buy more on weakness below cost avg (cold-2nd threat-B fix:
                # placed AFTER the max-hold check so a position that just initiated its
                # force-exit self-guards on state != ACTIVE and gets no dip-add). ACTIVE
                # ACTIVE holds only; DORMANT unless _DIP_ADD_ENABLED; never raises.
                self._maybe_dip_add(pos, self._now_et().strftime("%Y-%m-%d"))

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
                        if not self._is_tranche_due(pos, today_str):
                            continue
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
            if open_orders is None:
                # B2 fix: unknown book — do NOT decide "expired → PENDING_ENTRY"
                # (which could double-submit) or "filled". Keep AWAITING_FILL;
                # next reconcile/cycle re-checks once the API is readable.
                logger.warning(
                    "QuarterlyHoldManager: %s AWAITING_FILL — order API "
                    "unreadable; deferring order-state decision to next check.",
                    pos.symbol,
                )
                return
            # B2 leg 2: str-normalize (SDK UUID vs JSON str) — raw membership
            # false-negatived here too, causing false "DAY order expired" resets.
            order_ids = {str(getattr(o, "id", "")) for o in open_orders}

            if str(pos.entry_order_id) not in order_ids:
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
            if open_orders is None:
                # B2 fix: API unreadable ≠ stop missing. Retry once, then keep
                # the position ACTIVE with its recorded stop id — NEVER flag a
                # possibly-live stop missing on unknown state (that stranded
                # both holds in PENDING_STOP_REPLACE on 2026-07-02). Loud so a
                # genuinely-missing stop during an outage is caught by a human;
                # run_weekly_check re-verifies every cycle once the API is back.
                import time as _time
                _time.sleep(2)
                open_orders = self._get_open_orders()
            if open_orders is None:
                # GAI final-audit fix (2026-07-02): do NOT stay ACTIVE on double
                # API failure — that would mask a GENUINELY missing stop with no
                # automated recovery. Transition to PENDING_STOP_REPLACE, which
                # now has a safe recovery both ways: resubmit_stop_if_needed
                # (every cycle) RE-ADOPTS the stop if it is in fact resting
                # (no duplicate submit), or resubmits if genuinely missing.
                # stop_order_id is retained in this state, so the HOLE-2 guard
                # still preserves the real resting stop meanwhile.
                logger.critical(
                    "QuarterlyHoldManager: %s — order API UNREADABLE at startup "
                    "(after retry); state → PENDING_STOP_REPLACE with recorded "
                    "stop %s retained. Recovery loop will re-adopt or resubmit "
                    "once the API is readable.",
                    pos.symbol, pos.stop_order_id,
                )
                self._alert(
                    f"🚨 QHM: order API unreadable at startup — {pos.symbol} → "
                    f"PENDING_STOP_REPLACE (stop id {pos.stop_order_id} retained; "
                    f"auto re-adopt/resubmit when API recovers)."
                )
                pos.state = HoldState.PENDING_STOP_REPLACE
                pos.updated_at = self._now_et().isoformat()  # RC-1
                return
            # B2 leg 2 (2026-07-03 runtime proof): SDK returns id as uuid.UUID;
            # stop_order_id reloaded from JSON is str. Raw membership NEVER
            # matched ("af11..." != UUID("af11...")), so every restart flagged
            # live stops missing. str-normalize both sides (same idiom Gro/GAI
            # approved in resubmit_stop_if_needed's re-adopt).
            order_ids = {str(getattr(o, "id", "")) for o in open_orders}
            if str(pos.stop_order_id) in order_ids:
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
        # B2 self-heal (2026-07-02, board+Gro+GAI): if the REGISTERED stop is in
        # fact still resting on Alpaca (we got here via an adoption false-negative,
        # not a genuinely missing stop), RE-ADOPT it instead of submitting a
        # duplicate — Alpaca rejects the duplicate with 40310000 (held_for_orders)
        # every cycle and the state never recovers to ACTIVE.
        if pos.stop_order_id:
            _book = self._get_open_orders()
            if _book is None:
                return False  # unknown book — never risk a duplicate submit blind
            if any(str(getattr(o, "id", None)) == str(pos.stop_order_id)
                   for o in _book):
                pos.state = HoldState.ACTIVE
                pos.updated_at = self._now_et().isoformat()  # RC-1
                self._save_state()
                logger.info(
                    "QuarterlyHoldManager: %s stop %s found RESTING on Alpaca — "
                    "re-adopted, state → ACTIVE (no resubmit needed).",
                    pos.symbol, pos.stop_order_id,
                )
                self._alert(
                    f"✅ QHM: {pos.symbol} protective stop verified resting — "
                    f"re-adopted (state machine healed, no duplicate submitted)."
                )
                return True
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
                # AWP audit fix (2026-06-28): missing format arg -- this %s
                # placeholder had nothing supplied, which would raise
                # TypeError at log-emit time. Now more reachable than before
                # since this function is wired into run_weekly_check()'s
                # every-cycle path, not just the bot-restart path.
                logger.warning(
                    "QuarterlyHoldManager: %s GTC stop resubmit returned None",
                    symbol,
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
        if pos.state not in (
            HoldState.ACTIVE, HoldState.PENDING_STOP_REPLACE, HoldState.PENDING_EARNINGS
        ):
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
                # trade_events.jsonl — CLAUDE.md §7 structured exit event (Change B)
                try:
                    _te_path = _ROOT / "logs" / "trade_events.jsonl"  # RC-2
                    _hold_days = 0
                    if pos.entry_day:
                        try:
                            from datetime import date as _date
                            _hold_days = (
                                self._now_et().date()
                                - _date.fromisoformat(pos.entry_day)
                            ).days
                        except (ValueError, TypeError):
                            pass
                    _te = {
                        "ts": datetime.now(PT).isoformat(),  # RC-1: PT (CLAUDE.md §8)
                        "event": "exit",
                        "exit_reason": "external_close_detected",
                        "symbol": pos.symbol,
                        "price": None,  # reconcile via Alpaca fills API
                        "price_pending": True,
                        "size": pos.qty_filled,
                        "mri_level": "N/A",
                        "score": 0,
                        "data_source": "qhm_external_close",
                        "hold_days": _hold_days,
                        "pdt_used": 0,
                    }
                    with open(_te_path, "a") as _f:
                        _f.write(json.dumps(_te) + "\n")
                except Exception as _te_e:  # RC-3
                    logger.warning(
                        "QuarterlyHoldManager: trade_events write failed for %s: %s",
                        pos.symbol, _te_e,
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
    # Earnings protection helpers
    # -----------------------------------------------------------------------

    def _maybe_enter_earnings_hold(self, pos: HoldPosition) -> None:
        """Cancel GTC stop if earnings within 5 calendar days. Gate on confirmed cancel.
        Harris: cancel failure must not advance state.
        DS: verify position still exists after cancel (cancel-fill race guard).
        """
        if pos.state != HoldState.ACTIVE:
            return
        try:
            from data.fmp_client import get_cached_earnings_dates
            today = self._now_et().date()
            earnings_dates = get_cached_earnings_dates(pos.symbol)
            upcoming = sorted([d for d in earnings_dates if d >= today])
            if not upcoming or (upcoming[0] - today).days > 5:
                return
            logger.info(
                "QuarterlyHoldManager: %s earnings on %s (%d days away) — "
                "attempting GTC stop cancel, state→PENDING_EARNINGS",
                pos.symbol, upcoming[0], (upcoming[0] - today).days,
            )
            if pos.stop_order_id and not self.dry_run:
                try:
                    from execution.broker import cancel_order
                    cancel_order(pos.stop_order_id)
                    logger.info(
                        "QuarterlyHoldManager: %s GTC stop %s cancelled pre-earnings",
                        pos.symbol, pos.stop_order_id,
                    )
                    # DS: cancel-fill race guard — verify position still exists
                    verify_pos = self.broker.get_position(pos.symbol)
                    if verify_pos is None:
                        logger.warning(
                            "QuarterlyHoldManager: %s stop filled during cancel "
                            "window — external close; will catch on next cycle",
                            pos.symbol,
                        )
                        return
                except Exception as _ce:  # RC-3 — Harris: cancel failure gates state
                    logger.warning(
                        "QuarterlyHoldManager: %s pre-earnings stop cancel FAILED — "
                        "staying ACTIVE with existing stop: %s",
                        pos.symbol, _ce,
                    )
                    self._alert(
                        f"⚠️ QHM: {pos.symbol} pre-earnings stop cancel FAILED — "
                        f"stays ACTIVE with existing stop."
                    )
                    return  # Do NOT transition if cancel failed
            pos.stop_order_id = None
            pos.earnings_gate_date = upcoming[0].isoformat()
            pos.state = HoldState.PENDING_EARNINGS
            pos.updated_at = self._now_et().isoformat()  # RC-1
            self._alert(
                f"⏳ QHM: {pos.symbol} earnings on {upcoming[0]} — GTC stop cancelled. "
                f"State→PENDING_EARNINGS. Stop resubmits post-earnings at startup."
            )
            self._save_state()
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _maybe_enter_earnings_hold error for %s: %s",
                pos.symbol, e,
            )

    def _reconcile_pending_earnings(
        self, pos: HoldPosition, result: ReconcileResult
    ) -> None:
        """Resubmit GTC stop after earnings pass. Called from reconcile_on_startup().
        Derman: FMP empty ambiguous — use earnings_gate_date fallback.
        Beck: restore _register_symbol on all paths after restart.
        """
        try:
            from data.fmp_client import get_cached_earnings_dates
            from datetime import date as _date
            today = self._now_et().date()
            earnings_dates = get_cached_earnings_dates(pos.symbol)
            upcoming = [d for d in earnings_dates if d >= today]

            if upcoming:
                # FMP confirms earnings still ahead
                _register_symbol(pos.symbol)  # Beck: restore block after restart
                logger.info(
                    "QuarterlyHoldManager: %s PENDING_EARNINGS — "
                    "next earnings %s still ahead",
                    pos.symbol, min(upcoming),
                )
                return

            # FMP returned empty — disambiguate failure vs. genuine clear (Derman)
            if not earnings_dates and pos.earnings_gate_date:
                try:
                    gate_dt = _date.fromisoformat(pos.earnings_gate_date)
                    if today <= gate_dt:
                        # Earnings date not yet reached — FMP likely failing; stay safe
                        _register_symbol(pos.symbol)
                        logger.warning(
                            "QuarterlyHoldManager: %s FMP empty, gate_date %s not yet "
                            "passed — staying PENDING_EARNINGS (FMP may be down)",
                            pos.symbol, pos.earnings_gate_date,
                        )
                        return
                    # today > gate_dt: earnings date passed — fall through to resubmit
                except (ValueError, TypeError):
                    pass

            # Earnings confirmed passed (or gate_date exceeded) — resubmit
            logger.info(
                "QuarterlyHoldManager: %s earnings passed — resubmitting GTC stop",
                pos.symbol,
            )
            success = self._resubmit_post_earnings_stop(pos)
            if success:
                result.orders_verified += 1
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _reconcile_pending_earnings error for %s: %s",
                pos.symbol, e,
            )
            _register_symbol(pos.symbol)  # always restore intraday block on error

    def _resubmit_post_earnings_stop(self, pos: HoldPosition) -> bool:
        """Post-earnings GTC stop anchored to current market price.
        LdP: entry price wrong anchor post-earnings gap — use live price.
        GAI: long only — short buy-stop requires price + X, not price - X.
        Returns True on successful GTC stop submission.
        """
        if self.dry_run:
            return False
        if pos.direction != "long":
            logger.error(
                "QuarterlyHoldManager: _resubmit_post_earnings_stop: "
                "direction=%r on %s unsupported — only 'long'. Stays PENDING_EARNINGS.",
                pos.direction, pos.symbol,
            )
            _register_symbol(pos.symbol)
            return False
        current_price = self._get_live_price(pos.symbol)
        if not current_price or current_price <= 0:
            logger.warning(
                "QuarterlyHoldManager: %s no live price post-earnings — "
                "PENDING_STOP_REPLACE",
                pos.symbol,
            )
            self._handle_missing_stop(pos)
            _register_symbol(pos.symbol)
            return False
        try:
            from data.fetcher import fetch_bars
            import config as _cfg
            tf = getattr(_cfg, "TF_WEEKLY", "1Week")
            bars = fetch_bars(pos.symbol, tf, num_bars=_ATR_BARS + 5)
            if not bars.empty and len(bars) >= _ATR_PERIOD_WEEKS + 1:
                trs = []
                for i in range(1, len(bars)):
                    h = float(bars.iloc[i]["high"])
                    lo = float(bars.iloc[i]["low"])
                    pc = float(bars.iloc[i - 1]["close"])
                    trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
                atr = sum(trs[-_ATR_PERIOD_WEEKS:]) / _ATR_PERIOD_WEEKS
                atr_stop = current_price - atr * _ATR_MULT
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s insufficient weekly bars post-earnings — "
                    "using hard floor only",
                    pos.symbol,
                )
                atr_stop = current_price * (1 - _HARD_FLOOR_PCT)
            floor_stop = current_price * (1 - _HARD_FLOOR_PCT)
            stop_price = max(atr_stop, floor_stop)
            stop_price = round(stop_price, 2)
            if stop_price <= 0:
                stop_price = round(floor_stop, 2)
            pos.stop_price = stop_price
            stop_side = "sell"  # direction == "long" confirmed above
            order = self._dispatcher.submit_gtc_stop(
                self.broker, pos.symbol, pos.qty_filled, stop_side, stop_price
            )
            if order and hasattr(order, "id"):
                pos.stop_order_id = order.id
                pos.state = HoldState.ACTIVE
                pos.earnings_gate_date = None
                pos.updated_at = self._now_et().isoformat()  # RC-1
                _register_symbol(pos.symbol)
                self._save_state()
                logger.info(
                    "QuarterlyHoldManager: %s post-earnings GTC stop @ $%.2f "
                    "(anchored to current $%.2f, entry was $%.2f)",
                    pos.symbol, stop_price, current_price, pos.avg_entry_price,
                )
                self._alert(
                    f"✅ QHM: {pos.symbol} earnings passed — "
                    f"GTC stop @ ${stop_price:.2f} (current ${current_price:.2f})."
                )
                return True
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s post-earnings stop submit failed — "
                    "PENDING_STOP_REPLACE",
                    pos.symbol,
                )
                self._handle_missing_stop(pos)
                _register_symbol(pos.symbol)  # restore intraday block even on failure
                return False
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: _resubmit_post_earnings_stop error for %s: %s",
                pos.symbol, e,
            )
            _register_symbol(pos.symbol)  # always restore intraday block on error
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
            if bars is None or len(bars) < 2:
                logger.warning(
                    "QuarterlyHoldManager: %s entry gate — insufficient bars",
                    symbol,
                )
                return False
            prior_close = float(bars.iloc[-2]["close"])
            current_close = float(bars.iloc[-1]["close"])
            if prior_close <= 0:
                return False
            passes = current_close > prior_close * gate_pct
            logger.info(
                "QuarterlyHoldManager: %s Day-1 gate: %.2f > %.2f × %.2f → %s",
                symbol, current_close, prior_close, gate_pct,
                "PASS" if passes else "FAIL",
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

    @staticmethod
    def _quarter_tag(today_str: str) -> str:
        """Calendar-quarter tag 'YYYY-Qn' for the dip-add per-quarter counter."""
        _d = datetime.strptime(today_str, "%Y-%m-%d").date()
        return "%d-Q%d" % (_d.year, (_d.month - 1) // 3 + 1)

    def _maybe_dip_add(self, pos: HoldPosition, today_str: str) -> None:
        """DIP-ADD: buy more of a conviction hold on weakness below cost average.
        Two rungs (A -2% small-to-target, B -5% aggressive-to-ceiling), a HARD pre-fill
        %-equity ceiling + a quarterly max-shares cap, a -15% first-entry stop-adding
        floor, and anti-oscillation (max N/quarter, >= K days apart, none within ~7 days
        of the earnings exit). DORMANT unless _DIP_ADD_ENABLED. Never raises into the
        weekly-check caller. Spec: logs/qhm_v2_design_2026-07-11.md PART 1 FINALIZED.
        """
        if not _DIP_ADD_ENABLED or self.dry_run:
            return
        try:
            if (pos.state != HoldState.ACTIVE or pos.qty_filled <= 0
                    or pos.avg_entry_price <= 0 or pos.tranche1_price <= 0):
                return
            live_price = self._get_live_price(pos.symbol)
            if not live_price or live_price <= 0:
                return

            # STOP-ADDING FLOOR (first-entry anchor) — thesis may be re-rating: halt.
            floor_price = pos.tranche1_price * (1 - _DIP_ADD_STOP_FLOOR_PCT)
            if live_price < floor_price:
                logger.warning(
                    "QHM dip-add: %s $%.2f below -%.0f%% first-entry floor ($%.2f) — "
                    "STOP adding, escalate to board for a conviction re-vote.",
                    pos.symbol, live_price, _DIP_ADD_STOP_FLOOR_PCT * 100, floor_price)
                return

            # Trigger off COST AVERAGE. Rung B (deeper) implies Rung A.
            rung_b = live_price <= pos.avg_entry_price * (1 - _DIP_ADD_RUNG_B_PCT)
            rung_a = live_price <= pos.avg_entry_price * (1 - _DIP_ADD_RUNG_A_PCT)
            if not rung_a:
                return

            # Pre-earnings blackout (final ~N days before the earnings exit).
            if pos.earnings_gate_date:
                try:
                    _ed = datetime.strptime(pos.earnings_gate_date, "%Y-%m-%d").date()
                    _days_to_er = (_ed - self._now_et().date()).days
                    if 0 <= _days_to_er <= _DIP_ADD_NO_ADD_DAYS_PRE_EARNINGS:
                        return
                except ValueError:
                    pass

            # Anti-oscillation: reset the per-quarter counter on a new quarter.
            _qtag = self._quarter_tag(today_str)
            if pos.dip_add_quarter_tag != _qtag:
                pos.dip_add_quarter_tag = _qtag
                pos.dip_adds_quarter = 0
            if pos.dip_adds_quarter >= _DIP_ADD_MAX_PER_QUARTER:
                return
            if pos.last_dip_add_date:
                try:
                    _last = datetime.strptime(pos.last_dip_add_date, "%Y-%m-%d").date()
                    if (self._now_et().date() - _last).days < _DIP_ADD_MIN_DAYS_BETWEEN:
                        return
                except ValueError:
                    pass

            equity = self._get_account_equity()
            if equity <= 0:
                return
            cur_notional = pos.qty_filled * live_price
            cur_weight = cur_notional / equity
            target_weight = pos.target_equity_pct
            ceiling_weight = target_weight * _DIP_ADD_CEILING_MULT
            # Rung A caps AT target; Rung B may run up to the ceiling.
            cap_weight = ceiling_weight if rung_b else target_weight
            if cur_weight >= cap_weight - 1e-9:
                return  # already at/above the applicable cap for this rung

            if rung_b:
                # Aggressive: "equal or greater" than existing position (to ceiling).
                add_qty = max(pos.qty_filled, 1)
            else:
                # Rung A: small add toward target only.
                add_qty = int((target_weight * equity - cur_notional) / live_price)
            if add_qty < 1:
                return

            # HARD PRE-FILL %-equity cap: never let (cur + add) exceed cap_weight.
            max_by_weight = int((cap_weight * equity - cur_notional) / live_price)
            add_qty = min(add_qty, max_by_weight)
            # HARD max-shares-per-name cap derived from the ceiling (belt-and-suspenders
            # on this lumpy account where the capital cap binds before the price floor).
            # max_shares_ceiling: total-shares cap from the CEILING (Rung-B bound).
            # For Rung A, max_by_weight (target-based, above) is the tighter binding
            # cap — do not drop it in a refactor and leave only this looser ceiling cap.
            max_shares_ceiling = int((ceiling_weight * equity) / live_price)
            add_qty = min(add_qty, max(max_shares_ceiling - pos.qty_filled, 0))
            if add_qty < 1:
                return

            # MARGIN AFFORDABILITY (BGG unanimous 2026-07-13): the ceiling is
            # sized off EQUITY (real capital at risk), but confirm the add is affordable
            # against RegT (overnight/settled) buying power — a QHM position is held
            # overnight, so RegT is the binding constraint, NOT intraday effective BP.
            # Shrink to what BP allows; fail-CLOSED if BP unreadable or < 1 share.
            try:
                _acct = self.broker.get_account()
                _regt_bp = float(getattr(_acct, "regt_buying_power", None)
                                 or getattr(_acct, "buying_power", 0) or 0)
            except Exception as _bp_e:
                logger.warning("QHM dip-add: %s BP read failed (%s) — skip add",
                               pos.symbol, _bp_e)
                return
            if _regt_bp <= 0:
                # BP exhausted by resting orders / negative on a Reg-T call — cannot
                # afford anything. Fail-CLOSED (cold-2nd threat #7, 2026-07-13).
                logger.warning("QHM dip-add: %s RegT BP <= 0 ($%.0f) — skip add",
                               pos.symbol, _regt_bp)
                return
            if add_qty * live_price > _regt_bp:
                _afford = int(_regt_bp / live_price)
                if _afford < 1:
                    logger.warning(
                        "QHM dip-add: %s unaffordable ($%.0f > RegT BP $%.0f) skip",
                        pos.symbol, add_qty * live_price, _regt_bp)
                    return
                logger.info(
                    "QHM dip-add: %s affordability bounded %d→%d sh (RegT BP $%.0f)",
                    pos.symbol, add_qty, _afford, _regt_bp)
                add_qty = _afford

            # ── OPTION C: stop-safe add (board + Gro + GAI unanimous 2026-07-13) ──
            # A QHM position holds a resting GTC sell-stop; Alpaca blocks a same-symbol
            # BUY (wash-trade). So, RTH-only: cancel the stop -> marketable-limit add ->
            # poll for fill (<=15s) -> resubmit the stop for the ACTUAL held qty (Alpaca
            # truth via _resync). INVARIANT across every branch: never return without a
            # resting stop OR PENDING_STOP_REPLACE + a Slack alert (resubmit_if_needed
            # + startup reconcile are the outer backstops).
            from execution.broker import (
                cancel_order as _cancel_order,
                get_order as _get_order,
                is_market_open as _is_market_open,
            )
            import time as _time
            try:
                if not _is_market_open():
                    logger.info("QHM dip-add: %s market closed — defer", pos.symbol)
                    return
            except Exception as _clk_e:
                logger.warning("QHM dip-add: %s clock check failed (%s) — defer",
                               pos.symbol, _clk_e)
                return

            _stop_side = "sell" if pos.direction == "long" else "buy"
            _orig_qty = pos.qty_filled
            _stop_id = pos.stop_order_id
            limit_price = round(live_price * (1 + _LIMIT_PRICE_TOLERANCE), 2)

            def _restore_or_pending(_qty: int, _reason: str) -> None:
                # Resting stop for _qty, else PENDING_STOP_REPLACE + alert. Never naked.
                _r = None
                try:
                    if _qty >= 1 and pos.stop_price > 0:
                        _r = self._dispatcher.submit_gtc_stop(
                            self.broker, pos.symbol, _qty, _stop_side, pos.stop_price)
                except Exception as _rse:
                    logger.critical("QHM dip-add: %s stop resubmit threw: %s",
                                    pos.symbol, _rse)
                if _r is not None and hasattr(_r, "id"):
                    pos.stop_order_id = _r.id
                    pos.state = HoldState.ACTIVE
                    pos.updated_at = self._now_et().isoformat()
                    self._save_state()
                    return
                try:
                    self._resync_from_alpaca(pos)
                except Exception:
                    pass
                pos.state = HoldState.PENDING_STOP_REPLACE
                pos.updated_at = self._now_et().isoformat()
                self._save_state()
                logger.critical("QHM dip-add: %s %s — PENDING_STOP_REPLACE",
                                pos.symbol, _reason)
                try:
                    self._alert(":rotating_light: QHM %s dip-add %s — stop pending "
                                "resubmit" % (pos.symbol, _reason))
                except Exception:
                    pass

            # Branch 0 — cancel the resting stop. Cancel failure => abort, stop intact.
            if _stop_id:
                if not _cancel_order(str(_stop_id)):
                    logger.warning("QHM dip-add: %s stop cancel failed — abort "
                                   "(stop intact)", pos.symbol)
                    return
                pos.stop_order_id = None
                # Finding #1 (cold-2nd): cancel_order maps "already filled" -> ok, so
                # the stop may have FIRED during the cancel. Never re-buy into a
                # position the stop just exited. Re-check Alpaca truth; if reduced/flat,
                # abort the add and protect only what is actually held.
                try:
                    _pn = self.broker.get_position(pos.symbol)
                    _held = int(float(getattr(_pn, "qty", 0) or 0)) if _pn else 0
                except Exception:
                    _held = _orig_qty  # unknown -> assume unchanged (do not over-react)
                if _held < _orig_qty:
                    logger.warning("QHM dip-add: %s reduced %d->%d during stop cancel "
                                   "(stop likely fired) — abort add", pos.symbol,
                                   _orig_qty, _held)
                    if _held >= 1:
                        try:
                            self._resync_from_alpaca(pos)
                        except Exception:
                            pass
                        _restore_or_pending(pos.qty_filled, "stop-fired-during-cancel")
                    # _held == 0: flat — no stop needed; external-close cleans up.
                    return

            # Submit the marketable-limit add (0.1% over live => crosses, fills fast).
            try:
                _add = self._dispatcher.submit_limit(
                    self.broker, pos.symbol, add_qty,
                    "buy" if pos.direction == "long" else "sell_short", limit_price)
            except Exception as _ae:
                logger.warning("QHM dip-add: %s add threw (%s)", pos.symbol, _ae)
                _add = None
            if not (_add is not None and hasattr(_add, "id")):
                # Branch 1 — add failed after the cancel: restore the ORIGINAL stop now.
                _restore_or_pending(_orig_qty, "add-failed")
                return

            # Poll for fill: 2s initial + 1s polls to a 15s monotonic deadline.
            _deadline = _time.monotonic() + 15.0
            _time.sleep(2)
            _filled = 0
            while _time.monotonic() < _deadline:
                try:
                    _o = _get_order(str(_add.id))
                    _st = str(getattr(_o, "status", "")).lower()
                    _filled = int(float(getattr(_o, "filled_qty", 0) or 0))
                    if _st in ("filled", "canceled", "cancelled", "rejected",
                               "expired"):
                        break
                except Exception:
                    pass
                _time.sleep(1)

            # Branch 2 — not fully filled: cancel the add (partials keep filled shares).
            if _filled < add_qty:
                try:
                    _cancel_order(str(_add.id))
                except Exception:
                    pass
            # Resync to Alpaca truth (full/partial/no fill), resubmit the stop for the
            # ACTUAL held qty. Branch 3 (resubmit fails) => PENDING inside the helper.
            try:
                self._resync_from_alpaca(pos)
            except Exception:
                pass
            _restore_or_pending(pos.qty_filled, "post-add stop resubmit")
            if _filled >= 1:
                pos.dip_adds_quarter += 1
                pos.last_dip_add_date = today_str
                self._save_state()
                logger.warning(
                    "QHM DIP-ADD OK: %s rung %s +%d filled — stop for %d @ $%.2f "
                    "(#%d/%d qtr)", pos.symbol, "B" if rung_b else "A", _filled,
                    pos.qty_filled, pos.stop_price, pos.dip_adds_quarter,
                    _DIP_ADD_MAX_PER_QUARTER)
        except Exception as e:  # RC-3
            logger.warning("QHM dip-add error for %s: %s", pos.symbol, e)

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
            if not bars.empty and len(bars) >= _ATR_PERIOD_WEEKS + 1:
                trs = []
                for i in range(1, len(bars)):
                    h = float(bars.iloc[i]["high"])
                    lo = float(bars.iloc[i]["low"])
                    pc = float(bars.iloc[i - 1]["close"])
                    tr = max(h - lo, abs(h - pc), abs(lo - pc))
                    trs.append(tr)
                atr = sum(trs[-_ATR_PERIOD_WEEKS:]) / _ATR_PERIOD_WEEKS
                atr_stop = pos.avg_entry_price - atr * _ATR_MULT
            else:
                logger.warning(
                    "QuarterlyHoldManager: %s insufficient weekly bars (%d) for ATR — "
                    "using hard floor only",
                    pos.symbol, len(bars) if not bars.empty else 0,
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
                _atr_valid = not bars.empty and len(bars) >= _ATR_PERIOD_WEEKS
                _atr = atr_stop if _atr_valid else 0
                logger.info(
                    "QuarterlyHoldManager: %s GTC stop @ $%.2f submitted "
                    "(ATR=%.2f, floor=%.2f, entry=%.2f)",
                    pos.symbol, stop_price, _atr, floor_stop, pos.avg_entry_price,
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
            if not success:
                # AWP audit fix (2026-06-28): close_position() already treats
                # "position not found" as success=True (see execution/broker.py)
                # — a False here means a REAL failure (rejected order, API
                # error), and the position is still actually open at the
                # broker. The old code marked it CLOSED anyway and
                # unconditionally deregistered the symbol, which would (a)
                # permanently stop QHM from ever managing/reconciling this
                # position again (CLOSED positions are skipped by
                # _detect_external_close() and run_weekly_check()), and (b)
                # immediately unblock the symbol for the intraday MTF bot to
                # enter a SEPARATE position in the same name while this
                # quarterly hold was still actually open. Leave state and
                # registration untouched so the next run_weekly_check() cycle
                # naturally retries (days_held >= _MAX_HOLD_CALENDAR_DAYS
                # will still be true).
                logger.error(
                    "QuarterlyHoldManager: %s exit attempt (%s) FAILED — "
                    "position remains open and managed. Will retry next cycle.",
                    pos.symbol, reason,
                )
                self._alert(
                    f"⚠️ QHM: {pos.symbol} exit attempt FAILED ({reason}) — "
                    f"position still open. Will retry next cycle. "
                    f"Manual review if this persists."
                )
                return
            pos.state = HoldState.PENDING_EXIT
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
                except Exception:  # RC-3
                    logger.debug("QHM: notional fetch error for %s — using 0", sym)
        return total

    def _get_live_price(self, symbol: str) -> Optional[float]:
        """Get current price via broker position; fallback to Alpaca Data."""
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
            # No position yet (PENDING_ENTRY) — fetch live trade price from Alpaca Data
            from data.alpaca_data import get_latest_trade as _glt
            trade_price = _glt(symbol)
            if trade_price:
                logger.debug("QHM: live price %s via data: %.4f", symbol, trade_price)
                return float(trade_price)
            return None
        except Exception as e:  # RC-3
            logger.warning(
                "QuarterlyHoldManager: get_live_price failed for %s: %s", symbol, e
            )
            return None

    def _get_open_orders(self) -> "list | None":
        """Fetch open orders via broker module-level function.

        B2 fix (2026-07-02, board+Gro+GAI): returns None on API failure —
        DISTINCT from [] (genuinely empty book). The old `or []` conversion
        made a transient API error indistinguishable from "no orders", so
        _adopt_existing_stop flagged LIVE stops as missing at restart and
        stranded holds in PENDING_STOP_REPLACE (confirmed live 2026-07-02:
        both QHM stops resting on Alpaca while state said stop-missing).
        Callers must treat None as UNKNOWN and never change state on it.
        """
        try:
            from execution.broker import get_open_orders
            return get_open_orders()  # broker returns None on API error
        except Exception as e:  # RC-3
            logger.warning("QuarterlyHoldManager: get_open_orders failed: %s", e)
            return None

    def _alert(self, message: str) -> None:
        """Fire Slack alert via injected alerter. Fail-soft (RC-3)."""
        if not self.alerter:
            return
        try:
            self.alerter.send(message)
        except Exception as e:  # RC-3
            logger.warning("QuarterlyHoldManager: alerter failed: %s", e)
