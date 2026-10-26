from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from execution.quarterly_hold_manager import (
    HoldPosition,
    HoldState,
    QuarterlyHoldManager,
)


def _pos(**kw):
    base = dict(symbol="NVDA", direction="long", target_equity_pct=0.2,
                state=HoldState.ACTIVE, qty_filled=2, qty_total=2,
                stop_price=200.0, stop_order_id="old-stop")
    base.update(kw)
    return HoldPosition(**base)


def _order(**kw):
    base = dict(id="stop-1", symbol="NVDA", client_order_id="QH-NVDA-sell-1",
                side="sell", type="stop", status="accepted", qty="2",
                filled_qty="0", stop_price="200.00", time_in_force="gtc")
    base.update(kw)
    return SimpleNamespace(**base)


def _manager(strict=(2, 2), saves=(True,)):
    m = object.__new__(QuarterlyHoldManager)
    m.dry_run = False
    m._positions = {}
    m._strict_resync_qty = Mock(side_effect=list(strict))
    m._save_state = Mock(side_effect=list(saves))
    m._alert = Mock()
    m._now_et = Mock(return_value=datetime.now(timezone.utc))
    return m


class QHMStopQuantityReconcileTests(TestCase):
    def test_strict_qty_rejects_fractional_boolean_and_nonfinite_evidence(self):
        for broker_qty, ledger_qty in (
            ("2.5", 2), (True, 2), ("nan", 2), (2, 1.5), (2, True), (2, float("inf")),
        ):
            with self.subTest(broker_qty=broker_qty, ledger_qty=ledger_qty):
                m = object.__new__(QuarterlyHoldManager)
                m.dry_run = False
                m.broker = Mock(return_value=None)
                m.broker.get_position.return_value = SimpleNamespace(
                    qty=broker_qty, side="long"
                )
                p = _pos()
                fake = ModuleType("execution.ownership_guard")
                fake.load_ledger = Mock(return_value={})
                fake.tier_qty = Mock(return_value=ledger_qty)
                with patch.dict("sys.modules", {"execution.ownership_guard": fake}):
                    self.assertIsNone(m._strict_resync_qty(p))

    def test_exact_stop_is_adopted_and_persisted(self):
        m, p = _manager(), _pos(stop_order_id="stale-id")
        skip, blocked = m._reconcile_stop_coverage(p, [_order(id="live-id")])
        self.assertEqual((skip, blocked), (False, False))
        self.assertEqual((p.stop_order_id, p.state), ("live-id", HoldState.ACTIVE))
        self.assertEqual(m._strict_resync_qty.call_count, 2)
        m._save_state.assert_called_once()

    def test_shared_symbol_uses_qhm_owned_qty_for_atomic_resize(self):
        m, p = _manager(strict=(2, 2)), _pos(qty_filled=9, qty_total=9)
        replacement = _order(id="stop-2", qty="2")
        fake = ModuleType("execution.broker")
        fake.replace_stop_order = Mock(return_value=replacement)
        with patch.dict("sys.modules", {"execution.broker": fake}):
            skip, blocked = m._reconcile_stop_coverage(p, [_order(qty="5")])
        self.assertEqual((skip, blocked), (True, False))
        fake.replace_stop_order.assert_called_once_with("NVDA", "stop-1", 200.0, qty=2)
        self.assertEqual(p.stop_order_id, "stop-2")

    def test_qh_buy_is_not_a_stop_candidate_or_replace_target(self):
        m, p = _manager(strict=(2,), saves=(True, True)), _pos()
        fake = ModuleType("execution.broker")
        fake.replace_stop_order = Mock()
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertEqual(
                m._reconcile_stop_coverage(p, [_order(side="buy")]), (False, False)
            )
        fake.replace_stop_order.assert_not_called()
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_partial_stop_is_ambiguous(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        self.assertEqual(
            m._reconcile_stop_coverage(p, [_order(qty="3", filled_qty="1")]),
            (True, True),
        )

    def test_multiple_qh_stops_cause_zero_mutation(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        fake = ModuleType("execution.broker")
        fake.replace_stop_order = Mock()
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertEqual(
                m._reconcile_stop_coverage(p, [_order(id="a"), _order(id="b")]),
                (True, True),
            )
        fake.replace_stop_order.assert_not_called()

    def test_adoption_save_failure_blocks_stray_guard(self):
        m = _manager(strict=(2, 2), saves=(False, False))
        p = _pos(stop_order_id="stale")
        self.assertEqual(
            m._reconcile_stop_coverage(p, [_order(id="live")]), (True, True)
        )
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_no_qh_stop_arms_existing_recovery_without_blocking_it(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        self.assertEqual(m._reconcile_stop_coverage(p, []), (False, False))
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_no_stop_save_failure_blocks_same_cycle_recovery(self):
        m, p = _manager(strict=(2,), saves=(False,)), _pos()
        self.assertEqual(m._reconcile_stop_coverage(p, []), (True, True))

    def test_qh_buy_order_does_not_make_exact_sell_stop_ambiguous(self):
        m, p = _manager(strict=(2, 2), saves=(True,)), _pos()
        buy = _order(id="add-1", side="buy", type="limit", qty="1")
        self.assertEqual(
            m._reconcile_stop_coverage(p, [buy, _order(id="stop-exact")]),
            (False, False),
        )
        self.assertEqual(p.stop_order_id, "stop-exact")

    def test_wrong_replacement_result_is_not_accepted(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        fake = ModuleType("execution.broker")
        fake.replace_stop_order = Mock(return_value=_order(id="bad", qty="3"))
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertEqual(
                m._reconcile_stop_coverage(p, [_order(qty="5")]), (True, True)
            )
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)
        self.assertNotEqual(p.stop_order_id, "bad")

    def test_unreadable_ownership_causes_no_order_mutation(self):
        m, p = _manager(strict=(None,), saves=(True,)), _pos()
        fake = ModuleType("execution.broker")
        fake.replace_stop_order = Mock()
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertEqual(
                m._reconcile_stop_coverage(p, [_order(qty="5")]), (True, True)
            )
        fake.replace_stop_order.assert_not_called()

    def test_supplied_empty_snapshot_does_not_reread_broker(self):
        m = object.__new__(QuarterlyHoldManager)
        m.dry_run = False
        m._positions = {}
        fake = ModuleType("execution.broker")
        fake.get_open_orders = Mock(side_effect=AssertionError("must not re-read"))
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertEqual(m.cancel_stray_sell_orders(open_orders=[]), 0)
        fake.get_open_orders.assert_not_called()

    def test_weekly_check_reuses_one_snapshot_for_preflight_and_guard(self):
        m, p = _manager(strict=(None,), saves=(True,)), _pos()
        m._positions = {p.symbol: p}
        snapshot = [_order()]
        events = []
        m._get_open_orders = Mock(return_value=snapshot)
        m._reconcile_stop_coverage = Mock(
            side_effect=lambda _p, book: (
                events.append(("preflight", book)) or (False, False)
            )
        )
        m.cancel_stray_sell_orders = Mock(
            side_effect=lambda open_orders, skip_symbols: events.append(
                ("guard", open_orders, skip_symbols)
            ) or 0
        )
        m._detect_external_close = Mock(return_value=False)
        m._strict_resync_qty = Mock(return_value=None)
        m.run_weekly_check()
        self.assertIs(events[0][1], snapshot)
        self.assertIs(events[1][1], snapshot)
        self.assertEqual(events[0][0], "preflight")
        self.assertEqual(events[1][0], "guard")
        m._get_open_orders.assert_called_once()

    def test_blocked_preflight_skips_same_snapshot_guard_for_symbol(self):
        m, p = _manager(strict=(None,), saves=(True,)), _pos()
        m._positions = {p.symbol: p}
        snapshot = [_order()]
        m._get_open_orders = Mock(return_value=snapshot)
        m._reconcile_stop_coverage = Mock(return_value=(True, True))
        m.cancel_stray_sell_orders = Mock(return_value=0)
        m.run_weekly_check()
        kwargs = m.cancel_stray_sell_orders.call_args.kwargs
        self.assertIs(kwargs["open_orders"], snapshot)
        self.assertEqual(kwargs["skip_symbols"], {"NVDA"})

    def test_unreadable_snapshot_blocks_all_active_stop_mutation(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        m._positions = {p.symbol: p}
        m._get_open_orders = Mock(return_value=None)
        m.cancel_stray_sell_orders = Mock()
        m._detect_external_close = Mock()
        m._maybe_earnings_trim = Mock()
        m._maybe_trailing_lock = Mock()
        m._maybe_dip_add = Mock()
        m.run_weekly_check()
        m.cancel_stray_sell_orders.assert_not_called()
        m._detect_external_close.assert_not_called()
        m._maybe_earnings_trim.assert_not_called()
        m._maybe_trailing_lock.assert_not_called()
        m._maybe_dip_add.assert_not_called()
        m._alert.assert_called_once()

    def test_one_preflight_exception_does_not_skip_second_hold_or_guard(self):
        m = _manager(strict=(2,), saves=(True,))
        first = _pos(symbol="NVDA")
        second = _pos(symbol="AAPL")
        m._positions = {first.symbol: first, second.symbol: second}
        snapshot = [_order()]
        m._get_open_orders = Mock(return_value=snapshot)
        m._reconcile_stop_coverage = Mock(
            side_effect=[RuntimeError("bad NVDA"), (False, False)]
        )
        m.cancel_stray_sell_orders = Mock(return_value=0)
        m._detect_external_close = Mock(return_value=False)
        m.run_weekly_check()
        self.assertEqual(m._reconcile_stop_coverage.call_count, 2)
        m.cancel_stray_sell_orders.assert_called_once_with(
            open_orders=snapshot, skip_symbols={"NVDA"}
        )

    def test_stray_guard_preserves_other_tier_stop_on_coheld_symbol(self):
        for code in ("DT", "IN", "F6"):
            with self.subTest(code=code):
                m, p = _manager(), _pos()
                m._positions = {p.symbol: p}
                foreign_stop = _order(
                    id=f"{code}-stop", client_order_id=f"{code}-NVDA-s-1"
                )
                fake = ModuleType("execution.broker")
                fake.cancel_order = Mock(return_value=True)
                with patch.dict("sys.modules", {"execution.broker": fake}):
                    self.assertEqual(m.cancel_stray_sell_orders([foreign_stop]), 0)
                fake.cancel_order.assert_not_called()

    def test_stray_guard_still_cancels_untagged_manual_sell(self):
        for coid in ("", "manual-123", "ZZ-NVDA-s-1", "DT-AAPL-s-1"):
            with self.subTest(coid=coid):
                m, p = _manager(), _pos()
                m._positions = {p.symbol: p}
                manual_sell = _order(
                    id="manual-sell", client_order_id=coid, type="limit"
                )
                fake = ModuleType("execution.broker")
                fake.cancel_order = Mock(return_value=True)
                with patch.dict("sys.modules", {"execution.broker": fake}):
                    self.assertEqual(m.cancel_stray_sell_orders([manual_sell]), 1)
                fake.cancel_order.assert_called_once_with("manual-sell")

    def test_exact_adoption_clears_durable_recovery_metadata(self):
        m, p = _manager(strict=(2, 2), saves=(True,)), _pos()
        p.stop_recovery_client_order_id = "QH-NVDA-s-old"
        p.stop_recovery_intended_qty = 2
        self.assertEqual(
            m._reconcile_stop_coverage(p, [_order(id="adopted")]), (False, False)
        )
        self.assertIsNone(p.stop_recovery_client_order_id)
        self.assertIsNone(p.stop_recovery_intended_qty)

class QHMStopCrossPathTests(TestCase):
    def _assert_timeout_accepted_stop_is_recovered(self, qty):
        m, p = _manager(strict=(qty,), saves=(True,)), _pos(
            qty_filled=qty, qty_total=qty
        )
        m.broker = Mock()
        m._get_open_orders = Mock(return_value=[])
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(side_effect=TimeoutError("accepted, reply lost"))
        )
        lookups = 0

        def lookup(client_id):
            nonlocal lookups
            lookups += 1
            if lookups == 1:
                return None
            return _order(
                id=f"recovered-{qty}", qty=str(qty), client_order_id=client_id
            )

        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(side_effect=lookup)
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertIsNone(m._recover_or_submit_stop(
                p, qty, "sell", p.stop_price
            ))
            durable_id = p.stop_recovery_client_order_id
            self.assertIsNotNone(durable_id)
            recovered = m._recover_or_submit_stop(
                p, qty, "sell", p.stop_price
            )
        self.assertEqual(recovered.id, f"recovered-{qty}")
        self.assertEqual(p.stop_recovery_client_order_id, durable_id)
        m._dispatcher.submit_gtc_stop.assert_called_once()

    def test_fallback_stop_timeout_acceptance_recovers_exact_intent(self):
        self._assert_timeout_accepted_stop_is_recovered(1)

    def test_final_stop_timeout_acceptance_recovers_exact_intent(self):
        self._assert_timeout_accepted_stop_is_recovered(2)

    def test_exact_id_lookup_requires_matching_client_id_and_order_id(self):
        for returned in (
            _order(id="", client_order_id="QH-NVDA-s-intent"),
            _order(id="live", client_order_id="QH-NVDA-s-other"),
        ):
            with self.subTest(returned=returned):
                m, p = _manager(strict=(2,), saves=(True,)), _pos()
                p.stop_recovery_client_order_id = "QH-NVDA-s-intent"
                p.stop_recovery_intended_qty = 2
                m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
                fake = ModuleType("execution.broker")
                fake.get_order_by_client_order_id = Mock(return_value=returned)
                with patch.dict("sys.modules", {"execution.broker": fake}):
                    self.assertIsNone(m._recover_or_submit_stop(
                        p, 2, "sell", p.stop_price
                    ))
                m._dispatcher.submit_gtc_stop.assert_not_called()
                self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_failed_intent_save_rolls_back_id_before_later_retry(self):
        m, p = _manager(strict=(2,), saves=(False, True)), _pos()
        m.broker = Mock()
        m._get_open_orders = Mock(return_value=[])
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=_order(id="live"))
        )
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(return_value=None)
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertIsNone(m._recover_or_submit_stop(
                p, 2, "sell", p.stop_price
            ))
            self.assertIsNone(p.stop_recovery_client_order_id)
            self.assertIsNotNone(m._recover_or_submit_stop(
                p, 2, "sell", p.stop_price
            ))
        self.assertEqual(m._save_state.call_count, 2)
        m._dispatcher.submit_gtc_stop.assert_called_once()

    def test_commit_recovery_final_snapshot_rejects_foreign_reducing_order(self):
        m, p = _manager(strict=(2,), saves=(True, True)), _pos()
        foreign = _order(
            id="swing-stop", qty="1", client_order_id="IN-NVDA-s-foreign"
        )
        m._get_open_orders = Mock(return_value=[foreign])
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=_order(id="must-not-submit"))
        )
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(return_value=None)
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertIsNone(m._recover_or_submit_stop(
                p, 2, "sell", p.stop_price, reject_foreign_reducers=True
            ))
        m._dispatcher.submit_gtc_stop.assert_not_called()
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_commit_recovery_final_snapshot_rejects_unreadable_foreign_side(self):
        m, p = _manager(strict=(2,), saves=(True, True)), _pos()
        foreign = _order(
            id="unknown-order", qty="1", client_order_id="foreign-unknown"
        )
        foreign.side = None
        m._get_open_orders = Mock(return_value=[foreign])
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=_order(id="must-not-submit"))
        )
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(return_value=None)
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertIsNone(m._recover_or_submit_stop(
                p, 2, "sell", p.stop_price, reject_foreign_reducers=True
            ))
        m._dispatcher.submit_gtc_stop.assert_not_called()
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_terminal_zero_fill_intent_rotates_and_submits_new_id(self):
        m, p = _manager(strict=(2,), saves=(True, True)), _pos()
        p.stop_recovery_client_order_id = "QH-NVDA-s-old"
        p.stop_recovery_intended_qty = 2
        m.broker = Mock()
        m._get_open_orders = Mock(return_value=[])
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=_order(id="new-live"))
        )
        terminal = _order(
            id="old", status="canceled", client_order_id="QH-NVDA-s-old"
        )
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(
            side_effect=lambda coid: terminal if coid == "QH-NVDA-s-old" else None
        )
        with patch.dict("sys.modules", {"execution.broker": fake}):
            result = m._recover_or_submit_stop(p, 2, "sell", p.stop_price)
        self.assertEqual(result.id, "new-live")
        self.assertNotEqual(p.stop_recovery_client_order_id, "QH-NVDA-s-old")
        m._dispatcher.submit_gtc_stop.assert_called_once()

    def test_terminal_partial_fill_proves_residual_before_new_stop(self):
        m, p = _manager(strict=(1,), saves=(True, True)), _pos()
        p.stop_recovery_client_order_id = "QH-NVDA-s-old"
        p.stop_recovery_intended_qty = 2
        m.broker = Mock()
        m._get_open_orders = Mock(return_value=[])
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=_order(id="residual", qty="1"))
        )
        terminal = _order(
            id="old", qty="2", filled_qty="1", status="canceled",
            client_order_id="QH-NVDA-s-old",
        )
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(
            side_effect=lambda coid: terminal if coid == "QH-NVDA-s-old" else None
        )
        with patch.dict("sys.modules", {"execution.broker": fake}):
            result = m._recover_or_submit_stop(p, 2, "sell", p.stop_price)
        self.assertEqual(result.id, "residual")
        m._strict_resync_qty.assert_called_once_with(p, 1, 2)
        self.assertEqual(
            m._dispatcher.submit_gtc_stop.call_args.args[2], 1
        )

    def test_terminal_fractional_evidence_never_rotates_or_submits(self):
        for qty, filled in (("2.5", "0"), ("2.5", "1.5")):
            with self.subTest(qty=qty, filled=filled):
                m, p = _manager(strict=(2,), saves=(True,)), _pos()
                p.stop_recovery_client_order_id = "QH-NVDA-s-old"
                p.stop_recovery_intended_qty = 2
                m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
                terminal = _order(
                    id="old", qty=qty, filled_qty=filled, status="canceled",
                    client_order_id="QH-NVDA-s-old",
                )
                fake = ModuleType("execution.broker")
                fake.get_order_by_client_order_id = Mock(return_value=terminal)
                with patch.dict("sys.modules", {"execution.broker": fake}):
                    self.assertIsNone(m._recover_or_submit_stop(
                        p, 2, "sell", p.stop_price
                    ))
                self.assertEqual(
                    p.stop_recovery_client_order_id, "QH-NVDA-s-old"
                )
                m._dispatcher.submit_gtc_stop.assert_not_called()

    def test_filled_status_with_zero_fill_is_never_retired_or_resubmitted(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        p.stop_recovery_client_order_id = "QH-NVDA-s-old"
        p.stop_recovery_intended_qty = 2
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(return_value=_order(
            status="filled", qty="2", filled_qty="0",
            client_order_id="QH-NVDA-s-old",
        ))
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertIsNone(m._recover_or_submit_stop(p, 2, "sell", 200.0))
        self.assertEqual(p.stop_recovery_client_order_id, "QH-NVDA-s-old")
        m._dispatcher.submit_gtc_stop.assert_not_called()

    def test_coheld_registered_stop_fill_never_resubmits_over_other_tier(self):
        m, p = _manager(strict=(None,), saves=(True,)), _pos(
            state=HoldState.PENDING_STOP_REPLACE
        )
        m._positions = {p.symbol: p}
        m.broker = Mock()
        m.broker.get_position.return_value = SimpleNamespace(qty="5", side="long")
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        broker = ModuleType("execution.broker")
        broker.get_order = Mock(return_value=_order(
            id="old-stop", status="filled", qty="2", filled_qty="2"
        ))
        broker.get_order_by_client_order_id = Mock(return_value=None)
        from execution import ownership_guard as ownership
        with patch.dict("sys.modules", {"execution.broker": broker}), patch.object(
            ownership, "apply_authorized_tier_fill_reduction", return_value=True
        ) as reduce_fill:
            self.assertFalse(m.resubmit_stop_if_needed(p.symbol, []))
        reduce_fill.assert_called_once_with(
            "NVDA", "qhm", 2, 5, "qhm_registered_stop:old-stop"
        )
        self.assertEqual(p.state, HoldState.CLOSED)
        m._dispatcher.submit_gtc_stop.assert_not_called()

    def test_full_terminal_stop_fill_save_failure_rolls_back_without_deregister(self):
        m, p = _manager(strict=(None,), saves=(False,)), _pos(
            state=HoldState.PENDING_STOP_REPLACE
        )
        m._positions = {p.symbol: p}
        m.broker = Mock()
        m.broker.get_position.return_value = SimpleNamespace(qty="5", side="long")
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        broker = ModuleType("execution.broker")
        broker.get_order = Mock(return_value=_order(
            id="old-stop", status="filled", qty="2", filled_qty="2"
        ))
        broker.get_order_by_client_order_id = Mock(return_value=None)
        from execution import ownership_guard as ownership
        with patch.dict("sys.modules", {"execution.broker": broker}), patch.object(
            ownership, "apply_authorized_tier_fill_reduction", return_value=True
        ), patch(
            "execution.quarterly_hold_manager._deregister_symbol"
        ) as deregister:
            self.assertFalse(m.resubmit_stop_if_needed(p.symbol, []))
        self.assertEqual(
            (p.qty_filled, p.qty_total, p.stop_order_id, p.state),
            (2, 2, "old-stop", HoldState.PENDING_STOP_REPLACE),
        )
        deregister.assert_not_called()
        m._dispatcher.submit_gtc_stop.assert_not_called()

    def test_fractional_registered_stop_fill_cannot_cover_other_tier_shares(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos(
            state=HoldState.PENDING_STOP_REPLACE
        )
        m._positions = {p.symbol: p}
        m.broker = Mock()
        m.broker.get_position.return_value = SimpleNamespace(qty="2", side="long")
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        broker = ModuleType("execution.broker")
        broker.get_order = Mock(return_value=_order(
            id="old-stop", status="canceled", qty="2", filled_qty="0.5"
        ))
        broker.get_order_by_client_order_id = Mock(return_value=None)
        from execution import ownership_guard as ownership
        with patch.dict("sys.modules", {"execution.broker": broker}), patch.object(
            ownership, "apply_authorized_tier_fill_reduction"
        ) as reduce_fill:
            self.assertFalse(m.resubmit_stop_if_needed(p.symbol, []))
        reduce_fill.assert_not_called()
        m._dispatcher.submit_gtc_stop.assert_not_called()
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_stale_qhm_id_pointing_to_day_stop_cannot_reduce_qhm_ledger(self):
        terminal_cases = (
            {"client_order_id": "DT-NVDA-s-1"},
            {"client_order_id": "QH-AAPL-s-1", "symbol": "AAPL"},
        )
        from execution import ownership_guard as ownership
        for terminal_fields in terminal_cases:
            with self.subTest(terminal_fields=terminal_fields):
                m, p = _manager(strict=(3,), saves=(True,)), _pos(
                    qty_filled=3, qty_total=3, stop_order_id="day-stop",
                    state=HoldState.PENDING_STOP_REPLACE,
                )
                m._positions = {p.symbol: p}
                m.broker = Mock()
                m.broker.get_position.return_value = SimpleNamespace(
                    qty="3", side="long"
                )
                m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
                broker = ModuleType("execution.broker")
                broker.get_order = Mock(return_value=_order(
                    id="day-stop", status="filled", qty="2", filled_qty="2",
                    **terminal_fields,
                ))
                broker.get_order_by_client_order_id = Mock(return_value=None)
                with patch.dict(
                    "sys.modules", {"execution.broker": broker}
                ), patch.object(
                    ownership, "apply_authorized_tier_fill_reduction"
                ) as reduce_fill:
                    self.assertFalse(m.resubmit_stop_if_needed(p.symbol, []))
                reduce_fill.assert_not_called()
                m._dispatcher.submit_gtc_stop.assert_not_called()
                self.assertEqual(
                    (p.qty_filled, p.state), (3, HoldState.PENDING_STOP_REPLACE)
                )

    def test_fractional_special_recovery_fill_never_reaches_owner_proof(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos(
            state=HoldState.PENDING_STOP_REPLACE
        )
        p.stop_recovery_requires_qty_proof = True
        p.stop_recovery_canceled_order_id = "canceled-old"
        p.stop_recovery_baseline_qty = 2
        m._positions = {p.symbol: p}
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        fake = ModuleType("execution.broker")
        fake.get_order = Mock(return_value=_order(
            id="canceled-old", status="canceled", filled_qty="0.5"
        ))
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertFalse(m.resubmit_stop_if_needed(p.symbol, []))
        m._strict_resync_qty.assert_not_called()
        m._dispatcher.submit_gtc_stop.assert_not_called()

    def test_partial_fill_retirement_save_failure_restores_retry_state(self):
        m, p = _manager(
            strict=(1, 1), saves=(False, True, True)
        ), _pos(qty_filled=2, qty_total=2)
        def prove_residual(position, owner_qty, baseline_qty):
            self.assertEqual((owner_qty, baseline_qty), (1, 2))
            position.qty_filled = owner_qty
            position.qty_total = owner_qty
            position.updated_at = "proof-mutated"
            return owner_qty

        m._strict_resync_qty = Mock(side_effect=prove_residual)
        p.stop_recovery_client_order_id = "QH-NVDA-s-old"
        p.stop_recovery_intended_qty = 2
        original_updated = p.updated_at
        m.broker = Mock()
        m._get_open_orders = Mock(return_value=[])
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=_order(id="residual", qty="1"))
        )
        terminal = _order(
            id="old", qty="2", filled_qty="1", status="canceled",
            client_order_id="QH-NVDA-s-old",
        )
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(
            side_effect=lambda coid: terminal if coid == "QH-NVDA-s-old" else None
        )
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertIsNone(m._recover_or_submit_stop(
                p, p.qty_filled, "sell", p.stop_price
            ))
            self.assertEqual((p.qty_filled, p.qty_total), (2, 2))
            self.assertEqual(p.updated_at, original_updated)
            result = m._recover_or_submit_stop(
                p, p.qty_filled, "sell", p.stop_price
            )
        self.assertEqual(result.id, "residual")
        self.assertEqual((p.qty_filled, p.qty_total), (1, 1))
        m._dispatcher.submit_gtc_stop.assert_called_once()

    def test_public_recovery_accepts_preflight_refreshed_residual_qty(self):
        m, p = _manager(strict=(1, 1), saves=(True, True, True)), _pos(
            qty_filled=2, qty_total=2, stop_order_id=None,
            state=HoldState.PENDING_STOP_REPLACE,
        )
        p.stop_recovery_client_order_id = "QH-NVDA-s-old"
        p.stop_recovery_intended_qty = 2
        m._positions = {p.symbol: p}
        m.broker = Mock()
        m._get_open_orders = Mock(return_value=[])

        def prove(position, owner_qty_hint=None, baseline_owner_qty=None):
            if owner_qty_hint is not None:
                self.assertEqual((owner_qty_hint, baseline_owner_qty), (1, 2))
            position.qty_filled = 1
            position.qty_total = 1
            return 1

        m._strict_resync_qty = Mock(side_effect=prove)
        m._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=_order(id="residual", qty="1"))
        )
        terminal = _order(
            id="old", qty="2", filled_qty="1", status="canceled",
            client_order_id="QH-NVDA-s-old",
        )
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(
            side_effect=lambda coid: terminal if coid == "QH-NVDA-s-old" else None
        )
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertTrue(m.resubmit_stop_if_needed(p.symbol, []))
        self.assertEqual((p.qty_filled, p.state), (1, HoldState.ACTIVE))
        self.assertIsNone(p.stop_recovery_client_order_id)
        self.assertIsNone(p.stop_recovery_intended_qty)
        self.assertEqual(m._dispatcher.submit_gtc_stop.call_args.args[2], 1)

    def test_legacy_coid_without_valid_intended_qty_never_submits(self):
        for invalid_qty in (None, 1.5, True, 0):
            with self.subTest(invalid_qty=invalid_qty):
                m, p = _manager(strict=(2,), saves=(True,)), _pos()
                p.stop_recovery_client_order_id = "QH-NVDA-s-legacy"
                p.stop_recovery_intended_qty = invalid_qty
                m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
                fake = ModuleType("execution.broker")
                fake.get_order_by_client_order_id = Mock(return_value=None)
                with patch.dict("sys.modules", {"execution.broker": fake}):
                    self.assertIsNone(m._recover_or_submit_stop(
                        p, 2, "sell", p.stop_price
                    ))
                fake.get_order_by_client_order_id.assert_not_called()
                m._dispatcher.submit_gtc_stop.assert_not_called()
                self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_bulk_recovery_adopts_live_replacement_without_submitting(self):
        m, p = _manager(strict=(2, 2), saves=(True,)), _pos()
        p.state = HoldState.PENDING_STOP_REPLACE
        m._positions = {p.symbol: p}
        m._get_open_orders = Mock(return_value=[_order(id="new-stop")])
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())

        self.assertEqual(m.resubmit_pending_stops(), 1)
        self.assertEqual((p.stop_order_id, p.state), ("new-stop", HoldState.ACTIVE))
        m._dispatcher.submit_gtc_stop.assert_not_called()
        m._get_open_orders.assert_called_once()

    def test_bulk_recovery_blocks_two_qh_stops_without_adopting_or_submitting(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        p.state = HoldState.PENDING_STOP_REPLACE
        m._positions = {p.symbol: p}
        m._get_open_orders = Mock(return_value=[
            _order(id="old-stop"), _order(id="extra-stop")
        ])
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())

        self.assertEqual(m.resubmit_pending_stops(), 0)
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)
        self.assertEqual(p.stop_order_id, "old-stop")
        m._dispatcher.submit_gtc_stop.assert_not_called()

    def test_ordinary_submit_save_failure_can_be_adopted_next_pass(self):
        m, p = _manager(strict=(2, 2, 2), saves=(True, False, True)), _pos()
        p.state = HoldState.PENDING_STOP_REPLACE
        p.stop_order_id = None
        m._positions = {p.symbol: p}
        m.broker = Mock()
        live = _order(id="new-live-stop")
        m._get_open_orders = Mock(side_effect=[[], []])
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock(return_value=live))

        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(return_value=None)
        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertFalse(m.resubmit_stop_if_needed(p.symbol))
        self.assertEqual(p.stop_order_id, "new-live-stop")
        sent_coid = m._dispatcher.submit_gtc_stop.call_args.kwargs[
            "client_order_id"
        ]
        self.assertTrue(sent_coid.startswith("QH-NVDA-s-"))
        self.assertFalse(p.stop_recovery_requires_qty_proof)
        self.assertIsNone(p.stop_recovery_canceled_order_id)
        self.assertIsNone(p.stop_recovery_baseline_qty)
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

        self.assertEqual(
            m._reconcile_stop_coverage(p, [live]), (False, False)
        )
        self.assertEqual(p.state, HoldState.ACTIVE)

    def test_stop_appearing_after_initial_snapshot_blocks_recovery_submit(self):
        m, p = _manager(strict=(2,), saves=(True, True)), _pos()
        p.state = HoldState.PENDING_STOP_REPLACE
        p.stop_order_id = None
        m._positions = {p.symbol: p}
        m.broker = Mock()
        appeared = _order(id="appeared-before-send")
        m._get_open_orders = Mock(return_value=[appeared])
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        fake = ModuleType("execution.broker")
        fake.get_order_by_client_order_id = Mock(return_value=None)

        with patch.dict("sys.modules", {"execution.broker": fake}):
            self.assertFalse(m.resubmit_stop_if_needed(p.symbol, []))
        m._dispatcher.submit_gtc_stop.assert_not_called()
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)
        self.assertIsNotNone(p.stop_recovery_client_order_id)

    def test_special_recovery_blocks_two_live_qh_stops_in_every_caller(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        p.state = HoldState.PENDING_STOP_REPLACE
        p.stop_recovery_requires_qty_proof = True
        p.stop_recovery_canceled_order_id = "canceled-old"
        p.stop_recovery_baseline_qty = 2
        m._positions = {p.symbol: p}
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())

        self.assertFalse(m.resubmit_stop_if_needed(
            p.symbol, [_order(id="stop-a"), _order(id="stop-b")]
        ))
        m._dispatcher.submit_gtc_stop.assert_not_called()
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_missing_filled_qty_is_never_exact_or_replaceable(self):
        malformed = _order()
        delattr(malformed, "filled_qty")
        self.assertIsNone(QuarterlyHoldManager._qh_stop_shape(malformed, _pos()))

    def test_registered_untagged_stop_is_not_id_only_readopted(self):
        m, p = _manager(strict=(2,), saves=(True,)), _pos()
        p.state = HoldState.PENDING_STOP_REPLACE
        m._positions = {p.symbol: p}
        m._get_open_orders = Mock(return_value=[
            _order(id="old-stop", client_order_id="manual-order")
        ])
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        self.assertFalse(m.resubmit_stop_if_needed(p.symbol))
        m._dispatcher.submit_gtc_stop.assert_not_called()
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)

    def test_registered_exact_stop_save_failure_does_not_heal(self):
        m, p = _manager(strict=(2,), saves=(False, False)), _pos()
        p.state = HoldState.PENDING_STOP_REPLACE
        m._positions = {p.symbol: p}
        m._get_open_orders = Mock(return_value=[_order(id="old-stop")])
        m._dispatcher = SimpleNamespace(submit_gtc_stop=Mock())
        self.assertFalse(m.resubmit_stop_if_needed(p.symbol))
        self.assertEqual(p.state, HoldState.PENDING_STOP_REPLACE)
        m._dispatcher.submit_gtc_stop.assert_not_called()

    def test_startup_adopts_replacement_before_stray_guard(self):
        m, p = _manager(strict=(2, 2), saves=(True, True)), _pos()
        m.dry_run = False
        p.stop_order_id = "old-stop"
        m._positions = {p.symbol: p}
        replacement = _order(id="new-stop")
        snapshot = [replacement]
        m._get_open_orders = Mock(return_value=snapshot)
        seen = []
        m.cancel_stray_sell_orders = Mock(
            side_effect=lambda open_orders, skip_symbols: seen.append(
                (p.stop_order_id, open_orders, skip_symbols)
            ) or 0
        )
        result = m.reconcile_on_startup()
        self.assertEqual(p.stop_order_id, "new-stop")
        self.assertEqual(result.orders_verified, 1)
        self.assertEqual(seen[0][0], "new-stop")
        self.assertIs(seen[0][1], snapshot)
