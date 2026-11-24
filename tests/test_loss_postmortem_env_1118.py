# ruff: noqa: E501
"""LOSS-PM-ENV-1118 (owner-approved 2026-11-18): the nightly loss post-mortem loads the repo .env, prints missing values
as "unknown", and exits non-zero when the Slack card is not delivered. No network, no broker."""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT))


def _load():
    if "loss_postmortem" in sys.modules:
        return importlib.reload(sys.modules["loss_postmortem"])
    import loss_postmortem
    return loss_postmortem


_TRADE = {"symbol": "MSFT", "tier": "daytrade", "mode": "FADE", "side": "short", "entry_pt": None, "exit_pt": None,
          "realized_pnl": -6.52, "realized_R": None, "stop_in_daily_atr": None, "mfe_R": None,
          "after_exit_best_R": None, "verdict": None}
_REP = {"date": "2026-11-20", "losing_trades": [_TRADE], "total_loss": -6.52, "pattern_all_closed_trades": {}}


class LoadsDotenv(unittest.TestCase):
    def test_load_dotenv_called_with_repo_env_at_import(self):
        with mock.patch("dotenv.load_dotenv", return_value=True) as ld:
            lp = _load()
        ld.assert_called_once_with(lp._ROOT / ".env")
        self.assertTrue(lp._ENV_LOADED)


class UnknownNotNone(unittest.TestCase):
    def test_text_line_prints_unknown_for_missing_values(self):
        lp = _load()
        head, body = lp._slack_text(_REP)
        self.assertNotIn("None", body)
        self.assertIn("Stop unknown x daily range", body)
        self.assertIn("best while held unknown R", body)

    def test_absent_keys_do_not_raise(self):
        lp = _load()
        rep = dict(_REP, losing_trades=[{"realized_pnl": -1.0}])
        _head, body = lp._slack_text(rep)
        self.assertIn("unknown", body)

    def test_card_prints_unknown_for_missing_times_and_r(self):
        lp = _load()
        lp._cause = lambda row: "unclear"
        _text, blocks = lp._blocks(_REP)
        flat = str(blocks)
        self.assertNotIn("None", flat)
        self.assertIn("unknown", flat)


class CardUnknownToo(unittest.TestCase):
    def test_card_with_missing_symbol_tier_side_prints_unknown(self):
        lp = _load()
        lp._cause = lambda row: "unclear"
        rep = dict(_REP, losing_trades=[{"realized_pnl": -1.0, "symbol": None, "tier": None, "side": None}])
        _text, blocks = lp._blocks(rep)
        flat = str(blocks)
        self.assertNotIn("None", flat)
        self.assertIn("unknown", flat)

    def test_card_with_absent_keys_does_not_raise(self):
        lp = _load()
        lp._cause = lambda row: "unclear"
        _text, blocks = lp._blocks(dict(_REP, losing_trades=[{"realized_pnl": -1.0}]))
        self.assertIn("unknown", str(blocks))


class FailedSendExitsNonZero(unittest.TestCase):
    def test_not_delivered_returns_2(self):
        lp = _load()
        with mock.patch.object(lp, "build", return_value=_REP), \
                mock.patch("alerts.send_slack_blocks", return_value=False), \
                mock.patch.object(lp, "_OUT_DIR", Path(os.environ.get("TMPDIR", "/tmp")) / "lpm_test_out"):
            self.assertEqual(lp.main(["--date", "2026-11-20"]), 2)

    def test_delivered_returns_0(self):
        lp = _load()
        with mock.patch.object(lp, "build", return_value=_REP), \
                mock.patch("alerts.send_slack_blocks", return_value=True), \
                mock.patch.object(lp, "_OUT_DIR", Path(os.environ.get("TMPDIR", "/tmp")) / "lpm_test_out"):
            self.assertEqual(lp.main(["--date", "2026-11-20"]), 0)


if __name__ == "__main__":
    unittest.main()
