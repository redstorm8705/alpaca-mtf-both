# ruff: noqa: E501
"""DAY-ALIGN-1123 (owner-approved 2026-11-23): the pre-registered fade stop rule counts and gates EVERY Track A FADE
(with-trend too), not only counter-trend fades. Pure-function tests plus a check that the Track A loop gates FADE
entries through it. No network, no broker."""
import inspect
import unittest

from strategy import day_tier_alignment as al


def _trade(tid, pnl, ts, mode="FADE", counter=False, track=None, via_fill=False):
    """A decision record + its exit. via_fill: the live join (decision_id -> entry_fill.trade_id -> exit_fill)."""
    dec = {"event": "decision", "trigger": {"mode": mode, "counter_trend": counter}}
    if track:
        dec["decision"] = {"track": track}
    if via_fill:
        dec["decision_id"] = f"D-{tid}"
        return [dec, {"event": "entry_fill", "decision_id": f"D-{tid}", "trade_id": tid},
                {"event": "exit_fill", "trade_id": tid, "realized_pnl": pnl, "ts": ts}]
    dec["trade_id"] = tid
    return [dec, {"event": "exit_fill", "trade_id": tid, "realized_pnl": pnl, "ts": ts}]


class FadeStopRuleCoversWithTrendFades(unittest.TestCase):
    def test_with_trend_fade_losses_now_count(self):
        # the 11-23 NVDA short: a with-trend FADE (counter_trend False) -59.13 after earlier with-trend FADE losses
        ev = (_trade("a", -20.0, "2026-11-19T10:00", via_fill=True)
              + _trade("nvda", -59.13, "2026-11-23T06:58", via_fill=True))
        ok, why = al.counter_trend_fades_ok(ev)
        self.assertFalse(ok)
        self.assertIn("cumulative", why)

    def test_before_this_change_with_trend_fades_were_not_counted(self):
        # a non-FADE, non-counter-trend trade still does not count (RIDE/PULLBACK/DRIVE unaffected)
        ev = (_trade("r1", -30.0, "2026-11-19T10:00", mode="RIDE")
              + _trade("p1", -30.0, "2026-11-19T11:00", mode="PULLBACK"))
        self.assertTrue(al.counter_trend_fades_ok(ev)[0])

    def test_three_consecutive_with_trend_fade_losses_disable(self):
        ev = (_trade("a", 5.0, "2026-11-18T09:00") + _trade("b", -1.0, "2026-11-18T10:00")
              + _trade("c", -1.0, "2026-11-18T11:00") + _trade("d", -1.0, "2026-11-18T12:00"))
        ok, why = al.counter_trend_fades_ok(ev)
        self.assertFalse(ok)
        self.assertIn("3 consecutive", why)

    def test_track_b_fade_mode_is_never_counted(self):
        ev = _trade("b1", -40.0, "2026-11-18T10:00", track="B")
        self.assertTrue(al.counter_trend_fades_ok(ev)[0])

    def test_counter_trend_fades_still_count_as_before(self):
        ev = _trade("c1", -30.0, "2026-11-18T10:00", mode="FADE", counter=True)
        self.assertFalse(al.counter_trend_fades_ok(ev)[0])

    def test_mode_case_insensitive_and_missing_mode_ignored(self):
        self.assertFalse(al.counter_trend_fades_ok(_trade("x", -30.0, "2026-11-18T10:00", mode="fade"))[0])
        ev = [{"event": "decision", "trade_id": "y", "trigger": {}},
              {"event": "exit_fill", "trade_id": "y", "realized_pnl": -30.0, "ts": "2026-11-18T10:00"}]
        self.assertTrue(al.counter_trend_fades_ok(ev)[0])

    def test_winning_fades_keep_fades_allowed(self):
        ev = _trade("w1", 6.0, "2026-11-18T10:00") + _trade("w2", -2.0, "2026-11-18T11:00")
        self.assertTrue(al.counter_trend_fades_ok(ev)[0])


class TrackAGatesEveryFade(unittest.TestCase):
    def test_track_a_loop_checks_the_fade_rule_for_fade_mode(self):
        import run_day_tier
        src = inspect.getsource(run_day_tier)
        i = src.index("DAY-ALIGN-1123")
        block = src[i:i + 900]
        self.assertIn('str(trigger.get("mode") or "").upper() == "FADE"', block)
        self.assertIn("_counter_trend_fades_ok()", block)
        self.assertIn("_log_direction_skip(", block)
        self.assertIn("continue", block)


if __name__ == "__main__":
    unittest.main()
