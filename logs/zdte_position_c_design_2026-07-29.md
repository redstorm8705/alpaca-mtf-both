# 0DTE Direction Engine — Position C (BGG-aligned 2026-07-29, Rafael APPROVED)

## Decision
Gate 0DTE SPY direction on the **INTRADAY tape**, not the multi-day MTF swing score.
Using MTF x/12 (a multi-day swing confluence score) to pick a same-day 0DTE direction is a
**timeframe category error**. BGG unanimous (board seat + Gro + GAI; Gro & GAI both conceded
their prior strict-9/12 stance). Rafael approved 2026-07-29.

## The policy (Position C)
- **PRIMARY gate = intraday momentum (price vs 1-min VWAP / opening-range) AND dealer-gamma
  (GEX) / cross-asset regime congruence.**
- **MTF swing score = VETO/context only** — a strongly OPPOSING multi-day lean can veto; it
  NEVER greenlights.
- **Any disagreement of the primary intraday signals → NO-REC** ("no clean read — skip").
- **Hard time-of-day / theta cutoff** — no fresh long 0DTE into terminal decay.
- **Conviction shown as intraday confluence** (momentum + gamma-regime agreeing), NOT a swing
  x/12 score. Log every rec with VWAP state + gamma regime + outcome → a measurable, falsifiable edge.
- 0DTE remains LONG-only (buy call/put; max loss = premium paid).

## Gamma regime rule (dealer gamma / GEX)
- **Positive dealer gamma → tape pins / mean-reverts** → a directional 0DTE long bleeds to theta →
  demote/skip directional longs.
- **Negative gamma → moves amplify / trend** → directional 0DTE long has convexity → favor.
- GEX is ALREADY computed in the bot (`data/gex.py`, `_gex_context`/`_gex_overlay` in
  options_scanner.py) but only lightly used — Position C wires it in as a FIRST-CLASS 0DTE
  direction/regime gate. (This also closes Rafael's "why aren't we using GEX overnight/for 0DTE" gap.)

## Citations (documented professional practice)
- Gao, Han, Li & Zhou, "Market Intraday Momentum," *Journal of Financial Economics* (2018) —
  SPY first-half-hour return predicts last-half-hour, OOS. Backbone for intraday-primary gating.
- Berkowitz, Logue & Noser, "The Total Cost of Transactions on the NYSE," *J. Finance* (1988) —
  VWAP as institutional intraday directional benchmark. Brian Shannon, *Maximum Trading Profits* (2008).
- Toby Crabel, *Day Trading with Short Term Price Patterns and Opening Range Breakout* (1990) — ORB.
- SpotGamma / Menthor Q / Charlie McElligott (Nomura) — dealer-gamma regime (pos gamma pins, neg gamma trends).
- Euan Sinclair, *Volatility Trading* (2013), *Positional Option Trading* (2020) — option buyer's
  edge-vs-theta; long 0DTE is the extreme case. tastylive/tastytrade (Sosnoff/Battista) — long
  directional 0DTE is the disfavored side. Dan Nathan/RiskReversal — catalyst-anchored defined-risk archetype.
- CBOE 0DTE research — 0DTE ~50%+ of SPX option volume, flow overwhelmingly intraday/tactical.

## The two 0DTE recs (Rafael distinguishes them — do NOT conflate)
1. **Scanner tile** (`scan_results.html` top-right; `scan_to_html.py:_compute_0dte_rec`) — ALREADY
   closest to C (uses VWAP + regime + stickiness). FIXES: (a) direction/delta DESYNC — tile showed
   "$734 call" with Δ −0.380 (a PUT delta) + PUT ALERT; label must match the option; (b) SHOW a
   conviction score (intraday confluence: momentum + gamma agreement); (c) wire GEX regime as a
   primary gate. Keep it recommending. SHIP FIRST (fastest relief).
2. **Options page INDEX** (`options.html`; `options_scanner.py:_build_0dte_directional` +
   `_build_index_anchor`) — currently commits the category error (keys off MTF) AND emits BOTH a
   call and a put ("play either way" = a hidden long straddle, negative expectancy). REBUILD to
   consume the SAME intraday-primary single-direction decision; ONE leg, with conviction; NO-REC
   when no clean read. Ideally a SINGLE SOURCE OF TRUTH shared with the scanner tile so they can
   never disagree. SHIP SECOND.

## ROOT CAUSE of the scanner-tile desync (confirmed 2026-07-29 via direct read)
The "$734 call / Δ−0.380 (put) / PUT ALERT" contradiction is a **dual-writer race on
`logs/dte_prev.json`**. TWO scripts read+write the SAME file with INCOMPATIBLE schemas:
- `options_scanner.py:1425` (run_scan) writes `{direction: LOWERCASE "call"/"put", side, strike,
  symbol, score}` — its own `_select_directional_otm` strike.
- `scan_to_html.py:_save_dte_prev` (L1778) writes `{direction: UPPERCASE "CALL"/"PUT", strike,
  size, ts, confirm_count}` — its own delta-selected strike; `_compute_0dte_rec` returns UPPERCASE
  direction with a MATCHING delta (internally consistent — NOT the bug).
They clobber each other → the tile renders a Frankenstein rec (direction from one writer, strike/
delta from the other). This is the concrete manifestation of the two-engines problem and validates
the SINGLE-SOURCE-OF-TRUTH fix. IMPLEMENTATION MUST: make ONE engine own the 0DTE SPY direction +
its state file (one schema, one direction-case), and have BOTH pages consume it. Do NOT let two
scripts write `dte_prev.json`. (Deployed scan_to_html.py == local HEAD, 0 diff — so this is a live bug.)

## Gate (each ship, full sequence)
full read (Explore for >1000-line files) → 10-pt + RC → board (options + reliability) → Gro+GAI on
diff → statics → cold-2nd → propose → Rafael approve → FINAL preship → ship. Recommend-only scanners
(never trade) — no restart, but root .py = gated (markers required).

## IMPLEMENTATION PLAN (drafted 2026-07-29 — turnkey; still requires the full gate before ship)

### Architecture — ONE source of truth
Extract the 0DTE SPY direction decision into a SINGLE shared helper both consumers import, so two
scripts can never disagree or clobber state again:

- **NEW `strategy/zdte_direction.py`** (or a function in an existing shared module) exposing:
  `decide_spy_0dte(spy_result, chain_data, vwap_state, gex_ctx, regime, prev_state) -> dict`
  returning the canonical rec: `{status: "REC"|"NO_REC", direction: "CALL"|"PUT"|None, strike, delta,
  mid, iv, oi, conviction_n_of_3, conviction_components:{momentum,gamma,regime}, mtf_score,
  mtf_veto: bool, size, reasons:[...], ts}`. UPPERCASE direction, delta ALWAYS matches direction.
- **State file:** this helper (called from scan_to_html, the 5-min RTH engine) owns `logs/dte_prev.json`
  with ONE schema. `options_scanner.py` STOPS writing it (delete the L1421–1433 writer) and instead
  READS the canonical state for the SPY INDEX row. (Fixes the dual-writer race root cause.)

### Position C direction algorithm (in the shared helper)
1. **Intraday momentum (PRIMARY):** price vs 1-min VWAP (>0.10% separation) + opening-range read.
   Up→CALL lean, down→PUT lean, hugging VWAP→neutral. (Gao et al. 2018; Berkowitz 1988; Crabel 1990.)
2. **Dealer-gamma / GEX regime (PRIMARY):** NEGATIVE gamma → trends amplify → FAVOR directional long;
   POSITIVE gamma → pin/mean-revert → DISFAVOR (bias toward NO_REC). UNKNOWN/near-flip → neutral
   (fail-safe). GEX already computed (data/gex.py; live SPY = NEGATIVE all day 7/28). Source it the
   same way run_cycle Layer-8 does; never crash on stale/missing (UNKNOWN→neutral).
3. **Cross-asset regime (CONFIRM):** BULL/BEAR/NEUTRAL from `cr`/composite_regime.
4. **MTF swing score = VETO/context ONLY:** a STRONG opposing multi-day lean (e.g. strong_bull vs a
   PUT setup) → NO_REC; otherwise MTF does NOT gate. Never greenlights on its own.
5. **Greenlight rule:** REC only if the two PRIMARY signals (momentum + gamma-regime) agree on a
   direction AND cross-asset regime does not oppose AND MTF does not veto. ANY disagreement → NO_REC.
6. **Theta / time-of-day cutoff:** no fresh 0DTE REC into terminal decay (keep the existing opening-30-min
   block; add a late-day "no new entries after ~2:00 PM ET / flat by 3:45 ET" advisory per the board).
7. **Conviction = N/3** = how many of {momentum, gamma-regime, cross-asset regime} agree with the chosen
   direction. Display "Conviction N/3 · MTF x/12 · VWAP✓ · GEX(neg/pos) · Regime✓". Honest, legible,
   no black-box weight.

### Per-file changes
- **scan_to_html.py `_compute_0dte_rec`:** refactor to call the shared helper; drop the multi-day
  score as a *gate* (keep as veto input D); keep stickiness (flip needs confluence, not just score) +
  VWAP; ADD the GEX gamma-regime input; return the canonical rec (conviction_n_of_3 etc.). Tile render:
  show conviction N/3 + MTF; direction/delta now always consistent (single engine). NO_REC → clean
  "no clean directional read — skip" (not a stale Frankenstein).
- **options_scanner.py:** DELETE the `logs/dte_prev.json` writer (L1421–1433). `_build_0dte_directional`:
  for SPY, consume the canonical state (read dte_prev.json / call the helper) → ONE leg. For QQQ/Mag-7,
  apply the same single-direction Position C logic (one leg per name, NO_REC on no-confluence) instead of
  emitting both call+put. `_build_index_anchor`: render the single direction + conviction, or "NO 0DTE —
  no clean read" — never both legs.
- **Conviction/NO-REC display:** both pages show the N/3 conviction and a clean NO-REC state.

### Verification (when API back)
Unit-test the helper: (a) momentum+gamma agree → REC with correct direction+delta+N/3; (b) momentum vs
gamma disagree → NO_REC; (c) strong opposing MTF → NO_REC (veto); (d) positive-gamma pin → disfavor;
(e) stale/UNKNOWN GEX → neutral, no crash; (f) delta sign ALWAYS matches direction (regression on the
desync). Then live smoke on OCI: confirm scan_results tile + options INDEX show the SAME single
direction + conviction, zero dual-leg, no dte_prev clobber.

## Sequencing
1. Scanner-tile desync + conviction score + GEX gate (scan_to_html.py). FIRST.
2. Options-page single-direction rebuild consuming the shared decision (options_scanner.py). SECOND.
3. (Follow-up) exit-data "no output" — mri_level not persisted on trade record + entry events
   missing (D1 aftermath); stops/targets aggregation key-mismatch (weekly_review.py L757). SEPARATE.
