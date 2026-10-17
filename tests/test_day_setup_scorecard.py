# ruff: noqa: E501
"""Day-tier setup scorecard (design record logs/design_records/day_setup_scorecard_2026-10-17.md).
Pure statistics + the runner's gate helper. No broker or network access; requires scipy (present on OCI)."""
import random
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from strategy import day_setup_scorecard as sc

try:
    import scipy  # noqa: F401
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _rows(key, days, r60, rclose, per_day=3, jitter=0.05):
    rng = random.Random(7)
    out = []
    for d in range(days):
        for _ in range(per_day):
            out.append({"key": key, "day": f"2026-10-{d + 1:02d}",
                        "rhold": r60 + rng.uniform(-jitter, jitter), "rclose": rclose + rng.uniform(-jitter, jitter)})
    return out


def _book(bad60=-0.6, badclose=-0.8, bad_days=12):
    rows = []
    rows += _rows("short/FADE", bad_days, bad60, badclose)
    rows += _rows("long/FADE", 12, 0.3, 0.4)
    rows += _rows("long/RIDE", 10, 0.1, 0.2)
    rows += _rows("short/DRIVE", 10, 0.2, -0.1)
    return rows


@skipUnless(_HAS_SCIPY, "scipy needed (present on the server)")
class ScoreTests(TestCase):
    def test_consistently_losing_setup_is_blocked_others_not(self):
        card = sc.build_scorecard(_book(), "2026-10-16", "now")
        self.assertTrue(card["setups"]["short/FADE"]["blocked"])
        for k in ("long/FADE", "long/RIDE", "short/DRIVE"):
            self.assertFalse(card["setups"][k]["blocked"], k)

    def test_needs_both_horizons_negative(self):
        card = sc.build_scorecard(_book(bad60=-0.6, badclose=0.4), "d", "now")
        self.assertFalse(card["setups"]["short/FADE"]["blocked"])

    def test_one_day_of_evidence_never_blocks(self):
        card = sc.build_scorecard(_book(bad_days=1), "d", "now")
        self.assertFalse(card["setups"]["short/FADE"]["blocked"])

    def test_no_between_setup_spread_means_nothing_blocked(self):
        rows = _rows("short/FADE", 10, -0.1, -0.1, jitter=0.5) + _rows("long/FADE", 10, -0.1, -0.1, jitter=0.5)
        card = sc.build_scorecard(rows, "d", "now")
        self.assertFalse(any(v["blocked"] for v in card["setups"].values()))

    def test_drift_flag_when_horizons_disagree(self):
        card = sc.build_scorecard(_book(), "d", "now")
        self.assertTrue(card["setups"]["short/DRIVE"]["drift_flag"])

    def test_explore_probability_is_small_for_a_clear_loser(self):
        card = sc.build_scorecard(_book(), "d", "now")
        p = card["setups"]["short/FADE"]["explore_probability"]
        self.assertIsNotNone(p)
        self.assertLess(p, card["setups"]["long/FADE"]["explore_probability"] or 1.0)


class EvaluateTests(TestCase):
    _card = {"built_for_session": "2026-10-16",
             "setups": {"short/FADE": {"blocked": True, "explore_probability": 0.25},
                        "long/FADE": {"blocked": False, "explore_probability": 0.9}}}

    def test_unblocked_and_unknown_setups_allow(self):
        self.assertEqual(sc.evaluate("long", "FADE", self._card)["action"], "allow")
        self.assertEqual(sc.evaluate("short", "PULLBACK", self._card)["action"], "allow")
        self.assertEqual(sc.evaluate("short", "FADE", {})["action"], "allow")

    def test_blocked_setup_blocks_or_explores_by_its_probability(self):
        class R:
            def __init__(self, v):
                self.v = v

            def random(self):
                return self.v
        self.assertEqual(sc.evaluate("short", "FADE", self._card, R(0.10))["action"], "explore")
        self.assertEqual(sc.evaluate("short", "FADE", self._card, R(0.60))["action"], "block")

    def test_malformed_card_allows(self):
        self.assertEqual(sc.evaluate("short", "FADE", {"setups": "oops"})["action"], "allow")


class LoadTests(TestCase):
    def test_torn_file_keeps_last_good(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "card.json"
            sc.write_scorecard({"setups": {"x/Y": {"blocked": True}}, "built_for_session": "a"}, p)
            self.assertEqual(sc.load_scorecard(p)["built_for_session"], "a")
            p.write_text("{not json")
            self.assertEqual(sc.load_scorecard(p)["built_for_session"], "a")


class RunnerGateTests(TestCase):
    def test_block_skips_and_logs_evidence(self):
        import run_day_tier as rdt
        card = {"setups": {"short/FADE": {"blocked": True, "explore_probability": 0.0}}}
        with patch.object(rdt, "_log_direction_skip") as skip:
            self.assertIsNone(rdt._setup_scorecard_gate("MSFT", {}, {"direction": "short", "mode": "FADE"}, card, "b1"))
        self.assertIn("setup_scorecard", skip.call_args[0][2])

    def test_allow_passes_through(self):
        import run_day_tier as rdt
        v = rdt._setup_scorecard_gate("MSFT", {}, {"direction": "long", "mode": "FADE"}, {}, "b1")
        self.assertEqual(v["action"], "allow")


@skipUnless(_HAS_SCIPY, "scipy needed (present on the server)")
class PosteriorSETests(TestCase):
    def test_posterior_sd_is_sqrt_shrunk(self):
        import math
        h = sc.score_horizon(_book(), "rhold")["short/FADE"]
        n, k = h["days"], h["k"]
        self.assertAlmostEqual(h["post_se"], h["se"] * math.sqrt(n / (n + k)))


class StaleCheckTests(TestCase):
    def test_card_is_kept_when_page_mark_write_fails(self):
        import run_day_tier as rdt
        from datetime import datetime
        card = {"built_for_session": "2000-01-01", "setups": {}}
        with patch.object(sc, "load_scorecard", return_value=card), \
                patch("pathlib.Path.write_text", side_effect=OSError("disk")), \
                patch("alerts.send_slack"):
            self.assertIs(rdt._setup_scorecard_for_tick(datetime(2026, 10, 19, 10, 0, tzinfo=rdt.ET)), card)

    def test_fresh_card_does_not_page(self):
        import run_day_tier as rdt
        from datetime import datetime
        card = {"built_for_session": "2026-10-16", "setups": {}}
        with patch.object(sc, "load_scorecard", return_value=card), patch("alerts.send_slack") as page:
            rdt._setup_scorecard_for_tick(datetime(2026, 10, 19, 10, 0, tzinfo=rdt.ET))   # Monday; prev weekday = Fri 10/16
        page.assert_not_called()


class DeterministicDrawTests(TestCase):
    def test_same_signal_gets_the_same_verdict_every_tick(self):
        import run_day_tier as rdt
        card = {"setups": {"short/FADE": {"blocked": True, "explore_probability": 0.5}}}
        with patch.object(rdt, "_log_direction_skip"):
            acts = {str((rdt._setup_scorecard_gate("MSFT", {}, {"direction": "short", "mode": "FADE"}, card, f"b{i}")
                         or {}).get("action")) for i in range(5)}
        self.assertEqual(len(acts), 1)
