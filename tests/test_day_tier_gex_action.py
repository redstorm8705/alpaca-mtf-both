#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for strategy/day_tier_gex_action.compute_gex_action (Day-Tier Layer B, INERT).

Mocks the GEX reads (data.gex.get_gex_levels / get_gex_regime) and injects the ET clock, proving
the mode decision (design §2), sign-reliability (§1.6), DTE/TOD conditioning (§1.5), the act_ok
gate, targets, side-bias carry-through, and every fail-safe branch.

Runs with plain unittest:  python3 -m unittest tests.test_day_tier_gex_action
"""
from __future__ import annotations

import sys
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo
import unittest

# data.gex imports `requests` at load; stub it so the harness runs without the dependency.
sys.modules.setdefault("requests", mock.MagicMock())

from strategy import day_tier_gex_action as ga  # noqa: E402

ET = ZoneInfo("America/New_York")
_AM = datetime(2026, 9, 1, 10, 0, tzinfo=ET)   # morning
_PM = datetime(2026, 9, 1, 15, 0, tzinfo=ET)   # afternoon (>= 2pm)


def _levels(label="POSITIVE", levels_ok=True, confidence=0.7, centroid=101.0, wall=100.0,
            call_wall=105.0, put_wall=95.0, dte=1):
    return {"label": label, "spot": 100.5, "centroid": centroid, "wall": wall,
            "call_wall": call_wall, "put_wall": put_wall, "confidence": confidence,
            "dispersion": 2.0, "expiry": "2026-09-05", "dte": dte,
            "age_minutes": 2.0, "levels_ok": levels_ok}


def _regime(label="POSITIVE", raw_gex_m=3.2):
    return {"label": label, "raw_gex_m": raw_gex_m, "flip_strike": 99.0, "age_minutes": 2.0}


class GexAction(unittest.TestCase):
    def _patch(self, levels, regime):
        return mock.patch.multiple("data.gex",
                                   get_gex_levels=mock.Mock(return_value=levels),
                                   get_gex_regime=mock.Mock(return_value=regime))

    # 1 -- POSITIVE + resolved + confident -> FADE, act_ok True, pin targets
    def test_positive_is_fade(self):
        with self._patch(_levels("POSITIVE", dte=1, confidence=0.8), _regime("POSITIVE")):
            r = ga.compute_gex_action("NVDA", now_et=_PM)
        self.assertEqual(r["action"], "FADE")
        self.assertTrue(r["act_ok"])
        self.assertEqual(r["targets"], {"pin": 101.0, "wall": 100.0})
        self.assertTrue(r["sign_reliable"])

    # 2 -- NEGATIVE -> RIDE, wall targets (favored in the AM)
    def test_negative_is_ride(self):
        with self._patch(_levels("NEGATIVE", dte=1, confidence=0.8), _regime("NEGATIVE")):
            r = ga.compute_gex_action("NVDA", now_et=_AM)
        self.assertEqual(r["action"], "RIDE")
        self.assertTrue(r["act_ok"])
        self.assertEqual(r["targets"], {"call_wall": 105.0, "put_wall": 95.0})

    # 3 -- NEAR-FLIP / STALE / UNKNOWN -> STAND_DOWN
    def test_nonactionable_regime_stands_down(self):
        for lab in ("NEAR-FLIP", "STALE", "UNKNOWN"):
            with self._patch(_levels(lab), _regime(lab)):
                r = ga.compute_gex_action("NVDA", now_et=_PM)
            self.assertEqual(r["action"], "STAND_DOWN", lab)
            self.assertFalse(r["act_ok"], lab)

    # 4 -- levels_ok False -> STAND_DOWN even on a POSITIVE label
    def test_no_levels_stands_down(self):
        with self._patch(_levels("POSITIVE", levels_ok=False), _regime("POSITIVE")):
            r = ga.compute_gex_action("NVDA", now_et=_PM)
        self.assertEqual(r["action"], "STAND_DOWN")
        self.assertFalse(r["act_ok"])
        self.assertIn("no actionable pin levels", r["reason"])

    # 5 -- single name below the confidence floor -> sign NOT reliable -> act_ok False, mode reported
    def test_single_name_low_confidence_not_reliable(self):
        with self._patch(_levels("POSITIVE", confidence=0.10, dte=1), _regime("POSITIVE")):
            r = ga.compute_gex_action("NVDA", now_et=_PM)
        self.assertEqual(r["action"], "FADE")          # mode still reported (for the shadow log)
        self.assertFalse(r["sign_reliable"])
        self.assertFalse(r["act_ok"])

    # 6 -- an INDEX is sign-reliable at any resolved pin (even low confidence)
    def test_index_always_sign_reliable(self):
        with self._patch(_levels("POSITIVE", confidence=0.10, dte=1), _regime("POSITIVE")):
            r = ga.compute_gex_action("SPY", now_et=_PM)
        self.assertTrue(r["sign_reliable"])

    # 7 -- TOD: FADE is stronger in the PM than the AM (same inputs otherwise)
    def test_fade_pm_stronger_than_am(self):
        with self._patch(_levels("POSITIVE", confidence=0.8, dte=1), _regime("POSITIVE")):
            pm = ga.compute_gex_action("NVDA", now_et=_PM)
        with self._patch(_levels("POSITIVE", confidence=0.8, dte=1), _regime("POSITIVE")):
            am = ga.compute_gex_action("NVDA", now_et=_AM)
        self.assertGreater(pm["strength"], am["strength"])
        self.assertEqual(pm["tod_factor"], ga._TOD_BOOST)
        self.assertEqual(am["tod_factor"], ga._TOD_OFF)

    # 8 -- DTE proximity: near-expiry (0 DTE) pins harder than mid-cycle (>= _DTE_ZERO)
    def test_dte_proximity_curve(self):
        self.assertEqual(ga._dte_proximity(0), 1.0)
        self.assertEqual(ga._dte_proximity(ga._DTE_ZERO), ga._DTE_FLOOR_FACTOR)
        self.assertEqual(ga._dte_proximity(None), ga._DTE_FLOOR_FACTOR)
        mid = ga._dte_proximity((ga._DTE_FULL + ga._DTE_ZERO) / 2)
        self.assertTrue(ga._DTE_FLOOR_FACTOR < mid < 1.0)

    # 9 -- weak strength (low conf) -> act_ok False even though sign is reliable (index)
    def test_weak_strength_not_act_ok(self):
        # index -> sign reliable; but conf 0.05 * factors < _MIN_STRENGTH
        with self._patch(_levels("POSITIVE", confidence=0.05, dte=9), _regime("POSITIVE")):
            r = ga.compute_gex_action("SPY", now_et=_AM)
        self.assertTrue(r["sign_reliable"])
        self.assertLess(r["strength"], ga._MIN_STRENGTH)
        self.assertFalse(r["act_ok"])

    # 10 -- a raising GEX read -> STAND_DOWN, never propagates
    def test_read_raises_stands_down(self):
        with mock.patch("data.gex.get_gex_levels", mock.Mock(side_effect=RuntimeError("boom"))), \
             mock.patch("data.gex.get_gex_regime", mock.Mock(return_value=_regime())):
            r = ga.compute_gex_action("NVDA", now_et=_PM)
        self.assertEqual(r["action"], "STAND_DOWN")
        self.assertFalse(r["act_ok"])

    # 11 -- side_bias is carried through into the output
    def test_side_bias_carried(self):
        with self._patch(_levels("POSITIVE", dte=1, confidence=0.8), _regime("POSITIVE")):
            r = ga.compute_gex_action("NVDA", side_bias={"side": "LONG", "score": 0.6}, now_et=_PM)
        self.assertEqual(r["side"], "LONG")

    # 12 -- result shape is stable
    def test_shape_stable(self):
        with self._patch(_levels("POSITIVE", dte=1), _regime("POSITIVE")):
            r = ga.compute_gex_action("NVDA", now_et=_PM)
        for k in ("symbol", "action", "gex_label", "sign_reliable", "pin_confidence", "dte",
                  "pin_strength", "tod_factor", "strength", "targets", "side", "act_ok", "reason"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
