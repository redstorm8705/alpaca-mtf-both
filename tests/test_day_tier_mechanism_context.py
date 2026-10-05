"""Regression tests for ChatGPT/Codex day-tier mechanism evidence (2026-10-05)."""

import json
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from strategy import day_tier_logger
from strategy.day_tier_mechanism_context import (
    MECHANISMS,
    build_decision_context,
)

NOW = datetime(2026, 10, 5, 10, 15, tzinfo=ZoneInfo("America/Los_Angeles"))


def test_snapshot_classifies_existing_family_without_fabricating_missing_data():
    context = build_decision_context(
        "nvda",
        {"track": "A"},
        {
            "mode": "FADE",
            "vwap": 190.25,
            "gex_sign": "POSITIVE",
            "gex_fresh": True,
            "sign_reliable": True,
            "act_ok": True,
            "wall_price": 191.0,
            "feature_asof": "2026-10-05T10:10:00-07:00",
        },
        {"budget": 400.0},
        NOW,
    )
    assert context["family_id"] == "gex_wall_fade_v1"
    assert context["risk_delta"] == {
        "size": 0,
        "frequency": 0,
        "concurrency": 0,
        "effect": "logging_only",
    }
    assert context["mechanisms"]["vwap_state"]["status"] == "OBSERVED"
    assert context["mechanisms"]["dealer_hedging_pressure"]["status"] == "OBSERVED"
    assert context["mechanisms"]["order_flow_imbalance"] == {
        "status": "UNKNOWN",
        "observations": {},
    }
    assert set(context["mechanisms"]) == set(MECHANISMS)


def test_track_b_and_ride_are_independent_families():
    orb = build_decision_context("AAPL", {"track": "B"}, {"mode": "ORB"}, {}, NOW)
    ride = build_decision_context("AAPL", {"track": "A"}, {"mode": "RIDE"}, {}, NOW)
    assert orb["family_id"] == "orb_break_hold_v1"
    assert ride["family_id"] == "gex_wall_ride_v1"


def test_family_classifier_does_not_promote_name_substrings_to_track_b():
    context = build_decision_context(
        "AAPL", {"track": "A"}, {"mode": "FADE_BREAK_FAILURE"}, {}, NOW
    )
    assert context["family_id"] == "unclassified_v1"


def test_feature_without_source_asof_is_partial_not_observed():
    context = build_decision_context(
        "AAPL", {"track": "A"}, {"mode": "FADE", "vwap": 100}, {}, NOW
    )
    assert context["mechanisms"]["vwap_state"]["status"] == "PARTIAL"
    assert context["mechanisms"]["time_of_day_auction_flow"]["status"] == "UNKNOWN"
    assert (
        context["capture_clock_note"] == "logger wall clock; not evidence source time"
    )


def test_invalid_or_future_asof_never_promotes_observation():
    invalid = (True, "", "not-a-time", "2026-10-05T10:10:00", "2099-01-01T00:00:00Z")
    for asof in invalid:
        context = build_decision_context(
            "AAPL",
            {"track": "A"},
            {"mode": "FADE", "vwap": 100, "feature_asof": asof},
            {},
            NOW,
        )
        assert context["mechanisms"]["vwap_state"]["status"] == "PARTIAL", asof
        assert "evidence_asof" not in context["mechanisms"]["vwap_state"], asof


def test_valid_asof_is_normalized_to_utc_and_bounded_by_capture_time():
    context = build_decision_context(
        "AAPL",
        {"track": "A"},
        {
            "mode": "FADE",
            "vwap": 100,
            "feature_asof": "2026-10-05T10:10:00-07:00",
        },
        {},
        NOW,
    )
    evidence = context["mechanisms"]["vwap_state"]
    assert evidence["status"] == "OBSERVED"
    assert evidence["evidence_asof"] == "2026-10-05T17:10:00Z"


def test_digest_is_stable_across_input_key_order():
    one = build_decision_context(
        "MSFT", {"track": "A", "x": 1}, {"mode": "FADE", "vwap": 10}, {}, NOW
    )
    two = build_decision_context(
        "MSFT", {"x": 1, "track": "A"}, {"vwap": 10, "mode": "FADE"}, {}, NOW
    )
    assert one["context_digest"] == two["context_digest"]


def test_malformed_inputs_fail_safe_and_never_claim_observation():
    context = build_decision_context("META", object(), object(), object(), NOW)
    assert context["build_status"] == "OK"
    assert all(
        value["status"] == "UNKNOWN"
        for key, value in context["mechanisms"].items()
        if key != "time_of_day_auction_flow"
    )


def test_logger_propagates_tags_to_entry_and_exit_after_cache_loss(tmp_path):
    path = tmp_path / "events.jsonl"
    entry = {
        "order_id": "entry-1",
        "decision_id": "d-1",
        "side": "buy",
        "requested_limit": 100,
        "fill_price": 99.9,
        "fill_qty": 1,
        "market_price_at_fill": 100,
        "equity_at_entry": 2500,
        "budget": 200,
        "notional": 99.9,
        "track": "B",
    }
    exit_ = {
        "order_id": "exit-1",
        "exit_reason": "target",
        "fill_price": 102,
        "fill_qty": 1,
        "market_price_at_exit": 102,
        "realized_pnl": 2.1,
    }

    with mock.patch.object(day_tier_logger, "_JSONL", path):
        day_tier_logger._mechanism_tags_by_trade.clear()
        assert day_tier_logger.log_decision(
            "d-1", "NVDA", {"track": "B"}, {"mode": "ORB", "vwap": 99}, {}, "DT-1"
        )
        day_tier_logger._mechanism_tags_by_trade.clear()  # simulate a restart
        assert day_tier_logger.log_entry_fill("DT-1", "NVDA", **entry)
        day_tier_logger._mechanism_tags_by_trade.clear()
        assert day_tier_logger.log_exit_fill("DT-1", "NVDA", **exit_)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    digest = rows[0]["mechanism_context"]["context_digest"]
    assert rows[1]["mechanism_tags"]["family_id"] == "orb_break_hold_v1"
    assert rows[2]["mechanism_tags"]["context_digest"] == digest


def test_wait_decision_has_context_but_no_trade_cache(tmp_path):
    path = tmp_path / "events.jsonl"
    with mock.patch.object(day_tier_logger, "_JSONL", path):
        day_tier_logger._mechanism_tags_by_trade.clear()
        assert day_tier_logger.log_decision("d-wait", "AAPL", {}, {}, {}, "")
    row = json.loads(path.read_text())
    assert row["trade_id"] == ""
    assert row["mechanism_context"]["family_id"] == "unclassified_v1"
    assert "" not in day_tier_logger._mechanism_tags_by_trade


def test_failed_decision_write_does_not_seed_non_durable_cache():
    day_tier_logger._mechanism_tags_by_trade.clear()
    with mock.patch.object(day_tier_logger, "_durable_append", return_value=False):
        assert not day_tier_logger.log_decision(
            "d-fail", "AAPL", {"track": "B"}, {"mode": "DRIVE"}, {}, "DT-FAIL"
        )
    assert "DT-FAIL" not in day_tier_logger._mechanism_tags_by_trade


def test_malformed_durable_tags_fall_back_without_dropping_exit(tmp_path):
    path = tmp_path / "events.jsonl"
    bad = {
        "event": "decision",
        "trade_id": "DT-BAD",
        "mechanism_context": {"mechanism_schema_v": "bad"},
    }
    path.write_text(json.dumps(bad) + "\n")
    with mock.patch.object(day_tier_logger, "_JSONL", path):
        day_tier_logger._mechanism_tags_by_trade.clear()
        assert day_tier_logger.log_exit_fill(
            "DT-BAD",
            "AAPL",
            order_id="exit-bad",
            exit_reason="stop",
            fill_price=99,
            fill_qty=1,
            market_price_at_exit=99,
            realized_pnl=-1,
        )
    row = json.loads(path.read_text().splitlines()[-1])
    assert row["event"] == "exit_fill"
    assert row["mechanism_tags"]["family_id"] == "unclassified_v1"
