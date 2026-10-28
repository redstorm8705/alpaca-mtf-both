from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

import config
from execution import broker as f6_broker
from execution.f6_cycle import resolve_f6_kill_route, run_f6_after_close
from execution.forever_hold_manager import ForeverHoldManager


def test_f6_kill_route_preserves_typed_policy() -> None:
    allow = SimpleNamespace(f6_genuine_loss_bypass_allowed=lambda: True)
    deny = SimpleNamespace(f6_genuine_loss_bypass_allowed=lambda: False)

    assert resolve_f6_kill_route(
        enabled=False, kill_tripped=False, control_fault="", risk=allow
    ).block_starter
    assert not resolve_f6_kill_route(
        enabled=True, kill_tripped=False, control_fault="", risk=deny
    ).block_starter
    assert resolve_f6_kill_route(
        enabled=True, kill_tripped=True, control_fault="bad account", risk=allow
    ).block_starter
    assert not resolve_f6_kill_route(
        enabled=True, kill_tripped=True, control_fault="", risk=allow
    ).block_starter
    assert resolve_f6_kill_route(
        enabled=True, kill_tripped=True, control_fault="", risk=deny
    ).block_starter

    def broken() -> bool:
        raise RuntimeError("corrupt verdict")

    assert resolve_f6_kill_route(
        enabled=True,
        kill_tripped=True,
        control_fault="",
        risk=SimpleNamespace(f6_genuine_loss_bypass_allowed=broken),
    ).block_starter


def test_after_close_trims_run_when_starter_blocked() -> None:
    manager = mock.Mock()
    frame = pd.DataFrame({"close": [100.0]})
    with (
        mock.patch(
            "execution.forever_hold_manager.ForeverHoldManager", return_value=manager
        ) as manager_class,
        mock.patch(
            "execution.forever_hold_manager._f6_entry_session", return_value=True
        ),
    ):
        result = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=datetime(2026, 10, 23, 17, 0),
            block_starter=True,
            fetch_bars=lambda *_a, **_k: frame,
        )
    manager.evaluate_and_execute_trims.assert_called_once_with()
    manager_class.assert_called_once_with(f6_broker)
    manager.maybe_start_accumulation.assert_not_called()
    assert result["trims_ran"] is True
    assert result["reason"] == "starter_blocked"


def test_after_close_emits_unique_post_live_proof(caplog) -> None:
    manager = mock.Mock()
    with (
        caplog.at_level(logging.INFO, logger="execution.f6_cycle"),
        mock.patch(
            "execution.forever_hold_manager._f6_entry_session", return_value=False
        ),
    ):
        run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=datetime(2026, 10, 23, 17, 0),
            block_starter=True,
            fetch_bars=lambda *_a, **_k: None,
            manager=manager,
        )
    assert "change=f6-sim-1023 after-close shared route active" in caplog.text


def test_after_close_routes_plan_and_execution() -> None:
    now_et = datetime(2026, 10, 23, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    plan = {
        "plan": [{"symbol": "NVDA", "price": 100.0}],
        "budget": 100.0,
        "trigger_evidence": {"armed": True},
    }
    execution = {
        "submitted": [{"symbol": "NVDA", "qty": 1}],
        "reserved": 100.0,
        "reason": "submitted",
    }
    frame = pd.DataFrame({"close": [100.0]})
    with (
        mock.patch.object(config, "FOREVER6_ENABLED", True),
        mock.patch.object(
            ForeverHoldManager,
            "evaluate_and_execute_trims",
            autospec=True,
            return_value={},
        ),
        mock.patch.object(
            ForeverHoldManager,
            "maybe_start_accumulation",
            autospec=True,
            return_value=plan,
        ) as maybe_start,
        mock.patch.object(
            ForeverHoldManager,
            "execute_starter",
            autospec=True,
            return_value=execution,
        ) as execute_starter,
        mock.patch(
            "execution.forever_hold_manager._f6_entry_session", return_value=True
        ),
    ):
        result = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=now_et,
            block_starter=False,
            fetch_bars=lambda *_a, **_k: frame,
        )
    manager = maybe_start.call_args.args[0]
    assert isinstance(manager, ForeverHoldManager)
    assert manager.broker is f6_broker
    assert maybe_start.call_args.args[2] == now_et.date()
    assert config.FOREVER6_ENABLED is False
    execute_starter.assert_called_once()
    assert result["submitted"] == [{"symbol": "NVDA", "qty": 1}]
    assert result["reserved"] == 100.0


def test_after_close_accepts_injected_offline_manager() -> None:
    manager = mock.Mock()
    manager.evaluate_and_execute_trims.return_value = {"NVDA": {"fired": []}}
    frame = pd.DataFrame({"close": [100.0]})
    with mock.patch(
        "execution.forever_hold_manager._f6_entry_session", return_value=True
    ):
        result = run_f6_after_close(
            enabled=True,
            is_after_close=True,
            now_et=datetime(2026, 10, 23, 17, 0),
            block_starter=True,
            fetch_bars=lambda *_a, **_k: frame,
            manager=manager,
        )
    assert result["trim_result"] == {"NVDA": {"fired": []}}
    manager.evaluate_and_execute_trims.assert_called_once_with()
