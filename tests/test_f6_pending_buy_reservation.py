# ruff: noqa: E501
from types import SimpleNamespace
import json
import threading
import time

import pytest

from execution import pending_protected_buys as ppb
from execution import broker
from execution import order_admission


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(ppb, "JOURNAL_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(ppb, "LOCK_PATH", tmp_path / ".pending.lock")
    monkeypatch.setattr(order_admission, "_REDUCTION_PATH", tmp_path / "reductions.json")
    monkeypatch.setattr(order_admission, "_REDUCTION_LOCK", tmp_path / ".reductions.lock")
    monkeypatch.setattr(order_admission, "_LOCK_DIR", tmp_path / "symbol-locks")


def order(*, status="filled", filled=1, coid="F6-NVDA-b-1-test", oid="order-1"):
    return SimpleNamespace(
        id=oid,
        client_order_id=coid,
        symbol="NVDA",
        side="buy",
        qty=1,
        filled_qty=filled,
        status=status,
    )


def clean_ledger(*, net, f6):
    return {
        "healed": True,
        "positions": {"NVDA": {"drift": 0, "alpaca_net_qty": net, "tiers": {
            "intraday": {"qty": 0}, "daytrade": {"qty": 0}, "qhm": {"qty": 0},
            "forever6": {"qty": f6},
        }}},
    }


def test_active_reservation_blocks_before_ledger_floor_exists():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    assert rid == "F6-NVDA-b-1-test"
    with pytest.raises(ppb.PendingProtectedBuyError, match="blocks reductions"):
        ppb.assert_reduction_clear("NVDA")


def test_exact_terminal_zero_releases():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order(status="canceled", filled=0)
    ppb.bind(rid, broker_order)
    # Real first-ever rejected-buy replay shape: no fills/position means the
    # canonical ledger omits the symbol rather than synthesizing a zero row.
    clean = {"healed": True, "positions": {}}
    result = ppb.reconcile([], [broker_order], {"order-1": rid}, clean)
    assert result == {"released": [rid], "pending": []}
    ppb.assert_reduction_clear("NVDA")


def test_terminal_zero_absent_symbol_requires_zero_baseline():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 2)
    broker_order = order(status="rejected", filled=0)
    ppb.bind(rid, broker_order)
    assert ppb.reconcile([], [broker_order], {"order-1": rid}, {"healed": True, "positions": {}}) == {
        "released": [], "pending": [rid],
    }


@pytest.mark.parametrize(
    "ledger_result",
    [
        {"healed": True},
        {"healed": True, "positions": []},
        {"healed": False, "positions": {}},
    ],
)
def test_terminal_zero_buy_requires_authoritative_symbol_absence(ledger_result):
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order(status="rejected", filled=0)
    ppb.bind(rid, broker_order)
    assert ppb.reconcile([], [broker_order], {"order-1": rid}, ledger_result) == {
        "released": [], "pending": [rid],
    }


@pytest.mark.parametrize("coid_map", [{}, {"order-1": "F6-WRONG"}])
def test_terminal_zero_buy_requires_exact_order_join(coid_map):
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order(status="rejected", filled=0)
    ppb.bind(rid, broker_order)
    assert ppb.reconcile(
        [], [broker_order], coid_map, {"healed": True, "positions": {}},
    ) == {"released": [], "pending": [rid]}


def test_terminal_zero_buy_rejects_duplicate_broker_order_identity():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order(status="rejected", filled=0)
    ppb.bind(rid, broker_order)
    conflicting = SimpleNamespace(
        id="order-1", client_order_id="F6-OTHER-b-1-conflict", symbol="NVDA",
        side="buy", qty=1, filled_qty=0, status="rejected",
    )
    assert ppb.reconcile(
        [], [broker_order, conflicting], {"order-1": rid},
        {"healed": True, "positions": {}},
    ) == {"released": [], "pending": [rid]}


def test_terminal_zero_with_exact_fill_stays_pending():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order(status="canceled", filled=0)
    ppb.bind(rid, broker_order)
    fill = {"id": "fill-1", "order_id": "order-1", "symbol": "NVDA", "side": "buy", "qty": "1"}
    clean = clean_ledger(net=1, f6=1)
    assert ppb.reconcile([fill], [broker_order], {"order-1": rid}, clean) == {
        "released": [], "pending": [rid],
    }


def test_filled_status_with_zero_fill_stays_pending():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order(status="filled", filled=0)
    ppb.bind(rid, broker_order)
    clean = clean_ledger(net=0, f6=0)
    assert ppb.reconcile([], [broker_order], {"order-1": rid}, clean) == {
        "released": [], "pending": [rid],
    }


def test_positive_fill_requires_exact_same_replay_provenance():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order()
    ppb.bind(rid, broker_order)
    fill = {
        "id": "fill-1", "order_id": "order-1", "symbol": "NVDA",
        "side": "buy", "qty": "1",
    }
    ledger = {"healed": True, "positions": {"NVDA": {"drift": 0.0, "alpaca_net_qty": 1, "tiers": {"intraday": {"qty": 0}, "daytrade": {"qty": 0}, "qhm": {"qty": 0}, "forever6": {"qty": 1}}}}}

    wrong_join = ppb.reconcile([fill], [broker_order], {"order-1": "F6-WRONG"}, ledger)
    assert wrong_join == {"released": [], "pending": [rid]}
    with pytest.raises(ppb.PendingProtectedBuyError):
        ppb.assert_reduction_clear("NVDA")

    result = ppb.reconcile([fill], [broker_order], {"order-1": rid}, ledger)
    assert result == {"released": [rid], "pending": []}
    row = ppb.load()["reservations"][rid]
    assert row["status"] == "released_accounted"
    assert row["fill_ids"] == ["fill-1"]
    assert row["accounted_qty"] == 1


def test_aggregate_claim_without_exact_fill_never_releases():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order()
    ppb.bind(rid, broker_order)
    ledger = {
        "healed": True,
        "positions": {"NVDA": {"drift": 0.0, "tiers": {"forever6": {"qty": 99}}}},
    }
    result = ppb.reconcile([], [broker_order], {"order-1": rid}, ledger)
    assert result == {"released": [], "pending": [rid]}


def test_exact_fill_without_f6_claim_increase_never_releases():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 2)
    broker_order = order()
    ppb.bind(rid, broker_order)
    fill = {
        "id": "fill-1", "order_id": "order-1", "symbol": "NVDA",
        "side": "buy", "qty": "1",
    }
    stale = {
        "healed": True,
        "positions": {
            "NVDA": {
                "drift": 0.0, "alpaca_net_qty": 2,
                "tiers": {
                    "intraday": {"qty": 0}, "daytrade": {"qty": 0},
                    "qhm": {"qty": 0}, "forever6": {"qty": 2},
                },
            }
        },
    }
    result = ppb.reconcile([fill], [broker_order], {"order-1": rid}, stale)
    assert result == {"released": [], "pending": [rid]}


def test_wrong_side_and_offsetting_negative_fill_never_release():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    broker_order = order()
    ppb.bind(rid, broker_order)
    malformed = [
        {"id": "a", "order_id": "order-1", "symbol": "NVDA", "side": "buy_to_cover", "qty": "2"},
        {"id": "b", "order_id": "order-1", "symbol": "NVDA", "side": "buy", "qty": "-1"},
    ]
    ledger = {
        "healed": True,
        "positions": {"NVDA": {"drift": 0, "alpaca_net_qty": 1, "tiers": {
            "intraday": {"qty": 0}, "daytrade": {"qty": 0}, "qhm": {"qty": 0},
            "forever6": {"qty": 1},
        }}},
    }
    result = ppb.reconcile(malformed, [broker_order], {"order-1": rid}, ledger)
    assert result == {"released": [], "pending": [rid]}


def test_corrupt_journal_fails_closed():
    ppb.JOURNAL_PATH.write_text('{"schema":1,"reservations":{"x":{}}}')
    with pytest.raises(ppb.PendingProtectedBuyError):
        ppb.assert_reduction_clear("NVDA")


@pytest.mark.parametrize("bad_coid", [None, True, 7, [], {}, "", "F6", "QH-NVDA-b-1-x"])
def test_malformed_client_id_fails_closed_without_ownership_guard(bad_coid):
    """Journal validation is type-safe and has no ownership-guard import edge."""
    ppb.JOURNAL_PATH.write_text(
        '{"schema":1,"reservations":{"x":{'
        '"status":"reserved","symbol":"NVDA","qty":1,'
        f'"client_order_id":{json.dumps(bad_coid)},'
        '"order_id":null,"created_at":"2026-10-14T00:00:00+00:00",'
        '"baseline_f6_qty":0}}}'
    )
    with pytest.raises(ppb.PendingProtectedBuyError, match="invalid client ID"):
        ppb.load()


def test_reserve_rejects_bare_f6_prefix():
    with pytest.raises(ppb.PendingProtectedBuyError, match="invalid F6 reservation"):
        ppb.reserve("NVDA", 1, "F6", 0)


def test_bind_rejects_wrong_identity_and_retains_reservation():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    with pytest.raises(ppb.PendingProtectedBuyError, match="exactly match"):
        ppb.bind(rid, order(coid="F6-NVDA-b-1-other"))
    assert ppb.load()["reservations"][rid]["status"] == "reserved"


def test_bind_is_idempotent_only_for_the_exact_submitted_order():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    accepted = order()
    ppb.bind(rid, accepted)
    ppb.bind(rid, accepted)
    assert ppb.load()["reservations"][rid]["order_id"] == "order-1"

    with pytest.raises(ppb.PendingProtectedBuyError, match="different broker order"):
        ppb.bind(rid, order(oid="order-2"))


def test_only_reserved_exact_identity_can_release_definitive_reject():
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    with pytest.raises(ppb.PendingProtectedBuyError, match="definitive broker rejection"):
        ppb.release_definitive_reject(rid, "F6-NVDA-b-1-wrong")
    assert ppb.load()["reservations"][rid]["status"] == "reserved"

    ppb.release_definitive_reject(rid, rid)
    assert ppb.load()["reservations"][rid]["status"] == "released_zero"
    ppb.assert_reduction_clear("NVDA")


def test_pending_reservation_blocks_direct_sell_before_broker(monkeypatch):
    ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    client = SimpleNamespace(
        submit_order=lambda _request: pytest.fail("broker was called")
    )
    monkeypatch.setattr(broker, "_get_trading_client", lambda: client)
    assert broker.submit_market_order("NVDA", 1, "sell", tier="intraday") is None


@pytest.mark.parametrize("keyword_side", [False, True])
def test_side_parameter_binding_admits_buys_and_blocks_sells(monkeypatch, keyword_side):
    """The decorator identifies side by name for positional and keyword calls."""
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    calls = []

    @order_admission.serialized_symbol_order(
        failure_value="blocked", side_parameter="side",
    )
    def submit(symbol, qty, side):
        calls.append((symbol, qty, side))
        return "submitted"

    ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    if keyword_side:
        assert submit("NVDA", 1, side="sell") == "blocked"
        assert submit("NVDA", 1, side="buy") == "submitted"
    else:
        assert submit("NVDA", 1, "sell") == "blocked"
        assert submit("NVDA", 1, "buy") == "submitted"
    assert calls == [("NVDA", 1, "buy")]


def test_pending_reservation_blocks_f6_trim_at_its_chokepoint(monkeypatch):
    """F6 trim must fail before position reads or its delegated sell path."""
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    monkeypatch.setattr(
        broker, "get_open_position", lambda _symbol: pytest.fail("position was read")
    )
    monkeypatch.setattr(
        broker, "partial_close_position", lambda *_a, **_k: pytest.fail("sell was delegated")
    )

    assert broker.submit_f6_trim("NVDA", 1) is False
    assert order_admission.active_reductions("NVDA") == ()


def test_pre_submit_blocked_sell_does_not_leak_reducer_admission(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-test", 0)
    client = SimpleNamespace(
        submit_order=lambda _request: pytest.fail("broker was called")
    )
    monkeypatch.setattr(broker, "_get_trading_client", lambda: client)

    assert broker.submit_market_order("NVDA", 1, "sell", tier="intraday") is None
    assert order_admission.active_reductions("NVDA") == ()

    canceled_buy = order(status="canceled", filled=0)
    ppb.bind(rid, canceled_buy)
    assert ppb.reconcile(
        [], [canceled_buy], {"order-1": rid}, {"healed": True, "positions": {}},
    ) == {"released": [rid], "pending": []}
    ppb.assert_reduction_clear("NVDA")
    assert order_admission.active_reductions("NVDA") == ()


def test_failed_after_broker_attempt_retains_ambiguous_reducer(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)

    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def ambiguous_submit(symbol):
        order_admission.note_reduction_attempted()
        return None

    assert ambiguous_submit("NVDA") is None
    rows = order_admission.active_reductions("NVDA")
    assert len(rows) == 1
    assert rows[0]["status"] == "admitted"


def test_definitive_wash_reject_does_not_leak_reducer_admission(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(
        broker,
        "_get_trading_client",
        lambda: SimpleNamespace(
            submit_order=lambda _request: (_ for _ in ()).throw(
                RuntimeError("potential wash trade detected")
            )
        ),
    )

    assert broker.submit_market_order("NVDA", 1, "sell", tier="intraday") is None
    assert order_admission.active_reductions("NVDA") == ()


def test_f6_market_submit_distinguishes_reject_from_ambiguous_duplicate(monkeypatch):
    errors = iter([
        RuntimeError("potential wash trade detected"),
        RuntimeError("40910000 duplicate client order"),
    ])
    monkeypatch.setattr(
        broker,
        "_get_trading_client",
        lambda: SimpleNamespace(
            submit_order=lambda _request: (_ for _ in ()).throw(next(errors))
        ),
    )

    assert broker.submit_market_order(
        "NVDA", 1, "buy", tier="forever6", client_order_id="F6-NVDA-b-1-r",
        distinguish_rejection=True,
    ) is broker.ORDER_DEFINITIVELY_REJECTED
    assert broker.submit_market_order(
        "NVDA", 1, "buy", tier="forever6", client_order_id="F6-NVDA-b-1-a",
        distinguish_rejection=True,
    ) is None


@pytest.mark.parametrize("outcome", [
    None,
    RuntimeError("unexpected EOF from proxy"),
    RuntimeError("upstream request failed; diagnostic=49999999"),
])
def test_f6_market_submit_unknown_outcome_remains_ambiguous(monkeypatch, outcome):
    def submit(_request):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(
        broker, "_get_trading_client", lambda: SimpleNamespace(submit_order=submit),
    )
    assert broker.submit_market_order(
        "NVDA", 1, "buy", tier="forever6", client_order_id="F6-NVDA-b-1-unknown",
        distinguish_rejection=True,
    ) is None


@pytest.mark.parametrize("envelope", [
    '{"code":42210000,"message":"order rejected"}',
    "{'code': 42210000, 'message': 'order rejected'}",
])
def test_f6_market_submit_accepts_only_parseable_alpaca_reject_envelope(monkeypatch, envelope):
    monkeypatch.setattr(
        broker, "_get_trading_client",
        lambda: SimpleNamespace(
            submit_order=lambda _request: (_ for _ in ()).throw(RuntimeError(envelope))
        ),
    )
    assert broker.submit_market_order(
        "NVDA", 1, "buy", tier="forever6", client_order_id="F6-NVDA-b-1-envelope",
        distinguish_rejection=True,
    ) is broker.ORDER_DEFINITIVELY_REJECTED


def test_existing_protection_sentinel_does_not_leak_reducer_admission(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(
        broker,
        "_get_trading_client",
        lambda: SimpleNamespace(
            submit_order=lambda _request: (_ for _ in ()).throw(
                RuntimeError("40310000 insufficient qty available held_for_orders")
            )
        ),
    )
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(broker, "_hold_state", lambda *_args: broker._HOLD_STABLE)

    result = broker.submit_day_stop_order(
        "NVDA", 1, "sell", 90.0, tier="intraday", allow_cancel_blocking=False,
    )
    assert result is broker.PROTECTION_ALREADY_HELD
    assert order_admission.active_reductions("NVDA") == ()


def test_f6_starter_exception_recovers_and_binds_exact_order(monkeypatch):
    import config
    from execution import forever_hold_manager as f6
    from execution import ownership_guard
    from execution import tier_capital_allocator as allocator

    recovered = order(status="accepted", filled=0)
    lease = SimpleNamespace(id="lease-1", client_order_id=recovered.client_order_id)
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(config, "FOREVER6_STARTER_CASH_FLOOR", 0.0)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(broker, "_make_idem_id", lambda *_args: recovered.client_order_id)
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        broker, "submit_market_order",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("lost response")),
    )
    monkeypatch.setattr(broker, "get_order_by_client_order_id", lambda _coid: recovered)
    monkeypatch.setattr(
        allocator, "live_admit",
        lambda *_args, **_kwargs: SimpleNamespace(approved=True, reason="ok", lease=lease),
    )
    monkeypatch.setattr(allocator, "live_release", lambda _lease, _reason: recovered)
    bound = []
    monkeypatch.setattr(allocator, "live_bind", lambda _lease, value: bound.append(value.id) or True)
    monkeypatch.setattr(
        ownership_guard, "load_ledger",
        lambda: clean_ledger(net=0, f6=0),
    )
    monkeypatch.setattr(ownership_guard, "tier_qty", lambda *_args: 0.0)

    manager = f6.ForeverHoldManager(SimpleNamespace())
    monkeypatch.setattr(manager, "_record_event", lambda *_args: None)
    monkeypatch.setattr(manager, "_verify_ledger_reflects", lambda *_args: None)
    result = manager.execute_starter(
        [{"symbol": "NVDA", "price": 100.0}], 200.0, settled_cash=1_000.0,
    )

    assert result["placed"][0]["order_id"] == "order-1"
    assert bound == ["order-1"]
    assert ppb.load()["reservations"][recovered.client_order_id]["status"] == "submitted"


def test_f6_definitive_buy_reject_releases_both_intents_and_stops_batch(monkeypatch):
    import config
    from execution import forever_hold_manager as f6
    from execution import ownership_guard
    from execution import tier_capital_allocator as allocator

    coid = "F6-NVDA-b-1-reject"
    lease = SimpleNamespace(id="lease-1", client_order_id=coid)
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(config, "FOREVER6_STARTER_CASH_FLOOR", 0.0)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(broker, "_make_idem_id", lambda *_args: coid)
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        broker, "submit_market_order",
        lambda *_args, **_kwargs: broker.ORDER_DEFINITIVELY_REJECTED,
    )
    monkeypatch.setattr(
        allocator, "live_admit",
        lambda *_args, **_kwargs: SimpleNamespace(approved=True, reason="ok", lease=lease),
    )
    released = []
    monkeypatch.setattr(
        allocator, "live_release_proven_no_order",
        lambda value, reason: released.append((value.id, reason)) or True,
    )
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: clean_ledger(net=0, f6=0))
    monkeypatch.setattr(ownership_guard, "tier_qty", lambda *_args: 0.0)

    manager = f6.ForeverHoldManager(SimpleNamespace())
    result = manager.execute_starter(
        [{"symbol": "NVDA", "price": 100.0}, {"symbol": "AAPL", "price": 100.0}],
        300.0,
        settled_cash=1_000.0,
    )

    assert result["placed"] == []
    assert [row["skip"] for row in result["skipped"]] == [
        "broker definitively rejected order", "stopped after broker rejection",
    ]
    assert released == [("lease-1", "broker_definitive_reject")]
    assert ppb.load()["reservations"][coid]["status"] == "released_zero"
    ppb.assert_reduction_clear("NVDA")


def test_allocator_release_failure_keeps_f6_rejection_reservation_active(monkeypatch):
    import config
    from execution import forever_hold_manager as f6
    from execution import ownership_guard
    from execution import tier_capital_allocator as allocator

    coid = "F6-NVDA-b-1-release-fail"
    lease = SimpleNamespace(id="lease-1", client_order_id=coid)
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(config, "FOREVER6_STARTER_CASH_FLOOR", 0.0)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(broker, "_make_idem_id", lambda *_args: coid)
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        broker, "submit_market_order",
        lambda *_args, **_kwargs: broker.ORDER_DEFINITIVELY_REJECTED,
    )
    monkeypatch.setattr(broker, "get_order_by_client_order_id", lambda _coid: None)
    monkeypatch.setattr(
        allocator, "live_admit",
        lambda *_args, **_kwargs: SimpleNamespace(approved=True, reason="ok", lease=lease),
    )
    monkeypatch.setattr(allocator, "live_release_proven_no_order", lambda *_args: False)
    monkeypatch.setattr(allocator, "live_release", lambda *_args: None)
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: clean_ledger(net=0, f6=0))
    monkeypatch.setattr(ownership_guard, "tier_qty", lambda *_args: 0.0)

    manager = f6.ForeverHoldManager(SimpleNamespace())
    result = manager.execute_starter(
        [{"symbol": "NVDA", "price": 100.0}], 200.0, settled_cash=1_000.0,
    )

    assert result["placed"] == []
    assert ppb.load()["reservations"][coid]["status"] == "reserved"
    with pytest.raises(ppb.PendingProtectedBuyError):
        ppb.assert_reduction_clear("NVDA")


def test_f6_ambiguous_timeout_keeps_protected_intent_active(monkeypatch):
    import config
    from execution import forever_hold_manager as f6
    from execution import ownership_guard
    from execution import tier_capital_allocator as allocator

    coid = "F6-NVDA-b-1-timeout"
    lease = SimpleNamespace(id="lease-1", client_order_id=coid)
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(config, "FOREVER6_STARTER_CASH_FLOOR", 0.0)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(broker, "_make_idem_id", lambda *_args: coid)
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(broker, "submit_market_order", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(broker, "get_order_by_client_order_id", lambda _coid: None)
    monkeypatch.setattr(
        allocator, "live_admit",
        lambda *_args, **_kwargs: SimpleNamespace(approved=True, reason="ok", lease=lease),
    )
    monkeypatch.setattr(allocator, "live_release", lambda *_args: None)
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: clean_ledger(net=0, f6=0))
    monkeypatch.setattr(ownership_guard, "tier_qty", lambda *_args: 0.0)

    manager = f6.ForeverHoldManager(SimpleNamespace())
    result = manager.execute_starter(
        [{"symbol": "NVDA", "price": 100.0}], 200.0, settled_cash=1_000.0,
    )

    assert result["placed"] == []
    assert ppb.load()["reservations"][coid]["status"] == "reserved"
    with pytest.raises(ppb.PendingProtectedBuyError):
        ppb.assert_reduction_clear("NVDA")


def test_f6_starter_recovers_after_pending_bind_then_allocator_bind_raises(monkeypatch):
    import config
    from execution import forever_hold_manager as f6
    from execution import ownership_guard
    from execution import tier_capital_allocator as allocator

    recovered = order(status="accepted", filled=0)
    lease = SimpleNamespace(id="lease-1", client_order_id=recovered.client_order_id)
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(config, "FOREVER6_STARTER_CASH_FLOOR", 0.0)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    monkeypatch.setattr(broker, "_make_idem_id", lambda *_args: recovered.client_order_id)
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(broker, "submit_market_order", lambda *_args, **_kwargs: recovered)
    monkeypatch.setattr(broker, "get_order_by_client_order_id", lambda _coid: recovered)
    monkeypatch.setattr(
        allocator, "live_admit",
        lambda *_args, **_kwargs: SimpleNamespace(approved=True, reason="ok", lease=lease),
    )
    monkeypatch.setattr(allocator, "live_release", lambda _lease, _reason: recovered)
    bind_calls = []

    def bind_then_raise(_lease, value):
        bind_calls.append(value.id)
        if len(bind_calls) == 1:
            raise RuntimeError("allocator journal fsync failed after F6 bind")
        return True

    monkeypatch.setattr(allocator, "live_bind", bind_then_raise)
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: clean_ledger(net=0, f6=0))
    monkeypatch.setattr(ownership_guard, "tier_qty", lambda *_args: 0.0)

    manager = f6.ForeverHoldManager(SimpleNamespace())
    monkeypatch.setattr(manager, "_record_event", lambda *_args: None)
    monkeypatch.setattr(manager, "_verify_ledger_reflects", lambda *_args: None)
    result = manager.execute_starter(
        [{"symbol": "NVDA", "price": 100.0}], 200.0, settled_cash=1_000.0,
    )

    assert result["placed"][0]["order_id"] == "order-1"
    assert bind_calls == ["order-1", "order-1"]
    assert ppb.load()["reservations"][recovered.client_order_id]["status"] == "submitted"


def test_symbol_gate_serializes_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(order_admission, "_LOCK_DIR", tmp_path / "locks")
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first():
        with order_admission.symbol_order_gate("NVDA"):
            first_entered.set()
            release_first.wait(2)

    def second():
        first_entered.wait(2)
        with order_admission.symbol_order_gate("NVDA"):
            second_entered.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    assert first_entered.wait(1)
    time.sleep(0.05)
    assert not second_entered.is_set()
    release_first.set()
    t1.join(2)
    t2.join(2)
    assert second_entered.is_set()


def test_pre_submit_exception_releases_reducer_admission(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)

    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def fail_before_submit(_symbol):
        raise RuntimeError("request construction failed")

    with pytest.raises(RuntimeError, match="construction"):
        fail_before_submit("NVDA")
    assert order_admission.active_reductions("NVDA") == ()


def test_false_f6_flag_bypasses_symbol_lock_and_reducer_journal(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", False)
    monkeypatch.setattr(
        order_admission, "symbol_order_gate",
        lambda _symbol: pytest.fail("dark F6 path touched filesystem lock"),
    )

    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def legacy(_symbol):
        return "legacy"

    assert legacy("NVDA") == "legacy"
    assert not order_admission._REDUCTION_PATH.exists()


def test_false_f6_flag_sell_creates_no_pending_journal_or_lock(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", False)
    accepted = SimpleNamespace(id="sell-1")
    calls = []
    monkeypatch.setattr(
        broker, "_get_trading_client",
        lambda: SimpleNamespace(
            submit_order=lambda request: calls.append(request) or accepted
        ),
    )

    assert broker.submit_market_order("NVDA", 1, "sell", tier="intraday") is accepted
    assert len(calls) == 1
    assert not ppb.JOURNAL_PATH.exists()
    assert not ppb.LOCK_PATH.exists()


def test_false_f6_flag_existing_active_journal_still_blocks_sell(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", False)
    ppb.reserve("NVDA", 1, "F6-NVDA-b-1-existing", 0)
    calls = []
    monkeypatch.setattr(
        broker, "_get_trading_client",
        lambda: SimpleNamespace(
            submit_order=lambda request: calls.append(request) or SimpleNamespace(id="sell-1")
        ),
    )

    assert broker.submit_market_order("NVDA", 1, "sell", tier="intraday") is None
    assert calls == []
    assert ppb.load()["reservations"]["F6-NVDA-b-1-existing"]["status"] == "reserved"


def test_boolean_schema_versions_fail_closed():
    ppb.JOURNAL_PATH.write_text('{"schema":true,"reservations":{}}')
    with pytest.raises(ppb.PendingProtectedBuyError, match="schema"):
        ppb.load()

    order_admission._REDUCTION_PATH.write_text('{"schema":true,"rows":{}}')
    with pytest.raises(ValueError, match="journal"):
        order_admission.active_reductions("NVDA")


def test_acknowledged_but_not_list_visible_reducer_blocks_f6(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def submit_reducer(symbol):
        return SimpleNamespace(
            id="sell-1", client_order_id="IN-NVDA-s-1-x",
            symbol=symbol, status="accepted", side="sell", qty=1,
        )

    assert submit_reducer("NVDA").id == "sell-1"
    # Simulates a lagging get_open_orders() result: exact intent/order lookup is
    # still open, so F6 admission remains blocked independently of list visibility.
    active = order_admission.active_reductions("NVDA")
    assert len(active) == 1

    terminal = SimpleNamespace(
        id="sell-1", symbol="NVDA", status="filled", side="sell", qty=1,
        filled_qty=1, client_order_id="IN-NVDA-s-1-x",
    )
    fill = {"id": "sell-fill", "order_id": "sell-1", "symbol": "NVDA", "side": "sell", "qty": 1}
    cleared = order_admission.reconcile_reductions(
        [fill], [terminal], {"sell-1": "IN-NVDA-s-1-x"}, clean_ledger(net=0, f6=0),
    )
    assert cleared["pending"] == []
    assert order_admission.active_reductions("NVDA") == ()


def test_terminal_zero_reducer_releases_when_clean_replay_omits_symbol(monkeypatch):
    """A manual flatten must not leave a canceled reducer blocking F6 forever."""
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)

    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def submit_reducer(symbol):
        return SimpleNamespace(
            id="sell-1", client_order_id="IN-NVDA-s-1-x", symbol=symbol,
            status="accepted", side="sell", qty=1,
        )

    assert submit_reducer("NVDA").id == "sell-1"
    terminal = SimpleNamespace(
        id="sell-1", client_order_id="IN-NVDA-s-1-x", symbol="NVDA",
        status="canceled", side="sell", qty=1, filled_qty=0,
    )
    result = order_admission.reconcile_reductions(
        [], [terminal], {"sell-1": "IN-NVDA-s-1-x"},
        {"healed": True, "positions": {}},
    )

    assert result["pending"] == []
    assert len(result["released"]) == 1
    assert order_admission.active_reductions("NVDA") == ()


@pytest.mark.parametrize(
    "ledger_result",
    [
        {"healed": True},
        {"healed": True, "positions": []},
        {"healed": False, "positions": {}},
    ],
)
def test_terminal_zero_reducer_requires_authoritative_symbol_absence(
    monkeypatch, ledger_result,
):
    """Missing, malformed, or unhealed replay evidence remains fail-closed."""
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)

    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def submit_reducer(symbol):
        return SimpleNamespace(
            id="sell-1", client_order_id="IN-NVDA-s-1-x", symbol=symbol,
            status="accepted", side="sell", qty=1,
        )

    submit_reducer("NVDA")
    terminal = SimpleNamespace(
        id="sell-1", client_order_id="IN-NVDA-s-1-x", symbol="NVDA",
        status="canceled", side="sell", qty=1, filled_qty=0,
    )
    result = order_admission.reconcile_reductions(
        [], [terminal], {"sell-1": "IN-NVDA-s-1-x"}, ledger_result,
    )

    assert result["released"] == []
    assert len(result["pending"]) == 1
    assert len(order_admission.active_reductions("NVDA")) == 1


def test_malformed_boolean_fill_identity_cannot_release_either_journal(monkeypatch):
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)

    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def submit_reducer(symbol):
        return SimpleNamespace(
            id="sell-1", client_order_id="IN-NVDA-s-1-x", symbol=symbol,
            status="accepted", side="sell", qty=1,
        )

    submit_reducer("NVDA")
    terminal_sell = SimpleNamespace(
        id="sell-1", client_order_id="IN-NVDA-s-1-x", symbol="NVDA",
        status="filled", side="sell", qty=1, filled_qty=1,
    )
    malformed_sell = {
        "id": True, "order_id": "sell-1", "symbol": "NVDA", "side": "sell", "qty": 1,
    }
    result = order_admission.reconcile_reductions(
        [malformed_sell], [terminal_sell], {"sell-1": "IN-NVDA-s-1-x"},
        clean_ledger(net=0, f6=0),
    )
    assert len(result["pending"]) == 1

    rid = ppb.reserve("AAPL", 1, "F6-AAPL-b-1-x", 0)
    buy = SimpleNamespace(
        id="buy-1", client_order_id=rid, symbol="AAPL", side="buy", qty=1,
        filled_qty=1, status="filled",
    )
    ppb.bind(rid, buy)
    malformed_buy = {
        "id": True, "order_id": "buy-1", "symbol": "AAPL", "side": "buy", "qty": 1,
    }
    ledger = {"healed": True, "positions": {"AAPL": {
        "drift": 0, "alpaca_net_qty": 1, "tiers": {
            "intraday": {"qty": 0}, "daytrade": {"qty": 0},
            "qhm": {"qty": 0}, "forever6": {"qty": 1},
        },
    }}}
    assert ppb.reconcile(
        [malformed_buy], [buy], {"buy-1": rid}, ledger,
    ) == {"released": [], "pending": [rid]}


@pytest.mark.parametrize("bad", [True, False])
@pytest.mark.parametrize("field", ["qty", "filled_qty"])
def test_boolean_order_quantities_cannot_bind_or_release_journals(bad, field):
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-bool-order", 0)
    malformed_buy = order(coid=rid, oid="buy-1")
    setattr(malformed_buy, field, bad)
    with pytest.raises(ppb.PendingProtectedBuyError, match="does not exactly match"):
        ppb.bind(rid, malformed_buy)
    assert ppb.load()["reservations"][rid]["status"] == "reserved"

    reduction_id = order_admission._begin_reduction("AAPL", "test")
    malformed_sell = SimpleNamespace(
        id="sell-1", client_order_id="IN-AAPL-s-1-bool-order", symbol="AAPL",
        status="accepted", side="sell", qty=1, filled_qty=0,
    )
    setattr(malformed_sell, field, bad)
    # Binding only consumes the submitted quantity; filled_qty is proven later.
    if field == "qty":
        with pytest.raises(ValueError, match="identity is incomplete"):
            order_admission._bind_reduction(reduction_id, malformed_sell)
        assert len(order_admission.active_reductions("AAPL")) == 1


@pytest.mark.parametrize("bad", [True, False])
@pytest.mark.parametrize("evidence", ["order_qty", "order_filled", "fill_qty", "ledger_drift"])
def test_boolean_reconciliation_evidence_keeps_both_journals_pending(bad, evidence):
    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-bool-proof", 0)
    buy = order(coid=rid, oid="buy-1")
    ppb.bind(rid, buy)
    terminal_buy = order(coid=rid, oid="buy-1")
    buy_fill = {
        "id": "buy-fill", "order_id": "buy-1", "symbol": "NVDA",
        "side": "buy", "qty": 1,
    }
    buy_ledger = clean_ledger(net=1, f6=1)

    reduction_id = order_admission._begin_reduction("AAPL", "test")
    accepted_sell = SimpleNamespace(
        id="sell-1", client_order_id="IN-AAPL-s-1-bool-proof", symbol="AAPL",
        status="accepted", side="sell", qty=1, filled_qty=0,
    )
    order_admission._bind_reduction(reduction_id, accepted_sell)
    terminal_sell = SimpleNamespace(
        id="sell-1", client_order_id="IN-AAPL-s-1-bool-proof", symbol="AAPL",
        status="filled", side="sell", qty=1, filled_qty=1,
    )
    sell_fill = {
        "id": "sell-fill", "order_id": "sell-1", "symbol": "AAPL",
        "side": "sell", "qty": 1,
    }
    sell_ledger = {
        "healed": True,
        "positions": {"AAPL": {"drift": 0, "alpaca_net_qty": 0, "tiers": {
            "intraday": {"qty": 0}, "daytrade": {"qty": 0},
            "qhm": {"qty": 0}, "forever6": {"qty": 0},
        }}},
    }

    if evidence == "order_qty":
        terminal_buy.qty = bad
        terminal_sell.qty = bad
    elif evidence == "order_filled":
        terminal_buy.filled_qty = bad
        terminal_sell.filled_qty = bad
    elif evidence == "fill_qty":
        buy_fill["qty"] = bad
        sell_fill["qty"] = bad
    else:
        buy_ledger["positions"]["NVDA"]["drift"] = bad
        sell_ledger["positions"]["AAPL"]["drift"] = bad

    assert ppb.reconcile(
        [buy_fill], [terminal_buy], {"buy-1": rid}, buy_ledger,
    ) == {"released": [], "pending": [rid]}
    reducer_result = order_admission.reconcile_reductions(
        [sell_fill], [terminal_sell], {"sell-1": "IN-AAPL-s-1-bool-proof"},
        sell_ledger,
    )
    assert reducer_result["released"] == []
    assert reducer_result["pending"] == [reduction_id]


@pytest.mark.parametrize(
    ("sell_qty", "sell_filled", "sell_status", "before", "after"),
    [(1, 1, "filled", 1, 0), (2, 1, "canceled", 2, 1)],
)
def test_f6_trim_replay_precedes_new_buy_baseline(
    monkeypatch, sell_qty, sell_filled, sell_status, before, after,
):
    """Full and canceled-partial F6 trims must replay before a later F6 buy."""
    import config
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)

    @order_admission.serialized_symbol_order(failure_value=None, reducing=True)
    def submit_trim(symbol):
        return SimpleNamespace(
            id="trim-1", client_order_id="F6-NVDA-s-1-trim", symbol=symbol,
            status="accepted", side="sell", qty=sell_qty, filled_qty=0,
        )

    assert submit_trim("NVDA").id == "trim-1"
    assert len(order_admission.active_reductions("NVDA")) == 1
    terminal = SimpleNamespace(
        id="trim-1", client_order_id="F6-NVDA-s-1-trim", symbol="NVDA",
        status=sell_status, side="sell", qty=sell_qty, filled_qty=sell_filled,
    )
    sell_fill = {
        "id": "trim-fill", "order_id": "trim-1", "symbol": "NVDA",
        "side": "sell", "qty": sell_filled,
    }
    replay = order_admission.reconcile_reductions(
        [sell_fill], [terminal], {"trim-1": "F6-NVDA-s-1-trim"},
        clean_ledger(net=after, f6=after),
    )
    assert replay["pending"] == []
    assert order_admission.active_reductions("NVDA") == ()

    rid = ppb.reserve("NVDA", 1, "F6-NVDA-b-1-after-trim", after)
    buy = order(coid=rid, oid="buy-1")
    ppb.bind(rid, buy)
    buy_fill = {"id": "buy-fill", "order_id": "buy-1", "symbol": "NVDA", "side": "buy", "qty": 1}
    result = ppb.reconcile(
        [sell_fill, buy_fill], [terminal, buy],
        {"trim-1": "F6-NVDA-s-1-trim", "buy-1": rid},
        clean_ledger(net=after + 1, f6=after + 1),
    )
    assert result == {"released": [rid], "pending": []}
