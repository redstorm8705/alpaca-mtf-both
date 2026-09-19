import json
from datetime import datetime, timezone

from research.core_mtf_observed_exits import reconcile_observed_exits
from research.core_mtf_replay_events import CoreMTFShortEvent


UTC = timezone.utc


def _candidate(line=1, symbol="TEST", price=100):
    return CoreMTFShortEvent(
        decision_time=datetime(2026, 9, 1, 15, tzinfo=UTC),
        symbol=symbol,
        logged_entry_price=price,
        stop=105,
        target=95,
        source_file="events.jsonl",
        source_line=line,
        score=10,
        mri_level="NORMAL",
    )


def test_links_only_explicit_entry_price_and_quantity_match(tmp_path):
    rows = [
        {"ts": "2026-09-01T15:00:00+00:00", "size": 2},
        {
            "ts": "2026-09-02T15:00:00+00:00",
            "event": "stop_hit",
            "symbol": "TEST",
            "price": 106,
            "size": 2,
            "reason": "overnight breach | entry=$100.00",
        },
    ]
    ledger = tmp_path / "events.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))

    links = reconcile_observed_exits(ledger, [_candidate()])

    assert links[0].status == "verified"
    assert links[0].exit_source_line == 2
    assert links[0].exit_price == 106


def test_does_not_promote_proximity_or_wrong_quantity_to_verified(tmp_path):
    rows = [
        {"ts": "2026-09-01T15:00:00+00:00", "size": 2},
        {
            "ts": "2026-09-02T15:00:00+00:00",
            "event": "exit",
            "symbol": "TEST",
            "price": 99,
            "size": 2,
            "reason": "external_close",
        },
        {
            "ts": "2026-09-02T16:00:00+00:00",
            "event": "stop_hit",
            "symbol": "TEST",
            "price": 106,
            "size": 3,
            "reason": "entry=$100.00",
        },
    ]
    ledger = tmp_path / "events.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))

    links = reconcile_observed_exits(ledger, [_candidate()])

    assert links[0].status == "unmatched"


def test_labels_explicit_partial_exit_without_claiming_full_reconciliation(tmp_path):
    rows = [
        {"ts": "2026-09-01T15:00:00+00:00", "size": 2},
        {
            "ts": "2026-09-02T15:00:00+00:00",
            "event": "stop_hit",
            "symbol": "TEST",
            "price": 106,
            "size": 1,
            "reason": "overnight breach | entry=$100.00",
        },
    ]
    ledger = tmp_path / "events.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))

    links = reconcile_observed_exits(ledger, [_candidate()])

    assert links[0].status == "partial_verified"
    assert links[0].reason == "terminal_reason_entry_partial_quantity_match"


def test_next_same_symbol_entry_bounds_the_match_window(tmp_path):
    rows = [
        {"ts": "2026-09-01T15:00:00+00:00", "size": 1},
        {"ts": "2026-09-02T15:00:00+00:00", "size": 1},
        {
            "ts": "2026-09-03T15:00:00+00:00",
            "event": "stop_hit",
            "symbol": "TEST",
            "price": 106,
            "size": 1,
            "reason": "entry=$100.00",
        },
    ]
    ledger = tmp_path / "events.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    first = _candidate(line=1, price=100)
    second = CoreMTFShortEvent(
        decision_time=datetime(2026, 9, 2, 15, tzinfo=UTC),
        symbol="TEST",
        logged_entry_price=101,
        stop=106,
        target=96,
        source_file="events.jsonl",
        source_line=2,
        score=10,
        mri_level="NORMAL",
    )

    links = reconcile_observed_exits(ledger, [first, second])

    assert links[0].status == "unmatched"
    assert links[1].status == "unmatched"
