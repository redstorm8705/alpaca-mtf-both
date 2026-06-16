"""
tca_logger.py
Transaction Cost Analysis — observational execution-quality logging.

Logs slippage between the decision/intended price and the actual Alpaca fill
price for entries and exits. Read-only / additive — never gates, delays, or
otherwise affects any trading decision. Mirrors trade_logger.py's pattern.

Data source: prices are passed in by the caller (already T1-sourced upstream
in execution/entry_logic.py and execution/exit_logic.py) — this module does
no data fetching of its own.

Output: logs/tca_metrics.jsonl (newline-delimited JSON), append-only.
All timestamps are PT (America/Los_Angeles) per Guardrail 8.

Usage:
  from tca_logger import log_entry_slippage, log_exit_slippage
  log_entry_slippage("AMZN", "long", signal_price=236.50, fill_price=236.78,
                      qty=4, fill_confirmed=True)
  log_exit_slippage("AMZN", "long", intended_price=230.00, fill_price=229.85,
                     qty=4, reason="hard_stop")
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

PT     = ZoneInfo("America/Los_Angeles")
_JSONL = Path(__file__).resolve().parent / "logs" / "tca_metrics.jsonl"


def _write(record: dict) -> None:
    try:
        _JSONL.parent.mkdir(parents=True, exist_ok=True)
        with open(_JSONL, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning(f"tca_metrics.jsonl write failed: {e}")


def log_entry_slippage(
    symbol: str,
    direction: str,
    signal_price: float,
    fill_price: float,
    qty: int,
    fill_confirmed: bool,
) -> None:
    """
    Log entry execution quality.

    signal_price   — pre-order decision price (bar close or live trade), i.e.
                      the price entry_logic.py had before submitting the order.
    fill_price     — actual Alpaca fill (filled_avg_price).
    fill_confirmed — True only if fill_price came from a confirmed Alpaca fill
                      poll, not the pre-order estimate fallback. Unconfirmed
                      fills are skipped — logging a fallback estimate against
                      itself would record fabricated zero slippage.
    """
    if not fill_confirmed:
        logger.debug(f"[{symbol}] TCA entry: fill unconfirmed — skipping")
        return
    if signal_price <= 0 or fill_price <= 0:
        logger.debug(f"[{symbol}] TCA entry: invalid price(s) — skipping")
        return

    slippage     = fill_price - signal_price
    slippage_pct = (slippage / signal_price) * 100
    # Long: paying more than signal price is adverse. Short: paying less is adverse.
    adverse = slippage > 0 if direction == "long" else slippage < 0

    _write({
        "ts":            datetime.now(PT).isoformat(),
        "leg":           "entry",
        "symbol":        symbol,
        "direction":     direction,
        "signal_price":  round(signal_price, 4),
        "fill_price":    round(fill_price, 4),
        "slippage":      round(slippage, 4),
        "slippage_pct":  round(slippage_pct, 4),
        "adverse":       adverse,
        "qty":           qty,
    })


def log_exit_slippage(
    symbol: str,
    direction: str,
    intended_price: float | None,
    fill_price: float,
    qty: int,
    reason: str,
) -> None:
    """
    Log exit execution quality.

    intended_price — the stop/target/trail level that triggered the exit
                      decision. None for price-agnostic exits (e.g. "signal",
                      "thesis_invalidation") — those are skipped rather than
                      logged against a fabricated reference.
    fill_price      — actual Alpaca fill from _fetch_actual_fill_price().
                      Callers must skip this call when
                      trade.get("_fill_unverified") is True.
    """
    if intended_price is None or intended_price <= 0 or fill_price <= 0:
        logger.debug(f"[{symbol}] TCA exit: no intended reference price — skipping")
        return

    slippage     = fill_price - intended_price
    slippage_pct = (slippage / intended_price) * 100
    # Long exit = sell: below intended is adverse. Short exit = buy: above is adverse.
    adverse = slippage < 0 if direction == "long" else slippage > 0

    _write({
        "ts":              datetime.now(PT).isoformat(),
        "leg":             "exit",
        "symbol":          symbol,
        "direction":       direction,
        "intended_price":  round(intended_price, 4),
        "fill_price":      round(fill_price, 4),
        "slippage":        round(slippage, 4),
        "slippage_pct":    round(slippage_pct, 4),
        "adverse":         adverse,
        "qty":             qty,
        "reason":          reason,
    })
