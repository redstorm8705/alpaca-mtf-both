# ruff: noqa: E501
"""SWING-OVERNIGHT-1030 (design v12) — Swing tier `trend_night` (QQQM overnight on trend nights).

Every broker call is a fake (FakeBroker); the real adapter is proven to refuse the broker in a test process. The
stop-coverage invariant (live TN sell remaining == broker-held QQQM, stop at avg entry × (1 − q) floored to the
cent) is asserted after every simulated broker event."""
import json
import math
import os
import statistics
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

import config
from execution import swing_overnight as so

ET = so.ET
UTC = timezone.utc
TERMINAL = {"filled", "canceled", "rejected", "expired", "replaced"}
Q = 0.04
ACCT = "PA-TEST-1"


def et(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=ET)


D1_CLOSE = et(2026, 10, 29, 16, 0)


def cid(step, n=1, close=D1_CLOSE):
    return so.build_coid(close, step, n)


CLS, MKT, OPG1 = cid("ecls"), cid("emkt"), cid("opg")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeBroker:
    """An in-memory Alpaca for one symbol. Orders are SimpleNamespaces shaped like alpaca-py Order objects."""

    def __init__(self, clock, sym="QQQM"):
        self.clock_ = clock
        self.sym = sym
        self.orders = {}
        self.position = 0
        self.extra_positions = []
        self.acct = SimpleNamespace(account_number=ACCT, equity=2555.0, buying_power=10000.0,
                                    regt_buying_power=5110.0, trading_blocked=False, account_blocked=False)
        self.cal = {}
        self.calendar_ok = True
        self.clock_ok = True
        self.clock_value = None
        self.list_ok = True
        self.position_ok = True
        self.next_submit = []
        self.next_replace = []
        self.cancel_mode = "immediate"
        self.lookup_unknown = False
        self.hidden = set()
        self.fill_log = []
        self.submits = []
        self.replaces = []
        self.cancels = []
        self._n = 0

    # ── adapter interface ──
    def account(self):
        return self.acct

    def positions(self):
        out = list(self.extra_positions)
        if self.position:
            out.append(SimpleNamespace(symbol=self.sym, qty=self.position, market_value=self.position * 294.0,
                                       asset_class="us_equity"))
        return out

    def position_qty(self, symbol):
        if not self.position_ok:
            raise RuntimeError("position endpoint down")
        return self.position

    def list_orders(self, symbol, after):
        if not self.list_ok:
            raise RuntimeError("orders endpoint down")
        return [o for o in self.orders.values() if o.symbol == symbol and o.client_order_id not in self.hidden
                and (after is None or o.submitted_at > after)]

    def get_by_coid(self, coid):
        if self.lookup_unknown:
            return "unknown", None
        for o in self.orders.values():
            if o.client_order_id == coid:
                return "found", o
        return "absent", None

    def _new_order(self, coid, side, qty, tif, otype, stop_price=None):
        self._n += 1
        o = SimpleNamespace(id=f"oid-{self._n}", client_order_id=coid, symbol=self.sym, side=side, order_type=otype,
                            time_in_force=tif, status="new", qty=qty, filled_qty=0, filled_avg_price=None,
                            stop_price=float(stop_price) if stop_price is not None else None,
                            submitted_at=self.clock_.now.astimezone(UTC), filled_at=None)
        self.orders[o.id] = o
        return o

    def submit(self, symbol, coid, side, qty, tif, stop_price=None):
        self.submits.append({"coid": coid, "side": side, "qty": qty, "tif": tif, "stop_price": stop_price})
        mode = "ok"
        if self.next_submit:
            head = self.next_submit[0]
            want_tif, m = head if isinstance(head, tuple) else (None, head)
            if want_tif is None or want_tif == tif:
                mode = m
                self.next_submit.pop(0)
        if mode == "reject":
            return "rejected", '{"code":42210000,"message":"order rejected"}'
        if mode == "held_for_orders":
            return "rejected", '{"code":40310000,"message":"insufficient qty available for order"}'
        if any(o.client_order_id == coid for o in self.orders.values()):
            return "unknown", "40910000 duplicate client order id"
        o = self._new_order(coid, side, qty, tif, "stop" if stop_price is not None else "market", stop_price)
        if mode == "unknown_accept":
            return "unknown", "read timeout"
        if mode == "unknown_absent":
            del self.orders[o.id]
            return "unknown", "read timeout"
        return "ack", o

    def replace(self, symbol, order_id, qty, stop_price, coid):
        self.replaces.append({"order_id": order_id, "qty": qty, "stop_price": stop_price, "coid": coid})
        mode = self.next_replace.pop(0) if self.next_replace else "ok"
        old = self.orders[order_id]
        if mode == "reject" or old.status in TERMINAL:
            return "rejected", '{"code":42210000,"message":"order is not replaceable"}'
        old.status = "replaced"
        new = self._new_order(coid, old.side, qty, old.time_in_force, "stop", stop_price)
        return "ack", new

    def cancel(self, order_id):
        self.cancels.append(order_id)
        o = self.orders[order_id]
        if o.status in TERMINAL:
            return True
        o.status = "pending_cancel" if self.cancel_mode == "pending" else "canceled"
        return True

    def clock(self):
        if not self.clock_ok:
            raise RuntimeError("clock down")
        return self.clock_value

    def calendar(self, start, end):
        if not self.calendar_ok:
            raise RuntimeError("calendar down")
        return [(d, o, c) for d, (o, c) in sorted(self.cal.items()) if start <= d <= end]

    def fills(self, after):
        """Account FILL activities (Alpaca /v2/account/activities/FILL shape), mocked — never the broker."""
        if not getattr(self, "fills_ok", True):
            raise RuntimeError("activities endpoint down")

        def after_ok(a):
            t = so._aware(a.get("transaction_time"))
            return t is None or t > after                 # like Alpaca: only fills after `after` (hand-made rows pass)
        return [a for a in self.fill_log if after_ok(a)]

    def get_order(self, order_id):
        """Order lookup by id (classifies an activity at its source); `order_lookup_ok = False` makes it fail."""
        if not getattr(self, "order_lookup_ok", True):
            return "unknown", None
        o = self.orders.get(order_id) or getattr(self, "manual_orders", {}).get(order_id)
        return ("found", o) if o is not None else ("absent", None)

    def manual_buy(self, qty, price):
        """The owner buys QQQM by hand: a BUY fill activity under an order that is NOT trend_night's."""
        self._n += 1
        oid = f"manual-{self._n}"
        self.manual_orders = getattr(self, "manual_orders", {})
        self.manual_orders[oid] = SimpleNamespace(id=oid, client_order_id=f"web-{self._n}", symbol=self.sym)
        self.position += qty
        self.fill_log.append({"id": f"act-{self._n}", "order_id": oid, "symbol": self.sym,
                              "side": "buy", "qty": str(qty), "price": str(price),
                              "transaction_time": self.clock_.now.astimezone(UTC).isoformat()})

    def manual_sell(self, qty, price):
        """The owner sells QQQM by hand: a SELL fill activity under an order that is NOT trend_night's."""
        self._n += 1
        self.position -= qty
        self.manual_orders = getattr(self, "manual_orders", {})
        self.manual_orders[f"manual-{self._n}"] = SimpleNamespace(id=f"manual-{self._n}", client_order_id=f"web-{self._n}",
                                                                 symbol=self.sym)
        self.fill_log.append({"id": f"act-{self._n}", "order_id": f"manual-{self._n}", "symbol": self.sym,
                              "side": "sell", "qty": str(qty), "price": str(price),
                              "transaction_time": self.clock_.now.astimezone(UTC).isoformat()})

    # ── simulation helpers (broker events) ──
    def by_coid(self, coid):
        return next(o for o in self.orders.values() if o.client_order_id == coid)

    def live(self, side=None):
        return [o for o in self.orders.values() if o.status not in TERMINAL and (side is None or o.side == side)]

    def fill(self, o, qty, price):
        prev = o.filled_qty * (o.filled_avg_price or 0.0)
        o.filled_qty += qty
        o.filled_avg_price = (prev + qty * price) / o.filled_qty
        o.status = "filled" if o.filled_qty >= o.qty else "partially_filled"
        o.filled_at = self.clock_.now.astimezone(UTC)
        self.position += qty if o.side == "buy" else -qty
        self._n += 1
        self.fill_log.append({"id": f"act-{self._n}", "order_id": o.id, "symbol": self.sym, "side": o.side,
                              "qty": str(qty), "price": str(price),
                              "transaction_time": self.clock_.now.astimezone(UTC).isoformat()})


def _hist(n=1200, ret=-Q, end=date(2026, 10, 29)):
    """Daily bars whose every overnight return is `ret` (so q = -ret exactly)."""
    idx = pd.bdate_range(end=pd.Timestamp(end) - pd.Timedelta(days=1), periods=n)
    close = np.full(n, 100.0)
    return pd.DataFrame({"open": close * (1 + ret), "high": close, "low": close * (1 + ret), "close": close,
                         "volume": 1e6}, index=idx)


class FakeData:
    def __init__(self):
        self.spy_last = date(2026, 10, 28)
        self.spy_up = True
        self.spy_empty = False
        self.price = 294.0
        self.hist = {}
        self.spread_list = [0.02, 0.02, 0.04]

    def spy_closed(self, symbol, n):
        if self.spy_empty:
            return pd.DataFrame()
        idx = pd.bdate_range(end=pd.Timestamp(self.spy_last), periods=n + 5)
        c = np.arange(1, len(idx) + 1, dtype=float)
        return pd.DataFrame({"close": c if self.spy_up else c[::-1]}, index=idx)

    def daily_history(self, symbol, before):
        return self.hist.get(symbol, _hist(end=before))

    def last_price(self, symbol):
        return self.price

    def spreads(self, symbol, start, end):
        return list(self.spread_list)


def expected_stop(avg, q=Q):
    return float((Decimal(str(avg)) * (Decimal(1) - Decimal(str(q)))).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


class Base(unittest.TestCase):
    D1 = date(2026, 10, 29)   # entry session (Thu)
    D2 = date(2026, 10, 30)   # exit session (Fri)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "tn.json"
        self.clock = Clock(et(2026, 10, 29, 15, 40))
        self.fb = FakeBroker(self.clock)
        for d in (date(2026, 10, 27), date(2026, 10, 28), self.D1, self.D2, date(2026, 11, 2)):
            self.fb.cal[d] = (datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                              datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))
        self.fd = FakeData()
        self.events, self.pages = [], []
        self.kill = (False, "")
        self.day_lots = None
        for p in (mock.patch.dict(os.environ, {"ALPACA_PAPER_ACCOUNT_NUMBER": ACCT}),
                  mock.patch.object(config, "SWING_TREND_NIGHT_ENABLED", True),
                  mock.patch("execution.overnight_capacity.overnight_k", return_value=1.9),
                  mock.patch.object(so, "day_tier_flatten_lots", side_effect=lambda: self.day_lots)):
            p.start()
            self.addCleanup(p.stop)
        self.eng = self.engine()

    def engine(self):
        return so.TrendNight(broker=self.fb, data=self.fd, state_path=self.state_path, now_fn=self.clock,
                             sleep_fn=lambda s: None, kill_fn=lambda today: self.kill,
                             page_fn=self.pages.append, log_fn=lambda ev, **kw: self.events.append((ev, kw)))

    def at(self, y, mo, d, h, mi, s=0):
        self.clock.now = et(y, mo, d, h, mi, s)

    def settle(self, eng=None):
        """The run loop without sleeping: tick until a pass takes no order action."""
        eng = eng or self.eng
        out = None
        for _ in range(30):
            out = eng.tick()
            if not out.acted:
                return out
        self.fail("engine did not settle")

    def invariant(self, q=None, eng=None):
        """q defaults to the q the engine measured for the held night (float data: 0.04 can measure as 0.0400…04,
        which floors the stop one cent lower — the conservative side)."""
        if q is None:
            nights = (eng or self.eng).state.get("nights", {})
            q = next((n["q"] for n in nights.values() if n.get("q") is not None), Q)
        held = self.fb.position
        sells = self.fb.live("sell")
        rem = sum(o.qty - o.filled_qty for o in sells)
        self.assertGreaterEqual(held, 0, "never short")
        if held == 0:
            self.assertEqual(rem, 0, "no live TN sell without shares")
            return
        self.assertEqual(rem, held, f"live TN sells {rem} must cover held {held} exactly")
        buys = [o for o in self.fb.orders.values() if o.side == "buy" and o.filled_qty > 0]
        avg = sum(o.filled_qty * o.filled_avg_price for o in buys) / sum(o.filled_qty for o in buys)
        for o in sells:
            if o.order_type == "stop":
                self.assertAlmostEqual(o.stop_price, expected_stop(avg, q), places=6)
                self.assertEqual(o.time_in_force, "gtc")

    def ev(self, name):
        return [kw for e, kw in self.events if e == name]

    def enter_and_fill(self, qty=None, price=294.10):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.invariant()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, qty or cls.qty, price)
        self.settle()
        self.invariant()
        return cls


class RefusesBroker(unittest.TestCase):
    """The real adapter can never reach Alpaca from a test process (execution.broker._get_trading_client refuses)."""

    def test_every_adapter_call_refuses(self):
        b = so.TNBroker()
        calls = {
            "account": lambda: b.account(), "positions": lambda: b.positions(),
            "position_qty": lambda: b.position_qty("QQQM"), "list_orders": lambda: b.list_orders("QQQM", None),
            "get_by_coid": lambda: b.get_by_coid(CLS),
            "submit": lambda: b.submit("QQQM", CLS, "buy", 1, "cls"),
            "submit_stop": lambda: b.submit("QQQM", cid("stop"), "sell", 1, "gtc", stop_price=Decimal("1.00")),
            "replace": lambda: b.replace("QQQM", "x", 1, Decimal("1.00"), cid("stop", 2)),
            "cancel": lambda: b.cancel("x"), "clock": lambda: b.clock(),
            "calendar": lambda: b.calendar(date(2026, 10, 29), date(2026, 10, 29)),
            "fills": lambda: b.fills(datetime(2026, 10, 29, tzinfo=UTC)),
        }
        with mock.patch("execution.broker.TradingClient", side_effect=AssertionError("a real client was built")):
            for name, fn in calls.items():
                with self.subTest(call=name):
                    with self.assertRaisesRegex(RuntimeError, "refused in a test process"):
                        fn()

    def test_broker_functions_used_refuse(self):
        from execution import broker
        for fn, args in ((broker.get_account, ()), (broker.get_open_positions, ()),
                         (broker.get_open_position, ("QQQM",)), (broker.cancel_order, ("x",)),
                         (broker.get_clock, ()), (broker.get_trading_client, ())):
            with self.subTest(fn=fn.__name__):
                with self.assertRaisesRegex(RuntimeError, "refused in a test process"):
                    fn(*args)

    def test_production_state_refused_in_tests(self):
        with self.assertRaises(RuntimeError):
            so.TrendNight(broker=object(), data=object())


class PureFunctions(unittest.TestCase):
    def test_stop_target_rounds_down_in_decimal(self):
        self.assertEqual(so.stop_target(294.10, 0.04), Decimal("282.33"))       # 282.336 → 282.33
        self.assertEqual(so.stop_target(295.2, 0.04), Decimal("283.39"))        # 283.392 → 283.39
        self.assertIsNone(so.stop_target(None, 0.04))
        self.assertIsNone(so.stop_target(294.0, 0.0))

    def test_regime(self):
        s = pd.Series(np.arange(1, 202, dtype=float), index=pd.bdate_range("2026-01-01", periods=201))
        self.assertTrue(so.regime(s, 200)["on"])
        self.assertFalse(so.regime(s[::-1].reset_index(drop=True).set_axis(s.index), 200)["on"])
        self.assertIsNone(so.regime(s.head(50), 200))

    def test_q_is_the_expanding_loss_quantile(self):
        r = so.overnight_returns(_hist(ret=-0.04))
        self.assertAlmostEqual(so.loss_quantile(r, 0.999), 0.04, places=9)
        r2 = pd.Series(np.linspace(-0.05, 0.03, 1001))
        self.assertAlmostEqual(so.loss_quantile(r2, 0.999), -float(r2.quantile(0.001)), places=12)
        self.assertIsNone(so.loss_quantile(pd.Series([], dtype=float), 0.999))

    def test_size_each_limit_can_bind(self):
        base = dict(equity=2555.0, price=294.0, q=0.04, b=0.05, k=1.9, gross_notional=0.0, gap_book_loss=0.0,
                    kill_pct=0.07, buying_power=5110.0)
        qty, info = so.size_night(**base)
        self.assertEqual((qty, info["bound_by"]), (10, "budget"))              # 127.75 / 11.76
        qty, info = so.size_night(**dict(base, gross_notional=4854.5 - 294 * 3))
        self.assertEqual((qty, info["bound_by"]), (3, "k_room"))
        qty, info = so.size_night(**dict(base, gap_book_loss=178.85 - 11.76 * 3 - 0.01))
        self.assertEqual((qty, info["bound_by"]), (3, "gap"))
        qty, info = so.size_night(**dict(base, buying_power=294.0 * 2 + 1))
        self.assertEqual((qty, info["bound_by"]), (2, "buying_power"))
        self.assertEqual(so.size_night(**dict(base, q=float("nan")))[0], 0)
        self.assertEqual(so.size_night(**dict(base, gross_notional=1e9))[0], 0)

    def test_fifo_pnl(self):
        f = [{"side": "buy", "qty": "4", "price": "294", "transaction_time": "1"},
             {"side": "buy", "qty": "6", "price": "296", "transaction_time": "2"},
             {"side": "sell", "qty": "10", "price": "295", "transaction_time": "3"}]
        self.assertAlmostEqual(so.fifo_pnl(f), 4 * 1 - 6 * 1)
        self.assertIsNone(so.fifo_pnl([{"side": "sell", "qty": "1", "price": "1"}]))


class FullPathNight(Base):
    def test_one_night_entry_stop_opg_swap_flat(self):
        # 15:40 — cls entry
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.invariant()
        cls = self.fb.by_coid(CLS)
        self.assertEqual((cls.side, cls.time_in_force, cls.qty), ("buy", "cls", 10))
        sizing = self.ev("sizing")[0]["sizing"]
        self.assertEqual(sizing["bound_by"], "budget")
        self.assertEqual(set(sizing["limits"]), {"budget", "k_room", "gap", "buying_power"})
        # 16:00 — fill → stop placed for held at avg × (1 − q)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 10, 294.10)
        self.settle()
        self.invariant()
        stops = self.fb.live("sell")
        self.assertEqual(len(stops), 1)
        self.assertEqual((stops[0].qty, stops[0].stop_price), (10, 282.33))
        # overnight minute runs: nothing changes
        self.at(2026, 10, 30, 2, 0)
        self.settle()
        self.invariant()
        self.assertEqual(len(self.fb.submits), 2)
        # 09:20 — swap the stop for the opg sell
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.invariant()
        opg = [o for o in self.fb.live("sell")]
        self.assertEqual(len(opg), 1)
        self.assertEqual((opg[0].time_in_force, opg[0].qty), ("opg", 10))
        self.assertEqual(stops[0].status, "canceled")
        # 09:30 — opg fills at the open → flat, booked
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg[0], 10, 295.00)
        self.settle()
        self.invariant()
        self.assertEqual(self.fb.position, 0)
        closed = self.eng.state["closed"]
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0]["pnl"], 9.0, places=6)
        self.assertEqual(closed[0]["path"], "cls")
        self.assertTrue(self.ev("night_summary"))
        self.assertEqual(self.eng.state["nights"]["20261029"]["status"], "closed")
        # 09:35 — result check passes, P&L recomputed from fills matches
        self.at(2026, 10, 30, 9, 35)
        self.settle()
        audit = self.ev("trend_night_audit")
        self.assertTrue(audit and audit[-1]["passed"], audit)
        self.assertEqual(self.pages, [])
        # every decision left an event; no shorts, no duplicate exits
        self.assertEqual([s["tif"] for s in self.fb.submits], ["cls", "gtc", "opg"])
        for name in ("signal", "sizing", "order_submit", "order_ack", "fill", "stop_place", "exit"):
            self.assertTrue(self.ev(name), name)

    def test_adverse_partial_entry_fills_stop_follows_cumulative_average(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 4, 294.0)
        self.settle()
        self.invariant()
        self.fb.fill(cls, 6, 296.0)            # fills arriving after the stop was placed
        self.settle()
        self.invariant()
        live = self.fb.live("sell")
        self.assertEqual((live[0].qty, live[0].stop_price), (10, expected_stop(295.2, self.eng.state["nights"]["20261029"]["q"])))
        # round 10 (design item 5): ONE replace_order_by_id — the shares are never without a stop
        self.assertEqual(len(self.fb.replaces), 1)
        self.assertEqual(len(self.fb.cancels), 0)

    def test_resized_stop_rejected_backs_off_then_new_stop(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 4, 294.0)
        self.settle()
        self.invariant()
        self.fb.next_replace = ["reject"]
        self.fb.fill(cls, 6, 296.0)
        self.settle()
        self.assertTrue(any("REFUSED" in p for p in self.pages), "a refused replace pages")
        self.clock.now += timedelta(seconds=10)
        self.settle()
        self.invariant()
        self.assertEqual(len(self.fb.live("sell")), 1)

    def test_fill_during_replace(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 3, 294.0)
        self.settle()
        orig_replace = self.fb.replace

        def replace_then_fill(*a, **kw):
            out = orig_replace(*a, **kw)
            if cls.filled_qty < cls.qty:
                self.fb.fill(cls, cls.qty - cls.filled_qty, 293.0)       # lands during the replace
            return out
        self.fb.fill(cls, 2, 295.0)
        self.fb.replace = replace_then_fill
        self.settle()
        self.invariant()


class EntryPaths(Base):
    def test_cls_rejected_fallback_market_at_close_minus_5(self):
        self.fb.next_submit = ["reject"]
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.assertEqual(self.fb.orders, {})
        self.at(2026, 10, 29, 15, 50)
        self.settle()
        self.assertEqual(len(self.fb.submits), 1)       # no fallback before close − 5
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        mkt = self.fb.by_coid(MKT)
        self.assertEqual((mkt.time_in_force, mkt.qty), ("day", 10))
        self.fb.fill(mkt, 10, 294.5)
        self.settle()
        self.invariant()
        self.assertEqual(self.eng.state["nights"]["20261029"]["path"], "fallback")

    def test_cls_expired_at_broker_also_falls_back(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.fb.by_coid(CLS).status = "expired"
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        self.assertTrue(self.fb.by_coid(MKT))

    def test_entry_unknown_blocks_second_entry_and_survives_restart(self):
        self.fb.next_submit = ["unknown_accept"]
        self.fb.hidden.add(CLS)
        self.fb.lookup_unknown = True
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.assertTrue(self.pages)
        self.at(2026, 10, 29, 15, 45)
        self.settle()
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        eng2 = self.engine()                      # restart while UNKNOWN
        self.settle(eng2)
        self.assertEqual(len(self.fb.submits), 1, "no second entry while the first is UNKNOWN")
        self.assertTrue(any("UNKNOWN" in kw.get("reason", "") for kw in self.ev("skip")))
        # resolution: the order exists → found by client id; its fill is then protected
        self.fb.lookup_unknown = False
        self.fb.hidden.clear()
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(self.fb.by_coid(CLS), 10, 294.0)
        self.settle(eng2)
        self.invariant()

    def test_cls_timed_out_and_never_placed_resolves_absent_then_fallback(self):
        self.fb.next_submit = ["unknown_absent"]
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.assertEqual(self.eng.state["nights"]["20261029"]["orders"][CLS]["state"], "unknown")
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        self.assertEqual(self.eng.state["nights"]["20261029"]["orders"][CLS]["state"], "absent")
        self.assertEqual(self.fb.by_coid(MKT).qty, 10)
        self.assertEqual(len([s for s in self.fb.submits if s["side"] == "buy"]), 2)

    def test_cls_timed_out_but_accepted_resolves_by_client_id(self):
        self.fb.next_submit = ["unknown_accept"]
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        orec = self.eng.state["nights"]["20261029"]["orders"][CLS]
        self.assertEqual(orec["state"], "ack")
        self.assertEqual(len(self.fb.submits), 1)

    def test_regime_off_missing_stale_no_entry(self):
        for setup, expect in ((lambda: setattr(self.fd, "spy_up", False), "regime off"),
                              (lambda: setattr(self.fd, "spy_empty", True), "missing"),
                              (lambda: setattr(self.fd, "spy_last", date(2026, 10, 27)), "stale")):
            with self.subTest(expect=expect):
                self.setUp()
                setup()
                self.at(2026, 10, 29, 15, 40)
                self.settle()
                self.assertEqual(self.fb.submits, [])
                self.assertTrue(any(expect in kw["reason"] for kw in self.ev("skip")), self.ev("skip"))

    def test_kill_switch_and_flag_block_entry(self):
        for setup, expect in ((lambda: setattr(self, "kill", (True, "daily kill switch / entry halt active")), "kill"),
                              (lambda: mock.patch.object(config, "SWING_TREND_NIGHT_ENABLED", False).start(), "kill flag")):
            with self.subTest(expect=expect):
                self.setUp()
                setup()
                self.at(2026, 10, 29, 15, 40)
                self.settle()
                self.assertEqual(self.fb.submits, [])
                self.assertTrue(any(expect in kw["reason"] for kw in self.ev("skip")))
        mock.patch.stopall()

    def test_paper_assertion_blocks_every_submit(self):
        with mock.patch.dict(os.environ, {"ALPACA_PAPER_ACCOUNT_NUMBER": "PA-OTHER"}):
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        self.assertEqual(self.fb.submits, [])
        self.assertTrue(any("paper assertion" in p for p in self.pages))

    def test_size_limited_by_k_room_and_gap_in_the_engine(self):
        with mock.patch("execution.overnight_capacity.overnight_k", return_value=0.5):
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        s = self.ev("sizing")[0]["sizing"]
        self.assertEqual(s["bound_by"], "k_room")
        self.assertEqual(self.fb.by_coid(CLS).qty, s["limits"]["k_room"])
        self.setUp()
        self.fb.extra_positions = [SimpleNamespace(symbol="NVDA", qty=10, market_value=1500.0, asset_class="us_equity")]
        self.fd.hist["NVDA"] = _hist(ret=-0.10)
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        s = self.ev("sizing")[0]["sizing"]
        self.assertEqual(s["bound_by"], "gap")                     # 0.07 × 2555 − 1500 × 0.10 = 28.85 → 2 sh
        self.assertEqual(s["limits"]["gap"], 2)

    def test_day_tier_lots_closing_today_do_not_use_overnight_room(self):
        # TREND-NIGHT-DAYLOTS-1103 replay of 11-02: a Day-tier AMZN short open at the entry minute (flattened before
        # the close) took the whole K room. Counted at what remains after the Day close it frees the room.
        amzn = SimpleNamespace(symbol="AMZN", qty=-6, market_value=-1479.9, asset_class="us_equity")
        self.fb.extra_positions = [amzn]
        self.fd.hist["AMZN"] = _hist(ret=-0.04)
        with mock.patch("execution.overnight_capacity.overnight_k", return_value=0.6):
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        s = self.ev("sizing")[0]["sizing"]
        self.assertEqual(s["bound_by"], "k_room")
        self.assertEqual(s["limits"]["k_room"], 0)                 # 0.6 × 2555 − 1479.9 = 53.1 < one share
        self.setUp()
        self.fb.extra_positions = [amzn]
        self.fd.hist["AMZN"] = _hist(ret=-0.04)
        self.day_lots = {"AMZN": -6}
        with mock.patch("execution.overnight_capacity.overnight_k", return_value=0.6):
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        s = self.ev("sizing")[0]["sizing"]
        self.assertEqual(s["gross_notional"], 0.0)
        self.assertEqual(s["limits"]["k_room"], 5)                 # 1533 / 294
        self.assertEqual(s["day_lot_adjustments"][0]["after_close_qty"], 0)

    def test_post_flatten_positions_rules(self):
        def P(sym, q, mv, ac="us_equity"):
            return SimpleNamespace(symbol=sym, qty=q, market_value=mv, asset_class=ac)

        v, adj = so.post_flatten_positions([P("AMZN", -6, -1500.0)], {"AMZN": -6})
        self.assertEqual(v[0].market_value, 0.0)
        v, _ = so.post_flatten_positions([P("AAPL", 10, 2500.0)], {"AAPL": 4})     # co-held: Swing 6 stays
        self.assertAlmostEqual(v[0].market_value, 1500.0)
        v, adj = so.post_flatten_positions([P("AAPL", 3, 750.0)], {"AAPL": 5})    # Day record > position: kept whole
        self.assertEqual((v[0].market_value, adj), (750.0, []))
        v, _ = so.post_flatten_positions([P("AAPL", 5, 1250.0)], {"AAPL": -3})    # Day short netted in: closing it grows the long
        self.assertAlmostEqual(v[0].market_value, 2000.0)
        v, adj = so.post_flatten_positions([P("MSFT", 2, 1000.0)], {})            # no Day lot: whole
        self.assertEqual((v[0].market_value, adj), (1000.0, []))
        v, _ = so.post_flatten_positions([P("SPY", 1, 90.0, "us_option")], {"SPY": 1})   # options never netted
        self.assertEqual(v[0].market_value, 90.0)
        v, _ = so.post_flatten_positions([P("X", 0, 0.0)], {"X": 2})
        self.assertEqual(v[0].market_value, 0.0)

    def test_half_day_calendar_moves_the_windows(self):
        self.fb.cal[self.D1] = (et(2026, 10, 29, 9, 30), et(2026, 10, 29, 13, 0))
        self.at(2026, 10, 29, 12, 40)
        self.settle()
        self.assertTrue(self.fb.by_coid(cid("ecls", 1, et(2026, 10, 29, 13, 0))))   # ids carry the half-day close

    def test_calendar_unavailable_skips_entry(self):
        self.fb.calendar_ok = False
        self.fb.clock_value = {"is_open": True, "next_open": None, "next_close": et(2026, 10, 29, 16, 0)}
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.assertEqual(self.fb.submits, [])


class ExitPaths(Base):
    def test_opg_rejected_restop_then_market_at_open(self):
        self.enter_and_fill()
        self.fb.next_submit = ["reject"]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.invariant()
        live = self.fb.live("sell")
        self.assertEqual((len(live), live[0].order_type), (1, "stop"))
        self.assertTrue(self.eng.state["exit"]["opg_rejected"])
        self.at(2026, 10, 30, 9, 28)
        self.settle()
        self.invariant()
        self.at(2026, 10, 30, 9, 30)
        self.settle()
        self.invariant()
        mkt = [o for o in self.fb.live("sell") if o.order_type == "market"]
        self.assertEqual((len(mkt), mkt[0].time_in_force, mkt[0].qty), (1, "day", 10))
        self.fb.fill(mkt[0], 10, 293.0)
        self.settle()
        self.invariant()
        self.assertEqual(self.eng.state["nights"]["20261029"]["status"], "closed")

    def test_held_for_orders_opg_reject_is_handled_as_rejected(self):
        self.enter_and_fill()
        self.fb.next_submit = ["held_for_orders"]
        self.at(2026, 10, 30, 9, 21)
        self.settle()
        self.invariant()
        self.assertTrue(self.eng.state["exit"]["opg_rejected"])

    def test_opg_unknown_at_cutoff_pages_and_submits_nothing_then_resolves_at_open(self):
        self.enter_and_fill()
        self.fb.next_submit = ["unknown_accept"]
        self.at(2026, 10, 30, 9, 20)
        self.fb.hidden.add(OPG1)
        self.fb.lookup_unknown = True
        self.settle()
        n_submits = len(self.fb.submits)
        self.at(2026, 10, 30, 9, 28)
        for _ in range(so._UNRESOLVED_POLL_ATTEMPTS + 1):
            self.clock.now += timedelta(seconds=5)
            self.settle()
        self.assertEqual(len(self.fb.submits), n_submits)
        self.assertTrue(any("UNKNOWN for" in p for p in self.pages))   # position-driven: one unknown-order page
        self.fb.hidden.clear()
        self.fb.lookup_unknown = False
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(self.fb.by_coid(OPG1), 10, 295.0)
        self.settle()
        self.invariant()
        self.assertEqual(self.fb.position, 0)
        self.assertEqual(len(self.fb.submits), n_submits)

    def test_opg_submit_timed_out_but_accepted(self):
        self.enter_and_fill()
        self.fb.next_submit = ["unknown_accept"]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.invariant()
        self.assertEqual(self.eng.state["nights"]["20261029"]["orders"][OPG1]["state"], "ack")

    def test_market_sell_open_60s_cancel_restop_retry(self):
        self.enter_and_fill()
        self.fb.next_submit = ["reject"]                      # opg rejected → market path at the open
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.settle()
        self.invariant()
        mkt1 = [o for o in self.fb.live("sell") if o.order_type == "market"][0]
        self.at(2026, 10, 30, 9, 30, 30)
        self.settle()
        self.assertEqual(mkt1.status, "new")                  # < 60 s: wait
        self.at(2026, 10, 30, 9, 31, 1)
        self.settle()
        self.invariant()
        self.assertEqual(mkt1.status, "canceled")
        live = self.fb.live("sell")
        self.assertEqual((len(live), live[0].order_type), (1, "stop"))
        self.at(2026, 10, 30, 9, 32, 2)
        self.settle()
        self.invariant()
        mkt2 = [o for o in self.fb.live("sell") if o.order_type == "market"][0]
        self.assertNotEqual(mkt2.id, mkt1.id)
        self.fb.fill(mkt2, 10, 294.0)
        self.settle()
        self.invariant()
        self.assertEqual(self.fb.position, 0)

    def test_market_sell_rejected_restop_retry_and_third_failure_pages(self):
        self.enter_and_fill()
        self.fb.next_submit = [("opg", "reject")] + [("day", "reject")] * 3   # opg + 3 market sells rejected
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        for minute in (30, 31, 32):
            self.at(2026, 10, 30, 9, minute, 1)
            self.settle()
            self.invariant()
            live = self.fb.live("sell")
            self.assertEqual((len(live), live[0].order_type), (1, "stop"))
        self.assertEqual(self.eng.state["exit"]["mkt_failures"], 3)
        self.assertTrue(any("exit attempt 3" in p for p in self.pages))
        self.at(2026, 10, 30, 9, 33, 2)
        self.settle()
        mkt = [o for o in self.fb.live("sell") if o.order_type == "market"]
        self.assertEqual(len(mkt), 1, "retries continue after the page")
        self.fb.fill(mkt[0], 10, 294.0)
        self.settle()
        self.invariant()

    def test_partial_stop_fill_during_cancel_window(self):
        self.enter_and_fill()
        self.fb.cancel_mode = "pending"
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        stop = next(o for o in self.fb.orders.values() if o.order_type == "stop")
        self.assertEqual(stop.status, "pending_cancel")
        self.fb.fill(stop, 4, 282.0)
        stop.status = "canceled"
        self.at(2026, 10, 30, 9, 20, 5)
        self.settle()
        self.invariant()
        opg = self.fb.by_coid(OPG1)
        self.assertEqual(opg.qty, 6)

    def test_stop_fills_at_the_open(self):
        self.enter_and_fill()
        self.fb.next_submit = ["reject"]
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        stop = self.fb.live("sell")[0]
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(stop, 10, 281.0)
        self.settle()
        self.invariant()
        self.assertEqual(self.fb.position, 0)
        self.assertEqual(self.eng.state["closed"][0]["exit_paths"], ["stop"])

    def test_unavailable_calendar_exit_runs_on_broker_clock(self):
        self.enter_and_fill()
        self.fb.calendar_ok = False
        self.eng.state.pop("calendar", None)
        self.fb.clock_value = {"is_open": True, "next_open": None, "next_close": et(2026, 10, 30, 16, 0)}
        self.at(2026, 10, 30, 9, 31)
        self.settle()
        self.invariant()
        mkt = [o for o in self.fb.live("sell") if o.order_type == "market"]
        self.assertEqual(len(mkt), 1)
        self.assertTrue(self.ev("clock_source"))

    def test_broker_clock_down_over_60s_uses_system_clock_for_exit(self):
        self.enter_and_fill()
        self.fb.calendar_ok = False
        self.fb.clock_ok = False
        self.eng.state.pop("calendar", None)
        self.at(2026, 10, 30, 9, 29)
        self.settle()
        self.assertFalse([o for o in self.fb.live("sell") if o.order_type == "market"])
        self.at(2026, 10, 30, 9, 31)
        self.settle()
        self.invariant()
        self.assertTrue([o for o in self.fb.live("sell") if o.order_type == "market"])
        self.assertEqual(self.eng.state["clock_source"], "system_clock")


class SafetyPaths(Base):
    def test_foreign_shares_are_never_traded_and_block_entry(self):
        # two-source minimum (round 7): shares trend_night's own orders do not account for are the owner's — paged,
        # never traded, and they block entries
        self.fb.position = 3
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.at(2026, 10, 29, 15, 40, 10)
        self.settle()
        self.assertEqual(self.fb.submits, [], "no entry, no stop on shares trend_night does not own")
        self.assertTrue(any("not trend_night's" in p for p in self.pages))
        self.assertTrue(any("not confirmed flat" in kw["reason"] for kw in self.ev("skip")))

    def test_unreadable_orders_or_position_submits_nothing(self):
        self.enter_and_fill()
        n = len(self.fb.submits)
        for attr in ("list_ok", "position_ok"):
            with self.subTest(attr=attr):
                setattr(self.fb, attr, False)
                self.at(2026, 10, 30, 9, 20)
                self.settle()
                self.assertEqual(len(self.fb.submits), n)
                setattr(self.fb, attr, True)

    def test_restart_mid_sequence_rebuilds_from_broker(self):
        self.enter_and_fill()
        self.state_path.unlink()                              # state lost: rebuild from the TN orders
        eng2 = self.engine()
        self.at(2026, 10, 30, 9, 20)
        self.settle(eng2)
        self.invariant()
        opg = self.fb.by_coid(OPG1)
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(opg, 10, 295.0)
        self.settle(eng2)
        self.invariant()
        self.assertEqual(eng2.state["nights"]["20261029"]["status"], "closed")

    def test_corrupt_state_pages_and_blocks_entry_today(self):
        self.state_path.write_text("{not json")
        self.at(2026, 10, 29, 15, 40)
        eng2 = self.engine()
        self.assertTrue(any("state unreadable" in p for p in self.pages))
        self.assertEqual(eng2.state["entry_blocked_day"], "2026-10-29")
        self.assertTrue(list(Path(self.tmp.name).glob("tn.json.corrupt-*")))   # kept for forensics
        self.settle(eng2)
        self.assertEqual(self.fb.submits, [])
        self.assertTrue(any("rebuilt" in kw["reason"] for kw in self.ev("skip")))

    def test_restart_while_stop_replace_unknown(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 4, 294.0)
        self.settle()
        orig = self.fb.replace

        def lost_reply(*a, **kw):
            orig(*a, **kw)
            return "unknown", "read timeout"
        self.fb.replace = lost_reply
        self.fb.fill(cls, 6, 296.0)
        self.eng.tick()
        self.fb.replace = orig
        eng2 = self.engine()
        self.settle(eng2)
        self.invariant()

    def test_outside_windows_flat_is_a_noop(self):
        self.at(2026, 10, 29, 15, 40)
        self.fd.spy_up = False
        self.settle()                                          # bootstraps; regime off
        calls = []
        self.fb.list_orders = lambda *a: calls.append(a) or []
        self.at(2026, 10, 29, 20, 0)
        out = self.eng.tick()
        self.assertEqual(calls, [])
        self.assertIn("no-op", out.note)

    def test_state_writes_are_atomic_json(self):
        self.enter_and_fill()
        d = json.loads(self.state_path.read_text())
        self.assertEqual(d["nights"]["20261029"]["status"], "holding")
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp*")))


class CoidV12(unittest.TestCase):
    """v9 item 1 / v10 item 1 / v11 item 3: IN-QQQM-<b|s>-<night_close_epoch_ms>-tn<step><n>, strictly parsed."""
    NOW = et(2026, 10, 29, 15, 40)

    def test_round_trip_and_ledger_tier(self):
        from execution.ownership_guard import tier_of_coid
        for step, side in (("ecls", "b"), ("emkt", "b"), ("stop", "s"), ("opg", "s"), ("mkt", "s")):
            with self.subTest(step=step):
                c = cid(step, 3)
                self.assertTrue(c.startswith(f"IN-QQQM-{side}-"))
                p = so.parse_coid(c, self.NOW)
                self.assertEqual((p["night"], p["step"], p["attempt"], p["side"]), ("20261029", step, 3, side))
                self.assertEqual(p["close_ms"], so.epoch_ms(D1_CLOSE))
                self.assertEqual(tier_of_coid(c), "intraday")      # the ledger credits the Swing tier

    def test_rejects_everything_else(self):
        ms = so.epoch_ms(D1_CLOSE)
        bad = {
            "side does not match step": CLS.replace("-b-", "-s-"),
            "sell step as buy": cid("stop").replace("-s-", "-b-"),
            "wrong symbol": CLS.replace("QQQM", "QQQ"),
            "unknown step": f"IN-QQQM-s-{ms}-tnfoo1",
            "zero attempt": f"IN-QQQM-s-{ms}-tnstop0",
            "malformed attempt": f"IN-QQQM-s-{ms}-tnstopx",
            "attempt too long": f"IN-QQQM-s-{ms}-tnstop12345",
            "non-numeric epoch": "IN-QQQM-s-17x1234567890-tnstop1",
            "before the first night": cid("stop", 1, et(2026, 10, 28, 16, 0)),
            "more than a day ahead": cid("stop", 1, et(2026, 11, 5, 16, 0)),
            "over Alpaca's length": CLS + "0" * 200,
            "trailing text": CLS + "x",
            "old TN- id": "TN-20261029-entry-cls",
            "a Swing-breakout id": f"IN-QQQM-b-{ms}-abcdef12",
            "other tier": CLS.replace("IN-", "DT-", 1),
        }
        for why, c in bad.items():
            with self.subTest(why=why):
                self.assertIsNone(so.parse_coid(c, self.NOW))
        self.assertIsNotNone(so.parse_coid(cid("stop", 1, et(2026, 11, 5, 16, 0)), et(2026, 11, 5, 15, 40)))
        with self.assertRaises(ValueError):
            so.build_coid(D1_CLOSE, "foo", 1)
        with self.assertRaises(ValueError):
            so.build_coid(D1_CLOSE, "stop", 0)


def _bars(closes, opens=None, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens if opens is not None else closes, dtype=float)
    return pd.DataFrame({"open": o, "close": c}, index=idx)


def _prior_from(rets, n_star=200.0, alpha=0.05):
    var = statistics.variance(rets)
    return {"ok": True, "n": len(rets), "mean": statistics.fmean(rets), "var": var, "sd": var ** 0.5,
            "sum": sum(rets), "sumsq": sum(x * x for x in rets), "n_star": n_star, "alpha": alpha,
            "rho": so.mixture_rho(n_star, alpha)}


class PriorAndBoundary(unittest.TestCase):
    def test_replay_prior_point_in_time(self):
        spy = _bars([1, 2, 3, 4, 5, 0.5, 6, 7, 8, 9])
        opens = [100, 101, 102, 101, 102, 101, 102, 101, 102, 101]
        qqq = _bars([100] * 10, opens)
        pr = so.replay_prior(spy, qqq, 3, 0.0002)
        self.assertTrue(pr["ok"])
        # SMA(3) state True from day 2; a night after day d uses day d-1's state, so nights 3..8 qualify except the
        # night after day 6 (day 5 broke below its SMA); the last day has no next open
        idx = qqq.index
        want = [opens[i + 1] / 100 - 1 - 0.0002 for i in (3, 4, 5, 7, 8)]
        self.assertEqual(pr["n"], len(want))
        self.assertAlmostEqual(pr["mean"], statistics.fmean(want), places=12)
        self.assertAlmostEqual(pr["var"], statistics.variance(want), places=12)
        self.assertEqual(pr["first"], str(idx[3])[:10])
        self.assertEqual(pr["n_star"], len(want))

    def test_replay_prior_unavailable_below_two_nights(self):
        pr = so.replay_prior(_bars([5, 4, 3, 2, 1]), _bars([100] * 5), 3, 0.0002)
        self.assertFalse(pr["ok"])
        self.assertIn("prior unavailable", pr["why"])

    def test_rho_and_halfwidth(self):
        rho = so.mixture_rho(200, 0.05)
        two_l = math.log(1 / 0.05 ** 2)
        self.assertAlmostEqual(rho, 200 / (two_l + math.log(1 + two_l)), places=12)
        hw1 = so.mixture_halfwidth(1e-4, 1, rho, 0.05, 1000)
        want = math.sqrt(1e-4 * 2 * (1 + rho) / 1 * math.log(math.sqrt((1 + rho) / rho) / 0.05) + 1e-4 / 1000)
        self.assertAlmostEqual(hw1, want, places=12)
        self.assertLess(so.mixture_halfwidth(1e-4, 500, rho, 0.05, 1000), hw1)
        self.assertIsNone(so.mixture_halfwidth(1e-4, 0, rho, 0.05, 1000))
        self.assertIsNone(so.mixture_rho(0, 0.05))

    def test_pooled_budget(self):
        self.assertEqual(so.pooled_budget(None, [], 0.04)["b"], 0.05)
        self.assertEqual(so.pooled_budget({"ok": False, "why": "x"}, [0.01], 0.04)["b"], 0.05)
        good = _prior_from([0.0004 + 0.001 * ((-1) ** i) for i in range(400)])
        out = so.pooled_budget(good, [], 0.04)
        allr = [0.0004 + 0.001 * ((-1) ** i) for i in range(400)]
        kelly = 0.25 * statistics.fmean(allr) / statistics.variance(allr) * 0.04
        self.assertAlmostEqual(out["kelly_b"], kelly, places=9)
        self.assertAlmostEqual(out["b"], max(0.05, kelly), places=9)
        # live nights move it from night one (no count switch)
        self.assertNotAlmostEqual(so.pooled_budget(good, [-0.02], 0.04)["kelly_b"], kelly, places=6)
        bad = _prior_from([-0.0004 + 0.001 * ((-1) ** i) for i in range(400)])
        self.assertEqual(so.pooled_budget(bad, [], 0.04)["b"], 0.05)       # non-positive mean → floor
        flat = {"ok": True, "n": 3, "sum": 0.003, "sumsq": 0.000003}
        self.assertEqual(so.pooled_budget(flat, [], 0.04)["b"], 0.05)      # zero variance clamped → floor


class AdmissionAndSizing(Base):
    def test_entry_admitted_with_the_swing_breakout_call_shape(self):
        from execution import tier_capital_allocator as tca
        calls, real = [], tca.live_admit

        def spy(*a, **kw):
            calls.append((a, kw))
            return real(*a, **kw)
        with mock.patch("execution.tier_capital_allocator.live_admit", side_effect=spy):
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        a, kw = calls[0]
        self.assertEqual(a[:5], ("swing", "intraday", "QQQM", "buy", 10))
        self.assertEqual((kw["overnight"], kw["client_order_id"], kw["time_in_force"], kw["order_type"]),
                         (True, CLS, "cls", "market"))
        self.assertEqual(self.fb.by_coid(CLS).qty, 10)

    def test_refused_admission_skips_the_night(self):
        from execution.tier_capital_allocator import Decision
        with mock.patch("execution.tier_capital_allocator.live_admit", return_value=Decision(False, "swing cap")):
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        self.assertEqual(self.fb.submits, [])
        self.assertTrue(any("allocator refused: swing cap" in kw["reason"] for kw in self.ev("skip")))

    def test_lease_bound_on_ack_released_on_reject(self):
        lease = SimpleNamespace(id="L1", client_order_id=CLS)
        dec = SimpleNamespace(approved=True, reason="ok", lease=lease)
        with mock.patch("execution.tier_capital_allocator.live_admit", return_value=dec), \
                mock.patch("execution.tier_capital_allocator.live_bind") as bind:
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        self.assertEqual(bind.call_args[0][0], lease)
        self.assertEqual(bind.call_args[0][1].client_order_id, CLS)
        self.setUp()
        self.fb.next_submit = ["reject"]
        with mock.patch("execution.tier_capital_allocator.live_admit", return_value=dec), \
                mock.patch("execution.tier_capital_allocator.live_release_proven_no_order") as rel:
            self.at(2026, 10, 29, 15, 40)
            self.settle()
        self.assertEqual(rel.call_args[0][0], lease)

    def test_broker_expired_cls_releases_its_lease(self):
        lease = SimpleNamespace(id="L1", client_order_id=CLS)
        dec = SimpleNamespace(approved=True, reason="ok", lease=lease)
        with mock.patch("execution.tier_capital_allocator.live_admit", return_value=dec), \
                mock.patch("execution.tier_capital_allocator.live_bind"), \
                mock.patch("execution.tier_capital_allocator.live_release") as rel:
            self.at(2026, 10, 29, 15, 40)
            self.settle()
            self.fb.by_coid(CLS).status = "expired"
            self.at(2026, 10, 29, 15, 55)
            self.settle()
        self.assertIn(mock.call(lease, "broker_terminal_zero_fill"), rel.call_args_list)

    def test_fallback_resized_from_a_fresh_broker_read(self):
        self.fb.next_submit = ["reject"]
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        self.fb.acct.buying_power = self.fb.acct.regt_buying_power = 294.0 * 3 + 1
        self.at(2026, 10, 29, 15, 55)
        self.settle()
        self.assertEqual(self.fb.by_coid(MKT).qty, 3)
        self.assertEqual(self.ev("sizing")[-1]["sizing"]["bound_by"], "buying_power")

    def test_prior_unavailable_sizes_at_the_owner_floor(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        bud = self.ev("sizing")[0]["sizing"]["budget"]
        self.assertEqual((bud["b"], bud["prior"]), (0.05, "unavailable"))
        self.assertTrue(self.ev("prior"))

    def test_every_order_of_a_night_is_tier_tagged_and_parses(self):
        from execution.ownership_guard import tier_of_coid
        self.enter_and_fill()
        self.at(2026, 10, 30, 9, 20)
        self.settle()
        self.at(2026, 10, 30, 9, 30)
        self.fb.fill(self.fb.by_coid(OPG1), 10, 295.0)
        self.settle()
        for o in self.fb.orders.values():
            self.assertEqual(tier_of_coid(o.client_order_id), "intraday")
            self.assertIsNotNone(so.parse_coid(o.client_order_id, self.clock.now))
        rec = self.eng.state["closed"][0]
        self.assertEqual(rec["b"], 0.05)
        self.assertAlmostEqual(rec["loss_budget_usd"], 10 * 294.10 * rec["q"])

    def test_restart_after_state_loss_never_reuses_an_attempt(self):
        self.at(2026, 10, 29, 15, 40)
        self.settle()
        cls = self.fb.by_coid(CLS)
        self.at(2026, 10, 29, 16, 0)
        self.fb.fill(cls, 4, 294.0)
        self.settle()
        self.assertTrue(self.fb.by_coid(cid("stop", 1)))
        self.state_path.unlink()
        eng2 = self.engine()
        self.fb.fill(cls, 6, 296.0)
        self.settle(eng2)
        self.clock.now += timedelta(seconds=10)               # the 10 sh confirmed on a later pass (rule 1)
        self.settle(eng2)
        self.invariant(eng=eng2)
        self.assertEqual(self.fb.replaces[-1]["coid"], cid("stop", 2), "attempt 1 is never re-issued")


class PageOnlyReviews(Base):
    def setUp(self):
        super().setUp()
        self.at(2026, 10, 30, 10, 0)
        self.eng._now = self.clock.now

    def _set_prior(self, mean=0.0003, sd=0.01, n=1000, n_star=200.0):
        pr = {"ok": True, "n": n, "mean": mean, "var": sd * sd, "sd": sd, "sum": mean * n,
              "sumsq": sd * sd * (n - 1) + n * mean * mean, "n_star": n_star, "alpha": 0.05,
              "rho": so.mixture_rho(n_star, 0.05), "date": "2026-10-30"}
        self.eng.state["prior_cache"] = pr
        return pr

    def test_edge_check_pages_but_never_switches_off(self):
        self._set_prior()
        self.eng.state["closed"] = [{"night": f"n{i}", "ret": -0.01, "pnl": -29.4} for i in range(60)]
        self.eng._edge_check()
        self.assertTrue(any("EDGE CHECK" in p for p in self.pages))
        ev = self.ev("edge_check")[-1]
        self.assertEqual(ev["result"], "below boundary")
        for k in ("live_nights", "live_mean", "prior_mean", "sigma", "rho", "alpha", "boundary", "nightly_returns"):
            self.assertIn(k, ev)
        self.assertIsNone(self.eng._entry_blockers("2026-10-30"))     # no automatic halt
        self.assertNotIn("auto_off", self.eng.state)

    def test_one_normal_losing_night_cannot_fire(self):
        self._set_prior()
        self.eng.state["closed"] = [{"night": "n0", "ret": -0.005, "pnl": -14.7}]
        self.eng._edge_check()
        self.assertEqual(self.pages, [])
        self.assertEqual(self.ev("edge_check")[-1]["result"], "within boundary")

    def test_edge_check_prior_unavailable_logs_only(self):
        self.eng.state["closed"] = [{"night": "n0", "ret": -0.5, "pnl": -100.0}]
        self.eng._edge_check()                    # FakeData's flat SPY → no trend nights → prior unavailable
        self.assertEqual(self.pages, [])
        self.assertEqual(self.ev("edge_check")[-1]["result"], "prior unavailable")

    def test_night_loss_review_uses_that_nights_own_budget(self):
        self.eng._after_booking({"night": "20261029", "pnl": -130.0, "b": 0.05, "equity": 2555.0,
                                 "loss_budget_usd": 127.75})
        self.assertTrue(any("size review" in p for p in self.pages))
        self.pages.clear()
        self.eng._after_booking({"night": "20261028", "pnl": -100.0, "b": 0.05, "equity": 2555.0,
                                 "loss_budget_usd": 127.75})
        self.assertEqual(self.pages, [])

    def _cost_setup(self, entry, exit_):
        self._set_prior()
        self.eng.state["spread_cache"] = {"date": "2026-10-30", "ok": True, "n": 20, "mean": 0.0001, "var": 1e-10,
                                          "sd": 1e-5}
        # FakeData QQQM bars: close 100, open 96 every session (overnight return -4%)
        self.eng.state["closed"] = [{"night": "20261027", "date": "2026-10-27", "exit_date": "2026-10-28",
                                     "entry_avg": entry, "exit_avg": exit_}]
        self.eng._fill_cost_step()
        return self.eng.state["closed"][0]["cost"]

    def test_realized_cost_formula_and_page(self):
        cost = self._cost_setup(100.5, 95.5)
        self.assertAlmostEqual(cost, ((100.5 - 100.0) + (96.0 - 95.5)) / 100.0, places=12)
        self.assertTrue(any("fill cost" in p for p in self.pages))
        self.assertIsNone(self.eng._entry_blockers("2026-10-30"))

    def test_realized_cost_within_boundary_no_page(self):
        self.assertAlmostEqual(self._cost_setup(100.0, 96.0), 0.0, places=12)
        self.assertEqual(self.pages, [])
        self.assertEqual(self.ev("fill_cost_check")[-1]["result"], "within boundary")

    def test_spread_prior_from_sip_quotes(self):
        sp = self.eng._spread_prior()           # sessions in the fake calendar before 10-30: 10-27→10-28, 10-28→10-29
        self.assertTrue(sp["ok"])
        self.assertEqual(sp["n"], 2)
        self.assertAlmostEqual(sp["mean"], statistics.fmean([0.02 / 100.0, 0.02 / 96.0]), places=12)
        self.at(2026, 10, 29, 10, 0)
        self.eng._now = self.clock.now
        self.assertFalse(self.eng._spread_prior()["ok"])          # one session → prior unavailable


class Reservation(Base):
    """v8 item 1: QQQM is reserved to trend_night in every path that could adopt, re-stop or mis-credit it."""

    def test_symbols_never_read_state(self):
        self.assertEqual(so.trend_night_symbols(), {"QQQM"})
        with mock.patch.object(so, "_DEFAULT_STATE", Path(self.tmp.name) / "missing.json"):
            self.assertEqual(so.trend_night_symbols(), {"QQQM"})

    def test_quantities_follow_the_reconciled_held(self):
        self.assertEqual(so.trend_night_quantities(Path(self.tmp.name) / "none.json"), {})
        self.enter_and_fill()
        self.assertEqual(so.trend_night_quantities(self.state_path), {"QQQM": 10.0})
        self.state_path.write_text("garbage")
        with self.assertRaises(ValueError):
            so.trend_night_quantities(self.state_path)

    def test_orphan_reconcile_never_adopts_qqqm(self):
        from execution import orphan_manager as om
        pos = SimpleNamespace(symbol="QQQM", qty="10", avg_entry_price="294.10")
        tracker = SimpleNamespace(open_trades={}, closed_trades=[], traded_today=set(), _save_log=lambda: None)
        with mock.patch.object(om, "get_open_positions", return_value=[pos]), \
                mock.patch.object(om, "_get_qhm_syms", return_value=frozenset()), \
                mock.patch.object(om, "_get_forever6_syms", return_value=set()), \
                mock.patch.object(om, "_get_breakout_syms", return_value=set()), \
                mock.patch.object(om, "_get_daytrade_syms", return_value=set()), \
                mock.patch.object(om, "submit_gtc_stop_order", side_effect=AssertionError("no adoption stop")), \
                mock.patch.object(om, "send_slack"):
            om.reconcile_positions(tracker)
        self.assertNotIn("QQQM", tracker.open_trades)

    def test_orphan_lookup_fails_closed(self):
        from execution import orphan_manager as om
        with mock.patch("execution.swing_overnight.trend_night_symbols", side_effect=RuntimeError("x")):
            self.assertEqual(om._get_trend_night_syms(), {"QQQM"})

    def test_stop_protection_excludes_qqqm(self):
        from execution import stop_protection as sp
        trade = {"symbol": "QQQM", "direction": "long", "status": "open", "qty": 10, "stop": 282.0}
        tracker = SimpleNamespace(open_trades={"QQQM": trade}, _save_log=lambda: None)
        pos = SimpleNamespace(symbol="QQQM", qty=10, side="long", current_price=294.0)
        fail = mock.Mock(side_effect=AssertionError("no stop on QQQM"))
        with mock.patch.object(sp, "get_open_orders", return_value=[]), \
                mock.patch.object(sp, "get_open_positions", return_value=[pos]), \
                mock.patch.object(sp, "submit_day_stop_order", fail), \
                mock.patch.object(sp, "submit_gtc_stop_order", fail), \
                mock.patch.object(sp, "close_position", fail), \
                mock.patch.object(sp, "_qhm_symbols", return_value=set()), \
                mock.patch.object(sp, "_forever6_symbols", return_value=set()), \
                mock.patch.object(sp, "_page"):
            summary = sp.reconcile_protection(tracker, None, session="ah")
        self.assertEqual(summary["excluded_trend_night"], ["QQQM"])
        with mock.patch("execution.swing_overnight.trend_night_symbols", side_effect=RuntimeError("x")):
            self.assertEqual(sp._trend_night_symbols(), {"QQQM"})

    def test_unmanaged_alarm_credits_trend_night(self):
        from execution import unmanaged_positions as um
        self.enter_and_fill()
        names = [n for n, _ in um._sources(SimpleNamespace(open_trades={}), self.clock.now)]
        self.assertIn("swing_trend_night", names)
        um._MEM.clear()
        pos = SimpleNamespace(symbol="QQQM", qty=10, market_value=2941.0, asset_class="us_equity")
        with mock.patch.object(so, "_DEFAULT_STATE", self.state_path), \
                mock.patch.object(um, "_STATE", Path(self.tmp.name) / "alarm.json"), \
                mock.patch("execution.broker.get_open_positions", return_value=[pos]), \
                mock.patch.object(um, "_sources", lambda tracker, now: [
                    ("swing_trend_night", lambda: so.trend_night_quantities())]), \
                mock.patch.object(um, "_page") as page:
            out = um.check_unmanaged(SimpleNamespace(open_trades={}), now_et=self.clock.now)
        um._MEM.clear()
        self.assertEqual(out, {})
        page.assert_not_called()

    def test_main_startup_count_excludes_qqqm(self):
        import inspect

        import main
        src = inspect.getsource(main.main)
        self.assertIn("_tn_syms = trend_night_symbols()", src)
        self.assertIn("p.symbol not in _qhm_syms and p.symbol not in _tn_syms", src)
        # fails closed like orphan_manager / stop_protection: a lookup error still excludes the configured symbol
        self.assertIn('_tn_syms = {str(getattr(config, "SWING_TREND_NIGHT_SYMBOL", "QQQM")).upper()}', src)


if __name__ == "__main__":
    unittest.main()


class DayTierLots(unittest.TestCase):
    """TREND-NIGHT-DAYLOTS-1103: the Day tier's force-flat set, signed; anything unreadable → None (count whole)."""

    def setUp(self):
        from execution import day_trade_manager as dtm
        self.state = {}
        p = mock.patch.object(dtm, "_load_state", side_effect=lambda: self.state)
        p.start()
        self.addCleanup(p.stop)

    def test_fill_unverified_is_never_netted(self):
        from execution import day_trade_manager as dtm
        self.state = {"entry::AMZN::x": {"symbol": "AMZN", "state": "fill_unverified"}}
        t = {"AMZN": {"qty": 6, "side": "short"}}
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value=t):
            self.assertEqual(so.day_tier_flatten_lots(), {})

    def test_signed_lots_from_flatten_targets(self):
        from execution import day_trade_manager as dtm
        t = {"AMZN": {"qty": 6, "side": "short"}, "aapl": {"qty": 2, "side": "long"}, "X": {"qty": 0, "side": "long"}}
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value=t), \
                mock.patch.object(so, "_never_promoted_long", return_value=True):
            self.assertEqual(so.day_tier_flatten_lots(), {"AMZN": -6, "AAPL": 2})
        # board Thorp 2026-11-03: a long that can still be promoted at 3:56 PM keeps counting overnight
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", return_value=t), \
                mock.patch.object(so, "_never_promoted_long", return_value=False):
            self.assertEqual(so.day_tier_flatten_lots(), {"AMZN": -6})

    def test_never_promoted_long(self):
        from execution import day_promotion as dp
        from strategy import day_tier_leverage as lev
        with mock.patch.object(dp, "enabled", return_value=False):
            self.assertTrue(so._never_promoted_long("NVDA"))
        with mock.patch.object(dp, "enabled", return_value=True), \
                mock.patch.object(lev, "exposure_sign", return_value=("NVDA", 1)):
            self.assertFalse(so._never_promoted_long("NVDA"))
        with mock.patch.object(dp, "enabled", return_value=True), \
                mock.patch.object(lev, "exposure_sign", return_value=("QQQ", 3)):
            self.assertTrue(so._never_promoted_long("TQQQ"))
        with mock.patch.object(dp, "enabled", return_value=True), \
                mock.patch.object(lev, "exposure_sign", side_effect=RuntimeError("map unreadable")):
            self.assertFalse(so._never_promoted_long("NVDA"))

    def test_unreadable_or_off_is_none(self):
        from execution import day_trade_manager as dtm
        with mock.patch.object(dtm, "_enabled", return_value=True), \
                mock.patch.object(dtm, "_flatten_targets", side_effect=RuntimeError("log unreadable")):
            self.assertIsNone(so.day_tier_flatten_lots())
        with mock.patch.object(dtm, "_enabled", return_value=False):
            self.assertIsNone(so.day_tier_flatten_lots())
