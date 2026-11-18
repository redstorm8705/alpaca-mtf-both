# ruff: noqa: E501
"""QHM-OWNERBUY-1113 — owner "buy more" (market or held limit) through QHM's stop-safe add, and the quick commands
(design record logs/design_records/qhm_owner_buy_2026-11-13.md, rev 2). Every broker, quote, bar and alert call is
mocked; the request file, the state and trade_events rows go to a temp directory. Nothing reaches Alpaca or Slack."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  — test guard (MTF_TEST_MODE, blank broker keys)
from execution import broker  # noqa: F401  — patched below, never called for real
from execution import quarterly_hold_manager as qm
from scripts import qhm_owner as cli
from tests import test_qhm_stop_safe_add as ssa

NOW = datetime(2026, 11, 13, 11, 0, tzinfo=qm.ET)
CREATED = "2026-11-13T06:30:00-08:00"


def _pos(sym="NVDA", qty=5, stop=140.0, avg=173.51, **kw):
    return qm.HoldPosition(symbol=sym, direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                           qty_filled=qty, qty_total=qty, entry_day="2026-09-29", stop_price=stop,
                           stop_order_id="stop-1", avg_entry_price=avg, restop_qty=qty, **kw)


def _stop(price, qty, sym="NVDA", oid="stop-2"):
    return SimpleNamespace(id=oid, symbol=sym, client_order_id=f"QH-{sym}-s-1-a", side="sell", type="stop",
                           status="new", time_in_force="gtc", qty=qty, filled_qty=0, stop_price=price)


def _req(qty=2, limit=None, until="2026-11-20", status="armed", **kw):
    return {"qty": qty, "requested_qty": qty, "filled_qty": 0, "limit": limit, "until": until, "status": status,
            "entry_day": "2026-09-29", "created_at": CREATED, **kw}


class _Base(unittest.TestCase):
    def setUp(self):
        qm._OWNER_STOP_DAY.clear()
        qm._OWNER_STOP_GAP_DAY.clear()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()
        self.req = self.tmp / "qhm_owner_buys.json"
        self.market_open = True
        self.fill_price = 151.02
        for p in (mock.patch.object(qm, "_OWNER_BUY_PATH", self.req), mock.patch.object(qm, "_ROOT", self.tmp),
                  mock.patch("execution.broker.is_market_open", side_effect=lambda: self.market_open),
                  mock.patch("execution.broker.get_order", side_effect=lambda oid: SimpleNamespace(
                      id=oid, filled_avg_price=self.fill_price, filled_at=datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc)))):
            p.start()
            self.addCleanup(p.stop)

    def _file(self, d):
        self.req.write_text(json.dumps(d))

    def _reqs(self):
        return json.loads(self.req.read_text())

    def _rows(self):
        p = self.tmp / "logs" / "trade_events.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def _mgr(self, *positions, ask=151.0, room=10, equity=2500.0, bp=100000.0, fill=None, outcome=None, book_qty=None):
        """A manager whose add path is a stand-in for _stop_safe_add: `fill(qty)` shares fill and the stop is re-placed
        for the new count (what the real helper does); `outcome` overrides the result without changing the hold."""
        m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
        m.dry_run = False
        m._positions = {p.symbol: p for p in positions}
        m.alerts = []
        m._alert = m.alerts.append
        m._save_state = lambda: True
        m._now_et = lambda: NOW
        m.quote = (ask, "fresh")
        m._reentry_ask = lambda s: m.quote
        m._get_account_equity = lambda: equity
        m._qhm_cap_room_shares = lambda s, px, eq: room
        m.broker = SimpleNamespace(get_account=lambda: SimpleNamespace(regt_buying_power=bp))
        m.adds = []

        def add(pos, qty, px, label, **kw):
            m.adds.append((pos.symbol, qty, px, label, kw))
            if outcome is not None:
                return outcome
            got = qty if fill is None else fill(qty)
            pos.qty_filled += got
            pos.stop_order_id = "stop-2"
            m._last_add_order_id = "add-1"       # what the real helper records for the fill-price read
            return qm.AddOutcome(True, got)
        m._stop_safe_add = add
        m._get_open_orders = lambda: [_stop(p.stop_price, p.qty_filled if book_qty is None else book_qty, p.symbol)
                                      for p in m._positions.values()]
        m.restops = []

        def restop(pos, book):            # QHM-RESTOP-1112's "no change" outcome: it re-baselines the share count
            m.restops.append(pos.symbol)
            pos.restop_qty = pos.qty_filled
        m._maybe_restop_after_add = restop
        m._owner_buy_cycle = None
        m._owner_buy_entry_halt = lambda: None      # no entry halt (the halt check has its own tests)
        return m

    def _cycle(self, m):
        """One cycle's owner-buy work, in the order _run_weekly_check does it."""
        m._owner_buy_cycle = m._load_owner_buys()
        try:
            m._owner_buy_sweep()
            return [m._maybe_owner_buy(p) for p in list(m._positions.values())]
        finally:
            m._owner_buy_cycle = None


class MarketBuy(_Base):
    def test_market_buy_runs_through_the_add_path_and_the_stop_is_recalculated_on_the_fill(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)
        del m._maybe_restop_after_add              # the real QHM-RESTOP-1112 pass
        m._cover_inputs = lambda p: ((p.qty_filled, 151.0), "")
        m._other_tier_claim = lambda s: 1.0         # not the sole holder -> QHM's own average is used
        m._overnight_gap_margin = lambda s, px: 1.5
        m._design_stop_price = lambda s, avg: 147.48
        with mock.patch("execution.broker.replace_stop_order",
                        side_effect=lambda sym, oid, px, qty=None: _stop(px, qty, oid="stop-3")) as rep:
            self.assertEqual(self._cycle(m), [True])
        self.assertEqual(m.adds, [("NVDA", 2, 151.0, "owner-buy", {"max_limit_price": None, "stamp_add_date": False})])
        rep.assert_called_once_with("NVDA", "stop-2", 147.48, qty=7)       # resized to 7 shares at the recalculated price
        self.assertEqual((pos.qty_filled, pos.stop_price, pos.stop_order_id, pos.restop_qty), (7, 147.48, "stop-3", 7))
        self.assertAlmostEqual(pos.avg_entry_price, (173.51 * 5 + 151.02 * 2) / 7)
        self.assertEqual(self._reqs(), {})                                  # fully bought: the request is gone
        line = m.alerts[-1]
        self.assertIn("bought 2 of 2 @ $151.02", line)
        self.assertIn("stop $140.00 -> $147.48 for 7 sh", line)
        self.assertIn("at risk at the stop; QHM losses are outside the daily kill switch", line)
        self.assertTrue(line.startswith("✅"))
        row = [r for r in self._rows() if r["event"] == "qhm_owner_buy" and r["action"] == "filled"][0]
        self.assertEqual((row["price"], row["size"], row["stop_before"], row["stop_after"], row["size_after_caps"]),
                         (151.02, 2, 140.0, 147.48, 2))
        self.assertIn("accepted", [r["action"] for r in self._rows() if r["event"] == "qhm_owner_buy"])

    def test_partial_fill_leaves_the_remainder_as_a_request(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos, fill=lambda q: 1)
        self._cycle(m)
        rec = self._reqs()["NVDA"]
        self.assertEqual((rec["qty"], rec["status"], rec["filled_qty"], rec["requested_qty"]), (1, "armed", 1, 2))
        self.assertIn("bought 1 of 2; 1 still waiting", m.alerts[-1])
        self.assertEqual(self._rows()[-1]["action"], "partial")
        # the next cycle buys the remainder; the line counts both fills
        m2 = self._mgr(pos)
        self._cycle(m2)
        self.assertEqual((m2.adds[0][1], self._reqs()), (1, {}))
        self.assertIn("bought 2 of 2", m2.alerts[-1])

    def test_no_fill_keeps_the_request_and_pages_once_a_day(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos, fill=lambda q: 0)
        self._cycle(m)
        self._cycle(m)
        self.assertEqual((self._reqs()["NVDA"]["qty"], self._reqs()["NVDA"]["status"]), (2, "armed"))
        lines = [a for a in m.alerts if "bought 0 of 2; 2 still waiting" in a]
        self.assertEqual(len(lines), 1)
        self.assertIn("no order is left at the broker", lines[0])
        self.assertEqual(m.restops, [])

    def test_fill_price_unreadable_is_reported_pending_never_estimated(self):
        self._file({"NVDA": _req(1)})
        pos = _pos()
        self.fill_price = None
        m = self._mgr(pos)
        self._cycle(m)
        self.assertIn("(1 @ price pending; see Alpaca)", m.alerts[-1])
        row = self._rows()[-1]
        self.assertEqual((row["price"], row["price_pending"]), (None, True))
        self.assertEqual(pos.avg_entry_price, 173.51)

    def test_market_closed_defers_and_says_so(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)
        self.market_open = False
        self.assertEqual(self._cycle(m), [False])
        self._cycle(m)
        self.assertEqual(m.adds, [])
        waits = [a for a in m.alerts if "the market is closed; it runs after the open" in a]
        self.assertEqual(len(waits), 1)
        self.assertEqual(self._reqs()["NVDA"]["status"], "armed")
        self.assertIn("deferred", [r["action"] for r in self._rows()])

    def test_add_path_refusal_re_arms_the_request(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos, outcome=qm.AddOutcome(False, 0))
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual(self._reqs()["NVDA"]["status"], "armed")
        self.assertTrue(any("placed no order this cycle" in a for a in m.alerts))

    def test_unproven_result_stays_reserved_pages_once_and_never_buys_again(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos, outcome=qm.AddOutcome(True, 0, open_buy=True))
        self._cycle(m)
        self._cycle(m)
        self._cycle(m)
        self.assertEqual(len(m.adds), 1)
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        pages = [a for a in m.alerts if "result is not confirmed" in a]
        self.assertEqual(len(pages), 1)
        self.assertIn("cancel NVDA buy", pages[0])
        self.assertNotIn("--force", pages[0])

    def test_reservation_that_cannot_be_written_places_no_buy(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos)
        m._update_owner_buy = lambda *a: False
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual(m.adds, [])


class LimitBuy(_Base):
    def test_limit_buy_waits_for_the_ask_then_buys_capped_at_the_owner_price(self):
        self._file({"NVDA": _req(2, limit=160.0)})
        pos = _pos()
        m = self._mgr(pos, ask=161.0)
        with mock.patch("execution.broker.get_open_orders") as book, \
                mock.patch("execution.broker.replace_stop_order") as rep:
            self.assertEqual(self._cycle(m), [False])
            self.assertEqual(self._cycle(m), [False])
            self.assertEqual(m.adds, [])                           # held by QHM: nothing sent, nothing rests
            self.assertEqual(self._reqs()["NVDA"]["status"], "armed")
            m.quote = (159.9, "fresh")
            self.assertEqual(self._cycle(m), [True])
            book.assert_not_called()
            rep.assert_not_called()
        self.assertEqual(m.adds, [("NVDA", 2, 159.9, "owner-buy", {"max_limit_price": 160.0, "stamp_add_date": False})])
        self.assertEqual(self._reqs(), {})

    def test_ask_equal_to_the_limit_triggers(self):
        self._file({"NVDA": _req(1, limit=160.0)})
        m = self._mgr(_pos(), ask=160.0)
        self.assertEqual(self._cycle(m), [True])

    def test_stale_quote_skips_the_cycle(self):
        self._file({"NVDA": _req(2)})
        m = self._mgr(_pos())
        m.quote = (None, "quote_stale")
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual(m.adds, [])
        self.assertTrue(any("no fresh two-sided quote" in a for a in m.alerts))

    def test_one_sided_and_old_quotes_are_refused_by_the_real_quote_check(self):
        self._file({"NVDA": _req(2, limit=160.0)})
        m = self._mgr(_pos())
        del m._reentry_ask                          # the real fresh two-sided check
        for quote in ({"bid": 0.0, "ask": 151.0, "ts": NOW.isoformat()},                       # one-sided
                      {"bid": 150.9, "ask": 151.0, "ts": "2026-11-13T10:00:00-05:00"}):         # an hour old
            with mock.patch("data.alpaca_data.get_latest_quote", return_value=quote):
                self.assertEqual(self._cycle(m), [False])
        self.assertEqual(m.adds, [])
        with mock.patch("data.alpaca_data.get_latest_quote",
                        return_value={"bid": 150.9, "ask": 151.0, "ts": NOW.isoformat()}):
            self.assertEqual(self._cycle(m), [True])
        self.assertEqual(m.adds[0][2], 151.0)

    def test_expiry_deletes_the_request_only(self):
        self._file({"NVDA": _req(2, limit=160.0, until="2026-11-12")})
        m = self._mgr(_pos())
        with mock.patch("execution.broker.cancel_order") as cancel, mock.patch("execution.broker.get_open_orders") as book:
            self.assertEqual(self._cycle(m), [False])
            cancel.assert_not_called()
            book.assert_not_called()
        self.assertEqual((self._reqs(), m.adds), ({}, []))
        self.assertTrue(any("expired" in a and "No order was at the broker" in a for a in m.alerts))
        self.assertEqual(self._rows()[-1]["action"], "expired")

    def test_last_day_is_still_active(self):
        self._file({"NVDA": _req(1, until="2026-11-13")})
        m = self._mgr(_pos())
        self.assertEqual(self._cycle(m), [True])


class Caps(_Base):
    def test_per_name_cap_reduces_and_pages(self):
        # equity 2500, 20% = $500; 4 shares at $100 held -> room for 1 more (owner price $100: order max $100)
        self._file({"NVDA": _req(2, limit=100.0)})
        pos = _pos(qty=4, stop=80.0, avg=100.0)
        m = self._mgr(pos, ask=100.0, room=1)
        self._cycle(m)
        self.assertEqual(m.adds[0][1], 1)
        self.assertIn("bought 1 of 2; 1 still waiting: the 20% per-stock limit", m.alerts[-1])
        rec = self._reqs()["NVDA"]                  # the owner's remainder stays, re-checked every cycle
        self.assertEqual((rec["qty"], rec["requested_qty"], rec["filled_qty"], rec["status"]), (1, 2, 1, "armed"))
        row = self._rows()[-1]
        self.assertEqual((row["size"], row["size_after_caps"], row["request_qty"]), (1, 1, 2))

    def test_no_room_keeps_the_request_and_pages_once_a_day(self):
        self._file({"NVDA": _req(2)})
        pos = _pos(qty=5, stop=80.0, avg=100.0)
        m = self._mgr(pos, ask=100.0, room=0)
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual(m.adds, [])
        self.assertEqual((self._reqs()["NVDA"]["qty"], self._reqs()["NVDA"]["status"]), (2, "armed"))
        pages = [a for a in m.alerts if "bought 0 of 2; 2 still waiting: the 20% per-stock limit leaves no room" in a]
        self.assertEqual(len(pages), 1)
        self.assertEqual([r["action"] for r in self._rows() if r["action"] == "reduced"], ["reduced"])

    def test_ten_reduced_to_three_then_refused_then_room_for_all(self):
        # equity 2500: 20% = $500; 2 shares at $100 held -> room for 3 under the per-stock limit
        self._file({"NVDA": _req(10, limit=100.0)})
        pos = _pos(qty=2, stop=80.0, avg=100.0)
        m = self._mgr(pos, ask=100.0, room=3)
        self._cycle(m)
        rec = self._reqs()["NVDA"]
        self.assertEqual((rec["qty"], rec["requested_qty"], rec["filled_qty"], rec["status"]), (7, 10, 3, "armed"))
        self.assertIn("bought 3 of 10; 7 still waiting: the 20% per-stock limit", m.alerts[-1])
        # next cycle: room 10 but the add path refuses (no order) -> the owner's 7 stay requested, not the 3
        qm._OWNER_STOP_DAY.clear()
        m2 = self._mgr(pos, ask=100.0, room=10, outcome=qm.AddOutcome(False, 0))
        self._cycle(m2)
        self.assertEqual(m2.adds[0][1], 7)
        rec = self._reqs()["NVDA"]
        self.assertEqual((rec["qty"], rec["filled_qty"], rec["status"], rec.get("attempt_qty")), (7, 3, "armed", None))
        # then room for everything: all 7 are bought and the request is done
        m3 = self._mgr(pos, ask=100.0, room=10)
        self._cycle(m3)
        self.assertEqual((m3.adds[0][1], self._reqs()), (7, {}))
        self.assertIn("bought 10 of 10", m3.alerts[-1])
        self.assertEqual(pos.qty_filled, 12)

    def test_scratch_reduced_then_refused_keeps_the_remainder(self):
        self._file({"NVDA": _req(5)})
        m = self._mgr(_pos(), room=2, outcome=qm.AddOutcome(False, 0))
        self._cycle(m)
        self.assertEqual((self._reqs()["NVDA"]["qty"], self._reqs()["NVDA"]["status"]), (5, "armed"))

    def test_cap_unreadable_waits(self):
        self._file({"NVDA": _req(2)})
        m = self._mgr(_pos(), room=None)
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual((m.adds, self._reqs()["NVDA"]["status"]), ([], "armed"))

    def test_buying_power_reduces(self):
        self._file({"NVDA": _req(3)})
        m = self._mgr(_pos(), bp=320.0)             # two shares at $151
        self._cycle(m)
        self.assertEqual(m.adds[0][1], 2)
        self.assertIn("bought 2 of 3; 1 still waiting: buying power", m.alerts[-1])

    def test_buying_power_is_checked_at_the_orders_highest_price_not_the_ask(self):
        # bp 302 = exactly 2 x the $151 ask, but the marketable limit is $151.15 -> only 1 share is affordable
        self._file({"NVDA": _req(2)})
        m = self._mgr(_pos(), bp=302.0)
        self._cycle(m)
        self.assertEqual(m.adds[0][1], 1)
        # with an owner price of $151.00 the order can never be priced above $151 -> 2 shares fit
        self._file({"NVDA": _req(2, limit=151.0)})
        m = self._mgr(_pos(), bp=302.0)
        self._cycle(m)
        self.assertEqual(m.adds[0][1], 2)

    def test_aggregate_cap_with_several_requests_one_at_a_time(self):
        # equity 2500: 40% = $1000 of QHM; held 1 + 1 + 4 shares at $100 = $600 -> room for 4 shares in total.
        # NVDA asks 3 (fits), then GOOGL asks 3: the real cap gate now sees NVDA's fill and leaves room for 1.
        self._file({"NVDA": _req(3, limit=100.0), "GOOGL": _req(3, limit=100.0)})
        nvda, googl, lly = (_pos("NVDA", 1, 80.0, 100.0), _pos("GOOGL", 1, 80.0, 100.0), _pos("LLY", 4, 80.0, 100.0))
        m = self._mgr(nvda, googl, lly, ask=100.0)
        del m._qhm_cap_room_shares                  # the real QHM cap gate
        m._get_live_price = lambda s: 100.0
        m._overnight_room_dollars = lambda eq: 1e9
        m._gf_shares = {}
        self._cycle(m)
        self.assertEqual([(a[0], a[1]) for a in m.adds], [("NVDA", 3), ("GOOGL", 1)])
        self.assertEqual((nvda.qty_filled, googl.qty_filled), (4, 2))
        line = [a for a in m.alerts if a.startswith("✅ GOOGL")][0]
        self.assertIn("bought 1 of 3; 2 still waiting: the 40% QHM total / overnight exposure limit", line)
        self.assertEqual(list(self._reqs()), ["GOOGL"])
        self.assertEqual(self._reqs()["GOOGL"]["qty"], 2)

    def test_at_most_the_per_cycle_number_of_adds(self):
        syms = ("NVDA", "GOOGL", "LLY")
        self._file({s: _req(1) for s in syms})
        m = self._mgr(*[_pos(s) for s in syms])
        self._cycle(m)
        self.assertEqual(len(m.adds), qm._TRANCHE_MAX_ADDS_PER_CYCLE)
        self._cycle(m)
        self.assertEqual(len(m.adds), 3)


class StopAfterFill(_Base):
    def test_active_owner_stop_wins_over_the_recalculated_stop(self):
        self._file({"NVDA": _req(2)})
        pos = _pos(stop=147.48, owner_stop_until="2026-11-20")
        m = self._mgr(pos)
        del m._maybe_restop_after_add              # the real pass: it must stand down for the owner's stop
        m._design_stop_price = lambda s, avg: 140.0
        with mock.patch("execution.broker.replace_stop_order") as rep:
            self._cycle(m)
        rep.assert_not_called()
        self.assertEqual((pos.qty_filled, pos.stop_price, pos.restop_qty), (7, 147.48, 5))
        self.assertIn("stop $147.48 kept (your owner stop) for 7 sh", m.alerts[-1])
        self.assertTrue(m.alerts[-1].startswith("✅"))

    def test_broker_stop_quantity_differing_from_qhm_shares_pages(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos, book_qty=5)              # the stop at the broker still covers 5 of 7
        self._cycle(m)
        self.assertTrue(any("the stop at Alpaca covers 5 share(s) in 1 QH stop order(s) but QHM holds 7" in a
                            for a in m.alerts))
        self.assertIn("stop_mismatch", [r["action"] for r in self._rows()])
        self.assertTrue(m.alerts[-1].startswith("🚨 ACTION"))
        self.assertFalse([r for r in self._rows() if r["action"] == "filled"][0]["stop_verified"])

    def test_stop_left_pending_by_the_add_is_not_a_proven_result(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)
        real_add = m._stop_safe_add

        def add(p, *a, **kw):
            out = real_add(p, *a, **kw)
            p.state = qm.HoldState.PENDING_STOP_REPLACE
            return out
        m._stop_safe_add = add
        self.assertEqual(self._cycle(m), [True])
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        self.assertTrue(any("result is not confirmed" in a for a in m.alerts))

    def test_unbaselined_hold_is_baselined_before_the_add_so_the_recalculation_runs(self):
        self._file({"NVDA": _req(1)})
        pos = _pos()
        pos.restop_qty = None
        m = self._mgr(pos)
        seen = []
        m._maybe_restop_after_add = lambda p, book: seen.append(p.restop_qty)
        self._cycle(m)
        self.assertEqual(seen, [5])


class RequestFile(_Base):
    def test_unreadable_file_buys_nothing_and_pages_once(self):
        self.req.write_text("{not json")
        m = self._mgr(_pos())
        with self.assertLogs(qm.logger, level="CRITICAL"):
            self.assertEqual(self._cycle(m), [False])
            self._cycle(m)
        self.assertEqual(m.adds, [])
        self.assertEqual(len([a for a in m.alerts if "owner-buy file is unreadable" in a]), 1)
        self.assertEqual(self.req.read_text(), "{not json")

    def test_missing_file_is_no_request(self):
        m = self._mgr(_pos())
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual((m.adds, m.alerts), ([], []))

    def test_malformed_and_unheld_requests_are_removed_with_a_page(self):
        self._file({"NVDA": {"qty": "two", "status": "armed", "created_at": CREATED, "until": "2026-11-20"},
                    "AAPL": _req(1), "GOOGL": _req(1, entry_day="2026-06-01")})
        m = self._mgr(_pos(), _pos("GOOGL"))
        self._cycle(m)
        self.assertEqual((self._reqs(), m.adds), ({}, []))
        self.assertEqual(len([a for a in m.alerts if "refused" in a]), 3)

    def test_request_cancelled_or_replaced_during_the_buy_is_not_rewritten(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos, fill=lambda q: 1)
        real_add = m._stop_safe_add

        def add(p, *a, **kw):
            self._file({"NVDA": _req(5, created_at="2026-11-13T08:00:00-08:00")})   # the owner replaced it meanwhile
            return real_add(p, *a, **kw)
        m._stop_safe_add = add
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["qty"], 5)
        self.assertIn("You replaced this request while the buy ran", m.alerts[-1])
        self.assertIn("bought 1 of 2 @", m.alerts[-1])
        self.assertNotIn("still waiting", m.alerts[-1])

    def test_one_file_read_per_cycle(self):
        self._file({"NVDA": _req(2, limit=100.0)})
        pos = _pos()
        m = self._mgr(pos)
        reads = []
        real = m._load_owner_buys
        m._load_owner_buys = lambda: reads.append(1) or real()
        m._load_owner_stops = lambda: ({}, "")

        def body():
            m._owner_buy_sweep()
            m._maybe_owner_buy(pos)
            m._maybe_owner_buy(pos)
        m._run_weekly_check = body
        m.run_weekly_check()
        self.assertEqual(len(reads), 1)
        self.assertIsNone(m._owner_buy_cycle)

    def test_the_cycle_calls_the_owner_buy_pass_for_an_active_hold(self):
        self._file({"NVDA": _req(1, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos)
        m._owner_sale_retry_day = {}
        m._get_open_orders = lambda: None           # preflight: blocked snapshot -> hold skipped; then readable
        m.cancel_stray_sell_orders = lambda **kw: 0
        m._maybe_owner_buy_adoption = lambda p: False
        m._maybe_apply_owner_stop = lambda p, book: None
        m._maybe_adopt_proven_shares = lambda p, book: None
        m._reconcile_stop_coverage = lambda p, book, **kw: (False, False)
        m._detect_external_close = lambda p, r: False
        m._strict_resync_qty = lambda p, *a: p.qty_filled
        m._maybe_owner_trim = lambda p: False
        calls = []
        m._maybe_owner_buy = lambda p: calls.append(p.symbol) or True
        m._get_open_orders = lambda: []
        m._run_weekly_check()
        self.assertEqual(calls, ["NVDA"])


class Unproven(_Base):
    def test_scratch_ambiguous_add_never_rearms_or_rebuys(self):
        """Ported from the reviewers' scratch proof: AddOutcome(True, 0) with the hold left PENDING (what
        _hold_ambiguous_add does) must not re-arm the request, even after recovery restores the hold."""
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)
        calls = []

        def add(p, qty, px, label, **kw):
            calls.append(qty)
            if len(calls) == 1:
                p.state = qm.HoldState.PENDING_STOP_REPLACE
                return qm.AddOutcome(True, 0)
            p.qty_filled += qty
            return qm.AddOutcome(True, qty)
        m._stop_safe_add = add
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        pos.qty_filled += 2                          # recovery later proves the first order filled 2
        pos.state = qm.HoldState.ACTIVE
        qm._OWNER_STOP_DAY.clear()
        self._cycle(m)
        self.assertEqual(len(calls), 1, "request re-armed after an unproven buy and bought again")
        self.assertEqual(len([a for a in m.alerts if "result is not confirmed" in a]), 1)
        self.assertFalse(any("no order is left at the broker" in a for a in m.alerts))

    def test_count_mismatch_is_unproven(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos, outcome=qm.AddOutcome(True, 2))   # claims 2 filled but QHM's count did not move
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        self.assertTrue(any("result is not confirmed" in a for a in m.alerts))


class RealAddChain(_Base):
    """maybe_owner_buy -> the REAL _stop_safe_add -> settle, with only broker / allocator / ledger calls mocked."""

    def _run_chain(self, pos, *, submit, get_order, cancel_ok=True, stop_ok=True, strict=None, save=None):
        m = self._mgr(pos)
        del m._stop_safe_add                         # the real stop-safe add
        m.broker = SimpleNamespace(get_account=lambda: SimpleNamespace(regt_buying_power=100000.0),
                                   get_position=lambda s: SimpleNamespace(qty=pos.qty_filled))
        m._grandfathered_notional_for_allocator = lambda: 0.0
        m._resync_from_alpaca = lambda p: None

        def _strict(p, hint=None, base=None):
            if hint is not None:
                p.qty_filled = hint
            return p.qty_filled
        m._strict_resync_qty = (lambda p, hint=None, base=None: strict(m, p, hint, base)) if strict else _strict
        if save:
            m._save_state = lambda: save(m, pos)
        m.limits, m.stops_placed = [], []

        def submit_limit(b, sym, qty, side, px, client_order_id=None):
            m.limits.append((sym, qty, side, px))
            return submit(sym, qty, side, client_order_id)
        m._dispatcher = SimpleNamespace(
            submit_limit=submit_limit,
            submit_gtc_stop=lambda b, s, q, side, px, client_order_id=None: m.stops_placed.append((q, px))
            or (SimpleNamespace(id="stop-new") if stop_ok else None))
        fake_broker = SimpleNamespace(is_market_open=lambda: True,
                                      cancel_order=lambda oid: cancel_ok or oid != "add-1",
                                      get_order=get_order, get_order_by_client_order_id=lambda coid: None)
        lease = SimpleNamespace(id="lease-1")
        fake_alloc = SimpleNamespace(live_admit=lambda *a, **kw: SimpleNamespace(approved=True, lease=lease),
                                     live_bind=lambda *a, **kw: True, live_release=lambda *a, **kw: True,
                                     live_order_id=lambda _l: "QH-NVDA-test")
        fake_guard = SimpleNamespace(tier_of_coid=lambda c: "qhm" if str(c).startswith("QH-") else None,
                                     apply_authorized_tier_fill_increase=lambda *a: True)
        clock = iter(range(100000))
        with mock.patch.dict(sys.modules, {"execution.broker": fake_broker, "execution.tier_capital_allocator": fake_alloc,
                                           "execution.ownership_guard": fake_guard}), \
                mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=lambda: float(next(clock))):
            first = self._cycle(m)
            pos.state = qm.HoldState.ACTIVE          # later recovery restores the hold
            pos.ambiguous_add_client_order_id = None
            pos.stop_order_id = pos.stop_order_id or "stop-new"
            qm._OWNER_STOP_DAY.clear()
            self._cycle(m)
        return m, first

    @staticmethod
    def _order(coid, qty, status="new", filled=0, **kw):
        return SimpleNamespace(id="add-1", client_order_id=coid, symbol="NVDA", side="buy", qty=qty,
                               filled_qty=filled, status=status, filled_avg_price=151.02,
                               filled_at=datetime(2026, 11, 13, 16, 0, tzinfo=timezone.utc), **kw)

    def _assert_held_reserved(self, m, pos):
        self.assertEqual(len(m.limits), 1, "a second buy was sent")
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        pages = [a for a in m.alerts if "result is not confirmed" in a]
        self.assertEqual(len(pages), 1)
        self.assertFalse(any("no order is left at the broker" in a for a in m.alerts))
        self.assertTrue(m.stops_placed, "the stop must be re-placed for the shares QHM can prove")
        # the add path took its ambiguous-outcome exit (_hold_ambiguous_add), which returns AddOutcome(True, 0)
        self.assertTrue(any("dip-add outcome AMBIGUOUS" in a for a in m.alerts))
        self.assertEqual(pos.qty_filled, 5)

    def test_exit_ambiguous_add_current_qty(self):
        # the submit raises and the order cannot be found by its client id -> _hold_ambiguous_add
        self._file({"NVDA": _req(2)})
        pos = _pos()

        def submit(*_a):
            raise RuntimeError("transport error after the buy may have reached Alpaca")
        m, first = self._run_chain(pos, submit=submit, get_order=lambda oid: None)
        self.assertEqual(first, [True])
        self._assert_held_reserved(m, pos)

    def test_exit_add_order_proof_invalid(self):
        # the submit reply passes the first proof, then reads back contradictory (filled > qty)
        self._file({"NVDA": _req(2)})
        pos = _pos()

        class Flip:
            def __init__(self, coid, qty):
                self.id, self.client_order_id, self.symbol, self.side = "add-1", coid, "NVDA", "buy"
                self.qty, self.status, self._reads = qty, "new", 0

            @property
            def filled_qty(self):
                self._reads += 1
                return 0 if self._reads == 1 else self.qty + 1
        m, _first = self._run_chain(pos, submit=lambda sym, qty, side, coid: Flip(coid, qty),
                                    get_order=lambda oid: None)
        self._assert_held_reserved(m, pos)

    def test_exit_add_cancel_not_terminal(self):
        # the buy never fills; its cancel is accepted but the order never reaches a terminal status
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m, _first = self._run_chain(pos, submit=lambda sym, qty, side, coid: self._order(coid, qty),
                                    get_order=lambda oid: self._order("QH-NVDA-test", 2))
        self._assert_held_reserved(m, pos)

    def test_full_fill_through_the_real_add_is_proven(self):
        self._file({"NVDA": _req(2, limit=151.05)})
        pos = _pos()
        filled = self._order("QH-NVDA-test", 2, status="filled", filled=2)
        m, first = self._run_chain(pos, submit=lambda sym, qty, side, coid: self._order(coid, qty, "filled", qty),
                                   get_order=lambda oid: filled)
        self.assertEqual(first, [True])
        self.assertEqual(m.limits, [("NVDA", 2, "buy", 151.05)])   # never priced above the owner's $151.05
        self.assertEqual((pos.qty_filled, self._reqs()), (7, {}))
        self.assertTrue(any(a.startswith("✅ NVDA") and "bought 2 of 2 @ $151.02" in a for a in m.alerts))
        self.assertIsNone(pos.last_dip_add_date)                     # the owner buy left the dip-add spacing alone


class ProvenFillStopNotProven(RealAddChain):
    """E1 / E3 (ported from the second-round proof tests): the real add chain with the stop, save or count failing."""

    def _filled_chain(self, pos, **kw):
        filled = self._order("QH-NVDA-test", 2, status="filled", filled=2)
        return self._run_chain(pos, submit=lambda sym, qty, side, coid: filled, get_order=lambda oid: filled, **kw)

    def _assert_fill_reported(self, m):
        self.assertEqual(len(m.limits), 1, "a second buy was sent")
        self.assertEqual(self._reqs(), {})                     # the proven 2 of 2 completed the request
        line = [a for a in m.alerts if "owner buy: bought 2 of 2" in a][0]
        self.assertTrue(line.startswith("🚨 ACTION"))
        self.assertIn("bought 2 of 2 @ $151.02", line)
        self.assertIn("being repaired by the bot's stop recovery", line)
        self.assertFalse(any("not confirmed" in a for a in m.alerts))

    def test_stop_re_place_failed_after_a_proven_fill(self):
        self._file({"NVDA": _req(2)})
        m, _f = self._filled_chain(_pos(), stop_ok=False)
        self._assert_fill_reported(m)

    def test_final_save_failed_after_a_proven_fill(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()

        def save(m, p):    # the add path's last save (ACTIVE, recovery cleared, 7 shares) fails once
            return not (m.limits and p.state == qm.HoldState.ACTIVE and p.ambiguous_add_client_order_id is None
                        and p.stop_recovery_requires_qty_proof is False and p.qty_filled == 7
                        and not getattr(m, "_saved_once", False) and not setattr(m, "_saved_once", True))
        m, _f = self._filled_chain(pos, save=save)
        self._assert_fill_reported(m)

    def test_share_count_unreadable_after_a_proven_fill(self):
        self._file({"NVDA": _req(2)})

        def strict(m, p, hint, base):
            if m.limits and hint == 7:
                return None
            if hint is not None:
                p.qty_filled = hint
            return p.qty_filled
        m, _f = self._filled_chain(_pos(), strict=strict)
        self._assert_fill_reported(m)

    def test_open_buy_says_it_may_still_be_open_never_that_no_order_rests(self):
        self._file({"NVDA": _req(2)})
        m, _f = self._run_chain(_pos(), submit=lambda sym, qty, side, coid: self._order(coid, qty),
                                get_order=lambda oid: self._order("QH-NVDA-test", 2), cancel_ok=False)
        self.assertEqual(len(m.limits), 1)
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        pages = [a for a in m.alerts if "may still be open at the broker" in a]
        self.assertEqual(len(pages), 1)
        self.assertFalse(any("no order is left" in a or "No order was at the broker" in a for a in m.alerts))


class Round2(_Base):
    """Second-round review items A-E."""

    # ---- A: entry halts ----------------------------------------------------------------------------------------
    def _halt_mgr(self, *, legs=None, kill=None, kill_raises=False, prev=True, row_ts="2026-11-13T07:58:00-08:00"):
        self._file({"NVDA": _req(2, seen_at="x")})
        m = self._mgr(_pos())
        del m._owner_buy_entry_halt                   # the real check
        m._owner_buy_prev_cycle_at = datetime(2026, 11, 13, 10, 55, tzinfo=qm.ET) if prev else None
        (self.tmp / "logs" / "trade_events.jsonl").unlink(missing_ok=True)
        if legs is not None:
            rows = [{"ts": "2026-11-13T07:40:00-08:00", "event": "halt_eval", "legs": []},   # an older cycle's row
                    {"ts": row_ts, "event": "halt_eval", "legs": legs},
                    {"ts": "2026-11-13T07:59:00-08:00", "event": "signal", "symbol": "AMD"}]
            (self.tmp / "logs" / "trade_events.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        def load():
            if kill_raises:
                raise ValueError("bad kill state")
            return kill or {}
        return m, load

    def _assert_waits(self, m, load, text):
        with mock.patch("execution.risk_manager._load_kill_state", side_effect=load):
            self.assertEqual(self._cycle(m), [False])
        self.assertEqual(m.adds, [])
        self.assertEqual(self._reqs()["NVDA"]["status"], "armed")
        page = [a for a in m.alerts if "is waiting" in a]
        self.assertEqual(len(page), 1)
        self.assertIn(text, page[0])
        self.assertIn("it runs when new entries are allowed again", page[0])
        self.assertEqual(self._rows()[-1]["action"], "deferred")

    def test_each_halt_leg_makes_the_buy_wait(self):
        for leg in ("keyword", "venue_closed", "spy_not_tradable", "mwcb_band", "tradable_unknown", "mwcb_unknown",
                    "session_closed"):
            with self.subTest(leg=leg):
                qm._OWNER_STOP_DAY.clear()
                m, load = self._halt_mgr(legs=[leg])
                self._assert_waits(m, load, "new entries are halted this cycle (%s)" % leg)

    def test_kill_switch_and_control_fault_make_the_buy_wait(self):
        for state, text in (({"date": "2026-11-13", "killed": True}, "daily kill switch"),
                            ({"date": "2026-11-13", "halt_entries": True, "cause": "CONTROL_FAULT"}, "CONTROL_FAULT")):
            with self.subTest(text=text):
                qm._OWNER_STOP_DAY.clear()
                m, load = self._halt_mgr(legs=[], kill=state)
                self._assert_waits(m, load, text)

    def test_unknown_halt_state_waits(self):
        cases = ((dict(legs=[], kill_raises=True), "kill-switch state is unreadable"),
                 (dict(legs=None), "could not be read"),                              # no halt_eval row at all
                 (dict(legs=[], row_ts="2026-11-13T07:50:00-08:00"), "could not be read"),   # only last cycle's
                 (dict(legs=[], prev=False), "could not be read"),                    # first cycle since a start
                 (dict(legs="x"), "unreadable"))
        for kw, text in cases:
            with self.subTest(kw=kw):
                qm._OWNER_STOP_DAY.clear()
                m, load = self._halt_mgr(**kw)
                self._assert_waits(m, load, text)

    def test_no_halt_buys(self):
        m, load = self._halt_mgr(legs=[], kill={"date": "2026-11-12", "killed": True})   # yesterday's kill
        with mock.patch("execution.risk_manager._load_kill_state", side_effect=load):
            self.assertEqual(self._cycle(m), [True])
        self.assertEqual(len(m.adds), 1)

    # ---- B: no-fill churn --------------------------------------------------------------------------------------
    def _bars(self, *rows):
        import pandas as pd
        idx = pd.DatetimeIndex([pd.Timestamp(t).tz_convert("UTC") for t, _low in rows])
        return pd.DataFrame({"low": [low for _t, low in rows]}, index=idx)

    def test_no_fill_waits_for_a_completed_bar_at_or_below_the_price(self):
        import pandas as pd
        self._file({"NVDA": _req(2, limit=151.0, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos, fill=lambda q: 0)
        t0 = pd.Timestamp(NOW)
        bars = self._bars()                            # nothing yet
        with mock.patch("data.fetcher.fetch_bars_window", side_effect=lambda *a, **kw: bars):
            for _ in range(10):
                self._cycle(m)
            self.assertEqual(len(m.adds), 1)           # 10 cycles, one attempt: no churn
            # bars after the attempt: one above the price, one at it (completed), and a forming bar -> one attempt
            bars = self._bars((t0 + pd.Timedelta(minutes=5), 151.5), (t0 + pd.Timedelta(minutes=10), 151.0),
                              (t0 + pd.Timedelta(minutes=15), 150.0))
            later = NOW + timedelta(minutes=20)
            m._now_et = lambda: later                  # the second attempt happens at 11:20
            self._cycle(m)
            self.assertEqual(len(m.adds), 2)
            self._cycle(m)                             # bars from before the 11:20 attempt no longer count
        self.assertEqual(len(m.adds), 2)
        self.assertEqual(len([r for r in self._rows() if r.get("action") == "no_fill"]), 2)   # a row per attempt
        page = [a for a in m.alerts if "bought 0 of 2" in a]
        self.assertEqual(len(page), 1)
        self.assertIn("1 attempt(s) today", page[0])
        self.assertIn("completed 5-minute bar trades at or below $151.00", page[0])
        self.assertEqual(self._reqs()["NVDA"]["attempts_today"], 2)

    def test_a_forming_bar_alone_does_not_count(self):
        import pandas as pd
        self._file({"NVDA": _req(2, seen_at="x")})      # market request: any completed bar after the attempt
        m = self._mgr(_pos(), fill=lambda q: 0)
        t0 = pd.Timestamp(NOW)
        with mock.patch("data.fetcher.fetch_bars_window",
                        side_effect=lambda *a, **kw: self._bars((t0 + pd.Timedelta(minutes=5), 1.0))):
            self._cycle(m)
            self._cycle(m)
        self.assertEqual(len(m.adds), 1)

    # ---- C: gap check ------------------------------------------------------------------------------------------
    def test_ask_at_or_below_the_stop_waits(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        m = self._mgr(_pos(stop=151.0), ask=151.0)
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual(m.adds, [])
        self.assertIn("the ask $151.00 is at or below the stop $151.00", m.alerts[-1])

    # ---- D: caps at the order's highest price ------------------------------------------------------------------
    def test_caps_are_checked_at_the_order_price(self):
        for limit, want in ((None, 151.15), (151.05, 151.05)):
            with self.subTest(limit=limit):
                self._file({"NVDA": _req(1, limit=limit, seen_at="x")})
                m = self._mgr(_pos(), ask=151.0)
                m.cap_px = []
                m._qhm_cap_room_shares = lambda s, px, eq, _m=m: _m.cap_px.append(px) or 10
                self._cycle(m)
                self.assertEqual(m.cap_px, [want])

    # ---- E2: a confirmed result survives a restart ---------------------------------------------------------------
    def test_failed_rewrite_survives_a_restart(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos, fill=lambda q: 1)
        real = m._update_owner_buy

        def flaky(sym, tok, fields, expect=None):
            if fields and fields.get("filled_qty") == 1:
                m._owner_buy_write_why = "busy"
                return False
            return real(sym, tok, fields, expect=expect)
        m._update_owner_buy = flaky
        self._cycle(m)
        self.assertEqual(pos.owner_buy_settle["fields"]["filled_qty"], 1)   # in the hold record, saved
        self.assertEqual(qm.HoldPosition.from_dict(pos.to_dict()).owner_buy_settle, pos.owner_buy_settle)
        qm._OWNER_STOP_DAY.clear()
        m2 = self._mgr(pos, fill=lambda q: 1)           # a restart: a new manager, nothing in memory
        self._cycle(m2)
        self.assertFalse(any("not confirmed" in a for a in m2.alerts))
        self.assertIsNone(pos.owner_buy_settle)
        self.assertEqual(self._reqs(), {})             # the remembered 1 + this cycle's 1 = 2 of 2
        self.assertIn("bought 2 of 2", m2.alerts[-1])

    # ---- E4: a malformed reserved request ----------------------------------------------------------------------
    def test_malformed_reserved_request_is_removed_with_result_unknown(self):
        self._file({"NVDA": {"status": "reserved", "qty": "x", "created_at": CREATED, "until": "2026-11-20"}})
        m = self._mgr(_pos())
        self._cycle(m)
        self.assertEqual(self._reqs(), {})
        line = m.alerts[-1]
        self.assertTrue(line.startswith("🚨 ACTION"))
        self.assertIn("result of its buy is unknown", line)
        self.assertNotIn("No order was at the broker", line)

    # ---- E5: the price covers every fill counted -------------------------------------------------------------------
    def test_price_is_the_average_of_every_fill_counted(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos, fill=lambda q: 1)
        self._cycle(m)
        self.fill_price = 150.0
        m2 = self._mgr(pos)
        self._cycle(m2)
        self.assertIn("bought 2 of 2 @ $150.51", m2.alerts[-1])

    # ---- E6: no "✅ stop -> " when the recalculation did not run ----------------------------------------------------
    def test_skipped_recalculation_is_not_reported_as_done(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos)
        del m._maybe_restop_after_add                 # the real pass: a QH limit sell in the book makes it stand down
        m._cover_inputs = lambda p: ((p.qty_filled, 151.0), "")
        trim = SimpleNamespace(id="trim-1", symbol="NVDA", client_order_id="QH-NVDA-s-9-t", side="sell", type="limit",
                               status="new", time_in_force="day", qty=1, filled_qty=0, limit_price=170.0)
        m._get_open_orders = lambda: [_stop(pos.stop_price, pos.qty_filled), trim]
        with mock.patch("execution.broker.replace_stop_order") as rep:
            self._cycle(m)
        rep.assert_not_called()
        line = m.alerts[-1]
        self.assertFalse(line.startswith("✅"))
        self.assertIn("its price was not recalculated this cycle", line)
        self.assertNotIn("->", line)

    # ---- E7: owner cancel before the buy -------------------------------------------------------------------------
    def test_owner_cancel_before_the_reservation_says_so(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        m = self._mgr(_pos())

        def ask(sym):
            self._file({})                            # the owner ran `cancel NVDA buy` meanwhile
            return 151.0, "fresh"
        m._reentry_ask = ask
        self._cycle(m)
        self.assertEqual(m.adds, [])
        self.assertIn("you cancelled the owner buy request before it ran; nothing was bought", m.alerts[-1])
        self.assertFalse(any("retried next cycle" in a for a in m.alerts))

    # ---- E8: an exception in the add path --------------------------------------------------------------------------
    def test_exception_before_submit_says_it_did_not_reach_the_broker(self):
        for submitted, text in ((False, "did not reach the broker"), (True, "it was sent but its result is not "
                                                                            "confirmed")):
            with self.subTest(submitted=submitted):
                qm._OWNER_STOP_DAY.clear()
                self._file({"NVDA": _req(2, seen_at="x")})
                pos = _pos()
                m = self._mgr(pos)

                def add(p, *a, _s=submitted, **kw):
                    p.stop_recovery_add_submitted = _s
                    raise RuntimeError("boom")
                m._stop_safe_add = add
                self.assertEqual(self._cycle(m), [True])
                self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
                self.assertIn(text, m.alerts[-1])
                self.assertEqual(self._reqs()["NVDA"]["attempt_result"], "unknown" if submitted else "not_sent")

    # ---- E9: allocator refusal reason ------------------------------------------------------------------------------
    def test_allocator_refusal_names_its_reason(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        m = self._mgr(_pos())

        def add(p, *a, **kw):
            m._last_add_refusal = "the capital allocator refused it (qhm budget used)"
            return qm.AddOutcome(False, 0)
        m._stop_safe_add = add
        self._cycle(m)
        self.assertIn("placed no order this cycle (the capital allocator refused it (qhm budget used))", m.alerts[-1])

    # ---- E10: a hold that cannot run this cycle -------------------------------------------------------------------
    def test_hold_not_active_or_blocked_waits_with_a_row(self):
        for state, blocked, text in ((qm.HoldState.PENDING_STOP_REPLACE, set(), "PENDING_STOP_REPLACE"),
                                     (qm.HoldState.ACTIVE, {"NVDA"}, "blocked from stop changes")):
            with self.subTest(text=text):
                qm._OWNER_STOP_DAY.clear()
                self._file({"NVDA": _req(2, seen_at="x")})
                pos = _pos()
                pos.state = state
                m = self._mgr(pos)
                m._owner_buy_cycle = m._load_owner_buys()
                m._owner_buy_sweep(blocked)
                self.assertIn(text, m.alerts[-1])
                self.assertEqual(self._rows()[-1]["action"], "deferred")


class Messages(_Base):
    def test_refusal_after_the_stop_was_cancelled_says_the_stop_is_being_repaired(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)

        def add(p, *a, **kw):
            p.state = qm.HoldState.PENDING_STOP_REPLACE
            p.stop_order_id = None
            return qm.AddOutcome(False, 0)
        m._stop_safe_add = add
        self._cycle(m)
        line = [a for a in m.alerts if "placed no order this cycle" in a][0]
        self.assertIn("being repaired", line)
        self.assertNotIn("unchanged", line)

    def test_owner_cancel_during_the_buy_says_so_and_states_the_fill(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)
        real_add = m._stop_safe_add

        def add(p, *a, **kw):
            self._file({})                            # the owner ran `cancel NVDA buy` meanwhile
            return real_add(p, *a, **kw)
        m._stop_safe_add = add
        self._cycle(m)
        line = m.alerts[-1]
        self.assertIn("bought 2 of 2 @ $151.02", line)
        self.assertIn("You cancelled this request while the buy ran", line)
        self.assertFalse(line.startswith("🚨"))
        self.assertNotIn("cancel NVDA buy", line)

    def test_failed_rewrite_after_a_fill_is_retried_and_never_paged_unconfirmed(self):
        self._file({"NVDA": _req(3)})
        pos = _pos()
        m = self._mgr(pos, fill=lambda q: 1)
        real = m._update_owner_buy
        fails = []

        def update(sym, tok, fields, expect=None):
            if fields is not None and "filled_qty" in fields and not fails:
                fails.append(1)
                m._owner_buy_write_why = "busy"
                return False
            return real(sym, tok, fields, expect=expect)
        m._update_owner_buy = update
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        self.assertIn("the bot retries it", m.alerts[-1])
        qm._OWNER_STOP_DAY.clear()
        self._cycle(m)                                # the sweep applies the retried rewrite first
        rec = self._reqs()["NVDA"]
        self.assertEqual((rec["status"], rec["qty"], rec["filled_qty"]), ("armed", 1, 2))
        self.assertFalse(any("not confirmed" in a for a in m.alerts))

    def test_a_qh_limit_sell_is_not_counted_as_stop_coverage(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)
        trim = SimpleNamespace(id="t", symbol="NVDA", client_order_id="QH-NVDA-s-9-b", side="sell", type="limit",
                               status="new", qty=1, filled_qty=0, limit_price=200.0)
        m._get_open_orders = lambda: [_stop(pos.stop_price, pos.qty_filled), trim]
        self._cycle(m)
        self.assertFalse(any("covers" in a for a in m.alerts))
        self.assertTrue(m.alerts[-1].startswith("✅"))

    def test_a_just_placed_stop_missing_from_the_first_listing_is_read_again(self):
        self._file({"NVDA": _req(2)})
        pos = _pos()
        m = self._mgr(pos)
        books = iter([[], [_stop(pos.stop_price, 7)]])
        m._get_open_orders = lambda: next(books)
        self._cycle(m)
        self.assertFalse(any("covers" in a for a in m.alerts))
        self.assertTrue(m.alerts[-1].startswith("✅"))


class Malformed(_Base):
    def test_huge_or_infinite_quantities_are_refused_not_crashing_the_sweep(self):
        self.req.write_text('{"NVDA": {"qty": 1e999, "status": "armed", "created_at": "%s", "until": "2026-11-20"},'
                            ' "GOOGL": {"qty": %d, "status": "armed", "created_at": "%s", "until": "2026-11-20"},'
                            ' "LLY": %s}' % (CREATED, 10 ** 400, CREATED, json.dumps(_req(1, seen_at="x"))))
        m = self._mgr(_pos(), _pos("GOOGL"), _pos("LLY"))
        self._cycle(m)
        self.assertEqual(list(self._reqs()), [])     # both bad ones refused and removed; LLY still bought
        self.assertEqual(len([a for a in m.alerts if "refused: the request is malformed" in a]), 2)
        self.assertEqual([a[0] for a in m.adds], ["LLY"])


class Budget(_Base):
    def test_owner_buys_share_the_tranche_add_budget(self):
        syms = ("NVDA", "GOOGL", "LLY")
        self._file({s: _req(1) for s in syms})
        positions = [_pos(s) for s in syms]
        m = self._mgr(*positions)
        self._cycle(m)
        self.assertEqual(len(m.adds), qm._TRANCHE_MAX_ADDS_PER_CYCLE)
        # the tranche entry pass later in the same cycle starts from the owner buys already made
        m._positions = {}
        m.maybe_enter_positions()
        self.assertEqual(m._tranche_cycle_attempts, qm._TRANCHE_MAX_ADDS_PER_CYCLE)
        m.maybe_enter_positions()                     # a cycle with no owner buy starts at 0, as before
        self.assertEqual(m._tranche_cycle_attempts, 0)
        self.assertIn("add budget used this cycle", [r.get("reason") for r in self._rows()])

    def test_one_owner_buy_leaves_one_tranche_add(self):
        self._file({"NVDA": _req(1)})
        m = self._mgr(_pos())
        self._cycle(m)
        m._positions = {}
        m.maybe_enter_positions()
        self.assertEqual(m._tranche_cycle_attempts, 1)


class Rows(_Base):
    def test_ask_above_price_and_reservation_failure_write_rows(self):
        self._file({"NVDA": _req(2, limit=150.0, seen_at="x")})
        m = self._mgr(_pos(), ask=151.0)
        self._cycle(m)
        self._cycle(m)
        self.assertEqual([r["reason"] for r in self._rows()], ["ask above the owner's price"])
        m.quote = (149.0, "fresh")
        m._update_owner_buy = lambda *a: False
        self._cycle(m)
        self.assertTrue(any("could not be updated" in str(r.get("reason")) for r in self._rows()))
        self.assertEqual(m.adds, [])


class Round3(_Base):
    """Third-round review items 1-5."""

    # ---- 1: account-read failure still waits (via the reads the buy itself needs) -----------------------------------
    def test_account_read_failure_waits(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        m = self._mgr(_pos(), equity=0.0)               # _get_account_equity returns 0 when the account read fails
        del m._qhm_cap_room_shares
        self.assertEqual(self._cycle(m), [False])
        self.assertIn("the QHM limits could not be checked", m.alerts[-1])
        qm._OWNER_STOP_DAY.clear()
        m2 = self._mgr(_pos())

        def boom():
            raise RuntimeError("account read failed")
        m2.broker = SimpleNamespace(get_account=boom)
        self.assertEqual(self._cycle(m2), [False])
        self.assertIn("buying power does not cover one share", m2.alerts[-1])
        self.assertEqual((m.adds, m2.adds), ([], []))

    def test_halt_scope_is_stated_in_the_docstring(self):
        doc = qm.QuarterlyHoldManager._owner_buy_entry_halt.__doc__ or ""
        self.assertIn("does NOT wait on a per-cycle kill-measure fault", doc)
        self.assertIn("exact tier attribution is unresolved", doc)

    # ---- 2: a remembered result is compare-and-set ------------------------------------------------------------------
    def test_c5_stale_settle_is_discarded_never_reapplied_over_progress(self):
        """Ported from adv3 test_c5: the request moved on (8 of 10 bought, 2 left) after the remembered result."""
        self._file({"NVDA": dict(_req(10, seen_at="x"), qty=2, filled_qty=8, requested_qty=10)})
        pos = _pos()
        pos.owner_buy_settle = {"token": CREATED, "base_filled": 0,
                                "fields": {"status": "armed", "qty": 5, "filled_qty": 5}}
        m = self._mgr(pos, room=100)
        self._cycle(m)
        self.assertIsNone(pos.owner_buy_settle)
        self.assertEqual(m.adds[0][1], 2)               # the live request's 2, never the stale 5
        self.assertIn("settle_discarded", [r["action"] for r in self._rows()])

    def test_settle_without_its_base_is_discarded(self):
        self._file({"NVDA": _req(2, seen_at="x", status="reserved")})
        pos = _pos()
        pos.owner_buy_settle = {"token": CREATED, "fields": None}   # an older record without base_filled
        m = self._mgr(pos)
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["status"], "reserved")
        self.assertIsNone(pos.owner_buy_settle)

    def test_settle_applies_once_even_if_clearing_it_is_not_saved(self):
        self._file({"NVDA": _req(2, seen_at="x", status="reserved")})
        pos = _pos()
        pos.owner_buy_settle = {"token": CREATED, "base_filled": 0,
                                "fields": {"status": "armed", "qty": 1, "filled_qty": 1}}
        m = self._mgr(pos)
        m._save_state = lambda: False                   # the clear is never saved
        m._owner_buy_cycle = m._load_owner_buys()
        m._owner_buy_apply_settles()
        self.assertEqual((self._reqs()["NVDA"]["qty"], self._reqs()["NVDA"]["filled_qty"]), (1, 1))
        # a restart reloads the old record (clear not saved): the request no longer matches -> not applied again
        pos.owner_buy_settle = {"token": CREATED, "base_filled": 0,
                                "fields": {"status": "armed", "qty": 1, "filled_qty": 1}}
        rec = dict(self._reqs()["NVDA"], qty=9)         # newer progress after the first application
        self._file({"NVDA": rec})
        m._owner_buy_apply_settles()
        self.assertEqual(self._reqs()["NVDA"]["qty"], 9)

    # ---- 3: tombstone of a cancelled unresolved buy -----------------------------------------------------------------
    def _tomb(self, **kw):
        return dict(_req(2, seen_at="x", status="cancelled_unresolved", attempt_result="unknown", qty_before=5), **kw)

    def test_the_bot_never_buys_for_a_tombstone_and_waits_until_resolved(self):
        self._file({"NVDA": self._tomb()})
        pos = _pos()
        pos.state = qm.HoldState.PENDING_STOP_REPLACE
        pos.ambiguous_add_client_order_id = "QH-NVDA-b-1-x"
        m = self._mgr(pos)
        self._cycle(m)
        self.assertEqual(m.adds, [])
        self.assertEqual(self._reqs()["NVDA"]["status"], "cancelled_unresolved")
        self.assertIn("cancelled request waiting for its earlier buy to resolve",
                      [r.get("reason") for r in self._rows()])

    def test_resolution_clears_the_tombstone_and_reports_the_fill(self):
        self._file({"NVDA": self._tomb()})
        pos = _pos(qty=7)                               # the recovery proved the earlier buy filled 2
        m = self._mgr(pos)
        self.assertEqual(self._cycle(m), [False])
        self.assertEqual((self._reqs(), m.adds), ({}, []))
        line = m.alerts[-1]
        self.assertIn("is now resolved: QHM holds 7 sh now, 5 when that buy was sent (+2", line)
        self.assertIn("A new `buy NVDA` is accepted again", line)
        self.assertEqual(self._rows()[-1]["action"], "tombstone_resolved")

    # ---- 4: messages --------------------------------------------------------------------------------------------------
    def test_c6_no_fill_while_the_owner_cancelled_is_one_true_line(self):
        """Ported from adv3 test_c6: the owner cancels while an unfilled buy runs."""
        self._file({"NVDA": _req(2, seen_at="x")})

        def fill(q):
            self.req.write_text("{}")
            return 0
        m = self._mgr(_pos(), fill=fill)
        self._cycle(m)
        line = m.alerts[-1]
        self.assertIn("you cancelled the owner buy request while its buy ran", line)
        self.assertIn("nothing more is bought for this request", line)
        self.assertNotIn("tries again", line)
        self.assertFalse(line.startswith("✅"))
        self.assertEqual(len([a for a in m.alerts if "owner buy" in a and "NVDA" in a and "accepted" not in a]), 1)

    def _cancel_to_tombstone_during_add(self):
        """What scripts/qhm_owner.py `cancel NVDA buy` writes while the bot's buy is in flight: the reserved request
        becomes a cancelled_unresolved tombstone under the same token."""
        cur = json.loads(self.req.read_text())["NVDA"]
        cur.update(status="cancelled_unresolved", attempt_result="unknown")
        self.req.write_text(json.dumps({"NVDA": cur}))

    def test_cancel_during_an_unfilled_add_is_never_re_armed(self):
        """Fourth-round review: the tombstone kept the token, so the settle write used to re-arm the request."""
        self._file({"NVDA": _req(2, seen_at="x")})

        def fill(q):
            self._cancel_to_tombstone_during_add()
            return 0
        m = self._mgr(_pos(), fill=fill)
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["status"], "cancelled_unresolved")
        self.assertIn("you cancelled the owner buy request while its buy ran", m.alerts[-1])
        self._cycle(m)                                  # later cycles never buy for the tombstone
        self.assertEqual(len(m.adds), 1)

    def test_cancel_during_a_partial_fill_buys_nothing_more(self):
        self._file({"NVDA": _req(2, seen_at="x")})

        def fill(q):
            self._cancel_to_tombstone_during_add()
            return 1
        m = self._mgr(_pos(), fill=fill)
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["status"], "cancelled_unresolved")
        self.assertIn("You cancelled this request while the buy ran; nothing more is bought for it.",
                      " ".join(m.alerts))
        self._cycle(m)
        self.assertEqual(len(m.adds), 1)

    def test_cancel_during_an_unsubmitted_add_is_never_re_armed(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos)

        def add(p, qty, *a, **kw):
            self._cancel_to_tombstone_during_add()
            return qm.AddOutcome(False, 0)
        m._stop_safe_add = add
        self._cycle(m)
        self.assertEqual(self._reqs()["NVDA"]["status"], "cancelled_unresolved")
        self.assertIn("you cancelled the owner buy request", m.alerts[-1])

    def test_malformed_filled_qty_refuses_a_compare_and_set_write(self):
        self._file({"NVDA": _req(2, seen_at="x", status="reserved", filled_qty="two")})
        m = self._mgr(_pos())
        self.assertFalse(m._update_owner_buy("NVDA", self._reqs()["NVDA"]["created_at"], {"status": "armed"},
                                             expect={"filled_qty": 0}))
        self.assertEqual(m._owner_buy_write_why, "stale")

    def test_proven_fill_with_a_confirmed_stop_states_the_stop(self):
        self._file({"NVDA": _req(2, seen_at="x")})
        pos = _pos()
        m = self._mgr(pos)

        def add(p, qty, *a, **kw):                     # the order's fill is proven; QHM's count did not move
            m._last_add_proven_fill = qty
            m._last_add_order_id = "add-1"
            return qm.AddOutcome(True, qty)
        m._stop_safe_add = add
        self._cycle(m)
        line = m.alerts[-1]
        self.assertIn("bought 2 of 2 @ $151.02", line)
        self.assertIn("stop $140.00 for 5 sh (checked at Alpaca)", line)
        self.assertNotIn("being repaired", line)
        self.assertTrue(line.startswith("HEADS-UP"))

    # ---- 5: the bar check uses the IEX feed; an empty frame waits honestly ------------------------------------------
    def test_bar_check_uses_iex_and_an_empty_window_waits_without_a_fault_page(self):
        """Fourth-round claims review: no bar yet since the attempt is normal, not a read failure."""
        import pandas as pd
        self._file({"NVDA": _req(2, seen_at="x", nofill_at=NOW.astimezone(timezone.utc).isoformat())})
        m = self._mgr(_pos())
        with mock.patch("data.fetcher.fetch_bars_window", return_value=pd.DataFrame()) as fetch:
            self._cycle(m)
            self._cycle(m)
        self.assertEqual(m.adds, [])
        self.assertEqual(fetch.call_args.kwargs.get("feed"), "iex")
        self.assertEqual([a for a in m.alerts if "could not be read" in a], [])
        self.assertIn("waiting for trading at or below the price after an unfilled attempt",
                      [r.get("reason") for r in self._rows()])

    def test_bar_read_failure_waits_with_one_true_page(self):
        self._file({"NVDA": _req(2, seen_at="x", nofill_at=NOW.astimezone(timezone.utc).isoformat())})
        m = self._mgr(_pos())
        with mock.patch("data.fetcher.fetch_bars_window", side_effect=RuntimeError("HTTP 500")):
            self._cycle(m)
            self._cycle(m)
        self.assertEqual(m.adds, [])
        pages = [a for a in m.alerts if "bar check could not be read" in a]
        self.assertEqual(len(pages), 1)
        self.assertIn("the bar request failed", pages[0])
        self.assertNotIn("tries again after", pages[0])


class StopSafeAddKeywords(unittest.TestCase):
    """Rulings 2 and 3: the two new _stop_safe_add keywords, and their defaults (every earlier caller unchanged)."""

    def _add(self, **kw):
        pos, d = ssa._pos(qty=2), ssa.FakeDispatcher()
        m = ssa._mgr(pos, ssa.FakeBroker(3), d, resync_qty=3)
        if kw:
            real = m._stop_safe_add
            m._stop_safe_add = lambda p, q, px, label: real(p, q, px, label, **kw)
        got, _cancels = ssa._run(m, pos, add_qty=1)          # live price 230.00
        return pos, d, got

    def test_default_keeps_the_marketable_limit_and_stamps_the_add_date(self):
        pos, d, got = self._add()
        self.assertEqual((got, d.adds), (1, [("NVDA", 1, "buy", 230.23)]))
        self.assertIsNotNone(pos.last_dip_add_date)

    def test_owner_price_caps_the_limit_so_a_fill_above_it_cannot_be_requested(self):
        pos, d, got = self._add(max_limit_price=230.1)
        self.assertEqual((got, d.adds), (1, [("NVDA", 1, "buy", 230.1)]))
        pos, d, got = self._add(max_limit_price=250.0)       # a higher owner price never lifts the bot's own limit
        self.assertEqual(d.adds, [("NVDA", 1, "buy", 230.23)])

    def test_owner_buy_does_not_stamp_the_dip_add_date(self):
        pos, d, got = self._add(stamp_add_date=False)
        self.assertEqual(got, 1)
        self.assertIsNone(pos.last_dip_add_date)

    def test_the_add_records_its_order_id_and_add_outcome_is_unchanged(self):
        pos, d = ssa._pos(qty=2), ssa.FakeDispatcher()
        m = ssa._mgr(pos, ssa.FakeBroker(3), d, resync_qty=3)
        seen = []
        real = m._stop_safe_add
        m._stop_safe_add = lambda *a: seen.append(real(*a)) or seen[-1]
        ssa._run(m, pos, add_qty=1)
        self.assertEqual(seen, [qm.AddOutcome(True, 1)])
        self.assertEqual(m._last_add_order_id, "add-1")
        self.assertEqual(qm.AddOutcome._fields, ("submitted", "filled_qty", "open_buy"))


class Script(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()
        self.holds = self.tmp / "quarterly_holds.json"
        self.holds.write_text(json.dumps({"NVDA": {"state": "ACTIVE", "qty_filled": 5, "avg_entry_price": 173.51,
                                                   "entry_day": "2026-09-29", "stop_price": 163.07,
                                                   "stop_order_id": "stop-1"}}))
        self.req = self.tmp / "qhm_owner_buys.json"
        self.trims = self.tmp / "qhm_owner_trims.json"
        self.stops = self.tmp / "qhm_owner_stops.json"
        self.events = self.tmp / "logs" / "trade_events.jsonl"
        clock = SimpleNamespace(now=lambda tz=None: NOW.astimezone(tz))
        for p in (mock.patch.object(cli, "REQ", self.req), mock.patch.object(cli, "HOLDS", self.holds),
                  mock.patch.object(cli, "EVENTS", self.events), mock.patch.object(cli, "datetime", clock),
                  mock.patch.object(cli.stop_cli, "REQ", self.stops), mock.patch.object(cli.stop_cli, "HOLDS", self.holds),
                  mock.patch.object(cli.stop_cli, "EVENTS", self.events), mock.patch.object(cli.stop_cli, "datetime", clock),
                  mock.patch.object(cli.trim_cli, "REQ", self.trims), mock.patch.object(cli.trim_cli, "HOLDS", self.holds),
                  mock.patch.object(cli.trim_cli, "datetime", clock)):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, *argv, market_open=True, orders=None):
        out = io.StringIO()
        with mock.patch("execution.broker.is_market_open", return_value=market_open), \
                mock.patch("execution.broker.get_account", return_value=SimpleNamespace(equity=2500.0)), \
                mock.patch("execution.broker.get_open_orders", return_value=orders), \
                mock.patch("execution.broker.submit_limit_order") as buy, \
                mock.patch("execution.broker.cancel_order") as cancel, redirect_stdout(out):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
                out.write(str(e.code))
            buy.assert_not_called()                 # the script never places or cancels an order
            cancel.assert_not_called()
        return rc, out.getvalue()

    def _rows(self):
        return [json.loads(x) for x in self.events.read_text().splitlines()] if self.events.exists() else []

    def test_market_buy_writes_the_request_and_a_row(self):
        rc, out = self._run("buy", "nvda", "2")
        self.assertEqual(rc, 0, out)
        rec = json.loads(self.req.read_text())["NVDA"]
        self.assertEqual((rec["qty"], rec["requested_qty"], rec["limit"], rec["status"], rec["entry_day"]),
                         (2, 2, None, "armed", "2026-09-29"))
        self.assertEqual(rec["until"], "2026-12-29")           # default: the hold's max-hold exit date
        self.assertIn("2 at market", out)
        self.assertIn("on the bot's next cycle", out)
        self.assertEqual((self._rows()[-1]["event"], self._rows()[-1]["action"]), ("qhm_owner_buy", "requested"))
        self.assertIsNotNone(qm.QuarterlyHoldManager._owner_buy_fields(rec))   # the bot can read what the script wrote

    def test_limit_buy_with_until_and_closed_market_says_it_waits_for_the_open(self):
        rc, out = self._run("buy", "NVDA", "2", "@160", "--until", "2026-11-20", market_open=False)
        self.assertEqual(rc, 0, out)
        rec = json.loads(self.req.read_text())["NVDA"]
        self.assertEqual((rec["limit"], rec["until"]), (160.0, "2026-11-20"))
        self.assertIn("when the ask is $160.00 or lower", out)
        self.assertIn("waits for the open", out)

    def test_unknown_symbol_is_refused(self):
        rc, out = self._run("buy", "AAPL", "2")
        self.assertNotEqual(rc, 0)
        self.assertIn("QHM pick process", out)
        self.assertFalse(self.req.exists())
        self.assertEqual(self._rows()[-1]["action"], "refused")

    def test_other_refusals(self):
        for argv in (("buy", "NVDA", "0"), ("buy", "NVDA", "1.5"), ("buy", "NVDA", "2", "160"),
                     ("buy", "NVDA", "2", "@-5"), ("buy", "NVDA", "2", "--until", "2026-11-12"),
                     ("buy", "NVDA", "2", "--until", "11/20/2026"), ("buy", "NVDA", "2", "--until", "2026-12-30")):
            rc, out = self._run(*argv)
            self.assertNotEqual(rc, 0, argv)
        self.assertFalse(self.req.exists())

    def test_new_request_replaces_an_armed_one_but_not_a_reserved_one(self):
        self._run("buy", "NVDA", "2")
        rc, out = self._run("buy", "NVDA", "3", "@150")
        self.assertEqual(rc, 0, out)
        self.assertIn("replaces your earlier request", out)
        rec = json.loads(self.req.read_text())
        self.assertEqual(rec["NVDA"]["qty"], 3)
        rec["NVDA"]["status"] = "reserved"
        self.req.write_text(json.dumps(rec))
        rc, out = self._run("buy", "NVDA", "1")
        self.assertNotEqual(rc, 0)
        self.assertEqual(json.loads(self.req.read_text())["NVDA"]["status"], "reserved")

    def test_cancel_of_an_armed_request_removes_it(self):
        self._run("buy", "NVDA", "2", "@160")
        rc, out = self._run("cancel", "NVDA", "buy")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.req.read_text()), {})
        self.assertIn("no order was at the broker", out)
        self.assertEqual(self._rows()[-1]["action"], "cancelled")
        rc, out = self._run("cancel", "NVDA", "buy")
        self.assertIn("no owner buy request to cancel", out)

    def test_cancel_of_an_unresolved_buy_keeps_a_tombstone_and_refuses_a_new_buy(self):
        self._run("buy", "NVDA", "2", "@160")
        rec = json.loads(self.req.read_text())
        rec["NVDA"].update(status="reserved", attempt_result="unknown", qty_before=5)
        self.req.write_text(json.dumps(rec))
        token = rec["NVDA"]["created_at"]
        rc, out = self._run("cancel", "NVDA", "buy")
        self.assertEqual(rc, 0, out)
        self.assertIn("may still fill at the broker", out)
        self.assertNotIn("no order was at the broker", out)
        tomb = json.loads(self.req.read_text())["NVDA"]
        self.assertEqual((tomb["status"], tomb["created_at"]), ("cancelled_unresolved", token))
        rc, out = self._run("buy", "NVDA", "1")                    # an immediate re-buy is refused
        self.assertNotEqual(rc, 0)
        self.assertIn("may still fill at the broker", out)
        self.assertEqual(json.loads(self.req.read_text())["NVDA"]["status"], "cancelled_unresolved")

    def test_cancel_of_a_provably_unsent_buy_removes_it(self):
        self._run("buy", "NVDA", "2")
        rec = json.loads(self.req.read_text())
        rec["NVDA"].update(status="reserved", attempt_result="not_sent")
        self.req.write_text(json.dumps(rec))
        rc, out = self._run("cancel", "NVDA", "buy")
        self.assertEqual(json.loads(self.req.read_text()), {})
        self.assertIn("never reached the broker", out)

    def test_unreadable_file_is_never_reset_by_buy_or_cancel(self):
        self.req.write_text("{not json")
        rc, out = self._run("show")
        self.assertNotEqual(rc, 0)
        self.assertIn("unreadable", out)
        self.assertIn("refuse and change nothing. Inspect it by hand", out)
        self.assertNotIn(".corrupt copy", out)
        for argv in (("buy", "NVDA", "1"), ("cancel", "NVDA", "buy")):
            rc, out = self._run(*argv)
            self.assertNotEqual(rc, 0, argv)
            self.assertIn("Inspect it by hand", out)
        self.assertEqual(self.req.read_text(), "{not json")     # a reserved buy in it is never discarded
        self.assertEqual([p for p in self.tmp.iterdir() if ".corrupt." in p.name], [])

    def test_stop_alias_passes_every_argument_through_unchanged(self):
        with mock.patch.object(cli.stop_cli, "main", return_value=0) as main:
            self._run("stop", "NVDA", "150", "--until", "2026-11-20", "--profile", "paper", "--confirm-risk")
        self.assertEqual(main.call_args.args[0],
                         ["set", "NVDA", "150", "--until", "2026-11-20", "--profile", "paper", "--confirm-risk"])

    def test_trim_without_deadline_is_refused_exactly_as_the_trim_script_refuses_it(self):
        def run(fn, argv):
            err = io.StringIO()
            with mock.patch.object(cli.sys, "argv", argv), redirect_stdout(io.StringIO()), \
                    mock.patch("sys.stderr", err):
                try:
                    rc = fn()
                except SystemExit as e:
                    rc = e.code
            return rc, err.getvalue()
        alias = run(lambda: cli.main(["trim", "NVDA", "1"]), ["qhm_owner.py"])
        direct = run(cli.trim_cli.main, ["qhm_owner_trim.py", "arm", "NVDA", "--qty", "1"])
        self.assertEqual(alias, direct)
        self.assertNotEqual(alias[0], 0)
        self.assertIn("arm needs --qty and --deadline", alias[1])
        self.assertFalse(self.trims.exists())

    def test_trim_alias_routes_to_the_existing_trim_script(self):
        rc, out = self._run("trim", "NVDA", "1", "--deadline", "2026-11-13")
        self.assertEqual(rc, 0, out)
        rec = json.loads(self.trims.read_text())["NVDA"]
        self.assertEqual((rec["qty"], rec["trigger_price"], rec["deadline"], rec["status"]),
                         (1, None, "2026-11-13", "armed"))
        rc, out = self._run("trim", "NVDA", "1", "--trigger", "170", "--deadline", "2026-11-20")
        rec = json.loads(self.trims.read_text())["NVDA"]
        self.assertEqual((rec["trigger_price"], rec["deadline"]), (170.0, "2026-11-20"))
        rc, out = self._run("trim", "NVDA", "1", "--trigger", "170")       # a trigger without a deadline: the trim
        self.assertNotEqual(rc, 0)                                          # script's own rule refuses it
        rc, out = self._run("trim", "NVDA", "5")                            # must leave one share: its own rule
        self.assertNotEqual(rc, 0)
        rc, out = self._run("cancel", "NVDA", "trim")
        self.assertEqual(json.loads(self.trims.read_text())["NVDA"]["status"], "cancelled")

    def test_trim_alias_passes_the_same_arguments_the_trim_script_takes(self):
        seen = []
        with mock.patch.object(cli.trim_cli, "main", side_effect=lambda: seen.append(list(cli.sys.argv)) or 0):
            self._run("trim", "NVDA", "1", "--trigger", "170", "--deadline", "2026-11-20")
        self.assertEqual(seen[0][1:], ["arm", "NVDA", "--qty", "1", "--trigger", "170", "--deadline", "2026-11-20"])

    def test_stop_alias_routes_to_the_existing_stop_script(self):
        rc, out = self._run("stop", "NVDA", "160", "--until", "2026-11-20")
        self.assertEqual(rc, 0, out)
        rec = json.loads(self.stops.read_text())["NVDA"]
        self.assertEqual((rec["price"], rec["until"]), (160.0, "2026-11-20"))
        with mock.patch.object(cli.stop_cli, "main", return_value=0) as main:
            self._run("stop", "NVDA", "100", "--until", "2026-11-20", "--confirm-risk")
            self._run("cancel", "NVDA", "stop")
        self.assertEqual([c.args[0] for c in main.call_args_list],
                         [["set", "NVDA", "100", "--until", "2026-11-20", "--confirm-risk"], ["clear", "NVDA"]])

    def test_show_lists_requests_orders_and_the_stop_per_name(self):
        self._run("buy", "NVDA", "2", "@160", "--until", "2026-11-20")
        self._run("stop", "NVDA", "150", "--until", "2026-11-20")
        rc, out = self._run("show", orders=[_stop(163.07, 5, oid="stop-1"),
                                            SimpleNamespace(id="x", symbol="AMD", client_order_id="DT-AMD-s-1",
                                                            side="sell", type="stop", qty=1, stop_price=90.0)])
        self.assertEqual(rc, 0, out)
        self.assertIn("NVDA: buy 2 when the ask is $160.0 or lower, until 2026-11-20 (armed, active)", out)
        self.assertIn("OWNER TRIMS\n  none", out)
        self.assertIn('"price": 150.0', out.split("OWNER STOPS")[1])
        self.assertIn("NVDA: sell stop 5 @ 163.07 (order stop-1)", out)
        self.assertNotIn("AMD", out)
        self.assertIn("NVDA: 5 sh, average $173.51; QHM recorded stop $163.07; broker QH stop ['5 @ 163.07']", out)

    def test_show_with_nothing_and_broker_unreadable(self):
        rc, out = self._run("show", orders=None)
        self.assertEqual(rc, 0, out)
        self.assertIn("OWNER BUY REQUESTS\n  none", out)
        self.assertIn("OPEN QH ORDERS AT ALPACA\n  unreadable", out)
        self.assertIn("broker QH stop unreadable", out)


if __name__ == "__main__":
    unittest.main()
