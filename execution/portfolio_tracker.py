# ruff: noqa: E501  — dense logger strings run long (project convention; matches execution/broker.py)
"""
execution/portfolio_tracker.py
Tracks open trades, P&L, day trades, and trade history.

Persistence:
  trade_log.json        — open + closed trades (atomic write with .bak)
  logs/kelly_stats.json — Kelly win/loss data (written by kelly.py)
"""

import json
import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ROOT           = Path(__file__).parent.parent.resolve()   # alpaca-mtf-bot_FINAL/
sys.path.insert(0, str(_ROOT))
# M1 (2026-07-06): stateless FIFO helpers + shared IO extracted to leaf modules.
# DAG: portfolio_tracker → fifo_pnl → state_io (no cycle). Call sites unchanged.
# noqa: E402 — intentional post-sys.path.insert placement (mirrors trade_logger).
from execution.state_io import _PT, _atomic_write, _iso_to_dt  # noqa: E402
from execution.fifo_pnl import (  # noqa: E402
    _fetch_alpaca_fills_for_date,
    _fifo_reconstruct,
    _load_prior_day_lots,
    _load_today_attribution,
    _save_open_lots_state,
)
try:
    from trade_logger import log_event as _log_event, _STOP_REASONS as _LOG_STOP_REASONS
except ImportError:
    def _log_event(*a, **kw): pass   # type: ignore[misc]  # fail-safe: logging never breaks the bot
    _LOG_STOP_REASONS = frozenset()
TRADE_LOG_FILE     = _ROOT / "trade_log.json"

_DRIFT_ALERT_FILE       = _ROOT / "logs" / "last_drift_alert.json"


def _load_drift_alert_date() -> str:
    try:
        if _DRIFT_ALERT_FILE.exists():
            return json.loads(_DRIFT_ALERT_FILE.read_text()).get("date", "")
    except Exception as _e:
        logger.warning(
            "_load_drift_alert_date: load failed (%s) — drift dedup reset", _e
        )
    return ""


_last_eod_drift_alert_date: str = _load_drift_alert_date()  # persisted across restarts

# Same-thread reentrancy guard for write_eod_summary()'s Alpaca-FIFO section
# (GAI audit 2026-06-27 — see write_eod_summary for full rationale).
_eod_fifo_in_progress: bool = False


class PortfolioTracker:
    def __init__(self):
        self.open_trades   = {}    # symbol -> trade dict
        self.closed_trades = []
        self.traded_today  = set()
        self._traded_today_date = datetime.now(_PT).strftime("%Y-%m-%d")
        # RC-4: symbol → list of live trade refs
        self._unverified_exits: dict[str, list[dict]] = {}
        # Guard A consecutive empty-batch skip counter (orphan_manager, 2026-07-04)
        self._reconcile_empty_skips: int = 0
        self._load_log()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_log(self):
        if TRADE_LOG_FILE.exists():
            try:
                with open(TRADE_LOG_FILE) as f:
                    data = json.load(f)
                    _disk_closed = data.get("closed", [])
                    if len(_disk_closed) >= len(self.closed_trades):
                        self.closed_trades = _disk_closed
                    # safe: inside try — rebuilt immediately below
                    self._unverified_exits.clear()
                    # RC-4: rebuild unverified index on restart —
                    # survives os.execv nightly reset
                    for _t in self.closed_trades:
                        if (
                            _t.get("_fill_unverified")
                            and not _t.get("_patch_applied_ts")
                        ):
                            _sym = _t.get("symbol")
                            if _sym:
                                self._unverified_exits.setdefault(_sym, []).append(_t)
                    # Restore open trades if bot restarted mid-session
                    # S47 Bug3 guard (GAI): match on (symbol, entry_time), not
                    # symbol alone — same-day re-entry has a different entry_time
                    # and must NOT warn (symbol-only check causes false positives).
                    _closed_entry_keys = {
                        (t.get("symbol"), t.get("entry_time"))
                        for t in self.closed_trades
                    }
                    for t in data.get("open", []):
                        sym = t.get("symbol")
                        if sym:
                            if (sym, t.get("entry_time")) in _closed_entry_keys:
                                logger.warning(
                                    "[%s] _load_log: same entry_time in open "
                                    "AND closed — double-record (failed pop)? "
                                    "Keeping open. Verify reconcile_eod.py.",
                                    sym,
                                )
                            # S47f Phase 2a.5: Route externally-closed phantom to
                            # closed_trades. Prevents Day 2 entry rejection caused
                            # by a position that closed on Alpaca overnight but was
                            # never removed from open_trades (GTC stop while down).
                            if t.get("_fifo_reconciled_closed"):
                                _t_key = (sym, t.get("entry_time"))
                                if _t_key not in _closed_entry_keys:
                                    self.closed_trades.append(t)
                                    _closed_entry_keys.add(_t_key)
                                logger.info(
                                    "[%s] _load_log: _fifo_reconciled_closed=True"
                                    " — routed to closed_trades (external close,"
                                    " P&L needs manual verification).",
                                    sym,
                                )
                                continue
                            self.open_trades[sym] = t
            except Exception as e:
                logger.warning(f"Could not load trade log: {e}")

    def _save_log(self):
        _atomic_write(TRADE_LOG_FILE, {
            "open":         list(self.open_trades.values()),
            "closed":       self.closed_trades,
            "stats":        self.get_stats(),
            "last_updated": datetime.now(_PT).isoformat(),
        })

    def get_unverified_exits(self, max_age_minutes: int = 5) -> tuple:
        """RC-4: Return pending (symbol, trade_copy) pairs with unverified fills.

        Filters out entries older than max_age_minutes (expired) and entries
        already patched (_patch_applied_ts set). Writes back filtered lists to
        evict expired/patched entries from the secondary index (T1 fix).

        Returns:
            pending:  list[tuple[str, dict]] — (symbol, trade_copy) to reconcile
            expired:  list[str]  — symbols whose window elapsed (log + Slack)
        """
        now    = datetime.now(_PT)
        cutoff = now - timedelta(minutes=max_age_minutes)
        pending:        list[tuple[str, dict]] = []
        expired:        list[str]              = []
        syms_to_remove: list[str]              = []
        for sym, trade_list in self._unverified_exits.items():
            remaining_in_list: list[dict] = []
            for trade in trade_list:
                if not trade.get("_fill_unverified") or trade.get("_patch_applied_ts"):
                    continue
                _exit_str = trade.get("exit_time") or ""
                if not _exit_str:
                    continue
                # RC-4 datetime fix (2026-07-19, BGG HARDENED): tolerant parse (handles the
                # Alpaca Z/variable-fraction filled_at that a raw fromisoformat rejected). A
                # genuinely-unparseable exit_time (None) is NOT silently skipped — it is routed
                # to `expired` so mark_fill_expired surfaces ONE RC-4 CRITICAL and stamps
                # _patch_applied_ts. That breaks the previous SILENT permanent-stuck (the trade
                # was dropped before the expiry check → never reconciled, never marked, re-queued
                # every restart, no alert ever). _iso_to_dt never raises.
                # Explicit None handling is PRIMARY (Reliability seat); the try/except is a
                # backstop that mirrors patch_exit_pnl's (_iso_to_dt never raises, so it is
                # defense-in-depth). On EITHER a None parse or any unexpected error, the trade
                # is routed to EXPIRED — never silently dropped/stuck.
                try:
                    _exit_dt = _iso_to_dt(_exit_str)
                    if _exit_dt is None:
                        # WARNING not CRITICAL: fill_reconciler fires the single authoritative
                        # operator CRITICAL+Slack for this `expired` symbol (cold-2nd 2026-07-19).
                        logger.warning(
                            "[%s] get_unverified_exits: unparseable exit_time %r — routing to "
                            "EXPIRED for manual verification (never silently stuck).",
                            sym, _exit_str,
                        )
                        if sym not in expired:
                            expired.append(sym)
                        continue
                    if _exit_dt.tzinfo is None:  # naive stored time → assume PT (prior behavior)
                        _exit_dt = _exit_dt.replace(tzinfo=_PT)
                except Exception as _e:          # backstop — should not fire; surface, never drop
                    logger.warning(
                        "[%s] get_unverified_exits: unexpected exit_time error %r (%s) — "
                        "routing to EXPIRED.", sym, _exit_str, _e,
                    )
                    if sym not in expired:
                        expired.append(sym)
                    continue
                if _exit_dt < cutoff:
                    if sym not in expired:
                        expired.append(sym)
                    continue
                remaining_in_list.append(trade)
                pending.append((sym, trade.copy()))
            # T1 fix: write back filtered list —
            # evicts expired/patched entries permanently
            self._unverified_exits[sym] = remaining_in_list
            if not remaining_in_list:
                syms_to_remove.append(sym)
        for sym in syms_to_remove:
            self._unverified_exits.pop(sym, None)
        return pending, expired

    def patch_exit_pnl(
        self,
        symbol: str,
        exit_price: float,
        fill_source: str = "reconciliation_pass",
        kelly=None,
        original_exit_time: str | None = None,
    ) -> bool:
        """RC-4: Overwrite exit_price + pnl on a fill_unverified closed trade.

        Searches closed_trades in reverse order (most recent first). Skips trades
        already patched (_patch_applied_ts set) and continues searching so that
        multiple unverified exits for the same symbol (re-entry same session) are
        each individually correctable (T6 fix).

        Uses _qty_at_close (pre-close remaining qty) to compute correct P&L for
        partial-exit trades where qty_remaining has already been zeroed (T1 fix).

        Updates pnl_pct using original full position qty (T7 fix).

        Returns True on success, False if no matching unverified trade found.
        """
        _trade: dict | None = None
        for _t in reversed(self.closed_trades):
            if _t.get("symbol") != symbol:
                continue
            if not _t.get("_fill_unverified"):
                continue
            if _t.get("_patch_applied_ts"):
                continue   # T6 fix: skip patched, keep searching for older unpatched
            if original_exit_time and _t.get("exit_time") != original_exit_time:
                continue
            _trade = _t
            break

        if _trade is None:
            logger.warning(
                f"[{symbol}] patch_exit_pnl: no _fill_unverified trade found"
            )
            return False

        if exit_price <= 0:
            logger.error(
                "[patch_exit_pnl] Invalid exit_price=%.4f for %s"
                " — skipping correction.",
                exit_price, symbol,
            )
            return False

        _original_exit_px = float(_trade.get("exit_price") or 0.0)
        _original_pnl     = float(_trade.get("pnl") or 0.0)
        _entry_px         = float(_trade.get("entry_price") or 0.0)

        # BUG-1 fix: 'or'-chain treats _qty_at_close=0 as falsy → falls back to
        # original qty. Use 'is not None' sentinel to distinguish legitimate zero
        # (fully partial-exited position) from absent field (legacy trade).
        # DS/GAI: add try/except + float() for type safety on non-numeric stored values.
        _qty_at_close_raw = _trade.get("_qty_at_close")
        try:
            if _qty_at_close_raw is not None and _qty_at_close_raw != "":
                _qty = int(float(_qty_at_close_raw))
            else:
                _qty = int(_trade.get("qty") or 0)
        except (ValueError, TypeError) as _qe:
            logger.warning(
                "[patch_exit_pnl] Non-numeric _qty_at_close %r for %s — "
                "falling back to original qty: %s",
                _qty_at_close_raw, symbol, _qe,
            )
            _qty = int(_trade.get("qty") or 0)
        if _qty < 0:
            logger.warning(
                "[patch_exit_pnl] Negative _qty=%d for %s — clamping to 0.",
                _qty, symbol,
            )
            _qty = 0

        _orig_qty_full = int(_trade.get("qty") or 0)
        _direction     = _trade.get("direction", "long")
        _dir_mult      = 1 if _direction == "long" else -1
        _partial_pnl   = float(_trade.get("partial_pnl") or 0.0)

        # GAI cross-bug guard: if _entry_px=0.0 (pending_overnight never promoted),
        # (exit_price - 0) * qty produces phantom profit equal to gross proceeds.
        # Preserve only partial_pnl; skip remaining-share P&L calculation.
        if _entry_px <= 0:
            logger.critical(
                "[patch_exit_pnl] _entry_px=%.4f is invalid for %s — "
                "cannot compute fill correction. Preserving partial_pnl=%.2f only.",
                _entry_px, symbol, _partial_pnl,
            )
            _new_pnl_remaining = 0.0
            _new_total_pnl     = round(_partial_pnl, 4)  # 4dp storage
        else:
            _new_pnl_remaining = round((exit_price - _entry_px) * _qty * _dir_mult, 4)
            _new_total_pnl     = round(_new_pnl_remaining + _partial_pnl, 4)  # 4dp

        # RC-4 datetime fix (2026-07-19): tolerant parse. _delay_secs is a LOG METRIC only —
        # it never feeds P&L (that comes from exit_price+entry_price below), so a None/naive
        # timestamp just leaves the metric 0.0. Explicit None + tz guards (Data-integrity seat)
        # so the aware/naive subtraction can't TypeError; the try/except is a belt-and-suspenders.
        _delay_secs = 0.0
        try:
            _exit_dt = _iso_to_dt(_trade.get("exit_time") or "")
            if _exit_dt is not None:
                if _exit_dt.tzinfo is None:      # naive → assume PT so the subtraction is aware/aware
                    _exit_dt = _exit_dt.replace(tzinfo=_PT)
                _delay_secs = (datetime.now(_PT) - _exit_dt).total_seconds()
            else:
                logger.warning(
                    "[patch_exit_pnl] unparseable exit_time %r for %s — delay metric left 0.0",
                    _trade.get("exit_time"), symbol,
                )
        except Exception as _e:
            logger.warning(
                "[patch_exit_pnl] Could not parse exit_time for %s: %s",
                symbol, _e,
            )

        _trade["exit_price"]         = exit_price
        _trade["pnl"]                = _new_total_pnl
        _trade["pnl_remaining"]      = _new_pnl_remaining
        _trade["_fill_unverified"]   = False
        _trade["_patch_applied_ts"]  = datetime.now(_PT).isoformat()
        _trade["_patch_fill_source"] = fill_source

        # T7 fix: update pnl_pct using original full qty (not zeroed qty_remaining)
        if _orig_qty_full > 0 and _entry_px > 0:
            _trade["pnl_pct"] = round(
                (_new_total_pnl / (_entry_px * _orig_qty_full)) * 100, 2
            )

        # Evict from secondary index — already patched, no longer pending
        if symbol in self._unverified_exits:
            self._unverified_exits[symbol] = [
                t for t in self._unverified_exits[symbol]
                if t.get("_fill_unverified") and not t.get("_patch_applied_ts")
            ]
            if not self._unverified_exits[symbol]:
                del self._unverified_exits[symbol]

        # TQI REPAIR (2026-07-16, board + Gro + GAI). The score recorded at exit came
        # from the FABRICATED pnl (entry_price fallback -> 0.00), so _record_tqi did NOT
        # add it to kelly._tqi_history. The P&L is VERIFIED now — recompute and append.
        # Why: _tqi_history feeds AB-3 (entry_logic.py:1128-1150) which does
        # `dollar_cap *= _tqi_kelly_adj`. _compute_tqi gives r_mult>=0 -> 10 pts but a
        # REAL LOSS -> 0, so a fabricated score inflates the average and sizes LARGER
        # (measured: RIVN's true -$41 scored 33/100 vs a true 23/100 = +10).
        # No double-count: _record_tqi appends only when NOT _fill_unverified; this runs
        # only for a trade that WAS (idempotent via _patch_applied_ts) — mutually
        # exclusive (GAI). Lazy import: exit_logic imports this module (circular at
        # top level). Fail-safe: TQI is SECONDARY — the P&L patch above is already
        # committed and must never be undone by a TQI failure (Gro + GAI required).
        try:
            from execution.exit_logic import _compute_tqi as _compute_tqi_patched
            _new_tqi = _compute_tqi_patched(_trade)
            _trade["tqi_score"] = _new_tqi
            if kelly is not None:
                kelly.append_tqi(_new_tqi)
                logger.info(
                    "[%s] TQI repaired after fill verification: %d/100 (from the true "
                    "P&L $%.2f) — appended to the rolling AB-3 history.",
                    symbol, _new_tqi, _new_total_pnl,
                )
        except Exception as _tqi_e:
            logger.warning(
                "[%s] patch_exit_pnl: TQI repair failed (%s) — P&L patch stands; this "
                "trade simply contributes no TQI to the rolling history.",
                symbol, _tqi_e,
            )

        self._save_log()

        if kelly is not None:
            try:
                kelly.rebuild_from_trades(self.closed_trades)
            except Exception as _ke:
                logger.warning(
                    f"[{symbol}] patch_exit_pnl: Kelly rebuild failed: {_ke}"
                )

        try:
            _log_event(
                "exit_pnl_correction",
                symbol=symbol, price=exit_price, size=_qty,
                score=_trade.get("score", 0),
                mri_level=_trade.get("mri_level", "NORMAL"),
                data_source=fill_source,
                original_exit_price=_original_exit_px,
                corrected_exit_price=exit_price,
                original_pnl=_original_pnl,
                corrected_pnl=_new_total_pnl,
                correction_delay_secs=round(_delay_secs, 1),
            )
        except Exception as _le:
            logger.warning(
                f"[{symbol}] patch_exit_pnl: trade_events.jsonl write failed: {_le}"
            )

        logger.warning(
            f"[{symbol}] RC-4 fill_correction: "
            f"exit {_original_exit_px:.2f}→{exit_price:.2f} "
            f"| pnl {_original_pnl:.2f}→{_new_total_pnl:.2f} "
            f"| delay={_delay_secs:.0f}s | src={fill_source}"
        )
        return True

    def mark_fill_expired(self, symbol: str) -> bool:
        """RC-4 loop fix: mark expired-but-unverified exits to stop
        _load_log() re-queuing.
        Only marks trades older than 5 minutes (same cutoff as get_unverified_exits).
        Fresh trades (< 5 min old) are skipped — left for patch_exit_pnl to handle.
        """
        now    = datetime.now(_PT)
        cutoff = now - timedelta(minutes=5)
        found  = False
        for _t in reversed(self.closed_trades):
            if _t.get("symbol") != symbol:
                continue
            if not _t.get("_fill_unverified"):
                continue
            if _t.get("_patch_applied_ts"):
                continue
            _exit_str = _t.get("exit_time", "")
            # RC-4 datetime fix (2026-07-19, BGG HARDENED): tolerant parse. Explicit None handling
            # is PRIMARY; the try/except is a backstop (mirrors patch_exit_pnl; _iso_to_dt never
            # raises). On None OR any unexpected error, MARK expired (stamp _patch_applied_ts FIRST
            # so get_unverified_exits + _load_log exclude it → the RC-4 alert fires once, no re-queue).
            try:
                _exit_dt = _iso_to_dt(_exit_str)
                if _exit_dt is None:
                    _t["_patch_applied_ts"]       = now.isoformat()
                    _t["_fill_reconcile_expired"] = True
                    found = True
                    logger.warning(     # WARNING: fill_reconciler emits the one operator CRITICAL+Slack
                        "[%s] mark_fill_expired: unparseable exit_time %r — marked expired for "
                        "manual verification (breaks the re-queue loop).",
                        symbol, _exit_str,
                    )
                    continue
                if _exit_dt.tzinfo is None:
                    _exit_dt = _exit_dt.replace(tzinfo=_PT)
                else:
                    _exit_dt = _exit_dt.astimezone(_PT)
                if _exit_dt >= cutoff:
                    continue  # fresh trade — leave for patch_exit_pnl
            except Exception as _e:      # backstop — should not fire; mark expired, never loop
                _t["_patch_applied_ts"]       = now.isoformat()
                _t["_fill_reconcile_expired"] = True
                found = True
                logger.warning(
                    "[%s] mark_fill_expired: unexpected exit_time error %r (%s) — marked expired.",
                    symbol, _exit_str, _e,
                )
                continue
            _t["_patch_applied_ts"]       = now.isoformat()
            _t["_fill_reconcile_expired"] = True
            found = True
        if found:
            self._save_log()
            logger.warning(
                f"[{symbol}] RC-4 fill_expired: _patch_applied_ts set — "
                f"will not re-queue on next restart"
            )
        return found

    # ── EOD summary ───────────────────────────────────────────────────────────

    def write_eod_summary(self, kelly_sizer=None):
        """
        Write a daily performance snapshot to logs/eod_YYYY-MM-DD.json.
        Called at EOD. Each file = one trading day. Feeds weekly_review.html.

        Phase 2: Alpaca fills FIFO is the authoritative source for pnl_today
        and all_time_stats.total_pnl. Tracker math runs in parallel for drift
        monitoring. Falls back to tracker on API failure (CRITICAL log + Slack).
        Dual-compute runs for 10 trading days (Phase 2a validation gate per Shaw).
        """
        today = datetime.now(_PT).strftime("%Y-%m-%d")
        eod_path = _ROOT / "logs" / f"eod_{today}.json"
        # Finding 6: anchor to project root, not CWD
        (_ROOT / "logs").mkdir(exist_ok=True)
        self._load_log()  # force reload — captures orphan/SIGKILL-reconciled exits

        today_trades = [
            t for t in self.closed_trades
            if t.get("exit_time", "").startswith(today)
        ]

        # PNL-DAYFIX: single source of truth for "what portion of this trade's P&L
        # belongs to today."  For overnight positions whose partial exit fired BEFORE
        # today, use only pnl_remaining to avoid double-counting the prior-day partial.
        # All other trades (same-day entries, same-day partials) use the full t["pnl"].
        # Called by today_pnls, score_buckets, and score_16pt_buckets —
        # must stay in sync.
        def _day_slice(t):
            _entry_date      = (t.get("entry_time") or "")[:10]
            _partial_exit_dt = (t.get("partial_exit_time") or "")[:10]
            _partial_pnl     = t.get("partial_pnl") or 0.0
            _pnl_rem         = t.get("pnl_remaining")
            if (
                _entry_date and _entry_date < today
                and abs(_partial_pnl) > 1e-8  # float exact-zero guard
                and _partial_exit_dt
                and _partial_exit_dt < today
                and _pnl_rem is not None
            ):
                return _pnl_rem
            return t.get("pnl", 0)

        today_pnls = [_day_slice(t) for t in today_trades]

        # Include partial_pnl from open positions whose last partial exit was today.
        # Partial exits are realized gains but the position stays in open_trades until
        # fully closed — they never appear in closed_trades / pnl_today otherwise.
        partial_realized_today = sum(
            float(t.get("partial_pnl", 0) or 0)
            for t in self.open_trades.values()
            if t.get("partial_exited")
            and (t.get("partial_exit_time") or "")[:10] == today
        )

        score_buckets = {}
        for t in today_trades:
            _sc = t.get('score', 0)
            bucket = f"{(_sc // 2) * 2}-{(_sc // 2) * 2 + 1}"
            if bucket not in score_buckets:
                score_buckets[bucket] = {"wins": 0, "losses": 0, "pnl": 0}
            _s = _day_slice(t)
            if _s > 0:
                score_buckets[bucket]["wins"] += 1
            else:
                score_buckets[bucket]["losses"] += 1
            score_buckets[bucket]["pnl"] = round(score_buckets[bucket]["pnl"] + _s, 2)

        # ── Tracker P&L (internal math — dual-compute cross-check) ──────────────
        _tracker_pnl = round(sum(today_pnls) + partial_realized_today, 2)

        # Consistency guard: score_bucket P&L sum must equal today_pnls sum.
        # A nonzero delta means a new P&L consumer forgot to call _day_slice().
        _bucket_sum = round(sum(v["pnl"] for v in score_buckets.values()), 2)
        _slice_sum  = round(sum(today_pnls), 2)
        if score_buckets and abs(_bucket_sum - _slice_sum) > 0.02:
            logger.warning(
                f"EOD INTERNAL DRIFT: score_buckets sum ${_bucket_sum:.2f} != "
                f"today_pnls sum ${_slice_sum:.2f} — check _day_slice() coverage"
            )

        # ── Phase 2a: Alpaca fills FIFO (authoritative source) ────────────────
        _alpaca_pnl       = None
        _alpaca_lots      = {}
        _alpaca_per_trade = []
        _alpaca_cumulative = None

        global _eod_fifo_in_progress
        if _eod_fifo_in_progress:
            # Historical note: this guard was added under the assumption that
            # Python resumes the interrupted call after the SIGTERM handler
            # returns. Re-verified 2026-06-28 (MTF FULL BOT AUDIT, Gro+GAI
            # both reversed their original position after rigorous review):
            # main.py's _handle_sigterm() ends with sys.exit(0), which raises
            # SystemExit and unwinds the entire call stack rather than
            # returning — the interrupted call does NOT resume, so the
            # specific same-thread-resumption race this comment used to
            # describe is not reachable via SIGTERM in this codebase. Left in
            # place as harmless defense-in-depth; do not widen further based
            # on the original (incorrect) rationale. Plain bool, not
            # threading.Lock — there was never a cross-thread concern here.
            logger.warning(
                "write_eod_summary: Alpaca-FIFO section already in progress "
                "(signal-handler reentrancy) — skipping to avoid clobbering "
                "the in-flight lot-state save. Falling back to tracker P&L "
                "for this call only."
            )
        else:
            _eod_fifo_in_progress = True
            try:
                _prior_lots, _processed_fill_ids = _load_prior_day_lots()
                # Bridge (2026-07-06 S-FIFO): baseline of today's already-
                # attributed FIFO P&L/per_trade from any earlier same-day run —
                # accumulated onto below so repeat runs don't falsely report
                # attributed=0 / understate the Alpaca cumulative.
                _prior_today_pnl, _prior_today_per_trade = _load_today_attribution()
                _day_fills        = _fetch_alpaca_fills_for_date(today)

                # ── FIFO Orphan Lot Seeding ──────────────────────────────────────
                # Seed prior lots for positions whose opening fills are absent from
                # today's Alpaca fills (externally purchased before bot tracking).
                # Without seeding, FIFO treats closing sells as new short openings
                # → P&L = $0.
                #
                # Guard 1: skip if symbol already in prior_lots (normal carry).
                # Guard 2: skip if Alpaca has an opening fill today — intraday
                #           trades are handled natively by FIFO.
                # Guard 3: accumulate ALL closing fills before seeding
                #           (partial-fill sequence safety).
                # Guard 4: direction-aware — only accumulate fills that close
                #           the tracked side.
                _long_opened_today  = {
                    f["symbol"] for f in _day_fills if f.get("side") == "buy"
                }
                _short_opened_today = {
                    f["symbol"] for f in _day_fills
                    if f.get("side") == "sell_short"
                }
                from execution.quarterly_hold_manager import get_quarterly_hold_symbols
                _qhm_oseed = get_quarterly_hold_symbols()
                _orphan_seed_map: dict = {}
                for _fill in _day_fills:
                    _fsym  = _fill.get("symbol", "")
                    if _fsym in _qhm_oseed:
                        continue   # QHM tracked separately — skip orphan seed/warning
                    _fside = _fill.get("side", "")
                    if not _fsym or _fsym in _prior_lots:
                        continue
                    _fqty = int(float(_fill.get("qty") or 0))
                    if _fqty <= 0:
                        continue
                    if _fsym not in _orphan_seed_map:
                        _tr = next(
                            (t for t in today_trades if t.get("symbol") == _fsym),
                            None,
                        ) or next(
                            (v for v in self.open_trades.values()
                             if v.get("symbol") == _fsym),
                            None,
                        )
                        if _tr is None:
                            logger.warning(
                                f"FIFO orphan: closing fill for {_fsym} has no "
                                f"prior lot and no tracker record — fill unmatched."
                            )
                            _orphan_seed_map[_fsym] = None  # sentinel
                            continue
                        _entry = float(_tr.get("entry_price") or 0.0)
                        if _entry <= 0:
                            logger.warning(
                                f"FIFO orphan: {_fsym} entry_price not confirmed"
                                f" — deferring seed."
                            )
                            _orphan_seed_map[_fsym] = None  # sentinel
                            continue
                        _dir = _tr.get("direction", "long")
                        # Guard 2: intraday — opening fill exists today
                        if ((_dir == "long" and _fsym in _long_opened_today) or
                                (_dir == "short" and _fsym in _short_opened_today)):
                            _orphan_seed_map[_fsym] = None  # sentinel: intraday
                            continue
                        _orphan_seed_map[_fsym] = {
                            "qty": 0, "direction": _dir, "entry": _entry,
                        }
                    _seed = _orphan_seed_map.get(_fsym)
                    if _seed is None:
                        continue  # sentinel — skip all fills for this symbol
                    _dir = _seed["direction"]
                    # Guard 4: only closing fills for the tracked direction
                    _is_closing = (
                        (_dir == "long"
                         and _fside in {"sell", "sell_short"}) or
                        (_dir == "short"
                         and _fside in {"buy", "buy_to_cover"})
                    )
                    if _is_closing:
                        _seed["qty"] += _fqty
                for _fsym, _seed in _orphan_seed_map.items():
                    if _seed is None or _seed["qty"] <= 0:
                        continue
                    _prior_lots[_fsym] = [{
                        "qty":   _seed["qty"],
                        "price": _seed["entry"],
                        "side":  _seed["direction"],
                    }]
                    logger.info(
                        f"FIFO orphan lot seeded: {_fsym} {_seed['qty']}sh "
                        f"@ ${_seed['entry']:.2f} ({_seed['direction']}) — "
                        f"no prior lot found (orphan/legacy entry)"
                    )
                # ── end orphan seeding ───────────────────────────────────────────

                _alpaca_pnl, _alpaca_lots, _alpaca_per_trade, _seen_fill_ids = (
                    _fifo_reconstruct(_day_fills, _prior_lots, _processed_fill_ids)
                )
                # Accumulate this run's newly-attributed fills onto the day's
                # persisted baseline (2026-07-06 S-FIFO): _fifo_reconstruct only
                # attributes fills NOT already in processed_fill_ids, so on a
                # repeat same-day run its per_trade is empty and today_pnl is $0.
                # Adding the earlier run's attribution reconstitutes the full-day
                # total — correcting the false A-4 unattributed flag AND the
                # understated Alpaca cumulative. No double-count: a fill is in the
                # baseline only if it is in processed_fill_ids, which this run skips.
                _alpaca_pnl       = round(_prior_today_pnl + _alpaca_pnl, 2)
                _alpaca_per_trade = _prior_today_per_trade + _alpaca_per_trade
                _save_open_lots_state(
                    _alpaca_lots, today, _seen_fill_ids,
                    today_pnl=_alpaca_pnl, per_trade=_alpaca_per_trade,
                )

                # Cumulative total = prior EOD total + today's Alpaca P&L
                for _back in range(1, 8):
                    _prev_date = (
                        datetime.strptime(today, "%Y-%m-%d") - timedelta(days=_back)
                    ).strftime("%Y-%m-%d")
                    _prev_eod_path = _ROOT / "logs" / f"eod_{_prev_date}.json"
                    if _prev_eod_path.exists():
                        try:
                            with open(_prev_eod_path) as _f:
                                _prev_data = json.load(_f)
                            _prev_total = float(
                                _prev_data.get(
                                    "all_time_stats", {}
                                ).get("total_pnl") or 0.0
                            )
                            _alpaca_cumulative = round(_prev_total + _alpaca_pnl, 2)
                            break
                        except Exception as _load_err:
                            logger.warning(
                                f"Failed to load prior EOD file {_prev_eod_path} — "
                                f"trying an earlier day: {_load_err}"
                            )
                if _alpaca_cumulative is None:
                    _alpaca_cumulative = _alpaca_pnl  # first trading day

                logger.info(
                    f"Alpaca FIFO EOD: pnl_today=${_alpaca_pnl:.2f} | "
                    f"cumulative=${_alpaca_cumulative:.2f} | "
                    f"tracker=${_tracker_pnl:.2f} | "
                    f"fills={len(_day_fills)} lots_closed={len(_alpaca_per_trade)}"
                )
            except Exception as _fifo_err:
                logger.critical(
                    f"Alpaca fills FIFO failed — falling back to tracker P&L: "
                    f"{_fifo_err}"
                )
                try:
                    from alerts import send_slack as _sl_fifo
                    _sl_fifo(
                        ":rotating_light: *EOD P&L: Alpaca FIFO failed"
                        " — using tracker math*\n"
                        f"Error: `{str(_fifo_err)[:200]}`\n"
                        f"Tracker P&L: ${_tracker_pnl:.2f}. Verify fills manually."
                    )
                except Exception as _slack_err:
                    logger.warning(
                        f"Slack alert for FIFO failure could not be sent: {_slack_err}"
                    )
            finally:
                _eod_fifo_in_progress = False

        # Authoritative P&L — Alpaca wins; tracker is fallback.
        # A-4/FIFO gap: Alpaca P&L is $0 but there ARE closed trades AND either
        #   (a) zero same-day fills (paper settlement delay — original A-4), OR
        #   (b) fills exist but FIFO attributed $0 to ALL of them
        #       (len(_alpaca_per_trade) == 0 — orphan-seed failed, closing sells
        #        reconstructed as synthetic-short $0). This is the phantom-$0 bug
        #        (e.g. 2026-07-02: Alpaca $0.00 vs tracker -$251.12).
        # In EITHER case $0 is NOT authoritative: fall back to tracker P&L AND
        # flag the day unreconciled so reconcile_eod finalizes it and downstream
        # renders "review" — never a silent authoritative $0.
        # (P0 fix 2026-07-05, board + Gro + GAI: tracker+flag over None to avoid
        #  a None impact-radius across Kelly / kill-switch / get_stats.)
        _a4_gap = (
            _alpaca_pnl is not None
            and _alpaca_pnl == 0.0
            and len(today_trades) > 0
            and (len(_day_fills) == 0 or len(_alpaca_per_trade) == 0)
        )
        _pnl_unreconciled = False
        _pnl_unreconciled_reason: Optional[str] = None
        if _a4_gap:
            _pnl_unreconciled = True
            _pnl_unreconciled_reason = (
                "alpaca_zero_fills" if len(_day_fills) == 0
                else "alpaca_fifo_unattributed"
            )
            logger.warning(
                "A-4/FIFO gap: Alpaca P&L=$0 despite %d closed trade(s) "
                "(fills=%d, attributed=%d, reason=%s) — using tracker P&L "
                "($%.2f) as FLAGGED-unreconciled fallback; reconcile_eod will "
                "finalize. Verify fills manually.",
                len(today_trades), len(_day_fills), len(_alpaca_per_trade),
                _pnl_unreconciled_reason, _tracker_pnl,
            )
            try:
                from alerts import send_slack as _sl_a4
                _sl_a4(
                    f":warning: *EOD P&L unreconciled* — Alpaca FIFO returned $0 "
                    f"with {len(today_trades)} closed trade(s) "
                    f"(fills={len(_day_fills)}, attributed={len(_alpaca_per_trade)}, "
                    f"reason={_pnl_unreconciled_reason}). Using tracker "
                    f"${_tracker_pnl:.2f} (flagged); reconcile_eod will correct."
                )
            except Exception as _sl_err:
                logger.warning("A-4 unreconciled Slack alert failed: %s", _sl_err)
        # Authoritative pnl_today from pnl_ledger (2026-07-10, board + Gro + GAI).
        # pnl_ledger recomputes realized P&L via PURE FIFO over the COMPLETE Alpaca
        # fill log — the phantom-proof source that replaced the RC-4 phantom-fill
        # class (the 2026-07-02 -$251.12-vs-+$41.08 corruption). GATED on its
        # reconciliation invariant (realized+unrealized ~= equity-deposits, $5 tol):
        # on invariant FAIL / error we fall back to the existing dual-compute exactly
        # as before — never write a ledger number that does not reconcile to Alpaca
        # equity. pnl_today keeps its INTRADAY meaning (per_day_intraday); QHM realized
        # and the reconciles-to-equity TOTAL are separate fields (heal_history schema).
        # `today` is the PT date (the eod file's own date). pnl_ledger keys per_day by
        # PT date too (see pnl_ledger._pt_date — converts Alpaca's UTC transaction_time
        # to PT so after-hours/overnight fills bucket into the correct trading day), so
        # per_day_intraday[today] is the correct PT-day realized P&L.
        #
        # DEFAULT = the exact prior dual-compute behavior (so _pnl_today is ALWAYS
        # bound and the ledger only ever OVERWRITES a known-good fallback on success).
        _pnl_today = (
            _tracker_pnl if _a4_gap
            else (_alpaca_pnl if _alpaca_pnl is not None else _tracker_pnl)
        )
        _pnl_today_qhm: Optional[float] = None
        _pnl_today_total: Optional[float] = None
        _pnl_ledger_authoritative = False
        try:
            from reporting.pnl_ledger import build_ledger as _build_pnl_ledger
            _led = _build_pnl_ledger()
            _led_inv = _led.get("invariant", {})
            _led_pdi = _led.get("per_day_intraday", {}) or {}
            _led_pd = _led.get("per_day", {}) or {}
            # A-4-gap protection (preserved): a day absent from the ledger's per_day
            # means $0 realized that day — correct to write authoritatively ONLY when
            # there were no closed trades today. If the tracker DID book closes today
            # but the (equity-reconciling) ledger shows nothing for today, that is a
            # real discrepancy — fall back + flag rather than assert authoritative $0.
            if (
                _led_inv.get("ok")
                and today not in _led_pd
                and today not in _led_pdi
                and len(today_trades) > 0
            ):
                raise RuntimeError(
                    f"ledger invariant ok but 0 per-day realized for {today} "
                    f"despite {len(today_trades)} closed trade(s) — keep fallback"
                )
            if _led_inv.get("ok"):
                _pi = _led_pdi.get(today, 0.0)
                _pq = _led.get("per_day_qhm", {}).get(today, 0.0)
                _pt = _led_pd.get(today, 0.0)
                _pnl_today = round(float(_pi), 2)
                _pnl_today_qhm = round(float(_pq), 2)
                _pnl_today_total = round(float(_pt), 2)
                _pnl_unreconciled = False
                _pnl_unreconciled_reason = None
                _pnl_ledger_authoritative = True
                logger.info(
                    "pnl_today from pnl_ledger (authoritative): $%.2f intraday "
                    "($%.2f qhm / $%.2f total) invariant ok drift=$%.2f",
                    _pnl_today, _pnl_today_qhm, _pnl_today_total,
                    _led_inv.get("drift", 0.0),
                )
            else:
                raise RuntimeError(
                    f"pnl_ledger invariant FAIL drift=${_led_inv.get('drift')}"
                )
        except Exception as _led_err:
            # _pnl_today already holds the dual-compute default set above.
            logger.warning(
                "pnl_ledger unavailable/invariant-fail for %s — dual-compute "
                "fallback kept: %s", today, _led_err,
            )
        _pnl_drift = (
            round(_alpaca_pnl - _tracker_pnl, 2) if _alpaca_pnl is not None else None
        )

        # Drift alert (Majors requirement: >$1.00 triggers Slack WARNING)
        # A-4 gap: suppress — we already chose tracker fallback and logged the reason.
        if _pnl_drift is not None and abs(_pnl_drift) > 1.00 and not _a4_gap:
            logger.warning(
                f"EOD P&L DRIFT: Alpaca=${_alpaca_pnl:.2f} "
                f"tracker=${_tracker_pnl:.2f} drift=${_pnl_drift:+.2f}"
            )
            global _last_eod_drift_alert_date
            _today_dedup = datetime.now(_PT).strftime("%Y-%m-%d")
            if _last_eod_drift_alert_date != _today_dedup:
                _last_eod_drift_alert_date = _today_dedup
                _atomic_write(_DRIFT_ALERT_FILE, {"date": _today_dedup})
                try:
                    from alerts import send_slack as _sl_drift
                    _sl_drift(
                        f":warning: *EOD P&L drift detected (>$1.00 threshold)*\n"
                        f"Alpaca FIFO: ${_alpaca_pnl:.2f}"
                        f" | Tracker: ${_tracker_pnl:.2f} | "
                        f"Drift: ${_pnl_drift:+.2f}\n"
                        f"EOD file uses Alpaca as authoritative source."
                    )
                except Exception as _slack_err:
                    logger.warning(f"Slack drift alert could not be sent: {_slack_err}")

        # Build corrected all_time_stats — override total_pnl with Alpaca cumulative
        _stats = self.get_stats()
        if _alpaca_cumulative is not None:
            _stats["total_pnl"] = _alpaca_cumulative

        summary = {
            "date":           today,
            "trades_today":   len(today_trades),
            "pnl_today":      _pnl_today,
            # pnl_ledger authoritative fields (2026-07-10) — match heal_history schema.
            "pnl_today_qhm":            _pnl_today_qhm,
            "pnl_today_total":          _pnl_today_total,
            "pnl_ledger_authoritative": _pnl_ledger_authoritative,
            "win_rate_today": round(
                len([p for p in today_pnls if p > 0]) / len(today_pnls) * 100, 1
            ) if today_pnls else 0,
            "score_buckets":  score_buckets,
            "all_time_stats": _stats,
            "trades":         today_trades,
            # Phase 2a dual-compute fields (10-day validation window per Shaw)
            "alpaca_pnl":      _alpaca_pnl,
            "tracker_pnl":     _tracker_pnl,
            "pnl_drift":       _pnl_drift,
            "alpaca_per_trade": _alpaca_per_trade,
            # P0 2026-07-05: flag days where Alpaca FIFO gave $0 but trades exist.
            # pnl_today carries the tracker fallback; reconcile_eod clears the flag.
            "pnl_unreconciled":        _pnl_unreconciled,
            "pnl_unreconciled_reason": _pnl_unreconciled_reason,
        }

        # 16pt score bucket analysis
        score_16pt_buckets = {}
        for t in today_trades:
            s16 = t.get("score_16pt")
            if s16 is not None:
                bucket = f"{(s16 // 2) * 2}-{(s16 // 2) * 2 + 1}"
                if bucket not in score_16pt_buckets:
                    score_16pt_buckets[bucket] = {"wins": 0, "losses": 0, "pnl": 0}
                _s16 = _day_slice(t)
                if _s16 > 0:
                    score_16pt_buckets[bucket]["wins"] += 1
                else:
                    score_16pt_buckets[bucket]["losses"] += 1
                score_16pt_buckets[bucket]["pnl"] = round(
                    score_16pt_buckets[bucket]["pnl"] + _s16, 2
                )
        summary["score_16pt_buckets"] = score_16pt_buckets

        # Score comparison log
        # Schema: {"date": str, "scan_time": str, "trade_mode": str, "universe": str,
        #          "tickers": [{"symbol": str, "long_12pt": int, "short_12pt": int,
        #                       "long_16pt": int, "short_16pt": int, ...}, ...]}
        # Written by scan_to_html.py at EOD. avg_12pt/16pt = mean of max(long, short)
        # per ticker (best directional score — McKinney: correct aggregation
        # for mixed scanner).
        score_cmp_path = _ROOT / "logs" / f"score_comparison_{today}.json"
        if score_cmp_path.exists():
            try:
                with open(score_cmp_path) as _f:
                    cmp_data = json.load(_f)
                if isinstance(cmp_data, dict):
                    # Current format (S28+): dict wrapper with per-symbol tickers list
                    tickers = cmp_data.get("tickers")
                    if isinstance(tickers, list) and tickers:
                        avg_12 = sum(
                            max(
                                int(e.get("long_12pt") or 0),
                                int(e.get("short_12pt") or 0),
                            )
                            for e in tickers
                        ) / len(tickers)
                        avg_16 = sum(
                            max(
                                int(e.get("long_16pt") or 0),
                                int(e.get("short_16pt") or 0),
                            )
                            for e in tickers
                        ) / len(tickers)
                        summary["score_comparison_summary"] = {
                            "scans_logged": len(tickers),
                            "avg_12pt":     round(avg_12, 2),
                            "avg_16pt":     round(avg_16, 2),
                            "scan_date":    cmp_data.get("date", today),
                            "trade_mode":   cmp_data.get("trade_mode", ""),
                        }
                        logger.info(
                            "EOD score_comparison: %d tickers | avg_12pt=%.2f"
                            " | avg_16pt=%.2f",
                            len(tickers), avg_12, avg_16,
                        )
                    # Empty tickers list → scanner ran but found no candidates; silent
                elif isinstance(cmp_data, list) and cmp_data:
                    # Legacy format (pre-S28): list of {score_12pt, score_16pt}
                    avg_12 = sum(
                        e.get("score_12pt", 0) for e in cmp_data
                    ) / len(cmp_data)
                    avg_16 = sum(
                        e.get("score_16pt", 0) for e in cmp_data
                    ) / len(cmp_data)
                    summary["score_comparison_summary"] = {
                        "scans_logged": len(cmp_data),
                        "avg_12pt":     round(avg_12, 2),
                        "avg_16pt":     round(avg_16, 2),
                    }
                elif cmp_data is not None:
                    logger.warning(
                        "write_eod_summary: score_comparison unexpected"
                        " type %s — skipped",
                        type(cmp_data).__name__,
                    )
            except Exception as _e:
                logger.warning(
                    "write_eod_summary: score_comparison load failed"
                    " (%s) — skipped",
                    _e,
                )

        summary["signals_skipped"]  = getattr(self, "_signals_skipped_today", [])
        # ── S47f Phase 2a.5: FIFO-driven overnight reconciliation ────────────────
        # Detects overnight positions that closed on Alpaca while the bot was down
        # (e.g. GTC stop fired) by comparing self.open_trades against _alpaca_lots
        # (FIFO remaining lots). Any overnight-flagged symbol absent from _alpaca_lots
        # has fully closed; we reconcile via record_exit() so it is correctly absent
        # from overnight_holds below.
        # Guards: _alpaca_pnl is not None (FIFO call succeeded); not _a4_gap (no
        # paper-API 0-fill anomaly that would make _alpaca_lots unreliable).
        # Q5: explicit catastrophe guard when both data structures are empty despite
        #     overnight positions (upstream silent FIFO failure).
        # Q4 (partial-close qty guard) deferred to P2 — board 3/4 OPTION B.
        if _alpaca_pnl is not None and not _a4_gap:
            _has_overnight = any(
                t.get("overnight") for t in self.open_trades.values()
            )
            if _has_overnight and not _alpaca_lots and not _alpaca_per_trade:
                # Q5 catastrophe guard: both data structures empty despite overnight
                # positions — likely upstream FIFO failure that didn't raise an
                # exception. Skip reconciliation to prevent spurious closure of all
                # open positions. EOD summary is written normally.
                logger.critical(
                    "write_eod_summary: Phase 2a.5 SKIPPED — overnight positions"
                    " exist in bot state but _alpaca_lots and _alpaca_per_trade"
                    " are both empty. Likely upstream data failure."
                    " Manual reconciliation required.",
                )
            else:
                for _sym_r in list(self.open_trades.keys()):
                    _tr_r = self.open_trades.get(_sym_r)
                    if not _tr_r or not _tr_r.get("overnight"):
                        continue
                    if _sym_r in _alpaca_lots:
                        continue  # Legitimately still open — FIFO lots remain
                    # ── Phantom-close guard (2026-08-12, board 3/3 + Gro + GAI) ──────
                    # FIFO-lot absence is BOOKKEEPING, not broker truth. A still-open
                    # overnight position can drop out of _alpaca_lots on a repeat
                    # same-day run (its opening fills are already in processed_fill_ids
                    # and get skipped, and the open_lots carry-forward did not retain it
                    # — fifo_pnl.py:197-198,316). Before recording ANY external_close,
                    # independently re-verify the position at Alpaca — mirrors
                    # orphan_manager.reconcile_positions Guard B. FAIL-CLOSED: on a query
                    # error OR a "still open" answer, RETAIN the position and skip the
                    # close (never book a close on unverified data). Placed before
                    # _fifo_matches so it protects BOTH the record_exit branch and the
                    # marker-only branch below. Root case: SMCI 3sh long recorded
                    # external_close 3x at exit≈entry (pnl≈$0) while the broker still
                    # held it — the fabricated flat trade also poisoned the Kelly edge.
                    # Lazy import (portfolio_tracker does not import broker at module
                    # level; avoids any circular import — mirrors the alerts import).
                    # record_exit makes no network call — this is the caller-side
                    # verification Guard D requires. Partial external close (broker holds
                    # FEWER shares than the tracker) stays the deferred Q4 item — this
                    # guard retains (fail-closed), never makes partial handling worse.
                    try:
                        from execution.broker import get_open_position as _pt_get_pos
                        _live_pos = _pt_get_pos(_sym_r)
                    except Exception as _verify_err:
                        logger.critical(
                            "write_eod_summary: %s — Phase 2a.5 live re-verify FAILED "
                            "(%s). NOT recording external_close (fail-closed); position "
                            "RETAINED. FIFO shows no lots but broker state unconfirmed "
                            "— retry next cycle.",
                            _sym_r, _verify_err,
                        )
                        try:
                            from alerts import send_slack as _pc_slack
                            _pc_slack(
                                f"🚨 {_sym_r}: EOD phantom-close guard — Alpaca "
                                f"re-verify FAILED, position retained (fail-closed). "
                                f"Check Alpaca API."
                            )
                        except Exception as _pc_slack_err:
                            logger.warning(
                                "[%s] phantom-close guard Slack alert failed: %s",
                                _sym_r, _pc_slack_err,
                            )
                        continue
                    if _live_pos is not None:
                        # Proven FIFO/lot desync: FIFO said no lots, broker says OPEN.
                        # The day's FIFO reconstruction is therefore built on a corrupted
                        # lot dict for >=1 symbol — flag the whole day's P&L unreconciled
                        # (mirror the _a4_gap posture, L775-796) so reconcile_eod
                        # finalizes it instead of writing a silently-wrong authoritative
                        # number. (Data-integrity board seat — condition of approval.)
                        logger.critical(
                            "write_eod_summary: %s — FIFO shows no remaining lots but the "
                            "position is STILL OPEN at Alpaca. FIFO/lot-state out of sync "
                            "— NOT a real close. RETAINING position, skipping "
                            "external_close, flagging day P&L unreconciled. Review "
                            "open_lots_prior_day.json.",
                            _sym_r,
                        )
                        summary["pnl_unreconciled"] = True
                        if not summary.get("pnl_unreconciled_reason"):
                            summary["pnl_unreconciled_reason"] = "fifo_lot_desync"
                        try:
                            from alerts import send_slack as _pc_slack2
                            _pc_slack2(
                                f"🚨 {_sym_r}: EOD phantom-close PREVENTED — FIFO said "
                                f"closed but Alpaca still holds it. Position retained; "
                                f"day P&L flagged unreconciled (FIFO/lot-state desync)."
                            )
                        except Exception as _pc_slack2_err:
                            logger.warning(
                                "[%s] phantom-close guard Slack alert failed: %s",
                                _sym_r, _pc_slack2_err,
                            )
                        continue
                    # _live_pos is None → double-confirmed genuinely absent; the existing
                    # branch (a)/(b) close logic below runs unchanged.
                    _fifo_matches = [
                        _pt for _pt in _alpaca_per_trade
                        if _pt.get("symbol") == _sym_r
                    ]
                    if _fifo_matches:
                        # DS P1: VWAP exit price across all per_trade lots (not [-1])
                        # DS P0: $0 guard — if all exit prices are 0.0, fall back
                        #         to marker only (no record_exit call).
                        _fifo_exits = [
                            float(pt.get("exit", 0.0))
                            for pt in _fifo_matches
                            if float(pt.get("exit", 0.0)) > 0.0
                        ]
                        _fifo_qtys = [
                            float(pt.get("qty", 0.0))
                            for pt in _fifo_matches
                            if float(pt.get("exit", 0.0)) > 0.0
                        ]
                        if not _fifo_exits:
                            # DS P0: no valid exit prices — marker-only path
                            if not _tr_r.get("_fifo_reconciled_closed"):
                                self.open_trades[_sym_r][
                                    "_fifo_reconciled_closed"
                                ] = True
                                # MTF FULL BOT AUDIT — JUNE 26 (Gro+GAI consensus):
                                # this trade never goes through record_exit(), so
                                # exit_price/pnl are never set. _load_log() later
                                # routes it directly into closed_trades. Without
                                # _fill_unverified=True, get_stats() (direct
                                # t["pnl"] indexing) raises KeyError, and kelly.py's
                                # rebuild_from_trades() treats the missing
                                # exit_price as 0.0 — a phantom catastrophic
                                # loss (long) or phantom huge win (short).
                                self.open_trades[_sym_r]["_fill_unverified"] = True
                                self._save_log()
                                logger.error(
                                    "write_eod_summary: %s has no valid FIFO exit"
                                    " prices (all 0.0) — marked"
                                    " _fifo_reconciled_closed + _fill_unverified."
                                    " Manual P&L reconciliation required.",
                                    _sym_r,
                                )
                            continue
                        _fifo_zip = zip(
                            _fifo_exits, _fifo_qtys, strict=False
                        )
                        _fifo_exit_px = (
                            sum(e * q for e, q in _fifo_zip)
                            / sum(_fifo_qtys)
                        )
                        logger.warning(
                            "write_eod_summary: %s (dir=%s, overnight_since=%s)"
                            " fully closed on Alpaca — no remaining FIFO lots."
                            " Reconciling via record_exit(reason=external_close,"
                            " exit=%.4f).",
                            _sym_r,
                            _tr_r.get("direction", "?"),
                            _tr_r.get("overnight_since", "?"),
                            _fifo_exit_px,
                        )
                        try:
                            self.record_exit(
                                _sym_r,
                                exit_price=_fifo_exit_px,
                                reason="external_close",
                                mri_level=_tr_r.get("mri_level", "NORMAL"),
                                # Verified: FIFO matched actual Alpaca close-fills
                                # that fully consume the lot (Guard D contract).
                                alpaca_confirmed_absent=True,
                            )
                            # Q1: correct exit_time to actual Alpaca fill timestamp
                            # so this reconciled trade does not appear in today_trades
                            # on any future write_eod_summary() call (today_trades
                            # filters by exit_time.startswith(today)).
                            # Q3: flag MRI level as uncertain for downstream analytics.
                            _actual_exit_ts = max(
                                (
                                    pt.get("filled_at") or ""
                                    for pt in _fifo_matches
                                    if pt.get("filled_at")
                                ),
                                default=None,
                            )
                            for _ct in reversed(self.closed_trades):
                                if (
                                    _ct.get("symbol") == _sym_r
                                    and not _ct.get("_recon_exit_ts_set")
                                ):
                                    if _actual_exit_ts:
                                        _ct["exit_time"] = _actual_exit_ts
                                    _ct["mri_at_exit_uncertain"] = True
                                    _ct["_recon_exit_ts_set"] = True
                                    break
                        except Exception as _recon_err:  # DS P1: try-except guard
                            if not self.open_trades.get(_sym_r, {}).get(
                                "_fifo_reconciled_closed"
                            ):
                                if _sym_r in self.open_trades:
                                    self.open_trades[_sym_r][
                                        "_fifo_reconciled_closed"
                                    ] = True
                                    # MTF FULL BOT AUDIT — JUNE 26: see matching
                                    # comment above — record_exit() failed here,
                                    # so exit_price/pnl were never set either.
                                    self.open_trades[_sym_r]["_fill_unverified"] = True
                                    self._save_log()
                            logger.error(
                                "write_eod_summary: record_exit() failed for %s"
                                " during FIFO reconciliation — marked"
                                " _fifo_reconciled_closed + _fill_unverified: %s",
                                _sym_r,
                                _recon_err,
                            )
                    else:
                        # No FIFO match — multi-day gap or fills absent from API.
                        if not _tr_r.get("_fifo_reconciled_closed"):
                            self.open_trades[_sym_r][
                                "_fifo_reconciled_closed"
                            ] = True
                            # MTF FULL BOT AUDIT — JUNE 26: see matching comment
                            # above — no record_exit() call on this path either.
                            self.open_trades[_sym_r]["_fill_unverified"] = True
                            self._save_log()
                            logger.warning(
                                "write_eod_summary: %s has no remaining FIFO lots"
                                " and no per_trade entry — marked"
                                " _fifo_reconciled_closed + _fill_unverified"
                                " and persisted.",
                                _sym_r,
                            )
        # ── end Phase 2a.5 ──────────────────────────────────────────────────────
        summary["overnight_holds"]  = [
            {
                "symbol":          s,
                "overnight_since": t.get("overnight_since", ""),
                "direction":       t.get("direction", ""),
            }
            for s, t in self.open_trades.items() if t.get("overnight")
        ]
        summary["exit_reasons"] = {}
        for t in today_trades:
            reason = t.get("exit_reason", "unknown")
            summary["exit_reasons"][reason] = summary["exit_reasons"].get(reason, 0) + 1

        _atomic_write(eod_path, summary)
        logger.info(f"EOD summary written → {eod_path}")

        # Rebuild Kelly stats from closed trades after FIFO (Thorp: Phase 2 deliverable)
        if kelly_sizer is not None:
            try:
                kelly_sizer.rebuild_from_trades(self.closed_trades)
                logger.info(
                    "Kelly stats rebuilt from corrected closed_trades post-EOD FIFO"
                )
            except Exception as _ke:
                logger.warning(f"Kelly rebuild after EOD FIFO failed: {_ke}")

        return summary

    # ── Trade recording ───────────────────────────────────────────────────────

    def record_entry(
        self,
        symbol: str,
        direction: str,
        qty: int,
        entry_price: float,
        stop: float,
        target: float,
        trade_mode: str,
        score: int,
        atr_value: Optional[float] = None,
        score_16pt: Optional[int] = None,
        # Guardrail 7 context — passed from main.py where full context is available
        mri_level: str = "NORMAL",
        data_source: str = "alpaca_data",
        **extra_log,
    ):
        _existing = self.open_trades.get(symbol)
        if _existing is not None and _existing.get("status") == "open":
            logger.warning(
                f"[{symbol}] record_entry() called while an OPEN position already "
                f"exists (entry=${_existing.get('entry_price', 0):.2f}, "
                f"qty_remaining={_existing.get('qty_remaining', '?')}) — "
                f"refusing to overwrite. Mirrors promote_pending_to_active()'s "
                f"duplicate-status guard."
            )
            return
        self.open_trades[symbol] = {
            "symbol":                 symbol,
            "direction":              direction,
            "qty":                    qty,
            "qty_remaining":          qty,
            "entry_price":            entry_price,
            "stop":                   stop,
            # AB-6R: immutable — never overwritten by breakeven/trail moves
            "original_stop":          stop,
            "target":                 target,
            "trail_stop":             None,
            "trade_mode":             trade_mode,
            "score":                  score,
            "score_16pt":             score_16pt,
            "atr_value":              atr_value,
            "partial_exited":         False,
            "entry_time":             datetime.now(_PT).isoformat(),
            "status":                 "open",
            "reversal_scan_count":    0,
            "reversal_confirm_count": 0,
            "overnight":              False,
            "overnight_since":        None,
            # GTC stop order submitted to Alpaca for overnight protection.
            # Cancelled at market open (so it doesn't show on L2 during RTH).
            "gtc_stop_order_id":      None,
            # Logged when Bucket A same-day block fires on a stop breach
            "stop_breached":          False,
            "stop_breach_price":      None,
        }
        self.traded_today.add(symbol)
        self._save_log()
        logger.info(
            f"[{symbol}] Entry recorded: {direction} {qty} @ ${entry_price:.2f}"
        )
        _log_event(
            "entry", symbol=symbol, price=entry_price, size=qty, score=score,
            mri_level=mri_level, data_source=data_source,
            direction=direction, stop=round(stop, 2), target=round(target, 2),
            trade_mode=trade_mode,
            # 2026-07-03: score_16pt is a NAMED param (stored on the trade dict
            # above) so it never reached **extra_log — every entry event since
            # inception logged without it, leaving the 16pt-vs-outcome validation
            # dataset empty at trade level while Layer 9 traded on the score.
            score_16pt=score_16pt,
            # GAI R1 guard: if a future caller also passes score_16pt inside
            # extra_log, drop it there — prevents duplicate-kwarg TypeError.
            **{k: v for k, v in extra_log.items() if k != "score_16pt"},
        )

    def set_gtc_stop_order_id(self, symbol: str, order_id: str):
        """Store the GTC stop order ID after submitting an overnight stop."""
        if symbol in self.open_trades:
            self.open_trades[symbol]["gtc_stop_order_id"] = order_id
            self._save_log()
            logger.debug(f"[{symbol}] GTC stop order ID stored: {order_id}")

    def clear_gtc_stop_order_id(self, symbol: str):
        """Clear GTC stop order ID after cancellation at market open."""
        if symbol in self.open_trades:
            self.open_trades[symbol]["gtc_stop_order_id"] = None
            self._save_log()

    def get_overnight_gtc_positions(self) -> list:
        """
        Return list of (symbol, trade_dict) for all positions with an active GTC stop.
        Used at market open to cancel overnight stops before RTH begins.
        """
        return [
            (sym, trade)
            for sym, trade in self.open_trades.items()
            if trade.get("gtc_stop_order_id")
        ]

    # ── Overnight pending entry state machine ─────────────────────────────────

    def record_pending_entry(
        self,
        symbol: str,
        order_id: str,
        direction: str,
        qty: int,
        limit_price: float,
        stop: float,
        target: float,
        score: int,
        atr_value: Optional[float] = None,
    ):
        """
        Record a submitted overnight DAY limit order — awaiting fill.
        order_id is persisted FIRST (David Park: crash-safe ordering).
        Status: pending_overnight → open (on fill) or removed (on cancel/expire).
        """
        self.open_trades[symbol] = {
            "symbol":                 symbol,
            "order_id":               order_id,   # stored first — crash safety
            "direction":              direction,
            "qty":                    qty,
            "qty_remaining":          qty,
            "limit_price":            limit_price,
            "entry_price":            None,        # set on fill confirmation
            "stop":                   stop,
            "target":                 target,
            "trail_stop":             None,
            "trade_mode":             "intraday",
            "score":                  score,
            "atr_value":              atr_value,
            "partial_exited":         False,
            "entry_time":             None,        # set on fill confirmation
            "status":                 "pending_overnight",
            "reversal_scan_count":    0,
            "reversal_confirm_count": 0,
            "overnight":              True,
            "overnight_since":        datetime.now(_PT).isoformat(),
            "gtc_stop_order_id":      None,
            "stop_breached":          False,
            "stop_breach_price":      None,
            "submitted_at":           datetime.now(_PT).isoformat(),
        }
        self._save_log()
        logger.info(
            f"[{symbol}] Pending overnight entry recorded: {direction} {qty} "
            f"limit@${limit_price:.2f} | Order ID: {order_id}"
        )

    def promote_pending_to_active(
        self,
        symbol: str,
        fill_price: float,
        filled_qty: int = 0,
        mri_level: str = "NORMAL",
    ):
        """Convert pending_overnight → open after Alpaca confirms fill."""
        if symbol not in self.open_trades:
            return
        t = self.open_trades[symbol]
        if t.get("status") != "pending_overnight":
            logger.warning(
                f"[{symbol}] promote_pending_to_active called on status="
                f"{t.get('status')!r} — skipping duplicate promotion"
            )
            return
        _qty = filled_qty if filled_qty > 0 else t.get("qty", 0)
        t["status"]      = "open"
        t["entry_price"] = fill_price
        t["entry_time"]  = datetime.now(_PT).isoformat()
        self.traded_today.add(symbol)
        self._save_log()
        logger.info(
            f"[{symbol}] Overnight limit FILLED @ ${fill_price:.2f}"
            " — position now active"
        )
        if _qty <= 0:
            logger.warning(
                f"[{symbol}] Entry event skipped: filled_qty={filled_qty} and "
                f"trade qty={t.get('qty', 0)} both zero — check trade state"
            )
            return
        try:
            _log_event(
                "entry",
                symbol      = symbol,
                score       = t.get("score", 0),
                mri_level   = mri_level,
                price       = fill_price,
                size        = _qty,
                data_source = "overnight_limit_fill",
                trade_mode  = t.get("trade_mode", "intraday"),
                direction   = t.get("direction"),
                stop        = t.get("stop"),
                target      = t.get("target"),
            )
        except Exception as _log_err:
            logger.warning(
                f"[{symbol}] Overnight entry event logging failed: {_log_err}"
            )

    def cancel_pending_entry(self, symbol: str):
        """Remove a pending_overnight entry from tracker
        (order cancelled or expired)."""
        if (
            symbol in self.open_trades
            and self.open_trades[symbol].get("status") == "pending_overnight"
        ):
            del self.open_trades[symbol]
            self._save_log()
            logger.info(f"[{symbol}] Pending overnight entry removed from tracker")

    def get_pending_overnight_entries(self) -> list:
        """Return list of (symbol, trade_dict) for all pending_overnight entries."""
        return [
            (sym, trade)
            for sym, trade in self.open_trades.items()
            if trade.get("status") == "pending_overnight"
        ]

    def record_partial_exit(
        self,
        symbol: str,
        exit_price: float,
        qty_closed: int,
        trail_stop: float,
    ) -> float:
        """
        Record a partial exit (first target hit).
        Updates qty_remaining, activates trailing stop on the remainder.
        Returns realized P&L on the closed portion.
        """
        if symbol not in self.open_trades:
            return 0.0
        trade     = self.open_trades[symbol]
        # Idempotency guard: if same price+qty was already partially exited,
        # skip duplicate
        _existing_partial_price = trade.get("partial_exit_price")
        _existing_partial_qty   = trade.get("partial_exit_qty_last")
        if (
            _existing_partial_price == exit_price
            and _existing_partial_qty == qty_closed
        ):
            logger.warning(
                f"[{symbol}] record_partial_exit called with same price/qty"
                f" as last partial — skipping duplicate (idempotency guard)"
            )
            return 0.0
        trade["partial_exit_qty_last"] = qty_closed  # track for idempotency
        direction = trade["direction"]
        # BUG-5 mirror (record_exit() pattern): validate entry_price before using
        # it in P&L math. A None/zero entry would otherwise produce a phantom
        # P&L of roughly ±(exit_price * qty_closed) — the same corruption class
        # found in kelly.py's rebuild_from_trades() missing exit_price guard.
        _raw_entry = trade.get("entry_price")
        if _raw_entry is None or float(_raw_entry or 0.0) <= 0:
            logger.critical(
                "[%s] record_partial_exit: entry_price=%r is None/zero — "
                "partial P&L forced to $0.00. Check pending_overnight promotion "
                "and fill logs.",
                symbol, _raw_entry,
            )
            try:
                from alerts import send_slack as _pep_slack
                _pep_slack(
                    f"🚨 CRITICAL: {symbol} partial exit with missing/zero "
                    f"entry_price. P&L forced to $0.00. Check OCI logs immediately."
                )
            except Exception as _pep_slack_err:
                logger.warning(
                    "[%s] partial-exit entry_price Slack alert failed: %s",
                    symbol, _pep_slack_err,
                )
            # Board vote (Thorp/quant-logic domain, MTF FULL BOT AUDIT — JUNE 26):
            # mirror the write_eod_summary() _fill_unverified pattern so this
            # trade is excluded from get_stats() win-rate/Sharpe math and from
            # kelly.py's rebuild_from_trades() — without this flag, the forced
            # $0.00 partial leg would silently understate realized P&L without
            # ever being excluded from edge/win-rate statistics the way a
            # FIFO-reconciliation failure already is.
            trade["_fill_unverified"] = True
            entry = 0.0
            pnl   = 0.0
        else:
            entry = float(_raw_entry)
            pnl = (exit_price - entry) * qty_closed if direction == "long" \
                  else (entry - exit_price) * qty_closed

        trade["qty_remaining"]    -= qty_closed
        # Guard: qty_remaining must be [0, qty] —
        # reconciliation can produce impossible states
        trade["qty_remaining"]     = max(0, min(trade["qty_remaining"], trade["qty"]))
        trade["partial_exited"]    = True
        trade["trail_stop"]        = trail_stop
        # breakeven floor after partial
        trade["stop"]              = trade.get("entry_price")
        trade["partial_exit_price"] = exit_price
        trade["partial_exit_time"]  = datetime.now(_PT).isoformat()
        # accumulate across all tranches
        # 4dp storage: prevents false-zero accumulation on tiny P&L
        trade["partial_pnl"]        = round(trade.get("partial_pnl", 0.0) + pnl, 4)

        self._save_log()
        logger.info(
            f"[{symbol}] Partial exit: {qty_closed} shares @ ${exit_price:.2f} "
            f"| P&L ${pnl:.2f} | {trade['qty_remaining']} remaining | "
            f"Trail stop: ${trail_stop:.2f}"
        )
        return pnl

    def update_trail_stop(self, symbol: str, new_trail_stop: float):
        """Ratchet the trailing stop in the favorable direction.

        Self-persists (calls self._save_log()) like every other state-mutating
        method on this class. Two call sites in exit_logic.py's trail-ratchet
        branches (check_partial_exits, _check_exits_extended_hours) had no
        unconditional save downstream of this call — several reachable
        early-exit paths (stop-cancel failure, GTC resubmit failure, position
        not found) left a ratcheted trail_stop unpersisted. Gro+GAI consensus
        fix, MTF FULL BOT AUDIT — JUNE 26.
        """
        if symbol not in self.open_trades:
            return
        trade     = self.open_trades[symbol]
        old       = trade.get("trail_stop")
        direction = trade["direction"]
        if direction == "long" and (old is None or new_trail_stop > old):
            trade["trail_stop"] = new_trail_stop
            self._save_log()
        elif direction == "short" and (old is None or new_trail_stop < old):
            trade["trail_stop"] = new_trail_stop
            self._save_log()

    def record_exit(
        self, symbol: str, exit_price: float, reason: str = "signal",
        # BUG-E2E-4: was always "NORMAL" (hardcoded default in _log_event)
        mri_level: str = "NORMAL",
        # Guard D (2026-07-04, board+Gro+GAI): defense-in-depth against the
        # false-drop root cause. An external_close exit must be Alpaca-VERIFIED
        # by the caller (reconcile double-confirm / Patch 1 get_open_position
        # None / EOD FIFO fill-match). record_exit stays a pure state mutation
        # — it makes NO network call — it only enforces the contract so no
        # future caller can silently drop a live position on an unverified
        # external_close. Non-external reasons (signal/stop/target/EOD) ignore
        # this flag.
        alpaca_confirmed_absent: bool = False,
    ):
        # BV-1: normalize None/""/UNKNOWN to baseline
        # (MRI is background-only; absence = NORMAL)
        if mri_level in (None, "", "UNKNOWN"):
            mri_level = "NORMAL"
        if symbol not in self.open_trades:
            return
        # Guard D: refuse an UNVERIFIED external_close drop.
        if str(reason).startswith("external_close") and not alpaca_confirmed_absent:
            logger.critical(
                "[%s] record_exit BLOCKED: external_close reason=%r without "
                "alpaca_confirmed_absent — caller must verify the position is "
                "gone at Alpaca first. Position RETAINED (fail-closed).",
                symbol, reason,
            )
            try:
                from alerts import send_slack as _gd_slack
                _gd_slack(
                    f"🚨 {symbol}: external_close record_exit BLOCKED "
                    f"(unverified) — position retained. Caller must re-verify "
                    f"against Alpaca."
                )
            except Exception as _gd_err:
                logger.warning("[%s] Guard-D Slack alert failed: %s", symbol, _gd_err)
            return
        # DS guard: reject already-closed trade in open_trades (corrupt restart state)
        if self.open_trades.get(symbol, {}).get("status") == "closed":
            logger.warning(
                f"[{symbol}] record_exit called on already-closed trade — skipping"
            )
            return
        # Validate original qty BEFORE pop — prevents orphaned trade on early return
        _original_qty = self.open_trades[symbol].get("qty", 0)
        if _original_qty <= 0:
            logger.error(
                f"[{symbol}] record_exit: invalid original qty"
                f" ({_original_qty}) — skipping"
            )
            return
        # BUG-5: validate entry_price BEFORE pop — if entry is None/0 the trade is
        # still closed (P&L=$0.00) but we need this check here to log CRITICAL + Slack
        # before pop so the warning is never lost if something goes wrong downstream.
        _raw_entry_chk = self.open_trades[symbol].get("entry_price")
        if _raw_entry_chk is None or float(_raw_entry_chk or 0.0) <= 0:
            logger.critical(
                "[%s] record_exit: entry_price=%r is None/zero — "
                "trade will be closed with P&L=$0.00. "
                "Check pending_overnight promotion and fill logs.",
                symbol, _raw_entry_chk,
            )
            try:
                from alerts import send_slack as _ep_slack
                _ep_slack(
                    f"🚨 CRITICAL: {symbol} closed with missing/zero entry_price. "
                    f"P&L forced to $0.00. Check OCI logs immediately."
                )
            except Exception as _ep_slack_err:
                logger.warning(
                    "[%s] entry_price Slack alert failed: %s", symbol, _ep_slack_err
                )
        trade     = self.open_trades.pop(symbol)
        # Clamp qty_remaining to [0, original_qty].
        # Trades opened before qty_remaining was added fall back to original qty
        # via default.
        # Never force 0 back to original_qty — 0 is valid after full partial exit (GAI).
        qty           = trade.get("qty_remaining", _original_qty)
        qty           = max(0, min(qty, _original_qty))
        _partial_pnl  = trade.get("partial_pnl", 0.0)
        entry         = float(trade.get("entry_price") or 0.0)
        direction     = trade["direction"]

        # qty=0 with no prior partial exits = reconciliation/restart corruption.
        # partial_exited flag (set by record_partial_exit L1170) is the reliable
        # discriminator — handles breakeven tranche where partial_pnl=0.
        if qty == 0 and not trade.get("partial_exited", False):
            _reason_str = str(reason) if reason else ""
            if _reason_str.startswith("external_close"):
                logger.warning(
                    "[%s] record_exit: external_close with zero qty and no partials — "
                    "likely already reconciled. Skipping fallback. "
                    "P&L set to $0.00. Review fill_reconciler/reconcile_eod logs.",
                    symbol,
                )
            elif entry > 0:
                logger.warning(
                    "[%s] record_exit: qty_remaining=0 with no partial P&L — "
                    "reconciliation artifact. Falling back to original qty %d. "
                    "Verify Alpaca fills; review fill_reconciler/reconcile_eod logs.",
                    symbol, _original_qty,
                )
                qty = _original_qty
            else:
                logger.error(
                    "[%s] record_exit: corrupt trade state — qty_remaining=0, "
                    "no partial exits, entry_price=%.2f (invalid). "
                    "Cannot fallback. P&L will be $0.00. Review logs.",
                    symbol, entry,
                )

        # BUG-5: if entry=0.0 (corrupt/never-promoted trade), force pnl=0.0.
        # Without this guard, pnl = (exit_price - 0.0) * qty = gross proceeds as profit.
        # S47 Bug2: set _fill_unverified=True so patch_exit_pnl() routes this to the
        # entry=0 guard at L564–574 (preserves partial_pnl only) and excludes this
        # trade from get_stats() denominators until reconciled (DS+GAI S47).
        if entry <= 0:
            pnl = 0.0
            trade["_fill_unverified"] = True
        else:
            pnl = (exit_price - entry) * qty if direction == "long" \
                  else (entry - exit_price) * qty

        # Include all locked-in partial exit P&L in the final trade record.
        # partial_pnl accumulates across T1/T2/T3 tranches in record_partial_exit().
        # Without this, partial profits are silently erased from win/loss stats,
        # daily P&L, and kill switch calculations.
        _total_pnl   = round(pnl + _partial_pnl, 4)  # 4dp storage

        # pnl_pct uses original full position value for accurate return measurement.
        # Use pre-validated _original_qty — avoids corrupt pnl_pct if
        # trade["qty"] missing.
        _orig_qty    = _original_qty
        trade.update({
            "exit_price":    exit_price,
            "exit_time":     datetime.now(_PT).isoformat(),
            "exit_reason":   reason,
            "pnl":           _total_pnl,
            # final-close portion only (for audit)
            "pnl_remaining": round(pnl, 4),  # 4dp storage
            "pnl_pct":       (
                round((_total_pnl / (entry * _orig_qty)) * 100, 2)
                if entry > 0 and _orig_qty > 0 else 0.0
            ),
            "status":        "closed",
            # BUG-1 fix: zero on close (was stale pre-exit value)
            "qty_remaining": 0,
            # RC-4: pre-close remaining qty for fill reconciliation
            "_qty_at_close": qty,
        })
        self.closed_trades.append(trade)
        # P2-MANUAL-AUDIT: write external closes to manual_audit.jsonl
        # for operator review
        if reason and str(reason).startswith("external_close"):
            _json_audit = json
            from zoneinfo import ZoneInfo as _ZI_audit
            _pt_now = datetime.now(_ZI_audit("America/Los_Angeles")).isoformat()
            _audit_record = {
                "ts_pt":       _pt_now,
                "event":       "external_close",
                "symbol":      symbol,
                "reason":      reason,
                "direction":   trade.get("direction"),
                "entry_price": trade.get("entry_price"),
                "exit_price":  exit_price,
                "qty":         trade.get("qty"),
                "pnl":         trade.get("pnl"),
                "score":       trade.get("score"),
            }
            try:
                _audit_path = (
                    Path(__file__).resolve().parent.parent
                    / "logs" / "manual_audit.jsonl"
                )
                with open(_audit_path, "a") as _af:
                    _af.write(_json_audit.dumps(_audit_record) + "\n")
                    _af.flush()
                    os.fsync(_af.fileno())  # durability: rare path (<1/week)
            except Exception as _audit_err:
                logger.warning(
                    f"[{symbol}] manual_audit.jsonl write failed: {_audit_err}"
                )
                try:
                    from alerts import send_slack as _audit_slack
                    _audit_slack(
                        f"[RC-5] manual_audit.jsonl fsync FAILED for {symbol} "
                        f"({_audit_err}) — external_close record may not be on disk."
                    )
                except Exception as _as_err:
                    logger.warning(
                        "[%s] audit fsync Slack alert failed: %s",
                        symbol, _as_err,
                    )
        # RC-4: populate index BEFORE _save_log() — ordering guard (DS F9)
        if trade.get("_fill_unverified"):
            self._unverified_exits.setdefault(symbol, []).append(trade)
        self._save_log()
        logger.info(
            f"[{symbol}] Exit recorded: ${exit_price:.2f}"
            f" | P&L: ${_total_pnl:.2f} ({reason})"
        )
        # Guardrail 7: structured exit event — stop_hit or exit depending on reason
        _evt = (
            "stop_hit"
            if any(s in str(reason or "") for s in _LOG_STOP_REASONS)
            else "exit"
        )
        _log_event(
            _evt, symbol=symbol, price=exit_price,
            size=qty, score=trade.get("score", 0),
            pnl=_total_pnl, reason=reason, direction=direction,
            mri_level=mri_level,   # BUG-E2E-4: pass through caller-supplied MRI level
        )
        # Return _total_pnl (remaining close + all partial tranches) — not pnl alone.
        # Every caller feeds this into risk.register_close() → daily_pnl → kill switch.
        # Returning pnl (remaining only) made kill switch blind to partial exit profits.
        return _total_pnl

    def record_gtc_triggered(self, symbol: str, exit_price: float):
        """
        Record that an overnight GTC stop was triggered by Alpaca.
        Called at startup when reconciling GTC orders.
        Closes the tracker record with reason='gtc_stop_triggered'.
        """
        if symbol not in self.open_trades:
            logger.warning(
                f"[{symbol}] record_gtc_triggered called but symbol not in tracker"
            )
            return
        pnl = self.record_exit(symbol, exit_price, reason="gtc_stop_triggered")
        logger.info(
            f"[{symbol}] GTC stop triggered overnight — "
            f"tracker closed at ${exit_price:.2f} | P&L: ${pnl:.2f}"
        )
        return pnl

    def record_stop_breach_blocked(self, symbol: str, current_price: float):
        """
        Called when Bucket A same-day block fires AND the stop is breached.
        Logs the breach in the trade dict for EOD review.
        """
        if symbol not in self.open_trades:
            return
        trade = self.open_trades[symbol]
        active_stop = trade.get("trail_stop") or trade.get("stop")
        trade["stop_breached"]    = True
        trade["stop_breach_price"] = current_price
        self._save_log()
        logger.warning(
            f"[{symbol}] Bucket A STOP BREACH BLOCKED — "
            f"price ${current_price:.2f} vs stop ${active_stop:.2f} | "
            f"Position held per same-day hard rule. Review at EOD."
        )

    # ── Utility queries ───────────────────────────────────────────────────────

    def is_in_trade(self, symbol: str) -> bool:
        today = datetime.now(_PT).strftime("%Y-%m-%d")
        if today != self._traded_today_date:
            self.traded_today = set()
            self._traded_today_date = today
        return symbol in self.open_trades or symbol in self.traded_today

    def get_trade(self, symbol: str) -> dict:
        return self.open_trades.get(symbol)

    def opened_today(self, symbol: str) -> bool:
        """Return True if this symbol was opened in the current trading session."""
        trade = self.open_trades.get(symbol)
        if not trade:
            return False
        return (
            trade.get("entry_time", "")[:10] == datetime.now(_PT).strftime("%Y-%m-%d")
        )

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        if not self.closed_trades:
            return {"total_trades": 0}
        import math
        import statistics as _stats

        # S47 Bug2: exclude _fill_unverified trades from stats denominators.
        # Unverified fills have pnl=$0.00 (forced by entry=None guard in record_exit
        # or by fetch_actual_fill_price fallback) — including them inflates total_trades
        # and skews win_rate/Sharpe without contributing real signal (DS+GAI S47).
        # MTF FULL BOT AUDIT — JUNE 26: defensive .get() fallback, belt-and-
        # suspenders alongside the write_eod_summary() root-cause fix (which
        # now sets _fill_unverified=True on every _fifo_reconciled_closed path).
        # Covers any trade that reaches closed_trades without "pnl" ever set —
        # historical records pre-dating that fix, or any future path that's
        # missed. Direct t["pnl"] indexing previously raised KeyError here,
        # which is called from inside _save_log() — meaning every subsequent
        # mutating call would also fail until restart.
        pnls   = [
            t.get("pnl", 0.0) for t in self.closed_trades
            if not t.get("_fill_unverified")
        ]
        wins   = [p for p in pnls if p > 0]
        # Exclude breakeven (pnl=0.0) from losses and the win_rate denominator.
        # DS+GAI consensus S20: scratches are not risk outcomes; including them
        # in losses deflates avg_loss and distorts the Kelly win rate input.
        losses = [p for p in pnls if p < 0]

        sharpe = 0
        if len(pnls) >= 2:
            mean_pnl = _stats.mean(pnls)
            std_pnl  = _stats.stdev(pnls)
            sharpe   = (
                round((mean_pnl / std_pnl) * math.sqrt(252), 3) if std_pnl > 0 else 0
            )

        sortino = 0
        downside = [p for p in pnls if p < 0]
        if len(downside) >= 2 and len(pnls) >= 2:
            mean_pnl     = _stats.mean(pnls)
            downside_std = _stats.stdev(downside)
            sortino      = round(
                (mean_pnl / downside_std) * math.sqrt(252), 3
            ) if downside_std > 0 else 0

        # S47 Bug1: count unverified before loop so it's available for return dict and
        # warning log even if r_multiples ends up empty.
        _unverified_count = sum(
            1 for t in self.closed_trades if t.get("_fill_unverified")
        )
        r_multiples = []
        for t in self.closed_trades:
            if t.get("_fill_unverified"):       # S47 Bug1: exclude — pnl unreliable
                continue
            entry         = t.get("entry_price") or 0
            # fallback for legacy trades
            original_stop = t.get("original_stop") or t.get("stop") or 0
            pnl           = t.get("pnl") or 0
            qty           = t.get("qty") or 1
            risk_per_share = abs(entry - original_stop)
            if risk_per_share <= 0 or qty <= 0:
                continue
            _r = pnl / (risk_per_share * qty)
            # ±50R clamp: prevents scratch-stop / corrupt-stop R explosion from
            # distorting avg_r_multiple (DS+GAI S47 — ±50R covers all realistic
            # paper-account outcomes; any |R|>50 is corrupt data, not a real trade).
            r_multiples.append(max(-50.0, min(50.0, _r)))

        if r_multiples:
            avg_r = round(sum(r_multiples) / len(r_multiples), 3)
        else:
            avg_r = 0
            if self.closed_trades:
                logger.warning(
                    "get_stats: no verified R-multiples (%d trades total, "
                    "%d unverified). avg_r_multiple=0 — Kelly will use "
                    "minimum sizing until verified trades accumulate.",
                    len(self.closed_trades), _unverified_count,
                )

        return {
            # S47 Bug2: total_trades = ALL closed (for audit); verified_trades and
            # unverified_trades let callers distinguish clean vs dirty subsets.
            "total_trades":     len(self.closed_trades),
            "verified_trades":  len(pnls),
            "unverified_trades": _unverified_count,
            "win_rate":       (
                round(len(wins) / (len(wins) + len(losses)) * 100, 1)
                if (wins or losses) else 0.0
            ),
            "total_pnl":      round(sum(pnls), 2),
            "avg_win":        round(sum(wins) / len(wins), 2) if wins else 0,
            "avg_loss":       round(sum(losses) / len(losses), 2) if losses else 0,
            "profit_factor":  round(
                sum(wins) / abs(sum(losses)), 2
            ) if losses else (0.0 if not wins else float("inf")),
            "sharpe_ratio":   sharpe,
            "sortino_ratio":  sortino,
            "avg_r_multiple": avg_r,
            "open_trades":    len(self.open_trades),
        }

    def attach_news_summary(self, news_summary: dict):
        self._news_summary = news_summary

    def get_news_summary(self) -> dict:
        return getattr(self, "_news_summary", {})

    def print_stats(self):
        stats = self.get_stats()
        print("\n" + "=" * 50)
        print("PORTFOLIO STATS")
        print("=" * 50)
        for k, v in stats.items():
            print(f"  {k:<20} {v}")
        print("=" * 50 + "\n")


