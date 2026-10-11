#!/usr/bin/env python3
# ruff: noqa: E501  — dense rationale comments run long (project convention)
"""
scripts/cohold_claim_eod.py — end-of-day flag for an unreadable Day share count on a co-held stock.

Design record: logs/design_records/cohold_unreadable_claim_retry_2026-10-11.md (board Harris/Taleb + GAI). When a
non-Day tier closes a co-held stock and the Day tier's share count cannot be read, execution/broker.py retries, sells
only the tier's own shares, and writes a `cohold_claim_unreadable` event to logs/trade_events.jsonl. This job reads
today's (ET) events and posts one Slack summary per symbol (first/last time, events, retries, outcomes) when any
exist. It always logs one line, so a quiet day is visible too.

Data: local file only (logs/trade_events.jsonl) — no API calls, no execution imports.
Cadence: OCI cron weekdays 8:15 PM ET (cron_tz_wrapper), after the after-hours session, so after-hours closes count. Failure: stderr + exit 1; no effect on trading.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "logs" / "trade_events.jsonl"
ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
TAG = "[cohold-claim-eod]"


def summarize(lines, day: str) -> dict:
    """Group today's `cohold_claim_unreadable` events by symbol. `day` is the ET date (YYYY-MM-DD)."""
    out: dict = defaultdict(lambda: {"events": 0, "attempts": 0, "outcomes": defaultdict(int), "first": None,
                                     "last": None, "tiers": set()})
    for raw in lines:
        try:
            e = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(e, dict) or e.get("event") != "cohold_claim_unreadable":
            continue
        try:
            ts = datetime.fromisoformat(str(e.get("ts")))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=PT)
        if ts.astimezone(ET).date().isoformat() != day:
            continue
        r = out[str(e.get("symbol") or "?")]
        r["events"] += 1
        r["attempts"] += int(e.get("attempts") or 0)
        r["outcomes"][str(e.get("outcome") or "?")] += 1
        r["tiers"].add(str(e.get("tier") or "?"))
        r["first"] = ts if r["first"] is None or ts < r["first"] else r["first"]
        r["last"] = ts if r["last"] is None or ts > r["last"] else r["last"]
    return dict(out)


def _fmt(ts) -> str:
    return ts.astimezone(PT).strftime("%I:%M %p PT") if ts is not None else "?"


def main() -> int:
    day = datetime.now(ET).date().isoformat()
    try:
        lines = EVENTS.read_text(encoding="utf-8").splitlines() if EVENTS.exists() else []
    except OSError as e:
        print(f"{TAG} {day}: trade_events.jsonl unreadable: {e}", file=sys.stderr)
        return 1
    s = summarize(lines, day)
    if not s:
        print(f"{TAG} {day}: no unreadable Day share counts today")
        return 0
    rows = []
    for sym, r in sorted(s.items()):
        oc = ", ".join(f"{k} x{v}" for k, v in sorted(r["outcomes"].items()))
        rows.append(f"• {sym} ({'/'.join(sorted(r['tiers']))}): {r['events']} close(s), {r['attempts']} reads, "
                    f"{_fmt(r['first'])}–{_fmt(r['last'])} — {oc}")
    msg = (f":warning: {TAG} {day}: the Day tier's share count was unreadable during another tier's close today. "
           f"Check that no stray Day stop or unprotected shares remain:\n" + "\n".join(rows))
    print(msg)
    try:
        sys.path.insert(0, str(ROOT))
        from alerts import send_slack
        send_slack(msg)
    except Exception as e:  # noqa: BLE001 — the printed line above is the record
        print(f"{TAG} Slack send failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
