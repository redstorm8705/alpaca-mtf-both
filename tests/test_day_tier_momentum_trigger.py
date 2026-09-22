#!/usr/bin/env python3
# ruff: noqa: E501  — synthetic OHLCV fixtures + assertions run long (matches tests/ convention)
"""
tests/test_day_tier_momentum_trigger.py — Track-B momentum entry trigger (Inc 1, instrument-scaled).
Design: logs/design_records/day_tier_track_b_momentum_2026-09-21.md (§7c of day_tier_v2).

Guards the CONFIRMED-CONTINUATION contract (hardened twice under the Inc-1 cold-2nd + adversarial gate):
  ENTER only on a volume-confirmed, non-over-extended BREAK-AND-HOLD of a TRADEABLE opening-range level
  on the gap side — a buffered break (floored vs price so a tiny OR can't collapse it), a confirmed hold
  (>= 1 bar after the CURRENT-held-episode start; the episode anchors on the current run, not the first
  break, so an early shakeout does not poison a later clean break), no round-trip / no deep post-break
  wick (capped vs price), not over-extended, latest close on the correct side of VWAP (unavailable -> WAIT).
  DRIVE (recent) vs PULLBACK (a real retest); an old break with no retest -> WAIT. Fail-safe on any error.

Standard OR (first 3 bars): OR high 101, OR low 99, range 2.0, prices ~100-103
  => break buffer 0.20, wick tol ~0.515, retest-near ~0.257, max extension 4.0, min-OR ~0.15.
"""
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pandas as pd  # noqa: E402

from strategy import day_tier_momentum_trigger as M  # noqa: E402


def _frame(rows, start="2026-09-21 13:30", tz="UTC"):
    idx = pd.date_range(start, periods=len(rows), freq="5min", tz=tz)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


_OR = [(99.5, 100.0, 99.0, 99.8, 1000),
       (99.8, 101.0, 99.5, 100.5, 1000),   # OR high = 101
       (100.5, 100.8, 100.0, 100.5, 1000)]  # OR low = 99


class MomentumTrigger(unittest.TestCase):
    def setUp(self):
        self._orig = M._vwap_last

    def tearDown(self):
        M._vwap_last = self._orig

    def _vwap(self, val):
        M._vwap_last = lambda df: val

    # ── DRIVE long ──────────────────────────────────────────────────────────────
    def test_drive_long(self):
        self._vwap(101.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),   # break: close 101.5 > 101.20
                      (101.5, 103.5, 102.0, 103.0, 3000)]    # latest holds; vol 3x
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["direction"], "long")
        self.assertEqual(r["mode"], "DRIVE")
        self.assertAlmostEqual(r["structural_level"], 101.0, places=4)
        self.assertAlmostEqual(r["entry_ref"], 103.0, places=4)

    # ── PULLBACK long (a real post-break retest) ────────────────────────────────
    def test_pullback_long(self):
        self._vwap(101.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 101.6, 101.0, 101.3, 1000),   # retest: low 101.0 <= 101.257, close holds
                      (101.3, 103.0, 101.8, 102.5, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["mode"], "PULLBACK")

    # ── A1c: an early shakeout does NOT poison a later clean break -> ENTER ──────
    def test_shakeout_then_clean_break_enters(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 102.0, 100.4, 101.5, 1000),   # early break
                      (101.5, 101.6, 99.8, 100.2, 1000),    # FADE back below 101 (shakeout)
                      (100.2, 102.5, 100.1, 101.6, 1000),   # RE-break, close 101.6 > 101.20
                      (101.6, 102.0, 101.3, 101.7, 1000),   # hold
                      (101.7, 103.5, 101.5, 102.8, 3000)]    # latest holds
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "ENTER")           # the current held episode is the clean break
        self.assertEqual(r["direction"], "long")

    # ── round-trip with only a 1-bar reclaim (episode too short) -> WAIT ─────────
    def test_round_trip_short_reclaim_is_wait(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 102.0, 100.4, 101.5, 1000),
                      (101.5, 101.6, 99.8, 100.2, 1000),    # fade
                      (100.2, 103.0, 100.1, 102.5, 3000)]    # reclaim held only 1 bar -> no confirming hold
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── break on the LATEST bar, no confirming hold -> WAIT ──────────────────────
    def test_break_on_latest_bar_is_wait(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 100.9, 100.4, 100.9, 1000),   # no break
                      (100.9, 101.7, 100.4, 101.5, 3000)]    # break is the latest bar
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── deep wick flush AFTER the break -> WAIT ─────────────────────────────────
    def test_deep_wick_flush_is_wait(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 101.6, 99.5, 101.3, 1000),    # flush low 99.5 < 101 - 0.515
                      (101.3, 103.0, 100.6, 102.5, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── one-tick clear below the buffer is not a structural break -> WAIT ────────
    def test_one_tick_break_below_buffer_is_wait(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 101.2, 100.4, 101.05, 3000),  # close 101.05 < 101.20 buffer
                      (101.05, 101.15, 100.6, 101.10, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── tiny opening range (not a tradeable structure) -> WAIT ──────────────────
    def test_tiny_opening_range_is_wait(self):
        self._vwap(99.0)
        tiny = [(99.99, 100.02, 99.98, 100.00, 1000),
                (100.00, 100.02, 99.98, 100.01, 1000),
                (100.01, 100.02, 99.99, 100.00, 1000)]      # OR range 0.04 < 0.0015 x price
        rows = tiny + [(100.00, 100.5, 100.0, 100.3, 3000),
                       (100.3, 100.6, 100.1, 100.5, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("too tight", r["reason"])

    # ── over-extended entry (huge implied stop) -> WAIT ─────────────────────────
    def test_over_extension_is_wait(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 106.5, 101.8, 106.0, 3000)]    # close 106 is > 4.0 beyond level 101
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── unconfirmed buffered break on the LATEST bar (prior bar held only sub-buffer) -> WAIT ─
    def test_unconfirmed_latest_buffered_break_is_wait(self):
        self._vwap(101.0)
        rows = _OR + [(100.5, 101.15, 100.6, 101.10, 1000),  # weak RAW hold (101.10 > 101) but < 101.20 buffer
                      (101.10, 101.7, 101.0, 101.50, 3000)]   # the ONLY buffered break is the latest bar
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── huge OR: the extension PRICE cap rejects a 20%-style chase -> WAIT ───────
    def test_huge_or_extension_price_cap_is_wait(self):
        self._vwap(100.0)
        huge = [(100.0, 105.0, 100.0, 103.0, 1000),
                (103.0, 110.0, 100.0, 108.0, 1000),   # OR high 110, OR low 100, range 10
                (108.0, 109.0, 104.0, 106.0, 1000)]
        rows = huge + [(106.0, 112.0, 105.5, 111.5, 1000),   # buffered break above 111
                       (111.5, 118.0, 110.5, 117.0, 3000)]    # close 117 is 7 above level 110 > 0.05 x 117 cap
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── old break, no retest, not recent -> WAIT (neither DRIVE nor PULLBACK) ────
    def test_old_break_no_retest_is_wait(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),   # break
                      (101.5, 102.5, 101.5, 102.0, 1000),   # runs, low 101.5 (no retest near 101.257)
                      (102.0, 102.6, 101.9, 102.3, 1000),
                      (102.3, 102.8, 102.1, 102.5, 1000),
                      (102.5, 103.0, 102.2, 102.7, 1000),
                      (102.7, 103.5, 102.4, 103.0, 3000)]    # latest; episode start 5 bars back, no retest
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── VWAP unavailable -> WAIT (hard filter fail-CLOSED) ──────────────────────
    def test_vwap_none_is_wait(self):
        self._vwap(None)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 103.5, 102.0, 103.0, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("VWAP unavailable", r["reason"])

    # ── VWAP filter blocks a long below VWAP ────────────────────────────────────
    def test_vwap_filter_blocks_long_below_vwap(self):
        self._vwap(105.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 103.5, 102.0, 103.0, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── NaN latest-bar volume -> WAIT (fail-CLOSED, cold-2nd T1) ─────────────────
    def test_nan_latest_volume_is_wait(self):
        self._vwap(101.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 103.5, 102.0, 103.0, float("nan"))]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── SHORT (gap-down that fails), mirror ─────────────────────────────────────
    def test_drive_short(self):
        self._vwap(99.0)
        rows = _OR + [(99.0, 99.1, 97.5, 98.0, 1000),       # break below 98.80 (99 - 0.20)
                      (98.0, 98.0, 96.5, 97.0, 3000)]        # latest holds < 99; high 98 (no wick > 99.5)
        r = M.compute_momentum_trigger("X", "down", _frame(rows))
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["direction"], "short")
        self.assertAlmostEqual(r["structural_level"], 99.0, places=4)

    def test_gap_up_does_not_take_a_short(self):
        self._vwap(99.0)
        rows = _OR + [(99.0, 99.1, 97.5, 98.0, 1000),
                      (98.0, 98.0, 96.5, 97.0, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    def test_no_gap_evaluates_both_sides(self):
        self._vwap(99.0)
        rows = _OR + [(99.0, 99.1, 97.5, 98.0, 1000),
                      (98.0, 98.0, 96.5, 97.0, 3000)]
        r = M.compute_momentum_trigger("X", None, _frame(rows))
        self.assertEqual(r["trigger"], "ENTER")
        self.assertEqual(r["direction"], "short")

    # ── Volume not confirmed -> WAIT ────────────────────────────────────────────
    def test_no_volume_confirmation_is_wait(self):
        self._vwap(101.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 103.5, 102.0, 103.0, 900)]     # latest 900 < 1.2 x median(1000)
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    # ── No break of the opening range -> WAIT ───────────────────────────────────
    def test_no_break_is_wait(self):
        self._vwap(100.0)
        rows = _OR + [(100.5, 100.9, 100.0, 100.6, 3000),
                      (100.6, 100.95, 100.1, 100.7, 3000),
                      (100.7, 100.99, 100.2, 100.8, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")

    def test_past_cutoff_is_wait(self):
        self._vwap(101.0)
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000)] * 20
        r = M.compute_momentum_trigger("X", "up", _frame(rows))
        self.assertEqual(r["trigger"], "WAIT")
        self.assertIn("cutoff", r["reason"])

    def test_insufficient_bars_is_wait(self):
        r = M.compute_momentum_trigger("X", "up", _frame(_OR[:2]))
        self.assertEqual(r["trigger"], "WAIT")

    def test_failsafe_on_bad_bars(self):
        class _Boom:
            empty = False
            columns = ["high", "low", "close"]

            def __len__(self):
                raise RuntimeError("boom")

        r = M.compute_momentum_trigger("X", "up", _Boom())
        self.assertEqual(r["trigger"], "WAIT")

    def test_real_vwap_integration_drive_long(self):
        rows = _OR + [(100.5, 102.0, 100.6, 101.5, 1000),
                      (101.5, 103.5, 102.0, 103.0, 3000)]
        r = M.compute_momentum_trigger("X", "up", _frame(rows))  # real _vwap_last
        self.assertEqual(r["trigger"], "ENTER")
        self.assertIsNotNone(r["vwap"])


if __name__ == "__main__":
    unittest.main()
