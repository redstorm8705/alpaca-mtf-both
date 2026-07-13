# WORK ORDER — 2026-07-13 PM (Rafael → session-resume cron 0365e14f @ ~1:45 PM PT)

**Method note (Rafael mandate 2026-07-13):** session-resume crons use **CronCreate one-shot** (session-only,
resume THIS session) — the method that worked last night. This is the standard for "set a cron" going forward.

## PRIORITY QUEUE (resumed session executes in order; full patch sequence + BGG gate on every gated file;
## ship on 3-way alignment per Rafael's auto-apply mandate; queue anything unaligned)

### 0) "overnight_atr_buffer_exit" — DIAGNOSED, WORKING AS DESIGNED. Remaining = transparency/naming/tag
### cleanup (P2, GATED). (Rafael flagged 2026-07-13: "why did RIVN sell at market, not the ~$15 stop?")
**RESOLUTION (read exit_logic.py:1187-1349): NOT A BUG.** The guard `_is_prior_session = entry_date < today`
+ `not opened_today` means this exit ONLY applies to OVERNIGHT-HELD positions (RIVN held since 7/9); QHM
exempt; suppressed before 10:00 AM ET (grace) then ACTIVE during RTH. So it is DESIGNED to run intraday on
stale overnight names — "overnight" = the POSITION is overnight-held, not the exit time. RIVN sat below
entry−0.5·ATR ($17.35) for 9 scans → exited @ $17.22 (−$17.64) to stop a bleed toward the $15.34 catastrophe
stop. Concern (a) RTH-firing = RESOLVED (by design, not systemic). REMAINING (all lower priority):
  (b) ✅ **TRANSPARENCY — SHIPPED (`8a195f9`).** Dashboard now shows 'soft ~$X · N/9' beside the GTC stop for
     overnight-held non-QHM positions (live breach count, color-escalating, tooltip). Verified served: NET
     ~$267.93·2/9, META ~$655.54·1/9; NVDA/GOOGL (QHM) excluded. FOLLOW-UP (P3): persist the EXACT per-scan
     be_thresh from exit_logic (gated) so it's penny-exact vs the current 0.5·ATR '~' approximation.
  (c) **ORDER TAGGING (P3):** the exit's close_position() submits an untagged (bare-UUID) market order;
     tag it with the tier client_order_id per the ownership-ledger design.
  (d) **RENAME (P3):** "overnight_atr_buffer_exit" is misleading (caused this confusion) → e.g.
     "overnight_held_breakeven_buffer_exit". Cosmetic; bundle with (b).
Original (a) systemic-misfire worry retired. No emergency; handle in normal queue order after the builds.
**ROOT CAUSE ESTABLISHED (Alpaca + bot log, authoritative):** RIVN was NOT stopped out. Its $15.34 GTC stop
was canceled/unfilled (correct). A SEPARATE soft-exit fired: `trade_events.jsonl` 2026-07-13T09:24:08
`event=stop_hit reason="overnight_atr_buffer_exit | 9-scan breach | entry=$18.06 thresh=$17.35
(ATR=1.419 Tier_adj=0.500)"` → market-sold 21sh @ $17.22, pnl −$17.64. The sell order carried a BARE-UUID
client_order_id (99964b72…), i.e. UNTAGGED (not IN-/mtf-/QH-). Mechanism: exit when price < entry−0.5·ATR
sustained 9 scans. **THREE things to review (each a real concern):**
  (a) **"OVERNIGHT" exit FIRED MID-SESSION (12:24 ET / 9:24 PT).** Is an overnight-ATR-buffer exit supposed
     to be RTH-active? If mis-scoped, it may be tightening exits intraday on ALL positions (systemic — check
     every open name for the same 9-scan ATR-buffer arming during RTH). This is the load-bearing question.
  (b) **TRANSPARENCY:** operator sees only the ~$15 GTC stop; the REAL exit is ~$17.35. Dashboard + options/
     scanner should surface BOTH the hard GTC stop AND the ATR-buffer soft-exit level so exits never surprise.
  (c) **ORDER TAGGING:** this exit submits an UNTAGGED market order (bare UUID) — violates the per-tier
     ownership-tag design (IN-/QH-/F6-). Tag it so ownership-guard/ledger can attribute it.
**DO:** full read of the overnight_atr_buffer_exit path (strategy/run_cycle.py + execution/exit_logic.py +
risk_manager.py — locate the `overnight_atr_buffer_exit` / `Tier_adj` / 9-scan-breach logic), 10-pt + RC audit,
board (incl. masked-loss seat — this is an EXIT that realizes losses) + Gro + GAI. Determine intended vs actual
RTH behavior BEFORE any change; if it's working-as-designed, the fix may be (b) transparency + (c) tagging only.
NOTE: entry event logged stop=$15.39/$18.41 at entry but the ATR-buffer thresh is computed separately — confirm
the two-stop model is intended and documented.

### 1) OPTIONS PAGE UX REDESIGN — options_scanner.py (display-only, NON-gated). Rafael feedback + Luke spec.
**Rafael's feedback (verbatim intent):** page has WAY too much text, not readable. Anchor SPY/QQQ on the
0DTE side so a random daytime glance shows call/put/strike immediately. Redesign with secondary dropdowns
(collapse SECONDARY conviction). Vol-events banner stays. Add last-scan + next-scan data; remove duplicative
data. The entire UPPER portion feels like static info that doesn't refresh — make it read LIVE.

**PROGRESS — 5/6 SHIPPED:** ✅ C (`ae679e6`) explainers→ⓘ popover+counts · ✅ B (`1d73215`) stat 5→2 ·
✅ E (`1a41c14`) SECONDARY→collapsed dropdown · ✅ A (`cafbdf2`) freshness pill (updated Xm/next in Ym,
age-color dot; killed running clock + nav-sub dup; next_scan_ts emitted) · ✅ D (`03a8886`) pinned
▌INDEX-0DTE SPY/QQQ anchor (call/put/strike large glyphs, de-duped from tier). ALL live+served, verified
in a full-page screenshot. **REMAINING: F only** (optional polish — pulse on 15m refresh + 200ms strike-
change highlight; the freshness dot already pulses). Options UX redesign COMPLETE + REFINED: header now matches the dashboard format (logo+clock+countdown+pulse
pill) and top/bottom duplication removed (verbose window pills + 5-line footer legend cut) — `9c2ac5b`. Rafael
screenshot was a cached pre-A view; live page is clean. Trivial follow-up: purge dead CSS (.top-nav/.nav-sub/
.legend-row/.fresh-pill now unused). Optional step F polish (strike-change flash) still open.
Next = gated builds: #2 Forever-6 cash-only, #3 spam/leak, #4 IC Phase 1b.

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
