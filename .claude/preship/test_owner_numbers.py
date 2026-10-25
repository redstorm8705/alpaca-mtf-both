# ruff: noqa: E501  — dense rationale lines run long (project convention)
"""OWNER-NUMBERS-1025 (owner-approved 2026-10-25; board Beck + Schneier, Gro, GAI): a number the owner chose passes
the no-static gate ONLY when his registered approval of the design lists that exact file, constant and value, and the
design record is byte-for-byte what he approved. Every forgery route must fail (written before the code, Beck)."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import no_static_scan as ns  # noqa: E402
import owner_approval_capture as cap  # noqa: E402
import preship_gate as pg  # noqa: E402

AID = "TEST-NUM-1025"
RQ = "\n".join(f"Q{i}: answered with real words for the test design." for i in range(1, 45))
RECORD = (f"# test design\n\nApproval-ID: {AID}\nCovers: config.py\n"
          "Owner numbers: config.py SWING_BREAKOUT_TOP_N=50; config.py SWING_BREAKOUT_MAX_SLOTS=10\n\n"
          f"## Required questions (RQ-4)\n{RQ}\n")
CONFIG = "SWING_BREAKOUT_TOP_N = 50\nSWING_BREAKOUT_MAX_SLOTS = 10\nOTHER = 7\n"


class Parse(unittest.TestCase):
    def test_owner_numbers_line(self):
        self.assertEqual(cap.parse_owner_numbers(RECORD),
                         [("config.py", "SWING_BREAKOUT_TOP_N", 50.0), ("config.py", "SWING_BREAKOUT_MAX_SLOTS", 10.0)])
        self.assertEqual(cap.parse_owner_numbers("no line here"), [])
        self.assertIsNone(cap.parse_owner_numbers("Owner numbers: config.py X=abc\n"))       # malformed -> None
        self.assertIsNone(cap.parse_owner_numbers("Owner numbers: config.py X=1\nOwner numbers: config.py Y=2\n"))

    def test_only_a_single_plain_module_assignment_qualifies(self):
        allowed = {"A": 50.0}
        self.assertEqual(ns.owner_number_lines("A = 50\n", allowed), {(1, 4): ("A", 50.0)})
        self.assertEqual(ns.owner_number_lines("A = 50.0\n", allowed), {(1, 4): ("A", 50.0)})
        self.assertEqual(ns.owner_number_lines("A = 50\nfor A in (1, 2):\n    pass\n", allowed), {})   # re-bound by a loop
        self.assertEqual(ns.owner_number_lines("A = 50\nimport os as A\n", allowed), {})                # re-bound by an import
        self.assertEqual(ns.owner_number_lines("A = 51\n", allowed), {})                    # different value
        self.assertEqual(ns.owner_number_lines("B = 50\n", allowed), {})                    # different name
        self.assertEqual(ns.owner_number_lines("A = 25 * 2\n", allowed), {})                # an expression
        self.assertEqual(ns.owner_number_lines('A = float("50")\n', allowed), {})           # a parsed string
        self.assertEqual(ns.owner_number_lines("A = 50\nA += 1\n", allowed), {})            # augmented assignment
        self.assertEqual(ns.owner_number_lines("A = 50\nA = 50\n", allowed), {})            # assigned twice
        self.assertEqual(ns.owner_number_lines('A = 50\nP = {"A": 99}\n', allowed), {})     # a profile override key
        self.assertEqual(ns.owner_number_lines("def f():\n    A = 50\n", allowed), {})      # not module level
        self.assertEqual(ns.owner_number_lines("A = -50\n", {"A": -50.0}), {(1, 4): ("A", -50.0)})

    def test_same_line_second_number_is_not_waived(self):
        # red-team 2026-10-25: `SWING_BREAKOUT_MAX_SLOTS = 10; MIN_LONG_SCORE = 10` must still block MIN_LONG_SCORE
        src = "SWING_BREAKOUT_MAX_SLOTS = 10; MIN_LONG_SCORE = 10\nRISK = foo(10); SWING_BREAKOUT_TOP_N = 50\n"
        viols = ns.scan("config.py", src)
        with mock.patch.object(pg, "_ci_records", return_value=[RECORD]):
            left, passed = pg._owner_number_waive("config.py", src, viols, ref="abc:config.py", ci=True)
        self.assertEqual(sorted((v.line, v.literal) for v in left), [(1, 10), (2, 10)])
        self.assertEqual(len(passed), 2)


class LocalGate(unittest.TestCase):
    """The local commit hook: a registered, valid owner approval + the unchanged record."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "logs", "design_records"))
        self.rec_path = os.path.join(self.root, "logs", "design_records", "test.md")
        with open(self.rec_path, "w") as f:
            f.write(RECORD)
        cap._DECL_CACHE.clear()
        cap.common_dir.cache_clear()
        cap._worktree_roots.cache_clear()
        cap._TEST_TRANSCRIPT_DIRS[:] = [self.root]
        self.addCleanup(cap._TEST_TRANSCRIPT_DIRS.clear)
        self.tp = os.path.join(self.root, "t.jsonl")

    def _approve(self, text=f"APPROVE {AID}"):
        with open(self.tp, "a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Approve {AID}?"}]}}) + "\n")
            f.write(json.dumps({"type": "user", "uuid": "u1", "timestamp": "2026-10-25T00:00:00Z", "origin": {"kind": "human"},
                                "message": {"role": "user", "content": text}}) + "\n")
        return cap.record(self.root, AID, text, "u1", self.tp, "s", "explicit", f"Approve {AID}?")

    def allowed(self):
        cap._DECL_CACHE.clear()
        return cap.owner_numbers_allowed(self.root, "config.py")

    def test_matching_approved_record_passes(self):
        self.assertIn("recorded", self._approve())
        a = self.allowed()
        self.assertEqual(set(a), {"SWING_BREAKOUT_TOP_N", "SWING_BREAKOUT_MAX_SLOTS"})
        self.assertEqual(a["SWING_BREAKOUT_TOP_N"][0], 50.0)
        self.assertEqual(a["SWING_BREAKOUT_TOP_N"][1], AID)

    def test_unregistered_approval_fails(self):
        self.assertEqual(self.allowed(), {})

    def test_record_edited_after_approval_fails(self):
        self._approve()
        with open(self.rec_path, "w") as f:
            f.write(RECORD.replace("SWING_BREAKOUT_MAX_SLOTS=10", "SWING_BREAKOUT_MAX_SLOTS=25"))
        self.assertEqual(self.allowed(), {})

    def test_other_file_gets_nothing(self):
        self._approve()
        self.assertEqual(cap.owner_numbers_allowed(self.root, "execution/x.py"), {})

    def test_revoked_approval_fails(self):
        self._approve()
        cap.remove(self.root, AID)
        self.assertEqual(self.allowed(), {})

    def test_forged_store_record_fails(self):
        # an agent writes an approval record by hand: no matching owner message in a real transcript
        rec = {"id": AID, "status": "APPROVED", "design_record": self.rec_path, "covers": ["config.py"],
               "covers_sha256": cap.covers_sha(["config.py"]), "prompt_sha256": "0" * 64, "message_identity": "x",
               "mode": "explicit", "transcript_path": self.tp,
               "record_sha256": hashlib.sha256(RECORD.encode()).hexdigest(),
               "owner_numbers": [["config.py", "SWING_BREAKOUT_TOP_N", 50.0]]}
        open(self.tp, "a").close()
        cap._write(self.root, rec)
        self.assertEqual(self.allowed(), {})

    def test_approval_without_record_hash_fails(self):
        # an approval captured before this gate existed carries no record hash: the owner must re-approve
        self._approve()
        p = os.path.join(cap.approvals_dir(self.root), f"{AID}.json")
        rec = json.load(open(p))
        rec.pop("record_sha256")
        cap._write(self.root, rec)
        self.assertEqual(self.allowed(), {})

    def test_codex_transcript_does_not_qualify(self):
        self._approve()
        with mock.patch.object(cap, "_is_codex_transcript", return_value=True):
            self.assertEqual(self.allowed(), {})

    def test_dynamic_gate_waives_only_the_approved_lines(self):
        self._approve()
        viols = [v for v in ns.scan("config.py", CONFIG)]
        self.assertEqual(sorted(v.literal for v in viols), [7, 10, 50])
        with mock.patch.object(pg, "_owner_root", return_value=self.root):
            left, passed = pg._owner_number_waive("config.py", CONFIG, viols, ref="", ci=False)
        self.assertEqual([v.literal for v in left], [7])                    # an unlisted number still fails
        self.assertEqual(len(passed), 2)
        self.assertTrue(all(AID in p for p in passed))


class CiGate(unittest.TestCase):
    """CI has no approval store: it waives only numbers listed in a committed design record that covers the file."""

    def test_ci_reads_committed_records(self):
        viols = ns.scan("config.py", CONFIG)
        with mock.patch.object(pg, "_ci_records", return_value=[RECORD]):
            left, passed = pg._owner_number_waive("config.py", CONFIG, viols, ref="abc:config.py", ci=True)
        self.assertEqual([v.literal for v in left], [7])
        with mock.patch.object(pg, "_ci_records", return_value=[RECORD.replace("Covers: config.py", "Covers: other.py")]):
            left, _ = pg._owner_number_waive("config.py", CONFIG, viols, ref="abc:config.py", ci=True)
        self.assertEqual(sorted(v.literal for v in left), [7, 10, 50])     # the record must cover the file

    def test_conflicting_records_waive_nothing(self):
        viols = ns.scan("config.py", CONFIG)
        other = RECORD.replace(AID, "TEST-NUM-1026").replace("SWING_BREAKOUT_TOP_N=50", "SWING_BREAKOUT_TOP_N=80")
        with mock.patch.object(pg, "_ci_records", return_value=[RECORD, other]):
            left, _ = pg._owner_number_waive("config.py", CONFIG, viols, ref="abc:config.py", ci=True)
        self.assertIn(50, [v.literal for v in left])                        # two values for one name -> fail closed


if __name__ == "__main__":
    unittest.main()
