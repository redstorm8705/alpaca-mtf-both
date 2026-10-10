#!/usr/bin/env python3
# ruff: noqa: E501
"""A Day-tier signal whose stop the last trade has already crossed is recorded as invalidated and not retried on the
same signal bar (losers audit 2026-10-09: AMZN's 09:30-bucket signal aborted at 09:36 and 09:38, entered at 09:40 and
was stopped at 09:43)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from execution import day_trade_manager as dtm

def _bar(hhmm):
    """A bar_id for TODAY (state records older than _STATE_TTL_DAYS are pruned on load)."""
    return f"{dtm._now_et():%Y%m%d}-{hhmm}"


_INVALID = "room stop: stop $258.24 already reached by the last trade $258.00 — setup invalidated, skip"


class InvalidatedSetup(unittest.TestCase):
    def _attempt(self, state_path, bar_id, room):
        from execution import broker
        from data.live_price import LivePrice
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "FADE", "entry_ref": 258.57, "target": 259.02,
                   "wall_ref": 255.0}
        rs = mock.Mock(return_value=room)
        sub = mock.Mock(return_value=None)
        with mock.patch.object(dtm, "_STATE", state_path), \
                mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_room_stop", rs), \
                mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None), \
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")), \
                mock.patch("data.live_price.live_price", return_value=LivePrice(258.57, "iex_trade", 1.0)), \
                mock.patch("data.alpaca_data.get_latest_quote", return_value={"bid": 258.5, "ask": 258.6}), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_logger.log_decision", return_value=True), \
                mock.patch.object(broker, "get_account", return_value=acct), \
                mock.patch.object(broker, "get_open_positions", return_value=[]), \
                mock.patch.object(broker, "get_open_orders", return_value=[]), \
                mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30), \
                mock.patch.object(broker, "submit_limit_order", sub):
            ok = dtm.place_entry("AMZN", {"would_consider": True}, trigger, {"size_ok": True, "shares": 2},
                                 bar_id=bar_id, equity=2500.0)
        return ok, rs, sub

    def test_invalidated_signal_is_not_retried_on_the_same_bar(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            ok, rs, sub = self._attempt(p, _bar("0930"), (None, _INVALID))
            self.assertFalse(ok)
            rec = json.loads(p.read_text())[dtm._entry_key("AMZN", _bar("0930"))]
            self.assertEqual(rec["state"], "setup_invalidated")
            sub.assert_not_called()
            ok2, rs2, sub2 = self._attempt(p, _bar("0930"), (257.0, "kept"))     # 09:40, same signal bucket
            self.assertFalse(ok2)
            rs2.assert_not_called()                                                 # skipped before any pricing
            sub2.assert_not_called()

    def test_a_later_bar_is_a_new_setup(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            self._attempt(p, _bar("0930"), (None, _INVALID))
            _ok, rs, _sub = self._attempt(p, _bar("0945"), (None, _INVALID))
            rs.assert_called_once()                                                 # evaluated again

    def test_other_aborts_do_not_bench_the_bar(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            self._attempt(p, _bar("0930"), (None, "room stop: invalid entry/stop/direction"))
            self.assertNotIn(dtm._entry_key("AMZN", _bar("0930")), json.loads(p.read_text()) if p.exists() else {})

    def test_invalidated_record_is_terminal(self):
        self.assertIn("setup_invalidated", dtm._TERMINAL_STATES)


if __name__ == "__main__":
    unittest.main()
