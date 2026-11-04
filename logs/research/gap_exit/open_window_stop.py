"""Opening-window stop study (owner 2026-11-04: Swing XOM stop hit at 09:32 ET; "I hate selling within 15 mins of the open").
Question: when a long position's stop is hit in the first minutes after the open (the open itself was ABOVE the stop, so
not a gap-through), does ignoring the stop until W minutes after the open beat selling at once?
Panel: 26 names, SIP 5-min RTH bars, 2025-01-02 .. today (T1 via data/fetcher). ATR14 from daily bars before the day.
Hypothetical stop S = O - k*ATR, k in {0.25, 0.5, 1.0} (the stop sits that far under the open). Event: O > S and a bar
inside the first W minutes trades at or below S.
 A  stop at once: exit at min(bar open, S)
 B  delay W minutes: from the first bar at/after 09:30+W, if that bar's open <= S exit at that open; otherwise keep the
    stop S for the rest of the day (exit at min(bar open, S) when touched); else mark at the day's close.
 B' same as B but with a disaster floor S - k*ATR during the window (exit there if touched).
Comparison: B - A in bps of O, marked at the day's close for positions still held (same horizon for both).
Discovery 2025, test 2026 (16 names; 2024 dropped for download time). Read-only. Usage (server, repo root, .env exported): venv/bin/python3 <this file>"""
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config  # noqa: E402
from data.fetcher import fetch_bars_window  # noqa: E402

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ["XOM", "AAPL", "AMZN", "MSFT", "GOOGL", "META", "NVDA", "TSLA", "AMD", "AVGO", "NFLX", "JPM", "V", "GE", "UNH", "QQQM"]
CACHE = os.path.join(HERE, "ows_bars.pkl")
END = datetime.now(timezone.utc) - timedelta(minutes=20)


def load():
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    out = {}
    for s in NAMES:
        d = fetch_bars_window(s, config.TF_DAILY, datetime(2023, 11, 1, tzinfo=timezone.utc), END, feed="sip", adjustment="split")
        m = fetch_bars_window(s, config.TF_5M, datetime(2025, 1, 2, tzinfo=timezone.utc), END, feed="sip", adjustment="split")
        if d is None or d.empty or m is None or m.empty:
            continue
        out[s] = (d, m)
        print(s, len(d), len(m), flush=True)
    pickle.dump(out, open(CACHE, "wb"))
    return out


def main():
    data = load()
    res = {}
    for s, (d, m) in data.items():
        d = d.copy(); d.index = d.index.tz_convert(ET)
        h, lo, c = d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy()
        pc = np.r_[np.nan, c[:-1]]
        tr = np.nanmax(np.c_[h - lo, np.abs(h - pc), np.abs(lo - pc)], axis=1)
        atr = {}
        for i in range(15, len(d)):
            atr[d.index[i].date()] = float(np.mean(tr[i - 14:i]))
        m = m.copy(); m.index = m.index.tz_convert(ET)
        mins = m.index.hour * 60 + m.index.minute
        m = m[(mins >= 570) & (mins < 960)]
        for day, g in m.groupby(m.index.date):
            A = atr.get(day)
            if not A or len(g) < 20:
                continue
            arr = g[["open", "high", "low", "close"]].to_numpy(float)
            tm = np.array([t.hour * 60 + t.minute for t in g.index])
            O = arr[0][0]
            for k in (0.25, 0.5, 1.0):
                S = O - k * A
                for W in (15, 30):
                    first = [i for i in range(len(arr)) if tm[i] < 570 + W and arr[i][2] <= S]
                    if not first:
                        continue
                    i0 = first[0]
                    a = min(arr[i0][0], S)
                    vals = {}
                    for nm, floor in (("B", None), ("Bf", S - k * A)):
                        px = None
                        for i in range(i0, len(arr)):
                            if tm[i] < 570 + W:
                                if floor is not None and arr[i][2] <= floor:
                                    px = min(arr[i][0], floor); break
                                continue
                            if tm[i] >= 570 + W and px is None and (i == 0 or tm[i - 1] < 570 + W):
                                if arr[i][0] <= S:
                                    px = arr[i][0]; break
                            if arr[i][2] <= S:
                                px = min(arr[i][0], S); break
                        if px is None:
                            px = arr[-1][3]
                        vals[nm] = (px - a) / O * 1e4
                    res.setdefault((k, W), []).append((day.year, s, day, vals["B"], vals["Bf"]))
    for (k, W), rows in sorted(res.items()):
        for era, f in (("2025", lambda y: y == 2025), ("2026", lambda y: y >= 2026)):
            sub = [r for r in rows if f(r[0])]
            for j, nm in ((3, "delay"), (4, "delay+floor")):
                v = np.array([r[j] for r in sub])
                if len(v) == 0:
                    continue
                print(f"k={k} W={W} {era} {nm:11s} n={len(v):5d} mean={v.mean():+6.1f} med={np.median(v):+6.1f} "
                      f"p05={np.quantile(v, .05):+7.1f} better%={(v > 0).mean() * 100:4.1f}", flush=True)
    xs = [r for r in res.get((0.25, 15), []) if r[1] == "XOM" and str(r[2]) == "2026-11-04"]
    print("XOM 2026-11-04 (k=0.25,W=15):", [(round(r[3], 1), round(r[4], 1)) for r in xs])


if __name__ == "__main__":
    main()
