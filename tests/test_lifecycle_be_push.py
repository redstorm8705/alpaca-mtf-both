# ruff: noqa: E501 — fixture lines (project convention)
"""apply_mri_breakeven_push moves stops IN PLACE (P0 2026-09-25, UBER 2026-09-18: 204 min unprotected).

Every broker call is mocked; nothing reaches Alpaca or Slack."""
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd

from execution import broker, lifecycle


class FakeTracker:
    def __init__(self, trades):
        self.open_trades = trades
        self.saves = 0

    def _save_log(self):
        self.saves += 1


class FakeMRI:
    def level(self):
        return "STRESSED"

    def score(self):
        return 70


def _trade(**kw):
    t = {"entry_price": 70.0, "direction": "long", "atr_value": 2.0, "stop": 67.0,
         "qty": 2, "qty_remaining": 2, "rth_day_stop_order_id": "OLD", "score": 11}
    t.update(kw)
    return t


def _order(oid, status="new", stop_price=67.0, qty="2", tif="day", side="sell", otype="stop",
           coid="IN-UBER-s-1-abcd"):
    return SimpleNamespace(id=oid, status=status, stop_price=stop_price, qty=qty,
                           time_in_force=tif, side=side, type=otype, client_order_id=coid)


class Base(unittest.TestCase):
    PRICE = 72.0   # ≥ entry + 0.5×ATR → push eligible

    def setUp(self):
        lifecycle._be_fail_counts.clear()
        lifecycle._be_last_page.clear()
        self.pages = []
        self.events = []
        patches = {
            "fetch_bars": mock.Mock(return_value=pd.DataFrame({"close": [self.PRICE, self.PRICE]})),
            "get_latest_trade": mock.Mock(return_value=self.PRICE),
            "get_open_position": mock.Mock(return_value=SimpleNamespace(qty="2")),
            "resolve_live_order": mock.Mock(side_effect=lambda oid: (_order(oid), oid)),
            "replace_stop_order": mock.Mock(side_effect=lambda sym, oid, px, qty=None: _order(oid + "-r", stop_price=px)),
            "submit_day_stop_order": mock.Mock(side_effect=lambda **k: _order("NEW", stop_price=k["stop_price"])),
            "get_open_orders": mock.Mock(return_value=[]),
            "_log_trade_event": mock.Mock(side_effect=lambda *a, **k: self.events.append((a, k))),
        }
        self.m = {}
        for name, val in patches.items():
            p = mock.patch.object(lifecycle, name, val)
            self.m[name] = p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(lifecycle, "_be_page", side_effect=lambda s, c, b, **k: self.pages.append((s, c, b)))
        p.start()
        self.addCleanup(p.stop)
        # hard guard: no real broker order call may happen
        for fn in ("cancel_order", "_get_trading_client"):
            g = mock.patch.object(broker, fn, side_effect=AssertionError(f"real broker.{fn} called"))
            g.start()
            self.addCleanup(g.stop)

    def run_push(self, trade):
        tr = FakeTracker({"UBER": trade})
        lifecycle.apply_mri_breakeven_push(tr, FakeMRI())
        return tr


class TestMoveInPlace(Base):
    def test_long_moves_stop_in_place_never_cancels(self):
        t = _trade()
        tr = self.run_push(t)
        self.m["replace_stop_order"].assert_called_once()
        args, kw = self.m["replace_stop_order"].call_args
        self.assertEqual(args[:2], ("UBER", "OLD"))
        self.assertTrue(69.94 <= args[2] <= 69.99)        # entry − 1..5c offset
        self.assertEqual(kw["qty"], 2)                     # capped to the live Alpaca qty
        self.m["submit_day_stop_order"].assert_not_called()
        self.assertEqual(t["rth_day_stop_order_id"], "OLD-r")
        self.assertTrue(t["be_pushed_by_mri"])
        self.assertEqual(t["stop"], 70.0)
        self.assertNotIn("be_broker_pending", t)
        self.assertAlmostEqual(t["broker_stop_px"], args[2])
        self.assertGreaterEqual(tr.saves, 1)
        self.assertEqual(self.events[0][1]["method"], "replace")
        self.assertEqual(self.pages, [])

    def test_uber_2026_09_18_replay_short_buy_stop_moved_in_place(self):
        # production log: UBER SHORT 2, DAY stop BUY 2 @ $75.46 (ea420e21…); at 14:05:10 the old
        # push cancelled it and the resubmit @ $71.75 hit 40310000 → 204 min with no broker stop.
        self.m["fetch_bars"].return_value = pd.DataFrame({"close": [69.5, 69.5]})
        self.m["get_latest_trade"].return_value = 69.5
        self.m["get_open_position"].return_value = SimpleNamespace(qty="-2", side="short")
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, stop_price=75.46, side="buy"), oid)
        t = _trade(direction="short", entry_price=71.72, stop=75.46, atr_value=3.0,
                   rth_day_stop_order_id="ea420e21")
        self.run_push(t)
        self.m["replace_stop_order"].assert_called_once()
        args, kw = self.m["replace_stop_order"].call_args
        self.assertEqual(args[1], "ea420e21")
        self.assertTrue(71.73 <= args[2] <= 71.77)          # entry + 1..5c, tighter than $75.46
        self.assertEqual(kw["qty"], 2)
        self.m["submit_day_stop_order"].assert_not_called()  # no cancel, no resubmit race
        self.assertTrue(t["be_pushed_by_mri"])

    def test_replace_refused_keeps_old_stop_and_flag_unset_then_warns_after_3(self):
        self.m["replace_stop_order"].side_effect = lambda *a, **k: None
        t = _trade()
        for _ in range(3):
            self.run_push(t)
            self.assertEqual(t["rth_day_stop_order_id"], "OLD")
            self.assertEqual(t["stop"], 70.0)             # software stop tightened (never looser)
            self.assertTrue(t["be_broker_pending"])       # broker move retried next cycle
            self.assertNotIn("be_pushed_by_mri", t)
        self.assertEqual(self.m["replace_stop_order"].call_count, 3)
        self.assertEqual(len(self.pages), 1)
        sym, critical, body = self.pages[0]
        self.assertFalse(critical)
        self.assertIn("LIVE at the old level", body)

    def test_refused_move_on_a_stop_no_longer_live_is_unknown_not_protected(self):
        state = {"n": 0}

        def resolve(oid):                     # live at the status read, gone after the PATCH
            state["n"] += 1
            return (_order(oid), oid) if state["n"] % 2 == 1 else (_order(oid, status="filled"), oid)
        self.m["resolve_live_order"].side_effect = resolve
        self.m["replace_stop_order"].side_effect = lambda *a, **k: None
        t = _trade()
        for _ in range(3):
            self.run_push(t)
        self.assertEqual(len(self.pages), 1)
        self.assertNotIn("IS protected", self.pages[0][2])

    def test_not_replaceable_yet_is_skipped_without_counting(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, status="pending_new"), oid)
        t = _trade()
        for _ in range(5):
            self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()
        self.assertEqual(lifecycle._be_fail_counts, {})
        self.assertEqual(self.pages, [])

    def test_filled_stop_skips_everything(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, status="filled"), oid)
        t = _trade()
        self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()
        self.m["submit_day_stop_order"].assert_not_called()
        self.assertNotIn("be_pushed_by_mri", t)

    def test_replaced_chain_is_followed(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (_order("LIVE2"), "LIVE2")
        t = _trade()
        self.run_push(t)
        self.assertEqual(self.m["replace_stop_order"].call_args[0][1], "LIVE2")
        self.assertEqual(t["rth_day_stop_order_id"], "LIVE2-r")

    def test_qty_capped_to_this_tiers_shares_not_the_whole_position(self):
        # masked-loss/adversarial: a co-held day-tier lot makes Alpaca show 5; this trade owns 2
        self.m["get_open_position"].return_value = SimpleNamespace(qty="5", side="long")
        t = _trade()
        self.run_push(t)
        self.assertEqual(self.m["replace_stop_order"].call_args[1]["qty"], 2)

    def test_qty_capped_on_submit_path_too(self):
        self.m["get_open_position"].return_value = SimpleNamespace(qty="5", side="long")
        t = _trade(rth_day_stop_order_id=None)
        self.run_push(t)
        self.assertEqual(self.m["submit_day_stop_order"].call_args[1]["qty"], 2)

    def test_net_position_on_opposite_side_is_never_used_to_resize(self):
        self.m["get_open_position"].return_value = SimpleNamespace(qty="-3", side="short")
        t = _trade()
        self.run_push(t)
        self.assertIsNone(self.m["replace_stop_order"].call_args[1]["qty"])

    def test_broken_chain_is_unknown_never_protected(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, status="replaced"), oid)
        t = _trade()
        for _ in range(3):
            self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()
        self.assertEqual(len(self.pages), 1)
        self.assertIn("could NOT be read", self.pages[0][2])
        self.assertNotIn("IS protected", self.pages[0][2])

    def test_one_symbol_error_does_not_abort_the_loop(self):
        with mock.patch.object(lifecycle, "_position_qty", side_effect=lambda s, d: (_ for _ in ()).throw(RuntimeError("x")) if s == "BAD" else ("open", 2)):
            tr = FakeTracker({"BAD": _trade(), "UBER": _trade()})
            lifecycle.apply_mri_breakeven_push(tr, FakeMRI())
        self.assertTrue(tr.open_trades["UBER"]["be_pushed_by_mri"])

    def test_stop_already_above_target_is_never_loosened(self):
        # trail ratchet already moved the broker stop to $70.50 (> breakeven): leave it alone
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, stop_price=70.5), oid)
        t = _trade()
        self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()
        self.m["submit_day_stop_order"].assert_not_called()
        self.assertTrue(t["be_pushed_by_mri"])
        self.assertEqual(t["broker_stop_px"], 70.5)
        self.assertEqual(self.events[0][1]["method"], "already")

    def test_pending_retry_after_trail_ratchet_does_not_pull_stop_down(self):
        # an earlier push tightened only the software stop; since then the trail moved the broker
        # stop above entry — the retry must finish without moving it back to breakeven
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, stop_price=71.2), oid)
        t = _trade(stop=70.0, trail_stop=71.2, be_broker_pending=True)
        self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()
        self.assertTrue(t["be_pushed_by_mri"])
        self.assertNotIn("be_broker_pending", t)

    def test_short_moves_down_to_breakeven_plus_offset(self):
        self.m["fetch_bars"].return_value = pd.DataFrame({"close": [67.0, 67.0]})
        self.m["get_latest_trade"].return_value = 67.0
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, stop_price=73.0, side="buy"), oid)
        self.m["get_open_position"].return_value = SimpleNamespace(qty="-2", side="short")
        t = _trade(direction="short", stop=73.0)
        self.run_push(t)
        px = self.m["replace_stop_order"].call_args[0][2]
        self.assertTrue(70.01 <= px <= 70.05)
        self.assertEqual(self.m["replace_stop_order"].call_args[1]["qty"], 2)
        self.assertEqual(t["stop"], 70.0)

    def test_position_gone_is_skipped(self):
        self.m["get_open_position"].return_value = None
        t = _trade()
        self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()

    def test_position_unreadable_moves_price_only(self):
        self.m["get_open_position"].side_effect = RuntimeError("503")
        t = _trade()
        self.run_push(t)
        self.assertIsNone(self.m["replace_stop_order"].call_args[1]["qty"])
        self.assertTrue(t["be_pushed_by_mri"])

    def test_unreadable_stop_pages_unknown_never_claims_protection(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (None, oid)
        t = _trade()
        for _ in range(3):
            self.run_push(t)
        self.assertEqual(len(self.pages), 1)
        self.assertIn("could NOT be read", self.pages[0][2])
        self.assertNotIn("be_pushed_by_mri", t)


class TestSubmitPath(Base):
    def test_software_only_position_gets_new_day_stop_with_no_cancel_blocking(self):
        t = _trade(rth_day_stop_order_id=None)
        self.run_push(t)
        kw = self.m["submit_day_stop_order"].call_args[1]
        self.assertFalse(kw["allow_cancel_blocking"])
        self.assertEqual(kw["qty"], 2)
        self.assertEqual(t["rth_day_stop_order_id"], "NEW")
        self.assertTrue(t["be_pushed_by_mri"])
        self.assertEqual(self.events[0][1]["method"], "submit")

    def test_stale_cancelled_id_then_failed_submit_is_critical(self):
        self.m["resolve_live_order"].side_effect = lambda oid: (_order(oid, status="canceled"), oid)
        self.m["submit_day_stop_order"].side_effect = lambda **k: None
        t = _trade()
        self.run_push(t)
        self.assertIsNone(t["rth_day_stop_order_id"])
        self.assertEqual(len(self.pages), 1)
        self.assertTrue(self.pages[0][1])                 # had a broker stop, none now → CRITICAL
        self.assertNotIn("be_pushed_by_mri", t)

    def test_software_only_failed_submit_is_not_critical(self):
        self.m["submit_day_stop_order"].side_effect = lambda **k: None
        t = _trade(rth_day_stop_order_id=None)
        self.run_push(t)
        self.assertEqual(self.pages, [])                  # warning only after 3
        self.assertEqual(lifecycle._be_fail_counts["UBER"], 1)

    def test_untracked_stop_of_another_tier_is_never_adopted(self):
        self.m["submit_day_stop_order"].side_effect = lambda **k: broker.PROTECTION_ALREADY_HELD
        self.m["get_open_orders"].return_value = [_order("DTLEG", coid="a1b2c3-untagged-oco-leg"),
                                                  _order("QH", coid="QH-UBER-s-1-x", tif="gtc")]
        t = _trade(rth_day_stop_order_id=None)
        self.run_push(t)
        self.assertIsNone(t["rth_day_stop_order_id"])
        self.assertEqual(lifecycle._be_fail_counts["UBER"], 1)

    def test_untracked_stop_larger_than_this_tier_is_never_adopted(self):
        self.m["submit_day_stop_order"].side_effect = lambda **k: broker.PROTECTION_ALREADY_HELD
        self.m["get_open_orders"].return_value = [_order("BIG", qty="5")]
        t = _trade(rth_day_stop_order_id=None)
        self.run_push(t)
        self.assertIsNone(t["rth_day_stop_order_id"])

    def test_held_by_untracked_stop_is_adopted_not_cancelled(self):
        self.m["submit_day_stop_order"].side_effect = lambda **k: broker.PROTECTION_ALREADY_HELD
        self.m["get_open_orders"].return_value = [_order("UNTRACKED", tif="day")]
        t = _trade(rth_day_stop_order_id=None)
        self.run_push(t)
        self.assertEqual(t["rth_day_stop_order_id"], "UNTRACKED")
        self.assertNotIn("be_pushed_by_mri", t)          # moved in place next cycle
        self.run_push(t)
        self.assertEqual(self.m["replace_stop_order"].call_args[0][1], "UNTRACKED")
        self.assertTrue(t["be_pushed_by_mri"])

    def test_both_ids_is_an_anomaly_page_and_both_are_moved(self):
        t = _trade(gtc_stop_order_id="GTC1")
        self.run_push(t)
        self.assertEqual(self.m["replace_stop_order"].call_count, 2)
        self.assertEqual((t["rth_day_stop_order_id"], t["gtc_stop_order_id"]), ("OLD-r", "GTC1-r"))
        self.assertIn("anomaly", self.pages[0][2])


class TestUnchangedGates(Base):
    def test_no_push_without_half_atr_buffer(self):
        self.m["fetch_bars"].return_value = pd.DataFrame({"close": [70.5, 70.5]})
        self.m["get_latest_trade"].return_value = 70.5
        t = _trade()
        self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()
        self.assertNotIn("be_pushed_by_mri", t)

    def test_already_at_breakeven_sets_flag_only(self):
        t = _trade(stop=70.0)
        self.run_push(t)
        self.m["replace_stop_order"].assert_not_called()
        self.assertTrue(t["be_pushed_by_mri"])


if __name__ == "__main__":
    unittest.main()


class TestPageThrottle(unittest.TestCase):
    def setUp(self):
        lifecycle._be_fail_counts.clear()
        lifecycle._be_last_page.clear()
        p = mock.patch.object(lifecycle, "_log_trade_event")   # never write the real event log
        p.start()
        self.addCleanup(p.stop)

    def test_anomaly_page_never_masks_the_failure_warning(self):
        sent = []
        import alerts
        with mock.patch.object(alerts, "_send", side_effect=lambda t, b, **k: sent.append(b)):
            lifecycle._be_page("UBER", False, "anomaly body", kind="anomaly")
            for _ in range(3):
                lifecycle._be_failed("UBER", "old_live", "refused")
        self.assertEqual(len(sent), 2)
        self.assertIn("failing 3x", sent[1])

    def test_same_kind_throttled_for_30_min(self):
        sent = []
        import alerts
        with mock.patch.object(alerts, "_send", side_effect=lambda t, b, **k: sent.append(b)), \
             mock.patch.object(lifecycle.time, "monotonic", side_effect=[1000.0, 1100.0, 3000.0]):
            for _ in range(3):
                lifecycle._be_page("UBER", True, "x")
        self.assertEqual(len(sent), 2)
