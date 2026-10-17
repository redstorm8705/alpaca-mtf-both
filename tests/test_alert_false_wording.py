#!/usr/bin/env python3
# ruff: noqa: E501
"""alerts.py wording that used to state something false (CEO 2026-10-16 Slack review). Transports are mocked."""
import unittest
from unittest import mock

import tests  # noqa: F401  — test guard
import alerts


class FalseWording(unittest.TestCase):
    def _sent(self, fn, *a, **k):
        with mock.patch.object(alerts, "_send") as send:
            fn(*a, **k)
        send.assert_called_once()
        title, body = send.call_args.args[:2]
        return title, body

    def test_stop_breach_default_says_close_failed_not_stop_placed(self):
        # the only caller (exit_logic hard-stop path) calls this after close_position() FAILED, without gtc_submitted
        title, body = self._sent(alerts.alert_stop_breach, symbol="NVDA", direction="long", current_price=180.0, stop=185.0)
        self.assertIn("CLOSE FAILED", title)
        self.assertIn("close order failed", body)
        self.assertIn("$180.00", body)
        self.assertIn("$185.00", body)
        self.assertNotIn("stop was placed", body.lower())
        self.assertNotIn("gtc stop placed", body.lower())

    def test_stop_breach_gtc_branch_still_available(self):
        _, body = self._sent(alerts.alert_stop_breach, "NVDA", "long", 180.0, 185.0, gtc_submitted=True)
        self.assertIn("GTC stop was placed instead", body)

    def test_stale_feed_does_not_blame_yfinance(self):
        title, body = self._sent(alerts.alert_systemic_stale_feed, median_age=27.4, cycles=3)
        self.assertNotIn("yfinance", (title + body).lower())
        self.assertIn("27 minutes", body)
        self.assertIn("3 scans in a row", body)
        self.assertIn("Alpaca", body)

    def test_rc4_reads_as_fill_price_check(self):
        out = alerts._sanitize("[AAPL] RC-4: ATR exit fill unverified — exit recorded at $0.00.")
        self.assertIn("Fill-price check", out)
        self.assertNotIn("Position-Mismatch", out)


if __name__ == "__main__":
    unittest.main()
