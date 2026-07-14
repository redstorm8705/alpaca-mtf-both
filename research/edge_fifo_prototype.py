#!/usr/bin/env python3
# ruff: noqa: E501
"""
research/edge_fifo_prototype.py — PROTOTYPE (read-only): rebuild the Strategy Edge Report's
per-score P&L from ALPACA FIFO instead of the bot tracker's (corrupted) pnl field.

Goal: prove the approach reconciles before porting into weekly_review._strategy_validation_html.
Steps: (1) fetch ALL account fills, (2) FIFO-reconstruct all-time per-trade realized P&L via the
existing execution.fifo_pnl._fifo_reconstruct, (3) match each FIFO trade to the bot's closed trade
by symbol + nearest exit time to attach its SCORE, (4) bin by score using FIFO pnl, (5) reconcile
Σ FIFO realized vs Alpaca (equity − 2500 − open-unrealized).
"""
from __future__ import annotations

import os
import sys
import json
import ssl
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

from execution.fifo_pnl import _fifo_reconstruct  # noqa: E402

_BASE = "https://paper-api.alpaca.markets"
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CTX = ssl.create_default_context()


def _hdr() -> dict:
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def fetch_all_fills(start: str = "2026-04-01") -> list:
    """All FILL activities from `start` → now, paginated by after_id (asc)."""
    out: list = []
    after_id = None
    while True:
        params = {"direction": "asc", "page_size": "100", "after": start + "T00:00:00Z"}
        if after_id:
            params["after_id"] = after_id
        url = f"{_BASE}/v2/account/activities/FILL?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=_hdr())
        page = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
                    page = json.loads(r.read().decode())
                break
            except Exception:
                time.sleep(2 ** attempt)
        if not page:
            break
        out.extend(page)
        if len(page) < 100:
            break
        after_id = page[-1]["id"]
    return out


def _alpaca_account() -> tuple[float, float]:
    """(equity, open_unrealized_total)."""
    def g(p):
        with urllib.request.urlopen(urllib.request.Request(_BASE + p, headers=_hdr()), timeout=15, context=_CTX) as r:
            return json.loads(r.read().decode())
    eq = float(g("/v2/account")["equity"])
    up = sum(float(p["unrealized_pl"]) for p in g("/v2/positions"))
    return eq, up


def match_score(pt: dict, closed: list) -> "int | None":
    """Attach a SCORE to a FIFO per-trade by matching the bot's closed trade with the same symbol
    and the nearest exit time to the FIFO fill time."""
    sym = pt.get("symbol")
    ft = pt.get("filled_at") or pt.get("exit_time") or ""
    try:
        fdt = datetime.fromisoformat(str(ft).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        fdt = None
    best, best_dt = None, None
    for t in closed:
        if t.get("symbol") != sym:
            continue
        et = t.get("exit_time") or t.get("closed_at") or t.get("entry_time") or ""
        try:
            edt = datetime.fromisoformat(str(et).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        if fdt is None:
            best = t
            break
        gap = abs((fdt - edt).total_seconds())
        if best_dt is None or gap < best_dt:
            best, best_dt = t, gap
    return int(best["score"]) if best and best.get("score") is not None else None


def main() -> int:
    fills = fetch_all_fills()
    print(f"fetched {len(fills)} all-time fills")
    _pnl, _lots, per_trade, _seen = _fifo_reconstruct(fills, {}, set())
    fifo_total = round(sum(float(t.get("pnl") or 0) for t in per_trade), 2)
    print(f"FIFO per-trade count: {len(per_trade)}  Σ realized = ${fifo_total:+.2f}")

    tl = json.loads((_ROOT / "trade_log.json").read_text())
    closed = tl.get("closed", [])
    tracker_total = round(sum(float(t.get("pnl") or 0) for t in closed), 2)
    print(f"tracker closed count: {len(closed)}  Σ (corrupted) = ${tracker_total:+.2f}")

    # Bin FIFO per-trade by matched score
    bands: dict = {}
    unmatched = 0
    for pt in per_trade:
        sc = match_score(pt, closed)
        key = sc if sc is not None else "?"
        if sc is None:
            unmatched += 1
        bands.setdefault(key, []).append(float(pt.get("pnl") or 0))
    print(f"\nPER-SCORE (Alpaca-FIFO pnl), unmatched-to-score: {unmatched}")
    print(f"  {'score':<10}{'n':>4}{'Σpnl':>12}{'win%':>8}")
    binned_total = 0.0
    for key in sorted(bands.keys(), key=lambda x: (0, x) if isinstance(x, int) else (1, 0)):
        v = bands[key]
        s = sum(v)
        binned_total += s
        wr = 100 * sum(1 for x in v if x > 0) / len(v) if v else 0
        print(f"  {str(key) + '/12':<10}{len(v):>4}{s:>+12.2f}{wr:>7.0f}%")
    print(f"  {'TOTAL':<10}{len(per_trade):>4}{binned_total:>+12.2f}")

    eq, up = _alpaca_account()
    print(f"\nRECONCILE: FIFO realized ${fifo_total:+.2f} + open-unrealized ${up:+.2f} = ${fifo_total+up:+.2f}")
    print(f"           vs Alpaca equity-2500 = ${eq-2500:+.2f}   residual ${(eq-2500)-(fifo_total+up):+.2f}")
    print(f"(tracker was ${tracker_total:+.2f} — off by ${fifo_total - tracker_total:+.2f} from FIFO truth)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
