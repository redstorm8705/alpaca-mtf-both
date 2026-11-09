# ruff: noqa: E501
"""QHM-STATUS-1109: QHM reads an order's status as plain text whether alpaca-py hands back an OrderStatus enum or a
string. On Python 3.10 str(OrderStatus.CANCELED) is "OrderStatus.CANCELED", so NVDA's stop recovery (2026-11-09)
could never prove its cancelled stop terminal and NVDA stayed without a stop."""
import ast
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from alpaca.trading.enums import OrderStatus  # noqa: E402

from execution.quarterly_hold_manager import QuarterlyHoldManager  # noqa: E402

_status = QuarterlyHoldManager._order_status


class OrderStatusText(unittest.TestCase):
    def test_enum_status_reads_as_plain_text(self):
        self.assertEqual(str(OrderStatus.CANCELED).lower() in ("canceled", "orderstatus.canceled"), True)
        for enum, text in ((OrderStatus.CANCELED, "canceled"), (OrderStatus.FILLED, "filled"),
                           (OrderStatus.EXPIRED, "expired"), (OrderStatus.REJECTED, "rejected"),
                           (OrderStatus.NEW, "new"), (OrderStatus.ACCEPTED, "accepted"),
                           (OrderStatus.PARTIALLY_FILLED, "partially_filled")):
            self.assertEqual(_status(SimpleNamespace(status=enum)), text)

    def test_string_and_missing_status(self):
        self.assertEqual(_status(SimpleNamespace(status="Canceled")), "canceled")
        self.assertEqual(_status(SimpleNamespace(status="OrderStatus.FILLED")), "filled")
        self.assertEqual(_status(SimpleNamespace(status=None)), "")
        self.assertEqual(_status(SimpleNamespace()), "")
        self.assertEqual(_status(None), "")

    def test_partial_fill_is_not_terminal(self):
        terminal = {"filled", "canceled", "cancelled", "rejected", "expired"}
        self.assertNotIn(_status(SimpleNamespace(status=OrderStatus.PARTIALLY_FILLED)), terminal)
        self.assertIn(_status(SimpleNamespace(status=OrderStatus.CANCELED)), terminal)

    def test_no_raw_status_stringify_left_in_qhm(self):
        """Every str(getattr(x, "status", ...)) in the QHM manager must unwrap the enum (.value) or split it."""
        path = os.path.join(os.path.dirname(__file__), "..", "execution", "quarterly_hold_manager.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str"
                    and node.args):
                inner = node.args[0]
                if isinstance(inner, ast.BoolOp):
                    inner = inner.values[0]
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == "getattr"
                        and len(inner.args) >= 2 and isinstance(inner.args[1], ast.Constant)
                        and inner.args[1].value == "status"):
                    bad.append(node.lineno)
        # the one remaining raw read (line ~2108) splits on "." itself; allow only reads followed by split
        src = open(path, encoding="utf-8").read().splitlines()
        bad = [n for n in bad if 'split(".")' not in src[n - 1]]
        self.assertEqual(bad, [], "raw enum stringify at lines %s" % bad)


if __name__ == "__main__":
    unittest.main()
