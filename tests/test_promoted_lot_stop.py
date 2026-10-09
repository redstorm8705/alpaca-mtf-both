#!/usr/bin/env python3
# ruff: noqa: E501
"""Promoted Day-tier lots keep their Swing stop (CEO 2026-10-09, AAPL): the MRI break-even push skips them, and
orphan adoption tags a lot whose most recent opening fill is a DT- (Day tier) order."""
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import lifecycle as lc
from execution import orphan_manager as om


class _T:
    def __init__(self, trades):
        self.open_trades = trades

    def _save_log(self):
        pass


class Predicate(unittest.TestCase):
    def test_flagged_lot_skips_and_logs_once(self):
        t = {"_promoted_from_day_tier": True, "stop": 327.74, "entry_price": 335.24}
        with mock.patch.object(lc, "_log_trade_event") as ev:
            self.assertTrue(lc.promoted_lot_keeps_swing_stop("AAPL", t))
            self.assertTrue(lc.promoted_lot_keeps_swing_stop("AAPL", t))
        ev.assert_called_once()
        self.assertEqual(ev.call_args.kwargs["reason"], "promoted_from_day_tier")

    def test_unflagged_or_killed(self):
        self.assertFalse(lc.promoted_lot_keeps_swing_stop("AAPL", {}))
        with mock.patch.object(lc.config, "SWING_PROMOTED_LOT_NO_BE", False, create=True):
            self.assertFalse(lc.promoted_lot_keeps_swing_stop("AAPL", {"_promoted_from_day_tier": True}))


class MriPush(unittest.TestCase):
    def test_promoted_lot_is_not_pushed_to_break_even(self):
        t = {"_promoted_from_day_tier": True, "entry_price": 335.24, "direction": "long", "atr_value": 6.25,
             "stop": 327.74}
        mri = SimpleNamespace(level=lambda: "STRESSED", score=lambda: 70)
        with mock.patch.object(lc, "fetch_bars", side_effect=AssertionError("no price read for a skipped lot")), \
                mock.patch.object(lc, "_log_trade_event"):
            lc.apply_mri_breakeven_push(_T({"AAPL": t}), mri)
        self.assertEqual(t["stop"], 327.74)
        self.assertNotIn("be_pushed_by_mri", t)


def _ord(side, coid, filled_at):
    return SimpleNamespace(side=SimpleNamespace(value=side), client_order_id=coid, filled_at=filled_at)


class AdoptionTag(unittest.TestCase):
    def _check(self, orders, direction="long"):
        client = mock.Mock()
        client.get_orders.return_value = orders
        with mock.patch("execution.broker.get_trading_client", return_value=client):
            return om._entry_from_day_tier("AAPL", direction)

    def test_day_tier_buy_is_promoted(self):
        self.assertTrue(self._check([_ord("buy", "DT-AAPL-b-1-abc", "2026-10-07T14:12:14Z")]))

    def test_latest_opening_fill_decides(self):
        orders = [_ord("buy", "DT-AAPL-b-1-abc", "2026-10-07T14:12:14Z"),
                  _ord("buy", "IN-AAPL-b-2-def", "2026-10-08T15:00:00Z")]
        self.assertFalse(self._check(orders))

    def test_closing_side_ignored_and_short_mirrors(self):
        orders = [_ord("sell", "DT-EWY-s-1-abc", "2026-10-07T14:00:00Z"),
                  _ord("buy", "IN-EWY-b-2-def", "2026-10-08T15:00:00Z")]
        self.assertTrue(self._check(orders, direction="short"))

    def test_untagged_or_read_failure_is_not_promoted(self):
        self.assertFalse(self._check([_ord("buy", "abc-123", "2026-10-07T14:12:14Z")]))
        with mock.patch("execution.broker.get_trading_client", side_effect=RuntimeError("api down")):
            self.assertFalse(om._entry_from_day_tier("AAPL", "long"))


if __name__ == "__main__":
    unittest.main()
