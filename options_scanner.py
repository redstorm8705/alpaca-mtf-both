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
  0DTE:   same-day expiry. Entry window 10:05–10:20 ET ONLY. 3:45 ET hard close.
          Max $75/trade, 1 contract. Blocked entirely in High VIX tertile.

Universe: SPY, QQQ, AAPL, MSFT, AMZN (core) + META, GOOGL, AMD (conditional, premium ≤ $3)
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
from ui_tokens import LIVE_CLOCK_HTML  # live-clock rule (2026-07-06)
import logging
import argparse
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

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# ── Universe ─────────────────────────────────────────────────────────────────
CORE_UNIVERSE        = ["SPY", "QQQ", "AAPL", "MSFT", "AMZN"]
CONDITIONAL_UNIVERSE = ["META", "GOOGL", "AMD"]   # only when ATM premium ≤ $3.00
PREMIUM_LIMIT_CONDITIONAL = 3.00

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
    0DTE: blocked entirely in High VIX (undefined tail risk at 0DTE expiry).
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

        # 0DTE: block in High VIX regime — undefined tail risk at same-day expiry
        if mode == "0dte" and vix_tertile == "High":
            watchlist.append({**sig, "watch_reason": "0DTE blocked: High VIX regime — tail risk (Vilkov 2026)"})
            rejections.append({"symbol": sym, "reason": "0DTE blocked: High VIX regime — undefined tail risk (Vilkov 2026)", "score": score})  # P2-OPTIONS-REJECT
            continue

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

    # Score universe once — reused for both weekly and 0DTE
    all_symbols = CORE_UNIVERSE + CONDITIONAL_UNIVERSE
    scored = score_universe(all_symbols)

    # VIX tertile — cached 30 min; one yfinance T4 call per session
    vix_tertile = _get_vix_tertile()
    logger.info(f"VIX tertile: {vix_tertile}")

    # ── Weekly recommendations ─────────────────────────────────────────────────
    weekly_recs, weekly_watch, weekly_rejections = _build_recs(  # P2-OPTIONS-REJECT
        scored, weekly_expiry, "weekly", in_win_weekly, vix_tertile, events
    )

    # ── 0DTE recommendations (RTH only — 0DTE worthless after close) ───────────
    # Direction is locked at the first scan at/after 10:05 ET and never re-derived.
    # This prevents whipsawing between SHORT PUT and SHORT CALL every 15 minutes.
    now_et  = datetime.now(ET)
    is_rth  = (
        (9 * 60 + 30) <= (now_et.hour * 60 + now_et.minute) < (16 * 60)
        and now_et.weekday() < 5
    )
    # Load existing day-lock (None if no lock for today)
    dte_lock = _load_dte_lock()
    locked_directions = dte_lock.get("directions") if dte_lock else None
    dte_lock_time = dte_lock.get("set_at_et") if dte_lock else None

    if is_rth:
        dte_recs, dte_watch, dte_rejections = _build_recs(  # P2-OPTIONS-REJECT
            scored, today_str, "0dte", in_win_0dte, vix_tertile, events,
            locked_directions=locked_directions,
        )
        # Save direction lock after the first 0DTE scan at or after 10:05 ET.
        # Once saved, all subsequent scans today use the locked directions.
        now_mins = now_et.hour * 60 + now_et.minute
        past_window_open = now_mins >= (10 * 60 + 5)
        if not dte_lock and past_window_open and dte_recs:
            new_lock = {
                r["symbol"]: {"side": r["side"], "opt_type": r["direction"]}
                for r in dte_recs
            }
            _save_dte_lock(new_lock)
            dte_lock_time = datetime.now(ET).strftime("%H:%M")
            logger.info(f"0DTE direction locked for the day at {dte_lock_time} ET")
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

    result = {
        "scan_time":          datetime.now(PT).isoformat(),
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
    """Compact summary row — click expands detail."""
    sym        = rec["symbol"]
    direction  = rec["direction"]
    side       = rec.get("side", "long")
    conviction = rec["conviction"]
    score      = rec["score"]
    price      = rec["price"]
    pct        = rec.get("pct_change")
    pct_str    = f"{pct:+.2f}%" if pct is not None else "—"
    pct_col    = "#30d158" if (pct or 0) > 0 else "#ff3b5c"
    bias_lbl, bias_col = _bias_info(rec)
    score_col  = "#30d158" if score >= 10 else "#ffd60a" if score >= 8 else "#ff9f0a"
    conv_col   = "#7fff4f" if conviction == "HIGH" else "#ffd60a"
    high_bg    = "background:#0a1308;" if conviction == "HIGH" else ""

    # Signal label and color (4 variants)
    if side == "long":
        sig_lbl = "LONG CALL ↑" if direction == "call" else "LONG PUT ↓"
        sig_col = "#30d158" if direction == "call" else "#ff3b5c"
    else:
        sig_lbl = "SHORT CALL ↓" if direction == "call" else "SHORT PUT ↑"
        sig_col = "#ff9f0a"   # orange for short premium

    vrp       = rec.get("vrp")
    vrp_str   = f"VRP {vrp:+.1f}" if vrp is not None else ""
    vrp_col   = ("#ff9f0a" if vrp and vrp > VRP_HIGH_THRESHOLD
                 else "#30d158" if vrp and vrp < 0
                 else "#636680")

    return (f'<tr class="rec-row" onclick="tog({idx})" style="cursor:pointer;{high_bg}">'
            f'<td style="font-size:14px;font-weight:700;color:#e2e4ee;white-space:nowrap">{sym}'
            f'  <span style="font-size:9px;font-weight:600;color:{conv_col};margin-left:5px;'
            f'    padding:2px 5px;border-radius:10px;background:rgba(127,255,79,.08)">{conviction}</span></td>'
            f'<td style="white-space:nowrap"><span style="font-weight:600">${price:.2f}</span>'
            f'  <span style="font-size:11px;color:{pct_col};margin-left:5px">{pct_str}</span></td>'
            f'<td style="font-size:11px;font-weight:700;color:{bias_col};white-space:nowrap">{bias_lbl}</td>'
            f'<td style="font-weight:700;color:{score_col};white-space:nowrap">{score}/12</td>'
            f'<td style="color:{vrp_col};font-size:11px;white-space:nowrap">{vrp_str}</td>'
            f'<td style="font-weight:700;color:{sig_col};white-space:nowrap">{sig_lbl}'
            f'  <span style="font-size:10px;color:{conv_col};margin-left:3px">({conviction})</span></td>'
            f'<td style="font-weight:700;color:#e2e4ee;font-size:13px;white-space:nowrap">${rec["strike"]:.0f}</td>'
            f'<td style="color:#30d158;font-size:12px">{rec["delta"]:.2f}</td>'
            f'<td style="color:#b8bdd4;font-size:12px">{rec["iv"]:.1f}%</td>'
            f'<td style="color:#b8bdd4;font-size:12px">{rec["DTE"]}d</td>'
            f'<td id="arr-{idx}" style="color:#636680;font-size:11px;text-align:center">▼</td>'
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
  <td colspan="9" style="padding:0;border-bottom:1px solid #1e2440">
    <div style="padding:16px 20px;background:#0b0e16;border-top:1px solid #1e2440">
      {mode_badge}
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">

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
    return (f'<tr><td colspan="11" style="padding:8px 14px;font-size:11px;font-weight:700;'
            f'letter-spacing:.08em;text-transform:uppercase;color:{color};'
            f'background:#0a0d14;border-top:1px solid #161a28">{label}{tail}</td></tr>')


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

    body = (
        _tier_header("▲ Highest Conviction · score ≥ 10", "#30d158", len(high))
        + _rows(high, id_offset)
        + _tier_header("◆ Secondary · score 8–9", "#ffd60a", len(mod))
        + _rows(mod, id_offset + 500)
    )
    return f"""<table style="width:100%;border-collapse:collapse">
  <thead><tr>
    <th>Ticker</th><th>Price</th><th>Bias</th><th>Score</th>
    <th>VRP</th><th>Signal</th><th>Strike</th><th>δ</th><th>IV</th><th>DTE</th>
    <th style="text-align:center;width:32px">▼</th>
  </tr></thead>
  <tbody>{body}</tbody>
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
    generated   = data["generated"]
    expiry      = data["expiry"]
    today_str   = data.get("today", date.today().isoformat())
    win_label   = data["window_label"]
    in_win      = data["in_window"]
    win_0dte    = data.get("window_label_0dte", "")
    in_win_0dte = data.get("in_window_0dte", False)
    vix_tertile  = data.get("vix_tertile", "—")
    dte_lock_time = data.get("dte_lock_time")   # HH:MM ET when 0DTE direction was locked, or None

    exp_display = expiry_display(expiry)
    exp_full    = datetime.strptime(expiry, "%Y-%m-%d").strftime("%A, %B %-d")
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
    dte_table    = _build_rec_table(dte_recs,    id_offset=1000)

    # Cross-strategy clarity: tickers in BOTH weekly (directional) and 0DTE
    # (premium-selling) are NOT a contradiction — they are different strategies.
    # Flag it explicitly (Rafael + board + Gro + GAI, 2026-07-06).
    _both_syms = sorted({r["symbol"] for r in weekly_recs} & {r["symbol"] for r in dte_recs})
    if _both_syms:
        _both_str = ", ".join(_both_syms)
        _conflict_note = (
            f'<div class="strat-conflict">&#9888; <b>{_both_str}</b> '
            f'appear{"" if len(_both_syms) > 1 else "s"} in BOTH sections below — this is '
            f'<b>not a contradiction</b>. The Weekly row is a <b>directional</b> trade '
            f'(you BUY, betting the stock moves your way); the 0DTE row is a '
            f'<b>premium-selling</b> trade (you SELL, betting it stays put today). '
            f'Different strategies, different holding periods — either can stand alone.</div>'
        )
    else:
        _conflict_note = ""

    # Alignment banner: both timeframes agree on SPY direction
    _spy_weekly_dir = next((r["direction"] for r in weekly_recs if r["symbol"] == "SPY"), None)
    _spy_0dte_dir   = next((r["direction"] for r in dte_recs   if r["symbol"] == "SPY"), None)
    _aligned        = _spy_weekly_dir and _spy_0dte_dir and _spy_weekly_dir == _spy_0dte_dir
    if _aligned:
        _dir_word = "BULLISH" if _spy_weekly_dir == "call" else "BEARISH"
        _dir_color = "#30d158" if _spy_weekly_dir == "call" else "#ff3b5c"
        _align_banner = (
            f'<div style="background:#0d1a0d;border:1px solid {_dir_color};border-left:4px solid {_dir_color};'
            f'padding:10px 20px;display:flex;align-items:center;gap:10px">'
            f'<span style="font-size:16px">{"📈" if _spy_weekly_dir == "call" else "📉"}</span>'
            f'<div>'
            f'<span style="font-weight:700;color:{_dir_color};font-size:13px">TIMEFRAME ALIGNMENT — SPY {_dir_word}</span>'
            f'<span style="color:#b8bdd4;font-size:11px;margin-left:10px">'
            f'Weekly ({str(_spy_weekly_dir).upper()}) and 0DTE ({str(_spy_0dte_dir).upper()}) point the same direction — '
            f'elevated confluence</span>'
            f'</div>'
            f'</div>'
        )
    else:
        _align_banner = ""

    # Rejections section — P2-OPTIONS-REJECT
    rejections_html = _rejections_section(rejections)

    # 0DTE window pill
    dte_win_cls = "open" if in_win_0dte else "closed"
    dte_win_dot = '<div class="win-dot"></div>' if in_win_0dte else ""

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
        d.style.display = "table-row"; a.innerHTML = "▲";
      }} else {{
        d.style.display = "none"; a.innerHTML = "▼";
      }}
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
    }}
  </style>
</head>
<body>

<div class="top-nav">
  <div style="display:flex;align-items:center;gap:16px">
    <div>
      <span class="nav-title">Options Scanner</span>
      <span class="nav-sub">
        Weekly {exp_full} ({exp_display}) · {len(weekly_recs)} rec{'s' if len(weekly_recs)!=1 else ''}
        &nbsp;·&nbsp; 0DTE {dte_display} · {len(dte_recs)} rec{'s' if len(dte_recs)!=1 else ''}
        &nbsp;·&nbsp; {generated} &nbsp;·&nbsp; {LIVE_CLOCK_HTML}
      </span>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <a href="scan_results.html" class="nav-back">← Scanner</a>
    <div class="win-pill {'open' if in_win else 'closed'}">
      {'<div class="win-dot"></div>' if in_win else ''}
      <span>{win_label}</span>
    </div>
    <div class="win-pill {dte_win_cls}" style="font-size:10px">
      {dte_win_dot}
      <span>{win_0dte or "0DTE"}</span>
    </div>
  </div>
</div>

<!-- 5-tile stat bar -->
<div class="stat-bar">
  <div class="stat-tile">
    <div class="s-lbl">Weekly Recs</div>
    <div class="s-val" style="color:{'#30d158' if weekly_recs else '#4a5070'}">{len(weekly_recs)}</div>
    <div class="s-sub">{high_count_weekly} high conviction</div>
  </div>
  <div class="stat-tile accent">
    <div class="s-lbl">0DTE Recs</div>
    <div class="s-val" style="color:{'#30d158' if dte_recs else '#4a5070'}">{len(dte_recs)}</div>
    <div class="s-sub">{high_count_dte} high · 3:45 ET close</div>
  </div>
  <div class="stat-tile accent">
    <div class="s-lbl">High Conviction</div>
    <div class="s-val" style="color:{'#30d158' if high_count_total else '#4a5070'}">{high_count_total}</div>
    <div class="s-sub">score ≥ 10/12 (both sets)</div>
  </div>
  <div class="stat-tile accent">
    <div class="s-lbl">Weekly Expiry</div>
    <div class="s-val" style="font-size:18px;margin-top:3px">{exp_display}</div>
    <div class="s-sub">{exp_full}</div>
  </div>
  <div class="stat-tile accent">
    <div class="s-lbl">VIX Tertile</div>
    <div class="s-val" style="font-size:18px;margin-top:3px;color:{vix_t_col}">{vix_tertile}</div>
    <div class="s-sub">POST-2022-ERA regime · Vilkov 2026</div>
  </div>
</div>

{_event_banner(events)}
{_align_banner}
{_conflict_note}

<!-- ── WEEKLY DIRECTIONAL (long premium) ──────────────────────────────────── -->
<div class="section-hdr">📈 Weekly Directional — {exp_display} Expiry · Buy Calls / Puts
  <span style="margin-left:8px;font-size:10px;color:#636680;font-weight:400;
    text-transform:none;letter-spacing:0">· click row to expand · entry windows 10:00–11:30 / 14:00–15:00 ET</span>
</div>
<div class="strat-explainer"><b>Directional — you BUY the option.</b> Profit if the underlying moves your way before expiry; risk is capped at the premium paid. A bet on <b>movement</b>.</div>
{weekly_table}

<!-- ── 0DTE PREMIUM SELLING (short premium) ───────────────────────────────── -->
<div class="section-hdr dte-hdr" style="margin-top:4px">
  <span style="color:#ff9f0a">⚡</span> 0DTE Premium Selling — {dte_display} Same-Day · Sell Puts / Calls
  <span style="margin-left:8px;font-size:10px;color:#ff3b30;font-weight:700;
    text-transform:none;letter-spacing:0">⏰ HARD CLOSE 3:45 PM ET — entry window 10:05–10:20 ET only</span>
  {'<span style="margin-left:10px;font-size:10px;color:#30d158;font-weight:700;text-transform:none;letter-spacing:0">🔒 DIRECTION LOCKED ' + dte_lock_time + ' ET — hold to 3:45 ET</span>' if dte_lock_time else '<span style="margin-left:10px;font-size:10px;color:#ffd60a;font-weight:600;text-transform:none;letter-spacing:0">⏳ Direction sets at 10:05 ET open</span>'}
  {'<span style="margin-left:12px;font-size:10px;color:#ff3b30;font-weight:700">BLOCKED — High VIX regime</span>' if vix_tertile == "High" else ''}
</div>
<div class="strat-explainer"><b>Premium selling — you SELL the option.</b> Profit if the underlying stays put and time decay erodes it by the 3:45 ET close. Short premium. A bet on <b>no big move today</b> — <b>not</b> a bet against the weekly.</div>
{dte_table}

<!-- ── REJECTIONS ─────────────────────────────────────────────────────────── -->
{rejections_html}

<div class="footer">
  <div>Options Scanner · MTF Confluence Bot · Auto-refresh every 15 min ·
    Log fills: <code>python3.10 log_fill.py SYMBOL long_call|short_put|… STRIKE EXPIRY PREMIUM CONTRACTS</code>
  </div>
  <div class="legend-row">
    <div><span style="color:#30d158;font-weight:700">●</span> LONG CALL/PUT — buy direction, low VRP</div>
    <div><span style="color:#ff9f0a;font-weight:700">●</span> SHORT CALL/PUT — sell premium, high VRP (IV > RV + 5 pts)</div>
    <div><span style="color:#30d158;font-weight:700">HIGH</span> score ≥ 10/12 &nbsp; <span style="color:#ffd60a;font-weight:700">MOD</span> score 8–9/12</div>
    <div style="color:#b8bdd4">VIX tertile: Low/Mid/High from post-2022 data (Vilkov 2026)</div>
    <div style="color:#b8bdd4">0DTE blocked in High VIX — hard close 3:45 ET regardless of P&L</div>
  </div>
</div>
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
