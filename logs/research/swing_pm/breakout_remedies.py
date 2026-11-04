"""Swing breakout (swing_breakout_55d_v1) remediation replay — XOM loss post-mortem 2026-11-04 (owner: every losing trade
gets a formal evaluation, remediation and optimization). Read-only. Daily SIP bars (split-adjusted), T1 via data/fetcher;
earnings dates T2 FMP /stable/earnings (names FMP returns 402 for count as 'unknown').
Base rule (live config): close > highest close of the prior 55 sessions and close > SMA200 -> buy next session's open; stop = entry -
2.5*ATR14 (fill at min(open, stop)); exit at the close when the close is below the prior 20-session low; time exit at the
30th session's close. One lot per name at a time. R = (exit - entry) / (2.5*ATR).
Candidates (chosen on 2020-23, test 2024-26 read once):
 E  skip when an earnings report falls within the next N sessions (N = 3, 5, 10); unknown dates are not skipped
 X  skip when the entry open is more than x ATR above the breakout level (x = 0.5, 1.0, 1.5)
 B  move the stop to break-even once the close is >= entry + y*ATR (y = 1, 1.5, 2)
 T  chandelier: stop = max(stop, highest close since entry - z*ATR) (z = 2.5, 3)
 combinations of the best single E, X and B/T on discovery.
Universe: the live top-50-by-dollar-volume list on 2026-10-26 (survivorship: today's leaders; stated).
Usage (server, repo root, .env exported): venv/bin/python3 logs/research/swing_pm/breakout_remedies.py"""
import itertools
import json
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config  # noqa: E402
from data.fetcher import fetch_bars_window  # noqa: E402

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
UNIV = ["MU", "NVDA", "SNDK", "AAPL", "TSLA", "META", "MSFT", "AMD", "INTC", "AMZN", "GOOGL", "AVGO", "PLTR", "MRVL",
        "LITE", "ORCL", "NBIS", "BE", "STX", "WDC", "DELL", "AMAT", "LLY", "MSTR", "LRCX", "WMT", "CRM", "NFLX", "ASML",
        "V", "BRK.B", "CRWV", "JPM", "XOM", "GEV", "PANW", "HOOD", "CAT", "CRWD", "COST", "COHR", "GS", "BAC", "CSCO",
        "NOW", "QCOM", "UNH", "KLAC"]
CACHE = os.path.join(HERE, "daily.pkl")
SECTOR = {**{k: "XLK" for k in ("MU", "NVDA", "SNDK", "AAPL", "MSFT", "AMD", "INTC", "AVGO", "MRVL", "LITE", "ORCL", "STX",
                                "WDC", "DELL", "AMAT", "LRCX", "CRM", "ASML", "CSCO", "NOW", "QCOM", "KLAC", "PLTR", "NBIS",
                                "CRWV", "COHR", "CRWD", "PANW")},
          **{k: "XLC" for k in ("META", "GOOGL", "NFLX")}, **{k: "XLY" for k in ("TSLA", "AMZN")},
          **{k: "XLP" for k in ("WMT", "COST")}, **{k: "XLF" for k in ("V", "JPM", "BRK.B", "GS", "BAC", "HOOD", "MSTR")},
          "XOM": "XLE", "LLY": "XLV", "UNH": "XLV", **{k: "XLI" for k in ("GEV", "CAT", "BE")}}
SEC_CACHE = os.path.join(HERE, "sectors.pkl")


def load_sectors():
    if os.path.exists(SEC_CACHE):
        return pickle.load(open(SEC_CACHE, "rb"))
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    out = {}
    for e in sorted(set(SECTOR.values())):
        d = fetch_bars_window(e, config.TF_DAILY, datetime(2019, 1, 1, tzinfo=timezone.utc), end, feed="sip", adjustment="split")
        out[e] = {ix.tz_convert(ET).date(): float(c) for ix, c in zip(d.index, d["close"])}
    pickle.dump(out, open(SEC_CACHE, "wb"))
    return out


SECS = {}
SPLIT = 2024


def load():
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb"))
    out = {}
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    for s in UNIV:
        d = fetch_bars_window(s, config.TF_DAILY, datetime(2019, 1, 1, tzinfo=timezone.utc), end, feed="sip", adjustment="split")
        if d is None or d.empty:
            continue
        r = requests.get("https://financialmodelingprep.com/stable/earnings", params={"symbol": s, "apikey": os.environ["FMP_API_KEY"]}, timeout=20)
        ed = sorted({x["date"] for x in r.json()} if r.status_code == 200 else set())
        out[s] = (d, ed)
        print(s, len(d), len(ed), flush=True)
    pickle.dump(out, open(CACHE, "wb"))
    return out


def trades(d, ed, N=None, X=None, B=None, T=None, S=None, F=None, sym=None):
    o, h, lo, c = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    days = [ix.tz_convert(ET).date() for ix in d.index]
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.c_[h - lo, np.abs(h - pc), np.abs(lo - pc)], axis=1)
    atr = np.array([np.nan] * 15 + [tr[i - 14:i].mean() for i in range(15, len(c))])
    sma = np.array([np.nan] * 200 + [c[i - 200:i].mean() for i in range(200, len(c))])
    eds = [datetime.strptime(x, "%Y-%m-%d").date() for x in ed] if ed else []
    out, i = [], 201
    while i < len(c) - 1:
        lvl = c[i - 55:i].max()   # live rule: highest CLOSE of the prior 55 sessions
        if not (c[i] > lvl and c[i] > sma[i] and np.isfinite(atr[i]) and atr[i] > 0):
            i += 1; continue
        e_i = i + 1; entry = o[e_i]; A = atr[i]
        if F is not None:   # freshness: prior breakout days in the last 20 sessions
            prior = sum(1 for k in range(i - 20, i) if c[k] > c[k - 55:k].max() and c[k] > sma[k])
            if prior >= F:
                i += 1; continue
        if S is not None and sym in SECTOR:   # sector ETF above its own S-session SMA on the signal day
            ser = SECS.get(SECTOR[sym], {})
            dd = [x for x in sorted(ser) if x <= days[i]][-S - 1:]
            if len(dd) == S + 1 and not ser[dd[-1]] > np.mean([ser[x] for x in dd[:-1]]):
                i += 1; continue
        if N is not None and eds:
            horizon = days[min(len(days) - 1, e_i + N)]
            if any(days[e_i] <= x <= horizon for x in eds):
                i += 1; continue
        if X is not None and entry > lvl + X * A:
            i += 1; continue
        stop = entry - 2.5 * A; risk = 2.5 * A; hi_close = entry; exit_px, j = None, e_i
        for j in range(e_i, min(len(c), e_i + 31)):
            if j > e_i and o[j] <= stop:
                exit_px = o[j]; break
            if lo[j] <= stop:
                exit_px = min(o[j], stop); break
            lo20 = lo[j - 20:j].min()
            if c[j] < lo20 or j - e_i + 1 >= 30:
                exit_px = c[j]; break
            hi_close = max(hi_close, c[j])
            if B is not None and c[j] >= entry + B * A:
                stop = max(stop, entry)
            if T is not None:
                stop = max(stop, hi_close - T * A)
        if exit_px is None:
            break
        out.append((days[e_i].year, (exit_px - entry) / risk, days[e_i]))
        i = j + 1
    return out


def stats(rs):
    v = np.array([r[1] for r in rs])
    if len(v) == 0:
        return "n=0"
    return f"n={len(v):4d} meanR={v.mean():+.3f} win%={(v > 0).mean() * 100:4.1f} medR={np.median(v):+.2f} p05={np.quantile(v, .05):+.2f}"


def run(data, **kw):
    allt = []
    for s, (d, ed) in data.items():
        allt += [(y, r, s, dd) for y, r, dd in trades(d, ed, sym=s, **kw)]
    return [(y, r) for y, r, *_ in allt if y < SPLIT], [(y, r) for y, r, *_ in allt if y >= SPLIT], allt


def main():
    data = load()
    SECS.update(load_sectors())
    grid = [("base", {})] + [(f"E N={n}", {"N": n}) for n in (3, 5, 10)] + [(f"X x={x}", {"X": x}) for x in (0.5, 1.0, 1.5)] \
        + [(f"B y={y}", {"B": y}) for y in (1, 1.5, 2)] + [(f"T z={z}", {"T": z}) for z in (2.5, 3)] \
        + [(f"S sma={n}", {"S": n}) for n in (20, 50)] + [(f"F prior>={f}", {"F": f}) for f in (2, 3, 4)]
    res = {}
    for name, kw in grid:
        d_, t_, _ = run(data, **kw)
        res[name] = (kw, d_, t_)
        print(f"{name:10s} disc {stats(d_)} | test {stats(t_)}", flush=True)
    def best(prefix):
        c = [(np.mean([r for _, r in v[1]]), k) for k, v in res.items() if k.startswith(prefix)]
        return max(c)[1]
    picks = [best("E"), best("X"), best("S"), best("F")]
    print("discovery picks:", picks)
    for r_ in range(2, 5):
        for combo in itertools.combinations(picks, r_):
            kw = {}
            for k in combo: kw.update(res[k][0])
            d_, t_, _ = run(data, **kw)
            print(f"{' + '.join(combo):30s} disc {stats(d_)} | test {stats(t_)}", flush=True)
    print("candidates tried:", len(grid) - 1 + 11, "| test years read once per candidate in this run")
    _, _, allt = run(data)
    print("XOM 2026 trades (base):", [(str(dd), round(r, 2)) for y, r, s, dd in allt if s == "XOM" and y == 2026])
    d, ed = data["XOM"]
    print("XOM last 10 closes:", [(str(ix.tz_convert(ET).date()), round(float(c), 2)) for ix, c in zip(d.index[-10:], d["close"].to_numpy()[-10:])])
    print("XOM FMP report dates (recent):", ed[-4:])
    json.dump({k: {"disc": stats(v[1]), "test": stats(v[2])} for k, v in res.items()}, open(os.path.join(HERE, "remedies_out.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
