#!/usr/bin/env python3
# ruff: noqa: E501
"""
Unit tests for strategy/day_tier_entry_trigger.compute_entry_trigger (Day-Tier Track-A entry
trigger, INERT). Injects synthetic 5m bars + GEX levels directly (no mocking), proving the
failed-sweep (FADE) / close-through (RIDE) detection, the volume confirmation, and every fail-safe.

Runs with plain unittest:  python3 -m unittest tests.test_day_tier_entry_trigger
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

# config import chain is light, but stub alpaca/requests defensively so the harness runs anywhere.
for _mod in ("alpaca", "alpaca.data", "alpaca.data.timeframe", "requests"):
    sys.modules.setdefault(_mod, mock.MagicMock())

import pandas as pd  # noqa: E402

from strategy import day_tier_entry_trigger as et  # noqa: E402


def _bars(rows):
    """rows: list of (close, high, low, volume) -> a fetch_bars-shaped 5m frame."""
    return pd.DataFrame({"close": [r[0] for r in rows], "high": [r[1] for r in rows],
                         "low": [r[2] for r in rows], "volume": [r[3] for r in rows]})


def _levels(call_wall=105.0, put_wall=95.0, centroid=100.0, levels_ok=True):
    return {"label": "POSITIVE", "spot": 100.0, "centroid": centroid, "wall": 100.0,
            "call_wall": call_wall, "put_wall": put_wall, "confidence": 0.7, "dispersion": 2.0,
            "expiry": "2026-09-05", "dte": 1, "age_minutes": 2.0, "levels_ok": levels_ok}


def _decision(action="FADE", would_consider=True):
    return {"symbol": "NVDA", "side": "SHORT", "gex_action": action, "act_ok": True,
            "strength": 0.7, "would_consider": would_consider, "conviction": 0.6}


# 6 calm bars (vol 1000) then a big-volume trigger bar appended per-test.
_CALM = [(100.0, 100.5, 99.5, 1000)] * 6


class EntryTrigger(unittest.TestCase):
    # 1 -- FADE: failed UPSIDE sweep of call_wall + volume -> ENTER short toward pin
    def test_fade_failed_upside_sweep(self):
        rows = _CALM + [(105.6, 105.8, 104.0, 3000), (104.0, 104.2, 103.5, 3000)]  # poke >105, close back <105
        r = et.compute_entry_trigger("NVDA", _decision("FADE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["direction"], "short")
        self.assertEqual(r["target"], 100.0)          # the pin centroid
        self.assertTrue(r["vol_confirmed"])

    # 2 -- FADE: failed DOWNSIDE sweep of put_wall -> ENTER long
    def test_fade_failed_downside_sweep(self):
        rows = _CALM + [(94.4, 96.0, 94.2, 3000), (96.0, 96.2, 95.5, 3000)]   # poke <95, close back >95
        r = et.compute_entry_trigger("NVDA", _decision("FADE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["direction"], "long")

    # 3 -- RIDE: close-through ABOVE call_wall -> ENTER long
    def test_ride_break_up(self):
        rows = _CALM + [(105.0, 105.2, 104.0, 3000), (106.0, 106.5, 105.0, 3000)]  # latest close >105
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["direction"], "long")
        self.assertEqual(r["wall_ref"], 105.0)

    # 4 -- RIDE: close-through BELOW put_wall -> ENTER short
    def test_ride_break_down(self):
        rows = _CALM + [(95.0, 96.0, 94.5, 3000), (94.0, 94.5, 93.5, 3000)]   # latest close <95
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["direction"], "short")

    # 5 -- candidate present but volume NOT confirmed -> WAIT
    def test_break_without_volume_waits(self):
        rows = _CALM + [(105.0, 105.2, 104.0, 1000), (106.0, 106.5, 105.0, 1000)]  # vol not elevated
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")
        self.assertFalse(r["vol_confirmed"])
        self.assertIn("volume not confirmed", r["reason"])

    # 6 -- not a would_consider candidate -> WAIT
    def test_not_candidate_waits(self):
        rows = _CALM + [(106.0, 106.5, 105.0, 3000)]
        r = et.compute_entry_trigger("NVDA", _decision("RIDE", would_consider=False), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("would_consider", r["reason"])

    # 7 -- levels not ok -> WAIT
    def test_no_levels_waits(self):
        rows = _CALM + [(106.0, 106.5, 105.0, 3000)]
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels(levels_ok=False))
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("no actionable GEX levels", r["reason"])

    # 8 -- insufficient bars -> WAIT
    def test_insufficient_bars_waits(self):
        rows = [(100.0, 100.5, 99.5, 1000)] * 3   # < _MIN_BARS
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("insufficient bars", r["reason"])

    # 9 -- FADE with no failed sweep (price never poked the wall) -> WAIT
    def test_fade_no_sweep_waits(self):
        rows = _CALM + [(101.0, 101.5, 100.5, 3000), (101.0, 101.2, 100.5, 3000)]  # nowhere near a wall
        r = et.compute_entry_trigger("NVDA", _decision("FADE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("no failed wall-sweep", r["reason"])

    # 10 -- garbage bars (missing columns) -> WAIT, never raises
    def test_garbage_bars_fail_safe(self):
        bad = pd.DataFrame({"close": [1.0] * 8})   # no high/low/volume
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=bad, levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")   # vol_ok False -> no ENTER; never raises

    # 11 -- a RIDE up-break must NOT also fire when close is below call_wall (no false ENTER)
    def test_ride_no_break_waits(self):
        rows = _CALM + [(104.0, 104.5, 103.0, 3000), (104.0, 104.2, 103.5, 3000)]  # close 104 < 105 wall
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("no wall close-through", r["reason"])

    # 12 -- result shape is stable
    def test_shape_stable(self):
        rows = _CALM + [(106.0, 106.5, 105.0, 3000)]
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        for k in ("symbol", "trigger", "direction", "mode", "entry_ref", "target", "wall_ref", "vol_confirmed", "reason"):
            self.assertIn(k, r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
