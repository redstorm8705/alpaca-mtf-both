# ruff: noqa: E501
"""
execution/reentry_cooldown.py

Re-entry cooldown (2026-08-01, Rafael + BGG). After a STOP-BASED LOSS exit on a name,
do NOT re-enter the SAME symbol in the SAME direction again for the rest of the trading
session (PT calendar day, matching the bot's own traded_today day-boundary). Stops the
"re-short a rising stock on every scan" spam that bled SMCI this week (shorted $24.24 →
stopped $25.35, re-shorted $25.85 → stopped $27.71, re-shorted $27.70 — the SHORT score
sat at 10-12 on nearly every 5-min scan, so the bot kept reloading the same losing short).

BGG-APPROVED design forks (board + Gro + GAI):
  1. TRIGGER  — stop-based LOSS exits only (the exact reasons trade_logger labels "stop_hit":
     hard_stop / trail_stop / gtc_stop_triggered / overnight_atr_buffer_exit / breakeven_stop).
     A target / opposite_signal(#12c) / EOD / external_close exit NEVER triggers a cooldown.
  2. SCOPE    — same symbol + SAME direction only. A genuine OPPOSITE-direction reversal is
     still allowed; every other symbol is unaffected.
  3. DURATION — rest of the trading session (resets next PT day). This is the defensible
     "1-in-10" static exception under the no-static-regimes mandate: it is a discipline RULE
     with ZERO fitted parameters, and the data that would derive a dynamic duration
     (per-(symbol,direction) re-entry-after-stop win rate) does not exist yet.
     ROADMAP: replace with a data-derived duration once ≥30 per-(symbol,direction) stop-outs
     accrue in logs/trade_events.jsonl (same precedent as trade_logger's flagged static throttle).
  4. STATE    — INFERRED from the already-persisted tracker.closed_trades (each carries symbol/
     direction/exit_reason/exit_time). No new state file: it survives the nightly/watchdog
     restart FOR FREE because closed_trades reloads from trade_log.json. The "write" is
     record_exit's existing atomic persistence, so enforcement is reliable when state is healthy.
  5. FAILURE  — FAIL-OPEN. A soft discipline gate must never block the whole book on a scan
     glitch, so any error → (False, None) = allow the entry.

Pure + self-contained + independently testable (anti-silo isolation boundary): the only input
is the closed_trades list. No network I/O, no live-execution imports. NEVER RAISES.
"""
import logging
from datetime import datetime

from execution.state_io import _PT   # same PT zone the tracker stamps exit_time with

logger = logging.getLogger(__name__)

# Reuse the SAME stop-exit classification the tracker/logger use so the cooldown fires on
# exactly the reasons record_exit labels "stop_hit". Fail-safe hardcoded fallback mirrors that
# set if the import ever fails — an import error must never silently disable the whole class.
try:
    from trade_logger import _STOP_REASONS as _STOP_REASONS
except Exception:  # pragma: no cover - defensive
    _STOP_REASONS = frozenset({
        "hard_stop", "trail_stop", "gtc_stop_triggered",
        "overnight_atr_buffer_exit", "breakeven_stop",
    })


def _is_stop_reason(reason) -> bool:
    """True when an exit reason is a stop-based LOSS exit — substring match, mirroring
    record_exit's own `any(s in str(reason) for s in _LOG_STOP_REASONS)` classification."""
    _r = str(reason or "")
    return any(s in _r for s in _STOP_REASONS)


def is_in_cooldown(symbol, direction, closed_trades, now_pt=None):
    """Return (blocked: bool, blocking_trade: dict | None).

    blocked=True iff `closed_trades` contains a trade on the SAME `symbol` + SAME `direction`
    that closed via a STOP-BASED LOSS exit during the CURRENT PT trading day. Reads the
    already-loaded in-memory list (no I/O). FAIL-OPEN: returns (False, None) on any error —
    never blocks an entry on a scan glitch. NEVER RAISES.

    now_pt is injectable for testing; defaults to datetime.now(_PT).
    """
    try:
        _today = (now_pt or datetime.now(_PT)).strftime("%Y-%m-%d")
        for _t in reversed(closed_trades or []):
            try:
                if not isinstance(_t, dict):
                    continue
                if _t.get("symbol") != symbol:
                    continue
                if _t.get("direction") != direction:
                    continue
                if not _is_stop_reason(_t.get("exit_reason")):
                    continue
                # exit_time is a PT ISO timestamp (record_exit writes datetime.now(_PT).isoformat()),
                # so the leading 10 chars are the PT date — compare directly to today's PT date.
                # A missing/short timestamp fails the match (fail-open on that row).
                _xt = str(_t.get("exit_time") or "")
                if _xt[:10] == _today:
                    return True, _t
            except Exception:
                continue   # one malformed row must never abort the scan (fail-open)
        return False, None
    except Exception as _e:  # RC-3: logged, never silent; fail-open
        logger.warning(
            "reentry_cooldown.is_in_cooldown(%s, %s): scan failed (%s) — FAIL-OPEN (allowing entry).",
            symbol, direction, _e,
        )
        return False, None
