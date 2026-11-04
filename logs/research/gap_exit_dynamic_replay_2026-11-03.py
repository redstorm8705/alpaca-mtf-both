"""Walk-forward dynamic W2 (read-only replay). For each gap event, using ONLY earlier non-earnings events:
x = median fraction of the gap retraced at the first-30-min high; T = median minute of the post-open low (first 2 h);
F = 75th pct post-open drawdown (ATR units) to T. Compare with static W2 (F=.51, x=.15, h=.5, T=10:00) and A (open)."""
import os, pickle, numpy as np, pandas as pd
from sim import EV, bpol, imp, stats, fmt, SPLIT
EV = sorted([e for e in EV if not e["earn"]], key=lambda e: e["day"])
def retr(e):
    r, tm = e["rth"], e["rmin"]; m = tm < 600
    hi = r[m][:, 1].max() if m.any() else e["O"]
    g = e["C"] - e["O"]; return (hi - e["O"]) / g if g > 0 else 0.0
def tlow(e):
    r, tm = e["rth"], e["rmin"]; m = tm < 690
    return tm[m][np.argmin(r[m][:, 2])] if m.any() else 600
def draw(e, T):
    r, tm = e["rth"], e["rmin"]; m = tm < T; mn = r[m][:, 2].min() if m.any() else e["O"]
    return (e["O"] - mn) / e["atr"]
dyn, sta, opn, params = [], [], [], []
for i, e in enumerate(EV):
    hist = EV[:i]
    if len(hist) < 30: continue
    x = float(np.median([retr(h) for h in hist])); T = float(np.median([tlow(h) for h in hist])) + 5
    F = float(np.quantile([draw(h, T) for h in hist], 0.75))
    dyn.append((e, imp(e, bpol(e, F, x, 0.5, T)))); sta.append((e, imp(e, bpol(e, 0.51, 0.15, 0.5, 600))))
    params.append((e["day"], x, T, F))
for name, f in (("disc", lambda e: e["day"] < SPLIT), ("test", lambda e: e["day"] >= SPLIT)):
    print(name, "DYN ", fmt(stats([v for e, v in dyn if f(e)])))
    print(name, "W2  ", fmt(stats([v for e, v in sta if f(e)])))
p = pd.DataFrame(params, columns=["day", "x", "T", "F"]); print(p.describe().loc[["mean", "min", "max"]].round(3).to_string())
print("last params", p.tail(1).to_string(index=False))
