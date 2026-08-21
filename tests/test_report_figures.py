# ruff: noqa: E501
"""tests/test_report_figures.py — regression harness for the single-source reporting layer.

Locks the coherence guarantees of reporting.report_figures.ReportFigures so a future edit
cannot silently reintroduce the "day-tiles don't sum to the headline" / "red day at 100% WR"
class of bug. Uses SYNTHETIC round-trips (no live Alpaca fetch) so it runs offline in CI.

Run: `python3 tests/test_report_figures.py`  (also discoverable by pytest).
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reporting.report_figures import ReportFigures, build_report_figures, reconcile  # noqa: E402


def _rt(exit_date: str, pnl: float, symbol: str = "X", entry_time: str = "e") -> dict:
    return {"exit_date": exit_date, "pnl": pnl, "symbol": symbol,
            "entry_time": entry_time, "direction": "long", "qty": 1}


def _mk(round_trips: list) -> ReportFigures:
    by: dict = defaultdict(list)
    for r in round_trips:
        by[r["exit_date"]].append(r)
    return ReportFigures(available=True, version="test", ts_pt="t",
                         round_trips=round_trips, equity=2600.0, net_deposits=2500.0,
                         _by_exit_date=dict(by))


def test_day_pnl_and_wr_from_same_set():
    f = _mk([_rt("2026-08-10", 5.0), _rt("2026-08-10", -3.0)])
    s = f.day_stats("2026-08-10")
    assert s["pnl"] == 2.0, s
    assert s["n_trades"] == 2, s
    assert s["win_rate"] == 50.0, s


def test_red_day_cannot_show_100pct_wr():
    # a day summing < 0 MUST contain a loser -> WR < 100 (the impossible row, eliminated)
    f = _mk([_rt("2026-08-10", 5.0), _rt("2026-08-10", -10.0)])
    s = f.day_stats("2026-08-10")
    assert s["pnl"] < 0 and s["win_rate"] < 100.0, s


def test_green_day_cannot_show_0pct_wr():
    f = _mk([_rt("2026-08-10", -1.0), _rt("2026-08-10", 5.0)])
    s = f.day_stats("2026-08-10")
    assert s["pnl"] > 0 and s["win_rate"] > 0.0, s


def test_headline_equals_sum_of_day_cells():
    rts = [_rt("2026-08-03", 54.51), _rt("2026-08-04", -2.38), _rt("2026-08-20", 28.60)]
    f = _mk(rts)
    days = f.period_days("2026-08-01", "2026-08-31")
    cells = [f.day_pnl(d) for d in days]
    headline = f.period_pnl("2026-08-01", "2026-08-31")
    ok, drift = reconcile(headline, cells)
    assert ok and drift == 0.0, (headline, cells, drift)
    assert round(sum(cells), 2) == headline


def test_reconcile_flags_a_real_mismatch():
    ok, drift = reconcile(100.0, [50.0, 40.0])  # cells sum 90 != headline 100
    assert not ok and drift == 10.0, drift


def test_reconcile_tolerates_float_noise():
    ok, drift = reconcile(0.3, [0.1, 0.1, 0.1])  # 0.1*3 float noise must not false-fail
    assert ok, drift


def test_entry_level_merges_partials_of_one_position():
    # two partial closes of ONE entry -> one entry-level trade, net +8 = a win
    rts = [_rt("2026-08-10", 10.0, entry_time="e1"), _rt("2026-08-11", -2.0, entry_time="e1")]
    f = _mk(rts)
    st = f.lifetime_stats()
    assert st["total_trades"] == 1, st          # merged, not double-counted
    assert st["win_rate"] == 100.0, st          # net positive


def test_period_stats_total_pnl_ties_to_grid():
    rts = [_rt("2026-08-05", 3.0, entry_time="a"), _rt("2026-08-06", -1.0, entry_time="b")]
    f = _mk(rts)
    ps = f.period_stats("2026-08-01", "2026-08-31")
    assert ps["total_pnl"] == f.period_pnl("2026-08-01", "2026-08-31") == 2.0, ps


def test_degrade_returns_unavailable_never_crashes():
    import reporting.pnl_ledger as pl
    orig = pl.build_ledger

    def _boom(*a, **k):
        raise RuntimeError("simulated ledger outage")

    pl.build_ledger = _boom
    try:
        f = build_report_figures()   # imports build_ledger at call time -> sees the patch
        assert f.available is False, f
        assert f.version == "unavailable"
    finally:
        pl.build_ledger = orig


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} report_figures coherence tests PASS")


if __name__ == "__main__":
    _run_all()
