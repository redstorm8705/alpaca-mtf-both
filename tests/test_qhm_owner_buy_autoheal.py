"""QHM owner app/web buy adoption and stop-resize transaction."""

import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import execution
from execution.manual_sale_proof import BuyProof, evaluate_owner_buy
from execution.quarterly_hold_manager import (
    HoldPosition,
    HoldState,
    QuarterlyHoldManager,
)

try:
    import execution.broker  # noqa: F401
except ModuleNotFoundError:
    # The production host intentionally has no test-only Alpaca SDK installed.
    # Give patch() a real module target so this file also runs independently.
    broker_stub = ModuleType("execution.broker")
    for name in (
        "cancel_stop_confirmed", "get_order", "get_order_by_client_order_id",
        "replace_stop_order",
    ):
        setattr(broker_stub, name, Mock())
    sys.modules["execution.broker"] = broker_stub
    execution.broker = broker_stub


SINCE = "2026-10-23T15:00:00+00:00"
OWNER_ID = "11111111-2222-4333-8444-555555555555"
REPLACEMENT_ID = "QH-GE-s-1761234567890-deadbeef"
FINAL_REPLACEMENT_ID = "QH-GE-s-1761234567891-feedface"
FIRST_REPLACEMENT_ID = "QH-GE-s-1761234567892-deadbee1"
SECOND_REPLACEMENT_ID = "QH-GE-s-1761234567893-deadbee2"


def _raw_order(**changes):
    row = {
        "id": "owner-order", "symbol": "GE", "side": "buy", "status": "filled",
        "qty": "1", "filled_qty": "1", "filled_at": "2026-10-23T16:00:00+00:00",
        "source": None, "client_order_id": OWNER_ID,
    }
    row.update(changes)
    return row


def _bot_signal():
    return {
        "id": "bot-stop", "symbol": "GE", "side": "sell", "status": "new",
        "qty": "2", "filled_qty": "0", "filled_at": None,
        "source": "access_key", "client_order_id": "QH-GE-s-1-proof",
    }


def _stop(oid="old-stop", qty="2", price="272.71", coid="QH-GE-s-old-proof"):
    return SimpleNamespace(
        id=oid, symbol="GE", side="sell", type="stop", status="new",
        time_in_force="gtc",
        qty=qty, filled_qty="0", stop_price=price, client_order_id=coid,
    )


def _position(qty="3", avg="302.496667", current="286.00"):
    return SimpleNamespace(
        qty=qty, side="long", avg_entry_price=avg, current_price=current
    )


def _hold(**changes):
    values = dict(
        symbol="GE", direction="long", target_equity_pct=0.2,
        state=HoldState.ACTIVE, qty_total=2, qty_filled=2,
        avg_entry_price=310.38, stop_price=272.71, stop_order_id="old-stop",
        owner_add_baseline_qty=2, owner_add_proven_at=SINCE,
    )
    values.update(changes)
    return HoldPosition(**values)


def _manager(pos):
    manager = object.__new__(QuarterlyHoldManager)
    manager.dry_run = False
    manager.broker = Mock()
    manager.broker.get_position.return_value = _position()
    manager.alerter = None
    manager._positions = {pos.symbol: pos}
    manager._save_state = Mock(return_value=True)
    manager._alert = Mock()
    manager._now_et = Mock(
        return_value=datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    )
    manager._design_stop_price = Mock(return_value=257.12)
    manager._orders_from_iso = Mock(return_value=SINCE)
    return manager


class OwnerBuyProofTests(TestCase):
    def test_terminal_owner_buy_is_proven(self):
        proof = evaluate_owner_buy([_raw_order(), _bot_signal()], 2, 3, SINCE)
        self.assertTrue(proof.proven)
        self.assertEqual(proof.order_qty_by_id, {"owner-order": 1})

    def test_ambiguous_sources_and_mutable_fills_fail_closed(self):
        cases = (
            _raw_order(source="access_key"),
            _raw_order(side="sell"),
            _raw_order(status="partially_filled", qty="2"),
            _raw_order(qty="1.5", filled_qty="1.5"),
            _raw_order(qty=True),
            _raw_order(filled_qty=True),
            {"_truncated": True},
        )
        for row in cases:
            with self.subTest(row=row):
                proof = evaluate_owner_buy([row, _bot_signal()], 2, 3, SINCE)
                self.assertFalse(proof.proven)
        self.assertFalse(evaluate_owner_buy([_raw_order()], 2, 3, SINCE).proven)
        self.assertFalse(evaluate_owner_buy(
            [_raw_order(id=""), _bot_signal()], 2, 3, SINCE
        ).proven)
        self.assertFalse(evaluate_owner_buy(
            [_raw_order(), _raw_order(), _bot_signal()], 2, 4, SINCE
        ).proven)


class QHMOwnerBuyTransactionTests(TestCase):
    def test_unexpected_recovery_exception_keeps_symbol_fenced(self):
        pos = _hold(owner_add_stage={"phase": "STAGED"})
        manager = _manager(pos)
        manager._recover_owner_add_stage = Mock(side_effect=RuntimeError("api hung up"))

        self.assertTrue(manager._maybe_owner_buy_adoption(pos))

        self.assertIsNotNone(pos.owner_add_stage)
        manager._alert.assert_called_once()

    @patch("execution.ownership_guard.apply_authorized_owner_buy_assignment")
    @patch("execution.broker.replace_stop_order")
    def test_restart_in_aborting_phase_only_resumes_containment(
        self, replace, assign
    ):
        pos = _hold(owner_add_stage={
            "phase": "ABORTING", "containment_reason": "foreign reducer",
            "baseline_qty": 2, "expected_qty": 3, "delta_qty": 1,
            "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[_stop()])

        with patch("execution.order_admission.symbol_order_gate") as gate:
            self.assertFalse(manager._recover_owner_add_stage(pos))
        gate.assert_called_once_with("GE")
        replace.assert_not_called()
        assign.assert_not_called()
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(pos.stop_order_id, "old-stop")

    def test_malformed_durable_stage_never_reads_or_mutates_broker(self):
        base = {
            "phase": "STAGED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667,
            "old_stop_id": "old-stop", "old_stop_price": 272.71,
                "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        }
        cases = (
            ("baseline_qty", 1.5), ("baseline_qty", True),
            ("expected_qty", 2.5), ("expected_qty", True),
            ("delta_qty", 1.5), ("delta_qty", True),
            ("broker_avg", float("inf")), ("broker_avg", True),
            ("stop_price", float("nan")), ("stop_price", True),
            ("old_stop_price", "corrupt"),
            ("old_stop_price", float("nan")),
            ("old_stop_price", True),
            ("expected_qty", 4),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                stage = dict(base)
                stage[field] = value
                pos = _hold(owner_add_stage=stage)
                manager = _manager(pos)
                manager._get_open_orders = Mock()

                self.assertFalse(manager._recover_owner_add_stage(pos))
                manager.broker.get_position.assert_not_called()
                manager._get_open_orders.assert_not_called()
                self.assertIsNotNone(pos.owner_add_stage)

        missing_old_stop = dict(base)
        missing_old_stop.pop("old_stop_price")
        pos = _hold(owner_add_stage=missing_old_stop)
        manager = _manager(pos)
        manager._get_open_orders = Mock()
        self.assertFalse(manager._recover_owner_add_stage(pos))
        manager.broker.get_position.assert_not_called()
        manager._get_open_orders.assert_not_called()
        self.assertIsNotNone(pos.owner_add_stage)

        bad_maps = (
            {"owner-order": True},
            {"owner-order": 1.5},
            {"owner-order": float("nan")},
            {"owner-order": float("inf")},
            {"": 1},
            {1: 1},
            {" owner-order": 1},
            {"owner-order": 2},
        )
        for order_map in bad_maps:
            with self.subTest(order_map=order_map):
                stage = dict(base)
                stage["order_qty_by_id"] = order_map
                pos = _hold(owner_add_stage=stage)
                manager = _manager(pos)
                manager._get_open_orders = Mock()

                self.assertFalse(manager._recover_owner_add_stage(pos))
                manager.broker.get_position.assert_not_called()
                manager._get_open_orders.assert_not_called()
                self.assertIsNotNone(pos.owner_add_stage)

        bad_fields = (
            ("phase", "UNKNOWN"),
            ("replacement_client_order_id", True),
            ("replacement_client_order_id", 123),
            ("replacement_client_order_id", " QH-GE-s-1761234567890-deadbeef"),
            ("replacement_client_order_id", "QH-NVDA-s-1761234567890-deadbeef"),
            ("replacement_client_order_id", "QH-GE-b-1761234567890-deadbeef"),
            ("replacement_client_order_id", "QH-GE-s-123-deadbeef"),
            ("replacement_client_order_id", "QH-GE-s-1761234567890-nothexzz"),
            ("old_stop_id", True),
            ("old_stop_id", 123),
            ("old_stop_id", " old-stop"),
        )
        for field, value in bad_fields:
            with self.subTest(field=field, value=value):
                stage = dict(base)
                stage[field] = value
                pos = _hold(owner_add_stage=stage)
                manager = _manager(pos)
                manager._get_open_orders = Mock()

                self.assertFalse(manager._recover_owner_add_stage(pos))
                manager.broker.get_position.assert_not_called()
                manager._get_open_orders.assert_not_called()
                self.assertIsNotNone(pos.owner_add_stage)

    @patch("execution.manual_sale_proof.prove_owner_buy")
    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.replace_stop_order")
    def test_happy_path_persists_stage_before_atomic_resize(
        self, replace, assign, prove
    ):
        pos = _hold()
        manager = _manager(pos)
        new = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        old = _stop()
        manager._get_open_orders = Mock(side_effect=[[old], [old], [new]])
        prove.return_value = BuyProof(
            True, "owner", 1, ["owner-order"], {"owner-order": 1}
        )

        def _replace(*args, **kwargs):
            self.assertIsNotNone(pos.owner_add_stage)
            self.assertEqual(pos.owner_add_stage["phase"], "PATCH_ATTEMPTED")
            new.client_order_id = kwargs["client_order_id"]
            return new

        replace.side_effect = _replace
        self.assertTrue(manager._maybe_owner_buy_adoption(pos))
        self.assertEqual((pos.qty_filled, pos.qty_total), (3, 3))
        self.assertAlmostEqual(pos.avg_entry_price, 302.496667)
        self.assertEqual((pos.stop_order_id, pos.stop_price), ("new-stop", 272.71))
        self.assertIsNone(pos.owner_add_stage)
        assign.assert_called_once()
        manager._alert.assert_called_once()

    @patch("execution.manual_sale_proof.prove_owner_buy")
    @patch("execution.broker.replace_stop_order")
    def test_failed_initial_stage_save_never_mutates_broker(
        self, replace, prove
    ):
        pos = _hold()
        manager = _manager(pos)
        manager._save_state.return_value = False
        manager._get_open_orders = Mock(return_value=[_stop()])
        prove.return_value = BuyProof(
            True, "owner", 1, ["owner-order"], {"owner-order": 1}
        )

        self.assertFalse(manager._maybe_owner_buy_adoption(pos))

        replace.assert_not_called()
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual((pos.qty_filled, pos.qty_total), (2, 2))

    @patch("execution.manual_sale_proof.prove_owner_buy")
    @patch("execution.broker.replace_stop_order")
    def test_nonfinite_designed_stop_never_stages_or_replaces(self, replace, prove):
        pos = _hold()
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[_stop()])
        manager._design_stop_price.return_value = float("nan")
        prove.return_value = BuyProof(
            True, "owner", 1, ["owner-order"], {"owner-order": 1}
        )

        self.assertFalse(manager._maybe_owner_buy_adoption(pos))

        self.assertIsNone(pos.owner_add_stage)
        replace.assert_not_called()

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    def test_restart_adopts_exact_replacement_without_second_patch(self, assign):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[
            _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        ])
        with patch("execution.broker.replace_stop_order") as replace:
            self.assertTrue(manager._recover_owner_add_stage(pos))
        replace.assert_not_called()
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(pos.qty_filled, 3)

    def test_foreign_order_prevents_mutation(self):
        pos = _hold()
        manager = _manager(pos)
        foreign = SimpleNamespace(
            id="foreign", symbol="GE", side="sell", type="stop", status="new",
            qty="1", filled_qty="0", stop_price="250", client_order_id="IN-GE-s-1-x",
        )
        manager._get_open_orders = Mock(return_value=[_stop(), foreign])
        with patch("execution.manual_sale_proof.prove_owner_buy") as prove, \
             patch("execution.broker.replace_stop_order") as replace:
            prove.return_value = BuyProof(
                True, "owner", 1, ["owner-order"], {"owner-order": 1}
            )
            self.assertFalse(manager._maybe_owner_buy_adoption(pos))
        replace.assert_not_called()
        self.assertIsNone(pos.owner_add_stage)

    @patch("execution.broker.get_order", return_value=None)
    @patch("execution.broker.get_order_by_client_order_id", return_value=None)
    def test_foreign_live_order_is_contained_before_terminal_fill_accounting(
        self, by_coid, get_order
    ):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = _position("2")
        foreign = SimpleNamespace(
            id="foreign", symbol="GE", side="sell", type="stop", status="new",
            qty="1", filled_qty="0", stop_price="250", client_order_id="IN-GE-s-1-x",
        )
        manager._get_open_orders = Mock(return_value=[foreign])
        manager._contain_owner_add_stage = Mock(return_value=True)
        manager._owner_add_terminal_fill = Mock(return_value=True)

        self.assertFalse(manager._recover_owner_add_stage(pos))
        manager._contain_owner_add_stage.assert_called_once()
        manager._owner_add_terminal_fill.assert_not_called()

    def test_containment_refuses_after_ownership_commit_phase_started(self):
        pos = _hold(owner_add_stage={
            "phase": "OWNERSHIP_COMMITTING", "baseline_qty": 2,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[_stop()])

        self.assertFalse(manager._contain_owner_add_stage(pos, 3, "foreign"))
        self.assertEqual(pos.owner_add_stage["phase"], "OWNERSHIP_COMMITTING")
        self.assertEqual(pos.stop_order_id, "old-stop")

    @patch("execution.broker.cancel_stop_confirmed")
    def test_containment_validates_durable_stop_price_before_cancel(self, cancel):
        pos = _hold(owner_add_stage={
            "phase": "ABORTING", "baseline_qty": 2,
            "old_stop_price": "corrupt",
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[_stop()])

        self.assertFalse(manager._contain_owner_add_stage(pos, 3, "changed"))

        cancel.assert_not_called()
        manager._get_open_orders.assert_not_called()
        manager.broker.get_position.assert_not_called()
        self.assertEqual(pos.stop_order_id, "old-stop")
        self.assertIsNotNone(pos.owner_add_stage)

    def test_containment_rechecks_position_before_retaining_baseline_stop(self):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[_stop()])
        manager.broker.get_position.side_effect = [
            _position("3"), SimpleNamespace(
                qty="1", side="short", avg_entry_price="300", current_price="300"
            ),
        ]

        self.assertFalse(manager._contain_owner_add_stage(pos, 3, "changed"))
        self.assertIsNotNone(pos.owner_add_stage)
        self.assertEqual(pos.stop_order_id, "old-stop")

    @patch("execution.broker.cancel_stop_confirmed", return_value=True)
    def test_post_cancel_reprotection_requires_stable_position_sandwich(
        self, cancel
    ):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2,
            "order_qty_by_id": {"owner-order": 1},
            "old_stop_price": 272.71,
        })
        manager = _manager(pos)
        foreign = SimpleNamespace(
            id="foreign", symbol="GE", side="sell", type="stop", status="new",
            qty="2", filled_qty="0", stop_price="250", client_order_id="IN-GE-s-x",
        )
        manager._get_open_orders = Mock(
            side_effect=[[_stop(), foreign], [foreign], []]
        )
        manager.broker.get_position.side_effect = [
            _position("3"), _position("3"), _position("3"), _position("1"),
        ]
        manager._recover_or_submit_stop = Mock()

        self.assertFalse(manager._contain_owner_add_stage(pos, 3, "changed"))
        cancel.assert_called_once_with("GE", "old-stop")
        manager._recover_or_submit_stop.assert_not_called()
        self.assertIsNotNone(pos.owner_add_stage)

    @patch("execution.manual_sale_proof.prove_owner_buy")
    def test_locked_reread_rejects_nan_price_and_changed_proof(self, prove):
        for second_proof in (
            BuyProof(True, "owner", 1, ["owner-order"], {"owner-order": 1}),
            BuyProof(False, "API order appeared", 0, [], {}),
        ):
            with self.subTest(second_proof=second_proof):
                prove.reset_mock()
                pos = _hold()
                manager = _manager(pos)
                first = BuyProof(
                    True, "owner", 1, ["owner-order"], {"owner-order": 1}
                )
                prove.side_effect = [first, second_proof]
                locked = _position()
                if second_proof.proven:
                    locked.current_price = float("nan")
                manager.broker.get_position.side_effect = [_position(), locked]
                manager._get_open_orders = Mock(return_value=[_stop()])
                self.assertFalse(manager._maybe_owner_buy_adoption(pos))
                self.assertIsNone(pos.owner_add_stage)
                self.assertEqual(prove.call_count, 1 if second_proof.proven else 2)

    def test_unreadable_foreign_sell_cancels_only_qhm_descendant(self):
        pos = _hold(owner_add_stage={
            "phase": "STAGED", "baseline_qty": 2,
            "order_qty_by_id": {"owner-order": 1},
            "old_stop_price": 272.71,
        })
        manager = _manager(pos)
        foreign = SimpleNamespace(
            id="foreign", symbol="GE", side="sell", type="stop", status="new",
            qty="bad", filled_qty="0", stop_price="250", client_order_id="IN-GE-s-1-x",
        )
        manager._get_open_orders = Mock(
            side_effect=[[_stop(), foreign], [foreign], []]
        )
        manager._recover_or_submit_stop = Mock(
            return_value=_stop("recovered", "2")
        )
        with patch(
            "execution.broker.cancel_stop_confirmed", return_value=True
        ) as cancel:
            self.assertTrue(manager._contain_owner_add_stage(pos, 3, "changed"))
        cancel.assert_called_once_with("GE", "old-stop")
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(pos.state, HoldState.ACTIVE)
        self.assertEqual(pos.owner_add_blocked_order_ids, ["owner-order"])

    def test_unreadable_foreign_side_cannot_clear_containment(self):
        pos = _hold(owner_add_stage={
            "phase": "STAGED", "baseline_qty": 2,
            "order_qty_by_id": {"owner-order": 1},
            "old_stop_price": 272.71,
        })
        manager = _manager(pos)
        foreign = SimpleNamespace(
            id="foreign", symbol="GE", side=None, type="stop", status="new",
            qty="2", filled_qty="0", stop_price="250",
            client_order_id="IN-GE-s-1-x",
        )
        manager._get_open_orders = Mock(
            side_effect=[[_stop(), foreign], [foreign], [foreign]]
        )
        manager.broker.get_position.side_effect = [
            _position("3"), _position("3"), _position("3"), _position("3"),
        ]
        manager._recover_or_submit_stop = Mock()

        with patch(
            "execution.broker.cancel_stop_confirmed", return_value=True
        ) as cancel:
            self.assertFalse(manager._contain_owner_add_stage(pos, 3, "changed"))
        cancel.assert_called_once_with("GE", "old-stop")
        manager._recover_or_submit_stop.assert_not_called()
        self.assertIsNotNone(pos.owner_add_stage)

    @patch("execution.broker.replace_stop_order")
    @patch("execution.broker.get_order", return_value=None)
    @patch("execution.broker.get_order_by_client_order_id", return_value=None)
    def test_unknown_postpatch_position_preserves_new_stop(
        self, by_coid, get_order, replace
    ):
        pos = _hold(owner_add_stage={
            "phase": "STAGED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        old = _stop()
        new = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        manager._get_open_orders = Mock(side_effect=[[old], [new]])
        manager.broker.get_position.side_effect = [
            _position(), RuntimeError("api down")
        ]
        replace.return_value = new
        with patch("execution.broker.cancel_stop_confirmed") as cancel:
            self.assertFalse(manager._recover_owner_add_stage(pos))
        cancel.assert_not_called()
        self.assertEqual(pos.owner_add_stage["phase"], "PATCH_ATTEMPTED")

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id")
    @patch("execution.broker.get_order")
    def test_terminal_replacement_fill_commits_and_closes(
        self, get_order, by_coid, assign
    ):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = None
        manager._get_open_orders = Mock(return_value=[])
        terminal = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        terminal.status = "filled"
        terminal.filled_qty = "3"
        old = _stop("old-stop", "2", "272.71", "QH-GE-s-old-proof")
        old.status = "replaced"
        by_coid.return_value = terminal
        get_order.return_value = old
        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertEqual((pos.qty_filled, pos.state), (0, HoldState.CLOSED))
        self.assertEqual(pos.closed_reason, "qhm_stop_out")
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(assign.call_args.kwargs["terminal_stop_fill_qty"], 3)

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id")
    @patch("execution.broker.get_order")
    def test_combined_old_and_replacement_fills_are_summed(
        self, get_order, by_coid, assign
    ):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = None
        manager._get_open_orders = Mock(return_value=[])
        old = _stop("old-stop", "2", "272.71", "QH-GE-s-old-proof")
        old.status = "replaced"
        old.filled_qty = "1"
        replacement = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        replacement.status = "canceled"
        replacement.filled_qty = "2"
        get_order.return_value = old
        by_coid.return_value = replacement

        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertEqual((pos.qty_filled, pos.state), (0, HoldState.CLOSED))
        self.assertEqual(assign.call_args.kwargs["terminal_stop_fill_qty"], 3)
        self.assertEqual(
            assign.call_args.kwargs["terminal_stop_id"], "new-stop+old-stop"
        )

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id", return_value=None)
    @patch("execution.broker.get_order")
    def test_expanded_recovered_stop_full_fill_commits_residual(
        self, get_order, by_coid, assign
    ):
        pos = _hold(owner_add_stage={
            "phase": "STAGED", "baseline_qty": 2, "stop_baseline_qty": 3,
            "ownership_projection_started": True, "committed_qty": 3,
            "expected_qty": 4, "delta_qty": 2, "broker_avg": 299.25,
            "old_stop_id": "recovered-stop", "old_stop_price": 272.71,
            "stop_price": 272.71,
            "replacement_client_order_id": FINAL_REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1, "owner-order-2": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = _position("1", "299.25")
        manager._get_open_orders = Mock(return_value=[])
        filled = _stop(
            "recovered-stop", "3", "272.71", "QH-GE-s-recovery-proof"
        )
        filled.status = "filled"
        filled.filled_qty = "3"
        get_order.return_value = filled
        residual = _stop("residual-stop", "1", "272.71", "QH-GE-s-residual")
        manager._recover_or_submit_stop = Mock(return_value=residual)

        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertEqual((pos.qty_filled, pos.qty_total), (1, 1))
        self.assertEqual(pos.stop_order_id, "residual-stop")
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(assign.call_args.kwargs["terminal_stop_fill_qty"], 3)

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id", return_value=None)
    @patch("execution.broker.get_order")
    def test_expanded_recovered_stop_partial_fill_commits_residual(
        self, get_order, by_coid, assign
    ):
        pos = _hold(owner_add_stage={
            "phase": "STAGED", "baseline_qty": 2, "stop_baseline_qty": 3,
            "ownership_projection_started": True, "committed_qty": 3,
            "expected_qty": 4, "delta_qty": 2, "broker_avg": 299.25,
            "old_stop_id": "recovered-stop", "old_stop_price": 272.71,
            "stop_price": 272.71,
            "replacement_client_order_id": FINAL_REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1, "owner-order-2": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = _position("2", "299.25")
        manager._get_open_orders = Mock(return_value=[])
        partial = _stop(
            "recovered-stop", "3", "272.71", "QH-GE-s-recovery-proof"
        )
        partial.status = "canceled"
        partial.filled_qty = "2"
        get_order.return_value = partial
        residual = _stop("residual-stop", "2", "272.71", "QH-GE-s-residual")
        manager._recover_or_submit_stop = Mock(return_value=residual)

        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertEqual((pos.qty_filled, pos.qty_total), (2, 2))
        self.assertEqual(pos.stop_order_id, "residual-stop")
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(assign.call_args.kwargs["terminal_stop_fill_qty"], 2)

    @patch("execution.broker.get_order_by_client_order_id", return_value=None)
    @patch("execution.broker.get_order")
    def test_replaced_ancestor_requires_readable_matching_descendant(
        self, get_order, by_coid
    ):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        old = _stop("old-stop", "2", "272.71", "QH-GE-s-old-proof")
        old.status = "replaced"
        old.filled_qty = "1"
        old.replaced_by = "new-stop"
        get_order.side_effect = [old, None]

        self.assertFalse(manager._owner_add_terminal_fill(pos, 2, _position("2")))
        self.assertIsNotNone(pos.owner_add_stage)

    @patch("execution.broker.get_order_by_client_order_id")
    def test_live_replacement_omitted_from_list_never_restores_second_stop(
        self, by_coid
    ):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = _position("2")
        manager._get_open_orders = Mock(return_value=[])
        live_replacement = _stop(
            "new-stop", "3", "272.71", REPLACEMENT_ID
        )
        live_replacement.status = "new"
        by_coid.return_value = live_replacement
        manager._contain_owner_add_stage = Mock(return_value=True)

        self.assertFalse(manager._recover_owner_add_stage(pos))
        manager._contain_owner_add_stage.assert_not_called()
        self.assertIsNotNone(pos.owner_add_stage)

    @patch("execution.broker.get_order_by_client_order_id")
    def test_unknown_nonterminal_replacement_status_preserves_stage(self, by_coid):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[])
        pending = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        pending.status = "pending_new"
        by_coid.return_value = pending
        manager._contain_owner_add_stage = Mock(return_value=True)

        self.assertFalse(manager._recover_owner_add_stage(pos))
        manager._contain_owner_add_stage.assert_not_called()
        self.assertIsNotNone(pos.owner_add_stage)

    @patch("execution.broker.get_order")
    @patch("execution.broker.get_order_by_client_order_id")
    def test_replaced_direct_order_waits_for_fresh_book_without_chain_walk(
        self, by_coid, get_order
    ):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager._get_open_orders = Mock(return_value=[])
        replaced = _stop("R1", "3", "272.71", REPLACEMENT_ID)
        replaced.status = "replaced"
        replaced.replaced_by = "R2"
        by_coid.return_value = replaced
        manager._contain_owner_add_stage = Mock(return_value=True)

        self.assertFalse(manager._recover_owner_add_stage(pos))
        get_order.assert_not_called()
        manager._contain_owner_add_stage.assert_not_called()
        self.assertIsNotNone(pos.owner_add_stage)

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id")
    def test_commit_phase_recovers_canceled_zero_fill_replacement(
        self, by_coid, assign
    ):
        pos = _hold(owner_add_stage={
            "phase": "OWNERSHIP_COMMITTING", "baseline_qty": 2,
            "expected_qty": 3, "delta_qty": 1, "broker_avg": 302.496667,
            "old_stop_id": "old-stop", "old_stop_price": 272.71,
                "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = _position("3")
        manager._get_open_orders = Mock(return_value=[])
        terminal = _stop(
            "new-stop", "3", "272.71", REPLACEMENT_ID
        )
        terminal.status = "canceled"
        terminal.filled_qty = "0"
        by_coid.return_value = terminal
        recovered = _stop(
            "recovered-stop", "3", "272.71", "QH-GE-s-recovery-proof"
        )
        manager._recover_or_submit_stop = Mock(return_value=recovered)

        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(pos.stop_order_id, "recovered-stop")
        self.assertEqual((pos.qty_filled, pos.qty_total), (3, 3))
        self.assertGreaterEqual(assign.call_count, 2)
        manager._recover_or_submit_stop.assert_called_once_with(
            pos, 3, "sell", 272.71, reject_foreign_reducers=True
        )

    @patch("execution.manual_sale_proof.prove_owner_buy")
    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id")
    def test_owner_buy_fill_during_commit_expands_stage_for_next_stop_resize(
        self, by_coid, assign, prove
    ):
        pos = _hold(owner_add_stage={
            "phase": "OWNERSHIP_COMMITTING", "baseline_qty": 2,
            "expected_qty": 3, "delta_qty": 1, "broker_avg": 302.496667,
            "old_stop_id": "old-stop", "old_stop_price": 272.71,
                "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
            "proof_since": SINCE, "created_at": SINCE,
        })
        manager = _manager(pos)
        manager.broker.get_position.side_effect = [
            _position("3"), _position("3"), _position("4", "299.25"),
        ]
        owner_buy = SimpleNamespace(
            id="future-owner-buy", symbol="GE", side="buy", type="limit",
            status="new", qty="1", filled_qty="0", client_order_id=OWNER_ID,
        )
        manager._get_open_orders = Mock(return_value=[owner_buy])
        terminal = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        terminal.status = "canceled"
        terminal.filled_qty = "0"
        by_coid.return_value = terminal
        recovered = _stop(
            "recovered-stop", "3", "272.71", "QH-GE-s-recovery-proof"
        )
        manager._recover_or_submit_stop = Mock(return_value=recovered)
        prove.return_value = BuyProof(
            True, "owner", 2, ["owner-order", "owner-order-2"],
            {"owner-order": 1, "owner-order-2": 1},
        )

        self.assertFalse(manager._recover_owner_add_stage(pos))
        stage = pos.owner_add_stage
        self.assertEqual(stage["phase"], "STAGED")
        self.assertEqual((stage["expected_qty"], stage["delta_qty"]), (4, 2))
        self.assertEqual(stage["old_stop_id"], "recovered-stop")
        self.assertEqual(stage["stop_baseline_qty"], 3)
        self.assertTrue(stage["ownership_projection_started"])
        self.assertEqual(
            stage["order_qty_by_id"],
            {"owner-order": 1, "owner-order-2": 1},
        )
        self.assertEqual(pos.stop_order_id, "recovered-stop")
        self.assertEqual(pos.state, HoldState.PENDING_STOP_REPLACE)
        self.assertIsNone(pos.stop_recovery_client_order_id)
        self.assertGreaterEqual(assign.call_count, 1)

        stop3 = _stop("recovered-stop", "3", "272.71", "QH-GE-s-recovery-proof")
        stop4 = _stop("final-stop", "4", "272.71", FINAL_REPLACEMENT_ID)
        manager.broker.get_position.side_effect = None
        manager.broker.get_position.return_value = _position("4", "299.25")
        manager._get_open_orders = Mock(side_effect=[[stop3], [stop4]])

        def _replace(*args, **kwargs):
            stop4.client_order_id = kwargs["client_order_id"]
            return stop4

        with patch("execution.broker.replace_stop_order", side_effect=_replace):
            self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual((pos.qty_filled, pos.qty_total), (4, 4))
        self.assertEqual(pos.stop_order_id, "final-stop")
        self.assertEqual(pos.state, HoldState.ACTIVE)

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id")
    def test_commit_phase_recovers_stop_with_nonreducing_foreign_buy(
        self, by_coid, assign
    ):
        pos = _hold(owner_add_stage={
            "phase": "OWNERSHIP_COMMITTING", "baseline_qty": 2,
            "expected_qty": 3, "delta_qty": 1, "broker_avg": 302.496667,
            "old_stop_id": "old-stop", "old_stop_price": 272.71,
                "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        owner_buy = SimpleNamespace(
            id="future-owner-buy", symbol="GE", side="buy", type="limit",
            status="new", qty="1", filled_qty="0", client_order_id=OWNER_ID,
        )
        manager._get_open_orders = Mock(return_value=[owner_buy])
        terminal = _stop(
            "new-stop", "3", "272.71", REPLACEMENT_ID
        )
        terminal.status = "canceled"
        terminal.filled_qty = "0"
        by_coid.return_value = terminal
        recovered = _stop(
            "recovered-stop", "3", "272.71", "QH-GE-s-recovery-proof"
        )
        manager._recover_or_submit_stop = Mock(return_value=recovered)

        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertEqual(pos.stop_order_id, "recovered-stop")
        self.assertIsNone(pos.owner_add_stage)

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id")
    def test_commit_phase_adopts_live_recovery_stop_after_restart(
        self, by_coid, assign
    ):
        pos = _hold(
            stop_recovery_client_order_id="QH-GE-s-recovery",
            owner_add_stage={
                "phase": "OWNERSHIP_COMMITTING", "baseline_qty": 2,
                "expected_qty": 3, "delta_qty": 1, "broker_avg": 302.496667,
                "old_stop_id": "old-stop", "old_stop_price": 272.71,
                "stop_price": 272.71,
                "replacement_client_order_id": REPLACEMENT_ID,
                "order_qty_by_id": {"owner-order": 1},
            },
        )
        manager = _manager(pos)
        recovery = _stop(
            "recovery-stop", "3", "272.71", "QH-GE-s-recovery"
        )
        manager._get_open_orders = Mock(return_value=[recovery])
        canceled = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        canceled.status = "canceled"
        by_coid.return_value = canceled

        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertEqual(pos.stop_order_id, "recovery-stop")
        self.assertIsNone(pos.owner_add_stage)
        assign.assert_called_once()

    @patch(
        "execution.ownership_guard.apply_authorized_owner_buy_assignment",
        return_value=True,
    )
    @patch("execution.broker.get_order_by_client_order_id")
    @patch("execution.broker.get_order")
    def test_terminal_recovery_stop_fill_is_included_after_restart(
        self, get_order, by_coid, assign
    ):
        pos = _hold(
            stop_recovery_client_order_id="QH-GE-s-recovery",
            owner_add_stage={
                "phase": "OWNERSHIP_COMMITTING", "baseline_qty": 2,
                "expected_qty": 3, "delta_qty": 1, "broker_avg": 302.496667,
                "old_stop_id": "old-stop", "old_stop_price": 272.71,
                "stop_price": 272.71,
                "replacement_client_order_id": REPLACEMENT_ID,
                "order_qty_by_id": {"owner-order": 1},
            },
        )
        manager = _manager(pos)
        manager.broker.get_position.return_value = None
        manager._get_open_orders = Mock(return_value=[])
        old = _stop("old-stop", "2", "272.71", "QH-GE-s-old-proof")
        old.status = "replaced"
        old.filled_qty = "0"
        get_order.return_value = old
        canceled = _stop("new-stop", "3", "272.71", REPLACEMENT_ID)
        canceled.status = "canceled"
        canceled.filled_qty = "0"
        filled = _stop("recovery-stop", "3", "272.71", "QH-GE-s-recovery")
        filled.status = "filled"
        filled.filled_qty = "3"
        by_coid.side_effect = lambda coid: (
            filled if coid == "QH-GE-s-recovery" else canceled
        )

        self.assertTrue(manager._recover_owner_add_stage(pos))
        self.assertEqual(pos.state, HoldState.CLOSED)
        self.assertEqual(assign.call_args.kwargs["terminal_stop_fill_qty"], 3)
        self.assertIn("recovery-stop", assign.call_args.kwargs["terminal_stop_id"])

    @patch("execution.broker.cancel_stop_confirmed", return_value=True)
    def test_ambiguous_replacement_abandons_adoption_and_blocks_replay(self, cancel):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": FIRST_REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        first = _stop("first", "3", "272.71", FIRST_REPLACEMENT_ID)
        second = _stop("second", "3", "272.71", SECOND_REPLACEMENT_ID)
        manager._get_open_orders = Mock(
            side_effect=[[first, second], [first, second], [], []]
        )
        manager._recover_or_submit_stop = Mock(
            return_value=_stop("recovered", "2")
        )
        self.assertFalse(manager._recover_owner_add_stage(pos))
        self.assertIsNone(pos.owner_add_stage)
        self.assertEqual(pos.state, HoldState.ACTIVE)
        self.assertEqual(pos.stop_order_id, "recovered")
        self.assertEqual(pos.owner_add_blocked_order_ids, ["owner-order"])
        self.assertEqual(cancel.call_count, 2)

    @patch("execution.broker.cancel_stop_confirmed", return_value=True)
    def test_short_position_never_finishes_or_submits_sell_protection(self, cancel):
        pos = _hold(owner_add_stage={
            "phase": "PATCH_ATTEMPTED", "baseline_qty": 2, "expected_qty": 3,
            "delta_qty": 1, "broker_avg": 302.496667, "old_stop_id": "old-stop",
            "old_stop_price": 272.71, "stop_price": 272.71,
            "replacement_client_order_id": REPLACEMENT_ID,
            "order_qty_by_id": {"owner-order": 1},
        })
        manager = _manager(pos)
        manager.broker.get_position.return_value = _position(qty="3")
        manager.broker.get_position.return_value.side = "short"
        manager._get_open_orders = Mock(side_effect=[[_stop()], [], []])
        manager._recover_or_submit_stop = Mock()
        self.assertFalse(manager._recover_owner_add_stage(pos))
        cancel.assert_not_called()
        self.assertIsNotNone(pos.owner_add_stage)
        manager._recover_or_submit_stop.assert_not_called()
        self.assertEqual(pos.owner_add_stage["phase"], "ABORTING")
        self.assertEqual(pos.state, HoldState.ACTIVE)

    def test_day_stop_is_not_accepted_as_qhm_protection(self):
        day = _stop()
        day.time_in_force = "day"
        self.assertIsNone(QuarterlyHoldManager._qh_stop_shape(day, _hold()))

    def test_staged_owner_add_fences_active_tranche_entry(self):
        pos = _hold(
            tranches_filled=1, tranche=2, owner_add_stage={"phase": "STAGED"}
        )
        manager = _manager(pos)
        manager._tranche_cycle_attempts = 0
        manager._add_tranche_active = Mock(return_value=True)
        self.assertEqual(manager.maybe_enter_positions(), [])
        manager._add_tranche_active.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()
