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
  B6 — Wire-time quantity is clamped by the all-tier account gross cap, day-tier gross cap,
       current buying power after the main-book reserve, actual equity-minus-maintenance cushion,
       pending entries, and stop-distance risk. Every source is live and failures close the gate.

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
import math
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from tier_names import tier_label

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# Idempotency / open-position state file (single-owner = the 2-3 min runner; see CONCURRENCY note).
# Anchored to __file__ (RC-2), atomic tmp+replace+fsync (RC-5).
_STATE = Path(__file__).resolve().parent.parent / "logs" / "day_tier_state.json"

_KILL_KEY = "_tier_killed_date"  # a "killed for the day" flag; survives a restart via the state file
_HALT_KEY = "_tier_halted_date"   # data-blind fail-closed halt; protected positions remain managed
# Terminal entry-record states pruned after _STATE_TTL_DAYS (reliability: unbounded-growth leak +
# rising fsync cost on the hot path). Non-terminal states are NEVER pruned (they gate re-entry).
_TERMINAL_STATES = frozenset({"protected", "flattened_no_stop", "flatten_failed",
                              "flattened_invalid_geometry", "submit_failed", "unfilled_cancelled"})
_STATE_TTL_DAYS = 3
# Why the LAST place_entry on a symbol stopped at wire-time sizing (this process only). The runner reads it to route an
# unaffordable stock to its ETF (CEO 2026-10-07: ETFs only when the stock is unaffordable). Cleared at each call.
_LAST_ENTRY_SKIP: dict = {}


def last_entry_skip(symbol: str) -> "dict | None":
    """{"reason": "below_min_qty" | "risk_budget", "wired": n, "min": m} when the last place_entry on `symbol` stopped
    because the sized share count was below its minimum; None otherwise (any other skip, or an entry)."""
    v = _LAST_ENTRY_SKIP.get(str(symbol))
    return dict(v) if isinstance(v, dict) else None


def _min_entry_qty(symbol: str, min_qty: object) -> int:
    """Smallest share count place_entry will submit: `min_qty` (>= 1), and >= 2 for any leveraged or inverse ETF
    (CEO 2026-10-07: never one share of a leveraged ETF). Never raises; an unreadable map counts the symbol as an ETF
    only when it is in config.LEVERAGED_3X_TICKERS."""
    try:
        m = max(1, int(min_qty))  # type: ignore[call-overload]  # non-int caught below
    except (TypeError, ValueError):
        m = 1
    sym = str(symbol or "").strip().upper()
    is_etf = sym in set(_cfg("LEVERAGED_3X_TICKERS", set()) or set())
    try:
        from strategy import day_tier_leverage as _lev
        is_etf = is_etf or _lev.exposure_sign(sym, "long")[0] != sym
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] leveraged-ETF map unreadable (min qty from config only): %s", sym, e)
    return max(m, 2) if is_etf else m


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
    today = f"{_now_et():%Y%m%d}"
    return state.get(_KILL_KEY) == today or state.get(_HALT_KEY) == today


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


def _enum_text(value) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _account_gross(positions_by_symbol: dict) -> float:
    """Authoritative all-tier gross from Alpaca positions. Any malformed lot fails closed."""
    gross = 0.0
    for pos in positions_by_symbol.values():
        market_value = getattr(pos, "market_value", None)
        if market_value is not None:
            notional = abs(float(market_value))
        else:
            notional = abs(float(getattr(pos, "qty", 0))) * abs(float(getattr(pos, "current_price", 0)))
        if not math.isfinite(notional):
            raise ValueError("non-finite position notional")
        gross += notional
    return gross


def _pending_entry_gross(open_orders: list, positions_by_symbol: dict) -> tuple[float, float]:
    """Return (all-tier, day-tier) pending increasing-order notional; reducing orders do not count."""
    from execution.ownership_guard import tier_of_coid
    total = 0.0
    day = 0.0
    for order in open_orders:
        symbol = str(getattr(order, "symbol", "") or "")
        side = _enum_text(getattr(order, "side", None))
        pos = positions_by_symbol.get(symbol)
        pos_side = _enum_text(getattr(pos, "side", None)) if pos is not None else ""
        qty = abs(float(getattr(order, "qty", 0) or 0))
        filled = abs(float(getattr(order, "filled_qty", 0) or 0))
        if not (math.isfinite(qty) and math.isfinite(filled)):
            raise ValueError(f"non-finite pending qty for order {getattr(order, 'id', '?')}")
        remaining = max(0.0, qty - filled)
        is_reducing = ((pos_side == "long" and side == "sell")
                       or (pos_side == "short" and side == "buy"))
        if is_reducing:
            held = abs(float(getattr(pos, "qty", 0) or 0))
            if not math.isfinite(held):
                raise ValueError(f"non-finite held qty for pending order {getattr(order, 'id', '?')}")
            # Only the portion covered by the current position is reducing. Any excess could reverse
            # the account and is therefore new gross exposure, including an oversized stop.
            remaining = max(0.0, remaining - held)
        if remaining <= 0:
            continue
        price_raw = getattr(order, "limit_price", None) or getattr(order, "stop_price", None)
        if price_raw is None:
            raise ValueError(f"unpriceable pending entry {getattr(order, 'id', '?')}")
        price = abs(float(price_raw))
        notional = remaining * price
        if not (math.isfinite(notional) and price > 0):
            raise ValueError(f"unpriceable pending entry {getattr(order, 'id', '?')}")
        total += notional
        if tier_of_coid(getattr(order, "client_order_id", None)) == "daytrade":
            day += notional
    return total, day


def _bounded_entry_qty(requested_qty: int, order_price: float, stop_price: float, equity: float,
                       open_trades: dict, positions_by_symbol: dict, buying_power: float,
                       maintenance_margin: float, maintenance_rate: float,
                       open_orders: list, risk_equity: float | None = None,
                       symbol: str = "", track: str = "A",
                       track_budget: float | None = None,
                       extra_b_lots: dict | None = None,
                       risk_mult: float = 1.0,
                       max_size: bool = False) -> tuple[int, str]:
    """Clamp an entry to every live account/day-tier/risk budget. All bad inputs fail closed.

    `symbol` selects the DEEP-LIQUIDITY carve-out (aggression guardrail 2026-09-18): a deep-liquidity
    (Mag-7) name may use the full DAYTRADE_TRACK_A_EQUITY_CEILING_PCT; a non-deep (thin) name keeps the
    base ceiling AND a per-name notional cap. An unknown/empty symbol is treated as NON-deep (conservative).

    `track` "B" + DAYTRADE_TRACK_B_CASH_ONLY (default ON) applies the Track-B BUDGET CAP (min()-only):
    (1) at most `requested_qty` — compute_day_tier_size's budget share count — so the Part-B risk-basis
    sizing (which deliberately does NOT cap at requested_qty for Track A) can never up-size a Track-B mover;
    and (2) open Track-B notional + this entry at the ORDER price <= `track_budget` (the whole Track-B
    budget, not per entry). A missing/invalid track_budget fails CLOSED (0). `extra_b_lots` adds day-tier
    lots known ONLY to the state file (a failed log write / the submit→log crash window) to the Track-B sum
    — the Track-B budget only; Track A's caps are unchanged. This bounds Track-B EXPOSURE
    (halt-reopen gap containment, design §7b.6); it does not change how the account funds it — a Track-B
    short, or a buy while the account's cash is negative, is still margin-financed. Any track other than
    "B" is Track A (unchanged)."""
    try:
        risk_basis = min(equity, float(risk_equity)) if risk_equity is not None else equity
        values = (order_price, stop_price, equity, buying_power, maintenance_margin, maintenance_rate, risk_basis)
        if requested_qty < 1 or not all(math.isfinite(float(v)) for v in values):
            return 0, "invalid/non-finite risk input — fail closed"
        if order_price <= 0 or stop_price <= 0 or equity <= 0 or buying_power <= 0:
            return 0, "non-positive risk input — fail closed"
        if maintenance_margin < 0 or not (0 < maintenance_rate <= 1):
            return 0, "maintenance data unavailable — fail closed"

        reserve = float(_cfg("DAYTRADE_MAIN_BOT_BP_RESERVE_USD", 1200.0))
        cushion = float(_cfg("DAYTRADE_MAINT_CUSHION_USD", 650.0))
        day_pct = float(_cfg("DAYTRADE_TRACK_A_EQUITY_CEILING_PCT", 0.60))  # FULL ceiling (deep-liquidity)
        global_ratio = float(_cfg("MAX_GROSS_EXPOSURE_RATIO", 2.5))
        # DEEP-LIQUIDITY CARVE-OUT (aggression guardrail 2026-09-18): the raised full ceiling applies ONLY
        # to deep-liquidity (Mag-7) names; a NON-deep (thin) name keeps the base ceiling and (below) a
        # per-name notional cap — its forced-close market fill is 2-3% off and the entry spread-gate does
        # NOT cover the exit liquidation. Unknown/empty symbol → treated NON-deep (conservative).
        base_ceiling = float(_cfg("DAYTRADE_TRACK_A_BASE_CEILING_PCT", 0.60))
        deep_set = set(_cfg("DAYTRADE_DEEP_LIQUIDITY_SYMBOLS", []) or [])
        is_deep = bool(symbol) and symbol in deep_set
        eff_ceiling = day_pct if is_deep else min(day_pct, base_ceiling)
        # PER-SINGLE-NAME GROSS SUB-CAP (aggression guardrail): bounds a single name's per-entry notional
        # so a single-name intraday gap-through stays inside the account kill at the raised ceiling.
        single_name_pct = float(_cfg("DAYTRADE_MAX_SINGLE_NAME_NOTIONAL_PCT", 0.60))
        # Part B (hairpin fix 2026-09-18): RISK is the sizing BASIS. Active basis = the F1 basis, clamped
        # to the RETAINED 2% hard ceiling (DAYTRADE_PER_TRADE_RISK_EQUITY_PCT) so it can never exceed it.
        risk_pct_basis = float(_cfg("DAYTRADE_PER_TRADE_RISK_BASIS_PCT", 0.01))
        risk_pct_ceiling = float(_cfg("DAYTRADE_PER_TRADE_RISK_EQUITY_PCT", 0.02))
        if not all(math.isfinite(v) and v >= 0 for v in (reserve, cushion, day_pct, base_ceiling, eff_ceiling, single_name_pct, global_ratio, risk_pct_basis, risk_pct_ceiling)):
            return 0, "invalid configured risk limit — fail closed"
        risk_pct = min(risk_pct_basis, risk_pct_ceiling)  # active basis, never above the retained ceiling
        # Per-track risk scale (Track M starts at 0.5x). SHRINK-ONLY: anything outside (0, 1] fails closed, so a
        # caller can never raise per-trade risk above the configured basis through this argument.
        try:
            _rm = float(risk_mult)
        except (TypeError, ValueError):
            return 0, "invalid risk_mult — fail closed"
        if not (math.isfinite(_rm) and 0 < _rm <= 1.0):
            return 0, f"risk_mult {risk_mult!r} outside (0, 1] — fail closed"
        risk_pct *= _rm

        account_gross = _account_gross(positions_by_symbol)
        day_gross = _current_daytrade_gross(open_trades, positions_by_symbol)
        if not (math.isfinite(day_gross) and day_gross >= 0):
            return 0, "day-tier gross unreadable — fail closed"
        pending_all, pending_day = _pending_entry_gross(open_orders, positions_by_symbol)

        rooms = {
            "global_gross": equity * global_ratio - account_gross - pending_all,
            "day_gross": equity * eff_ceiling - day_gross - pending_day,  # deep→full ceiling; thin→base
            "buying_power": buying_power - reserve,
            # Pending entries have no posted maintenance yet. Charge them at a conservative 100%
            # until they resolve so a concurrent main-book order cannot consume the cushion between
            # this snapshot and our submit.
            "maintenance": (equity - maintenance_margin - cushion - pending_all) / maintenance_rate,
        }
        if not is_deep:
            # THIN-NAME PER-NAME NOTIONAL CAP: bound a non-deep name's per-entry notional to its
            # wide-spread liquidation cost (same-symbol re-entry is blocked upstream, so this is a fresh
            # per-entry cap). Deep names are unconstrained here (governed by the full ceiling above).
            rooms["thin_name"] = float(_cfg("DAYTRADE_THIN_NAME_MAX_NOTIONAL_USD", 1000.0))
        # PER-SINGLE-NAME GROSS SUB-CAP (applies to ALL names, deep + thin): no single name's per-entry
        # notional may exceed single_name_pct × equity, so a single-name gap-through stays inside the
        # account kill even at the raised 1.0× aggregate ceiling (the aggregate is reached by ≥2 names).
        rooms["single_name"] = single_name_pct * equity
        stop_distance = abs(order_price - stop_price)
        if stop_distance <= 0 or not math.isfinite(stop_distance):
            return 0, "invalid stop distance — fail closed"
        # Part B: RISK is the sizing BASIS, not a late ceiling. Target the fixed per-trade dollar risk
        # (risk% × SOD-equity ÷ stop_distance), then clamp DOWN by every notional/BP/gross/maintenance
        # cap. The stop-blind conviction-notional (requested_qty) no longer caps the size below the risk
        # target — a tighter (but Part-A-valid) stop earns MORE shares for the SAME dollar risk (design
        # "same risk on every trade"). Doubly bounded: <= the per-trade budget + gross/BP/maintenance caps
        # (notional_qty, which enforces every account limit) AND the 1% active basis (<= the retained 2%
        # ceiling). requested_qty>=1 stays a validity precondition only (checked at the top). risk_qty==0
        # (one share's stop-risk already exceeds the budget) naturally SKIPS — this is exactly the F4
        # min-1-share floor's subordination to the risk cap.
        risk_qty = math.floor((risk_basis * risk_pct) / stop_distance)
        notional_room = min(rooms.values())
        notional_qty = math.floor(max(0.0, notional_room) / order_price)
        safe_qty = max(0, min(int(risk_qty), int(notional_qty)))
        if max_size is True:
            # 10/10 MAXIMUM SIZE (Rafael CEO directive 2026-10-06): size to the account/exposure caps (every room
            # above: global gross, day gross, buying power after the main-bot reserve, maintenance cushion, thin-
            # name and single-name caps) instead of the per-trade risk basis; the Track-B budget cap below is
            # skipped. Still bounded afterwards by the daily day-tier dollar budget in place_entry, the protective
            # stop, the EOD force-flat and the 7% account kill.
            safe_qty = max(0, int(notional_qty))
            why = (f"MAX-SIZE (10/10): requested {requested_qty} → notional cap {notional_qty}sh "
                   f"(risk-basis {risk_qty}sh not applied); rooms="
                   + ",".join(f"{k}:${v:.2f}" for k, v in rooms.items()))
            return safe_qty, why
        # TRACK-B EXPOSURE CAP (Track B Inc 2 Part 2, 2026-09-23; flag DAYTRADE_TRACK_B_CASH_ONLY). Track B's
        # notional is bounded by its small equity-slice budget (§7b.6 — a halted mover can reopen far through
        # its stop), never a risk-sized position. It bounds EXPOSURE, not funding: a B short / a buy on a
        # negative-cash account is still margin-financed. requested_qty IS the budget's share count
        # (compute_day_tier_size(track="B"): floor(equity × alloc × B-share × conviction / px), min-1-share
        # floored only when the budget affords one whole share). min() only — it can shrink, never grow, the
        # wired qty.
        cash_note = ""
        # Only an explicit False disables the cap: None/0/""/"False" all keep it ON (fail-safe — risk seat nit).
        if str(track or "A").strip().upper() == "B" and _cfg("DAYTRADE_TRACK_B_CASH_ONLY", True) is not False:
            try:
                b_budget = float(track_budget) if track_budget is not None else float("nan")
            except (TypeError, ValueError):
                b_budget = float("nan")
            if not (math.isfinite(b_budget) and b_budget > 0):
                return 0, "track-B budget unavailable — fail closed"
            # Open Track-B notional = own recorded qty × live price (entry/stop price fallback), over the durable
            # log's open set PLUS any state-only lot (extra_b_lots) — over-counting a stale record is the
            # fail-closed direction (risk seat R2).
            open_b = 0.0
            for t in list(open_trades.values()) + list((extra_b_lots or {}).values()):
                if str(t.get("track") or "A").upper() != "B":
                    continue
                q = abs(float(t.get("fill_qty") or 0.0))
                pos = positions_by_symbol.get(t.get("symbol"))
                px = abs(float(getattr(pos, "current_price", 0.0) or 0.0)) if pos is not None else 0.0
                if not (math.isfinite(px) and px > 0):
                    px = abs(float(t.get("entry_price") or 0.0))
                if not (math.isfinite(q) and math.isfinite(px)):
                    return 0, "open track-B notional unreadable — fail closed"
                open_b += q * px
            b_room_qty = math.floor(max(0.0, b_budget - open_b) / order_price)
            # CEO order 2026-10-06 ("the budget is never the reason a trade is skipped"): the Track-B budget may SHRINK
            # an entry but never below ONE share; every account room above (and the daily dollar budget in
            # place_entry) still bounds it.
            b_cap = max(0, min(int(requested_qty), max(1, int(b_room_qty))))
            cash_note = (f"; track-B budget cap: requested {int(requested_qty)}sh, open-B ${open_b:.2f} of "
                         f"${b_budget:.2f} → room {b_room_qty}sh")
            if b_cap < safe_qty:
                cash_note += f" → wired {safe_qty}→{b_cap}sh"
                safe_qty = b_cap
        why = (f"requested {requested_qty} → risk-basis {risk_qty}sh "
               f"(risk {risk_pct:.2%}×${risk_basis:.0f}/stop ${stop_distance:.4f}) → wired {safe_qty}; rooms="
               + ",".join(f"{k}:${v:.2f}" for k, v in rooms.items())
               + f"; notional cap={notional_qty}sh" + cash_note)
        return safe_qty, why
    except Exception as e:
        return 0, f"entry-cap error (fail-closed): {e!r}"


def _short_maintenance_rate(rate: "float | None", price: float) -> "float | None":
    """Alpaca's SHORT maintenance requirement (docs.alpaca.markets margin-and-short-selling, verified 2026-10-07):
    price < $5 -> greater of $2.50/share or 100%; price >= $5 -> greater of $5.00/share or 30%. The asset's posted
    rate is the LONG rate, so a short uses the larger of the two. None when the inputs are unusable. Never raises.
    As a RATE the rule is max(dollar-floor / price, percent-floor). Worked: $100 short -> $5/sh = 5% vs 30% -> 30%
    ($30/sh — the greater of $5/sh and 30%); $10 short -> $5/sh = 50% vs 30% -> 50%; $4 short -> $2.50/sh = 62.5%
    vs 100% -> 100%; $2 short -> 125% (> 100%, the caller skips). p <= 0 returns None BEFORE any division."""
    try:
        r, p = float(rate), float(price)  # type: ignore[arg-type]
        if not (math.isfinite(r) and math.isfinite(p) and r > 0 and p > 0):
            return None
        floor = max(1.0, 2.5 / p) if p < 5.0 else max(0.30, 5.0 / p)
        return max(r, floor)
    except (TypeError, ValueError):
        return None


def _account_entry_halt_reason(account) -> str | None:
    """Return a reason when Alpaca or the durable main-book kill state forbids new entries."""
    try:
        if bool(getattr(account, "trading_blocked", False) or getattr(account, "account_blocked", False)):
            return "Alpaca account is trading-blocked"
        from execution.risk_manager import _load_kill_state
        state = _load_kill_state()
        today = f"{_now_et():%Y-%m-%d}"
        if state.get("date") == today and (state.get("killed") or state.get("halt_entries")):
            return "main account kill/halt is active"
        return None
    except Exception as e:
        return f"account halt state unreadable (fail-closed): {e!r}"


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
        if mode == "TRACK_M":
            # Track M (QQQ Monday weekend dip): the trigger carries an explicit stop level (1% below the entry
            # reference, strategy.day_tier_track_m). Missing -> no sane stop -> the caller aborts.
            stop_ref = trigger.get("stop_ref")
            if stop_ref is None:
                return None
            stop = float(stop_ref)
        elif mode == "FADE" and target is not None:
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


def _post_fill_exit_geometry(direction: str, fill_px: float, stop_px: float,
                             target_px: "float | None") -> tuple[bool, str]:
    """Validate exit geometry against the actual fill instead of the older signal reference.

    A marketable limit can fill through the structural stop or target. In that case the original
    setup no longer exists and an OCO leg would be inverted or immediately marketable. The caller
    must flatten the new lot rather than retain an invalid trade behind a stop-only fallback.
    """
    try:
        fill = float(fill_px)
        stop = float(stop_px)
        target = float(target_px) if target_px is not None else None
        values = (fill, stop) if target is None else (fill, stop, target)
        if not all(math.isfinite(v) and v > 0 for v in values):
            return False, "post-fill geometry contains a non-finite/non-positive price"
        if direction == "long":
            if not stop < fill:
                return False, f"long stop {stop:.4f} is not below fill {fill:.4f}"
            if target is not None and not fill < target:
                return False, f"long target {target:.4f} is not above fill {fill:.4f}"
        elif direction == "short":
            if not fill < stop:
                return False, f"short stop {stop:.4f} is not above fill {fill:.4f}"
            if target is not None and not target < fill:
                return False, f"short target {target:.4f} is not below fill {fill:.4f}"
        else:
            return False, f"unknown direction {direction!r}"
        return True, "post-fill exit geometry valid"
    except (TypeError, ValueError) as e:
        return False, f"post-fill geometry unreadable: {e!r}"


# ── min-stop-distance gate (hairpin fix Part A — 2026-09-18, board + Gro + GAI + masked-loss) ─────
def _robust_atr_5m(symbol: str) -> "float | None":
    """Robust 5-min ATR in DOLLARS = the MEDIAN true range over the last DAYTRADE_ATR_PERIOD bars
    (MEDIAN, not mean, so a single spiky 5-min bar cannot inflate the floor and let a hairpin through).
    Sources the SAME fetch_bars(symbol, TF_5M, 30) the entry trigger already fetched THIS tick — 30
    matches strategy.day_tier_entry_trigger._INTRADAY_BARS, so this is a shared-TTL-cache HIT (no extra
    API call / rate-budget cost). Returns None if fewer than DAYTRADE_ATR_MIN_BARS usable bars or on any
    error → the caller fails CLOSED (skips the entry). Never raises."""
    try:
        from data.fetcher import fetch_bars
        period = int(_cfg("DAYTRADE_ATR_PERIOD", 14))
        min_bars = int(_cfg("DAYTRADE_ATR_MIN_BARS", 15))
        # 30 == day_tier_entry_trigger._INTRADAY_BARS (cache-key parity → hit). A divergence only wastes
        # one cached-miss fetch; correctness is unaffected.
        df = fetch_bars(symbol, config.TF_5M, num_bars=30)
        if df is None or getattr(df, "empty", True):
            return None
        for col in ("high", "low", "close"):
            if col not in getattr(df, "columns", []):
                return None
        if len(df) < min_bars:
            return None
        highs = [float(x) for x in df["high"].tolist()]
        lows = [float(x) for x in df["low"].tolist()]
        closes = [float(x) for x in df["close"].tolist()]
        if not (len(highs) == len(lows) == len(closes)) or len(closes) < min_bars:
            return None
        trs = []
        for i in range(1, len(closes)):
            h, lo, pc = highs[i], lows[i], closes[i - 1]
            if not (math.isfinite(h) and math.isfinite(lo) and math.isfinite(pc)):
                continue
            tr = max(h - lo, abs(h - pc), abs(lo - pc))
            if math.isfinite(tr) and tr >= 0:
                trs.append(tr)
        window = trs[-period:]
        # Require a stable-enough sample: at least (min_bars - 1) true ranges (min_bars bars → min_bars-1 TRs).
        if len(window) < (min_bars - 1):
            return None
        window.sort()
        m = len(window)
        atr = window[m // 2] if m % 2 == 1 else (window[m // 2 - 1] + window[m // 2]) / 2.0
        # Return a computed 0.0 as a VALID reading (a genuinely flat 5-min tape), not None: the design's
        # spread backstop max(k×ATR, spread_mult×spread) is meant to supply the floor when ATR≈0. None is
        # reserved for UNAVAILABLE data (handled by the early returns above); a real 0-range measurement
        # is a number, and the gate's own min_stop>0 check fails closed if the spread is also ~0.
        return atr if (math.isfinite(atr) and atr >= 0) else None
    except Exception as e:  # noqa: BLE001 — ATR failure fails CLOSED at the caller (skip), never raises
        logger.warning("[%s] day-tier ATR(5m) compute failed: %s", symbol, e)
        return None


def _min_stop_room_ok(symbol: str, direction: str, entry_px: float, stop_px: float) -> "tuple[bool, str]":
    """Part A gate: True iff the structural stop sits at least a volatility-scaled distance from the
    entry = max(k×ATR(5m), spread_mult×live_spread). Fails CLOSED (returns False) on an invalid ATR
    (<DAYTRADE_ATR_MIN_BARS bars) OR a broken/crossed/stale/unreadable quote — a no-room trade must
    never enter, and an unverifiable room budget is treated as no room. NEVER widens the stop (the
    caller SKIPS). Applies identically to FADE and RIDE. Never raises."""
    try:
        stop_distance = abs(float(entry_px) - float(stop_px))
        if not (math.isfinite(stop_distance) and stop_distance > 0):
            return False, "min-stop gate: invalid stop distance — skip (fail-closed)"
        k = float(_cfg("DAYTRADE_MIN_STOP_ATR_MULT", 1.5))
        spread_mult = float(_cfg("DAYTRADE_MIN_STOP_SPREAD_MULT", 2.0))
        sanity_pct = float(_cfg("DAYTRADE_STOP_SPREAD_SANITY_PCT", 0.02))
        atr = _robust_atr_5m(symbol)
        if atr is None or not (math.isfinite(atr) and atr >= 0):
            return False, "min-stop gate: ATR(5m) unavailable (<min bars/invalid) — skip (fail-closed)"
        atr_floor = k * atr
        # Live spread backstop (thin/quiet tape where ATR≈0). A broken/crossed/stale quote (non-positive,
        # crossed, or spread > sanity_pct of mid) is NOT a usable room reference → fail CLOSED.
        from data.alpaca_data import get_latest_quote
        q = get_latest_quote(symbol)
        if not isinstance(q, dict):
            return False, "min-stop gate: quote unreadable (402/None) — skip (fail-closed)"
        try:
            bid = float(q.get("bid") or 0.0)
            ask = float(q.get("ask") or 0.0)
        except (TypeError, ValueError):
            return False, "min-stop gate: non-numeric quote — skip (fail-closed)"
        if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
            return False, f"min-stop gate: invalid/crossed quote bid={bid} ask={ask} — skip (fail-closed)"
        mid = (bid + ask) / 2.0
        spread = ask - bid
        if mid <= 0 or spread > sanity_pct * mid:
            return False, (f"min-stop gate: spread ${spread:.4f} > {sanity_pct:.1%} of mid ${mid:.2f} "
                           f"(broken/crossed/stale quote) — skip (fail-closed)")
        min_stop = max(atr_floor, spread_mult * spread)
        # If NEITHER ATR nor the spread yields a positive floor (a flat tape AND a locked/zero-spread
        # quote), there is no measurable room reference — fail CLOSED rather than let any tiny stop pass.
        # This also makes a mis-set spread_mult=0 safe when ATR happens to be 0 (Gro NIT-D).
        if not (min_stop > 0):
            return False, "min-stop gate: no measurable room floor (ATR≈0 and spread≈0) — skip (fail-closed)"
        # LIVE-PRICE ROOM (2026-09-29): the caller passes the marketable-LIMIT price, which sits
        # slippage-% on the favorable side of the stale bar reference, so the entry→stop distance above
        # is inflated by ~the slippage allowance. The fill happens at the live touch we trade into
        # (short sells the bid, long buys the ask). NVDA 2026-09-29: stop 230.29, limit 229.50, bid
        # ~230.28 → the old check saw $0.79 of room; the live room was ~$0.01 and the fill (230.30)
        # crossed the stop. Measure room from the live touch too and use the SMALLER distance; a stop
        # the live touch has already reached is no trade at all.
        if direction == "short":
            live_ref, live_dist = bid, float(stop_px) - bid
        elif direction == "long":
            live_ref, live_dist = ask, ask - float(stop_px)
        else:
            return False, f"min-stop gate: unknown direction {direction!r} — skip (fail-closed)"
        if not (math.isfinite(live_dist) and live_dist > 0):
            return False, (f"min-stop gate: stop ${float(stop_px):.4f} already reached by the live "
                           f"{'bid' if direction == 'short' else 'ask'} ${live_ref:.4f} — NO ROOM, skip")
        stop_distance = min(stop_distance, live_dist)
        if stop_distance + 1e-9 < min_stop:
            return False, (f"min-stop gate: stop_distance ${stop_distance:.4f} < required ${min_stop:.4f} "
                           f"(max of {k}×ATR ${atr_floor:.4f}, {spread_mult}×spread ${spread_mult * spread:.4f}) "
                           f"— NO ROOM, skip (never widen the pin)")
        return True, (f"min-stop gate OK: stop_distance ${stop_distance:.4f} >= ${min_stop:.4f} "
                      f"({k}×ATR ${atr_floor:.4f} | {spread_mult}×spread ${spread_mult * spread:.4f})")
    except Exception as e:  # noqa: BLE001 — a gate error fails CLOSED (skip); never an unchecked entry
        # WARN (not INFO): a normal no-room skip is INFO at the caller; an EXCEPTION here is a gate
        # malfunction (e.g. a data-API outage skipping every entry) and must be visible above INFO.
        logger.warning("[%s] day-tier min-stop gate error (fail-closed skip): %s", symbol, e)
        return False, f"min-stop gate error (fail-closed skip): {e!r}"


def _realtime_vol_room(symbol: str, k: float) -> "float | None":
    """Volatility room from REAL-TIME completed IEX 5-min bars (losers audit 2026-10-09). _robust_atr_5m reads the
    default feed — consolidated SIP, ~15 min delayed on this plan — so at the open its 14 bars are mostly the PRIOR
    AFTERNOON: AMZN 10/09 09:40 ET got a $0.30 room (1.5 x ~$0.20) while its first three bars ranged $2.79 / $1.25 /
    $1.35, and the stop hit 3 minutes before the target. Returns max(k x median true range of the last 14 completed
    real-time bars, mean true range of TODAY's completed regular-session bars); None when no fresh real-time bars
    (the caller keeps the existing room). Replay of all 26 logged Day trades (research/day_tier_open_room_replay.py):
    -$20.07 -> -$10.98 at the same dollar risk. Never raises."""
    try:
        from strategy.day_tier_entry_trigger import fetch_bars_ref   # real-time IEX, completed bars, freshness-guarded
        df = fetch_bars_ref(symbol, config.TF_5M, 30)
        if df is None or getattr(df, "empty", True) or len(df) < 15:
            return None
        highs = [float(x) for x in df["high"].tolist()]
        lows = [float(x) for x in df["low"].tolist()]
        closes = [float(x) for x in df["close"].tolist()]
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
               for i in range(1, len(closes))]
        window = sorted(t for t in trs[-14:] if math.isfinite(t) and t >= 0)
        if len(window) < 14:
            return None
        med = (window[7] + window[6]) / 2.0
        today = datetime.now(ET).date()
        idx = [i.astimezone(ET) for i in df.index]
        t_tr: list = []
        prev: "float | None" = None
        for j, ts in enumerate(idx):
            if ts.date() != today or (ts.hour, ts.minute) < (9, 30) or ts.hour >= 16:
                continue
            h, lo = highs[j], lows[j]
            t_tr.append(h - lo if prev is None else max(h - lo, abs(h - prev), abs(lo - prev)))
            prev = closes[j]
        today_mean = (sum(t_tr) / len(t_tr)) if t_tr else 0.0
        room = max(k * med, today_mean)
        return room if (math.isfinite(room) and room > 0) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] day-tier real-time volatility room failed (existing room kept): %s", symbol, e)
        return None


def _room_stop(symbol: str, direction: str, limit_px: float, stop_px: float) -> "tuple[float | None, str]":
    """CEO order 2026-10-06 ("the day tier must trade"; replay of 10/05-06: 9 of 25 Track-A setups died in the
    min-stop gate). Returns a protective stop with at least the volatility room k x ATR(5m), measured from BOTH the
    marketable limit and the LAST TRADE price — WIDENING a too-tight structural stop instead of skipping the trade
    (wire-time sizing then sizes to the wider stop). An unavailable ATR falls back to DAYTRADE_ROOM_FALLBACK_PCT of
    price. Returns (None, why) only when no usable price exists. Never raises.

    LAST PRICE, NOT BID/ASK (CEO 2026-10-09): the IEX bid/ask is not the national quote and swings wildly — across 29
    room stops on 10/07-08 the old 2 x spread term jumped MU $3.50 -> $37.86 two minutes apart, TSLA $0 -> $11.44,
    SNDK $3.60 -> $37.16, and on 10/08 09:38 ET a $6.47 META spread widened the stop 719.44 -> 709.67 (volatility
    room $4.43). Stops trigger on trades, so the room is measured from the last trade and the stock's own volatility."""
    try:
        e = float(limit_px)
        s = float(stop_px)
        if not (math.isfinite(e) and math.isfinite(s) and e > 0 and s > 0) or direction not in ("long", "short"):
            return None, "room stop: invalid entry/stop/direction"
        k = float(_cfg("DAYTRADE_MIN_STOP_ATR_MULT", 1.5))
        fallback_pct = float(_cfg("DAYTRADE_ROOM_FALLBACK_PCT", 0.005))  # PROV:daytier-must-trade-2026-10-06
        band = float(_cfg("DAYTRADE_LIVE_PRICE_SANITY_PCT", 0.05))  # PROV:daytier-must-trade-2026-10-06
        live_ref, src = None, ""
        from data.alpaca_data import get_latest_trade
        try:
            lt = get_latest_trade(symbol)
            if (lt is not None and math.isfinite(float(lt)) and float(lt) > 0
                    and abs(float(lt) / e - 1.0) <= band):   # a print > band off the limit = a bad print
                live_ref, src = float(lt), "last trade"
        except Exception:  # noqa: BLE001
            live_ref = None
        if live_ref is None:
            live_ref, src = e, "limit price (no usable last trade)"
        # A last trade already AT/THROUGH the structural stop means the setup is invalidated (cold-2nd 2026-10-06) —
        # that is not a "too tight" stop to widen; skip, exactly as a post-fill cross would flatten.
        if src == "last trade" and (
                (direction == "long" and live_ref <= s) or (direction == "short" and live_ref >= s)):
            return None, (f"room stop: stop ${s:.2f} already reached by the {src} ${live_ref:.2f} — "
                          f"setup invalidated, skip")
        atr = _robust_atr_5m(symbol)
        # Price floor for a tiny-but-positive ATR (board Thorp+Harris 2026-10-09: a near-flat tape must not size up
        # into a sub-tick stop). 0.10% of price binds on none of the 29 logged 10/07-08 rooms (smallest 0.16%).
        min_pct = float(_cfg("DAYTRADE_ROOM_MIN_PCT", 0.001))  # PROV:room-stop-last-price-2026-10-09
        vol_floor = (max(k * atr, min_pct * live_ref) if (atr is not None and math.isfinite(atr) and atr > 0)
                     else fallback_pct * live_ref)
        rt_room = _realtime_vol_room(symbol, k)
        rt_note = ""
        # Cap the real-time contribution (board Harris+Thorp 2026-10-10): one halt-reopen / news bar must not set a
        # 5-8% stop — on a 10/10 max-size entry (sized to notional caps, not stop distance) that multiplies the dollar
        # risk. The cap never narrows the existing room.
        max_pct = float(_cfg("DAYTRADE_ROOM_MAX_PCT", 0.0125))  # PROV:room-open-realtime-2026-10-10
        if rt_room is not None and math.isfinite(max_pct) and max_pct > 0 and rt_room > max_pct * live_ref:
            rt_note = f"; real-time room ${rt_room:.4f} capped at {max_pct:.2%} of price"
            rt_room = max_pct * live_ref
        if rt_room is not None and rt_room > vol_floor:
            rt_note += f"; real-time/open volatility raised the room ${vol_floor:.4f}->${rt_room:.4f}"
            vol_floor = rt_room
        if direction == "long":
            need = round(min(e, live_ref) - vol_floor, 2)
            out = min(s, need)
        else:
            need = round(max(e, live_ref) + vol_floor, 2)
            out = max(s, need)
        out = round(out, 2)
        if not (math.isfinite(out) and out > 0):
            return None, "room stop: no positive protective stop"
        how = "kept" if out == round(s, 2) else f"WIDENED {s:.2f}->{out:.2f}"
        return out, (f"room stop {how}: min room ${vol_floor:.4f} (vol ${vol_floor:.4f}) from limit ${e:.2f} / "
                     f"live ${live_ref:.2f} [{src}]{rt_note}")
    except Exception as ex:  # noqa: BLE001
        logger.warning("[%s] day-tier room-stop error: %s", symbol, ex)
        return None, f"room stop error: {ex!r}"


# ── fill confirmation ──────────────────────────────────────────────────────────────────────────
def _confirm_fill(order_id: str, expected_qty: int = 0) -> bool:
    """Poll broker.get_order until the entry order is FULLY filled (filled_qty >= expected_qty), reaches a
    terminal status, or the poll budget is exhausted. Returns True if ANY share filled, False if nothing filled.
    (2026-10-07: it used to return at the FIRST partial fill and the caller cancelled the rest — 6 of 14 day-tier
    entries filled partly, 47 of 61 wired shares; EWY 7 -> 1.) The caller then cancels any resting remainder,
    waits for the order to be terminal and re-reads its AUTHORITATIVE final filled_qty. Never raises."""
    from execution import broker
    polls = max(1, int(_cfg("DAYTRADE_FILL_POLL_MAX", 8)))
    wait = float(_cfg("DAYTRADE_FILL_POLL_S", 1.0))
    any_fill = False
    for i in range(polls):
        try:
            o = broker.get_order(order_id)
            if o is not None:
                fq = float(getattr(o, "filled_qty", 0) or 0)
                status = _enum_text(getattr(o, "status", None))
                if fq > 0:
                    any_fill = True
                    if expected_qty < 1 or fq + 1e-9 >= expected_qty or status == "filled":
                        return True
                if status in ("canceled", "expired", "rejected", "done_for_day"):
                    return any_fill  # terminal: a zero fill has nothing to protect
        except Exception as e:  # noqa: BLE001
            logger.debug("fill poll error (order %s): %s", order_id, e)
        if i < polls - 1:
            time.sleep(wait)
    return any_fill


def _await_entry_terminal(order_id: str) -> bool:
    """After the resting remainder is cancelled, poll the ENTRY order until its status is terminal, so its
    filled_qty is final before the protective stop is sized (board Harris + Taleb 2026-10-07: a cancel can sit in
    pending_cancel and fill more shares that the stop would not cover). True = terminal. Never raises."""
    from execution import broker
    polls = max(1, int(_cfg("DAYTRADE_FILL_POLL_MAX", 8)))
    wait = float(_cfg("DAYTRADE_FILL_POLL_S", 1.0))
    for i in range(polls):
        try:
            o = broker.get_order(order_id)
            if o is not None and _enum_text(getattr(o, "status", None)) in (
                    "filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"):
                return True
        except Exception as e:  # noqa: BLE001
            logger.debug("entry terminal poll error (order %s): %s", order_id, e)
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


def _confirmed_order_fill(order_id: str, expected_qty: int) -> tuple[bool, float, float]:
    """Poll an exit order for an actual fill. Returns (order_readable, qty, average price)."""
    from execution import broker
    polls = max(1, int(_cfg("DAYTRADE_FILL_POLL_MAX", 8)))
    wait = float(_cfg("DAYTRADE_FILL_POLL_S", 1.0))
    latest = (0.0, 0.0)
    readable = False
    for i in range(polls):
        order = broker.get_order(order_id)
        if order is not None:
            readable = True
            status = _enum_text(getattr(order, "status", None))
            zero_terminal = ("canceled", "expired", "rejected", "done_for_day")
            try:
                qty = float(getattr(order, "filled_qty", 0) or 0)
                price = float(getattr(order, "filled_avg_price", 0) or 0)
                numeric = math.isfinite(qty) and math.isfinite(price) and qty >= 0 and price >= 0
                exact_fill = numeric and qty > 0 and price > 0
                if exact_fill:
                    latest = (qty, price)
                    if qty + 1e-9 >= expected_qty:
                        return True, *latest
                # Every terminal order with a possible positive fill requires exact cumulative qty
                # AND price. Only an explicitly numeric qty=0 proves a zero-fill cancellation.
                if status == "filled" and not exact_fill:
                    return False, 0.0, 0.0
                if status in zero_terminal and not (exact_fill or (numeric and qty == 0)):
                    return False, 0.0, 0.0
                if status in zero_terminal:
                    return True, *latest
            except (TypeError, ValueError):
                if status == "filled" or status in zero_terminal:
                    return False, 0.0, 0.0
        if i < polls - 1:
            time.sleep(wait)
    return readable, *latest


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


def _submit_verified_plain_stop(symbol: str, qty: int, direction: str, stop_px: float):
    """Submit one scoped emergency stop and return ``(status, order)``.

    ``status`` is ``live`` only for a broker-acknowledged order, ``absent`` only for an explicit
    no-order result, and ``unknown`` when acceptance may have happened but cannot be proved.

    Deliberately one attempt: an exception can be a lost acknowledgement after broker acceptance.
    Retrying with a new client-order id could create two full-quantity stops that later reverse the
    account. The caller halts/reconciles an ambiguous result instead.
    """
    from execution import broker
    stop_side = "sell" if direction == "long" else "buy"
    try:
        stop_obj = broker.submit_day_stop_order(
            symbol, qty, stop_side, stop_px, tier="daytrade", allow_cancel_blocking=False)
    except Exception as e:  # noqa: BLE001 — ambiguous accept: never retry with a fresh id
        logger.warning("[%s] emergency stop submit raised (no blind retry): %s", symbol, e)
        return "unknown", None
    if _stop_is_live(stop_obj):
        return "live", stop_obj
    if (stop_obj is None or stop_obj is broker.PROTECTION_ALREADY_HELD
            or stop_obj is broker.PROTECTION_UNKNOWN):
        return "unknown", None
    return "unknown", None


def _live_net_covers_owned(symbol: str, direction: str, qty: int) -> bool:
    """Prove a stop for `qty` reduces the live net; false on any ambiguity."""
    if qty < 1:
        return False
    try:
        from execution import broker
        pos = broker.get_open_position(symbol)
        if pos is None:
            return False
        net_is_long = getattr(pos, "side", None) == "long"
        net_qty = abs(int(float(getattr(pos, "qty", 0) or 0)))
        return net_is_long == (direction == "long") and net_qty >= qty
    except Exception:  # noqa: BLE001 — an exposure-increasing stop is worse than a failed restore
        return False


def _durable_exit_recorded(trade_id: str) -> bool:
    """True only when the day-tier journal contains a complete exit for this exact trade."""
    if not trade_id:
        return False
    try:
        from strategy import day_tier_logger
        events, readable = day_tier_logger.read_events_checked(trade_id)
        return bool(readable
                    and any(e.get("event") == "entry_fill" for e in events)
                    and any(e.get("event") == "exit_fill" for e in events))
    except Exception:  # noqa: BLE001 — unreadable journal can never prove a terminal exit
        return False


def _record_partial_exit(target: dict, order_id: str, fill_qty: int, fill_price: float,
                         market_price: float, reason: str) -> bool:
    """Persist a confirmed partial close and reduce the state-owned quantity before any retry."""
    from strategy import day_tier_logger
    import trade_logger
    if fill_qty < 1 or not (math.isfinite(fill_price) and fill_price > 0):
        return False
    trade_id = str(target.get("trade_id") or "")
    symbol = str(target.get("symbol") or "")
    side = str(target.get("side") or "long")
    entry = abs(float(target.get("entry_price") or 0.0))
    if not trade_id or not symbol or not (math.isfinite(entry) and entry > 0):
        return False
    realized = round((fill_price - entry) * fill_qty if side == "long"
                     else (entry - fill_price) * fill_qty, 2)
    if not day_tier_logger.log_partial_exit_fill(
        trade_id, symbol, order_id=order_id, exit_reason=reason,
        fill_price=fill_price, fill_qty=float(fill_qty), market_price_at_exit=market_price,
        realized_pnl=realized,
    ):
        return False
    state = _load_state()
    for key, value in state.items():
        if key.startswith("entry::") and isinstance(value, dict) and value.get("coid") == trade_id:
            old_qty = abs(int(float(value.get("fill_qty") or value.get("qty") or 0)))
            value["fill_qty"] = max(0, old_qty - fill_qty)
            if str(value.get("pending_exit_order_id") or "") == order_id:
                _prev_q = float(value.get("pending_exit_accounted_qty") or 0.0)
                value["pending_exit_accounted_qty"] = _prev_q + fill_qty
                # Track booked VALUE only while it is complete: a record written before this field existed (qty already
                # booked, no value) stays "unknown" so _increment_price falls back to the average (cold-2nd 2026-10-07).
                if value.get("pending_exit_accounted_value") is not None or _prev_q <= 0:
                    value["pending_exit_accounted_value"] = (float(value.get("pending_exit_accounted_value") or 0.0)
                                                             + fill_qty * fill_price)
    if not _save_state(state):
        return False
    target["qty"] = max(0, int(target.get("qty") or 0) - fill_qty)
    try:
        trade_logger.log_event("partial_exit", symbol=symbol, price=fill_price, size=fill_qty,
                               data_source="daytrade", tier="daytrade", exit_reason=reason,
                               trade_id=trade_id, realized_pnl=realized)
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] partial-exit trade_logger write failed: %s", symbol, e)
    return True


def _bump_pending_ticks(trade_id: str, ticks: int) -> None:
    """Persist how many ticks the after-hours exit has rested unfilled. Best-effort; never raises."""
    try:
        state = _load_state()
        for key, value in state.items():
            if key.startswith("entry::") and isinstance(value, dict) and value.get("coid") == trade_id:
                value["pending_exit_open_ticks"] = int(ticks)
                _save_state(state)
                return
    except Exception as e:  # noqa: BLE001
        logger.debug("pending tick count write failed: %s", e)


def _set_pending_exit(trade_id: str, order_id: str, requested_qty: int, kind: str = "", reprices: int = 0) -> bool:
    """Persist a close order before polling it, so a later fill cannot be retried blind. `kind` "after_hours" marks
    the extended-hours exit limit, which after_hours_exit keeps managing (re-pricing) even during regular hours."""
    if not trade_id or not order_id or requested_qty < 1:
        return False
    state = _load_state()
    for key, value in state.items():
        if key.startswith("entry::") and isinstance(value, dict) and value.get("coid") == trade_id:
            value["pending_exit_order_id"] = order_id
            value["pending_exit_requested_qty"] = requested_qty
            value["pending_exit_accounted_qty"] = 0.0
            value["pending_exit_accounted_value"] = 0.0
            value["pending_exit_kind"] = kind
            value["pending_exit_open_ticks"] = 0
            value["pending_exit_reprices"] = int(reprices)
            return _save_state(state)
    return False


def _clear_pending_exit(trade_id: str) -> None:
    """Best-effort cleanup after a complete durable exit."""
    state = _load_state()
    for key, value in state.items():
        if key.startswith("entry::") and isinstance(value, dict) and value.get("coid") == trade_id:
            value["pending_exit_order_id"] = ""
            value["pending_exit_requested_qty"] = 0
            value["pending_exit_accounted_qty"] = 0.0
            value["pending_exit_accounted_value"] = 0.0
            value["pending_exit_kind"] = ""
            value["pending_exit_open_ticks"] = 0
            _save_state(state)
            return


def _increment_price(cum_qty: float, avg_px: float, booked_qty: float, booked_value: "float | None", delta: int) -> float:
    """Price of the NEW shares of an order's fill: Alpaca reports a running average, so the increment is
    (cum_qty*avg - value already booked) / delta (adversarial review 2026-10-07: booking every increment at the running
    average mis-states a split fill — 4 @ 100 then 6 @ 99 would book the 6 @ 99.40). Falls back to the average when
    nothing was booked yet (exact then) or the result is not a positive number. Never raises."""
    try:
        if booked_qty <= 0 or delta < 1 or booked_value is None:
            return float(avg_px)   # nothing booked yet (exact), or the booked value is unknown (legacy record)
        p = (float(cum_qty) * float(avg_px) - float(booked_value)) / float(delta)
        return round(p, 4) if (math.isfinite(p) and p > 0) else float(avg_px)
    except (TypeError, ValueError, ZeroDivisionError):
        return float(avg_px)


def _resolve_pending_exit(target: dict) -> bool:
    """Account a prior forced-close order, then make this tick ineligible to submit another one.

    False means no recorded pending close exists. True means a pending order was observed or was
    unreadable, so callers must wait a tick after reconciliation rather than risk a duplicate close.
    """
    from execution import broker
    trade_id = str(target.get("trade_id") or "")
    if not trade_id:
        return False
    state = _load_state()
    entry = next((v for k, v in state.items() if k.startswith("entry::") and isinstance(v, dict)
                  and v.get("coid") == trade_id), None)
    if not isinstance(entry, dict):
        return False
    order_id = str(entry.get("pending_exit_order_id") or "")
    if not order_id:
        return False
    requested = int(entry.get("pending_exit_requested_qty") or 0)
    accounted = float(entry.get("pending_exit_accounted_qty") or 0.0)
    readable, cumulative, price = _confirmed_order_fill(order_id, max(1, requested))
    if not readable or not (math.isfinite(cumulative) and math.isfinite(accounted)) or cumulative + 1e-9 < accounted:
        _halt_unresolved_exit(str(target.get("symbol") or ""), "Prior forced-close order is unreadable or inconsistent.")
        return True
    delta = int(math.floor(cumulative - accounted))
    if delta > 0:
        _bv0 = entry.get("pending_exit_accounted_value")
        price = _increment_price(cumulative, price, accounted,
                                 float(_bv0) if _bv0 is not None else None, delta) if price > 0 else price
        if price <= 0 or not _record_partial_exit(target, order_id, delta, price, price, "forced_close_late_fill"):
            _halt_unresolved_exit(str(target.get("symbol") or ""), "Prior forced-close fill could not be persisted.")
            return True
    try:
        order = broker.get_order(order_id)
        terminal = _enum_text(getattr(order, "status", None)) in ("filled", "canceled", "expired", "rejected", "done_for_day")
    except Exception:
        terminal = False
    if terminal:
        _clear_pending_exit(trade_id)
    return True


# ── flatten (scoped, B1) ─────────────────────────────────────────────────────────────────────
def _oco_leg_ids(oco) -> "tuple[str, str, str]":
    """Extract (stop_order_id, take_profit_order_id, oco_parent_id) from a submitted Alpaca OCO order.

    Alpaca OCO shape (VERIFIED — docs.alpaca.markets/us/docs/orders-at-alpaca, OCO section: "the
    take-profit order shows up as the PARENT order while the stop-loss order appears as a CHILD
    order" in `.legs`). So the PARENT id IS the take-profit order id (unless a limit leg is ever
    explicitly returned), and the STOP id is the child leg. A missing leg falls back to the parent
    id so reconcile/flatten still have a usable id (fail-closed → halt+page, never a masked/naked
    exit) rather than an empty string. Never raises.

    Getting this right is load-bearing: it is what lets the reconcile heal poll the REAL take-profit
    fill and book a harvested WINNER as take_profit. Recording an empty tp id (the pre-fix bug) made
    every TP-harvested winner halt the tier instead of booking the win."""
    oco_id = str(getattr(oco, "id", "") or "")
    tp_id = stop_id = ""
    try:
        for _leg in (getattr(oco, "legs", None) or []):
            _lt = str(getattr(_leg, "order_type", None) or getattr(_leg, "type", "") or "").lower()
            _lid = str(getattr(_leg, "id", "") or "")
            if not _lid:
                continue
            if "stop" in _lt:
                stop_id = _lid
            elif "limit" in _lt:
                tp_id = _lid
    except Exception:  # noqa: BLE001 — malformed legs → parent-id fallbacks below (fail-closed)
        pass
    if not tp_id:
        tp_id = oco_id      # the OCO PARENT is the take-profit order
    if not stop_id:
        stop_id = oco_id    # degenerate fallback (legs missing) — reconcile/heal fail closed
    return stop_id, tp_id, oco_id


def _cancel_daytrade_exit_legs(symbol: str) -> None:
    """Cancel the day-tier's recorded OCO exit legs (take-profit + protective stop) for `symbol`
    by EXPLICIT order id. Alpaca OCO child legs do NOT carry the DT- tier client_order_id, so the
    tier-scoped cancel_open_orders_for_symbol(only_tier='daytrade') cannot reach them (tier_of_coid
    → None → fail-toward-inaction leaves them alone). An uncancelled OCO STOP leg holds the qty and
    would block a flatten's market reduce, and its sibling could fill after the flatten. Cancelling
    any leg of an OCO auto-cancels its sibling; we cancel every recorded id (oco parent + both legs)
    defensively. cancel_order is idempotent (already-resolved → True). Never raises."""
    from execution import broker
    try:
        st = _load_state()
    except Exception:  # noqa: BLE001
        return
    ids: set = set()
    for k, v in st.items():
        if not (k.startswith("entry::") and isinstance(v, dict) and v.get("symbol") == symbol):
            continue
        for _f in ("oco_order_id", "stop_order_id", "tp_order_id"):
            _id = str(v.get(_f) or "")
            if _id:
                ids.add(_id)
    for _id in ids:
        try:
            broker.cancel_order(_id)
        except Exception:  # noqa: BLE001 — best-effort; the flatten's own recovery handles a residual hold
            pass


def _cancel_recorded_exit_legs_confirmed(symbol: str) -> "bool | None":
    """Cancel recorded Day Tier exit legs and prove every one terminal.

    True means every readable recorded id is terminal; False means at least one remains live or
    pending; None means an id/order is unreadable. A successful cancel request alone is never proof.
    """
    from execution import broker
    try:
        state = _load_state()
    except Exception:  # noqa: BLE001
        return None
    ids: set = set()
    for k, v in state.items():
        if not (k.startswith("entry::") and isinstance(v, dict) and v.get("symbol") == symbol):
            continue
        for field in ("oco_order_id", "stop_order_id", "tp_order_id"):
            order_id = str(v.get(field) or "")
            if order_id:
                ids.add(order_id)
    terminal = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}
    unresolved = False
    for order_id in ids:
        try:
            order = broker.get_order(order_id)
        except Exception:  # noqa: BLE001
            return None
        if order is None:
            return None
        status = _enum_text(getattr(order, "status", None))
        if status in terminal:
            continue
        order_type = _enum_text(
            getattr(order, "order_type", None) or getattr(order, "type", None))
        if "stop" in order_type:
            if not broker.cancel_stop_confirmed(symbol, order_id):
                unresolved = True
            continue
        try:
            broker.cancel_order(order_id)
            reread = broker.get_order(order_id)
        except Exception:  # noqa: BLE001
            return None
        if reread is None:
            return None
        if _enum_text(getattr(reread, "status", None)) not in terminal:
            unresolved = True
    return not unresolved


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
        pending_target = {"trade_id": trade_id, "symbol": symbol, "side": position_side,
                          "entry_price": entry_price, "qty": qty}
        if _resolve_pending_exit(pending_target):
            # A prior close may have filled after its original polling budget. Reconcile its
            # cumulative broker fill before a later tick considers another close.
            return False
        # Cancel our OWN resting orders (the protective stop) so the market reduce isn't blocked.
        # Two cancels: (1) the recorded OCO exit legs by EXPLICIT id (child legs lack the DT- coid,
        # so the tier-scoped cancel below cannot reach them — an uncancelled OCO stop leg would hold
        # the qty and block this reduce); (2) the tier-scoped cancel for the plain-stop fallback path
        # (a DT-coid day stop) and any other DT-tagged resting order.
        _cancel_daytrade_exit_legs(symbol)
        try:
            broker.cancel_open_orders_for_symbol(symbol, only_tier="daytrade")
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] flatten: tier-scoped cancel raised (continuing): %s", symbol, e)
        close_order = broker.partial_close_position(symbol, int(qty), tier="daytrade", _return_order=True)
        close_order_id = str(getattr(close_order, "id", "") or "")
        if not close_order_id:
            _page(f"[{symbol}] day-tier flatten submit was not attributable to an order ({reason}) — "
                  f"cannot confirm the exit; {qty} sh may still be OPEN.")
            return False
        if not _set_pending_exit(trade_id, close_order_id, qty):
            _halt_unresolved_exit(symbol, "Forced-close order could not be durably recorded before fill polling.")
            return False
        close_readable, closed_qty, closed_px = _confirmed_order_fill(close_order_id, qty)
        if not close_readable or closed_qty + 1e-9 < qty or closed_px <= 0:
            partial_qty = int(math.floor(closed_qty)) if math.isfinite(closed_qty) else 0
            if partial_qty > 0 and closed_px > 0:
                partial_target = pending_target
                if not _record_partial_exit(partial_target, close_order_id, partial_qty, closed_px,
                                            mark, f"{reason}_partial"):
                    _halt_unresolved_exit(symbol, "A forced-close partial fill could not be persisted safely.")
            _page(f"[{symbol}] day-tier flatten fill UNCONFIRMED/PARTIAL ({closed_qty:g}/{qty}, {reason}) — "
                  "exit is not being recorded as complete; next tick retries any live residual.")
            return False
        # Broker-confirmed realized P&L; market mark remains a separate audit field.
        ep = abs(float(entry_price or 0.0))
        realized = 0.0
        if ep > 0:
            realized = round((closed_px - ep) * qty if position_side == "long" else (ep - closed_px) * qty, 2)
        if not day_tier_logger.log_exit_fill(trade_id or f"DT-{symbol}", symbol,
                                             order_id=close_order_id, exit_reason=reason,
                                             fill_price=closed_px, fill_qty=float(qty),
                                             market_price_at_exit=mark, realized_pnl=realized):
            _halt_unresolved_exit(symbol, "Forced-close fill could not be durably recorded.")
            return False
        try:
            trade_logger.log_event("exit", symbol=symbol, price=closed_px, size=int(qty),
                                   data_source="daytrade", tier="daytrade", exit_reason=reason,
                                   trade_id=trade_id, realized_pnl=realized)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] forced-close trade_logger write failed: %s", symbol, e)
        _clear_pending_exit(trade_id)
        logger.info("[%s] day-tier flattened %d sh (%s) fill %.2f realized %.2f",
                    symbol, qty, reason, closed_px, realized)
        return True
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
    # A lot marked for promotion to the Swing tier (execution/day_promotion.py, CEO 2026-10-09) is NOT a flatten
    # target: its own DAY stop protects it until 4:00 and the after-close hand-off books it. Unreadable state ->
    # nothing excluded (every lot keeps its normal Day exit).
    _promoting: set = set()
    try:
        _promoting = {str(v.get("coid") or "") for k, v in _load_state().items()
                      if k.startswith("entry::") and isinstance(v, dict) and v.get("state") == "promote_pending"}
    except Exception as e:  # noqa: BLE001
        logger.warning("flatten targets: promotion state read failed: %s", e)
    try:
        for t in day_tier_logger.open_trades_from_log().values():
            sym = str(t.get("symbol") or "")
            if sym and str(t.get("trade_id") or "") in _promoting:
                continue
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
                _fq = v.get("fill_qty")
                if _fq is not None and float(_fq) <= 0:
                    # fill_qty explicitly 0 = the lot was fully closed by booked partial exits (cold-2nd 2026-10-07:
                    # `fill_qty or qty` fell back to the ORIGINAL qty and resurrected a closed lot).
                    continue
                if sym and sym not in targets:  # log is preferred; state fills the crash-window gap
                    targets[sym] = {"symbol": sym, "side": str(v.get("side") or "long"),
                                    "qty": abs(int(float(_fq if _fq is not None else (v.get("qty") or 0)))),
                                    "entry_price": float(v.get("fill_px") or v.get("stop_px") or 0.0),
                                    "trade_id": str(v.get("coid") or ""),
                                    "order_id": str(v.get("order_id") or ""),
                                    "stop_order_id": str(v.get("stop_order_id") or ""),
                                    "tp_order_id": str(v.get("tp_order_id") or ""),
                                    "oco_order_id": str(v.get("oco_order_id") or ""),
                                    "geometry_error": str(v.get("geometry_error") or "")}
                elif sym and sym in targets:
                    # Enrich the log-sourced target with the state's recorded exit-leg ids so the
                    # reconcile HEAL can book a TP-harvested WINNER (take_profit) even if the
                    # best-effort target_placed event write failed (cold-2nd nit #1 / masked-loss A).
                    if not targets[sym].get("stop_order_id"):
                        targets[sym]["stop_order_id"] = str(v.get("stop_order_id") or "")
                    if not targets[sym].get("tp_order_id"):
                        targets[sym]["tp_order_id"] = str(v.get("tp_order_id") or "")
                    if not targets[sym].get("oco_order_id"):
                        targets[sym]["oco_order_id"] = str(v.get("oco_order_id") or "")
                    if not targets[sym].get("geometry_error"):
                        targets[sym]["geometry_error"] = str(v.get("geometry_error") or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("flatten targets: state read failed: %s", e)
    return targets


def _eod_stop_cleared(symbol: str) -> bool:
    """EOD exit (CEO 2026-10-07): cancel the day tier's OWN protective orders on `symbol` — the recorded OCO legs by
    id and any DT-tagged stop — and PROVE none is still live. Until this returns True the stop keeps protecting the
    lot (a pending_cancel stop can still execute), so the market exit is not sent yet and the caller retries.
    Never cancels another tier's order. Never raises."""
    from execution import broker
    try:
        legs = _cancel_recorded_exit_legs_confirmed(symbol)
        broker.cancel_open_orders_for_symbol(symbol, only_tier="daytrade")
        return legs is True and _has_live_daytrade_stop(symbol) is False
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] day-tier EOD stop cancel not confirmed (retrying): %s", symbol, e)
        return False


def _force_flat_one(tgt: dict, reason: str, confirm_cancel: bool,
                    submit_by: "float | None" = None, tries: "dict | None" = None) -> "tuple[bool, bool]":
    """One flatten attempt for one target. Returns (done, flattened): done=True when nothing more can be done for
    it in this call (closed, already flat, taken over by the swing tier, or a refusal a retry cannot fix);
    done=False means retry (the EOD stop cancel is not confirmed yet, or the close did not complete).
    `submit_by` (time.monotonic()): no market close is sent at/after it — a market order after the 4:00 close would
    be queued for the next open (cold-2nd 2026-10-07); the after-hours exit takes the lot instead."""
    from execution import broker
    sym = tgt["symbol"]
    if _closed_in_log(str(tgt.get("trade_id") or "")):
        # Already booked closed (e.g. a market close earlier this pass): retire the state record so it can never
        # be re-targeted — on a co-held symbol a re-target would sell ANOTHER tier's shares (cold-2nd 2026-10-07).
        _retire_trade_record(str(tgt.get("trade_id") or ""))
        return True, False
    try:
        pos = broker.get_open_position(sym)
    except Exception:
        pos = None
    if pos is None:
        return True, False
    held = abs(int(float(getattr(pos, "qty", 0) or 0)))
    want = int(tgt["qty"])
    if want < 1:
        _page(f"[{sym}] day-tier force-flat: recorded own-qty is 0/missing — REFUSING to "
              f"close (never close the cross-tier net); manual check needed. ({reason})")
        return True, False
    qty = min(held, want)
    # Only a lot with NO live day-tier stop can be "held by another tier" (our own OCO is flattened as usual);
    # the foreign stop must cover the whole position.
    _ft_eod: list = []
    if (qty >= 1 and _has_live_daytrade_stop(sym) is False
            and _foreign_stop_covers(sym, str(tgt.get("side") or "long"), held, _ft_eod)):
        if _record_transfer(tgt, _ft_eod, held, want, abs(float(getattr(pos, "current_price", 0) or 0))):
            return True, False
        _page_once_today(sym, "foreign_stop_eod",
                         f"[{sym}] day-tier force-flat ({reason}) skipped — the lot was taken over by the "
                         f"{_tier_names(_ft_eod)}, whose stop protects it and which now manages it. Close "
                         f"manually if it must be flat.")
        return True, False
    if qty < 1:
        return True, False
    if confirm_cancel:
        if not _eod_stop_cleared(sym):
            return False, False   # the stop is still live (protecting the lot) — retry the cancel next pass
        # The stop is terminal — it may have FILLED during the cancel (cold-2nd 2026-10-07). Book that first; never
        # follow it with a market sell of shares the lot no longer owns (on a co-held symbol they are another tier's).
        _stop_fill = _record_confirmed_stop_exit(tgt)
        if _stop_fill is True:
            _retire_trade_record(str(tgt.get("trade_id") or ""))
            return True, False
        if _stop_fill is None:
            return False, False   # stop fill unreadable/partial-unbooked — retry next pass
        _fresh = _flatten_targets().get(sym)
        if not _fresh or _fresh.get("trade_id") != tgt.get("trade_id"):
            return True, False
        qty = min(held, int(_fresh.get("qty") or 0))
        if qty < 1:
            return True, False
    if submit_by is not None and time.monotonic() >= submit_by:
        return True, False    # too close to the close for a market order; after_hours_exit takes it
    if tries is not None:
        tries[sym] = tries.get(sym, 0) + 1
        if tries[sym] > 3:
            return True, False    # 3 market attempts per lot per window (no page storm); after_hours_exit follows
    ok = flatten_position(sym, qty, tgt["side"], entry_price=tgt["entry_price"],
                          trade_id=tgt["trade_id"], order_id_hint=tgt["order_id"], reason=reason)
    if ok:
        _retire_trade_record(str(tgt.get("trade_id") or ""))   # retire it now (the 4:00 tick runs after_hours_exit first)
    return ok, ok


def force_flat_all(reason: str = "eod_force_flat", deadline: "float | None" = None) -> int:
    """Flatten EVERY open day-tier position (EOD force-flat / tier-kill). Unions the durable-log
    open set with the state file's filled/protected records (crash-window safety), reconciled
    against the live broker position. NEVER falls back to the whole broker-held qty (B1: masked-loss
    /cold-2nd — a missing recorded qty must SKIP+PAGE, never close `held`, which is the cross-tier
    net). Returns the count flattened. No-op when DAYTRADE_ENABLED is False.

    `deadline` (time.monotonic() value; the EOD exit, CEO 2026-10-07): retry in passes until every lot is closed or
    the deadline passes — each lot's stop is cancelled with CONFIRMATION before its market close (the stop protects
    the lot until then), and a partial close is retried for the remainder. None = one pass (tier kill, clock loss).
    A lot still open at the deadline is left to after_hours_exit (an extended-hours limit at the bid/ask)."""
    if not _enabled():
        return 0
    n = 0
    done: set = set()
    tries: dict = {}
    retry_s = float(_cfg("DAYTRADE_EOD_RETRY_S", 1.0))
    # The last moment a market close may be SENT: 2 s before the close (deadline = close - EOD_EXIT_GUARD_S).
    submit_by = (deadline + max(0.0, float(_cfg("DAYTRADE_EOD_EXIT_GUARD_S", 10.0)) - 2.0)
                 if deadline is not None else None)
    try:
        while True:
            targets = _flatten_targets()
            todo = [t for s, t in targets.items() if s not in done]
            if not todo:
                break
            for tgt in todo:
                if deadline is not None and time.monotonic() >= deadline:
                    break   # no new lot starts after the deadline (cold-2nd 2026-10-07)
                try:
                    finished, flat = _force_flat_one(tgt, reason, confirm_cancel=deadline is not None,
                                                     submit_by=submit_by,
                                                     tries=tries if deadline is not None else None)
                except Exception as e:  # noqa: BLE001 — one symbol never stops the others
                    finished, flat = False, False
                    logger.warning("[%s] day-tier force-flat attempt raised: %s", tgt.get("symbol"), e)
                n += 1 if flat else 0
                if finished:
                    done.add(tgt["symbol"])
            if deadline is None or time.monotonic() + retry_s >= deadline:
                break
            if all(s in done for s in _flatten_targets()):
                break
            time.sleep(retry_s)
        if deadline is not None:
            left = [s for s in _flatten_targets() if s not in done]
            if left:
                _page(f"day-tier EOD exit: {', '.join(sorted(left))} still open at the close ({reason}) — the "
                      f"after-hours exit (extended-hours limit at the bid/ask) will close them.")
    except Exception as e:  # noqa: BLE001
        _page(f"day-tier force_flat_all RAISED ({reason}): {e!r}")
    return n


def _ah_exit_price(symbol: str, close_side: str, step: float = 0.0) -> "tuple[float | None, str]":
    """After-hours exit price (CEO 2026-10-07: "fill at whatever the bid is"): the live IEX bid for a sell, the ask
    for a buy; with no usable quote, the latest trade -/+ DAYTRADE_AH_FALLBACK_PCT. `step` (0..0.02) moves the price
    that fraction further through the touch after unfilled ticks (board Harris 2026-10-07: an unchanging IEX quote can
    leave the order unfilled). (None, why) when no price exists. Never raises."""
    try:
        step = max(0.0, min(float(step), 0.02))
        px, why = _ah_touch_price(symbol, close_side)
        if px is None or step <= 0:
            return px, why
        px2 = round(px * (1.0 - step if close_side == "sell" else 1.0 + step), 2)
        return px2, f"{why} stepped {step:.1%} through"
    except Exception as e:  # noqa: BLE001
        return None, f"price read failed: {e!r}"


def _ah_touch_price(symbol: str, close_side: str) -> "tuple[float | None, str]":
    """The bid (sell) / ask (buy), else latest trade -/+ DAYTRADE_AH_FALLBACK_PCT. Never raises."""
    try:
        from data.alpaca_data import get_latest_quote, get_latest_trade
        q = get_latest_quote(symbol) or {}
        bid, ask = float(q.get("bid") or 0.0), float(q.get("ask") or 0.0)
        if close_side == "sell" and math.isfinite(bid) and bid > 0 and (ask <= 0 or bid <= ask):
            return round(bid, 2), f"bid {bid:.2f}"
        if close_side == "buy" and math.isfinite(ask) and ask > 0 and (bid <= 0 or bid <= ask):
            return round(ask, 2), f"ask {ask:.2f}"
        lt = get_latest_trade(symbol)
        f = float(_cfg("DAYTRADE_AH_FALLBACK_PCT", 0.01))
        if lt is not None and math.isfinite(float(lt)) and float(lt) > 0:
            px = float(lt) * (1.0 - f if close_side == "sell" else 1.0 + f)
            return round(px, 2), f"latest trade {float(lt):.2f} {'-' if close_side == 'sell' else '+'}{f:.1%} (no usable quote)"
        return None, "no usable quote or trade"
    except Exception as e:  # noqa: BLE001
        return None, f"price read failed: {e!r}"


def _ah_account(tgt: dict, order_id: str, accounted: float) -> str:
    """Book any new fill on the after-hours exit order: the whole remaining lot -> exit_fill (closes the trade);
    less -> partial_exit_fill (reduces the owned qty). Returns 'closed' | 'terminal' | 'open' | 'unreadable'.
    The fill price is the order's cumulative average (exact for a single fill; the Alpaca fills are the P&L system
    of record). Never raises."""
    from execution import broker
    from strategy import day_tier_logger
    import trade_logger
    sym = str(tgt.get("symbol") or "")
    try:
        o = broker.get_order(order_id)
        if o is None:
            return "unreadable"
        cum = float(getattr(o, "filled_qty", 0) or 0)
        px = float(getattr(o, "filled_avg_price", 0) or 0)
        status = _enum_text(getattr(o, "status", None))
        if not (math.isfinite(cum) and math.isfinite(px) and math.isfinite(accounted)) or cum + 1e-9 < accounted:
            return "unreadable"
        delta = int(math.floor(cum - accounted + 1e-9))
        remaining = int(tgt.get("qty") or 0)
        if delta >= 1 and px > 0:
            _bv = (_entry_record(str(tgt.get("trade_id") or "")) or {}).get("pending_exit_accounted_value")
            px = _increment_price(cum, px, accounted, float(_bv) if _bv is not None else None, delta)  # these shares' price
            if delta >= remaining:
                entry = abs(float(tgt.get("entry_price") or 0.0))
                side = str(tgt.get("side") or "long")
                realized = round((px - entry) * remaining if side == "long" else (entry - px) * remaining, 2) if entry > 0 else 0.0
                if not day_tier_logger.log_exit_fill(str(tgt.get("trade_id") or ""), sym, order_id=order_id,
                                                     exit_reason="eod_after_hours_exit", fill_price=px,
                                                     fill_qty=float(remaining), market_price_at_exit=px,
                                                     realized_pnl=realized):
                    _halt_unresolved_exit(sym, "After-hours exit filled but the fill could not be durably recorded.")
                    return "unreadable"
                try:
                    trade_logger.log_event("exit", symbol=sym, price=px, size=remaining, data_source="daytrade",
                                           tier="daytrade", exit_reason="eod_after_hours_exit",
                                           trade_id=str(tgt.get("trade_id") or ""), realized_pnl=realized)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[%s] after-hours exit trade_logger write failed: %s", sym, e)
                _clear_pending_exit(str(tgt.get("trade_id") or ""))
                _retire_trade_record(str(tgt.get("trade_id") or ""))
                logger.info("[%s] day-tier after-hours exit FILLED %d @ %.2f (realized %.2f)", sym, remaining, px, realized)
                return "closed"
            if not _record_partial_exit(tgt, order_id, delta, px, px, "eod_after_hours_exit_partial"):
                _halt_unresolved_exit(sym, "After-hours exit partial fill could not be durably recorded.")
                return "unreadable"
        if status == "filled" and not (px > 0):
            return "unreadable"   # a fill with no price cannot be booked (matches _confirmed_order_fill)
        if status in ("filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"):
            _clear_pending_exit(str(tgt.get("trade_id") or ""))
            return "terminal"
        return "open"
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] after-hours exit fill read failed: %s", sym, e)
        return "unreadable"


def _entry_record(trade_id: str) -> "dict | None":
    """The state entry record whose coid is `trade_id`, or None. Never raises."""
    try:
        return next((v for k, v in _load_state().items() if k.startswith("entry::") and isinstance(v, dict)
                     and v.get("coid") == trade_id), None)
    except Exception:  # noqa: BLE001
        return None


def _pending_accounted(trade_id: str) -> float:
    """Shares of the recorded exit order already booked (state pending_exit_accounted_qty). Never raises."""
    try:
        return float((_entry_record(trade_id) or {}).get("pending_exit_accounted_qty") or 0.0)
    except (TypeError, ValueError):
        return float("nan")   # _ah_account treats a non-finite watermark as unreadable


def after_hours_exit(only_pending: bool = False) -> dict:
    """CEO 2026-10-07: every day-tier lot (no promotion yet) is closed out by end of day. A lot still open after the
    regular-hours close gets an extended-hours GTC LIMIT at the live bid (sell) / ask (buy), re-priced each tick when
    the touch moves away, until it fills. Run by the runner whenever the market is closed (after hours and pre-
    market). Never a market order after hours (Alpaca queues those for the next open). Never touches another tier's
    shares (net side + own recorded qty). `only_pending=True` (regular hours): manage ONLY lots that already carry an
    after-hours exit order (one still unfilled at the open keeps being re-priced at the touch); such a lot has no stop
    (cancelled at the 3:58 exit) and no other day-tier order can exist on its symbol (place_entry blocks re-entry
    while it is open). Returns a summary. Never raises."""
    if not _enabled():
        return {"enabled": False}
    from execution import broker
    summary = {"checked": 0, "placed": 0, "repriced": 0, "closed": 0, "waiting": 0, "skipped": 0}
    try:
        for sym, tgt in _flatten_targets().items():
            trade_id = str(tgt.get("trade_id") or "")
            if _closed_in_log(trade_id):
                # Booked closed (e.g. by the 3:58 market exit, or a stop that filled before 4:00) but the state record
                # was not yet retired. Never re-target it — on a co-held symbol the "position" is another tier's
                # (cold-2nd 2026-10-07). First make sure no exit order of ours is still resting: a later fill would sell
                # shares the lot no longer owns. Retire only once that order is confirmed terminal.
                _pc = str((_entry_record(trade_id) or {}).get("pending_exit_order_id") or "")
                if _pc:
                    _po = broker.get_order(_pc)
                    _terminal = ("filled", "canceled", "cancelled", "expired", "rejected", "done_for_day")
                    if _po is None or _enum_text(getattr(_po, "status", None)) not in _terminal:
                        broker.cancel_order(_pc)
                        _po = broker.get_order(_pc)
                        if _po is None or _enum_text(getattr(_po, "status", None)) not in _terminal:
                            summary["waiting"] += 1
                            _page_once_today(sym, "ah_exit_closed_cancel",
                                             f"[{sym}] day-tier lot is already closed but its exit order {_pc} is not "
                                             f"confirmed cancelled yet — retrying each tick.")
                            continue
                    _clear_pending_exit(trade_id)
                _retire_trade_record(trade_id)
                continue
            _rec0 = _entry_record(trade_id) if trade_id else None
            _pend0 = str((_rec0 or {}).get("pending_exit_order_id") or "")
            _ah0 = _pend0 and str((_rec0 or {}).get("pending_exit_kind") or "") == "after_hours"
            if only_pending and not _ah0:
                continue
            summary["checked"] += 1
            try:
                pos = broker.get_open_position(sym)
            except Exception as e:  # noqa: BLE001
                summary["skipped"] += 1
                logger.warning("[%s] after-hours exit: position read failed (next tick): %s", sym, e)
                continue
            if pos is None:
                if _ah0:
                    # Flat (filled between ticks, or closed another way): book any fill, and never leave the GTC exit
                    # resting with no position behind it — a later fill would OPEN a new position.
                    if _ah_account(tgt, _pend0, _pending_accounted(trade_id)) == "open":
                        broker.cancel_order(_pend0)
                        _page_once_today(sym, "ah_exit_orphan_cancel",
                                         f"[{sym}] day-tier lot is flat but its after-hours exit order {_pend0} was "
                                         f"still open — cancelled so it cannot open a new position.")
                continue  # flat — reconcile books how it closed
            side = str(tgt.get("side") or "long")
            want = int(tgt.get("qty") or 0)
            held = abs(int(float(getattr(pos, "qty", 0) or 0)))
            if want < 1 or not trade_id or (getattr(pos, "side", None) == "long") != (side == "long") or held < 1:
                summary["skipped"] += 1
                _page_once_today(sym, "ah_exit_refused",
                                 f"[{sym}] day-tier after-hours exit REFUSED — own qty {want}, live net "
                                 f"{getattr(pos, 'side', '?')} {held} vs our {side}: will not trade another tier's "
                                 f"shares. Manual check.")
                continue
            close_side = "sell" if side == "long" else "buy"
            rec = _entry_record(trade_id)
            pending = str((rec or {}).get("pending_exit_order_id") or "")
            _stale_inc = 0   # 1 when this reprice is because the order sat unfilled (not because the touch moved)
            if pending:
                res = _ah_account(tgt, pending, _pending_accounted(trade_id))
                if res == "closed":
                    summary["closed"] += 1
                    continue
                if res == "unreadable":
                    summary["waiting"] += 1
                    continue
                if res == "open":
                    o = broker.get_order(pending)
                    lim = float(getattr(o, "limit_price", 0) or 0) if o is not None else 0.0
                    px_now, _src = _ah_touch_price(sym, close_side)
                    away = (px_now is not None and lim > 0
                            and ((close_side == "sell" and px_now < lim) or (close_side == "buy" and px_now > lim)))
                    # A recorded exit that is NOT the after-hours limit (a regular-hours market close sent near 4:00 is
                    # queued for the next open) is replaced by the after-hours limit (board Taleb 2026-10-07).
                    stale_rth = str((rec or {}).get("pending_exit_kind") or "") != "after_hours"
                    ticks = int((rec or {}).get("pending_exit_open_ticks") or 0) + 1
                    if not (away or stale_rth or ticks >= 2):
                        _bump_pending_ticks(trade_id, ticks)
                        summary["waiting"] += 1
                        continue
                    _stale_inc = 0 if (away or stale_rth) else 1
                    broker.cancel_order(pending)
                    for _ in range(5):
                        time.sleep(1.0)
                        res = _ah_account(tgt, pending, _pending_accounted(trade_id))
                        if res in ("closed", "terminal", "unreadable"):
                            break
                    if res == "closed":
                        summary["closed"] += 1
                        continue
                    if res != "terminal":
                        summary["waiting"] += 1   # cancel not confirmed yet — never two exit orders at once
                        continue
                    summary["repriced"] += 1
                # 'terminal': the old order is done; re-read the owned qty (a partial reduced it) and the live position,
                # then place a new one.
                tgt = _flatten_targets().get(sym) or {}
                want = int(tgt.get("qty") or 0)
                if want < 1:
                    continue
                try:
                    _pos2 = broker.get_open_position(sym)
                except Exception:  # noqa: BLE001
                    _pos2 = None
                if _pos2 is None or (getattr(_pos2, "side", None) == "long") != (side == "long"):
                    continue
                held = abs(int(float(getattr(_pos2, "qty", 0) or 0)))
            # Taken over by another tier (its stop now protects the lot — the 2026-10-07 EWY/AAPL case): never place an
            # exit for shares another tier manages. A whole-position take-over by the swing tier is booked as a
            # transfer at the mark (same as the 3:58 path); anything else is skipped and paged once.
            _ft_ah: list = []
            if _has_live_daytrade_stop(sym) is False and _foreign_stop_covers(sym, side, held, _ft_ah):
                if _record_transfer(tgt, _ft_ah, held, want, abs(float(getattr(pos, "current_price", 0) or 0))):
                    summary["closed"] += 1
                    continue
                summary["skipped"] += 1
                _page_once_today(sym, "ah_foreign_stop",
                                 f"[{sym}] day-tier lot is protected by the {_tier_names(_ft_ah)}'s stop after the "
                                 f"close — no after-hours exit placed; the {_tier_names(_ft_ah)} manages it.")
                continue
            if not _eod_stop_cleared(sym):
                summary["waiting"] += 1
                _page_once_today(sym, "ah_exit_stop_live",
                                 f"[{sym}] day-tier after-hours exit waiting — a day-tier stop/leg is still live or "
                                 f"its cancel is unconfirmed; retrying each tick.")
                continue
            # The stop is terminal — it may have FILLED before the close (cold-2nd 2026-10-07). Book that first and never
            # place an exit for shares the lot no longer owns (on a co-held symbol they are another tier's).
            _sf = _record_confirmed_stop_exit(tgt)
            if _sf is True:
                _retire_trade_record(trade_id)
                summary["closed"] += 1
                continue
            if _sf is None:
                summary["waiting"] += 1
                _page_once_today(sym, "ah_exit_stop_unreadable",
                                 f"[{sym}] day-tier after-hours exit WAITING — the lot's stop fill cannot be read "
                                 f"(an exit-order id is unreadable), so no exit is placed; the lot is open with no stop "
                                 f"after hours. Retrying each tick — manual check if it persists.")
                continue
            _fresh = _flatten_targets().get(sym)
            if not _fresh or _fresh.get("trade_id") != trade_id:
                continue
            want = int(_fresh.get("qty") or 0)
            if want < 1:
                continue
            qty = min(held, want)
            # Unfilled-only reprices step 0.5% further through the touch each (cap 2%); a reprice because the touch
            # moved goes to the new touch at the current step.
            _reprices = int((rec or {}).get("pending_exit_reprices") or 0) + _stale_inc
            px, src = _ah_exit_price(sym, close_side, step=0.005 * _reprices)
            if px is None or not (math.isfinite(px) and px > 0):
                summary["skipped"] += 1
                _page_once_today(sym, "ah_exit_no_price",
                                 f"[{sym}] day-tier after-hours exit NOT placed — {src}. {qty} sh {side} still open; "
                                 f"retrying each tick.")
                continue
            order = broker.submit_limit_order(sym, qty, close_side, px, extended_hours=True, tier="daytrade",
                                              time_in_force="gtc")
            oid = str(getattr(order, "id", "") or "") if order is not None else ""
            if not oid:
                summary["skipped"] += 1
                _page_once_today(sym, "ah_exit_submit_failed",
                                 f"[{sym}] day-tier after-hours exit submit FAILED ({close_side} {qty} @ {px:.2f}); "
                                 f"retrying each tick.")
                continue
            if not _set_pending_exit(trade_id, oid, qty, kind="after_hours", reprices=_reprices):
                _halt_unresolved_exit(sym, f"After-hours exit order {oid} could not be durably recorded.")
                continue
            summary["placed"] += 1
            _page_once_today(sym, "ah_exit_placed",
                             f"[{sym}] day-tier lot still open after the close — after-hours exit placed: "
                             f"{close_side} {qty} @ ${px:.2f} ({src}), extended-hours GTC limit, re-priced each tick "
                             f"until filled.")
        logger.info("day-tier after-hours exit: %s", summary)
        return summary
    except Exception as e:  # noqa: BLE001
        _page(f"day-tier after_hours_exit RAISED: {e!r}")
        return {"error": repr(e)}


# ── daily dollar risk budget (Rafael 2026-10-04 — replaces the 3-position count cap) ──────────────
def _daily_risk_used(open_trades: dict, state: dict, risk_ceiling_usd: float) -> "tuple[float, float, str] | None":
    """Dollars already at risk for the day tier TODAY = realized day-tier losses today (loss-only; gains
    never offset) + every open day-tier lot's loss-if-stopped (|entry - stop| x qty). Day-tier stops never
    trail, so the loss-if-stopped is fixed at entry. A lot whose stop or entry cannot be read counts at
    `risk_ceiling_usd` (the per-trade maximum the sizing allows) — never as zero. A same-day non-terminal
    state record with no logged entry_fill (submit/log crash window) also counts at the ceiling — including a
    crash-left 'submitting' record, which keeps counting until the day ends (deliberate conservatism).
    Returns (realized_loss_usd >= 0, open_stop_risk_usd >= 0, detail) or None if the realized-loss journal is
    unreadable (caller fails closed)."""
    from strategy import day_tier_logger
    realized, readable = _realized_loss_today()
    if not readable:
        return None
    events, ev_ok = day_tier_logger.read_events_checked()
    stops: dict = {}
    if ev_ok:
        for e in events:
            if e.get("event") == "stop_placed" and e.get("trade_id"):
                stops[str(e["trade_id"])] = e.get("stop_price")
    open_risk = 0.0
    n_open = 0
    logged = set()
    for t in open_trades.values():
        tid = str(t.get("trade_id") or "")
        logged.add(tid)
        n_open += 1
        try:
            qty = abs(float(t.get("fill_qty") or 0.0))
            entry = float(t.get("entry_price") or 0.0)
            _sp = stops.get(tid)
            stop = float(_sp) if _sp is not None else float("nan")
            r = abs(entry - stop) * qty
            if not (math.isfinite(r) and qty > 0 and entry > 0 and stop > 0):
                raise ValueError("unreadable lot risk")
        except (TypeError, ValueError):
            r = risk_ceiling_usd
        open_risk += r
    # A lot with a logged entry_fill is either OPEN (counted above at its stop) or EXITED (its loss is in
    # realized) — never a phantom. A 'protected' state record never transitions after its stop/target fills,
    # so without this a closed winner would be charged at the ceiling all day (masked-loss seat 2026-10-04;
    # mirrors the Track-B _state_only logic). Unreadable log -> only the open set is known (over-count, safe).
    if ev_ok:
        logged |= {str(e.get("trade_id") or "") for e in events if e.get("event") == "entry_fill"}
    today = f"{_now_et():%Y%m%d}"
    for k, v in state.items():
        if (k.startswith("entry::") and isinstance(v, dict)
                and v.get("state") in ("submitting", "submitted", "filled", "protected", "fill_unverified")
                and str(v.get("bar_id") or "").split("-", 1)[0] == today
                and str(v.get("coid") or "") not in logged):
            open_risk += risk_ceiling_usd
            n_open += 1
    realized_loss = max(0.0, -realized)
    return realized_loss, open_risk, f"realized loss ${realized_loss:.2f} + open stop-risk ${open_risk:.2f} ({n_open} lot(s))"


def _retire_unsubmitted(state: dict, key: str) -> bool:
    """Mark a pre-submit ('submitting') entry record terminal ('submit_failed') when NO order was sent, so it is
    not counted as open risk by _daily_risk_used and does not block re-entry. Only a 'submitting' record is
    touched (anything later may own a live order). Returns the save result. Never raises."""
    try:
        rec = state.get(key)
        if isinstance(rec, dict) and rec.get("state") == "submitting":
            rec["state"] = "submit_failed"
            return _save_state(state)
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("retire-unsubmitted failed for %s: %s", key, e)
        return False


def _budget_fit_qty(qty: int, per_share_risk: float, budget: float, realized_loss: float,
                    open_risk: float, slip_mult: float) -> int:
    """Largest share count <= qty whose risk fits the daily dollar budget:
    realized_loss + slip_mult x (open_risk + n x per_share_risk) <= budget. Realized losses are booked (no
    slippage scaling); open and new stop-risk are scaled for gap-through. Returns 0 when nothing fits or any
    input is invalid (fail closed). Pure function; never raises."""
    try:
        vals = (per_share_risk, budget, realized_loss, open_risk, slip_mult)
        if qty < 1 or not all(math.isfinite(float(v)) for v in vals):
            return 0
        if per_share_risk <= 0 or budget <= 0 or realized_loss < 0 or open_risk < 0 or slip_mult < 1.0:
            return 0
        room = budget - realized_loss - slip_mult * open_risk
        if room <= 0:
            return 0
        return max(0, min(int(qty), math.floor(room / (slip_mult * per_share_risk))))
    except (TypeError, ValueError):
        return 0


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
                bar_id: str, equity: float, decision_id: str = "", min_qty: int = 1) -> bool:
    """Place ONE day-tier entry (Track A, or Track B when size["track"]=="B" — exposure-capped at wire time)
    with a confirmed protective stop. Idempotent per (symbol, bar_id). Returns True on a filled+protected entry, False otherwise. NEVER raises into
    the caller (the runner). No-op when DAYTRADE_ENABLED is False. `min_qty`: skip (recorded for last_entry_skip)
    when fewer shares fit; a leveraged/inverse ETF always needs >= 2 (_min_entry_qty)."""
    _LAST_ENTRY_SKIP.pop(str(symbol), None)
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
    if not math.isfinite(entry_ref) or entry_ref <= 0:
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

        # No position-COUNT cap (Rafael 2026-10-04): concurrency is bounded by the daily DOLLAR risk budget
        # applied after wire-time sizing below (realized loss + every open lot's loss-if-stopped + this entry's
        # risk <= DAYTRADE_TIER_KILL_EQUITY_PCT x start-of-day equity). No sector/correlation limit either.

        # Structural stop FIRST (never enter a position we can't protect — B2 precondition).
        stop_px = _compute_stop_price(trigger, direction, entry_ref)
        if stop_px is None:
            logger.warning("[%s] day-tier entry aborted — no sane structural stop", symbol)
            return False

        # Bound against the actual marketable-limit price, not the signal reference. This closes
        # the long-side +slippage boundary breach where 15×$100 passed a $1,500 cap but the submitted
        # 15×$100.20 order reserved $1,503.
        slip = float(_cfg("DAYTRADE_ENTRY_SLIPPAGE_PCT", 0.002))
        # LIVE LIMIT (CEO order 2026-10-06; replay: entries priced off a minutes-old bar never filled): price the
        # marketable limit off the real-time IEX price when one is fresh, else the signal reference.
        _px_base = entry_ref
        try:
            from data.live_price import live_price as _live_price
            _lp = _live_price(symbol)
            _band = float(_cfg("DAYTRADE_LIVE_PRICE_SANITY_PCT", 0.05))  # PROV:daytier-must-trade-2026-10-06
            if (_lp is not None and math.isfinite(_lp.price) and _lp.price > 0
                    and abs(_lp.price / entry_ref - 1.0) <= _band):   # a print >5% off the signal = a bad print
                _px_base = float(_lp.price)
        except Exception as _lpe:  # noqa: BLE001 — a failed read keeps the signal reference
            logger.warning("[%s] day-tier live limit price unavailable (signal ref used): %s", symbol, _lpe)
        limit_px = round(_px_base * (1.0 + slip) if direction == "long" else _px_base * (1.0 - slip), 2)
        # MARKETABLE AT THE TOUCH (2026-10-07; board Harris + Taleb, Gro, GAI): a limit priced off the last trade rests
        # when the trade sits off the bid/ask (EWY 10/07: sell limit $181.12, bid $180.44 -> 1 of 7 filled). With a
        # usable IEX quote (spread <= DAYTRADE_STOP_SPREAD_SANITY_PCT of mid, mid within the live band), a long pays up to max(ask, trade) + slip and a short sells
        # down to min(bid, trade) - slip, never more than DAYTRADE_ENTRY_TOUCH_CAP_PCT through the trade price.
        _touch_src = "trade"
        try:
            from data.alpaca_data import get_latest_quote as _glq
            _q = _glq(symbol) or {}
            _bid, _ask = float(_q.get("bid") or 0.0), float(_q.get("ask") or 0.0)
            _sanity = float(_cfg("DAYTRADE_STOP_SPREAD_SANITY_PCT", 0.02))
            _cap = float(_cfg("DAYTRADE_ENTRY_TOUCH_CAP_PCT", 0.01))  # PROV:entry-fill-rate-2026-10-07
            _band_q = float(_cfg("DAYTRADE_LIVE_PRICE_SANITY_PCT", 0.05))
            if (math.isfinite(_bid) and math.isfinite(_ask) and 0 < _bid <= _ask
                    and (_ask - _bid) <= _sanity * ((_bid + _ask) / 2.0)
                    and abs(((_bid + _ask) / 2.0) / entry_ref - 1.0) <= _band_q and 0 < slip < _cap):
                if direction == "long":
                    limit_px = round(min(max(_px_base, _ask) * (1.0 + slip), _px_base * (1.0 + _cap)), 2)
                else:
                    limit_px = round(max(min(_px_base, _bid) * (1.0 - slip), _px_base * (1.0 - _cap)), 2)
                _touch_src = f"touch (bid {_bid:.2f} / ask {_ask:.2f}, trade {_px_base:.2f})"
        except Exception as _tqe:  # noqa: BLE001 — an unreadable quote keeps the trade-based limit
            logger.warning("[%s] day-tier touch quote unavailable (trade-based limit): %s", symbol, _tqe)
        logger.info("[%s] day-tier marketable limit %.2f from %s", symbol, limit_px, _touch_src)
        if not (math.isfinite(limit_px) and limit_px > 0):
            logger.warning("[%s] day-tier entry aborted — invalid marketable-limit price", symbol)
            return False

        # ROOM STOP (CEO order 2026-10-06 — replaces the skip-on-no-room gate for live entries): a structural stop
        # inside the volatility band is WIDENED to the minimum room (from the limit and the live touch) instead of
        # skipping the trade; wire-time sizing and the daily dollar budget then size to the wider stop.
        stop_px, room_why = _room_stop(symbol, direction, limit_px, stop_px)
        if stop_px is None or (direction == "long" and stop_px >= limit_px) or \
                (direction == "short" and stop_px <= limit_px):
            logger.warning("[%s] day-tier entry aborted — %s", symbol, room_why)
            return False
        logger.info("[%s] day-tier %s", symbol, room_why)

        # Live book (fail-CLOSED) — used for BOTH the opposite-side guard and the B6 gross cap.
        try:
            acct = broker.get_account()
            buying_power = float(getattr(acct, "buying_power", 0.0) or 0.0)
            live_equity = float(getattr(acct, "equity", 0.0) or 0.0)
            day_start_raw = getattr(acct, "last_equity", None)
            maintenance_raw = getattr(acct, "maintenance_margin", None)
            if day_start_raw is None or maintenance_raw is None:
                raise ValueError("account risk fields missing")
            day_start_equity = float(day_start_raw)
            maintenance_margin = float(maintenance_raw)
            positions = broker.get_open_positions()
            pos_by_sym = {getattr(p, "symbol", None): p for p in (positions or [])}
            open_orders = broker.get_open_orders()
            maintenance_rate = broker.get_asset_maintenance_margin_rate(symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] day-tier entry aborted — live book unreadable (fail-closed): %s", symbol, e)
            return False
        halt_reason = _account_entry_halt_reason(acct)
        if halt_reason:
            logger.warning("[%s] day-tier entry aborted — %s", symbol, halt_reason)
            return False
        if open_orders is None or maintenance_rate is None:
            logger.warning("[%s] day-tier entry aborted — order book or maintenance rate unreadable (fail-closed)", symbol)
            return False
        if direction == "short":
            # limit_px is finite and > 0 here: it was checked right after it was computed (the entry aborts otherwise),
            # and _short_maintenance_rate re-checks math.isfinite + > 0 before any division (returns None -> skip).
            # A short carries Alpaca's SHORT maintenance requirement (board Thorp + Taleb 2026-10-07): the posted asset
            # rate is the long rate. Above 100% (a short under $2.50) the maintenance room cannot be sized -> skip.
            maintenance_rate = _short_maintenance_rate(maintenance_rate, limit_px)
            if maintenance_rate is None or maintenance_rate > 1.0:
                logger.info("[%s] day-tier short skipped — short maintenance requirement %s > 100%% at $%.2f",
                            symbol, maintenance_rate, limit_px)
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
            # NO SAME-SIDE CO-HOLD (interim, 2026-10-07; board Peterffy/Taleb + Gro + GAI): the day tier's own lots
            # already returned "already active" above, so a live position here belongs to ANOTHER tier. The main
            # bot's exits are whole-symbol closes priced from the first fill on the symbol, so a shared symbol could
            # sell the day tier's shares and book a foreign fill (a masked loss). The runner routes longs on such a
            # symbol to a free 2x ETF; anything that still reaches here is skipped. Checked right before submit, from
            # the live book read above. Kill flag: DAYTRADE_NO_COHOLD (only an explicit False disables it).
            if _cfg("DAYTRADE_NO_COHOLD", True) is not False:
                logger.info("[%s] day-tier entry skipped — another tier holds a %s position on this symbol "
                            "(no co-hold until the per-tier ownership guard ships)", symbol,
                            getattr(existing, "side", "?"))
                return False
        if _cfg("DAYTRADE_NO_COHOLD", True) is not False:
            # ...nor while ANOTHER tier has a pending order on the symbol (an entry about to create a co-hold;
            # masked-loss seat 2026-10-07). Our own day-tier orders were cleared by the "already active" check.
            # open_orders is the SAME pre-submit book read validated above (None -> return False): no new API call.
            from execution.ownership_guard import tier_of_coid as _tier_of
            _other = [o for o in (open_orders or [])
                      if str(getattr(o, "symbol", "") or "") == symbol
                      and _tier_of(getattr(o, "client_order_id", None)) != "daytrade"]
            if _other:
                logger.info("[%s] day-tier entry skipped — another tier has %d open order(s) on this symbol "
                            "(no co-hold)", symbol, len(_other))
                return False

        # Per-track attribution (Track B Inc 2 Part 2): the size dict carries compute_day_tier_size's track.
        # Anything other than "B" (incl. a missing key on a legacy caller) is Track A — unchanged behavior.
        # Either dict marking "B" routes to the Track-B cap (defense in depth — risk seat nit).
        _tracks = (str(size.get("track") or "").strip().upper(),
                   str((decision or {}).get("track") or "").strip().upper())
        # Track M (QQQ Monday weekend dip) is attributed separately but sized/capped like Track A (only "B" takes
        # the Track-B exposure cap); B wins if both are present (the stricter cap).
        _track = "B" if "B" in _tracks else ("M" if "M" in _tracks else "A")
        # State-only lots (non-terminal, same-day, with NO entry_fill in the durable log at all — a failed log
        # write or the submit→log crash window) must still count against the Track-B budget (risk seat R2). A
        # lot whose entry_fill IS logged is either open (counted via open_trades) or already EXITED (a
        # "protected" state record never transitions after its stop/target fills) — never counted here (cold-2nd
        # R1). If the log cannot be read, every same-day non-terminal record counts (fail-closed over-count).
        _logged = {str(t.get("trade_id") or "") for t in open_trades.values()}
        try:
            _evs, _evs_ok = day_tier_logger.read_events_checked()
        except Exception:  # noqa: BLE001 — unreadable -> over-count (fail closed)
            _evs, _evs_ok = [], False
        if _evs_ok:
            _logged |= {str(e.get("trade_id") or "") for e in _evs if e.get("event") == "entry_fill"}
        _state_only = {
            k: {"symbol": v.get("symbol"), "track": v.get("track") or "A",
                "fill_qty": v.get("fill_qty") or v.get("qty") or 0,
                "entry_price": v.get("fill_px") or v.get("stop_px") or 0.0}
            for k, v in state.items()
            if k.startswith("entry::") and isinstance(v, dict) and _state_blocks(v)
            and str(v.get("coid") or "") not in _logged
        }
        qty, why = _bounded_entry_qty(qty, limit_px, stop_px, live_equity, open_trades, pos_by_sym,
                                      buying_power, maintenance_margin, maintenance_rate, open_orders,
                                      risk_equity=day_start_equity, symbol=symbol, track=_track,
                                      track_budget=size.get("budget"), extra_b_lots=_state_only,
                                      risk_mult=size.get("risk_mult", 1.0),
                                      max_size=size.get("max_size") is True)
        _min_q = _min_entry_qty(symbol, min_qty)
        if qty < _min_q:
            # Only a SIZE result routes to an ETF; a fail-closed zero (unreadable data / invalid limit — no "rooms=" in
            # the reason) is final (cold-2nd 2026-10-08).
            if "rooms=" in why:
                _LAST_ENTRY_SKIP[str(symbol)] = {"reason": "below_min_qty", "wired": max(0, int(qty)), "min": _min_q}
            logger.info("[%s] day-tier entry skipped — %s (minimum %d sh)", symbol, why, _min_q)
            return False
        # DAILY DOLLAR RISK BUDGET (Rafael 2026-10-04; replaces the 3-position cap). Every dollar the day tier
        # could lose today — realized losses + each open lot's loss-if-stopped + THIS entry's risk, the at-risk
        # part scaled by DAYTRADE_BUDGET_SLIPPAGE_MULT for gap-through — must fit inside the day-tier kill
        # (DAYTRADE_TIER_KILL_EQUITY_PCT x start-of-day equity). It only shrinks qty or skips (min-only).
        _kill_pct = float(_cfg("DAYTRADE_TIER_KILL_EQUITY_PCT", 0.04))  # same fallback as tier_kill_check
        _slip_mult = float(_cfg("DAYTRADE_BUDGET_SLIPPAGE_MULT", 1.2))
        _risk_ceiling = float(_cfg("DAYTRADE_PER_TRADE_RISK_EQUITY_PCT", 0.02)) * day_start_equity
        _budget = _kill_pct * day_start_equity
        _per_share = abs(limit_px - stop_px)
        if not all(math.isfinite(v) and v > 0 for v in (_kill_pct, _budget, _per_share, _risk_ceiling)) or _slip_mult < 1.0:
            logger.warning("[%s] day-tier entry aborted — daily risk budget inputs invalid (fail-closed)", symbol)
            return False
        _used = _daily_risk_used(open_trades, state, _risk_ceiling)
        if _used is None:
            logger.warning("[%s] day-tier entry aborted — realized-loss journal unreadable (fail-closed)", symbol)
            return False
        _realized_usd, _open_usd, _used_detail = _used
        _fit = _budget_fit_qty(qty, _per_share, _budget, _realized_usd, _open_usd, _slip_mult)
        if _fit < _min_q:
            _LAST_ENTRY_SKIP[str(symbol)] = {"reason": "risk_budget", "wired": max(0, int(_fit)), "min": _min_q}
            logger.info("[%s] day-tier entry skipped — daily risk budget fits %d sh (minimum %d): %s; budget $%.2f",
                        symbol, _fit, _min_q, _used_detail, _budget)
            return False
        if _fit < qty:
            why += f"; daily risk budget → {qty}→{_fit}sh ({_used_detail}, budget ${_budget:.2f})"
            qty = _fit
        logger.info("[%s] day-tier wire-time sizing — %s", symbol, why)
        size = {**size, "shares": qty, "notional": round(qty * limit_px, 2),
                "wire_cap_reason": why}

        # B3: mint the coid + WRITE the idempotency record BEFORE submit — and FAIL CLOSED if the
        # write does not persist (Finding C: a swallowed write let the same ENTER re-fire → double).
        coid = _mint_coid(symbol, direction)
        state[key] = {"bar_id": bar_id, "coid": coid, "state": "submitting", "symbol": symbol,
                      "side": direction, "ts": datetime.now(PT).isoformat(), "qty": qty, "stop_px": stop_px,
                      "track": _track}
        if not _save_state(state):
            _page(f"[{symbol}] day-tier entry ABORTED — could not persist the idempotency record "
                  f"(B3). Not submitting (a re-fire could double the position).")
            return False

        # Marketable-limit entry (cap the worst fill vs a naked market order).
        order_side = "buy" if direction == "long" else "sell"
        day_tier_logger.log_decision(decision_id or coid, symbol, decision=decision, trigger=trigger,
                                     size=size, trade_id=coid)
        from execution.tier_capital_allocator import live_admit, live_bind, live_release, live_order_id
        _capital = live_admit("daytrade", "daytrade", symbol, order_side, qty, limit_px, stop_price=stop_px)
        if not _capital.approved:
            logger.warning("[%s] day-tier entry skipped — allocator: %s", symbol, _capital.reason)
            # No order was submitted: retire the pre-submit record so it neither blocks re-entry nor counts
            # as open risk in the daily dollar budget for the rest of the day (cold-2nd 2026-10-04).
            _retire_unsubmitted(state, key)
            return False
        order = broker.submit_limit_order(
            symbol, qty, order_side, limit_px, tier="daytrade",
            client_order_id=live_order_id(_capital.lease),
        )
        if order is None:
            order = live_release(_capital.lease, "submit_none")
        elif not live_bind(_capital.lease, order):
            logger.critical("[%s] allocator bind failed after submit; preserving durable reservation", symbol)
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
        got_fill = _confirm_fill(entry_order_id, qty)
        try:
            broker.cancel_open_orders_for_symbol(symbol, only_tier="daytrade")  # stop further fills
        except Exception as _ce:  # noqa: BLE001
            logger.warning("[%s] day-tier entry remainder cancel raised: %s", symbol, _ce)
        if not _await_entry_terminal(entry_order_id):
            # The order is still live (pending cancel): shares can fill after the stop is sized to today's count.
            _page(f"[{symbol}] day-tier entry order {entry_order_id} not confirmed terminal after the cancel — the "
                  f"stop covers the shares filled so far; a late fill could be UNPROTECTED. Manual check.")
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
                                       equity_at_entry=live_equity, budget=float(size.get("budget") or 0.0),
                                       notional=round(filled_qty_i * fill_px, 2), track=_track)
        trade_logger.log_event("entry", symbol=symbol, price=fill_px, size=filled_qty_i,
                               data_source="daytrade", tier="daytrade", direction=direction,
                               trade_id=entry_coid, stop=stop_px)
        state[key].update(state="filled", fill_qty=filled_qty_i, fill_px=fill_px)
        _save_state(state)

        # Rebuild the intended target from the ACTUAL fill, then validate both exit legs around
        # that fill. META/AAPL on 2026-09-23 proved that valid pre-submit geometry can become
        # inverted after execution. Once crossed, flatten with an explicit reason: never submit an
        # immediately-marketable bracket and never keep an invalid thesis behind a plain stop.
        _stop_dist = abs(fill_px - stop_px)
        _tp_raw = trigger.get("target")
        tp_px: "float | None" = None
        if _tp_raw is not None:
            try:
                _t = float(_tp_raw)
                tp_px = _t if (math.isfinite(_t) and _t > 0) else None
            except (TypeError, ValueError):
                tp_px = None
            # A pin target the LIVE fill already passed (the price moved between signal and fill) is not a loss to
            # book: fall back to the R-multiple target below instead of an inverted bracket / flatten (2026-10-06
            # replay: META long filled 740.72 vs pin 739.23).
            if (tp_px is not None and _cfg("DAYTRADE_FADE_PIN_FALLBACK", True) is not False
                    and ((direction == "long" and tp_px <= fill_px) or (direction == "short" and tp_px >= fill_px))):
                tp_px = None   # kill flag DAYTRADE_FADE_PIN_FALLBACK=False restores the flatten-on-crossed-target path
        if tp_px is None and _stop_dist > 0 and trigger.get("no_target") is not True:
            _ride_r = float(_cfg("DAYTRADE_RIDE_TARGET_R", 2.0))
            tp_px = (fill_px + _ride_r * _stop_dist) if direction == "long" else (fill_px - _ride_r * _stop_dist)
        if trigger.get("no_target") is True:
            # Track M: no profit target by design (the right tail carries the edge) -> no OCO; the plain
            # protective stop below + the EOD force-flat are the only exits.
            tp_px = None

        geometry_ok, geometry_reason = _post_fill_exit_geometry(direction, fill_px, stop_px, tp_px)
        if not geometry_ok:
            _page(f"[{symbol}] day-tier fill INVALIDATED the setup: {geometry_reason}. "
                  f"Flattening {filled_qty_i} sh; no exit bracket will be submitted.")
            # A crossed target can still have a valid loss-side stop. Confirm that stop before the
            # scoped close begins. flatten_position cancels owned exits immediately before its
            # market reduce; if that close is unresolved, restoration below runs only when no live
            # pending-close order can race the stop and reverse the account.
            stop_geometry_ok, _ = _post_fill_exit_geometry(direction, fill_px, stop_px, None)
            emergency_stop = None
            if stop_geometry_ok:
                emergency_status, emergency_stop = _submit_verified_plain_stop(
                    symbol, filled_qty_i, direction, stop_px)
                if emergency_status == "unknown":
                    fresh = _load_state()
                    candidate = fresh.get(key)
                    rec = candidate if isinstance(candidate, dict) else dict(state[key])
                    rec["state"] = "filled"
                    rec["geometry_error"] = geometry_reason
                    fresh[key] = rec
                    _save_state(fresh)
                    _halt_unresolved_exit(
                        symbol,
                        "Emergency-stop submission outcome is unknown; no second reducer or market "
                        "close will be submitted until broker reconciliation proves the order state.",
                    )
                    return False
                if emergency_status == "live" and emergency_stop is not None:
                    emergency_id = str(getattr(emergency_stop, "id", "") or "")
                    # Bind the stop before flatten_position attempts cancellation. Explicit state
                    # identity lets recovery distinguish a still-live first stop from an absent
                    # stop; it must never submit a duplicate on an unproven cancel.
                    state[key]["stop_order_id"] = emergency_id
                    _save_state(state)
                    day_tier_logger.log_stop_placed(
                        entry_coid, symbol,
                        stop_order_id=emergency_id,
                        stop_price=stop_px,
                    )
                    # Never overlap this full-qty stop with the market close. A submitted cancel is
                    # insufficient: pending_cancel can still fill and both reducers can reverse the
                    # account. Proceed only after the broker proves the stop terminal.
                    if not broker.cancel_stop_confirmed(symbol, emergency_id):
                        fresh = _load_state()
                        candidate = fresh.get(key)
                        rec = candidate if isinstance(candidate, dict) else dict(state[key])
                        rec["state"] = "filled"
                        rec["geometry_error"] = geometry_reason
                        fresh[key] = rec
                        _save_state(fresh)
                        _halt_unresolved_exit(
                            symbol,
                            "Invalid-geometry emergency stop cancellation is unconfirmed; "
                            "scoped close deferred to prevent two full-quantity reducers.",
                        )
                        return False
                    stop_readable, stop_filled, _ = _confirmed_order_fill(
                        emergency_id, filled_qty_i)
                    if not stop_readable:
                        _halt_unresolved_exit(
                            symbol,
                            "Emergency stop is terminal but its fill quantity is unreadable; "
                            "no replacement stop or scoped close will be submitted until the exact "
                            "cumulative fill is recovered.",
                        )
                        return False
                    if stop_filled > 0:
                        target = {"trade_id": entry_coid, "symbol": symbol, "side": direction,
                                  "entry_price": fill_px, "qty": filled_qty_i,
                                  "stop_order_id": emergency_id}
                        if _record_confirmed_stop_exit(target) and _durable_exit_recorded(entry_coid):
                            fresh = _load_state()
                            candidate = fresh.get(key)
                            rec = candidate if isinstance(candidate, dict) else dict(state[key])
                            rec["state"] = "flattened_invalid_geometry"
                            rec["geometry_error"] = geometry_reason
                            fresh[key] = rec
                            _save_state(fresh)
                        else:
                            # A terminal stop can partially fill. The recorder reduces the durable
                            # owned qty; protect only that proven residual, and only if the live net
                            # still has our side/qty. Never turn a restore into an increasing order.
                            fresh = _load_state()
                            candidate = fresh.get(key)
                            rec = candidate if isinstance(candidate, dict) else dict(state[key])
                            journal_remaining = abs(int(float(
                                rec.get("fill_qty") or rec.get("qty") or filled_qty_i)))
                            # Broker-confirmed cumulative fill is an independent upper bound. If
                            # partial journaling failed, stale state must never let a replacement
                            # stop consume same-side shares belonging to another tier.
                            confirmed_remaining = max(
                                0, filled_qty_i - int(math.floor(stop_filled)))
                            remaining = min(journal_remaining, confirmed_remaining)
                            residual_status, residual_stop = ("absent", None)
                            if _live_net_covers_owned(symbol, direction, remaining):
                                residual_status, residual_stop = _submit_verified_plain_stop(
                                    symbol, remaining, direction, stop_px)
                            if residual_status == "live" and residual_stop is not None:
                                residual_id = str(getattr(residual_stop, "id", "") or "")
                                rec["stop_order_id"] = residual_id
                                day_tier_logger.log_stop_placed(
                                    entry_coid, symbol, stop_order_id=residual_id,
                                    stop_price=stop_px)
                            rec["state"] = "filled"
                            rec["geometry_error"] = geometry_reason
                            fresh[key] = rec
                            _save_state(fresh)
                            _halt_unresolved_exit(
                                symbol,
                                "Emergency stop filled but the exact exit is incomplete; residual "
                                f"protection={residual_status}.",
                            )
                        return False
                    state[key]["stop_order_id"] = ""
                    _save_state(state)
            flat_ok = flatten_position(symbol, filled_qty_i, direction, entry_price=fill_px,
                                       trade_id=entry_coid, order_id_hint=entry_order_id,
                                       reason="fill_invalidated_setup")
            # True can also mean the position endpoint said "already absent". Only the exact
            # trade's durable exit_fill proves a terminal, booked close.
            exit_proven = flat_ok and _durable_exit_recorded(entry_coid)
            if exit_proven:
                # A close plus a surviving reduce-only stop can reverse the account later. Retry
                # explicit cancellation, then require a readable proof that no DT stop remains.
                _cancel_daytrade_exit_legs(symbol)
            stop_state = _has_live_daytrade_stop(symbol)
            fresh = _load_state()  # flatten may have persisted partial/pending-close fields
            candidate = fresh.get(key)
            rec = candidate if isinstance(candidate, dict) else dict(state[key])
            remaining_qty = abs(int(float(rec.get("fill_qty") or rec.get("qty") or filled_qty_i)))
            rec["geometry_error"] = geometry_reason
            if exit_proven and stop_state is False:
                rec["state"] = "flattened_invalid_geometry"
                fresh[key] = rec
                _save_state(fresh)
                return False

            pending_close = str(rec.get("pending_exit_order_id") or "")
            restored_status, restored = ("absent", None)
            if (stop_geometry_ok and remaining_qty >= 1 and not pending_close
                    and stop_state is False
                    and _live_net_covers_owned(symbol, direction, remaining_qty)):
                restored_status, restored = _submit_verified_plain_stop(
                    symbol, remaining_qty, direction, stop_px)
                if restored_status == "live" and restored is not None:
                    restored_id = str(getattr(restored, "id", "") or "")
                    day_tier_logger.log_stop_placed(
                        entry_coid, symbol, stop_order_id=restored_id, stop_price=stop_px)
                    rec["stop_order_id"] = restored_id
            # Stay nonterminal so reconcile_open_state owns the next attempt. Halt all new Day Tier
            # entries until exact exit ownership/P&L is recovered.
            rec["state"] = "filled"
            fresh[key] = rec
            _save_state(fresh)
            protection = "restored stop" if restored_status == "live" else (
                "pending close order" if pending_close else (
                    "original stop still live" if stop_state is True else
                    "stop state UNKNOWN" if stop_state is None else
                    "stop submission UNKNOWN" if restored_status == "unknown" else
                    "NO CONFIRMED PROTECTION"))
            _halt_unresolved_exit(
                symbol,
                f"Invalid post-fill geometry close is unresolved ({protection}); exact exit fill "
                "must be recovered before entries resume.",
            )
            return False

        # B2 (bracket-exit build): place a broker-native OCO exit pair (take-profit LIMIT +
        # protective MARKET stop, one-cancels-other) sized to the ACTUAL filled qty, so the tier
        # HARVESTS the target instead of only stopping out / EOD-flatting (the v1 gap: it computed
        # the pin but never placed an order to take it). If the OCO can't be placed for any reason,
        # FALL BACK to the plain protective stop (today's verified path) — degrade to stop-only,
        # NEVER to no protection.
        stop_side = "sell" if direction == "long" else "buy"
        # Take-profit: FADE → the GEX pin (trigger['target'], already validated profit-side by
        # _compute_stop_price which aborts a loss-side fade). RIDE → target is None, so use an
        # R-multiple of the actual entry→stop distance on the profit side (DAYTRADE_RIDE_TARGET_R).
        oco = None
        if tp_px is not None and _stop_dist > 0:
            try:
                oco = broker.submit_oco_exit(symbol, filled_qty_i, direction, tp_px, stop_px, tier="daytrade")
            except Exception as e:  # noqa: BLE001 — a transient raise falls through to the plain-stop fallback
                logger.warning("[%s] day-tier OCO exit raised: %s — falling back to a plain stop", symbol, e)
                oco = None
        if oco is not None and getattr(oco, "id", None):
            # Record the exit-leg order-ids explicitly — Alpaca OCO child legs do NOT carry the DT-
            # coid, so reconcile/flatten track & cancel them by id, not via tier_of_coid. The OCO
            # PARENT is the take-profit; the STOP is the child leg (see _oco_leg_ids).
            stop_leg_id, tp_leg_id, _oco_id = _oco_leg_ids(oco)
            _tp_num = float(tp_px) if tp_px is not None else 0.0  # oco is set only when tp_px is not None
            # Mark protected BEFORE logging (a log raise must never leave protected=False and trigger
            # a false-flatten of a genuinely-live OCO — cold-2nd T5).
            state[key].update(state="protected", stop_order_id=stop_leg_id,
                              tp_order_id=tp_leg_id, oco_order_id=_oco_id)
            _save_state(state)
            protected = True
            day_tier_logger.log_stop_placed(entry_coid, symbol, stop_order_id=(stop_leg_id or _oco_id),
                                            stop_price=stop_px)
            try:
                day_tier_logger.log_target_placed(entry_coid, symbol, tp_order_id=tp_leg_id,
                                                  target_price=round(_tp_num, 2))
            except Exception:  # noqa: BLE001 — target logging is best-effort; protection gates safety
                pass
            logger.info("[%s] day-tier PROTECTED (OCO): %d sh @ fill %.2f, stop %.2f, target %.2f",
                        symbol, filled_qty_i, fill_px, stop_px, _tp_num)

        if not protected:
            # OCO unavailable/rejected/inverted-geometry → fall back to the plain protective stop
            # (verified v1 path: submit, VERIFY live, retry cancelling the prior stop each time, else
            # flatten). No harvest, but the position is never left naked (cold-2nd Threat 1).
            logger.info("[%s] day-tier OCO exit unavailable — falling back to a plain protective stop", symbol)
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
                    state[key].update(state="protected", stop_order_id=str(getattr(stop_obj, "id", "")))
                    _save_state(state)
                    protected = True
                    day_tier_logger.log_stop_placed(entry_coid, symbol, stop_order_id=str(getattr(stop_obj, "id", "")),
                                                    stop_price=stop_px)
                    logger.info("[%s] day-tier PROTECTED (plain stop): %d sh @ fill %.2f, stop %.2f (attempt %d)",
                                symbol, filled_qty_i, fill_px, stop_px, attempt + 1)
                    break
                if attempt < retries:
                    logger.warning("[%s] day-tier stop not confirmed (attempt %d/%d) — retrying in %.1fs",
                                   symbol, attempt + 1, retries + 1, wait)
                    time.sleep(wait)

        if not protected:
            _page(f"[{symbol}] day-tier protection UNCONFIRMED (OCO + plain-stop fallback both failed) — "
                  f"flattening the {filled_qty_i}-sh day-tier position to avoid a naked ride.")
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


# ── tier-kill ────────────────────────────────────────────────────────────────────────────────
def _realized_loss_today() -> tuple[float, bool]:
    """Loss-only realized floor from durable day-tier exits; gains cannot mask a later loss."""
    from strategy import day_tier_logger
    events, readable = day_tier_logger.read_events_checked()
    if not readable:
        return 0.0, False
    total = 0.0
    today = _now_et().date()
    try:
        for ev in events:
            if ev.get("event") not in ("exit_fill", "partial_exit_fill"):
                continue
            ts = datetime.fromisoformat(str(ev.get("ts") or ""))
            if ts.astimezone(ET).date() != today:
                continue
            pnl_raw = ev.get("realized_pnl")
            if pnl_raw is None:
                return 0.0, False
            pnl = float(pnl_raw)
            if not math.isfinite(pnl):
                return 0.0, False
            total += min(0.0, pnl)
        return total, True
    except Exception:
        return 0.0, False


def tier_kill_check(equity: float, day_start_equity: float | None = None) -> bool:
    """If cumulative realized losses plus OPEN unrealized P&L breach the tier loss budget,
    force-flat the whole tier and mark it killed for the day. Reads the LIVE book. An unreadable
    quote FAILS CLOSED (masked-loss Finding B): it falls back to Alpaca's own unrealized_pl for the
    lot (prorated by the day-tier's share) and PAGES if even that is unavailable — never silently
    dropping a lot from the loss sum. No-op when disabled."""
    if not _enabled():
        return False
    from execution import broker
    try:
        state = _load_state()
        today_key = f"{_now_et():%Y%m%d}"
        if state.get(_KILL_KEY) == today_key:
            # A kill latches entry permission, but liquidation is not one-shot: retry every tick
            # until no owned target remains. A transient close failure must not become permanent.
            residual = _flatten_targets()
            if residual:
                closed = force_flat_all(reason="tier_kill_retry")
                if closed < len(residual):
                    _page(f"DAY-TIER KILL remains active: flattened {closed}/{len(residual)} residual "
                          "position(s); retrying next tick.")
            return True
        if state.get(_HALT_KEY) == today_key:
            return True
        # Kill re-based to a DIRECT fraction of EQUITY (2026-09-08 BP sizing) — the old
        # −25%×(equity×alloc) ≈ −$94 basis is a ~1.4% move = intraday noise once positions are BP-sized.
        # Account-terms tier kill; validate_config asserts it stays < the 7% paper account kill.
        kill_equity_pct = float(_cfg("DAYTRADE_TIER_KILL_EQUITY_PCT", 0.04))  # PROV:daytier-bp-2026-09-08
        baseline_raw = day_start_equity if day_start_equity is not None else equity
        baseline = float(baseline_raw)
        if not (math.isfinite(baseline) and baseline > 0):
            state = _load_state()
            state[_HALT_KEY] = f"{_now_et():%Y%m%d}"
            _save_state(state)
            _page("DAY-TIER tier-kill baseline unreadable — halting new entries for the day (fail-closed).")
            return True
        kill_threshold = kill_equity_pct * baseline
        realized_loss, realized_readable = _realized_loss_today()
        if not realized_readable:
            state = _load_state()
            state[_HALT_KEY] = f"{_now_et():%Y%m%d}"
            _save_state(state)
            _page("DAY-TIER realized-loss log unreadable — halting new entries for the day (fail-closed); "
                  "existing protected positions remain managed.")
            return True
        targets = _flatten_targets()
        if realized_loss <= -abs(kill_threshold):
            closed = force_flat_all(reason="tier_kill_realized") if targets else 0
            state = _load_state()
            state[_KILL_KEY] = today_key
            _save_state(state)
            if targets and closed < len(targets):
                _page(f"DAY-TIER KILL from realized losses: flattened {closed}/{len(targets)} position(s); "
                      "residual liquidation will retry next tick.")
            return True
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
            if all(math.isfinite(v) and v > 0 for v in (cur, ent, q)):
                upl += (cur - ent) * q if side == "long" else (ent - cur) * q
                continue
            # Quote unreadable → fall back to Alpaca's own unrealized_pl, prorated to our share.
            try:
                pos_upl = float(getattr(pos, "unrealized_pl", 0.0) or 0.0)
                pos_qty = abs(float(getattr(pos, "qty", 0.0) or 0.0))
                if (math.isfinite(pos_upl) and math.isfinite(pos_qty) and math.isfinite(q)
                        and pos_qty > 0 and q > 0):
                    upl += pos_upl * min(1.0, q / pos_qty)
                    continue
            except Exception:
                pass
            blind = True  # could not evaluate this lot's P&L at all
        if blind:
            _page("DAY-TIER tier-kill check is BLIND on ≥1 lot (no quote, no unrealized_pl) — "
                  "halting new entries for the day; protected positions remain managed.")
            state = _load_state()
            state[_HALT_KEY] = f"{_now_et():%Y%m%d}"
            _save_state(state)
            return True
        loss_measure = realized_loss + upl
        if loss_measure <= -abs(kill_threshold):
            _page(f"DAY-TIER KILL: realized-loss floor {realized_loss:.2f} + open unrealized {upl:.2f} "
                  f"= {loss_measure:.2f} ≤ −{kill_equity_pct:.0%} of SOD equity ${baseline:.2f} "
                  f"(−${abs(kill_threshold):.2f}) — force-flattening the tier for the day.")
            force_flat_all(reason="tier_kill")
            state = _load_state()
            state[_KILL_KEY] = today_key
            _save_state(state)
            return True
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("day-tier tier_kill_check error: %s", e)
        return False


# ── per-tick reconcile (the board-named go-live gate for the cron runner) ────────────────────────
def _has_live_daytrade_stop(symbol: str) -> "bool | None":
    """True if live day-tier downside protection rests on `symbol`; False if none; None if the order
    book is UNREADABLE (the caller treats None as 'cannot confirm' — fail-safe: never flatten a
    possibly-protected position on a transient read failure). Recognizes BOTH protection forms:
      (a) a DT-tagged STOP order — the plain-stop fallback path (coid parses to 'daytrade'); and
      (b) an OCO exit leg whose order id matches a recorded OCO/stop id in the state file — the OCO
          harvest path (Alpaca child legs do NOT carry the DT- coid, so they are matched by id; an
          OCO is both-or-neither, so any live leg means the protective stop leg is live).
    A resting DT ENTRY (limit) order is not protection and does NOT count."""
    from execution import broker
    from execution.ownership_guard import tier_of_coid
    try:
        orders = broker.get_open_orders(symbol)
    except Exception:
        return None
    if orders is None:
        return None
    # Recorded OCO leg / parent ids for this symbol (matched by id — the OCO legs lack the DT- coid).
    recorded_ids: set = set()
    try:
        st = _load_state()
        for k, v in st.items():
            if k.startswith("entry::") and isinstance(v, dict) and v.get("symbol") == symbol:
                for _f in ("stop_order_id", "oco_order_id", "tp_order_id"):
                    _id = str(v.get(_f) or "")
                    if _id:
                        recorded_ids.add(_id)
    except Exception:  # noqa: BLE001 — state unreadable → fall back to coid-only detection below
        recorded_ids = set()
    for o in orders:
        try:
            oid = str(getattr(o, "id", "") or "")
            if oid and oid in recorded_ids:
                return True  # (b) a live recorded OCO leg = active protection
            if tier_of_coid(getattr(o, "client_order_id", None)) != "daytrade":
                continue
            otype = str(getattr(o, "order_type", None) or getattr(o, "type", "") or "").lower()
            if "stop" in otype:
                return True  # (a) DT-tagged plain stop
        except Exception:
            continue
    return False


# Slack names — the four tier names (tier_names: Day / Swing / QHM / F6, CEO 2026-10-07)
_TIER_DISPLAY = {k: f"{tier_label(k)} tier" for k in ("intraday", "qhm", "forever6")}


def _foreign_stop_covers(symbol: str, side: str, qty: int, tiers_out: "list | None" = None) -> bool:
    """True when ANOTHER tier's live STOP order on `symbol` reduces our `side` for at least `qty` shares — the day
    tier's lot is protected, but by a stop it does not own (2026-10-07: the main bot's orphan scan adopted the
    day-tier EWY short and AAPL long, cancelled the day-tier OCO and placed its own IN- stops; the day tier then
    saw 'naked', its scoped close was refused (shares held by the foreign stop) and it paged every tick). False on
    any read error (the caller keeps today's behaviour). Never raises."""
    from execution import broker
    from execution.ownership_guard import tier_of_coid
    try:
        orders = broker.get_open_orders(symbol)
        if not orders or qty < 1:
            return False
        # Our OWN OCO legs carry no DT- coid (Alpaca child legs) — exclude them by their recorded ids, exactly as
        # _has_live_daytrade_stop does (cold-2nd 2026-10-07: otherwise our own stop leg reads as "foreign").
        own_ids: set = set()
        for k, v in _load_state().items():
            if k.startswith("entry::") and isinstance(v, dict) and v.get("symbol") == symbol:
                for _f in ("stop_order_id", "oco_order_id", "tp_order_id"):
                    if v.get(_f):
                        own_ids.add(str(v.get(_f)))
        want_side = "sell" if side == "long" else "buy"
        covered = 0.0
        for o in orders:
            # FOREIGN only when the tag POSITIVELY names another tier (IN-/QH-/F6-). An untagged order — our own
            # OCO child legs carry Alpaca ids, and an unreadable state file leaves own_ids empty — never counts
            # (cold-2nd 2026-10-07: an empty state would otherwise turn our own leg into "foreign" cover).
            _tier = tier_of_coid(getattr(o, "client_order_id", None))
            if _tier is None or _tier == "daytrade" or str(getattr(o, "id", "") or "") in own_ids:
                continue
            otype = _enum_text(getattr(o, "order_type", None) or getattr(o, "type", None))
            if "stop" not in otype or _enum_text(getattr(o, "side", None)) != want_side:
                continue
            if tiers_out is not None and _tier not in tiers_out:
                tiers_out.append(_tier)
            covered += abs(float(getattr(o, "qty", 0) or 0)) - abs(float(getattr(o, "filled_qty", 0) or 0))
        return covered + 1e-9 >= qty
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] foreign-stop check failed: %s", symbol, e)
        return False


def _page_once_today(symbol: str, key: str, msg: str) -> None:
    """Page at most once per symbol per ET day for `key` (state-file marker); the log still records every tick."""
    try:
        state = _load_state()
        k = f"_paged::{key}::{symbol}"
        today = f"{_now_et():%Y%m%d}"
        if state.get(k) == today:
            logger.warning(msg)
            return
        state[k] = today
        _save_state(state)
    except Exception as e:  # noqa: BLE001 — a dedupe failure pages (never silences)
        logger.debug("page dedupe failed: %s", e)
    _page(msg)


def _tier_names(tiers: list) -> str:
    return ", ".join(_TIER_DISPLAY.get(t, t) for t in tiers) or "another tier"


def _record_transfer(tgt: dict, tiers: list, held: int, want: int, mark: float) -> bool:
    """The SWING tier (intraday tag) has taken over this day-tier lot (its stop now protects it — 2026-10-07 EWY/AAPL).
    When the lot is the WHOLE position (held == want), the day tier's trade ends at the take-over: book an exit at the
    live Alpaca mark (`mark` = the position's current_price), exit_reason 'transferred_to_swing_tier', realized P&L
    from that mark (a loss is booked, never masked; it counts in the day tier's own loss journal today), and retire
    the lot. From then on the swing tier owns the position and books its own result — no later fill matching (two
    cold-2nd FAILs showed another tier's or a later trade's fill could be mis-booked). A co-held symbol (held !=
    want) or a non-swing adopter is NOT transferred (the caller keeps the protected/halt path). False on any
    failure (the caller retries next tick). Never raises."""
    from strategy import day_tier_logger
    try:
        trade_id = str(tgt.get("trade_id") or "")
        sym = str(tgt.get("symbol") or "")
        side = str(tgt.get("side") or "long")
        entry = abs(float(tgt.get("entry_price") or 0.0))
        m = float(mark)
        if ("intraday" not in tiers or held != want or want < 1 or not trade_id or not sym
                or not (entry > 0 and math.isfinite(m) and m > 0)):
            return False
        if _durable_exit_recorded(trade_id):
            # Already booked (a prior tick's retire save failed): only retry the retire — never a second exit_fill.
            _mark_symbol_flattened(sym)
            return True
        realized = round((m - entry) * want if side == "long" else (entry - m) * want, 2)
        if not day_tier_logger.log_exit_fill(trade_id, sym, order_id="", exit_reason="transferred_to_swing_tier",
                                             fill_price=round(m, 4), fill_qty=float(want),
                                             market_price_at_exit=round(m, 4), realized_pnl=realized):
            return False
        _mark_symbol_flattened(sym)
        _page_once_today(sym, "transferred",
                         f"[{sym}] Day-tier lot ({want} sh {side}) was taken over by the Swing tier — the Day-tier "
                         f"trade is closed in its journal at the take-over MARK ${m:.2f} (realized ${realized:+.2f}; a "
                         f"mark, not a broker fill). The Swing tier now holds the shares at the original entry "
                         f"${entry:.2f}, so the move to the mark appears in both tiers' records; the account P&L "
                         f"(Alpaca fills) counts it once.")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[%s] day-tier transfer booking failed: %s", tgt.get("symbol"), e)
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


def _record_confirmed_stop_exit(target: dict) -> "bool | None":
    """Heal a broker-filled stop. True=recorded, False=no fill, None=unresolved/partial."""
    from strategy import day_tier_logger
    import trade_logger
    trade_id = str(target.get("trade_id") or "")
    if not trade_id:
        return False
    events, readable = day_tier_logger.read_events_checked(trade_id)
    if not readable:
        return None
    if any(e.get("event") == "exit_fill" for e in events):
        return True
    # Exit-leg candidates: (order_id, exit_reason). Stop legs → "protective_stop"; OCO take-profit
    # legs → "take_profit". With OCO a WINNER harvests via the TP leg and the stop auto-cancels, so
    # the heal MUST poll the TP leg too — else a TP-closed winner is misrecorded as unresolved (and
    # its P&L never booked). TP-leg ids come from the durable 'target_placed' events (reliable even
    # when the reconcile target dict lacks them).
    stop_cands: list = []
    tp_cands: list = []
    for e in events:
        if e.get("event") == "stop_placed":
            _sid = str(e.get("stop_order_id") or "")
            if _sid:
                stop_cands.append((_sid, "protective_stop"))
        elif e.get("event") == "target_placed":
            _tid = str(e.get("tp_order_id") or "")
            if _tid:
                tp_cands.append((_tid, "take_profit"))
    _state_stop = str(target.get("stop_order_id") or "")
    if _state_stop:
        stop_cands.append((_state_stop, "protective_stop"))
    _state_tp = str(target.get("tp_order_id") or "")
    if _state_tp:
        tp_cands.append((_state_tp, "take_profit"))
    expected = int(target.get("qty") or 0)
    if expected < 1:
        return None
    # STOPS FIRST (loss-preferring, masked-loss seat): if a broker OCO ever fills BOTH legs (sibling-
    # cancel loses a fast-market race), book the LOSS, never the gain. Most-recent within each class.
    # TP legs are polled only after every stop shows no fill — so a normal TP-harvest (the stop leg
    # auto-cancelled → 0 fill) still books take_profit.
    ordered = list(reversed(stop_cands)) + list(reversed(tp_cands))
    ordered = [(i, r) for i, r in ordered if i]
    if not ordered:
        return False
    any_readable = False
    any_unreadable = False
    for stop_id, _reason in ordered:
        order_readable, qty, price = _confirmed_order_fill(stop_id, expected)
        any_readable = any_readable or order_readable
        any_unreadable = any_unreadable or not order_readable
        # Alpaca reports filled_qty cumulatively. Durable partial-exit rows are the
        # watermark: only an unrecorded delta may reduce day-tier ownership.
        try:
            accounted = sum(
                abs(float(e.get("fill_qty") or 0.0)) for e in events
                if e.get("event") == "partial_exit_fill" and str(e.get("order_id") or "") == stop_id
            )
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(qty) and math.isfinite(accounted)) or qty + 1e-9 < accounted:
            return None
        delta = qty - accounted
        if delta > 1e-9:  # PROV:daytier-bp-2026-09-08 — floating fill-quantity tolerance
            partial_qty = int(math.floor(delta))
            if partial_qty < 1 or price <= 0:
                return None
            if partial_qty + 1e-9 < expected:
                if _record_partial_exit(target, stop_id, partial_qty, price, price,
                                        f"{_reason}_partial"):
                    return False
                return None
            entry = abs(float(target.get("entry_price") or 0.0))
            side = str(target.get("side") or "long")
            if not (math.isfinite(entry) and entry > 0):
                return None
            realized = round((price - entry) * expected if side == "long" else (entry - price) * expected, 2)
            if not day_tier_logger.log_exit_fill(
                trade_id, str(target.get("symbol") or ""), order_id=stop_id,
                exit_reason=_reason, fill_price=price, fill_qty=float(expected),
                market_price_at_exit=price, realized_pnl=realized,
            ):
                return None
            try:
                trade_logger.log_event("exit", symbol=str(target.get("symbol") or ""), price=price,
                                       size=expected, data_source="daytrade", tier="daytrade",
                                       exit_reason=_reason, trade_id=trade_id,
                                       realized_pnl=realized)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] day-tier exit trade_logger write failed: %s", target.get("symbol"), e)
            return True
        # qty is cumulative. When delta==0 this exact fill is already journaled; it cannot close
        # the current residual merely because the old cumulative qty equals/exceeds residual qty.
        continue
    return False if any_readable and not any_unreadable else None


def _halt_unresolved_exit(symbol: str, detail: str) -> None:
    """Persist a no-entry halt and page once per symbol/day until exit P&L is reconciled."""
    state = _load_state()
    today = f"{_now_et():%Y%m%d}"
    page_key = f"_unresolved_exit::{symbol}"
    first = state.get(page_key) != today
    state[_HALT_KEY] = today
    state[page_key] = today
    _save_state(state)
    if first:
        _page(f"[{symbol}] day-tier exit is unresolved — halting new entries for the day. {detail}")


def _retire_trade_record(trade_id: str) -> None:
    """Retire ONLY the state record(s) of this exact trade (coid == trade_id) to 'flattened_no_stop' — never another
    lot on the same symbol (a re-entry, or a 'submitted' crash-window record). Never raises."""
    if not trade_id:
        return
    try:
        st = _load_state()
        changed = False
        for k, v in st.items():
            if (k.startswith("entry::") and isinstance(v, dict) and v.get("coid") == trade_id
                    and v.get("state") in ("submitted", "filled", "protected", "fill_unverified")):
                v["state"] = "flattened_no_stop"
                changed = True
        if changed:
            _save_state(st)
    except Exception as e:  # noqa: BLE001
        logger.debug("_retire_trade_record(%s) failed: %s", trade_id, e)


def _closed_in_log(trade_id: str) -> bool:
    """True when the durable log shows this trade entered and is no longer open (an exit_fill, or partial exits that
    add up to the whole lot). False on any doubt. Never raises."""
    if not trade_id:
        return False
    try:
        from strategy import day_tier_logger
        events, readable = day_tier_logger.read_events_checked(trade_id)
        if not readable or not any(e.get("event") == "entry_fill" for e in events):
            return False
        return trade_id not in day_tier_logger.open_trades_from_log()
    except Exception:  # noqa: BLE001
        return False


def reconcile_open_state(allow_flatten: bool = True) -> dict:
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
    order for the symbol (so it can't fill mid-next-bar) + mark the record terminal.

    allow_flatten=False (market closed — CEO 2026-10-07): a naked lot is NOT market-closed here (Alpaca would queue
    a market order for the next open); after_hours_exit closes it with an extended-hours limit."""
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
                if sym not in submitted and _closed_in_log(str(tgt.get("trade_id") or "")):
                    # Checked FIRST: a booked-closed trade never gets another (partial) fill booked (cold-2nd 2026-10-07).
                    summary["cleared"] += 1
                    _retire_trade_record(str(tgt.get("trade_id") or ""))
                    continue
                if sym not in submitted and _resolve_pending_exit(tgt):
                    # A recorded exit order (EOD market close / after-hours limit) closed it: its fills are booked.
                    summary["cleared"] += 1
                    continue
                if sym in submitted:
                    _page(f"[{sym}] day-tier reconcile: 'submitted' order filled {want} but position "
                          f"absent (endpoint lag) — NOT retiring; next tick reconciles.")
                elif tgt.get("geometry_error"):
                    # A journaled market close can coexist with a stale emergency stop after a
                    # cancel failure. Never terminalize while that reducer is live or unreadable.
                    cleared = _cancel_recorded_exit_legs_confirmed(sym)
                    stale_stop = _has_live_daytrade_stop(sym)
                    if cleared is not True or stale_stop is not False:
                        summary["unreadable"] += 1
                        _halt_unresolved_exit(
                            sym,
                            "Position is flat after invalid geometry but a recorded exit order is "
                            "still live or unreadable; terminalization awaits confirmed cancellation.",
                        )
                    elif _durable_exit_recorded(str(tgt.get("trade_id") or "")):
                        summary["cleared"] += 1
                        _mark_symbol_flattened(sym)
                    else:
                        _halt_unresolved_exit(
                            sym,
                            "Position is flat after invalid geometry but the exact durable exit is missing.",
                        )
                elif _record_confirmed_stop_exit(tgt):
                    summary["cleared"] += 1
                    _mark_symbol_flattened(sym)
                    logger.info("[%s] day-tier reconcile: recorded broker-confirmed protective-stop exit", sym)
                else:
                    _halt_unresolved_exit(
                        sym,
                        "Position is absent but no complete broker-confirmed protective-stop fill "
                        "could be recovered; realized loss cannot be bounded safely.",
                    )
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
            exit_state = _record_confirmed_stop_exit(tgt)
            if exit_state is True:
                summary["cleared"] += 1
                _mark_symbol_flattened(sym)
                logger.info("[%s] day-tier reconcile: recorded filled stop; remaining net belongs to another tier", sym)
                continue
            if exit_state is None:
                summary["unreadable"] += 1
                _halt_unresolved_exit(
                    sym,
                    "No live protective stop remains, but its terminal fill is unreadable or partial; "
                    "refusing to close an unattributable cross-tier quantity.",
                )
                continue
            want = int(tgt.get("qty") or 0)
            held = abs(int(float(getattr(pos, "qty", 0) or 0)))
            # The foreign stop must cover the WHOLE live position (cold-2nd 2026-10-07): once our stop is gone every
            # remaining stop belongs to another tier, and on a co-held symbol a stop sized to ITS shares would
            # otherwise "cover" our naked lot.
            _ft: list = []
            if want > 0 and _foreign_stop_covers(sym, str(tgt.get("side") or "long"), held, _ft):
                # Protected by ANOTHER tier's stop (that tier adopted the lot): not naked. A scoped close would be
                # refused (the foreign stop holds the shares) and must never cancel another tier's order (B1).
                if _record_transfer(tgt, _ft, held, want, abs(float(getattr(pos, "current_price", 0) or 0))):
                    summary["cleared"] += 1   # the day-tier trade ended at the take-over; the swing tier owns it now
                    continue
                summary["protected"] += 1
                _page_once_today(sym, "foreign_stop",
                                 f"[{sym}] day-tier lot ({want} sh {tgt.get('side')}) was taken over by the "
                                 f"{_tier_names(_ft)}, whose stop protects it — the {_tier_names(_ft)} manages it now.")
                continue
            if not allow_flatten:
                # Market closed: after_hours_exit owns this lot (extended-hours limit). Never a market order here.
                summary["deferred_after_hours"] = summary.get("deferred_after_hours", 0) + 1
                continue
            # NAKED (no live DT stop) → scoped-flatten the day-tier's OWN CONFIRMED qty. flatten_position's
            # net-side/qty guard additionally protects any co-held tier.
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
