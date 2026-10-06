# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/swing_breakout_manager.py — Confluence 2.0 swing tier: megacap 55-day breakout (RISK-PATH).

Design record: logs/design_records/c2_swing_breakout_tier_2026-09-29.md (evidence: labs 3-9 in
logs/design_records/entry_rebuild_2026-09-26.md; live since 2026-04-06 the 12-point swing lost -$126.00).

WHAT IT TRADES (long only, one lot per symbol):
  Universe  = the SWING_BREAKOUT_TOP_N names of SWING_BREAKOUT_POOL with the highest 60-day median dollar
              volume, ranked each day on CLOSED daily bars (T1 Alpaca via data.fetcher.fetch_closed_bars).
  Entry     = the last closed daily bar closes above the highest close of the prior 55 sessions AND above its
              200-day SMA → a marketable DAY limit at the next session (first cycle >= 10:05 ET).
  Protection= a GTC sell stop at fill − 2.5 × ATR(14, daily) placed immediately after the fill; if it cannot be
              placed after 3 tries the lot is flattened. Every cycle _reconcile repairs ANY held lot to "open with a
              resting stop for min(lot qty, broker long qty)": a stop cancelled/expired/rejected by anyone, never
              placed, or lost to a restart is re-placed (paged, retried each cycle); a stop is never placed on
              shares the broker does not show (no accidental short); a lot missing at the broker is booked closed.
  Exit      = in the last 10 minutes before the real close: live price below the lowest low of the prior 20 completed sessions, or the 30th
              session held (entry day = session 1). Exit = cancel the stop (confirmed) then close ONLY this tier's
              quantity (broker.partial_close_position(tier=TIER)) — never a whole-symbol close.

SIZING (board risk seat 2026-09-29): SWING_BREAKOUT_MAX_SLOTS slots × SWING_BREAKOUT_SLOT_PCT of equity,
  whole shares (a 1-share floor up to SWING_BREAKOUT_MIN1_MAX_PCT of equity when one share exceeds the slot);
  the per-trade risk at the stop is capped at SWING_BREAKOUT_MAX_RISK_PCT of equity; the tier's
  overnight notional is capped at the smaller of (a) min(SWING_BREAKOUT_BUDGET_PCT, 100% − other swing/day
  notional, QHM/F6 excluded) and (b) SWING_BREAKOUT_TOTAL_OVERNIGHT_K × equity − ALL position notional
  (Architecture Invariant #11 as amended 2026-10-02, Rafael-approved, BGG 4/4). No room → skip + log
  (never shrink; block-only, never a forced sale).

ISOLATION: a symbol that already has ANY Alpaca position or open order, or that is a quarterly-hold name,
  is skipped (no co-holds, no wash-trade collisions with another tier's resting sell stop). Orders carry the
  swing owner tag ("intraday"; the allocator lease's IN- client_order_id); this tier's own lots are identified
  by its state file (orphan/drift exclusion reads get_breakout_symbols()).
CAPITAL: every entry is admitted by execution.tier_capital_allocator.live_admit("swing", ...) before submit;
  a denial is a logged skip. Entries are admitted with overnight=False (the same as entry_logic's RTH swing
  path): the allocator's overnight flag caps TOTAL account gross — QHM and Forever-6 included — at 40%, and
  Rafael excluded those buy-and-hold tiers from swing exposure limits (CLAUDE.md, 2026-09-26). This tier's own
  overnight bound is the two-limb sizing cap above (Invariant #11). While TIER_CAPITAL_ALLOCATOR_ENABLED is
  False (since 2026-10-02) live_admit approves without a lease and the sizing cap is the binding bound.

LOGGING: every entry, skip and exit writes its decision stack to trade_events.jsonl (Rule D).
FAIL-SAFE: SWING_BREAKOUT_ENABLED False → no new entries; lots already held keep their stops and managed exits. Any error in one symbol aborts THAT
  symbol only and never raises into run_cycle.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
# Ownership: breakout lots ARE the swing tier (Rafael 2026-09-27: the swing tier's entries), so they carry the
# swing owner tag "intraday" (IN- client_order_id) and draw from the live tier capital allocator's "swing"
# budget — the same path as execution/entry_logic.py. A separate ledger tier would break the allocator's
# four-tier reconciliation (tier_capital_allocator._ledger_tier_gross) and block every tier's entries.
TIER = "intraday"
ALLOC_TIER = "swing"
FAMILY_ID = "swing_breakout_55d_v1"
HYPOTHESIS_VERSION = "c2-swing-breakout-2026-09-29"
_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "swing_breakout.json"

# Lot statuses that may still hold shares at the broker: counted as this tier's (slots, notional, orphan/drift
# exclusion) so no other system adopts or closes them. Only "open" lots are actively managed.
_ENTRY_PENDING = ("submitting", "submitted", "submit_unknown", "entry_unverified")
_HOLDING = ("open", "exit_unverified") + _ENTRY_PENDING
_ENTRY_START_MIN = 10 * 60 + 5     # first entry cycle: 10:05 ET — after run_cycle's 09:30-10:00 opening return
                                   # and after the QHM entry hook's own >= 10:05 gate (QHM picks go first)
_PAGE_EVERY_S = 1800               # repeat an unresolved-lot page at most every 30 minutes

_DEFAULT_POOL = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "NFLX", "AMD",
                 "ORCL", "PLTR", "MU", "COIN", "JPM", "V", "MA", "LLY", "UNH", "XOM", "COST", "WMT",
                 "CRM", "INTC", "SMCI", "ADBE", "QCOM", "UBER", "MSTR", "BAC"]


def _cfg(name: str, default: Any) -> Any:
    return getattr(config, name, default)


def enabled() -> bool:
    return bool(_cfg("SWING_BREAKOUT_ENABLED", False))


# ── pure decision functions (unit-tested) ───────────────────────────────────────────────────────
def median_dollar_volume(df, window: int = 60) -> Optional[float]:
    """60-day median of close × volume on closed daily bars; None when fewer than 40 bars."""
    try:
        if df is None or len(df) < 40:
            return None
        dv = (df["close"].astype(float) * df["volume"].astype(float)).tail(window)
        v = float(dv.median())
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def rank_universe(bars: dict, top_n: int) -> list[str]:
    """Top `top_n` symbols by 60-day median dollar volume (ties broken by symbol for determinism)."""
    scored = [(v, s) for s, df in bars.items() if (v := median_dollar_volume(df)) is not None]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, s in scored[:top_n]]


def atr14(df) -> Optional[float]:
    """Simple 14-day average true range on closed daily bars (the lab's base.atr definition)."""
    try:
        if df is None or len(df) < 15:
            return None
        h, lo, c = (df[k].astype(float).to_numpy() for k in ("high", "low", "close"))
        trs = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(len(c) - 14, len(c))]
        a = sum(trs) / 14.0
        return a if math.isfinite(a) and a > 0 else None
    except Exception:
        return None


def breakout_signal(df, lookback: int = 55) -> Optional[dict]:
    """The lab-8c rule on the LAST closed bar: close > highest close of the prior `lookback` sessions and
    close > SMA(200). Returns {close, prior_high, sma200, atr} or None (also None on insufficient data)."""
    try:
        if df is None or len(df) < max(200, lookback + 1):
            return None
        c = df["close"].astype(float)
        last = float(c.iloc[-1])
        prior_high = float(c.iloc[-(lookback + 1):-1].max())
        sma200 = float(c.tail(200).mean())
        a = atr14(df)
        if a is None or not all(math.isfinite(x) for x in (last, prior_high, sma200)):
            return None
        if last > prior_high and last > sma200:
            return {"close": last, "prior_high": prior_high, "sma200": sma200, "atr": a}
        return None
    except Exception:
        return None


def prior_low(df, n: int = 20) -> Optional[float]:
    """Lowest low of the last `n` COMPLETED sessions (the exit reference checked against the live price)."""
    try:
        if df is None or len(df) < n:
            return None
        v = float(df["low"].astype(float).tail(n).min())
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def sessions_held(df, entry_date: str) -> Optional[int]:
    """Sessions held INCLUDING today (entry day = 1): completed sessions on/after the entry date, plus today.
    `df` holds completed daily bars only, so today's session is added explicitly."""
    try:
        idx = [str(t)[:10] for t in df.index]
        return sum(1 for d in idx if d >= entry_date) + 1
    except Exception:
        return None


def exit_reason(live_price: float, lo20: Optional[float], held: Optional[int], max_hold: int) -> Optional[str]:
    """'trend_break' when the live price is below the prior 20-session low; 'time' on the max_hold-th session;
    None otherwise. Unknown inputs never force an exit (the GTC stop still protects the lot)."""
    if lo20 is not None and math.isfinite(live_price) and live_price > 0 and live_price < lo20:
        return "trend_break"
    if held is not None and held >= max_hold:
        return "time"
    return None


def size_order(equity: float, price: float, atr: float, other_notional: float, tier_notional: float,
               protected_notional: float = 0.0) -> tuple[int, str]:
    """Whole shares for one slot. Every bound is a floor; any bad input → 0 (skip).
    `other_notional` = every non-tier position EXCEPT the buy-and-hold tiers; `protected_notional` = QHM + F6.
    Invariant #11 as amended (Rafael approved 2026-10-02, BGG 4/4): swing/day ≤ 100% of equity with QHM/F6
    excluded, AND total overnight notional (all tiers) ≤ SWING_BREAKOUT_TOTAL_OVERNIGHT_K × equity."""
    try:
        if not all(math.isfinite(x) for x in (equity, price, atr, other_notional, tier_notional, protected_notional)):
            return 0, "non-finite sizing input"
        if equity <= 0 or price <= 0 or atr <= 0 or protected_notional < 0:
            return 0, "non-positive sizing input"
        slot = float(_cfg("SWING_BREAKOUT_SLOT_PCT", 0.20)) * equity
        room_tier = min(float(_cfg("SWING_BREAKOUT_BUDGET_PCT", 0.80)) * equity, equity - other_notional) - tier_notional
        room_total = (float(_cfg("SWING_BREAKOUT_TOTAL_OVERNIGHT_K", 1.75)) * equity
                      - (other_notional + protected_notional + tier_notional))
        budget = min(room_tier, room_total)
        risk_cap = float(_cfg("SWING_BREAKOUT_MAX_RISK_PCT", 0.02)) * equity
        stop_dist = float(_cfg("SWING_BREAKOUT_STOP_ATR", 2.5)) * atr
        q_slot = math.floor(min(slot, budget) / price) if budget > 0 else 0
        q_risk = math.floor(risk_cap / stop_dist)
        qty = max(0, min(q_slot, q_risk))
        floor_note = ""
        if qty == 0 and q_risk >= 1:
            # MIN-1-SHARE floor (BGGN 2026-09-29, risk seat + Gro + GAI): a name whose ONE share exceeds the slot
            # may still enter as 1 share when that share is <= min(SWING_BREAKOUT_MIN1_MAX_PCT × equity, budget
            # room) — the loss at the stop stays <= the risk cap (q_risk >= 1). Keeps high-priced megacaps in the
            # traded universe (no selection bias vs the backtest); the allocator still admits or denies it.
            one_cap = min(float(_cfg("SWING_BREAKOUT_MIN1_MAX_PCT", 0.25)) * equity, budget)
            if price <= one_cap:
                qty = 1
                floor_note = f", 1-share floor (≤ ${one_cap:.0f})"
        why = (f"slot ${slot:.0f}, budget room ${budget:.0f} (tier-limb ${room_tier:.0f}, total-limb ${room_total:.0f}; "
               f"tier ${tier_notional:.0f}, other ${other_notional:.0f}, QHM/F6 ${protected_notional:.0f}), "
               f"risk cap ${risk_cap:.0f} / stop ${stop_dist:.2f} → slot {q_slot} sh, risk {q_risk} sh → {qty} sh{floor_note}")
        return int(qty), why
    except Exception as e:
        return 0, f"sizing error: {e!r}"


def _lot_notional(rec: dict) -> float:
    """Notional a lot holds or may hold: filled qty × entry price when known, else the requested qty × limit
    (an in-flight / unverified entry claims its full request — never 0). Non-finite or bad input → 0."""
    try:
        qty, px = float(rec.get("qty") or 0), float(rec.get("entry_px") or 0)
        if not (qty > 0 and px > 0):
            qty, px = float(rec.get("req_qty") or 0), float(rec.get("limit") or 0)
        v = qty * px
        return v if math.isfinite(v) and v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _protected_notional(positions: list) -> float:
    """Market value of the QHM + Forever-6 share of `positions`, from the ownership ledger's protected tiers
    (per symbol: |market_value| × min(protected qty, |broker qty|) / |broker qty|). FAIL-SAFE: an unreadable
    ledger or position returns 0.0 for it, so those shares stay in the stricter 100% limb (fewer entries,
    never more)."""
    try:
        from execution.ownership_guard import load_ledger, protected_floor
        led = load_ledger()
    except Exception as e:
        logger.warning("swing_breakout: ownership ledger unreadable (%s) — QHM/F6 not excluded (fail-safe)", e)
        return 0.0
    total = 0.0
    for p in positions or []:
        try:
            qty = abs(float(getattr(p, "qty", 0) or 0))
            mv = abs(float(getattr(p, "market_value", 0.0) or 0.0))
            if qty <= 0 or not math.isfinite(mv):
                continue
            prot = min(max(float(protected_floor(led, getattr(p, "symbol", ""))), 0.0), qty)
            total += mv * prot / qty
        except Exception as e:
            logger.warning("swing_breakout: protected share of %s unreadable (%s) — counted as swing (fail-safe)",
                           getattr(p, "symbol", "?"), e)
    return total if math.isfinite(total) else 0.0


# ── state (atomic) ──────────────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        if _STATE.exists():
            d = json.loads(_STATE.read_text())
            if isinstance(d, dict):
                d.setdefault("positions", {})
                return d
            raise ValueError("state is not a dict")
    except Exception as e:
        # Unreadable state must NOT look like "no positions" (that would re-enter and orphan stops):
        # raise so the caller skips the cycle.
        raise RuntimeError(f"swing_breakout state unreadable: {e}") from e
    return {"positions": {}}


def _save_state(state: dict) -> bool:
    try:
        os.makedirs(_STATE.parent, exist_ok=True)
        tmp = _STATE.with_suffix(f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STATE)
        return True
    except Exception as e:
        logger.error("swing_breakout state write FAILED: %s", e)
        return False


_last_good_symbols: frozenset = frozenset()


def get_breakout_symbols() -> set:
    """Symbols this tier currently holds or may hold (for orphan/drift exclusion). On an unreadable state file
    returns the LAST GOOD set (never empty-on-error, which would let a restart adopt this tier's lots)."""
    global _last_good_symbols
    try:
        syms = frozenset(s for s, p in _load_state().get("positions", {}).items() if p.get("status") in _HOLDING)
        _last_good_symbols = syms
        return set(syms)
    except Exception as e:
        # FAIL CLOSED (as QHM does): last good set, else the whole configured pool (over-protective — a restart
        # must never adopt this tier's lots as intraday orphans because its own state file is unreadable)
        fallback = set(_last_good_symbols) or set(_cfg("SWING_BREAKOUT_POOL", _DEFAULT_POOL) or _DEFAULT_POOL)
        logger.critical("swing_breakout: state unreadable (%s) — excluding %d symbol(s) from orphan adoption", e, len(fallback))
        return fallback


def _page(msg: str) -> None:
    logger.critical(msg)
    try:
        from alerts import send_slack
        send_slack("🚨 SWING-BREAKOUT ALERT\n" + msg)
    except Exception as e:  # pragma: no cover
        logger.error("swing_breakout: page send failed: %s", e)


def _page_throttled(rec: dict, msg: str) -> None:
    """Page for an unresolved lot at most every _PAGE_EVERY_S (the throttle timestamp lives in the lot record,
    which the caller saves)."""
    now = time.time()
    if now - float(rec.get("last_page_ts") or 0.0) >= _PAGE_EVERY_S:
        rec["last_page_ts"] = now
        _page(msg)
    else:
        logger.critical(msg)


def _log(event: str, symbol: str, **kw) -> None:
    try:
        import trade_logger
        trade_logger.log_event(event, symbol=symbol, data_source="alpaca_data", tier=TIER,
                               setup="c2_breakout_55d", family_id=FAMILY_ID,
                               hypothesis_version=HYPOTHESIS_VERSION, **kw)
    except Exception as e:
        logger.warning("[%s] swing_breakout trade_logger write failed: %s", symbol, e)


_TERMINAL = ("filled", "canceled", "cancelled", "rejected", "expired", "done_for_day")


def _is_terminal(status: Any) -> bool:
    """True for a final order status. 'partially_filled' is NOT terminal (it also ends in 'filled')."""
    st = str(status or "").lower().split(".")[-1]
    return st in _TERMINAL


class SwingBreakoutManager:
    """Owns the breakout tier's lots. All methods are called from run_cycle (single-threaded)."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    # ── data ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _bars(symbol: str):
        from data.fetcher import fetch_closed_bars
        return fetch_closed_bars(symbol, getattr(config, "TF_DAILY", "1Day"), num_bars=260).df

    # ── order / position reads (None = UNREADABLE, never "zero") ─────────────
    @staticmethod
    def _read_order(order_id: str) -> Optional[tuple[int, float, bool]]:
        """(filled_qty, avg_price, is_terminal) from one order read; None when the order is UNREADABLE — never
        read an unreadable order as "zero filled" (that would orphan a filled lot or mask a stop fill)."""
        from execution import broker
        if not order_id:
            return None
        try:
            o = broker.get_order(order_id)
            if o is None:
                return None
            return (int(float(getattr(o, "filled_qty", 0) or 0)), float(getattr(o, "filled_avg_price", 0) or 0),
                    _is_terminal(getattr(o, "status", "")))
        except Exception as e:
            logger.warning("swing_breakout: order %s unreadable: %s", order_id, e)
            return None

    def _final_fill(self, order_id: str) -> Optional[tuple[int, float]]:
        r = self._read_order(order_id)
        return None if r is None else (r[0], r[1])

    @staticmethod
    def _broker_qty(symbol: str) -> Optional[int]:
        """Long shares held at the broker for `symbol` (0 = no position); None when unreadable."""
        from execution import broker
        try:
            pos = broker.get_open_position(symbol)
            return 0 if pos is None else max(int(float(getattr(pos, "qty", 0) or 0)), 0)
        except Exception as e:
            logger.warning("[%s] swing_breakout: position unreadable: %s", symbol, e)
            return None

    def _await_fill(self, order_id: str) -> tuple[int, float]:
        """Poll until terminal or SWING_BREAKOUT_FILL_WAIT_S; keeps the HIGHEST fill seen (an unreadable read
        never lowers it). Always reads at least once."""
        deadline = time.monotonic() + float(_cfg("SWING_BREAKOUT_FILL_WAIT_S", 15.0))
        filled, px = 0, 0.0
        while True:
            r = self._read_order(order_id)
            if r is not None:
                if r[0] >= filled:
                    filled, px = r[0], (r[1] or px)
                if r[2]:
                    break
            if time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        return filled, px

    def _settle(self, order_id: str, wait_s: float = 3.0) -> Optional[tuple[int, float, bool]]:
        """Bring an order to a FINAL status before its fill count is trusted: read it; if still working, cancel it
        and poll (0.25 s) up to wait_s for a terminal status. A fill that lands while the cancel is pending is
        therefore counted. Returns the last read (terminal flag False = still working), or None if unreadable."""
        from execution import broker
        r = self._read_order(order_id)
        if r is not None and r[2]:
            return r
        try:
            broker.cancel_order(order_id)      # also when the first read failed: every caller wants it dead
        except Exception as e:
            logger.warning("swing_breakout: cancel of %s failed: %s", order_id, e)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            r2 = self._read_order(order_id)
            if r2 is not None:
                r = r2
                if r2[2]:
                    break
        return r

    # ── entries ───────────────────────────────────────────────────────────────
    def run_entries(self, now: Optional[datetime] = None) -> list[str]:
        """Once per ET day, from 10:05 ET: evaluate the universe on the prior closed bar and enter."""
        if not enabled() or self.dry_run:
            return []
        now = now or datetime.now(ET)
        if now.hour * 60 + now.minute < _ENTRY_START_MIN:
            return []
        today = now.strftime("%Y-%m-%d")
        try:
            state = _load_state()
        except RuntimeError as e:
            _page(f"entries skipped — {e}")
            return []
        if state.get("last_entry_scan") == today:
            return []
        # latch FIRST so an exception mid-scan never re-fires entries later the same day
        state["last_entry_scan"] = today
        if not _save_state(state):
            _page("entries skipped — could not persist the daily latch (fail-closed)")
            return []
        from execution import broker
        from execution.quarterly_hold_manager import get_quarterly_hold_symbols
        entered: list[str] = []
        pool = list(_cfg("SWING_BREAKOUT_POOL", _DEFAULT_POOL) or _DEFAULT_POOL)
        bars = {}
        for s in pool:
            try:
                df = self._bars(s)
                if df is not None and not df.empty:
                    bars[s] = df
            except Exception as e:
                logger.warning("[%s] swing_breakout bars fetch failed: %s", s, e)
        if not bars:
            # a total data outage must not burn the day: un-latch so the next cycle retries
            state["last_entry_scan"] = None
            _save_state(state)
            logger.warning("swing_breakout: no daily bars for any pool symbol — entries retry next cycle")
            return []
        universe = rank_universe(bars, int(_cfg("SWING_BREAKOUT_TOP_N", 10)))
        _log("signal", "", note="breakout universe", universe=universe, pool_n=len(pool), bars_n=len(bars))
        try:
            positions = broker.get_open_positions() or []
            open_orders = broker.get_open_orders()
            acct = broker.get_account()
            equity = float(getattr(acct, "equity", 0.0) or 0.0)
            buying_power = float(getattr(acct, "buying_power", 0.0) or 0.0)
        except Exception as e:
            state["last_entry_scan"] = None      # transient read failure must not burn the day: retry next cycle
            _save_state(state)
            _page(f"entries skipped — live book unreadable (fail-closed, retry next cycle): {e!r}")
            return []
        if open_orders is None:
            state["last_entry_scan"] = None
            _save_state(state)
            _page("entries skipped — open orders unreadable (fail-closed, retry next cycle)")
            return []
        held_syms = {getattr(p, "symbol", None) for p in positions}
        order_syms = {getattr(o, "symbol", None) for o in open_orders}
        mine = {s for s, p in state["positions"].items() if p.get("status") in _HOLDING}
        added = 0.0
        try:
            qhm = set(get_quarterly_hold_symbols())
        except Exception as e:
            logger.warning("swing_breakout: QHM symbols unreadable (%s) — entries retry next cycle (fail-closed)", e)
            state["last_entry_scan"] = None
            _save_state(state)
            return []
        try:
            # QHM's configured picks, INCLUDING ones still pending entry: QHM may buy them later, and a co-held
            # symbol would collide on the wash-trade rule and on whole-symbol stop accounting
            from execution.quarterly_hold_manager import _configured_qhm_symbols
            qhm |= set(_configured_qhm_symbols())
        except Exception as e:
            logger.warning("swing_breakout: QHM configured picks unreadable: %s", e)
        # Forever-6 universe names too: a resting breakout sell stop would make Alpaca reject F6's buy on the
        # same symbol (wash-trade rule) for up to 30 sessions — F6 crash-buys must never be blocked by this tier.
        qhm |= {str(x).upper() for x in (_cfg("FOREVER6_UNIVERSE", []) or [])}
        for s in universe:
            if len({x for x, p in state["positions"].items() if p.get("status") in _HOLDING}) >= int(_cfg("SWING_BREAKOUT_MAX_SLOTS", 4)):
                _log("skip", s, reason="all slots full")
                continue
            sig = breakout_signal(bars.get(s))
            if sig is None:
                continue
            if s in mine or s in held_syms or s in order_syms or s in qhm:
                _log("skip", s, reason="symbol already held/ordered/QHM", signal=sig)
                continue
            try:
                if self._enter(s, sig, state, positions, equity, buying_power, today, added):
                    entered.append(s)
            except Exception as e:
                _page(f"[{s}] entry raised (symbol aborted): {e!r}")
            # Count EVERY lot this scan left holding or possibly holding (open, in-flight, unverified, or an
            # unconfirmed flatten) — not only clean entries — so the next symbol never sizes into room a
            # late fill already uses (board risk seat 2026-10-02).
            r = state["positions"].get(s) or {}
            if r.get("status") in _HOLDING:
                added += _lot_notional(r)
        return entered

    def _enter(self, symbol: str, sig: dict, state: dict, positions: list, equity: float,
               buying_power: float, today: str, added_notional: float = 0.0) -> bool:
        from data.alpaca_data import get_latest_quote
        from execution import broker
        q = get_latest_quote(symbol)
        if not isinstance(q, dict) or not (0 < float(q.get("bid") or 0) <= float(q.get("ask") or 0)):
            _log("skip", symbol, reason="quote unreadable", signal=sig)
            return False
        ask = float(q["ask"])
        mine_notional = 0.0
        other_notional = 0.0
        tier_syms = {s for s, p in state["positions"].items() if p.get("status") in _HOLDING}
        pos_syms = {getattr(p, "symbol", None) for p in positions}
        for p in positions:
            mv = abs(float(getattr(p, "market_value", 0.0) or 0.0))
            if getattr(p, "symbol", None) in tier_syms:
                mine_notional += mv
            else:
                other_notional += mv
        # held/pending lots NOT yet visible as a broker position (in-flight or unverified entries from earlier
        # cycles) still claim their requested notional; lots added in THIS scan arrive via added_notional
        for s_, r_ in state["positions"].items():
            if r_.get("status") in _HOLDING and s_ not in pos_syms and s_ != symbol and r_.get("entry_date") != today:
                mine_notional += _lot_notional(r_)
        # buy-and-hold (QHM/F6) share of the other notional — excluded from the 100% limb, counted in the K limb
        protected_notional = min(_protected_notional([p for p in positions
                                                      if getattr(p, "symbol", None) not in tier_syms]), other_notional)
        other_notional -= protected_notional
        # positions is the start-of-scan snapshot: lots entered earlier in THIS scan are added explicitly
        mine_notional += added_notional
        buying_power -= added_notional
        qty, why = size_order(equity, ask, sig["atr"], other_notional, mine_notional, protected_notional)
        if qty < 1:
            _log("skip", symbol, reason="no size", sizing=why, signal=sig)
            return False
        limit = round(ask * (1 + float(_cfg("SWING_BREAKOUT_LIMIT_SLIP_PCT", 0.002))), 2)
        if buying_power < qty * limit * 1.10:
            _log("skip", symbol, reason="buying power", bp=buying_power, sizing=why, signal=sig)
            return False
        stop_px = round(ask - float(_cfg("SWING_BREAKOUT_STOP_ATR", 2.5)) * sig["atr"], 2)
        from execution.tier_capital_allocator import live_admit, live_bind, live_order_id, live_release
        capital = live_admit("swing", TIER, symbol, "buy", int(qty), limit,
                             stop_price=stop_px, risk_price_bound=limit)
        if not capital.approved:
            _log("skip", symbol, reason=f"allocator: {capital.reason}", sizing=why, signal=sig)
            return False
        coid = live_order_id(capital.lease) or ""
        if not coid:
            # allocator OFF (no lease): mint our own IN- tagged client id and STORE it, so an ambiguous submit is
            # recoverable by client id and its working remainder can be cancelled (board execution seat 2026-10-02)
            import uuid
            from execution.ownership_guard import make_coid
            coid = make_coid(TIER, symbol, "buy", int(time.time() * 1000), uuid.uuid4().hex[:8])
        rec: dict[str, Any] = {"status": "submitting", "entry_date": today, "coid": coid, "qty": 0, "signal": sig,
               "sizing": why, "limit": limit, "req_qty": int(qty), "ts": datetime.now(PT).isoformat()}
        state["positions"][symbol] = rec
        if not _save_state(state):
            live_release(capital.lease, "state_write_failed")
            state["positions"].pop(symbol, None)
            _page(f"[{symbol}] entry ABORTED — could not persist the pre-submit record")
            return False
        order = broker.submit_limit_order(symbol, qty, "buy", limit, tier=TIER, client_order_id=coid or None)
        if order is None:
            order = live_release(capital.lease, "submit_none")   # recovers an ambiguous submit by client id
        else:
            live_bind(capital.lease, order)
        if order is None or not getattr(order, "id", None):
            # ambiguous: the order may be live at Alpaca. Kept as a HELD status; _reconcile resolves it by client id.
            rec["status"] = "submit_unknown"
            _page_throttled(rec, f"[{symbol}] breakout entry submit AMBIGUOUS — resolving by client id each cycle")
            _save_state(state)
            return False
        rec["entry_order_id"] = str(getattr(order, "id", ""))
        rec["status"] = "submitted"
        _save_state(state)
        self._await_fill(rec["entry_order_id"])
        self._resolve_entry(symbol, rec, state)
        if rec.get("status") != "open":
            return False
        for _ in range(2):                                   # 3 stop attempts in total (one inside _resolve_entry)
            if rec.get("status") != "open" or not rec.get("unprotected"):
                break                                        # protected, or already closed by _reprotect
            time.sleep(1.0)
            self._reprotect(symbol, rec, state, int(rec.get("qty") or 0))
        if rec.get("status") != "open":
            return False
        if rec.get("unprotected") and self._broker_qty(symbol) == 0:
            # the position read still lags the fill: selling now could act on a stale read — leave the lot open +
            # unprotected; _reconcile re-places the stop next cycle (paged by _reprotect)
            return False
        if rec.get("unprotected"):
            _page(f"[{symbol}] breakout stop NOT placed after 3 tries — flattening the {rec.get('qty')}-sh lot")
            self._close_lot(symbol, rec, state, "stop_unconfirmed_flatten")
            return False
        logger.info("[%s] breakout ENTERED %s sh @ %.2f, GTC stop %.2f", symbol, rec.get("qty"),
                    float(rec.get("entry_px") or 0), float(rec.get("stop_px") or 0))
        return True

    def _resolve_entry(self, symbol: str, rec: dict, state: dict) -> None:
        """Drive an in-flight entry (submitting / submitted / submit_unknown / entry_unverified — also after a
        restart) to a final answer: settle the order (cancel any working remainder, count late fills), then
        either promote the filled qty to "open" with a stop, or mark it unfilled. Unresolvable → stays held
        (excluded from orphan adoption) and paged."""
        from execution import broker
        oid = str(rec.get("entry_order_id") or "")
        if not oid and rec.get("coid"):
            try:
                o = broker.get_order_by_client_order_id(rec["coid"])
            except Exception as e:
                logger.warning("[%s] breakout client-id lookup failed: %s", symbol, e)
                o = None
            if o is not None and getattr(o, "id", None):
                oid = str(getattr(o, "id", ""))
                rec["entry_order_id"] = oid
                _save_state(state)
        r = self._settle(oid) if oid else None
        if r is None:
            # no readable order: decide from the broker. This symbol had NO position or order when entry began,
            # so shares held now are this lot's. Held 0 with no open order for it → the entry never happened.
            held = self._broker_qty(symbol)
            try:
                orders = broker.get_open_orders(symbol)
            except Exception as e:
                logger.warning("[%s] breakout open-orders read failed: %s", symbol, e)
                orders = None
            if held == 0 and orders is not None and not any(
                    str(getattr(o, "side", "")).lower().endswith("buy") for o in orders):
                rec["status"] = "unfilled"
                _save_state(state)
                _log("skip", symbol, reason="entry not placed/filled (no order, no position)", signal=rec.get("signal"))
                return
            buy_working = orders is None or any(str(getattr(o, "side", "")).lower().endswith("buy") for o in orders)
            if held is None or held == 0 or buy_working:
                # never promote while a buy may still be filling (later fills would sit outside the lot's stop)
                rec["status"] = "entry_unverified"
                _page_throttled(rec, f"[{symbol}] breakout entry UNVERIFIED — order unreadable, position "
                                     f"{'unreadable' if held is None else ('flat' if held == 0 else f'{held} sh')}"
                                     f"{' with a working/unknown buy' if buy_working else ''}; retrying each cycle")
                _save_state(state)
                return
            # at most what this entry asked for: any excess belongs to another tier that bought meanwhile
            filled, px, terminal = min(held, int(rec.get("req_qty") or held)), 0.0, True
        else:
            filled, px, terminal = r
        if not terminal:
            rec["status"] = "submitted"          # cancel still pending: re-settled next cycle
            _page_throttled(rec, f"[{symbol}] breakout entry order {oid} still working after cancel — re-checking each cycle")
            _save_state(state)
            return
        if filled < 1:
            rec["status"] = "unfilled"
            _save_state(state)
            _log("skip", symbol, reason="entry not filled", limit=rec.get("limit"), sizing=rec.get("sizing"),
                 signal=rec.get("signal"))
            return
        sig = rec.get("signal") or {}
        if px <= 0:
            px = float(rec.get("limit") or sig.get("close") or 0.0)   # estimate for the stop anchor only
            rec["entry_px_estimated"] = True
        stop_px = round(px - float(_cfg("SWING_BREAKOUT_STOP_ATR", 2.5)) * float(sig.get("atr") or 0.0), 2)
        rec.update(status="open", qty=int(filled), entry_px=px, stop_px=stop_px, stop_order_id="",
                   opened_ts=time.time())
        _save_state(state)
        _log("entry", symbol, price=px, size=int(filled), direction="long", stop=stop_px,
             limit=rec.get("limit"), sizing=rec.get("sizing"), signal=sig, trade_id=rec.get("coid"),
             entry_px_estimated=bool(rec.get("entry_px_estimated")))
        self._reprotect(symbol, rec, state, int(filled))

    # ── protection ────────────────────────────────────────────────────────────
    def _reprotect(self, symbol: str, rec: dict, state: dict, want_qty: int, depth: int = 0) -> bool:
        """Place a fresh GTC stop for min(want_qty, the broker's long qty) — never on shares the broker does not
        show (a sell stop with nothing behind it could open a short). Broker shows 0 → the lot is booked gone.
        Unreadable broker / failed stop → the lot stays "open" flagged unprotected, paged (throttled) and retried
        every cycle. A price already through the stop → the lot is closed instead (depth guard: once)."""
        from execution import broker
        if rec.get("status") == "closed":
            return False
        held = self._broker_qty(symbol)
        if held is None:
            rec.update(unprotected=True, stop_order_id="")
            _page_throttled(rec, f"[{symbol}] breakout lot UNPROTECTED — position unreadable, stop not placed; retrying each cycle")
            _save_state(state)
            return False
        qty = min(max(int(want_qty), 0), held)
        if qty < 1:
            if not self._confirmed_gone(symbol, rec):
                # a zero read that is not corroborated (position-endpoint lag right after a fill) is UNREADABLE,
                # not "gone": keep the lot open + unprotected and retry (cold-2nd 2026-10-02)
                rec.update(unprotected=True, stop_order_id="")
                _page_throttled(rec, f"[{symbol}] breakout lot: broker shows 0 sh but not confirmed flat — "
                                     f"stop not placed; retrying each cycle")
                _save_state(state)
                return False
            self._book_gone(symbol, rec, state)
            return False
        if qty < int(want_qty):
            # shares this lot expected are not at the broker and were never booked: flag the P&L as unknown
            rec["partial_pnl_unknown"] = True
            _page(f"[{symbol}] breakout lot expected {want_qty} sh but the broker holds {held} — protecting {qty}; "
                  f"{int(want_qty) - qty} sh unaccounted (P&L marked unknown); check fills")
        stop_px = float(rec.get("stop_px") or 0.0)
        o: Any = None
        if stop_px > 0:
            try:
                o = broker.submit_gtc_stop_order(symbol, qty, "sell", stop_px, tier=TIER, allow_cancel_blocking=False)
            except Exception as e:
                logger.warning("[%s] breakout stop submit raised: %s", symbol, e)
                o = None
        held_sentinel = getattr(broker, "PROTECTION_ALREADY_HELD", None)
        if o is not None and held_sentinel is not None and o is held_sentinel:
            # a stable live reducing order already holds these shares (typically this lot's own stop that a read
            # could not see) — protected; nothing cancelled. The next cycle re-checks.
            rec.update(status="open", qty=qty, unprotected=False, stop_order_id=self._find_resting_stop(symbol, qty))
            _save_state(state)
            logger.info("[%s] breakout stop not placed: shares already held by a live reducing order (%s)",
                        symbol, rec["stop_order_id"] or "id not found")
            return True
        if o is not None and getattr(o, "id", None):
            rec.update(status="open", qty=qty, unprotected=False, stop_order_id=str(getattr(o, "id", "")))
            _save_state(state)
            logger.info("[%s] breakout lot protected: %d sh, GTC stop %.2f", symbol, qty, stop_px)
            return True
        unknown_sentinel = getattr(broker, "PROTECTION_UNKNOWN", None)
        if o is not None and unknown_sentinel is not None and o is unknown_sentinel:
            # the order book is unreadable: protection status UNKNOWN (not proven missing) — never close on it
            rec.update(status="open", qty=qty, unprotected=True, stop_order_id="")
            _page_throttled(rec, f"[{symbol}] breakout lot protection UNKNOWN (order book unreadable) — re-checking each cycle")
            _save_state(state)
            return False
        # failure (None / no id / no stop price)
        if depth == 0 and self._market_open() and self._price_through_stop(symbol, stop_px):
            # regular hours only: after hours a market close only queues for the open (and would be cancelled)
            _page_throttled(rec, f"[{symbol}] breakout stop could not be placed and the price is already through "
                                 f"${stop_px:.2f} — closing the lot")
            rec.update(status="open", qty=qty, unprotected=True, stop_order_id="")
            _save_state(state)
            self._close_lot(symbol, rec, state, "stop_breached_unprotected", _depth=1)
            return False
        rec.update(status="open", qty=qty, unprotected=True, stop_order_id="")
        _page_throttled(rec, f"[{symbol}] breakout lot of {qty} sh is UNPROTECTED — GTC stop placement failed; retrying each cycle")
        _save_state(state)
        return False

    def _confirmed_gone(self, symbol: str, rec: dict) -> bool:
        """True only when a FLAT broker position is corroborated: the lot is older than
        SWING_BREAKOUT_FLAT_GRACE_S (a just-filled lot's position read can lag) AND two more reads ~1 s and ~2 s
        apart both show 0. Any non-zero or unreadable read → False (the caller treats the lot as still held)."""
        opened = float(rec.get("opened_ts") or 0.0)
        if opened and time.time() - opened < float(_cfg("SWING_BREAKOUT_FLAT_GRACE_S", 300)):
            return False
        for wait in (1.0, 2.0):
            time.sleep(wait)
            if self._broker_qty(symbol) != 0:
                return False
        return True

    @staticmethod
    def _find_resting_stop(symbol: str, qty: int) -> str:
        """Id of a live SELL stop for exactly `qty` shares of `symbol` with this tier's owner tag, else ""."""
        from execution import broker
        try:
            for o in broker.get_open_orders(symbol) or []:
                if (str(getattr(o, "side", "")).lower().endswith("sell")
                        and "stop" in str(getattr(o, "order_type", getattr(o, "type", ""))).lower()
                        and int(float(getattr(o, "qty", 0) or 0)) == int(qty)
                        and str(getattr(o, "client_order_id", "") or "").startswith("IN-")):
                    return str(getattr(o, "id", "") or "")
        except Exception as e:
            logger.warning("[%s] breakout resting-stop lookup failed: %s", symbol, e)
        return ""

    @staticmethod
    def _market_open() -> bool:
        """Alpaca clock is_open; False when unreadable (then no market close is attempted — the lot stays
        flagged unprotected, paged and retried)."""
        try:
            from execution import broker
            return bool(broker.get_clock().get("is_open"))
        except Exception as e:
            logger.warning("swing_breakout: clock read failed (%s) — treating the market as closed", e)
            return False

    @staticmethod
    def _price_through_stop(symbol: str, stop_px: float) -> bool:
        try:
            from data.alpaca_data import get_latest_quote
            q = get_latest_quote(symbol)
            bid = float(q.get("bid") or 0) if isinstance(q, dict) else 0.0
            return stop_px > 0 and 0 < bid <= stop_px
        except Exception as e:
            logger.warning("[%s] breakout quote read failed: %s", symbol, e)
            return False

    # ── exits ─────────────────────────────────────────────────────────────────
    def run_exit_check(self, now: Optional[datetime] = None) -> list[str]:
        """Every cycle: reconcile every held lot. Near the real close: trend-break / max-hold exits, latched per
        lot per day only once DECIDED (a deferred/unconfirmed exit retries on the next cycle).
        Runs even when SWING_BREAKOUT_ENABLED is False: the flag stops NEW entries only, and lots already held
        keep their managed exits (a no-op when the state file holds no lots)."""
        if self.dry_run:
            return []
        try:
            state = _load_state()
        except RuntimeError as e:
            _page(f"exit check skipped — {e}")
            return []
        self._reconcile(state)
        now = now or datetime.now(ET)          # call-time clock by default (the hook runs mid-cycle)
        if not self._near_close(now):
            return []
        today = now.strftime("%Y-%m-%d")
        from data.alpaca_data import get_latest_quote
        closed: list[str] = []
        for s, rec in list(state["positions"].items()):
            if rec.get("status") != "open" or rec.get("exit_checked") == today:
                continue
            try:
                df = self._bars(s)
                q = get_latest_quote(s)
                live = (float(q["bid"]) + float(q["ask"])) / 2.0 if isinstance(q, dict) and q.get("bid") and q.get("ask") else float("nan")
                lo20 = prior_low(df, 20)
                held = sessions_held(df, str(rec.get("entry_date")))
                why = exit_reason(live, lo20, held, int(_cfg("SWING_BREAKOUT_MAX_HOLD", 30)))
                if why is None:
                    if math.isfinite(live) and lo20 is not None and held is not None:
                        rec["exit_checked"] = today      # decided: hold (unknown inputs retry next cycle)
                        _save_state(state)
                    continue
                if self._close_lot(s, rec, state, why, live=live, lo20=lo20, held=held):
                    closed.append(s)
            except Exception as e:
                _page(f"[{s}] exit check raised (lot kept, GTC stop still rests): {e!r}")
        return closed

    @staticmethod
    def _near_close(now: datetime) -> bool:
        """Within SWING_BREAKOUT_EXIT_MINUTES of the REAL close (Alpaca clock next_close — half-day aware);
        falls back to 15:50 ET when the clock is unreadable."""
        mins = float(_cfg("SWING_BREAKOUT_EXIT_MINUTES", 10))
        try:
            from execution import broker
            nc = broker.get_clock().get("next_close")
            if nc is not None:
                left = (nc - now).total_seconds() / 60.0
                return 0 < left <= mins
        except Exception as e:
            logger.warning("swing_breakout: clock read failed (%s) — 15:50 ET fallback", e)
        return 16 * 60 - mins <= now.hour * 60 + now.minute < 16 * 60

    def _close_lot(self, symbol: str, rec: dict, state: dict, reason: str, _depth: int = 0, **ctx) -> bool:
        """Cancel this tier's stop (confirmed), book any stop fill, then close ONLY this tier's remaining qty and
        settle the close order to a final status. Unconfirmed → book what sold, re-protect what the broker still
        holds. Never a whole-symbol close; never a stop on shares that are not held."""
        from execution import broker
        if rec.get("status") == "closed":
            return True                          # already closed and booked — never sell or book it again
        stop_id = str(rec.get("stop_order_id") or "")
        if stop_id and not broker.cancel_stop_confirmed(symbol, stop_id):
            _page(f"[{symbol}] breakout exit ({reason}) deferred — stop cancel not confirmed; GTC stop still protects the lot")
            return False
        if stop_id:
            final = self._final_fill(stop_id)
            if final is None:
                # the stop is confirmed cancelled but its fill is unreadable: never sell blind (a fired stop plus
                # a close would short the name). _reconcile resolves it next cycle.
                rec["status"] = "exit_unverified"
                _page_throttled(rec, f"[{symbol}] breakout exit ({reason}) HALTED — stop cancelled but its fill is "
                                     f"unreadable; the lot may be UNPROTECTED — resolving each cycle")
                _save_state(state)
                return False
            filled, px = final
            qty = int(rec.get("qty") or 0)
            if filled >= qty > 0 and px > 0:   # the stop fired during the cancel
                self._book_exit(symbol, rec, state, "protective_stop", px, filled)
                return True
            if 0 < filled < qty:
                self._book_partial(symbol, rec, state, px, filled, "protective_stop_partial", clear_key="stop_order_id")
            rec["stop_order_id"] = ""           # cancelled and fully accounted
            _save_state(state)
        qty = int(rec.get("qty") or 0)
        if qty < 1:
            rec["status"] = "closed"
            _save_state(state)
            return True
        order = broker.partial_close_position(symbol, qty, tier=TIER, _return_order=True)
        # True = the broker found no position to close; False/None = submit failed. Neither carries an order:
        # _reprotect below checks the broker's actual long qty before any stop is placed.
        oid = str(getattr(order, "id", "") or "") if order not in (None, False, True) else ""
        filled, px = 0, 0.0
        if oid:
            rec["close_order_id"] = oid
            rec["exit_reason_pending"] = reason      # lets a restart book this close by its real fill
            _save_state(state)
            filled, px = self._await_fill(oid)
            if not (filled >= qty and px > 0):
                r = self._settle(oid)
                if r is None or not r[2]:
                    rec["status"] = "exit_unverified"
                    _page_throttled(rec, f"[{symbol}] breakout close ({reason}) order {oid} unreadable/still working — "
                                         f"resolving each cycle")
                    _save_state(state)
                    return False
                filled, px = max(filled, r[0]), (r[1] or px)
            if filled >= qty and px > 0:
                self._book_exit(symbol, rec, state, reason, px, filled, **ctx)
                return True
            if 0 < filled < qty and px > 0:
                self._book_partial(symbol, rec, state, px, filled, f"{reason}_partial", clear_key="close_order_id")
            rec["close_order_id"] = ""
            _save_state(state)
        _page_throttled(rec, f"[{symbol}] breakout close ({reason}) UNCONFIRMED ({filled}/{qty}) — re-protecting the remainder")
        self._reprotect(symbol, rec, state, int(rec.get("qty") or 0), depth=_depth + 1)
        return False

    def _book_partial(self, symbol: str, rec: dict, state: dict, px: float, filled: int, reason: str,
                      clear_key: str = "") -> None:
        """Book a PARTIAL sale of this lot: its P&L is added to the lot's running realized P&L (so the final
        exit never books only the remainder) and the lot's qty shrinks by what sold. `clear_key` (the order-id
        field that produced the fill) is cleared in the SAME save, so a crash can never re-book the leg."""
        entry = float(rec.get("entry_px") or 0.0)
        # an ESTIMATED entry price never produces a P&L number (never fabricate — booked unknown instead)
        pnl = (round((px - entry) * filled, 2) if entry > 0 and px > 0 and not rec.get("entry_px_estimated")
               else None)
        if pnl is not None:
            rec["partial_pnl"] = round(float(rec.get("partial_pnl") or 0.0) + pnl, 2)
        else:
            rec["partial_pnl_unknown"] = True
        rec["qty"] = max(int(rec.get("qty") or 0) - int(filled), 0)
        if clear_key:
            rec[clear_key] = ""
        _save_state(state)
        _log("partial_exit", symbol, price=px, size=int(filled), exit_reason=reason, realized_pnl=pnl,
             trade_id=rec.get("coid"), entry_px_estimated=bool(rec.get("entry_px_estimated")))

    def _book_exit(self, symbol: str, rec: dict, state: dict, reason: str, px: float, qty: int, **ctx) -> None:
        """Final exit. realized_pnl = this fill's P&L + every partial booked earlier (None if any part is unknown)."""
        entry = float(rec.get("entry_px") or 0.0)
        leg = (round((px - entry) * qty, 2) if entry > 0 and px > 0 and not rec.get("entry_px_estimated")
               else None)
        pnl = (None if leg is None or rec.get("partial_pnl_unknown")
               else round(leg + float(rec.get("partial_pnl") or 0.0), 2))
        rec.update(status="closed", exit_px=px, exit_reason=reason, exit_date=datetime.now(ET).strftime("%Y-%m-%d"),
                   realized_pnl=pnl, stop_order_id="", close_order_id="", unprotected=False)
        _save_state(state)
        _log("exit", symbol, price=px, size=int(qty), exit_reason=reason, realized_pnl=pnl,
             entry_px_estimated=bool(rec.get("entry_px_estimated")),
             trade_id=rec.get("coid"), **{k: v for k, v in ctx.items()
                                             if v is not None and not (isinstance(v, float) and not math.isfinite(v))})
        logger.info("[%s] breakout EXIT %s %d sh @ %.2f (P&L %s)", symbol, reason, qty, px, pnl)

    def _book_gone(self, symbol: str, rec: dict, state: dict) -> None:
        """The broker shows no position for this lot. Cancel any resting stop first (a sell stop on a flat
        symbol could open a short); if that stop turns out to have filled, book it as the protective stop."""
        from execution import broker
        stop_id = str(rec.get("stop_order_id") or "")
        if stop_id:
            if not broker.cancel_stop_confirmed(symbol, stop_id):
                _page_throttled(rec, f"[{symbol}] breakout lot gone at the broker but its stop {stop_id} could not be "
                                     f"confirmed cancelled — a resting sell stop on a flat symbol; retrying each cycle")
                _save_state(state)
                return
            final = self._final_fill(stop_id)
            qty = int(rec.get("qty") or 0)
            if final is not None and final[0] >= qty > 0 and final[1] > 0:
                self._book_exit(symbol, rec, state, "protective_stop", final[1], final[0])
                return
        close_id = str(rec.get("close_order_id") or "")
        if close_id:
            # a restart between our close submit and its booking: book the REAL close fill, not "unknown"
            # (board execution seat 2026-10-02)
            r = self._settle(close_id)
            if r is not None and r[2] and r[0] > 0 and r[1] > 0:
                reason = str(rec.get("exit_reason_pending") or "close")
                if r[0] >= int(rec.get("qty") or 0):
                    self._book_exit(symbol, rec, state, reason, r[1], r[0])
                    return
                self._book_partial(symbol, rec, state, r[1], r[0], f"{reason}_partial", clear_key="close_order_id")
        _page(f"[{symbol}] breakout lot of {rec.get('qty')} sh is GONE at the broker without a stop fill — "
              f"booked as an unverified external close (P&L unknown); check fills")
        rec.update(status="closed", exit_reason="external_close_unverified", realized_pnl=None, stop_order_id="",
                   close_order_id="", unprotected=False, exit_date=datetime.now(ET).strftime("%Y-%m-%d"))
        _save_state(state)
        # price 0.0 + price_unknown: trade_logger float()s the price, so None would drop this event (Rule D)
        _log("exit", symbol, price=0.0, price_unknown=True, size=int(rec.get("qty") or 0), exit_reason="external_close_unverified",
             realized_pnl=None, trade_id=rec.get("coid"), entry_px_estimated=bool(rec.get("entry_px_estimated")))

    # ── reconcile (every cycle) ───────────────────────────────────────────────
    def _reconcile(self, state: dict) -> None:
        """Bring every held lot to a known state: resolve in-flight entries and unverified exits; for open lots
        book a filled stop, book a partial fill, RE-PROTECT a lot whose stop is gone (cancelled/expired/rejected
        by anyone, never placed, lost to a restart) and book a lot missing at the broker."""
        for s, rec in list(state["positions"].items()):
            try:
                st = rec.get("status")
                if st in _ENTRY_PENDING:
                    self._resolve_entry(s, rec, state)
                    continue
                if st == "exit_unverified":
                    self._resolve_exit_unverified(s, rec, state)
                    continue
                if st != "open":
                    continue
                qty = int(rec.get("qty") or 0)
                stop_id = str(rec.get("stop_order_id") or "")
                if not stop_id or rec.get("unprotected"):
                    self._reprotect(s, rec, state, qty)
                    continue
                r = self._read_order(stop_id)
                if r is None:
                    continue                       # unreadable this cycle: no action (stop presumed resting)
                filled, px, terminal = r
                if filled >= qty > 0 and px > 0:
                    self._book_exit(s, rec, state, "protective_stop", px, filled)
                    continue
                if terminal:
                    # the stop ended without covering the lot (cancelled by another path, expired, rejected)
                    if 0 < filled < qty and px > 0:
                        self._book_partial(s, rec, state, px, filled, "protective_stop_partial", clear_key="stop_order_id")
                    rec["stop_order_id"] = ""
                    _page_throttled(rec, f"[{s}] breakout stop {stop_id} ENDED with {filled}/{qty} filled — re-protecting")
                    self._reprotect(s, rec, state, int(rec.get("qty") or 0))
                    continue
                if 0 < filled < qty and not rec.get("partial_stop_paged"):
                    # a resting stop that partly filled still covers the rest; flag once for review
                    rec["partial_stop_paged"] = True
                    _save_state(state)
                    _page(f"[{s}] breakout stop PARTIALLY filled ({filled}/{qty}) — remainder still stopped; review")
                if self._broker_qty(s) == 0 and self._confirmed_gone(s, rec):
                    self._book_gone(s, rec, state)
            except Exception as e:
                logger.warning("[%s] breakout reconcile failed (no action this cycle): %s", s, e)

    def _resolve_exit_unverified(self, symbol: str, rec: dict, state: dict) -> None:
        """An exit whose stop fill or close fill could not be read: once BOTH are readable and final, book what
        sold (full or partial), then re-protect whatever the broker still holds and return the lot to "open"."""
        qty = int(rec.get("qty") or 0)
        for key, label in (("stop_order_id", "protective_stop"), ("close_order_id", "close")):
            oid = str(rec.get(key) or "")
            if not oid:
                continue
            r = self._settle(oid)
            if r is None or not r[2]:
                return                                   # still unreadable / working: retry next cycle
            filled, px, _ = r
            qty = int(rec.get("qty") or 0)
            if filled >= qty > 0 and px > 0:
                self._book_exit(symbol, rec, state, label, px, filled)
                return
            if 0 < filled < qty and px > 0:
                self._book_partial(symbol, rec, state, px, filled, f"{label}_partial", clear_key=key)
            rec[key] = ""
            _save_state(state)
        if self._reprotect(symbol, rec, state, int(rec.get("qty") or 0), depth=1):
            _page(f"[{symbol}] breakout lot re-protected ({rec.get('qty')} sh, GTC stop {rec.get('stop_px')}) after an unverified exit")

    def status(self) -> dict:
        try:
            return _load_state()
        except Exception as e:
            return {"error": repr(e)}


# One manager per process (run_cycle is single-threaded; the manager holds no per-cycle state in memory).
MANAGER = SwingBreakoutManager()
