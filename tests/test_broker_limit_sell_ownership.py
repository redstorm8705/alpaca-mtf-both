from __future__ import annotations

import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import config
from execution import broker
from execution import order_admission
from execution import ownership_guard as guard
from execution import pending_protected_buys as pending
from execution.ownership_ledger_codec import dumps


def _ledger(*, intraday: float = 0.0, qhm: float = 0.0, f6: float = 0.0) -> dict:
    quantities = {
        "intraday": intraday,
        "qhm": qhm,
        "forever6": f6,
        "daytrade": 0.0,
    }
    total = sum(quantities.values())
    return {
        "version": 1,
        "last_reconciled_utc": "2026-11-04T00:00:00+00:00",
        "positions": {
            "AAPL": {
                "alpaca_net_qty": total,
                "drift": 0.0,
                "tiers": {
                    tier: {
                        "qty": qty,
                        "avg_cost": 100.0 if qty else 0.0,
                        "last_fill_id": f"fill-{tier}" if qty else None,
                    }
                    for tier, qty in quantities.items()
                },
            }
        },
    }


class _RealGuardHarness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = mock.Mock()
        self.client.submit_order.return_value = SimpleNamespace(
            id="paper-order",
            symbol="AAPL",
            status="accepted",
            side="sell",
            qty="2",
            client_order_id="IN-AAPL-s-1-guard",
        )
        self.stack = ExitStack()
        self.stack.enter_context(
            mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True)
        )
        self.stack.enter_context(mock.patch.object(config, "FOREVER6_ENABLED", False))
        self.stack.enter_context(
            mock.patch.object(guard, "_LEDGER_PATH", self.root / "ledger.json")
        )
        self.stack.enter_context(
            mock.patch.object(guard, "_LEDGER_BAK_PATH", self.root / "ledger.bak.json")
        )
        self.stack.enter_context(
            mock.patch.object(
                guard,
                "_PROTECTED_WATERMARK_PATH",
                self.root / "protected-watermark.json",
            )
        )
        self.stack.enter_context(
            mock.patch.object(guard, "page_floor_blind", return_value=None)
        )
        self.stack.enter_context(
            mock.patch.object(pending, "JOURNAL_PATH", self.root / "pending.json")
        )
        self.stack.enter_context(
            mock.patch.object(pending, "LOCK_PATH", self.root / ".pending.lock")
        )
        self.stack.enter_context(
            mock.patch.object(order_admission, "_LOCK_DIR", self.root / "order-locks")
        )
        self.stack.enter_context(
            mock.patch.object(
                order_admission,
                "_REDUCTION_PATH",
                self.root / "reductions.json",
            )
        )
        self.stack.enter_context(
            mock.patch.object(
                order_admission,
                "_REDUCTION_LOCK",
                self.root / ".reductions.lock",
            )
        )
        self.stack.enter_context(
            mock.patch.object(broker, "_get_trading_client", return_value=self.client)
        )

    def close(self) -> None:
        self.stack.close()
        self.temp.cleanup()

    def write_current(self, ledger: dict) -> None:
        guard._LEDGER_PATH.write_bytes(dumps(ledger, target=1))

    def write_backup(self, ledger: dict) -> None:
        guard._LEDGER_BAK_PATH.write_bytes(dumps(ledger, target=1))
        symbols = [
            symbol
            for symbol in ledger.get("positions", {})
            if guard.protected_floor(ledger, symbol) > 0
        ]
        guard._PROTECTED_WATERMARK_PATH.write_text(
            json.dumps({"schema": 1, "symbols": symbols}),
            encoding="utf-8",
        )

    def submit(
        self,
        *,
        position,
        qty: float | int | str | bool | None = None,
    ) -> object:
        position_effect = position if isinstance(position, BaseException) else None
        with mock.patch.object(
            broker,
            "get_open_position",
            side_effect=position_effect,
            return_value=None if position_effect else position,
        ):
            return broker.submit_limit_order(
                "AAPL",
                qty if qty is not None else (1 if position_effect else 2),
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-1-guard",
                time_in_force="gtc",
            )

    def submit_market(self, *, position, qty: float | int = 2) -> object:
        position_effect = position if isinstance(position, BaseException) else None
        with mock.patch.object(
            broker,
            "get_open_position",
            side_effect=position_effect,
            return_value=None if position_effect else position,
        ), mock.patch.object(config, "FOREVER6_ENABLED", True):
            return broker.submit_market_order(
                "AAPL",
                qty,
                "sell",
                tier="intraday",
                client_order_id="IN-AAPL-s-1-guard",
            )


def test_real_guard_nonprotected_current_allows_unknown_net_exit() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=1))
        result = h.submit(position=RuntimeError("paper position endpoint timeout"))
        assert result.id == "paper-order"
        h.client.submit_order.assert_called_once()
    finally:
        h.close()


def test_real_guard_valid_never_protected_watermark_allows_unknown_net_exit() -> None:
    h = _RealGuardHarness()
    try:
        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        h.write_backup(_ledger(intraday=1))
        result = h.submit(position=RuntimeError("paper position endpoint timeout"))
        assert result.id == "paper-order"
        h.client.submit_order.assert_called_once()
    finally:
        h.close()


def test_real_guard_protected_current_blocks_unknown_net_exit() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(qhm=1))
        assert (
            h.submit(position=RuntimeError("paper position endpoint timeout")) is None
        )
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_protected_watermark_blocks_unknown_net_exit() -> None:
    h = _RealGuardHarness()
    try:
        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        h.write_backup(_ledger(qhm=1))
        assert (
            h.submit(position=RuntimeError("paper position endpoint timeout")) is None
        )
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_double_unreadable_state_blocks_unknown_net_exit() -> None:
    h = _RealGuardHarness()
    try:
        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        guard._LEDGER_BAK_PATH.write_text("{also-corrupt", encoding="utf-8")
        guard._PROTECTED_WATERMARK_PATH.write_text("{also-corrupt", encoding="utf-8")
        assert (
            h.submit(position=RuntimeError("paper position endpoint timeout")) is None
        )
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_market_short_cannot_liquidate_protected_f6_shares() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(f6=2))
        position = SimpleNamespace(qty="2", side="long")
        assert h.submit_market(position=position) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_market_short_on_never_protected_symbol_still_submits() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger())
        result = h.submit_market(position=None)
        assert result.id == "paper-order"
        h.client.submit_order.assert_called_once()
    finally:
        h.close()


def test_fractional_market_sell_is_rejected_before_guard_or_broker() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=1, f6=1))
        position = SimpleNamespace(qty="2", side="long")
        assert h.submit_market(position=position, qty=1.9) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_rejects_nonfinite_broker_quantity() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=2, f6=1))
        position = SimpleNamespace(qty="nan", side="long")
        assert h.submit(position=position) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_limit_sell_rejects_fractional_requested_qty_before_guard_or_broker() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=1, f6=1))
        position = SimpleNamespace(qty="2", side="long")
        assert h.submit(position=position, qty=1.9) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_negative_qhm_cannot_cancel_f6_floor() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=1, qhm=-1, f6=1))
        position = SimpleNamespace(qty="1", side="long")
        assert h.submit(position=position) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_fractional_qhm_cannot_cancel_fractional_f6_floor() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=1, qhm=-0.5, f6=0.5))
        position = SimpleNamespace(qty="1", side="long")
        assert h.submit(position=position) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_stale_backup_cannot_forget_new_protected_floor() -> None:
    h = _RealGuardHarness()
    try:
        h.write_backup(_ledger(intraday=1))
        guard._PROTECTED_WATERMARK_PATH.write_text(
            '{"schema": 1, "symbols": ["AAPL"]}', encoding="utf-8"
        )
        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        position = SimpleNamespace(qty="1", side="long")
        assert h.submit(position=position) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_pending_protected_buy_blocks_exit() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=1))
        pending.reserve(
            "AAPL",
            1,
            "F6-AAPL-b-1-pending",
            0.0,
            limit_price=100.0,
            extended_hours=True,
        )
        assert (
            h.submit(position=RuntimeError("paper position endpoint timeout")) is None
        )
        h.client.submit_order.assert_not_called()
    finally:
        h.close()


def test_real_guard_bounded_quantity_is_refused_not_silently_shrunk() -> None:
    h = _RealGuardHarness()
    try:
        h.write_current(_ledger(intraday=1, qhm=2))
        position = SimpleNamespace(qty="3", side="long")
        assert h.submit(position=position) is None
        h.client.submit_order.assert_not_called()
    finally:
        h.close()
