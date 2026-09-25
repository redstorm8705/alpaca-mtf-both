# ruff: noqa: E501 — fixture lines mirror broker payloads (project convention)
"""broker.replace_stop_order / resolve_live_order / cancel_stop_confirmed — P0 stop-replace (2026-09-25).

A fake TradingClient models Alpaca's documented replace semantics: success returns a NEW order and
marks the old one "replaced" (replaced_by = new id); pending_* orders reject replace and cancel."""
import unittest
from types import SimpleNamespace
from unittest import mock

from execution import broker


class FakeClient:
    def __init__(self):
        self.orders = {}
        self.replace_calls = []
        self.cancel_calls = []
        self.replace_raises = None       # exception to raise from replace_order_by_id
        self.replace_applies = True      # on raise: did the replace happen anyway (lost reply)?
        self.cancel_to = "canceled"      # status an accepted cancel moves the order to

    def add(self, oid, status="new", stop_price=100.0, qty="5", replaced_by=None,
            coid="IN-UBER-s-1700000000000-abcd1234", side="sell"):
        self.orders[oid] = SimpleNamespace(id=oid, status=status, stop_price=stop_price, qty=qty,
                                           replaced_by=replaced_by, client_order_id=coid, side=side)
        return self.orders[oid]

    def get_order_by_id(self, oid):
        if oid not in self.orders:
            raise RuntimeError("404 order not found")
        return self.orders[oid]

    def replace_order_by_id(self, oid, req):
        self.replace_calls.append((oid, req))
        old = self.orders[oid]
        if old.status in ("accepted", "pending_new", "pending_cancel", "pending_replace",
                          "replaced", "canceled", "filled", "expired", "rejected"):
            raise RuntimeError("422 order cannot be replaced in status " + old.status)
        new_id = oid + "-r"
        new = self.add(new_id, stop_price=req.stop_price, qty=str(req.qty) if req.qty else old.qty,
                       coid=req.client_order_id or "auto-generated-untagged", side=old.side)
        if self.replace_raises is not None:
            if self.replace_applies:
                old.status, old.replaced_by = "replaced", new_id
            else:
                del self.orders[new_id]
            raise self.replace_raises
        old.status, old.replaced_by = "replaced", new_id
        return new

    def cancel_order_by_id(self, oid):
        self.cancel_calls.append(oid)
        o = self.orders[oid]
        if o.status in ("pending_replace", "pending_cancel"):
            raise RuntimeError("422 order is not cancelable")
        o.status = self.cancel_to


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        p = mock.patch.object(broker, "_get_trading_client", return_value=self.fake)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(broker.time, "sleep", lambda s: None)
        s.start()
        self.addCleanup(s.stop)


class TestReplace(Base):
    def test_success_returns_new_order_and_never_cancels(self):
        self.fake.add("A")
        new = broker.replace_stop_order("UBER", "A", 99.974)
        self.assertEqual(new.id, "A-r")
        self.assertEqual(new.stop_price, 99.97)
        self.assertEqual(self.fake.orders["A"].status, "replaced")
        self.assertEqual(self.fake.cancel_calls, [])
        self.assertIsNone(self.fake.replace_calls[0][1].qty)

    def test_replacement_keeps_the_tier_prefix(self):
        from execution.ownership_guard import tier_of_coid
        for coid, tier in (("IN-UBER-s-1-a", "intraday"), ("DT-UBER-b-1-a", "daytrade"), ("QH-UBER-s-1-a", "qhm")):
            self.fake.orders.clear()
            self.fake.add("A", coid=coid, side="buy" if tier == "daytrade" else "sell")
            new = broker.replace_stop_order("UBER", "A", 99.0)
            self.assertEqual(tier_of_coid(new.client_order_id), tier)
            self.assertNotEqual(new.client_order_id, coid)      # fresh id, same tier

    def test_untagged_or_unreadable_prior_sends_no_client_order_id(self):
        self.fake.add("A", coid="legacy-untagged")
        broker.replace_stop_order("UBER", "A", 99.0)
        self.assertIsNone(self.fake.replace_calls[-1][1].client_order_id)

    def test_qty_resize_sent_with_price(self):
        self.fake.add("A", qty="5")
        new = broker.replace_stop_order("NVDA", "A", 101.0, qty=3)
        self.assertEqual(self.fake.replace_calls[0][1].qty, 3)
        self.assertEqual(new.qty, "3")

    def test_refused_leaves_old_stop_in_force(self):
        self.fake.add("A", status="pending_replace")
        self.assertIsNone(broker.replace_stop_order("UBER", "A", 99.0))
        self.assertEqual(self.fake.orders["A"].status, "pending_replace")
        self.assertEqual(self.fake.cancel_calls, [])

    def test_lost_reply_adopts_the_replacing_order(self):
        self.fake.add("A")
        self.fake.replace_raises = TimeoutError("read timed out")
        new = broker.replace_stop_order("UBER", "A", 99.0)
        self.assertIsNotNone(new)
        self.assertEqual(new.id, "A-r")

    def test_raise_without_replace_returns_none(self):
        self.fake.add("A")
        self.fake.replace_raises = RuntimeError("500")
        self.fake.replace_applies = False
        self.assertIsNone(broker.replace_stop_order("UBER", "A", 99.0))
        self.assertEqual(self.fake.orders["A"].status, "new")

    def test_stale_replaced_id_at_another_price_is_not_a_success(self):
        # cold-2nd: A was replaced EARLIER by B at $95; moving "A" to $100 must not report B as done
        self.fake.add("A", status="replaced", replaced_by="B")
        self.fake.add("B", stop_price=95.0)
        self.assertIsNone(broker.replace_stop_order("UBER", "A", 100.0))

    def test_stale_replaced_id_already_at_requested_price_is_adopted(self):
        self.fake.add("A", status="replaced", replaced_by="B")
        self.fake.add("B", stop_price=100.0)
        self.assertEqual(broker.replace_stop_order("UBER", "A", 100.0).id, "B")

    def test_recovery_never_adopts_a_terminal_order(self):
        self.fake.add("A", status="replaced", replaced_by="B")
        self.fake.add("B", stop_price=100.0, status="rejected")
        self.assertIsNone(broker.replace_stop_order("UBER", "A", 100.0))

    def test_float_whole_qty_accepted(self):
        self.fake.add("A", qty="5")
        self.assertIsNotNone(broker.replace_stop_order("NVDA", "A", 101.0, qty=3.0))
        self.assertEqual(self.fake.replace_calls[0][1].qty, 3)

    def test_invalid_inputs_do_nothing(self):
        self.fake.add("A")
        for args in (("", 99.0, None), ("A", 0.0, None), ("A", 99.0, 0), ("A", float("nan"), None),
                     ("A", 99.0, 1.5)):
            self.assertIsNone(broker.replace_stop_order("UBER", args[0], args[1], qty=args[2]))
        self.assertEqual(self.fake.replace_calls, [])


class TestResolve(Base):
    def test_follows_replaced_by_chain(self):
        self.fake.add("A", status="replaced", replaced_by="B")
        self.fake.add("B", status="replaced", replaced_by="C")
        self.fake.add("C", status="new")
        order, oid = broker.resolve_live_order("A")
        self.assertEqual((oid, order.status), ("C", "new"))

    def test_replaced_without_replaced_by_stops_there(self):
        self.fake.add("A", status="replaced")
        order, oid = broker.resolve_live_order("A")
        self.assertEqual((oid, order.status), ("A", "replaced"))

    def test_hop_limit(self):
        for i in range(8):
            self.fake.add(f"O{i}", status="replaced", replaced_by=f"O{i + 1}")
        self.fake.add("O8")
        order, oid = broker.resolve_live_order("O0", max_hops=5)
        self.assertEqual(oid, "O5")

    def test_unreadable_returns_none(self):
        order, oid = broker.resolve_live_order("missing")
        self.assertEqual((order, oid), (None, "missing"))


class TestCancelConfirmed(Base):
    def test_live_stop_cancelled_and_confirmed(self):
        self.fake.add("A")
        self.assertTrue(broker.cancel_stop_confirmed("UBER", "A"))
        self.assertEqual(self.fake.cancel_calls, ["A"])

    def test_cancels_the_order_now_in_force_not_the_replaced_id(self):
        self.fake.add("A", status="replaced", replaced_by="B")
        self.fake.add("B")
        self.assertTrue(broker.cancel_stop_confirmed("UBER", "A"))
        self.assertEqual(self.fake.cancel_calls, ["B"])
        self.assertEqual(self.fake.orders["B"].status, "canceled")

    def test_already_filled_is_confirmed(self):
        self.fake.add("A", status="filled")
        self.assertTrue(broker.cancel_stop_confirmed("UBER", "A"))
        self.assertEqual(self.fake.cancel_calls, [])

    def test_stuck_pending_replace_is_not_confirmed(self):
        # a 422 on a pending_replace order must never read as "cancelled"
        self.fake.add("A", status="pending_replace")
        with mock.patch.object(broker.time, "monotonic", side_effect=[0.0, 0.5, 1.0, 1.5, 2.5]):
            self.assertFalse(broker.cancel_stop_confirmed("UBER", "A", max_wait_s=2.0))
        self.assertEqual(self.fake.cancel_calls, [])

    def test_accepted_overnight_stop_is_cancelled(self):
        # adversarial: overnight GTC stops sit in "accepted" for hours and CAN be cancelled
        self.fake.add("A", status="accepted")
        self.assertTrue(broker.cancel_stop_confirmed("UBER", "A"))
        self.assertEqual(self.fake.cancel_calls, ["A"])

    def test_cancel_sent_once_then_polled(self):
        self.fake.add("A")
        self.fake.cancel_to = "pending_cancel"
        with mock.patch.object(broker.time, "monotonic", side_effect=[0.0, 0.5, 1.0, 1.5, 2.5]):
            self.assertFalse(broker.cancel_stop_confirmed("UBER", "A", max_wait_s=2.0))
        self.assertEqual(self.fake.cancel_calls, ["A"])

    def test_unreadable_order_is_not_confirmed(self):
        self.assertFalse(broker.cancel_stop_confirmed("UBER", "missing"))

    def test_cancel_accepted_but_still_new_until_deadline_is_not_confirmed(self):
        self.fake.add("A")
        self.fake.cancel_to = "new"          # venue has not acted yet
        with mock.patch.object(broker.time, "monotonic", side_effect=[0.0, 1.0, 3.0]):
            self.assertFalse(broker.cancel_stop_confirmed("UBER", "A", max_wait_s=2.0))
        self.assertEqual(self.fake.cancel_calls, ["A"])   # sent once, then only polled


if __name__ == "__main__":
    unittest.main()
