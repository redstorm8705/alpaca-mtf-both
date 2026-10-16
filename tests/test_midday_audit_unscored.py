# ruff: noqa: E501
"""midday_audit — entries that are not 12-point scored (Day tier, Day-to-Swing promotion, Swing 55-day breakout) are
labelled "no 12-pt score (<source>)" instead of "score 0/12" (Claude 2026-10-16; triage
logs/design_records/audit_triage_2026-10-16.md #2). Records are built with the real trade_logger.log_event; no
broker, no network, no Slack."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  — test guard (blank broker keys / Slack, MTF_TEST_MODE)
import midday_audit as ma


def _logged(event, **kw):
    """One event exactly as production writes it: trade_logger.log_event() into a temp JSONL, read back."""
    import json as _json

    import trade_logger
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "trade_events.jsonl"
        with mock.patch.object(trade_logger, "_JSONL", p):
            trade_logger.log_event(event, **kw)
        return _json.loads(p.read_text().splitlines()[-1])


class TestUnscoredEntries(unittest.TestCase):
    """2026-10-09 / 2026-10-13 midday reports: Day-tier, promotion and Swing-breakout entries are not 12-pt scored;
    trade_logger writes score=0 for them, printed as "score 0/12" and reported as a MIN_SCORE gate bypass."""

    def setUp(self):
        # the exact calls at execution/day_trade_manager.py:2736, execution/day_promotion.py:598,
        # execution/swing_breakout_manager.py:332+708 and execution/portfolio_tracker.py:1517 (core, scored)
        self.day = _logged("entry", symbol="AMZN", price=258.43, size=6, data_source="daytrade", tier="daytrade",
                           direction="long", trade_id="DT-1", stop=257.0)
        self.promo = _logged("entry", symbol="AAPL", price=250.0, size=1, data_source="promotion", tier="intraday",
                             direction="long", stop=245.0, trade_id="T2", promoted_from_day_tier=True, day_cost=249.0)
        self.brk = _logged("entry", symbol="MU", price=120.0, size=1, data_source="alpaca_data", tier="intraday",
                           setup="c2_breakout_55d", family_id="swing_breakout_55d_v1", direction="long", stop=110.0)
        self.core = _logged("entry", symbol="PLTR", price=181.72, size=2, score=12, mri_level="NORMAL",
                            data_source="alpaca_data")
        self.core0 = _logged("entry", symbol="SOFI", price=17.0, size=3, score=0, data_source="alpaca_data")
        self.exits = [_logged("exit", symbol="AMZN", price=258.14, size=6),
                      _logged("exit", symbol="PLTR", price=206.73, size=2)]

    def test_production_records_carry_score_zero(self):
        # the premise the first draft got wrong: the key is never absent
        for rec in (self.day, self.promo, self.brk):
            self.assertEqual(rec["score"], 0)

    def test_unscored_paths_are_labelled_by_their_own_markers(self):
        self.assertEqual(ma._unscored_source(self.day), "Day tier")
        self.assertEqual(ma._unscored_source(self.promo), "Day-to-Swing promotion")
        self.assertEqual(ma._unscored_source(self.brk), "Swing 55-day breakout")
        for rec in (self.day, self.promo, self.brk):
            self.assertIsNone(ma._entry_score(rec))
        # a 12-pt-scored entry keeps its logged score, a real 0 included (never excused)
        self.assertIsNone(ma._unscored_source(self.core0))
        self.assertEqual(ma._entry_score(self.core), 12)
        self.assertEqual(ma._entry_score(self.core0), 0)
        self.assertIsNone(ma._entry_score({"score": "x"}))
        self.assertIsNone(ma._entry_score({"score": [1]}))
        self.assertEqual(ma._entry_score({"score": "9"}), 9)

    def test_pnl_and_prompt(self):
        events = [self.day, self.core] + self.exits
        pnl, ent = ma.analyse_pnl(events), ma.analyse_entries(events)
        by = {t["symbol"]: t for t in pnl["trades"]}
        self.assertIsNone(by["AMZN"]["score"])
        self.assertEqual(by["PLTR"]["score"], 12)
        self.assertAlmostEqual(by["PLTR"]["pnl"], 50.02, places=2)       # 2 sh x $25.01 (10-13 "mismatch" refuted)
        with mock.patch.object(ma, "_build_config_constants_block", return_value=""):
            p = ma._build_gemini_prompt(ent, {}, {}, pnl, [], "", {"available": False})
        self.assertNotIn("score 0/12", p)
        self.assertIn("AMZN: entry $258.43 → exit $258.14 ($-1.74) | no 12-pt score (Day tier)", p)
        self.assertIn("score 12/12", p)
        self.assertIn("NOT a score-gate bypass", p)

    def test_slack_body(self):
        events = [self.day, self.core] + self.exits
        pnl, ent = ma.analyse_pnl(events), ma.analyse_entries(events)
        body = ma.build_slack_body(ent, {"levels": {}}, {"error_count": 0, "warning_count": 0,
                                                         "watchdog_count": 0, "watchdog_lines": [],
                                                         "error_samples": []}, pnl)
        self.assertNotIn("0/12", body)
        self.assertIn("no 12-pt score (Day tier)", body)               # Worst line
        self.assertIn("no 12-pt score: 0/1W", body)                    # Score→Outcome bucket for unscored
        self.assertIn("12/12: 1/1W", body)

    def test_flags(self):
        ent = ma.analyse_entries([self.day, dict(self.core, score=7, symbol="NFLX"), self.core0])
        self.assertEqual([(f["symbol"], f["flags"]) for f in ent["flagged_entries"]],
                         [("NFLX", ["low score (7/12)"])])                # 0 is not "low score" (unchanged rule)
        ent = ma.analyse_entries([dict(self.brk, mri_level="STRESSED")])
        fe = ent["flagged_entries"][0]
        self.assertEqual(ma._score_label(fe["score"], fe["unscored_source"]), "no 12-pt score (Swing 55-day breakout)")
        self.assertEqual(ma._score_label(None), "no 12-pt score")


if __name__ == "__main__":
    unittest.main()
