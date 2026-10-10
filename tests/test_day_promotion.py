#!/usr/bin/env python3
# ruff: noqa: E501
"""Day-tier -> Swing-tier promotion (CEO 2026-10-09; execution/day_promotion.py): decide at 3:56, hand off after the
close, adopt into the Swing tracker."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from execution import day_promotion as dp
from execution import day_trade_manager as dtm
from execution import tier_transfers as _tt

EQUITY = 2600.0


def _pos(sym="AAPL", qty=4, side="long", px=341.0):
    return SimpleNamespace(symbol=sym, qty=str(qty), side=side, current_price=str(px), market_value=str(qty * px))


def _state(sym="AAPL", qty=4, entry=335.24, stop=333.0, st="protected", coid="DT-AAPL-b-1-x"):
    return {f"entry::{sym}::20261009-1500": {"bar_id": "20261009-1500", "coid": coid, "state": st, "symbol": sym,
                                             "side": "long", "qty": qty, "fill_qty": qty, "fill_px": entry,
                                             "stop_px": stop, "stop_order_id": "S1", "tp_order_id": "T1",
                                             "oco_order_id": "T1"}}


class _Env:
    """Temp state + hand-off files and patched collaborators."""

    def __init__(self, state, positions, *, px=341.0, side="LONG", atr=4.0, swing_risk=0.0):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state_path, self.hand_path = d / "state.json", d / "hand.json"
        self.state_path.write_text(json.dumps(state))
        self.patches = [
            mock.patch.object(dtm, "_STATE", self.state_path),
            mock.patch.object(dp, "_HANDOFF", self.hand_path),
            mock.patch.object(_tt, "_PATH", d / "tier_transfers.json"),     # never the real journal
            mock.patch.object(dtm, "_enabled", return_value=True),
            mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}),
            mock.patch("execution.broker.get_open_positions", return_value=positions),
            mock.patch("data.live_price.live_price", return_value=SimpleNamespace(price=px)),
            mock.patch("strategy.day_tier_side.compute_side_bias", return_value={"side": side}),
            mock.patch.object(dp, "_daily_atr", return_value=atr),
            mock.patch.object(dp, "_swing_open_risk", return_value=swing_risk),
            mock.patch("execution.swing_breakout_manager._protected_notional", return_value=0.0),
            mock.patch("trade_logger.log_event"),
            mock.patch.object(dp, "_slack"),
        ]

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def state(self):
        return json.loads(self.state_path.read_text())

    def hand(self):
        return json.loads(self.hand_path.read_text()) if self.hand_path.exists() else {}


def _rec(env):
    return next(v for k, v in env.state().items() if k.startswith("entry::"))


class Select(unittest.TestCase):
    def test_qualifying_long_is_marked_pending(self):
        with _Env(_state(), [_pos()]) as env:
            self.assertEqual(dp.select_promotions(EQUITY), ["AAPL"])
            r = _rec(env)
            self.assertEqual(r["state"], "promote_pending")
            self.assertAlmostEqual(r["promote"]["swing_stop"], round(335.24 - dp.config.INTRADAY_STOP_ATR_MULT * 4.0, 2))
            self.assertEqual(dp.select_promotions(EQUITY), [])          # once per day

    def test_pending_lot_is_not_a_flatten_target(self):
        with _Env(_state(), [_pos()]):
            dp.select_promotions(EQUITY)
            self.assertNotIn("AAPL", dtm._flatten_targets())

    def _rejected(self, **kw):
        state = _state(**{k: v for k, v in kw.items() if k in ("qty", "entry", "stop", "st")})
        env_kw = {k: v for k, v in kw.items() if k in ("px", "side", "atr", "swing_risk")}
        positions = kw.get("positions", [_pos(qty=state[next(iter(state))]["qty"])])
        with _Env(state, positions, **env_kw) as env:
            self.assertEqual(dp.select_promotions(EQUITY), [])
            self.assertNotEqual(_rec(env)["state"], "promote_pending")

    def test_not_enough_profit(self):
        self._rejected(px=336.0)                                       # +0.76 < 0.5R (R = 2.24 -> 1.12)

    def test_trend_not_long(self):
        self._rejected(side="TWO_SIDED")

    def test_risk_over_two_pct(self):
        self._rejected(atr=40.0)                                       # 4 x (341 - (335.24 - mult x 40)) > 2% x $2,600

    def test_aggregate_overnight_risk(self):
        self._rejected(swing_risk=150.0)                               # + $23 > 6% ($156)

    def test_co_held_or_partial_position(self):
        self._rejected(positions=[_pos(qty=6)])

    def test_co_held_day_lot_keeps_other_tier_shares_overnight(self):
        # MSFT: Day lot 1 sh, broker 11 sh (10 Swing). The 10 Swing shares ($5,000) stay overnight and push the
        # AAPL candidate past the 100%-of-equity limb.
        st = _state()
        st["entry::MSFT::20261009-1400"] = {"bar_id": "20261009-1400", "coid": "DT-MSFT-b-1-y", "state": "protected",
                                            "symbol": "MSFT", "side": "long", "qty": 1, "fill_qty": 1, "fill_px": 500.0,
                                            "stop_px": 495.0, "stop_order_id": "S2"}
        msft = SimpleNamespace(symbol="MSFT", qty="11", side="long", current_price="500", market_value="5500")
        with _Env(st, [_pos(), msft]) as env:
            self.assertEqual(dp.select_promotions(EQUITY), [])
            self.assertEqual(env.state()["entry::AAPL::20261009-1500"]["state"], "protected")

    def test_unsettled_state(self):
        self._rejected(st="submitted")

    def test_etf_and_short_never_promote(self):
        st = _state(sym="NVDL", coid="DT-NVDL-b-1-x")
        with _Env(st, [_pos(sym="NVDL")]) as env:
            self.assertEqual(dp.select_promotions(EQUITY), [])
            self.assertEqual(_rec(env)["state"], "protected")
        st = _state()
        st[next(iter(st))]["side"] = "short"
        with _Env(st, [_pos(side="short")]) as env:
            self.assertEqual(dp.select_promotions(EQUITY), [])

    def test_unreadable_state_is_never_wiped(self):
        with _Env(_state(), [_pos()]) as env:
            env.state_path.write_text("{broken")
            self.assertEqual(dp.select_promotions(EQUITY), [])
            self.assertEqual(env.state_path.read_text(), "{broken")

    def test_kill_flag(self):
        with _Env(_state(), [_pos()]) as env, mock.patch.object(dp.config, "DAYTRADE_PROMOTION_ENABLED", False, create=True):
            self.assertEqual(dp.select_promotions(EQUITY), [])
            self.assertEqual(_rec(env)["state"], "protected")


class Handoff(unittest.TestCase):
    def _pending(self):
        st = _state(st="promote_pending")
        rec = st[next(iter(st))]
        rec["promote"] = {"qty": 4, "day_entry": 335.24, "day_stop": 333.0, "atr": 4.0, "swing_stop": 330.24,
                          "target": 345.24, "decision_price": 341.0}
        return st

    def _run(self, env, *, legs=True, stop_fill=False, pos=None, log_ok=True, closed=False, past_close=5.0):
        with mock.patch.object(dtm, "_closed_in_log", return_value=closed), \
                mock.patch.object(dp, "_past_close_min", return_value=past_close), \
                mock.patch.object(dtm, "_cancel_recorded_exit_legs_confirmed", return_value=legs), \
                mock.patch.object(dtm, "_record_confirmed_stop_exit", return_value=stop_fill), \
                mock.patch("execution.broker.get_open_position", return_value=pos), \
                mock.patch("strategy.day_tier_logger.log_exit_fill", return_value=log_ok) as lx:
            return dp.complete_handoffs(), lx

    def test_books_and_hands_over(self):
        with _Env(self._pending(), []) as env:
            summ, lx = self._run(env, pos=_pos(px=340.42))
            self.assertEqual(summ["promoted"], 1)
            self.assertEqual(lx.call_args.kwargs["exit_reason"], "promoted_to_swing")
            self.assertAlmostEqual(lx.call_args.kwargs["realized_pnl"], round((340.42 - 335.24) * 4, 2))
            h = env.hand()["DT-AAPL-b-1-x"]
            self.assertEqual((h["status"], h["take_over_mark"]), ("ready", 340.42))
            self.assertEqual(_rec(env)["state"], "promoted")
            self.assertEqual(_tt.load_transfers(), [])      # ledger transfer waits for the Swing adoption

    def test_waits_while_a_day_leg_may_be_live(self):
        with _Env(self._pending(), []) as env:
            summ, lx = self._run(env, legs=None, pos=_pos())
            self.assertEqual(summ["waiting"], 1)
            lx.assert_not_called()
            self.assertEqual(_rec(env)["state"], "promote_pending")

    def test_stop_filled_before_four_is_not_promoted(self):
        with _Env(self._pending(), []) as env:
            summ, lx = self._run(env, stop_fill=True, pos=None)
            self.assertEqual(summ["exited"], 1)
            lx.assert_not_called()
            self.assertEqual(env.hand(), {})
            self.assertEqual(_rec(env)["state"], "flattened_no_stop")
            # second tick: nothing left pending, never a "ready" hand-off
            summ2, lx2 = self._run(env, stop_fill=True, pos=None, closed=True)
            self.assertEqual(summ2, {"promoted": 0, "exited": 0, "reverted": 0, "waiting": 0})
            self.assertEqual(env.hand(), {})

    def test_closed_in_log_without_handoff_is_not_promoted(self):
        with _Env(self._pending(), []) as env:
            summ, lx = self._run(env, closed=True, pos=_pos())
            self.assertEqual(summ["exited"], 1)
            lx.assert_not_called()
            self.assertEqual(env.hand(), {})
            self.assertEqual(_rec(env)["state"], "flattened_no_stop")

    def test_closed_in_log_with_booking_handoff_finishes(self):
        with _Env(self._pending(), []) as env:
            env.hand_path.write_text(json.dumps({"DT-AAPL-b-1-x": {"symbol": "AAPL", "status": "booking",
                                                                   "take_over_mark": 340.0, "swing_stop": 330.24}}))
            summ, lx = self._run(env, closed=True, pos=_pos())
            self.assertEqual(summ["promoted"], 1)
            lx.assert_not_called()
            self.assertEqual(env.hand()["DT-AAPL-b-1-x"]["status"], "ready")
            self.assertEqual(_rec(env)["state"], "promoted")

    def test_deadline_reverts_unresolved_lot_to_day_tier(self):
        with _Env(self._pending(), []) as env:
            summ, lx = self._run(env, legs=None, pos=_pos(), past_close=25.0)
            self.assertEqual(summ["reverted"], 1)
            lx.assert_not_called()
            r = _rec(env)
            self.assertEqual(r["state"], "protected")
            self.assertNotIn("promote", r)

    def test_lot_decided_on_an_earlier_day_reverts_pre_market(self):
        st = self._pending()
        st[next(iter(st))]["promote"]["decided_ts"] = "2026-10-01T12:56:00-07:00"
        with _Env(st, []) as env:
            summ, _lx = self._run(env, legs=None, pos=_pos(), past_close=-700.0)
            self.assertEqual(summ["reverted"], 1)

    def test_deadline_never_reverts_a_lot_mid_booking(self):
        with _Env(self._pending(), []) as env:
            env.hand_path.write_text(json.dumps({"DT-AAPL-b-1-x": {"symbol": "AAPL", "status": "booking",
                                                                   "take_over_mark": 340.0, "swing_stop": 330.24}}))
            summ, lx = self._run(env, pos=_pos(), past_close=25.0)
            self.assertEqual(summ["promoted"], 1)
            self.assertEqual(lx.call_args.kwargs["fill_price"], 340.0)

    def test_take_over_below_swing_stop_reverts(self):
        with _Env(self._pending(), []) as env:
            summ, lx = self._run(env, pos=_pos(px=328.0))
            self.assertEqual(summ["reverted"], 1)
            lx.assert_not_called()
            self.assertEqual(env.hand(), {})
            self.assertEqual(_rec(env)["state"], "protected")

    def test_ready_handoff_is_never_rebooked_or_reverted(self):
        with _Env(self._pending(), []) as env:
            env.hand_path.write_text(json.dumps({"DT-AAPL-b-1-x": {"symbol": "AAPL", "status": "adopted",
                                                                   "take_over_mark": 340.0, "swing_stop": 330.24}}))
            summ, lx = self._run(env, pos=_pos(), past_close=25.0)
            lx.assert_not_called()
            self.assertEqual(summ["reverted"], 0)
            self.assertEqual(_rec(env)["state"], "promoted")
            self.assertEqual(env.hand()["DT-AAPL-b-1-x"]["status"], "adopted")

    def test_own_booking_not_misread_as_stop_fill(self):
        with _Env(self._pending(), []) as env:
            env.hand_path.write_text(json.dumps({"DT-AAPL-b-1-x": {"symbol": "AAPL", "status": "booking",
                                                                   "take_over_mark": 340.0, "swing_stop": 330.24}}))
            with mock.patch.object(dp, "_booked_promotion", return_value=True):
                summ, lx = self._run(env, stop_fill=True, pos=_pos())
            self.assertEqual(summ["promoted"], 1)
            lx.assert_not_called()
            self.assertEqual(_rec(env)["state"], "promoted")

    def test_failed_ready_write_is_finished_next_tick(self):
        with _Env(self._pending(), []) as env:
            real, n = dp._update, {"k": 0}

            def flaky(tid, fields):
                if fields.get("status") == "ready" and n["k"] == 0:
                    n["k"] += 1
                    return False
                return real(tid, fields)
            with mock.patch.object(dp, "_update", side_effect=flaky):
                s1, _ = self._run(env, pos=_pos(px=340.42))
            self.assertEqual((s1["waiting"], _rec(env)["state"]), (1, "promote_pending"))
            s2, lx2 = self._run(env, pos=_pos(px=340.42), closed=True)
            lx2.assert_not_called()
            self.assertEqual(s2["promoted"], 1)
            self.assertEqual((env.hand()["DT-AAPL-b-1-x"]["status"], _rec(env)["state"]), ("ready", "promoted"))

    def test_unreadable_handoff_file_waits(self):
        with _Env(self._pending(), []) as env:
            env.hand_path.write_text("{broken")
            summ, lx = self._run(env, closed=True, pos=_pos(), past_close=25.0)
            self.assertEqual(summ["waiting"], 1)
            lx.assert_not_called()
            self.assertEqual(_rec(env)["state"], "promote_pending")

    def test_handoff_update_keeps_other_records(self):
        with _Env({}, []) as env:
            env.hand_path.write_text(json.dumps({"OTHER": {"symbol": "MSFT", "status": "ready"}}))
            self.assertTrue(dp._update("DT-AAPL-b-1-x", {"status": "booking"}))
            h = env.hand()
            self.assertEqual((h["OTHER"]["status"], h["DT-AAPL-b-1-x"]["status"]), ("ready", "booking"))

    def test_changed_position_reverts_to_day_exit(self):
        with _Env(self._pending(), []) as env:
            summ, _lx = self._run(env, pos=_pos(qty=3))
            self.assertEqual(summ["reverted"], 1)
            self.assertEqual(_rec(env)["state"], "protected")

    def test_failed_booking_retries_without_promoting(self):
        with _Env(self._pending(), []) as env:
            summ, _lx = self._run(env, pos=_pos(), log_ok=False)
            self.assertEqual(summ["waiting"], 1)
            self.assertEqual(_rec(env)["state"], "promote_pending")
            self.assertEqual(env.hand()["DT-AAPL-b-1-x"]["status"], "booking")


class Adopt(unittest.TestCase):
    def _hand(self, env, status="ready"):
        env.hand_path.write_text(json.dumps({"DT-AAPL-b-1-x": {
            "symbol": "AAPL", "trade_id": "DT-AAPL-b-1-x", "qty": 4, "day_entry": 335.24, "take_over_mark": 340.42,
            "swing_stop": 330.24, "target": 345.24, "atr": 4.0, "status": status}}))

    def _tracker(self, trades=None):
        t = SimpleNamespace(open_trades=dict(trades or {}), _save_log=mock.Mock())
        t.set_gtc_stop_order_id = lambda s, oid: t.open_trades[s].update(gtc_stop_order_id=oid)
        return t

    def test_adopts_with_swing_stop_from_day_cost_and_gtc(self):
        with _Env({}, []) as env:
            self._hand(env)
            tr = self._tracker()
            with mock.patch("execution.broker.get_open_position", return_value=_pos()), \
                    mock.patch("execution.broker.get_clock", return_value={"is_open": False}), \
                    mock.patch("execution.broker.submit_gtc_stop_order", return_value=SimpleNamespace(id="G1")) as g:
                self.assertEqual(dp.adopt_promotions(tr, risk=SimpleNamespace(open_positions=0)), ["AAPL"])
            t = tr.open_trades["AAPL"]
            self.assertEqual((t["entry_price"], t["stop"], t["promoted_day_cost"]), (340.42, 330.24, 335.24))
            self.assertTrue(t["_promoted_from_day_tier"] and t["overnight"])
            self.assertEqual(t["gtc_stop_order_id"], "G1")
            self.assertEqual(g.call_args.args, ("AAPL", 4, "sell", 330.24))
            self.assertEqual(env.hand()["DT-AAPL-b-1-x"]["status"], "adopted")
            rows = _tt.load_transfers()
            self.assertEqual([(r["ref"], r["qty"], r["price"]) for r in rows], [("DT-AAPL-b-1-x", 4.0, 340.42)])
            self.assertEqual(dp.adopt_promotions(tr), [])                  # idempotent

    def test_never_overwrites_another_swing_trade(self):
        with _Env({}, []) as env:
            self._hand(env)
            tr = self._tracker({"AAPL": {"symbol": "AAPL", "entry_price": 300.0}})
            self.assertEqual(dp.adopt_promotions(tr), [])
            self.assertEqual(tr.open_trades["AAPL"]["entry_price"], 300.0)
            self.assertEqual(env.hand()["DT-AAPL-b-1-x"]["status"], "ready")

    def test_position_gone_is_flagged_not_adopted(self):
        with _Env({}, []) as env:
            self._hand(env)
            tr = self._tracker()
            with mock.patch("execution.broker.get_open_position", return_value=None):
                self.assertEqual(dp.adopt_promotions(tr), [])
            self.assertEqual(env.hand()["DT-AAPL-b-1-x"]["status"], "gone")
            self.assertNotIn("AAPL", tr.open_trades)

    def test_partial_position_adopts_what_is_held(self):
        with _Env({}, []) as env:
            self._hand(env)
            tr = self._tracker()
            with mock.patch("execution.broker.get_open_position", return_value=_pos(qty=3)), \
                    mock.patch("execution.broker.get_clock", return_value={"is_open": False}), \
                    mock.patch("execution.broker.submit_gtc_stop_order", return_value=SimpleNamespace(id="G1")) as g:
                self.assertEqual(dp.adopt_promotions(tr), ["AAPL"])
            self.assertEqual(tr.open_trades["AAPL"]["qty"], 3)
            self.assertEqual(g.call_args.args[1], 3)
            self.assertEqual(env.hand()["DT-AAPL-b-1-x"]["adopted_qty"], 3)
            self.assertEqual([r["qty"] for r in _tt.load_transfers()], [3.0])

    def test_unreadable_handoff_does_nothing(self):
        with _Env({}, []) as env:
            env.hand_path.write_text("{broken")
            self.assertEqual(dp.adopt_promotions(self._tracker()), [])


if __name__ == "__main__":
    unittest.main()
