"""How high do earnings gap-down bounces go? For each FMP-tagged earnings gap (gx_events_nd.pkl), the best bounce as a
fraction of the gap (C - O), measured before the price first trades below a floor:
 day1  = max(high from 09:30 to close) on the gap day
 day5  = max(high over the gap day + next 4 sessions)
and the share of gaps whose bounce reaches 25/38.2/50/61.8/100% of the gap. Also the low reached BEFORE that high
(drawdown first, in ATR units). Read-only."""
import os
import pickle
import numpy as np
EV = [e for e in pickle.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "gx_events_nd.pkl"), "rb"))
      if e["earn"] and len(e.get("next", [])) == 5]
for era, f in (("2020-23", lambda e: e["day"].year < 2024), ("2024-26", lambda e: e["day"].year >= 2024), ("all", lambda e: True)):
    sub = [e for e in EV if f(e)]
    g = np.array([e["C"] - e["O"] for e in sub])
    d1 = np.array([e["rth"][:, 1].max() - e["O"] for e in sub]) / g
    d5 = np.array([max(e["rth"][:, 1].max(), e["next"][:4, 1].max()) - e["O"] for e in sub]) / g
    c5 = np.array([e["next"][3, 3] - e["O"] for e in sub]) / g
    dd = []
    for e in sub:
        r = e["rth"]; i = int(np.argmax(r[:, 1])); dd.append((e["O"] - r[: i + 1, 2].min()) / e["atr"])
    print(f"{era} n={len(sub)} median gap {np.median(g / np.array([e['C'] for e in sub])) * 100:.1f}%")
    for nm, a in (("day-1 high", d1), ("5-day high", d5), ("day-5 close", c5)):
        print(f"  {nm}: quantiles 25/50/75 = {np.round(np.quantile(a, [.25, .5, .75]), 2)}; reach " +
              " ".join(f"{int(p * 1000) / 10}%:{(a >= p).mean() * 100:.0f}%" for p in (.25, .382, .5, .618, 1.0)))
    print(f"  drawdown before the day-1 high (ATR): median {np.median(dd):.2f}, 75th {np.quantile(dd, .75):.2f}, 90th {np.quantile(dd, .9):.2f}")
