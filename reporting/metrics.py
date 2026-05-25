"""reporting/metrics.py — shared P&L and trade metrics for review pages."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BASE = Path(__file__).resolve().parent.parent   # project root
_LOGS = _BASE / "logs"

# Paper account starting equity — used to compute authoritative lifetime P&L.
# Change only if the account was reset or reseeded.
INITIAL_PAPER_EQUITY = 2500.0


def _fetch_alpaca_equity() -> float | None:
    """
    Fetch current equity from Alpaca paper account API.
    Returns float equity, or None if keys are missing / API unreachable.
    Used by compute_lifetime_stats() to provide Alpaca-authoritative total_pnl.
    Loads API keys via python-dotenv if available, otherwise reads os.environ.
    """
    try:
        import os
        import ssl as _ssl
        import urllib.request as _ur
        # Load .env so keys are available when called from standalone scripts
        # (weekly_review.py already calls load_dotenv; monthly_review.py does not)
        try:
            from dotenv import load_dotenv as _ld
            _ld(_BASE / ".env", override=False)
        except ImportError:
            pass
        _api_key    = os.environ.get("ALPACA_API_KEY", "").strip()
        _secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not _api_key or not _secret_key:
            logger.warning(
                "Alpaca keys not found in environment — lifetime P&L using EOD fallback"
            )
            return None
        _ctx = _ssl.create_default_context()
        req  = _ur.Request(
            "https://paper-api.alpaca.markets/v2/account",
            headers={
                "APCA-API-KEY-ID":     _api_key,
                "APCA-API-SECRET-KEY": _secret_key,
            },
        )
        with _ur.urlopen(req, context=_ctx, timeout=8) as r:
            acct = json.load(r)
        equity = float(acct.get("equity") or 0)
        return equity if equity > 0 else None
    except Exception as exc:
        logger.warning(
            "Alpaca equity fetch failed: %s — lifetime P&L using EOD fallback", exc
        )
        return None


def _load_eod(d: date) -> dict | None:
    path = _LOGS / f"eod_{d.isoformat()}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load EOD file %s: %s", path, e)
        return None


def _partial_on_same_day(t: dict) -> bool:
    pt = (t.get("partial_exit_time") or "")[:10]
    et = (t.get("exit_time") or "")[:10]
    return bool(pt and et and pt == et)


def _day_pnl(t: dict) -> float:
    """Day-attributed P&L. Avoids double-counting cross-day partial exits."""
    if t.get("partial_exited") and not _partial_on_same_day(t):
        return float(t.get("pnl_remaining") or t.get("pnl") or 0.0)
    return float(t.get("pnl") or 0.0)


def compute_period_stats(start_date: date, end_date: date) -> dict[str, Any]:
    """
    Trade statistics for a date range. Only status=='closed' trades are counted.
    P&L uses _day_pnl to avoid double-counting cross-day partial exits.
    total_pnl is summed from pnl_today (Alpaca-reconciled) in each eod file.
    """
    closed_trades: list[dict] = []
    total_pnl = 0.0
    pdt_count = 0
    overnight_count = 0

    d = start_date
    while d <= end_date:
        eod = _load_eod(d)
        if eod:
            closed_trades.extend(
                t for t in (eod.get("trades") or [])
                if t.get("status") == "closed"
            )
            _ap = eod.get("alpaca_pnl")
            total_pnl       += float(_ap if _ap is not None else (eod.get("pnl_today") or 0.0))  # noqa: E501
            pdt_count       += len(eod.get("pdt_slots_used") or [])
            overnight_count += len(eod.get("overnight_holds") or [])
        d += timedelta(days=1)

    total = len(closed_trades)
    wins  = sum(1 for t in closed_trades if _day_pnl(t) > 0)
    win_rate = (wins / total * 100) if total else 0.0

    scores = [t["score"] for t in closed_trades if t.get("score") is not None]
    tqis   = [t["tqi_score"] for t in closed_trades if t.get("tqi_score") is not None]

    gross_p = sum(_day_pnl(t) for t in closed_trades if _day_pnl(t) > 0)
    gross_l = sum(abs(_day_pnl(t)) for t in closed_trades if _day_pnl(t) < 0)
    pf = (gross_p / gross_l) if gross_l > 0 else None

    return {
        "total_pnl":       round(total_pnl, 2),
        "total_trades":    total,
        "wins":            wins,
        "win_rate":        win_rate,
        "avg_score":       (sum(scores) / len(scores)) if scores else None,
        "avg_tqi":         (sum(tqis) / len(tqis)) if tqis else None,
        "profit_factor":   pf,
        "pdt_count":       pdt_count,
        "overnight_count": overnight_count,
    }


def compute_lifetime_stats(
    equity: float | None = None,
    skip_fetch: bool = False,
) -> dict[str, Any]:
    """
    Lifetime stats across ALL eod files.
    total_pnl: Alpaca-authoritative (equity - INITIAL_PAPER_EQUITY) when API available.
    Fallback: sum of alpaca_pnl/pnl_today across EOD files.
    equity: if provided (e.g. from generate_dashboard.py which already fetched it),
            skips the Alpaca API call. Pass None to fetch from Alpaca directly.
    skip_fetch: if True and equity is None, skip the Alpaca API call entirely
                (use when the caller already knows the API is down, e.g. after
                _load_alpaca() returned an error dict). Avoids a redundant 8s block.
    Trade counts use closed trades only with _day_pnl win attribution.
    """
    if equity is not None:
        _equity: float | None = equity
    elif skip_fetch:
        _equity = None
    else:
        _equity = _fetch_alpaca_equity()
    _alpaca_pnl = (
        round(_equity - INITIAL_PAPER_EQUITY, 2) if _equity is not None else None
    )

    eod_paths = sorted(_LOGS.glob("eod_????-??-??.json"))
    _empty: dict[str, Any] = {
        "total_pnl": _alpaca_pnl if _alpaca_pnl is not None else 0.0,
        "total_trades": 0, "wins": 0, "win_rate": 0.0,
        "avg_score": None, "avg_tqi": None, "profit_factor": None,
        "pdt_count": 0, "overnight_count": 0,
    }
    if not eod_paths:
        return _empty

    first_date: date | None = None
    last_date: date | None = None
    for p in eod_paths:
        try:
            d = date.fromisoformat(p.stem[4:])   # strip leading "eod_"
            if first_date is None or d < first_date:
                first_date = d
            if last_date is None or d > last_date:
                last_date = d
        except ValueError as e:
            logger.warning("Skipping malformed EOD filename %s: %s", p.name, e)

    if first_date is None or last_date is None:
        return _empty

    stats = compute_period_stats(first_date, last_date)
    if _alpaca_pnl is not None:
        stats["total_pnl"] = _alpaca_pnl
    return stats
