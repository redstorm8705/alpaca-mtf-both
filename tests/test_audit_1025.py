#!/usr/bin/env python3
# ruff: noqa: E501
"""AUDIT-1025 (external QA EX-3): a stop is never sized for a trade whose live net is on the OTHER side. All broker
calls mocked."""
from __future__ import annotations

import unittest
from unittest import mock

from execution import exit_logic


class OppositeSideNet(unittest.TestCase):
    def test_opposite_net_sends_no_stop(self):
        pos = mock.MagicMock(qty="-10", side="short")
        with mock.patch.object(exit_logic, "get_open_position", return_value=pos), \
                mock.patch.object(exit_logic, "submit_day_stop_order") as day, \
                mock.patch.object(exit_logic, "submit_gtc_stop_order") as gtc:
            ok = exit_logic._submit_new_stop("AAA", {"overnight": False}, mock.MagicMock(), 95.0, 20, "long", None, "trail")
        self.assertFalse(ok)
        day.assert_not_called()
        gtc.assert_not_called()

    def test_same_side_net_still_caps(self):
        pos = mock.MagicMock(qty="6", side="long")
        with mock.patch.object(exit_logic, "get_open_position", return_value=pos), \
                mock.patch.object(exit_logic, "submit_day_stop_order", return_value=mock.MagicMock(id="S")) as day, \
                mock.patch.object(exit_logic, "_log_trade_event"):
            self.assertTrue(exit_logic._submit_new_stop("AAA", {"overnight": False}, mock.MagicMock(), 95.0, 20, "long", None, "trail"))
        self.assertEqual(day.call_args.kwargs["qty"], 6)


    def test_enum_style_side_text_is_normalized(self):
        pos = mock.MagicMock(qty="6", side="PositionSide.LONG")
        with mock.patch.object(exit_logic, "get_open_position", return_value=pos), \
                mock.patch.object(exit_logic, "submit_day_stop_order", return_value=mock.MagicMock(id="S")) as day, \
                mock.patch.object(exit_logic, "_log_trade_event"):
            self.assertTrue(exit_logic._submit_new_stop("AAA", {"overnight": False}, mock.MagicMock(), 95.0, 20, "long", None, "trail"))
        self.assertEqual(day.call_args.kwargs["qty"], 6)

    def test_unknown_side_text_falls_back_to_the_qty_sign(self):
        pos = mock.MagicMock(qty="6")
        pos.side = mock.MagicMock()  # not a long/short string
        with mock.patch.object(exit_logic, "get_open_position", return_value=pos), \
                mock.patch.object(exit_logic, "submit_day_stop_order", return_value=mock.MagicMock(id="S")) as day, \
                mock.patch.object(exit_logic, "_log_trade_event"):
            self.assertTrue(exit_logic._submit_new_stop("AAA", {"overnight": False}, mock.MagicMock(), 95.0, 20, "long", None, "trail"))
        self.assertEqual(day.call_args.kwargs["qty"], 6)


if __name__ == "__main__":
    unittest.main()
