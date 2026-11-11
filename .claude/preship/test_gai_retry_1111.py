#!/usr/bin/env python3
# ruff: noqa: E501
"""Offline tests for GAI-RETRY-1111: Google's full refusal reason (quota id, limit, retryDelay) survives into the error;
the GAI retry waits the time Google asked for instead of a fixed 6 s; a daily quota or an over-long wait skips the
retry. No network: _curl's subprocess.run and time.sleep are stubbed. Run: python3 .claude/preship/test_gai_retry_1111.py
Signed: Claude, 2026-11-11.
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preship_audit as pa  # noqa: E402

KEY = "AIzaTESTKEY-not-real-0123456789"


def _google_429(retry="23s", quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", limit="15"):
    """The shape of a real Gemini 429 body (google.rpc QuotaFailure + RetryInfo)."""
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                                "quotaId": quota_id, "quotaValue": limit}]},
               {"@type": "type.googleapis.com/google.rpc.Help", "links": [{"url": "https://ai.google.dev/"}]}]
    if retry is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "You exceeded your current quota, please check your plan and billing details. " * 3,
                      "details": details}}


def _ok():
    return {"candidates": [{"content": {"parts": [{"text": "VERDICT: APPROVE"}]}}]}


class ErrorSummary(unittest.TestCase):
    def test_keeps_quota_and_retry_delay(self):
        msg, retry, per_day = pa._error_summary(_google_429()["error"])
        self.assertIn("429", msg)
        self.assertIn("RESOURCE_EXHAUSTED", msg)
        self.assertIn("quota=GenerateRequestsPerMinutePerProjectPerModel-FreeTier limit=15", msg)
        self.assertIn("retryDelay=23s", msg)
        self.assertEqual(retry, 23.0)
        self.assertFalse(per_day)

    def test_key_straddling_the_message_cut_is_fully_masked(self):
        # cold-2nd round 1: the message was cut at 240 BEFORE scrubbing, leaving the key's first 15 characters.
        for pad in (200, 225, 239, 240):
            msg = pa._error_summary({"code": 400, "message": "x" * pad + KEY}, KEY)[0]
            self.assertNotIn(KEY[:8], msg, pad)
        self.assertNotIn(KEY[:8], pa._error_summary("y" * 290 + KEY, KEY)[0])

    def test_odd_shapes_keep_a_summary(self):
        self.assertIn("foo", pa._error_summary({"foo": "bar"})[0])        # no code/status/message -> the dict itself
        self.assertIn("500", pa._error_summary({"code": 500, "details": 5})[0])   # non-list details: no crash
        self.assertIn("500", pa._error_summary({"code": 500, "details": [{"violations": 7}]})[0])
        two = {"code": 429, "details": [{"retryDelay": "40s"}, {"retryDelay": "12s"}]}
        self.assertEqual(pa._error_summary(two)[1], 12.0)                   # the soonest delay wins

    def test_daily_quota_flagged(self):
        _m, retry, per_day = pa._error_summary(_google_429("3600s", "GenerateRequestsPerDayPerProjectPerModel-FreeTier")["error"])
        self.assertTrue(per_day)
        self.assertEqual(retry, 3600.0)

    def test_fractional_and_missing_delay(self):
        self.assertEqual(pa._error_summary(_google_429("1.5s")["error"])[1], 1.5)
        self.assertIsNone(pa._error_summary(_google_429(None)["error"])[1])
        self.assertIsNone(pa._error_summary(_google_429("soon")["error"])[1])   # unparseable -> no delay, no crash

    def test_non_google_shapes(self):
        groq = {"message": "Request too large for model openai/gpt-oss-120b ... Requested 8985 > Limit 8000",
                "type": "tokens", "code": "rate_limit_exceeded"}
        msg, retry, per_day = pa._error_summary(groq)
        self.assertRegex(msg, pa._TOO_LARGE_RE)      # Gro's size detection still sees Groq's own text
        self.assertIsNone(retry)
        self.assertFalse(per_day)
        self.assertEqual(pa._error_summary("plain string")[0], "plain string")
        self.assertIn("x", pa._error_summary({"code": 500, "details": ["x", None, {"violations": [None]}], "message": "x"})[0])


class CurlRaisesProviderError(unittest.TestCase):
    def _run(self, body):
        cp = subprocess.CompletedProcess([], 0, stdout=json.dumps(body), stderr="")
        with mock.patch.object(pa.subprocess, "run", return_value=cp):
            return pa._curl("https://generativelanguage.googleapis.com/v1beta/models/m:generateContent",
                            [f"x-goog-api-key: {KEY}"], {}, KEY)

    def test_google_429_reason_survives(self):
        with self.assertRaises(pa.ProviderError) as cm:
            self._run(_google_429())
        e = cm.exception
        self.assertIsInstance(e, RuntimeError)        # every existing `except RuntimeError` still catches it
        self.assertEqual(e.retry_after, 23.0)
        self.assertIn("retryDelay=23s", str(e))
        self.assertIn("quota=GenerateRequestsPerMinute", str(e))

    def test_key_echoed_in_error_is_masked(self):
        body = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": f"API key {KEY} not valid"}}
        with self.assertRaises(pa.ProviderError) as cm:
            self._run(body)
        self.assertNotIn(KEY, str(cm.exception))


class GaiRetryWait(unittest.TestCase):
    """_gai: ladder of models; one retry of the whole ladder after a transient failure."""

    def _gai(self, plan):
        """plan: list of replies (dict) consumed in call order; a dict with 'error' becomes the real ProviderError."""
        calls = []

        def fake_curl(url, headers, body, key, timeout=120):
            calls.append(url.split("/models/")[1].split(":")[0])
            r = plan.pop(0) if plan else _google_429()
            if r.get("error"):
                m, ra, pd = pa._error_summary(r["error"], key)
                raise pa.ProviderError(pa._scrub(m, key)[:600], ra, pd, raw=pa._scrub(str(r["error"]), key)[:200])
            return r
        with mock.patch.object(pa, "_curl", fake_curl), mock.patch.object(pa.time, "sleep") as sl, \
                mock.patch.dict(os.environ, {"GEMINI_ALLOW_PAID": ""}):
            try:
                out, err = pa._gai("prompt", KEY), None
            except Exception as e:  # noqa: BLE001
                out, err = None, e
        return out, err, [c.args[0] for c in sl.call_args_list], calls

    def test_waits_soonest_retry_delay_plus_one(self):
        n = 6   # ladder length; first pass all 429 with different delays, retry succeeds on the first model
        first = [_google_429(f"{d}s") for d in (40, 12, 30, 50, 44, 41)]
        out, err, sleeps, calls = self._gai(first + [_ok()])
        self.assertIsNone(err)
        self.assertEqual(out, "VERDICT: APPROVE")
        self.assertEqual(sleeps, [13.0])
        self.assertEqual(len(calls), n + 1)

    def test_no_retry_delay_keeps_default_wait(self):
        out, _e, sleeps, _c = self._gai([_google_429(None)] * 6 + [_ok()])
        self.assertEqual(out, "VERDICT: APPROVE")
        self.assertEqual(sleeps, [pa._GAI_DEFAULT_WAIT_S])

    def test_daily_quota_on_every_model_skips_wait(self):
        day = _google_429("50000s", "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        _o, err, sleeps, calls = self._gai([day] * 6)
        self.assertIsInstance(err, pa.ProviderError)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(calls), 6)                 # one ladder pass, no retry
        self.assertIn("PerDay", str(err))

    def test_one_minute_quota_among_daily_still_waits(self):
        day = _google_429("50000s", "GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        _o, _e, sleeps, _c = self._gai([day] * 5 + [_google_429("20s")] + [_ok()])
        self.assertEqual(sleeps, [21.0])

    def test_wait_longer_than_window_skips_retry(self):
        _o, err, sleeps, calls = self._gai([_google_429(f"{pa._GAI_MAX_WAIT_S + 30}s")] * 6)
        self.assertIsInstance(err, pa.ProviderError)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(calls), 6)

    def test_boundary_wait_equal_to_cap_still_waits(self):
        _o, _e, sleeps, _c = self._gai([_google_429(f"{pa._GAI_MAX_WAIT_S - 1}s")] * 6 + [_ok()])
        self.assertEqual(sleeps, [float(pa._GAI_MAX_WAIT_S)])

    def test_error_names_every_model_and_masks_key(self):
        bad = {"error": {"code": 404, "status": "NOT_FOUND", "message": f"model retired (key {KEY})"}}
        _o, err, _s, _c = self._gai([bad] * 12)
        self.assertNotIn(KEY, str(err))
        for m in ("gemini-3.1-flash-lite", "gemini-flash-latest"):
            self.assertIn(m, str(err))

    def test_realistic_reason_survives_to_the_raised_error(self):
        # adversarial round 1: the long message came first and a 300/1500 cut dropped the quota id, the wait and
        # the last models. Now quota + retryDelay precede the message and all six models are named.
        day = _google_429("38s", "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "250")
        _o, err, _s, _c = self._gai([day] * 6)
        text = str(err)
        self.assertIn("quota=GenerateRequestsPerDayPerProjectPerModel-FreeTier limit=250", text)
        self.assertIn("retryDelay=38s", text)
        for m in ("gemini-3.1-flash-lite", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash",
                  "gemini-3-flash-preview", "gemini-flash-latest"):
            self.assertIn(f"{m}: 429 | RESOURCE_EXHAUSTED | quota=", text)

    def test_last_model_404_keeps_old_classification_even_with_paid_allowed(self):
        # adversarial round 1: classifying on the joined text made a 6 s retry + a paid pass reachable when the
        # last model 404'd. Classification is on the last model's own reason again, exactly as on origin/main.
        gone = {"error": {"code": 404, "status": "NOT_FOUND", "message": "model retired"}}
        calls = []

        def fake_curl(url, headers, body, key, timeout=120):
            calls.append(key)
            r = ([_google_429("10s")] * 5 + [gone])[(len(calls) - 1) % 6]
            m, ra, pd = pa._error_summary(r["error"], key)
            raise pa.ProviderError(m, ra, pd, raw=pa._scrub(str(r["error"]), key)[:200])
        with mock.patch.object(pa, "_curl", fake_curl), mock.patch.object(pa.time, "sleep") as sl, \
                mock.patch.dict(os.environ, {"GEMINI_ALLOW_PAID": "1"}):
            with self.assertRaises(pa.ProviderError):
                pa._gai("prompt", KEY, paid_key="PAIDKEY-not-real")
        self.assertEqual(len(calls), 6)                  # one free pass: no retry, no paid pass
        self.assertNotIn("PAIDKEY-not-real", calls)
        self.assertEqual(sl.call_args_list, [])

    def test_classification_parity_with_origin_main(self):
        # cold-2nd round 3: retry and paid decisions must classify origin/main's exact text (scrubbed str(error)[:200]).
        cases = [
            ({"code": 400, "message": "A" * 195 + " quota exceeded", "status": "INVALID_ARGUMENT"}, 6),
            ({"code": 403, "message": "short", "status": "PERMISSION_DENIED", "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED",
                 "metadata": {"quota_limit": "x"}}]}, 18),
            ({"code": 500, "message": "B" * 200, "status": "UNAVAILABLE"}, 6),
            ({"code": 400, "message": "bad", "status": "FAILED_PRECONDITION",
              "details": [{"violations": [{"quotaId": "X"}]}]}, 18),
        ]
        for err, want_calls in cases:
            calls = []

            def fake_curl(url, headers, body, key, timeout=120, err=err, calls=calls):
                calls.append(key)
                m, ra, pd = pa._error_summary(err, key)
                raise pa.ProviderError(m, ra, pd, raw=pa._scrub(str(err), key)[:200])
            old_text = str(err)[:200].lower()
            old_transient = any(w in old_text for w in ("429", "resource_exhausted", "quota", "rate limit",
                                                        "rate-limit", "503", "unavailable"))
            old_quota = any(w in old_text for w in ("429", "resource_exhausted", "quota", "rate limit", "rate-limit"))
            expect = 6 * (2 if old_transient else 1) + (6 if old_quota else 0)
            self.assertEqual(expect, want_calls)      # the hand-checked origin/main behaviour for this input
            with self.subTest(err=str(err)[:40]), mock.patch.object(pa, "_curl", fake_curl), \
                    mock.patch.object(pa.time, "sleep"), mock.patch.dict(os.environ, {"GEMINI_ALLOW_PAID": "1"}):
                with self.assertRaises(pa.ProviderError):
                    pa._gai("prompt", KEY, paid_key="PAIDKEY-not-real")
                self.assertEqual(len(calls), expect)

    def test_mixed_daily_and_minute_violations_are_not_daily(self):
        err = _google_429("20s")["error"]
        err["details"][0]["violations"].append({"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                                "quotaValue": "250"})
        self.assertFalse(pa._error_summary(err)[2])

    def test_non_transient_error_does_not_retry(self):
        bad = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad request"}}
        _o, err, sleeps, calls = self._gai([bad] * 6)
        self.assertIsNotNone(err)
        self.assertEqual(sleeps, [])
        self.assertEqual(len(calls), 6)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
