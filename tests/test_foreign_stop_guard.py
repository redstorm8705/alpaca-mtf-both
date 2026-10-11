#!/usr/bin/env python3
# ruff: noqa: E501
"""Foreign-stop defer (2026-10-11; design record logs/design_records/cohold_buys_vs_foreign_stops_2026-10-11.md, Option A):
a Swing / breakout entry defers while ANOTHER tier's (or an untagged) opposite-side stop rests on the symbol, because
Alpaca's wash rule rejects it; a block lasting beyond one session pages once a day.
Run: PYTHONPATH=. python3 -m pytest -q tests/test_foreign_stop_guard.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from execution import foreign_stop_guard as fsg


def _o(side, otype="stop", coid="DT-NVDA-s-1-aaaa", sym="NVDA"):
    return SimpleNamespace(id="o1", symbol=sym, side=side, type=otype, client_order_id=coid)


class ForeignOppositeStop(unittest.TestCase):
    def test_day_stop_blocks_a_swing_buy(self):
        self.assertIsNotNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday", [_o("sell")]))

    def test_untagged_bracket_leg_blocks(self):
        self.assertIsNotNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday", [_o("sell", coid="leg-uuid")]))

    def test_short_entry_blocked_by_a_foreign_buy_stop(self):
        self.assertIsNotNone(fsg.foreign_opposite_stop("NVDA", "sell", "intraday", [_o("buy")]))

    def test_own_tier_same_side_non_stop_or_other_symbol_do_not_block(self):
        self.assertIsNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday", [_o("sell", coid="IN-NVDA-s-1-a")]))
        self.assertIsNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday", [_o("buy")]))
        self.assertIsNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday", [_o("sell", otype="limit")]))
        self.assertIsNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday", [_o("sell", sym="AAPL")]))

    def test_enum_values_and_stop_limit(self):
        o = SimpleNamespace(id="o", symbol="NVDA", side=SimpleNamespace(value="sell"),
                            type=SimpleNamespace(value="stop_limit"), client_order_id="QH-NVDA-s-1-a")
        self.assertIsNotNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday", [o]))

    def test_unreadable_book_does_not_defer(self):
        with mock.patch("execution.broker.get_open_orders", return_value=None):
            self.assertIsNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday"))
        with mock.patch("execution.broker.get_open_orders", side_effect=RuntimeError("down")):
            self.assertIsNone(fsg.foreign_opposite_stop("NVDA", "buy", "intraday"))


class Deferrals(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(fsg, "_STATE", Path(tmp.name) / "d.json")
        p.start()
        self.addCleanup(p.stop)
        self.slack = mock.Mock()
        p2 = mock.patch.dict("sys.modules", {"alerts": SimpleNamespace(send_slack=self.slack)})
        p2.start()
        self.addCleanup(p2.stop)

    def test_same_session_block_logs_without_a_page(self):
        with self.assertLogs(fsg.logger, "INFO") as cm:
            fsg.note_deferral("NVDA", "intraday", _o("sell"))
        self.assertIn(fsg.TAG, cm.output[0])
        self.slack.assert_not_called()

    def _prev_session(self):
        from datetime import datetime, timedelta
        d = datetime.now(fsg.ET).date() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()

    def test_block_from_an_earlier_session_pages_once_a_day(self):
        prev = self._prev_session()
        fsg._save({"NVDA|intraday": {"first_seen": "2026-01-02", "last_seen": prev, "blocker": "daytrade", "paged": ""}})
        fsg.note_deferral("NVDA", "intraday", _o("sell"))
        fsg.note_deferral("NVDA", "intraday", _o("sell"))
        self.slack.assert_called_once()

    def test_gap_or_new_blocker_restarts_the_clock(self):
        fsg._save({"NVDA|intraday": {"first_seen": "2026-01-02", "last_seen": "2026-01-05", "blocker": "daytrade",
                                     "paged": ""}})
        fsg.note_deferral("NVDA", "intraday", _o("sell"))                    # stale record: no false page
        self.slack.assert_not_called()
        prev = self._prev_session()
        fsg._save({"NVDA|intraday": {"first_seen": "2026-01-02", "last_seen": prev, "blocker": "qhm", "paged": ""}})
        fsg.note_deferral("NVDA", "intraday", _o("sell"))                    # blocker changed (now daytrade)
        self.slack.assert_not_called()

    def test_clear_forgets_the_block(self):
        fsg.note_deferral("NVDA", "intraday", _o("sell"))
        fsg.clear_deferral("NVDA", "intraday")
        self.assertEqual(fsg._load(), {})


if __name__ == "__main__":
    unittest.main()
