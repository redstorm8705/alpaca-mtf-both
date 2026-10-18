"""F6 trim rungs advance only after exact fill and canonical ownership replay."""

from __future__ import annotations

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading

from execution import broker, forever_hold_manager as f6, ownership_guard


def _order(*, status: str, filled: int, oid: str = "trim-1", qty: int = 2):
    return SimpleNamespace(
        id=oid,
        client_order_id="F6-NVDA-s-1-trim",
        symbol="NVDA",
        side="sell",
        status=status,
        qty=str(qty),
        filled_qty=str(filled),
    )


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr(f6, "_TRIM_STATE", tmp_path / "forever6_trims.json")
    monkeypatch.setattr(f6, "_TRIM_STATE_LOCK", tmp_path / ".forever6_trims.lock")
    return f6.ForeverHoldManager(SimpleNamespace(), quote_loader=lambda _s: None)


def _reserved(manager, *, qty=2, baseline=8):
    assert manager._save_trim_state(
        {
            "NVDA": {
                "trim1": "reserved",
                "trim1_order": {
                    "order_id": "trim-1",
                    "client_order_id": "F6-NVDA-s-1-trim",
                    "qty": qty,
                    "baseline_qty": baseline,
                },
            },
        }
    )


def test_acknowledgement_stays_reserved_until_terminal_fill(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    seen: dict[str, object] = {}

    def submit(_symbol, qty, _label, coid):
        acknowledged = SimpleNamespace(
            id="trim-1", client_order_id=coid, symbol="NVDA", side="sell",
            status="accepted", qty=str(qty), filled_qty="0",
        )
        seen["order"] = acknowledged
        return acknowledged

    monkeypatch.setattr(manager, "_submit_trim", submit)
    monkeypatch.setattr(broker, "get_order", lambda _oid: seen["order"])

    fired, sold = manager._try_rung("NVDA", "trim1", 2, "10x", 10.0, 8.0)

    assert (fired, sold) == (False, 0)
    row = manager._load_trim_state()["NVDA"]
    assert row["trim1"] == "reserved"
    assert row["trim1_order"]["order_id"] == "trim-1"


def test_terminal_fill_updates_f6_claim_then_marks_done(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager)
    terminal = _order(status="filled", filled=2)
    monkeypatch.setattr(broker, "get_order", lambda _oid: terminal)
    monkeypatch.setattr(
        broker,
        "get_open_position",
        lambda _symbol: SimpleNamespace(qty="6", side="long"),
    )
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 8.0,
                "drift": 0.0,
                "tiers": {
                    "intraday": {"qty": 0.0},
                    "daytrade": {"qty": 0.0},
                    "qhm": {"qty": 0.0},
                    "forever6": {"qty": 8.0},
                },
            },
        },
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)

    def apply(symbol, tier, filled, net, source):
        assert (symbol, tier, filled, net, source) == (
            "NVDA",
            "forever6",
            2,
            6.0,
            "f6_trim:trim-1",
        )
        ledger["positions"]["NVDA"]["tiers"]["forever6"]["qty"] = 6.0
        ledger["positions"]["NVDA"]["alpaca_net_qty"] = 6.0
        return True

    monkeypatch.setattr(ownership_guard, "apply_authorized_tier_fill_reduction", apply)
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync,
        "sync_once",
        lambda: {"ok": True, "healed": True},
    )

    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (True, 2)
    row = manager._load_trim_state()["NVDA"]
    assert row["trim1"] == "done"
    assert row["trim1_order"]["filled_qty"] == 2
    assert ledger["positions"]["NVDA"]["tiers"]["forever6"]["qty"] == 6.0


def test_terminal_zero_fill_reopens_only_after_clean_replay(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager)
    monkeypatch.setattr(
        broker,
        "get_order",
        lambda _oid: _order(status="canceled", filled=0),
    )
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 8.0,
                "drift": 0.0,
                "tiers": {"forever6": {"qty": 8.0}},
            },
        },
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync,
        "sync_once",
        lambda: {"ok": True, "healed": True},
    )

    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (False, 0)
    row = manager._load_trim_state()["NVDA"]
    assert row["trim1"] == "none"
    assert "trim1_order" not in row


def test_contradictory_terminal_evidence_stays_reserved(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager)
    wrong = _order(status="filled", filled=2, oid="someone-elses-order")
    monkeypatch.setattr(broker, "get_order", lambda _oid: wrong)

    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (False, 0)
    assert manager._load_trim_state()["NVDA"]["trim1"] == "reserved"


def test_already_applied_fill_is_replay_safe(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager)
    monkeypatch.setattr(
        broker,
        "get_order",
        lambda _oid: _order(status="filled", filled=2),
    )
    monkeypatch.setattr(
        broker,
        "get_open_position",
        lambda _symbol: SimpleNamespace(qty="6", side="long"),
    )
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 6.0,
                "drift": 0.0,
                "tiers": {"forever6": {"qty": 6.0}},
            },
        },
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)
    monkeypatch.setattr(
        ownership_guard,
        "apply_authorized_tier_fill_reduction",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not reapply")),
    )
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync,
        "sync_once",
        lambda: {"ok": True, "healed": True},
    )

    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (True, 2)
    assert manager._load_trim_state()["NVDA"]["trim1"] == "done"


def test_lost_submit_response_recovers_exact_pre_reserved_coid(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager)
    state = manager._load_trim_state()
    state["NVDA"]["trim1_order"]["order_id"] = None
    manager._save_trim_state(state)
    terminal = _order(status="filled", filled=2)
    monkeypatch.setattr(broker, "get_order_by_client_order_id", lambda _coid: terminal)
    monkeypatch.setattr(
        broker,
        "get_open_position",
        lambda _symbol: SimpleNamespace(qty="6", side="long"),
    )
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 8.0,
                "drift": 0.0,
                "tiers": {"forever6": {"qty": 8.0}},
            }
        },
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)

    def apply(*_args):
        ledger["positions"]["NVDA"]["alpaca_net_qty"] = 6.0
        ledger["positions"]["NVDA"]["tiers"]["forever6"]["qty"] = 6.0
        return True

    monkeypatch.setattr(ownership_guard, "apply_authorized_tier_fill_reduction", apply)
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync, "sync_once", lambda: {"ok": True, "healed": True}
    )

    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (True, 2)
    row = manager._load_trim_state()["NVDA"]
    assert row["trim1"] == "done"
    assert row["trim1_order"]["order_id"] == "trim-1"


def test_terminal_partial_expiry_reconciles_exact_filled_qty(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager, qty=2, baseline=8)
    monkeypatch.setattr(
        broker,
        "get_order",
        lambda _oid: _order(status="expired", filled=1),
    )
    monkeypatch.setattr(
        broker,
        "get_open_position",
        lambda _symbol: SimpleNamespace(qty="7", side="long"),
    )
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 8.0,
                "drift": 0.0,
                "tiers": {"forever6": {"qty": 8.0}},
            }
        }
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)

    def apply(_symbol, _tier, filled, _net, _source):
        assert filled == 1
        ledger["positions"]["NVDA"]["alpaca_net_qty"] = 7.0
        ledger["positions"]["NVDA"]["tiers"]["forever6"]["qty"] = 7.0
        return True

    monkeypatch.setattr(ownership_guard, "apply_authorized_tier_fill_reduction", apply)
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync, "sync_once", lambda: {"ok": True, "healed": True}
    )

    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (True, 1)
    assert manager._load_trim_state()["NVDA"]["trim1_order"]["filled_qty"] == 1


def test_live_side_flip_blocks_ledger_mutation(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager)
    monkeypatch.setattr(
        broker, "get_order", lambda _oid: _order(status="filled", filled=2)
    )
    monkeypatch.setattr(
        broker,
        "get_open_position",
        lambda _symbol: SimpleNamespace(qty="6", side="short"),
    )
    monkeypatch.setattr(
        ownership_guard,
        "apply_authorized_tier_fill_reduction",
        lambda *_a: (_ for _ in ()).throw(AssertionError("wrong side must block")),
    )

    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (False, 0)
    assert manager._load_trim_state()["NVDA"]["trim1"] == "reserved"


def test_stopped_and_done_for_day_orders_remain_reserved(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    for status in ("stopped", "done_for_day"):
        _reserved(manager)
        monkeypatch.setattr(
            broker,
            "get_order",
            lambda _oid, status=status: _order(status=status, filled=0),
        )
        assert manager._reconcile_reserved_rung("NVDA", "trim1") == (False, 0)
        assert manager._load_trim_state()["NVDA"]["trim1"] == "reserved"


def test_terminal_zero_fill_reruns_blocked_startup_preflight(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manager.universe = ["NVDA"]
    _reserved(manager)
    monkeypatch.setattr(
        broker, "get_order", lambda _oid: _order(status="canceled", filled=0)
    )
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 8.0,
                "drift": 0.0,
                "tiers": {"forever6": {"qty": 8.0, "avg_cost": 10.0}},
            }
        }
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync, "sync_once", lambda: {"ok": True, "healed": True}
    )
    monkeypatch.setattr(f6.config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(f6, "_STARTUP_READY", False)
    degraded = {"value": True}
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: degraded["value"])
    preflights: list[bool] = []

    def preflight():
        preflights.append(True)
        degraded["value"] = False
        f6._STARTUP_READY = True
        return f6.F6StartupReconcileResult(ready=True)

    monkeypatch.setattr(manager, "reconcile_on_startup", preflight)
    monkeypatch.setattr(manager, "_fetch_prices", lambda: {})

    manager.evaluate_and_execute_trims()

    assert preflights == [True]
    assert manager._load_trim_state()["NVDA"]["trim1"] == "none"
    assert f6._STARTUP_READY is True


def test_wrong_ack_coid_is_not_bound_then_exact_coid_recovers(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)

    def wrong_ack(_symbol, qty, _label, _coid):
        return SimpleNamespace(
            id="wrong-order",
            client_order_id="F6-NVDA-s-2-other",
            symbol="NVDA",
            side="sell",
            status="accepted",
            qty=str(qty),
            filled_qty="0",
        )

    monkeypatch.setattr(manager, "_submit_trim", wrong_ack)
    assert manager._try_rung("NVDA", "trim1", 2, "10x", 10.0, 8.0) == (False, 0)
    row = manager._load_trim_state()["NVDA"]
    expected_coid = row["trim1_order"]["client_order_id"]
    assert row["trim1_order"]["order_id"] is None
    correct = SimpleNamespace(
        id="correct-order",
        client_order_id=expected_coid,
        symbol="NVDA",
        side="sell",
        status="canceled",
        qty="2",
        filled_qty="0",
    )
    monkeypatch.setattr(broker, "get_order_by_client_order_id", lambda _coid: correct)
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 8.0,
                "drift": 0.0,
                "tiers": {"forever6": {"qty": 8.0}},
            }
        }
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync, "sync_once", lambda: {"ok": True, "healed": True}
    )
    assert manager._reconcile_reserved_rung("NVDA", "trim1") == (False, 0)
    assert manager._load_trim_state()["NVDA"]["trim1"] == "none"


def test_two_callers_submit_exactly_one_trim(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    calls: list[str] = []
    call_lock = threading.Lock()

    def submit(_symbol, _qty, _label, coid):
        with call_lock:
            calls.append(coid)
        return SimpleNamespace(
            id="trim-race",
            client_order_id=coid,
            symbol="NVDA",
            side="sell",
            status="accepted",
            qty="2",
            filled_qty="0",
        )

    monkeypatch.setattr(manager, "_submit_trim", submit)
    monkeypatch.setattr(
        broker,
        "get_order",
        lambda _oid: SimpleNamespace(
            id="trim-race",
            client_order_id=calls[0],
            symbol="NVDA",
            side="sell",
            status="accepted",
            qty="2",
            filled_qty="0",
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _i: manager._try_rung("NVDA", "trim1", 2, "10x", 10.0, 8.0),
                range(2),
            )
        )

    assert outcomes == [(False, 0), (False, 0)]
    assert len(calls) == 1
    row = manager._load_trim_state()["NVDA"]
    assert row["trim1"] == "reserved"
    assert row["trim1_order"]["client_order_id"] == calls[0]


def test_broker_refuses_guard_bounded_trim_instead_of_changing_reserved_qty(
    monkeypatch,
):
    monkeypatch.setattr(f6, "f6_trim_reservation_matches", lambda *_a: True)
    monkeypatch.setattr(
        broker, "get_open_position", lambda _s: SimpleNamespace(qty="3", side="long")
    )
    monkeypatch.setattr(
        ownership_guard,
        "check_never_sell_floor",
        lambda *_a, **_k: ownership_guard.GuardResult("QTY_BOUND", 1.0, "own cap"),
    )
    monkeypatch.setattr(
        broker,
        "partial_close_position",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not submit")),
    )

    raw = broker.submit_f6_trim.__wrapped__
    assert raw("NVDA", 2, client_order_id="F6-NVDA-s-1-trim") is False


def test_broker_refuses_f6_trim_when_live_net_has_flipped_short(monkeypatch):
    monkeypatch.setattr(f6, "f6_trim_reservation_matches", lambda *_a: True)
    monkeypatch.setattr(
        broker, "get_open_position", lambda _s: SimpleNamespace(qty="3", side="short")
    )
    monkeypatch.setattr(
        ownership_guard,
        "check_never_sell_floor",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("wrong-side position must fail before the floor guard")
        ),
    )
    monkeypatch.setattr(
        broker,
        "partial_close_position",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not submit")),
    )

    raw = broker.submit_f6_trim.__wrapped__
    assert raw("NVDA", 1, client_order_id="F6-NVDA-s-1-trim") is False


def test_trim_reservation_match_rejects_wrong_symbol_side_and_tier(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    _reserved(manager)
    assert f6.f6_trim_reservation_matches("NVDA", "F6-NVDA-s-1-trim", 2)
    assert not f6.f6_trim_reservation_matches("AAPL", "F6-NVDA-s-1-trim", 2)
    assert not f6.f6_trim_reservation_matches("NVDA", "F6-NVDA-b-1-trim", 2)
    assert not f6.f6_trim_reservation_matches("NVDA", "QH-NVDA-s-1-trim", 2)


def test_second_rung_sizes_from_reconciled_post_fill_f6_quantity(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manager.universe = ["NVDA"]
    _reserved(manager, qty=2, baseline=8)
    terminal = _order(status="filled", filled=2)
    monkeypatch.setattr(broker, "get_order", lambda _oid: terminal)
    monkeypatch.setattr(
        broker,
        "get_open_position",
        lambda _symbol: SimpleNamespace(qty="6", side="long"),
    )
    ledger = {
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 8.0,
                "drift": 0.0,
                "tiers": {
                    "intraday": {"qty": 0.0},
                    "daytrade": {"qty": 0.0},
                    "qhm": {"qty": 0.0},
                    "forever6": {"qty": 8.0, "avg_cost": 10.0},
                },
            }
        }
    }
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)

    def apply(_symbol, _tier, _filled, _net, _source):
        ledger["positions"]["NVDA"]["alpaca_net_qty"] = 6.0
        ledger["positions"]["NVDA"]["tiers"]["forever6"]["qty"] = 6.0
        return True

    monkeypatch.setattr(ownership_guard, "apply_authorized_tier_fill_reduction", apply)
    import run_ledger_sync

    monkeypatch.setattr(
        run_ledger_sync, "sync_once", lambda: {"ok": True, "healed": True}
    )
    monkeypatch.setattr(manager, "_fetch_prices", lambda: {"NVDA": 250.0})
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(f6.config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(f6.config, "FOREVER6_TRIM_1_MULT", 10.0)
    monkeypatch.setattr(f6.config, "FOREVER6_TRIM_2_MULT", 20.0)
    monkeypatch.setattr(f6.config, "FOREVER6_TRIM_1_FRAC", 0.25)
    monkeypatch.setattr(f6.config, "FOREVER6_TRIM_2_FRAC", 0.25)
    submitted: list[int] = []

    def submit(_symbol, qty, _label, coid):
        submitted.append(qty)
        return SimpleNamespace(
            id="trim-2",
            client_order_id=coid,
            symbol="NVDA",
            side="sell",
            status="accepted",
            qty=str(qty),
            filled_qty="0",
        )

    monkeypatch.setattr(manager, "_submit_trim", submit)
    monkeypatch.setattr(f6, "_slack_safe", lambda _msg: None)

    result = manager.evaluate_and_execute_trims()

    assert submitted == [1]  # floor(25% of reconciled 6), never stale 8
    assert result["NVDA"]["fired"] == [("10x", 2)]
    assert manager._load_trim_state()["NVDA"]["trim2"] == "reserved"
