#!/usr/bin/env python3
# ruff: noqa: E501
"""Day-tier room stop uses the LAST TRADE price and volatility, never the IEX bid/ask (CEO 2026-10-09). META
2026-10-08 09:38 ET: a $6.47 IEX spread widened the stop 719.44 -> 709.67 (2 x spread $12.94 vs volatility room
$4.43)."""
import unittest
from unittest import mock

from execution import day_trade_manager as dtm

ATR = 4.425 / 1.5            # 1.5 x ATR = the $4.425 volatility room logged for META


def _run(trade, atr=ATR, direction="long", limit=724.06, stop=719.44, quote=None):
    q = mock.Mock(return_value=quote or {"bid": 719.38, "ask": 725.85})
    with mock.patch.object(dtm, "_robust_atr_5m", return_value=atr), \
            mock.patch("data.alpaca_data.get_latest_quote", q), \
            mock.patch("data.alpaca_data.get_latest_trade", return_value=trade):
        out, why = dtm._room_stop("META", direction, limit, stop)
    return out, why, q


class RoomStopLastPrice(unittest.TestCase):
    def test_meta_uses_last_trade_and_volatility(self):
        out, why, q = _run(trade=722.61)
        self.assertEqual(out, round(722.61 - 4.425, 2))       # 718.19, not the 10/08 709.67
        self.assertIn("[last trade]", why)
        q.assert_not_called()                                 # the bid/ask is never read

    def test_tight_quote_makes_no_difference(self):
        out, _w, _q = _run(trade=722.61, quote={"bid": 722.60, "ask": 722.61})
        self.assertEqual(out, round(722.61 - 4.425, 2))

    def test_short_mirrors(self):
        out, why, _q = _run(trade=720.0, direction="short", limit=719.0, stop=723.0)
        self.assertEqual(out, round(720.0 + 4.425, 2))
        self.assertIn("[last trade]", why)

    def test_no_atr_uses_fallback_pct(self):
        for atr in (None, 0.0):                               # unavailable, or a flat 0.0 reading
            out, _w, _q = _run(trade=722.61, atr=atr)
            self.assertEqual(out, round(722.61 - 0.005 * 722.61, 2))

    def test_tiny_atr_gets_the_price_floor(self):
        # a near-flat tape: 1.5 x ATR = $0.015 on a $50 name -> the 0.10% floor ($0.05) applies
        out, _w, _q = _run(trade=50.0, atr=0.01, limit=50.10, stop=49.99)
        self.assertEqual(out, round(50.0 - 0.05, 2))

    def test_structural_stop_wider_than_room_is_kept(self):
        out, why, _q = _run(trade=722.61, stop=715.0)
        self.assertEqual(out, 715.0)
        self.assertIn("kept", why)

    def test_last_trade_through_stop_skips(self):
        out, why, _q = _run(trade=719.0)                      # long stop 719.44 already traded through
        self.assertIsNone(out)
        self.assertIn("setup invalidated", why)

    def test_bad_or_missing_print_falls_back_to_limit(self):
        for bad in (None, 0.0, 800.0):                        # missing, zero, > 5% off the limit
            out, why, _q = _run(trade=bad, stop=722.0)
            self.assertEqual(out, round(724.06 - 4.425, 2))
            self.assertIn("limit price", why)


if __name__ == "__main__":
    unittest.main()
