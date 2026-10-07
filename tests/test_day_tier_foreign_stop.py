#!/usr/bin/env python3
# ruff: noqa: E501
"""2026-10-07: the main bot's orphan scan adopted the day-tier EWY short and AAPL long (cancelled the day-tier OCO,
placed its own IN- stops). The day tier then read 'naked', its scoped close was refused (the foreign stop holds the
shares) and it paged every 2-min tick. A lot covered by ANOTHER tier's live stop is protected: no close attempt
(never cancel another tier's order — B1), one page per symbol per day."""
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import day_trade_manager as dtm


def _ord(side, qty, otype="stop", coid="IN-EWY-b-1-x", filled=0):
    return SimpleNamespace(side=side, qty=qty, filled_qty=filled, order_type=otype, client_order_id=coid)


class ForeignStopCovers(unittest.TestCase):
    def _c(self, orders, side, qty):
        with mock.patch("execution.broker.get_open_orders", return_value=orders):
            return dtm._foreign_stop_covers("EWY", side, qty)

    def test_covers(self):
        self.assertTrue(self._c([_ord("buy", 1)], "short", 1))                       # EWY short, IN- buy stop
        self.assertTrue(self._c([_ord("sell", 4, coid="IN-AAPL-s-1-x")], "long", 4))

    def test_not_covering(self):
        self.assertFalse(self._c([_ord("sell", 1)], "short", 1))                     # wrong side
        self.assertFalse(self._c([_ord("buy", 1, otype="limit")], "short", 1))       # not a stop
        self.assertFalse(self._c([_ord("buy", 2)], "short", 3))                      # too small
        self.assertFalse(self._c([_ord("buy", 3, filled=2)], "short", 3))            # remaining 1 < 3
        self.assertFalse(self._c([_ord("buy", 1, coid="DT-EWY-b-1-x")], "short", 1)) # our own stop is not foreign
        self.assertFalse(self._c([], "short", 1))
        self.assertFalse(self._c(None, "short", 1))
        with mock.patch("execution.broker.get_open_orders", side_effect=RuntimeError("down")):
            self.assertFalse(dtm._foreign_stop_covers("EWY", "short", 1))


class PageOnce(unittest.TestCase):
    def test_pages_once_per_day(self):
        state: dict = {}
        with mock.patch.object(dtm, "_load_state", side_effect=lambda: state), \
                mock.patch.object(dtm, "_save_state", side_effect=lambda st: True), \
                mock.patch.object(dtm, "_page") as pg:
            for _ in range(5):
                dtm._page_once_today("EWY", "foreign_stop", "msg")
        self.assertEqual(pg.call_count, 1)


class ReconcileAndForceFlat(unittest.TestCase):
    TGT = {"EWY": {"symbol": "EWY", "side": "short", "qty": 1, "entry_price": 181.46, "trade_id": "DT-EWY",
                   "order_id": "O1"}}

    def _patches(self, covers, pos_qty="-1"):
        pos = SimpleNamespace(qty=pos_qty, side="short")
        return [mock.patch.object(dtm, "_enabled", return_value=True),
                mock.patch.object(dtm, "_flatten_targets", return_value=dict(self.TGT)),
                mock.patch.object(dtm, "_load_state", return_value={}),
                mock.patch.object(dtm, "_save_state", return_value=True),
                mock.patch("execution.broker.get_open_position", return_value=pos),
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False),
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False),
                mock.patch.object(dtm, "_foreign_stop_covers", return_value=covers),
                mock.patch.object(dtm, "_page")]

    def _run(self, fn, covers):
        from contextlib import ExitStack
        with ExitStack() as st:
            mocks = [st.enter_context(p) for p in self._patches(covers)]
            flat = st.enter_context(mock.patch.object(dtm, "flatten_position", return_value=True))
            out = fn()
        return out, flat, mocks[-1]

    def test_reconcile_foreign_stop_is_protected_no_close(self):
        out, flat, pg = self._run(dtm.reconcile_open_state, True)
        flat.assert_not_called()
        self.assertEqual(out["protected"], 1)
        self.assertEqual(pg.call_count, 1)

    def test_reconcile_truly_naked_still_flattens(self):
        out, flat, _ = self._run(dtm.reconcile_open_state, False)
        flat.assert_called_once()

    def test_force_flat_skips_a_foreign_stop_lot(self):
        n, flat, pg = self._run(lambda: dtm.force_flat_all("eod_force_flat"), True)   # _has_live_daytrade_stop False
        flat.assert_not_called()
        self.assertEqual(n, 0)
        self.assertEqual(pg.call_count, 1)

    def test_force_flat_closes_own_lot(self):
        n, flat, _ = self._run(lambda: dtm.force_flat_all("eod_force_flat"), False)
        flat.assert_called_once()


class OwnOcoLegIsNotForeign(unittest.TestCase):
    """Cold-2nd 2026-10-07: our OCO stop leg has no DT- coid. At EOD a normal protected lot must still be flattened."""

    def test_own_leg_by_recorded_id_is_excluded(self):
        st = {"entry::AAPL::b": {"symbol": "AAPL", "stop_order_id": "LEG1", "oco_order_id": "OCO1"}}
        orders = [SimpleNamespace(id="LEG1", side="sell", qty=4, filled_qty=0, order_type="stop", client_order_id=None)]
        with mock.patch.object(dtm, "_load_state", return_value=st), \
                mock.patch("execution.broker.get_open_orders", return_value=orders):
            self.assertFalse(dtm._foreign_stop_covers("AAPL", "long", 4))

    def test_eod_flattens_a_normal_oco_protected_lot(self):
        pos = SimpleNamespace(qty="4", side="long")
        tgt = {"AAPL": {"symbol": "AAPL", "side": "long", "qty": 4, "entry_price": 335.0, "trade_id": "DT-A",
                        "order_id": "O1"}}
        st = {"entry::AAPL::b": {"symbol": "AAPL", "stop_order_id": "LEG1", "oco_order_id": "OCO1"}}
        orders = [SimpleNamespace(id="LEG1", side="sell", qty=4, filled_qty=0, order_type="stop", client_order_id=None)]
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value=tgt), \
                mock.patch.object(dtm, "_load_state", return_value=st), \
                mock.patch.object(dtm, "_save_state", return_value=True), \
                mock.patch("execution.broker.get_open_position", return_value=pos), \
                mock.patch("execution.broker.get_open_orders", return_value=orders), \
                mock.patch.object(dtm, "_page"), \
                mock.patch.object(dtm, "flatten_position", return_value=True) as flat:
            n = dtm.force_flat_all("eod_force_flat")
        flat.assert_called_once()
        self.assertEqual((n, flat.call_args.args[1]), (1, 4))


class EmptyStateOwnLeg(unittest.TestCase):
    """Cold-2nd 2026-10-07: with the state file unreadable/empty, our own UUID-id OCO stop leg must NOT count as a
    foreign stop — EOD still flattens."""

    def test_untagged_leg_never_counts(self):
        orders = [SimpleNamespace(id="0b9f-uuid", side="sell", qty=4, filled_qty=0, order_type="stop",
                                  client_order_id="5f1e2c3a-uuid")]
        with mock.patch.object(dtm, "_load_state", return_value={}), \
                mock.patch("execution.broker.get_open_orders", return_value=orders):
            self.assertFalse(dtm._foreign_stop_covers("AAPL", "long", 4))

    def test_eod_flattens_with_empty_state(self):
        pos = SimpleNamespace(qty="4", side="long")
        tgt = {"AAPL": {"symbol": "AAPL", "side": "long", "qty": 4, "entry_price": 335.0, "trade_id": "DT-A",
                        "order_id": "O1"}}
        orders = [SimpleNamespace(id="P", side="sell", qty=4, filled_qty=0, order_type="limit",
                                  client_order_id="DT-AAPL-b-1-x"),
                  SimpleNamespace(id="0b9f-uuid", side="sell", qty=4, filled_qty=0, order_type="stop",
                                  client_order_id="5f1e2c3a-uuid")]
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value=tgt), \
                mock.patch.object(dtm, "_load_state", return_value={}), \
                mock.patch.object(dtm, "_save_state", return_value=True), \
                mock.patch("execution.broker.get_open_position", return_value=pos), \
                mock.patch("execution.broker.get_open_orders", return_value=orders), \
                mock.patch.object(dtm, "_page"), \
                mock.patch.object(dtm, "flatten_position", return_value=True) as flat:
            dtm.force_flat_all("eod_force_flat")
        flat.assert_called_once()


class CoHeldCoverage(unittest.TestCase):
    """Cold-2nd 2026-10-07: main bot long 10 with its own 10-sh stop + a day-tier long 4 whose stop never landed
    -> broker 14. The foreign stop covers 10 < 14, so the day-tier lot is NAKED and must still be flattened."""

    def test_coverage_is_checked_against_the_whole_position(self):
        pos = SimpleNamespace(qty="14", side="long")
        tgt = {"AAPL": {"symbol": "AAPL", "side": "long", "qty": 4, "entry_price": 335.0, "trade_id": "DT-A",
                        "order_id": "O1"}}
        orders = [SimpleNamespace(side="sell", qty=10, filled_qty=0, order_type="stop", client_order_id="IN-AAPL-s-1-x")]
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value=tgt), \
                mock.patch.object(dtm, "_load_state", return_value={}), \
                mock.patch.object(dtm, "_save_state", return_value=True), \
                mock.patch("execution.broker.get_open_position", return_value=pos), \
                mock.patch("execution.broker.get_open_orders", return_value=orders), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False), \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False), \
                mock.patch.object(dtm, "_page"), \
                mock.patch.object(dtm, "flatten_position", return_value=True) as flat:
            dtm.reconcile_open_state()
        flat.assert_called_once()                       # our 4 shares are closed, not mislabelled protected
        self.assertEqual(flat.call_args.args[1], 4)


if __name__ == "__main__":
    unittest.main()
