"""Earnings dead-cat variants, round 3: let the first minutes decide (chosen on DISCOVERY only; test read once).
Bounce target x = walk-forward median first-30-min retrace (as sim8). Exit-early rules:
 R1 floor = the low of the first k 5-min bars (opening-range low), k in {1,2,3,6}; sell ALL at the bounce; else close
 R2 if the first 5-min bar closes red (close < open) sell at the 2nd bar open; else R1 with k=1
 R3 R1 k=1 but a bounce that comes sells only half; rest at the close or the OR-low
 R4 R1 with time cap 60 min (no bounce -> sell)
Candidates: 4 + 1 + 1 + 4 = 10 (plus sim8's 18 and sim7's 13 = 41 earnings variants tried in total). Read-only."""
import numpy as np
from sim7_earn import EV, SPLIT, imp, retr, stats

X = {}
for e in EV:
    h = [q for q in EV if q["day"] < e["day"]]
    if len(h) >= 20: X[id(e)] = float(np.median([retr(q) for q in h]))
rows = [e for e in EV if id(e) in X]


def r1(e, k, half=False, cap=None):
    r, tm, O, C = e["rth"], e["rmin"], e["O"], e["C"]
    tgt = O + X[id(e)] * (C - O); orl = r[:k, 2].min(); rem, got, leg = 1.0, 0.0, True
    for i, (op, hi, lo, _c, _v) in enumerate(r):
        if cap and tm[i] >= 570 + cap: return got + rem * op
        if i >= k and lo < orl: return got + rem * min(op, orl)
        if leg and hi > tgt:
            q = 0.5 if half else 1.0; got += q * tgt; rem -= q; leg = False
            if rem <= 1e-9: return got
    return got + rem * r[-1][3]


def r2(e):
    r = e["rth"]
    if r[0][3] < r[0][0] and len(r) > 1: return r[1][0]
    return r1(e, 1)


C = [(f"R1 k={k}", lambda e, k=k: r1(e, k)) for k in (1, 2, 3, 6)] + [("R2", r2), ("R3", lambda e: r1(e, 1, half=True))] + \
    [(f"R4 k={k}", lambda e, k=k: r1(e, k, cap=60)) for k in (1, 2, 3, 6)]
res = sorted(((np.mean([imp(e, f(e)) for e in rows if e["day"] < SPLIT]), n, f) for n, f in C), key=lambda z: -z[0])
for m, n, _ in res: print(f" disc {n}: {m:+.1f}")
m, n, f = res[0]
print("WINNER", n)
for name, g in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT)):
    v = [imp(e, f(e)) for e in rows if g(e["day"])]
    print(" ", name, stats(v))
    by = {}
    for e in rows:
        if g(e["day"]): by.setdefault(e["day"], []).append(imp(e, f(e)))
    ks = list(by); rng = np.random.default_rng(0)
    bs = [np.mean([x for i in rng.choice(len(ks), len(ks)) for x in by[ks[i]]]) for _ in range(2000)]
    print("   95% CI (date-clustered)", np.round(np.quantile(bs, [.025, .975]), 1))
