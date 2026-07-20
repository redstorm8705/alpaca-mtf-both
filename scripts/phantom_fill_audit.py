#!/usr/bin/env python3
"""Quantify RC-4 phantom-fill contamination in trade_log.json closed[].

BACKGROUND
----------
CLAUDE.md records RC-4 as "found+fixed 2026-07-03": fetch_actual_fill_price() with
submitted_after=None matched months-old fills as today's close, writing exit prices
that never happened (documented: TSLA ~$347, PANW -$182.79). The patch stopped NEW
phantoms being written. It did NOT clean rows already on disk. Those rows still feed
every all-time stat computed from closed[] — win rate, profit factor, max drawdown.

WHAT THIS DOES
--------------
For every closed trade, pull the underlying's ACTUAL Alpaca FILL activities on that
trade's exit date and check whether the recorded exit_price matches any real fill.
Alpaca fills are authoritative per the project's P&L Sourcing Rule; trade_log math
is a cross-check only.

Classification per row:
  OK          - recorded exit price matches a real fill that day (within tolerance)
  PHANTOM     - fills exist for that symbol/date, but NONE match the recorded exit price
  NO_FILLS    - no fills at all for that symbol on that date (exit price unverifiable)
  NO_EXIT_TS  - row has no exit_time; cannot be checked (the same data defect that
                also corrupts max-drawdown ordering at weekly_review.py:528)

Read-only. Never writes to trade_log.json. Emits one JSON report to logs/.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

PT = ZoneInfo("America/Los_Angeles")
TOL_ABS = 0.02      # cents of rounding slack
TOL_REL = 0.005     # 0.5% — generous; a phantom is off by tens of percent, not 0.5%

_BASE = "https://paper-api.alpaca.markets"


def _headers() -> dict:
    k = os.getenv("ALPACA_API_KEY", "")
    s = os.getenv("ALPACA_SECRET_KEY", "")
    if not k or not s:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not in environment.",
              file=sys.stderr)
        sys.exit(2)
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def fills_for_date(date_str: str) -> list:
    """All FILL activities on a given YYYY-MM-DD, paginated."""
    out: list = []
    tok = None
    while True:
        q = {"date": date_str, "page_size": 100}
        if tok:
            q["page_token"] = tok
        url = f"{_BASE}/v2/account/activities/FILL?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers=_headers())
        try:
            rows = json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:
            print(f"  WARN: fills fetch failed for {date_str}: {e}", file=sys.stderr)
            return out
        if not rows:
            break
        out += rows
        if len(rows) < 100:
            break
        tok = rows[-1]["id"]
    return out


def price_matches(recorded: float, fills: list) -> bool:
    """Does the recorded exit price correspond to any real fill price that day?

    Accepts a single fill price OR the quantity-weighted average of same-side fills
    (the bot legitimately records a VWAP when an exit is filled in pieces).
    """
    prices = [float(f["price"]) for f in fills]
    for p in prices:
        if abs(recorded - p) <= max(TOL_ABS, p * TOL_REL):
            return True
    # weighted average across all fills for that symbol/day
    tq = sum(float(f["qty"]) for f in fills)
    if tq > 0:
        vwap = sum(float(f["price"]) * float(f["qty"]) for f in fills) / tq
        if abs(recorded - vwap) <= max(TOL_ABS, vwap * TOL_REL):
            return True
    return False


def main() -> int:
    tl_path = os.path.join(_ROOT, "trade_log.json")
    with open(tl_path) as f:
        tl = json.load(f)
    closed = tl.get("closed", [])
    print(f"closed trades in trade_log.json: {len(closed)}")

    # group rows by exit date so we fetch each date's fills exactly once
    by_date: dict = defaultdict(list)
    no_exit_ts = []
    for t in closed:
        ex = t.get("exit_time")
        if not ex:
            no_exit_ts.append(t)
            continue
        by_date[str(ex)[:10]].append(t)

    print(f"distinct exit dates: {len(by_date)}   "
          f"rows with NO exit_time: {len(no_exit_ts)}")

    fill_cache: dict = {}
    results = {"OK": [], "PHANTOM": [], "NO_FILLS": [], "NO_EXIT_TS": []}

    for row in no_exit_ts:
        results["NO_EXIT_TS"].append({
            "symbol": row.get("symbol"), "entry_price": row.get("entry_price"),
            "pnl": row.get("pnl"), "reason": "no exit_time on record",
        })

    for i, (d, rows) in enumerate(sorted(by_date.items()), 1):
        if d not in fill_cache:
            fill_cache[d] = fills_for_date(d)
        day_fills = fill_cache[d]
        bysym: dict = defaultdict(list)
        for a in day_fills:
            bysym[a["symbol"]].append(a)
        print(f"  [{i}/{len(by_date)}] {d}: {len(day_fills)} fills, "
              f"{len(rows)} closed row(s)")

        for t in rows:
            sym = t.get("symbol")
            rec = t.get("exit_price")
            rec_f = float(rec) if rec not in (None, "") else None
            sym_fills = bysym.get(sym, [])
            item = {
                "symbol": sym, "exit_date": d,
                "recorded_exit": rec_f,
                "recorded_pnl": t.get("pnl"),
                "entry_price": t.get("entry_price"),
                "qty": t.get("qty"),
                "actual_fills": [
                    {"side": a["side"], "qty": a["qty"], "price": float(a["price"])}
                    for a in sorted(sym_fills, key=lambda x: x["transaction_time"])
                ],
            }
            if rec_f is None:
                item["reason"] = "no exit_price recorded"
                results["NO_FILLS"].append(item)
            elif not sym_fills:
                results["NO_FILLS"].append(item)
            elif price_matches(rec_f, sym_fills):
                results["OK"].append(item)
            else:
                prices = [float(a["price"]) for a in sym_fills]
                item["nearest_actual"] = min(prices, key=lambda p: abs(p - rec_f))
                item["pct_off"] = round(
                    (rec_f - item["nearest_actual"]) / item["nearest_actual"] * 100, 2
                )
                results["PHANTOM"].append(item)

    n = len(closed)
    print("\n" + "=" * 70)
    print("CONTAMINATION SUMMARY")
    print("=" * 70)
    for k in ("OK", "PHANTOM", "NO_FILLS", "NO_EXIT_TS"):
        c = len(results[k])
        print(f"  {k:11s} {c:4d}  ({c / n * 100:5.1f}%)")

    if results["PHANTOM"]:
        bad_pnl = sum((r.get("recorded_pnl") or 0) for r in results["PHANTOM"])
        print(f"\n  P&L currently attributed to PHANTOM rows: ${bad_pnl:+,.2f}")
        print("  worst offenders by |pct_off|:")
        for r in sorted(results["PHANTOM"],
                        key=lambda x: -abs(x.get("pct_off") or 0))[:12]:
            print(f"    {r['exit_date']} {r['symbol']:6s} "
                  f"recorded ${r['recorded_exit']:.2f} "
                  f"vs actual ${r['nearest_actual']:.2f} ({r['pct_off']:+.1f}%) "
                  f"pnl={r['recorded_pnl']}")

    outp = os.path.join(_ROOT, "logs",
                        f"phantom_fill_audit_{datetime.now(PT):%Y-%m-%d}.json")
    with open(outp, "w") as f:
        json.dump({
            "generated": datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT"),
            "n_closed": n,
            "counts": {k: len(v) for k, v in results.items()},
            "phantom_pnl_total": round(
                sum((r.get("recorded_pnl") or 0) for r in results["PHANTOM"]), 2),
            "results": results,
        }, f, indent=2)
    print(f"\nwrote {outp}")
    print("\nNOTE: read-only. trade_log.json was NOT modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
