# ruff: noqa: E501
"""SWING-OVERNIGHT-1030 — fixes from the cold review (FAIL) and the board seats (Taleb risk, Thorp sizing).

One test per item; each fails on the pre-fix code (see the per-test note) and passes after. Every broker call goes to
the FakeBroker of tests/test_swing_overnight.py; nothing reaches Alpaca."""
import os
import unittest
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from execution import swing_overnight as so
from tests.test_swing_overnight import CLS, MKT, OPG1, Base, FakeBroker, cid, et, expected_stop


class Item1Booking(Base):
    """Cold review #1: never book a night without an entry fill until its close has passed."""

    def test_delayed_cls_rejection_still_falls_back(self):
        # pre-fix: at 15:52 the terminal cls booked the night "unfilled" and advanced `since` past it, so at 15:55 the
        # cls vanished from the listing and _fallback waited on "cls UNKNOWN" forever — no fallback order
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.fb.by_coid(CLS).status = "rejected"            # the broker rejects it later (delayed)
        self.at(2026, 10, 29, 15, 52)
        self.settle()
        self.assertEqual(self.eng.state["nights"]["20261029"]["status"], "entry_pending")
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        mkt = self.fb.by_coid(MKT)
        self.assertEqual(mkt.qty, 10)
        self.fb.fill(mkt, 10, 294.5)
        self.settle()
        self.invariant()

    def test_immediate_rejection_then_fallback_fill_is_never_booked_unfilled(self):
        # pre-fix: the immediate cls rejection booked the night "unfilled" at 15:40; the fallback lot was then carried
        # all night under an "unfilled" night (idle ticks skipped reconcile/coverage)
        self.fb.next_submit = ["reject"]
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        self.fb.fill(self.fb.by_coid(MKT), 10, 294.5)
        self.at(2026, 10, 29, 15, 56)
        self.settle()
        self.invariant()
        trans = [kw["current"] for kw in self.ev("state_transition") if kw.get("night") == "20261029"]
        self.assertNotIn("unfilled", trans)
        self.assertEqual(self.eng.state["nights"]["20261029"]["status"], "holding")
        self.at(2026, 10, 29, 20, 0)                        # outside every window: still reconciles (not idle)
        self.assertNotIn("no-op", self.eng.tick().note)

    def test_no_fill_night_is_booked_unfilled_after_the_close(self):
        self.fb.next_submit = ["reject", ("day", "reject")]
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        self.assertEqual(self.eng.state["nights"]["20261029"]["status"], "entry_pending")
        self.at(2026, 10, 29, 16, 1)
        self.settle()
        self.assertEqual(self.eng.state["nights"]["20261029"]["status"], "unfilled")

    def test_a_stale_booking_never_hides_held_shares(self):
        # position-driven (round 6): a night's booking status is reporting only — the held shares stay protected
        self.enter_and_fill()
        self.eng.state["nights"]["20261029"]["status"] = "unfilled"     # e.g. a stale booking from an older build
        for o in self.fb.live("sell"):
            o.status = "canceled"                                        # and its stop gone
        self.at(2026, 10, 29, 16, 1)
        self.settle()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10])


class Item2StopBasis(Base):
    """Cold review #2: the stop is priced from the holding night's own entry fills, never a cross-night average."""

    def test_state_loss_after_an_earlier_night_prices_from_tonights_lot(self):
        # pre-fix: with `since` reset, reconcile averaged night 1 ($294.10) and night 2 ($300) → a stop near $240,
        # far below tonight's correct $288
        self.enter_and_fill(price=294.10)
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(self.fb.by_coid(OPG1), 10, 295.0)
        self.settle()
        self.fd.spy_last = date(2026, 10, 29)
        self.at(2026, 10, 30, 15, 40)
        self.settle()
        cls2 = self.fb.by_coid(cid("ecls", 1, self.fb.cal[date(2026, 10, 30)][1]))
        self.state_path.unlink()                             # state lost: `since` resets, all history is listed
        eng2 = self.engine()
        self.at(2026, 10, 30, 16, 0)
        self.fb.fill(cls2, 10, 300.0)
        self.settle(eng2)
        self.clock.now += timedelta(seconds=10)              # state lost: the position is confirmed on a later pass
        self.settle(eng2)
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"]
        self.assertEqual(len(stop), 1)
        q = eng2.state["nights"]["20261030"]["q"]
        self.assertAlmostEqual(stop[0].stop_price, expected_stop(300.0, q), places=6)
        self.assertEqual(stop[0].qty, 10)

    def test_unreadable_listing_price_stop_priced_from_the_activity(self):
        # round 9: quantities and prices come from the fill activities; a listing without a fill price changes nothing
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 10, 294.10)
        cls.filled_avg_price = None
        self.settle()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"]
        self.assertEqual(len(stop), 1)
        q = self.eng.state["nights"]["20261029"]["q"]
        self.assertAlmostEqual(stop[0].stop_price, expected_stop(294.10, q), places=6)

    def test_night_positions_is_per_night_fifo(self):
        v = lambda coid, side, fq, px, t: {"coid": coid, "side": side, "filled_qty": fq, "filled_avg_price": px,  # noqa: E731
                                           "filled_at": t, "submitted_at": t,
                                           "parsed": so.parse_coid(coid, so.datetime(2026, 10, 31, tzinfo=so.ET))}
        d1 = self.fb.cal[date(2026, 10, 29)][1]
        d2 = self.fb.cal[date(2026, 10, 30)][1]
        views = {x["coid"]: x for x in (
            v(so.build_coid(d1, "ecls", 1), "buy", 10, 200.0, d1),
            v(so.build_coid(d1, "opg", 1), "sell", 10, 201.0, d1 + timedelta(hours=18)),
            v(so.build_coid(d2, "ecls", 1), "buy", 4, 300.0, d2),
            v(so.build_coid(d2, "emkt", 1), "buy", 6, 310.0, d2 + timedelta(minutes=1)),
            v(so.build_coid(d2, "stop", 1), "sell", 5, 280.0, d2 + timedelta(hours=1)),
        )}
        pos = so.night_positions(views)
        self.assertEqual(pos["20261029"], (0, None))
        self.assertEqual(pos["20261030"], (5, 310.0))       # FIFO: the 5 sold came from the 4 @300 + 1 @310


class Item3RejectedStop(Base):
    """Cold review #3: a rejected stop pages, backs off, and the attempt counter cannot outgrow the id pattern."""

    def test_rejected_stop_pages_and_backs_off(self):
        # pre-fix: "rejected" returned "acted"; run() re-passed with no wait, re-submitting every pass, never paging
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.fb.next_submit = [("gtc", "reject")] * 10
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(self.fb.by_coid(CLS), 10, 294.10)
        self.settle()
        gtc = lambda: [s for s in self.fb.submits if s["tif"] == "gtc"]  # noqa: E731
        self.assertEqual(len(gtc()), 1)
        self.assertTrue(any("stop order REJECTED" in p for p in self.pages))
        self.clock.now += timedelta(seconds=float(so.config.SWING_TREND_NIGHT_POLL_SECONDS) - 1)
        self.settle()
        self.assertEqual(len(gtc()), 1, "still backing off")
        self.clock.now += timedelta(seconds=2)
        self.settle()
        self.assertEqual(len(gtc()), 2)
        self.clock.now += timedelta(seconds=float(so.config.SWING_TREND_NIGHT_POLL_SECONDS) + 1)
        self.settle()
        self.assertEqual(len(gtc()), 2, "the second rejection doubles the wait")

    def test_attempt_counter_bounded_by_the_pattern(self):
        # pre-fix: build_coid accepted attempt 10000 — an id parse_coid rejects, so the order would be invisible
        self.assertTrue(so.attempt_fits(9999))
        self.assertFalse(so.attempt_fits(10000))
        with self.assertRaises(ValueError):
            so.build_coid(self.fb.cal[date(2026, 10, 29)][1], "stop", 10000)
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.eng.state["nights"]["20261029"]["seq"]["stop"] = 9999
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(self.fb.by_coid(CLS), 10, 294.10)
        self.eng.tick()
        self.assertEqual([s for s in self.fb.submits if s["tif"] == "gtc"], [])
        self.assertTrue(any("attempts exhausted" in p for p in self.pages))


class Item4Mismatch(Base):
    """Board risk seat: a broker/fill mismatch never leaves a held lot unprotected or unsold."""

    def test_broker_below_fills_protects_and_exits_the_broker_quantity(self):
        # pre-fix: reconcile returned ok=False and tick() returned before coverage/exit — stuck all night
        self.enter_and_fill()
        self.fb.manual_sell(3, 290.0)                     # 3 sh sold by hand (a foreign SELL fill activity)
        self.at(2026, 10, 29, 16, 5)
        self.settle()
        # position-driven (round 6): a DEcrease applies at once — the stop follows the broker down to 7
        live = self.fb.live("sell")
        self.assertEqual(sum(o.qty - o.filled_qty for o in live), 7)
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 7, "never sells more than the broker holds")
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 7, 295.0)
        self.settle()
        self.assertEqual(self.fb.position, 0)
        for s_ in (6, 12, 18):                               # rule 4: same lower read a poll later, orders quiet
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
        rec = self.eng.state["closed"][0]                    # booking is reporting only
        self.assertEqual((rec["sold_qty"], rec["unaccounted_qty"]), (7, 3))
        self.assertAlmostEqual(rec["pnl"], 7 * (295.0 - 294.10), places=6)
        # round 8: the manual sale is recorded from its fill activity, with its own price, as an unbooked line
        self.assertTrue(any("sold OUTSIDE trend_night" in p for p in self.pages))
        self.assertEqual(rec["foreign_sold_qty"], 3)
        self.assertAlmostEqual(rec["foreign_unbooked_pnl_usd"], round(3 * (290.0 - 294.10), 2))

    def test_broker_above_fills_protects_only_the_lot(self):
        self.enter_and_fill()
        self.fb.position = 13
        self.at(2026, 10, 29, 16, 5)
        self.settle()
        self.assertEqual(sum(o.qty - o.filled_qty for o in self.fb.live("sell")), 10)


class Item5NightLoss(Base):
    """Board risk seat: the night-loss review uses qty × entry × q, not b × equity."""

    def test_loss_above_the_modelled_worst_night_pages(self):
        # pre-fix: threshold b × equity = 0.05 × 2555 = $127.75; a $121 loss on 10 sh × $294.10 × q 0.04 ($117.64)
        # did not page
        self.enter_and_fill(price=294.10)
        self.fb.next_submit = [("opg", "reject")]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.settle()
        mkt = [o for o in self.fb.live("sell") if o.order_type == "market"][0]
        self.fb.fill(mkt, 10, 282.0)
        self.settle()
        rec = self.eng.state["closed"][0]
        self.assertAlmostEqual(rec["loss_budget_usd"], 10 * 294.10 * rec["q"], places=6)
        self.assertTrue(any("modelled worst night" in p for p in self.pages))


class Item6PooledEdge(Base):
    """Board sizing seat: a non-positive pooled mean still trades at the floor but pages each night."""

    def test_nonpositive_pooled_mean_pages_and_still_trades(self):
        n, mean, sd = 500, -0.0002, 0.01
        self.eng.state["prior_cache"] = {"ok": True, "n": n, "mean": mean, "var": sd * sd, "sd": sd, "sum": mean * n,
                                         "sumsq": sd * sd * (n - 1) + n * mean * mean, "n_star": 200.0, "alpha": 0.05,
                                         "rho": so.mixture_rho(200.0, 0.05), "date": "2026-10-29"}
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.assertTrue(any("pooled replay + live mean" in p for p in self.pages))
        self.assertEqual(self.fb.by_coid(CLS).qty, 10)
        self.assertTrue(self.ev("pooled_edge_nonpositive"))


class NanosecondBroker(FakeBroker):
    """Alpaca as it really lists: submitted_at strings carry NANOSECONDS, and `after` is INCLUSIVE at the microsecond
    precision a datetime holds — so after=<the newest order's stamp> lists that order again."""

    def list_orders(self, symbol, after):
        if not self.list_ok:
            raise RuntimeError("orders endpoint down")
        out = []
        for o in self.orders.values():
            if o.symbol != symbol or o.client_order_id in self.hidden:
                continue
            if after is not None and o.submitted_at < after:           # inclusive, like the truncated real stamp
                continue
            ns = o.submitted_at.astimezone(so.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "168Z"
            out.append(SimpleNamespace(**{**vars(o), "submitted_at": ns}))
        return out


class BlockerNanosecondSince(Base):
    """Cold review blocker: after a normal booked night the next trend night must enter normally."""

    def setUp(self):
        super().setUp()
        nb = NanosecondBroker(self.clock)
        nb.cal = self.fb.cal
        self.fb = nb
        self.eng = self.engine()

    def _night_one(self):
        self.enter_and_fill(price=294.10)
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(self.fb.by_coid(OPG1), 10, 295.0)
        self.settle()
        self.assertEqual(self.eng.state["nights"]["20261029"]["status"], "closed")

    def test_full_night_then_a_second_night_enters_normally(self):
        # pre-fix: since = max(stamp) re-listed the filled opg sell → tn_held = −10 → reconcile blocked forever
        self._night_one()
        self.fd.spy_last = date(2026, 10, 29)
        self.at(2026, 10, 30, 15, 40)
        self.settle()
        cls2 = self.fb.by_coid(cid("ecls", 1, self.fb.cal[date(2026, 10, 30)][1]))
        self.at(2026, 10, 30, 16, 0)
        self.fb.fill(cls2, 10, 300.0)
        self.settle()
        stops = [o for o in self.fb.live("sell") if o.order_type == "stop"]
        q = self.eng.state["nights"]["20261030"]["q"]
        self.assertEqual([(o.qty, o.stop_price) for o in stops], [(10, expected_stop(300.0, q))])
        self.assertEqual(self.ev("reconcile_blocked"), [])
        self.assertEqual(self.eng.state["nights"]["20261030"]["status"], "holding")

    def test_since_advances_one_microsecond_past_the_newest_booked_order(self):
        self._night_one()
        newest = max(o.submitted_at for o in self.fb.orders.values())
        self.assertEqual(so._aware(self.eng.state["since"]), newest + timedelta(microseconds=1))

    def test_a_relisted_booked_order_never_counts_as_held(self):
        # (a) on its own: even with `since` left exactly on the newest stamp (an older state file), the booked
        # night's re-listed sell is not counted, so nothing blocks
        self._night_one()
        newest = max(o.submitted_at for o in self.fb.orders.values())
        self.eng.state["since"] = newest.isoformat()
        r = self.eng.reconcile()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual((r.held, r.own), (0, 0))
        self.assertTrue(any(_coid == OPG1 for _coid in r.views), "the opg sell WAS re-listed")

    def test_nanosecond_string_stamps_parse(self):
        self.assertEqual(so._aware("2026-07-07T14:14:11.271472168Z"),
                         so.datetime(2026, 7, 7, 14, 14, 11, 271472, tzinfo=so.UTC))


class NitPnlCheck(Base):
    def test_half_cent_rounding_is_not_a_mismatch_and_missing_date_is_handled(self):
        self.at(2026, 10, 30, 9, 35)
        self.eng._now = self.clock.now
        self.fb.fill_log = [{"order_id": "a", "side": "buy", "qty": "10", "price": "294.105", "transaction_time": "1"},
                            {"order_id": "b", "side": "sell", "qty": "10", "price": "295.0", "transaction_time": "2"}]
        self.eng.state["closed"] = [{"night": "20261029", "date": None, "exit_date": "2026-10-30", "pnl": 8.945,
                                     "order_ids": ["a", "b"]}]
        ok, detail = self.eng._pnl_matches("2026-10-30")       # fills FIFO 8.95 vs booked 8.945: half a cent
        self.assertTrue(ok, detail)
        self.eng.state["closed"][0]["pnl"] = 8.80               # beyond the slack → a real mismatch
        self.assertFalse(self.eng._pnl_matches("2026-10-30")[0])


class NitPaperBeforeAttempt(Base):
    def test_failed_paper_check_burns_no_attempt(self):
        self.enter_and_fill()
        seq = dict(self.eng.state["nights"]["20261029"]["seq"])
        with mock.patch.dict(os.environ, {"ALPACA_PAPER_ACCOUNT_NUMBER": "PA-OTHER"}):
            for _ in range(3):
                self.assertEqual(self.eng._submit("20261029", "stop", "sell", 1, "gtc", stop_price=Decimal("1.00")),
                                 "blocked")
        self.assertEqual(self.eng.state["nights"]["20261029"]["seq"], seq)


class NitFrozenLastTradeBasis(Base):
    def test_last_trade_basis_is_frozen_for_the_night(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 10, 294.10)
        cls.filled_avg_price = None
        self.settle()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        first = stop.stop_price
        self.fd.price = 290.0                                   # the last trade moves
        for minute in (1, 2, 3):
            self.at(2026, 10, 29, 16, minute)
            self.settle()
        self.assertEqual(self.fb.replaces, [], "stop not chased by the last trade")
        self.assertEqual([o for o in self.fb.live("sell") if o.order_type == "stop"][0].stop_price, first)


class NitUnknownStopPages(Base):
    def test_unknown_stop_pages_after_a_few_passes(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.fb.next_submit = [("gtc", "unknown_accept")]
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(self.fb.by_coid(CLS), 10, 294.10)
        self.fb.lookup_unknown = True
        self.fb.hidden.add(cid("stop", 1))
        for _ in range(so._UNRESOLVED_POLL_ATTEMPTS + 2):
            self.clock.now += timedelta(seconds=5)
            self.eng.tick()
        self.assertTrue(any("UNKNOWN for" in p for p in self.pages))


class NitLossSlack(Base):
    def test_ordinary_stop_out_at_the_rounded_stop_does_not_page(self):
        self.at(2026, 10, 30, 9, 35)
        self.eng._now = self.clock.now
        q = 0.04
        stop = expected_stop(294.10, q)                         # 282.33 (282.336 rounded down)
        loss = 10 * (294.10 - stop)                             # 117.70 > 10 × 294.10 × 0.04 = 117.64
        base = {"night": "20261029", "qty": 10, "q": q, "loss_budget_usd": 10 * 294.10 * q}
        self.eng._after_booking(dict(base, pnl=-loss))
        self.assertEqual(self.pages, [])
        self.eng._after_booking(dict(base, night="20261028", pnl=-(10 * 294.10 * q + 0.11)))
        self.assertTrue(any("modelled worst night" in p for p in self.pages))


class Round4Probes(Base):
    """Cold review round 4 — the reviewer's exact probe sequences (probe_reopen.py, probe2.py)."""

    def _sell_at_open(self, entry_close_day=date(2026, 10, 29), exit_day=date(2026, 10, 30), price=295.0):
        self.at(exit_day.year, exit_day.month, exit_day.day, 9, 20)
        self.settle()
        opg = [o for o in self.fb.live("sell") if o.time_in_force == "opg"][0]
        self.assertIsNotNone(so.parse_coid(opg.client_order_id, self.clock.now))
        self.at(exit_day.year, exit_day.month, exit_day.day, 9, 30)
        self.fb.fill(opg, opg.qty, price)
        self.settle()
        return opg

    def test_probe2_two_zero_reads_then_shares_back(self):
        # pre-fix: the night was booked "closed" with 10 unaccounted, `since` advanced, and the 10 sh that came back
        # had no stop and no exit
        self.enter_and_fill()
        self.fb.position = 0
        self.at(2026, 10, 29, 16, 5)
        self.eng.tick()
        self.at(2026, 10, 29, 16, 6)
        self.eng.tick()
        self.fb.position = 10
        self.at(2026, 10, 29, 16, 7)
        self.settle()
        self.at(2026, 10, 29, 16, 7, 10)                     # position-driven: the increase is confirmed a poll later
        self.settle()
        self.assertEqual([(o.qty, o.order_type) for o in self.fb.live("sell")], [(10, "stop")])
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual([(o.qty, o.order_type) for o in self.fb.live("sell")], [(10, "stop")], "overnight")
        opg = self._sell_at_open()
        self.assertEqual(opg.qty, 10)
        self.assertEqual(self.fb.position, 0)
        self.assertEqual(self.fb.live("sell"), [])

    def test_probe_reopen_stale_closed_night_without_a_stop(self):
        # pre-fix: reopened then re-booked "closed" in the same tick → no stop, nothing sold at the open
        self.enter_and_fill()
        for o in self.fb.live("sell"):
            o.status = "canceled"
        self.eng.state["nights"]["20261029"]["status"] = "closed"
        self.at(2026, 10, 29, 16, 5)
        self.settle()
        self.assertEqual([(o.qty, o.order_type) for o in self.fb.live("sell")], [(10, "stop")])
        self._sell_at_open()
        self.assertEqual(self.fb.position, 0)

    def test_zero_read_shrinks_at_once_and_the_lot_is_reprotected_next_pass(self):
        # position-driven (round 6, rule 1): a decrease applies at once; the shares coming back are re-protected only
        # once a later pass, a poll interval on, confirms them
        self.enter_and_fill()
        stop = self.fb.live("sell")[0]
        self.fb.position = 0
        self.at(2026, 10, 29, 16, 5)
        self.settle()
        self.assertEqual(stop.status, "canceled")
        self.assertEqual(self.fb.live("sell"), [])
        self.fb.position = 10
        self.clock.now += timedelta(seconds=2)
        self.settle()
        # two-source minimum: own fills say 10 and the broker says 10 again → re-protected at once
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10])

    def test_unknown_sell_after_the_open_pages(self):
        # pre-fix: _exit_step returned "wait" forever with no page while a sell was UNKNOWN after the open
        self.enter_and_fill()
        self.fb.next_submit = [("opg", "unknown_accept")]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.fb.hidden.add(OPG1)
        self.fb.lookup_unknown = True
        self.at(2026, 10, 30, 9, 30)
        for _ in range(so._UNRESOLVED_POLL_ATTEMPTS + 2):
            self.clock.now += timedelta(seconds=5)
            self.eng.tick()
        self.assertTrue(any("UNKNOWN for" in p for p in self.pages))


class Round4NotFound(unittest.TestCase):
    """Nit 4: only a genuine not-found answer resolves a client-id lookup as absent."""

    def _lookup(self, exc):
        client = SimpleNamespace(get_order_by_client_id=mock.Mock(side_effect=exc))
        with mock.patch.object(so.TNBroker, "_client", staticmethod(lambda: client)):
            return so.TNBroker().get_by_coid(CLS)[0]

    def test_not_found_classification(self):
        http404 = RuntimeError("not found")
        http404.status_code = 404
        self.assertEqual(self._lookup(http404), "absent")
        self.assertEqual(self._lookup(RuntimeError('{"code":40410000,"message":"order not found"}')), "absent")
        self.assertEqual(self._lookup(RuntimeError("read timed out after 404 ms")), "unknown")
        self.assertEqual(self._lookup(RuntimeError("upstream proxy: 404 page")), "unknown")
        http500 = RuntimeError("server error")
        http500.status_code = 500
        self.assertEqual(self._lookup(http500), "unknown")


class ThreeNightsEndToEnd(Base):
    """Three consecutive trend nights; a transient zero position read on night 2. Every night has a live stop
    overnight and is sold at its next open; no false "left the account" page."""

    def test_three_nights(self):
        self.fb.cal[date(2026, 11, 3)] = (so.datetime(2026, 11, 3, 9, 30, tzinfo=so.ET),
                                          so.datetime(2026, 11, 3, 16, 0, tzinfo=so.ET))
        nights = [(date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 30)),
                  (date(2026, 10, 30), date(2026, 10, 29), date(2026, 11, 2)),
                  (date(2026, 11, 2), date(2026, 10, 30), date(2026, 11, 3))]
        for i, (d, prior, nxt) in enumerate(nights):
            with self.subTest(night=str(d)):
                self.fd.spy_last = prior
                self.at(d.year, d.month, d.day, 15, 40)
                self.settle()
                cls = self.fb.by_coid(cid("ecls", 1, self.fb.cal[d][1]))
                self.at(d.year, d.month, d.day, 16, 0)
                self.fb.fill(cls, cls.qty, 294.0 + i)
                self.settle()
                if i == 1:                                   # transient zero read
                    self.fb.position = 0
                    self.at(d.year, d.month, d.day, 16, 5)
                    self.eng.tick()
                    self.fb.position = cls.qty
                    self.clock.now += timedelta(seconds=2)
                    self.settle()
                    self.clock.now += timedelta(seconds=10)  # position-driven: re-protected once confirmed
                    self.settle()
                self.at(d.year, d.month, d.day, 20, 0)
                self.settle()
                stops = [o for o in self.fb.live("sell") if o.order_type == "stop"]
                self.assertEqual([o.qty for o in stops], [cls.qty], "live stop overnight")
                self.at(nxt.year, nxt.month, nxt.day, 9, 20)
                self.settle()
                opg = self.fb.by_coid(cid("opg", 1, self.fb.cal[d][1]))
                self.at(nxt.year, nxt.month, nxt.day, 9, 30)
                self.fb.fill(opg, opg.qty, 295.0 + i)
                self.settle()
                self.assertEqual(self.fb.position, 0, "sold at the next open")
                self.assertEqual(self.fb.live("sell"), [])
        self.assertEqual([x["unaccounted_qty"] for x in self.eng.state["closed"]], [0, 0, 0])
        self.assertEqual(len(self.eng.state["closed"]), 3)
        self.assertFalse(any("left the account" in p for p in self.pages))
        self.assertEqual(self.ev("reconcile_blocked"), [])


class Round5ColdProbe(Base):
    """Cold review round 5 — the reviewer's probe (cold13/test_probe_cold13.py): a stale-HIGH position read right after
    the night-2 exit fill, with a confirmed manual sale booked on night 1."""

    def _night1_with_manual_sale(self):
        self.enter_and_fill(price=294.10)
        self.fb.manual_sell(3, 290.0)                     # 3 sh sold by hand (a foreign SELL fill activity)
        for minute in (5, 6):
            self.at(2026, 10, 29, 16, minute)
            self.settle()
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(self.fb.by_coid(OPG1), 7, 295.0)
        self.settle()
        self.at(2026, 10, 30, 9, 31)
        self.settle()
        rec = dict(self.eng.state["closed"][0])
        self.assertEqual((rec["sold_qty"], rec["unaccounted_qty"]), (7, 3))
        return rec

    def _night2_to_stale_read(self):
        self.fd.spy_last = date(2026, 10, 29)
        self.at(2026, 10, 30, 15, 40)
        self.settle()
        c2 = self.fb.cal[date(2026, 10, 30)][1]
        cls2 = self.fb.by_coid(cid("ecls", 1, c2))
        self.at(2026, 10, 30, 16, 0)
        self.fb.fill(cls2, cls2.qty, 300.0)
        self.settle()
        self.at(2026, 11, 2, 9, 20)
        self.settle()
        opg2 = self.fb.by_coid(cid("opg", 1, c2))
        self.at(2026, 11, 2, 9, 30)
        self.fb.fill(opg2, cls2.qty, 301.0)               # flat at the broker …
        self.fb.position = cls2.qty                       # … but the position endpoint lags one read
        return cls2.qty

    def _no_oversell(self):
        self.assertGreaterEqual(self.fb.position, 0, "never short")
        self.assertEqual(self.ev("reconcile_blocked"), [])

    def test_A_stale_high_read_sells_nothing(self):
        rec1 = self._night1_with_manual_sale()
        q = self._night2_to_stale_read()
        n = len(self.fb.submits)
        self.eng.tick()
        self.assertEqual([s for s in self.fb.submits[n:] if s["side"] == "sell"], [], "no sell the broker can't fill")
        self.fb.position = 0
        self.at(2026, 11, 2, 9, 30, 10)
        for _ in range(5):
            self.eng.tick()
        self.assertEqual(self.fb.position, 0)
        self._no_oversell()
        self.assertEqual(self.eng.state["closed"][0], rec1, "the 10/29 record is final")
        self.assertEqual([x["night"] for x in self.eng.state["closed"]], ["20261029", "20261030"])
        self.assertEqual(self.eng.state.get("special", {}), {}, "a one-read surplus is not a lot")
        self.assertTrue(q)

    def test_B_no_short_and_the_next_night_enters(self):
        rec1 = self._night1_with_manual_sale()
        self._night2_to_stale_read()
        self.eng.tick()
        self.fb.position = 0
        self.assertEqual([o for o in self.fb.live("sell") if o.order_type == "market"], [])
        for m in range(1, 4):
            self.at(2026, 11, 2, 9, 30 + m)
            self.eng.tick()
        self._no_oversell()
        self.assertEqual(self.eng.state["closed"][0], rec1)
        self.fd.spy_last = date(2026, 10, 30)
        self.at(2026, 11, 2, 15, 40)
        self.settle()
        self.assertEqual(self.fb.by_coid(cid("ecls", 1, self.fb.cal[date(2026, 11, 2)][1])).qty, 10)


def _lag_fill(o, qty, price, clock, fb=None):
    """The listing — and, with `fb`, the account fill activity — report a fill that the broker POSITION does not show
    yet (the caller sets the position)."""
    prev = o.filled_qty * (o.filled_avg_price or 0.0)
    o.filled_qty += qty
    o.filled_avg_price = (prev + qty * price) / o.filled_qty
    o.status = "filled" if o.filled_qty >= o.qty else "partially_filled"
    o.filled_at = clock.now.astimezone(so.UTC)
    if fb is not None:
        fb._n += 1
        fb.fill_log.append({"id": f"act-{fb._n}", "order_id": o.id, "symbol": fb.sym, "side": o.side,
                            "qty": str(qty), "price": str(price), "transaction_time": clock.now.astimezone(so.UTC).isoformat()})


class TwoSourceBase(Base):
    """Round 7 invariants, asserted after every step: never a resting sell above the TRUE position, never short."""

    def safe(self, true_position=None):
        pos = self.fb.position if true_position is None else true_position
        self.assertGreaterEqual(self.fb.position, 0, "never short")
        rest = sum(o.qty - o.filled_qty for o in self.fb.live("sell"))
        self.assertLessEqual(rest, max(pos, 0), f"resting sells {rest} above the true position {pos}")

    def step(self, seconds=6, n=1):
        for _ in range(n):
            self.clock.now += timedelta(seconds=seconds)
            self.settle()

    def next_night_enters(self, day=date(2026, 10, 30), prior=date(2026, 10, 29)):
        self.fd.spy_last = prior
        self.at(day.year, day.month, day.day, 15, 40)
        self.settle()
        self.assertTrue(self.fb.by_coid(cid("ecls", 1, self.fb.cal[day][1])), "the next night enters")


class Round7ForeignAndShort(TwoSourceBase):
    def test_manual_buy_is_paged_never_traded_and_blocks_entry_until_resolved(self):
        self.fb.position = 5                              # the owner buys 5 QQQM by hand
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.step(n=2)
        self.assertEqual([s for s in self.fb.submits], [], "never traded")
        self.assertTrue(any("not trend_night's" in p for p in self.pages))
        self.fb.position = 0                              # the owner sells them
        self.at(2026, 10, 29, 15, 45)
        self.settle()
        self.assertEqual(self.fb.by_coid(CLS).qty, 10, "entry resumes once flat")

    def test_short_submits_nothing_pages_and_blocks_entry(self):
        self.fb.position = -3
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.step(n=2)
        self.assertEqual(self.fb.submits, [], "trend_night submits NOTHING while the account is short")
        self.assertTrue(any("SHORT 3" in p for p in self.pages))
        self.fb.position = 0
        self.at(2026, 10, 29, 15, 45)
        self.settle()
        self.assertEqual(self.fb.by_coid(CLS).qty, 10)


class Round7ColdSix(TwoSourceBase):
    """The reviewer's cold6 probes (scratchpad/cold6/test_cold6.py) with the round-7 end conditions asserted."""

    def _exit_to_opg(self):
        self.enter_and_fill()
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        return self.fb.by_coid(OPG1)

    def test_B_own_buy_position_before_listing_is_never_double_counted(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.position = 10                             # the position shows the buy first
        self.settle()
        self.step()
        self.safe()
        self.assertEqual([s for s in self.fb.submits if s["side"] == "sell"], [], "own orders still say 0")
        _lag_fill(cls, 10, 294.10, self.clock, self.fb)            # the listing catches up
        self.step()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10], "protected in one poll")
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 10)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 10, 295.0)
        self.step(n=3)
        self.safe()
        self.assertEqual(self.fb.position, 0)
        self.assertEqual([s for s in self.fb.submits if s["side"] == "buy"], [self.fb.submits[0]], "one buy only")
        self.next_night_enters()

    def test_D_partial_reflection_never_sells_against_a_flat_account(self):
        opg = self._exit_to_opg()
        self.at(2026, 10, 30, 9, 30)
        _lag_fill(opg, 10, 295.0, self.clock, self.fb)             # listing first; the position still says 10
        self.settle()
        self.fb.position = 5                              # partly reflected — TRULY the account is flat
        self.step(n=3)
        self.safe(true_position=0)
        self.assertEqual([s for s in self.fb.submits if s["side"] == "sell" and s["tif"] == "day"], [])
        self.fb.position = 0
        self.step(n=4)
        self.safe()
        self.next_night_enters()

    def test_E_own_sell_position_before_listing_no_phantom_short(self):
        opg = self._exit_to_opg()
        self.at(2026, 10, 30, 9, 30)
        self.fb.cancel = lambda oid: (self.fb.cancels.append(oid) or True)   # the opg already filled: cancel no-ops
        self.fb.position = 0                              # the position shows the opg fill first
        self.eng.tick()
        _lag_fill(opg, 10, 295.0, self.clock, self.fb)
        for _ in range(3):
            self.clock.now += timedelta(seconds=6)
            self.eng.tick()
        self.safe()
        self.assertFalse(any("SHORT" in p for p in self.pages))
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["unaccounted_qty"]), (10, 0))
        self.next_night_enters()

    def test_F_overnight_stop_fill_position_before_listing(self):
        self.enter_and_fill()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        self.fb.cancel = lambda oid: (self.fb.cancels.append(oid) or True)
        self.at(2026, 10, 29, 20, 0)
        self.fb.position = 0
        self.eng.tick()
        _lag_fill(stop, 10, 282.0, self.clock, self.fb)
        for _ in range(2):
            self.clock.now += timedelta(seconds=60)
            self.eng.tick()
        self.safe()
        self.assertFalse(any("SHORT" in p for p in self.pages))
        self.assertEqual(self.eng.state["closed"][0]["sold_qty"], 10)
        self.next_night_enters()

    def test_I_state_loss_after_exit_fill_with_a_stale_high_position(self):
        opg = self._exit_to_opg()
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 10, 295.0)
        self.fb.position = 10                             # listing filled, position stale-high
        self.state_path.unlink()
        self.eng = self.engine()
        for _ in range(3):
            self.clock.now += timedelta(seconds=6)
            self.settle()
            self.safe(true_position=0)
        self.assertEqual([s for s in self.fb.submits if s["side"] == "sell" and s["tif"] == "day"], [],
                         "no second sell of 10")
        self.fb.position = 0
        self.step(n=2)
        self.next_night_enters()


class Round7EarlierProbes(TwoSourceBase):
    """cold5 / cold13 probes under the two-source rules."""

    def test_P3_state_loss_after_a_manual_sale_resizes_the_stop(self):
        self.enter_and_fill()
        old = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        self.fb.manual_sell(3, 290.0)                     # 3 sh sold by hand (a foreign SELL fill activity)
        self.state_path.unlink()
        self.eng = self.engine()
        self.at(2026, 10, 29, 16, 5)
        self.settle()
        self.safe()
        self.assertEqual(old.status, "replaced", "resized by replace_order_by_id")
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [7])
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = [o for o in self.fb.live("sell") if o.time_in_force == "opg"][0]
        self.assertEqual(opg.qty, 7)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 7, 295.0)
        self.step(n=4)
        self.assertEqual(self.fb.position, 0)
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["unaccounted_qty"]), (7, 3))
        self.next_night_enters()

    def test_P3b_held_for_orders_rejections_page_back_off_and_stay_capped(self):
        self.enter_and_fill()
        self.fb.next_submit = ["held_for_orders"] * 50
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        for s_ in range(0, 60, 5):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
            self.safe()
        self.assertTrue(any("REJECTED" in p for p in self.pages))
        exit_sells = [s for s in self.fb.submits if s["side"] == "sell" and s["tif"] == "day"]
        self.assertLessEqual(len(exit_sells), 5, exit_sells)
        b = self.eng.state["backoff"]
        cap = float(so.config.SWING_TREND_NIGHT_SWAP_MIN) * 60
        for kind, rec in b.items():
            wait = (so._aware(rec["retry_after"]) - self.clock.now).total_seconds()
            self.assertLessEqual(wait, cap, kind)

    def test_unbooked_left_account_fails_the_result_check(self):
        self.enter_and_fill()
        self.fb.manual_sell(3, 290.0)                     # 3 sh sold by hand (a foreign SELL fill activity)
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = [o for o in self.fb.live("sell") if o.time_in_force == "opg"][0]
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 7, 295.0)
        self.step(n=4)
        self.at(2026, 10, 30, 9, 36)
        self.settle()
        audit = [a for a in self.ev("trend_night_audit") if a["check"] == "open"][-1]
        self.assertFalse(audit["passed"])
        line = audit["pnl"]["20261029"]
        self.assertEqual(line["unbooked_qty"], 3)
        self.assertAlmostEqual(line["unbooked_cost_usd"], round(3 * 294.10, 2))


class Round8ColdSeven(TwoSourceBase):
    """Cold review round 8 — the reviewer's cold7 probes (scratchpad/cold7/test_cold7.py, test_cold7b.py): a departure
    is recorded only from a foreign SELL fill activity; a lagging read or listing never persists anything."""

    def _book_and_next(self, entry=294.10, exit_px=None, sold=10, day=date(2026, 10, 30), prior=date(2026, 10, 29)):
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["unaccounted_qty"]), (sold, 0))
        if exit_px is not None:
            self.assertAlmostEqual(rec["pnl"], sold * (exit_px - entry), places=6)
        self.assertEqual(self.eng.state["nights"]["20261029"].get("left_account", 0), 0)
        self.next_night_enters(day, prior)

    def test_1_stop_fill_with_a_lagging_listing_books_the_true_loss(self):
        self.enter_and_fill()
        self.fb.next_submit = [("opg", "reject")]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        self.fb.position = 0                              # the broker: stop filled; the listing lags (no activity yet)
        for s_ in (0, 6, 12, 18):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
            self.safe()
        self.assertEqual(self.eng.state["nights"]["20261029"].get("left_account", 0), 0, "nothing inferred")
        _lag_fill(stop, stop.qty, 280.0, self.clock, self.fb)      # the listing catches up
        for m in (31, 35):
            self.at(2026, 10, 30, 9, m)
            self.settle()
        self._book_and_next(exit_px=280.0)                # −$141 booked, not hidden

    def test_1b_cancel_refused_while_the_listing_lags(self):
        self.enter_and_fill()
        self.fb.next_submit = [("opg", "reject")]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        real_cancel = self.fb.cancel
        self.fb.cancel = lambda oid: (self.fb.cancels.append(oid), False)[1]
        self.fb.position = 0                              # the stop filled at the broker; the listing still says "new"
        n = len(self.fb.submits)
        for s_ in (0, 6, 12, 18, 24):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
            self.assertEqual([x for x in self.fb.submits[n:] if x["side"] == "sell"], [], "no new sell while flat")
        _lag_fill(stop, stop.qty, 280.0, self.clock, self.fb)
        self.fb.cancel = real_cancel
        for m in (31, 35):
            self.at(2026, 10, 30, 9, m)
            self.settle()
        self._book_and_next(exit_px=280.0)

    def test_2_stale_low_position_after_the_entry_fill(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        _lag_fill(cls, cls.qty, 294.1, self.clock, self.fb)       # listing shows the fill; the position still reads 0
        for s_ in (0, 6, 12, 18):
            self.at(2026, 10, 29, 16, 0, s_)
            self.settle()
            self.safe()
        self.fb.position = cls.qty                        # the shares appear
        self.at(2026, 10, 29, 16, 1)
        self.settle()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10], "protected in one poll")
        self.assertFalse(any("not trend_night's" in p for p in self.pages), "own shares are never 'foreign'")
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10], "overnight")
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 10)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 10, 295.0)
        self.step(n=2)
        self._book_and_next(entry=294.1, exit_px=295.0)

    def test_activity_resolves_a_lagging_listing_at_once(self):
        # rule 2: an own fill in the activities counts even while the listing still shows the order unfilled
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 10, 294.10)
        cls.filled_qty, cls.status, cls.filled_avg_price = 0, "new", None   # the listing lags; the activity exists
        self.settle()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"]
        self.assertEqual([o.qty for o in stop], [10])
        self.assertAlmostEqual(stop[0].stop_price, expected_stop(294.10, self.eng.state["nights"]["20261029"]["q"]),
                               places=6)

    def test_true_manual_sale_is_recorded_from_its_activity_and_booked(self):
        self.enter_and_fill()
        self.at(2026, 10, 29, 20, 0)
        self.fb.manual_sell(4, 289.0)                     # the owner sells 4 by hand overnight
        self.settle()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [6])
        self.assertEqual(self.eng.reconcile().derived["nights"]["20261029"]["departed"], 4)   # derived, not stored
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 6)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 6, 295.0)
        self.step(n=2)
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["foreign_sold_qty"], rec["never_sold_qty"]), (6, 4, 0))
        self.assertAlmostEqual(rec["pnl"], 6 * (295.0 - 294.10), places=6)
        self.assertAlmostEqual(rec["foreign_unbooked_pnl_usd"], round(4 * (289.0 - 294.10), 2))
        self.assertEqual([x["price"] for x in rec["foreign_fills"]], [289.0])
        self.at(2026, 10, 30, 9, 36)
        self.settle()
        audit = [a for a in self.ev("trend_night_audit") if a["check"] == "open"][-1]
        self.assertFalse(audit["passed"], "the unbooked foreign sale fails the result check")
        self.assertEqual(audit["pnl"]["20261029"]["foreign_fills"][0]["price"], 289.0)
        self.next_night_enters()

    def test_a_sell_never_makes_a_night_negative(self):
        self.enter_and_fill()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        self.at(2026, 10, 29, 16, 1)
        self.eng._now = self.clock.now
        _lag_fill(stop, 10, 282.0, self.clock, self.fb)   # the stop fills 10 …
        self.fb._n += 1                                   # … and a bad row reports a further own sell of 5
        self.fb.fill_log.append({"id": f"act-{self.fb._n}", "order_id": stop.id, "symbol": "QQQM", "side": "sell",
                                 "qty": "5", "price": "282.0", "transaction_time": self.clock.now.astimezone(so.UTC).isoformat()})
        r = self.eng.reconcile()
        self.assertEqual(r.own, 0, "clamped at 0, never −10")
        self.assertTrue(any("data inconsistency" in p for p in self.pages))


class Round9ColdEight(TwoSourceBase):
    """Cold review round 9 — the reviewer's cold8 probes (scratchpad/cold8/test_cold8.py): F comes ONLY from fill
    activities, each classified by its order at the source; nothing about departures is persisted."""

    def _exit_and_book(self, exit_day=date(2026, 10, 30), price=296.0):
        for (h, m, s_) in ((9, 20, 0), (9, 30, 0), (9, 30, 10), (9, 31, 0), (9, 40, 0)):
            self.at(exit_day.year, exit_day.month, exit_day.day, h, m, s_)
            self.settle()
            self.safe()
            for x in self.fb.live("sell"):
                if x.order_type != "stop":
                    self.fb.fill(x, x.qty - x.filled_qty, price)
        self.step(n=2)
        self.assertEqual(self.fb.position, 0, "sold at the next open")

    def test_C3_unknown_own_market_sell_fill_is_own_not_foreign(self):
        self.enter_and_fill()
        self.fb.next_submit = [("opg", "reject")]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.fb.next_submit = [("day", "unknown_accept")]
        self.fb.lookup_unknown = True
        self.fb.hidden.add(cid("mkt"))
        self.at(2026, 10, 30, 9, 30)
        self.settle()
        mk = self.fb.by_coid(cid("mkt"))
        self.fb.fill(mk, 4, 295.0)                        # 4 fill under an order the engine has never seen listed
        for s_ in (6, 12):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
            self.safe()
        r = self.eng.reconcile()
        self.assertEqual((r.own, r.derived["depart"]), (6, 0), "the activity is classified OWN by its order")
        self.fb.hidden.clear()
        self.fb.lookup_unknown = False
        for m in (31, 33, 35, 40):
            self.at(2026, 10, 30, 9, m)
            self.settle()
            self.safe()
            for x in self.fb.live("sell"):
                if x.order_type != "stop" and x.filled_qty == 0 and x is not mk:
                    self.fb.fill(x, x.qty, 296.0)
        self.step(n=2)
        self.assertEqual(self.fb.position, 0)
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["foreign_sold_qty"]), (10, 0))
        self.next_night_enters()

    def test_A_manual_round_trip_is_never_charged_to_the_strategy(self):
        self.enter_and_fill()
        self.at(2026, 10, 29, 17, 0)
        self.fb.manual_buy(5, 295.0)
        self.settle()
        self.step(n=2)
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10], "own 10 only")
        self.at(2026, 10, 29, 17, 5)
        self.fb.manual_sell(5, 296.0)                     # the owner sells the SAME 5
        self.settle()
        self.step()
        self.assertEqual(self.eng.reconcile().own, 10, "a manual round trip never touches own shares")
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10])
        self._exit_and_book(price=297.0)
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["foreign_sold_qty"]), (10, 0))
        self.next_night_enters()

    def test_D_duplicate_activity_rows_count_once(self):
        self.enter_and_fill()
        self.fb.next_submit = [("opg", "reject")]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.settle()
        mk = [o for o in self.fb.live("sell") if o.time_in_force == "day"][0]
        self.fb.fill(mk, 4, 295.0)
        self.fb.fill_log.append(dict(self.fb.fill_log[-1]))   # the same activity twice (page overlap)
        self.at(2026, 10, 30, 9, 30, 6)
        self.settle()
        self.safe()
        self.assertEqual(self.eng.reconcile().own, 6)

    def test_3_own_entry_fill_only_in_activities_with_the_order_missing_from_the_listing(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.fb.hidden.add(CLS)                           # the listing omits the order entirely …
        self.fb.lookup_unknown = True                     # … and client-id resolution cannot find it either
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 10, 294.10)
        self.settle()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"]
        self.assertEqual([o.qty for o in stop], [10], "protected from the activity alone")
        self.assertAlmostEqual(stop[0].stop_price, expected_stop(294.10, self.eng.state["nights"]["20261029"]["q"]),
                               places=6)

    def test_failed_order_lookup_leaves_activities_unclassified_and_holds_entry(self):
        self.fb.manual_buy(5, 295.0)                      # a foreign activity whose order cannot be looked up
        self.fb.manual_sell(5, 296.0)
        self.fb.order_lookup_ok = False
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.assertFalse([o for o in self.fb.orders.values() if o.side == "buy"], "entry waits while unclassified")
        self.assertTrue(self.ev("activities_unclassified"))
        self.fb.order_lookup_ok = True
        self.at(2026, 10, 29, 15, 41)
        self.settle()
        self.assertEqual(self.fb.by_coid(CLS).qty, 10, "classified foreign → flat → entry")

    def test_activity_fetch_failure_keeps_the_last_good_F(self):
        self.enter_and_fill()
        self.fb.fills_ok = False
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10])
        self.fb.fills_ok = True

    def test_activities_page_with_page_token_until_exhausted(self):
        rows = [{"id": f"a{i:05d}", "symbol": "QQQM", "side": "buy", "qty": "1", "price": "1", "order_id": "x",
                 "transaction_time": f"2026-10-29T20:00:{i % 60:02d}Z"} for i in range(so._FILL_PAGE_SIZE * 3 + 7)]
        calls = []

        class Client:
            def get(self, path, params):
                calls.append(dict(params))
                start = 0 if "page_token" not in params else next(i for i, r in enumerate(rows)
                                                                  if r["id"] == params["page_token"]) + 1
                return rows[start:start + so._FILL_PAGE_SIZE]
        got = so.page_activities(Client(), so.datetime(2026, 10, 29, tzinfo=so.ET))
        self.assertEqual(len(got), len(rows))
        self.assertEqual(len(calls), 4)
        self.assertTrue(all("page_token" in c for c in calls[1:]))


class Round10ListingLead(TwoSourceBase):
    """The cold7 probes exactly as written: the order LISTING reports a fill that no activity has reported yet.
    Listing orders are trend_night's by definition, so per order the own qty is max(listing, activities)."""

    def test_cold7_1_listing_only_stop_fill_books_the_true_loss(self):
        self.enter_and_fill()
        self.fb.next_submit = [("opg", "reject")]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        self.fb.position = 0                              # the broker: stop filled; no listing, no activity yet
        for s_ in (0, 6, 12, 18):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
            self.safe()
        _lag_fill(stop, stop.qty, 280.0, self.clock)      # the LISTING catches up; still no activity
        for m in (31, 35, 45):
            self.at(2026, 10, 30, 9, m)
            self.settle()
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["foreign_sold_qty"]), (10, 0))
        self.assertAlmostEqual(rec["pnl"], 10 * (280.0 - 294.10), places=6)
        # the stop's activity arrives late, stamped with its real fill time: before the checkpoint, so never an
        # "own sell with no buy" inconsistency
        self.fb._n += 1
        self.fb.fill_log.append({"id": f"act-{self.fb._n}", "order_id": stop.id, "symbol": "QQQM", "side": "sell",
                                 "qty": "10", "price": "280.0",
                                 "transaction_time": stop.filled_at.astimezone(so.UTC).isoformat()})
        self.at(2026, 10, 30, 9, 50)
        self.settle()
        self.assertFalse(any("data inconsistency" in p for p in self.pages))
        self.next_night_enters()

    def test_cold7_2_listing_only_entry_fill_is_protected_and_sold(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        _lag_fill(cls, cls.qty, 294.1, self.clock)        # the listing shows the fill; no activity, position still 0
        for s_ in (0, 6, 12, 18):
            self.at(2026, 10, 29, 16, 0, s_)
            self.settle()
            self.safe()
        self.fb.position = cls.qty                        # the shares appear at the broker
        for m in (1, 2, 5):
            self.at(2026, 10, 29, 16, m)
            self.settle()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10], "protected")
        self.assertFalse(any("not trend_night's" in p for p in self.pages))
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10], "overnight")
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 10)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 10, 295.0)
        self.step(n=2)
        self.assertEqual(self.fb.position, 0)
        rec = self.eng.state["closed"][0]
        self.assertEqual((rec["sold_qty"], rec["foreign_sold_qty"]), (10, 0))
        self.assertAlmostEqual(rec["pnl"], 10 * (295.0 - 294.1), places=6)
        self.next_night_enters()

    def test_listing_and_activity_for_one_order_never_double_count(self):
        self.enter_and_fill()                             # the cls fill is in BOTH the listing and the activities
        r = self.eng.reconcile()
        self.assertEqual(r.own, 10)

    def test_unresolved_order_lookup_pages_instead_of_blocking_silently(self):
        self.fb.manual_buy(5, 295.0)
        self.fb.manual_sell(5, 296.0)
        self.fb.order_lookup_ok = False
        for s_ in range(0, 5 * (so._UNRESOLVED_POLL_ATTEMPTS + 2), 5):
            self.at(2026, 10, 29, 15, 40, s_)
            self.settle()
        self.assertTrue(any("cannot be classified" in p for p in self.pages))


class Round10ActivitiesDown(TwoSourceBase):
    """The reviewer's cold9 probes (scratchpad/cold9/test_cold9.py): with fill activities unreadable, no new or larger
    sell is submitted and Q is also capped by the listing's own net."""

    def _to_opg(self):
        self.enter_and_fill()
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.settle()
        return self.fb.by_coid(OPG1)

    def test_A_activities_down_and_a_lagging_position_never_oversell(self):
        opg = self._to_opg()
        self.at(2026, 10, 30, 9, 30, 0)
        self.fb.fills_ok = False
        self.fb.fill(opg, opg.qty, 296.0)                 # true position now 0
        self.fb.position = 10                             # the position endpoint still shows 10
        n = len(self.fb.submits)
        for s_ in (2, 7, 12):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
            self.safe(true_position=0)
        self.assertEqual([x for x in self.fb.submits[n:] if x["side"] == "sell"], [], "no sell at all")
        self.assertTrue(any("activities UNREADABLE" in p for p in self.pages))
        self.fb.position = 0                              # the true position; activities back
        self.fb.fills_ok = True
        self.step(n=2)
        self.assertEqual(self.fb.position, 0, "never short")
        self.assertEqual(self.eng.state["closed"][0]["sold_qty"], 10)
        self.next_night_enters()

    def test_B_activities_down_never_sells_the_owners_shares(self):
        self.enter_and_fill()
        self.at(2026, 10, 29, 17, 0)
        self.fb.manual_buy(5, 295.0)                      # the owner's 5 beside trend_night's 10
        self.settle()
        self.at(2026, 10, 29, 17, 1)
        self.settle()
        opg = self._exit_opg()
        self.at(2026, 10, 30, 9, 30, 0)
        self.fb.fills_ok = False
        self.fb.fill(opg, opg.qty, 296.0)                 # the broker now holds only the owner's 5
        n = len(self.fb.submits)
        for s_ in (2, 7, 12):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
        self.assertEqual([x for x in self.fb.submits[n:] if x["side"] == "sell"], [], "the owner's 5 are untouched")
        self.assertEqual(self.fb.position, 5)
        self.assertTrue(any("activities UNREADABLE" in p for p in self.pages))
        self.assertFalse([o for o in self.fb.orders.values() if o.side == "buy" and o.client_order_id != CLS],
                         "no entry while activities are down")

    def _exit_opg(self):
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.settle()
        return self.fb.by_coid(OPG1)

class Round10ReplaceKeepsAStop(TwoSourceBase):
    def test_every_resize_keeps_a_live_stop_at_every_step(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        for part, px in ((3, 294.0), (3, 295.0), (4, 296.0)):
            self.fb.fill(cls, part, px)
            for _ in range(3):                             # each pass, one at a time
                self.eng.tick()
                live = [o for o in self.fb.live("sell") if o.order_type == "stop"]
                self.assertEqual(len(live), 1, "a live stop at every step")
                self.safe()
        self.assertEqual([o.qty for o in self.fb.live("sell") if o.order_type == "stop"], [10])
        self.assertEqual(self.fb.cancels, [], "no cancel-then-place gap")
        self.assertEqual(len(self.fb.replaces), 2)


class Round11DegradedProtects(TwoSourceBase):
    """The reviewer's cold10 probes (scratchpad/cold10/test_cold10.py): degraded mode must fail toward PROTECTED
    shares — Q = max(0, min(R, the listing's own net)) — while never overselling."""

    def stops(self):
        return [o.qty - o.filled_qty for o in self.fb.live("sell") if o.order_type == "stop"]

    def test_P1_entry_fills_while_activities_are_down(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 15, 45)
        self.fb.fills_ok = False
        self.settle()
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, cls.qty, 294.10)
        self.settle()
        self.assertEqual(self.stops(), [10], "protected within one poll")
        self.assertTrue(any("activities UNREADABLE" in p for p in self.pages))
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual(self.stops(), [10], "overnight")
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 10)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 10, 295.0)
        self.step(n=2)
        self.safe()
        self.assertEqual(self.fb.position, 0, "sold at the next open while degraded")
        self.fb.fills_ok = True
        self.at(2026, 10, 30, 10, 0)
        self.settle()
        self.step(n=2)
        self.assertEqual(self.eng.state["closed"][0]["sold_qty"], 10, "booked once activities are back")

    def test_P2_state_lost_while_activities_are_down_keeps_the_stop(self):
        self.enter_and_fill()
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        self.state_path.unlink()
        self.fb.fills_ok = False
        self.eng = self.engine()
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual(self.fb.cancels, [], "an unknown F is never read as 0")
        self.assertEqual(stop.status, "new")
        self.assertEqual(self.stops(), [10])

    def test_P3_refused_replace_while_degraded_is_re_placed(self):
        self.enter_and_fill()
        self.fb.fills_ok = False
        self.fb.manual_sell(4, 290.0)                    # broker 6, the listing still says 10 → Q 6
        self.fb.next_replace = ["reject"]
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.step(n=3)
        self.assertEqual(self.stops(), [6], "cancel-then-place after the refused replace")
        self.at(2026, 10, 29, 21, 0)
        self.settle()
        self.assertEqual(self.stops(), [6], "still protected an hour later")
        self.safe()

    def test_P4_partial_opg_while_degraded_sells_the_rest_the_same_session(self):
        self.enter_and_fill()
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.at(2026, 10, 30, 9, 30, 0)
        self.fb.fills_ok = False
        self.fb.fill(opg, 6, 296.0)
        opg.status = "canceled"                          # the auction remainder is cancelled
        for s_ in (5, 20, 40):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
            self.safe()
            for x in self.fb.live("sell"):
                if x.time_in_force == "day":
                    self.fb.fill(x, x.qty - x.filled_qty, 296.5)
        self.assertEqual(self.fb.position, 0, "the remainder is sold the same session, not held another night")
        self.assertEqual([s["qty"] for s in self.fb.submits if s["tif"] == "day"], [4])

    def test_P5_partial_stop_fill_resizes_by_replace(self):
        self.enter_and_fill()
        self.at(2026, 10, 30, 9, 0)
        stop = [o for o in self.fb.live("sell") if o.order_type == "stop"][0]
        self.fb.fill(stop, 3, 282.0)
        self.settle()
        self.safe()
        self.assertEqual(self.stops(), [7])

    def test_degraded_never_sells_more_than_the_listing_or_the_broker(self):
        self.enter_and_fill()
        self.fb.fills_ok = False
        self.fb.manual_sell(4, 290.0)                    # broker 6
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual(self.stops(), [6])
        self.assertTrue(any("activities UNREADABLE" in p for p in self.pages))
        self.fb.manual_buy(5, 291.0)                     # broker 11 > the listing's 10
        self.at(2026, 10, 29, 20, 1)
        self.settle()
        self.assertEqual(self.stops(), [10], "capped by the listing's own net")
        ev = self.ev("activities_degraded")[-1]                   # one fixed-key page; the evidence is in the event
        self.assertEqual((ev["broker_qty"], ev["protected"]), (11, 10))


class Round12StaleByData(TwoSourceBase):
    """The reviewer's cold11 probes (scratchpad/cold11/test_cold11.py)."""

    def stops(self):
        return [o.qty - o.filled_qty for o in self.fb.live("sell") if o.order_type == "stop"]

    def test_T1_stale_F_read_just_before_the_fill_is_not_trusted(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0, 1)
        self.fb.position = 10                             # the position shows the fill; listing + activities lag
        self.settle()
        cls.filled_qty, cls.filled_avg_price, cls.status = 10, 294.10, "filled"
        cls.filled_at = (et(2026, 10, 29, 16, 0, 0) + timedelta(milliseconds=200)).astimezone(so.UTC)  # before the read
        self.fb.fills_ok = False
        for m in (0, 1, 5, 30):
            self.at(2026, 10, 29, 16, 1 + m)
            self.settle()
        self.assertEqual(self.stops(), [10], "protected overnight")
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 10)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 10, 295.0)
        self.step(n=3)
        self.assertEqual(self.fb.position, 0, "sold at the open")

    def test_T2_state_loss_never_counts_booked_nights_departed_shares(self):
        self.enter_and_fill()
        self.at(2026, 10, 29, 20, 0)
        self.fb.manual_sell(10, 290.0)                    # the owner sells night 1's 10 by hand
        self.step(n=3)
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 40)
        self.settle()
        self.step(n=3)
        self.assertEqual(self.eng.state["closed"][0]["foreign_sold_qty"], 10)
        self.assertTrue(self.eng._ledger_path().exists(), "the booked night is in the ledger")
        self.next_night_enters()
        c2 = self.fb.by_coid(cid("ecls", 1, self.fb.cal[date(2026, 10, 30)][1]))
        self.at(2026, 10, 30, 16, 0)
        self.fb.fill(c2, c2.qty, 295.0)
        self.settle()
        self.at(2026, 10, 30, 18, 0)
        self.fb.manual_buy(5, 296.0)                      # the owner's 5 beside night 2's 10
        self.step(n=3)
        self.state_path.unlink()                          # state lost AND activities down
        self.fb.fills_ok = False
        self.eng = self.engine()
        self.at(2026, 10, 30, 20, 0)
        self.settle()
        self.assertEqual(self.stops(), [10], "only trend_night's 10")
        self.at(2026, 10, 30, 20, 0, 10)
        self.settle()
        self.assertTrue(any("not trend_night's" in p for p in self.pages), "the owner's 5 are paged")
        self.at(2026, 11, 2, 9, 20)
        self.settle()
        opg = [o for o in self.fb.live("sell") if o.time_in_force == "opg"][0]
        self.assertEqual(opg.qty, 10)
        self.at(2026, 11, 2, 9, 30)
        self.fb.fill(opg, 10, 296.5)
        self.step(n=3)
        self.assertEqual(self.fb.position, 5, "the owner's 5 untouched")

    def test_unreadable_ledger_caps_at_the_newest_night_and_pages(self):
        self.enter_and_fill()
        self.eng._ledger_path().mkdir()                   # the file itself cannot be read (a torn LINE is skipped)
        self.fb.fills_ok = False
        self.at(2026, 10, 29, 20, 0)
        self.settle()
        self.assertEqual(self.stops(), [10])
        self.assertTrue(any("ledger is unreadable" in p for p in self.pages))

    def test_partial_fill_with_null_filled_at_counts_while_degraded(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.fb.fills_ok = False
        self.at(2026, 10, 29, 16, 0)
        cls.filled_qty, cls.filled_avg_price, cls.status, cls.filled_at = 6, 294.10, "partially_filled", None
        self.fb.position = 6
        self.settle()
        self.assertEqual(self.stops(), [6], "a partial with no filled_at still counts")


class Round13DeparturesAndTornLedger(TwoSourceBase):
    """The reviewer's cold12 probes (scratchpad/cold12/test_cold12.py)."""

    def test_P4_owner_sell_then_rebuy_is_never_sold_while_degraded(self):
        self.enter_and_fill()                             # own 10 (night 10/29)
        self.at(2026, 10, 29, 17, 0)
        for o in self.fb.live("sell"):                    # the owner cancels the stop to sell by hand
            o.status = "canceled"
        self.fb.manual_sell(3, 295.0)                     # a departure of 3 (activities up)
        self.settle()
        self.at(2026, 10, 29, 17, 1)
        self.settle()
        self.fb.manual_buy(3, 295.5)                      # the owner's own 3 back (activities up: foreign)
        self.at(2026, 10, 29, 17, 2)
        self.settle()
        self.at(2026, 10, 29, 17, 3)
        self.settle()
        self.assertTrue(any("not trend_night" in p for p in self.pages), "the owner's 3 are paged before the open")
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.settle()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 7, "only trend_night's 7")
        self.at(2026, 10, 30, 9, 30, 0)
        self.fb.fills_ok = False                          # activities go down as the opg fills
        self.fb.fill(opg, opg.qty, 296.0)
        n = len(self.fb.submits)
        for s_ in (2, 7, 12, 20, 40, 59):
            self.at(2026, 10, 30, 9, 30, s_)
            self.settle()
        for m in (31, 32, 33):
            self.at(2026, 10, 30, 9, m)
            self.settle()
        self.assertEqual([x for x in self.fb.submits[n:] if x["side"] == "sell"], [], "the owner's 3 never sold")
        self.assertEqual(self.fb.position, 3)
        self.assertEqual(self.eng.state["act_last"]["departed"], {"20261029": 3})
        self.assertFalse(any("unprotected" in p for p in self.pages), "the owner's 3 are not ours to protect")

    def test_P1_torn_ledger_line_is_skipped_and_later_lines_still_read(self):
        self.enter_and_fill()
        lp = self.eng._ledger_path()
        lp.write_text('{"night": "2026')                 # a torn append (crash / disk full mid-write)
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(self.fb.by_coid(OPG1), 10, 296.0)
        self.step(n=3)
        booked, ok = self.eng._booked_ledger()
        self.assertTrue(ok, "one torn line never makes the ledger unreadable")
        self.assertEqual(len(booked), 1, "the line appended after the torn one still reads")
        self.assertTrue(lp.read_text().startswith('{"night": "2026\n'), "the append started on a fresh line")
        self.assertEqual(sum("undecodable" in p for p in self.pages), 1, "paged once")
        self.eng._booked_ledger()
        self.assertEqual(sum("undecodable" in p for p in self.pages), 1, "not re-paged for the same line")


class Round5FreshCapAndEntryRace(Base):
    def test_every_sell_is_capped_by_a_fresh_broker_read(self):
        self.enter_and_fill()
        self.at(2026, 10, 30, 9, 20)
        with mock.patch.object(so.TrendNight, "_fresh_position", lambda self: 4):
            self.settle()
        self.assertEqual(self.fb.by_coid(OPG1).qty, 4, "opg capped at the fresh read")
        self.setUp()
        self.enter_and_fill()
        n = len(self.fb.submits)
        self.at(2026, 10, 30, 9, 20)
        with mock.patch.object(so.TrendNight, "_fresh_position", lambda self: 0):
            self.settle()
        self.assertEqual([s for s in self.fb.submits[n:] if s["side"] == "sell"], [], "fresh read 0 → no sell")
        self.assertTrue(self.ev("sell_skipped"))

    def test_entry_fill_visible_in_position_before_the_listing_creates_no_sell(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        n = len(self.fb.submits)
        self.fb.position = 10                             # the position shows the fill; the listing does not yet
        for s in (0, 10, 20):
            self.at(2026, 10, 29, 15, 41, s)
            self.settle()
        # two-source minimum: own orders account for 0 sh until the listing shows the fill → no sell at all
        self.assertEqual([x for x in self.fb.submits[n:] if x["side"] == "sell"], [])
        self.fb.position = 0
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 10, 294.10)                     # the listing catches up (position back to 10)
        self.settle()
        live = self.fb.live("sell")
        self.assertEqual([(o.qty, o.order_type) for o in live], [(10, "stop")])
        self.assertTrue(all(s_["tif"] == "gtc" for s_ in self.fb.submits if s_["side"] == "sell"), "no exit sells")


if __name__ == "__main__":
    unittest.main()
