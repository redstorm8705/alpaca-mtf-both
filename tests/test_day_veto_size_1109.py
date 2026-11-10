#!/usr/bin/env python3
# ruff: noqa: E501
"""DAY-VETO-SIZE-1109 (design record logs/design_records/day_veto_size_2026-11-09.md, revision 8): Track B trend vetoes
change size, not count. Every broker / order call is mocked — nothing here can reach Alpaca."""
from __future__ import annotations

import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import config
import data.alpaca_data  # noqa: F401 — patched below by dotted path
import run_day_tier
from execution import broker
from execution import day_trade_manager as dtm
from execution import day_veto_sizing as dvs
from execution import earned_sizing as es

REPO_PRIOR = Path(dvs.__file__).resolve().parent.parent / "data" / "state" / "day_veto_prior.json"


def _prior(tmp: Path, sessions=None, variants=34) -> Path:
    p = tmp / "prior.json"
    p.write_text(json.dumps({"variants_tried": variants, "pause_upper_quantile": 0.95, "sessions": sessions or [
        {"date": "2025-01-0%d" % i, "n": 4, "wins": 2, "sum_win_r": 3.0, "sum_loss_r": 2.0, "sum_r": 1.0}
        for i in range(1, 8)]}))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        dvs._TICK.clear()
        dvs._SUBMITTED.clear()
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._p = [mock.patch.object(dvs, "PAUSE_PATH", self.tmp / "pause.json"),
                   mock.patch.object(dvs, "LEDGER_PATH", self.tmp / "ledger.json"),
                   mock.patch.object(dvs, "PRIOR_PATH", self.tmp / "missing_prior.json"),
                   mock.patch.object(dvs, "PAGES_PATH", self.tmp / "pages.json"),
                   mock.patch.object(dvs, "_tertiary_path", return_value=self.tmp / "tertiary.json"),
                   mock.patch.object(dvs, "_secondary_has", side_effect=lambda k: k in self.secondary),
                   mock.patch.object(dvs, "_secondary_mark", side_effect=self._sec_mark),
                   mock.patch.object(config, "DAYTRADE_VETO_DOWNSIZE_ENABLED", True, create=True)]
        self.secondary: dict = {}
        self.secondary_ok = True
        for p in self._p:
            p.start()
        dvs._PAGED.clear()

    def _sec_mark(self, k):
        if not self.secondary_ok:
            return False
        self.secondary[k] = True
        return True

    def tearDown(self):
        for p in self._p:
            p.stop()
        dvs._TICK.clear()
        dvs._SUBMITTED.clear()
        dvs._PAGED.clear()
        self._tmp.cleanup()


# ── direction rules (item 1) ───────────────────────────────────────────────────────────────────────────────────────
class DirectionRulesB(Base):
    def _blk(self, side, direction, checks=None):
        al = {"aligned": True, "checks": checks or {}, "reason": "test"}
        with mock.patch.object(run_day_tier, "_alignment_for", return_value=al), \
                mock.patch.object(config, "DAYTRADE_ALIGN_GATE_BLOCKS", False, create=True), \
                mock.patch.object(config, "DAYTRADE_DIRECTION_RULES_BLOCK", True, create=True):
            return run_day_tier._direction_block_b("NVDA", side, direction, "DRIVE", {"alignment": {}})

    def test_two_sided_relaxed_and_tagged(self):
        blk, why, veto = self._blk("TWO_SIDED", "long")
        self.assertFalse(blk)
        self.assertIn("no clear trend direction", veto)

    def test_unknown_stays_blocked(self):
        blk, why, veto = self._blk("UNKNOWN", "long")
        self.assertTrue(blk)
        self.assertEqual(veto, "")

    def test_long_against_short_side_relaxed(self):
        blk, _why, veto = self._blk("SHORT", "long")
        self.assertFalse(blk)
        self.assertIn("long against the SHORT daily side", veto)

    def test_rule2_opposition_still_blocks_a_relaxed_setup(self):
        opp = {"ema2": False, "vwap2": False, "ema5": False, "vwap5": False}
        for side in ("TWO_SIDED", "SHORT"):
            blk, why, veto = self._blk(side, "long", checks=opp)
            self.assertTrue(blk, side)
            self.assertIn("every 2m/5m check opposes", why)
            self.assertEqual(veto, "")

    def test_invalid_direction_blocks(self):
        self.assertTrue(self._blk("TWO_SIDED", "sideways")[0])

    def test_flag_false_restores_blocking(self):
        with mock.patch.object(config, "DAYTRADE_VETO_DOWNSIZE_ENABLED", False):
            for side in ("TWO_SIDED", "SHORT"):
                blk, _why, veto = self._blk(side, "long")
                self.assertTrue(blk)
                self.assertEqual(veto, "")

    def test_with_trend_unchanged(self):
        self.assertEqual(self._blk("LONG", "long"), (False, "", ""))


# ── posterior / m math (items 2, 3) ──────────────────────────────────────────────────────────────────────────────
class Math(Base):
    def test_repo_prior_matches_design_record(self):
        pr = dvs.load_prior(REPO_PRIOR)
        self.assertIsNotNone(pr)
        self.assertEqual(pr["n_trades"], 682)
        post = dvs.posterior(pr, [])
        self.assertAlmostEqual(post["n_prior_eff"], 682 / 34, places=3)
        self.assertAlmostEqual(post["f_star"], 0.012099, places=5)
        self.assertLess(post["f_lcb"], post["f_star"])
        self.assertAlmostEqual(post["p"] - (1 - post["p"]) / post["b"], post["f_star"], places=9)

    def test_missing_prior_gives_m_floor(self):
        self.assertIsNone(dvs.load_prior(self.tmp / "nope.json"))
        self.assertIsNone(dvs.posterior(None, []))
        self.assertIsNone(dvs.m_kelly(None))
        m, path = dvs.choose_m(dvs.m_kelly(None), 0.2)
        self.assertEqual(m, 0.2)
        self.assertIn("m_floor", path)

    def test_m_floor_survives_the_bounded_step(self):
        for q, ps, rb, rp in ((1, 3.3, 2500.0, 0.015), (2, 0.07, 2471.13, 0.015), (3, 1.0 / 3, 1000.0, 0.01)):
            mf = dvs.m_floor(q, ps, rb, rp)
            self.assertGreaterEqual(int((rb * (rp * mf)) // ps), q)
        self.assertIsNone(dvs.m_floor(1, 0.0, 2500.0, 0.015))
        self.assertGreater(dvs.m_floor(1, 100.0, 2500.0, 0.015), 1.0)

    def test_live_losses_lower_the_lcb_and_clamp_to_floor(self):
        pr = dvs.load_prior(_prior(self.tmp))
        good = dvs.posterior(pr, [])
        bad = dvs.posterior(pr, [{"R": -1.0, "session": f"2026-11-0{i}"} for i in range(2, 8)])
        self.assertLess(bad["f_lcb"], good["f_lcb"])
        m, _ = dvs.choose_m(dvs.m_kelly(bad), 0.1)
        self.assertEqual(m, 0.1)


# ── place_entry sizing (items 2, 3, 4, 5, 7) ─────────────────────────────────────────────────────────────────────
class PlaceEntryVeto(Base):
    def _run(self, *, trigger_extra=None, open_log=None, events=None, equity="2500", conv_called=None, state=None,
             whole_journal=None, positions=None):
        if positions is None:
            # default broker book: the broker holds every journal / state lot the test sets up (revision 5 item 1 —
            # the stale-lot and unreadable-book tests pass their own book)
            positions = []
            for t in list((open_log or {}).values()) + [v for v in (state or {}).values() if isinstance(v, dict)]:
                q = float(t.get("fill_qty") or t.get("qty") or 0)
                if t.get("symbol") and q > 0:
                    positions.append(SimpleNamespace(symbol=t["symbol"], qty=str(q), side=t.get("side") or "long",
                                                     market_value=str(q), current_price="1"))
        from data.live_price import LivePrice
        sub: dict = {}
        logged: dict = {}
        acct = SimpleNamespace(equity=equity, last_equity=equity, buying_power="9000", maintenance_margin="0",
                               trading_blocked=False, account_blocked=False)

        def _submit(sym, qty, side, px, **k):
            sub.update(qty=qty, px=px, coid=k.get("client_order_id"))
            return None

        def _log_decision(did, sym, **k):
            logged.setdefault("calls", []).append((did, k))
            return True
        trigger = {"trigger": "ENTER", "direction": "long", "mode": "DRIVE", "entry_ref": 100.0, "target": None,
                   "wall_ref": 97.0, "veto_downsized": True, "veto_reason": "no clear trend direction (side=TWO_SIDED)",
                   "veto": {"rule": "no clear trend direction (side=TWO_SIDED)", "side": "TWO_SIDED", "m_kelly": None},
                   **(trigger_extra or {})}
        ci = mock.Mock(return_value={"stage": "ok"})
        cc = mock.Mock(return_value=(None, None))
        with ExitStack() as x:
            for p in (
                mock.patch.object(dtm, "_enabled", return_value=True),
                mock.patch.object(dtm, "_load_state", side_effect=lambda: dict(state or {})),
                mock.patch("strategy.day_tier_logger.read_events",
                           side_effect=lambda trade_id=None: [e for e in (whole_journal or [])
                                                              if trade_id is None or e.get("trade_id") == trade_id]),
                mock.patch.object(dtm, "_save_state", return_value=True),
                mock.patch.object(dtm, "_room_stop", side_effect=lambda _s, _d, _l, st: (st, "kept")),
                mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None),
                mock.patch.object(dtm, "_daily_risk_used", return_value=(0.0, 0.0, "")),
                mock.patch.object(dtm, "_conv_inputs", ci),
                mock.patch.object(dtm, "_conv_compute", cc),
                mock.patch("data.live_price.live_price", return_value=LivePrice(100.0, "iex_trade", 1.0)),
                mock.patch("data.alpaca_data.get_latest_quote", return_value=None),
                mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value=open_log or {}),
                mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(list(events or []), True)),
                mock.patch("strategy.day_tier_logger.log_decision", side_effect=_log_decision),
                mock.patch.object(broker, "get_account", return_value=acct),
                (mock.patch.object(broker, "get_open_positions", side_effect=positions)
                 if isinstance(positions, Exception)
                 else mock.patch.object(broker, "get_open_positions", return_value=list(positions))),
                mock.patch.object(broker, "get_open_orders", return_value=[]),
                mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30),
                mock.patch("execution.tier_capital_allocator.live_admit",
                           return_value=SimpleNamespace(approved=True, lease=None, reason="")),
                mock.patch("execution.tier_capital_allocator.live_order_id", return_value=None),
                mock.patch("execution.tier_capital_allocator.live_release", return_value=None),
                mock.patch.object(broker, "submit_limit_order", side_effect=_submit),
                mock.patch.object(broker, "submit_bracket_entry", side_effect=AssertionError("no bracket")),
                mock.patch.object(config, "DAYTRADE_TRACK_B_CASH_ONLY", False),
            ):
                x.enter_context(p)
            size = {"size_ok": True, "shares": 5, "conviction": 0.5, "track": "B", "budget": 300.0, "max_size": True}
            ok = dtm.place_entry("AAPL", {"would_consider": True, "conviction": 0.5, "track": "B"}, trigger, size,
                                 bar_id="20261110-1000", equity=float(equity))
        return ok, sub, logged, ci, cc

    def test_veto_setup_reaches_submit_at_min_qty_with_marker_and_no_conv(self):
        _ok, sub, logged, ci, cc = self._run()
        self.assertGreaterEqual(sub.get("qty", 0), 1)                 # min_entry_qty of a stock
        self.assertTrue(dvs.is_veto_coid(sub["coid"]))                # item 4: the order carries the veto marker
        ci.assert_not_called()                                        # item 2b: CONV-1018 excluded
        cc.assert_not_called()
        did, kw = logged["calls"][-1]
        v = kw["size"]["veto"]
        self.assertTrue(kw["size"]["veto_downsized"])
        self.assertFalse(kw["size"]["max_size"])                      # never 10/10 maximum size
        self.assertAlmostEqual(kw["size"]["risk_mult"], v["m_floor"], places=6)   # prior missing -> m_floor
        for k in ("rule", "side", "m_floor", "m", "risk_mult", "qty_without_veto_bounded_path", "qty_without_veto_note",
                  "sizing_path", "final_qty", "dollar_risk_pct_equity", "cluster_open_lots", "cluster_stop_risk_usd"):
            self.assertIn(k, v)
        self.assertGreater(v["qty_without_veto_bounded_path"], v["final_qty"])   # downsized vs the bounded path at m=1
        self.assertEqual(len(dvs._SUBMITTED), 0)                       # submit returned None: nothing submitted

    def test_kelly_m_above_floor_is_used(self):
        _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 0.5}})
        v = logged["calls"][-1][1]["size"]["veto"]
        self.assertAlmostEqual(v["m"], 0.5)
        self.assertGreater(sub["qty"], 1)

    def test_one_share_over_cap_skipped_before_m_math(self):
        with mock.patch.object(dvs, "m_floor", side_effect=AssertionError("m math ran")) as mf:
            _ok, sub, logged, *_ = self._run(trigger_extra={"wall_ref": 40.0})   # 60/share x 1.2 > 2% of $2,500
        mf.assert_not_called()
        self.assertEqual(sub, {})
        self.assertIn("per-trade cap", logged["calls"][-1][1]["trigger"]["skip_reason"])
        self.assertEqual(dtm.last_entry_skip("AAPL")["reason"], "below_min_qty")

    def test_m_floor_above_one_skipped(self):
        # 1.5% basis of $2,500 = $37.50 < a $45 one-share stop risk, while 45 x 1.2 = $54 > $50 cap would skip first;
        # widen the per-trade cap so only the risk-basis check fails.
        with mock.patch.object(config, "DAYTRADE_PER_TRADE_RISK_EQUITY_PCT", 0.025):
            with mock.patch.object(config, "DAYTRADE_PER_TRADE_RISK_BASIS_PCT", 0.015):
                _ok, sub, logged, *_ = self._run(trigger_extra={"wall_ref": 55.0})
        self.assertEqual(sub, {})
        self.assertIn("risk basis allows", logged["calls"][-1][1]["trigger"]["skip_reason"])

    def test_cluster_shrinks_then_skips(self):
        coid = "DT-NVDA-b-1-vd" + "a" * 32
        open_log = {coid: {"trade_id": coid, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 5}}
        # existing veto long lot risks 5 x $7 = $35 (x1.2 = $42) of the $50 cap -> room $8 / (1.2 x 3.3) = 2 shares
        ev = [{"event": "stop_placed", "trade_id": coid, "stop_price": 193.0}]
        _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=ev)
        self.assertEqual(sub["qty"], 2)
        v = logged["calls"][-1][1]["size"]["veto"]
        self.assertEqual(v["cluster_open_lots"], 1)
        # bigger open lot: no room for one share -> skipped
        ev2 = [{"event": "stop_placed", "trade_id": coid, "stop_price": 191.0}]
        _ok, sub2, logged2, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=ev2)
        self.assertEqual(sub2, {})
        self.assertIn("cluster full", logged2["calls"][-1][1]["trigger"]["skip_reason"])

    def test_opposite_direction_lot_does_not_count(self):
        coid = "DT-NVD-b-1-vd" + "b" * 32   # an inverse-fund buy = SHORT exposure
        open_log = {coid: {"trade_id": coid, "symbol": "NVD", "side": "long", "entry_price": 10.0, "fill_qty": 100}}
        ev = [{"event": "stop_placed", "trade_id": coid, "stop_price": 9.6}]
        with mock.patch.object(dvs, "exposure_sign", side_effect=lambda s, side: -1 if s == "NVD" else 1):
            _ok, sub, logged, *_ = self._run(open_log=open_log, events=ev)
        self.assertEqual(logged["calls"][-1][1]["size"]["veto"]["cluster_open_lots"], 0)

    def test_flag_false_refuses_a_tagged_setup(self):
        with mock.patch.object(config, "DAYTRADE_VETO_DOWNSIZE_ENABLED", False):
            ok, sub, *_ = self._run()
        self.assertFalse(ok)
        self.assertEqual(sub, {})

    def test_submitted_veto_counts_in_the_cluster_this_tick(self):
        dvs.record_submitted("DT-X-b-1-vd" + "c" * 32, 1, 35.0)
        _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}})
        self.assertEqual(sub["qty"], 2)

    # cold review threat 2: an overnight veto lot (stop row from an earlier day) is counted with its real stop
    def test_overnight_lot_counted_from_its_whole_journal_stop(self):
        coid = "DT-NVDA-b-1-vd" + "f" * 32
        open_log = {coid: {"trade_id": coid, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 5}}
        old_stop = [{"event": "stop_placed", "trade_id": coid, "stop_price": 193.0, "ts": "2026-11-09T12:00:00-08:00"}]
        _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=[],
                                         whole_journal=old_stop)
        self.assertEqual(sub["qty"], 2)                               # counted at its $7 stop, not blocked
        self.assertEqual(logged["calls"][-1][1]["size"]["veto"]["cluster_open_lots"], 1)

    def test_overnight_lot_stop_from_state_record(self):
        coid = "DT-NVDA-b-1-vd" + "1" * 32
        open_log = {coid: {"trade_id": coid, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 5}}
        state = {"entry::NVDA::20261109-1000": {"coid": coid, "symbol": "NVDA", "side": "long", "state": "protected",
                                                "bar_id": "20261109-1000", "qty": 5, "stop_px": 193.0}}
        _ok, sub, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=[], state=state)
        self.assertEqual(sub["qty"], 2)

    def test_lot_with_no_stop_anywhere_fails_closed_and_pages_once(self):
        coid = "DT-NVDA-b-1-vd" + "2" * 32
        open_log = {coid: {"trade_id": coid, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 5}}
        with mock.patch("alerts.send_slack") as page:
            _ok, sub, logged, *_ = self._run(open_log=open_log, events=[])
            _ok, sub2, *_ = self._run(open_log=open_log, events=[])
        self.assertEqual(sub, {})
        self.assertEqual(sub2, {})
        self.assertIn("stop/entry not found", logged["calls"][-1][1]["trigger"]["skip_reason"])
        self.assertEqual(page.call_count, 1)

    # cold review threat 3: a state-only veto lot (entry_fill write failed) counts in the cluster at its state stop
    def test_state_only_veto_lot_counts(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        coid = "DT-NVDA-b-1-vd" + "3" * 32
        state = {f"entry::NVDA::{today}-0945": {"coid": coid, "symbol": "NVDA", "side": "long", "state": "protected",
                                                "bar_id": f"{today}-0945", "qty": 5, "fill_qty": 5, "fill_px": 200.0,
                                                "stop_px": 193.0, "track": "B"}}
        _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, state=state)
        self.assertEqual(sub["qty"], 2)
        self.assertEqual(logged["calls"][-1][1]["size"]["veto"]["cluster_open_lots"], 1)

    # nit b: SKIP-VETO decision ids are unique per attempt
    def test_skip_ids_unique(self):
        _o1, _s1, l1, *_ = self._run(trigger_extra={"wall_ref": 40.0})
        _o2, _s2, l2, *_ = self._run(trigger_extra={"wall_ref": 40.0})
        self.assertNotEqual(l1["calls"][-1][0], l2["calls"][-1][0])

    # nit e: an unmapped fund never counts as long
    def test_unmapped_fund_sign_unknown_skips_and_pages_once(self):
        with mock.patch.object(config, "LEVERAGED_3X_TICKERS", {"ZZZQ"}), mock.patch("alerts.send_slack") as page:
            self.assertIsNone(dvs.exposure_sign("ZZZQ", "long"))
            self.assertIsNone(dvs.exposure_sign("ZZZQ", "long"))
            self.assertEqual(page.call_count, 1)
            self.assertEqual(dvs.exposure_sign("AAPL", "short"), -1)
            coid = "DT-ZZZQ-b-1-vd" + "4" * 32
            open_log = {coid: {"trade_id": coid, "symbol": "ZZZQ", "side": "long", "entry_price": 10.0, "fill_qty": 5}}
            _ok, sub, logged, *_ = self._run(open_log=open_log)
        self.assertEqual(sub, {})
        self.assertIn("exposure direction unknown", logged["calls"][-1][1]["trigger"]["skip_reason"])

    # revision 3 item 1: a missing side is unknown (never long). open_trades_from_log copies the entry_fill row's side,
    # so a malformed row without it yields side None — exercised here through the real open-set derivation.
    def test_missing_side_fails_closed_and_pages_once(self):
        from strategy import day_tier_logger as dtl
        coid = "DT-NVDA-s-1-vd" + "9" * 32
        open_log = dtl.open_trades_from_log([{"event": "entry_fill", "trade_id": coid, "symbol": "NVDA",
                                              "fill_price": 200.0, "fill_qty": 5}])
        self.assertIsNone(open_log[coid]["side"])
        ev = [{"event": "stop_placed", "trade_id": coid, "stop_price": 207.0}]
        with mock.patch("alerts.send_slack") as page:
            _ok, sub, logged, *_ = self._run(open_log=open_log, events=ev)
            self._run(open_log=open_log, events=ev)
        self.assertEqual(sub, {})
        self.assertIn("side unknown", logged["calls"][-1][1]["trigger"]["skip_reason"])
        self.assertEqual(page.call_count, 1)

    # revision 3 item 2: an OVERNIGHT veto lot whose entry_fill write failed (state record only, earlier day) counts
    def test_overnight_state_only_veto_lot_counts_and_exited_one_does_not(self):
        coid, gone = "DT-NVDA-b-1-vd" + "a1" * 16, "DT-NVDA-b-2-vd" + "b2" * 16
        state = {"entry::NVDA::20261106-1000": {"coid": coid, "symbol": "NVDA", "side": "long", "state": "protected",
                                                "bar_id": "20261106-1000", "qty": 5, "fill_qty": 5, "fill_px": 200.0,
                                                "stop_px": 193.0, "track": "B"},
                 "entry::MU::20261106-1015": {"coid": gone, "symbol": "MU", "side": "long", "state": "protected",
                                              "bar_id": "20261106-1015", "qty": 50, "fill_qty": 50, "fill_px": 100.0,
                                              "stop_px": 90.0, "track": "B"}}
        whole = [{"event": "exit_fill", "trade_id": gone, "fill_price": 95.0, "fill_qty": 50}]
        _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, state=state, whole_journal=whole)
        self.assertEqual(sub["qty"], 2)                               # the $35 NVDA lot counted; MU (exited) not
        self.assertEqual(logged["calls"][-1][1]["size"]["veto"]["cluster_open_lots"], 1)

    # revision 5 item 1: the broker is the truth — a state-only FILLED lot that exited at the broker (no journal rows)
    # is not counted and pages once
    def test_state_only_filled_lot_gone_at_broker_not_counted(self):
        coid = "DT-NVDA-b-1-vd" + "c3" * 16
        state = {"entry::NVDA::20261106-1000": {"coid": coid, "symbol": "NVDA", "side": "long", "state": "protected",
                                                "bar_id": "20261106-1000", "qty": 5, "fill_qty": 5, "fill_px": 200.0,
                                                "stop_px": 193.0, "track": "B"}}
        with mock.patch("alerts.send_slack") as page:
            _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, state=state, positions=[])
            dvs._PAGED.clear()
            self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, state=state, positions=[])
        self.assertEqual(logged["calls"][-1][1]["size"]["veto"]["cluster_open_lots"], 0)
        self.assertGreater(sub["qty"], 2)                             # full room: the stale lot does not block
        self.assertEqual(sum("stale record" in c.args[0] for c in page.call_args_list), 1)

    def test_lots_of_one_symbol_capped_at_broker_qty(self):
        a, b = "DT-NVDA-b-1-vd" + "c4" * 16, "DT-NVDA-b-2-vd" + "c5" * 16
        open_log = {a: {"trade_id": a, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 3},
                    b: {"trade_id": b, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 3}}
        ev = [{"event": "stop_placed", "trade_id": a, "stop_price": 193.0},
              {"event": "stop_placed", "trade_id": b, "stop_price": 193.0}]
        book = [SimpleNamespace(symbol="NVDA", qty="4", side="long", market_value="800", current_price="200")]
        with mock.patch("alerts.send_slack"):
            _ok, _sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=ev,
                                              positions=book)
        v = logged["calls"][-1][1]["size"]["veto"]
        self.assertAlmostEqual(v["cluster_stop_risk_usd"], round(1.2 * 7.0 * 4, 2))   # 4 broker shares, not 6

    def test_broker_book_unreadable_skips_new_veto_setups(self):
        _ok, sub, *_ = self._run(positions=RuntimeError("positions endpoint down"))
        self.assertEqual(sub, {})                                     # place_entry fails closed on the live book
        r = dvs.cluster_risk({}, {}, 1, broker_positions=None)
        self.assertFalse(r["ok"])
        self.assertIn("broker position book unreadable", r["why"])

    # revision 5 item 5 (nit 7): _veto_sizing has no unused `size` argument
    def test_veto_sizing_signature(self):
        import inspect
        self.assertNotIn("size", inspect.signature(dtm._veto_sizing).parameters)

    # revision 6 item a: a lot the broker no longer holds is dropped BEFORE any missing-data fail-closed
    def test_unheld_lot_with_no_stop_does_not_block(self):
        coid = "DT-NVDA-b-1-vd" + "d7" * 16
        open_log = {coid: {"trade_id": coid, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 5}}
        with mock.patch("alerts.send_slack") as page:
            _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=[],
                                             positions=[])
            dvs._PAGED.clear()
            _ok, sub2, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=[],
                                      positions=[])
        self.assertGreater(sub["qty"], 2)                             # not blocked, full room
        self.assertGreater(sub2["qty"], 2)
        self.assertEqual(logged["calls"][-1][1]["size"]["veto"]["cluster_open_lots"], 0)
        texts = [c.args[0] for c in page.call_args_list]
        self.assertEqual(sum("stale record" in t for t in texts), 1)
        self.assertFalse(any("no readable stop" in t for t in texts))

    # revision 6 item c: the stale page names the lot's exposure and what the broker holds
    def test_stale_page_names_signs(self):
        coid = "DT-NVDA-b-1-vd" + "d8" * 16
        open_log = {coid: {"trade_id": coid, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 5}}
        ev = [{"event": "stop_placed", "trade_id": coid, "stop_price": 193.0}]
        book = [SimpleNamespace(symbol="NVDA", qty="-5", side="short", market_value="-1000", current_price="200")]
        with mock.patch("alerts.send_slack") as page:
            self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=ev, positions=book)
        msg = next(c.args[0] for c in page.call_args_list if "stale record" in c.args[0])
        self.assertIn("no position with long exposure in NVDA", msg)
        self.assertIn("short exposure, 5 sh", msg)

    # revision 6 item b (the simpler rule): a counted lot older than the retention window pages once per lot per day
    def test_counted_lot_older_than_retention_pages_once_per_day(self):
        from datetime import datetime, timedelta
        coid = "DT-NVDA-b-1-vd" + "d9" * 16
        old = (datetime.now(dvs.PT) - timedelta(days=dtm._STATE_TTL_DAYS + 2)).isoformat()
        open_log = {coid: {"trade_id": coid, "symbol": "NVDA", "side": "long", "entry_price": 200.0, "fill_qty": 5,
                           "entry_ts": old}}
        ev = [{"event": "stop_placed", "trade_id": coid, "stop_price": 193.0}]
        with mock.patch("alerts.send_slack") as page:
            for _ in range(2):
                dvs._PAGED.clear()
                _ok, sub, logged, *_ = self._run(trigger_extra={"veto": {"m_kelly": 1.0}}, open_log=open_log, events=ev)
        self.assertEqual(sub["qty"], 2)                               # still counted (the broker holds it)
        self.assertEqual(sum("retention window" in c.args[0] for c in page.call_args_list), 1)


class BandRecordExclusion(unittest.TestCase):
    def test_veto_trades_never_enter_the_conv_band_record(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = datetime(2026, 11, 10, 12, tzinfo=ZoneInfo("America/New_York"))
        old = now - timedelta(days=10)
        base = [{"trade_id": f"DT-A-b-{i}-abcdef01", "conviction": c, "R": r, "exit_ts": old}
                for i, (c, r) in enumerate([(0.1, 1), (0.2, -1), (0.5, 1), (0.6, 1), (0.9, -1), (0.95, -1)])]
        veto = [{"trade_id": f"DT-A-b-9{i}-vd" + "d" * 32, "conviction": 0.55, "R": -5.0, "exit_ts": old}
                for i in range(5)]
        with mock.patch.object(es, "completed_trades", return_value=base):
            p0 = es.plan(0.55, [], now=now)
        with mock.patch.object(es, "completed_trades", return_value=base + veto):
            p1 = es.plan(0.55, [], now=now)
        self.assertEqual(p0, p1)


# ── pause (item 6) ───────────────────────────────────────────────────────────────────────────────────────────────
class Pause(Base):
    def test_pause_per_direction_and_reset_next_day(self):
        pr = dvs.load_prior(_prior(self.tmp))
        losers = [{"trade_id": f"L{i}", "R": -1.0, "session": f"2026-11-0{2 + i % 3}", "sign": 1} for i in range(30)]
        shorts = [{"trade_id": f"S{i}", "R": 1.0, "session": f"2026-11-0{2 + i % 3}", "sign": -1} for i in range(6)]
        trades = losers + shorts
        with mock.patch("alerts.send_slack") as page:
            out = dvs.evaluate_pause(pr, trades, [t["trade_id"] for t in trades], day="20261110")
            self.assertIn(1, out)
            self.assertNotIn(-1, out)
            page.assert_called_once()
            self.assertIn("n=30", page.call_args.args[0])
            # same day, no new closed trade -> no second page
            dvs.evaluate_pause(pr, trades, [t["trade_id"] for t in trades], day="20261110")
            page.assert_called_once()
        self.assertIsNotNone(dvs.pause_info(1, "20261110"))
        self.assertIsNone(dvs.pause_info(-1, "20261110"))
        self.assertIsNone(dvs.pause_info(1, "20261111"))              # resets next trading day

    def test_one_session_never_pauses(self):
        pr = dvs.load_prior(_prior(self.tmp))
        trades = [{"trade_id": f"L{i}", "R": -1.0, "session": "2026-11-02", "sign": 1} for i in range(30)]
        with mock.patch("alerts.send_slack") as page:
            self.assertEqual(dvs.evaluate_pause(pr, trades, [t["trade_id"] for t in trades], day="20261110"), {})
        page.assert_not_called()

    def test_ledger_rebuilds_r_from_alpaca_orders(self):
        tid = "DT-AAPL-b-1-vd" + "e" * 32
        events = [
            {"event": "entry_fill", "trade_id": tid, "symbol": "AAPL", "side": "long", "fill_price": 100.0,
             "fill_qty": 2, "ts": "2026-11-09T07:00:00-08:00"},
            {"event": "stop_placed", "trade_id": tid, "stop_price": 98.0},
            {"event": "exit_fill", "trade_id": tid, "order_id": "X1", "fill_price": 103.0, "fill_qty": 2},
        ]
        fake = SimpleNamespace(
            get_order_by_client_order_id=lambda c: SimpleNamespace(filled_avg_price="100.0", side="buy",
                                                                   filled_at="2026-11-09T15:00:00+00:00", symbol="AAPL"),
            get_order=lambda oid: SimpleNamespace(filled_avg_price="104.0"))
        rows, new, info = dvs.live_trades(events, fake)
        self.assertEqual(new, [tid])
        self.assertAlmostEqual(rows[0]["R"], 2.0)                     # Alpaca exit 104, not the journal's 103
        self.assertEqual(rows[0]["sign"], 1)
        self.assertEqual(rows[0]["source"], "alpaca")
        self.assertEqual(info["api_calls"], 2)                       # nit c: counted for the tick budget

    # cold review threat 1: one unrebuildable lot never disables the ledger
    def _lot(self, tid, stop=True, entry=100.0, exit_px=97.0, day="09"):
        ev = [{"event": "entry_fill", "trade_id": tid, "symbol": "AAPL", "side": "long", "fill_price": entry,
               "fill_qty": 2, "ts": f"2026-11-{day}T07:00:00-08:00"},
              {"event": "exit_fill", "trade_id": tid, "order_id": "", "fill_price": exit_px, "fill_qty": 2}]
        if stop:
            ev.insert(1, {"event": "stop_placed", "trade_id": tid, "stop_price": 98.0})
        return ev

    def test_unrebuildable_lot_falls_back_or_is_excluded_and_others_still_count(self):
        good, nostop, staterec = ("DT-AAPL-b-1-vd" + c * 32 for c in "567")
        events = self._lot(good) + self._lot(nostop, stop=False) + self._lot(staterec, stop=False, day="06")
        calls: list = []

        def _gbc(c):
            calls.append(c)
            return None                                               # Alpaca has nothing (e.g. order read down)
        unread = SimpleNamespace(get_order_by_client_order_id=_gbc, get_order=lambda o: None)
        with mock.patch("alerts.send_slack") as page:
            rows, new, info = dvs.live_trades(events, unread, st_stops={staterec: 98.5}, day="20261110")
            ids = {r["trade_id"]: r for r in rows}
            self.assertIn(good, ids)                                  # journal evidence when Alpaca is missing
            self.assertEqual(ids[good]["source"], "journal_fallback")
            self.assertAlmostEqual(ids[good]["R"], -1.5)
            self.assertIn(staterec, ids)                              # stop from the state record
            self.assertNotIn(nostop, ids)                             # no stop anywhere -> excluded, counted, paged
            self.assertIn(nostop, info["excluded"])
            self.assertEqual(page.call_count, 1)
            n_calls = len(calls)
            # same day: no re-page, no Alpaca retry of a failed lookup (nit c)
            dvs.live_trades(events, unread, st_stops={staterec: 98.5}, day="20261110")
            self.assertEqual(page.call_count, 1)
            self.assertEqual(len(calls), n_calls)
            # next day: the failed lookups are retried once
            dvs.live_trades(events, unread, st_stops={staterec: 98.5}, day="20261111")
            self.assertGreater(len(calls), n_calls)

    def test_torn_journal_line_does_not_disable_the_ledger_or_pause(self):
        tid = "DT-AAPL-b-1-vd" + "8" * 32
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=(self._lot(tid), False)), \
                mock.patch("execution.day_trade_manager._load_state", return_value={}), \
                mock.patch("alerts.send_slack") as page:
            ctx = dvs.refresh(broker=SimpleNamespace(get_order_by_client_order_id=lambda c: None,
                                                     get_order=lambda o: None))
        self.assertIsNotNone(ctx["trades"])
        self.assertEqual(len(ctx["trades"]), 1)
        self.assertTrue(any("torn" in c.args[0] for c in page.call_args_list))

    # revision 3 item 3: no page storm when the primary dedupe file cannot be written (new process every tick)
    def test_page_dedupe_falls_back_and_never_storms(self):
        real = dvs._atomic_write
        no_primary = mock.patch.object(dvs, "_atomic_write",
                                       side_effect=lambda p, o: False if p == dvs.PAGES_PATH else real(p, o))
        with no_primary, mock.patch("alerts.send_slack") as page:
            self.assertTrue(dvs.page_once("k1", "first"))
            dvs._PAGED.clear()                                        # the next tick is a new process
            self.assertFalse(dvs.page_once("k1", "first"))
            self.assertEqual(page.call_count, 1)
            self.assertIn("k1", self.secondary)
            # both bot stores unwritable: stamped in the temp-dir marker, never re-sent that day
            self.secondary_ok = False
            self.assertTrue(dvs.page_once("k2", "second"))
            self.assertIn("temp-dir marker", page.call_args.args[0])
            dvs._PAGED.clear()
            self.assertFalse(dvs.page_once("k2", "second"))
            self.assertEqual(page.call_count, 2)
            # no store writable at all: logged only, never paged (so it can never repeat every tick)
            with mock.patch.object(dvs, "_tertiary_mark", return_value=False):
                self.assertFalse(dvs.page_once("k3", "third"))
            self.assertEqual(page.call_count, 2)

    # revision 5 item 3 (nit 3): inside place_entry's call path the Day tier state file is never written
    def test_no_secondary_write_inside_place_entry_path(self):
        real = dvs._atomic_write
        with mock.patch.object(dvs, "_atomic_write",
                               side_effect=lambda p, o: False if p == dvs.PAGES_PATH else real(p, o)), \
                mock.patch("alerts.send_slack") as page, dvs.no_secondary_store():
            self.assertTrue(dvs.page_once("k9", "inside place_entry"))
        self.assertNotIn("k9", self.secondary)
        self.assertTrue(dvs._tertiary_has("k9"))
        self.assertIn("temp-dir marker", page.call_args.args[0])
        self.assertEqual(dvs._NO_SECONDARY, [])                     # the context unwinds

    # revision 5 item 2 (nit 2): an unresolved prior-day record pages once per lot PER DAY
    def test_unresolved_record_pages_once_per_day(self):
        coid = "DT-NVDA-b-1-vd" + "c6" * 16
        st = {"entry::NVDA::20261103-1000": {"coid": coid, "symbol": "NVDA", "side": "long", "state": "submitting",
                                             "bar_id": "20261103-1000", "qty": 5}}
        with mock.patch("alerts.send_slack") as page:
            for d in ("20261110", "20261110", "20261111"):
                dvs._PAGED.clear()
                dvs.state_open_veto_lots(st, [], resolved={}, day=d)
        self.assertEqual(page.call_count, 2)

    # revision 5 item 4 (nit 6): a naive journal timestamp is ET, not the host's local zone
    def test_naive_timestamp_read_as_et(self):
        tid = "DT-AAPL-b-1-vd" + "c7" * 16
        g = {"entry": {"symbol": "AAPL", "side": "long", "fill_price": 100.0, "fill_qty": 2,
                       "ts": "2026-11-09T23:30:00"},
             "stop": {"stop_price": 98.0},
             "exits": [{"order_id": "", "fill_price": 101.0, "fill_qty": 2}]}
        row, _why, _f = dvs._rebuild_r(tid, g, None, {}, False, {"api": 0})
        self.assertEqual(row["session"], "2026-11-09")

    # revision 3 item 4: ledger broker reads are capped per tick; the rest are deferred, not failed
    def test_ledger_reads_capped_and_deferred(self):
        tids = ["DT-AAPL-b-%d-vd" % i + "c" * 32 for i in range(3)]
        events = []
        for t in tids:
            events += self._lot(t)
            events[-1]["order_id"] = "X-" + t                         # each lot: 1 entry read + 1 exit read
        reads: list = []

        def _gbc(c):
            reads.append(c)
            return SimpleNamespace(filled_avg_price="100.0", side="buy", filled_at="2026-11-09T15:00:00+00:00")

        def _go(o):
            reads.append(o)
            return SimpleNamespace(filled_avg_price="97.0")
        b = SimpleNamespace(get_order_by_client_order_id=_gbc, get_order=_go)
        rows, new, info = dvs.live_trades(events, b, day="20261110", max_reads=2)
        self.assertEqual(info["api_calls"], 2)
        self.assertEqual(len(reads), 2)
        self.assertEqual(info["deferred"], 2)
        self.assertEqual(len(rows), 3)                                # deferred lots carry their journal value
        self.assertEqual(sorted(r["source"] for r in rows), ["alpaca", "journal_fallback", "journal_fallback"])
        rows2, _n, info2 = dvs.live_trades(events, b, day="20261110", max_reads=10)
        self.assertEqual(info2["deferred"], 0)
        self.assertTrue(all(r["source"] == "alpaca" for r in rows2))  # read on a later tick

    # revision 3 nit: an unreadable pause file fails closed and pages once a day
    def test_unreadable_pause_file_fails_closed(self):
        dvs.PAUSE_PATH.write_text("{not json")
        with mock.patch("alerts.send_slack") as page:
            self.assertTrue(dvs.pause_info(1, "20261110")["unreadable"])
            self.assertTrue(dvs.pause_info(-1, "20261110")["unreadable"])
        self.assertEqual(page.call_count, 1)

    # nit d: a failed pause-file write pages once
    def test_pause_write_failure_pages_once(self):
        pr = dvs.load_prior(_prior(self.tmp))
        trades = [{"trade_id": f"L{i}", "R": -1.0, "session": f"2026-11-0{2 + i % 3}", "sign": 1} for i in range(30)]
        real = dvs._atomic_write
        with mock.patch.object(dvs, "_atomic_write",
                               side_effect=lambda p, o: False if p == dvs.PAUSE_PATH else real(p, o)), \
                mock.patch("alerts.send_slack") as page:
            self.assertEqual(dvs.evaluate_pause(pr, trades, [t["trade_id"] for t in trades], day="20261110"), {})
            dvs.evaluate_pause(pr, trades, [t["trade_id"] for t in trades], day="20261110")
        self.assertEqual(page.call_count, 1)
        self.assertIn("NOT in force", page.call_args.args[0])
        self.assertIsNone(dvs.pause_info(1, "20261110"))


# ── runner (items 1, 2, 6, flag) ─────────────────────────────────────────────────────────────────────────────────
class RunnerVeto(Base):
    def _tick(self, side="TWO_SIDED", extra=None, mom_over=None, pause=None):
        from datetime import datetime
        acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power="4000")
        mom = {"symbol": "UBER", "trigger": "ENTER", "direction": "long", "mode": "DRIVE",
               "entry_ref": 70.0, "structural_level": 69.0, "vol_confirmed": True, **(mom_over or {})}
        refresh = mock.Mock(return_value={"prior": dvs.load_prior(REPO_PRIOR), "trades": [], "new": [], "paused": {}})
        patches = [
            mock.patch.object(run_day_tier, "_clock_state", return_value=("open", 300.0)),
            mock.patch.object(run_day_tier, "_touch_heartbeat"),
            mock.patch.object(run_day_tier, "_maybe_sample_prices"),
            mock.patch.object(run_day_tier, "_track_b_daily_context", return_value=(100.0, 1e6)),
            mock.patch("execution.broker.get_account", return_value=acct),
            mock.patch("execution.day_trade_manager.reconcile_open_state", return_value={"checked": 0}),
            mock.patch("execution.day_trade_manager.tier_kill_check", return_value=False),
            mock.patch("execution.day_trade_manager._account_entry_halt_reason", return_value=None),
            mock.patch("strategy.day_tier_track_b.track_b_in_window", return_value=True),
            mock.patch("strategy.day_tier_track_b.track_b_universe", return_value=["UBER"]),
            mock.patch("strategy.day_tier_track_b.screen_mover", return_value={"is_mover": True, "gap_direction": "up"}),
            mock.patch("strategy.day_tier_momentum_trigger.compute_momentum_trigger", return_value=mom),
            mock.patch.object(config, "DAYTRADE_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_TRACK_B_ENABLED", True),
            mock.patch.object(config, "DAYTRADE_UNIVERSE", []),
            mock.patch.object(config, "DAYTRADE_TRACK_M_ENABLED", False),
            mock.patch("execution.day_trade_manager._load_state", return_value={}),
            mock.patch("execution.day_trade_manager._save_state", return_value=True),
            mock.patch.object(run_day_tier, "_prev_session_date", return_value="2026-11-09"),
            mock.patch.object(run_day_tier, "_side_for", return_value=side),
            mock.patch.object(run_day_tier, "_day_tier_exposure", return_value={}),
            mock.patch.object(run_day_tier, "_watch_day_ok", return_value=(True, "watch day (test)")),
            mock.patch.object(run_day_tier, "_alignment_for", return_value={"aligned": True, "checks": {}, "reason": "ok"}),
            mock.patch.object(run_day_tier, "_record_counter_trend_intent", return_value="DID-1"),
            mock.patch("strategy.day_tier_leverage.etf_for_order", return_value=None),
            mock.patch("strategy.day_tier_leverage.inverse_etf_for_order", return_value=None),
            mock.patch.object(run_day_tier, "_held_by_other_tiers", return_value=set()),
            mock.patch.object(dvs, "refresh", refresh),
            mock.patch.object(dvs, "pause_info", return_value=pause),
            mock.patch.object(config, "DAYTRADE_ALIGN_GATE_BLOCKS", False, create=True),
            mock.patch.object(config, "DAYTRADE_DIRECTION_RULES_BLOCK", True, create=True),
        ] + list(extra or [])
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            rm = stack.enter_context(mock.patch("execution.risk_manager.RiskManager"))
            stack.enter_context(mock.patch("strategy.day_tier_track_b.build_session_frame",
                                           return_value=mock.MagicMock(name="frame")))
            pe = stack.enter_context(mock.patch("execution.day_trade_manager.place_entry", return_value=True))
            skip = stack.enter_context(mock.patch.object(run_day_tier, "_log_direction_skip"))
            rm.return_value.check_kill_switch.return_value = False
            _ = datetime
            run_day_tier.run_tick()
        return pe, skip, refresh

    def test_two_sided_signal_reaches_place_entry_tagged(self):
        pe, _skip, _r = self._tick()
        pe.assert_called_once()
        trig = pe.call_args.args[2]
        self.assertTrue(trig["veto_downsized"])
        self.assertIn("no clear trend direction", trig["veto_reason"])
        v = trig["veto"]
        for k in ("rule", "side", "side_score", "p", "b", "n_prior", "n_live", "f_lcb", "m_kelly"):
            self.assertIn(k, v)
        self.assertEqual(v["n_prior"], 682)
        self.assertNotEqual(pe.call_args.args[3].get("max_size"), True)

    def test_unknown_side_blocked(self):
        pe, skip, _r = self._tick(side="UNKNOWN")
        pe.assert_not_called()
        self.assertIn("no clear trend direction", skip.call_args.args[3])

    def test_flag_false_blocks_two_sided(self):
        pe, skip, _r = self._tick(extra=[mock.patch.object(config, "DAYTRADE_VETO_DOWNSIZE_ENABLED", False)])
        pe.assert_not_called()

    def test_one_share_risk_over_cap_skipped_before_m_math(self):
        # entry 70, level 10 -> one share risks ~$60 x 1.2 > 2% of $2,500
        pe, skip, refresh = self._tick(mom_over={"structural_level": 10.0})
        pe.assert_not_called()
        self.assertIn("one share risks more than the per-trade cap", skip.call_args.args[3])

    def test_paused_direction_skips(self):
        pe, skip, _r = self._tick(pause={"n": 9, "mean": -0.8, "upper": -0.1})
        pe.assert_not_called()
        self.assertIn("veto path paused", skip.call_args.args[3])

    def test_rule2_opposition_blocks_in_the_runner(self):
        opp = {"aligned": False, "checks": {"ema2": False, "vwap2": False, "ema5": False, "vwap5": False},
               "reason": "opp"}
        pe, skip, _r = self._tick(extra=[mock.patch.object(run_day_tier, "_alignment_for", return_value=opp)])
        pe.assert_not_called()
        self.assertIn("every 2m/5m check opposes", skip.call_args.args[3])

    # revision 4 nit b: the ledger read cap keeps one per-entry estimate for each track that can still enter
    def test_ledger_read_cap_reserves_each_track(self):
        _pe, _skip, refresh = self._tick(extra=[mock.patch.object(config, "DAYTRADE_TRACK_M_ENABLED", True),
                                                mock.patch.object(run_day_tier, "_run_track_m",
                                                                  return_value=(0, "outside_window"))])
        budget = int(getattr(config, "DAYTRADE_MAX_API_CALLS_PER_RUN", 60))
        per_entry = int(getattr(config, "DAYTRADE_CALLS_PER_ENTRY_EST", 15))
        spent = 4                                                     # the runner's own start estimate (no reconcile)
        kw = next(c.kwargs for c in refresh.call_args_list if "max_reads" in c.kwargs)
        self.assertEqual(kw["max_reads"], max(0, budget - spent - per_entry * 2))   # B and M on; A universe empty


class Rev4(Base):
    """Revision 4: threat 1 (stale no-fill state record) and nits a, c, d."""

    def _rec(self, coid, day, state="submitting", fill_px=None):
        return {f"entry::NVDA::{day}-1000": {"coid": coid, "symbol": "NVDA", "side": "long", "state": state,
                                             "bar_id": f"{day}-1000", "qty": 5, "stop_px": 193.0,
                                             **({"fill_px": fill_px, "fill_qty": 5} if fill_px else {})}}

    def test_today_no_fill_record_charged_and_paged_once(self):
        today = dvs._today()
        coid = "DT-NVDA-b-1-vd" + "d1" * 16
        lots = dvs.state_open_veto_lots(self._rec(coid, today), [], resolved={})
        self.assertEqual(len(lots), 1)
        with mock.patch("alerts.send_slack") as page:
            for _ in range(2):
                dvs._PAGED.clear()
                r = dvs.cluster_risk({}, {}, 1, state_only=lots, unknown_lot_usd=40.0,
                                     broker_positions={"NVDA": SimpleNamespace(qty="5", side="long")})
                self.assertEqual(r["risk"], 40.0)
        self.assertEqual(page.call_count, 1)

    def test_prior_day_no_fill_record_ignored_unless_broker_confirms(self):
        coid = "DT-NVDA-b-1-vd" + "d2" * 16
        st = self._rec(coid, "20261103")
        with mock.patch("alerts.send_slack") as page:
            self.assertEqual(dvs.state_open_veto_lots(st, [], resolved={}), [])          # unresolved: ignored
            dvs._PAGED.clear()
            dvs.state_open_veto_lots(st, [], resolved={})
        self.assertEqual(page.call_count, 1)                                            # paged once per lot
        filled = dvs.state_open_veto_lots(st, [], resolved={coid: {"status": "filled", "fill_px": 200.0,
                                                                   "fill_qty": 5}})
        self.assertEqual(filled[0]["entry_price"], 200.0)
        with mock.patch("alerts.send_slack") as page2:
            self.assertEqual(dvs.state_open_veto_lots(st, [], resolved={coid: {"status": "none"}}), [])
        page2.assert_not_called()

    def test_prior_day_records_resolved_against_broker_under_budget(self):
        a, b, c = ("DT-NVDA-b-%d-vd" % i + "e" * 32 for i in range(3))
        st = {**self._rec(a, "20261103"),
              "entry::MU::20261103-1000": {"coid": b, "symbol": "MU", "side": "long", "state": "submitted",
                                           "bar_id": "20261103-1000", "qty": 5, "stop_px": 90.0},
              "entry::AMD::20261103-1000": {"coid": c, "symbol": "AMD", "side": "long", "state": "submitting",
                                            "bar_id": "20261103-1000", "qty": 5, "stop_px": 90.0}}
        orders = {a: SimpleNamespace(filled_qty="5", filled_avg_price="200.0", status="filled"),
                  b: SimpleNamespace(filled_qty="0", filled_avg_price=None, status="canceled"), c: None}
        reads: list = []

        def _gbc(coid):
            reads.append(coid)
            return orders[coid]
        brk = SimpleNamespace(get_order_by_client_order_id=_gbc)
        self.assertEqual(dvs._resolve_state_records(st, [], brk, 0), 0)                  # no budget: no read
        self.assertEqual(dvs._resolve_state_records(st, [], brk, 10), 3)
        res = dvs._read_json(dvs.LEDGER_PATH)["state_resolved"]
        self.assertEqual(res[a]["status"], "filled")
        self.assertEqual(res[b]["status"], "none")
        self.assertEqual(res[c]["status"], "unknown")
        self.assertEqual(dvs._resolve_state_records(st, [], brk, 10), 0)                 # no re-read today
        with mock.patch("alerts.send_slack"):
            lots = dvs.state_open_veto_lots(st, [])
        self.assertEqual([x["coid"] for x in lots], [a])

    # nit a: a deferred lot with no journal R is excluded after deferral on two trading days
    def test_deferred_lot_without_journal_r_excluded_on_second_day(self):
        tid = "DT-AAPL-b-1-vd" + "f1" * 16
        ev = [{"event": "entry_fill", "trade_id": tid, "symbol": "AAPL", "side": "long", "fill_price": 100.0,
               "fill_qty": 2, "ts": "2026-11-09T07:00:00-08:00"},
              {"event": "exit_fill", "trade_id": tid, "order_id": "X9", "fill_price": 97.0, "fill_qty": 2}]   # no stop
        brk = SimpleNamespace(get_order_by_client_order_id=lambda c: None, get_order=lambda o: None)
        with mock.patch("alerts.send_slack") as page:
            _r, _n, i1 = dvs.live_trades(ev, brk, day="20261110", max_reads=0)
            self.assertNotIn(tid, i1["excluded"])
            _r, _n, i2 = dvs.live_trades(ev, brk, day="20261110", max_reads=0)          # same day: still deferred
            self.assertNotIn(tid, i2["excluded"])
            _r, _n, i3 = dvs.live_trades(ev, brk, day="20261111", max_reads=0)
        self.assertIn(tid, i3["excluded"])
        self.assertEqual(page.call_count, 1)

    # nit a: a journal-fallback R corrected from Alpaca is re-evaluated by the pause
    def test_source_change_is_reevaluated(self):
        pr = dvs.load_prior(_prior(self.tmp))
        day = dvs._today()
        jt = [{"trade_id": f"L{i}", "R": 0.5, "session": f"2026-11-0{2 + i % 3}", "sign": 1,
               "source": "journal_fallback", "added": day} for i in range(30)]
        with mock.patch("alerts.send_slack"):
            self.assertEqual(dvs.evaluate_pause(pr, jt, [t["trade_id"] for t in jt], day=day), {})
            at = [{**t, "R": -1.0, "source": "alpaca"} for t in jt]
            self.assertIn(1, dvs.evaluate_pause(pr, at, [t["trade_id"] for t in at], day=day))

    # nit c: dedupe stamps and the evaluated map are pruned to the Day tier's state-retention window
    def test_pruning_to_the_retention_window(self):
        from datetime import datetime, timedelta
        old = (datetime.now(dvs.PT) - timedelta(days=dtm._STATE_TTL_DAYS + 1)).isoformat()
        dvs.PAGES_PATH.write_text(json.dumps({"sent": {"old:key": old, "fresh:key": datetime.now(dvs.PT).isoformat()}}))
        with mock.patch("alerts.send_slack"):
            dvs.page_once("new:key", "m")
        sent = dvs._read_json(dvs.PAGES_PATH)["sent"]
        self.assertNotIn("old:key", sent)
        self.assertIn("fresh:key", sent)
        pr = dvs.load_prior(_prior(self.tmp))
        old_day = (datetime.now(dvs.ET) - timedelta(days=dtm._STATE_TTL_DAYS + 1)).strftime("%Y%m%d")
        trades = [{"trade_id": "OLD", "R": 1.0, "session": "2026-10-01", "sign": 1, "added": old_day},
                  {"trade_id": "NEW", "R": 1.0, "session": "2026-11-09", "sign": 1, "added": dvs._today()}]
        dvs.evaluate_pause(pr, trades, ["OLD", "NEW"])
        ev = dvs._read_json(dvs.PAUSE_PATH)["evaluated"]
        self.assertEqual(sorted(ev), ["NEW"])                              # OLD is never new again: pruned, not re-run

    def test_secondary_store_prunes_old_stamps(self):
        from datetime import datetime, timedelta
        old = (datetime.now(dvs.PT) - timedelta(days=dtm._STATE_TTL_DAYS + 1)).isoformat()
        store = {dvs._SECONDARY_PREFIX + "old": old, "entry::X": {"state": "protected"}}
        saved: dict = {}
        with mock.patch.object(dtm, "_load_state", return_value=dict(store)), \
                mock.patch.object(dtm, "_save_state", side_effect=lambda s: saved.update(s) or True):
            self.assertTrue(self._real_secondary_mark("new"))
        self.assertNotIn(dvs._SECONDARY_PREFIX + "old", saved)
        self.assertIn(dvs._SECONDARY_PREFIX + "new", saved)
        self.assertIn("entry::X", saved)

    def _real_secondary_mark(self, key):
        for p in self._p:
            if getattr(p, "attribute", "") == "_secondary_mark":
                p.stop()
                try:
                    return dvs._secondary_mark(key)
                finally:
                    p.start()
        return False

    # nit d: CONV-1018's J excludes veto lots
    def test_jump_ratio_excludes_veto_lots(self):
        veto = {"trade_id": "DT-A-b-1-vd" + "a" * 32}
        normal = {"trade_id": "DT-A-b-2-abcdef01"}
        with mock.patch("strategy.day_tier_logger.read_events_checked", return_value=([], True)), \
                mock.patch.object(es, "plan", return_value={"proven": True}), \
                mock.patch.object(es, "completed_trades", return_value=[veto, normal]), \
                mock.patch.object(es, "jump_ratio", return_value=None) as jr:
            dtm._conv_inputs("AAPL", {"conviction": 0.5}, {}, (0.0, 0.0, ""))
        self.assertEqual(jr.call_args.args[0], [normal])


if __name__ == "__main__":
    unittest.main()
