"""One-shot execution of pre-registered Test 3 from PR #475 / merge 8d04528.

Research only. Baltussen et al. rest-of-day -> final-half-hour momentum.
Owner/signature: ChatGPT/Codex.
"""

import math
import os
from datetime import time as tm
import numpy as np
import pandas as pd
from scipy.stats import norm, skew, kurtosis

D = os.environ.get("DAY_TIER_SIP_DIR", "data/cache/lab/bars/1Min_sip")
SYMS = ["SPY", "QQQ"]
N_TRIALS = 20
RNG = np.random.default_rng(20261005)


def table(sym):
    d = pd.read_csv(f"{D}/{sym}.csv.gz", index_col=0, compression="gzip")
    d.index = pd.to_datetime(d.index, utc=True).tz_convert("America/New_York")
    rows = []
    for day, g in d.groupby(d.index.date):
        t = g.index.time

        def at(h, m, c, group=g, times=t):
            z = group[times == tm(h, m)]
            return float(z[c].iloc[0]) if len(z) else np.nan

        rows.append(
            dict(
                date=pd.Timestamp(day),
                last=float(g.close.iloc[-1]),
                c1529=at(15, 29, "close"),
                o1530=at(15, 30, "open"),
                c1549=at(15, 49, "close"),
                c1559=at(15, 59, "close"),
                full=t[-1] >= tm(15, 59),
            )
        )
    x = pd.DataFrame(rows).set_index("date").sort_index()
    x["prev"] = x["last"].shift(1)
    return x[x.full].dropna()


def dsr_prob(r, ntrials):
    r = np.asarray(r)
    s = r.mean() / r.std(ddof=1)
    n = len(r)
    sr_sd = math.sqrt((1 + 0.5 * s * s) / (n - 1))
    e = 0.5772156649
    sr0 = sr_sd * (
        (1 - e) * norm.ppf(1 - 1 / ntrials) + e * norm.ppf(1 - 1 / (ntrials * math.e))
    )
    z = (
        (s - sr0)
        * math.sqrt(n - 1)
        / math.sqrt(
            max(1e-12, 1 - skew(r) * s + (kurtosis(r, fisher=False) - 1) * s * s / 4)
        )
    )
    return float(norm.cdf(z)), s * math.sqrt(252)


def block_p(r, block=10, B=5000):
    r = np.asarray(r)
    obs = r.mean()
    centered = r - r.mean()
    n = len(r)
    means = []
    for _ in range(B):
        vals = []
        while len(vals) < n:
            st = int(RNG.integers(0, n))
            vals.extend(centered[np.arange(st, st + block) % n])
        means.append(np.mean(vals[:n]))
    return (1 + sum(x >= obs for x in means)) / (B + 1)


frames = {s: table(s) for s in SYMS}
for cost_side in [0, 1, 3, 5, 10]:
    cols = []
    for s, x in frames.items():
        direction = np.sign(x.c1529 / x.prev - 1)
        gross = direction * (x.c1549 / x.o1530 - 1)
        cols.append((gross - 2 * cost_side / 1e4).rename(s))
    p = pd.concat(cols, axis=1).dropna()
    r = p.mean(axis=1)
    dsr, sr = dsr_prob(r, N_TRIALS)
    bp = r.mean() * 1e4
    pf = r[r > 0].sum() / abs(r[r < 0].sum())
    yr = (r.groupby(r.index.year).mean() * 1e4).round(2).to_dict()
    top20 = r.nlargest(20).sum() / r.sum() if r.sum() > 0 else float("nan")
    fields = (
        f"COST {cost_side}bp/side n={len(r)} mean={bp:+.3f}bp "
        f"SR={sr:+.3f} DSR={dsr:.4f} block_p={block_p(r):.4f} "
        f"win={(r > 0).mean():.3f} PF={pf:.3f} "
        f"top20share={top20:.3f} years={yr}"
    )
    print(fields)
# per-symbol at required 5bp and 15:59 benchmark
for s, x in frames.items():
    direction = np.sign(x.c1529 / x.prev - 1)
    for exitcol in ["c1549", "c1559"]:
        r = direction * (x[exitcol] / x.o1530 - 1) - 10 / 1e4
        d, sr = dsr_prob(r, N_TRIALS)
        years = (r.groupby(r.index.year).mean() * 1e4).round(2).to_dict()
        print(
            f"{s} {exitcol} 5bp/side mean={r.mean() * 1e4:+.3f}bp "
            f"SR={sr:+.3f} DSR={d:.4f} years={years}"
        )
# random-sign null for primary pooled date-level at 5bp/side
G = []
for s, x in frames.items():
    G.append((np.sign(x.c1529 / x.prev - 1) * (x.c1549 / x.o1530 - 1)).rename(s))
g = pd.concat(G, axis=1).dropna()
obs = (g.mean(axis=1) - 0.001).mean()
null = []
for _ in range(5000):
    signs = RNG.choice([-1, 1], size=g.shape)
    null.append(
        (pd.DataFrame(signs, index=g.index, columns=g.columns) * g.abs())
        .mean(axis=1)
        .mean()
        - 0.001
    )
null_p = (1 + sum(v >= obs for v in null)) / 5001
print(
    f"RANDOM_SIGN observed={obs * 1e4:+.3f}bp "
    f"null95={np.quantile(null, 0.95) * 1e4:+.3f}bp p={null_p:.4f}"
)
