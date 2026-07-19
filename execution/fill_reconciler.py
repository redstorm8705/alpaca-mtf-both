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

import config
from execution.fill_helpers import fetch_actual_fill_price_or_none

logger = logging.getLogger(__name__)

# (removed _MIN_PRICE_DIFF 2026-07-16: the fragile `abs(fill-entry) < _MIN_PRICE_DIFF`
# failure-guess is gone — fetch_actual_fill_price_or_none returns None on a true miss,
# so a real fill (incl. a genuine scratch at ~entry) is no longer mistaken for a failure.)

# RC-4 reconciliation retry window (2026-07-16 root-cause fix — board + Gro + GAI).
# WAS a hardcoded 5 minutes, which made the reconciler STRUCTURALLY UNABLE TO FIRE: the
# observed run_cycle cadence is ~5.5-6 min (cycles at 13:30:24 / 13:36:22 / 13:41:53), so
# a 5-min window gave AT MOST ONE attempt and usually ZERO. Real case 2026-07-16: RIVN's
# open-auction cover filled in 4 pieces over 13:32:51-13:34:15 — recoverable — but the
# first reconciler touch came at 14:06:30 (36 min later) and found the trade already
# "expired" → zero recovery attempts ever made → a real +$0.51 stayed recorded as $0.00
# (the same mechanism recorded RIVN's real -$41 as $0.00 on 7/7, plus 6 other trades).
# This is a RETRY BUDGET ONLY — it does NOT widen the Alpaca query, which is independently
# bounded by the trade's own entry_time in fill_helpers._derive_close_lower_bound (plus a
# protective-side filter and a ±50% sanity band). So a wider window CANNOT match a fill a
# narrower one wouldn't; it only buys attempts (board: "zero-delta" wrong-fill risk).
# Expiry semantics shift accordingly: an RC-4 CRITICAL now means "90 min of failed
# recovery" (a genuinely stuck fill) rather than "5 min" (a normal slow fill) — making the
# alert meaningful instead of routine noise. The operator is never left silent either way:
# the FIRST CRITICAL + Slack already fires at exit time from _fill_unverified_fallback.
_RC4_WINDOW_MIN = getattr(config, "RC4_RECONCILE_WINDOW_MINUTES", 90)

# FLOOR CLAMP (board catch 2026-07-16): portfolio_tracker.mark_fill_expired hardcodes a
# 5-minute floor guard (`if _exit_dt >= cutoff: continue`) to avoid marking FRESH trades.
# That is safe while this window is larger, but if anyone ever set this below 5, expired
# trades would be silently skipped there — never marked, re-queued by _load_log() on every
# restart → the exact infinite RC-4 CRITICAL loop the T1 fix exists to prevent. Config
# drives one constant; its partner is hardcoded. Clamp so the two can never drift into it.
if _RC4_WINDOW_MIN < 5:
    logger.warning(
        "[fill_reconciler] RC4_RECONCILE_WINDOW_MINUTES=%s is below the 5-min floor "
        "hardcoded in mark_fill_expired — clamping to 5 to avoid silently skipping "
        "expired trades (infinite re-queue loop).", _RC4_WINDOW_MIN,
    )
    _RC4_WINDOW_MIN = 5


def run_fill_reconciliation(tracker, kelly=None, risk=None) -> None:  # noqa: ARG001
    """RC-4: Non-blocking reconciliation pass — called from run_cycle after check_exits().

    Called from BOTH run_cycle paths that can close a position: the main pass, and the
    9:30-10:00 ET "opening noise window" block (which returns early — before this fix the
    reconciler was unreachable there, which is exactly where open stop-breach covers fire).

    Args:
        tracker: PortfolioTracker instance (owns _unverified_exits + patch_exit_pnl)
        kelly:   KellySizer instance (optional) — passed to patch_exit_pnl for rebuild
        risk:    RiskManager instance (optional) — reserved; adjust_daily_pnl dropped.
                 daily_pnl needs no adjustment here because risk.update_daily_pnl_from_alpaca()
                 sources it from ALPACA, not from the tracker — so a fabricated tracker pnl
                 never corrupted it. (The old note claimed that call "overwrites daily_pnl
                 each cycle"; that is FALSE in the opening-window path, where run_cycle
                 returns above it — but the conclusion still holds, because the value is
                 Alpaca-sourced rather than refreshed-per-cycle. Board catch 2026-07-16.)
                 Blast radius of an unpatched fill is therefore Kelly / win-rate / EOD —
                 NOT the kill switch, which reads Alpaca equity.
    """
    try:
        pending, expired = tracker.get_unverified_exits(max_age_minutes=_RC4_WINDOW_MIN)
    except Exception as _e:
        logger.warning(f"[fill_reconciler] get_unverified_exits failed: {_e}")
        return

    # ── Expired entries — reconciliation window closed ─────────────────────────
    for sym in expired:
        logger.critical(
            f"[{sym}] RC-4 FILL RECONCILIATION EXPIRED: "
            f"fill_unverified trade outside {_RC4_WINDOW_MIN}-min window — P&L remains "
            f"unreliable. Manual verification required."
        )
        try:
            from alerts import send_slack
            send_slack(
                f":rotating_light: RC-4 FILL RECONCILIATION EXPIRED [{sym}] "
                f"fill_unverified trade outside the {_RC4_WINDOW_MIN}-min reconciliation "
                f"window. P&L for this trade is unreliable — MANUAL VERIFICATION REQUIRED."
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
        _original_exit_time = trade_copy.get("exit_time")

        # Bug A fix (RIVN P&L corruption, 2026-07-16): call fetch_actual_fill_price with
        # submitted_after=None so it uses the EXTERNAL-CLOSE path (fill_helpers.py:240-279)
        # — entry-time-bounded CLOSED-orders query, filled_at DESC, protective-side filter,
        # ±50% sanity band. The prior code derived submitted_after from entry_time, which
        # forced the legacy P5-H2 ASC/limit=5 path (fill_helpers.py:280-304): it returns the
        # 5 OLDEST orders after entry and MISSES a close that settled hours later → fell back
        # to entry_price → pnl=0.0 (the RIVN −$41 real loss recorded as 0.0 that tripped the
        # kill switch). The external path derives the entry-time bound INTERNALLY from the
        # same trade dict, so the 2026-07-03 phantom-fill protection is preserved.
        #
        # Guards before querying (Gro + GAI design audit 2026-07-16):
        #  - direction must be long/short — the external side filter (sell=long / buy=short)
        #    would otherwise silently drop the real close and leave the trade unverified.
        #  - entry_time must be present + parseable — it bounds the query; skip this cycle
        #    (no _fill_unverified fallback spam) if not; RC-4 expiry surfaces a trade that
        #    never resolves.
        if trade_copy.get("direction") not in ("long", "short"):
            logger.warning(
                "[%s] fill_reconciler: missing/invalid direction (%r) — skipping "
                "reconciliation this cycle.", sym, trade_copy.get("direction"),
            )
            continue
        _et_str = trade_copy.get("entry_time") or ""
        if not _et_str:
            logger.warning(
                "[%s] fill_reconciler: no entry_time to bound the close query — "
                "skipping this cycle.", sym,
            )
            continue
        # RC-4 datetime fix (2026-07-19): tolerant parse (defense-in-depth — entry_time is
        # bot-generated PT-aware today, but Alpaca Z/fraction forms now also validate). MINIMAL
        # skip on a genuinely-unparseable entry_time — the trade just retries next cycle; RC-4
        # expiry surfaces it if it never resolves (board: fill_reconciler stays MINIMAL, since
        # recovery is bounded by entry_time and a corrupt entry_time can't bound the query).
        from execution.state_io import _iso_to_dt
        if _iso_to_dt(_et_str) is None:
            logger.warning(
                "[fill_reconciler] entry_time parse failed for %s (%r) — skipping this cycle "
                "to avoid an unbounded/stale fill match.",
                sym, _et_str[:80],
            )
            continue

        try:
            # None-on-failure sibling (2026-07-16, board + Gro + GAI): distinguishes
            # "no fill found yet" (None → leave pending, retry next cycle) from a real
            # fill — INCLUDING a genuine scratch that filled at ~entry. The old code
            # used `abs(fill - entry) < _MIN_PRICE_DIFF` to GUESS failure, which skipped
            # a real scratch trade FOREVER (never patched → rode to RC-4 EXPIRED as a
            # false CRITICAL, re-queued every restart). fetch_actual_fill_price_or_none
            # does NOT set _fill_unverified / fire CRITICAL+Slack — this is a retry, not
            # a final give-up (that is what RC-4 expiry is for).
            _fill = fetch_actual_fill_price_or_none(
                symbol=sym,
                trade=trade_copy,
                poll_secs=0.1,
                submitted_after=None,   # external-close path: entry-bounded, filled_at DESC
                no_retry=True,
            )
        except Exception as _fe:
            logger.warning(f"[{sym}] fill_reconciler: fetch_actual_fill_price_or_none error: {_fe}")
            continue

        if _fill is None:
            logger.debug(
                f"[{sym}] fill_reconciler: no verified close fill yet — Alpaca still "
                f"unsettled; leaving pending for the next cycle (RC-4 expiry will "
                f"surface it if it never resolves)."
            )
            continue

        # Real fill found (a genuine scratch at ~entry is a REAL fill and IS patched —
        # pnl 0.00 but VERIFIED, which clears _fill_unverified). Patch the closed record.
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
