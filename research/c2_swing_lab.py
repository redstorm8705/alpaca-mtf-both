#!/usr/bin/env python3
# ruff: noqa: E501  — long formula/citation lines (project research convention, cf. deflated_sharpe.py)
"""
research/c2_swing_lab.py — Confluence 2.0 swing-feature walk-forward test (lab step 3).

Question: do the BGGN-aligned daily/weekly swing features (design record
logs/design_records/entry_rebuild_2026-09-26.md §Confluence 2.0) pick better 2-20 day swing
entries than (a) a random pick from the same point-in-time universe and (b) a daily-bar proxy
of today's 12-point score, OUT OF SAMPLE, after costs?

Data (offline, no network): data/cache/lab/bars/1Day/*.csv.gz (lab step 2, split-adjusted SIP
daily bars) + logs/lab/universe/*_membership.csv (lab step 1, point-in-time S&P 500 / NDX).

No look-ahead: every feature at date t uses bars with timestamp <= t (closed daily bars); entry
is the NEXT session's open; sector assignment and feature weights are fit only on data before
each test year (walk-forward, 25-day purge+embargo gap before the test year).

Research only. Never imported by the bot; writes only logs/lab/c2/.
"""
from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
_BARS = _ROOT / "data" / "cache" / "lab" / "bars" / "1Day"
_UNIV = _ROOT / "logs" / "lab" / "universe"
_OUT = _ROOT / "logs" / "lab" / "c2"
ET = ZoneInfo("America/New_York")

SECTOR_ETFS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]
FEATURES = ["hi52", "resmom_fip", "sector_rs", "wk_trend", "eff_ratio"]
# Label (design record: TP 2.5 ATR, SL = stop, vertical 20 trading days)
SL_ATR = 1.5
TP_ATR = 2.5
HORIZON = 20
COST_BPS_ROUND_TRIP = 10.0      # 5 bps a side
GAP_DAYS = HORIZON + 5          # purge (label length) + embargo (5); re-derived from --horizon in main()
MIN_PRICE = 5.0
N_RANDOM = 100                  # random-baseline draws (20 gave a seed-unstable 5th-95th band)
MIN_DOLLAR_VOL = 20e6           # 20-day median dollar volume


# ----------------------------------------------------------------------------------------- data
def _read(key: str) -> pd.DataFrame | None:
    p = _BARS / f"{key}.csv.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt") as fh:
        df = pd.read_csv(fh)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["timestamp"].str[:10])
    return df.drop(columns=["timestamp"]).drop_duplicates("date").set_index("date").sort_index()


def _parse_start(s: str) -> pd.Timestamp:
    s = str(s).strip()
    return pd.Timestamp("1900-01-01") if s.startswith("<=") else pd.Timestamp(s)


def load_panel() -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, pd.DataFrame]]:
    """Return (wide OHLCV frames keyed by field, membership mask, ETF frames)."""
    mem = pd.concat([pd.read_csv(_UNIV / f, dtype=str, keep_default_na=False)
                     for f in ("sp500_membership.csv", "ndx_membership.csv")], ignore_index=True)
    intervals: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for _, r in mem.iterrows():
        end = r["end"].strip()
        key = r["ticker"] if not end else f"{r['ticker']}@{end}"
        e = pd.Timestamp(end) if end else pd.Timestamp("2100-01-01")
        intervals.setdefault(key, []).append((_parse_start(r["start"]), e))

    fields: dict[str, dict[str, pd.Series]] = {k: {} for k in ("open", "high", "low", "close", "volume")}
    for key in intervals:
        df = _read(key)
        if df is None or len(df) < 30:
            continue
        for k in fields:
            fields[k][key] = df[k].astype(float)
    wide = {k: pd.DataFrame(v).sort_index() for k, v in fields.items()}
    idx = wide["close"].index
    mask = pd.DataFrame(False, index=idx, columns=wide["close"].columns)
    for key in mask.columns:
        for s, e in intervals[key]:
            # membership is [start, end): the stock leaves the index at `end`
            mask.loc[(idx >= s) & (idx < e), key] = True
    # one column per stock per day: a stock in BOTH indexes that left one of them has two files
    # (e.g. CHTR and CHTR@2026-06-22) covering the same days; keep the latest-ending column only
    by_base: dict[str, list[str]] = {}
    for key in mask.columns:
        by_base.setdefault(key.split("@")[0], []).append(key)
    for keys in by_base.values():
        if len(keys) < 2:
            continue
        keys = sorted(keys, key=lambda k: k.split("@")[1] if "@" in k else "9999")
        taken = np.zeros(len(idx), dtype=bool)
        for key in reversed(keys):                 # latest end first
            col = mask[key].to_numpy() & ~taken
            taken |= col
            mask[key] = col
    etfs = {}
    for t in ["SPY"] + SECTOR_ETFS:
        df = _read(t)
        if df is not None:
            etfs[t] = df.reindex(idx)
    return wide, mask, etfs


# ------------------------------------------------------------------------------------- features
def ema(df, n: int):
    return df.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def atr(wide: dict[str, pd.DataFrame], n: int = 14) -> pd.DataFrame:
    c, h, lo = wide["close"], wide["high"], wide["low"]
    pc = c.shift(1)
    tr = np.fmax(np.fmax((h - lo).to_numpy(), (h - pc).abs().to_numpy()), (lo - pc).abs().to_numpy())
    tr = pd.DataFrame(tr, index=c.index, columns=c.columns)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def assign_sectors(ret: pd.DataFrame, etf_ret: pd.DataFrame) -> pd.DataFrame:
    """Per stock, per calendar year Y: the sector ETF with the highest daily-return correlation
    over the 252 sessions ending at the last session of Y-1 (no look-ahead)."""
    out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype=object)
    for y in sorted(set(ret.index.year)):
        hist = ret.index[ret.index.year < y]
        if len(hist) < 120:
            continue
        win = hist[-252:]
        r = ret.loc[win]
        er = etf_ret.loc[win].dropna(axis=1, thresh=100)
        rows = ret.index.year == y
        for col in r.columns:
            s = r[col]
            if s.notna().sum() < 100:
                continue
            corr = er.corrwith(s, min_periods=100)
            if corr.notna().any():
                out.loc[rows, col] = corr.idxmax()
    return out


def build_features(wide, etfs) -> dict[str, pd.DataFrame]:
    c = wide["close"]
    ret = c.pct_change(fill_method=None)
    spy = etfs["SPY"]["close"]
    spy_ret = spy.pct_change(fill_method=None)
    etf_close = pd.DataFrame({t: etfs[t]["close"] for t in SECTOR_ETFS if t in etfs})
    etf_ret = etf_close.pct_change(fill_method=None)
    sectors = assign_sectors(ret, etf_ret)

    sec_ret = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
    spy_np = spy_ret.to_numpy()
    for col in c.columns:
        s = sectors[col]
        vals = spy_np.copy()                      # SPY until a sector is assigned
        for sec in s.dropna().unique():
            m = (s == sec).to_numpy()
            vals[m] = etf_ret[sec].to_numpy()[m]
        sec_ret[col] = vals

    f: dict[str, pd.DataFrame] = {}
    # 1 George-Hwang 52-week-high proximity
    f["hi52"] = c / wide["high"].rolling(252, min_periods=240).max()
    # 2 residual 12-1 momentum vs sector (Blitz-Huij-Martens): rolling beta, residual sum / std
    m_r = ret.rolling(252, min_periods=200).mean()
    m_s = sec_ret.rolling(252, min_periods=200).mean()
    cov = (ret * sec_ret).rolling(252, min_periods=200).mean() - m_r * m_s
    var = sec_ret.rolling(252, min_periods=200).var(ddof=0)
    beta = (cov / var.where(var > 0)).clip(-3, 3)
    resid = ret - beta * sec_ret
    rs_ = resid.shift(21)
    f["resmom"] = rs_.rolling(231, min_periods=200).sum() / rs_.rolling(231, min_periods=200).std().replace(0, np.nan)
    # 3 Da-Gurun-Warachka frog-in-the-pan continuity over the 12-1 window (higher = smoother)
    r_w = ret.shift(21)
    pos = (r_w > 0).astype(float).where(r_w.notna()).rolling(231, min_periods=200).mean()
    neg = (r_w < 0).astype(float).where(r_w.notna()).rolling(231, min_periods=200).mean()
    pret = c.shift(21) / c.shift(252) - 1
    f["fip"] = -(np.sign(pret) * (neg - pos))
    # 4 sector relative strength (63d sector return minus SPY 63d)
    sec63 = np.exp(np.log1p(sec_ret).rolling(63, min_periods=60).sum()) - 1
    spy63 = spy / spy.shift(63) - 1
    f["sector_rs"] = sec63.sub(spy63, axis=0)
    # 5 weekly trend structure: EMA13/EMA30 of COMPLETED weekly closes. A week's close is known
    # at its last session's close; label weeks by that last session, then forward-fill.
    grp = c.index.to_period("W-FRI")
    wk = c.groupby(grp).last()
    last_day = pd.Series(c.index, index=c.index).groupby(grp).max()
    wk.index = pd.DatetimeIndex(last_day.loc[wk.index].to_numpy())
    wk_ratio = ema(wk, 13) / ema(wk, 30) - 1
    f["wk_trend"] = wk_ratio.reindex(c.index).ffill()
    # 6 Kaufman efficiency ratio, signed, 20d
    f["eff_ratio"] = (c - c.shift(20)) / c.diff().abs().rolling(20, min_periods=20).sum().replace(0, np.nan)

    # control: daily-bar proxy of today's 12-point score (the correlated trend checks)
    macd = ema(c, 12) - ema(c, 26)
    old = ((c > c.rolling(150).mean()).astype(int) + (c > c.rolling(200).mean()).astype(int)
           + (ema(c, 13) > ema(c, 30)).astype(int) + (macd > ema(macd, 9)).astype(int)
           + ((rsi(c) >= 40) & (rsi(c) <= 70)).astype(int) + (c > c.rolling(50).mean()).astype(int))
    f["_old"] = old.astype(float).where(c.notna())
    # harness sanity control: 1-month reversal (Jegadeesh 1990) — buy the 21-day losers
    f["_rev21"] = -(c / c.shift(21) - 1)
    return f


# ---------------------------------------------------------------------------------------- labels
def triple_barrier(wide, atr14: pd.DataFrame, direction: int, cost: bool = True) -> pd.DataFrame:
    """R-multiple outcome for a trade decided at close t, entered at open t+1.
    Stop SL_ATR*ATR(t), target TP_ATR*ATR(t) from the entry; vertical exit at the close of the
    HORIZON-th session after entry. Same-bar stop+target -> stop first. Costs deducted in R."""
    o, h, lo, c = (wide[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = atr14.to_numpy()
    T, N = o.shape
    R = np.full((T, N), np.nan)
    entry = np.full((T, N), np.nan)
    entry[:-1] = o[1:]
    risk = SL_ATR * a
    stop = entry - direction * risk
    tgt = entry + direction * TP_ATR * a
    done = np.zeros((T, N), dtype=bool)
    with np.errstate(invalid="ignore"):
        for k in range(1, HORIZON + 1):     # session k after the decision day = entry day is k=1
            if k >= T:
                break
            hh = np.full((T, N), np.nan)
            ll = np.full((T, N), np.nan)
            cc = np.full((T, N), np.nan)
            hh[:-k] = h[k:]
            ll[:-k] = lo[k:]
            cc[:-k] = c[k:]
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
            if k == HORIZON:
                v = ~done & ~np.isnan(cc)
                R[v] = (direction * (cc - entry) / risk)[v]
                done |= v
        if cost:
            R = R - (COST_BPS_ROUND_TRIP / 1e4) * entry / risk
    R[~np.isfinite(R)] = np.nan
    return pd.DataFrame(R, index=atr14.index, columns=atr14.columns)


# ----------------------------------------------------------------------------------- evaluation
def xs_rank(df: pd.DataFrame, elig: pd.DataFrame) -> pd.DataFrame:
    return df.where(elig).rank(axis=1, pct=True)


def ic_weights(ranks: dict[str, pd.DataFrame], label: pd.DataFrame, rows) -> dict[str, float]:
    """Mean daily Spearman IC (rank vs label rank) over `rows`, floored at 0 (sign-constrained)."""
    lab_r = label.loc[rows].rank(axis=1, pct=True)
    w = {}
    for k, r in ranks.items():
        ic = r.loc[rows].corrwith(lab_r, axis=1)
        w[k] = float(max(0.0, np.nanmean(ic)))
    s = sum(w.values())
    return {k: (v / s if s > 0 else 1.0 / len(w)) for k, v in w.items()}


def pick(score: pd.DataFrame, k: int, cooldown: int | None = None, rng=None) -> list:
    """Top-k per day, skipping a stock picked within the last `cooldown` sessions (default: the
    run's HORIZON). Keyed by the underlying ticker so TICKER and TICKER@END columns share it."""
    if cooldown is None:
        cooldown = HORIZON
    last: dict[str, int] = {}
    out = []
    vals = score.to_numpy()
    cols = score.columns
    for i, d in enumerate(score.index):
        row = vals[i]
        ok = np.where(np.isfinite(row))[0]
        if ok.size == 0:
            continue
        order = rng.permutation(ok) if rng is not None else ok[np.argsort(-row[ok], kind="stable")]
        n = 0
        for j in order:
            t = cols[j]
            base = t.split("@")[0]
            if i - last.get(base, -10**9) < cooldown:
                continue
            out.append((d, t))
            last[base] = i
            n += 1
            if n >= k:
                break
    return out


def trade_stats(trades, label: pd.DataFrame) -> dict:
    if not trades:
        return {"n": 0}
    rr = label.stack()
    r = rr.reindex(pd.MultiIndex.from_tuples(trades)).to_numpy(dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 3:
        return {"n": int(r.size)}
    sd = float(r.std(ddof=1))
    return {"n": int(r.size), "mean_R": float(r.mean()), "win": float((r > 0).mean()),
            "sr": float(r.mean() / sd) if sd > 0 else float("nan"), "sum_R": float(r.sum()),
            "r": r.tolist()}


def main() -> int:
    global HORIZON, SL_ATR, TP_ATR, GAP_DAYS
    ap = argparse.ArgumentParser()
    ap.add_argument("--first-test-year", type=int, default=2019)
    ap.add_argument("--k", type=int, nargs="+", default=[1, 2, 3])
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--sl", type=float, default=SL_ATR)
    ap.add_argument("--tp", type=float, default=TP_ATR)
    ap.add_argument("--tag", default="base")
    args = ap.parse_args()
    HORIZON, SL_ATR, TP_ATR = args.horizon, args.sl, args.tp
    GAP_DAYS = HORIZON + 5          # the purge must cover the full label length (cold-2nd r2)
    _OUT.mkdir(parents=True, exist_ok=True)

    wide, mask, etfs = load_panel()
    c = wide["close"]
    dvol = (c * wide["volume"]).rolling(20, min_periods=15).median()
    feats = build_features(wide, etfs)
    a14 = atr(wide)
    elig = mask & (c >= MIN_PRICE) & (dvol >= MIN_DOLLAR_VOL) & wide["open"].shift(-1).notna()
    for k in ["hi52", "resmom", "fip", "sector_rs", "wk_trend", "eff_ratio", "_old"]:
        elig &= feats[k].notna()
    elig &= a14.notna() & (a14 > 0)
    labels = {d: triple_barrier(wide, a14, d).where(elig) for d in (+1, -1)}
    # cost-free labels for IC only: ~58% of labels sit exactly on a barrier and the cost term
    # (proportional to 1/ATR%) would otherwise decide their rank order (cold-2nd 2026-09-27)
    gross = {d: triple_barrier(wide, a14, d, cost=False).where(elig) for d in (+1, -1)}

    # frog-in-the-pan continuity weights momentum (design record): residual-momentum rank x
    # (0.5 + continuity rank), re-ranked — smooth winners outrank jumpy ones of equal momentum
    fip_w = 0.5 + xs_rank(feats["fip"], elig)
    feats["resmom_fip"] = xs_rank(feats["resmom"], elig) * fip_w
    ranks = {k: xs_rank(feats[k], elig) for k in FEATURES}
    # short leg: a smooth LOSER must rank as the strongest short, so weight (1 - momentum rank) by
    # continuity and map back onto the long-oriented scale (sgn() below takes 1 - r for shorts)
    short_mom = 1 - xs_rank(xs_rank(1 - xs_rank(feats["resmom"], elig), elig) * fip_w, elig)
    # old-score proxy: 0-6 integer; break ties randomly-but-deterministically
    tie = pd.DataFrame(np.random.default_rng(1).random(c.shape), index=c.index, columns=c.columns)
    old_rank = xs_rank(feats["_old"] + 1e-3 * tie, elig)
    rev_rank = xs_rank(feats["_rev21"], elig)

    idx = c.index
    years = sorted({y for y in idx.year if y >= args.first_test_year})
    test_mask = idx.year >= args.first_test_year
    results: dict = {"generated": datetime.now(ET).isoformat(), "params": {
        "SL_ATR": SL_ATR, "TP_ATR": TP_ATR, "HORIZON": HORIZON, "COST_BPS_RT": COST_BPS_ROUND_TRIP,
        "GAP_DAYS": GAP_DAYS, "features": FEATURES, "k": args.k,
        "elig_names_per_day_median": float(elig.loc[test_mask].sum(axis=1).median())},
        "runs": {}, "weights": {}, "feature_ic": {}}

    all_ranks = {**ranks, "_old": old_rank}
    for direction in (+1, -1):
        lab_r = gross[direction].rank(axis=1, pct=True)
        ic: dict = {}
        for k, r in all_ranks.items():
            sr = r if direction == 1 else 1 - r
            ic[k] = {str(y): float(np.nanmean(sr.loc[idx.year == y].corrwith(lab_r.loc[idx.year == y], axis=1)))
                     for y in years}
        results["feature_ic"][str(direction)] = ic

    rng = np.random.default_rng(20260927)
    for direction in (+1, -1):
        lab = labels[direction]
        sgn = {k: (r if direction == 1 else 1 - r) for k, r in ranks.items()}
        if direction == -1:
            sgn["resmom_fip"] = 1 - short_mom
        comp_eq = sum(sgn.values()) / len(sgn)
        comp_ic = pd.DataFrame(np.nan, index=idx, columns=c.columns)
        for y in years:
            test = idx.year == y
            first = idx[test][0]
            tr_pos = np.where(idx < first)[0]
            if len(tr_pos) <= GAP_DAYS + 120:
                continue
            w = ic_weights(sgn, gross[direction], idx[tr_pos[:-GAP_DAYS]])
            results["weights"][f"{direction}:{y}"] = w
            comp_ic.loc[test] = sum(w[k] * sgn[k].loc[test] for k in FEATURES)
        old_s = old_rank if direction == 1 else 1 - old_rank
        variants = {"c2_equal": comp_eq, "c2_icw": comp_ic, "old12_proxy": old_s,
                    "rev21_control": rev_rank if direction == 1 else 1 - rev_rank}
        for kk in args.k:
            for name, sc in variants.items():
                tr = pick(sc.where(elig).loc[test_mask], kk)
                st = trade_stats(tr, lab)
                st["by_year"] = {str(y): {x: v for x, v in trade_stats([t for t in tr if t[0].year == y], lab).items()
                                          if x != "r"} for y in years}
                results["runs"][f"{direction}|k{kk}|{name}"] = st
            rs = [trade_stats(pick(comp_eq.where(elig).loc[test_mask], kk, rng=rng), lab) for _ in range(N_RANDOM)]
            results["runs"][f"{direction}|k{kk}|random"] = {
                "n": int(np.mean([x["n"] for x in rs])),
                "mean_R": float(np.mean([x["mean_R"] for x in rs])),
                "mean_R_p05": float(np.percentile([x["mean_R"] for x in rs], 5)),
                "mean_R_p95": float(np.percentile([x["mean_R"] for x in rs], 95)),
                "win": float(np.mean([x["win"] for x in rs])),
                "sr_std_across_draws": float(np.std([x["sr"] for x in rs], ddof=1))}
            rnd_trades = [pick(comp_eq.where(elig).loc[test_mask], kk, rng=rng) for _ in range(20)]
            results["runs"][f"{direction}|k{kk}|random"]["by_year_mean_R"] = {
                str(y): float(np.nanmean([trade_stats([t for t in tr if t[0].year == y], lab).get("mean_R", np.nan)
                                          for tr in rnd_trades])) for y in years}

    results["params"].update({"SL_ATR": SL_ATR, "TP_ATR": TP_ATR, "HORIZON": HORIZON, "GAP_DAYS": GAP_DAYS, "tag": args.tag})
    out = _OUT / f"c2_swing_lab_results_{args.tag}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=1, default=str))
    tmp.replace(out)
    for k, v in results["runs"].items():
        print(k, {x: (round(y, 4) if isinstance(y, float) else y) for x, y in v.items() if x not in ("by_year", "r")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
