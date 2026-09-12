"""Focused protocol tests for the offline score-model lifecycle evaluator."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from research import shadow_lifecycle as sl

ET = ZoneInfo("America/New_York")


def _event(stamp: str, rows: list[dict]) -> dict:
    return {
        "schema_v": 1,
        "scan_time": stamp,
        "trade_mode": "intraday",
        "universe": 2,
        "tickers": rows,
    }


def _row(
    symbol="ABC",
    long12=True,
    short12=False,
    long16=True,
    short16=False,
    long12score=10,
    short12score=8,
    long16score=12,
    short16score=7,
) -> dict:
    return {
        "symbol": symbol,
        "long_12pt": long12score,
        "short_12pt": short12score,
        "long_signal_12": long12,
        "short_signal_12": short12,
        "long_16pt": long16score,
        "short_16pt": short16score,
        "long_signal_16": long16,
        "short_signal_16": short16,
    }


def _bar(hour: int, minute: int, opening: float, closing: float) -> sl.Bar:
    return sl.Bar(datetime(2026, 9, 10, hour, minute, tzinfo=ET), opening, closing)


class TestCandidateSelection(unittest.TestCase):
    def test_first_qualifying_scan_per_symbol_variant_wins(self):
        early = _event("2026-09-10T09:31:00-04:00", [_row("ABC")])
        later = _event("2026-09-10T10:01:00-04:00", [_row("ABC")])
        candidates, counts = sl.select_candidates([later, early])
        self.assertEqual(len(candidates), 2)
        self.assertEqual({x["variant"] for x in candidates}, {"12pt", "16pt"})
        self.assertTrue(
            all(x["scan_time"].startswith("2026-09-10T09:31") for x in candidates)
        )
        self.assertEqual(counts["12pt:later_qualifying_scan"], 1)
        self.assertEqual(counts["16pt:later_qualifying_scan"], 1)

    def test_tied_two_direction_signal_is_skipped(self):
        row = _row(
            long12=True,
            short12=True,
            long16=True,
            short16=True,
            long12score=10,
            short12score=10,
            long16score=11,
            short16score=11,
        )
        candidates, counts = sl.select_candidates(
            [_event("2026-09-10T09:31:00-04:00", [row])]
        )
        self.assertEqual(candidates, [])
        self.assertEqual(counts["12pt:direction_tie"], 1)
        self.assertEqual(counts["16pt:direction_tie"], 1)

    def test_after_hours_event_is_not_a_same_session_candidate(self):
        candidates, counts = sl.select_candidates(
            [_event("2026-09-10T16:01:00-04:00", [_row()])]
        )
        self.assertEqual(candidates, [])
        self.assertEqual(counts["outside_rth"], 1)


class TestLifecycleMath(unittest.TestCase):
    def test_strict_after_scan_entry_and_scalar_cost(self):
        candidate = {
            "session": "2026-09-10",
            "variant": "12pt",
            "symbol": "ABC",
            "direction": "long",
            "scan_time": "2026-09-10T09:32:00-04:00",
        }
        bars = [_bar(9, 30, 100, 101), _bar(9, 35, 102, 103), _bar(15, 55, 104, 105)]
        result = sl.evaluate_candidate(candidate, bars, 10.0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["entry_price"], 102)
        self.assertEqual(result["exit_price"], 105)
        self.assertAlmostEqual(result["gross_return"], 3 / 102)
        self.assertAlmostEqual(result["cost_return"], 0.001)
        self.assertAlmostEqual(result["net_return"], 3 / 102 - 0.001)

    def test_short_return_is_direction_adjusted(self):
        candidate = {
            "session": "2026-09-10",
            "variant": "16pt",
            "symbol": "ABC",
            "direction": "short",
            "scan_time": "2026-09-10T09:31:00-04:00",
        }
        result = sl.evaluate_candidate(
            candidate, [_bar(9, 35, 100, 100), _bar(15, 55, 90, 90)], 0.0
        )
        self.assertAlmostEqual(result["gross_return"], 0.1)
        self.assertAlmostEqual(result["net_return"], 0.1)

    def test_missing_strict_after_scan_bar_is_unevaluable(self):
        candidate = {
            "session": "2026-09-10",
            "variant": "12pt",
            "symbol": "ABC",
            "direction": "long",
            "scan_time": "2026-09-10T15:56:00-04:00",
        }
        result = sl.evaluate_candidate(candidate, [_bar(15, 55, 100, 100)], 10.0)
        self.assertEqual(result["status"], "unevaluable")
        self.assertEqual(result["reason"], "no_strict_after_scan_entry_bar")


class TestPersistenceAndDsr(unittest.TestCase):
    def test_malformed_rows_are_skipped_and_append_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text('{"schema_v": 1}\n{broken\n')
            rows, bad = sl.load_jsonl(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(bad, 1)
            result = {
                "schema_v": 1,
                "protocol_v": sl._PROTOCOL_V,
                "session": "2026-09-10",
                "variant": "12pt",
                "symbol": "ABC",
                "scan_time": "2026-09-10T09:31:00-04:00",
                "status": "unevaluable",
                "reason": "missing",
            }
            self.assertEqual(sl.append_new_results(path, [result]), 1)
            self.assertEqual(sl.append_new_results(path, [result]), 0)

    def test_daily_returns_are_equal_weighted(self):
        results = [
            {
                "schema_v": 1,
                "protocol_v": sl._PROTOCOL_V,
                "status": "complete",
                "variant": "12pt",
                "session": "2026-09-10",
                "net_return": 0.02,
            },
            {
                "schema_v": 1,
                "protocol_v": sl._PROTOCOL_V,
                "status": "complete",
                "variant": "12pt",
                "session": "2026-09-10",
                "net_return": 0.00,
            },
            {
                "schema_v": 1,
                "protocol_v": sl._PROTOCOL_V,
                "status": "unevaluable",
                "variant": "16pt",
                "session": "2026-09-10",
            },
        ]
        self.assertAlmostEqual(sl.daily_returns(results)["12pt"]["2026-09-10"], 0.01)
        self.assertEqual(sl.daily_returns(results)["16pt"], {})

    def test_session_summary_distinguishes_no_candidate_and_unevaluable(self):
        event = _event("2026-09-10T09:31:00-04:00", [_row(long12=False, long16=False)])
        candidates = [
            {
                "session": "2026-09-10",
                "variant": "12pt",
                "symbol": "ABC",
                "scan_time": "2026-09-10T09:31:00-04:00",
            }
        ]
        results = [
            {
                "schema_v": 1,
                "protocol_v": sl._PROTOCOL_V,
                "session": "2026-09-10",
                "variant": "12pt",
                "status": "unevaluable",
            }
        ]
        summary = sl.session_summary([event], candidates, results)
        self.assertEqual(summary["2026-09-10"]["12pt"]["candidate_count"], 1)
        self.assertEqual(summary["2026-09-10"]["12pt"]["unevaluable_count"], 1)
        self.assertEqual(summary["2026-09-10"]["12pt"]["status"], "unevaluable")
        self.assertEqual(summary["2026-09-10"]["16pt"]["candidate_count"], 0)
        self.assertEqual(summary["2026-09-10"]["16pt"]["status"], "no_observation")

    def test_dsr_requires_attested_registry_and_matched_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.json"
            registry.write_text(
                json.dumps({"schema_v": 1, "complete": False, "trials": [{"id": "x"}]})
            )
            self.assertEqual(
                sl.dsr_report([], registry)["reason"],
                "trial_registry_not_attested_complete",
            )
            registry.write_text(
                json.dumps(
                    {
                        "schema_v": 1,
                        "complete": True,
                        "trials": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
                    }
                )
            )
            rows = []
            for day in range(31):
                session = f"2026-01-{day + 1:02d}"
                rows.extend(
                    [
                        {
                            "schema_v": 1,
                            "protocol_v": sl._PROTOCOL_V,
                            "status": "complete",
                            "variant": "12pt",
                            "session": session,
                            "net_return": 0.01 + (day % 3) * 0.001,
                        },
                        {
                            "schema_v": 1,
                            "protocol_v": sl._PROTOCOL_V,
                            "status": "complete",
                            "variant": "16pt",
                            "session": session,
                            "net_return": 0.006 + (day % 4) * 0.001,
                        },
                    ]
                )
            report = sl.dsr_report(rows, registry)
            self.assertTrue(report["available"])
            self.assertEqual(report["n_trials"], 3)
            self.assertEqual(len(report["sessions"]), 31)
            self.assertIn("12pt", report["variants"])
            self.assertIn("16pt", report["variants"])

    def test_dsr_refuses_fewer_than_30_common_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_v": 1,
                        "complete": True,
                        "trials": [{"id": "a"}, {"id": "b"}],
                    }
                )
            )
            rows = []
            for day in range(29):
                session = f"2026-02-{day + 1:02d}"
                for variant in ("12pt", "16pt"):
                    rows.append(
                        {
                            "schema_v": 1,
                            "protocol_v": sl._PROTOCOL_V,
                            "status": "complete",
                            "variant": variant,
                            "session": session,
                            "net_return": 0.01,
                        }
                    )
            self.assertEqual(
                sl.dsr_report(rows, registry)["reason"], "fewer_than_30_common_sessions"
            )


if __name__ == "__main__":
    unittest.main()
