"""Date-clustered bootstrap CIs for sim12 (BEAT hold-5 and BEAT minus MISS). Read-only."""
import numpy as np
from sim7_earn import EV, SPLIT, imp
from sim12_beat import tag
rng = np.random.default_rng(0)


def boot(D):
    k = list(D)
    return np.array([np.mean([x for i in rng.choice(len(k), len(k)) for x in D[k[i]]]) for _ in range(4000)])


for name, g in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT), ("all", lambda d: True)):
    B, M = {}, {}
    for e in EV:
        if not g(e["day"]): continue
        t = tag(e); v = imp(e, e["next"][3][3])
        if t == "BEAT": B.setdefault(e["day"], []).append(v)
        elif t == "MISS": M.setdefault(e["day"], []).append(v)
    b, m = boot(B), boot(M)
    print(f"CI {name}: BEAT hold-5 {np.round(np.quantile(b, [.025, .975]))} P(>0)={np.mean(b > 0):.2f} | "
          f"BEAT-MISS {np.round(np.quantile(b - m, [.025, .975]))}")
