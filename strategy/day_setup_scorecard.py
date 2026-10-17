# ruff: noqa: E501
"""day_setup_scorecard.py — per-setup evidence gate for the Day tier (Claude, 2026-10-17).

Design record: logs/design_records/day_setup_scorecard_2026-10-17.md (board López de Prado/Thorp seat, Gro, GAI).

A "setup" is direction × trigger mode (e.g. short/FADE). Every logged Day-tier signal, taken or skipped, gets two
market-adjusted outcomes (after the Day tier's median holding time, derived from its own fills, and at the session close, signal return minus SPY's over the same
minutes, signed by direction). The nightly builder (run_day_setup_scorecard.py) turns those into this file's scorecard;
the runner only reads it.

Statistics (all derived from the data):
  * outcomes are averaged per trading day first (same-day signals share the market's move);
  * empirical-Bayes shrinkage of each setup's mean toward the leave-one-out pool mean, weight k = within-setup
    variance / between-setup variance (k = infinity, i.e. nothing blocked, when the between variance is <= 0);
  * one-sided upper bound = posterior + q * posterior SE, q = Student-t quantile at 1 - 1/(m+m) with (days - 1) degrees
    of freedom, m = number of setups scored (multiple-testing correction);
  * a setup is BLOCKED only when both horizons' bounds are below zero and it was seen on at least two days;
  * a blocked setup is still EXPLORED — the entry is allowed, at its normal size (wire-time sizing re-derives the quantity
    in place_entry, so a 1-share cap would not hold) — with probability = its posterior probability of a positive edge.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import statistics
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
SCORECARD_PATH = _ROOT / "data" / "state" / "day_setup_scorecard.json"
HORIZONS = ("rhold", "rclose")   # the Day tier's median holding time, and the session close

_last_good: dict = {}           # in-memory last good scorecard (a torn or unreadable file never blanks the gate)


def setup_key(direction: object, mode: object) -> str:
    return f"{str(direction or '').lower()}/{str(mode or '').upper()}"


def _t_quantile(p: float, df: int) -> Optional[float]:
    try:
        from scipy.stats import t as _t
        return float(_t.ppf(p, df))
    except Exception as exc:  # RC-3: no scipy -> no bound -> nothing blocked
        logger.warning("day_setup_scorecard: t quantile unavailable (%s)", exc)
        return None


def _t_sf(x: float, df: int) -> Optional[float]:
    try:
        from scipy.stats import t as _t
        return float(_t.sf(x, df))
    except Exception as exc:  # RC-3
        logger.warning("day_setup_scorecard: t survival unavailable (%s)", exc)
        return None


def _daily_means(rows: list, key: str, horizon: str) -> dict:
    by_day: dict = {}
    for r in rows:
        if r.get("key") == key and isinstance(r.get(horizon), (int, float)) and math.isfinite(r[horizon]):
            by_day.setdefault(r["day"], []).append(float(r[horizon]))
    return {d: statistics.fmean(v) for d, v in by_day.items()}


def _pool_daily_means(rows: list, exclude_key: str, horizon: str) -> list:
    by_day: dict = {}
    for r in rows:
        if r.get("key") != exclude_key and isinstance(r.get(horizon), (int, float)) and math.isfinite(r[horizon]):
            by_day.setdefault(r["day"], []).append(float(r[horizon]))
    return [statistics.fmean(v) for v in by_day.values()]


def score_horizon(rows: list, horizon: str) -> dict:
    """{key: {days, mean, se, posterior, post_se, k, q, bound, p_positive}} for one horizon. Pure."""
    keys = sorted({r["key"] for r in rows if r.get("key")})
    per = {k: _daily_means(rows, k, horizon) for k in keys}
    stats: dict = {}
    for k, dm in per.items():
        vals = list(dm.values())
        n = len(vals)
        var = statistics.variance(vals) if n > 1 else None
        stats[k] = {"days": n, "mean": statistics.fmean(vals) if vals else None, "var": var,
                    "se": math.sqrt(var / n) if var is not None else None}
    usable = [s for s in stats.values() if s["se"] is not None]
    m = len(keys)
    k_weight: Optional[float] = None
    if len(usable) > 1:
        within = statistics.fmean(s["var"] for s in usable)
        between = statistics.variance([s["mean"] for s in usable]) - statistics.fmean(s["se"] ** 2 for s in usable)
        k_weight = (within / between) if between > 0 else None   # None = infinite shrinkage -> nothing blocked
    out: dict = {}
    for k, s in stats.items():
        rec = {"days": s["days"], "mean": s["mean"], "se": s["se"], "posterior": None, "post_se": None,
               "k": k_weight, "q": None, "bound": None, "p_positive": None}
        if s["se"] is not None and k_weight is not None and m > 1:
            pool = _pool_daily_means(rows, k, horizon)
            if pool:
                n = s["days"]
                post = (n * s["mean"] + k_weight * statistics.fmean(pool)) / (n + k_weight)
                post_se = s["se"] * math.sqrt(n / (n + k_weight))   # normal-normal posterior SD
                q = _t_quantile(1 - 1 / (m + m), n - 1)   # Bonferroni, two-sided, across the m setups scored
                rec.update(posterior=post, post_se=post_se, q=q)
                if q is not None:
                    rec["bound"] = post + q * post_se
                if post_se > 0:
                    rec["p_positive"] = _t_sf(-post / post_se, n - 1)
        out[k] = rec
    return out


def build_scorecard(rows: list, built_for_session: str, built_at: str) -> dict:
    """Pure: the scorecard dict written by the builder."""
    horizons = {h: score_horizon(rows, h) for h in HORIZONS}
    setups: dict = {}
    for key in sorted({r["key"] for r in rows if r.get("key")}):
        hs = {h: horizons[h].get(key, {}) for h in HORIZONS}
        bounds = [hs[h].get("bound") for h in HORIZONS]
        blocked = all(b is not None and b < 0 for b in bounds) and all((hs[h].get("days") or 0) > 1 for h in HORIZONS)
        probs = [hs[h].get("p_positive") for h in HORIZONS if hs[h].get("p_positive") is not None]
        means = [hs[h].get("mean") for h in HORIZONS]
        setups[key] = {
            "blocked": blocked,
            "explore_probability": min(probs) if probs else None,
            "horizons": hs,
            "drift_flag": bool(None not in means and (means[0] > 0) != (means[1] > 0)),
        }
    return {"schema_v": 1, "built_for_session": built_for_session, "built_at": built_at,
            "signals": len(rows), "setups": setups}


def write_scorecard(card: dict, path: Path = SCORECARD_PATH) -> None:
    """Atomic write (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(card, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_scorecard(path: Path = SCORECARD_PATH) -> dict:
    """The newest readable scorecard; a missing/torn file returns the last good one held in memory ({} if none)."""
    global _last_good
    try:
        card = json.loads(path.read_text())
        if isinstance(card, dict) and isinstance(card.get("setups"), dict):
            _last_good = card
    except FileNotFoundError:
        pass
    except Exception as exc:  # RC-3: keep the last good copy
        logger.warning("day_setup_scorecard: scorecard unreadable (%s) — using last good copy", exc)
    return _last_good


def evaluate(direction: object, mode: object, card: dict, rng: "random.Random | None" = None) -> dict:
    """Gate verdict for one signal: action 'allow' | 'block' | 'explore' plus the evidence. Never raises."""
    key = setup_key(direction, mode)
    out = {"action": "allow", "setup": key, "scorecard_session": (card or {}).get("built_for_session")}
    try:
        rec = ((card or {}).get("setups") or {}).get(key)
        if not rec or not rec.get("blocked"):
            out["reason"] = "no scorecard" if not card else ("setup not scored" if not rec else "setup not blocked")
            return out
        out["evidence"] = rec
        p = rec.get("explore_probability")
        draw = (rng or random).random()
        if p is not None and draw < p:
            out.update(action="explore", reason=f"setup {key} blocked by its record; allowed this time to keep testing it (p={p:.2f})")
        else:
            out.update(action="block", reason=f"setup {key} blocked by its record (both horizons' bounds below zero)")
    except Exception as exc:  # RC-3: never block on an error
        logger.warning("day_setup_scorecard: evaluate failed for %s: %s", key, exc)
        out.update(action="allow", reason=f"evaluate error: {exc}")
    return out
