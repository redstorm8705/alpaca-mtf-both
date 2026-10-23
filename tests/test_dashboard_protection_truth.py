from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import execution.risk_manager as risk_manager
import generate_dashboard as dashboard
from reporting.html_ui import tier_badges


def _order(**overrides):
    order = {
        "id": "stop-1",
        "symbol": "AAPL",
        "side": "sell",
        "qty": "1",
        "filled_qty": "0",
        "type": "stop",
        "status": "new",
        "stop_price": "328.70",
        "limit_price": None,
        "client_order_id": "IN-AAPL-s-1792073537393-7a3d7963",
    }
    order.update(overrides)
    return order


def _position(**overrides):
    position = {
        "symbol": "AAPL",
        "qty": 1.0,
        "side": "long",
        "market_value": 346.32,
        "unrealized_pl": 1.17,
        "unrealized_intraday_pl": 2.33,
        "unrealized_plpc": 0.003,
        "current_price": 346.32,
        "avg_entry_price": 345.15,
    }
    position.update(overrides)
    return position


def _ownership():
    return {
        "AAPL": {
            "alpaca_net_qty": 1.0,
            "drift": 0.0,
            "tiers": {
                "intraday": {"qty": 1.0},
                "daytrade": {"qty": 0.0},
                "qhm": {"qty": 0.0},
                "forever6": {"qty": 0.0},
            },
        }
    }


def _kill_snapshot():
    return {
        "schema": 1,
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "generated_at": datetime.now(ZoneInfo("UTC")),
        "session_date_et": (
            datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        ),
        "max_age_seconds": 900.0,
        "trading_pnl": -9.80,
        "daily_start_value": 2687.41,
        "limit_pct": 0.07,
        "limit_usd": 188.1187,
        "consumed_pct": abs(-9.80) / 188.1187 * 100.0,
        "attribution_exact": True,
        "killed": False,
        "stale_trigger_snapshot": False,
        "included_tiers": ["day", "swing"],
        "excluded_held_tiers": ["qhm", "forever_6"],
    }


def _write_durable_kill(
    root: Path, payload: dict, *, trading_pnl: float | None = None
) -> Path:
    state_path = root / "data" / "state" / "kill_switch_state.json"
    state_pnl = payload["trading_pnl"] if trading_pnl is None else trading_pnl
    daily_start = payload["daily_start_value"]
    with mock.patch.object(risk_manager, "_KILL_STATE_FILE", state_path):
        risk_manager._save_kill_state(
            killed=True,
            halt_entries=True,
            cause=risk_manager.KillVerdictCause.GENUINE_TRADING_LOSS,
            trading_pnl=state_pnl,
            daily_start_value=daily_start,
            loss_pct=state_pnl / daily_start,
            attribution_exact=True,
        )
    return state_path


def test_position_tier_badge_can_omit_redundant_share_count():
    assert "Swing 1" in tier_badges([("swing", 1)])
    badge = tier_badges([("swing", 1)], show_qty=False)
    assert "Swing" in badge
    assert "Swing 1" not in badge


def test_protection_join_reports_complete_partial_missing_and_unavailable():
    pos = _position(qty=2.0)
    tiers = [("swing", 2.0)]
    complete = dashboard._position_protection(
        pos, tiers, [_order(qty="2")], orders_available=True
    )[0]
    partial = dashboard._position_protection(
        pos, tiers, [_order(qty="1")], orders_available=True
    )[0]
    missing = dashboard._position_protection(pos, tiers, [], orders_available=True)[0]
    unavailable = dashboard._position_protection(
        pos, tiers, [], orders_available=False
    )[0]
    assert complete["status"] == "PROTECTED"
    assert partial["status"] == "PARTIAL"
    assert missing["status"] == "MISSING"
    assert unavailable["status"] == "UNAVAILABLE"


def test_oco_stop_leg_inherits_immutable_parent_tier_identity():
    child = _order(
        client_order_id="broker-generated-uuid",
        owner_client_order_id="DT-AAPL-s-1792073537393-7a3d7963",
    )
    fact = dashboard._position_protection(
        _position(), [("day", 1.0)], [child], orders_available=True
    )[0]
    assert fact["status"] == "PROTECTED"
    assert fact["stops"] == [(328.7, 1.0)]


def test_kill_snapshot_publisher_records_runtime_profile_without_changing_verdict(
    tmp_path: Path,
):
    target = tmp_path / "kill_switch_snapshot.json"
    with (
        mock.patch.object(risk_manager, "_KILL_SNAPSHOT_FILE", target),
        mock.patch.object(risk_manager.config, "MAX_DAILY_LOSS_PCT", 0.07),
    ):
        risk_manager._write_kill_snapshot({
            "schema": 1,
            "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
            "session_date_et": datetime.now(
                ZoneInfo("America/New_York")
            ).date().isoformat(),
            "max_age_seconds": 900.0,
            "trading_pnl": -9.80,
            "daily_start_value": 2687.41,
            "attribution_exact": True,
            "killed": False,
            "limit_pct": 0.07,
            "limit_usd": 188.1187,
            "consumed_pct": abs(-9.80) / 188.1187 * 100.0,
            "included_tiers": ["day", "swing"],
            "excluded_held_tiers": ["qhm", "forever_6"],
        })
    payload = json.loads(target.read_text())
    assert payload["limit_pct"] == 0.07
    assert round(payload["limit_usd"], 2) == 188.12
    assert round(payload["consumed_pct"], 1) == 5.2
    assert payload["included_tiers"] == ["day", "swing"]
    assert payload["excluded_held_tiers"] == ["qhm", "forever_6"]


def test_kill_snapshot_publisher_never_raises_or_leaves_temp_file(tmp_path: Path):
    target = tmp_path / "missing" / "kill_switch_snapshot.json"
    with (
        mock.patch.object(risk_manager, "_KILL_SNAPSHOT_FILE", target),
        mock.patch.object(risk_manager.config, "MAX_DAILY_LOSS_PCT", 0.07),
        mock.patch.object(Path, "replace", side_effect=OSError("disk failure")),
    ):
        risk_manager._write_kill_snapshot({"schema": 1})
    assert not target.exists()
    assert list(target.parent.glob(".kill_switch_snapshot.json.*.tmp")) == []


def test_kill_verdict_latches_before_snapshot_publication():
    risk = object.__new__(risk_manager.RiskManager)
    risk.killed = False
    risk.daily_start_value = 1000.0
    risk.portfolio_value = 900.0
    risk._intraday_trading_pnl = lambda: (-100.0, True)
    with (
        mock.patch.object(risk_manager.config, "MAX_DAILY_LOSS_PCT", 0.07),
        mock.patch.object(risk_manager.config, "OWNERSHIP_GUARD_ENFORCE", True),
        mock.patch.object(risk_manager, "_save_kill_state"),
        mock.patch.object(
            risk_manager,
            "_publish_kill_snapshot",
            side_effect=KeyboardInterrupt,
        ),
    ):
        try:
            risk.check_kill_switch()
        except KeyboardInterrupt:
            pass
    assert risk.killed is True


def test_legacy_symbol_exclusion_is_not_published_as_exact_attribution():
    risk = object.__new__(risk_manager.RiskManager)
    risk.killed = False
    risk.daily_start_value = 1000.0
    risk.portfolio_value = 990.0
    risk._intraday_trading_pnl = lambda: (-10.0, True)
    with (
        mock.patch.object(risk_manager.config, "MAX_DAILY_LOSS_PCT", 0.07),
        mock.patch.object(risk_manager.config, "OWNERSHIP_GUARD_ENFORCE", False),
        mock.patch.object(risk_manager, "_publish_kill_snapshot") as publish,
    ):
        assert risk.check_kill_switch() is False
    assert publish.call_args.kwargs["attribution_exact"] is False


def test_stop_coverage_uses_remaining_qty_and_rejects_transitional_or_bad_stop():
    pos = _position(qty=1.0)
    tiers = [("swing", 1.0)]
    partial_fill = _order(qty="2", filled_qty="1", status="partially_filled")
    fact = dashboard._position_protection(
        pos, tiers, [partial_fill], orders_available=True
    )[0]
    assert fact["status"] == "PROTECTED"
    assert fact["stop_qty"] == 1.0

    for bad in (
        _order(status="pending_new"),
        _order(status="pending_cancel"),
        _order(status="unknown"),
        _order(stop_price="-5"),
        _order(filled_qty=None),
    ):
        fact = dashboard._position_protection(
            pos, tiers, [bad], orders_available=True
        )[0]
        assert fact["status"] == "MISSING"

    breached = dashboard._position_protection(
        _position(current_price=90.0),
        tiers,
        [_order(stop_price="100")],
        orders_available=True,
    )[0]
    assert breached["status"] == "BREACHED"
    for bad_current in (None, 0, float("nan"), float("inf")):
        fact = dashboard._position_protection(
            _position(current_price=bad_current),
            tiers,
            [_order()],
            orders_available=True,
        )[0]
        assert fact["status"] == "UNAVAILABLE"


def test_kill_snapshot_rejects_bad_session_future_and_stale(tmp_path: Path):
    payload = _kill_snapshot()
    payload.pop("generated_at")
    target = tmp_path / "kill_switch_snapshot.json"
    with mock.patch.object(dashboard, "LOG_DIR", tmp_path):
        payload["session_date_et"] = "WRONG"
        target.write_text(json.dumps(payload))
        assert dashboard._load_kill_switch_snapshot() == {}

        payload = _kill_snapshot()
        payload.pop("generated_at")
        payload["max_age_seconds"] = 86400.0
        target.write_text(json.dumps(payload))
        assert dashboard._load_kill_switch_snapshot() == {}

        payload = _kill_snapshot()
        payload.pop("generated_at")
        payload["generated_at_utc"] = "2000-01-01T00:00:00+00:00"
        target.write_text(json.dumps(payload))
        assert dashboard._load_kill_switch_snapshot() == {}

        payload = _kill_snapshot()
        payload.pop("generated_at")
        future = datetime.now(ZoneInfo("UTC")).timestamp() + 60
        payload["generated_at_utc"] = datetime.fromtimestamp(
            future, ZoneInfo("UTC")
        ).isoformat()
        target.write_text(json.dumps(payload))
        assert dashboard._load_kill_switch_snapshot() == {}


def test_same_session_killed_snapshot_remains_visible_after_freshness_window(
    tmp_path: Path,
):
    payload = _kill_snapshot()
    payload.pop("generated_at")
    payload["killed"] = True
    payload["trading_pnl"] = -200.0
    payload["consumed_pct"] = (
        abs(payload["trading_pnl"]) / payload["limit_usd"] * 100.0
    )
    old = datetime.now(ZoneInfo("UTC")).timestamp() - 3600
    payload["generated_at_utc"] = datetime.fromtimestamp(
        old, ZoneInfo("UTC")
    ).isoformat()
    (tmp_path / "kill_switch_snapshot.json").write_text(json.dumps(payload))
    state_path = _write_durable_kill(tmp_path, payload)
    with (
        mock.patch.object(dashboard, "LOG_DIR", tmp_path),
        mock.patch.object(dashboard, "ROOT", tmp_path),
    ):
        loaded = dashboard._load_kill_switch_snapshot()
        assert loaded["killed"] is True
        assert loaded["stale_trigger_snapshot"] is True

    state_path.unlink()
    with (
        mock.patch.object(dashboard, "LOG_DIR", tmp_path),
        mock.patch.object(dashboard, "ROOT", tmp_path),
    ):
        assert dashboard._load_kill_switch_snapshot() == {}


def test_expired_kill_rejects_durable_state_without_real_threshold_breach(
    tmp_path: Path,
):
    payload = _kill_snapshot()
    payload.pop("generated_at")
    payload["killed"] = True
    old = datetime.now(ZoneInfo("UTC")).timestamp() - 3600
    payload["generated_at_utc"] = datetime.fromtimestamp(
        old, ZoneInfo("UTC")
    ).isoformat()
    (tmp_path / "kill_switch_snapshot.json").write_text(json.dumps(payload))
    _write_durable_kill(tmp_path, payload)
    with (
        mock.patch.object(dashboard, "LOG_DIR", tmp_path),
        mock.patch.object(dashboard, "ROOT", tmp_path),
    ):
        assert dashboard._load_kill_switch_snapshot() == {}


def test_fresh_pre_kill_snapshot_cannot_contradict_durable_latch(tmp_path: Path):
    payload = _kill_snapshot()
    payload.pop("generated_at")
    payload["killed"] = False
    (tmp_path / "kill_switch_snapshot.json").write_text(json.dumps(payload))
    _write_durable_kill(tmp_path, payload, trading_pnl=-200.0)
    with (
        mock.patch.object(dashboard, "LOG_DIR", tmp_path),
        mock.patch.object(dashboard, "ROOT", tmp_path),
    ):
        assert dashboard._load_kill_switch_snapshot() == {}


def test_kill_snapshot_rejects_killed_flag_that_contradicts_its_math(
    tmp_path: Path,
):
    target = tmp_path / "kill_switch_snapshot.json"
    for trading_pnl, killed in ((-1.0, True), (-80.0, False)):
        payload = _kill_snapshot()
        payload.pop("generated_at")
        payload.update({
            "daily_start_value": 1000.0,
            "limit_pct": 0.07,
            "limit_usd": 70.0,
            "trading_pnl": trading_pnl,
            "consumed_pct": abs(trading_pnl) / 70.0 * 100.0,
            "killed": killed,
        })
        target.write_text(json.dumps(payload))
        with (
            mock.patch.object(dashboard, "LOG_DIR", tmp_path),
            mock.patch.object(dashboard, "ROOT", tmp_path),
        ):
            assert dashboard._load_kill_switch_snapshot() == {}


def test_dashboard_renders_broker_stop_action_table_and_exact_daily_brake(
    tmp_path: Path,
):
    alpaca = {
        "equity": 2641.04,
        "last_equity": 2687.41,
        "cash": 1935.08,
        "buying_power": 6114.76,
        "status": "ACTIVE",
        "positions": [_position()],
        "orders": [_order()],
        "orders_available": True,
        "error": None,
    }
    trade_log = {
        "open": [{
            "symbol": "AAPL",
            "entry_price": 345.15,
            "target": 360.0,
            "score": 10,
        }],
        "closed": [],
        "stats": {},
    }
    with (
        mock.patch.object(dashboard, "LOG_DIR", tmp_path),
        mock.patch.object(dashboard, "_load_edge_snapshot", return_value={}),
        mock.patch.object(
            dashboard, "_load_ownership_positions", return_value=_ownership()
        ),
        mock.patch.object(
            dashboard, "_load_kill_switch_snapshot", return_value=_kill_snapshot()
        ),
        mock.patch.object(dashboard, "_compute_spy_levels", return_value={}),
        mock.patch.object(
            dashboard,
            "compute_lifetime_stats",
            return_value={"total_pnl": 141.04},
        ),
    ):
        html = dashboard._build_html(
            alpaca,
            trade_log,
            {},
            {"trades": []},
            bot_status={},
            market_news={"items": []},
        )
    assert "Swing 1" not in html
    assert ">Swing<" in html
    assert "$328.70 (1 sh)" in html
    assert "1/1 protected" in html
    assert "PROTECTED" in html
    assert "Protection &amp; next action" in html
    assert ">Open Orders<" not in html
    assert "Day + Swing loss vs 7% daily brake" in html
    assert "$-9.80 of $188.12" in html
    assert "5.2% used" in html


def test_dashboard_labels_expired_kill_values_as_trigger_time_not_current():
    payload = _kill_snapshot()
    payload["killed"] = True
    payload["stale_trigger_snapshot"] = True
    with (
        mock.patch.object(
            dashboard, "_load_kill_switch_snapshot", return_value=payload
        ),
        mock.patch.object(dashboard, "compute_lifetime_stats", return_value={}),
        mock.patch.object(dashboard, "_load_edge_snapshot", return_value={}),
        mock.patch.object(dashboard, "_load_ownership_positions", return_value={}),
        mock.patch.object(dashboard, "_compute_spy_levels", return_value={}),
    ):
        html = dashboard._build_html(
            {"equity": 2500, "positions": [], "orders": []},
            {"open": [], "closed": []},
            {},
            {},
        )
    assert "Daily brake TRIGGERED" in html
    assert "Trigger-time loss" in html
    assert "current loss unavailable" in html


def test_dashboard_rejects_wrong_day_or_inexact_kill_snapshot(tmp_path: Path):
    payload = _kill_snapshot()
    payload.pop("generated_at")
    (tmp_path / "kill_switch_snapshot.json").write_text(json.dumps(payload))
    with mock.patch.object(dashboard, "LOG_DIR", tmp_path):
        assert dashboard._load_kill_switch_snapshot()

    payload["attribution_exact"] = False
    (tmp_path / "kill_switch_snapshot.json").write_text(json.dumps(payload))
    with mock.patch.object(dashboard, "LOG_DIR", tmp_path):
        assert dashboard._load_kill_switch_snapshot() == {}
