# ruff: noqa: E501 — fixture lines (project convention)
"""exit_logic stop moves in place (P0 increment 3, 2026-09-26). Every broker call is mocked."""
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from execution import broker, exit_logic as el


def _order(oid, status="new", stop_price=95.0, qty="10"):
    return SimpleNamespace(id=oid, status=status, stop_price=stop_price, qty=qty)


class FakeTracker:
    def __init__(self, trades):
        self.open_trades = trades
        self.saves = 0

    def _save_log(self):
        self.saves += 1

    def update_trail_stop(self, symbol, px):
        t = self.open_trades[symbol]
        cur = t.get("trail_stop")
        if cur is None or (t["direction"] == "long" and px > cur) or (t["direction"] == "short" and px < cur):
            t["trail_stop"] = px

    def set_gtc_stop_order_id(self, symbol, oid):
        self.open_trades[symbol]["gtc_stop_order_id"] = oid


class Base(unittest.TestCase):
    def setUp(self):
        self.m = {}
        pats = {
            "resolve_live_order": mock.Mock(side_effect=lambda oid: (_order(oid), oid)),
            "replace_stop_order": mock.Mock(side_effect=lambda sym, oid, px, qty=None: _order(oid + "-r", stop_price=px, qty=str(qty or 10))),
            "get_open_position": mock.Mock(return_value=SimpleNamespace(qty="10", qty_available="10", side="long")),
            "submit_day_stop_order": mock.Mock(side_effect=lambda **k: _order("NEWDAY", stop_price=k["stop_price"])),
            "submit_gtc_stop_order": mock.Mock(side_effect=lambda **k: _order("NEWGTC", stop_price=k["stop_price"])),
            "cancel_stop_confirmed": mock.Mock(return_value=True),
            "_log_trade_event": mock.Mock(),
        }
        for n, v in pats.items():
            p = mock.patch.object(el, n, v)
            self.m[n] = p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(el.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        for fn in ("cancel_order", "_get_trading_client", "cancel_open_orders_for_symbol"):   # no real broker call
            g = mock.patch.object(broker, fn, side_effect=AssertionError(f"real broker.{fn}"))
            g.start()
            self.addCleanup(g.stop)


class TestMoveStops(Base):
    def test_moves_price_in_place_and_stores_new_id(self):
        t = {"direction": "long", "rth_day_stop_order_id": "S1"}
        tr = FakeTracker({"X": t})
        self.assertEqual(el._move_stops("X", t, tr, 97.004, None, "long"), "moved")
        self.m["replace_stop_order"].assert_called_once_with("X", "S1", 97.0, qty=None)
        self.assertEqual(t["rth_day_stop_order_id"], "S1-r")
        self.assertEqual(t["broker_stop_px"], 97.0)

    def test_never_loosens(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, stop_price=98.0), oid)
        t = {"direction": "long", "rth_day_stop_order_id": "S1"}
        self.assertEqual(el._move_stops("X", t, FakeTracker({"X": t}), 97.0, None, "long"), "moved")
        self.m["replace_stop_order"].assert_not_called()
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, stop_price=101.0), oid)
        el._move_stops("X", t, FakeTracker({"X": t}), 102.0, None, "short")
        self.m["replace_stop_order"].assert_not_called()

    def test_resize_keeps_price(self):
        t = {"direction": "long", "rth_day_stop_order_id": "S1"}
        el._move_stops("X", t, FakeTracker({"X": t}), None, 7, "long")
        self.m["replace_stop_order"].assert_called_once_with("X", "S1", 95.0, qty=7)

    def test_statuses(self):
        t = {"direction": "long", "rth_day_stop_order_id": "S1"}
        tr = FakeTracker({"X": t})
        for st, want in (("filled", "filled"), ("pending_replace", "retry"), ("accepted", "retry"),
                         ("replaced", "unknown"), ("suspended", "unknown")):
            self.m["resolve_live_order"].side_effect = lambda oid, _s=st: (_order(oid, status=_s), oid)
            self.assertEqual(el._move_stops("X", dict(t), tr, 97.0, None, "long"), want, st)
        self.m["resolve_live_order"].side_effect = lambda oid: (None, oid)
        self.assertEqual(el._move_stops("X", dict(t), tr, 97.0, None, "long"), "unknown")
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, status="canceled"), oid)
        t2 = dict(t)
        self.assertEqual(el._move_stops("X", t2, tr, 97.0, None, "long"), "none")
        self.assertIsNone(t2["rth_day_stop_order_id"])
        self.m["replace_stop_order"].assert_not_called()

    def test_both_stops_anomaly_pages_once(self):
        el._both_stops_warned.discard("Y")
        t = {"direction": "long", "rth_day_stop_order_id": "D1", "gtc_stop_order_id": "G1"}
        with mock.patch("alerts.send_slack") as sl:
            el._move_stops("Y", t, FakeTracker({"Y": t}), 97.0, None, "long")
            el._move_stops("Y", t, FakeTracker({"Y": t}), 98.0, None, "long")
        self.assertEqual(sl.call_count, 1)
        # the fake broker reports every stop at $95, so each call moves BOTH stops: 2 calls × 2 stops
        self.assertEqual(self.m["replace_stop_order"].call_count, 4)
        self.assertEqual((t["rth_day_stop_order_id"], t["gtc_stop_order_id"]), ("D1-r-r", "G1-r-r"))

    def test_refused_replace_is_retry(self):
        self.m["replace_stop_order"].side_effect = lambda *a, **k: None
        t = {"direction": "long", "rth_day_stop_order_id": "S1"}
        self.assertEqual(el._move_stops("X", t, FakeTracker({"X": t}), 97.0, None, "long"), "retry")
        self.assertEqual(t["rth_day_stop_order_id"], "S1")

    def test_no_stored_stop_is_none(self):
        self.assertEqual(el._move_stops("X", {"direction": "long"}, FakeTracker({}), 97.0, None, "long"), "none")


class TestWaitSharesFree(Base):
    def test_free_immediately_and_timeout(self):
        self.m["get_open_position"].return_value = SimpleNamespace(qty="9", qty_available="3")
        self.assertTrue(el._wait_shares_free("X", 3))
        self.m["get_open_position"].return_value = SimpleNamespace(qty="9", qty_available="0")
        with mock.patch.object(el.time, "monotonic", side_effect=[0.0, 0.5, 3.0]):
            self.assertFalse(el._wait_shares_free("X", 3, max_wait_s=2.0))

    def test_short_position_negative_available(self):
        self.m["get_open_position"].return_value = SimpleNamespace(qty="-9", qty_available="-3")
        self.assertTrue(el._wait_shares_free("X", 3))

    def test_unreadable_or_gone(self):
        self.m["get_open_position"].return_value = SimpleNamespace(qty="9")
        self.assertFalse(el._wait_shares_free("X", 3))
        self.m["get_open_position"].return_value = None
        self.assertFalse(el._wait_shares_free("X", 3))


class TestSubmitNewStop(Base):
    def test_sized_to_this_trade_not_a_coheld_lot_and_never_cancels(self):
        self.m["get_open_position"].return_value = SimpleNamespace(qty="15", side="long")
        t = {"direction": "long"}
        self.assertTrue(el._submit_new_stop("X", t, FakeTracker({"X": t}), 97.0, 10, "long", None, "t"))
        kw = self.m["submit_day_stop_order"].call_args[1]
        self.assertEqual((kw["qty"], kw["allow_cancel_blocking"]), (10, False))

    def test_clamped_to_smaller_position(self):
        self.m["get_open_position"].return_value = SimpleNamespace(qty="4", side="long")
        t = {"direction": "long"}
        el._submit_new_stop("X", t, FakeTracker({"X": t}), 97.0, 10, "long", None, "t")
        self.assertEqual(self.m["submit_day_stop_order"].call_args[1]["qty"], 4)

    def test_overnight_gtc_unless_force_day(self):
        t = {"direction": "long", "overnight": True}
        el._submit_new_stop("X", t, FakeTracker({"X": t}), 97.0, 10, "long", None, "t")
        self.assertEqual(t["gtc_stop_order_id"], "NEWGTC")
        t2 = {"direction": "long", "overnight": True}
        el._submit_new_stop("X", t2, FakeTracker({"X": t2}), 97.0, 10, "long", None, "t", force_day=True)
        self.assertEqual(t2["rth_day_stop_order_id"], "NEWDAY")

    def test_outcomes(self):
        t = {"direction": "long"}
        tr = FakeTracker({"X": t})
        self.m["submit_day_stop_order"].side_effect = lambda **k: broker.PROTECTION_ALREADY_HELD
        self.assertTrue(el._submit_new_stop("X", t, tr, 97.0, 10, "long", None, "t"))
        self.m["submit_day_stop_order"].side_effect = lambda **k: None
        self.assertFalse(el._submit_new_stop("X", t, tr, 97.0, 10, "long", None, "t"))
        self.m["get_open_position"].return_value = None
        self.assertTrue(el._submit_new_stop("X", t, tr, 97.0, 10, "long", None, "t"))   # nothing to protect


class TestPartialExitSequence(Base):
    """T1 partial on a long 9 @ $100 (target $110, stop $95): tranche 3 shares, 6 remain."""

    def setUp(self):
        super().setUp()
        main_stub = types.ModuleType("main")
        main_stub._spy_event_type = ""
        p = mock.patch.dict(sys.modules, {"main": main_stub})
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        more = {
            "fetch_bars": mock.Mock(return_value=pd.DataFrame({"close": [104.5, 104.5]})),
            "get_latest_trade": mock.Mock(return_value=104.5),
            "_get_qhm_syms": mock.Mock(return_value=set()),
            "partial_close_position": mock.Mock(side_effect=lambda s, q, **k: self.calls.append(("partial", q)) or True),
            "_fetch_actual_fill_price": mock.Mock(return_value=104.5),
            "alert_partial": mock.Mock(),
            "_record_partial_tqi": mock.Mock(),
            "get_open_orders": mock.Mock(return_value=[]),
            "cancel_order": mock.Mock(return_value=True),
        }
        for n, v in more.items():
            p = mock.patch.object(el, n, v)
            self.m[n] = p.start()
            self.addCleanup(p.stop)
        self.m["replace_stop_order"].side_effect = (
            lambda sym, oid, px, qty=None: self.calls.append(("replace", oid, px, qty)) or _order(oid + "-r", stop_price=px, qty=str(qty or 6)))
        self.m["get_open_position"].return_value = SimpleNamespace(qty="9", qty_available="3", side="long")
        p = mock.patch.object(el.config, "PARTIAL_EXIT_ENABLED", True)
        p.start()
        self.addCleanup(p.stop)
        el._get_partial_fail_counts().clear()
        self.addCleanup(el._get_partial_fail_counts().clear)

    def _trade(self, **kw):
        t = {"direction": "long", "entry_price": 100.0, "atr_value": 4.0, "qty": 9, "qty_remaining": 9,
             "target": 110.0, "stop": 95.0, "rth_day_stop_order_id": "S1", "trade_mode": "swing", "score": 10}
        t.update(kw)
        return t

    def _run(self, t):
        tr = FakeTracker({"X": t})
        el.check_partial_exits(tr, kelly=mock.Mock(), risk=mock.Mock(), mri=None, last_vix=15.0)
        return tr

    def test_shrink_then_sell_then_move_never_cancel(self):
        t = self._trade()
        self._run(t)
        self.assertEqual(self.calls[0], ("replace", "S1", 95.0, 6))            # shrink to the 6 that remain
        self.assertEqual(self.calls[1], ("partial", 3))                        # then sell the tranche
        self.assertEqual(self.calls[2][0], "replace")                          # then move to breakeven/trail
        self.assertGreaterEqual(self.calls[2][2], 100.0)
        self.m["cancel_stop_confirmed"].assert_not_called()
        self.m["cancel_order"].assert_not_called()
        self.assertEqual(t["qty_remaining"], 6)

    def test_failed_partial_restores_stop_to_full_size_and_keeps_other_stops(self):
        self.m["partial_close_position"].side_effect = lambda s, q, **k: self.calls.append(("partial", q)) or False
        blocking = SimpleNamespace(id="LMT", type="limit")
        stop = SimpleNamespace(id="S1-r", type="stop")
        self.m["get_open_orders"].return_value = [blocking, stop]
        t = self._trade()
        self._run(t)
        self.assertEqual(self.calls[0], ("replace", "S1", 95.0, 6))
        self.assertEqual(self.calls[2], ("replace", "S1-r", 95.0, 9))          # restored to all 9 shares
        self.m["cancel_order"].assert_called_once_with("LMT")                  # never the stop

    def test_shares_not_freed_restores_and_does_not_sell(self):
        with mock.patch.object(el, "_wait_shares_free", return_value=False):
            t = self._trade()
            self._run(t)
        self.assertNotIn(("partial", 3), self.calls)
        self.assertEqual(self.calls[-1], ("replace", "S1-r", 95.0, 9))
        self.assertEqual(t["qty_remaining"], 9)

    def test_stop_not_resizable_defers_tranche(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, status="pending_replace"), oid)
        t = self._trade()
        self._run(t)
        self.assertEqual(self.calls, [])
        self.assertEqual(t["qty_remaining"], 9)

    def test_final_tranche_failure_re_places_the_cancelled_stop(self):
        # T3 reached with 3 shares left: stop cancelled (confirmed), sale fails → stop re-placed for all 3
        self.m["fetch_bars"].return_value = pd.DataFrame({"close": [110.5, 110.5]})
        self.m["get_latest_trade"].return_value = 110.5
        self.m["get_open_position"].return_value = SimpleNamespace(qty="3", qty_available="3", side="long")
        self.m["partial_close_position"].side_effect = lambda s, q, **k: self.calls.append(("partial", q)) or False
        t = self._trade(qty=9, qty_remaining=3, profit_tranche_level=2, trail_stop=None)
        self._run(t)
        self.m["cancel_stop_confirmed"].assert_called_once_with("X", "S1")
        self.assertIn(("partial", 3), self.calls)
        kw = self.m["submit_day_stop_order"].call_args[1]
        self.assertEqual((kw["qty"], kw["allow_cancel_blocking"]), (3, False))
        self.assertEqual(t["rth_day_stop_order_id"], "NEWDAY")

    def test_final_tranche_not_cancelled_defers(self):
        self.m["fetch_bars"].return_value = pd.DataFrame({"close": [110.5, 110.5]})
        self.m["get_latest_trade"].return_value = 110.5
        self.m["cancel_stop_confirmed"].return_value = False
        t = self._trade(qty=9, qty_remaining=3, profit_tranche_level=2, trail_stop=None)
        self._run(t)
        self.assertNotIn(("partial", 3), self.calls)
        self.assertEqual(t["rth_day_stop_order_id"], "S1")

    def test_software_only_position_sells_then_places_stop(self):
        t = self._trade(rth_day_stop_order_id=None)
        self._run(t)
        self.assertEqual(self.calls[0], ("partial", 3))
        self.assertEqual(self.m["submit_day_stop_order"].call_args[1]["allow_cancel_blocking"], False)


class TestTrailRatchet(Base):
    def setUp(self):
        super().setUp()
        main_stub = types.ModuleType("main")
        main_stub._spy_event_type = ""
        p = mock.patch.dict(sys.modules, {"main": main_stub})
        p.start()
        self.addCleanup(p.stop)
        for n, v in {"fetch_bars": mock.Mock(return_value=pd.DataFrame({"close": [108.0, 108.0]})),
                     "get_latest_trade": mock.Mock(return_value=108.0),
                     "_get_qhm_syms": mock.Mock(return_value=set())}.items():
            p = mock.patch.object(el, n, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(el.config, "PARTIAL_EXIT_ENABLED", False)
        p.start()
        self.addCleanup(p.stop)

    def test_ratchet_moves_stop_in_place(self):
        t = {"direction": "long", "entry_price": 100.0, "atr_value": 4.0, "qty": 6, "qty_remaining": 6,
             "stop": 100.0, "trail_stop": 103.0, "trail_phase": 1, "rth_day_stop_order_id": "S1",
             "trade_mode": "swing", "score": 10}
        el.check_partial_exits(FakeTracker({"X": t}), kelly=mock.Mock(), risk=mock.Mock(), mri=None, last_vix=15.0)
        args = self.m["replace_stop_order"].call_args[0]
        self.assertEqual(args[:2], ("X", "S1"))
        self.assertEqual(args[2], 105.0)                                       # 108 − 0.75×4
        self.m["submit_day_stop_order"].assert_not_called()

    def test_refused_move_sets_sync_pending(self):
        self.m["replace_stop_order"].side_effect = lambda *a, **k: None
        t = {"direction": "long", "entry_price": 100.0, "atr_value": 4.0, "qty": 6, "qty_remaining": 6,
             "stop": 100.0, "trail_stop": 103.0, "trail_phase": 1, "rth_day_stop_order_id": "S1",
             "trade_mode": "swing", "score": 10}
        el.check_partial_exits(FakeTracker({"X": t}), kelly=mock.Mock(), risk=mock.Mock(), mri=None, last_vix=15.0)
        self.assertTrue(t["_stop_sync_pending"])
        self.assertEqual(t["rth_day_stop_order_id"], "S1")


if __name__ == "__main__":
    unittest.main()
