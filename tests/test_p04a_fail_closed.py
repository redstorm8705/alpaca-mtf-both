"""P0-4a (2026-09-26): risk limits fail CLOSED on an unexpected error.

The gross-exposure cap used to ALLOW the entry when its own evaluation raised.
It now blocks the entry (a cap that cannot be evaluated never over-exposes).
The normal allow/block paths are unchanged. The run_cycle account-read /
kill-evaluation half is covered structurally in
tests/test_run_cycle_kill_switch_exits.py.
"""
import sys
import unittest
from unittest import mock

# risk_manager imports `requests` at load; no network call runs in these tests.
sys.modules.setdefault("requests", mock.MagicMock())

from execution import risk_manager as rm  # noqa: E402


class _BoomTrades:
    def values(self):
        raise RuntimeError("tracker exploded")


class _Tracker:
    def __init__(self, open_trades):
        self.open_trades = open_trades


def _risk(equity=1000.0):
    with mock.patch.object(rm, "_load_kill_state", return_value={}):
        return rm.RiskManager(equity, daily_start_value=equity)


class GrossCapFailsClosed(unittest.TestCase):
    def test_error_blocks_entry(self):
        r = _risk()
        t = _Tracker(_BoomTrades())
        self.assertFalse(r.check_gross_exposure_for_order(t, 10.0, 5))

    def test_under_cap_allows(self):
        r = _risk(1000.0)
        t = _Tracker({"A": {"qty": 10, "entry_price": 50.0, "status": "open"}})
        with mock.patch.object(rm.config, "MAX_GROSS_EXPOSURE_RATIO", 2.5):
            # 500 open + 100 new <= 2500 cap
            self.assertTrue(r.check_gross_exposure_for_order(t, 10.0, 10))

    def test_over_cap_blocks(self):
        r = _risk(1000.0)
        t = _Tracker({"A": {"qty": 40, "entry_price": 60.0, "status": "open"}})
        with mock.patch.object(rm.config, "MAX_GROSS_EXPOSURE_RATIO", 2.5):
            # 2400 open + 200 new > 2500 cap
            self.assertFalse(r.check_gross_exposure_for_order(t, 10.0, 20))

    def test_malformed_row_skipped_not_fatal(self):
        r = _risk(1000.0)
        t = _Tracker({"A": {"qty": "x", "entry_price": 60.0, "status": "open"}})
        with mock.patch.object(rm.config, "MAX_GROSS_EXPOSURE_RATIO", 2.5):
            self.assertTrue(r.check_gross_exposure_for_order(t, 10.0, 10))


if __name__ == "__main__":
    unittest.main()
