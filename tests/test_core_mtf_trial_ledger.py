import json
from datetime import datetime, timezone

import pytest

from research.core_mtf_trial_ledger import (
    _records_hash,
    load_registry,
    record_and_append,
    record_trial,
)


UTC = timezone.utc


def _simulation(results):
    return {
        "kind": "core_mtf_simulated_ohlcv_replay",
        "execution_claims_permitted": False,
        "execution_evidence": "not_bid_ask_or_quote_data",
        "ledger_sha256": "ledger",
        "bar_data_sha256": "bars",
        "results": results,
    }


def _write(tmp_path, simulation):
    path = tmp_path / "simulation.json"
    path.write_text(json.dumps(simulation))
    return path


def _record(path, *, count=1, prior=None):
    return record_trial(
        path,
        trial_id=f"trial-{count}",
        hypothesis="testable mechanism",
        parameters={"rule": "baseline"},
        declared_trial_count=count,
        prior_records=[] if prior is None else prior,
        recorded_at=datetime(2026, 9, 20, tzinfo=UTC),
    )


def test_seven_complete_and_four_unevaluable_is_ineligible(tmp_path):
    results = [{"status": "complete", "net_return": 0.01}] * 7
    results += [{"status": "unevaluable"}] * 4
    record = _record(_write(tmp_path, _simulation(results)))
    assert record["selection_permitted"] is False
    assert record["outcome_accounting"]["statistically_eligible"] is False
    assert record["outcome_accounting"]["unevaluable_outcomes"] == 4


def test_thirty_outcomes_is_eligible_but_never_selectable(tmp_path):
    record = _record(
        _write(tmp_path, _simulation([{"status": "complete", "net_return": 0.01}] * 30))
    )
    assert record["outcome_accounting"]["statistically_eligible"] is True
    assert record["selection_permitted"] is False


def test_bad_complete_or_executable_simulation_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="invalid net_return"):
        _record(
            _write(tmp_path, _simulation([{"status": "complete", "net_return": "nan"}]))
        )
    executable = _simulation([])
    executable["execution_claims_permitted"] = True
    with pytest.raises(ValueError, match="execution claims"):
        _record(_write(tmp_path, executable))


def test_registry_position_and_parameter_type_are_enforced(tmp_path):
    path = _write(tmp_path, _simulation([]))
    with pytest.raises(ValueError, match="registry position"):
        _record(path, count=2)
    with pytest.raises(TypeError, match="parameters"):
        record_trial(
            path,
            trial_id="x",
            hypothesis="h",
            parameters=[],
            declared_trial_count=1,
            prior_records=[],
            recorded_at=datetime.now(UTC),
        )


def test_registry_rejects_broken_chain_duplicate_and_count_gap(tmp_path):
    path = _write(tmp_path, _simulation([]))
    first = _record(path)
    registry = tmp_path / "registry.jsonl"
    registry.write_text(json.dumps(first) + "\n")
    assert len(load_registry(registry)) == 1
    duplicate = dict(first)
    duplicate["declared_trial_count"] = 2
    duplicate["prior_trial_registry_sha256"] = "broken"
    registry.write_text(json.dumps(first) + "\n" + json.dumps(duplicate) + "\n")
    with pytest.raises(ValueError, match="duplicate|broken"):
        load_registry(registry)


def test_locked_append_rejects_stale_count_and_preserves_chain(tmp_path):
    path = _write(tmp_path, _simulation([]))
    registry = tmp_path / "registry.jsonl"
    output = tmp_path / "one.json"
    first = record_and_append(
        path,
        registry,
        output,
        trial_id="one",
        hypothesis="h",
        parameters={},
        declared_trial_count=1,
        recorded_at=datetime.now(UTC),
    )
    with pytest.raises(ValueError, match="registry position"):
        record_and_append(
            path,
            registry,
            tmp_path / "stale.json",
            trial_id="stale",
            hypothesis="h",
            parameters={},
            declared_trial_count=1,
            recorded_at=datetime.now(UTC),
        )
    assert load_registry(registry)[0]["trial_id"] == first["trial_id"]


def test_registry_chain_and_nonfinite_parameters_fail_closed(tmp_path):
    path = _write(tmp_path, _simulation([]))
    first = _record(path)
    broken = dict(first)
    broken["prior_trial_registry_sha256"] = "broken"
    registry = tmp_path / "broken.jsonl"
    registry.write_text(json.dumps(broken) + "\n")
    with pytest.raises(ValueError, match="broken hash"):
        load_registry(registry)
    with pytest.raises(ValueError, match="canonical finite"):
        record_trial(
            path,
            trial_id="nan",
            hypothesis="h",
            parameters={"nested": [float("nan")]},
            declared_trial_count=1,
            prior_records=[],
            recorded_at=datetime.now(UTC),
        )


def test_sequential_append_produces_contiguous_hash_chain(tmp_path):
    path = _write(tmp_path, _simulation([]))
    registry = tmp_path / "registry.jsonl"
    one = record_and_append(
        path, registry, tmp_path / "one.json", trial_id="one", hypothesis="h",
        parameters={}, declared_trial_count=1, recorded_at=datetime.now(UTC),
    )
    two = record_and_append(
        path, registry, tmp_path / "two.json", trial_id="two", hypothesis="h",
        parameters={}, declared_trial_count=2, recorded_at=datetime.now(UTC),
    )
    records = load_registry(registry)
    assert [row["declared_trial_count"] for row in records] == [1, 2]
    assert two["prior_trial_registry_sha256"] == _records_hash([one])


def test_sidecar_failure_keeps_committed_registry_record(tmp_path, monkeypatch):
    import research.core_mtf_trial_ledger as module

    path = _write(tmp_path, _simulation([]))
    registry = tmp_path / "registry.jsonl"
    def fail_sidecar(*_args):
        raise OSError("disk")

    monkeypatch.setattr(module, "write_trial_record", fail_sidecar)
    with pytest.raises(RuntimeError, match="committed.*registry"):
        record_and_append(
            path, registry, tmp_path / "broken.json", trial_id="one", hypothesis="h",
            parameters={}, declared_trial_count=1, recorded_at=datetime.now(UTC),
        )
    assert load_registry(registry)[0]["trial_id"] == "one"
