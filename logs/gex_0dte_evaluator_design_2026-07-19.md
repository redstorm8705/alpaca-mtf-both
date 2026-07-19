# GEX / 0DTE Accuracy Audit + Recurring Evaluator — ALIGNED DESIGN

**Date:** 2026-07-19 (interactive, Rafael present)
**Status:** BGG ALIGNED — Board 4/4 + Gro APPROVE + GAI APPROVE. Awaiting Rafael APPROVE/REJECT.
**Trigger:** Rafael request — audit GEX + 0DTE recommendation accuracy against actual SPY OHLC;
build a recurring evaluation; apply dynamic sourcing; BGG to recommend scope (Rafael's lean: "full").

---

## Voices convened

| Voice | Seat / lens | Vote |
|---|---|---|
| Board 1 | Emanuel Derman — quant architecture / model risk | APPROVE NARROWER SCOPE |
| Board 2 | Marcos López de Prado — statistics / overfitting / CPCV | APPROVE NARROWER SCOPE |
| Board 3 | Sinclair + Sosnoff + Nathan — 0DTE / options / IV | APPROVE NARROWER SCOPE |
| Board 4 | Charity Majors + Wes McKinney — observability / data integrity | APPROVE NARROWER SCOPE |
| Gro | Groq llama-3.3-70b, HFT staff-engineer framing | APPROVE NARROWER SCOPE *(after counter-prompt round 1)* |
| GAI | Gemini 2.5-flash, Head of Quant Engineering framing | APPROVE NARROWER SCOPE *(after counter-prompt round 1)* |

All four board seats read `data/gex.py` (537 lines) and `options_scanner.py` (1905 lines) in full
plus the git history and raw audit JSON. Gro and GAI did not have code access on the first pass and
were counter-prompted with the board's evidence per the DISAGREEMENT PROTOCOL (not re-rolled).

---

## FINDING 1 — flip=694 is a CLOSED pre-fix artifact, not a live defect

Two independent proofs:

1. **Git timestamps.** `aad518a` ("local nearest-spot flip") committed **2026-07-15 10:02:11 -0700**.
   The `flip: 694.0` record in `logs/gex_daily_audit_2026-07-15.json` is timestamped
   **06:37 AM PT the same day** — 3h25m BEFORE the fix.
2. **Arithmetic impossibility.** `data/gex.py:88` `_FLIP_WINDOW_PCTILE = 0.05`; the loop at
   `data/gex.py:387-390` hard-skips strikes outside `[spot*0.95, spot*1.05]`. At spot ≈ 752 that
   band is `[714.4, 789.6]`. 694 is outside it — the current code cannot emit it under ANY data
   conditions.

Corroboration: 7/15 SPY flips read 689–702 from 06:37 to 11:13 PT, go `null` at 11:20/11:23 (the new
fail-to-UNKNOWN gate engaging), then read 755.0 at 11:30 PT. The regime break lands exactly at deploy.

**The defect was not one day.** Distinct SPY flips logged against a 748–755 spot week:
7/13: 640–690 · 7/14: 670–690 · 7/15: 685–702 then 755 · 7/16: 725–760 · 7/17: 728–731.
694 was the *center* of a three-day regime in which every emitted flip was 8–14% below spot.
Nobody was looking at the distribution, only at individual values.

## FINDING 2 — THE LIVE DEFECT: the fix produced a TAUTOLOGICAL flip (worse, not better)

`data/gex.py:384-397` computes *the strike K at which a running sum of signed gamma-dollars over
strikes ≤ K changes sign, **with spot held fixed***. A gamma flip level is a different object:
the hypothetical spot S\* at which net dealer gamma, **repriced at S\***, equals zero —

    G(S*) = Σ_k sign_k · Γ_k(S*, K_k, T_k, σ_k) · OI_k · 100 · (S*)²

solved for the root in S\*. The code never varies `spot` (passed once at line 272, used at 316/320/323).

Under BS, Γ(S₀,K) peaks at K ≈ S₀, so the sign change occurs near spot **by construction**, and
`_best_dist` (line 393) then explicitly argmin-selects the crossing NEAREST SPOT.

Observed post-fix: SPY flip = **755.0, 755.0, 755.0** vs spot 754.68. 7/17: 728/731/729/728 vs a
742–747 spot.

**The 7/14–7/15 fix replaced a visibly-absurd answer with an uninformative one that looks plausible
forever.** 694 was caught in a day; a flip that always prints ≈ spot will never be caught by inspection.

## FINDING 3 — other confirmed live defects

- **Per-expiry collapse** (`gex.py:327`): `strike_gex` is keyed on `strike` alone while
  `_expiry_range()` spans today→Friday. Since Γ ∝ 1/√T, the 0DTE leg carries ~2.8× the 2DTE gamma and
  ~7× the 7DTE — the label is a 0DTE reading wearing a weekly label's name, and its character CHANGES
  ACROSS THE WEEK (5 expiries Monday → pure 0DTE Friday). This silently destroys any evaluator that
  pools weekdays. Worse: the highest-gamma 0DTE contracts carry **T+1-stale OI** containing none of
  today's flow, and freshly-listed same-day strikes with OI=0 are silently dropped (line 168) —
  a hole in the grid exactly where the flip is searched.
- **Chain censoring, 67.4%** (SPY 7/15 11:27 PT, `contracts_fetched=755`): zero_bid 227 (30.1%),
  wide_spread 152 (20.1%), no_iv 130 (17.2%). The filters delete ATM contracts preferentially —
  and gamma is maximal ATM. The estimator is blind exactly where the signal lives.
- **`_MAX_CONTRACT_PAGES = 2`** (`gex.py:67`) truncates at 2000 contracts without recording that it
  truncated. OCC symbols sort calls before puts, so truncation drops PUTS first — and puts carry
  `sign = -1.0` (line 322), biasing `net_gex` positive.
- **`raw_gex_m` is ~100× mislabelled.** Line 323 omits the conventional per-1%-move `× 0.01`. The
  logged 595411.8 "M" reads as $595 BILLION of SPY gamma. Does not move the sign-based label, but
  every absolute GEX number in the audit history is uninterpretable — and any future threshold
  derived from it would be fit to a fiction.
- **`_get_spot`** (`gex.py:102`) has no `feed` param → resolves to IEX, thin and stale-prone, with no
  cross-check. A bad spot poisons IV, gamma, ATM count, capture window AND the flip window
  simultaneously — every quality gate is computed relative to spot, so a wrong spot passes all of them.
- **`"—"` string sentinel** for missing gamma/vega (`options_scanner.py:1073, 951`) makes the column
  dtype `object`; `isna()` returns False for a missing value. Rendering concern written to disk.

## FINDING 4 — the 0DTE gap is a RETENTION gap, not a logging gap

`options_scanner.py:1173-1175` already writes the full rec list (strike, expiry, premium_mid, bid,
ask, iv, delta, theta, oi, volume, score, vrp, isk, vix_tertile, cost_pct) atomically to
`logs/options_scan.json` — then `os.replace` **overwrites it every 15 minutes**. The system computes
the wide event and destroys it. Fix cost: ~12 lines (append before replace), not a new emitter.

`logs/options_scan.json` is dated **Apr 26**; `logs/dte_prev.json` **Jun 5**; exactly one
`options.html` exists. **There is no archive — reconstruction has no substrate.**

## FINDING 5 — statistical reality (López de Prado seat, unchallenged)

- Effective N ≈ **1.2 observations/trading day** (OI is T+1-constant intraday, so the 26 snapshots are
  not 26 observations; SPY and QQQ are highly correlated).
- Label serial correlation ρ ≈ 0.7 → variance inflation factor **5.67**.
- 10-ppt edge, α=0.05, 80% power, Šidák over 24 hypotheses → 340 effective → 1,927 nominal →
  **≈960 trading days (~3.8 years)**.
- Minimum Track Record Length for a Sharpe claim ≈ **1,130 sessions**. Against the False Strategy
  Theorem hurdle at N≈100 prior trials (E[max SR] ≈ **1.27**), a true Sharpe ≤ 1.2 is
  **unestablishable at any sample size**.
- **~30 tunable knobs + 4 structural variants** across the two files → N=100 is a generous
  UNDERESTIMATE of the trial count.
- **Structural fix that changes the answer:** score the vol-regime claim at **15-minute resolution**
  against the SUBSEQUENT 15-min realized range, with AFML Ch.4 average-uniqueness weights. That is
  26×/day already being generated and discarded as a scoring surface — compressing validation from
  ~4 years to roughly **37–90 sessions**. Highest-value single design decision in the review.

---

## ALIGNED SCOPE — build in this order, stop at each gate

**S0 — Demote GEX to display-only, TODAY.** `GEX_EDGE_MULT` → 1.0 for all labels; MIN_SCORE bump → 0.
Dashboard card relabelled **SHADOW — NOT IN SIZING**. 0DTE column gets **UNVALIDATED — NOT FOR
EXECUTION**. Rationale: `get_gex_regime()` feeds `kelly.py`'s edge multiplier, so a mis-specified
edge is a LEVERAGE error applied multiplicatively across the ENTIRE book, not just GEX-motivated
trades. Three consecutive days of arithmetically-impossible flips fed `kelly.py`. Errors in bet
sizing are not symmetric with errors in signal generation (AFML Ch.10).

**S1 — Forward-only structured logging.** Append the already-built rec dict to
`logs/recs_0dte.jsonl` BEFORE `os.replace` destroys it. `null` replaces the `"—"` sentinel. Every
record carries **`code_version` (git SHA) and `config_hash`** — the fields whose absence made the
7/13–7/15 incident undiagnosable. Companion `logs/zdte_rejections.jsonl` (the denominator — without
it every hit rate is survivorship-inflated). Outcomes to a SEPARATE `logs/recs_0dte_outcomes.jsonl`
written by a separate job, joined on `rec_id`, so no code path touches a feature and its label in one
execution. **NO reconstruction** — permitted only as telemetry flagged `provenance: "reconstructed"`,
hard-refused by the evaluator when incrementing n.

**S2 — Write-time invariants in `_compute_gex`.** Start with the FREE tautological check —
`_w_lo <= flip <= _w_hi` — which has no threshold in it, needs no calibration, and **alone would have
caught 694 in 15 minutes instead of 3 days** by saying "the deployed binary does not contain the
window fix." Plus: spot cross-check vs the prior snapshot and the 15m bar close (fatal);
`label==UNKNOWN ⇒ raw_gex null` (a populated number under an UNKNOWN label is unmarked quarantined
data); fixed-point self-check `G(flip) ≈ 0` (fatal); call/put composition ratio; intraday flip
dispersion vs realized range. Graded severity: **fatal → refuse to emit the field (`UNKNOWN`), never
raise** (an exception in a 15-min cron is a silent data gap, strictly worse than a flagged bad
record); **degrade → emit with `quality_flags` that travel with the record permanently**; **alert →
first occurrence per session per invariant** (26 snapshots × 2 symbols × 8 invariants = 416 potential
pages/day; alert fatigue is how this gets ignored).

**S3 — Offline discriminating checks BEFORE any new GEX code.** (a) **Regress emitted `flip` on
`spot` across post-fix records — slope ≈ 1 and R² ≈ 1 proves the flip is a rescaling of spot carrying
zero independent information.** Needs no new instrumentation; highest-value single check. (b) Three
synthetic chains: calls-only (a true flip cannot exist → correct output `None`), symmetric OI (flip at
the OI centroid), put OI 3× call (flip well below spot) — the current function returns ≈ spot in all
three. (c) Per-expiry decomposition (prediction: nearest expiry > 70% of `total_abs` on Monday →
100% Friday). (d) Moneyness × type survival table. These determine whether `_compute_gex` is
repairable or must be rewritten as a proper root-find in S\*.

**S4 — Sealed evaluator.** Computes and stores everything; **renders exactly one string until its
pre-registered n is met**: `VERDICT: INSUFFICIENT EVIDENCE — n=X/Y, clock started <date>,
config_hash <8>`. Build it sealed rather than deferring it — deferring guarantees someone runs the
analysis informally instead. Scores the **vol-regime claim only** (flip-as-magnet is dropped from
scope: it would instrument an artifact). Nulls are the hard ones: **persistence and the ATM straddle
`exp_move`**, not a coin flip; for 0DTE, **the contract's own delta** (~0.30–0.35 under risk-neutral
pricing), not 50%; and **beat −12.5%/trade**, not zero, since `BAS_MAX_0DTE=0.25` admits a 25%
round-trip. Admissible metrics at low n: paired Wilcoxon, Clopper-Pearson intervals (never Wald),
and data-quality telemetry. **Mechanically un-emittable below n:** Sharpe, max drawdown, IC,
calibration curves, any per-symbol breakdown.

**S4b — The config-hash tripwire.** On evaluator startup, if the live `config_hash` differs from the
registry's, **`n_accrued` resets to 0 with NO override path in the code** — not a flag, not an env
var. Changing a threshold means starting over; that is the true statistical cost, made visible at
the moment of the decision. `gex.py` shows **four specification revisions in twelve days** (07-03,
07-06, 07-14, 07-15) — each one already restarted the evidence clock and nobody noticed.

**S5 — Deadman, off-box.** The evaluator's final act is an HTTP ping to an external dead-man's-switch
alerting on ABSENCE within 90 minutes. An on-box watchdog dies with the box, the disk, the cron
daemon and the network — all four of which are the failure modes being guarded. Plus
`logs/eval_runs.jsonl` written in a `finally` on success AND failure, and a volume invariant
(`n_scored > 0` on any open-market day). **Precedent: this system already lost 67 days to exactly
this failure mode** — `gex.py:16-18` records 1,269 consecutive invalid snapshots (2026-04-27 →
07-03) discovered by a human reading code, not by any alarm.

**S6 — Q4 dynamic sourcing: DEFERRED to a second sequence gated on ≥60 CLEAN sessions.** The
standing no-static-thresholds rule is right, but `gex_history.jsonl` today contains ≥3 code versions
with no version field, a ~100×-mislabelled magnitude, an 84%-UNKNOWN day, and a probably-one-sided
truncation. **Deriving thresholds from it would fit parameters to the defects.** Two hard caveats
when it does arrive: (i) a re-derived threshold runs 12 trials/year and must be ADDED to the FST
trial count — dynamic sourcing makes the multiple-testing problem worse, not better; (ii) deriving a
data-quality gate from the data it gates is CIRCULAR — `_MIN_CAPTURE_RATIO` is a deliberate
documented exception. Note `_get_vix_tertile` (lines 597-642) **already does this correctly** and is
the in-repo template. Prerequisite: raw per-contract input logging
(`logs/gex_raw_YYYY-MM-DD.jsonl.gz`) — without it not one threshold in the inventory is derivable,
and the R3/R4/R5 root-cause tests are dead letters.

---

## EXPLICITLY REJECTED (unanimous, all 6 voices after counter-prompt)

1. **Retroactive reconstruction as an inferential input.** No substrate exists; and
   `_get_vix_tertile` downloads `period="2y"` ending TODAY (`options_scanner.py:618`, q33/q67 at 626)
   — reconstructing 7/15's tertile uses VIX from 7/16 onward, injecting look-ahead into the
   **admission criterion** (line 1032), not merely a feature. Plus unrecoverable survivorship
   (the denominator was never written) and a regime break on 2026-07-13 (VRP premium-selling →
   directional long premium). Cost of starting today-forward: **~4 sessions against a 170+ floor.**
2. **Evaluator auto-degrading the signal weight.** Gro originally recommended it; withdrawn on
   counter-prompt. Auto-degrade at n≈40 is a coin flip wired to the position sizer, and is itself a
   selection procedure that inflates the trial count. **Alert only; the weight is a config constant a
   human edits, with the edit logged.**
3. **Building a flip-level / pin-magnet accuracy evaluator now.** It would instrument an artifact
   (Finding 2). Dropped from scope until `_compute_gex` is re-specified.
4. **"Staged-ACTIVE" as a state.** Live enough to move P&L, informal enough that nobody counts its
   retunes as trials — that combination is how N reaches 100 unnoticed. Two states only:
   zero-weight-and-accruing, or live-with-every-retune-counted.

---

## SPY ACTUAL OHLC — week of 2026-07-13 (Alpaca T1, authoritative)

| Date | Open | High | Low | Close |
|---|---|---|---|---|
| Mon 7/13 | 752.62 | 753.91 | 748.06 | 749.13 |
| Tue 7/14 | 750.93 | 753.31 | 748.71 | 751.94 |
| Wed 7/15 | 754.23 | 755.54 | 750.25 | 754.77 |
| Thu 7/16 | 752.80 | 754.55 | 747.88 | 750.87 |
| Fri 7/17 | 742.17 | 747.25 | 740.80 | 743.28 |

Note 7/17: SPY gaps down from a 750.87 close to a 742.17 open — **and that is the day SPY goes
UNKNOWN on 21 of 25 snapshots.** The signal did not warn; it withdrew. Its errors are maximal
exactly when spreads widen, i.e. in stress, i.e. when sizing matters most.

---

## NEXT ACTION

Rafael APPROVE / REJECT the aligned scope above. On APPROVE, S0 (demote to display-only) ships first
through the full mandatory patch sequence + FINAL PRE-SHIP Gro+GAI gate on the exact diff.
