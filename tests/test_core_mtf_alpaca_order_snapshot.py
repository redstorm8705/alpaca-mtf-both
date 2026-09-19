import json
from datetime import datetime, timezone

import pytest

from research.core_mtf_alpaca_order_snapshot import (
    OrderHistoryResult,
    fetch_all_orders,
    write_snapshot,
)

UTC = timezone.utc


def _order(order_id, created_at):
    return {
        "id": order_id,
        "created_at": created_at,
        "submitted_at": created_at,
        "symbol": "TEST",
    }


def test_fetches_two_pages_with_overlap_and_deduplicates_boundary():
    responses = [
        [_order("new", "2026-09-02T00:00:00Z"), _order("old", "2026-09-01T00:00:00Z")],
        [
            _order("old", "2026-09-01T00:00:00Z"),
            _order("older", "2026-08-31T00:00:00Z"),
        ],
        [],
    ]
    seen_urls = []

    def request_fn(request):
        seen_urls.append(request.full_url)
        return json.dumps(responses.pop(0)).encode()

    # Use a small page only by replacing the module constant, so pagination is
    # exercised without manufacturing 500 records.
    import research.core_mtf_alpaca_order_snapshot as module

    original = module._PAGE_SIZE
    module._PAGE_SIZE = 2
    try:
        orders = fetch_all_orders(
            api_key="key", secret_key="secret", request_fn=request_fn
        )
    finally:
        module._PAGE_SIZE = original

    assert [row["id"] for row in orders.orders] == ["older", "old", "new"]
    assert orders.raw_rows_received == 4
    assert orders.overlap_duplicates_removed == 1
    assert len(seen_urls) == 3
    assert "until=" in seen_urls[1]


def test_rejects_a_full_page_that_makes_no_progress():
    response = [_order("same", "2026-09-01T00:00:00Z")]

    def request_fn(_request):
        return json.dumps(response).encode()

    import research.core_mtf_alpaca_order_snapshot as module

    original = module._PAGE_SIZE
    module._PAGE_SIZE = 1
    try:
        with pytest.raises(ValueError, match="made no progress"):
            fetch_all_orders(api_key="key", secret_key="secret", request_fn=request_fn)
    finally:
        module._PAGE_SIZE = original


def test_rejects_a_partial_page_that_makes_no_progress():
    responses = [
        [_order("new", "2026-09-02T00:00:00Z"), _order("same", "2026-09-01T00:00:00Z")],
        [_order("same", "2026-09-01T00:00:00Z")],
    ]

    def request_fn(_request):
        return json.dumps(responses.pop(0)).encode()

    import research.core_mtf_alpaca_order_snapshot as module

    original = module._PAGE_SIZE
    module._PAGE_SIZE = 2
    try:
        with pytest.raises(ValueError, match="made no progress"):
            fetch_all_orders(api_key="key", secret_key="secret", request_fn=request_fn)
    finally:
        module._PAGE_SIZE = original


def test_rejects_an_unordered_provider_page():
    def request_fn(_request):
        return json.dumps([
            _order("old", "2026-09-01T00:00:00Z"),
            _order("new", "2026-09-02T00:00:00Z"),
        ]).encode()

    with pytest.raises(ValueError, match="not descending"):
        fetch_all_orders(api_key="key", secret_key="secret", request_fn=request_fn)


def test_rejects_a_cursor_that_does_not_move_backward():
    responses = [
        [_order("one", "2026-09-01T00:00:00Z")],
        [_order("two", "2026-09-01T00:00:00Z")],
    ]

    def request_fn(_request):
        return json.dumps(responses.pop(0)).encode()

    import research.core_mtf_alpaca_order_snapshot as module

    original = module._PAGE_SIZE
    module._PAGE_SIZE = 1
    try:
        with pytest.raises(ValueError, match="did not move backward"):
            fetch_all_orders(api_key="key", secret_key="secret", request_fn=request_fn)
    finally:
        module._PAGE_SIZE = original


def test_snapshot_binds_hash_and_prohibits_ledger_identity_claims(tmp_path):
    path = tmp_path / "orders.json"
    artifact = write_snapshot(
        path,
        OrderHistoryResult(
            orders=[_order("one", "2026-09-01T00:00:00Z")],
            page_count=1,
            raw_rows_received=1,
            overlap_duplicates_removed=0,
        ),
        fetched_at=datetime(2026, 9, 2, tzinfo=UTC),
    )

    persisted = json.loads(path.read_text())
    assert artifact["ledger_event_identity_claims_permitted"] is False
    assert persisted["order_data_sha256"] == artifact["order_data_sha256"]
    assert persisted["order_count"] == 1
    assert (
        persisted["retrieval"]["earliest_submitted_at_utc"]
        == "2026-09-01T00:00:00+00:00"
    )
