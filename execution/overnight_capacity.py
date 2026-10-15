# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/overnight_capacity.py — the DYNAMIC overnight cap K (Architecture Invariant #11, total limb). Claude 2026-10-14.

Total overnight notional across ALL tiers must stay <= K x equity. K used to be the fixed board ceiling
`config.SWING_BREAKOUT_TOTAL_OVERNIGHT_K` (1.9). It is now derived from data and can only be TIGHTER than that ceiling
(design: logs/design_records/dynamic_capacity_2026-10-14.md, variant D, board Thorp + Taleb seats + Gro + GAI 4/4):

    K_raw = 1 / (m + (1 - m) x d_book)        margin call iff 1 - d x K < m x K x (1 - d)
    d_book = max(stress_hist x vix_scale, concentration) / book_leverage
    stress_hist = worst-ever 5-session loss of TODAY's signed book weights (position value / equity) over each name's
                  FULL split-adjusted history  x  R, the MEDIAN ratio of each new worst-5-session record to the
                  previous one (records above the median |5-session move| only) — "the next record is usually ~R x
                  the last"
    vix_scale = max(1, VIX / median VIX over its full history)            (a tightener only)
    concentration = max over names of |weight| x that name's worst adverse day (a name with a shorter history than
                    the book's longest takes the largest worst day in the book — its own record understates it)
    m = the account's live maintenance ratio: the stricter of maintenance_margin / gross and / long position value
    K_dyn = min(ceiling, rolling 5-session minimum of K_raw)              tighten at once, loosen only after 5 sessions
    (one K_raw per session of data — keyed by the last daily bar — keeping the tightest reading of that session)

A breach only BLOCKS new overnight entries; nothing here ever sells. refresh() runs from cron (run_overnight_k.py)
before the open and writes data/state/overnight_k.json atomically. overnight_k() is the only reader the tiers call:
missing / unreadable state -> the ceiling (today's behaviour); stale state -> the last good K_dyn (never looser than
the last good value, never above the ceiling). refresh() never raises; overnight_k() raises only on a non-finite
config ceiling, and every caller fails closed.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
_DEFAULT_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "overnight_k.json"
_STATE = _DEFAULT_STATE


def _test_guard() -> bool:
    """True in a test process using the PRODUCTION state path: tests never read or write live state (a test must
    patch _STATE to a temp file). Mirrors the broker's refuse-under-test guard."""
    return os.environ.get("MTF_TEST_MODE") == "1" and _STATE == _DEFAULT_STATE
VIX_FETCH_TIMEOUT_SECONDS = 30
_warned_stale: set = set()


def ceiling() -> float:
    """The Invariant #11 ceiling from config. K_dyn is never above it. A non-finite value raises (every caller fails
    closed) instead of returning NaN, which a min() would silently skip."""
    cap = float(config.SWING_BREAKOUT_TOTAL_OVERNIGHT_K)
    if not math.isfinite(cap):
        raise ValueError(f"overnight K ceiling is not finite: {cap}")
    return cap


def _load() -> "dict | None":
    try:
        if _test_guard() or not _STATE.exists():
            return None
        d = json.loads(_STATE.read_text())
        return d if isinstance(d, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("overnight K state unreadable (%s) — using the ceiling", e)
        return None


def overnight_k() -> float:
    """The overnight cap K the tiers enforce now: min(ceiling, last computed K_dyn). Raises only when the config ceiling
    itself is not a finite number (every caller then fails closed); any state problem returns the ceiling."""
    cap = ceiling()
    try:
        st = _load()
        if st is None:
            return cap
        k = float(st.get("k_dyn") or "nan")
        if not (math.isfinite(k) and k > 0):
            return cap
        today = f"{datetime.now(ET):%Y-%m-%d}"
        if st.get("session") != today and today not in _warned_stale:
            _warned_stale.add(today)
            logger.warning("overnight K state is from %s (not today) — using its K %.3f (never looser than the "
                           "last good value)", st.get("session"), min(cap, k))
        return min(cap, k)
    except Exception as e:  # noqa: BLE001
        logger.warning("overnight K unreadable (%s) — using the ceiling", e)
        return cap


def compute_k_raw(weights: dict, closes, maint_ratio: float, vix=None, vix_scale_fallback=None) -> "dict | None":
    """Pure computation (no I/O). weights: {symbol: signed position value / equity}; closes: DataFrame of split-
    adjusted daily closes (columns = symbols); maint_ratio: m; vix: Series of daily VIX closes or None;
    vix_scale_fallback: the last good VIX scale, used only when vix is None (never below 1).
    Returns {"k_raw", inputs...} or None when the history cannot support a number (caller keeps the last good K)."""
    import pandas as pd
    w_all = pd.Series({s: float(v) for s, v in weights.items() if math.isfinite(float(v))})
    w = w_all[[s for s in w_all.index if s in closes.columns]]
    no_history = sorted(set(w_all.index) - set(w.index))   # e.g. an option or a first-day listing: no daily bars
    lev = float(w_all.abs().sum())
    if w.empty or not (lev > 0) or not (math.isfinite(maint_ratio) and 0 < maint_ratio < 1):
        return None
    px = closes[list(w.index)].astype(float)
    # forward-fill a gap (halt / missing bar) INSIDE each name's own history, so the move across the gap lands on the
    # day trading resumes instead of vanishing as NaN (adversarial review 2026-10-14); never before its first bar
    px = px.apply(lambda c: c.loc[c.first_valid_index():c.last_valid_index()].ffill().reindex(c.index)
                  if c.first_valid_index() is not None else c)
    rets = px.pct_change(fill_method=None)
    book = (rets.fillna(0) * w).sum(axis=1)
    loss5 = -book.rolling(5).sum().dropna()
    if loss5.empty or not (loss5.max() > 0):
        return None
    rec = loss5.cummax()
    jumps = rec[rec.diff() > 0]
    ratios = (jumps / jumps.shift()).dropna()
    ratios = ratios[jumps.shift().reindex(ratios.index) > loss5.abs().median()]
    record_jump = max(1.0, float(ratios.median())) if len(ratios) else 1.0
    stress_hist = float(loss5.max()) * record_jump
    if vix is not None and len(vix):
        vix_scale = max(1.0, float(vix.iloc[-1]) / float(vix.median()))
    else:
        vix_scale = max(1.0, float(vix_scale_fallback or 1.0))
    worst = {}
    for s in w.index:
        r = rets[s].dropna()
        worst[s] = float(-r.min()) if w[s] > 0 else float(r.max())   # the worst ADVERSE day for this side
    starts = {s: closes[s].first_valid_index() for s in w.index}
    earliest = min(starts.values())
    finite = [v for v in worst.values() if math.isfinite(v)]
    if not finite:
        return None
    book_worst = max(finite)
    for s in w.index:
        # shorter history than the book's longest (or too short to have a worst day): its own record understates it
        if starts[s] > earliest or not math.isfinite(worst[s]):
            worst[s] = max(worst[s], book_worst) if math.isfinite(worst[s]) else book_worst
    for s in no_history:                # no history at all: charge it the book's worst day
        worst[s] = book_worst
    concentration = max(abs(float(w_all[s])) * worst[s] for s in w_all.index)
    stress = max(stress_hist * vix_scale, concentration)
    d_book = stress / lev
    k_raw = 1.0 / (maint_ratio + (1.0 - maint_ratio) * d_book)
    return {"k_raw": k_raw, "book_leverage": lev, "maint_ratio": maint_ratio, "worst_5s": float(loss5.max()),
            "record_jump": record_jump, "vix_scale": vix_scale, "concentration": concentration, "d_book": d_book,
            "history_start": f"{earliest:%Y-%m-%d}", "last_bar": f"{closes.index.max():%Y-%m-%d}",
            "no_history": no_history}


def _fetch_vix():
    """Full ^VIX daily history (T4 yfinance — ^VIX is not on Alpaca Data; permitted by the data-source rules).
    None on failure/timeout (the caller keeps the last good VIX scale). Never raises."""
    import concurrent.futures as cf

    def _get():
        import yfinance as yf
        s = yf.download("^VIX", period="max", progress=False, auto_adjust=False)["Close"].squeeze()
        return s.dropna()
    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_get)
        return fut.result(timeout=VIX_FETCH_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("overnight K: ^VIX history unavailable via yfinance (T4) — %s", e)
        return None
    finally:
        ex.shutdown(wait=False)


def _alert(msg: str) -> None:
    """WARNING log + Slack (alerts.py refuses to send in a test process). Never raises."""
    logger.warning(msg)
    try:
        from alerts import send_slack
        send_slack(f":warning: {msg}")
    except Exception as e:  # noqa: BLE001
        logger.warning("overnight K alert not sent: %s", e)


def _write(d: dict) -> None:
    if _test_guard():
        raise RuntimeError("refusing to write the production overnight K state from a test")
    os.makedirs(_STATE.parent, exist_ok=True)
    tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _STATE)


def refresh() -> "dict | None":
    """Compute today's K_raw from the live book and history, fold it into the rolling minimum, write the state.
    Returns the new state, or None when nothing could be computed (the last good state stays). Never raises."""
    try:
        import pandas as pd
        from data.fetcher import fetch_bars_window
        from execution import broker
        cap = ceiling()
        acct = broker.get_account()
        equity = float(acct.equity)
        positions = broker.get_open_positions() or []
        weights = {p.symbol: float(p.market_value) / equity for p in positions if float(p.market_value or 0) != 0}
        gross = sum(abs(float(p.market_value)) for p in positions)
        today = f"{datetime.now(ET):%Y-%m-%d}"
        prev = _load() or {}
        history = dict(prev.get("history") or {})
        last_vs = (prev.get("inputs") or {}).get("vix_scale")
        key = None
        if not weights or not (gross > 0) or not (equity > 0):
            # A flat book measures nothing: add NO reading, so a stress reading is never pushed out of the window by
            # a ceiling placeholder (adversarial review 2026-10-14: after a flatten, K jumped 1.40 -> 1.90 same day).
            k_raw, inputs = cap, {"note": "no open positions — no new reading", "vix_scale": last_vs}
        else:
            # m: the stricter of maintenance / gross and maintenance / long value (the margin formula is derived
            # for a long book; a short in the book must never make m — and so K — looser)
            long_mv = sum(float(p.market_value) for p in positions if float(p.market_value) > 0)
            maint = float(acct.maintenance_margin) / gross
            if long_mv > 0:
                maint = max(maint, float(acct.maintenance_margin) / long_mv)
            end = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            start = datetime.fromtimestamp(0, timezone.utc)                 # full history
            cols = {}
            for s in weights:
                df = fetch_bars_window(s, config.TF_DAILY, start, end, feed="sip", adjustment="split")   # T1, split-adjusted
                if df is not None and not df.empty:
                    c = df["close"].astype(float)
                    idx = pd.to_datetime(c.index)
                    idx = idx.tz_localize(timezone.utc) if idx.tz is None else idx   # Alpaca bars are UTC-stamped
                    c.index = idx.tz_convert(ET).tz_localize(None).normalize()
                    c = c[c.index < pd.Timestamp(today)]         # completed sessions only (no in-progress bar)
                    if not c.empty:
                        cols[s] = c
            missing = sorted(set(weights) - set(cols))
            if missing:
                _alert(f"overnight K: no daily history for {missing} — each is charged the book's worst day")
            vix = _fetch_vix()
            if vix is not None and not len(vix):
                vix = None
            if vix is None:
                _alert(f"overnight K: ^VIX history unavailable (yfinance, T4) — using the last good VIX scale {last_vs}")
            res = compute_k_raw(weights, pd.DataFrame(cols), maint, vix, vix_scale_fallback=last_vs)
            if res is None:
                logger.warning("overnight K: history cannot support a number — last good K stays")
                return None
            k_raw, inputs = float(res.pop("k_raw")), res
            key = str(res["last_bar"])
        # One reading per SESSION OF DATA (the last daily bar), keeping the TIGHTEST: the morning and after-close runs
        # of one day (and a holiday run) share a key, so a later, looser run can never erase a stress reading — the
        # 5-session rolling minimum below counts real sessions (cold-2nd 2026-10-14).
        if key is not None:
            history[key] = min(k_raw, float(history.get(key, k_raw)))
        series = pd.Series(history, dtype=float).sort_index()
        k_dyn = min(cap, float(series.rolling(5, min_periods=1).min().iloc[-1])) if len(series) else cap
        state = {"session": today, "computed_at": datetime.now(PT).isoformat(), "ceiling": cap, "k_raw": k_raw,
                 "k_dyn": k_dyn, "inputs": inputs, "history": {str(k): float(v) for k, v in series.items()}}
        _write(state)
        logger.info("overnight K refreshed: K_raw %.3f, K_dyn %.3f (ceiling %.2f) | %s", k_raw, k_dyn, cap, inputs)
        try:
            import trade_logger
            trade_logger.log_event("overnight_k", data_source="alpaca_data", k_raw=round(k_raw, 4),
                                   k_dyn=round(k_dyn, 4), ceiling=cap, inputs=inputs)
        except Exception as _le:  # noqa: BLE001
            logger.warning("overnight_k event not logged: %s", _le)
        return state
    except Exception as e:  # noqa: BLE001
        logger.error("overnight K refresh failed (last good K stays): %s", e)
        return None
