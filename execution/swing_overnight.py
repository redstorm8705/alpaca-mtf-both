# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
execution/swing_overnight.py — Swing tier sub-strategy `trend_night`: hold QQQM overnight on trend nights.

Approval: SWING-OVERNIGHT-1030 (logs/design_records/swing_overnight_strength_2026-10-29.md, through amendment v12).
Runs only from its own cron process, run_swing_overnight.py — never on the run_cycle thread.

RULE (spec item → code):
  1 Regime       SPY's prior settled daily close > SMA of the SWING_TREND_NIGHT_SMA_SESSIONS settled closes ending that
                 day (T1 Alpaca daily bars, data.fetcher.fetch_closed_bars). Stale (an Alpaca-calendar session between
                 the last closed bar and today) or missing → no entry, logged.             → regime(), _regime_reading()
  2 Instrument   config.SWING_TREND_NIGHT_SYMBOL (QQQM), long only; reserved to this sub-strategy everywhere
                 (trend_night_symbols(), read by main.py / orphan_manager / stop_protection / unmanaged_positions).
  3 Size         min of four limits, priced every night                                     → size_night(), pooled_budget()
                   budget  floor(b × equity / (price × q)); b = max(owner floor, ¼ Kelly from the POOLED sufficient
                           statistics of the replay prior + every live night — no count switch, v8 item 2a);
                           q = max(QQQ's expanding 99.9th-pct overnight loss, QQQM's own over whatever nights exist)
                   k_room  K × equity − every open position's notional (Invariant #11 limb 2, overnight_capacity)
                   gap     Σ (notional × own 99th-pct overnight loss) of every position + QQQM's ≤ the daily kill × equity
                   buying power
                 The replay prior (replay_prior()) is recomputed every night, point-in-time, from T1 daily bars: QQQ's
                 close→next-open return on every prior trend night, net of SWING_TREND_NIGHT_REPLAY_COST_FRAC.
                 Fewer than two prior nights → "prior unavailable": b = the owner floor, q = the last good measurement.
  4 Entry        `cls` market order in [close − ENTRY_START, close − ENTRY_END] (Alpaca calendar); fallback market
                 order in [close − FALLBACK, close) only after the cls order is broker-terminal rejected/expired (or
                 proven never placed), re-sized from a same-second broker read. Every entry is admitted first by
                 execution.tier_capital_allocator.live_admit (bound with live_bind, released on a terminal non-fill);
                 with TIER_CAPITAL_ALLOCATOR_ENABLED False it approves without a lease (v12 item 2). UNKNOWN blocks every
                 second entry.                                                              → _entry_step(), _fallback()
  POSITION-DRIVEN ORDERS (cold review round 6 — QQQM is reserved, so every QQQM share is trend_night's to protect and
  exit). Truth is the FRESH broker position each pass; trend_night's own fills drive reporting only.
  5 Quantity     TWO-SOURCE MINIMUM (cold review round 7): F = trend_night's own open quantity from its own orders
                 (filled buys − filled sells − recorded "left the account" shares); R = the fresh broker reading.
                 Q = max(0, min(F, R)) is the only quantity any order uses, and R is re-read right before every sell:
                 either source lagging can only undersell (retried next pass) — never oversell, never short. R > F
                 (shares trend_night did not buy) is paged and never traded; F > R held on two reads a poll apart with
                 no order working or changing records the shares as having left the account.  → _quantities()
  6 Protection   outside the exit window: exactly ONE live GTC sell stop for Q, at FIFO entry cost × (1 − q)
                 for shares own fills explain and the frozen last trade × (1 − q) for the rest (share-weighted, rounded
                 DOWN); a size / price change is ONE replace_order_by_id (the shares stay protected), cancel-then-
                 place only after a refused replace. Activities unreadable → Q = max(0, min(R, the listing's own
                 net)) (the stale F only when newer than the newest listed fill); protection and exit run as normal
                 on it; entries and booking held; a page names any unprotected shares.  → _protect(), _quantities()
  7 Exit         from open − SWAP until the entry window: swap the stop for one opg sell of Q (none after the opg
                 cutoff or an opg rejection); at/after the open cancel what rests and market-sell Q; a market sell
                 open after MKT_SELL_SECONDS is cancelled, re-protected, retried; page from the third failure. Every
                 sell is also capped by a fresh read immediately before submit.             → _exit()
  8 Short        R < 0 is foreign (no trend_night sell can exceed min(F, R)): page every 30 min, cancel any resting
                 trend_night sell, submit NOTHING, block entries — the owner's to resolve.
  9 Safety       entry only when F == 0 and R == 0 (re-read fresh right before the submit), nothing working or
                 UNKNOWN, plus the 7% kill state, SWING_TREND_NIGHT_ENABLED, fresh data, the runner lock and admission.
                 Rejections page and back off one poll interval per consecutive refusal, capped at the swap window's
                 length; an UNKNOWN submit waits one poll interval; attempts are bounded and paged early. Paper
                 assertion before every submit.
  Runtime        stays alive and polls every SWING_TREND_NIGHT_POLL_SECONDS inside the order windows.  → TrendNight.run()
  Logging        every transition → trade_events.jsonl (tier swing, strategy trend_night); end-of-night summary.
  Booking        REPORTING ONLY, once confirmed flat: sell fills allocated FIFO to the nights' entry fills; shares no own
                 sell explains are paged and recorded, never acted on.                       → _book_step()
  Result check   `trend_night_audit` at open + RESULT_CHECK (flat, no exit order resting) and at the close (flat at some
                 point in today's exit window), booked P&L == FIFO P&L from Alpaca fill activities. → _result_step()
  Reviews (PAGE ONLY — nothing here switches the strategy off; that stays the owner's SWING_TREND_NIGHT_ENABLED):
                 edge check   live mean night return < prior mean − mixture_halfwidth(σ², n, ρ, α, N_prior)  (v10 item 2)
                 night loss   a night losing more than qty × entry × q + a cent a share
                 fill cost    live mean realized round-trip cost > prior mean + mixture_halfwidth(...) on the quoted-
                              spread cost sample of the last SWING_TREND_NIGHT_SPREAD_SESSIONS sessions (v11 item 1)
                                                                                     → _after_booking(), _fill_cost_step()

ORDER IDS (v9 item 1 / v10 item 1 / v11 item 3): `IN-QQQM-<b|s>-<night_close_epoch_ms>-tn<step><n>`, built with
execution.ownership_guard.make_coid("intraday", ...) so ownership_guard.tier_of_coid() and the ledger credit the Swing
tier exactly as for Swing-breakout orders. <night_close_epoch_ms> is the entry session's close; <step> is ecls / emkt
(the two entries) or stop / opg / mkt (sells); <n> is the attempt. parse_coid() is fully anchored
and is the only way an order is recognised as trend_night's. Protective / exit orders carry the newest night
with an entry fill (else today's) for attribution and id uniqueness only.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
APPROVAL_ID = "SWING-OVERNIGHT-1030"
TIER = "swing"
OWNER_TIER = "intraday"          # ownership_guard tier code IN — the Swing tier's ledger tier
STRATEGY = "trend_night"
LEDGER_LOT = "swing:trend_night"
_DEFAULT_STATE = Path(__file__).resolve().parent.parent / "data" / "state" / "swing_trend_night.json"

# Order steps. Entries are buys; every other step is a sell (the parser rejects a side that does not match its step).
_ENTRY_CLS, _ENTRY_MKT = "ecls", "emkt"
_ENTRY_STEPS = frozenset({_ENTRY_CLS, _ENTRY_MKT})
_SELL_STEPS = frozenset({"stop", "opg", "mkt"})
_ALL_STEPS = _ENTRY_STEPS | _SELL_STEPS
_LIVE_SINCE = date.fromisoformat("2026-10-29")   # the strategy's first possible night (v11 item 3: older epochs rejected)
_API_COID_CHARS = 128                            # Alpaca's client_order_id length limit
_ATTEMPT_DIGITS = 4                              # digits of <n> the order-id pattern accepts (attempts 1 … 9999)
_UNRESOLVED_POLL_ATTEMPTS = 3                    # passes a sell may stay UNKNOWN (shares held) before it pages

# Broker-final statuses (spec item 6: canceled / rejected / expired / filled). "replaced" is also final for THAT order:
# its successor carries its own trend_night client id and is listed separately, so it is tracked on its own.
_TERMINAL = frozenset({"filled", "canceled", "cancelled", "rejected", "expired", "replaced"})
_TRANSITIONAL = frozenset({"pending_cancel", "pending_replace"})
_ACTIVE_NIGHT = ("entry_pending", "holding", "exiting")
_TICK_DIGITS = 2                 # US equities above $1 trade in whole cents
_PAGE_SIZE = 500                 # Alpaca /v2/orders maximum page
_FILL_PAGE_SIZE = 100            # Alpaca /v2/account/activities maximum page
_PAGE_ATTEMPTS = 50
_RECONCILE_RETRIES = 25          # immediate re-passes in one tick loop after an action (no wait between passes)


def _cfg(name: str) -> Any:
    return getattr(config, name)


def fmt_pt(dt: datetime) -> str:
    """User-facing time in PT (CLAUDE.md Rule 8)."""
    return dt.astimezone(PT).strftime("%Y-%m-%d %I:%M:%S %p PT")


def _under_test() -> bool:
    try:
        from execution.broker import _under_test as _bt
        return bool(_bt())
    except Exception:  # noqa: BLE001 — cannot tell → behave as a test (never touch production state)
        return True


# ── order views ────────────────────────────────────────────────────────────────────────────────────
def _enum(v: Any) -> str:
    return str(getattr(v, "value", v) or "").lower().split(".")[-1]


def _num(v: Any) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _aware(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            # Alpaca stamps carry nanoseconds; datetime holds microseconds — drop the digits it cannot hold
            v = re.sub(r"(\.\d{6})\d+", r"\1", v)
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo is not None else d.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def order_view(o: Any) -> dict:
    """A broker order (SDK model or dict) as a plain dict with normalised fields."""
    def g(k: str) -> Any:
        return o.get(k) if isinstance(o, dict) else getattr(o, k, None)
    avg = g("filled_avg_price")
    sp = g("stop_price")
    return {
        "id": str(g("id") or ""), "coid": str(g("client_order_id") or ""), "symbol": str(g("symbol") or ""),
        "side": _enum(g("side")), "type": _enum(g("order_type") or g("type")), "tif": _enum(g("time_in_force")),
        "status": _enum(g("status")), "qty": int(_num(g("qty"))), "filled_qty": int(_num(g("filled_qty"))),
        "filled_avg_price": (_num(avg) or None) if avg is not None else None,
        "stop_price": (_num(sp) or None) if sp is not None else None,
        "submitted_at": _aware(g("submitted_at") or g("created_at")),
        "filled_at": _aware(g("filled_at")),
    }


def remaining(v: dict) -> int:
    return max(0, int(v["qty"]) - int(v["filled_qty"]))


def is_live(v: dict) -> bool:
    return v["status"] not in _TERMINAL


def night_key(d: date) -> str:
    return d.strftime("%Y%m%d")


def epoch_ms(dt: datetime) -> int:
    """Milliseconds since the Unix epoch of an aware datetime."""
    secs = dt.timestamp()
    return int(round(secs * 1000))


def _ms_to_et(close_ms: int) -> datetime:
    epoch_secs = close_ms / 1000
    return datetime.fromtimestamp(epoch_secs, UTC).astimezone(ET)


def _step_side(step: str) -> str:
    return "b" if step in _ENTRY_STEPS else "s"


def build_coid(night_close: datetime, step: str, attempt: int) -> str:
    """`IN-QQQM-<b|s>-<night_close_epoch_ms>-tn<step><n>` via ownership_guard.make_coid (v9 item 1)."""
    from execution.ownership_guard import make_coid
    if step not in _ALL_STEPS or not attempt_fits(attempt):
        raise ValueError(f"bad trend_night order step/attempt {step!r}/{attempt!r}")
    return make_coid(OWNER_TIER, str(_cfg("SWING_TREND_NIGHT_SYMBOL")), _step_side(step), epoch_ms(night_close),
                     f"tn{step}{int(attempt)}")


def _coid_pattern() -> "re.Pattern[str]":
    steps = "|".join(sorted(_ALL_STEPS))
    sym = re.escape(str(_cfg("SWING_TREND_NIGHT_SYMBOL")))
    more = _ATTEMPT_DIGITS - 1
    return re.compile(rf"\AIN-{sym}-(?P<side>[bs])-(?P<epoch>[1-9][0-9]{{12}})-tn(?P<step>{steps})(?P<n>[1-9][0-9]{{0,{more}}})\Z")


class AttemptsExhausted(RuntimeError):
    """The next attempt number would not fit the order-id pattern (_ATTEMPT_DIGITS digits)."""


def attempt_fits(attempt: int) -> bool:
    return int(attempt) >= 1 and len(str(int(attempt))) <= _ATTEMPT_DIGITS


def parse_coid(coid: str, now: Optional[datetime] = None) -> Optional[dict]:
    """A trend_night order id → {night, step, attempt, side, close_ms}; None for any other id (v10 item 1, v11 item 3).
    Rejects: a different prefix or symbol, a side that does not match its step, an unknown step, a malformed or zero
    attempt, an epoch before the strategy's first night or more than one day after `now`, an id over Alpaca's limit."""
    if not isinstance(coid, str) or len(coid) > _API_COID_CHARS:
        return None
    m = _coid_pattern().match(coid)
    if m is None:
        return None
    step = m.group("step")
    if m.group("side") != _step_side(step):
        return None
    ms = int(m.group("epoch"))
    try:
        at = _ms_to_et(ms)
    except (OverflowError, OSError, ValueError):
        return None
    ref = (now or datetime.now(ET)).astimezone(ET)
    if at.date() < _LIVE_SINCE or at > ref + timedelta(days=1):
        return None
    return {"night": night_key(at.date()), "step": step, "attempt": int(m.group("n")), "side": m.group("side"),
            "close_ms": ms}


def _step(v: dict) -> str:
    p = v.get("parsed")
    return p["step"] if p else ""


def _night_of(v: dict) -> str:
    p = v.get("parsed")
    return p["night"] if p else ""


def _is_entry(v: dict) -> bool:
    return _step(v) in _ENTRY_STEPS


def _tick() -> Decimal:
    return Decimal(1).scaleb(-_TICK_DIGITS)


def stop_target(avg_entry: Optional[float], q: Optional[float]) -> Optional[Decimal]:
    """avg entry fill × (1 − q), rounded DOWN to the tick, in Decimal (spec item 4/5). None on any bad input."""
    try:
        if avg_entry is None or q is None or not (avg_entry > 0 and 0 < q < 1):
            return None
        v = (Decimal(str(avg_entry)) * (Decimal(1) - Decimal(str(q)))).quantize(_tick(), rounding=ROUND_DOWN)
        return v if v > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _price_eq(stop_price: Optional[float], target: Decimal) -> bool:
    if stop_price is None:
        return False
    try:
        return Decimal(str(stop_price)).quantize(_tick()) == target
    except (InvalidOperation, ValueError):
        return False


# ── pure decision functions ────────────────────────────────────────────────────────────────────────
def regime(closes: Any, n: int) -> Optional[dict]:
    """SPY's last settled close vs the simple average of the `n` settled closes ending that day. None when there are
    fewer than n closes or a value is not finite."""
    try:
        c = closes.astype(float)
        if len(c) < n or n < 1:
            return None
        last = float(c.iloc[-1])
        sma = float(c.tail(n).mean())
        if not (math.isfinite(last) and math.isfinite(sma)):
            return None
        return {"close": last, "sma": sma, "on": last > sma, "bar_date": str(c.index[-1])[:10],
                "first_sma_bar": str(c.tail(n).index[0])[:10]}
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_night: regime unreadable: %s", e)
        return None


def overnight_returns(df: Any) -> Any:
    """open[t] / close[t−1] − 1 over consecutive settled daily bars (finite values only)."""
    import numpy as np
    o = df["open"].astype(float)
    c = df["close"].astype(float)
    r = (o / c.shift(1) - 1).dropna()
    return r[np.isfinite(r.to_numpy())]


def loss_quantile(rets: Any, level: float) -> Optional[float]:
    """The `level` quantile of overnight LOSS (a positive fraction), point-in-time over every night given."""
    try:
        if rets is None or len(rets) == 0:
            return None
        v = -float(rets.quantile(1 - level))
        return v if math.isfinite(v) and v > 0 else None
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_night: loss quantile failed: %s", e)
        return None


def gain_quantile(rets: Any, level: float) -> Optional[float]:
    """The adverse overnight move for a SHORT: the `level` quantile of overnight gains."""
    try:
        if rets is None or len(rets) == 0:
            return None
        v = float(rets.quantile(level))
        return v if math.isfinite(v) and v > 0 else None
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_night: gain quantile failed: %s", e)
        return None


def _finite_rets(closed: list, key: str) -> list:
    out = []
    for r in closed or []:
        if isinstance(r, dict) and r.get(key) is not None:
            try:
                x = float(r[key])
            except (TypeError, ValueError):
                continue
            if math.isfinite(x):
                out.append(x)
    return out


def replay_prior(spy: Any, qqq: Any, sma_sessions: int, cost_frac: float) -> dict:
    """The replay prior, point-in-time (v8 item 2, v10 item 2). For every day d with a next session: SPY's state on
    d−1 (settled close > SMA of the `sma_sessions` closes ending d−1 — the live rule's prior settled close) decides
    whether the night after d is a trend night; that night's return is QQQ open[d+1] / close[d] − 1 − `cost_frac`.
    Returns {"ok", "n", "mean", "var", "sd", "sum", "sumsq", "n_star", "rho" (absent), "first", "last"}; ok is False
    with a "why" when fewer than two prior trend nights exist (prior unavailable)."""
    try:
        import numpy as np
        import pandas as pd
        c = spy["close"].astype(float)
        sma = c.rolling(int(sma_sessions)).mean()
        state = (c > sma) & sma.notna()
        prev_on = state.shift(1, fill_value=False).astype(bool)
        qo, qc = qqq["open"].astype(float), qqq["close"].astype(float)
        night = qo.shift(-1) / qc - 1 - float(cost_frac)
        on = prev_on.reindex(qc.index, fill_value=False).astype(bool)
        rets = night[on.to_numpy() & np.isfinite(night.to_numpy())]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"replay prior incomputable: {e!r}", "n": 0}
    if len(rets) < 2:
        return {"ok": False, "why": f"prior unavailable: {len(rets)} prior trend night(s)", "n": int(len(rets))}
    vals = [float(x) for x in rets.to_numpy()]
    per_year = pd.Series(vals, index=pd.DatetimeIndex(rets.index)).groupby(pd.DatetimeIndex(rets.index).year).size()
    var = statistics.variance(vals)
    return {"ok": True, "n": len(vals), "mean": statistics.fmean(vals), "var": var, "sd": math.sqrt(var),
            "sum": math.fsum(vals), "sumsq": math.fsum(x ** 2 for x in vals),
            "n_star": float(per_year.median()), "first": str(rets.index[0])[:10], "last": str(rets.index[-1])[:10]}


def mixture_rho(n_star: float, alpha: float) -> Optional[float]:
    """ρ = n* / (2·ln(1/α) + ln(1 + 2·ln(1/α))) — the normal-mixture tuning that makes the boundary tightest near n*
    (Howard, Ramdas, McAuliffe & Sekhon 2021; v10 item 2)."""
    try:
        if not (n_star > 0 and 0 < alpha < 1):
            return None
        two_l = math.log(1 / (alpha * alpha))          # 2·ln(1/α)
        return n_star / (two_l + math.log(1 + two_l))
    except (TypeError, ValueError):
        return None


def mixture_halfwidth(var: float, n: int, rho: float, alpha: float, n_prior: int) -> Optional[float]:
    """sqrt( σ²·2(n+ρ)/n² · ln( sqrt((n+ρ)/ρ) / α ) + σ²/N_prior ) — the normal-mixture confidence-sequence half-width
    plus the replay mean's own sampling variance (v10 item 2; Robbins 1970; Howard et al. 2021). An approximation for
    about α under a normal model, not a guarantee. None on a bad input."""
    try:
        if not (var >= 0 and n >= 1 and rho > 0 and 0 < alpha < 1 and n_prior >= 1):
            return None
        span = n + rho
        inner = var * (span + span) / n ** 2 * math.log(math.sqrt(span / rho) / alpha) + var / n_prior
        return math.sqrt(inner) if inner >= 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def pooled_budget(prior: Optional[dict], live_rets: list, q: float) -> dict:
    """Worst-night budget b (fraction of equity) = max(owner floor, ¼ Kelly), Kelly from the POOLED sufficient
    statistics of the replay prior and every live night (count, sum, sum of squares — the pooled variance includes the
    between-means term). No count switch (v8 item 2a). Kelly leverage L* = mean / var; ¼ Kelly in worst-night terms
    is b = KELLY_FRACTION × L* × q. A non-positive pooled mean or variance gives the floor; the variance is clamped at
    zero against round-off (v9 item 4). Prior unavailable → the owner floor (v11 item 4)."""
    floor_b = float(_cfg("SWING_TREND_NIGHT_MIN_BUDGET_PCT"))
    out: dict[str, Any] = {"floor": floor_b, "live_nights": len(live_rets), "kelly_b": None, "source": "owner_floor"}
    if not prior or not prior.get("ok"):
        out.update(b=floor_b, prior="unavailable", why=(prior or {}).get("why"))
        return out
    cnt = int(prior["n"]) + len(live_rets)
    tot = float(prior["sum"]) + math.fsum(live_rets)
    sq = float(prior["sumsq"]) + math.fsum(x ** 2 for x in live_rets)
    mean = tot / cnt
    var = max(0.0, (sq - tot * mean) / (cnt - 1))
    out.update(pooled_nights=cnt, prior_nights=int(prior["n"]), pooled_mean=mean, pooled_var=var)
    kb = 0.0
    if mean > 0 and var > 0 and q > 0:
        kb = float(_cfg("SWING_TREND_NIGHT_KELLY_FRACTION")) * mean / var * q
        out["kelly_b"] = kb
    out["b"] = max(floor_b, kb)
    if kb > floor_b:
        out["source"] = "quarter_kelly_pooled"
    return out


def size_night(equity: float, price: float, q: float, b: float, k: float, gross_notional: float,
               gap_book_loss: float, kill_pct: float, buying_power: float) -> tuple[int, dict]:
    """Whole shares = min of the four limits (spec item 3). Any bad input → 0 (skip)."""
    vals = (equity, price, q, b, k, gross_notional, gap_book_loss, kill_pct, buying_power)
    if not all(isinstance(x, (int, float)) and math.isfinite(float(x)) for x in vals):
        return 0, {"why": "non-finite sizing input"}
    if equity <= 0 or price <= 0 or q <= 0 or b <= 0 or k <= 0 or kill_pct <= 0:
        return 0, {"why": "non-positive sizing input"}
    room = k * equity - gross_notional
    gap_room = kill_pct * equity - gap_book_loss
    limits = {
        "budget": math.floor(b * equity / (price * q)),
        "k_room": math.floor(room / price) if room > 0 else 0,
        "gap": math.floor(gap_room / (price * q)) if gap_room > 0 else 0,
        "buying_power": math.floor(buying_power / price) if buying_power > 0 else 0,
    }
    bound = min(limits, key=lambda name: limits[name])
    qty = max(0, int(limits[bound]))
    info = {"limits": limits, "bound_by": bound, "qty": qty, "equity": equity, "price": price, "q": q, "b": b,
            "k": k, "gross_notional": round(gross_notional, 2), "k_room_usd": round(room, 2),
            "gap_book_loss": round(gap_book_loss, 2), "kill_pct": kill_pct, "gap_room_usd": round(gap_room, 2),
            "buying_power": round(buying_power, 2)}
    return qty, info


@dataclass
class _HeldView:
    """A position as it will stand after the close (symbol, market_value, asset_class) — what _gap_book reads."""
    symbol: str
    market_value: float
    asset_class: Any = ""


def post_flatten_positions(positions: list, day_lots: dict) -> tuple[list, list]:
    """TREND-NIGHT-DAYLOTS-1103: each position as it stands once the Day tier closes its own same-day lots before the
    close. `day_lots` = {SYMBOL: signed shares the Day tier will flatten (+ long, - short)}. Remaining shares =
    broker shares - Day shares; a same-side Day record larger than the position leaves the position whole (it may be
    a co-held lot whose Day exit is unbooked); an opposite-side Day lot ADDS to what remains (closing it grows the net).
    Priced at the position's own market_value / qty. A position whose qty or value is unreadable is kept whole.
    Returns (views, adjustments) — adjustments name every position that changed, for the sizing record."""
    out: list = []
    adj: list = []
    for p in positions:
        sym = str(getattr(p, "symbol", "") or "")
        mv = _num(getattr(p, "market_value", 0))
        ac = getattr(p, "asset_class", "")
        d = day_lots.get(sym.upper()) if sym else None
        qty = _num(getattr(p, "qty", 0))
        if not d or qty == 0 or mv == 0 or "option" in _enum(ac):
            out.append(_HeldView(sym, mv, ac))
            continue
        px = mv / qty                                   # signed qty × signed value → positive price
        rest = qty - d
        if qty * d > 0 and qty * rest < 0:
            # a same-side Day record LARGER than the broker position cannot be told apart from a co-held lot whose
            # Day exit is not booked yet: count the position whole (board Taleb 2026-11-03, never-mask-a-loss)
            out.append(_HeldView(sym, mv, ac))
            continue
        out.append(_HeldView(sym, rest * px, ac))
        adj.append({"symbol": sym, "broker_qty": qty, "day_qty": d, "after_close_qty": rest,
                    "notional_before": round(abs(mv), 2), "notional_after": round(abs(rest * px), 2)})
    return out, adj


def _never_promoted_long(sym: str) -> bool:
    """True only when a Day LONG on `sym` provably cannot be promoted to the Swing tier at 3:56 PM ET (promotion runs
    AFTER this sizing read and does not see trend_night's working entry — board Thorp 2026-11-03): promotion is off,
    or `sym` is a known leveraged/inverse ETF (day_promotion never promotes those). Any read error → False (the lot
    keeps counting overnight)."""
    try:
        from execution import day_promotion as dp
        if not dp.enabled():
            return True
        s = str(sym or "").upper()
        if s in set(getattr(config, "LEVERAGED_3X_TICKERS", set()) or set()):
            return True
        from strategy import day_tier_leverage as lev
        return lev.exposure_sign(s, "long")[0] != s
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_night: promotion eligibility of %s unreadable, its Day lot keeps counting: %s", sym, e)
        return False


def day_tier_flatten_lots() -> Optional[dict]:
    """{SYMBOL: signed shares} the Day tier will close before the close: its EOD force-flat target set (which already
    leaves out lots marked promote_pending), restricted to lots that cannot still be promoted later today — shorts
    (v1 promotes longs only) and _never_promoted_long() longs. A promotable long keeps counting overnight. None when
    the Day tier state is unreadable or the tier is off — the caller then counts every position whole (pre-1103)."""
    try:
        from execution import day_trade_manager as dtm
        if not dtm._enabled():
            return None
        out: dict = {}
        # a fill the Day tier has not verified may not be its own shares: never netted (board Taleb 2026-11-03)
        unverified = {str(v.get("symbol") or "").upper() for k, v in dtm._load_state().items()
                      if k.startswith("entry::") and isinstance(v, dict) and v.get("state") == "fill_unverified"}
        for sym, t in dtm._flatten_targets().items():
            n = abs(int(float(t.get("qty") or 0)))
            if sym and n > 0 and str(sym).upper() not in unverified:
                short = str(t.get("side") or "long").lower().startswith("short")
                if not short and not _never_promoted_long(str(sym)):
                    continue
                sign = -1 if short else 1
                out[str(sym).upper()] = out.get(str(sym).upper(), 0) + sign * n
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_night: Day-tier lots unreadable, counting every position whole: %s", e)
        return None


def fifo_pnl(fills: list) -> Optional[float]:
    """Realised P&L of a long-only sequence of fills (dicts: side, qty, price, time) by FIFO; None on a sell with no
    matching buy (that would be a short — never this strategy)."""
    lots: list = []
    pnl = 0.0
    for f in sorted(fills, key=lambda x: str(x.get("transaction_time") or x.get("time") or "")):
        q, px = _num(f.get("qty")), _num(f.get("price"))
        if q <= 0 or px <= 0:
            continue
        if str(f.get("side", "")).lower().startswith("buy"):
            lots.append([q, px])
            continue
        while q > 0:
            if not lots:
                return None
            take = min(q, lots[0][0])
            pnl += take * (px - lots[0][1])
            lots[0][0] -= take
            q -= take
            if lots[0][0] <= 0:
                lots.pop(0)
    return pnl


def night_positions(views: dict) -> dict:
    """{night: (remaining shares, average entry price of THOSE shares or None)} — each night priced only from its own
    entry fills, FIFO (entry orders oldest fill first) against that night's own sell fills. Never a cross-night average
    (cold review #2). The price is None when any entry order still contributing remaining shares has no readable fill
    price."""
    out: dict = {}
    nights = {_night_of(v) for v in views.values() if _night_of(v)}
    epoch = datetime.min.replace(tzinfo=UTC)
    for night in nights:
        mine = [v for v in views.values() if _night_of(v) == night]
        lots = [[v["filled_qty"], v["filled_avg_price"]]
                for v in sorted((v for v in mine if _is_entry(v) and v["filled_qty"] > 0),
                                key=lambda v: (v["filled_at"] or v["submitted_at"] or epoch))]
        sold = sum(v["filled_qty"] for v in mine if v["side"] == "sell")
        for lot in lots:
            take = min(sold, lot[0])
            lot[0] -= take
            sold -= take
        rest = [lot for lot in lots if lot[0] > 0]
        qty = sum(lot[0] for lot in rest) - sold
        if not rest or any(lot[1] is None for lot in rest):
            out[night] = (qty, None)
        else:
            out[night] = (qty, math.fsum(lot[0] * float(lot[1]) for lot in rest) / sum(lot[0] for lot in rest))
    return out


def fifo_lots(views: dict) -> list:
    """[(shares, entry price or None)] still held by trend_night's own fills, all nights together: entry fills oldest
    first, minus every trend_night sell fill, FIFO. Used ONLY to price the stop (rule 2); never to size an order."""
    epoch = datetime.min.replace(tzinfo=UTC)
    lots = [[v["filled_qty"], v["filled_avg_price"]]
            for v in sorted((v for v in views.values() if _is_entry(v) and v["filled_qty"] > 0),
                            key=lambda v: (v["filled_at"] or v["submitted_at"] or epoch))]
    sold = sum(v["filled_qty"] for v in views.values() if v["side"] == "sell")
    for lot in lots:
        take = min(sold, lot[0])
        lot[0] -= take
        sold -= take
    return [(int(q), px) for q, px in lots if q > 0]


# ── kill-switch state (read-only) ──────────────────────────────────────────────────────────────────
def kill_halted(today: str) -> tuple[bool, str]:
    """True when the durable 7% daily kill (execution.risk_manager) has tripped or halted entries today. An unreadable
    state FAILS CLOSED (entry blocked) — the exit never reads this."""
    try:
        from execution.risk_manager import _load_kill_state
        st = _load_kill_state()
    except Exception as e:  # noqa: BLE001
        return True, f"kill state unreadable (fail-closed): {e!r}"
    if st.get("date") == today and (st.get("killed") or st.get("halt_entries")):
        return True, "daily kill switch / entry halt active"
    return False, ""


_NOT_FOUND_CODE = "40410000"                     # Alpaca's own not-found error code


def _is_not_found(e: Exception) -> bool:
    """True only for a genuine not-found answer: HTTP status 404 on the error object, or Alpaca's 40410000 error code
    as the response's `code` field. A message that merely contains '404' (a timeout text, an order id, a proxy page)
    is NOT proof of absence (cold review nit 4)."""
    status_code = getattr(e, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(e, "response", None), "status_code", None)
    if status_code == 404:
        return True
    api_code = getattr(e, "code", None)
    if api_code is not None and str(api_code) == _NOT_FOUND_CODE:
        return True
    try:
        body = json.loads(str(e))
    except (TypeError, ValueError):
        return False
    return isinstance(body, dict) and str(body.get("code")) == _NOT_FOUND_CODE


def page_activities(client: Any, after: datetime) -> list:
    """GET /account/activities/FILL after `after`, oldest first, following page_token until a short page."""
    out: list = []
    token: Optional[str] = None
    seen: set = set()
    while True:
        params: dict = {"after": after.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                        "direction": "asc", "page_size": _FILL_PAGE_SIZE}
        if token:
            params["page_token"] = token
        page = client.get("/account/activities/FILL", params) or []
        if not isinstance(page, list):
            raise RuntimeError("fill activities returned a non-list page")
        out.extend(page)
        if len(page) < _FILL_PAGE_SIZE:
            return out
        token = str(page[-1].get("id") or "")
        if not token or token in seen:
            raise RuntimeError("fill activity paging made no progress")
        seen.add(token)


# ── broker + data adapters (T1 only; the broker's own trading client — never a new one) ───────────
class TNBroker:
    """Every call goes through execution.broker (TradingClient is created ONLY there; in a test process it refuses to
    build one). Methods RAISE on transport/API failure — the engine turns that into UNKNOWN / unreadable."""

    @staticmethod
    def _client() -> Any:
        from execution import broker
        return broker.get_trading_client()

    def account(self) -> Any:
        from execution import broker
        return broker.get_account()

    def positions(self) -> list:
        from execution import broker
        return list(broker.get_open_positions() or [])

    def position_qty(self, symbol: str) -> int:
        """Signed shares held (0 = flat). Raises when unreadable."""
        from execution import broker
        pos = broker.get_open_position(symbol)
        return 0 if pos is None else int(_num(getattr(pos, "qty", 0)))

    def list_orders(self, symbol: str, after: Optional[datetime]) -> list:
        """Every order on `symbol` submitted after `after` (all statuses), fully paged newest-first."""
        c = self._client()
        from alpaca.common.enums import Sort
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        out: dict = {}
        until: Optional[datetime] = None
        for _ in range(_PAGE_ATTEMPTS):
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[symbol], limit=_PAGE_SIZE, after=after,
                                   until=until, direction=Sort.DESC, nested=False)
            page = list(c.get_orders(filter=req) or [])
            before = len(out)
            for o in page:
                out[str(getattr(o, "id", ""))] = o
            if len(page) < _PAGE_SIZE:
                return list(out.values())
            if len(out) == before:
                raise RuntimeError("TN order paging made no progress")
            stamps = [s for s in (_aware(getattr(o, "submitted_at", None) or getattr(o, "created_at", None))
                                  for o in page) if s is not None]
            if not stamps:
                raise RuntimeError("TN order page has no timestamps to page by")
            until = min(stamps) + timedelta(microseconds=1)
        raise RuntimeError("TN order paging did not terminate")

    def get_by_coid(self, coid: str) -> tuple[str, Any]:
        """('found', order) | ('absent', None) — Alpaca says no such order | ('unknown', None) — lookup failed."""
        c = self._client()
        try:
            return "found", c.get_order_by_client_id(coid)
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                return "absent", None
            logger.warning("trend_night: client-id lookup %s failed: %s", coid, e)
            return "unknown", None

    @staticmethod
    def _classify(e: Exception) -> tuple[str, Any]:
        from execution.broker import _is_explicit_alpaca_reject
        err = str(e)
        if "40910000" in err or "duplicate client order" in err.lower():
            return "unknown", err            # an order with this id may already exist → resolve by client id
        if _is_explicit_alpaca_reject(err):
            return "rejected", err
        return "unknown", err

    def submit(self, symbol: str, coid: str, side: str, qty: int, tif: str,
               stop_price: Optional[Decimal] = None) -> tuple[str, Any]:
        """('ack', order) | ('rejected', err) — Alpaca refused before accepting | ('unknown', err)."""
        c = self._client()
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest
        from execution.order_admission import symbol_order_gate
        osd = OrderSide.BUY if side == "buy" else OrderSide.SELL
        req: Any
        if stop_price is not None:
            req = StopOrderRequest(symbol=symbol, qty=int(qty), side=osd, time_in_force=TimeInForce.GTC,
                                   stop_price=float(stop_price), client_order_id=coid)
        else:
            req = MarketOrderRequest(symbol=symbol, qty=int(qty), side=osd, time_in_force=TimeInForce(tif),
                                     client_order_id=coid)
        try:
            with symbol_order_gate(symbol):
                o = c.submit_order(req)
        except Exception as e:  # noqa: BLE001
            return self._classify(e)
        return ("ack", o) if getattr(o, "id", None) else ("unknown", "no order id in the response")

    def replace(self, symbol: str, order_id: str, qty: int, stop_price: Decimal, coid: str) -> tuple[str, Any]:
        """Quantity and stop price replaced together in one request (spec item 5)."""
        c = self._client()
        from alpaca.trading.requests import ReplaceOrderRequest
        from execution.order_admission import symbol_order_gate
        try:
            with symbol_order_gate(symbol):
                o = c.replace_order_by_id(order_id, ReplaceOrderRequest(qty=int(qty), stop_price=float(stop_price),
                                                                        client_order_id=coid))
        except Exception as e:  # noqa: BLE001
            return self._classify(e)
        return ("ack", o) if getattr(o, "id", None) else ("unknown", "no order id in the response")

    def cancel(self, order_id: str) -> bool:
        """A cancel REQUEST (never proof of anything — the status listing decides)."""
        from execution import broker
        return bool(broker.cancel_order(order_id))

    def clock(self) -> dict:
        from execution import broker
        return broker.get_clock()

    def calendar(self, start: date, end: date) -> list:
        """[(session date, open ET, close ET)] from the Alpaca calendar."""
        c = self._client()
        from alpaca.trading.requests import GetCalendarRequest
        out = []
        for row in c.get_calendar(GetCalendarRequest(start=start, end=end)) or []:
            d = getattr(row, "date", None)
            op, cl = getattr(row, "open", None), getattr(row, "close", None)
            if isinstance(d, date) and isinstance(op, datetime) and isinstance(cl, datetime):
                out.append((d, op.replace(tzinfo=ET) if op.tzinfo is None else op.astimezone(ET),
                            cl.replace(tzinfo=ET) if cl.tzinfo is None else cl.astimezone(ET)))
        return out

    def fills(self, after: datetime) -> list:
        """FILL activities after `after`, oldest first, paged with page_token until exhausted (no row cap). A page that
        repeats its last id (no progress) raises instead of looping."""
        return page_activities(self._client(), after)

    def get_order(self, order_id: str) -> tuple[str, Any]:
        """('found', order) | ('absent', None) — a genuine not-found | ('unknown', None) — the lookup failed."""
        c = self._client()
        try:
            return "found", c.get_order_by_id(order_id)
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                return "absent", None
            logger.warning("trend_night: order lookup %s failed: %s", order_id, e)
            return "unknown", None


class TNData:
    """T1 Alpaca market data (data.fetcher / data.alpaca_data only)."""

    @staticmethod
    def spy_closed(symbol: str, n: int) -> Any:
        from data.fetcher import fetch_closed_bars
        return fetch_closed_bars(symbol, config.TF_DAILY, num_bars=n).df

    @staticmethod
    def daily_history(symbol: str, before: date) -> Any:
        """Every settled split-adjusted daily bar of `symbol` before `before` (ET dates), T1 SIP."""
        import pandas as pd
        from data.fetcher import fetch_bars_window
        end = datetime(before.year, before.month, before.day, tzinfo=ET).astimezone(UTC)
        df = fetch_bars_window(symbol, config.TF_DAILY, datetime.fromtimestamp(0, UTC), end, feed="sip",
                               adjustment="split")
        if df is None or df.empty:
            return pd.DataFrame()
        idx = pd.to_datetime(df.index)
        idx = idx.tz_localize(UTC) if idx.tz is None else idx
        df = df.copy()
        df.index = idx.tz_convert(ET).tz_localize(None).normalize()
        return df[df.index < pd.Timestamp(before)]

    @staticmethod
    def last_price(symbol: str) -> Optional[float]:
        from data.alpaca_data import get_latest_trade
        p = get_latest_trade(symbol)
        return float(p) if p is not None and math.isfinite(float(p)) and float(p) > 0 else None

    @staticmethod
    def spreads(symbol: str, start: datetime, end: datetime) -> Optional[list]:
        """Quoted bid-ask spreads ($) in [start, end] from SIP quotes (T1, data.alpaca_data); None when unreadable."""
        from data.alpaca_data import get_quote_spreads
        return get_quote_spreads(symbol, start, end, feed="sip")


def _emit(event: str, **kw: Any) -> None:
    try:
        import trade_logger
        trade_logger.log_event(event, data_source="alpaca_data", **kw)
    except Exception as e:  # noqa: BLE001
        logger.warning("trend_night: trade_events write failed (%s): %s", event, e)


def _send_page(msg: str) -> None:
    logger.critical(msg)
    try:
        from alerts import send_slack
        send_slack("🚨 SWING trend_night\n" + msg)
    except Exception as e:  # noqa: BLE001
        logger.error("trend_night: page not sent: %s", e)


# ── engine ─────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Session:
    day: date
    is_session: bool
    open: Optional[datetime]
    close: Optional[datetime]
    source: str                      # calendar | broker_clock | system_clock | none


@dataclass
class Recon:
    ok: bool
    reason: str = ""
    views: dict = field(default_factory=dict)        # coid → order view (trend_night orders only)
    position: int = 0                                # R: the fresh broker QQQM reading this pass
    own: int = 0                                     # F: trend_night's own open quantity from its own orders
    held: int = 0                                    # Q = max(0, min(F, R)) — the only quantity any order uses
    short: int = 0                                   # shares the broker shows short (a foreign short; never traded)
    foreign: int = 0                                 # confirmed shares beyond F (the owner's; never traded)
    derived: dict = field(default_factory=dict)      # this pass's derivation from the fill activities (reporting)
    acts_ok: bool = True                             # False: activities unreadable this pass (degraded, rule 1)
    unclassified: int = 0                            # activities whose order could not be looked up this pass
    last_act_time: Optional[str] = None
    live_sells: list = field(default_factory=list)
    live_buys: list = field(default_factory=list)
    unknown_sell_qty: int = 0
    unknown_buys: int = 0

    @property
    def unknowns(self) -> bool:
        return self.unknown_sell_qty > 0 or self.unknown_buys > 0


@dataclass
class TickOut:
    acted: bool = False
    keep_alive: bool = False
    note: str = ""


class TrendNight:
    """One instance per runner process. tick() is one full pass; run() loops it inside the order windows."""

    def __init__(self, broker: Any = None, data: Any = None, *, state_path: Optional[Path] = None,
                 now_fn: Optional[Callable[[], datetime]] = None, sleep_fn: Optional[Callable[[float], None]] = None,
                 kill_fn: Optional[Callable[[str], tuple]] = None, page_fn: Optional[Callable[[str], None]] = None,
                 log_fn: Optional[Callable[..., None]] = None) -> None:
        self.b = broker if broker is not None else TNBroker()
        self.d = data if data is not None else TNData()
        self.state_path = Path(state_path) if state_path is not None else _DEFAULT_STATE
        self.now_fn = now_fn or (lambda: datetime.now(ET))
        self.sleep_fn = sleep_fn or time.sleep
        self.kill_fn = kill_fn or kill_halted
        self.page_fn = page_fn or _send_page
        self.log_fn = log_fn or _emit
        self.sym = str(_cfg("SWING_TREND_NIGHT_SYMBOL"))
        self.state: dict = {}
        self._now = datetime.now(ET)
        self._leases: dict = {}          # coid → allocator lease held by THIS process (None while the allocator is off)
        self._seen_attempts: dict = {}   # (night, step) → highest attempt seen at the broker (restart-safe ids)
        self._last_send = ""             # raw result of the last _send (ack / rejected / unknown / blocked)
        self.load()

    # ── state (atomic) ────────────────────────────────────────────────────────────────────────────
    def _guard(self) -> None:
        if self.state_path == _DEFAULT_STATE and _under_test():
            raise RuntimeError("refusing to use the production trend_night state from a test process")

    @staticmethod
    def _fresh() -> dict:
        return {"schema": APPROVAL_ID, "since": None, "nights": {}, "closed": [], "pages": {}, "held": 0,
                "result_checks": {}, "clock_source": None, "clock_fail_since": None, "bootstrapped": False}

    def load(self) -> None:
        self._guard()
        if not self.state_path.exists():
            self.state = self._fresh()
            return
        try:
            d = json.loads(self.state_path.read_text())
            if not isinstance(d, dict) or not isinstance(d.get("nights"), dict):
                raise ValueError("state is not a trend_night state dict")
            base = self._fresh()
            base.update(d)
            self.state = base
        except Exception as e:  # noqa: BLE001
            # Unreadable state must not look like "flat": keep the corrupt file, rebuild from the broker (full TN
            # history) and block entries for today. Exits keep running.
            bad = self.state_path.with_name(f"{self.state_path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(self.state_path, bad)
            except OSError as oe:
                logger.error("trend_night: could not set aside corrupt state: %s", oe)
            self.state = self._fresh()
            self.state["entry_blocked_day"] = self.now_fn().astimezone(ET).date().isoformat()
            self.page_fn(f"trend_night state unreadable ({e!r}) — rebuilt from broker TN orders; no entry today")

    def save(self) -> bool:
        self._guard()
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f"{self.state_path.name}.tmp{os.getpid()}")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("trend_night: state write FAILED: %s", e)
            self.page_fn(f"trend_night state write FAILED: {e!r}")
            return False

    # ── logging / paging ──────────────────────────────────────────────────────────────────────────
    def event(self, event: str, **kw: Any) -> None:
        self.log_fn(event, symbol=self.sym, tier=TIER, strategy=STRATEGY, approval_id=APPROVAL_ID,
                    ledger_lot=LEDGER_LOT, at_pt=fmt_pt(self._now), **kw)

    def page(self, key: str, msg: str) -> None:
        pages = self.state.setdefault("pages", {})
        last = _aware(pages.get(key))
        repeat = timedelta(seconds=float(_cfg("SWING_TREND_NIGHT_PAGE_REPEAT_SECONDS")))
        if last is not None and self._now - last < repeat:
            logger.critical("[page throttled] %s", msg)
            return
        pages[key] = self._now.isoformat()
        self.page_fn(f"{msg} ({fmt_pt(self._now)})")

    # ── paper assertion (spec item 8) ────────────────────────────────────────────────────────────
    def paper_ok(self) -> tuple[bool, str]:
        env_name = str(_cfg("SWING_TREND_NIGHT_PAPER_ACCOUNT_ENV"))
        want = os.getenv(env_name, "").strip()
        if not want:
            return False, f"paper account number not recorded (env {env_name} empty)"
        try:
            acct = self.b.account()
        except Exception as e:  # noqa: BLE001
            return False, f"account unreadable: {e!r}"
        have = str(getattr(acct, "account_number", "") or "").strip()
        if have != want:
            return False, "account number does not match the recorded paper account"
        return True, ""

    def _paper_gate(self, what: str) -> bool:
        ok, why = self.paper_ok()
        if not ok:
            self.event("skip", reason=f"paper assertion failed — {what} not submitted: {why}")
            self.page("paper", f"trend_night: paper assertion FAILED — every submit blocked ({why})")
        return ok

    # ── nights / order records ────────────────────────────────────────────────────────────────────
    def _nights(self) -> dict:
        return self.state.setdefault("nights", {})

    def _night(self, key: str) -> dict:
        n = self._nights().setdefault(key, {"status": "new", "orders": {}, "seq": {}})
        n.setdefault("orders", {})
        n.setdefault("seq", {})
        return n

    def _night_close(self, night: str) -> datetime:
        """The entry session's close for `night` — the epoch every order id of that night carries."""
        n = self._night(night)
        ms = n.get("close_ms")
        if ms is None:
            raise RuntimeError(f"trend_night {night}: no session close recorded for its order ids")
        return _ms_to_et(int(ms))

    def _coid(self, night: str, step: str, attempt: int) -> str:
        return build_coid(self._night_close(night), step, attempt)

    def _next_coid(self, night: str, step: str) -> str:
        """The next attempt for (night, step): above every attempt in state AND every attempt seen at the broker, so a
        lost state file can never re-issue an id the broker already holds."""
        n = self._night(night)
        seq = max(int(n["seq"].get(step, 0)), int(self._seen_attempts.get((night, step), 0))) + 1
        if not attempt_fits(seq):
            self.page(f"attempts_{night}_{step}", f"trend_night {night}: {step} order attempts exhausted ({seq - 1}) — "
                                                  f"no further {step} order can be sent; MANUAL action needed")
            raise AttemptsExhausted(f"{night} {step} attempt {seq}")
        if len(str(seq)) >= _ATTEMPT_DIGITS:          # the top digit band: well before half the budget is used
            self.page(f"attempts_high_{night}_{step}", f"trend_night {night}: {step} order attempt {seq} — the attempt "
                                                       f"budget is running down; check why orders keep failing")
        n["seq"][step] = seq
        return self._coid(night, step, seq)

    # ── capital admission (v11 item 2 / v12 item 2) ──────────────────────────────────────────────
    def _admit(self, coid: str, qty: int, price: float, q: float, tif: str) -> tuple[bool, str]:
        """execution.tier_capital_allocator.live_admit exactly as the Swing-breakout path calls it. With
        TIER_CAPITAL_ALLOCATOR_ENABLED False it approves without a lease. Any error refuses (fail closed)."""
        try:
            from execution.tier_capital_allocator import live_admit
            stop_px = stop_target(price, q)
            d = live_admit("swing", OWNER_TIER, self.sym, "buy", int(qty), float(price),
                           stop_price=float(stop_px) if stop_px is not None else 0.0, risk_price_bound=float(price),
                           overnight=True, order_type="market", time_in_force=tif, client_order_id=coid)
        except Exception as e:  # noqa: BLE001
            return False, f"allocator error (fail-closed): {e!r}"
        if not getattr(d, "approved", False):
            return False, str(getattr(d, "reason", "") or "refused")
        self._leases[coid] = getattr(d, "lease", None)
        return True, str(getattr(d, "reason", "") or "")

    def _lease_after_submit(self, coid: str, status: str, payload: Any) -> None:
        lease = self._leases.get(coid)
        if lease is None:
            return
        try:
            from execution.tier_capital_allocator import live_bind, live_release, live_release_proven_no_order
            if status == "ack":
                live_bind(lease, payload)
            elif status == "rejected":
                live_release_proven_no_order(lease, "broker_rejected_at_submit")
                self._leases.pop(coid, None)
            else:
                got = live_release(lease, "submit_none")     # exact client-id recovery; keeps the lease if unknown
                if got is not None:
                    live_bind(lease, got)
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: allocator lease update for %s failed: %s", coid, e)

    def _release_terminal_lease(self, coid: str, v: Optional[dict]) -> None:
        lease = self._leases.get(coid)
        if lease is None or v is None or is_live(v) or v["filled_qty"] > 0:
            return
        try:
            from execution.tier_capital_allocator import live_release
            live_release(lease, "broker_terminal_zero_fill")
            self._leases.pop(coid, None)
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: allocator release for %s failed: %s", coid, e)

    def _record_submit(self, night: str, coid: str, step: str, side: str, qty: int, **extra: Any) -> bool:
        n = self._night(night)
        n["orders"][coid] = {"state": "submitting", "step": step, "side": side, "qty": int(qty),
                             "submitted_ts": self._now.isoformat(), **extra}
        return self.save()

    def _record_result(self, night: str, coid: str, res: tuple[str, Any]) -> str:
        status, payload = res
        orec = self._night(night)["orders"][coid]
        if status == "ack":
            v = order_view(payload)
            orec.update(state="ack", order_id=v["id"])
            self.event("order_ack", coid=coid, order_id=v["id"], status=v["status"], qty=orec["qty"], step=orec["step"])
        elif status == "rejected":
            orec.update(state="rejected", error=str(payload)[:300])
            self.event("order_rejected", coid=coid, step=orec["step"], qty=orec["qty"], error=str(payload)[:300])
        else:
            orec.update(state="unknown", error=str(payload)[:300])
            self.event("order_unknown", coid=coid, step=orec["step"], qty=orec["qty"], error=str(payload)[:300])
        self.save()
        return status

    def _submit(self, night: str, step: str, side: str, qty: int, tif: str, *, stop_price: Optional[Decimal] = None,
                coid: Optional[str] = None, **extra: Any) -> str:
        """Persist the intent, assert paper, submit, record the outcome. Returns ack / rejected / unknown / blocked."""
        if not self._paper_gate(f"{step} {side} {qty}"):
            return "blocked"                      # before taking an attempt number: a failed check burns none
        if side == "sell":
            # cold review round 5: a fresh broker read immediately before EVERY sell; never sell more than it shows
            fresh = self._fresh_position()
            if fresh is None or fresh <= 0:
                self.event("sell_skipped", night=night, step=step, planned=int(qty), fresh=fresh)
                return "blocked"
            if fresh < int(qty):
                self.event("sell_capped", night=night, step=step, planned=int(qty), fresh=fresh)
                qty = fresh
        if coid is None:
            try:
                coid = self._next_coid(night, step)
            except AttemptsExhausted:
                return "blocked"
        if not self._record_submit(night, coid, step, side, qty, tif=tif,
                                   stop_price=str(stop_price) if stop_price is not None else None, **extra):
            return "blocked"
        self.event("order_submit", coid=coid, step=step, side=side, qty=int(qty), tif=tif,
                   stop_price=str(stop_price) if stop_price is not None else None, **extra)
        try:
            res = self.b.submit(self.sym, coid, side, int(qty), tif, stop_price=stop_price)
        except Exception as e:  # noqa: BLE001 — a raised submit is UNKNOWN (the order may exist)
            res = ("unknown", repr(e))
        if step in _ENTRY_STEPS:
            self._lease_after_submit(coid, res[0], res[1])
        return self._record_result(night, coid, res)

    def _cancel(self, v: dict, why: str) -> bool:
        """Send a cancel request unless one is already in flight (pending_cancel) or was sent less than one poll
        interval ago. Returns True when a request was sent (the caller re-passes at once), False to just poll."""
        if v["status"] == "pending_cancel":
            return False
        sent_at = self.state.setdefault("cancel_sent", {})
        last = _aware(sent_at.get(v["id"]))
        if last is not None and self._now - last < timedelta(seconds=float(_cfg("SWING_TREND_NIGHT_POLL_SECONDS"))):
            return False
        try:
            sent = self.b.cancel(v["id"])
        except Exception as e:  # noqa: BLE001
            sent = False
            logger.warning("trend_night: cancel of %s raised: %s", v["id"], e)
        sent_at[v["id"]] = self._now.isoformat()
        self.event("order_cancel_request", coid=v["coid"], order_id=v["id"], status=v["status"], sent=sent, why=why,
                   step=_step(v))
        return True

    # ── reconcile: broker truth + the trend_night order listing (POSITION-DRIVEN, cold review round 6) ──────────
    def _view(self, o: Any) -> Optional[dict]:
        """A broker order as a view when — and only when — its client id parses as trend_night's (v10 item 1)."""
        v = order_view(o)
        p = parse_coid(v["coid"], self._now)
        if p is None or v["symbol"] not in ("", self.sym):
            return None
        side = "buy" if p["side"] == "b" else "sell"
        if v["side"] and v["side"] != side:
            return None
        v["parsed"] = p
        return v

    def reconcile(self) -> Recon:
        """One broker read: every trend_night order (listing + client-id resolution of what was submitted but not
        listed) and a FRESH QQQM position. Order decisions use only Q = max(0, min(F, R)) (_quantities) and the
        open orders; trend_night's own fills drive reporting only (booking, P&L, edge, fill cost, loss review)."""
        since = _aware(self.state.get("since"))
        try:
            raw = self.b.list_orders(self.sym, since)
        except Exception as e:  # noqa: BLE001
            return Recon(ok=False, reason=f"trend_night order listing unreadable: {e!r}")
        views: dict = {}
        for o in raw:
            v = self._view(o)
            if v is not None:
                views[v["coid"]] = v
        unknown_sell_qty, unknown_buys = 0, 0
        poll = timedelta(seconds=float(_cfg("SWING_TREND_NIGHT_POLL_SECONDS")))
        for nkey, n in self._nights().items():
            booked = n.get("status") in ("closed", "unfilled")
            for coid, orec in n.get("orders", {}).items():
                st = orec.get("state")
                if coid in views:
                    if st != "ack":
                        orec.update(state="ack", order_id=views[coid]["id"])
                        self.event("order_resolved", coid=coid, resolution="found_in_listing", status=views[coid]["status"])
                    continue
                if st in ("absent", "rejected") or (booked and st == "ack"):
                    continue                     # booked: every order of it was terminal when `since` advanced
                try:
                    found, o = self.b.get_by_coid(coid)
                except Exception as e:  # noqa: BLE001
                    found, o = "unknown", None
                    logger.warning("trend_night: client-id lookup %s raised: %s", coid, e)
                if found == "found" and o is not None:
                    v = self._view(o) or dict(order_view(o), parsed=parse_coid(coid, self._now))
                    views[coid] = v
                    orec.update(state="ack", order_id=v["id"])
                    self.event("order_resolved", coid=coid, resolution="client_id_lookup", status=v["status"])
                    continue
                submitted = _aware(orec.get("submitted_ts")) or self._now
                if found == "absent" and self._now - submitted >= poll:
                    orec["state"] = "absent"
                    self.event("order_resolved", coid=coid, resolution="absent_at_broker", night=nkey)
                    continue
                if orec.get("side") == "sell":
                    unknown_sell_qty += int(orec.get("qty") or 0)
                else:
                    unknown_buys += 1
        try:
            bq = int(self.b.position_qty(self.sym))
        except Exception as e:  # noqa: BLE001
            return Recon(ok=False, reason=f"broker position unreadable: {e!r}", views=views)
        for v in views.values():
            p = v.get("parsed")
            if p:
                k = (p["night"], p["step"])
                self._seen_attempts[k] = max(int(self._seen_attempts.get(k, 0)), int(p["attempt"]))
                self._night(p["night"]).setdefault("close_ms", p["close_ms"])
        live = [v for v in views.values() if is_live(v)]
        r = Recon(ok=True, views=views, position=bq, live_sells=[v for v in live if v["side"] == "sell"],
                  live_buys=[v for v in live if v["side"] == "buy"], unknown_sell_qty=unknown_sell_qty,
                  unknown_buys=unknown_buys)
        self._quantities(r)
        return r

    # ── F from FILL ACTIVITIES only (cold review round 9) ────────────────────────────────────────────────────
    def _activities(self) -> Optional[list]:
        """QQQM fill activities after the last checkpoint (state loss: from the strategy's first possible night),
        fully paged, deduplicated by activity id. None when the fetch failed (the caller keeps the last good F)."""
        ck = _aware((self.state.get("act_ckpt") or {}).get("after"))
        after = ck or datetime(_LIVE_SINCE.year, _LIVE_SINCE.month, _LIVE_SINCE.day, tzinfo=ET)
        try:
            rows = self.b.fills(after)
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: fill activities unreadable: %s", e)
            return None
        out, ids = [], set()
        for a in rows or []:
            if not isinstance(a, dict) or str(a.get("symbol") or self.sym).upper() != self.sym:
                continue
            aid = str(a.get("id") or "")
            if not aid or aid in ids:
                continue                                    # a repeated row (page overlap) counts once
            ids.add(aid)
            out.append(a)
        return sorted(out, key=lambda a: (str(a.get("transaction_time") or ""), str(a.get("id"))))

    def _classify(self, acts: list, views: dict) -> tuple[list, list, list]:
        """Each activity by its ORDER, checked at the source: a client_order_id that parse_coid accepts is
        trend_night's own; any other order is foreign; a failed lookup leaves the activity unclassified this pass.
        Order ids already known (the listing, or our own acknowledged submits) need no lookup; results are cached."""
        known = {v["id"]: v["coid"] for v in views.values()}
        for n in self._nights().values():
            for coid, o in (n.get("orders") or {}).items():
                if o.get("order_id"):
                    known[str(o["order_id"])] = coid
        cache = self.state.setdefault("order_class", {})
        own, foreign, unclassified = [], [], []
        for a in acts:
            oid = str(a.get("order_id") or "")
            coid = known.get(oid)
            if coid is None and oid in cache:
                coid = cache[oid]
            if coid is None:
                try:
                    found, o = self.b.get_order(oid)
                except Exception as e:  # noqa: BLE001
                    found, o = "unknown", None
                    logger.warning("trend_night: order lookup %s raised: %s", oid, e)
                if found == "found" and o is not None:
                    coid = str(order_view(o)["coid"])
                elif found == "absent":
                    coid = ""
                else:
                    unclassified.append(a)
                    continue
                cache[oid] = coid
            p = parse_coid(coid, self._now) if coid else None
            if p is not None:
                own.append(dict(a, night=p["night"], step=p["step"]))
            else:
                foreign.append(a)
        return own, foreign, unclassified

    @staticmethod
    def _listing_lead(own: list, views: dict) -> list:
        """Own fills the order LISTING reports before their activity (listing orders are trend_night's by definition):
        per order id, own filled qty = max(listing filled_qty, Σ that order's own activities), so the listing's lead
        becomes one synthetic own fill (price: the activities' average when present, else the listing's average). Per
        order id max — never a double count. Departures still come only from classified foreign activities."""
        by_order: dict = {}
        for a in own:
            by_order.setdefault(str(a.get("order_id") or ""), []).append(a)
        out = []
        for v in views.values():
            p = v.get("parsed")
            mine = by_order.get(v["id"]) or []
            got = sum(int(_num(a.get("qty"))) for a in mine)
            lead = int(v["filled_qty"]) - got
            if p is None or lead <= 0:
                continue
            if mine:
                price = math.fsum(_num(a.get("qty")) * _num(a.get("price")) for a in mine) / max(got, 1)
            else:
                price = _num(v["filled_avg_price"])
            when = v["filled_at"] or v["submitted_at"]
            out.append({"id": f"listing:{v['id']}", "order_id": v["id"], "symbol": v["symbol"], "side": v["side"],
                        "qty": str(lead), "price": str(price), "night": p["night"], "step": p["step"],
                        "transaction_time": when.astimezone(UTC).isoformat() if when else "", "source": "listing"})
        return out

    def _derive(self, own: list, foreign: list) -> dict:
        """Rule 2, walked in fill-time order and re-derived every pass (never persisted):
        - an own buy adds a lot to its night (night from the parsed client_order_id);
        - an own sell takes from the open lots FIFO (oldest night first) — no night can go negative; an excess sell
          is paged as a data inconsistency and not used;
        - a foreign buy adds to the foreign pool; a foreign sell first takes from that pool (a manual round trip
          never touches own shares), and only the part beyond it is a DEPARTURE of own shares held at that moment,
          taken newest night first.
        G = foreign buys − foreign sells; `pool` = foreign shares still held."""
        events = sorted([(str(a.get("transaction_time") or ""), str(a.get("id")), "own", a) for a in own]
                        + [(str(a.get("transaction_time") or ""), str(a.get("id")), "foreign", a) for a in foreign])
        nights: dict = {}
        pool, g, excess = 0, 0, 0
        for _t, _i, src, a in events:
            qty = int(_num(a.get("qty")))
            buy = str(a.get("side") or "").lower().startswith("buy")
            if src == "own" and buy:
                n = nights.setdefault(a["night"], {"lots": [], "sells": [], "departures": [], "remaining": 0,
                                                   "departed": 0, "bought": 0, "bought_cost": 0.0, "buy_orders": []})
                # an unknown price (a listing lead with no average yet) stays None → the stop uses the frozen last trade
                n["lots"].append([qty, _num(a.get("price")) or None, str(a.get("transaction_time"))])
                n["bought"] += qty
                n["bought_cost"] += qty * _num(a.get("price"))
                n["buy_orders"].append(str(a.get("order_id")))
                continue
            if src == "foreign" and buy:
                pool += qty
                g += qty
                continue
            left = qty
            if src == "foreign":
                g -= qty
                take = min(pool, left)
                pool -= take
                left -= take
                order = sorted(nights, reverse=True)            # a departure: newest night first
            else:
                order = sorted(nights)                          # an own sell: oldest night first (FIFO)
            for k in order:
                for lot in (nights[k]["lots"] if src == "own" else reversed(nights[k]["lots"])):
                    if left <= 0:
                        break
                    take = min(left, lot[0])
                    if take <= 0:
                        continue
                    lot[0] -= take
                    left -= take
                    rec = {"qty": take, "price": _num(a.get("price")), "order_id": a.get("order_id"),
                           "activity_id": a.get("id"), "entry_price": lot[1]}
                    if src == "own":
                        nights[k]["sells"].append(dict(rec, step=a.get("step")))
                    else:
                        nights[k]["departures"].append(rec)
                        nights[k]["departed"] += take
            if src == "own":
                excess += left
        for n in nights.values():
            n["remaining"] = sum(lot[0] for lot in n["lots"])
        if excess:
            self.event("own_sells_exceed_buys", excess=excess)
            self.page("own_negative", f"trend_night: own sell fills exceed own buy fills by {excess} sh — a data "
                                      f"inconsistency; nights are clamped at 0")
        return {"nights": nights, "own": sum(n["remaining"] for n in nights.values()), "g": g, "pool": pool,
                "depart": sum(n["departed"] for n in nights.values())}

    def _quantities(self, r: Recon) -> None:
        """F from activities; Q = max(0, min(F, R)). If the activity fetch fails, F keeps the last good derivation
        (none → Q = 0 and a page); unclassified activities hold entries and booking. R > F pages as foreign shares
        (re-evaluated every pass, never traded)."""
        acts = self._activities()
        if acts is None:
            # round 10: DEGRADED — entries / booking held, no new or larger sell, Q capped by the listing too
            last = self.state.get("act_last")
            r.acts_ok = False
            r.derived = (last.get("derived") if isinstance(last, dict) else None) or {"nights": {}, "own": 0, "g": 0,
                                                                                       "depart": 0}

        else:
            own, foreign, unclassified = self._classify(acts, r.views)
            own = own + self._listing_lead(own, r.views)
            r.derived = self._derive(own, foreign)
            r.unclassified = len(unclassified)
            if unclassified:
                self.event("activities_unclassified", count=len(unclassified),
                           order_ids=[a.get("order_id") for a in unclassified])
                self.state["unclassified_passes"] = int(self.state.get("unclassified_passes") or 0) + 1
                if self.state["unclassified_passes"] > _UNRESOLVED_POLL_ATTEMPTS:
                    self.page("unclassified", f"trend_night: {len(unclassified)} {self.sym} fill(s) cannot be "
                                              f"classified (order lookup failing for "
                                              f"{sorted({str(a.get('order_id')) for a in unclassified})}) for "
                                              f"{self.state['unclassified_passes']} passes — entries and booking "
                                              f"wait until the lookup works")
            else:
                self.state["unclassified_passes"] = 0
            # round 12: the per-own-order filled qty this derivation used — the stale F is trusted later only if the
            # listing still shows exactly these quantities (decided by DATA, not by read time)
            used = {v["id"]: int(v["filled_qty"]) for v in r.views.values() if v.get("parsed")}
            for a in own:
                oid = str(a.get("order_id") or "")
                used[oid] = max(int(used.get(oid, 0)),
                                sum(int(_num(b.get("qty"))) for b in own if str(b.get("order_id") or "") == oid))
            # round 12: each night's departed qty (own shares that left via foreign sells) — the degraded cap nets it out
            departed = {str(k): int(n.get("departed") or 0) for k, n in (r.derived.get("nights") or {}).items()}
            self.state["act_last"] = {"derived": r.derived, "at": self._now.isoformat(), "seen": used,
                                      "departed": departed}
            r.last_act_time = str(acts[-1].get("transaction_time")) if acts else None
        f = int(r.derived.get("own") or 0)
        raw = int(r.position)
        # the listing's own net on unbooked nights — booked nights (state, plus the booked-nights ledger that survives a
        # state loss) excluded: a cap while degraded, and a page guard below (never a quantity source otherwise)
        booked, ledger_ok = self._booked_ledger()
        nets: dict = {}
        for v in r.views.values():
            k = _night_of(v)
            if k in booked or self._nights().get(k, {}).get("status") in ("closed", "unfilled"):
                continue
            nets[k] = nets.get(k, 0) + (v["filled_qty"] if v["side"] == "buy" else -v["filled_qty"])
        listed_net = sum(nets.values())
        # round 12: the listing cannot see own shares that left via foreign sells — net each night's last known
        # departures out (from this pass's derivation, else the last good one) so the cap and the foreign-shares
        # guard never count shares the owner already sold as ours
        last_dep = (self.state.get("act_last") or {}).get("departed")
        dep_by_night = last_dep if isinstance(last_dep, dict) else {}
        adj_net = sum(max(0, n - int(dep_by_night.get(str(k), 0) or 0)) for k, n in nets.items())
        r.short = max(0, -raw)
        if r.acts_ok:
            r.own = f
            r.held = max(0, min(f, raw))
        else:
            # DEGRADED (rounds 11–12): Q = max(0, min(R, the listing's own net)); an unknown F is never read as 0. The
            # stale F joins the min only when the listing still shows exactly the own fills it was derived from (and no
            # new own order has fills) — decided by data, not by read time.
            last = self.state.get("act_last") or {}
            seen_map = last.get("seen") if isinstance(last.get("seen"), dict) else None
            own_now = {v["id"]: int(v["filled_qty"]) for v in r.views.values() if v.get("parsed") and v["filled_qty"] > 0}
            use_stale = seen_map is not None and all(int(seen_map.get(i, -1)) == q for i, q in own_now.items())
            cap = adj_net
            if not ledger_ok:
                newest = max(nets, default=None)            # ledger unreadable: only the newest unbooked night
                cap = max(0, nets.get(newest, 0) - int(dep_by_night.get(str(newest), 0) or 0)) \
                    if newest is not None else 0
                self.page("booked_ledger", "trend_night: the booked-nights ledger is unreadable — degraded protection "
                                           "limited to the newest night's own shares")
            r.own = max(0, cap)
            r.held = max(0, min(raw, cap, f if use_stale else cap))
            gap = max(0, min(raw, cap)) - r.held            # own shares (by the cap) left unprotected
            self.event("activities_degraded", broker_qty=raw, listed_own_net=listed_net, adjusted_own_net=adj_net, cap=cap, stale_own=f,
                       stale_used=use_stale, protected=r.held)
            self.page("activities", f"trend_night: fill activities UNREADABLE — protecting / selling {r.held} sh from "
                                    f"the order listing; entries and booking held"
                                    + (f"; {gap} shares unprotected (the broker holds {raw})" if gap else ""))
        poll = timedelta(seconds=float(_cfg("SWING_TREND_NIGHT_POLL_SECONDS")))
        working = any(v["type"] != "stop" for v in r.live_sells) or bool(r.live_buys) or r.unknown_sell_qty > 0 \
            or r.unknown_buys > 0
        if raw > max(r.own, adj_net) and r.own >= 0 and not working and not r.unclassified:
            seen = self.state.get("above_seen")
            if not isinstance(seen, dict) or seen.get("key") != [r.own, raw]:
                self.state["above_seen"] = {"key": [r.own, raw], "first": self._now.isoformat()}
            elif self._now - (_aware(seen.get("first")) or self._now) >= poll:
                r.foreign = raw - r.own
                self.event("foreign_shares", broker_qty=raw, own_qty=r.own, foreign=r.foreign)
                self.page("foreign_shares", f"trend_night: the broker holds {raw} {self.sym} but trend_night's own "
                                            f"fills account for {r.own} — {r.foreign} sh are not trend_night's (a "
                                            f"manual buy?). They are NOT traded; entries are blocked until resolved.")
        else:
            self.state.pop("above_seen", None)
        if 0 <= raw < r.own:
            below = self.state.get("below_since")
            if not isinstance(below, dict) or below.get("key") != [r.own, raw]:
                self.state["below_since"] = {"key": [r.own, raw], "first": self._now.isoformat()}
            else:
                swap_mins = float(_cfg("SWING_TREND_NIGHT_SWAP_MIN"))
                if self._now - (_aware(below.get("first")) or self._now) >= timedelta(minutes=swap_mins):
                    self.page("broker_below_own", f"trend_night: the broker has read {raw} {self.sym} since "
                                                  f"{below.get('first')} but trend_night's own fills account for "
                                                  f"{r.own} — protecting/selling only {r.held}. Check the account.")
        else:
            self.state.pop("below_since", None)

    # ── booked-nights ledger (round 12): append-only, next to the state file, survives a state-file loss ────────
    def _ledger_path(self) -> Path:
        return self.state_path.with_name(f"{self.state_path.name}.booked.jsonl")

    def _booked_ledger(self) -> tuple[set, bool]:
        """({night keys booked}, readable). A missing file is an empty ledger; an unreadable FILE is (set(), False).
        Round 12: parsed line by line — an undecodable line (a torn append) is skipped, logged and paged once, and every
        other line still counts, so one torn write never makes the whole ledger unreadable."""
        p = self._ledger_path()
        if not p.exists():
            return set(), True
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: booked-nights ledger unreadable: %s", e)
            return set(), False
        out = set()
        bad = []
        for n, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.add(str(json.loads(line)["night"]))
            except Exception as e:  # noqa: BLE001
                logger.warning("trend_night: booked-nights ledger line %d skipped (%s): %r", n, e, line)
                bad.append(n)
        if bad and self.state.get("ledger_torn") != bad:
            self.state["ledger_torn"] = bad
            self.page("ledger_torn", f"trend_night: booked-nights ledger line(s) {bad} undecodable — skipped; the "
                                     f"other lines still count")
        return out, True

    def _append_booked(self, night: str, own_net: int, departed: int) -> None:
        """Atomic append (one line, flushed + fsynced) — never rewritten."""
        self._guard()
        try:
            p = self._ledger_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            # round 12: a torn last line (no trailing newline) must not swallow this record into it
            lead = ""
            if p.exists() and p.stat().st_size > 0:
                with open(p, "rb") as rh:
                    rh.seek(-1, os.SEEK_END)
                    lead = "" if rh.read(1) == b"\n" else "\n"
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(lead + json.dumps({"night": night, "own_net": int(own_net), "departed": int(departed),
                                     "at": self._now.isoformat()}) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as e:  # noqa: BLE001
            logger.error("trend_night: booked-nights ledger append failed: %s", e)
            self.page("booked_ledger_write", f"trend_night: could not append night {night} to the booked-nights ledger "
                                             f"({e!r})")

    def _checkpoint(self, r: Recon) -> None:
        """Rule 4: advance the activity window only at a fully flat, settled point."""
        d = r.derived or {}
        if (r.acts_ok and r.position == 0 and r.own == 0 and int(d.get("pool") or 0) == 0 and not r.unclassified
                and not r.live_sells and not r.live_buys and r.unknown_sell_qty == 0 and r.unknown_buys == 0
                and all(n.get("status") in ("closed", "unfilled", "skipped", "new")
                        for k, n in self._nights().items() if k in (d.get("nights") or {}))
                and r.last_act_time):
            # past every fill either source has shown — a listing-led fill's activity arriving later (same fill time)
            # then falls before the window instead of reading as an own sell with no buy
            listed = [v["filled_at"] for v in r.views.values() if v["filled_at"] is not None]
            at = max([t for t in [_aware(r.last_act_time)] + listed if t is not None], default=None)
            if at is not None:
                self.state["act_ckpt"] = {"after": (at + timedelta(microseconds=1)).isoformat(),
                                          "at": self._now.isoformat()}
                stamps = [v["submitted_at"] for v in r.views.values() if v["submitted_at"] is not None]
                if stamps:                                   # the order listing (state only) starts there too
                    self.state["since"] = (max(stamps) + timedelta(microseconds=1)).astimezone(UTC).isoformat()

    # ── session times (spec item 6.6 / 8) ─────────────────────────────────────────────────────────
    def session(self) -> Session:
        today = self._now.date()
        cache = self.state.get("calendar") or {}
        if cache.get("date") != today.isoformat():
            try:
                rows = self.b.calendar(today, today)
                row = next(((op_, cl_) for d_, op_, cl_ in rows if d_ == today), None)
                cache = {"date": today.isoformat(), "is_session": row is not None,
                         "open": row[0].isoformat() if row else None, "close": row[-1].isoformat() if row else None}
                self.state["calendar"] = cache
            except Exception as e:  # noqa: BLE001
                logger.warning("trend_night: calendar unavailable: %s", e)
                cache = {}
        if cache.get("date") == today.isoformat():
            self._clock_source("calendar")
            op, cl = _aware(cache.get("open")), _aware(cache.get("close"))
            return Session(today, bool(cache.get("is_session")), op.astimezone(ET) if op else None,
                           cl.astimezone(ET) if cl else None, "calendar")
        try:
            clk = self.b.clock()
            self.state["clock_fail_since"] = None
            self._clock_source("broker_clock")
            if clk.get("is_open"):
                nc = _aware(clk.get("next_close"))
                return Session(today, True, self._now, nc.astimezone(ET) if nc else None, "broker_clock")
            no = _aware(clk.get("next_open"))
            if no is not None and no.astimezone(ET).date() == today:
                return Session(today, True, no.astimezone(ET), None, "broker_clock")
            return Session(today, False, None, None, "broker_clock")
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: broker clock unavailable: %s", e)
        since = _aware(self.state.get("clock_fail_since"))
        if since is None:
            self.state["clock_fail_since"] = self._now.isoformat()
            since = self._now
        if self._now - since > timedelta(seconds=float(_cfg("SWING_TREND_NIGHT_CLOCK_FALLBACK_SECONDS"))):
            self._clock_source("system_clock")
            if self._now.weekday() < 5:
                op = datetime(today.year, today.month, today.day, tzinfo=ET) + timedelta(
                    minutes=float(_cfg("SWING_TREND_NIGHT_FALLBACK_OPEN_MIN_ET")))
                return Session(today, True, op, None, "system_clock")
        return Session(today, False, None, None, "none")

    def _clock_source(self, src: str) -> None:
        if self.state.get("clock_source") != src:
            self.event("clock_source", previous=self.state.get("clock_source"), current=src)
            self.state["clock_source"] = src

    # ── fills + night status ──────────────────────────────────────────────────────────────────────
    def _log_fills(self, r: Recon) -> None:
        seen = self.state.setdefault("seen_fills", {})
        for v in r.views.values():
            prev = int(seen.get(v["id"], 0))
            if v["filled_qty"] > prev:
                night = self._nights().get(_night_of(v)) or {}
                self.event("fill", coid=v["coid"], order_id=v["id"], step=_step(v), side=v["side"],
                           qty=v["filled_qty"] - prev, cum_qty=v["filled_qty"], price=v["filled_avg_price"] or 0.0,
                           fill_time=fmt_pt(v["filled_at"]) if v["filled_at"] else None, path=night.get("path"))
                seen[v["id"]] = v["filled_qty"]

    def _set_status(self, key: str, new: str, **kw: Any) -> None:
        n = self._night(key)
        if n.get("status") != new:
            self.event("state_transition", night=key, previous=n.get("status"), current=new, **kw)
            n["status"] = new

    def _q_for(self, key: str) -> Optional[float]:
        n = self._nights().get(key) or {}
        q = n.get("q")
        if q is None:
            m = self._measure_q()
            q = m.get("q") if m else None
            if q is not None:
                self._night(key)["q"] = q
        return float(q) if q is not None else None

    # ── position-driven orders (cold review round 6: rules 2–7) ──────────────────────────────────────────────
    def _order_night(self, r: Recon) -> str:
        """The night key every protective / exit order is tagged with: the newest night with an entry fill in
        the listing, else a night record for today (its epoch = now, frozen). Tagging is for attribution and id
        uniqueness only — no order quantity or price reads a night's fills except the stop basis below."""
        filled = sorted({_night_of(v) for v in r.views.values() if _is_entry(v) and v["filled_qty"] > 0})
        if filled:
            return filled[-1]
        key = night_key(self._now.date())
        n = self._night(key)
        if n.get("close_ms") is None:
            n["close_ms"] = epoch_ms(self._now)
        return key

    def _fresh_position(self) -> Optional[int]:
        """A fresh broker read of the symbol's position, taken immediately before a submit; None when unreadable."""
        try:
            return int(self.b.position_qty(self.sym))
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: fresh position read failed: %s", e)
            return None

    def _backoff_ok(self, kind: str) -> bool:
        b = (self.state.get("backoff") or {}).get(kind) or {}
        at = _aware(b.get("retry_after"))
        return at is None or self._now >= at

    def _note_result(self, kind: str, res: str, why: str) -> None:
        """Rule 6: a rejection pages (throttled) and backs off one poll interval per consecutive rejection."""
        book = self.state.setdefault("backoff", {})
        poll_secs = float(_cfg("SWING_TREND_NIGHT_POLL_SECONDS"))
        if res == "ack":
            book.pop(kind, None)
            return
        if res == "unknown":                         # an UNKNOWN submit waits one poll interval before anything else
            b = book.setdefault(kind, {"n": 0})
            b["retry_after"] = (self._now + timedelta(seconds=poll_secs)).isoformat()
            return
        if res != "rejected":
            return
        b = book.setdefault(kind, {"n": 0})
        b["n"] = int(b.get("n") or 0) + 1
        # the wait grows one poll interval per consecutive refusal, capped at the swap window's length, so a long
        # refusal run can never leave an unbounded gap (rule 6)
        swap_mins = float(_cfg("SWING_TREND_NIGHT_SWAP_MIN"))
        cap_secs = swap_mins * 60
        wait_secs = min(poll_secs * b["n"], cap_secs)
        b["retry_after"] = (self._now + timedelta(seconds=wait_secs)).isoformat()
        self.event("order_rejected_backoff", kind=kind, rejects=b["n"], retry_after=b["retry_after"], why=why)
        self.page(f"rejected_{kind}", f"trend_night: {kind} order REJECTED ({why}; {b['n']} in a row) — retrying with "
                                      f"back-off; shares may be unprotected meanwhile")

    def _send(self, r: Recon, step: str, qty: int, tif: str, stop_price: Optional[Decimal] = None) -> str:
        if qty < 1:
            return "ok"
        kind = step                                     # each order kind backs off on its own consecutive refusals
        if not self._backoff_ok(kind):
            return "wait"
        night = self._order_night(r)
        side = "buy" if _step_side(step) == "b" else "sell"
        res = self._submit(night, step, side, qty, tif, stop_price=stop_price)
        self._last_send = res
        if step == "stop":
            self.event("stop_place", night=night, qty=qty, stop_price=str(stop_price), result=res)
        self._note_result(kind, res, f"{step} {side} {qty}")
        return "acted" if res in ("ack", "unknown") else "wait"

    def _note_unknown(self, r: Recon) -> bool:
        """True while any trend_night order's outcome is UNKNOWN (no new order of that side meanwhile). Pages
        (throttled, repeating) once more than _UNRESOLVED_POLL_ATTEMPTS passes went by."""
        if r.unknown_sell_qty <= 0 and r.unknown_buys <= 0:
            self.state["unknown_passes"] = 0
            return False
        self.state["unknown_passes"] = int(self.state.get("unknown_passes") or 0) + 1
        if self.state["unknown_passes"] > _UNRESOLVED_POLL_ATTEMPTS:
            self.page("order_unknown", f"trend_night: an order has been UNKNOWN for {self.state['unknown_passes']} "
                                       f"passes ({r.unknown_sell_qty} sh of sells, {r.unknown_buys} buy order(s)) while "
                                       f"the broker holds {r.position} — resolving by client id")
        return True

    def _stop_price(self, r: Recon) -> Optional[Decimal]:
        """Rule 2: the held shares that trend_night's own fills explain are priced at their FIFO entry cost × (1 − q);
        shares no fill explains at the last trade × (1 − q), frozen when first seen. One stop, so the two are
        share-weighted and rounded DOWN to the tick."""
        held = r.held
        if r.acts_ok:
            lots = [(int(lot[0]), lot[1]) for k in sorted((r.derived or {}).get("nights") or {})
                    for lot in r.derived["nights"][k]["lots"] if lot[0] > 0]
        else:
            lots = fifo_lots(r.views)                       # degraded: the listing's own fills price the stop
        while sum(q for q, _ in lots) > held and lots:          # fewer shares than the fills: the oldest went first
            extra = sum(q for q, _ in lots) - held
            if lots[0][0] <= extra:
                lots.pop(0)
            else:
                lots[0] = (lots[0][0] - extra, lots[0][1])
        known = [(q, px) for q, px in lots if px is not None]
        explained = sum(q for q, _ in known)
        unexplained = held - explained
        q_night = self._q_for(self._order_night(r))
        parts: list = []
        if explained:
            avg = math.fsum(q * float(px) for q, px in known) / explained
            t = stop_target(avg, q_night)
            if t is None:
                return None
            parts.append((explained, t))
        frozen = self.state.get("orphan_stop")
        if unexplained > 0:
            if not isinstance(frozen, dict) or not frozen.get("px"):
                basis = self.d.last_price(self.sym)
                t = stop_target(basis, q_night)
                if t is None:
                    return None
                frozen = {"px": str(t), "basis": basis, "at": self._now.isoformat(), "qty": unexplained}
                self.state["orphan_stop"] = frozen
                self.page("orphan_shares", f"trend_night: {unexplained} {self.sym} held that no trend_night fill explains "
                                           f"— protected at the last trade ${basis} × (1 − q) = {t} and sold at the next "
                                           f"exit window. Check for a manual buy.")
            parts.append((unexplained, Decimal(str(frozen["px"]))))
        else:
            self.state.pop("orphan_stop", None)
        if not parts:
            return None
        total = sum(q for q, _ in parts)
        price = sum((Decimal(q) * px for q, px in parts), Decimal(0)) / Decimal(total)
        return price.quantize(_tick(), rounding=ROUND_DOWN)

    def _protect(self, r: Recon) -> str:
        """Rule 2: exactly ONE live trend_night GTC sell stop for Q at the stop price. A size / price change is made by
        replace_order_by_id (design item 5 — the shares stay protected; no cancel-then-place gap); only a REJECTED
        replace falls back to cancel-then-place, paging."""
        held = r.held
        if r.unknown_sell_qty > 0:
            return "wait"
        if held == 0:
            sent = [self._cancel(v, "no confirmed shares") for v in r.live_sells]
            return "acted" if any(sent) else ("wait" if r.live_sells else "ok")
        others = [v for v in r.live_sells if v["type"] != "stop"]
        if others:
            sent = [self._cancel(v, "outside the exit window only a stop rests") for v in others]
            return "acted" if any(sent) else "wait"
        stops = sorted((v for v in r.live_sells if v["type"] == "stop"),
                       key=lambda v: v["submitted_at"] or datetime.min.replace(tzinfo=UTC))
        if len(stops) > 1:
            sent = [self._cancel(v, "more than one stop") for v in stops[:-1]]
            return "acted" if any(sent) else "wait"
        target = self._stop_price(r)
        if target is None:
            self.page("stop_price", f"trend_night: stop price cannot be computed — {held} sh may be unprotected")
            return "fail"
        if stops:
            s = stops[0]
            if remaining(s) == held and _price_eq(s["stop_price"], target):
                return "ok"
            if s["status"] in _TRANSITIONAL:
                return "wait"
            return self._replace_stop(r, s, held, target)
        return self._send(r, "stop", held, "gtc", stop_price=target)

    def _replace_stop(self, r: Recon, s: dict, held: int, target: Decimal) -> str:
        """Resize / reprice the resting stop in ONE broker call. The new size is capped by a fresh read (never above
        what the broker holds); a refused replace pages and falls back to cancel-then-place on the next pass."""
        fresh = self._fresh_position()
        if fresh is None or fresh <= 0:
            return "wait"
        want = min(int(held), fresh)
        if remaining(s) == want and _price_eq(s["stop_price"], target):
            return "wait"
        if not self._backoff_ok("stop"):
            return "wait"
        if s["id"] in (self.state.get("replace_failed") or {}):
            return "acted" if self._cancel(s, "replace refused — cancel, then a new stop") else "wait"
        if not self._paper_gate("stop replace"):
            return "fail"
        key = self._order_night(r)
        try:
            coid = self._next_coid(key, "stop")
        except AttemptsExhausted:
            return "fail"
        if not self._record_submit(key, coid, "stop", "sell", want, replaces=s["id"], stop_price=str(target)):
            return "fail"
        try:
            rep = self.b.replace(self.sym, s["id"], want, target, coid)
        except Exception as e:  # noqa: BLE001
            rep = ("unknown", repr(e))
        self.event("stop_replace", night=key, old_order_id=s["id"], old_qty=remaining(s), old_stop=s["stop_price"],
                   qty=want, stop_price=str(target), result=rep[0])
        out = self._record_result(key, coid, rep)
        self._note_result("stop", out, f"stop replace {remaining(s)} → {want} @ {target}")
        if out == "rejected":
            self.state.setdefault("replace_failed", {})[s["id"]] = self._now.isoformat()
            self.page("replace_refused", f"trend_night: the stop resize ({remaining(s)} → {want} @ {target}) was "
                                         f"REFUSED — cancelling and placing a new stop (a brief gap)")
        return "acted"

    def _exit_window(self, sess: Session) -> bool:
        """Rule 3: from open − SWAP until the entry window opens (close − ENTRY_START) the target position is FLAT;
        any confirmed shares are sold. From the entry window to the next swap they are held under the stop."""
        if not sess.is_session or sess.open is None:
            return False
        if self._now < sess.open - timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_SWAP_MIN"))):
            return False
        if sess.close is not None and self._now >= sess.close - timedelta(
                minutes=float(_cfg("SWING_TREND_NIGHT_ENTRY_START_MIN"))):
            return False
        return True

    def _exit(self, r: Recon, sess: Session) -> str:
        """Rule 3: before the open, swap the stop for one opg sell of the confirmed quantity (none after the opg
        cutoff, or after an opg rejection — then the stop stays); at/after the open, cancel anything resting and sell
        the confirmed quantity at market, a stale market sell cancelled after MKT_SELL_SECONDS."""
        held = r.held
        if held == 0 or r.unknown_sell_qty > 0:
            return self._protect(r)
        assert sess.open is not None
        op = sess.open
        ex = self.state.setdefault("exit", {})
        if ex.get("day") != sess.day.isoformat():
            ex.clear()
            ex["day"] = sess.day.isoformat()
        if self._now < op:
            cutoff = op - timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_OPG_CUTOFF_MIN")))
            opgs = [v for v in r.live_sells if v["tif"] == "opg"]
            if ex.get("opg_rejected") or (self._now >= cutoff and not opgs):
                return self._protect(r)
            if opgs:
                if remaining(opgs[0]) <= held and len(r.live_sells) == 1:
                    return "wait"                               # the opg acts at the open; any rest sells at market
                sent = [self._cancel(v, "opg qty differs / extra sells") for v in r.live_sells]
                return "acted" if any(sent) else "wait"
            if r.live_sells:
                sent = [self._cancel(v, "swap: stop → opg") for v in r.live_sells]
                return "acted" if any(sent) else "wait"
            self._last_send = ""
            res = self._send(r, "opg", held, "opg")
            if self._last_send == "rejected":
                ex["opg_rejected"] = True
                self.event("exit_step", step="opg_rejected", note="the stop is re-placed; market sell at the open")
                return self._protect(r)
            return res
        mkts = [v for v in r.live_sells if v["type"] != "stop" and v["tif"] != "opg"]
        if mkts:
            age = self._now - (mkts[0]["submitted_at"] or self._now)
            if age < timedelta(seconds=float(_cfg("SWING_TREND_NIGHT_MKT_SELL_SECONDS"))):
                return "wait"
            self._exit_failed(ex, "market sell open too long")
            ex["retry_after"] = (self._now + timedelta(seconds=float(_cfg("SWING_TREND_NIGHT_MKT_SELL_SECONDS")))).isoformat()
            sent = [self._cancel(v, "market sell open too long") for v in mkts]
            return "acted" if any(sent) else "wait"
        retry_at = _aware(ex.get("retry_after"))
        if (retry_at is not None and self._now < retry_at) or not self._backoff_ok("mkt"):
            return self._protect(r)                             # protected between exit attempts
        if r.live_sells:
            sent = [self._cancel(v, "open: clear resting sells before the market sell") for v in r.live_sells]
            return "acted" if any(sent) else "wait"
        self._last_send = ""
        res = self._send(r, "mkt", held, "day")
        if self._last_send == "rejected":
            self._exit_failed(ex, "market sell rejected")
            return self._protect(r)
        return res

    def _exit_failed(self, ex: dict, why: str) -> None:
        ex["mkt_failures"] = int(ex.get("mkt_failures") or 0) + 1
        self.event("exit_step", step="market_failed", why=why, failures=ex["mkt_failures"])
        if ex["mkt_failures"] >= int(_cfg("SWING_TREND_NIGHT_EXIT_PAGE_FAILURES")):
            self.page("exit_fail", f"trend_night: exit attempt {ex['mkt_failures']} failed ({why}) — stop re-placed, "
                                   f"retrying")

    # ── entry (spec items 1, 3, 4, 7) ─────────────────────────────────────────────────────────────
    def _entry_blockers(self, today: str) -> Optional[str]:
        if not bool(_cfg("SWING_TREND_NIGHT_ENABLED")):
            return "kill flag SWING_TREND_NIGHT_ENABLED is off"
        if self.state.get("entry_blocked_day") == today:
            return "state was rebuilt today — entries blocked for the session"
        halted, why = self.kill_fn(today)
        if halted:
            return why
        return None

    def _measure_q(self) -> Optional[dict]:
        """q = max(QQQ's expanding 99.9th-pct overnight loss, QQQM's own over whatever QQQM nights exist — the max only
        tightens, v8 item 2b); measured once per day. A failed measurement falls back to the last good one, flagged
        (v11 item 4); None only when no measurement has ever succeeded."""
        today = self._now.date()
        cache = self.state.get("q_cache") or {}
        if cache.get("date") == today.isoformat() and cache.get("q"):
            return cache
        level = float(_cfg("SWING_TREND_NIGHT_Q_LEVEL"))
        q_proxy: Optional[float] = None
        rq: Any = []
        rm: Any = []
        try:
            rq = overnight_returns(self.d.daily_history(str(_cfg("SWING_TREND_NIGHT_Q_SYMBOL")), today))
            q_proxy = loss_quantile(rq, level)
            rm = overnight_returns(self.d.daily_history(self.sym, today))
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: q history unavailable: %s", e)
        if q_proxy is None:
            last = self.state.get("q_last_good")
            if isinstance(last, dict) and last.get("q"):
                return dict(last, stale=True, stale_on=today.isoformat())
            return None
        q_own = loss_quantile(rm, level) if len(rm) > 0 else None
        q = max(q_proxy, q_own) if q_own is not None else q_proxy
        cache = {"date": today.isoformat(), "q": q, "q_proxy": q_proxy, "q_own": q_own, "proxy_nights": len(rq),
                 "own_nights": len(rm)}
        self.state["q_cache"] = cache
        self.state["q_last_good"] = cache
        return cache

    def _prior(self) -> dict:
        """The replay prior, recomputed once per day from T1 daily bars (SPY regime × QQQ close→open), with n*, ρ and α
        for the edge and fill-cost reviews. {"ok": False, "why"} when unavailable (v11 item 4: no stored fallback)."""
        today = self._now.date()
        cache = self.state.get("prior_cache") or {}
        if cache.get("date") == today.isoformat():
            return cache
        try:
            spy = self.d.daily_history(str(_cfg("SWING_TREND_NIGHT_REGIME_SYMBOL")), today)
            qqq = self.d.daily_history(str(_cfg("SWING_TREND_NIGHT_Q_SYMBOL")), today)
            pr = replay_prior(spy, qqq, int(_cfg("SWING_TREND_NIGHT_SMA_SESSIONS")),
                              float(_cfg("SWING_TREND_NIGHT_REPLAY_COST_FRAC")))
        except Exception as e:  # noqa: BLE001
            pr = {"ok": False, "why": f"prior bars unavailable: {e!r}", "n": 0}
        alpha = float(_cfg("SWING_TREND_NIGHT_AUTO_OFF_ALPHA"))
        pr["alpha"] = alpha
        if pr.get("ok"):
            pr["rho"] = mixture_rho(float(pr["n_star"]), alpha)
            if pr["rho"] is None:
                pr.update(ok=False, why=f"rho incomputable (n* {pr.get('n_star')})")
        pr["date"] = today.isoformat()
        self.state["prior_cache"] = pr
        self.event("prior", prior={k: pr.get(k) for k in ("ok", "why", "n", "mean", "sd", "n_star", "rho", "alpha",
                                                          "first", "last")})
        return pr

    def _regime_reading(self, sess: Session) -> tuple[Optional[dict], str]:
        n = int(_cfg("SWING_TREND_NIGHT_SMA_SESSIONS"))
        try:
            df = self.d.spy_closed(str(_cfg("SWING_TREND_NIGHT_REGIME_SYMBOL")), n)
        except Exception as e:  # noqa: BLE001
            return None, f"regime bars unavailable: {e!r}"
        if df is None or len(df) == 0:
            return None, "regime bars missing"
        rg = regime(df["close"], n)
        if rg is None:
            return None, "regime incomputable (fewer settled closes than the SMA needs)"
        try:
            last = date.fromisoformat(rg["bar_date"])
            sessions = self.b.calendar(last, sess.day)
        except Exception as e:  # noqa: BLE001
            return None, f"calendar unavailable for the staleness check: {e!r}"
        missing = [d.isoformat() for d, _, _ in sessions if last < d < sess.day]
        if missing:
            return None, f"regime data stale: last settled bar {rg['bar_date']}, sessions missing {missing}"
        return rg, ""

    def _gap_book(self, positions: list, q: float) -> tuple[float, list]:
        """Σ |notional| × own 99th-pct adverse overnight move over every open position (options: 100%)."""
        level = float(_cfg("SWING_TREND_NIGHT_GAP_LEVEL"))
        rows: list = []
        for p in positions:
            sym = str(getattr(p, "symbol", "") or "")
            mv = _num(getattr(p, "market_value", 0))
            if not sym or mv == 0:
                continue
            p99: Optional[float]
            if "option" in _enum(getattr(p, "asset_class", "")):
                p99 = 1.0
            else:
                try:
                    rets = overnight_returns(self.d.daily_history(sym, self._now.date()))
                    p99 = loss_quantile(rets, level) if mv > 0 else gain_quantile(rets, level)
                except Exception as e:  # noqa: BLE001
                    logger.warning("trend_night: %s overnight history unavailable: %s", sym, e)
                    p99 = None
            rows.append({"symbol": sym, "notional": abs(mv), "p99": p99})
        known = [x["p99"] for x in rows if x["p99"] is not None] + [q]
        worst = max(known)
        for x in rows:
            if x["p99"] is None:
                x["p99"], x["charged_worst"] = worst, True
        return sum(x["notional"] * x["p99"] for x in rows), rows

    def _entry_step(self, r: Recon, sess: Session) -> str:
        if sess.close is None or sess.source != "calendar" or not sess.is_session:
            return "ok"
        cl = sess.close
        start = cl - timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_ENTRY_START_MIN")))
        end = cl - timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_ENTRY_END_MIN")))
        fb = cl - timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_FALLBACK_MIN")))
        key = night_key(sess.day)
        n = self._nights().get(key)
        if n and n.get("entry_submitted"):
            return self._fallback(r, key, n) if fb <= self._now < cl else "ok"
        if not (start <= self._now <= end):
            return "ok"
        if n and n.get("decided"):
            return "ok"
        minute = self._now.strftime("%H:%M")
        n = self._night(key)
        if n.get("last_eval_minute") == minute:
            return "ok"
        n["last_eval_minute"] = minute
        today = sess.day.isoformat()

        def skip(reason: str, final: bool, **kw: Any) -> str:
            self.event("skip", night=key, reason=reason, final=final, **kw)
            if final:
                n.update(decided=True, status="skipped", skip_reason=reason)
            return "ok"

        why = self._entry_blockers(today)
        if why:
            return skip(why, True, kill_switch=why)
        if not self._flat(r):
            return skip("not confirmed flat (QQQM held, short, unconfirmed, or a trend_night order working / "
                        "UNKNOWN) — no entry", False, own=r.own, position=r.position,
                        live_sells=len(r.live_sells), live_buys=len(r.live_buys))
        rg, why = self._regime_reading(sess)
        if rg is None:
            return skip(why, False)
        self.event("signal", night=key, regime=rg)
        n["signal"] = rg
        if not rg["on"]:
            return skip("regime off: SPY prior close not above its SMA", True, signal=rg)
        n["close_ms"] = epoch_ms(cl)
        qty, sizing, why = self._size_now(key)
        if qty is None:
            return skip(why, sizing.get("final", False), **({"sizing": sizing} if sizing else {}))
        if qty < 1:
            return skip(f"no size (bound by {sizing.get('bound_by')})", True, sizing=sizing)
        coid = self._coid(key, _ENTRY_CLS, 1)
        ok, adm = self._admit(coid, qty, float(sizing["price"]), float(sizing["q"]), "cls")
        if not ok:
            return skip(f"allocator refused: {adm}", True, sizing=sizing)
        self.event("admission", night=key, coid=coid, approved=True, reason=adm)
        second = self._fresh_position()                # rule 5: R == 0 on a second, fresh read right before the entry
        if second != 0:
            return skip(f"second position read is {second}, not flat — no entry this pass", False)
        n.update(status="entry_pending", qty=qty, q=sizing["q"], b=sizing["b"], sizing=sizing, equity=sizing["equity"],
                 path="cls", date=sess.day.isoformat(), entry_submitted=True, decided=True)
        res = self._submit(key, _ENTRY_CLS, "buy", qty, "cls", coid=coid, path="cls")
        if res == "blocked":
            n.update(entry_submitted=False, decided=False, status="new")
            n["orders"].pop(coid, None)
            self.save()
            return "fail"
        if res == "unknown":
            self.page(f"entry_unknown_{key}", f"trend_night {key}: cls entry UNKNOWN — resolving by client id; no "
                                              f"second entry until resolved")
        return "acted"

    def _size_now(self, key: str) -> tuple[Optional[int], dict, str]:
        """Size from a same-second broker read (v12 item 2): q, price, equity, buying power, K room and the all-tier gap
        book read immediately before the submit. (None, info, why) when an input is unavailable."""
        qm = self._measure_q()
        if not qm:
            return None, {}, "q (overnight loss quantile) unavailable"
        q = float(qm["q"])
        price = self.d.last_price(self.sym)
        if price is None:
            return None, {}, f"{self.sym} last trade unavailable"
        try:
            acct = self.b.account()
            positions = self.b.positions()
        except Exception as e:  # noqa: BLE001
            return None, {}, f"account/positions unreadable: {e!r}"
        if bool(getattr(acct, "trading_blocked", False) or getattr(acct, "account_blocked", False)):
            return None, {"final": True}, "Alpaca account is trading-blocked"
        equity = _num(getattr(acct, "equity", 0))
        bps = [x for x in (_num(getattr(acct, "buying_power", None)), _num(getattr(acct, "regt_buying_power", None)))
               if x > 0]
        bp = min(bps) if bps else 0.0
        try:
            from execution.overnight_capacity import overnight_k
            k = float(overnight_k())
        except Exception as e:  # noqa: BLE001
            return None, {}, f"overnight K unreadable (fail-closed): {e!r}"
        day_lots = day_tier_flatten_lots()
        held, day_adj = post_flatten_positions(positions, day_lots or {})
        gross = sum(abs(_num(getattr(p, "market_value", 0))) for p in held)
        gap_loss, gap_rows = self._gap_book(held, q)
        kill_pct = float(config.PROFILES[str(_cfg("SWING_TREND_NIGHT_KILL_PROFILE"))]["MAX_DAILY_LOSS_PCT"])
        prior = self._prior()
        live = _finite_rets(self.state.get("closed") or [], "ret")
        bb = pooled_budget(prior, live, q)
        if bb.get("pooled_mean") is not None and float(bb["pooled_mean"]) <= 0:
            # board sizing seat: still trades at the owner floor, but the owner is told every night this holds
            self.event("pooled_edge_nonpositive", night=key, pooled_mean=bb["pooled_mean"],
                       pooled_nights=bb.get("pooled_nights"), b=bb["b"])
            self.page(f"pooled_edge_{key}", f"trend_night {key}: pooled replay + live mean night return is "
                                            f"{float(bb['pooled_mean']):.5f} (≤ 0 over {bb.get('pooled_nights')} "
                                            f"nights) — still trading at the owner floor b {bb['b']}; your call "
                                            f"(SWING_TREND_NIGHT_ENABLED)")
        qty, sizing = size_night(equity, price, q, float(bb["b"]), k, gross, gap_loss, kill_pct, bp)
        sizing.update(budget=bb, q_inputs=qm, gap_book=gap_rows, day_lots=day_lots, day_lot_adjustments=day_adj)
        self.event("sizing", night=key, sizing=sizing, kill_switch="clear")
        return qty, sizing, ""

    def _fallback(self, r: Recon, key: str, n: dict) -> str:
        cls_coid, mkt_coid = self._coid(key, _ENTRY_CLS, 1), self._coid(key, _ENTRY_MKT, 1)
        if mkt_coid in n.get("orders", {}):
            return "ok"
        orec = n["orders"].get(cls_coid) or {}
        v = r.views.get(cls_coid)
        if v is None and orec.get("state") in ("submitting", "unknown", "ack"):
            if not n.get("fallback_blocked_logged"):
                n["fallback_blocked_logged"] = True
                self.event("skip", night=key, reason="fallback blocked: cls entry UNKNOWN (client-id resolution first)")
            return "wait"
        dead = orec.get("state") in ("rejected", "absent") or (
            v is not None and v["status"] in ("rejected", "expired") and v["filled_qty"] == 0)
        if not dead:
            return "ok"
        self._release_terminal_lease(cls_coid, v)
        if not self._flat(r):
            return "wait"
        why = self._entry_blockers(sess_day := str(n.get("date") or self._now.date().isoformat()))
        if why:
            n["fallback_skipped"] = why
            self.event("skip", night=key, reason=f"fallback not sent: {why}", day=sess_day)
            return "ok"
        fresh, sizing, why = self._size_now(key)
        if fresh is None:
            self.event("skip", night=key, reason=f"fallback not sent this pass: {why}")
            return "wait"
        qty = min(int(n.get("qty") or 0), int(fresh))
        if qty < 1:
            n["fallback_skipped"] = f"no size (bound by {sizing.get('bound_by')})"
            self.event("skip", night=key, reason=f"fallback not sent: {n['fallback_skipped']}", sizing=sizing)
            return "ok"
        ok, adm = self._admit(mkt_coid, qty, float(sizing["price"]), float(sizing["q"]), "day")
        if not ok:
            n["fallback_skipped"] = f"allocator refused: {adm}"
            self.event("skip", night=key, reason=f"fallback not sent: {n['fallback_skipped']}")
            return "ok"
        n.update(path="fallback", qty=qty, q=sizing["q"], b=sizing["b"], equity=sizing["equity"], fallback_sizing=sizing)
        res = self._submit(key, _ENTRY_MKT, "buy", qty, "day", coid=mkt_coid, path="fallback")
        if res == "unknown":
            self.page(f"entry_unknown_{key}", f"trend_night {key}: fallback entry UNKNOWN — resolving by client id")
        return "acted"

    # ── booking (REPORTING ONLY — nothing here drives an order) ──────────────────────────────────────────────
    def _flat(self, r: Recon) -> bool:
        """F == 0 from readable, fully classified activities, R == 0, nothing working or UNKNOWN (rule 5)."""
        return (r.acts_ok and not r.unclassified and r.own == 0 and r.position == 0 and not r.live_sells
                and not r.live_buys and r.unknown_sell_qty == 0 and r.unknown_buys == 0)

    def _book_step(self, r: Recon) -> None:
        """Book each open night whose own shares are all accounted for by the activities (sold by its own orders, or
        departed through a foreign sale) once the account is confirmed flat. P&L from own sell fills at their activity
        prices; departed shares are an explicit UNBOOKED line at the foreign sale prices — never a P&L of 0."""
        if not self._flat(r):
            return
        derived = (r.derived or {}).get("nights") or {}
        for key in sorted(k for k, n in self._nights().items() if n.get("status") not in ("closed", "unfilled", "skipped")):
            n = self._night(key)
            if self._unresolved(n):
                return
            d = derived.get(key)
            if not d or not d.get("bought"):
                listed_fill = any(v["filled_qty"] > 0 for v in r.views.values() if _night_of(v) == key and _is_entry(v))
                if listed_fill:
                    continue                  # the listing shows a fill whose activity has not arrived: not unfilled
                if self._past_close(key) and n.get("close_ms") is not None and (n.get("entry_submitted") or n.get("orders")):
                    self._set_status(key, "unfilled")
                    self.event("exit", night=key, result="entry not filled", path=n.get("path"))
                continue
            if d["remaining"] != 0:
                continue
            bq = int(d["bought"])
            entry_avg = float(d["bought_cost"]) / bq
            sells, deps = d.get("sells") or [], d.get("departures") or []
            if any(x.get("entry_price") is None or not x.get("price") for x in sells + deps):
                self.page(f"book_prices_{key}", f"trend_night {key}: a fill price is not readable yet — P&L not booked")
                continue
            sq = sum(int(x["qty"]) for x in sells)
            proceeds = math.fsum(int(x["qty"]) * float(x["price"]) for x in sells)
            pnl = math.fsum(int(x["qty"]) * (float(x["price"]) - float(x["entry_price"])) for x in sells)
            dep_q = sum(int(x["qty"]) for x in deps)
            dep_pnl = math.fsum(int(x["qty"]) * (float(x["price"]) - float(x["entry_price"])) for x in deps)
            sold_cost = math.fsum(int(x["qty"]) * float(x["entry_price"]) for x in sells)
            eq = _num(n.get("equity"))
            q_night = self._q_for(key)
            rec = {"night": key, "date": n.get("date"), "exit_date": self._now.date().isoformat(), "qty": bq,
                   "sold_qty": sq, "unaccounted_qty": dep_q, "entry_avg": entry_avg,
                   "exit_avg": proceeds / sq if sq else None, "pnl": pnl, "notional": sold_cost,
                   "ret": pnl / sold_cost if sold_cost > 0 else None, "equity": eq,
                   "ret_equity": pnl / eq if eq > 0 else None, "path": n.get("path"),
                   "exit_paths": sorted({str(x.get("step")) for x in sells}),
                   "order_ids": sorted({str(x["order_id"]) for x in sells} | set(d.get("buy_orders") or [])),
                   "b": n.get("b"), "q": q_night,
                   "loss_budget_usd": (bq * entry_avg * q_night) if q_night else None,
                   "foreign_sold_qty": dep_q, "foreign_fills": deps,
                   "foreign_unbooked_pnl_usd": round(dep_pnl, _TICK_DIGITS), "never_sold_qty": 0}
            if dep_q:
                self.page(f"unaccounted_{key}", f"trend_night {key}: {dep_q} of {bq} sh were sold OUTSIDE trend_night "
                                                f"(unbooked P&L ${dep_pnl:+.2f}) — P&L booked on the {sq} sh it sold")
            self.state["closed"] = (self.state.get("closed") or []) + [rec]
            own_listed = sum((v["filled_qty"] if v["side"] == "buy" else -v["filled_qty"]) for v in r.views.values()
                             if _night_of(v) == key)
            self._append_booked(key, own_listed, dep_q)
            self._set_status(key, "closed", pnl=round(pnl, 2))
            self.event("exit", night=key, price=rec["exit_avg"] or 0.0, size=bq, pnl=round(pnl, 2), result="flat",
                       path=n.get("path"), exit_paths=rec["exit_paths"])
            self.event("night_summary", night=key, summary={k: rec[k] for k in rec if k != "order_ids"},
                       signal=n.get("signal"), sizing=n.get("sizing"))
            self._after_booking(rec)

    @staticmethod
    def _unresolved(n: dict) -> bool:
        return any(o.get("state") in ("submitting", "unknown") for o in (n.get("orders") or {}).values())

    def _past_close(self, key: str) -> bool:
        """True once the night's own session close has passed (no fallback entry is possible any more)."""
        ms = self._night(key).get("close_ms")
        if ms is None:
            return True                           # no entry was ever attempted for this night
        return self._now >= _ms_to_et(int(ms))

    def _after_booking(self, rec: dict) -> None:
        """Page-only reviews after a night is booked (nothing here blocks entry or switches the strategy off)."""
        budget = rec.get("loss_budget_usd")
        # one cent per share of slack: the stop sits at entry × (1 − q) ROUNDED DOWN to the cent, so an ordinary
        # stop-out loses up to a cent a share more than qty × entry × q and must not page
        slack = float(_tick()) * int(rec.get("qty") or 0)
        if budget is not None and float(rec["pnl"]) < -(float(budget) + slack):
            self.event("review", night=rec["night"], rule="night loss above its modelled worst night (qty × entry × q)",
                       pnl=rec["pnl"], loss_budget_usd=budget, qty=rec.get("qty"), q=rec.get("q"))
            self.page(f"loss_review_{rec['night']}", f"trend_night {rec['night']}: night lost ${-float(rec['pnl']):.2f}, "
                                                     f"more than its modelled worst night ${float(budget):.2f} "
                                                     f"({rec.get('qty')} sh × entry × q {rec.get('q')}) — size review")
        self._edge_check()

    def _edge_check(self) -> None:
        """Live mean night return vs the replay prior through the normal-mixture boundary (v10 item 2). PAGES the owner
        with the evidence when crossed; turning the strategy off stays the owner's SWING_TREND_NIGHT_ENABLED."""
        live = _finite_rets(self.state.get("closed") or [], "ret")
        if not live:
            return
        prior = self._prior()
        if not prior.get("ok"):
            self.event("edge_check", live_nights=len(live), result="prior unavailable", why=prior.get("why"))
            return
        hw = mixture_halfwidth(float(prior["var"]), len(live), float(prior["rho"]), float(prior["alpha"]),
                               int(prior["n"]))
        if hw is None:
            self.event("edge_check", live_nights=len(live), result="boundary incomputable")
            return
        live_mean = statistics.fmean(live)
        boundary = float(prior["mean"]) - hw
        crossed = live_mean < boundary
        evidence = {"live_nights": len(live), "live_mean": live_mean, "prior_mean": prior["mean"], "sigma": prior["sd"],
                    "prior_nights": prior["n"], "n_star": prior["n_star"], "rho": prior["rho"], "alpha": prior["alpha"],
                    "boundary": boundary}
        self.event("edge_check", result="below boundary" if crossed else "within boundary", nightly_returns=live,
                   **evidence)
        if crossed:
            self.page("edge_check", "trend_night EDGE CHECK: live mean night return {live_mean:.5f} over {live_nights} "
                                    "night(s) is below the replay boundary {boundary:.5f} (prior mean {prior_mean:.5f}, "
                                    "sigma {sigma:.5f}, N_prior {prior_nights}, rho {rho:.2f}, alpha {alpha}). Entries "
                                    "continue; turning it off is your decision (SWING_TREND_NIGHT_ENABLED). Nightly "
                                    "returns: {rets}".format(rets=[round(x, 5) for x in live], **evidence))

    def _spread_prior(self) -> dict:
        """The quoted round-trip cost sample (v11 item 1): for each of the last SWING_TREND_NIGHT_SPREAD_SESSIONS
        sessions with a next session, the mean of (median QQQM SIP quoted spread in the last minute before that
        session's close / its official close) and (median spread in the first minute after the next session's open /
        its official open) — i.e. half of each spread, summed. The bar print stands in for the quote mid. Once per day.
        {"ok": False} with fewer than two sessions."""
        today = self._now.date()
        cache = self.state.get("spread_cache") or {}
        if cache.get("date") == today.isoformat():
            return cache
        samples: list = []
        why = ""
        try:
            bars = self.d.daily_history(self.sym, today)
            want = int(_cfg("SWING_TREND_NIGHT_SPREAD_SESSIONS"))
            days = [d.date() for d in bars.index[-(want + 1):]]
            cal = {d: (op, cl) for d, op, cl in self.b.calendar(days[0], days[-1])} if days else {}
            minute = timedelta(minutes=1)
            pos = {d.date(): i for i, d in enumerate(bars.index)}
            for d0, d1 in zip(days, days[1:], strict=False):
                if d0 not in cal or d1 not in cal:
                    continue
                close_px = float(bars["close"].iloc[pos[d0]])
                open_px = float(bars["open"].iloc[pos[d1]])
                sc = self.d.spreads(self.sym, cal[d0][1] - minute, cal[d0][1])
                so_ = self.d.spreads(self.sym, cal[d1][0], cal[d1][0] + minute)
                if not sc or not so_ or not (close_px > 0 and open_px > 0):
                    continue
                samples.append(statistics.fmean([statistics.median(sc) / close_px, statistics.median(so_) / open_px]))
        except Exception as e:  # noqa: BLE001
            why = f"spread sample unavailable: {e!r}"
        if len(samples) < 2:
            out = {"date": today.isoformat(), "ok": False, "n": len(samples), "why": why or "prior unavailable"}
        else:
            var = statistics.variance(samples)
            out = {"date": today.isoformat(), "ok": True, "n": len(samples), "mean": statistics.fmean(samples),
                   "var": var, "sd": math.sqrt(var)}
        self.state["spread_cache"] = out
        return out

    def _fill_cost_step(self) -> None:
        """Realized round-trip cost per night (v12 item 1) = ((entry fill − official close) + (official open − exit
        fill)) / official close, quantity-weighted fills, the open being the session after the entry session; measured
        once both bars are settled. Pages when the live mean exceeds prior mean + the mixture half-width (v11 item 1)."""
        closed = self.state.get("closed") or []
        pending = [x for x in closed if x.get("cost") is None and x.get("exit_date")
                   and x["exit_date"] < self._now.date().isoformat() and x.get("exit_avg")]
        if not pending:
            return
        try:
            bars = self.d.daily_history(self.sym, self._now.date())
        except Exception as e:  # noqa: BLE001
            logger.warning("trend_night: fill-cost bars unavailable: %s", e)
            return
        idx = {str(t)[:10]: i for i, t in enumerate(bars.index)}
        for x in pending:
            ie = idx.get(str(x.get("date")))
            if ie is None or ie + 1 >= len(bars):
                continue
            close_px, open_px = float(bars["close"].iloc[ie]), float(bars["open"].iloc[ie + 1])
            if close_px > 0 and open_px > 0:
                x["cost"] = ((float(x["entry_avg"]) - close_px) + (open_px - float(x["exit_avg"]))) / close_px
                self.event("fill_cost", night=x["night"], cost_frac=x["cost"], official_close=close_px,
                           official_open=open_px, entry_avg=x["entry_avg"], exit_avg=x["exit_avg"], path=x.get("path"))
        costs = _finite_rets(closed, "cost")
        if not costs:
            return
        sp, prior = self._spread_prior(), self._prior()
        if not sp.get("ok") or not prior.get("ok"):
            self.event("fill_cost_check", live_nights=len(costs), result="prior unavailable",
                       why=sp.get("why") if not sp.get("ok") else prior.get("why"))
            return
        hw = mixture_halfwidth(float(sp["var"]), len(costs), float(prior["rho"]), float(prior["alpha"]), int(sp["n"]))
        if hw is None:
            self.event("fill_cost_check", live_nights=len(costs), result="boundary incomputable")
            return
        live_mean = statistics.fmean(costs)
        boundary = float(sp["mean"]) + hw
        crossed = live_mean > boundary
        self.event("fill_cost_check", result="above boundary" if crossed else "within boundary", live_nights=len(costs),
                   live_mean=live_mean, prior_mean=sp["mean"], sigma=sp["sd"], prior_sessions=sp["n"],
                   rho=prior["rho"], alpha=prior["alpha"], boundary=boundary, costs=costs)
        if crossed:
            self.page("fill_cost", f"trend_night: mean realized round-trip fill cost {live_mean:.5f} over {len(costs)} "
                                   f"night(s) is above the quoted-spread boundary {boundary:.5f} — review (entries continue)")

    # ── result check (machine-checked line) ───────────────────────────────────────────────────────
    def _result_step(self, r: Recon, sess: Session) -> None:
        if sess.source != "calendar" or not sess.is_session or sess.open is None:
            return
        day = sess.day.isoformat()
        done = self.state.setdefault("result_checks", {}).setdefault(day, {})
        labels = []
        if self._now >= sess.open + timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_RESULT_CHECK_MIN"))) \
                and "open" not in done:
            labels.append("open")
        if sess.close is not None and self._now >= sess.close and "close" not in done:
            labels.append("close")
        for label in labels:
            # target: flat after the open (rule 3) — at the open check now, at the close check at any time in the
            # exit window today (a position bought at today's close is tonight's lot, not a miss)
            flat_now = r.own == 0 and r.position == 0 and not r.live_sells
            if label == "open":
                prior_held = 0 if flat_now else r.held
            else:
                had = (self.state.get("exit_held") or {}).get(day)
                got_flat = (self.state.get("exit_flat") or {}).get(day)
                prior_held = 0 if (not had or got_flat) else r.held
            prior_open = [v["coid"] for v in r.live_sells if v["type"] != "stop"] if label == "open" else []
            pnl_ok, detail = self._pnl_matches(day)
            passed = r.ok and prior_held == 0 and not prior_open and pnl_ok
            done[label] = {"passed": passed, "at": self._now.isoformat()}
            self.event("trend_night_audit", check=label, passed=passed, prior_held=prior_held, prior_open=prior_open,
                       reconcile_ok=r.ok, pnl=detail)
            if label == "open":
                self._fill_cost_step()
            if not passed:
                sev = "MISS (exit retries continue)" if label == "open" else "FAILURE"
                self.page(f"result_{label}_{day}", f"trend_night result check {label}: {sev} — held from prior nights "
                                                   f"{prior_held}, open TN orders {prior_open}, P&L {detail}")

    def _pnl_matches(self, day: str) -> tuple[bool, dict]:
        recs = [x for x in self.state.get("closed") or [] if x.get("exit_date") == day]
        if not recs:
            return True, {"nights": 0}
        out: dict = {}
        ok = True
        dates = []
        for x in recs:
            try:
                dates.append(date.fromisoformat(str(x.get("date"))))
            except ValueError:
                # a night rebuilt after lost state has no entry date: list from its night key instead
                logger.warning("trend_night: night %s has no entry date (%r) — listing fills from its night key",
                               x.get("night"), x.get("date"))
                try:
                    dates.append(datetime.strptime(str(x.get("night")), "%Y%m%d").date())
                except ValueError:
                    return False, {"error": f"night {x.get('night')!r} has neither an entry date nor a night key"}
        try:
            after = datetime(min(dates).year, min(dates).month, min(dates).day, tzinfo=ET)
            fills = self.b.fills(after - timedelta(days=1))
        except Exception as e:  # noqa: BLE001
            return False, {"error": f"fill activities unreadable: {e!r}"}
        cent = float(_tick())
        for x in recs:
            ids = set(x.get("order_ids") or [])
            mine = [f for f in fills if str(f.get("order_id")) in ids]
            got = fifo_pnl(mine)
            # a cent per fill of tolerance: Alpaca's average fill prices are rounded, so half-cent rounding must
            # never read as a mismatch
            match = got is not None and (round(abs(got - float(x["pnl"])), _TICK_DIGITS)
                                         <= round(cent * max(len(mine), 1), _TICK_DIGITS))
            bought = sum(_num(f.get("qty")) for f in mine if str(f.get("side", "")).lower().startswith("buy"))
            sold = sum(_num(f.get("qty")) for f in mine if not str(f.get("side", "")).lower().startswith("buy"))
            unbooked = int(x.get("unaccounted_qty") or 0)
            line: dict = {"booked": round(float(x["pnl"]), _TICK_DIGITS),
                    "fills_fifo": None if got is None else round(got, _TICK_DIGITS), "fills": len(mine),
                    "bought": bought, "sold": sold}
            if unbooked or bought != sold:
                # rule 7: shares bought but never sold by a listed trend_night order are an explicit UNBOOKED line —
                # the night's P&L is incomplete, so the check fails (never a silent P&L of 0 for them)
                unb = unbooked or int(bought - sold)
                line["unbooked_qty"] = unb
                line["unbooked_cost_usd"] = round(_num(x.get("entry_avg")) * unb, _TICK_DIGITS)
                line["foreign_fills"] = x.get("foreign_fills") or []
                line["foreign_unbooked_pnl_usd"] = x.get("foreign_unbooked_pnl_usd")
                match = False
            line["match"] = match
            out[x["night"]] = line
            ok = ok and match
        return ok, out

    # ── one pass ──────────────────────────────────────────────────────────────────────────────────
    def _flat_and_idle(self) -> bool:
        if not self.state.get("bootstrapped"):
            return False
        if int(self.state.get("held") or 0) != 0 or int(self.state.get("own") or 0) != 0 \
                or int(self.state.get("broker_qty") or 0) != 0:
            return False
        for n in self._nights().values():
            if n.get("status") in _ACTIVE_NIGHT:
                return False
            if any(o.get("state") in ("submitting", "unknown") for o in n.get("orders", {}).values()):
                return False
        return True

    def _windows(self, sess: Session) -> tuple[bool, bool]:
        close_win = open_win = False
        if sess.is_session and sess.close is not None and sess.source == "calendar":
            close_win = (sess.close - timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_ENTRY_START_MIN")))
                         <= self._now <= sess.close + timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_CLOSE_POLL_MIN"))))
        if sess.is_session and sess.open is not None:
            open_win = (sess.open - timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_SWAP_MIN")))
                        <= self._now <= sess.open + timedelta(minutes=float(_cfg("SWING_TREND_NIGHT_OPEN_POLL_MIN"))))
        return close_win, open_win

    def tick(self) -> TickOut:
        """One position-driven pass (cold review round 6): read the broker, confirm the position, then — from the
        two-source quantity Q and the open orders alone — stand down on a short, or exit / protect a long, then consider
        entry.
        Booking and the reviews run from fills afterwards and never place an order."""
        self._now = self.now_fn()
        sess = self.session()
        close_win, open_win = self._windows(sess)
        result_due = sess.source == "calendar" and sess.is_session and sess.open is not None and (
            "open" not in (self.state.get("result_checks", {}).get(sess.day.isoformat()) or {})
            and self._now >= sess.open)
        if self._flat_and_idle() and not close_win and not open_win and not result_due:
            self.save()
            return TickOut(note="flat, outside every window — no-op")
        r = self.reconcile()
        if not r.ok:
            self.event("reconcile_blocked", reason=r.reason)
            self.page("reconcile", f"trend_night: broker unreadable — {r.reason}. Submitting nothing; retrying.")
            self.save()
            return TickOut(keep_alive=close_win or open_win, note=r.reason)
        self.state["bootstrapped"] = True
        self.state["held"] = int(r.held)              # read by unmanaged_positions (trend_night_quantities)
        self.state["own"], self.state["broker_qty"] = int(r.own), int(r.position)
        self.state["held_at"] = self._now.isoformat()
        self._log_fills(r)
        for v in r.views.values():                    # reporting status only
            k = _night_of(v)
            if _is_entry(v) and v["filled_qty"] > 0 and self._night(k).get("status") in ("new", "entry_pending"):
                self._set_status(k, "holding")
        unknown = self._note_unknown(r)
        if r.short > 0:
            # rule 3: the account is short — not caused by trend_night (no sell exceeds min(F, R)); the owner's to
            # handle. Cancel any resting trend_night sell, submit nothing, block entries, page every 30 min.
            self.page("short", f"trend_night: the broker shows {self.sym} SHORT {r.short} sh (trend_night's own orders "
                               f"account for {r.own}) — submitting NOTHING; entries blocked until you resolve it")
            sent = [self._cancel(v, "account short: no sell may rest") for v in r.live_sells]
            res = "acted" if any(sent) else "wait"
        elif unknown and r.unknown_sell_qty > 0:
            res = "wait"
        elif self._exit_window(sess) and r.held > 0:
            res = self._exit(r, sess)
        else:
            res = self._protect(r)
        acted, pending = res == "acted", res in ("wait", "fail")
        if self._exit_window(sess):
            day_key = sess.day.isoformat()
            if r.held > 0:
                self.state.setdefault("exit_held", {})[day_key] = True
            else:
                self.state.setdefault("exit_flat", {})[day_key] = True
        er = self._entry_step(r, sess)
        acted = acted or er == "acted"
        pending = pending or er == "wait"
        self._book_step(r)
        self._checkpoint(r)
        self._result_step(r, sess)
        self.save()
        exit_retry = r.held > 0 and self._exit_window(sess) and sess.open is not None and self._now >= sess.open
        pending = pending or bool(r.live_buys) or unknown or any(v["type"] != "stop" for v in r.live_sells)
        return TickOut(acted=acted, keep_alive=(close_win or open_win or exit_retry or r.short > 0) and (pending or acted),
                       note=str(res))

    def run(self) -> int:
        """Ticks until nothing is pending inside a window. After an action it re-passes at once (no wait); while
        waiting on the broker it polls every SWING_TREND_NIGHT_POLL_SECONDS."""
        ok, why = self.paper_ok()
        if not ok:
            self.page("paper", f"trend_night: paper assertion FAILED at start — every submit blocked ({why})")
        passes = 0
        while True:
            try:
                out = self.tick()
            except Exception as e:  # noqa: BLE001 — never raise out of the cron; the next minute retries
                logger.exception("trend_night tick failed")
                self.page("tick_error", f"trend_night: tick raised {e!r} — retrying next pass")
                return 1
            if not out.keep_alive:
                return 0
            if out.acted and passes < _RECONCILE_RETRIES:
                passes += 1
                continue
            passes = 0
            self.sleep_fn(float(_cfg("SWING_TREND_NIGHT_POLL_SECONDS")))


# ── helpers for other modules (the QQQM reservation — v8 item 1) ──────────────────────────────────────────────────
def trend_night_symbols() -> set:
    """{SWING_TREND_NIGHT_SYMBOL}, ALWAYS — no state file is read, so a lost or corrupt state file can never
    un-protect a held lot. main.py startup, orphan_manager (adoption + closed-market emergency stops), stop_protection
    and unmanaged_positions exclude or credit it; no other tier trades the symbol."""
    return {str(_cfg("SWING_TREND_NIGHT_SYMBOL")).upper()}


def trend_night_quantities(state_path: Optional[Path] = None) -> dict:
    """{SYMBOL: shares this sub-strategy holds} from the runner's last broker-reconciled `held` (unmanaged_positions
    credits them to trend_night). {} when the runner has never written state. RAISES on an unreadable state file, so
    the alarm treats the source as unreadable (last good reading + one page) instead of reading it as flat."""
    path = Path(state_path) if state_path is not None else _DEFAULT_STATE
    if not path.exists():
        return {}
    d = json.loads(path.read_text())
    if not isinstance(d, dict):
        raise ValueError("trend_night state is not a dict")
    held = int(d.get("held") or 0)
    if held < 0:
        raise ValueError(f"trend_night state held {held} is negative")
    return {str(_cfg("SWING_TREND_NIGHT_SYMBOL")).upper(): float(held)} if held else {}
