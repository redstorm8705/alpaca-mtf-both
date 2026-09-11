#!/usr/bin/env python3
"""Focused durability and failure-isolation tests for score comparison events."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from strategy import signal_generator as generator


class ScoreComparisonEventTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name) / "score_comparison_events.jsonl"
        self.previous_output = generator._SCORE_COMPARISON_EVENTS
        generator._SCORE_COMPARISON_EVENTS = self.output

    def tearDown(self):
        generator._SCORE_COMPARISON_EVENTS = self.previous_output
        self.directory.cleanup()

    def test_append_writes_parseable_complete_event_and_fsyncs(self):
        event = {
            "schema_v": 1,
            "scan_time": "2026-09-11T10:00:00-04:00",
            "trade_mode": "intraday",
            "universe": 2,
            "tickers": [{"symbol": "NVDA", "long_12pt": 10, "long_16pt": 12}],
        }
        with mock.patch.object(generator.os, "fsync") as fsync:
            self.assertTrue(generator._append_score_comparison_event(event))
        self.assertTrue(fsync.called)
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8").strip()),
            event,
        )

    def test_append_failure_is_reported_without_raising(self):
        event = {"schema_v": 1, "tickers": []}
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(generator._append_score_comparison_event(event))
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
