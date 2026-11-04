"""Earnings round 4: wait for the bounce only on SMALLER earnings gaps (gap in ATR units below the walk-forward median of
earlier earnings gaps); larger ones exit at the open. Wait rule = sim9 R4 k=1 or sim7 V1. Also the same split by
gap size for every earnings gap, reported by bucket. Read-only."""
import numpy as np
from sim7_earn import EV, SPLIT, imp, stats
import sim9_earn as s9
import sim7_earn as s7
import contextlib, io
rows = s9.rows
K = {id(e): float(np.median([q["kgap"] for q in EV if q["day"] < e["day"]])) for e in rows}
F7 = {}
for e in rows:
    h = [q for q in EV if q["day"] < e["day"]]
    F7[id(e)] = (s9.X[id(e)], float(np.quantile([s7.ddraw(q) for q in h], .75)))
rules = {"R4k1": lambda e: s9.r1(e, 1, cap=60), "V1": lambda e: s7.run(e, *F7[id(e)], "V1", 1)}
for rn, f in rules.items():
    for name, g in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT)):
        small = [imp(e, f(e)) for e in rows if g(e["day"]) and e["kgap"] < K[id(e)]]
        big = [imp(e, f(e)) for e in rows if g(e["day"]) and e["kgap"] >= K[id(e)]]
        cond = [imp(e, f(e)) if e["kgap"] < K[id(e)] else 0.0 for e in rows if g(e["day"])]
        print(f"{rn} {name} small-gap wait  ", stats(small))
        print(f"{rn} {name} big-gap wait    ", stats(big))
        print(f"{rn} {name} policy(small wait, big open)", stats(cond))
