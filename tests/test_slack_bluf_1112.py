# ruff: noqa: E501
"""SLK16 (owner 2026-11-12): every Slack report puts the bottom line first, in a CEO's words. Tests the check in
alerts.bluf_problems and that the loss review report passes it. Nothing is sent."""
import importlib.util
import unittest
from pathlib import Path

import tests  # noqa: F401  — test guard
import alerts

_SPEC = importlib.util.spec_from_file_location(
    "loss_postmortem", Path(__file__).resolve().parent.parent / "scripts" / "loss_postmortem.py")
lp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lp)


def _sec(t):
    return {"type": "section", "text": {"type": "mrkdwn", "text": t}}


_GOOD = [{"type": "header", "text": {"type": "plain_text", "text": "Loss review · Thu Nov 12"}},
         _sec("*4 losing trades today: $41.34 lost.* 2 wrong entries."),
         _sec("• A profit lock would have saved $17.04."),
         _sec("*Your action:* none.")]


class BlufCheck(unittest.TestCase):
    def test_compliant_report(self):
        self.assertEqual(alerts.bluf_problems(_GOOD), [])

    def test_wall_of_text_without_a_bottom_line(self):
        p = alerts.bluf_problems([_sec("Bot is: - AMZN daytrade PULLBACK long 07:01 PT: $-13.41 (-0.991R).")])
        self.assertIn("no bold bottom line first", p)
        self.assertIn("no 'Your action' line in the first four sections", p)

    def test_internal_codes_in_the_summary(self):
        for code in ("-0.99R", "PULLBACK", "Track B", "change=x", "$-13.41"):
            blocks = [_sec(f"*Bottom line {code}.*"), _sec("*Your action:* none.")]
            self.assertTrue(any("internal code" in x for x in alerts.bluf_problems(blocks)), code)

    def test_codes_are_fine_in_muted_context(self):
        self.assertEqual(alerts.bluf_problems(_GOOD + [{"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Day tier · PULLBACK · best +1.11R"}]}]), [])

    def test_never_raises(self):
        self.assertTrue(alerts.bluf_problems([None, 5, {"type": "section", "text": None}]))


class LossReviewFormat(unittest.TestCase):
    def test_todays_report_passes_and_reads_bottom_line_first(self):
        rep = {"date": "2026-11-12", "total_loss": -30.32, "pattern_all_closed_trades": {
            "entry 10:00-10:10 ET": {"n": 12, "sum_R": -6.86}}, "losing_trades": [
            {"symbol": "AMD", "tier": "daytrade", "mode": "PULLBACK", "side": "long", "entry_pt": "08:06",
             "exit_pt": "09:26", "realized_pnl": -16.91, "mfe_R": 1.11, "after_exit_best_R": 1.37,
             "verdict": "gave back a winner: it was up 1R or more before losing"},
            {"symbol": "AMZN", "tier": "daytrade", "mode": "PULLBACK", "side": "long", "entry_pt": "07:01",
             "exit_pt": "07:20", "realized_pnl": -13.41, "mfe_R": 0.17, "after_exit_best_R": 0.4,
             "verdict": "failed at once and did not come back: the entry was wrong, the exit was right"}]}
        text, blocks = lp._blocks(rep)
        self.assertEqual(alerts.bluf_problems(blocks), [])
        self.assertTrue(text.startswith("Loss review Thu Nov 12: 2 losing trades, $30.32 lost"))
        self.assertIn("−$16.91", blocks[5]["text"]["text"])          # biggest loss first, signed correctly
        self.assertNotIn("$-", str(blocks))
        self.assertIn("$16.91", blocks[2]["text"]["text"])             # the profit lock would have saved AMD's loss


if __name__ == "__main__":
    unittest.main()
