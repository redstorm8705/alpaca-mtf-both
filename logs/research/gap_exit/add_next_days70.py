"""Add the next 70 sessions' daily OHLC (SIP, split-adjusted) to each event in gx_events.pkl -> gx_events_nd70.pkl.
Read-only research for the earnings dead-cat-bounce variants. Usage (server, repo root, .env exported):
venv/bin/python3 logs/research/gap_exit/add_next_days.py"""
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
import config  # noqa: E402
from data.fetcher import fetch_bars_window  # noqa: E402

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
EV = pickle.load(open(os.path.join(HERE, "gx_events.pkl"), "rb"))
START = datetime(2020, 9, 1, tzinfo=timezone.utc)
END = datetime.now(timezone.utc) - timedelta(minutes=20)
for sym in sorted({e["sym"] for e in EV}):
    d = fetch_bars_window(sym, config.TF_DAILY, START, END, feed="sip", adjustment="split")
    if d is None or d.empty:
        continue
    days = [ix.tz_convert(ET).date() for ix in d.index]
    arr = d[["open", "high", "low", "close"]].to_numpy(float)
    pos = {dd: i for i, dd in enumerate(days)}
    for e in EV:
        if e["sym"] != sym:
            continue
        i = pos.get(e["day"].date())
        e["next"] = arr[i + 1:i + 71] if i is not None else arr[:0]
    print(sym, flush=True)
pickle.dump(EV, open(os.path.join(HERE, "gx_events_nd70.pkl"), "wb"))
print("done", sum(len(e.get("next", [])) >= 5 for e in EV), "of", len(EV))
