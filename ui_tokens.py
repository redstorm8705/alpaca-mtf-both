"""
ui_tokens.py — Shared design-token source of truth for all HTML generators.

Approach B (board 3-1 + Rafael sign-off, 2026-07-04): ONE flat module of design
tokens that every HTML-generating script imports and interpolates into its own
CSS strings. No CSS-emitter functions at this stage — value constants only.

Council consensus (Gro + GAI + Wroblewski, 2026-07-04): constants-only, flat,
semantic names, at repo root. This keeps the FIRST migration byte-for-byte
identical (a golden-diff gate proves it) while establishing the single decision
layer through which the later visual convergence flows.

Migration order: monthly -> weekly -> scan -> dashboard -> options.
  monthly / weekly ... ungated (standalone / subprocess-spawned).
  scan / dashboard ... enter the live RTH import chain -> full Gro+GAI pre-ship.

GOVERNING INVARIANT (Rafael, hard): algo-hedge-fund monitoring density + Apple/
Tesla clarity. Preserve every data field; cut only redundancy (duplicated literal
values); clarity through hierarchy, never subtraction.

Token values below are TODAY'S EXACT VALUES harvested from monthly_review.py.
They are the current design system, not the target. The visible convergence to
the north-star palette (cyan accent, 28/20/16/14 scale) is a separate, LATER,
intentionally-visible step — see the RESERVED section at the bottom.
"""

from __future__ import annotations

# ── Colors — backgrounds (hierarchical depth: base -> panel -> elevated) ───────
BG_BASE      = "#0d0f1a"   # page background
BG_PANEL     = "#13162a"   # card / panel / cell surface
BG_ELEVATED  = "#1e2240"   # interactive controls (buttons, selects)
BG_TODAY     = "#161934"   # semantic: today's calendar cell
BG_WEEKEND   = "#0f1121"   # semantic: weekend calendar cell
BG_LT_BANNER = "#0d1f12"   # semantic: lifetime-P&L banner (green-tinted panel)

# ── Colors — text (signal strength: primary -> bright -> muted -> dim) ─────────
TEXT_PRIMARY = "#c8cce4"   # default body text
TEXT_BRIGHT  = "#e8ecff"   # headers, emphasis, stat values
TEXT_MUTED   = "#636680"   # secondary labels, captions
TEXT_DIM     = "#363a5a"   # tertiary hints (empty cells, footer)

# ── Colors — borders (mirror the background hierarchy) ─────────────────────────
BORDER_PANEL = "#252847"   # panel / cell borders (also nav hover surface)
BORDER_LIGHT = "#363a5a"   # control borders / subtle dividers (== TEXT_DIM today)
BORDER_TODAY = "#5055a0"   # highlight ring for today's cell

# ── Colors — status (semantic; hue is meaning, not decoration) ─────────────────
STATUS_POSITIVE = "#30d158"   # wins / gains / healthy
STATUS_NEGATIVE = "#ff3b30"   # losses / errors
STATUS_WARNING  = "#ffd60a"   # caution / mechanical / mid-tier

# ── Typography ─────────────────────────────────────────────────────────────────
FONT_FAMILY = "-apple-system,BlinkMacSystemFont,'Segoe UI',monospace"

# Type scale (px, ints). Interpolate as f"{FS_BODY}px". Named by display role;
# several roles share a size today. The later convergence collapses this 9-value
# scale toward the 4-tier north-star (28/20/16/14) — encoding sizes here makes
# that flip a one-line change per token, not a five-file hunt.
FS_DISPLAY = 28   # lifetime-P&L headline number
FS_STAT    = 20   # stat-box values
FS_H1      = 18   # page title
FS_PNL     = 17   # calendar-cell P&L
FS_BODY    = 14   # base body text (FLOOR — never below 14)
FS_NAV     = 13   # nav buttons / selects
FS_LABEL   = 12   # sub-labels, dates, back-link
FS_MICRO   = 11   # small labels, meta, footer
FS_TINY    = 10   # badges

# ── Radii (px, ints). Interpolate as f"{RADIUS_MD}px". ─────────────────────────
RADIUS_SM = 4   # badges
RADIUS_MD = 6   # buttons, selects
RADIUS_LG = 8   # panels, cells, banners

# ── RESERVED — north-star convergence targets (UNUSED in Step 1) ───────────────
# Defined now, deliberately not referenced by any generator yet, so the later
# VISIBLE convergence step is a value flip here rather than a cross-file hunt.
# Do NOT wire these into templates until that step is explicitly approved.
ACCENT_CYAN = "#00e5ff"                 # north-star accent (replaces ad-hoc highlights)
TYPE_FLOOR_TARGET = 14                  # body floor stays 14
TYPE_SCALE_TARGET = (28, 20, 16, 14)    # 4-tier target scale (h1/h2/h3/body)
