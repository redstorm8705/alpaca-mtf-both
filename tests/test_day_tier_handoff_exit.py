#!/usr/bin/env python3
# ruff: noqa: E501
"""2026-10-07: the swing tier (intraday tag) took over day-tier EWY/AAPL (its stops now protect them). When such a
lot is the WHOLE position, the day-tier trade ends at the take-over: an exit at the live Alpaca mark,
'transferred_to_swing_tier', realized P&L from that mark (a loss is booked, never masked), and the lot is retired —
so the day tier never halts later when the swing tier closes it, and no later fill has to be matched (two cold-2nd
FAILs: another tier's / a later trade's fill could be mis-booked). Co-held symbols / non-swing adopters keep the
protected (later halt) path."""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from execution import day_trade_manager as dtm

AAPL = {"symbol": "AAPL", "side": "long", "qty": 4, "entry_price": 335.24, "trade_id": "DT-A", "order_id": "O1"}
EWY = {"symbol": "EWY", "side": "short", "qty": 1, "entry_price": 181.46, "trade_id": "DT-E", "order_id": "O2"}


def _swing_stop(side, qty):
    return SimpleNamespace(id="X", side=side, qty=qty, filled_qty=0, order_type="stop", client_order_id="IN-S-s-1-x")


class RecordTransfer(unittest.TestCase):
    def _t(self, tgt, tiers, held, want, mark, log_ok=True, already=False):
        with mock.patch.object(dtm, "_durable_exit_recorded", return_value=already), \
                mock.patch("strategy.day_tier_logger.log_exit_fill", return_value=log_ok) as log, \
                mock.patch.object(dtm, "_mark_symbol_flattened") as fl, \
                mock.patch.object(dtm, "_page_once_today") as pg:
            ok = dtm._record_transfer(tgt, tiers, held, want, mark)
        return ok, log, fl, pg

    def test_long_loss_booked_at_the_mark(self):
        ok, log, fl, pg = self._t(AAPL, ["intraday"], 4, 4, 330.0)
        self.assertTrue(ok)
        kw = log.call_args.kwargs
        self.assertEqual((kw["exit_reason"], kw["fill_qty"], kw["fill_price"]), ("transferred_to_swing_tier", 4.0, 330.0))
        self.assertEqual(kw["realized_pnl"], round((330.0 - 335.24) * 4, 2))       # -20.96: a loss, not masked
        fl.assert_called_once_with("AAPL")
        self.assertIn("Swing tier", pg.call_args.args[2])   # the four tier names (CEO 2026-10-07)

    def test_short_sign(self):
        ok, log, _, _ = self._t(EWY, ["intraday"], 1, 1, 184.0)
        self.assertTrue(ok)
        self.assertEqual(log.call_args.kwargs["realized_pnl"], round(181.46 - 184.0, 2))

    def test_no_transfer_cases(self):
        for tiers, held, want, mark in ((["intraday", "qhm"], 6, 4, 330.0),   # co-held
                                         (["qhm"], 4, 4, 330.0),               # not the swing tier
                                         (["intraday"], 4, 4, 0.0),            # no usable mark
                                         (["intraday"], 4, 4, float("nan")),
                                         (["intraday"], 0, 0, 330.0)):
            ok, log, fl, _ = self._t(AAPL, tiers, held, want, mark)
            self.assertFalse(ok, (tiers, held, want, mark))
            log.assert_not_called()
            fl.assert_not_called()

    def test_already_booked_only_retries_the_retire(self):
        ok, log, fl, _ = self._t(AAPL, ["intraday"], 4, 4, 330.0, already=True)
        self.assertTrue(ok)
        log.assert_not_called()                 # never a second exit_fill (cold-2nd 2026-10-07)
        fl.assert_called_once_with("AAPL")

    def test_failed_log_write_does_not_retire(self):
        ok, _, fl, _ = self._t(AAPL, ["intraday"], 4, 4, 330.0, log_ok=False)
        self.assertFalse(ok)
        fl.assert_not_called()


class OneExitAcrossRepeatedCalls(unittest.TestCase):
    """Real journal semantics: the retire save fails, the transfer runs again — still exactly one exit_fill."""

    def test_repeat(self):
        journal: list = []

        def _log(trade_id, sym, **kw):
            journal.append({"event": "exit_fill", "trade_id": trade_id, **kw})
            return True
        with mock.patch("strategy.day_tier_logger.log_exit_fill", side_effect=_log), \
                mock.patch("strategy.day_tier_logger.read_events_checked",
                           side_effect=lambda tid=None: ([{"event": "entry_fill", "trade_id": "DT-A"}] + list(journal), True)), \
                mock.patch.object(dtm, "_mark_symbol_flattened"), \
                mock.patch.object(dtm, "_page_once_today"):
            for _ in range(3):
                self.assertTrue(dtm._record_transfer(AAPL, ["intraday"], 4, 4, 330.0))
        self.assertEqual(len(journal), 1)


class ReconcileAndForceFlatTransfer(unittest.TestCase):
    def _patches(self, pos, orders):
        return [mock.patch.object(dtm, "_enabled", return_value=True),
                mock.patch.object(dtm, "_flatten_targets", return_value={"AAPL": dict(AAPL)}),
                mock.patch.object(dtm, "_load_state", return_value={}),
                mock.patch.object(dtm, "_save_state", return_value=True),
                mock.patch("execution.broker.get_open_position", return_value=pos),
                mock.patch("execution.broker.get_open_orders", return_value=orders),
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False),
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False),
                mock.patch.object(dtm, "_page")]

    def _run(self, fn, pos_qty="4", stop_qty=4):
        pos = SimpleNamespace(qty=pos_qty, side="long", current_price="330.00")
        with ExitStack() as st:
            for p in self._patches(pos, [_swing_stop("sell", stop_qty)]):
                st.enter_context(p)
            tr = st.enter_context(mock.patch.object(dtm, "_record_transfer", return_value=True))
            flat = st.enter_context(mock.patch.object(dtm, "flatten_position", side_effect=AssertionError("no close")))
            out = fn()
        return out, tr, flat

    def test_reconcile_transfers_a_sole_swing_lot(self):
        out, tr, _ = self._run(dtm.reconcile_open_state)
        self.assertEqual(tr.call_args.args[1:], (["intraday"], 4, 4, 330.0))
        self.assertEqual(out["cleared"], 1)

    def test_force_flat_transfers_too(self):
        n, tr, _ = self._run(lambda: dtm.force_flat_all("eod_force_flat"))
        tr.assert_called_once()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
