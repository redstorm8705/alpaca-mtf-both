#!/usr/bin/env python3
# ruff: noqa: E501,E402  — E501: Slack mrkdwn literals; E402: imports follow the sys.path/.env bootstrap (cron self-auth)
"""
scripts/pnl_snapshot.py — hourly per-tier UNREALIZED P&L snapshot to Slack (READ-ONLY).

Rafael 2026-09-04: "an hourly Slack notification that gives me a snapshot of the P/L for the
day, similar to what Alpaca is showing — a snapshot of how each tier is performing (their daily
P/L at that moment). I want the snapshot so it can be UNREALIZED. At market close and after the
official reconcile, we can have it be realized P/L."

TWO MODES (one file, one cron each):
  - default (hourly RTH): UNREALIZED per tier — open positions marked to market now.
  - `--realized` (once, post-close after the reconcile): REALIZED per tier — Alpaca-FIFO P&L
    from round-trips CLOSED today, attributed by client_order_id tier tag. Rafael 2026-09-04:
    "at market close and after the official reconcile, we can have it be realized P/L."

WHAT IT POSTS (compact Block Kit card):
  - Reference headline: account day P&L = equity − last_equity (exactly Alpaca's day number) + % + equity.
  - Per tier (intraday / qhm / forever6 / daytrade): today's UNREALIZED P&L =
        this tier's ownership-split of each open position's unrealized_intraday_pl (today's MTM move),
    with a per-position "today $" line under each tier that holds something.
  - "Other / untracked": the unrealized on any shares not tier-tagged in the ownership ledger,
    so the per-tier lines + Other always sum EXACTLY to the total open unrealized today.
  - Footer: notes it is UNREALIZED (marked to market now); realized finalizes in the post-close reconcile.

SOURCING (per the P&L SOURCING RULE — Alpaca-authoritative, never tracker math):
  - equity / last_equity / positions (current_price, lastday_price, unrealized_intraday_pl, qty) :
        Alpaca REST via reporting.pnl_ledger.
  - per-tier qty (to split each position's today-MTM) : the ownership ledger
        (execution.ownership_guard) positions[symbol].tiers[tier].qty — exact, not proportional.

READ-ONLY: fetches Alpaca + reads the ownership ledger + posts one Slack card + writes one log
line. It submits no orders and mutates no state. Runs on the RTH-hour cron.
"""
from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:                                             # self-auth under cron (matches sync_reports.py / run_day_tier_shadow.py)
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_ROOT / ".env")
except Exception as _e:                          # dotenv optional / already-exported env — never fatal
    logging.getLogger("pnl_snapshot").debug("dotenv load skipped: %s", _e)

from reporting import pnl_ledger as pl          # Alpaca REST wrappers (fetch_account / fetch_positions)
from execution import ownership_guard as og      # per-tier qty ledger (load_ledger)
from scripts import audit_slack                   # Block Kit post_to_slack

PT = ZoneInfo("America/Los_Angeles")
_TIERS = ("intraday", "qhm", "forever6", "daytrade")
_TIER_LABEL = {"intraday": "Intraday", "qhm": "QHM", "forever6": "Forever-6", "daytrade": "Day-Trade"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("pnl_snapshot")


def _dollar(v: float) -> str:
    s = f"${abs(v):,.2f}"
    return f"−{s}" if v < 0 else s


def compute_snapshot() -> dict:
    """Pure computation — fetch Alpaca + the ownership ledger and return the UNREALIZED snapshot.
    Degrades gracefully: an unreadable ledger leaves MTM in the 'Other' residual, which still
    sums to the total open unrealized today."""
    account = pl.fetch_account() or {}
    equity = float(account.get("equity", 0.0) or 0.0)
    last_equity = float(account.get("last_equity", equity) or 0.0)
    account_today = round(equity - last_equity, 2)
    account_pct = round(account_today / last_equity * 100, 2) if last_equity else 0.0

    positions = pl.fetch_positions() or []

    try:
        ledger = og.load_ledger() or {}
    except Exception as e:                         # corrupt/unreadable ledger → MTM falls to the Other residual
        logger.warning("ownership ledger unreadable (%s) — unrealized falls to Other", e)
        ledger = {}
    led_positions = ledger.get("positions", {}) if isinstance(ledger, dict) else {}
    if not isinstance(led_positions, dict):        # defensive: a non-dict "positions" would break .get() below
        logger.warning("pnl_snapshot: ownership ledger 'positions' is not a dict — treating as empty (all MTM → Other)")
        led_positions = {}

    tier_unreal = {t: 0.0 for t in _TIERS}
    pos_lines: dict[str, list] = {t: [] for t in _TIERS}   # per-tier [(symbol, today unrealized $)]
    untracked_syms: list = []                              # [(symbol, unattributed unrealized $)]
    total_unreal_today = 0.0
    for p in positions:
        sym = p.get("symbol", "")
        try:
            pos_qty = abs(float(p.get("qty", 0.0) or 0.0))
            intraday_pl = float(p.get("unrealized_intraday_pl", 0.0) or 0.0)
        except (TypeError, ValueError):
            logger.warning("pnl_snapshot: skipping position with unparseable qty/unrealized_intraday_pl: %s", p.get("symbol", "?"))
            continue
        if not sym or pos_qty <= 0:
            continue
        total_unreal_today += intraday_pl
        sym_entry = led_positions.get(sym)
        tiers = (sym_entry or {}).get("tiers", {}) if isinstance(sym_entry, dict) else {}
        attributed_qty = 0.0
        for t in _TIERS:
            try:
                tq = abs(float((tiers.get(t, {}) or {}).get("qty", 0.0) or 0.0))
            except (TypeError, ValueError):
                logger.debug("pnl_snapshot: ledger tier qty unparseable for %s/%s — treated as 0 (routes to Other)", sym, t)
                tq = 0.0
            if tq <= 0:
                continue
            eff = min(tq, pos_qty - attributed_qty)         # never over-allocate past the live qty
            if eff <= 0:
                continue
            share = eff / pos_qty                           # exact split (ledger tracks per-tier qty)
            tier_pos_today = round(share * intraday_pl, 2)
            tier_unreal[t] += tier_pos_today
            pos_lines[t].append((sym, tier_pos_today))
            attributed_qty += eff
        unattr_qty = pos_qty - attributed_qty
        if unattr_qty > 1e-6 and abs(intraday_pl) >= 0.005:
            untracked_syms.append((sym, round(unattr_qty / pos_qty * intraday_pl, 2)))

    tier_unreal = {t: round(tier_unreal[t], 2) for t in _TIERS}
    tier_sum = round(sum(tier_unreal.values()), 2)
    total_unreal_today = round(total_unreal_today, 2)
    other_today = round(total_unreal_today - tier_sum, 2)   # unrealized on untagged shares; sums exactly

    return {
        "equity": equity,
        "account_today": account_today,
        "account_pct": account_pct,
        "tier_unreal": tier_unreal,
        "pos_lines": pos_lines,
        "total_unreal_today": total_unreal_today,
        "other_today": other_today,
        "untracked_syms": untracked_syms,
        "n_positions": len(positions),
    }


def _tier_rows(tier_vals: dict, pos_lines: dict, empty_word: str) -> list:
    """Compact per-tier rows: active tiers show name+total then inline positions; tiers with
    nothing are collapsed into a single dim line. Shared by the unrealized + realized cards."""
    rows: list = []
    flat: list = []
    for t in _TIERS:
        lines = pos_lines.get(t, [])
        if lines:
            pos = " · ".join(f"{sym} {_dollar(pv)}" for sym, pv in sorted(lines, key=lambda x: x[1]))
            rows.append(f"*{_TIER_LABEL[t]}*  {_dollar(tier_vals[t])}\n{pos}")
        else:
            flat.append(_TIER_LABEL[t])
    if flat:
        rows.append(f"_{' · '.join(flat)}: {empty_word}_")
    return rows


def build_card(s: dict) -> dict:
    """Render the UNREALIZED snapshot as a compact Slack Block Kit payload (no equity, inline
    positions, empty tiers collapsed)."""
    now_pt = datetime.now(PT).strftime("%-I:%M %p PT")
    sign_pct = f"{s['account_pct']:+.2f}%"
    rows = [f"*Unrealized by tier*  ·  {_dollar(s['total_unreal_today'])}"]
    rows += _tier_rows(s["tier_unreal"], s["pos_lines"], "flat")
    if abs(s["other_today"]) >= 0.50:
        rows.append(f"_Other {_dollar(s['other_today'])}_")
    blocks: list = [
        {"type": "header", "text": {"type": "plain_text", "text": f"💹 P&L Snapshot · {now_pt}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Today  {_dollar(s['account_today'])}  ({sign_pct})*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "open positions, marked to market · realized after close"}]},
    ]
    fallback = f"P&L {now_pt}: today {_dollar(s['account_today'])} ({sign_pct}), unrealized {_dollar(s['total_unreal_today'])}"
    return {"blocks": blocks, "text": fallback}


# ── REALIZED mode (post-close) ────────────────────────────────────────────────

def _today_pt() -> str:
    """PT calendar date, matching reporting.pnl_ledger._pt_date's per_day keys."""
    return datetime.now(PT).strftime("%Y-%m-%d")


def _realized_by_tier(fills: list, coid_map: dict, today: str) -> tuple[dict, dict, float]:
    """One unified FIFO per symbol where each LOT carries its entry tier; a close books each
    matched lot's realized P&L to THAT lot's tier. Correct across cross-tier closes (a full-net
    sell matches an intraday lot and a daytrade lot and books each to its own tier) with NO phantom-
    lot corruption and NO reliance on entry-timestamp uniqueness. Mirrors
    reporting.pnl_ledger.compute_realized's FIFO (same net-based cover-first logic), tier-tagged.
    Returns (tier_today, tier_syms_today, total_today) for exit_date == today only."""
    lots: dict = defaultdict(deque)          # sym -> deque[(qty, price, side, tier)]
    tier_today = {t: 0.0 for t in _TIERS}
    tier_syms: dict = {t: defaultdict(float) for t in _TIERS}
    total_today = 0.0

    def _close(sym: str, want_side: str, qty: int, price: float, day: str) -> int:
        nonlocal total_today
        dq = lots[sym]
        rem = qty
        while rem > 0 and dq and dq[0][2] == want_side:
            lqty, lprice, lside, ltier = dq[0]
            take = min(lqty, rem)
            pnl = (price - lprice) * take if want_side == "long" else (lprice - price) * take
            if day == today:
                tier_today[ltier] += pnl
                tier_syms[ltier][sym] += pnl
                total_today += pnl
            if lqty == take:
                dq.popleft()
            else:
                dq[0] = (lqty - take, lprice, lside, ltier)
            rem -= take
        return rem

    for f in fills:
        sym = f.get("symbol", "")
        side = f.get("side", "")                       # byte-identical to compute_realized (no .lower())
        try:
            qty = int(float(f.get("qty", 0)))
            price = float(f.get("price", 0.0))
        except (TypeError, ValueError):
            continue
        day = pl._pt_date(f.get("transaction_time", ""))   # identical bucketing to compute_realized
        if not sym or qty <= 0 or not day:
            continue
        tier = og.tier_of_coid(coid_map.get(f.get("order_id"))) or "intraday"
        if tier not in _TIERS:
            tier = "intraday"
        dq = lots[sym]
        net = sum(q * (1 if sd == "long" else -1) for q, _p, sd, _tr in dq)
        if side in ("buy", "buy_to_cover"):
            if net < 0:
                leftover = _close(sym, "short", qty, price, day)
                if leftover > 0:
                    dq.append((leftover, price, "long", tier))
            else:
                dq.append((qty, price, "long", tier))
        elif side in ("sell", "sell_short"):
            if net > 0:
                leftover = _close(sym, "long", qty, price, day)
                if leftover > 0:
                    dq.append((leftover, price, "short", tier))
            else:
                dq.append((qty, price, "short", tier))
    return tier_today, tier_syms, round(total_today, 2)


def compute_realized_snapshot() -> dict:
    """Post-close REALIZED P&L per tier from Alpaca-FIFO trades CLOSED today.

    The TOTAL comes from the audited reporting.pnl_ledger.compute_realized (authoritative). The
    per-tier SPLIT comes from _realized_by_tier — a unified FIFO whose lots carry their entry tier,
    so a single full-net close of a symbol co-held by two non-protected tiers (intraday+daytrade;
    only forever6/qhm are ring-fenced) books each tier's shares to that tier exactly. This avoids
    both the per-tier SUBSET-FIFO phantom-lot corruption and the entry-timestamp-collision failure
    (cold-2nd 2026-09-04). `unattributed` = authoritative total − Σ tiers; it is ~0 by construction
    (same FIFO), and any real divergence is logged + surfaced rather than showing a wrong split."""
    today = _today_pt()
    account = pl.fetch_account() or {}
    equity = float(account.get("equity", 0.0) or 0.0)
    last_equity = float(account.get("last_equity", equity) or 0.0)
    account_today = round(equity - last_equity, 2)
    account_pct = round(account_today / last_equity * 100, 2) if last_equity else 0.0

    fills = pl.fetch_all_fills() or []
    orders = pl.fetch_all_orders() or []
    coid_map = pl.build_coid_map(orders)              # {order_id: client_order_id}

    full = pl.compute_realized(fills)                 # authoritative TOTAL
    total_realized = round(float(full.get("per_day", {}).get(today, 0.0) or 0.0), 2)

    tier_raw, tier_syms, _tier_total = _realized_by_tier(fills, coid_map, today)   # per-tier SPLIT
    tier_realized = {t: round(tier_raw[t], 2) for t in _TIERS}
    pos_lines: dict[str, list] = {
        t: [(s, round(v, 2)) for s, v in tier_syms[t].items() if abs(v) >= 0.005] for t in _TIERS
    }
    unattributed = round(total_realized - round(sum(tier_realized.values()), 2), 2)
    if abs(unattributed) >= 0.50:                     # tier-FIFO diverged from the audited total → surface, don't hide
        logger.warning("pnl_snapshot realized: per-tier split %.2f != authoritative total %.2f (residual %.2f)",
                       sum(tier_realized.values()), total_realized, unattributed)
    return {
        "equity": equity,
        "account_today": account_today,
        "account_pct": account_pct,
        "tier_realized": tier_realized,
        "pos_lines": pos_lines,
        "total_realized": total_realized,
        "unattributed": unattributed,
    }


def build_realized_card(s: dict) -> dict:
    """Render the REALIZED snapshot as a compact Slack Block Kit payload (no equity, inline
    positions, empty tiers collapsed)."""
    now_pt = datetime.now(PT).strftime("%-I:%M %p PT")
    sign_pct = f"{s['account_pct']:+.2f}%"
    rows = ["*Realized by tier*"]
    rows += _tier_rows(s["tier_realized"], s["pos_lines"], "none")
    if abs(s.get("unattributed", 0.0)) >= 0.50:      # rounding/edge guard — should not normally render
        rows.append(f"_Unattributed {_dollar(s['unattributed'])}_")
    blocks: list = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📕 Realized · Close · {now_pt}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Realized today  {_dollar(s['total_realized'])}*   ·   day {_dollar(s['account_today'])} ({sign_pct})"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(rows)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "closed-trade P&L, Alpaca FIFO · booked to the opening tier"}]},
    ]
    fallback = f"Realized {now_pt}: realized {_dollar(s['total_realized'])}, day {_dollar(s['account_today'])}"
    return {"blocks": blocks, "text": fallback}


# ── Slack post + entrypoint ────────────────────────────────────────────────────

def _post(payload: dict, label: str) -> int:
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        logger.error("SLACK_WEBHOOK_URL not set — %s computed but not posted", label)
        logger.info("%s: %s", label, payload["text"])
        return 1
    try:
        status = audit_slack.post_to_slack(payload, webhook)
        logger.info("posted %s (HTTP %s): %s", label, status, payload["text"])
        return 0
    except Exception as e:
        logger.error("Slack post failed (%s): %s", label, e)
        return 1


def main() -> int:
    realized = "--realized" in sys.argv[1:]
    try:
        if realized:
            payload = build_realized_card(compute_realized_snapshot())
        else:
            payload = build_card(compute_snapshot())
    except Exception as e:
        logger.error("snapshot computation/render failed — NOT posting (no wrong number): %s", e)
        return 1
    return _post(payload, "realized P&L snapshot" if realized else "P&L snapshot")


if __name__ == "__main__":
    raise SystemExit(main())
