"""Does a beat-but-gapped-down earnings gap recover more? Tag each earnings gap with FMP eps/revenue surprise for the
report date (event day or the session before). Outcome: day-5 close vs the open, and the sim11 rule (target q, stop
90th pct) per group. Groups: BEAT = eps and revenue both above estimate; MISS = otherwise (any missing -> excluded).
Read-only. Selection: the split is a single pre-specified hypothesis (owner 2026-11-04 AAPL case), not a grid."""
import json, os
from datetime import timedelta
import numpy as np
from sim7_earn import EV, SPLIT, imp, stats
import sim11_earn as s11
S = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "surprises.json")))


def tag(e):
    rows = S.get(e["sym"])
    if not isinstance(rows, list): return None
    d = e["day"].date()
    for r in rows:
        rd = r.get("date")
        if rd and str(d - timedelta(days=4)) <= rd <= str(d):
            ea, ee, ra, re_ = r.get("epsActual"), r.get("epsEstimated"), r.get("revenueActual"), r.get("revenueEstimated")
            if None in (ea, ee, ra, re_): return None
            return "BEAT" if (ea > ee and ra > re_) else "MISS"
    return None


for name, g in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT), ("all ", lambda d: True)):
    for grp in ("BEAT", "MISS"):
        sub = [e for e in EV if g(e["day"]) and tag(e) == grp]
        if not sub: continue
        d5 = [imp(e, e["next"][3][3]) for e in sub]
        rule = []
        for e in sub:
            h = [p for p in EV if p["day"] < e["day"]]
            if len(h) < 20: continue
            rule.append(imp(e, s11.run(e, float(np.quantile([s11.best5(p) for p in h], .5)), float(np.quantile([s11.ddh(p) for p in h], .9)))))
        print(f"{name} {grp} hold-5-days", stats(d5))
        if rule: print(f"{name} {grp} target-q50   ", stats(rule))
