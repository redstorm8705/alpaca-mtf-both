#!/usr/bin/env python3
# ruff: noqa: E501
"""
research/day_tier_trackb_oos_replay.py — OUT-OF-SAMPLE test of two by-product findings of
research/day_tier_trackb_watchday_replay.py (in-sample 2025-10-01..2026-10-09):

  H1: Track B SHORTS with no clear daily side (gate == "no_clear_side") are positive (IS n=128, +0.163R, t 1.47;
      room_ok: n=94, +0.238R, t 1.95).
  H2: WITH-TREND Track B LONGS (gate == "with_trend") are negative (IS n=264, -0.184R, t -2.49).

Rules FROZEN: signal detection, side, watch day, stop, target, EOD flat, cost and room gate are the in-sample
script's own functions (imported, not copied), run on sessions BEFORE 2025-10-01. Added: a realistic entry variant
that enters at the NEXT 5m SIP bar's open (same stop level, same 2R target re-measured from the new entry, same
5 bps cost); a signal whose next open is already at/through the stop is counted as "no fill" for that variant.

Statistics per group: n, win %, mean / median R, t, a SESSION-BLOCKED bootstrap 90% CI (resample whole sessions,
since same-day trades share the market move), per-year split, and the multiple-testing thresholds for ~20 trials.

READ-ONLY research: no orders, no execution imports. T1 Alpaca bars via data.fetcher.fetch_bars_window (through the
in-sample module), cached to logs/cache/trackb_watchday/.

Run: uv run -q --python 3.11 --with pandas --with numpy --with alpaca-py==0.43.3 --with requests --with python-dotenv \
       python research/day_tier_trackb_oos_replay.py [--start 2023-10-01] [--end 2025-09-30] [--dstart 2022-03-01]
Output: logs/trackb_oos_replay_<today>.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

import pandas as pd  # noqa: E402

import day_tier_trackb_watchday_replay as base  # noqa: E402  (frozen in-sample rules)

ET = base.ET
N_TRIALS = 20


def replay_symbol(sym: str, start: str, end: str, dstart: str) -> list:
    """base.replay_symbol verbatim in logic, plus the next-bar-open entry variant (fields prefixed nx_)."""
    from strategy import day_tier_track_b as tb
    from strategy.day_tier_momentum_trigger import compute_momentum_trigger
    iex = base.fetch_5m(sym, "iex", start, end)
    sip = base.fetch_5m(sym, "sip", start, end)
    d_sip = base.fetch_daily(sym, "sip", dstart, end)
    d_iex = base.fetch_daily(sym, "iex", dstart, end)
    if iex.empty or sip.empty or d_sip.empty or d_iex.empty:
        print(f"[{sym}] missing data — skipped", flush=True)
        return []
    out = []
    days = sorted(set(iex.index.date))
    for day in days:
        today = pd.Timestamp(day)
        prior = d_sip[d_sip.index < today]
        prior_iex = d_iex[d_iex.index < today]
        if prior.empty or len(prior_iex) < 15:
            continue
        if prior.index[-1] != prior_iex.index[-1]:
            continue
        prior_close = float(prior["close"].iloc[-1])
        adv = float(prior_iex["volume"].dropna().tail(20).mean())
        day_bars = iex[iex.index.date == day]
        if day_bars.empty or (day_bars.index[0].hour, day_bars.index[0].minute) != (9, 30):
            continue
        for k in range(5, min(len(day_bars), 72) + 1):
            frame = day_bars.iloc[:k]
            span = (frame.index[-1] - frame.index[0]).total_seconds()
            if len(frame) < int(round(span / 300)) + 1:
                ok, _ = tb._iex_holes_ok(frame)
                if not ok:
                    continue
            now = frame.index[-1].to_pydatetime() + timedelta(minutes=5)
            if (now.hour * 60 + now.minute) > 15 * 60 + 35:
                break
            scr = tb.screen_mover(sym, frame, prior_close, adv, now)
            if not scr.get("is_mover"):
                continue
            mom = compute_momentum_trigger(sym, scr.get("gap_direction"), frame)
            if mom.get("trigger") != "ENTER":
                continue
            direction = mom["direction"]
            entry = float(mom["entry_ref"])
            level = float(mom["structural_level"])
            stop = round(level * (1 + base.STOP_BUF) if direction == "short" else level * (1 - base.STOP_BUF), 2)
            valid = (stop > entry) if direction == "short" else (stop < entry)
            side, score = base.side_pit(d_sip["close"], entry, today)
            wd = base.watch_day(d_sip, today)
            hist5 = iex[iex.index <= frame.index[-1]]
            atr = base.robust_atr(hist5)
            room_ok = bool(valid and atr is not None and abs(entry - stop) >= base.MIN_STOP_ATR * atr)
            path = sip[(sip.index.date == day) & (sip.index > frame.index[-1])
                       & ((sip.index.hour * 60 + sip.index.minute) <= 15 * 60 + 50)]
            sim = base.simulate(path, direction, entry, stop) if valid else {}
            nx: dict = {}
            if valid and not path.empty:
                e2 = float(path["open"].iloc[0])
                if (direction == "short" and e2 < stop) or (direction == "long" and e2 > stop):
                    s2 = base.simulate(path, direction, e2, stop)
                    nx = {f"nx_{kk}": vv for kk, vv in s2.items()}
                    nx["nx_entry"] = e2
                else:
                    nx = {"nx_nofill": True, "nx_entry": e2}
            if direction == "short":
                if side == "LONG":
                    gate = "watchday_confirmed" if wd else "watchday_block"
                elif side == "SHORT":
                    gate = "with_trend"
                else:
                    gate = "no_clear_side"
            else:
                gate = "with_trend" if side == "LONG" else ("counter_block" if side == "SHORT" else "no_clear_side")
            out.append({"symbol": sym, "date": str(day), "time_et": frame.index[-1].strftime("%H:%M"),
                        "direction": direction, "mode": mom.get("mode"), "gap_pct": scr.get("gap_pct"),
                        "rvol": scr.get("rvol"), "entry": entry, "level": level, "stop": stop,
                        "side": side, "side_score": score, "watch_day": wd, "gate": gate,
                        "room_ok": room_ok, "atr": atr, **sim, **nx})
            break
    print(f"[{sym}] {len(days)} sessions, {len(out)} signals", flush=True)
    return out


# ---------------------------------------------------------------- stats
def _rs(rows: list, key: str) -> list:
    return [(r["date"], r[key]) for r in rows if key in r]


def tstat(xs: list) -> "float | None":
    if len(xs) < 2:
        return None
    sd = statistics.stdev(xs)
    return round(statistics.mean(xs) / (sd / math.sqrt(len(xs))), 2) if sd > 0 else None


def session_bootstrap(pairs: list, n: int = 10000, seed: int = 7) -> dict:
    by: dict = defaultdict(list)
    for d, x in pairs:
        by[d].append(x)
    sess = list(by.values())
    if len(sess) < 2:
        return {}
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        pick = rng.choices(sess, k=len(sess))
        flat = [x for s in pick for x in s]
        means.append(sum(flat) / len(flat))
    means.sort()
    return {"sessions": len(sess), "ci90": [round(means[int(0.05 * n)], 3), round(means[int(0.95 * n)], 3)],
            "p_mean_le_0": round(sum(1 for m in means if m <= 0) / n, 3),
            "p_mean_ge_0": round(sum(1 for m in means if m >= 0) / n, 3)}


def gstats(rows: list, key: str) -> dict:
    pairs = _rs(rows, key)
    xs = [x for _, x in pairs]
    if not xs:
        return {"n": 0}
    res = {"n": len(xs), "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 3),
           "mean_R": round(statistics.mean(xs), 3), "median_R": round(statistics.median(xs), 3),
           "t_stat": tstat(xs), **session_bootstrap(pairs)}
    years: dict = defaultdict(list)
    for d, x in pairs:
        years[d[:4]].append(x)
    res["by_year"] = {y: {"n": len(v), "mean_R": round(statistics.mean(v), 3), "t_stat": tstat(v)}
                      for y, v in sorted(years.items())}
    return res


def thresholds(n_trials: int = N_TRIALS) -> dict:
    """Normal-approx critical values for the max of n_trials tests (Bonferroni / Sidak) and the expected max of
    n_trials iid N(0,1) (the deflated-Sharpe benchmark, Bailey & Lopez de Prado 2014)."""
    from statistics import NormalDist
    nd = NormalDist()
    em = 0.5772156649
    e_max = (1 - em) * nd.inv_cdf(1 - 1 / n_trials) + em * nd.inv_cdf(1 - 1 / (n_trials * math.e))
    return {"n_trials": n_trials,
            "bonferroni_two_sided_5pct": round(nd.inv_cdf(1 - 0.05 / (2 * n_trials)), 2),
            "bonferroni_one_sided_5pct": round(nd.inv_cdf(1 - 0.05 / n_trials), 2),
            "expected_max_t_under_null": round(e_max, 2),
            "single_prespecified_one_sided_5pct": round(nd.inv_cdf(0.95), 2)}


def verdict(g: dict, expected_sign: int) -> str:
    if not g.get("n"):
        return "NO DATA"
    m, t, ci = g["mean_R"], g.get("t_stat") or 0.0, g.get("ci90")
    if m * expected_sign < 0 and abs(t) >= 2:
        return "REVERSED"
    ci_ok = bool(ci) and ((ci[0] > 0) if expected_sign > 0 else (ci[1] < 0))
    if m * expected_sign > 0 and abs(t) >= 2 and ci_ok:
        return "CONFIRMED"
    return "NOT CONFIRMED"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-10-01")
    ap.add_argument("--end", default="2025-09-30")
    ap.add_argument("--dstart", default="2022-03-01")
    a = ap.parse_args()
    from strategy.day_tier_track_b import track_b_universe
    out_path = ROOT / "logs" / f"trackb_oos_replay_{datetime.now(ET):%Y-%m-%d}.json"
    sigs: list = []
    for sym in track_b_universe():
        sigs += replay_symbol(sym, a.start, a.end, a.dstart)
        out_path.write_text(json.dumps({"partial": True, "signals": sigs}, indent=1, default=str))
    sh = [s for s in sigs if s["direction"] == "short"]
    lg = [s for s in sigs if s["direction"] == "long"]
    groups = {
        "H1_short_no_clear_side": ([s for s in sh if s["gate"] == "no_clear_side"], +1),
        "H1_short_no_clear_side_room_ok": ([s for s in sh if s["gate"] == "no_clear_side" and s["room_ok"]], +1),
        "H2_long_with_trend": ([s for s in lg if s["gate"] == "with_trend"], -1),
        "H2_long_with_trend_room_ok": ([s for s in lg if s["gate"] == "with_trend" and s["room_ok"]], -1),
        "ctx_short_all": (sh, 0), "ctx_long_all": (lg, 0),
        "ctx_short_watchday_block": ([s for s in sh if s["gate"] == "watchday_block"], 0),
        "ctx_short_with_trend": ([s for s in sh if s["gate"] == "with_trend"], 0),
        "ctx_long_counter_block": ([s for s in lg if s["gate"] == "counter_block"], 0),
    }
    res: dict = {"window": [a.start, a.end], "n_signals": len(sigs),
                 "sessions_with_signal": len({s["date"] for s in sigs}),
                 "nx_nofill": sum(1 for s in sigs if s.get("nx_nofill")),
                 "thresholds": thresholds(), "groups": {}}
    for name, (rows, sign) in groups.items():
        g = {"bar_close_entry": gstats(rows, "R"), "next_open_entry": gstats(rows, "nx_R"),
             "nx_nofill": sum(1 for s in rows if s.get("nx_nofill"))}
        if sign:
            g["verdict_bar_close"] = verdict(g["bar_close_entry"], sign)
            g["verdict_next_open"] = verdict(g["next_open_entry"], sign)
        res["groups"][name] = g
    res["signals"] = sigs
    out_path.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "signals"}, indent=1, default=str))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
