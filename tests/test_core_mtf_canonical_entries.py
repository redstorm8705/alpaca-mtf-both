import hashlib
import json

import pytest

from research.core_mtf_canonical_entries import build_canonical_intake


def _order(order_id="one", **overrides):
    row = {
        "id": order_id,
        "client_order_id": "mtf-TEST-buy-abc",
        "symbol": "TEST",
        "position_intent": "buy_to_open",
        "side": "buy",
        "type": "market",
        "status": "filled",
        "submitted_at": "2026-04-01T14:00:00Z",
        "filled_at": "2026-04-01T14:00:01Z",
        "qty": "2",
        "filled_qty": "2",
        "filled_avg_price": "100.25",
    }
    row.update(overrides)
    return row


def _snapshot(tmp_path, orders):
    raw = json.dumps(orders, sort_keys=True, separators=(",", ":")).encode()
    payload = {
        "schema_v": 1,
        "kind": "alpaca_paper_order_history_snapshot",
        "access_mode": "read_only_https_get",
        "ledger_event_identity_claims_permitted": False,
        "order_data_sha256": hashlib.sha256(raw).hexdigest(),
        "order_count": len(orders),
        "retrieval": {
            "earliest_submitted_at_utc": "2026-04-01T14:00:00+00:00",
            "latest_submitted_at_utc": "2026-09-19T14:00:00+00:00",
        },
        "orders": orders,
    }
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(payload))
    return path


def test_admits_exact_long_and_short_entries(tmp_path):
    orders = [
        _order(),
        _order(
            "two",
            client_order_id="mtf-TEST-sell-def",
            position_intent="sell_to_open",
            side="sell",
            qty="3",
            filled_qty="1",
            status="canceled",
        ),
    ]
    result = build_canonical_intake(_snapshot(tmp_path, orders))

    assert result["execution_claims_permitted"] is False
    assert result["outcome_claims_permitted"] is False
    assert [row["direction"] for row in result["entries"]] == ["long", "short"]
    assert result["entries"][1]["fill_completeness"] == "partial"
    assert result["entries"][1]["canceled_quantity"] == "2"


def test_counts_candidate_exclusions_without_admitting_them(tmp_path):
    orders = [
        _order("one", position_intent="sell_to_close", side="sell"),
        _order("two", type="stop"),
        _order("other", client_order_id="DT-TEST-buy-x"),
    ]
    result = build_canonical_intake(_snapshot(tmp_path, orders))

    assert result["accounting"]["mtf_namespace_candidates"] == 2
    assert result["accounting"]["broker_exact_entries_admitted"] == 0
    assert result["accounting"]["exclusions_by_reason"] == {
        "non_entry_position_intent": 1,
        "unsupported_entry_order_type": 1,
    }


def test_legacy_intraday_ledger_row_is_not_schema_proof(tmp_path):
    snapshot = _snapshot(tmp_path, [_order()])
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "entry",
                "trade_mode": "intraday",
                "symbol": "TEST",
                "entry_order_id": "one",
            }
        )
        + "\n"
    )
    result = build_canonical_intake(snapshot, ledger_path=ledger)

    assert result["ledger_source"]["schema_proven_rows"] == 0
    assert result["ledger_source"]["unproven_rows"] == 1
    assert result["entries"][0]["provenance"] == ["broker_exact"]


def test_exact_schema_proof_joins_only_by_broker_id(tmp_path):
    snapshot = _snapshot(tmp_path, [_order()])
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "entry",
                "strategy_tier": "core_mtf",
                "entry_order_id": "one",
                "symbol": "TEST",
            }
        )
        + "\n"
    )
    result = build_canonical_intake(snapshot, ledger_path=ledger)

    assert result["ledger_source"]["schema_proven_rows"] == 1
    assert result["entries"][0]["provenance"] == [
        "broker_exact",
        "ledger_schema_proven",
    ]


def test_duplicate_ids_tampered_hash_and_unknown_proof_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="duplicate broker"):
        build_canonical_intake(_snapshot(tmp_path, [_order(), _order()]))

    snapshot = _snapshot(tmp_path, [_order()])
    payload = json.loads(snapshot.read_text())
    payload["orders"][0]["symbol"] = "TAMPERED"
    snapshot.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="hash mismatch"):
        build_canonical_intake(snapshot)

    snapshot = _snapshot(tmp_path, [_order()])
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "entry",
                "strategy_tier": "core_mtf",
                "entry_order_id": "unknown",
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="unknown Core MTF"):
        build_canonical_intake(snapshot, ledger_path=ledger)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"filled_at": "2026-04-01T14:00:01"}, "invalid_filled_at"),
        ({"qty": "1", "filled_qty": "2"}, "filled_quantity_exceeds_requested"),
        ({"side": "sell"}, "intent_side_contradiction"),
        ({"filled_qty": "0"}, "zero_fill_status_contradiction"),
    ],
)
def test_source_integrity_defects_fail_closed(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        build_canonical_intake(_snapshot(tmp_path, [_order(**overrides)]))


def test_ledger_exact_id_with_contradictory_symbol_fails_closed(tmp_path):
    snapshot = _snapshot(tmp_path, [_order()])
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "entry",
                "strategy_tier": "core_mtf",
                "entry_order_id": "one",
                "symbol": "WRONG",
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="contradicts broker entry symbol"):
        build_canonical_intake(snapshot, ledger_path=ledger)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"status": "invented"}, "unsupported_filled_order_status"),
        (
            {"status": "filled", "qty": "2", "filled_qty": "1"},
            "filled_status_quantity_contradiction",
        ),
        (
            {"status": "partially_filled", "qty": "2", "filled_qty": "2"},
            "partial_status_quantity_contradiction",
        ),
        (
            {"filled_at": "2026-04-01T13:59:59Z"},
            "filled_at_precedes_submitted_at",
        ),
    ],
)
def test_status_quantity_and_time_contradictions_fail_closed(
    tmp_path, overrides, message
):
    with pytest.raises(ValueError, match=message):
        build_canonical_intake(_snapshot(tmp_path, [_order(**overrides)]))


def test_conflicting_ledger_aliases_fail_closed(tmp_path):
    snapshot = _snapshot(tmp_path, [_order()])
    for row, message in [
        (
            {
                "event": "entry",
                "strategy_tier": "core_mtf",
                "tier": "other",
                "entry_order_id": "one",
            },
            "conflicting tier aliases",
        ),
        (
            {
                "event": "entry",
                "strategy_tier": "core_mtf",
                "entry_order_id": "one",
                "broker_order_id": "other",
            },
            "conflicting broker order aliases",
        ),
    ]:
        ledger = tmp_path / "events.jsonl"
        ledger.write_text(json.dumps(row) + "\n")
        with pytest.raises(ValueError, match=message):
            build_canonical_intake(snapshot, ledger_path=ledger)


def test_fractional_quantities_are_preserved_without_binary_float_drift(tmp_path):
    result = build_canonical_intake(
        _snapshot(
            tmp_path,
            [
                _order(
                    status="canceled",
                    qty="0.3",
                    filled_qty="0.1",
                    filled_avg_price="100.1234",
                )
            ],
        )
    )
    entry = result["entries"][0]
    assert entry["requested_quantity"] == "0.3"
    assert entry["filled_quantity"] == "0.1"
    assert entry["unfilled_quantity"] == "0.2"
    assert entry["canceled_quantity"] == "0.2"


@pytest.mark.parametrize("status", ["canceled", "expired", "done_for_day", "replaced"])
def test_terminal_zero_fill_is_counted_as_exclusion(tmp_path, status):
    result = build_canonical_intake(
        _snapshot(
            tmp_path,
            [
                _order(
                    status=status,
                    filled_qty="0",
                    filled_at=None,
                    filled_avg_price=None,
                )
            ],
        )
    )
    assert result["accounting"]["broker_exact_entries_admitted"] == 0
    assert result["accounting"]["exclusions_by_reason"] == {
        "zero_fill_terminal_entry": 1
    }


def test_partially_filled_zero_fill_remains_a_hard_contradiction(tmp_path):
    with pytest.raises(ValueError, match="zero_fill_status_contradiction"):
        build_canonical_intake(
            _snapshot(
                tmp_path,
                [
                    _order(
                        status="partially_filled",
                        filled_qty="0",
                        filled_at=None,
                        filled_avg_price=None,
                    )
                ],
            )
        )
