#!/usr/bin/env python3
# ruff: noqa: E501
"""
research/ic_engine.py — IC / ICIR / half-life learning-loop engine (Phase 1).

Rafael mandate 2026-07-13 ("begin building the IC/ICIR learning loop"). PURE STATS —
no Claude/GAI API (numpy/scipy only; runs free on OCI). READ-ONLY: reads the trade log,
computes signal quality, prints a ranked report. It NEVER changes weights — any weight
change it later PROPOSES goes through the board+Gro+GAI gate (Architecture Invariant #1).

WHAT IT MEASURES (plain):
  - IC (Information Coefficient): Spearman corr(signal_at_entry, realized_return). Does a
    higher signal precede a better trade? +ve = predictive, ~0 = noise.
  - ICIR: IC's consistency = mean(IC) / std(IC) across bootstrap resamples. A steady small
    edge beats a flashy inconsistent one.
  - Half-life: how fast the signal's edge decays (AR(1) on the per-trade IC series).
  - Bootstrap 95% CI + a small-sample honesty flag (n<50 => "too few to trust").

PHASE-1 DATA REALITY (2026-07-13): trade_events.jsonl `entry` events log the TOTAL score
(`score`, `score_16pt`) + TSMOM fields, NOT the 12 per-factor components. So this phase scores
the signals we HAVE (total 12pt, 16pt, tsmom_12m/6m). PER-FACTOR IC (which of the 12 components
predict) needs a 1-line logging add at entry (log the SCORE_WEIGHTS breakdown) — Phase 1b.

USAGE: python3 -m research.ic_engine   (or) python3 research/ic_engine.py
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

logger = logging.getLogger("ic_engine")

_ROOT = Path(__file__).resolve().parent.parent
_EVENTS = _ROOT / "logs" / "trade_events.jsonl"

_MIN_TRUST_N = 50  # below this, IC is dominated by noise on our tiny sample (PBO caveat)


def _load_events() -> list[dict]:
    """All JSON events from trade_events.jsonl. Fail-soft on a bad line."""
    out: list[dict] = []
    if not _EVENTS.exists():
        return out
    for ln in _EVENTS.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _match_trades(events: list[dict]) -> list[dict]:
    """Pair each `entry` with its FIFO `exit`/`partial_exit` for the same symbol to get a
    realized per-trade return. Direction-aware (long vs short). Returns a list of dicts:
    {symbol, score, score_16pt, tsmom_12m, tsmom_6m, direction, entry_price, exit_price, ret}.
    A trade with no matching exit yet is skipped (no realized return)."""
    from collections import defaultdict, deque
    opens: dict[str, deque] = defaultdict(deque)
    trades: list[dict] = []
    for e in events:
        ev = e.get("event")
        sym = e.get("symbol")
        if not sym:
            continue
        if ev == "entry":
            opens[sym].append(e)
        elif ev in ("exit", "partial_exit"):
            if not opens[sym]:
                continue
            en = opens[sym].popleft() if ev == "exit" else opens[sym][0]
            try:
                ep = float(en.get("price") or 0.0)
                xp = float(e.get("price") or 0.0)
            except (TypeError, ValueError):
                continue
            if ep <= 0 or xp <= 0:
                continue
            direction = str(en.get("direction") or "long").lower()
            ret = (xp - ep) / ep if direction != "short" else (ep - xp) / ep
            trades.append({
                "symbol": sym,
                "score": _num(en.get("score")),
                "score_16pt": _num(en.get("score_16pt")),
                "tsmom_12m": _num(en.get("tsmom_12m")),
                "tsmom_6m": _num(en.get("tsmom_6m")),
                "direction": direction,
                "entry_price": ep,
                "exit_price": xp,
                "ret": ret,
            })
    return trades


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (no scipy dependency). None if degenerate."""
    n = len(xs)
    if n < 3:
        return None

    def _rank(a: list[float]) -> list[float]:
        order = sorted(range(len(a)), key=lambda i: a[i])
        r = [0.0] * len(a)
        i = 0
        while i < len(a):
            j = i
            while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for kk in range(i, j + 1):
                r[order[kk]] = avg
            i = j + 1
        return r

    rx, ry = _rank(xs), _rank(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    vy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def _bootstrap_icir(xs: list[float], ys: list[float], iters: int = 500) -> dict:
    """Bootstrap the IC to get ICIR (mean/std) + a 95% CI. Deterministic LCG (no
    Math.random / numpy needed; reproducible). Returns {ic, icir, lo, hi, n}."""
    n = len(xs)
    base = _spearman(xs, ys)
    if base is None or n < 5:
        return {"ic": base, "icir": None, "lo": None, "hi": None, "n": n}
    seed = 1234567
    ics: list[float] = []
    for _ in range(iters):
        bx, by = [], []
        for _ in range(n):
            seed = (1103515245 * seed + 12345) & 0x7FFFFFFF
            idx = seed % n
            bx.append(xs[idx])
            by.append(ys[idx])
        ic = _spearman(bx, by)
        if ic is not None:
            ics.append(ic)
    if not ics:
        return {"ic": base, "icir": None, "lo": None, "hi": None, "n": n}
    mean = sum(ics) / len(ics)
    var = sum((c - mean) ** 2 for c in ics) / max(len(ics) - 1, 1)
    std = math.sqrt(var)
    ics.sort()
    lo = ics[int(0.025 * len(ics))]
    hi = ics[min(int(0.975 * len(ics)), len(ics) - 1)]
    return {"ic": base, "icir": (mean / std if std > 0 else None),
            "lo": lo, "hi": hi, "n": n}


def analyze(signals=("score", "score_16pt", "tsmom_12m", "tsmom_6m")) -> dict:
    """Compute IC/ICIR/CI for each signal vs realized return. Returns a report dict."""
    events = _load_events()
    trades = _match_trades(events)
    report = {"n_trades": len(trades), "signals": {}}
    for sig in signals:
        pairs = [(t[sig], t["ret"]) for t in trades
                 if t.get(sig) is not None and t.get("ret") is not None]
        if len(pairs) < 5:
            report["signals"][sig] = {"n": len(pairs), "note": "too few pairs"}
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        r = _bootstrap_icir(xs, ys)
        r["ci_excludes_zero"] = (r["lo"] is not None and r["hi"] is not None
                                 and (r["lo"] > 0 or r["hi"] < 0))
        r["trustworthy"] = (r["n"] >= _MIN_TRUST_N)
        report["signals"][sig] = r
    return report


def _fmt(v, d=3):
    return f"{v:+.{d}f}" if isinstance(v, (int, float)) else "—"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    rep = analyze()
    print("=" * 70)
    print("IC / ICIR ENGINE — Phase 1 (total-score + TSMOM level)")
    print(f"matched trades with realized returns: {rep['n_trades']}")
    print("-" * 70)
    print(f"{'signal':<14}{'n':>4}{'IC':>9}{'ICIR':>9}"
          f"{'95% CI':>20}  flags")
    for sig, r in rep["signals"].items():
        if "ic" not in r:
            print(f"{sig:<14}{r.get('n', 0):>4}   {r.get('note', '')}")
            continue
        ci = f"[{_fmt(r['lo'])},{_fmt(r['hi'])}]"
        flags = []
        if r.get("ci_excludes_zero"):
            flags.append("SIGNIFICANT")
        if not r.get("trustworthy"):
            flags.append(f"n<{_MIN_TRUST_N}-noisy")
        print(f"{sig:<14}{r['n']:>4}{_fmt(r['ic']):>9}{_fmt(r['icir']):>9}"
              f"{ci:>20}  {' '.join(flags)}")
    print("-" * 70)
    print("READ: IC>0 = the signal precedes better returns; ICIR = consistency; a 95% CI\n"
          "that excludes 0 is statistically real. n<50 on our tiny sample => treat as noise\n"
          "(PBO caveat). NEXT (Phase 1b): log per-factor SCORE_WEIGHTS at entry to get IC of\n"
          "each of the 12 components individually. This engine PROPOSES; the board decides.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
