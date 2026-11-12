#!/usr/bin/env python3
# ruff: noqa: E501
"""KILL-BACKUP-1111 (owner-approved; logs/design_records/kill_backup_measure_2026-11-11.md).

Owner 2026-11-11: the kill switch may stop new entries only on a measured -7% Day+Swing loss, never on a measurement
glitch. When the exact join fails, the backup measures from Alpaca fills + the ledger checkpoint + the fill journal.
These tests pin the board's and GAI's cases. No network: positions, fills, the ledger and bars are mocked.
"""
import unittest
from datetime import datetime, timezone
from unittest import mock

import config
from execution import risk_manager as rm

CKPT = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
BEFORE = "2026-11-11T14:30:00Z"
AFTER = "2026-11-11T15:10:00Z"


def _pos(sym, qty, cur, prior):
    return {"symbol": sym, "qty": str(qty), "current_price": str(cur), "lastday_price": str(prior)}


def _ckpt(claims, ts=CKPT, from_file=True):
    return mock.patch.object(rm, "_ledger_checkpoint", return_value=(claims, ts, from_file))


class BackupMeasure(unittest.TestCase):
    def setUp(self):
        rm._PRIOR_CLOSE_CACHE.clear()
        rm._PRIOR_CLOSE_MISS.clear()

    def _b(self, fills, ts, positions, claims, ckpt_ts=CKPT, prior=None, oids=None, credited=frozenset()):
        rm._LAST_GOOD_CHECKPOINT["credited"] = credited
        with _ckpt(claims, ckpt_ts), mock.patch.object(rm, "_prior_session_close", return_value=prior):
            return rm._backup_trading_pnl(tuple(fills), tuple(ts), positions,
                                          None if oids is None else tuple(oids))[0]

    def test_qhm_buy_unbooked_never_moves_b(self):
        """11-06 shape: QHM holds 1 NVDA (checkpoint), buys 1 more after it; NVDA +$10 from the prior close. The QHM
        shares stay out of B; the Day loss elsewhere counts in full."""
        fills = [("NVDA", "buy", 1, 185.0, "qhm"), ("AMD", "buy", 2, 250.0, "intraday")]
        ts = [AFTER, AFTER]
        positions = [_pos("NVDA", 2, 190.0, 180.0), _pos("AMD", 2, 240.0, 245.0)]
        claims = {"NVDA": (0.0, 1.0)}
        b = self._b(fills, ts, positions, claims)
        # AMD: open (240-245)*2 = -10, fill 2*(245-250) = -10  -> -20 ; NVDA: a=0, p=2, u=0 -> 0
        self.assertEqual(b, -20.0)

    def test_drift_row_cannot_hide_a_day_loss(self):
        """Board round 2: QHM holds 50, buys 10 (unbooked); NVDA +$15. A $150 Day loss elsewhere must not be offset."""
        fills = [("NVDA", "buy", 10, 200.0, "qhm"), ("AMD", "sell", 10, 235.0, "intraday")]
        positions = [_pos("NVDA", 60, 215.0, 200.0)]
        claims = {"NVDA": (0.0, 50.0), "AMD": (10.0, 0.0)}
        with _ckpt(claims), mock.patch.object(rm, "_prior_session_close", return_value=250.0):
            b, _terms = rm._backup_trading_pnl(tuple(fills), (AFTER, AFTER), positions)
        self.assertEqual(b, -150.0)   # AMD sold 10 at 235 vs prior 250; NVDA's QHM gain excluded

    def test_unbooked_active_shares_count_only_when_losing(self):
        """A Day buy the journal cannot place (fill before the checkpoint but missing from the claims): one-sided."""
        positions = [_pos("AMD", 5, 240.0, 250.0)]
        b = self._b([], [], positions, {"AMD": (0.0, 0.0)})
        self.assertEqual(b, -50.0)                       # loss counts
        positions = [_pos("AMD", 5, 260.0, 250.0)]
        self.assertEqual(self._b([], [], positions, {"AMD": (0.0, 0.0)}), 0.0)   # gain does not

    def test_day_short_against_qhm_long_in_a_held_symbol(self):
        """QHM long 10 GOOGL, Day short 4 (ledger knows it); broker net 6; GOOGL +$30. Day loss -$120 counted."""
        positions = [_pos("GOOGL", 6, 330.0, 300.0)]
        claims = {"GOOGL": (-4.0, 10.0)}
        self.assertEqual(self._b([], [], positions, claims), -120.0)

    def test_day_short_netting_qhm_long_to_flat_fails_closed(self):
        """QHM long 10, Day short 10: Alpaca reports no GOOGL position, so no price -> the backup cannot measure."""
        claims = {"GOOGL": (-10.0, 10.0)}
        self.assertIsNone(self._b([], [], [], claims))

    def test_sold_out_symbol_open_leg_zero_fills_priced_from_prior_close(self):
        fills = [("QQQM", "sell", 1, 283.59, "intraday")]
        b = self._b(fills, [AFTER], [], {"QQQM": (1.0, 0.0)}, prior=277.87)
        self.assertEqual(b, round(283.59 - 277.87, 2))

    def test_protected_fill_excluded_and_tag_spelling(self):
        fills = [("GE", "sell", 1, 300.0, "forever6"), ("GE", "buy", 1, 300.0, "qhm")]
        positions = [_pos("GE", 1, 310.0, 305.0)]
        self.assertEqual(self._b(fills, [AFTER, AFTER], positions, {"GE": (0.0, 1.0)}), 0.0)
        self.assertEqual(rm._PROTECTED_FILL_TIERS, frozenset({"qhm", "forever6"}))

    def test_no_checkpoint_fails_closed(self):
        """Cold review 2026-11-11: with no checkpoint the broker's net can mix a QHM long with a Day short (QHM 10
        long, Day short 4 -> net 6, up $30 -> B would read 0 against a true -$120). No checkpoint -> None (the old
        entries-only block)."""
        positions = [_pos("GOOGL", 6, 330.0, 300.0)]
        fills = [("GOOGL", "sell_short", 4, 300.0, "intraday")]
        self.assertIsNone(self._b(fills, [AFTER], positions, {}, ckpt_ts=None))

    def test_missing_prior_close_fails_closed(self):
        positions = [{"symbol": "ZZZ", "qty": "1", "current_price": "10", "lastday_price": None}]
        self.assertIsNone(self._b([], [], positions, {"ZZZ": (1.0, 0.0)}, prior=None))

    def test_fill_times_with_any_fraction_length(self):
        from execution.ownership_guard import _parse_fill_time
        for raw in ("2026-11-11T15:10:00Z", "2026-11-11T15:10:00.5Z", "2026-11-11T15:10:00.123Z",
                    "2026-11-11T15:10:00.12345Z", "2026-11-11T15:10:00.123456789Z", "2026-11-11T15:10:00.123+00:00"):
            self.assertIsNotNone(_parse_fill_time(raw), raw)
        self.assertGreater(_parse_fill_time("2026-11-11T15:10:01.5Z"), _parse_fill_time("2026-11-11T15:10:01Z"))
        self.assertIsNone(_parse_fill_time("garbage"))
        self.assertTrue(rm._fill_after("2026-11-11T15:10:00.123456789Z", CKPT))

    def test_same_day_round_trip_needs_no_prior_close(self):
        fills = [("TSLA", "buy", 5, 400.0, "intraday"), ("TSLA", "sell", 5, 398.0, "intraday")]
        self.assertEqual(self._b(fills, [AFTER, AFTER], [], {}, prior=None), -10.0)

    def test_ledger_without_stamp_fails_closed(self):
        """Cold review 2026-11-11: an unstamped ledger (claims QHM 10, then QHM sold 10 and Day bought 10, down $10)
        would read 0 against a true -$100. No stamp -> None until a rev-4 rebuild writes one."""
        positions = [_pos("NVDA", 10, 180.0, 190.0)]
        fills = [("NVDA", "sell", 10, 190.0, "qhm"), ("NVDA", "buy", 10, 190.0, "intraday")]
        self.assertIsNone(self._b(fills, [AFTER, AFTER], positions, {"NVDA": (0.0, 10.0)}, ckpt_ts=None))

    def test_fill_credited_after_stamp_is_not_counted_twice(self):
        """Adversarial review 2026-11-11: QHM's dip-add is credited to the claim at once (claim 2 -> 3, order Q1),
        after the stamp. The journal must skip Q1, or u = -1 charges a QHM share as a loss."""
        positions = [_pos("NVDA", 3, 190.0, 180.0)]
        fills = [("NVDA", "buy", 1, 185.0, "qhm")]
        self.assertEqual(self._b(fills, [AFTER], positions, {"NVDA": (0.0, 3.0)}, oids=["Q1"],
                                 credited=frozenset({"Q1"})), 0.0)
        # without the credit record the same input double-counts (the bug the record prevents)
        self.assertEqual(self._b(fills, [AFTER], positions, {"NVDA": (0.0, 3.0)}, oids=["Q1"]), -10.0)

    def test_day_fill_with_same_symbol_credit_still_counts(self):
        """Day 10 + QHM 2 in one symbol, QHM add credited (Q1); a Day buy (D1) after the stamp still counts."""
        positions = [_pos("NVDA", 14, 190.0, 180.0)]
        fills = [("NVDA", "buy", 1, 185.0, "qhm"), ("NVDA", "buy", 1, 185.0, "intraday")]
        b = self._b(fills, [AFTER, AFTER], positions, {"NVDA": (10.0, 3.0)}, oids=["Q1", "D1"],
                    credited=frozenset({"Q1"}))
        self.assertEqual(b, 110.0 + (180.0 - 185.0))   # Day 11 shares x +10, the D1 fill repriced: -5

    def test_untagged_sale_of_qhm_shares_fails_closed(self):
        """Cold review 2026-11-11: the owner sells 5 QHM NVDA in the app (no tag) at $210, NVDA now $190, and a Day
        AMD lot is down $100. Counting the untagged sale as Day would read 0; the backup does not measure."""
        positions = [_pos("NVDA", 5, 190.0, 200.0), _pos("AMD", 10, 90.0, 100.0)]
        fills = [("NVDA", "sell", 5, 210.0, None)]
        self.assertIsNone(self._b(fills, [AFTER], positions, {"NVDA": (0.0, 10.0), "AMD": (10.0, 0.0)}, oids=["o1"]))

    def test_untagged_fill_without_protected_shares_still_counts(self):
        positions = [_pos("AMD", 11, 90.0, 100.0)]
        fills = [("AMD", "buy", 1, 95.0, None)]
        self.assertEqual(self._b(fills, [AFTER], positions, {"AMD": (10.0, 0.0)}, oids=["o1"]), -110.0 + 5.0)

    def test_fill_id_count_mismatch_fails_closed(self):
        self.assertIsNone(self._b([("A", "buy", 1, 1.0, "intraday")], [AFTER], [], {}, oids=[]))

    def test_fill_time_mismatch_fails_closed(self):
        self.assertIsNone(self._b([("A", "buy", 1, 1.0, "intraday")], [], [], {}))


class CheckpointFallback(unittest.TestCase):
    def test_unreadable_ledger_uses_last_good_checkpoint(self):
        rm._LAST_GOOD_CHECKPOINT.clear()
        rm._LAST_GOOD_CHECKPOINT.update(claims={"NVDA": (0.0, 2.0)}, ts=CKPT)
        with mock.patch("execution.ownership_guard.load_ledger", side_effect=ValueError("corrupt")), \
                self.assertLogs(rm.logger, level="CRITICAL") as logs:
            claims, ts, from_file = rm._ledger_checkpoint()
        # claims kept, but no checkpoint time: fills since that read may predate today's journal (cold review)
        self.assertEqual((claims, ts, from_file), ({"NVDA": (0.0, 2.0)}, None, False))
        self.assertIn("CRITICAL_LEDGER_UNREADABLE", logs.output[0])

    def test_checkpoint_reads_claims_not_net_or_drift(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        led = {"last_reconciled_utc": None, "fills_through_utc": now.isoformat(), "positions": {"NVDA": {
            "alpaca_net_qty": 60.0, "drift": 10.0,
            "tiers": {"day": {"qty": 0.0}, "swing": {"qty": 0.0}, "qhm": {"qty": 50.0}, "forever_6": {"qty": 0.0}}}}}
        with mock.patch("execution.ownership_guard.load_ledger", return_value=led):
            claims, ts, from_file = rm._ledger_checkpoint()
        self.assertEqual(claims["NVDA"], (0.0, 50.0))
        self.assertTrue(from_file)
        self.assertEqual(ts, now)

    def _read(self, **extra):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        led = {"fills_through_utc": now.isoformat(), "positions": {}, **extra}
        with mock.patch("execution.ownership_guard.load_ledger", return_value=led):
            return rm._ledger_checkpoint()[1]

    def test_stamp_before_today_is_no_checkpoint(self):
        """The journal holds today's fills only: a stamp from an earlier day leaves a gap -> no checkpoint."""
        with mock.patch("execution.ownership_guard.load_ledger", return_value={
                "fills_through_utc": "2026-01-02T15:00:00+00:00", "positions": {}}):
            self.assertIsNone(rm._ledger_checkpoint()[1])

    def test_unkeyed_edit_after_stamp_is_no_checkpoint(self):
        self.assertIsNone(self._read(unkeyed_edit_since_stamp=True))
        self.assertIsNotNone(self._read(unkeyed_edit_since_stamp=False))

    def test_credited_sources_parse_to_order_ids(self):
        self._read(credited_since_stamp=["qhm_registered_stop:abc-1", "f6_trim:def-2", "ghi-3"])
        self.assertEqual(rm._LAST_GOOD_CHECKPOINT["credited"], frozenset({"abc-1", "def-2", "ghi-3"}))


class KillDecision(unittest.TestCase):
    def _risk(self):
        with mock.patch.object(rm, "_load_kill_state", return_value={}):
            return rm.RiskManager(2500.0, daily_start_value=2500.0)

    def _check(self, backup_value, fills_ok=True, positions=()):
        r = self._risk()
        saved = []
        with mock.patch.object(config, "OWNERSHIP_GUARD_ENFORCE", True), \
                mock.patch.object(rm.RiskManager, "_intraday_trading_pnl", return_value=(0.0, False)), \
                mock.patch.object(rm, "_today_fills", return_value=((), fills_ok)), \
                mock.patch.object(rm.RiskManager, "_backup_positions", return_value=list(positions)), \
                mock.patch.object(rm, "_backup_trading_pnl", return_value=(backup_value, [])), \
                mock.patch.object(rm, "_backup_escalate"), \
                mock.patch.object(rm, "_save_kill_state", side_effect=lambda **k: saved.append(k)), \
                mock.patch.object(rm, "_publish_kill_snapshot"):
            return r, r.check_kill_switch(), saved

    def test_glitch_without_loss_keeps_trading(self):
        r, killed, saved = self._check(-20.0)
        self.assertFalse(killed)
        self.assertEqual(saved, [])

    def test_backup_trip_latches_with_control_fault(self):
        r, killed, saved = self._check(-0.08 * 2500.0)
        self.assertTrue(killed)
        self.assertTrue(r.killed)
        self.assertEqual(saved[0]["cause"], rm.KillVerdictCause.CONTROL_FAULT)   # never unlocks the F6 bypass
        self.assertFalse(saved[0]["attribution_exact"])

    def test_alpaca_unreadable_still_blocks(self):
        with self.assertRaises(RuntimeError):
            self._check(None, fills_ok=False)

    def test_unpriceable_backup_still_blocks(self):
        with self.assertRaises(RuntimeError):
            self._check(None)


class Escalation(unittest.TestCase):
    def test_pages_once_after_a_rebuild_that_did_not_repair(self):
        rm._BACKUP_STATE.update(day="", cycles=0, first=None, paged=False)
        import os
        import tempfile
        from pathlib import Path
        import alerts
        from execution import ownership_guard as og
        tmp = Path(tempfile.mkdtemp()) / "ledger.json"
        tmp.write_text("{}")
        os.utime(tmp, (946684800, 946684800))                       # rewritten in 2000: before the episode
        with mock.patch.object(og, "_LEDGER_PATH", tmp), mock.patch.object(alerts, "notify") as n:
            rm._backup_escalate("exact_join_unresolved", -5.0, [])
            n.assert_not_called()                                   # no rebuild since the episode began: no page
            os.utime(tmp, (4102444800, 4102444800))                 # the ledger file rewritten (a rebuild)
            rm._backup_escalate("exact_join_unresolved", -5.0, [])
            rm._backup_escalate("exact_join_unresolved", -5.0, [])
        self.assertEqual(n.call_count, 1)
        self.assertEqual(rm._BACKUP_STATE["cycles"], 3)
        rm._backup_episode_end()                                    # exact measure repaired: the episode ends
        with mock.patch.object(og, "_LEDGER_PATH", tmp), mock.patch.object(alerts, "notify") as n2:
            rm._backup_escalate("exact_join_unresolved", -5.0, [])  # a new episode, rebuilt ledger newer: pages
        self.assertEqual(n2.call_count, 1)


if __name__ == "__main__":
    unittest.main()
