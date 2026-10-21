# ruff: noqa: E501
"""AUDIT-DIFF-1020: a modified file too long to send whole goes to the nightly auditor as its last-24h diff, not as
a head-truncated copy that hides the change. No git or network: the file tree is a temp dir and the diff is mocked."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import nightly_audit as na


class CollectModifiedFiles(unittest.TestCase):
    def _tree(self, files):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, body in files.items():
            p = Path(d.name) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
            os.utime(p, (time.time(), time.time()))
        return Path(d.name)

    def test_long_file_sent_as_labelled_diff(self):
        root = self._tree({"data/gex.py": "x = 1\n" * (na.MAX_FILE_CHARS // 4)})
        with mock.patch.object(na, "BASE_DIR", root), \
                mock.patch.object(na, "_diff_24h", return_value="@@ -1 +1 @@\n-old\n+change=gex-spot-1019\n"):
            out = na._collect_modified_files()
        body = out["data/gex.py"]
        self.assertTrue(body.startswith("# NOTE TO AUDITOR"))
        self.assertIn("LAST-24H DIFF", body)
        self.assertIn("change=gex-spot-1019", body)

    def test_long_file_without_diff_falls_back_to_truncated_head(self):
        root = self._tree({"data/gex.py": "y = 2\n" * (na.MAX_FILE_CHARS // 4)})
        with mock.patch.object(na, "BASE_DIR", root), mock.patch.object(na, "_diff_24h", return_value=None):
            body = na._collect_modified_files()["data/gex.py"]
        self.assertIn(f"[truncated at {na.MAX_FILE_CHARS} chars]", body)
        self.assertLessEqual(len(body), na.MAX_FILE_CHARS + 60)

    def test_short_file_sent_whole(self):
        root = self._tree({"strategy/small.py": "z = 3\n"})
        with mock.patch.object(na, "BASE_DIR", root), mock.patch.object(na, "_diff_24h") as dd:
            body = na._collect_modified_files()["strategy/small.py"]
        dd.assert_not_called()
        self.assertEqual(body, "z = 3\n")

    def test_long_diff_is_capped(self):
        root = self._tree({"data/gex.py": "w = 4\n" * (na.MAX_FILE_CHARS // 4)})
        with mock.patch.object(na, "BASE_DIR", root), mock.patch.object(na, "_diff_24h", return_value="+" * (na.MAX_FILE_CHARS * 2)):
            body = na._collect_modified_files()["data/gex.py"]
        self.assertIn("[diff truncated at", body)

    def test_diff_failure_never_raises(self):
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            self.assertIsNone(na._diff_24h("data/gex.py"))


if __name__ == "__main__":
    unittest.main()
