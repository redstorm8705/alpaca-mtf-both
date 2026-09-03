# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/day_trade_manager.py — Day-Tier Track-A ORDER EXECUTION (RISK-PATH; INERT behind DAYTRADE_ENABLED).

Increment 3 of the live day-tier build. Turns a triggered, sized day-tier decision into a LIVE
(paper) order with a confirmed protective stop, and owns the day-tier's flatten paths — entirely
behind the `config.DAYTRADE_ENABLED` master flag (False today → every public entrypoint no-ops, so
this ships INERT even though it is risk-path CODE; the runner that calls it is a later increment).

Design record: logs/design_records/day_tier_live_build_2026-09-02.md (blockers B1-B9 + Rafael's
amendments). Hardened after a 3-seat risk-path review (cold-2nd + reliability + masked-loss,
2026-09-02) — the fixes are called out inline as "(review …)".

SAFETY GUARANTEES (each traced to a board finding):
  B1 — NEVER a whole-symbol close. The day-tier flattens ONLY its own recorded qty via
       broker.partial_close_position(qty, tier="daytrade"). The module has NO code path to
       broker.close_position / close_all_positions (grep-verifiable — this structural absence is
       the guard; there is no runtime assert). close_position with OWNERSHIP_GUARD_ENFORCE=False
       (today's default) is a raw Alpaca DELETE /positions/{symbol} = liquidates the combined net
       across ALL tiers. PLUS (masked-loss seat D): the day-tier NEVER opens a side OPPOSITE an
       existing position on the symbol — partial_close infers the close side from the NET Alpaca
       position, so an opposite-side co-hold would net down and close another tier's shares; the
       opposite-side entry guard keeps net side == day-tier side, so the inferred close side is
       always ours. With that invariant, the flatten is structurally unable to touch another tier.
  B2 — Confirm-fill → place stop → VERIFY it is live → RETRY DAYTRADE_STOP_RETRIES more times
       (cancelling the prior stop each attempt so at most one ever rests) → only if STILL not live,
       scoped-flatten (B1). Every post-fill step is inside a try/except whose except path flattens
       an unprotected fill and PAGES — a raise after a fill can never leave a naked position or
       propagate into the runner (cold-2nd Threat 1). The kill/risk gate reads POST-fill state.
  B3 — Per-(symbol, bar_id) idempotency written to day_tier_state.json (atomic tmp+replace+fsync)
       BEFORE submit; the write is CHECKED and the entry ABORTS if it fails (cold-2nd/reliability/
       masked-loss Finding C — a swallowed write let the same ENTER re-fire and double the size).
       An already-open day-tier position on the symbol (log OR state) also blocks re-entry.
  B6 — Cumulative day-tier gross cap (≤ DAYTRADE_ALLOC_PCT × DAYTRADE_TRACK_A_PCT of equity) + the
       ~$650 maintenance cushion, read from the LIVE book at wire-time, fail-CLOSED.

CONCURRENCY: this module does whole-file read-modify-write on day_tier_state.json and has NO
internal lock. Correctness under concurrent invocations is DELEGATED to the runner's flock (a
single day-tier runner process). The runner MUST hold an exclusive flock for the life of a tick.

v1 EXIT MODEL (safety-first, deliberate): entry + protective DAY stop ONLY — NO separate target
limit. A naked stop + a naked target with no broker OCO could BOTH fill (stop fires, then target
executes on a now-closed position → a NEW opposite position). v1 exits via the stop (downside) or
the EOD force-flat (captures intraday gain). The EOD force-flat is the RUNNER's responsibility
(force_flat_all at T-DAYTRADE_FORCE_FLAT_MINUTES) — without it a position rides overnight, so the
runner increment MUST wire it. A bracketed pin-target / trailing stop is a tracked fast-follow.

DURABLE LOGGING (keystone): every decision/entry_fill/stop_placed/exit_fill → day_tier_logger
(fsync'd price path, trade_id=coid), AND the canonical entry/exit → trade_logger (trade_events.jsonl,
the P&L system of record — realized P&L is authoritatively reconstructed there from Alpaca FILL
activities). The exit realized_pnl/price in day_tier_events is a flatten-TIME MARK (not the exact
fill), captured before the close so a losing exit is never logged as $0.00 (masked-loss Finding A).

FAIL-SAFE: DAYTRADE_ENABLED False → all public functions no-op; any error in an entry attempt aborts
THAT symbol only (never raises into the runner loop); a flatten failure PAGES loudly.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# Idempotency / open-position state file (single-owner = the 2-3 min runner; see CONCURRENCY note).
# Anchored to __file__ (RC-2), atomic tmp+replace+fsync (RC-5).
_STATE = Path(__file__).resolve().parent.parent / "logs" / "day_tier_state.json"

_KILL_KEY = "_tier_killed_date"  # a "killed for the day" flag; survives a restart via the state file
# Terminal entry-record states pruned after _STATE_TTL_DAYS (reliability: unbounded-growth leak +
# rising fsync cost on the hot path). Non-terminal states are NEVER pruned (they gate re-entry).
_TERMINAL_STATES = frozenset({"protected", "flattened_no_stop", "flatten_failed",
                              "submit_failed", "unfilled_cancelled"})
_STATE_TTL_DAYS = 3


def _cfg(name: str, default):
    return getattr(config, name, default)


def _now_et() -> datetime:
    return datetime.now(ET)


def bar_id_for(now_et: datetime | None = None, minutes: int = 15) -> str:
    """Signal-bar idempotency key: the ET open of the current `minutes`-bucket, 'YYYYMMDD-HHMM'."""
    n = now_et or _now_et()
    bucket_min = (n.minute // minutes) * minutes
    return f"{n:%Y%m%d}-{n.hour:02d}{bucket_min:02d}"


# ── state file (atomic; single-owner via the runner flock) ─────────────────────────────────────
def _prune_state(state: dict) -> dict:
    """Drop TERMINAL entry:: records older than _STATE_TTL_DAYS (parsed from the record's bar_id
    date prefix). Non-terminal records and non-entry keys (e.g. the kill flag) are kept."""
    try:
        cutoff = (_now_et() - timedelta(days=_STATE_TTL_DAYS)).strftime("%Y%m%d")
        drop = []
        for k, v in state.items():
            if not k.startswith("entry::") or not isinstance(v, dict):
                continue
            if v.get("state") in _TERMINAL_STATES:
                bid = str(v.get("bar_id") or "")
                day = bid.split("-", 1)[0] if "-" in bid else ""
                if day and day < cutoff:
                    drop.append(k)
        for k in drop:
            state.pop(k, None)
    except Exception as e:  # noqa: BLE001 — pruning must never break state I/O
        logger.debug("state prune skipped: %s", e)
    return state


def _load_state() -> dict:
    try:
        if _STATE.exists():
            with open(_STATE, encoding="utf-8") as f:
                d = json.load(f)
                return _prune_state(d) if isinstance(d, dict) else {}
    except Exception as e:
        logger.warning("day_tier_state read failed (treating as empty): %s", e)
    return {}


def _save_state(state: dict) -> bool:
    """Atomic tmp→replace→fsync (RC-5). Returns False on failure (never raises). Callers on the
    safety path (the pre-submit idempotency write) MUST check the return and fail closed."""
    try:
        os.makedirs(_STATE.parent, exist_ok=True)
        tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STATE)
        return True
    except Exception as e:
        logger.error("day_tier_state write FAILED: %s", e)
        return False


def _entry_key(symbol: str, bar_id: str) -> str:
    return f"entry::{symbol}::{bar_id}"


def _page(msg: str) -> None:
    """Loud operator escalation for a flatten/naked failure — never raises."""
    logger.critical(msg)
    try:
        from alerts import send_slack
        send_slack("🚨 DAY-TIER ORDER ALERT\n" + msg)
    except Exception as e:  # pragma: no cover
        logger.error("day_tier_manager: page send failed: %s", e)


def _enabled() -> bool:
    return bool(_cfg("DAYTRADE_ENABLED", False))


def _tier_killed_today(state: dict) -> bool:
    return state.get(_KILL_KEY) == f"{_now_et():%Y%m%d}"


# ── gross-cap + cushion (B6) ───────────────────────────────────────────────────────────────────
def _current_daytrade_gross(open_trades: dict, positions_by_symbol: dict) -> float:
    """Day-tier's OWN gross notional = Σ (own recorded qty × current price). Uses the day-tier's
    recorded fill_qty (NOT the position's whole market_value — a symbol may be co-held), priced at
    the live position's current_price, falling back to the recorded entry price so a missing quote
    never UNDER-counts gross (fail-toward-conservative for the cap)."""
    gross = 0.0
    for t in open_trades.values():
        sym = t.get("symbol")
        qty = abs(float(t.get("fill_qty") or 0.0))
        pos = positions_by_symbol.get(sym)
        px = 0.0
        if pos is not None:
            try:
                px = abs(float(getattr(pos, "current_price", 0.0) or 0.0))
            except Exception:
                px = 0.0
        if px <= 0:
            try:
                px = abs(float(t.get("entry_price") or 0.0))
            except Exception:
                px = 0.0
        gross += qty * px
    return gross


def _gross_cap_ok(new_notional: float, equity: float, open_trades: dict,
                  positions_by_symbol: dict, buying_power: float) -> tuple[bool, str]:
    """B6: adding `new_notional` must keep (a) total Track-A day-tier gross ≤ alloc×trackA×equity,
    and (b) buying power minus the new notional ≥ the maintenance cushion. Fail-CLOSED on bad input."""
    try:
        alloc = float(_cfg("DAYTRADE_ALLOC_PCT", 0.15))
        track_a = float(_cfg("DAYTRADE_TRACK_A_PCT", 0.65))
        cushion = float(_cfg("DAYTRADE_MAINT_CUSHION_USD", 650.0))
        if equity <= 0 or new_notional <= 0:
            return False, f"bad inputs equity={equity} new_notional={new_notional}"
        cap = equity * alloc * track_a
        cur = _current_daytrade_gross(open_trades, positions_by_symbol)
        if cur + new_notional > cap + 1e-6:
            return False, f"gross cap: {cur:.2f}+{new_notional:.2f} > cap {cap:.2f} (alloc {alloc}×A {track_a}×eq {equity:.2f})"
        if buying_power - new_notional < cushion:
            return False, f"cushion: BP {buying_power:.2f} − {new_notional:.2f} < ${cushion:.0f}"
        return True, f"gross {cur:.2f}+{new_notional:.2f} ≤ cap {cap:.2f}; BP ok"
    except Exception as e:
        return False, f"gross-cap check error (fail-closed): {e!r}"


# ── structural stop price ──────────────────────────────────────────────────────────────────────
def _compute_stop_price(trigger: dict, direction: str, entry_px: float) -> float | None:
    """Structural stop (design §7 'stop tight relative to pin distance, R≈1:1'):
      FADE (target=centroid on the PROFIT side): risk = reward → stop mirrors the target across entry.
      RIDE (target=None): stop = the broken wall (wall_ref) ± a small buffer (the setup invalidates
        if price falls back through the wall).
    Returns None if it cannot compute a SANE, protective stop (caller aborts — never a naked entry)."""
    try:
        mode = trigger.get("mode")
        target = trigger.get("target")
        wall = trigger.get("wall_ref")
        buf = float(_cfg("DAYTRADE_STOP_BUFFER_PCT", 0.001))
        e = float(entry_px)
        if e <= 0:
            return None
        if mode == "FADE" and target is not None:
            t = float(target)
            # A valid fade targets the pin on the PROFIT side (long → pin above entry; short → below).
            # A loss-side target is NOT a fade-to-pin → abort rather than place an inverted-R:R stop.
            if (direction == "long" and t <= e) or (direction == "short" and t >= e):
                return None
            reward = abs(e - t)
            if reward <= 0:
                return None
            stop = e + reward if direction == "short" else e - reward
        else:  # RIDE (or FADE with no target) → wall-based invalidation
            if wall is None:
                return None
            w = float(wall)
            stop = w * (1.0 - buf) if direction == "long" else w * (1.0 + buf)
        stop = round(float(stop), 2)
        if direction == "long" and stop >= e:
            return None
        if direction == "short" and stop <= e:
            return None
        if stop <= 0:
            return None
        return stop
    except Exception as e:  # noqa: BLE001
        logger.warning("day-tier stop-price compute failed: %s", e)
        return None


# ── fill confirmation ──────────────────────────────────────────────────────────────────────────
def _confirm_fill(order_id: str) -> bool:
    """Poll broker.get_order until the entry order shows ANY fill (filled_qty > 0) or the poll
    budget is exhausted. Returns True on the first sign of a fill (the caller then cancels the
    resting remainder and re-reads the order for the AUTHORITATIVE final filled_qty — so a partial
    can never leave an uncovered remainder), False if nothing filled. Never raises."""
    from execution import broker
    polls = max(1, int(_cfg("DAYTRADE_FILL_POLL_MAX", 8)))
    wait = float(_cfg("DAYTRADE_FILL_POLL_S", 1.0))
    for i in range(polls):
        try:
            o = broker.get_order(order_id)
            if o is not None:
                fq = float(getattr(o, "filled_qty", 0) or 0)
                status = str(getattr(o, "status", "")).lower()
                if fq > 0:
                    return True
                if status in ("canceled", "expired", "rejected", "done_for_day"):
                    return False  # terminal with zero fill → nothing to protect
        except Exception as e:  # noqa: BLE001
            logger.debug("fill poll error (order %s): %s", order_id, e)
        if i < polls - 1:
            time.sleep(wait)
    return False


def _final_fill(order_id: str) -> tuple[float, float]:
    """Re-read the entry order AFTER the resting remainder has been cancelled → its final
    (filled_qty, filled_avg_price). This is the AUTHORITATIVE day-tier fill (the ORDER's fill, not
    the symbol's net position, which may be co-held). Returns (0.0, 0.0) if unreadable/unfilled."""
    from execution import broker
    try:
        o = broker.get_order(order_id)
        if o is None:
            return 0.0, 0.0
        fq = float(getattr(o, "filled_qty", 0) or 0)
        fp = getattr(o, "filled_avg_price", None)
        return (fq, float(fp)) if (fq > 0 and fp is not None) else (0.0, 0.0)
    except Exception as e:  # noqa: BLE001
        logger.debug("final-fill read error (order %s): %s", order_id, e)
        return 0.0, 0.0


def _stop_is_live(order_obj) -> bool:
    """A submit_day_stop_order return is 'live' iff Alpaca ACCEPTED it: a real order object with a
    non-empty id (the PROTECTION_* sentinels and None are NOT). We TRUST the accepted submit return
    directly (reliability seat: a fresh get_order re-read that transiently fails would drive an
    unnecessary false-flatten of a genuinely-protected position). Duplicate-stop safety comes from
    cancelling the prior stop before each retry, not from a re-read."""
    from execution import broker
    if order_obj is None or order_obj is broker.PROTECTION_ALREADY_HELD or order_obj is broker.PROTECTION_UNKNOWN:
        return False
    return bool(getattr(order_obj, "id", None))


# ── flatten (scoped, B1) ─────────────────────────────────────────────────────────────────────
def flatten_position(symbol: str, qty: int, position_side: str, *, entry_price: float = 0.0,
                     trade_id: str = "", order_id_hint: str = "", reason: str = "flatten") -> bool:
    """Close ONLY the day-tier's own `qty` shares of `symbol` (B1). position_side is the day-tier's
    position side ("long"/"short"); the close is the opposite. Cancels the day-tier's OWN resting
    orders first (tier-scoped — never a co-held tier's stop), then partial_close_position(tier=
    "daytrade"). NEVER a whole-symbol close. Captures a flatten-TIME market mark BEFORE the close so
    the durable exit log records a real (non-zero) exit price + realized P&L (masked-loss Finding A);
    the exact fill lives in Alpaca/fifo_pnl. PAGES on failure. Returns True on a confirmed close."""
    from execution import broker
    from strategy import day_tier_logger
    import trade_logger
    if qty < 1:
        return True
    try:
        # Read the live position ONCE — for the flatten-time mark AND the net-side/qty guard.
        pos = None
        try:
            pos = broker.get_open_position(symbol)
        except Exception:
            pos = None
        if pos is None:
            return True  # already flat — nothing to close
        # NET-SIDE / QTY GUARD (masked-loss D on the FLATTEN path): partial_close infers the close
        # side from the LIVE NET; a co-held tier that stacked an OPPOSITE position AFTER our entry
        # would flip the net, so a blind close would reduce the WRONG tier's shares and leave us
        # naked. REFUSE + page unless the net side is OURS and the net covers our qty.
        net_is_long = (getattr(pos, "side", None) == "long")
        if net_is_long != (position_side == "long"):
            _page(f"[{symbol}] day-tier flatten REFUSED — live net side ({getattr(pos, 'side', '?')}) "
                  f"is OPPOSITE our {position_side} (a co-held tier flipped the net). NOT closing "
                  f"(would reduce another tier); {qty} sh day-tier {position_side} may be OPEN. ({reason})")
            return False
        net_qty = abs(int(float(getattr(pos, "qty", 0) or 0)))
        if net_qty < qty:
            _page(f"[{symbol}] day-tier flatten REFUSED — live net qty {net_qty} < our {qty} "
                  f"(drift/partial co-hold) — NOT closing more than the net. ({reason})")
            return False
        # Flatten-time mark (fallback to entry_price so a losing exit is never logged 0.0 on a quote outage).
        try:
            mark = abs(float(getattr(pos, "current_price", 0.0) or 0.0))
        except Exception:
            mark = 0.0
        if mark <= 0:
            mark = abs(float(entry_price or 0.0))
        # Cancel our OWN resting orders (the protective stop) so the market reduce isn't blocked.
        try:
            broker.cancel_open_orders_for_symbol(symbol, only_tier="daytrade")
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] flatten: tier-scoped cancel raised (continuing): %s", symbol, e)
        ok = broker.partial_close_position(symbol, int(qty), tier="daytrade")
        # Mark-based realized for the enrichment log (exact fill is authoritative in Alpaca).
        ep = abs(float(entry_price or 0.0))
        realized = 0.0
        if mark > 0 and ep > 0:
            realized = round((mark - ep) * qty if position_side == "long" else (ep - mark) * qty, 2)
        if ok:
            day_tier_logger.log_exit_fill(trade_id or f"DT-{symbol}", symbol,
                                          order_id=order_id_hint, exit_reason=reason,
                                          fill_price=mark, fill_qty=float(qty),
                                          market_price_at_exit=mark, realized_pnl=realized)
            trade_logger.log_event("exit", symbol=symbol, price=mark, size=int(qty),
                                   data_source="daytrade", tier="daytrade", exit_reason=reason,
                                   trade_id=trade_id, realized_pnl=realized)
            logger.info("[%s] day-tier flattened %d sh (%s) mark %.2f realized~%.2f",
                        symbol, qty, reason, mark, realized)
            return True
        _page(f"[{symbol}] day-tier flatten FAILED ({reason}) — {qty} sh may still be OPEN. Manual check required.")
        return False
    except Exception as e:  # noqa: BLE001
        _page(f"[{symbol}] day-tier flatten RAISED ({reason}): {e!r} — {qty} sh may be OPEN.")
        return False


def _flatten_targets() -> dict:
    """The day-tier's open positions to force-flat, keyed by symbol, unioning TWO sources: (1) the
    exit-aware durable log (open_trades_from_log — the authoritative open set) and (2) the state
    file's filled/protected/fill_unverified records. The state arm covers the case where the LOG
    write failed but the STATE write succeeded (filled/protected are written after the log attempt),
    and surfaces a fill_unverified record for a loud EOD page. It does NOT cover the process-crash
    submit→log window (that record stays 'submitted') — that gap is closed by the runner's startup
    coid↔Alpaca reconcile, not here (masked-loss re-review). Each target carries qty/side/entry_price/
    trade_id/order_id for a scoped flatten."""
    from strategy import day_tier_logger
    targets: dict = {}
    try:
        for t in day_tier_logger.open_trades_from_log().values():
            sym = str(t.get("symbol") or "")
            if sym:
                targets[sym] = {"symbol": sym, "side": str(t.get("side") or "long"),
                                "qty": abs(int(float(t.get("fill_qty") or 0))),
                                "entry_price": float(t.get("entry_price") or 0.0),
                                "trade_id": str(t.get("trade_id") or ""),
                                "order_id": str(t.get("order_id") or "")}
    except Exception as e:  # noqa: BLE001
        logger.warning("flatten targets: log read failed: %s", e)
    try:
        state = _load_state()
        for k, v in state.items():
            if not k.startswith("entry::") or not isinstance(v, dict):
                continue
            if v.get("state") in ("filled", "protected", "fill_unverified"):
                sym = str(v.get("symbol") or "")
                if sym and sym not in targets:  # log is preferred; state fills the crash-window gap
                    targets[sym] = {"symbol": sym, "side": str(v.get("side") or "long"),
                                    "qty": abs(int(float(v.get("fill_qty") or v.get("qty") or 0))),
                                    "entry_price": float(v.get("fill_px") or v.get("stop_px") or 0.0),
                                    "trade_id": str(v.get("coid") or ""),
                                    "order_id": str(v.get("order_id") or "")}
    except Exception as e:  # noqa: BLE001
        logger.warning("flatten targets: state read failed: %s", e)
    return targets


def force_flat_all(reason: str = "eod_force_flat") -> int:
    """Flatten EVERY open day-tier position (EOD force-flat / tier-kill). Unions the durable-log
    open set with the state file's filled/protected records (crash-window safety), reconciled
    against the live broker position. NEVER falls back to the whole broker-held qty (B1: masked-loss
    /cold-2nd — a missing recorded qty must SKIP+PAGE, never close `held`, which is the cross-tier
    net). Returns the count flattened. No-op when DAYTRADE_ENABLED is False."""
    if not _enabled():
        return 0
    from execution import broker
    n = 0
    try:
        for tgt in _flatten_targets().values():
            sym = tgt["symbol"]
            try:
                pos = broker.get_open_position(sym)
            except Exception:
                pos = None
            if pos is None:
                continue
            held = abs(int(float(getattr(pos, "qty", 0) or 0)))
            want = int(tgt["qty"])
            if want < 1:
                _page(f"[{sym}] day-tier force-flat: recorded own-qty is 0/missing — REFUSING to "
                      f"close (never close the cross-tier net); manual check needed. ({reason})")
                continue
            qty = min(held, want)
            if qty >= 1 and flatten_position(sym, qty, tgt["side"], entry_price=tgt["entry_price"],
                                             trade_id=tgt["trade_id"], order_id_hint=tgt["order_id"],
                                             reason=reason):
                n += 1
    except Exception as e:  # noqa: BLE001
        _page(f"day-tier force_flat_all RAISED ({reason}): {e!r}")
    return n


# ── entry (B1/B2/B3/B6 + Rafael amendments) ─────────────────────────────────────────────────────
def _mint_coid(symbol: str, direction: str) -> str:
    """Stable day-tier client_order_id via ownership_guard.make_coid (tier 'daytrade' → 'DT-...').
    Falls back to a plain unique string if make_coid rejects (never raises)."""
    side1 = "b" if direction == "long" else "s"
    epoch = int(time.time() * 1000)
    uniq = uuid.uuid4().hex[:8]
    try:
        from execution.ownership_guard import make_coid
        return make_coid("daytrade", symbol, side1, epoch, uniq)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] make_coid failed (%s) — fallback coid", symbol, e)
        return f"DT-{symbol}-{side1}-{epoch}-{uniq}"


def place_entry(symbol: str, decision: dict, trigger: dict, size: dict, *,
                bar_id: str, equity: float, decision_id: str = "") -> bool:
    """Place ONE day-tier Track-A entry with a confirmed protective stop. Idempotent per
    (symbol, bar_id). Returns True on a filled+protected entry, False otherwise. NEVER raises into
    the caller (the runner). No-op when DAYTRADE_ENABLED is False."""
    if not _enabled():
        return False
    from execution import broker
    from strategy import day_tier_logger
    import trade_logger

    # ── validate the signal ───────────────────────────────────────────────────────────────────
    if not (isinstance(trigger, dict) and trigger.get("trigger") == "ENTER"):
        return False
    if not (isinstance(size, dict) and size.get("size_ok")):
        return False
    direction = trigger.get("direction")
    if direction not in ("long", "short"):
        return False
    qty = int(size.get("shares") or 0)
    if qty < 1:
        return False
    entry_ref = trigger.get("entry_ref")
    try:
        entry_ref = float(entry_ref)  # type: ignore[arg-type]  # None/non-numeric caught below
    except (TypeError, ValueError):
        return False
    if entry_ref <= 0:
        return False

    key = _entry_key(symbol, bar_id)
    entry_coid = ""
    entry_order_id = ""
    filled_qty_i = 0        # >0 once we hold shares — the naked-guard trips on this in the except
    fill_px = 0.0
    protected = False
    try:
        state = _load_state()
        if _tier_killed_today(state):
            logger.info("[%s] day-tier entry skipped — tier killed for the day", symbol)
            return False
        # B3 idempotency: one entry per (symbol, bar_id); also block if a non-terminal record exists.
        if key in state:
            logger.info("[%s] day-tier entry skipped — already acted for bar %s", symbol, bar_id)
            return False
        # Already holding a day-tier position on this symbol. The exit-aware durable log is
        # authoritative for filled/exited (a trade with an exit_fill is NOT in open_trades_from_log);
        # the state file adds the submit→log window ('submitted', any day) plus SAME-DAY filled/
        # protected/fill_unverified records. A prior-day post-log record must NOT block (cold-2nd T3:
        # 'protected' gets no exit transition here, so it would otherwise block next-day re-entry).
        open_trades = day_tier_logger.open_trades_from_log()
        _today = f"{_now_et():%Y%m%d}"

        def _state_blocks(v: dict) -> bool:
            # Block re-entry only on a SAME-DAY unresolved record. reconcile_open_state resolves a
            # 'submitted' record every tick (before entries), and a prior-day record is stale — it
            # must not permanently bench the symbol (masked-loss re-review note).
            if v.get("state") in ("submitted", "filled", "protected", "fill_unverified"):
                return str(v.get("bar_id") or "").split("-", 1)[0] == _today
            return False

        if any(t.get("symbol") == symbol for t in open_trades.values()) or any(
            k.startswith("entry::") and isinstance(v, dict) and v.get("symbol") == symbol and _state_blocks(v)
            for k, v in state.items()
        ):
            logger.info("[%s] day-tier entry skipped — day-tier position/order already active", symbol)
            return False

        # Structural stop FIRST (never enter a position we can't protect — B2 precondition).
        stop_px = _compute_stop_price(trigger, direction, entry_ref)
        if stop_px is None:
            logger.warning("[%s] day-tier entry aborted — no sane structural stop", symbol)
            return False

        # Live book (fail-CLOSED) — used for BOTH the opposite-side guard and the B6 gross cap.
        try:
            acct = broker.get_account()
            buying_power = float(getattr(acct, "buying_power", 0.0) or 0.0)
            positions = broker.get_open_positions()
            pos_by_sym = {getattr(p, "symbol", None): p for p in (positions or [])}
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] day-tier entry aborted — live book unreadable (fail-closed): %s", symbol, e)
            return False

        # OPPOSITE-SIDE CO-HOLD GUARD (masked-loss D): never open a side opposite an existing
        # position on this symbol — the flatten infers its side from the NET position, so an
        # opposite-side co-hold would net down and close another tier's shares. Compare with the
        # proven `== "long"` pattern (broker.py:1261) — robust to the Alpaca PositionSide str-enum
        # (str(enum).lower() renders 'positionside.long', never == 'long' — cold-2nd T2).
        existing = pos_by_sym.get(symbol)
        was_flat = existing is None
        if existing is not None:
            if (getattr(existing, "side", None) == "long") != (direction == "long"):
                logger.info("[%s] day-tier entry skipped — existing %s position opposite our %s "
                            "(no cross-tier netting)", symbol, getattr(existing, "side", "?"), direction)
                return False

        new_notional = qty * entry_ref
        ok, why = _gross_cap_ok(new_notional, equity, open_trades, pos_by_sym, buying_power)
        if not ok:
            logger.info("[%s] day-tier entry skipped — %s", symbol, why)
            return False

        # B3: mint the coid + WRITE the idempotency record BEFORE submit — and FAIL CLOSED if the
        # write does not persist (Finding C: a swallowed write let the same ENTER re-fire → double).
        coid = _mint_coid(symbol, direction)
        state[key] = {"bar_id": bar_id, "coid": coid, "state": "submitting", "symbol": symbol,
                      "side": direction, "ts": datetime.now(PT).isoformat(), "qty": qty, "stop_px": stop_px}
        if not _save_state(state):
            _page(f"[{symbol}] day-tier entry ABORTED — could not persist the idempotency record "
                  f"(B3). Not submitting (a re-fire could double the position).")
            return False

        # Marketable-limit entry (cap the worst fill vs a naked market order).
        slip = float(_cfg("DAYTRADE_ENTRY_SLIPPAGE_PCT", 0.002))
        order_side = "buy" if direction == "long" else "sell"
        limit_px = round(entry_ref * (1.0 + slip) if direction == "long" else entry_ref * (1.0 - slip), 2)
        day_tier_logger.log_decision(decision_id or coid, symbol, decision=decision, trigger=trigger,
                                     size=size, trade_id=coid)
        order = broker.submit_limit_order(symbol, qty, order_side, limit_px, tier="daytrade")
        if order is None or not getattr(order, "id", None):
            state[key]["state"] = "submit_failed"
            _save_state(state)
            logger.warning("[%s] day-tier entry submit returned no order — aborting", symbol)
            return False
        entry_order_id = str(getattr(order, "id", "") or "")
        entry_coid = str(getattr(order, "client_order_id", coid) or coid)
        state[key].update(state="submitted", order_id=entry_order_id, coid=entry_coid)
        _save_state(state)

        # Confirm ANY fill, then cancel the resting remainder and re-read the ORDER's FINAL fill.
        got_fill = _confirm_fill(entry_order_id)
        try:
            broker.cancel_open_orders_for_symbol(symbol, only_tier="daytrade")  # stop further fills
        except Exception:
            pass
        fq, fp = _final_fill(entry_order_id)
        # A fill may need a beat to settle on the order object — retry the authoritative order read.
        for _ in range(3):
            if fq >= 1:
                break
            time.sleep(0.5)
            fq, fp = _final_fill(entry_order_id)
        if not got_fill and fq < 1:
            # Nothing filled (and the cancel caught any late fill) → clean abort, no position.
            state[key]["state"] = "unfilled_cancelled"
            _save_state(state)
            logger.warning("[%s] day-tier entry never filled — cancelled resting entry", symbol)
            return False
        if fq < 1 and was_flat:
            # A fill was CONFIRMED but the order read can't quantify it; the symbol was FLAT before
            # entry, so the whole live position IS the day-tier's own qty (no co-hold to over-read).
            try:
                pos = broker.get_open_position(symbol)
                fq = abs(float(getattr(pos, "qty", 0) or 0)) if pos is not None else 0.0
                fp = abs(float(getattr(pos, "avg_entry_price", entry_ref) or entry_ref)) if pos is not None else entry_ref
            except Exception:
                fq, fp = 0.0, entry_ref
        filled_qty_i = max(0, int(fq))
        fill_px = float(fp) if fp > 0 else entry_ref
        if filled_qty_i < 1:
            # got_fill was True (else we returned above) but our OWN shares are UNVERIFIABLE (order
            # read failed; or co-held so the net can't be attributed to us). A real position may be
            # OPEN and NAKED — never silently abandon it (cold-2nd T1). PAGE loudly + leave a
            # 'fill_unverified' record so force_flat_all / the operator re-checks once the fill settles.
            state[key]["state"] = "fill_unverified"
            _save_state(state)
            _page(f"[{symbol}] day-tier fill CONFIRMED but qty UNVERIFIABLE (order read failed"
                  f"{'' if was_flat else '; symbol co-held so the net cannot be attributed'}) — a "
                  f"day-tier position may be OPEN and UNPROTECTED. Manual reconcile needed. coid={entry_coid}")
            return False

        # Durable entry snapshot (price-path point 1): fill price + market price at fill.
        mkt_at_fill = fill_px
        try:
            pos = broker.get_open_position(symbol)
            if pos is not None:
                mkt_at_fill = abs(float(getattr(pos, "current_price", fill_px) or fill_px))
        except Exception:
            pass
        day_tier_logger.log_entry_fill(entry_coid, symbol, order_id=entry_order_id, decision_id=decision_id or coid,
                                       side=direction, requested_limit=limit_px, fill_price=fill_px,
                                       fill_qty=float(filled_qty_i), market_price_at_fill=mkt_at_fill,
                                       equity_at_entry=equity, budget=float(size.get("budget") or 0.0),
                                       notional=round(filled_qty_i * fill_px, 2))
        trade_logger.log_event("entry", symbol=symbol, price=fill_px, size=filled_qty_i,
                               data_source="daytrade", tier="daytrade", direction=direction,
                               trade_id=entry_coid, stop=stop_px)
        state[key].update(state="filled", fill_qty=filled_qty_i, fill_px=fill_px)
        _save_state(state)

        # B2: place the protective stop, VERIFY it is live (trust the accepted submit return),
        # RETRY DAYTRADE_STOP_RETRIES more times — CANCELLING the prior stop before each retry so at
        # most one stop ever rests (cold-2nd/reliability duplicate-stop) — then (only if still not
        # live) scoped-flatten. Each submit is guarded so a transient raise becomes a retry, not a
        # naked ride (cold-2nd Threat 1).
        stop_side = "sell" if direction == "long" else "buy"
        retries = int(_cfg("DAYTRADE_STOP_RETRIES", 2))
        wait = float(_cfg("DAYTRADE_STOP_RETRY_WAIT_S", 1.0))
        for attempt in range(retries + 1):
            if attempt > 0:
                try:
                    broker.cancel_open_orders_for_symbol(symbol, only_tier="daytrade")  # clear a prior stop
                except Exception:
                    pass
            try:
                stop_obj = broker.submit_day_stop_order(symbol, filled_qty_i, stop_side, stop_px, tier="daytrade")
            except Exception as e:  # noqa: BLE001 — a transient submit raise is a RETRY, not a naked ride
                logger.warning("[%s] day-tier stop submit raised (attempt %d): %s", symbol, attempt + 1, e)
                stop_obj = None
            if _stop_is_live(stop_obj):
                # Mark protected BEFORE logging so a log call can never leave protected=False and
                # trigger a false-flatten of a genuinely-live stop (cold-2nd T5).
                state[key]["state"] = "protected"
                _save_state(state)
                protected = True
                day_tier_logger.log_stop_placed(entry_coid, symbol, stop_order_id=str(getattr(stop_obj, "id", "")),
                                                stop_price=stop_px)
                logger.info("[%s] day-tier PROTECTED: %d sh @ fill %.2f, stop %.2f (attempt %d)",
                            symbol, filled_qty_i, fill_px, stop_px, attempt + 1)
                break
            if attempt < retries:
                logger.warning("[%s] day-tier stop not confirmed (attempt %d/%d) — retrying in %.1fs",
                               symbol, attempt + 1, retries + 1, wait)
                time.sleep(wait)

        if not protected:
            _page(f"[{symbol}] day-tier stop UNCONFIRMED after {retries + 1} attempts — flattening the "
                  f"{filled_qty_i}-sh day-tier position to avoid a naked ride.")
            flat_ok = flatten_position(symbol, filled_qty_i, direction, entry_price=fill_px,
                                       trade_id=entry_coid, order_id_hint=entry_order_id,
                                       reason="stop_unconfirmed_flatten")
            state[key]["state"] = "flattened_no_stop" if flat_ok else "flatten_failed"
            _save_state(state)
            return False
        return True

    except Exception as e:  # noqa: BLE001 — NEVER raise into the runner; a post-fill raise must not leave a naked ride
        logger.error("[%s] day-tier place_entry raised: %s", symbol, e)
        if filled_qty_i >= 1 and not protected:
            _page(f"[{symbol}] day-tier place_entry RAISED after a {filled_qty_i}-sh fill and before "
                  f"protection — flattening to avoid a naked ride. Error: {e!r}")
            try:
                _fok = flatten_position(symbol, filled_qty_i, direction, entry_price=fill_px,
                                        trade_id=entry_coid or f"DT-{symbol}", order_id_hint=entry_order_id,
                                        reason="post_fill_exception_flatten")
                st = _load_state()
                if key in st:
                    st[key]["state"] = "flattened_no_stop" if _fok else "flatten_failed"
                    _save_state(st)
            except Exception as fe:  # noqa: BLE001
                _page(f"[{symbol}] day-tier post-exception flatten ALSO failed: {fe!r} — {filled_qty_i} sh may be NAKED.")
        else:
            # Pre-fill raise: don't leave the key stuck at 'submitting' and block a legit re-entry.
            try:
                st = _load_state()
                if key in st and st[key].get("state") in ("submitting",):
                    st[key]["state"] = "submit_failed"
                    _save_state(st)
            except Exception:
                pass
        return False


# ── tier-kill (25% of tier budget) ───────────────────────────────────────────────────────────
def tier_kill_check(equity: float) -> bool:
    """If the day-tier's OPEN unrealized loss breaches DAYTRADE_TIER_KILL_PCT of the tier budget,
    force-flat the whole tier and mark it killed for the day. Reads the LIVE book. An unreadable
    quote FAILS CLOSED (masked-loss Finding B): it falls back to Alpaca's own unrealized_pl for the
    lot (prorated by the day-tier's share) and PAGES if even that is unavailable — never silently
    dropping a lot from the loss sum. No-op when disabled."""
    if not _enabled():
        return False
    from execution import broker
    try:
        alloc = float(_cfg("DAYTRADE_ALLOC_PCT", 0.15))
        kill_pct = float(_cfg("DAYTRADE_TIER_KILL_PCT", 0.25))
        tier_budget = equity * alloc
        if tier_budget <= 0:
            return False
        targets = _flatten_targets()
        if not targets:
            return False
        positions = broker.get_open_positions()
        pos_by_sym = {getattr(p, "symbol", None): p for p in (positions or [])}
        upl = 0.0
        blind = False
        for sym, tgt in targets.items():
            pos = pos_by_sym.get(sym)
            if pos is None:
                continue
            q = abs(float(tgt.get("qty") or 0.0))
            ent = abs(float(tgt.get("entry_price") or 0.0))
            try:
                cur = abs(float(getattr(pos, "current_price", 0.0) or 0.0))
            except Exception:
                cur = 0.0
            side = tgt.get("side", "long")
            if cur > 0 and ent > 0 and q > 0:
                upl += (cur - ent) * q if side == "long" else (ent - cur) * q
                continue
            # Quote unreadable → fall back to Alpaca's own unrealized_pl, prorated to our share.
            try:
                pos_upl = float(getattr(pos, "unrealized_pl", 0.0) or 0.0)
                pos_qty = abs(float(getattr(pos, "qty", 0.0) or 0.0))
                if pos_qty > 0 and q > 0:
                    upl += pos_upl * min(1.0, q / pos_qty)
                    continue
            except Exception:
                pass
            blind = True  # could not evaluate this lot's P&L at all
        if blind:
            _page(f"DAY-TIER tier-kill check is BLIND on ≥1 lot (no quote, no unrealized_pl) — "
                  f"cannot fully evaluate the −{kill_pct:.0%} kill this tick. Manual check advised.")
        if upl <= -abs(kill_pct * tier_budget):
            _page(f"DAY-TIER KILL: open unrealized {upl:.2f} ≤ −{kill_pct:.0%} of tier budget "
                  f"${tier_budget:.2f} — force-flattening the tier for the day.")
            force_flat_all(reason="tier_kill")
            state = _load_state()
            state[_KILL_KEY] = f"{_now_et():%Y%m%d}"
            _save_state(state)
            return True
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("day-tier tier_kill_check error: %s", e)
        return False


# ── per-tick reconcile (the board-named go-live gate for the cron runner) ────────────────────────
def _has_live_daytrade_stop(symbol: str) -> "bool | None":
    """True if a live DT-tagged STOP order rests on `symbol`; False if none rests; None if the order
    book is UNREADABLE (the caller treats None as 'cannot confirm' — fail-safe: never flatten a
    possibly-protected position on a transient read failure). A resting DT ENTRY (limit) order is not
    a stop and does NOT count."""
    from execution import broker
    from execution.ownership_guard import tier_of_coid
    try:
        orders = broker.get_open_orders(symbol)
    except Exception:
        return None
    if orders is None:
        return None
    for o in orders:
        try:
            if tier_of_coid(getattr(o, "client_order_id", None)) != "daytrade":
                continue
            otype = str(getattr(o, "order_type", None) or getattr(o, "type", "") or "").lower()
            if "stop" in otype:
                return True
        except Exception:
            continue
    return False


def _order_filled_qty(order_id: str) -> "tuple[bool, float]":
    """(readable, filled_qty) for an order. readable=False when the order cannot be read (empty id,
    None, or an exception) — the caller must NOT treat an unreadable order as zero-fill."""
    from execution import broker
    if not order_id:
        return False, 0.0
    try:
        o = broker.get_order(order_id)
    except Exception:
        return False, 0.0
    if o is None:
        return False, 0.0
    try:
        return True, float(getattr(o, "filled_qty", 0) or 0)
    except Exception:
        return False, 0.0


def _mark_symbol_flattened(symbol: str) -> None:
    """After a reconcile flatten of `symbol`, transition its non-terminal entry:: state records to
    'flattened_no_stop' so a submitted/filled/protected/fill_unverified record does not permanently
    bench the symbol from re-entry (masked-loss re-review note). Never raises."""
    try:
        st = _load_state()
        changed = False
        for k, v in st.items():
            if (k.startswith("entry::") and isinstance(v, dict) and v.get("symbol") == symbol
                    and v.get("state") in ("submitted", "filled", "protected", "fill_unverified")):
                v["state"] = "flattened_no_stop"
                changed = True
        if changed:
            _save_state(st)
    except Exception as e:  # noqa: BLE001
        logger.debug("_mark_symbol_flattened(%s) failed: %s", symbol, e)


def reconcile_open_state() -> dict:
    """Per-tick reconcile — called at the TOP of each runner tick, BEFORE entries. The runner is a
    2-3 min cron, so every tick is a fresh process; this is the board-named go-live gate that ensures
    no day-tier position is ever left NAKED or double-managed across that process boundary (the
    submit→log / fill→stop crash windows). No-op when DAYTRADE_ENABLED is False; never raises.

    For each day-tier-OWNED symbol (durable-log open set ∪ the state file's non-terminal records,
    incl. the 'submitted' crash window) that has a LIVE broker position:
      • a live DT stop rests           → count 'protected', leave it (already safe);
      • NO live DT stop rests (NAKED)   → scoped-flatten own qty + page (fill-without-stop, a
                                          'submitted' crash-window fill, or a vanished stop);
      • order book UNREADABLE           → do NOT flatten (fail-safe) + page.
    A 'submitted' record with NO position (a crashed entry that never filled) → cancel any resting DT
    order for the symbol (so it can't fill mid-next-bar) + mark the record terminal."""
    if not _enabled():
        return {"enabled": False}
    from execution import broker
    summary = {"checked": 0, "flattened": 0, "protected": 0, "unreadable": 0, "cleared": 0}
    try:
        targets = _flatten_targets()  # log ∪ state(filled/protected/fill_unverified)
        state = _load_state()
        submitted = {}
        for k, v in state.items():
            if k.startswith("entry::") and isinstance(v, dict) and v.get("state") == "submitted":
                sym = str(v.get("symbol") or "")
                if sym:
                    submitted[sym] = k
                    targets.setdefault(sym, {
                        "symbol": sym, "side": str(v.get("side") or "long"),
                        "qty": abs(int(float(v.get("qty") or 0))),
                        "entry_price": float(v.get("fill_px") or v.get("stop_px") or 0.0),
                        "trade_id": str(v.get("coid") or ""), "order_id": str(v.get("order_id") or ""),
                    })
        for sym, tgt in targets.items():
            summary["checked"] += 1
            # OWNED-QTY RESOLUTION (cold-2nd Threat 1 + masked-loss #4/residual): a target sourced ONLY
            # from a 'submitted' record carries the INTENDED size, NOT an owned qty. It must never drive
            # a flatten or a retire until the ORDER's ACTUAL fill is confirmed — else a never-filled
            # entry on a co-held symbol would flatten the OTHER tier's shares (a B1 breach). Log/filled/
            # protected targets carry a CONFIRMED owned qty and are trusted as-is.
            if sym in submitted:
                oid = tgt.get("order_id", "")
                readable, filled = _order_filled_qty(oid)
                if not readable:
                    summary["unreadable"] += 1
                    _page(f"[{sym}] day-tier reconcile: 'submitted' order UNREADABLE — cannot confirm "
                          f"fill/ownership; NO action this tick. coid={oid}")
                    continue
                if filled < 1:
                    # never filled → the day-tier owns 0 of this symbol; cancel our resting entry and
                    # retire. Any live position on the symbol belongs to ANOTHER tier — never touched.
                    try:
                        broker.cancel_open_orders_for_symbol(sym, only_tier="daytrade")
                    except Exception:
                        pass
                    # cancel-race: a fill can land between the read above and the async cancel — re-read;
                    # if it now shows filled/unreadable, do NOT retire (next tick's position read handles it).
                    r2, f2 = _order_filled_qty(oid)
                    if f2 >= 1 or not r2:
                        _page(f"[{sym}] day-tier reconcile: 'submitted' order filled/unreadable AFTER "
                              f"cancel (race) — NOT retiring; next tick reconciles. coid={oid}")
                        continue
                    st = _load_state()
                    if submitted[sym] in st:
                        st[submitted[sym]]["state"] = "unfilled_cancelled"
                        _save_state(st)
                    summary["cleared"] += 1
                    continue
                want = int(filled)                     # CONFIRMED owned qty from the order's fill
            else:
                want = int(tgt.get("qty") or 0)        # confirmed-owned (log / filled / protected)

            # Position + protection check. get_open_position returns None ONLY on a confirmed 404; it
            # RAISES on any other error (429/500/network) → NEVER treat an unreadable read as 'flat'.
            try:
                pos = broker.get_open_position(sym)
                pos_readable = True
            except Exception:
                pos, pos_readable = None, False
            if not pos_readable:
                summary["unreadable"] += 1
                _page(f"[{sym}] day-tier reconcile: position read failed (transient) — NO action this "
                      f"tick. Manual check if persistent.")
                continue
            if pos is None:
                # Confirmed absent. A 'submitted' target we just confirmed filled>=1 but with no live
                # position = endpoint lag → leave it (next tick reconciles); do NOT retire. A confirmed-
                # owned (log/filled/protected) target that is gone simply closed — nothing to do.
                if sym in submitted:
                    _page(f"[{sym}] day-tier reconcile: 'submitted' order filled {want} but position "
                          f"absent (endpoint lag) — NOT retiring; next tick reconciles.")
                continue
            stop_state = _has_live_daytrade_stop(sym)
            if stop_state is True:
                summary["protected"] += 1
                continue
            if stop_state is None:
                summary["unreadable"] += 1
                _page(f"[{sym}] day-tier reconcile: order book unreadable — cannot confirm a "
                      f"protective stop; NOT flattening (fail-safe). Manual check.")
                continue
            # NAKED (no live DT stop) → scoped-flatten the day-tier's OWN CONFIRMED qty. flatten_position's
            # net-side/qty guard additionally protects any co-held tier.
            held = abs(int(float(getattr(pos, "qty", 0) or 0)))
            qty = min(held, want) if want > 0 else 0
            if qty < 1:
                _page(f"[{sym}] day-tier reconcile: NAKED position but own confirmed-qty 0 — NOT closing "
                      f"the cross-tier net; manual check.")
                continue
            if flatten_position(sym, qty, tgt["side"], entry_price=tgt["entry_price"],
                                trade_id=tgt["trade_id"], order_id_hint=tgt["order_id"],
                                reason="reconcile_naked_flatten"):
                summary["flattened"] += 1
                _mark_symbol_flattened(sym)  # retire the state record(s) so re-entry is not benched
        logger.info("day-tier reconcile: %s", summary)
        return summary
    except Exception as e:  # noqa: BLE001 — reconcile must never crash the runner tick
        logger.error("day-tier reconcile_open_state raised: %s", e)
        return {"error": repr(e)}
