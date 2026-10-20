#!/usr/bin/env python3
# ruff: noqa: E501
"""REPLAY-1019 golden master for the Day-tier entry (owner approval 2026-10-19,
logs/design_records/sizing_replay_gate_2026-10-19.md, "Revised design" item 1).

Runs execution.day_trade_manager.place_entry end to end over a hand-built grid. Every broker, data, journal and state
read is replaced with fixed inputs. For each case it records what the entry would do: return value, submitted
qty/side/limit, the saved stop and record fields, the skip reason, and the logged wire reason.

The pinned results live in tests/golden/day_entry_grid.json. They may change only when the owner-approved design
record lists the change ("Expected replay differences"). Regenerate with:

    python -m tests.golden_day_entry --write

No case reaches the broker: submit_limit_order and submit_bracket_entry are mocked."""
import contextlib
import itertools
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
FIXTURE = Path(__file__).resolve().parent / "golden" / "day_entry_grid.json"

TRACKS = ("A", "B", "M")
MAX_SIZE = (False, True)
SYMBOLS = ("AAPL", "XYZQ")            # deep (DAYTRADE_DEEP_LIQUIDITY_SYMBOLS) / thin
DIRECTIONS = ("long", "short")
RESERVE = (False, True)
CONV = ("off", "unproven", "proven")  # journal unreadable / losing band / winning band
BUYING_POWER = (20000.0, 1500.0, 1250.0)   # ample / binding / under one share (skip)
# Second grid (masked-loss seat 2026-10-19): the daily dollar budget (trim / risk_budget skip / unreadable-journal
# abort), a quote-priced limit and the real-time stop room, crossed with track, conviction band and direction.
USED = ((0.0, 0.0), (110.0, 5.0), (124.5, 0.0), None)
QUOTES = (None, (99.98, 100.02), (99.6, 100.4))
RT_ROOM = (None, 3.0)


def _trade(tid, conv, entry, stop, exit_px, qty=10, day=datetime(2026, 10, 7, 10, 0, tzinfo=ET)):
    pnl = round((exit_px - entry) * qty, 2)
    return [
        {"event": "decision", "trade_id": tid, "symbol": "AAPL", "size": {"conviction": conv}, "decision": {}},
        {"event": "entry_fill", "trade_id": tid, "symbol": "AAPL", "side": "long", "fill_price": entry,
         "fill_qty": qty, "ts": day.astimezone(PT).isoformat()},
        {"event": "stop_placed", "trade_id": tid, "symbol": "AAPL", "stop_price": stop},
        {"event": "exit_fill", "trade_id": tid, "symbol": "AAPL", "fill_qty": qty, "realized_pnl": pnl,
         "ts": (day + timedelta(minutes=30)).astimezone(PT).isoformat()},
    ]


def _journal(conv: str) -> list:
    ev: list = []
    for i, c in enumerate((0.40, 0.41, 0.42, 0.50, 0.51, 0.52, 0.70, 0.71, 0.72)):
        win = conv == "proven" and 0.45 < c < 0.6
        ev += _trade(f"G{i}", c, 100.0, 99.0, 101.0 if win else 99.0)
    return ev


def cases() -> list:
    out = []
    for t, ms, sym, d, rb, cv, bp in itertools.product(TRACKS, MAX_SIZE, SYMBOLS, DIRECTIONS, RESERVE, CONV,
                                                       BUYING_POWER):
        out.append({"track": t, "max_size": ms, "symbol": sym, "direction": d, "reserve": rb, "conv": cv, "bp": bp})
    for t, cv, d, used, q, rt in itertools.product(TRACKS, ("off", "proven"), DIRECTIONS, USED, QUOTES, RT_ROOM):
        out.append({"track": t, "max_size": False, "symbol": "AAPL", "direction": d, "reserve": False, "conv": cv,
                    "bp": 20000.0, "used": used, "quote": q, "rt": rt})
    return out


def case_id(c: dict) -> str:
    base = (f"{c['track']}|max={int(c['max_size'])}|{c['symbol']}|{c['direction']}|res={int(c['reserve'])}|"
            f"{c['conv']}|bp={int(c['bp'])}")
    if "used" in c:
        base += f"|used={c['used']}|quote={c['quote']}|rt={c['rt']}"
    return base


def run_case(c: dict) -> dict:
    from data.live_price import LivePrice
    from execution import broker
    from execution import day_trade_manager as dtm
    from execution import earned_sizing as es

    long = c["direction"] == "long"
    sub: dict = {}
    saved: list = []
    logged: dict = {}
    acct = SimpleNamespace(equity="2500", last_equity="2500", buying_power=str(c["bp"]), maintenance_margin="0",
                           trading_blocked=False, account_blocked=False)
    if c["track"] == "M":
        trigger = {"trigger": "ENTER", "direction": c["direction"], "mode": "TRACK_M", "entry_ref": 100.0,
                   "stop_ref": 99.0 if long else 101.0, "target": None}
    else:
        trigger = {"trigger": "ENTER", "direction": c["direction"], "mode": "RIDE", "entry_ref": 100.0,
                   "target": None, "wall_ref": 97.0 if long else 103.0}
    size = {"size_ok": True, "shares": 5, "conviction": 0.51, "track": c["track"], "budget": 300.0,
            "max_size": c["max_size"], "risk_mult": 1.0}
    journal = _journal(c["conv"])

    def _rec(trade_id=None, scope=None):
        if scope is None and c["conv"] == "off":
            return [], False
        evs = journal if scope is None else []
        return ([e for e in evs if trade_id is None or e.get("trade_id") == trade_id], True)

    def _submit(sym, qty, side, px, **k):
        sub.update(kind="limit", qty=int(qty), side=side, px=round(float(px), 4))
        return None

    def _bracket(sym, qty, side, px, tp, sl, **k):
        sub.update(kind="bracket", qty=int(qty), side=side, px=round(float(px), 4), tp=tp, sl=sl)
        return None

    def _log_decision(did, sym, **k):
        logged["wire"] = (k.get("size") or {}).get("wire_cap_reason")
        logged["shares"] = (k.get("size") or {}).get("shares")
        return True

    patches = [
        mock.patch.object(dtm, "_enabled", return_value=True),
        mock.patch.object(dtm, "_load_state", return_value={}),
        mock.patch.object(dtm, "_save_state", side_effect=lambda st: saved.append(
            {k: dict(v) for k, v in st.items() if isinstance(v, dict) and k.startswith("entry::")}) or True),
        mock.patch.object(dtm, "_account_entry_halt_reason", return_value=None),
        mock.patch.object(dtm, "_daily_risk_used",
                          return_value=(None if c.get("used", (0.0, 0.0)) is None
                                        else (*c.get("used", (0.0, 0.0)), "golden"))),
        mock.patch.object(dtm, "_track_b_reserve_active", return_value=c["reserve"]),
        mock.patch.object(dtm, "_robust_atr_5m", return_value=0.4),
        mock.patch.object(dtm, "_realtime_vol_room", return_value=c.get("rt")),
        mock.patch.object(dtm, "_page"),
        mock.patch.object(es, "jump_ratio", return_value=0.005),
        mock.patch("data.live_price.live_price", return_value=LivePrice(100.0, "iex_trade", 1.0)),
        mock.patch("data.alpaca_data.get_latest_quote",
                   return_value=({"bid": c["quote"][0], "ask": c["quote"][1]} if c.get("quote") else None)),
        mock.patch("data.alpaca_data.get_latest_trade", return_value=100.0),
        mock.patch("data.alpaca_data.get_quote_spreads", return_value=[0.1, 0.1, 0.2]),
        mock.patch("strategy.day_tier_logger.open_trades_from_log", return_value={}),
        mock.patch("strategy.day_tier_logger.read_events_checked", side_effect=_rec),
        mock.patch("strategy.day_tier_logger.log_decision", side_effect=_log_decision),
        mock.patch.object(broker, "get_account", return_value=acct),
        mock.patch.object(broker, "get_open_positions", return_value=[]),
        mock.patch.object(broker, "get_open_orders", return_value=[]),
        mock.patch.object(broker, "get_asset_maintenance_margin_rate", return_value=0.30),
        mock.patch.object(broker, "submit_limit_order", side_effect=_submit),
        mock.patch.object(broker, "submit_bracket_entry", side_effect=_bracket),
        mock.patch("execution.tier_capital_allocator.live_admit",
                   return_value=SimpleNamespace(approved=True, lease=None, reason="")),
        mock.patch("execution.tier_capital_allocator.live_order_id", return_value="DT-golden"),
        mock.patch("execution.tier_capital_allocator.live_release", return_value=None),
    ]
    with contextlib.ExitStack() as es_stack:
        for p in patches:
            es_stack.enter_context(p)
        ok = dtm.place_entry(c["symbol"], {"would_consider": True, "conviction": 0.51}, trigger, size,
                             bar_id="20261020-0945", equity=2500.0)
        skip = dtm._LAST_ENTRY_SKIP.get(c["symbol"])
    rec = {}
    for s in saved:
        for v in s.values():
            rec = {k: v.get(k) for k in ("qty", "stop_px", "track", "risk_per_share", "side") if k in v}
            break
        if rec:
            break
    return {"ok": bool(ok), "submit": sub or None, "record": rec or None, "skip": skip,
            "wire": logged.get("wire"), "logged_shares": logged.get("shares")}


def compute_all() -> dict:
    return {case_id(c): run_case(c) for c in cases()}


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FIXTURE.with_suffix(".tmp")
        tmp.write_text(json.dumps(compute_all(), indent=1, sort_keys=True) + "\n")
        tmp.replace(FIXTURE)
        print(f"wrote {FIXTURE} ({len(cases())} cases)")
    else:
        print(json.dumps(compute_all(), indent=1, sort_keys=True)[:4000])
