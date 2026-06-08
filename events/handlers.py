"""
events/handlers.py
Mass-close event handler — extracted from main.py Phase 2.

Owns:
  - _halt_entries_for_session   H-6: set True after NEWS HALT; block new entries
  - get_halt_entries()          read accessor
  - set_halt_entries(value)     write accessor (startup restore + daily reset)
  - safe_close_all()            close all positions on halt/circuit-breaker event

Broker imports: close_position from execution.broker.
Fill price: fetch_actual_fill_price from execution.fill_helpers.
"""

import logging

import config
from execution.broker import close_position
from execution.fill_helpers import fetch_actual_fill_price

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# H-6 session halt flag — set True after safe_close_all (NEWS HALT)
# ---------------------------------------------------------------------------

_halt_entries_for_session: bool = False


def get_halt_entries() -> bool:
    """Return current session halt state. Called from execute_entries() gate."""
    return _halt_entries_for_session


def set_halt_entries(value: bool) -> None:
    """Set session halt state. Called at startup restore and daily reset."""
    global _halt_entries_for_session
    _halt_entries_for_session = value


# ---------------------------------------------------------------------------
# Mass-close handler
# ---------------------------------------------------------------------------

def safe_close_all(tracker, risk=None,
                   circuit_breaker: bool = False, mri_level: str = "NORMAL") -> None:
    """Close all positions, with optional Bucket A same-day exemption.

    circuit_breaker=False (default — routine news/HALT signal):
      Bucket A (TQQQ/SQQQ/TSLL) same-day opens are protected from closure.
      These are intended hedges; closing them into a news spike burns a PDT slot
      on the hedge leg. The internal stop continues to manage risk.

    circuit_breaker=True (exchange halt, news_size_mult==0.0, or user shutdown):
      No exemptions. All positions closed unconditionally.
      Board ruling (Apr 8 2026): 3x leverage gaps are uncontrollable during a true
      circuit-breaker event. The hedge rationale does not apply — close everything.

    H-6: Records each exit in tracker + calls risk.register_close() per position.
         Sets _halt_entries_for_session=True to block new entries for remainder of RTH.
    """
    global _halt_entries_for_session

    # Bucket A exemption only applies to routine halts, never circuit-breakers
    protected = [] if circuit_breaker else [
        s for s in tracker.open_trades
        if s in config.BUCKET_A_TICKERS and tracker.opened_today(s)
    ]
    symbols_to_close = [
        s for s in list(tracker.open_trades)
        if s not in protected
    ]

    if protected:
        logger.warning(
            f"[HARD RULE] Bucket A same-day close blocked during mass exit: "
            f"{protected} — closing all other positions individually."
        )

    if symbols_to_close:
        import time as _time
        for sym in symbols_to_close:
            trade = tracker.open_trades.get(sym, {})
            # P5-H2: anchor before close to filter stale fills
            _close_ts = _time.time()
            success = close_position(sym)
            if success:
                # SF-02: fetch actual Alpaca fill price (poll 1.2 s for settlement)
                # instead of fabricating the stored stop level as exit price.
                exit_price = fetch_actual_fill_price(sym, trade, poll_secs=1.2,
                                                     submitted_after=_close_ts)
                pnl = tracker.record_exit(sym, exit_price, reason="safe_close_all",
                                          mri_level=mri_level)
                if risk is not None:
                    risk.register_close(pnl or 0.0)
    else:
        # Finding 10: symbols_to_close is empty — nothing left to close.
        logger.info(
            f"safe_close_all: no non-protected positions to close "
            f"(protected: {protected})."
        )

    # H-12: Only halt new entries if at least one position was actually closed.
    # If safe_close_all() was called but only protected Bucket A positions exist
    # (symbols_to_close was empty), no closure occurred — locking out entries for
    # the rest of RTH would be wrong and unnecessary.
    _any_closed = bool(symbols_to_close)   # True if we attempted to close anything
    if _any_closed:
        _halt_entries_for_session = True
        logger.warning(
            "safe_close_all complete — new entries HALTED for remainder of RTH."
        )
        # BUG-ADV-1: persist halt state so watchdog os.execv() restart restores it
        from execution.risk_manager import _save_kill_state as _persist_session_halt
        _persist_session_halt(killed=risk.killed if risk else False, halt_entries=True)
    else:
        logger.info(
            "safe_close_all: no positions closed (all protected) — "
            "entries NOT halted. Bucket A stops remain active."
        )
