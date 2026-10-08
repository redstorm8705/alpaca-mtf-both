"""Cross-tier ownership snapshot contract.  Authored-by: OpenAI Codex (GPT-6)."""

import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

import execution
from execution.ownership_snapshot import build_ownership_snapshot

ET = ZoneInfo("America/New_York")


def _ledger(**claims):
    positions = {}
    for symbol, tiers in claims.items():
        positions[symbol] = {
            "tiers": {tier: {"qty": qty} for tier, qty in tiers.items()}
        }
    return {"positions": positions}


def test_exclusive_daytrade_long_and_short_require_signed_full_net():
    positions = [
        SimpleNamespace(symbol="AAPL", qty="2"),
        SimpleNamespace(symbol="EWY", qty="-1"),
    ]
    ledger = _ledger(AAPL={"daytrade": 2}, EWY={"daytrade": -1})
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims",
        return_value={"AAPL": 2, "EWY": -1},
    ):
        snap = build_ownership_snapshot(positions, ledger)
    assert snap.exclusively_owned("AAPL", "daytrade")
    assert snap.exclusively_owned("EWY", "daytrade")
    assert snap.foreign_symbols("daytrade") == frozenset()


def test_partial_claim_detects_a_cohold_instead_of_hiding_it():
    positions = [SimpleNamespace(symbol="AAPL", qty="3")]
    ledger = _ledger(AAPL={"daytrade": 1, "intraday": 2})
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims", return_value={"AAPL": 1}
    ):
        snap = build_ownership_snapshot(positions, ledger)
    assert not snap.exclusively_owned("AAPL", "daytrade")
    assert snap.claimants("AAPL") == frozenset({"daytrade", "intraday"})
    assert snap.foreign_symbols("daytrade") == frozenset({"AAPL"})


def test_realtime_daytrade_fill_replaces_stale_zero_ledger_claim():
    positions = [SimpleNamespace(symbol="NVDA", qty="1")]
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims",
        return_value={"NVDA": 1.0},
    ):
        snap = build_ownership_snapshot(positions, _ledger())
    assert snap.exclusively_owned("NVDA", "daytrade")
    assert snap.residual_qty("NVDA") == 0.0


def test_realtime_claim_does_not_erase_a_foreign_ledger_claim():
    positions = [SimpleNamespace(symbol="NVDA", qty="2")]
    ledger = _ledger(NVDA={"intraday": 1})
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims",
        return_value={"NVDA": 1.0},
    ):
        snap = build_ownership_snapshot(positions, ledger)
    assert not snap.exclusively_owned("NVDA", "daytrade")
    assert snap.residual_qty("NVDA") == 0.0


def test_stale_prior_day_log_does_not_claim_current_position():
    trades = {
        "old": {
            "symbol": "AAPL",
            "fill_qty": 1,
            "side": "long",
            "entry_ts": "2026-10-06T10:00:00-04:00",
        }
    }
    with mock.patch(
        "strategy.day_tier_logger.open_trades_from_log_checked",
        return_value=(trades, True),
    ):
        snap = build_ownership_snapshot(
            [SimpleNamespace(symbol="AAPL", qty="1")],
            _ledger(),
            now_et=datetime(2026, 10, 7, 12, tzinfo=ET),
        )
    assert snap.foreign_symbols("daytrade") == frozenset({"AAPL"})


def test_daytrade_log_failure_cannot_trust_a_stale_ledger_claim():
    positions = [SimpleNamespace(symbol="NVDA", qty="1")]
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims",
        side_effect=OSError("bad log"),
    ):
        snap = build_ownership_snapshot(positions, _ledger(NVDA={"daytrade": 1}))
    assert snap.errors == ("daytrade_log:OSError",)
    assert not snap.exclusively_owned("NVDA", "daytrade")
    assert snap.foreign_symbols("daytrade") == frozenset({"NVDA"})


def test_ledger_failure_cannot_trust_the_realtime_log_as_the_whole_account():
    positions = [SimpleNamespace(symbol="NVDA", qty="1")]
    fake_guard = ModuleType("execution.ownership_guard")
    fake_guard.load_ledger = mock.Mock(side_effect=ValueError("corrupt"))
    with (
        mock.patch.dict(sys.modules, {"execution.ownership_guard": fake_guard}),
        mock.patch(
            "execution.ownership_snapshot._today_daytrade_claims",
            return_value={"NVDA": 1.0},
        ),
    ):
        snap = build_ownership_snapshot(positions)
    assert snap.errors == ("ownership_ledger:ValueError",)
    assert not snap.exclusively_owned("NVDA", "daytrade")
    assert snap.foreign_symbols("daytrade") == frozenset({"NVDA"})


def test_broker_failure_is_not_misrepresented_as_an_empty_book():
    fake_broker = ModuleType("execution.broker")
    fake_broker.get_open_positions = mock.Mock(side_effect=OSError("down"))
    with (
        mock.patch.dict(sys.modules, {"execution.broker": fake_broker}),
        mock.patch.object(execution, "broker", fake_broker, create=True),
    ):
        try:
            build_ownership_snapshot()
        except RuntimeError as exc:
            assert "broker positions unreadable" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("broker failure must raise")


def test_duplicate_broker_rows_fail_closed_instead_of_overwriting_quantity():
    positions = [
        SimpleNamespace(symbol="AAPL", qty="2"),
        SimpleNamespace(symbol="AAPL", qty="1"),
    ]
    try:
        build_ownership_snapshot(positions, _ledger(AAPL={"daytrade": 1}))
    except RuntimeError as exc:
        assert "duplicate broker position" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("duplicate broker rows must fail closed")


def test_nonfinite_broker_quantity_fails_closed_instead_of_disappearing():
    try:
        build_ownership_snapshot([SimpleNamespace(symbol="AAPL", qty="nan")], _ledger())
    except RuntimeError as exc:
        assert "non-finite qty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("non-finite broker quantity must fail closed")


def test_opposite_foreign_claims_cannot_cancel_each_other():
    positions = [SimpleNamespace(symbol="AAPL", qty="1")]
    ledger = _ledger(AAPL={"daytrade": 1, "intraday": -1, "qhm": 1})
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims", return_value={"AAPL": 1}
    ):
        snap = build_ownership_snapshot(positions, ledger)
    assert not snap.exclusively_owned("AAPL", "daytrade")


def test_none_broker_payload_fails_closed():
    fake_broker = ModuleType("execution.broker")
    fake_broker.get_open_positions = mock.Mock(return_value=None)
    with (
        mock.patch.dict(sys.modules, {"execution.broker": fake_broker}),
        mock.patch.object(execution, "broker", fake_broker, create=True),
    ):
        try:
            build_ownership_snapshot()
        except RuntimeError as exc:
            assert "payload is None" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("None broker payload must fail closed")


def test_clean_empty_day_log_clears_stale_daytrade_ledger_claim():
    positions = [SimpleNamespace(symbol="AAPL", qty="1")]
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims", return_value={}
    ):
        snap = build_ownership_snapshot(positions, _ledger(AAPL={"daytrade": 1}))
    assert not snap.exclusively_owned("AAPL", "daytrade")
    assert snap.foreign_symbols("daytrade") == frozenset({"AAPL"})


def test_malformed_or_nonfinite_ledger_claim_denies_exclusivity():
    positions = [SimpleNamespace(symbol="NVDA", qty="1")]
    malformed = {"positions": {"NVDA": {"tiers": {"qhm": {"qty": "nan"}}}}}
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims", return_value={"NVDA": 1}
    ):
        snap = build_ownership_snapshot(positions, malformed)
    assert "ledger_claim_nonfinite:NVDA:qhm" in snap.errors
    assert not snap.exclusively_owned("NVDA", "daytrade")


def test_incomplete_real_day_log_is_an_ownership_source_error():
    with mock.patch(
        "strategy.day_tier_logger.open_trades_from_log_checked",
        return_value=({}, False),
    ):
        try:
            from execution.ownership_snapshot import _today_daytrade_claims

            _today_daytrade_claims(datetime(2026, 10, 7, 12, tzinfo=ET))
        except ValueError as exc:
            assert "incomplete" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("partial lifecycle log must fail closed")


def test_nonfinite_day_log_qty_is_an_ownership_source_error():
    trades = {
        "bad": {
            "symbol": "NVDA",
            "fill_qty": "nan",
            "side": "long",
            "entry_ts": "2026-10-07T10:00:00-04:00",
        }
    }
    with mock.patch(
        "strategy.day_tier_logger.open_trades_from_log_checked",
        return_value=(trades, True),
    ):
        try:
            from execution.ownership_snapshot import _today_daytrade_claims

            _today_daytrade_claims(datetime(2026, 10, 7, 12, tzinfo=ET))
        except ValueError as exc:
            assert "non-finite" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("non-finite lifecycle qty must fail closed")


def test_real_checked_replay_marks_semantically_bad_partial_exit_incomplete():
    from strategy import day_tier_logger

    events = [
        {"event": "entry_fill", "trade_id": "DT-1", "fill_qty": 2},
        {"event": "partial_exit_fill", "trade_id": "DT-1", "fill_qty": "bad"},
    ]
    with mock.patch.object(
        day_tier_logger, "read_events_checked", return_value=(events, True)
    ):
        trades, complete = day_tier_logger.open_trades_from_log_checked()
    assert not complete
    assert trades["DT-1"]["fill_qty"] == 2


def test_malformed_ledger_container_denies_exclusivity():
    positions = [SimpleNamespace(symbol="NVDA", qty="1")]
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims", return_value={"NVDA": 1}
    ):
        snap = build_ownership_snapshot(positions, {"positions": []})
    assert "ownership_ledger:invalid_schema" in snap.errors
    assert not snap.exclusively_owned("NVDA", "daytrade")


def test_missing_broker_qty_fails_closed():
    try:
        build_ownership_snapshot([SimpleNamespace(symbol="AAPL")], _ledger())
    except RuntimeError as exc:
        assert "missing qty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing broker qty must fail closed")


def test_duplicate_entry_fill_id_makes_lifecycle_source_incomplete():
    from strategy import day_tier_logger

    events = [
        {"event": "entry_fill", "trade_id": "DT-1", "fill_qty": 1},
        {"event": "entry_fill", "trade_id": "DT-1", "fill_qty": 2},
    ]
    with mock.patch.object(
        day_tier_logger, "read_events_checked", return_value=(events, True)
    ):
        _, complete = day_tier_logger.open_trades_from_log_checked()
    assert not complete


def test_date_only_lifecycle_timestamp_cannot_prove_ownership():
    trades = {
        "bad": {
            "symbol": "NVDA",
            "fill_qty": 1,
            "side": "long",
            "entry_ts": "2026-10-07",
        }
    }
    with mock.patch(
        "strategy.day_tier_logger.open_trades_from_log_checked",
        return_value=(trades, True),
    ):
        try:
            from execution.ownership_snapshot import _today_daytrade_claims

            _today_daytrade_claims(datetime(2026, 10, 7, 12, tzinfo=ET))
        except ValueError as exc:
            assert "timestamp invalid" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("date-only lifecycle timestamp must fail closed")


def test_unknown_ledger_tier_denies_exclusivity():
    positions = [SimpleNamespace(symbol="NVDA", qty="1")]
    ledger = {"positions": {"NVDA": {"tiers": {"swing": {"qty": 1}}}}}
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims", return_value={"NVDA": 1}
    ):
        snap = build_ownership_snapshot(positions, ledger)
    assert "ledger_unknown_tier:NVDA:swing" in snap.errors
    assert not snap.exclusively_owned("NVDA", "daytrade")


def test_present_malformed_zero_like_ledger_claim_denies_exclusivity():
    positions = [SimpleNamespace(symbol="AAPL", qty="1")]
    ledger = {"positions": {"AAPL": {"tiers": {"intraday": {"qty": ""}}}}}
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims", return_value={"AAPL": 1}
    ):
        snap = build_ownership_snapshot(positions, ledger)
    assert "ledger_claim:AAPL:intraday" in snap.errors
    assert not snap.exclusively_owned("AAPL", "daytrade")


def test_missing_symbol_in_open_lifecycle_claim_denies_exclusivity():
    trades = {
        "good": {
            "symbol": "AAPL",
            "fill_qty": 1,
            "side": "long",
            "entry_ts": "2026-10-07T10:00:00-04:00",
        },
        "bad": {
            "symbol": None,
            "fill_qty": 1,
            "side": "long",
            "entry_ts": "2026-10-07T10:01:00-04:00",
        },
    }
    with mock.patch(
        "strategy.day_tier_logger.open_trades_from_log_checked",
        return_value=(trades, True),
    ):
        snap = build_ownership_snapshot(
            [SimpleNamespace(symbol="AAPL", qty="1")],
            _ledger(),
            now_et=datetime(2026, 10, 7, 12, tzinfo=ET),
        )
    assert snap.errors == ("daytrade_log:ValueError",)
    assert not snap.exclusively_owned("AAPL", "daytrade")


def test_whitespace_padded_ledger_symbol_denies_exclusivity():
    positions = [SimpleNamespace(symbol="NVDA", qty="1")]
    ledger = {"positions": {" NVDA ": {"tiers": {"intraday": {"qty": 1}}}}}
    with mock.patch(
        "execution.ownership_snapshot._today_daytrade_claims",
        return_value={"NVDA": 1},
    ):
        snap = build_ownership_snapshot(positions, ledger)
    assert "ledger_symbol:' NVDA '" in snap.errors
    assert not snap.exclusively_owned("NVDA", "daytrade")


def test_future_dated_lifecycle_claim_denies_exclusivity():
    trades = {
        "future": {
            "symbol": "NVDA",
            "fill_qty": 1,
            "side": "long",
            "entry_ts": "2026-10-07T15:00:00-04:00",
        }
    }
    with mock.patch(
        "strategy.day_tier_logger.open_trades_from_log_checked",
        return_value=(trades, True),
    ):
        snap = build_ownership_snapshot(
            [SimpleNamespace(symbol="NVDA", qty="1")],
            _ledger(),
            now_et=datetime(2026, 10, 7, 10, tzinfo=ET),
        )
    assert snap.errors == ("daytrade_log:ValueError",)
    assert not snap.exclusively_owned("NVDA", "daytrade")


def test_boolean_broker_quantity_fails_closed():
    try:
        build_ownership_snapshot([SimpleNamespace(symbol="AAPL", qty=True)], _ledger())
    except RuntimeError as exc:
        assert "invalid qty" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("boolean broker quantity must fail closed")


def test_whitespace_trade_id_cannot_certify_ownership():
    from strategy import day_tier_logger

    events = [
        {
            "event": "entry_fill",
            "trade_id": " ",
            "symbol": "NVDA",
            "side": "long",
            "fill_qty": 1,
            "ts": "2026-10-07T10:00:00-04:00",
        }
    ]
    with mock.patch.object(
        day_tier_logger, "read_events_checked", return_value=(events, True)
    ):
        trades, complete = day_tier_logger.open_trades_from_log_checked()
    assert not complete
    assert " " in trades


def test_even_bounded_future_timestamp_cannot_certify_ownership():
    trades = {
        "future": {
            "symbol": "NVDA",
            "fill_qty": 1,
            "side": "long",
            "entry_ts": "2026-10-07T10:00:05-04:00",
        }
    }
    with mock.patch(
        "strategy.day_tier_logger.open_trades_from_log_checked",
        return_value=(trades, True),
    ):
        snap = build_ownership_snapshot(
            [SimpleNamespace(symbol="NVDA", qty="1")],
            _ledger(),
            now_et=datetime(2026, 10, 7, 10, tzinfo=ET),
        )
    assert snap.errors == ("daytrade_log:ValueError",)
    assert not snap.exclusively_owned("NVDA", "daytrade")
