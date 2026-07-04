"""
test_ui_tokens.py — guardrails for the shared design-token module.

Run: python3 test_ui_tokens.py   (exit 0 = pass, 1 = fail)

Catches the failure modes that would silently corrupt every generated page:
typo'd hex codes, non-int type-scale/radius values, and a body floor that
ever drops below 14px (the accessibility floor Rafael locked).
"""

from __future__ import annotations

import re
import sys

import ui_tokens as T

_HEX = re.compile(r"^#[0-9a-f]{6}$")

# Every color token must be a valid 6-digit lowercase hex string.
_COLOR_TOKENS = [
    "BG_BASE", "BG_PANEL", "BG_ELEVATED", "BG_TODAY", "BG_WEEKEND", "BG_LT_BANNER",
    "TEXT_PRIMARY", "TEXT_BRIGHT", "TEXT_MUTED", "TEXT_DIM",
    "BORDER_PANEL", "BORDER_LIGHT", "BORDER_TODAY",
    "STATUS_POSITIVE", "STATUS_NEGATIVE", "STATUS_WARNING",
    "ACCENT_CYAN",
]

# Every size/radius token must be a positive int (interpolated as f"{X}px").
_INT_TOKENS = [
    "FS_DISPLAY", "FS_STAT", "FS_H1", "FS_PNL", "FS_BODY", "FS_NAV",
    "FS_LABEL", "FS_MICRO", "FS_TINY",
    "RADIUS_SM", "RADIUS_MD", "RADIUS_LG",
    "TYPE_FLOOR_TARGET",
]


def main() -> int:
    failures: list[str] = []

    for name in _COLOR_TOKENS:
        val = getattr(T, name, None)
        if not isinstance(val, str) or not _HEX.match(val):
            failures.append(f"{name}={val!r} is not a valid #rrggbb lowercase hex")

    for name in _INT_TOKENS:
        val = getattr(T, name, None)
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            failures.append(f"{name}={val!r} is not a positive int")

    # Body floor invariant (Rafael-locked): base body text never below 14px.
    if getattr(T, "FS_BODY", 0) < 14:
        failures.append(f"FS_BODY={T.FS_BODY} violates the 14px body floor")

    # Font family must be a non-empty string.
    if not isinstance(getattr(T, "FONT_FAMILY", None), str) or not T.FONT_FAMILY:
        failures.append("FONT_FAMILY missing or not a non-empty string")

    if failures:
        print("FAIL — ui_tokens invariants violated:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS — {len(_COLOR_TOKENS)} color + {len(_INT_TOKENS)} numeric tokens "
        f"valid; body floor {T.FS_BODY}px OK"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
