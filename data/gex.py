# ruff: noqa: E501
"""
data/gex.py
Gamma Exposure (GEX) computation via Alpaca Options REST API.

Data tier: T1 (Alpaca Options Data — data.alpaca.markets)
Output:
  logs/gex_snapshot.json   — current snapshot, atomic write
  logs/gex_history.jsonl   — rolling append
Cadence: called by live_data_writer.py every 15 min during RTH.
Display-only — no sizing or scoring impact (board vote required for that).

GREEKS SOURCE (2026-07-03 fix — board 2/2 Kyle+McKinney, Gro APPROVE, GAI APPROVE):
The free 'indicative' options feed returns ONLY latestQuote (bp/ap) — no greeks,
no open interest, ever (OPRA feed = 403 without signed agreement). The original
implementation read gamma/OI from the snapshot and therefore produced ZERO valid
datapoints from 2026-04-27 through 2026-07-03 (1,269 snapshots, all count=0 or
error). Fixed by:
  1. OI read from the CONTRACT object (/v2/options/contracts open_interest) —
     the field verified present on live response 2026-07-03 (RC-6 closed).
  2. Gamma computed locally: implied vol from quote mid via Black-Scholes
     bisection, then analytic BS gamma. Pure stdlib math — no new dependencies.
  3. Zero valid contracts → label UNKNOWN (was: mislabeled POSITIVE).
Model assumptions are module constants below and logged with every snapshot.
Accuracy target is the 15-min REGIME LABEL only — not pricing or execution.
"""

import math
import os
import json
import logging
import requests  # type: ignore[import-untyped]
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# RC-2: absolute path anchor — file lives at data/gex.py, project root is one level up
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOGS_DIR     = _PROJECT_ROOT / "logs"
_STATE_FILE   = _PROJECT_ROOT / "data" / "state" / "hybrid_state.json"
_SNAP_PATH    = _LOGS_DIR / "gex_snapshot.json"
_HIST_PATH    = _LOGS_DIR / "gex_history.jsonl"

_BASE_DATA    = "https://data.alpaca.markets"     # market data: stocks, options snapshots
_BASE_TRADING = "https://paper-api.alpaca.markets"  # trading API: contract listings
_TIMEOUT = 8.0

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

# Market-level anchors — always computed
_MARKET_SYMBOLS = ["SPY", "QQQ"]

# Near-flip threshold: net GEX is < 10% of total gross gamma exposure
_NEAR_FLIP_RATIO = 0.10

# ── Local-greeks model assumptions (2026-07-03 board conditions) ──────────────
# Regime label is insensitive to ±100bp on either constant (Kyle board note);
# both are logged with every snapshot so the shadow review can audit them.
_RISK_FREE_RATE   = 0.045   # continuous r
_DIV_YIELD        = 0.012   # SPY trailing dividend yield approx; acceptable for QQQ at label granularity
_MAX_SPREAD_RATIO = 0.25    # skip quotes with (ask-bid)/mid above this (hygiene — zero-bid/wide filter)
_IV_LO, _IV_HI    = 0.01, 3.0   # bisection bracket; mid outside bracket → contract skipped
_MIN_T_YEARS      = 1.0 / (365.0 * 24.0)  # 1-hour floor on time-to-expiry
_MAX_CONTRACT_PAGES = 2     # up to 2000 contracts per underlying (API-budget cap for mtf-writer)


# ── Auth ─────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     os.getenv("ALPACA_API_KEY", ""),
        "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
    }


# ── Spot price ────────────────────────────────────────────────────────────────

def _get_spot(symbol: str) -> float | None:
    """Fetch latest trade price for underlying. T1 — Alpaca Data REST."""
    try:
        resp = requests.get(
            f"{_BASE_DATA}/v2/stocks/{symbol}/trades/latest",
            headers=_headers(),
            timeout=4.0,
        )
        if resp.status_code == 200:
            price = resp.json().get("trade", {}).get("p")
            return float(price) if price else None
        logger.warning("GEX spot [%s]: HTTP %s", symbol, resp.status_code)
    except Exception as e:
        logger.warning("GEX spot [%s] failed: %s", symbol, e)
    return None


# ── Contract list (with open interest) ────────────────────────────────────────

def _expiry_range() -> tuple[str, str]:
    """Nearest 3 weekly expiries — 21-day window from today ET."""
    today = datetime.now(ET)
    return today.strftime("%Y-%m-%d"), (today + timedelta(days=21)).strftime("%Y-%m-%d")


def _fetch_contracts(symbol: str, date_gte: str, date_lte: str) -> dict[str, float]:
    """Return {occ_symbol: open_interest} within the expiry window.

    OI lives on the CONTRACT object (verified live 2026-07-03) — the snapshot
    endpoint never carries it on the indicative feed. Contracts with missing or
    zero OI (fresh weeklies before first T+1 OI report) are excluded here.
    Paginates up to _MAX_CONTRACT_PAGES pages of 1000.
    """
    oi_map: dict[str, float] = {}
    page_token: str | None = None
    try:
        for _page in range(_MAX_CONTRACT_PAGES):
            params: dict = {
                "underlying_symbols": symbol,
                "expiration_date_gte": date_gte,
                "expiration_date_lte": date_lte,
                "status": "active",
                "limit": 1000,
            }
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(
                f"{_BASE_TRADING}/v2/options/contracts",
                headers=_headers(),
                params=params,
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("GEX contracts [%s]: HTTP %s", symbol, resp.status_code)
                break
            body = resp.json()
            for c in body.get("option_contracts", []):
                _sym = c.get("symbol")
                _oi  = c.get("open_interest")
                if not _sym or _oi in (None, "0", 0):
                    continue
                try:
                    oi_map[_sym] = float(_oi)
                except (TypeError, ValueError):
                    logger.debug("GEX [%s]: unparseable open_interest %r on %s", symbol, _oi, _sym)
            page_token = body.get("next_page_token")
            if not page_token:
                break
    except Exception as e:
        logger.warning("GEX contracts [%s] failed: %s", symbol, e)
    return oi_map


# ── Snapshots (quotes — indicative feed carries latestQuote only) ─────────────

def _fetch_snapshots(contract_symbols: list[str]) -> dict:
    """Fetch option snapshots in batches of 100. T1 — Alpaca Options Data REST."""
    if not contract_symbols:
        return {}
    results: dict = {}
    for i in range(0, len(contract_symbols), 100):
        batch = contract_symbols[i : i + 100]
        try:
            resp = requests.get(
                f"{_BASE_DATA}/v1beta1/options/snapshots",
                headers=_headers(),
                params={"symbols": ",".join(batch), "feed": "indicative"},
                timeout=_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning("GEX snapshots batch: HTTP %s", resp.status_code)
                continue
            results.update(resp.json().get("snapshots", {}))
        except Exception as e:
            logger.warning("GEX snapshots batch failed: %s", e)
    return results


# ── Black-Scholes helpers (stdlib only) ───────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price(S: float, K: float, T: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes price with continuous dividend yield (_DIV_YIELD, _RISK_FREE_RATE)."""
    d1 = (math.log(S / K) + (_RISK_FREE_RATE - _DIV_YIELD + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * math.exp(-_DIV_YIELD * T) * _norm_cdf(d1) - K * math.exp(-_RISK_FREE_RATE * T) * _norm_cdf(d2)
    return K * math.exp(-_RISK_FREE_RATE * T) * _norm_cdf(-d2) - S * math.exp(-_DIV_YIELD * T) * _norm_cdf(-d1)


def _bs_gamma(S: float, K: float, T: float, sigma: float) -> float:
    d1 = (math.log(S / K) + (_RISK_FREE_RATE - _DIV_YIELD + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return math.exp(-_DIV_YIELD * T) * _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def _implied_vol(mid: float, S: float, K: float, T: float, is_call: bool) -> float | None:
    """Bisection on [_IV_LO, _IV_HI]. None if mid is outside the price bracket.

    60 iterations halve the bracket to <1e-16 of its width — convergence is
    guaranteed inside the bracket; the 1e-4 price tolerance just exits early.
    """
    lo, hi = _IV_LO, _IV_HI
    p_lo = _bs_price(S, K, T, lo, is_call)
    p_hi = _bs_price(S, K, T, hi, is_call)
    if not (p_lo <= mid <= p_hi):
        return None
    for _ in range(60):
        m = 0.5 * (lo + hi)
        p = _bs_price(S, K, T, m, is_call)
        if abs(p - mid) < 1e-4:
            return m
        if p < mid:
            lo = m
        else:
            hi = m
    return 0.5 * (lo + hi)


def _parse_occ(occ_symbol: str) -> tuple[datetime, bool, float] | None:
    """SPY260717C00620000 → (expiry 16:00 ET, is_call, strike). None on parse failure."""
    try:
        strike = int(occ_symbol[-8:]) / 1000.0
        cp = occ_symbol[-9]
        if cp not in ("C", "P"):
            return None
        yy = int(occ_symbol[-15:-13])
        mm = int(occ_symbol[-13:-11])
        dd = int(occ_symbol[-11:-9])
        expiry = datetime(2000 + yy, mm, dd, 16, 0, tzinfo=ET)
        return expiry, cp == "C", strike
    except (ValueError, IndexError):
        return None


# ── GEX computation ───────────────────────────────────────────────────────────

def _compute_gex(snapshots: dict, oi_map: dict, spot: float) -> dict:
    """
    Net GEX = Σ_k [ sign_k × gamma_k × OI_k × 100 × spot² ]
    Calls → positive, Puts → negative (dealer long-call / short-put convention).
    Gamma from local BS (IV implied from quote mid); OI from the contract object.
    NEAR-FLIP when |net| < 10% of total gross exposure.
    Zero valid contracts → label UNKNOWN (never POSITIVE on empty — 2026-07-03 fix).
    """
    net_gex     = 0.0
    total_abs   = 0.0
    strike_gex: dict[float, float] = {}
    count       = 0
    skips = {"no_quote": 0, "zero_bid": 0, "wide_spread": 0, "parse": 0, "expired": 0, "no_iv": 0}
    now_et = datetime.now(ET)

    for sym, snap in snapshots.items():
        oi = oi_map.get(sym)
        if not oi:
            continue   # snapshot for a contract we did not request OI for — ignore
        quote = snap.get("latestQuote") or {}
        bid = quote.get("bp")
        ask = quote.get("ap")
        if bid is None or ask is None:
            skips["no_quote"] += 1
            continue
        if bid <= 0 or ask < bid:
            skips["zero_bid"] += 1
            continue
        mid = 0.5 * (float(bid) + float(ask))
        if mid <= 0 or (float(ask) - float(bid)) / mid > _MAX_SPREAD_RATIO:
            skips["wide_spread"] += 1
            continue
        parsed = _parse_occ(sym)
        if parsed is None:
            skips["parse"] += 1
            logger.debug("GEX: unparseable OCC symbol %r — skipping", sym)
            continue
        expiry, is_call, strike = parsed
        T = (expiry - now_et).total_seconds() / (365.0 * 24 * 3600)
        if T < _MIN_T_YEARS:
            skips["expired"] += 1
            continue
        iv = _implied_vol(mid, spot, strike, T, is_call)
        if iv is None:
            skips["no_iv"] += 1
            continue
        gamma = _bs_gamma(spot, strike, T, iv)

        sign    = 1.0 if is_call else -1.0
        contrib = sign * gamma * oi * 100.0 * spot ** 2
        net_gex   += contrib
        total_abs += abs(contrib)
        count     += 1
        strike_gex[strike] = strike_gex.get(strike, 0.0) + contrib

    # Label — UNKNOWN on zero valid contracts (fix: was POSITIVE via net_gex >= 0)
    if count == 0:
        label = "UNKNOWN"
    elif total_abs > 0 and abs(net_gex) / total_abs < _NEAR_FLIP_RATIO:
        label = "NEAR-FLIP"
    elif net_gex >= 0:
        label = "POSITIVE"
    else:
        label = "NEGATIVE"

    # Gamma flip strike — lowest strike where cumulative GEX crosses zero
    flip_strike = None
    if strike_gex:
        cumulative  = 0.0
        prev_sign   = None
        for strike in sorted(strike_gex):
            cumulative += strike_gex[strike]
            curr_sign   = 1 if cumulative >= 0 else -1
            if prev_sign is not None and curr_sign != prev_sign:
                flip_strike = round(strike, 2)
                break
            prev_sign = curr_sign

    return {
        "raw_gex_m":       round(net_gex / 1_000_000, 1),
        "label":           label,
        "flip_strike":     flip_strike,
        "contract_count":  count,
        "skips":           skips,
        "model":           {"r": _RISK_FREE_RATE, "q": _DIV_YIELD, "max_spread": _MAX_SPREAD_RATIO},
    }


# ── Position symbols ──────────────────────────────────────────────────────────

def _position_symbols() -> list[str]:
    """Read current open position symbols from hybrid_state.json."""
    try:
        with open(_STATE_FILE) as f:
            state = json.load(f)
        return list(state.get("positions", {}).keys())
    except Exception as e:
        logger.debug("GEX: could not read positions from state: %s", e)
        return []


# ── Main entry point ──────────────────────────────────────────────────────────

def refresh_gex() -> None:
    """
    Compute GEX for market anchors + current positions.
    Writes gex_snapshot.json (atomic) and appends to gex_history.jsonl.
    Called by live_data_writer.py every 15 min during RTH.
    """
    _LOGS_DIR.mkdir(exist_ok=True)

    # RC-1: all datetimes tz-aware
    ts_pt  = datetime.now(PT)
    ts_str = ts_pt.strftime("%Y-%m-%d %I:%M %p PT")

    date_gte, date_lte = _expiry_range()

    # Universe: market anchors + current bot positions (deduped, order preserved)
    symbols = list(dict.fromkeys(_MARKET_SYMBOLS + _position_symbols()))

    results: dict = {}
    for symbol in symbols:
        spot = _get_spot(symbol)
        if not spot:
            logger.warning("GEX [%s]: no spot price — skipping", symbol)
            results[symbol] = {"error": "no_spot"}
            continue

        oi_map = _fetch_contracts(symbol, date_gte, date_lte)
        if not oi_map:
            logger.warning("GEX [%s]: no contracts with open interest in window — skipping", symbol)
            results[symbol] = {"error": "no_contracts"}
            continue

        snapshots = _fetch_snapshots(list(oi_map))
        if not snapshots:
            logger.warning("GEX [%s]: no snapshots returned — skipping", symbol)
            results[symbol] = {"error": "no_snapshots"}
            continue

        gex = _compute_gex(snapshots, oi_map, spot)
        gex["spot"]              = round(spot, 2)
        gex["contracts_fetched"] = len(oi_map)
        results[symbol]          = gex
        # INFO (was debug): this line IS the shadow-review record — must be visible
        # at production log level or the 30-session clock never accumulates evidence.
        logger.info(
            "GEX [%s]: %s $%sM | flip=%s | valid=%d/%d | skips=%s",
            symbol, gex["label"], gex["raw_gex_m"], gex["flip_strike"],
            gex["contract_count"], len(oi_map), gex["skips"],
        )

    snapshot = {"ts": ts_str, "symbols": results}

    # RC-5: atomic write for snapshot
    _tmp = _SNAP_PATH.with_suffix(".tmp")
    _tmp.write_text(json.dumps(snapshot, indent=2))
    _tmp.replace(_SNAP_PATH)

    # History — append-only, non-atomic acceptable
    with open(_HIST_PATH, "a") as f:
        f.write(json.dumps({"ts": ts_str, "symbols": results}) + "\n")

    logger.info("GEX snapshot written: %d symbols @ %s", len(results), ts_str)


def get_gex_regime(symbol: str = "SPY") -> dict:
    """Read cached GEX snapshot and return regime for given symbol.

    Returns dict: label (POSITIVE/NEGATIVE/NEAR-FLIP/STALE/UNKNOWN),
    raw_gex_m, flip_strike, age_minutes.
    Stale guard: if snapshot > GEX_STALE_MINUTES old, returns label=STALE.
    Called by run_cycle.py (Layer 8) and kelly.py (edge multiplier) during RTH.
    """
    import config as _cfg
    stale_minutes = getattr(_cfg, "GEX_STALE_MINUTES", 45)
    try:
        if not _SNAP_PATH.exists():
            return {"label": "UNKNOWN", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": None}
        snap = json.loads(_SNAP_PATH.read_text())
        ts_str = snap.get("ts", "")
        try:
            ts_dt       = datetime.strptime(ts_str, "%Y-%m-%d %I:%M %p PT").replace(tzinfo=PT)
            age_minutes = (datetime.now(PT) - ts_dt).total_seconds() / 60
        except Exception:
            age_minutes = 9999.0
        if age_minutes > stale_minutes:
            logger.debug(
                "GEX [%s]: snapshot %.0f min old (> %d min stale threshold)",
                symbol, age_minutes, stale_minutes,
            )
            return {"label": "STALE", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": age_minutes}
        sym_data = snap.get("symbols", {}).get(symbol, {})
        if not sym_data or "error" in sym_data:
            return {"label": "UNKNOWN", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": age_minutes}
        return {
            "label":       sym_data.get("label", "UNKNOWN"),
            "raw_gex_m":   sym_data.get("raw_gex_m", 0.0),
            "flip_strike": sym_data.get("flip_strike"),
            "age_minutes": age_minutes,
        }
    except Exception as e:
        logger.warning("GEX get_gex_regime failed: %s", e)
        return {"label": "UNKNOWN", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": None}
