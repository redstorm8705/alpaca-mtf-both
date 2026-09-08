#!/usr/bin/env python3
# ruff: noqa: E501
"""Tests for research/deflated_sharpe.py (PSR/DSR — Bailey & López de Prado).

Pins the units contract (golden PSR = Φ(sr·√(n-1)) at skew=0/kurt=3), the Gaussian denominator
reduction (1 + sr²/2), the board-required hard small-sample floor, the σ_trials-required DSR (no SE
fallback), the exposed SR standard error, and DSR monotonicity in n_trials."""
import math
import unittest

import numpy as np
from scipy import stats

from research import deflated_sharpe as ds


class TestFormulaUnits(unittest.TestCase):
    def test_gaussian_denominator_reduces_to_lo(self):
        # γ3=0, γ4=3 (Pearson normal) -> estimator variance numerator == 1 + sr²/2
        for sr in (0.0, 0.1, 0.5, 1.0):
            self.assertAlmostEqual(ds._sr_estimator_variance(sr, 0.0, 3.0), 1.0 + sr * sr / 2.0, places=12)

    def test_psr_golden_normal_case(self):
        # skew=0, kurt=3, sr*=0 -> PSR = Φ(sr·√(n-1)/√(1+sr²/2))
        sr, n = 0.15, 120
        expected = float(stats.norm.cdf(sr * math.sqrt(n - 1) / math.sqrt(1.0 + sr * sr / 2.0)))
        self.assertAlmostEqual(ds.probabilistic_sharpe_ratio(sr, n, 0.0, 3.0, 0.0), expected, places=12)

    def test_psr_degenerate_returns_nan(self):
        self.assertTrue(math.isnan(ds.probabilistic_sharpe_ratio(0.1, 1, 0.0, 3.0)))       # n<2
        # var<=0: skew=5, sr=1, kurt=3 -> 1 - 5*1 + ((3-1)/4)*1 = -3.5 < 0 -> NaN
        self.assertTrue(math.isnan(ds.probabilistic_sharpe_ratio(1.0, 100, 5.0, 3.0)))

    def test_expected_max_sharpe(self):
        self.assertEqual(ds.expected_max_sharpe(1, 0.5), 0.0)          # no deflation for a single trial
        self.assertEqual(ds.expected_max_sharpe(50, 0.0), 0.0)        # zero dispersion
        e10, e100 = ds.expected_max_sharpe(10, 0.5), ds.expected_max_sharpe(100, 0.5)
        self.assertGreater(e100, e10)                                 # more trials -> higher expected max
        self.assertGreater(e10, 0.0)


class TestSharpeStats(unittest.TestCase):
    def test_raises_on_degenerate(self):
        with self.assertRaises(ValueError):
            ds.sharpe_stats([1.0])                     # <2 finite
        with self.assertRaises(ValueError):
            ds.sharpe_stats([0.5, 0.5, 0.5])           # zero variance

    def test_kurtosis_is_pearson(self):
        rng = np.random.default_rng(1)
        n, sr, sk, ku = ds.sharpe_stats(rng.normal(0, 1, 5000))
        self.assertAlmostEqual(ku, 3.0, delta=0.4)     # Pearson normal ~3 (not ~0 excess)


class TestDeflatedSharpe(unittest.TestCase):
    def _series(self, mu, n, seed=7):
        return np.random.default_rng(seed).normal(mu, 1.0, n)

    def test_hard_floor_below_30_is_nan_and_not_noise(self):
        r = ds.deflated_sharpe(self._series(0.2, 25), n_trials=10, trials_sr_std=0.5)
        self.assertEqual(r.n, 25)
        self.assertTrue(math.isnan(r.psr))
        self.assertTrue(math.isnan(r.dsr))
        self.assertFalse(r.sharpe_not_noise)
        self.assertIn("hard floor", r.honest_flag)

    def test_dsr_requires_trials_sr_std(self):
        r = ds.deflated_sharpe(self._series(0.15, 200), n_trials=20, trials_sr_std=None)
        self.assertFalse(math.isnan(r.psr))            # PSR still computed
        self.assertTrue(math.isnan(r.dsr))             # DSR NaN without dispersion (no SE fallback)
        self.assertFalse(r.sharpe_not_noise)
        self.assertIn("missing/invalid", r.honest_flag)

    def test_invalid_trials_sr_std_gives_nan_dsr(self):
        # NaN (a single-sample np.std result), 0.0, and negative must NOT collapse to an undeflated
        # PSR masquerading as a confident DSR — DSR must be NaN and sharpe_not_noise False.
        for bad in (float("nan"), 0.0, -0.5):
            r = ds.deflated_sharpe(self._series(0.15, 200), n_trials=1000, trials_sr_std=bad)
            self.assertTrue(math.isnan(r.dsr), f"dsr should be NaN for trials_sr_std={bad}")
            self.assertFalse(r.sharpe_not_noise, f"not_noise should be False for trials_sr_std={bad}")
            self.assertIn("missing/invalid", r.honest_flag)

    def test_confidence_out_of_range_raises(self):
        for bad in (0.0, 1.0, -0.5, 1.5, float("nan")):
            with self.assertRaises(ValueError):
                ds.deflated_sharpe(self._series(0.15, 200), n_trials=10, trials_sr_std=0.5, confidence=bad)

    def test_sr_standard_error_exposed(self):
        r = ds.deflated_sharpe(self._series(0.15, 200), n_trials=20, trials_sr_std=0.5)
        self.assertTrue(math.isfinite(r.sr_standard_error) and r.sr_standard_error > 0)

    def test_more_trials_lowers_dsr(self):
        s = self._series(0.15, 300)
        d5 = ds.deflated_sharpe(s, n_trials=5, trials_sr_std=0.5).dsr
        d500 = ds.deflated_sharpe(s, n_trials=500, trials_sr_std=0.5).dsr
        self.assertGreater(d5, d500)                   # more multiple-testing -> lower DSR

    def test_n_trials_one_dsr_equals_psr(self):
        r = ds.deflated_sharpe(self._series(0.15, 300), n_trials=1, trials_sr_std=0.5)
        self.assertEqual(r.sr_star_deflated, 0.0)
        self.assertAlmostEqual(r.dsr, r.psr, places=12)  # no deflation

    def test_moment_fallback_below_50(self):
        r = ds.deflated_sharpe(self._series(0.2, 40), n_trials=10, trials_sr_std=0.5)
        self.assertFalse(r.moments_used)               # 30<=n<50 -> normal-Sharpe fallback
        self.assertIn("skew/kurtosis dropped", r.honest_flag)
        self.assertFalse(math.isnan(r.psr))            # still computed (above hard floor)


if __name__ == "__main__":
    unittest.main()
