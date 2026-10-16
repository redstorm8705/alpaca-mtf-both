#!/usr/bin/env python3
# ruff: noqa: E501
"""auto_ai_audit Day-tier stop evidence + Swing breakout rendering (Claude 2026-10-16)."""
import json
import unittest
from pathlib import Path
from unittest import mock

import auto_ai_audit as audit


class DayStopEvidence(unittest.TestCase):
    """2026-10-15: Groq reported "no protective stops" because the Day-tier OCO stop legs carry no Day-tier tag and
    the entry line showed no stop. Each Day entry line now shows its recorded stop (or that none was recorded)."""

    def test_day_entry_line_shows_its_stop(self):
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp())
        (tmp / "day_tier_events.jsonl").write_text(json.dumps({
            "ts": "2026-10-15T07:00:40-07:00", "event": "stop_placed", "trade_id": "DT-TSLA-s-1-a",
            "symbol": "TSLA", "stop_order_id": "f07c86b8-6e8f-4f0b-ba00-92ba694b5ca3", "stop_price": 357.79}) + "\n")
        with mock.patch.object(audit, "_LOGS_DIR", tmp):
            stops = audit._load_day_stop_evidence()
        self.assertEqual(stops["DT-TSLA-s-1-a"]["stop_price"], 357.79)
        ctx = {"stats": {"n_fills": 0, "n_entries": 1, "infrastructure_gaps": [], "per_symbol": {},
                         "score_distribution": {}, "mri_distribution": {}},
               "prior_directives": [], "events": [
                   {"ts": "2026-10-15T07:00:41-07:00", "event": "entry", "symbol": "TSLA", "tier": "daytrade",
                    "trade_id": "DT-TSLA-s-1-a", "direction": "short", "price": 355.47, "size": 4},
                   {"ts": "2026-10-15T07:05:00-07:00", "event": "entry", "symbol": "AMD", "tier": "daytrade",
                    "trade_id": "DT-AMD-b-2-b", "direction": "long", "price": 100.0, "size": 1},
                   {"ts": "2026-10-15T07:12:17-07:00", "event": "entry", "symbol": "AAPL", "tier": "intraday",
                    "score": 0, "setup": "c2_breakout_55d", "stop": 328.7, "price": 345.15, "size": 1}],
               "fills": [], "chart_proxies": {}, "macro_events": [], "rejected_signals": [], "day_stops": stops}
        body = audit._format_meta_audit_body(ctx)
        self.assertIn("ENTRY TSLA", body)
        self.assertIn("protective_stop=$357.79 (broker stop order f07c86b8", body)
        self.assertIn("ENTRY AMD | tier=day | dir=long | setup=UNKNOWN | conviction=UNKNOWN | "
                      "protective_stop=NOT_RECORDED_IN_DAY_LOG", body)
        self.assertIn("setup=c2_breakout_55d (not 12-point scored) | protective_stop=$328.70", body)
        self.assertNotIn("score=0", body)


if __name__ == "__main__":
    unittest.main()
