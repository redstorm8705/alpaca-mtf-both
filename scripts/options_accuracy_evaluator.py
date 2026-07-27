#!/usr/bin/env python3
# ruff: noqa: E501
"""
scripts/options_accuracy_evaluator.py
FORWARD-ACCURACY TRACKER for the 0DTE options scanner (Item 4b, 2026-07-27, board + Rafael).

READ-ONLY measurement substrate. Imports data/ only (NEVER execution/) and writes ONLY
logs/options_recs_accuracy.jsonl — it cannot touch a trade. Runs as a post-close cron (~4:35pm ET),
off the run_cycle trading thread. Idempotent (rec_id-keyed), never raises (a bad OCC / dead feed is
marked "unevaluable", never a false hit). The dynamic delta/weight recalibration that CONSUMES this
is a separate, board-gated Item 4c (default-OFF) — nothing here moves a live weight.

Per rec it records a measurement VECTOR (not a scalar), separating:
  SIGNAL      hit_strike       — did the UNDERLYING reach the strike (direction right).
  TRADEABLE   hit_target       — did a resting +100% limit fill (a volume-backed option bar
                                 high >= limit_sell). The honest tradeable win.
  OPPORTUNITY mfe_pct          — peak option swing (max bar high / premium_mid - 1). NOT a P&L.
  HEAT/PATH   mae_pct, target_before_ruin, minutes_to_target — a +100% peak that first craters
                                 -60% is not tradeable; order is the whole game on 0DTE.
  REALIZED    realized_nostop / realized_stop50 — modeled exit both ways (D1): +100% limit else
                                 EOD-hold, and +100% limit else -50% stop (the page's Mental Stop).
  THETA       runway_min_at_scan, final_pct — 0DTE theta is hyperbolic into the close.
All returns are fractions == R-multiples (risk = premium; long max loss = -1R).

Correctness (board): volume-corroborated bars only; strict ET/UTC windowing with NO look-ahead
(bars with t >= scan_time only; last usable 5-min bar is 15:55 ET); lineage (config_hash +
code_version carried from the rec) so 4c never pools across a config change; time_bucket None
(never hit) != EOD.
"""

import os
import sys
import json
import math
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import data.fetcher as fetcher  # noqa: E402  (shared client + global _rate_gate)
from data.option_bars import fetch_option_bars  # noqa: E402
from alpaca.data.requests import StockBarsRequest  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("accuracy_evaluator")

_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGS      = os.path.join(_ROOT, "logs")
_HISTORY   = os.path.join(_LOGS, "options_recs_history.jsonl")
_OUT       = os.path.join(_LOGS, "options_recs_accuracy.jsonl")

PT  = ZoneInfo("America/Los_Angeles")
ET  = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# Time buckets (minutes from scan_time). None => never hit; EOD => hit only in the final reachable
# window. Stored alongside the CONTINUOUS minutes_to_target (buckets are a derived rollup).
_BUCKET_EDGES = [(30, "30m"), (60, "1h"), (120, "2h"), (180, "3h")]  # else "EOD"

# Actionability floor for the (future) 4c recalibration — a slice below this reports
# "insufficient sample", never a number. Session floor guards day-clustering (a day's recs share
# the path, so raw count overstates independence). 30 matches KELLY_MIN_SAMPLE_SIZE.
_MIN_EVALUABLE_PER_SLICE = 30
_MIN_SESSIONS_PER_SLICE  = 20


# ── OCC symbol construction ───────────────────────────────────────────────────

def occ_symbol(symbol, expiry, direction, strike) -> str | None:
    """Build the OCC symbol from logged rec fields, e.g. SPY 2026-07-27 call 739 ->
    SPY260727C00739000. Root is the un-padded Alpaca ticker; strike is int(round(strike*1000))
    zero-padded to 8 (round, NOT truncate — 550.5 must be 00550500 not 00550499). None on bad input."""
    try:
        d = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
        cp = "C" if str(direction).lower() == "call" else "P"
        k = int(round(float(strike) * 1000))
        if k <= 0 or not symbol:
            return None
        return f"{symbol}{d.strftime('%y%m%d')}{cp}{k:08d}"
    except (TypeError, ValueError):
        return None


# ── Time helpers ──────────────────────────────────────────────────────────────

def _parse_scan_time(rec: dict) -> datetime | None:
    """rec['scan_time'] is an ISO PT timestamp (tz-aware via .isoformat()). Return an aware dt or None."""
    try:
        dt = datetime.fromisoformat(str(rec.get("scan_time")))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=PT)
        return dt
    except (TypeError, ValueError):
        return None


def _expiry_close_et(expiry) -> datetime | None:
    """16:00 ET on the expiry date (tz-aware). The last usable 5-min bar STARTS 15:55 ET."""
    try:
        d = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
        return datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
    except (TypeError, ValueError):
        return None


def _bar_dt(bar: dict) -> datetime | None:
    """Alpaca bar 't' is ISO-8601 UTC (Z) labeling the bar START. Return an aware UTC dt or None."""
    try:
        return datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError, KeyError):
        return None


def _bucket(minutes) -> str | None:
    if minutes is None:
        return None
    for edge, label in _BUCKET_EDGES:
        if minutes <= edge:
            return label
    return "EOD"


# ── Underlying 5-min bars for an explicit [start, end] window (backfill-safe) ──

def _underlying_5m(symbol, start: datetime, end: datetime) -> list:
    """Underlying 5-min bars over [start, end] via the SHARED fetcher client + global _rate_gate.
    Explicit start/end (not fetch_bars' tail) so any past rec date evaluates correctly. Returns a
    list of (dt_utc, o, h, l, c, v) tuples ascending, or [] on failure. Never raises."""
    try:
        fetcher._rate_gate()
        client = fetcher.get_client()
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=fetcher.TF_MAP[config.TF_5M],
            start=start.astimezone(UTC),
            end=end.astimezone(UTC),
        )
        df = client.get_stock_bars(req).df
        if df is None or df.empty:
            return []
        import pandas as pd  # local — pandas already a fetcher dep
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        out = []
        for ts, row in df.iterrows():
            _dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=UTC)
            out.append((_dt.astimezone(UTC), float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"]), float(row["volume"])))
        return out
    except Exception as e:
        logger.warning("underlying 5m [%s] failed: %s", symbol, e)
        return []


# ── Path-dependent modeled exit (D1: +100% limit vs -50% stop) ────────────────

def _path_exit(bars: list, premium: float, target: float, stop: float) -> tuple:
    """Walk volume-backed option bars ascending; return (realized_stop50_R, target_before_ruin).
    Whichever triggers FIRST chronologically wins: target(+100% RESTING LIMIT fill)-> +1.00R;
    -50% MENTAL stop -> -0.50R. A single bar spanning BOTH resolves to the TARGET (+1.00): the
    +100% is a resting limit that fills AUTOMATICALLY on the bar high, while -50% is a MENTAL stop
    requiring manual reaction, so the resting limit has execution priority (it fills before a hand
    reacts). Consequently realized_stop50 differs from no-stop only via a chronologically-EARLIER
    bar that hits the stop WITHOUT reaching target. Neither -> hold to last close (final R).
    Self-guards premium<=0 -> (0.0, None) so it is safe standalone (the caller already
    guarantees premium>0; this makes the pure function safe for any future caller too)."""
    if not premium or premium <= 0 or not bars:
        return 0.0, None
    for (_dt, _o, h, low, _c, _v) in bars:
        hit_t = h >= target
        hit_s = low <= stop
        if hit_t:
            return 1.00, True            # resting +100% limit fills on the high (auto) — priority over the mental stop
        if hit_s:
            return -0.50, False          # -50% (mental) hit on a bar that did NOT reach target
    last_c = bars[-1][4]
    return (last_c / premium - 1.0), None   # never triggered -> hold to close


# ── Per-rec evaluation → the measurement vector ───────────────────────────────

def evaluate_rec(rec: dict) -> dict | None:
    """Evaluate ONE 0DTE rec into an outcome row (the measurement vector). Never raises; returns
    None only if the rec is structurally unusable (no rec_id / scan_time / expiry)."""
    rec_id = rec.get("rec_id")
    scan   = _parse_scan_time(rec)
    close_et = _expiry_close_et(rec.get("expiry"))
    if not rec_id or scan is None or close_et is None:
        return None

    symbol    = rec.get("symbol")
    direction = rec.get("direction")
    strike    = rec.get("strike")
    premium   = rec.get("premium_mid")
    target    = rec.get("limit_sell")

    win_start = scan
    win_end   = close_et
    out = {
        "rec_id": rec_id, "symbol": symbol, "direction": direction,
        "conviction_pct": rec.get("conviction_pct"), "base_conviction_pct": rec.get("base_conviction_pct"),
        "gex_regime": rec.get("gex_regime"), "gex_applied": rec.get("gex_applied"),
        "runway_min_at_scan": round((win_end - win_start).total_seconds() / 60.0, 1),
        "hit_strike": None, "minutes_to_strike": None,
        "hit_target": None, "minutes_to_target": None, "time_bucket": None,
        "mfe_pct": None, "max_close_pct": None, "mae_pct": None,
        "realized_nostop": None, "realized_stop50": None, "target_before_ruin": None, "final_pct": None,
        "evaluable": False, "unevaluable_reason": None,
        "config_hash": rec.get("config_hash"), "code_version": rec.get("code_version"),
        "evaluated_at": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
    }

    # ── SIGNAL: hit_strike (underlying) — independent of the option feed ──
    try:
        und = _underlying_5m(symbol, win_start, win_end)
        und = [b for b in und if b[0] >= win_start.astimezone(UTC)]
        if und and strike is not None:
            k = float(strike)
            is_call = str(direction).lower() == "call"
            for (_dt, _o, h, low, _c, _v) in und:
                if (is_call and h >= k) or ((not is_call) and low <= k):
                    out["hit_strike"] = True
                    out["minutes_to_strike"] = round((_dt - win_start.astimezone(UTC)).total_seconds() / 60.0, 1)
                    break
            if out["hit_strike"] is None:
                out["hit_strike"] = False
    except Exception as e:
        logger.debug("hit_strike [%s] failed: %s", rec_id, e)

    # ── TRADEABLE + OPPORTUNITY + PATH: option bars (volume-corroborated) ──
    try:
        if not (premium and float(premium) > 0 and target):
            out["unevaluable_reason"] = "no_premium_or_target"
            return out
        premium = float(premium)
        target  = float(target)
        occ = occ_symbol(symbol, rec.get("expiry"), direction, strike)
        if not occ:
            out["unevaluable_reason"] = "bad_occ"
            return out
        raw = fetch_option_bars(occ, win_start, win_end)
        bars = []
        for b in raw:
            bdt = _bar_dt(b)
            if bdt is None or bdt < win_start.astimezone(UTC) or bdt > win_end.astimezone(UTC):
                continue
            if (b.get("v") or 0) <= 0:                 # volume-corroborated only (no phantom prints)
                continue
            h = b.get("h")
            low = b.get("l")
            c = b.get("c")
            o = b.get("o")
            if None in (h, low, c, o):
                continue
            bars.append((bdt, float(o), float(h), float(low), float(c), float(b.get("v") or 0)))
        bars.sort(key=lambda x: x[0])
        if not bars:
            out["unevaluable_reason"] = "no_option_bars"   # dead/illiquid feed -> UNEVALUABLE, not a miss
            return out

        highs = [x[2] for x in bars]
        peak_idx = max(range(len(highs)), key=lambda i: highs[i])
        stop = premium * 0.5

        out["mfe_pct"]       = round(max(highs) / premium - 1.0, 4)
        out["max_close_pct"] = round(max(x[4] for x in bars) / premium - 1.0, 4)
        out["mae_pct"]       = round(min(x[3] for x in bars[:peak_idx + 1]) / premium - 1.0, 4)
        out["final_pct"]     = round(bars[-1][4] / premium - 1.0, 4)

        for (_dt, _o, h, _low, _c, _v) in bars:
            if h >= target:
                out["hit_target"] = True
                out["minutes_to_target"] = round((_dt - win_start.astimezone(UTC)).total_seconds() / 60.0, 1)
                break
        if out["hit_target"] is None:
            out["hit_target"] = False
        out["time_bucket"] = _bucket(out["minutes_to_target"])

        out["realized_nostop"] = 1.00 if out["hit_target"] else out["final_pct"]
        r_stop, tbr = _path_exit(bars, premium, target, stop)
        out["realized_stop50"] = round(r_stop, 4)
        out["target_before_ruin"] = tbr
        out["evaluable"] = True
    except Exception as e:
        logger.warning("option eval [%s] failed: %s", rec_id, e)
        out["unevaluable_reason"] = "eval_exception"
    return out


# ── Idempotency + eligibility ─────────────────────────────────────────────────

def _evaluated_ids() -> set:
    ids: set = set()
    if not os.path.exists(_OUT):
        return ids
    try:
        with open(_OUT) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line).get("rec_id"))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("could not read accuracy log: %s", e)
    return ids


def _eligible(rec: dict, now_et: datetime) -> bool:
    """Only evaluate a rec whose expiry 16:00 ET has PASSED (0DTE: same day after close; weekly:
    after its Friday close). Not-yet-expired recs are skipped and picked up on a later run."""
    close_et = _expiry_close_et(rec.get("expiry"))
    return close_et is not None and now_et >= close_et


def _append(row: dict) -> None:
    """Append one outcome row durably (flush + fsync) — mirrors _persist_rec_history so a mid-run
    crash is resumable at row granularity."""
    with open(_OUT, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ── Aggregates (report-only; the 4c recalibration engine consumes these) ──────

def _wilson_lb(k: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound for a binomial rate — the conservative hit-rate for gating."""
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round((center - margin) / denom, 4)


def _conv_bucket(pct) -> str:
    try:
        p = int(pct)
    except (TypeError, ValueError):
        return "na"
    if p >= 90:
        return "90+"
    if p >= 75:
        return "75-89"
    if p >= 60:
        return "60-74"
    return "<60"


def summarize(rows: list) -> dict:
    """Per-slice (conviction-bucket x gex_regime) hit_target rate with Wilson LB + effective
    sessions + ACTIONABLE flag. Report-only — NOTHING here moves a live weight (that is 4c)."""
    slices: dict = {}
    for r in rows:
        if not r.get("evaluable"):
            continue
        key = f"{_conv_bucket(r.get('conviction_pct'))}|{r.get('gex_regime')}"
        s = slices.setdefault(key, {"n": 0, "hits": 0, "sessions": set(), "mfe": []})
        s["n"] += 1
        s["hits"] += 1 if r.get("hit_target") else 0
        _st = str(r.get("evaluated_at"))  # session proxy = the rec's trading day (from rec_id prefix)
        rid = str(r.get("rec_id", ""))
        s["sessions"].add(rid.split("T")[0] if "T" in rid else _st[:10])
        if isinstance(r.get("mfe_pct"), (int, float)):
            s["mfe"].append(r["mfe_pct"])
    report = {}
    for key, s in slices.items():
        n, hits, nsess = s["n"], s["hits"], len(s["sessions"])
        mfe_sorted = sorted(s["mfe"])
        report[key] = {
            "n_evaluable": n, "n_sessions": nsess,
            "hit_target_rate": round(hits / n, 4) if n else None,
            "hit_target_wilson_lb": _wilson_lb(hits, n),
            "median_mfe_pct": (mfe_sorted[len(mfe_sorted) // 2] if mfe_sorted else None),
            "actionable": (n >= _MIN_EVALUABLE_PER_SLICE and nsess >= _MIN_SESSIONS_PER_SLICE),
        }
    return report


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    if not os.path.exists(_HISTORY):
        logger.info("no rec history at %s — nothing to evaluate", _HISTORY)
        return 0
    now_et = datetime.now(ET)
    done = _evaluated_ids()
    n_eval = n_skip = n_new = 0
    new_rows: list = []
    with open(_HISTORY) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("mode") != "0dte":        # v1: 0DTE only (weekly = future extension)
                continue
            rid = rec.get("rec_id")
            if not rid or rid in done:
                n_skip += 1
                continue
            if not _eligible(rec, now_et):
                continue                          # not expired yet — pick up on a later run
            row = evaluate_rec(rec)
            if row is None:
                continue
            _append(row)
            done.add(rid)
            new_rows.append(row)
            n_new += 1
            n_eval += 1 if row.get("evaluable") else 0
    logger.info("evaluated %d new rec(s) (%d evaluable), %d already-done skipped",
                n_new, n_eval, n_skip)
    if new_rows:
        rep = summarize(new_rows)
        logger.info("this-run per-slice summary: %s", json.dumps(rep))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as _e:               # cron-safe: never crash the scheduler
        logger.error("accuracy evaluator top-level error (non-fatal): %s", _e)
        sys.exit(0)
