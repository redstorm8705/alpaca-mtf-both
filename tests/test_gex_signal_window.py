#!/usr/bin/env python3
# ruff: noqa: E501
"""Regression tests for the GEX Friday-conditional skip-0DTE signal window (data/gex.py).

Guards the fix (board 2026-09-08): refresh_gex's live signal window must SKIP today's 0DTE ONLY on a
weekly (non-monthly-OpEx) Friday, leave Mon-Thu byte-identical to _expiry_range (so kelly's live
Mon-Thu SPY multiplier input is unchanged), and STAND DOWN (this-week collapse) on a monthly OpEx
Friday. Pure date logic — no network."""
import unittest
from datetime import date

from data import gex


class TestMonthlyOpex(unittest.TestCase):
    def test_third_friday_is_opex(self):
        self.assertTrue(gex._is_monthly_opex(date(2026, 9, 18)))    # 3rd Fri Sep
        self.assertTrue(gex._is_monthly_opex(date(2026, 10, 16)))   # 3rd Fri Oct
        self.assertTrue(gex._is_monthly_opex(date(2026, 11, 20)))   # 3rd Fri Nov

    def test_non_third_fridays_not_opex(self):
        self.assertFalse(gex._is_monthly_opex(date(2026, 9, 11)))   # 2nd Fri
        self.assertFalse(gex._is_monthly_opex(date(2026, 9, 25)))   # 4th Fri
        self.assertFalse(gex._is_monthly_opex(date(2026, 9, 4)))    # 1st Fri

    def test_weekday_in_range_not_opex(self):
        self.assertFalse(gex._is_monthly_opex(date(2026, 9, 16)))   # Wed, in 15-21 but not Friday


class TestSignalWindow(unittest.TestCase):
    def test_weekly_friday_skips_0dte(self):
        gte, lte = gex._signal_window_for(date(2026, 9, 11))        # weekly Fri
        self.assertEqual(gte, "2026-09-12")                         # tomorrow (today's 0DTE skipped)
        self.assertEqual(lte, "2026-09-20")                         # +8 days
        self.assertNotEqual(gte, "2026-09-11")                      # must NOT include the 0DTE Friday

    def test_monthly_opex_friday_stands_down(self):
        gte, lte = gex._signal_window_for(date(2026, 9, 18))        # monthly OpEx Fri
        self.assertEqual(gte, "2026-09-18")
        self.assertEqual(lte, "2026-09-18")                         # collapses -> UNKNOWN (conservative)

    def test_weekday_unchanged_vs_expiry_range(self):
        # Mon-Thu must be byte-identical to the this-week window (today, coming Fri)
        self.assertEqual(gex._signal_window_for(date(2026, 9, 8)), ("2026-09-08", "2026-09-11"))   # Tue
        self.assertEqual(gex._signal_window_for(date(2026, 9, 14)), ("2026-09-14", "2026-09-18"))  # Mon
        self.assertEqual(gex._signal_window_for(date(2026, 9, 10)), ("2026-09-10", "2026-09-11"))  # Thu

    def test_returns_two_iso_strings(self):
        for d in (date(2026, 9, 8), date(2026, 9, 11), date(2026, 9, 18)):
            gte, lte = gex._signal_window_for(d)
            self.assertRegex(gte, r"^\d{4}-\d{2}-\d{2}$")
            self.assertRegex(lte, r"^\d{4}-\d{2}-\d{2}$")
            self.assertLessEqual(gte, lte)


if __name__ == "__main__":
    unittest.main()
