# ruff: noqa: E501 — long literal fixtures (project convention)
"""KILL-PARTIAL-1104: the kill switch judges each fill by its own running total, not a cached filled_qty.

Real case 2026-11-04: XOM Swing stop be404bfa (sell 3) filled 1 @ 172.18 (14:32:37Z, cum 1) then 2 @ 171.70
(14:33:58Z, cum 3). The order was read between the two fills and its filled_qty (1) cached; the 2-share fill then
"mismatched" all day and the 7% kill ran on the equity fallback. Every broker call here is mocked.

Signed: Claude, 2026-11-05.
"""
from __future__ import annotations

from datetime import datetime
from unittest import mock

import pytest

from execution import broker
from execution import risk_manager as rm
from reporting import pnl_ledger

TODAY = datetime.now(rm._PT).strftime("%Y-%m-%d")


def _order(order_id: str, *, coid: str = "IN-XOM-s-1793023959331-ba0a0ec7", symbol: str = "XOM",
           side: str = "sell", qty: str = "3", filled_qty: str = "1", tif: str = "gtc") -> mock.Mock:
    return mock.Mock(id=order_id, client_order_id=coid, symbol=symbol, side=side, qty=qty, filled_qty=filled_qty,
                     time_in_force=tif)


def _fill(aid: str, *, oid: str = "be404bfa", qty: str, cum: object, t: str, side: str = "sell",
          symbol: str = "XOM", price: str = "171.70") -> dict:
    return {"id": aid, "order_id": oid, "symbol": symbol, "side": side, "qty": qty, "cum_qty": cum,
            "price": price, "transaction_time": f"{TODAY}T{t}Z"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), ok=False)
    rm._intraday_order_tier_cache.clear()
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    rm._FILL_MISMATCH_LAST_LOGGED["signature"] = None
    rm._FILL_SEQ_LOGGED.update(pt_date="", oids=set())
    monkeypatch.setattr(pnl_ledger, "_pt_date", lambda _value: TODAY)
    yield


def _run(monkeypatch, fills: list, order: mock.Mock):
    calls = {}

    def _fetch(**kw):
        calls.update(kw)
        return list(fills)
    monkeypatch.setattr(pnl_ledger, "fetch_all_fills", _fetch)
    monkeypatch.setattr(broker, "get_order", mock.Mock(return_value=order))
    out = rm._today_fills()
    return out, calls


def test_xom_two_piece_fill_stays_exact_after_a_partial_snapshot(monkeypatch) -> None:
    """The order snapshot shows 1 of 3 filled (read between the fills); both fills are still accepted."""
    fills = [_fill("a1", qty="1", cum="1", t="14:32:37.37442", price="172.18"),
             _fill("a2", qty="2", cum="3", t="14:33:58.151712")]
    (out, ok), calls = _run(monkeypatch, fills, _order("be404bfa", filled_qty="1"))
    assert ok is True
    assert calls == {"strict": True}
    assert [(sym, side, q) for sym, side, q, _px, _tier in out] == [("XOM", "sell", 1), ("XOM", "sell", 2)]
    assert {tier for *_x, tier in out} == {"intraday"}


@pytest.mark.parametrize("seq", [
    [("a1", "1", "2", "14:32:37"), ("a2", "2", "3", "14:33:58")],      # base 1 (earlier fill), then cum 3 != 2 + 2
    [("a1", "2", "3", "14:32:37"), ("a2", "1", "3", "14:33:58")],      # cum does not advance with a second fill
])
def test_codex_malformed_sequences_degrade(monkeypatch, seq) -> None:
    fills = [_fill(aid, qty=q, cum=c, t=t) for aid, q, c, t in seq]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="3"))
    assert (out, ok) == ((), False)


def test_cum_above_order_qty_degrades(monkeypatch) -> None:
    fills = [_fill("a1", qty="1", cum="1", t="14:32:37"), _fill("a2", qty="3", cum="4", t="14:33:58")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", qty="4", filled_qty="4"))
    assert ok is True                                    # 4-share order: consistent
    rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), ok=False)
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    fills = [_fill("a1", qty="1", cum="4", t="14:32:37")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", qty="3", filled_qty="3"))
    assert (out, ok) == ((), False)                      # cum 4 > order qty 3


def test_activity_above_order_qty_degrades(monkeypatch) -> None:
    fills = [_fill("a1", qty="4", cum="4", t="14:32:37")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", qty="3", filled_qty="3"))
    assert (out, ok) == ((), False)


def test_duplicate_activity_id_degrades(monkeypatch) -> None:
    fills = [_fill("a1", qty="1", cum="1", t="14:32:37"), _fill("a1", qty="2", cum="3", t="14:33:58")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="3"))
    assert (out, ok) == ((), False)


@pytest.mark.parametrize("bad_cum", [None, "", "x", "nan", True, "1.5", "0", "-1"])
def test_missing_or_bad_cum_qty_degrades(monkeypatch, bad_cum) -> None:
    fills = [_fill("a1", qty="1", cum=bad_cum, t="14:32:37")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="1"))
    assert (out, ok) == ((), False)


def test_missing_activity_id_degrades(monkeypatch) -> None:
    fills = [_fill("", qty="1", cum="1", t="14:32:37")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="1"))
    assert (out, ok) == ((), False)


def test_cross_session_gtc_order_with_earlier_fills_is_accepted(monkeypatch) -> None:
    """A GTC stop that filled 1 of 3 yesterday and 2 today: today's first cum (3) exceeds its qty (2) by the base."""
    fills = [_fill("a9", qty="2", cum="3", t="14:33:58")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="3"))
    assert ok is True and [q for _s, _d, q, _p, _t in out] == [2]


def test_out_of_order_delivery_is_sorted_by_time(monkeypatch) -> None:
    fills = [_fill("a2", qty="2", cum="3", t="14:33:58"), _fill("a1", qty="1", cum="1", t="14:32:37")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="3"))
    assert ok is True


def test_protected_qhm_order_filling_in_two_pieces_is_excluded_only_when_proven(monkeypatch) -> None:
    coid = "QH-NVDA-s-1793023959331-abc123"
    fills = [_fill("q1", oid="qh1", symbol="NVDA", qty="1", cum="1", t="14:38:01", price="192.39"),
             _fill("q2", oid="qh1", symbol="NVDA", qty="1", cum="2", t="14:38:02", price="192.39")]
    (out, ok), _ = _run(monkeypatch, fills, _order("qh1", coid=coid, symbol="NVDA", qty="2", filled_qty="1"))
    assert ok is True
    assert [tier for *_x, tier in out] == ["qhm", "qhm"]
    # a broken sequence on the protected order degrades the WHOLE pass (nothing partly attributed)
    rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), ok=False)
    rm._intraday_order_tier_cache.update(pt_date="", tiers={})
    bad = [fills[0], _fill("q2", oid="qh1", symbol="NVDA", qty="1", cum="1", t="14:38:02")]
    (out, ok), _ = _run(monkeypatch, bad, _order("qh1", coid=coid, symbol="NVDA", qty="2", filled_qty="2"))
    assert (out, ok) == ((), False)


def test_truncated_history_raises_and_degrades(monkeypatch) -> None:
    def _raise(**kw):
        raise RuntimeError("fill history page has no advancing page_token")
    monkeypatch.setattr(pnl_ledger, "fetch_all_fills", _raise)
    assert rm._today_fills() == ((), False)


def test_mismatch_log_is_detailed_and_not_repeated(monkeypatch, caplog) -> None:
    fills = [_fill("a1", qty="2", cum="3", t="14:32:37"), _fill("a2", qty="1", cum="3", t="14:33:58")]
    with caplog.at_level("WARNING", logger=rm.logger.name):
        for _ in range(3):
            rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), ok=False)
            _run(monkeypatch, fills, _order("be404bfa", filled_qty="3"))
    lines = [r.getMessage() for r in caplog.records if "kill-partial-1104" in r.getMessage()]
    assert len(lines) == 1
    assert "be404bfa" in lines[0] and "cum_sequence" in lines[0] and "equity fallback" in lines[0]
    assert '"activity_id": "a2"' in lines[0] and '"order_qty": 3' in lines[0]


def test_validated_multi_fill_order_logs_proof_once(monkeypatch, caplog) -> None:
    fills = [_fill("a1", qty="1", cum="1", t="14:32:37"), _fill("a2", qty="2", cum="3", t="14:33:58")]
    with caplog.at_level("INFO", logger=rm.logger.name):
        for _ in range(2):
            rm._intraday_cache.update(ts=0.0, pt_date="", fills=(), ok=False)
            _run(monkeypatch, fills, _order("be404bfa", filled_qty="1"))
    lines = [r.getMessage() for r in caplog.records if "validated across" in r.getMessage()]
    assert lines == ["kill-switch fills: order be404bfa validated across 2 fills today (cum 3 of 3) "
                     "[change=kill-partial-1104]"]


def test_earlier_fills_allowed_only_on_a_gtc_order(monkeypatch) -> None:
    """Reviewer case: a DAY order whose first piece today is missing (cum 3 on a 2-share fill) must degrade."""
    fills = [_fill("a9", qty="2", cum="3", t="14:33:58")]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="3", tif="day"))
    assert (out, ok) == ((), False)


@pytest.mark.parametrize("times", [("14:32:37.1", "14:32:37.15"), ("14:32:37", "14:32:37.2")])
def test_same_second_pieces_with_trimmed_fractions_stay_exact(monkeypatch, times) -> None:
    fills = [_fill("a1", qty="1", cum="1", t=times[0]), _fill("a2", qty="2", cum="3", t=times[1])]
    (out, ok), _ = _run(monkeypatch, fills, _order("be404bfa", filled_qty="1"))
    assert ok is True and sorted(q for _s, _d, q, _p, _t in out) == [1, 2]
