#!/usr/bin/env python3
# ruff: noqa: E501
"""Day-tier daily DOLLAR risk budget (Rafael 2026-10-04) — replaces the 3-position count cap. A new entry must fit
realized day-tier loss + every open lot's loss-if-stopped + its own risk (open/new scaled for gap-through) inside
DAYTRADE_TIER_KILL_EQUITY_PCT x start-of-day equity. No count cap, no sector/correlation cap."""
import unittest
from unittest import mock

import config
from execution import day_trade_manager as dtm


class BudgetFit(unittest.TestCase):
    # $2,500 equity: budget = 5% = $125; a full-size trade risks 1.5% = $37.50; gap-through allowance 1.2.
    def test_first_trade_fits_whole(self):
        self.assertEqual(dtm._budget_fit_qty(10, 3.75, 125.0, 0.0, 0.0, 1.2), 10)   # 10 x 3.75 x 1.2 = 45

    def test_shrinks_to_remaining_room(self):
        # two full-size lots open (75 at stop) -> room = 125 - 1.2*75 = 35 -> floor(35 / 4.5) = 7 sh
        self.assertEqual(dtm._budget_fit_qty(10, 3.75, 125.0, 0.0, 75.0, 1.2), 7)

    def test_skip_when_budget_full(self):
        self.assertEqual(dtm._budget_fit_qty(10, 3.75, 125.0, 0.0, 105.0, 1.2), 0)   # 126 > 125

    def test_realized_losses_count_unscaled(self):
        # $100 already lost today -> room $25 -> floor(25 / 4.5) = 5 sh
        self.assertEqual(dtm._budget_fit_qty(10, 3.75, 125.0, 100.0, 0.0, 1.2), 5)

    def test_many_small_trades_allowed(self):
        # no count cap: eight small lots ($10 at stop each = $80) still leave room for a ninth small one
        self.assertEqual(dtm._budget_fit_qty(2, 1.0, 125.0, 0.0, 80.0, 1.2), 2)

    def test_exact_multiple_lands_on_budget(self):
        # 45 / (1.2 x 3.75) = 10 exactly -> 10 shares fill the budget to the dollar
        self.assertEqual(dtm._budget_fit_qty(20, 3.75, 45.0, 0.0, 0.0, 1.2), 10)

    def test_exact_boundary_fits(self):
        # 125 - 1.2*0 = 125 ; 1.2 x 3.75 x n <= 125 -> n = floor(27.78) = 27
        self.assertEqual(dtm._budget_fit_qty(100, 3.75, 125.0, 0.0, 0.0, 1.2), 27)

    def test_invalid_inputs_fail_closed(self):
        for args in [(10, 0.0, 125, 0, 0, 1.2), (10, 3.75, 0, 0, 0, 1.2), (10, 3.75, 125, -1, 0, 1.2),
                     (10, 3.75, 125, 0, -1, 1.2), (10, 3.75, 125, 0, 0, 0.9), (0, 3.75, 125, 0, 0, 1.2),
                     (10, float("nan"), 125, 0, 0, 1.2), (10, 3.75, float("inf"), 0, 0, 1.2)]:
            self.assertEqual(dtm._budget_fit_qty(*args), 0, args)


class RiskUsed(unittest.TestCase):
    def _run(self, open_trades, events, state=None, realized=(0.0, True), ev_ok=True):
        with mock.patch.object(dtm, "_realized_loss_today", return_value=realized), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(events, ev_ok)), \
                mock.patch.object(dtm, "_now_et", return_value=dtm.datetime(2026, 10, 5, 11, 0, tzinfo=dtm.ET)):
            return dtm._daily_risk_used(open_trades, state or {}, 50.0)

    def test_open_lot_counts_entry_to_stop(self):
        ot = {"T1": {"trade_id": "T1", "fill_qty": 10, "entry_price": 100.0}}
        ev = [{"event": "stop_placed", "trade_id": "T1", "stop_price": 96.25}]
        realized, open_risk, _ = self._run(ot, ev)
        self.assertEqual(realized, 0.0)
        self.assertAlmostEqual(open_risk, 37.5)

    def test_short_lot(self):
        ot = {"T2": {"trade_id": "T2", "fill_qty": 5, "entry_price": 50.0}}
        ev = [{"event": "stop_placed", "trade_id": "T2", "stop_price": 52.0}]
        self.assertAlmostEqual(self._run(ot, ev)[1], 10.0)

    def test_unknown_stop_counts_at_ceiling(self):
        ot = {"T3": {"trade_id": "T3", "fill_qty": 10, "entry_price": 100.0}}
        self.assertAlmostEqual(self._run(ot, [])[1], 50.0)

    def test_unreadable_event_log_counts_every_lot_at_ceiling(self):
        ot = {"A": {"trade_id": "A", "fill_qty": 1, "entry_price": 10.0},
              "B": {"trade_id": "B", "fill_qty": 1, "entry_price": 10.0}}
        ev = [{"event": "stop_placed", "trade_id": "A", "stop_price": 9.9}]
        self.assertAlmostEqual(self._run(ot, ev, ev_ok=False)[1], 100.0)

    def test_unlogged_same_day_state_record_counts_at_ceiling(self):
        st = {"entry::NVDA::20261005-1045": {"state": "submitted", "bar_id": "20261005-1045", "coid": "X"},
              "entry::AMD::20261004-1045": {"state": "submitted", "bar_id": "20261004-1045", "coid": "Y"},   # prior day
              "entry::MU::20261005-1000": {"state": "protected", "bar_id": "20261005-1000", "coid": "T1"}}  # logged
        ot = {"T1": {"trade_id": "T1", "fill_qty": 10, "entry_price": 100.0}}
        ev = [{"event": "stop_placed", "trade_id": "T1", "stop_price": 96.25}]
        self.assertAlmostEqual(self._run(ot, ev, state=st)[1], 37.5 + 50.0)

    def test_closed_lot_with_stale_protected_record_is_not_phantom_risk(self):
        st = {"entry::NVDA::20261005-1000": {"state": "protected", "bar_id": "20261005-1000", "coid": "W1"},
              "entry::AMD::20261005-1015": {"state": "protected", "bar_id": "20261005-1015", "coid": "W2"}}
        ev = [{"event": "entry_fill", "trade_id": "W1"}, {"event": "exit_fill", "trade_id": "W1", "realized_pnl": 4.0},
              {"event": "entry_fill", "trade_id": "W2"}, {"event": "exit_fill", "trade_id": "W2", "realized_pnl": 2.0}]
        self.assertEqual(self._run({}, ev, state=st)[1], 0.0)

    def test_stale_record_counts_when_log_unreadable(self):
        st = {"entry::NVDA::20261005-1000": {"state": "protected", "bar_id": "20261005-1000", "coid": "W1"}}
        self.assertEqual(self._run({}, [], state=st, ev_ok=False)[1], 50.0)

    def test_realized_loss_reported_positive(self):
        self.assertEqual(self._run({}, [], realized=(-42.0, True))[0], 42.0)

    def test_unreadable_realized_journal_returns_none(self):
        self.assertIsNone(self._run({}, [], realized=(0.0, False)))


class AllocatorDenialRetires(unittest.TestCase):
    def test_submitting_record_is_retired(self):
        st = {"entry::NVDA::20261005-1045": {"state": "submitting", "bar_id": "20261005-1045", "coid": "X"}}
        with mock.patch.object(dtm, "_save_state", return_value=True) as save:
            self.assertTrue(dtm._retire_unsubmitted(st, "entry::NVDA::20261005-1045"))
        self.assertEqual(st["entry::NVDA::20261005-1045"]["state"], "submit_failed")
        save.assert_called_once()

    def test_later_states_are_never_touched(self):
        for s_ in ("submitted", "filled", "protected", "fill_unverified"):
            st = {"k": {"state": s_}}
            with mock.patch.object(dtm, "_save_state", return_value=True) as save:
                self.assertFalse(dtm._retire_unsubmitted(st, "k"))
            self.assertEqual(st["k"]["state"], s_)
            save.assert_not_called()

    def test_retired_record_no_longer_counts(self):
        st = {"entry::NVDA::20261005-1045": {"state": "submit_failed", "bar_id": "20261005-1045", "coid": "X"}}
        r = RiskUsed()._run({}, [], state=st)
        self.assertEqual(r[1], 0.0)

    def test_place_entry_retires_on_allocator_denial(self):
        import inspect
        src = inspect.getsource(dtm.place_entry)
        deny = src[src.index("if not _capital.approved:"):src.index("order = broker.submit_limit_order(")]
        self.assertIn("_retire_unsubmitted(state, key)", deny)


class ConfigShape(unittest.TestCase):
    def test_count_cap_is_gone_and_allowance_is_strict(self):
        self.assertFalse(hasattr(config, "DAYTRADE_MAX_CONCURRENT_POSITIONS"))
        self.assertGreaterEqual(config.DAYTRADE_BUDGET_SLIPPAGE_MULT, 1.0)

    def test_place_entry_has_no_count_cap(self):
        import inspect
        src = inspect.getsource(dtm.place_entry)
        self.assertNotIn("DAYTRADE_MAX_CONCURRENT_POSITIONS", src)
        self.assertIn("_budget_fit_qty(", src)


if __name__ == "__main__":
    unittest.main()
