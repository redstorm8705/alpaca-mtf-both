# ruff: noqa: E501
"""SLACK-EOD-1020: the post-market card's P&L and closed count come from Alpaca fills (FIFO), split exactly as the
8:30 PM ET heal splits them (trading tiers = pnl_today, quarterly holds on their own line), not from a string that
never parsed. No network: every Alpaca read is mocked."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nightly_audit as na
import scripts.audit_slack as asl

DAY = "2026-10-19"


def _ledger(trips, intraday, qhm):
    return {"round_trips": trips, "per_day_intraday": intraday, "per_day_qhm": qhm}


class FifoDay(unittest.TestCase):
    def _run(self, led):
        with mock.patch("reporting.pnl_ledger.fetch_all_fills", return_value=[{}]), \
                mock.patch("reporting.pnl_ledger.fetch_all_orders", return_value=[{}]), \
                mock.patch("reporting.pnl_ledger._get_qhm_symbols", return_value={"LLY"}), \
                mock.patch("reporting.pnl_ledger.build_coid_map", return_value={}), \
                mock.patch("reporting.pnl_ledger.compute_realized", return_value=led) as cr:
            out = asl._fifo_day(DAY)
        self.assertEqual(cr.call_args.kwargs["qhm_symbols"], {"LLY"})   # same split inputs as the heal
        return out

    def test_split_and_closing_orders_for_the_day(self):
        trips = [{"exit_date": DAY, "exit_order_id": "o1"}, {"exit_date": DAY, "exit_order_id": "o1"},
                 {"exit_date": DAY, "exit_order_id": "o2"}, {"exit_date": "2026-10-16", "exit_order_id": "o0"}]
        self.assertEqual(self._run(_ledger(trips, {DAY: 16.18}, {DAY: -34.35})), (16.18, -34.35, 2))

    def test_no_closes_is_zero_not_none(self):
        self.assertEqual(self._run(_ledger([], {}, {})), (0.0, 0.0, 0))

    def test_fill_or_order_read_failure_returns_none(self):
        with mock.patch("reporting.pnl_ledger.fetch_all_fills", side_effect=RuntimeError("HTTP 503")):
            self.assertIsNone(asl._fifo_day(DAY))
        with mock.patch("reporting.pnl_ledger.fetch_all_fills", return_value=[]), \
                mock.patch("reporting.pnl_ledger.fetch_all_orders", side_effect=RuntimeError("non-list page")):
            self.assertIsNone(asl._fifo_day(DAY))


class NightlyFields(unittest.TestCase):
    def test_uses_fills_when_readable(self):
        with mock.patch.object(asl, "_fifo_day", return_value=(3.29, 0.0, 1)):
            p = asl.build_pnl_fields("nightly", {"pnl_today": 0.0, "trades_today": 0}, day=DAY)
        self.assertEqual(p["today"][0]["text"], "*Realized P&L*\n$3.29")
        self.assertEqual(p["today"][1]["text"], "*Closed today*\n1")
        self.assertEqual(len(p["today"]), 2)                 # no holds line on a day without a hold sale
        self.assertIn("fills", p["source_note"])
        self.assertEqual(p["injected_numbers"], ["$3.29"])

    def test_hold_sale_is_its_own_line(self):
        # 10/14: trading tiers +16.18, LLY quarterly-hold sale -34.35 — never folded into one number.
        with mock.patch.object(asl, "_fifo_day", return_value=(16.18, -34.35, 4)):
            p = asl.build_pnl_fields("nightly", {}, day=DAY)
        self.assertEqual(p["today"][0]["text"], "*Realized P&L*\n$16.18")
        self.assertEqual(p["today"][2]["text"], "*Quarterly holds (separate)*\n−$34.35")
        self.assertTrue(asl.validate_no_pnl_rewrite(
            asl.render_card("nightly", DAY, "PASS", p, []), p["injected_numbers"])[0])

    def test_loss_is_shown_as_a_loss(self):
        with mock.patch.object(asl, "_fifo_day", return_value=(-9.3, 0.0, 1)):
            p = asl.build_pnl_fields("nightly", {}, day=DAY)
        self.assertEqual(p["today"][0]["text"], "*Realized P&L*\n−$9.30")

    def test_falls_back_to_eod_labeled_with_no_fake_count(self):
        with mock.patch.object(asl, "_fifo_day", return_value=None):
            p = asl.build_pnl_fields("nightly", {"pnl_today": 68.21, "pnl_today_qhm": 0.0, "trades_today": 0}, day=DAY)
        self.assertEqual(p["today"][0]["text"], "*Realized P&L*\n$68.21")
        self.assertEqual(p["today"][1]["text"], "*Closed today*\n—")
        self.assertIn("provisional", p["source_note"])

    def test_unreconciled_alarm_still_wins(self):
        with mock.patch.object(asl, "_fifo_day", return_value=(3.29, 0.0, 1)):
            p = asl.build_pnl_fields("nightly", {"pnl_unreconciled": True}, day=DAY)
        self.assertTrue(p["mismatch"])
        self.assertIn("reconciliation unresolved", p["today"][0]["text"])

    def test_no_day_reads_the_snapshot(self):
        with mock.patch.object(asl, "_fifo_day") as fd:
            p = asl.build_pnl_fields("nightly", {"pnl_today": 1.5})
        fd.assert_not_called()
        self.assertEqual(p["today"][0]["text"], "*Realized P&L*\n$1.50")


class LoadEodDict(unittest.TestCase):
    def _with(self, name, body):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        if body is not None:
            (Path(d.name) / name).write_text(body)
        return mock.patch.object(na, "LOGS_DIR", Path(d.name)), mock.patch.object(na, "AUDIT_DATE", DAY)

    def test_reads_todays_raw_dict(self):
        a, b = self._with(f"eod_{DAY}.json", json.dumps({"date": DAY, "pnl_today": 3.29}))
        with a, b:
            self.assertEqual(na._load_eod_dict()["pnl_today"], 3.29)

    def test_other_days_file_is_ignored(self):
        a, b = self._with(f"eod_{DAY}.json", json.dumps({"date": "2026-10-16", "pnl_today": 68.21}))
        with a, b:
            self.assertEqual(na._load_eod_dict(), {})

    def test_missing_or_corrupt_is_empty(self):
        a, b = self._with(f"eod_{DAY}.json", None)
        with a, b:
            self.assertEqual(na._load_eod_dict(), {})
        a, b = self._with(f"eod_{DAY}.json", "{not json")
        with a, b:
            self.assertEqual(na._load_eod_dict(), {})


if __name__ == "__main__":
    unittest.main()
