#!/usr/bin/env python3
# ruff: noqa: E501  — synthetic fixtures + assertions run long (matches tests/ convention)
"""
tests/test_day_tier_track_b.py — Track-B Increment-2 Part 1 pure helpers (screen + frame + adapter).
Design: logs/design_records/day_tier_track_b_inc2_2026-09-22.md (§7.63 pre-registered screen, §7c adapter).

Guards the three pure, fail-safe helpers that feed/route the Inc-1 momentum trigger:
  screen_mover()        — is_mover ONLY when |gap%| in [2%, 60%], RVOL >= 3x, price >= $5; a >60% move
                          (split/bad data) rejects (NFLX-split lesson); fail-safe on any bad input.
  build_session_frame() — today's RTH 5m frame FROM the 09:30 ET open; a non-contiguous frame (a HALT) or
                          a first bar that is not the 09:30 open -> None (never trade a halted/misaligned name).
  momentum_to_entry()   — maps an ENTER momentum result to place_entry's (decision, trigger): wall_ref ==
                          structural_level, mode carried, target None, conviction shorts-smaller.

Frame convention (matches the Inc-1 test): UTC-indexed 5m bars; 13:30 UTC == 09:30 EDT (Sept = EDT).
"""
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pandas as pd  # noqa: E402

from strategy import day_tier_track_b as B  # noqa: E402

ET = ZoneInfo("America/New_York")
_NOW = datetime(2026, 9, 21, 10, 30, tzinfo=ET)  # 60 min into the session (frac = 60/390 ≈ 0.1538)
# For the frame tests: an 8-bar 09:30-open frame ends at 10:05 EDT, so a "now" ~2 min later keeps the
# newest bar inside the end-of-frame freshness window (B1). A far-later now is the stale/halt case.
_FRAME_NOW = datetime(2026, 9, 21, 10, 7, tzinfo=ET)


def _frame(closes, vols, start="2026-09-21 13:30"):
    """UTC-indexed 5m OHLCV frame from a list of closes + volumes (13:30 UTC == 09:30 EDT)."""
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz="UTC")
    rows = [(c, c + 1.0, c - 1.0, c, v) for c, v in zip(closes, vols, strict=True)]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# ── screen_mover ─────────────────────────────────────────────────────────────
class ScreenMover(unittest.TestCase):
    def test_mover_up(self):
        # price 103 vs prior_close 100 -> +3% gap; vol 5x100k=500k vs adv 1M x 0.1538 ≈ 153.8k -> RVOL ≈ 3.25x
        df = _frame([103.0] * 5, [100_000] * 5)
        r = B.screen_mover("NVDA", df, prior_close=100.0, avg_daily_volume=1_000_000, now_et=_NOW)
        self.assertTrue(r["is_mover"])
        self.assertEqual(r["gap_direction"], "up")
        self.assertGreaterEqual(r["rvol"], 3.0)

    def test_mover_down(self):
        df = _frame([97.0] * 5, [100_000] * 5)
        r = B.screen_mover("NVDA", df, prior_close=100.0, avg_daily_volume=1_000_000, now_et=_NOW)
        self.assertTrue(r["is_mover"])
        self.assertEqual(r["gap_direction"], "down")

    def test_gap_below_threshold_not_mover(self):
        df = _frame([100.5] * 5, [100_000] * 5)  # +0.5% < 2%
        r = B.screen_mover("NVDA", df, prior_close=100.0, avg_daily_volume=1_000_000, now_et=_NOW)
        self.assertFalse(r["is_mover"])

    def test_split_like_move_rejected(self):
        df = _frame([150.0] * 5, [100_000] * 5)  # +66% vs 90 -> exceeds the 60% sanity cap
        r = B.screen_mover("NVDA", df, prior_close=90.0, avg_daily_volume=1_000_000, now_et=_NOW)
        self.assertFalse(r["is_mover"])
        self.assertEqual(r["gap_direction"], "none")

    def test_price_floor_rejected(self):
        df = _frame([4.0] * 5, [100_000] * 5)  # < $5 floor
        r = B.screen_mover("PENNY", df, prior_close=3.5, avg_daily_volume=1_000_000, now_et=_NOW)
        self.assertFalse(r["is_mover"])

    def test_low_rvol_not_mover(self):
        df = _frame([103.0] * 5, [20_000] * 5)  # 100k vol vs ~153.8k expected -> RVOL ≈ 0.65x
        r = B.screen_mover("NVDA", df, prior_close=100.0, avg_daily_volume=1_000_000, now_et=_NOW)
        self.assertFalse(r["is_mover"])

    def test_adv_unavailable_fail_closed(self):
        df = _frame([103.0] * 5, [100_000] * 5)
        r = B.screen_mover("NVDA", df, prior_close=100.0, avg_daily_volume=0, now_et=_NOW)
        self.assertFalse(r["is_mover"])

    def test_insufficient_frame_not_mover(self):
        df = _frame([103.0] * 3, [100_000] * 3)  # < _MIN_FRAME_BARS
        r = B.screen_mover("NVDA", df, prior_close=100.0, avg_daily_volume=1_000_000, now_et=_NOW)
        self.assertFalse(r["is_mover"])

    def test_bad_input_fail_safe(self):
        r = B.screen_mover("NVDA", None, prior_close=None, avg_daily_volume=None, now_et=_NOW)
        self.assertFalse(r["is_mover"])  # never raises


# ── track_b_conviction ───────────────────────────────────────────────────────
class Conviction(unittest.TestCase):
    def test_drive_vs_pullback(self):
        self.assertAlmostEqual(B.track_b_conviction("DRIVE", "long"), 0.5, places=3)
        self.assertAlmostEqual(B.track_b_conviction("PULLBACK", "long"), 0.6, places=3)

    def test_shorts_smaller(self):
        self.assertLess(B.track_b_conviction("DRIVE", "short"), B.track_b_conviction("DRIVE", "long"))
        self.assertAlmostEqual(B.track_b_conviction("PULLBACK", "short"), 0.6 * 0.7, places=3)

    def test_bad_input_safe(self):
        self.assertGreaterEqual(B.track_b_conviction(None, None), 0.0)  # never raises


# ── momentum_to_entry ────────────────────────────────────────────────────────
class Adapter(unittest.TestCase):
    def _mom(self, direction="long", mode="DRIVE"):
        # Real geometry: a long breaks ABOVE the OR level (entry > level); a short breaks BELOW (entry < level).
        entry, level = (103.0, 101.0) if direction == "long" else (99.0, 101.0)
        return {"symbol": "NVDA", "trigger": "ENTER", "direction": direction, "mode": mode,
                "entry_ref": entry, "structural_level": level, "vwap": 102.0,
                "vol_confirmed": True, "reason": "confirmed continuation"}

    def test_enter_long_maps_stop_to_structural_level(self):
        decision, trigger = B.momentum_to_entry(self._mom("long", "DRIVE"), gap_direction="up")
        self.assertEqual(trigger["trigger"], "ENTER")
        self.assertEqual(trigger["direction"], "long")
        self.assertEqual(trigger["mode"], "DRIVE")
        self.assertEqual(trigger["wall_ref"], 101.0)   # structural_level -> wall_ref (the stop reference)
        self.assertIsNone(trigger["target"])           # RIDE-style -> R-multiple OCO take-profit at wire time
        self.assertEqual(trigger["entry_ref"], 103.0)
        self.assertTrue(decision["would_consider"])
        self.assertEqual(decision["side"], "LONG")
        self.assertEqual(decision["track"], "B")
        self.assertAlmostEqual(decision["conviction"], 0.5, places=3)

    def test_enter_short_smaller_conviction(self):
        d_long, _ = B.momentum_to_entry(self._mom("long", "DRIVE"))
        d_short, t_short = B.momentum_to_entry(self._mom("short", "DRIVE"))
        self.assertEqual(t_short["direction"], "short")
        self.assertEqual(d_short["side"], "SHORT")
        self.assertLess(d_short["conviction"], d_long["conviction"])

    def test_non_enter_momentum_yields_wait(self):
        _, trigger = B.momentum_to_entry({"symbol": "NVDA", "trigger": "WAIT"}, gap_direction="up")
        self.assertNotEqual(trigger["trigger"], "ENTER")

    def test_malformed_yields_wait(self):
        _, trigger = B.momentum_to_entry(None)
        self.assertNotEqual(trigger["trigger"], "ENTER")  # never raises

    def test_missing_direction_yields_wait(self):
        _, trigger = B.momentum_to_entry({"symbol": "NVDA", "trigger": "ENTER", "direction": "none"})
        self.assertNotEqual(trigger["trigger"], "ENTER")

    def test_enter_missing_structural_level_yields_wait(self):
        # An ENTER with a None stop reference must NOT emit an ENTER with wall_ref=None (adversarial N4).
        mom = self._mom("long", "DRIVE")
        mom["structural_level"] = None
        _, trigger = B.momentum_to_entry(mom)
        self.assertNotEqual(trigger["trigger"], "ENTER")

    def test_enter_wrong_side_geometry_yields_wait(self):
        # A long whose structural_level is ABOVE entry (wrong side -> would invert the stop) -> WAIT.
        mom = self._mom("long", "DRIVE")
        mom["structural_level"] = 105.0  # >= entry_ref 103.0 -> invalid geometry for a long
        _, trigger = B.momentum_to_entry(mom)
        self.assertNotEqual(trigger["trigger"], "ENTER")


# ── build_session_frame (fetch_bars_window injected) ─────────────────────────
class SessionFrame(unittest.TestCase):
    def setUp(self):
        # Inject a controllable fake data.fetcher so the test is independent of the alpaca SDK.
        self._orig = sys.modules.get("data.fetcher")
        self._fake = types.ModuleType("data.fetcher")
        self._ret = pd.DataFrame()
        self._fake.fetch_bars_window = lambda *a, **k: self._ret  # noqa: E731
        sys.modules["data.fetcher"] = self._fake

    def tearDown(self):
        if self._orig is not None:
            sys.modules["data.fetcher"] = self._orig
        else:
            sys.modules.pop("data.fetcher", None)

    def _utc(self, n_bars, start="2026-09-21 13:30", drop=None):
        idx = pd.date_range(start, periods=n_bars, freq="5min", tz="UTC")
        if drop is not None:
            idx = idx[[i for i in range(n_bars) if i != drop]]
        rows = [(100.0, 101.0, 99.0, 100.5, 1000)] * len(idx)
        return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)

    def test_contiguous_from_open_returns_frame(self):
        self._ret = self._utc(8)  # 13:30..14:05 UTC = 09:30..10:05 EDT, contiguous; newest bar fresh vs _FRAME_NOW
        df = B.build_session_frame("NVDA", now_et=_FRAME_NOW)
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 8)

    def test_stale_frame_returns_none(self):
        # Contiguous from the open, but the newest bar (10:05 EDT) is ~55 min before now = a still-halted /
        # stalled name (B1). Must be rejected even though internal contiguity is intact.
        self._ret = self._utc(8)
        self.assertIsNone(B.build_session_frame("NVDA", now_et=_NOW))  # _NOW = 10:30 -> age 25 min > 10 min

    def test_halt_gap_returns_none(self):
        self._ret = self._utc(8, drop=4)  # a missing middle bar = a HALT
        self.assertIsNone(B.build_session_frame("NVDA", now_et=_FRAME_NOW))

    def test_first_bar_not_open_returns_none(self):
        self._ret = self._utc(8, start="2026-09-21 13:35")  # 09:35 EDT, not the 09:30 open
        self.assertIsNone(B.build_session_frame("NVDA", now_et=_FRAME_NOW))

    def test_too_few_bars_returns_none(self):
        self._ret = self._utc(3)
        self.assertIsNone(B.build_session_frame("NVDA", now_et=_FRAME_NOW))

    def test_empty_fetch_returns_none(self):
        self._ret = pd.DataFrame()
        self.assertIsNone(B.build_session_frame("NVDA", now_et=_FRAME_NOW))

    def test_pre_open_returns_none(self):
        pre = datetime(2026, 9, 21, 9, 0, tzinfo=ET)  # before the open
        self.assertIsNone(B.build_session_frame("NVDA", now_et=pre))


if __name__ == "__main__":
    unittest.main()
