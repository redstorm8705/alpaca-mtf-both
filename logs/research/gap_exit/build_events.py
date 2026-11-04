"""Rebuild the gap-through event panel for the GAP-EXIT-1029 replays (the 2026-10-29 panel lived in a temp folder that
was cleared). Read-only research. Data: T1 Alpaca SIP via data/fetcher.fetch_bars_window (split-adjusted daily bars for
ATR14 / prior close / open; 5-min bars 04:00-16:00 ET on each event day); T2 FMP earnings history.
Event: RTH open O < prior close C - k*ATR14(prior), k >= 1. Earnings tag: the event day is the report date or the next
session after it (timing unknown -> both). Output: logs/research/gap_exit/gx_events.pkl (list of dicts).
Usage (server, repo root): venv/bin/python3 logs/research/gap_exit/build_events.py"""
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
from data.fmp_client import _fetch_symbol_earnings  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gx_events.pkl")
NAMES = list(config.DAY_TIER_UNIVERSE) + ["NOW", "GE", "XOM", "JPM", "LLY", "CRWD", "UNH", "ORCL"]
START = datetime(2020, 9, 1, tzinfo=timezone.utc)
END = datetime.now(timezone.utc) - timedelta(minutes=20)   # SIP plan: no last-15-min data
EXCLUDE = {("GE", "2023-01-04"), ("GE", "2024-04-02")}   # spin-off days


def main() -> None:
    events = []
    for sym in NAMES:
        d = fetch_bars_window(sym, config.TF_DAILY, START, END, feed="sip", adjustment="split")
        if d is None or d.empty or len(d) < 30:
            print(sym, "no daily bars", flush=True)
            continue
        d = d.copy()
        d.index = d.index.tz_convert(ET)
        h, lo, c = d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy()
        pc = np.r_[np.nan, c[:-1]]
        tr = np.nanmax(np.c_[h - lo, np.abs(h - pc), np.abs(lo - pc)], axis=1)
        atr_full = np.full(len(tr), np.nan)
        if len(tr) > 14:
            atr_full[14:] = np.convolve(tr, np.ones(14) / 14, "valid")[:-1]   # ATR of the 14 sessions before
        st, er = _fetch_symbol_earnings(sym, limit=80)
        rep = sorted(set(er))
        sessions = [ix.date() for ix in d.index]
        earn_days = set()
        for r in rep:
            nxt = [s for s in sessions if s >= r][:2]
            if nxt and nxt[0] == r:
                earn_days.update(nxt)
            elif nxt:
                earn_days.add(nxt[0])
        n_ev = 0
        for i in range(15, len(d)):
            A, C, op0 = atr_full[i], c[i - 1], d["open"].iat[i]
            if not np.isfinite(A) or A <= 0 or not op0 < C - A:
                continue
            day = sessions[i]
            if (sym, str(day)) in EXCLUDE:
                continue
            s0 = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET)
            b = fetch_bars_window(sym, config.TF_5M, s0, s0 + timedelta(hours=12), feed="sip", adjustment="split")
            if b is None or b.empty:
                continue
            b = b.copy()
            b.index = b.index.tz_convert(ET)
            mins = np.array([t.hour * 60 + t.minute for t in b.index])
            rth = mins >= 570
            pre = mins < 570
            if not rth.any():
                continue
            arr = b[["open", "high", "low", "close", "volume"]].to_numpy(float)
            events.append(dict(sym=sym, day=__import__("pandas").Timestamp(day), O=float(arr[rth][0][0]), C=float(C),
                               atr=float(A), kgap=float((C - arr[rth][0][0]) / A), earn=day in earn_days,
                               earn_status=st,
                               rth=arr[rth], rmin=mins[rth],
                               pre_last=float(arr[pre][-1][3]) if pre.any() else float("nan")))
            n_ev += 1
        print(sym, "daily", len(d), "events", n_ev, "earn_http", st, "reports", len(rep), flush=True)
    pickle.dump(events, open(OUT, "wb"))
    print("total", len(events), "earn", sum(e["earn"] for e in events), "->", OUT)


if __name__ == "__main__":
    main()
