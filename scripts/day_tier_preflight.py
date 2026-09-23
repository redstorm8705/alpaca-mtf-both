#!/usr/bin/env python3
# ruff: noqa: E501,E402,E702  — E501: table literals; E402: imports follow the sys.path/.env bootstrap (cron self-auth); E702: compact per-row diagnostic assignments
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

TRACK B: evaluated only inside its ~09:50-11:05 ET trigger window. To exercise the full Track-B path pre-market,
replay a past session read-only:  python3 scripts/day_tier_preflight.py --asof 2026-09-22T10:15   (ET; completed
bars only; the wire-time cap uses the CURRENT account snapshot; the live-quote min-stop gate is skipped in replay).

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
                    symbol=sym,  # parity with place_entry (deep-liquidity carve-out) — was omitted (sim drift)
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


def evaluate_track_b(equity: float, buying_power: float = 0.0,
                     risk_snapshot: dict | None = None, now_et=None) -> list:
    """Read-only Track-B (dynamic movers) pipeline per pre-registered symbol — mirrors run_day_tier.py's
    Track-B loop (track_b_in_window -> build_session_frame -> settled daily context -> screen_mover ->
    momentum trigger -> adapter -> compute_day_tier_size(track="B") -> optional wire-time cap incl. the
    Track-B exposure cap) but NEVER calls place_entry. Reuses the live functions so the sim cannot drift
    from production. Reports, per symbol, the FIRST gate that stopped it. `now_et` (ET-aware) replays a past
    session (--asof) — the frame + daily context are then historical reads; the wire-time cap still uses
    the CURRENT account snapshot."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    from strategy import day_tier_track_b as tb
    from strategy.day_tier_momentum_trigger import compute_momentum_trigger
    from run_day_tier import _track_b_daily_context
    et = _ZI("America/New_York")
    replay = now_et is not None
    now_et = now_et or _dt.now(et)
    in_window = tb.track_b_in_window(now_et)
    used_today: set = set()
    if not replay:  # mirror the runner's one-shot-per-symbol-per-day rule (read-only state read)
        try:
            from execution import day_trade_manager as _dtm
            from run_day_tier import _track_b_symbols_today
            used_today = _track_b_symbols_today(_dtm._load_state(), now_et.strftime("%Y%m%d"))
        except Exception as e:  # a diagnostic must never crash; report it
            logger.warning("preflight: per-day Track-B state read failed: %s", e)
    rows = []
    for sym in tb.track_b_universe():
        row = {"symbol": sym, "is_mover": None, "gap": None, "rvol": None, "trigger": None,
               "size_ok": None, "shares": None, "stop": "", "reason": ""}
        if not in_window:
            row["stop"] = "outside_window"
            row["reason"] = (f"{now_et:%H:%M} ET is outside the trigger window (~09:50-11:05 ET) — the runner "
                             "skips Track B; replay a session with --asof YYYY-MM-DDTHH:MM (ET)")
            rows.append(row); continue
        if sym in used_today:
            row["stop"] = "used_today"; row["reason"] = "Track-B shot already used for this symbol today"
            rows.append(row); continue
        try:
            frame = tb.build_session_frame(sym, now_et)
            if frame is None:
                row["stop"] = "no_frame"
                row["reason"] = "no from-open / contiguous / fresh RTH 5m frame (pre-open, halt, or stale)"
                rows.append(row); continue
            prior_close, avg_vol = _track_b_daily_context(sym, now_et, write_cache=False)  # read-only
            if prior_close is None:
                row["stop"] = "no_daily"; row["reason"] = "daily context unavailable (split-adj SIP close / IEX ADV / prev-session check)"
                rows.append(row); continue
            screen = tb.screen_mover(sym, frame, prior_close, avg_vol, now_et)
            row["is_mover"] = bool(screen.get("is_mover"))
            row["gap"] = screen.get("gap_pct"); row["rvol"] = screen.get("rvol")
            if not screen.get("is_mover"):
                row["stop"] = "not_mover"; row["reason"] = _trunc(screen.get("reason"))
                rows.append(row); continue
            mom = compute_momentum_trigger(sym, screen.get("gap_direction"), frame)
            row["trigger"] = mom.get("trigger")
            if mom.get("trigger") != "ENTER":
                row["stop"] = "no_trigger"; row["reason"] = _trunc(mom.get("reason"))
                rows.append(row); continue
            d, t = tb.momentum_to_entry(mom, screen.get("gap_direction"))
            z = compute_day_tier_size(sym, d, t.get("entry_ref"), equity, buying_power=buying_power, track="B")
            z = z if isinstance(z, dict) else {}
            row["size_ok"] = bool(z.get("size_ok")); row["shares"] = z.get("shares")
            if not z.get("size_ok"):
                row["stop"] = "size_ok=False"; row["reason"] = _trunc(z.get("reason"))
                rows.append(row); continue
            if risk_snapshot is not None:
                from execution import broker, day_trade_manager as dtm
                direction = t.get("direction"); entry_ref_raw = t.get("entry_ref")
                if direction not in ("long", "short") or entry_ref_raw is None:
                    row["stop"] = "WIRE_RISK_UNKNOWN"; row["reason"] = "direction/entry reference unavailable"
                    rows.append(row); continue
                entry_ref = float(entry_ref_raw)
                stop_px = dtm._compute_stop_price(t, direction, entry_ref)
                slip = float(getattr(config, "DAYTRADE_ENTRY_SLIPPAGE_PCT", 0.002))
                limit_px = round(entry_ref * (1.0 + slip) if direction == "long" else entry_ref * (1.0 - slip), 2)
                rate = broker.get_asset_maintenance_margin_rate(sym)
                if stop_px is None or rate is None or risk_snapshot.get("orders") is None:
                    row["stop"] = "WIRE_RISK_UNKNOWN"; row["reason"] = "stop/order-book/maintenance unavailable"
                    rows.append(row); continue
                if not replay:
                    # Same min-stop-room gate place_entry runs (live ATR + live quote) — parity with production.
                    # Skipped in an --asof replay: its quote/ATR would be TODAY's, not the replayed session's.
                    room_ok, room_why = dtm._min_stop_room_ok(sym, direction, limit_px, stop_px)
                    if not room_ok:
                        row["stop"] = "MIN_STOP_ROOM"; row["reason"] = _trunc(room_why)
                        rows.append(row); continue
                safe_qty, wire_reason = dtm._bounded_entry_qty(
                    int(z.get("shares") or 0), limit_px, stop_px, equity,
                    risk_snapshot.get("open_trades", {}), risk_snapshot.get("positions", {}),
                    buying_power, float(risk_snapshot.get("maintenance_margin", 0.0)),
                    rate, risk_snapshot.get("orders", []),
                    risk_equity=float(risk_snapshot.get("last_equity", 0.0)), symbol=sym, track="B",
                    track_budget=z.get("budget"))
                row["shares"] = safe_qty
                if safe_qty < 1:
                    row["stop"] = "WIRE_CAP"; row["reason"] = _trunc(wire_reason)
                    rows.append(row); continue
            row["stop"] = "WOULD_ENTER"; row["reason"] = _trunc(mom.get("reason"))
            rows.append(row)
        except Exception as e:  # a diagnostic must never crash mid-universe
            row["stop"] = "ERROR"; row["reason"] = _trunc(repr(e))
            rows.append(row)
    return rows


def _parse_asof(argv: list):
    """--asof YYYY-MM-DDTHH:MM (ET) -> an ET-aware datetime, else None. A malformed value raises SystemExit
    (an operator typo must not silently run the live-clock sim instead of the requested replay)."""
    if "--asof" not in argv:
        return None
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    i = argv.index("--asof")
    try:
        return _dt.strptime(argv[i + 1], "%Y-%m-%dT%H:%M").replace(tzinfo=_ZI("America/New_York"))
    except (IndexError, ValueError) as e:
        raise SystemExit("--asof needs YYYY-MM-DDTHH:MM (ET), e.g. --asof 2026-09-22T10:15") from e


def main() -> int:
    as_json = "--json" in sys.argv[1:]
    asof_et = _parse_asof(sys.argv[1:])
    universe = list(getattr(config, "DAYTRADE_UNIVERSE", []))
    if not universe:  # Track A has nothing to simulate — Track B still runs below (exec seat F4d)
        print("DAYTRADE_UNIVERSE is empty — no Track-A symbols to simulate.")
    risk_snapshot = _fetch_risk_snapshot()
    equity = float(risk_snapshot.get("equity", 0.0))
    buying_power = float(risk_snapshot.get("buying_power", 0.0))
    rows = evaluate_universe(universe, equity, buying_power, risk_snapshot=risk_snapshot)

    would_enter = [r for r in rows if r["stop"] == "WOULD_ENTER"]
    stops = Counter(r["stop"] for r in rows)

    # Track B (movers) — only when the tier is armed. Read-only, same live functions as the runner.
    track_b_on = bool(getattr(config, "DAYTRADE_TRACK_B_ENABLED", False))
    tb_rows = (evaluate_track_b(equity, buying_power, risk_snapshot=risk_snapshot, now_et=asof_et)
               if track_b_on else [])
    tb_would = [r for r in tb_rows if r["stop"] == "WOULD_ENTER"]
    tb_stops = Counter(r["stop"] for r in tb_rows)

    if as_json:
        print(json.dumps({"equity": equity, "buying_power": buying_power, "universe": len(universe),
                          "would_enter": [r["symbol"] for r in would_enter],
                          "stops": dict(stops), "rows": rows,
                          "track_b_enabled": track_b_on,
                          "track_b_asof": asof_et.isoformat() if asof_et else None,
                          "track_b_would_enter": [r["symbol"] for r in tb_would],
                          "track_b_stops": dict(tb_stops), "track_b_rows": tb_rows}, default=str))
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

    if track_b_on:
        print(f"\nTRACK B (movers) PREFLIGHT — {len(tb_rows)} pre-registered symbols"
              + (f"  [REPLAY as of {asof_et:%Y-%m-%d %H:%M} ET]" if asof_et else ""))
        print(f"{'SYM':<6}{'mover?':<8}{'gap':<9}{'rvol':<8}{'trigger':<9}{'size_ok':<9}{'stop':<18}reason")
        for r in tb_rows:
            _gap = f"{r['gap']:+.1%}" if isinstance(r.get("gap"), (int, float)) else str(r.get("gap"))
            _rv = f"{r['rvol']:.1f}x" if isinstance(r.get("rvol"), (int, float)) else str(r.get("rvol"))
            print(f"{r['symbol']:<6}{str(r['is_mover']):<8}{_gap:<9}{_rv:<8}"
                  f"{str(r['trigger']):<9}{str(r['size_ok']):<9}{str(r['stop']):<18}{r['reason']}")
        print(f"SUMMARY (Track B): {len(tb_would)}/{len(tb_rows)} WOULD ENTER"
              + (f"  ({', '.join(r['symbol'] for r in tb_would)})" if tb_would else "")
              + f"  ·  stops: {dict(tb_stops)}")
        print("NOTE: Track B needs a real intraday mover on the pre-registered list (gap≥2%, RVOL≥3x) that "
              "then breaks-and-holds its opening range on volume; on a quiet day 'not_mover'/'no_trigger' "
              "blocking every symbol is the CORRECT no-go, not a wiring failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
