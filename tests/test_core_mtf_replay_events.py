import json
from datetime import datetime, timezone

from research.core_mtf_replay_events import iter_core_mtf_short_entries


UTC = timezone.utc


def test_extracts_only_structurally_valid_core_mtf_shorts(tmp_path):
    valid = {
        "ts": "2026-09-01T15:00:00+00:00",
        "event": "entry",
        "direction": "short",
        "trade_mode": "intraday",
        "symbol": " nvda ",
        "price": 100,
        "stop": 105,
        "target": 95,
        "score": 10,
        "mri_level": "NORMAL",
    }
    invalid_target = {**valid, "symbol": "BAD", "target": 101}
    day_tier = {**valid, "symbol": "DAY", "trade_mode": "day_trade"}
    ledger = tmp_path / "events.jsonl"
    ledger.write_text(
        "not-json\n"
        + json.dumps(valid)
        + "\n"
        + json.dumps(invalid_target)
        + "\n"
        + json.dumps(day_tier)
        + "\n"
    )

    events = list(iter_core_mtf_short_entries(ledger))

    assert len(events) == 1
    event = events[0]
    assert event.symbol == "NVDA"
    assert event.source_line == 2
    assert event.stop == 105
    assert event.target == 95


def test_time_window_is_timezone_aware_and_inclusive(tmp_path):
    rows = [
        {
            "ts": "2026-09-01T14:59:00+00:00",
            "event": "entry",
            "direction": "short",
            "trade_mode": "intraday",
            "symbol": "EARLY",
            "price": 100,
            "stop": 105,
            "target": 95,
        },
        {
            "ts": "2026-09-01T15:00:00+00:00",
            "event": "entry",
            "direction": "short",
            "trade_mode": "intraday",
            "symbol": "EDGE",
            "price": 100,
            "stop": 105,
            "target": 95,
        },
    ]
    ledger = tmp_path / "events.jsonl"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    start = datetime(2026, 9, 1, 15, tzinfo=UTC)

    events = list(iter_core_mtf_short_entries(ledger, start=start, end=start))

    assert [event.symbol for event in events] == ["EDGE"]
