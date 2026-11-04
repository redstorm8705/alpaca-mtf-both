"""Q34 alpha vs beta for the BEAT hold-to-next-report result (sim13 S3, no stop) and S2 (3 ATR stop): subtract SPY's
return over the same holding window (SPY open on the gap day -> SPY close on the exit day), scaled by the stock's prior
gap (bps of the prior close, as sim13). Also the MISS group held the same way as a control. Read-only."""
import json, os
import numpy as np
import sim13_qhm as s13
from sim12_beat import tag
SPY = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "spy_daily.json")))
days = sorted(SPY)


def spy_ret(d0, n):
    i = days.index(d0) if d0 in days else None
    if i is None or i + n >= len(days): return None
    return SPY[days[i + n]][1] / SPY[days[i]][0] - 1


def exit_index(e, stop, n):
    seq = [tuple(b[:4]) for b in e["rth"][:, :4]]
    if stop is not None and any(lo <= stop for _, _, lo, _ in seq): return 0
    for k, b in enumerate(e["next"][:n]):
        if stop is not None and b[2] <= stop: return k + 1
    return n


rng = np.random.default_rng(0)
for grp in ("BEAT", "MISS"):
    for nm, stop_fn in (("no stop", lambda e: None), ("3 ATR stop", lambda e: e["O"] - 3 * e["atr"])):
        raw, ex = [], []
        for e in s13.ded:
            if tag(e) != grp: continue
            n = s13.next_report_idx(e)
            if n is None: continue
            st = stop_fn(e); r = s13.imp(e, s13.hold(e, st, n)); k = exit_index(e, st, n)
            sr = spy_ret(str(e["day"].date()), k)
            if sr is None: continue
            raw.append(r); ex.append(r - sr * e["O"] / e["C"] * 1e4)
        b = [np.mean(rng.choice(ex, len(ex))) for _ in range(3000)]
        print(f"{grp} {nm}: n={len(raw)} raw mean {np.mean(raw):+.0f} | minus SPY mean {np.mean(ex):+.0f} median {np.median(ex):+.0f} "
              f"CI {np.round(np.quantile(b, [.025, .975]))}")
