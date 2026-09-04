#!/usr/bin/env python3
# ruff: noqa: E501,E402  — E501: Slack mrkdwn literals; E402: imports follow the sys.path/.env bootstrap (cron self-auth)
"""
scripts/pnl_snapshot.py — hourly per-tier UNREALIZED P&L snapshot to Slack (READ-ONLY).

Rafael 2026-09-04: "an hourly Slack notification that gives me a snapshot of the P/L for the
day, similar to what Alpaca is showing — a snapshot of how each tier is performing (their daily
P/L at that moment). I want the snapshot so it can be UNREALIZED. At market close and after the
official reconcile, we can have it be realized P/L."

So the HOURLY card is UNREALIZED (open positions marked to market now), broken out per tier.
A realized per-tier version is a separate post-close job (fast-follow, tracked in the design
record) — realized-per-tier attribution belongs after the authoritative reconcile, not intraday.

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


def build_card(s: dict) -> dict:
    """Render the snapshot dict as a Slack Block Kit payload {"blocks":[...], "text": fallback}."""
    now_pt = datetime.now(PT).strftime("%-I:%M %p PT")
    sign_pct = f"{s['account_pct']:+.2f}%"
    blocks: list = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"💹 P&L Snapshot · {now_pt}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Today (Alpaca):* {_dollar(s['account_today'])}  ({sign_pct})   ·   *Equity:* {_dollar(s['equity'])}"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "Account day P&L = Alpaca `equity − last_equity`. Per-tier below is *unrealized* (open positions, marked to market now)."}]},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Unrealized today — by tier*   (total {_dollar(s['total_unreal_today'])})"}},
    ]
    for t in _TIERS:
        v = s["tier_unreal"][t]
        lines = s["pos_lines"][t]
        head = f"*{_TIER_LABEL[t]}*   {_dollar(v)}"
        if lines:
            body = "\n".join(f"• {sym}  {_dollar(pv)}" for sym, pv in sorted(lines, key=lambda x: x[1]))
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n{body}"}})
        else:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{head}   _(no open positions)_"}})
    other_v = s["other_today"]
    if abs(other_v) >= 0.50:                        # suppress sub-dollar rounding residue; show a real untracked amount
        unsyms = s.get("untracked_syms", [])
        names = ", ".join(sym for sym, _v in sorted(unsyms, key=lambda x: x[1])[:8]) if unsyms else ""
        note = f"\n_{len(unsyms)} position(s) not tier-tagged: {names}_" if names else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Other / untracked*   {_dollar(other_v)}{note}"}})
    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "Unrealized (open-position) P&L, marked to market now — realized P&L finalizes in the post-close reconcile. Per-tier split from the ownership ledger; the account headline is authoritative."}]})

    fallback = f"P&L Snapshot {now_pt}: today {_dollar(s['account_today'])} ({sign_pct}), open unrealized {_dollar(s['total_unreal_today'])}, equity {_dollar(s['equity'])}"
    return {"blocks": blocks, "text": fallback}


def main() -> int:
    try:
        s = compute_snapshot()
        payload = build_card(s)
    except Exception as e:
        logger.error("snapshot computation/render failed — NOT posting (no wrong number): %s", e)
        return 1
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        logger.error("SLACK_WEBHOOK_URL not set — snapshot computed but not posted")
        logger.info("snapshot: %s", payload["text"])
        return 1
    try:
        status = audit_slack.post_to_slack(payload, webhook)
        logger.info("posted P&L snapshot (HTTP %s): %s", status, payload["text"])
        return 0
    except Exception as e:
        logger.error("Slack post failed: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
