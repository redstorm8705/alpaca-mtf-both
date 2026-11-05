from __future__ import annotations

from research.f6_guard_activation_probe import SOURCE_FILES, run_probe


def _exact_facts() -> dict:
    return {
        "deployed_sha": "a" * 40,
        "profile": "paper",
        "host_identity": "oci-paper-redacted",
        "guard_flag": False,
        "f6_flag": False,
        "ledger_sha256": "a" * 64,
        "ledger_backup_sha256": "b" * 64,
        "pending_buy_journal": "absent",
        "reduction_journal": "absent",
        "positions": [{"symbol": "NVDA", "qty": "2", "side": "long"}],
        "open_orders": [],
        "ownership": [
            {
                "symbol": "NVDA",
                "net": 2.0,
                "drift": 0.0,
                "tiers": {"qhm": 2.0},
            }
        ],
        "submit_count": 0,
        "cancel_count": 0,
        "replace_count": 0,
        "close_count": 0,
    }


def test_f6_guard_activation_probe_is_source_bound_and_passes() -> None:
    artifact = run_probe(oci_facts=_exact_facts())
    assert artifact["overall"] == "PASS"
    assert artifact["configuration"] == {
        "ownership_guard_enforce": True,
        "forever6_enabled": False,
        "paper": True,
    }
    assert set(artifact["candidate"]["source_sha256"]) == set(SOURCE_FILES)
    assert {row["boundary"] for row in artifact["hermetic_phase"]["cases"]} == {
        "full",
        "partial",
        "limit",
        "stop",
    }
    assert all(row["status"] == "PASS" for row in artifact["hermetic_phase"]["cases"])
    assert artifact["live_mutation_counts"] == {
        "submit": 0,
        "cancel": 0,
        "replace": 0,
        "close": 0,
    }


def test_f6_guard_activation_probe_blocks_without_oci_facts() -> None:
    artifact = run_probe()
    assert artifact["overall"] == "FAIL"
    assert artifact["oci_read_only"]["reconciliation_status"] == "MISSING_OCI_FACTS"


def test_f6_guard_activation_probe_blocks_canonical_f6_mismatch() -> None:
    facts = _exact_facts()
    facts["ownership"] = [
        {
            "symbol": "NVDA",
            "net": 3.0,
            "drift": 0.0,
            "tiers": {"qhm": 2.0, "forever6": 1.0},
        }
    ]
    artifact = run_probe(oci_facts=facts)
    assert artifact["overall"] == "PREDEPLOY_RECONCILE_REQUIRED"
    assert artifact["oci_read_only"]["reconciliation_status"] == (
        "PREDEPLOY_RECONCILE_REQUIRED"
    )
    assert artifact["oci_read_only"]["protected_mismatches"] == [
        {
            "symbol": "NVDA",
            "ledger_net": 3.0,
            "broker_net": 2.0,
            "protected_qty": 3.0,
            "drift": 0.0,
            "reason": "net",
        }
    ]


def test_f6_guard_activation_probe_rejects_live_profile_or_enabled_f6() -> None:
    for field, value in (("profile", "live"), ("f6_flag", True)):
        facts = _exact_facts()
        facts[field] = value
        artifact = run_probe(oci_facts=facts)
        assert artifact["overall"] == "FAIL"


def test_f6_guard_activation_probe_rejects_missing_mutation_counts() -> None:
    facts = _exact_facts()
    for key in ("submit_count", "cancel_count", "replace_count", "close_count"):
        facts.pop(key)
    artifact = run_probe(oci_facts=facts)
    assert artifact["overall"] == "FAIL"


def test_f6_guard_activation_probe_blocks_protected_drift() -> None:
    for drift in (1.0, float("inf"), "bad", None):
        facts = _exact_facts()
        facts["ownership"][0]["drift"] = drift
        artifact = run_probe(oci_facts=facts)
        assert artifact["overall"] == "PREDEPLOY_RECONCILE_REQUIRED"
        assert artifact["oci_read_only"]["protected_mismatches"][0]["reason"] == (
            "drift"
        )


def test_invalid_environment_is_not_downgraded_to_reconcile_required() -> None:
    facts = _exact_facts()
    facts["f6_flag"] = True
    facts["ownership"][0]["net"] = 3.0
    artifact = run_probe(oci_facts=facts)
    assert artifact["overall"] == "FAIL"


def test_f6_guard_activation_probe_blocks_qhm_mismatch() -> None:
    facts = _exact_facts()
    facts["ownership"][0]["net"] = 3.0
    artifact = run_probe(oci_facts=facts)
    assert artifact["overall"] == "PREDEPLOY_RECONCILE_REQUIRED"
    assert artifact["oci_read_only"]["protected_mismatches"][0]["protected_qty"] == 2.0


def test_f6_guard_activation_probe_cli_nonzero_when_reconcile_required(
    tmp_path, monkeypatch
) -> None:
    import json
    import sys

    from research import f6_guard_activation_probe as probe

    facts = _exact_facts()
    facts["ownership"][0]["net"] = 3.0
    facts_path = tmp_path / "facts.json"
    output_path = tmp_path / "artifact.json"
    facts_path.write_text(json.dumps(facts), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "f6_guard_activation_probe.py",
            "--oci-facts",
            str(facts_path),
            "--output",
            str(output_path),
        ],
    )
    assert probe.main() == 1
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["overall"] == "PREDEPLOY_RECONCILE_REQUIRED"
