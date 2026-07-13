# WORK ORDER — 2026-07-13 PM (Rafael → session-resume cron 0365e14f @ ~1:45 PM PT)

**Method note (Rafael mandate 2026-07-13):** session-resume crons use **CronCreate one-shot** (session-only,
resume THIS session) — the method that worked last night. This is the standard for "set a cron" going forward.

## PRIORITY QUEUE (resumed session executes in order; full patch sequence + BGG gate on every gated file;
## ship on 3-way alignment per Rafael's auto-apply mandate; queue anything unaligned)

### 1) OPTIONS PAGE UX REDESIGN — options_scanner.py (display-only, NON-gated). Rafael feedback + Luke spec.
**Rafael's feedback (verbatim intent):** page has WAY too much text, not readable. Anchor SPY/QQQ on the
0DTE side so a random daytime glance shows call/put/strike immediately. Redesign with secondary dropdowns
(collapse SECONDARY conviction). Vol-events banner stays. Add last-scan + next-scan data; remove duplicative
data. The entire UPPER portion feels like static info that doesn't refresh — make it read LIVE.

**PROGRESS:** ✅ Step C SHIPPED (`ae679e6`) — paragraph explainers → ⓘ popover + rec counts folded into
each column header. Live+served on OCI. REMAINING for the resume: A (header freshness pill), D (SPY/QQQ
anchor block), E (SECONDARY dropdown), B (tiles 5→2), F (live cues).

**LUKE WROBLEWSKI SPEC (mobile-first / progressive disclosure) — build in this order (highest readability
win first):**
1. ✅ **C — kill the two 3-sentence explainer paragraphs → an ⓘ info-icon popover** [DONE `ae679e6`] next to each column title
   (`0DTE Directional ⓘ`). Biggest density drop, lowest risk. Explainer text moves into the popover.
2. **A — header collapse + FRESHNESS PILL.** Header = one row: `Options Scanner` (title only, kill the
   sub-caption sentence) + right-side **freshness pill** `● Updated 2m ago · next in 13m` (dot pulses on
   refresh, green→grey as it ages). This is the ONLY clock — DELETE the standalone running seconds-clock
   (reads as a screensaver, not data). Nav `← Scanner` to right edge; the "Wait — next window / 0DTE window
   closed" becomes a small status CHIP next to the pill, not a button.
3. **D — SPY/QQQ ANCHOR block** (the core glance ask). A pinned, never-collapsing, never-reordering
   `▌ INDEX 0DTE` sub-section ABOVE the tiered Mag-7 list, visually heavier (larger type, left accent bar):
   `SPY  ▲ CALL 552  ▼ PUT 550  IV 14` / `QQQ  ▲ CALL 488  ▼ PUT 486 …`. Call/put/strike = largest glyphs on
   the page, arrows color-coded. A glance any hour lands here first.
4. **E — SECONDARY → collapsed disclosure.** HIGHEST stays open; SECONDARY becomes a closed `▸ Secondary
   (score 8–9) · N names` disclosure (tap to expand), both columns. Each column header carries its own count
   (`0DTE · 13`), absorbing the cut stat tiles.
5. **B — STAT BAR 5 tiles → 2.** KEEP `High Conviction` + `VIX Tertile`. CUT `0DTE Recs`, `Weekly Recs`
   (counts now in each column header), `Weekly Expiry` (inline in the Weekly column title).
6. **F — LIVE cues (polish last):** freshness pill ticks every second (`next in 12m 59s`); on each 15-min
   refresh the dot pulses + changed strikes get a 200ms highlight fade; age states <5m green / 5–15m amber /
   >15m red "stale — retrying."
**Build notes:** emit `last_scan_ts` + `next_scan_ts` (= last+15m) from options_scanner.py; countdown is
client-side JS off next_scan_ts; all timestamps PT. Presentation only — no data-source/sizing logic touched.
Vol-events banner STAYS. After build: render-verify (preview desktop+mobile), Gro+GAI sanity (as done for the
2-col + 0DTE reframe), ship (commit/push) + OCI git pull + regenerate options.html + verify served + sync.

### 2) FOREVER-6 CASH-ONLY STARTER BUILD [GATED — Rafael APPROVED cascade+concept+cash-only]
Full spec: `logs/f6_starter_bgg_2026-07-13.md`. UNANIMOUS BGG: **CASH-ONLY (no margin** — margin lets an
unrelated intraday loss force-sell the never-sell book), **fund 1–3 highest-priority names** (breadth then
correlation-ranked), **−3% DYNAMIC close** trigger `−max(2, 0.15·VIX)%`, catalyst screen (RIVN-type exclusion),
**segregated starter budget** that can't cannibalize the deep-crash ladder, per-event + per-month caps. F6-first
cascade then QHM. Feature-Design gate first; RISK-PATH → cold masked-loss seat MANDATORY + Gro/GAI on the diff.
Note: the Q2 catalyst screen ties into the pending catalyst/news engine.

### 3) OPTION A — Slack-spam + main.py MEMORY LEAK [GATED — Rafael 'proceed']
Root cause already diagnosed: main.py RSS grows ~120→600MB over hours → false RAM alerts (available drops) +
memory-pressure scan-cycle hangs; 4 un-throttled alert sources (memory_watchdog.sh, monitoring/watchdog.py,
main.py/alerts.py restart trio) amplify each restart. START with the read-only tracemalloc leak diagnostic to
pin the leaking object, THEN the gated patch: throttle/dedupe the 3 alert sources (scripts/*.sh ARE preship-
gated) + fix the leak in main.py (RTH path). Board+Gro+GAI on the diff.

### 4) IC/ICIR PHASE 1b [GATED] — IF USAGE REMAINS
Add per-factor logging at the entry path: confluence.py's score_long/short_signal already returns a
`conditions` dict (the 12-component breakdown); persist it (+ score_16pt) into the trade_events.jsonl entry
event via a small trade_logger passthrough, so research/ic_engine.py (Phase 1 shipped, 81a4c08) can rank each
of the 12 components by IC/ICIR. Gated RTH entry path → full sequence + board + Gro/GAI.

## STATE AT WRAP (2026-07-13 ~10:40 AM PT)
- SHIPPED today: options 2-col (`4e74fac`) + 0DTE directional reframe (`d4a9874`); IC engine Phase 1 (`81a4c08`);
  GOOGL QHM add (2sh @ $359.30, stop bbed81da). All live + synced.
- Positions: GOOGL 2sh, NVDA 2sh, META 1sh, NET 1sh, RIVN 21sh — all with GTC stops. Equity ~$2,745, cash ~$681.
- Bot healthy on OCI (mtf-bot/mtf-writer/mtf-http active). Market open until 4:00 PM ET.
