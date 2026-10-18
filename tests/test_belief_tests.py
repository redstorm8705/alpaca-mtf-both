#!/usr/bin/env python3
# ruff: noqa: E501
"""RULES-1018 rule 3 belief tests (logs/design_records/article_rules_2026-10-18.md).

Pure logic on synthetic trades — no network, no broker:
  * a belief reads decision-time inputs only (an entry carrying an outcome field is refused: the look-ahead fixture);
  * labels shuffled at random stay "inconclusive" (the false-positive check);
  * a strong, repeated effect across many days is "held"; the reverse is "failed";
  * Holm-corrected p-values are never below the raw ones; the GEX stand-down belief is reported "not testable";
  * results append one JSON line per belief."""
import json
import os
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import belief_tests as bt  # noqa: E402


def make_history(score_effect: float, days: int = 60, seed: int = 1, shuffle: bool = False):
    """Two trades a day: one high-score, one low-score. P&L = noise + score_effect for high-score trades."""
    rng = random.Random(seed)
    start = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
    round_trips, entries, spy = [], [], {}
    price = 500.0
    for d in range(days + 3):
        spy[(start + timedelta(days=d)).date()] = price
        price *= 1 + rng.gauss(0, 0.01)
    for d in range(1, days + 1):
        day = start + timedelta(days=d)
        for k, score in enumerate((11, 9)):
            entry_t = day + timedelta(minutes=10 * k)
            exit_t = entry_t + timedelta(hours=2)
            ret = rng.gauss(0, 0.01) + (score_effect if score == 11 else 0.0)
            label = rng.choice((11, 9)) if shuffle else score
            oid = f"o{d}-{k}"
            round_trips.append({"symbol": "AAA", "direction": "long", "tier": "swing", "entry_order_id": oid,
                                "entry_time": entry_t.isoformat(), "exit_time": exit_t.isoformat(),
                                "entry_price": 100.0, "qty": 10, "pnl": ret * 1000.0, "lifecycle_complete": True})
            entries.append({"event": "entry", "symbol": "AAA", "direction": "long", "tier": "swing",
                            "score": label, "ts": (entry_t + timedelta(seconds=5)).isoformat(),   # logged after the fill
                            "gex_spy_regime": None, "tsmom_direction": None})
    return round_trips, entries, spy


def verdict(results, name):
    return next(r for r in results if r["belief"] == name)


class BeliefTests(unittest.TestCase):
    def test_outcome_fields_are_refused(self):
        with self.assertRaises(ValueError):
            bt.decision_inputs({"symbol": "AAA", "score": 10, "pnl": 5.0})
        self.assertEqual(bt.decision_inputs({"symbol": "AAA", "score": 10})["score"], 10)

    def test_shuffled_labels_stay_inconclusive(self):
        hits = 0
        for seed in range(10):
            rt, en, spy = make_history(0.01, seed=seed, shuffle=True)
            hits += verdict(bt.evaluate(rt, en, spy, rounds=500), "score_floor")["verdict"] != "inconclusive"
        self.assertLessEqual(hits, 1)               # at most the expected false-positive rate

    def test_strong_effect_held_and_reverse_failed(self):
        rt, en, spy = make_history(0.02)
        self.assertEqual(verdict(bt.evaluate(rt, en, spy, rounds=1000), "score_floor")["verdict"], "held")
        rt, en, spy = make_history(-0.02)
        self.assertEqual(verdict(bt.evaluate(rt, en, spy, rounds=1000), "score_floor")["verdict"], "failed")

    def test_holm_and_untestable_belief(self):
        rt, en, spy = make_history(0.0)
        res = bt.evaluate(rt, en, spy, rounds=300)
        for r in res:
            if "p_holm" in r:
                self.assertGreaterEqual(r["p_holm"], r["p"])
        self.assertEqual(verdict(res, "gex_standdown")["verdict"], "not testable")
        self.assertEqual(verdict(res, "gex_negative")["verdict"], "inconclusive")   # no tagged entries

    def test_trend_sign_is_read_as_logged(self):
        rt, en, spy = make_history(0.0, days=20)
        for e in en:
            e["tsmom_direction"] = 1 if e["score"] == 11 else -1      # the live log's 1 / -1 form
        r = verdict(bt.evaluate(rt, en, spy, rounds=200), "trend_aligned")
        self.assertEqual(r["n_a"] + r["n_b"], 40)

    def test_a_decision_logged_after_a_trade_closed_is_not_its_decision(self):
        """Review T1: trade A (no event of its own) must not borrow trade B's later record; a record is used once."""
        t = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
        a = {"symbol": "AAPL", "direction": "long", "tier": "swing", "entry_order_id": "A", "entry_time": t.isoformat(),
             "exit_time": (t + timedelta(hours=1)).isoformat(), "entry_price": 1.0, "qty": 1, "pnl": 0.1,
             "lifecycle_complete": True}
        b = dict(a, entry_order_id="B", entry_time=(t + timedelta(hours=4)).isoformat(),
                 exit_time=(t + timedelta(hours=5)).isoformat())
        ev = {"symbol": "AAPL", "direction": "long", "tier": "swing", "score": 12,
              "ts": (t + timedelta(hours=4, seconds=2)).isoformat()}
        joined = bt.join_entries(bt.lifecycles([a, b]), [ev])
        self.assertEqual([j["entry_time"] for j in joined], [b["entry_time"]])
        qhm = dict(b, tier="qhm", entry_order_id="Q")
        self.assertEqual(bt.join_entries(bt.lifecycles([qhm]), [ev]), [])
        with self.assertRaises(ValueError):
            bt.evaluate([a, b], [ev], {})                 # no SPY data -> unavailable, not "0 trades"

    def test_rerun_of_a_week_does_not_duplicate_lines(self):
        rt, en, spy = make_history(0.0, days=8)
        res = bt.evaluate(rt, en, spy, rounds=50)
        with tempfile.TemporaryDirectory() as d:
            p = __import__("pathlib").Path(d) / "b.jsonl"
            bt.append_log(res, date(2026, 10, 16), path=p)
            bt.append_log(res, date(2026, 10, 16), path=p)
            self.assertEqual(len(open(p).read().splitlines()), len(res))

    def test_no_entries_joined_means_inconclusive_not_a_guess(self):
        rt, en, spy = make_history(0.02)
        res = bt.evaluate(rt, [], spy, rounds=200)
        self.assertEqual(verdict(res, "score_floor")["verdict"], "inconclusive")

    def test_append_log_writes_one_line_per_belief(self):
        rt, en, spy = make_history(0.0, days=10)
        res = bt.evaluate(rt, en, spy, rounds=100)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.jsonl")
            bt.append_log(res, date(2026, 10, 16), path=__import__("pathlib").Path(p))
            lines = [json.loads(x) for x in open(p)]
        self.assertEqual(len(lines), len(res))
        self.assertTrue(all(x["week_ending"] == "2026-10-16" for x in lines))
        self.assertTrue(bt.report_lines(res))


if __name__ == "__main__":
    unittest.main()
