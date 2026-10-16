# ruff: noqa: E501
"""scripts/state_freshness_check.py — stale-state alarm (Claude 2026-10-16). Mocked clock, calendar and Slack;
the broker is never reached (get_trading_client is patched; the conftest guard refuses a real client anyway)."""
import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import tests  # noqa: F401  — test guard (blank broker keys, MTF_TEST_MODE)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import state_freshness_check as sfc  # noqa: E402

ET = ZoneInfo("America/New_York")


def _day(d, close_h=16):
    """An Alpaca Calendar row as alpaca-py returns it: naive ET open/close datetimes."""
    return types.SimpleNamespace(date=d, open=datetime(d.year, d.month, d.day, 9, 30),
                                 close=datetime(d.year, d.month, d.day, close_h, 0))


# Oct 2026: Mon 12 .. Fri 16; pretend Wed 14 is a holiday in some tests
WEEK = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]


def _client(days):
    c = mock.Mock()
    c.get_calendar.side_effect = lambda req: [_day(d) for d in days if req.start <= d <= req.end]
    return c


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.files = {
            "dip_zones": (root / "dip_zones.json",) + sfc.FILES["dip_zones"][1:],
            "overnight_k": (root / "overnight_k.json",) + sfc.FILES["overnight_k"][1:],
        }
        self.p_files = mock.patch.object(sfc, "FILES", self.files)
        self.p_files.start()

    def tearDown(self):
        self.p_files.stop()
        self.tmp.cleanup()

    def write(self, name, session, **extra):
        self.files[name][0].write_text(json.dumps({"session": session, "computed_at": "x", **extra}))

    def run_check(self, now, days=WEEK, client=None):
        cl = client or _client(days)
        with mock.patch("execution.broker.get_trading_client", return_value=cl):
            return sfc.check(now), cl


class Staleness(Base):
    MORNING_FRI = datetime(2026, 10, 16, 8, 50, tzinfo=ET)    # check runs before Friday's open

    def test_fresh_today_no_page(self):
        self.write("dip_zones", "2026-10-16")
        self.write("overnight_k", "2026-10-16")
        out, _ = self.run_check(self.MORNING_FRI)
        self.assertEqual(out, [])

    def test_previous_session_is_not_stale(self):
        # Friday morning run failed, Thursday's runs fine: only Thursday's close passed since -> zero completed after
        self.write("dip_zones", "2026-10-15")
        self.write("overnight_k", "2026-10-15")
        out, _ = self.run_check(self.MORNING_FRI)
        self.assertEqual(out, [])

    def test_one_completed_session_missed_pages(self):
        self.write("dip_zones", "2026-10-14")
        self.write("overnight_k", "2026-10-16")
        out, _ = self.run_check(self.MORNING_FRI)
        self.assertEqual(len(out), 1)
        self.assertIn("dip_zones.json", out[0])
        self.assertIn("1 trading session has closed", out[0])
        self.assertIn("Thu Oct 15", out[0])
        self.assertIn("run_dip_zones.py", out[0])

    def test_holiday_does_not_count(self):
        # Wed 14 is a holiday: file from Tue 13, check Thu morning -> no completed session after Tue
        self.write("dip_zones", "2026-10-13")
        self.write("overnight_k", "2026-10-13")
        days = [d for d in WEEK if d != date(2026, 10, 14)]
        out, _ = self.run_check(datetime(2026, 10, 15, 8, 50, tzinfo=ET), days=days)
        self.assertEqual(out, [])

    def test_session_not_completed_until_its_close(self):
        # file from Thu 15; at Fri 15:59 ET Friday is still open -> fresh; at 16:00 ET Friday has closed -> stale
        self.write("dip_zones", "2026-10-15")
        self.write("overnight_k", "2026-10-16")
        out, _ = self.run_check(datetime(2026, 10, 16, 15, 59, tzinfo=ET))
        self.assertEqual(out, [])
        out, _ = self.run_check(datetime(2026, 10, 16, 16, 0, tzinfo=ET))
        self.assertEqual(len(out), 1)

    def test_half_day_close_from_calendar(self):
        # a 13:00 ET early close counts as completed at 13:00, read from the calendar row, not an hour constant
        self.write("dip_zones", "2026-10-15")
        self.write("overnight_k", "2026-10-16")
        cl = mock.Mock()
        cl.get_calendar.side_effect = lambda req: [_day(date(2026, 10, 16), close_h=13)]
        out, _ = self.run_check(datetime(2026, 10, 16, 13, 5, tzinfo=ET), client=cl)
        self.assertEqual(len(out), 1)

    def test_weekend_counts_no_sessions(self):
        # file from Fri 16; Monday-morning check: Sat/Sun are not sessions -> fresh
        self.write("dip_zones", "2026-10-16")
        self.write("overnight_k", "2026-10-16")
        out, _ = self.run_check(datetime(2026, 10, 19, 8, 50, tzinfo=ET))
        self.assertEqual(out, [])

    def test_aware_calendar_close_is_honoured(self):
        self.write("dip_zones", "2026-10-15")
        self.write("overnight_k", "2026-10-16")
        cl = mock.Mock()
        cl.get_calendar.side_effect = lambda req: [types.SimpleNamespace(
            date=date(2026, 10, 16), close=datetime(2026, 10, 16, 20, 0, tzinfo=ZoneInfo("UTC")))]   # 16:00 ET
        out, _ = self.run_check(datetime(2026, 10, 16, 15, 59, tzinfo=ET), client=cl)
        self.assertEqual(out, [])
        out, _ = self.run_check(datetime(2026, 10, 16, 16, 1, tzinfo=ET), client=cl)
        self.assertEqual(len(out), 1)

    def test_calendar_queried_only_after_the_file_session(self):
        self.write("dip_zones", "2026-10-13")
        self.write("overnight_k", "2026-10-16")
        _, cl = self.run_check(self.MORNING_FRI)
        req = cl.get_calendar.call_args.args[0]
        self.assertEqual(req.start, date(2026, 10, 14))
        self.assertEqual(req.end, date(2026, 10, 16))


class FallbackAndBadFiles(Base):
    def test_calendar_unreadable_uses_weekdays_before_today(self):
        self.write("dip_zones", "2026-10-14")
        self.write("overnight_k", "2026-10-15")
        bad = mock.Mock()
        bad.get_calendar.side_effect = RuntimeError("boom")
        out, _ = self.run_check(datetime(2026, 10, 16, 23, 0, tzinfo=ET), client=bad)
        # weekdays strictly before Fri 16: dip zones (Wed) missed Thu -> stale; overnight K (Thu) -> fresh
        self.assertEqual(len(out), 1)
        self.assertIn("dip_zones.json", out[0])
        self.assertIn("weekdays were counted", out[0])

    def test_broker_refusal_in_test_falls_back(self):
        # unpatched: the test guard refuses a real client -> weekday fallback, never a broker call.
        # Thu Oct 08 -> check Fri Oct 16: weekdays strictly before today are Oct 09, 12-15 (the weekend is skipped)
        self.write("dip_zones", "2026-10-08")
        self.write("overnight_k", "2026-10-16")
        out = sfc.check(datetime(2026, 10, 16, 9, 0, tzinfo=ET))
        self.assertEqual(len(out), 1)
        self.assertIn("5 trading sessions have closed", out[0])
        self.assertIn("Fri Oct 09, Mon Oct 12, Tue Oct 13, Wed Oct 14, Thu Oct 15", out[0])
        self.assertIn("weekdays were counted", out[0])

    def test_empty_calendar_falls_back_to_weekdays(self):
        self.write("dip_zones", "2026-10-01")
        self.write("overnight_k", "2026-10-16")
        cl = mock.Mock()
        cl.get_calendar.return_value = []
        out, _ = self.run_check(datetime(2026, 10, 16, 23, 0, tzinfo=ET), client=cl)
        self.assertEqual(len(out), 1)
        self.assertIn("weekdays were counted", out[0])

    def test_future_session_pages(self):
        self.write("dip_zones", "2099-01-01")
        self.write("overnight_k", "2026-10-16")
        out, cl = self.run_check(datetime(2026, 10, 16, 8, 50, tzinfo=ET))
        self.assertEqual(len(out), 1)
        self.assertIn("in the future", out[0])

    def test_string_calendar_fields_and_rows_on_or_before_the_session(self):
        # a row dated on/before the file's session is ignored; string date/close are parsed
        self.write("dip_zones", "2026-10-14")
        self.write("overnight_k", "2026-10-16")
        cl = mock.Mock()
        cl.get_calendar.return_value = [
            types.SimpleNamespace(date="2026-10-14", close="2026-10-14 16:00"),
            types.SimpleNamespace(date="2026-10-15", close="2026-10-15T16:00:00"),
        ]
        out, _ = self.run_check(datetime(2026, 10, 16, 8, 50, tzinfo=ET), client=cl)
        self.assertEqual(len(out), 1)
        self.assertIn("1 trading session has closed since then (Thu Oct 15)", out[0])

    def test_missing_file_pages(self):
        self.write("overnight_k", "2026-10-16")
        out, _ = self.run_check(datetime(2026, 10, 16, 8, 50, tzinfo=ET))
        self.assertEqual(len(out), 1)
        self.assertIn("does not exist", out[0])

    def test_non_object_json_pages(self):
        self.files["dip_zones"][0].write_text("[]")
        self.write("overnight_k", "2026-10-16")
        out, _ = self.run_check(datetime(2026, 10, 16, 8, 50, tzinfo=ET))
        self.assertEqual(len(out), 1)
        self.assertIn("not a JSON object", out[0])

    def test_unstamped_and_corrupt_files_page(self):
        self.files["dip_zones"][0].write_text(json.dumps({"zones": {}}))
        self.files["overnight_k"][0].write_text("{not json")
        out, _ = self.run_check(datetime(2026, 10, 16, 8, 50, tzinfo=ET))
        self.assertEqual(len(out), 2)
        self.assertIn("no session date", out[0])
        self.assertIn("could not be read", out[1])

    def test_read_session_real_writer_format(self):
        # the exact keys the writers stamp (dip_zones.refresh / overnight_capacity.refresh)
        self.write("overnight_k", "2026-10-16", k_dyn=1.4, ceiling=1.9)
        s, comp, err = sfc.read_session(self.files["overnight_k"][0])
        self.assertEqual((s, comp, err), (date(2026, 10, 16), "x", ""))


class Main(Base):
    def test_pages_slack_once_with_all_problems_and_exit_1(self):
        slack = mock.Mock()
        with mock.patch.object(sfc, "check", return_value=["a", "b"]), \
                mock.patch.dict(sys.modules, {"alerts": types.SimpleNamespace(send_slack=slack)}):
            rc = sfc.main()
        self.assertEqual(rc, 1)
        slack.assert_called_once()
        self.assertIn("• a", slack.call_args.args[0])
        self.assertIn("• b", slack.call_args.args[0])

    def test_fresh_no_slack_exit_0(self):
        slack = mock.Mock()
        with mock.patch.object(sfc, "check", return_value=[]), \
                mock.patch.dict(sys.modules, {"alerts": types.SimpleNamespace(send_slack=slack)}):
            rc = sfc.main()
        self.assertEqual(rc, 0)
        slack.assert_not_called()

    def test_slack_failure_never_raises(self):
        def boom(_m):
            raise RuntimeError("webhook down")
        with mock.patch.object(sfc, "check", return_value=["a"]), \
                mock.patch.dict(sys.modules, {"alerts": types.SimpleNamespace(send_slack=boom)}):
            self.assertEqual(sfc.main(), 1)


if __name__ == "__main__":
    unittest.main()
