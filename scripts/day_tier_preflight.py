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


def _fetch_equity() -> float:
    """Read-only account equity for the sizing step. 0.0 if unavailable (sizing then reports so)."""
    try:
        from reporting import pnl_ledger as pl
        acct = pl.fetch_account() or {}
        return float(acct.get("equity", 0.0) or 0.0)
    except Exception as e:
        logger.warning("equity fetch failed (%s) — sizing step will report equity_unavailable", e)
        return 0.0


def _trunc(s, n: int = 80) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def evaluate_universe(universe, equity: float) -> list:
    """Run the live decision→trigger→size pipeline read-only for each symbol; return per-symbol rows.
    Mirrors run_day_tier.py's loop (decision.would_consider → trigger==ENTER → size.size_ok) but never
    calls place_entry."""
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

            z = compute_day_tier_size(sym, d, t.get("entry_ref"), equity, track="A")
            z = z if isinstance(z, dict) else {}
            row["size_ok"] = bool(z.get("size_ok"))
            row["shares"] = z.get("shares")
            if not z.get("size_ok"):
                row["stop"] = "size_ok=False"
                row["reason"] = _trunc(z.get("reason"))
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
    equity = _fetch_equity()
    rows = evaluate_universe(universe, equity)

    would_enter = [r for r in rows if r["stop"] == "WOULD_ENTER"]
    stops = Counter(r["stop"] for r in rows)

    if as_json:
        print(json.dumps({"equity": equity, "universe": len(universe),
                          "would_enter": [r["symbol"] for r in would_enter],
                          "stops": dict(stops), "rows": rows}, default=str))
        return 0

    print(f"DAY-TIER PREFLIGHT — equity=${equity:,.2f} · {len(universe)} symbols "
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
