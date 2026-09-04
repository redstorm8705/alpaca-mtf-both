#!/usr/bin/env python3
# ruff: noqa: E501  — long docstring / comment lines (project convention)
"""Regression tests for scripts/pnl_snapshot.py realized-mode per-tier attribution.

Guards the money-correctness bug class the tier-tagged FIFO rewrite fixed (cold-2nd 2026-09-04):
a single full-net close of a symbol co-held by two non-protected tiers (intraday+daytrade) must
book each tier's shares to that tier — with no reliance on entry-timestamp uniqueness, no phantom
lots, and Σ tiers == the authoritative total. Read-only: the Alpaca fetchers are monkeypatched.
"""
import unittest
from unittest.mock import patch

from scripts import pnl_snapshot as ps
from reporting import pnl_ledger as pl


def _fill(sym, side, qty, price, ts, oid):
    return {"symbol": sym, "side": side, "qty": str(qty), "price": str(price),
            "transaction_time": ts, "order_id": oid}


class TestRealizedTierAttribution(unittest.TestCase):
    def _run(self, fills, orders, acct=None):
        acct = acct or {"equity": "1000", "last_equity": "920"}
        with patch.object(pl, "fetch_all_fills", lambda: fills), \
             patch.object(pl, "fetch_all_orders", lambda: orders), \
             patch.object(pl, "fetch_account", lambda: acct):
            return ps.compute_realized_snapshot()

    def _ts(self, hh):
        return f"{ps._today_pt()}T{hh}:00:00-07:00"   # PT today, so exit_date == today

    def test_cross_tier_same_timestamp(self):
        """intraday 50 + daytrade 30 co-hold XYZ, one 80-share sell tagged intraday, SAME buy ts.
        Each tier's shares must book to that tier; plus a clean intraday ABC round-trip."""
        fills = [
            _fill("XYZ", "buy", 50, 10, self._ts("09"), "o1"),
            _fill("XYZ", "buy", 30, 10, self._ts("09"), "o2"),   # same ts as o1 (collision case)
            _fill("XYZ", "sell", 80, 11, self._ts("12"), "o3"),
            _fill("ABC", "buy", 40, 12, self._ts("10"), "o4"),
            _fill("ABC", "sell", 40, 13, self._ts("13"), "o5"),
        ]
        orders = [
            {"id": "o1", "client_order_id": "IN-XYZ-b-1-0"},
            {"id": "o2", "client_order_id": "DT-XYZ-b-2-0"},
            {"id": "o3", "client_order_id": "IN-XYZ-s-3-0"},
            {"id": "o4", "client_order_id": "IN-ABC-b-4-0"},
            {"id": "o5", "client_order_id": "IN-ABC-s-5-0"},
        ]
        s = self._run(fills, orders)
        self.assertEqual(s["tier_realized"]["intraday"], 90.0)   # XYZ 50 + ABC 40
        self.assertEqual(s["tier_realized"]["daytrade"], 30.0)   # XYZ 30
        self.assertEqual(s["tier_realized"]["qhm"], 0.0)
        self.assertEqual(s["total_realized"], 120.0)
        self.assertEqual(s["unattributed"], 0.0)

    def test_short_round_trip(self):
        """daytrade short 10 @100 -> cover @95 = +50, booked to daytrade."""
        fills = [
            _fill("QQQ", "sell_short", 10, 100, self._ts("09"), "a1"),
            _fill("QQQ", "buy_to_cover", 10, 95, self._ts("12"), "a2"),
        ]
        orders = [
            {"id": "a1", "client_order_id": "DT-QQQ-s-1-0"},
            {"id": "a2", "client_order_id": "DT-QQQ-b-2-0"},
        ]
        s = self._run(fills, orders)
        self.assertEqual(s["tier_realized"]["daytrade"], 50.0)
        self.assertEqual(s["total_realized"], 50.0)
        self.assertEqual(s["unattributed"], 0.0)

    def test_untagged_fill_defaults_intraday(self):
        """A fill whose order has no tier-tagged client_order_id books to intraday."""
        fills = [
            _fill("SPY", "buy", 5, 500, self._ts("09"), "b1"),
            _fill("SPY", "sell", 5, 510, self._ts("12"), "b2"),
        ]
        orders = [
            {"id": "b1", "client_order_id": "legacy-untagged-1"},
            {"id": "b2", "client_order_id": None},
        ]
        s = self._run(fills, orders)
        self.assertEqual(s["tier_realized"]["intraday"], 50.0)
        self.assertEqual(s["total_realized"], 50.0)
        self.assertEqual(s["unattributed"], 0.0)

    def test_sum_equals_total_invariant(self):
        """Σ per-tier + unattributed must equal the authoritative total."""
        fills = [
            _fill("XYZ", "buy", 50, 10, self._ts("09"), "o1"),
            _fill("XYZ", "buy", 30, 10, self._ts("09"), "o2"),
            _fill("XYZ", "sell", 80, 11, self._ts("12"), "o3"),
        ]
        orders = [
            {"id": "o1", "client_order_id": "IN-XYZ-b-1-0"},
            {"id": "o2", "client_order_id": "DT-XYZ-b-2-0"},
            {"id": "o3", "client_order_id": "IN-XYZ-s-3-0"},
        ]
        s = self._run(fills, orders)
        self.assertAlmostEqual(sum(s["tier_realized"].values()) + s["unattributed"],
                               s["total_realized"], places=2)


if __name__ == "__main__":
    unittest.main()
