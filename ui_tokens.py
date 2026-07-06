"""
ui_tokens.py — Canonical design-token source of truth for all HTML generators.

Approach B (board 3-1 + Rafael sign-off, 2026-07-04): ONE flat module of design
tokens every HTML-generating script imports and interpolates into its own CSS.
Value constants only — no CSS-emitter functions (council: Gro + GAI + Wroblewski).

CANONICAL SYSTEM (Rafael-approved 2026-07-04, board GAI + Wroblewski):
The 5 monitoring pages (dashboard, weekly, scanner, options, monthly) all converge
to the CANONICAL tokens below. Decisions locked by Rafael:
  - Base background #0d0f1a family (monthly seed; rejected #0a0e14 / #080c10).
  - P&L stays GREEN gain / RED loss (trader convention), distinct in *use* from
    the 3-tier system-health status even though the hues coincide.
  - Weekly (first convergence page) gets full palette + type scale in one pass.

GOVERNING INVARIANT (Rafael, hard): algo-hedge-fund monitoring density + Apple/
Tesla clarity. Preserve every data field; cut only redundancy; clarity through
hierarchy, never subtraction.

Migration state: monthly extracted (Step 1) then aligned to canonical text tiers
here (muted/dim shift to the lighter canonical grays; primary/secondary remapped
byte-identically). Monthly keeps its granular FS_* sizes until the global type
pass. Weekly next (full canonical: palette + TYPE_* scale). scan/dashboard enter
the RTH import chain -> full Gro+GAI pre-ship at those steps.
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
# CANONICAL TOKENS — the shared target every page converges to
# ══════════════════════════════════════════════════════════════════════════════

# ── Backgrounds (3-step depth ladder) ──────────────────────────────────────────
BG_BASE     = "#0d0f1a"   # page background
BG_PANEL    = "#13162a"   # cards / panels / cells / tables
BG_ELEVATED = "#1e2240"   # modals, dropdowns, hover, higher separation

# ── Text tiers (hard-coded, not opacity — legible at any density) ──────────────
TEXT_PRIMARY   = "#e8ecff"   # body text, main figures
TEXT_SECONDARY = "#c8cce4"   # supporting copy, reduced-emphasis values
TEXT_MUTED     = "#8a94ae"   # labels, captions, metadata
TEXT_DIM       = "#5a6580"   # footer, hints, tertiary

# ── Borders ────────────────────────────────────────────────────────────────────
BORDER_DEFAULT = "#252847"   # separators, table rows, card edges
BORDER_STRONG  = "#5055a0"   # focus / hover / strong hierarchy

# ── Status 3-tier: SYSTEM HEALTH (fg + low-alpha bg tint). NOT P&L. ────────────
STATUS_POSITIVE    = "#30d158"                  # NORMAL / healthy / pass
STATUS_POSITIVE_BG = "rgba(48,209,88,.12)"
STATUS_WARNING     = "#ffd60a"                  # CAUTION / review / threshold
STATUS_WARNING_BG  = "rgba(255,214,10,.10)"
STATUS_NEGATIVE    = "#ff3b30"                  # CRITICAL / stop / risk breach
STATUS_NEGATIVE_BG = "rgba(255,59,48,.15)"

# ── P&L semantic colors (SEPARATE concept from status; hues coincide by design) ─
PNL_GAIN = "#30d158"   # realized/unrealized gain
PNL_LOSS = "#ff3b30"   # realized/unrealized loss

# ── Accent — cyan. Interactive / focus / active / one key figure per view. ─────
# NOT for: table headers, body copy, status badges, generic "positive".
ACCENT_CYAN = "#00e5ff"

# ── Typography ─────────────────────────────────────────────────────────────────
FONT_FAMILY = "-apple-system,BlinkMacSystemFont,'Segoe UI',monospace"

# Canonical type scale (px ints; interpolate f"{TYPE_BODY}px"). 14px is the floor.
TYPE_TITLE   = 28   # page title (1 per page)
TYPE_SECTION = 20   # section / card header
TYPE_BODY    = 16   # body text, hero numbers
TYPE_LABEL   = 14   # labels, dense table cells — FLOOR (kills 9-13px)

# ── Radii (px ints; interpolate f"{RADIUS_MD}px") ──────────────────────────────
RADIUS_SM = 4   # badges
RADIUS_MD = 6   # buttons, selects
RADIUS_LG = 8   # panels, cells, banners

# ── Extended CATEGORICAL palette — DISTINCT from semantic status/P&L ────────────
# Weekly encodes ~20 categorical distinctions (exit reasons, risk tiers, SPY event
# types, board roles) that exceed the 3-tier status + P&L + cyan set. These hues
# preserve those distinctions (board audit 2026-07-05 — "preserve every field").
# They are categorical taxonomy colors, NOT semantic status. Kept as hex strings
# so the alpha-suffix pattern (f"{CAT_TRAIL}18") for low-alpha tints still works.
CAT_TRAIL  = "#ff6b35"   # exit: trail-stop · SPY: war/geo (warm orange)
CAT_AMBER  = "#ff9f0a"   # exit: breakeven · risk: HIGH · SPY: technical (amber)
CAT_PURPLE = "#bf5af2"   # board role: Quant Research (purple)
CAT_INFO   = "#0a84ff"   # info-blue: SECTOR event, info badges, clickable signals
CAT_INFO_BG = "rgba(10,132,255,.15)"   # info-badge background tint
# Exit-target/signal/hard-stop/other reuse STATUS_POSITIVE/WARNING/NEGATIVE/TEXT_MUTED.

# ── Narrow density carve-out (Rafael-approved 2026-07-05) ──────────────────────
# 14px is the floor for ALL structural text. This sub-floor is permitted ONLY for
# the densest count-badges and multi-column table cells where 14px wraps/overflows.
TYPE_DENSE = 12   # documented carve-out — densest count badges / table cells only

# ══════════════════════════════════════════════════════════════════════════════
# PAGE-SPECIFIC / TRANSITIONAL — not yet unified into the canonical set.
# Monthly's granular sizes + its bespoke cell backgrounds/borders. These migrate
# to the canonical TYPE_* scale + BORDER_DEFAULT during the global type pass;
# kept here so monthly renders unchanged (except the intended muted/dim shift).
# ══════════════════════════════════════════════════════════════════════════════
BG_TODAY     = "#161934"   # monthly: today's calendar cell
BG_WEEKEND   = "#0f1121"   # monthly: weekend calendar cell
BG_LT_BANNER = "#0d1f12"   # monthly: lifetime-P&L banner (green-tinted panel)
BORDER_LIGHT = "#363a5a"   # monthly: nav-control borders / subtle dividers
BORDER_TODAY = "#5055a0"   # monthly: today-cell ring (== BORDER_STRONG value)

# Monthly's granular type sizes (pre-canonical; migrate to TYPE_* in the type pass)
FS_DISPLAY = 28   # lifetime-P&L headline
FS_STAT    = 20   # stat-box values
FS_H1      = 18   # page title
FS_PNL     = 17   # calendar-cell P&L
FS_BODY    = 14   # base body text
FS_NAV     = 13   # nav buttons / selects
FS_LABEL   = 12   # sub-labels, dates
FS_MICRO   = 11   # small labels, meta, footer
FS_TINY    = 10   # badges

# ── Live clock — cross-page rule (Rafael 2026-07-06) ──────────────────────────
# Every HTML page shows a DYNAMIC current-PT clock that ticks every second
# in-browser — never a static server-rendered timestamp. Insert LIVE_CLOCK_HTML
# into any page header. The "data as of / last scan" freshness labels stay put
# (they convey data AGE, a different thing from the wall clock).
# Plain string (NOT an f-string) so its literal { } braces are safe when a page
# inserts it via f"...{LIVE_CLOCK_HTML}...". Timezone forced to PT regardless of
# the viewer's locale (project TZ rule). tabular-nums stops digit jitter.
LIVE_CLOCK_HTML = (
    '<span id="live-clock" class="live-clock" '
    'style="font-variant-numeric:tabular-nums;white-space:nowrap"></span>'
    '<script>(function(){var el=document.getElementById("live-clock");'
    'if(!el)return;function t(){var n=new Date();'
    'el.textContent=n.toLocaleString("en-US",{timeZone:"America/Los_Angeles",'
    'weekday:"short",month:"short",day:"numeric",hour:"numeric",'
    'minute:"2-digit",second:"2-digit",hour12:true})+" PT";}'
    't();setInterval(t,1000);})();</script>'
)
