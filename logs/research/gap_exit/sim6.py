"""GAP-EXIT-1029 rev 3.2 replay: the budget-floor rule (design §3e item 2). Read-only.
Same walk-forward parameters as sim5 (x, T, F from gaps on EARLIER days only; bounce fills only when a bar trades through
the level). New: the pre-open stop is the lot's budget floor bf = C * (1 - a), a = 7% headroom / (lot weight w * number of
gapped lots that day); an open at or below bf exits at the open (improvement 0); in the first 5-min bar after the open the
stop is still bf (the raise happens in the first post-open cycle); from the second bar the floor is max(O - F*ATR, bf).
Same slippage s on every leg of both policies (waiting effect only). Lot weight w = position notional / equity.
n_same: 'one' = the lot is the only gapped lot; 'panel' = every panel name gapping that day is held (an upper bound)."""
import collections

import numpy as np
from sim import EV as ALL, imp, stats, fmt, SPLIT
from sim5 import retr, tlow, draw, SLIP

KILL = 0.07
EV = sorted([e for e in ALL if not e["earn"]], key=lambda e: e["day"])
same = collections.Counter(e["day"] for e in ALL)


def pol(e, F, x, h, T, bf):
    r, tm = e["rth"], e["rmin"]; O, C, A = e["O"], e["C"], e["atr"]
    if O <= bf:
        return O, "open<=bf"
    raised = max(O - F * A, bf); tgt = O + x * (C - O); rem, got, leg = 1.0, 0.0, h > 0
    for i in range(len(r)):
        op, hi, lo, cl, vo = r[i]
        floor = bf if i == 0 else raised
        if tm[i] >= T: return got + rem * op, "time"
        if lo <= floor: return got + rem * min(op, floor), "floor"
        if leg and hi > tgt:
            got += h * tgt; rem -= h; leg = False
    return got + rem * r[-1][3], "close"


params = []
for e in EV:
    hist = [q for q in EV if q["day"] < e["day"]]
    if len(hist) < 30: continue
    x = float(np.median([retr(q) for q in hist])); T = float(np.median([tlow(q) for q in hist])) + 5
    F = float(np.quantile([draw(q, T) for q in hist], 0.75))
    params.append((e, x, T, F))
print("non-earnings events", len(EV), "with >=30 prior", len(params),
      "disc", sum(p[0]["day"] < SPLIT for p in params), "test", sum(p[0]["day"] >= SPLIT for p in params))
for w in (0.10, 0.25, 0.50, 1.00):
    for nm in ("one", "panel"):
        for s in (0.0, SLIP):
            for name, f in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT)):
                vals, why = [], collections.Counter()
                for e, x, T, F in params:
                    if not f(e["day"]): continue
                    n = 1 if nm == "one" else same[e["day"]]
                    bf = e["C"] * (1 - KILL / (w * n))
                    px, k = pol(e, F, x, 0.5, T, bf)
                    why[k] += 1
                    vals.append(imp(e, px * (1 - s)) + s * e["O"] / e["C"] * 1e4)
                print(f"w={w:.2f} n={nm:5s} s={s:.4f} {name}", fmt(stats(vals)), dict(why))
