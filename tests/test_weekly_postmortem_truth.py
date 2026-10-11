from __future__ import annotations

import unittest
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import weekly_postmortem as wtp


class WeeklyPostmortemTruth(unittest.TestCase):
    def _trade(self, *, size=4):
        return {
            "symbol": "AAPL", "direction": "long", "entry_price": 100.0,
            "exit_price": 101.0, "entry_time": "2026-09-21T09:30:00-07:00",
            "exit_time": "2026-09-21T10:30:00-07:00", "pnl": float(size),
            "exit_reason": "target", "score": "?", "mri_level": "?", "size": size,
            "exit_date": date(2026, 9, 21), "_unmatched": False,
        }

    def test_friday_delta_is_per_share_diagnostic_not_false_dollars(self):
        trades = [self._trade(size=4)]
        with mock.patch.object(wtp, "_load_postmortem", return_value={}), \
             mock.patch.object(wtp, "_had_earnings_during_hold", return_value=False):
            table, slack, stats = wtp._build_wtp_table(
                trades, {"AAPL": 110.0}, {"AAPL": [90, 95, 100, 110]}, {}
            )
        self.assertIn("Δ/share Exit→Fri", table)
        self.assertIn("+$9.00", table)  # per share: 110 Friday - 101 exit
        self.assertIn("Fri Δ/sh +$9.00", slack)
        self.assertNotIn("agg_missed", stats)
        self.assertEqual(stats["positive_friday_diagnostics"], 1)
        # The old report falsely published 4 × $9 = $36 as dollars left on the table.
        self.assertNotIn("$36", table + slack + str(stats))

    def test_prompt_forbids_missing_as_zero_and_false_opportunity_cost(self):
        trade = self._trade()
        trade.update(_stage="Stage 2", _earn=False, _tqi="?", _hold="1h",
                     _wkly=110.0, _delta_s="+$9.00")
        stats = {"total_pnl": 4.0, "count": 1, "winners": 1, "losers": 0,
                 "earnings_cnt": 0, "positive_friday_diagnostics": 1}
        prompt = wtp._build_gemini_prompt(
            "2026-09-21 → 2026-09-25", "table", [trade], stats
        )
        self.assertIn("UNKNOWN, never zero", prompt)
        self.assertIn("must not be\n                     summed", prompt)
        self.assertIn("Do not invent dollar opportunity cost", prompt)
        self.assertNotIn("Aggregate missed directional move", prompt)

    def test_slack_summary_never_claims_dollars_left_on_table(self):
        stats = {"total_pnl": 4.0, "winners": 1, "losers": 0, "unmatched": 0,
                 "earnings_cnt": 0, "positive_friday_diagnostics": 1}
        sent = []
        with mock.patch.object(wtp, "SLACK_WEBHOOK", "test"), \
             mock.patch.object(wtp, "_slack_raw", side_effect=sent.append):
            wtp._post_slack(
                "AAPL    +$4.00", stats, Path("wtp_2026-09-25.md"),
                "2026-09-21 → 2026-09-25",
            )
        self.assertEqual(len(sent), 1)
        self.assertIn("Fri-positive diagnostics `1`", sent[0])
        self.assertNotIn("left on table", sent[0])

    def _rt(self, *, tier="intraday", entry_id="IN-1", pnl=5.0,
            exit_date="2026-10-09", qty=1, complete=True, exit_id="EXIT-1",
            entry_time="2026-09-30T14:00:00Z", exit_time=None):
        exit_time = exit_time or f"{exit_date}T19:00:00Z"
        return {
            "symbol": "AAPL", "direction": "long", "qty": qty,
            "entry_price": 100.0, "exit_price": 100.0 + pnl / qty,
            "pnl": pnl, "entry_time": entry_time,
            "exit_time": exit_time, "exit_date": exit_date,
            "entry_order_id": entry_id, "exit_order_id": exit_id,
            "tier": tier, "lifecycle_complete": complete,
        }

    def test_week_view_keeps_same_symbol_coholds_in_their_entry_tiers(self):
        rows = [
            self._rt(tier="intraday", entry_id="IN-1", pnl=10.0, exit_id="SX"),
            self._rt(tier="daytrade", entry_id="DT-1", pnl=-3.0, exit_id="DX"),
        ]
        trades, accounting = wtp._week_ledger_view(
            rows, date(2026, 10, 5), date(2026, 10, 9)
        )
        self.assertEqual([(t["tier"], t["pnl"]) for t in trades],
                         [("swing", 10.0), ("day", -3.0)])
        self.assertEqual(accounting["weekly_by_tier"]["swing"], 10.0)
        self.assertEqual(accounting["weekly_by_tier"]["day"], -3.0)
        self.assertEqual(accounting["weekly_realized"], 7.0)

    def test_open_partial_is_cash_but_never_a_completed_win(self):
        rows = [self._rt(pnl=8.0, qty=1, complete=False)]
        trades, accounting = wtp._week_ledger_view(
            rows, date(2026, 10, 5), date(2026, 10, 9)
        )
        self.assertEqual(trades, [])
        self.assertEqual(accounting["weekly_realized"], 8.0)
        self.assertEqual(accounting["partial_open_lifecycles"], 1)
        self.assertEqual(accounting["partial_open_weekly_pnl"], 8.0)

    def test_completed_lifecycle_uses_prior_partial_but_week_cash_does_not(self):
        rows = [
            self._rt(pnl=4.0, qty=1, exit_date="2026-10-02", exit_id="P1"),
            self._rt(pnl=-1.0, qty=1, exit_date="2026-10-09", exit_id="F1"),
        ]
        trades, accounting = wtp._week_ledger_view(
            rows, date(2026, 10, 5), date(2026, 10, 9)
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["pnl"], 3.0)
        self.assertEqual(trades[0]["week_realized_pnl"], -1.0)
        self.assertEqual(accounting["weekly_realized"], -1.0)

    def test_unknown_tier_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "tier attribution is incomplete"):
            wtp._week_ledger_view(
                [self._rt(tier="unattributed")],
                date(2026, 10, 5), date(2026, 10, 9),
            )

    def test_related_prior_leg_with_conflicting_tier_fails_closed(self):
        rows = [
            self._rt(tier="unattributed", entry_id="SAME", pnl=10.0,
                     exit_date="2026-10-02", exit_id="OLD"),
            self._rt(tier="intraday", entry_id="SAME", pnl=-1.0,
                     exit_date="2026-10-09", exit_id="FINAL"),
        ]
        with self.assertRaisesRegex(ValueError, "tier attribution is incomplete"):
            wtp._week_ledger_view(
                rows, date(2026, 10, 5), date(2026, 10, 9)
            )

    def test_padded_current_or_prior_entry_identity_fails_closed(self):
        cases = [
            [self._rt(entry_id=" CURRENT ")],
            [
                self._rt(entry_id=" SAME ", exit_date="2026-10-02", exit_id="OLD"),
                self._rt(entry_id="SAME", exit_date="2026-10-09", exit_id="FINAL"),
            ],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(ValueError, "surrounding whitespace"):
                    wtp._week_ledger_view(
                        rows, date(2026, 10, 5), date(2026, 10, 9)
                    )

    def test_padded_current_or_prior_symbol_fails_closed(self):
        current = self._rt()
        current["symbol"] = "AAPL "
        prior = self._rt(entry_id="SAME", exit_date="2026-10-02", exit_id="OLD")
        prior["symbol"] = " AAPL"
        cases = [
            [current],
            [prior, self._rt(entry_id="SAME", exit_id="FINAL")],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaisesRegex(
                    ValueError, "symbol has surrounding whitespace"
                ):
                    wtp._week_ledger_view(
                        rows, date(2026, 10, 5), date(2026, 10, 9)
                    )

    def test_target_week_timestamp_cannot_be_hidden_by_exit_date(self):
        for exit_date in ("", "2026-10-02"):
            with self.subTest(exit_date=exit_date):
                row = self._rt(
                    exit_date=exit_date,
                    exit_time="2026-10-09T19:00:00Z",
                )
                with self.assertRaisesRegex(
                    ValueError, "time is invalid|date/time disagree"
                ):
                    wtp._week_ledger_view(
                        [row], date(2026, 10, 5), date(2026, 10, 9)
                    )

    def test_entry_after_exit_fails_closed(self):
        row = self._rt(
            entry_time="2026-10-10T14:00:00Z",
            exit_time="2026-10-09T19:00:00Z",
        )
        with self.assertRaisesRegex(ValueError, "entry occurs after exit"):
            wtp._week_ledger_view(
                [row], date(2026, 10, 5), date(2026, 10, 9)
            )

    def test_same_entry_order_can_have_multiple_fill_timestamps(self):
        rows = [
            self._rt(entry_id="SAME", exit_date="2026-10-08", exit_id="PARTIAL"),
            self._rt(
                entry_id="SAME",
                entry_time="2026-09-30T14:00:01Z",
                exit_date="2026-10-09",
                exit_id="FINAL",
            ),
        ]
        trades, accounting = wtp._week_ledger_view(
            rows, date(2026, 10, 5), date(2026, 10, 9)
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(accounting["weekly_realized"], 10.0)

    def test_lifecycle_orders_mixed_offsets_by_instant(self):
        rows = [
            self._rt(
                entry_id="SAME",
                exit_time="2026-10-09T20:00:00+00:00",
                exit_id="EARLIER",
            ),
            self._rt(
                entry_id="SAME",
                exit_time="2026-10-09T14:00:00-07:00",
                exit_id="ACTUAL-FINAL",
            ),
        ]
        trades, _accounting = wtp._week_ledger_view(
            rows, date(2026, 10, 5), date(2026, 10, 9)
        )
        self.assertEqual(trades[0]["final_exit_order_id"], "ACTUAL-FINAL")
        self.assertEqual(trades[0]["exit_time"], "2026-10-09T14:00:00-07:00")

    def test_distinct_final_exit_ids_tied_at_same_instant_fail_closed(self):
        def rows_in(order):
            return [
                self._rt(entry_id="SAME", exit_id=exit_id)
                for exit_id in order
            ]

        for order in (("EXIT-A", "EXIT-B"), ("EXIT-B", "EXIT-A")):
            with self.subTest(order=order):
                with self.assertRaisesRegex(
                    ValueError, "final exit identity is ambiguous"
                ):
                    wtp._week_ledger_view(
                        rows_in(order), date(2026, 10, 5), date(2026, 10, 9)
                    )

    def test_canonical_integrity_requires_true_account_reconciliation(self):
        base = dict(
            available=True,
            unmatched_closes=[],
            missing_order_joins=[],
            missing_close_identities=[],
        )
        for invariant in ({}, {"ok": False}, None, {"ok": "true"}):
            with self.subTest(invariant=invariant):
                figures = SimpleNamespace(**base, invariant=invariant)
                self.assertIn(
                    "the account equity reconciliation invariant did not pass",
                    wtp._canonical_integrity_errors(figures),
                )
        figures = SimpleNamespace(**base, invariant={"ok": True})
        self.assertEqual(wtp._canonical_integrity_errors(figures), [])

    def test_unrelated_historical_unattributed_row_is_ignored(self):
        rows = [
            self._rt(tier="unattributed", entry_id="OLD", pnl=10.0,
                     exit_date="2026-10-02", exit_id="OLD-EXIT"),
            self._rt(tier="intraday", entry_id="CURRENT", pnl=-1.0,
                     exit_date="2026-10-09", exit_id="FINAL"),
        ]
        trades, accounting = wtp._week_ledger_view(
            rows, date(2026, 10, 5), date(2026, 10, 9)
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(accounting["weekly_realized"], -1.0)

    def test_malformed_current_week_rows_fail_closed(self):
        cases = [
            ("lifecycle_complete", "false", "completion state"),
            ("pnl", None, "pnl is invalid"),
            ("pnl", float("inf"), "pnl is non-finite"),
            ("qty", True, "quantity is invalid"),
            ("entry_price", 0.0, "entry_price is not positive"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                row = self._rt()
                row[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    wtp._week_ledger_view(
                        [row], date(2026, 10, 5), date(2026, 10, 9)
                    )

    def test_metadata_join_requires_exact_exit_order(self):
        rows = [self._trade()]
        rows[0].update(final_exit_order_id="RIGHT", tier="swing")
        events = [
            {"event": "exit", "symbol": "AAPL", "order_id": "WRONG",
             "reason": "wrong-tier", "score": 1, "ts": "2026-09-21T10:29:59-07:00"},
            {"event": "exit", "symbol": "AAPL", "order_id": "RIGHT",
             "reason": "target", "score": 11, "ts": "2026-09-21T10:30:00-07:00"},
        ]
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as td:
            path = Path(td) / "trade_events.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events))
            with mock.patch.object(wtp, "TRADE_EVENTS", path):
                wtp._enrich_metadata_from_events(
                    rows, date(2026, 9, 21), date(2026, 9, 25)
                )
        self.assertEqual(rows[0]["exit_reason"], "target")
        self.assertEqual(rows[0]["score"], 11)

    def test_metadata_uses_final_exit_id_and_matching_symbol(self):
        rows = [self._trade()]
        rows[0].update(final_exit_order_id="A-FINAL", tier="swing")
        events = [
            {"event": "partial_exit", "symbol": "AAPL", "order_id": "Z-PARTIAL",
             "reason": "partial", "score": 8, "ts": "2026-09-21T10:00:00-07:00"},
            {"event": "exit", "symbol": "MSFT", "order_id": "A-FINAL",
             "reason": "wrong-symbol", "score": 1, "ts": "2026-09-21T10:30:01-07:00"},
            {"event": "exit", "symbol": "AAPL", "order_id": "A-FINAL",
             "reason": "final-stop", "score": 11, "ts": "2026-09-21T10:30:00-07:00"},
        ]
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as td:
            path = Path(td) / "trade_events.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events))
            with mock.patch.object(wtp, "TRADE_EVENTS", path):
                wtp._enrich_metadata_from_events(
                    rows, date(2026, 9, 21), date(2026, 9, 25)
                )
        self.assertEqual(rows[0]["exit_reason"], "final-stop")
        self.assertEqual(rows[0]["score"], 11)


if __name__ == "__main__":
    unittest.main()
