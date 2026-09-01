#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for strategy/day_tier_side.compute_side_bias (Day-Tier Layer A, INERT).

Proves the SIDE-bias scoring + every fail-safe branch by mocking the T1 fetch_bars with
synthetic close series:
  * uptrend (close above the whole stack)   -> LONG
  * downtrend (close below the whole stack) -> SHORT
  * flat (close == every MA, direction 0)   -> TWO_SIDED, score 0
  * daily fetch None                        -> UNKNOWN (no anchor)
  * fetch_bars raises                       -> UNKNOWN (never propagates)
  * thin daily history + no weekly/monthly  -> UNKNOWN (coverage < floor)
  * weekly/monthly missing but daily rich   -> still leans (partial-coverage normalization)

Runs with plain unittest:  python3 -m unittest tests.test_day_tier_side
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

# Stub the Alpaca SDK so data.fetcher imports at load (fetch_bars is mock-patched in every test).
for _mod in ("alpaca", "alpaca.data", "alpaca.data.timeframe",
             "alpaca.data.requests", "alpaca.data.historical"):
    sys.modules.setdefault(_mod, mock.MagicMock())

import pandas as pd  # noqa: E402

from strategy import day_tier_side as ds  # noqa: E402


def _frame(values):
    """A fetch_bars-shaped frame with a 'close' column from `values`."""
    return pd.DataFrame({"close": list(values)})


def _up(n, start=50.0, step=0.5):
    """Monotonic uptrend of length n -> last close is the max, above every trailing MA."""
    return _frame([start + i * step for i in range(n)])


def _down(n, start=400.0, step=0.5):
    """Monotonic downtrend of length n -> last close is the min, below every trailing MA."""
    return _frame([start - i * step for i in range(n)])


def _flat(n, level=100.0):
    """Constant series -> every MA equals the close -> direction 0 -> score 0."""
    return _frame([level] * n)


class DayTierSide(unittest.TestCase):
    def _patch(self, side_effect):
        """Patch ds.fetch_bars with a (symbol, timeframe, num_bars) -> frame|None|raise fn."""
        return mock.patch.object(ds, "fetch_bars", mock.Mock(side_effect=side_effect))

    def _by_tf(self, daily=None, weekly=None, monthly=None, raise_all=False):
        def _fn(symbol, timeframe, num_bars=0):
            if raise_all:
                raise RuntimeError("boom")
            import config
            if timeframe == config.TF_DAILY:
                return daily
            if timeframe == config.TF_WEEKLY:
                return weekly
            if timeframe == config.TF_MONTHLY:
                return monthly
            return None
        return _fn

    # 1 -- full uptrend across all timeframes -> LONG, score high, full coverage
    def test_uptrend_is_long(self):
        with self._patch(self._by_tf(daily=_up(360), weekly=_up(14), monthly=_up(13))):
            r = ds.compute_side_bias("NVDA")
        self.assertEqual(r["side"], "LONG")
        self.assertGreaterEqual(r["score"], ds._LONG_THRESHOLD)
        self.assertGreaterEqual(r["weight_coverage"], 0.99)
        self.assertTrue(all(v == 1 for v in r["stack"].values()))

    # 2 -- full downtrend -> SHORT
    def test_downtrend_is_short(self):
        with self._patch(self._by_tf(daily=_down(360), weekly=_down(14), monthly=_down(13))):
            r = ds.compute_side_bias("META")
        self.assertEqual(r["side"], "SHORT")
        self.assertLessEqual(r["score"], ds._SHORT_THRESHOLD)
        self.assertTrue(all(v == -1 for v in r["stack"].values()))

    # 3 -- flat (close == every MA) -> TWO_SIDED, score exactly 0
    def test_flat_is_two_sided(self):
        with self._patch(self._by_tf(daily=_flat(360), weekly=_flat(14), monthly=_flat(13))):
            r = ds.compute_side_bias("AAPL")
        self.assertEqual(r["side"], "TWO_SIDED")
        self.assertEqual(r["score"], 0.0)
        self.assertGreaterEqual(r["weight_coverage"], 0.99)

    # 4 -- daily fetch None -> UNKNOWN (no anchor price)
    def test_no_daily_is_unknown(self):
        with self._patch(self._by_tf(daily=None, weekly=_up(14), monthly=_up(13))):
            r = ds.compute_side_bias("TSLA")
        self.assertEqual(r["side"], "UNKNOWN")
        self.assertIsNone(r["score"])
        self.assertIn("daily", r["reason"].lower())

    # 5 -- fetch_bars RAISES -> UNKNOWN, never propagates
    def test_fetch_raises_is_unknown(self):
        with self._patch(self._by_tf(raise_all=True)):
            r = ds.compute_side_bias("GOOGL")
        self.assertEqual(r["side"], "UNKNOWN")
        self.assertIsNone(r["score"])

    # 6 -- thin daily (30 bars) + no weekly/monthly -> coverage < floor -> UNKNOWN
    def test_thin_coverage_is_unknown(self):
        with self._patch(self._by_tf(daily=_up(30), weekly=None, monthly=None)):
            r = ds.compute_side_bias("AMZN")
        self.assertEqual(r["side"], "UNKNOWN")
        self.assertLess(r["weight_coverage"], ds._MIN_WEIGHT_COVERAGE)
        # the fast MAs DID vote (recorded), even though overall coverage failed
        self.assertEqual(r["stack"].get("ema13"), 1)
        self.assertIsNone(r["stack"].get("sma325"))

    # 7 -- weekly/monthly missing but daily rich -> still leans (partial-coverage normalized)
    def test_partial_coverage_still_leans(self):
        with self._patch(self._by_tf(daily=_up(360), weekly=None, monthly=None)):
            r = ds.compute_side_bias("MSFT")
        self.assertEqual(r["side"], "LONG")
        self.assertGreaterEqual(r["weight_coverage"], ds._MIN_WEIGHT_COVERAGE)
        self.assertLess(r["weight_coverage"], 1.0)          # 10wk/10mo did NOT vote
        self.assertIsNone(r["stack"].get("sma_10week"))
        self.assertIsNone(r["stack"].get("sma_10month"))

    # 8 -- score is always in [-1, 1] and the result shape is stable
    def test_score_bounds_and_shape(self):
        with self._patch(self._by_tf(daily=_up(360), weekly=_up(14), monthly=_up(13))):
            r = ds.compute_side_bias("NVDA")
        self.assertTrue(-1.0 <= r["score"] <= 1.0)
        for k in ("symbol", "side", "score", "stack", "weight_coverage", "reason"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
