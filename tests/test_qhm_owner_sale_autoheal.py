# ruff: noqa: E501
"""Owner-sale auto-heal (design record logs/design_records/qhm_owner_sale_autoheal_2026-10-17.md).

Every broker, ledger and network call is mocked: these tests can never reach Alpaca."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import execution
import execution.quarterly_hold_manager as qhm_mod
from execution import manual_sale_proof as msp
from execution.quarterly_hold_manager import HoldPosition, HoldState, QuarterlyHoldManager

_T0 = datetime(2026, 10, 16, 17, 0, tzinfo=timezone.utc)
_UUID = "300fdc66-2dd5-4d9c-922c-ee4d9795465c"


def _row(**kw):
    base = {"id": "o1", "side": "sell", "source": None, "client_order_id": _UUID,
            "filled_qty": "1", "filled_at": (_T0 + timedelta(minutes=7)).isoformat()}
    base.update(kw)
    return base


_BOT = {"id": "qh-buy", "side": "buy", "source": "access_key", "client_order_id": "QH-LLY-b-1-x",
        "filled_qty": "1", "filled_at": (_T0 - timedelta(days=60)).isoformat()}


def _pos(**kw):
    base = dict(symbol="LLY", direction="long", target_equity_pct=0.2, state=HoldState.ACTIVE,
                qty_filled=1, qty_total=1, stop_price=1058.13, stop_order_id="qh-stop",
                qty_proven_at=_T0.isoformat(), created_at="2026-08-07T10:00:00-04:00")
    base.update(kw)
    return HoldPosition(**base)


def _manager(position=None, raise_on_position=False):
    m = object.__new__(QuarterlyHoldManager)
    m.dry_run = False
    m._positions = {}
    m._owner_sale_unproven = {}
    m._owner_sale_retry_day = {}
    m.broker = Mock()
    if raise_on_position:
        m.broker.get_position.side_effect = RuntimeError("503 from Alpaca")
    else:
        m.broker.get_position.return_value = position
    m._save_state = Mock(return_value=True)
    m._alert = Mock()
    m._now_et = Mock(return_value=datetime(2026, 10, 16, 14, 7, tzinfo=qhm_mod.ET))
    m._get_open_orders = Mock(return_value=[])
    return m


def _fake_guard(ledger_qhm=1.0, heal_ok=True):
    g = ModuleType("execution.ownership_guard")
    g.apply_authorized_tier_reduction = Mock(return_value=heal_ok)
    g.load_ledger = Mock(return_value={})
    g.tier_qty = Mock(return_value=ledger_qhm)
    g.tier_of_coid = lambda c: ("qhm" if str(c or "").startswith("QH-") else
                                "daytrade" if str(c or "").startswith("DT-") else None)
    return g


class ProofRuleTests(TestCase):
    def setUp(self):
        self.g = _fake_guard()
        self._p = patch.dict("sys.modules", {"execution.ownership_guard": self.g})
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def ev(self, rows, held=1, broker=0, with_bot=True):
        return msp.evaluate_owner_sale(None if rows is None else rows + ([_BOT] if with_bot else []),
                                       held, broker, _T0.isoformat())

    def test_signal_must_be_live_in_the_same_response(self):
        self.assertFalse(self.ev([_row()], with_bot=False).proven)

    def test_app_sale_is_proven(self):
        r = self.ev([_row()])
        self.assertTrue(r.proven)
        self.assertEqual((r.sold_qty, r.order_ids), (1, ["o1"]))

    def test_api_source_is_not_proven(self):
        self.assertFalse(self.ev([_row(source="access_key")]).proven)

    def test_any_other_source_value_is_not_proven(self):   # e.g. a broker liquidation
        self.assertFalse(self.ev([_row(source="liquidation")]).proven)

    def test_missing_source_key_is_not_proven(self):
        row = _row()
        del row["source"]
        self.assertFalse(self.ev([row]).proven)

    def test_non_uuid_order_id_is_not_proven(self):
        self.assertFalse(self.ev([_row(client_order_id="my-custom-id")]).proven)

    def test_buy_in_window_is_not_proven(self):
        self.assertFalse(self.ev([_row(), _row(id="b", side="buy")], held=2, broker=1).proven)

    def test_quantity_must_match_exactly(self):
        self.assertFalse(self.ev([_row(filled_qty="1")], held=2, broker=0).proven)

    def test_boolean_fill_quantity_is_not_evidence(self):
        self.assertFalse(self.ev([_row(filled_qty=True)]).proven)

    def test_fill_before_window_is_ignored(self):
        early = _row(id="old", filled_at=(_T0 - timedelta(days=2)).isoformat())
        self.assertTrue(self.ev([early, _row()]).proven)

    def test_order_placed_before_window_but_filled_inside_counts(self):
        self.assertTrue(self.ev([_row(created_at=(_T0 - timedelta(hours=3)).isoformat())]).proven)

    def test_bot_tagged_order_with_null_source_trips_the_wire(self):
        tagged = _row(id="t", client_order_id="QH-LLY-s-1-x", filled_qty="0", filled_at=None)
        self.assertFalse(self.ev([tagged, _row()]).proven)

    def test_unreadable_history_or_no_window_is_not_proven(self):
        self.assertFalse(self.ev(None).proven)
        self.assertFalse(msp.prove_owner_sale("LLY", None, 1, 0).proven)

    def test_unfilled_orders_are_ignored(self):
        self.assertTrue(self.ev([_row(id="c", filled_qty="0", filled_at=None, source="access_key",
                                      client_order_id="QH-LLY-s-2-y"), _row()]).proven)


class FlatHoldTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "logs").mkdir()
        self._root = patch.object(qhm_mod, "_ROOT", Path(self.tmp.name))
        self._dereg = patch.object(qhm_mod, "_deregister_symbol", Mock())
        self._root.start()
        self._dereg.start()

    def tearDown(self):
        self._root.stop()
        self._dereg.stop()
        self.tmp.cleanup()

    def _broker_mod(self, stop_status="canceled", cancel_ok=True):
        b = ModuleType("execution.broker")
        b.get_order = Mock(return_value=SimpleNamespace(status=stop_status))
        b.cancel_order = Mock(return_value=cancel_ok)
        return b

    def test_flat_preflight_hands_off_without_paging(self):
        m, p = _manager(position=None), _pos()
        self.assertEqual(m._reconcile_stop_coverage(p, []), (False, False))
        m._alert.assert_not_called()
        self.assertEqual(p.state, HoldState.ACTIVE)

    def test_broker_read_error_still_pages(self):
        m, p = _manager(raise_on_position=True), _pos()
        g = _fake_guard()
        with patch.dict("sys.modules", {"execution.ownership_guard": g}):
            self.assertEqual(m._reconcile_stop_coverage(p, []), (True, True))
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)
        m._alert.assert_called_once()

    def test_proven_owner_sale_closes_and_heals(self):
        m, p = _manager(position=None), _pos()
        m._get_open_orders.return_value = [SimpleNamespace(symbol="LLY", client_order_id="QH-LLY-s-9", side="sell", id="qh-stop")]
        g, b = _fake_guard(), self._broker_mod()
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=[_row(), _BOT])):
            self.assertTrue(m._detect_external_close(p, qhm_mod.ReconcileResult()))
        self.assertEqual(p.state, HoldState.CLOSED)
        g.apply_authorized_tier_reduction.assert_called_once_with("LLY", "qhm", 0.0, "owner_app_sale")
        b.cancel_order.assert_called_once_with("qh-stop")
        self.assertIn("Nothing to do", m._alert.call_args[0][0])
        events = (Path(self.tmp.name) / "logs" / "trade_events.jsonl").read_text()
        self.assertIn("owner_sale_autoheal", events)

    def test_unproven_sale_closes_but_does_not_heal(self):
        m, p = _manager(position=None), _pos()
        g, b = _fake_guard(), self._broker_mod()
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=[_row(source="access_key")])):
            self.assertTrue(m._detect_external_close(p, qhm_mod.ReconcileResult()))
        self.assertEqual(p.state, HoldState.CLOSED)
        g.apply_authorized_tier_reduction.assert_not_called()
        msg = m._alert.call_args[0][0]
        self.assertIn("confirm_ledger_heal.py LLY qhm 0", msg)
        self.assertIn("do not confirm", msg)

    def test_own_stop_fill_keeps_stop_out_heal_and_skips_proof(self):
        m, p = _manager(position=None), _pos()
        g, b = _fake_guard(), self._broker_mod(stop_status="filled")
        fetch = Mock()
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True), \
                patch.object(msp, "fetch_closed_orders_since", fetch):
            self.assertTrue(m._detect_external_close(p, qhm_mod.ReconcileResult()))
        g.apply_authorized_tier_reduction.assert_called_once_with("LLY", "qhm", 0.0, "qhm_stop_fill")
        fetch.assert_not_called()

    def test_kill_switch_off_falls_back_to_confirm(self):
        m, p = _manager(position=None), _pos()
        g, b = _fake_guard(), self._broker_mod()
        fetch = Mock(return_value=[_row(), _BOT])
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True), \
                patch.object(msp, "fetch_closed_orders_since", fetch), \
                patch.object(qhm_mod, "_OWNER_SALE_AUTOHEAL_ENABLED", False):
            m._detect_external_close(p, qhm_mod.ReconcileResult())
        fetch.assert_not_called()
        g.apply_authorized_tier_reduction.assert_not_called()

    def test_failed_stop_cancel_pages(self):
        m, p = _manager(position=None), _pos()
        m._get_open_orders.return_value = [SimpleNamespace(symbol="LLY", client_order_id="QH-LLY-s-9", side="sell", id="qh-stop")]
        g, b = _fake_guard(), self._broker_mod(cancel_ok=False)
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=[_row(), _BOT])):
            m._detect_external_close(p, qhm_mod.ReconcileResult())
        self.assertTrue(any("could not cancel" in c[0][0] for c in m._alert.call_args_list))


class PartialSaleTests(TestCase):
    def test_proven_partial_sale_heals_to_broker_qty(self):
        p = _pos(symbol="NVDA", qty_filled=2, qty_total=2)
        m = _manager(position=SimpleNamespace(qty="1", side="long"))
        g = _fake_guard(ledger_qhm=2.0)
        with patch.dict("sys.modules", {"execution.ownership_guard": g}), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=[_row(), _BOT])), \
                patch.object(QuarterlyHoldManager, "_record_owner_sale", Mock()):
            self.assertTrue(m._heal_owner_partial_sale(p))
        g.apply_authorized_tier_reduction.assert_called_once_with("NVDA", "qhm", 1.0, "owner_app_sale")

    def test_unproven_partial_sale_is_cached_and_not_requeried(self):
        p = _pos(symbol="NVDA", qty_filled=2, qty_total=2)
        m = _manager(position=SimpleNamespace(qty="1", side="long"))
        g = _fake_guard(ledger_qhm=2.0)
        fetch = Mock(return_value=[_row(source="access_key")])
        with patch.dict("sys.modules", {"execution.ownership_guard": g}), \
                patch.object(msp, "fetch_closed_orders_since", fetch):
            self.assertFalse(m._heal_owner_partial_sale(p))
            self.assertFalse(m._heal_owner_partial_sale(p))
        self.assertEqual(fetch.call_count, 1)
        g.apply_authorized_tier_reduction.assert_not_called()

    def test_position_moving_during_check_aborts(self):
        p = _pos(symbol="NVDA", qty_filled=2, qty_total=2)
        m = _manager()
        m.broker.get_position.side_effect = [SimpleNamespace(qty="1", side="long"), None]
        g = _fake_guard(ledger_qhm=2.0)
        with patch.dict("sys.modules", {"execution.ownership_guard": g}), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=[_row(), _BOT])):
            self.assertFalse(m._heal_owner_partial_sale(p))
        g.apply_authorized_tier_reduction.assert_not_called()


class PageDedupeAndStateTests(TestCase):
    def test_same_reason_pages_once_a_day_new_reason_pages(self):
        m, p = _manager(position=None), _pos()
        m._stop_coverage_pending(p, "ownership proof unavailable")
        m._stop_coverage_pending(p, "ownership proof unavailable")
        self.assertEqual(m._alert.call_count, 1)
        m._stop_coverage_pending(p, "multiple QH stop candidates")
        self.assertEqual(m._alert.call_count, 2)
        m._now_et.return_value = datetime(2026, 10, 17, 10, 0, tzinfo=qhm_mod.ET)
        m._stop_coverage_pending(p, "ownership proof unavailable")
        self.assertEqual(m._alert.call_count, 3)

    def test_from_dict_ignores_unknown_keys(self):
        d = _pos().to_dict()
        d["some_future_field"] = 1
        self.assertEqual(HoldPosition.from_dict(d).symbol, "LLY")


class StopFirstTests(TestCase):
    def test_partial_sale_shrinks_oversized_stop_before_the_record(self):
        p = _pos(symbol="NVDA", qty_filled=2, qty_total=2)
        m = _manager(position=SimpleNamespace(qty="1", side="long"))
        stop = SimpleNamespace(id="s2", symbol="NVDA", client_order_id="QH-NVDA-s-1", side="sell", type="stop",
                               status="new", time_in_force="gtc", qty="2", filled_qty="0", stop_price="192.41")
        m._get_open_orders.return_value = [stop]
        calls = []
        g = _fake_guard(ledger_qhm=2.0)
        g.apply_authorized_tier_reduction.side_effect = lambda *a: calls.append("ledger") or True
        b = ModuleType("execution.broker")
        b.replace_stop_order = Mock(side_effect=lambda *a, **k: calls.append("stop") or SimpleNamespace(
            id="s3", symbol="NVDA", client_order_id="QH-NVDA-s-1", side="sell", type="stop", status="new",
            time_in_force="gtc", qty="1", filled_qty="0", stop_price="192.41"))
        rows = [_row(), dict(_BOT, client_order_id="QH-NVDA-b-1-x")]
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=rows)), \
                patch.object(QuarterlyHoldManager, "_record_owner_sale", Mock()):
            self.assertTrue(m._heal_owner_partial_sale(p))
        self.assertEqual(calls, ["stop", "ledger"])
        self.assertEqual(p.stop_order_id, "s3")

    def test_unconfirmed_stop_shrink_leaves_the_record_alone(self):
        p = _pos(symbol="NVDA", qty_filled=2, qty_total=2)
        m = _manager(position=SimpleNamespace(qty="1", side="long"))
        m._get_open_orders.return_value = [SimpleNamespace(
            id="s2", symbol="NVDA", client_order_id="QH-NVDA-s-1", side="sell", type="stop", status="new",
            qty="2", filled_qty="0", stop_price="192.41")]
        g = _fake_guard(ledger_qhm=2.0)
        b = ModuleType("execution.broker")
        b.replace_stop_order = Mock(return_value=None)
        rows = [_row(), dict(_BOT, client_order_id="QH-NVDA-b-1-x")]
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=rows)):
            self.assertFalse(m._heal_owner_partial_sale(p))
        g.apply_authorized_tier_reduction.assert_not_called()


class NoBuyBackTests(TestCase):
    def _mgr(self, prev, nbf):
        m = _manager()
        m._thesis_config = {"LLY": {"not_before_date": nbf}}
        m._positions = {"LLY": prev}
        return m

    def test_owner_sold_hold_is_not_re_added(self):
        prev = _pos(state=HoldState.CLOSED, closed_reason="owner_app_sale", updated_at="2026-10-16T14:07:00-04:00")
        m = self._mgr(prev, "2026-08-07")
        m.add_candidate("LLY", 0.2)
        self.assertIs(m._positions["LLY"], prev)
        m._save_state.assert_not_called()

    def test_re_dated_pick_is_re_added(self):
        prev = _pos(state=HoldState.CLOSED, closed_reason="owner_confirmed", updated_at="2026-10-16T14:07:00-04:00")
        m = self._mgr(prev, "2026-11-02")
        m.add_candidate("LLY", 0.2)
        self.assertEqual(m._positions["LLY"].state, HoldState.PENDING_ENTRY)

    def test_stop_out_close_is_still_re_added(self):
        prev = _pos(state=HoldState.CLOSED, closed_reason="qhm_stop_out", updated_at="2026-10-16T14:07:00-04:00")
        m = self._mgr(prev, "2026-08-07")
        m.add_candidate("LLY", 0.2)
        self.assertEqual(m._positions["LLY"].state, HoldState.PENDING_ENTRY)


class AlreadyConfirmedTests(FlatHoldTests):
    def test_unproven_but_already_confirmed_is_owner_exit_without_confirm_request(self):
        m, p = _manager(position=None), _pos(state=HoldState.PENDING_STOP_REPLACE, qty_proven_at=None)
        g, b = _fake_guard(ledger_qhm=0.0), self._broker_mod()
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True):
            self.assertTrue(m._detect_external_close(p, qhm_mod.ReconcileResult()))
        self.assertEqual((p.state, p.closed_reason), (HoldState.CLOSED, "owner_confirmed"))
        self.assertNotIn("confirm_ledger_heal", m._alert.call_args[0][0])
        g.apply_authorized_tier_reduction.assert_not_called()


class RetryAndApiSaleTests(FlatHoldTests):
    def test_unreadable_history_keeps_hold_open_same_day_then_closes(self):
        m, p = _manager(position=None), _pos()
        g, b = _fake_guard(), self._broker_mod()
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=None)):
            self.assertFalse(m._detect_external_close(p, qhm_mod.ReconcileResult()))
            self.assertEqual(p.state, HoldState.ACTIVE)
            m._now_et.return_value = datetime(2026, 10, 17, 10, 0, tzinfo=qhm_mod.ET)
            self.assertTrue(m._detect_external_close(p, qhm_mod.ReconcileResult()))
        self.assertEqual((p.state, p.closed_reason), (HoldState.CLOSED, "external_close_detected"))
        self.assertIn("confirm_ledger_heal.py LLY qhm 0", m._alert.call_args[0][0])

    def test_api_sale_with_ledger_zero_is_not_called_owner_confirmed(self):
        m, p = _manager(position=None), _pos()
        g, b = _fake_guard(ledger_qhm=0.0), self._broker_mod()
        rows = [_row(source="access_key"), _BOT]
        with patch.dict("sys.modules", {"execution.ownership_guard": g, "execution.broker": b}), \
                patch.object(execution, "ownership_guard", g, create=True), \
                patch.object(msp, "fetch_closed_orders_since", Mock(return_value=rows)):
            self.assertTrue(m._detect_external_close(p, qhm_mod.ReconcileResult()))
        self.assertEqual(p.closed_reason, "external_close_detected")

    def test_full_order_page_is_a_definitive_not_proven(self):
        self.assertEqual(msp.evaluate_owner_sale([{"_truncated": True}], 1, 0, _T0.isoformat()).proven, False)
