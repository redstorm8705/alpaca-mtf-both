# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/day_veto_sizing.py — DAY-VETO-SIZE-1109: Track B trend vetoes change SIZE, not count.

Design record: logs/design_records/day_veto_size_2026-11-09.md (revision 8). Scope: the Track B call of the Day tier's
direction rules only (run_day_tier.py). Track A is unchanged. Kill flag: config.DAYTRADE_VETO_DOWNSIZE_ENABLED (only an
explicit True enables the path; False restores today's blocking).

What lives here (pure helpers + small state files; the only broker reads are the R ledger's order reads):
  * the veto client-order-id marker (`-vd<hex>` suffix) — veto lots are found in Alpaca orders by it;
  * the replay prior (data/state/day_veto_prior.json, built by research/day_veto_prior.py) and the posterior: replay
    trades weighted 1 / variants_tried each (n_eff = n / variants), live closed veto trades weighted 1, combined per
    session cluster; p, b, f* = p - (1 - p) / b = p (1 - L / A), and its session-clustered standard error (delta
    method; a prior cluster's squared influence is scaled by 1 / its weight so the prior carries n_eff trades of
    information, never n);
  * m = clamp(c x f*_LCB / RISK_BASIS_PCT, m_floor, 1) with c = SWING_BREAKOUT_KELLY_FRACTION, and m_floor = the
    smallest m at which min_entry_qty survives _bounded_entry_qty's risk step;
  * the own R ledger (data/state/day_veto_ledger.json): each closed veto lot's R rebuilt from its Alpaca entry order
    (found by client order id) and exit orders (by order id; a journal exit row without an order id uses the row's
    fill price);
  * the same-direction cluster bound (exposure sign; an inverse-fund buy is short exposure);
  * the automatic per-direction pause (data/state/day_veto_pause.json, keyed by trading date): the posterior 90%
    session-clustered CI upper bound of mean R < 0 with live veto fills from at least two sessions -> paused for the
    rest of that trading day, one Slack page; re-checked only after a NEW closed veto trade, so it resets next day.
Missing / unreadable inputs never raise: sizing falls to m_floor; the pause is not evaluated (logged).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
_ROOT = Path(__file__).resolve().parent.parent
PRIOR_PATH = _ROOT / "data" / "state" / "day_veto_prior.json"
LEDGER_PATH = _ROOT / "data" / "state" / "day_veto_ledger.json"
PAUSE_PATH = _ROOT / "data" / "state" / "day_veto_pause.json"
CHANGE = "day-veto-size-1109"
TAG = "veto_downsized"
_MARK = "vd"                         # not a hex digit: a normal 8-hex uniq never starts with it
_MARK_RE = re.compile(r"-vd[0-9a-f]+$")

# Veto entries submitted in THIS process (the runner is one process per tick): the cluster bound counts them before
# their fill reaches the journal. [{"coid", "sign", "risk_usd"}]
_SUBMITTED: list = []


def enabled() -> bool:
    """Only an explicit True enables the veto path (a missing or mistyped flag keeps today's blocking)."""
    return getattr(config, "DAYTRADE_VETO_DOWNSIZE_ENABLED", False) is True


def veto_uniq() -> str:
    """The coid uniq part of a veto lot: the marker + a full uuid4 hex (unique per order)."""
    return _MARK + uuid.uuid4().hex


def is_veto_coid(coid: object) -> bool:
    return bool(_MARK_RE.search(str(coid or "")))


def _known_fund_symbols() -> set:
    """Every symbol the bot knows to be a leveraged / inverse fund: the Day tier's route maps (bull, alternate bull,
    inverse, alternate inverse, the config bull-map override) and config's leveraged ticker sets."""
    from strategy import day_tier_leverage as lev
    out = set(lev._DEFAULT_BULL_2X.values()) | set(lev._DEFAULT_BULL_2X_ALT.values())
    out |= {etf for etf, _k in list(lev._DEFAULT_INVERSE.values()) + list(lev._DEFAULT_INVERSE_ALT.values())}
    _ov = getattr(config, "DAYTRADE_LEVERAGED_BULL_MAP", None)
    if isinstance(_ov, dict):
        out |= {str(v).strip().upper() for v in _ov.values()}
    out |= {str(s).strip().upper() for s in (getattr(config, "LEVERAGED_TICKERS", set()) or set())}
    out |= {str(s).strip().upper() for s in (getattr(config, "LEVERAGED_3X_TICKERS", set()) or set())}
    return out


def exposure_sign(symbol: str, side: str) -> "int | None":
    """+1 long / -1 short exposure on the underlying (an inverse-fund buy is -1). AFFIRMATIVE only (cold review nit e):
    a fund whose direction the route maps do not state (an unmapped leveraged / inverse ticker) is None, never "long";
    the caller fails closed and pages once per symbol per day. None also when anything is unreadable."""
    try:
        from strategy import day_tier_leverage as lev
        sym = str(symbol).strip().upper()
        sgn = 1 if str(side or "").lower() == "long" else (-1 if str(side or "").lower() == "short" else 0)
        if sgn == 0 or not sym:
            return None
        if lev.fund_leverage(sym) is not None:            # mapped fund: the maps state its direction
            return int(lev.exposure_sign(sym, str(side).lower())[1])
        _ov = getattr(config, "DAYTRADE_LEVERAGED_BULL_MAP", None)
        if isinstance(_ov, dict) and sym in {str(v).strip().upper() for v in _ov.values()}:
            return sgn                                     # a configured BULL fund: same direction as the order
        if sym in _known_fund_symbols():
            page_once(f"sign:{sym}:{_today()}", f"DAY tier veto: {sym} is a fund with no mapped direction — veto "
                                                 f"setups on it are skipped (cluster direction unknown). [change={CHANGE}]")
            return None
        return sgn                                         # a stock
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] veto exposure sign unreadable: %s", symbol, e)
        return None


def _finite(*vals) -> bool:
    try:
        return all(math.isfinite(float(v)) for v in vals)
    except (TypeError, ValueError):
        return False


def _atomic_write(path: Path, obj) -> bool:
    try:
        os.makedirs(path.parent, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[change=%s] state write failed (%s): %s", CHANGE, path.name, e)
        return False


PAGES_PATH = _ROOT / "data" / "state" / "day_veto_pages.json"
_PAGED: set = set()   # in-process dedupe (also covers a failed pages-file write within one tick)


_SECONDARY_PREFIX = "_paged::day-veto::"   # key prefix inside the Day tier state file (the secondary dedupe store)


def _retention_days() -> int:
    """The Day tier's own state-retention window (day_trade_manager._STATE_TTL_DAYS, the value _prune_state uses)."""
    from execution import day_trade_manager as _dtm
    return int(_dtm._STATE_TTL_DAYS)


def _within_retention(stamp: object) -> bool:
    """True when an ISO timestamp is inside the retention window (an unreadable stamp is kept — fail toward dedupe)."""
    try:
        from datetime import timedelta
        t = datetime.fromisoformat(str(stamp))
        if t.tzinfo is None:
            return True
        return datetime.now(PT) - t <= timedelta(days=_retention_days())
    except (TypeError, ValueError):
        return True


def _day_within_retention(day: object) -> bool:
    """Same for a YYYYMMDD ET day (unreadable -> kept)."""
    try:
        from datetime import timedelta
        d = datetime.strptime(str(day), "%Y%m%d").date()
        return datetime.now(ET).date() - d <= timedelta(days=_retention_days())
    except (TypeError, ValueError):
        return True


_NO_SECONDARY: list = []   # non-empty while inside place_entry's call path (revision 5 item 3)


class no_secondary_store:
    """Context manager: page_once never writes logs/day_tier_state.json inside it (place_entry keeps its own in-memory
    copy of that file and would later overwrite the stamp); its dedupe falls back to the temp-dir marker only."""

    def __enter__(self):
        _NO_SECONDARY.append(True)
        return self

    def __exit__(self, *exc):
        if _NO_SECONDARY:
            _NO_SECONDARY.pop()
        return False


def _secondary_has(key: str) -> bool:
    try:
        from execution import day_trade_manager as _dtm
        return f"{_SECONDARY_PREFIX}{key}" in _dtm._load_state()
    except Exception:  # noqa: BLE001
        return False


def _secondary_mark(key: str) -> bool:
    """Stamp `key` in logs/day_tier_state.json (the Day tier's own atomic state file, the store _page_once_today uses)."""
    try:
        from execution import day_trade_manager as _dtm
        st = _dtm._load_state()
        for k in [k for k, v in st.items() if str(k).startswith(_SECONDARY_PREFIX) and not _within_retention(v)]:
            st.pop(k, None)   # nit c: prune stamps older than the Day tier's state-retention window
        st[f"{_SECONDARY_PREFIX}{key}"] = datetime.now(PT).isoformat()
        return bool(_dtm._save_state(st))
    except Exception as e:  # noqa: BLE001
        logger.error("[change=%s] secondary page dedupe write failed: %s", CHANGE, e)
        return False


def _tertiary_path() -> Path:
    """Last-resort per-day dedupe marker in the system temp dir (like the runner's /tmp flock file): used only when
    both bot state stores are unwritable."""
    import tempfile
    return Path(tempfile.gettempdir()) / f"day_veto_pages_{_today()}.json"


def _tertiary_has(key: str) -> bool:
    d = _read_json(_tertiary_path()) or {}
    return key in (d.get("sent") or {})


def _tertiary_mark(key: str) -> bool:
    d = _read_json(_tertiary_path()) or {}
    sent = dict(d.get("sent") or {})
    sent[key] = datetime.now(PT).isoformat()
    return _atomic_write(_tertiary_path(), {"sent": sent})


def page_once(key: str, msg: str) -> bool:
    """One Slack page per `key` (day-scoped keys carry their date). No page storm across the one-process-per-tick
    runner: the key is STAMPED before the page is sent — in data/state/day_veto_pages.json, else (outside place_entry's
    call path only, revision 5 item 3) in the Day tier state file logs/day_tier_state.json, else in a per-day marker in
    the system temp dir. A key found in any store is never re-sent. A page stamped only in a fallback store says so.
    When no store can be written the page is not sent (logged only), so it can never repeat every tick.
    Returns True when a page was sent now. Never raises."""
    try:
        if key in _PAGED:
            return False
        d = _read_json(PAGES_PATH) or {}
        # nit c: stamps older than the Day tier's state-retention window are dropped on every write
        sent = {k: v for k, v in (d.get("sent") or {}).items() if _within_retention(v)}
        if key in sent or _secondary_has(key) or _tertiary_has(key):
            _PAGED.add(key)
            return False
        _PAGED.add(key)
        logger.warning("[change=%s] %s", CHANGE, msg)
        sent[key] = datetime.now(PT).isoformat()
        if not _atomic_write(PAGES_PATH, {"sent": sent}):
            if not _NO_SECONDARY and _secondary_mark(key):
                msg = f"{msg} (page dedupe: data/state unwritable — stamped in logs/day_tier_state.json)"
            elif _tertiary_mark(key):
                msg = f"{msg} (page dedupe: data/state unwritable — stamped in the temp-dir marker)"
            else:
                logger.error("[change=%s] page dedupe stores unwritable — not paged (logged only): %s", CHANGE, msg)
                return False
        try:
            from alerts import send_slack
            send_slack(msg)
        except Exception as e:  # noqa: BLE001
            logger.error("[change=%s] page send failed: %s", CHANGE, e)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[change=%s] page_once error: %s", CHANGE, e)
        return False


def _read_json(path: Path) -> "dict | None":
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[change=%s] %s unreadable: %s", CHANGE, path.name, e)
    return None


# ── prior ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_prior(path: "Path | None" = None) -> "dict | None":
    """The replay prior, validated; None when missing or malformed (the caller sizes at m_floor)."""
    d = _read_json(path or PRIOR_PATH)
    if d is None:
        return None
    try:
        variants = float(d["variants_tried"])
        q = float(d["pause_upper_quantile"])
        sessions = d["sessions"]
        if not (_finite(variants, q) and variants > 0 and 0 < q < 1 and isinstance(sessions, list) and sessions):
            return None
        rows = []
        for s in sessions:
            n, w = int(s["n"]), int(s["wins"])
            sw, sl, sr = float(s["sum_win_r"]), float(s["sum_loss_r"]), float(s["sum_r"])
            if n < 1 or w < 0 or w > n or not _finite(sw, sl, sr) or sw < 0 or sl < 0:
                return None
            rows.append({"date": str(s.get("date") or ""), "n": n, "wins": w, "sum_win_r": sw, "sum_loss_r": sl,
                         "sum_r": sr})
        return {"variants_tried": variants, "pause_upper_quantile": q, "sessions": rows,
                "n_trades": sum(r["n"] for r in rows)}
    except (KeyError, TypeError, ValueError):
        return None


def _live_clusters(live: list) -> list:
    by: dict = {}
    for t in live:
        r = float(t["R"])
        g = by.setdefault(str(t["session"]), {"n": 0, "wins": 0, "sum_win_r": 0.0, "sum_loss_r": 0.0, "sum_r": 0.0})
        g["n"] += 1
        g["sum_r"] += r
        if r > 0:
            g["wins"] += 1
            g["sum_win_r"] += r
        else:
            g["sum_loss_r"] -= r
    return list(by.values())


def posterior(prior: "dict | None", live: "list | None") -> "dict | None":
    """Prior (weight 1/variants per replay trade) + live closed veto trades (weight 1). Returns p, b, f*, its
    session-clustered SE and f*_LCB, mean R with its SE and CI upper bound, and the counts; None when the prior or the
    live ledger is missing. f*-fields are None when undefined (no wins). Never raises."""
    try:
        if prior is None or live is None:
            return None
        w_prior = 1.0 / float(prior["variants_tried"])
        clusters = [(w_prior, c) for c in prior["sessions"]]
        live_c = _live_clusters([t for t in live if _finite(t.get("R"))])
        clusters += [(1.0, c) for c in live_c]
        big_w = sum(w * c["n"] for w, c in clusters)
        if not (big_w > 0):
            return None
        p = sum(w * c["wins"] for w, c in clusters) / big_w
        a = sum(w * c["sum_win_r"] for w, c in clusters) / big_w     # E[R; R > 0]
        lo = sum(w * c["sum_loss_r"] for w, c in clusters) / big_w   # E[|R|; R <= 0]
        mean_r = sum(w * c["sum_r"] for w, c in clusters) / big_w
        g = len(clusters)
        corr = g / (g - 1) if g > 1 else float("nan")
        var_m = corr * sum(w * (c["sum_r"] - c["n"] * mean_r) ** 2 for w, c in clusters) / big_w ** 2
        se_m = math.sqrt(var_m) if _finite(var_m) and var_m >= 0 else None
        z = statistics.NormalDist().inv_cdf(float(prior["pause_upper_quantile"]))
        out: dict = {"p": p, "b": None, "f_star": None, "se_f": None, "f_lcb": None, "mean_r": mean_r, "se_mean": se_m,
                     "upper": (mean_r + z * se_m) if se_m is not None else None,
                     "n_prior": int(prior["n_trades"]), "n_prior_eff": round(int(prior["n_trades"]) * w_prior, 4),
                     "n_live": sum(c["n"] for c in live_c), "n_live_sessions": len(live_c)}
        if a > 0:
            f = p * (1 - lo / a)
            if 0 < p < 1 and lo > 0:
                out["b"] = (a / p) / (lo / (1 - p))
            d_p, d_a, d_l = 1 - lo / a, p * lo / a ** 2, -p / a
            var_f = corr * sum(w * (d_p * (c["wins"] - c["n"] * p) + d_a * (c["sum_win_r"] - c["n"] * a)
                                    + d_l * (c["sum_loss_r"] - c["n"] * lo)) ** 2 for w, c in clusters) / big_w ** 2
            out["f_star"] = f
            if _finite(var_f) and var_f >= 0:
                out["se_f"] = math.sqrt(var_f)
                out["f_lcb"] = f - out["se_f"]
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("[change=%s] posterior error (m_floor sizing): %s", CHANGE, e)
        return None


def m_kelly(post: "dict | None") -> "float | None":
    """c x f*_LCB / RISK_BASIS_PCT (unclamped), or None when any input is missing (the caller then uses m_floor)."""
    try:
        if not post or post.get("f_lcb") is None:
            return None
        c = float(config.SWING_BREAKOUT_KELLY_FRACTION)
        basis = float(config.DAYTRADE_PER_TRADE_RISK_BASIS_PCT)
        if not (_finite(c, basis, post["f_lcb"]) and basis > 0 and c > 0):
            return None
        return c * float(post["f_lcb"]) / basis
    except (TypeError, ValueError, AttributeError):
        return None


# ── sizing pieces (pure) ─────────────────────────────────────────────────────────────────────────────────────────
def one_share_fit(min_q: int, per_share: float, slip_mult: float, cap_usd: float) -> "tuple[bool, int]":
    """(min_q x per_share x slip within the per-trade cap, how many shares fit it). Bad inputs -> (False, 0)."""
    if not (_finite(per_share, slip_mult, cap_usd) and per_share > 0 and slip_mult > 0 and cap_usd > 0):
        return False, 0
    fit = math.floor(cap_usd / (slip_mult * per_share))
    return int(min_q) * per_share * slip_mult <= cap_usd, max(0, int(fit))


def m_floor(min_q: int, per_share: float, risk_basis_usd: float, risk_pct: float) -> "float | None":
    """Smallest m at which _bounded_entry_qty's risk step (floor((risk_basis x (risk_pct x m)) / stop)) gives min_q.
    None on unusable input. May exceed 1 (the caller skips)."""
    if not (_finite(per_share, risk_basis_usd, risk_pct) and per_share > 0 and risk_basis_usd > 0 and risk_pct > 0
            and int(min_q) >= 1):
        return None
    m = int(min_q) * per_share / (risk_basis_usd * risk_pct)
    for _ in range(8):   # float rounding: step up one ulp at a time until the exact sizing expression clears min_q
        if math.floor((risk_basis_usd * (risk_pct * m)) / per_share) >= int(min_q):
            return m
        m = math.nextafter(m, math.inf)
    return None


def choose_m(mk: "float | None", mf: float) -> "tuple[float, str]":
    """clamp(mk, mf, 1); mk None -> mf."""
    if mk is None or not _finite(mk):
        return min(1.0, mf), "m_floor (posterior unavailable)"
    m = min(1.0, max(mf, float(mk)))
    return m, ("m_floor" if m == mf else ("kelly" if m < 1.0 else "cap 1"))


def stops_by_trade(events: list) -> dict:
    """Latest stop_placed price per trade_id."""
    out: dict = {}
    for e in events or []:
        if isinstance(e, dict) and e.get("event") == "stop_placed" and e.get("trade_id"):
            out[str(e["trade_id"])] = e.get("stop_price")
    return out


def state_stops(state: "dict | None") -> dict:
    """{coid: stop_px} from the Day tier's state records (the entry's stop; Day stops never trail)."""
    out: dict = {}
    for k, v in (state or {}).items():
        if str(k).startswith("entry::") and isinstance(v, dict) and v.get("coid") and v.get("stop_px") is not None:
            out[str(v["coid"])] = v.get("stop_px")
    return out


_OPEN_STATES = ("submitting", "submitted", "filled", "protected", "fill_unverified", "promote_pending")


def _journal_ids(journal: "list | None") -> set:
    out: set = set()
    for e in journal or []:
        if (isinstance(e, dict) and e.get("event") in ("entry_fill", "exit_fill", "partial_exit_fill")
                and e.get("trade_id")):
            out.add(str(e["trade_id"]))
    return out


def _state_veto_records(state: "dict | None", journal: "list | None") -> list:
    """(coid, record) of every veto-marked Day state record in an open order state whose coid has no entry_fill or exit
    in the WHOLE journal (a journaled lot belongs to open_trades_from_log, which is authoritative for it)."""
    known = _journal_ids(journal)
    out = []
    for k, v in (state or {}).items():
        if not (str(k).startswith("entry::") and isinstance(v, dict) and is_veto_coid(v.get("coid"))):
            continue
        coid = str(v.get("coid"))
        if v.get("state") in _OPEN_STATES and coid not in known:
            out.append((coid, v))
    return out


def _has_fill(v: dict) -> bool:
    return _px(v.get("fill_px")) > 0


def _rec_day(v: dict) -> str:
    return str(v.get("bar_id") or "").split("-", 1)[0]


def prior_day_unfilled(state: "dict | None", journal: "list | None", day: "str | None" = None) -> list:
    """Coids of prior-day veto state records with no fill price on record (e.g. a 'submitting' record left by a hard
    kill) — the ones refresh resolves against the broker by client order id."""
    day = day or _today()
    return [c for c, v in _state_veto_records(state, journal) if not _has_fill(v) and _rec_day(v) != day]


def resolve_state_lot(broker, coid: str) -> "dict | None":
    """One broker read: {"status": "filled", "fill_px", "fill_qty"} for a fill, {"status": "none"} for an order that is
    confirmed done with nothing filled (canceled / rejected / expired), None when unknown (not found or unreadable —
    retried on a later tick)."""
    try:
        o = broker.get_order_by_client_order_id(coid)
    except Exception as e:  # noqa: BLE001
        logger.warning("[change=%s] state lot %s broker read failed: %s", CHANGE, coid, e)
        return None
    if o is None:
        return None
    fq, fp = _px(_o(o, "filled_qty")), _px(_o(o, "filled_avg_price"))
    if fq > 0 and fp > 0:
        return {"status": "filled", "fill_px": fp, "fill_qty": fq}
    st = str(_o(o, "status") or "").lower()
    if st in ("canceled", "cancelled", "rejected", "expired", "done_for_day"):
        return {"status": "none"}
    return None


def state_open_veto_lots(state: "dict | None", journal: "list | None", resolved: "dict | None" = None,
                         day: "str | None" = None) -> list:
    """Every veto-marked Day state record that may still hold shares (revision 3 item 2; revision 4 threat 1):
      * a record WITH a fill price on record: counted on any day;
      * a record WITHOUT one (e.g. 'submitting' left by a hard kill): counted only on its own trading day (as
        _daily_risk_used / _state_blocks do); from a prior day it is resolved against the broker by client order id
        (refresh, under the tick's read budget; `resolved` = the cached results): filled -> counted with its broker
        fill; confirmed nothing filled -> ignored; unresolved -> ignored, paged once per lot.
    A record whose entry_fill reached the journal is left to open_trades_from_log."""
    day = day or _today()
    if resolved is None:
        resolved = dict((_read_json(LEDGER_PATH) or {}).get("state_resolved") or {})
    out = []
    for coid, v in _state_veto_records(state, journal):
        rec = {"coid": coid, "symbol": v.get("symbol"), "side": v.get("side"),
               "fill_qty": v.get("fill_qty") or v.get("qty"), "entry_price": v.get("fill_px")}
        if _has_fill(v) or _rec_day(v) == day:
            out.append(rec)
            continue
        r = resolved.get(coid)
        if isinstance(r, dict) and r.get("status") == "filled":
            out.append({**rec, "entry_price": r.get("fill_px"), "fill_qty": r.get("fill_qty")})
        elif not (isinstance(r, dict) and r.get("status") == "none"):
            page_once(f"state_unresolved:{coid}:{day}",f"DAY tier veto: prior-day veto state record {coid} ({v.get('symbol')}, "
                                                  f"state {v.get('state')}) has no fill on record and the broker has not "
                                                  f"confirmed it — ignored by the veto cluster. Manual check. "
                                                  f"[change={CHANGE}]")
    return out


def current_stop(tid: str, today_stops: dict, st_stops: dict, journal_lookup=None) -> "float | None":
    """A veto lot's current stop from the widest scope that has it (cold review threat 2: an overnight lot's stop row
    is from an earlier day): today's journal stop rows, then the WHOLE journal for this trade (latest stop_placed),
    then the state record's stop_px. None only when no source has a usable stop."""
    cands = [today_stops.get(tid)]
    if cands[0] is None and journal_lookup is not None:
        try:
            cands.append(stops_by_trade(journal_lookup(tid)).get(tid))
        except Exception as e:  # noqa: BLE001
            logger.warning("[change=%s] whole-journal stop read for %s failed: %s", CHANGE, tid, e)
    cands.append(st_stops.get(tid))
    for c in cands:
        try:
            v = float(c)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if _finite(v) and v > 0:
            return v
    return None


def cluster_risk(open_trades: dict, today_stops: dict, sign: int, *, st_stops: "dict | None" = None,
                 state_only: "list | None" = None, journal_lookup=None, unknown_lot_usd: float = 0.0,
                 broker_positions: "dict | None" = None) -> dict:
    """Same-direction veto cluster: open veto lots (journal), state-only veto lots (filled in an earlier tick whose
    entry_fill write failed — cold review threat 3) and this process's submitted veto entries. Each lot's
    loss-if-stopped from entry at its current stop (current_stop: today's journal, the whole journal, the state
    record), UNSCALED. A state-only lot with no fill price is charged `unknown_lot_usd` (the caller passes one full
    per-trade cap). Revision 5: every journal / state lot is confirmed against `broker_positions` (the live book,
    {symbol: position}); unconfirmed lots are dropped and paged once each, and the lots of one symbol are capped at the
    broker quantity. `broker_positions` None (book unreadable) -> ok False (new veto setups skipped). Returns {"ok",
    "n", "risk", "why"}; ok False (fail closed, paged once per lot per day) also when a lot's direction or stop
    genuinely cannot be found anywhere."""
    n, risk = 0, 0.0
    seen: set = set()
    sts = st_stops or {}
    cands: list = []
    # Revision 5 item 1 — the BROKER is the truth for which lots exist (design 2g: "open veto lots from the broker").
    # Revision 6 item a: each lot is confirmed against the book FIRST, so a lot the broker no longer holds is dropped
    # (paged once as stale) and never fails the cluster closed for missing side / sign / stop / entry data; only a lot
    # the broker DOES hold can fail it closed.
    if broker_positions is None:
        return {"ok": False, "n": n, "risk": risk, "why": "broker position book unreadable this tick"}
    lots = [(str(tid), t, "journal") for tid, t in (open_trades or {}).items() if is_veto_coid(tid)]
    lots += [(str(s.get("coid") or ""), s, "state") for s in (state_only or []) if is_veto_coid(s.get("coid"))]
    for tid, t, src in lots:
        if tid in seen:
            continue
        seen.add(tid)
        sym = str(t.get("symbol") or "").strip().upper()
        held0 = _broker_held(sym, broker_positions)   # (sign, qty) the broker holds in sym, or None
        # revision 3 item 1: a missing / unknown side is UNKNOWN, never "long"
        side = str(t.get("side") or "").lower()
        sgn = exposure_sign(sym, side) if side in ("long", "short") else None
        if sgn is None:
            if held0 is None:
                _page_stale(tid, sym, None, held0)    # nothing held: a stale record, not a reason to block
                continue
            why = "side unknown" if side not in ("long", "short") else "exposure direction unknown"
            page_once(f"cluster_side:{tid}:{_today()}", f"DAY tier veto: open veto lot {tid} ({sym}, held at the broker) "
                                                        f"has {why} — new veto setups skipped until it is resolved. "
                                                        f"[change={CHANGE}]")
            return {"ok": False, "n": n, "risk": risk, "why": f"veto lot {tid}: {why}"}
        if sgn != sign:
            continue
        if held0 is None or held0[0] != sgn or held0[1] <= 0:
            _page_stale(tid, sym, sgn, held0)
            continue
        stop = current_stop(tid, today_stops, sts, journal_lookup)
        try:
            qty = abs(float(t.get("fill_qty") or t.get("qty")))  # type: ignore[arg-type]  # None caught below
            entry = float(t.get("entry_price")) if t.get("entry_price") is not None else float("nan")
        except (TypeError, ValueError):
            qty, entry = float("nan"), float("nan")
        age = (t.get("entry_ts"), None) if src == "journal" else (None, str(t.get("bar_id") or "").split("-", 1)[0])
        if src == "state" and not (_finite(entry) and entry > 0) and _finite(qty) and qty > 0:
            cands.append({"tid": tid, "symbol": sym, "sign": sgn, "qty": qty, "per_share": None, "age": age})
            continue
        if stop is None or not (_finite(entry, qty) and entry > 0 and qty > 0):
            page_once(f"cluster_stop:{tid}:{_today()}", f"DAY tier veto: open veto lot {tid} ({sym}, held at the broker) "
                                                        f"has no readable stop/entry in the journal or state — new "
                                                        f"same-direction veto setups skipped. [change={CHANGE}]")
            return {"ok": False, "n": n, "risk": risk, "why": f"veto lot {tid}: stop/entry not found"}
        cands.append({"tid": tid, "symbol": sym, "sign": sgn, "qty": qty, "age": age,
                      "per_share": max(0.0, (entry - stop) if side == "long" else (stop - entry))})
    # The lots of one symbol are capped together at the broker quantity.
    left: dict = {}
    for c in cands:
        if c["symbol"] not in left:
            left[c["symbol"]] = _broker_held(c["symbol"], broker_positions)
        held = left[c["symbol"]]
        if held is None or held[0] != c["sign"] or held[1] <= 0:
            _page_stale(c["tid"], c["symbol"], c["sign"], _broker_held(c["symbol"], broker_positions),
                        exhausted=True)
            continue
        q = min(c["qty"], held[1])
        left[c["symbol"]] = (held[0], held[1] - q)
        n += 1
        # Revision 6 item b (the simpler rule): a counted veto lot older than the Day tier's state-retention window
        # (_STATE_TTL_DAYS) is paged once per lot per day — a Day lot that old is likely held only because another
        # tier's same-sign shares cover it on the broker book.
        _ts, _day = c["age"]
        if (_ts is not None and not _within_retention(_ts)) or (_day and not _day_within_retention(_day)):
            page_once(f"old_veto:{c['tid']}:{_today()}", f"DAY tier veto lot {c['tid']} ({c['symbol']}) is older than the "
                                                         f"Day tier's {_retention_days()}-day retention window and is "
                                                         f"still counted in the veto cluster (the broker holds "
                                                         f"{c['symbol']} with the same exposure — possibly another "
                                                         f"tier's shares). Manual check. [change={CHANGE}]")
        if c["per_share"] is None:
            risk += float(unknown_lot_usd)   # no fill price on record: charged one full per-trade cap
            page_once(f"state_charged:{c['tid']}", f"DAY tier veto: state-only veto lot {c['tid']} ({c['symbol']}) has "
                                                   f"no fill price on record — charged one full per-trade cap in the "
                                                   f"same-direction cluster. [change={CHANGE}]")
        else:
            risk += c["per_share"] * q
    for s in _SUBMITTED:
        if s.get("sign") == sign and str(s.get("coid")) not in seen:
            n += 1
            risk += float(s.get("risk_usd") or 0.0)
    return {"ok": True, "n": n, "risk": risk, "why": ""}


def _page_stale(tid: str, sym: str, lot_sign: "int | None", held: "tuple[int | None, float] | None",
                exhausted: bool = False) -> None:
    """Revision 6 item c: one page per stale lot naming the lot's exposure and what the broker actually holds."""
    want = {1: "long", -1: "short"}.get(lot_sign or 0, "unknown")
    has = ("nothing" if held is None else
           f"{ {1: 'long', -1: 'short'}.get(held[0] or 0, 'unknown-direction')} exposure, {held[1]:g} sh")
    extra = " (that quantity is already attributed to other veto lots)" if exhausted else ""
    page_once(f"stale_veto:{tid}", f"DAY tier veto lot {tid}: the broker holds no position with {want} exposure in "
                                   f"{sym} for it (broker holds in {sym}: {has}{extra}) — stale record, left out of the "
                                   f"veto cluster. Manual check. [change={CHANGE}]")


def _broker_held(symbol: str, positions: dict) -> "tuple[int | None, float] | None":
    """(exposure sign, |qty|) of the live broker position in `symbol`, or None when the book holds none / it is
    unreadable. The side comes from the position's side field, else the sign of its qty. The sign is None for a held
    position whose exposure direction is not affirmatively known (an unmapped fund)."""
    pos = (positions or {}).get(symbol)
    if pos is None:
        return None
    try:
        q = float(getattr(pos, "qty", 0) or 0)
    except (TypeError, ValueError):
        return None
    raw = getattr(pos, "side", None)
    s = str(getattr(raw, "value", raw) or "").lower()
    side = "short" if "short" in s else ("long" if "long" in s else ("short" if q < 0 else "long"))
    if not _finite(q) or q == 0:
        return None
    # a held position whose exposure direction is unknown (an unmapped fund) is (None, qty): held, sign unknown
    return exposure_sign(symbol, side), abs(q)


def cluster_qty_cap(cap_usd: float, slip_mult: float, existing_usd: float, per_share: float) -> int:
    """Shares a new veto lot may add so slip x (existing + new) stays within the cap; 0 when no room."""
    if not (_finite(cap_usd, slip_mult, existing_usd, per_share) and per_share > 0 and slip_mult > 0):
        return 0
    room = cap_usd - slip_mult * existing_usd
    return max(0, math.floor(room / (slip_mult * per_share))) if room > 0 else 0


def record_submitted(coid: str, sign: int, risk_usd: float) -> None:
    _SUBMITTED.append({"coid": str(coid), "sign": sign, "risk_usd": float(risk_usd)})


# ── own R ledger (Alpaca orders) ─────────────────────────────────────────────────────────────────────────────────
def _o(order, name):
    v = getattr(order, name, None)
    return getattr(v, "value", v)


def _closed_veto_groups(events: list) -> dict:
    by: dict = {}
    for e in events or []:
        if not isinstance(e, dict):
            continue
        tid = str(e.get("trade_id") or "")
        if not is_veto_coid(tid):
            continue
        g = by.setdefault(tid, {"entry": None, "stop": None, "exits": [], "full": False})
        k = e.get("event")
        if k == "entry_fill" and g["entry"] is None:
            g["entry"] = e
        elif k == "stop_placed" and g["stop"] is None:
            g["stop"] = e
        elif k in ("exit_fill", "partial_exit_fill"):
            g["exits"].append(e)
            g["full"] = g["full"] or k == "exit_fill"
    out = {}
    for tid, g in by.items():
        if not (g["entry"] and g["exits"]):
            continue
        try:
            xq = sum(abs(float(x.get("fill_qty") or 0.0)) for x in g["exits"])
            if g["full"] or xq >= abs(float(g["entry"].get("fill_qty") or 0.0)):
                out[tid] = g
        except (TypeError, ValueError):
            out[tid] = g
    return out


def _px(v: object) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
        return f if math.isfinite(f) and f > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _rebuild_r(tid: str, g: dict, broker, st_stops: dict, use_alpaca: bool, calls: dict) -> "tuple[dict | None, str, bool]":
    """One closed veto lot's R from the best evidence (cold review threat 1): Alpaca orders first (entry by client
    order id, exits by order id), each missing piece from the journal (fill prices, side, ts), the stop from the first
    journal stop row, else the state record's stop_px. Returns (row | None, why, alpaca_failed). `use_alpaca` False
    (a lookup already failed today) skips the broker. Every broker read is counted in calls["api"]."""
    entry_ev = g["entry"] or {}
    alpaca_failed = False
    eo = None
    if use_alpaca:
        calls["api"] = calls.get("api", 0) + 1
        try:
            eo = broker.get_order_by_client_order_id(tid)
        except Exception as e:  # noqa: BLE001
            logger.warning("[change=%s] veto ledger: entry order read for %s failed: %s", CHANGE, tid, e)
            eo = None
        alpaca_failed = eo is None or _px(_o(eo, "filled_avg_price")) == 0.0
    src_alpaca = use_alpaca and not alpaca_failed
    entry_px = _px(_o(eo, "filled_avg_price")) if eo is not None else 0.0
    if entry_px == 0.0:
        entry_px = _px(entry_ev.get("fill_price"))
    side_raw = str(_o(eo, "side") or "").lower() if eo is not None else ""
    side = {"buy": "long", "sell": "short"}.get(side_raw) or str(entry_ev.get("side") or "").lower()
    if side not in ("long", "short"):
        return None, "entry side unknown", alpaca_failed
    if entry_px == 0.0:
        return None, "no entry fill price (Alpaca or journal)", alpaca_failed
    stop = _px((g["stop"] or {}).get("stop_price")) or _px(st_stops.get(tid))
    dist = abs(entry_px - stop)
    if not (stop > 0 and dist > 0):
        return None, "no protective stop on record (journal or state)", alpaca_failed
    num, den = 0.0, 0.0
    for x in g["exits"]:
        q = abs(_px(x.get("fill_qty")))
        oid = str(x.get("order_id") or "")
        px = 0.0
        if oid and use_alpaca and not alpaca_failed:
            calls["api"] = calls.get("api", 0) + 1
            try:
                xo = broker.get_order(oid)
            except Exception as e:  # noqa: BLE001
                logger.warning("[change=%s] veto ledger: exit order %s read failed: %s", CHANGE, oid, e)
                xo = None
            px = _px(_o(xo, "filled_avg_price")) if xo is not None else 0.0
            if px == 0.0:
                alpaca_failed = True
                src_alpaca = False
        if px == 0.0:
            px = _px(x.get("fill_price"))
            if oid:
                src_alpaca = False
        if not (px > 0 and q > 0):
            return None, "an exit fill has no usable price or quantity", alpaca_failed
        num += px * q
        den += q
    if not den > 0:
        return None, "no exit quantity", alpaca_failed
    exit_px = num / den
    r = (1 if side == "long" else -1) * (exit_px - entry_px) / dist
    filled_at = _o(eo, "filled_at") if eo is not None else None
    try:
        ts = filled_at if isinstance(filled_at, datetime) else datetime.fromisoformat(
            str(filled_at or entry_ev.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            # revision 5 item 4: a naive timestamp is read as ET explicitly (the project's naive-time convention; the
            # journal itself writes zone-aware PT timestamps) — never as the host machine's local zone
            ts = ts.replace(tzinfo=ET)
        session = ts.astimezone(ET).date().isoformat()
    except (TypeError, ValueError):
        return None, "entry time unreadable", alpaca_failed
    sym = str(entry_ev.get("symbol") or (_o(eo, "symbol") if eo is not None else "") or "")
    sign = exposure_sign(sym, side)
    if sign is None or not _finite(r):
        return None, "exposure direction unknown", alpaca_failed
    return ({"R": round(r, 6), "session": session, "sign": sign, "symbol": sym, "entry": entry_px, "exit": exit_px,
             "stop": stop, "source": "alpaca" if src_alpaca else "journal_fallback"}, "", alpaca_failed)


def live_trades(events: "list | None", broker=None, st_stops: "dict | None" = None,
                day: "str | None" = None, max_reads: "int | None" = None) -> "tuple[list | None, list, dict]":
    """(every closed veto lot's ledger row, the trade_ids newly added this call, info). info = {"excluded": {tid:
    reason}, "api_calls": n}. One lot that cannot be rebuilt NEVER disables the ledger (cold review threat 1): its R
    comes from the best evidence (_rebuild_r); a lot whose R truly cannot be computed is EXCLUDED, counted in the
    logged "excluded veto lots" figure and paged once per lot. A failed Alpaca lookup is not retried until the next
    day (its journal-evidence row stands meanwhile). (None, [], info) only when the journal itself is missing."""
    info: dict = {"excluded": {}, "api_calls": 0}
    if events is None:
        return None, [], info
    day = day or _today()
    try:
        if broker is None:
            from execution import broker as _b
            broker = _b
        cache = _read_json(LEDGER_PATH) or {}
        rows = dict(cache.get("trades") or {})
        excluded = dict(cache.get("excluded") or {})
        failed = {k: v for k, v in (cache.get("alpaca_fail") or {}).items() if v == day}
        new: list = []
        calls: dict = {"api": 0}
        changed = False
        deferred = 0
        deferred_days = dict(cache.get("deferred_days") or {})
        for tid, g in _closed_veto_groups(events).items():
            have = rows.get(tid)
            if isinstance(have, dict) and have.get("source") == "alpaca":
                continue
            if (have is not None or tid in excluded) and failed.get(tid) == day:
                continue                           # looked up today already: no retry until tomorrow
            if tid in excluded and excluded[tid].get("day") == day:
                continue
            use_alpaca = failed.get(tid) != day
            if use_alpaca and max_reads is not None:
                # revision 3 item 4: broker reads capped per tick; a lot not reached keeps its cached / journal value
                # and is read on a later tick (never marked failed or excluded for lack of budget)
                need = 1 + sum(1 for x in g["exits"] if x.get("order_id"))
                if calls["api"] + need > max_reads:
                    deferred += 1
                    if have is not None:
                        continue
                    jrow, _jwhy, _ = _rebuild_r(tid, g, broker, st_stops or {}, False, calls)
                    if jrow is not None:
                        rows[tid] = {**jrow, "added": day}
                        new.append(tid)
                        changed = True
                        continue
                    # revision 4 nit a: a deferred lot whose journal R cannot be computed is excluded (paged once)
                    # once it has been deferred on two distinct trading days — never skipped silently for ever
                    ddays = sorted(set(list(deferred_days.get(tid) or []) + [day]))
                    deferred_days[tid] = ddays
                    changed = True
                    if len(ddays) > 1:
                        excluded[tid] = {"reason": f"deferred on {len(ddays)} trading days and no journal R ({_jwhy})",
                                         "day": day}
                        page_once(f"excluded:{tid}", f"DAY tier veto lot {tid} EXCLUDED from the veto R ledger (deferred "
                                                     f"on {len(ddays)} trading days; {_jwhy}). Manual check. "
                                                     f"[change={CHANGE}]")
                    continue
            row, why, a_fail = _rebuild_r(tid, g, broker, st_stops or {}, use_alpaca, calls)
            if a_fail:
                failed[tid] = day
            changed = True
            if row is not None:
                if have is None:
                    new.append(tid)
                if have is None or have.get("source") != row.get("source") or have.get("R") != row.get("R"):
                    row = {**row, "added": day}   # nit a: a changed row (e.g. journal -> Alpaca) is re-evaluated
                else:
                    row = {**row, "added": have.get("added", day)}
                rows[tid] = row
                excluded.pop(tid, None)
                deferred_days.pop(tid, None)
                continue
            excluded[tid] = {"reason": why, "day": day}
            page_once(f"excluded:{tid}", f"DAY tier veto lot {tid} EXCLUDED from the veto R ledger ({why}) — its "
                                         f"result could not be computed from Alpaca, the journal or the state record. "
                                         f"Manual check. [change={CHANGE}]")
        if changed:
            _atomic_write(LEDGER_PATH, {**cache, "trades": rows, "excluded": excluded, "alpaca_fail": failed,
                                        "deferred_days": deferred_days, "updated": datetime.now(PT).isoformat()})
        info = {"excluded": {k: v.get("reason") for k, v in excluded.items()}, "api_calls": calls["api"],
                "deferred": deferred}
        if deferred:
            logger.info("[change=%s] veto ledger: %d lot(s) deferred to a later tick (broker read budget %s used %d)",
                        CHANGE, deferred, max_reads, calls["api"])
        if excluded:
            logger.warning("[change=%s] veto ledger: %d excluded veto lot(s) (R not computable): %s", CHANGE,
                           len(excluded), ", ".join(sorted(excluded)))
        return [{**v, "trade_id": k} for k, v in rows.items()], new, info
    except Exception as e:  # noqa: BLE001
        logger.warning("[change=%s] veto ledger error (m_floor this tick): %s", CHANGE, e)
        return None, [], info


# ── automatic pause per direction ────────────────────────────────────────────────────────────────────────────────
def _today() -> str:
    return datetime.now(ET).strftime("%Y%m%d")


def pause_info(sign: int, day: "str | None" = None) -> "dict | None":
    """Today's pause record for this exposure sign, else None (a record from another day never applies). Revision 3
    nit: a pause file that EXISTS but cannot be read fails closed — treated as paused for this tick ("unreadable": True)
    and paged once per day; a missing file means no pause has ever been written."""
    raw = _read_json(PAUSE_PATH)
    if raw is None and PAUSE_PATH.exists():
        page_once(f"pause_unreadable:{day or _today()}", "DAY tier veto pause file is unreadable — veto setups are "
                                                         f"treated as paused until it is fixed. [change={CHANGE}]")
        return {"unreadable": True, "n": None, "mean": None, "upper": None}
    d = raw or {}
    rec = ((d.get("pauses") or {}).get(day or _today()) or {}).get(str(sign))
    return rec if isinstance(rec, dict) else None


def evaluate_pause(prior: "dict | None", trades: "list | None", new_ids: list, day: "str | None" = None) -> dict:
    """After NEW closed veto trades: per exposure sign among them, pause today when the posterior CI upper bound of
    mean R < 0 and live veto fills span at least two sessions. One page per (day, sign). Returns {sign: record}."""
    day = day or _today()
    out: dict = {}
    if prior is None or trades is None or not new_ids:
        return out
    st_raw = _read_json(PAUSE_PATH)
    if st_raw is None and PAUSE_PATH.exists():
        return out   # unreadable: never overwrite it (its evaluated list); pause_info fails closed meanwhile
    st = st_raw or {}
    # Revision 4 nit a + c: evaluated = {trade_id: R source}. A trade is (re-)evaluated when it is new or its source
    # changed (journal_fallback -> alpaca). Only ledger rows added / changed within the Day tier's state-retention window
    # can be new, so the evaluated map is pruned to that window without ever re-evaluating an old trade.
    _ev = st.get("evaluated") or {}
    evaluated = dict(_ev) if isinstance(_ev, dict) else {str(t): None for t in _ev}
    by_id = {t.get("trade_id"): t for t in trades}
    recent = {i for i in new_ids if i in by_id and _day_within_retention(by_id[i].get("added"))}
    todo = [i for i in recent if i not in evaluated or evaluated.get(i) != by_id[i].get("source")]
    if not todo:
        return out
    signs = {t.get("sign") for t in trades if t.get("trade_id") in todo}
    pauses = {k: v for k, v in (st.get("pauses") or {}).items() if k == day}
    today_p = dict(pauses.get(day) or {})
    for sign in sorted(s for s in signs if s is not None):
        post = posterior(prior, [t for t in trades if t.get("sign") == sign])
        if not post or post.get("upper") is None:
            continue
        if post["upper"] < 0 and post["n_live_sessions"] > 1 and str(sign) not in today_p:
            rec = {"n": post["n_live"], "mean": round(post["mean_r"], 4), "upper": round(post["upper"], 4),
                   "sessions": post["n_live_sessions"], "ts": datetime.now(PT).isoformat()}
            today_p[str(sign)] = rec
            out[sign] = rec
    st["pauses"] = {**pauses, day: today_p}
    for i in todo:
        evaluated[i] = by_id[i].get("source")
    st["evaluated"] = {i: s for i, s in evaluated.items() if i in recent}   # pruned to the retention window
    if not _atomic_write(PAUSE_PATH, st):
        # not persisted -> not in force; the trades stay un-evaluated and are re-checked next tick (cold review nit d)
        page_once(f"pause_write_fail:{day}", f"DAY tier veto pause file could not be written — the automatic pause "
                                             f"{'(which would have triggered now) ' if out else ''}is NOT in force until "
                                             f"the write succeeds. [change={CHANGE}]")
        return {}
    for sign, rec in out.items():
        msg = (f"DAY tier veto path PAUSED for the rest of today ({'long' if sign > 0 else 'short'} exposure): live mean R "
               f"CI upper < 0 (n={rec['n']}, mean={rec['mean']:+.3f}R, upper={rec['upper']:+.3f}R, "
               f"{rec['sessions']} sessions). Resets at the next trading day. [change={CHANGE}]")
        logger.warning("[change=%s] %s", CHANGE, msg)
        try:
            from alerts import send_slack
            send_slack(msg)
        except Exception as e:  # noqa: BLE001
            logger.error("[change=%s] pause page failed: %s", CHANGE, e)
    return out


def _resolve_state_records(state: dict, journal: "list | None", broker, max_reads: int) -> int:
    """Revision 4 threat 1: resolve prior-day veto state records with no fill on record against the broker by client
    order id, at most `max_reads` reads (the rest wait for a later tick). Results are cached in the ledger file under
    "state_resolved" (kept only for coids still in the state file). Returns the broker reads used. Never raises."""
    try:
        todo = prior_day_unfilled(state, journal)
        cache = _read_json(LEDGER_PATH) or {}
        live = {str(v.get("coid")) for k, v in (state or {}).items() if str(k).startswith("entry::")
                and isinstance(v, dict) and v.get("coid")}
        resolved = {c: r for c, r in (cache.get("state_resolved") or {}).items() if c in live}
        used = 0
        if broker is None and todo:
            from execution import broker as _b
            broker = _b
        for coid in todo:
            prev = resolved.get(coid) or {}
            if prev.get("status") in ("filled", "none") or (prev.get("status") == "unknown"
                                                            and prev.get("day") == _today()):
                continue   # settled, or not found / unreadable today already: no retry until the next day
            if used + 1 > max_reads:
                logger.info("[change=%s] state lot %s: broker check deferred (read budget)", CHANGE, coid)
                continue
            used += 1
            r = resolve_state_lot(broker, coid)
            resolved[coid] = {**(r or {"status": "unknown"}), "day": _today()}
        if resolved != (cache.get("state_resolved") or {}):
            _atomic_write(LEDGER_PATH, {**cache, "state_resolved": resolved})
        return used
    except Exception as e:  # noqa: BLE001
        logger.warning("[change=%s] state lot resolution error: %s", CHANGE, e)
        return 0


_TICK: dict = {}


def refresh(broker=None, max_reads: int = 0) -> dict:
    """Once per runner tick: journal -> R ledger (Alpaca orders for newly closed veto lots) -> pause check.
    `max_reads` caps this tick's broker order reads (the runner passes a share of the tick's remaining API budget; the
    default 0 = journal evidence only, so a caller that did not size the budget never spends it).
    Returns {"prior", "trades", "new", "paused", "excluded", "api_calls", "deferred"}; cached for the process (one
    tick). Never raises."""
    if _TICK:
        return _TICK
    prior = load_prior()
    trades: "list | None" = None
    new: list = []
    info: dict = {"excluded": {}, "api_calls": 0}
    try:
        from strategy import day_tier_logger
        # Tolerant read (cold review threat 1): read_events_checked returns every parseable line; a torn line is only
        # skipped (paged once a day), never allowed to disable the whole ledger and pause.
        events, ok = day_tier_logger.read_events_checked()
        if not ok:
            page_once(f"journal_torn:{_today()}", "DAY tier journal has a torn/malformed line — the veto R ledger skips "
                                                  f"that line only. Manual check. [change={CHANGE}]")
        st_stops: dict = {}
        dstate: dict = {}
        try:
            from execution import day_trade_manager as _dtm
            dstate = _dtm._load_state()
            st_stops = state_stops(dstate)
        except Exception as e:  # noqa: BLE001
            logger.warning("[change=%s] veto refresh: state stops unreadable: %s", CHANGE, e)
        cap_reads = max(0, int(max_reads))
        trades, new, info = live_trades(events, broker, st_stops, max_reads=cap_reads)
        info["api_calls"] = int(info.get("api_calls") or 0) + _resolve_state_records(
            dstate, events, broker, cap_reads - int(info.get("api_calls") or 0))
    except Exception as e:  # noqa: BLE001
        logger.warning("[change=%s] veto refresh: journal unreadable: %s", CHANGE, e)
    paused: dict = {}
    try:
        # every ledger trade not yet evaluated (the pause file records evaluated ids, so a failed write re-checks)
        paused = evaluate_pause(prior, trades, [t.get("trade_id") for t in (trades or [])])
    except Exception as e:  # noqa: BLE001
        logger.warning("[change=%s] veto pause evaluation error: %s", CHANGE, e)
    if prior is None:
        logger.warning("[change=%s] veto prior missing/unreadable — veto setups size at m_floor", CHANGE)
    _TICK.update(prior=prior, trades=trades, new=new, paused=paused, excluded=info.get("excluded") or {},
                 api_calls=int(info.get("api_calls") or 0), deferred=int(info.get("deferred") or 0))
    return _TICK
