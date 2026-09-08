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


class TestUnrealizedCard(unittest.TestCase):
    """Rafael 2026-09-07: the hourly UNREALIZED card is a fixed Day + 4-tier layout — no per-position
    names, no 'Unrealized by tier' header, no empty-tier 'flat' collapse."""

    def _snap(self, other=0.0):
        return {"equity": 2523.78, "account_today": -12.34, "account_pct": -0.49,
                "tier_unreal": {"intraday": -4.27, "qhm": -8.07, "forever6": 0.0, "daytrade": 0.0},
                "pos_lines": {"intraday": [("AVGO", -4.27)], "qhm": [("GE", -8.07)], "forever6": [], "daytrade": []},
                "total_unreal_today": -12.34, "other_today": other, "untracked_syms": [], "n_positions": 6}

    def test_fixed_layout_no_noise(self):
        body = ps.build_card(self._snap())["blocks"][1]["text"]["text"]
        self.assertNotIn("Unrealized by tier", body)      # redundant header removed
        self.assertNotIn("AVGO", body)                    # no per-position names
        self.assertNotIn("flat", body)                    # no empty-tier collapse
        # "Overall" headline (distinct from the "Day-Trade" tier), then all four tiers, one per line
        for lbl in ("Overall", "Intraday", "QHM", "F6", "Day-Trade"):
            self.assertIn(lbl, body)
        self.assertEqual(body.count("\n"), 4)             # Overall + 4 tiers = 5 lines

    def test_other_only_when_material(self):
        self.assertNotIn("Other", ps.build_card(self._snap(0.10))["blocks"][1]["text"]["text"])
        self.assertIn("Other", ps.build_card(self._snap(-1.50))["blocks"][1]["text"]["text"])


class TestTradingDayGuard(unittest.TestCase):
    """Rafael 2026-09-07: no snapshot on market-closed days — holiday-aware via the Alpaca calendar
    (a cron day-of-week guard cannot catch a weekday holiday like Labor Day)."""

    def _today(self):
        return ps.datetime.now(ps._ET).strftime("%Y-%m-%d")

    def test_calendar_true_false_none(self):
        with patch.object(pl, "_get_json", lambda url: [{"date": self._today()}]):
            self.assertIs(ps._is_trading_day_today(), True)
        with patch.object(pl, "_get_json", lambda url: []):                       # holiday/weekend
            self.assertIs(ps._is_trading_day_today(), False)
        with patch.object(pl, "_get_json", lambda url: (_ for _ in ()).throw(RuntimeError("x"))):
            self.assertIsNone(ps._is_trading_day_today())                          # unreadable → None

    def test_main_skips_closed_day_without_posting(self):
        def _boom(*a, **k):
            raise AssertionError("posted on a closed day")
        with patch.object(ps, "_is_trading_day_today", lambda: False), \
             patch.object(ps, "_post", _boom):
            self.assertEqual(ps.main(), 0)                # returns 0, never reaches _post

    def test_main_posts_on_trading_day_and_on_unknown(self):
        s = TestUnrealizedCard()._snap()                  # non-degenerate (account_today = -12.34)
        for tv in (True, None):                           # None (unknown calendar) fails OPEN → posts (movement present)
            posted = []

            def _rec(payload, label, _sink=posted):       # _sink default binds this iteration's list (no B023)
                _sink.append(1)
                return 0
            with patch.object(ps, "_is_trading_day_today", lambda _tv=tv: _tv), \
                 patch.object(ps, "compute_snapshot", lambda: s), \
                 patch.object(ps, "_post", _rec):
                self.assertEqual(ps.main(), 0)
            self.assertEqual(len(posted), 1)

    def test_main_unknown_calendar_skips_when_fully_flat(self):
        """Fail-open safety: if the calendar is unreadable (None) AND the snapshot is fully flat
        (account_today == 0 and every tier == 0), skip — that is what a closed day looks like, so
        don't post a spurious all-$0.00 card even when the calendar API blips."""
        flat = {"equity": 2523.78, "account_today": 0.0, "account_pct": 0.0,
                "tier_unreal": {"intraday": 0.0, "qhm": 0.0, "forever6": 0.0, "daytrade": 0.0},
                "pos_lines": {t: [] for t in ps._TIERS}, "total_unreal_today": 0.0,
                "other_today": 0.0, "untracked_syms": [], "n_positions": 6}

        def _boom(*a, **k):
            raise AssertionError("posted a flat card on an unknown-calendar day")
        with patch.object(ps, "_is_trading_day_today", lambda: None), \
             patch.object(ps, "compute_snapshot", lambda: flat), \
             patch.object(ps, "_post", _boom):
            self.assertEqual(ps.main(), 0)


if __name__ == "__main__":
    unittest.main()
