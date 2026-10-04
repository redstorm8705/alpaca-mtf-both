#!/usr/bin/env python3
# ruff: noqa: E501
"""Overnight broker stops are never looser than the trade's trailing stop (Claude 2026-10-03).

orphan_manager Patch 1 re-submitted `trade["stop"]` while its adoption check compared against
`trail_stop or stop`: a ratcheted trade (META 2026-09-04: trail $605.86, stop $576.48) got its
broker stop re-placed BELOW the trail, cancelled + resubmitted every closed-market cycle.
Runs where the Alpaca SDK is installed (OCI / CI); skipped otherwise.
"""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from execution import orphan_manager as om
except ModuleNotFoundError as _e:  # pragma: no cover — local Mac without alpaca-py
    om = None
    _SKIP = str(_e)


class _Tracker:
    def __init__(self, trades):
        self.open_trades = trades
        self.gtc = {}

    def get_overnight_gtc_positions(self):
        # non-empty (the function returns early on an empty list); an entry with no stored
        # order id is skipped by the reconcile loop, so only Patch 1 acts
        return [("ZZZ", {"gtc_stop_order_id": None})]

    def set_gtc_stop_order_id(self, sym, oid):
        self.open_trades[sym]["gtc_stop_order_id"] = oid

    def _save_log(self):
        pass


@unittest.skipIf(om is None, "alpaca-py not installed")
class Patch1UsesTrail(unittest.TestCase):
    def _run(self, trade, live_px):
        tracker = _Tracker({"META": dict(trade)})
        sent = []

        def _submit(sym, qty, side, px):
            sent.append((sym, qty, side, px))
            return SimpleNamespace(id="new-stop")

        fake_data = SimpleNamespace(get_latest_trade=lambda s: live_px)
        with mock.patch.object(om, "get_tod_phase", return_value="closed"), \
                mock.patch.object(om, "_is_trading_day_today", return_value=True), \
                mock.patch.object(om, "get_open_orders", return_value=[]), \
                mock.patch.object(om, "get_open_position", return_value=SimpleNamespace(qty=2)), \
                mock.patch.object(om, "submit_gtc_stop_order", side_effect=_submit), \
                mock.patch.object(om, "send_slack"), mock.patch.object(om, "alert_gtc_failed"), \
                mock.patch.dict(sys.modules, {"data.alpaca_data": fake_data}):
            om.cancel_and_reconcile_gtc_stops(tracker)
        return sent, tracker

    def test_trail_beyond_entry_is_the_submitted_level(self):
        sent, _ = self._run({"status": "open", "direction": "long", "qty": 2, "qty_remaining": 2,
                             "entry_price": 576.48, "stop": 576.48, "trail_stop": 605.86,
                             "gtc_stop_order_id": None}, live_px=612.0)
        self.assertEqual(sent, [("META", 2, "sell", 605.86)])

    def test_no_trail_uses_stop(self):
        sent, _ = self._run({"status": "open", "direction": "short", "qty": 1, "qty_remaining": 1,
                             "entry_price": 71.60, "stop": 71.60, "trail_stop": None,
                             "gtc_stop_order_id": None}, live_px=67.0)
        self.assertEqual(sent, [("META", 1, "buy", 71.60)])

    def test_trail_through_market_falls_back_to_stop_floor(self):
        # trail $605.86 is above live $600 (a sell stop there is invalid): keep a broker floor at
        # the plain stop instead of leaving the position with no broker stop overnight
        sent, tr = self._run({"status": "open", "direction": "long", "qty": 2, "qty_remaining": 2,
                              "entry_price": 576.48, "stop": 576.48, "trail_stop": 605.86,
                              "gtc_stop_order_id": None}, live_px=600.0)
        self.assertEqual(sent, [("META", 2, "sell", 576.48)])
        self.assertFalse(tr.open_trades["META"].get("internal_hard_stop_active"))

    def test_both_levels_through_market_still_skips_with_flag(self):
        sent, tr = self._run({"status": "open", "direction": "long", "qty": 2, "qty_remaining": 2,
                              "entry_price": 576.48, "stop": 576.48, "trail_stop": 605.86,
                              "gtc_stop_order_id": None}, live_px=570.0)
        self.assertEqual(sent, [])
        self.assertTrue(tr.open_trades["META"].get("internal_hard_stop_active"))


if __name__ == "__main__":
    unittest.main()
