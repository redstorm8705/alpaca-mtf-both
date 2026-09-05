#!/usr/bin/env python3
# ruff: noqa: E501,E402  — E501: log strings; E402: imports follow the sys.path/.env bootstrap (cron self-auth)
"""
scripts/gex_skip0dte_shadow.py — READ-ONLY soak validating the GEX skip-0DTE window fix.

Root cause (2026-09-04, board+Gro+GAI): data.gex._expiry_range() returns `today → coming Friday`,
which on expiry / daily-expiry days collapses to a 0DTE-only window; 0DTE contracts are gutted by the
time-to-expiry floor + zero-bid → 0 valid contracts → regime label UNKNOWN AND pin.kind="none"
(levels_ok=False) → day-tier Layer-B STAND_DOWN + main-bot MIN_SCORE inflation. Live test confirmed
SKIPPING the 0DTE expiry restores a valid, near-spot signal.

This validator recomputes GEX with a SKIP-0DTE window for the day-tier universe each RTH cycle and logs
the label / pin / flip-vs-spot, so we can confirm over ≥1 live session (incl. an expiry Friday) that the
fix yields sane, near-spot labels+pins BEFORE the live `_expiry_range` change touches GEX-driven sizing
(the risk seat's required shadow soak; also the pre-market readiness sim that should have run pre-open).

STRICTLY READ-ONLY: reuses data.gex._fetch_contracts / _fetch_snapshots / _compute_gex (the live GEX
pipeline's own functions) + the Alpaca stock latest-trade for spot. It does NOT modify data/gex.py, does
NOT write gex_snapshot.json, and touches no GEX/sizing/day-tier/order path — it only appends to its own
logs/gex_skip0dte_shadow.jsonl. Never raises into the caller.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:                                             # self-auth under cron
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_ROOT / ".env")
except Exception as _e:
    logging.getLogger("gex_skip0dte_shadow").debug("dotenv load skipped: %s", _e)

import data.gex as gex                            # reuse the live GEX pipeline's fetch + compute (read-only)

PT = ZoneInfo("America/Los_Angeles")
_SYMS = ("SPY", "QQQ", "AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA")
_OUT = _ROOT / "logs" / "gex_skip0dte_shadow.jsonl"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("gex_skip0dte_shadow")


def _spot(symbol: str) -> float:
    """Underlying spot via Alpaca stock latest-trade (usable during RTH). Raises on failure."""
    k = os.environ.get("ALPACA_API_KEY", "")
    s = os.environ.get("ALPACA_SECRET_KEY", "")
    req = urllib.request.Request(
        f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
        headers={"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return float(json.load(r)["trade"]["p"])


def _skip0dte_window() -> tuple[str, str]:
    """Skip today's 0DTE: start at the next calendar day, span +8 days so a liquid weekly expiry is
    always captured (validated live: this window restored NEAR-FLIP/547 valid/pin-at-spot on SPY)."""
    start = date.today() + timedelta(days=1)
    return start.isoformat(), (start + timedelta(days=8)).isoformat()


def evaluate(symbol: str) -> dict:
    """Compute skip-0DTE GEX for one symbol. Returns a flat dict for the jsonl row. Never raises."""
    try:
        spot = _spot(symbol)
        gte, lte = _skip0dte_window()
        oi = gex._fetch_contracts(symbol, gte, lte)
        if not oi:
            return {"symbol": symbol, "error": "no_contracts", "window": [gte, lte]}
        snaps = gex._fetch_snapshots(list(oi.keys()))
        r = gex._compute_gex(snaps, oi, spot)
        pin = r.get("pin", {}) or {}
        flip = r.get("flip_strike")
        cent = pin.get("centroid")

        def _dist_pct(v) -> "float | None":
            return round(abs(float(v) - spot) / spot * 100, 2) if (v is not None and spot) else None

        return {
            "symbol": symbol,
            "spot": round(spot, 2),
            "window": [gte, lte],
            "label": r.get("label"),
            "count": r.get("contract_count"),
            "capture": r.get("capture_ratio"),
            "quality_ok": bool(r.get("quality_ok")),
            "flip_strike": flip,
            "flip_dist_pct": _dist_pct(flip),
            "pin_kind": pin.get("kind"),
            "pin_centroid": cent,
            "pin_dist_pct": _dist_pct(cent),
            "levels_ok": pin.get("kind") == "centroid+wall",   # mirrors get_gex_levels' pin-resolution test
        }
    except Exception as e:
        logger.warning("skip-0DTE shadow %s failed: %s", symbol, e)
        return {"symbol": symbol, "error": str(e)}


def main() -> int:
    ts = datetime.now(PT).isoformat()
    rows = [evaluate(sym) for sym in _SYMS]
    try:
        with open(_OUT, "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps({"ts": ts, **row}) + "\n")
    except Exception as e:
        logger.error("gex_skip0dte_shadow: write failed: %s", e)
        return 1
    q = sum(1 for r in rows if r.get("quality_ok"))
    lv = sum(1 for r in rows if r.get("levels_ok"))
    logger.info("gex skip-0DTE shadow: %d/%d quality_ok, %d/%d levels_ok", q, len(_SYMS), lv, len(_SYMS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
