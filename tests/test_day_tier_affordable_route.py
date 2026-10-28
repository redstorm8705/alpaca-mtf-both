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

import config
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


class FakeDtmB(FakeDtm):
    """FakeDtm that also records each attempt's size dict and extra kwargs (decision_id); `skips` overrides the skip
    record a fund attempt leaves (e.g. the B.3 volume skip)."""

    def __init__(self, fits, skips=None):
        super().__init__(fits)
        self.sizes, self.kws, self.skips = [], [], dict(skips or {})

    def place_entry(self, sym, decision, trigger, size, *, bar_id, equity, min_qty=1, **kw):
        self.sizes.append(size)
        self.kws.append(kw)
        if sym in self.skips:
            self.calls.append((sym, min_qty))
            self._skip[sym] = self.skips[sym]
            return False
        return super().place_entry(sym, decision, trigger, size, bar_id=bar_id, equity=equity, min_qty=min_qty, **kw)


class PlaceTrackB(unittest.TestCase):
    """TRACKB-SIZE-ETF-1028 B.1: Track B's 2-share rule on the WIRED size (run_day_tier._place_track_b)."""
    TRIG = {"trigger": "ENTER", "direction": "long", "entry_ref": 235.84, "wall_ref": 233.0}

    def _run(self, fits, route=("NVDL",), direction="long", order_sym="NVDA", route_fund="NVDL", ten=False,
             pe_kw=None, max_tries=3, tick_t0=None, skips=None, budget_shares=None):
        fake = FakeDtmB(fits, skips)
        trig = {**self.TRIG, "direction": direction}
        fund = {"size_ok": True, "shares": 5, "budget": 10.0, "track": "B"}

        def _bull(sym, d, t, held, eq, bp, prior_close):
            if not route:
                return None, "no route"
            return (d, {**t, "symbol": route[0], "entry_ref": 40.0}, dict(fund), route[0]), "bull route -> " + route[0]

        def _inv(sym, d, t, held, eq, bp, track, prior_close=None):
            if not route:
                return None, "no route"
            return ((d, {**t, "symbol": route[0], "direction": "long", "entry_ref": 40.0}, dict(fund), route[0]),
                    "inverse route -> " + route[0])
        with mock.patch.object(rdt, "_track_b_bull_route", side_effect=_bull) as bp, \
                mock.patch.object(rdt, "_inverse_pivot", side_effect=_inv) as ip:
            out = rdt._place_track_b(fake, "NVDA", order_sym, {}, trig, {"size_ok": True, "shares": 1, "track": "B"},
                                     bar_id="b", equity=2500.0, buying_power=1500.0, held=set(), route_fund=route_fund,
                                     ten=ten, prior_close=230.0, pe_kw=pe_kw, max_tries=max_tries,
                                     tick_t0=rdt.time.monotonic() if tick_t0 is None else tick_t0,
                                     budget_shares=budget_shares)
        self.bull, self.inv = bp, ip
        return out, fake

    def test_stock_fits_two_plus_trades_the_stock(self):
        (ok, sym, _d, tries), fake = self._run({"NVDA": 3})
        self.assertEqual((ok, sym, tries), (True, "NVDA", 1))
        self.assertEqual(fake.calls, [("NVDA", 2)])
        self.bull.assert_not_called()

    def test_stock_fits_one_and_fund_ok_trades_the_fund(self):
        (ok, sym, d, tries), fake = self._run({"NVDA": 1, "NVDL": 5})
        self.assertEqual((ok, sym, d, tries), (True, "NVDL", "long", 2))
        self.assertEqual(fake.calls, [("NVDA", 2), ("NVDL", 1)])   # the fund's own minimum is 2 (_min_entry_qty)

    def test_stock_fits_one_and_no_fund_trades_one_stock_share(self):
        (ok, sym, _d, tries), fake = self._run({"NVDA": 1}, route=())
        self.assertEqual((ok, sym, tries), (True, "NVDA", 2))
        self.assertEqual(fake.calls, [("NVDA", 2), ("NVDA", 1)])

    def test_stock_fits_one_and_fund_fits_one_falls_back_to_one_stock_share(self):
        (ok, sym, _d, tries), fake = self._run({"NVDA": 1, "NVDL": 1})
        self.assertEqual((ok, sym, tries), (True, "NVDA", 3))
        self.assertEqual(fake.calls[-1], ("NVDA", 1))

    def test_stock_fits_zero_and_fund_ok_trades_the_fund(self):
        (ok, sym, _d, _t), _fake = self._run({"NVDA": 0, "NVDL": 4})
        self.assertEqual((ok, sym), (True, "NVDL"))

    def test_stock_fits_zero_and_no_fund_is_no_trade(self):
        (ok, _s, _d, _t), fake = self._run({"NVDA": 0}, route=())
        self.assertFalse(ok)
        self.assertEqual(fake.calls, [("NVDA", 2)])

    def test_short_routes_to_the_inverse_fund(self):
        (ok, sym, d, _t), _fake = self._run({"NVDA": 1, "NVD": 4}, route=("NVD",), direction="short",
                                            route_fund="NVD")
        self.assertEqual((ok, sym, d), (True, "NVD", "long"))
        self.inv.assert_called_once()
        self.assertEqual(self.inv.call_args.args[6], "B")

    def test_coheld_pre_route_places_the_fund_only(self):
        (ok, sym, _d, tries), fake = self._run({"NVDL": 4}, order_sym="NVDL", route_fund=None)
        self.assertEqual((ok, sym, tries), (True, "NVDL", 1))
        self.assertEqual(fake.calls, [("NVDL", 1)])
        self.bull.assert_not_called()

    def test_no_fund_route_places_the_stock_at_one_share_minimum(self):
        (ok, sym, _d, _t), fake = self._run({"NVDA": 1}, route_fund=None)
        self.assertEqual((ok, sym), (True, "NVDA"))
        self.assertEqual(fake.calls, [("NVDA", 1)])

    def test_ten_of_ten_keeps_max_size_on_the_fund(self):
        (_ok, sym, _d, _t), fake = self._run({"NVDA": 1, "NVDL": 5}, ten=True)
        self.assertEqual(sym, "NVDL")
        self.assertTrue(fake.sizes[-1].get("max_size"))
        (_ok, _s, _d, _t), fake2 = self._run({"NVDA": 1, "NVDL": 5}, ten=False)
        self.assertFalse(fake2.sizes[-1].get("max_size"))

    def test_counter_trend_decision_id_goes_to_every_attempt(self):
        (_ok, _s, _d, _t), fake = self._run({"NVDA": 1, "NVDL": 1}, pe_kw={"decision_id": "CT-NVDA-1"})
        self.assertEqual(len(fake.kws), 3)
        self.assertTrue(all(k.get("decision_id") == "CT-NVDA-1" for k in fake.kws))

    def test_fund_volume_skip_falls_back_to_one_stock_share(self):
        # B.3: an unreadable fund volume skips the fund (not below_min_qty) -> the 1-share stock fallback still applies
        skip = {"reason": "fund_volume_unreadable", "wired": 5, "min": 2}
        (ok, sym, _d, tries), fake = self._run({"NVDA": 1}, skips={"NVDL": skip})
        self.assertEqual((ok, sym, tries), (True, "NVDA", 3))

    def test_fund_attempt_ending_without_a_size_skip_never_adds_the_stock(self):
        fake = FakeDtmB({"NVDA": 1})
        orig = fake.place_entry

        def _pe(sym, *a, **k):
            if sym == "NVDL":
                fake.calls.append((sym, 1))
                fake._skip.pop(sym, None)
                return False                  # e.g. submitted, fill unverified: no size skip record
            return orig(sym, *a, **k)
        fake.place_entry = _pe
        with mock.patch.object(rdt, "_track_b_bull_route",
                               return_value=(({}, {"direction": "long"}, {"size_ok": True}, "NVDL"), "ok")):
            ok, _s, _d, tries = rdt._place_track_b(fake, "NVDA", "NVDA", {}, self.TRIG, {"size_ok": True},
                                                   bar_id="b", equity=1.0, buying_power=1.0, held=set(),
                                                   route_fund="NVDL", ten=False, prior_close=None, pe_kw=None,
                                                   max_tries=3, tick_t0=rdt.time.monotonic())
        self.assertFalse(ok)
        self.assertEqual([c[0] for c in fake.calls], ["NVDA", "NVDL"])
        self.assertEqual(tries, 2)

    def test_fund_attempt_budget_floored_at_one_fund_share(self):
        # Under DAYTRADE_TRACK_B_CASH_ONLY the fund's budget is floored at one share at its worst marketable limit
        # (entry_ref 40 x the loop's multiplier), exactly as the stock's is.
        fake = FakeDtmB({"NVDA": 1, "NVDL": 5})
        with mock.patch.object(rdt, "_track_b_bull_route",
                               return_value=(({}, {"symbol": "NVDL", "direction": "long", "entry_ref": 40.0},
                                              {"size_ok": True, "budget": 10.0}, "NVDL"), "ok")):
            rdt._place_track_b(fake, "NVDA", "NVDA", {}, self.TRIG, {"size_ok": True}, bar_id="b", equity=1.0,
                               buying_power=1.0, held=set(), route_fund="NVDL", ten=False, prior_close=None,
                               pe_kw=None, max_tries=3, tick_t0=rdt.time.monotonic(), worst_mult=1.05)
        self.assertAlmostEqual(fake.sizes[-1]["budget"], 84.0, places=6)   # the fund's 2-share minimum (Codex preship)
        self.assertEqual(rdt._track_b_one_share_floor({"entry_ref": 40.0}, {"size_ok": True, "budget": 10.0}, None),
                         {"size_ok": True, "budget": 10.0})

    def test_max_tries_one_makes_one_attempt_at_todays_minimum(self):
        # cold review FAIL-1: with budget for one attempt, the stock goes at the 1-share minimum (today's trade kept)
        (ok, _s, _d, tries), fake = self._run({"NVDA": 1, "NVDL": 5}, max_tries=1)
        self.assertEqual((ok, tries, fake.calls), (True, 1, [("NVDA", 1)]))

    def test_without_two_time_reserves_one_attempt_at_todays_minimum(self):
        old = rdt.time.monotonic() - rdt._TICK_CADENCE_S   # the tick's time is already used up
        (ok, _s, _d, tries), fake = self._run({"NVDA": 1, "NVDL": 5}, tick_t0=old)
        self.assertEqual((ok, tries, fake.calls), (True, 1, [("NVDA", 1)]))

    def test_tight_tick_with_budget_buying_one_or_none_routes_the_fund_first(self):
        # adversarial D1: SNDK-like stock, one attempt left -> today's budget route: the fund in one attempt
        (ok, sym, _d, tries), fake = self._run({"NVDA": 0, "NVDL": 4}, max_tries=1, budget_shares=0)
        self.assertEqual((ok, sym, tries), (True, "NVDL", 1))
        self.assertEqual(fake.calls, [("NVDL", 1)])
        self.assertIs(fake.kws[-1].get("pre_order_sentinel"), True)

    def test_tight_tick_no_fund_route_places_the_stock_at_one_share(self):
        (ok, sym, _d, tries), fake = self._run({"NVDA": 1}, route=(), max_tries=1, budget_shares=0)
        self.assertEqual((ok, sym, tries), (True, "NVDA", 1))
        self.assertEqual(fake.calls, [("NVDA", 1)])

    def test_tight_tick_with_budget_buying_two_places_the_stock_at_one_share(self):
        (ok, sym, _d, _t), fake = self._run({"NVDA": 1, "NVDL": 4}, max_tries=1, budget_shares=3)
        self.assertEqual((ok, sym), (True, "NVDA"))
        self.assertEqual(fake.calls, [("NVDA", 1)])

    def test_fund_pre_order_skip_still_allows_the_one_share_stock(self):
        # adversarial D2: the fund attempt stops before any order (pre_order record) -> 1-share stock fallback
        (ok, sym, _d, _t), fake = self._run({"NVDA": 1}, skips={"NVDL": {"reason": "pre_order", "wired": 0, "min": 0}})
        self.assertEqual((ok, sym), (True, "NVDA"))
        self.assertEqual(fake.calls, [("NVDA", 2), ("NVDL", 1), ("NVDA", 1)])

    def test_max_tries_two_keeps_the_one_share_fallback(self):
        # cold review FAIL-1 residual: 2 attempts left, wired 1 -> no fund attempt (it would use the last attempt);
        # the 1-share stock trade is kept
        (ok, sym, _d, tries), fake = self._run({"NVDA": 1, "NVDL": 1}, max_tries=2)
        self.assertEqual((ok, sym, tries), (True, "NVDA", 2))
        self.assertEqual(fake.calls, [("NVDA", 2), ("NVDA", 1)])

    def test_one_share_fallback_is_not_lost_to_the_clock(self):
        # two reserves at the start, but the clock runs out after the stock attempt: the 1-share fallback still runs
        t = [rdt.time.monotonic()]
        with mock.patch.object(rdt.time, "monotonic", side_effect=lambda: t.__setitem__(0, t[0] + rdt._TICK_CADENCE_S / 2) or t[0]):
            (ok, sym, _d, _tries), fake = self._run({"NVDA": 1}, route=(), tick_t0=t[0])
        self.assertEqual((ok, sym), (True, "NVDA"))
        self.assertEqual(fake.calls[-1], ("NVDA", 1))


class FundRouteStopFloor(unittest.TestCase):
    """TRACKB-SIZE-ETF-1028 B.2(a): the pivots floor the fund's stop reference before sizing and the liquidity gate."""
    QUOTE = {"bid": 4.565, "ask": 4.575}

    def test_pltd_wall_is_floored_to_five_ticks(self):
        t2, why = rdt._floor_fund_wall("PLTD", {"entry_ref": 4.57, "wall_ref": round(4.57 - 0.0489, 4)}, self.QUOTE)
        self.assertAlmostEqual(t2["wall_ref"], 4.52, places=6)
        self.assertIn("floored", why)

    def test_unreadable_quote_is_no_route(self):
        t2, _why = rdt._floor_fund_wall("PLTD", {"entry_ref": 4.57, "wall_ref": 4.52}, None)
        self.assertIsNone(t2)

    def test_watch_day_exempt_short_with_a_widening_floor_is_no_route(self):
        t2, why = rdt._floor_fund_wall("PLTD", {"entry_ref": 4.57, "wall_ref": 4.5211, "watch_day_exempt_b": True},
                                       self.QUOTE)
        self.assertIsNone(t2)
        self.assertIn("watch-day-exempt", why)
        # ...but an exempt short whose fund stop already has the room keeps the route
        t3, _ = rdt._floor_fund_wall("PLTD", {"entry_ref": 4.57, "wall_ref": 4.50, "watch_day_exempt_b": True},
                                     self.QUOTE)
        self.assertEqual(t3["wall_ref"], 4.50)

    def _inverse(self, trigger, quote, track="B"):
        from data.live_price import LivePrice
        lp = {"PLTR": LivePrice(190.0, "iex_trade", 1.0), "PLTD": LivePrice(4.57, "iex_trade", 1.0)}
        size = mock.Mock(return_value={"size_ok": True, "shares": 10})
        with mock.patch("data.live_price.live_price", side_effect=lambda s, **k: lp.get(s)), \
                mock.patch.object(rdt, "_prior_close", return_value=None), \
                mock.patch("strategy.day_tier_leverage.etf_tracking_ok", return_value=(True, "tracking ok")), \
                mock.patch("data.alpaca_data.get_latest_quote", return_value=quote) as gq, \
                mock.patch.object(rdt, "_today_iex_volume", return_value=4_000_000.0), \
                mock.patch("strategy.day_tier_sizing.compute_day_tier_size", size):
            out = rdt._inverse_pivot("PLTR", {"symbol": "PLTR"}, trigger, set(), 2500.0, 1500.0, track,
                                     prior_close=200.0)
        return out, gq, size

    def test_pltr_10_28_inverse_route_now_passes_with_the_floored_stop(self):
        # 2026-10-28: PLTR short, structural stop +0.0489 on PLTD -> "stop 0.0489 < 5 ticks" refused the route.
        trig = {"symbol": "PLTR", "trigger": "ENTER", "direction": "short", "entry_ref": 190.0,
                "wall_ref": 190.0 + 0.0489 * 200.0 / 4.57}
        (route, why), gq, size = self._inverse(trig, self.QUOTE)
        self.assertIsNotNone(route, why)
        _d2, t2, _s, etf = route
        self.assertEqual(etf, "PLTD")
        self.assertAlmostEqual(t2["wall_ref"], 4.52, places=6)
        self.assertEqual(t2["fund_today_volume"], 4_000_000.0)
        gq.assert_called_once()                       # one quote read for the floor and the liquidity gate
        size.assert_called_once()

    def test_watch_day_exempt_short_with_a_widening_floor_keeps_the_stock_path(self):
        trig = {"symbol": "PLTR", "trigger": "ENTER", "direction": "short", "entry_ref": 190.0,
                "wall_ref": 190.0 + 0.0489 * 200.0 / 4.57, "watch_day_exempt_b": True}
        (route, why), _gq, size = self._inverse(trig, self.QUOTE)
        self.assertIsNone(route)
        self.assertIn("watch-day-exempt", why)
        size.assert_not_called()                      # refused before sizing

    def test_track_b_bull_route_floors_the_fund_stop_before_sizing(self):
        from data.live_price import LivePrice
        lp = {"PLTR": LivePrice(190.0, "iex_trade", 1.0), "PLTU": LivePrice(40.0, "iex_trade", 1.0)}
        trig = {"symbol": "PLTR", "trigger": "ENTER", "direction": "long", "entry_ref": 190.0, "wall_ref": 189.9}
        size = mock.Mock(return_value={"size_ok": True, "shares": 10})
        with mock.patch("data.live_price.live_price", side_effect=lambda s, **k: lp.get(s)), \
                mock.patch("data.alpaca_data.get_latest_quote", return_value={"bid": 39.99, "ask": 40.01}), \
                mock.patch.object(rdt, "_today_iex_volume", return_value=1_000_000.0), \
                mock.patch("strategy.day_tier_sizing.compute_day_tier_size", size):
            route, _why = rdt._track_b_bull_route("PLTR", {"symbol": "PLTR"}, trig, set(), 2500.0, 1500.0, None)
        self.assertIsNotNone(route)
        _d2, t2, _s, etf = route
        self.assertEqual(etf, "PLTU")
        self.assertLessEqual(t2["wall_ref"], 40.0 - 0.08 + 1e-9)   # spread 0.02 / 0.25 = 0.08 > 5 ticks
        self.assertEqual(size.call_args[0][2], 40.0)

    def test_track_a_fund_route_is_not_floored(self):
        # Track A fund routes are unchanged by TRACKB-SIZE-ETF-1028: no floor, so the sub-5-tick stop is still refused
        trig = {"symbol": "PLTR", "trigger": "ENTER", "direction": "short", "entry_ref": 190.0,
                "wall_ref": 190.0 + 0.0489 * 200.0 / 4.57}
        (route, why), _gq, _size = self._inverse(trig, self.QUOTE, track="A")
        self.assertIsNone(route)
        self.assertIn("ticks", why)

    def test_unreadable_quote_is_still_no_route(self):
        trig = {"symbol": "PLTR", "trigger": "ENTER", "direction": "short", "entry_ref": 190.0, "wall_ref": 192.0}
        (route, _why), _gq, _size = self._inverse(trig, None)
        self.assertIsNone(route)


class FundPlaceEntry(unittest.TestCase):
    """TRACKB-SIZE-ETF-1028 B.2(b) + B.3 inside place_entry for a fund symbol (every broker call mocked)."""

    def _place(self, symbol="PLTD", track="B", stop=4.55, wired=40, volume=4_000_000.0, quote=(4.565, 4.575),
               capture=None, sentinel=False):
        from execution import broker
        from data.live_price import LivePrice
        sub: dict = {}
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)
        seen: dict = {}

        def _submit(sym, qty, side, px, **k):
            sub.update(qty=qty, px=px)
            return None

        def _bounded(qty, limit_px, stop_px, *a, **k):
            seen["stop"] = stop_px
            return wired, "rooms=test"
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "RIDE", "entry_ref": 4.57, "target": None,
                   "wall_ref": stop}
        if volume is not None:
            trigger["fund_today_volume"] = volume
        q = {"bid": quote[0], "ask": quote[1]} if quote else None
        patches = [
            mock.patch.object(dtm, "_enabled", return_value=True),
            mock.patch.object(dtm, "_load_state", return_value={}),
            mock.patch.object(dtm, "_save_state", return_value=True),
            mock.patch.object(dtm, "_compute_stop_price", return_value=stop),
            mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")),
            mock.patch.object(dtm, "_track_a_noise_geometry", return_value=None),
            mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None),
            mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")),
            mock.patch.object(dtm, "_bounded_entry_qty", side_effect=_bounded),
            mock.patch.object(config, "DAYTRADE_CONV_SIZING_ENABLED", False, create=True),
            mock.patch("data.live_price.live_price", return_value=LivePrice(4.57, "iex_trade", 1.0)),
            mock.patch("data.alpaca_data.get_latest_quote", return_value=q),
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
            mock.patch.object(broker, "submit_bracket_entry", return_value=None),
        ] + list(capture or [])
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            dtm.place_entry(symbol, {"would_consider": True}, trigger,
                            {"size_ok": True, "shares": 5, "track": track, "budget": 140.0},
                            bar_id="20261028-1040", equity=2500.0, pre_order_sentinel=sentinel)
        return sub, seen, dtm.last_entry_skip(symbol)

    def test_floor_is_applied_before_the_share_count(self):
        # limit 4.58 (ask 4.575 + slippage); structural stop 4.55 is 3 ticks away -> floored to 4.53 BEFORE sizing
        sub, seen, _skip = self._place(stop=4.55)
        self.assertEqual(sub.get("px"), 4.58)
        self.assertAlmostEqual(seen["stop"], 4.53, places=6)
        self.assertEqual(sub.get("qty"), 40)

    def test_wide_enough_stop_is_unchanged(self):
        _sub, seen, _skip = self._place(stop=4.40)
        self.assertAlmostEqual(seen["stop"], 4.40, places=6)

    def test_unreadable_quote_uses_the_tick_floor(self):
        # cold review FAIL-2: no quote -> the 5-tick floor alone, never the 3-tick stop
        sub, seen, _skip = self._place(stop=4.55, quote=None)
        self.assertLessEqual(seen["stop"], round(sub["px"] - 0.05, 2) + 1e-9)

    def test_stock_symbol_is_not_floored(self):
        _sub, seen, _skip = self._place(symbol="PLTR", stop=4.55, volume=None)
        self.assertAlmostEqual(seen["stop"], 4.55, places=6)

    def test_floor_also_bounds_the_conviction_sizing_min_room(self):
        captured: dict = {}

        def _wired(**kw):
            captured.update(kw)
            return {"ok": False, "skip": None}
        ci = {"stage": "ok", "plan": {"G": 0.0, "proven": True}, "j": 0.0, "used": (0.0, 0.0, ""), "atr": 0.02,
              "spread_med": 0.01, "rt_room": None}
        extra = [mock.patch.object(config, "DAYTRADE_CONV_SIZING_ENABLED", True, create=True),
                 mock.patch.object(dtm, "_conv_inputs", return_value=ci),
                 mock.patch.object(dtm, "_wired_entry", side_effect=_wired)]
        self._place(stop=4.55, capture=extra)
        self.assertAlmostEqual(captured["stop_px"], 4.53, places=6)
        self.assertAlmostEqual(captured["conv_in"]["noise_room"], 0.05, places=6)

    def test_volume_cap_shrinks_the_wired_shares(self):
        # today's IEX volume 3,000 / 100 -> at most 30 shares (wired 40)
        sub, _seen, _skip = self._place(wired=40, volume=3000.0)
        self.assertEqual(sub.get("qty"), 30)

    def test_volume_cap_never_grows(self):
        sub, _seen, _skip = self._place(wired=40, volume=4_000_000.0)
        self.assertEqual(sub.get("qty"), 40)

    def test_unreadable_volume_skips_the_fund_fail_closed(self):
        for bad in (None, 0.0, -5.0, float("nan"), "x"):
            sub, _seen, skip = self._place(wired=40, volume=bad)
            self.assertIsNone(sub.get("qty"), repr(bad))
            self.assertEqual(skip["reason"], "fund_volume_unreadable", repr(bad))   # NOT below_min_qty

    def test_volume_cap_below_the_fund_minimum_skips(self):
        sub, _seen, skip = self._place(wired=40, volume=150.0)   # 150 / 100 -> 1 share < the fund's 2
        self.assertIsNone(sub.get("qty"))
        self.assertEqual(skip["reason"], "fund_volume_cap")

    def test_track_a_fund_lot_has_no_volume_cap(self):
        sub, _seen, _skip = self._place(track="A", wired=40, volume=None)
        self.assertEqual(sub.get("qty"), 40)

    def test_pre_order_sentinel_marks_an_abort_before_any_order(self):
        # with the sentinel, an allocator refusal (no order sent) leaves the pre_order record
        cap = [mock.patch("execution.tier_capital_allocator.live_admit",
                          return_value=SimpleNamespace(approved=False, lease=None, reason="test refusal"))]
        sub, _seen, skip = self._place(stop=4.40, capture=cap, sentinel=True)
        self.assertIsNone(sub.get("qty"))
        self.assertEqual(skip["reason"], "pre_order")

    def test_pre_order_sentinel_is_cleared_before_submit(self):
        sub, _seen, skip = self._place(stop=4.40, sentinel=True)
        self.assertEqual(sub.get("qty"), 40)
        self.assertIsNone(skip)

    def test_track_a_fund_lot_stop_is_not_floored(self):
        # Track A fund lots are unchanged by TRACKB-SIZE-ETF-1028: the 3-tick stop is not widened
        _sub, seen, _skip = self._place(track="A", stop=4.55)
        self.assertAlmostEqual(seen["stop"], 4.55, places=6)


if __name__ == "__main__":
    unittest.main()
