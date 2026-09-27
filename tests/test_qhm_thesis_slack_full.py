"""The QHM thesis Slack post must carry the FULL report: nothing trimmed, and no
pointer to a server file the reader cannot open (Rafael 2026-09-27, repeated ask).
This test is the gate."""

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import qhm_thesis as qt  # noqa: E402


def _long_board(n_picks=14):
    parts = ["## EXECUTIVE SUMMARY"]
    for k in range(3):
        parts.append(f"- **PICK{k}** – thesis sentence number {k} " + "detail " * 40)
    parts += [
        "",
        "## PER-CANDIDATE",
        "| Ticker | Sector | Thesis | Key risk |",
        "|---|---|---|---|",
    ]
    for k in range(n_picks):
        parts.append(
            f"| **SYM{k}** | Sector{k} | "
            + f"thesis{k} " * 90
            + f"| risk{k} "
            + "r " * 30
            + "|"
        )
    parts += [
        "",
        "## BOARD VOTE",
        "| Seat | Assessment |",
        "|---|---|",
        "| **AB** | " + "kelly " * 400 + "|",
        "",
        "## CURRENT HOLDS",
        "- **GEV** HOLD " + "note " * 300,
    ]
    return "\n".join(parts)


def _memo():
    ranked = [
        {
            "sym": f"C{k}",
            "sector": "S",
            "earn": "2026-10-20",
            "grade": "Buy",
            "dr_count": 3,
            "dr_holders": ["Fund A", "Fund B"],
            "m": {"last": 10.0 + k, "vs50": -5.0, "off20h": -9.0, "rsi": 40},
        }
        for k in range(9)
    ]
    holds = [
        {
            "sym": "GEV",
            "pct_eq": "38",
            "over_cap": True,
            "dip": "dip -2.6% off high, RSI 52",
        },
        {"sym": "GE", "pct_eq": "?", "over_cap": False, "dip": "dip -5.3% off high"},
    ]
    long = _long_board()
    return qt.build_memo("2026-09-27", ranked, holds, 2541.0, long, long), ranked, holds


def _first_col_labels(memo: str):
    """Counter of the words in each table's first header cell (e.g. 'Sym', 'Ticker').
    Records lead with the ticker itself, so exactly these occurrences are intentionally
    not repeated (Rafael); any OTHER occurrence of the same word is still checked."""
    from collections import Counter

    out: Counter = Counter()
    lines = memo.splitlines()
    for i, ln in enumerate(lines[:-1]):
        if ln.strip().startswith("|") and qt._is_table_separator(lines[i + 1]):
            first = ln.strip().strip("|").split("|")[0]
            out.update(_words(first))
    return out


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9.%$+\-]*", text)


class FullReportInSlack(unittest.TestCase):
    def setUp(self):
        self.memo, ranked, holds = _memo()
        self.blocks, self.fallback = qt.build_slack(
            "2026-09-27", ranked, holds, 2541.0, True, True, self.memo
        )
        self.text = "\n".join(
            b["text"]["text"]
            if b.get("type") in ("section", "header")
            else " ".join(e["text"] for e in b.get("elements", []))
            for b in self.blocks
            if b.get("type") != "divider"
        )

    def test_memo_is_long_enough_to_have_been_truncated_before(self):
        self.assertGreater(len(self.memo), 30000)

    def test_every_memo_word_reaches_slack(self):
        # Tables become one record per row (column labels repeat per record), so compare
        # counts, not raw order: every memo word appears in Slack at least as often.
        from collections import Counter

        have = Counter(_words(self.text))
        need = Counter(_words(self.memo)) - _first_col_labels(self.memo)
        for w, n in need.items():
            self.assertGreaterEqual(have[w], n, f"memo word missing from Slack: {w!r}")

    def test_sections_arrive_in_memo_order(self):
        heads = [m.strip() for m in re.findall(r"^#{2,6}\s+(.*)$", self.memo, re.M)]
        pos = 0
        for h in heads:
            key = re.sub(r"\*", "", h)
            found = self.text.find(key, pos)
            self.assertGreaterEqual(found, 0, f"section missing or out of order: {key}")
            pos = found + 1

    def test_no_block_over_slack_limit(self):
        for b in self.blocks:
            if b.get("type") == "section":
                self.assertLessEqual(len(b["text"]["text"]), 3000)

    def test_no_reference_to_files_the_reader_cannot_open(self):
        low = self.text.lower()
        banned = (
            "logs/",
            ".md",
            "full memo",
            "in the memo",
            "see memo",
            "trimmed for slack",
        )
        for bad in banned:
            self.assertNotIn(bad, low, bad)

    def test_slack_syntax_not_raw_markdown(self):
        for b in self.blocks:
            if b.get("type") == "section":
                t = b["text"]["text"]
                self.assertNotIn("**", t)
                self.assertFalse(re.search(r"^\s*#{1,6}\s", t, re.M), t[:80])
                self.assertFalse(re.search(r"^\s*\|.*\|\s*$", t, re.M), t[:80])


class NoLabelBeforeTicker(unittest.TestCase):
    def test_record_leads_with_bold_ticker_only(self):
        memo = "| Sym | Sector |\n|---|---|\n| NFLX | Media |"
        lines = qt._memo_lines_for_slack(memo)
        self.assertIn("*NFLX*", lines)
        self.assertFalse(any(ln.startswith("Sym") for ln in lines), lines)
        self.assertIn("Sector: Media", lines)


class LabelExemptionIsNarrow(unittest.TestCase):
    def test_label_word_elsewhere_is_still_checked(self):
        from collections import Counter

        memo = "Ticker notes matter.\n| Ticker | Flag |\n|---|---|\n| GEV | hold |"
        need = Counter(_words(memo)) - _first_col_labels(memo)
        self.assertEqual(
            need["Ticker"], 1
        )  # the prose occurrence must still reach Slack
        text = "\n".join(qt._memo_lines_for_slack(memo))
        self.assertIn("Ticker notes matter.", text)


class TruncatedTable(unittest.TestCase):
    def test_header_row_without_separator_is_not_raw_pipes(self):
        cut = "## CURRENT HOLDS\n\n| Ticker | Flag |\n"  # LLM reply cut off mid-table
        memo, ranked, holds = _memo()
        memo = qt.build_memo("2026-09-27", ranked, holds, 2541.0, cut, cut)
        blocks, _ = qt.build_slack(
            "2026-09-27", ranked, holds, 2541.0, True, True, memo
        )
        text = "\n".join(
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        )
        self.assertNotRegex(text, r"(?m)^\s*\|.*\|\s*$")
        self.assertIn("Ticker · Flag", text)


class TableEdgeCases(unittest.TestCase):
    def test_header_then_plain_rule_keeps_words(self):
        memo = "| Ticker | Flag |\n---\nNVDA flagged for review, thesis intact."
        text = "\n".join(qt._memo_lines_for_slack(memo))
        for w in ("Ticker", "Flag", "NVDA", "intact"):
            self.assertIn(w, text)
        self.assertNotRegex(text, r"(?m)^\s*\|.*\|\s*$")

    def test_header_and_separator_without_rows_keeps_labels(self):
        text = "\n".join(
            qt._memo_lines_for_slack("| Ticker | Flag |\n|---|---|\n\nnext")
        )
        self.assertIn("Ticker · Flag", text)
        self.assertIn("next", text)

    def test_separator_recognition(self):
        self.assertTrue(qt._is_table_separator("|---|---|"))
        self.assertTrue(qt._is_table_separator("| :--- | ---: |"))
        self.assertTrue(qt._is_table_separator("---|---"))
        self.assertFalse(qt._is_table_separator("---"))
        self.assertFalse(qt._is_table_separator("| a | b |"))


class RandomMessyMemos(unittest.TestCase):
    """Property check over many generated memos mixing headings, bullets, rules, prose
    and well-formed / cut-off / separator-only tables: no word is lost and no raw
    pipe row, heading or **bold** reaches Slack."""

    PIECES = [
        "## HEAD{n}",
        "### Sub{n}",
        "- bullet{n} **bold{n}** text",
        "plain prose{n} line",
        "---",
        "| A{n} | B{n} |",
        "|---|---|",
        "| a{n} | b{n} |",
        "| x{n} | | z{n} |",
        "|---|",
        "",
        "> quote{n}",
        "* star{n}",
        "**lead{n}** trailing",
    ]

    def test_many_random_memos(self):
        import random
        from collections import Counter

        rng = random.Random(20260927)
        for trial in range(2000):
            lines = [
                rng.choice(self.PIECES).format(n=f"{trial}x{k}")
                for k in range(rng.randint(1, 25))
            ]
            memo = "\n".join(lines)
            blocks = qt.memo_to_blocks(memo)
            text = "\n".join(b["text"]["text"] for b in blocks)
            have = Counter(_words(text))
            need = Counter(_words(memo)) - _first_col_labels(memo)
            for w, n in need.items():
                self.assertGreaterEqual(
                    have[w], n, f"trial {trial}: lost {w!r}\n{memo}"
                )
            self.assertNotRegex(text, r"(?m)^\s*\|.*\|\s*$", memo)
            self.assertNotRegex(text, r"(?m)^\s*#{1,6}\s", memo)
            self.assertNotIn("**", text, memo)


if __name__ == "__main__":
    unittest.main()
