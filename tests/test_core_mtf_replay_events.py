import json
from datetime import datetime, timezone

from research.core_mtf_replay_events import (
    extract_core_mtf_short_entries,
    iter_core_mtf_short_entries,
)


UTC = timezone.utc
EXTRACTED_AT = datetime(2026, 9, 2, tzinfo=UTC)


def _valid(**overrides):
    row = {
        "ts": "2026-09-01T15:00:00+00:00",
        "event": "entry",
        "direction": "short",
        "trade_mode": "intraday",
        "symbol": "NVDA",
        "price": 100,
        "stop": 105,
        "target": 95,
        "score": 10,
        "mri_level": "NORMAL",
    }
    return {**row, **overrides}


def test_extracts_only_structurally_valid_core_mtf_shorts(tmp_path):
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        "not-json\n"
        + json.dumps(_valid(symbol=" nvda "))
        + "\n"
        + json.dumps(_valid(symbol="BAD", target=101))
        + "\n"
        + json.dumps(_valid(symbol="DAY", trade_mode="day_trade"))
        + "\n"
    )

    events = list(iter_core_mtf_short_entries(ledger))

    assert len(events) == 1
    event = events[0]
    assert event.symbol == "NVDA"
    assert event.source_line == 2
    assert event.stop == 105
    assert event.target == 95


def test_manifest_accounts_for_every_discard_class_and_candidate_line(tmp_path):
    rows = [
        "malformed",
        json.dumps(["not", "an", "object"]),
        json.dumps(_valid(event="exit")),
        json.dumps(_valid(trade_mode="day_trade")),
        json.dumps(_valid(ts="not-a-time")),
        json.dumps(_valid(symbol="BAD", target=101)),
        json.dumps(_valid()),
    ]
    ledger = tmp_path / "events.jsonl"
    ledger.write_text("\n".join(rows) + "\n")

    result = extract_core_mtf_short_entries(ledger, extracted_at=EXTRACTED_AT)

    assert result.manifest.total_source_lines == 7
    assert result.manifest.accepted_count == 1
    assert result.manifest.discarded_counts == {
        "invalid_required_fields": 1,
        "invalid_short_geometry": 1,
        "malformed_json": 1,
        "non_object_row": 1,
        "not_core_mtf_legacy_mode": 1,
        "not_short_entry": 1,
    }
    discarded_candidates = [
        (item.source_line, item.reason)
        for item in result.manifest.discarded_candidates
    ]
    assert discarded_candidates == [
        (5, "invalid_required_fields"),
        (6, "invalid_short_geometry"),
    ]


def test_normalizes_offsets_and_applies_inclusive_utc_window(tmp_path):
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(json.dumps(_valid(ts="2026-09-01T08:00:00-07:00")) + "\n")
    boundary = datetime(2026, 9, 1, 15, tzinfo=UTC)

    result = extract_core_mtf_short_entries(
        ledger,
        start=boundary,
        end=boundary,
        extracted_at=EXTRACTED_AT,
    )

    assert result.events[0].decision_time == boundary
    assert result.manifest.start_utc == "2026-09-01T15:00:00+00:00"
    assert result.manifest.end_utc == "2026-09-01T15:00:00+00:00"


def test_identical_input_has_same_accounting_and_digest(tmp_path):
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(json.dumps(_valid()) + "\n")

    first = extract_core_mtf_short_entries(ledger, extracted_at=EXTRACTED_AT)
    second = extract_core_mtf_short_entries(ledger, extracted_at=EXTRACTED_AT)
    ledger.write_text(json.dumps(_valid(symbol="META")) + "\n")
    changed = extract_core_mtf_short_entries(ledger, extracted_at=EXTRACTED_AT)

    assert first.manifest.as_dict() == second.manifest.as_dict()
    assert first.manifest.source_sha256 != changed.manifest.source_sha256
    assert first.manifest.source_bytes == changed.manifest.source_bytes
