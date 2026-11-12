"""Regression coverage for ownership-ledger replay invariants."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution import ownership_guard as guard


class TestProtectedReplayBalances(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = {
            "_LEDGER_PATH": root / "ownership_ledger.json",
            "_LEDGER_BAK_PATH": root / "ownership_ledger.bak.json",
            "_PROTECTED_WATERMARK_PATH": root / "protected-watermark.json",
            "_LEDGER_LOCK_PATH": root / ".ledger.lock",
            "_HEAL_CONFIRM_PATH": root / "ledger_heal_confirmations.json",
        }
        self.patches = [
            mock.patch.object(guard, name, value)
            for name, value in self.paths.items()
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def _fill(order_id, side, qty):
        return {"symbol": "NVDA", "order_id": order_id, "side": side,
                "qty": qty, "price": 200.0, "transaction_time": order_id}

    def test_rebuild_stamps_the_newest_replayed_fill(self):
        """KILL-BACKUP-1111 rev 4: the ledger keeps the newest fill time it replayed."""
        fills = [
            {"symbol": "NVDA", "order_id": "1", "side": "buy", "qty": 1, "price": 200.0,
             "transaction_time": "2026-11-11T14:30:00Z"},
            {"symbol": "NVDA", "order_id": "2", "side": "buy", "qty": 1, "price": 201.0,
             "transaction_time": "2026-11-11T15:05:50Z"},
        ]
        coids = {"1": "QH-NVDA-b-1-a", "2": "QH-NVDA-b-2-a"}
        result = guard.sync_ledger(fills, [{"symbol": "NVDA", "qty": "2"}],
                                   coid_by_order_id=coids)
        self.assertTrue(result.get("healed"))
        stamp = guard.load_ledger().get("fills_through_utc")
        self.assertEqual(stamp, "2026-11-11T15:05:50+00:00")
        # an exact fill credited after the stamp is recorded by its source;
        # the next rebuild clears the record
        self.assertTrue(guard.apply_authorized_tier_fill_increase(
            "NVDA", "qhm", 1, 3, "qhm_add:9"))
        led = guard.load_ledger()
        self.assertEqual(led.get("credited_since_stamp"), ["qhm_add:9"])
        self.assertEqual(led.get("fills_through_utc"),
                         "2026-11-11T15:05:50+00:00")
        fills.append({"symbol": "NVDA", "order_id": "9", "side": "buy",
                      "qty": 1, "price": 202.0,
                      "transaction_time": "2026-11-11T16:00:00Z"})
        coids["9"] = "QH-NVDA-b-9-a"
        guard.sync_ledger(fills, [{"symbol": "NVDA", "qty": "3"}],
                          coid_by_order_id=coids)
        led = guard.load_ledger()
        self.assertEqual(led.get("credited_since_stamp"), [])
        self.assertIs(led.get("unkeyed_edit_since_stamp"), False)

    def test_negative_protected_replay_is_reclassified_without_changing_net(self):
        """A protected tier never persists as short when broker net is flat."""
        fills = [
            self._fill("1", "buy", 1),
            self._fill("2", "sell", 3),
            self._fill("3", "buy", 2),
        ]
        result = guard.sync_ledger(
            fills,
            [],
            coid_by_order_id={
                "1": "QH-NVDA-b-1-a",
                "2": "QH-NVDA-s-2-a",
                "3": "IN-NVDA-b-3-a",
            },
        )
        self.assertTrue(result["healed"])
        tiers = result["positions"]["NVDA"]["tiers"]
        self.assertEqual(tiers["qhm"]["qty"], 0.0)
        self.assertEqual(tiers["forever6"]["qty"], 0.0)
        self.assertEqual(tiers["intraday"]["qty"], 0.0)
        self.assertEqual(result["positions"]["NVDA"]["drift"], 0.0)

    def test_real_positive_protected_floor_reduction_still_refuses(self):
        """Normalizing impossible negatives must not permit a real floor shrink."""
        baseline = guard._empty_ledger()
        baseline["positions"]["NVDA"] = {
            "alpaca_net_qty": 2.0,
            "tiers": {tier: {"qty": 0.0, "avg_cost": 0.0, "last_fill_id": None}
                      for tier in guard._TIERS},
            "drift": 0.0,
        }
        baseline["positions"]["NVDA"]["tiers"]["qhm"]["qty"] = 2.0
        guard.save_ledger(baseline)
        result = guard.sync_ledger(
            [self._fill("1", "buy", 1)],
            [{"symbol": "NVDA", "qty": 1.0}],
            coid_by_order_id={"1": "QH-NVDA-b-1-a"},
        )
        self.assertFalse(result["healed"])
        self.assertEqual(result["shrink"]["NVDA/qhm"]["was"], 2.0)
        self.assertEqual(result["shrink"]["NVDA/qhm"]["would_be"], 1.0)


if __name__ == "__main__":
    unittest.main()
