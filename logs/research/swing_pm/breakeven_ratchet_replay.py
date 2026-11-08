# ruff: noqa: E501  — dense research-report lines run long (project convention)
"""Swing breakout break-even ratchet replay — 2026-11-05. Owner rule under test: "once a Swing lot is well into profit, its
stop must never sit below the entry price." Read-only research: places no orders, touches no state, restarts nothing.

Reuses giveback_replay.py (which reuses breakout_remedies.py) exactly: same universe (UNIV), same T1 daily bars cache
(data.fetcher, SIP, split-adjusted), same entry rule (close > highest close of the prior 55 sessions and > SMA200 -> buy
the next session's open), same initial stop (entry - 2.5*ATR14, ATR = mean of the 14 true ranges before the signal bar,
fixed for the trade), same fills (a stop fills at min(open, stop); a close-based exit fills at the close), same 20-session-
low close exit, same 30-session time exit, one lot per name, R = (exit - entry) / (2.5*ATR). Costs: none in the headline
(as the base); a net column at 5 bps per side (assumption).

Families (the 20-session-low exit and the 30-session time exit stay on in every one):
 R   ratchet to break-even: once the highest close since entry >= entry + k*R, stop = max(stop, entry). k = 1, 1.5, 2, 3
 R+  ratchet plus cushion: once the highest close since entry >= entry + k*R, stop = max(stop, entry + 0.25R). k = 2, 3
 C   (extra, labelled: same rule on a CLOSE basis) once armed at +k*R, exit at the close when the close < entry; the
     intraday stop stays at the initial stop. k = 2, 3. Tests whether the cost is the intraday wick, not the rule.
Every stop change is computed from that day's close and takes effect from the next session (no same-bar look-ahead).
Selection: best mean R on 2020-2023 only; 2024-2026 printed once after the pick is fixed.
SWING-RATCHET-1105 (owner-approved rule R+ k=2, a risk-shape choice, not an edge pick): after the report the run writes
logs/research/swing_pm/ratchet_params.json (trigger, lock, tick, the replay's own numbers for that variant, the trial
count), atomically. execution/swing_breakout_manager.py reads its trigger and lock from that file only; re-run each
quarter on the tier's universe to re-derive it. `--no-params` prints the report without writing the file.
Usage (server, repo root, .env exported): venv/bin/python3 logs/research/swing_pm/breakeven_ratchet_replay.py"""
import json
import math
import os
import sys
from datetime import date

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from giveback_replay import COST_SIDE, ND, SPLIT, fmt, load, paired_diff, prep, stats  # noqa: E402

PRIOR_LOOKS = 38   # 27 candidates (2026-11-04) + 11 (2026-11-05 give-back) on the same 2024-26 test years
LOCK_R = 0.25      # the R+ family's cushion above entry, in R (the variant the owner chose: R+ k=2)
OWNER_VARIANT = ("R+", 2)          # SWING-RATCHET-1105: the rule the owner approved (risk shape, not the mean-R pick)
TICK_USD = 0.01    # SEC Rule 612: stop prices for stocks >= $1 are quoted in whole cents
PARAMS_PATH = os.path.join(HERE, "ratchet_params.json")


def trades(p, fam=None, k=None):
    o, h, lo, c, atr, sma, days = (p[x] for x in ("o", "h", "lo", "c", "atr", "sma", "days"))
    out, i, open_trade = [], 201, None
    while i < len(c) - 1:
        lvl = c[i - 55:i].max()
        if not (c[i] > lvl and c[i] > sma[i] and np.isfinite(atr[i]) and atr[i] > 0):
            i += 1
            continue
        e_i = i + 1
        entry = o[e_i]
        A = atr[i]
        risk = 2.5 * A
        stop0 = entry - risk
        stop = stop0
        hi_close = entry
        exit_px = None
        j = e_i
        reason = None
        armed = False
        armed_day = None
        lock = entry + (LOCK_R * risk if fam == "R+" else 0.0)
        for j in range(e_i, min(len(c), e_i + 31)):
            if j > e_i and o[j] <= stop:
                exit_px = o[j]
                reason = "ratchet" if stop > stop0 else "stop"
                break
            if lo[j] <= stop:
                exit_px = min(o[j], stop)
                reason = "ratchet" if stop > stop0 else "stop"
                break
            low20 = lo[j - 20:j].min()
            if fam == "C" and armed and c[j] < entry:
                exit_px = c[j]
                reason = "ratchet"
                break
            if c[j] < low20 or j - e_i + 1 >= 30:
                exit_px = c[j]
                hi_close = max(hi_close, c[j])
                reason = "low20" if c[j] < low20 else "time"
                break
            hi_close = max(hi_close, c[j])
            if fam in ("R", "R+", "C") and not armed and hi_close >= entry + k * risk:
                armed = True
                armed_day = days[j]
            if fam in ("R", "R+") and armed:
                stop = max(stop, lock)
        if exit_px is None:
            open_trade = dict(sig=days[i], entry_day=days[e_i], entry=entry, A=A, risk=risk, stop=stop, hi_close=hi_close,
                              armed=armed, armed_day=armed_day, last_close=c[-1], last_day=days[-1],
                              low20_next=lo[len(c) - 20:].min(), held=len(c) - e_i)
            break
        R = (exit_px - entry) / risk
        cost = COST_SIDE * (entry + exit_px) / risk
        out.append(dict(y=days[e_i].year, R=R, Rn=R - cost, peak=(hi_close - entry) / risk, ed=days[e_i], why=reason,
                        armed=armed))
        i = j + 1
    return out, open_trade


def run(P, fam=None, k=None):
    allt, opens = [], {}
    for s, p in P.items():
        ts, ot = trades(p, fam, k)
        allt += [dict(t, sym=s) for t in ts]
        if ot:
            opens[s] = ot
    return [t for t in allt if t["y"] < SPLIT], [t for t in allt if t["y"] >= SPLIT], opens


def decompose(var, base):
    """Per matched trade (same symbol + entry date): cut-short (ratchet exit below base R), saved (ratchet exit above base R).
    Unmatched trades come from the one-lot-per-name chain shifting (an earlier exit frees the name for a new entry)."""
    kb = {(t["sym"], t["ed"]): t for t in base}
    cut, saved, cut2, other = [], [], 0, []
    for t in var:
        b = kb.get((t["sym"], t["ed"]))
        if b is None:
            continue
        d = t["R"] - b["R"]
        if t["why"] == "ratchet" and d < 0:
            cut.append(d)
            if b["R"] > 2:
                cut2 += 1
        elif t["why"] == "ratchet" and d > 0:
            saved.append(d)
        else:
            other.append(d)
    kv = {(t["sym"], t["ed"]) for t in var}
    un_v = [t["R"] for t in var if (t["sym"], t["ed"]) not in kb]
    un_b = [t["R"] for t in base if (t["sym"], t["ed"]) not in kv]
    n = len(base)
    return dict(ncut=len(cut), cutR=sum(cut), nsaved=len(saved), savedR=sum(saved), cut2=cut2, otherR=sum(other),
                n_new=len(un_v), newR=sum(un_v), n_lost=len(un_b), lostR=sum(un_b), n=n)


def main():
    raw, src = load()
    P = {s: prep(d) for s, d in raw.items()}
    print(f"data: {src}; symbols={len(P)}; last completed bar={max(p['days'][-1] for p in P.values())}")
    grid = [("base", None, None)] + [(f"R k={k}", "R", k) for k in (1, 1.5, 2, 3)] + [(f"R+ k={k}", "R+", k) for k in (2, 3)] \
        + [(f"C k={k}", "C", k) for k in (2, 3)]
    res = {name: (fam, k) + run(P, fam, k) for name, fam, k in grid}
    ntr = len(grid) - 1

    print("\n=== DISCOVERY 2020-2023 (selection made here) ===")
    sd = {n: stats(v[2]) for n, v in res.items()}
    for n in res:
        print(f"{n:10s} {fmt(sd[n])}")
    pick = max((sd[n]["mean"], n) for n in res if n != "base")[1]
    pick_owner = max((sd[n]["mean"], n) for n in res if n.startswith("R"))[1]   # best of the owner's R / R+ families only
    print(f"discovery pick (all families): {pick} | best of the owner's R/R+ families: {pick_owner}")

    print("\n=== TEST 2024-2026 (printed once, after the picks were fixed) ===")
    st = {n: stats(v[3]) for n, v in res.items()}
    for n in res:
        tag = "   <- pick" if n == pick else ("   <- best R/R+" if n == pick_owner else "")
        print(f"{n:10s} {fmt(st[n])}{tag}")

    print("\n=== EVERY VARIANT vs BASE, same trades (paired on symbol + entry date) ===")
    for n in res:
        if n == "base":
            continue
        dm, dt, dn = paired_diff(res[n][2], res["base"][2])
        tm, tt, tn = paired_diff(res[n][3], res["base"][3])
        print(f"{n:10s} mean dR disc {sd[n]['mean'] - sd['base']['mean']:+.3f} test {st[n]['mean'] - st['base']['mean']:+.3f} | "
              f"paired disc dR={dm:+.3f} t={dt:+.2f} (n={dn}) | paired test dR={tm:+.3f} t={tt:+.2f} (n={tn})")
    for label, n in (("PICK", pick), ("BEST R/R+", pick_owner)):
        tm, tt, tn = paired_diff(res[n][3], res["base"][3])
        p_one = 1 - ND.cdf(tt) if math.isfinite(tt) else float("nan")
        looks = PRIOR_LOOKS + ntr
        print(f"{label} {n}: test paired dR={tm:+.3f} t={tt:+.2f} n={tn}, one-sided p(better)={p_one:.3f}, "
              f"Bonferroni x{ntr}={min(1, p_one * ntr):.3f}, x{looks}={min(1, p_one * looks):.3f}")

    print("\n=== WHERE THE DIFFERENCE COMES FROM (matched trades; R summed) ===")
    for n in res:
        if n == "base":
            continue
        for lab, a, b in (("disc", res[n][2], res["base"][2]), ("test", res[n][3], res["base"][3])):
            x = decompose(a, b)
            print(f"{n:10s} {lab}: cut short {x['ncut']:3d} trades ({x['cutR']:+6.1f}R; {x['cut2']:2d} of them base exit > +2R) | "
                  f"saved {x['nsaved']:3d} ({x['savedR']:+6.1f}R) | other matched {x['otherR']:+5.1f}R | chain: +{x['n_new']} new "
                  f"trades {x['newR']:+5.1f}R, -{x['n_lost']} base trades {-x['lostR']:+5.1f}R")

    print("\n=== Q2: base exits above +2R, variant stops out at its ratchet level (counts, matched trades) ===")
    for n in res:
        if n == "base":
            continue
        row = []
        for lab, a, b in (("disc", res[n][2], res["base"][2]), ("test", res[n][3], res["base"][3])):
            kb = {(t["sym"], t["ed"]): t for t in b}
            hits = [(t, kb[(t["sym"], t["ed"])]) for t in a if (t["sym"], t["ed"]) in kb and t["why"] == "ratchet"
                    and kb[(t["sym"], t["ed"])]["R"] > 2]
            base_gt2 = sum(1 for t in b if t["R"] > 2)
            lostR = sum(bb["R"] - t["R"] for t, bb in hits)
            row.append(f"{lab} {len(hits):3d} of {base_gt2} base >+2R winners cut (gave up {lostR:5.1f}R)")
        print(f"{n:10s} " + " | ".join(row))
    # base trades that reached +2R and still closed at a loss (what the rule is meant to stop)
    for lab, b in (("disc", res["base"][2]), ("test", res["base"][3])):
        bad = [t for t in b if t["peak"] >= 2 and t["R"] < 0]
        reached = sum(1 for t in b if t["peak"] >= 2)
        print(f"base {lab}: {reached} trades reached a +2R close; {len(bad)} of them still closed below entry "
              f"(sum {sum(t['R'] for t in bad):+.1f}R)")

    print("\n=== INDEPENDENT RE-PRICE from raw bars (not the replay loop): test trades with base > +2R cut by R k=2 ===")
    kb = {(t["sym"], t["ed"]): t for t in res["base"][3]}
    for t in res["R k=2"][3]:
        b = kb.get((t["sym"], t["ed"]))
        if not b or t["why"] != "ratchet" or b["R"] <= 2:
            continue
        d = raw[t["sym"]]
        dd = [ix.tz_convert("America/New_York").date() for ix in d.index]
        o, h, lo, c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        e = dd.index(t["ed"])
        s = e - 1
        pc = np.r_[np.nan, c[:-1]]
        tr = np.nanmax(np.c_[h - lo, np.abs(h - pc), np.abs(lo - pc)], axis=1)
        A = tr[s - 14:s].mean()
        R1 = 2.5 * A
        entry = o[e]
        arm = next(j for j in range(e, e + 31) if c[e:j + 1].max() >= entry + 2 * R1)
        hit = next(j for j in range(arm + 1, e + 31) if lo[j] <= entry)
        px = min(o[hit], entry)
        print(f"  {t['sym']:6s} entry {dd[e]} @ {entry:.2f}, 1R ${R1:.2f}; +2R close on {dd[arm]} ({c[arm]:.2f}); low {lo[hit]:.2f} "
              f"on {dd[hit]} -> fill {px:.2f} = {(px - entry) / R1:+.2f}R (replay {t['R']:+.2f}R) | base exit {b['R']:+.2f}R ({b['why']})")

    print(f"\ncandidates this run: {ntr} (R x4, R+ x2, C x2) + base; test years computed for all after the picks were fixed. "
          f"Cumulative looks at the 2024-26 test years: {PRIOR_LOOKS} earlier + {ntr} = {PRIOR_LOOKS + ntr}.")

    print("\n=== BRK.B ===")
    p = P.get("BRK.B")
    if p and p["partial"] is not None:
        print(f"today's partial-session last price (not used): {p['partial']:.2f}")
    # the live lot's own entry stop distance (state file: entry 544.60, entry stop 527.81); rev 1 used 16.39 from the
    # replay's ATR — corrected (design record rev 1)
    live_entry, live_R, live_peak = 544.60, 16.79, 579.09
    live_stop0 = live_entry - live_R
    if p:
        dd = p["days"]
        idx = [t for t, d in enumerate(dd) if str(d) >= "2026-10-27"]
        if idx:
            pk = max(p["c"][t] for t in idx)
            print(f"bars check: highest close since 2026-10-27 = {pk:.2f}; last close {p['c'][-1]:.2f} on {dd[-1]}; "
                  f"prior 20-session low (close-exit level) {p['lo'][-20:].min():.2f}")
    print(f"LIVE lot: entry {live_entry:.2f}, 1R ${live_R:.2f}, initial stop {live_stop0:.2f}, peak close {live_peak:.2f} "
          f"= {(live_peak - live_entry) / live_R:+.2f}R")
    for n, (fam, k, *_r) in res.items():
        if fam is None:
            s, note = live_stop0, "base: initial stop"
        else:
            armed = live_peak >= live_entry + k * live_R
            if fam == "C":
                s = live_stop0
                note = (f"armed: exit at the close if a close < {live_entry:.2f}; intraday stop {s:.2f}" if armed
                        else f"not armed (needs close >= {live_entry + k * live_R:.2f})")
            else:
                lockp = live_entry + (LOCK_R * live_R if fam == "R+" else 0.0)
                s = max(live_stop0, lockp) if armed else live_stop0
                note = "armed" if armed else f"not armed (needs close >= {live_entry + k * live_R:.2f})"
        print(f"  {n:10s} stop {s:8.2f} ({(s - live_entry) / live_R:+.2f}R)  {note}")
    print("REPLAY lot (replay enters on the first breakout's next open, which live did not take):")
    for n, v in res.items():
        ot = v[4].get("BRK.B")
        if not ot:
            print(f"  {n:10s} no open replay trade")
            continue
        r = ot["risk"]
        print(f"  {n:10s} entry {ot['entry_day']} @ {ot['entry']:.2f} R$={r:.2f} peak {ot['hi_close']:.2f} "
              f"({(ot['hi_close'] - ot['entry']) / r:+.2f}R) stop {ot['stop']:.2f} ({(ot['stop'] - ot['entry']) / r:+.2f}R)"
              f"{' armed ' + str(ot['armed_day']) if ot['armed'] else ''}")
    if "--no-params" not in sys.argv:
        write_params(res, sd, st, ntr, src)


def params_record(res, sd, st, ntr, src):
    """The parameter record for the owner's variant, built from this run's own results."""
    fam, k = OWNER_VARIANT
    name = f"{fam} k={k}"
    if name not in res:
        raise SystemExit(f"owner variant {name} missing from the grid — params not written")
    dm, dt, dn = paired_diff(res[name][3], res["base"][3])
    return {
        "version": 1,
        "change": "swing-ratchet-1105",
        "variant": name,
        "trigger_r": float(k),
        "lock_r": float(LOCK_R),
        "tick_usd": TICK_USD,
        "produced": date.today().isoformat(),
        "replay": "logs/research/swing_pm/breakeven_ratchet_replay.py",
        "data": str(src),
        "trial_count": PRIOR_LOOKS + ntr,
        "selection": "owner risk-shape choice (no +2R winner becomes a loss); not the best-mean-R pick",
        "discovery": {"n": int(sd[name]["n"]), "mean_r": round(float(sd[name]["mean"]), 4),
                      "base_mean_r": round(float(sd["base"]["mean"]), 4)},
        "test": {"n": int(st[name]["n"]), "mean_r": round(float(st[name]["mean"]), 4),
                 "base_mean_r": round(float(st["base"]["mean"]), 4), "paired_dr": round(float(dm), 4),
                 "paired_t": round(float(dt), 2), "paired_n": int(dn)},
        "rederive": "quarterly, on the Swing tier's universe",
    }


def write_params(res, sd, st, ntr, src, path=PARAMS_PATH):
    """Write the record atomically (tmp -> fsync -> replace)."""
    rec = params_record(res, sd, st, ntr, src)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print(f"\nwrote {path}: trigger {rec['trigger_r']}R, lock {rec['lock_r']}R, trials {rec['trial_count']}")


if __name__ == "__main__":
    main()
