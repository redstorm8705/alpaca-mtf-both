"""No test may page the operator; production alerting is untouched.

2026-09-27: day-tier tests sent five fake "UBER fill INVALIDATED" pages to the real
Slack channel. alerts.py now refuses to reach Slack/ntfy during a test run, and the
tests package blanks the transports before any test module is imported.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import alerts  # noqa: E402


def _no_network(*a, **k):
    raise AssertionError("a test reached the network through alerts.py")


class TestRunNeverPages(unittest.TestCase):
    def setUp(self):
        p = [
            mock.patch.object(
                alerts, "_SLACK_WEBHOOK", "https://hooks.slack.com/services/X"
            ),
            mock.patch.object(alerts, "_NTFY_TOPIC", "topic"),
            mock.patch.object(alerts.urllib.request, "urlopen", _no_network),
        ]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def test_detected_as_test_run(self):
        self.assertTrue(alerts._under_test())

    def test_every_public_sender_is_suppressed(self):
        alerts.send_slack("[UBER] day-tier fill INVALIDATED the setup")
        alerts.alert_entry("UBER", "short", 2, 338.29, 10, 1.0)
        alerts.alert_exit("UBER", "short", -1.0, "stop")
        alerts.alert_kill_switch(-100.0, 0.07, 2500.0)
        alerts.alert_gtc_failed("UBER", "short", 339.24, "x")
        self.assertFalse(alerts.send_slack_blocks([{"type": "section"}], "x"))
        self.assertFalse(alerts.send_slack_blocks([], "x"))
        self.assertFalse(alerts.alert_floor_blind("UBER", "swing", "k", "d", True))
        self.assertFalse(alerts.alert_startup_test())

    def test_day_tier_page_is_suppressed(self):
        from execution import day_trade_manager as dtm

        dtm._page("[UBER] day-tier fill INVALIDATED the setup: short target 338.76")


class ProductionStillPages(unittest.TestCase):
    """The detector must be False for every production entry point, or live alerts
    would be silenced."""

    def _under_test_with(self, argv0):
        env = {k: v for k, v in os.environ.items() if k != "MTF_TEST_MODE"}
        mods = {k: v for k, v in sys.modules.items() if k != "pytest"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.dict(sys.modules, mods, clear=True),
            mock.patch.object(sys, "argv", [argv0]),
        ):
            return alerts._under_test()

    def test_production_entry_points_not_detected(self):
        for argv0 in (
            "main.py",
            "/home/ubuntu/mtf-bot/main.py",
            "live_data_writer.py",
            "run_day_tier.py",
            "backtest_12pt.py",
            "weekly_postmortem.py",
            "reconcile_eod.py",
            "",
        ):
            self.assertFalse(self._under_test_with(argv0), argv0)

    def test_test_runners_detected(self):
        for argv0 in ("python -m unittest", "/usr/bin/pytest", "tests/test_x.py"):
            self.assertTrue(self._under_test_with(argv0), argv0)

    def test_production_send_reaches_network(self):
        sent = []

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _urlopen(req, **k):
            sent.append(req.full_url)
            return _Resp()

        with (
            mock.patch.object(alerts, "_under_test", return_value=False),
            mock.patch.object(
                alerts, "_SLACK_WEBHOOK", "https://hooks.slack.com/services/X"
            ),
            mock.patch.object(alerts.urllib.request, "urlopen", _urlopen),
        ):
            alerts.send_slack("live alert")
        self.assertEqual(sent, ["https://hooks.slack.com/services/X"])


class GuardFilesPresent(unittest.TestCase):
    def test_package_init_and_conftest_mark_test_mode(self):
        for name in ("__init__.py", "conftest.py"):
            text = (_ROOT / "tests" / name).read_text()
            self.assertIn('os.environ["MTF_TEST_MODE"] = "1"', text, name)
            self.assertIn("SLACK_WEBHOOK_URL", text, name)
        # The package init runs only when tests/ is imported as a package
        # (`python -m unittest tests.x`, `discover -s tests -t .`, pytest via
        # conftest). Under `discover -s tests` it does not run; alerts.py still
        # suppresses there via the test-runner argv check (test_detected_as_test_run).
        if "tests" in sys.modules or "pytest" in sys.modules:
            self.assertEqual(os.environ.get("MTF_TEST_MODE"), "1")


if __name__ == "__main__":
    unittest.main()
