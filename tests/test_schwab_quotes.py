# ruff: noqa: E501, E402
"""Unit tests for data/schwab_quotes.py and scripts/schwab_login.py (SCHWAB-QUOTES-1103).

Every HTTP call is mocked (the module's `requests` attribute is replaced); every state file lives
in a temp dir. No Schwab or Alpaca client is created and nothing reaches the network or Slack.
"""

import ast
import json
import logging
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The bot's venv has requests; a bare dev interpreter may not. Every HTTP call in these tests is
# mocked, so a stub module is enough for the import to succeed.
try:
    import requests  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["requests"] = types.ModuleType("requests")

from data import schwab_quotes as sq

APP_KEY, APP_SECRET = "APPKEY_123456", "SECRET_XYZ_987"
ACCESS, REFRESH, NEW_ACCESS, CODE = "ACCESS_ABC_111", "REFRESH_DEF_222", "ACCESS_NEW_333", "CODE_GHI_444"
SECRET_STRINGS = (APP_SECRET, ACCESS, REFRESH, NEW_ACCESS, CODE)


class FakeResp:
    def __init__(self, status=200, body=None, headers=None, bad_json=False):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("truncated body")
        return self._body


def qbody(**quotes):
    """quotes: schwab_symbol -> dict overrides of the quote fields."""
    now_ms = int(time.time() * 1000)
    out = {}
    for sym, over in quotes.items():
        q = {"bidPrice": 100.0, "askPrice": 100.05, "bidSize": 3, "askSize": 4, "lastPrice": 100.02, "quoteTime": now_ms}
        q.update(over or {})
        out[sym] = {"symbol": sym, "quote": q}
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(sq, "STATE_DIR", d),
            mock.patch.object(sq, "TOKEN_FILE", d / "schwab_token.json"),
            mock.patch.object(sq, "LOCK_FILE", d / "schwab_token.lock"),
            mock.patch.object(sq, "KILL_FILE", d / "schwab_kill.json"),
            mock.patch.object(sq, "BUDGET_FILE", d / "schwab_budget.json"),
            mock.patch.object(sq, "PAGE_DEDUPE_FILE", d / "schwab_page_dedupe.json"),
            mock.patch.object(sq, "REVOKED_FILE", d / "schwab_revoked.json"),
            mock.patch.object(sq, "QUOTE_LOG", d / "schwab_quotes.jsonl"),
            mock.patch.object(sq, "SCHWAB_QUOTES_ENABLED", True),
            mock.patch.dict(os.environ, {"SCHWAB_APP_KEY": APP_KEY, "SCHWAB_APP_SECRET": APP_SECRET}),
        ]
        for p in self.patches:
            p.start()
        self.http = types.SimpleNamespace(get=mock.MagicMock(name="get"), post=mock.MagicMock(name="post"))
        p = mock.patch.object(sq, "requests", self.http)
        p.start()
        self.patches.append(p)
        self.slack = []
        p = mock.patch.object(sq, "_send_slack", side_effect=lambda t: self.slack.append(t) or True)
        p.start()
        self.patches.append(p)
        sq._latencies.clear()
        sq._refresh_latencies.clear()
        sq._last_reasons.clear()
        sq._known_secrets.clear()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def valid_token(self, **over):
        tok = {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer",
               "access_expires_at": time.time() + 1800, "refresh_issued_at": time.time(),
               "scope_ok": True, "refresh_dead": False, "written_at": time.time()}
        tok.update(over)
        sq.write_token(tok)
        return tok

    def route_get(self, quotes_resp=None, accounts_resp=None):
        def _get(url, **kw):
            if url == sq.ACCOUNTS_URL:
                if isinstance(accounts_resp, Exception):
                    raise accounts_resp
                return accounts_resp or FakeResp(403)
            if isinstance(quotes_resp, Exception):
                raise quotes_resp
            return quotes_resp
        self.http.get.side_effect = _get


class TestSymbolMap(unittest.TestCase):
    def test_round_trip_and_share_classes(self):
        pairs = {"AAPL": "AAPL", "BRK.B": "BRK/B", "BRK.A": "BRK/A", "BF.B": "BF/B", "SPY": "SPY"}
        for alp, sch in pairs.items():
            self.assertEqual(sq.to_schwab(alp), sch)
            self.assertEqual(sq.to_alpaca(sch), alp)
            self.assertEqual(sq.to_alpaca(sq.to_schwab(alp)), alp)
        self.assertEqual(len(set(map(sq.to_schwab, pairs))), len(pairs))  # one-to-one, no collisions

    def test_unmappable(self):
        self.assertIsNone(sq.to_schwab("BTC/USD"))   # an Alpaca symbol with "/" never maps
        self.assertIsNone(sq.to_alpaca("BRK.B"))     # a Schwab symbol with "." never maps
        self.assertIsNone(sq.to_schwab(""))
        self.assertIsNone(sq.to_schwab("A B"))


class TestTokenFile(Base):
    def test_mode_600_even_with_open_umask(self):
        old = os.umask(0)
        try:
            self.valid_token()
            self.valid_token(access_token="X2")  # replace path also 600
        finally:
            os.umask(old)
        self.assertEqual(os.stat(sq.TOKEN_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(sq.TOKEN_FILE.read_text())["access_token"], "X2")
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir() if p.name.endswith(".tmp")], [])

    def test_concurrent_refresh_does_one_refresh(self):
        self.valid_token(access_expires_at=time.time() - 10, scope_ok=False)

        def slow_post(url, **kw):
            time.sleep(0.3)
            return FakeResp(200, {"access_token": NEW_ACCESS, "refresh_token": REFRESH, "expires_in": 1800})
        self.http.post.side_effect = slow_post
        self.route_get(accounts_resp=FakeResp(401))
        results = []

        def worker():
            with sq._FileLock():
                results.append(sq._ensure_token_locked())
        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(self.http.post.call_count, 1)
        self.assertEqual(results, [(NEW_ACCESS, None), (NEW_ACCESS, None)])
        self.assertTrue(json.loads(sq.TOKEN_FILE.read_text())["scope_ok"])

    def test_lock_timeout_falls_back(self):
        self.valid_token()
        with sq._FileLock():
            with mock.patch.object(sq, "_lock_deadline_s", return_value=0.05):
                out = sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(out, {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "LOCK_TIMEOUT"})


class TestScopeProof(Base):
    def test_scope_200_deletes_token_kills_pages_no_exception(self):
        self.valid_token(access_expires_at=time.time() - 10, scope_ok=False)
        self.http.post.return_value = FakeResp(200, {"access_token": NEW_ACCESS, "refresh_token": REFRESH, "expires_in": 1800})
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={})), accounts_resp=FakeResp(200, [{"accountNumber": "1"}]))
        out = sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(out, {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "scope_allowed"})
        self.assertFalse(sq.TOKEN_FILE.exists())
        kill = json.loads(sq.KILL_FILE.read_text())
        self.assertEqual(kill["date"], sq._et_today())
        self.assertEqual(len(self.slack), 1)
        # no quote call was made with the over-scoped token
        self.assertTrue(all(c.args[0] == sq.ACCOUNTS_URL for c in self.http.get.call_args_list))
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertTrue(sq.last_reasons()["AAPL"].startswith("killed"))
        self.assertEqual(len(self.slack), 1)  # paged once

    def test_scope_inconclusive_falls_back_and_retries(self):
        self.valid_token(scope_ok=False)
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={})), accounts_resp=FakeResp(500))
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "scope_unproven"})
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={})), accounts_resp=FakeResp(403))
        self.assertIn("AAPL", sq.nbbo(["AAPL"], {"AAPL": 5}))


class TestQuoteCalls(Base):
    def test_quote_401_writes_kill_state(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(401))
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "auth_401"})
        self.assertEqual(json.loads(sq.KILL_FILE.read_text())["date"], sq._et_today())
        self.assertEqual(len(self.slack), 1)
        self.http.get.reset_mock()
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(self.http.get.call_count, 0)  # disabled for the rest of the day

    def test_429_falls_back_and_counts(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(429, headers={"Retry-After": "30"}))
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "http_429"})
        b = json.loads(sq.BUDGET_FILE.read_text())
        self.assertEqual(b["count_429"]["n"], 1)
        self.assertGreater(b["blocked_until"], time.time())
        self.assertFalse(sq.KILL_FILE.exists())
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(sq.last_reasons(), {"AAPL": "rate_backoff"})

    def test_budget_from_headers(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={}), headers={"X-RateLimit-Limit": "2", "X-RateLimit-Remaining": "0"}))
        self.assertIn("AAPL", sq.nbbo(["AAPL"], {"AAPL": 5}))
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "rate_budget"})

    def test_budget_conservative_limit_when_header_absent(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={}), headers={"X-RateLimit-Limit": "50"}))
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={}), headers={"X-RateLimit-Limit": "120"}))
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={})))
        sq.nbbo(["AAPL"], {"AAPL": 5})
        b = json.loads(sq.BUDGET_FILE.read_text())
        self.assertEqual(sq._budget_capacity(b), 50.0)

    def test_budget_unknown_allows_one_batch(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={}, MSFT={})))
        out = sq.nbbo(["AAPL", "MSFT"], {"AAPL": 5, "MSFT": 5})
        self.assertEqual(set(out), {"AAPL", "MSFT"})
        self.assertEqual(self.http.get.call_count, 1)

    def test_validity_rules(self):
        self.valid_token()
        old_ms = int((time.time() - 120) * 1000)
        body = qbody(**{"CROS": {"bidPrice": 10.1, "askPrice": 10.0},
                        "ONE": {"bidPrice": 0.0},
                        "ZERO": {"askSize": 0},
                        "LOCK": {"bidPrice": 50.0, "askPrice": 50.0},
                        "OLD": {"quoteTime": old_ms},
                        "NOB": {},
                        "BRK/B": {"bidPrice": 480.0, "askPrice": 480.2}})
        self.route_get(quotes_resp=FakeResp(200, body))
        syms = ["CROS", "ONE", "ZERO", "LOCK", "OLD", "NOB", "BRK.B"]
        bounds = {s: 10 for s in syms if s != "NOB"}
        out = sq.nbbo(syms, bounds)
        self.assertEqual(set(out), {"LOCK", "BRK.B"})
        self.assertEqual(out["LOCK"].spread, 0.0)
        self.assertTrue(out["LOCK"].locked)
        self.assertAlmostEqual(out["BRK.B"].spread, 0.2, places=6)
        self.assertEqual(out["BRK.B"].schwab_symbol, "BRK/B")
        self.assertIsNotNone(out["BRK.B"].quote_time.tzinfo)
        self.assertEqual(sq.last_reasons(), {"CROS": "crossed", "ONE": "one_sided", "ZERO": "zero_size",
                                             "OLD": "stale", "NOB": "no_bound"})

    def test_clock_skew_beyond_bound_is_not_used_and_pages_once(self):
        self.valid_token()
        future_ms = int((time.time() + 30) * 1000)
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={"quoteTime": future_ms}, MSFT={})))
        out = sq.nbbo(["AAPL", "MSFT"], {"AAPL": 10, "MSFT": 10})
        self.assertEqual(out, {})   # the clock disagreement applies to the whole batch
        self.assertEqual(sq.last_reasons(), {"AAPL": "clock_skew", "MSFT": "clock_skew"})
        sq.nbbo(["AAPL"], {"AAPL": 10})
        self.assertEqual(sum("clock" in s for s in self.slack), 1)

    def test_clock_lag_inside_bound_is_corrected_not_ignored(self):
        # server clock 4 s behind Schwab: one quote stamped 4 s "in the future", another truly 8.5 s old
        self.valid_token()
        now = time.time()
        body = qbody(SPY={"quoteTime": int((now + 4) * 1000)}, AAPL={"quoteTime": int((now - 4.5) * 1000)})
        self.route_get(quotes_resp=FakeResp(200, body))
        out = sq.nbbo(["SPY", "AAPL"], {"SPY": 5, "AAPL": 5})
        self.assertEqual(set(out), {"SPY"})
        self.assertEqual(sq.last_reasons(), {"AAPL": "stale"})

    def test_bad_last_price_is_not_used(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={"lastPrice": 0.0})))
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 10}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "bad_last"})

    def test_deadline_wrapper_returns_fallback_without_waiting(self):
        self.valid_token()
        gate = __import__("threading").Event()

        def _slow(url, **kw):
            gate.wait(5)
            return FakeResp(200, qbody(AAPL={}))
        self.http.get.side_effect = _slow
        t0 = time.monotonic()
        quotes, reasons = sq.nbbo_with_reasons(["AAPL"], {"AAPL": 10}, deadline_s=0.2)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual((quotes, reasons), ({}, {"AAPL": "deadline"}))
        gate.set()

    def test_nan_or_inf_bound_is_no_bound(self):
        self.valid_token()
        old_ms = int((time.time() - 3600) * 1000)
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={"quoteTime": old_ms}, MSFT={"quoteTime": old_ms})))
        out = sq.nbbo(["AAPL", "MSFT"], {"AAPL": float("nan"), "MSFT": float("inf")})
        self.assertEqual(out, {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "no_bound", "MSFT": "no_bound"})

    def test_deadline_path_never_raises_and_keeps_reasons(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={})))
        for bad in (float("nan"), "x", -1):
            self.assertEqual(sq.nbbo_with_reasons(["AAPL"], {"AAPL": 10}, deadline_s=bad),
                             ({}, {"AAPL": "internal_error"}))
        quotes, reasons = sq.nbbo_with_reasons((s for s in ["AAPL"]), {"AAPL": 10}, deadline_s=5)
        self.assertEqual(set(quotes), {"AAPL"})

    def test_reasons_returned_per_call(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={"askSize": 0})))
        quotes, reasons = sq.nbbo_with_reasons(["AAPL"], {"AAPL": 10})
        self.assertEqual((quotes, reasons), ({}, {"AAPL": "zero_size"}))

    def test_failed_page_send_is_not_marked_sent(self):
        calls = []

        def _boom(t):
            calls.append(t)
            return False   # alerts.send_slack_delivery reports a failed delivery
        with mock.patch.object(sq, "_send_slack", side_effect=_boom):
            sq._page("x", "first")
        sq._page("x", "second")
        self.assertEqual(calls, ["first"])
        self.assertEqual(self.slack, ["second"])

    def test_missing_symbol(self):
        self.valid_token()
        body = qbody(AAPL={})
        body["errors"] = {"invalidSymbols": ["ZZZZ"]}
        self.route_get(quotes_resp=FakeResp(200, body))
        out = sq.nbbo(["AAPL", "ZZZZ"], {"AAPL": 5, "ZZZZ": 5})
        self.assertEqual(set(out), {"AAPL"})
        self.assertEqual(sq.last_reasons(), {"ZZZZ": "MISSING_IN_RESPONSE"})

    def test_unexpected_shape(self):
        self.valid_token()
        body = qbody(AAPL={}, MSFT={})
        del body["MSFT"]["quote"]["bidSize"]
        self.route_get(quotes_resp=FakeResp(200, body))
        self.assertEqual(sq.nbbo(["AAPL", "MSFT"], {"AAPL": 5, "MSFT": 5}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "shape", "MSFT": "shape"})
        self.assertEqual(len(self.slack), 1)
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(len(self.slack), 1)  # once a day
        self.route_get(quotes_resp=FakeResp(200, bad_json=True))
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(sq.last_reasons(), {"AAPL": "shape"})

    def test_crc_line_written_and_verifiable(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={}, MSFT={"bidPrice": 0.0})))
        sq.nbbo(["AAPL", "MSFT"], {"AAPL": 5, "MSFT": 5})
        lines = sq.QUOTE_LOG.read_text().splitlines()
        self.assertEqual(len(lines), 2)  # every received quote is stored, valid or not
        for ln in lines:
            self.assertTrue(sq.verify_line(ln))
            d = json.loads(ln)
            self.assertEqual(set(d), {"symbol", "quote_time", "bid", "ask", "bid_size", "ask_size", "last", "fetch_time", "crc"})
        tampered = json.loads(lines[0])
        tampered["bid"] += 0.01
        self.assertFalse(sq.verify_line(json.dumps(tampered)))
        self.assertFalse(sq.verify_line(lines[0][:-5]))

    def test_disabled_and_missing_credentials(self):
        with mock.patch.object(sq, "SCHWAB_QUOTES_ENABLED", False):
            self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
            self.assertEqual(sq.last_reasons(), {"AAPL": "disabled"})
        with mock.patch.dict(os.environ, {"SCHWAB_APP_SECRET": ""}):
            sq.nbbo(["AAPL"], {"AAPL": 5})
            self.assertEqual(sq.last_reasons(), {"AAPL": "no_key"})
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(sq.last_reasons(), {"AAPL": "no_token"})
        self.assertEqual(self.http.get.call_count + self.http.post.call_count, 0)

    def test_nbbo_never_raises(self):
        self.valid_token()
        self.route_get(quotes_resp=RuntimeError("boom"))
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "network_error"})
        self.valid_token(access_expires_at=time.time() - 10)
        self.http.post.side_effect = ConnectionError("down")
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "refresh_error"})
        with mock.patch.object(sq, "_ensure_token_locked", side_effect=KeyError("x")):
            self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})
            self.assertEqual(sq.last_reasons(), {"AAPL": "internal_error"})
        self.assertEqual(sq.nbbo(None, None), {})
        self.assertIsInstance(sq.status(), dict)

    def test_timeout_resets_latency_history(self):
        self.valid_token()
        sq._latencies.extend([0.1] * 10)
        self.assertLess(sq._timeouts()[1], sq._ALPACA_QUOTE_TIMEOUT)

        class ReadTimeout(Exception):
            pass
        self.route_get(quotes_resp=ReadTimeout("slow"))
        sq.nbbo(["AAPL"], {"AAPL": 5})
        self.assertEqual(sq.last_reasons(), {"AAPL": "timeout"})
        self.assertEqual(sq._timeouts(), (sq._ALPACA_QUOTE_TIMEOUT, sq._ALPACA_QUOTE_TIMEOUT))


def ibody(**entries):
    """Index response shaped like the live 2026-11-04 read: {"$VIX": {"symbol", "assetMainType": "INDEX",
    "realtime": true, "quote": {"lastPrice", "tradeTime"(epoch ms)}}}. Overrides go to the entry or, under
    key "quote", to the quote."""
    now_ms = int(time.time() * 1000)
    out = {}
    for sym, over in entries.items():
        over = dict(over or {})
        q = {"lastPrice": 16.61, "closePrice": 16.61, "tradeTime": now_ms}
        q.update(over.pop("quote", {}))
        e = {"symbol": sym, "assetMainType": "INDEX", "realtime": True, "quote": q}
        e.update(over)
        out[sym] = e
    return out


class TestIndexValues(Base):
    """REGIME-SIZE-1104 rev 3.3-3.6: CBOE index values through their own path."""

    def test_live_shape_with_no_bid_ask_is_accepted(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(200, ibody(**{"$VIX": {}, "$VIX3M": {"quote": {"lastPrice": 18.8}}})))
        vals, reasons = sq.index_last_with_reasons(["$VIX", "$vix3m"], {"$VIX": 600, "$VIX3M": 600})
        self.assertEqual(reasons, {})
        self.assertEqual(vals["$VIX"].last, 16.61)
        self.assertEqual(vals["$VIX3M"].last, 18.8)
        self.assertIsNotNone(vals["$VIX"].trade_time.tzinfo)
        self.assertEqual(vals["$VIX"].source, "schwab_index")
        self.assertEqual(self.http.get.call_args.kwargs["params"]["symbols"], "$VIX,$VIX3M")

    def test_rejections_each_have_a_reason(self):
        self.valid_token()
        old_ms = int((time.time() - 1200) * 1000)
        body = ibody(**{"$A": {"realtime": False}, "$B": {"assetMainType": "EQUITY"},
                        "$C": {"quote": {"lastPrice": 0}}, "$D": {"quote": {"lastPrice": float("nan")}},
                        "$E": {"quote": {"tradeTime": None}}, "$F": {"quote": {"tradeTime": old_ms}},
                        "$G": {"quote": {"lastPrice": True}}, "$H": {}})
        self.route_get(quotes_resp=FakeResp(200, body))
        syms = ["$A", "$B", "$C", "$D", "$E", "$F", "$G", "$H", "$ZZ", "VIX"]
        bounds = {s: 600 for s in syms if s != "$H"}
        vals, reasons = sq.index_last_with_reasons(syms, bounds)
        self.assertEqual(vals, {})
        self.assertEqual(reasons, {"$A": "not_realtime", "$B": "not_index", "$C": "bad_last", "$D": "bad_last",
                                   "$E": "bad_timestamp", "$F": "stale", "$G": "bad_last", "$H": "no_bound",
                                   "$ZZ": "MISSING_IN_RESPONSE", "VIX": "not_index_symbol"})

    def test_future_trade_time_beyond_bound_is_clock_skew(self):
        self.valid_token()
        future_ms = int((time.time() + 900) * 1000)
        self.route_get(quotes_resp=FakeResp(200, ibody(**{"$VIX": {"quote": {"tradeTime": future_ms}}})))
        vals, reasons = sq.index_last_with_reasons(["$VIX"], {"$VIX": 600})
        self.assertEqual((vals, reasons), ({}, {"$VIX": "clock_skew"}))

    def test_shape_and_symbol_mismatch(self):
        self.valid_token()
        body = ibody(**{"$VIX": {}})
        body["$VIX"]["symbol"] = "$VXX"
        self.route_get(quotes_resp=FakeResp(200, body))
        self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "shape"}))
        self.route_get(quotes_resp=FakeResp(200, bad_json=True))
        self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "shape"}))
        self.assertEqual(sum("index values" in t for t in self.slack), 1)   # paged once a day, own kind

    def test_disabled_flag_makes_no_call(self):
        self.valid_token()
        with mock.patch.object(sq, "SCHWAB_INDEX_ENABLED", False):
            self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "disabled"}))
        self.assertEqual(self.http.get.call_count + self.http.post.call_count, 0)

    def test_auth_refusal_kills_400_does_not(self):
        self.valid_token()
        self.route_get(quotes_resp=FakeResp(400))
        self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "http_400"}))
        self.assertFalse(sq.KILL_FILE.exists())
        self.route_get(quotes_resp=FakeResp(403))
        self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "auth_403"}))
        self.assertEqual(json.loads(sq.KILL_FILE.read_text())["date"], sq._et_today())
        self.http.get.reset_mock()
        self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600})[1]["$VIX"], "killed:index_http_403")
        self.assertEqual(self.http.get.call_count, 0)
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 5}), {})                 # same token: whole module off today
        self.assertTrue(sq.last_reasons()["AAPL"].startswith("killed"))

    def test_deadline_lock_timeout_and_no_shared_reasons(self):
        self.valid_token()
        sq._last_reasons.update({"AAPL": "sentinel"})
        gate = threading.Event()

        def _slow(url, **kw):
            gate.wait(5)
            return FakeResp(200, ibody(**{"$VIX": {}}))
        self.http.get.side_effect = _slow
        t0 = time.monotonic()
        self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}, deadline_s=0.2), ({}, {"$VIX": "deadline"}))
        self.assertLess(time.monotonic() - t0, 2.0)
        gate.set()
        with sq._FileLock():
            with mock.patch.object(sq, "_lock_deadline_s", return_value=0.05):
                self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "LOCK_TIMEOUT"}))
        self.assertEqual(sq.last_reasons(), {"AAPL": "sentinel"})              # nbbo's shared reasons untouched

    def test_never_raises(self):
        self.valid_token()
        self.route_get(quotes_resp=RuntimeError("boom"))
        self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "network_error"}))
        with mock.patch.object(sq, "_ensure_token_locked", side_effect=KeyError("x")):
            self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}), ({}, {"$VIX": "internal_error"}))
        self.assertEqual(sq.index_last_with_reasons(None, None), ({}, {}))
        for bad in (float("nan"), "x", -1):
            self.assertEqual(sq.index_last_with_reasons(["$VIX"], {"$VIX": 600}, deadline_s=bad),
                             ({}, {"$VIX": "internal_error"}))

    def test_index_symbols_do_not_change_the_stock_symbol_map(self):
        self.assertIsNone(sq.to_schwab("$VIX"))
        self.assertIsNone(sq.to_alpaca("$VIX"))


class TestRedaction(Base):
    def test_every_failure_path_is_redacted(self):
        leak = (f"conn failed Authorization: Bearer {ACCESS} refresh_token={REFRESH} code={CODE} "
                f"secret {APP_SECRET} {{'access_token': '{NEW_ACCESS}'}}")
        with self.assertLogs(sq.logger, level=logging.DEBUG) as cm:
            # quote network error carrying secrets in the exception text
            self.valid_token()
            self.route_get(quotes_resp=RuntimeError(leak))
            sq.nbbo(["AAPL"], {"AAPL": 5})
            # refresh failure (exception) and refresh HTTP 401
            self.valid_token(access_expires_at=time.time() - 10)
            self.http.post.side_effect = RuntimeError(leak)
            sq.nbbo(["AAPL"], {"AAPL": 5})
            self.http.post.side_effect = None
            self.http.post.return_value = FakeResp(401, {"error": leak})
            sq.nbbo(["AAPL"], {"AAPL": 5})
            # scope proof call error
            self.valid_token(scope_ok=False)
            self.route_get(accounts_resp=RuntimeError(leak))
            sq.nbbo(["AAPL"], {"AAPL": 5})
            # scope proof 200
            self.route_get(accounts_resp=FakeResp(200))
            sq.nbbo(["AAPL"], {"AAPL": 5})
            # quote 401 / 429 / 500 / bad json / shape
            for resp in (FakeResp(401), FakeResp(429), FakeResp(500), FakeResp(200, bad_json=True), FakeResp(200, {"AAPL": leak})):
                sq.KILL_FILE.unlink(missing_ok=True)
                self.valid_token()
                self.route_get(quotes_resp=resp)
                sq.nbbo(["AAPL"], {"AAPL": 5})
            # internal error with a traceback carrying secrets
            with mock.patch.object(sq, "_ensure_token_locked", side_effect=ValueError(leak)):
                sq.nbbo(["AAPL"], {"AAPL": 5})
            sq.logger.error("direct %s", leak, exc_info=(ValueError, ValueError(leak), None))
        text = "\n".join(cm.output) + "\n".join(self.slack)
        self.assertGreater(len(cm.output), 5)
        for s in SECRET_STRINGS:
            self.assertNotIn(s, text, f"secret leaked: {s}")

    def test_redact_function(self):
        sq._remember_secret(ACCESS)
        s = sq.redact(f"https://127.0.0.1/?code={CODE}&session=x Authorization: Basic Zm9vOmJhcg== "
                      f'"refresh_token": "{REFRESH}" {APP_SECRET} raw {ACCESS}')
        for v in SECRET_STRINGS + ("Zm9vOmJhcg==",):
            self.assertNotIn(v, s)


class TestImportsAndLogin(Base):
    def _imports(self, path):
        tree = ast.parse(Path(path).read_text())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
                names += [f"{node.module}.{a.name}" for a in node.names]
        return names

    def test_no_execution_imports(self):
        for path in (_ROOT / "data" / "schwab_quotes.py", _ROOT / "scripts" / "schwab_login.py"):
            for n in self._imports(path):
                self.assertFalse(n == "execution" or n.startswith("execution.") or ".execution" in n
                                 or n.endswith("broker"), f"{path.name} imports {n}")
            src = Path(path).read_text()
            for forbidden in ("TradingClient", "submit_order", "place_order"):
                self.assertNotIn(forbidden, src)

    def test_login_flow_state_check_and_scope_proof(self):
        from scripts import schwab_login as sl
        state = "STATE_OK"
        with self.assertRaises(ValueError):
            sl.parse_redirect(f"https://127.0.0.1/?code={CODE}&state=WRONG", state)
        with self.assertRaises(ValueError):
            sl.parse_redirect(f"https://evil.example/?code={CODE}&state={state}", state)
        self.assertEqual(sl.parse_redirect(f"https://127.0.0.1/?code=C0%40&state={state}", state), "C0@")
        self.http.post.return_value = FakeResp(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 1800})
        self.route_get(accounts_resp=FakeResp(403))
        printed = []
        with mock.patch.object(sl.secrets, "token_urlsafe", return_value=state), \
                mock.patch("builtins.input", return_value=f"https://127.0.0.1/?code={CODE}&state={state}"), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = sl.main([])
        self.assertEqual(rc, 0)
        tok = json.loads(sq.TOKEN_FILE.read_text())
        self.assertTrue(tok["scope_ok"])
        self.assertEqual(os.stat(sq.TOKEN_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(self.http.post.call_args.kwargs["data"]["grant_type"], "authorization_code")
        out = "\n".join(printed)
        for s in (APP_SECRET, ACCESS, REFRESH, CODE):
            self.assertNotIn(s, out)

    def test_code_plus_kept_and_percent_decoded_once(self):
        from scripts import schwab_login as sl
        state = "STATE_OK"
        self.assertEqual(sl.parse_redirect(f"https://127.0.0.1/?code=A+B%2BC%40&session=x&state={state}", state),
                         "A+B+C@")
        self.assertEqual(sl.parse_redirect(f"https://127.0.0.1/?state={state}&code=C0%2540", state), "C0%40")

    def test_refused_exchange_shows_schwab_reason_redacted(self):
        from scripts import schwab_login as sl
        state = "STATE_OK"
        self.http.post.return_value = FakeResp(400, {"error": "invalid_client",
                                                      "error_description": f"bad client {APP_SECRET}"})
        printed = []
        with mock.patch.object(sl.secrets, "token_urlsafe", return_value=state), \
                mock.patch("builtins.input", return_value=f"https://127.0.0.1/?code={CODE}&state={state}"), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = sl.main([])
        self.assertEqual(rc, 1)
        out = "\n".join(printed)
        self.assertIn("HTTP 400", out)
        self.assertIn("error=invalid_client", out)
        self.assertIn("re-copy the App Secret", out)
        self.assertIn("SCHWAB_APP_SECRET length", out)
        for secret in (APP_SECRET, CODE):
            self.assertNotIn(secret, out)
        self.assertEqual(self.http.post.call_count, 1)          # the code is spent exactly once

    def test_revoke_clears_state(self):
        from scripts import schwab_login as sl
        self.valid_token()
        sq._write_kill("x")
        sq._atomic_write_json(sq.BUDGET_FILE, {"tokens": 1})
        sq._atomic_write_json(sq.PAGE_DEDUPE_FILE, {"date": "x", "kinds": []})
        with mock.patch("builtins.print"):
            self.assertEqual(sl.main(["--revoke"]), 0)
        for p in (sq.TOKEN_FILE, sq.KILL_FILE, sq.BUDGET_FILE, sq.PAGE_DEDUPE_FILE):
            self.assertFalse(p.exists())
        self.assertEqual(len(self.slack), 1)
        self.assertTrue(sq.REVOKED_FILE.exists())

    def test_revoked_marker_blocks_quotes_and_in_flight_refresh(self):
        tok = self.valid_token(access_expires_at=0)

        def _post(url, **kw):   # a refresh that completes after a revoke landed
            sq._atomic_write_json(sq.REVOKED_FILE, {"revoked_at": 1})
            return FakeResp(200, {"access_token": "NEWACCESS", "refresh_token": REFRESH, "expires_in": 1800})
        self.http.post.side_effect = _post
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={})))
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 10}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "revoked"})
        self.assertEqual(json.loads(sq.TOKEN_FILE.read_text())["access_token"], tok["access_token"])
        self.assertEqual(sq.nbbo(["AAPL"], {"AAPL": 10}), {})
        self.assertEqual(sq.last_reasons(), {"AAPL": "revoked"})

    def test_login_clears_revoked_marker(self):
        from scripts import schwab_login as sl
        sq._atomic_write_json(sq.REVOKED_FILE, {"revoked_at": 1})
        self.http.post.return_value = FakeResp(200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 1800})
        self.route_get(accounts_resp=FakeResp(403))
        with mock.patch.object(sl.secrets, "token_urlsafe", return_value="S"), \
                mock.patch("builtins.input", return_value=f"https://127.0.0.1/?code={CODE}&state=S"), \
                mock.patch("builtins.print"):
            self.assertEqual(sl.main([]), 0)
        self.assertFalse(sq.REVOKED_FILE.exists())

    def test_refresh_expiry_reminder_pages_command_only(self):
        self.valid_token(refresh_issued_at=time.time() - sq.REFRESH_LIFE_S + sq.REFRESH_REMIND_S / 2)
        self.route_get(quotes_resp=FakeResp(200, qbody(AAPL={})))
        sq.nbbo(["AAPL"], {"AAPL": 10})
        sq.nbbo(["AAPL"], {"AAPL": 10})
        rem = [t for t in self.slack if "weekly login" in t]
        self.assertEqual(len(rem), 1)
        self.assertIn("schwab_login.py", rem[0])
        self.assertNotIn("http", rem[0])


if __name__ == "__main__":
    unittest.main()
