# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/foreign_stop_guard.py — defer an entry while ANOTHER tier's opposite-side stop rests on the symbol.

Design record: logs/design_records/cohold_buys_vs_foreign_stops_2026-10-11.md (board Harris+Brandt, GAI; revised
decision "Option A — defer").

WHY: Alpaca's wash-trade check (docs user-protection) rejects a new buy while ANY stop sell rests on the symbol, and a
new sell while any stop buy rests ("always rejected"), whichever tier owns that stop. Bracket/OCO orders are exempt,
but the Swing tier moves its broker stop only by replace, and Alpaca does not support replacing an OTO leg, while a
bracket's take-profit leg would block the software tranche sells. So a Swing or breakout entry facing another tier's
opposite-side stop DEFERS that cycle (logged) and fires on a later scan if its signal still holds. A Day lot always
exits the same session. (F6 and QHM are out of scope here: F6 is in ChatGPT's queue; QHM follows ChatGPT's in-flight
QHM change.)

  foreign_opposite_stop(symbol, open_side, own_tier) -> (order | None)   the blocking order, or None
  note_deferral(symbol, own_tier, blocking_order)                       log + page once a day after a session
  clear_deferral(symbol, own_tier)                                      forget a cleared block

An unreadable order book returns None (today's behaviour: the broker's own check still applies). Never raises.
State: data/state/foreign_stop_deferrals.json (atomic tmp -> replace). Data tier: T1 (Alpaca trading REST).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "foreign_stop_deferrals.json"
TAG = "[foreign-stop-defer]"      # the change's unique log tag (post-live expectation proof)


def _text(v: object) -> str:
    return str(getattr(v, "value", v) or "").lower()


def foreign_opposite_stop(symbol: str, open_side: str, own_tier: str, orders: "list | None" = None):
    """The first open STOP order on `symbol` on the side opposite `open_side` ("buy" / "sell") whose owner is not
    `own_tier`: another tier's, or an untagged one such as a Day bracket/OCO leg. None when there is none or the book
    is unreadable. `orders` lets a caller pass a book it already read. Never raises."""
    try:
        from execution import broker
        from execution.ownership_guard import tier_of_coid
        book = orders if orders is not None else broker.get_open_orders(symbol)
        if book is None:
            return None
        opposite = "sell" if str(open_side).lower() == "buy" else "buy"
        for o in book:
            if str(getattr(o, "symbol", symbol) or symbol) != symbol:
                continue
            if "stop" not in _text(getattr(o, "type", None) or getattr(o, "order_type", None)):
                continue
            if _text(getattr(o, "side", None)) != opposite:
                continue
            if tier_of_coid(getattr(o, "client_order_id", None)) == own_tier:
                continue
            return o
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] foreign-stop check failed (not deferring): %s", symbol, e)
        return None


def _load() -> dict:
    try:
        d = json.loads(_STATE.read_text(encoding="utf-8")) if _STATE.exists() else {}
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
        os.replace(tmp, _STATE)
    except Exception as e:  # noqa: BLE001
        logger.warning("foreign-stop deferral state not saved: %s", e)


def note_deferral(symbol: str, own_tier: str, blocking) -> None:
    """Log the deferral with the blocking order's owner. A block on the same symbol/tier first seen on an EARLIER ET
    session pages once per day (board: a multi-session block comes back for review instead of silently blocking).
    Never raises."""
    try:
        from execution.ownership_guard import tier_of_coid
        blocker = tier_of_coid(getattr(blocking, "client_order_id", None)) or "untagged"
        today = datetime.now(ET).date().isoformat()
        logger.info("%s [%s] %s entry deferred: a %s-owned %s stop rests on the symbol (order %s) — Alpaca's "
                    "wash rule would reject it; retrying next scan", TAG, symbol, own_tier, blocker,
                    _text(getattr(blocking, "side", "")), getattr(blocking, "id", "?"))
        d = _load()
        key = f"{symbol}|{own_tier}"
        rec = d.get(key) if isinstance(d.get(key), dict) else None
        # A block is CONTINUOUS only if it was also seen on the previous weekday session (or today) by the same blocking
        # tier; after a gap or a change of blocker the clock restarts (board: the page must mean a real multi-session
        # block). Weekends are skipped; a holiday only makes a restart one session early (the quiet direction).
        prev = datetime.now(ET).date() - timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= timedelta(days=1)
        if (rec is None or rec.get("blocker") != blocker
                or str(rec.get("last_seen", "")) < prev.isoformat()):
            rec = {"first_seen": today, "blocker": blocker, "paged": ""}
        rec["last_seen"], rec["blocker"] = today, blocker
        if rec.get("first_seen", today) < today and rec.get("paged") != today:
            rec["paged"] = today
            try:
                from alerts import send_slack
                send_slack(f":warning: {TAG} [{symbol}] {own_tier} entries have been deferred since "
                           f"{rec['first_seen']} by a {blocker} stop resting on the symbol. Review whether that stop "
                           f"should stay, or route the entry another way.")
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] foreign-stop page failed: %s", symbol, e)
        d[key] = rec
        _save(d)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] foreign-stop deferral note failed: %s", symbol, e)


def clear_deferral(symbol: str, own_tier: str) -> None:
    """Forget a block once the entry is no longer deferred. Never raises."""
    try:
        d = _load()
        if d.pop(f"{symbol}|{own_tier}", None) is not None:
            _save(d)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] foreign-stop deferral clear failed: %s", symbol, e)
