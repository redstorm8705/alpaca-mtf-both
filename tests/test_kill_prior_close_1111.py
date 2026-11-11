#!/usr/bin/env python3
# ruff: noqa: E501
"""KILL-PRIOR-CLOSE-1111 (owner-approved; logs/design_records/kill_prior_close_2026-11-11.md).

2026-11-11: the Swing trend-night lot (1 QQQM held overnight) sold at 06:33 PT; QQQM was then flat, so the broker sent no
lastday_price, the kill measure returned (0, False) and new entries were blocked for the rest of the session. These
tests pin the fix: a fully sold held-over lot is priced from the prior session's daily close; every miss still fails
closed. No network: fills, positions and bars are mocked.
"""
import math
import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import config
from execution import risk_manager as rm

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")


def _bars(rows):
    """Daily bars stamped at ET midnight (Alpaca's convention), UTC index."""
    idx = pd.DatetimeIndex([datetime(*d, tzinfo=ET).astimezone(timezone.utc) for d, _c in rows])
    return pd.DataFrame({"open": [c for _d, c in rows], "high": [c for _d, c in rows], "low": [c for _d, c in rows],
                         "close": [c for _d, c in rows], "volume": [1] * len(rows)}, index=idx)


class _Clock:
    """Freezes datetime.now inside risk_manager at a given PT wall time."""

    def __init__(self, pt: datetime):
        self.pt = pt

    def now(self, tz=None):
        return self.pt.astimezone(tz) if tz else self.pt.replace(tzinfo=None)

    def __getattr__(self, name):
        return getattr(datetime, name)

    def __call__(self, *a, **k):
        return datetime(*a, **k)


def _patch_now(pt: datetime):
    clock = _Clock(pt)
    return mock.patch.object(rm, "datetime", mock.Mock(wraps=datetime, now=clock.now,
                                                       side_effect=lambda *a, **k: datetime(*a, **k)))


class PriorCloseLookup(unittest.TestCase):
    def setUp(self):
        rm._PRIOR_CLOSE_CACHE.clear()
        rm._PRIOR_CLOSE_MISS.clear()

    def _call(self, bars, pt=datetime(2026, 11, 11, 7, 0, tzinfo=PT)):
        with _patch_now(pt), mock.patch("data.fetcher.fetch_bars_window", return_value=bars) as f:
            out = rm._prior_session_close("qqqm")
        return out, f

    def test_last_session_before_today(self):
        out, f = self._call(_bars([((2026, 11, 7), 270.0), ((2026, 11, 10), 277.87)]))
        self.assertEqual(out, 277.87)
        kw = f.call_args.kwargs
        self.assertEqual((kw["feed"], kw["adjustment"]), ("sip", "split"))
        args = f.call_args.args
        self.assertEqual(args[0], "QQQM")
        self.assertEqual(args[1], config.TF_DAILY)
        self.assertEqual(args[3], datetime(2026, 11, 11, tzinfo=ET).astimezone(timezone.utc))   # ends at today's ET midnight

    def test_todays_bar_is_never_the_prior_close(self):
        out, _f = self._call(_bars([((2026, 11, 11), 290.0)]))
        self.assertIsNone(out)
        rm._PRIOR_CLOSE_MISS.clear()   # a miss is remembered for one interval; this is a separate case
        out, _f = self._call(_bars([((2026, 11, 10), 277.87), ((2026, 11, 11), 290.0)]))
        self.assertEqual(out, 277.87)

    def test_late_pt_evening_uses_the_pt_day(self):
        """21:30 PT on 11-11 is already 11-12 in ET; the 11-11 bar must still not count as the prior close (risk seat)."""
        rm._PRIOR_CLOSE_CACHE.clear()
        out, _f = self._call(_bars([((2026, 11, 10), 100.0), ((2026, 11, 11), 90.0)]),
                             pt=datetime(2026, 11, 11, 21, 30, tzinfo=PT))
        self.assertEqual(out, 100.0)

    def test_bar_stamp_convention_change_degrades(self):
        bars = _bars([((2026, 11, 10), 277.87)])
        bars.index = bars.index - pd.Timedelta(hours=5)   # stamped 00:00Z instead of ET midnight
        out, _f = self._call(bars)
        self.assertIsNone(out)

    def test_empty_or_bad_close_degrades(self):
        self.assertIsNone(self._call(pd.DataFrame())[0])
        rm._PRIOR_CLOSE_MISS.clear()
        self.assertIsNone(self._call(_bars([((2026, 11, 10), float("nan"))]))[0])
        rm._PRIOR_CLOSE_MISS.clear()
        self.assertIsNone(self._call(_bars([((2026, 11, 10), 0.0)]))[0])

    def test_fetch_error_never_raises(self):
        with _patch_now(datetime(2026, 11, 11, 7, 0, tzinfo=PT)), \
                mock.patch("data.fetcher.fetch_bars_window", side_effect=RuntimeError("boom")):
            self.assertIsNone(rm._prior_session_close("QQQM"))

    def test_found_value_cached_for_the_day(self):
        out, f = self._call(_bars([((2026, 11, 10), 277.87)]))
        out2, f2 = self._call(pd.DataFrame())
        self.assertEqual((out, out2), (277.87, 277.87))
        f2.assert_not_called()

    def test_miss_not_refetched_within_one_interval(self):
        with mock.patch.object(rm.time, "monotonic", return_value=1000.0):
            self.assertIsNone(self._call(pd.DataFrame())[0])
        with mock.patch.object(rm.time, "monotonic", return_value=1001.0):
            out, f = self._call(_bars([((2026, 11, 10), 277.87)]))
        self.assertIsNone(out)
        f.assert_not_called()
        later = 1000.0 + config.SCAN_INTERVAL_INTRADAY * 60 + 1
        with mock.patch.object(rm.time, "monotonic", return_value=later):
            out, f = self._call(_bars([((2026, 11, 10), 277.87)]))
        self.assertEqual(out, 277.87)


class SoldOutLotInKillMeasure(unittest.TestCase):
    """The full measure: a held-over lot fully closed today is priced from its prior close, both signs; a miss still
    returns ok=False (the caller's fail-closed block)."""

    def setUp(self):
        rm._PRIOR_CLOSE_CACHE.clear()
        rm._PRIOR_CLOSE_MISS.clear()

    def _measure(self, fills, positions, prior):
        r = object.__new__(rm.RiskManager)
        r._kill_attr = {}
        resp = mock.Mock(status_code=200)
        resp.json.return_value = positions
        with mock.patch.object(rm, "_today_fills", return_value=(tuple(fills), True)), \
                mock.patch.object(rm.RiskManager, "_protected_held_qty", return_value={}), \
                mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True), \
                mock.patch.dict("os.environ", {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}), \
                mock.patch.object(rm.requests, "get", return_value=resp), \
                mock.patch.object(rm, "_prior_session_close", return_value=prior) as pc, \
                mock.patch.object(rm, "_kill_join_resolved"):
            out = r._intraday_trading_pnl()
        return out, pc

    def test_replay_2026_11_11_qqqm(self):
        out, pc = self._measure([("QQQM", "sell", 1, 283.59, "intraday")], [], 277.87)
        self.assertEqual(out, (round(283.59 - 277.87, 2), True))
        pc.assert_called_once_with("QQQM")

    def test_long_gap_down_loss_is_counted(self):
        out, _pc = self._measure([("ABC", "sell", 10, 90.0, "intraday")], [], 100.0)
        self.assertEqual(out, (-100.0, True))

    def test_short_covered_today_loss_is_counted(self):
        out, _pc = self._measure([("ABC", "buy_to_cover", 2, 160.0, "intraday")], [], 150.0)
        self.assertEqual(out, (-20.0, True))

    def test_reverse_split_long_on_split_basis(self):
        """1:10 reverse split: 10 held at $1.00 become 1 share; sold at $9.00. Split-adjusted prior close = $10.00, so
        the loss is -$1, not the +$8 a raw $1.00 close would show (Thorp seat)."""
        out, _pc = self._measure([("ABC", "sell", 1, 9.0, "intraday")], [], 10.0)
        self.assertEqual(out, (-1.0, True))

    def test_forward_split_short_on_split_basis(self):
        """2:1 split: short 1 at a $300 close becomes short 2; covered at $160 each. Split-adjusted prior close $150 ->
        -$20, not the +$280 a raw close would show."""
        out, _pc = self._measure([("ABC", "buy_to_cover", 2, 160.0, "intraday")], [], 150.0)
        self.assertEqual(out, (-20.0, True))

    def test_rebought_and_resold_today(self):
        fills = [("ABC", "sell", 1, 105.0, "intraday"), ("ABC", "buy", 1, 103.0, "intraday"),
                 ("ABC", "sell", 1, 101.0, "intraday")]
        out, _pc = self._measure(fills, [], 100.0)
        self.assertTrue(math.isclose(out[0], (105.0 - 100.0) + (100.0 - 103.0) + (101.0 - 100.0)))
        self.assertTrue(out[1])

    def test_miss_still_fails_closed(self):
        out, _pc = self._measure([("QQQM", "sell", 1, 283.59, "intraday")], [], None)
        self.assertEqual(out, (0.0, False))

    def test_pure_day_round_trip_needs_no_lookup(self):
        fills = [("ABC", "buy", 1, 100.0, "intraday"), ("ABC", "sell", 1, 102.0, "intraday")]
        out, pc = self._measure(fills, [], None)
        self.assertEqual(out, (2.0, True))
        pc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
