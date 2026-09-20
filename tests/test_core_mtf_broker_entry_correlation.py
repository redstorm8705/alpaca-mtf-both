import json
from datetime import datetime, timezone

import pytest

from research.core_mtf_broker_entry_correlation import (
    correlate_broker_entries,
    write_correlation_artifact,
)
from research.core_mtf_replay_events import CoreMTFShortEvent


UTC = timezone.utc


def _event():
    return CoreMTFShortEvent(
        decision_time=datetime(2026, 9, 1, 15, 0, tzinfo=UTC),
        symbol="TEST",
        logged_entry_price=100,
        stop=105,
        target=95,
        source_file="ledger.jsonl",
        source_line=1,
        score=10,
        mri_level="NORMAL",
    )


def _order(
    order_id="one",
    *,
    submitted_at="2026-09-01T14:59:30Z",
    side="sell",
    coid="IN-TEST-s-x",
    filled_qty="1",
):
    return {
        "id": order_id,
        "symbol": "TEST",
        "side": side,
        "client_order_id": coid,
        "submitted_at": submitted_at,
        "status": "filled",
        "qty": "2",
        "filled_qty": filled_qty,
        "filled_avg_price": "100",
    }


def test_unique_filled_order_is_temporal_correlation_not_identity():
    result = correlate_broker_entries((_event(),), [_order()])[0]

    assert result.status == "unique_temporal_legacy_sell_order"
    assert result.order_id == "one"
    assert "identity" not in result.status
    assert result.entry_intent_proven is False
    assert result.fill_completeness == "partial"


def test_partial_fill_is_retained_even_when_broker_order_is_cancelled():
    order = _order(filled_qty="1")
    order["status"] = "canceled"

    result = correlate_broker_entries((_event(),), [order])[0]

    assert result.status == "unique_temporal_legacy_sell_order"
    assert result.requested_quantity == 2
    assert result.filled_quantity == 1
    assert result.order_status == "canceled"
    assert result.fill_completeness == "partial"


def test_nearby_sell_stop_has_unknown_entry_intent():
    order = _order(filled_qty="0")
    order["type"] = "stop"
    order["stop_price"] = "99"

    result = correlate_broker_entries((_event(),), [order])[0]

    assert result.status == "unique_temporal_legacy_sell_order"
    assert result.order_type == "stop"
    assert result.entry_intent_proven is False
    assert result.fill_completeness == "none"


def test_future_nonlegacy_and_wrong_side_orders_do_not_match():
    orders = [
        _order(submitted_at="2026-09-01T15:00:01Z"),
        _order(order_id="two", coid="DT-TEST-s-x"),
        _order(order_id="three", side="buy"),
    ]

    result = correlate_broker_entries((_event(),), orders)[0]

    assert result.status == "unmatched"


def test_multiple_qualifying_orders_are_ambiguous():
    result = correlate_broker_entries(
        (
            _event(),
        ),
        [
            _order(),
            _order(order_id="two", submitted_at="2026-09-01T14:59:40Z"),
        ],
    )[0]

    assert result.status == "ambiguous"
    assert result.order_id is None


def test_rejects_tampered_snapshot_before_correlating(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "event": "entry",
                "direction": "short",
                "trade_mode": "intraday",
                "ts": "2026-09-01T15:00:00Z",
                "symbol": "TEST",
                "price": 100,
                "stop": 105,
                "target": 95,
            }
        )
        + "\n"
    )
    snapshot = tmp_path / "orders.json"
    snapshot.write_text(
        json.dumps(
            {
                "kind": "alpaca_paper_order_history_snapshot",
                "access_mode": "read_only_https_get",
                "ledger_event_identity_claims_permitted": False,
                "orders": [_order()],
                "order_data_sha256": "not-a-real-hash",
            }
        )
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        write_correlation_artifact(tmp_path / "out.json", ledger, snapshot)
