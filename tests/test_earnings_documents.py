# ruff: noqa: E501
"""Tests for data/earnings_documents.py — every HTTP call mocked; nothing reaches SEC, Alpha Vantage or a broker."""
import json
from datetime import date, datetime, timezone
from unittest import mock

import pytest

import data.earnings_documents as ed

SUB = {"filings": {"recent": {
    "form": ["10-Q", "8-K", "8-K", "8-K"],
    "items": ["", "5.07", "2.02,9.01", "2.02,9.01"],
    "acceptanceDateTime": ["2026-10-31T20:00:00.000Z", "2026-11-03T12:00:00.000Z",
                           "2026-11-02T21:30:23.000Z", "2026-07-30T20:30:00.000Z"],
    "accessionNumber": ["a-1", "a-2", "0000320193-26-000024", "a-4"]}}}
FULL = ("<SEC-DOCUMENT><DOCUMENT>\n<TYPE>8-K\n<TEXT>cover</TEXT></DOCUMENT>"
        "<DOCUMENT>\n<TYPE>EX-99.1\n<TEXT><html><body><p>Apple reports fourth quarter results</p>"
        "<p>Revenue &amp; EPS — up 8%</p><script>x()</script></body></html></TEXT></DOCUMENT></SEC-DOCUMENT>")


class R:
    def __init__(self, payload=None, text=""):
        self._p, self.text = payload, text

    def json(self):
        return self._p

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def _no_network(monkeypatch, tmp_path):
    monkeypatch.setattr(ed, "_TICKERS_FILE", tmp_path / "t.json")
    monkeypatch.setattr(ed, "_CACHE", tmp_path)
    monkeypatch.setattr(ed, "_SEC_MIN_GAP_S", 0.0)
    monkeypatch.setattr(ed.requests, "get", mock.Mock(side_effect=AssertionError("unmocked network call")))


def _route(url, **_):
    if "company_tickers" in url:
        return R({"0": {"cik_str": 320193, "ticker": "AAPL"}, "1": {"cik_str": 1067983, "ticker": "BRK-B"}})
    if "submissions" in url:
        return R(SUB)
    if url.endswith(".txt"):
        return R(text=FULL)
    raise AssertionError(url)


def test_release_picks_newest_202_since_and_extracts_exhibit():
    with mock.patch.object(ed, "_get", side_effect=lambda url, **k: _route(url, **k)):
        out = ed.edgar_earnings_release("AAPL", datetime(2026, 11, 2, 20, 0, tzinfo=timezone.utc))
    assert out["ok"] and out["accession"] == "0000320193-26-000024"
    assert out["accepted_at"].startswith("2026-11-02T21:30:23")
    assert "Apple reports fourth quarter results" in out["text"] and "x()" not in out["text"]
    assert "Revenue & EPS" in out["text"] and len(out["sha256"]) == 64


def test_release_none_since_cutoff():
    with mock.patch.object(ed, "_get", side_effect=lambda url, **k: _route(url, **k)):
        out = ed.edgar_earnings_release("AAPL", datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc))
    assert not out["ok"] and out["reason"] == "no_filing_since"


def test_release_bad_inputs_and_unknown_ticker():
    assert ed.edgar_earnings_release("AAPL", datetime(2026, 11, 2))["reason"] == "bad_input"   # naive
    assert ed.edgar_earnings_release("AAPL", datetime(2026, 11, 2, tzinfo=timezone.utc), 0)["reason"] == "bad_input"
    with mock.patch.object(ed, "_get", side_effect=lambda url, **k: _route(url, **k)):
        assert ed.edgar_earnings_release("ZZZZ", datetime(2026, 11, 2, tzinfo=timezone.utc))["reason"] == "no_cik"
        assert ed.cik_for("BRK.B") == 1067983


def test_release_no_exhibit_and_errors_never_raise():
    def no_ex(url, **k):
        return R(text="<DOCUMENT><TYPE>8-K<TEXT>x</TEXT></DOCUMENT>") if url.endswith(".txt") else _route(url, **k)
    with mock.patch.object(ed, "_get", side_effect=no_ex):
        assert ed.edgar_earnings_release("AAPL", datetime(2026, 11, 2, tzinfo=timezone.utc))["reason"] == "no_exhibit"

    def boom(url, **k):
        if "submissions" in url:
            raise ConnectionError("down")
        return _route(url, **k)
    with mock.patch.object(ed, "_get", side_effect=boom):
        out = ed.edgar_earnings_release("AAPL", datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert not out["ok"] and out["reason"].startswith("submissions_error")


def test_transcript_no_key(monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    assert ed.alpha_vantage_transcript("AAPL", "2026Q3")["reason"] == "no_key"


def test_transcript_ok_note_and_error_hides_key(monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "SECRETKEY")
    body = {"symbol": "AAPL", "quarter": "2026Q3", "transcript": [
        {"speaker": "Tim", "title": "CEO", "content": "We set an all-time record."}, {"speaker": "x", "content": ""}]}
    with mock.patch.object(ed, "_get", return_value=R(body)):
        out = ed.alpha_vantage_transcript("aapl", "2026Q3")
    assert out["ok"] and out["turns"] == 1 and "all-time record" in out["text"]
    with mock.patch.object(ed, "_get", return_value=R({"Information": "rate limit"})):
        assert ed.alpha_vantage_transcript("AAPL", "2026Q3")["reason"].startswith("api_note")
    with mock.patch.object(ed, "_get", side_effect=ConnectionError("https://x?apikey=SECRETKEY")):
        out = ed.alpha_vantage_transcript("AAPL", "2026Q3")
    assert not out["ok"] and "SECRETKEY" not in json.dumps(out)
    assert ed.alpha_vantage_transcript("AAPL", "2026-Q3")["reason"] == "bad_input"


def test_quarter_label():
    assert ed.quarter_label(date(2026, 11, 2)) == "2026Q3"
    assert ed.quarter_label(date(2026, 1, 29)) == "2025Q4"
    assert ed.quarter_label(date(2026, 4, 30)) == "2026Q1"
    assert ed.quarter_label(date(2026, 7, 30)) == "2026Q2"


def test_quote_check_normalizes_but_never_fuzzes():
    doc = "Revenue grew “strongly” — up  8% year over year."
    assert ed.quote_found('Revenue grew "strongly" - up 8%', doc)
    assert not ed.quote_found("Revenue grew strongly", doc)
    assert not ed.quote_found("", doc)


def test_deadline_passed_raises_inside_get_but_caller_safe(monkeypatch):
    with pytest.raises(TimeoutError):
        ed._get("https://example.invalid", sec=False, deadline=0.0)


def test_falls_back_to_older_filing_and_skips_bad_rows():
    sub = {"filings": {"recent": {
        "form": ["8-K", "8-K", "8-K/A"], "items": ["2.02", "2.02,9.01", "2.02"],
        "acceptanceDateTime": ["not-a-date", "2026-11-02T21:30:23.000Z", "2026-11-03T15:00:00.000Z"],
        "accessionNumber": ["bad", "0000320193-26-000024", "amend-1"]}}}

    def route(url, **k):
        if "submissions" in url:
            return R(sub)
        if "amend1" in url.replace("-", ""):
            return R(text="<DOCUMENT><TYPE>8-K/A<TEXT>typo fix</TEXT></DOCUMENT>")
        return _route(url, **k)
    with mock.patch.object(ed, "_get", side_effect=route):
        out = ed.edgar_earnings_release("AAPL", datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert out["ok"] and out["accession"] == "0000320193-26-000024"


def test_helpers_never_raise():
    assert ed.quarter_label(None) == ""
    assert ed.quote_found(5, "doc") is False and ed.quote_found("doc", None) is False
    assert ed.html_to_text(None) == "" and ed.cik_for(None) is None


def test_non_string_symbol_never_raises(monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    assert ed.edgar_earnings_release(123, datetime(2026, 11, 2, tzinfo=timezone.utc))["ok"] is False
    assert ed.alpha_vantage_transcript(123, "2026Q3")["reason"] == "no_key"

    class Ambiguous:   # like pd.NA / numpy arrays: truth value and str() both raise
        def __bool__(self):
            raise TypeError("ambiguous")

        def __str__(self):
            raise ValueError("no str")
    assert ed.edgar_earnings_release(Ambiguous(), datetime(2026, 11, 2, tzinfo=timezone.utc))["ok"] is False
    assert ed.alpha_vantage_transcript(Ambiguous(), "2026Q3")["ok"] is False


def test_urllib3_logger_has_its_own_level():
    import logging
    assert logging.getLogger("urllib3.connectionpool").level >= logging.INFO


def test_cache_save_failure_keeps_fetched_map(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(ed.tempfile, "mkstemp", boom)
    with mock.patch.object(ed, "_get", side_effect=lambda url, **k: _route(url, **k)):
        assert ed.cik_for("AAPL") == 320193


def test_concurrent_cold_cache_lookups_all_resolve():
    import threading
    out = []
    with mock.patch.object(ed, "_get", side_effect=lambda url, **k: _route(url, **k)):
        ts = [threading.Thread(target=lambda: out.append(ed.cik_for("AAPL"))) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert out == [320193] * 8
