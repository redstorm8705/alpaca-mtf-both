# ruff: noqa: E501
"""QHM-OWNERSTOP-1113 — the owner sets a QHM stop price that the bot keeps until a date (design record
logs/design_records/qhm_owner_stop_2026-11-13.md, rev 3). Every broker, bar and alert call is mocked; the request file,
the state and trade_events rows go to a temp directory. Nothing reaches Alpaca or Slack."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import tests  # noqa: F401  — test guard (MTF_TEST_MODE)
from execution import quarterly_hold_manager as qm
from scripts import qhm_owner_stop as cli

NOW = datetime(2026, 11, 13, 11, 0, tzinfo=qm.ET)


def _pos(stop=163.07, until=None, qty=5, avg=173.51, restop_qty=5, **kw):
    return qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                           qty_filled=qty, qty_total=qty, entry_day="2026-09-29", stop_price=stop,
                           stop_order_id="stop-1", avg_entry_price=avg, restop_qty=restop_qty,
                           owner_stop_until=until, **kw)


def _stop(price, qty=5, oid="stop-1"):
    return SimpleNamespace(id=oid, symbol="NVDA", client_order_id="QH-NVDA-s-1-a", side="sell", type="stop",
                           status="new", time_in_force="gtc", qty=qty, filled_qty=0, stop_price=price)


class _Base(unittest.TestCase):
    def setUp(self):
        qm._OWNER_STOP_DAY.clear()
        qm._OWNER_STOP_GAP_DAY.clear()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()
        self.req = self.tmp / "qhm_owner_stops.json"
        for p in (mock.patch.object(qm, "_OWNER_STOP_PATH", self.req), mock.patch.object(qm, "_ROOT", self.tmp)):
            p.start()
            self.addCleanup(p.stop)

    def _file(self, d):
        self.req.write_text(json.dumps(d))

    def _entry(self, price=147.48, until="2026-11-20", avg=173.51):
        self._file({"NVDA": {"price": price, "until": until, "avg_at_set": avg}})

    def _rows(self):
        p = self.tmp / "logs" / "trade_events.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def _mgr(self, pos, *, live=155.95, margin=1.5, now=NOW, design=147.48, save_ok=True, equity=2500.0):
        m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
        m.dry_run = False
        m._positions = {pos.symbol: pos}
        m.alerts = []
        m._alert = m.alerts.append
        m.saves = []
        m._save_state = lambda: m.saves.append(pos.stop_price) or save_ok
        m.clock = [now]
        m._now_et = lambda: m.clock[0]
        m.live_reads = []
        m._get_live_price = lambda s: m.live_reads.append(s) or live
        m._overnight_gap_margin = lambda s, px: margin
        m._design_stop_price = lambda s, avg: design
        m._get_account_equity = lambda: equity
        m._owner_stop_cycle = None
        return m


class ApplyAndRefuse(_Base):
    def test_apply_sets_record_marker_page_and_row(self):
        self._entry()
        pos = _pos()
        m = self._mgr(pos)
        live = m._maybe_apply_owner_stop(pos, [_stop(163.07)])
        self.assertEqual(live, 155.95)
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (147.48, "2026-11-20"))
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("stop set by owner $147.48 until 2026-11-20 (was $163.07)", m.alerts[0])
        self.assertIn("outside the daily kill switch", m.alerts[0])
        rows = self._rows()
        self.assertEqual([r["action"] for r in rows], ["apply"])
        self.assertEqual(rows[0]["stop_order_id"], "stop-1")
        # second cycle: already applied and the broker matches -> nothing new
        m._maybe_apply_owner_stop(pos, [_stop(147.48)])
        self.assertEqual(len(m.alerts), 1)

    def test_too_close_is_refused_once_a_day(self):
        self._entry(price=155.0)          # live 155.95 - margin 1.5 = 154.45 <= 155.0 -> refused
        pos = _pos()
        m = self._mgr(pos)
        m._maybe_apply_owner_stop(pos, [_stop(163.07)])
        m._maybe_apply_owner_stop(pos, [_stop(163.07)])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (163.07, None))
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("not applied", m.alerts[0])
        self.assertEqual([r["action"] for r in self._rows()], ["refused"])
        self.assertIsNone(m._owner_stop(pos, 155.95))

    def test_above_market_is_never_applied(self):
        self._entry(price=170.0)
        pos = _pos(stop=140.0)
        m = self._mgr(pos)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual(pos.stop_price, 140.0)

    def test_live_unreadable_changes_nothing(self):
        self._entry()
        pos = _pos()
        m = self._mgr(pos, live=None)
        self.assertIsNone(m._maybe_apply_owner_stop(pos, []))
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (163.07, None))

    def test_save_failure_reverts(self):
        self._entry()
        pos = _pos()
        m = self._mgr(pos, save_ok=False)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (163.07, None))

    def test_broker_mismatch_pages_once(self):
        self._entry()
        pos = _pos(stop=147.48, until="2026-11-20")
        m = self._mgr(pos)
        m._maybe_apply_owner_stop(pos, [_stop(163.07)])
        m._maybe_apply_owner_stop(pos, [_stop(163.07)])
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("moving it back to $147.48", m.alerts[0])

    def test_average_change_keeps_applying_and_pages_once(self):
        self._entry(avg=173.51)
        pos = _pos(stop=147.48, until="2026-11-20", qty=7, avg=168.00)
        m = self._mgr(pos)
        m._maybe_apply_owner_stop(pos, [_stop(147.48, qty=7)])
        m._maybe_apply_owner_stop(pos, [_stop(147.48, qty=7)])
        self.assertEqual(pos.stop_price, 147.48)
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("average is now $168.00", m.alerts[0])
        self.assertIn("$143.64 at risk", m.alerts[0])   # 7 x (168.00 - 147.48)

    def test_day_before_expiry_heads_up(self):
        self._entry(until="2026-11-14")
        pos = _pos(stop=147.48, until="2026-11-14")
        m = self._mgr(pos)
        m._maybe_apply_owner_stop(pos, [_stop(147.48)])
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("ends after 2026-11-14", m.alerts[0])


class ExpiryClearFreeze(_Base):
    def test_expiry_at_the_et_date_boundary(self):
        self._entry(until="2026-11-13")
        pos = _pos(stop=147.48, until="2026-11-13", restop_qty=3)
        m = self._mgr(pos, now=datetime(2026, 11, 13, 23, 59, tzinfo=qm.ET), design=140.0)
        m._maybe_apply_owner_stop(pos, [_stop(147.48)])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (147.48, "2026-11-13"))   # 23:59 still active
        m.clock[0] = datetime(2026, 11, 14, 0, 0, tzinfo=qm.ET)
        m._maybe_apply_owner_stop(pos, [_stop(147.48)])
        self.assertEqual((pos.stop_price, pos.owner_stop_until, pos.restop_qty), (140.0, None, 5))
        self.assertIn("$147.48 -> $140.00", m.alerts[-1])
        self.assertEqual(self._rows()[-1]["action"], "end")
        # afterwards the stale expired entry is inert
        n = len(m.alerts)
        m._maybe_apply_owner_stop(pos, [_stop(140.0)])
        self.assertEqual((pos.stop_price, len(m.alerts)), (140.0, n))

    def test_expiry_never_raises_through_the_market(self):
        self._entry(until="2026-11-12")
        pos = _pos(stop=147.48, until="2026-11-12")
        m = self._mgr(pos, design=155.0)          # 155.0 is not below live 155.95 - 1.5
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (147.48, None))
        self.assertIn("your last price kept", m.alerts[-1])

    def test_expiry_on_a_winner_never_lowers_the_stop(self):
        # cold review 11-14: avg 100, owner stop 150, live 180, the bot's own stop 85 -> the stop stays 150
        self._file({})
        pos = _pos(stop=150.0, until="2026-11-20", avg=100.0)
        m = self._mgr(pos, live=180.0, margin=1.8, design=85.0)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (150.0, None))
        self.assertIn("in profit", m.alerts[-1])
        self.assertEqual(self._rows()[-1]["stop_after"], 150.0)

    def test_expiry_on_a_loser_still_uses_the_bots_own_stop(self):
        self._file({})
        pos = _pos(stop=150.0, until="2026-11-20", avg=173.51)
        m = self._mgr(pos, live=155.95, design=140.0)        # live below the average: not a winner
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (140.0, None))

    def test_clear_ends_the_override(self):
        self._file({})
        pos = _pos(stop=147.48, until="2026-11-20")
        m = self._mgr(pos, design=145.0)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (145.0, None))
        self.assertIn("was cleared", m.alerts[-1])

    def test_expiry_with_live_unreadable_stays_frozen(self):
        self._file({})
        pos = _pos(stop=147.48, until="2026-11-20")
        m = self._mgr(pos, live=None)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (147.48, "2026-11-20"))

    def _freeze(self, m, pos):
        with self.assertLogs(qm.logger, level="CRITICAL") as cm:
            m._maybe_apply_owner_stop(pos, [])
            m._maybe_apply_owner_stop(pos, [])
        self.assertTrue(any("frozen" in x for x in cm.output))
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (147.48, "2026-11-20"))
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("ACTION", m.alerts[0])
        self.assertTrue(m._owner_stop_blocks(pos))
        self.assertEqual(m.saves, [])

    def test_unreadable_file_freezes(self):
        self.req.write_text("{not json")
        pos = _pos(stop=147.48, until="2026-11-20")
        self._freeze(self._mgr(pos), pos)

    def test_missing_file_freezes(self):
        pos = _pos(stop=147.48, until="2026-11-20")
        self._freeze(self._mgr(pos), pos)

    def test_entry_for_an_earlier_hold_is_not_carried_over(self):
        self._file({"NVDA": {"price": 147.48, "until": "2026-11-20", "entry_day": "2026-06-01"}})
        pos = _pos()
        m = self._mgr(pos)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (163.07, None))
        self.assertFalse(m._owner_stop_blocks(pos))

    def test_expiry_with_design_stop_unreadable_retries(self):
        self._file({})
        pos = _pos(stop=147.48, until="2026-11-20")
        m = self._mgr(pos, design=None)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (147.48, "2026-11-20"))

    def test_frozen_hold_fresh_submitter_keeps_frozen_price(self):
        self.req.write_text("{not json")
        pos = _pos(stop=147.48, until="2026-11-20")
        m = self._mgr(pos)
        self.assertEqual(m._use_owner_stop(pos, 163.07, "stop after a trim"), 147.48)
        m2 = self._mgr(pos, live=148.0)            # frozen price no longer below live - m -> QHM's own
        self.assertEqual(m2._use_owner_stop(pos, 140.0, "stop after a trim"), 140.0)

    def test_missing_file_without_marker_is_no_override(self):
        pos = _pos()
        m = self._mgr(pos)
        self.assertIsNone(m._maybe_apply_owner_stop(pos, []))
        self.assertFalse(m._owner_stop_blocks(pos))
        self.assertEqual(m.alerts, [])

    def test_one_file_read_per_cycle(self):
        self._entry()
        pos = _pos()
        m = self._mgr(pos)
        reads = []
        real = m._load_owner_stops
        m._load_owner_stops = lambda: reads.append(1) or real()

        def body():
            m._owner_stop_blocks(pos)
            m._owner_stop_blocks(pos)
            m._maybe_apply_owner_stop(pos, [])
        m._run_weekly_check = body
        m.run_weekly_check()
        self.assertEqual(len(reads), 1)
        self.assertIsNone(m._owner_stop_cycle)


class RaiseWriters(_Base):
    def test_trailing_lock_skipped_while_active(self):
        self._entry()
        pos = _pos(stop=150.0, avg=150.0, until="2026-11-20")
        m = self._mgr(pos, live=180.0)
        with mock.patch("execution.broker.cancel_order") as cancel, \
                mock.patch("execution.broker.is_market_open", return_value=True):
            m._maybe_trailing_lock(pos, "2026-11-13")
            m._maybe_trailing_lock(pos, "2026-11-13")
        cancel.assert_not_called()
        self.assertEqual(pos.stop_price, 150.0)
        self.assertEqual([r["action"] for r in self._rows()], ["skip"])

    def test_restop_skipped_while_active(self):
        self._entry()
        pos = _pos(stop=147.48, until="2026-11-20", qty=7, restop_qty=5)
        m = self._mgr(pos)
        with mock.patch("execution.broker.replace_stop_order") as rep:
            self.assertIsNone(m._maybe_restop_after_add(pos, [_stop(147.48, qty=7)]))
        rep.assert_not_called()
        self.assertEqual((pos.stop_price, pos.restop_qty), (147.48, 5))

    def test_add_raise_helper_skips_and_rows_once(self):
        self._entry()
        pos = _pos(stop=147.48, until="2026-11-20")
        m = self._mgr(pos)
        self.assertTrue(m._owner_stop_skip(pos, "tranche-add stop raise"))
        self.assertTrue(m._owner_stop_skip(pos, "tranche-add stop raise"))
        self.assertEqual([r["writer"] for r in self._rows()], ["tranche-add stop raise"])

    def test_refused_request_does_not_turn_off_the_raise_writers(self):
        # claims review 11-14: request $155.00, price $155.95, margin $1.50 -> refused; QHM's own rules keep running
        self._entry(price=155.0)
        pos = _pos(stop=150.0)
        m = self._mgr(pos, live=155.95, margin=1.5)
        m._maybe_apply_owner_stop(pos, [])
        self.assertEqual((pos.stop_price, pos.owner_stop_until), (150.0, None))
        self.assertFalse(m._owner_stop_skip(pos, "trailing lock"))
        self.assertEqual([r["action"] for r in self._rows()], ["refused"])

    def test_request_not_yet_applied_does_not_skip(self):
        self._entry()
        pos = _pos()                                  # active request in the file, nothing applied to the hold yet
        self.assertFalse(self._mgr(pos)._owner_stop_skip(pos, "tranche-add stop raise"))

    def test_no_override_does_not_skip(self):
        self._file({})
        pos = _pos()
        self.assertFalse(self._mgr(pos)._owner_stop_skip(pos, "trailing lock"))


class FreshStopSubmitters(_Base):
    def _mgr_submit(self, pos, **kw):
        m = self._mgr(pos, **kw)
        m.submitted = []
        m._dispatcher = SimpleNamespace(submit_gtc_stop=lambda b, s, q, side, px: m.submitted.append(px)
                                        or SimpleNamespace(id="stop-9"))
        m.broker = SimpleNamespace()
        m._handle_missing_stop = lambda p, dry_run=False: None
        m._resync_from_alpaca = lambda p: None
        return m

    def test_first_stop_uses_owner_price(self):
        self._entry(price=150.0)
        pos = _pos(stop=0.0, avg=200.0)
        m = self._mgr_submit(pos, live=190.0)
        with mock.patch("data.fetcher.fetch_bars", return_value=pd.DataFrame()):
            m._compute_and_submit_stop(pos)
        self.assertEqual((m.submitted, pos.stop_price, pos.owner_stop_until), ([150.0], 150.0, "2026-11-20"))

    def test_first_stop_with_live_unreadable_uses_qhm_own(self):
        self._entry(price=150.0)
        pos = _pos(stop=0.0, avg=200.0)
        m = self._mgr_submit(pos, live=None)
        with mock.patch("data.fetcher.fetch_bars", return_value=pd.DataFrame()):
            m._compute_and_submit_stop(pos)
        self.assertEqual(m.submitted, [170.0])   # QHM's own 15% floor on $200

    def test_post_earnings_stop_uses_owner_price(self):
        self._entry(price=150.0)
        pos = _pos(stop=0.0, avg=200.0)
        pos.state = qm.HoldState.PENDING_EARNINGS
        m = self._mgr_submit(pos, live=190.0)
        with mock.patch("data.fetcher.fetch_bars", return_value=pd.DataFrame()):
            self.assertTrue(m._resubmit_post_earnings_stop(pos))
        self.assertEqual(m.submitted, [150.0])

    def test_restore_after_trim_uses_owner_price(self):
        self._entry(price=147.48)
        pos = _pos()
        m = self._mgr_submit(pos)
        m._earnings_trim_restore_stop(pos, 3, 163.07, "owner-trim post-sell stop")
        self.assertEqual((m.submitted, pos.stop_price), ([147.48], 147.48))

    def test_restore_without_override_unchanged(self):
        self._file({})
        pos = _pos()
        m = self._mgr_submit(pos)
        m._earnings_trim_restore_stop(pos, 3, 163.07, "owner-trim post-sell stop")
        self.assertEqual(m.submitted, [163.07])


class Script(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()
        self.holds = self.tmp / "quarterly_holds.json"
        self.holds.write_text(json.dumps({"NVDA": {"state": "ACTIVE", "qty_filled": 5, "avg_entry_price": 173.51,
                                                   "entry_day": "2026-09-29", "stop_price": 163.07,
                                                   "stop_order_id": "stop-1"}}))
        self.req = self.tmp / "qhm_owner_stops.json"
        self.events = self.tmp / "logs" / "trade_events.jsonl"
        for p in (mock.patch.object(cli, "REQ", self.req), mock.patch.object(cli, "HOLDS", self.holds),
                  mock.patch.object(cli, "EVENTS", self.events),
                  mock.patch.object(cli, "datetime", SimpleNamespace(now=lambda tz=None: NOW.astimezone(tz)))):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, *argv, equity=2500.0, orders=None):
        out = io.StringIO()
        with mock.patch("execution.broker.get_account", return_value=SimpleNamespace(equity=equity)), \
                mock.patch("execution.broker.get_open_orders", return_value=orders), redirect_stdout(out):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
                out.write(str(e.code))
        return rc, out.getvalue()

    def test_set_writes_file_and_row(self):
        # loss 5 x (173.51 - 160) = 67.55 <= 7% x 2500 = 175 -> no --confirm-risk needed
        rc, out = self._run("set", "nvda", "160", "--until", "2026-11-20")
        self.assertEqual(rc, 0, out)
        rec = json.loads(self.req.read_text())["NVDA"]
        self.assertEqual((rec["price"], rec["until"], rec["avg_at_set"]), (160.0, "2026-11-20", 173.51))
        self.assertIn("2.7% of equity", out)
        row = json.loads(self.events.read_text().splitlines()[-1])
        self.assertEqual((row["action"], row["stop_order_id"]), ("set", "stop-1"))

    def test_confirm_risk_required_above_the_kill_share(self):
        # loss 5 x (173.51 - 100) = 367.55 > 175 -> refused, then accepted with --confirm-risk
        rc, out = self._run("set", "NVDA", "100", "--until", "2026-11-20")
        self.assertNotEqual(rc, 0)
        self.assertIn("--confirm-risk", out)
        self.assertFalse(self.req.exists())
        rc, out = self._run("set", "NVDA", "100", "--until", "2026-11-20", "--confirm-risk")
        self.assertEqual(rc, 0, out)
        self.assertTrue(json.loads(self.req.read_text())["NVDA"]["confirm_risk"])

    def test_equity_unreadable_requires_confirm_risk(self):
        rc, out = self._run("set", "NVDA", "160", "--until", "2026-11-20", equity=float("nan"))
        self.assertNotEqual(rc, 0)
        self.assertFalse(self.req.exists())

    def test_refusals(self):
        self.assertNotEqual(self._run("set", "AAPL", "160", "--until", "2026-11-20")[0], 0)   # not held
        self.assertNotEqual(self._run("set", "NVDA", "-1", "--until", "2026-11-20")[0], 0)    # price
        self.assertNotEqual(self._run("set", "NVDA", "160", "--until", "2026-11-12")[0], 0)   # past
        self.assertNotEqual(self._run("set", "NVDA", "160", "--until", "11/20/2026")[0], 0)   # malformed
        rc, out = self._run("set", "NVDA", "160", "--until", "2026-12-30")                    # past the max-hold exit
        self.assertNotEqual(rc, 0)
        self.assertIn("2026-12-29", out)
        self.assertFalse(self.req.exists())

    def test_clear_rewrites_never_deletes(self):
        self._run("set", "NVDA", "160", "--until", "2026-11-20")
        rc, _out = self._run("clear", "NVDA")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.req.read_text()), {})
        self.assertEqual(json.loads(self.events.read_text().splitlines()[-1])["action"], "clear")

    def test_unreadable_file_show_reports_and_clear_repairs(self):
        self.req.write_text("{not json")
        rc, out = self._run("show")
        self.assertNotEqual(rc, 0)
        self.assertIn("unreadable", out)
        self.assertEqual(self.req.read_text(), "{not json")              # show never changes the file
        rc, out = self._run("clear", "NVDA")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.req.read_text()), {})
        kept = [p for p in self.tmp.iterdir() if ".corrupt." in p.name]
        self.assertEqual([p.read_text() for p in kept], ["{not json"])

    def test_unreadable_file_set_repairs_and_sets(self):
        self.req.write_text("{not json")
        rc, out = self._run("set", "NVDA", "160", "--until", "2026-11-20")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.req.read_text())["NVDA"]["price"], 160.0)

    def test_unreadable_file_refused_set_still_leaves_a_readable_file(self):
        self.req.write_text("{not json")
        self.assertNotEqual(self._run("set", "AAPL", "160", "--until", "2026-11-20")[0], 0)
        self.assertEqual(json.loads(self.req.read_text()), {})

    def test_clear_recreates_a_missing_file(self):
        rc, out = self._run("clear", "NVDA")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.req.read_text()), {})

    def test_show_lists_owner_qhm_and_broker_stops(self):
        self._run("set", "NVDA", "160", "--until", "2026-11-20")
        rc, out = self._run("show", orders=[_stop(163.07)])
        self.assertEqual(rc, 0)
        self.assertIn("owner stop $160.0 until 2026-11-20 (active)", out)
        self.assertIn("QHM recorded stop $163.07", out)
        self.assertIn("163.07", out.split("broker QH stop")[1])

    def test_show_broker_unreadable(self):
        self._run("set", "NVDA", "160", "--until", "2026-11-20")
        rc, out = self._run("show", orders=None)
        self.assertEqual(rc, 0)
        self.assertIn("broker QH stop unreadable", out)


if __name__ == "__main__":
    unittest.main()
