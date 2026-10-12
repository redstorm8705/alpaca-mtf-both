#!/usr/bin/env python3
# ruff: noqa: E501
"""
research/day_tier_trackb_core_review.py — does Track B's CORE setup have an edge at all? (Claude, 2026-10-12)

Context: out-of-sample (2023-10..2025-09) all Track B shorts were -0.096R (n=877) and longs -0.078R (n=975)
(logs/trackb_oos_replay_2026-10-11.json). The design record (day_tier_v2_design_2026-08-29.md §7 / §7c, quoted
in strategy/day_tier_momentum_trigger.py) claims raw gap-continuation is negative base-rate and that conditioning on
a volume-confirmed break-and-hold of the opening range flips it positive.

PRE-DECLARED ANALYSES (written before any result of this script was seen; no other split is reported):
  1. Design-record claim — CONDITIONED (the bot's trigger: first ENTER per symbol-day, as the prior replays) vs the
     RAW-GAP BASELINE: every symbol-day that passed the mover screen, entered in the gap direction at the close of
     the FIRST bar (k >= 5, the earliest bar the trigger itself can fire) where screen_mover passes. Baseline stop =
     the opening-range extreme AGAINST the trade (long: OR low x (1-0.001); short: OR high x (1+0.001)) — the
     OR-level stop a raw gap entry has (there is no "broken level" yet); 2R target; flat 15:50. Also reported: the
     COMPLEMENT (screened movers that never triggered, baseline-entered) and conditioned - baseline (session-blocked
     bootstrap of the difference). Net bps reported beside R since the two stop rules differ in width.
  2. Exit structure on the conditioned entries: (a) OR-level stop + 2R target + flat 15:50 (current),
     (b) 1R target, (c) no target, flat 15:50.
  3. Mode: DRIVE vs PULLBACK (exit a).
  4. Entry time: entry (= signal bar close = bar start + 5 min) before 11:00 ET vs at/after (exit a).
  Each cell: all / long / short.  Windows: IS 2025-10-01..2026-10-09, OOS 2023-10-01..2025-09-30, POOLED.
  Stats: n, mean R, median R, total R, t, SESSION-BLOCKED bootstrap 90% CI (whole sessions resampled).
  TRIAL COUNT for the verdict: 8 candidate variants (baseline, conditioned/exit a, exit b, exit c, DRIVE, PULLBACK,
  early, late) x 3 direction cuts = 24 trials -> one-sided Bonferroni 5% threshold reported.
  VERDICT RULE: a variant shows an edge only if mean R > 0 in BOTH windows AND the pooled 90% CI lower bound > 0;
  it is "multiple-testing robust" only if the pooled t also clears the 24-trial Bonferroni threshold.

Frozen rules: screen_mover / compute_momentum_trigger / _iex_holes_ok are the bot's own functions; bar loaders,
simulate geometry, STOP_BUF, 5 bps cost are research/day_tier_trackb_watchday_replay.py's (same cache,
logs/cache/trackb_watchday/). Entry at the signal bar close (the prior replays' primary convention).

READ-ONLY research: no orders, no execution imports.
Run: uv run -q --python 3.11 --with pandas --with numpy --with alpaca-py==0.43.3 --with requests --with python-dotenv \
       python research/day_tier_trackb_core_review.py
Output: logs/trackb_core_review_<today>.json
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

import pandas as pd  # noqa: E402

import day_tier_trackb_watchday_replay as base  # noqa: E402

ET = base.ET
WINDOWS = {"IS": ("2025-10-01", "2026-10-09", "2024-03-01"), "OOS": ("2023-10-01", "2025-09-30", "2022-03-01")}
N_TRIALS = 24


def simulate(path: pd.DataFrame, direction: str, entry: float, stop: float, target_r: "float | None") -> dict:
    """base.simulate geometry with a configurable target (None = no target, flat at the last path bar)."""
    r = abs(entry - stop)
    tgt = None if target_r is None else (entry - target_r * r if direction == "short" else entry + target_r * r)
    exit_px, how = None, "eod"
    for o, h, low in zip(path["open"].values, path["high"].values, path["low"].values):
        o, h, low = float(o), float(h), float(low)
        if direction == "short":
            if h >= stop:
                exit_px, how = (max(o, stop) if o >= stop else stop), "stop"
                break
            if tgt is not None and low <= tgt:
                exit_px, how = (min(o, tgt) if o <= tgt else tgt), "target"
                break
        else:
            if low <= stop:
                exit_px, how = (min(o, stop) if o <= stop else stop), "stop"
                break
            if tgt is not None and h >= tgt:
                exit_px, how = (max(o, tgt) if o >= tgt else tgt), "target"
                break
    if exit_px is None:
        if path.empty:
            return {}
        exit_px = float(path["close"].iloc[-1])
    gross = (entry - exit_px) if direction == "short" else (exit_px - entry)
    cost = base.COST_BPS / 1e4 * entry
    return {"R": round((gross - cost) / r, 4), "bps": round(gross / entry * 1e4 - base.COST_BPS, 2), "how": how}


def replay_symbol(args: tuple) -> list:
    sym, start, end, dstart, win = args
    from strategy import day_tier_track_b as tb
    from strategy.day_tier_momentum_trigger import compute_momentum_trigger
    iex = base.fetch_5m(sym, "iex", start, end)
    sip = base.fetch_5m(sym, "sip", start, end)
    d_sip = base.fetch_daily(sym, "sip", dstart, end)
    d_iex = base.fetch_daily(sym, "iex", dstart, end)
    if iex.empty or sip.empty or d_sip.empty or d_iex.empty:
        return []
    iex_by = {d: g for d, g in iex.groupby(iex.index.date)}
    sip_by = {d: g for d, g in sip.groupby(sip.index.date)}
    out = []
    for day in sorted(iex_by):
        today = pd.Timestamp(day)
        prior = d_sip[d_sip.index < today]
        prior_iex = d_iex[d_iex.index < today]
        if prior.empty or len(prior_iex) < 15 or prior.index[-1] != prior_iex.index[-1]:
            continue
        prior_close = float(prior["close"].iloc[-1])
        adv = float(prior_iex["volume"].dropna().tail(20).mean())
        day_bars = iex_by[day]
        if (day_bars.index[0].hour, day_bars.index[0].minute) != (9, 30):
            continue
        sday = sip_by.get(day)
        if sday is None:
            continue
        sday = sday[(sday.index.hour * 60 + sday.index.minute) <= 15 * 60 + 50]
        or_hi = float(day_bars["high"].iloc[:3].max())
        or_lo = float(day_bars["low"].iloc[:3].min())
        base_done = cond_done = False
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
            path = sday[sday.index > frame.index[-1]]
            entry_t = now.strftime("%H:%M")
            if not base_done:
                base_done = True
                d = "long" if scr.get("gap_direction") == "up" else "short"
                e = float(frame["close"].iloc[-1])
                st = round(or_lo * (1 - base.STOP_BUF) if d == "long" else or_hi * (1 + base.STOP_BUF), 2)
                valid = (st < e) if d == "long" else (st > e)
                rec = {"kind": "baseline", "win": win, "symbol": sym, "date": str(day), "entry_t": entry_t,
                       "direction": d, "valid": valid}
                if valid:
                    rec.update({f"a_{kk}": v for kk, v in simulate(path, d, e, st, 2.0).items()})
                out.append(rec)
            if not cond_done:
                mom = compute_momentum_trigger(sym, scr.get("gap_direction"), frame)
                if mom.get("trigger") == "ENTER":
                    cond_done = True
                    d = mom["direction"]
                    e = float(mom["entry_ref"])
                    lvl = float(mom["structural_level"])
                    st = round(lvl * (1 + base.STOP_BUF) if d == "short" else lvl * (1 - base.STOP_BUF), 2)
                    valid = (st > e) if d == "short" else (st < e)
                    rec = {"kind": "cond", "win": win, "symbol": sym, "date": str(day), "entry_t": entry_t,
                           "direction": d, "mode": mom.get("mode"), "valid": valid}
                    if valid:
                        for tag, tr in (("a", 2.0), ("b", 1.0), ("c", None)):
                            rec.update({f"{tag}_{kk}": v for kk, v in simulate(path, d, e, st, tr).items()})
                    out.append(rec)
            if base_done and cond_done:
                break
    print(f"[{win} {sym}] {sum(1 for r in out if r['kind']=='cond')} cond, {sum(1 for r in out if r['kind']=='baseline')} base", flush=True)
    return out


# ---------------------------------------------------------------- stats
def _boot_means(by_sess: dict, rng: random.Random, n: int) -> list:
    sess = list(by_sess.values())
    out = []
    for _ in range(n):
        flat = [x for s in rng.choices(sess, k=len(sess)) for x in s]
        out.append(sum(flat) / len(flat))
    return sorted(out)


def cell(rows: list, key: str = "a_R", nboot: int = 5000) -> dict:
    pairs = [(r["date"], r[key]) for r in rows if key in r]
    xs = [x for _, x in pairs]
    if len(xs) < 2:
        return {"n": len(xs)}
    sd = statistics.stdev(xs)
    by: dict = defaultdict(list)
    for d, x in pairs:
        by[d].append(x)
    m = _boot_means(by, random.Random(7), nboot)
    bkey = key.replace("_R", "_bps")
    bps = [r[bkey] for r in rows if bkey in r]
    return {"n": len(xs), "sessions": len(by), "mean_R": round(statistics.mean(xs), 3),
            "median_R": round(statistics.median(xs), 3), "total_R": round(sum(xs), 1),
            "t": round(statistics.mean(xs) / (sd / math.sqrt(len(xs))), 2) if sd > 0 else None,
            "ci90": [round(m[int(0.05 * nboot)], 3), round(m[int(0.95 * nboot)], 3)],
            "mean_bps": round(statistics.mean(bps), 1) if bps else None}


def diff_boot(a: list, b: list, key: str = "a_R", nboot: int = 5000) -> dict:
    """Session-blocked bootstrap of mean(a) - mean(b), resampling sessions jointly."""
    sa: dict = defaultdict(list)
    sb: dict = defaultdict(list)
    for r in a:
        if key in r:
            sa[r["date"]].append(r[key])
    for r in b:
        if key in r:
            sb[r["date"]].append(r[key])
    sess = sorted(set(sa) | set(sb))
    rng = random.Random(11)
    ds = []
    for _ in range(nboot):
        pick = rng.choices(sess, k=len(sess))
        xa = [x for s in pick for x in sa.get(s, [])]
        xb = [x for s in pick for x in sb.get(s, [])]
        if xa and xb:
            ds.append(sum(xa) / len(xa) - sum(xb) / len(xb))
    ds.sort()
    pt = statistics.mean([x for v in sa.values() for x in v]) - statistics.mean([x for v in sb.values() for x in v])
    return {"diff_R": round(pt, 3), "ci90": [round(ds[int(0.05 * len(ds))], 3), round(ds[int(0.95 * len(ds))], 3)]}


def dirs(rows: list) -> dict:
    return {"all": rows, "long": [r for r in rows if r["direction"] == "long"],
            "short": [r for r in rows if r["direction"] == "short"]}


def early(r: dict) -> bool:
    return r["entry_t"] < "11:00"


def analyse(rows: list) -> dict:
    cond = [r for r in rows if r["kind"] == "cond" and r["valid"]]
    bl = [r for r in rows if r["kind"] == "baseline" and r["valid"]]
    trig_days = {(r["symbol"], r["date"]) for r in rows if r["kind"] == "cond"}
    comp = [r for r in bl if (r["symbol"], r["date"]) not in trig_days]
    res: dict = {"n_cond_invalid": sum(1 for r in rows if r["kind"] == "cond" and not r["valid"]),
                 "n_base_invalid": sum(1 for r in rows if r["kind"] == "baseline" and not r["valid"])}
    a1: dict = {}
    for dname, _ in dirs(cond).items():
        c, b, x = dirs(cond)[dname], dirs(bl)[dname], dirs(comp)[dname]
        a1[dname] = {"conditioned": cell(c), "baseline": cell(b), "complement": cell(x), "cond_minus_base": diff_boot(c, b)}
    res["1_claim"] = a1
    res["2_exit"] = {dn: {"a_2R": cell(rs, "a_R"), "b_1R": cell(rs, "b_R"), "c_noTarget": cell(rs, "c_R")}
                     for dn, rs in dirs(cond).items()}
    res["3_mode"] = {dn: {"DRIVE": cell([r for r in rs if r["mode"] == "DRIVE"]),
                          "PULLBACK": cell([r for r in rs if r["mode"] == "PULLBACK"])} for dn, rs in dirs(cond).items()}
    res["4_time"] = {dn: {"before_11": cell([r for r in rs if early(r)]),
                          "after_11": cell([r for r in rs if not early(r)])} for dn, rs in dirs(cond).items()}
    return res


def variants(an: dict) -> dict:
    v = {}
    for dn in ("all", "long", "short"):
        v[f"baseline/{dn}"] = an["1_claim"][dn]["baseline"]
        v[f"cond_2R/{dn}"] = an["2_exit"][dn]["a_2R"]
        v[f"exit_1R/{dn}"] = an["2_exit"][dn]["b_1R"]
        v[f"exit_noTarget/{dn}"] = an["2_exit"][dn]["c_noTarget"]
        v[f"DRIVE/{dn}"] = an["3_mode"][dn]["DRIVE"]
        v[f"PULLBACK/{dn}"] = an["3_mode"][dn]["PULLBACK"]
        v[f"before_11/{dn}"] = an["4_time"][dn]["before_11"]
        v[f"after_11/{dn}"] = an["4_time"][dn]["after_11"]
    return v


def main() -> None:
    from strategy.day_tier_track_b import track_b_universe
    jobs = [(s, *WINDOWS[w], w) for w in WINDOWS for s in track_b_universe()]
    rows: list = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for r in ex.map(replay_symbol, jobs):
            rows += r
    res: dict = {"windows": WINDOWS, "n_trials": N_TRIALS,
                 "bonferroni_one_sided_5pct_t": round(NormalDist().inv_cdf(1 - 0.05 / N_TRIALS), 2), "by_window": {}}
    for w in ("IS", "OOS", "POOLED"):
        sub = rows if w == "POOLED" else [r for r in rows if r["win"] == w]
        res["by_window"][w] = analyse(sub)
    vi, vo, vp = (variants(res["by_window"][w]) for w in ("IS", "OOS", "POOLED"))
    verdict = {}
    for k in vp:
        same_pos = (vi[k].get("mean_R", -1) > 0) and (vo[k].get("mean_R", -1) > 0)
        ci_pos = bool(vp[k].get("ci90")) and vp[k]["ci90"][0] > 0
        verdict[k] = {"IS": vi[k].get("mean_R"), "OOS": vo[k].get("mean_R"), "pooled": vp[k].get("mean_R"),
                      "pooled_t": vp[k].get("t"), "pooled_ci90": vp[k].get("ci90"),
                      "edge": same_pos and ci_pos,
                      "robust": same_pos and ci_pos and (vp[k].get("t") or 0) >= res["bonferroni_one_sided_5pct_t"]}
    res["verdict"] = verdict
    res["rows"] = rows
    out = ROOT / "logs" / f"trackb_core_review_{datetime.now(ET):%Y-%m-%d}.json"
    out.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
