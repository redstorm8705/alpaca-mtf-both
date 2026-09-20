"""Write immutable accounting for Core MTF admission-hypothesis trials.

The ledger makes trial count and source provenance explicit before any metric may
be compared or selected. It does not optimize parameters or authorize trading.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MIN_OBSERVATIONS = 30


def record_trial(
    simulation_path: Path,
    *,
    trial_id: str,
    hypothesis: str,
    parameters: dict[str, Any],
    declared_trial_count: int,
    prior_records: list[dict[str, Any]],
    recorded_at: datetime,
) -> dict[str, Any]:
    """Create one non-selectable trial record from a simulated replay artifact."""
    if not trial_id.strip() or not hypothesis.strip():
        raise ValueError("trial_id and hypothesis must be non-empty")
    if declared_trial_count < 1:
        raise ValueError("declared_trial_count must be positive")
    if not isinstance(parameters, dict):
        raise TypeError("parameters must be an object")
    parameters_json = _canonical_json(parameters)
    if not isinstance(prior_records, list):
        raise TypeError("prior_records must be a list")
    _validate_records(prior_records)
    prior_ids = [record.get("trial_id") for record in prior_records]
    if any(not isinstance(value, str) or not value for value in prior_ids):
        raise ValueError("prior trial registry has invalid trial_id")
    if len(set(prior_ids)) != len(prior_ids) or trial_id in prior_ids:
        raise ValueError("trial_id is not unique in registry")
    if declared_trial_count != len(prior_records) + 1:
        raise ValueError("declared_trial_count must equal registry position")
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    raw = simulation_path.read_bytes()
    simulation = json.loads(raw)
    _validate_simulation(simulation)
    complete: list[dict[str, Any]] = []
    unevaluable_count = 0
    for row in simulation["results"]:
        status = row.get("status")
        if status == "complete":
            if _finite(row.get("net_return")) is None:
                raise ValueError("complete simulation result has invalid net_return")
            complete.append(row)
        elif status == "unevaluable":
            unevaluable_count += 1
        else:
            raise ValueError("simulation result has unsupported status")
    returns = [_finite(row["net_return"]) for row in complete]
    observation_count = len(returns)
    eligible = observation_count >= _MIN_OBSERVATIONS
    return {
        "schema_v": 1,
        "kind": "core_mtf_trial_ledger_record",
        "selection_permitted": False,
        "trial_id": trial_id,
        "hypothesis": hypothesis,
        "parameters": parameters,
        "parameters_sha256": hashlib.sha256(parameters_json.encode()).hexdigest(),
        "declared_trial_count": declared_trial_count,
        "prior_trial_registry_sha256": _records_hash(prior_records),
        "recorded_at_utc": recorded_at.astimezone(timezone.utc).isoformat(),
        "simulation_source": {
            "path": str(simulation_path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "ledger_sha256": simulation["ledger_sha256"],
            "bar_data_sha256": simulation["bar_data_sha256"],
        },
        "outcome_accounting": {
            "complete_simulated_outcomes": observation_count,
            "unevaluable_outcomes": unevaluable_count,
            "mean_net_return": sum(returns) / observation_count if returns else None,
            "minimum_observations": _MIN_OBSERVATIONS,
            "statistically_eligible": eligible,
            "eligibility_reason": (
                "minimum_observations_met" if eligible else "insufficient_observations"
            ),
        },
    }


def write_trial_record(output_path: Path, record: dict[str, Any]) -> None:
    """Atomically write an already validated immutable trial record."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    temporary.replace(output_path)


def load_registry(registry_path: Path) -> list[dict[str, Any]]:
    """Load an append-only JSONL registry, rejecting malformed rows."""
    if not registry_path.exists():
        return []
    return _parse_registry(registry_path.read_text())


def _parse_registry(raw: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("trial registry contains a non-object row")
        records.append(row)
    _validate_records(records)
    return records


def _validate_records(records: list[dict[str, Any]]) -> None:
    for index, row in enumerate(records):
        if row.get("kind") != "core_mtf_trial_ledger_record":
            raise ValueError("trial registry has unexpected record kind")
        if not isinstance(row.get("trial_id"), str) or not row["trial_id"]:
            raise ValueError("trial registry has invalid trial_id")
        if any(record["trial_id"] == row["trial_id"] for record in records[:index]):
            raise ValueError("trial registry has duplicate trial_id")
        if row.get("declared_trial_count") != index + 1:
            raise ValueError("trial registry has non-contiguous trial count")
        if row.get("prior_trial_registry_sha256") != _records_hash(records[:index]):
            raise ValueError("trial registry has broken hash chain")


def record_and_append(
    simulation_path: Path, registry_path: Path, output_path: Path, **kwargs: Any
) -> dict[str, Any]:
    """Create and append a trial in one locked, hash-chained transaction."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with registry_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            record = record_trial(
                simulation_path, prior_records=_parse_registry(handle.read()), **kwargs
            )
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            try:
                write_trial_record(output_path, record)
            except OSError as exc:
                raise RuntimeError(
                    f"trial {record['trial_id']} committed to {registry_path}; "
                    f"regenerate sidecar {output_path} from registry"
                ) from exc
            return record
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_simulation(simulation: Any) -> None:
    if not isinstance(simulation, dict):
        raise TypeError("simulation must be an object")
    if simulation.get("kind") != "core_mtf_simulated_ohlcv_replay":
        raise ValueError("unexpected simulation kind")
    if simulation.get("execution_claims_permitted") is not False:
        raise ValueError("simulation permits execution claims")
    if simulation.get("execution_evidence") != "not_bid_ask_or_quote_data":
        raise ValueError("simulation lacks execution-evidence limitation")
    if not isinstance(simulation.get("results"), list):
        raise ValueError("simulation lacks results")
    for field in ("ledger_sha256", "bar_data_sha256"):
        if not isinstance(simulation.get(field), str) or not simulation[field]:
            raise ValueError(f"simulation lacks {field}")


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _records_hash(records: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _canonical_json(value: Any) -> str:
    """Return deterministic JSON, rejecting non-finite or unsupported values."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("parameters must be canonical finite JSON") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulation", required=True, type=Path)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--hypothesis", required=True)
    parser.add_argument("--parameters-json", required=True)
    parser.add_argument("--declared-trial-count", required=True, type=int)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    parameters = json.loads(args.parameters_json)
    if not isinstance(parameters, dict):
        raise ValueError("parameters-json must be an object")
    record = record_and_append(
        args.simulation,
        args.registry,
        args.output,
        trial_id=args.trial_id,
        hypothesis=args.hypothesis,
        parameters=parameters,
        declared_trial_count=args.declared_trial_count,
        recorded_at=datetime.now(timezone.utc),
    )
    outcome_count = record["outcome_accounting"]["complete_simulated_outcomes"]
    print(f"wrote {args.output}: {outcome_count} outcomes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
