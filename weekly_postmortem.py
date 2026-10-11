#!/usr/bin/env python3
# ruff: noqa: E501, E701  (long report/prompt strings; intentional one-line dispatch in _shorten_reason)
"""
weekly_postmortem.py
Weekly Trade Post-Mortem (WTP) — reads trade_events.jsonl, fetches
weekly/daily Alpaca T1 bars, checks earnings cache, computes Weinstein
stage, sends to Gemini for adversarial analysis, posts summary to Slack.

Saturday 6 AM PT (cron: 0 13,14 * * 6, cron_tz_wrapper 06:00)
Sunday  6 AM PT (cron: 0 13,14 * * 0, cron_tz_wrapper 06:00 — retry if Sat failed)

Script skips automatically if logs/wtp_{friday}.md already exists.

Read-only — no execution imports, no order calls, no shared state.
"""

import json
import logging
import math
import os
import re
import ssl
import sys
import time as _time_mod
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from gai_client import GAI_MODEL_LADDER  # single source of truth for the live Gemini model ladder
from tier_names import TIER_IDS, TierId, canonical_tier, tier_label
from dotenv import load_dotenv
load_dotenv()

PT  = ZoneInfo("America/Los_Angeles")
ET  = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("weekly_postmortem")

# ── Config ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "").strip()
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL", "").strip()
ALPACA_KEY      = os.getenv("ALPACA_API_KEY", "").strip()
ALPACA_SECRET   = os.getenv("ALPACA_SECRET_KEY", "").strip()
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
GEMINI_MODEL    = GAI_MODEL_LADDER[0]   # primary + display; the full ladder drives the fallback loop

BASE_DIR        = Path(__file__).resolve().parent
LOGS_DIR        = BASE_DIR / "logs"
TRADE_EVENTS    = LOGS_DIR / "trade_events.jsonl"
POSTMORTEM_DIR  = BASE_DIR / "data" / "state" / "postmortem"
EARNINGS_CACHE  = BASE_DIR / "data" / "cache" / "earnings_week.json"

_TIER_ZERO = {tier: 0.0 for tier in TIER_IDS}

try:
    import certifi as _certifi
    _SSL_CTX = ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# ── Week range ─────────────────────────────────────────────────────────────────

def _get_week_range() -> tuple[date, date]:
    """Return (monday, friday) for the most recently completed Mon-Fri week."""
    today = datetime.now(PT).date()
    # Weekday: Mon=0 … Fri=4 … Sat=5 … Sun=6
    # Find the most recent Friday
    days_back = (today.weekday() - 4) % 7   # 0 if today IS Friday
    if days_back == 0 and today.weekday() != 4:
        days_back = 7  # today is the exact weekday but not Friday — shouldn't happen
    if today.weekday() == 5:   # Saturday → yesterday was Friday
        days_back = 1
    elif today.weekday() == 6: # Sunday → two days ago was Friday
        days_back = 2
    friday = today - timedelta(days=days_back)
    monday = friday - timedelta(days=4)
    return monday, friday

# ── Trade loading ──────────────────────────────────────────────────────────────

def _load_trades_for_week(monday: date, friday: date) -> list[dict]:
    """
    Read trade_events.jsonl (authoritative), match entry→exit pairs
    for all exits in [monday, friday]. Returns list of enriched dicts.
    """
    if not TRADE_EVENTS.exists():
        logger.error(f"trade_events.jsonl not found: {TRADE_EVENTS}")
        return []

    entries: dict[str, dict] = {}   # symbol → most recent entry event
    trades: list[dict]       = []

    with open(TRADE_EVENTS, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event  = ev.get("event", "")
            sym    = ev.get("symbol", "")
            ts_str = ev.get("ts", "")
            if not sym:
                continue

            try:
                ts      = datetime.fromisoformat(ts_str)
                ev_date = ts.astimezone(PT).date()
            except Exception:
                continue

            if event == "entry":
                entries[sym] = ev
            elif event == "exit" and monday <= ev_date <= friday:
                entry_ev = entries.get(sym, {})
                trades.append({
                    "symbol":      sym,
                    "direction":   ev.get("direction") or entry_ev.get("direction", ""),
                    "entry_price": entry_ev.get("price"),
                    "exit_price":  ev.get("price"),
                    "entry_time":  entry_ev.get("ts", ""),
                    "exit_time":   ev.get("ts", ""),
                    "pnl":         ev.get("pnl", 0.0) or 0.0,
                    "exit_reason": ev.get("reason", ""),
                    "score":       entry_ev.get("score") or ev.get("score", "?"),
                    "mri_level":   entry_ev.get("mri_level") or ev.get("mri_level", "?"),
                    "size":        ev.get("size", 1),
                    "exit_date":   ev_date,
                })
    return trades

# ── Alpaca fills FIFO reconstruction (authoritative Entry$/Exit$/P&L — Part B) ──
# WHY: _load_trades_for_week (above) reads Entry$/Exit$/P&L from trade_events.jsonl, the bot's
# own event log. When an `entry` event is missing (e.g. DDOG/XOM week of 2026-07-20 had NONE),
# Entry$/Hold/R-Mult/TQI all blank and the report's whole quantitative core is empty. The column
# header even claimed "Alpaca fills API (authoritative)" while the code never called it. These
# functions make that true: reconstruct closed round-trips from the Alpaca FILL activities API
# (FIFO), so entry price AND entry timestamp are authoritative and complete regardless of any
# event-log gap. Read-only; NO execution imports (this script's contract) — the FIFO logic mirrors
# execution/fifo_pnl.py but is kept local. score/MRI/exit_reason (not in fills) are enriched
# best-effort from trade_events.jsonl afterward.

_FILLS_LOOKBACK_DAYS = 90   # generous: captures the opening fill for intraday+swing+most-of-quarter
                            # holds so a within-week EXIT matches its real entry (not a pre-window gap)


def _parse_alpaca_ts(ts_str: str) -> datetime:
    """Tolerant ISO-8601 parser: Z→+00:00, fractional seconds padded/truncated to 6 digits
    (Alpaca emits variable precision; Py3.10 fromisoformat needs exactly 3 or 6). Read-only
    mirror of execution/fifo_pnl.py::_parse_alpaca_ts, kept local to honor the no-execution-import
    contract. Raises ValueError on genuinely malformed input — caller decides the fallback."""
    s = str(ts_str).strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?[T ]\d{2}:\d{2}:\d{2})\.(\d+)(.*)$", s)
    if m:
        head, frac, tail = m.groups()
        s = f"{head}.{frac[:6].ljust(6, '0')}{tail}"
    dt = datetime.fromisoformat(s)
    # Alpaca transaction_time is always UTC (Z). Defensive: if a value ever arrives offset-less,
    # assume UTC so a later `.astimezone(ET).date()` can't silently mis-date it via the host's
    # local tz (data-integrity board note, 2026-07-26).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_fills_range(start_d: date, end_d: date) -> "tuple[list[dict], bool]":
    """All Alpaca FILL activities in [start_d 00:00 ET, end_d 23:59 ET], paginated with
    `page_token` = the last activity's id. (Empirically verified 2026-07-26 against the live paper
    endpoint: `page_token` advances the window; `after_id` does NOT — it repeats page 1, which
    would loop on the oldest fills and never reach the target week. A `seen`-id guard stops a
    non-advancing token from spinning to the page cap.)

    Returns (fills, complete). `complete` is True ONLY when pagination reached a natural end
    (a <100 page, or a fully-seen page, or a genuine empty result). It is False on ANY partial
    read — keys unset, a page's 3 retries exhausted, a missing next-page token, or the 500-page
    cap. This flag is LOAD-BEARING: fills are fetched oldest-first (asc), so the TARGET WEEK is on
    the LAST pages — a mid-pagination failure returns only OLD fills with zero in-week trades. The
    caller MUST NOT treat a partial read (complete=False) as an authoritative 'no-trade week'
    (data-integrity board T1, 2026-07-26); it falls back / flags instead."""
    if not ALPACA_KEY or not ALPACA_SECRET:
        logger.warning("Alpaca keys unset — cannot fetch fills; falling back to event log.")
        return [], False
    et_start = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=ET)
    et_end   = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=ET)
    headers  = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
    base     = "https://paper-api.alpaca.markets/v2/account/activities/FILL"
    all_fills: list[dict] = []
    seen_ids: set = set()
    page_token: "str | None" = None
    complete = False
    for _page in range(500):   # backstop cap; real pagination stops on the first <100 page
        params = {"direction": "asc", "page_size": "100",
                  "after": et_start.isoformat(), "until": et_end.isoformat()}
        if page_token:
            params["page_token"] = page_token
        url  = f"{base}?{urllib.parse.urlencode(params)}"
        page: "list | dict | None" = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
                    page = json.loads(r.read().decode())
                # The FILL endpoint returns a JSON ARRAY. ANY dict body is an error/anomaly (an
                # error object like {"code":...} or {"message":...}, or an unexpected shape) — treat
                # it as a failed page, not data. Gating only on `.get("code")` let a code-less dict
                # slip to `[f for f in page ...]` which iterates dict KEYS → str.get → AttributeError,
                # crashing the whole read-only report (cold-2nd T1, 2026-07-26). Fail CLOSED instead.
                if isinstance(page, dict):
                    raise RuntimeError(f"Alpaca FILL API returned a non-array body: {str(page)[:200]}")
                break
            except Exception as e:
                logger.warning(f"fills fetch attempt {attempt + 1}/3 failed: {e}")
                page = None
                _time_mod.sleep(2 ** attempt)
        if page is None:
            complete = False           # hard failure after retries → PARTIAL read
            break
        if not page:                   # genuine empty list → complete (nothing more to fetch)
            complete = True
            break
        # Non-advancing token: a full page whose ids are ALL already seen means `page_token`
        # stopped advancing (an API clamp/regression) — we are STUCK on the oldest pages with
        # PARTIAL data, not at a natural end (the natural end is the empty-page or <100-page path
        # above/below). Fail CLOSED here: complete=False → the caller falls back + warns, never
        # presents oldest-only fills as an authoritative no-trade week (data-integrity board T1
        # round-2 residual, 2026-07-26). A false 'incomplete' only triggers a loud fallback; a
        # false 'complete' would lock in a wrong week — so this direction is the safe one.
        new = [f for f in page if f.get("id") not in seen_ids]
        if not new:
            complete = False
            break
        seen_ids.update(f.get("id") for f in new)
        all_fills.extend(new)
        if len(page) < 100:            # last page → natural end
            complete = True
            break
        page_token = page[-1].get("id")
        if not page_token:             # cannot paginate further but there may be more → PARTIAL
            complete = False
            break
    # for-else: loop ran the full 500 pages without breaking → cap hit → PARTIAL (complete stays False)
    return all_fills, complete


def _mk_trade(sym, direction, entry, exit_, qty, entry_time, exit_time, pnl) -> dict:
    """Build a WTP trade dict from a matched round-trip. entry/pnl may be None (a close whose
    opening fill is older than the lookback window — shown as '?' and excluded from totals)."""
    return {
        "symbol": sym, "direction": direction,
        "entry_price": (round(float(entry), 4) if entry is not None else None),
        "exit_price":  round(float(exit_), 4),
        "entry_time":  entry_time or "", "exit_time": exit_time or "",
        "pnl":         (round(float(pnl), 4) if pnl is not None else None),
        "size":        int(qty),
        "score": "?", "mri_level": "?", "exit_reason": "",
        "_unmatched": entry is None,
    }


def _reconstruct_closed_trades(fills: list[dict], monday: date, friday: date) -> list[dict]:
    """FIFO-reconstruct round-trips from Alpaca fills; return those whose EXIT ET date ∈
    [monday, friday]. Net-position-aware: a buy/buy_to_cover covers open shorts FIFO; a
    sell/sell_short closes open longs FIFO. Entry price AND timestamp come from the matched
    opening lot (authoritative). A close with no open lot (opening fill older than the lookback
    window) is emitted UNMATCHED (entry/pnl = None → '?'), never fabricated. No QHM special-case
    (excluding QHM would need an execution import this script forbids; a QHM close in-week is a
    legitimate row). Mirrors execution/fifo_pnl.py::_fifo_reconstruct, read-only, single pass."""
    def _key(f):
        try:
            return _parse_alpaca_ts(f.get("transaction_time", ""))
        except Exception:
            return datetime.max.replace(tzinfo=ET)
    ordered = sorted(fills, key=_key)
    lots: dict[str, list[dict]] = {}
    closed: list[dict] = []
    for f in ordered:
        sym  = f.get("symbol", "")
        side = f.get("side", "")
        try:
            qty   = int(float(f.get("qty", 0)))
            price = float(f.get("price", 0))
        except (TypeError, ValueError):
            continue
        filled_at = f.get("transaction_time", "")
        if not sym or qty <= 0:
            continue
        cur = lots.setdefault(sym, [])
        net = sum(lot["qty"] * (1 if lot["side"] == "long" else -1) for lot in cur)

        if side in ("buy", "buy_to_cover"):
            if net < 0:                       # cover open shorts FIFO
                rem = qty
                while rem > 0 and cur and cur[0]["side"] == "short":
                    lot = cur[0]
                    mch = min(lot["qty"], rem)
                    closed.append(_mk_trade(sym, "short", lot["price"], price, mch,
                                            lot["filled_at"], filled_at, (lot["price"] - price) * mch))
                    lot["qty"] -= mch
                    rem        -= mch
                    if lot["qty"] == 0:
                        cur.pop(0)
                if rem > 0:                   # over-covered → flips to long
                    cur.append({"qty": rem, "price": price, "side": "long", "filled_at": filled_at})
            elif side == "buy_to_cover":      # cover with no open short = opening fill pre-window
                closed.append(_mk_trade(sym, "short", None, price, qty, "", filled_at, None))
            else:                             # plain buy → open/extend long
                cur.append({"qty": qty, "price": price, "side": "long", "filled_at": filled_at})

        elif side in ("sell", "sell_short"):
            if net > 0:                       # close open longs FIFO
                rem = qty
                while rem > 0 and cur and cur[0]["side"] == "long":
                    lot = cur[0]
                    mch = min(lot["qty"], rem)
                    closed.append(_mk_trade(sym, "long", lot["price"], price, mch,
                                            lot["filled_at"], filled_at, (price - lot["price"]) * mch))
                    lot["qty"] -= mch
                    rem        -= mch
                    if lot["qty"] == 0:
                        cur.pop(0)
                if rem > 0:                   # over-sold → flips to short
                    cur.append({"qty": rem, "price": price, "side": "short", "filled_at": filled_at})
            elif side == "sell_short":        # deliberate short OPEN
                cur.append({"qty": qty, "price": price, "side": "short", "filled_at": filled_at})
            else:                             # plain `sell` with no open long = opening fill pre-window
                closed.append(_mk_trade(sym, "long", None, price, qty, "", filled_at, None))

    out: list[dict] = []
    for t in closed:
        try:
            ed = _parse_alpaca_ts(t["exit_time"]).astimezone(ET).date()
        except Exception:
            continue
        if monday <= ed <= friday:
            t["exit_date"] = ed
            out.append(t)
    return out


def _aggregate_by_position(trades: list[dict]) -> list[dict]:
    """Collapse fill-level round-trips into position-level rows: group by (symbol, direction, exit
    ET date). qty-weighted-avg entry/exit, summed qty, summed P&L, earliest real entry_time /
    latest exit_time. The numbers are identical (a sum of the same FIFO round-trips) — just fewer,
    readable rows: a position exited in N partial fills becomes 1 row, not N. If ANY row in a group
    is unmatched (opening fill pre-window), the whole position is marked unmatched with P&L '?'
    (an honest — never a partial-summed — position P&L)."""
    groups: dict = {}
    for t in trades:
        groups.setdefault((t["symbol"], t["direction"], t["exit_date"]), []).append(t)
    out: list[dict] = []
    for (sym, direction, ed), rows in groups.items():
        qty      = sum(r["size"] for r in rows)
        any_unm  = any(r.get("_unmatched") for r in rows)
        matched  = [r for r in rows if r["entry_price"] is not None]
        mq       = sum(r["size"] for r in matched)
        # Explicit guards (not a ternary) so the div-by-zero IMPOSSIBILITY is unmistakable: the
        # division runs ONLY inside `if mq > 0:` / `if qty > 0:`, never otherwise. mq==0 only when
        # every row is unmatched → w_entry stays None; qty is always >=1 (every row has size>=1).
        w_entry = None
        if mq > 0:
            w_entry = round(sum(r["entry_price"] * r["size"] for r in matched) / mq, 4)
        w_exit = 0.0
        if qty > 0:
            w_exit = round(sum(r["exit_price"] * r["size"] for r in rows) / qty, 4)
        pnl = None if any_unm else round(sum(r["pnl"] for r in rows), 4)
        ent_times = [r["entry_time"] for r in rows if r["entry_time"]]
        agg = _mk_trade(sym, direction, w_entry, w_exit, qty,
                        (min(ent_times) if ent_times else ""),
                        max(r["exit_time"] for r in rows), pnl)
        agg["exit_date"]  = ed
        agg["_unmatched"] = any_unm or (w_entry is None)
        out.append(agg)
    out.sort(key=lambda t: t["exit_time"])
    return out


def _canonical_integrity_errors(figures) -> list[str]:
    """Return every condition that makes canonical weekly figures unsafe to publish."""
    errors = []
    if not figures.available:
        errors.append("the canonical Alpaca ledger was unavailable")
    if figures.unmatched_closes:
        errors.append(f"{len(figures.unmatched_closes)} unmatched closing fill(s)")
    if figures.missing_order_joins:
        errors.append(f"{len(figures.missing_order_joins)} missing fill-to-order join(s)")
    if figures.missing_close_identities:
        errors.append(f"{len(figures.missing_close_identities)} missing close identity(ies)")
    if not isinstance(figures.invariant, dict) or figures.invariant.get("ok") is not True:
        errors.append("the account equity reconciliation invariant did not pass")
    return errors


def _week_ledger_view(round_trips: list[dict], monday: date, friday: date) -> tuple[list[dict], dict]:
    """Build the weekly accounting and completed-lifecycle views from canonical FIFO rows.

    Weekly realized cash is attributed by each closing fill's PT date.  W/L is a separate
    performance layer: only an entry lifecycle whose final close lands in this week is counted,
    and its P&L includes every partial close in that lifecycle.  A still-open lifecycle can
    contribute real weekly cash, but can never masquerade as a completed winner.
    """
    start, end = monday.isoformat(), friday.isoformat()
    groups: dict[tuple, list[dict]] = {}
    weekly_realized = 0.0
    weekly_by_tier = dict(_TIER_ZERO)

    relevant_ids: set[str] = set()
    expected_signature: dict[str, tuple[str, str, str]] = {}

    def _identity_time(row: dict) -> tuple[str, str, str, datetime, datetime, date]:
        """Validate immutable broker identity and chronology before week selection."""
        symbol = row.get("symbol")
        direction = row.get("direction")
        entry_id = row.get("entry_order_id")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("weekly lifecycle symbol is missing")
        if symbol != symbol.strip():
            raise ValueError("weekly lifecycle symbol has surrounding whitespace")
        if direction not in ("long", "short"):
            raise ValueError(f"weekly lifecycle direction is invalid for {symbol}: {direction!r}")
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError(f"weekly entry order identity is missing for {symbol}")
        if entry_id != entry_id.strip():
            raise ValueError(f"weekly entry order identity has surrounding whitespace for {symbol}")
        entry_raw = row.get("entry_time")
        exit_raw = row.get("exit_time")
        exit_date_raw = row.get("exit_date")
        if not isinstance(entry_raw, str):
            raise ValueError(f"weekly lifecycle time is invalid for {symbol}")
        if not isinstance(exit_raw, str):
            raise ValueError(f"weekly lifecycle time is invalid for {symbol}")
        if not isinstance(exit_date_raw, str):
            raise ValueError(f"weekly lifecycle time is invalid for {symbol}")
        try:
            entry_time = _parse_alpaca_ts(entry_raw)
            exit_time = _parse_alpaca_ts(exit_raw)
            exit_day = date.fromisoformat(exit_date_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"weekly lifecycle time is invalid for {symbol}") from exc
        if entry_time > exit_time:
            raise ValueError(f"weekly lifecycle entry occurs after exit for {symbol}")
        if exit_time.astimezone(PT).date() != exit_day:
            raise ValueError(f"weekly exit date/time disagree for {symbol}")
        return symbol, direction, entry_id, entry_time, exit_time, exit_day

    def _validated(row: dict) -> tuple[str, str, str, datetime, TierId, int, float]:
        symbol, direction, entry_id, entry_time, _exit_time, _exit_day = _identity_time(row)
        try:
            tier = canonical_tier(row.get("tier"))
        except ValueError as exc:
            raise ValueError(
                f"weekly tier attribution is incomplete for {symbol}: {row.get('tier')!r}"
            ) from exc
        qty = row.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"weekly lifecycle quantity is invalid for {symbol}: {qty!r}")
        complete = row.get("lifecycle_complete")
        if not isinstance(complete, bool):
            raise ValueError(
                f"weekly lifecycle completion state is invalid for {symbol}: {complete!r}"
            )
        for name in ("entry_price", "exit_price", "pnl"):
            value = row.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"weekly {name} is invalid for {symbol}: {value!r}")
            if not math.isfinite(float(value)):
                raise ValueError(f"weekly {name} is non-finite for {symbol}: {value!r}")
            if name != "pnl" and float(value) <= 0:
                raise ValueError(f"weekly {name} is not positive for {symbol}: {value!r}")
        return symbol, direction, entry_id, entry_time, tier, qty, float(row["pnl"])

    for row in round_trips:
        symbol, direction, entry_id, _entry_time, _exit_time, exit_day = _identity_time(row)
        if not (monday <= exit_day <= friday):
            continue
        symbol, direction, entry_id, _entry_time, tier, _qty, _pnl = _validated(row)
        signature = (symbol, direction, tier)
        if entry_id in expected_signature and expected_signature[entry_id] != signature:
            raise ValueError(f"weekly entry identity has conflicting ownership: {entry_id}")
        expected_signature[entry_id] = signature
        relevant_ids.add(entry_id)

    for row in round_trips:
        raw_entry_id = row.get("entry_order_id")
        if raw_entry_id not in relevant_ids:
            continue
        symbol, direction, entry_id, entry_time, tier, _qty, pnl = _validated(row)
        if expected_signature[entry_id] != (symbol, direction, tier):
            raise ValueError(f"weekly entry identity has conflicting ownership: {entry_id}")
        item = dict(row)
        item["tier"] = tier
        item["_entry_dt"] = entry_time
        item["_exit_dt"] = _parse_alpaca_ts(item["exit_time"])
        key = (symbol, entry_id, direction, tier)
        groups.setdefault(key, []).append(item)
        item_exit_day = str(item.get("exit_date") or "")
        if start <= item_exit_day <= end:
            weekly_realized += pnl
            weekly_by_tier[tier] += pnl

    trades: list[dict] = []
    partial_open_lifecycles = 0
    partial_open_weekly_pnl = 0.0
    partial_open_by_tier = dict(_TIER_ZERO)
    for (_sym, _identity, _direction, tier), rows in groups.items():
        rows.sort(key=lambda row: row["_exit_dt"])
        weekly_rows = [
            row for row in rows
            if start <= str(row.get("exit_date") or "") <= end
        ]
        if not weekly_rows:
            continue
        complete = all(row["lifecycle_complete"] for row in rows)
        if not complete:
            value = sum(float(row.get("pnl") or 0.0) for row in weekly_rows)
            partial_open_lifecycles += 1
            partial_open_weekly_pnl += value
            partial_open_by_tier[tier] += value
            continue
        final_instant = rows[-1]["_exit_dt"]
        final_ids = {
            str(row.get("exit_order_id") or "")
            for row in rows
            if row["_exit_dt"] == final_instant
        }
        if len(final_ids) != 1:
            raise ValueError(f"weekly final exit identity is ambiguous for {_sym}")
        final = rows[-1]
        final_day = str(final.get("exit_date") or "")
        if not (start <= final_day <= end):
            continue
        qty = sum(int(row.get("qty") or 0) for row in rows)
        if qty <= 0:
            raise ValueError(f"weekly lifecycle quantity is invalid for {_sym}: {qty}")
        entry_value = sum(float(row["entry_price"]) * int(row["qty"]) for row in rows)
        exit_value = sum(float(row["exit_price"]) * int(row["qty"]) for row in rows)
        trade = _mk_trade(
            _sym,
            _direction,
            entry_value / qty,
            exit_value / qty,
            qty,
            min(rows, key=lambda row: row["_entry_dt"])["entry_time"],
            final["exit_time"],
            sum(float(row.get("pnl") or 0.0) for row in rows),
        )
        trade.update({
            "exit_date": date.fromisoformat(final_day),
            "tier": tier,
            "entry_order_id": final.get("entry_order_id") or _identity,
            "final_exit_order_id": str(final.get("exit_order_id") or ""),
            "week_realized_pnl": round(
                sum(float(row.get("pnl") or 0.0) for row in weekly_rows), 2
            ),
            "_unmatched": False,
        })
        trades.append(trade)

    trades.sort(key=lambda trade: str(trade.get("exit_time") or ""))
    _weekly_by_tier_rounded = {
        tier: round(weekly_by_tier[tier], 2) for tier in TIER_IDS
    }
    accounting = {
        "weekly_realized": round(weekly_realized, 2),
        "weekly_by_tier": _weekly_by_tier_rounded,
        "partial_open_lifecycles": partial_open_lifecycles,
        "partial_open_weekly_pnl": round(partial_open_weekly_pnl, 2),
        "partial_open_by_tier": {
            tier: round(partial_open_by_tier[tier], 2) for tier in TIER_IDS
        },
    }
    _tier_total = sum(_weekly_by_tier_rounded[tier] for tier in TIER_IDS)
    if round(_tier_total, 2) != accounting["weekly_realized"]:
        raise ValueError("weekly tier split does not reconcile to weekly realized P&L")
    return trades, accounting


def _enrich_metadata_from_events(trades: list[dict], monday: date, friday: date) -> None:
    """Best-effort metadata join using exact broker order identity only.

    Same-symbol co-holds make nearest-time/day matching unsafe: a Day stop and a Swing exit may land
    seconds apart. Missing exact identity leaves metadata unknown rather than borrowing another tier's
    score, MRI, or exit reason. Prices, P&L, and tier attribution always remain broker-sourced.
    """
    if not TRADE_EVENTS.exists():
        return
    by_order: dict[str, list[dict]] = {}
    try:
        with open(TRADE_EVENTS, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") not in ("exit", "partial_exit", "stop_hit"):
                    continue
                for key in ("order_id", "exit_order_id", "stop_order_id"):
                    oid = str(ev.get(key) or "")
                    if oid:
                        by_order.setdefault(oid, []).append(ev)
    except Exception as e:
        logger.warning(f"metadata enrich: could not read trade_events: {e}")
        return
    for t in trades:
        final_id = str(t.get("final_exit_order_id") or "")
        matches = [
            event for event in by_order.get(final_id, [])
            if event.get("symbol") == t.get("symbol")
        ]
        if matches:
            def _event_time(event: dict) -> datetime:
                try:
                    value = datetime.fromisoformat(str(event.get("ts") or ""))
                    return value if value.tzinfo else value.replace(tzinfo=PT)
                except (TypeError, ValueError):
                    return datetime.min.replace(tzinfo=timezone.utc)

            best = max(matches, key=_event_time)
            t["score"] = best.get("score", "?")
            t["mri_level"] = best.get("mri_level", "?")
            t["exit_reason"] = best.get("reason", "") or t["exit_reason"]


# ── Postmortem ─────────────────────────────────────────────────────────────────

def _load_postmortem(symbol: str, exit_date: date) -> dict:
    """Load postmortem JSON for symbol near exit_date. Returns {} if not found."""
    for delta in [0, -1, 1, -2, 2]:
        path = POSTMORTEM_DIR / f"{(exit_date + timedelta(days=delta)).isoformat()}_{symbol}.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
    return {}

# ── Alpaca bars ────────────────────────────────────────────────────────────────

def _alpaca_get(params: str) -> dict:
    url = f"{ALPACA_BARS_URL}?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        },
    )
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        logger.warning(f"Alpaca API error: {e}")
        return {}


def _fetch_weekly_closes(symbols: list[str], weeks: int = 22) -> dict[str, list[float]]:
    """Fetch last {weeks} weekly closes for stage computation."""
    if not ALPACA_KEY or not symbols:
        return {}
    start = (datetime.now(PT).date() - timedelta(weeks=weeks + 2)).isoformat()
    data  = _alpaca_get(
        f"symbols={','.join(symbols)}&timeframe=1Week"
        f"&start={start}T00:00:00Z&limit=1000&sort=asc"
    )
    return {sym: [b["c"] for b in bars] for sym, bars in (data.get("bars") or {}).items()}


def _fetch_friday_close(symbols: list[str], friday: date) -> dict[str, float]:
    """Fetch the Friday (last daily bar of the week) close for each symbol."""
    if not ALPACA_KEY or not symbols:
        return {}
    monday = friday - timedelta(days=4)
    data   = _alpaca_get(
        f"symbols={','.join(symbols)}&timeframe=1Day"
        f"&start={monday.isoformat()}T00:00:00Z"
        f"&end={friday.isoformat()}T23:59:59Z&limit=500&sort=desc"
    )
    result = {}
    for sym, bars in (data.get("bars") or {}).items():
        if bars:
            result[sym] = bars[0]["c"]   # desc sort → first = most recent = Friday
    return result

# ── Weinstein stage ────────────────────────────────────────────────────────────

def _compute_weinstein_stage(weekly_closes: list[float]) -> str:
    """
    Simplified Stage Analysis using last 20 weekly closes and 20-wk SMA.
    Stage 2: above SMA + rising week-over-week
    Stage 3: above SMA + declining (topping)
    Stage 4: below SMA + declining
    Stage 1: below SMA + rising (basing)
    """
    if len(weekly_closes) < 4:
        return "N/A"
    window = weekly_closes[-20:]
    sma    = sum(window) / len(window)
    last   = window[-1]
    prev   = window[-2]
    above  = last > sma
    rising = last > prev
    if above and rising:
        return "Stage 2"
    if above and not rising:
        return "Stage 3"
    if not above and not rising:
        return "Stage 4"
    return "Stage 1"

# ── Earnings ───────────────────────────────────────────────────────────────────

def _load_earnings_cache() -> dict:
    try:
        with open(EARNINGS_CACHE) as f:
            return json.load(f)
    except Exception:
        return {}


def _had_earnings_during_hold(symbol: str, entry_time: str, exit_time: str, cache: dict) -> bool:
    """True if any FMP earnings date falls within the hold window."""
    try:
        t0 = datetime.fromisoformat(entry_time).date()
        t1 = datetime.fromisoformat(exit_time).date()
    except Exception:
        return False
    for d_str in (cache.get("data") or {}).get(symbol.upper(), []):
        try:
            if t0 <= date.fromisoformat(str(d_str)[:10]) <= t1:
                return True
        except Exception:
            pass
    # Supplement: check earnings_surprise_{symbol}.json cache files
    surprise = BASE_DIR / "data" / "cache" / f"earnings_surprise_{symbol}.json"
    if surprise.exists():
        try:
            with open(surprise) as f:
                items = json.load(f)
            for item in (items if isinstance(items, list) else []):
                d_str = item.get("date", "")
                if d_str:
                    try:
                        if t0 <= date.fromisoformat(str(d_str)[:10]) <= t1:
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
    return False

# ── Helpers ────────────────────────────────────────────────────────────────────

def _shorten_reason(reason: str) -> str:
    r = reason.lower()
    if "overnight_atr_buffer_exit" in r: return "ATR Buffer"
    if "hard_stop"                  in r: return "Hard Stop"
    if "breakeven"                  in r: return "Breakeven"
    if "target"                     in r: return "Target"
    if "fill_unverified"            in r: return "Fill Unverf."
    if "external_close_detected_ah" in r: return "AH External"
    if "external_close"             in r: return "Ext. Close"
    if "trail_stop"                 in r: return "Trail Stop"
    if "opposite_signal"            in r: return "Signal"
    if "signal"                     in r: return "Signal"
    if "pm_exit"                    in r: return "PM Exit"
    if "thesis_invalidation"        in r: return "Thesis Inv."
    return reason[:18]


def _hold_hours(entry_time: str, exit_time: str) -> str:
    try:
        h = (datetime.fromisoformat(exit_time) - datetime.fromisoformat(entry_time)).total_seconds() / 3600
        return f"{round(h, 1)}h"
    except Exception:
        return "?"


def _r_multiple(entry: float | None, pnl: "float | None", size: int, pm: dict) -> str:
    try:
        stop = pm.get("stop") or pm.get("original_stop")
        if not stop or not entry or pnl is None:
            return "?"
        risk = abs(float(entry) - float(stop))
        if risk < 0.01:
            return "?"
        return f"{(pnl / max(size, 1)) / risk:.2f}R"
    except Exception:
        return "?"

# ── Table builder ──────────────────────────────────────────────────────────────

def _build_wtp_table(
    trades: list[dict],
    friday_closes: dict[str, float],
    weekly_closes: dict[str, list[float]],
    earnings_cache: dict,
    accounting: "dict | None" = None,
) -> tuple[str, str, dict]:
    """
    Returns (full_table_md, slack_compact, stats_dict).
    full_table_md: all 16 columns — goes to the .md report + the Gemini prompt.
    slack_compact: a mobile-friendly one-liner per trade for the Slack post (the old wide
    9-col markdown table wrapped into unreadable soup on phones — Rafael 2026-08-30).
    """
    full_header = (
        "| Tier | Symbol | Dir | Entry$ | Exit$ | Lifecycle P&L | Exit Reason | Score | MRI | "
        "Earnings | TQI | Hold | R-Mult | Stage | Fri Close | Δ/share Exit→Fri* |\n"
        "|------|--------|-----|--------|-------|---------------|-------------|-------|-----|"
        "----------|-----|------|--------|-------|------------|-------------|\n"
    )
    full_rows  = []
    slack_rows: list[tuple] = []    # (pnl_sort_key, monospace row) — sorted worst→best for the Slack post
    total_pnl  = 0.0
    positive_friday_diagnostics = 0

    for t in trades:
        sym    = t["symbol"]
        dir_   = t["direction"]
        entry  = t.get("entry_price")
        exit_  = t.get("exit_price")
        pnl    = t.get("pnl")   # may be None for an UNMATCHED close (opening fill pre-window)
        score  = t.get("score", "?")
        mri    = t.get("mri_level", "?")
        size   = t.get("size", 1)
        tier   = canonical_tier(t.get("tier", "swing"))
        tier_s = tier_label(tier)

        pm     = _load_postmortem(sym, t["exit_date"])
        tqi    = pm.get("tqi_score", "?")
        earn   = _had_earnings_during_hold(sym, t["entry_time"], t["exit_time"], earnings_cache)

        wkly   = friday_closes.get(sym, None)
        wkly_s = f"${wkly:.2f}" if wkly is not None else "?"

        delta_s = "?"
        if isinstance(exit_, (int, float)) and wkly is not None:
            delta = (wkly - exit_) if dir_ == "long" else (exit_ - wkly)
            delta_s = (f"+${delta:.2f}" if delta >= 0 else f"-${abs(delta):.2f}")
            if delta > 0:
                positive_friday_diagnostics += 1

        stage  = _compute_weinstein_stage(weekly_closes.get(sym, []))
        entry_s = f"${entry:.2f}"  if isinstance(entry, (int, float)) else "?"
        exit_s  = f"${exit_:.2f}" if isinstance(exit_, (int, float)) else "?"
        pnl_s   = ("?" if pnl is None else (f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"))
        dir_s   = "▲" if dir_ == "long" else "▼"
        earn_s  = "⚠️" if earn else "—"
        hold_s  = _hold_hours(t["entry_time"], t["exit_time"])
        r_s     = _r_multiple(entry, pnl, size, pm)
        rsn_s   = _shorten_reason(t.get("exit_reason", ""))

        if pnl is not None:
            total_pnl += pnl
        t.update({
            "_stage": stage, "_wkly": wkly, "_delta_s": delta_s,
            "_earn": earn, "_tqi": tqi, "_hold": hold_s, "_r": r_s,
            "_pm": pm,
        })

        full_rows.append(
            f"| {tier_s} | {sym} | {dir_s} | {entry_s} | {exit_s} | {pnl_s} | {rsn_s} | "
            f"{score} | {mri} | {earn_s} | {tqi} | {hold_s} | {r_s} | "
            f"{stage} | {wkly_s} | {delta_s} |"
        )
        # Mobile-friendly monospace row per trade. Drops the ambiguous long/short arrow (dir_s) that
        # read as a win/loss marker next to P&L; an empty exit-reason renders as "—" (not a dangling
        # "· ·"); rows are sorted worst→best on return so the biggest loss sits on top. The full 16-col
        # table (with direction) stays in the .md report + Gemini prompt. This is deliberately a
        # PER-SHARE Friday diagnostic, not dollars "left on the table": these rows mix horizons,
        # partial lots and positions whose runners remained open, so summing it creates a false total.
        _rsn = rsn_s or "—"
        _sk  = pnl if pnl is not None else float("inf")   # unmatched (P&L '?') sort last
        slack_rows.append(
            (_sk, f"{tier_s:<5} {sym:<5} {pnl_s:>8}  {_rsn:<12} "
                  f"{stage:<8} (Fri Δ/sh {delta_s})")
        )

    _accounting = accounting or {}
    _weekly_realized = _accounting.get("weekly_realized")
    if _weekly_realized is None:
        _weekly_realized = round(total_pnl, 2)
    stats = {
        "total_pnl":    round(float(_weekly_realized), 2),
        "completed_lifecycle_pnl": round(total_pnl, 2),
        "count":        len(trades),
        "winners":      sum(1 for t in trades if (t.get("pnl") or 0) > 0),
        "losers":       sum(1 for t in trades if (t.get("pnl") or 0) < 0),
        "earnings_cnt": sum(1 for t in trades if t.get("_earn")),
        "positive_friday_diagnostics": positive_friday_diagnostics,
        # rows whose OPENING fill predates the 90-day lookback → entry/P&L unknown ('?'),
        # excluded from total_pnl. Surfaced so the total is never silently understated.
        "unmatched":    sum(1 for t in trades if t.get("_unmatched")),
        "weekly_by_tier": _accounting.get("weekly_by_tier", {}),
        "partial_open_lifecycles": _accounting.get("partial_open_lifecycles", 0),
        "partial_open_weekly_pnl": _accounting.get("partial_open_weekly_pnl", 0.0),
        "partial_open_by_tier": _accounting.get("partial_open_by_tier", {}),
    }
    full_table = full_header + "\n".join(full_rows)
    slack_rows.sort(key=lambda r: r[0])   # worst (most negative P&L) first; unmatched ('?') last
    return full_table, "\n".join(row for _, row in slack_rows), stats

# ── Gemini ─────────────────────────────────────────────────────────────────────

def _build_gemini_prompt(week_str: str, full_table: str, trades: list[dict], stats: dict) -> str:
    details = "\n".join(
        f"  {tier_label(t.get('tier'))} | {t['symbol']} | {t['direction']} | entry={t.get('entry_price')} "
        f"exit={t.get('exit_price')} pnl={t.get('pnl')} size={t.get('size')} | "
        f"reason={t.get('exit_reason','')} | stage={t.get('_stage','')} | "
        f"earnings={t.get('_earn',False)} | mri={t.get('mri_level','')} | "
        f"score={t.get('score','')} | tqi={t.get('_tqi','')} | "
        f"hold={t.get('_hold','')} | wkly_close={t.get('_wkly','')} | Δ={t.get('_delta_s','')}"
        for t in trades
    )
    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    return f"""You are an adversarial performance auditor for a four-tier paper-trading
algorithmic bot running on Alpaca Markets. The tiers are Day, Swing, QHM, and F6.

Week reviewed: {week_str}
Weekly realized cash P&L: {pnl_sign}${stats['total_pnl']} | Completed lifecycles: {stats['count']} |
Winners: {stats['winners']} | Losers: {stats['losers']} |
Earnings-adjacent entries: {stats['earnings_cnt']} |
Positive Friday per-share diagnostics: {stats['positive_friday_diagnostics']}

═══════════════════════════════════════════
WEEKLY TRADE POST-MORTEM (WTP) TABLE
═══════════════════════════════════════════
{full_table}

Column definitions:
  Tier              — entry-order owner: Day, Swing, QHM, or F6
  Entry$ / Exit$   — actual fill prices from Alpaca fills API (authoritative)
  Lifecycle P&L    — dollars across every partial close in the fully closed entry lifecycle
  Exit Reason      — bot's internal exit classification (shortened)
  Score            — 12-pt confluence score at entry (9+ = valid)
  MRI              — Macro Risk Index at entry (NORMAL/ELEVATED/STRESSED/HIGH/CRITICAL)
  Earnings ⚠️      — FMP earnings event occurred during hold period
  TQI              — Trade Quality Index 0-100 (target=35pts, trail=28, ATR buffer=10, hard_stop=0)
  Hold             — Duration from entry to exit
  R-Mult           — P&L per share / initial stop risk (positive = profit)
  Stage            — Weinstein Stage from 20-week SMA (2=advance, 3=top, 4=decline)
  Wkly Close       — Friday closing price (where stock ended the week)
  Δ/share Exit→Fri — Per-share directional difference between the exit and Friday close.
                     DIAGNOSTIC ONLY: it is not quantity-adjusted opportunity cost and must not be
                     summed. Rows can mix same-day and multi-day horizons, partial exits, and open
                     runners. A positive value does not prove the exit was wrong.

The weekly realized cash total is based only on close fills booked this week. A lifecycle row may
include a partial close from an earlier week, so lifecycle rows do not have to sum to weekly cash.
Open lifecycles with partial exits contribute weekly cash but are excluded from W/L. Missing fields
shown as '?' are UNKNOWN, never zero. Do not infer a failed score gate from missing metadata.

═══════════════════════════════════════════
TRADE DETAIL (raw)
═══════════════════════════════════════════
{details}

═══════════════════════════════════════════
YOUR TASK — adversarial performance audit
═══════════════════════════════════════════
1. PATTERN ANALYSIS (3–5 bullets)
   Which exit mechanisms deserve replay based on repeated positive per-share Friday diagnostics?
   Are earnings-adjacent entries concentrated in a specific direction or stage?
   Any MRI/Score combinations that consistently underperformed?

2. SYSTEMIC FINDINGS (ranked by strength of evidence, minimum 3)
   For each: [mechanism] → [observable failure] → [evidence]. Do not invent dollar opportunity cost.

3. EXECUTION QUALITY SCORE (1–10)
   Grade execution separately from signal quality. Two-sentence justification.

4. DS/GAI AUDIT PREP (paste-ready)
   Top 3 code changes with:
   [PRIORITY] File: function_name() — Change: description — Evidence and required validation.

5. ONE-SENTENCE VERDICT
   Signal problem, execution problem, or both?

Be specific. Cite exact exit reasons and dollar amounts. Do not fabricate.
If a field shows '?' note it and continue."""


def _call_gemini(prompt: str) -> str:
    # gemini-3.1-flash-lite needs max_output_tokens + thinking_budget=0, or "thinking" eats the
    # output budget and response.text comes back empty/truncated (project-documented Gemini
    # gotcha) — the silent cause of a blank/half "adversarial analysis" section here. The prior
    # fallback gemini-2.0-flash-lite is now 404/retired; gemini-3.1-flash-lite is the working
    # replacement (both verified live 2026-07-24 in weekly_review.py, the sibling fix #6/PR #8).
    try:
        from google import genai
        from google.genai import types as _gtypes
        client   = genai.Client(api_key=GEMINI_API_KEY)
        cfg      = _gtypes.GenerateContentConfig(
            max_output_tokens=8192,
            thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
        )
        last_err = None
        for model in GAI_MODEL_LADDER:   # single-source live ladder (was [primary, gemini-flash-latest])
            try:
                logger.info(f"  Trying model: {model}")
                r   = client.models.generate_content(model=model, contents=prompt, config=cfg)
                txt = (r.text or "").strip()
                if not txt:
                    # Empty text (thinking consumed the budget, or a truncated/blocked response)
                    # → treat as a failure so we fall through to the next model instead of
                    # writing an empty/"None" analysis section into the report.
                    raise ValueError("empty response.text (no content returned)")
                logger.info(f"  Success: {model}")
                return txt
            except Exception as e:
                last_err = e
                logger.warning(f"  {model} failed: {e}")
        return f"_(Gemini adversarial analysis unavailable this run — all models failed. Last error: {last_err})_"
    except ImportError:
        return "_(google-genai not installed — adversarial analysis skipped)_"
    except Exception as e:
        # Broad catch (mirrors weekly_review.py): a Client()/config-construction error (e.g. an SDK
        # build lacking ThinkingConfig, or an auth error) must degrade to a visible notice — NEVER
        # propagate out of _call_gemini and abort the whole report + Slack post in main().
        return f"_(Gemini adversarial analysis unavailable this run — client/setup error: {e})_"

# ── Slack ──────────────────────────────────────────────────────────────────────

def _slack_raw(text: str) -> None:
    """POST a raw text message to Slack. Best-effort; logs on failure, never raises."""
    if not SLACK_WEBHOOK:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack.")
        return
    try:
        data = json.dumps({"text": text}).encode()
        req  = urllib.request.Request(
            SLACK_WEBHOOK, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as r:
            logger.info(f"Slack: HTTP {r.status}")
    except Exception as e:
        logger.warning(f"Slack post failed: {e}")


def _post_slack(slack_lines: str, stats: dict, report_path: Path, week_str: str,
                fallback_note: str = "") -> None:
    if not SLACK_WEBHOOK:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack.")
        return
    _pnl_disp = (f"+${stats['total_pnl']:.2f}" if stats["total_pnl"] >= 0
                 else f"-${abs(stats['total_pnl']):.2f}")   # -$36.67, matching the per-row style
    _unm = f"  ·  unmatched (pre-window) `{stats['unmatched']}`" if stats.get("unmatched") else ""
    # A non-authoritative (fills-fetch-failed) run leads with a loud warning so the operator never
    # reads the fallback tracker estimate as Alpaca-FIFO truth (data-integrity board T1).
    _warn = (":warning: *fills fetch INCOMPLETE — figures are the trade_events estimate, NOT "
             "authoritative Alpaca-FIFO P&L; investigate.*\n") if fallback_note else ""
    # Earnings-adjacent only surfaces when non-zero (0 was pure noise). Per-trade rows go in a
    # monospace code block (aligned columns, worst→best) so they never render as wrapped soup.
    _earn_seg = f"  ·  earnings-adj `{stats['earnings_cnt']}`" if stats.get("earnings_cnt") else ""
    _tier_bits = []
    for _tier in TIER_IDS:
        _value = float((stats.get("weekly_by_tier") or {}).get(_tier, 0.0))
        _tier_bits.append(f"{tier_label(_tier)} `{_value:+.2f}`")
    _tier_line = " · ".join(_tier_bits)
    _partial = ""
    if stats.get("partial_open_lifecycles"):
        _partial = (
            f"\nOpen partials `{stats['partial_open_lifecycles']}` · realized this week "
            f"`${stats['partial_open_weekly_pnl']:+.2f}` · excluded from W/L"
        )
    _rows = f"_worst → best_\n```\n{slack_lines}\n```\n" if slack_lines.strip() else ""
    msg = (
        _warn
        + f":bar_chart: *Weekly Post-Mortem — {week_str}*\n"
        f"Realized cash (Alpaca-FIFO): `{_pnl_disp}`  ·  "
        f"completed W/L `{stats['winners']}/{stats['losers']}`  ·  "
        f"Fri-positive diagnostics `{stats['positive_friday_diagnostics']}`{_earn_seg}{_unm}\n"
        f"{_tier_line}{_partial}\n"
        f"{_rows}"
        f"_Full 16-col table + Gemini analysis: `logs/{report_path.name}`_"
    )
    _slack_raw(msg)

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    monday, friday = _get_week_range()
    week_str       = f"{monday.isoformat()} → {friday.isoformat()}"
    report_path    = LOGS_DIR / f"wtp_{friday.isoformat()}.md"

    # Saturday succeeded → Sunday skips
    if report_path.exists():
        logger.info(f"WTP already exists for {friday.isoformat()} — nothing to do.")
        return

    logger.info(f"=== Weekly Trade Post-Mortem: {week_str} ===")

    # Same-direction co-holds make symbol/date FIFO unsafe. One canonical fills+orders snapshot
    # supplies both realized P&L and exact entry-tier ownership. If the join is incomplete, publish
    # no figures: an unattributed dollar must never be guessed into one of the four tiers.
    logger.info("Building canonical fills+orders FIFO snapshot...")
    from reporting.report_figures import build_report_figures
    figures = build_report_figures()
    integrity_errors = _canonical_integrity_errors(figures)
    if integrity_errors:
        reason = "; ".join(integrity_errors)
        report_path.write_text(
            f"# Weekly Trade Post-Mortem — {week_str}\n\n"
            f"> **UNAVAILABLE:** Canonical tier accounting failed closed: {reason}.\n",
            encoding="utf-8",
        )
        _slack_raw(
            f":rotating_light: *Weekly Post-Mortem unavailable — {week_str}*\n"
            f"Canonical tier accounting failed closed: {reason}. No P&L or W/L was published."
        )
        logger.error("WTP unavailable: %s", reason)
        return

    try:
        trades, accounting = _week_ledger_view(figures.round_trips, monday, friday)
    except (TypeError, ValueError, KeyError) as exc:
        report_path.write_text(
            f"# Weekly Trade Post-Mortem — {week_str}\n\n"
            f"> **UNAVAILABLE:** Canonical tier/lifecycle accounting failed: {exc}.\n",
            encoding="utf-8",
        )
        _slack_raw(
            f":rotating_light: *Weekly Post-Mortem unavailable — {week_str}*\n"
            f"Canonical tier/lifecycle accounting failed: {exc}. No metrics were published."
        )
        logger.error("WTP tier/lifecycle accounting failed: %s", exc)
        return
    fallback_note = ""
    _enrich_metadata_from_events(trades, monday, friday)
    logger.info(
        "change=weekly-postmortem-tier-split Canonical WTP: %d completed lifecycle(s), "
        "weekly realized $%.2f, %d open partial(s).",
        len(trades), accounting["weekly_realized"], accounting["partial_open_lifecycles"],
    )

    if not trades and not accounting["partial_open_lifecycles"] and not accounting["weekly_realized"]:
        logger.warning(f"No closed trades found for {week_str}.")
        report_path.write_text(f"# WTP {week_str}\n\nNo realized closes this week.\n")
        return

    symbols = sorted({t["symbol"] for t in trades})
    logger.info(f"Symbols: {symbols}")

    logger.info("Fetching weekly bars (Alpaca T1)...")
    weekly_closes  = _fetch_weekly_closes(symbols)

    logger.info("Fetching Friday closes (Alpaca T1)...")
    friday_closes  = _fetch_friday_close(symbols, friday)

    logger.info("Loading earnings cache...")
    earnings_cache = _load_earnings_cache()

    logger.info("Building WTP table...")
    full_table, slack_lines, stats = _build_wtp_table(
        trades, friday_closes, weekly_closes, earnings_cache, accounting
    )

    if GEMINI_API_KEY:
        logger.info("Calling Gemini for adversarial analysis...")
        prompt        = _build_gemini_prompt(week_str, full_table, trades, stats)
        gemini_report = _call_gemini(prompt)
    else:
        logger.warning("GEMINI_API_KEY not set — no Gemini analysis.")
        gemini_report = "(GEMINI_API_KEY not configured — skipped)"

    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    ts_pt    = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")

    _pnl_label = "canonical Alpaca fills+orders FIFO"
    _sources_ln = (
        "_Sources: **one canonical Alpaca fills+orders FIFO snapshot** "
        "(authoritative tier/Entry$/Exit$/P&L) · Alpaca T1 bars · FMP earnings cache · "
        "trade\\_events.jsonl (score/MRI/reason metadata only)_"
    )
    report = "\n".join([
        f"# Weekly Trade Post-Mortem — {week_str}",
        f"_Generated {ts_pt} | {GEMINI_MODEL}_",
        (f"\n> {fallback_note}\n" if fallback_note else ""),
        "",
        "## Summary",
        f"- **Weekly realized cash P&L ({_pnl_label}):** `{pnl_sign}${stats['total_pnl']}`",
        "- **By entry tier:** " + " · ".join(
            f"{tier_label(tier)} `{stats['weekly_by_tier'].get(tier, 0.0):+.2f}`"
            for tier in TIER_IDS
        ),
        f"- **Completed lifecycles:** {stats['count']} ({stats['winners']}W / {stats['losers']}L); "
        f"full-lifecycle P&L `{stats['completed_lifecycle_pnl']:+.2f}`",
        f"- **Open partial lifecycles:** {stats['partial_open_lifecycles']}; realized this week "
        f"`{stats['partial_open_weekly_pnl']:+.2f}` (included in cash P&L, excluded from W/L)",
        f"- **Earnings-adjacent entries:** {stats['earnings_cnt']}",
        (f"- **Friday comparison:** {stats['positive_friday_diagnostics']} positive per-share diagnostic(s); "
         "not summed into dollars because the rows mix horizons, partial exits, and retained runners."),
        "",
        "## Completed Lifecycle Overview",
        ("_* Δ/share Exit→Fri is a per-share diagnostic only. It is not quantity-adjusted dollars "
         "left on the table, and a positive value does not by itself prove an exit error._"),
        full_table,
        "",
        "---",
        "## Gemini Adversarial Analysis",
        gemini_report,
        "",
        "---",
        _sources_ln,
    ])

    report_path.write_text(report, encoding="utf-8")
    logger.info(f"WTP written → {report_path}")

    _post_slack(slack_lines, stats, report_path, week_str, fallback_note)
    logger.info("Done.")


if __name__ == "__main__":
    if not ALPACA_KEY:
        logger.error("ALPACA_API_KEY not set — aborting.")
        sys.exit(1)
    main()
