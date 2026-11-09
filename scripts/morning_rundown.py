#!/usr/bin/env python3
# ruff: noqa: E501
"""
scripts/morning_rundown.py — MORNING-1029 (owner-approved 2026-11-08): daily Slack morning rundown for SPY and QQQ.

Two sends per trading day (OCI cron via scripts/cron_tz_wrapper.py, ET targets):
  --send 0600  (09:00 ET)  PRE-MARKET · reference: levels + reference setups. Always REFERENCE ONLY — Alpaca takes no
                           extended-hours option orders, and pre-market IEX coverage is 0–20% (spec §1, §4).
  --send 0646  (09:46 ET)  WATCHLIST: per symbol and side, the trigger / target / invalidation and the strike that ranks
                           first at send time with its ask and quote time. NO limit price and NO stop: those come only in
                           the TRIGGERED alert from scripts/morning_watch.py, rebuilt from fresh quotes at the break (§3b).

What it does (spec sections):
  §2a  two prices: SIP one-minute bars (15-min delayed on this plan) build the LEVELS; the IEX latest trade (real time)
       decides whether a trigger is unbroken, and is current only if its age < t_max = (merge_distance / σ_1m)² minutes.
       Trigger history = real-time IEX one-minute bars covering every minute from the SIP cutoff to now; a level is broken
       if a SIP bar since the level time crossed it, or an IEX gap bar came within the side's venue buffer (b_high / b_low).
  §2b  levels: GEX call wall / put wall / gamma flip (data/gex.py internals, prior-close OI), prior-day H/L/C, overnight
       H/L, expected move from the ATM straddle ASKS × √(2π)/2, and (06:46 only) VWAP from completed bars since the open.
       Levels closer than the merge distance merge into one "confluence" level.
  §2c  setup completeness: trigger, target and invalidation are all required, else NO ACTIONABLE SETUP and no ranking.
  §3   strikes: 0DTE strikes from the trigger out to the target plus one beyond; eligible only with a two-sided quote that
       updated inside the send-time polling window and an ASK <= $1.25 (owner cap, per share). Rank (v9.2): reward =
       intrinsic value at the target; risk = ask − Black-Scholes premium at the invalidation price at the decision time
       using the quote's own implied volatility; fallback intrinsic ÷ ask (logged). Ties go to the cheaper ask (v8).
  §6   logging: per-row ts_utc / ts_local / tz / schema_version; logs/morning_rundown_recs.jsonl (fcntl-locked appends);
       logs/morning_rundown_chain/<date>_<HHMM>.json.gz raw payloads; logs/morning_rundown_<date>.md and the heartbeat
       (atomic); the `violations` counts (must be 0) — a non-zero count replaces that section with its reference form.
  v8   logs/0dte_recommendations.jsonl: one row per printed 06:46 line (locked append).

SAFETY: a REPORT for the owner's manual decisions. It places NO orders, imports nothing from execution/, and writes no
bot state (data/state untouched). Data: Alpaca T1 only — data/fetcher.py (bars), data/alpaca_data.py (latest trade),
data/gex.py internals (contracts, option snapshots, GEX, Black-Scholes). The Alpaca calendar is read from the trading
API (read-only). Every time shown to the owner is PT. Per-symbol isolation: one symbol failing never blanks the other.
"""
from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("morning_rundown")

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    logger.debug("morning_rundown: python-dotenv absent — using ambient environment")
except Exception as _dotenv_err:
    logger.warning("morning_rundown: load_dotenv skipped (%s) — using ambient environment", _dotenv_err)

import numpy as np                                     # noqa: E402
import pandas as pd                                    # noqa: E402
import requests                                        # noqa: E402  (calendar only; read-only trading API)

import config                                          # noqa: E402
import data.gex as gex                                 # noqa: E402  (read-only compute internals)
from data import alpaca_data, fetcher                  # noqa: E402  (T1 bars + latest trade)
from data.live_price import _SIP_DELAY_S               # noqa: E402  (this plan's SIP entitlement delay)
from alerts import send_slack, send_slack_blocks       # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
TZ_NAME = "America/Los_Angeles"
SCHEMA_VERSION = 1

SYMBOLS = ("SPY", "QQQ")
SIDES = ("CALL", "PUT")

# ── Numbers (spec §7) ─────────────────────────────────────────────────────────────────────────────────────────────
PREMIUM_CAP_PER_SHARE = 1.25     # owner number (§11 Q1, approved 2026-11-08): $1.25 per share of premium = $125/contract
STOP_FLOOR_PER_SHARE = 0.05      # board number (v9 item 3, owner-approved with the spec): a model stop <= $0.05 is not placeable
SHARES_PER_CONTRACT = 100        # OCC standard equity-option multiplier (display of the maximum loss only)
STATS_SESSIONS = 20              # §7 "20-session windows": the estimator sample (sessions) behind σ_1m, b, merge distance
EM_FACTOR = math.pi / math.sqrt(math.tau)   # = √(2π)/2 ≈ 1.2533 (straddle → expected move); never typed as a number
QUOTE_POLL_TIMEOUT_SECONDS = 60.0           # plumbing: the send-time quote polling window (§3 rule 1)
QUOTE_POLL_SLEEP_SECONDS = 2.0              # plumbing: spacing between snapshot polls inside the window
REQUEST_TIMEOUT_SECONDS = 8.0               # plumbing: HTTP timeout for the calendar call
OVERNIGHT_START_PT = dtime(1, 0)            # §2b-3: overnight high/low from 01:00 PT (spec)
SIP_DELAY = timedelta(seconds=_SIP_DELAY_S)  # data/live_price.py: this plan's SIP entitlement delay
ONE_MINUTE = timedelta(minutes=1)

_LOGS = _ROOT / "logs"
STATE = {
    "recs": _LOGS / "morning_rundown_recs.jsonl",
    "recommendations": _LOGS / "0dte_recommendations.jsonl",
    "heartbeat": _LOGS / "morning_rundown_heartbeat.json",
    "chain_dir": _LOGS / "morning_rundown_chain",
}

STATES = ("WATCHING", "ACTIONABLE", "REFERENCE ONLY", "NO ACTIONABLE SETUP", "NO CURRENT EXECUTABLE QUOTE",
          f"NO ACTIONABLE CONTRACT UNDER ${PREMIUM_CAP_PER_SHARE:.2f}")
_WATCHING, _ACTIONABLE, _REFERENCE, NO_SETUP, NO_QUOTE, NO_CONTRACT = STATES
VIOLATION_KEYS = ("ask_over_cap", "quote_not_current", "trigger_text_without_history", "ranked_without_setup",
                  "actionable_without_stop", "limit_or_stop_shown_when_stale", "stop_invalid")


# ══ time helpers ══════════════════════════════════════════════════════════════════════════════════════════════════

def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def stamp(now: datetime) -> dict:
    """The DST-safe record stamp every row carries (§6)."""
    return {"ts_utc": iso_z(now), "ts_local": now.astimezone(PT).isoformat(), "tz": TZ_NAME,
            "schema_version": SCHEMA_VERSION}


def fmt_pt(dt: datetime | None, seconds: bool = False) -> str:
    if dt is None:
        return "n/a"
    return dt.astimezone(PT).strftime("%H:%M:%S PT" if seconds else "%H:%M PT")


def parse_ts(ts: Any) -> datetime | None:
    """RFC-3339 (nanoseconds allowed) -> tz-aware UTC datetime (microsecond precision); None if unparseable/naive."""
    if isinstance(ts, datetime):
        return ts.astimezone(UTC) if ts.tzinfo else None
    if not ts:
        return None
    try:
        t = pd.Timestamp(str(ts))
    except (TypeError, ValueError):
        return None
    if t is pd.NaT or t.tzinfo is None:
        return None
    return t.tz_convert(UTC).floor("us").to_pydatetime()


def utc_offset_text(dt: datetime) -> str:
    return str(dt.isoformat(timespec="seconds"))[-6:]


def clock_record(now: datetime, wrapper_target_et: str) -> dict:
    now_et = now.astimezone(ET)
    return {"now_utc": iso_z(now), "now_et": now_et.isoformat(), "detected_et_offset": utc_offset_text(now_et),
            "wrapper_target_et": wrapper_target_et}


def years_between(later: datetime, earlier: datetime) -> float:
    """Year fraction (365-day year, as data/gex.py): data/gex._MIN_T_YEARS is one hour expressed in years."""
    left_seconds = (later - earlier).total_seconds()
    left_hours = left_seconds / 3600
    return left_hours * gex._MIN_T_YEARS


def floor_cent(x: float) -> float:
    """Round DOWN to the option tick (SPY/QQQ options quote in pennies), via the decimal text (no float drift)."""
    whole, _, decimals_text = f"{x:.6f}".partition(".")
    return float(f"{whole}.{str(decimals_text)[:2]}")


# ══ I/O helpers (atomic writes, locked appends) ═══════════════════════════════════════════════════════════════════

def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as e:
            logger.warning("morning_rundown: tmp cleanup failed (%s)", e)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def locked_append_jsonl(path: Path, rows: list[dict]) -> None:
    """Append JSON lines under an fcntl exclusive lock (two writers never interleave a line)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(r, default=str, sort_keys=True) + "\n" for r in rows)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _deep_merge(base: dict, patch: dict) -> dict:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def read_json(path: Path) -> dict | None:
    try:
        out = json.loads(path.read_text(encoding="utf-8"))
        return out if isinstance(out, dict) else None
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("morning_rundown: unreadable %s (%s)", path.name, e)
        return None


def update_heartbeat(patch: dict) -> dict:
    """Locked read-modify-write of the shared heartbeat (the rundown and the watcher both write it); atomic replace."""
    hb_path = STATE["heartbeat"]
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hb_path.with_suffix(".lock"), "a+") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            hb = read_json(hb_path) or {}
            _deep_merge(hb, patch)
            atomic_write_text(hb_path, json.dumps(hb, default=str, sort_keys=True, indent=1))
            return hb
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


# ══ data access (T1 only; every function here is mocked in tests) ═════════════════════════════════════════════════

def calendar(start: date, end: date) -> list[dict] | None:
    """Alpaca trading calendar [start, end] -> [{'date', 'open' (ET dt), 'close' (ET dt)}]; None on failure."""
    try:
        resp = requests.get(f"{gex._BASE_TRADING}/v2/calendar", headers=gex._headers(),
                            params={"start": start.isoformat(), "end": end.isoformat()},
                            timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            logger.warning("morning_rundown: calendar HTTP %s", resp.status_code)
            return None
        out = []
        for row in resp.json() or []:
            d = date.fromisoformat(str(row["date"]))
            o = datetime.combine(d, dtime.fromisoformat(str(row["open"])), tzinfo=ET)
            c = datetime.combine(d, dtime.fromisoformat(str(row["close"])), tzinfo=ET)
            out.append({"date": d, "open": o, "close": c})
        return out
    except Exception as e:
        logger.warning("morning_rundown: calendar fetch failed (%s)", e)
        return None


def get_bars(symbol: str, start: datetime, end: datetime, feed: str) -> pd.DataFrame:
    """One-minute bars (T1, data/fetcher.py), UTC index of bar STARTS; empty on any miss."""
    return fetcher.fetch_bars_window(symbol, config.TF_1M, start, end, feed=feed)


def get_daily_bars(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    return fetcher.fetch_bars_window(symbol, config.TF_DAILY, start, end, feed="sip")


def latest_trade(symbol: str) -> tuple[float, datetime] | None:
    """IEX latest trade (this plan's real-time feed) with its time (data/alpaca_data.py)."""
    return alpaca_data.get_latest_trade_with_time(symbol)


def fetch_contracts(symbol: str, date_gte: str, date_lte: str) -> dict[str, float]:
    return gex._fetch_contracts(symbol, date_gte, date_lte)


def fetch_snapshots(symbols: list[str]) -> dict:
    return gex._fetch_snapshots(symbols)


# ══ bar frames ═════════════════════════════════════════════════════════════════════════════════════════════════════

def _norm(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                            index=pd.DatetimeIndex([], tz=UTC))
    out = df.copy()
    idx = pd.DatetimeIndex(out.index)
    out.index = idx.tz_localize(UTC) if idx.tz is None else idx.tz_convert(UTC)
    return out.sort_index()


def completed(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Only bars confirmed complete by timestamp: start + 60 s <= now (the forming bar is never used)."""
    df = _norm(df)
    if len(df) == 0:
        return df
    ends = df.index + pd.Timedelta(ONE_MINUTE)
    return df[ends <= pd.Timestamp(now)]


def missing_minutes(df: pd.DataFrame, after: datetime, through: datetime) -> list[datetime]:
    """Bar-start minutes in (after, through] with no bar in `df` (the coverage check)."""
    have = {ts.to_pydatetime() for ts in _norm(df).index}
    out: list[datetime] = []
    t = after + ONE_MINUTE
    while t <= through:
        if t not in have:
            out.append(t)
        t += ONE_MINUTE
    return out


def floor_minute(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(second=0, microsecond=0)


def _in_clock_window(df: pd.DataFrame, days: set, start_t: dtime, end_t: dtime) -> pd.DataFrame:
    df = _norm(df)
    if len(df) == 0:
        return df
    et = df.index.tz_convert(ET)
    mask = [(ts.date() in days) and (start_t <= ts.time() < end_t) for ts in et]
    return df[np.array(mask, dtype=bool)]


# ══ §2a statistics (σ_1m, b_high, b_low, merge distance, t_max) ════════════════════════════════════════════════════

def derive_stats(sip: pd.DataFrame, iex: pd.DataFrame, days: list[date], start_t: dtime, end_t: dtime,
                 today_sip: pd.DataFrame | None = None, today_iex: pd.DataFrame | None = None) -> dict:
    """Clock-window statistics over the given sessions (§2a, §2b). Cold start: no session bars -> today's own SIP bars
    (labelled 'today only'); none at all -> every statistic is None (both sides REFERENCE ONLY)."""
    out: dict = {"merge_distance": None, "sigma_1m": None, "b_high": None, "b_low": None, "t_max_min": None,
                 "sessions": {"merge_distance": 0, "sigma_1m": 0, "b_high": 0, "b_low": 0}, "basis": "sessions",
                 "notes": []}
    dayset = set(days)
    sip_w = _in_clock_window(sip, dayset, start_t, end_t)
    iex_w = _in_clock_window(iex, dayset, start_t, end_t)
    if len(sip_w) == 0 and today_sip is not None and len(_norm(today_sip)) > 0:
        sip_w, iex_w = _norm(today_sip), _norm(today_iex)
        out["basis"] = "today only"
    if len(sip_w) == 0:
        out["basis"] = "none"
        return out
    sip_days = {ts.date() for ts in sip_w.index.tz_convert(ET)}
    ranges = (sip_w["high"] - sip_w["low"]).to_numpy(dtype=float)
    out["merge_distance"] = float(np.median(ranges))
    out["sessions"]["merge_distance"] = len(sip_days)
    close_changes: list[float] = []
    for _d, grp in sip_w.groupby(sip_w.index.tz_convert(ET).date):
        steps = grp.index.to_series().diff()
        moves = grp["close"].diff()
        keep = (steps == pd.Timedelta(ONE_MINUTE)).to_numpy()
        close_changes.extend(float(v) for v in moves.to_numpy()[keep])
    if not len(close_changes) < 2:
        out["sigma_1m"] = float(np.std(np.array(close_changes), ddof=1))
        out["sessions"]["sigma_1m"] = len(sip_days)
    iex_w = _norm(iex_w)
    joined = sip_w.join(iex_w, how="inner", lsuffix="_sip", rsuffix="_iex")
    if len(joined) > 0:
        jdays = len({ts.date() for ts in joined.index.tz_convert(ET)})
        hi_gap = float(np.percentile((joined["high_sip"] - joined["high_iex"]).to_numpy(dtype=float), 99))
        lo_gap = float(np.percentile((joined["low_iex"] - joined["low_sip"]).to_numpy(dtype=float), 99))
        for key, val in (("b_high", hi_gap), ("b_low", lo_gap)):
            if val < 0:
                out["notes"].append(f"{key} percentile {val:.4f} < 0 (misaligned bars) -> treated as 0")
                logger.warning("morning_rundown: %s percentile %.4f < 0 -> 0", key, val)
            out[key] = max(0.0, val)
            out["sessions"][key] = jdays
    md, sg = out["merge_distance"], out["sigma_1m"]
    if md is not None and sg:
        out["t_max_min"] = (md / sg) ** 2
    return out


# ══ §2b levels ═════════════════════════════════════════════════════════════════════════════════════════════════════

def level(price: float, source: str, set_at: datetime | None) -> dict:
    return {"price": float(price), "sources": [source], "set_at": iso_z(set_at)}


def merge_levels(levels: list[dict], merge_distance: float | None) -> list[dict]:
    """Levels closer than the merge distance (to the cluster's first level) merge into one 'confluence' level at the
    cluster mean, labelled with every source; the earliest set_at is kept. No distance -> unmerged."""
    lv = sorted((dict(x) for x in levels if isinstance(x.get("price"), (int, float))), key=lambda x: x["price"])
    if merge_distance is None:
        for x in lv:
            x["confluence"] = False
        return lv
    out: list[dict] = []
    for x in lv:
        if out and x["price"] - out[-1]["_anchor"] < merge_distance:
            c = out[-1]
            c["_members"].append(x["price"])
            c["sources"] = c["sources"] + [s for s in x["sources"] if s not in c["sources"]]
            stamps = [s for s in (c.get("set_at"), x.get("set_at")) if s]
            c["set_at"] = min(stamps) if stamps else None
            c["price"] = float(np.mean(c["_members"]))
            c["confluence"] = True
        else:
            out.append({**x, "_anchor": x["price"], "_members": [x["price"]], "confluence": False})
    for c in out:
        c.pop("_anchor", None)
        c.pop("_members", None)
    return out


def expected_move(call_q: dict | None, put_q: dict | None, merge_distance: float | None) -> dict:
    """EM = (ATM call ask + ATM put ask) × √(2π)/2 (approx., skew ignored). Requires both legs two-sided and current, and
    the spread uncertainty √(2π)/2 × (call spread + put spread)/2 <= merge distance; otherwise unavailable (no fallback)."""
    res: dict = {"available": False, "em": None, "spread_uncertainty": None, "merge_distance": merge_distance,
                 "reason": None}
    for name, q in (("call", call_q), ("put", put_q)):
        ok, why = two_sided(q)
        if not ok or not (q or {}).get("current"):
            res["reason"] = f"ATM {name}: {why if not ok else 'quote not current'}"
            return res
    assert call_q is not None and put_q is not None
    unc = EM_FACTOR * float(np.mean([call_q["ask"] - call_q["bid"], put_q["ask"] - put_q["bid"]]))
    res["spread_uncertainty"] = unc
    if merge_distance is None:
        res["reason"] = "merge distance undefined"
        return res
    if unc > merge_distance:
        res["reason"] = f"spread uncertainty {unc:.2f} > merge distance {merge_distance:.2f}"
        return res
    res.update({"available": True, "em": EM_FACTOR * (call_q["ask"] + put_q["ask"])})
    return res


def vwap(bars: pd.DataFrame) -> float | None:
    b = _norm(bars)
    if len(b) == 0:
        return None
    vol = b["volume"].to_numpy(dtype=float)
    if vol.sum() <= 0:
        return None
    typical = b[["high", "low", "close"]].mean(axis=1).to_numpy(dtype=float)
    return float((typical * vol).sum() / vol.sum())


# ══ §2a broken check + §2c setup ══════════════════════════════════════════════════════════════════════════════════

def level_broken(lv: dict, above: bool, sip_today: pd.DataFrame, iex_gap: pd.DataFrame, stats: dict) -> tuple[bool, str]:
    """A level above the price (call side) is broken if any SIP bar after its set time reached it, or any IEX gap bar
    came within b_high of it; below the price (put side) mirrors with lows and b_low."""
    p = lv["price"]
    sip = _norm(sip_today)
    set_at = parse_ts(lv.get("set_at"))
    if set_at is not None and len(sip) > 0:
        sip = sip[sip.index > pd.Timestamp(set_at)]
    if len(sip) > 0:
        if above and float(sip["high"].max()) >= p:
            return True, "SIP high since level time crossed it"
        if not above and float(sip["low"].min()) <= p:
            return True, "SIP low since level time crossed it"
    gap = _norm(iex_gap)
    if len(gap) > 0:
        if above and stats.get("b_high") is not None and float(gap["high"].max()) >= p - stats["b_high"]:
            return True, "IEX gap high within b_high"
        if not above and stats.get("b_low") is not None and float(gap["low"].min()) <= p + stats["b_low"]:
            return True, "IEX gap low within b_low"
    return False, "unbroken"


def build_setup(ladder: list[dict], price: float, side: str, merge_distance: float | None,
                broken: dict[int, tuple[bool, str]]) -> dict:
    """§2c. CALL: trigger = first unbroken level above the price, not within one merge distance of it; target = the next
    level above the trigger; invalidation = the nearest level below the price. PUT mirrors. Missing parts are listed."""
    above = [i for i, lv in enumerate(ladder) if lv["price"] > price]
    below = [i for i, lv in enumerate(ladder) if lv["price"] < price]
    if side == "CALL":
        beyond, other = above, list(reversed(below))
    else:
        beyond, other = list(reversed(below)), above
    trig_pos = None
    skipped: list[dict] = []
    for pos, i in enumerate(beyond):
        lv = ladder[i]
        if broken.get(i, (False, ""))[0]:
            skipped.append({"price": lv["price"], "why": broken[i][1]})
            continue
        if merge_distance is not None and abs(lv["price"] - price) < merge_distance:
            skipped.append({"price": lv["price"], "why": "within one merge distance of the price"})
            continue
        trig_pos = pos
        break
    trigger = ladder[beyond[trig_pos]] if trig_pos is not None else None
    target = ladder[beyond[trig_pos + 1]] if trig_pos is not None and trig_pos + 1 < len(beyond) else None
    invalidation = ladder[other[0]] if other else None
    missing = [name for name, v in (("trigger", trigger), ("target", target), ("invalidation", invalidation)) if v is None]
    return {"side": side, "trigger": trigger, "target": target, "invalidation": invalidation, "missing": missing,
            "skipped": skipped, "complete": not missing}


# ══ §3 strikes ═════════════════════════════════════════════════════════════════════════════════════════════════════

def parse_chain(oi_map: dict[str, float], expiry: date) -> dict[str, dict]:
    """{occ: {'strike', 'is_call', 'expiry'}} for contracts expiring on `expiry` (the 0DTE set)."""
    out: dict[str, dict] = {}
    for occ in oi_map:
        p = gex._parse_occ(occ)
        if p is None:
            continue
        exp, is_call, strike = p
        if exp.date() == expiry:
            out[occ] = {"strike": strike, "is_call": is_call, "expiry": exp}
    return out


def candidate_contracts(chain: dict[str, dict], side: str, trigger: float, target: float) -> list[str]:
    """Strikes from the trigger out to the target, plus the first strike beyond the target (§3)."""
    is_call = side == "CALL"
    rows = sorted(((c["strike"], occ) for occ, c in chain.items() if c["is_call"] == is_call))
    if is_call:
        inside = [occ for k, occ in rows if trigger <= k <= target]
        past = [occ for k, occ in rows if k > target][:1]
    else:
        inside = [occ for k, occ in rows if target <= k <= trigger]
        past = [occ for k, occ in reversed(rows) if k < target][:1]
    return inside + past


def quote_from_snapshot(snap: dict | None) -> dict | None:
    q = (snap or {}).get("latestQuote") or {}
    if not q:
        return None
    try:
        return {"bid": float(q.get("bp") or 0.0), "ask": float(q.get("ap") or 0.0), "bid_size": q.get("bs"),
                "ask_size": q.get("as"), "bid_venue": q.get("bx"), "ask_venue": q.get("ax"),
                "condition": q.get("c"), "t": q.get("t")}
    except (TypeError, ValueError):
        return None


def two_sided(q: dict | None) -> tuple[bool, str]:
    if not q:
        return False, "no quote"
    if not (q.get("bid", 0) > 0):
        return False, "zero bid"
    if not (q.get("ask", 0) > 0):
        return False, "zero ask"
    if not (q["ask"] > q["bid"]):
        return False, "locked or crossed"
    return True, "two-sided"


def poll_quotes(contracts: list[str], now_fn: Callable[[], datetime] = utcnow,
                sleep_fn: Callable[[float], None] = time.sleep,
                poll_timeout_seconds: float = QUOTE_POLL_TIMEOUT_SECONDS) -> dict:
    """§3 rule 1: poll the latest quotes for every contract throughout a polling window. A quote is current only if its
    timestamp is inside the window (>= the window start). Ends early once every contract has a current quote.
    Returns {'window_start', 'window_end', 'quotes': {occ: quote|None (+ 'current', 'age_s')}, 'raw': [...], 'ok'}."""
    start = now_fn()
    quotes: dict[str, dict | None] = {c: None for c in contracts}
    raw: list = []
    fetched_any = False
    while True:
        try:
            snaps = fetch_snapshots(list(contracts)) if contracts else {}
        except Exception as e:
            logger.warning("morning_rundown: snapshot poll failed (%s)", e)
            snaps = {}
        raw.append({"at": iso_z(now_fn()), "snapshots": snaps})
        if snaps:
            fetched_any = True
        for c in contracts:
            q = quote_from_snapshot(snaps.get(c))
            if q is not None:
                quotes[c] = q
        polled_at = now_fn()
        for q in quotes.values():
            if q is not None:
                qt = parse_ts(q.get("t"))
                q["current"] = bool(qt is not None and qt >= start)
                q["age_s"] = (polled_at - qt).total_seconds() if qt is not None else None
        # end early only when every quote is both current and executable (two-sided); a fresh zero-bid / locked /
        # crossed quote keeps polling for the rest of the window (Codex pre-ship 11-09)
        if all(q is not None and q.get("current") and two_sided(q)[0] for q in quotes.values()):
            break
        if (now_fn() - start).total_seconds() >= poll_timeout_seconds:
            break
        sleep_fn(QUOTE_POLL_SLEEP_SECONDS)
    return {"window_start": start, "window_end": now_fn(), "quotes": quotes, "raw": raw, "ok": fetched_any}


def eligibility(q: dict | None, cap: float = PREMIUM_CAP_PER_SHARE) -> tuple[bool, str]:
    """§3 rule 1: two-sided AND current at send time AND ask <= cap. Last trade and mid never decide eligibility."""
    ok, why = two_sided(q)
    if not ok:
        return False, why
    assert q is not None
    if not q.get("current"):
        return False, "quote not current (no update inside the polling window)"
    if q["ask"] > cap:
        return False, f"ask {q['ask']:.2f} > cap {cap:.2f}"
    return True, "eligible"


def intrinsic(is_call: bool, strike: float, underlying: float) -> float:
    return max(0.0, underlying - strike) if is_call else max(0.0, strike - underlying)


def implied_vol(mid: float, S: float, K: float, T: float, is_call: bool) -> tuple[float | None, str]:
    """IV by bisection (data/gex.py), only when intrinsic < mid < S (call) / < K (put) and T > 0."""
    if T <= 0:
        return None, "no time to expiry"
    upper = S if is_call else K
    if not (intrinsic(is_call, K, S) < mid < upper):
        return None, "mid outside the no-arbitrage bounds"
    iv = gex._implied_vol(mid, S, K, T, is_call)
    return (iv, "ok") if iv else (None, "no IV root")


def model_premium(S: float, K: float, T: float, iv: float, is_call: bool) -> float:
    return float(gex._bs_price(S, K, T, iv, is_call))


def rank_candidates(cands: list[dict], target: float, invalidation: float, S: float, T: float) -> tuple[dict | None, list[dict]]:
    """v9.2 rank over ELIGIBLE candidates. reward = intrinsic at the target; risk = ask − BS premium at the invalidation
    price (decision time T, the quote's own IV). No IV root, or risk <= 0 -> fallback intrinsic ÷ ask (logged). Highest
    rank wins; ties go to the cheaper ask, then the strike nearer the money. If no eligible strike has intrinsic value at
    the target, the eligible strike nearest the money is picked with the note 'target does not reach this strike'."""
    ranked: list[dict] = []
    for c in cands:
        mid = float(np.mean([c["bid"], c["ask"]]))
        iv, iv_why = implied_vol(mid, S, c["strike"], T, c["is_call"])
        reward = intrinsic(c["is_call"], c["strike"], target)
        r = {**c, "mid": mid, "iv": iv, "iv_reason": iv_why, "reward": reward, "risk": None, "model_at_invalidation": None}
        if iv is not None:
            m_inv = model_premium(invalidation, c["strike"], T, iv, c["is_call"])
            r["model_at_invalidation"] = m_inv
            r["risk"] = c["ask"] - m_inv
        if r["risk"] is not None and r["risk"] > 0:
            r["rank"], r["rank_kind"] = reward / r["risk"], "reward_to_risk (model)"
        else:
            r["rank"], r["rank_kind"] = reward / c["ask"], "intrinsic_per_ask (fallback: " + (
                iv_why if iv is None else "model risk <= 0") + ")"
        r["moneyness"] = abs(c["strike"] - S)
        ranked.append(r)
    if not ranked:
        return None, ranked
    ranked.sort(key=lambda r: (-r["rank"], r["ask"], r["moneyness"]))
    if all(r["reward"] <= 0 for r in ranked):
        pick = dict(min(ranked, key=lambda r: (r["moneyness"], r["ask"])))
        pick["note"] = "target does not reach this strike"
        pick["rank_kind"] = "nearest the money (no eligible strike has intrinsic value at the target)"
        return pick, ranked
    return dict(ranked[0]), ranked


def model_stop(pick: dict, invalidation: float, T: float, bid: float) -> dict:
    """v9 item 3 + v9.3: sell-stop = BS premium at the invalidation price with the time then left (the quote's IV),
    rounded down to the tick. Placeable only when $0.05 < stop < the fresh bid."""
    out: dict[str, Any] = {"stop": None, "valid": False, "reason": None, "stop_check_bid": bid, "T_years": T}
    if pick.get("iv") is None:
        out["reason"] = f"no model stop ({pick.get('iv_reason') or 'no IV'})"
        return out
    raw = model_premium(invalidation, pick["strike"], T, pick["iv"], pick["is_call"])
    stop = floor_cent(raw)
    out["stop"], out["model_raw"] = stop, raw
    if not (stop > STOP_FLOOR_PER_SHARE):
        out["reason"] = f"model stop ${stop:.2f} <= ${STOP_FLOOR_PER_SHARE:.2f}"
    elif not (stop < bid):
        out["reason"] = f"model stop ${stop:.2f} at or above the bid ${bid:.2f}"
    else:
        out["valid"], out["reason"] = True, "placeable"
    return out


def select_strike(chain: dict[str, dict], setup: dict, S: float, decision_at: datetime, session_close: datetime,
                  poll: dict) -> dict:
    """Eligibility (rule 1) + ranking (rule 2) for a COMPLETE setup, from a finished quote poll. Returns the side's strike
    record: candidates (raw quote + rejection reason), pick, state hint."""
    cands = candidate_contracts(chain, setup["side"], setup["trigger"]["price"], setup["target"]["price"])
    T = years_between(session_close, decision_at)
    rows, eligible = [], []
    for occ in cands:
        q = poll["quotes"].get(occ)
        ok, why = eligibility(q)
        row = {"occ": occ, "strike": chain[occ]["strike"], "is_call": chain[occ]["is_call"], "quote": q,
               "eligible": ok, "reason": why}
        rows.append(row)
        if ok and q is not None:
            eligible.append({"occ": occ, "strike": chain[occ]["strike"], "is_call": chain[occ]["is_call"],
                             "bid": q["bid"], "ask": q["ask"], "quote": q})
    pick, ranked = rank_candidates(eligible, setup["target"]["price"], setup["invalidation"]["price"], S, T)
    any_current = any((r["quote"] or {}).get("current") and two_sided(r["quote"])[0] for r in rows)
    hint = None
    if pick is None:
        hint = NO_CONTRACT if any_current else NO_QUOTE
    return {"candidates": rows, "ranked": ranked, "pick": pick, "hint": hint, "T_years": T,
            "decision_at": iso_z(decision_at), "poll_window": [iso_z(poll["window_start"]), iso_z(poll["window_end"])]}


# ══ §3 / §6 rendering + violation checks ══════════════════════════════════════════════════════════════════════════

_TRIGGER_WORDS = re.compile(r"closes (above|below)|only if|breaks (above|below)|buy the", re.I)
_LIMIT_STOP_NUM = re.compile(r"Limit \$\d|sell-stop[^\n]*\$\d|Stop: *\$\d", re.I)


def _strike_label(pick: dict) -> str:
    k = pick["strike"]
    ks = f"{k:.0f}" if float(k).is_integer() else f"{k:g}"
    return f"{ks}{'C' if pick['is_call'] else 'P'}"


def side_text(symbol: str, rec: dict) -> str:
    """One side section (WATCHING / REFERENCE ONLY / NO ...). Never a limit or a stop: those exist only in TRIGGERED."""
    side, state, setup = rec["side"], rec["state"], rec["setup"]
    icon = "📈" if side == "CALL" else "📉"
    word = "above" if side == "CALL" else "below"
    pick = (rec.get("strike") or {}).get("pick")
    tg, tt, inv = setup.get("trigger"), setup.get("target"), setup.get("invalidation")
    cap = f"${PREMIUM_CAP_PER_SHARE:.2f}"
    if state == NO_SETUP:
        return f"{icon} {side} — {NO_SETUP} — missing {', '.join(setup['missing'])}"
    parts = (f"Target {tt['price']:.2f} · Invalidation {inv['price']:.2f}" if tt and inv else "")
    if state == "WATCHING" and pick:
        q = pick["quote"]
        kind = "call" if side == "CALL" else "put"
        maxloss = pick["ask"] * SHARES_PER_CONTRACT
        return (f"{icon} {side} 👀 WATCHING — If {symbol} closes {word} ${tg['price']:.2f}, buy the {symbol} "
                f"${pick['strike']:g} {kind}, ${pick['ask']:.2f} at {fmt_pt(parse_ts(q.get('t')), seconds=True)} "
                f"(re-checked at trigger) · max loss ≤ ${maxloss:.0f}\n   {parts}"
                + (f" · {pick['note']}" if pick.get("note") else "")
                + f"\n   _rank: {pick['rank_kind']}_")
    trig = f"trigger {tg['price']:.2f} (not verified)" if tg else "trigger n/a"
    if state in (NO_QUOTE, NO_CONTRACT):
        why = rec.get("state_reason") or ""
        return f"{icon} {side} — {state} ({why}) · {trig} · {parts}".rstrip(" ·")
    ref = ""
    if pick:
        ref = (f" · reference strike {_strike_label(pick)} ask ${pick['ask']:.2f} at "
               f"{fmt_pt(parse_ts(pick['quote'].get('t')), seconds=True)}")
    return (f"{icon} {side} — REFERENCE ONLY — {rec.get('state_reason') or 'trigger history not verified'} · {trig} · "
            f"{parts}{ref} · Limit/Stop: N/A — stale data · cap {cap}")


def reference_form(rec: dict, keys: list[str]) -> str:
    icon = "📈" if rec["side"] == "CALL" else "📉"
    return f"{icon} {rec['side']} — REFERENCE ONLY — section withheld (check failed: {', '.join(keys)}) · Limit/Stop: N/A"


def check_violations(rec: dict, text: str) -> list[str]:
    """§6 renderer check on the FINAL section text + its record. Every key returned is a violation (count must be 0)."""
    found: list[str] = []
    state = rec["state"]
    pick = (rec.get("strike") or {}).get("pick")
    if pick and pick.get("ask", 0) > PREMIUM_CAP_PER_SHARE:
        found.append("ask_over_cap")
    for m in re.finditer(r"\$(\d+\.\d\d) at \d\d:\d\d", text):
        if float(m.group(1)) > PREMIUM_CAP_PER_SHARE and "ask_over_cap" not in found:
            found.append("ask_over_cap")
    if pick and not (pick.get("quote") or {}).get("current"):
        found.append("quote_not_current")
    verified = state in ("WATCHING", "ACTIONABLE")
    if not verified and _TRIGGER_WORDS.search(text):
        found.append("trigger_text_without_history")
    if pick and not rec["setup"].get("complete"):
        found.append("ranked_without_setup")
    if state == "ACTIONABLE" and not (rec.get("stop") or {}).get("valid"):
        found.append("actionable_without_stop")
    if state != "ACTIONABLE" and _LIMIT_STOP_NUM.search(text):
        found.append("limit_or_stop_shown_when_stale")
    return found


def finalize_side(symbol: str, rec: dict, counts: dict) -> str:
    """Render, check, and replace a violating section with its reference form (logged; counted)."""
    text = side_text(symbol, rec)
    keys = check_violations(rec, text)
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    if keys:
        logger.error("morning_rundown: %s %s section replaced — violations %s", symbol, rec["side"], keys)
        rec["violations"] = keys
        rec["state_before_check"] = rec["state"]
        rec["state"] = "REFERENCE ONLY"
        return reference_form(rec, keys)
    return text


def ladder_text(ladder: list[dict], price: float | None, markers: dict[int, str]) -> str:
    lines = []
    placed = False
    for i in sorted(range(len(ladder)), key=lambda j: -ladder[j]["price"]):
        lv = ladder[i]
        if price is not None and not placed and lv["price"] < price:
            lines.append(f"   ── now {price:.2f} ──")
            placed = True
        dot = "🔴" if price is None or lv["price"] > price else "🟢"
        tag = " · confluence" if lv.get("confluence") else ""
        mk = f"   ← {markers[i]}" if i in markers else ""
        lines.append(f"{dot} {lv['price']:.2f}  {' + '.join(lv['sources'])}{tag}{mk}")
    if price is not None and not placed:
        lines.append(f"   ── now {price:.2f} ──")
    return "\n".join(lines) if lines else "(no levels)"


# ══ per-symbol build ═══════════════════════════════════════════════════════════════════════════════════════════════

def _gex_levels(symbol: str, spot: float, today: date) -> dict:
    """GEX walls/flip with prior-close OI (data/gex.py internals) + the raw chain for the 0DTE set and the archive."""
    out: dict = {"label": "UNKNOWN", "levels": [], "oi_map": {}, "snapshots": {}, "reason": None}
    try:
        date_gte, date_lte = gex._expiry_range()
        oi_map = fetch_contracts(symbol, date_gte, date_lte)
        out["oi_map"] = oi_map
        if not oi_map:
            out["reason"] = "no contracts"
            return out
        snaps = fetch_snapshots(list(oi_map))
        out["snapshots"] = snaps
        if not snaps:
            out["reason"] = "no snapshots"
            return out
        gx = gex._compute_gex(snaps, oi_map, spot)
        out["label"] = gx.get("label", "UNKNOWN")
        pin = gx.get("pin") if isinstance(gx.get("pin"), dict) else {}
        for key, name in (("call_wall", "call wall"), ("put_wall", "put wall")):
            v = (pin or {}).get(key)
            if isinstance(v, (int, float)):
                out["levels"].append(level(v, f"{name} (GEX, prior-close OI)", None))
        flip = gx.get("flip_strike")
        if isinstance(flip, (int, float)):
            out["levels"].append(level(flip, "gamma flip (GEX, prior-close OI)", None))
    except Exception as e:
        logger.warning("morning_rundown: GEX failed for %s (%s)", symbol, e)
        out["reason"] = f"error: {str(e)[:80]}"
    return out


def build_symbol(symbol: str, send: str, now: datetime, cal_today: dict, sessions: list[date],
                 now_fn: Callable[[], datetime] = utcnow, sleep_fn: Callable[[float], None] = time.sleep) -> dict:
    """Everything for one symbol (isolated by the caller). Returns the symbol record used by render/log/plan."""
    today = cal_today["date"]
    rec: dict = {"symbol": symbol, "send": send, "sides": {}, "errors": []}
    day_start_pt = datetime.combine(today, OVERNIGHT_START_PT, tzinfo=PT).astimezone(UTC)
    sip_end = now - SIP_DELAY
    sip_today = completed(get_bars(symbol, day_start_pt, sip_end, "sip"), now) if sip_end > day_start_pt else _norm(None)
    sip_cutoff = sip_today.index[-1].to_pydatetime() if len(sip_today) else None
    rec["sip_cutoff"] = iso_z(sip_cutoff)
    level_price = float(sip_today["close"].iloc[-1]) if len(sip_today) else None
    rec["level_price"] = {"value": level_price, "time": iso_z(sip_cutoff), "source": "SIP one-minute bar (delayed)"}

    # current price (IEX latest trade) — trade age vs t_max decided after the stats
    lt = latest_trade(symbol)
    price, trade_ts = (lt if lt else (None, None))
    trade_age_seconds = (now - trade_ts).total_seconds() if trade_ts else None

    # clock-window statistics over the last sessions: the window is [SIP cutoff time-of-day, send time-of-day)
    win_end_et = now.astimezone(ET)
    win_start_et = (sip_cutoff or (now - SIP_DELAY)).astimezone(ET)
    hist_sip = hist_iex = _norm(None)
    if sessions:
        h_start = datetime.combine(sessions[0], dtime(0, 0), tzinfo=ET)
        h_end = datetime.combine(sessions[-1], dtime(0, 0), tzinfo=ET) + timedelta(days=1)
        hist_sip = get_bars(symbol, h_start, h_end, "sip")
        hist_iex = get_bars(symbol, h_start, h_end, "iex")
    iex_today = completed(get_bars(symbol, day_start_pt, now, "iex"), now)
    stats = derive_stats(hist_sip, hist_iex, sessions, win_start_et.time(), win_end_et.time(),
                         today_sip=sip_today, today_iex=iex_today)
    rec["stats"] = stats
    t_max = stats["t_max_min"]
    price_current = bool(price is not None and trade_age_seconds is not None and t_max is not None
                         and trade_age_seconds / 60 < t_max)
    rec["current_price"] = {"value": price, "trade_time": iso_z(trade_ts), "age_s": trade_age_seconds, "t_max_min": t_max,
                            "current": price_current, "source": "IEX latest trade"}

    # IEX trigger history: every minute from the SIP cutoff to the last completed minute
    last_done = floor_minute(now) - ONE_MINUTE
    gap = _norm(None)
    gap_missing: list[datetime] = []
    if sip_cutoff is not None:
        gap = iex_today[iex_today.index > pd.Timestamp(sip_cutoff)]
        gap_missing = missing_minutes(gap, sip_cutoff, last_done)
        # T2: a TRAILING unpublished minute (only the tail is missing, bars exist before it) is waited for within t_max;
        # it never by itself forces REFERENCE ONLY. The 06:00 send is reference anyway, so it never waits.
        while send == "0646" and gap_missing and t_max is not None and len(gap) > 0 \
                and not bool((gap.index > pd.Timestamp(gap_missing[0])).any()):
            tail_age_seconds = (now_fn() - (gap_missing[0] + ONE_MINUTE)).total_seconds()
            if tail_age_seconds / 60 >= t_max:
                break
            logger.info("morning_rundown: %s IEX minute %s not published yet — waiting (t_max %.1f min)", symbol,
                        iso_z(gap_missing[0]), t_max)
            sleep_fn(QUOTE_POLL_SLEEP_SECONDS)
            iex_today = completed(get_bars(symbol, day_start_pt, now_fn(), "iex"), now_fn())
            gap = iex_today[iex_today.index > pd.Timestamp(sip_cutoff)]
            gap = gap[gap.index <= pd.Timestamp(last_done)]
            gap_missing = missing_minutes(gap, sip_cutoff, last_done)
    history_complete = sip_cutoff is not None and not gap_missing
    rec["iex_history"] = {"from": iso_z(sip_cutoff), "to": iso_z(last_done), "complete": history_complete,
                          "missing_minutes": len(gap_missing), "missing_list": [iso_z(m) for m in gap_missing]}
    rec["iex_cutoff_bar_ts"] = iso_z(gap.index[-1].to_pydatetime()) if len(gap) else iso_z(sip_cutoff)

    # levels
    levels: list[dict] = []
    prev = [d for d in sessions if d < today]
    if prev:
        pd_start = datetime.combine(prev[-1], dtime(0, 0), tzinfo=ET)
        daily = _norm(get_daily_bars(symbol, pd_start, pd_start + timedelta(days=1)))
        if len(daily):
            row = daily.iloc[-1]
            # set_at None: every SIP bar of today's window (from 01:00 PT, after the prior close) counts as "since"
            levels += [level(float(row["high"]), "prior-day high", None), level(float(row["low"]), "prior-day low", None),
                       level(float(row["close"]), "prior-day close", None)]
    if len(sip_today):
        hi_at = sip_today["high"].idxmax().to_pydatetime()
        lo_at = sip_today["low"].idxmin().to_pydatetime()
        levels += [level(float(sip_today["high"].max()), "overnight high", hi_at),
                   level(float(sip_today["low"].min()), "overnight low", lo_at)]
    spot = price if price is not None else level_price
    gx: dict[str, Any] = (_gex_levels(symbol, spot, today) if spot else
                          {"label": "UNKNOWN", "levels": [], "oi_map": {}, "snapshots": {}, "reason": "no spot"})
    rec["gex"] = {"label": gx["label"], "reason": gx["reason"], "levels": gx["levels"]}
    levels += gx["levels"]
    chain = parse_chain(gx["oi_map"], today)
    rec["raw_chain"] = {"oi_map": gx["oi_map"], "snapshots": gx["snapshots"]}

    # expected move from the ATM straddle asks (current two-sided quotes on both legs)
    em_res: dict = {"available": False, "reason": "no spot or chain"}
    atm_poll: dict = {}
    if spot and chain:
        strikes = sorted({c["strike"] for c in chain.values()})
        pairs = [k for k in strikes if any(c["strike"] == k and c["is_call"] for c in chain.values())
                 and any(c["strike"] == k and not c["is_call"] for c in chain.values())]
        if pairs:
            k_atm = min(pairs, key=lambda k: abs(k - spot))
            c_occ = next(o for o, c in chain.items() if c["strike"] == k_atm and c["is_call"])
            p_occ = next(o for o, c in chain.items() if c["strike"] == k_atm and not c["is_call"])
            atm_poll = poll_quotes([c_occ, p_occ], now_fn=now_fn, sleep_fn=sleep_fn)
            em_res = expected_move(atm_poll["quotes"].get(c_occ), atm_poll["quotes"].get(p_occ), stats["merge_distance"])
            em_res["atm_strike"] = k_atm
            if em_res["available"]:
                em_at = atm_poll["window_end"]
                levels += [level(spot + em_res["em"], "EM high (approx.)", em_at),
                           level(spot - em_res["em"], "EM low (approx.)", em_at)]
    rec["em"] = em_res
    rec["raw_atm_poll"] = atm_poll.get("raw", [])

    if send == "0646":
        vw_bars = completed(get_bars(symbol, cal_today["open"].astimezone(UTC), now, "iex"), now)
        vw = vwap(vw_bars)
        if vw is not None:
            levels.append(level(vw, "VWAP (IEX, completed bars from the open)", now))

    ladder = merge_levels(levels, stats["merge_distance"])
    rec["ladder"] = ladder
    ref_price = price if price is not None else level_price
    stats_ok = all(stats.get(k) is not None for k in ("merge_distance", "t_max_min", "b_high", "b_low"))
    verified = bool(price_current and history_complete and stats_ok)
    reasons = []
    if send == "0600":
        reasons.append("pre-market: Alpaca takes no extended-hours option orders")
    if not price_current:
        reasons.append(f"current price not current (IEX trade {fmt_pt(trade_ts, seconds=True)}, "
                       f"age {trade_age_seconds if trade_age_seconds is None else round(trade_age_seconds)}s, t_max "
                       f"{'n/a' if t_max is None else round(t_max, 1)} min)")
    if not history_complete:
        reasons.append(f"trigger history not verified ({len(gap_missing)} IEX gap minute(s) missing)")
    if not stats_ok:
        reasons.append("t_max / buffers undefined (no session statistics)")
    rec["verified"] = verified and send == "0646"

    poll_all: dict = {}
    setups: dict[str, dict] = {}
    if ref_price is not None:
        for side in SIDES:
            above = side == "CALL"
            broken = {i: level_broken(lv, lv["price"] > ref_price, sip_today, gap, stats) for i, lv in enumerate(ladder)}
            setups[side] = build_setup(ladder, ref_price, side, stats["merge_distance"], broken)
            setups[side]["broken_checks"] = [{"price": ladder[i]["price"], "broken": b[0], "why": b[1]}
                                             for i, b in broken.items() if (ladder[i]["price"] > ref_price) == above]
        to_poll = sorted({occ for s in setups.values() if s["complete"]
                          for occ in candidate_contracts(chain, s["side"], s["trigger"]["price"], s["target"]["price"])})
        if to_poll:
            poll_all = poll_quotes(to_poll, now_fn=now_fn, sleep_fn=sleep_fn)
    rec["raw_candidate_poll"] = poll_all.get("raw", [])
    # The quote polls above can take minutes: re-check the IEX trade's age now, so a WATCHING state never rests on a
    # price that went past t_max while the polls ran (Codex pre-ship review 11-08).
    if rec["verified"] and trade_ts is not None and t_max is not None:
        age_after_polls_seconds = (now_fn() - trade_ts).total_seconds()
        if age_after_polls_seconds / 60 >= t_max:
            rec["verified"] = False
            rec["current_price"]["current_after_polls"] = False
            reasons.append(f"current price went stale during the quote polls (IEX trade age "
                           f"{round(age_after_polls_seconds)}s, t_max {round(t_max, 1)} min)")
    # The trigger history was checked before the polls; minutes that completed while they ran are re-checked now, so
    # a setup whose trigger broke during the polls is never published as WATCHING (Codex pre-ship review 11-09).
    broken_after_polls: dict[str, str] = {}
    if rec["verified"] and sip_cutoff is not None:
        now_after = now_fn()
        last_done_after = floor_minute(now_after) - ONE_MINUTE
        if last_done_after > last_done:
            try:
                iex_after = completed(get_bars(symbol, day_start_pt, now_after, "iex"), now_after)
            except Exception as e:
                logger.warning("morning_rundown: %s post-poll IEX bar fetch failed (%s)", symbol, e)
                iex_after = None
            gap_after = _norm(None) if iex_after is None else iex_after[iex_after.index > pd.Timestamp(sip_cutoff)]
            gap_after = gap_after[gap_after.index <= pd.Timestamp(last_done_after)]
            missing_after = missing_minutes(gap_after, sip_cutoff, last_done_after) if iex_after is not None else [None]
            if missing_after:
                rec["verified"] = False
                reasons.append("trigger history not verified after the quote polls "
                               f"({len(missing_after)} IEX minute(s) missing or unreadable)")
            else:
                rec["iex_history"]["to"] = iso_z(last_done_after)
                rec["iex_cutoff_bar_ts"] = iso_z(gap_after.index[-1].to_pydatetime()) if len(gap_after) else \
                    rec["iex_cutoff_bar_ts"]
                assert ref_price is not None
                for side, s in setups.items():
                    if s.get("complete") and s.get("trigger"):
                        hit, why = level_broken(s["trigger"], s["trigger"]["price"] > ref_price, sip_today, gap_after,
                                                stats)
                        if hit:
                            broken_after_polls[side] = why

    for side in SIDES:
        setup = setups.get(side) or {"side": side, "trigger": None, "target": None, "invalidation": None,
                                     "missing": ["trigger", "target", "invalidation"], "complete": False, "skipped": []}
        srec: dict = {"side": side, "setup": setup, "strike": None, "state": None, "state_reason": None}
        if not setup["complete"]:
            srec["state"] = NO_SETUP
            logger.info("morning_rundown: %s %s NO ACTIONABLE SETUP — missing %s", symbol, side, setup["missing"])
        else:
            poll = poll_all or {"window_start": now, "window_end": now, "quotes": {}, "raw": []}
            assert ref_price is not None
            st = select_strike(chain, setup, ref_price, poll["window_end"], cal_today["close"], poll)
            srec["strike"] = st
            if st["pick"] is None:
                srec["state"] = st["hint"]
                lo = min((r["quote"]["ask"] for r in st["candidates"]
                          if r["quote"] and r["quote"].get("current") and two_sided(r["quote"])[0]), default=None)
                srec["state_reason"] = (f"lowest current ask ${lo:.2f}" if st["hint"] == NO_CONTRACT and lo is not None else
                                        f"no 0DTE {side.lower()} quote updated {fmt_pt(parse_ts(st['poll_window'][0]), True)}–"
                                        f"{fmt_pt(parse_ts(st['poll_window'][1]), True)}")
            elif rec["verified"]:
                srec["state"] = "WATCHING"
            else:
                srec["state"] = "REFERENCE ONLY"
                srec["state_reason"] = "; ".join(reasons) or "trigger history not verified"
        if side in broken_after_polls:
            # whatever the quote state, a trigger that broke during the polls is never watched (cold review 11-09:
            # NO_QUOTE / NO_CONTRACT sides are watched too, and the cursor has moved past the break)
            srec["state"] = "REFERENCE ONLY"
            srec["state_reason"] = f"trigger already broken during the quote polls ({broken_after_polls[side]})"
        if srec["state"] in (NO_QUOTE, NO_CONTRACT) and not rec["verified"]:
            srec["state_reason"] = (srec["state_reason"] or "") + "; reference only: " + "; ".join(reasons)
        rec["sides"][side] = srec
    return rec


# ══ message, logs, plan ════════════════════════════════════════════════════════════════════════════════════════════

def _symbol_header(rec: dict) -> str:
    cp, hist, em = rec["current_price"], rec["iex_history"], rec["em"]
    now_s = (f"{cp['value']:.2f} (IEX trade, {round(cp['age_s'])}s old)" if cp.get("value") is not None and cp.get("age_s") is not None
             else "n/a")
    hist_s = (f"IEX history {fmt_pt(parse_ts(hist['from']))}–{fmt_pt(parse_ts(hist['to']))} "
              + ("complete" if hist["complete"] else f"incomplete ({hist['missing_minutes']} min missing)"))
    em_s = f"EM ±${em['em']:.2f} (asks, approx.)" if em.get("available") else f"EM n/a ({em.get('reason')})"
    gex_s = f"GEX regime: {rec['gex']['label']} (gex.py classifier)" if rec["gex"]["label"] != "UNKNOWN" else \
        f"GEX n/a ({rec['gex'].get('reason')})"
    md = rec["stats"].get("merge_distance")
    md_s = f"merge {md:.2f}" + (" (today only)" if rec["stats"].get("basis") == "today only" else "") if md is not None \
        else "merge distance n/a"
    return (f"*{rec['symbol']}* now {now_s} · SIP to {fmt_pt(parse_ts(rec['sip_cutoff']))} · {hist_s}\n"
            f"{em_s} · {gex_s} · {md_s}")


def render(results: list[dict], send: str, now: datetime, counts: dict) -> tuple[list, str, str]:
    """Block Kit blocks, the fallback text (carries both ideas per symbol) and the markdown archive."""
    title = (f"☀️ Morning Rundown · {now.astimezone(PT).strftime('%a %b %d')} · {fmt_pt(now)}"
             + (" · PRE-MARKET · reference" if send == "0600" else " · watchlist"))
    blocks: list = [{"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}}]
    md_lines = [f"# {title}", ""]
    fb_bits: list[str] = []
    for rec in results:
        blocks.append({"type": "divider"})
        if rec.get("failed"):
            txt = f"*{rec['symbol']}* — build failed ({rec['failed']}); no ideas for this symbol"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": txt}})
            md_lines += [txt, ""]
            fb_bits.append(f"{rec['symbol']}: build failed")
            continue
        head = _symbol_header(rec)
        markers: dict[int, str] = {}
        for side in SIDES:
            s = rec["sides"][side]["setup"]
            for part, label in (("trigger", f"{side} trigger"), ("target", f"{side.lower()} target")):
                lv = s.get(part)
                if lv is not None:
                    idx = next((i for i, x in enumerate(rec["ladder"]) if x is lv or x == lv), None)
                    if idx is not None:
                        markers[idx] = label
        ladder = ladder_text(rec["ladder"], rec["current_price"]["value"] or rec["level_price"]["value"], markers)
        sides_txt = [finalize_side(rec["symbol"], rec["sides"][side], counts) for side in SIDES]
        body = f"{head}\n```\n{ladder}\n```\n" + "\n".join(sides_txt)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}})
        md_lines += [head, "", "```", ladder, "```", *sides_txt, ""]
        for side in SIDES:
            s = rec["sides"][side]
            pk = (s.get("strike") or {}).get("pick")
            if s["state"] == "WATCHING" and pk:
                fb_bits.append(f"{rec['symbol']} {side} {'>' if side == 'CALL' else '<'} "
                               f"{s['setup']['trigger']['price']:.2f}: {_strike_label(pk)} ${pk['ask']:.2f}")
            else:
                fb_bits.append(f"{rec['symbol']} {side}: {s['state']}")
    cap = f"${PREMIUM_CAP_PER_SHARE:.2f}"
    footer = (f"🚫 Don't: buy from this list · pay above {cap} · chase after the target prints · hold without the sell-stop\n"
              "_No TRIGGERED message = no entry. Limit and stop come with the TRIGGERED alert. The bot places no orders for these._")
    if send == "0600":
        footer += "\n_The watchlist comes at 06:46 PT; entries come only as TRIGGERED alerts after that._"
    blocks += [{"type": "divider"}, {"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]}]
    md_lines += ["---", footer, ""]
    fallback = f"{title}: " + " · ".join(fb_bits)
    return blocks, fallback, "\n".join(md_lines) + "\n"


def _jsonable(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


def rec_rows(results: list[dict], now: datetime) -> list[dict]:
    """§6 morning_rundown_recs.jsonl: one row per symbol, side and send."""
    rows = []
    for rec in results:
        if rec.get("failed"):
            rows.append({**stamp(now), "symbol": rec["symbol"], "send": rec.get("send"), "failed": rec["failed"]})
            continue
        for side in SIDES:
            s = rec["sides"][side]
            st = s.get("strike") or {}
            rows.append(_jsonable({
                **stamp(now), "symbol": rec["symbol"], "side": side, "send": rec["send"],
                "level_price": rec["level_price"], "current_price": rec["current_price"], "iex_history": rec["iex_history"],
                "stats": rec["stats"], "levels": rec["ladder"], "setup": {k: s["setup"].get(k) for k in
                                                                           ("trigger", "target", "invalidation", "missing", "complete", "skipped", "broken_checks")},
                "em": rec["em"], "gex": rec["gex"], "candidates": st.get("candidates"),
                "pick": {k: v for k, v in (st.get("pick") or {}).items() if k != "quote"} or None,
                "pick_quote": (st.get("pick") or {}).get("quote"), "decision_at": st.get("decision_at"),
                "T_years": st.get("T_years"), "state": s["state"], "state_reason": s.get("state_reason"),
                "violations": s.get("violations", []), "r": gex._RISK_FREE_RATE, "q": gex._DIV_YIELD,
            }))
    return rows


def recommendation_rows(results: list[dict], now: datetime) -> list[dict]:
    """v8 logs/0dte_recommendations.jsonl: one row per printed 06:46 line (both sides, both symbols)."""
    rows = []
    for rec in results:
        if rec.get("failed"):
            continue
        for side in SIDES:
            s = rec["sides"][side]
            setup, pk = s["setup"], (s.get("strike") or {}).get("pick")
            rows.append(_jsonable({
                **stamp(now), "event": "printed", "rec_id": f"{now.astimezone(ET).date()}-{rec['symbol']}-{side}",
                "date": str(now.astimezone(ET).date()), "symbol": rec["symbol"], "side": side, "state": s["state"],
                "trigger": (setup.get("trigger") or {}).get("price"), "out": (setup.get("invalidation") or {}).get("price"),
                "target": (setup.get("target") or {}).get("price"),
                "level_sources": {k: (setup.get(k) or {}).get("sources") for k in ("trigger", "target", "invalidation")},
                "contract": (pk or {}).get("occ"), "ask_at_print": (pk or {}).get("ask"),
                "ask_at_print_time": ((pk or {}).get("quote") or {}).get("t"), "ask_at_trigger": None,
                "triggered": False, "triggered_at": None, "r": gex._RISK_FREE_RATE, "q": gex._DIV_YIELD,
            }))
    return rows


def plan_path(day: date) -> Path:
    return _LOGS / f"morning_rundown_plan_{day.isoformat()}.json"


def build_plan(results: list[dict], now: datetime, cal_today: dict) -> dict:
    """The 06:46 watchlist the watcher consumes: per symbol the stats and IEX cutoff, per side the setup + candidates.
    Only sides with a complete setup AND verified history are watched (a REFERENCE ONLY side may already be broken)."""
    plan: dict = {**stamp(now), "date": cal_today["date"].isoformat(), "session_close": cal_today["close"].isoformat(),
                  "symbols": {}}
    for rec in results:
        if rec.get("failed"):
            continue
        sym = {"stats": rec["stats"], "iex_cutoff_bar_ts": rec["iex_cutoff_bar_ts"], "sides": {}}
        for side in SIDES:
            s = rec["sides"][side]
            setup = s["setup"]
            watch = bool(setup["complete"] and rec["verified"] and s["state"] in ("WATCHING", NO_QUOTE, NO_CONTRACT))
            cands = []
            if setup["complete"]:
                cands = [{"occ": r["occ"], "strike": r["strike"], "is_call": r["is_call"]}
                         for r in (s.get("strike") or {}).get("candidates") or []]
            sym["sides"][side] = {"state_0646": s["state"], "watch": watch,
                                  "trigger": (setup.get("trigger") or {}).get("price"),
                                  "target": (setup.get("target") or {}).get("price"),
                                  "invalidation": (setup.get("invalidation") or {}).get("price"),
                                  "sources": {k: (setup.get(k) or {}).get("sources") for k in ("trigger", "target", "invalidation")},
                                  "candidates": cands,
                                  "pick_0646": ((s.get("strike") or {}).get("pick") or {}).get("occ"),
                                  "rec_id": f"{cal_today['date']}-{rec['symbol']}-{side}"}
        plan["symbols"][rec["symbol"]] = sym
    return _jsonable(plan)


def weekly_marker_name(day: date) -> str:
    iso = day.isocalendar()
    return f"gex_weekly_sent_{iso[0]}-W{iso[1]:02d}.json"


def log_weekly_gex_skip(today: date, week_days: list[date]) -> str | None:
    """§5: from Wednesday on, no marker this ISO week and neither Monday nor Tuesday traded -> log the skip line."""
    if today.weekday() < 2 or (_LOGS / weekly_marker_name(today)).exists():
        return None
    monday = today - timedelta(days=today.weekday())
    if any(d in week_days for d in (monday, monday + timedelta(days=1))):
        return None
    msg = "weekly GEX report skipped: no trading Mon/Tue"
    logger.info("morning_rundown: %s", msg)
    return msg


def run(send: str, dry_run: bool = False, now: datetime | None = None,
        now_fn: Callable[[], datetime] = utcnow, sleep_fn: Callable[[float], None] = time.sleep) -> int:
    """One send. Returns 0 on success / no-send day, 1 on a build or delivery failure (heartbeat ok=false)."""
    now = now or now_fn()
    clock = clock_record(now, "09:00" if send == "0600" else "09:46")
    logger.info("morning_rundown: send=%s now_utc=%s now_et=%s et_offset=%s wrapper_target_et=%s", send,
                clock["now_utc"], clock["now_et"], clock["detected_et_offset"], clock["wrapper_target_et"])
    today = now.astimezone(ET).date()
    hb_base = {"last_run_utc": iso_z(now), "last_run_local": now.astimezone(PT).isoformat(), "send": send,
               "detected_et_offset": clock["detected_et_offset"], "clock": clock}
    cal = calendar(today - timedelta(weeks=STATS_SESSIONS), today)
    if cal is None:
        update_heartbeat({**hb_base, "ok": False, "slack_ok": False, "md_ok": False, "note": "calendar unavailable"})
        if not dry_run:
            send_slack(f":warning: Morning rundown {send}: Alpaca calendar unavailable — no rundown sent")
        return 1
    cal_today = next((c for c in cal if c["date"] == today), None)
    if cal_today is None:
        logger.info("morning_rundown: market closed today per the Alpaca calendar — no send")
        update_heartbeat({**hb_base, "ok": True, "slack_ok": None, "md_ok": None, "note": "market closed"})
        return 0
    week_days = [c["date"] for c in cal if today - timedelta(days=today.weekday()) <= c["date"] <= today]
    skip_line = log_weekly_gex_skip(today, week_days)
    sessions = [c["date"] for c in cal if c["date"] < today][-STATS_SESSIONS:]
    results: list[dict] = []
    for sym in SYMBOLS:
        try:
            results.append(build_symbol(sym, send, now, cal_today, sessions, now_fn=now_fn, sleep_fn=sleep_fn))
        except Exception as e:   # per-symbol isolation
            logger.error("morning_rundown: %s build failed (%s)", sym, e, exc_info=True)
            results.append({"symbol": sym, "send": send, "failed": str(e)[:120]})
    counts: dict = {k: 0 for k in VIOLATION_KEYS}
    try:
        blocks, fallback, md = render(results, send, now, counts)
    except Exception as e:
        logger.error("morning_rundown: render failed (%s)", e, exc_info=True)
        if not dry_run:
            send_slack(f":warning: Morning rundown {send} FAILED to build: {str(e)[:120]}")
        update_heartbeat({**hb_base, "ok": False, "slack_ok": False, "md_ok": False, "note": f"render failed: {e}"})
        return 1
    if skip_line:
        md += f"\n_{skip_line}_\n"

    # archives first (they never depend on Slack)
    stamp_name = f"{today.isoformat()}_{now.astimezone(PT).strftime('%H%M')}"
    try:
        raw = {"stamp": stamp(now), "symbols": {r["symbol"]: {k: r.get(k) for k in ("raw_chain", "raw_atm_poll", "raw_candidate_poll")}
                                               for r in results}}
        atomic_write_bytes(STATE["chain_dir"] / f"{stamp_name}.json.gz",
                           gzip.compress(json.dumps(raw, default=str).encode("utf-8")))
    except Exception as e:
        logger.warning("morning_rundown: chain archive failed (%s)", e)
    plan_ok: bool | None = None
    if send == "0646":            # T3: the watcher's plan first, in its own try (a log failure never costs the plan)
        try:
            atomic_write_text(plan_path(today), json.dumps(build_plan(results, now, cal_today), indent=1))
            plan_ok = True
        except Exception as e:
            plan_ok = False
            logger.error("morning_rundown: plan write failed (%s) — the watcher has nothing to watch", e)
    try:
        locked_append_jsonl(STATE["recs"], rec_rows(results, now))
    except Exception as e:
        logger.error("morning_rundown: recs JSONL append failed (%s)", e)
    if send == "0646":
        try:
            locked_append_jsonl(STATE["recommendations"], recommendation_rows(results, now))
        except Exception as e:
            logger.error("morning_rundown: recommendations JSONL append failed (%s)", e)
    md_ok = False
    try:
        atomic_write_text(_LOGS / f"morning_rundown_{today.isoformat()}{'' if send == '0646' else '_' + send}.md", md)
        md_ok = True
    except Exception as e:
        logger.warning("morning_rundown: md write failed (%s)", e)

    slack_ok: bool | None = None
    if dry_run:
        logger.info("morning_rundown: dry run — built, not sent\n%s", fallback)
    else:
        try:
            slack_ok = bool(send_slack_blocks(blocks, fallback))
        except Exception as e:
            slack_ok = False
            logger.warning("morning_rundown: Slack send raised (%s)", e)
        if slack_ok:
            logger.info("morning_rundown: Slack blocks sent (send=%s)", send)
        else:
            logger.warning("morning_rundown: Slack delivery FAILED or not configured (send=%s)", send)

    prev = read_json(STATE["heartbeat"]) or {}
    vt = prev.get("violations_today") if prev.get("violations_today_date") == today.isoformat() else None
    vt = {k: int((vt or {}).get(k, 0)) + counts.get(k, 0) for k in VIOLATION_KEYS}
    sym_hb = {}
    for r in results:
        if r.get("failed"):
            sym_hb[r["symbol"]] = {"failed": r["failed"]}
            continue
        st = r["stats"]
        sym_hb[r["symbol"]] = {"b_high": st["b_high"], "b_low": st["b_low"], "sigma_1m": st["sigma_1m"],
                               "t_max": st["t_max_min"], "merge_distance": st["merge_distance"],
                               "session_counts": st["sessions"], "stats_basis": st["basis"],
                               "sides": {s: {"state": r["sides"][s]["state"]} for s in SIDES}}
    ok = all(not r.get("failed") for r in results) and (dry_run or bool(slack_ok)) and md_ok and plan_ok is not False
    try:
        update_heartbeat({**hb_base, "ok": ok, "slack_ok": slack_ok, "md_ok": md_ok, "plan_ok": plan_ok,
                          "symbols": sym_hb, "violations": counts, "violations_today": vt,
                          "violations_today_date": today.isoformat(), "dry_run": dry_run,
                          "sends": {("watchlist" if send == "0646" else "premarket"): {"date": today.isoformat(), "slack_ok": slack_ok, "plan_ok": plan_ok,
                                           "ts_utc": iso_z(now)}}})
    except Exception as e:   # a heartbeat error after a good send must not report the send as failed
        logger.error("morning_rundown: heartbeat write failed (%s)", e)
    if any(r.get("failed") for r in results) and not dry_run:
        send_slack(f":warning: Morning rundown {send}: build failed for "
                   + ", ".join(r["symbol"] for r in results if r.get("failed")))
    if plan_ok is False and not dry_run:
        send_slack(":warning: Morning rundown 06:46: watchlist plan NOT written — no TRIGGERED alerts today")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    ap = argparse.ArgumentParser(description="MORNING-1029 morning rundown (report only; places no orders)")
    ap.add_argument("--send", choices=("0600", "0646"), required=True)
    ap.add_argument("--dry-run", action="store_true", help="build and archive, do not send to Slack (step 0)")
    args = ap.parse_args(argv)
    try:
        return run(args.send, dry_run=args.dry_run)
    except Exception as e:
        logger.error("morning_rundown: unexpected failure (%s)", e, exc_info=True)
        try:
            if not args.dry_run:
                send_slack(f":warning: Morning rundown {args.send} FAILED: {str(e)[:120]}")
            update_heartbeat({"last_run_utc": iso_z(utcnow()), "ok": False, "note": f"failed: {e}"})
        except Exception as e2:
            logger.error("morning_rundown: failure reporting failed (%s)", e2)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
