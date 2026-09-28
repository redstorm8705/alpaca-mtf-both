#!/usr/bin/env python3
# ruff: noqa: E501  — long formula/citation lines (project research convention, cf. deflated_sharpe.py)
"""
research/c2_trigger_lab.py — Confluence 2.0 lab step 4: entry-TRIGGER event test.

Pre-registered in logs/design_records/entry_rebuild_2026-09-26.md ("Lab step 4 ... PRE-REGISTRATION"),
written before any run. Question: does a swing entry TRIGGER (breakout -> back-test -> hold, base
breakout, Stage-2 breakout, pullback-in-uptrend, 1-month reversal; mirrored breakdown shorts) produce
better trades than random names entered on the same days, out of sample 2019-2026, after costs?

Reuses lab step 3's data loader, eligibility, ATR and triple-barrier label (research/c2_swing_lab.py).
Every trigger is computed on CLOSED daily bars at t; entry is the open of t+1; one open trade per
stock (cooldown = label horizon, keyed by the underlying ticker).

Research only. Never imported by the bot; writes only logs/lab/c2/.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import c2_swing_lab as base  # noqa: E402
from deflated_sharpe import deflated_sharpe  # noqa: E402

_OUT = base._OUT
FIRST_TEST_YEAR = 2019
N_RANDOM = 100
RETEST_WINDOW = 10          # sessions after the breakout in which the back-test must happen
RETEST_ATR = 0.25           # back-test = a low within 0.25 ATR of the level
LABELS = {"h20": (20, 1.5, 2.5), "h60": (60, 2.0, 4.0), "h5": (5, 1.5, 2.5)}


# ------------------------------------------------------------------------------------ triggers
def _cross_up(c: np.ndarray, lvl: np.ndarray) -> np.ndarray:
    prev_c = np.vstack([np.full((1, c.shape[1]), np.nan), c[:-1]])
    prev_l = np.vstack([np.full((1, c.shape[1]), np.nan), lvl[:-1]])
    with np.errstate(invalid="ignore"):
        return (c > lvl) & (prev_c <= prev_l)


def _retest_hold(brk: np.ndarray, lvl: np.ndarray, c: np.ndarray, lo_or_hi: np.ndarray,
                 a: np.ndarray, direction: int) -> np.ndarray:
    """From each breakout day b (level L_b = lvl[b], ATR_b = a[b]): the FIRST day b+j (1..RETEST_WINDOW)
    whose low comes back within RETEST_ATR*ATR_b of L_b while every close b+1..b+j stayed on the
    breakout side of L_b (the hold). Longs: low <= L+0.25ATR and close >= L. Shorts mirror."""
    T, N = c.shape
    out = np.zeros((T, N), dtype=bool)
    bi, bj = np.nonzero(brk)
    L = lvl[bi, bj]
    A = a[bi, bj]
    alive = np.isfinite(L) & np.isfinite(A)
    for j in range(1, RETEST_WINDOW + 1):
        t = bi + j
        ok = alive & (t < T)
        if not ok.any():
            break
        tt, jj = t[ok], bj[ok]
        cc, xx = c[tt, jj], lo_or_hi[tt, jj]
        Lk, Ak = L[ok], A[ok]
        with np.errstate(invalid="ignore"):
            if direction == 1:
                held = cc >= Lk
                touch = xx <= Lk + RETEST_ATR * Ak
            else:
                held = cc <= Lk
                touch = xx >= Lk - RETEST_ATR * Ak
        fire = held & touch
        idx_ok = np.nonzero(ok)[0]
        out[tt[fire], jj[fire]] = True
        # a breakout dies when it fires, fails to hold, or has no data
        dead = idx_ok[fire | ~held | ~np.isfinite(cc)]
        alive[dead] = False
    return out


def build_triggers(wide, feats, a14: pd.DataFrame) -> dict[str, tuple[int, np.ndarray]]:
    c = wide["close"].to_numpy()
    h = wide["high"].to_numpy()
    lo = wide["low"].to_numpy()
    v = wide["volume"].to_numpy()
    a = a14.to_numpy()
    cdf = wide["close"]

    def prior_max(x: pd.DataFrame, n: int) -> np.ndarray:
        return x.shift(1).rolling(n, min_periods=n).max().to_numpy()

    def prior_min(x: pd.DataFrame, n: int) -> np.ndarray:
        return x.shift(1).rolling(n, min_periods=n).min().to_numpy()

    vol50 = wide["volume"].shift(1).rolling(50, min_periods=40).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vr = v / vol50
    hi252 = prior_max(wide["high"], 252)
    lo252 = prior_min(wide["low"], 252)
    hi20 = prior_max(wide["high"], 20)
    lo20 = prior_min(wide["low"], 20)
    rng20 = (pd.DataFrame(hi20 - lo20, index=cdf.index, columns=cdf.columns) / a14.shift(1))
    rng_q20 = rng20.rolling(252, min_periods=200).quantile(0.20).to_numpy()
    with np.errstate(invalid="ignore"):
        contracted = rng20.to_numpy() <= rng_q20
    hi130 = prior_max(wide["high"], 130)
    sma150 = cdf.rolling(150, min_periods=150).mean()
    sma_rising = (sma150 > sma150.shift(20)).to_numpy()
    sma200 = cdf.rolling(200, min_periods=200).mean().to_numpy()
    rsi2 = base.rsi(cdf, 2).to_numpy()

    brk1 = _cross_up(c, hi252)
    brk1s = _cross_up(-c, -lo252)
    brk2 = _cross_up(c, hi20) & contracted
    brk2s = _cross_up(-c, -lo20) & contracted
    with np.errstate(invalid="ignore"):
        vol15 = vr >= 1.5
        t4 = _cross_up(c, hi130) & (vr >= 2.0) & sma_rising
        t5 = (c > sma200) & (rsi2 < 10)
    trig = {
        "T1": (+1, _retest_hold(brk1, hi252, c, lo, a, +1)),
        "T1v": (+1, _retest_hold(brk1 & vol15, hi252, c, lo, a, +1)),
        "T2": (+1, brk2),
        "T2v": (+1, brk2 & vol15),
        "T3": (+1, _retest_hold(brk2, hi20, c, lo, a, +1)),
        "T4": (+1, t4),
        "T5": (+1, t5),
        "T1s": (-1, _retest_hold(brk1s, lo252, c, h, a, -1)),
        "T2s": (-1, brk2s),
    }
    return trig


# ---------------------------------------------------------------------------------- evaluation
def take_trades(trig: np.ndarray, elig: np.ndarray, cols, horizon: int) -> list[tuple[int, int]]:
    """Every eligible trigger, one open trade per stock (cooldown = horizon, keyed by base ticker)."""
    base_of = [c.split("@")[0] for c in cols]
    last: dict[str, int] = {}
    out = []
    T = trig.shape[0]
    for i in range(T):
        js = np.nonzero(trig[i] & elig[i])[0]
        for j in js:
            b = base_of[j]
            if i - last.get(b, -10**9) < horizon:
                continue
            out.append((i, j))
            last[b] = i
    return out


def matched_random(trades, elig: np.ndarray, cols, horizon: int, rng) -> list[tuple[int, int]]:
    """Same number of names on each trade day, drawn at random from that day's eligible set, same cooldown."""
    base_of = [c.split("@")[0] for c in cols]
    per_day: dict[int, int] = {}
    for i, _ in trades:
        per_day[i] = per_day.get(i, 0) + 1
    last: dict[str, int] = {}
    out = []
    for i in sorted(per_day):
        cand = rng.permutation(np.nonzero(elig[i])[0])
        need = per_day[i]
        for j in cand:
            b = base_of[j]
            if i - last.get(b, -10**9) < horizon:
                continue
            out.append((i, j))
            last[b] = i
            need -= 1
            if need == 0:
                break
    return out


def r_of(trades, R: np.ndarray) -> np.ndarray:
    if not trades:
        return np.array([])
    ii, jj = zip(*trades, strict=True)
    r = R[np.array(ii), np.array(jj)]
    return r[np.isfinite(r)]


def pbo_cscv(mat: np.ndarray) -> float:
    """Probability of backtest overfitting (Bailey-Borwein-LdP-Zhu CSCV). mat: variants x blocks of
    performance. For every split of the blocks into equal halves, pick the in-sample best variant and
    record whether its out-of-sample rank is below the median. Returns that fraction."""
    V, B = mat.shape
    half = B // 2
    below = 0
    total = 0
    for ins in itertools.combinations(range(B), half):
        oos = [b for b in range(B) if b not in ins]
        is_perf = np.nanmean(mat[:, list(ins)], axis=1)
        oos_perf = np.nanmean(mat[:, oos], axis=1)
        if not np.isfinite(is_perf).any():
            continue
        best = int(np.nanargmax(is_perf))
        rank = (np.sum(oos_perf < oos_perf[best]) + 0.5 * np.sum(oos_perf == oos_perf[best])) / V
        below += int(rank < 0.5)
        total += 1
    return below / total if total else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run")
    ap.add_argument("--years", type=int, nargs=2, default=None, help="first and last test year (inclusive)")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these trigger names")
    args = ap.parse_args()
    _OUT.mkdir(parents=True, exist_ok=True)

    wide, mask, etfs = base.load_panel()
    c = wide["close"]
    dvol = (c * wide["volume"]).rolling(20, min_periods=15).median()
    feats = base.build_features(wide, etfs)
    a14 = base.atr(wide)
    elig_df = mask & (c >= base.MIN_PRICE) & (dvol >= base.MIN_DOLLAR_VOL) & wide["open"].shift(-1).notna()
    elig_df &= a14.notna() & (a14 > 0)
    idx = c.index
    y0, y1 = args.years if args.years else (FIRST_TEST_YEAR, 9999)
    test = (idx.year >= y0) & (idx.year <= y1)
    elig = elig_df.to_numpy() & test[:, None]
    cols = list(c.columns)
    years = sorted({y for y in idx.year if y0 <= y <= y1})
    yr = idx.year.to_numpy()

    trig = build_triggers(wide, feats, a14)
    # T6: the 2 names/day with the worst 21-day return (lab-3 reversal control, re-tested here)
    rev = (-(c / c.shift(21) - 1)).where(elig_df)
    t6 = np.zeros(c.shape, dtype=bool)
    rv = rev.to_numpy()
    for i in range(len(idx)):
        row = rv[i]
        ok = np.nonzero(np.isfinite(row))[0]
        if ok.size >= 2:
            t6[i, ok[np.argsort(-row[ok])[:2]]] = True
    trig["T6"] = (+1, t6)
    if args.only:
        trig = {k: v for k, v in trig.items() if k in args.only}

    entry = wide["open"].shift(-1).to_numpy()
    atr_pct = (a14 / c).to_numpy()
    runs: dict = {}
    mats: list[np.ndarray] = []
    names: list[str] = []
    srs: list[float] = []
    series: dict[str, np.ndarray] = {}
    rng = np.random.default_rng(20260928)
    for lname, (hz, sl, tp) in LABELS.items():
        base.HORIZON, base.SL_ATR, base.TP_ATR = hz, sl, tp
        gross = {d: base.triple_barrier(wide, a14, d, cost=False).to_numpy() for d in (+1, -1)}
        risk = sl * a14.to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            cost_flat = (base.COST_BPS_ROUND_TRIP / 1e4) * entry / risk
            cost_spread = (base.COST_BPS_ROUND_TRIP / 1e4 + 0.02 * atr_pct) * entry / risk
        for cname, cst in (("c10", cost_flat), ("cspread", cost_spread)):
            for tname, (direction, tmat) in trig.items():
                if lname == "h5" and tname != "T5":
                    continue
                R = gross[direction] - cst
                tr = take_trades(tmat, elig, cols, hz)
                r = r_of(tr, R)
                key = f"{tname}|{lname}|{cname}"
                if r.size < 30:
                    runs[key] = {"n": int(r.size)}
                    continue
                rand_means, rand_year = [], {y: [] for y in years}
                for _ in range(N_RANDOM):
                    rt = matched_random(tr, elig, cols, hz, rng)
                    rr = r_of(rt, R)
                    rand_means.append(float(rr.mean()))
                    ry = np.array([yr[i] for i, j in rt if np.isfinite(R[i, j])])
                    for y in years:
                        sel = rr[ry == y]
                        if sel.size:
                            rand_year[y].append(float(sel.mean()))
                ty = np.array([yr[i] for i, j in tr if np.isfinite(R[i, j])])
                by_year = {str(y): (float(r[ty == y].mean()) if (ty == y).any() else float("nan")) for y in years}
                rnd_year = {str(y): (float(np.mean(v)) if v else float("nan")) for y, v in rand_year.items()}
                beats = sum(1 for y in years if np.isfinite(by_year[str(y)]) and by_year[str(y)] > rnd_year[str(y)])
                sd = float(r.std(ddof=1))
                sr = float(r.mean() / sd) if sd > 0 else float("nan")
                runs[key] = {
                    "n": int(r.size), "per_day": float(r.size / max(1, test.sum())),
                    "mean_R": float(r.mean()), "win": float((r > 0).mean()), "sr": sr,
                    "rand_mean": float(np.mean(rand_means)),
                    "rand_p05": float(np.percentile(rand_means, 5)),
                    "rand_p95": float(np.percentile(rand_means, 95)),
                    "by_year": by_year, "rand_by_year": rnd_year, "beats_random_years": beats,
                }
                mats.append(np.array([by_year[str(y)] - rnd_year[str(y)] for y in years]))
                names.append(key)
                srs.append(sr)
                series[key] = r
                print(key, {k: (round(x, 4) if isinstance(x, float) else x) for k, x in runs[key].items()
                            if k not in ("by_year", "rand_by_year")}, flush=True)

    n_trials = len(names)
    sr_std = float(np.nanstd(srs, ddof=1)) if n_trials > 1 else float("nan")
    mat = np.vstack(mats) if mats else np.zeros((0, len(years)))
    pbo = pbo_cscv(mat) if len(mats) > 1 else float("nan")
    for key in names:
        d = deflated_sharpe(series[key], n_trials=n_trials, trials_sr_std=sr_std)
        runs[key]["psr"] = d.psr
        runs[key]["dsr"] = d.dsr
        rk = runs[key]
        rk["passes"] = bool(key.split("|")[1] == "h20" and rk["mean_R"] > rk["rand_p95"] and rk["beats_random_years"] >= 6
                            and np.isfinite(d.dsr) and d.dsr >= 0.9 and pbo <= 0.05 and rk["per_day"] >= 1.2)
    results = {"generated": datetime.now(base.ET).isoformat(), "n_trials": n_trials, "trials_sr_std": sr_std,
               "pbo": pbo, "runs": runs, "params": {"RETEST_WINDOW": RETEST_WINDOW, "RETEST_ATR": RETEST_ATR,
                                                     "LABELS": LABELS, "N_RANDOM": N_RANDOM}}
    out = _OUT / f"c2_trigger_lab_{args.tag}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=1, default=str))
    tmp.replace(out)
    print("n_trials", n_trials, "pbo", round(pbo, 3))
    for key in names:
        rk = runs[key]
        print(f"{key:22s} n={rk['n']:5d} /day={rk['per_day']:.2f} R={rk['mean_R']:+.3f} rand={rk['rand_mean']:+.3f} "
              f"[{rk['rand_p05']:+.3f},{rk['rand_p95']:+.3f}] yrs={rk['beats_random_years']}/8 dsr={rk['dsr']:.2f} pass={rk['passes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
