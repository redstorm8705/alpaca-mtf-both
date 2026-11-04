"""Earnings dead-cat variants, round 2 (chosen on DISCOVERY 2020-23 only; test 2024-26 read once for the winner).
Grid: floor = q_f quantile of earlier earnings gaps' full-day drawdown (ATR units), q_f in {.25,.5,.75};
bounce = q_x quantile of earlier gaps' first-30-min retrace (fraction of gap), q_x in {.25,.5};
time cap T (minutes after 09:30) in {30, 60, 390(close)}: no bounce by T -> sell at that bar's open.
Sell ALL at the bounce. 18 candidates. Read-only."""
import itertools
import numpy as np
from sim7_earn import EV, SPLIT, imp, retr, ddraw, stats


def run(e, x, F, T):
    r, tm, O, C, A = e["rth"], e["rmin"], e["O"], e["C"], e["atr"]
    floor, tgt = O - F * A, O + x * (C - O)
    for i, (op, hi, lo, _c, _v) in enumerate(r):
        if tm[i] >= 570 + T: return op
        if lo <= floor: return min(op, floor)
        if hi > tgt: return tgt
    return r[-1][3]


P = {}
for e in EV:
    h = [q for q in EV if q["day"] < e["day"]]
    if len(h) >= 20:
        P[id(e)] = (np.array([retr(q) for q in h]), np.array([ddraw(q) for q in h]))
rows = [e for e in EV if id(e) in P]
res = []
for qf, qx, T in itertools.product((.25, .5, .75), (.25, .5), (30, 60, 390)):
    def val(e):
        rt, dd = P[id(e)]
        return imp(e, run(e, float(np.quantile(rt, qx)), float(np.quantile(dd, qf)), T))
    d = [val(e) for e in rows if e["day"] < SPLIT]
    res.append((np.mean(d), qf, qx, T, val))
res.sort(key=lambda z: -z[0])
print("candidates", len(res), "disc mean of all %.1f, share>0 %.2f" % (np.mean([r[0] for r in res]), np.mean([r[0] > 0 for r in res])))
for m, qf, qx, T, _ in res[:6]:
    print(f" disc qf={qf} qx={qx} T={T}: mean {m:+.1f}")
m, qf, qx, T, val = res[0]
print(f"WINNER qf={qf} qx={qx} T={T}")
for name, f in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT)):
    v = [val(e) for e in rows if f(e["day"])]
    print(" ", name, stats(v))
    # date-clustered bootstrap CI
    by = {}
    for e in rows:
        if f(e["day"]): by.setdefault(e["day"], []).append(val(e))
    ks = list(by); rng = np.random.default_rng(0); bs = []
    for _ in range(2000):
        s = rng.choice(len(ks), len(ks)); vv = [x for i in s for x in by[ks[i]]]; bs.append(np.mean(vv))
    print("   95% CI (date-clustered)", np.round(np.quantile(bs, [.025, .975]), 1))
