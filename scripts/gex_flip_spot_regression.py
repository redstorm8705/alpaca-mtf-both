#!/usr/bin/env python3
"""Decisive test: does the emitted GEX 'flip' carry information independent of spot?

BACKGROUND
----------
data/gex.py:384-397 finds the "flip" by sweeping STRIKES with `spot` held FIXED and
then argmin-selecting the crossing NEAREST SPOT. A real gamma flip is the hypothetical
spot S* at which net dealer gamma, REPRICED at S*, crosses zero. Because Black-Scholes
gamma peaks at K ~= spot, a strike-sweep crossing lands near spot BY CONSTRUCTION.

THE TEST (named by the board 2026-07-19)
----------------------------------------
Regress the emitted flip on the underlying's actual price at the same instant.

    slope ~= 1 AND R^2 ~= 1  =>  the flip is a rescaling of spot and carries ZERO
                                 independent information. Tautological. Do not build
                                 an accuracy evaluator on it.
    otherwise                =>  the flip moves on something other than spot, and is
                                 worth evaluating on its own terms.

DATA
----
x: SPY/QQQ 15-min bar close at/just before each snapshot  (T1, data/fetcher.py -- the
   only approved bar source per the Data Source Hierarchy)
y: the `flip` recorded in logs/gex_daily_audit_*.json timelines

Read-only. Writes one JSON to logs/. Safe to run at any time.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from data.fetcher import fetch_bars  # noqa: E402  (T1 approved bar source)

PT = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")
SYMBOLS = ("SPY", "QQQ")
# The 2026-07-15 flip fix (commit aad518a, "local nearest-spot flip")
# landed at 10:02 PT.
FIX_TS = datetime(2026, 7, 15, 10, 2, tzinfo=PT).astimezone(UTC)
MAX_LAG_MIN = 30  # a snapshot with no bar within this window is dropped, not guessed


def ols(xs, ys):
    """Plain OLS. Returns (slope, intercept, r2, n) or None."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return slope, intercept, r2, n


def load_bar_series(symbol: str):
    """[(utc_datetime, close)] ascending, from the approved T1 source."""
    df = fetch_bars(symbol, "15Min", 800)
    if df is None or df.empty:
        return []
    out = []
    for ts, row in df.iterrows():
        dt = ts.to_pydatetime()
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        out.append((dt, float(row["close"])))
    out.sort()
    return out


def spot_at(series, dt_utc):
    """Most recent bar close at or before dt_utc, within MAX_LAG_MIN."""
    lo, hi, best = 0, len(series) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= dt_utc:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is None:
        return None
    bdt, close = series[best]
    return None if (dt_utc - bdt) > timedelta(minutes=MAX_LAG_MIN) else close


def load_timeline():
    rows = []
    for fp in sorted(glob.glob(os.path.join(_ROOT, "logs", "gex_daily_audit_*.json"))):
        try:
            d = json.load(open(fp))
        except Exception as e:
            print(f"  skip {os.path.basename(fp)}: {e}")
            continue
        for snap in d.get("gex", {}).get("timeline", []):
            ts = snap.get("ts")
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %I:%M %p PT").replace(tzinfo=PT)
            except Exception:
                continue
            for sym in SYMBOLS:
                v = snap.get(sym)
                if isinstance(v, dict):
                    rows.append({
                        "ts_pt": ts,
                        "dt_utc": dt.astimezone(UTC),
                        "sym": sym,
                        "label": v.get("label"),
                        "flip": v.get("flip"),
                        "raw_gex_m": v.get("raw_gex_m"),
                    })
    return rows


def describe(tag, sub, out_bucket=None):
    xs = [r["spot"] for r in sub]
    ys = [r["flip"] for r in sub]
    res = ols(xs, ys)
    if not res:
        print(f"\n{tag}: n={len(sub)} — too few to regress")
        return
    slope, intercept, r2, n = res
    devs = sorted(r["dev_pct"] for r in sub)
    med = devs[len(devs) // 2]
    mean_abs = sum(abs(d) for d in devs) / len(devs)
    within1 = sum(1 for d in devs if abs(d) <= 1.0) / len(devs) * 100
    print(f"\n{tag}:  n={n}")
    print(f"  slope     = {slope:+.4f}   (1.00 => flip moves 1:1 with spot)")
    print(f"  intercept = {intercept:+.2f}")
    print(f"  R^2       = {r2:.4f}   (1.00 => spot explains ALL flip variance)")
    print(f"  flip-vs-spot dev: median {med:+.2f}%  mean|dev| {mean_abs:.2f}%"
          f"  range [{devs[0]:+.2f}%, {devs[-1]:+.2f}%]")
    print(f"  within +/-1% of spot: {within1:.1f}%")
    if out_bucket is not None:
        out_bucket[tag] = {
            "n": n, "slope": round(slope, 4), "intercept": round(intercept, 2),
            "r2": round(r2, 4), "median_dev_pct": round(med, 3),
            "mean_abs_dev_pct": round(mean_abs, 3),
            "min_dev_pct": round(devs[0], 3), "max_dev_pct": round(devs[-1], 3),
            "pct_within_1pct_of_spot": round(within1, 1),
        }


def main():
    print("Loading T1 bars (data/fetcher.py)...")
    series = {}
    for sym in SYMBOLS:
        series[sym] = load_bar_series(sym)
        if series[sym]:
            print(f"  {sym}: {len(series[sym])} bars, "
                  f"{series[sym][0][0].date()} -> {series[sym][-1][0].date()}")
        else:
            print(f"  {sym}: NO BARS — this symbol will be skipped")

    rows = load_timeline()
    matched, unmatched = [], 0
    for r in rows:
        if r["flip"] is None or not series.get(r["sym"]):
            continue
        s = spot_at(series[r["sym"]], r["dt_utc"])
        if s is None:
            unmatched += 1
            continue
        r["spot"] = s
        r["dev_pct"] = (r["flip"] - s) / s * 100.0
        matched.append(r)

    print(f"\nsnapshot-symbol rows      : {len(rows)}")
    _n_flip = sum(1 for r in rows if r["flip"] is not None)
    print(f"  with non-null flip      : {_n_flip}")
    print(f"  matched to a bar (USED) : {len(matched)}")
    print(f"  dropped, no bar <= {MAX_LAG_MIN}m: {unmatched}")
    if not matched:
        print("\nNO USABLE ROWS — aborting rather than reporting a misleading result.")
        return 1

    out = {
        "generated": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
        "method": "OLS flip ~ spot; spot = T1 15Min bar close at/<=30m before snapshot",
        "n_rows_total": len(rows), "n_used": len(matched), "n_dropped": unmatched,
        "regressions": {}, "era_split_spy": {},
    }

    print("\n" + "=" * 74)
    print("REGRESSION:   flip  =  slope * spot + intercept")
    print("=" * 74)
    for sym in SYMBOLS:
        sub = [r for r in matched if r["sym"] == sym]
        if sub:
            describe(sym, sub, out["regressions"])
    describe("ALL", matched, out["regressions"])

    print("\n" + "=" * 74)
    print("SPY SPLIT AT THE 2026-07-15 10:02 PT FIX (commit aad518a)")
    print("=" * 74)
    for era, keep in (("PRE-fix", lambda r: r["dt_utc"] < FIX_TS),
                      ("POST-fix", lambda r: r["dt_utc"] >= FIX_TS)):
        sub = [r for r in matched if r["sym"] == "SPY" and keep(r)]
        describe(f"SPY {era}", sub, out["era_split_spy"])

    outp = os.path.join(_ROOT, "logs",
                        f"gex_flip_spot_regression_{datetime.now(PT):%Y-%m-%d}.json")
    with open(outp, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
