# ruff: noqa: E501
"""slack_format.py — shared Slack message formatting helper (WS2 of the Slack messaging overhaul,
design record logs/design_records/slack_messaging_overhaul_2026-08-19.md).

THE PROBLEM this fixes: audit/report emitters post raw LLM replies to Slack. Those replies contain
GitHub-flavored **markdown tables** (`| a | b |` rows + `|---|` separators). Slack does NOT render
markdown tables, and the prior code additionally did `.replace("\n", " ")`, collapsing the table's
newlines into ONE long pipe-delimited line that wraps into unreadable garbage on mobile.

THE FIX: `mobile_clean()` converts markdown tables into compact, line-broken `a · b · c` rows,
DROPS the `|---|` separator rows, and — critically — NEVER collapses newlines to spaces. It is a
pure, deterministic, never-raising text transform, safe for every Slack emitter to route its text
through. It touches formatting ONLY — it never changes trading behavior.
"""
from __future__ import annotations

import re

# A separator row is made up ONLY of pipes, colons, dashes and spaces, e.g. `|---|:--:|---|`.
_TABLE_SEP_CHARS = set("|:- ")
# Collapse 3+ consecutive newlines down to exactly one blank line.
_MULTI_BLANK = re.compile(r"\n{3,}")


def _is_table_sep(stripped: str) -> bool:
    """True for a markdown table separator row like `|---|:--:|---|` (nonempty, only |:- and spaces)."""
    return bool(stripped) and set(stripped) <= _TABLE_SEP_CHARS


def _is_table_row(stripped: str) -> bool:
    """True for a markdown table data/header row: starts or ends with a pipe, or has >=2 pipes."""
    return "|" in stripped and (
        stripped.startswith("|") or stripped.endswith("|") or stripped.count("|") >= 2
    )


def mobile_clean(text: str | None, max_chars: int | None = None) -> str:
    """Make LLM / markdown text safe and readable in a Slack message on mobile.

    - DROPS markdown table separator rows (`|---|`).
    - Converts markdown table rows (`| a | b | c |`) into compact `a · b · c` lines
      (empty cells dropped). One line per row — newlines are PRESERVED, never collapsed to spaces.
    - Collapses runs of 3+ blank lines to a single blank line; trims leading/trailing whitespace.
    - If `max_chars` is given and the cleaned text is longer, truncates on a word boundary and
      appends a single ellipsis ("…"); otherwise no ellipsis is added.

    Pure and deterministic. Never raises on odd input (None/empty -> "").
    """
    if not text:
        return ""
    out_lines: list[str] = []
    for raw in str(text).splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if _is_table_sep(stripped):
            continue  # drop |---|:--:|---| separator rows entirely
        if _is_table_row(stripped):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            cells = [c for c in cells if c]
            if cells:
                out_lines.append(" · ".join(cells))
            continue
        out_lines.append(line)
    cleaned = _MULTI_BLANK.sub("\n\n", "\n".join(out_lines)).strip()
    if max_chars is not None and max_chars > 0 and len(cleaned) > max_chars:
        head = cleaned[:max_chars].rsplit(" ", 1)[0].rstrip()
        cleaned = (head or cleaned[:max_chars].rstrip()) + "…"
    return cleaned
