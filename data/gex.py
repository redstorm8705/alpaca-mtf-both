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
"""

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


# ── Contract list ─────────────────────────────────────────────────────────────

def _expiry_range() -> tuple[str, str]:
    """Nearest 3 weekly expiries — 21-day window from today ET."""
    today = datetime.now(ET)
    return today.strftime("%Y-%m-%d"), (today + timedelta(days=21)).strftime("%Y-%m-%d")


def _fetch_contracts(symbol: str, date_gte: str, date_lte: str) -> list[str]:
    """Return OCC contract symbols for the underlying within the expiry window."""
    try:
        resp = requests.get(
            f"{_BASE_TRADING}/v2/options/contracts",
            headers=_headers(),
            params={
                "underlying_symbols": symbol,
                "expiration_date_gte": date_gte,
                "expiration_date_lte": date_lte,
                "status": "active",
                "limit": 1000,
            },
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("GEX contracts [%s]: HTTP %s", symbol, resp.status_code)
            return []
        contracts = resp.json().get("option_contracts", [])
        return [c["symbol"] for c in contracts if c.get("symbol")]
    except Exception as e:
        logger.warning("GEX contracts [%s] failed: %s", symbol, e)
        return []


# ── Snapshots (greeks + OI) ───────────────────────────────────────────────────

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


# ── GEX computation ───────────────────────────────────────────────────────────

def _contract_type(occ_symbol: str) -> str:
    """Extract 'C' or 'P' from OCC symbol (e.g. SPY240101C00500000)."""
    for i, ch in enumerate(occ_symbol):
        if ch in ("C", "P") and i > 0 and occ_symbol[i - 1].isdigit():
            return ch
    return "U"


def _compute_gex(snapshots: dict, spot: float) -> dict:
    """
    Net GEX = Σ_k [ sign_k × gamma_k × OI_k × 100 × spot² ]
    Calls → positive, Puts → negative (dealer long-call / short-put convention).
    NEAR-FLIP when |net| < 10% of total gross exposure.
    """
    net_gex     = 0.0
    total_abs   = 0.0
    strike_gex: dict[float, float] = {}
    count       = 0

    for sym, snap in snapshots.items():
        greeks  = snap.get("greeks") or {}
        gamma   = greeks.get("gamma")
        oi      = snap.get("openInterest") or snap.get("open_interest")
        if gamma is None or not oi:
            continue

        ctype = _contract_type(sym)
        if ctype == "U":
            continue

        sign    = 1.0 if ctype == "C" else -1.0
        contrib = sign * float(gamma) * float(oi) * 100.0 * spot ** 2
        net_gex   += contrib
        total_abs += abs(contrib)
        count     += 1

        try:
            strike = int(sym[-8:]) / 1000.0
            strike_gex[strike] = strike_gex.get(strike, 0.0) + contrib
        except (ValueError, IndexError):
            logger.debug("GEX: unparseable OCC symbol %r — skipping strike contribution", sym)

    # Label
    if total_abs > 0 and abs(net_gex) / total_abs < _NEAR_FLIP_RATIO:
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

        contracts = _fetch_contracts(symbol, date_gte, date_lte)
        if not contracts:
            logger.warning("GEX [%s]: no active contracts in window — skipping", symbol)
            results[symbol] = {"error": "no_contracts"}
            continue

        snapshots = _fetch_snapshots(contracts)
        if not snapshots:
            logger.warning("GEX [%s]: no snapshots returned — skipping", symbol)
            results[symbol] = {"error": "no_snapshots"}
            continue

        gex = _compute_gex(snapshots, spot)
        gex["spot"]              = round(spot, 2)
        gex["contracts_fetched"] = len(contracts)
        results[symbol]          = gex
        logger.debug(
            "GEX [%s]: %s $%sM | flip=%s | contracts=%d",
            symbol, gex["label"], gex["raw_gex_m"], gex["flip_strike"], gex["contract_count"],
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
