# ruff: noqa: E501
"""QHM-REENTRY-NEWS-1109: events.catalyst_engine.fetch_news_since reads EVERY article since a moment, page by page,
and says complete=True only when the API ends the listing. The HTTP layer is mocked; nothing reaches a network."""
import io
import json
import os
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import events.catalyst_engine as cat  # noqa: E402

_CREDS = {"ALPACA_API_KEY": "test-key", "ALPACA_SECRET_KEY": "test-secret"}


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _art(aid, when, headline="GE news", summary=""):
    return {"id": aid, "symbols": ["GE"], "headline": headline, "summary": summary, "created_at": _iso(when),
            "url": "", "source": "benzinga"}


class _Resp:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Server:
    """Plays a list of responses (a payload dict, an exception, or a _Resp) and records each request's query."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.queries = []

    def __call__(self, req, timeout=None, context=None):
        self.queries.append(parse_qs(urlparse(req.full_url).query))
        r = self.responses.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r if isinstance(r, _Resp) else _Resp(r)


def _http(code):
    return urllib.error.HTTPError("https://data.alpaca.markets/v1beta1/news", code, "err", {}, io.BytesIO(b""))


class FetchNewsSince(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.start = (self.now - timedelta(days=10)).replace(microsecond=0)

    def _run(self, server, start=None):
        with patch.dict("os.environ", _CREDS), patch("events.catalyst_engine.urllib.request.urlopen", server):
            return cat.fetch_news_since("ge", start or self.start)

    def test_single_page_without_token_is_complete_and_window_is_exact(self):
        srv = _Server({"news": [_art(3, self.now - timedelta(days=1)), _art(2, self.start),
                                _art(1, self.start - timedelta(seconds=1))], "next_page_token": None})
        got = self._run(srv)
        self.assertTrue(got.complete)
        self.assertEqual(got.error, "")
        self.assertEqual([i["id"] for i in got.items], [3, 2])      # the article AT the start counts; earlier ones don't
        q = srv.queries[0]
        self.assertEqual(q["symbols"], ["GE"])
        self.assertEqual(q["start"], [_iso(self.start)])
        self.assertEqual(q["sort"], ["desc"])
        self.assertNotIn("page_token", q)

    def test_start_is_floored_to_the_second_and_filter_stays_exact(self):
        start = self.start + timedelta(microseconds=500000)
        srv = _Server({"news": [_art(1, self.start)], "next_page_token": None})   # 0.5 s before the exact start
        got = self._run(srv, start=start)
        self.assertEqual(srv.queries[0]["start"], [_iso(self.start)])
        self.assertTrue(got.complete)
        self.assertEqual(got.items, [])

    def test_empty_200_is_the_only_no_news_result(self):
        got = self._run(_Server({"news": [], "next_page_token": None}))
        self.assertTrue(got.complete)
        self.assertEqual(got.items, [])

    def test_second_page_is_read_with_its_token_and_a_negative_there_is_returned(self):
        srv = _Server({"news": [_art(2, self.now - timedelta(days=6))], "next_page_token": "t1"},
                      {"news": [_art(1, self.now - timedelta(days=8), "GE faces SEC investigation")],
                       "next_page_token": None})
        got = self._run(srv)
        self.assertTrue(got.complete)
        self.assertEqual(got.pages, 2)
        self.assertEqual(srv.queries[1]["page_token"], ["t1"])
        self.assertIn("SEC investigation", got.items[-1]["headline"])
        self.assertEqual(cat._classify(got.items[-1]["headline"])[0], "negative")

    def test_failures_on_page_two_are_incomplete(self):
        for bad in (_http(429), _http(500), TimeoutError("timed out"), urllib.error.URLError("down"),
                    _Resp(b"not json"), _Resp({"news": []}, status=204), {"no_news_key": []}, {"news": None},
                    {"news": ["not an article"]}, {"news": [{"id": 9, "headline": "x", "created_at": ""}]},
                    {"news": [{"id": 9, "headline": "x", "created_at": "2026-11-01T10:00:00"}]}):
            srv = _Server({"news": [_art(2, self.now - timedelta(days=6))], "next_page_token": "t1"}, bad)
            got = self._run(srv)
            self.assertFalse(got.complete, bad)
            self.assertTrue(got.error, bad)

    def test_failure_on_page_one_is_incomplete(self):
        for bad in (_http(401), _http(429), TimeoutError("timed out")):
            got = self._run(_Server(bad))
            self.assertFalse(got.complete)
            self.assertEqual(got.items, [])

    def test_repeated_token_is_incomplete(self):
        srv = _Server({"news": [_art(3, self.now - timedelta(days=6))], "next_page_token": "t1"},
                      {"news": [_art(2, self.now - timedelta(days=7))], "next_page_token": "t1"})
        got = self._run(srv)
        self.assertFalse(got.complete)
        self.assertIn("repeated page token", got.error)

    def test_non_string_token_is_incomplete(self):
        got = self._run(_Server({"news": [_art(1, self.now - timedelta(days=6))], "next_page_token": 7}))
        self.assertFalse(got.complete)

    def test_page_cap_stops_a_listing_that_runs_past_the_window(self):
        # one page already spans 25 days of the 10-day window -> 1 page needed -> cap 2 (the same again as headroom)
        pages = [{"news": [_art(100 + i, self.now - timedelta(days=25, minutes=i))], "next_page_token": "t%d" % i}
                 for i in range(10)]
        got = self._run(_Server(*pages))
        self.assertFalse(got.complete)
        self.assertIn("page cap 2 reached", got.error)
        self.assertEqual(got.pages, 2)

    def test_cap_is_rederived_from_all_pages_so_a_dense_first_page_does_not_fix_it(self):
        start = self.now - timedelta(days=60)
        cap_seen = []
        real = cat._news_page_cap

        def _spy(*a):
            cap_seen.append(real(*a))
            return cap_seen[-1]

        srv = _Server({"news": [_art(10, self.now - timedelta(minutes=1)), _art(9, self.now - timedelta(minutes=30))],
                       "next_page_token": "t1"},                       # dense: 30 min of a 60-day window
                      {"news": [_art(8, self.now - timedelta(days=20))], "next_page_token": "t2"},
                      {"news": [_art(7, self.now - timedelta(days=45))], "next_page_token": None})
        with patch("events.catalyst_engine._news_page_cap", _spy):
            got = self._run(srv, start=start)
        self.assertTrue(got.complete)
        self.assertEqual((got.pages, [i["id"] for i in got.items]), (3, [10, 9, 8, 7]))
        self.assertGreater(cap_seen[0], 5000)        # the first page alone: 60 days / 30 min -> ~2880 pages x 2
        self.assertLess(cap_seen[1], 20)             # re-derived from both pages: 60 days / (20 days / 2) -> 6 x 2

    def test_missing_next_page_token_key_is_incomplete(self):
        for page in ({"news": []}, {"news": [_art(1, self.now - timedelta(days=1))]}):
            got = self._run(_Server(page))
            self.assertFalse(got.complete, page)
            self.assertIn("next_page_token", got.error)
        srv = _Server({"news": [_art(2, self.now - timedelta(days=6))], "next_page_token": "t1"},
                      {"news": [_art(1, self.now - timedelta(days=8))]})
        self.assertFalse(self._run(srv).complete)

    def test_time_budget_makes_the_read_incomplete(self):
        budget = cat._NEWS_READ_TIMEOUT_SECONDS
        clock = iter([0.0, 1.0, budget + 1.0])        # start, before page 1, before page 2
        srv = _Server({"news": [_art(2, self.now - timedelta(days=1))], "next_page_token": "t1"},
                      {"news": [], "next_page_token": None})
        with patch("events.catalyst_engine.time.monotonic", side_effect=lambda: next(clock)):
            got = self._run(srv)
        self.assertFalse(got.complete)
        self.assertIn("time budget", got.error)
        self.assertEqual((got.pages, len(srv.queries)), (1, 1))     # page 2 never requested

    def test_end_is_the_window_end_sent_to_the_api(self):
        srv = _Server({"news": [], "next_page_token": None})
        got = self._run(srv)
        self.assertTrue(got.complete)
        self.assertIsNotNone(got.end)
        self.assertEqual(got.end.microsecond, 0)
        self.assertEqual(srv.queries[0]["end"], [_iso(got.end)])
        self.assertLessEqual(got.end, datetime.now(timezone.utc))
        bad = self._run(_Server(_http(429)))
        self.assertFalse(bad.complete)                # end is reported, but a caller advances only on complete
        self.assertIsNotNone(bad.end)
        self.assertIsNone(cat.NewsFetch([], True, "", 0).end)      # positional construction stays valid

    def test_cap_not_derivable_from_an_empty_first_page_with_a_token(self):
        got = self._run(_Server({"news": [], "next_page_token": "t1"}))
        self.assertFalse(got.complete)
        self.assertIn("page cap", got.error)

    def test_duplicates_by_id_are_kept_once(self):
        a = _art(5, self.now - timedelta(days=6))
        srv = _Server({"news": [a], "next_page_token": "t1"}, {"news": [a, _art(4, self.now - timedelta(days=7))], "next_page_token": None})
        got = self._run(srv)
        self.assertTrue(got.complete)
        self.assertEqual([i["id"] for i in got.items], [5, 4])

    def test_bad_inputs_make_no_request(self):
        srv = _Server()
        with patch.dict("os.environ", {"ALPACA_API_KEY": "", "ALPACA_SECRET_KEY": ""}), \
                patch("events.catalyst_engine.urllib.request.urlopen", srv):
            self.assertFalse(cat.fetch_news_since("GE", self.start).complete)
        for sym, start in (("", self.start), ("GE", datetime(2026, 11, 1, 10, 0)),
                           ("GE", self.now + timedelta(hours=1))):
            self.assertFalse(self._run(srv, start=start).complete if sym else
                             self._run_sym(srv, sym).complete)
        self.assertEqual(srv.queries, [])

    def _run_sym(self, server, sym):
        with patch.dict("os.environ", _CREDS), patch("events.catalyst_engine.urllib.request.urlopen", server):
            return cat.fetch_news_since(sym, self.start)

    def test_whole_summary_is_kept(self):
        long = "x " * 400 + "going concern"
        got = self._run(_Server({"news": [_art(1, self.now - timedelta(days=1), "GE update", long)],
                                 "next_page_token": None}))
        self.assertEqual(cat._classify(got.items[0]["headline"] + " " + got.items[0]["summary"])[0], "negative")

    def test_the_read_is_logged(self):
        with self.assertLogs("catalyst_engine", level="INFO") as cm:
            self._run(_Server({"news": [], "next_page_token": ""}))
        self.assertTrue(any("catalyst news_since GE:" in m and "complete=True" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
