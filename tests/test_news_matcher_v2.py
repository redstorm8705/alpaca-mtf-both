# ruff: noqa: E501
"""NEWS-VENUE-HALT-1103 — v2 news keyword matcher and the venue-state guards.

Design record: logs/design_records/news_venue_halt_v2_2026-11-03.md. No broker client is created: the
calendar client and Slack are patched in every run_cycle test.
"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import events.news_monitor as nm

ET = ZoneInfo("America/New_York")


def _monitor():
    with mock.patch.object(nm.NewsMonitor, "_load_seen_hashes", return_value={}), \
            mock.patch.object(nm.NewsMonitor, "_restore_macro_risk_window", return_value=None):
        return nm.NewsMonitor()


class MatcherProbes(unittest.TestCase):
    """The probe headlines from the design review (round 3), plus the false positives that blocked
    trading in the 103-day replay."""

    def setUp(self):
        self.m = _monitor()

    def level(self, title, desc="", fed=False):
        text = f"federal reserve {title} {desc}" if fed else f"{title} {desc}"
        return self.m._classify(text, title=title, fed=fed)[0]

    def test_market_wide_halts_match(self):
        for t in ("Nasdaq announces trading halt in all symbols",
                  "NYSE announces halt all trading",
                  "Market-wide circuit breaker triggered",
                  "U.S. market-wide circuit breaker triggered at 10:00 A.M.",
                  "Level 1 circuit breaker hit as stocks plunge",
                  "MARKET–WIDE CIRCUIT BREAKER TRIGGERED",
                  "Officials weigh national emergency's reach",
                  "President declares national emergencies over grid"):
            self.assertEqual(self.level(t), "HALT", t)

    def test_fed_feed_emergency_phrases(self):
        self.assertEqual(self.level("Federal Reserve emergency meeting", fed=True), "HALT")
        self.assertEqual(self.level("emergency rate cut", fed=True), "HALT")
        # the Fed-only phrases do not fire on other feeds
        self.assertNotEqual(self.level("Bank holds emergency meeting on payroll software"), "HALT")

    def test_fed_synthetic_prefix_is_not_title(self):
        # "federal reserve emergency" must not be manufactured from the feed's own prefix
        self.assertNotEqual(self.level("Emergency lending facility terms updated", fed=True), "HALT")

    def test_replay_false_positives_do_not_halt(self):
        for t in ("PG&E names head of International Emergency Management",
                  "Senator says NATO talks continue",
                  "Scorpio Gold announces trading halt",
                  "Catastrophic disruption cascades driven by the nonlinearity of systemic risk"):
            self.assertNotEqual(self.level(t), "HALT", t)

    def test_halt_phrase_in_description_only_is_display(self):
        lvl, kws, _ = self.m._classify("Stocks slip on Friday national emergency talk in op-ed",
                                       title="Stocks slip on Friday")
        self.assertEqual(lvl, "CAUTION")
        self.assertIn("national emergency", kws)

    def test_crypto_venue_is_not_equity_halt(self):
        self.assertEqual(self.level("Crypto exchange halts all trading after hack"), "CAUTION")
        self.assertEqual(self.level("Bitcoin venue: market-wide circuit breaker triggered"), "CAUTION")
        for t in ("Cryptocurrency exchange halts all trading after hack", "Ethereum venue halts all trading",
                  "Coinbase halts all trading", "Binance halts all trading", "Kraken halts all trading"):
            self.assertEqual(self.level(t), "CAUTION", t)

    def test_equity_circuit_breaker_naming_bitcoin_stays_halt(self):
        self.assertEqual(self.level("Market-wide circuit breaker triggered as stocks, bitcoin plunge"), "HALT")
        self.assertEqual(self.level("NYSE halts all trading; bitcoin also falls"), "HALT")

    def test_fed_prefix_not_matched_in_description(self):
        self.assertIsNot(self.level("Emergency lending facility terms updated", fed=True), "HALT")
        lvl, kws, _ = self.m._classify("federal reserve Emergency lending facility terms updated",
                                       title="Emergency lending facility terms updated", fed=True)
        self.assertNotIn("federal reserve emergency", kws)

    def test_minus_sign_between_words(self):
        self.assertEqual(self.level("Market−wide circuit breaker triggered"), "HALT")

    def test_truth_social_whole_post(self):
        post = "great day. i am declaring a national emergency on the border. thank you!"
        self.assertEqual(self.m._classify(post)[0], "HALT")

    def test_halt_keywords_are_the_matched_text(self):
        lvl, kws, _ = self.m._classify("Market-Wide  Circuit Breaker triggered",
                                       title="Market-Wide  Circuit Breaker triggered")
        self.assertEqual(lvl, "HALT")
        self.assertEqual(kws, ["market-wide  circuit breaker"])

    def test_whole_word_caution(self):
        self.assertIsNone(self.m._classify("Kiran signs originator deal")[0])
        self.assertEqual(self.m._classify("Iran tensions rise")[0], "CAUTION")
        self.assertEqual(self.m._classify("rate-hike odds jump")[0], "CAUTION")
        self.assertEqual(self.m._classify("bankruptcies climb in Q3")[0], "CAUTION")

    def test_kill_flag_restores_substring_matcher(self):
        with mock.patch.object(nm, "NEWS_MATCHER_V2_ENABLED", False):
            self.assertEqual(self.m._classify("PG&E International Emergency Management",
                                              title="PG&E International Emergency Management")[0], "HALT")

    def test_active_halt_details(self):
        a = nm.BreakingNewsAlert("CNBC", "Market-wide circuit breaker triggered",
                                 ["market-wide circuit breaker"], "HALT", 1.0, datetime.now(ET))
        self.m._active_alerts = [a]
        self.assertTrue(self.m.has_active_halt_keyword())
        d = self.m.active_halt_details()
        self.assertEqual(d[0]["matched"], ["market-wide circuit breaker"])
        self.assertEqual(d[0]["matcher"], "v2")

    def test_pattern_is_linear_on_long_input(self):
        import time
        t0 = time.perf_counter()
        self.m._classify("market " * 20000 + "wide")
        self.assertLess(time.perf_counter() - t0, 5.0)


class _Row:
    def __init__(self, d, close):
        self.date, self.close = d, close


class VenueGuards(unittest.TestCase):
    def setUp(self):
        import strategy.run_cycle as rc
        self.rc = rc
        if hasattr(rc._todays_scheduled_close_et, "_cache"):
            del rc._todays_scheduled_close_et._cache

    def _client(self, rows):
        c = mock.MagicMock()
        c.get_calendar.return_value = rows
        return c

    def test_scheduled_close_from_calendar_naive_et(self):
        today = datetime.now(ET).date()
        naive_close = datetime(today.year, today.month, today.day, 13, 0)   # early close day
        with mock.patch("execution.broker.get_trading_client", return_value=self._client([_Row(today, naive_close)])):
            got = self.rc._todays_scheduled_close_et()
        self.assertEqual(got, naive_close.replace(tzinfo=ET))

    def test_scheduled_close_none_on_error_and_not_cached(self):
        c = mock.MagicMock()
        c.get_calendar.side_effect = RuntimeError("down")
        with mock.patch("execution.broker.get_trading_client", return_value=c):
            self.assertIsNone(self.rc._todays_scheduled_close_et())
        self.assertFalse(hasattr(self.rc._todays_scheduled_close_et, "_cache"))

    def test_scheduled_close_ignores_other_dates(self):
        today = datetime.now(ET).date()
        other = today - timedelta(days=1)
        with mock.patch("execution.broker.get_trading_client",
                        return_value=self._client([_Row(other, datetime(other.year, other.month, other.day, 16))])):
            self.assertIsNone(self.rc._todays_scheduled_close_et())

    def test_uncertain_page_once_per_day(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "venue_uncertain_page.json"
            with mock.patch.object(self.rc, "_VENUE_UNCERTAIN_PAGE", path), \
                    mock.patch.object(self.rc, "send_slack") as slack:
                self.rc._page_venue_uncertain_once("SPY prior close unavailable")
                self.rc._page_venue_uncertain_once("SPY prior close unavailable")
                self.assertEqual(slack.call_count, 1)
                self.assertEqual(json.loads(path.read_text())["date"], datetime.now(ET).strftime("%Y-%m-%d"))
                path.write_text(json.dumps({"date": (date.today() - timedelta(days=2)).isoformat()}))
                self.rc._page_venue_uncertain_once("x")
                self.assertEqual(slack.call_count, 2)

    def test_uncertain_page_failed_send_does_not_mark_sent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.json"
            with mock.patch.object(self.rc, "_VENUE_UNCERTAIN_PAGE", path), \
                    mock.patch.object(self.rc, "send_slack", side_effect=RuntimeError("slack down")):
                self.rc._page_venue_uncertain_once("x")
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
