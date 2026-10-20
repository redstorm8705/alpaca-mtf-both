import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from events import handlers
from execution import stop_protection


class _Tracker:
    def __init__(self, symbols=()):
        self.open_trades = {symbol: {"status": "open"} for symbol in symbols}


def test_shutdown_routes_every_swing_symbol_through_stop_enforcer(monkeypatch):
    tracker = _Tracker(("NVDA", "META"))
    calls = []

    def reconcile(*args, **kwargs):
        calls.append((args, kwargs))
        return {"already_protected": ["NVDA", "META"]}

    monkeypatch.setattr(stop_protection, "reconcile_protection", reconcile)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is True
    assert len(calls) == 1
    assert calls[0][1] == {
        "session": "closed",
        "place": False,
        "only_symbols": {"NVDA", "META"},
        "allow_cover": False,
        "require_gtc": True,
    }


def test_shutdown_surfaces_any_unresolved_stop_without_closing(monkeypatch):
    tracker = _Tracker(("NVDA",))
    monkeypatch.setattr(
        stop_protection,
        "reconcile_protection",
        lambda *args, **kwargs: {
            "paged": [("NVDA", "unknown")],
            "skipped": [("NVDA", "unreadable")],
        },
    )
    pages = []
    import alerts

    monkeypatch.setattr(alerts, "send_slack", pages.append)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False
    assert len(pages) == 1
    assert "NOT force-sold" in pages[0]


def test_shutdown_does_not_claim_coheld_exclusion_is_protected(monkeypatch):
    tracker = _Tracker(("NVDA",))
    monkeypatch.setattr(
        stop_protection,
        "reconcile_protection",
        lambda *args, **kwargs: {"excluded_qhm": ["NVDA"]},
    )
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False


@pytest.mark.parametrize(
    "proof_rows",
    [
        [("NVDA", 1, 1)],
        [("NVDA", 1, 1), ("NVDA", 1, 1), ("META", 1, 1)],
        [("NVDA", 1, 1), ("META", 1, 1), ("AAPL", 1, 1)],
    ],
)
def test_shutdown_requires_exactly_one_proof_for_every_swing_symbol(
    monkeypatch, proof_rows
):
    tracker = _Tracker(("NVDA", "META"))
    monkeypatch.setattr(
        stop_protection,
        "reconcile_protection",
        lambda *args, **kwargs: {"already_protected": proof_rows},
    )
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False


def test_shutdown_enforcer_exception_is_reported_not_raised(monkeypatch):
    tracker = _Tracker(("NVDA",))
    monkeypatch.setattr(
        stop_protection,
        "reconcile_protection",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    import alerts

    pages = []
    monkeypatch.setattr(alerts, "send_slack", pages.append)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False
    assert len(pages) == 1


def test_second_keyboard_interrupt_does_not_bypass_protection(monkeypatch):
    tracker = _Tracker(("NVDA",))
    monkeypatch.setattr(
        stop_protection,
        "reconcile_protection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            KeyboardInterrupt("second interrupt")
        ),
    )
    import alerts

    pages = []
    monkeypatch.setattr(alerts, "send_slack", pages.append)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False
    assert pages == []


def test_keyboard_interrupt_preserves_swing_instead_of_calling_mass_close():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "main.py").read_text(encoding="utf-8"))
    handlers_for_keyboard_interrupt = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "KeyboardInterrupt"
    ]
    assert len(handlers_for_keyboard_interrupt) == 1
    calls = {
        node.func.id
        for node in ast.walk(handlers_for_keyboard_interrupt[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_preserve_swing_on_shutdown" in calls
    assert "_safe_close_all" not in calls
    assert any(
        isinstance(node, ast.Continue)
        for node in ast.walk(handlers_for_keyboard_interrupt[0])
    )


def test_empty_swing_book_needs_no_broker_reconciliation(monkeypatch):
    monkeypatch.setattr(
        stop_protection,
        "reconcile_protection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("empty shutdown must not call broker")
        ),
    )
    assert handlers.preserve_swing_on_shutdown(_Tracker(), object()) is True


def _real_reconciler_tracker():
    tracker = _Tracker(("NVDA",))
    tracker.open_trades["NVDA"].update(
        direction="long", qty=1, qty_remaining=1, stop=100.0
    )
    return tracker


def _patch_real_reconciler_book(
    monkeypatch, *, orders, current_price, qty="1", side=None
):
    monkeypatch.setattr(stop_protection, "_qhm_symbols", lambda: set())
    monkeypatch.setattr(stop_protection, "_forever6_symbols", lambda: set())
    monkeypatch.setattr(stop_protection, "get_open_orders", lambda: orders)
    monkeypatch.setattr(
        stop_protection,
        "get_open_positions",
        lambda: [
            SimpleNamespace(
                symbol="NVDA", qty=qty, current_price=current_price, side=side
            )
        ],
    )
    monkeypatch.setattr(stop_protection.config, "OWNERSHIP_GUARD_ENFORCE", False)


def test_real_shutdown_mode_never_market_covers_breached_naked_swing(monkeypatch):
    _patch_real_reconciler_book(monkeypatch, orders=[], current_price="90")
    monkeypatch.setattr(
        stop_protection,
        "close_position",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("shutdown mode must not close")
        ),
    )
    result = stop_protection.reconcile_protection(
        _real_reconciler_tracker(),
        session="closed",
        place=True,
        allow_cover=False,
        require_gtc=True,
    )
    assert ("NVDA", "shutdown breached stop") in result["paged"]
    assert result["covered"] == []


def test_real_shutdown_mode_rejects_day_stop_as_overnight_protection(monkeypatch):
    day_stop = SimpleNamespace(
        symbol="NVDA",
        side="sell",
        type="stop",
        time_in_force="day",
        status="new",
        qty="1",
    )
    _patch_real_reconciler_book(monkeypatch, orders=[day_stop], current_price="110")
    result = stop_protection.reconcile_protection(
        _real_reconciler_tracker(),
        session="closed",
        place=True,
        allow_cover=False,
        require_gtc=True,
    )
    assert ("NVDA", "shutdown stop expires") in result["paged"]
    assert result["already_protected"] == []


def test_real_shutdown_mode_rejects_pending_cancel_gtc(monkeypatch):
    cancelling_stop = SimpleNamespace(
        symbol="NVDA",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="pending_cancel",
        qty="1",
        stop_price="100",
    )
    _patch_real_reconciler_book(
        monkeypatch, orders=[cancelling_stop], current_price="110"
    )
    result = stop_protection.reconcile_protection(
        _real_reconciler_tracker(),
        session="closed",
        place=True,
        allow_cover=False,
        require_gtc=True,
    )
    assert ("NVDA", "shutdown stop expires") in result["paged"]
    assert result["already_protected"] == []


def test_real_shutdown_accepts_stable_gtc_at_stored_stop(monkeypatch):
    live_stop = SimpleNamespace(
        symbol="NVDA",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="1",
        stop_price="100",
    )
    _patch_real_reconciler_book(monkeypatch, orders=[live_stop], current_price="110")
    assert handlers.preserve_swing_on_shutdown(
        _real_reconciler_tracker(), object()
    ) is True


@pytest.mark.parametrize("bad_price", [None, "0", "nan", "inf"])
def test_shutdown_rejects_unusable_broker_price(monkeypatch, bad_price):
    live_stop = SimpleNamespace(
        symbol="NVDA",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="1",
        stop_price="100",
    )
    _patch_real_reconciler_book(
        monkeypatch, orders=[live_stop], current_price=bad_price
    )
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(
        _real_reconciler_tracker(), object()
    ) is False


@pytest.mark.parametrize(
    ("tracker_direction", "broker_qty", "broker_side", "order_side"),
    [
        ("long", "-1", "short", "sell"),
        ("short", "1", "long", "buy"),
    ],
)
def test_shutdown_rejects_tracker_broker_direction_mismatch(
    monkeypatch, tracker_direction, broker_qty, broker_side, order_side
):
    live_stop = SimpleNamespace(
        symbol="NVDA",
        side=order_side,
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="1",
        stop_price="100",
    )
    _patch_real_reconciler_book(
        monkeypatch,
        orders=[live_stop],
        current_price="110",
        qty=broker_qty,
        side=broker_side,
    )
    tracker = _real_reconciler_tracker()
    tracker.open_trades["NVDA"]["direction"] = tracker_direction
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False


@pytest.mark.parametrize(
    ("direction", "broker_qty", "broker_side", "order_side", "stored", "actual"),
    [
        ("long", "1", "long", "sell", 100.0, 120.0),
        ("short", "-1", "short", "buy", 120.0, 100.0),
    ],
)
def test_shutdown_rejects_live_stop_already_breached(
    monkeypatch,
    direction,
    broker_qty,
    broker_side,
    order_side,
    stored,
    actual,
):
    live_stop = SimpleNamespace(
        symbol="NVDA",
        side=order_side,
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="1",
        stop_price=str(actual),
    )
    _patch_real_reconciler_book(
        monkeypatch,
        orders=[live_stop],
        current_price="110",
        qty=broker_qty,
        side=broker_side,
    )
    tracker = _real_reconciler_tracker()
    tracker.open_trades["NVDA"].update(direction=direction, stop=stored)
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False


def test_shutdown_does_not_submit_when_gtc_is_not_visible(monkeypatch):
    _patch_real_reconciler_book(monkeypatch, orders=[], current_price="110")
    submits = []
    monkeypatch.setattr(
        stop_protection,
        "submit_gtc_stop_order",
        lambda **kwargs: submits.append(kwargs),
    )
    import alerts

    pages = []
    monkeypatch.setattr(alerts, "send_slack", pages.append)
    assert handlers.preserve_swing_on_shutdown(
        _real_reconciler_tracker(), object()
    ) is False
    assert len(pages) == 1
    assert "NVDA" in pages[0]
    assert submits == []


def test_shutdown_rejects_stale_loose_gtc_stop(monkeypatch):
    stale_stop = SimpleNamespace(
        symbol="NVDA",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="1",
        stop_price="50",
    )
    _patch_real_reconciler_book(monkeypatch, orders=[stale_stop], current_price="110")
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(
        _real_reconciler_tracker(), object()
    ) is False


def test_shutdown_rejects_breached_intended_stop_even_with_live_gtc(monkeypatch):
    live_stop = SimpleNamespace(
        symbol="NVDA",
        side="sell",
        type="stop",
        time_in_force="gtc",
        status="new",
        qty="1",
        stop_price="100",
    )
    _patch_real_reconciler_book(monkeypatch, orders=[live_stop], current_price="90")
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(
        _real_reconciler_tracker(), object()
    ) is False


def test_shutdown_blocks_pending_overnight_tracker_row(monkeypatch):
    tracker = _real_reconciler_tracker()
    tracker.open_trades["NVDA"]["status"] = "pending_overnight"
    _patch_real_reconciler_book(monkeypatch, orders=[], current_price="110")
    import alerts

    monkeypatch.setattr(alerts, "send_slack", lambda message: None)
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False


def test_second_keyboard_interrupt_during_alert_does_not_escape(monkeypatch):
    tracker = _Tracker(("NVDA",))
    monkeypatch.setattr(
        stop_protection,
        "reconcile_protection",
        lambda *args, **kwargs: {"paged": [("NVDA", "unresolved")]},
    )
    import alerts

    monkeypatch.setattr(
        alerts,
        "send_slack",
        lambda message: (_ for _ in ()).throw(KeyboardInterrupt("again")),
    )
    assert handlers.preserve_swing_on_shutdown(tracker, object()) is False
