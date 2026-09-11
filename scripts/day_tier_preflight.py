#!/usr/bin/env python3
# ruff: noqa: E501,E402  — E501: table literals; E402: imports follow the sys.path/.env bootstrap (cron self-auth)
"""
scripts/day_tier_preflight.py — READ-ONLY preflight simulation of the day-tier decision path.

WHY (Rafael 2026-09-07 + "deployed ≠ ready, run a sim first"): the day-tier placed ZERO trades on
Fri 2026-09-04 and it was only found DURING market hours, because nothing simulated the decision path
before the open. This mirrors `run_day_tier.py`'s live scan loop VERBATIM — for each
config.DAYTRADE_UNIVERSE symbol it calls the SAME functions the runner calls
(compute_day_tier_decision → compute_entry_trigger → compute_day_tier_size) — but STOPS before
`place_entry`. It submits NO orders and mutates NO state. Reusing the live functions means the sim
cannot drift from production behavior; it just reports, per symbol, whether the tier WOULD enter and,
if not, the FIRST gate that stopped it and why.

WHEN TO RUN: GEX is refreshed only DURING RTH (data.gex.refresh_gex via live_data_writer, ~every 15 min),
so it reads STALE pre-market, on holidays, and on weekends — then every symbol stands down (correctly),
which only proves the path is WIRED, not that it will trade. For a real go/no-go, run it SHORTLY AFTER THE
OPEN (≈9:35–9:50 ET) once refresh_gex has run at least once and GEX has resolved to a live regime.

Design: logs/design_records/day_tier_preflight_2026-09-07.md
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_ROOT / ".env")
except Exception as _e:
    logging.getLogger("day_tier_preflight").debug("dotenv load skipped: %s", _e)

import config
from strategy.day_tier_decision import compute_day_tier_decision
from strategy.day_tier_entry_trigger import compute_entry_trigger
from strategy.day_tier_sizing import compute_day_tier_size

logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("day_tier_preflight")


def _fetch_risk_snapshot() -> dict:
    """Read-only account/book snapshot used by the same wire-time sizing guard as production."""
    try:
        from execution import broker
        from strategy import day_tier_logger
        acct = broker.get_account()
        positions = broker.get_open_positions()
        orders = broker.get_open_orders()
        last_equity_raw = getattr(acct, "last_equity", None)
        if last_equity_raw is None:
            raise ValueError("account last_equity is missing")
        return {
            "equity": float(getattr(acct, "equity", 0.0) or 0.0),
            "last_equity": float(last_equity_raw),
            "buying_power": float(getattr(acct, "buying_power", 0.0) or 0.0),
            "maintenance_margin": float(getattr(acct, "maintenance_margin", 0.0) or 0.0),
            "positions": {getattr(p, "symbol", None): p for p in (positions or [])},
            "orders": orders,
            "open_trades": day_tier_logger.open_trades_from_log(),
        }
    except Exception as e:
        logger.warning("risk snapshot failed (%s) — wire-time preflight will fail closed", e)
        return {"equity": 0.0, "last_equity": 0.0, "buying_power": 0.0, "maintenance_margin": 0.0,
                "positions": {}, "orders": None, "open_trades": {}}


def _trunc(s, n: int = 80) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def evaluate_universe(universe, equity: float, buying_power: float = 0.0,
                      risk_snapshot: dict | None = None) -> list:
    """Run the live decision→trigger→size pipeline read-only for each symbol; return per-symbol rows.
    Mirrors run_day_tier.py's loop (decision.would_consider → trigger==ENTER → size.size_ok) but never
    calls place_entry. Track A sizes off buying_power (BP sizing 2026-09-08)."""
    rows = []
    for sym in universe:
        row = {"symbol": sym, "would_consider": None, "act_ok": None, "gex_label": None,
               "trigger": None, "size_ok": None, "shares": None, "stop": None, "reason": ""}
        try:
            d = compute_day_tier_decision(sym) or {}   # a malformed/None return raises on .get → caught below
            row["would_consider"] = bool(d.get("would_consider"))
            row["act_ok"] = bool(d.get("act_ok"))
            row["gex_label"] = d.get("gex_label")
            if not row["would_consider"]:
                row["stop"] = "would_consider=False"
                row["reason"] = _trunc(d.get("reason"))
                rows.append(row)
                continue

            t = compute_entry_trigger(sym, d)
            t = t if isinstance(t, dict) else {}
            row["trigger"] = t.get("trigger")
            if t.get("trigger") != "ENTER":
                row["stop"] = "trigger!=ENTER"
                row["reason"] = _trunc(t.get("reason") or t.get("trigger"))
                rows.append(row)
                continue

            z = compute_day_tier_size(sym, d, t.get("entry_ref"), equity,
                                      buying_power=buying_power, track="A")
            z = z if isinstance(z, dict) else {}
            row["size_ok"] = bool(z.get("size_ok"))
            row["shares"] = z.get("shares")
            if not z.get("size_ok"):
                row["stop"] = "size_ok=False"
                row["reason"] = _trunc(z.get("reason"))
                rows.append(row)
                continue

            if risk_snapshot is not None:
                from execution import broker, day_trade_manager as dtm
                direction = t.get("direction")
                entry_ref_raw = t.get("entry_ref")
                if direction not in ("long", "short") or entry_ref_raw is None:
                    row["stop"] = "WIRE_RISK_UNKNOWN"
                    row["reason"] = "direction/entry reference unavailable — fail closed"
                    rows.append(row)
                    continue
                entry_ref = float(entry_ref_raw)
                stop_px = dtm._compute_stop_price(t, direction, entry_ref)
                slip = float(getattr(config, "DAYTRADE_ENTRY_SLIPPAGE_PCT", 0.002))
                limit_px = round(entry_ref * (1.0 + slip) if direction == "long"
                                 else entry_ref * (1.0 - slip), 2)
                rate = broker.get_asset_maintenance_margin_rate(sym)
                if stop_px is None or rate is None or risk_snapshot.get("orders") is None:
                    row["stop"] = "WIRE_RISK_UNKNOWN"
                    row["reason"] = "stop/order-book/maintenance data unavailable — fail closed"
                    rows.append(row)
                    continue
                safe_qty, wire_reason = dtm._bounded_entry_qty(
                    int(z.get("shares") or 0), limit_px, stop_px, equity,
                    risk_snapshot.get("open_trades", {}), risk_snapshot.get("positions", {}),
                    buying_power, float(risk_snapshot.get("maintenance_margin", 0.0)),
                    rate, risk_snapshot.get("orders", []),
                    risk_equity=float(risk_snapshot.get("last_equity", 0.0)),
                )
                row["shares"] = safe_qty
                if safe_qty < 1:
                    row["stop"] = "WIRE_CAP"
                    row["reason"] = _trunc(wire_reason)
                    rows.append(row)
                    continue

            row["stop"] = "WOULD_ENTER"
            row["reason"] = _trunc(t.get("reason"))
            rows.append(row)
        except Exception as e:                       # a diagnostic must never crash mid-universe
            row["stop"] = "ERROR"
            row["reason"] = _trunc(repr(e))
            rows.append(row)
    return rows


def main() -> int:
    as_json = "--json" in sys.argv[1:]
    universe = list(getattr(config, "DAYTRADE_UNIVERSE", []))
    if not universe:
        print("DAYTRADE_UNIVERSE is empty — nothing to simulate.")
        return 0
    risk_snapshot = _fetch_risk_snapshot()
    equity = float(risk_snapshot.get("equity", 0.0))
    buying_power = float(risk_snapshot.get("buying_power", 0.0))
    rows = evaluate_universe(universe, equity, buying_power, risk_snapshot=risk_snapshot)

    would_enter = [r for r in rows if r["stop"] == "WOULD_ENTER"]
    stops = Counter(r["stop"] for r in rows)

    if as_json:
        print(json.dumps({"equity": equity, "buying_power": buying_power, "universe": len(universe),
                          "would_enter": [r["symbol"] for r in would_enter],
                          "stops": dict(stops), "rows": rows}, default=str))
        return 0

    print(f"DAY-TIER PREFLIGHT — equity=${equity:,.2f} · BP=${buying_power:,.2f} · {len(universe)} symbols "
          f"(DAYTRADE_ENABLED={getattr(config, 'DAYTRADE_ENABLED', '?')})")
    print(f"{'SYM':<6}{'would?':<8}{'act_ok':<8}{'gex':<10}{'trigger':<9}{'size_ok':<9}{'stop':<21}reason")
    for r in rows:
        print(f"{r['symbol']:<6}{str(r['would_consider']):<8}{str(r['act_ok']):<8}"
              f"{str(r['gex_label']):<10}{str(r['trigger']):<9}{str(r['size_ok']):<9}"
              f"{str(r['stop']):<21}{r['reason']}")
    print(f"\nSUMMARY: {len(would_enter)}/{len(universe)} WOULD ENTER"
          + (f"  ({', '.join(r['symbol'] for r in would_enter)})" if would_enter else "")
          + f"  ·  stops: {dict(stops)}")
    if not would_enter:
        top = stops.most_common(1)[0][0] if stops else "?"
        print(f"NO-GO: dominant block = '{top}'. If gex STALE/UNKNOWN on a trading day pre-market, the "
              "GEX regime has not resolved (see the _expiry_range Friday-0DTE issue for expiry Fridays).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
