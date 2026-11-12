# ruff: noqa: E501
"""QHM-RESTOP-1112 — every QHM add recalculates the stop from the new average cost (design record
logs/design_records/qhm_restop_on_add_2026-11-12.md, rev 2 steps a-k). Every broker, bar, ledger and alert call is
mocked; nothing reaches Alpaca, Slack or trade_events.jsonl."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  — test guard
from execution import quarterly_hold_manager as qm

NOW = datetime(2026, 11, 12, 11, 0, tzinfo=qm.ET)


def _stop(qty, price, oid="stop-1"):
    return SimpleNamespace(id=oid, symbol="NVDA", client_order_id="QH-NVDA-s-1-a", side="sell", type="stop",
                           status="new", time_in_force="gtc", qty=qty, filled_qty=0, stop_price=price)


def _pos(qty=5, stop=163.07, restop_qty=2, avg=173.506, **kw):
    return qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                           qty_filled=qty, qty_total=qty, entry_day="2026-09-29", stop_price=stop,
                           stop_order_id="stop-1", avg_entry_price=avg, restop_qty=restop_qty, **kw)


def _design(sym, avg):
    """Stand-in for _design_stop_price with ATR 16.47 x 2.5 and a 15% floor (NVDA 11-12 inputs)."""
    return round(max(avg - 16.47 * 2.5, avg * 0.85), 2)


def _mgr(pos, *, broker_qty=5, live=155.95, broker_avg=173.506, margin=1.5, claimed=0.0, atr=16.47,
         design=_design, save_ok=True):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m.broker = SimpleNamespace(get_position=lambda s: SimpleNamespace(qty=broker_qty, side="long", current_price=live,
                                                                      avg_entry_price=broker_avg))
    m._positions = {pos.symbol: pos}
    m.alerts = []
    m._alert = m.alerts.append
    m.saves = []
    m._save_state = lambda: m.saves.append((pos.stop_price, pos.restop_qty)) or save_ok
    m._now_et = lambda: NOW
    m.pending = []
    m._stop_coverage_pending = lambda p, reason, extra="": m.pending.append(reason) or True
    m._other_tier_claim = lambda s: claimed
    m._overnight_gap_margin = lambda s, px: margin
    m._weekly_atr = lambda s: atr
    m._design_stop_price = design
    return m


class Restop(unittest.TestCase):
    def setUp(self):
        qm._RESTOP_PAGE_DAY.clear()

    def _run(self, m, pos, book, replaced=None):
        calls = []

        def _replace(sym, oid, price, qty):
            calls.append((sym, oid, price, qty))
            return replaced if replaced is not None else _stop(qty, price, oid="stop-2")
        with mock.patch("execution.broker.replace_stop_order", side_effect=_replace), \
                mock.patch("builtins.open", mock.mock_open()):
            out = m._maybe_restop_after_add(pos, book)
        return out, calls

    def test_nvda_average_down_lowers_stop_to_cost_stop(self):
        """5 shares, avg $173.51, old stop $163.07 above the market ($155.95): stop -> $147.48 for 5, Slack line."""
        pos = _pos()
        m = _mgr(pos)
        out, calls = self._run(m, pos, [_stop(5, 163.07)])
        self.assertEqual(out, (True, True))
        self.assertEqual(calls, [("NVDA", "stop-1", 147.48, 5)])
        self.assertEqual((pos.stop_price, pos.stop_order_id, pos.restop_qty), (147.48, "stop-2", 5))
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("163.07 -> $147.48", m.alerts[0])

    def test_first_sight_only_baselines(self):
        pos = _pos(restop_qty=None)
        m = _mgr(pos)
        out, calls = self._run(m, pos, [_stop(5, 163.07)])
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual(pos.restop_qty, 5)

    def test_trim_only_rebaselines(self):
        pos = _pos(qty=3, restop_qty=5)
        m = _mgr(pos, broker_qty=3)
        out, calls = self._run(m, pos, [_stop(3, 163.07)])
        self.assertIsNone(out)
        self.assertEqual((calls, pos.restop_qty, pos.stop_price), ([], 3, 163.07))

    def test_harris_premarket_winner_input_never_keeps_a_stop_above_live(self):
        """3 sh at $190 (stop $161.50) + 10 at $150 -> avg $159.23, live $160: the old stop is above live, so it is
        not kept; the result is below live - m."""
        pos = _pos(qty=13, stop=161.50, restop_qty=3, avg=159.23)
        m = _mgr(pos, broker_qty=13, live=160.0, broker_avg=159.23)
        out, calls = self._run(m, pos, [_stop(13, 161.50)])
        self.assertEqual(out, (True, True))
        self.assertLess(calls[0][2], 160.0 - 1.5)
        self.assertLess(pos.stop_price, 160.0 - 1.5)

    def test_risk_seat_input_keeps_a_still_valid_stop(self):
        """avg $173.51, old stop $135, live $140: cost stop $147.48 is above live - m, the price stop ($119) is lower
        than the still-valid $135 -> the stop stays $135 (no change)."""
        pos = _pos(stop=135.0)
        m = _mgr(pos, live=140.0)
        out, calls = self._run(m, pos, [_stop(5, 135.0)])
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual((pos.stop_price, pos.restop_qty), (135.0, 5))

    def test_winner_keeps_its_higher_valid_stop(self):
        """live - avg >= one ATR and the old stop is valid and above the cost stop -> kept."""
        pos = _pos(stop=190.0, avg=180.0)
        m = _mgr(pos, live=215.0, broker_avg=180.0)
        out, calls = self._run(m, pos, [_stop(5, 190.0)])
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual(pos.stop_price, 190.0)

    def test_raise_on_add_stays_below_market(self):
        """Owner rule: recalculated up as well as down; old $120, cost $147.48, live $155.95 -> $147.48."""
        pos = _pos(stop=120.0)
        m = _mgr(pos)
        out, calls = self._run(m, pos, [_stop(5, 120.0)])
        self.assertEqual(calls[0][2], 147.48)
        self.assertEqual(m.alerts, [])                       # only lowerings send the Slack line

    def test_no_level_below_market_pages_and_changes_nothing(self):
        pos = _pos(stop=163.07)
        m = _mgr(pos, design=lambda s, a: 200.0)              # both cost and price stops above live
        out, calls = self._run(m, pos, [_stop(5, 163.07)])
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("ACTION", m.alerts[0])
        self._run(m, pos, [_stop(5, 163.07)])
        self.assertEqual(len(m.alerts), 1)                   # once a day

    def test_unreadable_margin_pages_only_when_stop_not_below_market(self):
        pos = _pos(stop=120.0)
        m = _mgr(pos, margin=None)
        self.assertIsNone(self._run(m, pos, [_stop(5, 120.0)])[0])
        self.assertEqual(m.alerts, [])
        pos2 = _pos(stop=163.07)
        m2 = _mgr(pos2, margin=None)
        self._run(m2, pos2, [_stop(5, 163.07)])
        self.assertEqual(len(m2.alerts), 1)

    def test_unconfirmed_resize_changes_nothing(self):
        pos = _pos()
        m = _mgr(pos)
        out, _ = self._run(m, pos, [_stop(5, 163.07)], replaced=_stop(5, 163.07, oid="stop-2"))
        self.assertEqual(out, (True, True))
        self.assertEqual((pos.stop_price, pos.restop_qty), (163.07, 2))
        self.assertEqual(m.pending, ["stop recalculation resize unconfirmed"])

    def test_unreadable_atr_on_a_winner_keeps_the_valid_stop(self):
        """Cold review: avg $180, stop $190 (valid), live $215, weekly bars unreadable (design falls back to the 15%
        floor $153, ATR None) -> the stop stays $190, never loosened."""
        pos = _pos(stop=190.0, avg=180.0)
        m = _mgr(pos, live=215.0, broker_avg=180.0, atr=None, design=lambda s, a: round(a * 0.85, 2))
        out, calls = self._run(m, pos, [_stop(5, 190.0)])
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual(pos.stop_price, 190.0)

    def test_failed_save_keeps_the_broker_stop_in_memory(self):
        """Cold review: the broker stop moved to $147.48 but the save failed -> memory keeps $147.48 and the new id (the
        next reconcile matches it and never moves it back above the market); only the recalc re-arms; the Slack line
        is still sent."""
        pos = _pos()
        m = _mgr(pos, save_ok=False)
        out, _ = self._run(m, pos, [_stop(5, 163.07)])
        self.assertEqual(out, (True, True))
        self.assertEqual((pos.stop_price, pos.stop_order_id, pos.restop_qty), (147.48, "stop-2", 2))
        self.assertEqual(m.pending, ["stop recalculation did not persist"])
        self.assertEqual(len(m.alerts), 1)

    def test_lot_in_profit_never_loosens_a_valid_stop(self):
        """Round-2 review: avg $101 after the add, profit-locked stop $105, live $115 -> stop stays $105."""
        pos = _pos(stop=105.0, avg=101.0)
        m = _mgr(pos, live=115.0, broker_avg=101.0)
        out, calls = self._run(m, pos, [_stop(5, 105.0)])
        self.assertIsNone(out)
        self.assertEqual((calls, pos.stop_price), ([], 105.0))

    def test_profit_locked_stop_just_under_market_is_kept(self):
        """Round-3 review: 5 sh at $100, trail-locked stop $140, price $141, owner adds 1 at $141 (avg $106.83, m $1.50)
        -> the $140 stop stays (it is below the market even though within m of it)."""
        pos = _pos(qty=6, stop=140.0, restop_qty=5, avg=106.83)
        m = _mgr(pos, broker_qty=6, live=141.0, broker_avg=106.83)
        out, calls = self._run(m, pos, [_stop(6, 140.0)])
        self.assertIsNone(out)
        self.assertEqual((calls, pos.stop_price, pos.restop_qty), ([], 140.0, 6))

    def test_reconcile_never_moves_a_stop_up_through_the_market(self):
        """Round-2 review: a lost replace reply left the record at $163.07 while Alpaca holds stop-2 at $147.48 for 5
        and NVDA trades $155.95 -> the reconcile adopts $147.48 (no replace call) and sends one HEADS-UP line."""
        pos = _pos(stop=163.07, restop_qty=5)
        pos.state = qm.HoldState.PENDING_STOP_REPLACE
        m = _mgr(pos)
        m._strict_resync_qty = lambda p, *a, **k: 5
        m._get_live_price = lambda s: 155.95
        m._after_confirmed_stop = lambda *a, **k: None
        with mock.patch("execution.broker.replace_stop_order") as rep:
            out = m._reconcile_stop_coverage(pos, [_stop(5, 147.48, oid="stop-2")])
        rep.assert_not_called()
        self.assertEqual(out, (False, False))
        self.assertEqual((pos.stop_price, pos.stop_order_id, pos.state), (147.48, "stop-2", qm.HoldState.ACTIVE))
        self.assertEqual(len(m.alerts), 1)

    def test_reconcile_still_resizes_quantity_below_the_market(self):
        """The guard changes only a price at or above the market; a normal quantity resize is unchanged."""
        pos = _pos(stop=147.48, restop_qty=5)
        m = _mgr(pos)
        m._strict_resync_qty = lambda p, *a, **k: 5
        m._get_live_price = lambda s: 155.95
        with mock.patch("execution.broker.replace_stop_order",
                        return_value=_stop(5, 147.48, oid="stop-2")) as rep:
            m._reconcile_stop_coverage(pos, [_stop(4, 147.48)])
        rep.assert_called_once_with("NVDA", "stop-1", 147.48, qty=5)

    def test_broker_holds_fewer_than_qhm_count(self):
        pos = _pos()
        m = _mgr(pos, broker_qty=4)
        self.assertIsNone(self._run(m, pos, [_stop(5, 163.07)])[0])

    def test_f6_in_symbol_uses_qhm_average_and_count(self):
        """Another tier holds 2 more (broker 7): QHM's own average and its 5 shares, never the broker's."""
        pos = _pos(avg=173.506)
        m = _mgr(pos, broker_qty=7, broker_avg=150.0, claimed=2.0)
        out, calls = self._run(m, pos, [_stop(5, 163.07)])
        self.assertEqual(calls, [("NVDA", "stop-1", 147.48, 5)])

    def test_not_applicable_shapes(self):
        for pos, book in ((_pos(), []), (_pos(), [_stop(4, 163.07)]), (_pos(), [_stop(5, 150.0)]),
                          (_pos(owner_add_stage={"phase": "x"}), [_stop(5, 163.07)])):
            m = _mgr(pos)
            self.assertIsNone(self._run(m, pos, book)[0])

    def test_bound_three_bot_dip_adds(self):
        """Risk seat bound: averages only fall to prices the bot bought at, each add's cost stop is >= 85% of that
        average, so three adds at -15% steps keep the stop >= first entry x 0.85 x 0.85."""
        first = 200.0
        avg = first
        qty = 1
        for px in (first * 0.85, first * 0.85 * 0.95, first * 0.85 * 0.9):
            avg = (avg * qty + px) / (qty + 1)
            qty += 1
            stop = _design("NVDA", avg)
            self.assertGreaterEqual(stop, first * 0.85 * 0.85)

    def test_state_round_trip_keeps_restop_qty(self):
        pos = _pos(restop_qty=5)
        self.assertEqual(qm.HoldPosition.from_dict(pos.to_dict()).restop_qty, 5)


if __name__ == "__main__":
    unittest.main()
