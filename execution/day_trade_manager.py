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
                       symbol: str = "") -> tuple[int, str]:
    """Clamp an entry to every live account/day-tier/risk budget. All bad inputs fail closed.

    `symbol` selects the DEEP-LIQUIDITY carve-out (aggression guardrail 2026-09-18): a deep-liquidity
    (Mag-7) name may use the full DAYTRADE_TRACK_A_EQUITY_CEILING_PCT; a non-deep (thin) name keeps the
    base ceiling AND a per-name notional cap. An unknown/empty symbol is treated as NON-deep (conservative)."""
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
        why = (f"requested {requested_qty} → risk-basis {risk_qty}sh "
               f"(risk {risk_pct:.2%}×${risk_basis:.0f}/stop ${stop_distance:.4f}) → wired {safe_qty}; rooms="
               + ",".join(f"{k}:${v:.2f}" for k, v in rooms.items())
               + f"; notional cap={notional_qty}sh")
        return safe_qty, why
    except Exception as e:
        return 0, f"entry-cap error (fail-closed): {e!r}"


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
            try:
                qty = float(getattr(order, "filled_qty", 0) or 0)
                price = float(getattr(order, "filled_avg_price", 0) or 0)
                if math.isfinite(qty) and math.isfinite(price) and qty > 0 and price > 0:
                    latest = (qty, price)
                    if qty + 1e-9 >= expected_qty:
                        return True, *latest
                status = _enum_text(getattr(order, "status", None))
                if status in ("canceled", "expired", "rejected", "done_for_day"):
                    return True, *latest
            except (TypeError, ValueError):
                pass
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
                value["pending_exit_accounted_qty"] = float(value.get("pending_exit_accounted_qty") or 0.0) + fill_qty
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


def _set_pending_exit(trade_id: str, order_id: str, requested_qty: int) -> bool:
    """Persist a close order before polling it, so a later fill cannot be retried blind."""
    if not trade_id or not order_id or requested_qty < 1:
        return False
    state = _load_state()
    for key, value in state.items():
        if key.startswith("entry::") and isinstance(value, dict) and value.get("coid") == trade_id:
            value["pending_exit_order_id"] = order_id
            value["pending_exit_requested_qty"] = requested_qty
            value["pending_exit_accounted_qty"] = 0.0
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
            _save_state(state)
            return


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
                                    "order_id": str(v.get("order_id") or ""),
                                    "stop_order_id": str(v.get("stop_order_id") or "")}
                elif sym and sym in targets and not targets[sym].get("stop_order_id"):
                    targets[sym]["stop_order_id"] = str(v.get("stop_order_id") or "")
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

        # CONCURRENCY CAP (aggression guardrail 2026-09-18): bound concurrent day-tier positions so
        # MAX_CONCURRENT × per-trade-risk <= the tier kill (self-bounding correlated tail; validate_config
        # asserts it). Count DISTINCT active day-tier symbols (open log ∪ same-day non-terminal state);
        # this symbol is NOT among them (same-symbol re-entry was blocked just above). Skip if at the cap.
        _active_syms = {str(t.get("symbol") or "") for t in open_trades.values() if t.get("symbol")}
        _active_syms |= {str(v.get("symbol") or "") for k, v in state.items()
                         if k.startswith("entry::") and isinstance(v, dict) and v.get("symbol") and _state_blocks(v)}
        _active_syms.discard(symbol)
        _max_conc = int(_cfg("DAYTRADE_MAX_CONCURRENT_POSITIONS", 3))
        if len(_active_syms) >= _max_conc:
            logger.info("[%s] day-tier entry skipped — concurrency cap %d reached (%d active: %s)",
                        symbol, _max_conc, len(_active_syms), sorted(_active_syms))
            return False

        # Structural stop FIRST (never enter a position we can't protect — B2 precondition).
        stop_px = _compute_stop_price(trigger, direction, entry_ref)
        if stop_px is None:
            logger.warning("[%s] day-tier entry aborted — no sane structural stop", symbol)
            return False

        # Bound against the actual marketable-limit price, not the signal reference. This closes
        # the long-side +slippage boundary breach where 15×$100 passed a $1,500 cap but the submitted
        # 15×$100.20 order reserved $1,503.
        slip = float(_cfg("DAYTRADE_ENTRY_SLIPPAGE_PCT", 0.002))
        limit_px = round(entry_ref * (1.0 + slip) if direction == "long" else entry_ref * (1.0 - slip), 2)
        if not (math.isfinite(limit_px) and limit_px > 0):
            logger.warning("[%s] day-tier entry aborted — invalid marketable-limit price", symbol)
            return False

        # MIN-STOP-DISTANCE GATE (hairpin fix Part A — 2026-09-18). A structural stop inside the
        # volatility/noise band = a "no-room" trade a random tick stops out for pennies. Require the
        # entry→stop distance >= max(k×ATR(5m), spread_mult×live_spread); if the pin/wall is closer,
        # SKIP (NEVER widen past the pin — that breaks the setup's logic). Fail-closed on invalid
        # ATR / broken quote. Runs before the heavier live-book reads so a no-room setup skips cheaply.
        room_ok, room_why = _min_stop_room_ok(symbol, direction, limit_px, stop_px)
        if not room_ok:
            logger.info("[%s] day-tier entry skipped — %s", symbol, room_why)
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

        qty, why = _bounded_entry_qty(qty, limit_px, stop_px, live_equity, open_trades, pos_by_sym,
                                      buying_power, maintenance_margin, maintenance_rate, open_orders,
                                      risk_equity=day_start_equity, symbol=symbol)
        if qty < 1:
            logger.info("[%s] day-tier entry skipped — %s", symbol, why)
            return False
        logger.info("[%s] day-tier wire-time sizing — %s", symbol, why)
        size = {**size, "shares": qty, "notional": round(qty * limit_px, 2),
                "wire_cap_reason": why}

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
        order_side = "buy" if direction == "long" else "sell"
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
                                       equity_at_entry=live_equity, budget=float(size.get("budget") or 0.0),
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
                state[key].update(state="protected", stop_order_id=str(getattr(stop_obj, "id", "")))
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
    stop_ids = [str(e.get("stop_order_id") or "") for e in events if e.get("event") == "stop_placed"]
    state_stop_id = str(target.get("stop_order_id") or "")
    if state_stop_id:
        stop_ids.append(state_stop_id)
    expected = int(target.get("qty") or 0)
    if expected < 1:
        return None
    if not stop_ids:
        return False
    any_readable = False
    any_unreadable = False
    for stop_id in reversed([s for s in stop_ids if s]):
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
                                        "protective_stop_partial"):
                    return False
                return None
            entry = abs(float(target.get("entry_price") or 0.0))
            side = str(target.get("side") or "long")
            if not (math.isfinite(entry) and entry > 0):
                return None
            realized = round((price - entry) * expected if side == "long" else (entry - price) * expected, 2)
            if not day_tier_logger.log_exit_fill(
                trade_id, str(target.get("symbol") or ""), order_id=stop_id,
                exit_reason="protective_stop", fill_price=price, fill_qty=float(expected),
                market_price_at_exit=price, realized_pnl=realized,
            ):
                return None
            try:
                trade_logger.log_event("exit", symbol=str(target.get("symbol") or ""), price=price,
                                       size=expected, data_source="daytrade", tier="daytrade",
                                       exit_reason="protective_stop", trade_id=trade_id,
                                       realized_pnl=realized)
            except Exception as e:  # noqa: BLE001
                logger.warning("[%s] protective-stop trade_logger write failed: %s", target.get("symbol"), e)
            return True
        if qty + 1e-9 < expected or price <= 0:
            continue
        entry = abs(float(target.get("entry_price") or 0.0))
        side = str(target.get("side") or "long")
        if not (math.isfinite(entry) and entry > 0):
            return None
        realized = round((price - entry) * expected if side == "long" else (entry - price) * expected, 2)
        if not day_tier_logger.log_exit_fill(
            trade_id, str(target.get("symbol") or ""), order_id=stop_id,
            exit_reason="protective_stop", fill_price=price, fill_qty=float(expected),
            market_price_at_exit=price, realized_pnl=realized,
        ):
            return None
        trade_logger.log_event("exit", symbol=str(target.get("symbol") or ""), price=price,
                               size=expected, data_source="daytrade", tier="daytrade",
                               exit_reason="protective_stop", trade_id=trade_id,
                               realized_pnl=realized)
        return True
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
