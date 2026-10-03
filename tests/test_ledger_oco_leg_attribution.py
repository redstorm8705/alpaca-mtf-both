#!/usr/bin/env python3
"""OCO/bracket child legs inherit their parent's tier in the ownership-ledger sync.

Claude 2026-10-02: Alpaca generates an untagged client_order_id for an OCO leg,
and the flat order list carries no parent link, so day-tier OCO stop fills were
attributed to intraday (crossed AMZN/META/MSFT ledger rows).
fetch_all_orders(nested=True) flattens legs with an exact parent link;
run_ledger_sync._inherit_leg_tiers re-attributes them.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import run_ledger_sync as rls
from execution.ownership_guard import sync_ledger
from reporting import pnl_ledger as pl


def _parent(oid, coid, legs, created="2026-09-21T14:00:00Z"):
    return {"id": oid, "client_order_id": coid, "created_at": created, "legs": legs,
            "order_class": "oco", "type": "limit"}


def _leg(oid, coid):
    return {"id": oid, "client_order_id": coid, "order_class": "oco", "type": "stop"}


class NestedFetchTests(unittest.TestCase):
    def test_flattens_legs_with_parent_link_and_default_stays_flat(self):
        page = [_parent("p1", "DT-AMZN-b-1-x", [_leg("l1", "53abfab6-uuid")]),
                {"id": "o2", "client_order_id": "IN-SPY-b-1-y",
                 "created_at": "2026-09-20T14:00:00Z", "legs": None}]
        urls = []

        def fake_get(url, tries=8):
            urls.append(url)
            return page

        with patch.object(pl, "_get_json", fake_get):
            out = pl.fetch_all_orders(nested=True)
        self.assertIn("nested=true", urls[0])
        by_id = {o["id"]: o for o in out}
        self.assertEqual(set(by_id), {"p1", "l1", "o2"})
        self.assertEqual(by_id["l1"]["_parent_id"], "p1")
        self.assertEqual(by_id["l1"]["_parent_client_order_id"], "DT-AMZN-b-1-x")
        self.assertNotIn("_parent_id", by_id["p1"])

        urls.clear()
        with patch.object(pl, "_get_json", fake_get):
            flat = pl.fetch_all_orders()
        self.assertIn("nested=false", urls[0])
        self.assertEqual({o["id"] for o in flat}, {"p1", "o2"})

    def test_legs_count_toward_page_limit(self):
        # 499 parents + 1 leg = a FULL page of 500 rows: must fetch the next page.
        first = [_parent(f"p{i}", f"IN-X-b-{i}-z", [],
                         created=f"2026-09-21T14:00:{i % 60:02d}Z")
                 for i in range(498)]
        first.append(_parent("pL", "DT-AMZN-b-9-x", [_leg("lL", "uuid")],
                             created="2026-09-01T14:00:00Z"))
        second = [_parent("old", "IN-Y-b-1-z", [], created="2026-08-01T14:00:00Z")]
        calls = []

        def fake_get(url, tries=8):
            calls.append(url)
            return first if len(calls) == 1 else second

        with patch.object(pl, "_get_json", fake_get), patch.object(pl.time, "sleep"):
            out = pl.fetch_all_orders(nested=True)
        self.assertEqual(len(calls), 2)
        self.assertIn("old", {o["id"] for o in out})


class InheritTests(unittest.TestCase):
    def test_untagged_leg_inherits_tagged_parent_only(self):
        orders = [
            {"id": "l1", "client_order_id": "uuid-1", "_parent_id": "p1",
             "_parent_client_order_id": "DT-AMZN-b-1-x"},
            {"id": "l2", "client_order_id": "DT-MSFT-s-1-y", "_parent_id": "p2",
             "_parent_client_order_id": "IN-MSFT-b-1-z"},   # tagged leg: untouched
            {"id": "l3", "client_order_id": "uuid-3", "_parent_id": "p3",
             "_parent_client_order_id": "uuid-parent"},     # untagged parent: untouched
            {"id": "o4", "client_order_id": "uuid-4"},       # no parent link: untouched
        ]
        cm = pl.build_coid_map(orders)
        self.assertEqual(rls._inherit_leg_tiers(cm, orders), 1)
        self.assertEqual(cm["l1"], "DT-AMZN-b-1-x")
        self.assertEqual(cm["l2"], "DT-MSFT-s-1-y")
        self.assertEqual(cm["l3"], "uuid-3")
        self.assertEqual(cm["o4"], "uuid-4")

    def test_day_tier_short_covered_by_oco_stop_leaves_no_crossed_row(self):
        fills = [
            {"symbol": "AMZN", "side": "sell_short", "qty": "5", "price": "200",
             "order_id": "entry", "transaction_time": "2026-09-21T14:00:23Z"},
            {"symbol": "AMZN", "side": "buy", "qty": "5", "price": "202",
             "order_id": "l1", "transaction_time": "2026-09-21T14:03:06Z"},
        ]
        orders = [{"id": "entry", "client_order_id": "DT-AMZN-s-1-a"},
                  {"id": "l1", "client_order_id": "53abfab6-uuid", "_parent_id": "p1",
                   "_parent_client_order_id": "DT-AMZN-b-1-b"}]
        cm = pl.build_coid_map(orders)
        rls._inherit_leg_tiers(cm, orders)
        saved = {}
        with patch("execution.ownership_guard.save_ledger",
                   lambda led: saved.setdefault("led", led)), \
                patch("execution.ownership_guard.load_ledger",
                      lambda: {"version": 1, "positions": {}}), \
                patch("execution.ownership_guard._load_heal_confirmations", lambda: {}):
            led = sync_ledger(fills, [], coid_by_order_id=cm)
        tiers = led["positions"]["AMZN"]["tiers"]
        self.assertEqual(tiers["daytrade"]["qty"], 0.0)
        self.assertEqual(tiers["intraday"]["qty"], 0.0)
        self.assertEqual(led["positions"]["AMZN"]["drift"], 0.0)


if __name__ == "__main__":
    unittest.main()
