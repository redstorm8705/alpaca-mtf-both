"""Walk-forward dynamic W2, stricter (board 2026-11-03): history = gaps on EARLIER days only; the bounce leg fills only
when a bar trades THROUGH the level (high > level) at the level; a floor hit fills at min(bar open, floor) minus the
measured 10-29 paper stop slippage below the open (median of NOW -2.7%, NOW -4.0%, GOOGL -0.55% = 2.7%) charged on
the floor-hit leg; time-limit leg at that bar's open. Read-only replay."""
import numpy as np
from sim import EV, imp, stats, fmt, SPLIT
SLIP = float(np.median([0.027, 0.040, 0.0055]))
EV = sorted([e for e in EV if not e["earn"]], key=lambda e: e["day"])
def retr(e):
    r, tm = e["rth"], e["rmin"]; m = tm < 600
    hi = r[m][:, 1].max() if m.any() else e["O"]; g = e["C"] - e["O"]
    return (hi - e["O"]) / g if g > 0 else 0.0
def tlow(e):
    r, tm = e["rth"], e["rmin"]; m = tm < 690
    return tm[m][np.argmin(r[m][:, 2])] if m.any() else 600
def draw(e, T):
    r, tm = e["rth"], e["rmin"]; m = tm < T; mn = r[m][:, 2].min() if m.any() else e["O"]
    return (e["O"] - mn) / e["atr"]
def pol(e, F, x, h, T, slip):
    r, tm = e["rth"], e["rmin"]; O, C, A = e["O"], e["C"], e["atr"]
    floor = O - F * A; tgt = O + x * (C - O); rem, got, leg = 1.0, 0.0, h > 0
    for i in range(len(r)):
        op, hi, lo, cl, vo = r[i]
        if tm[i] >= T: return got + rem * op
        if lo <= floor: return got + rem * min(op, floor) * (1 - slip)
        if leg and hi > tgt:
            got += h * tgt; rem -= h; leg = False
    return got + rem * r[-1][3]
rows = []
for e in EV:
    hist = [h for h in EV if h["day"] < e["day"]]
    if len(hist) < 30: continue
    x = float(np.median([retr(h) for h in hist])); T = float(np.median([tlow(h) for h in hist])) + 5
    F = float(np.quantile([draw(h, T) for h in hist], 0.75))
    rows.append((e, imp(e, pol(e, F, x, 0.5, T, SLIP)), imp(e, pol(e, F, x, 0.5, T, 0.0)), imp(e, pol(e, F, x, 0.0, T, SLIP))))
for name, f in (("disc", lambda e: e["day"] < SPLIT), ("test", lambda e: e["day"] >= SPLIT)):
    sub = [r for r in rows if f(r[0])]
    print(name, "strict+slip ", fmt(stats([r[1] for r in sub])))
    print(name, "strict      ", fmt(stats([r[2] for r in sub])))
    print(name, "1-share(T)  ", fmt(stats([r[3] for r in sub])))
# book-level worst day: sum over gaps on the same day (bps of each position, equal weight)
import collections
by = collections.defaultdict(list)
for e, v, *_ in rows: by[e["day"]].append(v)
worst = sorted(((sum(v), d, len(v)) for d, v in by.items()))[:3]
print("worst days (sum of bps across positions gapping that day):", [(str(d.date()), round(s), n) for s, d, n in worst])

# Honest split (board round 2): the same slippage s on EVERY leg of both policies isolates the value of WAITING;
# the value of a marketable limit instead of a market-converted stop at the open is reported separately (= s itself).
def pol_all(e, F, x, h, T, s):
    return pol(e, F, x, h, T, 0.0) * (1 - s)
print("waiting effect, same slippage on every leg of both sides:")
for s in (0.0, 0.0055, SLIP):
    for name, f in (("disc", lambda e: e["day"] < SPLIT), ("test", lambda e: e["day"] >= SPLIT)):
        vals = []
        for e, *_ in [r for r in rows if f(r[0])]:
            hist = [h for h in EV if h["day"] < e["day"]]
            x = float(np.median([retr(h) for h in hist])); T = float(np.median([tlow(h) for h in hist])) + 5
            F = float(np.quantile([draw(h, T) for h in hist], 0.75))
            vals.append(imp(e, pol_all(e, F, x, 0.5, T, s)) + s * e["O"] / e["C"] * 1e4)
        print(f" s={s:.4f} {name}", fmt(stats(vals)))
