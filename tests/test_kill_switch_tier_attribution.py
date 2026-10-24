# ruff: noqa: E501  — dense test tables run long (project convention)
"""KILL-ATTR-1023 (owner approval 2026-10-23): with the ownership guard off, the 7% kill measures Day + Swing only, by
exact shares per symbol, and never hides a Day/Swing loss when the ledger cannot prove a symbol.
Design record: logs/design_records/kill_switch_tier_attribution_2026-10-23.md. No broker call: positions, fills, the
ledger and the QHM state are all mocked. Signed: Claude, 2026-10-23."""
from __future__ import annotations

from unittest import mock

import pytest

from execution import ownership_guard
from execution import quarterly_hold_manager as qhm_mod
from execution import risk_manager as rm


def _row(net: float, *, day: float = 0.0, swing: float = 0.0, qhm: float = 0.0, f6: float = 0.0,
         drift: float = 0.0) -> dict:
    return {"alpaca_net_qty": net, "drift": drift,
            "tiers": {"daytrade": {"qty": day}, "intraday": {"qty": swing}, "qhm": {"qty": qhm},
                      "forever6": {"qty": f6}}}


def _ledger(**rows: dict) -> dict:
    return {"version": 1, "last_reconciled_utc": "2026-10-23T12:00:00+00:00", "positions": rows}


def _positions(*rows: tuple) -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = [
        {"symbol": s, "qty": str(q), "current_price": str(cur), "lastday_price": str(pc),
         "unrealized_intraday_pl": str((cur - pc) * q)} for s, q, cur, pc in rows]
    return resp


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(rm.config, "OWNERSHIP_GUARD_ENFORCE", False)
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    monkeypatch.setattr(rm, "_save_kill_state", mock.Mock())
    monkeypatch.setattr(rm, "_publish_kill_snapshot", mock.Mock())
    rm._KILL_ATTR_LAST_LOGGED["signature"] = None

    def setup(*, ledger, positions, fills, qhm_state):
        if isinstance(ledger, Exception):
            monkeypatch.setattr(ownership_guard, "load_ledger", mock.Mock(side_effect=ledger))
        else:
            monkeypatch.setattr(ownership_guard, "load_ledger", lambda: ledger)
        monkeypatch.setattr(rm.requests, "get", lambda *a, **k: _positions(*positions))
        monkeypatch.setattr(rm, "_today_fills", lambda: (tuple(fills), True))
        if isinstance(qhm_state, Exception):
            monkeypatch.setattr(qhm_mod, "get_quarterly_hold_quantities", mock.Mock(side_effect=qhm_state))
        else:
            monkeypatch.setattr(qhm_mod, "get_quarterly_hold_quantities", lambda: dict(qhm_state))
        with mock.patch.object(rm, "_load_kill_state", return_value={}):
            return rm.RiskManager(2600.0, daily_start_value=2600.0)
    return setup


def test_cohold_day_lot_counts_only_qhm_shares_removed(env):
    """QHM 2 GOOGL + Day 10 GOOGL bought today at +$40 vs prior close, GOOGL now +$30: the Day lot's -$100 counts
    (the old measure dropped the whole symbol's open leg)."""
    risk = env(ledger=_ledger(GOOGL=_row(12, day=10, qhm=2)), positions=[("GOOGL", 12, 330.0, 300.0)],
               fills=[("GOOGL", "buy", 10, 340.0, "daytrade")], qhm_state={"GOOGL": 2})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(10 * 30 - 10 * 40)        # -100: QHM's 2-share +$60 removed
    assert risk._kill_attr["exact"] is True


def test_board_googl_input_untagged_exit_booked_to_wrong_tier(env):
    """Taleb seat's input: QHM 2; Day buys 10 at +$40; an untagged stop sells 2 at +$35; GOOGL +$30. The replay booked
    the sell to QHM (qhm 0, day 10). The QHM state still says 2 -> unproven -> more negative of the two -> true -$90."""
    risk = env(ledger=_ledger(GOOGL=_row(10, day=10, qhm=0)), positions=[("GOOGL", 10, 330.0, 300.0)],
               fills=[("GOOGL", "buy", 10, 340.0, "daytrade"), ("GOOGL", "sell", 2, 335.0, None)],
               qhm_state={"GOOGL": 2})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(-90.0)
    assert risk._kill_attr["exact"] is False
    assert risk._kill_attr["unproven"]["GOOGL"]["count_all"] == pytest.approx(-30.0)


def test_lagging_qhm_state_with_day_fill_is_not_excluded(env):
    """QHM 4 (state still 4), an untagged QHM stop sold 2, Day bought 2 (DT) at $105; prior $100, now $103: broker 4 ==
    state 4 but a Day fill exists, so the symbol is NOT excluded; the measure is at least as negative as the true -$4."""
    risk = env(ledger=_ledger(GOOGL=_row(4, day=2, qhm=2)), positions=[("GOOGL", 4, 103.0, 100.0)],
               fills=[("GOOGL", "buy", 2, 105.0, "daytrade"), ("GOOGL", "sell", 2, 101.0, None)],
               qhm_state={"GOOGL": 4})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl <= -4.0
    assert risk._kill_attr["unproven"]["GOOGL"]["rule"].startswith("more negative")


def test_owner_manual_qhm_add_is_excluded_and_entries_continue(env):
    """GE 10/23: QHM 3 by ledger and by its own state, the owner's untagged buy today, no Day/Swing fill -> excluded;
    no raise, so entries continue."""
    risk = env(ledger=_ledger(GE=_row(3, qhm=3)), positions=[("GE", 3, 286.0, 292.0)],
               fills=[("GE", "buy", 1, 286.74, None)], qhm_state={"GE": 3})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == 0.0
    assert risk._kill_attr["unproven"]["GE"]["rule"].startswith("excluded")
    assert risk.check_kill_switch() is False


def test_qhm_only_move_excluded_and_exact(env):
    risk = env(ledger=_ledger(NVDA=_row(2, qhm=2), AAPL=_row(1, swing=1)),
               positions=[("NVDA", 2, 180.0, 200.0), ("AAPL", 1, 340.0, 345.0)], fills=[],
               qhm_state={"NVDA": 2})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(-5.0)                     # only the Swing AAPL move counts
    assert risk._kill_attr["exact"] is True


def test_negative_claim_on_protected_row_is_unproven(env):
    risk = env(ledger=_ledger(GE=_row(3, day=-1, swing=1, qhm=3)), positions=[("GE", 3, 286.0, 292.0)],
               fills=[], qhm_state={"GE": 3})
    risk._intraday_trading_pnl()
    assert "GE" in risk._kill_attr["unproven"]


def test_unreadable_qhm_state_counts_every_share_of_an_unproven_symbol(env):
    risk = env(ledger=_ledger(GOOGL=_row(10, day=10, qhm=0, drift=2)), positions=[("GOOGL", 12, 330.0, 300.0)],
               fills=[("GOOGL", "buy", 10, 340.0, "daytrade")], qhm_state=RuntimeError("corrupt"))
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(12 * 30 - 10 * 40)
    assert risk._kill_attr["unproven"]["GOOGL"]["rule"] == "count-all"


def test_f6_claim_on_unproven_symbol_counts_every_share(env):
    risk = env(ledger=_ledger(NVDA=_row(3, day=2, f6=1)), positions=[("NVDA", 3, 190.0, 200.0)],
               fills=[("NVDA", "buy", 1, 195.0, None)], qhm_state={})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(3 * -10 + (200.0 - 195.0))
    assert risk._kill_attr["unproven"]["NVDA"]["rule"] == "count-all"


def test_fresh_symbol_without_ledger_row_counts_every_share(env):
    risk = env(ledger=_ledger(), positions=[("DRAM", -26, 49.26, 49.71)],
               fills=[("DRAM", "sell_short", 26, 48.51, "daytrade")], qhm_state={})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(-26 * (49.26 - 49.71) + 26 * (48.51 - 49.71))


def test_unreadable_ledger_raises_so_entries_pause(env):
    risk = env(ledger=ownership_guard.LedgerError("corrupt"), positions=[("GE", 3, 286.0, 292.0)], fills=[],
               qhm_state={"GE": 3})
    with pytest.raises(RuntimeError):
        risk.check_kill_switch()


def test_exact_measure_publishes_attribution_exact_true(env):
    risk = env(ledger=_ledger(AAPL=_row(1, swing=1)), positions=[("AAPL", 1, 340.0, 345.0)], fills=[],
               qhm_state={})
    assert risk.check_kill_switch() is False
    kwargs = rm._publish_kill_snapshot.call_args.kwargs
    assert kwargs["attribution_exact"] is True and kwargs["trading_pnl"] == pytest.approx(-5.0)


def test_unproven_measure_publishes_attribution_exact_false(env):
    risk = env(ledger=_ledger(GE=_row(3, qhm=3)), positions=[("GE", 3, 286.0, 292.0)],
               fills=[("GE", "buy", 1, 286.74, None)], qhm_state={"GE": 3})
    risk.check_kill_switch()
    assert rm._publish_kill_snapshot.call_args.kwargs["attribution_exact"] is False


def test_cohold_day_loss_trips_the_kill(env):
    """A -$200 Day loss on a QHM name (7% of $2,600 = $182) trips; the old measure would not see the open leg."""
    risk = env(ledger=_ledger(GOOGL=_row(12, day=10, qhm=2)), positions=[("GOOGL", 12, 320.0, 300.0)],
               fills=[("GOOGL", "buy", 10, 340.0, "daytrade")], qhm_state={"GOOGL": 2})
    assert risk.check_kill_switch() is True
    assert risk.last_kill_measure == pytest.approx(-200.0)


def test_stale_qhm_count_on_flat_symbol_does_not_degrade(env):
    """QHM state still lists 2 LLY but the broker holds none and nothing traded today: count-all (0), not a degrade."""
    risk = env(ledger=_ledger(LLY=_row(2, qhm=2)), positions=[("AAPL", 1, 340.0, 345.0)], fills=[],
               qhm_state={"LLY": 2})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(-5.0)
    assert risk._kill_attr["unproven"]["LLY"]["rule"].startswith("count-all")


def test_untagged_child_leg_stop_exit_blocks_exclusion(env):
    """Adversarial review input: QHM 3 GE plus a leftover 2-share Day lot; today the lot's bracket stop child (untagged)
    sells 2 at $284 vs prior $292. Broker 3 == QHM state 3 and no tagged fill, but the untagged SELL blocks exclusion,
    so the lot's -$16 is counted."""
    risk = env(ledger=_ledger(GE=_row(3, qhm=3)), positions=[("GE", 3, 286.0, 292.0)],
               fills=[("GE", "sell", 2, 284.0, None)], qhm_state={"GE": 3})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl <= 2 * (284.0 - 292.0)
    assert not risk._kill_attr["unproven"]["GE"]["rule"].startswith("excluded")


def test_flat_stale_symbol_qhm_gain_cannot_offset_day_loss(env):
    """Cold review input: broker flat, QHM state stale at 5; today QHM bought 5 @100 and sold 5 @110 (QH-tagged), Day
    bought 5 @100 and sold 5 @90 (DT). Count-all nets to $0; the Day loss is -$50 and must count."""
    risk = env(ledger=_ledger(MU=_row(5, qhm=5)), positions=[("AAPL", 1, 345.0, 345.0)],
               fills=[("MU", "buy", 5, 100.0, "qhm"), ("MU", "sell", 5, 110.0, "qhm"),
                      ("MU", "buy", 5, 100.0, "daytrade"), ("MU", "sell", 5, 90.0, "daytrade")],
               qhm_state={"MU": 5})
    pnl, ok = risk._intraday_trading_pnl()
    assert ok and pnl == pytest.approx(-50.0)
