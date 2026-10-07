# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
strategy/day_tier_leverage.py — Track B LEVERAGED-ETF PIVOT + 10/10 CONFIDENCE (pure helpers; wired by run_day_tier.py).

WHY (Rafael CEO directive 2026-10-06): Track B's budget (~15% x 35% of equity, ~$130) cannot buy one share of
most of its mover universe (2026-10-02: TSLA +5.5% gap sized to 0 shares at $371.72). Every Track-B name has a
2x daily bull ETF on Alpaca (verified tradable 2026-10-06). Rule (Rafael 2026-10-06): a Track-B LONG trades the
STOCK whenever its budget buys 2+ shares, and switches to the 2x ETF only when the budget buys 0 or 1 share of the
stock. The budget is the normal Track-B budget, or — for a 10/10 setup — the MAXIMUM-size notional (single-name cap,
plus the thin-name cap for non-deep names); a 10/10 trade takes maximum size on whichever instrument it trades. The
account/exposure caps, the daily day-tier dollar budget, the protective stop, the EOD force-flat and the 7% account
kill bound every path.
SHORTS (Rafael 2026-10-07; design record day_tier_inverse_etf_route_2026-10-07.md): a SHORT signal BUYS the stock's
inverse ETF (a long position in the ETF: no borrow, no co-hold) when another tier holds the stock, or when the budget
buys 0-1 shares; otherwise it shorts the stock itself. Only names with a liquid inverse are mapped; every pivot is
re-checked live (spread, today's volume, tracking) and skipped when any check fails.

10/10 CONFIDENCE (all must hold; the runner already enforces the first four before it gets here):
  mover screen PASS (gap + RVOL) . momentum ENTER (volume-confirmed) . the stock's daily trend side agrees .
  2m AND 5m EMA + VWAP agree . AND the 15m EMA and VWAP agree too (informational in the gate; required here).

STOP TRANSLATION (exact daily-reset form, board Harris 2026-10-07): a k-x daily fund's value since the stock's prior
close C is 1 + k(P/C - 1) (bull) or 1 - k(P/C - 1) (inverse). Moving the stock from its live price U to its structural
level L therefore moves the fund by +-[k(L-U)/C] / (1 +- k(U-C)/C), so the ETF stop reference = E x (1 +- that), with E
the ETF's LIVE price. Without a usable C it falls back to the first-order E x (1 +- k(L/U - 1)) (exact when the stock
is flat on the day). With NVDA -5% on the day the first-order stop sat ~0.3% past the structural level. place_entry then applies its usual wall buffer, min-stop-room gate, wire-time caps,
daily dollar budget, OCO stop + 2R target and EOD force-flat to the ETF symbol — every order/reconcile/flatten path
is keyed on the ordered symbol, so the ETF lot is managed exactly like any day-tier lot.

FAIL-SAFE: any missing/stale price, unmapped symbol, invalid geometry or error -> no pivot (None); the caller then
keeps today's behaviour (the stock, sized by its budget). Never raises.
"""
from __future__ import annotations

import logging
import math

import config

logger = logging.getLogger(__name__)

# 2x daily BULL ETF per Track-B underlying (Alpaca /v2/assets: tradable + active, checked 2026-10-06).
_DEFAULT_BULL_2X = {
    "TSLA": "TSLL", "NVDA": "NVDL", "META": "METU", "AMD": "AMDL", "AMZN": "AMZU", "AAPL": "AAPU",
    "MSFT": "MSFU", "GOOGL": "GGLL", "AVGO": "AVL", "NFLX": "NFXL", "MU": "MUU", "COIN": "CONL",
    "PLTR": "PLTU", "SMCI": "SMCL", "UBER": "UBRL",
}
# Second 2x bull ETF on the same stock (also verified tradable 2026-10-06), used when the primary ETF is already held
# by another tier: a co-held ETF could be closed whole by the main bot, taking the day tier's shares with it and
# leaving its OCO stop resting with no position (risk seat 2026-10-06). Names with no second ETF are not pivoted then.
_DEFAULT_BULL_2X_ALT = {"TSLA": "TSLT", "NVDA": "NVDU", "META": "FBL", "AVGO": "AVGX", "SMCI": "SMCX"}
_LEVERAGE = 2.0  # every mapped BULL ETF is a 2x DAILY product
# Inverse ETF per underlying -> (symbol, daily leverage k). Alpaca tradable + active and 10/06 IEX-only volume checked
# 2026-10-07: NVD 17.9M, PLTD 3.9M, AVS 2.5M, AMZD 1.5M, AAPD 426K, MUD 309K, MSFD 101K, TSLQ 54K; alternates NVDQ 441K,
# TSLZ 26K. Not mapped (too thin): GGLS (GOOGL), NFXS (NFLX), METD (META), AMDD (AMD), CONI (COIN).
_DEFAULT_INVERSE = {
    "NVDA": ("NVD", 2.0), "PLTR": ("PLTD", 1.0), "AVGO": ("AVS", 1.0), "AMZN": ("AMZD", 1.0),
    "AAPL": ("AAPD", 1.0), "MU": ("MUD", 1.0), "MSFT": ("MSFD", 1.0), "TSLA": ("TSLQ", 2.0),
}
_DEFAULT_INVERSE_ALT = {"NVDA": ("NVDQ", 2.0), "TSLA": ("TSLZ", 2.0)}
# Live liquidity / tracking gates on a pivoted ETF (board Harris + Taleb, Gro, GAI 2026-10-07). PROV starting values.
_INV_MAX_SPREAD_PCT = 0.005      # PROV:inverse-route-2026-10-07 — quoted spread <= 0.5% of mid ...
_INV_MAX_SPREAD_OF_STOP = 0.25   # PROV:inverse-route-2026-10-07 — ... and <= 25% of the stop distance
_INV_MIN_TODAY_VOL = 50_000      # PROV:inverse-route-2026-10-07 — IEX shares over a FULL session; prorated by the
                                 # minutes elapsed since 09:30 ET (adversarial 2026-10-07: a fixed floor starved
                                 # TSLQ ~54K/day and MSFD ~101K/day all morning)
_INV_MIN_VOL_X_QTY = 100         # PROV:inverse-route-2026-10-07 — today's IEX shares >= 100x the order
_INV_MIN_STOP_TICKS = 5          # PROV:inverse-route-2026-10-07 — stop >= 5 ticks from entry ($0.01 tick)
_INV_TRACK_TOL_PCT = 0.005       # PROV:inverse-route-2026-10-07 — ETF move vs implied move tolerance (or 3 ticks)
_TICK = 0.01


def pivot_enabled() -> bool:
    """Kill flag (Rule D): config.DAYTRADE_LEVERAGED_PIVOT_ENABLED, default ON. Only an explicit False disables."""
    return getattr(config, "DAYTRADE_LEVERAGED_PIVOT_ENABLED", True) is not False


def bull_etf_for(symbol: str) -> "str | None":
    """The 2x bull ETF for `symbol`, or None. config.DAYTRADE_LEVERAGED_BULL_MAP overrides the default map."""
    try:
        m = getattr(config, "DAYTRADE_LEVERAGED_BULL_MAP", None)
        if not isinstance(m, dict) or not m:
            m = _DEFAULT_BULL_2X
        etf = m.get(str(symbol).strip().upper())
        return str(etf).strip().upper() if etf else None
    except Exception:  # noqa: BLE001
        return None


def etf_for_order(symbol: str, held: "set | None") -> "str | None":
    """The 2x bull ETF to trade for `symbol`, skipping any ETF already held by ANOTHER tier (`held` = symbols with a
    live broker position the day tier does not own). Primary first, then the alternate; None when both are held,
    unmapped, or `held` is unknown (None -> unknown book -> no pivot, fail-safe). Never raises."""
    try:
        if held is None:
            return None
        for cand in (bull_etf_for(symbol), _DEFAULT_BULL_2X_ALT.get(str(symbol).strip().upper())):
            if cand and cand not in held:
                return cand
        return None
    except Exception:  # noqa: BLE001
        return None


def inverse_pivot_enabled() -> bool:
    """Kill flag (Rule D): config.DAYTRADE_INVERSE_PIVOT, default ON. Only an explicit False disables."""
    return getattr(config, "DAYTRADE_INVERSE_PIVOT", True) is not False


def inverse_etf_for_order(symbol: str, held: "set | None") -> "tuple[str, float] | None":
    """(inverse ETF, k) to BUY for a short signal on `symbol`, skipping any ETF another tier holds. Primary first,
    then the alternate; None when unmapped, both held, or `held` is unknown (fail-safe). Never raises."""
    try:
        if held is None:
            return None
        sym = str(symbol).strip().upper()
        for cand in (_DEFAULT_INVERSE.get(sym), _DEFAULT_INVERSE_ALT.get(sym)):
            if cand and cand[0] not in held:
                return cand[0], float(cand[1])
        return None
    except Exception:  # noqa: BLE001
        return None


def exposure_sign(symbol: str, side: str) -> "tuple[str, int]":
    """(underlying, +1 long / -1 short) of a day-tier position: a stock long +1 / short -1; a bull ETF long +1 on its
    underlying; an inverse ETF long -1 on its underlying. Used to keep ONE direction per underlying (board Taleb:
    never hold NVDL and NVD, or NVDA and NVD, at once). Never raises."""
    try:
        sym = str(symbol).strip().upper()
        sgn = 1 if str(side or "long").lower() == "long" else -1
        for und, etf in list(_DEFAULT_BULL_2X.items()) + list(_DEFAULT_BULL_2X_ALT.items()):
            if etf == sym:
                return und, sgn
        for und, (etf, _k) in list(_DEFAULT_INVERSE.items()) + list(_DEFAULT_INVERSE_ALT.items()):
            if etf == sym:
                return und, -sgn
        return sym, sgn
    except Exception:  # noqa: BLE001
        return str(symbol), 1


def etf_stop_level(etf_px, underlying_px, level, k: float, inverse: bool, prior_close=None) -> "float | None":
    """ETF stop reference for a stock structural level (see STOP TRANSLATION). None when inputs/geometry are not
    usable. Never raises."""
    try:
        e, u, lv = _pos(etf_px), _pos(underlying_px), _pos(level)
        kk = float(k)
        if e is None or u is None or lv is None or not (kk > 0):
            return None
        sgn = -1.0 if inverse else 1.0
        c = _pos(prior_close)
        if c is not None:
            den = 1.0 + sgn * kk * (u - c) / c
            if den <= 0:
                return None
            move = sgn * kk * (lv - u) / c / den
        else:
            move = sgn * kk * (lv / u - 1.0)
        out = e * (1.0 + move)
        return out if math.isfinite(out) else None
    except Exception:  # noqa: BLE001
        return None


def is_ten_of_ten(screen: dict, mom: dict, gate: dict) -> "tuple[bool, str]":
    """10/10 confidence for a Track-B LONG (see module docstring). Returns (ok, reason). Never raises."""
    try:
        if not (isinstance(screen, dict) and screen.get("is_mover")):
            return False, "not a screened mover"
        if not (isinstance(mom, dict) and mom.get("trigger") == "ENTER" and mom.get("vol_confirmed")):
            return False, "momentum ENTER not volume-confirmed"
        if not (isinstance(gate, dict) and gate.get("ok") and not gate.get("counter_trend")):
            return False, "daily trend / 2m+5m alignment not confirmed"
        al = gate.get("alignment") or {}
        checks = al.get("checks") or {}
        info = al.get("info") or {}
        need = ("ema2", "vwap2", "ema5", "vwap5")
        if not all(checks.get(k) is True for k in need):
            return False, "2m/5m checks not all true"
        if not (info.get("ema15") is True and info.get("vwap15") is True):
            return False, "15m EMA/VWAP not aligned"
        return True, "10/10: mover + volume-confirmed momentum + daily trend + 2m/5m/15m EMA+VWAP aligned"
    except Exception as e:  # noqa: BLE001
        return False, f"10/10 check error: {e!r}"


def _pos(x) -> "float | None":
    try:
        v = float(x)
        return v if (math.isfinite(v) and v > 0) else None
    except (TypeError, ValueError, OverflowError):
        return None


def leveraged_entry(decision: dict, trigger: dict, etf: str, underlying_px, etf_px,
                    prior_close=None) -> "tuple[dict, dict] | None":
    """Re-express a LONG ENTER on its 2x bull ETF. Returns (decision, trigger) for place_entry with
    symbol/entry_ref/wall_ref on the ETF, or None when the pivot cannot be done safely. `prior_close` (the stock's
    prior session close) enables the exact daily-reset stop; None -> first-order. Never raises."""
    try:
        if not (isinstance(trigger, dict) and trigger.get("trigger") == "ENTER" and trigger.get("direction") == "long"):
            return None
        u = _pos(underlying_px)
        e = _pos(etf_px)
        level = _pos(trigger.get("wall_ref"))
        if u is None or e is None or level is None or not etf:
            return None
        if level >= u:
            return None  # the stock already trades at/below its structural level: the long setup is gone
        etf_level = etf_stop_level(e, u, level, _LEVERAGE, inverse=False, prior_close=prior_close)
        if etf_level is None:
            return None
        frac = etf_level / e - 1.0  # negative for a long
        if not (-1.0 < frac < 0.0) or not (0 < etf_level < e):
            return None  # a >=100% ETF stop / wrong-side stop is not a sane day-tier stop
        sym_u = str(trigger.get("symbol") or decision.get("symbol") or "")
        t2 = {**trigger, "symbol": etf, "entry_ref": round(e, 4), "wall_ref": round(etf_level, 4),
              "target": None, "underlying": sym_u, "underlying_entry_ref": u, "underlying_wall_ref": level,
              "leverage": _LEVERAGE,
              "reason": f"{trigger.get('reason', '')} | LEVERAGED PIVOT {sym_u}->{etf} (2x): stock {u:.2f} stop-ref "
                        f"{level:.2f} ({(level / u - 1.0):+.2%}) -> ETF {e:.4f} stop-ref {etf_level:.4f} ({frac:+.2%})"}
        d2 = {**decision, "symbol": etf, "underlying": sym_u, "leverage": _LEVERAGE}
        return d2, t2
    except Exception as ex:  # noqa: BLE001
        logger.warning("[%s] leveraged pivot error (no pivot): %s", etf, ex)
        return None


def inverse_entry(decision: dict, trigger: dict, etf: str, k: float, underlying_px, etf_px,
                  prior_close=None) -> "tuple[dict, dict] | None":
    """Re-express a SHORT ENTER as a BUY of the stock's inverse ETF: the order side is LONG with the stop BELOW the
    ETF's entry (a stock rising to its stop level L drives the inverse ETF down). The records carry
    signal_direction=short, instrument=inverse_etf, underlying and k so research scores it as a short signal on the
    stock. None when the route cannot be done safely (the caller then keeps today's behaviour). Never raises."""
    try:
        if not (isinstance(trigger, dict) and trigger.get("trigger") == "ENTER" and trigger.get("direction") == "short"):
            return None
        u = _pos(underlying_px)
        e = _pos(etf_px)
        level = _pos(trigger.get("wall_ref"))
        if u is None or e is None or level is None or not etf:
            return None
        if level <= u:
            return None  # the stock already trades at/above its structural level: the short setup is gone
        etf_level = etf_stop_level(e, u, level, k, inverse=True, prior_close=prior_close)
        if etf_level is None:
            return None
        frac = etf_level / e - 1.0
        if not (-1.0 < frac < 0.0) or not (0 < etf_level < e):
            return None
        sym_u = str(trigger.get("symbol") or decision.get("symbol") or "")
        extra = {"underlying": sym_u, "leverage": float(k), "signal_direction": "short", "instrument": "inverse_etf"}
        t2 = {**trigger, "symbol": etf, "direction": "long", "entry_ref": round(e, 4), "wall_ref": round(etf_level, 4),
              "target": None, "underlying_entry_ref": u, "underlying_wall_ref": level,
              "underlying_prior_close": _pos(prior_close), **extra,
              "reason": f"{trigger.get('reason', '')} | INVERSE ROUTE {sym_u} short -> BUY {etf} ({k:g}x inverse): "
                        f"stock {u:.2f} stop-ref {level:.2f} ({(level / u - 1.0):+.2%}) -> ETF {e:.4f} stop-ref "
                        f"{etf_level:.4f} ({frac:+.2%})"}
        d2 = {**decision, "symbol": etf, **extra}
        return d2, t2
    except Exception as ex:  # noqa: BLE001
        logger.warning("[%s] inverse route error (no route): %s", etf, ex)
        return None


def etf_liquidity_ok(quote, today_volume, order_qty, entry_px, stop_px,
                     session_frac: float = 1.0) -> "tuple[bool, str]":
    """Live liquidity gate on a pivoted ETF: a usable uncrossed quote with spread <= min(0.5% of mid, 25% of the stop
    distance); today's IEX volume >= 50K shares x the session fraction elapsed (`session_frac`, 0-1; expected-by-now)
    and >= 100x the order; the stop >= 5 ticks away. Fail-safe False on any missing input. Never raises."""
    try:
        if not isinstance(quote, dict):
            return False, "no live quote"
        bid, ask = _pos(quote.get("bid")), _pos(quote.get("ask"))
        e, st = _pos(entry_px), _pos(stop_px)
        if bid is None or ask is None or ask < bid or e is None or st is None:
            return False, "quote unusable (missing/crossed) or no entry/stop"
        dist = abs(e - st)
        if dist < _INV_MIN_STOP_TICKS * _TICK - 1e-9:
            return False, f"stop {dist:.4f} < {_INV_MIN_STOP_TICKS} ticks"
        spread, mid = ask - bid, (ask + bid) / 2.0
        if spread > _INV_MAX_SPREAD_PCT * mid + 1e-9 or spread > _INV_MAX_SPREAD_OF_STOP * dist + 1e-9:
            return False, f"spread {spread:.4f} too wide (mid {mid:.4f}, stop distance {dist:.4f})"
        vol = float(today_volume) if today_volume is not None else -1.0
        q = max(1, int(order_qty or 1))
        frac = min(1.0, max(0.0, float(session_frac)))
        floor = _INV_MIN_TODAY_VOL * frac
        if not (math.isfinite(vol) and vol >= floor and vol >= _INV_MIN_VOL_X_QTY * q):
            return False, f"today's IEX volume {vol:.0f} < max({floor:.0f} expected by now, {_INV_MIN_VOL_X_QTY}x{q})"
        return True, f"liquid: spread {spread:.4f} ({spread / mid:.2%}), today vol {vol:.0f}"
    except Exception as ex:  # noqa: BLE001
        return False, f"liquidity check error: {ex!r}"


def etf_tracking_ok(etf_px, etf_prior_close, underlying_px, underlying_prior_close, k: float,
                    inverse: bool) -> "tuple[bool, str]":
    """The ETF's move since its prior close must match the implied +-k x the stock's move within max(0.5%, 3 ticks)
    (a premium / stale quote / halt dislocation proxy — no intraday NAV feed exists in the data tiers). Fail-safe
    False on any missing input. Never raises."""
    try:
        e, ep, u, c = _pos(etf_px), _pos(etf_prior_close), _pos(underlying_px), _pos(underlying_prior_close)
        if e is None or ep is None or u is None or c is None:
            return False, "tracking inputs missing"
        implied = (-1.0 if inverse else 1.0) * float(k) * (u / c - 1.0)
        actual = e / ep - 1.0
        tol = max(_INV_TRACK_TOL_PCT, 3 * _TICK / e)
        if abs(actual - implied) > tol:
            return False, f"tracking off: ETF {actual:+.2%} vs implied {implied:+.2%} (tol {tol:.2%})"
        return True, f"tracking ok: ETF {actual:+.2%} vs implied {implied:+.2%}"
    except Exception as ex:  # noqa: BLE001
        return False, f"tracking check error: {ex!r}"
