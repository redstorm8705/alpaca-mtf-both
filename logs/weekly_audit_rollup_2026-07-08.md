# Weekly Audit Rollup — 2026-07-02 → 07-09 (midday + post-market Gemini + meta audits)
**Aggregated for handoff.** Source: `logs/{midday_gemini,gemini_audit,meta_audit}_2026-07-0*.txt/json`.
The week produced ~25 "CRITICAL"/"HIGH" flags; **deduped, they collapse to two dominant roots + a short tier-2
list.** Prioritized where-to-continue below. (No board/Gro/GAI run — this is aggregation of existing audits.)

## #1 — P&L ATTRIBUTION CORRUPTION (THE root — flagged CRITICAL every day this week) ★ TOP OUTSTANDING
Chain: `execution/fill_helpers.py` **FILL UNVERIFIED** (can't recover a close fill from the activity feed) →
`orphan_manager` records `external_close` **at entry price → pnl = $0.00**. Consequences seen all week:
- **07-07:** 6 of 7 trades booked $0 → daily-loss math read **−73.86%** → **false kill-switch trip** (the incident).
- **07-02:** EOD P&L drift Alpaca $0.00 vs tracker **−$251** (10% of account). **07-08:** drift −$10.90 vs −$26.46.
- **RIVN $0 P&L** (07-07/08): entry $19.73 → exit $17.32 × 17 ≈ −$41 booked as $0.
- **RIVN direction corruption** (07-07): long → adopted as orphaned SHORT with no clean close; two RIVN records.
- **Stop-hit fidelity** (07-02 NVDA exit $197.96<stop $206.54 but "0 stops"; 07-06 RBLX short exit $57.45<stop $61.51 reason=stop_hit) — exit price/stop/reason don't reconcile (same fill-price-fidelity family).
**Fix direction (Gemini-consensus):** (a) recover fills by querying Alpaca **orders by ID** for fill price, not
just the activity feed; (b) on a genuine external_close, attribute P&L from the **actual fill / stop price /
last-known price** — NEVER fall back to $0; (c) the kill switch must run on reconciled P&L (Build A's
explained-P&L validator is adjacent — coordinate). **Files:** `execution/fill_helpers.py`,
`execution/orphan_manager.py`, `execution/portfolio_tracker.py`, `execution/fifo_pnl.py`, `trade_logger.py`.
**This is the single highest-value outstanding fix — a full week of CRITICALs, one root. It also overlaps Build B
(orphan-stop) and Build A (glitch safe-mode) — sequence it with them.**

## #2 — ENTRY-PIPELINE THROTTLING: qualified signals NOT being entered (NEW — dominant on 07-09) ★
07-09 surfaced a distinct cluster: the bot produces strong signals but doesn't act on them. Three related paths:
- **Sizing-cap STACKING → 0 shares (HIGH):** for high-score names (SOXL/MU/AMD/AVGO 10–12/12), the caps stack —
  `AB-3 TQI demotion` × `TSMOM vol-scale` × `VOTE-3 vol cap` × `VOTE-5 vol-target cap` — until `dollar_cap <
  price_per_share` → 0 shares → skip. e.g. `[SOXL] VOTE-5 vol-target cap produced 0 shares (σ=2.287 | equity=$2778
  | price=$201.20) — skipping entry.` On a ~$2.8K account, high-priced names become **un-tradeable even on 12/12
  signals.** (RC-7 zero-share-sizing family.) **Fix direction:** a small-account floor that lets ≥1 share through
  on a qualifying score, or don't stack all vol-caps multiplicatively.
- **"Score 9/12 — no allocation. Skipping" (MEDIUM):** PANW/SPY/QCOM met MIN_SCORE=9 but were skipped *before*
  Kelly/dollar-cap even ran — a silent filter/logic gap that never logs its reason.
- **ANOMALY-2 confirm_gate scans but no entry (CRITICAL on 07-09):** 9 symbols hit the confirm_gate but never
  entered while `open_positions=2–3` << `MAX_OPEN_POSITIONS=7`. Qualified signals blocked with **no stated reason.**
  Post-market audit: "severely impacting the bot's ability to enter trades." **Fix direction:** log the SPECIFIC
  block reason inside the ANOMALY-2 message (sector/correlation/news/liquidity/ORB), then fix whichever drops it.
**Net:** the bot's alpha is being throttled by the entry pipeline + small-account sizing. Distinct from #1 (which
is P&L *accounting*); this is *entering trades at all*. Files: `execution/entry_logic.py`, `strategy/run_cycle.py`
(confirm_gate), `execution/kelly.py` + the VOTE/TSMOM sizing caps. Good news: the bot IS trading again post-incident
(07-09 opened NET/HOOD/RIVN), so this is optimization, not a full stop.

## Tier-2 — genuine contained bugs
- **TOD/phase determination (07-03):** at 8:11 PM ET the bot logged "cycle @ 13:11 ET / phase=midday". Internal
  time/phase inconsistent with real time; currently masked by the "Market closed" gate but a real logic flaw.
  Contained fix in the TOD phase calc. File: `execution/orphan_manager.py::get_tod_phase` / `run_cycle` phase logic.
- **scan_to_html options NaN crash (07-08):** `_fetch_options_data` — `cannot convert float NaN to integer` (10×);
  display-layer, fails-closed. One-line NaN guard. NOTE: `scan_to_html.py` has a *rejected* Gro/GAI RC-3 item — care.
- **Cycle perf / scan timeouts (07-02, 07-06):** SLOW CYCLE 530–600s (vs ~200–250s designed); `signal_generator`
  phase-1 global timeout → **abandoned symbols** (TQQQ/SQQQ/MS dropped from the scan). Known (scan_to_html coupling).
  Degrades responsiveness + drops real symbols — worth a perf pass.

## Likely FALSE POSITIVE (verify ONCE, then document — do not chase)
- **VOLSHADOW "score discrepancy" (07-03, 07-06, 07-08, recurring):** Gemini flags `score_without_vol < MIN_SCORE`
  but `would_pass=true`, or VOLSHADOW score ≠ live trade score, and calls it "trading signals that fail criteria."
  This is almost certainly Gemini **misreading the log-only VOLUME SHADOW** (`VOLUME_CONFIRMATION_ENABLED=False`):
  `score_without_vol`/`would_pass` are shadow fields, NOT the live entry decision (live uses the 12-pt score with
  rsi_in_range). **Action: verify once that the live entry path never consumes VOLSHADOW fields; if confirmed,
  RENAME/clarify the shadow log fields so audits stop false-flagging it every day.** Not a trading bug.

## ALPHA / strategy (not bugs — need review, not a patch)
- **Exit discipline / R:R (07-02, 07-08):** TQI last-5 avg <30; avg_r_multiple 0.028 vs 2.5 target; "winners cut
  short." PARTLY a consequence of this week's false news-HALT liquidation (already fixed: interim 9d03be1 + Build F)
  and MRI overrides — but a genuine exit-quality question remains. → walk-forward/exit-review roadmap item.
- **High-score ≠ profitable (07-02):** 12/12 scores were the LOSERS (NVDA/SNOW/TSLA); 10-11 were winners. Scoring-
  model validation (the IC / walk-forward roadmap item). Not quick.
- **MRI data instability (07-02):** VIX/JPY frequently on stale cache / yfinance fallback (FMP failures) → MRI
  accuracy degraded. Known (VIX3M / data-source roadmap items).

## Minor / cosmetic
- `generate_dashboard.py` misleading "dotenv load failed" noise (07-07). HALT "kill switch active" log spam every
  cycle → log once (07-08). `scripts/collect_health_facts.py` exception-type polish (07-08).

## WHERE TO CONTINUE (handoff order)
1. **Merge Build F branch** (`claude/build-f-2026-07-08`) → deploy. (Ready now.)
2. **Forever-6 API build** (design locked). 
3. **Build A + B + the P&L-ATTRIBUTION ROOT (#1) together** — same reconciliation/fill family; the week's dominant
   CRITICAL. Build alongside A/B, not after.
4. **ENTRY-PIPELINE THROTTLING (#2)** — small-account sizing floor (≥1 share on a qualifying score / don't stack
   all vol-caps multiplicatively) + make ANOMALY-2 log the specific block reason + fix the silent "no allocation"
   skip. This is directly costing trades right now (07-09). High-value; board-led (touches sizing → risk-path).
5. **Tier-2 bugs:** TOD/phase, scan_to_html NaN, cycle-perf pass.
6. **VOLSHADOW verify-and-clarify** (kills the recurring false-flag).
7. **ALPHA reviews** (exit R:R, "high-score≠profit" scoring validation, MRI data, signals-generated-past-close) —
   strategy sessions, board-led, not patches. (07-09 meta-audit re-flagged "winners truncated by a blanket
   override" — that was the already-fixed false news-HALT, mis-attributed to MRI; interim 9d03be1 + Build F address it.)
