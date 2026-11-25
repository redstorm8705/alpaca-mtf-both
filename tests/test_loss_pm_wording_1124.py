# ruff: noqa: E501
"""LOSS-PM-WORDING-1124: a fast-failing loss whose after-exit move WAS measured is never labelled "unknown". No network."""
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT))

import loss_postmortem as lp  # noqa: E402


class VerdictWording(unittest.TestCase):
    def test_partial_comeback_is_named_with_its_size(self):
        v = lp._verdict({"mfe_R": 0.23, "after_exit_best_R": 0.84})   # 11-23 TSLA
        self.assertNotIn("unknown", v)
        self.assertIn("0.84R", v)
        self.assertIn("stop may be tight", v)

    def test_unmeasured_after_exit_says_not_measured(self):
        self.assertIn("unknown: not measured", lp._verdict({"mfe_R": 0.1, "after_exit_best_R": None}))

    def test_existing_branches_unchanged(self):
        self.assertTrue(lp._verdict({"mfe_R": 0.22, "after_exit_best_R": 0.38}).startswith("failed at once and did not come back"))
        self.assertTrue(lp._verdict({"mfe_R": 0.34, "after_exit_best_R": 0.99}).startswith("failed at once, then price came back"))
        self.assertTrue(lp._verdict({"mfe_R": 0.2, "after_exit_best_R": 1.2}).startswith("noise exit"))
        self.assertTrue(lp._verdict({"mfe_R": 1.1, "after_exit_best_R": None}).startswith("gave back a winner"))
        self.assertTrue(lp._verdict({"mfe_R": None, "after_exit_best_R": None}).startswith("no clear single cause"))

    def test_card_cause_for_partial_comeback(self):
        row = {"verdict": lp._verdict({"mfe_R": 0.23, "after_exit_best_R": 0.84})}
        self.assertEqual(lp._cause(row), "partly_back")

    def test_card_names_partial_comeback_not_unclear(self):
        t = {"symbol": "TSLA", "tier": "daytrade", "mode": "PULLBACK", "side": "long", "entry_pt": "07:30",
             "exit_pt": "08:42", "realized_pnl": -22.16, "realized_R": -1.0155, "stop_in_daily_atr": 0.28,
             "mfe_R": 0.23, "after_exit_best_R": 0.84,
             "verdict": lp._verdict({"mfe_R": 0.23, "after_exit_best_R": 0.84})}
        _text, blocks = lp._blocks({"date": "2026-11-23", "losing_trades": [t], "total_loss": -22.16,
                                    "pattern_all_closed_trades": {}})
        flat = str(blocks)
        self.assertIn("partly recovered", flat)
        self.assertNotIn("1 unclear", flat)


if __name__ == "__main__":
    unittest.main()
