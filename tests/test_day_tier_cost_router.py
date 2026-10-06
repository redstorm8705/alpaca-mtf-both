import json
from datetime import datetime, timezone

from strategy.day_tier_execution_costs import attribute_trade_costs
from strategy.day_tier_family_router import route_families

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _candidate(family_id, version, score=1, asof="2026-10-06T13:59:00Z"):
    return {
        "family_id": family_id,
        "hypothesis_version": version,
        "routing_score": score,
        "score_asof": asof,
    }


def test_cost_attribution_uses_directional_adverse_sign_and_zero_commission():
    events = [
        {
            "event": "decision",
            "trade_id": "T1",
            "symbol": "QQQ",
            "ts": "2026-10-05T09:45:00-04:00",
            "trigger": {"entry_ref": 100.0},
            "mechanism_context": {
                "mechanisms": {
                    "liquidity_implementation_shortfall": {
                        "status": "OBSERVED",
                        "evidence_asof": "2026-10-05T13:44:59Z",
                        "observations": {"arrival_mid": 100.1},
                    }
                }
            },
        },
        {
            "event": "entry_fill",
            "trade_id": "T1",
            "symbol": "QQQ",
            "ts": "2026-10-05T09:45:01-04:00",
            "side": "buy",
            "fill_price": 100.2,
            "market_price_at_fill": 100.15,
            "mechanism_tags": {
                "family_id": "monday_weekend_dip_v1",
                "hypothesis_version": "v1",
            },
        },
        {
            "event": "exit_fill",
            "trade_id": "T1",
            "symbol": "QQQ",
            "ts": "2026-10-05T15:40:00-04:00",
            "fill_qty": 1,
            "fill_price": 101.8,
            "market_price_at_exit": 101.9,
            "exit_reason": "eod",
        },
    ]
    result = attribute_trade_costs(events, "T1")
    assert result["commission_usd"] == 0.0
    assert result["decision_to_fill_latency_ms"] == 1000.0
    assert result["entry"]["decision_reference_bps"] == 20.0
    assert result["entry"]["arrival_mid_bps"] > 0
    assert result["exits"][0]["exit_shortfall_bps"] > 0


def test_cost_attribution_never_invents_missing_prices():
    result = attribute_trade_costs([{"event": "exit_fill", "trade_id": "T2"}], "T2")
    assert result["measurement_status"] == "INSUFFICIENT_DATA"
    assert result["entry"]["arrival_mid_bps"] is None


def test_arrival_mid_requires_observed_timestamped_liquidity_context():
    events = [
        {
            "event": "decision",
            "trade_id": "T3",
            "mechanism_context": {
                "mechanisms": {
                    "liquidity_implementation_shortfall": {
                        "status": "PARTIAL",
                        "observations": {"arrival_mid": 100},
                    }
                }
            },
        },
        {
            "event": "entry_fill",
            "trade_id": "T3",
            "side": "buy",
            "fill_price": 101,
        },
    ]
    assert attribute_trade_costs(events, "T3")["entry"]["arrival_mid"] is None


def test_arrival_mid_rejects_malformed_or_future_provenance():
    def measure(evidence_asof):
        return attribute_trade_costs(
            [
                {
                    "event": "decision",
                    "trade_id": "T4",
                    "ts": "2026-10-05T13:45:00Z",
                    "mechanism_context": {
                        "mechanisms": {
                            "liquidity_implementation_shortfall": {
                                "status": "OBSERVED",
                                "evidence_asof": evidence_asof,
                                "observations": {"arrival_mid": 100},
                            }
                        }
                    },
                },
                {
                    "event": "entry_fill",
                    "trade_id": "T4",
                    "side": "buy",
                    "fill_price": 101,
                },
            ],
            "T4",
        )["entry"]["arrival_mid"]

    assert measure("bad") is None
    assert measure("2026-10-05T13:45:01Z") is None


def _registry(tmp_path, families):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema_v": 1, "families": families}))
    return path


def test_router_returns_zero_for_missing_rejected_and_version_mismatch(tmp_path):
    path = _registry(
        tmp_path,
        {
            "rejected": {
                "status": "REJECTED",
                "hypothesis_version": "v1",
                "independent_audit": "FAIL",
                "max_router_share": 0,
                "max_score_age_seconds": 0,
            },
            "admitted": {
                "status": "ADMITTED_PAPER",
                "hypothesis_version": "v2",
                "independent_audit": "PASS",
                "max_router_share": 1,
                "max_score_age_seconds": 3600,
            },
        },
    )
    decisions = route_families(
        [
            _candidate("missing", "v1"),
            _candidate("rejected", "v1"),
            _candidate("admitted", "wrong"),
        ],
        path,
        now=NOW,
    )
    assert all(item.allocation == 0 for item in decisions)
    assert all(not item.admitted for item in decisions)


def test_router_allocates_only_explicit_audited_exact_version(tmp_path):
    path = _registry(
        tmp_path,
        {
            "m": {
                "status": "ADMITTED_PAPER",
                "hypothesis_version": "v1",
                "independent_audit": "PASS",
                "max_router_share": 0.6,
                "max_score_age_seconds": 3600,
            },
            "x": {
                "status": "ADMITTED_PAPER",
                "hypothesis_version": "v2",
                "independent_audit": "PASS",
                "max_router_share": 0.4,
                "max_score_age_seconds": 3600,
            },
        },
    )
    decisions = route_families(
        [
            _candidate("m", "v1", 3),
            _candidate("x", "v2", 1),
        ],
        path,
        now=NOW,
    )
    by_family = {item.family_id: item for item in decisions}
    assert by_family["m"].allocation == 0.6
    assert by_family["x"].allocation == 0.25


def test_default_registry_admits_only_track_m():
    decisions = route_families(
        [
            _candidate("monday_weekend_dip_v1", "daytier-track-m-2026-10-05"),
            _candidate("orb_break_hold_v1", "daytier-track-b-2026-09-21"),
        ],
        now=NOW,
    )
    by_family = {item.family_id: item for item in decisions}
    assert by_family["monday_weekend_dip_v1"].allocation == 1.0
    assert by_family["orb_break_hold_v1"].allocation == 0.0


def test_router_fails_closed_on_unreadable_registry(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    result = route_families(
        [_candidate("m", "v1")], path, now=NOW
    )
    assert result[0].allocation == 0
    assert not result[0].admitted


def test_router_rejects_duplicate_family_and_invalid_cap(tmp_path):
    path = _registry(
        tmp_path,
        {
            "m": {
                "status": "ADMITTED_PAPER",
                "hypothesis_version": "v1",
                "independent_audit": "PASS",
                "max_router_share": 2,
                "max_score_age_seconds": 3600,
            }
        },
    )
    result = route_families(
        [
            _candidate("m", "v1"),
            _candidate("m", "v1"),
        ],
        path,
        now=NOW,
    )
    assert all(item.allocation == 0 for item in result)
    assert {item.reason for item in result} == {
        "allocation cap invalid",
        "duplicate family candidate",
    }


def test_router_rejects_missing_stale_future_and_naive_score_provenance(tmp_path):
    path = _registry(
        tmp_path,
        {
            "m": {
                "status": "ADMITTED_PAPER",
                "hypothesis_version": "v1",
                "independent_audit": "PASS",
                "max_router_share": 1,
                "max_score_age_seconds": 60,
            }
        },
    )
    candidates = [
        _candidate("m", "v1", asof=None),
        _candidate("m2", "v1", asof="2026-10-06T13:58:00Z"),
    ]
    missing = route_families([candidates[0]], path, now=NOW)[0]
    stale = route_families(
        [_candidate("m", "v1", asof="2026-10-06T13:58:00Z")], path, now=NOW
    )[0]
    future = route_families(
        [_candidate("m", "v1", asof="2026-10-06T14:01:00Z")], path, now=NOW
    )[0]
    naive = route_families(
        [_candidate("m", "v1", asof="2026-10-06T13:59:30")], path, now=NOW
    )[0]
    assert {
        missing.allocation,
        stale.allocation,
        future.allocation,
        naive.allocation,
    } == {0.0}
    assert "provenance" in missing.reason
    assert all("stale or future-dated" in item.reason for item in (stale, future))
    assert "provenance" in naive.reason
    assert not any(item.admitted for item in (missing, stale, future, naive))


def test_router_fails_closed_for_malformed_candidate_and_naive_now(tmp_path):
    path = _registry(tmp_path, {})
    malformed = route_families([None], path, now=NOW)[0]
    invalid_clock = route_families(
        [_candidate("m", "v1")],
        path,
        now=datetime(2026, 10, 6, 14, 0),
    )[0]
    assert malformed.allocation == 0
    assert not malformed.admitted
    assert malformed.reason == "candidate payload invalid"
    assert invalid_clock.allocation == 0
    assert not invalid_clock.admitted
    assert invalid_clock.reason == "evaluation timestamp invalid"

    for invalid_now in ("bad", 1, object()):
        invalid_type = route_families(
            [_candidate("m", "v1")], path, now=invalid_now
        )[0]
        assert invalid_type.allocation == 0
        assert not invalid_type.admitted
        assert invalid_type.reason == "evaluation timestamp invalid"


def test_router_never_marks_rounded_zero_share_admitted(tmp_path):
    family = {
        "status": "ADMITTED_PAPER",
        "hypothesis_version": "v1",
        "independent_audit": "PASS",
        "max_router_share": 1,
        "max_score_age_seconds": 3600,
    }
    path = _registry(tmp_path, {"tiny": family, "large": family})
    decisions = route_families(
        [
            _candidate("tiny", "v1", score=1e-12),
            _candidate("large", "v1", score=1),
        ],
        path,
        now=NOW,
    )
    by_family = {item.family_id: item for item in decisions}
    assert by_family["tiny"].allocation == 0
    assert not by_family["tiny"].admitted
    assert by_family["large"].allocation == 1
    assert by_family["large"].admitted
