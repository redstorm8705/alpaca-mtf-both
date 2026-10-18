# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""execution/overnight_capacity.py — dynamic, tighten-only overnight cap K (Claude 2026-10-14). No broker, no network."""
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import tests  # noqa: F401  — test guard (blank broker keys, MTF_TEST_MODE)
from execution import overnight_capacity as oc
from execution import broker  # noqa: F401,E402  — patched below, never called for real
import data.fetcher  # noqa: F401,E402
import trade_logger  # noqa: F401,E402

CAP = 1.9


def _closes(rets: dict, start="2016-01-04") -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=max(len(r) for r in rets.values()) + 1)
    out = {}
    for s, r in rets.items():
        px = 100.0 * np.cumprod(np.concatenate([[1.0], 1.0 + np.asarray(r, dtype=float)]))
        col = pd.Series(np.nan, index=idx)
        col.iloc[len(idx) - len(px):] = px
        out[s] = col
    return pd.DataFrame(out)


class Reader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "overnight_k.json"
        self.p = mock.patch.object(oc, "_STATE", self.tmp)
        self.p.start()
        self.c = mock.patch.object(oc.config, "SWING_BREAKOUT_TOTAL_OVERNIGHT_K", CAP)
        self.c.start()

    def tearDown(self):
        self.p.stop()
        self.c.stop()

    def _put(self, **d):
        self.tmp.write_text(json.dumps(d))

    def test_missing_state_is_the_ceiling(self):
        self.assertEqual(oc.overnight_k(), CAP)

    def test_garbage_state_is_the_ceiling(self):
        self.tmp.write_text("{not json")
        self.assertEqual(oc.overnight_k(), CAP)
        self._put(k_dyn="abc")
        self.assertEqual(oc.overnight_k(), CAP)
        self._put(k_dyn=None)
        self.assertEqual(oc.overnight_k(), CAP)

    def test_never_above_the_ceiling(self):
        self._put(k_dyn=2.4, session=f"{datetime.now(oc.ET):%Y-%m-%d}")
        self.assertEqual(oc.overnight_k(), CAP)

    def test_tighter_value_is_used_fresh_or_stale(self):
        self._put(k_dyn=1.48, session=f"{datetime.now(oc.ET):%Y-%m-%d}")
        self.assertAlmostEqual(oc.overnight_k(), 1.48)

    def test_f6_strict_reader_requires_current_valid_persisted_state(self):
        with self.assertRaises(ValueError):
            oc.overnight_k_strict_current()
        self._put(k_dyn=1.48, session="2020-01-02")
        with self.assertRaises(ValueError):
            oc.overnight_k_strict_current()
        for value in ("abc", None, float("nan"), float("inf"), 0, -1, 2.0):
            with self.subTest(value=value):
                self._put(k_dyn=value, session=f"{datetime.now(oc.ET):%Y-%m-%d}")
                with self.assertRaises(ValueError):
                    oc.overnight_k_strict_current()
        self._put(k_dyn=1.48, session=f"{datetime.now(oc.ET):%Y-%m-%d}")
        self.assertAlmostEqual(oc.overnight_k_strict_current(), 1.48)
        self._put(k_dyn=1.48, session="2020-01-02")          # stale -> last good, never looser
        self.assertAlmostEqual(oc.overnight_k(), 1.48)

    def test_production_state_never_read_in_tests(self):
        with mock.patch.object(oc, "_STATE", oc._DEFAULT_STATE):
            self.assertIsNone(oc._load())
            with self.assertRaises(RuntimeError):
                oc._write({})


class Compute(unittest.TestCase):
    def test_margin_formula(self):
        rng = np.random.default_rng(7)
        r = rng.normal(0, 0.01, 2500)
        r[1200:1205] = -0.06                                 # one crash week
        res = oc.compute_k_raw({"AAA": 1.5}, _closes({"AAA": r}), 0.327)
        d = res["d_book"]
        self.assertAlmostEqual(res["k_raw"], 1.0 / (0.327 + (1 - 0.327) * d))
        # equity-denominated weights: the book loss is a loss of EQUITY, d_book converts it to a drop in long value
        self.assertAlmostEqual(d, max(res["worst_5s"] * res["record_jump"] * res["vix_scale"],
                                      res["concentration"]) / 1.5)

    def test_record_jump_is_the_median_and_at_least_one(self):
        rng = np.random.default_rng(1)
        r = rng.normal(0, 0.01, 2500)
        res = oc.compute_k_raw({"AAA": 1.0}, _closes({"AAA": r}), 0.3)
        self.assertGreaterEqual(res["record_jump"], 1.0)

    def test_vix_scale_tightens_only(self):
        rng = np.random.default_rng(2)
        cl = _closes({"AAA": rng.normal(0, 0.01, 1500)})
        calm = oc.compute_k_raw({"AAA": 1.0}, cl, 0.3, pd.Series([20.0, 20.0, 10.0]))
        stressed = oc.compute_k_raw({"AAA": 1.0}, cl, 0.3, pd.Series([20.0, 20.0, 40.0]))
        self.assertEqual(calm["vix_scale"], 1.0)
        self.assertEqual(stressed["vix_scale"], 2.0)
        self.assertLess(stressed["k_raw"], calm["k_raw"])

    def test_vix_fallback_uses_last_good_scale(self):
        rng = np.random.default_rng(3)
        cl = _closes({"AAA": rng.normal(0, 0.01, 1500)})
        res = oc.compute_k_raw({"AAA": 1.0}, cl, 0.3, None, vix_scale_fallback=1.7)
        self.assertEqual(res["vix_scale"], 1.7)

    def test_short_side_uses_the_worst_rally(self):
        r = np.zeros(1500)
        r[700] = 0.30                                        # a +30% day hurts a short
        res = oc.compute_k_raw({"SHRT": -0.5}, _closes({"SHRT": r}), 0.3)
        self.assertAlmostEqual(res["concentration"], 0.5 * 0.30, places=6)

    def test_short_history_name_takes_the_books_worst_day(self):
        old = np.zeros(2000)
        old[100] = -0.25
        new = np.zeros(200)
        new[50] = -0.02
        res = oc.compute_k_raw({"OLD": 0.1, "NEW": 0.9}, _closes({"OLD": old, "NEW": new}), 0.3)
        self.assertAlmostEqual(res["concentration"], 0.9 * 0.25, places=6)

    def test_one_bar_name_takes_the_books_worst_day(self):
        old = np.zeros(2000)
        old[100] = -0.25
        cl = _closes({"OLD": old, "NEW": [0.0]})
        res = oc.compute_k_raw({"OLD": 0.1, "NEW": 0.9}, cl, 0.3)
        self.assertAlmostEqual(res["concentration"], 0.9 * 0.25, places=6)

    def test_name_without_history_is_charged_the_books_worst_day(self):
        old = np.zeros(2000)
        old[100] = -0.25
        res = oc.compute_k_raw({"OLD": 0.2, "OPT": 0.8}, _closes({"OLD": old}), 0.3)
        self.assertEqual(res["no_history"], ["OPT"])
        self.assertAlmostEqual(res["concentration"], 0.8 * 0.25, places=6)
        self.assertAlmostEqual(res["book_leverage"], 1.0)

    def test_a_crash_across_a_missing_bar_is_kept(self):
        rng = np.random.default_rng(5)
        a = rng.normal(0, 0.002, 300)
        b = rng.normal(0, 0.002, 300)
        b[150] = -0.5                                        # B halves...
        clean = _closes({"A": a, "B": b})
        gapped = clean.copy()
        gapped.iloc[151, 1] = np.nan                         # ...across a one-day gap in its bars
        r_clean = oc.compute_k_raw({"A": 0.8, "B": 0.8}, clean, 0.327)
        r_gap = oc.compute_k_raw({"A": 0.8, "B": 0.8}, gapped, 0.327)
        self.assertGreater(r_gap["worst_5s"], 0.35)
        self.assertAlmostEqual(r_gap["k_raw"], r_clean["k_raw"], delta=0.05)

    def test_unusable_inputs_return_none(self):
        cl = _closes({"AAA": np.zeros(500)})
        self.assertIsNone(oc.compute_k_raw({"AAA": 1.0}, cl, 0.3))      # no loss ever -> no number
        self.assertIsNone(oc.compute_k_raw({}, cl, 0.3))
        self.assertIsNone(oc.compute_k_raw({"AAA": 1.0}, cl, 0.0))


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "overnight_k.json"
        self.p = mock.patch.object(oc, "_STATE", self.tmp)
        self.p.start()
        self.c = mock.patch.object(oc.config, "SWING_BREAKOUT_TOTAL_OVERNIGHT_K", CAP)
        self.c.start()

    def tearDown(self):
        self.p.stop()
        self.c.stop()

    def _run(self, k_raw, history, last_bar="2026-10-13"):
        self.tmp.write_text(json.dumps({"history": history, "k_dyn": CAP, "session": "x"}))
        acct = mock.Mock(equity="1000", maintenance_margin="300")
        pos = [mock.Mock(symbol="AAA", market_value="1500")]
        with mock.patch("execution.broker.get_account", return_value=acct), \
                mock.patch("execution.broker.get_open_positions", return_value=pos), \
                mock.patch("data.fetcher.fetch_bars_window",
                           return_value=pd.DataFrame({"close": [1.0, 2.0]}, index=pd.bdate_range("2020-01-01", periods=2))), \
                mock.patch.object(oc, "_fetch_vix", return_value=None), \
                mock.patch.object(oc, "compute_k_raw", return_value={"k_raw": k_raw, "vix_scale": 1.0, "last_bar": last_bar}), \
                mock.patch("trade_logger.log_event"):
            return oc.refresh()

    def test_tightens_at_once(self):
        st = self._run(1.5, {"2026-10-01": 2.2, "2026-10-02": 2.2})
        self.assertAlmostEqual(st["k_dyn"], 1.5)
        self.assertAlmostEqual(oc.overnight_k(), 1.5)

    def test_loosens_only_after_five_sessions(self):
        hist = {"2026-10-01": 1.5, "2026-10-02": 2.2, "2026-10-05": 2.2, "2026-10-06": 2.2}
        self.assertAlmostEqual(self._run(2.2, hist)["k_dyn"], 1.5)      # 1.5 is still inside the last 5 sessions
        hist = {"2026-09-01": 1.5, "2026-10-01": 2.2, "2026-10-02": 2.2, "2026-10-05": 2.2, "2026-10-06": 2.2}
        self.assertAlmostEqual(self._run(2.2, hist)["k_dyn"], CAP)        # aged out -> back to the ceiling

    def test_same_session_keeps_the_tightest_reading(self):
        hist = {"2026-10-07": 1.9, "2026-10-08": 1.9, "2026-10-09": 1.9, "2026-10-12": 1.9}
        self.assertAlmostEqual(self._run(1.2, hist, last_bar="2026-10-13")["k_dyn"], 1.2)       # morning
        st = json.loads(self.tmp.read_text())
        self.assertAlmostEqual(self._run(1.9, st["history"], last_bar="2026-10-13")["k_dyn"], 1.2)  # after close
        self.assertAlmostEqual(json.loads(self.tmp.read_text())["history"]["2026-10-13"], 1.2)

    def test_no_history_for_any_name_keeps_the_last_good_state(self):
        self.tmp.write_text(json.dumps({"k_dyn": 1.6, "session": "x"}))
        acct = mock.Mock(equity="1000", maintenance_margin="300")
        pos = [mock.Mock(symbol="AAA", market_value="1500")]
        with mock.patch("execution.broker.get_account", return_value=acct), \
                mock.patch("execution.broker.get_open_positions", return_value=pos), \
                mock.patch("data.fetcher.fetch_bars_window", return_value=pd.DataFrame()), \
                mock.patch.object(oc, "_fetch_vix", return_value=None), mock.patch.object(oc, "_alert"):
            self.assertIsNone(oc.refresh())
        self.assertAlmostEqual(oc.overnight_k(), 1.6)

    def test_flat_book_adds_no_reading(self):
        hist = {"2026-10-07": 1.4, "2026-10-08": 1.9, "2026-10-09": 1.9, "2026-10-12": 1.9, "2026-10-13": 1.9}
        self.tmp.write_text(json.dumps({"history": hist, "k_dyn": 1.4, "session": "x"}))
        acct = mock.Mock(equity="1000", maintenance_margin="0")
        with mock.patch("execution.broker.get_account", return_value=acct), \
                mock.patch("execution.broker.get_open_positions", return_value=[]), \
                mock.patch("trade_logger.log_event"):
            st = oc.refresh()
        self.assertAlmostEqual(st["k_dyn"], 1.4)
        self.assertEqual(st["history"], hist)

    def test_never_raises(self):
        with mock.patch("execution.broker.get_account", side_effect=RuntimeError("down")):
            self.assertIsNone(oc.refresh())


if __name__ == "__main__":
    os.environ.setdefault("MTF_TEST_MODE", "1")
    unittest.main()
