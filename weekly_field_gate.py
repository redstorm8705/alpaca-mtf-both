"""
weekly_field_gate.py — characterization gate for the weekly_review.py styling
rewrite (board audit 2026-07-05).

The weekly canonical rewrite is an INTENDED visual change (colors, sizes), so a
byte-diff can't gate it. But a pure *styling* change lives entirely in tags and
style="" attributes — the visible TEXT (every P&L value, ticker, %, count,
section label, exit-reason name, board POV) must be unchanged. This gate strips
all HTML tags, normalizes whitespace, drops the per-run timestamp, and compares
the visible-text of two renders. Any difference = a data field was altered or
dropped by the rewrite (invariant violation) — NOT a styling change.

Usage:
    python3 weekly_field_gate.py extract BEFORE.html > before.txt
    python3 weekly_field_gate.py extract AFTER.html  > after.txt
    diff before.txt after.txt   # expect: empty (visible data identical)
"""

from __future__ import annotations

import re
import sys

# Lines carrying an inherently per-run value that legitimately differs each render.
_VOLATILE = re.compile(r"(Updated|generated|as of|\bPT\b|\bET\b)", re.IGNORECASE)


def extract_visible_text(html: str) -> str:
    """Return the page's visible text: tags stripped, whitespace normalized,
    volatile timestamp lines dropped. Styling-only edits leave this invariant."""
    # Drop <style>/<script> bodies entirely (not visible data).
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Turn block boundaries into newlines so fields stay on separate lines.
    html = re.sub(r"<(/?)(div|tr|table|section|h[1-6]|li|br|p)\b[^>]*>", "\n", html,
                  flags=re.IGNORECASE)
    # Strip all remaining tags.
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode the handful of entities this page emits.
    for ent, ch in (("&amp;", "&"), ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
                    ("&middot;", "·"), ("&times;", "×")):
        text = text.replace(ent, ch)
    out: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line or _VOLATILE.search(line):
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "extract":
        print("usage: weekly_field_gate.py extract <html>", file=sys.stderr)
        return 2
    with open(sys.argv[2], encoding="utf-8") as f:
        sys.stdout.write(extract_visible_text(f.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
