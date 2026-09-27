"""data.alpaca_data.get_name_changes — pagination, 429 retry, fail-closed.

requests is mocked; no network, no real Slack/API calls.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from data import alpaca_data  # noqa: E402


def _resp(status, body=None):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = body or {}
    return r


def _page(changes, token=None):
    return {"corporate_actions": {"name_changes": changes}, "next_page_token": token}


FB = {"old_symbol": "fb", "new_symbol": "META", "process_date": "2022-06-09", "id": "x"}
FLT = {"old_symbol": "FLT", "new_symbol": "CPAY", "process_date": "2024-03-25"}


class GetNameChanges(unittest.TestCase):
    def test_paginates_and_normalises(self):
        pages = [_resp(200, _page([FB], "tok1")), _resp(200, _page([FLT]))]
        with mock.patch.object(alpaca_data.requests, "get", side_effect=pages) as g:
            out = alpaca_data.get_name_changes(
                "2016-01-01", "2026-09-27", ["FB", "FLT"]
            )
        self.assertEqual(
            out,
            [
                {
                    "old_symbol": "FB",
                    "new_symbol": "META",
                    "process_date": "2022-06-09",
                },
                {
                    "old_symbol": "FLT",
                    "new_symbol": "CPAY",
                    "process_date": "2024-03-25",
                },
            ],
        )
        self.assertEqual(g.call_count, 2)
        first, second = g.call_args_list
        self.assertEqual(first.kwargs["params"]["symbols"], "FB,FLT")
        self.assertEqual(first.kwargs["params"]["types"], "name_change")
        self.assertEqual(second.kwargs["params"]["page_token"], "tok1")

    def test_no_symbols_param_when_none(self):
        with mock.patch.object(
            alpaca_data.requests, "get", return_value=_resp(200, _page([]))
        ) as g:
            self.assertEqual(
                alpaca_data.get_name_changes("2016-01-01", "2016-12-31"), []
            )
        self.assertNotIn("symbols", g.call_args.kwargs["params"])

    def test_retries_429_then_succeeds(self):
        seq = [_resp(429), _resp(200, _page([FLT]))]
        with (
            mock.patch.object(alpaca_data.requests, "get", side_effect=seq),
            mock.patch.object(alpaca_data.time, "sleep") as sl,
        ):
            out = alpaca_data.get_name_changes("2016-01-01", "2026-09-27", ["FLT"])
        self.assertEqual(len(out), 1)
        sl.assert_called_once_with(1)

    def test_persistent_429_returns_none(self):
        with (
            mock.patch.object(alpaca_data.requests, "get", return_value=_resp(429)),
            mock.patch.object(alpaca_data.time, "sleep"),
        ):
            self.assertIsNone(alpaca_data.get_name_changes("2016-01-01", "2026-09-27"))

    def test_error_on_later_page_returns_none_not_partial(self):
        seq = [_resp(200, _page([FB], "tok1")), _resp(500)]
        with mock.patch.object(alpaca_data.requests, "get", side_effect=seq):
            self.assertIsNone(alpaca_data.get_name_changes("2016-01-01", "2026-09-27"))

    def test_exception_returns_none(self):
        with mock.patch.object(
            alpaca_data.requests, "get", side_effect=OSError("boom")
        ):
            self.assertIsNone(alpaca_data.get_name_changes("2016-01-01", "2026-09-27"))

    def test_incomplete_records_skipped(self):
        body = _page(
            [FLT, {"old_symbol": "X", "new_symbol": None, "process_date": "2020-01-01"}]
        )
        with mock.patch.object(
            alpaca_data.requests, "get", return_value=_resp(200, body)
        ):
            self.assertEqual(
                len(alpaca_data.get_name_changes("2016-01-01", "2026-09-27")), 1
            )


class PageCap(unittest.TestCase):
    def test_repeating_token_stops_and_returns_none(self):
        looping = _resp(200, _page([FLT], "same-token"))
        with (
            mock.patch.object(alpaca_data.requests, "get", return_value=looping) as g,
            mock.patch.object(alpaca_data, "_CA_MAX_PAGES", 5),
        ):
            self.assertIsNone(alpaca_data.get_name_changes("2016-01-01", "2026-09-27"))
        self.assertEqual(g.call_count, 5)


if __name__ == "__main__":
    unittest.main()
