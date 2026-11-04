"""Exit-policy replay on gap-through events. Improvement vs A (sell at the RTH open) in bps of the prior close.
A  sell everything at the 09:30 open print (today's resting GTC stop).
B  wait-and-scale: floor = O - F*ATR (F = discovery quantile p of the post-open drawdown, ATR units);
   sell fraction h at the first bounce (price >= O + x*(C-O), or 5m close above session VWAP), rest at time T
   or the floor, whichever first. Same-bar floor+target -> floor first (conservative). 5m bars."""
import os, pickle, numpy as np, pandas as pd
SP = os.path.dirname(os.path.abspath(__file__))
EV = [e for e in pickle.load(open(SP + "/gx_events.pkl", "rb"))
      if not (e["sym"] == "GE" and str(e["day"].date()) in ("2023-01-04", "2024-04-02"))]
SPLIT = pd.Timestamp("2024-01-01")
def disc(e): return e["day"] < SPLIT

def bpol(e, F, x, h, T):
    r, tm = e["rth"], e["rmin"]; O, C, A = e["O"], e["C"], e["atr"]
    floor = O - F * A; tgt = O + x * (C - O) if x != "vwap" else None
    rem = 1.0; got = 0.0; leg = h > 0
    pv = 0.0; vv = 0.0
    for i in range(len(r)):
        op, hi, lo, cl, vo = r[i]
        if tm[i] >= T:
            return got + rem * op
        if lo <= floor:
            return got + rem * min(op, floor)
        tp = (hi + lo + cl) / 3; pv += tp * vo; vv += vo
        if leg:
            if x == "vwap":
                if i >= 1 and vv > 0 and cl > pv / vv:
                    q = min(h, rem); got += q * cl; rem -= q; leg = False
            elif hi >= tgt:
                q = min(h, rem); got += q * max(op, tgt); rem -= q; leg = False
            if rem <= 1e-9: return got
    return got + rem * r[-1][3]

def imp(e, px): return (px - e["O"]) / e["C"] * 1e4

def draw(e, T):
    r, tm = e["rth"], e["rmin"]; m = r[tm < T][:, 2].min() if (tm < T).any() else e["O"]
    return (e["O"] - m) / e["atr"]

def stats(v):
    v = np.asarray(v); return dict(n=len(v), mean=v.mean(), med=np.median(v), p05=np.quantile(v, .05), worst=v.min(),
                                   worse=(v < 0).mean(), best=v.max())
def fmt(d): return f"n={d['n']:4d} mean={d['mean']:+7.1f} med={d['med']:+6.1f} p05={d['p05']:+7.1f} worst={d['worst']:+7.1f} worse%={d['worse']*100:4.1f}"

if __name__ == "__main__":
    D = [e for e in EV if disc(e)]; Tt = [e for e in EV if not disc(e)]
    print("events", len(EV), "disc", len(D), "test", len(Tt), "earn", sum(e["earn"] for e in EV))
    Ts = {"10:00": 600, "10:30": 630, "11:00": 660, "12:00": 720, "close": 2000}
    grid = []
    for p in (0.75, 0.9, 0.95):
        for Tn, T in Ts.items():
            F = np.quantile([draw(e, T) for e in D], p)
            for x in (0.15, 0.25, 0.5, "vwap", None):
                for h in ((0.5, 1.0) if x is not None else (0.0,)):
                    grid.append((p, F, Tn, T, x if x is not None else 0, h))
    print("candidates", len(grid))
    res = []
    for g in grid:
        p, F, Tn, T, x, h = g
        res.append((g, stats([imp(e, bpol(e, F, x, h, T)) for e in D])))
    res.sort(key=lambda z: -z[1]["mean"])
    print("TOP discovery by mean:")
    for g, s in res[:8]: print(" p=%.2f F=%.2f T=%s x=%s h=%.1f" % (g[0], g[1], g[2], g[4], g[5]), fmt(s))
    byp05 = sorted(res, key=lambda z: -z[1]["p05"])[:4]
    print("TOP discovery by p05:")
    for g, s in byp05: print(" p=%.2f F=%.2f T=%s x=%s h=%.1f" % (g[0], g[1], g[2], g[4], g[5]), fmt(s))
    print("discovery mean-of-all-candidates %.1f share>0 %.2f" % (np.mean([s["mean"] for _, s in res]), np.mean([s["mean"] > 0 for _, s in res])))
    win = res[0][0]; p, F, Tn, T, x, h = win
    pickle.dump(win, open(SP + "/gx_winner.pkl", "wb"))
    print("\nTEST (one look) winner p=%.2f F=%.2f T=%s x=%s h=%.1f" % (p, F, Tn, x, h))
    for name, sub in (("all", Tt), ("k>=2.5", [e for e in Tt if e["kgap"] >= 2.5]), ("k>=4", [e for e in Tt if e["kgap"] >= 4]),
                      ("earnings", [e for e in Tt if e["earn"]]), ("non-earn", [e for e in Tt if not e["earn"]])):
        if sub: print(f" {name:9s}", fmt(stats([imp(e, bpol(e, F, x, h, T)) for e in sub])))
    w2 = byp05[0][0]
    print("TEST p05-winner p=%.2f F=%.2f T=%s x=%s h=%.1f" % (w2[0], w2[1], w2[2], w2[4], w2[5]),
          fmt(stats([imp(e, bpol(e, w2[1], w2[4], w2[5], w2[3])) for e in Tt])))
    for Tn2, T2 in Ts.items():
        print(f" TEST hold-to-{Tn2} no floor", fmt(stats([imp(e, bpol(e, 1e9, 0, 0.0, T2)) for e in Tt])))
    for y in range(2020, 2027):
        sub = [e for e in EV if e["day"].year == y]
        if sub: print(" year", y, fmt(stats([imp(e, bpol(e, F, x, h, T)) for e in sub])))
    for e in EV:
        if str(e["day"].date()) == "2026-10-29":
            px = bpol(e, F, x, h, T); print(" 10-29", e["sym"], "open", e["O"], "B exit", round(px, 2), "imp bps", round(imp(e, px), 1))
