"""Track A stop-placement replay v2 (read-only research) — board Harris changes applied (2026-10-26):
  * fills: walk starts the minute AFTER the entry minute; a target fills only on a trade-through (>= $0.01 beyond);
    a stop fills at the worse of the stop and the bar's open (gaps through the stop are charged);
  * dollar risk never rises: lot qty0 (OCO lines log the half-lot: total = 2 x logged when a target was logged), budget
    = qty0 x logged stop distance; widened stop distance = min(max(logged, k x ATR5), budget / 1 share); shares =
    floor(budget / new distance) >= 1. Results in DOLLARS and in R of the budget;
  * noise yardstick ATR5 = median over the prior 5 sessions of the 5-minute ATR in the entry's time-of-day window
    (entry time -30 .. +30 minutes, ET);
  * exits: BASE = half at the logged target (2R when none), half rides; RANGE = whole lot at 0.35 x 20-day median
    daily range; all flatten 15:58 ET.
Walk-forward: k chosen on the first half of trades by time, scored on the second half."""
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from data.fetcher import fetch_bars_window  # noqa: E402

ET = ZoneInfo("America/New_York")
TICK = 0.01
trades = sorted([t for t in json.load(open(sys.argv[1])) if t["track"] == "A"], key=lambda t: t["ts"])


def atr_tod(sym, e):
    vals = []
    d = e.date()
    back = 0
    while len(vals) < 5 and back < 12:
        back += 1
        dd = d - timedelta(days=back)
        if dd.weekday() >= 5:
            continue
        lo = datetime(dd.year, dd.month, dd.day, e.hour, e.minute, tzinfo=ET) - timedelta(minutes=30)
        hi = lo + timedelta(minutes=60)
        b = fetch_bars_window(sym, "5Min", lo, hi, feed="sip")
        if b is None or len(b) < 4:
            continue
        prev = b["close"].shift(1)
        tr = (b["high"] - b["low"]).combine((b["high"] - prev).abs(), max).combine((b["low"] - prev).abs(), max)
        vals.append(float(tr.dropna().mean()))
    return statistics.median(vals) if vals else None


def walk(m, side, stop, target):
    sgn = 1 if side == "long" else -1
    for _, b in m.iterrows():
        o, hi, lo = float(b["open"]), float(b["high"]), float(b["low"])
        adverse, favour = (lo, hi) if side == "long" else (hi, lo)
        if (o - stop) * sgn <= 0:                      # opened through the stop: fill at the open (gap)
            return o
        if (adverse - stop) * sgn <= 0:
            return stop
        if target is not None and (favour - target) * sgn >= TICK:
            return target
    return float(m["close"].iloc[-1])


rows = []
for t in trades:
    e = datetime.strptime(t["ts"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(ET)
    day = e.date()
    start = (e + timedelta(minutes=1)).replace(second=0, microsecond=0)
    m = fetch_bars_window(t["symbol"], "1Min", start, datetime(day.year, day.month, day.day, 15, 58, tzinfo=ET), feed="sip")
    dd = fetch_bars_window(t["symbol"], "1Day", e - timedelta(days=40), datetime(day.year, day.month, day.day, tzinfo=ET) - timedelta(minutes=1), feed="sip")
    if dd is not None and len(dd):
        dd = dd[[ix.tz_convert(ET).date() < day for ix in dd.index]]   # no look-ahead: prior sessions only
    a5 = atr_tod(t["symbol"], e)
    if m is None or m.empty or not a5 or dd is None or len(dd) < 10:
        continue
    m.index = m.index.tz_convert(ET)
    sd0 = abs(t["fill"] - t["stop"])
    qty0 = t["qty"] * (2 if t["target"] else 1)
    rows.append({"t": t, "m": m, "a5": a5, "sd0": sd0, "qty0": qty0, "budget": qty0 * sd0,
                 "rng": statistics.median((dd.tail(20)["high"] - dd.tail(20)["low"]).tolist())})


KEEP = {"on": False, "cap": None}


def run(row, k=None, range_tgt=False):
    """Dollar P&L of the lot under the rule."""
    t, m = row["t"], row["m"]
    sgn = 1 if t["side"] == "long" else -1
    if KEEP["on"] and k is not None:
        sd = max(row["sd0"], k * row["a5"])
        qty = row["qty0"]
        if KEEP["cap"] is not None:
            qty = max(1, min(qty, math.floor(KEEP["cap"] / sd + 1e-9)))
    else:
        sd = row["sd0"] if k is None else min(max(row["sd0"], k * row["a5"]), row["budget"])
        qty = row["qty0"] if k is None else max(1, math.floor(row["budget"] / sd + 1e-9))
    stop = t["fill"] - sgn * sd
    if range_tgt:
        px = walk(m, t["side"], stop, t["fill"] + sgn * 0.35 * row["rng"])
        return (px - t["fill"]) * sgn * qty
    tgt = t["target"] if (k is None and t["target"]) else t["fill"] + sgn * 2 * sd
    half = qty // 2
    a = walk(m, t["side"], stop, tgt)
    b = walk(m, t["side"], stop, None)
    return ((a - t["fill"]) * half + (b - t["fill"]) * (qty - half)) * sgn


import os
if os.environ.get("KEEP"):
    KEEP["on"] = True
    KEEP["cap"] = float(os.environ["KEEP"]) if os.environ["KEEP"] != "nocap" else None
    for k in (4, 6):
        risks = sorted(max(1, (r["qty0"] if KEEP["cap"] is None else min(r["qty0"], math.floor(KEEP["cap"] / max(r["sd0"], k * r["a5"]) + 1e-9)))) * max(r["sd0"], k * r["a5"]) for r in rows)
        base = sorted(r["budget"] for r in rows)
        print(f"k={k} dollar risk per trade: median ${statistics.median(risks):.2f} max ${risks[-1]:.2f} (today median ${statistics.median(base):.2f} max ${base[-1]:.2f})")
ratios = [r["sd0"] / r["a5"] for r in rows]
print(f"trades {len(rows)} | logged stop / time-of-day 5-min ATR (5-session median): median {statistics.median(ratios):.2f}; "
      f"under 2x: {sum(1 for x in ratios if x < 2)}; one-share lots: {sum(1 for r in rows if r['qty0'] == 1)}")
half = len(rows) // 2
train, test = rows[:half], rows[half:]
cands = [("BASE", None, False), ("BASE+RANGE", None, True)] + \
    [(f"WIDE({k})", k, False) for k in (3, 4, 6)] + [(f"WIDE({k})+RANGE", k, True) for k in (3, 4, 6)]
res = []
for name, k, rt in cands:
    pnl = [run(r, k, rt) for r in rows]
    res.append((name, sum(pnl[:half]), sum(pnl[half:]), sum(pnl), sum(1 for x in pnl if x > 0),
                sum(p / r["budget"] for p, r in zip(pnl, rows))))
best = max(res[2:], key=lambda x: x[1])
for name, k, rt in [("BASE", None, False), ("WIDE(4)+RANGE", 4, True), ("WIDE(6)+RANGE", 6, True)]:
    pnl = sorted(run(r, k, rt) for r in rows)
    print(f"  {name:15s} worst ${pnl[0]:.2f}, median ${statistics.median(pnl):.2f}, total ${sum(pnl):.2f}")
print(f"{'policy':16s} {'train $':>9s} {'test $':>9s} {'all $':>9s} {'wins':>5s} {'all R':>7s}")
for name, a, b, c, w, rr in res:
    print(f"{name:16s} {a:9.2f} {b:9.2f} {c:9.2f} {w:5d} {rr:7.2f}" + ("  <- best on train" if name == best[0] else ""))
print(f"\nwalk-forward: BASE test ${res[0][2]:.2f} vs {best[0]} test ${best[2]:.2f} (chosen on train only). "
      f"Dollar risk per trade never above the logged budget; one-share lots keep their logged stop.")
