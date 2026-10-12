import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import execution.forever_hold_manager as f6
import execution.ownership_guard as guard


def _ledger(*, f6_qty=0.0, net=0.0, drift=0.0):
    return {
        "version": 1,
        "positions": {
            "NVDA": {
                "alpaca_net_qty": net,
                "drift": drift,
                "tiers": {
                    "intraday": {"qty": net - f6_qty},
                    "qhm": {"qty": 0.0},
                    "forever6": {"qty": f6_qty},
                    "daytrade": {"qty": 0.0},
                },
            }
        },
    }


class F6StartupReconcileTests(unittest.TestCase):
    def setUp(self):
        f6._STARTUP_READY = False
        self.temp = tempfile.TemporaryDirectory()
        self.degraded = Path(self.temp.name) / "degraded.json"
        self.degraded_patch = mock.patch.object(f6, "_SYNC_DEGRADED", self.degraded)
        self.degraded_patch.start()
        self.addCleanup(self.degraded_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.broker = mock.Mock()
        self.broker.get_open_orders.return_value = []
        self.manager = f6.ForeverHoldManager(self.broker)

    def _run(self, ledger):
        with (
            mock.patch(
                "run_ledger_sync.sync_once",
                return_value={"ok": True, "healed": True},
            ),
            mock.patch.object(guard, "load_ledger", return_value=ledger),
            mock.patch.object(f6, "_slack_safe"),
        ):
            return self.manager.reconcile_on_startup()

    def test_clean_shared_symbol_claim_is_ready(self):
        result = self._run(_ledger(f6_qty=2, net=5, drift=0))
        self.assertTrue(result.ready)
        self.assertEqual(result.symbols_reconciled, ("NVDA",))
        self.assertFalse(f6._read_sync_degraded())

    def test_open_f6_order_blocks_and_persists_interlock(self):
        self.broker.get_open_orders.return_value = [
            SimpleNamespace(id="order-1", client_order_id="F6-NVDA-b-1-abc")
        ]
        result = self._run(_ledger())
        self.assertFalse(result.ready)
        self.assertEqual(result.pending_order_ids, ("order-1",))
        self.assertTrue(f6._read_sync_degraded())

    def test_other_tier_open_order_does_not_block(self):
        self.broker.get_open_orders.return_value = [
            SimpleNamespace(id="order-2", client_order_id="IN-NVDA-s-1-abc")
        ]
        self.assertTrue(self._run(_ledger()).ready)

    def test_replay_failure_blocks_without_reading_orders(self):
        with (
            mock.patch(
                "run_ledger_sync.sync_once",
                return_value={"ok": False, "error": "timeout"},
            ),
            mock.patch.object(f6, "_slack_safe"),
        ):
            result = self.manager.reconcile_on_startup()
        self.assertFalse(result.ready)
        self.assertIn("timeout", result.reason)
        self.broker.get_open_orders.assert_not_called()
        self.assertTrue(f6._read_sync_degraded())

    def test_drift_or_fractional_f6_claim_blocks(self):
        for ledger in (
            _ledger(f6_qty=1, net=2, drift=1),
            _ledger(f6_qty=1.5, net=2, drift=0),
            _ledger(f6_qty=3, net=2, drift=0),
        ):
            with self.subTest(ledger=ledger):
                self.assertFalse(self._run(ledger).ready)
                self.assertTrue(f6._read_sync_degraded())

    def test_unreadable_open_orders_blocks(self):
        self.broker.get_open_orders.return_value = None
        self.assertFalse(self._run(_ledger()).ready)
        self.assertTrue(f6._read_sync_degraded())

    def test_trim_is_blocked_while_startup_interlock_is_set(self):
        f6._write_sync_degraded(True, "test")
        with mock.patch.object(guard, "load_ledger") as load:
            self.assertEqual(self.manager.evaluate_and_execute_trims(), {})
        load.assert_not_called()

    def test_enabled_process_cannot_buy_before_preflight(self):
        plan = [{"symbol": "NVDA", "price": 100.0}]
        with mock.patch.object(f6.config, "FOREVER6_ENABLED", True):
            result = self.manager.execute_starter(plan, 100.0, settled_cash=1_000.0)
        self.assertEqual(result["reason"], "startup ownership preflight not ready")

    def test_enabled_process_cannot_trim_before_preflight_even_without_flag_file(self):
        self.assertFalse(self.degraded.exists())
        with mock.patch.object(f6.config, "FOREVER6_ENABLED", True), \
             mock.patch.object(guard, "load_ledger") as load:
            self.assertEqual(self.manager.evaluate_and_execute_trims(), {})
        load.assert_not_called()

    def test_failed_clean_interlock_write_never_marks_process_ready(self):
        with mock.patch.object(f6, "_write_sync_degraded", return_value=False), \
             mock.patch.object(f6, "_slack_safe"):
            result = self._run(_ledger(f6_qty=1, net=1))
        self.assertFalse(result.ready)
        self.assertFalse(f6._STARTUP_READY)
        self.assertIn("persist", result.reason)


if __name__ == "__main__":
    unittest.main()
