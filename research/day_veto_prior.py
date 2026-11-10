#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
research/day_veto_prior.py — DAY-VETO-SIZE-1109: build the Track B vetoed-group replay prior the Day runner reads.

Design record: logs/design_records/day_veto_size_2026-11-09.md (revision 8, items 2, 2c, 2d, 2g).

READ-ONLY research script: it reads two already-computed replay signal files (no market-data call, no broker, no order
import) and writes ONE small JSON (atomic tmp -> replace) that execution/day_veto_sizing.py loads:

  input files (the Track B replays the design record's evidence table comes from):
    * the 2025-10..2026-11-06 window   (research/day_tier_trackb_watchday_replay.py output)
    * the 2023-10..2025-09-30 window   (research/day_tier_trackb_oos_replay.py output)
  vetoed group = signals with room_ok True and gate in ("no_clear_side", "counter_block") — the setups rules 1 and 3
  block today; R = the replay's own net-of-cost R (field "R", bar-close entry, 5 bps cost, 2R target).

  output (default data/state/day_veto_prior.json):
    * per-session sufficient statistics of the vetoed group (n, wins, sum of winning R, sum of |losing R|, sum of R),
      equal weights across both windows (revision 6: no fitted half-life);
    * variants_tried — the number of candidate variants tried in the review (design record: ~34, the Bonferroni
      t >= 2.81 line); the runtime prior weight per replay trade is 1 / variants_tried, so the replay counts as
      n / variants_tried effective trades (682 / 34 ~ 20), never as n;
    * pause_ci_level / pause_upper_quantile — the owner-approved 90% two-sided interval of revision 7 (upper end =
      the 95th percentile of the normal), stored here so the runtime carries no fixed number of its own;
    * a summary (p, b, f*, mean R) for the reader.

Usage:
  python3 research/day_veto_prior.py [--recent PATH] [--older PATH] [--out PATH] [--variants 34] [--ci 0.90]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
PT = ZoneInfo("America/Los_Angeles")
VETO_GATES = ("no_clear_side", "counter_block")
_SCRATCH = ("/private/tmp/claude-501/-Users-rafaeldeleon-Desktop-alpaca-mtf-bot-FINAL--claude-worktrees-optimize-"
            "token-usage-d0b393/a4b46f7b-3bfd-4e32-8226-88d6308cc67b/scratchpad")
DEFAULT_RECENT = f"{_SCRATCH}/wt-rb/logs/trackb_watchday_replay_2026-11-10.json"
DEFAULT_OLDER = ("/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.claude/worktrees/gamma-trading-strategy-308918/"
                 "logs/trackb_oos_replay_2026-10-11.json")
DEFAULT_OUT = ROOT / "data" / "state" / "day_veto_prior.json"


def vetoed_trades(path: str) -> list:
    """[(date, R)] of the vetoed group in one replay file."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    out = []
    for s in d.get("signals") or []:
        if not (isinstance(s, dict) and s.get("room_ok") is True and s.get("gate") in VETO_GATES):
            continue
        try:
            r = float(s["R"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(r) and s.get("date"):
            out.append((str(s["date"]), r))
    return out


def session_stats(trades: list) -> list:
    by: dict = {}
    for day, r in trades:
        g = by.setdefault(day, {"date": day, "n": 0, "wins": 0, "sum_win_r": 0.0, "sum_loss_r": 0.0, "sum_r": 0.0})
        g["n"] += 1
        g["sum_r"] += r
        if r > 0:
            g["wins"] += 1
            g["sum_win_r"] += r
        else:
            g["sum_loss_r"] += -r
    return [{**g, "sum_win_r": round(g["sum_win_r"], 6), "sum_loss_r": round(g["sum_loss_r"], 6),
             "sum_r": round(g["sum_r"], 6)} for _, g in sorted(by.items())]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--recent", default=DEFAULT_RECENT)
    ap.add_argument("--older", default=DEFAULT_OLDER)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--variants", type=int, default=34,
                    help="candidate variants tried (design record DAY-VETO-SIZE-1109: ~34, Bonferroni t >= 2.81)")
    ap.add_argument("--ci", type=float, default=0.90,
                    help="two-sided pause interval level (design record revision 7: 90%%)")
    a = ap.parse_args()
    if a.variants < 1 or not (0 < a.ci < 1):
        print("variants must be >= 1 and ci in (0, 1)", file=sys.stderr)
        return 2
    recent, older = vetoed_trades(a.recent), vetoed_trades(a.older)
    trades = older + recent
    if not trades:
        print("no vetoed trades found", file=sys.stderr)
        return 1
    rs = [r for _, r in trades]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    p = len(wins) / len(rs)
    b = (statistics.mean(wins) / statistics.mean(losses)) if (wins and losses) else None
    f_star = (p - (1 - p) / b) if b else None
    upper_q = 1 - (1 - a.ci) / 2
    out = {
        "change": "day-veto-size-1109",
        "built": datetime.now(PT).isoformat(),
        "source_files": [a.older, a.recent],
        "filter": "room_ok is True and gate in ('no_clear_side', 'counter_block')",
        "field": "R",
        "weights": "equal (revision 6: no fitted half-life)",
        "n_trades": len(rs),
        "n_by_window": {"older": len(older), "recent": len(recent)},
        "variants_tried": int(a.variants),
        "n_eff": round(len(rs) / a.variants, 4),
        "pause_ci_level": a.ci,
        "pause_upper_quantile": round(upper_q, 6),
        "summary": {"p": round(p, 6), "b": round(b, 6) if b else None, "f_star": round(f_star, 6) if f_star is not None else None,
                    "mean_r": round(statistics.mean(rs), 6), "sessions": len({d for d, _ in trades})},
        "sessions": session_stats(trades),
    }
    path = Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    print(json.dumps({k: v for k, v in out.items() if k != "sessions"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
