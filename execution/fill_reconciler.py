# ruff: noqa: E501
"""
execution/fill_reconciler.py
RC-4 fill reconciliation pass — non-blocking periodic reconciler.

Owns:
  - run_fill_reconciliation()   called from run_cycle after check_exits()

Design:
  After each check_exits() pass, any close that returned a fill_unverified trade
  is held in tracker._unverified_exits. This reconciler polls Alpaca closed orders
  using poll_secs=0.1 + no_retry=True (fills are 5–15 min old — settled). On a hit,
  calls tracker.patch_exit_pnl() to overwrite exit_price + pnl in closed_trades and
  trigger Kelly rebuild. On expiry (window > max_age_minutes), logs CRITICAL + Slack.

  Total blocking per symbol: ~0.1s + API latency.
  Max blocking per cycle (10 open trades): ~1s — well under watchdog threshold.
"""

import logging

from execution.fill_helpers import fetch_actual_fill_price

logger = logging.getLogger(__name__)

_MIN_PRICE_DIFF = 0.001   # ignore trivial float drift in fill comparison


def run_fill_reconciliation(tracker, kelly=None, risk=None) -> None:  # noqa: ARG001
    """RC-4: Non-blocking reconciliation pass — called from run_cycle after check_exits().

    Args:
        tracker: PortfolioTracker instance (owns _unverified_exits + patch_exit_pnl)
        kelly:   KellySizer instance (optional) — passed to patch_exit_pnl for rebuild
        risk:    RiskManager instance (optional) — reserved; adjust_daily_pnl dropped
                 (update_daily_pnl_from_alpaca already overwrites daily_pnl each cycle)
    """
    try:
        pending, expired = tracker.get_unverified_exits(max_age_minutes=5)
    except Exception as _e:
        logger.warning(f"[fill_reconciler] get_unverified_exits failed: {_e}")
        return

    # ── Expired entries — reconciliation window closed ─────────────────────────
    for sym in expired:
        logger.critical(
            f"[{sym}] RC-4 FILL RECONCILIATION EXPIRED: "
            f"fill_unverified trade outside 5-min window — P&L remains unreliable. "
            f"Manual verification required."
        )
        try:
            from alerts import send_slack
            send_slack(
                f":rotating_light: RC-4 FILL RECONCILIATION EXPIRED [{sym}] "
                f"fill_unverified trade outside 5-min reconciliation window. "
                f"P&L for this trade is unreliable — MANUAL VERIFICATION REQUIRED."
            )
        except Exception as _se:
            logger.warning(f"[{sym}] Slack RC-4 expiry alert failed: {_se}")
        # Without _patch_applied_ts, _load_log() re-adds expired entries on every
        # restart → infinite RC-4 CRITICAL warnings for old trades
        try:
            tracker.mark_fill_expired(sym)
        except Exception as _me:
            logger.warning(f"[{sym}] mark_fill_expired failed: {_me}")

    if not pending:
        return

    # ── Pending entries — query Alpaca for settled fill ────────────────────────
    logger.debug(
        f"[fill_reconciler] {len(pending)} pending fill_unverified trade(s): "
        f"{[s for s, _ in pending]}"
    )

    for sym, trade_copy in pending:
        _original_exit_px = float(trade_copy.get("exit_price") or 0.0)
        _original_exit_time = trade_copy.get("exit_time")

        # submitted_after: use exit_time ISO string converted to float if available
        _submitted_after: float | None = None
        _et_str = trade_copy.get("entry_time") or ""
        if _et_str:
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                _dt = datetime.fromisoformat(_et_str)
                if _dt.tzinfo is None:
                    _dt = _dt.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
                _submitted_after = _dt.timestamp()
            except Exception as _date_e:
                logger.warning(
                    "[fill_reconciler] entry_time parse failed for %s (%r) — "
                    "skipping reconciliation this cycle to avoid stale fill match: %s",
                    sym, _et_str[:80], _date_e
                )
                continue

        try:
            _fill = fetch_actual_fill_price(
                symbol=sym,
                trade=trade_copy,
                poll_secs=0.1,
                submitted_after=_submitted_after,
                no_retry=True,
            )
        except Exception as _fe:
            logger.warning(f"[{sym}] fill_reconciler: fetch_actual_fill_price error: {_fe}")
            continue

        # fill_helpers returns entry_price on failure — skip if no real fill found
        _entry_px = float(trade_copy.get("entry_price") or 0.0)
        if abs(_fill - _entry_px) < _MIN_PRICE_DIFF and trade_copy.get("_fill_unverified"):
            logger.debug(
                f"[{sym}] fill_reconciler: fill == entry_price ({_fill:.2f}) "
                f"— Alpaca still unsettled or genuine scratch trade; skipping patch"
            )
            continue

        # Real fill found — patch the closed trade record
        _ok = tracker.patch_exit_pnl(
            symbol=sym,
            exit_price=_fill,
            fill_source="fill_reconciler",
            kelly=kelly,
            original_exit_time=_original_exit_time,
        )
        if not _ok:
            logger.warning(
                f"[{sym}] fill_reconciler: patch_exit_pnl returned False "
                f"(trade may have been already patched or closed record missing)"
            )
