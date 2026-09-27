"""Lab bar history: fetch_bars_window's optional `asof`, and research/lab_bars.py
job planning. No network: the Alpaca client and request class are mocked."""

import sys
import types
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    import alpaca  # noqa: F401
except ImportError:  # stub only the names data/fetcher.py imports
    for _name in (
        "alpaca",
        "alpaca.data",
        "alpaca.data.historical",
        "alpaca.data.requests",
        "alpaca.data.timeframe",
        "alpaca.data.enums",
    ):
        sys.modules.setdefault(_name, types.ModuleType(_name))

    class _TF:
        def __init__(self, *a):
            self.a = a

    class _Unit:
        Minute, Hour, Day, Week, Month = "Minute", "Hour", "Day", "Week", "Month"

    class _Enum:
        RAW = SPLIT = SIP = IEX = object()

    sys.modules["alpaca.data.historical"].StockHistoricalDataClient = object
    sys.modules["alpaca.data.requests"].StockBarsRequest = object
    sys.modules["alpaca.data.timeframe"].TimeFrame = _TF
    sys.modules["alpaca.data.timeframe"].TimeFrameUnit = _Unit
    sys.modules["alpaca.data.enums"].Adjustment = _Enum
    sys.modules["alpaca.data.enums"].DataFeed = _Enum

import config  # noqa: E402
from data import fetcher  # noqa: E402
from research import lab_bars as lb  # noqa: E402

S = datetime(2020, 1, 1, tzinfo=timezone.utc)
E = datetime(2020, 2, 1, tzinfo=timezone.utc)


class FetchWindowAsof(unittest.TestCase):
    def _run(self, **kw):
        idx = pd.DatetimeIndex([datetime(2020, 1, 2, 5, tzinfo=timezone.utc)])
        bars = mock.Mock()
        bars.df = pd.DataFrame(
            {
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [10],
            },
            index=idx,
        )
        client = mock.Mock()
        client.get_stock_bars.return_value = bars
        req = mock.Mock(return_value="REQ")
        with (
            mock.patch.object(fetcher, "get_client", return_value=client),
            mock.patch.object(fetcher, "_rate_gate"),
            mock.patch.object(fetcher, "StockBarsRequest", req),
        ):
            df = fetcher.fetch_bars_window("PCLN", config.TF_DAILY, S, E, **kw)
        return df, req

    def test_default_sends_no_asof(self):
        df, req = self._run()
        self.assertEqual(len(df), 1)
        self.assertNotIn("asof", req.call_args.kwargs)

    def test_asof_passed_through(self):
        df, req = self._run(asof="2017-12-01")
        self.assertEqual(len(df), 1)
        self.assertEqual(req.call_args.kwargs["asof"], "2017-12-01")

    def test_malformed_asof_is_empty_and_never_requests(self):
        for bad in ("2017-13-01", "12/01/2017", "2017-1-1", "yesterday"):
            df, req = self._run(asof=bad)
            self.assertTrue(df.empty, bad)
            req.assert_not_called()


class JobPlanning(unittest.TestCase):
    TODAY = date(2026, 9, 27)
    W = lb.timedelta(days=lb.WARMUP_DAYS)

    def test_current_member_has_no_asof(self):
        jobs = lb.jobs_from_membership(
            [{"ticker": "AAPL", "start": "<=2016-09-27", "end": ""}], self.TODAY
        )
        j = jobs["AAPL"]
        self.assertIsNone(j.asof)
        self.assertEqual(j.end, self.TODAY)
        self.assertEqual(j.start, date(2016, 9, 27) - self.W)

    def test_departed_member_asof_is_its_end(self):
        rows = [{"ticker": "TWTR", "start": "<=2016-09-27", "end": "2022-11-01"}]
        jobs = lb.jobs_from_membership(rows, self.TODAY)
        j = jobs["TWTR@2022-11-01"]
        self.assertEqual((j.end, j.asof), (date(2022, 11, 1), "2022-11-01"))

    def test_reused_ticker_gets_one_job_per_interval(self):
        # company A held XYZ until 2018; company B later reused XYZ and is a member now
        rows = [
            {"ticker": "XYZ", "start": "<=2016-09-27", "end": "2018-06-01"},
            {"ticker": "XYZ", "start": "2021-01-04", "end": ""},
        ]
        jobs = lb.jobs_from_membership(rows, self.TODAY)
        self.assertEqual(sorted(jobs), ["XYZ", "XYZ@2018-06-01"])
        old, new = jobs["XYZ@2018-06-01"], jobs["XYZ"]
        self.assertEqual((old.asof, old.end), ("2018-06-01", date(2018, 6, 1)))
        self.assertEqual((new.asof, new.start), (None, date(2021, 1, 4) - self.W))

    def test_context_etfs_added_without_duplicates(self):
        jobs = lb.jobs_from_membership(
            [{"ticker": "SPY", "start": "2020-01-02", "end": ""}], self.TODAY
        )
        jobs = lb.add_context(jobs, date(2016, 9, 27), self.TODAY)
        self.assertIn("TLT", jobs)
        self.assertEqual(jobs["SPY"].start, date(2020, 1, 2) - self.W)

    def test_fetch_window_caps_at_today(self):
        j = lb.Job("A", date(2016, 1, 1), self.TODAY, None)
        s, e = lb.fetch_window(j, self.TODAY)
        self.assertEqual(e, datetime(2026, 9, 27, tzinfo=timezone.utc))
        j2 = lb.Job("B", date(2016, 1, 1), date(2022, 11, 1), "2022-11-01")
        self.assertEqual(
            lb.fetch_window(j2, self.TODAY)[1],
            datetime(2022, 11, 2, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
