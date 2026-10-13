# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/unmanaged_positions.py — page when the account holds shares no LIVE manager manages.

Design record: logs/design_records/unmanaged_position_alarm_2026-10-12.md (board Majors + Peterffy, GAI).

WHY (2026-10-12): 72 UBER from four test-placed bracket orders filled at 04:00 ET and sat in the account all session
with no owner, no stop after the close, and no page; buying power stayed $0, so no tier traded. The only checks were
main.py's P0-STARTUP (Swing tracker only, log-only, startup only) and the fill-based ownership ledger, which counted
the DT-tagged fills as Day-tier owned although the Day tier never recorded them.

WHAT: residual = Alpaca net (signed) minus what each live manager manages:
  Swing tracker open trades (qty_remaining/qty, signed by direction) + Swing breakout lots (swing_breakout.json
  holding states; an entry still settling counts its requested size) + Day->Swing hand-offs booked "ready" but not
  yet adopted + the Day tier's own lifecycle log (today's entries; until 09:45 ET of the next trading session also prior-day lots still
  in their overnight exit) + QHM active holds (get_quarterly_hold_quantities) + Forever-6 (the ledger's forever6 slice).
A residual of a whole share or more pages once it is confirmed on a following pass (net of what the tiers booked in
between), ONCE per (symbol, ET date, kind), re-paging when it grows or after it was gone for two passes. When Alpaca
holds less than the tiers record on a symbol it holds, the page says the tiers' records are ahead (an unbooked sale),
not "unmanaged shorts"; a tier record on a symbol Alpaca does not hold at all is not checked (pending orders would
false-page). A manager source that cannot be read pages ONCE per ET day; its LAST GOOD reading is used in its place (so
an unreadable QHM file never tells the operator to sell quarterly holds), and with no last good reading its symbols
page with a "verify before acting" caveat. Each flagged symbol also writes an `unmanaged_position` event.

PAGE-ONLY: this module never places, cancels or changes an order (no owner tag, no safe quantity and no stop level
exist for shares nobody manages; a synthetic stop would mask the broken ownership chain). Never raises.
State: data/state/unmanaged_alarm.json (atomic tmp -> replace). Data tier: T1 (Alpaca trading REST, read-only).
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "unmanaged_alarm.json"
TAG = "[unmanaged-position]"   # unique log tag (post-live proof)


def _signed(qty: object, direction: object) -> float:
    q = abs(float(qty))      # type: ignore[arg-type]
    return -q if str(direction or "long").lower() in ("short", "sell", "sell_short") else q


_DAY_CARRY_UNTIL_MIN = 9 * 60 + 45   # a prior-session Day lot (an unfilled after-hours exit worked overnight, flattened
                                     # at the next regular open) still counts as Day-managed until 09:45 ET of the NEXT
                                     # TRADING SESSION after its entry day (weekends and holidays carry)
_DAY_CARRY_MAX_DAYS = 6              # hard bound (a 4-day holiday weekend plus slack)
_SAME_DAY_FALLBACK = ("day_tier_log", "promotions")
_SESSIONS_CACHE: dict = {}           # {"at": epoch, "dates": [session dates]} — SPY daily bars, refreshed every 30 min


def _session_dates() -> "list | None":
    """Recent regular-session dates (ET) from SPY daily bars (T1, data/fetcher). None when unreadable."""
    import time
    if _SESSIONS_CACHE and time.time() - _SESSIONS_CACHE.get("at", 0) < 1800:
        return _SESSIONS_CACHE.get("dates")
    try:
        import config
        from data.fetcher import fetch_bars
        df = fetch_bars("SPY", config.TF_DAILY, num_bars=15)
        dates = sorted({(i.astimezone(ET) if getattr(i, "tzinfo", None) else i).date() for i in df.index})
        _SESSIONS_CACHE.update({"at": time.time(), "dates": dates})
        return dates
    except Exception as e:  # noqa: BLE001
        logger.warning("%s session calendar unreadable (weekday rule used): %s", TAG, e)
        _SESSIONS_CACHE.update({"at": time.time(), "dates": None})   # no retry storm on the main thread
        return None


def _carried(entry_day, now: datetime, sessions: "list | None") -> bool:
    """True while a lot entered on a prior day has not yet reached 09:45 ET of the next trading session after it."""
    today = now.date()
    if not 0 < (today - entry_day).days <= _DAY_CARRY_MAX_DAYS:
        return False
    before_cut = now.hour * 60 + now.minute < _DAY_CARRY_UNTIL_MIN
    if sessions:
        after = [d for d in sessions if entry_day < d <= today]
        if not after:
            return True                                    # no session since entry yet (weekend / holiday)
        return after[0] == today and before_cut            # today is the first session after entry: until 09:45
    # weekday fallback (holidays unknown): the first weekday after the entry day
    nxt = entry_day + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return today < nxt or (today == nxt and before_cut)


def _day_tier_claims(now: datetime) -> dict:
    """Signed open Day-tier quantity from the Day tier's own lifecycle log (never the fill-based ledger): today's
    entries, plus prior-day entries still open until 09:45 ET of the next trading session (overnight exit not yet
    filled). Raises when the log is incomplete or a record is malformed (the caller marks the source unreadable)."""
    from strategy import day_tier_logger
    trades, complete = day_tier_logger.open_trades_from_log_checked()
    if not complete:
        raise ValueError("day-tier lifecycle log incomplete")
    today = now.date()
    sessions = None
    out: dict = {}
    for t in trades.values():
        sym = str(t.get("symbol") or "").upper()
        raw = str(t.get("entry_ts") or "")
        if not sym or "T" not in raw:
            raise ValueError(f"day-tier lifecycle record invalid ({sym or '?'})")
        stamp = datetime.fromisoformat(raw)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo("America/Los_Angeles"))   # historical records: PT without offset
        entry_day = stamp.astimezone(ET).date()
        if entry_day != today:
            if sessions is None:
                sessions = _session_dates() or []
            if not _carried(entry_day, now, sessions):
                continue
        if stamp.astimezone(ET) > now:
            raise ValueError(f"day-tier lifecycle timestamp in the future ({sym})")
        qty = abs(float(t.get("fill_qty")))  # type: ignore[arg-type]  # None -> TypeError -> source unreadable
        if not math.isfinite(qty):
            raise ValueError(f"day-tier lifecycle qty invalid ({sym})")
        side = str(t.get("side") or "").lower()
        if side not in ("long", "buy", "short", "sell", "sell_short"):
            raise ValueError(f"day-tier lifecycle side invalid ({sym}): {side!r}")
        out[sym] = out.get(sym, 0.0) + _signed(qty, side)
    return out


def _sources(tracker, now: datetime) -> list:
    def swing_tracker():
        out: dict = {}
        for sym, t in list(getattr(tracker, "open_trades", {}).items()):
            if isinstance(t, dict) and t.get("status") != "closed":
                q = _signed(t.get("qty_remaining", t.get("qty", 0)) or 0, t.get("direction"))
                out[str(sym).upper()] = out.get(str(sym).upper(), 0.0) + q
        return out

    def swing_breakout():
        from execution import swing_breakout_manager as sbm
        out: dict = {}
        for sym, p in (sbm._load_state().get("positions") or {}).items():
            if isinstance(p, dict) and p.get("status") in sbm._HOLDING:
                # an entry still settling records qty 0 until confirmed: count the requested size (the tier owns it)
                q = p.get("qty") or (p.get("req_qty") if p.get("status") in sbm._ENTRY_PENDING else 0) or 0
                out[str(sym).upper()] = out.get(str(sym).upper(), 0.0) + _signed(q, p.get("direction"))
        return out

    def promotions():
        # Day -> Swing hand-off booked out of the Day log ("ready") but not yet adopted into the Swing tracker
        from execution import day_promotion
        d = day_promotion._load()
        if d is None:
            raise ValueError("day-promotion hand-off unreadable")
        out: dict = {}
        for h in d.values():
            if isinstance(h, dict) and h.get("status") == "ready":
                sym = str(h.get("symbol") or "").upper()
                out[sym] = out.get(sym, 0.0) + abs(float(h.get("qty") or 0))
        return out

    def qhm():
        from execution.quarterly_hold_manager import get_quarterly_hold_quantities
        return {str(s).upper(): float(q) for s, q in get_quarterly_hold_quantities().items()}

    def forever6():
        from execution.ownership_guard import load_ledger, tier_qty
        led = load_ledger()
        return {str(s).upper(): float(tier_qty(led, s, "forever6") or 0.0) for s in (led.get("positions") or {})}

    return [("swing_tracker", swing_tracker), ("swing_breakout", swing_breakout), ("promotions", promotions),
            ("day_tier_log", lambda: _day_tier_claims(now)), ("qhm", qhm), ("forever6", forever6)]


def _qhm_pending_entries() -> set:
    """Symbols with a QHM entry order outstanding (state file: an entry_order_id set). Its fill can sit at the broker
    before QHM books it (QHM books fills only inside its >=10:05 ET entry pass, which a kill/halt cycle or the close
    can skip), so a residual on such a symbol is paged as "QHM entry pending", never as "close them". Never raises."""
    try:
        from execution.quarterly_hold_manager import _QHM_STATE_FILE
        d = json.loads(_QHM_STATE_FILE.read_text()) if _QHM_STATE_FILE.exists() else {}
        return {str(sym).upper() for sym, p in (d or {}).items() if isinstance(p, dict) and p.get("entry_order_id")}
    except Exception as e:  # noqa: BLE001
        logger.warning("%s QHM pending-entry read failed: %s", TAG, e)
        return set()


def _managed_qty(tracker, now: "datetime | None" = None) -> "tuple[dict, list, dict]":
    """({symbol: signed qty managed by a live manager}, [unreadable source names], {source: its readings}).
    An unreadable source contributes nothing here; the caller substitutes its last good reading. Never raises."""
    now = (now or datetime.now(ET)).astimezone(ET)
    managed: dict = {}
    errors: list = []
    per: dict = {}
    for name, fn in _sources(tracker, now):
        try:
            got = {s: q for s, q in fn().items() if s and math.isfinite(q) and abs(q) > 1e-9}
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}:{type(e).__name__}")
            continue
        per[name] = got
        for s, q in got.items():
            managed[s] = managed.get(s, 0.0) + q
    return managed, errors, per


_MEM: dict = {}   # in-process copy of the last state: the confirmation memory survives a state file that cannot be
                  # written (an unwritable file must never turn every pass into a "first sighting")


def _load() -> dict:
    if isinstance(_MEM.get("state"), dict):
        return json.loads(json.dumps(_MEM["state"]))
    try:
        d = json.loads(_STATE.read_text()) if _STATE.exists() else {}
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    _MEM["state"] = json.loads(json.dumps(d))
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(d, indent=1))
        os.replace(tmp, _STATE)
        _MEM.pop("save_err_day", None)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s alarm state not saved: %s", TAG, e)
        if _MEM.get("save_err_day") != d.get("day"):
            _MEM["save_err_day"] = d.get("day")
            _page(f"{TAG} alarm state file cannot be written ({e}); dedupe kept in memory until the bot restarts.")


def _page(msg: str) -> None:
    logger.critical(msg)
    try:
        from alerts import send_slack
        send_slack(":rotating_light: " + msg)
    except Exception as e:  # noqa: BLE001
        logger.warning("%s page failed: %s", TAG, e)


def check_unmanaged(tracker, now_et: "datetime | None" = None) -> dict:
    """One pass: page any whole-share residual no live manager manages. Returns {symbol: residual} (empty when the
    broker read fails). Never raises; never places an order."""
    out: dict = {}
    try:
        from execution import broker
        now = (now_et or datetime.now(ET)).astimezone(ET)
        day = now.date().isoformat()
        state = _load()
        _lg = state.get("last_good")
        last_good: dict = _lg if isinstance(_lg, dict) else {}
        if state.get("day") != day:
            state = {"day": day, "paged": {}, "unverifiable": [], "seen_prev": state.get("seen_prev") or {},
                     "seen_prev2": state.get("seen_prev2") or {}}
        state["last_good"] = last_good
        seen = state.setdefault("unverifiable", [])
        if not isinstance(seen, list):
            seen = state["unverifiable"] = []
        try:
            positions = broker.get_open_positions()
            if positions is None:
                raise RuntimeError("positions unreadable")
        except Exception as e:  # noqa: BLE001
            if "broker" not in seen:
                seen.append("broker")
                _page(f"{TAG} cannot verify position ownership: broker positions unreadable ({e}).")
            _save(state)
            return out
        net = {}
        value = {}
        for p in positions:
            if str(getattr(p, "asset_class", "us_equity") or "us_equity").lower().split(".")[-1] != "us_equity":
                continue        # option contracts etc.: no share tier claims them
            s = str(getattr(p, "symbol", "") or "").upper()
            try:
                net[s] = float(getattr(p, "qty", 0) or 0)
                value[s] = float(getattr(p, "market_value", 0) or 0)
            except (TypeError, ValueError):
                continue
        managed, errors, per = _managed_qty(tracker, now)
        _lgd = state.get("last_good_day")
        lg_day: dict = _lgd if isinstance(_lgd, dict) else {}
        state["last_good_day"] = lg_day
        for name, got in per.items():
            last_good[name] = got
            lg_day[name] = day
        stale = []
        for err in errors:
            name = err.split(":", 1)[0]
            fallback = last_good.get(name)
            if name in _SAME_DAY_FALLBACK and lg_day.get(name) != day:
                fallback = None    # Day-tier lots and hand-offs turn over daily: a prior day's reading is not evidence
            if isinstance(fallback, dict):
                stale.append(name)
                for s, q in fallback.items():
                    managed[s] = managed.get(s, 0.0) + float(q)
            if err not in seen:
                seen.append(err)
                _page(f"{TAG} ownership source unreadable: {err}. "
                      + (f"Using its last good reading ({len(fallback)} symbol(s)); "
                         if isinstance(fallback, dict) else "No last good reading — its symbols will page as unmanaged; ")
                      + "verify any unmanaged-position page before acting.")
        caveat = (f" Ownership partly UNVERIFIABLE ({', '.join(errors)}"
                  f"{'; last good reading used' if stale else ''}) — verify before acting.") if errors else ""
        paged = state.setdefault("paged", {})
        qhm_pending = _qhm_pending_entries()
        # {symbol|kind: [|residual|, managed]} seen on each of the previous two passes. A residual pages only at the
        # size an earlier pass confirms, less whatever the tiers booked since then (managed moving toward the broker):
        # a fill seen seconds before its tier books it — even two in a row on one stock — never pages, while shares
        # no tier ever books (managed unchanged) page on the next pass.
        _sp, _sp2 = state.get("seen_prev"), state.get("seen_prev2")
        prev1: dict = _sp if isinstance(_sp, dict) else {}
        prev2: dict = _sp2 if isinstance(_sp2, dict) else {}
        seen_now: dict = {}
        keys_now: set = set()
        for s, q in net.items():
            m = managed.get(s, 0.0)
            resid = q - m
            if abs(resid) < 1.0 - 1e-9:
                continue
            out[s] = resid
            over = (q > 0 > resid) or (q < 0 < resid) or q == 0
            try:
                import trade_logger
                trade_logger.log_event("unmanaged_position", symbol=s, size=int(abs(resid)), broker_qty=q,
                                       managed_qty=m, residual=resid, over_claim=over,
                                       market_value=value.get(s, 0.0), unverifiable_sources=errors)
            except Exception as e:  # noqa: BLE001
                logger.warning("%s event not written for %s: %s", TAG, s, e)
            key = f"{s}|{'over' if over else ('long' if resid > 0 else 'short')}"
            seen_now[key] = [abs(resid), m]
            keys_now.add(key)
            sign = 1.0 if resid > 0 else -1.0
            earlier = 0.0
            for prev in (prev1, prev2):
                rec = prev.get(key)
                if isinstance(rec, list) and len(rec) == 2:
                    try:
                        booked = max(0.0, sign * (m - float(rec[1])))     # tier bookings that shrank the residual
                        earlier = max(earlier, float(rec[0]) - booked)
                    except (TypeError, ValueError):
                        continue
            confirmed = min(abs(resid), earlier)
            if confirmed < 1.0 - 1e-9:
                # first sighting: a tier's fill can land seconds before it books it (Day-tier fill-to-log race,
                # a hand-off mid-booking) — page only when the residual is still there on a following pass
                logger.warning("%s [%s] residual %g first seen — paging if it persists", TAG, s, resid)
            elif confirmed > float(paged.get(key) or 0.0) + 1e-9:
                paged[key] = confirmed
                if s in qhm_pending and resid > 0 and not over:
                    _page(f"{TAG} [{s}] {confirmed:g} share(s) held at Alpaca that no tier has booked yet, while a "
                          f"QHM entry order is outstanding on {s} — most likely its fill awaiting QHM's next booking "
                          f"pass (from 10:05 ET). Do NOT close; verify the QHM order filled.{caveat}")
                elif over:
                    _page(f"{TAG} [{s}] tiers record {m:g} share(s) but Alpaca holds {q:g} — a tier's records are "
                          f"ahead of the account (a sale or exit it has not booked).{caveat}")
                else:
                    mv = abs(value.get(s, 0.0)) * (confirmed / abs(q))
                    _page(f"{TAG} [{s}] {confirmed:g} share(s) ({'long' if resid > 0 else 'short'}, ~${mv:,.0f}) are "
                          f"held at Alpaca but NO tier manages them (broker {q:g}, managed {m:g}) — no tier will place "
                          f"a stop or exit them. Assign them to a tier or close them.{caveat}")
            else:
                # a residual that shrank for two passes lowers the mark, so a later regrowth the
                # same day pages again (e.g. UBER 72 -> 7 after a sale, then a new unmanaged lot)
                if key in paged:
                    _p1 = prev1.get(key)
                    try:
                        _last = float(_p1[0]) if isinstance(_p1, list) and _p1 else abs(resid)
                    except (TypeError, ValueError):
                        _last = abs(resid)     # a malformed memory record never stalls the alarm
                    # low this pass AND last pass (or absent last pass) -> the mark follows it down
                    paged[key] = min(float(paged[key]), max(abs(resid), _last))
                logger.warning("%s [%s] still unmanaged: residual %g (paged)", TAG, s, resid)
        state["seen_prev2"] = prev1
        state["seen_prev"] = seen_now
        for key in [k for k in paged if k not in keys_now and k not in prev1]:   # absent two passes in a row
            paged.pop(key, None)        # cleared, flipped kind, or no longer held: a reappearance pages again
        _save(state)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("%s check failed: %s", TAG, e)
        return out
