"""Swing breakout give-back replay — 2026-11-05. Owner concern: the 20-session-low exit gives back too much open profit
(BRK.B, entry $544.60 on 2026-10-27, ~$582 now, 20-session low ~$505). Read-only research: places no orders, touches no state.

Reuses breakout_remedies.py: same universe (UNIV), same T1 daily bars (data.fetcher, SIP, split-adjusted), same entry rule
(close > highest close of the prior 55 sessions and > SMA200 -> buy next open), same stop (entry - 2.5*ATR14, ATR = mean of the
14 true ranges before the signal bar, fixed for the trade), same fills (stop fills at min(open, stop); a close-based exit fills
at the close), same time exit (30th session's close), same one-lot-per-name, same R = (exit - entry) / (2.5*ATR).
The base script applies NO costs; that is kept for comparability, and a net column adds 5 bps per side (stated assumption).

Families (each tightens or adds to the base; the 20-session-low exit stays on unless the family replaces it):
 W  wider chandelier: stop = max(stop, highest close since entry - z*ATR), z = 4, 5, 6
 L  profit lock: once highest close >= entry + k*R (k = 2, 3, 4), stop = max(stop, entry + 0.5*(peak close - entry))
 Q  faster trend exit: close below the prior 10- or 15-session low instead of 20
 H  scale out: sell half at +2R or +3R (resting limit; a gap above fills at the open); the rest follows the base rule
 D  dynamic: allowance = 90th percentile of the stock's 10-session drawdown (1 - close / max close of that and the prior
    9 sessions) over the 250 sessions ending on the signal bar; stop = max(stop, highest close since entry * (1 - allowance))
Every stop change is computed from that day's close and takes effect from the next session (no same-bar look-ahead).
Selection: best mean R per family on 2020-2023 only; the test years 2024-2026 are then read for the picks.
Give-back = peak open R (highest CLOSE held, in R) - exit R, on trades whose peak reached >= +1R.
Usage (server, repo root, .env exported): venv/bin/python3 logs/research/swing_pm/giveback_replay.py"""
import math
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import config  # noqa: E402
from breakout_remedies import UNIV  # noqa: E402  (same universe as the base script)
from data.fetcher import fetch_bars_window  # noqa: E402

ET = ZoneInfo("America/New_York")
SPLIT = 2024
BASE_CACHE = os.path.join(HERE, "daily.pkl")          # the base script's cache, used if present
CACHE = os.path.join(HERE, "giveback_daily.pkl")      # this script's own bars-only cache otherwise
COST_SIDE = 0.0005                                    # 5 bps per side, assumption (base script: none)
ND = NormalDist()


def load():
    if os.path.exists(BASE_CACHE):
        return {s: v[0] for s, v in pickle.load(open(BASE_CACHE, "rb")).items()}, "daily.pkl (base cache)"
    if os.path.exists(CACHE):
        return pickle.load(open(CACHE, "rb")), "giveback_daily.pkl"
    out = {}
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    for s in UNIV:
        d = fetch_bars_window(s, config.TF_DAILY, datetime(2019, 1, 1, tzinfo=timezone.utc), end, feed="sip", adjustment="split")
        if d is None or d.empty:
            continue
        out[s] = d
    pickle.dump(out, open(CACHE, "wb"))
    return out, "fresh fetch (data.fetcher, SIP, split)"


def prep(d):
    """Arrays for one symbol; drops today's bar if the session has not closed (no partial bars in decisions)."""
    days = [ix.tz_convert(ET).date() for ix in d.index]
    now = datetime.now(ET)
    partial = None
    if days and days[-1] == now.date() and (now.hour, now.minute) < (16, 0):
        partial = float(d["close"].iloc[-1])
        d = d.iloc[:-1]; days = days[:-1]
    o, h, lo, c = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.c_[h - lo, np.abs(h - pc), np.abs(lo - pc)], axis=1)
    atr = np.array([np.nan] * 15 + [tr[i - 14:i].mean() for i in range(15, len(c))])
    sma = np.array([np.nan] * 200 + [c[i - 200:i].mean() for i in range(200, len(c))])
    roll10 = np.array([np.nan] * 9 + [c[t - 9:t + 1].max() for t in range(9, len(c))])
    dd10 = 1 - c / roll10
    dd90 = np.array([np.nan] * 259 + [np.quantile(dd10[i - 249:i + 1], 0.90) for i in range(259, len(c))])
    return dict(o=o, h=h, lo=lo, c=c, atr=atr, sma=sma, dd90=dd90, days=days, partial=partial)


def trades(p, fam=None, x=None):
    o, h, lo, c, atr, sma, dd90, days = (p[k] for k in ("o", "h", "lo", "c", "atr", "sma", "dd90", "days"))
    look = x if fam == "Q" else 20
    out, i, open_trade = [], 201, None
    while i < len(c) - 1:
        lvl = c[i - 55:i].max()
        if not (c[i] > lvl and c[i] > sma[i] and np.isfinite(atr[i]) and atr[i] > 0):
            i += 1; continue
        if fam == "D" and not np.isfinite(dd90[i]):
            i += 1; continue   # D needs 250 sessions of history before the signal; base-rule fallback would mix rules
        e_i = i + 1; entry = o[e_i]; A = atr[i]; risk = 2.5 * A
        stop = entry - risk; hi_close = entry; exit_px = None; j = e_i
        tgt = entry + x * risk if fam == "H" else None
        half_px = None; reason = None
        for j in range(e_i, min(len(c), e_i + 31)):
            if j > e_i and o[j] <= stop:
                exit_px = o[j]; reason = "stop"; break
            if tgt is not None and half_px is None and j > e_i and o[j] >= tgt:
                half_px = o[j]
            if lo[j] <= stop:                       # conservative: stop assumed before the target on the same bar
                exit_px = min(o[j], stop); reason = "stop"; break
            if tgt is not None and half_px is None and h[j] >= tgt:
                half_px = tgt
            lowN = lo[j - look:j].min()
            if c[j] < lowN or j - e_i + 1 >= 30:
                exit_px = c[j]; hi_close = max(hi_close, c[j]); reason = "lowN" if c[j] < lowN else "time"; break
            hi_close = max(hi_close, c[j])
            if fam == "W":
                stop = max(stop, hi_close - x * A)
            elif fam == "L" and hi_close >= entry + x * risk:
                stop = max(stop, entry + 0.5 * (hi_close - entry))
            elif fam == "D":
                stop = max(stop, hi_close * (1 - dd90[i]))
        if exit_px is None:
            open_trade = dict(sig=days[i], entry_day=days[e_i], entry=entry, A=A, risk=risk, stop=stop, hi_close=hi_close,
                              half_px=half_px, tgt=tgt, dd90=dd90[i], look=look, last_close=c[-1], last_day=days[-1],
                              lowN_next=lo[len(c) - look:].min(), held=len(c) - e_i)
            break
        rem = (exit_px - entry) / risk
        R = 0.5 * (half_px - entry) / risk + 0.5 * rem if half_px is not None else rem
        cost = COST_SIDE * (entry + (0.5 * (half_px + exit_px) if half_px is not None else exit_px)) / risk
        out.append(dict(y=days[e_i].year, R=R, Rn=R - cost, peak=(hi_close - entry) / risk, ed=days[e_i], why=reason))
        i = j + 1
    return out, open_trade


def stats(ts):
    if not ts:
        return dict(n=0)
    v = np.array([t["R"] for t in ts]); vn = np.array([t["Rn"] for t in ts])
    gb = [t["peak"] - t["R"] for t in ts if t["peak"] >= 1]
    return dict(n=len(v), mean=v.mean(), win=(v > 0).mean() * 100, med=np.median(v), p05=np.quantile(v, .05), tot=v.sum(),
                gb=np.mean(gb) if gb else float("nan"), ngb=len(gb), meann=vn.mean(), sd=v.std(ddof=1),
                skew=float(((v - v.mean()) ** 3).mean() / v.std() ** 3), kurt=float(((v - v.mean()) ** 4).mean() / v.var() ** 2))


def fmt(s):
    if s["n"] == 0:
        return "n=0"
    return (f"n={s['n']:4d} meanR={s['mean']:+.3f} win%={s['win']:4.1f} medR={s['med']:+.2f} p05={s['p05']:+.2f} "
            f"totR={s['tot']:+7.1f} giveback={s['gb']:.2f}(n{s['ngb']}) net5bp={s['meann']:+.3f}")


def run(P, fam=None, x=None):
    allt, opens = [], {}
    for s, p in P.items():
        ts, ot = trades(p, fam, x)
        allt += [dict(t, sym=s) for t in ts]
        if ot:
            opens[s] = ot
    return [t for t in allt if t["y"] < SPLIT], [t for t in allt if t["y"] >= SPLIT], opens


def paired_diff(a, b):
    """Paired t on trades with the same (symbol, entry date) in both runs: mean(a - b), t, n."""
    kb = {(t["sym"], t["ed"]): t["R"] for t in b}
    dif = np.array([t["R"] - kb[(t["sym"], t["ed"])] for t in a if (t["sym"], t["ed"]) in kb])
    if len(dif) < 3 or dif.std(ddof=1) == 0:
        return float("nan"), float("nan"), len(dif)
    return dif.mean(), dif.mean() / (dif.std(ddof=1) / math.sqrt(len(dif))), len(dif)


def dsr(sr_star, srs, s):
    """Deflated Sharpe (Bailey & Lopez de Prado 2014), per-trade SR, trials = len(srs)."""
    N = len(srs); V = np.var(srs, ddof=1); g = 0.5772156649
    sr0 = math.sqrt(V) * ((1 - g) * ND.inv_cdf(1 - 1 / N) + g * ND.inv_cdf(1 - 1 / (N * math.e)))
    den = math.sqrt(max(1e-12, 1 - s["skew"] * sr_star + (s["kurt"] - 1) / 4 * sr_star ** 2))
    return sr0, ND.cdf((sr_star - sr0) * math.sqrt(s["n"] - 1) / den)


def main():
    raw, src = load()
    P = {s: prep(d) for s, d in raw.items()}
    print(f"data: {src}; symbols={len(P)}; last completed bar={max(p['days'][-1] for p in P.values())}")
    grid = [("base", None, None)] + [(f"W z={z}", "W", z) for z in (4, 5, 6)] + [(f"L k={k}R", "L", k) for k in (2, 3, 4)] \
        + [(f"Q low={q}", "Q", q) for q in (10, 15)] + [(f"H half@{k}R", "H", k) for k in (2, 3)] + [("D dd90", "D", None)]
    res = {}
    for name, fam, x in grid:
        res[name] = (fam, x) + run(P, fam, x)
    # ---- discovery only: choose picks
    print("\n=== DISCOVERY 2020-2023 (selection made here) ===")
    sd = {k: stats(v[2]) for k, v in res.items()}
    for k in res:
        print(f"{k:12s} {fmt(sd[k])}")
    picks = {}
    for f in "WLQHD":
        picks[f] = max((sd[k]["mean"], k) for k in res if k.startswith(f))[1]
    print("discovery picks:", list(picks.values()))
    # ---- test years, read once
    print("\n=== TEST 2024-2026 (read once, after picks were fixed) ===")
    st = {k: stats(v[3]) for k, v in res.items()}
    for k in res:
        print(f"{k:12s} {fmt(st[k])}{'   <- pick' if k in picks.values() else ''}")
    # ---- verdicts
    b_d, b_t = sd["base"], st["base"]
    print("\n=== PICKS vs BASE ===")
    ntr = len(grid) - 1
    for _f, k in picks.items():
        dm, dt, dn = paired_diff(res[k][2], res["base"][2])
        tm, tt, tn = paired_diff(res[k][3], res["base"][3])
        p_one = 1 - ND.cdf(tt) if math.isfinite(tt) else float("nan")
        beats = sd[k]["mean"] > b_d["mean"] and st[k]["mean"] > b_t["mean"] and sd[k]["gb"] < b_d["gb"] and st[k]["gb"] < b_t["gb"]
        print(f"{k:12s} dMean disc {sd[k]['mean'] - b_d['mean']:+.3f} test {st[k]['mean'] - b_t['mean']:+.3f} | dGiveback disc "
              f"{sd[k]['gb'] - b_d['gb']:+.2f} test {st[k]['gb'] - b_t['gb']:+.2f} | paired test dR={tm:+.3f} t={tt:+.2f} (n={tn}) "
              f"one-sided p={p_one:.3f} Bonf x{ntr}={min(1, p_one * ntr):.3f} x{ntr + 27}={min(1, p_one * (ntr + 27)):.3f} | "
              f"beats base on both + cuts give-back: {beats}")
    print("\n=== GIVE-BACK CUT PER UNIT OF MEAN R LOST (all variants; disc | test) ===")
    for k in res:
        if k == "base":
            continue
        row = []
        for s, b in ((sd[k], b_d), (st[k], b_t)):
            dg, dmn = b["gb"] - s["gb"], b["mean"] - s["mean"]
            row.append(f"gb cut {dg:+.2f}R, meanR lost {dmn:+.3f}, ratio {dg / dmn if dmn > 0 else float('inf'):6.2f}")
        print(f"{k:12s} {row[0]} | {row[1]}")
    # ---- deflated Sharpe on the best discovery variant
    srs = [sd[k]["mean"] / sd[k]["sd"] for k in res]
    best = max((sd[k]["mean"], k) for k in res if k != "base")[1]
    sr0, p = dsr(sd[best]["mean"] / sd[best]["sd"], srs, sd[best])
    print(f"\nDSR: best discovery variant {best}: per-trade SR={sd[best]['mean'] / sd[best]['sd']:.3f}, expected max SR of "
          f"{len(srs)} trials under no skill={sr0:.3f}, DSR prob={p:.3f}. (Prior day's 27 candidates on the same data not in "
          f"this SR set; with them the hurdle only rises.)")
    print(f"candidates tried this run: {ntr} (+ base); test years read once for all of them after picks were fixed; "
          f"same 2024-26 data was also read for 27 candidates on 2026-11-04 -> cumulative {ntr + 27} looks at these test years.")
    # ---- BRK.B worked example
    print("\n=== BRK.B open trade, per variant, as of the last completed session ===")
    p = P.get("BRK.B")
    if p and p["partial"] is not None:
        print(f"today's partial-session last price (not used in decisions): {p['partial']:.2f}")
    for k, v in res.items():
        ot = v[4].get("BRK.B")
        if not ot:
            closed = [t for t in v[3] if t["sym"] == "BRK.B" and t["y"] == 2026][-1:]
            print(f"{k:12s} no open trade; last 2026 trade {[(str(t['ed']), round(t['R'], 2), t['why']) for t in closed]}")
            continue
        r = ot["risk"]
        line = (f"{k:12s} signal {ot['sig']} entry {ot['entry_day']} @ {ot['entry']:.2f} ATR {ot['A']:.2f} R$={r:.2f} held "
                f"{ot['held']} sessions; last close {ot['last_close']:.2f} ({(ot['last_close'] - ot['entry']) / r:+.2f}R) peak "
                f"close {ot['hi_close']:.2f} ({(ot['hi_close'] - ot['entry']) / r:+.2f}R); resting stop {ot['stop']:.2f} "
                f"({(ot['stop'] - ot['entry']) / r:+.2f}R); close-exit if close < {ot['lowN_next']:.2f} "
                f"(prior {ot['look']}-session low, {(ot['lowN_next'] - ot['entry']) / r:+.2f}R)")
        if ot["tgt"] is not None:
            line += f"; half-target {ot['tgt']:.2f} {'FILLED @ %.2f' % ot['half_px'] if ot['half_px'] else 'not hit'}"
        if v[0] == "D":
            line += f"; dd90 allowance {ot['dd90'] * 100:.1f}%"
        print(line)


if __name__ == "__main__":
    main()
