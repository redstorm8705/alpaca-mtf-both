# ruff: noqa: E501
"""execution/dip_zones.py — data-derived QHM dip-add zones (Claude 2026-10-15). No broker, no network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import tests  # noqa: F401  — test guard (blank broker keys, MTF_TEST_MODE)
from execution import dip_zones as dz


def _frame(rets: dict, days=300, start="2025-08-01"):
    idx = pd.bdate_range(start, periods=days)
    out = {}
    for s, r in rets.items():
        r = np.asarray(r, dtype=float)
        px = 100.0 * np.cumprod(1 + r)
        col = pd.Series(np.nan, index=idx)
        col.iloc[days - len(px):] = px
        out[s] = col
    return pd.DataFrame(out)


class Compute(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        self.calm = rng.normal(0, 0.008, 300)
        self.wild = rng.normal(0, 0.04, 300)

    def test_volatile_names_get_deeper_zones(self):
        z = dz.compute_zones(_frame({"CALM": self.calm, "WILD": self.wild}), 2)
        self.assertLessEqual(z["WILD"]["rung_a"], z["CALM"]["rung_a"])
        self.assertLess(z["CALM"]["rung_a"], 0)

    def test_calm_stock_rung_a_floored_at_pooled_tenth(self):
        f = _frame({"CALM": self.calm, "WILD": self.wild})
        z = dz.compute_zones(f, 2)
        two = (f / f.shift(2) - 1)[f.index > f.index.max() - pd.DateOffset(years=1)]
        own_a = two["CALM"].dropna().quantile(0.10)
        pool_a = two.stack().quantile(0.10)
        self.assertLess(pool_a, own_a)                          # the floor actually binds for the calm name
        self.assertAlmostEqual(z["CALM"]["rung_a"], pool_a, places=9)
        self.assertAlmostEqual(z["WILD"]["rung_a"], min(two["WILD"].dropna().quantile(0.10), pool_a), places=9)

    def test_rung_b_is_the_deepest_of_its_three_inputs(self):
        f = _frame({"CALM": self.calm, "WILD": self.wild})
        z = dz.compute_zones(f, 2)
        win = f[f.index > f.index.max() - pd.DateOffset(years=1)]
        two = (f / f.shift(2) - 1)[f.index > f.index.max() - pd.DateOffset(years=1)]
        pool_b = two.stack().quantile(0.025)
        own_b = two["CALM"].dropna().quantile(0.025)
        move = (f / f.shift(1) - 1).abs()[f.index > f.index.max() - pd.DateOffset(years=1)]["CALM"].median()
        self.assertAlmostEqual(z["CALM"]["rung_b"], min(own_b, pool_b, z["CALM"]["rung_a"] - move), places=9)
        self.assertLessEqual(z["CALM"]["rung_b"], z["CALM"]["rung_a"])
        self.assertTrue(len(win))

    def test_short_history_bounded_by_pooled_floor(self):
        f = _frame({"OLD": self.calm, "NEW": self.calm[-80:], "WILD": self.wild})
        z = dz.compute_zones(f, 2)
        two = (f / f.shift(2) - 1)[f.index > f.index.max() - pd.DateOffset(years=1)]
        floor = two.stack().quantile(0.05)
        self.assertTrue(z["NEW"]["short_history"])
        self.assertFalse(z["OLD"]["short_history"])
        self.assertLessEqual(z["NEW"]["rung_a"], floor + 1e-12)
        self.assertLessEqual(z["NEW"]["rung_b"], floor + 1e-12)

    def test_crash_across_a_missing_bar_is_kept(self):
        r = np.zeros(300)
        r[250] = -0.5
        f = _frame({"X": r, "Y": self.calm})
        gap = f.copy()
        gap.iloc[250, 0] = np.nan
        self.assertAlmostEqual(dz.compute_zones(gap, 2)["X"]["rung_b"], dz.compute_zones(f, 2)["X"]["rung_b"], places=6)

    def test_empty_or_bad_horizon(self):
        self.assertEqual(dz.compute_zones(pd.DataFrame(), 2), {})
        self.assertEqual(dz.compute_zones(_frame({"A": self.calm}), 0), {})


class Reader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "dip_zones.json"
        self.p = mock.patch.object(dz, "_STATE", self.tmp)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_missing_unknown_or_bad_zone_is_none(self):
        self.assertIsNone(dz.zones_for("NVDA"))
        self.tmp.write_text("{bad json")
        self.assertIsNone(dz.zones_for("NVDA"))
        self.tmp.write_text(json.dumps({"zones": {"NVDA": {"rung_a": -0.039, "rung_b": -0.085}}}))
        self.assertIsNone(dz.zones_for("GE"))
        for bad in ({"rung_a": 0.01, "rung_b": -0.05}, {"rung_a": -0.03, "rung_b": -0.01},
                    {"rung_a": "nan", "rung_b": -0.05}, {"rung_a": -0.03}):
            self.tmp.write_text(json.dumps({"zones": {"NVDA": bad}}))
            self.assertIsNone(dz.zones_for("NVDA"), bad)

    def test_valid_zone(self):
        self.tmp.write_text(json.dumps({"zones": {"NVDA": {"rung_a": -0.039, "rung_b": -0.085}}}))
        self.assertEqual(dz.zones_for("nvda"), (-0.039, -0.085))

    def test_production_state_never_touched_in_tests(self):
        with mock.patch.object(dz, "_STATE", dz._DEFAULT_STATE):
            self.assertIsNone(dz.zones_for("NVDA"))
            with self.assertRaises(RuntimeError):
                dz._write({})


class QhmTrigger(unittest.TestCase):
    """The QHM dip-add reads the zones: no zone -> no add; above Rung A -> no add; below it -> the add path runs."""

    def _mgr(self, price):
        from execution import quarterly_hold_manager as qm
        m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
        m.dry_run = False
        m._positions = {}
        m._get_live_price = lambda sym: price  # type: ignore[method-assign]
        m._get_account_equity = mock.Mock(return_value=2700.0)  # type: ignore[method-assign]
        m.broker = mock.Mock()
        m.broker.get_account.side_effect = RuntimeError("no broker in tests")   # the add path stops at the BP read
        pos = qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                              qty_filled=1, avg_entry_price=226.36, tranche1_price=226.36, stop_price=192.41)
        return m, pos

    def test_no_zone_means_no_add(self):
        m, pos = self._mgr(200.0)
        with mock.patch("execution.dip_zones.zones_for", return_value=None):
            m._maybe_dip_add(pos, "2026-10-15")
        m._get_account_equity.assert_not_called()

    def test_above_rung_a_no_add(self):
        m, pos = self._mgr(218.06)                    # -3.7% vs a -3.9% zone
        with mock.patch("execution.dip_zones.zones_for", return_value=(-0.039, -0.085)):
            m._maybe_dip_add(pos, "2026-10-15")
        m._get_account_equity.assert_not_called()

    def test_below_rung_a_reaches_sizing(self):
        m, pos = self._mgr(216.0)                     # -4.6%: inside Rung A, not Rung B
        with mock.patch("execution.dip_zones.zones_for", return_value=(-0.039, -0.085)):
            m._maybe_dip_add(pos, "2026-10-15")
        m._get_account_equity.assert_called()


if __name__ == "__main__":
    unittest.main()
