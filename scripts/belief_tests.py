#!/usr/bin/env python3
# ruff: noqa: E501  — long docstrings and report strings (project convention)
"""belief_tests.py — test the bot's built-in beliefs against its own completed trades (RULES-1018 rule 3).

Design record: logs/design_records/article_rules_2026-10-18.md (owner approval RULES-1018, 2026-10-18).

Each belief is an assumption that drives a LIVE decision today, phrased as "trades in group A do better than trades
in group B":
  * score_floor         — entries scoring above the median score beat those at or below it (the score floor/conviction);
  * gex_negative        — entries taken in a NEGATIVE SPY GEX regime beat the others (the live x1.30 GEX momentum sizing);
  * trend_aligned       — entries in the direction of the 12-month time-series momentum beat entries against it.
The GEX stand-down belief ("standing down on UNKNOWN GEX saves money") is reported as NOT TESTABLE until skipped
signals carry a counterfactual outcome; it is never guessed.

Method (board López de Prado seat): outcome = return on the entry's notional minus beta x the SPY move over the same
hold, signed by direction (beta estimated by least squares across all lifecycles); groups are compared on the mean
excess return; the interval is a bootstrap that resamples whole trading days (trades on one day share one market);
p-values are corrected with Holm across every belief tested. A belief is "held" or "failed" only when the corrected
result excludes zero; otherwise "inconclusive". Decision-time inputs only: a belief reads entry fields from
_DECISION_FIELDS, never an outcome field.

Read-only: no execution imports, no broker calls. The caller (weekly_postmortem.py) passes the canonical Alpaca round
trips, the entry events and SPY daily closes; results are appended to logs/belief_tests.jsonl.
"""
from __future__ import annotations

import json
import math
import os
import random
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
BELIEF_LOG = BASE_DIR / "logs" / "belief_tests.jsonl"
_ET = ZoneInfo("America/New_York")
_DECISION_FIELDS = ("symbol", "direction", "tier", "score", "gex_spy_regime", "tsmom_direction", "mri_level", "ts")
_OUTCOME_FIELDS = ("pnl", "exit_price", "exit_time", "exit_date", "lifecycle_complete")


def decision_inputs(entry: dict) -> dict:
    """The decision-time view of an entry event. Outcome fields are refused so no belief can look ahead."""
    leaked = [k for k in _OUTCOME_FIELDS if k in entry]
    if leaked:
        raise ValueError(f"entry carries outcome fields {leaked}; beliefs read decision-time inputs only")
    return {k: entry.get(k) for k in _DECISION_FIELDS}


def _parse(ts: object) -> "datetime | None":
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lifecycles(round_trips: list) -> list:
    """Completed entry lifecycles from canonical round trips: one row per entry order, P&L summed over its closes."""
    groups: dict = {}
    for r in round_trips:
        key = (r.get("symbol"), r.get("entry_order_id") or r.get("entry_time"), r.get("direction"))
        g = groups.setdefault(key, {"symbol": r.get("symbol"), "direction": r.get("direction"),
                                    "tier": r.get("tier"), "entry_time": r.get("entry_time"), "pnl": 0.0,
                                    "notional": 0.0, "exit_time": "", "complete": True})
        g["pnl"] += float(r.get("pnl") or 0.0)
        g["notional"] += float(r.get("entry_price") or 0.0) * int(r.get("qty") or 0)
        g["exit_time"] = max(str(g["exit_time"]), str(r.get("exit_time") or ""))
        g["complete"] = g["complete"] and bool(r.get("lifecycle_complete", True))
    return [g for g in groups.values() if g["complete"] and g["notional"] > 0]


_HOLD_TIERS = ("qhm", "forever6", "forever_6", "f6")   # buy-and-hold tiers: no per-entry decision record


def _tier_key(tier: object) -> str:
    t = str(tier or "").lower()
    return "day" if t in ("daytrade", "day") else "swing" if t in ("intraday", "swing", "") else t


def join_entries(cycles: list, entries: list) -> list:
    """Attach each lifecycle's entry decision record. The bot logs the entry event just after the fill confirms, so a
    record counts only if it was logged at or after the lifecycle's first fill and before its exit (never after the
    trade closed — no look-ahead), with the same symbol, direction and tier; each record is used once (the earliest
    eligible lifecycle takes it). Buy-and-hold tiers (QHM, F6) are skipped. Unjoined lifecycles are dropped."""
    events = []
    for e in entries:
        d = decision_inputs({k: v for k, v in e.items() if k not in _OUTCOME_FIELDS})
        t = _parse(d.get("ts"))
        if t is not None:
            events.append((t, d))
    events.sort(key=lambda x: x[0])
    used: set = set()
    out = []
    for c in sorted(cycles, key=lambda c: str(c.get("entry_time") or "")):
        if _tier_key(c.get("tier")) in _HOLD_TIERS:
            continue
        t0, t1 = _parse(c["entry_time"]), _parse(c["exit_time"])
        if t0 is None or t1 is None:
            continue
        for k, (t, d) in enumerate(events):
            if k in used or not (t0 <= t < t1):
                continue
            if d.get("symbol") != c["symbol"] or d.get("direction") != c["direction"]:
                continue
            if d.get("tier") and _tier_key(d.get("tier")) != _tier_key(c.get("tier")):
                continue
            used.add(k)
            out.append(dict(c, decision=d))
            break
    return out


def excess_returns(rows: list, spy_close: dict) -> list:
    """Direction-signed return on notional minus beta x the direction-signed SPY close-to-close move over the trading
    days of the hold (previous close before entry to the exit day's close, New York dates — includes the overnight gap
    before an intraday entry; minute bars are not used). Rows without a SPY close on both ends are dropped."""
    days = sorted(spy_close)

    def spy_move(t_in: "datetime | None", t_out: "datetime | None") -> "float | None":
        if t_in is None or t_out is None:
            return None
        before = [d for d in days if d < t_in.astimezone(_ET).date()]   # the close before entry (no look-ahead)
        upto = [d for d in days if d <= t_out.astimezone(_ET).date()]
        if not before or not upto or upto[-1] <= before[-1]:
            return None
        a, b = spy_close[before[-1]], spy_close[upto[-1]]
        return (b - a) / a if a else None

    pts = []
    for r in rows:
        sign = 1.0 if r["direction"] == "long" else -1.0
        mv = spy_move(_parse(r["entry_time"]), _parse(r["exit_time"]))
        if mv is None:
            continue
        pts.append((r, r["pnl"] / r["notional"], sign * mv))
    if not pts:
        return []
    n = len(pts)
    mx = sum(x for _, _, x in pts) / n
    my = sum(y for _, y, _ in pts) / n
    sxx = sum((x - mx) ** 2 for _, _, x in pts)
    beta = (sum((x - mx) * (y - my) for _, y, x in pts) / sxx) if sxx > 0 else 0.0
    return [dict(r, excess=y - beta * x, beta=beta) for r, y, x in pts]


def _day(r: dict) -> str:
    t = _parse(r.get("exit_time"))
    return t.astimezone(_ET).date().isoformat() if t else ""


def bootstrap_diff(group_a: list, group_b: list, rounds: int, seed: int = 7) -> "tuple[float, float, float, float]":
    """(mean diff A-B, lower, upper, two-sided p) from a bootstrap that resamples whole trading days."""
    def by_day(rows):
        out: dict = {}
        for r in rows:
            out.setdefault(_day(r), []).append(r["excess"])
        return list(out.values())

    da, db = by_day(group_a), by_day(group_b)
    if len(da) < 2 or len(db) < 2:
        return float("nan"), float("nan"), float("nan"), 1.0

    def mean(days):
        vals = [v for d in days for v in d]
        return sum(vals) / len(vals)

    observed = mean(da) - mean(db)
    rng = random.Random(seed)
    diffs = sorted(mean([rng.choice(da) for _ in da]) - mean([rng.choice(db) for _ in db]) for _ in range(rounds))
    lo, hi = diffs[int(0.025 * rounds)], diffs[int(0.975 * rounds) - 1]
    share_le0 = sum(1 for d in diffs if d <= 0) / rounds
    p = min(1.0, 2 * min(share_le0, 1 - share_le0))
    return observed, lo, hi, p


def _groups(belief: str, rows: list) -> "tuple[list, list] | None":
    if belief == "score_floor":
        scored = [r for r in rows if isinstance(r["decision"].get("score"), (int, float)) and r["decision"]["score"]]
        if not scored:
            return None
        vals = sorted(r["decision"]["score"] for r in scored)
        med = vals[(len(vals) - 1) // 2]          # lower median: ties at the median fall in the lower group
        return [r for r in scored if r["decision"]["score"] > med], [r for r in scored if r["decision"]["score"] <= med]
    if belief == "gex_negative":
        tagged = [r for r in rows if r["decision"].get("gex_spy_regime")]
        return ([r for r in tagged if r["decision"]["gex_spy_regime"] == "NEGATIVE"],
                [r for r in tagged if r["decision"]["gex_spy_regime"] != "NEGATIVE"])
    if belief == "trend_aligned":
        # the live log records the momentum sign as 1 / -1 (older text forms accepted too)
        tagged = [r for r in rows if r["decision"].get("tsmom_direction") in (1, -1, "long", "short", "up", "down")]

        def aligned(r):
            t = r["decision"]["tsmom_direction"]
            return (t in (1, "long", "up")) == (r["direction"] == "long")
        return [r for r in tagged if aligned(r)], [r for r in tagged if not aligned(r)]
    return None


BELIEFS = ("score_floor", "gex_negative", "trend_aligned")


def evaluate(round_trips: list, entries: list, spy_close: dict, rounds: int = 2000) -> list:
    if not spy_close:
        raise ValueError("SPY daily closes unavailable — belief tests not run")
    """One result per belief: counts, mean excess-return difference, day-clustered interval, Holm-corrected p and a
    verdict (held / failed / inconclusive), plus the untestable GEX stand-down belief."""
    rows = excess_returns(join_entries(lifecycles(round_trips), entries), spy_close)
    results: list = []
    for b in BELIEFS:
        g = _groups(b, rows)
        if not g:
            results.append({"belief": b, "n_a": 0, "n_b": 0, "diff": None, "lo": None, "hi": None, "p": 1.0})
            continue
        a, c = g
        diff, lo, hi, p = bootstrap_diff(a, c, rounds)
        results.append({"belief": b, "n_a": len(a), "n_b": len(c), "days_a": len({_day(r) for r in a}),
                        "days_b": len({_day(r) for r in c}),
                        "diff": None if math.isnan(diff) else round(diff, 5),
                        "lo": None if math.isnan(lo) else round(lo, 5),
                        "hi": None if math.isnan(hi) else round(hi, 5), "p": round(p, 4),
                        "beta": round(rows[0]["beta"], 3) if rows else None})
    # Holm: sort p ascending; the k-th smallest is compared to alpha / (m - k)
    m = len(results)
    order = sorted(range(m), key=lambda i: results[i]["p"])
    running = 0.0
    for k, i in enumerate(order):
        running = max(running, min(1.0, results[i]["p"] * (m - k)))
        results[i]["p_holm"] = round(running, 4)
    for r in results:
        sig = r["p_holm"] < 0.05 and r["lo"] is not None and (r["lo"] > 0 or r["hi"] < 0)
        r["verdict"] = ("held" if sig and r["lo"] > 0 else "failed" if sig else "inconclusive")
        r["owner_item"] = ({"failed": f"design review: belief '{r['belief']}' failed on the bot's own trades",
                            "held": f"none — belief '{r['belief']}' holds; keep it"}
                           .get(r["verdict"], f"none yet — {int(r['n_a']) + int(r['n_b'])} trades; keep collecting"))
    results.append({"belief": "gex_standdown", "verdict": "not testable",
                    "owner_item": "none — skipped signals carry no counterfactual outcome yet",
                    "n_a": 0, "n_b": 0})
    return results


def append_log(results: list, when: "date | None" = None, path: Path = BELIEF_LOG) -> None:
    """Append one JSON line per belief (each line written and fsynced whole)."""
    stamp = (when or datetime.now(timezone.utc).date()).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:   # a rerun of the same week (Sunday retry) must not append it twice
        with open(path, encoding="utf-8") as fh:
            if any(json.loads(line).get("week_ending") == stamp for line in fh if line.strip()):
                return
    except (OSError, ValueError):
        pass
    with open(path, "a", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(dict(r, week_ending=stamp), sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def report_lines(results: list) -> list:
    out = []
    for r in results:
        if r.get("diff") is None:
            out.append(f"- **{r['belief']}** — {r['verdict']} ({r.get('owner_item', '')})")
            continue
        out.append(f"- **{r['belief']}** — {r['verdict']}: excess return A−B {r['diff']:+.2%} "
                   f"(95% day-clustered interval {r['lo']:+.2%} to {r['hi']:+.2%}; Holm p {r['p_holm']}; "
                   f"{r['n_a']} vs {r['n_b']} trades over {r['days_a']}/{r['days_b']} days) — {r['owner_item']}")
    return out
