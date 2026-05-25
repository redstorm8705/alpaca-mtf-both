"""
data/market_breadth.py
Market breadth health score — T3 data tier (TraderMonty CSV, no API key).

Computes a 0-100 composite breadth score from 5 components:
  1. Current 8MA breadth level     (30 pts) — how many stocks are participating
  2. 200MA trend direction          (20 pts) — structural bull/bear backdrop
  3. 8MA vs 200MA structure         (20 pts) — momentum alignment
  4. Bearish signal flag            (15 pts) — TraderMonty's backtested bear flag
  5. Historical percentile          (15 pts) — current breadth vs full history

Score zones:
  80-100 STRONG    → 1.00x size (broad participation, trend up)
  60-79  HEALTHY   → 1.00x size (healthy but not peak)
  40-59  NEUTRAL   → 0.95x size (mixed signals, slight discount)
  20-39  WEAKENING → 0.85x size (breadth deteriorating)
  0-19   CRITICAL  → 0.65x size (breadth collapse, major risk-off)

Cache: data/cache/breadth_cache.json — 24-hour TTL.
Refresh: called once per pre-market scan (8:45-9:30 AM ET).

Data sources:
  T3: https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv
  T3: https://tradermonty.github.io/market-breadth-analysis/market_breadth_summary.csv

Guardrail 1: T3 data tier. No API key required.
Guardrail 2: No Alpaca SDK instantiation here.
Guardrail 3: Output to data/cache/ only.
"""

import json
import logging
import os
from datetime import datetime, date, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

PT = ZoneInfo("America/Los_Angeles")

_DETAIL_URL  = "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv"
_SUMMARY_URL = "https://tradermonty.github.io/market-breadth-analysis/market_breadth_summary.csv"
_CACHE_PATH  = Path(__file__).parent.parent / "data" / "cache" / "breadth_cache.json"
_TIMEOUT     = 10.0
_CACHE_TTL_H = 24   # hours before re-fetching

# Historical percentile anchors from TraderMonty summary
# Peak average 200MA ≈ 0.715, trough 8MA ≈ 0.231 — used to normalize breadth level
_HIST_MIN = 0.231
_HIST_MAX = 0.715

# Zone thresholds
_ZONES = [
    (80, "STRONG",    1.00),
    (60, "HEALTHY",   1.00),
    (40, "NEUTRAL",   0.95),
    (20, "WEAKENING", 0.85),
    ( 0, "CRITICAL",  0.65),
]


def _size_floor_for_score(score: int) -> float:
    for threshold, _, floor in _ZONES:
        if score >= threshold:
            return floor
    return 0.65


def _zone_for_score(score: int) -> str:
    for threshold, zone, _ in _ZONES:
        if score >= threshold:
            return zone
    return "CRITICAL"


def _load_cache() -> dict | None:
    """Return cached breadth dict if still fresh, else None."""
    try:
        if not _CACHE_PATH.exists():
            return None
        with open(_CACHE_PATH) as f:
            data = json.load(f)
        cached_at = data.get("cached_at_utc")
        if not cached_at:
            return None
        age_h = (datetime.now(timezone.utc).timestamp() -
                 datetime.fromisoformat(cached_at).timestamp()) / 3600
        if age_h > _CACHE_TTL_H:
            return None
        return data
    except Exception as e:
        logger.debug(f"breadth cache read failed: {e}")
        return None


def _save_cache(result: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        result["cached_at_utc"] = datetime.now(timezone.utc).isoformat()
        with open(_CACHE_PATH, "w") as f:
            json.dump(result, f, indent=2)
    except Exception as e:
        logger.debug(f"breadth cache write failed: {e}")


def _fetch_detail_csv() -> list[dict] | None:
    """Fetch market_breadth_data.csv, return list of row dicts (recent rows first)."""
    try:
        resp = requests.get(_DETAIL_URL, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"breadth detail CSV: HTTP {resp.status_code}")
            return None
        lines = resp.text.strip().split("\n")
        if len(lines) < 2:
            return None
        header = [h.strip() for h in lines[0].split(",")]
        rows = []
        for line in reversed(lines[1:]):   # most-recent first
            parts = line.strip().split(",")
            if len(parts) < len(header):
                continue
            row = {}
            for i, key in enumerate(header):
                row[key] = parts[i].strip()
            rows.append(row)
        return rows
    except Exception as e:
        logger.warning(f"breadth detail CSV fetch failed: {e}")
        return None


def _parse_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _parse_bool(val: str) -> bool:
    return str(val).strip().lower() in ("true", "1", "yes")


def _compute_score(rows: list[dict]) -> dict:
    """
    Compute 5-component breadth score (0-100) from detail rows.
    rows[0] is the most-recent trading day.
    """
    if not rows:
        return {"score": 50, "zone": "NEUTRAL", "size_floor": 0.95,
                "as_of": "unknown", "error": "no data"}

    latest  = rows[0]
    as_of   = latest.get("Date", "unknown")

    breadth_8ma    = _parse_float(latest.get("Breadth_Index_8MA",  "0"))
    breadth_200ma  = _parse_float(latest.get("Breadth_Index_200MA","0"))
    trend_200      = _parse_float(latest.get("Breadth_200MA_Trend","0"))
    bearish_signal = _parse_bool(latest.get("Bearish_Signal", "False"))
    breadth_raw    = _parse_float(latest.get("Breadth_Index_Raw",  "0"))

    # ── Component 1: Current 8MA level (30 pts) ─────────────────────────────
    c1 = min(30, round(breadth_8ma * 30))

    # ── Component 2: 200MA trend direction (20 pts) ─────────────────────────
    if trend_200 > 0:
        c2 = 20
    elif trend_200 == 0:
        c2 = 10
    else:
        c2 = 0

    # ── Component 3: 8MA vs 200MA structure (20 pts) ────────────────────────
    c3 = 20 if breadth_8ma > breadth_200ma else 0

    # ── Component 4: Bearish signal flag (15 pts) ───────────────────────────
    c4 = 0 if bearish_signal else 15

    # ── Component 5: Historical percentile (15 pts) ─────────────────────────
    hist_range = max(_HIST_MAX - _HIST_MIN, 0.001)
    pct = max(0.0, min(1.0, (breadth_raw - _HIST_MIN) / hist_range))
    if pct >= 0.80:
        c5 = 15
    elif pct >= 0.60:
        c5 = 12
    elif pct >= 0.40:
        c5 = 8
    elif pct >= 0.20:
        c5 = 4
    else:
        c5 = 0

    score = c1 + c2 + c3 + c4 + c5
    score = max(0, min(100, score))
    zone  = _zone_for_score(score)
    floor = _size_floor_for_score(score)

    return {
        "score":       score,
        "zone":        zone,
        "size_floor":  floor,
        "as_of":       as_of,
        "breadth_8ma": round(breadth_8ma, 4),
        "breadth_200ma": round(breadth_200ma, 4),
        "trend_200":   int(trend_200),
        "bearish_signal": bearish_signal,
        "breadth_raw": round(breadth_raw, 4),
        "components":  {"level": c1, "trend": c2, "structure": c3,
                        "bearish": c4, "percentile": c5},
    }


def get_breadth_score(force_refresh: bool = False) -> dict:
    """
    Return the current market breadth health score dict.

    Keys:
      score        — 0-100 composite score
      zone         — STRONG / HEALTHY / NEUTRAL / WEAKENING / CRITICAL
      size_floor   — multiplier to apply to position sizing (0.65–1.00)
      as_of        — date of most-recent data row (YYYY-MM-DD)
      breadth_8ma  — current 8MA breadth index (0-1)
      bearish_signal — TraderMonty bearish flag (bool)
      components   — sub-score breakdown dict

    Returns a neutral safe default {"score": 50, "zone": "NEUTRAL", "size_floor": 0.95}
    on any failure so the bot degrades gracefully.
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            logger.debug(
                f"breadth: cache hit (as_of={cached.get('as_of')} "
                f"score={cached.get('score')} zone={cached.get('zone')})"
            )
            return cached

    # T3: fetch TraderMonty CSV
    rows = _fetch_detail_csv()
    if not rows:
        logger.warning("breadth: CSV unavailable — returning neutral default")
        return {"score": 50, "zone": "NEUTRAL", "size_floor": 0.95,
                "as_of": "unavailable", "error": "fetch_failed"}

    result = _compute_score(rows)
    _save_cache(result)

    logger.info(
        f"breadth: score={result['score']}/100 zone={result['zone']} "
        f"floor={result['size_floor']:.2f}x | "
        f"8MA={result['breadth_8ma']:.3f} trend={result['trend_200']:+d} "
        f"bearish={result['bearish_signal']} as_of={result['as_of']}"
    )
    return result
