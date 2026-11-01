# ruff: noqa: E501
"""SWING-OVERNIGHT-1030 v8 item 1 / v9 item 5 — a main-bot restart with QQQM held at the broker and the trend_night
state file deleted.

The real main-bot functions run against a recording fake trading client: `execution.broker._get_trading_client` is
patched, so every account / position / order call the real code makes lands in FakeClient (tests never reach the
broker). The test then asserts that NO order of any kind (submit, close, cancel, replace, close-all) was sent for
QQQM by: orphan adoption (reconcile_positions), the startup / premarket GTC reconcile with its closed-market
emergency-stop sweep (cancel_and_reconcile_gtc_stops), check_exits, the mass close (safe_close_all, circuit breaker
included), the shutdown preserve path (preserve_swing_on_shutdown) and stop_protection (reconcile_protection).

Not driven here, with the reason:
  * main.main()'s inline P0-STARTUP block — it is the whole bot process (clients, schedulers, the run_cycle loop) and
    cannot be entered without starting it. Its QQQM filter feeds only risk.open_positions and log lines (no orders);
    the order-placing paths it hands the tracker to are the ones driven below.
  * strategy/run_cycle.py's 16:00 EOD flatten — it runs inside run_cycle() (full live loop) and iterates
    tracker.open_trades, the same book check_exits / safe_close_all iterate here; the restart leaves QQQM out of it.
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from execution import swing_overnight as so

_ORDER_CALLS = ("submit_order", "close_position", "close_all_positions", "cancel_order_by_id", "cancel_orders",
                "replace_order_by_id")


class FakeClient:
    """Records every order call; answers reads from a one-position account (QQQM long 10)."""

    def __init__(self):
        self.calls = []
        self.pos = SimpleNamespace(symbol="QQQM", qty="10", qty_available="0", side="long", avg_entry_price="294.10",
                                   current_price="295.00", market_value="2950.00", unrealized_pl="9.0",
                                   asset_class="us_equity", lastday_price="294.00")

    def get_all_positions(self):
        self.calls.append(("get_all_positions", (), {}))
        return [self.pos]

    def get_open_position(self, symbol):
        if symbol == "QQQM":
            return self.pos
        raise RuntimeError("40410000 position does not exist")

    def get_orders(self, *a, **kw):
        return []

    def get_order_by_id(self, *a, **kw):
        raise RuntimeError("40410000 order not found")

    def get_order_by_client_id(self, *a, **kw):
        raise RuntimeError("40410000 order not found")

    def get_account(self):
        return SimpleNamespace(equity="2555", last_equity="2555", buying_power="5000", regt_buying_power="5000",
                               account_number="PA-TEST", trading_blocked=False, account_blocked=False,
                               daytrade_count=0, pattern_day_trader=False, shorting_enabled=True)

    def get_clock(self):
        return SimpleNamespace(is_open=False, next_open=None, next_close=None, timestamp=datetime.now(so.ET))

    def __getattr__(self, name):
        def rec(*a, **kw):
            self.calls.append((name, a, kw))
            return SimpleNamespace(id=f"fake-{len(self.calls)}", status="accepted", client_order_id="x")
        if name in _ORDER_CALLS:
            return rec
        def read(*a, **kw):
            self.calls.append((name, a, kw))
            return []
        return read

    def order_calls(self):
        return [c for c in self.calls if c[0] in _ORDER_CALLS]


class _Tracker:
    """The legacy (Swing) tracker as a restarted process sees it: nothing open in its own book."""

    def __init__(self):
        self.open_trades = {}
        self.closed_trades = []
        self.traded_today = set()
        self.saved = 0

    def _save_log(self):
        self.saved += 1

    def opened_today(self, sym):
        return False

    def get_overnight_gtc_positions(self):           # same contract as PortfolioTracker
        return [(s, t) for s, t in self.open_trades.items() if t.get("gtc_stop_order_id")]

    def set_gtc_stop_order_id(self, sym, oid):
        raise AssertionError(f"no GTC stop may be recorded for {sym}")

    def clear_gtc_stop_order_id(self, sym):
        self.open_trades.get(sym, {}).pop("gtc_stop_order_id", None)

    def record_exit(self, *a, **kw):
        raise AssertionError("no exit may be recorded on a restart with only QQQM held")


class RestartWithStateDeleted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeClient()
        missing = Path(self.tmp.name) / "swing_trend_night.json"     # the trend_night state file is gone
        self.assertFalse(missing.exists())
        for p in (mock.patch("execution.broker._get_trading_client", return_value=self.fake),
                  mock.patch.object(so, "_DEFAULT_STATE", missing),
                  mock.patch("alerts.send_slack", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.tracker = _Tracker()

    def assert_no_qqqm_orders(self, where):
        bad = [c for c in self.fake.order_calls() if "QQQM" in repr(c)]
        self.assertEqual(bad, [], f"{where} sent a QQQM order: {bad}")

    def _orphan_reconcile(self):
        from execution import orphan_manager as om
        # other tiers' state readers return empty books (they hold nothing here); the trend_night lookup is real
        with mock.patch.object(om, "_get_qhm_syms", return_value=frozenset()), \
                mock.patch.object(om, "_get_forever6_syms", return_value=set()), \
                mock.patch.object(om, "_get_breakout_syms", return_value=set()), \
                mock.patch.object(om, "_get_daytrade_syms", return_value=set()):
            om.reconcile_positions(self.tracker)

    def test_full_restart_sequence_sends_zero_qqqm_orders(self):
        from events import handlers
        from execution import exit_logic, orphan_manager as om, stop_protection as sp

        # 1. startup orphan adoption: QQQM is in Alpaca, absent from the tracker → must NOT be adopted
        self._orphan_reconcile()
        self.assertNotIn("QQQM", self.tracker.open_trades)
        self.assert_no_qqqm_orders("reconcile_positions")
        # the real function ran and read the (fake) broker book that holds QQQM
        self.assertIn("get_all_positions", [c[0] for c in self.fake.calls])

        # 2. startup / premarket GTC reconcile incl. the closed-market emergency-stop sweep (forced "closed" phase)
        with mock.patch.object(om, "get_tod_phase", return_value="closed"):
            om.cancel_and_reconcile_gtc_stops(self.tracker)
        self.assert_no_qqqm_orders("cancel_and_reconcile_gtc_stops")

        # 3. check_exits on the restarted book
        risk = mock.MagicMock()
        exit_logic.check_exits(self.tracker, risk, kelly=mock.MagicMock(), gate_state=mock.MagicMock())
        self.assert_no_qqqm_orders("check_exits")

        # 4. the mass close — routine and circuit breaker
        with mock.patch("execution.risk_manager._save_kill_state"):
            handlers.safe_close_all(self.tracker, risk, circuit_breaker=False)
            handlers.safe_close_all(self.tracker, risk, circuit_breaker=True)
        self.assert_no_qqqm_orders("safe_close_all")

        # 5. operator shutdown
        self.assertTrue(handlers.preserve_swing_on_shutdown(self.tracker, risk))
        self.assert_no_qqqm_orders("preserve_swing_on_shutdown")

        # 6. stop_protection, every session
        for session in ("rth", "ah", "premarket", "closed"):
            sp.reconcile_protection(self.tracker, risk, session=session)
        self.assert_no_qqqm_orders("reconcile_protection")
        self.assertEqual(self.fake.order_calls(), [], "no order of any kind on this restart")

    def test_unexpected_tracker_entry_still_gets_no_emergency_stop_or_protection_stop(self):
        """Belt and braces: even if a QQQM record somehow sits in the tracker, the closed-market emergency sweep and
        stop_protection place nothing on it (the runner owns the one stop)."""
        from execution import orphan_manager as om, stop_protection as sp
        self.tracker.open_trades["QQQM"] = {"symbol": "QQQM", "direction": "long", "status": "open", "qty": 10,
                                            "stop": 282.33, "entry_price": 294.10, "trade_mode": "intraday",
                                            "overnight": True}
        with mock.patch.object(om, "get_tod_phase", return_value="closed"):
            om.cancel_and_reconcile_gtc_stops(self.tracker)
        self.assert_no_qqqm_orders("cancel_and_reconcile_gtc_stops (tracker entry)")
        summary = sp.reconcile_protection(self.tracker, None, session="closed")
        self.assertEqual(summary["excluded_trend_night"], ["QQQM"])
        self.assert_no_qqqm_orders("reconcile_protection (tracker entry)")

    def test_trend_night_lookup_failure_still_excludes(self):
        with mock.patch("execution.swing_overnight.trend_night_symbols", side_effect=RuntimeError("import broke")):
            self._orphan_reconcile()
        self.assertNotIn("QQQM", self.tracker.open_trades)
        self.assert_no_qqqm_orders("reconcile_positions (lookup failure)")


if __name__ == "__main__":
    unittest.main()
