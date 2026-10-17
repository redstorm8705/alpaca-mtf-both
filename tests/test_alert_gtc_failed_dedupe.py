#!/usr/bin/env python3
# ruff: noqa: E501
"""alerts.alert_gtc_failed — one page per (symbol, side, stop price) per ET day (CEO 2026-10-16 Slack review).
Transports are mocked; the page-state file is a temp path. Nothing reaches Slack, ntfy or the broker."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  — test guard
import alerts


class GtcFailedDedupe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "gtc_failed_pages.json"
        self.p_state = mock.patch.object(alerts, "_GTC_FAIL_PAGES", self.state)
        self.p_state.start()
        self.ntfy = mock.Mock(return_value=True)
        self.slack = mock.Mock(return_value=True)
        self.p_n = mock.patch.object(alerts, "_ntfy", self.ntfy)
        self.p_s = mock.patch.object(alerts, "_slack", self.slack)
        self.p_topic = mock.patch.object(alerts, "_NTFY_TOPIC", "t")
        self.p_hook = mock.patch.object(alerts, "_SLACK_WEBHOOK", "https://hooks.example/x")
        for p in (self.p_n, self.p_s, self.p_topic, self.p_hook):
            p.start()

    def tearDown(self):
        for p in (self.p_hook, self.p_topic, self.p_s, self.p_n, self.p_state):
            p.stop()
        self.tmp.cleanup()

    def test_first_failure_pages_phone_and_slack(self):
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "held_for_orders not released after 63s: x")
        self.ntfy.assert_called_once()
        self.slack.assert_called_once()
        title, body = self.slack.call_args.args[:2]
        self.assertIn("STOP ORDER FAILED — NVDA", title)
        self.assertIn("SELL protective stop @ $192.41", body)
        self.assertIn("has no stop at the broker", body)
        self.assertNotIn("overnight", body.lower())       # a DAY stop failure is not "overnight"

    def test_retries_same_stop_same_day_are_logged_not_paged(self):
        for _ in range(4):
            alerts.alert_gtc_failed("NVDA", "sell", 192.41, "PENDING_CANCEL 6 cycles")
        self.assertEqual(self.slack.call_count, 1)
        self.assertEqual(self.ntfy.call_count, 1)

    def test_new_price_side_or_symbol_pages(self):
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        alerts.alert_gtc_failed("NVDA", "sell", 190.00, "x")      # stop moved
        alerts.alert_gtc_failed("NVDA", "buy", 192.41, "x")       # other side
        alerts.alert_gtc_failed("AMZN", "sell", 192.41, "x")      # other symbol
        self.assertEqual(self.slack.call_count, 4)

    def test_failed_delivery_is_not_stamped(self):
        self.ntfy.return_value = False
        self.slack.return_value = False
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        self.assertFalse(self.state.exists())
        self.ntfy.return_value = True
        self.slack.return_value = True
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        self.assertEqual(self.slack.call_count, 2)                # retried, then delivered

    def test_new_et_day_pages_again_and_old_days_are_dropped(self):
        self.state.write_text(json.dumps({"2000-01-01": {"NVDA|sell|192.41": "09:00 PT"}}))
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        self.assertEqual(self.slack.call_count, 1)
        saved = json.loads(self.state.read_text())
        self.assertEqual(list(saved), [datetime.now(alerts._ET).strftime("%Y-%m-%d")])

    def test_corrupt_state_file_still_pages_and_is_repaired(self):
        self.state.write_text("{not json")
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        self.assertEqual(self.slack.call_count, 1)                # rewritten clean after the first page
        self.assertIn(datetime.now(alerts._ET).strftime("%Y-%m-%d"), json.loads(self.state.read_text()))

    def test_non_dict_day_value_pages(self):
        self.state.write_text(json.dumps({datetime.now(alerts._ET).strftime("%Y-%m-%d"): 7}))
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        self.assertEqual(self.slack.call_count, 1)

    def test_phone_retries_until_delivered_even_after_slack(self):
        self.ntfy.return_value = False                            # phone push down, Slack ok
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        self.ntfy.return_value = True
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")
        self.assertEqual(self.ntfy.call_count, 2)                 # retried once, then delivered, then quiet
        self.assertEqual(self.slack.call_count, 1)                # Slack paged once only

    def test_no_stop_price_case_reads_plainly(self):
        alerts.alert_gtc_failed("XYZ", "unknown", 0.0, "No stop price or qty=0 in trade log")
        body = self.slack.call_args.args[1]
        self.assertIn("XYZ has no stop price or share count in the trade log", body)
        self.assertNotIn("UNKNOWN", body)

    def test_odd_inputs_never_raise_and_still_page(self):
        alerts.alert_gtc_failed("XYZ", "unknown", 0.0, "No stop price or qty=0 in trade log")
        alerts.alert_gtc_failed("ABC", "sell", "n/a", "x")         # type: ignore[arg-type]
        alerts.alert_gtc_failed("DEF", "sell", None, "x")          # type: ignore[arg-type]
        self.assertEqual(self.slack.call_count, 3)

    def test_transport_exception_never_raises(self):
        self.slack.side_effect = RuntimeError("boom")
        alerts.alert_gtc_failed("NVDA", "sell", 192.41, "x")       # must not raise


if __name__ == "__main__":
    unittest.main()
