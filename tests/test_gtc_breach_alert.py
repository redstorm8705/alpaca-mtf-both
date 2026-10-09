#!/usr/bin/env python3
# ruff: noqa: E501
"""Cover-on-breach Slack alert (AAPL 2026-10-09): a long opened at $331.69 below its $335.23 stop; the bot covered at
market (real fill $332.09, -$12.61) but Slack said "P&L $0.00" (the entry-price fallback before the fill was
confirmed) and called it a "gap-up". The alert now names the right gap and never pages a placeholder P&L."""
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import gtc_manager as gm


class _Tracker:
    def __init__(self, trade):
        self.open_trades = {"AAPL": trade}
        self.exits = []

    def record_exit(self, sym, px, reason=""):
        self.exits.append((sym, px, reason))
        return round((px - 335.24) * 4, 2)

    def _save_log(self):
        pass


def _run(direction="long", stop=335.24, mkt=331.85, fill=335.24, unverified=True):
    trade = {"direction": direction, "stop": stop, "qty": 4, "overnight": True, "atr_value": 6.25}
    tr = _Tracker(trade)
    sent = []

    def _fill(sym, t, poll_secs=0.0, submitted_after=None):
        if unverified:
            t["_fill_unverified"] = True
        return fill

    gm._rth_day_stops_submitted_dates.clear()
    with mock.patch.object(gm, "get_open_orders", return_value=[]), \
            mock.patch.object(gm, "get_open_position", return_value=SimpleNamespace(current_price=mkt)), \
            mock.patch.object(gm, "close_position", return_value=True) as close, \
            mock.patch.object(gm, "fetch_actual_fill_price", side_effect=_fill), \
            mock.patch.object(gm, "send_slack", side_effect=sent.append), \
            mock.patch.object(gm.random, "uniform", return_value=0.01):
        gm.submit_rth_day_stops(tr, risk=mock.Mock())
    return sent, close, tr


class BreachAlert(unittest.TestCase):
    def test_unverified_fill_never_pages_zero_pnl(self):
        sent, close, tr = _run(unverified=True)
        close.assert_called_once_with("AAPL")
        self.assertEqual(len(sent), 1)
        self.assertIn("gap-down", sent[0])
        self.assertIn("pending reconciliation", sent[0])
        self.assertNotIn("P&L $0.00", sent[0])

    def test_verified_fill_reports_real_pnl(self):
        sent, _c, _t = _run(fill=332.09, unverified=False)
        self.assertIn("covered @ $332.09", sent[0])
        self.assertIn("P&L $-12.60", sent[0])
        self.assertIn("gap-down", sent[0])

    def test_short_breach_is_a_gap_up(self):
        sent, _c, _t = _run(direction="short", stop=100.0, mkt=104.0, fill=104.1, unverified=False)
        self.assertIn("gap-up", sent[0])


if __name__ == "__main__":
    unittest.main()
