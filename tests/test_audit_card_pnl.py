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


class FeesTotal(unittest.TestCase):
    def test_pages_until_short_page_and_sums(self):
        import reporting.pnl_ledger as pl
        pages = [[{"id": f"a{i}", "net_amount": "-0.01"} for i in range(100)], [{"id": "b", "net_amount": "-0.05"}]]
        with mock.patch.object(pl, "_get_json", side_effect=pages) as gj:
            self.assertEqual(asl._fees_total(pl), -1.05)
        self.assertIn("page_token=a99", gj.call_args_list[1].args[0])

    def test_non_list_page_raises(self):
        import reporting.pnl_ledger as pl
        with mock.patch.object(pl, "_get_json", return_value={"error": "x"}):
            with self.assertRaises(RuntimeError):
                asl._fees_total(pl)


class AccountReadsIsolated(unittest.TestCase):
    def test_fee_or_history_failure_keeps_realized_line(self):
        import reporting.pnl_ledger as pl
        with mock.patch.object(pl, "fetch_all_fills", return_value=[]), \
                mock.patch.object(pl, "fetch_all_orders", return_value=[]), \
                mock.patch.object(pl, "_get_qhm_symbols", return_value=set()), \
                mock.patch.object(pl, "build_coid_map", return_value={}), \
                mock.patch.object(pl, "compute_realized", return_value=_ledger([], {DAY: 3.29}, {})), \
                mock.patch.object(pl, "fetch_account", return_value={"equity": "2720.08", "last_equity": "2736.67"}), \
                mock.patch.object(pl, "fetch_positions", return_value=[]), \
                mock.patch.object(pl, "fetch_net_deposits", return_value=0.0), \
                mock.patch.object(pl, "_get_json", side_effect=RuntimeError("history 503")):
            p = asl.build_pnl_fields("nightly", {}, day=DAY)
        self.assertEqual(p["today"][0]["text"], "*Realized P&L*\n$3.29")
        self.assertFalse(any("Account" in f["text"] for f in p["today"]))
        self.assertIn("fills", p["source_note"])

    def test_fee_page_without_id_raises(self):
        import reporting.pnl_ledger as pl
        with mock.patch.object(pl, "_get_json", return_value=[{"net_amount": "-0.01"}] * 100):
            with self.assertRaises(RuntimeError):
                asl._fees_total(pl)


class FifoDay(unittest.TestCase):
    def _run(self, led):
        with mock.patch("reporting.pnl_ledger.fetch_all_fills", return_value=[{}]), \
                mock.patch("reporting.pnl_ledger.fetch_all_orders", return_value=[{}]), \
                mock.patch("reporting.pnl_ledger._get_qhm_symbols", return_value={"LLY"}), \
                mock.patch("reporting.pnl_ledger.build_coid_map", return_value={}), \
                mock.patch("reporting.pnl_ledger.compute_realized", return_value=led) as cr, \
                mock.patch("reporting.pnl_ledger.fetch_account", return_value={"equity": "2720.48", "last_equity": "2736.67"}), \
                mock.patch("reporting.pnl_ledger.fetch_positions", return_value=[]), \
                mock.patch("reporting.pnl_ledger.fetch_net_deposits", return_value=0.0), \
                mock.patch("reporting.pnl_ledger._get_json", return_value={}), \
                mock.patch.object(asl, "_fees_total", return_value=0.0):
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


def _hist(prev_equity=2736.67):
    # Alpaca stamps each daily point 20:00 ET on its own session date
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    ts = [int(datetime(2026, 10, d, 20, tzinfo=et).timestamp()) for d in (15, 16)]   # sessions before DAY (10-19)
    return {"timestamp": ts, "equity": [2722.49, prev_equity]}


def _src(lifetime=248.07, equity="2720.08", last="2736.67", positions=None, deposits=0.0, fees=-2.66,
         hist=None, per_day=None):
    import reporting.pnl_ledger as pl
    return {"led": {"per_day": per_day or {}, "lifetime": lifetime, "round_trips": [], "per_day_intraday": {},
                    "per_day_qhm": {}},
            "acct": {"equity": equity, "last_equity": last},
            "positions": positions if positions is not None else
            [{"unrealized_pl": "-19.77", "unrealized_intraday_pl": "-10.22"},
             {"unrealized_pl": "-5.54", "unrealized_intraday_pl": "-6.37"}],
            "deposits": deposits, "fees": fees, "history": hist if hist is not None else _hist(),
            "tolerance": 5.0, "check_invariant": pl.check_invariant}


class AccountPnl(unittest.TestCase):
    """ACCOUNT-PNL-1020: whole-account numbers, each cross-checked against an independent source."""

    def test_reconciled_numbers(self):
        a = asl._account_pnl(DAY, _src())
        self.assertEqual(a["today"], -16.59)            # 2720.08 - 2736.67 (account last_equity)
        self.assertEqual(a["today_alt"], -16.59)        # same, from the portfolio-history prior close
        self.assertTrue(a["today_ok"])
        self.assertEqual(a["open"], -25.31)
        self.assertEqual(a["since"], 220.08)            # equity - 2,500 start
        self.assertEqual(a["since_alt"], 220.10)        # lifetime realized 248.07 - fees 2.66 + open -25.31
        self.assertTrue(a["since_ok"])
        self.assertTrue(a["start_assumed"])

    def test_sold_overnight_lot_does_not_false_flag(self):
        # cold-2nd 2026-10-20: lot bought at 100, prior close 110, sold today at 110 -> the account made $0 today
        a = asl._account_pnl(DAY, _src(equity="2550.00", last="2550.00", positions=[], per_day={DAY: 50.0},
                                       hist=_hist(prev_equity=2550.00), lifetime=50.0, fees=0.0))
        self.assertEqual(a["today"], 0.0)
        self.assertTrue(a["today_ok"])

    def test_stale_last_equity_is_flagged(self):
        a = asl._account_pnl(DAY, _src(last="2800.00"))   # account prior close disagrees with portfolio history
        self.assertFalse(a["today_ok"])
        self.assertEqual(a["today_alt"], -16.59)

    def test_since_start_mismatch_is_flagged(self):
        self.assertFalse(asl._account_pnl(DAY, _src(lifetime=400.0))["since_ok"])

    def test_fees_are_part_of_the_reconciliation(self):
        a = asl._account_pnl(DAY, _src(fees=-50.0))      # unexplained fees break the since-start check
        self.assertFalse(a["since_ok"])

    def test_recorded_deposits_are_used(self):
        a = asl._account_pnl(DAY, _src(deposits=2000.0, lifetime=748.07))
        self.assertEqual(a["start"], 2000.0)
        self.assertFalse(a["start_assumed"])
        self.assertTrue(a["since_ok"])

    def test_unreadable_inputs_return_none(self):
        self.assertIsNone(asl._account_pnl(DAY, _src(equity="nan")))
        self.assertIsNone(asl._account_pnl(DAY, _src(hist={})))
        self.assertIsNone(asl._account_pnl(DAY, None))

    def test_card_shows_reconciled_and_flags_unreconciled(self):
        with mock.patch.object(asl, "_ledger_and_account", return_value=_src()):
            p = asl.build_pnl_fields("nightly", {}, day=DAY)
        texts = [f["text"] for f in p["today"]]
        self.assertIn("*Account today*\n−$16.59", texts)
        self.assertIn("*Open positions (unrealized)*\n−$25.31", texts)
        self.assertIn("*Account since start*\n$220.08", texts)
        self.assertTrue(any("$2,720.08 vs $2,500.00 start" in t and "assumed" in t for t in texts))
        self.assertTrue(asl.validate_no_pnl_rewrite(asl.render_card("nightly", DAY, "PASS", p, []),
                                                    p["injected_numbers"])[0])
        with mock.patch.object(asl, "_ledger_and_account", return_value=_src(last="2800.00")):
            p2 = asl.build_pnl_fields("nightly", {}, day=DAY)
        bad = [f["text"] for f in p2["today"] if f["text"].startswith("*Account today*")][0]
        self.assertIn("does not reconcile", bad)
        self.assertTrue(asl.validate_no_pnl_rewrite(asl.render_card("nightly", DAY, "PASS", p2, []),
                                                    p2["injected_numbers"])[0])

    def test_ledger_failure_keeps_old_card(self):
        with mock.patch.object(asl, "_ledger_and_account", return_value=None):
            p = asl.build_pnl_fields("nightly", {"pnl_today": 1.5}, day=DAY)
        self.assertFalse(any("Account" in f["text"] for f in p["today"]))
        self.assertIn("provisional", p["source_note"])


class NightlyFields(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(asl, "_ledger_and_account", return_value=None)   # these tests mock _fifo_day only
        p.start()
        self.addCleanup(p.stop)

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
