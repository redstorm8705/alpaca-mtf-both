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
    "TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_MUTED", "TEXT_DIM",
    "BORDER_DEFAULT", "BORDER_STRONG", "BORDER_LIGHT", "BORDER_TODAY",
    "STATUS_POSITIVE", "STATUS_NEGATIVE", "STATUS_WARNING",
    "PNL_GAIN", "PNL_LOSS",
    "ACCENT_CYAN",
]

# Every low-alpha status tint must be a valid rgba() string.
_RGBA_TOKENS = ["STATUS_POSITIVE_BG", "STATUS_WARNING_BG", "STATUS_NEGATIVE_BG"]

# Every size/radius token must be a positive int (interpolated as f"{X}px").
_INT_TOKENS = [
    "TYPE_TITLE", "TYPE_SECTION", "TYPE_BODY", "TYPE_LABEL",
    "FS_DISPLAY", "FS_STAT", "FS_H1", "FS_PNL", "FS_BODY", "FS_NAV",
    "FS_LABEL", "FS_MICRO", "FS_TINY",
    "RADIUS_SM", "RADIUS_MD", "RADIUS_LG",
]


def main() -> int:
    failures: list[str] = []

    for name in _COLOR_TOKENS:
        val = getattr(T, name, None)
        if not isinstance(val, str) or not _HEX.match(val):
            failures.append(f"{name}={val!r} is not a valid #rrggbb lowercase hex")

    for name in _RGBA_TOKENS:
        val = getattr(T, name, None)
        if not isinstance(val, str) or not re.match(r"^rgba\([\d,.\s]+\)$", val):
            failures.append(f"{name}={val!r} is not a valid rgba() string")

    for name in _INT_TOKENS:
        val = getattr(T, name, None)
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            failures.append(f"{name}={val!r} is not a positive int")

    # Canonical type floor (Rafael-locked): TYPE_LABEL is the 14px floor.
    if getattr(T, "TYPE_LABEL", 0) < 14:
        failures.append(f"TYPE_LABEL={T.TYPE_LABEL} violates the 14px type floor")

    # Font family must be a non-empty string.
    if not isinstance(getattr(T, "FONT_FAMILY", None), str) or not T.FONT_FAMILY:
        failures.append("FONT_FAMILY missing or not a non-empty string")

    if failures:
        print("FAIL — ui_tokens invariants violated:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"PASS — {len(_COLOR_TOKENS)} color + {len(_RGBA_TOKENS)} rgba + "
        f"{len(_INT_TOKENS)} numeric tokens valid; type floor {T.TYPE_LABEL}px OK"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
