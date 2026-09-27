from __future__ import annotations

import unittest
from collections import defaultdict
from unittest import mock

from monthly_review import _build_html, _strategy_edge_html
from reporting import pnl_ledger
from reporting.pnl_ledger import compute_realized
from reporting.report_figures import ReportFigures


def _fill(ts: str, symbol: str, side: str, qty: int, price: float,
          order_id: str) -> dict:
    return {
        "transaction_time": ts, "symbol": symbol, "side": side,
        "qty": str(qty), "price": str(price), "order_id": order_id,
        "id": f"fill:{order_id}:{ts}" if order_id else "",
    }


def _figures(round_trips: list[dict], unmatched_closes: list | None = None,
             missing_order_joins: list | None = None,
             missing_close_identities: list | None = None) -> ReportFigures:
    by_day: dict = defaultdict(list)
    for row in round_trips:
        by_day[row["exit_date"]].append(row)
    return ReportFigures(
        available=True, version="test", ts_pt="test", round_trips=round_trips,
        equity=2500.0, net_deposits=2500.0,
        unmatched_closes=unmatched_closes or [],
        missing_order_joins=missing_order_joins or [],
        missing_close_identities=missing_close_identities or [],
        _by_exit_date=dict(by_day),
    )


class TieredFifo(unittest.TestCase):
    def test_shared_symbol_close_books_pnl_to_each_entry_tier(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "AAPL", "buy", 2, 100, "IN_ENTRY"),
            _fill("2026-09-01T15:00:00Z", "AAPL", "buy", 1, 105, "DT_ENTRY"),
            _fill("2026-09-02T14:00:00Z", "AAPL", "sell", 2, 110, "EXIT_1"),
            _fill("2026-09-02T15:00:00Z", "AAPL", "sell", 1, 111, "EXIT_2"),
        ]
        coids = {"IN_ENTRY": "IN-AAPL-b-1-x", "DT_ENTRY": "DT-AAPL-b-2-x"}
        result = compute_realized(fills, coid_map=coids)
        self.assertEqual([(r["tier"], r["pnl"]) for r in result["round_trips"]],
                         [("intraday", 20.0), ("daytrade", 6.0)])
        self.assertTrue(all(r["lifecycle_complete"] for r in result["round_trips"]))

    def test_untagged_entry_remains_unattributed(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "AAPL", "buy", 1, 100, "OLD"),
            _fill("2026-09-02T14:00:00Z", "AAPL", "sell", 1, 101, "EXIT"),
        ]
        result = compute_realized(fills, coid_map={"OLD": "legacy-order"})
        self.assertEqual(result["round_trips"][0]["tier"], "unattributed")

    def test_strict_history_reads_reject_truncated_shapes(self):
        with mock.patch.object(
                pnl_ledger, "_get_json", return_value={"error": "partial"}):
            with self.assertRaises(RuntimeError):
                pnl_ledger.fetch_all_fills(strict=True)
            with self.assertRaises(RuntimeError):
                pnl_ledger.fetch_all_orders(strict=True)

    def test_fractional_fill_fails_closed_instead_of_becoming_zero(self):
        fills = [_fill("2026-09-01T14:00:00Z", "AAPL", "buy", 0.5, 100, "ENTRY")]
        with self.assertRaisesRegex(ValueError, "fractional fill quantity"):
            compute_realized(fills, coid_map={"ENTRY": "IN-AAPL-b-1-x"})

    def test_invalid_entry_and_close_prices_fail_closed(self):
        for bad_price in ("nan", "inf", "-1", "corrupt", None, ""):
            with self.subTest(kind="entry", price=bad_price):
                fills = [_fill("2026-09-01T14:00:00Z", "AAPL", "buy", 1, 100, "ENTRY")]
                fills[0]["price"] = bad_price
                with self.assertRaisesRegex(ValueError, "fill price"):
                    compute_realized(fills, coid_map={"ENTRY": "IN-AAPL-b-1-x"})
            with self.subTest(kind="close", price=bad_price):
                fills = [
                    _fill("2026-09-01T14:00:00Z", "AAPL", "buy", 1, 100, "ENTRY"),
                    _fill("2026-09-02T14:00:00Z", "AAPL", "sell", 1, 101, "EXIT"),
                ]
                fills[1]["price"] = bad_price
                with self.assertRaisesRegex(ValueError, "fill price"):
                    compute_realized(fills, coid_map={"ENTRY": "IN-AAPL-b-1-x"})

    def test_malformed_quantity_fails_closed(self):
        for bad_qty in ("corrupt", None, ""):
            with self.subTest(qty=bad_qty):
                fills = [_fill("2026-09-01T14:00:00Z", "AAPL", "buy", 1, 100, "ENTRY")]
                fills[0]["qty"] = bad_qty
                with self.assertRaisesRegex(ValueError, "fill quantity"):
                    compute_realized(fills, coid_map={"ENTRY": "IN-AAPL-b-1-x"})

    def test_missing_order_join_is_distinct_from_legacy_unattributed(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "AAPL", "buy", 1, 100, "MISSING"),
            _fill("2026-09-02T14:00:00Z", "AAPL", "sell", 1, 101, "EXIT"),
        ]
        result = compute_realized(fills, coid_map={"EXIT": "legacy-exit"})
        self.assertEqual(result["round_trips"][0]["tier"], "join_missing")
        self.assertEqual(result["missing_order_joins"][0]["order_id"], "MISSING")


class LifecycleMetrics(unittest.TestCase):
    def test_partial_open_is_realized_but_not_a_completed_win(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "MSFT", "buy", 2, 100, "ENTRY"),
            _fill("2026-09-02T14:00:00Z", "MSFT", "sell", 1, 110, "PARTIAL"),
        ]
        result = compute_realized(fills, coid_map={"ENTRY": "IN-MSFT-b-1-x"})
        edge = _figures(result["round_trips"]).strategy_edge_stats()["overall"]
        self.assertEqual(edge["realized_pnl"], 10.0)
        self.assertEqual(edge["completed_trades"], 0)
        self.assertIsNone(edge["win_rate"])
        self.assertEqual(edge["partial_open_lifecycles"], 1)
        self.assertEqual(edge["partial_open_realized_pnl"], 10.0)

    def test_two_exit_legs_merge_into_one_completed_trade(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "MSFT", "buy", 2, 100, "ENTRY"),
            _fill("2026-09-02T14:00:00Z", "MSFT", "sell", 1, 110, "PARTIAL"),
            _fill("2026-09-03T14:00:00Z", "MSFT", "sell", 1, 95, "FINAL"),
        ]
        result = compute_realized(fills, coid_map={"ENTRY": "IN-MSFT-b-1-x"})
        edge = _figures(result["round_trips"]).strategy_edge_stats()["overall"]
        self.assertEqual(edge["realized_pnl"], 5.0)
        self.assertEqual(edge["completed_trades"], 1)
        self.assertEqual(edge["wins"], 1)
        self.assertEqual(edge["win_rate"], 100.0)
        self.assertEqual(edge["partial_open_lifecycles"], 0)

    def test_multiple_entry_fills_for_one_order_are_one_lifecycle(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "NVDA", "buy", 1, 100, "ENTRY"),
            _fill("2026-09-01T14:00:01Z", "NVDA", "buy", 1, 101, "ENTRY"),
            _fill("2026-09-02T14:00:00Z", "NVDA", "sell", 2, 105, "FINAL"),
        ]
        result = compute_realized(fills, coid_map={"ENTRY": "IN-NVDA-b-1-x"})
        edge = _figures(result["round_trips"]).strategy_edge_stats()["overall"]
        self.assertEqual(edge["completed_trades"], 1)
        self.assertEqual(edge["realized_pnl"], 9.0)

    def test_html_uses_fifo_lifecycles_and_discloses_withheld_metadata(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "MSFT", "buy", 1, 100, "ENTRY"),
            _fill("2026-09-03T14:00:00Z", "MSFT", "sell", 1, 95, "FINAL"),
        ]
        result = compute_realized(fills, coid_map={"ENTRY": "IN-MSFT-b-1-x"})
        html, summary = _strategy_edge_html(_figures(result["round_trips"]))
        self.assertEqual(summary["completed_trades"], 1)
        self.assertIn("one live Alpaca fills+orders FIFO snapshot", html)
        self.assertIn("Score/setup and exit-reason panels are withheld", html)
        self.assertNotIn("trade_log.json", html)
        self.assertNotIn("fifo_edge.json", html)

    def test_unmatched_close_withholds_all_strategy_edge_metrics(self):
        html, summary = _strategy_edge_html(
            _figures([], [{"symbol": "AAPL", "qty": 1, "order_id": "CLOSE"}])
        )
        self.assertEqual(summary, {})
        self.assertIn("Strategy Edge unavailable", html)
        self.assertIn("1 closing fill(s)", html)
        self.assertNotIn("Completed trades", html)
        self.assertNotIn("Realized P&amp;L</small>", html)

    def test_missing_order_join_withholds_all_strategy_edge_metrics(self):
        html, summary = _strategy_edge_html(
            _figures([], missing_order_joins=[{"order_id": "MISSING"}])
        )
        self.assertEqual(summary, {})
        self.assertIn("incomplete order attribution", html)
        self.assertNotIn("Completed trades", html)

    def test_monthly_page_renders_integrity_failures_without_metric_summary(self):
        cases = [
            _figures([], unmatched_closes=[{"order_id": "CLOSE"}]),
            _figures([], missing_order_joins=[{"order_id": "MISSING"}]),
            _figures([], missing_close_identities=[{"symbol": "AAPL"}]),
        ]
        for figures in cases:
            with self.subTest(figures=figures), mock.patch(
                    "monthly_review.build_report_figures", return_value=figures):
                html = _build_html(2026, 9, False)
                self.assertIn("Strategy Edge Report — Unavailable", html)
                self.assertIn("Strategy Edge unavailable", html)
                self.assertNotIn("completed</span>", html)
                self.assertNotIn("% WR</span>", html)

    def test_missing_close_fill_identity_withholds_strategy_edge(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "AAPL", "buy", 1, 50, "ENTRY1"),
            _fill("2026-09-01T15:00:00Z", "MSFT", "buy", 1, 150, "ENTRY2"),
            _fill("2026-09-02T14:00:00Z", "AAPL", "sell", 1, 150, ""),
            _fill("2026-09-02T14:00:00Z", "MSFT", "sell", 1, 50, ""),
        ]
        result = compute_realized(
            fills, coid_map={"ENTRY1": "IN-AAPL-1", "ENTRY2": "IN-MSFT-1"})
        self.assertEqual(len(result["missing_close_identities"]), 2)
        html, summary = _strategy_edge_html(_figures(
            result["round_trips"],
            missing_close_identities=result["missing_close_identities"],
        ))
        self.assertEqual(summary, {})
        self.assertIn("incomplete close-fill identity", html)

    def test_drawdown_aggregates_fifo_legs_from_one_exit_fill(self):
        fills = [
            _fill("2026-09-01T14:00:00Z", "AAPL", "buy", 1, 50, "ENTRY1"),
            _fill("2026-09-01T15:00:00Z", "AAPL", "buy", 1, 150, "ENTRY2"),
            _fill("2026-09-02T14:00:00Z", "AAPL", "sell", 2, 100, "EXIT"),
        ]
        coids = {"ENTRY1": "IN-AAPL-1", "ENTRY2": "IN-AAPL-2", "EXIT": "exit"}
        result = compute_realized(fills, coid_map=coids)
        edge = _figures(result["round_trips"]).strategy_edge_stats()["overall"]
        self.assertEqual(edge["realized_pnl"], 0.0)
        self.assertEqual(edge["max_realized_drawdown"], 0.0)


if __name__ == "__main__":
    unittest.main()
