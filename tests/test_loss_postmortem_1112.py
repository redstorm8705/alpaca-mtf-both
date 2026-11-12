# ruff: noqa: E501
"""LOSS-PM-1112: the nightly losing-trade post-mortem. Bars, the reducer and Slack are mocked; nothing reaches
Alpaca or Slack and nothing is written outside a temp dir."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  — test guard

_SPEC = importlib.util.spec_from_file_location(
    "loss_postmortem", Path(__file__).resolve().parent.parent / "scripts" / "loss_postmortem.py")
lp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lp)


def _rec(**kw):
    base = {"trade_id": "DT-AMZN-b-1", "tier": "daytrade", "symbol": "AMZN", "side": "long",
            "ts_entry": "2026-11-12T07:01:20-08:00", "ts_exit": "2026-11-12T07:20:11-08:00",
            "entry_price": 272.63, "stop_price": 270.40, "target_price": 277.09, "exit_price": 270.40,
            "qty": 6, "realized_pnl": -13.41, "realized_R": -1.0, "mfe_R": 0.17, "mae_R": 1.48,
            "exit_reason": "protective_stop", "indicators": {"mode": "PULLBACK"}}
    base.update(kw)
    return base


class Verdicts(unittest.TestCase):
    def test_failed_at_once(self):
        self.assertIn("entry was wrong", lp._verdict({"mfe_R": 0.17, "after_exit_best_R": 0.4}))

    def test_noise_stop(self):
        self.assertIn("noise exit", lp._verdict({"mfe_R": 0.64, "after_exit_best_R": 1.37}))

    def test_no_after_exit_data_never_claims_the_exit_was_right(self):
        v = lp._verdict({"mfe_R": 0.2, "after_exit_best_R": None})
        self.assertIn("unknown", v)
        self.assertNotIn("exit was right", v)
        self.assertNotIn("exit was right", lp._verdict({"mfe_R": 0.2, "after_exit_best_R": 0.9}))

    def test_gave_back_a_winner(self):
        self.assertIn("gave back", lp._verdict({"mfe_R": 1.14, "after_exit_best_R": 0.02}))

    def test_unknown_inputs(self):
        self.assertIn("no clear", lp._verdict({"mfe_R": None, "after_exit_best_R": None}))

    def test_minutes_after_buffer_and_bucket(self):
        m = lp._minutes_after_buffer(datetime.fromisoformat("2026-11-12T07:01:20-08:00"))
        self.assertAlmostEqual(m, 1.3)
        self.assertEqual(lp._bucket(m), "10:00-10:10 ET")
        self.assertEqual(lp._bucket(-5), "before 10:00 ET")
        self.assertEqual(lp._bucket(None), "unknown")


class Build(unittest.TestCase):
    def _build(self, records, decisions=None, atr=10.0, after=0.9, band=(None, None)):
        with mock.patch("research.trade_record_reducer._reconstruct_mae_mfe", return_value=band), \
                mock.patch.object(lp, "_records", return_value=records), \
                mock.patch.object(lp, "_decisions", return_value=decisions or {}), \
                mock.patch.object(lp, "_daily_atr", return_value=atr), \
                mock.patch.object(lp, "_after_exit_best", return_value=after):
            return lp.build("2026-11-12")

    def test_only_todays_losers_are_reported(self):
        recs = [_rec(), _rec(trade_id="W", realized_pnl=5.0, realized_R=0.5),
                _rec(trade_id="Y", ts_exit="2026-11-11T07:20:11-08:00")]
        rep = self._build(recs, {"DT-AMZN-b-1": {"track": "B", "mode": "PULLBACK", "conviction": 0.6,
                                                  "ten_of_ten": True}})
        self.assertEqual([t["trade_id"] for t in rep["losing_trades"]], ["DT-AMZN-b-1"])
        t = rep["losing_trades"][0]
        self.assertEqual((t["track"], t["ten_of_ten"], t["entry_pt"]), ("B", True, "07:01"))
        self.assertEqual(t["stop_in_daily_atr"], 0.22)       # 2.23 / 10.0
        self.assertEqual(t["target_R"], 2.0)
        self.assertEqual(t["after_exit_best_R"], 0.4)         # 0.9 / 2.23
        self.assertEqual(rep["total_loss"], -13.41)
        self.assertIn("entry was wrong", t["verdict"])

    def test_missing_mfe_is_rebuilt_from_bars(self):
        rep = self._build([_rec(mfe_R=None, mae_R=None)], band=(269.0, 273.0))
        t = rep["losing_trades"][0]
        self.assertEqual((t["mfe_R"], t["mae_R"]), (0.17, 1.63))

    def test_sparse_logged_mfe_is_widened_by_bars(self):
        """Review: one 30-minute sample showed 0.2R, the bars show the trade was up 1.4R -> gave back a winner."""
        rep = self._build([_rec(mfe_R=0.2, mae_R=1.0)], band=(270.3, 275.75))
        t = rep["losing_trades"][0]
        self.assertEqual(t["mfe_R"], 1.4)
        self.assertIn("gave back", t["verdict"])

    def test_scaled_out_net_winner_is_not_a_pattern_loser(self):
        """Review: +$20 scale-out then -$10 on the rest: net +$10, final-leg R -1 -> not a loser anywhere."""
        rep = self._build([_rec(trade_id="S", realized_pnl=10.0, realized_R=-1.0)])
        self.assertEqual(rep["losing_trades"], [])
        self.assertEqual(rep["pattern_all_closed_trades"]["daytrade/-/PULLBACK"]["losers"], 0)

    def test_unreadable_bars_leave_honest_nulls(self):
        rep = self._build([_rec()], atr=None, after=None)
        t = rep["losing_trades"][0]
        self.assertIsNone(t["stop_in_daily_atr"])
        self.assertIsNone(t["after_exit_best_R"])

    def test_pattern_groups_by_mode_and_entry_time(self):
        rep = self._build([_rec(), _rec(trade_id="W", realized_pnl=5.0, realized_R=2.0, mfe_R=2.1,
                                        ts_entry="2026-11-12T08:30:00-08:00")])
        pat = rep["pattern_all_closed_trades"]
        self.assertEqual(pat["entry 10:00-10:10 ET"]["n"], 1)
        self.assertEqual(pat["entry 10:00-10:10 ET"]["losers_fast_fail"], 1)
        self.assertEqual(pat["daytrade/-/PULLBACK"]["sum_R"], 1.0)


class MainOutput(unittest.TestCase):
    def test_writes_report_and_sends_one_slack_message(self):
        rep = {"date": "2026-11-12", "losing_trades": [{"symbol": "AMZN", "tier": "daytrade", "mode": "PULLBACK",
               "side": "long", "entry_pt": "07:01", "realized_pnl": -13.41, "realized_R": -1.0,
               "stop_in_daily_atr": 0.22, "mfe_R": 0.17, "after_exit_best_R": 0.4, "verdict": "x"}],
               "total_loss": -13.41, "pattern_all_closed_trades": {}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lp, "_OUT_DIR", Path(d)), \
                mock.patch.object(lp, "build", return_value=rep), mock.patch("alerts.notify") as n:
            lp.main(["--date", "2026-11-12"])
            self.assertEqual(json.loads((Path(d) / "2026-11-12.json").read_text())["total_loss"], -13.41)
        self.assertEqual(n.call_count, 1)
        self.assertIn("1 losing trade", n.call_args[0][2])

    def test_dry_run_writes_and_sends_nothing(self):
        rep = {"date": "2026-11-12", "losing_trades": [], "total_loss": 0, "pattern_all_closed_trades": {}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(lp, "_OUT_DIR", Path(d)), \
                mock.patch.object(lp, "build", return_value=rep), mock.patch("alerts.notify") as n:
            lp.main(["--date", "2026-11-12", "--dry-run"])
            self.assertEqual(list(Path(d).iterdir()), [])
        n.assert_not_called()


if __name__ == "__main__":
    unittest.main()
