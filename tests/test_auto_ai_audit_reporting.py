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
    def test_markdown_table_is_vertical_and_complete(self):
        report = (
            "### Trade-by-trade\n\n---\n"
            "| Entry | Symbol | Outcome |\n"
            "|---|---|---|\n"
            "| 2026-09-10 | AVGO | **loss after stop** | retained extra field |\n"
        )
        rendered = audit._render_meta_report_text(report)
        self.assertIn("*Entry* 2026-09-10", rendered)
        self.assertIn("*Symbol* AVGO", rendered)
        self.assertIn("*Outcome* *loss after stop*", rendered)
        self.assertIn("*Additional detail* retained extra", rendered)
        self.assertNotIn("|", rendered)
        self.assertNotIn("---", rendered)

    def test_every_block_reaches_chunking_sender(self):
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
        self.assertGreater(len(blocks), 50)
        self.assertIn("Auto AI Meta-Audit", fallback)
        text = "\n".join(
            block.get("text", {}).get("text", "") for block in blocks
            if block.get("type") == "section"
        )
        self.assertIn("Finding 4999", text)


if __name__ == "__main__":
    unittest.main()
