# ruff: noqa: E501
"""REPORT-STATS-1109: the midday / after-close cards show a per-tier trade record from Alpaca fills, never the old
tracker's frozen all-time stats. Nothing here reaches a network: the ledger read is mocked."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import reporting.pnl_ledger as pl  # noqa: E402
import scripts.audit_slack as asl  # noqa: E402


def _rt(day, tier, pnl, exit_order, sym="AAA"):
    return {"exit_date": day, "tier": tier, "pnl": pnl, "exit_order_id": exit_order, "symbol": sym, "id": f"{day}-{sym}"}


RTS = [
    _rt("2026-10-20", "daytrade", 10.0, "o1"),
    _rt("2026-10-20", "daytrade", 5.0, "o1"),        # same closing order, two lots: ONE trade worth +15
    _rt("2026-10-21", "daytrade", -6.0, "o2"),
    _rt("2026-11-03", "daytrade", 4.0, "o3"),
    _rt("2026-11-04", "intraday", -2.0, "o4"),
    _rt("2026-11-05", "qhm", 20.0, "o5"),
    _rt("2026-11-05", "join_missing", 1.0, "o6"),
]


class TradeStats(unittest.TestCase):
    def test_one_trade_per_closing_order_and_per_tier(self):
        s = pl.trade_stats(RTS)
        self.assertEqual(s["Day"]["trades"], 3)
        self.assertEqual((s["Day"]["wins"], s["Day"]["losses"]), (2, 1))
        self.assertEqual(s["Day"]["win_rate"], 66.7)
        self.assertEqual(s["Day"]["profit_factor"], round(19.0 / 6.0, 2))
        self.assertEqual(s["Day"]["net"], 13.0)
        self.assertEqual(s["Swing + core"]["trades"], 1)
        self.assertEqual(s["QHM"]["profit_factor"], None)          # no losing trade
        self.assertEqual(s["Untagged"]["trades"], 1)
        self.assertEqual(s["All"]["trades"], 6)
        self.assertEqual(s["All"]["net"], 32.0)

    def test_since_keeps_closes_on_or_after_the_date(self):
        s = pl.trade_stats(RTS, since="2026-11-01")
        self.assertEqual(s["Day"]["trades"], 1)
        self.assertEqual(s["All"]["trades"], 4)

    def test_float_noise_is_a_zero_trade_not_a_win(self):
        rts = [_rt("2026-11-02", "daytrade", 0.1, "z"), _rt("2026-11-02", "daytrade", 0.2, "z"),
               _rt("2026-11-02", "daytrade", -0.3, "z")]
        s = pl.trade_stats(rts)
        self.assertEqual((s["Day"]["trades"], s["Day"]["wins"], s["Day"]["losses"]), (1, 0, 0))

    def test_empty(self):
        s = pl.trade_stats([])
        self.assertEqual(s["All"]["trades"], 0)
        self.assertIsNone(s["All"]["win_rate"])


class CardFields(unittest.TestCase):
    def _dollar(self, v):
        return f"${v:,.2f}" if v >= 0 else f"−${abs(v):,.2f}"

    def test_fields_come_from_the_ledger_not_the_eod_tracker_stats(self):
        fields = asl._trade_record_fields({"led": {"round_trips": RTS}}, self._dollar)
        texts = [f["text"] for f in fields]
        self.assertTrue(texts[0].startswith("*Day*\n67% win · 3 trades"))
        self.assertIn("*All*", texts[-1])
        self.assertFalse(any("34.6" in t for t in texts))

    def test_ledger_unreadable_shows_one_unavailable_field(self):
        with mock.patch.object(pl, "fetch_all_fills", side_effect=RuntimeError("401")):
            fields = asl._trade_record_fields(None, self._dollar)
        self.assertEqual(len(fields), 1)
        self.assertIn("unavailable", fields[0]["text"])

    def test_card_ignores_frozen_all_time_stats(self):
        eod = {"all_time_stats": {"win_rate": 34.6, "profit_factor": 0.83, "avg_r_multiple": -0.047}}
        with mock.patch.object(asl, "_trade_record_fields", return_value=[{"type": "mrkdwn", "text": "x"}]) as f:
            with mock.patch.object(pl, "fetch_all_fills", side_effect=RuntimeError("offline")):
                p = asl.build_pnl_fields("midday", eod, positions=[])
        f.assert_called_once()
        self.assertEqual(p["lifetime"], [{"type": "mrkdwn", "text": "x"}])
        self.assertFalse(any("34.6" in str(x) for x in p["lifetime"]))


class CardPassesRewriteGuard(unittest.TestCase):
    """CARD-STATS-1111: from 2026-11-10 the trade-record nets were printed on the card but not allow-listed, so
    validate_no_pnl_rewrite rejected every midday/nightly card (logs: "P&L-rewrite validator blocked card:
    unexpected dollar figure(s)") and the raw-text fallback went out instead."""

    def test_midday_card_with_tier_stats_validates(self):
        led = {"round_trips": RTS, "per_day": {}}
        with mock.patch.object(pl, "fetch_all_fills", return_value=[]), \
                mock.patch.object(pl, "fetch_all_orders", return_value=[]), \
                mock.patch.object(pl, "compute_realized", return_value=led), \
                mock.patch.object(pl, "_get_qhm_symbols", return_value=set()), \
                mock.patch.object(pl, "build_coid_map", return_value={}):
            pnl = asl.build_pnl_fields("midday", {}, positions=[])
        payload = asl.render_card("midday", "2026-11-11", "PASS", pnl, [], dist_footer=[], context_line="x")
        ok, reason = asl.validate_no_pnl_rewrite(payload, pnl["injected_numbers"])
        self.assertTrue(ok, reason)
        self.assertIn("$13.00", pnl["injected_numbers"])          # the Day tier's lifetime net is allow-listed

    def test_a_rogue_figure_is_still_blocked(self):
        payload = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "net $999.99"}}]}
        self.assertFalse(asl.validate_no_pnl_rewrite(payload, ["$13.00"])[0])


if __name__ == "__main__":
    unittest.main()
