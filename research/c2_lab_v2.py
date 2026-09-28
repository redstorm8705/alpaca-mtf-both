#!/usr/bin/env python3
# ruff: noqa: E501  — long formula/citation lines (project research convention, cf. deflated_sharpe.py)
"""
research/c2_lab_v2.py — Confluence 2.0 lab step 5: harness v2 + pre-registered event families.

Pre-registered in logs/design_records/entry_rebuild_2026-09-26.md ("Lab step 5 ... PRE-REGISTRATION"),
approved design update 2026-09-28. Differences from labs 3-4 (board 4/4 + GAI):
  * gap-aware fills: a session that OPENS beyond the stop/target fills at that open;
  * spread-aware cost by default: 10 bps + 0.02 x ATR% round trip;
  * metric = excess R over the EXACT characteristic-matched random expectation (mean R of all eligible
    names on the same day, same direction, same ATR% decile) instead of sampled random draws;
  * significance on a daily excess series: Newey-West t (lag = horizon), DSR against the family's
    eigenvalue effective trial count, PBO by CSCV over half-year blocks within the family;
  * Mag-7 slice and per-regime (SPY vs SMA200 x SPY 20d realized-vol tercile) reporting;
  * every run appended to a trial ledger.

Research only. Never imported by the bot; writes only logs/lab/c2/.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import c2_swing_lab as base  # noqa: E402
from c2_trigger_lab import _cross_up, _retest_hold, pbo_cscv  # noqa: E402
from deflated_sharpe import deflated_sharpe  # noqa: E402

_OUT = base._OUT
FIRST_TEST_YEAR = 2019
COST_BPS = 10.0
COST_ATR_FRAC = 0.02
MAG7 = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"}
SL_ATR, TP_ATR = 1.5, 2.5


# ------------------------------------------------------------------------------------- labels
def label_r(wide, a14: pd.DataFrame, direction: int, horizon: int) -> np.ndarray:
    """Costed R for a trade decided at close t, entered at open t+1. Stop SL_ATR*ATR(t), target
    TP_ATR*ATR(t). Session k=1 is the entry day (its open IS the entry, so no gap test); for k>=2 a
    session that opens beyond the stop (or target) fills at that open. Then intrabar: stop first on a
    same-bar hit. Vertical exit at the close of session `horizon`."""
    o, h, lo, c = (wide[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = a14.to_numpy()
    T, N = o.shape
    R = np.full((T, N), np.nan)
    entry = np.full((T, N), np.nan)
    entry[:-1] = o[1:]
    risk = SL_ATR * a
    stop = entry - direction * risk
    tgt = entry + direction * TP_ATR * a
    done = np.zeros((T, N), dtype=bool)

    def shifted(x, k):
        y = np.full((T, N), np.nan)
        y[:-k] = x[k:]
        return y

    with np.errstate(invalid="ignore", divide="ignore"):
        for k in range(1, horizon + 1):
            if k >= T:
                break
            oo, hh, ll, cc = shifted(o, k), shifted(h, k), shifted(lo, k), shifted(c, k)
            if k >= 2:
                if direction == 1:
                    gap_s, gap_t = oo <= stop, oo >= tgt
                else:
                    gap_s, gap_t = oo >= stop, oo <= tgt
                g = (gap_s | gap_t) & ~done
                R[g] = (direction * (oo - entry) / risk)[g]
                done |= g
            if direction == 1:
                hit_s, hit_t = ll <= stop, hh >= tgt
            else:
                hit_s, hit_t = hh >= stop, ll <= tgt
            s_now = hit_s & ~done
            R[s_now] = -1.0
            done |= s_now
            t_now = hit_t & ~done
            R[t_now] = TP_ATR / SL_ATR
            done |= t_now
            if k == horizon:
                v = ~done & np.isfinite(cc)
                R[v] = (direction * (cc - entry) / risk)[v]
                done |= v
        atr_pct = a / c
        R = R - (COST_BPS / 1e4 + COST_ATR_FRAC * atr_pct) * entry / risk
    R[~np.isfinite(R)] = np.nan
    return R


def matched_expectation(R: np.ndarray, elig: np.ndarray, atr_pct: np.ndarray) -> np.ndarray:
    """E[R | day, ATR% decile] over ALL eligible names — the exact random-pick expectation."""
    T, N = R.shape
    ap = np.where(elig, atr_pct, np.nan)
    rank = pd.DataFrame(ap).rank(axis=1, pct=True).to_numpy()
    dec = np.clip(np.floor(rank * 10), 0, 9)
    E = np.full((T, N), np.nan)
    Rm = np.where(elig & np.isfinite(R), R, np.nan)
    for q in range(10):
        m = dec == q
        vals = np.where(m, Rm, np.nan)
        cnt = np.sum(np.isfinite(vals), axis=1)
        with np.errstate(invalid="ignore"):
            mean_q = np.where(cnt > 0, np.nansum(vals, axis=1) / np.maximum(cnt, 1), np.nan)
        E = np.where(m, mean_q[:, None], E)
    return E


# ------------------------------------------------------------------------------------- events
def build_events(wide, a14: pd.DataFrame, elig: np.ndarray) -> dict[str, tuple[str, int, np.ndarray]]:
    """name -> (family, direction, bool matrix). Parameters are fixed by the pre-registration."""
    cdf = wide["close"]
    c = cdf.to_numpy()
    o = wide["open"].to_numpy()
    a = a14.to_numpy()
    lo = wide["low"].to_numpy()
    sma200 = cdf.rolling(200, min_periods=200).mean().to_numpy()
    sma20 = cdf.rolling(20, min_periods=20).mean().to_numpy()
    rsi2 = base.rsi(cdf, 2).to_numpy()
    down = (cdf < cdf.shift(1))
    three_down = (down & down.shift(1, fill_value=False) & down.shift(2, fill_value=False)).to_numpy()
    r21 = (cdf / cdf.shift(21) - 1).where(pd.DataFrame(elig, index=cdf.index, columns=cdf.columns))
    r5 = (cdf / cdf.shift(5) - 1).where(pd.DataFrame(elig, index=cdf.index, columns=cdf.columns))
    r21_rank = r21.rank(axis=1, pct=True).to_numpy()
    r5_rank = r5.rank(axis=1, pct=True).to_numpy()
    gap = (wide["open"] - cdf.shift(1)).abs() >= 3 * a14.shift(1)
    big_gap_5d = gap.rolling(5, min_periods=1).max().fillna(0).astype(bool).to_numpy()
    hi252 = wide["high"].shift(1).rolling(252, min_periods=252).max().to_numpy()
    with np.errstate(invalid="ignore"):
        up = c > sma200
        dn = c < sma200
        ev = {
            "L1": ("LONG-MR", 1, up & (rsi2 < 10)),
            "L2": ("LONG-MR", 1, up & three_down),
            "L3": ("LONG-MR", 1, up & (r21_rank <= 0.02)),
            "B1": ("LONG-BRK", 1, _retest_hold(_cross_up(c, hi252), hi252, c, lo, a, +1)),
            "S1": ("SHORT-MR", -1, dn & (rsi2 > 90)),
            "S2": ("SHORT-MR", -1, up & (rsi2 > 95) & (c > sma20 + 3 * a)),
            "S3": ("SHORT-MR", -1, (r5_rank >= 0.98) & ~big_gap_5d),
        }
    del o
    return {k: (f, d, m & elig) for k, (f, d, m) in ev.items()}


def take_trades(ev: np.ndarray, cols, horizon: int) -> list[tuple[int, int]]:
    """Every event, one open trade per stock (cooldown = horizon, keyed by base ticker)."""
    base_of = [x.split("@")[0] for x in cols]
    last: dict[str, int] = {}
    out = []
    for i in range(ev.shape[0]):
        for j in np.nonzero(ev[i])[0]:
            b = base_of[j]
            if i - last.get(b, -10**9) < horizon:
                continue
            out.append((i, j))
            last[b] = i
    return out


# --------------------------------------------------------------------------------- statistics
def newey_west_t(x: np.ndarray, lag: int) -> float:
    x = x[np.isfinite(x)]
    n = x.size
    if n < 30:
        return float("nan")
    mu = x.mean()
    e = x - mu
    s = float(e @ e) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1 - k / (lag + 1)
        s += 2 * w * float(e[k:] @ e[:-k]) / n
    if s <= 0:
        return float("nan")
    return float(mu / math.sqrt(s / n))


def effective_n(series: dict[str, pd.Series]) -> int:
    """Eigenvalue effective number of trials: (sum l)^2 / sum l^2 of the correlation matrix."""
    if len(series) <= 1:
        return len(series)
    df = pd.DataFrame(series).fillna(0.0)
    corr = np.nan_to_num(df.corr().to_numpy(), nan=0.0)
    np.fill_diagonal(corr, 1.0)
    lam = np.clip(np.linalg.eigvalsh(corr), 0, None)
    return max(1, int(math.ceil(lam.sum() ** 2 / float((lam ** 2).sum()))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v2")
    args = ap.parse_args()
    _OUT.mkdir(parents=True, exist_ok=True)

    wide, mask, _etfs = base.load_panel()
    c = wide["close"]
    idx = c.index
    dvol = (c * wide["volume"]).rolling(20, min_periods=15).median()
    a14 = base.atr(wide)
    elig_df = (mask & (c >= base.MIN_PRICE) & (dvol >= base.MIN_DOLLAR_VOL)
               & wide["open"].shift(-1).notna() & a14.notna() & (a14 > 0))
    test = (idx.year >= FIRST_TEST_YEAR)
    elig = elig_df.to_numpy() & test[:, None]
    cols = list(c.columns)
    atr_pct = (a14 / c).to_numpy()
    yr = idx.year.to_numpy()
    half = np.array([f"{y}H{1 if m <= 6 else 2}" for y, m in zip(idx.year, idx.month, strict=True)])
    mag = np.array([x.split("@")[0] in MAG7 for x in cols])

    spy = _etfs["SPY"]["close"]
    spy_up = (spy > spy.rolling(200, min_periods=200).mean()).to_numpy()
    rv = spy.pct_change().rolling(20, min_periods=20).std()
    q1 = rv.rolling(504, min_periods=252).quantile(1 / 3)
    q2 = rv.rolling(504, min_periods=252).quantile(2 / 3)
    vol_t = np.where(rv <= q1, "lowvol", np.where(rv <= q2, "midvol", "highvol"))
    vol_t = np.where(q1.isna(), "na", vol_t)
    regime = np.array([f"{'up' if u else 'down'}/{v}" for u, v in zip(spy_up, vol_t, strict=True)])

    events = build_events(wide, a14, elig)
    labels_for = {"LONG-MR": [5, 10], "SHORT-MR": [5, 10], "LONG-BRK": [20]}
    cache: dict = {}
    runs: dict = {}
    fam_series: dict[str, dict[str, pd.Series]] = {}
    fam_blocks: dict[str, dict[str, np.ndarray]] = {}
    years = sorted({int(y) for y in yr[test]})
    blocks = sorted(set(half[test]))
    for name, (fam, d, ev) in events.items():
        for H in labels_for[fam]:
            if (d, H) not in cache:
                R = label_r(wide, a14, d, H)
                E = matched_expectation(R, elig, atr_pct)
                cache[(d, H)] = (R, E)
            R, E = cache[(d, H)]
            tr = take_trades(ev, cols, H)
            ii = np.array([i for i, _ in tr], dtype=int)
            jj = np.array([j for _, j in tr], dtype=int)
            if ii.size == 0:
                continue
            r = R[ii, jj]
            x = r - E[ii, jj]
            ok = np.isfinite(r) & np.isfinite(x)
            ii, jj, r, x = ii[ok], jj[ok], r[ok], x[ok]
            key = f"{name}|h{H}"
            daily = pd.Series(x).groupby(ii).mean()
            daily.index = idx[daily.index]
            nw = newey_west_t(daily.to_numpy(), H)
            sd = float(daily.std(ddof=1)) if daily.size > 2 else float("nan")
            by_year = {str(y): float(x[yr[ii] == y].mean()) if (yr[ii] == y).any() else float("nan") for y in years}
            reg = {g: {"n": int((regime[ii] == g).sum()), "excess": float(x[regime[ii] == g].mean())}
                   for g in sorted(set(regime[ii]))}
            runs[key] = {
                "family": fam, "direction": d, "horizon": H, "n": int(x.size),
                "per_day": float(x.size / max(1, test.sum())),
                "mean_R": float(r.mean()), "mean_excess": float(x.mean()), "win": float((r > 0).mean()),
                "mean_excess_daywt": float(daily.mean()),
                "nw_t": nw, "daily_sr": float(daily.mean() / sd) if sd and sd > 0 else float("nan"),
                "years_pos_excess": int(sum(1 for v in by_year.values() if np.isfinite(v) and v > 0)),
                "by_year_excess": by_year,
                "mag7": {"n": int(mag[jj].sum()), "mean_excess": float(x[mag[jj]].mean()) if mag[jj].any() else float("nan"),
                         "mean_R": float(r[mag[jj]].mean()) if mag[jj].any() else float("nan")},
                "regime": reg,
            }
            fam_series.setdefault(fam, {})[key] = daily
            fam_blocks.setdefault(fam, {})[key] = np.array(
                [x[half[ii] == b].mean() if (half[ii] == b).any() else np.nan for b in blocks])
            print(key, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in runs[key].items()
                        if k in ("n", "per_day", "mean_R", "mean_excess", "nw_t", "years_pos_excess")}, flush=True)

    fam_rate = {f: float(sum(runs[k]["per_day"] for k in ser if k.endswith(f"h{min(labels_for[f])}")))
                for f, ser in fam_series.items()}
    set_rate = float(sum(fam_rate.values()))   # pre-registered: >= 1.2 events/day for the family set
    fam_stats = {}
    for fam, ser in fam_series.items():
        n_eff = effective_n(ser)
        srs = [runs[k]["daily_sr"] for k in ser]
        sr_std = float(np.nanstd(srs, ddof=1)) if len(srs) > 1 else float("nan")
        mat = np.vstack([fam_blocks[fam][k] for k in ser])
        pbo = pbo_cscv(mat) if len(ser) > 1 else float("nan")
        fam_stats[fam] = {"variants": len(ser), "n_eff": n_eff, "sr_std": sr_std, "pbo": pbo}
        for k, s in ser.items():
            rk = runs[k]
            if len(ser) > 1 and np.isfinite(sr_std) and sr_std > 0:
                dsr = deflated_sharpe(s.to_numpy(), n_trials=n_eff, trials_sr_std=sr_std).dsr
            else:
                dsr = float("nan")
            rk["dsr"] = dsr
            short_ok = rk["direction"] == 1 or rk["mean_R"] > 0
            rk["passes"] = bool(np.isfinite(rk["nw_t"]) and rk["nw_t"] >= 3.0 and np.isfinite(dsr) and dsr >= 0.9
                                and np.isfinite(pbo) and pbo <= 0.05 and rk["years_pos_excess"] >= 6 and short_ok
                                and np.isfinite(rk["mag7"]["mean_excess"]) and rk["mag7"]["mean_excess"] > 0
                                and set_rate >= 1.2)

    out = {"generated": datetime.now(base.ET).isoformat(), "tag": args.tag,
           "params": {"SL_ATR": SL_ATR, "TP_ATR": TP_ATR, "COST_BPS": COST_BPS, "COST_ATR_FRAC": COST_ATR_FRAC,
                      "first_test_year": FIRST_TEST_YEAR},
           "family_stats": fam_stats, "family_events_per_day": fam_rate, "set_events_per_day": set_rate, "runs": runs}
    p = _OUT / f"c2_lab_v2_{args.tag}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1, default=str))
    tmp.replace(p)
    ledger = _OUT / "trial_ledger.jsonl"
    with open(ledger, "a") as fh:
        for k, v in runs.items():
            fh.write(json.dumps({"ts": out["generated"], "tool": "c2_lab_v2", "tag": args.tag, "variant": k,
                                 "mean_excess": v["mean_excess"], "nw_t": v["nw_t"], "passes": v["passes"]}) + "\n")
    print("family_stats", json.dumps(fam_stats))
    for k, v in runs.items():
        print(f"{k:10s} n={v['n']:6d} /day={v['per_day']:.2f} R={v['mean_R']:+.3f} excess={v['mean_excess']:+.3f} "
              f"t={v['nw_t']:+.2f} dsr={v['dsr']:.2f} yrs+={v['years_pos_excess']}/8 "
              f"mag7={v['mag7']['mean_excess']:+.3f}(n={v['mag7']['n']}) pass={v['passes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
