"""Earnings-gap exit variants (owner 2026-11-04: "at least wait for the dead cat bounce"). Read-only replay on the
FMP-tagged earnings gaps in gx_events_nd.pkl. Improvement vs selling at the open, bps of the prior close. Walk-forward:
x (bounce as a fraction of the gap) and F (floor, ATR units) come from EARLIER earnings gaps only (>= 20). Bounce fills
only when a bar trades THROUGH the level, at the level. Floor fills at min(bar open, floor). Multi-day legs use daily
bars: floor checked on the day's low (fill at min(open, floor)), bounce on the day's high (fill at the level).
Variants:
 V1 sell all at the bounce, else at the day's close
 V2 sell all at the bounce, carrying into the next days (up to N), else at day N close
 V3 hold N days with the floor, sell at day N close (no bounce sale)
 V4 half at the bounce, rest at day N close (floor throughout)"""
import os
import pickle

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
EV = [e for e in pickle.load(open(os.path.join(HERE, "gx_events_nd.pkl"), "rb"))
      if e["earn"] and len(e.get("next", [])) == 5]
EV.sort(key=lambda e: e["day"])
SPLIT = pd.Timestamp("2024-01-01")


def imp(e, px): return (px - e["O"]) / e["C"] * 1e4


def retr(e):
    r, tm = e["rth"], e["rmin"]; m = tm < 600
    hi = r[m][:, 1].max() if m.any() else e["O"]; g = e["C"] - e["O"]
    return (hi - e["O"]) / g if g > 0 else 0.0


def ddraw(e):
    return (e["O"] - e["rth"][:, 2].min()) / e["atr"]


def run(e, x, F, mode, N):
    r, O, C, A = e["rth"], e["O"], e["C"], e["atr"]
    floor = O - F * A; tgt = O + x * (C - O)
    rem, got = 1.0, 0.0
    bounce = mode in ("V1", "V2", "V4"); frac = 0.5 if mode == "V4" else 1.0
    for op, hi, lo, _cl, _ in r:
        if lo <= floor: return got + rem * min(op, floor)
        if bounce and hi > tgt:
            got += frac * tgt; rem -= frac; bounce = False
            if rem <= 1e-9: return got
    if mode == "V1":
        return got + rem * r[-1][3]
    for op, hi, lo, _cl in e["next"][:N]:
        if lo <= floor or op <= floor: return got + rem * min(op, floor)
        if bounce and hi > tgt:
            got += frac * max(op, tgt); rem -= frac; bounce = False
            if rem <= 1e-9: return got
    return got + rem * e["next"][N - 1][3]


def stats(v):
    v = np.asarray(v)
    return f"n={len(v):3d} mean={v.mean():+7.1f} med={np.median(v):+6.1f} p05={np.quantile(v, .05):+7.1f} " \
           f"worst={v.min():+7.1f} worse%={(v < 0).mean() * 100:4.1f}"


rows = []
for e in EV:
    hist = [q for q in EV if q["day"] < e["day"]]
    if len(hist) < 20: continue
    x = float(np.median([retr(q) for q in hist]))
    F = float(np.quantile([ddraw(q) for q in hist], 0.75))
    rows.append((e, x, F))
print("earnings events", len(EV), "evaluated", len(rows), "disc", sum(r[0]["day"] < SPLIT for r in rows))
for mode, Ns in (("V1", (1,)), ("V2", (1, 3, 5)), ("V3", (1, 3, 5)), ("V4", (1, 3, 5))):
    for N in Ns:
        for name, f in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT), ("all ", lambda d: True)):
            v = [imp(e, run(e, x, F, mode, N)) for e, x, F in rows if f(e["day"])]
            print(f"{mode} N={N} {name}", stats(v))
a = [r for r in rows if r[0]["sym"] == "AAPL" and str(r[0]["day"].date()) == "2026-11-03"]
for e, x, F in a:
    print("AAPL 11-03 x=%.2f F=%.2f floor=%.2f bounce=%.2f" % (x, F, e["O"] - F * e["atr"], e["O"] + x * (e["C"] - e["O"])),
          {f"{m}{n}": round(run(e, x, F, m, n), 2) for m, n in (("V1", 1), ("V2", 1), ("V2", 3), ("V3", 1), ("V4", 1))})
