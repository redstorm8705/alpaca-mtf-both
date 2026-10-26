# ruff: noqa: E501
"""READ-ONLY research (no broker): SWING-OWNER-1025 inputs.
(1) the candidate pool: current S&P 500 / NDX members that ranked in the top 50 by 60-day median $ volume on any
    session of the last 252; (2) the replay R prior: the owner-design replay (top 50, prio 10, 10 lots, recover,
    stop-sized r 1%, cap 1.5x) trade R distribution + the mean loss multiple of stopped trades.
"""
import json
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/ubuntu/mtf-bot")
from research.c2_swing_lab import load_panel  # noqa: E402

wide, mask, etfs = load_panel()
o, h, lo, c, v = (wide[k] for k in ("open", "high", "low", "close", "volume"))
dv_rank = (c * v).rolling(60, min_periods=40).median().where(mask).rank(axis=1, ascending=False)
last = dv_rank.index[-1]
recent = dv_rank.iloc[-252:]
current = mask.iloc[-1]
pool = sorted(s for s in recent.columns if bool(current.get(s, False)) and (recent[s] <= 50).any())
print("last bar", last, "pool size", len(pool))

prior55 = c.shift(1).rolling(55).max()
sma200 = c.rolling(200).mean()
low20 = c.shift(1).rolling(20).min()
tr = pd.concat([(h - lo), (h - c.shift(1)).abs(), (lo - c.shift(1)).abs()]).groupby(level=0).max()
atr14 = tr.rolling(14).mean()
signal = (c > prior55) & (c > sma200) & mask
dates = c.index
start = dates.searchsorted(pd.Timestamp("2017-01-03"))
RS, STOP_RS = [], []


def run(top_n, priority, slots, risk_pct, cap):
    eq = 1.0
    held, pending = {}, {}
    for i in range(start, len(dates) - 1):
        d, nd = dates[i], dates[i + 1]
        for s in list(held):
            p = held[s]
            p["n"] += 1
            px_lo, px_o, px_c = lo.at[d, s], o.at[d, s], c.at[d, s]
            if np.isnan(px_c):
                continue
            exit_px, stopped = None, False
            if px_lo <= p["stop"]:
                exit_px = min(px_o, p["stop"]) if not np.isnan(px_o) else p["stop"]
                stopped = True
            elif px_c < low20.at[d, s] or p["n"] >= 30:
                exit_px = px_c
            if exit_px is not None:
                r = exit_px / p["entry"] - 1 - 0.001
                eq += p["alloc"] * r
                RS.append(r / p["sp"])
                if stopped:
                    STOP_RS.append(r / p["sp"])
                del held[s]
        ranks = dv_rank.loc[d]
        for s in signal.columns[signal.loc[d].fillna(False).to_numpy()]:
            if ranks.get(s, np.inf) <= top_n and s not in held:
                pending.setdefault(s, prior55.at[d, s])
        for s in list(pending):
            if s in held or np.isnan(c.at[d, s]) or c.at[d, s] < low20.at[d, s]:
                pending.pop(s, None)
        order = sorted(pending, key=lambda s: (0 if ranks.get(s, np.inf) <= priority else 1, ranks.get(s, np.inf)))
        for s in order:
            if len(held) >= slots:
                break
            op, lvl, a = o.at[nd, s], pending[s], atr14.at[d, s]
            if np.isnan(op) or np.isnan(a) or op < lvl:
                continue
            stop_pct = 2.5 * a / op
            room = cap * eq - sum(p["alloc"] for p in held.values())
            alloc = min(eq * risk_pct / stop_pct, room)
            if alloc <= eq * 0.01:
                continue
            held[s] = {"entry": op, "stop": op - 2.5 * a, "n": 0, "alloc": alloc, "sp": stop_pct}
            pending.pop(s, None)


run(50, 10, 10, 0.01, 1.5)
R = np.array(RS)
S = np.array(STOP_RS)
prior = {"source": "swing-owner-1025 replay: research/c2_swing_lab.load_panel, point-in-time S&P 500 + NDX, "
                   "2017-01-03.." + str(dates[-1].date()) + ", top 50 / prio 10 / 10 lots / recover / r 1% / cap 1.5x, "
                   "10 bps round trip, gap stops fill at the open",
         "n": int(len(R)), "mean_r": float(R.mean()), "sd_r": float(R.std(ddof=1)),
         "n_stopped": int(len(S)), "loss_mult_stopped": float(-S.mean()),
         "share_below_minus_1r": float((R < -1.0).mean()), "worst_r": float(R.min()),
         "pool": pool, "pool_rule": "current S&P 500 / NDX member ranked top 50 by 60-day median $ volume on any of "
                                     "the last 252 sessions (lab cache, last bar " + str(last.date()) + ")"}
print(json.dumps({k: v for k, v in prior.items() if k != "pool"}, indent=1))
print(json.dumps(pool))
with open("/home/ubuntu/mtf-bot/logs/lab/swing_owner_prior.json", "w") as f:
    json.dump(prior, f, indent=1)
