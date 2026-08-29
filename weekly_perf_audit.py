# ruff: noqa: E501
"""
weekly_perf_audit.py — Weekly trading performance audit
Classifies trades into 8 failure categories, computes statistics,
generates board hypotheses, writes HTML tearsheet, posts Slack summary.

Runs: Friday 4:15 PM ET via OCI cron
      15 20 * * 5 cd /home/ubuntu/mtf-bot && venv/bin/python3 weekly_perf_audit.py >> logs/weekly_audit_cron.log 2>&1

NOT in RTH import chain → no DS/GAI code gate.
RTH Block — REMOVED (Rafael mandate 2026-06-30). May now run at any time.
Atomic write on all output files.
Standalone: no imports from bot execution modules.
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests  # type: ignore[import-untyped]

try:
    import yfinance as yf  # type: ignore[import-untyped]

    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False

try:
    from scipy import stats as _scipy_stats  # type: ignore[import-untyped]

    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False

# AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This script
# writes only its own dedicated weekly output files (HTML tearsheet, JSON
# cache) -- no write-contention risk with the live bot's shared state.
_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

# ── Paths ──────────────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).resolve().parent
_LOG_DIR = _BASE_DIR / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_TRADE_EVENTS_FILE = _LOG_DIR / "trade_events.jsonl"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("weekly_perf_audit")

# ── Constants ─────────────────────────────────────────────────────────────────
_ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
_ALPACA_DATA_BASE = "https://data.alpaca.markets"
_FMP_BASE = "https://financialmodelingprep.com/api"

_DAYS_BACK = 7          # look-back window for fills + events
_CACHE_TTL_SECS = 7200  # 2-hour API cache
_API_TIMEOUT = 8.0      # per-call timeout (seconds)
_MAX_RETRIES = 2

# MIN_TRADES thresholds from spec §14 Addition 4
_MIN_TRADES_OFFENSIVE = 20   # MIN_SCORE, Kelly, scoring weights
_MIN_TRADES_DEFENSIVE = 12   # TOD gate, earnings block, stop widening
_MIN_TRADES_EMERGENCY = 5    # >25% drawdown escalation
_MIN_TRADES_MONTHLY = 10     # monthly mislabeling gate

# Addition 1: emergency escalation
_EMERGENCY_DRAWDOWN_PCT = 0.25  # >25% weekly equity drawdown

# Addition 2: VIX regime thresholds
_VIX_LOW_VOL = 15.0
_VIX_ELEVATED = 20.0
_VIX_HIGH = 25.0
_VIX_EXTREME = 30.0

# Category 1b low-vol stop tightness threshold
_LOW_VOL_TIGHT_STOP_RATIO = 0.5  # stop < 0.5× 30d avg ATR → flag

# Hypothesis gate: 40% impact minimum
_HYP_MIN_IMPACT_PCT = 0.40


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_pt(dt: datetime) -> str:
    """Convert any tz-aware datetime to PT display string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return dt.astimezone(_PT).strftime("%Y-%m-%d %I:%M %p PT")


def _week_label(dt: datetime | None = None) -> str:
    d = (dt or datetime.now(_PT)).date()
    return d.strftime("%Y-W%W")


def _alpaca_headers() -> dict:
    key = os.getenv("ALPACA_API_KEY", "")
    secret = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _safe_get(url: str, params: dict | None = None, headers: dict | None = None) -> dict | list | None:
    """GET with retries. Returns parsed JSON or None on failure."""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_API_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("GET %s → HTTP %d (attempt %d)", url, resp.status_code, attempt + 1)
        except Exception as exc:
            logger.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
        if attempt < _MAX_RETRIES:
            time.sleep(1.5 * (attempt + 1))
    return None


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via tmp→replace (RC-5 compliant)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.error("Atomic write to %s failed: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_fills(days_back: int = _DAYS_BACK) -> list[dict]:
    """Fetch FILL activities from Alpaca paper account for last N days.
    Paginated via after_id. Returns list of fill dicts."""
    try:
        headers = _alpaca_headers()
    except RuntimeError as exc:
        logger.warning("_fetch_fills: %s — returning empty", exc)
        return []

    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    all_fills: list[dict] = []
    after_id: str | None = None
    page = 0
    max_pages = 20

    while page < max_pages:
        params: dict = {"activity_type": "FILL", "after": since_str, "page_size": 100}
        if after_id:
            params["after_id"] = after_id
        result = _safe_get(f"{_ALPACA_PAPER_BASE}/v2/account/activities/FILL", params=params, headers=headers)
        if not isinstance(result, list) or not result:
            break
        all_fills.extend(result)
        after_id = result[-1].get("id")
        if len(result) < 100:
            break
        page += 1

    logger.info("_fetch_fills: fetched %d fills (past %d days)", len(all_fills), days_back)
    return all_fills


def _load_trade_events(days_back: int = _DAYS_BACK) -> list[dict]:
    """Load trade_events.jsonl entries from the last N days."""
    if not _TRADE_EVENTS_FILE.exists():
        logger.warning("trade_events.jsonl not found — returning empty")
        return []

    cutoff = datetime.now(_PT) - timedelta(days=days_back)
    events: list[dict] = []
    bad_lines = 0
    try:
        with _TRADE_EVENTS_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    ts_raw = ev.get("ts", "")
                    if not ts_raw:
                        continue
                    # Parse ISO-8601 (may be PT-offset or Z)
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_PT)
                    if ts.astimezone(_PT) >= cutoff:
                        events.append(ev)
                except (json.JSONDecodeError, ValueError):
                    bad_lines += 1
    except Exception as exc:
        logger.error("_load_trade_events: read error: %s", exc)
        return []

    if bad_lines:
        logger.warning("_load_trade_events: %d malformed lines skipped", bad_lines)
    logger.info("_load_trade_events: loaded %d events (past %d days)", len(events), days_back)
    return events


def _fetch_daily_bars(symbols: list[str], days_back: int = 14) -> dict[str, list[dict]]:
    """Fetch daily OHLCV bars from Alpaca Data T1 REST API.
    Returns {symbol: [{"t","o","h","l","c","v"}, ...]} oldest-first."""
    if not symbols:
        return {}
    try:
        headers = _alpaca_headers()
    except RuntimeError as exc:
        logger.warning("_fetch_daily_bars: %s — returning empty", exc)
        return {}

    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    sym_str = ",".join(symbols)
    url = f"{_ALPACA_DATA_BASE}/v2/stocks/bars"
    params = {
        "symbols": sym_str,
        "timeframe": "1Day",
        "start": since,
        "limit": days_back + 5,
        "adjustment": "split",
        "feed": "iex",
    }
    result = _safe_get(url, params=params, headers=headers)
    bars: dict[str, list[dict]] = {}
    if isinstance(result, dict):
        data = result.get("bars", {})
        for sym, bar_list in data.items():
            if isinstance(bar_list, list):
                bars[sym] = bar_list
    logger.info("_fetch_daily_bars: received bars for %d/%d symbols", len(bars), len(symbols))
    return bars


def _fetch_vix_history(days_back: int = 25) -> list[dict]:
    """Fetch ^VIX daily history via yfinance (T4 — audit-only, non-RTH).
    Returns [{"date": "YYYY-MM-DD", "close": float, "high": float, "low": float}]."""
    if not _YF_AVAILABLE:
        logger.warning("_fetch_vix_history: yfinance not available — VIX analysis disabled")
        return []
    try:
        import concurrent.futures
        def _dl():
            ticker = yf.Ticker("^VIX")
            hist = ticker.history(period=f"{days_back}d")
            return hist
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_dl)
            hist = fut.result(timeout=10)
        if hist is None or hist.empty:
            logger.warning("_fetch_vix_history: empty result from yfinance")
            return []
        records = []
        for idx, row in hist.iterrows():
            records.append({
                "date": str(idx.date()),
                "close": float(row.get("Close", 0) or 0),
                "high": float(row.get("High", 0) or 0),
                "low": float(row.get("Low", 0) or 0),
            })
        logger.info("_fetch_vix_history: %d VIX records fetched", len(records))
        return records
    except Exception as exc:
        logger.warning("_fetch_vix_history: failed: %s", exc)
        return []


def _build_vix_lookup(vix_records: list[dict]) -> dict[str, dict]:
    """Build {date_str: {close, high, low, zscore}} from VIX history.
    z-score computed over last 20 trading days available."""
    if not vix_records:
        return {}
    closes = [r["close"] for r in vix_records if r["close"] > 0]
    window = min(20, len(closes))
    lookup: dict[str, dict] = {}
    for i, rec in enumerate(vix_records):
        if rec["close"] <= 0:
            continue
        window_closes = closes[max(0, i - window + 1): i + 1]
        mean = sum(window_closes) / len(window_closes) if window_closes else rec["close"]
        variance = sum((x - mean) ** 2 for x in window_closes) / max(len(window_closes) - 1, 1)
        stdev = math.sqrt(variance) if variance > 0 else 1.0
        zscore = (rec["close"] - mean) / stdev if stdev > 0 else 0.0
        lookup[rec["date"]] = {
            "close": rec["close"],
            "high": rec["high"],
            "low": rec["low"],
            "zscore": round(zscore, 3),
            "mean_20d": round(mean, 2),
            "regime": (
                "LOW_VOL" if rec["close"] < _VIX_LOW_VOL else
                "NORMAL" if rec["close"] < _VIX_ELEVATED else
                "ELEVATED" if rec["close"] < _VIX_HIGH else
                "HIGH" if rec["close"] < _VIX_EXTREME else
                "EXTREME"
            ),
        }
    return lookup


def _fetch_earnings_calendar(symbols: list[str], days_back: int = 14) -> dict[str, list[str]]:
    """Fetch upcoming/recent earnings dates from FMP.
    Returns {symbol: [date_str, ...]}. Silently degrades if FMP key missing."""
    fmp_key = os.getenv("FMP_API_KEY", "")
    if not fmp_key or not symbols:
        logger.info("_fetch_earnings_calendar: FMP key missing or no symbols — skipping")
        return {}

    since = (datetime.now(_ET) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    until = (datetime.now(_ET) + timedelta(days=7)).strftime("%Y-%m-%d")
    url = f"{_FMP_BASE}/v3/earning_calendar"
    params = {"from": since, "to": until, "apikey": fmp_key}
    result = _safe_get(url, params=params)
    earnings: dict[str, list[str]] = {}
    if isinstance(result, list):
        for item in result:
            sym = item.get("symbol", "")
            dt = item.get("date", "")
            if sym in symbols and dt:
                earnings.setdefault(sym, []).append(dt)
    logger.info("_fetch_earnings_calendar: got earnings data for %d/%d symbols", len(earnings), len(symbols))
    return earnings


# ── Trade reconstruction ──────────────────────────────────────────────────────

def _parse_fill_ts(fill: dict) -> datetime | None:
    """Parse Alpaca fill transaction_time to tz-aware datetime."""
    raw = fill.get("transaction_time") or fill.get("filled_at") or ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _reconstruct_trades(fills: list[dict], events: list[dict]) -> list[dict]:
    """Join Alpaca fills to trade_events by symbol + ±30s timestamp.
    Each trade record = entry fill + exit fill + event context.
    Returns list of completed trade dicts."""
    # Build entry / exit fill buckets per symbol
    entry_fills: dict[str, list[dict]] = {}
    exit_fills: dict[str, list[dict]] = {}

    for fill in fills:
        sym = fill.get("symbol", "")
        side = fill.get("side", "")
        if not sym or not side:
            continue
        if side in ("buy", "sell_short"):
            entry_fills.setdefault(sym, []).append(fill)
        elif side in ("sell", "buy_to_cover"):
            exit_fills.setdefault(sym, []).append(fill)

    # Build event lookup: symbol → list of entry/exit events
    entry_events: dict[str, list[dict]] = {}
    exit_events: dict[str, list[dict]] = {}
    for ev in events:
        sym = ev.get("symbol", "")
        ev_type = ev.get("event", "")
        if not sym:
            continue
        if ev_type == "entry":
            entry_events.setdefault(sym, []).append(ev)
        elif ev_type in ("exit", "stop_hit", "partial_exit"):
            exit_events.setdefault(sym, []).append(ev)

    def _find_closest_event(evs: list[dict], ts: datetime, window_secs: int = 30) -> dict | None:
        best: dict | None = None
        best_delta: float = float(window_secs + 1)
        for ev in evs:
            ev_ts_raw = ev.get("ts", "")
            if not ev_ts_raw:
                continue
            try:
                ev_ts = datetime.fromisoformat(ev_ts_raw.replace("Z", "+00:00"))
                delta = abs((ts - ev_ts).total_seconds())
                if delta <= window_secs and delta < best_delta:
                    best, best_delta = ev, delta
            except ValueError:
                continue
        return best

    trades: list[dict] = []
    all_syms = set(entry_fills) | set(exit_fills)

    for sym in all_syms:
        e_fills = sorted(entry_fills.get(sym, []), key=lambda f: f.get("transaction_time", ""))
        x_fills = sorted(exit_fills.get(sym, []), key=lambda f: f.get("transaction_time", ""))

        for ef in e_fills:
            ef_ts = _parse_fill_ts(ef)
            if ef_ts is None:
                continue
            # Match to closest exit fill after entry
            matched_exit = None
            for xf in x_fills:
                xf_ts = _parse_fill_ts(xf)
                if xf_ts and xf_ts > ef_ts:
                    matched_exit = xf
                    break

            entry_ev = _find_closest_event(entry_events.get(sym, []), ef_ts)
            _exit_ts_parsed: datetime | None = _parse_fill_ts(matched_exit) if matched_exit else None
            _exit_match_ts: datetime = _exit_ts_parsed if _exit_ts_parsed is not None else ef_ts
            exit_ev = _find_closest_event(exit_events.get(sym, []), _exit_match_ts)

            entry_price = float(ef.get("price", 0) or 0)
            entry_qty = float(ef.get("qty", 0) or 0)
            exit_price = float(matched_exit.get("price", 0) if matched_exit else 0)
            exit_qty = float(matched_exit.get("qty", 0) if matched_exit else 0)

            direction = "long" if ef.get("side") == "buy" else "short"
            if exit_price > 0 and entry_price > 0:
                raw_pnl = (exit_price - entry_price) * exit_qty if direction == "long" else (entry_price - exit_price) * exit_qty
            else:
                raw_pnl = float(ef.get("net_amount", 0) or 0)

            pnl_source = "alpaca_fill" if exit_price > 0 else "tracker_estimate"
            if entry_ev:
                pnl_source = "alpaca_fill" if exit_price > 0 else "tracker_estimate"

            hold_mins: float = 0.0
            if matched_exit:
                xf_ts = _parse_fill_ts(matched_exit)
                if xf_ts:
                    hold_mins = (xf_ts - ef_ts).total_seconds() / 60

            entry_hour = ef_ts.astimezone(_ET).hour
            entry_time_et = ef_ts.astimezone(_ET)

            score = int(entry_ev.get("score", 0)) if entry_ev else 0
            mri_level = entry_ev.get("mri_level", "") if entry_ev else ""
            pdt_used = int(entry_ev.get("pdt_used", 0)) if entry_ev else 0

            trade_id = f"{sym}_{ef_ts.strftime('%Y%m%d_%H%M%S')}_{ef.get('id', 'na')[:6]}"

            trades.append({
                "trade_id": trade_id,
                "symbol": sym,
                "direction": direction,
                "status": "closed" if matched_exit else "open",
                "entry_ts": ef_ts.isoformat(),
                "entry_ts_pt": _fmt_pt(ef_ts),
                "entry_price": entry_price,
                "entry_qty": entry_qty,
                "entry_hour_et": entry_hour,
                "entry_time_et_mins": entry_time_et.hour * 60 + entry_time_et.minute,
                "exit_ts": _exit_ts_parsed.isoformat() if _exit_ts_parsed is not None else "",
                "exit_price": exit_price,
                "exit_qty": exit_qty,
                "exit_reason": exit_ev.get("event", "") if exit_ev else "",
                "hold_mins": round(hold_mins, 1),
                "hold_days": round(hold_mins / 390, 2),  # 390 trading mins/day
                "score_12pt": score,
                "mri_level": mri_level,
                "pdt_used_at_entry": pdt_used,
                "pnl_gross": round(raw_pnl, 2),
                "pnl_pct": round(raw_pnl / (entry_price * entry_qty) * 100, 3) if entry_price * entry_qty > 0 else 0,
                "pnl_source": pnl_source,
                "is_winner": raw_pnl > 0,
                "is_loser": raw_pnl < 0,
                "is_breakeven": raw_pnl == 0,
                "entry_fill_id": ef.get("id", ""),
                "exit_fill_id": matched_exit.get("id", "") if matched_exit else "",
                "event_status": "matched" if entry_ev else "orphan_fill",
                "classification": None,  # filled by _classify_trade
            })

    trades.sort(key=lambda t: t["entry_ts"])
    logger.info("_reconstruct_trades: %d trades (fills joined to events)", len(trades))
    return trades


# ── Classification ────────────────────────────────────────────────────────────

def _classify_trade(
    trade: dict,
    vix_lookup: dict[str, dict],
    spy_bars: list[dict],
    earnings_data: dict[str, list[str]],
    min_score: int = 10,
) -> dict:
    """Classify a trade into primary + secondary failure categories.
    Returns classification dict (also stored on trade['classification']).

    Categories:
        1a  Directional Macro Headwind
        1b  Volatility Regime Sizing Error  (+ 1b_low_vol sub-category)
        2   Marginal Score Low-Momentum
        3   Leveraged Sizing Error PDT-triggered
        4   Time-of-Day Bleed
        5   Earnings Risk
        6   VIX Stop Crush
        7   Holding Period Mismatch
        8   Unknown
    """
    cats: list[str] = []
    trace: list[dict] = []

    entry_date = trade["entry_ts"][:10] if trade["entry_ts"] else ""
    vix_day = vix_lookup.get(entry_date, {})
    vix_close = vix_day.get("close", 0.0)
    vix_zscore = vix_day.get("zscore", 0.0)
    vix_regime = vix_day.get("regime", "UNKNOWN")
    direction = trade["direction"]
    hold_mins = trade["hold_mins"]
    score = trade["score_12pt"]
    sym = trade["symbol"]
    entry_mins = trade["entry_time_et_mins"]
    pdt_used = trade["pdt_used_at_entry"]

    # ── SPY bar context ───────────────────────────────────────────────────────
    spy_bar_on_entry: dict | None = None
    for bar in spy_bars:
        bar_date = bar.get("t", "")[:10]
        if bar_date == entry_date:
            spy_bar_on_entry = bar
            break

    spy_bearish = False
    spy_ema_declining = False
    if spy_bar_on_entry:
        spy_close = float(spy_bar_on_entry.get("c", 0) or 0)
        spy_open = float(spy_bar_on_entry.get("o", 0) or 0)
        spy_bearish = spy_close < spy_open
        # Simple proxy for EMA decline: close < open for 2+ days (use bar order)
        idx = spy_bars.index(spy_bar_on_entry)
        if idx >= 1:
            prev_bar = spy_bars[idx - 1]
            prev_close = float(prev_bar.get("c", 0) or 0)
            spy_ema_declining = spy_close < prev_close and spy_bearish

    # ── 1a: Directional Macro Headwind ───────────────────────────────────────
    cat_1a = False
    if direction == "long" and spy_bearish:
        cat_1a = True
        trace.append({"rule": "1a_directional", "fired": True, "spy_bearish": True, "direction": direction})
    elif direction == "long" and spy_ema_declining:
        cat_1a = True
        trace.append({"rule": "1a_ema_decline", "fired": True, "spy_ema_declining": True})
    elif direction == "short" and not spy_bearish and spy_bar_on_entry:
        cat_1a = True
        trace.append({"rule": "1a_directional", "fired": True, "spy_bullish_on_short": True})

    if cat_1a:
        cats.append("1a")

    # ── 1b: Volatility Regime Sizing Error ───────────────────────────────────
    cat_1b = False
    if vix_zscore > 1.0:
        cat_1b = True
        trace.append({"rule": "1b_high_vol", "fired": True, "vix_zscore": vix_zscore, "note": "VIX z-score > 1.0 at entry — sizing may have been undersized for vol regime"})
    # Addition 2: LOW_VOL sub-case
    elif vix_close > 0 and vix_close < _VIX_LOW_VOL:
        cat_1b = True
        trace.append({"rule": "1b_low_vol_tight_stop", "fired": True, "vix": vix_close, "regime": "LOW_VOL", "note": "VIX < 15 — ATR may be artificially compressed, stop susceptible to normal noise"})

    if cat_1b:
        cats.append("1b")

    # ── 2: Marginal Score Low-Momentum ───────────────────────────────────────
    cat_2 = False
    if score in (min_score, min_score + 1):
        cat_2 = True
        trace.append({"rule": "2_marginal_score", "fired": True, "score": score, "min_score": min_score})

    if cat_2:
        cats.append("2")

    # ── 3: Leveraged Sizing Error PDT-triggered ───────────────────────────────
    _BUCKET_A = {"TQQQ", "SQQQ", "TSLL", "NVDL"}
    cat_3 = False
    if sym in _BUCKET_A and pdt_used >= 2:
        cat_3 = True
        trace.append({"rule": "3_leveraged_pdt", "fired": True, "symbol": sym, "pdt_used": pdt_used})

    if cat_3:
        cats.append("3")

    # ── 4: Time-of-Day Bleed ─────────────────────────────────────────────────
    _OPEN_WINDOW_END = 9 * 60 + 45   # 9:45 AM ET
    _OPEN_WINDOW_START = 9 * 60 + 30  # 9:30 AM ET
    _CLOSE_WINDOW_START = 15 * 60 + 30  # 3:30 PM ET
    cat_4 = False
    if _OPEN_WINDOW_START <= entry_mins <= _OPEN_WINDOW_END:
        cat_4 = True
        trace.append({"rule": "4_opening_window", "fired": True, "entry_mins_et": entry_mins, "window": "9:30-9:45 AM ET"})
    elif entry_mins >= _CLOSE_WINDOW_START:
        cat_4 = True
        trace.append({"rule": "4_closing_window", "fired": True, "entry_mins_et": entry_mins, "window": "3:30-4:00 PM ET"})

    if cat_4:
        cats.append("4")

    # ── 5: Earnings Risk ──────────────────────────────────────────────────────
    cat_5 = False
    if sym in earnings_data:
        for earn_date_str in earnings_data[sym]:
            try:
                earn_dt = datetime.strptime(earn_date_str, "%Y-%m-%d").date()
                entry_dt = datetime.fromisoformat(trade["entry_ts"][:10]).date()
                days_away = abs((earn_dt - entry_dt).days)
                if days_away <= 5:
                    cat_5 = True
                    trace.append({"rule": "5_earnings_proximity", "fired": True, "earnings_date": earn_date_str, "days_away": days_away})
                    break
            except ValueError:
                continue

    if cat_5:
        cats.append("5")

    # ── 6: VIX Stop Crush ────────────────────────────────────────────────────
    cat_6 = False
    if vix_day.get("high", 0) > 0 and vix_day.get("close", 0) > 0:
        # Intraday VIX range as proxy for stop crush risk
        vix_intraday_range = vix_day["high"] - vix_day.get("low", vix_day["close"])
        if vix_intraday_range > vix_day["close"] * 0.08:  # >8% intraday VIX spike
            cat_6 = True
            trace.append({"rule": "6_vix_intraday_spike", "fired": True, "vix_range": round(vix_intraday_range, 2), "vix_close": vix_day["close"]})

    if cat_6:
        cats.append("6")

    # ── 7: Holding Period Mismatch ───────────────────────────────────────────
    cat_7 = False
    if hold_mins > 0 and hold_mins < 5:
        cat_7 = True
        trace.append({"rule": "7_scalp_noise", "fired": True, "hold_mins": hold_mins, "note": "Exited in <5 minutes — scalped on noise"})
    elif hold_mins > 3 * 390:  # >3 trading days
        cat_7 = True
        trace.append({"rule": "7_overheld", "fired": True, "hold_days": trade["hold_days"], "note": "Held >3 trading days on an intraday signal"})

    if cat_7:
        cats.append("7")

    # ── Primary + secondary assignment ───────────────────────────────────────
    if not cats:
        primary = "8"
        secondary: list[str] = []
    else:
        primary = cats[0]
        secondary = cats[1:3]  # up to 3 secondary

    # Correlation weight: 1b + 6 both VIX → same root cause → 0.7×
    # Orthogonal: 1a + 4 → independent → 1.3×
    weight = 1.0
    if "1b" in cats and "6" in cats:
        weight = 0.7
    elif "1a" in cats and "4" in cats:
        weight = 1.3

    return {
        "primary": primary,
        "secondary": secondary,
        "fired_categories": cats,
        "confidence": round(min(0.5 + len(cats) * 0.15, 0.95), 2),
        "weight_multiplier": weight,
        "vix_regime": vix_regime,
        "vix_zscore": round(vix_zscore, 3),
        "spy_bearish": spy_bearish,
        "trace": trace,
    }


# ── Statistics ────────────────────────────────────────────────────────────────

def _compute_category_stats(trades: list[dict]) -> dict[str, dict]:
    """Compute per-category stats: count, loss total, hit rate, z-test vs baseline."""
    cat_ids = ["1a", "1b", "2", "3", "4", "5", "6", "7", "8"]
    closed = [t for t in trades if t["status"] == "closed"]
    total = len(closed)
    wins_total = sum(1 for t in closed if t["is_winner"])
    baseline_wr = wins_total / total if total > 0 else 0.5

    stats: dict[str, dict] = {}
    for cat in cat_ids:
        in_cat = [t for t in closed if t.get("classification") and t["classification"]["primary"] == cat]
        wins_in = sum(1 for t in in_cat if t["is_winner"])
        loss_in = sum(1 for t in in_cat if t["is_loser"])
        total_loss = sum(t["pnl_gross"] for t in in_cat if t["is_loser"])
        n = len(in_cat)
        cat_wr = wins_in / n if n > 0 else 0.0

        # 2-proportion z-test (category WR vs portfolio baseline)
        p_value = 1.0
        if _SCIPY_AVAILABLE and n >= 3:
            try:
                count = [wins_in, wins_total]
                nobs = [n, total]
                _, p_value = _scipy_stats.proportions_ztest(count, nobs, alternative="two-sided")
            except Exception:
                p_value = 1.0
        elif n >= 3:
            # Simple approximation without scipy
            p1 = cat_wr
            p2 = baseline_wr
            p_pool = (wins_in + wins_total) / (n + total) if (n + total) > 0 else 0.5
            se = math.sqrt(p_pool * (1 - p_pool) * (1 / n + 1 / total)) if n > 0 and total > 0 else 1.0
            z = (p1 - p2) / se if se > 0 else 0.0
            # rough 2-tailed p from z
            p_value = 2 * (1 - min(0.9999, abs(z) / 4.0))

        # 4-week trend (placeholder — would need prior weeks' data)
        stats[cat] = {
            "category": cat,
            "n_primary": n,
            "n_wins": wins_in,
            "n_losses": loss_in,
            "win_rate": round(cat_wr, 3),
            "baseline_wr": round(baseline_wr, 3),
            "total_loss": round(total_loss, 2),
            "p_value": round(float(p_value), 4),
            "significant": p_value < 0.20,
        }

    return stats


def _check_hypothesis_gate(category: str, trade_count: int, change_type: str) -> str:
    """Returns 'READY', 'INSUFFICIENT_DATA (N/MIN)', or 'CONCENTRATED_NEEDS_CONFIRMATION'."""
    thresholds = {
        "offensive": _MIN_TRADES_OFFENSIVE,
        "defensive": _MIN_TRADES_DEFENSIVE,
        "emergency": _MIN_TRADES_EMERGENCY,
    }
    min_trades = thresholds.get(change_type.lower(), _MIN_TRADES_DEFENSIVE)
    if trade_count < min_trades:
        return f"INSUFFICIENT_DATA ({trade_count}/{min_trades})"
    return "READY"


_CAT_PARAMS: dict[str, dict] = {
    "1a": {"param": "ORB window / SPY lookback", "change_type": "defensive"},
    "1b": {"param": "VIX_STOP_WIDEN_THRESHOLD / Kelly cap", "change_type": "defensive"},
    "2":  {"param": "MIN_SCORE", "change_type": "offensive"},
    "3":  {"param": "BUCKET_A Kelly 0.25→0.20", "change_type": "defensive"},
    "4":  {"param": "TOD entry gate activation", "change_type": "defensive"},
    "5":  {"param": "Earnings lookback 5→7 days", "change_type": "defensive"},
    "6":  {"param": "INTRADAY_STOP_ATR_MULT 1.25→1.5+", "change_type": "defensive"},
    "7":  {"param": "Exit signal thresholds / target multiplier", "change_type": "defensive"},
    "8":  {"param": "FORENSIC REVIEW — zero-based (no parameter change)", "change_type": "defensive"},
}

_CAT_TRIGGER_MIN: dict[str, int] = {
    "1a": 4, "1b": 2, "2": 15, "3": 3, "4": 12, "5": 4, "6": 3, "7": 20, "8": 0,
}


def _generate_hypotheses(
    cat_stats: dict[str, dict],
    total_loss: float,
) -> list[dict]:
    """Generate parameter change hypotheses from category stats.
    Only proposes when trigger threshold met AND impact >= 40%."""
    hypotheses: list[dict] = []
    for cat, s in cat_stats.items():
        if cat == "8":
            continue
        n = s["n_primary"]
        cat_loss = abs(s["total_loss"])
        impact_pct = cat_loss / abs(total_loss) if total_loss != 0 else 0.0
        trigger_min = _CAT_TRIGGER_MIN.get(cat, 999)
        meta = _CAT_PARAMS.get(cat, {})
        gate = _check_hypothesis_gate(cat, n, meta.get("change_type", "defensive"))

        if n >= trigger_min and impact_pct >= _HYP_MIN_IMPACT_PCT and gate == "READY":
            hypotheses.append({
                "category": cat,
                "parameter": meta.get("param", "?"),
                "change_type": meta.get("change_type", "?"),
                "n_trades": n,
                "estimated_loss_recovery": round(cat_loss, 2),
                "impact_pct": round(impact_pct * 100, 1),
                "p_value": s["p_value"],
                "significant": s["significant"],
                "gate_status": gate,
                "board_vote_required": True,
                "min_data_met": True,
            })

    return hypotheses


# ── Monthly mislabeling gate ──────────────────────────────────────────────────

def _is_first_friday_of_month() -> bool:
    now = datetime.now(_ET)
    if now.weekday() != 4:  # Friday
        return False
    return now.day <= 7


def _monthly_mislabeling_check(trades: list[dict]) -> dict:
    """Check if classifier results look internally consistent.
    Flags if category 8 (Unknown) > 30% of losers or total losers < MIN_TRADES_MONTHLY."""
    losers = [t for t in trades if t["is_loser"] and t["status"] == "closed"]
    n_losers = len(losers)
    if n_losers < _MIN_TRADES_MONTHLY:
        return {"status": "INSUFFICIENT_DATA", "n_losers": n_losers, "min": _MIN_TRADES_MONTHLY, "alert": False}

    cat8_losers = [t for t in losers if t.get("classification") and t["classification"]["primary"] == "8"]
    cat8_pct = len(cat8_losers) / n_losers if n_losers > 0 else 0.0

    alert = cat8_pct > 0.10
    return {
        "status": "MISLABELING_ALERT" if alert else "OK",
        "n_losers": n_losers,
        "cat8_count": len(cat8_losers),
        "cat8_pct": round(cat8_pct * 100, 1),
        "alert": alert,
        "action": "Pause hypothesis generation 2 weeks — post CRITICAL Slack, request operator review" if alert else "None",
    }


# ── Emergency escalation ─────────────────────────────────────────────────────

def _check_emergency_escalation(equity_start: float, equity_end: float) -> dict:
    """Check if weekly drawdown > 25%."""
    if equity_start <= 0:
        return {"triggered": False, "drawdown_pct": 0}
    drawdown = (equity_end - equity_start) / equity_start
    return {
        "triggered": drawdown <= -_EMERGENCY_DRAWDOWN_PCT,
        "drawdown_pct": round(drawdown * 100, 2),
        "threshold_pct": -_EMERGENCY_DRAWDOWN_PCT * 100,
    }


# ── HTML tearsheet ────────────────────────────────────────────────────────────

_CAT_NAMES = {
    "1a": "Directional Macro Headwind",
    "1b": "Volatility Regime Sizing Error",
    "2":  "Marginal Score Low-Momentum",
    "3":  "Leveraged Sizing Error PDT-triggered",
    "4":  "Time-of-Day Bleed",
    "5":  "Earnings Risk",
    "6":  "VIX Stop Crush",
    "7":  "Holding Period Mismatch",
    "8":  "Unknown / Doesn't Fit",
}


def _generate_html(
    week_label_str: str,
    trades: list[dict],
    cat_stats: dict[str, dict],
    hypotheses: list[dict],
    mislabeling: dict,
    emergency: dict,
    generated_at: str,
) -> str:
    closed = [t for t in trades if t["status"] == "closed"]
    winners = [t for t in closed if t["is_winner"]]
    losers = [t for t in closed if t["is_loser"]]
    total_pnl = sum(t["pnl_gross"] for t in closed)
    total_win_pnl = sum(t["pnl_gross"] for t in winners)
    total_loss_pnl = sum(t["pnl_gross"] for t in losers)
    wr = len(winners) / len(closed) * 100 if closed else 0
    avg_win = total_win_pnl / len(winners) if winners else 0
    avg_loss = total_loss_pnl / len(losers) if losers else 0
    rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    pnl_color = "#2ecc71" if total_pnl >= 0 else "#e74c3c"
    emergency_banner = ""
    if emergency.get("triggered"):
        emergency_banner = f'<div class="emergency-banner">🚨 EMERGENCY ESCALATION: Weekly drawdown {emergency["drawdown_pct"]:.1f}% exceeds {abs(emergency["threshold_pct"]):.0f}% threshold. HALT NEW ENTRIES.</div>'

    mislabeling_banner = ""
    if mislabeling.get("alert"):
        mislabeling_banner = f'<div class="warning-banner">⚠️ MISLABELING ALERT: {mislabeling["cat8_pct"]:.1f}% of losses classified as Unknown (>10% threshold). Hypothesis generation paused.</div>'

    # Trade rows
    trade_rows = ""
    for t in sorted(closed, key=lambda x: x["entry_ts"]):
        cl = t.get("classification") or {}
        pnl_cls = "win" if t["is_winner"] else "loss"
        cat_label = f"{cl.get('primary', '?')} — {_CAT_NAMES.get(cl.get('primary', ''), '')}" if cl else "—"
        trade_rows += f"""
        <tr class="{pnl_cls}">
            <td>{t["symbol"]}</td>
            <td>{t["direction"].upper()}</td>
            <td>{t["entry_ts_pt"]}</td>
            <td>${t["entry_price"]:.2f}</td>
            <td>${t["exit_price"]:.2f}</td>
            <td>{t["hold_mins"]:.0f}m</td>
            <td>{t["score_12pt"]}/12</td>
            <td>{t["mri_level"] or "—"}</td>
            <td style="color:{('#2ecc71' if t['is_winner'] else '#e74c3c')}">${t["pnl_gross"]:+.2f}</td>
            <td>{cat_label}</td>
        </tr>"""

    # Category breakdown rows
    cat_rows = ""
    for cat_id in ["1a", "1b", "2", "3", "4", "5", "6", "7", "8"]:
        s = cat_stats.get(cat_id, {})
        sig = "✓ p<0.20" if s.get("significant") else "ns"
        cat_rows += f"""
        <tr>
            <td><b>{cat_id}</b></td>
            <td>{_CAT_NAMES.get(cat_id, '')}</td>
            <td>{s.get('n_primary', 0)}</td>
            <td style="color:#e74c3c">${s.get('total_loss', 0):.2f}</td>
            <td>{s.get('win_rate', 0)*100:.1f}%</td>
            <td>{s.get('baseline_wr', 0)*100:.1f}%</td>
            <td>{sig}</td>
        </tr>"""

    # Hypothesis rows
    hyp_rows = ""
    for h in hypotheses:
        hyp_rows += f"""
        <tr>
            <td>Cat {h["category"]} — {_CAT_NAMES.get(h["category"], "")}</td>
            <td>{h["parameter"]}</td>
            <td>${h["estimated_loss_recovery"]:.2f} ({h["impact_pct"]:.0f}%)</td>
            <td>p={h["p_value"]:.3f} {'✓' if h["significant"] else 'ns'}</td>
            <td>{h["change_type"].title()}</td>
            <td>{'READY — Board vote required' if h["gate_status"] == "READY" else h["gate_status"]}</td>
        </tr>"""
    if not hyp_rows:
        hyp_rows = '<tr><td colspan="6" style="color:#888">No hypotheses met the 40% impact + MIN_TRADES threshold this week.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Weekly Performance Audit — {week_label_str}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }}
  h1, h2, h3 {{ color: #e6edf3; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; margin: 16px 0; }}
  .metric {{ display: inline-block; margin: 0 24px 16px 0; }}
  .metric-value {{ font-size: 2em; font-weight: 700; }}
  .metric-label {{ font-size: 0.85em; color: #8b949e; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9em; }}
  th {{ background: #21262d; color: #8b949e; padding: 8px 12px; text-align: left; border-bottom: 1px solid #30363d; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #21262d; }}
  tr.win td {{ color: #2ecc71; }}
  tr.loss td {{ color: #e74c3c; }}
  .emergency-banner {{ background: #5a0000; border: 1px solid #e74c3c; border-radius: 6px; padding: 14px 20px; margin-bottom: 20px; font-weight: bold; color: #ff8080; }}
  .warning-banner {{ background: #3a2a00; border: 1px solid #f39c12; border-radius: 6px; padding: 12px 20px; margin-bottom: 20px; color: #f5c842; }}
  .footer {{ color: #484f58; font-size: 0.8em; margin-top: 32px; }}
</style>
</head>
<body>
<h1>📊 Weekly Performance Audit — {week_label_str}</h1>
<p style="color:#8b949e">Generated: {generated_at}</p>
{emergency_banner}
{mislabeling_banner}

<div class="card">
  <h2>Executive Summary</h2>
  <div class="metric"><div class="metric-value" style="color:{pnl_color}">${total_pnl:+.2f}</div><div class="metric-label">Week P&amp;L</div></div>
  <div class="metric"><div class="metric-value">{wr:.1f}%</div><div class="metric-label">Win Rate ({len(winners)}/{len(closed)})</div></div>
  <div class="metric"><div class="metric-value" style="color:#2ecc71">${avg_win:.2f}</div><div class="metric-label">Avg Win</div></div>
  <div class="metric"><div class="metric-value" style="color:#e74c3c">${avg_loss:.2f}</div><div class="metric-label">Avg Loss</div></div>
  <div class="metric"><div class="metric-value">{rr:.2f}</div><div class="metric-label">Realized R:R</div></div>
</div>

<div class="card">
  <h2>All Trades ({len(closed)})</h2>
  <table>
    <thead><tr><th>Symbol</th><th>Dir</th><th>Entry (PT)</th><th>Entry $</th><th>Exit $</th><th>Hold</th><th>Score</th><th>MRI</th><th>P&amp;L</th><th>Failure Category</th></tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>Failure Category Breakdown</h2>
  <table>
    <thead><tr><th>ID</th><th>Category</th><th>Primary Count</th><th>Total Loss</th><th>Win Rate</th><th>Baseline WR</th><th>Significance</th></tr></thead>
    <tbody>{cat_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>Board Hypotheses ({len(hypotheses)} active)</h2>
  <table>
    <thead><tr><th>Category</th><th>Parameter</th><th>Est. Recovery</th><th>Stat. Sig.</th><th>Change Type</th><th>Gate Status</th></tr></thead>
    <tbody>{hyp_rows}</tbody>
  </table>
</div>

<div class="footer">
  alpaca-mtf-bot weekly_perf_audit.py | {generated_at} | Standalone audit script — not in RTH chain
</div>
</body>
</html>"""


# ── Slack output ──────────────────────────────────────────────────────────────

def _post_slack(text: str) -> bool:
    """Post plain-text summary to Slack webhook. Non-blocking on failure."""
    webhook = os.getenv("SLACK_WEBHOOK_URL", "")
    if not webhook:
        logger.info("_post_slack: SLACK_WEBHOOK_URL not set — skipping")
        return False
    try:
        resp = requests.post(webhook, json={"text": text}, timeout=5)
        if resp.status_code == 200:
            logger.info("_post_slack: posted OK")
            return True
        logger.warning("_post_slack: HTTP %d", resp.status_code)
        return False
    except Exception as exc:
        logger.warning("_post_slack: failed: %s", exc)
        return False


def _build_slack_summary(
    week_label_str: str,
    trades: list[dict],
    cat_stats: dict[str, dict],
    hypotheses: list[dict],
    mislabeling: dict,
    emergency: dict,
    ds_gai_prompt: str,
) -> str:
    closed = [t for t in trades if t["status"] == "closed"]
    winners = [t for t in closed if t["is_winner"]]
    losers = [t for t in closed if t["is_loser"]]
    total_pnl = sum(t["pnl_gross"] for t in closed)
    wr = len(winners) / len(closed) * 100 if closed else 0
    total_loss = sum(t["pnl_gross"] for t in losers)

    top_cats = sorted(
        [(k, v) for k, v in cat_stats.items() if v["n_primary"] > 0],
        key=lambda x: abs(x[1]["total_loss"]),
        reverse=True,
    )[:2]

    em_flag = "🚨 EMERGENCY " if emergency.get("triggered") else ""
    ml_flag = "\n⚠️ MISLABELING ALERT: Hypothesis generation paused." if mislabeling.get("alert") else ""

    lines = [
        f"📊 {em_flag}WEEKLY AUDIT — {week_label_str}",
        f"P&L: {'+'if total_pnl >= 0 else ''}{total_pnl:.2f} | Win Rate: {len(winners)}/{len(closed)} ({wr:.1f}%)",
        f"Avg Win: ${(sum(t['pnl_gross'] for t in winners)/len(winners) if winners else 0):.2f} | Avg Loss: ${(total_loss/len(losers) if losers else 0):.2f}",
        "",
        "TOP FAILURE CATEGORIES:",
    ]
    for rank, (cat_id, s) in enumerate(top_cats, 1):
        lines.append(f"  {rank}. Category {cat_id} ({_CAT_NAMES.get(cat_id, '')}): {s['n_primary']}× | ${s['total_loss']:.2f}")

    if hypotheses:
        lines += ["", "BOARD HYPOTHESES:"]
        for h in hypotheses[:3]:
            lines.append(f"  H: {h['parameter']} — est. recovery ${h['estimated_loss_recovery']:.2f} ({h['impact_pct']:.0f}% of losses)")
    else:
        lines += ["", "BOARD HYPOTHESES: None met threshold this week."]

    lines.append(ml_flag)
    lines += [
        "",
        f"STATUS: {'🔴 EMERGENCY — HALT ENTRIES' if emergency.get('triggered') else '🟢 DS/GAI REVIEW REQUIRED'}",
        "",
        "══════ DS/GAI PROMPT (copy to DeepSeek + Google AI Studio) ══════",
        ds_gai_prompt,
    ]
    return "\n".join(lines)


def _build_ds_gai_prompt(
    week_label_str: str,
    trades: list[dict],
    cat_stats: dict[str, dict],
    hypotheses: list[dict],
) -> str:
    """Build the weekly DS/GAI prompt. Delivered via Slack only, never saved to .md."""
    closed = [t for t in trades if t["status"] == "closed"]
    losers = [t for t in closed if t["is_loser"]]
    winners = [t for t in closed if t["is_winner"]]
    total_pnl = sum(t["pnl_gross"] for t in closed)
    wr = len(winners) / len(closed) * 100 if closed else 0
    avg_win = sum(t["pnl_gross"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl_gross"] for t in losers) / len(losers) if losers else 0

    loser_block = ""
    for t in losers:
        cl = t.get("classification") or {}
        loser_block += (
            f"\n{t['symbol']} {t['direction'].upper()} {t['entry_ts_pt']} → {t.get('exit_ts', '')[:10]}\n"
            f"  P&L: ${t['pnl_gross']:+.2f} ({t['pnl_pct']:.2f}%) | Score: {t['score_12pt']}/12 | MRI: {t['mri_level'] or '—'}\n"
            f"  Hold: {t['hold_mins']:.0f}min | VIX z={cl.get('vix_zscore', '?')} | Regime: {cl.get('vix_regime', '?')}\n"
            f"  Exit reason: {t.get('exit_reason', '—')}\n"
            f"  PRIMARY: {cl.get('primary', '?')} — {_CAT_NAMES.get(cl.get('primary', ''), '')}\n"
            f"  SECONDARY: {', '.join(cl.get('secondary', [])) or 'None'}\n"
        )

    hyp_block = ""
    for h in hypotheses:
        hyp_block += f"  H: {h['parameter']} — est. ${h['estimated_loss_recovery']:.2f} recovery ({h['impact_pct']:.0f}% of losses) | p={h['p_value']:.3f}\n"
    if not hyp_block:
        hyp_block = "  None met threshold this week.\n"

    cat_block = ""
    for cat_id, s in sorted(cat_stats.items(), key=lambda x: abs(x[1]["total_loss"]), reverse=True):
        if s["n_primary"] > 0:
            cat_block += f"  {cat_id} ({_CAT_NAMES.get(cat_id, '')}): {s['n_primary']} primary | loss ${s['total_loss']:.2f} | WR {s['win_rate']*100:.1f}% vs baseline {s['baseline_wr']*100:.1f}% | p={s['p_value']:.3f}\n"

    return f"""WEEKLY PERFORMANCE AUDIT — alpaca-mtf-bot
Week: {week_label_str}
Audit generated: {_fmt_pt(datetime.now(_PT))}

═══════ ACCOUNT SUMMARY ═══════
Total trades: {len(closed)} ({len(winners)} wins, {len(losers)} losses)
Week P&L: ${total_pnl:+.2f} | Win rate: {wr:.1f}%
Avg win: ${avg_win:.2f} | Avg loss: ${avg_loss:.2f}
R:R: {(abs(avg_win/avg_loss) if avg_loss != 0 else 0.0):.2f}

═══════ FAILURE CATEGORY BREAKDOWN ═══════
{cat_block}
═══════ LOSING TRADES ═══════
{loser_block}
═══════ BOARD HYPOTHESES ═══════
{hyp_block}
═══════ YOUR ANALYSIS QUESTIONS ═══════
Q1-Q8: [See full design spec — using standard template]
Please answer all 8 questions. Same prompt sent to DeepSeek AND Google AI Studio independently.
"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    logger.info("=" * 60)
    logger.info("  WEEKLY PERFORMANCE AUDIT")
    logger.info("  %s | Generated: %s", _week_label(), _fmt_pt(datetime.now(_PT)))
    logger.info("=" * 60)

    # ── Fetch data ────────────────────────────────────────────────────────────
    fills = _fetch_fills(days_back=_DAYS_BACK)
    events = _load_trade_events(days_back=_DAYS_BACK)

    # Get all unique symbols traded
    all_syms = list({f.get("symbol", "") for f in fills if f.get("symbol")})
    if "SPY" not in all_syms:
        all_syms.append("SPY")

    daily_bars = _fetch_daily_bars(all_syms, days_back=14)
    vix_records = _fetch_vix_history(days_back=25)
    vix_lookup = _build_vix_lookup(vix_records)
    earnings_data = _fetch_earnings_calendar([s for s in all_syms if s != "SPY"])

    # ── Circuit breaker ───────────────────────────────────────────────────────
    if not fills and not events:
        logger.critical("CIRCUIT BREAKER: fills API AND trade_events.jsonl both unavailable. Exiting.")
        return 1

    # ── Reconstruct trades ────────────────────────────────────────────────────
    spy_bars = daily_bars.get("SPY", [])
    trades = _reconstruct_trades(fills, events)

    if not trades:
        logger.warning("No trades reconstructed this week. Generating empty report.")

    # ── Classify each trade ───────────────────────────────────────────────────
    min_score = int(os.getenv("MIN_SCORE", "10"))
    for t in trades:
        t["classification"] = _classify_trade(t, vix_lookup, spy_bars, earnings_data, min_score=min_score)

    # ── Stats + hypotheses ────────────────────────────────────────────────────
    cat_stats = _compute_category_stats(trades)

    closed_trades = [t for t in trades if t["status"] == "closed"]
    total_loss = sum(t["pnl_gross"] for t in closed_trades if t["is_loser"])
    hypotheses = _generate_hypotheses(cat_stats, total_loss)
    logger.info("Hypotheses generated: %d", len(hypotheses))

    # ── Monthly mislabeling gate ──────────────────────────────────────────────
    mislabeling: dict = {"status": "SKIPPED", "alert": False}
    if _is_first_friday_of_month():
        logger.info("First Friday of month — running mislabeling gate")
        mislabeling = _monthly_mislabeling_check(trades)
        if mislabeling.get("alert"):
            logger.critical("MISLABELING ALERT: %s", mislabeling)

    # ── Emergency escalation ──────────────────────────────────────────────────
    # Approximate equity from fills (net P&L over the week)
    week_pnl = sum(t["pnl_gross"] for t in closed_trades)
    # equity_start approximated from env or default; real equity from Alpaca account would be better
    equity_estimate = float(os.getenv("PORTFOLIO_VALUE", "2852"))
    equity_start = equity_estimate - week_pnl
    emergency = _check_emergency_escalation(equity_start, equity_estimate)
    if emergency["triggered"]:
        logger.critical("EMERGENCY ESCALATION: weekly drawdown %s%%", emergency["drawdown_pct"])

    # ── Generate outputs ──────────────────────────────────────────────────────
    generated_at = _fmt_pt(datetime.now(_PT))
    week_str = _week_label()

    html_path = _LOG_DIR / f"weekly_perf_audit_{week_str}.html"
    ds_gai_prompt = _build_ds_gai_prompt(week_str, trades, cat_stats, hypotheses)
    html_content = _generate_html(week_str, trades, cat_stats, hypotheses, mislabeling, emergency, generated_at)
    _atomic_write(html_path, html_content)
    logger.info("HTML tearsheet written: %s", html_path)

    # ── Save cache ────────────────────────────────────────────────────────────
    cache_path = _LOG_DIR / f"weekly_audit_cache_{week_str}.json"
    cache_data = {
        "week": week_str,
        "generated_at": generated_at,
        "n_trades": len(trades),
        "n_fills": len(fills),
        "n_events": len(events),
        "total_pnl": week_pnl,
        "cat_stats": cat_stats,
        "hypotheses": hypotheses,
        "mislabeling": mislabeling,
        "emergency": emergency,
    }
    try:
        _atomic_write(cache_path, json.dumps(cache_data, indent=2, default=str))
        logger.info("Cache written: %s", cache_path)
    except Exception as exc:
        logger.warning("Cache write failed: %s", exc)

    # ── Slack ─────────────────────────────────────────────────────────────────
    slack_text = _build_slack_summary(week_str, trades, cat_stats, hypotheses, mislabeling, emergency, ds_gai_prompt)
    _post_slack(slack_text)

    logger.info("weekly_perf_audit complete. HTML: %s | Hypotheses: %d", html_path.name, len(hypotheses))
    return 0


if __name__ == "__main__":
    sys.exit(main())
