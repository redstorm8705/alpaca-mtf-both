# SLACK OUTPUT FORMAT — full text (canonical definition site for SLK01–SLK15)
<!-- Rafael mandate 2026-08-23: the QHM status report rendered as an unreadable wall on his phone.
     Root cause (verified vs docs.slack.dev): reports were built from markdown `#` headings + `| pipe
     tables |` (NEITHER renders in Slack) plus lines packing many ` | `-separated fields that wrap on
     a narrow screen. This spec — grounded in Slack's official formatting docs + a mobile-first review
     through Luke Wroblewski's documented UX work (Mobile First; "Obvious Always Wins"; Web Form Design
     eye-tracking) — is BINDING on EVERY bot Slack report/alert, existing and future. CORE/CLAUDE.md
     carries the pointer; THIS file is authoritative. -->

## Why (the failure this prevents)
Slack is NOT standard Markdown. `#`/`##` headings, `| pipe tables |`, `- `/`* ` auto-bullets, and `---`
rules DO NOT render — they print as literal characters. A report built from them, or from lines that
pack 6–7 fields separated by inline ` | `, collapses into a wall of wrapped text on a phone (where
Rafael reads). Block Kit + short single-column lines is the fix.

## SLK01 — Block Kit only, never a raw text blob
Every bot report is composed as a Block Kit `blocks` array (sent via the incoming-webhook `blocks`
field). Never assemble a report as one big mrkdwn string. Reserve plain-text `send_slack` for short
one-line operational alerts only.

## SLK02 — The notification `text` carries the ANSWER (accessibility + first screen)
Always set a top-level summary `text` alongside `blocks`. It is the phone notification preview AND the
screen-reader string, so it must be a real one-line answer with the headline number + any flag, never a
bare label. E.g. `QHM Weekly · 4 holds · net −$18.20 (−1.4%) · ⚠️1 near stop · 🔒2 earnings-locked`.

## SLK03 — Answer first, detail second (block order)
`header` (report name + date) → portfolio HEADLINE (`section`/`context`: net P&L, count, flags) →
`divider` → per-item blocks → footer `context` (source, PT timestamp, dashboard link). The single most
important number lands in the first ~2 blocks.

## SLK04 — mrkdwn is Slack's syntax, not standard Markdown
Bold = **single** asterisk `*bold*` (NOT `**double**`); `_italic_`, `~strike~`, `` `code` ``, `>quote`,
`\n` = line break, links `<url|text>`. FORBIDDEN (do not render): `#` headings, `| tables |`, `- `/`* `
auto-bullets, `---` rules, `**double-asterisk**` bold.

## SLK05 — One idea per line; budget every line to ~30–40 chars
A phone shows ~30–40 chars before wrapping. Every `\n`-separated line must fit that. NEVER pack fields
with inline ` | ` — that is the exact wrap failure. Split fields onto their own short lines.

## SLK06 — Single column beats a 2-column grid on a phone
Default to single-column `section` `text` with short `\n` lines for per-item records. Do NOT use
`section` `fields` (the 2-col grid) for per-item data — each column is ~40% of a narrow screen (values
wrap/truncate) and Slack's left-right-left-right fill scrambles visual columns. Reserve `fields` ONLY
for a short symmetric portfolio summary (≤4 pairs, each value ≤~12 chars: Equity / Cash / Holds / Net).

## SLK07 — Bold only the scannable value
Bold the P&L and warning flags; leave labels and supporting text plain. If everything is bold, nothing
stands out ("Obvious Always Wins" — emphasis works only when rare).

## SLK08 — Pre-attentive triage glyph, never color alone
Lead each item headline with one status glyph (🔴/🟢 or ▲/▼) so the eye triages winners/losers down the
left edge without reading. Add a warning glyph (⚠️ near stop / over cap, 🔒 earnings-locked) only when
the condition is true. ALWAYS pair the glyph with the signed number/text — never encode meaning in
color or emoji alone (colorblind + accessibility).

## SLK09 — `context` blocks for secondary detail (progressive disclosure)
`context` renders small + muted — Slack's built-in "secondary info" mechanism. Put Tier-3 numbers
(entry→current, shares, cost basis, source, timestamp) in `context`; keep `section` for the numbers
that matter.

## SLK10 — Dividers separate GROUPS, not items
A `divider` between every item fragments the scroll with heavy rules. Use it only between logical groups
(needs-attention vs on-track; holds vs summary; summary vs footer). Let section/context rhythm + glyphs
separate individual items.

## SLK11 — No ASCII / `code`-block tables
Never rebuild tables in monospace `code` to align numbers — long code lines force horizontal scroll on
a phone (the same failure returning) and read poorly to screen readers. Use labeled values on short lines.

## SLK12 — Meaningful, tappable links
Link text describes its destination and is long enough to tap (~44px): `<url|View NVDA chart>`, never a
bare `→` or `<url|here>`.

## SLK13 — Sort by attention
Most-important-first: losers / near-stop / earnings-imminent at the top. Optionally group under
`⚠️ Needs attention` / `✓ On track` with one divider between.

## SLK14 — NEVER truncate; deliver the FULL report, chunked across ordered messages
The reader must be able to read the ENTIRE report inside Slack — never cut off, never replaced by a
"…see full report at <path>" pointer (Rafael 2026-08-23: a non-clickable "link to full report" means
he cannot read it; a truncated message is useless). Respect Slack's hard limits — ≤50 blocks/message,
≤10 `fields`/section, `header` ≤150 chars (plain_text), and **≤3000 chars per section `text`** — by
SPLITTING the full content across multiple ordered Block Kit messages (labeled (i/N), sent in
sequence) so nothing is dropped or hidden. `send_slack_blocks` already chunks on block boundaries
(≤45 blocks/message); long prose (e.g. a research thesis) must additionally be split across multiple
section blocks so no single section exceeds ~3000 chars. A CLICKABLE dashboard URL
(`<https://…|View full dashboard>`) may be added as an EXTRA convenience — NEVER as a substitute for
the readable content. For an unavoidably long list, order by attention (SLK13) so the most important
items lead, but STILL send all of it.

## SLK15 — PT timestamps + semantic header
All times in PT (project rule §Timezone). Use the semantic `header` block for the title (not bold text
faking a heading), kept to the report name (+ short date) — portfolio data belongs in the summary
section, not the header.

## Canonical per-item pattern (QHM hold example)
Per hold = one `section` (3 short lines) + one `context` (muted detail), no divider between holds:
```
section.text (mrkdwn):
  🔴 *NVDA*  *−$4.39 (−2.00%)*
  🛡 Stop $186.24 · 13.3% cushion
  ⚖️ 8.1% of equity (cap 20%) · 🔒 Earnings locked (3d)
context.elements[0] (mrkdwn):
  1 sh · entry $219.11 → now $214.72
```
Renders as four clean, non-wrapping phone lines with the P&L as the only bold, glyph-led scan line.

## Enforcement
Per DOCUMENTATION-IS-NOT-ENFORCEMENT: a lint check (`rules_lint`/preship) SHOULD reject a bot Slack
payload that contains a literal `#`-heading line, a `|`-delimited table row, or a `send_slack` report
body over N chars without `blocks`. Until that lint ships this is review-enforced (the reviewer/board
checks any new or modified Slack output against SLK01–SLK15). Building the lint is a tracked follow-up.
