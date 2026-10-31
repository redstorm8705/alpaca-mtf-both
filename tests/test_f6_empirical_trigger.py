"""Leakage, alignment, crash classification, and evidence tests for F6."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from execution.f6_empirical_trigger import evaluate_empirical_starter


def _market(
    *, close: float = 95.0, low: float = 94.5, vix: float = 40.0,
    vix3m: float = 35.0,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    idx = pd.date_range("2025-12-29", periods=25, freq="B", tz="America/New_York")
    closes = pd.Series(100.0, index=idx)
    spy = pd.DataFrame(
        {"open": closes, "high": closes * 1.01, "low": closes * 0.99,
         "close": closes, "volume": 1_000_000.0}
    )
    spy.iloc[-1] = [100.0, 100.5, low, close, 2_000_000.0]
    vol_idx = pd.DatetimeIndex(idx.tz_localize(None))
    vix_frame = pd.DataFrame({"Close": [20.0] * 24 + [vix]}, index=vol_idx)
    vix3m_frame = pd.DataFrame({"Close": [22.0] * 24 + [vix3m]}, index=vol_idx)
    return spy, vix_frame, vix3m_frame


def _evaluate(spy, vix, vix3m, expected=None):
    return evaluate_empirical_starter(
        spy,
        expected_session_date=expected or spy.index[-1].date(),
        vix_bars=vix,
        vix3m_bars=vix3m,
    )


def _latest_weekday() -> date:
    session = datetime.now(ZoneInfo("America/New_York")).date()
    while session.weekday() >= 5:
        session -= timedelta(days=1)
    return session


def test_broad_selloff_and_heightened_vix_arm_tier_b() -> None:
    spy, vix, vix3m = _market()
    verdict = _evaluate(spy, vix, vix3m)
    assert verdict.armed is True
    assert verdict.tier == "B"
    assert round(float(verdict.close_return_pct or 0.0), 8) == -5.0
    assert verdict.vix_to_prior_avg == 2.0
    assert verdict.vix_term_ratio > 1.0
    assert verdict.cash_fraction == 0.20
    assert verdict.prior_record_sha256


def test_vix_spike_without_spy_selloff_never_arms() -> None:
    spy, vix, vix3m = _market(close=100.0, low=99.0, vix=50.0, vix3m=35.0)
    verdict = _evaluate(spy, vix, vix3m)
    assert verdict.armed is False
    assert verdict.vix_confirmed is True
    assert verdict.reason == "SPY did not close negative"


def test_spy_selloff_without_vix_confirmation_never_arms() -> None:
    spy, vix, vix3m = _market(close=95.0, low=94.5, vix=20.0, vix3m=25.0)
    verdict = _evaluate(spy, vix, vix3m)
    assert verdict.armed is False
    assert verdict.price_gate is True
    assert verdict.vix_confirmed is False
    assert "heightened VIX" in verdict.reason


def test_positive_close_after_intraday_crash_never_arms() -> None:
    spy, vix, vix3m = _market(close=100.5, low=95.0, vix=40.0, vix3m=35.0)
    verdict = _evaluate(spy, vix, vix3m)
    assert verdict.armed is False
    assert verdict.price_gate is True
    assert verdict.reason == "SPY did not close negative"


def test_tier_c_uses_worse_intraday_depth_and_bypasses_count_cap() -> None:
    spy, vix, vix3m = _market(close=99.0, low=92.5, vix=45.0, vix3m=38.0)
    verdict = _evaluate(spy, vix, vix3m)
    assert verdict.armed is True
    assert verdict.tier == "C"
    assert verdict.bypass_monthly_cap is True
    assert verdict.cash_fraction == 0.30


def test_tier_c_three_session_crash_is_not_blocked_by_daily_price_gate() -> None:
    spy, vix, vix3m = _market(vix=40.0, vix3m=35.0)
    closes = (100.0, 96.5489, 93.2169, 89.99)
    for offset, close in zip(range(-4, 0), closes, strict=True):
        spy.iloc[offset] = [close, close * 1.002, close, close, 2_000_000.0]

    verdict = _evaluate(spy, vix, vix3m)

    assert -3.5 < float(verdict.close_return_pct or 0.0) < 0
    assert -3.5 < float(verdict.low_return_pct or 0.0) < 0
    assert verdict.three_session_return_pct <= -10.0
    assert verdict.price_gate is True
    assert verdict.armed is True
    assert verdict.tier == "C"


def test_stale_or_misaligned_history_fails_closed() -> None:
    spy, vix, vix3m = _market()
    stale = _evaluate(spy, vix, vix3m, expected=date(2026, 3, 1))
    assert stale.armed is False
    assert "expected completed session" in stale.reason
    missing = _evaluate(spy, vix.iloc[:-1], vix3m)
    assert missing.armed is False
    assert "aligned" in missing.reason


def test_impossible_ohlc_bar_fails_closed_before_depth_classification() -> None:
    spy, vix, vix3m = _market(close=99.0, low=90.0, vix=45.0, vix3m=38.0)
    spy.iloc[-1, spy.columns.get_loc("high")] = 91.0

    verdict = _evaluate(spy, vix, vix3m)

    assert verdict.armed is False
    assert "inconsistent OHLC" in verdict.reason


def test_current_vix_never_enters_its_prior_average() -> None:
    spy, vix, vix3m = _market(vix=40.0)
    first = _evaluate(spy, vix, vix3m)
    vix.iloc[-1, 0] = 80.0
    second = _evaluate(spy, vix, vix3m)
    assert first.vix_prior_20d_avg == second.vix_prior_20d_avg == 20.0


def _verdict(evidence: dict) -> SimpleNamespace:
    return SimpleNamespace(
        armed=True, reason="armed", tier="B", bypass_monthly_cap=False,
        cash_fraction=0.20, max_names=3, close_return_pct=-5.0,
        low_return_pct=-5.5, vix_close=40.0, vix_to_prior_avg=2.0,
        vix_term_ratio=1.1, evidence=lambda: evidence,
    )


def test_manager_blocks_when_exact_evidence_cannot_be_appended(monkeypatch) -> None:
    from execution import forever_hold_manager as f6

    evidence = {"proof": "x"}
    monkeypatch.setattr(
        f6, "evaluate_empirical_starter", lambda *_a, **_k: _verdict(evidence)
    )
    session = _market()[0].index[-1].date()
    now = datetime.combine(session, datetime.min.time(), tzinfo=f6.ET).replace(
        hour=17
    )
    manager = f6.ForeverHoldManager(
        SimpleNamespace(), clock=lambda: now,
        volatility_loader=lambda _d: _market()[1:]
    )
    monkeypatch.setattr(manager, "_append_trigger_evidence", lambda _evidence: False)
    result = manager.maybe_start_accumulation(
        _market()[0], session
    )
    assert result["triggered"] is False
    assert result["reason"] == "trigger evidence write failed"
    assert result["trigger_evidence"]["proof"] == "x"
    assert result["trigger_evidence"]["evaluated_at"] == now.isoformat()


def test_manager_uses_tier_budget_and_carries_exact_evidence(monkeypatch) -> None:
    import config
    from execution import forever_hold_manager as f6

    evidence = {"schema_version": 2, "as_of_date": "2026-01-30", "armed": True,
                "tier": "B"}
    monkeypatch.setattr(
        f6, "evaluate_empirical_starter", lambda *_a, **_k: _verdict(evidence)
    )
    monkeypatch.setattr(f6, "f6_symbol_eligible", lambda *_args: (True, "ok"))
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["NVDA"])
    session = _market()[0].index[-1].date()
    now = datetime.combine(session, datetime.min.time(), tzinfo=f6.ET).replace(
        hour=17
    )
    manager = f6.ForeverHoldManager(
        SimpleNamespace(), clock=lambda: now,
        volatility_loader=lambda _d: _market()[1:]
    )
    monkeypatch.setattr(
        manager, "_append_trigger_evidence", lambda value: value["tier"] == "B"
    )
    monkeypatch.setattr(manager, "_load_state_strict", lambda: {"events": []})
    result = manager.maybe_start_accumulation(
        _market()[0], session,
        price_by_sym={"NVDA": 100.0}, settled_cash=1_000.0,
    )
    assert result["budget"] == 200.0
    assert result["plan"] == [{"symbol": "NVDA", "price": 100.0, "why": "new base"}]
    assert result["trigger_evidence"]["tier"] == "B"
    assert result["trigger_evidence"]["evaluated_at"] == now.isoformat()


def _current_evidence(now: datetime) -> dict:
    today = now.date()
    prior_sessions = []
    candidate = today - timedelta(days=1)
    while len(prior_sessions) < 5:
        if candidate.weekday() < 5:
            prior_sessions.append(candidate.isoformat())
        candidate -= timedelta(days=1)
    prior_sessions.reverse()
    return {
        "schema_version": 2, "armed": True,
        "reason": "broad-market crash tier B confirmed by SPY and VIX",
        "as_of_date": today.isoformat(), "tier": "B",
        "close_return_pct": -5.0, "low_return_pct": -5.5,
        "three_session_return_pct": -6.0, "vix_close": 40.0,
        "vix_prior_20d_avg": 20.0, "vix_to_prior_avg": 2.0,
        "vix3m_close": 35.0, "vix_term_ratio": 40.0 / 35.0,
        "price_gate": True, "vix_confirmed": True, "cash_fraction": 0.20,
        "max_names": 3, "bypass_monthly_cap": False,
        "history_observations": 20,
        "history_start": (today - timedelta(days=30)).isoformat(),
        "history_end": prior_sessions[-1],
        "prior_session_dates": prior_sessions,
        "prior_record_sha256": "ab" * 32,
        "evaluated_at": now.isoformat(),
    }


def test_money_boundary_requires_exact_current_appended_evidence(
    tmp_path, monkeypatch
) -> None:
    from execution import forever_hold_manager as f6

    path = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(f6, "_TRIGGER_EVIDENCE", path)
    today = _latest_weekday()
    now = datetime.combine(today, datetime.min.time(), tzinfo=f6.ET).replace(hour=17)
    manager = f6.ForeverHoldManager(SimpleNamespace(), clock=lambda: now)
    evidence = _current_evidence(now)
    assert manager._append_trigger_evidence(evidence) is True
    assert manager._trigger_evidence_is_current(evidence) is True
    assert manager._trigger_evidence_is_current(
        {**evidence, "close_return_pct": 0.0}
    ) is False
    assert manager._trigger_evidence_is_current(None) is False


def test_money_boundary_recomputes_gates_and_ratios(tmp_path, monkeypatch) -> None:
    from execution import forever_hold_manager as f6

    path = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(f6, "_TRIGGER_EVIDENCE", path)
    today = _latest_weekday()
    now = datetime.combine(today, datetime.min.time(), tzinfo=f6.ET).replace(hour=17)
    manager = f6.ForeverHoldManager(SimpleNamespace(), clock=lambda: now)
    valid = _current_evidence(now)
    forged = {
        **valid,
        "vix_close": 20.0,
        "vix_prior_20d_avg": 20.0,
        "vix_to_prior_avg": 1.0,
        "vix3m_close": 25.0,
        "vix_term_ratio": 0.8,
        "vix_confirmed": True,
    }
    assert manager._append_trigger_evidence(forged) is True
    assert manager._trigger_evidence_is_current(forged) is False

    inconsistent = {**valid, "vix_to_prior_avg": 1.9}
    assert manager._append_trigger_evidence(inconsistent) is True
    assert manager._trigger_evidence_is_current(inconsistent) is False


def test_f6_starter_session_waits_for_final_vix() -> None:
    from execution import forever_hold_manager as f6

    forming = datetime(2026, 10, 16, 16, 5, tzinfo=f6.ET)
    final = datetime(2026, 10, 16, 16, 16, tzinfo=f6.ET)
    assert f6._f6_entry_session(forming) is False
    assert f6._f6_entry_session(final) is True


def test_manager_never_loads_forming_vix_before_416() -> None:
    from execution import forever_hold_manager as f6

    session = _market()[0].index[-1].date()
    forming = datetime.combine(
        session, datetime.min.time(), tzinfo=f6.ET
    ).replace(hour=16, minute=5)
    calls: list[date] = []

    def _volatility_loader(expected: date):
        calls.append(expected)
        raise AssertionError("forming VIX must never be loaded")

    manager = f6.ForeverHoldManager(
        SimpleNamespace(), clock=lambda: forming,
        volatility_loader=_volatility_loader,
    )
    result = manager.maybe_start_accumulation(_market()[0], session)

    assert result == {
        "triggered": False,
        "reason": "outside completed-VIX starter session",
        "plan": [],
        "budget": 0.0,
    }
    assert calls == []


def test_money_boundary_rejects_evidence_evaluated_before_final_vix(
    tmp_path, monkeypatch
) -> None:
    from execution import forever_hold_manager as f6

    path = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(f6, "_TRIGGER_EVIDENCE", path)
    today = _latest_weekday()
    now = datetime.combine(today, datetime.min.time(), tzinfo=f6.ET).replace(hour=17)
    manager = f6.ForeverHoldManager(SimpleNamespace(), clock=lambda: now)
    evidence = _current_evidence(now)
    evidence["evaluated_at"] = now.replace(hour=16, minute=5).isoformat()

    assert manager._append_trigger_evidence(evidence) is True
    assert manager._trigger_evidence_is_current(evidence) is False
