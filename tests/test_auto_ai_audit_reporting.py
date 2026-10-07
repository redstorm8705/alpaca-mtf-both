#!/usr/bin/env python3
"""Regression tests for meta-audit history isolation and Slack rendering."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
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
                }
                body = audit._format_meta_audit_body(context)
                self.assertIn("Structured finding", body)
                self.assertNotIn("2026-08-27", body)
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
        self.assertIn(f"Full report: {fresh}", rendered)


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
