from datetime import datetime, timedelta
from types import SimpleNamespace
import threading

import pytest

import config
from execution import broker
from execution import forever_hold_manager as f6
from execution import order_admission
from execution import ownership_guard
from execution import pending_protected_buys as ppb
from execution import tier_capital_allocator as allocator


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ppb, "JOURNAL_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(ppb, "LOCK_PATH", tmp_path / ".pending.lock")
    monkeypatch.setattr(
        order_admission, "_REDUCTION_PATH", tmp_path / "reductions.json"
    )
    monkeypatch.setattr(
        order_admission, "_REDUCTION_LOCK", tmp_path / ".reductions.lock"
    )
    monkeypatch.setattr(order_admission, "_LOCK_DIR", tmp_path / "symbol-locks")
    monkeypatch.setattr(f6, "_STATE", tmp_path / "forever6_holds.json")
    monkeypatch.setattr(f6, "_STATE_LOCK", tmp_path / ".forever6_holds.lock")
    monkeypatch.setattr(f6, "_SYNC_DEGRADED", tmp_path / "sync.json")
    monkeypatch.setattr(config, "FOREVER6_ENABLED", True)
    monkeypatch.setattr(config, "FOREVER6_STARTER_CASH_FLOOR", 0.0)
    monkeypatch.setattr(f6, "_STARTUP_READY", True)
    monkeypatch.setattr(f6, "_read_sync_degraded", lambda: False)
    f6._F6_QUOTE_OBSERVATIONS.clear()


def _manager(*, now, quote):
    asset = {
        "symbol": "NVDA", "status": "active", "tradable": True,
        "asset_class": "us_equity", "name": "NVIDIA Corporation Common Stock",
    }
    api = SimpleNamespace(
        get_asset=lambda _symbol: asset,
        get_account=lambda: SimpleNamespace(cash=1_000.0),
        get_trading_client=lambda: SimpleNamespace(get_asset=lambda _symbol: asset),
    )
    quote_loader = quote if callable(quote) else (lambda _symbol: quote)
    manager = f6.ForeverHoldManager(
        api, clock=lambda: now, quote_loader=quote_loader,
    )
    manager._trigger_evidence_is_current = lambda _evidence: True
    return manager


def _fresh_quotes(now, *, bid=100.0, ask=100.01):
    f6._F6_QUOTE_OBSERVATIONS["NVDA"] = now - timedelta(microseconds=2)
    rows = iter([
        {"bid": bid, "ask": ask, "ts": (now - timedelta(microseconds=1)).isoformat()},
        {"bid": bid, "ask": ask, "ts": (now - timedelta(microseconds=1)).isoformat()},
    ])
    return lambda _symbol: next(rows)


def _exact_order(*, status="accepted", filled=0.0):
    return SimpleNamespace(
        id="order-1", client_order_id="F6-NVDA-b-1-bounded", symbol="NVDA",
        side="buy", qty=1, filled_qty=filled, status=status, type="limit",
        time_in_force="day", limit_price="100.01", extended_hours=True,
    )


def _wire_clean_boundary(monkeypatch, order):
    lease = SimpleNamespace(id="lease-1", client_order_id=order.client_order_id)
    monkeypatch.setattr(broker, "_make_idem_id", lambda *_args: order.client_order_id)
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(broker, "submit_limit_order", lambda *_args, **_kwargs: order)
    monkeypatch.setattr(
        allocator, "live_admit",
        lambda *_args, **_kwargs: SimpleNamespace(
            approved=True, reason="ok", lease=lease
        ),
    )
    monkeypatch.setattr(allocator, "live_bind", lambda *_args: True)
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: {
        "positions": {"NVDA": {"drift": 0, "alpaca_net_qty": 0, "tiers": {
            "intraday": {"qty": 0}, "daytrade": {"qty": 0},
            "qhm": {"qty": 0}, "forever6": {"qty": 0},
        }}},
    })
    monkeypatch.setattr(ownership_guard, "tier_qty", lambda *_args: 0.0)


def test_buy_limit_ceils_to_exact_cent():
    assert broker.canonical_buy_limit_price(100.001) == 100.01
    assert broker.canonical_buy_limit_price(100.01) == 100.01
    for value in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            broker.canonical_buy_limit_price(value)


def test_shared_limit_broker_preserves_non_f6_rounding(monkeypatch):
    requests = []
    client = SimpleNamespace(
        submit_order=lambda request: (
            requests.append(request) or SimpleNamespace(id="ok")
        )
    )
    monkeypatch.setattr(broker, "_get_trading_client", lambda: client)

    broker.submit_limit_order("NVDA", 1, "buy", 100.001, tier="swing")
    broker.submit_limit_order("NVDA", 1, "buy", 100.001, tier="forever6")

    assert float(requests[0].limit_price) == 100.0
    assert float(requests[1].limit_price) == 100.01


def test_allocator_exact_reserve_requires_full_f6_limit_contract():
    base = dict(
        owner_tier="forever6", symbol="NVDA", side="buy", qty=1,
        price_bound=100.01, stop_price=1.0, exact_cash_reserve=True,
        overnight=True,
    )
    wrong_tier = allocator.EntryRequest(tier="swing", **base)
    assert "F6-only" in allocator.TierCapitalAllocator._validate_request(wrong_tier)

    missing_contract = allocator.EntryRequest(tier="forever6", **base)
    assert "bounded" in allocator.TierCapitalAllocator._validate_request(
        missing_contract
    )

    exact = allocator.EntryRequest(
        tier="forever6", order_type="limit", time_in_force="day",
        extended_hours=True, **base,
    )
    assert allocator.TierCapitalAllocator._validate_request(exact) == ""

    missing_overnight = allocator.EntryRequest(
        tier="forever6", order_type="limit", time_in_force="day",
        extended_hours=True, **{**base, "overnight": False},
    )
    assert "overnight" in allocator.TierCapitalAllocator._validate_request(
        missing_overnight
    )


def test_f6_money_boundary_declares_overnight_allocator_exposure():
    source = f6.Path(f6.__file__).read_text()
    call = source[source.index("_capital = live_admit("):]
    call = call[:call.index(")\n", call.index("client_order_id=_coid"))]
    assert "overnight=True" in call


def test_starter_refuses_outside_extended_session(monkeypatch):
    now = datetime(2026, 10, 16, 20, 0, tzinfo=f6.ET)
    manager = _manager(now=now, quote=None)
    result = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)
    assert result["reason"] == "outside F6 entry session"


def test_crossed_quote_blocks_before_order_or_journal(monkeypatch):
    now = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    manager = _manager(
        now=now,
        quote={"bid": 101.0, "ask": 100.0, "ts": now.isoformat()},
    )
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    called = []
    monkeypatch.setattr(
        broker, "submit_limit_order", lambda *_args, **_kwargs: called.append(True)
    )
    result = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)
    assert result["placed"] == []
    assert called == []
    assert not ppb.JOURNAL_PATH.exists()


def test_same_session_quote_without_newer_update_is_stale(monkeypatch):
    now = datetime(2026, 10, 16, 19, 59, tzinfo=f6.ET)
    stale = {"bid": 100.0, "ask": 100.01,
             "ts": datetime(2026, 10, 16, 16, 5, tzinfo=f6.ET).isoformat()}
    manager = _manager(now=now, quote=stale)
    monkeypatch.setattr(broker, "get_open_orders", lambda *_args, **_kwargs: [])
    called = []
    monkeypatch.setattr(
        broker, "submit_limit_order", lambda *_args, **_kwargs: called.append(True)
    )

    result = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)

    assert result["placed"] == []
    assert called == []
    assert "freshness" in result["skipped"][0]["skip"]


def test_quote_must_advance_across_scan_cycles(monkeypatch):
    now = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    first = {"bid": 100.0, "ask": 100.01,
             "ts": (now - timedelta(seconds=2)).isoformat()}
    second = {"bid": 100.0, "ask": 100.01,
              "ts": (now - timedelta(seconds=1)).isoformat()}
    quotes = iter([first, second, second])
    manager = _manager(now=now, quote=lambda _symbol: next(quotes))
    exact = _exact_order()
    _wire_clean_boundary(monkeypatch, exact)
    manager._verify_ledger_reflects = lambda *_args: None

    armed = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)
    submitted = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)

    assert armed["placed"] == []
    assert "across cycles" in armed["reason"]
    assert submitted["placed"][0]["symbol"] == "NVDA"


def test_yesterday_observation_cannot_arm_today_stale_quote():
    now = datetime(2026, 10, 16, 19, 59, tzinfo=f6.ET)
    f6._F6_QUOTE_OBSERVATIONS["NVDA"] = now - timedelta(days=1)
    stale_today = {
        "bid": 100.0, "ask": 100.01,
        "ts": datetime(2026, 10, 16, 16, 5, tzinfo=f6.ET).isoformat(),
    }
    advanced_today = {
        "bid": 100.0, "ask": 100.01,
        "ts": datetime(2026, 10, 16, 16, 6, tzinfo=f6.ET).isoformat(),
    }

    assert f6._observe_advancing_quote("NVDA", stale_today, now) is None
    assert f6._observe_advancing_quote("NVDA", advanced_today, now) is not None


def test_regressed_timestamp_never_lowers_quote_watermark():
    now = datetime(2026, 10, 16, 19, 59, tzinfo=f6.ET)

    def quote(at):
        return {"bid": 100.0, "ask": 100.01, "ts": at.isoformat()}

    at_1700 = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    at_1630 = datetime(2026, 10, 16, 16, 30, tzinfo=f6.ET)
    at_1631 = datetime(2026, 10, 16, 16, 31, tzinfo=f6.ET)
    at_1701 = datetime(2026, 10, 16, 17, 1, tzinfo=f6.ET)

    assert f6._observe_advancing_quote("NVDA", quote(at_1700), now) is None
    assert f6._observe_advancing_quote("NVDA", quote(at_1630), now) is None
    assert f6._observe_advancing_quote("NVDA", quote(at_1631), now) is None
    assert f6._observe_advancing_quote("NVDA", quote(at_1701), now) is not None


def test_blocked_basket_does_not_consume_other_names_advancement():
    base = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    later = base + timedelta(minutes=1)
    f6._F6_QUOTE_OBSERVATIONS.update({"NVDA": base, "META": base})
    valid = f6.F6Quote(100.0, 100.01, later)
    unchanged = f6.F6Quote(100.0, 100.01, base)

    confirmed, blocked = f6._observe_advancing_basket({
        "NVDA": valid, "META": unchanged,
    })
    assert confirmed == {}
    assert blocked == {"META"}
    assert f6._F6_QUOTE_OBSERVATIONS["NVDA"] == base

    confirmed, blocked = f6._observe_advancing_basket({
        "NVDA": valid, "META": valid,
    })
    assert blocked == set()
    assert set(confirmed) == {"NVDA", "META"}


def test_second_symbol_pre_intent_error_still_verifies_first_order(monkeypatch):
    now = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    f6._F6_QUOTE_OBSERVATIONS.update({
        "NVDA": now - timedelta(seconds=2),
        "META": now - timedelta(seconds=2),
    })
    calls = {"NVDA": 0, "META": 0}

    def quotes(symbol):
        calls[symbol] += 1
        if symbol == "META" and calls[symbol] == 2:
            return {"bid": 101.0, "ask": 100.0, "ts": now.isoformat()}
        return {"bid": 100.0, "ask": 100.01,
                "ts": (now - timedelta(seconds=1)).isoformat()}

    manager = _manager(now=now, quote=quotes)
    manager.broker.get_trading_client = lambda: SimpleNamespace(
        get_asset=lambda symbol: {
            "symbol": symbol, "status": "active", "tradable": True,
            "asset_class": "us_equity", "name": f"{symbol} Common Stock",
        }
    )
    exact = _exact_order()
    _wire_clean_boundary(monkeypatch, exact)
    verified = []
    manager._verify_ledger_reflects = lambda rows: verified.extend(rows)

    result = manager.execute_starter([
        {"symbol": "NVDA", "price": 100},
        {"symbol": "META", "price": 100},
    ], 300)

    assert [row["symbol"] for row in result["placed"]] == ["NVDA"]
    assert [row["symbol"] for row in verified] == ["NVDA"]
    assert "boundary session/quote" in result["skipped"][-1]["skip"]


def test_event_reservation_is_serialized_across_callers(monkeypatch):
    manager = _manager(
        now=datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET), quote=None,
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def reserve(symbol):
        barrier.wait()
        try:
            outcomes.append(manager._reserve_event_intent(
                None, symbol, f"F6-{symbol}-b-1-race", 100.01, {"armed": True},
            ))
        except RuntimeError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=reserve, args=(symbol,))
               for symbol in ("NVDA", "META")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    state = manager._load_state_strict()
    assert len(state["events"]) == 1
    assert len([value for value in outcomes if "another F6 event" in value]) == 1


def test_exact_limit_is_reserved_submitted_and_not_reported_spent(monkeypatch):
    now = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    manager = _manager(
        now=now,
        quote=_fresh_quotes(now, ask=100.001),
    )
    exact = _exact_order()
    _wire_clean_boundary(monkeypatch, exact)
    manager._verify_ledger_reflects = lambda *_args: None

    result = manager.execute_starter([{"symbol": "NVDA", "price": 99}], 100.01)

    assert result["spent"] == 0.0
    assert result["reserved"] == 100.01
    assert result["submitted"][0]["hard_limit"] == 100.01
    row = ppb.load()["reservations"][exact.client_order_id]
    assert row["limit_price"] == "100.01"
    assert row["order_type"] == "limit"
    assert row["time_in_force"] == "day"
    assert row["extended_hours"] is True


def test_boundary_response_after_session_close_submits_nothing(monkeypatch):
    before_close = datetime(2026, 10, 16, 19, 59, 59, tzinfo=f6.ET)
    after_close = datetime(2026, 10, 16, 20, 0, tzinfo=f6.ET)
    f6._F6_QUOTE_OBSERVATIONS["NVDA"] = before_close - timedelta(seconds=2)
    quote = {
        "bid": 100.0, "ask": 100.01,
        "ts": (before_close - timedelta(seconds=1)).isoformat(),
    }
    clock_values = iter([before_close, before_close, before_close, after_close])
    manager = _manager(now=before_close, quote=quote)
    manager.clock = lambda: next(clock_values)
    called = []
    monkeypatch.setattr(
        broker, "submit_limit_order", lambda *_args, **_kwargs: called.append(True)
    )

    result = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)

    assert result["placed"] == []
    assert called == []
    assert "boundary session/quote" in result["skipped"][-1]["skip"]


def test_session_close_after_journals_releases_without_broker_call(monkeypatch):
    before_close = datetime(2026, 10, 16, 19, 59, 59, tzinfo=f6.ET)
    after_close = datetime(2026, 10, 16, 20, 0, tzinfo=f6.ET)
    manager = _manager(now=before_close, quote=_fresh_quotes(before_close))
    clock_values = iter([
        before_close, before_close, before_close, before_close, after_close,
    ])
    manager.clock = lambda: next(clock_values)
    exact = _exact_order()
    _wire_clean_boundary(monkeypatch, exact)
    released = []
    monkeypatch.setattr(
        allocator, "live_release_proven_no_order",
        lambda lease, reason: released.append((lease.id, reason)) or True,
    )
    submitted = []
    monkeypatch.setattr(
        broker, "submit_limit_order", lambda *_args, **_kwargs: submitted.append(True)
    )

    result = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)

    assert result["placed"] == []
    assert submitted == []
    assert released == [("lease-1", "session_closed_before_submit")]
    row = ppb.load()["reservations"][exact.client_order_id]
    assert row["status"] == "released_zero"
    state = manager._load_state_strict()
    assert state["events"][0]["status"] == "released_no_order"


def test_invalid_canonical_limit_blocks_before_allocator_or_event(monkeypatch):
    now = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    manager = _manager(now=now, quote=_fresh_quotes(now))
    monkeypatch.setattr(broker, "canonical_buy_limit_price", lambda _ask: float("nan"))
    admissions = []
    monkeypatch.setattr(
        allocator, "live_admit", lambda *_args, **_kwargs: admissions.append(True)
    )

    result = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)

    assert result["placed"] == []
    assert admissions == []
    assert not f6._STATE.exists()


def test_open_zero_fill_is_pending_not_sync_degraded(monkeypatch):
    now = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    manager = _manager(now=now, quote=None)
    exact = _exact_order()
    monkeypatch.setattr(broker, "get_order", lambda _oid: exact)
    writes = []
    monkeypatch.setattr(f6, "_write_sync_degraded", lambda *args: writes.append(args))

    manager._verify_ledger_reflects_inner([
        {"symbol": "NVDA", "qty": 1, "order_id": "order-1"},
    ])

    assert writes == []


def test_pending_journal_rejects_different_limit_contract():
    rid = ppb.reserve(
        "NVDA", 1, "F6-NVDA-b-1-bounded", 0,
        limit_price=100.01, extended_hours=True,
    )
    wrong = _exact_order()
    wrong.limit_price = "100.02"
    with pytest.raises(ppb.PendingProtectedBuyError, match="exactly match"):
        ppb.bind(rid, wrong)


def test_limit_submit_distinguishes_explicit_reject_from_duplicate(monkeypatch):
    class RejectingClient:
        def __init__(self, message):
            self.message = message

        def submit_order(self, _request):
            raise RuntimeError(self.message)

    monkeypatch.setattr(
        broker, "_get_trading_client", lambda: RejectingClient("wash trade detected")
    )
    assert broker.submit_limit_order(
        "NVDA", 1, "buy", 100.001, extended_hours=True,
        tier="forever6", client_order_id="F6-NVDA-b-1-reject",
        distinguish_rejection=True,
    ) is broker.ORDER_DEFINITIVELY_REJECTED

    monkeypatch.setattr(
        broker,
        "_get_trading_client",
        lambda: RejectingClient("40910000 duplicate client order id"),
    )
    assert broker.submit_limit_order(
        "NVDA", 1, "buy", 100.001, extended_hours=True,
        tier="forever6", client_order_id="F6-NVDA-b-1-duplicate",
        distinguish_rejection=True,
    ) is None


def test_event_persistence_failure_prevents_broker_submission(monkeypatch):
    now = datetime(2026, 10, 16, 17, 0, tzinfo=f6.ET)
    manager = _manager(
        now=now,
        quote=_fresh_quotes(now),
    )
    exact = _exact_order()
    _wire_clean_boundary(monkeypatch, exact)
    monkeypatch.setattr(
        manager, "_reserve_event_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fsync failed")),
    )
    submitted = []
    monkeypatch.setattr(
        broker, "submit_limit_order", lambda *_args, **_kwargs: submitted.append(True)
    )
    monkeypatch.setattr(allocator, "live_release", lambda *_args: None)

    result = manager.execute_starter([{"symbol": "NVDA", "price": 100}], 200)

    assert result["placed"] == []
    assert submitted == []
