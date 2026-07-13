# QHM v2 — Position-Management Framework Design (2026-07-11)

Rafael's mandate (2026-07-11): "The report should inform the config. Names may change but we keep adding
to previously-identified holds (NVDA, GOOGL) — conviction persists. Define the post-earnings config and the
take-profit-tranche / buy-more-on-weakness logic tree." Board + Gro + GAI convened. This doc = the design.

---

## PART 1 — DIP-ADD RULE ("buy more on weakness") — DECIDED, board 4-0 YES/MODIFY

Gro ✓ · GAI ✓ · cold seat Thorp/Asness ✓ · cold seat López de Prado/Taleb ✓.

**Why:** leaving NVDA at 7.6% (1 sh) vs its board-approved 20% target is a Kelly SIZING ERROR — ~67% of the
edge unutilized. Completing a high-conviction, board-approved hold on weakness (value/DCA) is correct and does
NOT contradict the initial-build day-3 re-confirm (different regime: initial-entry validation vs completing a
validated conviction). The under-build is "execution debt" (limit tranches 2/3 never filled), not a thesis change.

**THE RULE (with the key modification both cold seats demanded):**
- **Trigger measured from the ENTRY/tranche-1 price, NOT rolling prior close.** "-2% from prior close" was
  rejected as too frequent — NVDA moves ±2% ~40-50% of days, so it would fire on oscillation/noise, not a real
  dip, and complete the position in days regardless. Use **price ≤ tranche1_entry × (1 − 0.03)** (a fixed
  reference band), confirmed on a 2nd-bar close (no wick fills).
- **Only when the position is UNDER its target weight.** Never fires at/above target.
- **Hard cap at target weight** — never over-build (accept minor over-build only from share-lumpiness; floor 1 sh/add).
- **Max ~3 dip-adds per position per quarter** + **≥2 trading days (≈max 1/week) between adds** (anti-oscillation).
- **No adds in the final ~7 days before the earnings-exit** (don't compound into the event; let it ride or exit).
- **HARD STOP-ADDING FLOOR at −12% to −15% from tranche-1 entry** (LOAD-BEARING, non-negotiable): below this the
  market may be re-rating the thesis → STOP adding, escalate to the board for a conviction re-vote. This is the
  line between "cheap" (add) and "broken" (don't catch the knife).
- **KEEP the existing day-3 re-confirm** for the initial build tranches (separate control; not disabled).
- Sizing: `per_dip_qty = max(1, round((target_notional − current_notional)/price))`, capped so it never exceeds
  target by more than ~1 share.

**IMMEDIATE ACTION (Thorp/Asness seat, strong):** NVDA is 2 weeks of execution debt with current conviction —
do a **one-time catch-up**: buy ~1 share on the next qualifying dip (or at next open if already below the
tranche-1 band) to move ~7.6%→~16-17%, then the dip-rule governs the rest. GOOGL is already ≈ target (no action).

---

## PART 1 — FINALIZED MAGNITUDES (2026-07-12, board 2 cold seats + Gro + GAI; Rafael APPROVED)

Live state at decision: equity $2,751; NVDA 1sh @avg $199.07 (tranche1 $199.02), now $208.54 (+4.8%), =7.6%,
target 20%; GOOGL 1sh @avg $362.61, =12.9%, target 15% (≈target, no action).

- **FORK 1 — Rung B concentration ceiling = `1.375× target` (27.5% of equity).** Votes: Gro 1.5× · GAI 1.375×
  · seat-1 (Thorp/Taleb/Asness) 1.375× · seat-2 (LdP/Simons/Brandt) 1.25× → median/plurality **1.375×**. For
  NVDA (20% target) → 27.5% ceiling ≈ 3 whole shares at $208 (round DOWN to stay under the ceiling).
- **FORK 2 — Hard stop-adding floor = `−15% below FIRST-ENTRY (tranche1) price`.** 3–1 (seat-2 dissent −12% on
  Brandt price-structure). NVDA tranche1 $199.02 → STOP adding below **$169.17**, escalate to board for a
  conviction re-vote.
- **FORK 3 — NVDA one-time catch-up = `+1 share` → ~15.2%.** UNANIMOUS 4–0. Reject +2 (→22.7% over-shoots the
  20% target using a chase-priced entry above cost avg). Completes execution debt partially; the dip ladder
  finishes the rest at better prices.
- **FORK 4 — two-anchor design CONFIRMED, AMENDED: add a HARD CAP enforced pre-fill.** BOTH board seats
  overrode Gro/GAI's "no extra cap." Arithmetic (seat-2): because the −5% Rung-B trigger is measured off a
  FALLING cost average, a grinding decline re-arms it repeatedly — a 1-share base can stack to ~5 shares ≈
  **33.9% of equity BEFORE the −15% price floor fires** (blowing through even a 1.5× ceiling). The price floor
  caps DEPTH (thesis-broken?), not WIDTH (how much capital in one name); on a lumpy account WIDTH binds first.
  → The 27.5% ceiling MUST be a **hard pre-fill check on every Rung-B add** (not a target guideline), PLUS a
  **hard max-shares-per-name cap recomputed at the quarterly review** (`floor(ceiling_weight × equity /
  price_at_review)`), checked before every add.
- Brandt dissent LOGGED: he objects to the PREMISE (any structured averaging-down) even as the magnitudes are
  set; the counter that carried the board = pre-committed, capped, gated ladder on a validated conviction name
  (not a discretionary rescue), with price floor (F2) + hard capital cap (F4) + anti-oscillation controls.

**Anti-oscillation (already decided, unchanged):** max 3 dip-adds/quarter; ≥2 trading days between adds; no adds
in the final ~7 days before the earnings exit; keep the day-3 re-confirm for initial tranches. Rung A add caps
at target weight; Rung B may exceed target up to the 27.5% ceiling, add size ≥ existing position size.

**BUILD (next, gated — full patch sequence on execution/quarterly_hold_manager.py + new config.py constants):**
config: `QHM_DIP_ADD_RUNG_A_PCT=0.02`, `QHM_DIP_ADD_RUNG_B_PCT=0.05`, `QHM_DIP_ADD_CEILING_MULT=1.375`,
`QHM_DIP_ADD_STOP_FLOOR_PCT=0.15` (below tranche1), `QHM_DIP_ADD_MAX_PER_QUARTER=3`, `QHM_DIP_ADD_MIN_DAYS_BETWEEN=2`,
`QHM_DIP_ADD_NO_ADD_DAYS_PRE_EARNINGS=7`. Logic: dip-add evaluator in the weekly check + a one-time NVDA catch-up
(+1 share, tier="qhm"). NOTE: the NVDA catch-up is a LIVE paper order — needs Rafael's explicit go before it fires.

---

## PART 2 — QHM v2 FRAMEWORK (Gro + GAI designed; needs Rafael's forks)

### 2A. REPORT → CONFIG pipeline
**Rec: validated transform with a diff/approval step (NOT a blind direct write).** The research report is the
single source; a transform emits a *proposed* quarterly_holds_config.json, diffs it vs live, and it ships only on
approval (git-PR-style). Report must carry per name: `ticker, conviction_level, target_weight, entry_strategy
(tranche_days / limit), earnings_exit_date, persistence_flag, post_earnings_strategy, dip_add params,
take_profit_strategy, fallbacks`. Fixes today's stale-report-vs-config divergence.

### 2B. PERSISTENT CONVICTION BOOK (names carry across quarters)
Per-name `conviction_state`: **ACTIVE_PERSISTENT** (carries forward, keeps building), ACTIVE_QUARTERLY (this
quarter only), CONVICTION_DOWNGRADE_TRIMMING (→ trim to zero), REMOVED. Target weight carries forward. On a new
quarter: persistent names are UPDATED (not dropped) if still in the report; new picks added; names dropped from
the report (or persistence_flag→false) go to DOWNGRADE_TRIMMING → sell to zero (whole-share, opportunistic).
→ NVDA/GOOGL = ACTIVE_PERSISTENT: they keep being added to across quarters while conviction holds.

### 2C. POST-EARNINGS config (state machine)
Config-driven `post_earnings_strategy` per name:
- **HOLD_THROUGH** — highest conviction: don't exit before earnings at all; ride through (removes re-entry
  complexity). Candidate for NVDA/GOOGL if Rafael's conviction is that strong.
- **AUTO_REENTER** — exit before earnings, wait a 1-2 day cooling-off, auto-rebuild IF conviction persists AND a
  safety-net holds (e.g. don't re-enter if the stock gapped >15% down on the print).
- **MANUAL_RECONFIRM** — exit, then require a fresh board/PM reconfirm before rebuild (default for lower conviction).

### 2D. TAKE-PROFIT tranches (scale-OUT) — configurable, default debatable
Symmetric option to dip-adds. `take_profit_strategy`: **NONE (let winners run to the earnings-exit)** vs
FIXED_PERCENTAGE_TRIGGERS (trim N shares at +X%/+Y% from avg cost). Board split-ish: for a PURE long-term
conviction book, "let winners run" (NONE) is defensible; trimming helps a tiny account lock gains + recycle
capital into other names. **This is Rafael's call per the book's philosophy.**

### 2E. STATE MACHINE (per position)
PROPOSED → BUILDING → HOLDING ⇄ {ADD_ON_DIP, TRIM_ON_STRENGTH} → (PRE_EARNINGS_EXIT → POST_EARNINGS_WAITING →
{AUTO_REENTER→BUILDING | AWAITING_RECONFIRM | DOWNGRADE_TRIMMING})  OR  (HOLD_THROUGH stays in HOLDING).
CONVICTION_DOWNGRADE_TRIMMING → REMOVED. All transitions logged/auditable.

---

## FORKS FOR RAFAEL (the "ask-first" gate before building v2)
1. **Post-earnings for NVDA/GOOGL:** HOLD_THROUGH (ride the print) vs AUTO_REENTER (exit + rebuild)? Your
   "conviction is still there" leans HOLD_THROUGH or AUTO_REENTER.
2. **Take-profit:** do you want scale-OUT at all, or let winners run to the earnings-exit (NONE)? If yes, from
   what trigger (+% from avg cost) and how much?
3. **Report→config:** approve the validated-transform-with-diff approach (vs blind auto-write)?
4. **Sequencing:** ship the DIP-ADD rule + NVDA catch-up NOW (self-contained, fixes the live under-build), and
   build the full v2 framework (persistent book + post-earnings + report pipeline + take-profit) as the larger
   design after the Monday ownership wiring? (Recommended.)

Build order once approved: dip-add rule + catch-up (small, gated, RTH) → report→config transform → persistent-book
state field → post-earnings state machine → take-profit (if elected). Each its own Feature-Design gate.

---

## TIER DECISIONS — Rafael-answered 2026-07-11/12 (BGG-aligned)
- **T1 intraday universe/entry:** movers + confluence (as built). **Hold: NO time cap** — ride the RISING
  (ratcheting) trailing stop; exit only on stop-hit or target-hit. + SOFT stagnation guard (if profitable but
  flat/not-progressing ~3-5 days, tighten the trail or trim). Board-aligned, Rafael APPROVED.
- **T1↔all: ALL symbols tradable, period.** Remove the intraday-blocks-QHM gate — BUT only AFTER the never-sell
  floor is wired + tested (so a day-trade exit can never sell a QHM/F6 share). Floor-first sequence APPROVED.
- **T2 QHM dip-add — TWO rungs:** (a) -2% below COST AVERAGE (not prior close) → small add, capped at target.
  (b) -5% below cost average → LARGER/aggressive add that MAY go OVER the normal target weight (Rafael: "more
  aggressive than normal"), hard-capped at a concentration ceiling (board vote on exact multiple; ~1.25-1.5x
  target) + hard STOP-ADDING floor -12% to -20% below FIRST entry. Persistent across quarters (NVDA/GOOGL/LLY/GE
  carry forward; new picks don't negate old).
- **T3 Forever-6:** fire on market stress (SPY down ~1.5-2%+ AND VIX>25) AND stock worst-day-since-2020
  (bottom ~5% of its daily drops). Each F6 name needs HISTORICAL DATA (since 2022 bear) supporting its entries.
  Exit targets same as designed. + NEW: during market stress, the INTRADAY tier ALSO buys the confirmed bounce
  at support (bear-rally/reversion) aggressively — see Q3 build below.
- **Allocation 40-50/25-40/10-20:** SOFT guideline, NOT hard-enforced (tiny lumpy account). APPROVED.
- **Exits: each tier gets its OWN exit logic** (QHM/F6 NOT trailing-stopped like intraday). APPROVED. Rafael
  wants a per-tier exit-logic SUMMARY once built.

## NEW BUILD — SUPPORT/RESISTANCE + BUY-THE-BOUNCE (Rafael APPROVED w/ full-detail requirement) → improvement_queue
Rafael wants FULL details: historical data SINCE 2022 bear; WHERE charts are pulled; WHO/HOW support/resistance
levels are decided; MUST include SPY/SPX/QQQ GEX levels; and the data SHOWN on an HTML page (likely the existing
GEX section). Board-aligned method: S/R from confluence (prior swing highs/lows + volume-profile POC/HVN +
anchored VWAP + MA clusters + round numbers + GEX magnet/flip levels), scored by confluence; weekly-recomputed
per-symbol level table (new module + cache); intraday aggressive-buy gated by market-stress AND support-touch
AND confirmation (reversal candle / RSI oversold+divergence / volume-spike). Own Feature-Design gate when built.
