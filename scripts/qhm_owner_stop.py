# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""scripts/qhm_owner_stop.py — set, clear or show an OWNER STOP on a quarterly hold (QHM-OWNERSTOP-1113, 2026-11-13).

The bot (execution/quarterly_hold_manager._maybe_apply_owner_stop) reads the request file each cycle and keeps the hold's
stop at the owner's price through the --until date (ET), as long as the price stays below the market less one usual
overnight gap. After --until, or after `clear`, the bot recalculates its own stop (never through the market).
Design: logs/design_records/qhm_owner_stop_2026-11-13.md (rev 3).

This script writes only the request file (and a trade_events row). It never places, moves or cancels an order; `set`
and `show` make read-only account / open-order reads through execution.broker.

  python3 scripts/qhm_owner_stop.py set NVDA 147.48 --until 2026-11-20 [--confirm-risk]
  python3 scripts/qhm_owner_stop.py clear NVDA
  python3 scripts/qhm_owner_stop.py show
"""
import argparse
import calendar
import fcntl
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REQ = ROOT / "data" / "state" / "qhm_owner_stops.json"
HOLDS = ROOT / "data" / "state" / "quarterly_holds.json"
EVENTS = ROOT / "logs" / "trade_events.jsonl"
ET = ZoneInfo("America/New_York")      # the bot's expiry clock (QHM's _now_et)
PT = ZoneInfo("America/Los_Angeles")   # display / event timestamps
HELD_STATES = ("ACTIVE", "PENDING_STOP_REPLACE", "PENDING_EARNINGS")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    d = json.loads(path.read_text())
    if not isinstance(d, dict):
        raise SystemExit(f"{path} is not a JSON object — fix it by hand first")
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
    """One trade_events.jsonl row (event qhm_owner_stop). Best-effort: a failed row is printed, never fatal."""
    row = {"ts": datetime.now(PT).isoformat(), "event": "qhm_owner_stop", "symbol": sym, "tier": "qhm",
           "action": action, "source": "scripts/qhm_owner_stop.py", "change": "qhm-ownerstop-1113", **fields}
    try:
        EVENTS.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENTS, "a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        print(f"warning: trade_events row not written ({e})")


def _quarter_end(d: date) -> date:
    last_month = ((d.month - 1) // 3 + 1) * 3
    return date(d.year, last_month, calendar.monthrange(d.year, last_month)[1])


def _horizon(hold: dict, today: date) -> "tuple[date, str]":
    """The latest allowed --until: the hold's own QHM max-hold exit date (entry_day + QHM's max-hold days, the date the
    per-cycle check starts the max-hold exit); without an entry_day, the current quarter's end."""
    from execution.quarterly_hold_manager import _MAX_HOLD_CALENDAR_DAYS
    try:
        entry = date.fromisoformat(str(hold.get("entry_day")))
        return entry + timedelta(days=_MAX_HOLD_CALENDAR_DAYS), f"the hold's max-hold exit date (entered {entry})"
    except (TypeError, ValueError):
        return _quarter_end(today), "the current quarter's end (the hold record has no entry_day)"


def _equity() -> "float | None":
    """Read-only account equity; None when unreadable."""
    try:
        from execution.broker import get_account
        eq = float(getattr(get_account(), "equity", None))  # type: ignore[arg-type]
        return eq if math.isfinite(eq) and eq > 0 else None
    except Exception as e:  # any broker / transport error -> unknown
        print(f"account equity unreadable ({e})")
        return None


def _broker_stops() -> "dict | None":
    """{symbol: [(stop_price, qty, order id)]} of open QH sell stops (read-only); None when unreadable."""
    try:
        from execution.broker import get_open_orders
        orders = get_open_orders()
    except Exception as e:  # any broker / transport error -> unknown
        print(f"open orders unreadable ({e})")
        return None
    if orders is None:
        return None
    out: dict = {}
    for o in orders:
        coid = str(getattr(o, "client_order_id", "") or "")
        side = str(getattr(getattr(o, "side", ""), "value", getattr(o, "side", ""))).lower()
        otype = str(getattr(getattr(o, "type", ""), "value", getattr(o, "type", ""))).lower()
        if coid.startswith("QH-") and side == "sell" and "stop" in otype:
            out.setdefault(str(getattr(o, "symbol", "")).upper(), []).append(
                (getattr(o, "stop_price", None), getattr(o, "qty", None), str(getattr(o, "id", ""))))
    return out


def main(argv: "list | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("set", "clear", "show"))
    ap.add_argument("symbol", nargs="?")
    ap.add_argument("price", nargs="?", type=float, help="set: the stop price")
    ap.add_argument("--until", help="set: YYYY-MM-DD (ET); the bot keeps the stop through this date")
    ap.add_argument("--confirm-risk", action="store_true",
                    help="set: required when the loss at the stop is larger than the daily kill-switch share of equity")
    ap.add_argument("--profile", default="paper",
                    help="config profile whose MAX_DAILY_LOSS_PCT the loss is compared with (the bot runs paper)")
    a = ap.parse_args(argv)
    REQ.parent.mkdir(parents=True, exist_ok=True)
    with open(str(REQ) + ".lock", "w") as lk:      # serialises this CLI; the bot reads without the lock (atomic replace)
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _run(ap, a)


def _show(reqs: dict) -> int:
    holds = _load(HOLDS)
    today = datetime.now(ET).date()
    if not reqs:
        print("no owner stops")
        return 0
    stops = _broker_stops()
    for sym in sorted(reqs):
        rec = reqs[sym] if isinstance(reqs[sym], dict) else {}
        _h = holds.get(sym)
        hold: dict = _h if isinstance(_h, dict) else {}
        try:
            state = "active" if today <= date.fromisoformat(str(rec.get("until"))) else "expired"
        except ValueError:
            state = "unreadable date"
        broker = "unreadable" if stops is None else (stops.get(sym) or "none")
        print(f"{sym}: owner stop ${rec.get('price')} until {rec.get('until')} ({state}); QHM recorded stop "
              f"${hold.get('stop_price')} (applied until {hold.get('owner_stop_until')}); broker QH stop {broker}")
    return 0


def _run(ap, a) -> int:
    try:
        reqs = _load(REQ)
    except ValueError as e:                        # unreadable file (bad JSON / bad bytes): the bot freezes on it
        if a.action == "show":
            print(f"{REQ} is unreadable ({e}). Repair it with 'clear <SYMBOL>' or 'set ...': the unreadable file is "
                  "kept as a .corrupt copy and every owner stop that was in it must be set again.")
            return 1
        bad = Path(f"{REQ}.corrupt.{datetime.now(PT).strftime('%Y%m%dT%H%M%S')}")
        os.replace(REQ, bad)
        reqs = {}
        _write(reqs)
        print(f"warning: {REQ} was unreadable; it was moved to {bad.name} and a new empty file was written. Every "
              "owner stop that was in it must be set again.")
    if a.action == "show":
        return _show(reqs)
    if not a.symbol:
        ap.error("symbol required")
    sym = a.symbol.upper()
    now = datetime.now(PT).isoformat()
    if a.action == "clear":
        if sym not in reqs:
            if not REQ.exists():                   # a missing file is a fault to the bot: clear re-creates it
                _write(reqs)
                print(f"{REQ.name} was missing; a new empty file was written")
            print(f"{sym}: no owner stop to clear")
            return 0
        old = reqs.pop(sym)
        _write(reqs)                               # never deletes the file: a missing file is a fault to the bot
        _event(sym, "clear", cleared=old)
        print(f"{sym} owner stop cleared; the bot recalculates its own stop on its next cycle (never through the market)")
        return 0
    if a.price is None or a.until is None:
        ap.error("set needs SYMBOL PRICE --until YYYY-MM-DD")
    price = round(float(a.price), 2)
    if not math.isfinite(price) or price <= 0:
        ap.error("the stop price must be positive")
    try:
        until = date.fromisoformat(a.until)
    except ValueError:
        ap.error(f"--until {a.until!r} is not a YYYY-MM-DD date")
    today = datetime.now(ET).date()
    if until < today:
        ap.error(f"--until {until} is in the past")
    hold = _load(HOLDS).get(sym)
    if not isinstance(hold, dict) or hold.get("state") not in HELD_STATES or int(hold.get("qty_filled") or 0) < 1:
        raise SystemExit(f"{sym} is not a quarterly hold QHM holds ({(hold or {}).get('state')}) — nothing set")
    limit, why = _horizon(hold, today)
    if until > limit:
        raise SystemExit(f"--until {until} is past {why}: {limit} — nothing set")
    qty = int(hold.get("qty_filled") or 0)
    avg = float(hold.get("avg_entry_price") or 0.0)
    loss = round(qty * (avg - price), 2)
    import config
    kill = float(config.PROFILES[a.profile]["MAX_DAILY_LOSS_PCT"])
    eq = _equity()
    share = f"{loss / eq:.1%} of equity ${eq:,.2f}" if eq else "unknown share of equity"
    print(f"{sym}: {qty} shares, average ${avg:.2f}; a stop at ${price:.2f} risks ${loss:,.2f} ({share}). "
          "QHM losses are outside the daily kill switch.")
    if (eq is None or loss > kill * eq) and not a.confirm_risk:
        raise SystemExit(f"the loss at this stop is larger than the {kill:.0%} daily kill-switch share of equity "
                         "(or equity is unreadable); re-run with --confirm-risk to set it anyway — nothing set")
    reqs[sym] = {"price": price, "until": until.isoformat(), "avg_at_set": round(avg, 4), "qty_at_set": qty,
                 "loss_at_stop": loss, "equity_at_set": eq, "confirm_risk": bool(a.confirm_risk),
                 "entry_day": hold.get("entry_day"), "set_at": now}
    _write(reqs)
    _event(sym, "set", owner_price=price, until=until.isoformat(), stop_before=hold.get("stop_price"),
           stop_order_id=hold.get("stop_order_id"), size=qty, avg=avg, loss_at_stop=loss, equity=eq,
           confirm_risk=bool(a.confirm_risk))
    print(f"{sym} owner stop SET ${price:.2f} until {until} (ET); the bot applies it on its next cycle if it is below "
          "the price less one usual overnight gap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
