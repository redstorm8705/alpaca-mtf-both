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

# ── Day-trade tier universe (design record day_tier_v2_design_2026-08-29 §2) ────
# GEX is computed on the UNDERLYING's chain. A leveraged single-name/index tracker's OWN
# option chain is thin/ghost (board Harris+Sinclair 2026-08-29), so its dealer-gamma regime
# is INHERITED from the underlying it tracks — the regime LABEL is instrument-agnostic (a 2x
# tracker sits in the same dealer-gamma regime as its underlying). We therefore compute GEX for
# the Mag-7 underlyings and RESOLVE a tracker symbol to its underlying at read time
# (get_gex_regime) — no need to compute the tracker's own (censored) chain.
_DAYTRADE_UNDERLYINGS = ["AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA"]
# leveraged tracker -> underlying whose GEX regime it inherits. Only CLEAN single-underlying
# maps are wired now; SOXL is deferred (its index isn't directly optionable — SMH vs SOXX proxy
# is a pending design decision, not guessed here). QQQ/TSLA/NVDA underlyings are already computed
# above, so the map adds no new compute, only read-time resolution.
_ETF_UNDERLYING_MAP = {
    "TSLL": "TSLA",   # 1.5x TSLA
    "NVDL": "NVDA",   # 2x NVDA
    "TQQQ": "QQQ",    # 3x QQQ
}

# ── Local-greeks model assumptions (2026-07-03 board conditions) ──────────────
# Regime label is insensitive to ±100bp on either constant (Kyle board note);
# both are logged with every snapshot so the shadow review can audit them.
_RISK_FREE_RATE   = 0.045   # continuous r
_DIV_YIELD        = 0.012   # SPY trailing dividend yield approx; acceptable for QQQ at label granularity
_MAX_SPREAD_RATIO = 0.25    # skip quotes with (ask-bid)/mid above this (hygiene — zero-bid/wide filter)
_IV_LO, _IV_HI    = 0.01, 3.0   # bisection bracket; mid outside bracket → contract skipped
_MIN_T_YEARS      = 1.0 / (365.0 * 24.0)  # 1-hour floor on time-to-expiry
_MAX_CONTRACT_PAGES = 2     # up to 2000 contracts per underlying (API-budget cap for mtf-writer)

# ── GEX data-quality gate + flip window (2026-07-14, board + Gro + GAI unanimous) ──────
# The Alpaca INDICATIVE feed drops ATM options (they carry the widest relative spreads),
# leaving a deep-ITM-biased strike set that dragged flip_strike ~10% below spot and biased
# the NEGATIVE/POSITIVE label (2026-07-14: labeled NEGATIVE while SPY pinned dead-flat).
# FAIL-SAFE: if the surviving strikes are too biased/sparse to trust, emit label=UNKNOWN +
# flip_strike=None. UNKNOWN already = neutral Kelly (GEX_EDGE_MULT_NEUTRAL) + no MIN_SCORE
# bump downstream, so a weak-data day has ZERO execution effect — strictly safer than
# emitting a confident-but-wrong level that actively costs trades. Thresholds are
# microstructure-derived STARTING points; recalibrate from logs/gex_history.jsonl
# (roadmap: GEX threshold + _NEAR_FLIP_RATIO recalibration once enough clean sessions log).
_ATM_MONEYNESS      = 0.01   # a strike within ±1% of spot counts as ATM
_MIN_ATM_STRIKES    = 3      # need >= this many surviving ATM strikes, else UNKNOWN
# WINDOWED capture (2026-07-15): capture ratio + count are measured over ONLY the ±_CAPTURE_WINDOW
# near-spot strikes — NOT all contracts — so far-OTM sparseness on the indicative feed can't drag the
# gate to a false UNKNOWN when near-spot data is fine (first live SPY: atm_count=80 but ALL-contract
# capture was 34% → false UNKNOWN). This is the region that actually drives the ±5% flip.
_CAPTURE_WINDOW_PCTILE = 0.10  # capture ratio + windowed count measured over ±10% of spot
_MIN_CAPTURE_RATIO  = 0.40   # WINDOWED valid-gamma / OI-bearing (within ±_CAPTURE_WINDOW) must be >= this
_MIN_WINDOWED_COUNT = 10     # >= this many valid-gamma contracts within ±_CAPTURE_WINDOW, else UNKNOWN
_FLIP_WINDOW_PCTILE = 0.05   # flip computed only over strikes within ±5% of spot (nearest-spot crossing)


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


# ── Spot-consistency guard (2026-07-26, board 5 seats + Gro + GAI) ─────────────
# The consumed GEX signal is the regime LABEL, which depends on spot (BS gamma + the
# ATM/capture/flip moneyness windows). A plausible-but-wrong spot (a bad last-trade tick /
# a gap) can FLIP the label and drive book-wide sizing on a false signal — the one path the
# STALE + strike-quality gates do not catch. Cross-check the latest-trade spot against an
# INDEPENDENT same-instant quote; a suspect spot NEVER computes a fresh label (see refresh_gex).

def _get_spot_quote(symbol: str) -> dict | None:
    """Fetch the latest NBBO quote (bid/ask) for the underlying. T1 — Alpaca Data REST.
    Independent same-instant reference used to sanity-check `_get_spot`'s last-trade print.
    Returns {"bid","ask","mid","spread"} or None on failure / unusable quote.
    Raw field names verified live 2026-07-26: quote.bp (bid), quote.ap (ask) (RC-6)."""
    try:
        resp = requests.get(
            f"{_BASE_DATA}/v2/stocks/{symbol}/quotes/latest",
            headers=_headers(),
            timeout=4.0,
        )
        if resp.status_code == 200:
            q = resp.json().get("quote", {})
            if not isinstance(q, dict):
                return None
            _bp = q.get("bp")
            _ap = q.get("ap")
            if _bp is None or _ap is None:
                return None
            # Robust parse: an empty-string or non-numeric bid/ask fails safe to None (-> suspect
            # -> carry-forward / cold-start UNKNOWN), never a crash. float("")/float("x") -> ValueError.
            try:
                bid = float(_bp)
                ask = float(_ap)
            except (TypeError, ValueError):
                return None
            if bid <= 0 or ask < bid:
                return None
            mid = 0.5 * (bid + ask)
            return {"bid": bid, "ask": ask, "mid": mid, "spread": ask - bid}
        logger.warning("GEX quote [%s]: HTTP %s", symbol, resp.status_code)
    except Exception as e:
        logger.warning("GEX quote [%s] failed: %s", symbol, e)
    return None


def _spot_is_suspect(spot: float, quote: dict | None) -> tuple[bool, dict]:
    """Cross-check a latest-trade `spot` against an independent same-instant `quote`.

    SUSPECT (returns True) when either:
      - the quote cannot validate the trade (missing, or a broken/crossed quote whose
        spread exceeds GEX_SPOT_SPREAD_SANITY_PCT of mid), OR
      - the trade diverges from mid by more than a spread-scaled, floored band:
        band = max(GEX_SPOT_BAND_SPREAD_MULT x spread, GEX_SPOT_BAND_FLOOR_PCT x mid).
    A suspect spot is never used to compute a fresh regime label (fail-safe: carry-forward
    or cold-start UNKNOWN, handled by the caller). Returns (suspect, diag)."""
    import config as _cfg
    if not getattr(_cfg, "GEX_SPOT_GUARD_ENABLED", True):
        return False, {"guard": "disabled"}
    if quote is None:
        return True, {"reason": "no_quote_reference"}
    mid = quote["mid"]
    spread = quote["spread"]
    sanity = getattr(_cfg, "GEX_SPOT_SPREAD_SANITY_PCT", 0.02)
    if mid <= 0 or spread > sanity * mid:
        return True, {"reason": "quote_unusable", "mid": round(mid, 4), "spread": round(spread, 4)}
    band = max(
        getattr(_cfg, "GEX_SPOT_BAND_SPREAD_MULT", 5.0) * spread,
        getattr(_cfg, "GEX_SPOT_BAND_FLOOR_PCT", 0.005) * mid,
    )
    dev = abs(spot - mid)
    if dev > band:
        return True, {"reason": "trade_quote_divergence", "spot": round(spot, 4),
                      "mid": round(mid, 4), "dev": round(dev, 4), "band": round(band, 4)}
    return False, {"dev": round(dev, 4), "band": round(band, 4)}


def _prior_good_entry(symbol: str) -> dict | None:
    """Return the previous snapshot's entry for `symbol` IFF it carries a real (non-error,
    non-UNKNOWN) label with a confirmed-good timestamp. Used to carry the last confirmed-good
    label forward when the current spot is suspect — PRESERVING its `confirmed_ts` so the
    STALE clock keeps aging (never reset to now, which would defeat the time backstop and
    hold a label indefinitely). Never raises."""
    try:
        if not _SNAP_PATH.exists():
            return None
        snap = json.loads(_SNAP_PATH.read_text())
        entry = snap.get("symbols", {}).get(symbol)
        if not isinstance(entry, dict) or "error" in entry:
            return None
        if entry.get("label") in (None, "UNKNOWN"):
            return None
        if not entry.get("confirmed_ts"):
            return None
        return entry
    except Exception as e:
        logger.debug("GEX [%s]: prior-good read failed: %s", symbol, e)
        return None


# ── Contract list (with open interest) ────────────────────────────────────────

def _expiry_range() -> tuple[str, str]:
    """This WEEK's expiries only — today ET through the coming Friday (the weekly
    expiry). Narrowed from the prior 21-day / 3-expiry window (Rafael 2026-07-06):
    the wide window pulled far-OTM open interest from later expiries and dragged
    the zero-gamma flip implausibly far from spot (SPY flip $670 vs $752 spot).
    This-week-only OI concentrates near spot, giving a sensible weekly GEX + flip.
    NOTE: get_gex_regime() feeds kelly.py + run_cycle Layer 8, so this changes an
    execution-consumed signal — gated as such."""
    today  = datetime.now(ET)
    friday = today + timedelta(days=(4 - today.weekday()) % 7)  # coming Fri (today if Fri)
    return today.strftime("%Y-%m-%d"), friday.strftime("%Y-%m-%d")


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


# ── Earnings implied-move (2026-08-06, QHM earnings-trim feature) ─────────────

def get_earnings_implied_move_pct(symbol: str, target_date: str) -> float | None:
    """ATM-straddle implied move for the nearest listed expiry on/after target_date, as a
    fraction of spot (e.g. 0.08 = 8%). Returns None on ANY failure (thin/no options, fetch
    error, unpaired strike, stale/wide quotes) — fail-safe; callers must have a non-option
    fallback, matching this module's existing convention throughout (T1 — Alpaca Options
    Data REST, reuses this module's own contract-fetch/snapshot helpers).

    Straddle-to-move conversion uses the standard ~1.25x factor. Derivation: under
    Black-Scholes an ATM straddle is worth approximately 0.798 * S * sigma * sqrt(T)
    (= 2/sqrt(2*pi)), so the 1-sigma expected move is straddle / 0.798 ~= 1.25 * straddle.
    The straddle premium UNDERSTATES the 1-sigma move, hence a factor > 1. See Hull,
    "Options, Futures, and Other Derivatives," on the ATM approximation. (Note: this is the
    common desk shorthand, NOT tastytrade's published expected-move formula, which is a
    weighted straddle/strangle blend — do not attribute it to them.)
    """
    try:
        spot = _get_spot(symbol)
        if not spot or spot <= 0:
            return None
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
        # Search up to 10 calendar days past target for the nearest actual listed expiry —
        # weekly/monthly chains don't always have an expiry on the exact earnings date.
        date_lo = target.isoformat()
        date_hi = (target + timedelta(days=10)).isoformat()
        oi_map = _fetch_contracts(symbol, date_lo, date_hi)
        if not oi_map:
            return None

        # Group parseable contracts by (expiry_date, strike) -> {"C": occ_sym, "P": occ_sym}
        by_expiry: dict = {}
        for occ_sym in oi_map:
            parsed = _parse_occ(occ_sym)
            if parsed is None:
                continue
            expiry_dt, is_call, strike = parsed
            expiry_date = expiry_dt.date()
            if expiry_date < target:
                continue
            by_expiry.setdefault(expiry_date, {}).setdefault(strike, {})[
                "C" if is_call else "P"
            ] = occ_sym
        if not by_expiry:
            return None
        nearest_expiry = min(by_expiry.keys())
        strikes = by_expiry[nearest_expiry]

        # Nearest strike to spot that has BOTH a call and a put listed (a real straddle pair).
        # MONEYNESS BOUND: a "nearest pair" far from spot carries intrinsic value, which would
        # inflate the straddle mid and produce a nonsense implied move. Require the chosen
        # strike to sit within 10% of spot; otherwise the chain is too sparse to trust here.
        paired_strikes = [k for k, v in strikes.items() if "C" in v and "P" in v]
        if not paired_strikes:
            return None
        atm_strike = min(paired_strikes, key=lambda k: abs(k - spot))
        if abs(atm_strike - spot) / spot > 0.10:
            return None
        call_sym = strikes[atm_strike]["C"]
        put_sym = strikes[atm_strike]["P"]

        snapshots = _fetch_snapshots([call_sym, put_sym])
        call_quote = (snapshots.get(call_sym) or {}).get("latestQuote") or {}
        put_quote = (snapshots.get(put_sym) or {}).get("latestQuote") or {}
        c_bid, c_ask = call_quote.get("bp"), call_quote.get("ap")
        p_bid, p_ask = put_quote.get("bp"), put_quote.get("ap")
        if c_bid is None or c_ask is None or p_bid is None or p_ask is None:
            return None
        c_bid, c_ask, p_bid, p_ask = float(c_bid), float(c_ask), float(p_bid), float(p_ask)
        if c_bid <= 0 or c_ask < c_bid or p_bid <= 0 or p_ask < p_bid:
            return None

        straddle_mid = 0.5 * (c_bid + c_ask) + 0.5 * (p_bid + p_ask)
        if straddle_mid <= 0:
            return None
        implied_move_pct = (straddle_mid / spot) * 1.25
        # Sanity bound: an implied move outside (0%, 100%] of spot is not a credible
        # earnings-driven move — treat as bad data rather than pass it to a caller.
        if implied_move_pct <= 0 or implied_move_pct > 1.0:
            return None
        return implied_move_pct
    except Exception as e:
        logger.warning("get_earnings_implied_move_pct[%s]: %s", symbol, e)
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
    atm_count     = 0   # surviving strikes within ±_ATM_MONEYNESS of spot (data-quality gate)
    windowed_valid = 0  # surviving valid-gamma strikes within ±_CAPTURE_WINDOW_PCTILE of spot
    skips = {"no_quote": 0, "zero_bid": 0, "wide_spread": 0, "parse": 0, "expired": 0, "no_iv": 0}
    now_et = datetime.now(ET)

    # PIN LEVEL (board 2026-07-20: quant/options seat + GAI + Gro, all aligned). The zero-gamma
    # FLIP is a ROOT — a knife-edge set by the largest ATM gamma terms, which are exactly the ~67%
    # the indicative feed censors, so it jumps discontinuously and is untrustworthy on this data.
    # We instead emit an OI-gamma-weighted CENTROID + max-|Γ·OI| WALL: a weighted AVERAGE, which
    # censoring only adds NOISE to (never a discontinuous jump). Per-contract records are retained
    # (the old code discarded them) so the pin is computed over the FRONT (nearest) expiry only,
    # fixing the per-expiry gamma collapse. This is a gamma-CONCENTRATION / pin statistic — NOT a
    # zero-gamma regime boundary and NOT a trigger. Display-only.
    _pin_recs: list = []   # (expiry_dt, strike, gdollar_abs) for surviving valid-gamma contracts

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
        # Retain the per-contract gamma-dollar magnitude + expiry + call/put flag for the pin
        # (centroid/wall + call_wall/put_wall). Weight is |Γ·OI·100·S²| = |contrib|
        # (sign-independent — concentration, not direction); is_call splits the call/put walls.
        _pin_recs.append((expiry, strike, abs(contrib), is_call))
        if abs(strike - spot) <= spot * _ATM_MONEYNESS:
            atm_count += 1
        if abs(strike - spot) <= spot * _CAPTURE_WINDOW_PCTILE:
            windowed_valid += 1

    # ── Data-quality gate (2026-07-14, board + Gro + GAI) — fail-safe to UNKNOWN ──
    # If the surviving strike set is too biased/sparse (ATM dropped by the indicative
    # feed's wide spreads), the flip AND the label are untrustworthy — both derive from
    # the same censored set. Emit UNKNOWN so the signal has ZERO execution effect rather
    # than a confident-but-wrong level. capture_ratio = valid-gamma / OI-bearing contracts.
    # Windowed denominator: OI-bearing contracts whose STRIKE is within ±_CAPTURE_WINDOW of spot.
    _cap_lo = spot * (1.0 - _CAPTURE_WINDOW_PCTILE)
    _cap_hi = spot * (1.0 + _CAPTURE_WINDOW_PCTILE)
    windowed_oi_denom = 0
    for _osym in oi_map:
        _p = _parse_occ(_osym)
        if _p is not None and _cap_lo <= _p[2] <= _cap_hi:
            windowed_oi_denom += 1
    # capture_ratio is now WINDOWED (near-spot valid-gamma / near-spot OI-bearing), not over all strikes.
    capture_ratio = (windowed_valid / windowed_oi_denom) if windowed_oi_denom else 0.0
    quality_ok = (
        atm_count >= _MIN_ATM_STRIKES
        and capture_ratio >= _MIN_CAPTURE_RATIO
        and windowed_valid >= _MIN_WINDOWED_COUNT
    )

    # Label — UNKNOWN on zero valid contracts OR insufficient data quality
    if count == 0 or not quality_ok:
        label = "UNKNOWN"
        if count > 0 and not quality_ok:
            # record WHY the otherwise-nonempty snapshot was quarantined (shadow-review audit)
            if atm_count < _MIN_ATM_STRIKES:
                skips["insufficient_atm"] = atm_count
            if capture_ratio < _MIN_CAPTURE_RATIO:
                skips["low_windowed_capture_pct"] = int(round(capture_ratio * 100))
            if windowed_valid < _MIN_WINDOWED_COUNT:
                skips["too_few_windowed"] = windowed_valid
    elif total_abs > 0 and abs(net_gex) / total_abs < _NEAR_FLIP_RATIO:
        label = "NEAR-FLIP"
    elif net_gex >= 0:
        label = "POSITIVE"
    else:
        label = "NEGATIVE"

    # Gamma flip strike — zero-crossing NEAREST spot within a ±_FLIP_WINDOW_PCTILE band.
    # (Was: lowest crossing over ALL strikes — deep-ITM put survivors dragged it ~10% below
    # spot.) Only computed when data quality is OK; else None (untrustworthy → not emitted).
    flip_strike = None
    if strike_gex and spot > 0 and quality_ok:
        _w_lo = spot * (1.0 - _FLIP_WINDOW_PCTILE)
        _w_hi = spot * (1.0 + _FLIP_WINDOW_PCTILE)
        # Accumulate the running net-GEX over ONLY the in-window (near-spot) strikes, so the
        # flip reflects the LOCAL gamma balance around spot and is immune to far deep-ITM
        # survivors (which were dragging it ~10% below spot). Pick the zero-crossing of that
        # local cumulative that is nearest spot. (GAI preship catch 2026-07-14: accumulating
        # over ALL strikes let out-of-window contributions distort where the local flip appears.)
        cumulative = 0.0
        prev_sign  = None
        _best_dist = float("inf")
        for strike in sorted(strike_gex):
            if not (_w_lo <= strike <= _w_hi):
                continue
            cumulative += strike_gex[strike]
            curr_sign   = 1 if cumulative >= 0 else -1
            if prev_sign is not None and curr_sign != prev_sign:
                _dist = abs(strike - spot)
                if _dist < _best_dist:
                    _best_dist  = _dist
                    flip_strike = round(strike, 2)
            prev_sign = curr_sign

    # ── PIN LEVEL — front-expiry OI-gamma centroid + wall + confidence (2026-07-20) ──
    # Actionable EVERY day the data allows (unlike the null-prone flip), robust to ATM censoring
    # because a weighted average degrades gracefully where a root jumps. Computed over the FRONT
    # (nearest) expiry only, so the 0DTE/weekly per-expiry collapse cannot blend it.
    pin = _compute_pin(_pin_recs, spot, oi_map)

    return {
        # raw_gex_m was ~100x mislabelled (omitted the per-1%-move x0.01 on the S^2 term), so a
        # value read as "$M" was really ~$100M of that. Corrected here to a true per-1%-move GEX in
        # $millions; the POSITIVE/NEGATIVE/NEAR-FLIP label is scale-invariant so it is unaffected.
        "raw_gex_m":       round(net_gex * 0.01 / 1_000_000, 1),  # per-1%-move GEX, $M (was ~100x)
        "label":           label,                                  # REGIME (censoring-stable sign)
        "flip_strike":     flip_strike,                            # DEPRECATED root — kept for continuity, not actionable
        "pin":             pin,                                    # ACTIONABLE pin: centroid/wall/confidence + caveats
        "contract_count":  count,
        "atm_count":       atm_count,
        "capture_ratio":   round(capture_ratio, 3),
        "quality_ok":      quality_ok,
        "skips":           skips,
        "model":           {"r": _RISK_FREE_RATE, "q": _DIV_YIELD, "max_spread": _MAX_SPREAD_RATIO},
    }


def compute_call_put_walls(records: list) -> dict:
    """Pure, source-agnostic call/put gamma-wall computation — reusable by the GEX writer
    (Alpaca BS gamma) AND options_scanner.py (Public.com greeks). The caller supplies
    per-contract tuples (strike, weight, is_call) where weight = |gamma * open_interest|
    concentration (sign-independent). Returns the strike carrying the largest CALL gamma
    concentration (call_wall — resistance / magnet cap) and the largest PUT gamma
    concentration (put_wall — support), each with its share of that side's total weight so a
    dominant wall can be weighted above a weak one. Never raises; None walls on empty input.
    NOTE: OI is T+1-stale (esp. 0DTE) — a wall INFORMS/caps a level, it is not a hard trigger."""
    out = {"call_wall": None, "put_wall": None, "call_wall_frac": None, "put_wall_frac": None}
    try:
        calls = [(s, w) for (s, w, c) in records if c and w > 0]
        puts  = [(s, w) for (s, w, c) in records if (not c) and w > 0]
        if calls:
            _tot = sum(w for _, w in calls)
            _s, _w = max(calls, key=lambda t: t[1])
            out["call_wall"]      = round(_s, 2)
            out["call_wall_frac"] = round(_w / _tot, 3) if _tot > 0 else None
        if puts:
            _tot = sum(w for _, w in puts)
            _s, _w = max(puts, key=lambda t: t[1])
            out["put_wall"]      = round(_s, 2)
            out["put_wall_frac"] = round(_w / _tot, 3) if _tot > 0 else None
    except Exception as _we:
        logger.debug("compute_call_put_walls failed (non-fatal): %s", _we)
    return out


def _compute_pin(pin_recs: list, spot: float, oi_map: dict) -> dict:
    """OI-gamma-weighted gamma-CONCENTRATION pin over the FRONT (nearest) expiry.

    Returns a self-describing dict that is NEVER a bare number (board real-capital guardrail):
      centroid   — Σ|Γ·OI|·K / Σ|Γ·OI| over front-expiry contracts (primary; degrades smoothly)
      wall       — the single strike with max |Γ·OI| gamma-dollars (secondary; can jump if the
                   peak strike is censored, hence secondary to the centroid)
      dispersion — |Γ·OI|-weighted std of K around the centroid (the uncertainty band)
      confidence — min(near-spot capture, ATM capture): pessimistic, ATM-weighted, so it TANKS
                   exactly when the ATM strikes that drive the pin are the ones censored
      atm_capture, expiry, kind, note — the caveats that travel with the level, always.

    Never raises. Returns kind="none" when there is no front-expiry data to weight."""
    try:
        if not pin_recs or spot <= 0:
            return {"kind": "none", "centroid": None, "wall": None, "call_wall": None, "put_wall": None, "dispersion": None,
                    "confidence": 0.0, "atm_capture": 0.0, "expiry": None,
                    "note": "no valid front-expiry gamma to weight — pin unavailable"}
        # Front expiry = the nearest expiration present in the surviving set.
        _front = min(r[0] for r in pin_recs)
        _front_recs = [r for r in pin_recs if r[0] == _front]
        _wsum = sum(r[2] for r in _front_recs)
        if _wsum <= 0:
            return {"kind": "none", "centroid": None, "wall": None, "call_wall": None, "put_wall": None, "dispersion": None,
                    "confidence": 0.0, "atm_capture": 0.0,
                    "expiry": _front.strftime("%Y-%m-%d"),
                    "note": "front-expiry gamma weights sum to zero — pin unavailable"}
        _centroid = sum(r[1] * r[2] for r in _front_recs) / _wsum
        _var = sum(r[2] * (r[1] - _centroid) ** 2 for r in _front_recs) / _wsum
        _dispersion = _var ** 0.5
        _wall = max(_front_recs, key=lambda r: r[2])[1]
        # Call/put walls over the SAME front-expiry set (asymmetric concentration): call_wall =
        # max call-gamma strike (resistance / magnet cap), put_wall = max put-gamma strike
        # (support). Reuses the pure source-agnostic helper so the scanner computes them identically.
        _cp_walls = compute_call_put_walls([(r[1], r[2], r[3]) for r in _front_recs])
        # Confidence — FRONT-EXPIRY-CONSISTENT (GAI preship fix 2026-07-20): both captures are
        # measured over the SAME front-expiry contracts the centroid/wall use, so the confidence
        # reflects the data quality behind THIS pin, not a blended all-expiry chain.
        #   win_capture = front-expiry surviving valid-gamma within ±_CAPTURE_WINDOW / front-expiry
        #                 OI-bearing contracts in that band
        #   atm_capture = same, restricted to ±_ATM_MONEYNESS (the ATM guardrail: overall capture
        #                 can look fine while ATM is censored — gamma lives ATM, so this must gate)
        _cap_lo = spot * (1.0 - _CAPTURE_WINDOW_PCTILE)
        _cap_hi = spot * (1.0 + _CAPTURE_WINDOW_PCTILE)
        _atm_lo = spot * (1.0 - _ATM_MONEYNESS)
        _atm_hi = spot * (1.0 + _ATM_MONEYNESS)
        _win_num = sum(1 for r in _front_recs if _cap_lo <= r[1] <= _cap_hi)
        _atm_num = sum(1 for r in _front_recs if _atm_lo <= r[1] <= _atm_hi)
        _win_oi_denom = 0
        _atm_oi_denom = 0
        for _osym in oi_map:
            _p = _parse_occ(_osym)
            if _p is None or _p[0] != _front:   # FRONT EXPIRY ONLY — consistent with the pin
                continue
            if _cap_lo <= _p[2] <= _cap_hi:
                _win_oi_denom += 1
            if _atm_lo <= _p[2] <= _atm_hi:
                _atm_oi_denom += 1
        _win_capture = (_win_num / _win_oi_denom) if _win_oi_denom else 0.0
        _atm_capture = (_atm_num / _atm_oi_denom) if _atm_oi_denom else 0.0
        _confidence = round(min(_win_capture, _atm_capture), 2)   # pessimistic, ATM-weighted
        return {
            "kind":        "centroid+wall",
            "centroid":    round(_centroid, 2),
            "wall":        round(_wall, 2),
            "dispersion":  round(_dispersion, 2),
            "confidence":  _confidence,
            "atm_capture": round(_atm_capture, 2),
            "call_wall":      _cp_walls["call_wall"],
            "put_wall":       _cp_walls["put_wall"],
            "call_wall_frac": _cp_walls["call_wall_frac"],
            "put_wall_frac":  _cp_walls["put_wall_frac"],
            "expiry":      _front.strftime("%Y-%m-%d"),
            "note":        ("gamma-concentration PIN (front-expiry) — NOT a zero-gamma flip, NOT a "
                            "trigger. OI is T+1-stale (esp. 0DTE). Confidence is ATM-weighted."),
        }
    except Exception as _pe:
        logger.warning("GEX pin computation failed (non-fatal): %s", _pe)
        return {"kind": "error", "centroid": None, "wall": None, "call_wall": None, "put_wall": None, "dispersion": None,
                "confidence": 0.0, "atm_capture": 0.0, "expiry": None,
                "note": f"pin computation error: {_pe!r}"}


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
    try:
        # calendar days to the weekly expiry; 0 on the expiry Friday itself (0DTE
        # is correct — same-day expiry means zero days remaining, not one).
        _dte = (datetime.strptime(date_lte, "%Y-%m-%d").date() - datetime.now(ET).date()).days
    except Exception as _dte_e:  # RC-3: log, don't silently swallow (display-only value)
        logger.debug("GEX: dte calc failed (date_lte=%r): %s", date_lte, _dte_e)
        _dte = None

    # Universe: market anchors + Mag-7 day-tier underlyings + current bot positions.
    # Resolve any leveraged tracker (a held TSLL/NVDL/TQQQ from _position_symbols) to its
    # UNDERLYING at WRITE time too, so refresh computes the underlying's real chain — never the
    # tracker's thin/ghost chain (which read-time resolution in get_gex_regime would skip anyway).
    # dedup collapses a tracker+underlying pair to one compute; order preserved.
    _raw_universe = _MARKET_SYMBOLS + _DAYTRADE_UNDERLYINGS + _position_symbols()
    symbols = list(dict.fromkeys(_ETF_UNDERLYING_MAP.get(_s, _s) for _s in _raw_universe))

    results: dict = {}
    for symbol in symbols:
        spot = _get_spot(symbol)
        if not spot:
            logger.warning("GEX [%s]: no spot price — skipping", symbol)
            results[symbol] = {"error": "no_spot"}
            continue

        # ── Spot-consistency guard (2026-07-26) — a SUSPECT spot never computes a fresh
        # label. Carry the last confirmed-good label forward (preserving its confirmed_ts so
        # STALE keeps aging), or UNKNOWN at cold-start (no prior good). Fail-safe: any doubt
        # about the spot degrades toward neutral, never up-sizes the book on a bad tick.
        _quote = _get_spot_quote(symbol)
        _suspect, _sdiag = _spot_is_suspect(spot, _quote)
        if _suspect:
            _prior = _prior_good_entry(symbol)
            if _prior is not None:
                _carried = dict(_prior)
                _carried["spot_suspect"] = True
                _carried["suspect_diag"] = _sdiag
                _carried["suspect_spot"] = round(spot, 2)
                # confirmed_ts is INHERITED from _prior (NOT reset) — the STALE clock ages
                # from when the label was last computed on a clean spot.
                results[symbol] = _carried
                logger.warning(
                    "GEX [%s]: SUSPECT spot (%s) — carrying last-good label=%s confirmed_ts=%s",
                    symbol, _sdiag.get("reason", "?"),
                    _carried.get("label"), _carried.get("confirmed_ts"),
                )
            else:
                results[symbol] = {
                    "error": "spot_suspect_no_prior",
                    "suspect_diag": _sdiag,
                    "suspect_spot": round(spot, 2),
                }
                logger.warning(
                    "GEX [%s]: SUSPECT spot (%s) and NO prior good label — UNKNOWN (cold-start fail-safe)",
                    symbol, _sdiag.get("reason", "?"),
                )
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

        # ISOLATE per-symbol compute (2026-08-30): a bad quote / math edge on ONE symbol must not
        # abort the whole 15-min refresh (which would blank GEX for EVERY symbol). refresh_gex runs
        # on the live_data_writer thread — NOT the run_cycle trading thread, so this can never starve
        # check_exits() — but a blanked snapshot still degrades all downstream GEX reads to UNKNOWN.
        # Per-symbol try/except keeps one bad name from taking the others down; error entry -> UNKNOWN
        # read (fail-safe). Widening the universe to the Mag-7 makes this isolation load-bearing.
        try:
            gex = _compute_gex(snapshots, oi_map, spot)
            gex["spot"]              = round(spot, 2)
            gex["contracts_fetched"] = len(oi_map)
            gex["expiry"]            = date_lte     # this week's Friday (weekly expiry)
            gex["dte"]               = _dte         # calendar days to that Friday
            gex["window"]            = "weekly"
            # confirmed_ts marks this label as computed from a spot that PASSED the consistency
            # guard at ts_str. get_gex_regime ages off this (not the snapshot write time) so a
            # carried-forward label ages from its last-clean time. quote_mid kept for the audit.
            gex["confirmed_ts"]      = ts_str
            gex["quote_mid"]         = round(_quote["mid"], 2) if _quote else None
            results[symbol]          = gex
            # INFO (was debug): this line IS the shadow-review record — must be visible
            # at production log level or the 30-session clock never accumulates evidence.
            _pin = gex.get("pin", {})
            logger.info(
                "GEX [%s]: %s $%sM | PIN centroid=%s wall=%s conf=%s (atm=%s) exp=%s | valid=%d/%d | skips=%s",
                symbol, gex["label"], gex["raw_gex_m"],
                _pin.get("centroid"), _pin.get("wall"), _pin.get("confidence"),
                _pin.get("atm_capture"), _pin.get("expiry"),
                gex["contract_count"], len(oi_map), gex["skips"],
            )
        except Exception as _sym_e:
            logger.warning("GEX [%s]: per-symbol compute failed — %s", symbol, _sym_e)
            results[symbol] = {"error": "compute_exception", "detail": str(_sym_e)[:120]}

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
    A leveraged tracker (TSLL/NVDL/TQQQ) inherits its UNDERLYING's regime (design record
    day_tier_v2 §2; board Harris+Sinclair) — its own chain is thin/ghost. No-op otherwise.
    """
    symbol = _ETF_UNDERLYING_MAP.get(symbol, symbol)
    import config as _cfg
    stale_minutes = getattr(_cfg, "GEX_STALE_MINUTES", 30)
    stale_neg     = getattr(_cfg, "GEX_STALE_MINUTES_NEG", stale_minutes)
    try:
        if not _SNAP_PATH.exists():
            return {"label": "UNKNOWN", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": None}
        snap = json.loads(_SNAP_PATH.read_text())
        ts_str   = snap.get("ts", "")
        sym_data = snap.get("symbols", {}).get(symbol, {})
        if not isinstance(sym_data, dict) or not sym_data or "error" in sym_data:
            return {"label": "UNKNOWN", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": None}
        # Age off the per-symbol confirmed_ts (the last CLEAN spot-verified compute) if present,
        # so a carried-forward label ages from its last-clean time — NOT the snapshot write time
        # (else carry-forward would reset the STALE clock each cycle and hold a label forever).
        _age_ts = sym_data.get("confirmed_ts") or ts_str
        try:
            ts_dt       = datetime.strptime(_age_ts, "%Y-%m-%d %I:%M %p PT").replace(tzinfo=PT)
            age_minutes = (datetime.now(PT) - ts_dt).total_seconds() / 60
        except Exception:
            age_minutes = 9999.0
        label = sym_data.get("label", "UNKNOWN")
        # Direction-asymmetric stale window: the risk-INCREASING NEGATIVE (x1.30) leg ages to
        # neutral faster than the base (never hold "bigger" on stale data as long as "normal").
        _eff_stale = min(stale_minutes, stale_neg) if label == "NEGATIVE" else stale_minutes
        if age_minutes > _eff_stale:
            logger.debug(
                "GEX [%s]: label=%s %.0f min old (> %d min stale threshold) — STALE",
                symbol, label, age_minutes, _eff_stale,
            )
            return {"label": "STALE", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": age_minutes}
        return {
            "label":       label,
            "raw_gex_m":   sym_data.get("raw_gex_m", 0.0),
            "flip_strike": sym_data.get("flip_strike"),
            "age_minutes": age_minutes,
        }
    except Exception as e:
        logger.warning("GEX get_gex_regime failed: %s", e)
        return {"label": "UNKNOWN", "raw_gex_m": 0.0, "flip_strike": None, "age_minutes": None}
