#!/usr/bin/env python3
# ruff: noqa: E501
"""Offline tests for NV-REVIEW-1110: the NVIDIA pair (Mistral Large 4 + MiniMax M3.1) as reviewer #2 when Codex is out
and as the GAI stand-in; the open-REJECT ledger (no reviewer shopping); key safety in _curl; the cold-2nd precondition.
No network: every provider call goes through a stubbed _curl / subprocess.run. Run: python3 .claude/preship/test_nv_review_1110.py
Signed: Claude, 2026-11-10. Design: logs/design_records/nv_review_2026-11-10.md
"""
import hashlib
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preship_audit as pa  # noqa: E402

APPROVE_TXT = "Checked the changed lines.\nVERDICT: APPROVE"
REJECT_TXT = "Line +3 divides by zero when qty is 0.\nVERDICT: REJECT — qty=0 crashes"
MISTRAL, MINIMAX = pa._NVIDIA_PAIR
KINDS = ("APPROVE", "REJECT", "empty", "timeout", "404")


def _reply(kind):
    """One NVIDIA reply of the given kind (a raise for transport kinds, as the real _curl does)."""
    if kind == "APPROVE":
        return {"choices": [{"message": {"content": APPROVE_TXT}}]}
    if kind == "REJECT":
        return {"choices": [{"message": {"content": REJECT_TXT}}]}
    if kind == "empty":   # the bake-off failure mode: content empty, a verdict only in the hidden reasoning
        return {"choices": [{"message": {"content": "", "reasoning_content": "Looks fine.\nVERDICT: APPROVE"}}]}
    if kind == "timeout":
        raise RuntimeError("curl to integrate.api.nvidia.com timed out after 314s")
    if kind == "404":
        return {"status": 404, "title": "Not Found", "detail": "Function not found for account"}
    raise AssertionError(kind)


class FakeNvidia:
    """Stands in for _curl on the NVIDIA URL. plan[model] = one kind, or a list of kinds per attempt."""

    def __init__(self, plan):
        self.plan = {m: (list(k) if isinstance(k, (list, tuple)) else [k]) for m, k in plan.items()}
        self.calls = []

    def __call__(self, url, headers, body, key, timeout=120):
        assert url == pa._NVIDIA_URL, url
        m = body["model"]
        self.calls.append((m, timeout, body["max_tokens"], body["messages"][-1]["content"]))
        kinds = self.plan[m]
        return _reply(kinds.pop(0) if len(kinds) > 1 else kinds[0])

    def n(self, model):
        return sum(1 for c in self.calls if c[0] == model)


def _expected(ka, kb):
    vs = [k for k in (ka, kb) if k in ("APPROVE", "REJECT")]
    if "REJECT" in vs:
        return "REJECT", ""
    if "APPROVE" in vs:
        return "APPROVE", f"{vs.count('APPROVE')}-of-2"
    return None, ""


class PairCombination(unittest.TestCase):
    def test_every_combination(self):
        for ka, kb in itertools.product(KINDS, KINDS):
            with self.subTest(mistral=ka, minimax=kb):
                fake = FakeNvidia({MISTRAL: ka, MINIMAX: kb})
                with mock.patch.object(pa, "_curl", fake):
                    res = pa._nvidia_pair("diff", "nvkey", "\nREMIND")
                want_v, want_agree = _expected(ka, kb)
                self.assertEqual(res["verdict"], want_v)
                self.assertEqual(res["agreement"], want_agree)
                self.assertEqual(res["endpoint_down"], ka in ("timeout", "404") and kb in ("timeout", "404"))
                self.assertEqual([r["id"] for r in res["models"]], [MISTRAL, MINIMAX])
                for model, kind in ((MISTRAL, ka), (MINIMAX, kb)):
                    # a model without a verdict is retried exactly once; a model with one is asked once
                    self.assertEqual(fake.n(model), 1 if kind in ("APPROVE", "REJECT") else 2, (model, kind))

    def test_both_unusable_has_no_verdict(self):
        for ka, kb in itertools.product(("empty", "timeout", "404"), repeat=2):
            with self.subTest(mistral=ka, minimax=kb), mock.patch.object(pa, "_curl", FakeNvidia({MISTRAL: ka, MINIMAX: kb})):
                self.assertIsNone(pa._nvidia_pair("diff", "nvkey")["verdict"])

    def test_retry_carries_the_reminder_and_can_land_a_verdict(self):
        fake = FakeNvidia({MISTRAL: ["empty", "APPROVE"], MINIMAX: "REJECT"})
        with mock.patch.object(pa, "_curl", fake):
            res = pa._nvidia_pair("diff", "nvkey", "\nREMIND")
        self.assertEqual(res["models"], [{"id": MISTRAL, "verdict": "APPROVE"}, {"id": MINIMAX, "verdict": "REJECT"}])
        self.assertEqual(res["verdict"], "REJECT")             # union-reject
        mistral_prompts = [c[3] for c in fake.calls if c[0] == MISTRAL]
        self.assertFalse(mistral_prompts[0].endswith("REMIND"))
        self.assertTrue(mistral_prompts[1].endswith("REMIND"))

    def test_one_of_two_recorded(self):
        with mock.patch.object(pa, "_curl", FakeNvidia({MISTRAL: "APPROVE", MINIMAX: "empty"})):
            res = pa._nvidia_pair("diff", "nvkey")
        self.assertEqual((res["verdict"], res["agreement"]), ("APPROVE", "1-of-2"))
        self.assertEqual(res["models"][1], {"id": MINIMAX, "verdict": "NO_VERDICT"})

    def test_reasoning_content_is_never_a_verdict(self):
        with mock.patch.object(pa, "_curl", FakeNvidia({MISTRAL: "empty", MINIMAX: "empty"})):
            res = pa._nvidia_pair("diff", "nvkey")
        self.assertIsNone(res["verdict"])
        self.assertEqual([r["verdict"] for r in res["models"]], ["NO_VERDICT", "NO_VERDICT"])
        self.assertFalse(res["endpoint_down"])
        with mock.patch.object(pa, "_curl", return_value={"choices": [{"message": {"content": None, "reasoning_content": "VERDICT: APPROVE"}}]}):
            self.assertEqual(pa._nv_call(MISTRAL, "p", "k"), "")

    def test_models_run_in_parallel(self):
        def slow(url, headers, body, key, timeout=120):
            time.sleep(0.3)
            return _reply("APPROVE")
        t0 = time.time()
        with mock.patch.object(pa, "_curl", slow):
            self.assertEqual(pa._nvidia_pair("diff", "k")["agreement"], "2-of-2")
        self.assertLess(time.time() - t0, 0.55)

    def test_limits_derived_from_bakeoff_data(self):
        import statistics
        for m, s in pa._BAKEOFF_OK_SECONDS.items():
            p90 = statistics.quantiles(s, n=10, method="inclusive")[-1]
            self.assertEqual(pa._NVIDIA_TIMEOUT[m], int(-(-(2 * p90 - statistics.median(s)) // 1)))
        self.assertEqual(set(pa._NVIDIA_TIMEOUT), set(pa._NVIDIA_PAIR))
        fake = FakeNvidia({MISTRAL: "APPROVE", MINIMAX: "APPROVE"})
        with mock.patch.object(pa, "_curl", fake):
            pa._nvidia_pair("diff", "k")
        for m, timeout, max_tokens, _ in fake.calls:
            self.assertEqual(timeout, pa._NVIDIA_TIMEOUT[m])
            self.assertEqual(max_tokens, pa._BAKEOFF_MAX_TOKENS)

    def test_no_module_global_model_id(self):
        self.assertFalse(hasattr(pa, "_LAST_NVIDIA_MODEL"))
        self.assertFalse(hasattr(pa, "_NVIDIA_LADDER"))

    def test_compat_wrapper_single_verdict_line(self):
        with mock.patch.object(pa, "_curl", FakeNvidia({MISTRAL: "APPROVE", MINIMAX: "REJECT"})):
            txt = pa._nvidia("diff", "k")
        self.assertEqual(pa._verdict(txt), "REJECT")
        self.assertIn(MINIMAX, txt.splitlines()[-1])
        with mock.patch.object(pa, "_curl", FakeNvidia({MISTRAL: "APPROVE", MINIMAX: "empty"})):
            self.assertEqual(pa._verdict(pa._nvidia("diff", "k")), "APPROVE")
        with mock.patch.object(pa, "_curl", FakeNvidia({MISTRAL: "empty", MINIMAX: "404"})):
            with self.assertRaises(RuntimeError):
                pa._nvidia("diff", "k")

    def test_audit_path_never_chunks(self):
        import inspect
        src = inspect.getsource(pa._audit_file)
        self.assertNotIn("_gro_chunked", src)
        self.assertNotIn("_split_pure_add_hunk", src)


class _Routing(unittest.TestCase):
    """audit_file end to end: every external call stubbed, the verdict log and markers in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = os.path.join(self.tmp, "verdicts.jsonl")
        self.blob = b"x = 1\n"

        def fake_git(args, want_bytes=False):
            if args[:2] == ["cat-file", "blob"]:
                return True, self.blob, ""
            if args[:1] == ["diff"]:
                return True, "@@ -1 +1 @@\n-x = 0\n+x = 1\n", ""
            return True, "", ""
        self.patches = [mock.patch.object(pa, "_git", side_effect=fake_git),
                        mock.patch.object(pa, "MARKER_DIR", self.tmp),
                        mock.patch.object(pa._pg, "MARKER_DIR", self.tmp),
                        mock.patch.object(pa._pg, "_content_sha", side_effect=lambda ref: self.sha),
                        mock.patch.object(pa, "_verdict_log_path", return_value=self.log),
                        mock.patch.object(pa, "_static_facts", return_value=("", None)),
                        mock.patch.object(pa, "_codex_signed", return_value=False),
                        mock.patch.object(pa.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    @property
    def sha(self):
        return hashlib.sha256(self.blob).hexdigest()

    def cold2(self, verdict="PASS", age=0, sha=None):
        with open(os.path.join(self.tmp, "tool.py.cold2.json"), "w") as f:
            json.dump({"verdict": verdict, "sha256": sha or self.sha, "ts": int(time.time()) - age}, f)

    def marker(self):
        p = os.path.join(self.tmp, "tool.py.json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    @property
    def gai_log(self):
        return os.path.join(self.tmp, pa._GAI_LOG_NAME)

    def rows(self, path=None):
        path = path or self.log
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def seed(self, reviewer, verdict="REJECT", sha=None, evidence=False):
        with open(self.gai_log if reviewer == "gai" else self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "file": "tool.py", "sha256": sha or self.sha, "reviewer": reviewer,
                                "verdict": verdict, "evidence": evidence}) + "\n")

    def audit(self, nv=None, gai=APPROVE_TXT, codex=(None, "usage limit"), gro=APPROVE_TXT, evidence="", nk=True):
        """nv = FakeNvidia plan (or None); gai = text or an Exception (GAI down); gro = text or an Exception."""
        keys = {"GEMINI_API_KEY": "gk", "GROQ_API_KEY": "rk"}
        if nk:
            keys["NVIDIA_API_KEY"] = "nk"
        self.nv = FakeNvidia(nv or {MISTRAL: "APPROVE", MINIMAX: "APPROVE"})
        gai_kw = {"side_effect": gai} if isinstance(gai, (Exception, list)) else {"return_value": gai}
        gro_kw = {"side_effect": gro} if isinstance(gro, (Exception, list)) else {"return_value": gro}
        with mock.patch.object(pa, "_curl", self.nv), \
                mock.patch.object(pa, "_gai", **gai_kw), \
                mock.patch.object(pa, "_gro", **gro_kw) as self.gro, \
                mock.patch.object(pa, "_codex_review", return_value=codex):
            return pa.audit_file("tool.py", False, keys, evidence=evidence)


class PairRouting(_Routing):
    def test_codex_out_pair_approves_marker_records_both_models(self):
        self.cold2()
        ok, msg = self.audit()
        self.assertTrue(ok, msg)
        m = self.marker()
        self.assertEqual((m["gro"], m["gai"], m["reviewer_b"]), ("APPROVE", "APPROVE", "nvidia-pair"))
        self.assertEqual(m["reviewer_b_models"], [{"id": MISTRAL, "verdict": "APPROVE"}, {"id": MINIMAX, "verdict": "APPROVE"}])
        self.assertEqual(m["reviewer_b_agreement"], "2-of-2")
        self.gro.assert_not_called()
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self.rows()],
                         [("NVIDIA_" + MISTRAL, "APPROVE"), ("NVIDIA_" + MINIMAX, "APPROVE")])
        # GAI verdicts land in the separate GAI ledger beside it (NV-REVIEW-1110 item 5)
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self.rows(self.gai_log)], [("gai", "APPROVE")])

    def test_one_of_two_in_marker(self):
        self.cold2()
        ok, msg = self.audit(nv={MISTRAL: "empty", MINIMAX: "APPROVE"})
        self.assertTrue(ok, msg)
        self.assertEqual(self.marker()["reviewer_b_agreement"], "1-of-2")
        self.assertIn("1-of-2", msg)

    def test_pair_reject_blocks(self):
        self.cold2()
        ok, msg = self.audit(nv={MISTRAL: "APPROVE", MINIMAX: "REJECT"})
        self.assertFalse(ok)
        self.assertIn("REJECT", msg)
        self.assertIsNone(self.marker())
        self.gro.assert_not_called()

    def test_both_unusable_fails_closed_without_gro(self):
        self.cold2()
        for plan in ({MISTRAL: "empty", MINIMAX: "empty"}, {MISTRAL: "empty", MINIMAX: "timeout"}):
            with self.subTest(plan=plan):
                ok, msg = self.audit(nv=plan)
                self.assertFalse(ok)
                self.assertIn("neither NVIDIA pair model gave a verdict", msg)
                self.gro.assert_not_called()
                self.assertIsNone(self.marker())

    def test_gro_only_when_nvidia_transport_fails_and_diff_fits(self):
        self.cold2()
        ok, msg = self.audit(nv={MISTRAL: "timeout", MINIMAX: "404"})
        self.assertTrue(ok, msg)
        self.gro.assert_called()
        self.assertEqual(self.marker()["reviewer_b"], "gro")
        self.assertNotIn("reviewer_b_models", self.marker())

    def test_oversized_diff_with_nvidia_down_fails_closed(self):
        self.cold2()
        ok, msg = self.audit(nv={MISTRAL: "timeout", MINIMAX: "timeout"},
                             gro=RuntimeError("Request too large for model openai/gpt-oss-120b: Requested 9100, Limit 8000"))
        self.assertFalse(ok)
        self.assertIn("does not fit one Gro request", msg)
        self.assertIn("no chunked review", msg)
        self.assertEqual(self.gro.call_count, 1)
        self.assertIsNone(self.marker())

    def test_gai_down_pair_stands_in(self):
        self.cold2()
        ok, msg = self.audit(gai=RuntimeError("429 quota"), codex=("APPROVE", APPROVE_TXT))
        self.assertTrue(ok, msg)
        m = self.marker()
        self.assertEqual((m["gai"], m["gai_substituted"], m["reviewer_b"]), ("APPROVE", "nvidia-pair", "codex"))
        self.assertEqual([x["id"] for x in m["gai_models"]], [MISTRAL, MINIMAX])
        self.assertEqual(m["gai_agreement"], "2-of-2")

    def test_gai_down_pair_reject_blocks(self):
        self.cold2()
        ok, msg = self.audit(gai=RuntimeError("503"), codex=("APPROVE", APPROVE_TXT), nv={MISTRAL: "REJECT", MINIMAX: "APPROVE"})
        self.assertFalse(ok)
        self.assertIsNone(self.marker())


class Cold2Precondition(_Routing):
    def _assert_refused(self, **audit_kw):
        ok, msg = self.audit(**audit_kw)
        self.assertFalse(ok)
        self.assertIn("cold-2nd precondition not met", msg)
        self.assertEqual(self.nv.calls, [])          # the pair is never asked
        self.assertIsNone(self.marker())
        return msg

    def test_missing(self):
        self._assert_refused()
        self.gro.assert_not_called()

    def test_stale(self):
        self.cold2(age=pa._pg.MARKER_MAX_AGE_SEC + 60)
        self.assertIn("stale", self._assert_refused())

    def test_other_content(self):
        self.cold2(sha="0" * 64)
        self.assertIn("sha mismatch", self._assert_refused())

    def test_fail_verdict(self):
        self.cold2(verdict="FAIL")
        self._assert_refused()

    def test_gai_slot_too(self):
        self._assert_refused(gai=RuntimeError("429 quota"), codex=("APPROVE", APPROVE_TXT))


class NoReviewerShopping(_Routing):
    """A seeded open REJECT from each reviewer blocks every fallback on the same content; cleared only as designed."""
    REVIEWERS = ("codex", "gai", "gro", "NVIDIA_" + MISTRAL, "NVIDIA_" + MINIMAX)
    # each fallback: (name, its own verdict-log ids, audit kwargs that route the run through it)
    FALLBACKS: tuple = (("reviewer-2 pair", {"NVIDIA_" + MISTRAL, "NVIDIA_" + MINIMAX}, {}),
                 ("reviewer-2 gro", {"gro"}, {"nk": False}),
                 ("gai-slot pair", {"NVIDIA_" + MISTRAL, "NVIDIA_" + MINIMAX},
                  {"gai": RuntimeError("429 quota"), "codex": ("APPROVE", APPROVE_TXT)}))

    def _fresh(self):
        for p in (self.log, self.gai_log, os.path.join(self.tmp, "tool.py.json")):
            if os.path.exists(p):
                os.unlink(p)

    def test_every_seeded_reject_blocks_every_fallback(self):
        self.cold2()
        for rv in self.REVIEWERS:
            for name, _own, kw in self.FALLBACKS:
                with self.subTest(seed=rv, fallback=name):
                    self._fresh()
                    self.seed(rv)
                    ok, msg = self.audit(**kw)
                    # rev 4: a Codex REJECT on THIS content is never exempt, so it blocks the GAI-slot pair too (the
                    # exemption covers only a Codex REJECT on earlier content — see test_old_codex_reject_* below)
                    self.assertFalse(ok, msg)
                    self.assertIn("refused" if "gai" not in name else "may not stand in", msg)
                    self.assertIsNone(self.marker())
                    self.assertEqual(self.nv.calls, [])
                    self.gro.assert_not_called()

    def test_evidence_lets_only_the_rejecting_reviewer_clear_it(self):
        self.cold2()
        for rv in self.REVIEWERS:
            for name, own, kw in self.FALLBACKS:
                with self.subTest(seed=rv, fallback=name):
                    self._fresh()
                    self.seed(rv)
                    ok, msg = self.audit(evidence="the cited helper guards qty == 0 at line 40", **kw)
                    # only the reviewer that rejected may clear it: the fallback itself, or GAI when GAI is up and
                    # reviews this run as primary (its APPROVE with --evidence clears its own reject first)
                    # rev 4: a Codex REJECT on this content blocks the GAI-slot pair BEFORE Codex re-reviews, so a seeded
                    # Codex reject is never cleared through a fallback route here
                    clears = rv in own or (rv == "gai" and "gai" not in kw)
                    self.assertEqual(ok, clears, msg)
                    if ok:
                        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])

    def test_same_reviewer_no_verdict_does_not_clear(self):
        self.cold2()
        self.seed("NVIDIA_" + MISTRAL)
        ok, msg = self.audit(nv={MISTRAL: "empty", MINIMAX: "APPROVE"}, evidence="answer to the reject")
        self.assertFalse(ok)
        self.assertIn("not cleared", msg)
        self.assertIsNone(self.marker())

    def test_another_reviewers_approve_does_not_clear(self):
        self.seed("gro")
        self.seed("NVIDIA_" + MISTRAL, "APPROVE")
        self.seed("codex", "APPROVE")
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gro"])

    def test_indeterminate_and_waiver_do_not_clear(self):
        self.seed("gro")
        self.seed("gro", "INDETERMINATE")
        self.seed("waived", "WAIVED")
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gro"])

    def test_blind_same_content_approve_does_not_clear(self):
        self.seed("gro")
        self.seed("gro", "APPROVE")                       # a re-roll on the same content, no --evidence
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gro"])
        self.seed("gro", "APPROVE", evidence=True)
        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])

    def test_same_reviewer_approving_later_content_clears(self):
        old = "1" * 64
        self.seed("gai", sha=old)
        self.seed("gai", "APPROVE")                        # later content (this sha)
        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])

    # --- cold-review threats (2026-11-10) ---
    def test_threat1_reject_on_a_is_not_cleared_by_a_reject_on_b(self):
        a, b = "a" * 64, "b" * 64
        for rv in ("NVIDIA_" + MISTRAL, "gai", "gro"):
            with self.subTest(reviewer=rv):
                self._fresh()
                self.seed(rv, sha=a)
                self.seed(rv, sha=b)
                self.assertEqual(pa._open_rejects("tool.py", a), [rv])
                self.assertEqual(pa._open_rejects("tool.py", b), [rv])
                self.seed(rv, "APPROVE", sha=b)            # an APPROVE of B (a different, later sha) clears A ...
                self.assertEqual(pa._open_rejects("tool.py", a), [])
                self.assertEqual(pa._open_rejects("tool.py", b), [rv])   # ... but not B itself without --evidence
                self.seed(rv, "APPROVE", sha=b, evidence=True)
                self.assertEqual(pa._open_rejects("tool.py", b), [])

    def test_threat2_gai_reject_logged_even_when_counter_prompt_fails(self):
        self.cold2()
        reject = "VERDICT: REJECT — `pos` is undefined (NameError)"
        with mock.patch.object(pa, "_static_facts", return_value=("", True)), \
                mock.patch.object(pa, "_gai", side_effect=[reject, RuntimeError("503 unavailable")]), \
                mock.patch.object(pa, "_codex_review", return_value=("APPROVE", APPROVE_TXT)):
            ok, msg = pa.audit_file("tool.py", False, {"GEMINI_API_KEY": "gk", "NVIDIA_API_KEY": "nk"})
        self.assertFalse(ok)
        self.assertIn("counter-prompt failed", msg)
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self.rows(self.gai_log)], [("gai", "REJECT")])
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gai"])

    def test_threat2_gro_reject_logged_even_when_counter_prompt_fails(self):
        reject = "VERDICT: REJECT — `pos` is undefined (NameError)"
        with mock.patch.object(pa, "_static_facts", return_value=("", True)):
            ok, msg = self.audit(nk=False, gro=[reject, RuntimeError("503 unavailable")])
        self.assertFalse(ok)
        self.assertIn("counter-prompt failed", msg)
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self.rows()], [("gro", "REJECT")])

    def test_threat2_counter_prompt_result_is_its_own_row_and_clears(self):
        reject = "VERDICT: REJECT — `pos` is undefined (NameError)"
        with mock.patch.object(pa, "_static_facts", return_value=("", True)):
            ok, msg = self.audit(nk=False, gro=[reject, APPROVE_TXT])
        self.assertTrue(ok, msg)
        rows = self.rows()
        self.assertEqual([(r["reviewer"], r["verdict"], r["evidence"], r["auto_counter"]) for r in rows],
                         [("gro", "REJECT", False, False), ("gro", "APPROVE", False, True)])
        self.assertTrue(rows[0]["run"] and rows[0]["run"] == rows[1]["run"])
        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])

    # --- revision-3 threat: an automatic counter-prompt clears only the REJECT of its own run ---
    NAME_REJECT = "VERDICT: REJECT — `pos` is undefined (NameError)"
    GENUINE_REJECT = "The new stop path has no guard.\nVERDICT: REJECT — missing stop guard"

    def test_rev3_two_runs_gai_counter_does_not_clear_earlier_genuine_reject(self):
        self.cold2()
        with mock.patch.object(pa, "_static_facts", return_value=("", True)):
            ok1, _ = self.audit(gai=self.GENUINE_REJECT)                         # run 1: genuine GAI REJECT
            ok2, msg2 = self.audit(gai=[self.NAME_REJECT, APPROVE_TXT])          # run 2: same content, no --evidence
        self.assertFalse(ok1)
        self.assertFalse(ok2)
        self.assertIn("open REJECT from gai", msg2)
        self.assertEqual(self.nv.calls, [])                                      # the pair never stood in
        self.assertIsNone(self.marker())
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gai"])

    def test_rev3_single_run_gai_counter_clears_its_own_reject(self):
        self.cold2()
        with mock.patch.object(pa, "_static_facts", return_value=("", True)):
            ok, msg = self.audit(gai=[self.NAME_REJECT, APPROVE_TXT])
        self.assertTrue(ok, msg)                                                 # pair stands in for Codex
        self.assertIsNotNone(self.marker())
        g = self.rows(self.gai_log)
        self.assertEqual([(r["verdict"], r["auto_counter"]) for r in g], [("REJECT", False), ("APPROVE", True)])
        self.assertEqual(g[0]["run"], g[1]["run"])
        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])

    def test_rev3_two_runs_gro_counter_does_not_clear_earlier_genuine_reject(self):
        with mock.patch.object(pa, "_static_facts", return_value=("", True)):
            ok1, _ = self.audit(nk=False, gro=self.GENUINE_REJECT)
            ok2, msg2 = self.audit(nk=False, gro=[self.NAME_REJECT, APPROVE_TXT], evidence="")
        self.assertFalse(ok1)
        self.assertFalse(ok2)                         # refused up front: its own open REJECT needs --evidence
        self.assertIsNone(self.marker())
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gro"])

    def test_rev3_author_evidence_run_still_clears_earlier_rejects(self):
        self.cold2()
        with mock.patch.object(pa, "_static_facts", return_value=("", True)):
            self.assertFalse(self.audit(gai=self.GENUINE_REJECT)[0])
            ok, msg = self.audit(gai=APPROVE_TXT, evidence="the stop guard is at line 88 (quoted)")
        self.assertTrue(ok, msg)
        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])

    def test_rev3_ledger_rule_directly(self):
        # an auto_counter APPROVE clears only the same-run REJECT; an earlier run's REJECT stays open
        for row in ({"reviewer": "gai", "verdict": "REJECT", "run": "r1"},
                    {"reviewer": "gai", "verdict": "REJECT", "run": "r2"},
                    {"reviewer": "gai", "verdict": "APPROVE", "run": "r2", "auto_counter": True}):
            with open(self.gai_log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "file": "tool.py", "sha256": self.sha,
                                    "evidence": False, **row}) + "\n")
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gai"])
        with open(self.gai_log, "a", encoding="utf-8") as f:   # an auto_counter row without a run id clears nothing
            f.write(json.dumps({"ts": int(time.time()), "file": "tool.py", "sha256": self.sha, "reviewer": "gai",
                                "verdict": "APPROVE", "auto_counter": True, "evidence": False}) + "\n")
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gai"])

    def test_rev4_auto_counter_never_clears_another_sha(self):
        # rev 4 (adversarial claims check): an automatic counter-prompt APPROVE of new content B must not clear the same
        # reviewer's REJECT on content A from an earlier run (a plain later-content APPROVE would)
        other = "b" * 64
        for row in ({"reviewer": "gai", "verdict": "REJECT", "run": "r1", "sha256": self.sha},
                    {"reviewer": "gai", "verdict": "REJECT", "run": "r2", "sha256": other},
                    {"reviewer": "gai", "verdict": "APPROVE", "run": "r2", "auto_counter": True, "sha256": other}):
            with open(self.gai_log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "file": "tool.py", "evidence": False, **row}) + "\n")
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["gai"])     # A's reject stays open
        self.assertEqual(pa._open_rejects("tool.py", other), [])             # B's own-run reject is answered

    def test_rev4_same_content_codex_reject_blocks_gai_slot_pair(self):
        # rev 4: Codex REJECTed THIS content earlier; Gemini is down; Codex would re-APPROVE the same content without
        # --evidence (which does not clear its reject) -> the GAI-slot pair is refused and no marker is written
        self.cold2()
        self.seed("codex")
        ok, msg = self.audit(gai=RuntimeError("429 quota"), codex=("APPROVE", APPROVE_TXT))
        self.assertFalse(ok, msg)
        self.assertIn("codex", msg)
        self.assertIsNone(self.marker())
        self.assertEqual(self.nv.calls, [])

    def test_rev3_pair_model_reject_logged_before_its_counter_prompt(self):
        self.cold2()
        fake = FakeNvidia({MISTRAL: [  # first answer: a name-premise REJECT; the counter-prompt answer: APPROVE
            "REJECT", "APPROVE"], MINIMAX: "APPROVE"})
        fake_reply = _reply

        def name_reject_first(kind):
            if kind == "REJECT":
                return {"choices": [{"message": {"content": self.NAME_REJECT}}]}
            return fake_reply(kind)
        with mock.patch(__name__ + "._reply", side_effect=name_reject_first), \
                mock.patch.object(pa, "_static_facts", return_value=("", True)), \
                mock.patch.object(pa, "_curl", fake), mock.patch.object(pa, "_gai", return_value=APPROVE_TXT), \
                mock.patch.object(pa, "_codex_review", return_value=(None, "usage limit")):
            ok, msg = pa.audit_file("tool.py", False, {"GEMINI_API_KEY": "gk", "NVIDIA_API_KEY": "nk"})
        self.assertTrue(ok, msg)
        mrows = [r for r in self.rows() if r["reviewer"] == "NVIDIA_" + MISTRAL]
        self.assertEqual([(r["verdict"], r["auto_counter"]) for r in mrows], [("REJECT", False), ("APPROVE", True)])
        self.assertEqual(mrows[0]["run"], mrows[1]["run"])
        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])

    def test_threat3_old_codex_reject_does_not_block_gai_slot_before_codex_rereviews(self):
        self.cold2()
        self.seed("codex", sha="c" * 64)                   # Codex rejected EARLIER content
        ok, msg = self.audit(gai=RuntimeError("429 quota"), codex=("APPROVE", APPROVE_TXT))
        self.assertTrue(ok, msg)                           # pair stands in for GAI; Codex approves the fixed content
        self.assertEqual(self.marker()["gai_substituted"], "nvidia-pair")
        self.assertEqual(pa._open_rejects("tool.py", self.sha), [])   # Codex's APPROVE of later content cleared it

    def test_threat3_codex_still_decides_after_the_gai_slot(self):
        self.cold2()
        self.seed("codex", sha="c" * 64)
        ok, msg = self.audit(gai=RuntimeError("429 quota"), codex=("REJECT", REJECT_TXT))
        self.assertFalse(ok)
        self.assertIn("Codex REJECT", msg)
        self._fresh()
        self.seed("codex", sha="c" * 64)
        ok, msg = self.audit(gai=RuntimeError("429 quota"), codex=(None, "usage limit"))
        self.assertFalse(ok)                               # Codex out: its open reject blocks the reviewer-#2 fallback
        self.assertIn("NVIDIA pair fallback refused", msg)
        self.assertIsNone(self.marker())

    def test_threat3_waived_run_keeps_codex_entry_blocking_gai_slot(self):
        self.cold2()
        self.seed("codex", sha="c" * 64)
        with mock.patch.object(pa, "_curl", FakeNvidia({MISTRAL: "APPROVE", MINIMAX: "APPROVE"})) as nv, \
                mock.patch.object(pa, "_gai", side_effect=RuntimeError("429 quota")):
            ok, msg = pa.audit_file("tool.py", True, {"GEMINI_API_KEY": "gk", "NVIDIA_API_KEY": "nk"})
        self.assertFalse(ok)
        self.assertIn("open REJECT from codex", msg)
        self.assertEqual(nv.calls, [])

    def test_gai_reject_logged_by_a_real_run_blocks_the_pair(self):
        self.cold2()
        ok, _ = self.audit(gai="Bad stop.\nVERDICT: REJECT — inverted stop", codex=("APPROVE", APPROVE_TXT))
        self.assertFalse(ok)
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self.rows(self.gai_log)], [("gai", "REJECT")])
        self.assertEqual(self.rows(), [r for r in self.rows() if r["reviewer"] != "gai"])   # not in the reviewer ledger
        ok, msg = self.audit(gai=RuntimeError("429 quota"), codex=("APPROVE", APPROVE_TXT))   # GAI now down
        self.assertFalse(ok)
        self.assertIn("open REJECT from gai", msg)
        self.assertEqual(self.nv.calls, [])

    def test_reject_on_other_content_does_not_block_except_codex(self):
        old = "2" * 64
        for rv in self.REVIEWERS:
            self.seed(rv, sha=old)
        self.assertEqual(pa._open_rejects("tool.py", self.sha), ["codex"])   # Codex's reject is file-wide (kept)


class KeySafety(unittest.TestCase):
    """After a forced subprocess.TimeoutExpired no key value appears in any returned, raised or logged text."""
    GK, NK, RK = "AIzaGEMINIKEY-test-0123456789", "nvapi-NVIDIAKEY-test-0123456789", "gsk_GROQKEY-test-0123456789"

    def setUp(self):
        self.seen = []
        env = {"GEMINI_API_KEY": self.GK, "NVIDIA_API_KEY": self.NK, "GROQ_API_KEY": self.RK}

        def timeout_run(cmd, **kw):
            hdr = [a[1:] for a in cmd if isinstance(a, str) and a.startswith("@") and a.endswith(".hdr")]
            files = {}
            for h in hdr:
                with open(h, encoding="utf-8") as f:
                    files[h] = (f.read(), stat.S_IMODE(os.stat(h).st_mode))
            self.seen.append((list(cmd), files))
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        self.patches = [mock.patch.object(pa.subprocess, "run", side_effect=timeout_run),
                        mock.patch.object(pa, "_load_env", return_value=env),
                        mock.patch.object(pa.time, "sleep")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _check(self, text, key):
        self.assertTrue(self.seen)
        for cmd, files in self.seen:
            joined = " ".join(cmd)
            for k in (self.GK, self.NK, self.RK):
                self.assertNotIn(k, joined)                 # never on argv
            self.assertNotIn("key=", joined)                # never in a URL
            self.assertEqual(len(files), 1)
            for path, (content, mode) in files.items():
                self.assertIn(key, content)                 # the key travels in the header file only
                self.assertEqual(mode, 0o600)
                self.assertFalse(os.path.exists(path))      # and the file is gone afterwards
        for k in (self.GK, self.NK, self.RK):
            self.assertNotIn(k, text)

    def test_gemini(self):
        with self.assertRaises(Exception) as cm:
            pa._gai("diff", self.GK)
        self._check(str(cm.exception), self.GK)
        self.assertTrue(any(f"x-goog-api-key: {self.GK}" in c for _, fs in self.seen for c, _m in fs.values()))

    def test_nvidia(self):
        res = pa._nvidia_pair("diff", self.NK)
        self.assertTrue(res["endpoint_down"])
        self._check(json.dumps(res), self.NK)

    def test_groq(self):
        with self.assertRaises(Exception) as cm:
            pa._gro("diff", self.RK)
        self._check(str(cm.exception), self.RK)

    def test_end_to_end_message_and_verdict_log(self):
        tmp = tempfile.mkdtemp()
        log = os.path.join(tmp, "v.jsonl")
        blob = b"x = 1\n"
        sha = hashlib.sha256(blob).hexdigest()
        with open(os.path.join(tmp, "tool.py.cold2.json"), "w") as f:
            json.dump({"verdict": "PASS", "sha256": sha, "ts": int(time.time())}, f)

        def fake_git(args, want_bytes=False):
            if args[:2] == ["cat-file", "blob"]:
                return True, blob, ""
            if args[:1] == ["diff"]:
                return True, "@@ -1 +1 @@\n-x = 0\n+x = 1\n", ""
            return True, "", ""
        with mock.patch.object(pa, "_git", side_effect=fake_git), mock.patch.object(pa, "MARKER_DIR", tmp), \
                mock.patch.object(pa._pg, "MARKER_DIR", tmp), mock.patch.object(pa._pg, "_content_sha", return_value=sha), \
                mock.patch.object(pa, "_verdict_log_path", return_value=log), \
                mock.patch.object(pa, "_static_facts", return_value=("", None)), \
                mock.patch.object(pa, "_codex_signed", return_value=False), \
                mock.patch.object(pa, "_codex_review", return_value=(None, "usage limit")):
            # GAI times out → the pair stands in and times out → fail closed; then GAI up, pair down → Gro times out
            ok1, msg1 = pa.audit_file("tool.py", False, {"GEMINI_API_KEY": self.GK, "NVIDIA_API_KEY": self.NK, "GROQ_API_KEY": self.RK})
            with mock.patch.object(pa, "_gai", return_value=APPROVE_TXT):
                ok2, msg2 = pa.audit_file("tool.py", False, {"GEMINI_API_KEY": self.GK, "NVIDIA_API_KEY": self.NK, "GROQ_API_KEY": self.RK})
        self.assertFalse(ok1)
        self.assertFalse(ok2)
        with open(log, encoding="utf-8") as f:
            logged = f.read()
        self.assertIn("timed out", logged)
        hosts = {c[-1].split("/")[2] for c, _ in self.seen}
        self.assertEqual(hosts, {"generativelanguage.googleapis.com", "integrate.api.nvidia.com", "api.groq.com"})
        for text in (msg1, msg2, logged):
            for k in (self.GK, self.NK, self.RK):
                self.assertNotIn(k, text)

    def test_redact_applies_to_verdict_log_reason(self):
        tmp = tempfile.mkdtemp()
        log = os.path.join(tmp, "v.jsonl")
        with mock.patch.object(pa, "_verdict_log_path", return_value=log):
            pa._log_verdict("f.py", "s", "gro", "REJECT", f"echoed {self.RK} back", note=f"n {self.NK}")
        with open(log, encoding="utf-8") as f:
            row = f.read()
        self.assertNotIn(self.RK, row)
        self.assertNotIn(self.NK, row)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
