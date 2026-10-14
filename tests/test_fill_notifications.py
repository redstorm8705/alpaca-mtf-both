import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from execution.fill_notifications import (
    _RealizedPnl,
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
    ).startswith(":red_circle: *Sold EWY*")


def test_variable_fraction_timestamp_is_canonical_in_state(tmp_path: Path):
    state = tmp_path / "fills.json"
    fill = {**_fill(), "transaction_time": "2026-10-09T14:40:15.6787Z"}
    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: [fill] if day == "2026-10-09" else [],
        deliver=lambda _msg: True,
        tier_lookup=lambda _fill: "intraday",
    ) == 0  # an earlier trading date is acknowledged without a card
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
    assert "*Sold EWY* · Swing" in sent[0]
    assert "f1" in state.read_text()


def test_monday_initialization_fetches_friday_but_posts_only_today(tmp_path: Path):
    requested, sent = [], []
    friday = {**_fill("fri"), "transaction_time": "2026-10-09T14:40:15Z"}
    state = tmp_path / "fills.json"

    def fetch(day):
        requested.append(day)
        if day == "2026-10-09":
            return [friday]
        return [_fill()] if day == "2026-10-13" else []

    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET),
        state_path=state, fetch_fills=fetch,
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    ) == 1
    assert "2026-10-09" in requested
    assert len(sent) == 2 and "*Sold EWY*" in sent[0]
    assert "1 broker fill(s) from an earlier trading date were not posted" in sent[1]
    assert "fri" in json.loads(state.read_text())["seen"]


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
    assert "1 share @ $164.28 · $164.28" in sent[0]
    assert "*Sold EWY* · Swing" in sent[0]
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
    assert "*Sold EWY* · Day" in delivered[0]


def test_failed_backfill_delivery_retries_next_poll(tmp_path: Path):
    state = tmp_path / "fills.json"
    now = datetime(2026, 10, 13, 10, tzinfo=ET)
    today = _fill()

    def fetch(day):
        return [today] if day == "2026-10-13" else []

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
    assert "*Sold EWY*" in delivered[0]


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
    assert "*Sold" not in sent[0]


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
        assert "*Sold" not in sent[0]
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
    assert "STATE CORRUPT" in sent[0] and "*Sold EWY* · QHM" in sent[1]
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
    assert "*Sold EWY*" in sent[1]


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
    assert "*Sold EWY*" in sent[1]


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
    assert "*Sold EWY*" in sent[1]


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
        assert "*Sold EWY*" in sent[1]


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
    assert len([msg for msg in sent if "*Sold EWY*" in msg]) == 1
    assert "CONFLICTING BROKER FILL ID" in sent[-1]


def test_untagged_buy_is_notified_without_inventing_a_tier():
    text = _message(_fill(side="buy", qty="2", price="100", order_id=""), "untagged")
    assert "*Bought EWY* · Manual" in text
    assert "2 shares @ $100.00 · $200.00" in text


def test_lookup_failure_is_distinct_from_untagged():
    assert "*Sold EWY* · Tier unknown" in _message(_fill(), "unavailable")


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
        assert "*Bought" not in sent[0]
        assert "*Sold" not in sent[0]


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
    assert "*Sold EWY*" in sent[1]


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


def test_card_time_is_12_hour_pt_without_iso_or_send_time():
    text = _message(_fill(), "daytrade", {"status": "opening"})
    assert text.splitlines()[-1] == "6:03 AM PT · order 12345678"
    assert "2026-10-13T" not in text and "Realized" not in text


def _ctx(history, positions, tiers):
    ctx = _RealizedPnl(datetime(2026, 10, 13, 12, tzinfo=ET))
    ctx._history, ctx._positions = history, positions
    ctx._tiers = {"NVDA": tiers}
    ctx._tiers_complete = {"NVDA": True}
    return ctx


def _row(fid, side, qty, price, order, ts):
    return {"id": fid, "order_id": order, "symbol": "NVDA", "side": side,
            "qty": str(qty), "price": str(price), "transaction_time": ts}


def _nvda(fill):
    return {**fill, "symbol": "NVDA"}


def test_day_close_never_matches_another_tiers_cheaper_lot():
    swing = _row("s1", "buy", 10, 100, "o-sw", "2026-10-01T14:00:00Z")
    dbuy = _row("d1", "buy", 5, 110, "o-db", "2026-10-13T14:00:00Z")
    dsell = _row("d2", "sell", 5, 105, "o-ds", "2026-10-13T15:00:00Z")
    ctx = _ctx([swing, dbuy, dsell], {"NVDA": 10.0},
               {"o-sw": "intraday", "o-db": "daytrade", "o-ds": "daytrade"})
    out = ctx.lookup(dsell, "daytrade")
    assert out["status"] == "ok" and abs(out["pnl"] + 25.0) < 1e-9
    assert not out["mixed"]
    text = _message(_nvda(dsell), "daytrade", out)
    assert "Realized P/L: *-$25.00* (-4.5%)" in text


def test_day_close_ignores_an_old_lot_outside_the_window():
    # A buy-and-hold lot older than the lookback makes the account net disagree
    # with the broker; the Day tier's own same-session ledger is still complete.
    dbuy = _row("d1", "buy", 2, 50, "o-db", "2026-10-13T14:00:00Z")
    dsell = _row("d2", "sell", 2, 51, "o-ds", "2026-10-13T15:00:00Z")
    ctx = _ctx([dbuy, dsell], {"NVDA": 7.0},
               {"o-db": "daytrade", "o-ds": "daytrade"})
    out = ctx.lookup(dsell, "daytrade")
    assert out["status"] == "ok" and abs(out["pnl"] - 2.0) < 1e-9


def test_swing_close_with_entry_before_window_is_unavailable_not_estimated():
    sell = _row("s2", "sell", 1, 120, "o-ss", "2026-10-13T15:00:00Z")
    ctx = _ctx([sell], {"NVDA": 4.0}, {"o-ss": "intraday"})
    out = ctx.lookup(sell, "intraday")
    assert out == {"status": "unavailable", "reason": "entry older than 90 days",
                   "owner": "intraday"}
    text = _message(_nvda(sell), "intraday", out)
    assert "Realized P/L: unavailable (entry older than 90 days)" in text


def test_manual_close_uses_account_fifo_and_says_so():
    buy = _row("b1", "buy", 72, 70.14, "o-dt", "2026-10-12T13:59:00Z")
    sell = _row("m1", "sell", 65, 71.30, "o-man", "2026-10-13T14:40:00Z")
    ctx = _ctx([buy, sell], {"NVDA": 7.0},
               {"o-dt": "daytrade", "o-man": "untagged"})
    out = ctx.lookup(sell, "untagged")
    assert out["status"] == "ok" and out["mixed"]
    assert abs(out["pnl"] - 65 * 1.16) < 1e-6
    assert "· account FIFO" in _message(_nvda(sell), "untagged", out)


def test_tagged_sell_beyond_own_lots_is_unavailable():
    buy = _row("b1", "buy", 3, 100, "o-sw", "2026-10-10T14:00:00Z")
    sell = _row("d2", "sell", 2, 105, "o-ds", "2026-10-13T15:00:00Z")
    ctx = _ctx([buy, sell], {"NVDA": 1.0},
               {"o-sw": "intraday", "o-ds": "daytrade"})
    expected = {"status": "unavailable", "reason": "tier lots incomplete"}
    assert ctx.lookup(sell, "daytrade") == expected


def test_short_open_has_no_pnl_and_cover_reports_it():
    short = _row("x1", "sell_short", 4, 50, "o-a", "2026-10-13T14:00:00Z")
    cover = _row("x2", "buy", 4, 48, "o-b", "2026-10-13T15:00:00Z")
    ctx = _ctx([short, cover], {"NVDA": 0.0},
               {"o-a": "daytrade", "o-b": "daytrade"})
    assert ctx.lookup(short, "daytrade") == {"status": "opening", "owner": "daytrade"}
    out = ctx.lookup(cover, "daytrade")
    assert out["status"] == "ok" and abs(out["pnl"] - 8.0) < 1e-9
    text = _message(_nvda(cover), "daytrade", out)
    assert "*Covered NVDA* · Day" in text
    assert "Realized P/L: *+$8.00* (+4.0%)" in text


def test_partial_close_labels_the_closed_quantity():
    buy = _row("b1", "buy", 4, 10, "o-a", "2026-10-13T14:00:00Z")
    sell = _row("m1", "sell", 10, 11, "o-man", "2026-10-13T15:00:00Z")
    ctx = _ctx([buy, sell], {"NVDA": -6.0},
               {"o-a": "intraday", "o-man": "untagged"})
    out = ctx.lookup(sell, "untagged")
    assert out["status"] == "ok" and abs(out["pnl"] - 4.0) < 1e-9
    assert "Realized P/L on 4 sh: *+$4.00*" in _message(_nvda(sell), "untagged", out)


def test_broker_read_failure_is_unavailable():
    # The test environment blanks the Alpaca keys, so the history read raises.
    ctx = _RealizedPnl(datetime(2026, 10, 13, 12, tzinfo=ET))
    expected = {"status": "unavailable", "reason": "broker read failed"}
    assert ctx.lookup(_fill(), "daytrade") == expected


def test_truncated_order_history_is_unavailable_for_non_day_tiers():
    buy = _row("b1", "buy", 1, 100, "o-sw", "2026-10-10T14:00:00Z")
    sell = _row("s1", "sell", 1, 110, "o-ss", "2026-10-13T15:00:00Z")
    ctx = _ctx([buy, sell], {"NVDA": 0.0}, {"o-sw": "intraday", "o-ss": "intraday"})
    ctx._tiers_complete = {"NVDA": False}
    expected = {"status": "unavailable", "reason": "order history too long"}
    assert ctx.lookup(sell, "intraday") == expected
    ctx._tiers_complete = {"NVDA": True}
    assert ctx.lookup(sell, "intraday")["pnl"] == 10.0


def test_partial_fills_of_one_order_post_one_card(tmp_path: Path):
    sent = []
    pieces = [
        {**_fill(f"p{i}", qty=q, price="259.68", order_id="ord-amzn"),
         "transaction_time": f"2026-10-13T17:41:0{i}Z"}
        for i, q in enumerate(("3", "2", "1"))
    ]
    realized = {"status": "ok", "pnl": -2.19, "pct": -0.8, "cost": 261.87,
                "closed_qty": 1.0, "mixed": False}

    def lookup(fill, _tier):
        n = float(fill["qty"])
        return {**realized, "pnl": -2.19 * n, "cost": 261.87 * n, "closed_qty": n}

    assert poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 14, tzinfo=ET),
        state_path=tmp_path / "fills.json",
        fetch_fills=lambda day: list(pieces) if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "daytrade",
        realized_lookup=lookup,
    ) == 3
    assert len(sent) == 1
    assert "6 shares @ $259.68 · $1,558.08" in sent[0]
    assert "Realized P/L: *-$13.14* (-0.8%)" in sent[0]


def test_old_fill_ack_survives_a_poll_with_earlier_delivery(tmp_path: Path):
    state = tmp_path / "fills.json"
    today = _fill("t1")
    old = {**_fill("o1"), "transaction_time": "2026-10-12T15:00:00Z"}
    late = {**_fill("o2"), "transaction_time": "2026-10-12T16:00:00Z"}
    batches = {"2026-10-12": [old, late], "2026-10-13": [today]}
    poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET), state_path=state,
        fetch_fills=lambda day: list(batches.get(day, [])),
        deliver=lambda _msg: True, tier_lookup=lambda _fill: "intraday",
        realized_lookup=lambda _f, _t: {"status": "opening"},
    )
    seen = json.loads(state.read_text())["seen"]
    assert {"t1", "o1", "o2"} <= set(seen)


def test_manual_sale_inside_a_day_session_blocks_double_counting():
    # Cold-2nd F1: the manual sale consumed the $100 Day lot; the Day-only replay
    # must not match it again (that showed a $25 loss as a $25 gain).
    rows = [
        _row("d1", "buy", 5, 100, "o-b1", "2026-10-13T13:35:00Z"),
        _row("d2", "buy", 5, 110, "o-b2", "2026-10-13T14:00:00Z"),
        _row("m1", "sell", 5, 105, "o-man", "2026-10-13T14:30:00Z"),
        _row("d3", "sell", 5, 105, "o-s", "2026-10-13T15:00:00Z"),
    ]
    tiers = {"o-b1": "daytrade", "o-b2": "daytrade", "o-man": "untagged",
             "o-s": "daytrade"}
    ctx = _ctx(rows, {"NVDA": 0.0}, tiers)
    expected = {"status": "unavailable", "reason": "manual trade in session"}
    assert ctx.lookup(rows[3], "daytrade") == expected


def test_day_lots_carried_overnight_make_day_pnl_unavailable():
    rows = [
        _row("d0", "buy", 10, 100, "o-old", "2026-10-12T14:00:00Z"),
        _row("d1", "buy", 5, 120, "o-b", "2026-10-13T14:00:00Z"),
        _row("d2", "sell", 5, 110, "o-s", "2026-10-13T15:00:00Z"),
    ]
    tiers = {"o-old": "daytrade", "o-b": "daytrade", "o-s": "daytrade"}
    ctx = _ctx(rows, {"NVDA": 10.0}, tiers)
    expected = {"status": "unavailable", "reason": "Day lots carried overnight"}
    assert ctx.lookup(rows[2], "daytrade") == expected


def test_fill_from_an_unmapped_order_is_unavailable():
    rows = [
        _row("b1", "buy", 2, 100, "o-gtc", "2026-10-01T14:00:00Z"),
        _row("s1", "sell", 2, 110, "o-s", "2026-10-13T15:00:00Z"),
    ]
    ctx = _ctx(rows, {"NVDA": 0.0}, {"o-s": "intraday"})
    expected = {"status": "unavailable", "reason": "order history incomplete"}
    assert ctx.lookup(rows[1], "intraday") == expected


def test_history_pages_by_page_token_and_dedupes_boundary_rows(monkeypatch):
    import io
    from urllib.parse import parse_qs, urlparse

    import execution.fill_notifications as fn

    page1 = [{"id": f"a{i:03d}"} for i in range(100)]
    page2 = [{"id": "a099"}, {"id": "b000"}]
    tokens = []

    def fake_urlopen(req, timeout, context):
        query = parse_qs(urlparse(req.full_url).query)
        assert "after_id" not in query
        token = query.get("page_token", [None])[0]
        tokens.append(token)
        body = page1 if token is None else page2
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(fn.urllib.request, "urlopen", fake_urlopen)
    start = datetime(2026, 7, 1, tzinfo=ET)
    rows = fn._fetch_fills_range(start, datetime(2026, 10, 13, tzinfo=ET))
    assert tokens == [None, "a099"]
    assert [r["id"] for r in rows].count("a099") == 1 and len(rows) == 101


def test_incident_pages_keep_their_time(tmp_path: Path):
    sent = []
    poll_fill_notifications(
        now_et=datetime(2026, 10, 13, 10, tzinfo=ET),
        state_path=tmp_path / "fills.json",
        fetch_fills=lambda day: [_fill(fid="")] if day == "2026-10-13" else [],
        deliver=lambda msg: sent.append(msg) or True,
        tier_lookup=lambda _fill: "intraday",
    )
    assert sent[0].splitlines()[-1] == "7:00 AM PT"


def test_add_on_buy_to_an_open_position_shows_no_pnl_line():
    add = _row("f1", "buy", 1, 226.36, "o-f6", "2026-10-13T14:11:00Z")
    ctx = _ctx([add], {"NVDA": 3.0}, {"o-f6": "forever6"})
    out = ctx.lookup(add, "forever6")
    assert out["status"] == "opening"
    assert "Realized" not in _message(_nvda(add), "forever6", out)


def test_qhm_buy_under_mismatch_is_unavailable_because_qhm_can_short():
    add = _row("q1", "buy", 1, 226.36, "o-qhm", "2026-10-13T14:11:00Z")
    ctx = _ctx([add], {"NVDA": 3.0}, {"o-qhm": "qhm"})
    assert ctx.lookup(add, "qhm")["status"] == "unavailable"


def test_proved_untagged_order_is_labeled_manual():
    buy = _row("b1", "buy", 2, 100, "o-dt", "2026-10-13T14:00:00Z")
    sell = _row("m1", "sell", 2, 101, "o-man", "2026-10-13T15:00:00Z")
    ctx = _ctx([buy, sell], {"NVDA": 0.0},
               {"o-dt": "daytrade", "o-man": "untagged"})
    out = ctx.lookup(sell, "untagged")
    assert "*Sold NVDA* · Manual" in _message(_nvda(sell), "unavailable", out)


def test_manual_buy_under_mismatch_is_unavailable_not_hidden():
    # Cold-2nd round 2: a manual buy may be a cover; it must not be shown as an add.
    old_sell = _row("q0", "sell", 5, 90, "o-qs", "2026-07-20T14:00:00Z")
    short = _row("x1", "sell_short", 5, 100, "o-ds", "2026-10-13T14:00:00Z")
    cover = _row("m1", "buy", 5, 110, "o-man", "2026-10-13T15:00:00Z")
    add = _row("q1", "buy", 3, 112, "o-qa", "2026-10-13T15:00:30Z")
    tiers = {"o-qs": "qhm", "o-ds": "daytrade", "o-man": "untagged", "o-qa": "qhm"}
    ctx = _ctx([old_sell, short, cover, add], {"NVDA": 3.0}, tiers)
    out = ctx.lookup(cover, "untagged")
    assert out["status"] == "unavailable"
    assert "Realized P/L: unavailable" in _message(_nvda(cover), "untagged", out)


def test_tagged_close_after_a_manual_trade_in_window_is_unavailable():
    # Adversarial review: which tier's lots a manual sale drew from is unknowable.
    rows = [
        _row("q1", "buy", 10, 100, "o-q", "2026-09-01T14:00:00Z"),
        _row("s1", "buy", 5, 110, "o-s", "2026-09-02T14:00:00Z"),
        _row("m1", "sell", 5, 120, "o-m", "2026-09-03T14:00:00Z"),
        _row("s2", "sell", 5, 115, "o-x", "2026-10-13T14:00:00Z"),
    ]
    tiers = {"o-q": "qhm", "o-s": "intraday", "o-m": "untagged", "o-x": "intraday"}
    ctx = _ctx(rows, {"NVDA": 5.0}, tiers)
    out = ctx.lookup(rows[3], "intraday")
    assert out["status"] == "unavailable" and out["reason"] == "manual trade in window"


def test_card_fetch_pages_by_token_and_keeps_only_that_et_day(monkeypatch):
    import execution.fill_notifications as fn

    calls = []

    def fake_range(start, end):
        calls.append((start, end))
        return [
            {"id": "a", "transaction_time": "2026-10-13T13:30:00Z"},
            {"id": "b", "transaction_time": "2026-10-14T03:59:00Z"},
            {"id": "c", "transaction_time": "2026-10-14T04:01:00Z"},
        ]

    monkeypatch.setattr(fn, "_fetch_fills_range", fake_range)
    rows = fn._fetch_fills_for_day("2026-10-13")
    assert [r["id"] for r in rows] == ["a", "b"]
    assert calls[0][0] == datetime(2026, 10, 13, tzinfo=ET)


def test_swing_cover_under_mismatch_is_unavailable_not_an_add():
    # Final review: Swing (intraday tag) can short; its buy may be a cover.
    sell = _row("s1", "sell", 5, 100, "o-s", "2026-10-13T14:00:00Z")
    cover = _row("s2", "buy", 5, 110, "o-c", "2026-10-13T15:00:00Z")
    ctx = _ctx([sell, cover], {"NVDA": 10.0}, {"o-s": "intraday", "o-c": "intraday"})
    out = ctx.lookup(cover, "intraday")
    assert out["status"] == "unavailable"
