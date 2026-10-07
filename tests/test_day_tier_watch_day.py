#!/usr/bin/env python3
# ruff: noqa: E501
"""Watch-day rule (Rafael 2026-10-06): a day-tier SHORT against a LONG daily side needs a watch day — the previous
session closed red AND below the prior session's low. Every other trade passes. Unreadable data skips only that
counter-trend short. Today's partial daily bar never counts as 'yesterday'."""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd

import run_day_tier as rdt


def _daily(rows, last_day):
    """rows = [(open, high, low, close)] ending on last_day (ET date), one calendar day apart."""
    end = pd.Timestamp(last_day, tz="America/New_York")
    idx = pd.date_range(end=end, periods=len(rows), freq="D").tz_convert("UTC")
    return pd.DataFrame([{"open": o, "high": h, "low": lo, "close": c, "volume": 1} for o, h, lo, c in rows], index=idx)


class WatchDay(unittest.TestCase):
    def _run(self, df, side="LONG", direction="short"):
        with mock.patch("data.fetcher.fetch_bars", return_value=df):
            return rdt._watch_day_ok("NVDA", side, direction)

    def setUp(self):
        self.today = datetime.now(rdt.ET).date()
        self.y = (pd.Timestamp(self.today) - pd.Timedelta(days=1)).date()

    def test_only_shorts_against_a_long_side_are_checked(self):
        with mock.patch("data.fetcher.fetch_bars", side_effect=AssertionError("no fetch")):
            for side, d in (("LONG", "long"), ("SHORT", "short"), ("SHORT", "long"), ("TWO_SIDED", "short"), (None, "short")):
                self.assertTrue(rdt._watch_day_ok("NVDA", side, d)[0], (side, d))

    def test_red_close_below_prior_low_confirms(self):
        df = _daily([(100, 102, 99, 101), (101, 101.5, 97, 98)], self.y)   # y: open 101, close 98 < prior low 99
        ok, why = self._run(df)
        self.assertTrue(ok)
        self.assertIn("CONFIRMED", why)

    def test_red_but_not_below_prior_low_is_not_a_watch_day(self):
        df = _daily([(100, 102, 97, 101), (101, 101.5, 98, 99.5)], self.y)  # close 99.5 > prior low 97
        self.assertFalse(self._run(df)[0])

    def test_green_day_is_not_a_watch_day(self):
        df = _daily([(100, 102, 99, 101), (95, 98.5, 94, 98)], self.y)      # close 98 > open 95 (green) though < 99
        self.assertFalse(self._run(df)[0])

    def test_todays_partial_bar_is_ignored(self):
        # yesterday confirms; today's partial bar (green, above) must NOT replace yesterday
        df = _daily([(100, 102, 99, 101), (101, 101.5, 97, 98), (98, 105, 97.5, 104)], self.today)
        self.assertTrue(self._run(df)[0])

    def test_missing_or_short_data_skips_the_counter_trend_short(self):
        self.assertFalse(self._run(None)[0])
        self.assertFalse(self._run(pd.DataFrame())[0])
        self.assertFalse(self._run(_daily([(100, 102, 99, 101)], self.y))[0])
        with mock.patch("data.fetcher.fetch_bars", side_effect=RuntimeError("down")):
            self.assertFalse(rdt._watch_day_ok("NVDA", "LONG", "short")[0])

    def test_window_spanning_a_weekend_uses_the_last_two_completed_sessions(self):
        df = _daily([(100, 102, 99, 101), (101, 101.5, 97, 98)], self.y - timedelta(days=2))
        self.assertTrue(self._run(df)[0])


if __name__ == "__main__":
    unittest.main()
