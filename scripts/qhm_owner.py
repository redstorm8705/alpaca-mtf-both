# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""scripts/qhm_owner.py — quick owner commands for quarterly holds (QHM-OWNERBUY-1113, 2026-11-13).

Design: logs/design_records/qhm_owner_buy_2026-11-13.md (rev 2). `buy` writes a request file that the bot
(execution/quarterly_hold_manager._maybe_owner_buy) executes in its own cycle, in market hours, through its stop-safe
add: the QHM caps apply, the stop is resized on every fill and its price recalculated. A buy "@price" is held by the bot
until the live ask is at or below that price; no order ever rests at the broker, so `cancel` or the end date only
removes the request. This script never places, moves or cancels an order; `buy` and `show` make read-only clock /
open-order reads through execution.broker.

  python3 scripts/qhm_owner.py buy NVDA 2                       buy 2 at market (next market-hours cycle)
  python3 scripts/qhm_owner.py buy NVDA 2 @160 [--until DATE]   buy 2 when the ask is $160 or lower
  python3 scripts/qhm_owner.py trim NVDA 1 --deadline DATE [--trigger P]   alias: qhm_owner_trim.py arm
  python3 scripts/qhm_owner.py stop NVDA 147.48 --until DATE [--confirm-risk]   alias: qhm_owner_stop.py set
  python3 scripts/qhm_owner.py cancel NVDA buy|trim|stop
  python3 scripts/qhm_owner.py show
"""
import argparse
import fcntl
import json
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import qhm_owner_stop as stop_cli  # noqa: E402
from scripts import qhm_owner_trim as trim_cli  # noqa: E402

REQ = ROOT / "data" / "state" / "qhm_owner_buys.json"
HOLDS = ROOT / "data" / "state" / "quarterly_holds.json"
EVENTS = ROOT / "logs" / "trade_events.jsonl"
ET = ZoneInfo("America/New_York")      # the bot's expiry clock (QHM's _now_et)
PT = ZoneInfo("America/Los_Angeles")   # display / event timestamps


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    d = json.loads(path.read_text())
    if not isinstance(d, dict):
        raise ValueError(f"{path} is not a JSON object")
    return d


def _write(d: dict) -> None:
    REQ.parent.mkdir(parents=True, exist_ok=True)
    tmp = REQ.with_suffix(f".json.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, REQ)


def _event(sym: str, action: str, **fields) -> None:
    """One trade_events.jsonl row (event qhm_owner_buy). Best-effort: a failed row is printed, never fatal."""
    row = {"ts": datetime.now(PT).isoformat(), "event": "qhm_owner_buy", "symbol": sym, "tier": "qhm",
           "action": action, "price": None, "size": 0, "score": 0, "mri_level": "N/A", "data_source": "owner_request",
           "source": "scripts/qhm_owner.py", "change": "qhm-ownerbuy-1113", **fields}
    try:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        print(f"warning: trade_events row not written ({e})")


def _refuse(sym: str, why: str, **fields) -> int:
    _event(sym, "refused", reason=why, **fields)
    print(f"REFUSED {sym}: {why} — nothing requested")
    return 1


def _market_open() -> "bool | None":
    """Read-only market clock; None when unreadable."""
    try:
        from execution.broker import is_market_open
        return bool(is_market_open())
    except Exception as e:  # any broker / transport error -> unknown
        print(f"market clock unreadable ({e})")
        return None


def _qh_orders() -> "list | None":
    """Open QH-tagged orders at the broker as text rows (read-only); None when unreadable."""
    try:
        from execution.broker import get_open_orders
        orders = get_open_orders()
    except Exception as e:  # any broker / transport error -> unknown
        print(f"open orders unreadable ({e})")
        return None
    if orders is None:
        return None
    out = []
    for o in orders:
        if not str(getattr(o, "client_order_id", "") or "").startswith("QH-"):
            continue
        side = str(getattr(getattr(o, "side", ""), "value", getattr(o, "side", ""))).lower()
        otype = str(getattr(getattr(o, "type", ""), "value", getattr(o, "type", ""))).lower()
        px = getattr(o, "stop_price", None) or getattr(o, "limit_price", None)
        out.append((str(getattr(o, "symbol", "")).upper(), side, otype, getattr(o, "qty", None), px,
                    str(getattr(o, "id", ""))))
    return out


def _run_trim(argv: list) -> int:
    """The existing owner-trim script, unchanged (it reads sys.argv)."""
    old = sys.argv
    sys.argv = ["qhm_owner_trim.py", *argv]
    try:
        return trim_cli.main()
    finally:
        sys.argv = old


def _show() -> int:
    rc = 0
    try:
        holds = _load(HOLDS)
    except ValueError as e:
        print(f"quarterly holds unreadable ({e})")
        holds, rc = {}, 1
    today = datetime.now(ET).date()
    print("OWNER BUY REQUESTS")
    try:
        buys = _load(REQ)
    except ValueError as e:
        print(f"  {REQ} is unreadable ({e}); the bot runs no owner buy until it is fixed, and `buy` / `cancel ... "
              "buy` refuse and change nothing. Inspect it by hand (it may hold a buy that was in progress; check "
              "Alpaca for it), then repair or remove it.")
        buys, rc = {}, 1
    if not buys:
        print("  none")
    for sym in sorted(buys):
        rec = buys[sym] if isinstance(buys[sym], dict) else {}
        try:
            state = "active" if today <= date.fromisoformat(str(rec.get("until"))) else "past its end date"
        except ValueError:
            state = "unreadable date"
        how = "at market" if rec.get("limit") is None else f"when the ask is ${rec.get('limit')} or lower"
        print(f"  {sym}: buy {rec.get('qty')} {how}, until {rec.get('until')} ({rec.get('status')}, {state}); asked "
              f"{rec.get('requested_qty')}, bought so far {rec.get('filled_qty') or 0}; requested {rec.get('created_at')}")
    for title, mod in (("OWNER TRIMS", trim_cli), ("OWNER STOPS", stop_cli)):
        print(title)
        try:
            reqs = _load(mod.REQ)
        except ValueError as e:
            print(f"  unreadable ({e})")
            reqs, rc = {}, 1
        if not reqs:
            print("  none")
        for sym in sorted(reqs):
            print(f"  {sym}: {json.dumps(reqs[sym], default=str)}")
    orders = _qh_orders()
    print("OPEN QH ORDERS AT ALPACA")
    if orders is None:
        print("  unreadable")
    elif not orders:
        print("  none")
    else:
        for sym, side, otype, qty, px, oid in sorted(orders):
            print(f"  {sym}: {side} {otype} {qty} @ {px} (order {oid})")
    print("STOP PER NAME")
    held = {s: h for s, h in holds.items()
            if isinstance(h, dict) and h.get("state") in stop_cli.HELD_STATES and int(h.get("qty_filled") or 0) >= 1}
    if not held:
        print("  no quarterly holds")
    for sym in sorted(held):
        h = held[sym]
        broker = ("unreadable" if orders is None else
                  [f"{q} @ {p}" for s, side, t, q, p, _i in orders if s == sym and side == "sell" and "stop" in t]
                  or "none")
        print(f"  {sym}: {h.get('qty_filled')} sh, average ${h.get('avg_entry_price')}; QHM recorded stop "
              f"${h.get('stop_price')}; broker QH stop {broker}")
    return rc


def _buy(ap, a, reqs: dict) -> int:
    try:
        sym_raw, qty_raw, *rest = a.args
    except ValueError:
        ap.error("buy needs SYMBOL QTY [@PRICE]")
    if len(rest) > 1:
        ap.error("buy needs SYMBOL QTY [@PRICE]")
    sym = sym_raw.upper()
    try:
        qty = int(qty_raw)
    except ValueError:
        return _refuse(sym, f"quantity {qty_raw!r} is not a whole number of shares")
    if qty < 1:
        return _refuse(sym, "quantity must be at least 1")
    limit = None
    if rest:
        raw = rest[0]
        try:
            limit = round(float(raw[1:]), 2) if raw.startswith("@") else math.nan
        except ValueError:
            limit = math.nan
        if not math.isfinite(limit) or limit <= 0:
            return _refuse(sym, f"limit price {raw!r} must be written @PRICE with a positive price (e.g. @160)")
    hold = _load(HOLDS).get(sym)
    if (not isinstance(hold, dict) or hold.get("state") not in stop_cli.HELD_STATES
            or int(hold.get("qty_filled") or 0) < 1):
        return _refuse(sym, f"not a quarterly hold QHM holds ({(hold or {}).get('state') if isinstance(hold, dict) else None}); "
                            "a new QHM name goes through the QHM pick process, not a buy command")
    today = datetime.now(ET).date()
    horizon, why = stop_cli._horizon(hold, today)
    until = horizon
    if a.until:
        try:
            until = date.fromisoformat(a.until)
        except ValueError:
            return _refuse(sym, f"--until {a.until!r} is not a YYYY-MM-DD date")
    if until < today:
        return _refuse(sym, f"the end date {until} is in the past")
    if until > horizon:
        return _refuse(sym, f"--until {until} is past {why}: {horizon}")
    prior = reqs.get(sym)
    if isinstance(prior, dict) and prior.get("status") == "reserved":
        return _refuse(sym, "an earlier buy request is 'reserved' (the bot is buying it now, or its result is not "
                            f"confirmed). Check {sym} in Alpaca, then: cancel {sym} buy")
    if isinstance(prior, dict) and prior.get("status") == "cancelled_unresolved":
        return _refuse(sym, "an earlier buy you cancelled has an unconfirmed result and may still fill at the broker; "
                            "the bot pages its result once resolved, then a new buy is accepted")
    now = datetime.now(PT).isoformat()
    reqs[sym] = {"qty": qty, "requested_qty": qty, "filled_qty": 0, "limit": limit, "until": until.isoformat(),
                 "status": "armed", "entry_day": hold.get("entry_day"), "created_at": now, "updated_at": now}
    _write(reqs)
    _event(sym, "requested", request_qty=qty, limit=limit, until=until.isoformat(), replaced=prior,
           qty_held=hold.get("qty_filled"), stop_price=hold.get("stop_price"), stop_order_id=hold.get("stop_order_id"))
    how = "at market" if limit is None else f"when the ask is ${limit:.2f} or lower (never priced above it)"
    is_open = _market_open()
    when = ("on the bot's next cycle" if is_open else
            "the market is closed: it waits for the open" if is_open is False else
            "on the bot's next market-hours cycle (if the market is closed now, it waits for the open)")
    print(f"{sym} owner buy REQUESTED: {qty} {how}, good through {until} (ET); {when}."
          + (" It replaces your earlier request." if prior else ""))
    print("The bot's 20% per-stock and 40% QHM limits apply at the time of the buy; the stop is resized on every fill. "
          "No order rests at the broker.")
    return 0


def _cancel_buy(sym: str, reqs: dict) -> int:
    if sym not in reqs:
        if not REQ.exists():
            _write(reqs)
        print(f"{sym}: no owner buy request to cancel")
        return 0
    old = reqs[sym]
    status = old.get("status") if isinstance(old, dict) else None
    if status == "cancelled_unresolved":
        print(f"{sym}: this request is already cancelled; the bot is still resolving the earlier buy's result (it "
              "pages the result and then accepts a new buy).")
        return 0
    if status == "reserved" and isinstance(old, dict) and old.get("attempt_result") != "not_sent":
        # its buy may still fill: keep a tombstone (same token) so a new buy is refused until the bot resolves it
        reqs[sym] = {**old, "status": "cancelled_unresolved", "cancelled_at": datetime.now(PT).isoformat()}
        _write(reqs)
        _event(sym, "cancelled_unresolved", cancelled=old)
        print(f"{sym} owner buy request cancelled. The buy the bot started for it has an unconfirmed result and may "
              f"still fill at the broker; check {sym} in Alpaca (the bot's stop covers the shares it can prove). A new "
              f"`buy {sym}` is refused until the bot resolves that buy and pages the result.")
        return 0
    reqs.pop(sym)
    _write(reqs)                               # never deletes the file
    _event(sym, "cancelled", cancelled=old)
    if status == "reserved":
        print(f"{sym} owner buy request cancelled. Its buy never reached the broker, so nothing was bought for it.")
    else:
        print(f"{sym} owner buy request cancelled; no order was at the broker (the bot places none until it buys).")
    return 0


def main(argv: "list | None" = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "stop":              # alias: every argument goes to the owner-stop script unchanged
        return stop_cli.main(["set", *raw[1:]])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("verb", choices=("buy", "trim", "stop", "cancel", "show"))
    ap.add_argument("args", nargs="*")
    ap.add_argument("--until", help="buy: last day (ET, YYYY-MM-DD) of the request; stop: the stop's end date")
    ap.add_argument("--trigger", help="trim: sell when the last trade is at or above this price")
    ap.add_argument("--deadline", help="trim: YYYY-MM-DD, sell at market on/after this date if not triggered")
    a = ap.parse_args(raw)
    if a.verb == "show":
        return _show()
    first, second = "", ""
    if a.verb != "buy":
        try:
            first, second = a.args
        except ValueError:
            ap.error({"trim": "trim needs SYMBOL QTY --deadline YYYY-MM-DD [--trigger PRICE]",
                      "cancel": "cancel needs SYMBOL buy|trim|stop"}[a.verb])
    if a.verb == "trim":                      # alias: the existing owner-trim script, unchanged
        # passed through unchanged: the trim script's own rules apply (it requires --deadline)
        return _run_trim(["arm", first, "--qty", second] + (["--trigger", a.trigger] if a.trigger else [])
                         + (["--deadline", a.deadline] if a.deadline else []))
    if a.verb == "cancel":
        if second not in ("buy", "trim", "stop"):
            ap.error("cancel needs SYMBOL buy|trim|stop")
        if second == "trim":
            return _run_trim(["cancel", first])
        if second == "stop":
            return stop_cli.main(["clear", first])
    REQ.parent.mkdir(parents=True, exist_ok=True)
    with open(str(REQ) + ".lock", "w") as lk:      # the same lock the bot takes when it rewrites a request
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            reqs = _load(REQ)
        except ValueError as e:                    # unreadable file: never reset it (it may hold a reserved buy)
            print(f"REFUSED: {REQ} is unreadable ({e}). Nothing was changed. Inspect it by hand (it may hold a buy "
                  "that was in progress; check Alpaca for it), repair or remove it, then run the command again. The "
                  "bot runs no owner buy while it is unreadable.")
            return 1
        if a.verb == "cancel":
            return _cancel_buy(first.upper(), reqs)
        return _buy(ap, a, reqs)


if __name__ == "__main__":
    sys.exit(main())
