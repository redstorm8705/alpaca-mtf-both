# ruff: noqa: E501 — HTML template strings are intentionally long; line-wrapping breaks HTML
"""
generate_dashboard.py
Writes dashboard.html from live Alpaca data + trade_log.json + bot state files.
Called automatically by main.py at the end of each run_cycle() scan.
Can also be run manually: python3 generate_dashboard.py

Reads:
  - Alpaca /account + /positions + /orders (live)
  - trade_log.json (open/closed trades, stats)
  - logs/hybrid_state.json (SPY event type, scans_left)
  - logs/eod_YYYY-MM-DD.json (today's P&L baseline)

Writes:
  - dashboard.html (atomic write via tmp file)
"""

import os
import sys
import json
import logging
import math
import time
import re
import config as bot_config
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from reporting.metrics import compute_lifetime_stats, _net_deposits
from reporting.html_ui import (
    PRIMARY_NAV_CSS, TIER_COLORS, TIER_LABELS, normalize_tier_mapping,
    primary_nav, tier_badges,
)
from tier_names import TIER_IDS, canonical_tier
from ui_tokens import LIVE_CLOCK_HTML

# Load .env explicitly so API keys are available whether this module is
# imported from within main.py's process or run standalone. Without this,
# _load_alpaca() falls back to os.getenv() which returns empty strings if
# the calling shell never exported ALPACA_API_KEY / ALPACA_SECRET_KEY.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent / ".env", override=False)
except Exception as _dotenv_e:
    # dotenv optional — API keys may already be in environment from main.py
    print(
        "[generate_dashboard] dotenv load failed"
        f" (keys may still be in env): {_dotenv_e}",
        file=sys.stderr,
    )

# NOTE: No RTH block here — this module is imported by main.py and runs during
# trading hours by design. RTH guardrail #5 applies to standalone analysis
# scripts only, not to dashboard generators called by the bot itself.

ET  = ZoneInfo("America/New_York")
PT  = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).parent.resolve()
LOG_DIR = ROOT / "logs"
_ALPACA_OPEN_ORDER_PAGE_SIZE = 500  # Alpaca Trading API maximum page size.

logger = logging.getLogger("generate_dashboard")
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


# ── Data loaders ─────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default  # expected on first run or missing state file — silent
    except Exception as _load_e:
        logger.debug("_load_json(%s): load failed — %s", path.name, _load_e)
        return default


def _load_trade_log():
    return _load_json(ROOT / "trade_log.json", {"open": [], "closed":[], "stats": {}})


def _load_hybrid_state():
    # P0-REGIME: apply the same guards as main.py's _load_hybrid_state().
    # Without this, a stale hybrid_state.json from yesterday shows a prior-day
    # event on the dashboard while the bot (which cleared on startup) shows CLEAR
    # on the scan page — contradictory states for the operator.
    data = _load_json(LOG_DIR / "hybrid_state.json", {})
    if data:
        saved_at   = data.get("saved_at", "")
        today_et   = datetime.now(ET).strftime("%Y-%m-%d")
        scans_left = int(data.get("scans_left", 0))
        if not saved_at.startswith(today_et) or scans_left <= 0:
            return {}
    return data


def _load_today_eod():
    today = datetime.now(PT).date().isoformat()
    return _load_json(LOG_DIR / f"eod_{today}.json", {})


def _load_bot_status():
    return _load_json(LOG_DIR / "bot_status.json", {})


def _load_market_news():
    return _load_json(LOG_DIR / "market_news.json", {"items":[]})


def _load_gex_snapshot():
    return _load_json(LOG_DIR / "gex_snapshot.json", {})


def _load_durable_kill_state(limit_pct: float) -> dict:
    """Return today's exact durable trading-loss latch, or no usable state."""
    state = _load_json(ROOT / "data" / "state" / "kill_switch_state.json", {})
    try:
        evaluated = datetime.fromisoformat(str(state["evaluated_at"]))
        trading_pnl = float(state["trading_pnl"])
        daily_start = float(state["daily_start_value"])
        loss_pct = float(state["loss_pct"])
        session_date = datetime.now(ET).date().isoformat()
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != 2
            or state.get("session_date_et") != session_date
            or evaluated.tzinfo is None
            or evaluated.astimezone(ET).date().isoformat() != session_date
            or state.get("killed") is not True
            or state.get("halt_entries") is not True
            or state.get("cause") != "GENUINE_TRADING_LOSS"
            or state.get("attribution_exact") is not True
            or not all(math.isfinite(value) for value in (
                trading_pnl, daily_start, loss_pct,
            ))
            or daily_start <= 0
            or trading_pnl / daily_start != loss_pct
            or loss_pct > -limit_pct
        ):
            return {}
        return state
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}


def _durable_kill_matches(snapshot: dict, state: dict | None = None) -> bool:
    """Prove a killed snapshot matches today's durable trading-loss latch."""
    state = state or {}
    if not state or snapshot.get("killed") is not True:
        return False
    try:
        pairs = (
            (state["trading_pnl"], snapshot["trading_pnl"]),
            (state["daily_start_value"], snapshot["daily_start_value"]),
        )
        return all(
            math.isfinite(float(left))
            and math.isfinite(float(right))
            and math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-9)
            for left, right in pairs
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _load_kill_switch_snapshot() -> dict:
    """Strict read of the risk engine's exact, current-session display snapshot."""
    data = _load_json(LOG_DIR / "kill_switch_snapshot.json", {})
    if not isinstance(data, dict) or data.get("schema") != 1:
        return {}
    try:
        generated = datetime.fromisoformat(
            str(data["generated_at_utc"]).replace("Z", "+00:00")
        )
        if generated.tzinfo is None:
            return {}
        now_utc = datetime.now(ZoneInfo("UTC"))
        session_date_et = generated.astimezone(ET).date().isoformat()
        max_age_seconds = float(data["max_age_seconds"])
        scan_minutes = max(
            float(bot_config.SCAN_INTERVAL_INTRADAY),
            float(bot_config.DAYTRADE_SCAN_INTERVAL_MIN),
        )
        expected_max_age_seconds = (
            scan_minutes * 60.0
            + scan_minutes * 60.0
            + scan_minutes * 60.0
        )
        age_seconds = (now_utc - generated.astimezone(ZoneInfo("UTC"))).total_seconds()
        if (
            session_date_et != datetime.now(ET).date().isoformat()
            or data.get("session_date_et") != session_date_et
            or not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0
            or not math.isclose(
                max_age_seconds,
                expected_max_age_seconds,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or age_seconds < 0
        ):
            return {}
        raw_numbers = [
            data["trading_pnl"],
            data["daily_start_value"],
            data["limit_pct"],
            data["limit_usd"],
            data["consumed_pct"],
        ]
        if any(isinstance(value, bool) for value in raw_numbers):
            return {}
        trading_pnl, daily_start, limit_pct, limit_usd, consumed_pct = map(
            float, raw_numbers
        )
        if (
            not all(math.isfinite(value) for value in (
                trading_pnl, daily_start, limit_pct, limit_usd, consumed_pct
            ))
            or daily_start <= 0
            or not 0 < limit_pct < 1
            or limit_usd <= 0
            or consumed_pct < 0
            or data.get("attribution_exact") is not True
            or not isinstance(data.get("killed"), bool)
            or data.get("included_tiers") != ["day", "swing"]
            or data.get("excluded_held_tiers") != ["qhm", "forever_6"]
        ):
            return {}
        expected_limit = daily_start * limit_pct
        expected_consumed = abs(min(trading_pnl, 0.0)) / expected_limit * 100.0
        if not math.isclose(limit_usd, expected_limit, rel_tol=1e-12, abs_tol=1e-9):
            return {}
        if not math.isclose(
            consumed_pct, expected_consumed, rel_tol=1e-12, abs_tol=1e-9
        ):
            return {}
        expected_killed = trading_pnl / daily_start <= -limit_pct
        if data["killed"] is not expected_killed:
            return {}
        durable_kill = _load_durable_kill_state(limit_pct)
        durable_match = _durable_kill_matches(data, durable_kill)
        if (
            (durable_kill and not durable_match)
            or (age_seconds > max_age_seconds and not durable_match)
        ):
            return {}
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}
    clean = dict(data)
    clean.update({
        "generated_at": generated,
        "trading_pnl": trading_pnl,
        "daily_start_value": daily_start,
        "limit_pct": limit_pct,
        "limit_usd": limit_usd,
        "consumed_pct": consumed_pct,
        "stale_trigger_snapshot": age_seconds > max_age_seconds,
    })
    return clean


def _load_edge_snapshot() -> dict:
    data = _load_json(LOG_DIR / "strategy_edge_snapshot.json", {})
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("integrity_ok") is not True:
        return {}
    try:
        generated = datetime.fromisoformat(str(data["generated_at_utc"]).replace("Z", "+00:00"))
        age = datetime.now(ZoneInfo("UTC")) - generated
        if age.total_seconds() < 0 or age > timedelta(hours=36):
            return {}
        overall = data["overall"]
        by_tier = data["by_tier"]
        if not isinstance(overall, dict) or not isinstance(by_tier, dict):
            return {}
        def _valid_metrics(metrics: object) -> bool:
            if not isinstance(metrics, dict):
                return False
            completed = float(metrics.get("completed_trades", 0))
            realized = float(metrics.get("realized_pnl", 0))
            win_rate = metrics.get("win_rate")
            profit_factor = metrics.get("profit_factor")
            return (
                math.isfinite(completed) and completed >= 0 and completed.is_integer()
                and math.isfinite(realized)
                and (win_rate is None or (
                    math.isfinite(float(win_rate)) and 0 <= float(win_rate) <= 100
                ))
                and (profit_factor is None or (
                    math.isfinite(float(profit_factor)) and float(profit_factor) >= 0
                ))
            )
        if not _valid_metrics(overall):
            return {}
        canonical_by_tier = normalize_tier_mapping(by_tier)
        if any(not _valid_metrics(metrics) for metrics in canonical_by_tier.values()):
            return {}
        data = dict(data)
        data["by_tier"] = canonical_by_tier
    except (KeyError, TypeError, ValueError, OverflowError):
        return {}
    return data


def _load_ownership_positions() -> dict:
    data = _load_json(ROOT / "data" / "state" / "ownership_ledger.json", {})
    positions = data.get("positions", {}) if isinstance(data, dict) else {}
    return positions if isinstance(positions, dict) else {}


def _canonical_position_tiers(rec: object, net_qty: float) -> list[tuple[str, float]]:
    """Validate and canonicalize one ownership-ledger position for display."""
    if not isinstance(rec, dict):
        return []
    tiers = rec.get("tiers")
    if not isinstance(tiers, dict):
        return []
    try:
        raw_net = rec.get("alpaca_net_qty", 0.0)
        raw_drift = rec.get("drift", 0.0)
        if any(isinstance(value, bool) for value in (raw_net, raw_drift, net_qty)):
            return []
        ledger_net = float(raw_net or 0.0)
        drift = float(raw_drift or 0.0)
        if not all(math.isfinite(value) for value in (ledger_net, drift, net_qty)):
            return []
        normalized = normalize_tier_mapping(tiers)
        rows = []
        for tier, claim in normalized.items():
            if (
                tier == "unattributed"
                or not isinstance(claim, dict)
                or "qty" not in claim
                or isinstance(claim["qty"], bool)
            ):
                return []
            qty = float(claim["qty"])
            if not math.isfinite(qty):
                return []
            rows.append((tier, qty))
    except (TypeError, ValueError, OverflowError):
        return []
    if abs(ledger_net - net_qty) > 1e-6 or abs(drift) > 1e-6:
        return []
    active = [(tier, qty) for tier, qty in rows if abs(qty) > 1e-9]
    if abs(sum(qty for _tier, qty in active) - net_qty) > 1e-6:
        return []
    return active


def _order_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _remaining_order_qty(order: dict) -> float | None:
    qty = _order_number(order.get("qty"))
    filled = _order_number(order.get("filled_qty"))
    if qty is None or filled is None or qty <= 0 or filled < 0 or filled > qty:
        return None
    remaining = qty - filled
    return remaining if remaining > 0 else None


def _order_tier(order: dict) -> str | None:
    try:
        from execution.ownership_guard import tier_of_coid

        legacy = tier_of_coid(order.get("client_order_id"))
        if legacy is None:
            legacy = tier_of_coid(order.get("owner_client_order_id"))
        return canonical_tier(legacy) if legacy else None
    except (ImportError, TypeError, ValueError):
        return None


def _position_protection(
    position: dict,
    tiers: list[tuple[str, float]],
    orders: list[dict],
    *,
    orders_available: bool,
) -> list[dict]:
    """Join one broker position's owned tier lots to its live reducing orders."""
    symbol = str(position.get("symbol") or "").upper()
    side = str(position.get("side") or "").lower()
    current = _order_number(position.get("current_price"))
    reducing_side = "sell" if side == "long" else ("buy" if side == "short" else "")
    stop_types = {"stop", "stop_limit", "trailing_stop"}
    # Alpaca reports the inactive child of a live OCO exit pair as HELD. It is
    # still the broker-native protective leg (2026-10-23 AMZN:
    # six-share BUY stop at $250.25).  Exact parent tier, reducing side, stop
    # type, price, and remaining quantity checks below still apply.
    protective_statuses = {"accepted", "new", "held", "partially_filled"}
    relevant: dict[str, list[dict]] = {}
    if orders_available and reducing_side:
        for order in orders:
            status = str(order.get("status") or "").lower()
            # HELD has two materially different meanings. An OCO stop is a
            # live alternative exit for an existing position (the sibling
            # limit is NEW); a bracket-entry stop is conditional on an entry
            # that may not have filled.  Count only the broker-proven OCO case.
            owner_status = str(order.get("owner_status") or "").lower()
            owner_class = str(
                order.get("owner_order_class") or order.get("order_class") or ""
            ).lower()
            held_oco = status == "held" and owner_class == "oco" and owner_status in {
                "accepted", "new", "partially_filled",
            }
            # A bracket's stop leg stays HELD at Alpaca until it triggers; once the
            # bracket's entry (parent) is FILLED that held leg is the live protective
            # stop for the filled shares (2026-11-04 MSFT Day: parent buy 2 filled,
            # child sell stop $548.51 held).  An unfilled parent's leg is not counted.
            held_bracket = (
                status == "held" and owner_class == "bracket" and owner_status == "filled"
            )
            if (
                str(order.get("symbol") or "").upper() != symbol
                or str(order.get("side") or "").lower() != reducing_side
                or status not in protective_statuses
                or (status == "held" and not (held_oco or held_bracket))
            ):
                continue
            owner = _order_tier(order)
            if owner:
                relevant.setdefault(owner, []).append(order)

    facts = []
    for tier, tier_qty in tiers:
        owned = abs(float(tier_qty))
        tier_orders = relevant.get(tier, [])
        stop_orders = [
            order for order in tier_orders
            if str(order.get("type") or "").lower() in stop_types
            and _remaining_order_qty(order) is not None
            and (_order_number(order.get("stop_price")) or 0.0) > 0
        ]
        stop_details = sorted(
            (
                float(order["stop_price"]),
                float(_remaining_order_qty(order) or 0.0),
            )
            for order in stop_orders
        )
        stop_qty = sum(qty for _price, qty in stop_details)
        targets = sorted(
            (
                float(order["limit_price"]),
                float(_remaining_order_qty(order) or 0.0),
            )
            for order in tier_orders
            if str(order.get("type") or "").lower() == "limit"
            and _remaining_order_qty(order) is not None
            and (_order_number(order.get("limit_price")) or 0.0) > 0
        )
        breached = bool(stop_details) and (
            any(price >= current for price, _qty in stop_details)
            if side == "long" and current is not None and current > 0
            else any(price <= current for price, _qty in stop_details)
            if side == "short" and current is not None and current > 0
            else False
        )
        if not orders_available or current is None or current <= 0:
            status = "UNAVAILABLE"
        elif tier == "unattributed":
            status = "UNATTRIBUTED"
        elif breached:
            status = "BREACHED"
        elif stop_qty == 0:
            status = "MISSING"
        elif stop_qty < owned:
            status = "PARTIAL"
        elif stop_qty > owned:
            status = "OVER-COVERED"
        else:
            status = "PROTECTED"
        distance = None
        if current and current > 0 and len(stop_details) == 1:
            stop_price = stop_details[0][0]
            distance = (
                (current - stop_price) / current * 100.0
                if side == "long"
                else (stop_price - current) / current * 100.0
            )
        facts.append({
            "symbol": symbol,
            "tier": tier,
            "owned_qty": owned,
            "stop_qty": stop_qty,
            "stops": stop_details,
            "targets": targets,
            "distance_pct": distance,
            "status": status,
        })
    return facts


# ── Per-tier exit plans (display only; local state files, no network) ───────

_UNKNOWN = object()  # a tier's state source could not be read → its cells render "—"


def _session_holidays() -> tuple[set, set]:
    """(full-closure holiday dates, years the list covers) from events.calendar."""
    from events.calendar import STATIC_EVENTS, EventRisk, EventType

    days = set()
    for ev in STATIC_EVENTS:
        if ev.get("type") == EventType.MARKET_HOLIDAY and ev.get("risk") == EventRisk.BLACKOUT:
            days.add(datetime.strptime(str(ev["date"]), "%Y-%m-%d").date())
    return days, {d.year for d in days}


def _nth_session(start_iso: str, n: int):
    """Date of the n-th trading session counting `start_iso` as session 1, or None
    when the holiday list does not cover every year walked (never guessed)."""
    holidays, years = _session_holidays()
    day = datetime.strptime(start_iso, "%Y-%m-%d").date()
    count = 0
    while True:
        if day.year not in years:
            return None
        if day.weekday() < 5 and day not in holidays:
            count += 1
            if count >= n:
                return day
        day += timedelta(days=1)


def _fmt_day(day, today) -> str:
    return day.strftime("%b %-d" if day.year == today.year else "%b %-d %Y")


def _load_tier_plans(ownership_positions: dict, qhm_holds: dict) -> dict:
    """Read every tier's own open-lot state once per render. Each source fails
    independently to _UNKNOWN; nothing here touches the network or the broker."""
    plans: dict = {"qhm": qhm_holds if isinstance(qhm_holds, dict) else _UNKNOWN}
    state_dir = ROOT / "data" / "state"
    try:
        raw = json.loads((state_dir / "swing_breakout.json").read_text())
        plans["breakout"] = {
            str(sym).upper(): rec
            for sym, rec in (raw.get("positions") or {}).items()
            if isinstance(rec, dict) and rec.get("status") in ("open", "exit_unverified")
        }
    except FileNotFoundError:
        plans["breakout"] = {}
    except Exception as e:
        logger.warning("tier plans: swing_breakout state unreadable: %s", e)
        plans["breakout"] = _UNKNOWN
    try:
        from execution.swing_overnight import trend_night_quantities

        plans["trend_night"] = trend_night_quantities()
        plans["trend_night_symbol"] = str(bot_config.SWING_TREND_NIGHT_SYMBOL).upper()
    except Exception as e:
        logger.warning("tier plans: trend-night state unreadable: %s", e)
        plans["trend_night"] = _UNKNOWN
    try:
        from strategy import day_tier_logger

        day_lots: dict = {}
        for lot in day_tier_logger.open_trades_from_log().values():
            day_lots.setdefault(str(lot.get("symbol") or "").upper(), []).append(lot)
        plans["day"] = day_lots
    except Exception as e:
        logger.warning("tier plans: day-tier event log unreadable: %s", e)
        plans["day"] = _UNKNOWN
    try:
        day_state = _load_json(LOG_DIR / "day_tier_state.json", {})
        plans["day_promoting"] = {
            str(v.get("symbol") or "").upper()
            for k, v in day_state.items()
            if str(k).startswith("entry::") and isinstance(v, dict)
            and v.get("state") == "promote_pending"
        }
    except Exception as e:
        logger.warning("tier plans: day-tier state unreadable: %s", e)
        plans["day_promoting"] = set()
    plans["day_flat_pt"] = None
    try:
        cal = _load_json(state_dir / "swing_trend_night.json", {}).get("calendar") or {}
        today_et = datetime.now(ET).date().isoformat()
        if cal.get("date") == today_et and cal.get("is_session") is True and cal.get("close"):
            close = datetime.fromisoformat(str(cal["close"]))
            if close.tzinfo is not None:
                flat = close - timedelta(minutes=float(bot_config.DAYTRADE_FORCE_FLAT_MINUTES))
                plans["day_flat_pt"] = flat.astimezone(PT).strftime("%-I:%M %p PT")
    except Exception as e:
        logger.warning("tier plans: session calendar unreadable: %s", e)
    plans["qhm_trim"] = _load_json(state_dir / "qhm_earnings_trim.json", _UNKNOWN)
    plans["ledger"] = ownership_positions if isinstance(ownership_positions, dict) else {}
    return plans


_TREND_LEVEL_CACHE: dict = {}
_TREND_LEVEL_MISS: dict = {}
# Retry a failed bar read after one Swing exit-check window (the level is only acted on near the close).
_TREND_LEVEL_RETRY_S = 60 * float(getattr(bot_config, "SWING_BREAKOUT_EXIT_MINUTES", 10))


def _swing_trend_level(sym: str, today) -> float | None:
    """The Swing breakout exit level for `sym`: the lowest low of the last 20 COMPLETED sessions — the same
    `prior_low(df, 20)` the tier checks against the live price near the close (execution/swing_breakout_manager.py
    run_exit_check). T1 Alpaca daily bars via data.fetcher. Cached per symbol per day (the level only changes once
    a session closes). Display only; None on any failure (the cell then shows the rule as text)."""
    key = (str(sym).upper(), str(today))
    if key in _TREND_LEVEL_CACHE:
        return _TREND_LEVEL_CACHE[key]
    miss_at = _TREND_LEVEL_MISS.get(key)
    if miss_at is not None and time.monotonic() - miss_at < _TREND_LEVEL_RETRY_S:
        return None          # a recent fetch failed: do not re-fetch on every render
    level = None
    try:
        from data.fetcher import fetch_closed_bars
        from execution.swing_breakout_manager import prior_low

        df = fetch_closed_bars(key[0], getattr(bot_config, "TF_DAILY", "1Day"), num_bars=260).df
        level = prior_low(df, 20)
    except Exception as e:
        logger.warning("swing trend level unavailable for %s: %s", key[0], e)
        _TREND_LEVEL_MISS[key] = time.monotonic()
        return None
    if level is None:
        _TREND_LEVEL_MISS[key] = time.monotonic()
        return None
    if level is not None:
        _TREND_LEVEL_CACHE.clear() if len(_TREND_LEVEL_CACHE) > 200 else None
        _TREND_LEVEL_CACHE[key] = level
        logger.info("swing trend exit level %s %s: price near close < $%.2f (prior 20-session low) "
                    "[change=dashboard-held-leg-1104]", key[0], key[1], level)
    return level


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _limit_targets(fact: dict | None) -> list[str]:
    return [
        f"limit {_money(px)} ({q:g})" for px, q in ((fact or {}).get("targets") or [])
    ]


def _weighted_basis(lots: list[tuple[float, float]], owned: float) -> float | None:
    """Qty-weighted basis when the lots explain exactly the owned quantity."""
    total = sum(q for q, _px in lots)
    if not lots or abs(total - owned) > 1e-6 or any(px <= 0 for _q, px in lots):
        return None
    return sum(q * px for q, px in lots) / total


def _tier_slice_plan(
    tier: str, sym: str, owned: float, plans: dict, td: dict, fact: dict | None, today,
) -> tuple[float | None, str]:
    """(tier's own entry basis or None, target cell HTML) for one (symbol, tier) slice.
    Every value comes from that tier's own state or a live broker limit order;
    a rule whose level is not in state is shown as text, never as a number."""
    parts = _limit_targets(fact)
    basis = None
    if tier == "swing":
        breakout = plans.get("breakout")
        night = plans.get("trend_night")
        if breakout is _UNKNOWN and night is _UNKNOWN:
            return None, "—"
        lots: list[tuple[float, float]] = []
        rec = breakout.get(sym) if isinstance(breakout, dict) else None
        if rec:
            qty = float(rec.get("qty") or 0)
            lots.append((qty, float(rec.get("entry_px") or 0)))
            max_hold = int(bot_config.SWING_BREAKOUT_MAX_HOLD)
            exit_day = _nth_session(str(rec.get("entry_date")), max_hold)
            time_txt = (
                f"time exit {_fmt_day(exit_day, today)}" if exit_day
                else f"time exit on session {max_hold}"
            )
            parts.append(f"no profit target (trend exit) · {time_txt}")
        if isinstance(night, dict) and night.get(sym):
            parts.append("sells at next open")
        if td.get("target"):
            parts.append(_money(float(td["target"])))
            legacy_qty = td.get("qty_remaining") or td.get("qty")
            if td.get("entry_price") and legacy_qty:
                lots.append((abs(float(legacy_qty)), float(td["entry_price"])))
        basis = _weighted_basis(lots, owned)
    elif tier == "day":
        day = plans.get("day")
        if day is _UNKNOWN:
            return None, "—"
        lots = [
            (abs(float(lot.get("fill_qty") or 0)), float(lot.get("entry_price") or 0))
            for lot in (day or {}).get(sym, [])
        ]
        basis = _weighted_basis(lots, owned)
        if sym in plans.get("day_promoting", set()):
            parts.append("moves to Swing at close")
        elif plans.get("day_flat_pt"):
            parts.append(f"flat by {plans['day_flat_pt']}")
        else:
            parts.append(
                f"flat {float(bot_config.DAYTRADE_FORCE_FLAT_MINUTES):g} min before close"
            )
    elif tier == "qhm":
        holds = plans.get("qhm")
        hold = holds.get(sym) if isinstance(holds, dict) else None
        if not isinstance(hold, dict):
            return None, "—"
        from execution import quarterly_hold_manager as qhm_mod

        avg = float(hold.get("avg_entry_price") or 0)
        if abs(float(hold.get("qty_filled") or 0) - owned) <= 1e-6 and avg > 0:
            basis = avg
        trims = plans.get("qhm_trim")
        rung = trims.get(sym) if isinstance(trims, dict) else None
        if isinstance(rung, dict) and avg > 0 and rung.get("gate_date"):
            gate = datetime.strptime(str(rung["gate_date"]), "%Y-%m-%d").date()
            qty = int(owned)
            if gate >= today and rung.get("tier1") == "none" and qty >= 2:
                pct = rung.get("t1_target_pct")
                if isinstance(pct, (int, float)) and pct > 0:
                    trim_qty = min(
                        max(int(qty * qhm_mod._EARNINGS_TRIM_TIER1_FRAC), 1), qty - 1
                    )
                    parts.append(
                        f"trim {trim_qty} at {_money(avg * (1 + pct))} (+{pct:.1%}) "
                        f"before earnings {_fmt_day(gate, today)}"
                    )
            elif gate >= today and rung.get("tier1") == "done" and rung.get("tier2") == "none":
                fired = rung.get("t1_fired_pct")
                if isinstance(fired, (int, float)) and fired > 0:
                    pct = fired * qhm_mod._EARNINGS_TRIM_TIER2_MULT
                    parts.append(
                        f"sell rest at {_money(avg * (1 + pct))} (+{pct:.1%}) "
                        f"before earnings {_fmt_day(gate, today)}"
                    )
        if hold.get("entry_day"):
            start = datetime.strptime(str(hold["entry_day"]), "%Y-%m-%d").date()
            exit_day = start + timedelta(days=int(qhm_mod._MAX_HOLD_CALENDAR_DAYS))
            parts.append(f"max-hold exit {_fmt_day(exit_day, today)}")
    elif tier == "forever_6":
        rec = plans.get("ledger", {}).get(sym) or {}
        claim = normalize_tier_mapping(rec.get("tiers") or {}).get("forever_6") or {}
        cost = float(claim.get("avg_cost") or 0)
        mult = float(bot_config.FOREVER6_TRIM_1_MULT)
        frac = float(bot_config.FOREVER6_TRIM_1_FRAC)
        if cost > 0:
            basis = cost
            trim_qty = int(owned * frac)
            trim_txt = (
                f"trims {trim_qty} at {_money(cost * mult)} ({mult:g}× cost)"
                if trim_qty >= 1 else f"no trim below {math.ceil(1 / frac)} shares"
            )
            parts.append(f"never sells all · {trim_txt}")
        else:
            parts.append("never sells all")
    else:
        if td.get("target"):
            parts.append(_money(float(td["target"])))
    return basis, (" · ".join(parts) if parts else "—")


# ── Today's trades (local tier records; no network on the render path) ───────

_TRADE_EVENT_KINDS = ('"event": "entry"', '"event": "exit"', '"event": "partial_exit"',
                      '"event": "fill"')
_TAIL_CHUNK_BYTES = 1 << 20          # read trade_events.jsonl backwards 1 MiB at a time
_TAIL_MAX_BYTES = 64 << 20           # never read more than this per render
_TS_RE = re.compile(r'"ts": "([^"]+)"')
_EXIT_REASON_LABELS = {
    "protective_stop": "protective stop",
    "protective_stop_partial": "protective stop (part)",
    "take_profit": "target",
    "trend_break": "trend break",
    "time": "time exit",
    "qhm_stop_out": "protective stop",
    "external_close_detected": "closed outside the bot",
    "external_close_unverified": "closed outside the bot",
    "eod_force_flat": "end-of-day flat",
    "stop_breach_cover": "stop breach",
}


def _read_today_trade_events(path: Path, today_pt) -> list[dict]:
    """Today's (PT session date) entry/exit/fill events from trade_events.jsonl, read
    from the end of the file so a large log costs only today's bytes."""
    if not path.exists():
        return []
    events: list[dict] = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        pos = end
        carry = b""
        done = False
        while pos > 0 and not done and end - pos < _TAIL_MAX_BYTES:
            step = min(_TAIL_CHUNK_BYTES, pos)
            pos -= step
            f.seek(pos)
            block = f.read(step) + carry
            lines = block.split(b"\n")
            carry = lines[0] if pos > 0 else b""
            body = lines[1:] if pos > 0 else lines
            for raw in reversed(body):
                line = raw.decode("utf-8", "replace")
                match = _TS_RE.search(line)
                if not match:
                    continue
                try:
                    ts = datetime.fromisoformat(match.group(1))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    continue
                day = ts.astimezone(PT).date()
                if day < today_pt:
                    done = True          # the log is appended in time order
                    break
                if day != today_pt or not any(k in line for k in _TRADE_EVENT_KINDS):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    ev["_ts"] = ts
                    events.append(ev)
    return events  # newest first


def _event_tier(ev: dict) -> str:
    raw = ev.get("tier")
    if raw:
        try:
            return canonical_tier(raw)
        except ValueError:
            return "unattributed"
    src = str(ev.get("data_source") or "")
    if src.startswith("qhm"):
        return "qhm"
    if str(ev.get("trade_mode") or "") == "intraday":
        return "swing"
    return "unattributed"


def _today_trade_rows(events: list[dict]) -> list[dict]:
    """One row per fill the tiers recorded today, newest first."""
    rows = []
    for ev in events:
        kind = ev.get("event")
        tier = _event_tier(ev)
        trend_night = ev.get("strategy") == "trend_night"
        short = str(ev.get("direction") or "").lower() == "short" or "-s-" in str(
            ev.get("trade_id") or ""
        )
        if kind == "fill":
            # trend_night logs each broker fill; its sells are booked once by its
            # own "exit" record (with the night's P&L), so only its buys are rows here.
            if not trend_night or str(ev.get("side") or "").lower() != "buy":
                continue
            side, qty, pnl, reason = "BUY", ev.get("qty"), None, ""
        elif kind == "entry":
            side = "SELL" if short else "BUY"
            qty, pnl, reason = ev.get("size"), None, ""
        else:  # exit / partial_exit
            side = "BUY" if short else "SELL"
            qty = ev.get("size")
            pnl = ev.get("realized_pnl", ev.get("pnl"))
            raw_reason = str(ev.get("exit_reason") or ev.get("reason") or "")
            reason = "next open" if trend_night else _EXIT_REASON_LABELS.get(
                raw_reason, raw_reason.replace("_", " ")
            )
        price = _order_number(ev.get("price"))
        rows.append({
            "time": ev["_ts"].astimezone(PT).strftime("%-I:%M %p PT"),
            "symbol": str(ev.get("symbol") or "?"),
            "tier": tier,
            "side": side,
            "qty": _order_number(qty),
            "price": price if price and price > 0 else None,
            "pnl": _order_number(pnl),
            "reason": reason,
        })
    return rows


def _today_trades_html(rows: list[dict]) -> str:
    if not rows:
        return ('<tr><td colspan="8" style="color:#3a5068;text-align:center;padding:20px">'
                'No fills recorded today</td></tr>')
    out = []
    for r in rows:
        side_col = "#30d158" if r["side"] == "BUY" else "#ff3b30"
        qty = "—" if r["qty"] is None else f"({abs(r['qty']):g})"
        price = "—" if r["price"] is None else _money(r["price"])
        if r["pnl"] is None:
            pnl = "<td>—</td>"
        else:
            pnl = (f'<td style="color:{_col(r["pnl"])};font-weight:700">'
                   f'{"+" if r["pnl"] >= 0 else "-"}${abs(r["pnl"]):.2f}</td>')
        out.append(
            "<tr>"
            f'<td style="color:#b8bdd4;white-space:nowrap">{r["time"]}</td>'
            f'<td style="color:#e2e4ee;font-weight:700">{escape(r["symbol"])}</td>'
            f'<td>{tier_badges([(r["tier"], 0.0)], show_qty=False)}</td>'
            f'<td style="color:{side_col};font-weight:700">{r["side"]}</td>'
            f"<td>{qty}</td><td>{price}</td>{pnl}"
            f'<td style="color:#b8bdd4">{escape(r["reason"]) or "—"}</td>'
            "</tr>"
        )
    return "".join(out)


# ── Today's trades from broker fills (tier / P&L / reason joined from tier records) ─

_JOIN_WINDOW = timedelta(minutes=90)   # a tier may book an exit well after its fill
_FRACTION_RE = re.compile(r"\.(\d+)")


def _parse_broker_time(raw: object) -> datetime:
    """Alpaca timestamps carry 1-9 fractional digits ("…37.37442Z"); Python 3.10's
    fromisoformat accepts only 3 or 6, so normalise to microseconds first."""
    text = str(raw).replace("Z", "+00:00")
    text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    return datetime.fromisoformat(text)


def _event_side(ev: dict) -> str:
    """BUY/SELL the broker executed for one tier event."""
    short = str(ev.get("direction") or "").lower() == "short" or "-s-" in str(
        ev.get("trade_id") or ""
    )
    kind = ev.get("event")
    if kind == "fill":
        return "BUY" if str(ev.get("side") or "").lower() == "buy" else "SELL"
    if kind == "entry":
        return "SELL" if short else "BUY"
    return "BUY" if short else "SELL"


def _local_order_tiers(orders: list[dict], events: list[dict]) -> dict[str, str]:
    """{broker order id: tier} from what the bot already records locally: open
    orders' client ids, tier state files, the Day log and today's tier events."""
    out: dict[str, str] = {}

    def put(oid: object, tier: str) -> None:
        if oid and str(oid) not in out:
            out[str(oid)] = tier

    for order in orders or []:
        tier = _order_tier(order)
        if tier:
            put(order.get("id"), tier)
    for ev in events:
        if ev.get("order_id"):
            put(ev.get("order_id"), _event_tier(ev))
    state_dir = ROOT / "data" / "state"
    breakout = _load_json(state_dir / "swing_breakout.json", {})
    if isinstance(breakout, dict):
        recs = list((breakout.get("positions") or {}).values()) + list(
            breakout.get("closed_lots") or []
        )
        for rec in recs:
            if isinstance(rec, dict):
                for key in ("entry_order_id", "stop_order_id", "close_order_id"):
                    put(rec.get(key), "swing")
    night = _load_json(state_dir / "swing_trend_night.json", {})
    if isinstance(night, dict):
        for rec in (night.get("nights") or {}).values():
            for o in ((rec or {}).get("orders") or {}).values() if isinstance(rec, dict) else []:
                if isinstance(o, dict):
                    put(o.get("order_id"), "swing")
    day_state = _load_json(LOG_DIR / "day_tier_state.json", {})
    if isinstance(day_state, dict):
        for key, rec in day_state.items():
            if str(key).startswith("entry::") and isinstance(rec, dict):
                for k in ("order_id", "stop_order_id", "tp_order_id", "oco_order_id",
                          "stop2_order_id"):
                    put(rec.get(k), "day")
    holds = _load_json(state_dir / "quarterly_holds.json", {})
    if isinstance(holds, dict):
        for rec in holds.values():
            if isinstance(rec, dict):
                for k in ("entry_order_id", "stop_order_id"):
                    put(rec.get(k), "qhm")
    return out


def _single_holding_tiers(ownership_positions: dict, qhm_holds: dict) -> dict[str, str]:
    """{symbol: tier} where exactly one tier holds (or held, for a QHM hold) the symbol."""
    out: dict[str, str] = {}
    for sym, rec in (ownership_positions or {}).items():
        try:
            tiers = normalize_tier_mapping((rec or {}).get("tiers") or {})
            held = [t for t, c in tiers.items()
                    if isinstance(c, dict) and abs(float(c.get("qty") or 0)) > 1e-9]
        except (ValueError, TypeError, AttributeError):
            continue
        if len(held) == 1:
            out[str(sym).upper()] = held[0]
    for sym, rec in (qhm_holds or {}).items():
        if isinstance(rec, dict) and str(sym).upper() not in out and (
            float(rec.get("qty_filled") or 0) > 0 or rec.get("state") == "ACTIVE"
        ):
            out[str(sym).upper()] = "qhm"
    return out


def _broker_trade_rows(
    fills: list[dict],
    events: list[dict],
    order_tiers: dict[str, str],
    holding_tiers: dict[str, str],
    qhm_holds: dict,
) -> list[dict]:
    """One row per broker ORDER filled today (partial fills of one order are
    aggregated: total qty, volume-weighted price, time of the last fill), newest
    first. Tier: local order id → nearest same-symbol/side tier event → the single
    tier holding the symbol → "unattributed". A sale's P&L is qty × (fill − the
    lot's cost), the cost taken from the tier's exit record (price − P&L/size) or,
    for QHM, the hold's own average entry; unknown cost → no P&L."""
    groups: dict[str, dict] = {}
    unreadable = 0
    for fill in fills:
        qty = _order_number(fill.get("qty"))
        price = _order_number(fill.get("price"))
        try:
            ts = _parse_broker_time(fill["transaction_time"])
        except (KeyError, ValueError):
            unreadable += 1
            continue
        if qty is None or price is None or qty <= 0 or price <= 0 or ts.tzinfo is None:
            unreadable += 1
            continue
        oid = str(fill.get("order_id") or fill.get("id") or "")
        g = groups.setdefault(oid, {
            "symbol": str(fill.get("symbol") or "?").upper(),
            "side": "BUY" if str(fill.get("side") or "").lower() == "buy" else "SELL",
            "qty": 0.0, "notional": 0.0, "ts": ts,
        })
        g["qty"] += qty
        g["notional"] += qty * price
        g["ts"] = max(g["ts"], ts)

    if unreadable:
        logger.warning("Today's trades: %d broker fill record(s) unreadable — not shown", unreadable)
    tagged = []
    for ev in events:
        if ev.get("event") not in ("entry", "exit", "partial_exit", "fill"):
            continue
        tagged.append((ev, str(ev.get("symbol") or "").upper(), _event_side(ev), _event_tier(ev)))

    rows = []
    for oid, g in groups.items():
        sym, side, ts = g["symbol"], g["side"], g["ts"]
        vwap = g["notional"] / g["qty"]
        tier = order_tiers.get(oid)
        cands = [
            (ev, ev_tier) for ev, ev_sym, ev_side, ev_tier in tagged
            if ev_sym == sym and ev_side == side and abs(ev["_ts"] - ts) <= _JOIN_WINDOW
            and (tier is None or ev_tier == tier)
        ]
        match = min(
            cands,
            key=lambda c: (c[0].get("event") == "fill", abs(c[0]["_ts"] - ts)),
            default=None,
        )
        hit = match[0] if match else None
        if tier is None:
            tier = match[1] if match else holding_tiers.get(sym, "unattributed")
        reason, cost, short = "", None, False
        if hit is not None and hit.get("event") in ("exit", "partial_exit"):
            short = _event_side(hit) == "BUY"
            if hit.get("strategy") == "trend_night":
                reason = "next open"
            else:
                raw = str(hit.get("exit_reason") or hit.get("reason") or "")
                reason = _EXIT_REASON_LABELS.get(raw, raw.replace("_", " "))
            ev_px = _order_number(hit.get("price"))
            ev_pnl = _order_number(hit.get("realized_pnl", hit.get("pnl")))
            ev_size = _order_number(hit.get("size"))
            if ev_px and ev_pnl is not None and ev_size:
                cost = ev_px + ev_pnl / ev_size if short else ev_px - ev_pnl / ev_size
        closing = (hit is not None and hit.get("event") in ("exit", "partial_exit")) or (
            tier == "qhm" and side == "SELL"
        )
        if cost is None and closing and tier == "qhm" and side == "SELL":
            avg = _order_number(((qhm_holds or {}).get(sym) or {}).get("avg_entry_price"))
            cost = avg if avg and avg > 0 else None
        pnl = None
        if closing and cost is not None:
            pnl = g["qty"] * ((cost - vwap) if short else (vwap - cost))
        rows.append({
            "_ts": ts,
            "time": ts.astimezone(PT).strftime("%-I:%M %p PT"),
            "symbol": sym, "tier": tier, "side": side, "qty": g["qty"],
            "price": vwap, "pnl": pnl, "reason": reason,
        })
    rows.sort(key=lambda r: r["_ts"], reverse=True)
    return rows


def _compute_spy_levels() -> dict:
    """SPY GEX regime/flip (from gex_snapshot.json) + floor-trader pivot S/R from
    the last completed SPY daily bar (Alpaca Data API, T1). Standalone + fail-soft:
    every field defaults to None so the dashboard card always renders. GEX may be
    degraded (weekly expired) — pivots are always computable from the prior day."""
    out: dict = {"spot": None, "regime": None, "flip": None, "pin": None,
                 "P": None, "R1": None, "R2": None, "S1": None, "S2": None}
    try:
        gx = _load_gex_snapshot().get("symbols", {}).get("SPY", {})
        out["regime"] = gx.get("label")
        out["flip"]   = gx.get("flip_strike")
        out["pin"]    = gx.get("pin")   # actionable gamma-concentration pin (2026-07-20)
        out["spot"]   = gx.get("spot")
    except Exception as e:
        logger.warning(f"SPY levels: GEX snapshot read failed: {e}")
    try:
        import urllib.request as _ur
        import ssl as _ssl
        try:
            import certifi as _cert
            _ctx = _ssl.create_default_context(cafile=_cert.where())
        except ImportError:
            _ctx = _ssl.create_default_context()
        _k = os.getenv("ALPACA_API_KEY")
        _s = os.getenv("ALPACA_SECRET_KEY")
        if _k and _s:
            # start ~12 days back so the last COMPLETED daily bar is always present
            # (Alpaca returns bars=null when no explicit start window is given).
            _start = (datetime.now(ZoneInfo("UTC")) - timedelta(days=12)).strftime("%Y-%m-%d")
            url = ("https://data.alpaca.markets/v2/stocks/SPY/bars"
                   f"?timeframe=1Day&start={_start}&limit=10&feed=iex&adjustment=raw")
            req = _ur.Request(url, headers={"APCA-API-KEY-ID": _k,
                                            "APCA-API-SECRET-KEY": _s})
            with _ur.urlopen(req, context=_ctx, timeout=20) as r:
                bars = json.load(r).get("bars") or []
            if bars:
                b = bars[-1]  # most recent completed daily bar
                h, low, c = float(b["h"]), float(b["l"]), float(b["c"])
                p = (h + low + c) / 3.0
                out["P"]  = round(p, 2)
                out["R1"] = round(2 * p - low, 2)
                out["S1"] = round(2 * p - h, 2)
                out["R2"] = round(p + (h - low), 2)
                out["S2"] = round(p - (h - low), 2)
                if not out["spot"]:
                    out["spot"] = round(c, 2)
    except Exception as e:
        logger.warning(f"SPY levels: pivot fetch failed: {e}")
    return out


_FILL_PAGE_SIZE = 100   # Alpaca account-activities maximum page size
_FILL_MAX_PAGES = 20    # bounds one render's paging (2,000 fills)


def _fetch_today_fills(get) -> list[dict]:
    """Today's (PT session date) FILL activities, newest first, via the caller's
    read-only GET. Pages with page_token until a short page; raises on anything
    unexpected so the caller can fall back to the tiers' own records."""
    start = datetime.now(PT).replace(hour=0, minute=0, second=0, microsecond=0)
    after = start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    fills: list[dict] = []
    token = None
    for _page in range(_FILL_MAX_PAGES):
        path = (f"/v2/account/activities/FILL?after={after}&direction=desc"
                f"&page_size={_FILL_PAGE_SIZE}")
        if token:
            path += f"&page_token={token}"
        page = get(path)
        if not isinstance(page, list):
            raise ValueError(f"fills page is {type(page).__name__}, not a list")
        fills.extend(item for item in page if isinstance(item, dict))
        if len(page) < _FILL_PAGE_SIZE:
            return fills
        token = page[-1].get("id") if isinstance(page[-1], dict) else None
        if not token:
            raise ValueError("fills page has no id to continue from")
    raise ValueError("fills paging did not finish")


def _load_alpaca():
    """
    Fetch live account, positions, and open orders via direct Alpaca REST.
    Uses urllib.request — no alpaca-py SDK dependency, no MACL risk.
    Works from any Python version/process that has the keys in os.environ.
    """
    # Belt-and-suspenders: reload .env inside the function so keys are
    # guaranteed available regardless of import order or calling process.
    # override=True is intentional — if the shell exported empty strings for
    # these vars (e.g. via an unset export), this forces the .env values in.
    try:
        from dotenv import load_dotenv as _ld
        _ld(Path(__file__).parent / ".env", override=True)
    except Exception as _dotenv2_e:
        logger.debug(
            "_load_alpaca: dotenv reload failed (keys may still be in env): %s",
            _dotenv2_e,
        )
    import urllib.request as _ur
    import ssl as _ssl
    _api_key    = os.getenv("ALPACA_API_KEY", "").strip()
    _secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not _api_key or not _secret_key:
        return {
            "error": (
                "API keys not in environment. "
                "Restart the bot — main.py loads them at startup."
            ),
            "equity": 0, "cash": 0, "buying_power": 0, "last_equity": 0,
            "status": "unknown", "shorting": False,
            "positions": [], "orders":[],
            "orders_available": False,
            "fills": [], "fills_available": False,
        }
    try:
        import certifi as _cert
        _ctx = _ssl.create_default_context(cafile=_cert.where())
    except ImportError:
        _ctx = _ssl.create_default_context()

    _BASE    = "https://paper-api.alpaca.markets"
    _HEADERS = {"APCA-API-KEY-ID": _api_key, "APCA-API-SECRET-KEY": _secret_key}

    def _get(path):
        req = _ur.Request(f"{_BASE}{path}", headers=_HEADERS)
        with _ur.urlopen(req, context=_ctx, timeout=20) as r:
            return json.load(r)

    try:
        acct  = _get("/v2/account")
        raw_p = _get("/v2/positions")
        raw_o = _get(
            f"/v2/orders?status=open&limit={_ALPACA_OPEN_ORDER_PAGE_SIZE}&nested=true"
        )

        def _flatten_orders(items):
            flattened = []
            seen = set()
            pending = (
                [(order, None) for order in items]
                if isinstance(items, list) else []
            )
            while pending:
                order, inherited_coid = pending.pop(0)
                if not isinstance(order, dict):
                    continue
                order_id = str(order.get("id") or "")
                if not order_id or order_id in seen:
                    continue
                seen.add(order_id)
                display_order = dict(order)
                display_order["owner_client_order_id"] = inherited_coid
                display_order["owner_order_class"] = (
                    order.get("order_class") if inherited_coid else None
                )
                display_order["owner_status"] = (
                    order.get("status") if inherited_coid else None
                )
                flattened.append(display_order)
                own_coid = order.get("client_order_id")
                try:
                    from execution.ownership_guard import tier_of_coid
                    owner_coid = own_coid if tier_of_coid(own_coid) else inherited_coid
                except (ImportError, TypeError, ValueError):
                    owner_coid = inherited_coid
                legs = order.get("legs")
                if isinstance(legs, list):
                    parent_class = order.get("order_class")
                    for leg in legs:
                        if isinstance(leg, dict):
                            leg = {
                                **leg,
                                "_owner_order_class": parent_class,
                                "_owner_status": order.get("status"),
                            }
                        pending.append((leg, owner_coid))
            return flattened

        flat_o = _flatten_orders(raw_o)
        # A filled bracket parent drops out of status=open, and Alpaca then returns only its NEW
        # take-profit leg; the HELD stop leg is invisible (2026-11-04 MSFT Day: stop $548.51 held,
        # dashboard showed MISSING). Read recent closed brackets once and add the HELD stop legs of
        # FILLED parents for symbols still held. Isolated: a failure leaves the open list as it was.
        try:
            _held_syms = {str(p.get("symbol") or "").upper() for p in (raw_p or []) if isinstance(p, dict)}
            _seen_ids = {str(o.get("id") or "") for o in flat_o}
            _closed = _get(
                f"/v2/orders?status=closed&limit={_ALPACA_OPEN_ORDER_PAGE_SIZE}&nested=true&direction=desc"
            )
            _extra = [
                o for o in (_closed if isinstance(_closed, list) else [])
                if isinstance(o, dict)
                and str(o.get("order_class") or "").lower() == "bracket"
                and str(o.get("status") or "").lower() == "filled"
                and str(o.get("symbol") or "").upper() in _held_syms
                and any(
                    isinstance(leg, dict)
                    and str(leg.get("status") or "").lower() == "held"
                    and str(leg.get("id") or "") not in _seen_ids
                    for leg in (o.get("legs") or [])
                )
            ]
            if _extra:
                # Only the HELD legs are added (never the filled parent or legs already listed),
                # so nothing else on the page sees a closed order as open.
                flat_o = flat_o + [
                    o for o in _flatten_orders(_extra)
                    if str(o.get("id") or "") not in _seen_ids
                    and str(o.get("status") or "").lower() == "held"
                    and o.get("owner_client_order_id")
                ]
        except Exception as _held_e:
            logger.warning("held bracket legs not read — open orders only: %s", _held_e)
        # Today's fills: read-only, isolated — a failure here only switches the
        # Today's Trades table to the tiers' own records.
        try:
            fills = _fetch_today_fills(_get)
            fills_available = True
        except Exception as _fills_e:
            logger.warning("Alpaca fills fetch failed — tier records used: %s", _fills_e)
            fills, fills_available = [], False
        return {
            "equity":       float(acct.get("equity", 0) or 0),
            "last_equity":  float(acct.get("last_equity", 0) or 0),
            "cash":         float(acct.get("cash", 0) or 0),
            "buying_power": float(acct.get("buying_power", 0) or 0),
            "pdt_count":    int(acct.get("daytrade_count", 0) or 0),
            "status":       acct.get("status", "unknown"),
            "shorting":     acct.get("shorting_enabled", False),
            "positions": [
                {
                    "symbol":                 p["symbol"],
                    "qty":                    float(p["qty"]),
                    "side":                   p["side"],
                    "market_value":           float(p.get("market_value", 0) or 0),
                    "unrealized_pl":          float(p.get("unrealized_pl", 0) or 0),
                    "unrealized_intraday_pl": float(
                        p.get("unrealized_intraday_pl", 0) or 0),
                    "unrealized_plpc":        float(p.get("unrealized_plpc", 0) or 0),
                    "current_price":          float(p.get("current_price", 0) or 0),
                    "avg_entry_price":        float(p.get("avg_entry_price", 0) or 0),
                }
                for p in raw_p
            ],
            "orders":[
                {
                    "id":           o.get("id", ""),
                    "symbol":       o.get("symbol", "?"),
                    "side":         o.get("side", "?"),
                    "qty":          o.get("qty", "?"),
                    "filled_qty":   o.get("filled_qty"),
                    "type":         o.get("type") or o.get("order_type", "unknown"),
                    "status":       o.get("status", "?"),
                    "stop_price":   o.get("stop_price"),
                    "limit_price":  o.get("limit_price"),
                    "order_class":  o.get("order_class", ""),
                    "client_order_id": o.get("client_order_id", ""),
                    "owner_client_order_id": o.get("owner_client_order_id"),
                    "owner_order_class": (
                        o.get("_owner_order_class") or o.get("owner_order_class")
                    ),
                    "owner_status": o.get("_owner_status") or o.get("owner_status"),
                    "submitted_at": o.get("submitted_at", ""),
                }
                for o in flat_o
            ],
            "orders_available": (
                isinstance(raw_o, list)
                and len(raw_o) < _ALPACA_OPEN_ORDER_PAGE_SIZE
            ),
            "fills": fills,
            "fills_available": fills_available,
            "error": None,
        }
    except Exception as e:
        logger.error(f"Alpaca API load failed — dashboard will show zeros: {e}")
        return {
            "error": str(e), "equity": 0, "last_equity": 0,
            "cash": 0, "buying_power": 0,
            "status": "unknown", "shorting": False,
            "positions": [], "orders": [], "orders_available": False,
            "fills": [], "fills_available": False,
        }


# ── HTML builder ─────────────────────────────────────────────────────────────

def _col(val, pos_col="#30d158", neg_col="#ff3b30", zero_col="#636680"):
    if val is None:
        return zero_col
    return pos_col if val > 0 else (neg_col if val < 0 else zero_col)


def _pnl_str(val):
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:,.2f}"


def _market_status_info(now_et: datetime) -> dict:
    from datetime import timedelta
    mins = now_et.hour * 60 + now_et.minute
    is_open = (now_et.weekday() < 5) and (9 * 60 + 30 <= mins < 16 * 60)

    if not is_open:
        d = 1
        while True:
            cand = now_et + timedelta(days=d)
            if cand.weekday() < 5:
                next_et = cand.replace(hour=9, minute=30, second=0, microsecond=0)
                break
            d += 1
        next_pt   = next_et.astimezone(PT)
        hr        = str(int(next_pt.strftime("%I")))
        ampm      = next_pt.strftime("%p").lower()
        open_time = f"{hr}:{next_pt.strftime('%M')}{ampm} PT"

        delta     = next_et - now_et
        total_m   = int(delta.total_seconds() // 60)
        h, m      = divmod(total_m, 60)
        countdown = f"{h}h {m}m" if h else f"{m}m"
    else:
        open_time = ""
        countdown = ""
        close_et  = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        delta_c   = close_et - now_et
        total_m   = int(delta_c.total_seconds() // 60)
        h, m      = divmod(total_m, 60)
        countdown = f"closes in {h}h {m}m" if h else f"closes in {m}m"

    return {"is_open": is_open, "open_time": open_time, "countdown": countdown}


def _scan_countdown(bot_status: dict, is_market_open: bool) -> str:
    try:
        ts_str = bot_status.get("timestamp", "")
        if not ts_str:
            return ""
        last = datetime.fromisoformat(ts_str).astimezone(ET)
        now  = datetime.now(ET)
        elapsed_s = int((now - last).total_seconds())
        elapsed_m = elapsed_s // 60
        interval  = 5 if is_market_open else 10
        remaining = max(0, interval * 60 - elapsed_s)
        rem_m     = remaining // 60
        rem_s     = remaining % 60
        ago_str   = f"{elapsed_m}m ago" if elapsed_m > 0 else "just now"
        nxt_str   = f"{rem_m}m {rem_s:02d}s" if rem_m < interval else f"{rem_m}m"
        return f"last scan {ago_str} · next in {nxt_str}"
    except Exception as _scd_e:
        logger.debug("_scan_countdown: timestamp parse failed — %s", _scd_e)
        return ""


def _build_html(alpaca, trade_log, hybrid, eod, bot_status=None, market_news=None):
    if bot_status is None:
        bot_status = {}
    if market_news is None:
        market_news = {"items":[]}

    now_et   = datetime.now(ET)
    # Header time is now a LIVE in-browser clock (LIVE_CLOCK_HTML) — no static
    # server-rendered timestamp string needed (Rafael rule 2026-07-06).
    _mkt     = _market_status_info(now_et)
    _scan_cd = _scan_countdown(bot_status, _mkt["is_open"])

    equity   = alpaca.get("equity", 0)
    bp       = alpaca.get("buying_power", 0)

    open_pos = alpaca.get("positions",[])
    orders   = alpaca.get("orders",[])
    edge_snapshot = _load_edge_snapshot()
    ownership_positions = _load_ownership_positions()
    kill_snapshot = _load_kill_switch_snapshot()

    # All-time P&L — Alpaca authoritative via compute_lifetime_stats(equity=equity).
    # Pass equity already fetched above so we skip the second Alpaca API call.
    # Per P&L sourcing rule: Alpaca equity is the sole source of truth for all-time P&L.
    try:
        # equity-based: pass already-fetched Alpaca equity (avoids second API call).
        # Falls back to EOD sum when equity=0 (API error) via skip_fetch guard.
        _lt = compute_lifetime_stats(
            equity=equity if equity > 0 else None,
            skip_fetch=bool(alpaca.get("error")) or equity <= 0,
        )
    except Exception as _lt_e:
        logger.warning(
            "_build_html: compute_lifetime_stats failed"
            " — all-time P&L shows equity-based estimate: %s",
            _lt_e,
        )
        _lt = {}
    # all_pnl = TOTAL account P&L (equity - net_deposits), computed live by
    # compute_lifetime_stats (metrics._net_deposits). Always current from live equity.
    # Last-resort fallback (only if compute_lifetime_stats raised → _lt={}) uses the
    # SAME net_deposits basis — never the old equity-2500 hardcode.
    all_pnl    = _lt.get("total_pnl", round(float(equity) - _net_deposits(), 2))
    _edge_overall = edge_snapshot.get("overall", {})
    all_wr     = _edge_overall.get("win_rate")
    all_trades = _edge_overall.get("completed_trades")
    all_pf     = _edge_overall.get("profit_factor")
    all_realized = _edge_overall.get("realized_pnl")

    # NOTE: this module NO LONGER writes lifetime_pnl_cache.json (board 4-0 + Gro + GAI,
    # 2026-07-10). reporting.pnl_ledger.heal_history is now the SOLE authoritative writer
    # (TOTAL semantic + net_deposits + realized/unrealized components), eliminating the
    # dual-writer 'which cron won' ~$13 swing. The dashboard READS the live authoritative
    # total (equity - net_deposits) via compute_lifetime_stats; monthly_review reads the
    # cache. One writer, one meaning.

    # Unrealized P&L: from Alpaca's live open position data.
    # Filtered to symbols the bot has open so re-entries aren't double-counted.
    _open_syms_in_log = {t.get("symbol") for t in trade_log.get("open", [])}
    _open_unrealized  = sum(
        p.get("unrealized_intraday_pl", 0) for p in open_pos
        if p.get("symbol") in _open_syms_in_log
    )
    unrealized_pnl     = _open_unrealized  # still used: realized_pnl calc + kill switch gauge

    # Today P&L — Alpaca's authoritative account-level number.
    # equity - last_equity captures ALL realized gains (manual closes on Alpaca,
    # GTC fills, bot closes, partial exits) plus current unrealized P&L.
    # Never goes stale when a position closes mid-session — the previous approach
    # (eod["pnl_today"] + unrealized) zeroed out realized gains on any close
    # because the EOD file is only refreshed at AH, not intraday.
    alpaca_daily_pnl = equity - alpaca.get("last_equity", equity)
    today_pnl        = alpaca_daily_pnl

    # Cache today_pnl for scan_results.html bot health bar.
    # risk.daily_pnl resets to 0 on each bot restart; this Alpaca-authoritative
    # value (equity − last_equity) is the single source of truth for both pages.
    try:
        _pnl_cache_path = LOG_DIR / "daily_pnl_cache.json"
        _pnl_tmp = _pnl_cache_path.with_suffix(".tmp")
        _pnl_tmp.write_text(json.dumps({"daily_pnl": round(float(today_pnl), 2),
                                         "ts": datetime.now(ET).isoformat()}))
        _pnl_tmp.replace(_pnl_cache_path)
    except Exception as _cache_e:
        logger.warning(
            "_build_html: daily_pnl_cache write failed"
            " — scan_results.html health bar shows stale P&L: %s",
            _cache_e,
        )

    # Realized = Alpaca daily total minus current floating unrealized.
    # Correctly accounts for manual closes and any close that bypassed record_exit().
    realized_pnl     = round(today_pnl - unrealized_pnl, 2)
    realized_pnl_col = _col(realized_pnl)

    # SPY hybrid engine state
    spy_event   = hybrid.get("event_type", "")
    spy_scans   = hybrid.get("scans_left", 0)
    spy_mag     = hybrid.get("magnitude", 0.0)
    spy_active  = hybrid.get("active", False)
    spy_colors  = {
        "EXTREME":                  "#ff3b5c",
        "BROAD_GEO_CONFLICT":       "#ff6b35",
        "BROAD_GEO_ENERGY":         "#ff6b35",
        "BROAD_MACRO_SYSTEMIC":     "#ff3b5c",
        "BROAD_MACRO_CREDIT":       "#ff6b35",
        "BROAD_MACRO_FX":           "#ffd600",
        "BROAD_MACRO_MONETARY":     "#ffd600",
        "BROAD_GLOBAL_ASIA":        "#ffd600",
        "BROAD_GLOBAL_EU":          "#ffd600",
        "BROAD_TECHNICAL":          "#ffd600",
        "SECTOR":                   "#00e5ff",
    }
    spy_col  = spy_colors.get(spy_event, "#3a5068")
    spy_label = spy_event if spy_event else "CLEAR"

    # News + MRI state from bot_status.json
    _ns               = bot_status.get("news_summary", {})
    macro_risk_active = _ns.get("macro_risk_active", _ns.get("war_state_active", False))
    macro_event       = _ns.get("active_event_type", "")
    news_mult         = _ns.get("news_size_mult", 1.0)

    # MRI state (T1-B/T1-C)
    _mri              = _ns.get("mri", {})
    mri_score         = _mri.get("score", 0)
    mri_level         = _mri.get("level", "NORMAL")
    mri_size_floor    = _mri.get("size_floor", 1.0)

    _mri_colors = {
        "NORMAL":   "#636680",
        "ELEVATED": "#ffd600",
        "STRESSED": "#ff6b35",
        "HIGH":     "#ff3b5c",
        "CRITICAL": "#ff0040",
    }
    mri_col = _mri_colors.get(mri_level, "#3a5068")

    # P0-REGIME fallback: if hybrid_state.json was stale/absent (bot restart gap),
    # use spy_event_type from bot_status.json written by the after-hours path.
    if not spy_event:
        _bs_spy = bot_status.get("spy_event_type", "")
        if _bs_spy:
            spy_event  = _bs_spy
            spy_label  = _bs_spy
            spy_col    = spy_colors.get(_bs_spy, "#ff6b35")
            spy_active = True

    if not spy_event and macro_risk_active and macro_event:
        spy_label  = macro_event
        spy_col    = "#ff6b35"
        spy_active = True

    if spy_active and spy_event:
        spy_sub = f"Scans left: {spy_scans} · {spy_mag:+.2f}%"
        if macro_risk_active and macro_event:
            spy_sub += f" · ⚠ {macro_event}"
    elif spy_active and macro_risk_active:
        spy_sub = f"Size mult: {news_mult:.2f}x · Macro gate active"
    else:
        spy_sub = "No active SPY event"

    if mri_score > 0:
        spy_sub += f" · MRI {mri_score}/100"

    # SPY GEX & Levels card (Rafael 2026-07-12) — top row, directly right of the
    # SPY Regime/MRI tile. GEX regime/flip from the snapshot (shows "computing"
    # when the weekly is expired/degraded) + floor-trader pivot S/R (always live).
    _lv = _compute_spy_levels()
    _lv_regime = _lv.get("regime") or "—"
    _lv_rcol = {"POSITIVE": "#30d158", "NEAR-FLIP": "#ffd60a",
                "NEGATIVE": "#ff3b30"}.get(_lv_regime, "#636680")

    def _fx(v):
        return f"${v:,.2f}" if isinstance(v, (int, float)) else "—"

    _lv_spot = _fx(_lv.get("spot"))
    # GEX PIN (gamma-concentration centroid + wall + ATM-weighted confidence) — the actionable
    # daily level, replaces the deprecated null-prone flip in this title card (Rafael 2026-07-20).
    # Confidence colors the % so a low-confidence pin reads "don't trust me"; a 0-confidence /
    # absent pin (ATM censored) shows "computing" — never a number the eye can anchor on.
    _lp = _lv.get("pin") or {}
    _lp_c = _lp.get("centroid")
    _lp_w = _lp.get("wall")
    _lp_conf = _lp.get("confidence")
    if _lp_c is not None and (_lp_conf or 0) > 0:
        _lp_col = ("#00e5ff" if (_lp_conf or 0) >= 0.6 else "#ffd60a" if (_lp_conf or 0) >= 0.3 else "#ff6b6b")
        _lp_wall_s = (f' · wall ${_lp_w:,.0f}' if _lp_w is not None else '')
        _pin_disp = (f'pin ${_lp_c:,.0f}<span style="color:{_lp_col};font-weight:700"> '
                     f'{_lp_conf:.0%}</span>{_lp_wall_s}')
    else:
        _pin_disp = 'pin computing'
    spy_levels_card = (
        '  <div class="kpi" style="grid-column:span 2">\n'
        '    <div class="kpi-lbl">SPY GEX &amp; Levels</div>\n'
        '    <div class="kpi-val" style="display:flex;align-items:baseline;'
        'gap:10px;flex-wrap:wrap;font-size:18px">\n'
        f'      <span>{_lv_spot}</span>\n'
        f'      <span class="spy-badge" style="color:{_lv_rcol};'
        f'border-color:{_lv_rcol}">GEX {_lv_regime}</span>\n'
        f'      <span style="font-size:11px;color:var(--dim)" title="gamma-concentration pin '
        f'(front expiry) — NOT a zero-gamma flip, NOT a trigger; confidence is ATM-weighted, '
        f'OI is T+1-stale">{_pin_disp}</span>\n'
        '    </div>\n'
        '    <div class="kpi-sub" style="line-height:1.7">\n'
        f'      <b style="color:#ff453a">R2</b> {_fx(_lv.get("R2"))} · '
        f'<b style="color:#ff9f0a">R1</b> {_fx(_lv.get("R1"))} · '
        f'<b style="color:#8e8e93">P</b> {_fx(_lv.get("P"))} · '
        f'<b style="color:#30d158">S1</b> {_fx(_lv.get("S1"))} · '
        f'<b style="color:#0a84ff">S2</b> {_fx(_lv.get("S2"))}\n'
        '    </div>\n'
        '  </div>'
    )

    # The risk engine owns this calculation.  The dashboard only renders its
    # validated current-session snapshot; it never imports a profile default or
    # recreates tier attribution in this separate writer process.
    ks_available = bool(kill_snapshot)
    if ks_available:
        max_loss_pct = float(kill_snapshot["limit_pct"])
        ks_limit = float(kill_snapshot["limit_usd"])
        ks_trading_pnl = float(kill_snapshot["trading_pnl"])
        ks_used_pct = float(kill_snapshot["consumed_pct"])
        ks_fill_pct = min(ks_used_pct, 100.0)
        ks_asof = kill_snapshot["generated_at"].astimezone(PT).strftime("%-I:%M:%S %p PT")
        ks_col = (
            "#ff3b30" if kill_snapshot["killed"] or ks_used_pct >= 80
            else ("#ffd60a" if ks_used_pct >= 50 else "#30d158")
        )
        ks_prefix = "Daily brake TRIGGERED — " if kill_snapshot["killed"] else ""
        ks_title = (
            f"{ks_prefix}Day + Swing loss vs {max_loss_pct:.0%} daily brake "
            f"(QHM + F6 held shares excluded)"
        )
        if kill_snapshot["stale_trigger_snapshot"]:
            ks_value = (
                f"Trigger-time loss ${ks_trading_pnl:+,.2f} of ${ks_limit:,.2f} · "
                f"{ks_used_pct:.1f}% used · {ks_asof} · current loss unavailable"
            )
        else:
            ks_value = (
                f"${ks_trading_pnl:+,.2f} of ${ks_limit:,.2f} · "
                f"{ks_used_pct:.1f}% used · {ks_asof}"
            )
    else:
        max_loss_pct = 0.0
        ks_limit = 0.0
        ks_trading_pnl = 0.0
        ks_used_pct = 0.0
        ks_fill_pct = 0.0
        ks_col = "#ff9f0a"
        ks_title = "Daily brake status unavailable"
        ks_value = "Risk snapshot missing, stale, inexact, or malformed"

    # Build open positions rows
    pos_rows = ""
    protection_issues = 0
    orders_available = alpaca.get("orders_available") is True
    _open_tier_symbols: dict[str, set[str]] = {k: set() for k in TIER_LABELS}
    _open_tier_qty: dict[str, float] = {k: 0.0 for k in TIER_LABELS}

    def _exact_position_tiers(symbol: str, net_qty: float) -> list[tuple[str, float]]:
        return _canonical_position_tiers(ownership_positions.get(symbol), net_qty)
    # Overnight-held breakeven soft-exit transparency (Rafael 2026-07-13): the visible GTC
    # stop is the catastrophe backstop, but an OVERNIGHT-HELD non-QHM position actually exits
    # at ~entry−0.5×ATR (the "overnight breakeven buffer" in exit_logic) once it breaches for
    # 9 scans. Surface that real level + live breach count so the stop is never a surprise.
    _qhm_syms: set = set()
    _qh: dict = {}   # full quarterly_holds record — kept in scope so the position rows can read
                     # each QHM hold's real stop_price (below), not just the symbol set.
    try:
        _qh = _load_json(ROOT / "data" / "state" / "quarterly_holds.json", {})
        if not isinstance(_qh, dict):
            _qh = {}
        _qhm_syms = set(_qh.keys())
    except Exception:
        _qhm_syms = set()
        _qh = {}

    try:
        _plans = _load_tier_plans(ownership_positions, _qh)
    except Exception as _plans_e:
        logger.warning("_build_html: tier plans unavailable: %s", _plans_e)
        _plans = {}
    _today_et = datetime.now(ET).date()

    if open_pos:
        for p in open_pos:
            sym   = p["symbol"]
            side  = p["side"].upper()
            price = p["current_price"]

            entry_price = p["avg_entry_price"]
            tl_open = {t["symbol"]: t for t in trade_log.get("open",[])} if isinstance(trade_log.get("open"), list) else {}
            td = tl_open.get(sym, {})
            _tier_rows = _exact_position_tiers(sym, float(p["qty"]))
            if not _tier_rows:
                _tier_rows = [("unattributed", float(p["qty"]))]
            for _tier, _tier_qty in _tier_rows:
                _open_tier_symbols.setdefault(_tier, set()).add(sym)
                _open_tier_qty[_tier] = _open_tier_qty.get(_tier, 0.0) + abs(_tier_qty)
            _protection = _position_protection(
                p, _tier_rows, orders, orders_available=orders_available
            )
            _facts_by_tier = {fact["tier"]: fact for fact in _protection}
            for fact in _protection:
                _status = fact["status"]
                _status_col = {
                    "PROTECTED": "#30d158",
                    "PARTIAL": "#ffd60a",
                    "MISSING": "#ff3b30",
                    "OVER-COVERED": "#ff9f0a",
                    "BREACHED": "#ff3b30",
                    "UNAVAILABLE": "#ff9f0a",
                    "UNATTRIBUTED": "#ff9f0a",
                }[_status]
                _stop_text = " + ".join(
                    f"${stop_px:.2f} ({stop_qty:g})"
                    for stop_px, stop_qty in fact["stops"]
                ) or _status
                _coverage = f'{fact["stop_qty"]:g}/{fact["owned_qty"]:g} protected'
                _distance = fact["distance_pct"]
                _distance_text = "—" if _distance is None else f"{_distance:.1f}%"
                _targets = " + ".join(
                    f"${limit_px:.2f} ({limit_qty:g})"
                    for limit_px, limit_qty in fact["targets"]
                )
                if _status == "MISSING":
                    _action = "Place broker stop now"
                elif _status == "PARTIAL":
                    _action = f'Protect remaining ({fact["owned_qty"] - fact["stop_qty"]:g})'
                elif _status == "OVER-COVERED":
                    _action = "Resolve excess stop coverage"
                elif _status == "BREACHED":
                    _action = "Stop breached · verify broker fill now"
                elif _status in {"UNAVAILABLE", "UNATTRIBUTED"}:
                    _action = "Verify ownership + broker orders"
                elif _targets:
                    _action = f"Profit exit {_targets}"
                else:
                    _action = "Stop active · no resting target"
                if _status != "PROTECTED":
                    protection_issues += 1
                # Protection stays in the Open Positions STOP cell: the stop(s) in the
                # status colour; anything other than PROTECTED also shows its status
                # and the next action on a second line.
                _stop_title = escape(f"{_status} · {_coverage} · distance {_distance_text} · {_action}")
                _stop_note = (
                    "" if _status == "PROTECTED" or _stop_text == _status
                    else f'<br><span style="font-size:10px">{_status} · {_coverage}</span>'
                )
                _action_note = (
                    "" if _status == "PROTECTED"
                    else f'<br><span style="font-size:10px">{escape(_action)}</span>'
                )
                fact["stop_html"] = (
                    f'<span style="color:{_status_col};white-space:nowrap" title="{_stop_title}">'
                    f'{_stop_text}{_stop_note}{_action_note}</span>'
                )
            # Overnight-held soft-exit (~entry−0.5×ATR) + live breach count, non-QHM only.
            # be_mult is dynamic [0.25,0.65] in exit_logic; 0.5 is the representative midpoint
            # (RIVN's actual), labelled "~". Exact per-scan threshold persist = queued follow-up.
            # Shown on the Swing / unattributed slice only: trade_log "open" is the legacy
            # intraday (Swing-tier) tracker.
            _soft_html = ""
            _atr_v = td.get("atr_value")
            if td.get("overnight") and sym not in _qhm_syms and _atr_v and td.get("entry_price"):
                _dir_td = str(td.get("direction", "long")).lower()
                _soft = (entry_price - 0.5 * _atr_v) if _dir_td != "short" else (entry_price + 0.5 * _atr_v)
                _brc = int(td.get("breakeven_breach_count", 0) or 0)
                _brc_col = "#ff3b30" if _brc >= 6 else "#ff9f0a" if _brc >= 1 else "#8a94ae"
                _soft_html = (f'<br><span style="font-size:10px;color:{_brc_col}" '
                              f'title="Overnight breakeven buffer: exits ~entry−0.5×ATR after a 9-scan breach. '
                              f'The $ stop above is only the catastrophe backstop.">soft ~${_soft:.2f} · {_brc}/9</span>')
            # QHM holds have NO 12-pt confluence score (conviction picks) → "n/a", not "—".
            _qhm_na = ('<span title="Quarterly holds are conviction picks, not scored '
                       'by the 12-point confluence system.">n/a</span>')
            # One row per (symbol, tier) slice: that tier's quantity, its own entry basis,
            # its slice P&L, its broker stop(s) and its exit plan. Any per-tier lookup error
            # renders "—" for that cell only; the page always writes.
            for _tier, _tier_qty in _tier_rows:
                _slice_qty = abs(float(_tier_qty))
                _slice_short = float(_tier_qty) < 0 or (float(_tier_qty) == 0 and side == "SHORT")
                _slice_side = "SHORT" if _slice_short else "LONG"
                _fact = _facts_by_tier.get(_tier)
                try:
                    _basis, target_str = _tier_slice_plan(
                        _tier, sym, _slice_qty, _plans, td, _fact, _today_et
                    )
                except Exception as _plan_e:
                    logger.warning(
                        "_build_html: %s %s exit plan unavailable: %s", sym, _tier, _plan_e
                    )
                    _basis, target_str = None, "—"
                if _basis is not None:
                    _basis_html = f"${_basis:.2f}"
                    _px_basis = _basis
                else:
                    _basis_html = (
                        f'${entry_price:.2f} <span style="font-size:10px;color:#8a94ae" '
                        'title="This tier\'s own entry price is not in its state file; '
                        'showing the broker average for the whole symbol.">broker avg</span>'
                    )
                    _px_basis = entry_price
                try:
                    _slice_pnl = _slice_qty * (
                        (_px_basis - price) if _slice_short else (price - _px_basis)
                    )
                    _slice_pnl_html = (
                        f'<td style="color:{_col(_slice_pnl)};font-weight:700">'
                        f'{"+" if _slice_pnl >= 0 else "-"}${abs(_slice_pnl):.2f}</td>'
                    )
                except (TypeError, ValueError):
                    _slice_pnl_html = "<td>—</td>"
                _row_stop = (_fact or {}).get("stop_html", "—")
                if _tier == "swing" and isinstance(_plans.get("breakout"), dict) and _plans["breakout"].get(sym):
                    try:
                        _lvl = _swing_trend_level(sym, _today_et)
                    except Exception:
                        _lvl = None
                    _row_stop += (
                        f'<br><span style="font-size:10px;color:#ff9f0a" title="Sells near the close when the price '
                        f'is below the lowest low of the last 20 completed sessions.">trend exit: price near close &lt; '
                        f'{"$" + format(_lvl, ".2f") if _lvl else "20-session low"}</span>'
                    )
                _legacy_row = _tier in ("swing", "unattributed")
                if _tier == "qhm":
                    score_str = _qhm_na
                elif _legacy_row and td.get("score"):
                    score_str = f"{td['score']}/12"
                else:
                    score_str = "—"
                overnight = "🌙" if (_legacy_row and td.get("overnight")) else ""
                _row_soft = _soft_html if _legacy_row else ""
                pos_rows += f"""
            <tr>
              <td style="color:#e2e4ee;font-weight:700">{sym} {overnight}</td>
              <td>{tier_badges([(_tier, 0.0)], show_qty=False)}</td>
              <td style="color:{'#30d158' if _slice_side=='LONG' else '#ff3b30'}">{_slice_side}</td>
              <td>{_slice_qty:g}</td>
              <td>${price:.2f}</td>
              <td style="color:#b8bdd4">{_basis_html}</td>
              {_slice_pnl_html}
              <td>{_row_stop}{_row_soft}</td>
              <td style="color:#30d158">{target_str}</td>
              <td style="color:#b8bdd4">{score_str}</td>
            </tr>"""
    else:
        pos_rows = '<tr><td colspan="10" style="color:#3a5068;text-align:center;padding:20px">No open positions</td></tr>'

    protection_badge = (
        "" if protection_issues == 0
        else f' · <b style="color:#ff3b30">{protection_issues} issue{"s" if protection_issues != 1 else ""}</b>'
    )

    # Today's trades (PT session date): broker fills are the source of time, side,
    # qty and price; tier, P&L and exit reason are joined from the tiers' records.
    # Without broker fills the tiers' own records are shown, and the header says so.
    try:
        _events = _read_today_trade_events(LOG_DIR / "trade_events.jsonl", datetime.now(PT).date())
        _fills_ok = alpaca.get("fills_available") is True
        if _fills_ok:
            try:
                _trade_rows = _broker_trade_rows(
                    alpaca.get("fills") or [],
                    _events,
                    _local_order_tiers(orders, _events),
                    _single_holding_tiers(ownership_positions, _qh),
                    _qh,
                )
            except Exception as _join_e:
                logger.warning("_build_html: broker-fill join failed — tier records used: %s", _join_e)
                _fills_ok = False
        if not _fills_ok:
            _trade_rows = _today_trade_rows(_events)
        today_trades_rows = _today_trades_html(_trade_rows)
        _n_rows = len(_trade_rows)
        today_trades_meta = (
            f"{_n_rows} order{'s' if _n_rows != 1 else ''} filled · broker fills"
            if _fills_ok
            else f"{_n_rows} fill{'s' if _n_rows != 1 else ''} · from tier records (broker fills unavailable)"
        )
    except Exception as _tt_e:
        logger.warning("_build_html: today's trades unavailable: %s", _tt_e)
        today_trades_rows = ('<tr><td colspan="8" style="color:#ff9f0a;text-align:center;padding:20px">'
                             "Today's trades unavailable</td></tr>")
        today_trades_meta = "unavailable"

    _tier_cards = []
    _edge_by_tier = edge_snapshot.get("by_tier", {})
    for _tier in (*TIER_IDS, "unattributed"):
        _hist = _edge_by_tier.get(_tier, {}) if isinstance(_edge_by_tier, dict) else {}
        _rpnl = _hist.get("realized_pnl")
        _rpnl_s = "—" if _rpnl is None else f"{'+' if _rpnl >= 0 else ''}${_rpnl:,.2f}"
        _rpnl_c = "#8a94ae" if _rpnl is None else ("#30d158" if _rpnl >= 0 else "#ff3b30")
        _wr = _hist.get("win_rate")
        _wr_s = "—" if _wr is None else f"{_wr:.1f}% WR"
        _n = int(_hist.get("completed_trades") or 0)
        _open_n = len(_open_tier_symbols.get(_tier, set()))
        _color = TIER_COLORS[_tier]
        _tier_cards.append(
            f'<div class="tier-card" style="border-top-color:{_color}">'
            f'<div class="tier-name" style="color:{_color}">{TIER_LABELS[_tier]}</div>'
            f'<div class="tier-open">{_open_n} open <span>· ({_open_tier_qty.get(_tier, 0):g})</span></div>'
            f'<div class="tier-history"><b style="color:{_rpnl_c}">{_rpnl_s}</b>'
            f'<span>{_n} completed · {_wr_s}</span></div></div>'
        )
    _edge_asof = escape(str(edge_snapshot.get("generated_pt", "not yet generated")))
    tier_state_html = (
        '<section class="tier-section"><div class="tier-section-head">'
        '<div><b>Strategy tiers</b><span>Current ownership + broker-ledger history</span></div>'
        f'<small>edge snapshot {str(_edge_asof)}</small></div>'
        f'<div class="tier-grid">{"".join(_tier_cards)}</div></section>'
    )

    _risk_labels = {"HIGH_RISK": "HIGH RISK", "CAUTION": "CAUTION", "MONITOR": "MONITOR"}
    news_rows = ""
    _bot_items = bot_status.get("news_items", []) or _ns.get("news_items", []) or[]
    for item in _bot_items[:5]:
        risk  = item.get("risk", "MONITOR")
        badge = _risk_labels.get(risk, risk)
        news_rows += f"""<div class="news-item">
          <div class="news-headline">{item.get('headline','')}</div>
          <div class="news-meta">
            <span class="risk-badge risk-{risk}">{badge}</span>
            <span>{item.get('source','')}</span>
            <span>{item.get('time','')}</span>
          </div>
        </div>"""

    _mkt_items = market_news.get("items", [])
    _shown = len(_bot_items[:5])
    for item in _mkt_items[:max(0, 8 - _shown)]:
        news_rows += f"""<div class="news-item">
          <div class="news-headline">{item.get('headline','')}</div>
          <div class="news-meta">
            <span class="risk-badge risk-mkt">MARKET</span>
            <span>{item.get('source','')}</span>
            <span>{item.get('time','')}</span>
          </div>
        </div>"""
    if not news_rows:
        news_rows = '<div style="padding:20px 16px;font-family:var(--mono);font-size:11px;color:var(--muted)">No news items</div>'

    # Alert count + risk-aware badge for the collapsible news panel (risk-first:
    # routine news folds behind a badge; a HIGH_RISK alert auto-expands so it
    # can't be missed). Data is preserved — every item still renders inside.
    _news_n = _shown + len(_mkt_items[:max(0, 8 - _shown)])
    _news_has_risk = any(
        it.get("risk") == "HIGH_RISK"
        for it in _bot_items[:5] + _mkt_items[:max(0, 8 - _shown)]
    )
    _news_badge_col = "#ff3b30" if _news_has_risk else "#8a94ae"

    news_ts = market_news.get("generated", "")
    if news_ts:
        try:
            _ndt = datetime.fromisoformat(news_ts.replace("Z", "+00:00")).astimezone(PT)
            news_ts = _ndt.strftime("Updated %-I:%M %p PT")
        except Exception as _nts_e:
            logger.debug(
                "_build_html: news timestamp parse failed"
                " — timestamp display blank: %s",
                _nts_e,
            )
            news_ts = ""

    err_banner = ""
    if alpaca.get("error"):
        err_banner = f'<div style="background:#3a0010;border:1px solid #ff3b5c;border-radius:4px;padding:12px 20px;margin:16px 24px;font-family:monospace;font-size:12px;color:#ff3b5c">⚠ Alpaca API error: {alpaca["error"]}</div>'

    _weekly_files    = sorted(LOG_DIR.glob("weekly_*.html"))
    _today_pt        = datetime.now(PT).date()
    _cur_monday      = _today_pt - timedelta(days=_today_pt.weekday())
    _prev_monday     = _cur_monday - timedelta(weeks=1)
    _prev_week_fname = f"weekly_{_prev_monday.isoformat()}.html"
    _last_week_fname = (
        _prev_week_fname if (LOG_DIR / _prev_week_fname).exists()
        else (_weekly_files[-2].name if len(_weekly_files) >= 2 else "weekly_report.html")
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>MTF Bot — Dashboard</title>
<style>
:root{{--bg:#0d0f1a;--surface:#13162a;--border:#252847;--accent:#00e5ff;--green:#30d158;--red:#ff3b30;--yellow:#ffd60a;--muted:#8a94ae;--text:#e8ecff;--dim:#c8cce4;--mono:'SF Mono','Share Tech Mono',monospace;--sans:-apple-system,'SF Pro Text','Segoe UI',sans-serif}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px;min-height:100vh}}
header{{display:flex;align-items:center;justify-content:space-between;padding:13px 24px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;z-index:100}}
.logo{{font-size:16px;font-weight:700;color:var(--text);letter-spacing:-.01em}}
.logo span{{color:var(--muted);font-size:10px;font-weight:500;display:block;letter-spacing:.08em;text-transform:uppercase;margin-top:2px}}
.hdr-right{{display:flex;align-items:center;gap:16px;font-size:12px;color:var(--dim)}}
.pill{{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:6px;border:1px solid;font-size:11px;font-weight:600;letter-spacing:.03em}}
.pill.open{{border-color:var(--green);color:var(--green);background:rgba(48,209,88,.08)}}
.pill.closed{{border-color:var(--muted);color:var(--muted);background:rgba(99,102,128,.08)}}
.pulse{{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse 1.5s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.kpi-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;padding:16px 24px;background:var(--bg);border-bottom:1px solid var(--border)}}
.kpi{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px}}
.kpi-lbl{{font-size:9px;font-weight:700;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin-bottom:6px}}
.kpi-val{{font-size:22px;font-weight:700;color:var(--text);line-height:1.1}}
.kpi-sub{{font-size:10px;color:var(--dim);margin-top:4px}}
.health-bar{{display:flex;align-items:center;gap:16px;padding:8px 24px;background:var(--bg);border-bottom:1px solid var(--border);font-size:11px}}
.hb-item{{display:flex;align-items:center;gap:6px;color:var(--dim)}}
.hb-val{{font-weight:600}}
.main{{display:grid;grid-template-columns:1fr 360px;gap:16px;padding:16px 24px;background:var(--bg)}}
.panel{{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden}}
.ph{{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;border-bottom:1px solid var(--border)}}
.pt{{font-size:10px;font-weight:700;letter-spacing:.1em;color:var(--dim);text-transform:uppercase}}
.pm{{font-size:10px;color:var(--muted)}}
table{{width:100%;border-collapse:collapse}}
th{{font-size:9px;font-weight:600;letter-spacing:.08em;color:var(--muted);text-transform:uppercase;padding:8px 12px;text-align:left;border-bottom:1px solid var(--border)}}
td{{padding:10px 12px;border-bottom:1px solid var(--border);font-size:12px;color:var(--text)}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:rgba(255,255,255,.03)}}
.ks-bar-wrap{{padding:12px 24px;border-bottom:1px solid var(--border);background:var(--bg)}}
.ks-lbl{{font-size:10px;font-weight:600;color:var(--dim);margin-bottom:6px;display:flex;justify-content:space-between}}
.ks-track{{height:6px;background:var(--border);border-radius:3px;overflow:hidden}}
.ks-fill{{height:100%;border-radius:3px;transition:width .3s}}
.links{{padding:10px;display:flex;flex-direction:column;gap:6px}}
.link-btn{{display:block;padding:10px 14px;border:1px solid var(--border);background:transparent;color:var(--text);text-decoration:none;font-size:12px;font-weight:500;border-radius:6px;transition:all .15s}}
.link-btn:hover{{border-color:var(--accent);color:var(--accent);background:rgba(10,132,255,.06)}}
.spy-badge{{display:inline-block;padding:3px 10px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.04em;border:1px solid currentColor;background:rgba(255,255,255,.03)}}
.mri-badge{{font-size:9px;padding:2px 8px}}
footer{{padding:12px 24px;font-size:11px;color:var(--muted);border-top:1px solid var(--border);display:flex;justify-content:space-between;background:var(--surface)}}
.news-feed{{padding:4px 0}}
.news-details summary{{outline:none}}
.news-details summary::-webkit-details-marker{{display:none}}
.news-details[open] summary .pm{{opacity:.7}}
.news-item{{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:4px}}
.news-item:last-child{{border-bottom:none}}
.news-headline{{font-size:12px;color:var(--text);line-height:1.45}}
.news-meta{{display:flex;align-items:center;gap:8px;font-size:10px;color:var(--muted)}}
.risk-badge{{padding:2px 8px;border-radius:4px;font-size:9px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}}
.risk-HIGH_RISK{{background:rgba(255,59,48,.15);color:#ff3b30;border:1px solid rgba(255,59,48,.3)}}
.risk-CAUTION{{background:rgba(255,214,10,.1);color:#ffd60a;border:1px solid rgba(255,214,10,.25)}}
.risk-MONITOR{{background:rgba(99,102,128,.15);color:#8e92ad;border:1px solid rgba(99,102,128,.3)}}
.risk-mkt{{background:rgba(10,132,255,.08);color:#6aabf7;border:1px solid rgba(10,132,255,.2)}}
.tier-section{{padding:14px 24px 16px;border-bottom:1px solid var(--border);background:var(--bg)}}
.tier-section-head{{display:flex;justify-content:space-between;align-items:flex-end;margin-bottom:10px}}
.tier-section-head b{{font-size:13px}}.tier-section-head span{{font-size:10px;color:var(--muted);margin-left:8px}}
.tier-section-head small{{font-size:9px;color:var(--muted)}}
.tier-grid{{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:8px}}
.tier-card{{background:var(--surface);border:1px solid var(--border);border-top:2px solid;border-radius:8px;padding:10px 12px}}
.tier-name{{font-size:10px;font-weight:750;text-transform:uppercase;letter-spacing:.06em}}
.tier-open{{font-size:16px;font-weight:750;margin-top:4px}}.tier-open span{{font-size:10px;color:var(--muted);font-weight:500}}
.tier-history{{display:flex;justify-content:space-between;gap:6px;margin-top:5px;font-size:10px}}.tier-history span{{color:var(--muted)}}
.tier-badge{{display:inline-block;padding:2px 6px;border:1px solid;border-radius:4px;font-size:9px;font-weight:700;white-space:nowrap}}
{PRIMARY_NAV_CSS}
@media(max-width:768px){{
  .kpi-grid{{grid-template-columns:repeat(2,1fr)!important;padding:10px!important;gap:8px}}
  .kpi{{padding:10px 12px}}
  .kpi-val{{font-size:18px!important}}
  .kpi-lbl{{font-size:8px}}
  .kpi-sub{{font-size:9px}}
  .main{{grid-template-columns:1fr!important;padding:10px!important;gap:10px}}
  header{{flex-wrap:wrap;gap:6px;padding:8px 12px}}
  .logo span{{display:none}}
  .hdr-right{{flex-wrap:wrap;gap:6px;font-size:10px}}
  footer{{flex-direction:column;gap:4px;font-size:9px}}
  .panel table{{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}}
  th{{padding:6px 8px!important;font-size:9px!important;white-space:nowrap}}
  td{{padding:7px 8px!important;font-size:11px!important;white-space:nowrap}}
  .mob-nav{{display:flex!important}}
  .tier-grid{{grid-template-columns:repeat(2,1fr)}}
}}
.mob-nav{{display:none;position:fixed;bottom:0;left:0;right:0;z-index:200;
  background:var(--surface);border-top:1px solid var(--border);padding:8px 0}}
</style>
</head>
<body>

<div id="dash-ts" data-ts="{int(datetime.now(PT).timestamp())}" style="display:none"></div>

<header>
  <div class="logo">MTF Bot <span>COMMAND CENTER</span></div>
  {primary_nav("dashboard")}
  <div class="hdr-right">
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">
      <span style="font-size:12px;color:var(--text)">{LIVE_CLOCK_HTML}</span>
      <span style="font-size:9px;color:var(--dim)">{_scan_cd}</span>
    </div>
    <div class="pill {'open' if _mkt['is_open'] else 'closed'}">
      <span class="pulse"></span>
      {'MARKET OPEN · ' + _mkt['countdown'] if _mkt['is_open'] else 'closed · opens ' + _mkt['open_time'] + ' · in ' + _mkt['countdown']}
    </div>
    <div class="pill {'open' if alpaca.get('status') == 'ACTIVE' else 'closed'}" style="font-size:9px;padding:3px 8px">
      {alpaca.get('status','UNKNOWN')}
    </div>
  </div>
</header>

{err_banner}

<!-- KPI Strip -->
<div class="kpi-grid">
  <div class="kpi">
    <div class="kpi-lbl">Equity</div>
    <div class="kpi-val" style="color:var(--accent)">${equity:,.2f}</div>
    <div class="kpi-sub">Buying power: ${bp:,.2f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Account P&amp;L</div>
    <div class="kpi-val" style="color:{_col(all_pnl)}">{_pnl_str(all_pnl)}</div>
    <div class="kpi-sub">equity − deposits · includes open P&amp;L</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Realized P&L (today)</div>
    <div class="kpi-val" style="color:{realized_pnl_col}">{_pnl_str(realized_pnl)}</div>
    <div class="kpi-sub">{len(eod.get('trades', []))} closed trade(s)</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Open Positions</div>
    <div class="kpi-val" style="color:var(--accent)">{len(open_pos)}</div>
    <div class="kpi-sub">{protection_issues} protection issue{'s' if protection_issues != 1 else ''} · BP: ${bp:,.0f}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">Completed Edge</div>
    <div class="kpi-val" style="color:{_col((all_wr or 0) - 50)}">{'—' if all_wr is None else f'{all_wr:.1f}%'}</div>
    <div class="kpi-sub">{'edge snapshot unavailable' if all_trades is None else f'{all_trades} lifecycles · PF {all_pf:.2f}× · realized {all_realized:+.2f}' if all_pf is not None and all_realized is not None else f'{all_trades} lifecycles'}</div>
  </div>
  <div class="kpi">
    <div class="kpi-lbl">SPY Regime / MRI</div>
    <div class="kpi-val" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span class="spy-badge" style="color:{spy_col};border-color:{spy_col}">{spy_label}</span>
      <span class="spy-badge mri-badge" style="color:{mri_col};border-color:{mri_col}">{mri_level} {mri_score}</span>
    </div>
    <div class="kpi-sub">{spy_sub}</div>
  </div>
{spy_levels_card}
</div>

{tier_state_html}

<!-- Kill switch bar -->
<div class="ks-bar-wrap">
  <div class="ks-lbl">
    <span>{ks_title}</span>
    <span style="color:{ks_col}">{ks_value}</span>
  </div>
  <div class="ks-track">
    <div class="ks-fill" style="width:{ks_fill_pct:.1f}%;background:{ks_col}"></div>
  </div>
</div>

<div class="main">
  <!-- Left: positions + orders -->
  <div class="panel">
    <div class="ph">
      <span class="pt">Open Positions</span>
      <span class="pm">{len(open_pos)} active{protection_badge}</span>
    </div>
    <table>
      <thead><tr>
        <th>Symbol</th><th>Tier</th><th>Side</th><th>Qty</th><th>Current Price</th><th>Entry Price</th>
        <th>Float P&L</th><th>Stop</th><th>Target</th><th>Score</th>
      </tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>

    <div class="ph" style="margin-top:1px">
      <span class="pt">Today&#39;s Trades</span>
      <span class="pm">{today_trades_meta}</span>
    </div>
    <table>
      <thead><tr>
        <th>Time</th><th>Symbol</th><th>Tier</th><th>Side</th><th>Qty</th>
        <th>Fill Price</th><th>Realized P&amp;L</th><th>Exit Reason</th>
      </tr></thead>
      <tbody>{today_trades_rows}</tbody>
    </table>
  </div>

  <!-- Right: links + account info -->
  <div class="panel">
    <div class="ph">
      <span class="pt">Navigation</span>
    </div>
    <div class="links">
      <a class="link-btn" href="scan_results.html">→ Scan Results (Live)</a>
      <a class="link-btn" href="weekly_review.html">→ Weekly Review</a>
      <a class="link-btn" href="monthly_review.html">→ Monthly Review</a>
      <a class="link-btn" href="options.html">→ Options Recommendations</a>
      <a class="link-btn" href="logs/{_last_week_fname}">→ Last Week Report</a>
    </div>

    <details class="news-details"{' open' if _news_has_risk else ''}>
      <summary class="ph" style="margin-top:1px;cursor:pointer;list-style:none">
        <span class="pt">Breaking News <span style="color:{_news_badge_col};font-weight:700">· {_news_n} alert{'s' if _news_n != 1 else ''}</span></span>
        <span class="pm">{news_ts} ▾</span>
      </summary>
      <div class="news-feed">{news_rows}</div>
    </details>

    <div class="ph" style="margin-top:1px">
      <span class="pt">Bot State</span>
    </div>
    <table>
      <tbody>
        <tr><td style="color:var(--dim)">SPY Event</td><td><span style="color:{spy_col};font-weight:700">{spy_label}</span></td></tr>
        <tr><td style="color:var(--dim)">Scans to Clear</td><td>{spy_scans}</td></tr>
        <tr><td style="color:var(--dim)">SPY Move</td><td style="color:{'#00ff88' if spy_mag >= 0 else '#ff3b5c'}">{spy_mag:+.2f}%</td></tr>
        <tr><td style="color:var(--dim)">Daily Brake</td><td style="color:{ks_col}">{ks_value}</td></tr>
        <tr style="border-top:1px solid #1a2530">
          <td style="color:var(--dim)">MRI Score</td>
          <td><span style="color:{mri_col};font-weight:700">{mri_score}/100</span></td>
        </tr>
        <tr>
          <td style="color:var(--dim)">MRI Level</td>
          <td><span class="spy-badge" style="color:{mri_col};border-color:{mri_col};font-size:9px">{mri_level}</span></td>
        </tr>
        <tr>
          <td style="color:var(--dim)">MRI Size Floor</td>
          <td style="color:var(--green)">{mri_size_floor:.2f}x</td>
        </tr>

      </tbody>
    </table>
  </div>
</div>

<footer>
  <span>Auto-refreshes every 30 seconds</span>
  <span>MTF Confluence Bot · Paper Trading · $2,500 account</span>
</footer>

<script>
// P2-STALE-WARN: client-side staleness detection
(function() {{
    var tsEl = document.getElementById('dash-ts');
    if (!tsEl) return;
    var genTs = parseInt(tsEl.getAttribute('data-ts'), 10);
    function checkStale() {{
        var ageSeconds = Math.floor(Date.now() / 1000) - genTs;
        var banner = document.getElementById('stale-warn-banner');
        if (ageSeconds > 90) {{
            if (!banner) {{
                banner = document.createElement('div');
                banner.id = 'stale-warn-banner';
                banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:#ff453a;color:#fff;text-align:center;padding:8px 16px;font-size:13px;font-weight:600;letter-spacing:0.02em;';
                banner.innerHTML = '⚠ DASHBOARD DATA IS STALE (' + Math.floor(ageSeconds / 60) + 'm ' + (ageSeconds % 60) + 's) — Live writer may be down. Refresh or restart live_data_writer.py';
                document.body.prepend(banner);
            }}
        }} else {{
            if (banner) banner.remove();
        }}
    }}
    checkStale();
    setInterval(checkStale, 15000);
}})();
</script>

<nav class="mob-nav">
  <a href="dashboard.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#0a84ff;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">📊</span>Dashboard
  </a>
  <a href="scan_results.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#b8bdd4;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">🔍</span>Scanner
  </a>
  <a href="weekly_review.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#b8bdd4;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">📅</span>Weekly
  </a>
  <a href="options.html" style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;color:#b8bdd4;text-decoration:none;font-size:10px;font-weight:600">
    <span style="font-size:18px">⚡</span>Options
  </a>
</nav>

</body>
</html>
"""
    return html


def generate():
    """Load all data sources and write dashboard.html atomically."""
    alpaca      = _load_alpaca()
    trade_log   = _load_trade_log()
    hybrid      = _load_hybrid_state()
    eod         = _load_today_eod()
    bot_status  = _load_bot_status()
    market_news = _load_market_news()

    html = _build_html(alpaca, trade_log, hybrid, eod,
                       bot_status=bot_status, market_news=market_news)

    out_path = ROOT / "dashboard.html"
    tmp_path = out_path.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
        tmp_path.replace(out_path)
        logger.info(f"dashboard.html written ({len(html):,} bytes)")
    except Exception as e:
        logger.error(f"Failed to write dashboard.html: {e}")
        raise


if __name__ == "__main__":
    generate()
