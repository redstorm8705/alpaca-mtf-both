"""Read-only replay of CONV-1018 with the owner's "6% total" budget on the completed Day-tier trades.
Only trades in the proven conviction band are taken. Total open Day risk (stop distance + gap allowance) <= 6% equity;
a trade gets whatever budget is left when it enters. Variants on the real 1-minute IEX path:
  A  actual record (all bands, today's sizing)
  B  proven band only, today's sizing
  C  proven band, 6% budget, today's stop
  D  proven band, 6% budget, tight stop = max(0.5 x stop, 1.5 x 5-min ATR, 2 x entry spread)
  E  D + scale-out: 1/3 at +1R, 1/3 at +2R (R of the tight stop), stop to breakeven after the first partial,
     the rest exits where the real trade exited
Bands: in-sample terciles (fixed) and walk-forward (terciles + band mean R from earlier trades only)."""
import json
import math
import os
import statistics as st
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, "/home/ubuntu/mtf-bot")
from dotenv import load_dotenv  # noqa: E402

load_dotenv("/home/ubuntu/mtf-bot/.env")
exec(open("/tmp/rt/conviction_replay.py").read().split('print(f"trades with entry')[0])   # builds `rows`, `ev`
KEY, SEC = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
BUDGET = float(os.environ.get("BUDGET", "0.06"))
REALIZED = os.environ.get("REALIZED", "0") == "1"


def get(path, **q):
    url = f"https://data.alpaca.markets/v2/stocks/{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


rows = [r for r in rows if isinstance(r["conv"], (int, float)) and r["stop_dist"] > 0]
for r in rows:
    r["R"] = r["per_sh"] / r["stop_dist"]
    r["sign"] = 1 if r["side"] == "long" else -1
    r["t0"] = ts(ev[r["tid"]]["entry_fill"]["ts"])
    r["t1"] = ts(ev[r["tid"]]["exits"][-1]["ts"])
    r["exit_px"] = r["entry"] + r["sign"] * r["per_sh"]
rows.sort(key=lambda r: r["t0"])

# gap allowance: measured overshoot past the stop on real stop-outs, as a fraction of stop distance (worst seen)
over = [max(0.0, -r["R"] - 1.0) for r in rows if r["R"] < -0.9]
GAP = max(over) if over else 0.0
so = [r for r in rows if r["R"] < -0.9]
print("stop-outs: stop distance $, overshoot $, overshoot/stop")
for r in sorted(so, key=lambda r: r["stop_dist"]):
    o = max(0.0, -r["per_sh"] - r["stop_dist"])
    print(f"  {r['sym']:<5} stop {r['stop_dist']:7.3f}  overshoot {o:6.3f}  ratio {o / r['stop_dist']:.2f}")
xs = [r["stop_dist"] for r in so]; ys = [max(0.0, -r["per_sh"] - r["stop_dist"]) for r in so]
if len(xs) > 2:
    print(f"  corr(stop $, overshoot $) = {st.correlation(xs, ys):+.2f}; corr(stop $, ratio) = "
          f"{st.correlation(xs, [y / x for x, y in zip(xs, ys)]):+.2f}")
GAP_MODE = os.environ.get("GAP_MODE", "ratio")
print(f"trades {len(rows)}; stop-outs {len(over)}; worst overshoot past stop {GAP:.2f} x stop distance (gap allowance)")

for r in rows:
    try:
        b1 = get(f"{r['sym']}/bars", timeframe="1Min", start=(r["t0"] - timedelta(minutes=1)).isoformat(),
                 end=(r["t1"] + timedelta(minutes=1)).isoformat(), limit=10000, feed="iex").get("bars") or []
        b5 = get(f"{r['sym']}/bars", timeframe="5Min", start=(r["t0"] - timedelta(hours=6)).isoformat(),
                 end=r["t0"].isoformat(), limit=200, feed="iex").get("bars") or []
        qt = get(f"{r['sym']}/quotes", start=(r["t0"] - timedelta(minutes=5)).isoformat(), end=r["t0"].isoformat(),
                 limit=1000, feed="iex").get("quotes") or []
    except Exception as e:
        print("data failed", r["sym"], e)
        b1, b5, qt = [], [], []
    r["path"] = [b for b in b1 if ts(b["t"]) > r["t0"] and ts(b["t"]) <= r["t1"]]
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(b5, b5[1:])][-14:]
    r["atr5"] = st.mean(trs) if trs else 0.0
    sp = [q["ap"] - q["bp"] for q in qt if q.get("ap") and q.get("bp") and q["ap"] > q["bp"]]
    r["spread"] = st.median(sp) if sp else 0.0
    r["tight_spr"] = min(max(0.5 * r["stop_dist"], 1.5 * r["atr5"], 2 * r["spread"]), r["stop_dist"])
    r["tight"] = max(0.5 * r["stop_dist"], 1.5 * r["atr5"])
    r["tight"] = min(r["tight"], r["stop_dist"])          # never wider than today's stop


def sim(r, d, scale):
    if isinstance(scale, dict):
        return sim2(r, d, **scale)
    """Per-share outcome list [(fraction, per_share)] on the 1-minute path; stop checked before targets in a bar."""
    s, e = r["sign"], r["entry"]
    stop = e - s * d
    legs, left, be = [], 1.0, False
    tg = [(e + s * d, 1 / 3), (e + s * 2 * d, 1 / 3)] if scale else []
    for b in r["path"]:
        if (b["l"] <= stop) if s == 1 else (b["h"] >= stop):
            legs.append((left, s * (stop - e)))
            return legs
        while tg and ((b["h"] >= tg[0][0]) if s == 1 else (b["l"] <= tg[0][0])):
            px, f = tg.pop(0)
            legs.append((f, s * (px - e)))
            left -= f
            if not be:
                stop, be = e, True
    legs.append((left, s * (r["exit_px"] - e)))
    return legs


def sim2(r, d, tg=(), be=False, trail=None):
    s, e = r["sign"], r["entry"]
    stop = e - s * d
    legs, left = [], 1.0
    tg = [(e + s * k * d, f) for k, f in tg]
    hit_any = False
    best = e
    for b in r["path"]:
        if (b["l"] <= stop) if s == 1 else (b["h"] >= stop):
            legs.append((left, s * (stop - e)))
            return legs
        while tg and ((b["h"] >= tg[0][0]) if s == 1 else (b["l"] <= tg[0][0])):
            px, f = tg.pop(0)
            legs.append((f, s * (px - e)))
            left -= f
            hit_any = True
            if be:
                stop = e if s == 1 and stop < e or s == -1 and stop > e else stop
        best = max(best, b["h"]) if s == 1 else min(best, b["l"])
        if trail is not None and hit_any and not tg:
            ts_ = best - s * trail * d
            stop = max(stop, ts_) if s == 1 else min(stop, ts_)
    legs.append((left, s * (r["exit_px"] - e)))
    return legs


def gap_of(r, d):
    if GAP_MODE == "dollar":
        return GAP * r["stop_dist"]
    if GAP_MODE == "dollar_atr":
        return max(GAP * r["stop_dist"], 1.5 * r["atr5"])
    return GAP * d


def run(label, take, size_mode, stop_mode, scale):
    open_pos, tot, by_day, worst_t, n, kill = [], 0.0, defaultdict(float), 0.0, 0, 0
    day_real = defaultdict(float)
    for r in rows:
        if not take(r):
            continue
        open_pos = [p for p in open_pos if p["t1"] > r["t0"]]
        d = {"today": r["stop_dist"], "tight": r["tight"], "tight_spr": r["tight_spr"]}[stop_mode]
        if size_mode == "today":
            q = r["qty"]
        else:
            used = sum(p["risk"] for p in open_pos)
            gap_ps = gap_of(r, d)
            room = BUDGET * r["eq"] - used
            if REALIZED:
                room = min(room, 0.07 * r["eq"] + min(0.0, day_real[r["day"]]) - used)
            q = math.floor(room / (d + gap_ps)) if room > 0 else 0
            q = min(q, math.floor(4 * r["eq"] / r["entry"]))
            if q < 1:
                continue
        same_stop = d >= r["stop_dist"] - 1e-9
        legs = sim(r, d, scale) if (scale or not same_stop) else [(1.0, r["per_sh"])]
        pnl = sum(f * x for f, x in legs) * q
        open_pos.append({"t1": r["t1"], "risk": q * (d + gap_of(r, d))})
        day_real[r["day"]] += pnl
        tot += pnl
        n += 1
        by_day[r["day"]] += pnl
        worst_t = min(worst_t, pnl / r["eq"])
    wd = min(by_day.items(), key=lambda kv: kv[1]) if by_day else ("-", 0.0)
    kill = sum(1 for v in by_day.values() if v / rows[0]["eq"] <= -0.07)
    print(f"  {label:<58} n {n:>2}  P&L {tot:+9.2f}  worst trade {worst_t:+6.2%}  worst day {wd[0]} {wd[1]:+8.2f} "
          f"({wd[1] / rows[0]['eq']:+.2%})  kill-days {kill}")
    return tot


conv = sorted(r["conv"] for r in rows)
lo_c, hi_c = conv[len(conv) // 3], conv[2 * len(conv) // 3]
print(f"in-sample bands: low < {lo_c:.2f} <= middle < {hi_c:.2f} <= top")
fixed_mid = lambda r: lo_c <= r["conv"] <= hi_c


def wf_take(r):
    prior = [p for p in rows if p["t1"] < r["t0"]]
    if len(prior) < 6:
        return False
    c = sorted(p["conv"] for p in prior)
    a, b = c[len(c) // 3], c[2 * len(c) // 3]
    band = lambda x: 0 if x < a else (1 if x <= b else 2)
    same = [p["R"] for p in prior if band(p["conv"]) == band(r["conv"])]
    return len(same) >= 2 and st.mean(same) > 0


V = [("today stop, no scale-out", "today", False),
     ("tight stop (0.5x / 1.5 ATR, no spread term), no scale-out", "tight", False),
     ("tight stop WITH IEX spread term, no scale-out", "tight_spr", False),
     ("tight, 1/3 @1R + 1/3 @2R, breakeven (as tested)", "tight", {"tg": ((1, 1/3), (2, 1/3)), "be": True}),
     ("tight, 1/3 @1R + 1/3 @2R, no breakeven", "tight", {"tg": ((1, 1/3), (2, 1/3))}),
     ("tight, 1/3 @2R + 1/3 @3R, no breakeven (GAI)", "tight", {"tg": ((2, 1/3), (3, 1/3))}),
     ("tight, 1/3 @1.5R + 1/3 @3R, trail 0.5R (Gro)", "tight", {"tg": ((1.5, 1/3), (3, 1/3)), "trail": 0.5}),
     ("tight, 1/2 @2R, rest rides", "tight", {"tg": ((2, 1/2),)}),
     ("today stop, 1/3 @2R + 1/3 @3R, no breakeven", "today", {"tg": ((2, 1/3), (3, 1/3))})]
for name, take in (("IN-SAMPLE band", fixed_mid), ("WALK-FORWARD band", wf_take)):
    print(f"\n{name}:")
    run("actual record, all bands, today's sizing", lambda r: True, "today", "today", False)
    run("band only, today's sizing", take, "today", "today", False)
    for lab, sm, sc in V:
        run("6%: " + lab, take, "budget", sm, sc)
