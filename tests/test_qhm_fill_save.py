# ruff: noqa: E501
"""QHM-FILL-SAVE-1102. Live 2026-11-02: GOOGL tranche 1 filled and QHM placed its GTC stop, but
maybe_enter_positions saved state only `if entered:`. A tranche-1 fill submits no new order, so the
booked fill and the stop order id stayed in memory for ~10 minutes (ledger booked the share as intraday,
two CRITICAL pages). A restart in that window reads AWAITING_FILL with no stop id, cancels the live QH stop
as stray and rebuilds it. A booked fill (or any change to tranches_filled) now saves state at once."""
import unittest
from datetime import datetime

from execution import quarterly_hold_manager as qm

NOW = datetime(2026, 11, 2, 13, 6, tzinfo=qm.ET)


def _mgr(pos, mode):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m._clock = lambda: NOW
    m._positions = {pos.symbol: pos}
    m.saved = []

    def _save():
        m.saved.append((pos.state, pos.stop_order_id, pos.qty_filled))
        return True
    m._save_state = _save  # type: ignore[method-assign]
    m._is_tranche_due = lambda p, d: True  # type: ignore[method-assign]

    def _fill(p):
        if mode == "none":
            return False
        p.qty_filled, p.tranches_filled = 1, 1
        p.stop_order_id, p.state = "stop-1", qm.HoldState.ACTIVE
        if mode == "raised_after_stop":   # the helper's own except returns False after mutating
            return False
        p.tranche = 2
        return True
    m._check_fill_and_advance = _fill  # type: ignore[method-assign]
    return m


def _pos(symbol="GOOGL"):
    return qm.HoldPosition(symbol=symbol, direction="long", target_equity_pct=0.15,
                           state=qm.HoldState.AWAITING_FILL, tranche=1, tranches_filled=0,
                           entry_order_id="c799ac62", entry_day="2026-11-02", tranche1_price=283.15)


class FillSave(unittest.TestCase):
    def test_tranche1_fill_saves_state_with_nothing_entered(self):
        pos = _pos()
        m = _mgr(pos, "filled")
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.saved, [(qm.HoldState.ACTIVE, "stop-1", 1)])

    def test_mutation_without_true_return_still_saves(self):
        pos = _pos()
        m = _mgr(pos, "raised_after_stop")
        m.maybe_enter_positions()
        self.assertEqual(m.saved, [(qm.HoldState.ACTIVE, "stop-1", 1)])

    def test_no_fill_no_save(self):
        pos = _pos()
        m = _mgr(pos, "none")
        self.assertEqual(m.maybe_enter_positions(), [])
        self.assertEqual(m.saved, [])
        self.assertEqual(pos.state, qm.HoldState.AWAITING_FILL)


if __name__ == "__main__":
    unittest.main()
