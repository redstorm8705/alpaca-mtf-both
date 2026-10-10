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


def _tbars(rows):
    """rows: list of (ET 'YYYY-MM-DD HH:MM', close, volume) -> a tz-aware (UTC) 5m frame like fetch_bars_ref."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0], tz="America/New_York").tz_convert("UTC") for r in rows])
    return pd.DataFrame({"close": [r[1] for r in rows], "high": [r[1] + 0.2 for r in rows],
                         "low": [r[1] - 0.2 for r in rows], "volume": [r[2] for r in rows]}, index=idx)


class FreshCross(unittest.TestCase):
    """Losers audit 2026-10-09 (board Kyle+LdP, Gro, GAI): a RIDE close-through must be a fresh cross."""

    def test_stale_break_waits_with_bars_logged(self):
        rows = _CALM + [(105.0, 105.2, 104.0, 1000)] + [(106.0, 106.5, 105.5, 1000)] * 5 + [(106.5, 107.0, 106.0, 9000)]
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")
        self.assertEqual(r["bars_since_cross"], 6)
        self.assertIn("not a fresh cross", r["reason"])

    def test_cross_three_bars_back_still_enters(self):
        rows = _CALM + [(105.0, 105.2, 104.0, 1000), (106.0, 106.5, 105.5, 1000), (106.2, 106.5, 105.9, 1000),
                        (106.5, 107.0, 106.0, 9000)]
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_bars(rows), levels=_levels())
        self.assertEqual((r["trigger"], r["bars_since_cross"]), ("ENTER", 3))

    def test_gap_above_wall_since_prior_session_is_not_a_cross(self):
        # SNXX/SNDK 10/09: above the wall since the prior close; the opening bars never closed below it.
        rows = [("2026-10-08 15:50", 106.0, 1000), ("2026-10-08 15:55", 106.2, 1000)] + \
               [(f"2026-10-09 09:{m:02d}", 106.4, 1000) for m in range(30, 50, 5)] + [("2026-10-09 09:50", 106.6, 9000)]
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=_tbars(rows), levels=_levels())
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIsNone(r["bars_since_cross"])

    def test_gap_through_counts_when_first_bar_closes_beyond(self):
        rows = [("2026-10-08 15:45", 104.0, 1000), ("2026-10-08 15:50", 104.2, 1000), ("2026-10-08 15:55", 104.5, 1000),
                ("2026-10-09 08:00", 105.5, 50), ("2026-10-09 09:00", 105.6, 60),           # thin premarket
                ("2026-10-09 09:30", 106.0, 9000)]
        frame = _tbars(rows)
        self.assertEqual(et._bars_since_cross(frame, 105.0, "long"), 1)

    def test_premarket_bars_do_not_make_the_open_bar_heavy(self):
        # Opening bar vs thin premarket: no regular-session baseline yet and no same-time prior bar -> not confirmed.
        rows = [("2026-10-09 08:00", 104.0, 50), ("2026-10-09 08:30", 104.1, 40), ("2026-10-09 09:00", 104.2, 60),
                ("2026-10-09 09:30", 106.0, 3000)]
        self.assertFalse(et._vol_ok(_tbars(rows)))

    def test_open_bar_uses_same_time_prior_sessions(self):
        rows = [("2026-10-08 09:30", 104.0, 2000), ("2026-10-08 12:00", 104.0, 100),
                ("2026-10-09 09:00", 104.2, 60), ("2026-10-09 09:30", 106.0, 3000)]
        self.assertTrue(et._vol_ok(_tbars(rows)))                       # 3000 >= 1.2 x 2000
        rows[0] = ("2026-10-08 09:30", 104.0, 3000)
        self.assertFalse(et._vol_ok(_tbars(rows)))                      # 3000 < 1.2 x 3000

    @staticmethod
    def _session(day, vol, close=104.0):
        out = []
        for i in range(78):                                   # a full regular session of 5m bars
            h, m = divmod(9 * 60 + 30 + 5 * i, 60)
            out.append((f"{day} {h:02d}:{m:02d}", close, vol))
        return out

    def test_realistic_frame_confirms_the_opening_gap_through(self):
        # Full prior sessions (vol 2000), thin premarket, then the open gaps above the 105 wall on 5000 shares.
        rows = self._session("2026-10-07", 2000) + self._session("2026-10-08", 2000) + \
            [("2026-10-09 08:00", 104.5, 30), ("2026-10-09 09:00", 104.8, 40), ("2026-10-09 09:30", 106.0, 5000)]
        frame = _tbars(rows)
        self.assertTrue(et._vol_ok(frame))                    # 5000 >= 1.2 x the prior 09:30 bars (2000)
        r = et.compute_entry_trigger("NVDA", _decision("RIDE"), bars=frame, levels=_levels())
        self.assertEqual((r["trigger"], r["direction"], r["bars_since_cross"]), ("ENTER", "long", 1))

    def test_fetch_requests_the_whole_window(self):
        with mock.patch.object(et, "fetch_bars_ref", return_value=None) as f:
            et._recent_bars("NVDA", None)
        self.assertGreaterEqual(f.call_args.kwargs.get("num_bars", f.call_args.args[-1] if f.call_args.args else 0), 400)

    def test_later_bars_use_todays_session_baseline(self):
        rows = [("2026-10-08 15:55", 104.0, 50000)] + \
               [(f"2026-10-09 09:{m:02d}", 104.0, 1000) for m in range(30, 50, 5)] + [("2026-10-09 09:50", 106.0, 1300)]
        self.assertTrue(et._vol_ok(_tbars(rows)))                       # vs today's 1000s, not yesterday's 50000


if __name__ == "__main__":
    unittest.main(verbosity=2)
