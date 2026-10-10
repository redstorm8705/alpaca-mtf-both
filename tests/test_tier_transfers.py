#!/usr/bin/env python3
# ruff: noqa: E501
"""Tier-transfer journal + ownership-ledger replay of Day -> Swing hand-overs (CEO 2026-10-10;
execution/tier_transfers.py, ownership_guard.sync_ledger(transfers=...))."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from execution import ownership_guard as og
from execution import tier_transfers as tt


def _fill(sym, side, qty, price, ts, coid):
    return {"symbol": sym, "side": side, "qty": qty, "price": price, "transaction_time": ts,
            "order_id": f"o-{sym}-{ts}", "client_order_id": coid}


class _TmpState:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.p = [mock.patch.object(tt, "_PATH", d / "tier_transfers.json"),
                  mock.patch.object(og, "_LEDGER_PATH", d / "ledger.json"),
                  mock.patch.object(og, "_LEDGER_BAK_PATH", d / "ledger.bak.json"),
                  mock.patch.object(og, "_LEDGER_LOCK_PATH", d / ".ledger.lock"),
                  mock.patch.object(og, "_LEDGER_MIGRATION_LOCK_PATH", d / ".ledger.migration.lock"),
                  mock.patch.object(og, "_HEAL_CONFIRM_PATH", d / "heal.json")]
        for x in self.p:
            x.start()
        self.dir = d
        return self

    def __exit__(self, *a):
        for x in reversed(self.p):
            x.stop()
        self.tmp.cleanup()


def _tiers(led, sym):
    return {t: led["positions"][sym]["tiers"][t]["qty"] for t in ("daytrade", "intraday", "qhm", "forever6")}


class Journal(unittest.TestCase):
    def test_record_is_idempotent_and_loads_sorted(self):
        with _TmpState():
            t2 = datetime(2026, 10, 9, 20, 5, tzinfo=timezone.utc)
            t1 = datetime(2026, 10, 7, 14, 18, tzinfo=timezone.utc)
            self.assertTrue(tt.record_transfer("B", "msft", 1, "daytrade", "intraday", 500.0, when=t2))
            self.assertTrue(tt.record_transfer("A", "AAPL", 4, "daytrade", "intraday", 335.24, when=t1))
            self.assertTrue(tt.record_transfer("A", "AAPL", 9, "daytrade", "intraday", 1.0, when=t2))  # no-op
            rows = tt.load_transfers()
            self.assertEqual([r["ref"] for r in rows], ["A", "B"])
            self.assertEqual((rows[0]["qty"], rows[1]["symbol"]), (4.0, "MSFT"))

    def test_protected_or_invalid_transfers_are_rejected(self):
        with _TmpState():
            self.assertFalse(tt.record_transfer("Q", "NVDA", 1, "intraday", "qhm", 200.0))
            self.assertFalse(tt.record_transfer("Z", "NVDA", 0, "daytrade", "intraday", 200.0))
            self.assertFalse(tt.record_transfer("P", "NVDA", 1, "daytrade", "intraday", 0.0))
            self.assertEqual(tt.load_transfers(), [])

    def test_unreadable_journal_is_none_and_never_overwritten(self):
        with _TmpState() as s:
            (s.dir / "tier_transfers.json").write_text("{broken")
            self.assertIsNone(tt.load_transfers())
            self.assertFalse(tt.record_transfer("A", "AAPL", 4, "daytrade", "intraday", 335.24))
            self.assertEqual((s.dir / "tier_transfers.json").read_text(), "{broken")


class LedgerReplay(unittest.TestCase):
    def test_promoted_long_moves_to_swing_and_swing_sell_nets_to_zero(self):
        # AAPL 10/07-10/09: Day bought 4 (DT-), Swing adopted, then an untagged stop sold 4.
        fills = [_fill("AAPL", "buy", 4, 335.24, "2026-10-07T14:12:14.113778Z", "DT-AAPL-b-1-x"),
                 _fill("AAPL", "sell", 4, 332.09, "2026-10-09T13:36:19.918916Z", None)]
        tr = [{"ref": "r1", "symbol": "AAPL", "qty": 4.0, "from_tier": "daytrade", "to_tier": "intraday",
               "price": 335.24, "ts_utc": "2026-10-07T14:18:06.708000Z"}]
        with _TmpState():
            before = og.sync_ledger(fills, [], transfers=None)
            self.assertEqual(_tiers(before, "AAPL")["daytrade"], 4.0)        # the crossed row today
            self.assertEqual(_tiers(before, "AAPL")["intraday"], -4.0)
            led = og.sync_ledger(fills, [], transfers=tr)
            self.assertEqual(_tiers(led, "AAPL"), {"daytrade": 0.0, "intraday": 0.0, "qhm": 0.0, "forever6": 0.0})

    def test_open_promoted_long_carries_take_over_cost(self):
        fills = [_fill("MSFT", "buy", 2, 500.0, "2026-10-09T15:00:00.000000Z", "DT-MSFT-b-1-y")]
        tr = [{"ref": "r2", "symbol": "MSFT", "qty": 2.0, "from_tier": "daytrade", "to_tier": "intraday",
               "price": 510.0, "ts_utc": "2026-10-09T20:04:00.000000Z"}]
        with _TmpState():
            led = og.sync_ledger(fills, [{"symbol": "MSFT", "qty": "2"}], transfers=tr)
            t = led["positions"]["MSFT"]["tiers"]
            self.assertEqual((t["daytrade"]["qty"], t["intraday"]["qty"]), (0.0, 2.0))
            self.assertEqual(t["intraday"]["avg_cost"], 510.0)
            self.assertEqual(led["positions"]["MSFT"]["drift"], 0.0)

    def test_adopted_short_moves_to_swing(self):
        fills = [_fill("EWY", "sell_short", 1, 181.46, "2026-10-07T13:36:26.871711Z", "DT-EWY-s-1-z")]
        tr = [{"ref": "r3", "symbol": "EWY", "qty": -1.0, "from_tier": "daytrade", "to_tier": "intraday",
               "price": 181.46, "ts_utc": "2026-10-07T14:10:42.028000Z"}]
        with _TmpState():
            led = og.sync_ledger(fills, [{"symbol": "EWY", "qty": "-1"}], transfers=tr)
            self.assertEqual((_tiers(led, "EWY")["daytrade"], _tiers(led, "EWY")["intraday"]), (0.0, -1.0))

    def test_transfer_never_creates_shares(self):
        fills = [_fill("AMD", "buy", 1, 100.0, "2026-10-09T15:00:00.000000Z", "DT-AMD-b-1-w")]
        tr = [{"ref": "r4", "symbol": "AMD", "qty": 3.0, "from_tier": "daytrade", "to_tier": "intraday",
               "price": 101.0, "ts_utc": "2026-10-09T20:04:00.000000Z"},
              {"ref": "r5", "symbol": "AMD", "qty": 1.0, "from_tier": "daytrade", "to_tier": "intraday",
               "price": 101.0, "ts_utc": "2026-10-09T14:00:00.000000Z"}]           # before the buy
        with _TmpState():
            led = og.sync_ledger(fills, [{"symbol": "AMD", "qty": "1"}], transfers=tr)
            self.assertEqual((_tiers(led, "AMD")["daytrade"], _tiers(led, "AMD")["intraday"]), (1.0, 0.0))

    def test_protected_tier_transfer_is_ignored_by_the_replay(self):
        fills = [_fill("NVDA", "buy", 1, 200.0, "2026-10-09T15:00:00.000000Z", "QH-NVDA-b-1-v")]
        tr = [{"ref": "r6", "symbol": "NVDA", "qty": 1.0, "from_tier": "qhm", "to_tier": "intraday",
               "price": 200.0, "ts_utc": "2026-10-09T20:04:00.000000Z"}]
        with _TmpState():
            led = og.sync_ledger(fills, [{"symbol": "NVDA", "qty": "1"}], transfers=tr)
            self.assertEqual(_tiers(led, "NVDA")["qhm"], 1.0)


class PromotionRecordsTransfer(unittest.TestCase):
    def test_adopted_hand_off_records_the_transfer(self):
        from execution import day_promotion as dp
        with _TmpState():
            h = {"symbol": "AAPL", "qty": 4, "take_over_mark": 340.42, "adopted_ts": "2026-10-12T13:04:00-07:00"}
            self.assertTrue(dp._record_ledger_transfer("DT-AAPL-b-1-x", h))
            self.assertTrue(dp._record_ledger_transfer("DT-AAPL-b-1-x", h))     # idempotent
            rows = tt.load_transfers()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]["qty"], rows[0]["price"], rows[0]["ts_utc"]),
                             (4.0, 340.42, "2026-10-12T20:04:00.000000Z"))

    def test_no_hand_over_time_records_nothing(self):
        from execution import day_promotion as dp
        with _TmpState():
            self.assertFalse(dp._record_ledger_transfer("X", {"symbol": "AAPL", "qty": 4, "take_over_mark": 340.0}))
            self.assertEqual(tt.load_transfers(), [])

    def test_nan_transfer_is_skipped_by_the_replay(self):
        fills = [_fill("AMD", "buy", 1, 100.0, "2026-10-09T15:00:00.000000Z", "DT-AMD-b-1-w")]
        tr = [{"ref": "n", "symbol": "AMD", "qty": float("nan"), "from_tier": "daytrade", "to_tier": "intraday",
               "price": 101.0, "ts_utc": "2026-10-09T20:04:00.000000Z"}]
        with _TmpState():
            led = og.sync_ledger(fills, [{"symbol": "AMD", "qty": "1"}], transfers=tr)
            self.assertEqual(_tiers(led, "AMD")["daytrade"], 1.0)


if __name__ == "__main__":
    unittest.main()
