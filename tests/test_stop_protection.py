#!/usr/bin/env python3
# ruff: noqa: E501
"""
Failure-injection harness for execution/stop_protection.reconcile_protection.

FORCES each failure mode and asserts the invariant holds — the piece missing for months.
Runs with plain unittest (no pytest):  python3 -m tests.test_stop_protection

Proves closed, by construction:
  * Finding A (gate-before-submit → naked all session): no per-day gate; re-derives every call.
  * Finding B (_TERMINAL omits "rejected"): trusts Alpaca's LIVE open orders, never a stored id.
  * Cold-board REJECT fixes: cover books the actual fill; stop-vs-limit + over-covered page;
    unknown-state pages; double-place guard suppresses a not-yet-visible repeat.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

# Stub the Alpaca SDK so this harness runs anywhere — execution.broker imports these at load,
# but every broker function the reconciler calls is mock-patched below.
for _mod in ("alpaca", "alpaca.trading", "alpaca.trading.client",
             "alpaca.trading.requests", "alpaca.trading.enums"):
    sys.modules.setdefault(_mod, mock.MagicMock())

from execution import stop_protection as sp  # noqa: E402  (after the SDK stub)


def _order(side: str, otype: str = "stop", qty: float = 1, oid: str = "o1", status: str = "new"):
    return types.SimpleNamespace(id=oid, symbol="X", side=side, type=otype, qty=qty, status=status, stop_price=100.0)


_DEF_ORDER = _order("sell")  # module-level singleton default (ruff B008)


def _position(direction: str, qty: float, price: float):
    return types.SimpleNamespace(qty=qty, side=direction, current_price=price)


class _Tracker:
    def __init__(self, open_trades):
        self.open_trades = open_trades
        self.saved = 0
        self.exits = []
        self.gtc_set = []

    def record_exit(self, sym, px, reason=""):
        self.exits.append((sym, px, reason))
        self.open_trades.pop(sym, None)
        return 1.23

    def set_gtc_stop_order_id(self, sym, oid):
        self.gtc_set.append((sym, oid))

    def _save_log(self):
        self.saved += 1


class _Risk:
    def __init__(self):
        self.closes = []

    def register_close(self, pnl):
        self.closes.append(pnl)


def _trade(direction="long", stop=95.0, trail=None, qty=3):
    t = {"symbol": "X", "direction": direction, "status": "open", "qty": qty, "stop": stop}
    if trail is not None:
        t["trail_stop"] = trail
    return t


class StopProtectionInvariant(unittest.TestCase):
    def setUp(self):
        # Clear module-level guard/streak state between tests (deterministic).
        sp._recent_placements.clear()
        sp._skip_streak.clear()
        sp._unknown_page_streak.clear()
        sp._page_throttle.clear()

    def _run(self, *, open_orders, position, trade, session="rth", place=True,
             submit_day=_DEF_ORDER, submit_gtc=_DEF_ORDER, close_ok=True,
             position_raises=False, cover_fill=89.5, qhm=(), tracker=None):
        tracker = tracker if tracker is not None else _Tracker({"X": dict(trade)})
        risk = _Risk()
        goo = mock.Mock(return_value=open_orders)
        gop = mock.Mock(side_effect=RuntimeError("boom") if position_raises else None, return_value=position)
        sday = mock.Mock(return_value=submit_day)
        sgtc = mock.Mock(return_value=submit_gtc)
        cpos = mock.Mock(return_value=close_ok)
        fill = mock.Mock(return_value=cover_fill)
        qhmm = mock.Mock(return_value=set(qhm))
        page = mock.Mock()
        with mock.patch.object(sp, "get_open_orders", goo), \
             mock.patch.object(sp, "get_open_position", gop), \
             mock.patch.object(sp, "submit_day_stop_order", sday), \
             mock.patch.object(sp, "submit_gtc_stop_order", sgtc), \
             mock.patch.object(sp, "close_position", cpos), \
             mock.patch.object(sp, "fetch_actual_fill_price_or_none", fill), \
             mock.patch.object(sp, "_qhm_symbols", qhmm), \
             mock.patch.object(sp, "_page", page):
            summary = sp.reconcile_protection(tracker, risk, session=session, place=place)
        return summary, dict(day=sday, gtc=sgtc, close=cpos, fill=fill, page=page,
                             tracker=tracker, risk=risk)

    # 1 — naked + valid level, RTH → a DAY stop is placed
    def test_naked_places_day_stop(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade())
        self.assertEqual(len(s["placed"]), 1)
        m["day"].assert_called_once()
        self.assertEqual(m["day"].call_args.kwargs["side"], "sell")
        self.assertEqual(m["day"].call_args.kwargs["qty"], 3)
        m["gtc"].assert_not_called()
        m["close"].assert_not_called()

    # 2 — naked + breached → COVER at the FETCHED fill (not market)
    def test_naked_breached_covers_at_fill(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 90.0), trade=_trade(stop=95.0), cover_fill=88.75)
        self.assertEqual(len(s["covered"]), 1)
        self.assertEqual(len(s["placed"]), 0)
        m["close"].assert_called_once_with("X")
        m["fill"].assert_called_once()
        self.assertEqual(m["tracker"].exits[0][1], 88.75, "cover must book the fetched fill, not market")
        self.assertEqual(m["tracker"].exits[0][2], "stop_protect_cover")
        self.assertEqual(m["risk"].closes, [1.23])

    # 2b — cover but fill UNVERIFIED (None) → fall back to market + page
    def test_cover_fill_none_falls_back_and_pages(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 90.0), trade=_trade(stop=95.0), cover_fill=None)
        self.assertEqual(len(s["covered"]), 1)
        self.assertEqual(m["tracker"].exits[0][1], 90.0, "fallback to market price when fill unverified")
        self.assertTrue(m["page"].called)

    # 3 — already protected → NOTHING touched
    def test_protected_is_idempotent(self):
        s, m = self._run(open_orders=[_order("sell", qty=3)], position=_position("long", 3, 110.0), trade=_trade())
        self.assertEqual(len(s["already_protected"]), 1)
        m["day"].assert_not_called()
        m["gtc"].assert_not_called()
        m["close"].assert_not_called()

    # 4 — get_open_orders None → FAIL SAFE (no place) + PAGE the knowledge gap
    def test_orders_unavailable_fails_safe_and_pages(self):
        s, m = self._run(open_orders=None, position=_position("long", 3, 110.0), trade=_trade())
        self.assertEqual(len(s["skipped"]), 1)
        m["day"].assert_not_called()
        self.assertTrue(m["page"].called, "unknown state must page, not stay silent")

    # 5 — get_open_position raises → FAIL SAFE + PAGE
    def test_position_read_raises_fails_safe_and_pages(self):
        s, m = self._run(open_orders=[], position=None, trade=_trade(), position_raises=True)
        self.assertEqual(len(s["skipped"]), 1)
        m["day"].assert_not_called()
        self.assertTrue(m["page"].called)

    # 6 — FINDING B: stored (rejected) id but empty open orders → places a fresh stop
    def test_rejected_stop_not_trusted(self):
        tr = _trade()
        tr["gtc_stop_order_id"] = "dead-rejected-id"
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=tr)
        self.assertEqual(len(s["placed"]), 1)
        m["day"].assert_called_once()

    # 7 — place fails → PAGE, no crash
    def test_place_failure_pages(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(), submit_day=None)
        self.assertEqual(len(s["paged"]), 1)
        self.assertEqual(len(s["placed"]), 0)

    # 8 — under-covered stop → PAGE, no double-stop
    def test_under_covered_pages_no_double_stop(self):
        s, m = self._run(open_orders=[_order("sell", qty=1)], position=_position("long", 3, 110.0), trade=_trade())
        self.assertEqual(s["paged"][0][1], "under-covered")
        m["day"].assert_not_called()

    # 8b — OVER-covered stop (stop qty > position) → PAGE (reverse-fill risk), no silence
    def test_over_covered_pages(self):
        s, m = self._run(open_orders=[_order("sell", qty=10)], position=_position("long", 3, 110.0), trade=_trade())
        self.assertEqual(s["paged"][0][1], "over-covered")
        self.assertEqual(len(s["already_protected"]), 0)
        m["day"].assert_not_called()

    # 8c — resting reducing LIMIT (non-stop) + no stop → PAGE, do NOT auto-place a full-size stop
    def test_resting_limit_conflict_pages(self):
        s, m = self._run(open_orders=[_order("sell", otype="limit", qty=3)], position=_position("long", 3, 110.0), trade=_trade())
        self.assertEqual(s["paged"][0][1], "resting reducing order conflict")
        m["day"].assert_not_called()

    # 9 — no usable stop level → PAGE, no place
    def test_no_stop_level_pages(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0))
        self.assertEqual(s["paged"][0][1], "no stop level")
        m["day"].assert_not_called()

    # 10 — AH session uses GTC, not DAY
    def test_ah_uses_gtc(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                         session="ah", submit_gtc=_order("sell", oid="g1"))
        m["gtc"].assert_called_once()
        m["day"].assert_not_called()
        self.assertEqual(m["tracker"].gtc_set, [("X", "g1")])

    # 10b — unknown session → defaults to GTC (fail-safe, never a DAY stop that expires naked)
    def test_unknown_session_defaults_gtc(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                         session="weird", submit_gtc=_order("sell", oid="g9"))
        m["gtc"].assert_called_once()
        m["day"].assert_not_called()

    # 11 — short: reducing side BUY; trail preferred over stop
    def test_short_uses_buy_and_trail(self):
        s, m = self._run(open_orders=[], position=_position("short", 2, 90.0),
                         trade=_trade(direction="short", stop=105.0, trail=100.0))
        m["day"].assert_called_once()
        self.assertEqual(m["day"].call_args.kwargs["side"], "buy")
        self.assertEqual(m["day"].call_args.kwargs["stop_price"], 100.0)

    # 12 — shadow mode: nothing submitted
    def test_shadow_mode_submits_nothing(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(), place=False)
        self.assertEqual(len(s["placed"]), 1)
        m["day"].assert_not_called()

    # 13 — cover fails → PAGE naked, no false success
    def test_cover_failure_pages(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 90.0), trade=_trade(stop=95.0), close_ok=False)
        self.assertEqual(s["paged"][0][1], "cover failed")
        self.assertEqual(len(s["covered"]), 0)

    # 14 — QHM/quarterly-hold symbol is EXCLUDED (reconciler manages intraday only)
    def test_qhm_excluded(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(), qhm=("X",))
        self.assertEqual(s["excluded_qhm"], ["X"])
        m["day"].assert_not_called()

    # 15 — pos None (Alpaca flat) → skip quietly, no place, no crash
    def test_pos_none_skips(self):
        s, m = self._run(open_orders=[], position=None, trade=_trade())
        self.assertIn(("X", "no live Alpaca position"), s["skipped"])
        m["day"].assert_not_called()

    # 16 — DOUBLE-PLACE GUARD: a placed-but-not-yet-visible stop is not re-placed next sweep
    def test_double_place_guard(self):
        # First sweep: naked → places (records in the guard).
        s1, m1 = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade())
        self.assertEqual(len(s1["placed"]), 1)
        # Second sweep, same symbol still shows no live stop (visibility lag): must NOT re-place.
        s2, m2 = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade())
        m2["day"].assert_not_called()
        self.assertEqual(s2["paged"][0][1], "place-guard suppressed")

    # ── 2026-07-20: the destructive-broker-fallback fix (broker allow_cancel_blocking) ──

    # 17 — EVERY submit must opt out of the broker's cancel-blocking-orders recovery.
    # Without allow_cancel_blocking=False a 40310000 makes broker.py cancel the GOOD stop.
    def test_submit_opts_out_of_cancel_blocking(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade())
        self.assertIs(m["day"].call_args.kwargs["allow_cancel_blocking"], False,
                      "DAY submit MUST pass allow_cancel_blocking=False")
        # The first run armed the double-place guard for (X, sell, 95.00); clear it so the
        # GTC leg below actually reaches submit instead of being suppressed.
        sp._recent_placements.clear()
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                         session="ah", submit_gtc=_order("sell", oid="g2"))
        self.assertIs(m["gtc"].call_args.kwargs["allow_cancel_blocking"], False,
                      "GTC submit MUST pass allow_cancel_blocking=False")

    # 18 — PROTECTION_ALREADY_HELD => position is protected (stale read). No page, no order id
    # written, guard NOT armed. This is the branch a bare `is not None` would corrupt.
    def test_broker_held_treated_as_protected_not_success(self):
        tracker = _Tracker({"X": _trade()})
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                         submit_day=sp.PROTECTION_ALREADY_HELD, tracker=tracker)
        self.assertEqual(len(s["broker_held"]), 1)
        self.assertEqual(len(s["placed"]), 0, "a sentinel is NOT a placement")
        self.assertEqual(len(s["paged"]), 0, "already-protected must not page")
        self.assertFalse(m["page"].called)
        # THE REGRESSION: must never persist an empty order id over a live stop's real one.
        self.assertEqual(tracker.gtc_set, [])
        self.assertNotIn("rth_day_stop_order_id", tracker.open_trades["X"])
        self.assertEqual(sp._recent_placements, {}, "guard must not arm when nothing was placed")

    # 18b — same, GTC/AH path: must not call set_gtc_stop_order_id with an empty id.
    def test_broker_held_ah_does_not_write_empty_gtc_id(self):
        tracker = _Tracker({"X": _trade()})
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                         session="ah", submit_gtc=sp.PROTECTION_ALREADY_HELD, tracker=tracker)
        self.assertEqual(len(s["broker_held"]), 1)
        self.assertEqual(tracker.gtc_set, [], "must NOT overwrite a live GTC id with ''")

    # 19 — PROTECTION_UNKNOWN => protection unverifiable: fail loud, never claim protected.
    def test_broker_unknown_fails_loud(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                         submit_day=sp.PROTECTION_UNKNOWN)
        self.assertEqual(len(s["skipped"]), 1)
        self.assertEqual(len(s["broker_held"]), 0, "UNKNOWN must never count as protected")
        self.assertEqual(len(s["placed"]), 0)
        self.assertTrue(m["page"].called, "unverifiable protection must page, not stay silent")

    # 20 — a genuine None is still a failure and still pages (contract not weakened).
    def test_none_still_pages_as_failure(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                         submit_day=None)
        self.assertEqual(s["paged"][0][1], "place failed")
        self.assertEqual(len(s["broker_held"]), 0)

    # 21 — the sentinels must be distinguishable from each other, from None, and from an order.
    def test_sentinel_identity_contract(self):
        self.assertIsNot(sp.PROTECTION_ALREADY_HELD, sp.PROTECTION_UNKNOWN)
        self.assertIsNot(sp.PROTECTION_ALREADY_HELD, None)
        self.assertIsNot(sp.PROTECTION_UNKNOWN, None)
        self.assertFalse(hasattr(sp.PROTECTION_ALREADY_HELD, "id"),
                         "sentinel must have no .id — misuse should fail loudly")
        self.assertFalse(hasattr(sp.PROTECTION_UNKNOWN, "id"))

    # 21b — PROTECTION_UNKNOWN pages ONCE per episode, then suppresses. Uses the dedicated
    # _unknown_page_streak, NOT _skip_streak (which the clean-evaluation reset clears before
    # the submit and so cannot throttle anything downstream of it).
    def test_broker_unknown_pages_once_per_episode(self):
        s1, m1 = self._run(open_orders=[], position=_position("long", 3, 110.0),
                           trade=_trade(), submit_day=sp.PROTECTION_UNKNOWN)
        s2, m2 = self._run(open_orders=[], position=_position("long", 3, 110.0),
                           trade=_trade(), submit_day=sp.PROTECTION_UNKNOWN)
        s3, m3 = self._run(open_orders=[], position=_position("long", 3, 110.0),
                           trade=_trade(), submit_day=sp.PROTECTION_UNKNOWN)
        self.assertTrue(m1["page"].called, "first UNKNOWN must page")
        self.assertFalse(m2["page"].called, "repeat UNKNOWN must be suppressed")
        self.assertFalse(m3["page"].called, "repeat UNKNOWN must stay suppressed")
        # still recorded every sweep — suppression is of the PAGE, never of the record
        for s in (s1, s2, s3):
            self.assertEqual(len(s["skipped"]), 1)
        self.assertEqual(len(s1["paged"]), 1)
        self.assertEqual(len(s2["paged"]), 0)

    # 21b-2 — a DEFINITE outcome ends the episode, so a later UNKNOWN pages again (no
    # permanent suppression of a genuinely new incident).
    def test_unknown_episode_resets_after_definite_outcome(self):
        _, m1 = self._run(open_orders=[], position=_position("long", 3, 110.0),
                          trade=_trade(), submit_day=sp.PROTECTION_UNKNOWN)
        self.assertTrue(m1["page"].called)
        # definite outcome: broker confirms a live protective hold
        self._run(open_orders=[], position=_position("long", 3, 110.0),
                  trade=_trade(), submit_day=sp.PROTECTION_ALREADY_HELD)
        self.assertNotIn("X", sp._unknown_page_streak, "definite outcome must end the episode")
        # a NEW unknown incident must page again
        _, m3 = self._run(open_orders=[], position=_position("long", 3, 110.0),
                          trade=_trade(), submit_day=sp.PROTECTION_UNKNOWN)
        self.assertTrue(m3["page"].called, "a new episode must page again")

    # 21b-3 — streak state is pruned when the symbol leaves the book (bounded memory).
    def test_unknown_streak_pruned_when_symbol_closes(self):
        self._run(open_orders=[], position=_position("long", 3, 110.0),
                  trade=_trade(), submit_day=sp.PROTECTION_UNKNOWN)
        self.assertIn("X", sp._unknown_page_streak)
        self._run(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(),
                  tracker=_Tracker({}))          # symbol no longer open
        self.assertNotIn("X", sp._unknown_page_streak, "must prune state for closed symbols")

    # 21c — broker_held tuple shape is part of the contract consumers will read.
    def test_broker_held_tuple_shape(self):
        s, m = self._run(open_orders=[], position=_position("long", 3, 110.0),
                         trade=_trade(stop=95.0), submit_day=sp.PROTECTION_ALREADY_HELD)
        self.assertEqual(len(s["broker_held"]), 1)
        sym, qty, side, level = s["broker_held"][0]
        self.assertEqual((sym, qty, side, level), ("X", 3.0, "sell", 95.0))

    # 21d — a SHORT position hitting a sentinel must behave identically (buy-side reducing).
    def test_short_position_broker_held(self):
        tracker = _Tracker({"X": _trade(direction="short", stop=105.0)})
        s, m = self._run(open_orders=[], position=_position("short", 2, 90.0),
                         trade=_trade(direction="short", stop=105.0),
                         submit_day=sp.PROTECTION_ALREADY_HELD, tracker=tracker)
        self.assertEqual(len(s["broker_held"]), 1)
        self.assertEqual(s["broker_held"][0][2], "buy")
        self.assertEqual(len(s["placed"]), 0)
        self.assertEqual(tracker.gtc_set, [])

    # 22 — the summary line is emitted even on a fully-healthy sweep (observability):
    # "everything protected" must be distinguishable from "not wired at all".
    def test_summary_emitted_when_all_protected(self):
        with self.assertLogs("execution.stop_protection", level="INFO") as cm:
            self._run(open_orders=[_order("sell", qty=3)],
                      position=_position("long", 3, 110.0), trade=_trade())
        self.assertTrue(any("STOP-PROTECT [" in line for line in cm.output),
                        "a healthy sweep must still leave a trace")


class PageThrottle(unittest.TestCase):
    """PRE-WIRE-BLOCKER-1: per-(symbol, reason) page throttle. Proves the FIRST page always
    fires, repeats within the reason's TTL are suppressed (but still recorded in summary), a
    resolved-then-recurring condition re-pages immediately, tiers differ, keys are per-symbol,
    the tuple-keyed prune persists the key while open (the silent-no-op landmine) and drops it
    on close, and loop-error is a blind-spot page recorded in `skipped` only."""

    def setUp(self):
        sp._recent_placements.clear()
        sp._skip_streak.clear()
        sp._unknown_page_streak.clear()
        sp._page_throttle.clear()

    def _sweep(self, *, open_orders, position, trade, session="rth", place=True,
               submit_day=_DEF_ORDER, close_ok=True, mono=1000.0, tracker=None):
        tracker = tracker if tracker is not None else _Tracker({"X": dict(trade)})
        page = mock.Mock()
        with mock.patch.object(sp, "get_open_orders", mock.Mock(return_value=open_orders)), \
             mock.patch.object(sp, "get_open_position", mock.Mock(return_value=position)), \
             mock.patch.object(sp, "submit_day_stop_order", mock.Mock(return_value=submit_day)), \
             mock.patch.object(sp, "submit_gtc_stop_order", mock.Mock(return_value=_DEF_ORDER)), \
             mock.patch.object(sp, "close_position", mock.Mock(return_value=close_ok)), \
             mock.patch.object(sp, "fetch_actual_fill_price_or_none", mock.Mock(return_value=89.5)), \
             mock.patch.object(sp, "_qhm_symbols", mock.Mock(return_value=set())), \
             mock.patch.object(sp.time, "monotonic", mock.Mock(return_value=mono)), \
             mock.patch.object(sp, "_page", page):
            summary = sp.reconcile_protection(tracker, _Risk(), session=session, place=place)
        return summary, page

    # T1 — the first occurrence of a (symbol, reason) ALWAYS pages and arms the throttle key.
    def test_first_page_always_fires(self):
        s, page = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0))
        self.assertTrue(page.called)
        self.assertEqual(s["paged"][0][1], "no stop level")
        self.assertIn(("X", "no stop level"), sp._page_throttle)

    # T2 — a repeat within the TTL suppresses the PAGE but STILL records the occurrence.
    def test_repeat_within_ttl_suppressed_but_recorded(self):
        _, p1 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=1000.0)
        s2, p2 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=1000.0 + 899.0)
        self.assertTrue(p1.called)
        self.assertFalse(p2.called, "within the 900s naked TTL the page must be suppressed")
        self.assertEqual(len(s2["paged"]), 1, "the occurrence is still recorded in the summary")

    # T3 — after the TTL elapses, the page fires again.
    def test_fires_again_after_ttl(self):
        _, p1 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=1000.0)
        _, p2 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=1000.0 + 901.0)
        self.assertTrue(p1.called)
        self.assertTrue(p2.called, "after the TTL elapses the reminder must re-fire")

    # T4 — tiers differ: conflict (60m) still suppresses where naked (15m) would have re-fired;
    #      an unmapped/mistyped reason falls to the SHORT loud tier (fail loud, never quiet).
    def test_tiered_ttl_values(self):
        self.assertEqual(sp._ttl_for("no stop level"), 900.0)
        self.assertEqual(sp._ttl_for("cover failed"), 900.0)
        self.assertEqual(sp._ttl_for("loop error"), 900.0)
        self.assertEqual(sp._ttl_for("over-covered"), 3600.0)
        self.assertEqual(sp._ttl_for("resting reducing order conflict"), 3600.0)
        self.assertEqual(sp._ttl_for("totally-unmapped-typo"), 900.0, "unmapped reason must default to SHORT/loud")
        # behavioural: over-covered (conflict) is still suppressed at 1000s elapsed
        _, p1 = self._sweep(open_orders=[_order("sell", qty=10)], position=_position("long", 3, 110.0), trade=_trade(), mono=0.0)
        s2, p2 = self._sweep(open_orders=[_order("sell", qty=10)], position=_position("long", 3, 110.0), trade=_trade(), mono=1000.0)
        self.assertTrue(p1.called)
        self.assertFalse(p2.called, "conflict tier (3600s) still suppresses at 1000s elapsed")
        self.assertEqual(s2["paged"][0][1], "over-covered")

    # T5 — resolve clears the key; a recurrence within the original TTL pages FRESH (never masked).
    def test_resolve_rearms_fresh_page(self):
        _, p1 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=0.0)
        self.assertTrue(p1.called)
        s2, _ = self._sweep(open_orders=[_order("sell", qty=3)], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=10.0)
        self.assertEqual(len(s2["already_protected"]), 1)
        self.assertNotIn(("X", "no stop level"), sp._page_throttle, "a definite good outcome must clear the throttle key")
        _, p3 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=20.0)
        self.assertTrue(p3.called, "resolved-then-recurring within the TTL must page immediately, never be masked")

    # T6 — keys are per-symbol: two naked symbols both page; both keys persist independently.
    def test_keys_independent_per_symbol(self):
        tr1 = _Tracker({"X": _trade(stop=0.0), "Y": _trade(stop=0.0)})
        _, p1 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=0.0, tracker=tr1)
        self.assertEqual(p1.call_count, 2, "both symbols page on first occurrence")
        self.assertIn(("X", "no stop level"), sp._page_throttle)
        self.assertIn(("Y", "no stop level"), sp._page_throttle)

    # T7 — LANDMINE: the tuple key must PERSIST across the end-of-sweep prune while the symbol
    #      is open (a bare-symbol prune would drop it every sweep → throttle is a silent no-op),
    #      and must be dropped once the symbol leaves the book (bounded memory).
    def test_tuple_key_persists_while_open_and_prunes_on_close(self):
        _, p1 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=0.0)
        self.assertIn(("X", "no stop level"), sp._page_throttle,
                      "key must survive the sweep prune while X is open — else the throttle is a no-op")
        _, p2 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=100.0)
        self.assertFalse(p2.called, "persisted key must suppress the repeat (behavioural proof the prune did not drop it)")
        _, p3 = self._sweep(open_orders=[], position=_position("long", 3, 110.0), trade=_trade(stop=0.0), mono=200.0, tracker=_Tracker({}))
        self.assertNotIn(("X", "no stop level"), sp._page_throttle, "a closed symbol's throttle key must be pruned")

    # T8 — cover-failed RECURS (rejected close re-enters _cover each cycle) → throttled at naked tier.
    def test_cover_failed_recurs_and_is_throttled(self):
        s1, p1 = self._sweep(open_orders=[], position=_position("long", 3, 90.0), trade=_trade(stop=95.0), close_ok=False, mono=0.0)
        self.assertEqual(s1["paged"][0][1], "cover failed")
        self.assertTrue(p1.called)
        s2, p2 = self._sweep(open_orders=[], position=_position("long", 3, 90.0), trade=_trade(stop=95.0), close_ok=False, mono=100.0)
        self.assertFalse(p2.called, "cover failed recurs but the page is throttled within the naked TTL")
        self.assertEqual(len(s2["paged"]), 1, "still recorded every sweep")

    # T9 — loop-error is a BLIND-SPOT page: recorded in `skipped` ONLY (never inflates `paged`),
    #      pages on first occurrence, and lives on the SHORT/loud tier.
    def test_loop_error_blind_spot_skipped_only(self):
        boom = mock.Mock(side_effect=RuntimeError("kaboom"))
        page = mock.Mock()
        with mock.patch.object(sp, "get_open_orders", mock.Mock(return_value=[])), \
             mock.patch.object(sp, "get_open_position", mock.Mock(return_value=_position("long", 3, 110.0))), \
             mock.patch.object(sp, "submit_day_stop_order", boom), \
             mock.patch.object(sp, "submit_gtc_stop_order", mock.Mock(return_value=_DEF_ORDER)), \
             mock.patch.object(sp, "close_position", mock.Mock(return_value=True)), \
             mock.patch.object(sp, "fetch_actual_fill_price_or_none", mock.Mock(return_value=89.5)), \
             mock.patch.object(sp, "_qhm_symbols", mock.Mock(return_value=set())), \
             mock.patch.object(sp.time, "monotonic", mock.Mock(return_value=500.0)), \
             mock.patch.object(sp, "_page", page):
            s = sp.reconcile_protection(_Tracker({"X": _trade(stop=95.0)}), _Risk(), session="rth", place=True)
        self.assertTrue(any("loop error" in str(r[1]) for r in s["skipped"]), "loop error must be recorded in skipped")
        self.assertEqual([r for r in s["paged"] if r[1] == "loop error"], [], "loop error must NOT inflate paged")
        self.assertTrue(page.called, "a blind-spot loop error must page on first occurrence")

    # ── PRE-WIRE-BLOCKER-2: ONE account-wide get_open_orders fetch, sliced per symbol ──
    def _run_multi(self, *, open_trades, account_orders, positions, submit=None):
        tracker = _Tracker(dict(open_trades))
        goo = mock.Mock(return_value=account_orders)     # account-wide (no-arg) fetch
        gop = mock.Mock(side_effect=lambda sym: positions.get(sym))
        sub = submit if submit is not None else _order("sell", oid="newstop")
        sday = mock.Mock(return_value=sub)
        page = mock.Mock()
        with mock.patch.object(sp, "get_open_orders", goo), \
             mock.patch.object(sp, "get_open_position", gop), \
             mock.patch.object(sp, "submit_day_stop_order", sday), \
             mock.patch.object(sp, "submit_gtc_stop_order", mock.Mock(return_value=sub)), \
             mock.patch.object(sp, "close_position", mock.Mock(return_value=True)), \
             mock.patch.object(sp, "fetch_actual_fill_price_or_none", mock.Mock(return_value=89.5)), \
             mock.patch.object(sp, "_qhm_symbols", mock.Mock(return_value=set())), \
             mock.patch.object(sp, "_page", page):
            summary = sp.reconcile_protection(tracker, _Risk(), session="rth", place=True)
        return summary, goo, sday, page

    @staticmethod
    def _osym(symbol, side="sell", otype="stop", qty=3, status="new", oid="o"):
        return types.SimpleNamespace(id=oid, symbol=symbol, side=side, type=otype,
                                     qty=qty, status=status, stop_price=100.0)

    # THE N->1 PROPERTY: get_open_orders is called EXACTLY ONCE regardless of position count.
    def test_partA_single_account_wide_fetch(self):
        trades = {s: _trade() for s in ("AAA", "BBB", "CCC")}
        pos = {s: _position("long", 3, 110.0) for s in trades}
        s, goo, sday, page = self._run_multi(open_trades=trades, account_orders=[], positions=pos)
        goo.assert_called_once()                          # <-- one fetch, not three
        self.assertEqual(goo.call_args, mock.call())      # <-- no-arg (account-wide), not per-symbol
        self.assertEqual(len(s["placed"]), 3, "all three naked positions get a stop from one fetch")

    # PER-SYMBOL SLICING: a symbol sees ONLY its own orders from the shared book.
    def test_partA_symbol_gets_only_its_own_orders(self):
        trades = {"AAA": _trade(), "BBB": _trade()}
        pos = {"AAA": _position("long", 3, 110.0), "BBB": _position("long", 3, 110.0)}
        # The book has a covering stop for AAA only. BBB must NOT be seen as protected by it.
        book = [self._osym("AAA", side="sell", otype="stop", qty=3)]
        s, goo, sday, page = self._run_multi(open_trades=trades, account_orders=book, positions=pos)
        goo.assert_called_once()
        self.assertIn(("AAA", 3.0, 3.0), s["already_protected"], "AAA is covered by its own stop")
        self.assertEqual([p[0] for p in s["placed"]], ["BBB"], "BBB is naked — AAA's stop does not protect it")

    # NONE FAILS-SAFE THE WHOLE BOOK: one API failure skips ALL symbols, none placed.
    def test_partA_none_fetch_fails_safe_all_symbols(self):
        trades = {s: _trade() for s in ("AAA", "BBB", "CCC")}
        pos = {s: _position("long", 3, 110.0) for s in trades}
        s, goo, sday, page = self._run_multi(open_trades=trades, account_orders=None, positions=pos)
        goo.assert_called_once()
        self.assertEqual(len(s["placed"]), 0, "an account-wide API failure must place NOTHING")
        self.assertEqual(len(s["skipped"]), 3, "every symbol fails safe on the shared None fetch")
        sday.assert_not_called()
        self.assertTrue(page.called)

    # A symbol-less order in the account book is DROPPED, never miscounted toward a real
    # position (fail-safe: a real position with no stop of its own still reads naked → places).
    def test_partA_symbolless_order_dropped(self):
        trades = {"AAA": _trade()}
        pos = {"AAA": _position("long", 3, 110.0)}
        _bad = types.SimpleNamespace(id="bad", side="sell", type="stop", qty=3, status="new", stop_price=100.0)
        # _bad has NO .symbol attribute; a real AAA stop is absent → AAA must still place.
        s, goo, sday, page = self._run_multi(open_trades=trades, account_orders=[_bad], positions=pos)
        goo.assert_called_once()
        self.assertEqual([p[0] for p in s["placed"]], ["AAA"],
                         "a symbol-less order must NOT count as AAA's protection")
        self.assertEqual(len(s["already_protected"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
