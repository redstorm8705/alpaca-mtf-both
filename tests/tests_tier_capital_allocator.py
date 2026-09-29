#!/usr/bin/env python3
"""Focused mechanical tests for the live tier-capital allocator."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from execution import tier_capital_allocator as capital_module
from execution.quarterly_hold_manager import (
    HoldPosition,
    HoldState,
    QuarterlyHoldManager,
)
from execution.tier_capital_allocator import EntryRequest, TierCapitalAllocator


class _Broker:
    def __init__(
        self,
        *,
        equity=2500.0,
        cash=2500.0,
        bp=10000.0,
        maintenance=0.0,
        positions=None,
        orders=None,
    ):
        self.account = SimpleNamespace(
            equity=equity, cash=cash, buying_power=bp, maintenance_margin=maintenance
        )
        self.positions = positions or []
        self.orders = orders or []

    def get_account(self):
        return self.account

    def get_open_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders

    def get_order(self, order_id):
        return getattr(self, "historical_order", None)

    def get_order_by_client_order_id(self, client_order_id):
        return getattr(self, "recovered_order", None)


def _ledger(**tiers):
    out = {}
    for sym, values in tiers.items():
        out[sym] = {
            "tiers": {
                "intraday": {"qty": values.get("intraday", 0)},
                "daytrade": {"qty": values.get("daytrade", 0)},
                "qhm": {"qty": values.get("qhm", 0)},
                "forever6": {"qty": values.get("forever6", 0)},
            }
        }
    return {"positions": out}


class AllocatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state.json"
        self.audit = root / "audit.jsonl"
        self.now = 1000.0
        self.broker = _Broker()
        self.ledger = _ledger()
        self.alloc = TierCapitalAllocator(
            self.broker,
            state_path=self.state,
            audit_path=self.audit,
            ledger_loader=lambda: self.ledger,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def req(self, tier="daytrade", **kw):
        defaults = dict(
            tier=tier,
            owner_tier="intraday" if tier == "swing" else tier,
            symbol="NVDA",
            side="buy",
            qty=2,
            price_bound=100.0,
            stop_price=95.0,
            regime_evidence={"regime": "normal", "verified": True},
        )
        defaults.update(kw)
        return EntryRequest(**defaults)

    def test_reserves_before_submit_and_binds_afterward(self):
        decision = self.alloc.admit_entry(self.req())
        self.assertTrue(decision.approved)
        lease = decision.lease
        assert lease
        raw = json.loads(self.state.read_text())
        self.assertEqual(raw["reservations"][lease.id]["status"], "reserved")
        self.assertEqual(
            raw["reservations"][lease.id]["client_order_id"], lease.client_order_id
        )
        self.assertTrue(self.alloc.bind_submitted(lease.id, "abc"))
        raw = json.loads(self.state.read_text())
        self.assertEqual(raw["reservations"][lease.id]["broker_order_id"], "abc")
        self.assertEqual(raw["reservations"][lease.id]["status"], "submitted")
        self.assertTrue(self.audit.exists())

    def test_daytrade_tier_cap_uses_existing_ledger_position(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="NVDA", qty=11, side="long", current_price=100, market_value=1100
            )
        ]
        self.ledger = _ledger(NVDA={"daytrade": 11})
        # Normal Day Trade cap is $1,250; 11 shares plus 2 requested breaches it.
        decision = self.alloc.admit_entry(self.req())
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "tier capital cap")

    def test_swing_maps_to_intraday_ledger_owner(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="AAPL", qty=5, side="long", current_price=100, market_value=500
            )
        ]
        self.ledger = _ledger(AAPL={"intraday": 5})
        # Swing target is 20% of equity = $500; next $200 must deny.
        decision = self.alloc.admit_entry(self.req("swing", symbol="AAPL"))
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "tier capital cap")

    def test_qhm_cash_only_and_cash_floor(self):
        denied = self.alloc.admit_entry(self.req("qhm", side="sell_short"))
        self.assertFalse(denied.approved)
        self.assertIn("cash-only", denied.reason)
        self.broker.account.cash = 300.0
        self.alloc.policy.targets["normal"]["qhm"] = 1.0
        denied = self.alloc.admit_entry(self.req("qhm"))
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "settled cash floor")

    def test_qhm_grandfathered_book_does_not_block_new_money(self):
        """Pre-cap QHM holdings stay outside the approved new-money envelope."""
        self.broker.account.equity = 2532.18
        self.broker.account.cash = 5000.0
        self.broker.account.buying_power = 10000.0
        self.broker.positions = [
            SimpleNamespace(
                symbol="LLY", qty=2, side="long", current_price=1183.50,
                market_value=2367.0,
            ),
            SimpleNamespace(
                symbol="GEV", qty=1, side="long", current_price=953.0,
                market_value=953.0,
            ),
            SimpleNamespace(
                symbol="NVDA", qty=1, side="long", current_price=231.0,
                market_value=231.0,
            ),
        ]
        self.ledger = _ledger(
            LLY={"qhm": 2}, GEV={"qhm": 1}, NVDA={"qhm": 1}
        )
        grandfathered = 2367.0 + 953.0
        decision = self.alloc.admit_entry(
            self.req(
                "qhm",
                qty=1,
                price_bound=231.0,
                stop_price=220.0,
                exempt_existing_notional=grandfathered,
            )
        )
        self.assertTrue(decision.approved, decision.reason)

    def test_existing_notional_exemption_is_qhm_only_and_bounded(self):
        denied = self.alloc.admit_entry(
            self.req("daytrade", exempt_existing_notional=1.0)
        )
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "invalid existing-notional exemption")

        self.broker.positions = [
            SimpleNamespace(
                symbol="NVDA", qty=1, side="long", current_price=100.0,
                market_value=100.0,
            )
        ]
        self.ledger = _ledger(NVDA={"qhm": 1})
        denied = self.alloc.admit_entry(
            self.req("qhm", qty=1, exempt_existing_notional=101.0)
        )
        self.assertFalse(denied.approved)
        self.assertEqual(
            denied.reason, "existing-notional exemption exceeds ownership"
        )

    def test_f6_uses_slippage_buffer_for_cash_reserve(self):
        decision = self.alloc.admit_entry(self.req("forever6", qty=1, price_bound=100))
        self.assertTrue(decision.approved)
        assert decision.lease
        self.assertEqual(decision.lease.cash_reserve, 101.0)

    def test_account_gross_includes_pending_reversal_excess(self):
        # Existing long 5 shares; sell order 15 only adds 10 shares to gross.
        self.broker.positions = [
            SimpleNamespace(
                symbol="MSFT", qty=5, side="long", current_price=100, market_value=500
            )
        ]
        self.broker.orders = [
            SimpleNamespace(
                id="stop",
                status="accepted",
                symbol="MSFT",
                side="sell",
                qty=15,
                filled_qty=0,
                limit_price=None,
                stop_price=100,
            )
        ]
        self.ledger = _ledger(MSFT={"intraday": 5})
        # Existing $500 + pending $1,000 + requested $2,000 breaches $3,125.
        self.alloc.policy.targets["normal"]["daytrade"] = 1.0
        denied = self.alloc.admit_entry(self.req(qty=20, price_bound=100))
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "account gross cap")

    def test_open_bound_order_still_consumes_its_tier_envelope(self):
        first = self.alloc.admit_entry(self.req(qty=10, price_bound=100, stop_price=95))
        self.assertTrue(first.approved)
        assert first.lease
        self.assertTrue(self.alloc.bind_submitted(first.lease.id, "open-1"))
        self.broker.orders = [
            SimpleNamespace(
                id="open-1",
                status="accepted",
                symbol="NVDA",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=100,
                stop_price=None,
            )
        ]
        second = self.alloc.admit_entry(
            self.req(qty=10, price_bound=100, stop_price=95)
        )
        self.assertFalse(second.approved)
        self.assertEqual(second.reason, "tier capital cap")

    def test_pending_cancel_order_still_consumes_account_gross(self):
        self.broker.orders = [
            SimpleNamespace(
                id="canceling",
                status="pending_cancel",
                symbol="NVDA",
                side="buy",
                qty=30,
                filled_qty=0,
                limit_price=100,
                stop_price=None,
            )
        ]
        denied = self.alloc.admit_entry(self.req(qty=2, price_bound=100, stop_price=95))
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "account gross cap")

    def test_bad_broker_position_fails_closed(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="NVDA",
                qty=1,
                side="long",
                current_price=float("nan"),
                market_value=100,
            )
        ]
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertIn("broker snapshot unreadable", denied.reason)

    def test_nan_market_value_fails_closed(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="NVDA",
                qty=1,
                side="long",
                current_price=100,
                market_value=float("nan"),
            )
        ]
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertIn("broker snapshot unreadable", denied.reason)

    def test_account_gross_uses_qty_times_price_not_inconsistent_market_value(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="MSFT", qty=30, side="long", current_price=100, market_value=1
            )
        ]
        self.ledger = _ledger(MSFT={"qhm": 30})
        denied = self.alloc.admit_entry(self.req(qty=2, price_bound=100, stop_price=95))
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "account gross cap")

    def test_unknown_position_side_fails_closed(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="MSFT",
                qty=1,
                side="mystery",
                current_price=100,
                market_value=100,
            )
        ]
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertIn("broker snapshot unreadable", denied.reason)

    def test_negative_qty_short_reconciles_and_counts_gross(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="MSFT",
                qty=-2,
                side="short",
                current_price=100,
                market_value=-200,
            )
        ]
        self.ledger = _ledger(MSFT={"intraday": -2})
        decision = self.alloc.admit_entry(self.req(qty=1))
        self.assertTrue(decision.approved)

    def test_negative_qty_long_fails_closed(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="MSFT",
                qty=-1,
                side="long",
                current_price=100,
                market_value=-100,
            )
        ]
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertIn("broker snapshot unreadable", denied.reason)

    def test_positive_qty_short_fails_closed(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="MSFT",
                qty=1,
                side="short",
                current_price=100,
                market_value=-100,
            )
        ]
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertIn("broker snapshot unreadable", denied.reason)

    def test_missing_maintenance_margin_fails_closed(self):
        self.broker.account.maintenance_margin = None
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertIn("broker snapshot unreadable", denied.reason)

    def test_negative_margin_cash_blocks_cash_tier_but_not_margin_tier(self):
        self.broker.account.cash = -50
        margin_decision = self.alloc.admit_entry(self.req(qty=1))
        self.assertTrue(margin_decision.approved)
        assert margin_decision.lease
        self.alloc.release(margin_decision.lease.id, "test_cleanup")
        cash_decision = self.alloc.admit_entry(self.req("qhm", qty=1))
        self.assertFalse(cash_decision.approved)
        self.assertEqual(cash_decision.reason, "settled cash floor")

    def test_malformed_nan_reservation_state_fails_closed(self):
        self.state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "reservations": {
                        "bad": {
                            "status": "reserved",
                            "tier": "daytrade",
                            "symbol": "NVDA",
                            "side": "buy",
                            "client_order_id": "DT-NVDA-bad",
                            "qty": 1,
                            "price_bound": 100,
                            "notional": "NaN",
                            "cash_reserve": 0,
                            "maintenance_reserve": 100,
                            "worst_loss": 5,
                            "created_at": 1,
                            "expires_at": 2,
                            "baseline_tier_qty": 0,
                        }
                    },
                }
            )
        )
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "allocator state unreadable")

    def test_negative_reservation_cannot_increase_available_capital(self):
        self.state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "reservations": {
                        "bad": {
                            "status": "reserved",
                            "tier": "qhm",
                            "symbol": "NVDA",
                            "side": "buy",
                            "client_order_id": "QH-NVDA-bad",
                            "qty": 1,
                            "price_bound": 100,
                            "notional": 100,
                            "cash_reserve": -10000,
                            "maintenance_reserve": -10000,
                            "worst_loss": 5,
                            "created_at": 1,
                            "expires_at": 2,
                            "baseline_tier_qty": 0,
                        }
                    },
                }
            )
        )
        denied = self.alloc.admit_entry(self.req("qhm"))
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "allocator state unreadable")

    def test_inconsistent_reservation_fields_fail_closed(self):
        self.state.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "reservations": {
                        "bad": {
                            "status": "reserved",
                            "tier": "qhm",
                            "symbol": "NVDA",
                            "side": "buy",
                            "client_order_id": "QH-NVDA-bad",
                            "qty": 1000,
                            "price_bound": 100,
                            "notional": 1,
                            "cash_reserve": 0,
                            "maintenance_reserve": 1,
                            "worst_loss": 1,
                            "created_at": 1,
                            "expires_at": 2,
                            "baseline_tier_qty": 0,
                        }
                    },
                }
            )
        )
        denied = self.alloc.admit_entry(self.req("qhm"))
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "allocator state unreadable")

    def test_broker_ledger_quantity_drift_fails_closed(self):
        self.broker.positions = [
            SimpleNamespace(
                symbol="NVDA", qty=2, side="long", current_price=100, market_value=200
            )
        ]
        self.ledger = _ledger(NVDA={"daytrade": 1})
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertIn("drifted", denied.reason)

    def test_flat_historical_ledger_symbol_does_not_create_false_drift(self):
        self.ledger = _ledger(OLD={})
        decision = self.alloc.admit_entry(self.req(qty=1))
        self.assertTrue(decision.approved)

    def test_offsetting_nonzero_owner_claims_are_not_treated_as_flat(self):
        self.ledger = _ledger(OLD={"intraday": 10, "daytrade": -10})
        decision = self.alloc.admit_entry(self.req(qty=1))
        self.assertFalse(decision.approved)
        self.assertIn("drifted", decision.reason)

    def test_daytrade_envelope_can_use_bounded_margin(self):
        # Normal Day Trade receives 40% of the 1.25x gross envelope = $1,250.
        decision = self.alloc.admit_entry(
            self.req(qty=11, price_bound=100, stop_price=95)
        )
        self.assertTrue(decision.approved)

    def test_short_records_separate_lower_fill_bound_for_loss_metadata(self):
        decision = self.alloc.admit_entry(
            self.req(
                "swing",
                side="sell",
                qty=1,
                price_bound=101,
                risk_price_bound=99,
                stop_price=105,
            )
        )
        self.assertTrue(decision.approved)
        assert decision.lease
        self.assertEqual(decision.lease.notional, 101)
        self.assertEqual(decision.lease.worst_loss, 6)

    def test_unreadable_ledger_fails_closed(self):
        self.alloc.ledger_loader = lambda: (_ for _ in ()).throw(RuntimeError("bad"))
        denied = self.alloc.admit_entry(self.req())
        self.assertFalse(denied.approved)
        self.assertEqual(denied.reason, "tier ownership ledger unreadable")

    def test_submitted_terminal_zero_fill_releases_only_in_reconcile(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.assertTrue(self.alloc.bind_submitted(decision.lease.id, "o1"))
        self.broker.orders = [
            SimpleNamespace(
                id="o1",
                status="canceled",
                symbol="NVDA",
                side="buy",
                qty=2,
                filled_qty=0,
                limit_price=100,
                stop_price=None,
            )
        ]
        self.assertTrue(self.alloc.reconcile())
        raw = json.loads(self.state.read_text())
        self.assertEqual(raw["reservations"][decision.lease.id]["status"], "released")

    def test_filled_order_releases_lease_after_position_accounts_for_it(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.assertTrue(self.alloc.bind_submitted(decision.lease.id, "filled-1"))
        self.broker.historical_order = SimpleNamespace(
            id="filled-1", status="filled", filled_qty=2
        )
        self.broker.positions = [
            SimpleNamespace(
                symbol="NVDA", qty=2, side="long", current_price=100, market_value=200
            )
        ]
        self.ledger = _ledger(NVDA={"daytrade": 2})
        self.assertTrue(self.alloc.reconcile())
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "released")
        self.assertEqual(row["release_reason"], "broker_filled_position_accounted")

    def test_unknown_missing_order_retains_submitted_lease(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.assertTrue(self.alloc.bind_submitted(decision.lease.id, "unknown-1"))
        self.assertTrue(self.alloc.reconcile())
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "submitted")

    def test_partial_terminal_fill_waits_for_broker_and_owner_accounting(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.assertTrue(self.alloc.bind_submitted(decision.lease.id, "partial-1"))
        self.broker.historical_order = SimpleNamespace(
            id="partial-1", status="canceled", filled_qty=1
        )
        self.assertTrue(self.alloc.reconcile())
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "submitted")

        self.broker.positions = [
            SimpleNamespace(
                symbol="NVDA", qty=1, side="long", current_price=100, market_value=100
            )
        ]
        self.ledger = _ledger(NVDA={"daytrade": 1})
        self.assertTrue(self.alloc.reconcile())
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "released")

    def test_unbound_crash_lease_is_not_expired_or_laundered(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.now += 10000
        self.assertTrue(self.alloc.reconcile())
        raw = json.loads(self.state.read_text())
        self.assertEqual(raw["reservations"][decision.lease.id]["status"], "reserved")

    def test_timeout_but_accepted_binds_exact_recovered_order(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.broker.recovered_order = SimpleNamespace(
            id="accepted-1",
            status="accepted",
            filled_qty=0,
        )
        previous = capital_module._LIVE_ALLOCATOR
        capital_module._LIVE_ALLOCATOR = self.alloc
        try:
            recovered = capital_module.live_release(decision.lease, "submit_none")
        finally:
            capital_module._LIVE_ALLOCATOR = previous
        self.assertIs(recovered, self.broker.recovered_order)
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["broker_order_id"], "accepted-1")

    def test_verified_rejection_releases_ambiguous_lease(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.broker.recovered_order = SimpleNamespace(
            id="rejected-1",
            status="rejected",
            filled_qty=0,
        )
        previous = capital_module._LIVE_ALLOCATOR
        capital_module._LIVE_ALLOCATOR = self.alloc
        try:
            capital_module.live_release(decision.lease, "submit_none")
        finally:
            capital_module._LIVE_ALLOCATOR = previous
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "released")

    def test_terminal_partial_fill_is_returned_for_strategy_protection(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        self.broker.recovered_order = SimpleNamespace(
            id="partial-canceled", status="canceled", filled_qty=1
        )
        previous = capital_module._LIVE_ALLOCATOR
        capital_module._LIVE_ALLOCATOR = self.alloc
        try:
            recovered = capital_module.live_release(decision.lease, "submit_none")
        finally:
            capital_module._LIVE_ALLOCATOR = previous
        self.assertIs(recovered, self.broker.recovered_order)
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["broker_order_id"], "partial-canceled")

    def test_unknown_ambiguous_submit_retains_reservation(self):
        decision = self.alloc.admit_entry(self.req())
        assert decision.lease
        previous = capital_module._LIVE_ALLOCATOR
        capital_module._LIVE_ALLOCATOR = self.alloc
        try:
            capital_module.live_release(decision.lease, "submit_none")
        finally:
            capital_module._LIVE_ALLOCATOR = previous
        row = json.loads(self.state.read_text())["reservations"][decision.lease.id]
        self.assertEqual(row["status"], "reserved")

    def test_duplicate_client_id_recovers_original_order(self):
        # A duplicate-id broker response is operationally the same ambiguity as a
        # timeout: exact lookup binds the original order and prevents a second entry.
        self.test_timeout_but_accepted_binds_exact_recovered_order()

    def test_all_increasing_order_callers_use_live_allocator(self):
        root = Path(__file__).resolve().parent.parent
        expectations = {
            "execution/day_trade_manager.py": 1,
            "execution/entry_logic.py": 2,
            "execution/quarterly_hold_manager.py": 2,
            "execution/forever_hold_manager.py": 1,
        }
        for filename, count in expectations.items():
            text = (root / filename).read_text()
            self.assertGreaterEqual(text.count("live_admit("), count, filename)
        qhm = (root / "execution/quarterly_hold_manager.py").read_text()
        # One implementation, used by both dip-add and ACTIVE tranche adds.
        self.assertEqual(qhm.count("_stop_safe_add("), 3)
        self.assertIn(
            'live_release(_capital.lease, "stop_filled_before_add_submit")', qhm
        )
        for filename in (
            "execution/day_trade_manager.py",
            "execution/entry_logic.py",
            "execution/forever_hold_manager.py",
            "execution/quarterly_hold_manager.py",
        ):
            text = (root / filename).read_text()
            self.assertIn(
                '= live_release(_capital.lease, "submit_none")', text, filename
            )
        self.assertIn("ambiguous_add_client_order_id", qhm)
        self.assertIn("_get_order_by_client_id(_add_coid)", qhm)
        self.assertIn("_restore_or_pending(_protect_qty", qhm)
        self.assertIn("pos.state = HoldState.PENDING_STOP_REPLACE", qhm)
        self.assertIn("if _amb_status not in _amb_terminal", qhm)
        for live_status in ("open", "held", "pending_replace"):
            self.assertIn(live_status, qhm)
        swing = (root / "execution/entry_logic.py").read_text()
        self.assertIn("_capital_price_bound = entry_price * 1.01", swing)
        self.assertIn(
            '_execution_limit = entry_price * (1.01 if side == "buy" else 0.99)', swing
        )
        self.assertIn("risk_price_bound=_execution_limit", swing)
        f6 = (root / "execution/forever_hold_manager.py").read_text()
        self.assertIn('"buy", 1, px_reserve', f6)

    def test_qhm_recovery_metadata_must_persist_before_stop_cancel(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        manager._save_state = Mock(return_value=False)
        pos = HoldPosition("NVDA", "long", 0.2, state=HoldState.ACTIVE)
        self.assertFalse(manager._arm_ambiguous_add_recovery(pos, "QH-NVDA-test"))
        self.assertEqual(pos.state, HoldState.ACTIVE)
        self.assertIsNone(pos.ambiguous_add_client_order_id)

    def test_qhm_unreadable_qty_retains_provisional_stop_and_ambiguity(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager._positions = {}
        manager._strict_resync_qty = Mock(return_value=None)
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=True)
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-1",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        recovered = SimpleNamespace(id="add-1", status="filled")
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=recovered)
        fake_broker.get_order = Mock(side_effect=[
            SimpleNamespace(status="accepted", filled_qty=0),
            SimpleNamespace(status="canceled", filled_qty=0),
        ])
        fake_broker.cancel_order = Mock()
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertFalse(manager.resubmit_stop_if_needed(pos.symbol))
        fake_broker.cancel_order.assert_not_called()
        self.assertEqual(pos.stop_order_id, "stop-1")
        self.assertEqual(pos.ambiguous_add_client_order_id, "QH-NVDA-test")
        self.assertEqual(pos.state, HoldState.PENDING_STOP_REPLACE)

    def test_qhm_stop_fill_during_cancel_uses_post_cancel_qty(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager._positions = {}
        manager._strict_resync_qty = Mock(side_effect=[5, 0])
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=True)
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-1",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        recovered = SimpleNamespace(id="add-1", status="filled")
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=recovered)
        fake_broker.get_order = Mock(side_effect=[
            SimpleNamespace(status="accepted", filled_qty=0),
            SimpleNamespace(status="canceled", filled_qty=0),
        ])
        fake_broker.cancel_order = Mock(return_value=True)
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertFalse(manager.resubmit_stop_if_needed(pos.symbol))
        fake_broker.cancel_order.assert_called_once_with("stop-1")
        self.assertEqual(manager._strict_resync_qty.call_count, 2)
        # The mocked strict reader owns mutation in production.
        self.assertEqual(pos.qty_filled, 5)
        self.assertIsNone(pos.ambiguous_add_client_order_id)

    def test_qhm_final_state_save_failure_keeps_recovery_armed(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager._positions = {}
        manager._strict_resync_qty = Mock(side_effect=[5, 5])
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=False)
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        manager._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=SimpleNamespace(id="stop-2"))
        )
        manager.broker = SimpleNamespace()
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-1",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        recovered = SimpleNamespace(id="add-1", status="filled")
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=recovered)
        fake_broker.get_order = Mock(return_value=SimpleNamespace(
            status="canceled", filled_qty=0
        ))
        fake_broker.cancel_order = Mock(return_value=True)
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertFalse(manager.resubmit_stop_if_needed(pos.symbol))
        self.assertEqual(pos.stop_order_id, "stop-2")
        self.assertEqual(pos.ambiguous_add_client_order_id, "QH-NVDA-test")
        self.assertEqual(pos.state, HoldState.PENDING_STOP_REPLACE)

    def test_qhm_strict_qty_rejects_short_broker_truth(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager.broker = SimpleNamespace(
            get_position=Mock(return_value=SimpleNamespace(
                qty=-5, side="short", avg_entry_price=100.0
            ))
        )
        pos = HoldPosition("NVDA", "long", 0.2, qty_filled=5)
        self.assertIsNone(manager._strict_resync_qty(pos))
        self.assertEqual(pos.qty_filled, 5)

    def test_qhm_post_cancel_read_failure_attempts_conservative_stop(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager.broker = SimpleNamespace()
        manager._positions = {}
        manager._strict_resync_qty = Mock(side_effect=[5, None])
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=True)
        manager._alert = Mock()
        manager._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=SimpleNamespace(id="stop-fallback"))
        )
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-1",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        recovered = SimpleNamespace(id="add-1", status="filled")
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=recovered)
        fake_broker.get_order = Mock(return_value=SimpleNamespace(
            status="canceled", filled_qty=0
        ))
        fake_broker.cancel_order = Mock(return_value=True)
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertFalse(manager.resubmit_stop_if_needed(pos.symbol))
        manager._dispatcher.submit_gtc_stop.assert_called_once()
        self.assertEqual(pos.stop_order_id, "stop-fallback")
        self.assertEqual(pos.ambiguous_add_client_order_id, "QH-NVDA-test")
        manager._alert.assert_called_once()

    def test_qhm_restart_re_adopts_live_final_stop(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager._positions = {}
        manager._strict_resync_qty = Mock(return_value=5)
        covering = SimpleNamespace(id="stop-2")
        manager._live_covering_qhm_stop = Mock(return_value=covering)
        manager._save_state = Mock(return_value=True)
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stale-stop",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        recovered = SimpleNamespace(id="add-1", status="filled")
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=recovered)
        fake_broker.get_order = Mock(return_value=SimpleNamespace(
            status="canceled", filled_qty=0
        ))
        fake_broker.cancel_order = Mock()
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertTrue(manager.resubmit_stop_if_needed(pos.symbol))
        fake_broker.cancel_order.assert_not_called()
        self.assertEqual(pos.stop_order_id, "stop-2")
        self.assertIsNone(pos.ambiguous_add_client_order_id)
        self.assertEqual(pos.state, HoldState.ACTIVE)

    def test_qhm_final_stop_failure_pages_and_retains_recovery(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager.broker = SimpleNamespace()
        manager._positions = {}
        manager._strict_resync_qty = Mock(side_effect=[5, 5])
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=True)
        manager._alert = Mock()
        manager._dispatcher = SimpleNamespace(submit_gtc_stop=Mock(return_value=None))
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-1",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        recovered = SimpleNamespace(id="add-1", status="filled")
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=recovered)
        fake_broker.get_order = Mock(return_value=SimpleNamespace(
            status="canceled", filled_qty=0
        ))
        fake_broker.cancel_order = Mock(return_value=True)
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertFalse(manager.resubmit_stop_if_needed(pos.symbol))
        manager._alert.assert_called_once()
        self.assertEqual(pos.ambiguous_add_client_order_id, "QH-NVDA-test")
        self.assertEqual(pos.state, HoldState.PENDING_STOP_REPLACE)

    def test_qhm_covering_stop_requires_exact_symbol(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager._get_open_orders = Mock(return_value=[SimpleNamespace(
            id="stop-msft", symbol="MSFT", side="sell", type="stop",
            status="new", client_order_id="QH-MSFT-test", qty=10,
        )])
        pos = HoldPosition("NVDA", "long", 0.2, qty_filled=5)
        self.assertIsNone(manager._live_covering_qhm_stop(pos, 5))

    def test_qhm_covering_stop_uses_stable_remaining_quantity(self):
        manager = object.__new__(QuarterlyHoldManager)
        pos = HoldPosition("NVDA", "long", 0.2, qty_filled=5)
        base = dict(
            symbol="NVDA", side="sell", type="stop",
            client_order_id="QH-NVDA-test", qty=10,
        )
        unstable = (
            ("partially_filled", 0), ("pending_cancel", 0), ("", 0),
            ("new", 7), ("new", 2),
        )
        for status, filled in unstable:
            manager._get_open_orders = Mock(return_value=[SimpleNamespace(
                id="stop-1", status=status, filled_qty=filled, **base,
            )])
            self.assertIsNone(manager._live_covering_qhm_stop(pos, 5))
        manager._get_open_orders = Mock(return_value=[SimpleNamespace(
            id="stop-2", symbol="NVDA", side="sell", type="stop",
            client_order_id="QH-NVDA-test", qty=5,
            status="new", filled_qty=0,
        )])
        self.assertEqual(manager._live_covering_qhm_stop(pos, 5).id, "stop-2")

    def test_qhm_strict_qty_uses_owner_ledger_not_broker_net(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        manager.broker = SimpleNamespace(get_position=Mock(return_value=SimpleNamespace(
            qty=10, side="long", avg_entry_price=100.0,
        )))
        pos = HoldPosition("NVDA", "long", 0.2, qty_filled=5)
        fake_ownership = ModuleType("execution.ownership_guard")
        fake_ownership.load_ledger = Mock(return_value={})
        fake_ownership.tier_qty = Mock(return_value=5)
        with patch.dict(sys.modules, {"execution.ownership_guard": fake_ownership}):
            self.assertEqual(manager._strict_resync_qty(pos), 5)
            self.assertIsNone(manager._strict_resync_qty(pos, 7))
            self.assertEqual(manager._strict_resync_qty(pos, 7, 5), 7)
        self.assertEqual(pos.qty_filled, 7)

    def test_qhm_ambiguous_cancel_uses_terminal_late_fill(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager._positions = {}
        manager._strict_resync_qty = Mock(return_value=7)
        manager._live_covering_qhm_stop = Mock(
            return_value=SimpleNamespace(id="stop-7")
        )
        manager._save_state = Mock(return_value=True)
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-5",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        initial = SimpleNamespace(id="add-1", status="new", filled_qty=0)
        terminal = SimpleNamespace(id="add-1", status="canceled", filled_qty=2)
        stop_before = SimpleNamespace(id="stop-5", status="accepted", filled_qty=0)
        stop_terminal = SimpleNamespace(id="stop-5", status="canceled", filled_qty=0)
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=initial)
        fake_broker.get_order = Mock(
            side_effect=[terminal, stop_before, stop_terminal]
        )
        fake_broker.cancel_order = Mock(return_value=True)
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertTrue(manager.resubmit_stop_if_needed(pos.symbol))
        manager._strict_resync_qty.assert_called_once_with(pos, 7, 5)
        self.assertEqual(pos.stop_order_id, "stop-7")

    def test_qhm_provisional_stop_fill_reduces_final_owner_stop(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager.broker = SimpleNamespace()
        manager._positions = {}
        manager._strict_resync_qty = Mock(side_effect=[7, 4])
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=True)
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        manager._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=SimpleNamespace(id="stop-4"))
        )
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-5",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        add = SimpleNamespace(id="add-1", status="filled", filled_qty=2)
        stop_open = SimpleNamespace(id="stop-5", status="accepted", filled_qty=0)
        stop_done = SimpleNamespace(id="stop-5", status="canceled", filled_qty=3)
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=add)
        fake_broker.get_order = Mock(side_effect=[stop_open, stop_done])
        fake_broker.cancel_order = Mock(return_value=True)
        with patch.dict(sys.modules, {"execution.broker": fake_broker}):
            self.assertTrue(manager.resubmit_stop_if_needed(pos.symbol))
        self.assertEqual(
            manager._strict_resync_qty.call_args_list[-1].args[1:], (4, 2)
        )
        submitted = manager._dispatcher.submit_gtc_stop.call_args.args
        self.assertEqual(submitted[2], 4)
        self.assertEqual(pos.stop_order_id, "stop-4")
        self.assertEqual(pos.state, HoldState.ACTIVE)

    def test_qhm_preexisting_provisional_stop_fill_is_subtracted(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager.broker = SimpleNamespace(get_position=Mock(return_value=SimpleNamespace(
            qty=10, side="long", avg_entry_price=100.0,
        )))
        manager._positions = {}
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=True)
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        manager._dispatcher = SimpleNamespace(
            submit_gtc_stop=Mock(return_value=SimpleNamespace(id="stop-final-5"))
        )
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-provisional",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        add = SimpleNamespace(id="add-1", status="filled", filled_qty=2)
        stop_before = SimpleNamespace(
            id="stop-provisional", status="partially_filled", filled_qty=2
        )
        stop_terminal = SimpleNamespace(
            id="stop-provisional", status="canceled", filled_qty=2
        )
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=add)
        fake_broker.get_order = Mock(side_effect=[stop_before, stop_terminal])
        fake_broker.cancel_order = Mock(return_value=True)
        fake_ownership = ModuleType("execution.ownership_guard")
        fake_ownership.load_ledger = Mock(return_value={})
        fake_ownership.tier_qty = Mock(return_value=5)
        with patch.dict(sys.modules, {
            "execution.broker": fake_broker,
            "execution.ownership_guard": fake_ownership,
        }):
            self.assertTrue(manager.resubmit_stop_if_needed(pos.symbol))
        submitted = manager._dispatcher.submit_gtc_stop.call_args.args
        self.assertEqual(submitted[2], 5)
        self.assertEqual(pos.stop_order_id, "stop-final-5")

    def test_qhm_failed_final_stop_retry_keeps_adjusted_baseline(self):
        manager = object.__new__(QuarterlyHoldManager)
        manager.dry_run = False
        manager.broker = SimpleNamespace(get_position=Mock(return_value=SimpleNamespace(
            qty=10, side="long", avg_entry_price=100.0,
        )))
        manager._positions = {}
        manager._live_covering_qhm_stop = Mock(return_value=None)
        manager._save_state = Mock(return_value=True)
        manager._alert = Mock()
        manager._now_et = Mock(return_value=datetime.now(timezone.utc))
        manager._dispatcher = SimpleNamespace(submit_gtc_stop=Mock(side_effect=[
            None, SimpleNamespace(id="stop-retry-5")
        ]))
        pos = HoldPosition(
            "NVDA", "long", 0.2, state=HoldState.PENDING_STOP_REPLACE,
            qty_filled=5, stop_price=90.0, stop_order_id="stop-provisional",
            ambiguous_add_client_order_id="QH-NVDA-test",
            ambiguous_add_baseline_qty=5,
        )
        manager._positions[pos.symbol] = pos
        add = SimpleNamespace(id="add-1", status="filled", filled_qty=2)
        stop_before = SimpleNamespace(
            id="stop-provisional", status="partially_filled", filled_qty=2
        )
        stop_terminal = SimpleNamespace(
            id="stop-provisional", status="canceled", filled_qty=2
        )
        fake_broker = ModuleType("execution.broker")
        fake_broker.get_order_by_client_order_id = Mock(return_value=add)
        fake_broker.get_order = Mock(side_effect=[stop_before, stop_terminal])
        fake_broker.cancel_order = Mock(return_value=True)
        fake_ownership = ModuleType("execution.ownership_guard")
        fake_ownership.load_ledger = Mock(return_value={})
        fake_ownership.tier_qty = Mock(return_value=5)
        modules = {
            "execution.broker": fake_broker,
            "execution.ownership_guard": fake_ownership,
        }
        with patch.dict(sys.modules, modules):
            self.assertFalse(manager.resubmit_stop_if_needed(pos.symbol))
            self.assertEqual(pos.ambiguous_add_baseline_qty, 3)
            self.assertTrue(manager.resubmit_stop_if_needed(pos.symbol))
        submitted = manager._dispatcher.submit_gtc_stop.call_args.args
        self.assertEqual(submitted[2], 5)
        self.assertEqual(pos.stop_order_id, "stop-retry-5")

    def test_exit_api_is_absent(self):
        self.assertFalse(hasattr(self.alloc, "admit_exit"))
        self.assertFalse(hasattr(self.alloc, "flatten"))
        self.assertFalse(hasattr(self.alloc, "cancel_stop"))

    def test_real_regime_ledger_schema_maps_to_dynamic_policy(self):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "vol": {
                "fresh": True,
                "regime": "low_vol",
                "realized_vol": 11.0,
                "composite": "BULL",
                "vix_term_ratio": 0.90,
                "spy_vs_50sma_pct": 4.0,
            },
            "summary": {"vol_regime": "low_vol", "any_stale": False},
        }
        with patch.object(Path, "read_text", return_value=json.dumps(payload)):
            evidence = capital_module._load_regime_evidence()
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["regime"], "risk_on")

    def test_fresh_high_vol_stays_stressed_when_other_components_are_stale(self):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "vol": {
                "fresh": True,
                "regime": "high_vol",
                "realized_vol": 31.0,
                "composite": "HIGH_VOL",
                "vix_term_ratio": 1.2,
                "spy_vs_50sma_pct": -3.0,
            },
            "summary": {"vol_regime": "high_vol", "any_stale": True},
        }
        with patch.object(Path, "read_text", return_value=json.dumps(payload)):
            evidence = capital_module._load_regime_evidence()
        self.assertEqual(evidence["regime"], "stressed")

    def test_high_vol_maps_stressed_when_composite_fields_are_missing(self):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "vol": {
                "fresh": True,
                "regime": "high_vol",
                "realized_vol": 29.0,
                "composite": None,
                "vix_term_ratio": None,
                "spy_vs_50sma_pct": None,
            },
            "summary": {"vol_regime": "high_vol", "any_stale": False},
        }
        with patch.object(Path, "read_text", return_value=json.dumps(payload)):
            evidence = capital_module._load_regime_evidence()
        self.assertEqual(evidence["regime"], "stressed")

    def test_any_stale_canonical_state_cannot_expand_allocation(self):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "vol": {
                "fresh": True,
                "regime": "low_vol",
                "realized_vol": 11.0,
                "composite": "BULL",
                "vix_term_ratio": 0.90,
                "spy_vs_50sma_pct": 4.0,
            },
            "summary": {"vol_regime": "low_vol", "any_stale": True},
        }
        with patch.object(Path, "read_text", return_value=json.dumps(payload)):
            evidence = capital_module._load_regime_evidence()
        self.assertEqual(evidence["regime"], "normal")

    def test_missing_staleness_field_cannot_authorize_risk_on(self):
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "vol": {
                "fresh": True,
                "regime": "low_vol",
                "realized_vol": 11.0,
                "composite": "BULL",
                "vix_term_ratio": 0.90,
                "spy_vs_50sma_pct": 4.0,
            },
            "summary": {"vol_regime": "low_vol"},
        }
        with patch.object(Path, "read_text", return_value=json.dumps(payload)):
            evidence = capital_module._load_regime_evidence()
        self.assertEqual(evidence["regime"], "normal")

    def test_f6_expands_only_in_verified_crash_policy(self):
        self.assertEqual(self.alloc.policy.targets["normal"]["forever6"], 0.10)
        self.assertEqual(self.alloc.policy.targets["risk_on"]["forever6"], 0.10)
        self.assertEqual(self.alloc.policy.targets["stressed"]["forever6"], 0.10)
        self.assertGreater(self.alloc.policy.targets["crash"]["forever6"], 0.10)


if __name__ == "__main__":
    unittest.main()
