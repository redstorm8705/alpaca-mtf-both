"""Launch-level assertions for the hermetic F6 full-path simulator."""

from __future__ import annotations

import config
import pytest
from types import SimpleNamespace

from execution import forever_hold_manager as f6
from execution import tier_capital_allocator as allocator
from research.f6_full_path_simulation import (
    OfflineBroker,
    REQUIRED_SCENARIOS,
    REQUIRED_FILESYSTEM_REFUSALS,
    SOURCE_FILES,
    _artifact_runtime_evidence_valid,
    _artifact_source_binding_valid,
    _scenario_rows_valid,
    _source_tree_digest,
    run_simulation,
)


def test_offline_broker_rejects_duplicate_client_order_id() -> None:
    broker = OfflineBroker()
    request = SimpleNamespace(
        symbol="NVDA",
        qty=1,
        side="sell",
        client_order_id="F6-NVDA-s-1-sim",
        limit_price=400.0,
        extended_hours=True,
    )
    broker.submit_order(request)
    with pytest.raises(RuntimeError, match="duplicate client_order_id"):
        broker.submit_order(request)


def test_scenario_validation_rejects_duplicate_that_hides_failure() -> None:
    rows = [
        {"name": name, "status": "PASS"} for name in sorted(REQUIRED_SCENARIOS)
    ]
    rows.insert(0, {"name": rows[0]["name"], "status": "FAIL"})
    assert not _scenario_rows_valid(rows)


def test_artifact_runtime_evidence_rejects_network_and_duplicate_ids() -> None:
    transcript = [
        {"kind": "entry", "symbol": "NVDA", "side": "buy", "qty": 1,
         "limit": 100.0, "client_order_id": "id-0"},
        {"kind": "trim", "symbol": "NVDA", "side": "sell", "qty": 1,
         "client_order_id": "id-1"},
        {"kind": "trim", "symbol": "NVDA", "side": "sell", "qty": 2,
         "client_order_id": "id-2"},
        {"kind": "entry", "symbol": "NVDA", "side": "buy", "qty": 2,
         "limit": 100.0, "client_order_id": "id-3"},
    ]
    artifact = {
        "network_capable": False,
        "production_flag_remained_disabled": True,
        "order_transcript": transcript,
        "final_f6_qty": 3.0,
        "scenarios": [
            {"name": "entry_fill_ownership_replay", "evidence": {"f6_qty": 4.0}},
            {"name": "trim_fill_ownership_replay", "evidence": {"final_f6_qty": 3.0}},
            {"name": "partial_trim_fail_closed_then_terminal_replay", "evidence": {
                "partial_f6_qty": 8.0,
                "completed_f6_qty": 6.0,
                "new_orders_after_restart": 0,
            }},
            {
                "name": "filesystem_escape_refused",
                "evidence": {
                    "refused": {
                        name: True for name in REQUIRED_FILESYSTEM_REFUSALS
                    }
                },
            },
        ],
    }
    assert _artifact_runtime_evidence_valid(artifact)
    artifact["network_capable"] = True
    assert not _artifact_runtime_evidence_valid(artifact)
    artifact["network_capable"] = False
    transcript[2]["client_order_id"] = transcript[1]["client_order_id"]
    assert not _artifact_runtime_evidence_valid(artifact)


@pytest.mark.parametrize(
    ("row", "field", "bad"),
    ((1, "symbol", "AAPL"), (1, "side", "buy"), (2, "qty", 200),
     (0, "limit", 1.0)),
)
def test_artifact_runtime_evidence_rejects_order_tampering(
    row: int, field: str, bad: object
) -> None:
    result = run_simulation()
    result["order_transcript"][row][field] = bad
    assert not _artifact_runtime_evidence_valid(result)


def test_artifact_runtime_evidence_rejects_final_quantity_tampering() -> None:
    result = run_simulation()
    result["final_f6_qty"] = -197.0
    assert not _artifact_runtime_evidence_valid(result)


@pytest.mark.parametrize(
    ("name", "bad"),
    (
        ("subprocess_write", False),
        ("native_loader", None),
        ("process_system", None),
        ("process_posix_spawn", False),
    ),
)
def test_artifact_runtime_evidence_rejects_process_refusal_tampering(
    name: str,
    bad: object,
) -> None:
    result = run_simulation()
    scenarios = {row["name"]: row for row in result["scenarios"]}
    refusals = scenarios["filesystem_escape_refused"]["evidence"]["refused"]
    assert set(refusals) == set(REQUIRED_FILESYSTEM_REFUSALS)
    refusals[name] = bad
    assert not _artifact_runtime_evidence_valid(result)


def test_source_binding_includes_replay_and_ledger_dependencies() -> None:
    required = {
        "execution/ownership_ledger_codec.py",
        "execution/quarterly_hold_manager.py",
        "execution/tier_transfers.py",
        "execution/overnight_capacity.py",
        "events/catalyst_engine.py",
        "reporting/pnl_ledger.py",
        "tier_names.py",
    }
    assert required <= set(SOURCE_FILES)


def test_committed_check_rejects_stale_bytes_restored_in_dirty_worktree() -> None:
    stale = {"execution/f6_cycle.py": "a" * 64}
    head = {"execution/f6_cycle.py": "b" * 64}
    artifact = {
        "source_sha256_before": stale,
        "source_sha256_after": stale,
        "source_tree_sha256": _source_tree_digest(stale),
    }
    assert not _artifact_source_binding_valid(
        artifact,
        head_sources=head,
        index_sources=head,
        working_sources=stale,
    )


def test_committed_check_accepts_exact_clean_head_sources() -> None:
    head = {"execution/f6_cycle.py": "b" * 64}
    artifact = {
        "source_sha256_before": head,
        "source_sha256_after": head,
        "source_tree_sha256": _source_tree_digest(head),
    }
    assert _artifact_source_binding_valid(
        artifact,
        head_sources=head,
        index_sources=head,
        working_sources=head,
    )


def test_committed_check_rejects_staged_source_hidden_by_head_worktree() -> None:
    head = {"execution/f6_cycle.py": "a" * 64}
    staged = {"execution/f6_cycle.py": "b" * 64}
    artifact = {
        "source_sha256_before": head,
        "source_sha256_after": head,
        "source_tree_sha256": _source_tree_digest(head),
    }
    assert not _artifact_source_binding_valid(
        artifact,
        head_sources=head,
        index_sources=staged,
        working_sources=head,
    )


def test_f6_full_path_simulation_passes_and_remains_hermetic() -> None:
    result = run_simulation()

    assert result["overall"] == "PASS"
    assert result["network_capable"] is False
    assert result["production_flag_remained_disabled"] is True
    assert config.FOREVER6_ENABLED is False
    assert set(result["source_sha256"]) == set(SOURCE_FILES)
    assert all(len(value) == 64 for value in result["source_sha256"].values())
    assert result["source_sha256_before"] == result["source_sha256_after"]

    scenarios = {row["name"]: row for row in result["scenarios"]}
    assert set(scenarios) == REQUIRED_SCENARIOS
    assert all(row["status"] == "PASS" for row in scenarios.values())
    assert scenarios["filesystem_escape_refused"]["evidence"]["refused"][
        "subprocess_write"
    ] is True

    assert [row["kind"] for row in result["order_transcript"]] == [
        "entry",
        "trim",
        "trim",
        "entry",
    ]
    client_ids = [row["client_order_id"] for row in result["order_transcript"]]
    assert len(client_ids) == len(set(client_ids))
    assert result["order_transcript"][0]["symbol"] == "NVDA"
    assert result["order_transcript"][0]["side"] == "buy"
    assert result["order_transcript"][0]["qty"] == 1
    assert result["order_transcript"][1]["side"] == "sell"
    assert result["order_transcript"][1]["qty"] == 1
    assert result["final_f6_qty"] == 3.0


def test_f6_full_path_simulation_restores_globals_after_exception() -> None:
    original_ready = f6._STARTUP_READY
    original_quotes = dict(f6._F6_QUOTE_OBSERVATIONS)
    original_allocator = allocator._LIVE_ALLOCATOR
    ready_before = object()
    quote_before = {"SENTINEL": (1.0, "ts")}
    allocator_before = object()
    flag_before = config.FOREVER6_ENABLED

    f6._STARTUP_READY = ready_before  # type: ignore[assignment]
    f6._F6_QUOTE_OBSERVATIONS.clear()
    f6._F6_QUOTE_OBSERVATIONS.update(quote_before)  # type: ignore[arg-type]
    allocator._LIVE_ALLOCATOR = allocator_before  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="injected F6 simulation failure"):
            run_simulation(inject_failure=True)
        assert f6._STARTUP_READY is ready_before
        assert f6._F6_QUOTE_OBSERVATIONS == quote_before
        assert allocator._LIVE_ALLOCATOR is allocator_before
        assert config.FOREVER6_ENABLED is flag_before is False
    finally:
        f6._STARTUP_READY = original_ready
        f6._F6_QUOTE_OBSERVATIONS.clear()
        f6._F6_QUOTE_OBSERVATIONS.update(original_quotes)
        allocator._LIVE_ALLOCATOR = original_allocator
