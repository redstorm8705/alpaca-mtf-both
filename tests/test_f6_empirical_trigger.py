"""Leakage, freshness, evidence, and replay tests for the F6 empirical trigger."""

from datetime import date
from types import SimpleNamespace

import pandas as pd

from execution.f6_empirical_trigger import evaluate_empirical_starter


def _bars() -> pd.DataFrame:
    idx = pd.date_range("2024-01-02", "2026-01-30", freq="B", tz="America/New_York")
    close = pd.Series(100.0, index=idx)
    frame = pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": 1_000_000.0}
    )
    frame.iloc[-1] = [100.0, 101.0, 88.0, 90.0, 10_000_000.0]
    return frame


def test_paired_empirical_tails_arm_on_completed_crash_bar() -> None:
    bars = _bars()
    verdict = evaluate_empirical_starter(
        bars,
        expected_session_date=bars.index[-1].date(),
        monthly_event_cap=4,
    )
    assert verdict.armed is True
    assert verdict.current_return_pct < verdict.return_cutoff_pct
    assert verdict.current_stress_ratio > verdict.stress_cutoff_ratio
    assert verdict.prior_record_sha256


def test_current_bar_never_enters_its_own_distribution() -> None:
    bars = _bars()
    first = evaluate_empirical_starter(
        bars,
        expected_session_date=bars.index[-1].date(),
        monthly_event_cap=4,
    )
    bars.iloc[-1] = [100.0, 110.0, 70.0, 80.0, 20_000_000.0]
    second = evaluate_empirical_starter(
        bars,
        expected_session_date=bars.index[-1].date(),
        monthly_event_cap=4,
    )
    assert first.return_cutoff_pct == second.return_cutoff_pct
    assert first.stress_cutoff_ratio == second.stress_cutoff_ratio
    assert first.prior_record_sha256 == second.prior_record_sha256


def test_stale_or_incomplete_history_fails_closed() -> None:
    bars = _bars()
    stale = evaluate_empirical_starter(
        bars,
        expected_session_date=date(2026, 2, 2),
        monthly_event_cap=4,
    )
    assert stale.armed is False
    assert "expected completed session" in stale.reason

    short = evaluate_empirical_starter(
        bars.loc["2026-01"],
        expected_session_date=bars.index[-1].date(),
        monthly_event_cap=4,
    )
    assert short.armed is False
    assert "full calendar cycle" in short.reason


def test_return_tail_without_stress_confirmation_does_not_arm() -> None:
    bars = _bars()
    bars.iloc[-1] = [100.0, 100.0, 90.0, 90.0, 500_000.0]
    verdict = evaluate_empirical_starter(
        bars,
        expected_session_date=bars.index[-1].date(),
        monthly_event_cap=4,
    )
    assert verdict.current_return_pct <= verdict.return_cutoff_pct
    assert verdict.current_stress_ratio < verdict.stress_cutoff_ratio
    assert verdict.armed is False


def test_manager_blocks_when_exact_evidence_cannot_be_appended(monkeypatch) -> None:
    from execution import forever_hold_manager as f6

    verdict = SimpleNamespace(
        armed=True, reason="armed", evidence=lambda: {"proof": "x"}
    )
    monkeypatch.setattr(
        f6, "evaluate_empirical_starter", lambda *_args, **_kwargs: verdict
    )
    manager = f6.ForeverHoldManager(SimpleNamespace())
    monkeypatch.setattr(manager, "_append_trigger_evidence", lambda _evidence: False)
    result = manager.maybe_start_accumulation(_bars(), _bars().index[-1].date())
    assert result["triggered"] is False
    assert result["reason"] == "trigger evidence write failed"
    assert result["trigger_evidence"] == {"proof": "x"}


def test_manager_carries_exact_evidence_into_an_affordable_plan(monkeypatch) -> None:
    import config
    from execution import forever_hold_manager as f6

    evidence = {"schema_version": 1, "as_of_date": "2026-01-30", "armed": True}
    verdict = SimpleNamespace(
        armed=True,
        reason="armed",
        current_return_pct=-2.0,
        return_cutoff_pct=-1.0,
        current_stress_ratio=2.0,
        stress_cutoff_ratio=1.5,
        evidence=lambda: evidence,
    )
    monkeypatch.setattr(
        f6, "evaluate_empirical_starter", lambda *_args, **_kwargs: verdict
    )
    monkeypatch.setattr(f6, "f6_symbol_eligible", lambda *_args: (True, "ok"))
    monkeypatch.setattr(config, "FOREVER6_UNIVERSE", ["NVDA"])
    manager = f6.ForeverHoldManager(SimpleNamespace())
    monkeypatch.setattr(
        manager, "_append_trigger_evidence", lambda value: value == evidence
    )
    monkeypatch.setattr(manager, "_load_state", lambda: {"events": []})
    monkeypatch.setattr(manager, "_events_this_month", lambda _state: 0)
    result = manager.maybe_start_accumulation(
        _bars(),
        _bars().index[-1].date(),
        price_by_sym={"NVDA": 100.0},
        settled_cash=1_000.0,
    )
    assert result["plan"] == [{"symbol": "NVDA", "price": 100.0, "why": "new base"}]
    assert result["trigger_evidence"] == evidence


def _current_evidence() -> dict:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("America/New_York")).date()
    return {
        "schema_version": 1,
        "armed": True,
        "reason": "empirical return and stress tails confirmed",
        "as_of_date": today.isoformat(),
        "current_return_pct": -2.0,
        "return_cutoff_pct": -1.0,
        "current_stress_ratio": 2.0,
        "stress_cutoff_ratio": 1.5,
        "tail_probability": 0.2,
        "history_observations": 300,
        "history_start": (today - timedelta(days=500)).isoformat(),
        "history_end": (today - timedelta(days=1)).isoformat(),
        "prior_record_sha256": "ab" * 32,
    }


def test_money_boundary_requires_exact_current_appended_evidence(
    tmp_path, monkeypatch
) -> None:
    from execution import forever_hold_manager as f6

    path = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(f6, "_TRIGGER_EVIDENCE", path)
    manager = f6.ForeverHoldManager(SimpleNamespace())
    evidence = _current_evidence()
    assert manager._append_trigger_evidence(evidence) is True
    assert manager._trigger_evidence_is_current(evidence) is True

    inconsistent = {**evidence, "current_return_pct": 0.0}
    assert manager._trigger_evidence_is_current(inconsistent) is False
    assert manager._trigger_evidence_is_current(None) is False
