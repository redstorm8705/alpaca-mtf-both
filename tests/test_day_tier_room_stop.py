#!/usr/bin/env python3
# ruff: noqa: E501
"""Day-tier room stop uses the LAST TRADE price and volatility, never the IEX bid/ask (CEO 2026-10-09). META
2026-10-08 09:38 ET: a $6.47 IEX spread widened the stop 719.44 -> 709.67 (2 x spread $12.94 vs volatility room
$4.43)."""
import unittest
from unittest import mock

from execution import day_trade_manager as dtm

ATR = 4.425 / 1.5            # 1.5 x ATR = the $4.425 volatility room logged for META


def _run(trade, atr=ATR, direction="long", limit=724.06, stop=719.44, quote=None, rt=None):
    q = mock.Mock(return_value=quote or {"bid": 719.38, "ask": 725.85})
    with mock.patch.object(dtm, "_robust_atr_5m", return_value=atr), \
            mock.patch.object(dtm, "_realtime_vol_room", return_value=rt), \
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


class OpenVolatilityRoom(unittest.TestCase):
    """Losers audit 2026-10-09: AMZN 09:40 ET got a $0.30 room from prior-afternoon bars; its opening bars ranged
    $2.79 / $1.25 / $1.35 and the stop hit 3 minutes before the target."""

    @staticmethod
    def _frame(today_bars):
        import pandas as pd
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        now = datetime.now(et)
        rows, idx = [], []
        prior = (now - timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        for i in range(27 - len(today_bars)):              # quiet prior-afternoon bars: $0.20 ranges
            idx.append(prior + timedelta(minutes=5 * i))
            rows.append({"open": 255.0, "high": 255.1, "low": 254.9, "close": 255.0, "volume": 1000})
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        for i, (h, lo, c) in enumerate(today_bars):
            idx.append(start + timedelta(minutes=5 * i))
            rows.append({"open": c, "high": h, "low": lo, "close": c, "volume": 5000})
        return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))

    def test_open_bars_raise_the_room(self):
        df = self._frame([(258.92, 256.13, 258.63), (258.73, 257.48, 258.58), (258.93, 257.58, 257.97)])
        with mock.patch("strategy.day_tier_entry_trigger.fetch_bars_ref", return_value=df):
            room = dtm._realtime_vol_room("AMZN", 1.5)
        self.assertGreater(room, 1.5)                          # the opening bars, not 1.5 x $0.20

    def test_quiet_session_keeps_median_room(self):
        df = self._frame([])
        with mock.patch("strategy.day_tier_entry_trigger.fetch_bars_ref", return_value=df):
            room = dtm._realtime_vol_room("AMZN", 1.5)
        self.assertAlmostEqual(room, 1.5 * 0.2, places=4)

    def test_no_fresh_bars_returns_none(self):
        import pandas as pd
        with mock.patch("strategy.day_tier_entry_trigger.fetch_bars_ref", return_value=pd.DataFrame()):
            self.assertIsNone(dtm._realtime_vol_room("AMZN", 1.5))
        with mock.patch("strategy.day_tier_entry_trigger.fetch_bars_ref", side_effect=RuntimeError("down")):
            self.assertIsNone(dtm._realtime_vol_room("AMZN", 1.5))

    def test_room_stop_widens_to_the_open_room(self):
        out, why, _q = _run(trade=258.57, atr=0.2966 / 1.5, limit=259.52, stop=258.24, rt=2.02)
        self.assertEqual(out, round(258.57 - 2.02, 2))
        self.assertIn("real-time/open volatility", why)

    def test_realtime_room_is_capped_at_pct_of_price(self):
        out, why, _q = _run(trade=258.57, atr=0.2966 / 1.5, limit=259.52, stop=258.24, rt=20.0)   # an 8% halt bar
        self.assertEqual(out, round(258.57 - 0.0125 * 258.57, 2))
        self.assertIn("capped at 1.25%", why)

    def test_cap_never_narrows_the_existing_room(self):
        out, _why, _q = _run(trade=722.61, atr=12.0, rt=50.0)    # existing 1.5 x 12 = 18 > 1.25% x 722.61 = 9.03
        self.assertEqual(out, round(722.61 - 18.0, 2))

    def test_smaller_realtime_room_never_narrows(self):
        out, why, _q = _run(trade=722.61, rt=1.0)
        self.assertEqual(out, round(722.61 - 4.425, 2))
        self.assertNotIn("real-time/open volatility", why)


if __name__ == "__main__":
    unittest.main()
