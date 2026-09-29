# ruff: noqa: E501
"""Day tier: the min-stop room gate also measures from the live touch (2026-09-29).
Reproduces the NVDA 2026-09-29 page: short stop 230.29, limit 229.50, bid ~230.28 -> the old gate saw $0.79 of room."""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import day_trade_manager as dtm


def _run(direction, entry_px, stop_px, bid, ask, atr=0.2582):
    fake = SimpleNamespace(get_latest_quote=lambda sym: {"bid": bid, "ask": ask})
    with mock.patch.dict(sys.modules, {"data.alpaca_data": fake}), \
            mock.patch.object(dtm, "_robust_atr_5m", return_value=atr):
        return dtm._min_stop_room_ok("NVDA", direction, entry_px, stop_px)


class LiveRoom(unittest.TestCase):
    def test_nvda_2026_09_29_is_skipped(self):
        ok, why = _run("short", 229.50, 230.29, 230.28, 230.30)
        self.assertFalse(ok)
        self.assertIn("NO ROOM", why)

    def test_short_stop_already_reached_by_bid(self):
        ok, why = _run("short", 229.50, 230.29, 230.30, 230.32)
        self.assertFalse(ok)
        self.assertIn("already reached", why)

    def test_long_stop_already_reached_by_ask(self):
        ok, why = _run("long", 230.50, 229.70, 229.60, 229.69)
        self.assertFalse(ok)
        self.assertIn("already reached", why)

    def test_room_from_live_touch_passes(self):
        # short: bid 229.90, stop 230.80 -> $0.90 live room > 1.5 x 0.2582 = 0.387
        ok, why = _run("short", 229.50, 230.80, 229.90, 229.92)
        self.assertTrue(ok, why)

    def test_long_room_from_live_touch_passes(self):
        ok, why = _run("long", 230.46, 229.50, 230.00, 230.02)
        self.assertTrue(ok, why)

    def test_smaller_of_limit_and_live_distance_is_used(self):
        # long: limit 230.46 -> stop 229.50 is $0.96; live ask 229.80 -> only $0.30 < 0.387 -> skip
        ok, why = _run("long", 230.46, 229.50, 229.78, 229.80)
        self.assertFalse(ok)
        self.assertIn("NO ROOM", why)

    def test_unknown_direction_fails_closed(self):
        ok, _ = _run("flat", 230.0, 229.0, 230.0, 230.02)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
