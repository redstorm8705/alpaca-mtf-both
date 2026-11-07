#!/usr/bin/env python3
"""Source-bound, broker-hermetic proof for F6 ownership-guard activation.

The executable phase uses the real broker wrappers with an in-memory client and
temporary ownership journals.  The optional OCI facts are read-only inputs;
this program never receives credentials and never calls a live broker.

Signed: OpenAI Codex, 2026-11-05.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import config
from execution import broker
from execution import ownership_guard as guard
from execution import pending_protected_buys as pending
from execution.ownership_ledger_codec import dumps

ROOT = Path(__file__).resolve().parent.parent
CHANGE_ID = "F6-GUARD-1104"
SCHEMA_VERSION = 1
SOURCE_FILES = (
    "config.py",
    "execution/broker.py",
    "execution/ownership_guard.py",
    "execution/order_admission.py",
    "execution/risk_manager.py",
    "execution/stop_protection.py",
    "research/f6_guard_activation_probe.py",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _ledger(*, swing: float = 0.0, qhm: float = 0.0, f6: float = 0.0) -> dict:
    quantities = {
        "intraday": swing,
        "qhm": qhm,
        "forever6": f6,
        "daytrade": 0.0,
    }
    total = sum(quantities.values())
    return {
        "version": 1,
        "last_reconciled_utc": "2026-11-05T00:00:00+00:00",
        "positions": {
            "AAPL": {
                "alpaca_net_qty": total,
                "drift": 0.0,
                "tiers": {
                    tier: {
                        "qty": qty,
                        "avg_cost": 100.0 if qty else 0.0,
                        "last_fill_id": f"probe-{tier}" if qty else None,
                    }
                    for tier, qty in quantities.items()
                },
            }
        },
    }


def _request_qty(call: Any) -> int | None:
    if call is None:
        return None
    request = call.args[0] if call.args else call.kwargs.get("order_data")
    try:
        return int(request.qty)
    except (AttributeError, TypeError, ValueError):
        return None


def _case(
    name: str,
    boundary: str,
    requested_qty: int,
    expected_result: str,
    actual_result: str,
    client: mock.Mock,
    *,
    guard_result: guard.GuardResult | None = None,
) -> dict[str, Any]:
    submit_count = client.submit_order.call_count + client.close_position.call_count
    return {
        "name": name,
        "boundary": boundary,
        "requested_qty": requested_qty,
        "allowed_qty": None if guard_result is None else guard_result.qty,
        "guard_verdict": None if guard_result is None else guard_result.action,
        "guard_reason": None if guard_result is None else guard_result.reason,
        "expected_result": expected_result,
        "actual_result": actual_result,
        "broker_submit_count": submit_count,
        "submitted_qty": _request_qty(client.submit_order.call_args),
        "status": "PASS" if expected_result == actual_result else "FAIL",
    }


def run_probe(*, oci_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    candidate_configuration = {
        "ownership_guard_enforce": bool(config.OWNERSHIP_GUARD_ENFORCE),
        "forever6_enabled": bool(config.FOREVER6_ENABLED),
        "paper": True,
    }
    with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
        root = Path(temp_dir)
        client = mock.Mock()
        client.submit_order.return_value = SimpleNamespace(id="hermetic-order")
        position = SimpleNamespace(qty="3", side="long")
        client.get_open_position.return_value = position
        stack.enter_context(mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True))
        stack.enter_context(mock.patch.object(config, "FOREVER6_ENABLED", False))
        stack.enter_context(
            mock.patch.object(guard, "_LEDGER_PATH", root / "ledger.json")
        )
        stack.enter_context(
            mock.patch.object(guard, "_LEDGER_BAK_PATH", root / "ledger.bak.json")
        )
        stack.enter_context(
            mock.patch.object(
                guard, "_PROTECTED_WATERMARK_PATH", root / "protected-watermark.json"
            )
        )
        stack.enter_context(
            mock.patch.object(guard, "page_floor_blind", return_value=None)
        )
        stack.enter_context(
            mock.patch.object(pending, "JOURNAL_PATH", root / "pending.json")
        )
        stack.enter_context(
            mock.patch.object(pending, "LOCK_PATH", root / ".pending.lock")
        )
        stack.enter_context(
            mock.patch.object(broker, "_get_trading_client", return_value=client)
        )
        stack.enter_context(
            mock.patch.object(broker, "_cohold_bounded_close", return_value=None)
        )
        stack.enter_context(
            mock.patch.object(broker, "_cohold_own_cap", return_value=None)
        )

        def write_current(value: dict) -> None:
            guard._LEDGER_PATH.write_bytes(dumps(value, target=1))
            symbols = sorted(
                symbol
                for symbol in value.get("positions", {})
                if guard.protected_floor(value, symbol) > 0
            )
            guard._PROTECTED_WATERMARK_PATH.write_text(
                json.dumps({"schema": 1, "symbols": symbols}), encoding="utf-8"
            )

        def write_backup(value: dict) -> None:
            guard._LEDGER_BAK_PATH.write_bytes(dumps(value, target=1))
            symbols = sorted(
                symbol
                for symbol in value.get("positions", {})
                if guard.protected_floor(value, symbol) > 0
            )
            guard._PROTECTED_WATERMARK_PATH.write_text(
                json.dumps({"schema": 1, "symbols": symbols}), encoding="utf-8"
            )

        def reset_client() -> None:
            client.reset_mock()
            client.submit_order.return_value = SimpleNamespace(id="hermetic-order")
            client.get_open_position.return_value = position

        write_current(_ledger(swing=1))
        reset_client()
        with mock.patch.object(
            broker, "get_open_position", side_effect=RuntimeError("timeout")
        ):
            result = broker.submit_limit_order(
                "AAPL",
                1,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-limit",
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 1, "sell", None)
        cases.append(
            _case(
                "limit_nonprotected_position_error",
                "limit",
                1,
                "SUBMITTED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        write_current(_ledger(qhm=1))
        reset_client()
        with mock.patch.object(
            broker, "get_open_position", side_effect=RuntimeError("timeout")
        ):
            result = broker.submit_limit_order(
                "AAPL",
                1,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-protected",
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 1, "sell", None)
        cases.append(
            _case(
                "limit_protected_position_error",
                "limit",
                1,
                "BLOCKED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        write_backup(_ledger(qhm=1))
        reset_client()
        with mock.patch.object(
            broker, "get_open_position", side_effect=RuntimeError("timeout")
        ):
            result = broker.submit_limit_order(
                "AAPL",
                1,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-protected-backup",
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 1, "sell", None)
        cases.append(
            _case(
                "limit_protected_backup_position_error",
                "limit",
                1,
                "BLOCKED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        write_backup(_ledger(swing=1))
        reset_client()
        with mock.patch.object(
            broker, "get_open_position", side_effect=RuntimeError("timeout")
        ):
            result = broker.submit_limit_order(
                "AAPL",
                1,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-backup",
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 1, "sell", None)
        cases.append(
            _case(
                "limit_valid_nonprotected_backup",
                "limit",
                1,
                "SUBMITTED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        # A one-generation .bak can predate a 0→protected transition.  The
        # monotonic watermark must therefore block every reducing wrapper when
        # current ownership is unreadable, even though the stale backup says
        # the symbol was ordinary Swing stock.
        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        write_backup(_ledger(swing=1))
        guard._PROTECTED_WATERMARK_PATH.write_text(
            json.dumps({"schema": 1, "symbols": ["AAPL"]}), encoding="utf-8"
        )

        reset_client()
        result = broker.submit_day_stop_order(
            "AAPL", 1, "sell", 90.0, tier="intraday"
        )
        cases.append(
            _case(
                "stop_stale_backup_cannot_forget_floor",
                "stop",
                1,
                "BLOCKED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
            )
        )

        reset_client()
        result = broker.partial_close_position("AAPL", 1, tier="intraday")
        cases.append(
            _case(
                "partial_stale_backup_cannot_forget_floor",
                "partial",
                1,
                "BLOCKED",
                "SUBMITTED" if result else "BLOCKED",
                client,
            )
        )

        reset_client()
        result = broker.close_position("AAPL", tier="intraday")
        cases.append(
            _case(
                "full_stale_backup_cannot_forget_floor",
                "full",
                1,
                "BLOCKED",
                "SUBMITTED" if result else "BLOCKED",
                client,
            )
        )

        guard._LEDGER_PATH.write_text("{corrupt", encoding="utf-8")
        guard._LEDGER_BAK_PATH.write_text("{also-corrupt", encoding="utf-8")
        guard._PROTECTED_WATERMARK_PATH.write_text("{also-corrupt", encoding="utf-8")
        reset_client()
        with mock.patch.object(
            broker, "get_open_position", side_effect=RuntimeError("timeout")
        ):
            result = broker.submit_limit_order(
                "AAPL",
                1,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-double-blind",
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 1, "sell", None)
        cases.append(
            _case(
                "limit_both_ledgers_invalid",
                "limit",
                1,
                "BLOCKED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        write_current(_ledger(swing=1))
        guard._LEDGER_BAK_PATH.unlink(missing_ok=True)
        pending.reserve(
            "AAPL",
            1,
            "F6-AAPL-b-probe-pending",
            0.0,
            limit_price=100.0,
            extended_hours=True,
        )
        reset_client()
        with mock.patch.object(
            broker, "get_open_position", side_effect=RuntimeError("timeout")
        ):
            result = broker.submit_limit_order(
                "AAPL",
                1,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-pending",
            )
        cases.append(
            _case(
                "limit_pending_protected_buy",
                "limit",
                1,
                "BLOCKED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
            )
        )
        pending.JOURNAL_PATH.unlink()

        write_current(_ledger(swing=1, qhm=2))
        reset_client()
        with mock.patch.object(broker, "get_open_position", return_value=position):
            result = broker.submit_limit_order(
                "AAPL",
                2,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-bounded",
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 2, "sell", 3.0)
        cases.append(
            _case(
                "limit_guard_quantity_smaller",
                "limit",
                2,
                "BLOCKED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        drifted = _ledger(swing=1, qhm=2)
        drifted["positions"]["AAPL"]["drift"] = 1.0
        write_current(drifted)
        reset_client()
        with mock.patch.object(broker, "get_open_position", return_value=position):
            result = broker.submit_limit_order(
                "AAPL",
                1,
                "sell",
                100.0,
                extended_hours=True,
                tier="intraday",
                client_order_id="IN-AAPL-s-probe-drift",
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 1, "sell", 3.0)
        cases.append(
            _case(
                "limit_protected_ledger_drift",
                "limit",
                1,
                "BLOCKED",
                "SUBMITTED" if result is not None else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        write_current(_ledger(swing=1, qhm=2))
        reset_client()
        with mock.patch.object(broker, "get_open_position", return_value=position):
            result = broker.partial_close_position("AAPL", 2, tier="intraday")
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 2, "sell", 3.0)
        cases.append(
            _case(
                "partial_cohold_bounded",
                "partial",
                2,
                "SUBMITTED_1",
                f"SUBMITTED_{_request_qty(client.submit_order.call_args)}"
                if result
                else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        reset_client()
        with mock.patch.object(broker, "get_open_position", return_value=position):
            result = broker.submit_day_stop_order(
                "AAPL", 2, "sell", 90.0, tier="intraday"
            )
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 2, "sell", 3.0)
        cases.append(
            _case(
                "stop_cohold_bounded",
                "stop",
                2,
                "SUBMITTED_1",
                f"SUBMITTED_{_request_qty(client.submit_order.call_args)}"
                if result
                else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

        reset_client()
        with mock.patch.object(broker, "get_open_position", return_value=position):
            result = broker.close_position("AAPL", tier="intraday")
        verdict = guard.check_never_sell_floor("AAPL", "intraday", 3, "sell", 3.0)
        cases.append(
            _case(
                "full_close_cohold_bounded",
                "full",
                3,
                "SUBMITTED_1",
                f"SUBMITTED_{_request_qty(client.submit_order.call_args)}"
                if result
                else "BLOCKED",
                client,
                guard_result=verdict,
            )
        )

    source_hashes = {path: _sha(ROOT / path) for path in SOURCE_FILES}
    source_digest = hashlib.sha256(
        json.dumps(source_hashes, sort_keys=True).encode("utf-8")
    ).hexdigest()
    facts = dict(oci_facts or {})
    broker_qty = {
        str(row.get("symbol")): float(row.get("qty", 0.0))
        for row in facts.get("positions", [])
        if isinstance(row, dict) and row.get("symbol")
    }
    ownership_rows = {
        str(row.get("symbol")): row
        for row in facts.get("ownership", [])
        if isinstance(row, dict) and row.get("symbol")
    }
    protected_mismatches: list[dict[str, Any]] = []
    for symbol, row in ownership_rows.items():
        tiers = row.get("tiers", {}) if isinstance(row.get("tiers"), dict) else {}
        protected = float(tiers.get("qhm", 0.0) or 0.0) + float(
            tiers.get(
                "forever_6",
                tiers.get("forever6", tiers.get("f6", 0.0)),
            )
            or 0.0
        )
        try:
            drift = float(row.get("drift", 0.0))
        except (TypeError, ValueError):
            drift = float("nan")
        net_mismatch = (
            abs(float(row.get("net", 0.0) or 0.0) - broker_qty.get(symbol, 0.0)) > 1e-6
        )
        drift_mismatch = not (drift == drift and abs(drift) <= 1e-6)
        if protected > 0 and (net_mismatch or drift_mismatch):
            protected_mismatches.append(
                {
                    "symbol": symbol,
                    "ledger_net": row.get("net"),
                    "broker_net": broker_qty.get(symbol, 0.0),
                    "protected_qty": protected,
                    "drift": row.get("drift"),
                    "reason": (
                        "net_and_drift"
                        if net_mismatch and drift_mismatch
                        else "net"
                        if net_mismatch
                        else "drift"
                    ),
                }
            )
    mutation_counts = {
        "submit": int(facts.get("submit_count", 0)),
        "cancel": int(facts.get("cancel_count", 0)),
        "replace": int(facts.get("replace_count", 0)),
        "close": int(facts.get("close_count", 0)),
    }
    reconciliation_status = (
        "MISSING_OCI_FACTS"
        if not facts
        else "PREDEPLOY_RECONCILE_REQUIRED"
        if protected_mismatches
        else "EXACT"
    )
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "change_id": CHANGE_ID,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate": {
            "head_commit": _git("rev-parse", "HEAD"),
            "working_tree_source_digest": source_digest,
            "source_sha256": source_hashes,
        },
        "configuration": candidate_configuration,
        "oci_read_only": {
            **facts,
            "protected_mismatches": protected_mismatches,
            "reconciliation_status": reconciliation_status,
        },
        "hermetic_phase": {
            "network_capable": False,
            "live_credentials_loaded": False,
            "reduction_serializer": "hermetic-only while FOREVER6_ENABLED=False",
            "cases": cases,
        },
        "live_mutation_counts": mutation_counts,
        "post_deploy": {
            "required": True,
            "startup_reconcile": "PENDING_DEPLOY",
            "rollback_precondition": (
                "F6 remains off; do not disable ownership enforcement while any QHM/F6 "
                "claim or protected-buy reservation exists"
            ),
        },
    }
    required_oci = {
        "deployed_sha",
        "profile",
        "host_identity",
        "guard_flag",
        "f6_flag",
        "ledger_sha256",
        "ledger_backup_sha256",
        "pending_buy_journal",
        "reduction_journal",
        "positions",
        "open_orders",
        "ownership",
        "submit_count",
        "cancel_count",
        "replace_count",
        "close_count",
    }
    oci_complete = required_oci.issubset(facts)
    oci_environment_valid = (
        oci_complete
        and facts.get("profile") == "paper"
        and facts.get("f6_flag") is False
        and bool(facts.get("deployed_sha"))
    )
    oci_exact = oci_environment_valid and not protected_mismatches
    proof_passes = (
        all(row["status"] == "PASS" for row in cases)
        and candidate_configuration
        == {
            "ownership_guard_enforce": True,
            "forever6_enabled": False,
            "paper": True,
        }
        and all(value == 0 for value in mutation_counts.values())
        and oci_exact
    )
    if proof_passes:
        artifact["overall"] = "PASS"
    elif (
        all(row["status"] == "PASS" for row in cases)
        and candidate_configuration
        == {
            "ownership_guard_enforce": True,
            "forever6_enabled": False,
            "paper": True,
        }
        and all(value == 0 for value in mutation_counts.values())
        and oci_environment_valid
        and bool(protected_mismatches)
    ):
        artifact["overall"] = "PREDEPLOY_RECONCILE_REQUIRED"
    else:
        artifact["overall"] = "FAIL"
    return artifact


def _write_atomic(path: Path, artifact: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--oci-facts", type=Path)
    args = parser.parse_args()
    facts = json.loads(args.oci_facts.read_text()) if args.oci_facts else {}
    artifact = run_probe(oci_facts=facts)
    _write_atomic(args.output, artifact)
    print(
        json.dumps(
            {
                "overall": artifact["overall"],
                "cases": len(artifact["hermetic_phase"]["cases"]),
            }
        )
    )
    return 0 if artifact["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
