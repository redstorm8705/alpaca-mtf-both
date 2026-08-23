# QHM RESEARCH → BGG → DIVERSIFIED SELECTION → AUTO-EXECUTION PIPELINE — design record
**Status:** DESIGN-STAGE (Rafael scoped 2026-08-23). NO CODE until the Feature Design Protocol + the
mandatory RISK-PATH BGG design pass + Rafael design approval are complete. This is the most
capital-consequential feature in the bot; treat accordingly.

## What Rafael asked for (2026-08-23, verbatim intent)
A **weekly** QHM research report that is a full research→decision→execution pipeline:
1. **News-flow review on EXISTING QHM holds first** — what has changed since entry.
2. **Thesis-change detection** — if new info changes the core thesis (long OR short), convene **BGG**
   for how to MANAGE that position (hold / trim / exit / flip).
3. **New candidate research** — comprehensive, BGG-augmented: news, **13F flows, equity raises,
   upgrades/downgrades, debt issuance**, fundamentals.
4. **Diversification constraint** — new adds must be diversified; not over-exposed to one sector.
5. **Finalized recommendations become BUY INSTRUCTIONS the bot submits at the next market open.**

## Rafael DECISIONS (recorded)
- **Cadence:** weekly (was briefly "on-demand" — he corrected: scheduled).
- **Execution gate:** **FULLY AUTOMATIC, NO human checkpoint** — finalized new buys AND thesis-change
  position actions (trim/exit/flip) auto-submit at the next open. Chosen with the risk stated plainly
  in the prompt ("lets an AI research pipeline deploy capital unattended — a real widening of the risk
  envelope; requires a full board vote + your standing authorization"). This IS that standing
  authorization for the FEATURE. It does NOT waive the code-correctness / safety-control gates below.

## NON-NEGOTIABLE SAFETY CONSTRAINT (the line that makes "fully automatic" safe)
Every auto-submitted order MUST route through the EXISTING safety envelope — it may never size beyond,
or bypass, any of:
- Kelly fraction sizing (execution/kelly.py) + the per-trade size caps;
- gross-exposure cap and the 100%-equity overnight budget (Arch-Inv #11);
- correlated-exposure / sector gates (Arch-Inv #10; GE+GEV already treated as one position);
- the **7% kill switch** (S02) and `paper=True` (S01);
- the QHM manager's existing entry gates (earnings-gate lock, not-before-date, tranche logic).
The pipeline PROPOSES (what/whether); the envelope BOUNDS (how much) and can HALT (kill switch). Any
component that would let a pipeline order exceed a cap or skip the kill switch is a REJECT. This is
"more aggressive WITHIN the envelope," never a wider envelope (Profitable>Perfect scope line, G02).

## FAIL-SAFE (never-mask-a-loss applied to a research pipeline)
Any failure in the chain — data fetch, BGG unavailable, ambiguous thesis read, diversification
unresolved, sizing error — resolves to **NO ORDER**, never a default/guessed order. A missing signal
must never produce a trade. Fail closed, always.

## RISK-PATH CLASSIFICATION
MAXIMUM. It autonomously increases position SIZE, trade FREQUENCY, and CONCURRENCY (S06 definition on
all three axes). Therefore the FULL mandatory gate applies on BOTH the design and every diff: cold
board (parallel subagents) + the masked-loss/risk-asymmetry seat + Gro + GAI. The same-day/all-zero
fast path is NOT available. Default-to-risk-path on any ambiguity.

## Feature Design Protocol (preliminary — refine in the BGG design pass)
1. **Data source / tier / fallback:** news = events/news_monitor + T2 FMP; upgrades/downgrades,
   earnings, (some) 13F = T2 FMP; equity raises / debt issuance = SOURCE TBD (may need a new feed —
   FMP coverage to be verified; if unavailable, that input is omitted + flagged, never guessed).
   Fundamentals = FMP. Prices/positions = T1 Alpaca. Universe = quarterly_holds_config.json + screen.
2. **Output:** (a) the research memo → logs/quarterly_holds_research_*.md (git, cross-account); (b) a
   Slack post per **rules/slack_format.md** (Block Kit); (c) a finalized ACTION SET (new buys +
   position changes) written to the QHM entry/queue path for execution at next open.
3. **Integration point:** likely execution/quarterly_hold_manager.py `add_candidate()` /
   `maybe_enter_positions()` for new buys (so it inherits QHM sizing/gates), and the QHM exit/trim path
   for thesis-change actions. EXACT wiring is a board question — must reuse existing gated paths, not a
   new order surface.
4. **Failure mode:** fail-closed to NO ORDER (see FAIL-SAFE above).
5. **Board vote:** MANDATORY (risk-path, all three axes). Full cold board + masked-loss seat + Gro/GAI.

## OPEN DESIGN QUESTIONS (for the BGG design pass, with my leaning)
- **Scheduling mechanism:** the pipeline needs a reasoning + BGG pass; a cold OCI cron cannot convene a
  real board or write theses. Options: (a) OCI autonomous API run (autonomous_review-style) that runs
  the research + BGG + writes the action set; (b) an in-session weekly run. (a) conflicts with the
  session-bound-crons preference ([[feedback-session-bound-crons]]) but is the only way "fully
  automatic weekly" works unattended. RESOLVE with Rafael + BGG.
- **Diversification rule:** derive DYNAMICALLY (no static regime, B10) — e.g. sector-exposure cap from
  the current book + a correlation check, not a hardcoded "max 2 per sector." BGG to propose.
- **Thesis-change → action mapping:** how BGG's hold/trim/exit/flip maps to concrete order(s), all
  bounded by the envelope. A "flip" (long→short) on a quarterly hold is especially sensitive.
- **BGG-in-the-loop mechanics for a scheduled run:** which board seats (BoD+AB for pick selection per
  board protocol; + risk seats for the execution actions), and how Gro/GAI are called in the automated
  context (TPM limits, 503 handling).
- **Order timing / idempotency:** "next open" queueing; guard against double-submission across reruns;
  reconcile against existing positions before adding.

## DIRECTIONAL EARNINGS SCALING — new policy Rafael raised (2026-08-23), NOT in the current ruling
The existing earnings ruling (qhm_earnings_trim_design_2026-08-10, "balanced trim, keep core") only
DE-RISKS a runup: trim-if-running (scaled by implied move + runup), keep a core, cancel the stop
pre-print, PEAD re-add AFTER the crush. It has NO pre-earnings scale-IN and NO short side. Rafael wants
the pipeline to add the missing directional side:
- LONG + bullish earnings expectation + dropping INTO the print → SCALE IN (add on the pre-earnings dip)
  toward target; trim/bank if it's running (existing rule).
- SHORT + bearish expectation → the inverse: add to the short into the print, bank profit along the way
  as it falls.
This FLIPS the posture from "reduce into a binary event" to "ADD into a binary event" — a real strategy
change and squarely risk-path (more size/exposure into an earnings gap). It CANNOT ride on the existing
trim ruling; it needs its own **BoD+AB** vote (QHM pick/exit policy is BoD+AB only) inside this pipeline
design. Note the tension: the current earnings GATE LOCKS a position ~3 days pre-print (NVDA is LOCKED
now) and cancels the stop — a pre-earnings add must reconcile with that lock, and every add stays
bounded by the envelope (Kelly/gross/overnight/sector caps). NVDA is currently underweight (~8% vs 20%
target) because Rafael manually trimmed it — a live example of a hold that a bullish-thesis pre-earnings
scale-in WOULD target, but only via a governed, voted rule, never ad hoc. VERIFY at design time: NVDA's
actual next-earnings date (config `earnings_exit_before: 2026-08-19` looks stale vs the live LOCKED-3d
gate) and exactly which earnings sub-rules are shipped-live vs design-only.

## BOARD RULING — directional earnings scaling (2026-08-23) — UNANIMOUS
Voices: risk lens (Taleb/Thorp/Sinclair), edge lens (Simons/Shaw/Sosnoff/Brandt), Gro. GAI deferred
(free key 429'd this session) → captured at build-time on the DIFF, where Gro+GAI are mandatory anyway.
All three converged:
1. **Pre-print directional scale-in: ALLOW-LONG-ONLY**, as a bounded tactical exception — NOT because it
   has proven edge (it does not; the ATM straddle prices the magnitude efficiently, direction is ~coin-flip,
   and the bot has no measurable per-name earnings-direction sample → Kelly says add ~0 without a measured edge).
2. **SHORT side: BARRED (unanimous).** Adding to a short into a print is short-gamma / unbounded upside tail,
   unprotected through a naked print — the classic blow-up. QHM has no short support today; treat as separate
   net-new infra, deferred. (This DIRECTLY conflicts with Rafael's originally-stated "inverse for shorts" — surface it.)
3. **The SUPERIOR expression is POST-print re-add (PEAD), not pre-print.** The bot already does ~80% of this
   (de-risk + re-add after the vol crush). Post-print has a documented, measurable edge and zero binary tail;
   pre-print dip-add is the strictly inferior, capped, long-only add-on. Make post-print the primary build.
4. **Guardrails (unanimous):** bound the add by the ATM-straddle implied move σ_E (already computed in
   data/gex.py) — ladder dip-depth in σ_E units, not arbitrary %; CAP AT TARGET weight (no overshoot into a
   print; the 1.375× overshoot is strength/non-earnings only); single-event loss ≤ ~3% of equity → a notional
   cap that SHRINKS as implied move rises (skip names with implied move > ~12%); ONE name in earnings-add
   posture at a time / respect the GE+GEV correlation cap; the position-SIZE cap — NOT the 7% kill switch — is
   what actually contains an earnings gap (the kill switch fires the NEXT day, after the gap). Unify scale-in
   and de-risk on fill-ratio z=weight/target: scale-in only when z<1, harvest only when z≥1 — they can never
   fire together.
5. **RISK-PATH posture flip on EXISTING LIVE CODE** — `_maybe_dip_add` (7-day pre-earnings blackout, L1636/1669),
   `_maybe_earnings_trim` (L1983), `_maybe_enter_earnings_hold` (5-day stop-cancel, L1337). Gets the mandatory
   cold board + masked-loss seat + Gro/GAI on the DIFF at build time; NOT a same-day flip.
6. **ROOT-CAUSE FLAG (check FIRST):** "NVDA at 1 share vs 20% target" is likely the ENTRY tranche ladder
   under-filling to target, NOT the absence of a scale-in. Fixing the entry build may be the actual, cheaper,
   non-binary fix — the scale-in could be treating a symptom. Verify the tranche ladder (`_TRANCHE_DAYS`,
   Day-3 reconfirm) before building the earnings scale-in.

## RAFAEL DECISION (2026-08-23) — OVERRIDE the short-side bar (short side IS in scope, judiciously)
Rafael overrode the board's unanimous "bar the short side": "Some companies are extremely overvalued and
have historical red flags like debt, lack of earnings, and other clear speculative reasons why the stock is
higher. I'm fine being more judicious in when shorting into earnings is deployed but removing it all together
is also a mistake." He is the sole authority (G01) — short side IS in scope. His refinement addresses the
EDGE (a fundamentally-justified short is higher-conviction than a coin-flip); it does NOT change the unbounded
TAIL, so the RISK control must. Design consequence — the short-side earnings scale-in ships with TWO gates:
1. **EDGE gate (Rafael's high bar):** deploy a short scale-in only on a strong fundamental short thesis —
   extreme overvaluation + hard red flags (heavy debt / debt issuance, negative or no earnings / negative FCF,
   speculative-only valuation). Derive the screen from data (13F, fundamentals, debt, revisions), not a static
   list. A short is NOT deployed on price/direction alone.
2. **TAIL/RISK gate (non-negotiable, does the protecting regardless of thesis):** an EXTRA-STRICT single-name
   short notional cap — tighter than the long side — sized so a catastrophic squeeze (model +50–100%) stays
   within the kill-switch envelope; ONE short-into-earnings name at a time; NO adds inside the final naked-print
   window; the position-size cap (not the kill switch) is the containment. Preferred future expression = a
   DEFINED-RISK options structure (put spread / long put) so the tail is bounded by construction — deferred:
   QHM is equity-only today (no option legs), so v1 short = capped naked short stock, options-defined-risk = v1.1.
The exact caps + the screen are set by the mandatory cold board + masked-loss seat + Gro/GAI on the DIFF at
build time (risk-path). Long side ships first; short side follows with its own risk-path gate.

## RAFAEL REFINEMENT (2026-08-23) — LIFT the 7-day pre-earnings dip-add blackout when the bull thesis holds
Current code: `_maybe_dip_add` is BLOCKED within 7 days of earnings (`_DIP_ADD_NO_ADD_DAYS_PRE_EARNINGS=7`).
Rafael: "If NVDA is dropping into earnings (there's a bearish engulfing on the weekly) but our QHM BULLISH
thesis holds, we SHOULD be adding into earnings — especially if the market is feigning weakness despite the
macro strength." So the pre-print long dip-add must be ALLOWED (not blocked) when ALL hold: (a) the QHM
bullish thesis is re-confirmed/fresh, (b) the dip is technical/noise (e.g. a weekly bearish-engulfing pullback)
NOT a thesis-break, (c) macro backdrop is supportive (market "feigning weakness despite macro strength" — a
risk-on-underneath read). This is exactly the ALLOW-LONG-ONLY pre-print scale-in the board approved — the
design must REPLACE the flat 7-day blackout with a thesis-and-macro-gated allowance, still bounded by the
σ_E-scaled ladder + cap-at-target + single-event-loss≤~3%-equity + one-name-at-a-time guardrails, and still
NO adds inside the final naked-print window. Concretely for NVDA (earnings 2026-08-26, currently 1 share naked,
PENDING_EARNINGS): under this refinement, a bullish-thesis-intact weekly-engulfing dip into the print would
QUALIFY for a capped add toward target — where today it is flat-blocked. Requires: the thesis-freshness signal,
a weekly-pattern/structure read (ties to 10-pt #3 defined-range TA), and a macro-supportive read (MRI/regime).
Risk-path → full board + masked-loss on the diff. NOTE the entry-ladder-underfill root cause still applies
(check FIRST): NVDA may be at 1 share because the ENTRY build never reached target, independent of this.

## Relationship to the other QHM artifacts
- The **status/monitoring report** (scripts/qhm_report.py, Sundays 9am PT) stays as-is (see
  [[project-qhm-reports]]). This pipeline is the SEPARATE research+execution engine.
- Both post to Slack → both must follow rules/slack_format.md (SLK01–SLK15).

## Next steps (in order)
1. Ship the immediate Slack-legibility fix (spec + status-report Block Kit migration) — independent,
   ready, unblocks readable output for this pipeline later.
2. Verify data-source availability (13F / raises / downgrades / debt via FMP or a new feed).
3. Run the RISK-PATH BGG design pass on this full pipeline (cold board + masked-loss + Gro/GAI).
4. Bring Rafael the complete design (esp. the execution wiring + fail-safes + scheduling mechanism) for
   approval BEFORE any code.
