import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from execution.fill_notifications import (
    _message,
    _parse_broker_timestamp,
    _tier_for_fill,
    poll_fill_notifications,
)


ET = ZoneInfo("America/New_York")


def _fill(fid="f1", *, side="sell", qty="1", price="164.28", order_id="order-12345678"):
    return {
        "id": fid, "order_id": order_id,
        "transaction_time": "2026-10-13T13:03:20Z", "symbol": "EWY",
        "side": side, "qty": qty, "price": price,
    }


def test_python310_accepts_alpaca_variable_fraction_timestamps():
    four = _parse_broker_timestamp("2026-10-09T14:40:15.6787Z")
    five = _parse_broker_timestamp("2026-10-12T13:59:01.03589Z")
    assert four.microsecond == 678700
    assert five.microsecond == 35890
    assert _message(
        {**_fill(), "transaction_time": "2026-10-09T14:40:15.6787Z"},
        "unavailable",
    ).startswith(":red_circle: *BROKER SELL FILLED")


def test_variable_fraction_timestamp_is_canonical_in_state(tmp_path: Path):
    state = tmp_path / "fills.json"
    fill = {**_fill(), "transaction_time": "2026-10-09T14:40:15.6787Z"}
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [fill] if day == "2026-10-09" else [],
        deliver=lambda _msg: True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    saved = json.loads(state.read_text())
    assert saved["seen"]["f1"]["ts"] == "2026-10-09T14:40:15.678700+00:00"


def test_existing_variable_fraction_state_is_self_healed(tmp_path: Path):
    state = tmp_path / "fills.json"
    state.write_text(json.dumps({
        "version": 1,
        "seen": {
            "f1": {
                "ts": "2026-10-09T14:40:15.6787Z",
                "hash": "a" * 64,
            }
        },
        "malformed_seen": {},
        "last_poll_at": "2026-10-13T09:00:00-04:00",
    }))
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda _day: [], deliver=lambda _msg: True,
    ) == 0
    saved = json.loads(state.read_text())
    assert saved["seen"]["f1"]["ts"] == "2026-10-09T14:40:15.678700+00:00"


def test_first_run_backfills_instead_of_silently_baselining(tmp_path: Path):
    sent, state = [], tmp_path / "fills.json"
    count = poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [_fill()] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    )
    assert count == 1
    assert "BROKER SELL FILLED — EWY" in sent[0]
    assert "f1" in state.read_text()


def test_monday_initialization_fetches_friday(tmp_path: Path):
    requested, sent = [], []

    def fetch(day):
        requested.append(day)
        return [_fill()] if day == "2026-10-09" else []

    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET),
        state_path=tmp_path / "fills.json", fetch_fills=fetch,
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "2026-10-09" in requested
    assert len(sent) == 1


def test_new_fill_is_delivered_once_with_canonical_tier(tmp_path: Path):
    sent, current = [], []
    state = tmp_path / "fills.json"
    now = datetime(2026, 10, 13, 10, tzinfo=ET)
    def fetch(day):
        return list(current) if day == "2026-10-13" else []
    poll_fill_notifications(now_et=now, state_path=state, fetch_fills=fetch,
                            deliver=lambda msg: sent.append(msg) or True)
    current.append(_fill())
    assert poll_fill_notifications(
        now_et=now, state_path=state, fetch_fills=fetch,
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "1 share @ $164.28" in sent[0] and "Tier: *Swing*" in sent[0]
    assert poll_fill_notifications(
        now_et=now, state_path=state, fetch_fills=fetch,
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 0
    assert len(sent) == 1


def test_failed_delivery_is_not_acknowledged_and_retries(tmp_path: Path):
    state = tmp_path / "fills.json"
    now = datetime(2026, 10, 13, 10, tzinfo=ET)
    current = []
    def fetch(day):
        return list(current) if day == "2026-10-13" else []
    poll_fill_notifications(now_et=now, state_path=state, fetch_fills=fetch,
                            deliver=lambda _msg: True)
    current.append(_fill())
    assert poll_fill_notifications(
        now_et=now, state_path=state, fetch_fills=fetch,
        deliver=lambda _msg: False, tier_lookup=lambda _fill: "daytrade",
    ) == 0
    assert "f1" not in state.read_text()
    delivered = []
    assert poll_fill_notifications(
        now_et=now, state_path=state, fetch_fills=fetch,
        deliver=lambda msg: delivered.append(msg) or True,
        tier_lookup=lambda _fill: "daytrade",
    ) == 1
    assert "Tier: *Day*" in delivered[0]


def test_failed_friday_backfill_retries_on_monday(tmp_path: Path):
    state = tmp_path / "fills.json"
    now = datetime(2026, 10, 13, 10, tzinfo=ET)
    friday = _fill()

    def fetch(day):
        return [friday] if day == "2026-10-09" else []

    assert poll_fill_notifications(
        now_et=now, state_path=state, fetch_fills=fetch,
        deliver=lambda _msg: False, tier_lookup=lambda _fill: "intraday",
    ) == 0
    assert not state.exists() or "last_poll_at" not in state.read_text()
    delivered = []
    assert poll_fill_notifications(
        now_et=now, state_path=state, fetch_fills=fetch,
        deliver=lambda msg: delivered.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "BROKER SELL FILLED" in delivered[0]


def test_unknown_side_pages_data_error_and_never_claims_sell(tmp_path: Path):
    sent = []
    count = poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET),
        state_path=tmp_path / "fills.json",
        fetch_fills=lambda day: (
            [_fill(side="malformed")] if day == "2026-10-13" else []
        ),
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    )
    assert count == 0 and len(sent) == 1
    assert "MALFORMED BROKER FILL ACTIVITY" in sent[0]
    assert "BROKER SELL" not in sent[0]


def test_missing_activity_id_pages_without_normal_ack(tmp_path: Path):
    sent, state = [], tmp_path / "fills.json"
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [_fill(fid="")] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 0
    assert "MALFORMED BROKER FILL ACTIVITY" in sent[0]
    assert json.loads(state.read_text())["seen"] == {}


def test_non_string_activity_id_is_never_normalized_into_an_ack(tmp_path: Path):
    for index, invalid_id in enumerate((True, 1)):
        sent, state = [], tmp_path / f"fills-{index}.json"
        assert poll_fill_notifications(
            now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
            fetch_fills=lambda day, value=invalid_id: (
                [_fill(fid=value)] if day == "2026-10-13" else []
            ),
            deliver=lambda msg, output=sent: output.append(msg) or True,
            tier_lookup=lambda _fill: "intraday",
        ) == 0
        assert "MALFORMED BROKER FILL ACTIVITY" in sent[0]
        assert "BROKER SELL FILLED" not in sent[0]
        assert json.loads(state.read_text())["seen"] == {}


def test_corrupt_state_pages_then_replays_backfill(tmp_path: Path):
    state, sent = tmp_path / "fills.json", []
    state.write_text("not json")
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [_fill()] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "qhm",
    ) == 1
    assert "STATE CORRUPT" in sent[0] and "Tier: *QHM*" in sent[1]
    assert list(tmp_path.glob("fills.json.corrupt.*"))


def test_semantically_corrupt_seen_entry_cannot_mask_fill(tmp_path: Path):
    state, sent = tmp_path / "fills.json", []
    state.write_text(json.dumps({
        "version": 1, "seen": {"f1": None}, "malformed_seen": {},
        "last_poll_at": "2026-10-13T09:00:00-04:00",
    }))
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [_fill()] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "STATE CORRUPT" in sent[0]
    assert "BROKER SELL FILLED" in sent[1]


def test_boolean_state_version_cannot_mask_fill(tmp_path: Path):
    state, sent = tmp_path / "fills.json", []
    state.write_text(json.dumps({
        "version": True,
        "seen": {"f1": {"ts": "2026-10-13T09:00:00-04:00", "hash": "a" * 64}},
        "malformed_seen": {},
        "last_poll_at": "2026-10-13T09:00:00-04:00",
    }))
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [_fill()] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "STATE CORRUPT" in sent[0]
    assert "BROKER SELL FILLED" in sent[1]


def test_future_seen_timestamp_cannot_mask_fill(tmp_path: Path):
    state, sent = tmp_path / "fills.json", []
    state.write_text(json.dumps({
        "version": 1,
        "seen": {"f1": {"ts": "2099-01-01T00:00:00+00:00", "hash": "a" * 64}},
        "malformed_seen": {},
        "last_poll_at": "2026-10-13T09:00:00-04:00",
    }))
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [_fill()] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "STATE CORRUPT" in sent[0]
    assert "BROKER SELL FILLED" in sent[1]


def test_future_malformed_or_poll_timestamp_quarantines_state(tmp_path: Path):
    for field in ("malformed_seen", "last_poll_at"):
        state, sent = tmp_path / f"{field}.json", []
        payload = {
            "version": 1,
            "seen": {},
            "malformed_seen": {},
            "last_poll_at": "2026-10-13T09:00:00-04:00",
        }
        if field == "malformed_seen":
            payload[field] = {"fingerprint": "2099-01-01T00:00:00+00:00"}
        else:
            payload[field] = "2099-01-01T00:00:00+00:00"
        state.write_text(json.dumps(payload))
        assert poll_fill_notifications(
            now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
            fetch_fills=lambda day: [_fill()] if day == "2026-10-13" else [],
            deliver=lambda msg, output=sent: output.append(msg) or True,
            tier_lookup=lambda _fill: "intraday",
        ) == 1
        assert "STATE CORRUPT" in sent[0]
        assert "BROKER SELL FILLED" in sent[1]


def test_conflicting_duplicate_id_pages_instead_of_second_trade(tmp_path: Path):
    state = tmp_path / "fills.json"
    now = datetime(2026, 10, 13, 10, tzinfo=ET)
    sent, fills = [], [_fill(price="100")]
    def fetch(day):
        return list(fills) if day == "2026-10-13" else []
    poll_fill_notifications(now_et=now, state_path=state, fetch_fills=fetch,
                            deliver=lambda msg: sent.append(msg) or True,
                            tier_lookup=lambda _fill: "forever6")
    fills[:] = [_fill(price="101")]
    assert poll_fill_notifications(
        now_et=now, state_path=state, fetch_fills=fetch,
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "forever6",
    ) == 0
    assert len([msg for msg in sent if "BROKER SELL FILLED" in msg]) == 1
    assert "CONFLICTING BROKER FILL ID" in sent[-1]


def test_untagged_buy_is_notified_without_inventing_a_tier():
    text = _message(_fill(side="buy", qty="2", price="100", order_id=""), "untagged")
    assert "BROKER BUY FILLED" in text and "2 shares @ $100.00" in text
    assert "Tier: *Not tagged*" in text


def test_lookup_failure_is_distinct_from_untagged():
    assert "Tier: *Attribution unavailable*" in _message(_fill(), "unavailable")


def test_blank_symbol_timestamp_and_boolean_qty_are_malformed(tmp_path: Path):
    cases = [
        {**_fill(), "symbol": ""},
        {**_fill(), "transaction_time": ""},
        {**_fill(), "qty": True},
    ]
    for index, fill in enumerate(cases):
        sent = []
        fill["id"] = f"bad-{index}"

        def deliver(message, output=sent):
            output.append(message)
            return True

        assert poll_fill_notifications(
            now_et=datetime(2026, 10, 13, 10, tzinfo=ET),
            state_path=tmp_path / f"fills-{index}.json",
            fetch_fills=lambda day, item=fill: [item] if day == "2026-10-13" else [],
            deliver=deliver,
            tier_lookup=lambda _fill: "intraday",
        ) == 0
        assert "MALFORMED BROKER FILL ACTIVITY" in sent[0]
        assert "BROKER BUY FILLED" not in sent[0]
        assert "BROKER SELL FILLED" not in sent[0]


def test_non_object_activity_pages_and_valid_fill_still_delivers(tmp_path: Path):
    sent = []
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET),
        state_path=tmp_path / "fills.json",
        fetch_fills=lambda day: [None, _fill()] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "MALFORMED BROKER FILL ACTIVITY" in sent[0]
    assert "BROKER SELL FILLED" in sent[1]


def test_untagged_oco_child_inherits_parent_tier():
    child = SimpleNamespace(client_order_id="", parent_order_id="parent-1")
    parent = SimpleNamespace(client_order_id="DT-EWY-s-1-x")
    with mock.patch("execution.broker.get_order", side_effect=[child, parent]):
        assert _tier_for_fill(_fill()) == "daytrade"


def test_unproved_long_lived_child_is_attribution_unavailable():
    child = SimpleNamespace(client_order_id="", parent_order_id="")
    client = SimpleNamespace(get_orders=lambda filter: [])
    with mock.patch("execution.broker.get_order", return_value=child), mock.patch(
        "execution.broker._get_trading_client", return_value=client
    ):
        assert _tier_for_fill(_fill()) == "unavailable"


def test_state_schema_stays_json_object(tmp_path: Path):
    state = tmp_path / "fills.json"
    poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda _day: [], deliver=lambda _msg: True,
    )
    assert isinstance(json.loads(state.read_text())["seen"], dict)
