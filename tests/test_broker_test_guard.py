#!/usr/bin/env python3
"""No test may reach the real broker (2026-10-12: an unmocked bracket submit
in a Day-tier test placed 4 real paper UBER bracket orders — 72 sh, ~1.9x
equity, buying power $0 for a whole session). The test package blanks the
Alpaca keys and execution.broker refuses a trading client under test.
Run: PYTHONPATH=. python3 -m pytest -q tests/test_broker_test_guard.py
"""
import os
import unittest

from execution import broker


class BrokerTestGuard(unittest.TestCase):
    def test_trading_client_refused_in_a_test_process(self):
        broker._trading_client = None
        with self.assertRaises(RuntimeError):
            broker._get_trading_client()

    def test_unmocked_order_call_cannot_reach_alpaca(self):
        with self.assertRaises(RuntimeError):
            broker._get_trading_client().submit_order  # noqa: B018

    @unittest.skipUnless(os.environ.get("MTF_TEST_MODE") == "1",
                         "tests/__init__ not executed (unittest discover)")
    def test_keys_blanked_for_tests(self):
        self.assertEqual(os.environ.get("ALPACA_API_KEY"), "")
        self.assertEqual(os.environ.get("ALPACA_SECRET_KEY"), "")

    def test_detection(self):
        self.assertTrue(broker._under_test())


if __name__ == "__main__":
    unittest.main()
