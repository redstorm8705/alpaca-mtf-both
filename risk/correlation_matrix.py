"""
risk/correlation_matrix.py
Portfolio correlation gate — enforces Architecture Invariant #10.

Invariant #10: No more than 2 simultaneous positions with beta correlation >0.7
to each other. Sector gate enforces sector-level; this covers cross-sector beta overlap.

Design decisions (board vote S59, Harris/Dalio):
  - 60-day rolling daily returns (not 20-day — too few samples for stable estimate)
  - Spearman rank correlation (not Pearson — robust to tail events and outliers)
  - Fail-CLOSED: data unavailability → assume max correlation → block entry
    Rationale: data outages most likely during stress when correlation is highest
  - Directional-aware: only block same-direction pairs (long+long, short+short)
    A long TQQQ + short SQQQ has negative correlation but is a designed hedge

Data source: T1 (Alpaca Data) — daily bars via data/fetcher.py
No SDK instantiation here; fetch_bars is the sole approved bar source.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from data.fetcher import fetch_bars
import config

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_LOOKBACK_DAYS       = 60    # daily bars for Spearman correlation estimate
_BARS_FETCH          = 70    # fetch extra buffer for gaps/holidays
_CORR_THRESHOLD      = 0.7   # Invariant #10 threshold
_MAX_CORRELATED_PAIRS = 2    # Invariant #10: max pairs before blocking entry
_MIN_BARS_REQUIRED   = 20    # minimum valid returns to compute correlation


def _fetch_daily_returns(symbol: str) -> list[float] | None:
    """
    Fetch 60-day daily returns for symbol via T1 Alpaca Data.
    Returns list of daily pct returns (as decimals, e.g. 0.012), or None on failure.
    """
    try:
        df = fetch_bars(symbol, config.TF_DAILY, num_bars=_BARS_FETCH)
        if df is None or df.empty:
            logger.debug(f"[correlation] {symbol}: no bars returned")
            return None
        closes = df["close"].dropna()
        if len(closes) < _MIN_BARS_REQUIRED + 1:
            logger.debug(
                f"[correlation] {symbol}: only {len(closes)} bars "
                f"(need {_MIN_BARS_REQUIRED + 1})"
            )
            return None
        # Use last _LOOKBACK_DAYS + 1 bars so we get _LOOKBACK_DAYS returns
        closes = closes.iloc[-(min(_LOOKBACK_DAYS + 1, len(closes))):]
        returns = closes.pct_change().dropna().tolist()
        return returns if returns else None
    except Exception as e:
        logger.warning(f"[correlation] {symbol}: fetch_bars error — {e}")
        return None


def _spearman(x: list[float], y: list[float]) -> float | None:
    """
    Compute Spearman rank correlation between two equal-length series.
    Returns float in [-1, 1], or None if computation fails.
    Pure Python implementation — no scipy dependency.
    """
    n = min(len(x), len(y))
    if n < _MIN_BARS_REQUIRED:
        return None

    x = x[-n:]
    y = y[-n:]

    # Rank both series (average rank for ties)
    def _rank(series: list[float]) -> list[float]:
        indexed = sorted(enumerate(series), key=lambda t: t[1])
        ranks = [0.0] * len(series)
        i = 0
        while i < len(indexed):
            j = i
            while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg_rank
            i = j + 1
        return ranks

    rx = _rank(x)
    ry = _rank(y)

    # Pearson on ranks = Spearman
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    num  = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    den_x = sum((rx[i] - mean_rx) ** 2 for i in range(n)) ** 0.5
    den_y = sum((ry[i] - mean_ry) ** 2 for i in range(n)) ** 0.5

    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def would_breach_correlation_limit(
    proposed_symbol: str,
    proposed_direction: str,
    open_positions: list[dict],
) -> bool:
    """
    Returns True if adding proposed_symbol/direction would result in more than
    _MAX_CORRELATED_PAIRS same-direction pairs with Spearman correlation above
    _CORR_THRESHOLD.

    Fails CLOSED on any data unavailability — returns True (blocks entry).

    Args:
        proposed_symbol:    ticker to evaluate
        proposed_direction: "long" or "short"
        open_positions:     list of {"symbol": str, "direction": str} dicts
                            for all currently open positions

    Returns:
        True  → entry would breach limit (or data unavailable) — skip this symbol
        False → entry is safe to proceed
    """
    if not open_positions:
        return False

    if proposed_direction not in ("long", "short"):
        logger.error(
            f"[correlation] {proposed_symbol}: invalid direction "
            f"'{proposed_direction}' — fail-closed, blocking entry"
        )
        return True  # fail-closed on invalid caller input

    # Fetch returns for proposed symbol
    proposed_returns = _fetch_daily_returns(proposed_symbol)
    if proposed_returns is None:
        logger.warning(
            f"[correlation] {proposed_symbol}: returns unavailable — "
            f"fail-closed, blocking entry"
        )
        return True  # fail-closed

    corr_pair_count = 0
    seen_symbols: set[str] = set()  # deduplicate open_positions list

    for pos in open_positions:
        pos_symbol    = pos.get("symbol", "")
        pos_direction = pos.get("direction", "")

        if not pos_symbol:
            logger.warning(
                "[correlation] malformed position entry (missing symbol) — skipping"
            )
            continue

        # Directional-aware: only check same-direction pairs
        if pos_direction != proposed_direction:
            continue

        if pos_symbol == proposed_symbol:
            continue

        # Deduplicate: count each distinct open position symbol once
        if pos_symbol in seen_symbols:
            continue
        seen_symbols.add(pos_symbol)

        pos_returns = _fetch_daily_returns(pos_symbol)
        if pos_returns is None:
            logger.warning(
                f"[correlation] {pos_symbol}: returns unavailable — "
                f"fail-closed, blocking entry for {proposed_symbol}"
            )
            return True  # fail-closed

        rho = _spearman(proposed_returns, pos_returns)
        if rho is None:
            logger.warning(
                f"[correlation] {proposed_symbol} vs {pos_symbol}: "
                f"Spearman failed — fail-closed, blocking entry"
            )
            return True  # fail-closed

        logger.debug(
            f"[correlation] {proposed_symbol}/{proposed_direction} vs "
            f"{pos_symbol}/{pos_direction}: ρ={rho:.3f}"
        )

        # Inclusive boundary (>= 0.7) — conservative per board vote S59;
        # Invariant text says ">0.7" but board chose inclusive to err safe
        if abs(rho) >= _CORR_THRESHOLD:
            corr_pair_count += 1
            logger.info(
                f"[correlation] High correlation: {proposed_symbol} vs "
                f"{pos_symbol} ρ={rho:.3f} (threshold={_CORR_THRESHOLD}) "
                f"[pair {corr_pair_count}/{_MAX_CORRELATED_PAIRS}]"
            )

    if corr_pair_count >= _MAX_CORRELATED_PAIRS:
        logger.info(
            f"[correlation] GATE: {proposed_symbol}/{proposed_direction} "
            f"would create {corr_pair_count} same-direction pairs "
            f"with ρ>{_CORR_THRESHOLD} — blocking entry (Invariant #10)"
        )
        return True

    return False
