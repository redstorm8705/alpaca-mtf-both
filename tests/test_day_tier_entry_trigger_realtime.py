#!/usr/bin/env python3
# ruff: noqa: E501
"""Track-A entry trigger data source (2026-10-05 fix): real-time IEX 5m bars, completed bars only. Uses the REAL
data.fetcher (no alpaca stubs), so it runs where the bot's dependencies are installed (production Python 3.10)."""
import unittest
from unittest import mock

import pandas as pd

from strategy import day_tier_entry_trigger as et


class RealtimeClosedBars(unittest.TestCase):
    """2026-10-05 fix: the trigger reads the REAL-TIME IEX feed (the default feed is SIP delayed ~15 min on this
    plan) and only COMPLETED 5m bars."""

    def _run(self, now_min, minutes=(0, 5, 10, 15), hour=10):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et_tz = ZoneInfo("America/New_York")
        idx = pd.DatetimeIndex([pd.Timestamp(2026, 10, 6, hour, m, tz=et_tz).tz_convert("UTC") for m in minutes])
        frame = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": [float(i + 1) for i in range(len(minutes))],
                              "volume": 1}, index=idx)
        calls = []

        def fake_window(symbol, tf, start, end, feed="sip", adjustment="raw", asof=None):
            calls.append((tf, feed, start.tzinfo is not None, end.tzinfo is not None))
            return frame

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 6, 10, now_min, 30, tzinfo=et_tz)

        import data.fetcher as f
        with mock.patch.object(f, "fetch_bars_window", side_effect=fake_window), \
                mock.patch.object(f, "fetch_bars", side_effect=AssertionError("delayed default feed must not be used")), \
                mock.patch("datetime.datetime", _DT):
            out = et.fetch_bars_ref("NVDA", "5Min", num_bars=30)
        return out, calls

    def test_reads_iex_with_aware_window(self):
        _, calls = self._run(17)
        self.assertEqual(calls, [("5Min", "iex", True, True)])

    def test_forming_bar_is_dropped(self):
        out, _ = self._run(17)           # 10:15 bar is still forming at 10:17:30
        self.assertEqual(list(out["close"]), [1.0, 2.0, 3.0])

    def test_closed_bar_is_kept(self):
        out, _ = self._run(20)           # 10:15 bar closed at 10:20
        self.assertEqual(list(out["close"]), [1.0, 2.0, 3.0, 4.0])

    def test_stale_last_bar_returns_nothing(self):
        # newest closed bar 10:15 ended 10:20; at 10:31 it is 11 minutes old -> nothing (trigger WAITs)
        out, _ = self._run(31)
        self.assertTrue(out.empty)

    def test_last_bar_ten_minutes_old_is_still_used(self):
        out, _ = self._run(30)           # ended 10:20, now 10:30:30 -> 10.5 min -> stale
        self.assertTrue(out.empty)
        out, _ = self._run(29)           # 9.5 min -> fresh
        self.assertFalse(out.empty)

    def test_gapped_iex_bars(self):
        # thin name: IEX printed bars only at 10:00 and 10:10; at 10:17 the 10:10 bar (ended 10:15) is fresh
        out, _ = self._run(17, minutes=(0, 10))
        self.assertEqual(list(out["close"]), [1.0, 2.0])

    def test_only_old_bars_return_nothing(self):
        # only pre-market IEX bars exist (09:00, 09:05); at 10:33 the newest ended 09:10 -> stale -> nothing
        out, _ = self._run(33, minutes=(0, 5), hour=9)
        self.assertTrue(out.empty)



if __name__ == "__main__":
    unittest.main()
