"""E0 closed-bar gate (Confluence 2.0, 2026-09-27).

drop_forming_bars keeps only bars whose period has ended; fetch_closed_bars returns
ClosedBars; the Confluence 2.0 package may not import raw bar fetchers, and ClosedBars
is only constructed inside data/fetcher.py. alpaca-py is stubbed when absent so the
pure logic runs anywhere (OCI runs it against the real SDK).
"""

import ast
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

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
        RAW = SPLIT = SIP = IEX = None

    sys.modules["alpaca.data.historical"].StockHistoricalDataClient = object
    sys.modules["alpaca.data.requests"].StockBarsRequest = object
    sys.modules["alpaca.data.timeframe"].TimeFrame = _TF
    sys.modules["alpaca.data.timeframe"].TimeFrameUnit = _Unit
    sys.modules["alpaca.data.enums"].Adjustment = _Enum
    sys.modules["alpaca.data.enums"].DataFeed = _Enum

import config  # noqa: E402
from data import fetcher  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _frame(starts):
    idx = pd.DatetimeIndex([s.astimezone(UTC) for s in starts])
    n = len(starts)
    return pd.DataFrame(
        {
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [float(i) for i in range(n)],
            "volume": [100] * n,
        },
        index=idx,
    )


def _captured(df, at):
    df.attrs["fetched_at"] = at
    return df


def _et(*a):
    return datetime(*a, tzinfo=ET)


class DropFormingBars(unittest.TestCase):
    def test_intraday_15m_forming_dropped_at_boundary(self):
        df = _frame([_et(2026, 9, 25, 10, 30), _et(2026, 9, 25, 10, 45)])
        # 10:45 bar ends 11:00: forming at 10:59:59, closed at exactly 11:00
        self.assertEqual(
            len(
                fetcher.drop_forming_bars(
                    df, config.TF_15M, _et(2026, 9, 25, 10, 59, 59)
                )
            ),
            1,
        )
        self.assertEqual(
            len(fetcher.drop_forming_bars(df, config.TF_15M, _et(2026, 9, 25, 11, 0))),
            2,
        )

    def test_native_4h_anchor(self):
        df = _frame([_et(2026, 9, 25, 8), _et(2026, 9, 25, 12)])
        out = fetcher.drop_forming_bars(df, config.TF_4H, _et(2026, 9, 25, 13, 30))
        self.assertEqual(list(out["close"]), [0.0])

    def test_daily_forming_until_1600(self):
        df = _frame([_et(2026, 9, 24), _et(2026, 9, 25)])
        self.assertEqual(
            len(
                fetcher.drop_forming_bars(df, config.TF_DAILY, _et(2026, 9, 25, 15, 59))
            ),
            1,
        )
        self.assertEqual(
            len(
                fetcher.drop_forming_bars(df, config.TF_DAILY, _et(2026, 9, 25, 16, 0))
            ),
            2,
        )

    def test_daily_half_day_withheld_until_1600_never_admitted_early(self):
        df = _frame([_et(2025, 11, 28)])
        self.assertTrue(
            fetcher.drop_forming_bars(
                df, config.TF_DAILY, _et(2025, 11, 28, 14, 0)
            ).empty
        )
        self.assertEqual(
            len(
                fetcher.drop_forming_bars(df, config.TF_DAILY, _et(2025, 11, 28, 16, 0))
            ),
            1,
        )

    def test_weekly_closed_on_weekend_forming_midweek(self):
        df = _frame([_et(2026, 9, 14), _et(2026, 9, 21)])
        # Sunday after the week: the Sep 21 week is complete and must be KEPT
        self.assertEqual(
            len(fetcher.drop_forming_bars(df, config.TF_WEEKLY, _et(2026, 9, 27, 12))),
            2,
        )
        # Wednesday of that week: forming
        self.assertEqual(
            len(fetcher.drop_forming_bars(df, config.TF_WEEKLY, _et(2026, 9, 23, 12))),
            1,
        )

    def test_monthly_september_forming_on_sep27_august_closed(self):
        df = _frame([_et(2026, 8, 1), _et(2026, 9, 1)])
        out = fetcher.drop_forming_bars(df, config.TF_MONTHLY, _et(2026, 9, 27, 12))
        self.assertEqual(list(out["close"]), [0.0])
        # Sep 30 2026 is a Wednesday: closed at 16:00 that day
        self.assertEqual(
            len(fetcher.drop_forming_bars(df, config.TF_MONTHLY, _et(2026, 9, 30, 16))),
            2,
        )

    def test_monthly_ending_on_weekend_uses_last_weekday(self):
        df = _frame(
            [_et(2026, 5, 1)]
        )  # May 31 2026 is a Sunday -> last weekday Fri May 29
        self.assertEqual(
            len(fetcher.drop_forming_bars(df, config.TF_MONTHLY, _et(2026, 5, 29, 16))),
            1,
        )
        self.assertTrue(
            fetcher.drop_forming_bars(df, config.TF_MONTHLY, _et(2026, 5, 29, 15)).empty
        )

    def test_fail_closed_inputs(self):
        df = _frame([_et(2026, 9, 24)])
        self.assertTrue(fetcher.drop_forming_bars(df, "3Day", _et(2026, 9, 27)).empty)
        self.assertTrue(
            fetcher.drop_forming_bars(df, config.TF_DAILY, datetime(2026, 9, 27)).empty
        )
        naive = df.copy()
        naive.index = naive.index.tz_localize(None)
        self.assertTrue(
            fetcher.drop_forming_bars(naive, config.TF_DAILY, _et(2026, 9, 27)).empty
        )
        self.assertTrue(
            fetcher.drop_forming_bars(
                pd.DataFrame(), config.TF_DAILY, _et(2026, 9, 27)
            ).empty
        )
        self.assertTrue(
            fetcher.drop_forming_bars(None, config.TF_DAILY, _et(2026, 9, 27)).empty
        )


class FetchClosedBars(unittest.TestCase):
    def test_fetches_one_extra_and_returns_n_closed(self):
        starts = [_et(2026, 9, d) for d in (21, 22, 23, 24, 25)]
        raw = _captured(_frame(starts), _et(2026, 9, 25, 12))
        with mock.patch.object(fetcher, "fetch_bars", return_value=raw) as fb:
            cb = fetcher.fetch_closed_bars(
                "SPY", config.TF_DAILY, num_bars=3, now=_et(2026, 9, 25, 12)
            )
        fb.assert_called_once_with("SPY", config.TF_DAILY, num_bars=4)
        self.assertIsInstance(cb, fetcher.ClosedBars)
        self.assertEqual(list(cb.df["close"]), [1.0, 2.0, 3.0])  # Sep 25 still forming
        self.assertEqual((cb.symbol, cb.timeframe), ("SPY", config.TF_DAILY))

    def test_fetch_error_gives_empty(self):
        with mock.patch.object(fetcher, "fetch_bars", return_value=pd.DataFrame()):
            cb = fetcher.fetch_closed_bars(
                "SPY", config.TF_DAILY, num_bars=3, now=_et(2026, 9, 25, 12)
            )
        self.assertTrue(cb.df.empty)

    def test_bar_captured_while_forming_is_excluded_later(self):
        # Frame captured at 15:59 (Sep 25 daily bar still forming, partial prices),
        # served again from the TTL cache at 16:01: the bar's period has ended, but the
        # data predates the close, so it must stay excluded.
        raw = _captured(
            _frame([_et(2026, 9, 24), _et(2026, 9, 25)]), _et(2026, 9, 25, 15, 59)
        )
        with mock.patch.object(fetcher, "fetch_bars", return_value=raw):
            cb = fetcher.fetch_closed_bars(
                "SPY", config.TF_DAILY, num_bars=5, now=_et(2026, 9, 25, 16, 1)
            )
        self.assertEqual(list(cb.df["close"]), [0.0])
        self.assertEqual(cb.as_of, _et(2026, 9, 25, 15, 59))

    def test_missing_capture_time_fails_closed(self):
        raw = _frame([_et(2026, 9, 24)])
        with mock.patch.object(fetcher, "fetch_bars", return_value=raw):
            cb = fetcher.fetch_closed_bars(
                "SPY", config.TF_DAILY, num_bars=5, now=_et(2026, 9, 26)
            )
        self.assertTrue(cb.df.empty)

    def test_fetch_bars_stamps_capture_time_and_cache_hit_keeps_it(self):
        bars = mock.Mock()
        bars.df = _frame([_et(2026, 9, 24), _et(2026, 9, 25)])
        client = mock.Mock()
        client.get_stock_bars.return_value = bars
        with (
            mock.patch.object(fetcher, "get_client", return_value=client),
            mock.patch.object(fetcher, "_rate_gate"),
            mock.patch.object(fetcher, "StockBarsRequest"),
            mock.patch.object(config, "ALPACA_BAR_CACHE_TTL_SECS", 180),
        ):
            fetcher._bar_cache.clear()
            first = fetcher.fetch_bars("ZZTEST", config.TF_DAILY, num_bars=2)
            second = fetcher.fetch_bars("ZZTEST", config.TF_DAILY, num_bars=2)
            fetcher._bar_cache.clear()
        self.assertEqual(client.get_stock_bars.call_count, 1)  # second call = cache hit
        self.assertIsInstance(first.attrs.get("fetched_at"), datetime)
        self.assertEqual(second.attrs.get("fetched_at"), first.attrs["fetched_at"])


class ClosedBarsSelfValidates(unittest.TestCase):
    def _cb(self):
        df = _captured(
            _frame([_et(2026, 9, 24), _et(2026, 9, 25)]), _et(2026, 9, 25, 17)
        )
        with mock.patch.object(fetcher, "fetch_bars", return_value=df):
            return fetcher.fetch_closed_bars(
                "SPY", config.TF_DAILY, num_bars=5, now=_et(2026, 9, 25, 17)
            )

    def test_replace_with_forming_bars_raises(self):
        import dataclasses

        cb = self._cb()
        self.assertEqual(len(cb.df), 2)
        with self.assertRaises(ValueError):
            dataclasses.replace(cb, as_of=_et(2026, 9, 25, 12))  # Sep 25 now forming
        forming = _frame([_et(2026, 9, 28)])
        with self.assertRaises(ValueError):
            dataclasses.replace(cb, _frame=forming)

    def test_mutating_returned_frame_cannot_change_stored_bars(self):
        cb = self._cb()
        leaked = cb.df
        leaked.loc[pd.Timestamp("2099-01-01", tz="UTC")] = [1, 1, 1, 1, 100]
        leaked["close"] = -1.0
        self.assertEqual(len(cb.df), 2)
        self.assertEqual(list(cb.df["close"]), [0.0, 1.0])
        src = _frame([_et(2026, 9, 24)])
        built = fetcher.ClosedBars("SPY", config.TF_DAILY, _et(2026, 9, 26), src)
        src.loc[pd.Timestamp("2099-01-01", tz="UTC")] = [1, 1, 1, 1, 100]
        self.assertEqual(len(built.df), 1)  # constructor copied the caller's frame

    def test_direct_construction_is_validated(self):
        forming = _frame([_et(2026, 9, 25)])
        with self.assertRaises(ValueError):
            fetcher.ClosedBars("SPY", config.TF_DAILY, _et(2026, 9, 25, 12), forming)
        with self.assertRaises(ValueError):
            fetcher.ClosedBars("SPY", config.TF_DAILY, datetime(2026, 9, 25), forming)
        with self.assertRaises(ValueError):
            fetcher.ClosedBars("SPY", "3Day", _et(2026, 9, 26), forming)
        ok = fetcher.ClosedBars("SPY", config.TF_DAILY, _et(2026, 9, 26), forming)
        self.assertEqual(len(ok.df), 1)
        empty = fetcher.ClosedBars(
            "SPY", config.TF_DAILY, _et(2026, 9, 26), pd.DataFrame()
        )
        self.assertTrue(empty.df.empty)


_BANNED = {
    "fetch_bars",
    "fetch_multi_timeframe",
    "fetch_bars_window",
    "get_client",
    "DataFetcher",
}


def _raw_fetch_violations(tree):
    """Raw-fetcher use in a Confluence 2.0 module: banned from-imports (aliases too,
    since a.name is the original name), `import ...fetcher`, `.fetch_bars` attribute
    access, and string look-ups such as getattr(fetcher, "fetch_bars")."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("fetcher"):
            bad = {a.name for a in node.names} & (_BANNED | {"*"})
            if bad:
                out.append(f"imports {sorted(bad)}")
        elif isinstance(node, ast.Import):
            out += [f"import {a.name}" for a in node.names if "fetcher" in a.name]
        elif isinstance(node, ast.Attribute) and node.attr in _BANNED:
            out.append(f"attribute .{node.attr}")
        elif isinstance(node, ast.Constant) and node.value in _BANNED:
            out.append(f"string {node.value!r}")
    return out


def _closedbars_violations(tree):
    """Direct ClosedBars construction outside data/fetcher.py, including under an
    import alias (`from data.fetcher import ClosedBars as CB; CB(...)`), via an
    attribute (`fetcher.ClosedBars(...)`), or a string look-up
    (getattr(fetcher, "ClosedBars")). A lint guard against accidental misuse, not a
    security boundary. The hard guarantee is ClosedBars.__post_init__, which
    re-validates every construction (including dataclasses.replace)."""
    names = {"ClosedBars"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "ClosedBars" and a.asname:
                    names.add(a.asname)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id in names) or (
                isinstance(f, ast.Attribute) and f.attr == "ClosedBars"
            ):
                out.append("constructs ClosedBars")
        elif isinstance(node, ast.Constant) and node.value == "ClosedBars":
            out.append("string 'ClosedBars'")
    return out


class Enforcement(unittest.TestCase):
    def test_detectors_catch_known_bypasses(self):
        raw_cases = [
            "from data.fetcher import fetch_bars",
            "from data.fetcher import fetch_bars as fb",
            "from data.fetcher import *",
            "import data.fetcher as f",
            "from data import fetcher\nfetcher.fetch_bars('X', '1Day')",
            "from data import fetcher\ngetattr(fetcher, 'fetch_bars')('X', '1Day')",
        ]
        for src in raw_cases:
            self.assertTrue(_raw_fetch_violations(ast.parse(src)), src)
        ok = "from data.fetcher import fetch_closed_bars, ClosedBars"
        self.assertFalse(_raw_fetch_violations(ast.parse(ok)))
        cb_cases = [
            "ClosedBars('X', '1Day', None, None)",
            "from data.fetcher import ClosedBars as CB\nCB('X', '1Day', None, None)",
            "from data import fetcher\nfetcher.ClosedBars('X', '1Day', None, None)",
            "from data import fetcher\ngetattr(fetcher, 'ClosedBars')('X')",
        ]
        for src in cb_cases:
            self.assertTrue(_closedbars_violations(ast.parse(src)), src)
        typed = "from data.fetcher import ClosedBars\ndef f(b: ClosedBars): return b.df"
        self.assertFalse(_closedbars_violations(ast.parse(typed)))

    def test_confluence2_package_never_imports_raw_fetchers(self):
        pkg = _ROOT / "strategy" / "confluence2"
        self.assertTrue(pkg.is_dir())
        for path in pkg.rglob("*.py"):
            bad = _raw_fetch_violations(ast.parse(path.read_text()))
            self.assertFalse(bad, f"{path}: {bad}")

    def test_closedbars_constructed_only_in_fetcher(self):
        allowed = {Path("data") / "fetcher.py"}
        scanned = 0
        for path in _ROOT.rglob("*.py"):
            rel = path.relative_to(
                _ROOT
            )  # repo-relative: the repo may sit under .claude/
            if rel in allowed or rel.parts[0] in {"tests", ".claude", "venv", ".venv"}:
                continue
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            scanned += 1
            bad = _closedbars_violations(tree)
            self.assertFalse(bad, f"{rel}: {bad}")
        self.assertGreater(scanned, 50, "scan covered too few files to be meaningful")


if __name__ == "__main__":
    unittest.main()
