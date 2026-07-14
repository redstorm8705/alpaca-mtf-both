#!/usr/bin/env python3
# ruff: noqa: E501, E702
"""
research/fifo_reconcile.py — correct ALL-TIME FIFO realized-P&L engine, validated to reconcile
against Alpaca equity. Foundation for the Alpaca-sourced Strategy Edge Report (replaces the bot
tracker's corrupted per-trade pnl). Read-only.

Reconciliation identity (the correctness gate):
    equity - 2500(initial)  ==  Σ FIFO_realized  -  Σ fees  +  Σ open_unrealized   (± dividends)

Signed-FIFO: per symbol a queue of signed lots (+long / -short). A fill in the OPPOSITE direction
of the front lot realizes P&L FIFO; same direction opens/adds; a fill larger than the opposite
book flips the position. Handles longs, shorts and flips uniformly.
"""
from __future__ import annotations

import os
import json
import ssl
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import deque, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_PT = ZoneInfo("America/Los_Angeles")

_ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass

_BASE = "https://paper-api.alpaca.markets"
try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CTX = ssl.create_default_context()


def _hdr() -> dict:
    return {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}


def _get(path: str):
    url = _BASE + path
    for a in range(6):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_hdr()), timeout=25, context=_CTX) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (a + 1)); continue
            raise
    raise RuntimeError("429 exhausted")


def fetch_activities() -> tuple[list, float]:
    """All FILL activities (chronological) + total FEE amount. Robust page_token pagination
    with a hard cap + no-advance break (guards the runaway-loop bug)."""
    fills: list = []
    fees_total = 0.0
    seen: set = set()
    tok = None
    pages = 0
    while pages < 60:
        pages += 1
        p = {"page_size": "100", "direction": "asc"}
        if tok:
            p["page_token"] = tok
        page = _get("/v2/account/activities?" + urllib.parse.urlencode(p))
        if not page:
            break
        fresh = [a for a in page if a.get("id") not in seen]
        if not fresh:
            break
        for a in page:
            seen.add(a.get("id"))
        for a in fresh:
            at = a.get("activity_type")
            if at == "FILL":
                fills.append(a)
            elif at == "FEE":
                try:
                    fees_total += abs(float(a.get("net_amount") or a.get("price") or 0) or 0)
                except (TypeError, ValueError):
                    pass
        if len(page) < 100:
            break
        tok = page[-1].get("id")
        time.sleep(0.4)
    fills.sort(key=lambda f: str(f.get("transaction_time") or ""))
    return fills, round(fees_total, 2)


def fifo_realized(fills: list) -> tuple[float, list]:
    """Signed-FIFO over all fills. Returns (total_realized, per_trade[]).
    per_trade item: {symbol, qty, entry, exit, pnl, opened_at, closed_at, direction}."""
    lots: dict[str, deque] = defaultdict(deque)   # symbol -> deque of [signed_qty, price, ts]
    per_trade: list = []
    total = 0.0
    for f in fills:
        sym = f.get("symbol")
        side = f.get("side")
        try:
            qty = float(f.get("qty") or 0)
            px = float(f.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if not sym or qty <= 0 or px <= 0:
            continue
        ts = str(f.get("transaction_time") or "")
        signed = qty if side == "buy" else -qty   # sell / sell_short => negative
        q = lots[sym]
        # Consume opposite-sign lots first (realize P&L), then open/add.
        while signed != 0 and q and (q[0][0] > 0) != (signed > 0):
            lot_qty, lot_px, lot_ts = q[0]
            take = min(abs(signed), abs(lot_qty))
            if lot_qty > 0:            # closing a LONG (this fill is a sell)
                pnl = (px - lot_px) * take
                direction = "long"
            else:                      # closing a SHORT (this fill is a buy-to-cover)
                pnl = (lot_px - px) * take
                direction = "short"
            total += pnl
            per_trade.append({"symbol": sym, "qty": take, "entry": round(lot_px, 4),
                              "exit": round(px, 4), "pnl": round(pnl, 4),
                              "opened_at": lot_ts, "closed_at": ts, "direction": direction})
            # reduce the front lot
            if abs(lot_qty) > take:
                q[0][0] = lot_qty - take if lot_qty > 0 else lot_qty + take
            else:
                q.popleft()
            signed = signed + take if signed < 0 else signed - take
        if signed != 0:                # leftover opens/extends a position
            q.append([signed, px, ts])
    return round(total, 2), per_trade


def _match_score(pt: dict, closed: list) -> "int | None":
    sym = pt.get("symbol")
    try:
        cdt = datetime.fromisoformat(str(pt.get("closed_at")).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        cdt = None
    best, best_gap = None, None
    for t in closed:
        if t.get("symbol") != sym:
            continue
        et = t.get("exit_time") or t.get("closed_at") or t.get("entry_time") or ""
        try:
            edt = datetime.fromisoformat(str(et).replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        if cdt is None:
            return int(t["score"]) if t.get("score") is not None else None
        gap = abs((cdt - edt).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = t, gap
    return int(best["score"]) if best and best.get("score") is not None else None


_CACHE = _ROOT / "logs" / "fifo_edge.json"


def compute_edge_cache() -> dict:
    """Reconciled Alpaca-FIFO edge data for the Strategy Edge Report. Returns the full dict."""
    fills, fees = fetch_activities()
    realized, per_trade = fifo_realized(fills)
    eq = float(_get("/v2/account")["equity"])
    unreal = round(sum(float(p["unrealized_pl"]) for p in _get("/v2/positions")), 2)
    try:
        closed = json.loads((_ROOT / "trade_log.json").read_text()).get("closed", [])
    except Exception:
        closed = []
    by_score: dict = {}
    for pt in per_trade:
        sc = _match_score(pt, closed)
        key = str(sc) if sc is not None else "?"
        b = by_score.setdefault(key, {"n": 0, "pnl": 0.0, "wins": 0, "win_pnl": 0.0, "loss_pnl": 0.0, "loss_n": 0})
        b["n"] += 1
        b["pnl"] += pt["pnl"]
        if pt["pnl"] > 0:
            b["wins"] += 1; b["win_pnl"] += pt["pnl"]
        else:
            b["loss_n"] += 1; b["loss_pnl"] += pt["pnl"]
    for b in by_score.values():
        b["pnl"] = round(b["pnl"], 2)
        b["win_pct"] = round(100 * b["wins"] / b["n"], 0) if b["n"] else 0
        b["avg_win"] = round(b["win_pnl"] / b["wins"], 2) if b["wins"] else 0.0
        b["avg_loss"] = round(b["loss_pnl"] / b["loss_n"], 2) if b["loss_n"] else 0.0
    lhs = round(eq - 2500, 2)
    rhs = round(realized - fees + unreal, 2)
    total_wins = sum(1 for pt in per_trade if pt["pnl"] > 0)
    return {
        "updated": datetime.now(_PT).isoformat(),
        "realized": realized, "fees": fees, "unrealized": unreal,
        "equity_minus_2500": lhs, "residual": round(lhs - rhs, 2),
        "reconciles": abs(lhs - rhs) < 1.0, "n_legs": len(per_trade),
        "total_wins": total_wins,
        "win_pct": round(100 * total_wins / len(per_trade), 0) if per_trade else 0,
        "by_score": by_score,
    }


def write_cache() -> dict:
    """Atomically persist the reconciled edge cache to logs/fifo_edge.json (RC-5)."""
    d = compute_edge_cache()
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE.with_suffix(f".json.{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(_CACHE))
    return d


def main() -> int:
    d = write_cache()   # single fetch → compute → persist logs/fifo_edge.json
    print("=" * 66)
    print("FIFO RECONCILE — all-time  (wrote logs/fifo_edge.json)")
    print(f"  closed-trade legs   = {d['n_legs']}")
    print(f"  Σ FIFO realized     = ${d['realized']:+,.2f}")
    print(f"  Σ fees              = ${d['fees']:,.2f}")
    print(f"  open unrealized     = ${d['unrealized']:+,.2f}")
    print("  ------------------------------------")
    print(f"  equity - 2500       = ${d['equity_minus_2500']:+,.2f}")
    print(f"  RESIDUAL            = ${d['residual']:+,.2f}   {'✅ RECONCILES' if d['reconciles'] else '❌ gap >$1'}")
    print("-" * 66)
    print("PER-SCORE edge (Alpaca-FIFO):")
    print(f"  {'score':<10}{'n':>4}{'Σpnl':>12}{'win%':>8}")
    for k in sorted(d["by_score"].keys(), key=lambda x: (0, int(x)) if x.isdigit() else (1, 0)):
        b = d["by_score"][k]
        print(f"  {k + '/12':<10}{b['n']:>4}{b['pnl']:>+12.2f}{b['win_pct']:>7.0f}%")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
