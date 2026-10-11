#!/usr/bin/env python3
# ruff: noqa: E501
"""Track B watch-day exemption scope (board Thorp + López de Prado + GAI, 2026-10-12; design record
logs/design_records/day_tier_track_b_review_2026-10-11.md): an exempt Track B short trades only when the room stop
KEEPS its structural stop; one the room stop would widen is skipped (never widened). Non-exempt entries unchanged."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution import day_trade_manager as dtm


def _bar(hhmm):
    return f"{dtm._now_et():%Y%m%d}-{hhmm}"


class ExemptScope(unittest.TestCase):
    def _attempt(self, room=None, exempt=True, last_trade=100.0, atr=0.60, rt_room=None):
        """Track B-style RIDE short: structural stop = wall 100.70 x 1.001 = 100.80; limit ~99.78 (trade 100.00,
        bid 99.98). `room` mocks _room_stop; None runs the real _room_stop with patched inputs."""
        from execution import broker
        from data.live_price import LivePrice
        trigger = {"trigger": "ENTER", "direction": "short", "mode": "DRIVE", "entry_ref": 100.0, "target": None,
                   "wall_ref": 100.70}
        if exempt:
            trigger["watch_day_exempt_b"] = True
        get_acct = mock.Mock(side_effect=RuntimeError("stop here"))     # reached only past the scope check
        patches = [mock.patch.object(dtm, "_robust_atr_5m", return_value=atr),
                   mock.patch.object(dtm, "_realtime_vol_room", return_value=rt_room),
                   mock.patch("data.alpaca_data.get_latest_trade", return_value=last_trade)]
        if room is not None:
            patches.append(mock.patch.object(dtm, "_room_stop", return_value=room))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(dtm, "_STATE", Path(d) / "state.json"), \
                mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None), \
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")), \
                mock.patch("data.live_price.live_price", return_value=LivePrice(100.0, "iex_trade", 1.0)), \
                mock.patch("data.alpaca_data.get_latest_quote", return_value={"bid": 99.98, "ask": 100.02}), \
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}), \
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch("strategy.day_tier_logger.log_decision", return_value=True), \
                mock.patch.object(broker, "get_account", get_acct), \
                mock.patch.object(broker, "get_open_positions", return_value=[]), \
                mock.patch.object(broker, "get_open_orders", return_value=[]), \
                mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30), \
                mock.patch.object(broker, "submit_limit_order", return_value=None):
            for p in patches:
                p.start()
            try:
                ok = dtm.place_entry("TSLA", {"would_consider": True, "track": "B"}, trigger,
                                     {"size_ok": True, "shares": 1, "track": "B", "budget": 300.0},
                                     bar_id=_bar("1000"), equity=2500.0)
            finally:
                for p in reversed(patches):
                    p.stop()
        return ok, get_acct

    def test_kept_structural_stop_proceeds(self):
        _ok, ga = self._attempt(room=(100.80, "room stop: structural stop kept"))
        ga.assert_called()

    def test_widened_stop_is_skipped(self):
        ok, ga = self._attempt(room=(101.20, "room stop: WIDENED 100.80->101.20"))
        self.assertFalse(ok)
        ga.assert_not_called()

    def test_real_room_stop_from_the_last_trade_skips(self):
        # adversarial input: limit 99.78 has 1.02 of room but the room stop measures from the last trade 100.00:
        # need = 100.00 + 1.5 x 0.60 = 100.90 > 100.80 -> widened -> exempt short skipped.
        ok, ga = self._attempt(room=None, atr=0.60)
        self.assertFalse(ok)
        ga.assert_not_called()

    def test_real_room_stop_with_room_proceeds(self):
        _ok, ga = self._attempt(room=None, atr=0.30)     # need = 100.45 < 100.80 -> kept
        ga.assert_called()

    def test_non_exempt_widened_entry_is_unchanged(self):
        _ok, ga = self._attempt(room=(101.20, "room stop: WIDENED 100.80->101.20"), exempt=False)
        ga.assert_called()                                # the normal widen-and-size path


if __name__ == "__main__":
    unittest.main()
