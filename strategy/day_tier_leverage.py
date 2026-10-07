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
Shorts are unchanged (no pivot): only some names have a 2x inverse ETF.

10/10 CONFIDENCE (all must hold; the runner already enforces the first four before it gets here):
  mover screen PASS (gap + RVOL) . momentum ENTER (volume-confirmed) . the stock's daily trend side agrees .
  2m AND 5m EMA + VWAP agree . AND the 15m EMA and VWAP agree too (informational in the gate; required here).

STOP TRANSLATION: the momentum stop is the stock's structural level L. With the stock's LIVE price U, the stop sits
(L/U - 1) away; a 2x daily ETF moves ~2x the stock intraday, so the ETF stop reference = E x (1 + 2 x (L/U - 1)),
with E the ETF's LIVE price. place_entry then applies its usual wall buffer, min-stop-room gate, wire-time caps,
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
_LEVERAGE = 2.0  # every mapped ETF is a 2x DAILY product


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


def leveraged_entry(decision: dict, trigger: dict, etf: str, underlying_px, etf_px) -> "tuple[dict, dict] | None":
    """Re-express a Track-B LONG momentum ENTER on its 2x ETF. Returns (decision, trigger) for place_entry with
    symbol/entry_ref/wall_ref on the ETF, or None when the pivot cannot be done safely. Never raises."""
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
        frac = _LEVERAGE * (level / u - 1.0)  # negative for a long
        if not (-1.0 < frac < 0.0):
            return None  # a >=50% stock stop would be a >=100% ETF stop — not a sane day-tier stop
        etf_level = e * (1.0 + frac)
        if not (0 < etf_level < e):
            return None
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
