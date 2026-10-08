#!/usr/bin/env python3
# ruff: noqa: E501
"""CEO 2026-10-08: the ledger_sync "protected-floor reduction needs OPERATOR CONFIRMATION" page must not fire for the
owner tier's own exit. Live case: QHM's NVDA GTC stop (QH- tag) filled 13:31 UTC; the 13:40 sync paged; QHM's own
auto-heal ran at 14:03. A reduction NOT covered by the owner's own sells (a possible breach) still pages."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_ledger_sync as rls  # noqa: E402
from execution.ownership_guard import tier_of_coid  # noqa: E402

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
YDAY = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
SINCE = f"{TODAY}T13:20:00+00:00"     # last successful reconcile (fills at 13:31 are after it)
PENDING = {"NVDA/qhm": {"was": 1.0, "would_be": 0.0, "net": 0.0, "confirm_cmd": "confirm_ledger_heal.py NVDA qhm 0"}}


def _fill(oid, side="sell", qty=1, day=TODAY, sym="NVDA"):
    return {"symbol": sym, "side": side, "qty": str(qty), "order_id": oid, "transaction_time": f"{day}T13:31:56Z"}


class OwnerExitExplained(unittest.TestCase):
    def test_qhm_own_stop_today_explains(self):
        out = rls._owner_exit_explained(PENDING, [_fill("o1")], {"o1": "QH-NVDA-s-1-b2922ac4"}, tier_of_coid, SINCE)
        self.assertEqual(out, {"NVDA/qhm": True})

    def test_sell_by_another_tier_is_not_explained(self):
        out = rls._owner_exit_explained(PENDING, [_fill("o1")], {"o1": "IN-NVDA-s-1-x"}, tier_of_coid, SINCE)
        self.assertEqual(out, {"NVDA/qhm": False})

    def test_untagged_sell_is_not_explained(self):
        self.assertFalse(rls._owner_exit_explained(PENDING, [_fill("o1")], {"o1": "manual-uuid"}, tier_of_coid, SINCE)["NVDA/qhm"])

    def test_partial_owner_sell_is_not_explained(self):
        p = {"NVDA/qhm": {"was": 3.0, "would_be": 0.0}}
        self.assertFalse(rls._owner_exit_explained(p, [_fill("o1", qty=1)], {"o1": "QH-NVDA-s-1-x"}, tier_of_coid, SINCE)["NVDA/qhm"])

    def test_yesterdays_owner_sell_is_not_explained(self):
        self.assertFalse(rls._owner_exit_explained(PENDING, [_fill("o1", day=YDAY)], {"o1": "QH-NVDA-s-1-x"},
                                                   tier_of_coid, SINCE)["NVDA/qhm"])

    def test_buy_or_other_symbol_ignored(self):
        fills = [_fill("o1", side="buy"), _fill("o2", sym="GOOGL")]
        self.assertFalse(rls._owner_exit_explained(PENDING, fills, {"o1": "QH-NVDA-b-1-x", "o2": "QH-GOOGL-s-1-x"},
                                                   tier_of_coid, SINCE)["NVDA/qhm"])

    def test_forever6_own_sell(self):
        p = {"AMZN/forever6": {"was": 2.0, "would_be": 1.0}}
        out = rls._owner_exit_explained(p, [_fill("o1", sym="AMZN")], {"o1": "F6-AMZN-s-1-x"}, tier_of_coid, SINCE)
        self.assertEqual(out, {"AMZN/forever6": True})

    def test_owner_sell_already_booked_before_last_reconcile_is_not_reused(self):
        """cold-2nd: an owner trim booked at the 10:00 reconcile must not explain a later non-owner sale."""
        later_since = f"{TODAY}T14:00:00+00:00"
        self.assertFalse(rls._owner_exit_explained(PENDING, [_fill("o1")], {"o1": "QH-NVDA-s-1-x"}, tier_of_coid,
                                                   later_since)["NVDA/qhm"])

    def test_no_last_reconcile_pages(self):
        self.assertFalse(rls._owner_exit_explained(PENDING, [_fill("o1")], {"o1": "QH-NVDA-s-1-x"}, tier_of_coid,
                                                   None)["NVDA/qhm"])

    def test_malformed_pending_pages(self):
        self.assertEqual(rls._owner_exit_explained({"bad": {"was": "x"}}, [], {}, tier_of_coid, SINCE), {"bad": False})


class SyncOncePaging(unittest.TestCase):
    def _run(self, fills, coid_map, since=SINCE):
        led = {"healed": False, "reason": "protected-floor shrink — awaiting operator confirmation",
               "pending_heal": dict(PENDING), "shrink": dict(PENDING)}
        slack = mock.Mock()
        with mock.patch("reporting.pnl_ledger.fetch_all_fills", return_value=fills), \
                mock.patch("reporting.pnl_ledger.fetch_all_orders", return_value=[]), \
                mock.patch("reporting.pnl_ledger.fetch_positions", return_value=[]), \
                mock.patch("reporting.pnl_ledger.build_coid_map", return_value=dict(coid_map)), \
                mock.patch("execution.ownership_guard.sync_ledger", return_value=led), \
                mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_quantities", return_value={}), \
                mock.patch.object(rls, "_slack", slack), \
                mock.patch.object(rls, "_read_streak", return_value={"count": 0, "reasons": [], "last_utc": None,
                                                                    "last_healed_utc": since}), \
                mock.patch.object(rls, "_write_streak"), \
                mock.patch.object(rls.time, "sleep"):
            rls.sync_once()
        return slack

    def test_owner_exit_no_operator_page(self):
        slack = self._run([_fill("o1")], {"o1": "QH-NVDA-s-1-x"})
        self.assertFalse(any("OPERATOR CONFIRMATION" in str(c.args[0]) for c in slack.call_args_list))

    def test_no_recorded_healed_pass_pages(self):
        slack = self._run([_fill("o1")], {"o1": "QH-NVDA-s-1-x"}, since=None)
        self.assertTrue(any("OPERATOR CONFIRMATION" in str(c.args[0]) for c in slack.call_args_list))

    def test_healed_pass_records_its_time(self):
        led = {"healed": True, "positions": {}}
        writes = []
        with mock.patch("reporting.pnl_ledger.fetch_all_fills", return_value=[]), \
                mock.patch("reporting.pnl_ledger.fetch_all_orders", return_value=[]), \
                mock.patch("reporting.pnl_ledger.fetch_positions", return_value=[]), \
                mock.patch("reporting.pnl_ledger.build_coid_map", return_value={}), \
                mock.patch("execution.ownership_guard.sync_ledger", return_value=led), \
                mock.patch("execution.quarterly_hold_manager.get_quarterly_hold_quantities", return_value={}), \
                mock.patch.object(rls, "_read_streak", return_value={"count": 0}), \
                mock.patch.object(rls, "_write_streak", side_effect=writes.append), \
                mock.patch.object(rls.time, "sleep"):
            rls.sync_once()
        self.assertIsNotNone(rls._parse_utc(writes[-1]["last_healed_utc"]))

    def test_unexplained_reduction_still_pages(self):
        slack = self._run([_fill("o1")], {"o1": "IN-NVDA-s-1-x"})
        self.assertTrue(any("OPERATOR CONFIRMATION" in str(c.args[0]) for c in slack.call_args_list))


if __name__ == "__main__":
    unittest.main()
