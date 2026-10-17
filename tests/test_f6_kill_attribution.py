"""Quantity-aware F6/QHM attribution in the 7% intraday kill.

Signed: OpenAI Codex, 2026-10-15.
"""
from __future__ import annotations

from unittest import mock
from datetime import datetime

import pytest
from alpaca.trading.enums import OrderSide

from execution import ownership_guard
from execution import broker
from execution import risk_manager as rm
from reporting import pnl_ledger


def _ledger(*, net: float, f6: float = 0.0, qhm: float = 0.0) -> dict:
    return {
        "version": 1,
        "last_reconciled_utc": "2026-10-15T12:00:00+00:00",
        "positions": {
            "NVDA": {
                "alpaca_net_qty": net,
                "drift": 0.0,
                "tiers": {
                    "daytrade": {"qty": net - f6 - qhm},
                    "intraday": {"qty": 0.0},
                    "qhm": {"qty": qhm},
                    "forever6": {"qty": f6},
                },
            }
        },
    }


def _positions(
    *, qty: float, current: float = 90.0, prior: float = 100.0, symbol: str = "NVDA"
):
    response = mock.Mock()
    response.status_code = 200
    response.json.return_value = [
        {
            "symbol": symbol,
            "qty": str(qty),
            "current_price": str(current),
            "lastday_price": str(prior),
            "unrealized_intraday_pl": str((current - prior) * qty),
        }
    ]
    return response


def _risk() -> rm.RiskManager:
    with mock.patch.object(rm, "_load_kill_state", return_value={}):
        return rm.RiskManager(900.0, daily_start_value=1_000.0)


def _order(
    order_id: str,
    *,
    coid: str = "F6-NVDA-b-1-test",
    symbol: str = "NVDA",
    side: object = "buy",
    qty: object = "1",
    filled_qty: object = "1",
) -> mock.Mock:
    return mock.Mock(
        id=order_id,
        client_order_id=coid,
        symbol=symbol,
        side=side,
        qty=qty,
        filled_qty=filled_qty,
    )


def test_fill_order_owner_uses_exact_client_order_tag(monkeypatch) -> None:
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    lookup = mock.Mock(return_value=_order("order-1"))
    monkeypatch.setattr(broker, "get_order", lookup)

    assert rm._tier_for_fill_order(
        "order-1", "2026-10-15", "NVDA", "buy", 1
    ) == "forever6"
    assert rm._tier_for_fill_order(
        "order-1", "2026-10-15", "NVDA", "buy", 1
    ) == "forever6"
    lookup.assert_called_once_with("order-1", nested=True)


def test_fill_owner_accepts_real_alpaca_order_side_enum(monkeypatch) -> None:
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    monkeypatch.setattr(
        broker,
        "get_order",
        mock.Mock(return_value=_order("order-enum", side=OrderSide.BUY)),
    )

    assert rm._tier_for_fill_order(
        "order-enum", "2026-10-15", "NVDA", "buy", 1
    ) == "forever6"


def test_unreadable_fill_owner_degrades_and_is_not_cached(monkeypatch) -> None:
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    lookup = mock.Mock(
        side_effect=[
            None,
            _order("order-2"),
        ]
    )
    monkeypatch.setattr(broker, "get_order", lookup)

    assert rm._tier_for_fill_order(
        "order-2", "2026-10-15", "NVDA", "buy", 1
    ) == rm._UNRESOLVED_FILL_TIER
    assert rm._tier_for_fill_order(
        "order-2", "2026-10-15", "NVDA", "buy", 1
    ) == "forever6"
    assert lookup.call_count == 2


def test_missing_fill_order_id_is_unresolved() -> None:
    assert rm._tier_for_fill_order(
        None, "2026-10-15", "NVDA", "buy", 1
    ) == rm._UNRESOLVED_FILL_TIER


def test_nonfinite_fill_price_degrades_before_attribution(monkeypatch) -> None:
    today = datetime.now(rm._PT).strftime("%Y-%m-%d")
    rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), ok=False)
    monkeypatch.setattr(
        pnl_ledger,
        "fetch_all_fills",
        lambda: [{
            "transaction_time": today,
            "symbol": "NVDA",
            "side": "buy",
            "qty": "1",
            "price": "nan",
            "order_id": "order-nan",
        }],
    )
    monkeypatch.setattr(pnl_ledger, "_pt_date", lambda _value: today)

    assert rm._today_fills() == ((), False)


@pytest.mark.parametrize("bad_qty", ["1.9", True, "-1", "nan"])
def test_non_whole_positive_fill_quantity_degrades(
    monkeypatch, bad_qty: object
) -> None:
    today = datetime.now(rm._PT).strftime("%Y-%m-%d")
    rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), ok=False)
    monkeypatch.setattr(
        pnl_ledger,
        "fetch_all_fills",
        lambda: [{
            "transaction_time": today,
            "symbol": "NVDA",
            "side": "buy",
            "qty": bad_qty,
            "price": "110",
            "order_id": "order-bad-qty",
        }],
    )
    monkeypatch.setattr(pnl_ledger, "_pt_date", lambda _value: today)

    assert rm._today_fills() == ((), False)


def test_confirmed_untagged_order_is_cached_as_active(monkeypatch) -> None:
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    lookup = mock.Mock(return_value=_order("order-2", coid="manual-order"))
    monkeypatch.setattr(broker, "get_order", lookup)

    assert rm._tier_for_fill_order(
        "order-2", "2026-10-15", "NVDA", "buy", 1
    ) is None
    assert rm._tier_for_fill_order(
        "order-2", "2026-10-15", "NVDA", "buy", 1
    ) is None
    lookup.assert_called_once()


def test_fill_order_identity_mismatch_is_unresolved(monkeypatch) -> None:
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    monkeypatch.setattr(
        broker,
        "get_order",
        mock.Mock(return_value=_order("wrong", coid="manual")),
    )

    assert rm._tier_for_fill_order(
        "expected", "2026-10-15", "NVDA", "buy", 1
    ) == (
        rm._UNRESOLVED_FILL_TIER
    )


@pytest.mark.parametrize(
    ("changes", "fill"),
    [
        ({"symbol": "MSFT"}, ("NVDA", "buy", 1)),
        ({"side": "sell"}, ("NVDA", "buy", 1)),
        ({"qty": "1", "filled_qty": "1"}, ("NVDA", "buy", 2)),
    ],
)
def test_fill_order_fact_mismatch_is_unresolved(
    monkeypatch, changes: dict, fill: tuple
) -> None:
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    order = _order("order-facts")
    for field, value in changes.items():
        setattr(order, field, value)
    monkeypatch.setattr(broker, "get_order", mock.Mock(return_value=order))

    assert rm._tier_for_fill_order(
        "order-facts", "2026-10-15", fill[0], fill[1], fill[2]
    ) == rm._UNRESOLVED_FILL_TIER


@pytest.mark.parametrize(
    "bad_coid",
    [
        "F6-MSFT-b-1-test",
        "F6-not-an-order",
        "F6-NVDA-s-1-test",
        "F6-NVDA-b-notatime-test",
        "F6-NVDA-b-1-bad_nonce",
        "F6-nvda-b-1-test",
    ],
)
def test_protected_coid_must_bind_complete_order_envelope(
    monkeypatch, bad_coid: str
) -> None:
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    monkeypatch.setattr(
        broker,
        "get_order",
        mock.Mock(return_value=_order("order-coid", coid=bad_coid)),
    )

    assert rm._tier_for_fill_order(
        "order-coid", "2026-10-15", "NVDA", "buy", 1
    ) == rm._UNRESOLVED_FILL_TIER


def test_f6_cohold_excludes_only_f6_quantity_and_kill_trips(monkeypatch) -> None:
    """One protected share cannot hide ten Day shares losing ten dollars each."""
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=11, f6=1))
    monkeypatch.setattr(
        rm,
        "_today_fills",
        lambda: ((("NVDA", "buy", 10, 100.0, "daytrade"),), True),
    )
    monkeypatch.setattr(rm.requests, "get", lambda *a, **k: _positions(qty=11))
    monkeypatch.setattr(rm, "_save_kill_state", mock.Mock())

    risk = _risk()
    assert risk._intraday_trading_pnl() == (-100.0, True)
    assert risk.check_kill_switch() is True
    assert risk.last_kill_measure == -100.0


def test_f6_only_move_is_excluded_from_intraday_kill(monkeypatch) -> None:
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=1, f6=1))
    monkeypatch.setattr(rm, "_today_fills", lambda: ((), True))
    monkeypatch.setattr(rm.requests, "get", lambda *a, **k: _positions(qty=1))

    risk = _risk()
    assert risk._intraday_trading_pnl() == (0.0, True)
    assert risk.check_kill_switch() is False
    assert risk.last_kill_measure == 0.0


def test_exact_ledger_allows_active_tier_short_without_protected_shares(
    monkeypatch,
) -> None:
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=-2))
    monkeypatch.setattr(rm, "_today_fills", lambda: ((), True))
    monkeypatch.setattr(
        rm.requests, "get", lambda *a, **k: _positions(qty=-2, current=110.0)
    )

    assert _risk()._intraday_trading_pnl() == (-20.0, True)


def test_same_day_f6_buy_cannot_offset_near_threshold_day_loss(monkeypatch) -> None:
    """Protected fill correction must not rescue an active book below -7%."""
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=11, f6=1))
    monkeypatch.setattr(
        rm,
        "_today_fills",
        lambda: (
            (
                ("NVDA", "buy", 10, 100.0, "daytrade"),
                ("NVDA", "buy", 1, 90.0, "forever6"),
            ),
            True,
        ),
    )
    monkeypatch.setattr(
        rm.requests, "get", lambda *a, **k: _positions(qty=11, current=92.5)
    )
    monkeypatch.setattr(rm, "_save_kill_state", mock.Mock())

    risk = _risk()
    assert risk._intraday_trading_pnl() == (-75.0, True)
    assert risk.check_kill_switch() is True


def test_protected_buy_before_ledger_replay_becomes_control_fault(monkeypatch) -> None:
    """A tagged F6 fill cannot be excluded until ledger net reflects it."""
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=10))
    monkeypatch.setattr(
        rm,
        "_today_fills",
        lambda: (
            (
                ("NVDA", "buy", 10, 100.0, "daytrade"),
                ("NVDA", "buy", 1, 90.0, "forever6"),
            ),
            True,
        ),
    )
    monkeypatch.setattr(
        rm.requests, "get", lambda *a, **k: _positions(qty=11, current=92.5)
    )

    risk = _risk()
    assert risk._intraday_trading_pnl() == (0.0, False)
    with pytest.raises(RuntimeError, match="exact tier attribution is unresolved"):
        risk.check_kill_switch()


def test_same_day_f6_only_buy_has_zero_intraday_kill_pnl(monkeypatch) -> None:
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=1, f6=1))
    monkeypatch.setattr(
        rm,
        "_today_fills",
        lambda: ((("NVDA", "buy", 1, 90.0, "forever6"),), True),
    )
    monkeypatch.setattr(
        rm.requests, "get", lambda *a, **k: _positions(qty=1, current=92.5)
    )

    assert _risk()._intraday_trading_pnl() == (0.0, True)


def test_protected_sell_before_ledger_replay_becomes_control_fault(monkeypatch) -> None:
    """A filled protected sell cannot leave a stale share masking Day loss."""
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=11, f6=1))
    monkeypatch.setattr(
        rm,
        "_today_fills",
        lambda: (
            (
                ("NVDA", "buy", 10, 100.0, "daytrade"),
                ("NVDA", "sell", 1, 92.5, "forever6"),
            ),
            True,
        ),
    )
    monkeypatch.setattr(
        rm.requests, "get", lambda *a, **k: _positions(qty=10, current=92.5)
    )

    risk = _risk()
    assert risk._intraday_trading_pnl() == (0.0, False)
    with pytest.raises(RuntimeError, match="exact tier attribution is unresolved"):
        risk.check_kill_switch()


def test_broker_symbol_case_cannot_bypass_protected_quantity(monkeypatch) -> None:
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=1, f6=1))
    monkeypatch.setattr(rm, "_today_fills", lambda: ((), True))
    monkeypatch.setattr(
        rm.requests, "get", lambda *a, **k: _positions(qty=1, symbol="nvda")
    )

    assert _risk()._intraday_trading_pnl() == (0.0, True)


@pytest.mark.parametrize("field", ["qty", "current_price", "lastday_price"])
def test_nonfinite_position_input_degrades(monkeypatch, field: str) -> None:
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=1, f6=1))
    monkeypatch.setattr(rm, "_today_fills", lambda: ((), True))
    response = _positions(qty=1)
    response.json.return_value[0][field] = "nan"
    monkeypatch.setattr(rm.requests, "get", lambda *a, **k: response)

    assert _risk()._intraday_trading_pnl() == (0.0, False)


@pytest.mark.parametrize("bad_qty", [True, 1.5, 0])
def test_malformed_broker_position_quantity_degrades(
    monkeypatch, bad_qty: object
) -> None:
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: _ledger(net=1, f6=1))
    monkeypatch.setattr(rm, "_today_fills", lambda: ((), True))
    response = _positions(qty=1)
    response.json.return_value[0]["qty"] = bad_qty
    monkeypatch.setattr(rm.requests, "get", lambda *a, **k: response)

    assert _risk()._intraday_trading_pnl() == (0.0, False)


def test_drifted_protected_ledger_degrades_instead_of_masking(monkeypatch) -> None:
    bad = _ledger(net=11, f6=1)
    bad["positions"]["NVDA"]["drift"] = 1.0
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: bad)
    monkeypatch.setattr(rm, "_today_fills", lambda: ((), True))

    risk = _risk()
    assert risk._intraday_trading_pnl() == (0.0, False)


def test_tier_claims_must_sum_to_ledger_net(monkeypatch) -> None:
    """A false protected claim cannot hide an unowned losing share."""
    bad = _ledger(net=11, qhm=10)
    bad["positions"]["NVDA"]["tiers"]["daytrade"]["qty"] = 0
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: bad)

    assert _risk()._protected_held_qty() is None


@pytest.mark.parametrize(
    ("path", "bad_value"),
    [
        (("alpaca_net_qty",), True),
        (("alpaca_net_qty",), 1.5),
        (("drift",), True),
        (("drift",), 0.5),
        (("tiers", "qhm", "qty"), True),
        (("tiers", "qhm", "qty"), 0.5),
        (("tiers", "forever6", "qty"), float("nan")),
    ],
)
def test_malformed_ledger_quantities_are_not_trusted(
    monkeypatch, path: tuple[str, ...], bad_value: object
) -> None:
    bad = _ledger(net=1, f6=1)
    target = bad["positions"]["NVDA"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad_value
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setattr(ownership_guard, "load_ledger", lambda: bad)

    assert _risk()._protected_held_qty() is None


@pytest.mark.parametrize("in_rth", [True, False])
def test_unresolved_exact_fill_attribution_is_control_fault_in_all_sessions(
    monkeypatch, in_rth: bool
) -> None:
    """A cheap protected fill must never rescue a 7.5% Day loss."""
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", True)
    monkeypatch.setattr(rm, "_today_fills", lambda: ((), False))
    monkeypatch.setattr(rm, "_in_rth_now", lambda: in_rth)

    risk = _risk()
    risk.portfolio_value = 927.5
    with pytest.raises(
        RuntimeError, match="exact tier attribution is unresolved"
    ):
        risk.check_kill_switch()
