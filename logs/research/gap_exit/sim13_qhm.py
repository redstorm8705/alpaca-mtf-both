"""Codex round-1 asks: (5) replay the actual treatment for BEAT gaps — hold to the session before the NEXT report (QHM
holding period) with a stop — not a 5-session hold; (6) one event per report (first gapped session within the report
date .. next session; duplicates dropped); (7) CIs clustered by date AND by ticker. Stop proxies (QHM's own dip-zone
stop is not replayed here; flagged): S1 O - F*ATR (F = walk-forward 90th pct drawdown-before-high), S2 O - 3*ATR,
S3 none. Fill at min(open, stop) on the daily bar. Read-only. Baseline = sell at the open (0)."""
import json, os, pickle
from datetime import timedelta
import numpy as np
from sim12_beat import tag
HERE = os.path.dirname(os.path.abspath(__file__))
S = json.load(open(os.path.join(HERE, "surprises.json")))
EV = sorted([e for e in pickle.load(open(os.path.join(HERE, "gx_events_nd70.pkl"), "rb")) if e["earn"] and len(e.get("next", [])) >= 5],
            key=lambda e: (e["sym"], e["day"]))
ded, last = [], {}
for e in EV:   # one event per report: drop a second gapped session within 4 days of the previous one for the same name
    p = last.get(e["sym"])
    if p is not None and (e["day"] - p).days <= 4: continue
    ded.append(e); last[e["sym"]] = e["day"]
print("events", len(EV), "after one-per-report", len(ded))


def next_report_idx(e):
    rows = S.get(e["sym"])
    if not isinstance(rows, list): return None
    d = e["day"].date(); later = sorted(r["date"] for r in rows if r.get("date") and r["date"] > str(d + timedelta(days=5)))
    if not later: return None
    nd = later[0]; days = (np.datetime64(nd) - np.datetime64(str(d))).astype(int)
    return max(1, min(len(e["next"]), int(days * 252 / 365) - 1))   # sessions until the day before the next report


def ddh(e):
    r = e["rth"]; i = int(np.argmax(r[:, 1])); return (e["O"] - r[: i + 1, 2].min()) / e["atr"]


def hold(e, stop, n):
    for op, hi, lo, cl in [tuple(b[:4]) for b in e["rth"][:, :4]] + [tuple(b) for b in e["next"][:n]]:
        if stop is not None and lo <= stop: return min(op, stop)
    return e["next"][n - 1][3]


def imp(e, px): return (px - e["O"]) / e["C"] * 1e4


rows = []
for e in ded:
    if tag(e) != "BEAT": continue
    n = next_report_idx(e)
    if n is None or n > len(e["next"]): continue
    h = [p for p in ded if p["day"] < e["day"]]
    F = float(np.quantile([ddh(p) for p in h], .9)) if len(h) >= 20 else None
    rows.append(dict(e=e, n=n, S1=imp(e, hold(e, e["O"] - F * e["atr"], n)) if F else None,
                     S2=imp(e, hold(e, e["O"] - 3 * e["atr"], n)), S3=imp(e, hold(e, None, n)), H5=imp(e, e["next"][3][3])))
rng = np.random.default_rng(0)


def ci(vals, keys):
    D = {}
    for v, k in zip(vals, keys): D.setdefault(k, []).append(v)
    ks = list(D); b = [np.mean([x for i in rng.choice(len(ks), len(ks)) for x in D[ks[i]]]) for _ in range(3000)]
    return np.round(np.quantile(b, [.025, .975]))


for era, f in (("2020-23", lambda d: d.year < 2024), ("2024-26", lambda d: d.year >= 2024), ("all", lambda d: True)):
    sub = [r for r in rows if f(r["e"]["day"])]
    print(f"{era} BEAT n={len(sub)} median hold {np.median([r['n'] for r in sub]):.0f} sessions")
    for k in ("H5", "S1", "S2", "S3"):
        v = [(r[k], r["e"]) for r in sub if r[k] is not None]
        vals = [x for x, _ in v]
        if not vals: continue
        print(f"  {k}: n={len(vals)} mean={np.mean(vals):+7.1f} med={np.median(vals):+7.1f} p05={np.quantile(vals, .05):+7.1f} "
              f"worse%={np.mean(np.array(vals) < 0) * 100:4.1f} CI-date {ci(vals, [e['day'] for _, e in v])} CI-ticker {ci(vals, [e['sym'] for _, e in v])}")
