"""Earnings branch, deterministic part only (no transcript review): target = O + x*(C-O), x = walk-forward median of
earlier earnings gaps' 5-day best bounce fraction; stop = O - F*ATR, F = walk-forward 90th pct of the drawdown before the
day-1 high; horizon 5 sessions (sell at the day-5 close). Also 'thesis-intact' (x at 75th pct) and 'broken' (25th)
as the bounds the review would choose between. Read-only."""
import numpy as np
from sim7_earn import EV, SPLIT, imp, stats


def best5(e): return (max(e["rth"][:, 1].max(), e["next"][:4, 1].max()) - e["O"]) / (e["C"] - e["O"])


def ddh(e):
    r = e["rth"]; i = int(np.argmax(r[:, 1])); return (e["O"] - r[: i + 1, 2].min()) / e["atr"]


def run(e, x, F):
    O, C, A = e["O"], e["C"], e["atr"]; tgt, floor = O + x * (C - O), O - F * A
    for op, hi, lo, _c, _v in e["rth"]:
        if lo <= floor: return min(op, floor)
        if hi >= tgt: return tgt
    for op, hi, lo, _c in e["next"][:4]:
        if op <= floor or lo <= floor: return min(op, floor)
        if hi >= tgt: return max(op, tgt)
    return e["next"][3][3]


for q in (.25, .5, .75):
    for name, g in (("disc", lambda d: d < SPLIT), ("test", lambda d: d >= SPLIT)):
        v = []
        for e in EV:
            h = [p for p in EV if p["day"] < e["day"]]
            if len(h) < 20 or not g(e["day"]): continue
            v.append(imp(e, run(e, float(np.quantile([best5(p) for p in h], q)), float(np.quantile([ddh(p) for p in h], .9)))))
        print(f"target q={q} {name}", stats(v))
