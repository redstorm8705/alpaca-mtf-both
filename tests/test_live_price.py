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


def _fetcher_stub(fn):
    """Stand-in data.fetcher (some suites stub the alpaca SDK, so the real module cannot always be imported)."""
    import sys
    import types
    stub = types.ModuleType("data.fetcher")
    stub.fetch_bars_window = fn
    return mock.patch.dict(sys.modules, {"data.fetcher": stub})


class LivePrice(unittest.TestCase):
    def setUp(self):
        lp._cache.clear()
        lp._bar_paused_until = 0.0
        # the latest trade is now always read: default it to "request failed" so no test reaches the network
        _p = mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=None)
        _p.start()
        self.addCleanup(_p.stop)

    def _run(self, bars=None, trade=None, bar_exc=None, trade_age_s=20, max_age_s=lp._MAX_BAR_AGE_S):
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
            return lp.live_price("NVDA", now=NOW, max_age_s=max_age_s)

    def test_fresh_iex_bar_wins(self):
        r = self._run(_bars(["2026-10-06 10:14", "2026-10-06 10:15"], [180.0, 181.5]), trade=170.0, trade_age_s=60)
        self.assertEqual((r.price, r.source), (181.5, "iex_1m"))   # bar 30 s old beats a 60-s-old print
        self.assertEqual(r.age_s, 30.0)
        lp._cache.clear()
        r = self._run(_bars(["2026-10-06 10:14", "2026-10-06 10:15"], [180.0, 181.5]), trade=170.0, trade_age_s=2)
        self.assertEqual((r.price, r.source), (170.0, "iex_trade"))  # a 2-s-old print beats a 30-s-old bar

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
        from datetime import timedelta
        with mock.patch("data.fetcher.fetch_bars_window", side_effect=win), mock.patch.object(lp, "datetime", _DT), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=(180.9, NOW - timedelta(seconds=60))):
            lp.live_price("NVDA")
            lp.live_price("NVDA")
            self.assertEqual(len(calls), 1)          # live calls share the cache
            lp.live_price("NVDA", now=NOW)
            self.assertEqual(len(calls), 2)          # an explicit `now` bypasses it

    def test_exit_age_accepts_print_fresher_than_delayed_feed(self):
        # a 10-min-old IEX print is still fresher than the >=15-min-old delayed close -> exit paths must use it
        from datetime import timedelta
        got = (94.8, NOW - timedelta(seconds=600))
        with mock.patch.object(lp, "_from_iex_bars", return_value=None), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=got):
            self.assertIsNone(lp.live_price("NVDA", now=NOW))
            self.assertEqual(lp.DELAYED_FEED_AGE_S, 900.0 + lp.config.ALPACA_BAR_CACHE_TTL_SECS)   # SIP delay + fetch_bars cache
            r = lp.live_price("NVDA", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S)
            self.assertEqual((r.price, r.source), (94.8, "iex_trade"))
        with mock.patch.object(lp, "_from_iex_bars", return_value=None), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=(94.8, NOW - timedelta(seconds=1000))):
            self.assertEqual(lp.live_price("NVDA", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S).source, "iex_trade")   # 16.7 min old: fresher than an 18-min fallback
        with mock.patch.object(lp, "_from_iex_bars", return_value=None), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time",
                           return_value=(94.8, NOW - timedelta(seconds=lp.DELAYED_FEED_AGE_S + 1))):
            self.assertIsNone(lp.live_price("NVDA", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S))

    def test_newer_trade_beats_stale_bar_at_exit_age(self):
        # cold-2nd rev3 FAIL 2: a 10-min-old bar ($95) must not beat a 20-s-old print ($94) — the newer one wins
        from datetime import timedelta
        bar = lp.LivePrice(95.0, "iex_1m", 600.0)
        with mock.patch.object(lp, "_from_iex_bars", return_value=bar), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=(94.0, NOW - timedelta(seconds=20))):
            r = lp.live_price("X", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S)
        self.assertEqual((r.price, r.source), (94.0, "iex_trade"))
        with mock.patch.object(lp, "_from_iex_bars", return_value=bar), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=None):
            self.assertEqual(lp.live_price("X", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S), bar)   # no print: stale bar still beats the delayed close
        # cold-2nd rev8 FAIL: the bars API never returns the minute in progress, so even a "fresh" bar is 60-120 s
        # old. Stop $95: bar (90 s, $96) says no breach, the 2-s-old print ($94) says breach -> the print must win.
        fresh = lp.LivePrice(96.0, "iex_1m", 90.0)
        with mock.patch.object(lp, "_from_iex_bars", return_value=fresh), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=(94.0, NOW - timedelta(seconds=2))):
            r = lp.live_price("X", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S)
        self.assertEqual((r.price, r.source), (94.0, "iex_trade"))

    def test_bar_cached_trade_always_reread(self):
        bar = mock.Mock(return_value=None)
        trade = mock.Mock(return_value=None)
        with mock.patch.object(lp, "_from_iex_bars", bar), mock.patch.object(lp, "_from_latest_trade", trade):
            self.assertIsNone(lp.live_price("NVDA"))
            self.assertIsNone(lp.live_price("NVDA"))
        self.assertEqual((bar.call_count, trade.call_count), (1, 2))   # bar cached within the TTL; trade re-read

    def test_exit_site_never_reuses_partial_site_print(self):
        # cold-2nd rev9 FAIL: partial site reads $95.10; 10 s later the print is $94.80 -> the exit site must see $94.80
        from datetime import timedelta
        clock = [NOW]

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]
        prints = mock.Mock(side_effect=[(95.10, NOW - timedelta(seconds=1)), (94.80, NOW + timedelta(seconds=9))])
        with mock.patch.object(lp, "datetime", _DT), mock.patch.object(lp, "_from_iex_bars", return_value=None), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", prints):
            self.assertEqual(lp.live_price("X", max_age_s=lp.DELAYED_FEED_AGE_S).price, 95.10)
            clock[0] = NOW + timedelta(seconds=10)
            self.assertEqual(lp.live_price("X", max_age_s=lp.DELAYED_FEED_AGE_S).price, 94.80)
        self.assertEqual(prints.call_count, 2)

    def test_cached_bar_is_aged_and_expires_against_limit(self):
        mono = [1000.0]
        with mock.patch.object(lp.time, "monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(lp, "_from_iex_bars", return_value=lp.LivePrice(95.0, "iex_1m", 170.0)) as fb:
            self.assertEqual(lp.live_price("X").age_s, 170.0)
            mono[0] += 5
            self.assertEqual(lp.live_price("X").age_s, 175.0)          # reused, aged by 5 s
            mono[0] += 6
            self.assertIsNone(lp.live_price("X"))                       # 181 s > 180 s limit: not served
            self.assertEqual(fb.call_count, 1)

    def test_failed_trade_request_is_not_cached(self):
        # cold-2nd rev7: a trade request that FAILS at the partial-exit read must be re-tried at the stop check
        from datetime import timedelta
        stale_bar = lp.LivePrice(95.0, "iex_1m", 600.0)
        clock = [NOW]

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]
        got = mock.Mock(side_effect=[None, (94.0, NOW - timedelta(seconds=1))])     # timeout, then a 1-s-old print
        with mock.patch.object(lp, "datetime", _DT), mock.patch.object(lp, "_from_iex_bars", return_value=stale_bar), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", got):
            self.assertEqual(lp.live_price("X", max_age_s=lp.DELAYED_FEED_AGE_S), stale_bar)       # site 1: bar only
            r = lp.live_price("X", max_age_s=lp.DELAYED_FEED_AGE_S)                                 # site 2: re-read
        self.assertEqual((r.price, r.source), (94.0, "iex_trade"))
        self.assertEqual(got.call_count, 2)

    def test_old_print_is_reread_each_call(self):
        from datetime import timedelta
        got = mock.Mock(return_value=(94.0, NOW - timedelta(seconds=5000)))                       # came back, too old
        clock = [NOW]

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]
        with mock.patch.object(lp, "datetime", _DT), mock.patch.object(lp, "_from_iex_bars", return_value=None), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", got):
            self.assertIsNone(lp.live_price("X", max_age_s=lp.DELAYED_FEED_AGE_S))
            self.assertIsNone(lp.live_price("X", max_age_s=lp.DELAYED_FEED_AGE_S))
        self.assertEqual(got.call_count, 2)                     # trade results are never cached

    def test_slow_bar_read_pauses_bars_only_and_trade_still_used(self):
        # cold-2nd rev3 FAIL 1: a 61s bar ladder followed by a successful trade read must still pause bar reads
        mono = [1000.0]
        good = lp.LivePrice(94.0, "iex_trade", 5.0)
        slow_bar = mock.Mock(side_effect=lambda *a, **k: mono.__setitem__(0, mono[0] + 61.0))          # 429 ladder, then empty
        with mock.patch.object(lp.time, "monotonic", side_effect=lambda: mono[0]), \
                mock.patch.object(lp, "_from_iex_bars", slow_bar), mock.patch.object(lp, "_from_latest_trade", return_value=good):
            self.assertEqual(lp.live_price("A"), good)
            self.assertGreater(lp._bar_paused_until, mono[0])
            self.assertEqual(lp.live_price("B"), good)           # B: bar read skipped, trade still read
            self.assertEqual(slow_bar.call_count, 1)             # at most ONE slow bar read per pause
        mono[0] += lp._BAR_PAUSE_S + 1
        ok_bar = mock.Mock(return_value=lp.LivePrice(95.0, "iex_1m", 10.0))
        with mock.patch.object(lp.time, "monotonic", side_effect=lambda: mono[0]), mock.patch.object(lp, "_from_iex_bars", ok_bar):
            self.assertEqual(lp.live_price("C").source, "iex_1m")  # pause expired: bars read again
        lp._bar_paused_until = 0.0
        lp._cache.clear()
        mono[0] = 5000.0
        fast_ok = mock.Mock(return_value=lp.LivePrice(95.0, "iex_1m", 10.0))
        with mock.patch.object(lp.time, "monotonic", side_effect=lambda: mono[0]), mock.patch.object(lp, "_from_iex_bars", fast_ok):
            lp.live_price("D")
        self.assertEqual(lp._bar_paused_until, 0.0)              # a fast read never pauses

    def test_trade_printed_during_slow_bar_read_is_accepted(self):
        # cold-2nd rev4 FAIL: `now` taken before a 61s bar read made a fresh print look 60s "in the future"
        from datetime import timedelta
        clock = [NOW]

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        def slow_bars(*a, **k):
            clock[0] = NOW + timedelta(seconds=61)
            return pd.DataFrame()
        with mock.patch.object(lp, "datetime", _DT), _fetcher_stub(slow_bars), \
                mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=(94.0, NOW + timedelta(seconds=60))):
            r = lp.live_price("NVDA", max_age_s=lp.DELAYED_FEED_AGE_S)
        self.assertEqual((r.price, r.source), (94.0, "iex_trade"))

    def test_bar_aged_against_clock_after_read(self):
        from datetime import timedelta
        clock = [NOW]

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        def slow_bars(*a, **k):
            clock[0] = NOW + timedelta(seconds=61)
            return _bars(["2026-10-06 10:13:00"], [180.0])                   # 150s old at the call, 211s after it
        with mock.patch.object(lp, "datetime", _DT), _fetcher_stub(slow_bars):
            self.assertIsNone(lp._from_iex_bars("NVDA", NOW, lp._MAX_BAR_AGE_S, live=True))
            self.assertEqual(lp._from_iex_bars("NVDA", NOW, lp._MAX_BAR_AGE_S).age_s, 150.0)   # replay: uses `now`

    def test_exit_age_bar_must_close_before_sip_cutoff(self):
        # cold-2nd rev6: trade read fails; a 1000s-old IEX bar must not beat a ~900s-old delayed close
        old = _bars(["2026-10-06 09:58:50"], [180.0])                        # started 1000s before NOW
        ok = _bars(["2026-10-06 10:02:10"], [181.0])                         # started 800s before NOW
        with _fetcher_stub(lambda *a, **k: old), mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=None):
            self.assertIsNone(lp.live_price("NVDA", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S))
        with _fetcher_stub(lambda *a, **k: ok), mock.patch("data.alpaca_data.get_latest_trade_with_time", return_value=None):
            r = lp.live_price("NVDA", now=NOW, max_age_s=lp.DELAYED_FEED_AGE_S)
        self.assertEqual((r.price, r.source, r.age_s), (181.0, "iex_1m", 800.0))

    def test_cached_value_served_while_bars_paused(self):
        ok = lp.LivePrice(181.0, "iex_1m", 10.0)
        with mock.patch.object(lp, "_from_iex_bars", return_value=ok), mock.patch.object(lp, "_from_latest_trade", return_value=None):
            lp.live_price("NVDA")
        lp._bar_paused_until = 10 ** 12
        with mock.patch.object(lp, "_from_iex_bars") as fb, mock.patch.object(lp, "_from_latest_trade", return_value=None) as ft:
            self.assertEqual(lp.live_price("NVDA").price, ok.price)     # cached bar reused (aged), no bar read
            fb.assert_not_called()
            ft.assert_called_once()                                     # the trade is still read

    def test_live_price_or_falls_back_with_label(self):
        with mock.patch.object(lp, "live_price", return_value=None):
            self.assertEqual(lp.live_price_or("NVDA", 179.9, "exit check"), (179.9, "delayed_fallback"))
        with mock.patch.object(lp, "live_price", return_value=lp.LivePrice(181.0, "iex_1m", 10.0)):
            self.assertEqual(lp.live_price_or("NVDA", 179.9, "exit check"), (181.0, "iex_1m"))


if __name__ == "__main__":
    unittest.main()
