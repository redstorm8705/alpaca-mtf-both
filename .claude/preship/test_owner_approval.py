# ruff: noqa: E501
"""Tests for the owner-approval gates (design record logs/design_records/owner_approval_gates_2026-10-17.md)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
GATE = os.path.join(HERE, "owner_approval_gate.py")
INTEGRITY = os.path.join(HERE, "behaviour_integrity.py")
CAPTURE = os.path.join(HERE, "owner_approval_capture.py")
LAUNCHER = os.path.join(REPO, "scripts", "managed_gates", "run_gate.py")
with open(os.path.join(HERE, "required_questions.json"), encoding="utf-8") as _f:
    _NQ = len(json.load(_f)["questions"])  # all required questions
DESIGN = """# Test design

Approval-ID: TEST-42
Covers: execution/thing.py, server-ops

## Required questions
""" + "".join(f"Q{i}: answered fully for this unit test case\n" for i in range(1, _NQ + 1)) + """
## Prior work
- none
"""


def run(script, payload, root, *args):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=root, MTF_TEST_MODE="1")   # no Slack from tests
    r = subprocess.run([sys.executable, script, *args], input=json.dumps(payload).encode(), env=env, capture_output=True)
    return r.returncode, r.stderr.decode() + r.stdout.decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        for d in ("execution", "logs/design_records", ".claude/approvals", "docs"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        with open(os.path.join(self.root, "logs/design_records/test.md"), "w") as f:
            f.write(DESIGN)
        self.transcript = os.path.join(self.root, "t.jsonl")
        open(self.transcript, "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def owner_says(self, text, real=True):
        if real:
            with open(self.transcript, "a") as f:
                f.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        return run(CAPTURE, {"prompt": text, "session_id": "s", "transcript_path": self.transcript}, self.root)

    def gate(self, tool, ti):
        return run(GATE, {"tool_name": tool, "tool_input": ti}, self.root)[0]


class CaptureTests(Base):
    def test_approve_writes_record_and_reject_removes(self):
        self.owner_says("APPROVE TEST-42")
        rec = json.load(open(os.path.join(self.root, ".claude/approvals/TEST-42.json")))
        self.assertEqual(rec["covers"], ["execution/thing.py", "server-ops"])
        self.owner_says("REJECT TEST-42")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/TEST-42.json")))

    def test_unknown_id_records_nothing(self):
        self.owner_says("APPROVE NOPE-1")
        self.assertFalse(os.listdir(os.path.join(self.root, ".claude/approvals")))

    def _natural_design(self):
        """A design whose ID ends in a number (here today's MMDD), so natural wording can name it."""
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).date()      # the capture hook dates designs in UTC
        self.n = day.strftime("%m%d")
        os.remove(os.path.join(self.root, "logs/design_records/test.md"))
        with open(os.path.join(self.root, f"logs/design_records/test_{day.isoformat()}.md"), "w") as f:
            f.write(DESIGN.replace("Approval-ID: TEST-42", f"Approval-ID: TEST-{self.n}"))

    def test_natural_wording_approves_and_gate_accepts_it(self):
        self._natural_design()
        self.owner_says(f"Approve test {self.n}. \n\nApprove master brain cleanup.")
        self.assertTrue(os.path.exists(os.path.join(self.root, f".claude/approvals/TEST-{self.n}.json")))
        self.assertEqual(self.gate("Edit", {"file_path": "execution/thing.py"}), 0)
        self.owner_says(f"reject test {self.n}")
        self.assertFalse(os.path.exists(os.path.join(self.root, f".claude/approvals/TEST-{self.n}.json")))
        self.owner_says(f"Approved test {self.n}\r\n")
        self.assertTrue(os.path.exists(os.path.join(self.root, f".claude/approvals/TEST-{self.n}.json")))

    def test_non_decisions_record_nothing(self):
        self._natural_design()
        for text in (f"don\u2019t approve {self.n}", f"I wouldn't approve {self.n}", f"I refuse to approve {self.n}",
                     f"I would not, under any circumstances, approve {self.n}", f"approve {self.n} later, not now",
                     f"Explain what happens if I approve {self.n}", f"if I approve {self.n}, what changes",
                     f"Gro approved {self.n} but GAI rejected it", f"should I approve {self.n}?", f"approve {self.n} ?",
                     f'You wrote: "reply \'approve test {self.n}\' to proceed"', f"> approve {self.n}", "approve 1",
                     "approve master brain cleanup", f"approve {self.n} \u2014 no wait, reject {self.n}"):
            self.owner_says(text)
            self.assertFalse(os.path.exists(os.path.join(self.root, f".claude/approvals/TEST-{self.n}.json")), text)

    def test_pr_number_and_bare_number_approve_when_unique(self):
        os.remove(os.path.join(self.root, "logs/design_records/test.md"))
        with open(os.path.join(self.root, "logs/design_records/pr.md"), "w") as f:
            f.write(DESIGN.replace("Approval-ID: TEST-42", "Approval-ID: PR-689"))
        for text in ("approve 689", "Approve PR 689.", "approved pr 689"):    # owner 2026-10-17: a PR number works
            self.owner_says(text)
            self.assertTrue(os.path.exists(os.path.join(self.root, ".claude/approvals/PR-689.json")), text)
            self.owner_says("reject 689")
            self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/PR-689.json")), text)
        with open(os.path.join(self.root, "logs/design_records/exit.md"), "w") as f:
            f.write("Approval-ID: EXIT-689\nCovers: execution/thing.py\n")       # a rival with the same number
        self.owner_says("approve 689")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/PR-689.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/EXIT-689.json")))
        self.owner_says("approve pr 689")                     # naming the word resolves it
        self.assertTrue(os.path.exists(os.path.join(self.root, ".claude/approvals/PR-689.json")))

    def test_every_recorded_approval_pages_the_owner(self):
        self._natural_design()
        sys.path.insert(0, HERE)
        import owner_approval_capture as cap
        sent = []
        orig = cap._page_owner
        cap._page_owner = lambda text: sent.append(text) or True      # in-process stub; no env-var seam
        try:
            for text in (f"approve test {self.n}", f"reject test {self.n}", f"APPROVE TEST-{self.n}"):
                with open(self.transcript, "a") as f:
                    f.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
                cap.capture({"prompt": text, "session_id": "s", "transcript_path": self.transcript}, self.root)
        finally:
            cap._page_owner = orig
        self.assertEqual(len(sent), 2)                                 # natural + strict approvals, not the reject
        self.assertTrue(all(f"TEST-{self.n}" in p and "execution/thing.py" in p for p in sent))

    def test_undelivered_notice_is_reported(self):
        self._natural_design()
        code, out = self.owner_says(f"approve test {self.n}")          # MTF_TEST_MODE=1: no Slack -> False
        self.assertIn("NOT delivered", out)

    def test_reported_or_example_lines_record_nothing(self):
        self._natural_design()
        for text in (f"Gro:\napproved {self.n}", f"e.g. approve {self.n}", f"i.e. approved {self.n}"):
            self.owner_says(text)
            self.assertFalse(os.path.exists(os.path.join(self.root, f".claude/approvals/TEST-{self.n}.json")), text)

    def test_ambiguous_or_short_ids_record_nothing(self):
        self._natural_design()
        with open(os.path.join(self.root, "logs/design_records/other.md"), "w") as f:
            f.write(f"Approval-ID: OTHER-{self.n}\nCovers: docs/x.md\n\n## Required questions\n")
        self.owner_says(f"approve {self.n}")                  # two designs end in the same number
        self.assertFalse(os.path.exists(os.path.join(self.root, f".claude/approvals/TEST-{self.n}.json")))
        self.assertFalse(os.path.exists(os.path.join(self.root, f".claude/approvals/OTHER-{self.n}.json")))
        with open(os.path.join(self.root, "logs/design_records/option.md"), "w") as f:
            f.write("Approval-ID: OPTION-2\nCovers: execution/thing.py\n\n## Required questions\n")
        self.owner_says("approve option 2")                   # IDs not ending in 3+ digits need the strict form
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/OPTION-2.json")))


class WriteGateTests(Base):
    def test_behaviour_write_blocked_without_approval(self):
        self.assertEqual(self.gate("Edit", {"file_path": "execution/thing.py"}), 2)
        self.assertEqual(self.gate("Write", {"file_path": "docs/notes.md"}), 0)
        self.assertEqual(self.gate("Read", {"file_path": "execution/thing.py"}), 0)

    def test_approved_path_allowed_other_paths_not(self):
        self.owner_says("APPROVE TEST-42")
        self.assertEqual(self.gate("Edit", {"file_path": "execution/thing.py"}), 0)
        self.assertEqual(self.gate("Edit", {"file_path": "execution/other.py"}), 2)

    def test_forged_record_without_owner_message_is_refused(self):
        self.owner_says("APPROVE TEST-42", real=False)       # no user entry in the transcript
        self.assertEqual(self.gate("Edit", {"file_path": "execution/thing.py"}), 2)

    def test_user_settings_never_writable_by_tools(self):
        self.assertEqual(self.gate("Edit", {"file_path": "/Users/x/.claude/settings.json"}), 2)
        self.assertEqual(self.gate("Write", {"file_path": "/Users/x/.claude/settings.local.json"}), 2)
        self.assertEqual(self.gate("Bash", {"command": "echo '{}' > ~/.claude/settings.json"}), 2)
        self.assertEqual(self.gate("Bash", {"command": "cat ~/.claude/settings.json"}), 0)
        self.assertEqual(self.gate("Read", {"file_path": "/Users/x/.claude/settings.json"}), 0)
        home = os.path.expanduser("~")
        for spelled in (f"{home}/.claude//settings.json", f"{home}/.claude/./settings.json",
                        f"{home}/.claude/../.claude/settings.local.json",
                        os.path.relpath(f"{home}/.claude/settings.json", self.root)):
            self.assertEqual(self.gate("Edit", {"file_path": spelled}), 2, spelled)

    def test_settings_watch_reports_any_change(self):
        sys.path.insert(0, HERE)
        import behaviour_integrity as bi
        import owner_approval_gate as oag
        fake = os.path.join(self.root, "fake_settings.json")
        with open(fake, "w") as f:
            f.write("{}")
        orig = oag.settings_files
        oag.settings_files = lambda root=None: [fake]
        snap = os.path.join(self.root, ".claude/quarantine/settings_watch.json")
        try:
            self.assertTrue(bi.watch_settings(self.root)[0].startswith("(settings watch baseline missing"))
            bi.commit_settings(self.root)
            self.assertEqual(bi.watch_settings(self.root), [])          # unchanged
            with open(fake, "w") as f:                                  # changed by any route
                f.write('{"env": {"MTF_TEST_MODE": "1"}}')
            self.assertEqual(bi.watch_settings(self.root), [fake])
            self.assertEqual(bi.watch_settings(self.root), [fake])      # not delivered yet: reported again
            bi.commit_settings(self.root)                               # delivered: baseline advances
            self.assertEqual(bi.watch_settings(self.root), [])
            os.remove(snap)                                             # a deleted baseline is reported
            self.assertTrue(bi.watch_settings(self.root)[0].startswith("(settings watch baseline missing"))
            with open(snap, "w") as f:                                  # a corrupted one too
                f.write("[]")
            self.assertTrue(bi.watch_settings(self.root)[0].startswith("(settings watch baseline unreadable"))
        finally:
            oag.settings_files = orig

    def test_approvals_dir_never_writable_by_tools(self):
        self.assertEqual(self.gate("Write", {"file_path": ".claude/approvals/X-1.json"}), 2)

    def test_mcp_edit_tool_is_gated(self):
        self.assertEqual(self.gate("mcp__token-savior__replace_symbol_source", {"file_path": "execution/thing.py"}), 2)

    def test_gate_files_are_behaviour(self):
        self.assertEqual(self.gate("Edit", {"file_path": ".claude/preship/preship_gate.py"}), 2)
        self.assertEqual(self.gate("Edit", {"file_path": ".claude/settings.local.json"}), 2)


class BashGateTests(Base):
    def b(self, cmd):
        return self.gate("Bash", {"command": cmd})

    def test_hook_skipping_always_blocked(self):
        for cmd in ("git -c core.hooksPath=/dev/null commit -m x", "git commit --no-verify -m x", "git commit -nm x",
                    "GIT_CONFIG_COUNT=1 git commit -m x", "git push --no-verify"):
            self.assertEqual(self.b(cmd), 2, cmd)

    def test_reads_and_redirects_to_scratch_allowed(self):
        self.assertEqual(self.b("git diff origin/main -- execution/thing.py > /tmp/x.diff"), 0)
        self.assertEqual(self.b("cat execution/thing.py | head"), 0)
        self.assertEqual(self.b("git commit -m 'fix execution/thing.py'"), 0)   # commit is checked by preship_gate

    def test_shell_writes_to_behaviour_paths_blocked(self):
        for cmd in ("sed -i 's/a/b/' execution/thing.py", "echo x > execution/thing.py",
                    "python3 -c \"open('execution/thing.py','w').write('x')\"", "cp /tmp/x execution/thing.py",
                    "git checkout HEAD~1 -- execution/thing.py"):
            self.assertEqual(self.b(cmd), 2, cmd)

    def test_server_writes_need_server_ops(self):
        self.assertEqual(self.b("ssh mtf-bot 'tail -5 /home/ubuntu/mtf-bot/logs/mtf_bot.log'"), 0)
        self.assertEqual(self.b("ssh mtf-bot 'cd /home/ubuntu/mtf-bot && git pull origin main --ff-only'"), 0)
        self.assertEqual(self.b("ssh mtf-bot '(crontab -l; echo x) | crontab -'"), 2)
        self.owner_says("APPROVE TEST-42")
        self.assertEqual(self.b("ssh mtf-bot '(crontab -l; echo x) | crontab -'"), 0)

    def test_direct_bgg_calls_blocked_wrapper_allowed(self):
        self.assertEqual(self.b("curl https://api.groq.com/openai/v1/chat/completions -d @p.json"), 2)
        self.assertEqual(self.b("python3 .claude/preship/bgg_call.py /tmp/prompt.txt"), 0)
        script = os.path.join(self.root, "my_bgg.py")
        open(script, "w").write("import gai_client\n")
        self.assertEqual(self.b(f"python3 {script} prompt.txt"), 2)


class RequiredQuestionsTests(Base):
    def test_design_prompt_needs_frame_and_the_full_question_block(self):
        sys.path.insert(0, HERE)
        import bgg_call
        import required_questions as rq
        bad = "PAPER account, goal $25K. Design review. Is the statistics sound? End with VERDICT."
        self.assertTrue(bgg_call.problems(bad))
        good = bad + "\n\n" + rq.block()
        self.assertEqual(bgg_call.problems(good), [])
        self.assertTrue(bgg_call.problems(good + " The board already approved it."))
        partial = good.replace(rq.load()["questions"][4]["q"][:60], "")
        self.assertTrue(bgg_call.problems(partial))                    # dropping one question is refused

    def test_board_design_prompt_without_block_is_blocked(self):
        bias = os.path.join(HERE, "bgg_prompt_bias_gate.py")
        sys.path.insert(0, HERE)
        import required_questions as rq
        p = "PAPER account, $25K goal. Board seat design review of X. Give a verdict, approve or reject."
        self.assertEqual(run(bias, {"tool_name": "Agent", "tool_input": {"prompt": p}}, self.root)[0], 2)
        self.assertEqual(run(bias, {"tool_name": "Agent", "tool_input": {"prompt": p + "\n" + rq.block()}},
                             self.root)[0], 0)

    def test_blank_or_placeholder_answers_do_not_count(self):
        import required_questions as rq
        blank = "## Required questions\n" + "".join(f"Q{i}:\n" for i in range(1, _NQ + 1))
        self.assertEqual(len(rq.unanswered(blank)), _NQ)
        na = "## Required questions\n" + "".join(f"Q{i}: N/A\n" for i in range(1, _NQ + 1))
        self.assertEqual(len(rq.unanswered(na)), _NQ)
        ok = "### Required Questions (RQ-3)\n" + "".join(f"- **Q{i}:** N/A — no trading code changes\n" for i in range(1, _NQ + 1))
        self.assertEqual(rq.unanswered(ok), [])
        for filler in ("TBD TBD TBD", "yes yes yes", "see above for details", "... ... ...", "a b c", "N/A - see above ok"):
            body = "## Required questions\n" + "".join(f"Q{i}: {filler}\n" for i in range(1, _NQ + 1))
            self.assertEqual(len(rq.unanswered(body)), _NQ, filler)

    def test_proposal_prompt_needs_the_block(self):
        import required_questions as rq
        self.assertTrue(rq.is_design_prompt("Draft a design proposal for the Day-tier exit"))
        self.assertTrue(rq.is_design_prompt("write the design record for X"))
        self.assertFalse(rq.is_design_prompt("Cold second-agent review of this diff: check off-by-one"))

    def test_ordinary_prompts_mentioning_proposal_are_not_blocked(self):
        bias = os.path.join(HERE, "bgg_prompt_bias_gate.py")
        for p in ("Explore: propose nothing; read execution/x.py verbatim",
                  "How do I propose a hook in claude code? check the settings docs",
                  "Summarise the proposal section of this design record for me"):
            self.assertEqual(run(bias, {"tool_name": "Agent", "tool_input": {"prompt": p}}, self.root)[0], 0, p)

    def test_unanswered_design_cannot_be_approved(self):
        open(os.path.join(self.root, "logs/design_records/test.md"), "w").write(
            DESIGN.replace("Q7: answered fully for this unit test case\n", ""))
        out = self.owner_says("APPROVE TEST-42")[1]
        self.assertIn("Q7", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/TEST-42.json")))


class LauncherTests(unittest.TestCase):
    def test_manifest_mismatch_blocks_and_match_runs(self):
        with tempfile.TemporaryDirectory() as d:
            proj = os.path.join(d, "alpaca-mtf-bot_X")
            pre = os.path.join(proj, ".claude", "preship")
            os.makedirs(pre)
            open(os.path.join(pre, "g.py"), "w").write("import sys; sys.exit(0)\n")
            gates = os.path.join(d, "gates")
            os.makedirs(gates)
            import shutil
            shutil.copy(LAUNCHER, gates)
            sha = hashlib.sha256(open(os.path.join(pre, "g.py"), "rb").read()).hexdigest()
            json.dump({"files": {"g.py": sha}}, open(os.path.join(gates, "manifest.json"), "w"))
            env = dict(os.environ, CLAUDE_PROJECT_DIR=proj)
            ok = subprocess.run([sys.executable, os.path.join(gates, "run_gate.py"), "g.py"], input=b"{}", env=env)
            self.assertEqual(ok.returncode, 0)
            open(os.path.join(pre, "g.py"), "a").write("# edited\n")
            bad = subprocess.run([sys.executable, os.path.join(gates, "run_gate.py"), "g.py"], input=b"{}", env=env,
                                 capture_output=True)
            self.assertEqual(bad.returncode, 2)
            other = dict(os.environ, CLAUDE_PROJECT_DIR=os.path.join(d, "some-other-project"))
            self.assertEqual(subprocess.run([sys.executable, os.path.join(gates, "run_gate.py"), "g.py"],
                                            input=b"{}", env=other).returncode, 0)


class ReviewFixTests(Base):
    def b(self, cmd):
        return self.gate("Bash", {"command": cmd})

    def test_an_unrelated_owner_message_cannot_back_a_forged_approval(self):
        self.owner_says("looks good, continue")                       # real owner text, no APPROVE
        import hashlib as h
        rec = {"id": "TEST-42", "status": "APPROVED", "covers": ["execution/thing.py"], "transcript_path": self.transcript,
               "prompt_sha256": h.sha256(b"looks good, continue").hexdigest()}
        json.dump(rec, open(os.path.join(self.root, ".claude/approvals/TEST-42.json"), "w"))
        self.assertEqual(self.gate("Edit", {"file_path": "execution/thing.py"}), 2)

    def test_routine_commands_not_blocked(self):
        self.assertEqual(self.b("ssh mtf-bot 'tail -50 /home/ubuntu/mtf-bot/logs/mtf_bot.log 2>&1 | grep ERROR'"), 0)
        self.assertEqual(self.b("git commit -m 'docs: explain why --no-verify is blocked'"), 0)

    def test_directory_and_variable_forms_blocked(self):
        self.assertEqual(self.b("git checkout main -- execution"), 2)
        self.assertEqual(self.b("cp /tmp/x $PWD/execution/b.py"), 2)
        self.assertEqual(self.b("echo x >> ~/.claude/projects/foo/t.jsonl"), 2)


class IntegrityTests(Base):
    def setUp(self):
        super().setUp()
        def g(*a):
            return subprocess.run(["git", "-C", self.root, *a], capture_output=True)
        self.g = g
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        open(os.path.join(self.root, "execution/thing.py"), "w").write("A = 1\n")
        open(os.path.join(self.root, "docs/notes.md"), "w").write("n\n")
        g("add", "-A")
        g("commit", "-qm", "init")
        sys.path.insert(0, HERE)
        import behaviour_integrity as bi
        bi.commit_settings(self.root)        # the settings-watch baseline exists, as after the first install page

    def hook(self):
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.root, MTF_TEST_MODE="1")
        return subprocess.run([sys.executable, INTEGRITY], input=b'{"tool_name":"Bash"}', env=env,
                              capture_output=True).returncode

    def test_any_route_change_is_undone(self):
        # e.g. a script written in /tmp then run — the hook only looks at the file afterwards
        open(os.path.join(self.root, "execution/thing.py"), "w").write("A = 2\n")
        self.assertEqual(self.hook(), 2)
        self.assertEqual(open(os.path.join(self.root, "execution/thing.py")).read(), "A = 1\n")
        kept = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.root, ".claude/quarantine")) for f in fs]
        self.assertTrue(any(open(k).read() == "A = 2\n" for k in kept))   # the undone version is kept, not lost

    def test_new_behaviour_file_is_quarantined_not_deleted(self):
        open(os.path.join(self.root, "execution/new.py"), "w").write("x\n")
        self.assertEqual(self.hook(), 2)
        self.assertFalse(os.path.exists(os.path.join(self.root, "execution/new.py")))
        self.assertTrue(any("new.py" in f for _, _, fs in os.walk(os.path.join(self.root, ".claude/quarantine")) for f in fs))

    def test_approved_or_non_behaviour_changes_are_kept(self):
        open(os.path.join(self.root, "docs/notes.md"), "w").write("changed\n")
        self.owner_says("APPROVE TEST-42")
        open(os.path.join(self.root, "execution/thing.py"), "w").write("A = 3\n")
        self.assertEqual(self.hook(), 0)
        self.assertEqual(open(os.path.join(self.root, "execution/thing.py")).read(), "A = 3\n")


class Round3Tests(IntegrityTests):
    def test_hook_stub_skips_checkout_without_githook(self):
        sys.path.insert(0, HERE)
        import behaviour_integrity as bi
        bi.ensure_git_hooks(self.root)
        open(os.path.join(self.root, "a.txt"), "w").write("x")
        self.g("add", "a.txt")
        r = self.g("commit", "-qm", "plain")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_pre_commit_stands_down_during_merge(self):
        open(os.path.join(self.root, "execution/thing.py"), "w").write("A = 7\n")
        self.g("add", "execution/thing.py")
        open(os.path.join(self.root, ".git/MERGE_HEAD"), "w").write("0" * 40 + "\n")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.root)
        r = subprocess.run([sys.executable, os.path.join(HERE, "owner_approval_githook.py"), "pre-commit"],
                           cwd=self.root, env=env, capture_output=True)
        self.assertEqual(r.returncode, 0)


class Round2Tests(IntegrityTests):
    def test_covers_widened_after_approval_is_refused(self):
        self.owner_says("APPROVE TEST-42")
        self.assertEqual(self.gate("Edit", {"file_path": "execution/thing.py"}), 0)
        p = os.path.join(self.root, "logs/design_records/test.md")
        open(p, "w").write(DESIGN.replace("execution/thing.py, server-ops", "execution/, server-ops"))
        self.assertEqual(self.gate("Edit", {"file_path": "execution/other.py"}), 2)
        self.assertEqual(self.gate("Edit", {"file_path": "execution/thing.py"}), 2)   # the whole approval is void

    def test_duplicate_approval_id_records_nothing(self):
        open(os.path.join(self.root, "logs/design_records/0dup.md"), "w").write(DESIGN)
        self.owner_says("APPROVE TEST-42")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/TEST-42.json")))

    def test_approve_must_start_a_line(self):
        self.owner_says("do NOT APPROVE TEST-42")
        self.assertFalse(os.path.exists(os.path.join(self.root, ".claude/approvals/TEST-42.json")))

    def test_merge_in_progress_is_left_alone(self):
        open(os.path.join(self.root, "execution/thing.py"), "w").write("A = 9\n")
        gd = os.path.join(self.root, ".git")
        open(os.path.join(gd, "MERGE_HEAD"), "w").write("0" * 40 + "\n")
        self.assertEqual(self.hook(), 0)
        self.assertEqual(open(os.path.join(self.root, "execution/thing.py")).read(), "A = 9\n")

    def test_local_settings_hook_keys_are_stripped(self):
        p = os.path.join(self.root, ".claude/settings.local.json")
        json.dump({"disableAllHooks": True, "permissions": {}}, open(p, "w"))
        self.assertEqual(self.hook(), 2)
        self.assertEqual(json.load(open(p)), {"permissions": {}})

    def test_git_pre_commit_refuses_unapproved_behaviour_commit(self):
        open(os.path.join(self.root, "execution/thing.py"), "w").write("A = 5\n")
        self.g("add", "execution/thing.py")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=self.root)
        r = subprocess.run([sys.executable, os.path.join(HERE, "owner_approval_githook.py"), "pre-commit"],
                           cwd=self.root, env=env, capture_output=True)
        self.assertEqual(r.returncode, 1)
        self.owner_says("APPROVE TEST-42")
        r = subprocess.run([sys.executable, os.path.join(HERE, "owner_approval_githook.py"), "pre-commit"],
                           cwd=self.root, env=env, capture_output=True)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
