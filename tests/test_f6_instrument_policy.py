"""F6 product-policy regression tests.

Signed: OpenAI Codex, 2026-10-15.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock

import config
from execution import forever_hold_manager as f6
from execution.instrument_policy import (
    LEVERAGED_ETF_TICKERS,
    f6_live_asset_eligible,
    f6_symbol_eligible,
    instrument_policy,
    is_leveraged_etf,
)
from strategy import day_tier_leverage


def _entry_clock():
    return datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)


def _admit_crash_evidence(manager: f6.ForeverHoldManager) -> dict:
    """Keep product-policy tests focused past the independent crash gate."""
    today = _entry_clock().date()
    prior_sessions = []
    candidate = today - timedelta(days=1)
    while len(prior_sessions) < 5:
        if candidate.weekday() < 5:
            prior_sessions.append(candidate.isoformat())
        candidate -= timedelta(days=1)
    prior_sessions.reverse()
    evidence = {
        "tier": "A", "max_names": 2, "cash_fraction": 0.10,
        "bypass_monthly_cap": False,
        "as_of_date": today.isoformat(),
        "prior_session_dates": prior_sessions,
    }
    manager._trigger_evidence_is_current = lambda _evidence: True
    return evidence


def test_current_f6_universe_has_no_leveraged_etf() -> None:
    assert not ({s.upper() for s in config.FOREVER6_UNIVERSE} & LEVERAGED_ETF_TICKERS)


def test_current_f6_universe_contains_common_stocks_only() -> None:
    assert "DRAM" not in config.FOREVER6_UNIVERSE
    assert "EWY" not in config.FOREVER6_UNIVERSE
    assert all(
        instrument_policy(symbol) is not None
        and instrument_policy(symbol).kind == "equity"
        for symbol in config.FOREVER6_UNIVERSE
    )


def test_registry_covers_every_day_router_product_above_one_x() -> None:
    bull = set(day_tier_leverage._DEFAULT_BULL_2X.values())
    bull |= set(day_tier_leverage._DEFAULT_BULL_2X_ALT.values())
    inverse = {
        symbol
        for symbol, leverage in (
            list(day_tier_leverage._DEFAULT_INVERSE.values())
            + list(day_tier_leverage._DEFAULT_INVERSE_ALT.values())
        )
        if abs(float(leverage)) > 1.0
    }
    assert bull | inverse <= LEVERAGED_ETF_TICKERS


def test_every_day_router_product_is_rejected_by_live_map_veto() -> None:
    products = set(day_tier_leverage._DEFAULT_BULL_2X.values())
    products |= set(day_tier_leverage._DEFAULT_BULL_2X_ALT.values())
    products |= {v[0] for v in day_tier_leverage._DEFAULT_INVERSE.values()}
    products |= {v[0] for v in day_tier_leverage._DEFAULT_INVERSE_ALT.values()}
    for symbol in products:
        eligible, reason = f6_symbol_eligible(symbol, [symbol])
        assert not eligible
        assert "existing bot map" in reason


def test_active_day_router_override_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        config, "DAYTRADE_LEVERAGED_BULL_MAP", {"ANY": "DRAM"}, raising=False
    )
    eligible, reason = f6_symbol_eligible("DRAM", ["DRAM"])
    assert not eligible
    assert "existing bot map" in reason


def test_unleveraged_funds_are_not_admitted_to_f6() -> None:
    for symbol in ("DRAM", "EWY"):
        assert f6_symbol_eligible(symbol, [symbol]) == (
            False,
            "etf_not_admitted: F6 accepts common stocks only",
        )
        assert not is_leveraged_etf(symbol)


def test_one_x_inverse_is_not_leveraged_but_is_f6_incompatible() -> None:
    assert not is_leveraged_etf("AAPD")
    eligible, reason = f6_symbol_eligible("AAPD", ["AAPD"])
    assert not eligible
    assert "leveraged/inverse" in reason


def test_unknown_instrument_fails_closed() -> None:
    assert f6_symbol_eligible("UNKNOWN", ["UNKNOWN"]) == (
        False,
        "instrument classification is missing",
    )


def _asset(symbol: str = "NVDA", **updates: object) -> dict:
    row: dict[str, object] = {
        "symbol": symbol,
        "status": "active",
        "tradable": True,
        "asset_class": "us_equity",
        "name": "NVIDIA Corporation Common Stock",
    }
    row.update(updates)
    return row


def test_live_asset_policy_accepts_reviewed_active_equity() -> None:
    assert f6_live_asset_eligible("NVDA", _asset()) == (True, "ok")
    rest_shape = _asset()
    rest_shape["class"] = rest_shape.pop("asset_class")
    assert f6_live_asset_eligible("NVDA", rest_shape) == (True, "ok")


def test_live_asset_policy_fails_closed_and_vetoes_leverage_name() -> None:
    assert not f6_live_asset_eligible("NVDA", None)[0]
    assert not f6_live_asset_eligible("NVDA", _asset(symbol="TSLA"))[0]
    assert not f6_live_asset_eligible("NVDA", _asset(status="inactive"))[0]
    assert not f6_live_asset_eligible("NVDA", _asset(tradable=False))[0]
    assert not f6_live_asset_eligible(
        "NVDA", _asset(name="Example Daily 2X Long NVDA ETF")
    )[0]


def test_config_validation_rejects_leveraged_f6_symbol(monkeypatch) -> None:
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["NVDA", "NVDL"])
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    with mock.patch.object(config, "FOREVER6_ENABLED", False):
        try:
            config.validate_config()
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError(
                "leveraged F6 configuration must fail startup validation"
            )


def test_f6_activation_requires_enforced_ownership(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(config, "OWNERSHIP_GUARD_ENFORCE", False)
    try:
        config.validate_config()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("F6 cannot be armed while ownership enforcement is off")


def test_execute_starter_blocks_leveraged_symbol_before_broker(monkeypatch) -> None:
    broker = mock.Mock()
    manager = f6.ForeverHoldManager(broker, clock=_entry_clock)
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["NVDA", "NVDL"])
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    slack = mock.Mock()
    monkeypatch.setattr(f6, "_slack_safe", slack)

    result = manager.execute_starter(
        [{"symbol": "NVDL", "price": 50.0, "why": "invalid external plan"}],
        budget=100.0,
        settled_cash=1_000.0,
        trigger_evidence=_admit_crash_evidence(manager),
    )

    assert result["placed"] == []
    assert result["spent"] == 0.0
    assert "leveraged/inverse" in result["skipped"][0]["skip"]
    broker.assert_not_called()
    slack.assert_called_once()


def test_execute_starter_blocks_unleveraged_etf_before_broker(monkeypatch) -> None:
    broker = mock.Mock()
    manager = f6.ForeverHoldManager(broker, clock=_entry_clock)
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["DRAM"])
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(f6, "_slack_safe", mock.Mock())

    result = manager.execute_starter(
        [{"symbol": "DRAM", "price": 40.0, "why": "invalid external plan"}],
        budget=100.0,
        settled_cash=1_000.0,
        trigger_evidence=_admit_crash_evidence(manager),
    )

    assert result["placed"] == []
    assert "etf_not_admitted" in result["skipped"][0]["skip"]
    broker.assert_not_called()


def test_execute_starter_blocks_live_asset_contradiction_before_reservation(
    monkeypatch,
) -> None:
    client = mock.Mock()
    client.get_asset.return_value = _asset(name="Example Daily 2X Long NVDA ETF")
    broker = mock.Mock()
    broker.get_trading_client.return_value = client
    manager = f6.ForeverHoldManager(broker, clock=_entry_clock)
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["NVDA"])
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(f6, "_slack_safe", mock.Mock())

    result = manager.execute_starter(
        [{"symbol": "NVDA", "price": 50.0, "why": "contradictory live asset"}],
        budget=100.0,
        settled_cash=1_000.0,
        trigger_evidence=_admit_crash_evidence(manager),
    )

    assert result["placed"] == []
    assert "live instrument policy" in result["skipped"][0]["skip"]
    client.get_asset.assert_called_once_with("NVDA")


def test_execute_starter_blocks_symbol_removed_after_plan(monkeypatch) -> None:
    broker = mock.Mock()
    manager = f6.ForeverHoldManager(broker, clock=_entry_clock)
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["NVDA"])
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(f6, "_slack_safe", mock.Mock())

    result = manager.execute_starter(
        [{"symbol": "TSLA", "price": 50.0, "why": "stale plan"}],
        budget=100.0,
        settled_cash=1_000.0,
        trigger_evidence=_admit_crash_evidence(manager),
    )

    assert result["placed"] == []
    assert "not in FOREVER6_UNIVERSE" in result["skipped"][0]["skip"]
    broker.assert_not_called()


def test_execute_starter_master_flag_blocks_valid_plan_at_money_boundary(
    monkeypatch,
) -> None:
    broker = mock.Mock()
    manager = f6.ForeverHoldManager(broker)
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["NVDA"])
    monkeypatch.setattr(config, "FOREVER6_ENABLED", False)

    result = manager.execute_starter(
        [{"symbol": "NVDA", "price": 50.0, "why": "valid but dark"}],
        budget=100.0,
        settled_cash=1_000.0,
    )

    assert result["placed"] == []
    assert result["reason"] == "F6 disabled"
    broker.assert_not_called()
