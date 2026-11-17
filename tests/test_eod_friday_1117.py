# ruff: noqa: E501
"""EOD-FRIDAY-1117 tests (logs/design_records/eod_friday_2026-11-17.md): the restart day list covers every PT weekday
session exactly once, the cron installer edits only its one line, and the one-time lots repair is exact and safe."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import repair_fifo_lots_1117 as repair  # noqa: E402

INSTALL = ROOT / "scripts" / "install_nightly_restart_cron.sh"
UTC, ET, PT = ZoneInfo("UTC"), ZoneInfo("America/New_York"), ZoneInfo("America/Los_Angeles")


def _script_line(name: str) -> str:
    m = re.search(rf'^readonly {name}="(.*)"$', INSTALL.read_text(), re.M)
    assert m, name
    return m.group(1)


def _sessions_covered(day_field: str, start: datetime, days: int) -> dict:
    """PT session date -> number of 02:00 ET restarts that would process it, for cron '0 6,7 * * <day_field>'.
    A restart runs when cron fires at 06:00 or 07:00 UTC on an allowed UTC weekday AND the ET wall clock reads 02:00
    (cron_tz_wrapper); its end-of-day pass processes the PT date at that moment."""
    lo, hi = (int(x) for x in day_field.split("-"))
    allowed = set(range(lo, hi + 1))
    out: dict = {}
    for d in range(days):
        day = (start + timedelta(days=d)).date()
        for hour in (6, 7):
            t = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
            if (t.isoweekday() % 7) not in allowed or t.astimezone(ET).hour != 2:
                continue
            pt_day = t.astimezone(PT).date()
            out[pt_day] = out.get(pt_day, 0) + 1
    return out


class RestartDays(unittest.TestCase):
    def test_new_line_covers_every_weekday_session_once_across_dst(self):
        days = _script_line("NEW_LINE").split()[4]
        self.assertEqual(days, "1-6")
        start = datetime(2026, 1, 1, tzinfo=UTC)          # a full year, both DST switches
        cov = _sessions_covered(days, start, 370)
        for d in range(365):
            day = (start + timedelta(days=d)).date()
            if day.weekday() < 5:                         # Mon-Fri PT session: exactly one pass
                self.assertEqual(cov.get(day, 0), 1, day)
            elif day.weekday() == 5:                      # Saturday: no session, no pass
                self.assertEqual(cov.get(day, 0), 0, day)
            else:                                         # Sunday: the existing Monday-UTC restart, unchanged
                self.assertEqual(cov.get(day, 0), _sessions_covered("1-5", start, 370).get(day, 0), day)

    def test_old_line_missed_friday(self):
        days = _script_line("OLD_LINE").split()[4]
        cov = _sessions_covered(days, datetime(2026, 11, 9, tzinfo=UTC), 14)
        self.assertEqual(cov.get(datetime(2026, 11, 13).date(), 0), 0)   # Friday 11-13: no pass (the bug)

    def test_lines_differ_only_in_day_list(self):
        old, new = _script_line("OLD_LINE").split(), _script_line("NEW_LINE").split()
        self.assertEqual([i for i, (a, b) in enumerate(zip(old, new, strict=True)) if a != b], [4])


class InstallScript(unittest.TestCase):
    """Runs the installer against a fake `crontab` command on PATH (never the real crontab)."""

    def _run(self, initial: str):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "tab"
            store.write_text(initial)
            fake = Path(tmp) / "crontab"
            fake.write_text(f'#!/usr/bin/env bash\nif [ "$1" = "-l" ]; then cat "{store}"; else cp "$1" "{store}"; fi\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}")
            r = subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True, env=env, timeout=30)
            return r.returncode, r.stdout + r.stderr, store.read_text()

    def test_changes_only_the_restart_line(self):
        other = "*/20 13-21 * * 1-5 cd /home/ubuntu/mtf-bot && run_ledger_sync.py\n"
        code, out, tab = self._run(other + _script_line("OLD_LINE") + "\n" + other)
        self.assertEqual(code, 0, out)
        self.assertEqual(tab, other + _script_line("NEW_LINE") + "\n" + other)

    def test_idempotent(self):
        code, out, tab = self._run(_script_line("NEW_LINE") + "\n")
        self.assertEqual(code, 0, out)
        self.assertEqual(tab, _script_line("NEW_LINE") + "\n")

    def test_refuses_when_line_missing_or_duplicated(self):
        for initial in ("", _script_line("OLD_LINE") + "\n" + _script_line("OLD_LINE") + "\n"):
            code, out, tab = self._run(initial)
            self.assertEqual(code, 1, out)
            self.assertEqual(tab, initial)


class Repair(unittest.TestCase):
    GOOD = {"date": "2026-11-16", "open_lots": {"QQQM": repair.EXPECTED, "BRK.B": [{"qty": 2, "price": 544.6, "side": "long"}]},
            "processed_fill_ids": [], "alpaca_today_pnl": 0.0, "alpaca_per_trade": []}

    def _file(self, data):
        d = tempfile.mkdtemp()
        p = Path(d) / "open_lots_prior_day.json"
        p.write_text(json.dumps(data))
        return p

    def test_dry_run_changes_nothing(self):
        p = self._file(self.GOOD)
        before = p.read_text()
        self.assertEqual(repair.main(["--path", str(p)]), 0)
        self.assertEqual(p.read_text(), before)

    def test_apply_removes_only_the_two_lots_and_keeps_a_backup(self):
        p = self._file(self.GOOD)
        with mock.patch.object(repair, "bot_active", return_value=False):
            self.assertEqual(repair.main(["--path", str(p), "--apply"]), 0)
        after = json.loads(p.read_text())
        self.assertNotIn("QQQM", after["open_lots"])
        self.assertEqual(after["open_lots"]["BRK.B"], self.GOOD["open_lots"]["BRK.B"])
        self.assertEqual({k: v for k, v in after.items() if k != "open_lots"},
                         {k: v for k, v in self.GOOD.items() if k != "open_lots"})
        backups = list(p.parent.glob("open_lots_prior_day.json.bak_repair_1117_*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.GOOD)
        self.assertEqual(list(p.parent.glob(".*.tmp")), [])

    def test_refuses_while_bot_active_or_unknown(self):
        p = self._file(self.GOOD)
        before = p.read_text()
        with mock.patch.object(repair, "bot_active", return_value=True):
            self.assertEqual(repair.main(["--path", str(p), "--apply"]), 1)
        self.assertEqual(p.read_text(), before)
        with mock.patch.object(repair.subprocess, "run", side_effect=OSError("no systemctl")):
            self.assertTrue(repair.bot_active())

    def test_refuses_on_unexpected_content_missing_entry_missing_or_corrupt_file(self):
        bad = json.loads(json.dumps(self.GOOD))
        bad["open_lots"]["QQQM"] = [{"qty": 2, "price": 279.91, "side": "long"}]
        for data in (bad, {"open_lots": {}}, {"lots": {"QQQM": repair.EXPECTED}}):
            p = self._file(data)
            before = p.read_text()
            with mock.patch.object(repair, "bot_active", return_value=False):
                self.assertEqual(repair.main(["--path", str(p), "--apply"]), 1)
            self.assertEqual(p.read_text(), before)
        self.assertEqual(repair.main(["--path", "/nonexistent/open_lots_prior_day.json", "--apply"]), 1)
        p = Path(tempfile.mkdtemp()) / "open_lots_prior_day.json"
        p.write_text("{not json")
        self.assertEqual(repair.main(["--path", str(p), "--apply"]), 1)
        self.assertEqual(p.read_text(), "{not json")


if __name__ == "__main__":
    unittest.main()
