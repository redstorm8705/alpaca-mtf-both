"""F6 may bypass the trading-tier kill only for a proven genuine loss."""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import alerts
import execution.risk_manager as rm


def _state(cause: rm.KillVerdictCause, **overrides: object) -> dict:
    now = datetime.now(timezone.utc)
    state = {
        "schema_version": rm._KILL_STATE_SCHEMA_VERSION,
        "date": now.astimezone(rm._ET).strftime("%Y-%m-%d"),
        "session_date_et": now.astimezone(rm._ET).strftime("%Y-%m-%d"),
        "cause": cause.value,
        "killed": True,
        "halt_entries": True,
        "evaluated_at": now.isoformat(),
        "trading_pnl": -75.0,
        "daily_start_value": 1000.0,
        "loss_pct": -0.075,
        "attribution_exact": True,
    }
    state.update(overrides)
    return state


def test_genuine_current_session_loss_allows_f6() -> None:
    verdict = rm.KillVerdict.from_state(
        _state(rm.KillVerdictCause.GENUINE_TRADING_LOSS)
    )
    assert verdict is not None
    assert verdict.allows_f6_starter() is True


def test_control_fault_never_allows_f6() -> None:
    verdict = rm.KillVerdict.from_state(
        _state(
            rm.KillVerdictCause.CONTROL_FAULT,
            trading_pnl=None,
            daily_start_value=None,
            loss_pct=None,
        )
    )
    assert verdict is not None
    assert verdict.allows_f6_starter() is False


def test_legacy_stale_and_inconsistent_records_fail_closed() -> None:
    assert rm.KillVerdict.from_state({"date": "2026-10-16", "killed": True}) is None

    stale_time = datetime.now(timezone.utc) - timedelta(days=1)
    stale = _state(
        rm.KillVerdictCause.GENUINE_TRADING_LOSS,
        session_date_et=stale_time.astimezone(rm._ET).strftime("%Y-%m-%d"),
        date=stale_time.astimezone(rm._ET).strftime("%Y-%m-%d"),
        evaluated_at=stale_time.isoformat(),
    )
    assert rm.KillVerdict.from_state(stale) is None

    inconsistent = _state(
        rm.KillVerdictCause.GENUINE_TRADING_LOSS,
        loss_pct=-0.50,
    )
    assert rm.KillVerdict.from_state(inconsistent) is None


def test_risk_manager_reloads_durable_record_before_allowing_bypass(
    monkeypatch,
) -> None:
    state = _state(rm.KillVerdictCause.GENUINE_TRADING_LOSS)
    monkeypatch.setattr(rm, "_load_kill_state", lambda: state)
    risk = rm.RiskManager(1000.0, 1000.0)
    assert risk.killed is True
    assert risk.f6_genuine_loss_bypass_allowed() is True

    state["cause"] = rm.KillVerdictCause.CONTROL_FAULT.value
    state["trading_pnl"] = None
    state["daily_start_value"] = None
    state["loss_pct"] = None
    assert risk.f6_genuine_loss_bypass_allowed() is False


def test_trip_persists_typed_loss_evidence(monkeypatch) -> None:
    monkeypatch.setattr(rm, "_load_kill_state", lambda: {})
    save = mock.Mock()
    monkeypatch.setattr(rm, "_save_kill_state", save)
    risk = rm.RiskManager(925.0, 1000.0)

    def _exact_measure():
        # KILL-ATTR-1023 T1: "exact" only when every symbol was proven.
        risk._kill_attr = {"exact": True}
        return -75.0, True

    monkeypatch.setattr(risk, "_intraday_trading_pnl", _exact_measure)

    assert risk.check_kill_switch() is True
    save.assert_called_once_with(
        killed=True,
        halt_entries=True,
        cause=rm.KillVerdictCause.GENUINE_TRADING_LOSS,
        trading_pnl=-75.0,
        daily_start_value=1000.0,
        loss_pct=-0.075,
        attribution_exact=True,
    )


def test_trip_on_unproven_measure_is_not_typed_exact(monkeypatch) -> None:
    """KILL-ATTR-1023 T1: a trip measured with an unproven symbol persists
    attribution_exact=False, so it never counts as GENUINE loss for F6."""
    monkeypatch.setattr(rm, "_load_kill_state", lambda: {})
    save = mock.Mock()
    monkeypatch.setattr(rm, "_save_kill_state", save)
    risk = rm.RiskManager(925.0, 1000.0)

    def _unproven_measure():
        risk._kill_attr = {"exact": False, "unproven": {"GE": {}}}
        return -75.0, True

    monkeypatch.setattr(risk, "_intraday_trading_pnl", _unproven_measure)

    assert risk.check_kill_switch() is True
    assert save.call_args.kwargs["attribution_exact"] is False


def test_atomic_state_writer_emits_versioned_verdict(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "kill.json"
    monkeypatch.setattr(rm, "_KILL_STATE_FILE", state_path)
    rm._save_kill_state(
        killed=True,
        halt_entries=True,
        cause=rm.KillVerdictCause.GENUINE_TRADING_LOSS,
        trading_pnl=-75.0,
        daily_start_value=1000.0,
        loss_pct=-0.075,
        attribution_exact=True,
    )

    data = json.loads(state_path.read_text())
    assert data["schema_version"] == rm._KILL_STATE_SCHEMA_VERSION
    assert data["cause"] == "GENUINE_TRADING_LOSS"
    assert data["session_date_et"] == datetime.now(rm._ET).strftime("%Y-%m-%d")
    assert rm.KillVerdict.from_state(data) is not None


def test_equity_fallback_loss_cannot_unlock_f6(monkeypatch) -> None:
    monkeypatch.setattr(rm, "_load_kill_state", lambda: {})
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", False)
    monkeypatch.setattr(rm, "_in_rth_now", lambda: True)
    save = mock.Mock()
    monkeypatch.setattr(rm, "_save_kill_state", save)
    risk = rm.RiskManager(925.0, 1000.0)
    monkeypatch.setattr(risk, "_intraday_trading_pnl", lambda: (0.0, False))
    monkeypatch.setattr(risk, "_buy_and_hold_intraday_pl", lambda: 0.0)

    assert risk.check_kill_switch() is True
    assert save.call_args.kwargs["cause"] is rm.KillVerdictCause.CONTROL_FAULT
    assert save.call_args.kwargs["attribution_exact"] is False


def test_non_object_json_is_a_control_fault_not_an_absent_state(
    tmp_path, monkeypatch
) -> None:
    state_path = tmp_path / "kill.json"
    state_path.write_text("[]")
    monkeypatch.setattr(rm, "_KILL_STATE_FILE", state_path)
    monkeypatch.setattr(alerts, "send_slack", mock.Mock())
    loaded = rm._load_kill_state()
    assert loaded["killed"] is True
    assert loaded["cause"] == rm.KillVerdictCause.CONTROL_FAULT.value
    verdict = rm.KillVerdict.from_state(loaded)
    assert verdict is not None
    assert verdict.allows_f6_starter() is False
