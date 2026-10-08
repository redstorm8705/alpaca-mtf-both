#!/usr/bin/env python3
# ruff: noqa: E501
"""CEO 2026-10-07 close sequence for the day tier:
- 3:40 no new entries; 3:58 cancel the stop, RETRYING until Alpaca confirms (the stop protects the lot until then),
  then a market exit the moment it confirms, remainder retried until just before the close;
- after the close, any lot still open gets an extended-hours GTC LIMIT at the bid (sell) / ask (buy), re-priced
  each tick until filled — never a market order after hours (Alpaca would queue it for the next open)."""
import unittest
from types import SimpleNamespace
from unittest import mock

import config
from execution import day_trade_manager as dtm

LONG = {"symbol": "EWY", "side": "long", "qty": 4, "entry_price": 180.0, "trade_id": "DT-L", "order_id": "O1"}
SHORT = {"symbol": "AAPL", "side": "short", "qty": 3, "entry_price": 250.0, "trade_id": "DT-S", "order_id": "O2"}


def _pos(side, qty, px=181.0):
    return SimpleNamespace(side=side, qty=qty, current_price=px)


class EodSubmitDeadline(unittest.TestCase):
    def test_stop_filled_during_cancel_is_booked_not_resold(self):
        """cold-2nd: the confirmed-terminal stop FILLED during the 3:58 cancel -> book it, never market-sell again."""
        with mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch("execution.broker.get_open_position", return_value=_pos("long", 10)), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=True), \
                mock.patch.object(dtm, "_eod_stop_cleared", return_value=True), \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=True), \
                mock.patch.object(dtm, "_retire_trade_record") as rt, \
                mock.patch.object(dtm, "flatten_position") as fp:
            self.assertEqual(dtm._force_flat_one(dict(LONG), "eod_force_flat", True), (True, False))
        fp.assert_not_called()
        rt.assert_called_once_with("DT-L")

    def test_market_attempts_capped_at_three(self):
        tries: dict = {}
        with mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch("execution.broker.get_open_position", return_value=_pos("long", 4)), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=True), \
                mock.patch.object(dtm, "_eod_stop_cleared", return_value=True), \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False), \
                mock.patch.object(dtm, "_flatten_targets", return_value={"EWY": dict(LONG)}), \
                mock.patch.object(dtm, "flatten_position", return_value=False) as fp:
            for _ in range(5):
                dtm._force_flat_one(dict(LONG), "eod_force_flat", True, tries=tries)
        self.assertEqual(fp.call_count, 3)

    def test_no_market_order_at_or_after_submit_by(self):
        """cold-2nd F2: a lot reached after the last safe moment is left to the after-hours exit — no market order."""
        with mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False), \
                mock.patch.object(dtm, "_flatten_targets", return_value={"EWY": dict(LONG)}), \
                mock.patch("execution.broker.get_open_position", return_value=_pos("long", 4)), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=True), \
                mock.patch.object(dtm, "_eod_stop_cleared", return_value=True), \
                mock.patch.object(dtm, "flatten_position") as fp, \
                mock.patch.object(dtm.time, "monotonic", return_value=100.0):
            done, flat = dtm._force_flat_one(dict(LONG), "eod_force_flat", True, submit_by=100.0)
        fp.assert_not_called()
        self.assertEqual((done, flat), (True, False))

    def test_closed_in_log_is_retired(self):
        with mock.patch.object(dtm, "_closed_in_log", return_value=True), \
                mock.patch.object(dtm, "_retire_trade_record") as mf, \
                mock.patch.object(dtm, "flatten_position") as fp:
            self.assertEqual(dtm._force_flat_one(dict(LONG), "eod_force_flat", True), (True, False))
        fp.assert_not_called()
        mf.assert_called_once_with("DT-L")


class EodExitRetries(unittest.TestCase):
    def _run(self, cleared_seq, flatten_ok=True, passes_allowed=5):
        clock = {"t": 0.0}

        def _mono():
            return clock["t"]

        def _sleep(s):
            clock["t"] += s
        targets = {"EWY": dict(LONG)}
        flat_calls = []

        def _flatten(sym, qty, side, **kw):
            flat_calls.append((sym, qty, side, kw.get("reason")))
            if flatten_ok:
                targets.clear()
            return flatten_ok
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch.object(dtm, "_retire_trade_record"), \
                mock.patch.object(dtm, "_flatten_targets", side_effect=lambda: dict(targets)), \
                mock.patch("execution.broker.get_open_position", return_value=_pos("long", 4)), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False), \
                mock.patch.object(dtm, "_foreign_stop_covers", return_value=False), \
                mock.patch.object(dtm, "_eod_stop_cleared", side_effect=list(cleared_seq)) as clr, \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False), \
                mock.patch.object(dtm, "flatten_position", side_effect=_flatten), \
                mock.patch.object(dtm, "_page") as pg, \
                mock.patch.object(dtm.time, "monotonic", side_effect=_mono), \
                mock.patch.object(dtm.time, "sleep", side_effect=_sleep):
            n = dtm.force_flat_all(reason="eod_force_flat", deadline=float(passes_allowed))
        return n, flat_calls, clr, pg

    def test_no_market_exit_until_cancel_confirmed(self):
        n, flat, clr, pg = self._run([False, False, True])
        self.assertEqual(n, 1)
        self.assertEqual(clr.call_count, 3)            # retried the cancel twice; the stop protected the lot meanwhile
        self.assertEqual(flat, [("EWY", 4, "long", "eod_force_flat")])
        pg.assert_not_called()

    def test_never_confirmed_never_market_and_pages(self):
        n, flat, clr, pg = self._run([False] * 20, passes_allowed=3)
        self.assertEqual(n, 0)
        self.assertEqual(flat, [])                     # never sent a market exit while the stop could still fire
        self.assertIn("after-hours exit", pg.call_args.args[0])

    def test_failed_close_is_retried(self):
        targets_calls = {"n": 0}
        clock = {"t": 0.0}
        res = iter([False, True])

        def _flatten(*a, **k):
            ok = next(res)
            if ok:
                targets_calls["n"] = 1
            return ok
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch.object(dtm, "_retire_trade_record"), \
                mock.patch.object(dtm, "_flatten_targets", side_effect=lambda: {} if targets_calls["n"] else {"EWY": dict(LONG)}), \
                mock.patch("execution.broker.get_open_position", return_value=_pos("long", 4)), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False), \
                mock.patch.object(dtm, "_foreign_stop_covers", return_value=False), \
                mock.patch.object(dtm, "_eod_stop_cleared", return_value=True), \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False), \
                mock.patch.object(dtm, "flatten_position", side_effect=_flatten) as fp, \
                mock.patch.object(dtm, "_page"), \
                mock.patch.object(dtm.time, "monotonic", side_effect=lambda: clock["t"]), \
                mock.patch.object(dtm.time, "sleep", side_effect=lambda s: clock.__setitem__("t", clock["t"] + s)):
            n = dtm.force_flat_all(reason="eod_force_flat", deadline=10.0)
        self.assertEqual((n, fp.call_count), (1, 2))

    def test_no_deadline_is_one_pass_without_confirm(self):
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_closed_in_log", return_value=False), \
                mock.patch.object(dtm, "_flatten_targets", return_value={"EWY": dict(LONG)}), \
                mock.patch("execution.broker.get_open_position", return_value=_pos("long", 4)), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=True), \
                mock.patch.object(dtm, "_foreign_stop_covers", return_value=False), \
                mock.patch.object(dtm, "_eod_stop_cleared") as clr, \
                mock.patch.object(dtm, "flatten_position", return_value=False) as fp, \
                mock.patch.object(dtm.time, "sleep") as sl:
            dtm.force_flat_all(reason="tier_kill")
        clr.assert_not_called()
        self.assertEqual(fp.call_count, 1)
        sl.assert_not_called()


class AfterHoursExit(unittest.TestCase):
    def _env(self, tgt, pos, quote, state_rec=None, cleared=True, orders=None):
        self.submits = []
        self.pending = []
        ord_by = dict(orders or {})

        def _submit(sym, qty, side, px, **kw):
            self.submits.append((sym, qty, side, px, kw))
            return SimpleNamespace(id=f"AH{len(self.submits)}")
        st = {"entry::X::b": dict(state_rec or {"coid": tgt["trade_id"], "symbol": tgt["symbol"]})}
        return [
            mock.patch.object(dtm, "_enabled", return_value=True),
            mock.patch.object(dtm, "_flatten_targets", return_value={tgt["symbol"]: dict(tgt)}),
            mock.patch.object(dtm, "_load_state", return_value=st),
            mock.patch("execution.broker.get_open_position", return_value=pos),
            mock.patch("execution.broker.get_order", side_effect=lambda oid: ord_by.get(oid)),
            mock.patch("execution.broker.cancel_order", return_value=True),
            mock.patch("execution.broker.submit_limit_order", side_effect=_submit),
            mock.patch("execution.broker.partial_close_position") ,
            mock.patch("data.alpaca_data.get_latest_quote", return_value=quote),
            mock.patch("data.alpaca_data.get_latest_trade", return_value=None),
            mock.patch.object(dtm, "_eod_stop_cleared", return_value=cleared),
            mock.patch.object(dtm, "_set_pending_exit", side_effect=lambda t, o, q, kind="", reprices=0: self.pending.append((t, o, q, kind)) or True),
            mock.patch.object(dtm, "_bump_pending_ticks"),
            mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False),
            mock.patch.object(dtm, "_foreign_stop_covers", return_value=False),
            mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False),
            mock.patch.object(dtm, "_closed_in_log", return_value=False),
            mock.patch.object(dtm, "_page_once_today"),
            mock.patch.object(dtm.time, "sleep"),
        ]

    def _go(self, patches):
        from contextlib import ExitStack
        with ExitStack() as es:
            ms = [es.enter_context(p) for p in patches]
            out = dtm.after_hours_exit()
        return out, ms

    def test_long_sells_at_the_bid_gtc_extended(self):
        out, ms = self._go(self._env(LONG, _pos("long", 4), {"bid": 179.10, "ask": 179.40}))
        self.assertEqual(out["placed"], 1)
        sym, qty, side, px, kw = self.submits[0]
        self.assertEqual((sym, qty, side, px), ("EWY", 4, "sell", 179.10))
        self.assertEqual((kw["extended_hours"], kw["time_in_force"], kw["tier"]), (True, "gtc", "daytrade"))
        self.assertEqual(self.pending, [("DT-L", "AH1", 4, "after_hours")])
        ms[7].assert_not_called()                    # never a market order after hours

    def test_short_buys_at_the_ask(self):
        out, _ = self._go(self._env(SHORT, _pos("short", 3), {"bid": 251.0, "ask": 251.3}))
        self.assertEqual(self.submits[0][:4], ("AAPL", 3, "buy", 251.3))

    def test_stop_still_live_waits(self):
        out, _ = self._go(self._env(LONG, _pos("long", 4), {"bid": 179.1, "ask": 179.4}, cleared=False))
        self.assertEqual((out["placed"], out["waiting"]), (0, 1))
        self.assertEqual(self.submits, [])

    def test_opposite_net_refused(self):
        out, _ = self._go(self._env(LONG, _pos("short", 4), {"bid": 179.1, "ask": 179.4}))
        self.assertEqual((out["placed"], out["skipped"]), (0, 1))

    def test_no_quote_uses_trade_minus_fallback(self):
        ps = self._env(LONG, _pos("long", 4), {})
        ps[9] = mock.patch("data.alpaca_data.get_latest_trade", return_value=180.0)
        self._go(ps)
        self.assertEqual(self.submits[0][3], round(180.0 * (1 - config.DAYTRADE_AH_FALLBACK_PCT), 2))

    def test_closed_trade_is_retired_not_retargeted(self):
        """cold-2nd F1: the 3:58 market exit booked the lot; at 4:00 the state record still says protected and the
        symbol is co-held (swing tier long 10). The after-hours exit must NOT sell the swing tier's shares."""
        ps = self._env(LONG, _pos("long", 10), {"bid": 179.1, "ask": 179.4})
        ps = [p for p in ps if getattr(p, "attribute", None) != "_closed_in_log"]
        ps.append(mock.patch.object(dtm, "_closed_in_log", return_value=True))
        ps.append(mock.patch.object(dtm, "_retire_trade_record"))
        out, ms = self._go(ps)
        self.assertEqual(self.submits, [])
        ms[-1].assert_called_once_with("DT-L")

    def test_lot_protected_by_another_tiers_stop_gets_no_exit(self):
        ps = self._env(LONG, _pos("long", 4), {"bid": 179.1, "ask": 179.4})
        ps = [p for p in ps if getattr(p, "attribute", None) != "_foreign_stop_covers"]
        ps += [mock.patch.object(dtm, "_foreign_stop_covers", return_value=True),
               mock.patch.object(dtm, "_record_transfer", return_value=False)]
        out, _ = self._go(ps)
        self.assertEqual(self.submits, [])
        self.assertEqual(out["skipped"], 1)

    def test_swing_takeover_after_close_is_booked_as_transfer(self):
        ps = self._env(LONG, _pos("long", 4), {"bid": 179.1, "ask": 179.4})
        ps = [p for p in ps if getattr(p, "attribute", None) != "_foreign_stop_covers"]
        ps += [mock.patch.object(dtm, "_foreign_stop_covers", return_value=True),
               mock.patch.object(dtm, "_record_transfer", return_value=True)]
        out, _ = self._go(ps)
        self.assertEqual(self.submits, [])
        self.assertEqual(out["closed"], 1)

    def test_stop_filled_before_close_on_cohold_never_sells_other_tier(self):
        """cold-2nd rev3: swing long 5 + day long 10; the day stop filled 10 at 3:59:55. At 4:00 the exit must book the
        stop and place NOTHING (the 5 shares left are the swing tier's)."""
        ps = self._env(LONG, _pos("long", 5), {"bid": 179.1, "ask": 179.4})
        ps = [p for p in ps if getattr(p, "attribute", None) != "_record_confirmed_stop_exit"]
        ps.append(mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=True))
        ps.append(mock.patch.object(dtm, "_retire_trade_record"))
        out, ms = self._go(ps)
        self.assertEqual(self.submits, [])
        ms[-1].assert_called_once_with("DT-L")

    def test_closed_trade_with_open_exit_order_cancels_before_retire(self):
        state = {"coid": "DT-L", "symbol": "EWY", "pending_exit_order_id": "AH0", "pending_exit_kind": "after_hours"}
        seq = iter([SimpleNamespace(status="new"), SimpleNamespace(status="canceled")])
        ps = self._env(LONG, _pos("long", 5), {"bid": 179.1, "ask": 179.4}, state_rec=state)
        ps[4] = mock.patch("execution.broker.get_order", side_effect=lambda oid: next(seq))
        ps = [p for p in ps if getattr(p, "attribute", None) != "_closed_in_log"]
        ps += [mock.patch.object(dtm, "_closed_in_log", return_value=True),
               mock.patch.object(dtm, "_clear_pending_exit"),
               mock.patch.object(dtm, "_retire_trade_record")]
        out, ms = self._go(ps)
        ms[5].assert_called_once_with("AH0")
        ms[-1].assert_called_once_with("DT-L")
        self.assertEqual(self.submits, [])

    def test_stale_rth_market_order_replaced_after_close(self):
        """board Taleb: a regular-hours market close still open after 4:00 (queued for the open) is cancelled and
        replaced by the after-hours limit."""
        state = {"coid": "DT-L", "symbol": "EWY", "pending_exit_order_id": "MK0", "pending_exit_accounted_qty": 0.0,
                 "pending_exit_kind": ""}
        seq = iter([SimpleNamespace(filled_qty=0, filled_avg_price=0, status="accepted", limit_price=None),
                    SimpleNamespace(filled_qty=0, filled_avg_price=0, status="accepted", limit_price=None),
                    SimpleNamespace(filled_qty=0, filled_avg_price=0, status="canceled", limit_price=None)])
        ps = self._env(LONG, _pos("long", 4), {"bid": 179.0, "ask": 179.3}, state_rec=state)
        ps[4] = mock.patch("execution.broker.get_order", side_effect=lambda oid: next(seq))
        with mock.patch.object(dtm, "_clear_pending_exit"):
            out, ms = self._go(ps)
        ms[5].assert_called_once_with("MK0")
        self.assertEqual(self.submits[0][2], "sell")
        self.assertEqual(self.submits[0][4]["time_in_force"], "gtc")

    def test_unfilled_two_ticks_reprices_through_the_touch(self):
        state = {"coid": "DT-L", "symbol": "EWY", "pending_exit_order_id": "AH0", "pending_exit_accounted_qty": 0.0,
                 "pending_exit_kind": "after_hours", "pending_exit_open_ticks": 1, "pending_exit_reprices": 0}
        seq = iter([SimpleNamespace(filled_qty=0, filled_avg_price=0, status="new", limit_price=179.0),
                    SimpleNamespace(filled_qty=0, filled_avg_price=0, status="new", limit_price=179.0),
                    SimpleNamespace(filled_qty=0, filled_avg_price=0, status="canceled", limit_price=179.0)])
        ps = self._env(LONG, _pos("long", 4), {"bid": 179.0, "ask": 179.3}, state_rec=state)
        ps[4] = mock.patch("execution.broker.get_order", side_effect=lambda oid: next(seq))
        with mock.patch.object(dtm, "_clear_pending_exit"):
            out, ms = self._go(ps)
        self.assertEqual(out["repriced"], 1)
        self.assertEqual(self.submits[0][3], round(179.0 * (1 - 0.005), 2))   # 0.5% through the bid

    def test_open_order_repriced_when_bid_falls(self):
        state = {"coid": "DT-L", "symbol": "EWY", "pending_exit_order_id": "AH0", "pending_exit_accounted_qty": 0.0,
                 "pending_exit_kind": "after_hours"}
        seq = iter([SimpleNamespace(filled_qty=0, filled_avg_price=0, status="new", limit_price=180.0),   # account
                    SimpleNamespace(filled_qty=0, filled_avg_price=0, status="new", limit_price=180.0),   # limit read
                    SimpleNamespace(filled_qty=0, filled_avg_price=0, status="canceled", limit_price=180.0)])
        ps = self._env(LONG, _pos("long", 4), {"bid": 179.0, "ask": 179.3}, state_rec=state)
        ps[4] = mock.patch("execution.broker.get_order", side_effect=lambda oid: next(seq))
        with mock.patch.object(dtm, "_clear_pending_exit"):
            out, ms = self._go(ps)
        ms[5].assert_called_once_with("AH0")         # old order cancelled first
        self.assertEqual(out["repriced"], 1)
        self.assertEqual(self.submits[0][:4], ("EWY", 4, "sell", 179.0))

    def test_open_order_at_the_bid_waits(self):
        state = {"coid": "DT-L", "symbol": "EWY", "pending_exit_order_id": "AH0", "pending_exit_accounted_qty": 0.0,
                 "pending_exit_kind": "after_hours"}
        o = SimpleNamespace(filled_qty=0, filled_avg_price=0, status="new", limit_price=179.0)
        out, ms = self._go(self._env(LONG, _pos("long", 4), {"bid": 179.2, "ask": 179.4}, state_rec=state,
                                     orders={"AH0": o}))
        self.assertEqual(out["waiting"], 1)
        ms[5].assert_not_called()
        self.assertEqual(self.submits, [])

    def test_full_fill_books_exit_and_retires(self):
        state = {"coid": "DT-L", "symbol": "EWY", "pending_exit_order_id": "AH0", "pending_exit_accounted_qty": 0.0,
                 "pending_exit_kind": "after_hours"}
        o = SimpleNamespace(filled_qty=4, filled_avg_price=179.0, status="filled", limit_price=179.0)
        ps = self._env(LONG, _pos("long", 4), {"bid": 179.2, "ask": 179.4}, state_rec=state, orders={"AH0": o})
        with mock.patch("strategy.day_tier_logger.log_exit_fill", return_value=True) as lx, \
                mock.patch("trade_logger.log_event"), \
                mock.patch.object(dtm, "_clear_pending_exit"), \
                mock.patch.object(dtm, "_retire_trade_record") as mf:
            out, _ = self._go(ps)
        self.assertEqual(out["closed"], 1)
        kw = lx.call_args.kwargs
        self.assertEqual((kw["exit_reason"], kw["fill_qty"], kw["realized_pnl"]), ("eod_after_hours_exit", 4.0, round((179.0 - 180.0) * 4, 2)))
        mf.assert_called_once_with("DT-L")
        self.assertEqual(self.submits, [])


class AfterHoursPendingOnly(unittest.TestCase):
    _env = AfterHoursExit._env
    _go = AfterHoursExit._go

    def test_rth_only_pending_ignores_lots_without_an_ah_exit(self):
        ps = self._env(LONG, _pos("long", 4), {"bid": 179.1, "ask": 179.4})
        from contextlib import ExitStack
        with ExitStack() as es:
            ms = [es.enter_context(p) for p in ps]
            out = dtm.after_hours_exit(only_pending=True)
        self.assertEqual((out["checked"], self.submits), (0, []))
        ms[10].assert_not_called()                   # never touches a live lot's stop in regular hours
        ms[3].assert_not_called()                    # no position read for lots it does not manage

    def test_flat_lot_cancels_its_open_ah_order(self):
        state = {"coid": "DT-L", "symbol": "EWY", "pending_exit_order_id": "AH0", "pending_exit_accounted_qty": 0.0,
                 "pending_exit_kind": "after_hours"}
        o = SimpleNamespace(filled_qty=0, filled_avg_price=0, status="new", limit_price=179.0)
        out, ms = self._go(self._env(LONG, None, {"bid": 179.2, "ask": 179.4}, state_rec=state, orders={"AH0": o}))
        ms[5].assert_called_once_with("AH0")
        self.assertEqual(self.submits, [])


class ReconcileClosedMarket(unittest.TestCase):
    def test_naked_lot_not_market_closed_when_closed(self):
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value={"EWY": dict(LONG)}), \
                mock.patch.object(dtm, "_load_state", return_value={}), \
                mock.patch("execution.broker.get_open_position", return_value=_pos("long", 4)), \
                mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False), \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=False), \
                mock.patch.object(dtm, "_foreign_stop_covers", return_value=False), \
                mock.patch.object(dtm, "flatten_position") as fp:
            out = dtm.reconcile_open_state(allow_flatten=False)
        fp.assert_not_called()
        self.assertEqual(out.get("deferred_after_hours"), 1)

    def test_flat_lot_with_recorded_exit_order_is_cleared(self):
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value={"EWY": dict(LONG)}), \
                mock.patch.object(dtm, "_load_state", return_value={}), \
                mock.patch("execution.broker.get_open_position", return_value=None), \
                mock.patch.object(dtm, "_resolve_pending_exit", return_value=True), \
                mock.patch.object(dtm, "_halt_unresolved_exit") as h:
            out = dtm.reconcile_open_state(allow_flatten=False)
        h.assert_not_called()
        self.assertEqual(out["cleared"], 1)


class RunnerPhases(unittest.TestCase):
    def _tick(self, clock, mins):
        import run_day_tier as rdt
        acct = SimpleNamespace(equity=2500.0, last_equity=2500.0, buying_power=3000.0)
        with mock.patch.object(config, "DAYTRADE_ENABLED", True), \
                mock.patch.object(rdt, "_clock_state", return_value=(clock, mins)), \
                mock.patch("execution.broker.get_account", return_value=acct), \
                mock.patch.object(dtm, "after_hours_exit", return_value={"placed": 1}) as ah, \
                mock.patch.object(dtm, "reconcile_open_state", return_value={}) as rc, \
                mock.patch.object(dtm, "force_flat_all", return_value=0) as ff, \
                mock.patch.object(dtm, "tier_kill_check", return_value=False), \
                mock.patch.object(rdt, "_touch_heartbeat"):
            out = rdt.run_tick()
        return out, ah, rc, ff

    def test_closed_runs_after_hours_exit_then_non_flattening_reconcile(self):
        out, ah, rc, ff = self._tick("closed", None)
        ah.assert_called_once()
        rc.assert_called_once_with(allow_flatten=False)
        ff.assert_not_called()
        self.assertEqual(out["skipped"], "market_closed")

    def test_unknown_clock_after_hours_takes_the_after_hours_path(self):
        import run_day_tier as rdt
        with mock.patch.object(rdt, "_host_in_rth", return_value=False):
            out, ah, rc, ff = self._tick("unknown", None)
        ah.assert_called_once_with()
        rc.assert_called_once_with(allow_flatten=False)
        ff.assert_not_called()

    def test_unknown_clock_in_hours_keeps_fail_closed_flatten(self):
        import run_day_tier as rdt
        with mock.patch.object(rdt, "_host_in_rth", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value={"EWY": dict(LONG)}):
            out, ah, rc, ff = self._tick("unknown", None)
        ff.assert_called_once()

    def test_host_rth_window(self):
        import run_day_tier as rdt
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        self.assertTrue(rdt._host_in_rth(datetime(2026, 10, 8, 15, 59, tzinfo=et)))
        self.assertFalse(rdt._host_in_rth(datetime(2026, 10, 8, 16, 0, tzinfo=et)))
        self.assertFalse(rdt._host_in_rth(datetime(2026, 10, 8, 9, 29, tzinfo=et)))
        self.assertFalse(rdt._host_in_rth(datetime(2026, 10, 10, 12, 0, tzinfo=et)))   # Saturday

    def test_last_two_seconds_no_flatten_in_reconcile(self):
        out, ah, rc, ff = self._tick("open", 1.5 / 60.0)
        rc.assert_called_once_with(allow_flatten=False)

    def test_entry_cutoff_window_no_entries_no_exit(self):
        out, ah, rc, ff = self._tick("open", 10.0)
        self.assertEqual(out["phase"], "eod_no_entries")
        ff.assert_not_called()
        ah.assert_called_once_with(only_pending=True)   # regular hours: only an existing after-hours exit is managed

    def test_slightly_early_tick_waits_for_the_window(self):
        import run_day_tier as rdt
        with mock.patch.object(rdt.time, "sleep") as sl:
            out, _, _, ff = self._tick("open", 2.2)
        self.assertEqual(out["phase"], "force_flat")
        self.assertAlmostEqual(sl.call_args.args[0], 12.0, delta=0.5)   # waited until ~exactly 2.0 min to the close
        ff.assert_called_once()

    def test_eod_exit_window_passes_a_deadline(self):
        out, _, _, ff = self._tick("open", 1.9)
        self.assertEqual(out["phase"], "force_flat")
        self.assertIsNotNone(ff.call_args.kwargs["deadline"])


class RetireOnlyThisTrade(unittest.TestCase):
    def test_other_lot_on_same_symbol_untouched(self):
        st = {"entry::EWY::a": {"coid": "DT-OLD", "symbol": "EWY", "state": "protected"},
              "entry::EWY::b": {"coid": "DT-NEW", "symbol": "EWY", "state": "submitted"}}
        saved = {}
        with mock.patch.object(dtm, "_load_state", return_value=st), \
                mock.patch.object(dtm, "_save_state", side_effect=lambda d: saved.update(d) or True):
            dtm._retire_trade_record("DT-OLD")
        self.assertEqual(saved["entry::EWY::a"]["state"], "flattened_no_stop")
        self.assertEqual(saved["entry::EWY::b"]["state"], "submitted")


class IncrementPrice(unittest.TestCase):
    def test_split_fill_books_each_piece_at_its_own_price(self):
        # 4 @ 100 booked, then the order shows 10 filled at a running average of 99.40 -> the 6 new are @ 99.00
        self.assertAlmostEqual(dtm._increment_price(10, 99.40, 4, 400.0, 6), 99.0, places=4)

    def test_first_booking_uses_the_average(self):
        self.assertEqual(dtm._increment_price(4, 100.0, 0, 0.0, 4), 100.0)

    def test_legacy_record_without_value_falls_back_to_average(self):
        self.assertEqual(dtm._increment_price(10, 99.4, 4, None, 6), 99.4)

    def test_bad_inputs_fall_back_to_average(self):
        self.assertEqual(dtm._increment_price(10, 99.4, 4, 5000.0, 6), 99.4)   # negative -> fallback


class ConfigValues(unittest.TestCase):
    def test_close_timing(self):
        self.assertEqual((config.DAYTRADE_FORCE_FLAT_MINUTES, config.DAYTRADE_ENTRY_CUTOFF_MINUTES), (2, 20))
        self.assertLess(config.DAYTRADE_EOD_EXIT_GUARD_S, config.DAYTRADE_FORCE_FLAT_MINUTES * 60)


if __name__ == "__main__":
    unittest.main()
