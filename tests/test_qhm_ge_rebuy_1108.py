# ruff: noqa: E501
"""GE-REBUY-1108 (design record logs/design_records/qhm_ge_rebuy_bug_2026-11-08.md).

QHM must not buy back a hold that left the account without its own stop firing; a not-yet-time-stamped fill is a
retry, not an unproven close; Alpaca timestamps with any number of fractional digits parse on Python 3.10.
Every broker and network call is mocked: these tests can never reach Alpaca."""
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock, patch

from execution import manual_sale_proof as msp
from execution.quarterly_hold_manager import HoldState
from tests.test_qhm_owner_sale_autoheal import _BOT, _T0, _manager, _pos, _row


class NoBuyBackAfterExternalClose(TestCase):
    def _mgr(self, prev, nbf):
        m = _manager()
        m._thesis_config = {"GE": {"not_before_date": nbf}}
        m._positions = {"GE": prev}
        return m

    def test_external_close_is_not_re_added(self):
        """GE 11-04: an unproven outside sale closed the hold as external_close_detected; QHM must not re-add it."""
        prev = _pos(symbol="GE", state=HoldState.CLOSED, closed_reason="external_close_detected",
                    updated_at="2026-11-04T13:17:06-05:00")
        m = self._mgr(prev, "2026-07-25")
        m.add_candidate("GE", 0.2)
        self.assertIs(m._positions["GE"], prev)
        m._save_state.assert_not_called()

    def test_external_close_re_dated_pick_is_re_added(self):
        prev = _pos(symbol="GE", state=HoldState.CLOSED, closed_reason="external_close_detected",
                    updated_at="2026-11-04T13:17:06-05:00")
        m = self._mgr(prev, "2026-11-20")
        m.add_candidate("GE", 0.2)
        self.assertEqual(m._positions["GE"].state, HoldState.PENDING_ENTRY)

    def test_own_stop_out_is_locked_until_earnings(self):
        """QHM's own stop-out is NOT treated as an owner sale (no re-date-only block). Since QHM-REENTRY-1108 it is
        locked instead: no re-add until its next earnings report has passed (or a Rung A dip release)."""
        from datetime import date

        from execution.quarterly_hold_manager import ET
        prev = _pos(symbol="GE", state=HoldState.CLOSED, closed_reason="qhm_stop_out",
                    updated_at="2026-11-04T13:17:06-05:00")
        m = self._mgr(prev, "2026-07-25")
        m._reentry_earnings = Mock(return_value=(date(2026, 12, 15), "finnhub_symbol", "amc"))  # no provider call
        with patch("trade_logger.log_event"):
            m.add_candidate("GE", 0.2)
            self.assertIs(m._positions["GE"], prev)                    # locked: report not passed, no trigger
            m._now_et.return_value = datetime(2026, 12, 16, 11, 0, tzinfo=ET)   # the session after the report
            m.add_candidate("GE", 0.2)
        self.assertEqual(m._positions["GE"].state, HoldState.PENDING_ENTRY)
        self.assertEqual(m._positions["GE"].reentry_release, "earnings_passed")


class FillTimeParsing(TestCase):
    def test_any_fraction_length_parses(self):
        for raw, micro in (("2026-11-04T18:10:36.47355Z", 473550), ("2026-11-04T18:10:37.1Z", 100000),
                           ("2026-11-04T18:10:37.106777Z", 106777), ("2026-11-04T18:10:37.123456789Z", 123456),
                           ("2026-11-04T18:10:37Z", 0), ("2026-11-04T18:10:37.5+00:00", 500000)):
            ts = msp._parse_ts(raw)
            self.assertIsNotNone(ts, raw)
            self.assertEqual(ts.microsecond, micro, raw)
            self.assertEqual(ts.tzinfo, timezone.utc)

    def test_naive_and_garbage_stay_unparsed(self):
        for raw in (None, "", "not a time", "2026-11-04T18:10:37.5"):
            self.assertIsNone(msp._parse_ts(raw), raw)

    def test_five_digit_fill_time_proves_the_sale(self):
        rows = [_row(filled_at="2026-10-16T17:07:36.47355Z"), _BOT]
        proof = msp.evaluate_owner_sale(rows, 1, 0, _T0.isoformat())
        self.assertTrue(proof.proven, proof.reason)


class MissingFillTimeIsRetried(TestCase):
    def test_missing_fill_time_is_a_retry(self):
        rows = [_row(filled_at=None), _BOT]
        proof = msp.evaluate_owner_sale(rows, 1, 0, _T0.isoformat())
        self.assertFalse(proof.proven)
        self.assertTrue(proof.retry)

    def test_other_failures_are_not_retries(self):
        rows = [_row(source="access_key"), _BOT]
        proof = msp.evaluate_owner_sale(rows, 1, 0, _T0.isoformat())
        self.assertFalse(proof.proven)
        self.assertFalse(proof.retry)

    def test_flat_hold_with_untimed_fill_stays_open_and_retries(self):
        """The QHM flat-hold check keeps the hold open on a retryable proof (same path as an unreadable history)."""
        m = _manager(position=None)
        p = _pos(symbol="GE", state=HoldState.ACTIVE, stop_order_id=None)   # no broker stop lookup in this test
        m._positions = {"GE": p}
        unproven = msp.SaleProof(False, "order x has fills but no fill time", retry=True)
        with patch.object(msp, "prove_owner_sale", return_value=unproven), \
                patch.object(m, "_now_et", return_value=datetime(2026, 11, 4, 13, 17, tzinfo=timezone.utc)):
            closed = m._detect_external_close(p, _result())
        self.assertFalse(closed)
        self.assertNotEqual(p.state, HoldState.CLOSED)


def _result():
    from execution.quarterly_hold_manager import ReconcileResult
    return ReconcileResult()
