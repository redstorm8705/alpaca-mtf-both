# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/earned_sizing.py — CONV-1018 conviction-band sizing for the Day tier (pure helpers; no orders).

Design record: logs/design_records/earned_concentration_sizing_2026-10-18.md, section "Gate-compliant revision"
(owner approval CONV-1018, 2026-10-19; Thorp + Taleb seats, Gro and GAI aligned).

WHAT IT DECIDES (place_entry calls it; this module never places an order):
  1. The conviction BAND of an entry: terciles of the conviction of every Day trade completed before this week's
     Monday 00:00 ET (recalculated each week), and that band's mean R (R = realized $/share / initial stop distance,
     from the journal's broker-confirmed fills). A band with mean R <= 0 takes no entry; a band with mean R > 0 is
     sized by the budget below. No usable record (too few trades, no conviction) -> None = today's sizing.
  2. The SIZE of a proven-band entry (conv_size): tight stop d' = min(max(k x ATR5, m x median spread), S), never wider
     than today's stop S; per-share risk = max(d' + A, price x J) with gap allowance A = max(G x S, k x ATR5);
     shares = remaining budget / per-share risk. k, m are the existing DAYTRADE_MIN_STOP_ATR_MULT /
     DAYTRADE_MIN_STOP_SPREAD_MULT; G = worst observed stop overshoot ratio, J = worst observed adverse one-minute
     move in the Day tier's own trades (both measured, ratchet up only). Missing/non-positive ATR or spread -> d' = S.
No new number: every value is an existing setting or measured from the journal and Alpaca bars (T1).
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
BAND_NAMES = ("low", "middle", "top")   # terciles: the number of bands is the length of this tuple
_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "earned_sizing.json"


def week_start_et(now: "datetime | None" = None) -> datetime:
    """Monday 00:00 ET of the week containing `now` (the weekly recalculation boundary)."""
    n = (now or datetime.now(ET)).astimezone(ET)
    d = n.date() - timedelta(days=n.weekday())
    return datetime(d.year, d.month, d.day, tzinfo=ET)


def _ts(raw: object) -> "datetime | None":
    try:
        t = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
        return t if t.tzinfo else None
    except (TypeError, ValueError):
        return None


def _conviction(rec: dict) -> "float | None":
    for part in ("size", "decision"):
        v = (rec.get(part) or {}).get("conviction") if isinstance(rec.get(part), dict) else None
        try:
            f = float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def completed_trades(events: list) -> list:
    """Every completed Day trade in the journal: entry fill, its first protective stop, its exit fills (full exit or
    partials covering the whole entry), its R and its conviction (from the decision whose decision_id the entry fill
    carries, else one logged with the same trade_id; None when absent). Malformed records are skipped. Never raises."""
    by: dict = {}
    conv_by_decision: dict = {}
    for e in events or []:
        if isinstance(e, dict) and e.get("event") == "decision" and e.get("decision_id"):
            c = _conviction(e)
            if c is not None:
                conv_by_decision.setdefault(str(e["decision_id"]), c)
    for e in events or []:
        if not isinstance(e, dict):
            continue
        tid = e.get("trade_id")
        kind = e.get("event")
        if not tid:
            continue
        g = by.setdefault(tid, {"entry": None, "stop": None, "exits": [], "full": False, "conviction": None})
        if kind == "entry_fill" and g["entry"] is None:
            g["entry"] = e
        elif kind == "stop_placed" and g["stop"] is None:
            g["stop"] = e
        elif kind in ("exit_fill", "partial_exit_fill"):
            g["exits"].append(e)
            g["full"] = g["full"] or kind == "exit_fill"
        elif kind == "decision" and g["conviction"] is None:
            g["conviction"] = _conviction(e)   # fallback; the decision_id join below is authoritative
    out = []
    for tid, g in by.items():
        if not (g["entry"] and g["stop"] and g["exits"]):
            continue
        try:
            entry = float(g["entry"]["fill_price"])
            qty = abs(float(g["entry"]["fill_qty"]))
            stop = float(g["stop"]["stop_price"])
            xq = sum(abs(float(x["fill_qty"])) for x in g["exits"])
            pnl = sum(float(x["realized_pnl"]) for x in g["exits"])
        except (KeyError, TypeError, ValueError):
            continue
        dist = abs(entry - stop)
        if not all(math.isfinite(v) for v in (entry, qty, stop, xq, pnl)) or dist <= 0 or xq <= 0:
            continue
        if not (g["full"] or xq >= qty):
            continue   # still open
        conv = conv_by_decision.get(str(g["entry"].get("decision_id") or ""), g["conviction"])
        exit_ts = [t for t in (_ts(x.get("ts")) for x in g["exits"]) if t is not None]
        entry_ts = _ts(g["entry"].get("ts"))
        if not exit_ts or entry_ts is None:
            continue
        out.append({"trade_id": tid, "symbol": g["entry"].get("symbol"), "side": g["entry"].get("side"),
                    "conviction": conv, "R": (pnl / xq) / dist, "stop_dist": dist,
                    "entry_price": entry, "entry_ts": entry_ts, "exit_ts": max(exit_ts)})
    return out


def band_cuts(convictions: list) -> list:
    """Tercile cut points of the given convictions (index-based, the same rule the replay used); [] when there are
    fewer trades than bands."""
    c = sorted(float(x) for x in convictions)
    k = len(BAND_NAMES)
    if len(c) < k:
        return []
    return [c[(i * len(c)) // k] for i in range(1, k)]


def band_index(x: float, cuts: list) -> int:
    """Band of conviction `x`: below the first cut -> 0; from the first cut up to and including the last cut ->
    the middle bands; above the last cut -> the top band."""
    idx = 0
    for j, cut in enumerate(cuts):
        if (x >= cut) if j == 0 else (x > cut):
            idx = j + 1
    return idx


def overshoot_ratio(trades: list) -> "float | None":
    """G: the worst observed loss beyond the stop, as a fraction of the stop distance, over every completed trade
    (0 when no stop ever overshot). Computed over the whole history, so it can only rise as trades are added."""
    if not trades:
        return None
    return max(max(0.0, -float(t["R"]) - 1.0) for t in trades)


def plan(conviction: object, events: list, now: "datetime | None" = None) -> "dict | None":
    """The band decision for an entry at `conviction`. None when there is no usable record (the caller keeps today's
    sizing). Otherwise a dict with the band, its trade count and mean R, the cuts, G and `proven` (mean R > 0)."""
    try:
        conv = float(conviction)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(conv):
        return None
    trades = completed_trades(events)
    cutoff = week_start_et(now)
    prior = [t for t in trades if t["exit_ts"] < cutoff and t["conviction"] is not None]
    cuts = band_cuts([t["conviction"] for t in prior])
    if not cuts:
        return None
    b = band_index(conv, cuts)
    per_band = [[t["R"] for t in prior if band_index(t["conviction"], cuts) == i] for i in range(len(BAND_NAMES))]
    # Every band needs more than one completed trade before any band is judged (the replay's walk-forward minimum:
    # two per band, six in all) — one trade cannot make a band "proven".
    if min(len(x) for x in per_band) <= 1:
        return None
    rs = per_band[b]
    g = overshoot_ratio(trades)
    if g is None:
        return None
    mean_r = statistics.mean(rs)
    return {"band": BAND_NAMES[b], "band_n": len(rs), "band_mean_r": round(mean_r, 4),
            "cuts": [round(c, 4) for c in cuts], "week_start": cutoff.date().isoformat(),
            "n_prior": len(prior), "G": round(g, 4), "proven": mean_r > 0}


# ── J: worst adverse one-minute move in the Day tier's own trades (cached, ratchets up) ──────────────────────────
def _adverse_one_minute(side: str, bars) -> "float | None":
    """Worst adverse move of one 1-minute bar vs the prior bar's close (the first bar vs its own open), as a fraction
    of that reference price. None when no usable bar."""
    worst = None
    prev = None
    try:
        for o, h, lo, c in zip(bars["open"].tolist(), bars["high"].tolist(), bars["low"].tolist(),
                               bars["close"].tolist(), strict=True):
            ref = float(prev if prev is not None else o)
            if not (math.isfinite(ref) and ref > 0):
                prev = c
                continue
            adv = (ref - float(lo)) / ref if side == "long" else (float(h) - ref) / ref
            if math.isfinite(adv):
                worst = adv if worst is None else max(worst, adv)
            prev = c
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return worst


def _load_cache() -> dict:
    try:
        if _STATE.exists():
            with open(_STATE, encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("earned_sizing cache read failed: %s", e)
    return {}


def _save_cache(d: dict) -> bool:
    try:
        os.makedirs(_STATE.parent, exist_ok=True)
        tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STATE)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("earned_sizing cache write failed: %s", e)
        return False


def jump_ratio(trades: list, fetch=None) -> "float | None":
    """J: the pooled worst adverse one-minute move over every completed trade's own IEX 1-minute path, cached in
    data/state/earned_sizing.json. Only trades not yet scanned are fetched; J only ever rises. A failed fetch leaves
    that trade for the next call. None when no path has been measured yet (the caller keeps today's sizing)."""
    cache = _load_cache()
    try:
        j = float(cache["J"]) if cache.get("J") is not None else None
    except (TypeError, ValueError):
        j = None
    if j is not None and not (math.isfinite(j) and j >= 0):
        j = None
    # An unreadable J with a scan list would let a rescan of only new trades LOWER J: rescan everything instead.
    scanned = set(cache.get("scanned") or []) if j is not None else set()
    todo = [t for t in trades if t["trade_id"] not in scanned]
    if todo:
        if fetch is None:
            import config
            from data.fetcher import fetch_bars_window

            def fetch(t):  # T1 Alpaca bars, IEX feed (the Day tier trades off IEX)
                return fetch_bars_window(t["symbol"], config.TF_1M, t["entry_ts"].astimezone(timezone.utc),
                                         t["exit_ts"].astimezone(timezone.utc), feed="iex")
        changed = False
        for t in todo:
            try:
                bars = fetch(t)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] earned_sizing: 1-minute path fetch failed (retried later): %s", t.get("symbol"), e)
                continue
            if bars is None or getattr(bars, "empty", True):
                continue
            try:
                bars = bars[bars.index > t["entry_ts"]]
            except Exception:  # noqa: BLE001
                continue
            adv = _adverse_one_minute(str(t.get("side") or "long"), bars)
            scanned.add(t["trade_id"])
            changed = True
            if adv is not None:
                j = adv if j is None else max(j, adv)
        if changed:
            _save_cache({"J": j, "scanned": sorted(scanned), "updated": datetime.now(ET).isoformat()})
    return j


# ── sizing (pure) ────────────────────────────────────────────────────────────────────────────────────────────────
def conv_size(direction: str, limit_px: float, stop_px: float, anchor_px: float, atr5: "float | None",
              spread_med: "float | None", atr_mult: float, spread_mult: float, g: float, j: float,
              budget_room: float, rt_room: "float | None" = None) -> dict:
    """Size a proven-band entry. `stop_px` is today's protective stop (S = |limit - stop|); `anchor_px` the price the
    tight stop is measured from (the more adverse of the limit and the last trade). Returns {"ok", "stop", "d",
    "S", "A", "per_share", "qty", "binding", "why"}. ok False on any unusable input (the caller keeps today's
    sizing). `rt_room` (optional): the real-time / open volatility room the room stop uses; the tight stop never goes
    inside it. Never raises."""
    out: dict = {"ok": False, "why": ""}
    try:
        vals = (limit_px, stop_px, anchor_px, atr_mult, spread_mult, g, j, budget_room)
        if direction not in ("long", "short") or not all(math.isfinite(float(v)) for v in vals):
            out["why"] = "conv sizing: unusable input"
            return out
        s = abs(float(limit_px) - float(stop_px))
        if s <= 0 or anchor_px <= 0 or g < 0 or j < 0:
            out["why"] = "conv sizing: unusable stop/anchor/G/J"
            return out
        atr_ok = atr5 is not None and math.isfinite(float(atr5)) and float(atr5) > 0
        spr_ok = spread_med is not None and math.isfinite(float(spread_med)) and float(spread_med) > 0
        if atr_ok and spr_ok:
            terms = {"atr": atr_mult * float(atr5), "spread": spread_mult * float(spread_med)}  # type: ignore[arg-type]
            if rt_room is not None and math.isfinite(float(rt_room)) and float(rt_room) > 0:
                terms["realtime"] = float(rt_room)   # the room stop's real-time / open volatility floor
            binding = max(terms, key=lambda name: terms[name])
            d = min(terms[binding], s)
            binding = "stop" if d >= s else binding
        else:
            d, binding = s, "stop (atr/spread unavailable)"
        stop = round(float(anchor_px) - d, 2) if direction == "long" else round(float(anchor_px) + d, 2)
        # Never wider than today's stop, and on the protective side of the anchor.
        stop = max(stop, float(stop_px)) if direction == "long" else min(stop, float(stop_px))
        if (direction == "long" and not stop < anchor_px) or (direction == "short" and not stop > anchor_px):
            out["why"] = "conv sizing: tight stop not protective"
            return out
        d = abs(float(limit_px) - stop)
        a = max(g * s, atr_mult * float(atr5)) if atr_ok else g * s  # type: ignore[arg-type]
        per_share = max(d + a, float(limit_px) * j)
        if not (math.isfinite(per_share) and per_share > 0):
            out["why"] = "conv sizing: per-share risk unusable"
            return out
        qty = math.floor(budget_room / per_share) if budget_room > 0 else 0
        out.update(ok=True, stop=stop, d=round(d, 4), S=round(s, 4), A=round(a, 4), per_share=round(per_share, 4),
                   qty=max(0, int(qty)), binding=binding,
                   why=(f"conv: S ${s:.4f} -> d' ${d:.4f} ({binding}); A ${a:.4f} (G {g:.3f}); J {j:.4%}; "
                        f"per-share risk ${per_share:.4f}; budget room ${budget_room:.2f} -> {max(0, int(qty))} sh"))
        return out
    except (TypeError, ValueError) as e:
        out["why"] = f"conv sizing error: {e!r}"
        return out
