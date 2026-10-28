# ruff: noqa: E501
"""DAY-UNIVERSE-1027: both Day tracks and the options refresh read ONE stock list; added names keep their routes.

No broker or network call: config constants, the Track B universe reader, the GEX universe reader and the static ETF
maps only.
"""
import unittest
from unittest import mock

import config
from data import gex
from strategy import day_tier_leverage as lev
from strategy import day_tier_track_b as tb

EXPECTED = ("NVDA", "TSLA", "META", "AMD", "AMZN", "AAPL", "MSFT", "GOOGL", "AVGO", "NFLX", "MU", "COIN", "PLTR",
            "SMCI", "UBER", "SNDK", "DRAM", "EWY")


class SharedUniverse(unittest.TestCase):
    def test_one_canonical_tuple(self):
        self.assertEqual(config.DAY_TIER_UNIVERSE, EXPECTED)
        self.assertIsInstance(config.DAY_TIER_UNIVERSE, tuple)
        self.assertIs(config.DAYTRADE_UNIVERSE, config.DAY_TIER_UNIVERSE)
        self.assertIs(config.DAYTRADE_TRACK_B_UNIVERSE, config.DAY_TIER_UNIVERSE)
        self.assertEqual(len(set(EXPECTED)), len(EXPECTED))

    def test_track_b_reads_the_shared_list_in_order(self):
        self.assertEqual(tb.track_b_universe(), list(EXPECTED))

    def test_options_refresh_reads_the_shared_list(self):
        self.assertEqual(gex._day_tier_underlyings(), list(EXPECTED))
        self.assertEqual(gex._DAYTRADE_UNDERLYINGS, list(EXPECTED))

    def test_options_refresh_follows_a_list_change(self):
        with mock.patch.object(config, "DAY_TIER_UNIVERSE", ("NVDA", "coin")):
            self.assertEqual(gex._day_tier_underlyings(), ["NVDA", "COIN"])

    def test_options_refresh_fails_safe_when_list_missing(self):
        with mock.patch.object(config, "DAY_TIER_UNIVERSE", None):
            self.assertEqual(gex._day_tier_underlyings(), [])


class AddedNameRoutes(unittest.TestCase):
    """The routes the added names resolve to (strategy/day_tier_leverage.py maps); no other tier holds them here."""

    def test_bull_routes(self):
        want = {"AMD": "AMDL", "AVGO": "AVL", "NFLX": "NFXL", "COIN": "CONL", "PLTR": "PLTU", "SMCI": "SMCL",
                "UBER": "UBRL"}
        for sym, etf in want.items():
            self.assertEqual(lev.etf_for_order(sym, set()), etf, sym)
        for sym in ("DRAM", "EWY"):
            self.assertIsNone(lev.etf_for_order(sym, set()), sym)

    def test_inverse_routes(self):
        want = {"AMD": "DAMD", "AVGO": "AVS", "NFLX": "NFXS", "COIN": "CONI", "PLTR": "PLTD"}
        for sym, etf in want.items():
            self.assertEqual(lev.inverse_etf_for_order(sym, set())[0], etf, sym)
        for sym in ("SMCI", "UBER", "DRAM", "EWY"):   # no inverse fund: the short trades the stock
            self.assertIsNone(lev.inverse_etf_for_order(sym, set()), sym)

    def test_added_names_use_thin_name_rules(self):
        deep = set(config.DAYTRADE_DEEP_LIQUIDITY_SYMBOLS)
        for sym in ("AMD", "AVGO", "NFLX", "COIN", "PLTR", "SMCI", "UBER", "DRAM", "EWY"):
            self.assertNotIn(sym, deep, sym)


if __name__ == "__main__":
    unittest.main()
