#!/usr/bin/env python3
# ruff: noqa: E501
"""data/live_price.py — real-time current price (IEX 1m bar -> latest trade -> None) with freshness and a short cache."""
import unittest
from datetime import datetime
from unittest import mock

import pandas as pd

from data import live_price as lp

ET = lp._ET
NOW = datetime(2026, 10, 6, 10, 15, 30, tzinfo=ET)


def _bars(stamps, closes):
    idx = pd.DatetimeIndex([pd.Timestamp(s, tz=ET).tz_convert("UTC") for s in stamps])
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1}, index=idx)


class LivePrice(unittest.TestCase):
    def setUp(self):
        lp._cache.clear()

    def _run(self, bars=None, trade=None, bar_exc=None, trade_age_s=20):
        def win(symbol, tf, start, end, feed="sip", adjustment="raw", asof=None):
            self.assertEqual(feed, "iex")
            self.assertEqual(tf, "1Min")
            if bar_exc:
                raise bar_exc
            return bars if bars is not None else pd.DataFrame()
        from datetime import timedelta
        got = (trade, NOW - timedelta(seconds=trade_age_s)) if trade is not None else None
        with mock.patch("data.fetcher.fetch_bars_window", side_effect=win), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=got):
            return lp.live_price("NVDA", now=NOW)

    def test_fresh_iex_bar_wins(self):
        r = self._run(_bars(["2026-10-06 10:14", "2026-10-06 10:15"], [180.0, 181.5]), trade=170.0)
        self.assertEqual((r.price, r.source), (181.5, "iex_1m"))
        self.assertEqual(r.age_s, 30.0)

    def test_stale_bar_falls_back_to_latest_trade(self):
        r = self._run(_bars(["2026-10-06 10:05"], [180.0]), trade=181.2)      # bar 10.5 min old, trade 20 s old
        self.assertEqual((r.price, r.source, r.age_s), (181.2, "iex_trade", 20.0))

    def test_old_trade_is_never_used(self):
        # thin name: no IEX bar in 10 min and the last IEX print is 30 min old -> None (caller keeps its own source)
        self.assertIsNone(self._run(None, trade=181.2, trade_age_s=1800))

    def test_small_clock_skew_tolerated(self):
        r = self._run(_bars(["2026-10-06 10:15:33"], [181.0]), trade=None)   # bar 3 s "in the future"
        self.assertEqual((r.source, r.age_s), ("iex_1m", 0.0))

    def test_boundary_three_minutes(self):
        self.assertEqual(self._run(_bars(["2026-10-06 10:12:30"], [180.0]), trade=None).source, "iex_1m")   # 180s
        lp._cache.clear()
        self.assertIsNone(self._run(_bars(["2026-10-06 10:12:29"], [180.0]), trade=None))                    # 181s

    def test_nothing_available_returns_none(self):
        self.assertIsNone(self._run(None, trade=None))

    def test_bad_values_rejected(self):
        self.assertIsNone(self._run(_bars(["2026-10-06 10:15"], [float("nan")]), trade=0.0))

    def test_bar_exception_falls_back(self):
        r = self._run(bar_exc=RuntimeError("down"), trade=181.0)
        self.assertEqual(r.source, "iex_trade")

    def test_cache_reuses_within_ttl_for_live_calls_only(self):
        calls = []

        def win(*a, **k):
            calls.append(1)
            return _bars(["2026-10-06 10:15"], [181.0])

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        with mock.patch("data.fetcher.fetch_bars_window", side_effect=win), mock.patch.object(lp, "datetime", _DT):
            lp.live_price("NVDA")
            lp.live_price("NVDA")
            self.assertEqual(len(calls), 1)          # live calls share the cache
            lp.live_price("NVDA", now=NOW)
            self.assertEqual(len(calls), 2)          # an explicit `now` bypasses it

    def test_live_price_or_falls_back_with_label(self):
        with mock.patch.object(lp, "live_price", return_value=None):
            self.assertEqual(lp.live_price_or("NVDA", 179.9, "exit check"), (179.9, "delayed_fallback"))
        with mock.patch.object(lp, "live_price", return_value=lp.LivePrice(181.0, "iex_1m", 10.0)):
            self.assertEqual(lp.live_price_or("NVDA", 179.9, "exit check"), (181.0, "iex_1m"))


if __name__ == "__main__":
    unittest.main()
