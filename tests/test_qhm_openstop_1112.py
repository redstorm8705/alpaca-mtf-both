# ruff: noqa: E501
"""QHM-OPENSTOP-1112 — a missing QHM stop is re-placed in pre-market and the 9:30-10:00 ET opening window too (design
record logs/design_records/qhm_open_stop_rearm_2026-11-12.md). The QHM manager and alerts are mocked; nothing reaches
the broker or Slack."""
import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

import tests  # noqa: F401  — test guard
from execution.quarterly_hold_manager import HoldState
from strategy import run_cycle as rc


def _qhm(n=1, pending=()):
    positions = {s: SimpleNamespace(state=HoldState.PENDING_STOP_REPLACE) for s in pending}
    positions["GE"] = SimpleNamespace(state=HoldState.ACTIVE)
    return SimpleNamespace(resubmit_pending_stops=mock.Mock(return_value=n), _positions=positions)


class Helper(unittest.TestCase):
    def setUp(self):
        rc._QHM_OPEN_PAGED.clear()

    def test_rearms_through_qhm_and_returns_the_count(self):
        main = SimpleNamespace(qhm=_qhm(n=2, pending=("NVDA",)))
        self.assertEqual(rc._rearm_qhm_stops(main, "pre-market"), 2)
        main.qhm.resubmit_pending_stops.assert_called_once_with()

    def test_no_qhm_is_a_no_op(self):
        self.assertEqual(rc._rearm_qhm_stops(SimpleNamespace(qhm=None), "pre-market"), 0)
        self.assertEqual(rc._rearm_qhm_stops(SimpleNamespace(), "pre-market"), 0)

    def test_an_error_never_breaks_the_cycle(self):
        q = _qhm(pending=("NVDA",))
        q.resubmit_pending_stops.side_effect = RuntimeError("broker down")
        self.assertEqual(rc._rearm_qhm_stops(SimpleNamespace(qhm=q), "opening window", page_unfixed=True), 0)

    def test_opening_window_pages_once_per_symbol_per_day(self):
        main = SimpleNamespace(qhm=_qhm(n=0, pending=("NVDA",)))
        with mock.patch("alerts.notify") as n:
            rc._rearm_qhm_stops(main, "opening window", page_unfixed=True)
            rc._rearm_qhm_stops(main, "opening window", page_unfixed=True)
        self.assertEqual(n.call_count, 1)
        self.assertEqual(n.call_args[0][:2], ("action", "NVDA"))

    def test_pre_market_and_closed_never_page(self):
        main = SimpleNamespace(qhm=_qhm(n=0, pending=("NVDA",)))
        with mock.patch("alerts.notify") as n:
            rc._rearm_qhm_stops(main, "pre-market")
            rc._rearm_qhm_stops(main, "market closed")
        n.assert_not_called()

    def test_no_broker_read_when_nothing_is_pending(self):
        main = SimpleNamespace(qhm=_qhm(n=0))
        self.assertEqual(rc._rearm_qhm_stops(main, "pre-market"), 0)
        main.qhm.resubmit_pending_stops.assert_not_called()

    def test_no_page_when_every_stop_is_back(self):
        q = _qhm(n=1, pending=("NVDA",))

        def _fixed():
            q._positions["NVDA"].state = HoldState.ACTIVE
            return 1
        q.resubmit_pending_stops.side_effect = _fixed
        with mock.patch("alerts.notify") as n:
            rc._rearm_qhm_stops(SimpleNamespace(qhm=q), "opening window", page_unfixed=True)
        n.assert_not_called()


class CallSites(unittest.TestCase):
    """Order in run_cycle (board + GAI): pre-market rearm after the overnight GTC cancel; opening-window rearm after
    check_exits and the fill reconcile, and also when those raise; the closed-market branch uses the same helper."""
    src = inspect.getsource(rc.run_cycle)

    def test_pre_market_rearm_follows_the_gtc_cancel(self):
        pre = self.src.index('if tod_phase == "premarket":')
        cancel = self.src.index("_cancel_and_reconcile_gtc_stops(tracker, risk)", pre)
        rearm = self.src.index('_rearm_qhm_stops(_main, "pre-market")', pre)
        self.assertLess(cancel, rearm)

    def test_opening_window_rearm_after_exits_and_in_both_paths(self):
        op = self.src.index('if tod_phase == "opening":')
        end = self.src.index("return", self.src.index("_touch_cycle_ts()", op))
        block = self.src[op:end]
        self.assertLess(block.index("_main.check_exits("), block.index('_rearm_qhm_stops(_main, "opening window", page_unfixed=True)'))
        self.assertEqual(block.count('_rearm_qhm_stops(_main, "opening window", page_unfixed=True)'), 2)   # except + finally
        self.assertIn("finally:", block)
        self.assertLess(block.index("_run_fill_recon("), block.rindex("_rearm_qhm_stops("))

    def test_closed_market_uses_the_helper(self):
        self.assertIn('_rearm_qhm_stops(_main, "market closed")', self.src)
        self.assertEqual(self.src.count("resubmit_pending_stops()"), 0)   # only through the helper


if __name__ == "__main__":
    unittest.main()
