"""Point-in-time universe builder (research/pit_universe.py) — pure logic only.

No network: table parsing, primary-source classification, document matching,
date-aware rename resolution, and the backward membership walk.
"""

import sys
import unittest
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    import lxml.html  # noqa: F401

    HAVE_LXML = True
except ImportError:
    HAVE_LXML = False

from research import pit_universe as pu  # noqa: E402

_HTML = """
<html><body>
<table class="wikitable">
<tr><th rowspan="2">Effective Date</th><th colspan="2">Added</th>
    <th colspan="2">Removed</th><th rowspan="2">Reason</th>
    <th rowspan="2">Refs</th></tr>
<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>
<tr><td rowspan="2">March 22, 2021</td><td>NXPI</td><td>NXP Semiconductors</td>
    <td>FLS</td><td>Flowserve</td><td rowspan="2">Market cap.</td>
    <td><sup><a href="#cite_note-a-1">[1]</a></sup></td></tr>
<tr><td>PENN</td><td>Penn National Gaming</td><td>SLG</td><td>SL Green</td>
    <td><sup><a href="#cite_note-a-1">[1]</a></sup></td></tr>
<tr><td>June 9, 2022</td><td></td><td></td><td>XYZ</td><td>Xyz Corp</td>
    <td>Acquired.</td><td></td></tr>
</table>
<ol class="references">
<li id="cite_note-a-1"><a href="https://www.spglobal.com/spdji/en/documents/x.pdf">S&amp;P</a>
    <a href="https://www.reuters.com/y">Reuters</a></li>
</ol>
</body></html>
"""


def _ch(eff, added="", removed="", **kw):
    return pu.Change(
        index="sp500",
        effective=eff,
        added=added,
        added_name="",
        removed=removed,
        removed_name="",
        reason="",
        **kw,
    )


@unittest.skipUnless(HAVE_LXML, "lxml not installed (lab dependency)")
class ParseChanges(unittest.TestCase):
    def test_rowspans_expanded_and_refs_mapped(self):
        changes, refs = pu.parse_changes(_HTML, "sp500")
        self.assertEqual(len(changes), 3)
        a, b, c = changes
        self.assertEqual(
            (a.effective, a.added, a.removed), (date(2021, 3, 22), "NXPI", "FLS")
        )
        # second row inherits the rowspanned date and reason
        self.assertEqual(
            (b.effective, b.added, b.removed, b.reason),
            (date(2021, 3, 22), "PENN", "SLG", "Market cap."),
        )
        self.assertEqual((c.added, c.removed), ("", "XYZ"))
        self.assertEqual(a.ref_ids, ["cite_note-a-1"])
        self.assertEqual(len(refs["cite_note-a-1"]), 2)


class PrimaryClassification(unittest.TestCase):
    def test_primary_domains(self):
        urls = [
            "https://www.reuters.com/y",
            "https://www.spglobal.com/spdji/en/documents/x.pdf",
            "https://press.spglobal.com/2026-08-13-Reddit-Set-to-Join",
        ]
        self.assertEqual(pu.primary_urls(urls, "sp500"), urls[1:])
        self.assertEqual(
            pu.primary_urls(["https://www.globenewswire.com/n"], "ndx"),
            ["https://www.globenewswire.com/n"],
        )
        self.assertEqual(
            pu.primary_urls(["https://www.globenewswire.com/n"], "sp500"), []
        )

    def test_wayback_raw_url(self):
        self.assertEqual(
            pu.wayback_raw("https://a.com/x.pdf", "20260908234923"),
            "https://web.archive.org/web/20260908234923id_/https://a.com/x.pdf",
        )


class DocumentMatching(unittest.TestCase):
    TEXT = (
        "NXP Semiconductors NV (NASD:NXPI) will replace Flowserve Corp. "
        "(NYSE:FLS) in the S&P 500"
    )

    def test_ticker_token_and_name(self):
        self.assertTrue(pu.side_found(self.TEXT, "NXPI", "NXP Semiconductors"))
        self.assertTrue(pu.side_found(self.TEXT, "ZZZZ", "Flowserve Corporation"))
        self.assertFalse(pu.side_found(self.TEXT, "QQQQ", "Unrelated Holdings"))
        self.assertTrue(pu.side_found(self.TEXT, "", ""))

    def test_ticker_not_matched_inside_longer_token(self):
        self.assertFalse(pu.side_found("(NASD:NXPI)", "XPI", ""))

    def test_verify_requires_both_sides(self):
        ok = _ch(date(2021, 3, 22), "NXPI", "FLS")
        bad = _ch(date(2021, 3, 22), "NXPI", "AAPL")
        self.assertTrue(pu.verify_change(ok, self.TEXT))
        self.assertFalse(pu.verify_change(bad, self.TEXT))


RENAMES = [
    {"old_symbol": "FB", "new_symbol": "META", "process_date": "2022-06-09"},
    {"old_symbol": "META", "new_symbol": "METV", "process_date": "2022-01-31"},
    {"old_symbol": "FISV", "new_symbol": "FI", "process_date": "2023-06-07"},
    {"old_symbol": "FI", "new_symbol": "FISV", "process_date": "2025-11-11"},
    {"old_symbol": "FBHS", "new_symbol": "FBIN", "process_date": "2022-12-15"},
]


class ResolveCurrent(unittest.TestCase):
    def test_reused_ticker_is_date_aware(self):
        self.assertEqual(pu.resolve_current("FB", date(2017, 1, 3), RENAMES), "META")
        # the 2021 "META" (Metaverse ETF) became METV; Meta Platforms' META did not
        self.assertEqual(pu.resolve_current("META", date(2021, 6, 1), RENAMES), "METV")
        self.assertEqual(pu.resolve_current("META", date(2023, 1, 3), RENAMES), "META")

    def test_chain_and_round_trip(self):
        self.assertEqual(pu.resolve_current("FISV", date(2020, 1, 2), RENAMES), "FISV")
        self.assertEqual(pu.resolve_current("FI", date(2024, 1, 2), RENAMES), "FISV")

    def test_grace_window_for_stale_ticker(self):
        self.assertEqual(
            pu.resolve_current("FBHS", date(2022, 12, 19), RENAMES), "FBIN"
        )
        self.assertEqual(pu.resolve_current("FBHS", date(2023, 3, 1), RENAMES), "FBHS")

    def test_unknown_ticker_unchanged(self):
        self.assertEqual(pu.resolve_current("AAPL", date(2020, 1, 2), RENAMES), "AAPL")


class WalkMembership(unittest.TestCase):
    def test_backward_walk_intervals(self):
        current = {"AAA", "NEW"}
        changes = [
            _ch(date(2024, 1, 2), added="NEW", removed="OLD"),
            _ch(date(2020, 1, 2), added="OLD", removed="GONE"),
        ]
        iv, anomalies = pu.walk_membership(current, changes, date(2016, 1, 1))
        self.assertEqual(anomalies, [])
        self.assertEqual(iv["NEW"], [(date(2024, 1, 2), None)])
        self.assertEqual(iv["OLD"], [(date(2020, 1, 2), date(2024, 1, 2))])
        self.assertEqual(iv["GONE"], [(None, date(2020, 1, 2))])
        self.assertEqual(iv["AAA"], [(None, None)])

    def test_changes_before_since_ignored(self):
        iv, _ = pu.walk_membership(
            {"A"}, [_ch(date(2015, 6, 1), added="A", removed="Z")], date(2016, 1, 1)
        )
        self.assertEqual(iv, {"A": [(None, None)]})

    def test_contradictions_flagged(self):
        changes = [_ch(date(2024, 1, 2), added="NOTMEMBER", removed="STILLHERE")]
        _, anomalies = pu.walk_membership({"STILLHERE"}, changes, date(2016, 1, 1))
        self.assertEqual(len(anomalies), 2)

    def test_rename_row_is_not_a_membership_change(self):
        ch = _ch(date(2024, 2, 1), added="DAY", removed="CDAY")
        ch.added_cur, ch.removed_cur = "DAY", "DAY"
        iv, anomalies = pu.walk_membership({"DAY"}, [ch], date(2016, 1, 1))
        self.assertEqual(anomalies, [])
        self.assertEqual(iv["DAY"], [(None, None)])

    def test_uses_resolved_tickers(self):
        ch = _ch(date(2017, 6, 19), added="RE", removed="MJN")
        ch.added_cur, ch.removed_cur = "EG", "MJN"
        iv, anomalies = pu.walk_membership({"EG"}, [ch], date(2016, 1, 1))
        self.assertEqual(anomalies, [])
        self.assertEqual(iv["EG"], [(date(2017, 6, 19), None)])
        self.assertEqual(iv["MJN"], [(None, date(2017, 6, 19))])


class SameDayBatch(unittest.TestCase):
    def test_same_day_chain_any_row_order(self):
        # Z replaced by Y, and Y replaced by X, both effective the same day
        a = _ch(date(2024, 3, 18), added="Y", removed="Z")
        b = _ch(date(2024, 3, 18), added="X", removed="Y")
        for order in ([a, b], [b, a]):
            iv, anomalies = pu.walk_membership({"X"}, order, date(2016, 1, 1))
            self.assertEqual(anomalies, [], order)
            self.assertEqual(iv["X"], [(date(2024, 3, 18), None)])
            self.assertEqual(iv["Z"], [(None, date(2024, 3, 18))])
            self.assertNotIn("Y", iv)


class RenameReason(unittest.TestCase):
    def test_reason_regex(self):
        self.assertTrue(
            pu._RENAME_REASON.search(
                "Ceridian changed its ticker symbol from CDAY to DAY."
            )
        )
        self.assertTrue(pu._RENAME_REASON.search("Ticker symbol change."))
        self.assertFalse(
            pu._RENAME_REASON.search(
                "Willis Holdings merged with Towers Watson and was renamed Willis "
                "Towers Watson."
            )
        )
        self.assertFalse(pu._RENAME_REASON.search("Market capitalization changes."))


class StrictMatching(unittest.TestCase):
    def test_one_letter_tickers_need_exchange_form(self):
        text = "A replacement was announced. AT&T Inc. (NYSE: T) will replace ..."
        self.assertTrue(pu.side_found(text, "T", ""))
        self.assertFalse(pu.side_found(text, "A", ""))
        self.assertFalse(pu.side_found("the IT sector", "IT", ""))
        self.assertTrue(pu.side_found("Gartner Inc. (NYSE:IT)", "IT", ""))

    def test_share_class_not_matched_by_base(self):
        self.assertFalse(pu.side_found("(NYSE: BRK.B)", "BRK", ""))
        self.assertTrue(pu.side_found("(NYSE: BRK.B)", "BRK.B", ""))

    def test_common_first_word_needs_second_word(self):
        self.assertEqual(pu._name_key("American Water Works"), "american water")
        self.assertFalse(
            pu.side_found("American Express will replace", "", "American Water Works")
        )
        self.assertTrue(
            pu.side_found("American  Water Works Co", "", "American Water Works")
        )


class PressMatching(unittest.TestCase):
    MULTI = (
        "S&P MidCap 400 constituent Acme Inc. (NYSE: ACM) will replace Widget Co. "
        "(NYSE: WDG) in the S&P 500. " + "x" * 600 + " In the S&P MidCap 400, Zimmer "
        "Biomet (NYSE: ZBH) will move to the S&P SmallCap 600 and Abiomed (NASD: ABMD) "
        "will move up to the S&P MidCap 400."
    )

    def test_press_verifies_the_s_and_p_500_pair(self):
        self.assertTrue(
            pu.verify_press_change(_ch(date(2024, 3, 18), "ACM", "WDG"), self.MULTI)
        )

    def test_unrelated_pair_elsewhere_in_release_rejected(self):
        ch = _ch(date(2024, 3, 18), "ZBH", "ABMD")
        self.assertTrue(pu.verify_change(ch, self.MULTI))  # the loose check would pass
        self.assertFalse(pu.verify_press_change(ch, self.MULTI))

    def test_one_sided_row_needs_s_and_p_500_nearby(self):
        self.assertTrue(
            pu.verify_press_change(_ch(date(2024, 3, 18), "ACM", ""), self.MULTI)
        )
        self.assertFalse(
            pu.verify_press_change(_ch(date(2024, 3, 18), "ZBH", ""), self.MULTI)
        )

    def test_press_candidates_window_nearest_first(self):
        rel = [
            (date(2024, 2, 1), "old", "u_old"),
            (date(2024, 3, 1), "a", "u_a"),
            (date(2024, 3, 8), "b", "u_b"),
            (date(2024, 3, 19), "after", "u_after"),
        ]
        self.assertEqual(pu.press_candidates(rel, date(2024, 3, 18)), ["u_b", "u_a"])

    def test_ten_years_before(self):
        self.assertEqual(pu.ten_years_before(date(2026, 9, 27)), date(2016, 9, 27))
        self.assertEqual(pu.ten_years_before(date(2028, 2, 29)), date(2018, 2, 28))


if __name__ == "__main__":
    unittest.main()
