# ruff: noqa: E501
"""GEX-SPOT-1019: a quote too wide to validate the last trade falls back to the recent tape (owner approval 2026-10-20).

The trade is accepted only when it is fresh, regular-way, and inside [bar low - ATR, bar high + ATR] of the last completed
5-minute bar that ended at or before it. Anything else keeps the spot suspect, exactly as before. No network: every
data call is mocked.
"""
import unittest
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd

import data.gex as gex

ET = gex.ET


def _bars(now, n=6, base=100.0, rng=1.0):
    """n completed 5-minute RTH bars ending at or before `now` (most recent ends exactly at `now`)."""
    idx = [now - timedelta(minutes=5 * (n - i)) for i in range(n)]
    return pd.DataFrame({"open": [base] * n, "high": [base + rng / 2] * n, "low": [base - rng / 2] * n,
                         "close": [base] * n, "volume": [1000] * n}, index=pd.DatetimeIndex(idx))


class BarValidatedSpot(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 19, 11, 0, 30, tzinfo=ET)
        self.trade_ts = self.now - timedelta(seconds=20)

    def _run(self, price=100.4, conds=(), trade_ts=None, df="default"):
        trade = {"price": price, "ts": trade_ts or self.trade_ts, "conds": list(conds)}
        frame = _bars(self.now.replace(second=0) - timedelta(minutes=0)) if isinstance(df, str) else df

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return self.now

        with mock.patch.object(gex, "_get_spot_trade", return_value=trade), \
                mock.patch.object(gex, "datetime", _DT), \
                mock.patch("strategy.day_tier_entry_trigger.fetch_bars_ref", return_value=frame):
            return gex._bar_validated_spot("SNDK")

    def test_in_band_trade_is_accepted(self):
        ok, d = self._run(price=100.9)   # bar high 100.5 + ATR 1.0 = 101.5
        self.assertTrue(ok, d)
        self.assertEqual(d["reason"], "inside_bar_atr_band")

    def test_out_of_band_trade_kept_suspect(self):
        ok, d = self._run(price=102.0)
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "outside_bar_atr_band")

    def test_band_lower_edge(self):
        self.assertTrue(self._run(price=98.5)[0])     # bar low 99.5 - ATR 1.0
        self.assertFalse(self._run(price=98.49)[0])

    def test_stale_trade_kept_suspect(self):
        ok, d = self._run(trade_ts=self.now - timedelta(minutes=11))
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "stale_trade")

    def test_non_regular_condition_kept_suspect(self):
        ok, d = self._run(conds=("@", "Z"))
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "trade_condition")

    def test_extended_hours_print_kept_suspect(self):
        ok, d = self._run(conds=("@", "T"))
        self.assertFalse(ok)
        self.assertEqual(d["bad_conds"], ["T"])

    def test_odd_lot_is_kept(self):
        self.assertTrue(self._run(conds=("@", "I"))[0])

    def test_no_bars_kept_suspect(self):
        ok, d = self._run(df=None)
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "no_fresh_bars")

    def test_too_few_session_bars_kept_suspect(self):
        ok, d = self._run(df=_bars(self.now.replace(second=0), n=1))
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "too_few_session_bars")

    def test_bar_containing_the_trade_is_not_the_reference(self):
        # A forming bar that started before the trade but ends after it must not be used (time-ordered reference).
        df = _bars(self.now.replace(second=0), n=6)
        forming = pd.DataFrame({"open": [150.0], "high": [150.0], "low": [150.0], "close": [150.0], "volume": [1]},
                               index=pd.DatetimeIndex([self.now.replace(second=0)]))
        ok, d = self._run(price=150.0, df=pd.concat([df, forming]))
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "outside_bar_atr_band")

    def test_prior_session_bars_ignored(self):
        old = _bars(self.now.replace(second=0) - timedelta(days=1), n=6)
        ok, d = self._run(df=old)
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "too_few_session_bars")

    def test_no_trade_kept_suspect(self):
        with mock.patch.object(gex, "_get_spot_trade", return_value=None):
            self.assertEqual(gex._bar_validated_spot("SNDK"), (False, {"reason": "no_trade"}))

    def test_exception_kept_suspect(self):
        with mock.patch.object(gex, "_get_spot_trade", side_effect=RuntimeError("boom")):
            ok, d = gex._bar_validated_spot("SNDK")
        self.assertFalse(ok)
        self.assertEqual(d["reason"], "error")


class GetSpotTrade(unittest.TestCase):
    def _resp(self, code, body):
        r = mock.Mock()
        r.status_code = code
        r.json.return_value = body
        return r

    def test_parses_nanosecond_timestamp_and_conditions(self):
        body = {"trade": {"p": 1385.2, "t": "2026-10-19T15:00:10.123456789Z", "c": ["@", "I"]}}
        with mock.patch.object(gex.requests, "get", return_value=self._resp(200, body)):
            t = gex._get_spot_trade("SNDK")
        self.assertEqual(t["price"], 1385.2)
        self.assertEqual(t["conds"], ["@", "I"])
        self.assertEqual(t["ts"].microsecond, 123456)

    def test_http_error_and_bad_price_return_none(self):
        with mock.patch.object(gex.requests, "get", return_value=self._resp(500, {})):
            self.assertIsNone(gex._get_spot_trade("SNDK"))
        with mock.patch.object(gex.requests, "get",
                               return_value=self._resp(200, {"trade": {"p": 0, "t": "2026-10-19T15:00:10Z"}})):
            self.assertIsNone(gex._get_spot_trade("SNDK"))


if __name__ == "__main__":
    unittest.main()
