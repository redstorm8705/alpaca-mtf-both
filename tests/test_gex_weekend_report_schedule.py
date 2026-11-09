# ruff: noqa: E501
"""MORNING-1029 — weekly GEX report schedule move (Saturday -> first trading day of the week, 09:00 ET / 06:00 PT).

Spec §5 + §9a-6: Monday trading day -> sends; Monday holiday -> Tuesday sends; a failed Monday send -> Tuesday sends
("Monday send missing"); second run in the same ISO week -> exits without sending (marker present); the wrapper's ET
target matches 06:00 PT across the 2026-11-01 DST change and the logged ET offset switches at the change.
Every HTTP call is mocked; a guard fails the test on any unmocked network connection.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts.gex_weekend_report as gwr  # noqa: E402
import scripts.cron_tz_wrapper as wrapper  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")


def _no_network(*_a, **_k):
    raise AssertionError("unmocked network call in a test")


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _NetGuard(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection"):
            p = mock.patch(target, side_effect=_no_network)
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        logs = Path(self.tmp.name)
        for name, val in (("_LOGS", logs), ("_HEARTBEAT", logs / "hb.json")):
            p = mock.patch.object(gwr, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.logs = logs


def _fake_get(trading_days: list[str]):
    """requests.get stand-in: calendar returns `trading_days`; every other URL fails (report rows -> UNKNOWN)."""
    calls: list = []

    def get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        if url.endswith("/v2/calendar"):
            start = params["start"]
            return _Resp(200, [{"date": d, "open": "09:30", "close": "16:00"} for d in trading_days if d == start])
        if url.endswith("/bars/latest"):
            return _Resp(200, {"bar": {"c": 600.5, "t": "2026-11-09T13:59:00Z"}})
        return _Resp(500, {})
    return get, calls


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestWeeklyGate(_NetGuard):
    # 2026-11-09 is a Monday (EST: 09:00 ET = 14:00 UTC); 2026-11-10 Tuesday.
    MON = _utc(2026, 11, 9, 14)
    TUE = _utc(2026, 11, 10, 14)

    def _run(self, now, trading_days, slack_ok=True):
        get, calls = _fake_get(trading_days)
        with mock.patch.object(gwr.requests, "get", side_effect=get), \
                mock.patch.object(gwr.gex, "_fetch_contracts", return_value={}), \
                mock.patch.object(gwr, "send_slack_blocks", return_value=slack_ok) as sb, \
                mock.patch.object(gwr, "send_slack"):
            rc = gwr.main(now_utc=now)
        return rc, sb, calls

    def test_monday_trading_day_sends_and_writes_marker(self):
        rc, sb, _ = self._run(self.MON, ["2026-11-09"])
        self.assertEqual(rc, 0)
        sb.assert_called_once()
        self.assertTrue((self.logs / "gex_weekly_sent_2026-W46.json").exists())

    def test_monday_holiday_then_tuesday_sends(self):
        rc, sb, _ = self._run(self.MON, [])          # Monday closed
        sb.assert_not_called()
        self.assertFalse((self.logs / "gex_weekly_sent_2026-W46.json").exists())
        with self.assertLogs("gex_weekend_report", level="INFO") as cm:
            rc, sb, _ = self._run(self.TUE, ["2026-11-10"])
        sb.assert_called_once()
        self.assertTrue(any("Monday send missing" in m for m in cm.output))
        self.assertTrue((self.logs / "gex_weekly_sent_2026-W46.json").exists())

    def test_failed_monday_send_leaves_tuesday_to_send(self):
        self._run(self.MON, ["2026-11-09"], slack_ok=False)
        self.assertFalse((self.logs / "gex_weekly_sent_2026-W46.json").exists())
        rc, sb, _ = self._run(self.TUE, ["2026-11-10"])
        sb.assert_called_once()

    def test_second_run_same_week_exits_without_sending(self):
        self._run(self.MON, ["2026-11-09"])
        rc, sb, calls = self._run(self.TUE, ["2026-11-10"])
        self.assertEqual(rc, 0)
        sb.assert_not_called()
        self.assertEqual(calls, [])                   # marker short-circuits before any HTTP call

    def test_calendar_down_does_not_send(self):
        with mock.patch.object(gwr.requests, "get", return_value=_Resp(503, {})), \
                mock.patch.object(gwr, "send_slack_blocks") as sb:
            gwr.main(now_utc=self.MON)
        sb.assert_not_called()
        hb = json.loads((self.logs / "hb.json").read_text())
        self.assertFalse(hb["ok"])

    def test_spot_is_latest_premarket_bar_with_time(self):
        get, _ = _fake_get(["2026-11-09"])
        with mock.patch.object(gwr.requests, "get", side_effect=get), \
                mock.patch.object(gwr.gex, "_fetch_contracts", return_value={"X": 1.0}), \
                mock.patch.object(gwr.gex, "_fetch_snapshots", return_value={"X": {}}), \
                mock.patch.object(gwr.gex, "_compute_gex", return_value={"label": "POSITIVE", "pin": {}}):
            row = gwr._compute_symbol("SPY", "2026-11-09", "2026-11-13")
        self.assertEqual(row["spot"], 600.5)
        self.assertEqual(row["spot_time_pt"], "05:59 AM PT")
        self.assertIn("IEX", row["spot_source"])

    def test_report_text_no_longer_says_friday_close(self):
        get, _ = _fake_get(["2026-11-09"])
        with mock.patch.object(gwr.requests, "get", side_effect=get), \
                mock.patch.object(gwr.gex, "_fetch_contracts", return_value={}):
            md, blocks, fb = gwr.build_report()
        self.assertNotIn("Friday close", md)
        self.assertNotIn("Friday close", json.dumps(blocks))


class TestWrapperAcrossDst(unittest.TestCase):
    """The crontab pair `0 13,14 * * 1,2` + wrapper target 09:00 fires exactly once per day, at 06:00 PT, before
    and after the 2026-11-01 DST change; the run's logged ET offset switches from -04:00 to -05:00."""

    def _fires(self, utc_dt: datetime) -> bool:
        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return utc_dt.astimezone(tz)
        with mock.patch.object(wrapper, "datetime", _DT), \
                mock.patch.object(wrapper.os, "execvp") as ex, \
                mock.patch.object(wrapper.sys, "argv", ["w", "09:00", "/bin/true"]):
            try:
                wrapper.main()
            except SystemExit:
                return False
            return ex.called

    def test_one_fire_at_0600_pt_each_side_of_dst(self):
        for day, want_utc_hour, want_offset in (((2026, 10, 27), 13, "-04:00"), ((2026, 11, 3), 14, "-05:00")):
            fired = [h for h in (13, 14) if self._fires(_utc(*day, h))]
            self.assertEqual(fired, [want_utc_hour], day)
            fire = _utc(*day, want_utc_hour)
            self.assertEqual(fire.astimezone(PT).strftime("%H:%M"), "06:00")
            self.assertEqual(gwr._clock_record(fire)["detected_et_offset"], want_offset)
            self.assertEqual(gwr._clock_record(fire)["wrapper_target_et"], "09:00")


if __name__ == "__main__":
    unittest.main()
