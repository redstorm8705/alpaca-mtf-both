"""sim6 rule applied to EARNINGS gaps (FMP-tagged), walk-forward parameters from earlier earnings gaps. Read-only."""
import numpy as np
from sim import EV as ALL, imp, stats, fmt, SPLIT
from sim5 import retr, tlow, draw
import sim6
EV = sorted([e for e in ALL if e["earn"]], key=lambda e: e["day"])
for w in (0.10, 0.25):
    for name, f in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT)):
        vals = []
        for e in EV:
            hist = [q for q in EV if q["day"] < e["day"]]
            if len(hist) < 30 or not f(e["day"]): continue
            x = float(np.median([retr(q) for q in hist])); T = float(np.median([tlow(q) for q in hist])) + 5
            F = float(np.quantile([draw(q, T) for q in hist], 0.75))
            px, _ = sim6.pol(e, F, x, 0.5, T, e["C"] * (1 - 0.07 / w))
            vals.append(imp(e, px))
        print(f"EARNINGS w={w} {name}", fmt(stats(vals)) if vals else "n=0")
