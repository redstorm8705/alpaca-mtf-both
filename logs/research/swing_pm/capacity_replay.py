"""Capacity-constrained dollar replay of the Swing breakout rule and its candidate filters (board López de Prado,
LOSS-PM-1104 1b item 3). Read-only; uses the cached daily bars and FMP dates of breakout_remedies.py (daily.pkl,
sectors.pkl). Survivorship: today's top-50 list (point-in-time membership not available here; stated).
Portfolio rules (live, approximated): 10 slots; each lot risks r = 1.48% of current equity at a 2.5-ATR stop (the live
lot's r from the replay prior); per-lot notional <= 20% of equity... not binding at this r for most names, so it is
applied as min(risk size, 0.6 x equity notional); total Swing notional <= 1.25 x equity (the 1.9x K ceiling less the
~0.65x QHM/F6 book today). When more setups qualify than room allows, they are taken in order of 60-day dollar volume
(the live universe rank). Entry next open; same exits as breakout_remedies.trades. Equity compounds daily on closes.
Reports CAGR, max drawdown, trades and final equity per candidate, discovery 2020-23 and test 2024-26 separately
(each period restarts at $2,500)."""
import os
import pickle
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import breakout_remedies as br  # noqa: E402

R_FRAC, SLOTS, NOTIONAL_CAP, LOT_CAP = 0.0148, 10, 1.25, 0.6


def prep(data):
    P = {}
    for s, (d, ed) in data.items():
        o, h, lo, c, v = (d[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
        days = [ix.tz_convert(br.ET).date() for ix in d.index]
        pc = np.r_[np.nan, c[:-1]]
        tr = np.nanmax(np.c_[h - lo, np.abs(h - pc), np.abs(lo - pc)], axis=1)
        atr = np.array([np.nan] * 15 + [tr[i - 14:i].mean() for i in range(15, len(c))])
        sma = np.array([np.nan] * 200 + [c[i - 200:i].mean() for i in range(200, len(c))])
        dv = np.array([np.nan] * 60 + [np.median((c * v)[i - 60:i]) for i in range(60, len(c))])
        eds = [date.fromisoformat(x) for x in ed] if ed else []
        P[s] = dict(o=o, h=h, lo=lo, c=c, days=days, atr=atr, sma=sma, dv=dv, eds=eds, ix={dd: i for i, dd in enumerate(days)})
    return P


def qualifies(p, i, sym, N, F, S):
    c, sma, atr = p["c"], p["sma"], p["atr"]
    if i < 201 or not (c[i] > c[i - 55:i].max() and c[i] > sma[i] and np.isfinite(atr[i]) and atr[i] > 0):
        return False
    if F is not None and sum(1 for k in range(i - 20, i) if c[k] > c[k - 55:k].max() and c[k] > sma[k]) >= F:
        return False
    if N is not None and p["eds"] and i + 1 < len(c):
        hz = p["days"][min(len(c) - 1, i + 1 + N)]
        if any(p["days"][i + 1] <= x <= hz for x in p["eds"]):
            return False
    if S is not None and sym in br.SECTOR:
        ser = br.SECS.get(br.SECTOR[sym], {})
        dd = [x for x in sorted(ser) if x <= p["days"][i]][-S - 1:]
        if len(dd) == S + 1 and not ser[dd[-1]] > np.mean([ser[x] for x in dd[:-1]]):
            return False
    return True


def run(P, start, end, N=None, F=None, S=None):
    cal = sorted({d for p in P.values() for d in p["days"] if start <= d <= end})
    eq, peak, mdd, trades = 2500.0, 2500.0, 0.0, []
    pos = {}   # sym -> dict(qty, entry, stop, e_i)
    pending = []
    for d in cal:
        # 1. entries at today's open from yesterday's signals
        for sym in pending:
            p = P[sym]; i = p["ix"].get(d)
            if i is None or sym in pos or len(pos) >= SLOTS:
                continue
            A = p["atr"][i - 1]; entry = p["o"][i]
            notional_now = sum(x["qty"] * P[s]["c"][P[s]["ix"].get(d, x["e_i"]) - 1] for s, x in pos.items())
            qty = int(min(R_FRAC * eq / (2.5 * A), LOT_CAP * eq / entry, max(0.0, NOTIONAL_CAP * eq - notional_now) / entry))
            if qty < 1:
                continue
            pos[sym] = dict(qty=qty, entry=entry, stop=entry - 2.5 * A, e_i=i)
        pending = []
        # 2. exits and marks
        for sym in list(pos):
            p = P[sym]; i = p["ix"].get(d)
            if i is None:
                continue
            x = pos[sym]; px = None
            if i > x["e_i"] and p["o"][i] <= x["stop"]:
                px = p["o"][i]
            elif p["lo"][i] <= x["stop"]:
                px = min(p["o"][i], x["stop"])
            elif p["c"][i] < p["lo"][i - 20:i].min() or i - x["e_i"] + 1 >= 30:
                px = p["c"][i]
            if px is not None:
                pnl = (px - x["entry"]) * x["qty"]; eq += pnl
                trades.append(pnl); del pos[sym]
        mark = eq + sum((P[s]["c"][P[s]["ix"][d]] - x["entry"]) * x["qty"] for s, x in pos.items() if d in P[s]["ix"])
        peak = max(peak, mark); mdd = max(mdd, 1 - mark / peak)
        # 3. today's signals, ranked by 60-day dollar volume
        cands = []
        for sym, p in P.items():
            i = p["ix"].get(d)
            if i is not None and sym not in pos and qualifies(p, i, sym, N, F, S):
                cands.append((-(p["dv"][i] if np.isfinite(p["dv"][i]) else 0), sym))
        pending = [s for _, s in sorted(cands)]
    yrs = max(1e-9, (cal[-1] - cal[0]).days / 365.25)
    return dict(final=eq, cagr=(eq / 2500) ** (1 / yrs) - 1, mdd=mdd, n=len(trades),
                win=np.mean([t > 0 for t in trades]) if trades else 0)


def main():
    data = pickle.load(open(br.CACHE, "rb")); br.SECS.update(pickle.load(open(br.SEC_CACHE, "rb")))
    P = prep(data)
    cands = [("live rule", {}), ("earnings skip 10", {"N": 10}), ("late-breakout skip >=4", {"F": 4}),
             ("sector 50d", {"S": 50}), ("all three", {"N": 10, "F": 4, "S": 50})]
    for name, kw in cands:
        a = run(P, date(2020, 1, 1), date(2023, 12, 31), **kw)
        b = run(P, date(2024, 1, 1), date(2026, 11, 4), **kw)
        print(f"{name:24s} 2020-23: final ${a['final']:,.0f} CAGR {a['cagr']*100:5.1f}% maxDD {a['mdd']*100:4.1f}% n={a['n']} win {a['win']*100:4.1f}% | "
              f"2024-26: final ${b['final']:,.0f} CAGR {b['cagr']*100:5.1f}% maxDD {b['mdd']*100:4.1f}% n={b['n']} win {b['win']*100:4.1f}%", flush=True)


if __name__ == "__main__":
    main()
