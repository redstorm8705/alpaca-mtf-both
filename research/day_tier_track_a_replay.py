#!/usr/bin/env python3
# ruff: noqa: E501
"""Replay of every Day-tier Track A ENTER decision (since 2026-09-15): real-time room gate (Q1), C2 FADE stop widening
with risk-preserving whole-share sizing (Q2), C3 RIDE time stop (Q3).

Read-only research. No execution imports, no orders. Data tier: T1 Alpaca bars via data.fetcher.fetch_bars_window
(IEX 5-min for the real-time room, the feed the live code reads; SIP 1-min for exits). Bars cached under
logs/day_tier_track_a_cache/. Output: logs/day_tier_track_a_replay.json.

Usage: python research/day_tier_track_a_replay.py [path/to/day_tier_events.jsonl]
"""
from __future__ import annotations

import json
import math
import pickle
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
CACHE = ROOT / "logs" / "day_tier_track_a_cache"
OUT = ROOT / "logs" / "day_tier_track_a_replay.json"
K = 1.5                  # DAYTRADE_MIN_STOP_ATR_MULT
BUF = 0.001              # DAYTRADE_STOP_BUFFER_PCT
MAX_AGE = timedelta(minutes=10)   # _MAX_LAST_BAR_AGE
EOD = (15, 55)


def _fetch(sym, tf, start, end, feed):
    from data.fetcher import fetch_bars_window
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{sym}_{tf}_{feed}_{start:%Y%m%d%H%M}_{end:%Y%m%d%H%M}.pkl"
    if p.exists():
        return pickle.loads(p.read_bytes())
    df = fetch_bars_window(sym, tf, start, end, feed=feed)
    p.write_bytes(pickle.dumps(df))
    return df


def structural_stop(trig, direction, e):
    """Replica of execution.day_trade_manager._compute_stop_price (FADE/RIDE branches)."""
    mode, target, wall = trig.get("mode"), trig.get("target"), trig.get("wall_ref")
    if mode == "FADE" and target is not None:
        t = float(target)
        if (direction == "long" and t <= e) or (direction == "short" and t >= e):
            return None
        r = abs(e - t)
        stop = e + r if direction == "short" else e - r
    else:
        if wall is None:
            return None
        stop = float(wall) * (1 - BUF) if direction == "long" else float(wall) * (1 + BUF)
    stop = round(stop, 2)
    if (direction == "long" and stop >= e) or (direction == "short" and stop <= e):
        return None
    return stop


def rt_room(sym, t0):
    """Replica of execution.day_trade_manager._realtime_vol_room at time t0 (IEX completed 5m bars, last 30)."""
    import config
    df = _fetch(sym, config.TF_5M, (t0 - timedelta(days=5)).replace(second=0, microsecond=0),
                t0.replace(second=0, microsecond=0) + timedelta(minutes=1), "iex")
    if df is None or df.empty:
        return None, "no IEX bars"
    df = df[[i.to_pydatetime() + timedelta(minutes=5) <= t0 for i in df.index]].tail(30)
    if df.empty:
        return None, "no completed bars"
    if t0 - (df.index[-1].to_pydatetime() + timedelta(minutes=5)) > MAX_AGE:
        return None, "stale last bar (freshness guard)"
    if len(df) < 15:
        return None, "<15 bars"
    h = [float(x) for x in df["high"]]
    lo = [float(x) for x in df["low"]]
    c = [float(x) for x in df["close"]]
    trs = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))]
    w = sorted(x for x in trs[-14:] if math.isfinite(x) and x >= 0)
    if len(w) < 14:
        return None, "<14 TRs"
    med = (w[7] + w[6]) / 2.0
    today = t0.astimezone(ET).date()
    t_tr, prev = [], None
    for j, ts in enumerate(df.index):
        ts = ts.astimezone(ET)
        if ts.date() != today or (ts.hour, ts.minute) < (9, 30) or ts.hour >= 16:
            continue
        t_tr.append(h[j] - lo[j] if prev is None else max(h[j] - lo[j], abs(h[j] - prev), abs(lo[j] - prev)))
        prev = c[j]
    tm = sum(t_tr) / len(t_tr) if t_tr else 0.0
    return max(K * med, tm), f"1.5xmed={K*med:.4f} today_mean={tm:.4f}"


def minute_bars(sym, t0):
    import config
    day = t0.astimezone(ET)
    end = day.replace(hour=EOD[0], minute=EOD[1], second=0, microsecond=0)
    start = (t0.astimezone(ET).replace(second=0, microsecond=0) + timedelta(minutes=1))
    df = _fetch(sym, config.TF_1M if hasattr(config, "TF_1M") else "1Min",
                day.replace(hour=9, minute=30, second=0, microsecond=0), end, "sip")
    if df is None or df.empty:
        return []
    out = []
    for i, r in df.iterrows():
        ts = i.to_pydatetime().astimezone(ET)
        if start <= ts < end:
            out.append((ts, float(r["high"]), float(r["low"]), float(r["close"])))
    return out


def walk(bars, side, entry, stop, tgt, time_stop_at=None, risk=None):
    """side +1 long / -1 short. Stop first on a both-touch bar. Optional time stop: at the close of the 1m bar ending at
    time_stop_at, exit if MFE < 0.5R. Else EOD (close of the last bar before 15:55)."""
    mfe = 0.0
    for ts, hi, lo, cl in bars:
        if (side == 1 and lo <= stop) or (side == -1 and hi >= stop):
            return stop, "stop", ts
        if tgt is not None and ((side == 1 and hi >= tgt) or (side == -1 and lo <= tgt)):
            return tgt, "target", ts
        mfe = max(mfe, (hi - entry) if side == 1 else (entry - lo))
        if time_stop_at is not None and ts + timedelta(minutes=1) >= time_stop_at:
            if mfe < 0.5 * risk:
                return cl, "time_stop", ts
            time_stop_at = None
    if not bars:
        return None, "no bars", None
    return bars[-1][3], "eod", bars[-1][0]


def _tot(rs, kp, kr):
    return dict(n=len(rs), usd=round(sum(r[kp] for r in rs), 2), R=round(sum(r[kr] for r in rs), 2))


def _sens(rs, kp, kr, alt_p, alt_r):
    """Delta (alt - base) in $ and R, and the same with the single best / worst trade (by alt-base $ delta) removed."""
    if not rs:
        return {}
    d = sorted(rs, key=lambda r: r[alt_p] - r[kp])
    def delta(xs):
        return dict(n=len(xs), d_usd=round(sum(r[alt_p] - r[kp] for r in xs), 2), d_R=round(sum(r[alt_r] - r[kr] for r in xs), 2))
    return dict(all=delta(d), drop_most_helped=delta(d[:-1]), drop_most_hurt=delta(d[1:]),
                most_helped=f"{d[-1]['sym']} {d[-1]['ts'][:10]}", most_hurt=f"{d[0]['sym']} {d[0]['ts'][:10]}")


def summarize(rows):
    S = {}
    closed = [r for r in rows if r["filled"] and r.get("exit_reason") not in (None, "transferred_to_swing_tier")]
    S["filled"] = sum(r["filled"] for r in rows)
    S["filled_fill_closed"] = len(closed)
    S["reconstructed_all"] = sum(r.get("recon") == "ok" for r in rows)
    S["unreconstructed"] = [f"{r['sym']} {r['ts'][:16]} {r['recon']}" for r in rows if r.get("recon") != "ok"]
    # Q1 skip-gate counterfactual
    skipped = [r for r in rows if r["filled"] and r.get("gate_pass") is False]
    sk_closed = [r for r in skipped if r in closed]
    S["q1_filled_skipped"] = [f"{r['sym']} {r['ts'][:10]} {r['mode']} act ${r.get('actual_pnl')} R {None if r.get('actual_R') is None else round(r['actual_R'], 2)}" for r in skipped]
    S["q1_filled_skipped_closed_actual"] = dict(n=len(sk_closed), usd=round(sum(r["actual_pnl"] for r in sk_closed), 2),
                                               R=round(sum(r.get("actual_R") or 0 for r in sk_closed), 2))
    S["q1_filled_unrecon_would_fail"] = [f"{r['sym']} {r['ts'][:10]} struct-stop already crossed at the fill" for r in rows if r["filled"] and r.get("recon") != "ok"]
    kept = [r for r in closed if r.get("gate_pass") is not False and r.get("recon") == "ok"]
    S["q1_filled_kept_closed_actual"] = dict(n=len(kept), usd=round(sum(r["actual_pnl"] for r in kept), 2),
                                            R=round(sum(r.get("actual_R") or 0 for r in kept), 2))
    for mode in ("FADE", "RIDE"):
        S[f"q1_{mode}_skipped_of_filled_recon"] = f"{sum(1 for r in rows if r['filled'] and r['mode']==mode and r.get('gate_pass') is False)} of {sum(1 for r in rows if r['filled'] and r['mode']==mode and r.get('recon')=='ok')}"
    unf = [r for r in rows if not r["filled"] and r.get("recon") == "ok"]
    up = [r for r in unf if r.get("gate_pass") is not False and "base_pnl" in r]
    uf = [r for r in unf if r.get("gate_pass") is False and "base_pnl" in r]
    S["q1_unfilled_pass"] = _tot(up, "base_pnl", "base_R") | {"list": [f"{r['sym']} {r['ts'][:16]} {r['mode']} {r['base_how']} {r['base_R']:.2f}R ${r['base_pnl']:.2f}" for r in up]}
    S["q1_unfilled_fail"] = _tot(uf, "base_pnl", "base_R")
    # Q2 C2 on FADE (filled, reconstructable)
    f2 = [r for r in rows if r["filled"] and r["mode"] == "FADE" and "c2_pnl" in r and "base_pnl" in r]
    S["q2_fade_filled"] = dict(base=_tot(f2, "base_pnl", "base_R"), c2=_tot(f2, "c2_pnl", "c2_R"),
                               actual_closed=dict(n=sum(1 for r in f2 if r in closed), usd=round(sum(r["actual_pnl"] for r in f2 if r in closed), 2),
                                                  R=round(sum(r.get("actual_R") or 0 for r in f2 if r in closed), 2)),
                               risk_increase_usd=round(sum(r["c2_risk_increase"] for r in f2), 2),
                               risk_increase_list=[f"{r['sym']} {r['ts'][:10]} +${r['c2_risk_increase']:.2f} (orig ${r['q']*r['struct_dist']:.2f})" for r in f2 if r["c2_risk_increase"] > 0.005],
                               sens=_sens(f2, "base_pnl", "base_R", "c2_pnl", "c2_R"))
    f2u = [r for r in rows if not r["filled"] and r["mode"] == "FADE" and "c2_pnl" in r and "base_pnl" in r]
    S["q2_fade_unfilled"] = dict(base=_tot(f2u, "base_pnl", "base_R"), c2=_tot(f2u, "c2_pnl", "c2_R"),
                                 risk_increase_usd=round(sum(r["c2_risk_increase"] for r in f2u), 2))
    # Q3 RIDE time stop (filled, fill-closed)
    f3 = [r for r in rows if "c3_base" in r and r in closed]
    for r in f3:
        r["_b_pnl"], r["_b_R"] = r["c3_base"]["pnl"], r["c3_base"]["R"]
        for n in (3, 6, 12):
            r[f"_n{n}_pnl"], r[f"_n{n}_R"] = r[f"c3_N{n}"]["pnl"], r[f"c3_N{n}"]["R"]
    S["q3_ride"] = dict(actual=dict(n=len(f3), usd=round(sum(r["actual_pnl"] for r in f3), 2), R=round(sum(r["actual_R"] for r in f3), 2)),
                        base=_tot(f3, "_b_pnl", "_b_R"),
                        **{f"N{n}": _tot(f3, f"_n{n}_pnl", f"_n{n}_R") | {"sens": _sens(f3, "_b_pnl", "_b_R", f"_n{n}_pnl", f"_n{n}_R"),
                                                                       "exits": [f"{r['sym']} {r['ts'][:10]} {r[f'c3_N{n}']['how']} {r[f'c3_N{n}']['R']:.2f}R" for r in f3]} for n in (3, 6, 12)},
                        per_trade=[f"{r['sym']} {r['ts'][:10]} act {r['actual_R']:.2f}R base {r['c3_base']['how']} {r['_b_R']:.2f}R" for r in f3])
    for r in f3:
        for k in [k for k in r if k.startswith("_")]:
            del r[k]
    return S


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import config  # noqa: F401
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "logs" / "day_tier_events.jsonl"
    ev = [json.loads(x) for x in open(path) if x.strip()]
    fills = {e["decision_id"]: e for e in ev if e["event"] == "entry_fill" and e.get("decision_id")}
    by_tid = {}
    for e in ev:
        if e["event"] in ("stop_placed", "target_placed", "exit_fill"):
            by_tid.setdefault(e["trade_id"], {}).setdefault(e["event"], e)
    decs = [e for e in ev if e["event"] == "decision" and (e.get("trigger") or {}).get("trigger") == "ENTER"
            and (e.get("size") or {}).get("track") == "A" and e["ts"] >= "2026-09-15"]
    rows = []
    for d in decs:
        trig, size = d["trigger"], d["size"]
        direction = trig["direction"]
        side = 1 if direction == "long" else -1
        f = fills.get(d["decision_id"])
        x = by_tid.get(f["trade_id"], {}) if f else {}
        t0 = datetime.fromisoformat((f or d)["ts"]).astimezone(ET)
        entry = float(f["fill_price"]) if f else float(trig["entry_ref"])
        q = float(f["fill_qty"]) if f else float(size.get("shares") or 0)
        sstruct = structural_stop(trig, direction, float(trig["entry_ref"]))
        placed = float(x["stop_placed"]["stop_price"]) if x.get("stop_placed") else None
        tgt_placed = float(x["target_placed"]["target_price"]) if x.get("target_placed") else None
        tgt = tgt_placed if tgt_placed is not None else (float(trig["target"]) if trig.get("target") is not None else None)
        ex = x.get("exit_fill")
        r = dict(ts=t0.isoformat(), sym=d["symbol"], mode=trig["mode"], dir=direction, filled=bool(f), q=q,
                 entry=entry, entry_ref=float(trig["entry_ref"]), struct_stop=sstruct, placed_stop=placed, target=tgt,
                 exit_reason=ex.get("exit_reason") if ex else None,
                 actual_pnl=float(ex["realized_pnl"]) if ex and ex.get("realized_pnl") is not None else None,
                 skip_reason=trig.get("skip_reason"), size_reason=size.get("reason", "")[:120])
        if f and placed is not None and r["actual_pnl"] is not None:
            r["actual_R"] = r["actual_pnl"] / (q * abs(entry - placed))
        room, why = rt_room(d["symbol"], t0)
        r["rt_room"], r["rt_why"] = room, why
        bars = minute_bars(d["symbol"], t0)
        r["n_1m"] = len(bars)
        if sstruct is None or (side == 1 and sstruct >= entry) or (side == -1 and sstruct <= entry):
            r["recon"] = "no valid structural stop vs entry"
            rows.append(r)
            continue
        r["recon"] = "ok" if bars else "no 1m bars"
        dist = abs(entry - sstruct)
        r["struct_dist"] = dist
        r["gate_pass"] = None if room is None else bool(dist + 1e-9 >= room)
        risk_usd = q * dist
        # baseline sim: structural stop, target, EOD
        px, how, _ = walk(bars, side, entry, sstruct, tgt)
        if px is not None:
            r["base_px"], r["base_how"] = px, how
            r["base_pnl"] = side * (px - entry) * q
            r["base_R"] = side * (px - entry) / dist
        # C2 (FADE): widened stop, risk-preserving whole shares (min 1)
        if trig["mode"] == "FADE" and room is not None:
            nd = max(dist, room)
            nstop = round(entry + side * -nd, 2)
            nq = max(1, math.floor(risk_usd / nd + 1e-9))
            px2, how2, _ = walk(bars, side, entry, nstop, tgt)
            if px2 is not None:
                r.update(c2_stop=nstop, c2_q=nq, c2_px=px2, c2_how=how2, c2_pnl=side * (px2 - entry) * nq,
                         c2_R=side * (px2 - entry) * nq / risk_usd if risk_usd > 0 else None,
                         c2_risk_increase=max(0.0, nq * nd - risk_usd))
        # C3 (RIDE): time stop on the placed stop (actual R) — filled only
        if trig["mode"] == "RIDE" and f and placed is not None:
            pr = abs(entry - placed)
            b5 = t0.replace(minute=t0.minute - t0.minute % 5, second=0, microsecond=0)
            px0, how0, _ = walk(bars, side, entry, placed, tgt)
            r["c3_base"] = dict(px=px0, how=how0, R=side * (px0 - entry) / pr, pnl=side * (px0 - entry) * q)
            for n in (3, 6, 12):
                ta = b5 + timedelta(minutes=5 * (n + 1))
                pxn, hown, _ = walk(bars, side, entry, placed, tgt, time_stop_at=ta, risk=pr)
                r[f"c3_N{n}"] = dict(px=pxn, how=hown, R=side * (pxn - entry) / pr, pnl=side * (pxn - entry) * q)
        rows.append(r)
    summary = summarize(rows)
    OUT.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, default=str))
    print(json.dumps(summary, indent=1, default=str))
    print(f"decisions={len(decs)} filled={sum(r['filled'] for r in rows)} recon_ok={sum(r.get('recon')=='ok' for r in rows)}")
    for r in rows:
        print(r["ts"][:16], r["sym"], r["mode"], r["dir"], "F" if r["filled"] else "-", r["q"],
              "ent", round(r["entry"], 2), "ss", r["struct_stop"], "ps", r["placed_stop"], "tg", r["target"],
              "dist", round(r.get("struct_dist", 0), 3), "room", None if r["rt_room"] is None else round(r["rt_room"], 3),
              "pass", r.get("gate_pass"), "act", r.get("actual_pnl"), r.get("exit_reason"),
              "base", r.get("base_how"), round(r.get("base_R", 0), 2), round(r.get("base_pnl", 0), 2),
              "c2", r.get("c2_how"), r.get("c2_q"), None if r.get("c2_R") is None else round(r["c2_R"], 2),
              None if r.get("c2_pnl") is None else round(r["c2_pnl"], 2), r["recon"], r["rt_why"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
