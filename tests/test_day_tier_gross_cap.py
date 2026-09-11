#!/usr/bin/env python3
# ruff: noqa: E501
"""Risk-path tests for day-tier wire-time sizing and cumulative loss kill."""
from __future__ import annotations

import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import config
from execution import day_trade_manager as dtm
from execution import broker
import run_day_tier


class _Pos:
    def __init__(self, symbol, current_price, qty=1.0, market_value=None, side="long", unrealized_pl=0.0):
        self.symbol = symbol
        self.current_price = current_price
        self.qty = qty
        self.market_value = market_value if market_value is not None else qty * current_price
        self.side = side
        self.unrealized_pl = unrealized_pl


class _Order:
    def __init__(self, symbol, qty, price, side="buy", coid="DT-SPY-b-1-x", filled=0, otype="limit"):
        self.id = "o"
        self.symbol = symbol
        self.qty = qty
        self.filled_qty = filled
        self.limit_price = price
        self.stop_price = None
        self.side = side
        self.type = otype
        self.order_type = otype
        self.client_order_id = coid


def _bounded(requested=20, order_price=100.0, stop_price=98.0, equity=2500.0,
             positions=None, bp=10000.0, maint=1000.0, rate=0.30, orders=None, open_trades=None):
    pos_by = {p.symbol: p for p in (positions or [])}
    return dtm._bounded_entry_qty(requested, order_price, stop_price, equity, open_trades or {},
                                  pos_by, bp, maint, rate, orders or [])


class BoundedEntryQty(unittest.TestCase):
    def test_counts_every_tier_in_global_gross(self):
        qty, why = _bounded(requested=5, positions=[_Pos("LLY", 6000, market_value=6000)], maint=1000)
        self.assertEqual(qty, 2)
        self.assertIn("global_gross:$250.00", why)

    def test_maintenance_cushion_uses_account_maintenance_margin(self):
        qty, why = _bounded(requested=20, equity=2500, maint=1700, rate=0.30)
        self.assertEqual(qty, 5)
        self.assertIn("maintenance:$500.00", why)

    def test_clamps_high_conviction_instead_of_rejecting(self):
        qty, _ = _bounded(requested=20, maint=0, rate=0.30)
        self.assertEqual(qty, 15)

    def test_uses_actual_limit_price_at_boundary(self):
        qty, _ = _bounded(requested=15, order_price=100.20, maint=0, rate=0.30)
        self.assertEqual(qty, 14)

    def test_pending_increasing_orders_consume_room(self):
        pending = _Order("NVDA", 10, 100, side="buy")
        qty, why = _bounded(requested=10, orders=[pending], maint=0, rate=0.30)
        self.assertEqual(qty, 5)
        self.assertIn("day_gross:$500.00", why)

    def test_reducing_and_stop_orders_do_not_consume_entry_room(self):
        positions = [_Pos("NVDA", 100, qty=10, market_value=1000, side="long")]
        reducing = _Order("NVDA", 5, 110, side="sell")
        stop = _Order("NVDA", 10, 95, side="sell", otype="stop")
        qty, _ = _bounded(requested=5, positions=positions, orders=[reducing, stop], maint=0, rate=0.30)
        self.assertEqual(qty, 5)

    def test_oversized_reducing_order_counts_reversal_excess(self):
        positions = [_Pos("NVDA", 100, qty=5, market_value=500, side="long")]
        reversal = _Order("NVDA", 15, 100, side="sell", otype="stop")
        qty, why = _bounded(requested=10, positions=positions, orders=[reversal], maint=0, rate=0.30)
        self.assertEqual(qty, 5)
        self.assertIn("day_gross:$500.00", why)

    def test_stop_distance_risk_cap(self):
        qty, why = _bounded(requested=20, order_price=100, stop_price=80, maint=0, rate=0.30)
        self.assertEqual(qty, 2)
        self.assertIn("stop-risk cap=2sh", why)

    def test_bad_values_fail_closed(self):
        for bad in (float("nan"), float("inf"), 0.0, -1.0):
            qty, _ = _bounded(equity=bad)
            self.assertEqual(qty, 0)
        qty, _ = _bounded(rate=float("nan"))
        self.assertEqual(qty, 0)
        qty, _ = _bounded(positions=[_Pos("X", 1, market_value=float("nan"))])
        self.assertEqual(qty, 0)
        qty, _ = _bounded(orders=[_Order("X", float("nan"), 100)])
        self.assertEqual(qty, 0)


class TierKill(unittest.TestCase):
    def _run(self, cur_price, entry, qty, equity, realized_loss=0.0, readable=True):
        target = {"MSFT": {"symbol": "MSFT", "side": "long", "qty": qty, "entry_price": entry,
                           "trade_id": "t", "order_id": "o"}}
        pos = _Pos("MSFT", cur_price, qty=qty, unrealized_pl=(cur_price - entry) * qty)
        with mock.patch.object(dtm, "_enabled", return_value=True), \
             mock.patch.object(dtm, "_tier_killed_today", return_value=False), \
             mock.patch.object(dtm, "_realized_loss_today", return_value=(realized_loss, readable)), \
             mock.patch.object(dtm, "_flatten_targets", return_value=target), \
             mock.patch.object(dtm, "force_flat_all", return_value=1) as ff, \
             mock.patch.object(dtm, "_page"), \
             mock.patch.object(dtm, "_load_state", return_value={}), \
             mock.patch.object(dtm, "_save_state", return_value=True), \
             mock.patch("execution.broker.get_open_positions", return_value=[pos]):
            killed = dtm.tier_kill_check(equity, day_start_equity=2500.0)
        return killed, ff

    def test_cumulative_realized_loss_plus_open_loss_fires(self):
        killed, ff = self._run(96.0, 100.0, 10, 2475.0, realized_loss=-65.0)
        self.assertTrue(killed)
        ff.assert_called_once()

    def test_positive_exit_never_offsets_loss_floor(self):
        events = [
            {"event": "exit_fill", "ts": dtm.datetime.now(dtm.PT).isoformat(), "realized_pnl": 50},
            {"event": "exit_fill", "ts": dtm.datetime.now(dtm.PT).isoformat(), "realized_pnl": -30},
        ]
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(events, True)):
            pnl, readable = dtm._realized_loss_today()
        self.assertTrue(readable)
        self.assertEqual(pnl, -30.0)

    def test_unreadable_realized_log_halts_fail_closed_without_forced_flatten(self):
        killed, ff = self._run(100.0, 100.0, 10, 2500.0, readable=False)
        self.assertTrue(killed)
        ff.assert_not_called()

    def test_nonfinite_open_pnl_halts_fail_closed(self):
        killed, ff = self._run(float("nan"), 100.0, 10, 2500.0, realized_loss=-50.0)
        self.assertTrue(killed)
        ff.assert_not_called()

    def test_latched_kill_retries_residual_liquidation(self):
        with mock.patch.object(dtm, "_enabled", return_value=True), \
             mock.patch.object(dtm, "_load_state", return_value={dtm._KILL_KEY: f"{dtm._now_et():%Y%m%d}"}), \
             mock.patch.object(dtm, "_flatten_targets", return_value={"MSFT": {}}), \
             mock.patch.object(dtm, "force_flat_all", return_value=1) as ff:
            self.assertTrue(dtm.tier_kill_check(2500.0, day_start_equity=2500.0))
        ff.assert_called_once_with(reason="tier_kill_retry")


class MaintenanceRate(unittest.TestCase):
    def test_alpaca_percent_is_normalized(self):
        asset = type("Asset", (), {"maintenance_margin_requirement": "30"})()
        client = mock.Mock()
        client.get_asset.return_value = asset
        with mock.patch.object(broker, "_get_trading_client", return_value=client):
            self.assertEqual(broker.get_asset_maintenance_margin_rate("MSFT"), 0.30)

    def test_bad_rate_is_unknown(self):
        asset = type("Asset", (), {"maintenance_margin_requirement": "nan"})()
        client = mock.Mock()
        client.get_asset.return_value = asset
        with mock.patch.object(broker, "_get_trading_client", return_value=client):
            self.assertIsNone(broker.get_asset_maintenance_margin_rate("MSFT"))


class DurableLossRead(unittest.TestCase):
    def test_malformed_line_marks_risk_read_incomplete(self):
        from strategy import day_tier_logger
        with TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text('{"event":"exit_fill","realized_pnl":-120\n')
            with mock.patch.object(day_tier_logger, "_JSONL", path):
                events, complete = day_tier_logger.read_events_checked()
        self.assertEqual(events, [])
        self.assertFalse(complete)

    def test_partial_exit_reduces_restart_safe_open_quantity(self):
        from strategy import day_tier_logger
        rows = [
            {"event": "entry_fill", "trade_id": "DT-x", "symbol": "MSFT", "side": "long",
             "fill_price": 100, "fill_qty": 10, "ts": "2026-09-10T09:30:00-07:00"},
            {"event": "partial_exit_fill", "trade_id": "DT-x", "symbol": "MSFT",
             "fill_price": 90, "fill_qty": 4, "realized_pnl": -40,
             "ts": "2026-09-10T10:00:00-07:00"},
        ]
        with mock.patch.object(day_tier_logger, "read_events", return_value=rows):
            opened = day_tier_logger.open_trades_from_log()
        self.assertEqual(opened["DT-x"]["fill_qty"], 6.0)


class RunnerOrdering(unittest.TestCase):
    def test_account_failure_cannot_bypass_eod_flatten(self):
        with mock.patch.object(run_day_tier, "_clock_state", return_value=("open", 15.0)), \
             mock.patch.object(run_day_tier, "_touch_heartbeat"), \
             mock.patch("execution.broker.get_account", side_effect=RuntimeError("down")), \
             mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 1}) as recon, \
             mock.patch("execution.day_trade_manager.force_flat_all", return_value=1) as flat, \
             mock.patch.object(config, "DAYTRADE_ENABLED", True):
            result = run_day_tier.run_tick()
        self.assertEqual(result["phase"], "force_flat")
        recon.assert_called_once()
        flat.assert_called_once_with(reason="eod_force_flat")

    def test_account_halt_cannot_bypass_latched_tier_liquidation(self):
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="4000")
        with mock.patch.object(run_day_tier, "_clock_state", return_value=("open", 120.0)), \
             mock.patch.object(run_day_tier, "_touch_heartbeat"), \
             mock.patch("execution.broker.get_account", return_value=acct), \
             mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 1}), \
             mock.patch("execution.day_trade_manager.tier_kill_check", return_value=True) as tier_kill, \
             mock.patch("execution.day_trade_manager._account_entry_halt_reason") as account_halt, \
             mock.patch.object(config, "DAYTRADE_ENABLED", True):
            result = run_day_tier.run_tick()
        self.assertEqual(result["phase"], "tier_killed")
        tier_kill.assert_called_once_with(2500.0, day_start_equity=2500.0)
        account_halt.assert_not_called()

    def test_unknown_clock_reconciles_then_flattens_owned_tier_only(self):
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="4000")
        targets = {"MSFT": {"symbol": "MSFT", "qty": 2, "side": "long"}}
        with mock.patch.object(run_day_tier, "_clock_state", return_value=("unknown", None)), \
             mock.patch.object(run_day_tier, "_touch_heartbeat") as heartbeat, \
             mock.patch("execution.broker.get_account", return_value=acct), \
             mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 1}) as recon, \
             mock.patch("execution.day_trade_manager._flatten_targets", return_value=targets), \
             mock.patch("execution.day_trade_manager.force_flat_all", return_value=1) as flat, \
             mock.patch.object(config, "DAYTRADE_ENABLED", True):
            result = run_day_tier.run_tick()
        self.assertEqual(result["phase"], "clock_unknown")
        self.assertEqual(result["owned_targets"], 1)
        recon.assert_called_once()
        flat.assert_called_once_with(reason="clock_unavailable")
        heartbeat.assert_called_once_with("clock_unknown_force_flat")

    def test_unknown_clock_blocks_entries_without_phantom_flatten(self):
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="4000")
        with mock.patch.object(run_day_tier, "_clock_state", return_value=("unknown", None)), \
             mock.patch.object(run_day_tier, "_touch_heartbeat") as heartbeat, \
             mock.patch("execution.broker.get_account", return_value=acct), \
             mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 0}) as recon, \
             mock.patch("execution.day_trade_manager._flatten_targets", return_value={}), \
             mock.patch("execution.day_trade_manager.force_flat_all") as flat, \
             mock.patch.object(config, "DAYTRADE_ENABLED", True):
            result = run_day_tier.run_tick()
        self.assertEqual(result["phase"], "clock_unknown")
        self.assertEqual(result["owned_targets"], 0)
        recon.assert_called_once()
        flat.assert_not_called()
        heartbeat.assert_called_once_with("clock_unknown")


class ExitConfirmation(unittest.TestCase):
    def test_flatten_logs_broker_fill_not_submit_mark(self):
        pos = _Pos("MSFT", 95.0, qty=2, market_value=190.0, side="long")
        close_order = SimpleNamespace(id="close-1")
        with mock.patch("execution.broker.get_open_position", return_value=pos), \
             mock.patch("execution.broker.cancel_open_orders_for_symbol", return_value=1), \
             mock.patch("execution.broker.partial_close_position", return_value=close_order), \
             mock.patch.object(dtm, "_set_pending_exit", return_value=True), \
             mock.patch.object(dtm, "_clear_pending_exit"), \
             mock.patch.object(dtm, "_confirmed_order_fill", return_value=(True, 2.0, 90.0)), \
             mock.patch("strategy.day_tier_logger.log_exit_fill", return_value=True) as log_exit, \
             mock.patch("trade_logger.log_event"):
            self.assertTrue(dtm.flatten_position("MSFT", 2, "long", entry_price=100.0, trade_id="DT-x"))
        self.assertEqual(log_exit.call_args.kwargs["fill_price"], 90.0)
        self.assertEqual(log_exit.call_args.kwargs["realized_pnl"], -20.0)

    def test_partial_forced_close_is_persisted_before_retry(self):
        pos = _Pos("MSFT", 95.0, qty=110, market_value=10450.0, side="long")
        close_order = SimpleNamespace(id="close-1")
        with mock.patch("execution.broker.get_open_position", return_value=pos), \
             mock.patch("execution.broker.cancel_open_orders_for_symbol", return_value=1), \
             mock.patch("execution.broker.partial_close_position", return_value=close_order), \
             mock.patch.object(dtm, "_set_pending_exit", return_value=True), \
             mock.patch.object(dtm, "_confirmed_order_fill", return_value=(True, 4.0, 90.0)), \
             mock.patch.object(dtm, "_record_partial_exit", return_value=True) as partial, \
             mock.patch.object(dtm, "_page"):
            self.assertFalse(dtm.flatten_position("MSFT", 10, "long", entry_price=100.0,
                                                  trade_id="DT-x", reason="tier_kill"))
        self.assertEqual(partial.call_args.args[2], 4)

    def test_filled_stop_on_coheld_symbol_is_recorded_without_flattening_other_tier(self):
        target = {"symbol": "MSFT", "side": "long", "qty": 2, "entry_price": 100.0,
                  "trade_id": "DT-x", "order_id": "entry-1", "stop_order_id": "stop-1"}
        with mock.patch.object(dtm, "_enabled", return_value=True), \
             mock.patch.object(dtm, "_flatten_targets", return_value={"MSFT": target}), \
             mock.patch.object(dtm, "_load_state", return_value={}), \
             mock.patch("execution.broker.get_open_position", return_value=_Pos("MSFT", 90, qty=5)), \
             mock.patch.object(dtm, "_has_live_daytrade_stop", return_value=False), \
             mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=True), \
             mock.patch.object(dtm, "_mark_symbol_flattened") as marked, \
             mock.patch.object(dtm, "flatten_position") as flat:
            result = dtm.reconcile_open_state()
        self.assertEqual(result["cleared"], 1)
        marked.assert_called_once_with("MSFT")
        flat.assert_not_called()

    def test_unreadable_latest_stop_cannot_be_overridden_by_readable_old_stop(self):
        target = {"symbol": "MSFT", "side": "long", "qty": 2, "entry_price": 100.0,
                  "trade_id": "DT-x", "stop_order_id": "new-stop"}
        events = [{"event": "stop_placed", "stop_order_id": "old-stop"}]
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(events, True)), \
             mock.patch.object(dtm, "_confirmed_order_fill",
                               side_effect=[(False, 0.0, 0.0), (True, 0.0, 0.0)]):
            self.assertIsNone(dtm._record_confirmed_stop_exit(target))

    def test_cumulative_partial_stop_is_not_counted_twice(self):
        # The durable log already owns four shares of the order's cumulative fill; a later
        # broker read of the same four must leave the six-share residual untouched.
        target = {"symbol": "MSFT", "side": "long", "qty": 6, "entry_price": 100.0,
                  "trade_id": "DT-x", "stop_order_id": "stop-1"}
        events = [
            {"event": "stop_placed", "stop_order_id": "stop-1"},
            {"event": "partial_exit_fill", "order_id": "stop-1", "fill_qty": 4},
        ]
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(events, True)), \
             mock.patch.object(dtm, "_confirmed_order_fill", return_value=(True, 4.0, 90.0)), \
             mock.patch.object(dtm, "_record_partial_exit") as partial:
            self.assertFalse(dtm._record_confirmed_stop_exit(target))
        self.assertEqual(target["qty"], 6)
        partial.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
