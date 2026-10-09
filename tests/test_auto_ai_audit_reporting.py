#!/usr/bin/env python3
"""Regression tests for meta-audit history isolation and Slack rendering."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import auto_ai_audit as audit


class PriorDirectiveIsolation(unittest.TestCase):
    def test_only_structured_directives_enter_current_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                (audit._LOGS_DIR / "audit_directives.jsonl").write_text(
                    json.dumps({
                        "week": "2026-W36", "status": "context_only",
                        "gro_directives_preview": (
                            "| Entry | Symbol |\n|---|---|\n| 2026-08-27 | SPY |"
                        ),
                    }) + "\n" + json.dumps({
                        "week": "2026-W36", "source": "gai_meta",
                        "status": "pending_review",
                        "file": "execution/example.py", "finding": "Structured finding",
                        "recommended_fix": "Keep this concise",
                    }) + "\n",
                    encoding="utf-8",
                )
                directives = audit._load_prior_directives()
                self.assertEqual(len(directives), 1)
                self.assertEqual(directives[0]["finding"], "Structured finding")
                context = {
                    "stats": {
                        "n_fills": 0, "n_entries": 0, "infrastructure_gaps": [],
                        "per_symbol": {}, "score_distribution": {},
                        "mri_distribution": {},
                    },
                    "prior_directives": directives, "events": [], "chart_proxies": {},
                    "fills": [], "macro_events": [], "bot_log_tail": "",
                    "rejected_signals": [],
                    "day_lifecycle_evidence": {},
                }
                body = audit._format_meta_audit_body(context)
                self.assertIn("Structured finding", body)
                self.assertNotIn("2026-08-27", body)
            finally:
                audit._LOGS_DIR = prior_logs


class DaySetupEvidence(unittest.TestCase):
    def _context(self, event: dict, proof: dict) -> dict:
        return {
            "stats": {
                "n_fills": 0, "n_entries": 1, "infrastructure_gaps": [],
                "per_symbol": {}, "score_distribution": {}, "mri_distribution": {},
            },
            "prior_directives": [], "refuted_findings": [], "events": [event],
            "day_lifecycle_evidence": proof, "chart_proxies": {}, "fills": [],
            "macro_events": [], "bot_log_tail": "", "rejected_signals": [],
        }

    def test_exact_join_replaces_inapplicable_score(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                rows = [
                    {"ts": "2020-10-09T08:00:00-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "NVDA", "decision": {
                        "track": "A", "mode": "FADE", "conviction": 0.61}},
                    {"ts": "2020-10-09T08:01:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-NVDA-b-1-x", "symbol": "NVDA",
                     "decision_id": "D1", "track": "A",
                     "mechanism_tags": {"family_id": "gex_wall_fade_v1"}},
                ]
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                event = {"ts": "2020-10-09T08:02:00-07:00", "event": "entry",
                         "symbol": "NVDA", "tier": "daytrade",
                         "trade_id": "DT-NVDA-b-1-x", "score": 0}
                body = audit._format_meta_audit_body(self._context(
                    event, audit._load_day_lifecycle_evidence()))
                self.assertIn("setup=gex_wall_fade_v1", body)
                self.assertIn("conviction=0.61", body)
                self.assertNotIn("score=0", body)
            finally:
                audit._LOGS_DIR = prior_logs

    def test_missing_join_is_explicitly_unknown(self):
        event = {"ts": "2020-10-09T08:02:00-07:00", "event": "entry",
                 "symbol": "META", "tier": "daytrade",
                 "trade_id": "DT-META-b-1-x", "score": 0}
        body = audit._format_meta_audit_body(self._context(event, {}))
        self.assertIn("setup=UNKNOWN", body)
        self.assertIn("conviction=UNKNOWN", body)
        self.assertNotIn("score=0", body)

    def test_conflicting_or_future_evidence_is_withheld(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                rows = [
                    {"ts": "2020-10-09T08:00:00-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "NVDA", "decision": {
                         "track": "A", "mode": "FADE", "conviction": 0.6}},
                    {"ts": "2020-10-09T08:00:01-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "NVDA", "decision": {
                         "track": "B", "mode": "DRIVE", "conviction": 0.8}},
                    {"ts": "2020-10-09T08:00:02-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "NVDA", "decision": {
                         "track": "A", "mode": "FADE", "conviction": 0.6}},
                    {"ts": "2999-10-09T08:01:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-FUTURE-b-1-x", "symbol": "NVDA",
                     "decision_id": "D1"},
                    {"ts": "2020-10-09T08:01:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-CONFLICT-b-1-x", "symbol": "NVDA",
                     "decision_id": "D1",
                     "track": "B"},
                ]
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    "not-json\n" + "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8",
                )
                proof = audit._load_day_lifecycle_evidence()
                self.assertNotIn("DT-FUTURE-b-1-x", proof)
                self.assertNotIn("DT-CONFLICT-b-1-x", proof)
            finally:
                audit._LOGS_DIR = prior_logs

    def test_score_distribution_excludes_day_and_keeps_swing(self):
        events = [
            {"event": "entry", "tier": "daytrade", "score": 0, "symbol": "NVDA"},
            {"event": "entry", "tier": "swing", "score": 11, "symbol": "MSFT"},
            {"event": "entry", "tier": "qhm", "score": 0, "symbol": "GE"},
            {"event": "entry", "tier": "forever_6", "score": 0, "symbol": "AAPL"},
        ]
        stats = audit._compute_trade_stats(events, [])
        self.assertEqual(stats["score_distribution"], {"11": 1})

    def test_missing_or_conflicting_tier_never_enters_swing_scores(self):
        events = [
            {"event": "entry", "trade_id": "DT-NVDA-b-1-x", "score": 0},
            {"event": "entry", "tier": "swing", "trade_id": "DT-X", "score": 0},
            {"event": "entry", "tier": "qhm", "trade_id": "DT-Y", "score": 0},
            {"event": "entry", "trade_id": "IN-MSFT-b-1-x", "score": 12},
        ]
        stats = audit._compute_trade_stats(events, [])
        self.assertEqual(stats["score_distribution"], {"12": 1})
        self.assertIsNone(audit._event_tier({"trade_id": "DT"}))
        self.assertIsNone(audit._event_tier({"trade_id": "IN"}))
        self.assertEqual(
            audit._event_tier({
                "trade_mode": "intraday", "trade_id": "INTRA-MSFT-20261009",
            }),
            "swing",
        )

        conflicted = {"ts": "2020-10-09T08:00:00-07:00", "event": "entry",
                      "symbol": "NVDA", "tier": "swing", "trade_id": "DT-X",
                      "score": 0}
        body = audit._format_meta_audit_body(self._context(conflicted, {}))
        self.assertIn("tier=UNKNOWN | tier_evidence=AMBIGUOUS", body)
        self.assertNotIn("score=0", body)

    def test_late_decision_cannot_label_entry_fill(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                rows = [
                    {"ts": "2020-10-09T08:01:00-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "META", "decision": {
                         "symbol": "META", "track": "A", "mode": "FADE",
                         "conviction": 0.9}},
                    {"ts": "2020-10-09T08:00:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-1", "decision_id": "D1", "symbol": "META",
                     "track": "A"},
                ]
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                self.assertNotIn("DT-1", audit._load_day_lifecycle_evidence())
            finally:
                audit._LOGS_DIR = prior_logs

    def test_malformed_duplicate_decision_poison_is_permanent(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                rows = [
                    {"ts": "2020-10-09T08:00:00-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "META", "decision": {
                         "symbol": "META", "track": "A", "mode": "FADE",
                         "conviction": 0.7}},
                    {"ts": "2020-10-09T08:00:01-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "META", "decision": []},
                    {"ts": "2020-10-09T08:00:02-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "META", "decision": {
                         "symbol": "META", "track": "A", "mode": "FADE",
                         "conviction": 0.7}},
                    {"ts": "2020-10-09T08:01:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-1", "decision_id": "D1", "symbol": "META",
                     "track": "A"},
                ]
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                self.assertNotIn("DT-1", audit._load_day_lifecycle_evidence())
            finally:
                audit._LOGS_DIR = prior_logs

    def test_unrelated_row_cannot_replace_fill_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                rows = [
                    {"ts": "2020-10-09T08:00:00-07:00", "event": "decision",
                     "decision_id": "D1", "symbol": "META", "decision": {
                         "symbol": "META", "track": "A", "mode": "FADE",
                         "conviction": 0.7}},
                    {"ts": "2020-10-09T10:00:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-1", "decision_id": "D1", "symbol": "META",
                     "track": "A"},
                    {"ts": "2020-10-09T09:00:00-07:00", "event": "price_sample"},
                ]
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                proof = audit._load_day_lifecycle_evidence()
                event = {"ts": "2020-10-09T09:30:00-07:00", "event": "entry",
                         "symbol": "META", "tier": "daytrade", "trade_id": "DT-1"}
                body = audit._format_meta_audit_body(self._context(event, proof))
                self.assertIn("setup=UNKNOWN", body)
            finally:
                audit._LOGS_DIR = prior_logs

    def test_one_second_future_decision_and_fill_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                rows = [
                    {"ts": "2020-10-09T08:00:01+00:00", "event": "decision",
                     "decision_id": "D1", "symbol": "META", "decision": {
                         "symbol": "META", "track": "A", "mode": "FADE",
                         "conviction": 0.7}},
                    {"ts": "2020-10-09T08:00:01+00:00", "event": "entry_fill",
                     "trade_id": "DT-1", "decision_id": "D1", "symbol": "META",
                     "track": "A"},
                ]
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                with mock.patch.object(
                    audit, "_audit_now_utc",
                    return_value=datetime(2020, 10, 9, 8, 0, tzinfo=timezone.utc),
                ):
                    self.assertEqual(audit._load_day_lifecycle_evidence(), {})
            finally:
                audit._LOGS_DIR = prior_logs

    def test_missing_decision_and_whitespace_symbol_are_withheld(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                rows = [
                    {"ts": "2020-10-09T08:00:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-1", "decision_id": "D-MISSING",
                     "symbol": "NVDA", "track": "A",
                     "mechanism_tags": {"family_id": "gex_wall_fade_v1"}},
                    {"ts": "2020-10-09T08:00:00-07:00", "event": "decision",
                     "decision_id": "D2", "symbol": "NVDA", "decision": {
                         "symbol": "NVDA", "track": "A", "mode": "FADE",
                         "conviction": 0.7}},
                    {"ts": "2020-10-09T08:01:00-07:00", "event": "entry_fill",
                     "trade_id": "DT-2", "decision_id": "D2",
                     "symbol": " NVDA ", "track": "A"},
                ]
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                self.assertEqual(audit._load_day_lifecycle_evidence(), {})
            finally:
                audit._LOGS_DIR = prior_logs

    def test_late_evidence_cannot_label_earlier_entry(self):
        event = {"ts": "2020-10-09T08:00:00-07:00", "event": "entry",
                 "symbol": "META", "tier": "daytrade", "trade_id": "DT-1", "score": 0}
        proof = {"DT-1": {"setup": "gex_wall_fade_v1", "conviction": 0.7,
                           "symbol": "META", "as_of": "2020-10-09T15:00:01+00:00"}}
        body = audit._format_meta_audit_body(self._context(event, proof))
        self.assertIn("setup=UNKNOWN", body)

    def test_naive_lifecycle_timestamp_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            prior_logs = audit._LOGS_DIR
            audit._LOGS_DIR = Path(directory)
            try:
                row = {"ts": "2020-10-09T08:00:00", "event": "entry_fill",
                       "trade_id": "DT-NAIVE", "symbol": "META", "track": "A"}
                (audit._LOGS_DIR / "day_tier_events.jsonl").write_text(
                    json.dumps(row) + "\n", encoding="utf-8")
                self.assertEqual(audit._load_day_lifecycle_evidence(), {})
            finally:
                audit._LOGS_DIR = prior_logs


class MetaAuditSlackRendering(unittest.TestCase):
    def test_report_digest_uses_directives_instead_of_raw_tables(self):
        report = (
            "| Entry | Symbol | Outcome |\n"
            "|---|---|---|\n"
            "| 2026-09-10 | AVGO | loss after stop |\n"
            "[DIRECTIVE-1] Recheck the exit guard.\n"
            "### 5. FINAL VERDICT\nWARN — review required"
        )
        blocks = audit._meta_report_blocks(
            {"text": report, "error": None}, "Groq"
        )
        rendered = blocks[0]["text"]["text"]
        self.assertIn("Recheck the exit guard", rendered)
        self.assertNotIn("| Entry |", rendered)

    def test_compact_digest_reaches_sender_without_unpublished_link(self):
        result = {
            "text": "\n".join(f"Finding {i}" for i in range(5_000)),
            "error": None,
        }
        with mock.patch.dict(
            os.environ, {"SLACK_WEBHOOK_URL": "https://example.invalid"}
        ), \
             mock.patch("alerts.send_slack_blocks", return_value=True) as sender:
            audit._post_slack_summary(result, result, Path("unused.json"))
        blocks, fallback = sender.call_args.args
        self.assertLess(len(blocks), 10)
        self.assertIn("Auto AI Meta-Audit", fallback)
        text = "\n".join(
            block.get("text", {}).get("text", "") for block in blocks
            if block.get("type") == "section"
        )
        self.assertNotIn("Full report:", text)

    def test_fresh_report_link_is_used_when_provided(self):
        result = {"text": "### 5. FINAL VERDICT\nPASS — clean", "error": None}
        fresh = "https://example.invalid/report.json?v=fresh"
        with mock.patch.dict(
            os.environ, {"SLACK_WEBHOOK_URL": "https://example.invalid"}
        ), mock.patch("alerts.send_slack_blocks", return_value=True) as sender:
            audit._post_slack_summary(
                result, result, Path("unused.json"), report_url=fresh
            )
        blocks = sender.call_args.args[0]
        rendered = "\n".join(
            block.get("text", {}).get("text", "") for block in blocks
            if block.get("type") == "section"
        )
        self.assertIn(f"Full report (raw): {fresh}", rendered)


class GistFreshness(unittest.TestCase):
    def test_missing_token_reports_publish_failure(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(audit._push_to_gist({"ts_iso": "now"}))

    def test_success_reports_fresh_public_copy(self):
        response = mock.MagicMock()
        response.status = 200
        content = json.dumps({"ts_iso": "now"}, indent=2, ensure_ascii=False)
        response.read.return_value = json.dumps({
            "files": {"meta_audit_latest.json": {"content": content}}
        }).encode()
        response.__enter__.return_value = response
        with mock.patch.dict(os.environ, {"GITHUB_GIST_TOKEN": "secret"}), \
             mock.patch("urllib.request.urlopen", return_value=response):
            self.assertTrue(audit._push_to_gist({"ts_iso": "now"}))

    def test_malformed_http_200_is_not_fresh(self):
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b"not a gist response"
        response.__enter__.return_value = response
        with mock.patch.dict(os.environ, {"GITHUB_GIST_TOKEN": "secret"}), \
             mock.patch("urllib.request.urlopen", return_value=response):
            self.assertFalse(audit._push_to_gist({"ts_iso": "now"}))


if __name__ == "__main__":
    unittest.main()
