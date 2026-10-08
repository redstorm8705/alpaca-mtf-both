#!/usr/bin/env python3
# ruff: noqa: E501
"""ETF only when the stock is unaffordable (CEO 2026-10-07; design record tier_safety_and_cohold_plan_2026-10-08.md).
Live case 2026-10-08: NVDA $235.84 Track-A ENTERs wired to 0 shares (global gross room ~$207) all morning while NVDL
traded ~$37. Rules: the stock first; 0-1 affordable shares -> the 2x bull / inverse ETF; a leveraged ETF needs 2+
shares; no usable ETF and exactly 1 stock share fits -> that 1 share."""
import contextlib
import unittest
from types import SimpleNamespace
from unittest import mock

import run_day_tier as rdt
from execution import day_trade_manager as dtm


class MinEntryQty(unittest.TestCase):
    def test_stock_and_etfs(self):
        self.assertEqual(dtm._min_entry_qty("NVDA", 1), 1)
        self.assertEqual(dtm._min_entry_qty("NVDA", 2), 2)
        for etf in ("NVDL", "NVDU", "NVD", "TSLL", "MSFD", "TQQQ", "SQQQ"):
            self.assertEqual(dtm._min_entry_qty(etf, 1), 2, etf)

    def test_bad_min_is_one(self):
        for bad in (None, "x", 0, -3):
            self.assertEqual(dtm._min_entry_qty("NVDA", bad), 1)


class PlaceEntryMinimum(unittest.TestCase):
    """place_entry stops at its minimum share count and records why."""

    def _place(self, symbol, wired, fit=None, min_qty=1, why="rooms=global_gross:$207.47"):
        from execution import broker
        from data.live_price import LivePrice
        sub = {}
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)

        def _submit(sym, qty, side, px, **k):
            sub.update(qty=qty)
            return None
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "RIDE", "entry_ref": 100.0, "target": None,
                   "wall_ref": 97.0}
        patches = [
            mock.patch.object(dtm, "_enabled", return_value=True),
            mock.patch.object(dtm, "_load_state", return_value={}),
            mock.patch.object(dtm, "_save_state", return_value=True),
            mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")),
            mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None),
            mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")),
            mock.patch.object(dtm, "_bounded_entry_qty", return_value=(wired, why)),
            mock.patch.object(dtm, "_budget_fit_qty", return_value=wired if fit is None else fit),
            mock.patch("data.live_price.live_price", return_value=LivePrice(100.0, "iex_trade", 1.0)),
            mock.patch("data.alpaca_data.get_latest_quote", return_value=None),
            mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}),
            mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)),
            mock.patch("strategy.day_tier_logger.log_decision", return_value=True),
            mock.patch.object(broker, "get_account", return_value=acct),
            mock.patch.object(broker, "get_open_positions", return_value=[]),
            mock.patch.object(broker, "get_open_orders", return_value=[]),
            mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30),
            mock.patch("execution.tier_capital_allocator.live_admit",
                       return_value=SimpleNamespace(approved=True, lease=None, reason="")),
            mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-x"),
            mock.patch("execution.tier_capital_allocator.live_release", return_value=None),
            mock.patch.object(broker, "submit_limit_order", side_effect=_submit),
        ]
        with contextlib.ExitStack() as stack:   # > 20 context managers in one `with` is a SyntaxError on py3.10/3.11
            for p in patches:
                stack.enter_context(p)
            dtm.place_entry(symbol, {"would_consider": True}, trigger, {"size_ok": True, "shares": 5},
                            bar_id="20261008-0930", equity=2500.0, min_qty=min_qty)
        return sub.get("qty"), dtm.last_entry_skip(symbol)

    def test_one_etf_share_is_never_submitted(self):
        qty, skip = self._place("NVDL", 1)
        self.assertIsNone(qty)
        self.assertEqual(skip, {"reason": "below_min_qty", "wired": 1, "min": 2})

    def test_two_etf_shares_submit(self):
        qty, skip = self._place("NVDL", 2)
        self.assertEqual(qty, 2)
        self.assertIsNone(skip)

    def test_stock_min_two_records_zero(self):
        qty, skip = self._place("NVDA", 0, min_qty=2)
        self.assertIsNone(qty)
        self.assertEqual(skip["reason"], "below_min_qty")
        self.assertEqual(skip["wired"], 0)

    def test_stock_one_share_submits_at_min_one(self):
        qty, skip = self._place("NVDA", 1)
        self.assertEqual(qty, 1)
        self.assertIsNone(skip)

    def test_fail_closed_zero_is_not_routed(self):
        qty, skip = self._place("NVDA", 0, min_qty=2, why="maintenance data unavailable — fail closed")
        self.assertIsNone(qty)
        self.assertIsNone(skip)

    def test_daily_risk_budget_below_minimum(self):
        qty, skip = self._place("NVDA", 5, fit=1, min_qty=2)
        self.assertIsNone(qty)
        self.assertEqual(skip, {"reason": "risk_budget", "wired": 1, "min": 2})


class FakeDtm:
    """place_entry stand-in: `fits` maps symbol -> shares that fit; entry when fits >= the minimum."""

    def __init__(self, fits):
        self.fits, self.calls, self._skip = fits, [], {}

    def place_entry(self, sym, decision, trigger, size, *, bar_id, equity, min_qty=1, **kw):
        self.calls.append((sym, min_qty))
        self._skip.pop(sym, None)
        need = dtm._min_entry_qty(sym, min_qty)
        n = self.fits.get(sym, 0)
        if n >= need:
            return True
        self._skip[sym] = {"reason": "below_min_qty", "wired": n, "min": need}
        return False

    def last_entry_skip(self, sym):
        return self._skip.get(sym)


class PlaceAffordable(unittest.TestCase):
    TRIG = {"trigger": "ENTER", "direction": "long", "entry_ref": 235.84, "wall_ref": 233.0}

    def _run(self, fits, route=("NVDL",), direction="long"):
        fake = FakeDtm(fits)
        trig = {**self.TRIG, "direction": direction}

        def _bull(sym, d, t, held, eq, bp, track):
            if not route:
                return None, "no route"
            return (d, {**t, "symbol": route[0]}, {"size_ok": True, "shares": 5}, route[0]), "ok"

        def _inv(sym, d, t, held, eq, bp, track):
            if not route:
                return None, "no route"
            return (d, {**t, "symbol": route[0], "direction": "long"}, {"size_ok": True, "shares": 5}, route[0]), "ok"
        with mock.patch.object(rdt, "_bull_pivot", side_effect=_bull), \
                mock.patch.object(rdt, "_inverse_pivot", side_effect=_inv):
            out = rdt._place_affordable(fake, "NVDA", {}, trig, {"size_ok": True, "shares": 1}, bar_id="b",
                                        equity=2500.0, buying_power=1500.0, held=set(), track="A", exposure={})
        return out, fake.calls

    def test_affordable_stock_trades_the_stock(self):
        (ok, sym, _d, tries), calls = self._run({"NVDA": 3, "NVDL": 9})
        self.assertEqual((ok, sym, tries), (True, "NVDA", 1))
        self.assertEqual(calls, [("NVDA", 2)])

    def test_zero_stock_shares_routes_to_etf(self):   # the 2026-10-08 NVDA case
        (ok, sym, d, tries), calls = self._run({"NVDA": 0, "NVDL": 5})
        self.assertEqual((ok, sym, d, tries), (True, "NVDL", "long", 2))

    def test_one_stock_share_prefers_etf(self):
        (ok, sym, _d, _t), _c = self._run({"NVDA": 1, "NVDL": 3})
        self.assertEqual((ok, sym), (True, "NVDL"))

    def test_one_stock_share_fallback_when_etf_fits_one(self):
        (ok, sym, _d, tries), calls = self._run({"NVDA": 1, "NVDL": 1})
        self.assertEqual((ok, sym, tries), (True, "NVDA", 3))
        self.assertEqual(calls[-1], ("NVDA", 1))

    def test_one_stock_share_fallback_when_no_route(self):
        (ok, sym, _d, _t), _c = self._run({"NVDA": 1}, route=())
        self.assertEqual((ok, sym), (True, "NVDA"))

    def test_etf_order_failed_after_submit_no_stock_added(self):
        """risk seat 2026-10-08: an ETF attempt that ended for a non-size reason (fill_unverified, flattened_no_stop)
        may have left a position -> never add the 1-share stock on top."""
        fake = FakeDtm({"NVDA": 1, "NVDL": 5})
        orig = fake.place_entry

        def _pe(sym, *a, **k):
            if sym == "NVDL":
                fake.calls.append((sym, k.get("min_qty", 1)))
                fake._skip.pop(sym, None)
                return False                      # submitted, fill unverified: no size skip record
            return orig(sym, *a, **k)
        fake.place_entry = _pe
        with mock.patch.object(rdt, "_bull_pivot", return_value=(({}, {"direction": "long"}, {}, "NVDL"), "ok")):
            ok, sym, _d, tries = rdt._place_affordable(fake, "NVDA", {}, self.TRIG, {}, bar_id="b", equity=1.0,
                                                       buying_power=1.0, held=set(), track="A", exposure={})
        self.assertFalse(ok)
        self.assertEqual([c[0] for c in fake.calls], ["NVDA", "NVDL"])
        self.assertEqual(tries, 2)

    def _plain(self, fits, **kw):
        fake = FakeDtm(fits)
        with mock.patch.object(rdt, "_bull_pivot",
                               return_value=(({}, {"direction": "long"}, {}, "NVDL"), "ok")) as bp:
            out = rdt._place_affordable(fake, "NVDA", {}, self.TRIG, {}, bar_id="b", equity=1.0, buying_power=1.0,
                                        held=set(), track="A", **kw)
        return out, fake.calls, bp

    def test_day_tier_already_holds_the_etf_no_stack(self):
        """cold-2nd 2026-10-08: tick 2 of the same signal must not add NVDA on an open NVDL (or NVDL again)."""
        (ok, _s, _d, tries), calls, _bp = self._plain({"NVDA": 3, "NVDL": 5}, exposure={"NVDA": 1})
        self.assertEqual((ok, tries, calls), (False, 0, []))

    def test_exposure_unreadable_stock_only(self):
        (ok, sym, _d, _t), calls, bp = self._plain({"NVDA": 0, "NVDL": 5}, exposure=None)
        self.assertFalse(ok)
        self.assertEqual(calls, [("NVDA", 1)])
        bp.assert_not_called()

    def test_max_tries_caps_attempts(self):
        (ok, _s, _d, tries), calls, _bp = self._plain({"NVDA": 1, "NVDL": 1}, exposure={}, max_tries=1)
        self.assertEqual((ok, tries, calls), (False, 1, [("NVDA", 2)]))

    def test_nothing_fits(self):
        (ok, _s, _d, _t), calls = self._run({"NVDA": 0, "NVDL": 1})
        self.assertFalse(ok)
        self.assertEqual([c[0] for c in calls], ["NVDA", "NVDL"])

    def test_short_routes_to_inverse(self):
        (ok, sym, d, _t), _c = self._run({"NVDA": 0, "NVD": 4}, route=("NVD",), direction="short")
        self.assertEqual((ok, sym, d), (True, "NVD", "long"))

    def test_non_size_skip_is_final(self):
        fake = FakeDtm({"NVDA": 0})
        fake.last_entry_skip = lambda s: None          # e.g. co-hold / stop skip: no size record
        with mock.patch.object(rdt, "_bull_pivot") as bp:
            ok, *_ = rdt._place_affordable(fake, "NVDA", {}, self.TRIG, {}, bar_id="b", equity=1.0,
                                           buying_power=1.0, held=set(), track="A", exposure={})
        self.assertFalse(ok)
        bp.assert_not_called()

    def test_risk_budget_one_share_trades_stock_without_etf(self):
        fake = FakeDtm({"NVDA": 0})
        fake.last_entry_skip = lambda s: {"reason": "risk_budget", "wired": 1, "min": 2}
        fake.fits["NVDA"] = 0
        orig = fake.place_entry

        def _pe(sym, *a, min_qty=1, **k):
            if min_qty == 1:
                fake.calls.append((sym, 1))
                return True
            return orig(sym, *a, min_qty=min_qty, **k)
        fake.place_entry = _pe
        with mock.patch.object(rdt, "_bull_pivot") as bp:
            ok, sym, _d, _t = rdt._place_affordable(fake, "NVDA", {}, self.TRIG, {}, bar_id="b", equity=1.0,
                                                    buying_power=1.0, held=set(), track="A", exposure={})
        self.assertEqual((ok, sym), (True, "NVDA"))
        bp.assert_not_called()

    def test_unknown_book_no_route_stock_min_one(self):
        fake = FakeDtm({"NVDA": 1})
        ok, sym, _d, _t = rdt._place_affordable(fake, "NVDA", {}, self.TRIG, {}, bar_id="b", equity=1.0,
                                                buying_power=1.0, held=None, track="A", exposure={})
        self.assertEqual((ok, sym), (True, "NVDA"))
        self.assertEqual(fake.calls, [("NVDA", 1)])


if __name__ == "__main__":
    unittest.main()
