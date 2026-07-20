# ruff: noqa: E501
"""
execution/stop_protection.py — single-invariant protective-stop reconciler.

THE INVARIANT (continuously enforced, DERIVED FROM ALPACA every cycle):
  Every open intraday position has a LIVE exchange stop on the reducing side, covering
  the position qty — or this reconciler places it THIS cycle; if the intended level is
  already breached, it covers at market; if it cannot do either (or the state is
  ambiguous), it PAGES. Protection state is read from Alpaca's LIVE open orders +
  position each cycle, NEVER from stored order IDs — so a restart, a premarket->open
  transition, or a rejected/expired stop is just another cycle. There is no per-day gate
  to burn (kills the gate-before-submit naked-all-session bug) and no stored order id to
  trust (kills the "_TERMINAL set omits 'rejected'" class — get_open_orders returns ONLY
  live orders, so we never classify a terminal status).

STEP 1 = ADDITIVE SAFETY NET (this file). It ACTS only on a position Alpaca shows has NO
live protecting stop (or a breached-naked one). It never cancels or replaces a
correctly-protecting stop → cannot regress a protected position. It FAILS SAFE on any
unknown state (API error → skip the ORDER, but PAGE the knowledge gap — fail-safe on the
action, fail-LOUD on the uncertainty). Retiring the ~13 legacy submit sites + 6 status
sets is Phase B, gated separately.

REVIEW-HARDENED 2026-07-19 (cold board REJECT → revision; both reverse-fill paths closed):
  * Cover books the ACTUAL fill (fetch_actual_fill_price_or_none), never current_price (RC-4).
  * Double-place (stop-vs-stop): a repeat submit is suppressed by a recent-successful-placement
    guard (client_order_id is non-idempotent, so Alpaca can't dedupe within the get_orders
    visibility lag / back-to-back sweeps). A visible false page beats a silent second live
    stop. NOTE (Phase-B, bulletproof): give protective stops a DETERMINISTIC client_order_id
    in broker.submit_*_stop_order so Alpaca enforces the dedup (40910000).
  * Reverse-fill (stop-vs-limit): a resting reducing NON-stop order, or an over-covered stop,
    PAGES instead of placing/keeping a full-size stop that could over-sell into a reverse.
  * Unknown-state now PAGES (throttled), quarterly-hold symbols are excluded, session is
    validated, qty is whole-share-guarded.

HARDENED 2026-07-20 (the destructive-broker-fallback fix — this is the load-bearing one):
  Every submit passes allow_cancel_blocking=False. WITHOUT it, a 40310000 (held_for_orders)
  rejection — the EXACT thing that happens when our get_open_orders read is stale and the
  position is in fact protected — sent broker.py into cancel_open_orders_for_symbol(),
  CANCELLING the good stop and resubmitting at a different level, or leaving the position
  naked if its 63s re-poll exhausted. So the "never regresses a protected position" claim
  above was true inside this module and FALSE through its broker dependency. It is now true
  end-to-end. The submit result is a 4-WAY contract (PROTECTION_ALREADY_HELD /
  PROTECTION_UNKNOWN / None / order) and MUST be matched by identity — a bare `is not None`
  treats a sentinel as success and writes an empty order id over a live stop's real one.

Data tier: T1 (Alpaca Trading REST via execution.broker). No SDK instantiation here.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from execution.broker import (
    PROTECTION_ALREADY_HELD,
    PROTECTION_UNKNOWN,
    close_position,
    get_open_orders,
    get_open_position,
    submit_day_stop_order,
    submit_gtc_stop_order,
)
from execution.fill_helpers import fetch_actual_fill_price_or_none

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_QTY_EPS = 1e-6

# Sessions that hold a position past today's close want a GTC stop (survives to tomorrow);
# an intraday RTH session wants a DAY stop (auto-expires 4pm ET so it never lingers on L2 or
# collides with tonight's GTC submission).
_GTC_SESSIONS = frozenset(("ah", "premarket", "closed"))
_RTH_SESSIONS = frozenset(("rth",))
_VALID_SESSIONS = _GTC_SESSIONS | _RTH_SESSIONS

# Double-place guard: a stop client_order_id is NON-idempotent (broker._make_idem_id stamps
# epoch+uuid), so Alpaca will not dedupe a repeat submit inside the get_orders(OPEN)
# visibility lag or a back-to-back sweep. We record every SUCCESSFUL placement and refuse to
# re-place the same (symbol, side, stop-cents) within the TTL — pageing instead. Keyed by
# monotonic clock (never wall-time), pruned each sweep. Cross-process (restart) is not
# covered, but by the time a new process runs, the stop is visible (>> TTL) → already_protected.
_recent_placements: dict = {}
_PLACEMENT_TTL_SEC = 90.0

# Per-symbol consecutive unknown-state skip counter — pages ONCE per outage episode per
# symbol (streak 0->1), then suppresses re-pages until the symbol evaluates cleanly again.
_skip_streak: dict = {}

# Dedicated throttle for the broker-unverifiable-hold path (PROTECTION_UNKNOWN).
# Deliberately NOT _skip_streak: that dict is cleared on every clean evaluation, which happens
# BEFORE the submit, so it cannot throttle anything downstream of that point (the pre-existing
# loop-error handler has the same problem — tracked as PRE-WIRE-BLOCKER-1, which generalises
# this into a per-(symbol,reason) throttle covering the four sticky MANUAL-required paths too).
# Pages ONCE per episode per symbol; cleared the moment the symbol reaches any DEFINITE outcome
# (protected / broker-held / placed / covered), and pruned when it leaves the book.
_unknown_page_streak: dict = {}


def _qhm_symbols() -> set:
    """Quarterly-hold symbols this reconciler must NOT manage (intraday-only). Never raises."""
    try:
        from execution.quarterly_hold_manager import get_quarterly_hold_symbols
        return set(get_quarterly_hold_symbols() or [])
    except Exception as e:  # noqa: BLE001
        logger.debug("stop-protect: QHM symbol lookup failed (treating none as QHM): %s", e)
        return set()


def _order_side(o) -> str:
    _s = getattr(o, "side", "")
    return str(getattr(_s, "value", _s)).lower()


def _is_stop_type(o) -> bool:
    _t = getattr(o, "type", "")
    return "stop" in str(getattr(_t, "value", _t)).lower()


def _order_qty(o) -> float:
    try:
        return abs(float(getattr(o, "qty", 0) or 0))
    except (TypeError, ValueError):
        return 0.0


def _reducing_side_for(direction: str) -> str:
    """The order side that REDUCES a position of the given direction."""
    return "sell" if direction == "long" else "buy"


def _reducing_qty(orders, direction: str, *, stop_only: bool) -> float:
    """Total qty of LIVE (open) orders that REDUCE a `direction` position. `orders` comes
    from get_open_orders(sym) which returns ONLY open orders, so every element is live by
    construction; we never inspect a terminal status. stop_only=True → protective stops;
    stop_only=False → reducing NON-stop orders (e.g. profit-taking limits)."""
    want = _reducing_side_for(direction)
    total = 0.0
    for o in orders:
        if _order_side(o) != want:
            continue
        is_stop = _is_stop_type(o)
        if (stop_only and is_stop) or (not stop_only and not is_stop):
            total += _order_qty(o)
    return total


def _intended_stop(trade: dict) -> float | None:
    """The stop PRICE this position should currently rest at: the live trail level if one is
    active, else the (possibly breakeven-moved) hard stop. Single resolver."""
    for key in ("trail_stop", "stop"):
        v = trade.get(key)
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _is_breached(direction: str, stop_px: float, mkt_px: float) -> bool:
    """A long's sell-stop at/above market, or a short's buy-stop at/below market, is already
    breached — Alpaca would reject it (42210000); the position must be COVERED, not stopped."""
    if direction == "long":
        return stop_px >= mkt_px
    return stop_px <= mkt_px


def _page(symbol: str, msg: str) -> None:
    """Loud operator page — never raises (a failed alert must not break the loop)."""
    logger.critical("[%s] STOP-PROTECT: %s", symbol, msg)
    try:
        from alerts import send_slack
        send_slack(f":rotating_light: [{symbol}] STOP-PROTECT: {msg}")
    except Exception as _e:  # noqa: BLE001 — alerting must never break protection
        logger.debug("[%s] stop-protect Slack page failed: %s", symbol, _e)


def _place_key(symbol: str, side: str, stop_px: float) -> tuple:
    return (symbol, side, int(round(stop_px * 100)))


def _recently_placed(symbol: str, side: str, stop_px: float, now_mono: float) -> bool:
    ts = _recent_placements.get(_place_key(symbol, side, stop_px))
    return ts is not None and (now_mono - ts) < _PLACEMENT_TTL_SEC


def reconcile_protection(
    tracker,
    risk=None,
    *,
    session: str,
    place: bool = True,
) -> dict:
    """Ensure every open intraday position has a live protective stop — the single enforcer.

    ADDITIVE NET (Step 1): only acts on a position Alpaca shows is UNPROTECTED. Idempotent,
    fail-safe on any unknown state (skips the order, PAGES the uncertainty). Returns a
    per-symbol action summary (also the assertion surface for the failure-injection harness).

    Args:
      tracker : PortfolioTracker (reads open_trades; record_exit / set_gtc_stop_order_id / _save_log)
      risk    : RiskManager | None (register_close on a cover)
      session : "rth" | "ah" | "premarket" | "closed" — selects DAY vs GTC stop tif
      place   : when False, DETECT + report only (shadow mode) — submits nothing
    """
    summary: dict = {
        "session": session,
        "already_protected": [],
        "placed": [],
        "covered": [],
        "paged": [],
        "skipped": [],       # unknown state — failed safe on the order (also paged, throttled)
        "excluded_qhm": [],
        # Broker said the qty is already held by a stable live protective order, i.e. our
        # get_open_orders read was STALE and the position was protected all along. Counted
        # separately from already_protected (which is derived from our own read) because the
        # rate of this bucket IS the measurement of get_open_orders visibility lag — the exact
        # evidence the shadow-mode run needs before placement is enabled anywhere.
        "broker_held": [],
    }
    if session not in _VALID_SESSIONS:
        # Unknown session → default to GTC (a stop that lasts too long is far safer than one
        # that expires and leaves a position naked overnight). Loud, not silent.
        logger.warning("STOP-PROTECT: unknown session %r — defaulting to GTC (fail-safe).", session)
        use_gtc = True
    else:
        use_gtc = session in _GTC_SESSIONS

    qhm = _qhm_symbols()
    now_mono = time.monotonic()
    open_symbols = set()

    for symbol, trade in list(getattr(tracker, "open_trades", {}).items()):
        open_symbols.add(symbol)
        try:
            if str(trade.get("status", "")).lower() not in ("open", ""):
                continue
            if symbol in qhm:
                summary["excluded_qhm"].append(symbol)  # quarterly holds are not ours to manage
                continue
            direction = str(trade.get("direction", "")).lower()
            if direction not in ("long", "short"):
                _page(symbol, f"open position has an unknown direction {trade.get('direction')!r} — "
                              f"cannot determine a reducing side; MANUAL review, position may be unprotected.")
                summary["paged"].append((symbol, "unknown direction"))
                continue

            # ── Alpaca-derived truth ──
            orders = get_open_orders(symbol)
            if orders is None:
                _skip_unknown(symbol, summary, "get_open_orders unavailable (API failure)")
                continue
            try:
                pos = get_open_position(symbol)
            except Exception as _pe:  # non-404 API error → unknown → fail safe + page
                _skip_unknown(symbol, summary, f"get_open_position raised: {_pe!r}")
                continue
            if pos is None:
                # Alpaca has no position — nothing to protect (closed / stale tracker entry).
                # Not a naked-position risk; observability only, and clear any skip streak.
                _skip_streak.pop(symbol, None)
                summary["skipped"].append((symbol, "no live Alpaca position"))
                logger.warning("[%s] STOP-PROTECT: tracker-open but Alpaca shows no position "
                               "(closed/stale) — deferring to orphan reconcile.", symbol)
                continue
            try:
                net_qty = abs(float(getattr(pos, "qty", 0) or 0))
                mkt_px = float(getattr(pos, "current_price", 0) or 0)
            except (TypeError, ValueError):
                _skip_unknown(symbol, summary, "unparseable position qty/price")
                continue
            if net_qty <= _QTY_EPS:
                _skip_streak.pop(symbol, None)
                continue  # already flat

            # A clean evaluation from here → clear any prior unknown-state streak.
            _skip_streak.pop(symbol, None)

            stop_cov = _reducing_qty(orders, direction, stop_only=True)

            # OVER-COVERED: a stop for MORE than the position (e.g. a resting limit filled and
            # shrank the position but a full-size stop remains) → on trigger it over-sells into
            # a reverse. v1 has no resize path → PAGE, do not treat as protected-and-silent.
            if stop_cov > net_qty + _QTY_EPS:
                _page(symbol, f"OVER-COVERED: stop qty {stop_cov:g} > position {net_qty:g} ({direction}) — "
                              f"reverse-fill risk if triggered. MANUAL resize/cancel required.")
                summary["paged"].append((symbol, "over-covered"))
                continue

            if stop_cov >= net_qty - _QTY_EPS:
                _unknown_page_streak.pop(symbol, None)   # definite outcome — end any episode
                summary["already_protected"].append((symbol, net_qty, stop_cov))
                continue  # fully protected → never touch it (additive net)

            # UNDER-COVERED partial stop → page (no second stop; avoids a double-stop race).
            if stop_cov > _QTY_EPS:
                _page(symbol, f"UNDER-COVERED: stop covers {stop_cov:g} of {net_qty:g} {direction} shares — "
                              f"{net_qty - stop_cov:g} naked. MANUAL top-up required.")
                summary["paged"].append((symbol, "under-covered"))
                continue

            # No protective stop at all. If a resting REDUCING non-stop order exists (e.g. a
            # profit limit), a full-size stop could later over-sell into a reverse once that
            # order fills → PAGE for manual handling instead of auto-placing (v1 conservatism).
            other_cov = _reducing_qty(orders, direction, stop_only=False)
            if other_cov > _QTY_EPS:
                _page(symbol, f"UNPROTECTED but a resting reducing order covers {other_cov:g} shares — "
                              f"auto-placing a full-size stop risks a reverse if it fills. MANUAL stop required.")
                summary["paged"].append((symbol, "resting reducing order conflict"))
                continue

            intended = _intended_stop(trade)
            if intended is None:
                _page(symbol, f"UNPROTECTED {direction} x{net_qty:g} with NO usable stop level "
                              f"(trail_stop/stop missing) — cannot place a stop; MANUAL stop required NOW.")
                summary["paged"].append((symbol, "no stop level"))
                continue

            side = _reducing_side_for(direction)

            # Whole-share guard (positions are integer shares; guard against float drift / a
            # sub-share position that would truncate to a 0-share order).
            qty_int = int(round(net_qty))
            if qty_int < 1:
                _page(symbol, f"UNPROTECTED sub-share {direction} position ({net_qty:g}) — cannot place an "
                              f"integer stop; MANUAL review.")
                summary["paged"].append((symbol, "sub-share qty"))
                continue

            if not place:  # shadow mode — report intended action, submit nothing
                summary["placed"].append((symbol, "SHADOW", side, qty_int, intended))
                logger.info("[%s] STOP-PROTECT (shadow): WOULD place %s stop x%d @ $%.2f",
                            symbol, side, qty_int, intended)
                continue

            if mkt_px > 0 and _is_breached(direction, intended, mkt_px):
                _cover(symbol, trade, mkt_px, intended, direction, tracker, risk, summary)
                continue

            # Double-place guard: refuse a repeat submit of the same stop inside the visibility
            # window (a placed-but-not-yet-visible stop would otherwise be duplicated → reverse).
            if _recently_placed(symbol, side, intended, now_mono):
                _page(symbol, f"recently placed a {side} stop @ ${intended:.2f} not yet visible in open "
                              f"orders — NOT re-placing (double-stop guard). Verify; self-heals next cycle.")
                summary["paged"].append((symbol, "place-guard suppressed"))
                continue

            submit = submit_gtc_stop_order if use_gtc else submit_day_stop_order
            # allow_cancel_blocking=False is LOAD-BEARING (broker.py, 2026-07-20): without it a
            # 40310000 (held_for_orders) rejection sends the broker into
            # cancel_open_orders_for_symbol() — CANCELLING the very stop that caused the
            # rejection, i.e. destroying live protection this reconciler exists to preserve,
            # and possibly leaving the position naked if the 63s re-poll then exhausts.
            order = submit(symbol=symbol, qty=qty_int, side=side,
                           stop_price=round(intended, 2), allow_cancel_blocking=False)

            # ── 4-WAY result contract (see broker.py sentinels) ──────────────────────────
            # These MUST be identity checks. The sentinels are NOT None, so the old
            # `if order is not None:` would take the SUCCESS branch on a sentinel, read
            # getattr(order, "id", "") -> "" and persist an EMPTY order id over a live stop's
            # real one — corrupting exactly the protection state this module protects.
            if order is PROTECTION_ALREADY_HELD:
                # Alpaca refused because a STABLE live reducing order already holds the qty.
                # So the position IS protected and our get_open_orders read above was stale
                # (visibility lag). Not a placement and NOT a failure: do not page, do not
                # record an order id, do not arm the double-place guard (we placed nothing).
                logger.info(
                    "[%s] STOP-PROTECT: broker reports the qty is already held by a live "
                    "protective order — our order-book read was stale. Treating as PROTECTED; "
                    "nothing placed, nothing cancelled.", symbol,
                )
                _unknown_page_streak.pop(symbol, None)   # definite outcome — end any episode
                summary["broker_held"].append((symbol, net_qty, side, intended))
                continue

            if order is PROTECTION_UNKNOWN:
                # Held, but the broker could not read the book to confirm a stable hold.
                # Protection is genuinely UNKNOWN — NEVER claim it (never-mask-a-loss). Fail
                # loud, but throttled to ONCE per outage episode per symbol via the dedicated
                # _unknown_page_streak (NOT _skip_streak, which is already cleared by the
                # clean-evaluation reset above and so cannot throttle anything down here).
                _reason = "broker could not verify the protective hold (order book unreadable)"
                summary["skipped"].append((symbol, _reason))
                _unknown_page_streak[symbol] = _unknown_page_streak.get(symbol, 0) + 1
                if _unknown_page_streak[symbol] == 1:
                    _page(symbol, f"UNPROTECTED-OR-NOT-UNKNOWN {direction} x{qty_int}: the broker "
                                  f"could not verify whether a protective stop holds this qty "
                                  f"({_reason}). NOT claiming protection. VERIFY the stop manually. "
                                  f"Suppressing repeats until this symbol resolves.")
                    summary["paged"].append((symbol, "broker-unverifiable hold"))
                else:
                    logger.warning("[%s] STOP-PROTECT: hold still unverifiable (%s), occurrence "
                                   "#%d — page suppressed until the symbol resolves.",
                                   symbol, _reason, _unknown_page_streak[symbol])
                continue

            if order is not None:
                _recent_placements[_place_key(symbol, side, intended)] = now_mono
                oid = str(getattr(order, "id", "") or "")
                try:
                    if use_gtc and hasattr(tracker, "set_gtc_stop_order_id"):
                        tracker.set_gtc_stop_order_id(symbol, oid)  # persists internally (_save_log)
                    else:
                        trade["rth_day_stop_order_id"] = oid
                        tracker._save_log()
                except Exception as _se:  # id write-back failed; the stop is ALREADY live at Alpaca
                    logger.warning("[%s] STOP-PROTECT: stop placed (order %s) but tracker id write-back "
                                   "failed: %s", symbol, oid, _se)
                logger.warning("[%s] STOP-PROTECT: placed MISSING %s stop x%d @ $%.2f (%s) — order %s",
                               symbol, side, qty_int, intended, "GTC" if use_gtc else "DAY", oid)
                _unknown_page_streak.pop(symbol, None)   # definite outcome — end any episode
                summary["placed"].append((symbol, oid, side, qty_int, intended))
            else:
                _page(symbol, f"UNPROTECTED {direction} x{qty_int} and stop placement FAILED "
                              f"(broker returned None @ ${intended:.2f}) — MANUAL stop required NOW.")
                summary["paged"].append((symbol, "place failed"))

        except Exception as _loop_err:  # one bad symbol must never abort the sweep
            _skip_unknown(symbol, summary, f"loop error: {_loop_err!r}")

    # Prune guard/streak state for symbols no longer open (bounded memory).
    for _k in [k for k in _recent_placements if k[0] not in open_symbols]:
        _recent_placements.pop(_k, None)
    for _s in [s for s in _skip_streak if s not in open_symbols]:
        _skip_streak.pop(_s, None)
    for _u in [u for u in _unknown_page_streak if u not in open_symbols]:
        _unknown_page_streak.pop(_u, None)

    # UNCONDITIONAL summary line (reliability seat, 2026-07-20). Previously gated on
    # `if _n_act:`, so a fully-healthy sweep left NO trace — making "wired and everything is
    # protected" indistinguishable from "silently not wired at all". For a module that spent
    # weeks deployed-but-inert, that is the one state we cannot afford to be unable to observe.
    logger.info("STOP-PROTECT [%s]: protected %d | broker-held %d | placed %d | covered %d | "
                "paged %d | skipped %d | qhm-excl %d | @ %s ET", session,
                len(summary["already_protected"]), len(summary["broker_held"]),
                len(summary["placed"]), len(summary["covered"]), len(summary["paged"]),
                len(summary["skipped"]), len(summary["excluded_qhm"]),
                datetime.now(ET).strftime("%H:%M:%S"))
    return summary


def _skip_unknown(symbol: str, summary: dict, reason: str) -> None:
    """Fail-safe on the ORDER (skip) but fail-LOUD on the knowledge gap: page ONCE per outage
    episode per symbol (streak 0->1), then suppress until the symbol evaluates cleanly."""
    summary["skipped"].append((symbol, reason))
    _skip_streak[symbol] = _skip_streak.get(symbol, 0) + 1
    if _skip_streak[symbol] == 1:
        _page(symbol, f"cannot determine protection state ({reason}) for a tracker-open position — "
                      f"fail-safe (no order placed); VERIFY the stop manually. Suppressing repeats until clear.")
        summary["paged"].append((symbol, f"unknown-state page: {reason}"))
    else:
        logger.warning("[%s] STOP-PROTECT: still unknown (%s), skip #%d — page suppressed.",
                        symbol, reason, _skip_streak[symbol])


def _cover(symbol, trade, mkt_px, intended, direction, tracker, risk, summary) -> None:
    """Cover a breached-naked position at market and book the ACTUAL fill (RC-4)."""
    cov_ts = time.time()
    if not close_position(symbol):
        _page(symbol, f"breached-naked {direction} and cover FAILED (close_position rejected) — "
                      f"position NAKED, manual cover required NOW.")
        summary["paged"].append((symbol, "cover failed"))
        return
    fill = fetch_actual_fill_price_or_none(symbol, trade, poll_secs=1.0, submitted_after=cov_ts)
    exit_px = fill if fill is not None else mkt_px
    try:
        pnl = tracker.record_exit(symbol, exit_px, reason="stop_protect_cover")
        if risk is not None:
            risk.register_close(pnl or 0.0)
    except Exception as _re:  # broker already flat; only bookkeeping failed — loud, not silent
        pnl = None
        _page(symbol, f"covered breached-naked position but record_exit raised: {_re!r}")
    if fill is None:
        _page(symbol, f"breached-naked {direction} COVERED, but the actual fill was UNVERIFIED — "
                      f"booked at market estimate ${mkt_px:.2f} (pnl {pnl}); verify vs Alpaca.")
    else:
        _page(symbol, f"breached-naked {direction} (stop ${intended:.2f} vs mkt ${mkt_px:.2f}) — "
                      f"COVERED at fill ${exit_px:.2f} (pnl {pnl}).")
    summary["covered"].append((symbol, intended, exit_px))
