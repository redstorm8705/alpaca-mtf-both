from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from options_scanner import _primary_action_card
from monthly_review import _strategy_edge_html
from generate_dashboard import _canonical_position_tiers, _load_edge_snapshot
from reporting.html_ui import primary_nav, tier_badges, tier_performance_table
from reporting.report_figures import ReportFigures, _write_strategy_edge_snapshot


def _figures() -> ReportFigures:
    return ReportFigures(
        available=True,
        version="ux-test",
        ts_pt="2026-09-27 12:00 PT",
        equity=2600.0,
        net_deposits=2500.0,
        round_trips=[{
            "symbol": "AAPL", "entry_time": "2026-09-20T10:00:00Z",
            "exit_time": "2026-09-21T10:00:00Z", "exit_date": "2026-09-21",
            "pnl": 10.0, "tier": "intraday", "entry_order_id": "entry-1",
            "lifecycle_complete": True,
        }],
        _by_exit_date={"2026-09-21": []},
    )


def test_shared_navigation_has_every_operator_page_and_active_state():
    html = primary_nav("options")
    for href in ("dashboard.html", "scan_results.html", "options.html",
                 "weekly_review.html", "monthly_review.html"):
        assert href in html
    assert 'href="options.html" class="active" aria-current="page"' in html


def test_scanner_header_wraps_shared_navigation_on_small_screens():
    source = Path("scan_to_html.py").read_text()
    assert 'class="scanner-header"' in source
    assert "flex-wrap:wrap" in source


def test_options_primary_action_is_unambiguous_and_preserves_alternatives():
    html = _primary_action_card({
        "direction": "put", "symbol": "SPY", "strike": 650,
        "expiry": "2026-10-02", "premium_mid": 2.25, "score": 11,
        "cost_pct": 0.08,
    }, "Weekly", "#00e5ff")
    assert "BUY PUT" in html
    assert "primary qualified setup" in html
    assert "Every other qualified setup" in html
    assert "take both directions" in html


def test_options_primary_action_rejects_unknown_direction_and_escapes_fields():
    assert "No actionable setup" in _primary_action_card(
        {"direction": "unknown"}, "Weekly", "#fff")
    html = _primary_action_card({
        "direction": "call", "symbol": '<img src=x onerror="bad">',
        "expiry": "<script>bad</script>", "strike": 1, "score": 10,
    }, "Weekly", "#fff")
    assert "<img" not in html and "<script>bad" not in html
    assert "&lt;img" in html and "&lt;script&gt;" in html


def test_tier_components_use_current_bot_vocabulary():
    badges = tier_badges([("intraday", 2), ("qhm", 1)])
    assert "Swing 2" in badges
    assert "QHM 1" in badges
    table = tier_performance_table(_figures().strategy_edge_stats())
    for label in ("Swing", "Day", "QHM", "Forever 6", "Unattributed"):
        assert label in table
    assert "+$10.00" in table


def test_canonical_and_legacy_badges_share_labels_and_colors():
    canonical = tier_badges(
        [("swing", 1), ("day", 1), ("qhm", 1), ("forever_6", 1)]
    )
    legacy = tier_badges(
        [("intraday", 1), ("daytrade", 1), ("qhm", 1), ("forever6", 1)]
    )
    assert canonical == legacy
    assert "Unattributed" not in canonical


def test_edge_snapshot_is_atomic_and_carries_integrity(tmp_path: Path):
    target = tmp_path / "strategy_edge_snapshot.json"
    with mock.patch("reporting.report_figures._EDGE_SNAPSHOT", target):
        _write_strategy_edge_snapshot(_figures())
    payload = json.loads(target.read_text())
    assert payload["integrity_ok"] is True
    assert payload["generated_at_utc"]
    assert payload["overall"]["completed_trades"] == 1
    assert payload["by_tier"]["intraday"]["realized_pnl"] == 10.0
    assert not target.with_suffix(".tmp").exists()


def test_dashboard_rejects_stale_or_malformed_edge_snapshot(tmp_path: Path):
    target = tmp_path / "strategy_edge_snapshot.json"
    base = {
        "schema": 1, "integrity_ok": True,
        "generated_at_utc": "2020-01-01T00:00:00+00:00",
        "overall": {"completed_trades": 1, "realized_pnl": 1, "win_rate": 50,
                    "profit_factor": 1},
        "by_tier": {},
    }
    target.write_text(json.dumps(base))
    with mock.patch("generate_dashboard.LOG_DIR", tmp_path):
        assert _load_edge_snapshot() == {}
    base["generated_at_utc"] = "not-a-date"
    target.write_text(json.dumps(base))
    with mock.patch("generate_dashboard.LOG_DIR", tmp_path):
        assert _load_edge_snapshot() == {}


def test_dashboard_rejects_nonfinite_tier_metrics(tmp_path: Path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    target = tmp_path / "strategy_edge_snapshot.json"
    target.write_text(json.dumps({
        "schema": 1, "integrity_ok": True,
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "overall": {"completed_trades": 1, "realized_pnl": 1,
                    "win_rate": 50, "profit_factor": 1},
        "by_tier": {"intraday": {"completed_trades": "nan",
                                   "realized_pnl": "nan", "win_rate": "nan"}},
    }))
    with mock.patch("generate_dashboard.LOG_DIR", tmp_path):
        assert _load_edge_snapshot() == {}


def test_dashboard_normalizes_legacy_edge_tiers_and_accepts_canonical(tmp_path: Path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    metrics = {
        "completed_trades": 1,
        "realized_pnl": 2,
        "win_rate": 100,
        "profit_factor": 2,
    }
    target = tmp_path / "strategy_edge_snapshot.json"
    target.write_text(json.dumps({
        "schema": 1,
        "integrity_ok": True,
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "overall": metrics,
        "by_tier": {"intraday": metrics, "day": metrics},
    }))
    with mock.patch("generate_dashboard.LOG_DIR", tmp_path):
        loaded = _load_edge_snapshot()
    assert set(loaded["by_tier"]) == {"swing", "day"}


def test_dashboard_rejects_legacy_canonical_tier_collision(tmp_path: Path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    metrics = {
        "completed_trades": 1,
        "realized_pnl": 2,
        "win_rate": 100,
        "profit_factor": 2,
    }
    target = tmp_path / "strategy_edge_snapshot.json"
    target.write_text(json.dumps({
        "schema": 1,
        "integrity_ok": True,
        "generated_at_utc": datetime.now(ZoneInfo("UTC")).isoformat(),
        "overall": metrics,
        "by_tier": {"intraday": metrics, "swing": metrics},
    }))
    with mock.patch("generate_dashboard.LOG_DIR", tmp_path):
        assert _load_edge_snapshot() == {}


def test_tier_performance_rejects_legacy_canonical_collision():
    edge = _figures().strategy_edge_stats()
    edge["by_tier"]["swing"] = edge["by_tier"]["intraday"]
    html = tier_performance_table(edge)
    assert "ambiguous tier attribution" in html
    assert "+$10.00" not in html


def test_dashboard_ownership_rejects_nonfinite_foreign_claim():
    rec = {
        "alpaca_net_qty": 1,
        "drift": 0,
        "tiers": {
            "intraday": {"qty": "nan"},
            "daytrade": {"qty": 1},
        },
    }
    assert _canonical_position_tiers(rec, 1) == []


def test_dashboard_ownership_rejects_boolean_or_missing_quantities():
    for rec in (
        {
            "alpaca_net_qty": True,
            "drift": False,
            "tiers": {"daytrade": {"qty": True}},
        },
        {
            "alpaca_net_qty": 1,
            "drift": 0,
            "tiers": {"intraday": {}, "daytrade": {"qty": 1}},
        },
        {
            "alpaca_net_qty": 1,
            "drift": 0,
            "tiers": {"intraday": {"qty": False}, "daytrade": {"qty": 1}},
        },
    ):
        assert _canonical_position_tiers(rec, 1) == []


def test_monthly_accepts_canonical_tiers_and_rejects_alias_collision():
    figures = _figures()
    original = figures.strategy_edge_stats()
    canonical = dict(original)
    canonical["by_tier"] = {
        "swing": original["by_tier"]["intraday"],
        "day": original["by_tier"]["daytrade"],
        "qhm": original["by_tier"]["qhm"],
        "forever_6": original["by_tier"]["forever6"],
        "unattributed": original["by_tier"]["unattributed"],
    }
    with mock.patch.object(figures, "strategy_edge_stats", return_value=canonical):
        html, _ = _strategy_edge_html(figures)
    assert "Swing" in html and "Day" in html and "Forever 6" in html

    collision = dict(original)
    collision["by_tier"] = dict(original["by_tier"])
    collision["by_tier"]["swing"] = original["by_tier"]["intraday"]
    with mock.patch.object(figures, "strategy_edge_stats", return_value=collision):
        html, payload = _strategy_edge_html(figures)
    assert "ambiguous tier attribution" in html
    assert payload == {}
