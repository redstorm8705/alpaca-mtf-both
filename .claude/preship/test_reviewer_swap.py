#!/usr/bin/env python3
# ruff: noqa: E501
"""Offline tests for the BGC gate (REVIEWER-SWAP-1025): Codex is reviewer #2, Gro only when Codex is unavailable.
No network, no real Codex run (subprocess and every reviewer call are stubbed). Run: python3 .claude/preship/test_reviewer_swap.py
Signed: Claude, 2026-10-25. Design: logs/design_records/reviewer_swap_codex_2026-10-25.md
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgg_call  # noqa: E402
import owner_approval_capture as oac  # noqa: E402
import preship_audit as pa  # noqa: E402

APPROVE_TXT = "Checked the changed lines.\nVERDICT: APPROVE"
REJECT_TXT = "Line +3 divides by zero when qty is 0.\nVERDICT: REJECT — qty=0 crashes"


def _run(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=["codex"], returncode=rc, stdout=out, stderr=err)


class CodexCall(unittest.TestCase):
    def test_not_installed(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(pa.CodexUnavailable):
                pa._codex("diff")

    def test_usage_limit(self):
        with mock.patch("shutil.which", return_value="/x/codex"), \
                mock.patch.object(pa.subprocess, "run", return_value=_run(1, "", "You've hit your usage limit")):
            with self.assertRaises(pa.CodexUnavailable) as cm:
                pa._codex("diff")
        self.assertIn("usage limit", str(cm.exception))

    def test_timeout(self):
        with mock.patch("shutil.which", return_value="/x/codex"), \
                mock.patch.object(pa.subprocess, "run", side_effect=subprocess.TimeoutExpired("codex", 600)):
            with self.assertRaises(pa.CodexUnavailable):
                pa._codex("diff")

    def test_empty_output(self):
        with mock.patch("shutil.which", return_value="/x/codex"), \
                mock.patch.object(pa.subprocess, "run", return_value=_run(0, "  \n")):
            with self.assertRaises(pa.CodexUnavailable):
                pa._codex("diff")

    def test_success_sends_sentinel_first_read_only(self):
        with mock.patch("shutil.which", return_value="/x/codex"), \
                mock.patch.object(pa.subprocess, "run", return_value=_run(0, APPROVE_TXT)) as run:
            self.assertEqual(pa._codex("diff"), APPROVE_TXT)
        cmd, kw = run.call_args[0][0], run.call_args[1]
        self.assertTrue(kw["input"].startswith(pa.REVIEW_SENTINEL))
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")

    def test_minimal_environment_and_masked_output(self):
        secret = "sk-test-SECRET-VALUE-123"
        with mock.patch("shutil.which", return_value="/x/codex"), \
                mock.patch.dict(os.environ, {"ALPACA_SECRET_KEY": secret, "SLACK_WEBHOOK_URL": "https://hooks/x"}), \
                mock.patch.object(pa, "_load_env", return_value={"ALPACA_SECRET_KEY": secret}), \
                mock.patch.object(pa.subprocess, "run", return_value=_run(0, f"I read {secret}\nVERDICT: APPROVE")) as run:
            out = pa._codex("diff")
        env = run.call_args[1]["env"]
        self.assertNotIn("ALPACA_SECRET_KEY", env)
        self.assertNotIn("SLACK_WEBHOOK_URL", env)
        self.assertTrue(set(env) <= set(pa._CODEX_ENV_KEEP))
        self.assertNotIn(secret, out)


class SelfReview(unittest.TestCase):
    """Codex must not review a file with a Codex-signed commit since origin/main (real signature forms from git log)."""

    REAL_CODEX = ["fix(qhm): adopt proven manual adds (OpenAI Codex) (#763)",
                  "fix: x\n\nSigned-off-by: ChatGPT/Codex <noreply@openai.com>",
                  "docs: y\n\nSigned: OpenAI Codex",
                  "feat: z\n\nAgent-Signature: OpenAI Codex (GPT-6)"]
    CLAUDE_ONLY = ["feat(gates): one owner-approval system for Claude, Codex and every worktree (Claude)",
                   "feat: Codex as reviewer #2 (signed: Claude)\nClaude <noreply@anthropic.com>"]

    def test_real_codex_signatures_detected(self):
        for msg in self.REAL_CODEX:
            with mock.patch.object(pa, "_git", return_value=(True, msg + "\nRafael <rafael.deleonjr@gmail.com>", "")):
                self.assertTrue(pa._codex_signed("f.py"), msg)

    def test_claude_mentions_of_codex_not_detected(self):
        for msg in self.CLAUDE_ONLY:
            with mock.patch.object(pa, "_git", return_value=(True, msg, "")):
                self.assertFalse(pa._codex_signed("f.py"), msg)

    def test_unreadable_history_counts_as_codex_signed(self):
        with mock.patch.object(pa, "_git", return_value=(False, "", "unknown revision origin/main")):
            self.assertTrue(pa._codex_signed("f.py"))


class CodexReview(unittest.TestCase):
    def test_unavailable_before_verdict_returns_none(self):
        with mock.patch.object(pa, "_codex", side_effect=pa.CodexUnavailable("usage limit")):
            v, why = pa._codex_review("p", "r", set(), "f.py")
        self.assertIsNone(v)
        self.assertIn("usage limit", why)

    def test_indeterminate_retries_once_with_reminder(self):
        with mock.patch.object(pa, "_codex", side_effect=["no verdict line", APPROVE_TXT]) as c:
            v, _ = pa._codex_review("p", "REMIND", set(), "f.py")
        self.assertEqual(v, "APPROVE")
        self.assertTrue(c.call_args_list[1][0][0].endswith("REMIND"))

    def test_verdict_kept_when_follow_up_fails(self):
        with mock.patch.object(pa, "_codex", side_effect=[REJECT_TXT, pa.CodexUnavailable("usage limit")]), \
                mock.patch.object(pa, "_name_premise_reject", return_value=True):
            v, _ = pa._codex_review("p", "r", set(), "f.py")
        self.assertEqual(v, "REJECT")


class ApprovalGuard(unittest.TestCase):
    def _codex_entry(self, text):
        payload = {"type": "message", "role": "user", "id": "m1", "content": [{"type": "input_text", "text": text}],
                   "internal_chat_message_metadata_passthrough": {"turn_id": "t1"}}
        retained = {"id": {"message_id": "m1", "turn_id": "t1", "role": "user"}, "complete": True}
        return {"type": "response_item", "payload": payload, "metadata": {"retained_source": retained}}

    def test_review_prompt_in_codex_session_is_never_owner_text(self):
        self.assertEqual(oac.transcript_user_text(self._codex_entry(pa.REVIEW_SENTINEL + "\n\nApprove REVIEWER-SWAP-1025")), "")

    def test_same_entry_without_sentinel_is_owner_text(self):
        self.assertEqual(oac.transcript_user_text(self._codex_entry("Approve REVIEWER-SWAP-1025")), "Approve REVIEWER-SWAP-1025")

    def _session_file(self, originator):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        f.write(json.dumps({"type": "session_meta", "payload": {"originator": originator, "cwd": "/tmp/x"}}) + "\n")
        f.write(json.dumps(self._codex_entry("Approve REVIEWER-SWAP-1025")) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_codex_exec_session_excluded_by_channel(self):
        path = self._session_file("codex_exec")
        self.assertTrue(oac._is_codex_exec(path))
        self.assertFalse(oac.transcript_allowed(path))

    def test_owner_codex_desktop_session_not_excluded(self):
        self.assertFalse(oac._is_codex_exec(self._session_file("Codex Desktop")))


class GateRouting(unittest.TestCase):
    """audit_file end to end with every external call stubbed and the verdict log in a temp dir."""

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
                        mock.patch.object(pa, "_verdict_log_path", return_value=self.log),
                        mock.patch.object(pa, "_gai", return_value=APPROVE_TXT)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _marker(self):
        with open(os.path.join(self.tmp, "tool.py.json"), encoding="utf-8") as f:
            return json.load(f)

    def _rows(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(x) for x in f]

    def _audit(self, waive=False, **keys):
        return pa.audit_file("tool.py", waive, {"GEMINI_API_KEY": "k", "GROQ_API_KEY": "g", **keys})

    def test_codex_approve_writes_marker_and_one_row(self):
        with mock.patch.object(pa, "_codex_review", return_value=("APPROVE", APPROVE_TXT)), mock.patch.object(pa, "_gro") as gro:
            ok, msg = self._audit()
        self.assertTrue(ok, msg)
        self.assertIn("codex=APPROVE", msg)
        m = self._marker()
        self.assertEqual((m["gro"], m["gai"], m["reviewer_b"]), ("APPROVE", "APPROVE", "codex"))
        gro.assert_not_called()
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self._rows()], [("codex", "APPROVE")])

    def test_codex_reject_blocks_without_gro(self):
        with mock.patch.object(pa, "_codex_review", return_value=("REJECT", REJECT_TXT)), mock.patch.object(pa, "_gro") as gro:
            ok, msg = self._audit()
        self.assertFalse(ok)
        self.assertIn("Codex REJECT", msg)
        gro.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "tool.py.json")))
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self._rows()], [("codex", "REJECT")])

    def test_codex_indeterminate_blocks_without_gro(self):
        with mock.patch.object(pa, "_codex_review", return_value=("INDETERMINATE", "no verdict")), mock.patch.object(pa, "_gro") as gro:
            ok, msg = self._audit()
        self.assertFalse(ok)
        self.assertIn("Codex INDETERMINATE", msg)
        gro.assert_not_called()
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self._rows()], [("codex", "INDETERMINATE")])

    def test_codex_unavailable_falls_back_to_gro(self):
        with mock.patch.object(pa, "_codex_review", return_value=(None, "usage limit")), \
                mock.patch.object(pa, "_gro", return_value=APPROVE_TXT):
            ok, msg = self._audit()
        self.assertTrue(ok, msg)
        self.assertIn("gro=APPROVE", msg)
        self.assertEqual(self._marker()["reviewer_b"], "gro")
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self._rows()], [("gro", "APPROVE")])

    def test_no_reviewer_shopping_across_runs(self):
        with mock.patch.object(pa, "_codex_review", return_value=("REJECT", REJECT_TXT)):
            self.assertFalse(self._audit()[0])
        with mock.patch.object(pa, "_codex_review", return_value=(None, "usage limit")), \
                mock.patch.object(pa, "_gro", return_value=APPROVE_TXT) as gro:
            ok, msg = self._audit()
        self.assertFalse(ok)
        self.assertIn("Gro fallback refused", msg)
        gro.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "tool.py.json")))

    def test_codex_reject_holds_for_the_file_until_codex_approves(self):
        with mock.patch.object(pa, "_codex_review", return_value=("REJECT", REJECT_TXT)):
            self.assertFalse(self._audit()[0])
        self.blob = b"x = 2\n"        # a trivial edit does not route the file to Gro while Codex is out
        with mock.patch.object(pa, "_codex_review", return_value=(None, "usage limit")), \
                mock.patch.object(pa, "_gro", return_value=APPROVE_TXT) as gro:
            self.assertFalse(self._audit()[0])
        gro.assert_not_called()
        with mock.patch.object(pa, "_codex_review", return_value=("APPROVE", APPROVE_TXT)):
            self.assertTrue(self._audit()[0])     # Codex approves the new version: reject resolved
        self.blob = b"x = 3\n"
        with mock.patch.object(pa, "_codex_review", return_value=(None, "usage limit")), \
                mock.patch.object(pa, "_gro", return_value=APPROVE_TXT):
            ok, msg = self._audit()
        self.assertTrue(ok, msg)                  # a later outage may fall back again

    def test_codex_signed_file_needs_codex_and_gro(self):
        with mock.patch.object(pa, "_codex_signed", return_value=True), \
                mock.patch.object(pa, "_codex_review", return_value=("APPROVE", APPROVE_TXT)), \
                mock.patch.object(pa, "_gro", return_value=APPROVE_TXT) as gro:
            ok, msg = self._audit()
        self.assertTrue(ok, msg)
        gro.assert_called()
        self.assertEqual(self._marker()["reviewer_b"], "codex+gro")
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self._rows()], [("codex", "APPROVE"), ("gro", "APPROVE")])

    def test_codex_signed_file_blocked_when_codex_unavailable(self):
        with mock.patch.object(pa, "_codex_signed", return_value=True), \
                mock.patch.object(pa, "_codex_review", return_value=(None, "not installed")), \
                mock.patch.object(pa, "_gro", return_value=APPROVE_TXT) as gro:
            ok, msg = self._audit()
        self.assertFalse(ok)
        self.assertIn("needs Codex AND Gro", msg)
        gro.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "tool.py.json")))

    def test_indeterminate_does_not_clear_an_open_reject(self):
        with mock.patch.object(pa, "_codex_review", return_value=("REJECT", REJECT_TXT)):
            self.assertFalse(self._audit()[0])
        with mock.patch.object(pa, "_codex_review", return_value=("INDETERMINATE", "no verdict")):
            self.assertFalse(self._audit()[0])
        with mock.patch.object(pa, "_codex_review", return_value=(None, "usage limit")), \
                mock.patch.object(pa, "_gro", return_value=APPROVE_TXT) as gro:
            self.assertFalse(self._audit()[0])
        gro.assert_not_called()

    def test_codex_signed_file_blocked_when_gro_rejects(self):
        with mock.patch.object(pa, "_codex_signed", return_value=True), \
                mock.patch.object(pa, "_codex_review", return_value=("APPROVE", APPROVE_TXT)), \
                mock.patch.object(pa, "_gro", return_value=REJECT_TXT):
            ok, msg = self._audit()
        self.assertFalse(ok)
        self.assertIn("Gro REJECT", msg)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "tool.py.json")))

    def test_nvidia_substitute_recorded(self):
        pa._LAST_NVIDIA_MODEL = "meta/test-model"
        with mock.patch.object(pa, "_codex_review", return_value=(None, "not installed")), \
                mock.patch.object(pa, "_gro", side_effect=RuntimeError("503 unavailable")), \
                mock.patch.object(pa, "_nvidia", return_value=APPROVE_TXT):
            ok, msg = self._audit(NVIDIA_API_KEY="n")
        self.assertTrue(ok, msg)
        self.assertTrue(self._marker()["reviewer_b"].startswith("NVIDIA_"))
        self.assertTrue(self._rows()[-1]["reviewer"].startswith("NVIDIA_"))

    def test_waive_skips_codex(self):
        with mock.patch.object(pa, "_codex_review") as cr:
            ok, msg = self._audit(waive=True)
        self.assertTrue(ok, msg)
        cr.assert_not_called()
        self.assertEqual((self._marker()["gro"], self._marker()["reviewer_b"]), ("WAIVED", "waived"))
        self.assertEqual([(r["reviewer"], r["verdict"]) for r in self._rows()], [("waived", "WAIVED")])


class BggCallRouting(unittest.TestCase):
    """Design reviews: Codex first, Gro only when Codex is unavailable, --gai-only skips both."""

    def _main(self, argv, codex):
        prompt = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
        prompt.write("Code question about one function; no proposal.\n")
        prompt.close()
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["bgg_call.py", prompt.name] + argv), \
                mock.patch.object(bgg_call, "problems", return_value=[]), \
                mock.patch.object(bgg_call, "_env"), \
                mock.patch.object(pa, "_codex", **codex) as cx, \
                mock.patch.object(bgg_call.urllib.request, "urlopen") as up, \
                mock.patch.dict(sys.modules, {"gai_client": mock.MagicMock(call_gai=lambda *a, **k: "GAI TEXT")}), \
                redirect_stdout(out):
            bgg_call.main()
        os.unlink(prompt.name)
        return out.getvalue(), cx, up

    def test_codex_reply_skips_gro(self):
        text, cx, up = self._main(["--gro-only"], {"return_value": "CODEX TEXT"})
        self.assertIn("=== CODEX ===", text)
        up.assert_not_called()

    def test_codex_unavailable_uses_gro(self):
        text, cx, up = self._main(["--gro-only"], {"side_effect": pa.CodexUnavailable("usage limit")})
        self.assertIn("CODEX UNAVAILABLE", text)
        up.assert_called_once()

    def test_gai_only_skips_codex(self):
        text, cx, up = self._main(["--gai-only"], {"return_value": "CODEX TEXT"})
        cx.assert_not_called()
        up.assert_not_called()
        self.assertIn("GAI TEXT", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
