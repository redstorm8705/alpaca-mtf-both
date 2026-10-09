#!/usr/bin/env python3
# ruff: noqa: E501
"""#12c opposite-signal exit (2026-10-08 EWY): a Swing short held with a resting RTH DAY buy stop; a 10/12 long signal
tried to exit with a plain market BUY, Alpaca rejected it (40310000 held_for_orders) and the reversal exit never
happened. The exit now uses the standard close path (confirmed GTC cancel -> broker.close_position, which clears the
DAY stop on 40310000) and the real Alpaca fill."""
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import entry_logic as el

TRADE = {"symbol": "EWY", "direction": "short", "qty": 1, "entry_price": 181.46, "stop": 181.46,
         "rth_day_stop_order_id": "8e55d4fb", "gtc_stop_order_id": None}


class Exit12c(unittest.TestCase):
    def _run(self, cancel_ok=True, close_ok=True, fill=180.10):
        tracker = mock.Mock()
        tracker.record_exit.return_value = 1.36
        risk = mock.Mock()
        mri = SimpleNamespace(level=lambda: "STRESSED")
        with mock.patch.object(el, "_cancel_open_gtc_orders", return_value=cancel_ok) as cancel, \
                mock.patch.object(el, "close_position", return_value=close_ok) as close, \
                mock.patch.object(el, "_fetch_actual_fill_price", return_value=fill) as fill_fn, \
                mock.patch.object(el, "submit_market_order") as mkt, \
                mock.patch.object(el, "send_slack") as slack:
            out = el._exit_on_opposite_signal("EWY", dict(TRADE), tracker, risk, mri)
        self.slack = slack
        return out, tracker, risk, cancel, close, fill_fn, mkt

    def test_exits_through_close_position_with_the_real_fill(self):
        out, tracker, risk, _c, close, fill_fn, mkt = self._run()
        self.assertEqual(out, "exited")
        self.slack.assert_not_called()
        close.assert_called_once_with("EWY")
        mkt.assert_not_called()                                   # no plain market order any more
        tracker.record_exit.assert_called_once()
        self.assertEqual(tracker.record_exit.call_args.args[:2], ("EWY", 180.10))
        self.assertEqual(tracker.record_exit.call_args.kwargs["reason"], "opposite_signal")
        self.assertEqual(tracker.record_exit.call_args.kwargs["mri_level"], "STRESSED")
        risk.register_close.assert_called_once_with(1.36)
        self.assertIsNotNone(fill_fn.call_args.kwargs.get("submitted_after"))

    def test_unconfirmed_gtc_cancel_does_not_close(self):
        out, tracker, risk, _c, close, _f, _m = self._run(cancel_ok=False)
        self.assertEqual(out, "cancel_unconfirmed")
        close.assert_not_called()
        tracker.record_exit.assert_not_called()
        risk.register_close.assert_not_called()

    def test_failed_close_records_nothing(self):
        out, tracker, risk, _c, _cl, fill_fn, _m = self._run(close_ok=False)
        self.assertEqual(out, "close_failed")
        self.slack.assert_called_once()                            # a possibly-unprotected lot pages a human
        fill_fn.assert_not_called()
        tracker.record_exit.assert_not_called()
        risk.register_close.assert_not_called()

    def test_none_pnl_registers_zero(self):
        tracker = mock.Mock()
        tracker.record_exit.return_value = None
        risk = mock.Mock()
        with mock.patch.object(el, "_cancel_open_gtc_orders", return_value=True), \
                mock.patch.object(el, "close_position", return_value=True), \
                mock.patch.object(el, "_fetch_actual_fill_price", return_value=181.0):
            self.assertEqual(el._exit_on_opposite_signal("EWY", dict(TRADE), tracker, risk, None), "exited")
        risk.register_close.assert_called_once_with(0.0)
        self.assertEqual(tracker.record_exit.call_args.kwargs["mri_level"], "NORMAL")


if __name__ == "__main__":
    unittest.main()
