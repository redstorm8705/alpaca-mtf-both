# ruff: noqa: E501
"""QHM-ADOPT-1111 — QHM adopts shares its own tagged fills prove it bought, stop first (design record
logs/design_records/qhm_adopt_proven_2026-11-11.md). Every broker, ledger, alert and tier source is mocked; nothing
reaches Alpaca, Slack, the ownership ledger file or trade_events.jsonl."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  — test guard
from execution import quarterly_hold_manager as qm

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=qm.ET)


def _stop(qty, oid="stop-2", price=163.07, status="new"):
    return SimpleNamespace(id=oid, symbol="NVDA", client_order_id="QH-NVDA-s-1-a", side="sell", type="stop",
                           status=status, time_in_force="gtc", qty=qty, filled_qty=0, stop_price=price)


def _pos(qty=2, **kw):
    return qm.HoldPosition(symbol="NVDA", direction="long", target_equity_pct=0.2, state=qm.HoldState.ACTIVE,
                           qty_filled=qty, qty_total=qty, entry_day="2026-09-29", stop_price=163.07,
                           stop_order_id="stop-2", **kw)


def _mgr(pos, *, broker_qty=3, price=180.0, proved=3, claimed=0.0, save_ok=True):
    m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
    m.dry_run = False
    m.broker = SimpleNamespace(get_position=lambda s: SimpleNamespace(qty=broker_qty, side="long",
                                                                      current_price=price))
    m._positions = {pos.symbol: pos}
    m.alerts = []
    m._alert = m.alerts.append
    m.saves = []
    m._save_state = lambda: m.saves.append(pos.qty_filled) or save_ok
    m._now_et = lambda: NOW
    m.pending = []
    m._stop_coverage_pending = lambda p, reason, extra="": m.pending.append(reason) or True
    m._qhm_proved_fill_qty = mock.Mock(return_value=proved)
    m._other_tier_claim = lambda s: claimed
    m._record_adoption = mock.Mock()
    return m


class Adopt(unittest.TestCase):
    def setUp(self):
        qm._ADOPT_LOG_DAY.clear()

    def _run(self, m, pos, book, replaced=None):
        calls = []

        def _replace(sym, oid, price, qty):
            calls.append((sym, oid, price, qty))
            return replaced if replaced is not None else _stop(qty, oid="stop-3")
        with mock.patch("execution.broker.replace_stop_order", side_effect=_replace):
            out = m._maybe_adopt_proven_shares(pos, book)
        return out, calls

    def test_nvda_replay_adopts_after_the_stop_covers_three(self):
        """11-09 NVDA: proved 3, record 2, broker 3, no other tier -> stop 2 -> 3 at $163.07, then record 3."""
        pos = _pos()
        m = _mgr(pos)
        out, calls = self._run(m, pos, [_stop(2)])
        self.assertEqual(out, (True, True))
        self.assertEqual(calls, [("NVDA", "stop-2", 163.07, 3)])
        self.assertEqual((pos.qty_filled, pos.qty_total, pos.stop_order_id, pos.proven_adopt_qty), (3, 3, "stop-3", 3))
        self.assertEqual(m.saves, [3])
        self.assertEqual(m.alerts, [])                       # broker 3 == stop 3: no post-check page

    def test_unconfirmed_resize_adopts_nothing(self):
        pos = _pos()
        m = _mgr(pos)
        out, _ = self._run(m, pos, [_stop(2)], replaced=_stop(2, oid="stop-3"))   # broker still shows 2
        self.assertEqual(out, (True, True))
        self.assertEqual((pos.qty_filled, pos.proven_adopt_qty), (2, None))
        self.assertEqual(m.pending, ["proven-share adoption stop resize unconfirmed"])
        self.assertEqual(m.saves, [])

    def test_failed_save_restores_the_record(self):
        pos = _pos()
        m = _mgr(pos, save_ok=False)
        out, _ = self._run(m, pos, [_stop(2)])
        self.assertEqual(out, (True, True))
        self.assertEqual((pos.qty_filled, pos.qty_total, pos.proven_adopt_qty), (2, 2, None))
        self.assertEqual(m.pending, ["proven-share adoption did not persist"])

    def test_no_gap_reads_no_proof(self):
        pos = _pos(qty=3)
        m = _mgr(pos)
        out, calls = self._run(m, pos, [_stop(3)])
        self.assertIsNone(out)
        m._qhm_proved_fill_qty.assert_not_called()
        self.assertEqual(calls, [])

    def test_price_at_or_below_stop_never_resizes(self):
        pos = _pos()
        m = _mgr(pos, price=163.07)
        out, calls = self._run(m, pos, [_stop(2, price=163.07)])
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual(pos.qty_filled, 2)

    def test_other_open_sells_hold_the_shares(self):
        """The owner's own 1-share stop rests: broker 3 - open sells 1 < proved 3 -> no resize call."""
        pos = _pos()
        m = _mgr(pos)
        owner = SimpleNamespace(id="own-1", symbol="NVDA", client_order_id="web-1", side="sell", type="stop",
                                status="new", qty=1, filled_qty=0, stop_price=160.0)
        out, calls = self._run(m, pos, [_stop(2), owner])
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_another_tier_claim_is_a_contradiction_not_an_adoption(self):
        pos = _pos()
        m = _mgr(pos, claimed=1.0)
        out, calls = self._run(m, pos, [_stop(2)])
        self.assertIsNone(out)
        self.assertEqual(calls, [])
        self.assertEqual(len(m.alerts), 1)
        self._run(m, pos, [_stop(2)])
        self.assertEqual(len(m.alerts), 1)                   # once a day

    def test_unreadable_inputs_adopt_nothing(self):
        for kw in ({"proved": None}, {"claimed": None}):
            pos = _pos()
            m = _mgr(pos, **kw)
            out, calls = self._run(m, pos, [_stop(2)])
            self.assertIsNone(out, kw)
            self.assertEqual(calls, [], kw)

    def test_proof_above_broker_or_not_above_record(self):
        for proved, broker in ((4, 3), (2, 3)):
            pos = _pos()
            m = _mgr(pos, proved=proved, broker_qty=broker)
            out, calls = self._run(m, pos, [_stop(2)])
            self.assertIsNone(out)
            self.assertEqual(calls, [])

    def test_not_applicable_shapes(self):
        cases = [
            (_pos(), [_stop(2), _stop(2, oid="x")]),   # two QH sells
            (_pos(), [_stop(2, price=150.0)]),                                         # stop price differs
            (_pos(), [_stop(1)]),                                                      # stop != record
            (_pos(), []),                                                              # no QH stop
        ]
        for pos, book in cases:
            m = _mgr(pos)
            out, calls = self._run(m, pos, book)
            self.assertIsNone(out)
            self.assertEqual(calls, [])
        staged = _pos(owner_add_stage={"phase": "x"})
        self.assertIsNone(self._run(_mgr(staged), staged, [_stop(2)])[0])

    def test_post_check_pages_when_broker_holds_more(self):
        """Owner bought 2 more by hand (broker 5) before the adoption: QHM adopts its proven 3, the post-check
        names the other 2."""
        pos = _pos()
        m = _mgr(pos, broker_qty=5, claimed=0.0)
        out, calls = self._run(m, pos, [_stop(2)])
        self.assertEqual(calls[0][3], 3)
        self.assertEqual(pos.qty_filled, 3)
        self.assertEqual(len(m.alerts), 1)
        self.assertIn("holds 5", m.alerts[0])


class ResyncFloor(unittest.TestCase):
    """The strict resync keeps the adopted count until the ledger books it (else the per-cycle reconcile would
    shrink the stop back to the ledger's 2)."""

    def _resync(self, pos, broker_qty, ledger_qhm):
        m = qm.QuarterlyHoldManager.__new__(qm.QuarterlyHoldManager)
        m.dry_run = False
        m.broker = SimpleNamespace(get_position=lambda s: SimpleNamespace(qty=broker_qty, side="long"))
        m._now_et = lambda: NOW
        led = {"positions": {"NVDA": {"tiers": {"qhm": {"qty": ledger_qhm}, "intraday": {"qty": broker_qty - ledger_qhm}}}}}
        with mock.patch("execution.ownership_guard.load_ledger", return_value=led):
            return m._strict_resync_qty(pos)

    def test_floor_holds_until_the_ledger_books_it(self):
        pos = _pos(qty=3, proven_adopt_qty=3)
        self.assertEqual(self._resync(pos, 3, 2), 3)
        self.assertEqual(pos.proven_adopt_qty, 3)
        self.assertEqual(self._resync(pos, 3, 3), 3)
        self.assertIsNone(pos.proven_adopt_qty)            # booked: the floor clears

    def test_floor_clears_when_the_broker_holds_fewer(self):
        pos = _pos(qty=3, proven_adopt_qty=3)
        self.assertEqual(self._resync(pos, 2, 2), 2)
        self.assertIsNone(pos.proven_adopt_qty)

    def test_no_floor_is_unchanged(self):
        self.assertEqual(self._resync(_pos(qty=3), 3, 2), 2)

    def test_state_round_trip_keeps_the_floor(self):
        pos = _pos(qty=3, proven_adopt_qty=3)
        self.assertEqual(qm.HoldPosition.from_dict(pos.to_dict()).proven_adopt_qty, 3)


if __name__ == "__main__":
    unittest.main()
