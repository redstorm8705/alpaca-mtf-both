# ruff: noqa: E501  — pre-existing long-line debt (RULE C-4 option A, task_d2d4c1f5); fix as own sequence
"""
options_scanner.py
Options chain scanner and recommendation engine for manual execution.

Reads MTF confluence signals, fetches live options chains via Public.com (real-time Greeks)
with yfinance fallback, selects optimal strikes using Black-Scholes delta, and generates
options.html with two recommendation sets: Weekly and 0DTE.

STRUCTURES SUPPORTED:
  Long call  — bullish, low VRP: pay for premium, need the underlying to move up
  Long put   — bearish, low VRP: pay for premium, need the underlying to move down
  Short call — bearish, high VRP: collect premium; stock stays flat/down
  Short put  — bullish, high VRP: collect premium; stock holds or rises

VRP REGIME (Vilkov 2026, POST-2022-ERA):
  VRP = ATM IV − 20-day realized vol (both annualized %). Positive = options expensive.
  VRP > 5 vol pts → sell opposite direction (short put if bullish, short call if bearish)
  VRP < 5 vol pts → buy direction (long call if bullish, long put if bearish)
  Tertile cutoffs use post-2022 data only — pre-2022 0DTE dynamics are a different regime.

TWO RECOMMENDATION SETS:
  Weekly: next-Friday expiry. Entry windows 10:00–11:30 / 14:00–15:00 ET.
          Max $150/trade, 2 contracts. Target +100% (long) / 75% profit (short).
  0DTE:   DIRECTIONAL intraday-swing capture (reframed 2026-07-13 — NOT premium selling).
          Long OTM ~0.35Δ CALL (upside) + PUT (downside) per name so the operator plays the
          day either way. Same-day expiry, 10:05–10:20 ET entry, 3:45 ET hard close.
          Max $75/trade, 1 contract, +100% target. Runs in all VIX regimes (block removed 2026-07-25).
          Universe: SPY, QQQ + Mag 7 (AAPL/MSFT/GOOGL/AMZN/NVDA/META/TSLA).

Weekly universe: SPY, QQQ, AAPL, MSFT, AMZN (core) + META, GOOGL, AMD (conditional, ≤ $3)
Account: $500

Usage:
    python3.10 options_scanner.py          # run once → options.html
    python3.10 options_scanner.py --watch  # refresh every 15 min during market hours

Cron (every 15 min, 9:30 AM – 4:00 PM ET weekdays):
    */15 9-16 * * 1-5  cd /path/to/bot && /usr/local/bin/python3.10 options_scanner.py >> logs/options_scan.log 2>&1
"""

import os
import sys
import json
import time
import hashlib
import logging
import argparse
import subprocess
import numpy as np
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("options_scanner")

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR          = Path(__file__).parent
LOGS_DIR          = BASE_DIR / "logs"
OPTIONS_SCAN_JSON = LOGS_DIR / "options_scan.json"
ROBINHOOD_TRADES  = LOGS_DIR / "robinhood_trades.json"
OPTIONS_HTML      = BASE_DIR / "options.html"
_VRP_CACHE_PATH   = BASE_DIR / "data" / "cache" / "vrp_cache.json"  # VIX tertile + VRP cache
_DTE_LOCK_PATH    = BASE_DIR / "data" / "cache" / "dte_direction_lock.json"  # daily 0DTE direction lock
# S1 — forward-only rec retention (GEX/0DTE accuracy evaluator, design 2026-07-19 Finding 4).
# options_scan.json is os.replace-overwritten every 15 min, destroying the rec/rejection history
# the accuracy evaluator needs. These durable JSONL logs are APPENDED (never overwritten) before
# that replace, so recommendations AND their rejected-candidate denominator survive for scoring.
# Both weekly and 0DTE recs are captured (each carries its own `mode` field for filtering).
RECS_HISTORY_JSONL       = LOGS_DIR / "options_recs_history.jsonl"        # every emitted rec (weekly + 0DTE)
REJECTIONS_HISTORY_JSONL = LOGS_DIR / "options_rejections_history.jsonl"  # the denominator (why a candidate was NOT recommended)

# S1 config surface — the threshold/universe knobs that GOVERN which options get recommended.
# The accuracy evaluator resets its evidence clock when this hash changes (design S4b tripwire):
# tuning any of these is a new experiment, so recs logged under a different hash are a different
# population and must not be pooled. Referenced by name via globals() so a missing/renamed knob
# degrades to None rather than raising.
_CONFIG_SURFACE_KEYS = (
    "MIN_SCORE_ANY", "MIN_SCORE_HIGH", "MAX_TRADE_DOLLARS", "MAX_TRADE_DOLLARS_0DTE",
    "LIMIT_SELL_MULT", "BAS_MAX_0DTE", "PREMIUM_LIMIT_CONDITIONAL",
    "ZDTE_DELTA_TARGET", "ZDTE_DELTA_MIN", "ZDTE_DELTA_MAX",
    "DELTA_TARGET_HIGH", "DELTA_TARGET_MOD", "DELTA_TARGET_SHORT",
    "CORE_UNIVERSE", "CONDITIONAL_UNIVERSE", "ZDTE_UNIVERSE",
)


def _code_version() -> str:
    """Short git SHA of the deployed scanner, or 'nogit' if unavailable. Never raises."""
    try:
        _sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(BASE_DIR), stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        return _sha or "nogit"
    except Exception:
        return "nogit"


def _config_hash() -> str:
    """Stable 8-char hash of the rec-governing config surface (design S4b tripwire). Never raises."""
    try:
        _surface = {k: globals().get(k) for k in _CONFIG_SURFACE_KEYS}
        return hashlib.sha256(
            json.dumps(_surface, sort_keys=True, default=str).encode()
        ).hexdigest()[:8]
    except Exception:
        return "nohash"


def _null_greeks(rec: dict) -> dict:
    """Return a shallow COPY with the '—' string sentinel (missing gamma/vega on the yfinance
    path) replaced by None, so downstream isna()/float parsing works. Never mutates the live rec."""
    out = dict(rec)
    for _k in ("gamma", "vega"):
        if out.get(_k) == "—":
            out[_k] = None
    return out


def _persist_rec_history(weekly_recs: list, dte_recs: list, rejections: list, scan_now) -> None:
    """S1 — append every rec (weekly + 0DTE) and every rejection to durable JSONL logs BEFORE
    options_scan.json is os.replace-overwritten (which destroys them every 15 min). Each row is
    stamped with a rec_id, scan_time, code_version (git SHA) and config_hash — the provenance
    fields whose absence made the 2026-07-13..15 flip incident undiagnosable. Forward-only:
    appends, never reconstructs. NEVER RAISES — a logging failure must not break a scan."""
    try:
        _cv, _ch = _code_version(), _config_hash()
        _iso  = scan_now.isoformat()
        _base = scan_now.strftime("%Y%m%dT%H%M%S")
        _rows = []
        for _i, _rec in enumerate(list(weekly_recs) + list(dte_recs)):
            _row = _null_greeks(_rec)
            _row["rec_id"]       = f"{_base}-{_row.get('mode', '?')}-{_row.get('symbol', '?')}-{_row.get('direction', '?')}-{_i}"
            _row["scan_time"]    = _iso
            _row["code_version"] = _cv
            _row["config_hash"]  = _ch
            _rows.append(_row)
        if _rows:
            with open(RECS_HISTORY_JSONL, "a") as _rf:
                for _row in _rows:
                    _rf.write(json.dumps(_row, default=str) + "\n")
                _rf.flush()
                os.fsync(_rf.fileno())
        if rejections:
            with open(REJECTIONS_HISTORY_JSONL, "a") as _jf:
                for _j, _rej in enumerate(rejections):
                    _rr = dict(_rej)
                    _rr["rec_id"]       = f"{_base}-rej-{_rr.get('mode', 'weekly')}-{_rr.get('symbol', '?')}-{_j}"
                    _rr["scan_time"]    = _iso
                    _rr["code_version"] = _cv
                    _rr["config_hash"]  = _ch
                    _jf.write(json.dumps(_rr, default=str) + "\n")
                _jf.flush()
                os.fsync(_jf.fileno())
        logger.info("S1: persisted %d recs + %d rejections to history (cv=%s cfg=%s)",
                    len(_rows), len(rejections), _cv, _ch)
    except Exception as _e:
        logger.warning("S1 rec-history persist failed (non-fatal): %s", _e)


ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# ── Universe ─────────────────────────────────────────────────────────────────
CORE_UNIVERSE        = ["SPY", "QQQ", "AAPL", "MSFT", "AMZN"]
CONDITIONAL_UNIVERSE = ["META", "GOOGL", "AMD"]   # only when ATM premium ≤ $3.00
PREMIUM_LIMIT_CONDITIONAL = 3.00

# ── 0DTE universe — DIRECTIONAL intraday-swing capture (Rafael 2026-07-13) ─────
# 0DTE is NO LONGER premium-selling. It is directional capture of the same-day move
# EITHER WAY — a long OTM call (upside) AND a long OTM put (downside) per name, so the
# operator plays whichever direction the day develops. Universe: SPY, QQQ + the Mag 7.
ZDTE_UNIVERSE      = ["SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
ZDTE_DELTA_TARGET  = 0.35   # OTM strike delta target (capture the swing, not lottery/ATM)
ZDTE_DELTA_MIN     = 0.20   # too deep OTM → illiquid lottery
ZDTE_DELTA_MAX     = 0.48   # too close to ATM → overpaying theta on a 0DTE

# ── Signal thresholds ────────────────────────────────────────────────────────
MIN_SCORE_ANY  = 8    # below this → watchlist only
MIN_SCORE_HIGH = 10   # high-conviction

# ── Sizing — weekly ───────────────────────────────────────────────────────────
ACCOUNT_SIZE      = 500
MAX_TRADE_DOLLARS = 150   # 30% of account
LIMIT_SELL_MULT   = 2.0   # target +100% on long premium
SHORT_BUY_BACK    = 0.25  # buy back short at 25% of credit received (75% profit)
RISK_FREE_RATE    = 0.045

# ── Sizing — 0DTE ─────────────────────────────────────────────────────────────
MAX_TRADE_DOLLARS_0DTE = 75   # halved vs weekly — binary outcome risk

# ── Delta targets — weekly ────────────────────────────────────────────────────
DELTA_TARGET_HIGH  = 0.40   # long, HIGH conviction
DELTA_TARGET_MOD   = 0.30   # long, MOD conviction
DELTA_TARGET_SHORT = 0.20   # short (OTM — want to expire worthless)

# ── Delta targets — 0DTE ──────────────────────────────────────────────────────
DELTA_TARGET_HIGH_0DTE  = 0.30   # long 0DTE HIGH (slightly OTM — 0DTE gamma is severe)
DELTA_TARGET_MOD_0DTE   = 0.25   # long 0DTE MOD
DELTA_TARGET_SHORT_0DTE = 0.12   # short 0DTE (very OTM — pin risk)

# ── Entry windows (ET) ────────────────────────────────────────────────────────
ENTRY_WINDOWS      = [(10, 0, 11, 30), (14, 0, 15, 0)]  # weekly
ENTRY_WINDOWS_0DTE = [(10, 5, 10, 20)]                   # 0DTE: opening vol premium window only

# ── VRP / Vol regime thresholds (Vilkov 2026 / POST-2022-ERA) ─────────────────
# VRP = ATM_IV − RV_20d (both annualized %). Positive = options priced above realized vol.
# HIGH VRP (IV >> RV) → options expensive → sell premium (collect the overpricing).
# LOW VRP (IV ≤ RV)   → options cheap or fair → buy direction.
VRP_HIGH_THRESHOLD = 5.0   # IV > RV + 5 vol pts → flip to sell-premium vehicle
VRP_LOW_THRESHOLD  = 2.0   # IV < RV + 2 vol pts → insufficient edge; default long

# ── Bid-ask spread gates ──────────────────────────────────────────────────────
BAS_MAX_LONG  = 0.40   # (ask−bid)/mid > 40% → skip long entry
BAS_MAX_SHORT = 0.40   # (ask−bid)/mid > 40% → skip short entry
BAS_MAX_0DTE  = 0.25   # tighter for 0DTE (spreads widen sharply intraday)


# ── Black-Scholes Greeks ─────────────────────────────────────────────────────

def _bs_d1(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    return (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))


def bs_delta(S, K, T, r, sigma, opt_type: str) -> float:
    from scipy.stats import norm
    d1 = _bs_d1(S, K, T, r, sigma)
    return float(norm.cdf(d1) if opt_type == "call" else norm.cdf(d1) - 1)


def bs_theta(S, K, T, r, sigma, opt_type: str) -> float:
    """Daily theta (divide annualized by 365)."""
    from scipy.stats import norm
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = _bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * np.sqrt(T)
    term1 = -(S * norm.pdf(d1) * sigma) / (2 * np.sqrt(T))
    if opt_type == "call":
        return float((term1 - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365)
    else:
        return float((term1 + r * K * np.exp(-r * T) * norm.cdf(-d2)) / 365)


def expected_move(S: float, sigma: float, T: float) -> float:
    """±1σ expected move to expiry."""
    return round(S * sigma * np.sqrt(T), 2)


# ── Expiry logic ─────────────────────────────────────────────────────────────

def _load_blackout_dates() -> set:
    """Pull BLACKOUT dates from events/calendar.py static calendar."""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from events.calendar import STATIC_EVENTS, EventRisk
        return {
            e["date"] for e in STATIC_EVENTS
            if e.get("risk") == EventRisk.BLACKOUT
        }
    except Exception as e:
        logger.warning(f"Calendar load failed: {e} — using empty blackout set")
        return set()


def _load_event_calendar() -> list:
    """Return all events from static calendar for context/warnings."""
    try:
        sys.path.insert(0, str(BASE_DIR))
        from events.calendar import STATIC_EVENTS
        return STATIC_EVENTS
    except Exception:
        return []


def get_next_friday_expiry(from_date: date | None = None) -> str:
    """
    Find the next tradeable Friday expiry.
    - Skips NYSE market holidays (BLACKOUT from calendar.py)
    - If Friday is a holiday, rolls back to Thursday
    - Returns ISO date string e.g. '2026-04-25'
    """
    blackouts = _load_blackout_dates()
    d = from_date or date.today()

    days_ahead = (4 - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    friday = d + timedelta(days=days_ahead)

    friday_str = friday.isoformat()
    if friday_str in blackouts:
        thursday = friday - timedelta(days=1)
        thursday_str = thursday.isoformat()
        logger.info(f"Friday {friday_str} is a market holiday — rolling to Thursday {thursday_str}")
        return thursday_str

    return friday_str


def get_week_events(expiry_str: str) -> list:
    """Return events between today and expiry that affect volatility."""
    cal = _load_event_calendar()
    today_str = date.today().isoformat()
    vol_events = []
    for e in cal:
        if today_str <= e.get("date", "") <= expiry_str:
            risk_val = str(e.get("risk", "")).lower()
            if any(k in risk_val for k in ("high_risk", "caution", "opportunity", "quad", "fomc", "cpi")):
                vol_events.append(e)
    return vol_events


# ── Signal scoring ────────────────────────────────────────────────────────────

def _score_symbol(symbol: str) -> dict:
    """Run MTF confluence scoring for one symbol. Returns result dict."""
    try:
        from data.fetcher import fetch_multi_timeframe
        from strategy.confluence import prepare_df, score_long_signal, score_short_signal
        import config

        raw = fetch_multi_timeframe(symbol, mode="intraday")
        if not raw:
            return {"symbol": symbol, "error": "no_data"}

        tf = {k: prepare_df(v) for k, v in raw.items()}
        lr = score_long_signal(symbol, tf, config.TradeMode.INTRADAY)
        sr = score_short_signal(symbol, tf, config.TradeMode.INTRADAY)

        intra = tf.get(config.TF_15M)
        daily = tf.get(config.TF_DAILY)
        price = None
        pct_change = None
        atr = None
        rv_20d = None   # 20-day annualized realized vol (%) — used for VRP computation

        if intra is not None and not intra.empty:
            price = float(intra["close"].iloc[-1])
        elif daily is not None and not daily.empty:
            price = float(daily["close"].iloc[-1])

        if daily is not None and len(daily) >= 2:
            prev = float(daily["close"].iloc[-2])
            curr = float(daily["close"].iloc[-1])
            pct_change = round((curr - prev) / prev * 100, 2)
            try:
                from indicators.atr import calc_atr
                atr = calc_atr(daily)
            except Exception:
                pass

        # POST-2022-ERA: compute 20-day realized vol for VRP gate (Vilkov 2026)
        if daily is not None and len(daily) >= 21:
            try:
                log_rets = np.log(
                    daily["close"].values[1:] / daily["close"].values[:-1]
                )
                rv_20d = round(float(np.std(log_rets[-20:]) * np.sqrt(252) * 100), 2)
            except Exception:
                pass

        return {
            "symbol":       symbol,
            "long_score":   int(lr["score"]),
            "short_score":  int(sr["score"]),
            "long_signal":  bool(lr["signal"]),
            "short_signal": bool(sr["signal"]),
            "daily_bias":   str(lr.get("bias", "unknown")),
            "price":        price,
            "pct_change":   pct_change,
            "atr":          round(atr, 2) if atr else None,
            "rv_20d":       rv_20d,
            "error":        None,
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}


def score_universe(symbols: list, timeout: int = 25) -> list:
    """Score all symbols with per-symbol timeout. Returns list of result dicts."""
    results = []
    for sym in symbols:
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(_score_symbol, sym)
                result = fut.result(timeout=timeout)
        except FuturesTimeout:
            result = {"symbol": sym, "error": f"score timeout ({timeout}s)"}
        except Exception as e:
            result = {"symbol": sym, "error": str(e)}
        results.append(result)
        time.sleep(0.3)
    return results


# ── Public.com API client ─────────────────────────────────────────────────────

_PUBLIC_BASE    = "https://api.public.com"
_PUBLIC_SECRET  = os.getenv("PUBLIC_COM_SECRET", "")
_PUBLIC_ACCT_ID = os.getenv("PUBLIC_COM_ACCOUNT_ID", "")
_public_token: dict = {"token": None, "expires_at": 0}


def _get_public_token() -> str | None:
    """Fetch or reuse a short-lived Public.com JWT. Returns None if unavailable."""
    if not _PUBLIC_SECRET or not _PUBLIC_ACCT_ID:
        return None
    now = time.time()
    if _public_token["token"] and now < _public_token["expires_at"] - 30:
        return _public_token["token"]
    try:
        import requests
        resp = requests.post(
            f"{_PUBLIC_BASE}/userapiauthservice/personal/access-tokens",
            json={"secret": _PUBLIC_SECRET, "accountId": _PUBLIC_ACCT_ID},
            timeout=8,
        )
        resp.raise_for_status()
        token = resp.json()["accessToken"]
        _public_token["token"]      = token
        _public_token["expires_at"] = now + 3600
        logger.debug("Public.com token refreshed")
        return token
    except Exception as e:
        logger.warning(f"Public.com auth failed: {e}")
        return None


def _fetch_chain_public(symbol: str, expiry: str, opt_type: str) -> list | None:
    """
    Fetch live options chain from Public.com (one side).
    Kept for backward compatibility — new code uses _fetch_both_chains().
    Returns list of row dicts or None on failure.
    """
    token = _get_public_token()
    if not token:
        return None
    try:
        import requests
        headers = {"Authorization": f"Bearer {token}"}
        resp = requests.post(
            f"{_PUBLIC_BASE}/userapigateway/marketdata/{_PUBLIC_ACCT_ID}/option-chain",
            headers=headers,
            json={"instrument": {"symbol": symbol, "type": "EQUITY"}, "expirationDate": expiry},
            timeout=10,
        )
        resp.raise_for_status()
        data  = resp.json()
        items = data.get("calls", []) if opt_type == "call" else data.get("puts", [])

        exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        DTE = max((exp_date - date.today()).days, 0)

        rows = []
        for item in items:
            try:
                details = item.get("optionDetails") or {}
                greeks  = details.get("greeks") or {}
                K     = float(details.get("strikePrice", 0) or 0)
                bid   = float(item.get("bid",  0) or 0)
                ask   = float(item.get("ask",  0) or 0)
                mid   = float(details.get("midPrice", 0) or 0) or round((bid + ask) / 2, 2)
                iv    = float(greeks.get("impliedVolatility", 0) or 0) * 100
                delta = abs(float(greeks.get("delta", 0) or 0))
                theta = float(greeks.get("theta", 0) or 0)
                gamma = float(greeks.get("gamma", 0) or 0)
                vega  = float(greeks.get("vega",  0) or 0)
                oi    = int(float(item.get("openInterest", 0) or 0))
                vol   = int(float(item.get("volume",       0) or 0))
                if mid <= 0 or K <= 0:
                    continue
                sigma  = iv / 100
                exp_mv = expected_move(K, sigma, DTE / 365) if sigma > 0 else 0.0
                rows.append({
                    "strike": K, "bid": bid, "ask": ask, "mid": round(mid, 2),
                    "iv": round(iv, 1), "delta": round(delta, 3),
                    "theta": round(theta, 4), "gamma": round(gamma, 4),
                    "vega": round(vega, 4), "exp_move": exp_mv,
                    "breakeven": round(K + mid if opt_type == "call" else K - mid, 2),
                    "oi": oi, "volume": vol, "DTE": DTE,
                    "expiry": expiry, "opt_type": opt_type, "source": "public.com",
                })
            except Exception:
                continue
        logger.debug(f"[{symbol}] Public.com: {len(rows)} {opt_type} strikes for {expiry}")
        return rows if rows else None
    except Exception as e:
        logger.warning(f"[{symbol}] Public.com chain failed: {e}")
        return None


def _fetch_chain_yfinance(symbol: str, expiry: str, opt_type: str, spot: float) -> list:
    """Fallback: fetch options chain from yfinance with B-S computed Greeks."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)

        if expiry not in ticker.options:
            available = ticker.options
            today_d   = date.today()
            # P5-L2: filter to future expiries only — expired contracts remain
            # in ticker.options but produce DTE=0 and degenerate B-S output
            future = [d for d in available
                      if datetime.strptime(d, "%Y-%m-%d").date() > today_d]
            if not future:
                return []
            expiry = min(future, key=lambda d: abs(
                datetime.strptime(d, "%Y-%m-%d").date() -
                datetime.strptime(expiry, "%Y-%m-%d").date()
            ))

        chain = ticker.option_chain(expiry)
        df    = chain.calls if opt_type == "call" else chain.puts
        if df is None or df.empty:
            return []

        exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        DTE = max((exp_date - date.today()).days, 0)
        T   = DTE / 365.0
        rows = []
        for _, row in df.iterrows():
            K   = float(row["strike"])
            iv  = float(row.get("impliedVolatility", 0) or 0)
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else float(row.get("lastPrice", 0) or 0)
            _oi_raw = row.get("openInterest", 0)
            _vl_raw = row.get("volume", 0)
            oi  = int(_oi_raw) if _oi_raw and not np.isnan(float(_oi_raw)) else 0
            vol = int(_vl_raw) if _vl_raw and not np.isnan(float(_vl_raw)) else 0
            if mid <= 0 or iv <= 0:
                continue
            delta  = bs_delta(spot, K, T, RISK_FREE_RATE, iv, opt_type)
            theta  = bs_theta(spot, K, T, RISK_FREE_RATE, iv, opt_type)
            exp_mv = expected_move(spot, iv, T)
            rows.append({
                "strike": K, "bid": bid, "ask": ask, "mid": mid,
                "iv": round(iv * 100, 1), "delta": round(abs(delta), 3),
                "theta": round(theta, 4), "exp_move": exp_mv,
                "breakeven": round(K + mid if opt_type == "call" else K - mid, 2),
                "oi": oi, "volume": vol, "DTE": DTE,
                "expiry": expiry, "opt_type": opt_type, "source": "yfinance",
            })
        return rows
    except Exception as e:
        logger.warning(f"[{symbol}] yfinance chain failed: {e}")
        return []


# ── Options chain ─────────────────────────────────────────────────────────────

def fetch_chain(symbol: str, expiry: str, opt_type: str, spot: float) -> list:
    """Fetch one side of the chain. Public.com first, yfinance fallback."""
    rows = _fetch_chain_public(symbol, expiry, opt_type)
    if rows is not None:
        return rows
    logger.debug(f"[{symbol}] Falling back to yfinance chain")
    return _fetch_chain_yfinance(symbol, expiry, opt_type, spot)


def _fetch_both_chains(symbol: str, expiry: str, spot: float) -> dict:
    """
    Fetch full options chain (calls + puts) in ONE Public.com API call.
    Returns {"call": [...], "put": [...]} — both keys always present.
    Falls back to two yfinance calls if Public unavailable.

    POST-2022-ERA: Vilkov 2026 — chain used for VRP + ISK computation as well
    as strike selection. Single API call avoids double rate-limit cost.
    """
    token = _get_public_token()
    exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    DTE = max((exp_date - date.today()).days, 0)

    def _parse_side(items: list, opt_type: str) -> list:
        rows = []
        for item in items:
            try:
                details = item.get("optionDetails") or {}
                greeks  = details.get("greeks") or {}
                K     = float(details.get("strikePrice", 0) or 0)
                bid   = float(item.get("bid",  0) or 0)
                ask   = float(item.get("ask",  0) or 0)
                mid   = float(details.get("midPrice", 0) or 0) or round((bid + ask) / 2, 2)
                iv    = float(greeks.get("impliedVolatility", 0) or 0) * 100
                delta = abs(float(greeks.get("delta", 0) or 0))
                theta = float(greeks.get("theta", 0) or 0)
                gamma = float(greeks.get("gamma", 0) or 0)
                vega  = float(greeks.get("vega",  0) or 0)
                oi    = int(float(item.get("openInterest", 0) or 0))
                vol   = int(float(item.get("volume",       0) or 0))
                if mid <= 0 or K <= 0:
                    continue
                sigma  = iv / 100
                exp_mv = expected_move(K, sigma, DTE / 365) if sigma > 0 else 0.0
                rows.append({
                    "strike": K, "bid": bid, "ask": ask, "mid": round(mid, 2),
                    "iv": round(iv, 1), "delta": round(delta, 3),
                    "theta": round(theta, 4), "gamma": round(gamma, 4),
                    "vega": round(vega, 4), "exp_move": exp_mv,
                    "breakeven": round(K + mid if opt_type == "call" else K - mid, 2),
                    "oi": oi, "volume": vol, "DTE": DTE,
                    "expiry": expiry, "opt_type": opt_type, "source": "public.com",
                })
            except Exception:
                continue
        return rows

    if token:
        try:
            import requests
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.post(
                f"{_PUBLIC_BASE}/userapigateway/marketdata/{_PUBLIC_ACCT_ID}/option-chain",
                headers=headers,
                json={"instrument": {"symbol": symbol, "type": "EQUITY"}, "expirationDate": expiry},
                timeout=10,
            )
            resp.raise_for_status()
            data  = resp.json()
            calls = _parse_side(data.get("calls", []), "call")
            puts  = _parse_side(data.get("puts",  []), "put")
            logger.debug(f"[{symbol}] Public.com both-chain: {len(calls)} calls, {len(puts)} puts ({expiry})")
            if calls or puts:
                return {"call": calls, "put": puts}
        except Exception as e:
            logger.warning(f"[{symbol}] _fetch_both_chains Public failed: {e}")

    # Fallback: yfinance per side
    logger.debug(f"[{symbol}] _fetch_both_chains falling back to yfinance ({expiry})")
    return {
        "call": _fetch_chain_yfinance(symbol, expiry, "call", spot),
        "put":  _fetch_chain_yfinance(symbol, expiry, "put",  spot),
    }


# ── VRP / ISK / VIX-tertile helpers (Vilkov 2026 / POST-2022-ERA) ─────────────

def _compute_vrp(call_rows: list, sig: dict) -> float | None:
    """
    Variance Risk Premium in vol-point terms: ATM_IV − RV_20d.
    Positive = options priced above realized vol (sellers compensated).
    Negative = options cheap relative to realized vol (buyers get good value).

    ATM proxy: call strike with delta closest to 0.50.
    RV proxy: 20-day std of log returns × sqrt(252), pre-computed in _score_symbol().

    POST-2022-ERA: Vilkov 2026 — primary vehicle-selection gate.
    """
    if not call_rows:
        return None
    rv_20d = sig.get("rv_20d")
    if rv_20d is None:
        return None
    # ATM = call with delta closest to 0.50
    atm = min(call_rows, key=lambda r: abs(r["delta"] - 0.50))
    atm_iv = atm.get("iv", 0)
    if not atm_iv:
        return None
    return round(atm_iv - rv_20d, 2)


def _compute_isk(call_rows: list, put_rows: list) -> float | None:
    """
    Implied Skew approximation: put_25d_IV − call_25d_IV.
    Positive = put skew elevated (market pricing downside fear) → bearish tilt.
    Negative = call skew elevated (risk appetite, upside demand) → bullish tilt.
    |isk| > 2 vol pts → apply ±0.05 delta adjustment on strike selection.

    POST-2022-ERA: Vilkov 2026 ISK feature (approximated from live chain).
    """
    if not call_rows or not put_rows:
        return None
    call_25d = min(call_rows, key=lambda r: abs(r["delta"] - 0.25))
    put_25d  = min(put_rows,  key=lambda r: abs(r["delta"] - 0.25))
    c_iv = call_25d.get("iv", 0)
    p_iv = put_25d.get("iv",  0)
    if not c_iv or not p_iv:
        return None
    return round(p_iv - c_iv, 2)


def _get_vix_tertile() -> str:
    """
    Classify current VIX as 'Low' / 'Mid' / 'High' based on 252-day history.
    Uses post-2022 data only (Vilkov 2026 structural break — pre-2022 dynamics differ).
    Cached in data/cache/vrp_cache.json with 30-min TTL to avoid repeated yfinance calls.

    T4 (yfinance): ^VIX not available on Alpaca Data — yfinance is the approved T4 source.
    CLAUDE.md: T4 usage logged at WARNING + Slack alert policy applies.
    """
    # Check cache first (30-min TTL)
    try:
        cache = json.loads(_VRP_CACHE_PATH.read_text())
        cached_at = datetime.fromisoformat(cache["cached_at"]).replace(tzinfo=ET)
        if (datetime.now(ET) - cached_at).total_seconds() < 1800:
            return cache.get("vix_tertile", "Mid")
    except Exception:
        pass

    try:
        import yfinance as yf
        logger.warning("T4 fallback: fetching ^VIX 2y history for VIX tertile (Vilkov 2026 POST-2022-ERA)")
        vix_df = yf.download("^VIX", period="2y", interval="1d", progress=False)
        if vix_df.empty:
            return "Mid"
        closes = vix_df["Close"].dropna()
        # POST-2022-ERA: use only post-2022 data for tertile cutoffs
        closes = closes[closes.index >= "2022-01-01"]
        if len(closes) < 30:
            return "Mid"
        q33, q67 = float(closes.quantile(1 / 3)), float(closes.quantile(2 / 3))
        vix_now  = float(closes.iloc[-1])
        tertile  = "High" if vix_now > q67 else ("Low" if vix_now <= q33 else "Mid")

        _VRP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _VRP_CACHE_PATH.write_text(json.dumps({
            "cached_at":   datetime.now(ET).isoformat(),
            "vix_now":     round(vix_now, 2),
            "q33":         round(q33, 2),
            "q67":         round(q67, 2),
            "vix_tertile": tertile,
        }, indent=2))
        logger.info(f"VIX tertile: {tertile} (VIX={vix_now:.1f}, q33={q33:.1f}, q67={q67:.1f})")
        return tertile
    except Exception as e:
        logger.warning(f"VIX tertile fetch failed: {e} — defaulting to Mid")
        return "Mid"


# ── Strike selection ──────────────────────────────────────────────────────────

def select_strike(rows: list, score: int, side: str = "long", mode: str = "weekly", isk_adj: float = 0.0) -> dict | None:
    """
    Pick the strike with delta closest to target.
    side: "long" (buy) → closer to ATM for larger directional payoff
          "short" (sell) → further OTM to maximize probability of expiring worthless
    mode: "weekly" | "0dte" — 0DTE uses tighter delta targets (gamma risk)
    isk_adj: ±0.05 ISK tilt — nudges target delta when put/call skew exceeds 2 vol pts
    Returns None if no viable (mid > 0) row found.
    """
    if not rows:
        return None
    if side == "short":
        target = DELTA_TARGET_SHORT_0DTE if mode == "0dte" else DELTA_TARGET_SHORT
    elif score >= MIN_SCORE_HIGH:
        target = DELTA_TARGET_HIGH_0DTE if mode == "0dte" else DELTA_TARGET_HIGH
    else:
        target = DELTA_TARGET_MOD_0DTE if mode == "0dte" else DELTA_TARGET_MOD
    # Clamp after ISK adjustment — delta must stay in (0.05, 0.95)
    target = max(0.05, min(0.95, target + isk_adj))
    viable = [r for r in rows if r["mid"] > 0]
    if not viable:
        return None
    return min(viable, key=lambda r: abs(r["delta"] - target))


# ── Entry window checks ───────────────────────────────────────────────────────

def in_entry_window() -> tuple[bool, str]:
    """Returns (in_window, status_label) for weekly entry windows."""
    now  = datetime.now(ET)
    mins = now.hour * 60 + now.minute
    for h_open, m_open, h_close, m_close in ENTRY_WINDOWS:
        w_open  = h_open  * 60 + m_open
        w_close = h_close * 60 + m_close
        if w_open <= mins < w_close:
            return True, f"ENTRY WINDOW OPEN ({h_open:02d}:{m_open:02d}–{h_close:02d}:{m_close:02d} ET)"
    all_opens = [h * 60 + m for h, m, _, __ in ENTRY_WINDOWS]
    future    = [t for t in all_opens if t > mins]
    if future:
        nxt = min(future)
        return False, f"Wait — next window {nxt // 60:02d}:{nxt % 60:02d} ET"
    return False, "Entry windows closed for today"


def _in_0dte_window() -> tuple[bool, str]:
    """Returns (in_window, status_label) for the 0DTE entry window (10:05–10:20 ET)."""
    now  = datetime.now(ET)
    mins = now.hour * 60 + now.minute
    w_open  = 10 * 60 + 5
    w_close = 10 * 60 + 20
    if w_open <= mins < w_close:
        return True, "0DTE WINDOW OPEN (10:05–10:20 ET)"
    if mins < w_open:
        return False, "0DTE window opens 10:05 ET"
    return False, "0DTE window closed for today — 3:45 ET hard close if positions held"


# ── 0DTE direction lock — set once per day at entry window, never flips mid-day ─

def _load_dte_lock() -> dict | None:
    """
    Load today's 0DTE direction lock from disk.
    Returns the lock dict if it exists and is dated today; None otherwise.
    Lock format: {"date": "YYYY-MM-DD", "set_at_et": "HH:MM",
                  "directions": {sym: {"side": "short"|"long", "opt_type": "put"|"call"}}}
    """
    if not _DTE_LOCK_PATH.exists():
        return None
    try:
        with open(_DTE_LOCK_PATH) as f:
            lock = json.load(f)
        if lock.get("date") == date.today().isoformat():
            return lock
    except Exception:
        pass
    return None


def _save_dte_lock(directions: dict) -> None:
    """
    Persist today's 0DTE direction lock.
    directions: {sym: {"side": ..., "opt_type": ...}}
    Called once — the first 0DTE scan that runs at or after 10:05 ET.
    """
    now_et = datetime.now(ET)
    lock = {
        "date":       date.today().isoformat(),
        "set_at_et":  now_et.strftime("%H:%M"),
        "directions": directions,
    }
    try:
        _DTE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DTE_LOCK_PATH, "w") as f:
            json.dump(lock, f, indent=2)
        logger.info(
            f"0DTE direction lock saved for {lock['date']} at {lock['set_at_et']} ET: "
            + ", ".join(f"{s}={v['side']} {v['opt_type']}" for s, v in directions.items())
        )
    except Exception as e:
        logger.warning(f"Could not save 0DTE direction lock: {e}")


# ── Load existing manual fills ────────────────────────────────────────────────

def load_fills() -> list:
    if not ROBINHOOD_TRADES.exists():
        return []
    try:
        with open(ROBINHOOD_TRADES) as f:
            return json.load(f)
    except Exception:
        return []


# ── Core recommendation builder ───────────────────────────────────────────────

def _build_recs(
    scored: list,
    expiry: str,
    mode: str,
    in_win: bool,
    vix_tertile: str,
    events: list,
    locked_directions: dict | None = None,
) -> tuple[list, list, list]:   # P2-OPTIONS-REJECT: now returns (recs, watchlist, rejections)
    """
    Build recommendation and watchlist lists from pre-scored symbols.

    mode:             "weekly" | "0dte"
    vix_tertile:      "Low" | "Mid" | "High" — from _get_vix_tertile()
    events:           week event list for event_flags field
    locked_directions: {sym: {"side": "short"|"long", "opt_type": "put"|"call"}}
                      When provided (0DTE mode), direction is frozen — no intraday flip.
                      None means direction is derived fresh from VRP + MTF (weekly, or
                      first 0DTE scan of the day).

    VRP logic (POST-2022-ERA, Vilkov 2026):
      Bullish MTF + High VRP → short put  (collect premium; stock just needs to hold)
      Bullish MTF + Low VRP  → long call  (pay for premium; need the move up)
      Bearish MTF + High VRP → short call (collect premium; stock stays flat/down)
      Bearish MTF + Low VRP  → long put   (pay for premium; need the move down)

    ISK tilt: if |put_25d_IV − call_25d_IV| > 2 vol pts, nudge delta target ±0.05.
    BAS gate: skip if (ask−bid)/mid exceeds threshold for the mode/side.
    0DTE: runs in all VIX regimes (High-VIX block removed 2026-07-25; long-only defined risk).
    0DTE direction lock: once set at 10:05 ET, side+opt_type never re-derived mid-day.
    """
    recommendations = []
    watchlist       = []
    rejections      = []  # P2-OPTIONS-REJECT: per-symbol rejection reasons for HTML display
    max_dollars = MAX_TRADE_DOLLARS_0DTE if mode == "0dte" else MAX_TRADE_DOLLARS
    event_flags = [e["note"] for e in events]

    for sig in scored:
        sym   = sig["symbol"]
        err   = sig.get("error")
        price = sig.get("price")

        if err or not price:
            logger.debug(f"[{sym}] Skipping: {err or 'no price'}")
            rejections.append({"symbol": sym, "reason": err or "no price data", "score": 0})  # P2-OPTIONS-REJECT
            continue

        long_score  = sig.get("long_score",  0)
        short_score = sig.get("short_score", 0)

        # Determine directional bias from MTF signal
        if long_score >= short_score and long_score >= MIN_SCORE_ANY:
            mtf_bullish = True
            score       = long_score
        elif short_score > long_score and short_score >= MIN_SCORE_ANY:
            mtf_bullish = False
            score       = short_score
        else:
            _max_score = max(long_score, short_score)
            if _max_score >= 6:
                watchlist.append({**sig, "watch_reason": f"Score {_max_score}/12 — below threshold"})
            rejections.append({"symbol": sym, "reason": f"Score {_max_score}/12 below MIN_SCORE_ANY ({MIN_SCORE_ANY})", "score": _max_score})  # P2-OPTIONS-REJECT
            continue

        # Option B: weekly direction aligns to daily_bias when signal is directional.
        # Neutral/unknown keeps the raw MTF score-based direction.
        if mode == "weekly":
            _db = sig.get("daily_bias", "unknown")
            if _db in ("strong_bull", "bull"):
                mtf_bullish = True
            elif _db in ("strong_bear", "bear"):
                mtf_bullish = False

        # (High-VIX 0DTE block removed here 2026-07-25.) This _build_recs path is called for
        # "weekly" only — the active 0DTE path is _build_0dte_directional, where the block is also
        # removed. Kept mode=="0dte" out of this dead branch to avoid a misleading vestige.

        # Fetch both chain sides in one API call
        both       = _fetch_both_chains(sym, expiry, price)
        call_rows  = both["call"]
        put_rows   = both["put"]

        # VRP gate (POST-2022-ERA: Vilkov 2026)
        vrp = _compute_vrp(call_rows, sig)

        # ISK approximation (POST-2022-ERA: Vilkov 2026)
        isk = _compute_isk(call_rows, put_rows)

        # ── Direction determination ─────────────────────────────────────────────
        # 0DTE: if a day-lock exists for this symbol, use it — never re-derive mid-day.
        # This prevents whipsawing between SHORT PUT and SHORT CALL every 15 minutes.
        if mode == "0dte" and locked_directions and sym in locked_directions:
            _lk      = locked_directions[sym]
            side     = _lk["side"]
            opt_type = _lk["opt_type"]
            logger.debug(f"[{sym}] 0DTE direction LOCKED ({side} {opt_type}) — skipping re-derive")
        else:
            # Fresh derivation: VRP regime + MTF bias
            if vrp is not None and vrp > VRP_HIGH_THRESHOLD:
                # Options expensive: sell premium on the opposite side
                side = "short"
                if mtf_bullish:
                    opt_type = "put"   # bullish → short put
                else:
                    opt_type = "call"  # bearish → short call
            else:
                # Options fair or cheap: buy direction
                side = "long"
                opt_type = "call" if mtf_bullish else "put"

        # ISK tilt: soft ±0.05 delta nudge when skew signal is strong
        # Positive ISK (put skew elevated) → nudge toward slightly further OTM on calls
        isk_delta_adj = 0.0
        if isk is not None and abs(isk) > 2.0:
            isk_delta_adj = 0.05 if isk > 0 else -0.05

        # Select strike (isk_delta_adj applies ±0.05 tilt when |ISK| > 2 vol pts)
        chain = call_rows if opt_type == "call" else put_rows
        best  = select_strike(chain, score, side=side, mode=mode, isk_adj=isk_delta_adj)

        if best is None:
            watchlist.append({**sig, "watch_reason": f"No liquid {opt_type} strike ({expiry})"})
            rejections.append({"symbol": sym, "reason": f"No liquid {opt_type} strike for {expiry}", "score": score})  # P2-OPTIONS-REJECT
            continue

        # BAS gate
        bas_threshold = BAS_MAX_0DTE if mode == "0dte" else (BAS_MAX_SHORT if side == "short" else BAS_MAX_LONG)
        mid_px = best["mid"]
        cost_pct = round((best["ask"] - best["bid"]) / mid_px, 3) if mid_px > 0 else 1.0
        if cost_pct > bas_threshold:
            watchlist.append({
                **sig,
                "watch_reason": f"BAS {cost_pct:.0%} too wide (>{bas_threshold:.0%}) on {opt_type.upper()} {expiry}",
            })
            rejections.append({"symbol": sym, "reason": f"BAS {cost_pct:.0%} too wide (>{bas_threshold:.0%}) on {opt_type.upper()} {expiry}", "score": score})  # P2-OPTIONS-REJECT
            logger.info(f"[{sym}] BAS gate: {cost_pct:.0%} > {bas_threshold:.0%} — skipping")
            continue

        # Premium filter for conditional names (long only)
        if sym in CONDITIONAL_UNIVERSE and side == "long" and best["mid"] > PREMIUM_LIMIT_CONDITIONAL:
            watchlist.append({**sig, "watch_reason": f"Premium ${best['mid']:.2f} > ${PREMIUM_LIMIT_CONDITIONAL:.2f} limit"})
            rejections.append({"symbol": sym, "reason": f"Premium ${best['mid']:.2f} > ${PREMIUM_LIMIT_CONDITIONAL:.2f} limit (conditional universe)", "score": score})  # P2-OPTIONS-REJECT
            logger.info(f"[{sym}] {opt_type.upper()} excluded: premium ${best['mid']:.2f} too high")
            continue

        # Sizing
        if side == "long":
            max_contracts = max(1, int(max_dollars / (best["mid"] * 100)))
            contracts     = min(max_contracts, 1 if mode == "0dte" else 2)
            cost_per_cont = round(best["mid"] * 100, 2)
            total_cost    = round(contracts * cost_per_cont, 2)
            credit_received = None
            est_margin      = None
            limit_sell      = round(best["mid"] * LIMIT_SELL_MULT, 2)
        else:
            # Short: receive premium; cap at 1 contract (margin discipline)
            contracts       = 1
            credit_received = round(best["mid"] * 100, 2)
            cost_per_cont   = None
            total_cost      = None
            est_margin      = round(best["strike"] * 100 * 0.20, 2)  # Reg T rough estimate
            limit_sell      = round(best["mid"] * SHORT_BUY_BACK, 2)  # buy back at 25% remaining

        # Log command — includes side for log_fill.py routing
        log_cmd = (
            f"python3.10 log_fill.py {sym} {side}_{opt_type} "
            f"{int(best['strike'])} {expiry} {best['mid']} {contracts}"
        )

        rec = {
            "symbol":          sym,
            "direction":       opt_type,   # "call" | "put" — the option type traded
            "side":            side,        # "long" | "short"
            "score":           score,
            "long_score":      long_score,
            "short_score":     short_score,
            "price":           price,
            "pct_change":      sig.get("pct_change"),
            "strike":          best["strike"],
            "expiry":          expiry,
            "premium_mid":     best["mid"],
            "bid":             best["bid"],
            "ask":             best["ask"],
            "iv":              best["iv"],
            "delta":           best["delta"],
            "theta":           best["theta"],
            "gamma":           best.get("gamma", "—"),
            "vega":            best.get("vega",  "—"),
            "source":          best.get("source", "yfinance"),
            "exp_move":        best["exp_move"],
            "breakeven":       best["breakeven"],
            "DTE":             best["DTE"],
            "oi":              best["oi"],
            "volume":          best["volume"],
            "limit_sell":      limit_sell,
            "contracts":       contracts,
            "cost_per_cont":   cost_per_cont,
            "total_cost":      total_cost,
            "credit_received": credit_received,
            "est_margin":      est_margin,
            "log_cmd":         log_cmd,
            "event_flags":     event_flags,
            "in_window":       in_win,
            "conviction":      "HIGH" if score >= MIN_SCORE_HIGH else "MOD",
            "vrp":             vrp,
            "isk":             isk,
            "vix_tertile":     vix_tertile,
            "cost_pct":        cost_pct,
            "mode":            mode,
        }
        recommendations.append(rec)
        _vrp_s = f"{vrp:.1f}" if vrp is not None else "—"
        _isk_s = f"{isk:.1f}" if isk is not None else "—"
        logger.info(
            f"[{sym}] {side.upper()} {opt_type.upper()} {expiry} ${best['strike']:.0f} | "
            f"mid=${best['mid']:.2f} δ={best['delta']:.2f} score={score}/12 "
            f"vrp={_vrp_s} isk={_isk_s} vix={vix_tertile} bas={cost_pct:.0%} mode={mode}"
        )

    recommendations.sort(key=lambda r: (-int(r["conviction"] == "HIGH"), -r["score"]))
    return recommendations, watchlist, rejections  # P2-OPTIONS-REJECT: 3-tuple


# ── 0DTE directional intraday-swing builder (Rafael 2026-07-13) ────────────────

def _select_directional_otm(rows: list, spot: float, opt_type: str,
                            target: float = ZDTE_DELTA_TARGET) -> dict | None:
    """Pick the OTM strike (delta ~0.35) for directional 0DTE capture. OTM = strike>spot
    for a call, strike<spot for a put. Returns the row dict or None if none liquid."""
    if not rows or spot <= 0:
        return None
    if opt_type == "call":
        band = [r for r in rows if r["strike"] > spot
                and ZDTE_DELTA_MIN <= r["delta"] <= ZDTE_DELTA_MAX and r["mid"] > 0]
        otm = [r for r in rows if r["strike"] > spot and r["mid"] > 0]
    else:
        band = [r for r in rows if r["strike"] < spot
                and ZDTE_DELTA_MIN <= r["delta"] <= ZDTE_DELTA_MAX and r["mid"] > 0]
        otm = [r for r in rows if r["strike"] < spot and r["mid"] > 0]
    pool = band or otm
    if not pool:
        return None
    return min(pool, key=lambda r: abs(r["delta"] - target))


def _build_0dte_directional(
    scored: list, expiry_today: str, in_win: bool, vix_tertile: str, events: list,
) -> tuple[list, list, list]:
    """Directional intraday-swing 0DTE recommendations — LONG premium, capture the same-day
    move EITHER WAY. For each SPY/QQQ/Mag-7 name: a long OTM ~0.35Δ CALL (upside) AND a long
    OTM ~0.35Δ PUT (downside), so the operator plays whichever direction the day develops.
    Replaces the prior VRP premium-selling 0DTE path (Rafael 2026-07-13). 1 contract, ≤$75,
    hard-close 3:45 ET. Runs in ALL VIX regimes (High-VIX block removed 2026-07-25 — long-only,
    max loss = premium paid, so no undefined tail risk)."""
    recs: list = []
    watch: list = []
    rej: list = []
    event_flags = [e["note"] for e in events]

    for sig in scored:
        sym = sig["symbol"]
        err = sig.get("error")
        price = sig.get("price")
        score = max(int(sig.get("long_score", 0) or 0), int(sig.get("short_score", 0) or 0))

        if err or not price:
            rej.append({"symbol": sym, "reason": err or "no price data", "score": 0, "mode": "0DTE"})
            continue
        # High-VIX 0DTE block REMOVED 2026-07-25 (Rafael): the "undefined tail risk" rationale was a
        # premium-SELLING concern. This path is LONG-only directional (max loss = premium paid,
        # defined + ≤$75/leg), and high VIX is exactly when directional puts pay and QHM/forever-6
        # adds are cheapest. High VIX no longer blocks 0DTE for ANY universe symbol.

        both = _fetch_both_chains(sym, expiry_today, price)

        for opt_type in ("call", "put"):
            chain = both["call"] if opt_type == "call" else both["put"]
            best = _select_directional_otm(chain, price, opt_type)
            if best is None:
                rej.append({"symbol": sym, "reason": f"no liquid OTM {opt_type} for 0DTE {expiry_today}",
                            "score": score, "mode": "0DTE"})
                continue

            mid_px = best["mid"]
            cost_pct = round((best["ask"] - best["bid"]) / mid_px, 3) if mid_px > 0 else 1.0
            if cost_pct > BAS_MAX_0DTE:
                rej.append({"symbol": sym, "reason": f"BAS {cost_pct:.0%} too wide (>{BAS_MAX_0DTE:.0%}) on {opt_type.upper()} 0DTE",
                            "score": score, "mode": "0DTE"})
                continue

            contracts = 1
            cost_per_cont = round(best["mid"] * 100, 2)
            total_cost = round(contracts * cost_per_cont, 2)
            if total_cost > MAX_TRADE_DOLLARS_0DTE:
                rej.append({"symbol": sym, "reason": f"{opt_type.upper()} 0DTE ${total_cost:.0f} > ${MAX_TRADE_DOLLARS_0DTE} cap",
                            "score": score, "mode": "0DTE"})
                continue

            limit_sell = round(best["mid"] * LIMIT_SELL_MULT, 2)   # +100% target on long premium
            log_cmd = (f"python3.10 log_fill.py {sym} long_{opt_type} "
                       f"{int(best['strike'])} {expiry_today} {best['mid']} {contracts}")

            recs.append({
                "symbol": sym, "direction": opt_type, "side": "long", "score": score,
                "long_score": int(sig.get("long_score", 0) or 0),
                "short_score": int(sig.get("short_score", 0) or 0),
                "price": price, "pct_change": sig.get("pct_change"),
                "strike": best["strike"], "expiry": expiry_today, "premium_mid": best["mid"],
                "bid": best["bid"], "ask": best["ask"], "iv": best["iv"], "delta": best["delta"],
                "theta": best["theta"], "gamma": best.get("gamma", "—"), "vega": best.get("vega", "—"),
                "source": best.get("source", "yfinance"), "exp_move": best["exp_move"],
                "breakeven": best["breakeven"], "DTE": best["DTE"], "oi": best["oi"], "volume": best["volume"],
                "limit_sell": limit_sell, "contracts": contracts, "cost_per_cont": cost_per_cont,
                "total_cost": total_cost, "credit_received": None, "est_margin": None,
                "log_cmd": log_cmd, "event_flags": event_flags, "in_window": in_win,
                "conviction": "HIGH" if score >= MIN_SCORE_HIGH else "MOD",
                "vrp": None, "isk": None, "vix_tertile": vix_tertile, "cost_pct": cost_pct, "mode": "0dte",
            })

    # Rank: HIGH-conviction first, then by score, then call (upside) before put per name
    recs.sort(key=lambda r: (-int(r["conviction"] == "HIGH"), -r["score"], r["symbol"], r["direction"]))
    return recs, watch, rej


# ── Main scan ────────────────────────────────────────────────────────────────

def run_scan() -> dict:
    weekly_expiry = get_next_friday_expiry()
    today_str     = date.today().isoformat()   # 0DTE expiry
    events        = get_week_events(weekly_expiry)
    in_win_weekly, win_label_weekly = in_entry_window()
    in_win_0dte,   win_label_0dte   = _in_0dte_window()
    now_pt = datetime.now(PT).strftime("%b %d · %I:%M %p PT")

    logger.info(f"Scanning options — weekly expiry {weekly_expiry}, 0DTE expiry {today_str}")
    logger.info(f"Week events: {[e['note'] for e in events]}")

    # Score universe once — union of weekly (core+conditional) and 0DTE (SPY/QQQ/Mag7)
    _weekly_syms = set(CORE_UNIVERSE + CONDITIONAL_UNIVERSE)
    _zdte_syms   = set(ZDTE_UNIVERSE)
    all_symbols  = sorted(_weekly_syms | _zdte_syms)
    scored = score_universe(all_symbols)
    scored_by_sym = {s["symbol"]: s for s in scored}

    # VIX tertile — cached 30 min; one yfinance T4 call per session
    vix_tertile = _get_vix_tertile()
    logger.info(f"VIX tertile: {vix_tertile}")

    # ── Weekly recommendations (core + conditional universe only) ──────────────
    weekly_scored = [scored_by_sym[s] for s in (CORE_UNIVERSE + CONDITIONAL_UNIVERSE) if s in scored_by_sym]
    weekly_recs, weekly_watch, weekly_rejections = _build_recs(  # P2-OPTIONS-REJECT
        weekly_scored, weekly_expiry, "weekly", in_win_weekly, vix_tertile, events
    )

    # ── 0DTE recommendations — DIRECTIONAL intraday-swing capture (RTH only) ────
    # Reframed 2026-07-13: long OTM call (upside) + long OTM put (downside) per SPY/QQQ/Mag7
    # name, so the operator plays the day either way. No direction-lock needed — both sides
    # are always shown. Premium-selling / VRP 0DTE path retired.
    now_et  = datetime.now(ET)
    is_rth  = (
        (9 * 60 + 30) <= (now_et.hour * 60 + now_et.minute) < (16 * 60)
        and now_et.weekday() < 5
    )
    dte_lock_time = None   # retained for the HTML signature (directional 0DTE has no lock)

    if is_rth:
        zdte_scored = [scored_by_sym[s] for s in ZDTE_UNIVERSE if s in scored_by_sym]
        dte_recs, dte_watch, dte_rejections = _build_0dte_directional(
            zdte_scored, today_str, in_win_0dte, vix_tertile, events,
        )
    else:
        dte_recs, dte_watch, dte_rejections = [], [], []  # P2-OPTIONS-REJECT
        logger.info("0DTE scan skipped — outside RTH")

    # Merge watchlists (deduplicate by symbol — weekly takes precedence)
    combined_watch = list(weekly_watch)
    seen_syms = {w["symbol"] for w in combined_watch}
    for w in dte_watch:
        if w["symbol"] not in seen_syms:
            combined_watch.append(w)
            seen_syms.add(w["symbol"])

    # P2-OPTIONS-REJECT: merge rejection lists (weekly + 0DTE, both included for display)
    combined_rejections = list(weekly_rejections) + [
        {**r, "mode": "0DTE"} for r in dte_rejections
    ]

    _scan_now = datetime.now(PT)
    result = {
        "scan_time":          _scan_now.isoformat(),
        "next_scan_ts":       (_scan_now + timedelta(minutes=15)).isoformat(),  # 15-min cron cadence
        "expiry":             weekly_expiry,
        "today":              today_str,
        "in_window":          in_win_weekly,
        "window_label":       win_label_weekly,
        "in_window_0dte":     in_win_0dte,
        "window_label_0dte":  win_label_0dte,
        "week_events":        [{"date": e["date"], "note": e["note"]} for e in events],
        "recommendations":    weekly_recs,
        "recs_0dte":          dte_recs,
        "watchlist":          combined_watch,
        "rejections":         combined_rejections,   # P2-OPTIONS-REJECT
        "fills":              load_fills(),
        "generated":          now_pt,
        "vix_tertile":        vix_tertile,
        "dte_lock_time":      dte_lock_time,   # HH:MM ET when direction was set, or None
    }

    # S1 — persist the rec + rejection history to durable JSONL BEFORE the os.replace below
    # overwrites options_scan.json (Finding 4: the scan already computes this wide event and then
    # destroys it every 15 min). Forward-only substrate for the GEX/0DTE accuracy evaluator; the
    # helper never raises, so a logging failure cannot break the scan or the JSON write.
    _persist_rec_history(weekly_recs, dte_recs, combined_rejections, _scan_now)

    # Write JSON
    tmp = OPTIONS_SCAN_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2, default=str))
    os.replace(str(tmp), str(OPTIONS_SCAN_JSON))
    logger.info(
        f"Scan complete — {len(weekly_recs)} weekly recs, "
        f"{len(dte_recs)} 0DTE recs, {len(combined_watch)} watching, "
        f"{len(combined_rejections)} rejected"  # P2-OPTIONS-REJECT
    )

    # Update dte_prev.json for scan_results.html stat tile
    if dte_recs:
        try:
            _best_dte = dte_recs[0]
            (LOGS_DIR / "dte_prev.json").write_text(json.dumps({
                "direction": _best_dte["direction"],
                "side":      _best_dte["side"],
                "strike":    _best_dte["strike"],
                "symbol":    _best_dte["symbol"],
                "score":     _best_dte["score"],
            }, indent=2))
        except Exception:
            pass

    return result


# ── HTML generation ───────────────────────────────────────────────────────────

def _direction_badge(direction: str, side: str = "long") -> str:
    """Return colored badge for the 4 option structures."""
    if side == "long":
        if direction == "call":
            return '<span class="badge badge-long-call">LONG CALL ↑</span>'
        return '<span class="badge badge-long-put">LONG PUT ↓</span>'
    else:
        if direction == "call":
            return '<span class="badge badge-short-call">SHORT CALL ↓</span>'
        return '<span class="badge badge-short-put">SHORT PUT ↑</span>'


def _conviction_badge(conviction: str) -> str:
    cls = "badge-high" if conviction == "HIGH" else "badge-mod"
    return f'<span class="badge {cls}">{conviction}</span>'


def _score_bar(score: int, max_score: int = 12) -> str:
    pct   = int(score / max_score * 100)
    color = "#30d158" if score >= 10 else "#ffd60a" if score >= 8 else "#ff9f0a"
    return (
        f'<div class="score-bar-wrap">'
        f'<div class="score-bar" style="width:{pct}%;background:{color}"></div>'
        f'</div><span class="score-label">{score}/{max_score}</span>'
    )


def _event_banner(events: list) -> str:
    if not events:
        return ""
    items = "".join(f'<li>📅 {e["date"]} — {e["note"]}</li>' for e in events)
    return f'<div class="event-banner"><strong>⚠️ Volatility Events This Week</strong><ul>{items}</ul></div>'


# ── Expandable rec table rows ─────────────────────────────────────────────────

def _bias_info(rec: dict) -> tuple:
    """Return (label, color) reflecting market view (accounts for long/short)."""
    d    = rec["direction"]   # "call" | "put"
    s    = rec["score"]
    side = rec.get("side", "long")
    # short put = bullish (profit if stock holds); short call = bearish
    bullish = (d == "call" and side == "long") or (d == "put" and side == "short")
    if bullish:
        return ("STRONG BULL", "#30d158") if s >= 10 else ("BULL", "#7fff4f")
    return ("STRONG BEAR", "#ff3b30") if s >= 10 else ("BEAR", "#ff6b6b")


def _rec_row(rec: dict, idx: int) -> str:
    """Compact 8-field summary row — click expands full detail.

    2026-07-13 two-column redesign: slimmed to the mockup's field set
    (Tkr/Px/Sc/VRP/Signal/Strk/δ/IV) so two tables sit side-by-side. Bias,
    DTE, Greeks, targets and the log command all remain in the expand row —
    no data is dropped, only de-duplicated from the at-a-glance line.
    """
    sym        = rec["symbol"]
    direction  = rec["direction"]
    side       = rec.get("side", "long")
    conviction = rec["conviction"]
    score      = rec["score"]
    price      = rec["price"]
    pct        = rec.get("pct_change")
    pct_str    = f"{pct:+.1f}%" if pct is not None else "—"
    pct_col    = "#30d158" if (pct or 0) > 0 else "#ff3b5c"
    score_col  = "#30d158" if score >= 10 else "#ffd60a" if score >= 8 else "#ff9f0a"
    conv_col   = "#7fff4f" if conviction == "HIGH" else "#ffd60a"
    high_bg    = "background:#0a1308;" if conviction == "HIGH" else ""

    # Signal label and color (4 variants)
    if side == "long":
        sig_lbl = "Long call ↑" if direction == "call" else "Long put ↓"
        sig_col = "#30d158" if direction == "call" else "#ff3b5c"
    else:
        sig_lbl = "Short call ↓" if direction == "call" else "Short put ↑"
        sig_col = "#ff9f0a"   # orange for short premium

    vrp       = rec.get("vrp")
    vrp_str   = f"{vrp:+.1f}" if vrp is not None else "—"
    vrp_col   = ("#ff9f0a" if vrp and vrp > VRP_HIGH_THRESHOLD
                 else "#30d158" if vrp and vrp < 0
                 else "#636680")

    return (f'<tr class="rec-row" onclick="tog({idx})" style="cursor:pointer;{high_bg}">'
            f'<td style="font-size:13px;font-weight:700;color:#e2e4ee;white-space:nowrap">'
            f'<span id="arr-{idx}" style="color:#4a5070;font-size:9px;margin-right:3px">▸</span>{sym}'
            f'<span style="font-size:8px;font-weight:700;color:{conv_col};margin-left:4px;'
            f'padding:1px 4px;border-radius:8px;background:rgba(127,255,79,.08)">{conviction[0]}</span></td>'
            f'<td style="white-space:nowrap;font-weight:600">${price:.0f}'
            f'<span style="font-size:10px;color:{pct_col};margin-left:3px">{pct_str}</span></td>'
            f'<td style="font-weight:700;color:{score_col}">{score}</td>'
            f'<td style="color:{vrp_col};font-size:11px">{vrp_str}</td>'
            f'<td style="font-weight:700;color:{sig_col};font-size:11px;white-space:nowrap">{sig_lbl}</td>'
            f'<td style="font-weight:700;color:#e2e4ee;font-size:13px">${rec["strike"]:.0f}</td>'
            f'<td style="color:#30d158;font-size:11px">{rec["delta"]:.2f}</td>'
            f'<td style="color:#b8bdd4;font-size:11px">{rec["iv"]:.0f}%</td>'
            f'</tr>')


def _rec_detail(rec: dict, idx: int) -> str:
    """Hidden detail row, shown on click. Buy-side and sell-side layouts differ."""
    sym       = rec["symbol"]
    direction = rec["direction"]
    side      = rec.get("side", "long")
    score     = rec["score"]
    score_pct = int(score / 12 * 100)
    score_col = "#30d158" if score >= 10 else "#ffd60a" if score >= 8 else "#ff9f0a"
    gamma     = rec.get("gamma", "—")
    vega      = rec.get("vega",  "—")
    gamma_str = f"{gamma:.4f}" if isinstance(gamma, float) else str(gamma)
    vega_str  = f"{vega:.3f}"  if isinstance(vega, float)  else str(vega)
    vrp       = rec.get("vrp")
    isk       = rec.get("isk")
    vrp_str   = f"{vrp:+.1f} vol pts" if vrp is not None else "—"
    isk_str   = f"{isk:+.1f} vol pts" if isk is not None else "—"
    vrp_col   = ("#ff9f0a" if vrp and vrp > VRP_HIGH_THRESHOLD
                 else "#30d158" if vrp and vrp < 0
                 else "#e2e4ee")
    isk_col   = "#ff3b5c" if isk and isk > 2 else "#30d158" if isk and isk < -2 else "#e2e4ee"
    opt_label = ("CALL" if direction == "call" else "PUT")
    side_label = "SHORT " if side == "short" else ""

    cell = 'style="background:#0f1220;border-radius:6px;padding:10px 12px;border:1px solid #161a28"'
    lbl  = 'style="font-size:9px;color:#b8bdd4;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px"'
    val  = 'style="font-size:13px;font-weight:600;color:#e2e4ee"'

    # ── Trade details column (buy-side vs sell-side) ─────────────────────────
    if side == "long":
        entry    = rec["premium_mid"]
        sell_at  = rec["limit_sell"]
        stop_at  = round(entry * 0.50, 2)
        trade_col1 = f"""
        <div {cell}><div {lbl}>Entry (mid)</div><div {val}>${entry:.2f}</div></div>
        <div {cell}><div {lbl}>Limit Sell (+100%)</div><div style="font-size:13px;font-weight:600;color:#30d158">${sell_at:.2f}</div></div>
        <div {cell}><div {lbl}>Mental Stop (−50%)</div><div style="font-size:13px;font-weight:600;color:#ff3b5c">${stop_at:.2f}</div></div>
        <div {cell}><div {lbl}>Breakeven</div><div {val}>${rec['breakeven']:.2f}</div></div>
        <div {cell}><div {lbl}>Total Cost</div><div {val}>${rec['total_cost']:.0f}</div></div>
        <div {cell}><div {lbl}>Contracts</div><div {val}>{rec['contracts']}</div></div>"""
    else:
        credit   = rec.get("credit_received", 0) or 0
        buy_back = rec["limit_sell"]   # 25% of credit = 75% profit close
        margin   = rec.get("est_margin", 0) or 0
        trade_col1 = f"""
        <div {cell}><div {lbl}>Credit Received</div><div style="font-size:13px;font-weight:600;color:#30d158">${credit:.0f}</div></div>
        <div {cell}><div {lbl}>Buy Back at 75% profit</div><div style="font-size:13px;font-weight:600;color:#ffd60a">${buy_back:.2f}</div></div>
        <div {cell}><div {lbl}>Max Loss (uncapped)</div><div style="font-size:13px;font-weight:600;color:#ff3b5c">Unlimited</div></div>
        <div {cell}><div {lbl}>Breakeven at Expiry</div><div {val}>${rec['breakeven']:.2f}</div></div>
        <div {cell}><div {lbl}>Est. Margin (Reg T ~20%)</div><div style="font-size:13px;font-weight:600;color:#ff9f0a">${margin:,.0f}</div></div>
        <div {cell}><div {lbl}>Contracts</div><div {val}>{rec['contracts']}</div></div>"""

    # 0DTE hard close warning
    mode_badge = ""
    if rec.get("mode") == "0dte":
        mode_badge = ('<div style="margin-bottom:12px;padding:7px 12px;background:#2a0a0a;'
                      'border:1px solid #ff3b30;border-radius:6px;font-size:11px;'
                      'font-weight:700;color:#ff3b30;letter-spacing:.04em">'
                      '⏰ 0DTE — HARD CLOSE AT 3:45 PM ET — DO NOT HOLD TO EXPIRY</div>')

    return f"""<tr id="det-{idx}" style="display:none">
  <td colspan="8" style="padding:0;border-bottom:1px solid #1e2440">
    <div style="padding:14px 16px;background:#0b0e16;border-top:1px solid #1e2440">
      {mode_badge}
      <div style="display:grid;grid-template-columns:1fr;gap:14px">

        <div>
          <div style="font-size:10px;color:#636680;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px">
            {sym} {expiry_display(rec['expiry'])} ${rec['strike']:.0f} {side_label}{opt_label}
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            {trade_col1}
          </div>
        </div>

        <div>
          <div style="font-size:10px;color:#636680;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px">Greeks &amp; Conditions</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div {cell}><div {lbl}>Delta (δ)</div><div style="font-size:13px;font-weight:600;color:#30d158">{rec['delta']:.2f}</div></div>
            <div {cell}><div {lbl}>Theta (θ/day)</div><div style="font-size:13px;font-weight:600;color:#ff3b5c">${rec['theta']:.3f}</div></div>
            <div {cell}><div {lbl}>Gamma (γ)</div><div style="font-size:13px;font-weight:600;color:#30d158">{gamma_str}</div></div>
            <div {cell}><div {lbl}>Vega (ν)</div><div {val}>{vega_str}</div></div>
            <div {cell}><div {lbl}>IV</div><div {val}>{rec['iv']:.1f}%</div></div>
            <div {cell}><div {lbl}>DTE</div><div {val}>{rec['DTE']}d</div></div>
          </div>
        </div>

        <div>
          <div style="font-size:10px;color:#636680;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px">Market Context</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div {cell}><div {lbl}>±1σ Move</div><div {val}>${rec['exp_move']:.2f}</div></div>
            <div {cell}><div {lbl}>OI / Volume</div><div {val}>{rec['oi']:,} / {rec['volume']:,}</div></div>
            <div {cell}><div {lbl}>Bid / Ask (BAS {rec.get('cost_pct', 0):.0%})</div><div {val}>${rec['bid']:.2f} / ${rec['ask']:.2f}</div></div>
            <div {cell}><div {lbl}>VRP</div><div style="font-size:13px;font-weight:600;color:{vrp_col}">{vrp_str}</div></div>
            <div {cell}><div {lbl}>ISK (put−call skew)</div><div style="font-size:13px;font-weight:600;color:{isk_col}">{isk_str}</div></div>
            <div {cell}>
              <div {lbl}>Score</div>
              <div style="display:flex;align-items:center;gap:6px;margin-top:4px">
                <div style="flex:1;height:4px;background:#161a28;border-radius:2px">
                  <div style="width:{score_pct}%;height:100%;background:{score_col};border-radius:2px"></div>
                </div>
                <span style="font-size:11px;color:{score_col};font-weight:700">{score}/12</span>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
    <div style="padding:10px 20px;background:#07090f;border-top:1px solid #161a28;
      display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span style="font-size:10px;color:#b8bdd4;white-space:nowrap">Log fill after execution:</span>
      <code onclick="navigator.clipboard.writeText(this.innerText)"
        style="font-family:'SF Mono','Menlo',monospace;font-size:11px;color:#0a84ff;cursor:pointer;word-break:break-all"
        onmouseover="this.style.color='#30d158'" onmouseout="this.style.color='#0a84ff'">{rec['log_cmd']}</code>
      <span style="font-size:9px;color:#b8bdd4">click to copy</span>
    </div>
  </td>
</tr>"""


def expiry_display(expiry_str: str) -> str:
    try:
        d = datetime.strptime(expiry_str, "%Y-%m-%d")
        return d.strftime("%-m/%-d")
    except Exception:
        return expiry_str


def _tier_header(label: str, color: str, n: int) -> str:
    """A full-width tier divider row (HIGHEST / SECONDARY) with count or empty-state."""
    tail = (f'<span style="color:#636680;font-weight:400"> · {n}</span>' if n
            else '<span style="color:#4a5070;font-weight:400"> — none this scan</span>')
    return (f'<tr><td colspan="8" style="padding:8px 12px;font-size:10px;font-weight:700;'
            f'letter-spacing:.06em;text-transform:uppercase;color:{color};'
            f'background:#0a0d14;border-top:1px solid #161a28">{label}{tail}</td></tr>')


def _build_index_anchor(recs: list) -> str:
    """Luke step D — pinned INDEX-0DTE block for SPY/QQQ. Call/put/strike are the largest
    glyphs on the page so a glance any hour lands here first. Never collapses/reorders.
    Each name shows its long OTM call (upside) + put (downside) with strike + IV."""
    if not recs:
        return ""
    by_sym: dict = {}
    for r in recs:
        by_sym.setdefault(r["symbol"], {})[r["direction"]] = r
    rows = ""
    for sym in ("SPY", "QQQ"):
        if sym not in by_sym:
            continue
        c = by_sym[sym].get("call")
        p = by_sym[sym].get("put")
        ref = c or p
        px = ref["price"]
        iv = ref.get("iv")
        call_html = (f'<span class="idx-leg" style="color:#30d158">▲ CALL ${c["strike"]:.0f}</span>'
                     if c else '<span class="idx-leg" style="color:#4a5070">▲ —</span>')
        put_html  = (f'<span class="idx-leg" style="color:#ff3b5c">▼ PUT ${p["strike"]:.0f}</span>'
                     if p else '<span class="idx-leg" style="color:#4a5070">▼ —</span>')
        iv_html   = f'<span class="idx-iv">IV {iv:.0f}%</span>' if iv is not None else ''
        rows += (f'<div class="idx-row"><span class="idx-sym">{sym}</span>'
                 f'<span class="idx-px">${px:.0f}</span>{call_html}{put_html}{iv_html}</div>')
    if not rows:
        return ""
    return f'<div class="idx-anchor"><div class="idx-hdr">▌ INDEX 0DTE</div>{rows}</div>'


def _build_rec_table(recs: list, id_offset: int = 0) -> str:
    """Render a TIERED (Highest-Conviction / Secondary) expandable rec table.

    Splits by conviction so the highest-conviction contracts read first, per the
    2026-07-05 UX redesign (board + Gro + GAI: tiered). id_offset keeps det-{idx}
    unique across weekly(0)/0DTE(1000); +500 separates the MOD sub-block.
    """
    if not recs:
        return '<p class="muted center">No recommendations at current signal levels.</p>'
    high = [r for r in recs if r.get("conviction") == "HIGH"]
    mod  = [r for r in recs if r.get("conviction") != "HIGH"]

    def _rows(subset: list, base: int) -> str:
        return "".join(_rec_row(r, i + base) + _rec_detail(r, i + base)
                       for i, r in enumerate(subset))

    high_body = (
        _tier_header("▲ Highest Conviction · score ≥ 10", "#30d158", len(high))
        + _rows(high, id_offset)
    )
    # SECONDARY collapsed by default (Luke step E): a clickable summary row toggles a hidden
    # second <tbody> (valid HTML — a table may have multiple tbodies). Keeps the fast-scan view
    # to the highest-conviction rows; tap to reveal the 8–9 tier.
    if mod:
        sec_summary = (
            f'<tr class="sec-toggle" onclick="togSec({id_offset})" style="cursor:pointer">'
            f'<td colspan="8" style="padding:8px 12px;font-size:10px;font-weight:700;'
            f'letter-spacing:.06em;text-transform:uppercase;color:#ffd60a;background:#0a0d14;'
            f'border-top:1px solid #161a28">'
            f'<span id="secarr-{id_offset}">▸</span> Secondary · score 8–9 · {len(mod)}'
            f'<span style="color:#4a5070;font-weight:400;text-transform:none;letter-spacing:0">'
            f' — tap to show</span></td></tr>'
        )
        sec_body = _rows(mod, id_offset + 500)
    else:
        sec_summary = _tier_header("◆ Secondary · score 8–9", "#ffd60a", 0)
        sec_body = ""
    return f"""<table style="width:100%;border-collapse:collapse">
  <thead><tr>
    <th>Tkr</th><th>Px</th><th>Sc</th><th>VRP</th>
    <th>Signal</th><th>Strk</th><th>δ</th><th>IV</th>
  </tr></thead>
  <tbody>{high_body}{sec_summary}</tbody>
  <tbody id="secbody-{id_offset}" style="display:none">{sec_body}</tbody>
</table>"""


def _rejections_section(rejections: list) -> str:
    """P2-OPTIONS-REJECT: collapsible table of per-symbol rejection reasons."""
    if not rejections:
        return ""
    rows = ""
    for r in rejections:
        sym   = r.get("symbol", "—")
        reason = r.get("reason", "—")
        score  = r.get("score", 0)
        mode_tag = (f' <span style="font-size:9px;color:#636680;margin-left:4px">[{r["mode"]}]</span>'
                    if r.get("mode") else "")
        score_col = "#ff3b5c" if score == 0 else "#ffd60a" if score < 8 else "#ff9f0a"
        score_str = f"{score}/12" if score > 0 else "—"
        rows += (
            f'<tr>'
            f'<td style="font-size:13px;font-weight:700;color:#e2e4ee">{sym}{mode_tag}</td>'
            f'<td style="font-size:12px;color:{score_col}">{score_str}</td>'
            f'<td style="font-size:12px;color:#b8bdd4">{reason}</td>'
            f'</tr>'
        )
    return f"""
<details style="margin:0;border-top:1px solid #161a28">
  <summary style="padding:10px 20px;font-size:10px;font-weight:600;color:#636680;
    text-transform:uppercase;letter-spacing:.1em;cursor:pointer;background:#000000;
    list-style:none;display:flex;align-items:center;gap:8px">
    <span style="color:#4a5070">▶</span>
    Rejected Symbols — {len(rejections)} filtered out this scan
    <span style="color:#4a5070;font-size:9px;font-weight:400;text-transform:none;letter-spacing:0;margin-left:4px">(click to expand)</span>
  </summary>
  <div style="padding:0 20px 12px;background:#000000">
    <table style="width:100%;border-collapse:collapse">
      <thead><tr>
        <th style="padding:7px 14px;text-align:left;font-size:10px;color:#636680;
          text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid #161a28">Symbol</th>
        <th style="padding:7px 14px;text-align:left;font-size:10px;color:#636680;
          text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid #161a28">Score</th>
        <th style="padding:7px 14px;text-align:left;font-size:10px;color:#636680;
          text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid #161a28">Rejection Reason</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</details>"""


def generate_html(data: dict) -> str:
    weekly_recs = data["recommendations"]
    dte_recs    = data.get("recs_0dte", [])
    rejections  = data.get("rejections", [])   # P2-OPTIONS-REJECT
    events      = data["week_events"]
    scan_time   = data.get("scan_time", "")       # freshness pill (Luke step A)
    next_scan_ts = data.get("next_scan_ts", "")
    expiry      = data["expiry"]
    today_str   = data.get("today", date.today().isoformat())
    in_win      = data["in_window"]
    in_win_0dte = data.get("in_window_0dte", False)
    vix_tertile  = data.get("vix_tertile", "—")

    exp_display = expiry_display(expiry)
    dte_display = expiry_display(today_str)

    high_count_weekly = sum(1 for r in weekly_recs if r.get("conviction") == "HIGH")
    high_count_dte    = sum(1 for r in dte_recs    if r.get("conviction") == "HIGH")
    high_count_total  = high_count_weekly + high_count_dte

    # VIX tertile color
    vix_t_col = ("#ff3b30" if vix_tertile == "High"
                 else "#30d158" if vix_tertile == "Low"
                 else "#ffd60a")

    # Rec tables — 0DTE IDs offset by 1000 to avoid det-{idx} collisions
    weekly_table = _build_rec_table(weekly_recs, id_offset=0)
    # Luke step D: pull SPY/QQQ into a pinned INDEX anchor block; the rest tier normally.
    _anchor_syms = ("SPY", "QQQ")
    _anchor_recs = [r for r in dte_recs if r["symbol"] in _anchor_syms]
    _rest_recs   = [r for r in dte_recs if r["symbol"] not in _anchor_syms]
    index_anchor = _build_index_anchor(_anchor_recs)
    dte_table    = _build_rec_table(_rest_recs, id_offset=1000)

    # (Retired 2026-07-13) The cross-strategy conflict note + SPY-alignment banner were
    # premium-selling-specific (weekly BUY vs 0DTE SELL). 0DTE is now directional LONG
    # premium with BOTH sides shown per name, so a name in both sections is simply a
    # directional setup on two timeframes — no contradiction to flag, no single 0DTE
    # direction to align on. Both banners retired.
    _conflict_note = ""
    _align_banner = ""

    # Rejections section — P2-OPTIONS-REJECT
    rejections_html = _rejections_section(rejections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="900">
  <title>Options Scanner — {exp_display} Weekly · {dte_display} 0DTE</title>
  <script>
    function tog(i) {{
      var d = document.getElementById("det-"+i);
      var a = document.getElementById("arr-"+i);
      if (!d) return;
      if (d.style.display === "none" || d.style.display === "") {{
        d.style.display = "table-row"; if (a) a.innerHTML = "▾";
      }} else {{
        d.style.display = "none"; if (a) a.innerHTML = "▸";
      }}
    }}
    function togSec(o) {{
      var b = document.getElementById("secbody-"+o);
      var a = document.getElementById("secarr-"+o);
      if (!b) return;
      var hidden = (b.style.display === "none" || b.style.display === "");
      b.style.display = hidden ? "table-row-group" : "none";
      if (a) a.innerHTML = hidden ? "▾" : "▸";
    }}
  </script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:#0d0f1a;color:#e8ecff;font-family:-apple-system,'SF Pro Text',sans-serif;font-size:13px;line-height:1.4}}
    .top-nav{{display:flex;align-items:center;justify-content:space-between;padding:13px 20px;
      background:#13162a;border-bottom:1px solid #252847;position:sticky;top:0;z-index:10}}
    .nav-title{{font-size:15px;font-weight:700;letter-spacing:-.01em}}
    .nav-sub{{font-size:11px;color:#8a94ae;margin-left:10px}}
    .nav-back{{font-size:11px;font-weight:600;color:#8a94ae;text-decoration:none;
      padding:5px 11px;border:1px solid #252847;border-radius:6px;white-space:nowrap;
      transition:color .15s,border-color .15s}}
    .nav-back:hover{{color:#00e5ff;border-color:#00e5ff}}
    /* ── freshness pill (Luke step A: kill screensaver clock, show data liveness) ── */
    .fresh-pill{{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:20px;
      border:1px solid #252847;font-size:11px;font-weight:600;color:#b8bdd4;font-variant-numeric:tabular-nums}}
    .fresh-dot{{width:8px;height:8px;border-radius:50%;background:#30d158;box-shadow:0 0 6px currentColor;
      animation:fp 2s infinite}}
    @keyframes fp{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
    /* ── dashboard-matched header (Rafael 2026-07-13: follow the dashboard top format) ── */
    .opt-hdr{{display:flex;align-items:center;justify-content:space-between;padding:13px 24px;
      border-bottom:1px solid #252847;background:#13162a;position:sticky;top:0;z-index:100;flex-wrap:wrap;gap:8px}}
    .logo{{font-size:16px;font-weight:700;color:#e8ecff;letter-spacing:-.01em}}
    .logo span{{color:#636680;font-size:10px;font-weight:500;display:block;letter-spacing:.08em;
      text-transform:uppercase;margin-top:2px}}
    .hdr-right{{display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
    .opt-pill{{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;border:1px solid;
      font-size:11px;font-weight:600;letter-spacing:.03em}}
    .opt-pill.open{{border-color:#30d158;color:#30d158;background:rgba(48,209,88,.08)}}
    .opt-pill.closed{{border-color:#636680;color:#8a94ae;background:rgba(99,102,128,.08)}}
    .pulse{{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse 1.5s infinite}}
    @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .win-pill{{display:flex;align-items:center;gap:7px;padding:5px 12px;border-radius:20px;
      border:1px solid;font-size:11px;font-weight:600;letter-spacing:.08em}}
    .win-pill.open{{border-color:#30d158;color:#30d158}}
    .win-pill.closed{{border-color:#8a94ae;color:#8a94ae}}
    .win-dot{{width:7px;height:7px;border-radius:50%;background:currentColor;animation:wp 1.5s infinite}}
    @keyframes wp{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
    .stat-bar{{display:flex;gap:1px;background:#252847;border-bottom:1px solid #252847}}
    .stat-tile{{flex:1;padding:12px 18px;background:#13162a}}
    .stat-tile.accent{{border-left:1px solid #1e2240}}
    .s-lbl{{font-size:10px;color:#8a94ae;letter-spacing:.06em;text-transform:uppercase;margin-bottom:4px}}
    .s-val{{font-size:22px;font-weight:700}}
    .s-sub{{font-size:11px;color:#8a94ae;margin-top:2px}}
    .event-banner{{background:rgba(255,214,10,.10);border:1px solid rgba(255,214,10,.3);padding:10px 20px;font-size:12px}}
    .event-banner ul{{margin-top:5px;padding-left:18px;color:#ffd60a}}
    .section-hdr{{padding:10px 20px 6px;font-size:10px;font-weight:600;color:#8a94ae;
      text-transform:uppercase;letter-spacing:.1em;border-bottom:1px solid #252847;background:#0d0f1a}}
    .section-hdr.dte-hdr{{background:#1f1a0d;border-top:2px solid #ff9f0a}}
    .strat-explainer{{padding:8px 20px 12px;font-size:11px;color:#b8bdd4;background:#0d0f1a;border-bottom:1px solid #252847;line-height:1.5}}
    .strat-explainer b{{color:#e8ecff;font-weight:600}}
    .strat-conflict{{margin:10px 20px 0;padding:10px 14px;background:rgba(255,159,10,.08);border:1px solid rgba(255,159,10,.3);border-radius:6px;font-size:11px;color:#ffd60a;line-height:1.5}}
    .strat-conflict b{{color:#ffe08a;font-weight:700}}
    /* ── Two-column side-by-side layout (2026-07-13 redesign) ── */
    .cols{{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid #252847}}
    .col{{border-right:1px solid #252847;min-width:0;overflow-x:auto}}
    .col:last-child{{border-right:none}}
    .colhead{{padding:11px 16px;border-top:3px solid transparent}}
    .col.wk .colhead{{background:#0f1e14;border-top-color:#30d158}}
    .col.zd .colhead{{background:#1f1a0d;border-top-color:#ff9f0a}}
    .ch-t{{font-size:14px;font-weight:700;display:block}}
    .col.wk .ch-t{{color:#30d158}}
    .col.zd .ch-t{{color:#ff9f0a}}
    .ch-s{{font-size:10px;color:#8a94ae;display:block;margin-top:2px;line-height:1.4}}
    /* ── ⓘ progressive-disclosure popover (Luke UX 2026-07-13: kill paragraph text) ── */
    .xpl{{display:inline;position:relative}}
    .xpl summary{{display:inline;cursor:pointer;color:#8a94ae;font-size:11px;font-weight:600;
      list-style:none;margin-left:6px;vertical-align:middle}}
    .xpl summary::-webkit-details-marker{{display:none}}
    .xpl-pop{{position:absolute;left:0;top:20px;z-index:30;width:280px;max-width:78vw;background:#0d1220;
      border:1px solid #2b3157;border-radius:8px;padding:10px 12px;font-size:11px;font-weight:400;
      color:#b8bdd4;line-height:1.55;box-shadow:0 8px 24px rgba(0,0,0,.55)}}
    .xpl-pop b{{color:#e8ecff;font-weight:600}}
    /* ── pinned INDEX-0DTE anchor (Luke step D: SPY/QQQ glanceable) ── */
    .idx-anchor{{border-left:3px solid #ff9f0a;background:#140f05;padding:8px 14px 10px}}
    .idx-hdr{{font-size:10px;font-weight:700;letter-spacing:.08em;color:#ff9f0a;
      text-transform:uppercase;margin-bottom:4px}}
    .idx-row{{display:flex;align-items:baseline;gap:12px;padding:5px 0;border-top:1px solid #241a08;flex-wrap:wrap}}
    .idx-row:first-of-type{{border-top:none}}
    .idx-sym{{font-size:17px;font-weight:800;color:#e8ecff;min-width:46px}}
    .idx-px{{font-size:12px;color:#8a94ae;min-width:50px}}
    .idx-leg{{font-size:15px;font-weight:700}}
    .idx-iv{{font-size:11px;color:#8a94ae;margin-left:auto}}
    .col table{{width:100%}}
    .col thead th{{padding:6px 10px;font-size:9px}}
    .col td{{padding:7px 10px}}
    .col .strat-explainer{{border-bottom:1px solid #1e2240}}
    .content{{padding:16px 20px}}
    table{{width:100%;border-collapse:collapse}}
    thead th{{padding:7px 14px;text-align:left;font-size:10px;font-weight:500;color:#8a94ae;
      letter-spacing:.08em;text-transform:uppercase;background:#0d0f1a;
      border-bottom:1px solid #252847;white-space:nowrap}}
    td{{padding:10px 14px;border-bottom:1px solid #1e2240}}
    .rec-row:hover td{{background:#1e2240}}
    .total-row td{{border-top:1px solid #252847;border-bottom:none;font-weight:600}}
    .fills-table{{background:#13162a;border-radius:8px;overflow:hidden;border:1px solid #252847}}
    .muted{{color:#b8bdd4}}
    .center{{text-align:center;padding:28px;color:#b8bdd4}}
    code{{font-family:'SF Mono',monospace}}
    .footer{{padding:14px 20px;font-size:10px;color:#4a5070;border-top:1px solid #161a28;background:#000000}}
    .legend-row{{display:flex;gap:20px;flex-wrap:wrap;margin-top:8px}}
    /* ── Direction badges ── */
    .badge{{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px;letter-spacing:.04em}}
    .badge-long-call{{background:rgba(48,209,88,.15);color:#30d158;border:1px solid rgba(48,209,88,.3)}}
    .badge-long-put{{background:rgba(255,59,92,.15);color:#ff3b5c;border:1px solid rgba(255,59,92,.3)}}
    .badge-short-call{{background:rgba(255,159,10,.15);color:#ff9f0a;border:1px solid rgba(255,159,10,.3)}}
    .badge-short-put{{background:rgba(255,159,10,.15);color:#ff9f0a;border:1px solid rgba(255,159,10,.3)}}
    .badge-high{{background:rgba(127,255,79,.12);color:#7fff4f;border:1px solid rgba(127,255,79,.25)}}
    .badge-mod{{background:rgba(255,214,10,.10);color:#ffd60a;border:1px solid rgba(255,214,10,.2)}}
    .score-bar-wrap{{flex:1;height:4px;background:#161a28;border-radius:2px;display:inline-block;width:60px;vertical-align:middle;margin-right:4px}}
    .score-bar{{height:100%;border-radius:2px}}
    .score-label{{font-size:11px;font-weight:700;vertical-align:middle}}
    @media(max-width:768px){{
      .stat-bar{{flex-wrap:wrap}}
      .stat-tile{{flex:1 1 calc(50% - 1px);min-width:0}}
      .top-nav{{flex-wrap:wrap;gap:6px}}
      thead th{{font-size:9px;padding:5px 8px;white-space:normal}}
      td{{padding:6px 8px;font-size:11px}}
      .content{{padding:10px 12px}}
      .legend-row{{gap:10px}}
      .cols{{grid-template-columns:1fr}}
      .col{{border-right:none;border-bottom:1px solid #252847}}
    }}
  </style>
</head>
<body>

<header class="opt-hdr">
  <div class="logo">Options Scanner <span>MTF CONFLUENCE</span></div>
  <div class="hdr-right">
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
      <span style="font-size:12px;color:#e8ecff;font-variant-numeric:tabular-nums" id="freshClock">—</span>
      <span style="font-size:9px;color:#8a94ae" id="freshTxt">next scan —</span>
    </div>
    <div class="opt-pill {'open' if (in_win or in_win_0dte) else 'closed'}"><span class="pulse"></span>{'ENTRY OPEN' if (in_win or in_win_0dte) else 'ENTRY CLOSED'}</div>
    <a href="scan_results.html" class="nav-back">← Scanner</a>
  </div>
</header>

<!-- 5-tile stat bar -->
<div class="stat-bar">
  <div class="stat-tile">
    <div class="s-lbl">High Conviction</div>
    <div class="s-val" style="color:{'#30d158' if high_count_total else '#4a5070'}">{high_count_total}</div>
    <div class="s-sub">score ≥ 10/12 · both sets</div>
  </div>
  <div class="stat-tile accent">
    <div class="s-lbl">VIX Tertile</div>
    <div class="s-val" style="font-size:20px;margin-top:3px;color:{vix_t_col}">{vix_tertile}</div>
    <div class="s-sub">POST-2022-ERA regime · Vilkov 2026</div>
  </div>
</div>

{_event_banner(events)}
{_align_banner}
{_conflict_note}

<!-- ── TWO-COLUMN: 0DTE directional (LEFT) | Weekly directional (RIGHT) ─────── -->
<div class="cols">

  <div class="col zd">
    <div class="colhead">
      <span class="ch-t"><span style="color:#ff9f0a">⚡</span> 0DTE directional · {len(dte_recs)}<details class="xpl"><summary>ⓘ</summary><span class="xpl-pop"><b>You BUY the option</b> — a call for an up-move OR a put for a down-move. <b>Pick ONE per name</b> — the two rows are alternatives, not a combined trade. Long 0DTE premium is <b>speculative</b> (fast theta decay, often expires worthless); risk capped at premium. SPY/QQQ + Mag 7.</span></details></span>
      <span class="ch-s"><b style="color:#ff3b30">⏰ hard close 3:45 ET</b> · entry 10:05–10:20 ET</span>
    </div>
    {index_anchor}
    {dte_table}
  </div>

  <div class="col wk">
    <div class="colhead">
      <span class="ch-t">📈 Weekly directional · {len(weekly_recs)} · {exp_display}<details class="xpl"><summary>ⓘ</summary><span class="xpl-pop"><b>You BUY the option.</b> Profit if the underlying moves your way before expiry; risk capped at premium paid. A bet on <b>movement</b>.</span></details></span>
      <span class="ch-s">entry 10:00–11:30 / 14:00–15:00 ET · click a row to expand</span>
    </div>
    {weekly_table}
  </div>

</div>

<!-- ── REJECTIONS ─────────────────────────────────────────────────────────── -->
{rejections_html}

<div class="footer">
  <div>MTF Confluence Bot · auto-refresh 15 min · log fills: <code>python3.10 log_fill.py SYMBOL long_call|long_put STRIKE EXPIRY PREMIUM CONTRACTS</code></div>
</div>
<script>
  // Dashboard-matched header clock + scan countdown (Rafael 2026-07-13).
  var SCAN_TS = "{scan_time}", NEXT_TS = "{next_scan_ts}";
  function fmtFresh() {{
    var now = new Date();
    var clk = document.getElementById("freshClock");
    if (clk) clk.textContent = now.toLocaleString('en-US', {{timeZone:'America/Los_Angeles',
      weekday:'short', month:'short', day:'numeric', hour:'numeric', minute:'2-digit', hour12:true}}) + ' PT';
    var txt = document.getElementById("freshTxt");
    if (!txt) return;
    var scan = new Date(SCAN_TS), next = new Date(NEXT_TS);
    if (isNaN(scan)) {{ txt.textContent = "scan —"; return; }}
    var ageMin = Math.max(0, Math.floor((now - scan) / 60000));
    var nxt = isNaN(next) ? -1 : Math.max(0, Math.floor((next - now) / 1000));
    if (nxt > 0) {{
      var m = Math.floor(nxt / 60), s = nxt % 60;
      txt.textContent = "scanned " + ageMin + "m ago · next in " + m + "m " + (s < 10 ? "0" : "") + s + "s";
    }} else {{
      txt.textContent = "scanned " + ageMin + "m ago · refreshing…";
    }}
  }}
  fmtFresh(); setInterval(fmtFresh, 1000);
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic text write (RC-5): tmp in same dir -> fsync -> os.replace.

    Prevents a truncated/half-written options.html if the process crashes
    mid-write or a watch-mode scan overlaps a manual run. The pid-scoped tmp
    name stops two concurrent writers clobbering each other's temp file;
    os.replace is atomic on POSIX. Mirrors run_scan()'s OPTIONS_SCAN_JSON write.
    """
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        # Never orphan the tmp; the original file is untouched because os.replace
        # is the last step. Re-raise so callers handle it exactly as the old
        # write_text failure (watch-mode try/except; single-run propagation).
        try:
            tmp.unlink(missing_ok=True)
        except OSError as _cleanup_err:
            logger.debug("options.html tmp cleanup failed: %s", _cleanup_err)
        raise


def main():
    parser = argparse.ArgumentParser(description="Options chain scanner")
    parser.add_argument("--watch", action="store_true", help="Refresh every 15 min during market hours")
    args = parser.parse_args()

    if args.watch:
        logger.info("Watch mode — refreshing every 15 min during market hours")
        while True:
            now_et = datetime.now(ET)
            mins   = now_et.hour * 60 + now_et.minute
            is_mkt = (9 * 60 + 30) <= mins < (16 * 60) and now_et.weekday() < 5
            if is_mkt:
                try:
                    data = run_scan()
                    html = generate_html(data)
                    _atomic_write_text(OPTIONS_HTML, html)
                    logger.info(
                        f"options.html updated → {len(data['recommendations'])} weekly, "
                        f"{len(data.get('recs_0dte', []))} 0DTE → {OPTIONS_HTML}"
                    )
                except Exception as e:
                    logger.error(f"Scan error: {e}", exc_info=True)
            else:
                logger.debug("Market closed — skipping scan")
            time.sleep(900)   # 15 min
    else:
        data = run_scan()
        html = generate_html(data)
        _atomic_write_text(OPTIONS_HTML, html)
        logger.info(
            f"Done — {len(data['recommendations'])} weekly recs, "
            f"{len(data.get('recs_0dte', []))} 0DTE recs → {OPTIONS_HTML}"
        )


if __name__ == "__main__":
    main()
