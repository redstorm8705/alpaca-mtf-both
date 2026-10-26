import unittest
from types import SimpleNamespace

from main import _startup_managed_position_sets
from execution import swing_breakout_manager as swing_manager


def _tracker(**statuses):
    return SimpleNamespace(
        open_trades={symbol: {"status": status} for symbol, status in statuses.items()}
    )


class StartupManagedPositionTruthTests(unittest.TestCase):
    def test_dedicated_swing_and_day_positions_are_managed_outside_legacy_tracker(self):
        managed, unmanaged, stale = _startup_managed_position_sets(
            _tracker(),
            {"AAPL", "MSFT", "TSLA"},
            {"TSLA"},
            {"AAPL", "MSFT"},
        )

        self.assertEqual(managed, {"AAPL", "MSFT", "TSLA"})
        self.assertEqual(unmanaged, set())
        self.assertEqual(stale, set())

    def test_real_broker_orphan_remains_unmanaged(self):
        managed, unmanaged, stale = _startup_managed_position_sets(
            _tracker(), {"ORPHAN"}, set(), set()
        )

        self.assertEqual(managed, set())
        self.assertEqual(unmanaged, {"ORPHAN"})
        self.assertEqual(stale, set())

    def test_stale_legacy_tracker_row_remains_visible(self):
        managed, unmanaged, stale = _startup_managed_position_sets(
            _tracker(AAPL="open", OLD="open", CLOSED="closed"),
            {"AAPL"},
            set(),
            set(),
        )

        self.assertEqual(managed, {"AAPL", "OLD"})
        self.assertEqual(unmanaged, set())
        self.assertEqual(stale, {"OLD"})

    def test_stale_dedicated_state_is_not_misreported_by_legacy_check(self):
        managed, unmanaged, stale = _startup_managed_position_sets(
            _tracker(), set(), {"DAY_STALE"}, {"SWING_STALE"}
        )

        self.assertEqual(managed, set())
        self.assertEqual(unmanaged, set())
        self.assertEqual(stale, set())

    def test_excluded_qhm_legacy_row_preserves_existing_behavior(self):
        managed, unmanaged, stale = _startup_managed_position_sets(
            _tracker(GE="open"), set(), set(), set(), {"GE"}
        )

        self.assertEqual(managed, set())
        self.assertEqual(unmanaged, set())
        self.assertEqual(stale, set())

    def test_unreadable_swing_state_does_not_turn_candidate_pool_into_ownership(self):
        original = swing_manager._load_state
        try:
            swing_manager._load_state = lambda: (_ for _ in ()).throw(
                RuntimeError("corrupt fixture")
            )
            confirmed, readable = swing_manager.get_breakout_symbols_checked()
        finally:
            swing_manager._load_state = original

        self.assertFalse(readable)
        self.assertEqual(confirmed, set())


if __name__ == "__main__":
    unittest.main()
