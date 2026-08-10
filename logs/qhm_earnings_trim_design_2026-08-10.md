# QHM Profit-Target + Earnings-Runup De-Risking — Design (BGG-aligned 2026-08-10)

**Status:** DESIGN ALIGNED (Board + Gro + GAI + Rafael). No code shipped yet. Implementation is a
separate build behind the Feature Design Protocol + a full **BoD+AB** board vote on exact thresholds
(QHM pick/exit policy is BoD+AB only, per CLAUDE.md). Durable-synced per the DURABLE SYNC RULE.

## Problem
QHM (quarterly hold) positions (NVDA, GOOGL, GE, GEV, LLY) carry a protective **stop** but **no
profit-taking target** — they are held only to an equity-allocation cap. Two gaps:
1. No systematic profit target for a multi-month conviction hold.
2. No handling of the **earnings-runup** case: a hold up +15–40% approaching its earnings print,
   where a post-earnings gap-down / IV-crush can erase the accrued gain.

Surfaced 2026-08-10 by Rafael ("there should be a target for QHM" + "this goes into the runs-up-
into-earnings discussion"), triggered by the dashboard showing "—" for QHM stop/target.

## BGG SETTLED items (Gro, GAI, BoD-lens[Simons/Shaw/Taleb], AB-lens[Thorp/Sosnoff/Sinclair/
Nathan/Brandt/Douglas] all converge — NOT open questions):
1. **KILL "tighten the stop into earnings" — it is fake protection.** An earnings gap-down opens
   *below* the stop and fills under it; a stop cannot protect against an overnight jump. Into a
   print, **share reduction does ~100% of the protection; the stop does ~0%.** (Both board seats
   flagged this as the #1 design flaw.)
2. **Never trim to zero — always keep a CORE.** The reason to hold a compounder is the fat right
   tail (guide-up re-rating) + post-earnings drift (PEAD). Full exit kills the thesis.
3. **Scale the trim by the stock's IMPLIED earnings move (ATM straddle), not runup% alone.** A
   3%-implied-mover and a 15%-mover at the same +20% runup are not the same risk.
4. **Drop break-even-stop-at-+2R; use an ATR / structural TRAILING stop** on the remainder — a
   break-even stop whipsaws a multi-month hold on normal volatility.
5. **Real "scared-print" protection = OPTIONS (collar / put-spread), not selling the compounder.**
   SCOPING FACT (verified): QHM is **equity-only today** (state carries stops/tranches, no option
   legs), so the options-hedge path is a SEPARATE FUTURE capability — not in the first build.
6. **Precedence + per-window trim cap.** The R-ladder trim, the earnings trim, and the allocation-
   cap trim must not silently stack into an unintended 50–70% dump. Explicit ordering + a rolling-
   window ceiling required.
7. **Standardize on R** where possible (mixed R-vs-% coordinate systems conflict).
8. **Add a FUNDAMENTAL thesis-invalidation exit** distinct from price (guide cut / margin collapse /
   thesis break) — the correct earnings exit for a conviction hold is fundamental, not a price stop.
9. **PEAD re-add path:** if it gaps up and holds a confirmation level post-crush, re-add toward
   target once IV has normalized (recaptures the documented post-earnings drift).

## RAFAEL'S DECISION (2026-08-10): **Balanced trim, keep core.**
Default earnings policy = trim a partial scaled by BOTH implied move and runup, ALWAYS keep a core,
ATR trail on the remainder, re-add post-crush. (Chosen over pure "lean conviction / hold the core"
[BoD] and pure "lean capture / systematic aggressive trim" [AB] — both accept this middle.)

## PROPOSED DEFAULT POLICY (to be ratified by the BoD+AB vote at build time)
**A. Non-earnings profit target (layered R-multiple partial banks):**
- Trim ~20–25% at **+2R**; trim ~20–25% more at **+4R**. Keep the rest on an ATR/structural trail.
  (NOT stop-to-break-even.)

**B. Earnings-proximity de-risk (evaluated ~3 trading days before the scheduled print):**
Trigger only if unrealized gain ≥ +15%. Trim fraction scaled by implied move AND runup:
- implied move ≤8% and runup +15–25% → trim ~33%
- implied move >8% OR runup >25% → trim ~50%
- implied move >12% OR runup >40% → trim ~66%
- ALWAYS retain a core (never 100%). Raise the remainder's stop to a **structural/ATR** level (for
  post-event protection of the remaining shares — explicitly NOT sold as event protection).

**C. Allocation-cap trim:** if market value >120% of target allocation → trim back to ~110%.

**D. Precedence:** stop-breach (paramount) → earnings de-risk (time-sensitive) → R-ladder →
allocation-cap → valuation. Rolling-window cap on total trims (~50–60%/week) unless the allocation
cap alone requires more.

**E. PEAD re-add + fundamental kill-switch** as in SETTLED #8/#9.

## IMPLEMENTATION SCOPE (next build — NOT done)
- **Earnings dates:** FMP earnings calendar (T2, `data/fmp_client.py` — `get_earnings_calendar`
  already exists) → per-QHM-symbol next earnings date.
- **Implied move:** ATM straddle from the option chain (Alpaca options data / `options_scanner`
  path) for each QHM symbol near the print. Fail-safe: if no options data, fall back to runup-only
  scaling (documented degraded mode).
- **Trim mechanics:** `execution/quarterly_hold_manager.py` — partial close (qty-bounded), keep-core
  floor, ATR trail on remainder, precedence + window cap. Reuse the existing tranche/partial infra.
- **ATR:** already available in the intraday path; wire an ATR source for QHM.
- **Gates:** Feature Design Protocol first; full **BoD+AB** vote on the exact thresholds/fractions;
  then the standard patch sequence + the now-ENFORCED bias & adversarial gates on the diff.

## SEPARABLE QUICK FIX (independent of the above)
Dashboard D1: `generate_dashboard.py` L564-581 reads stop/target only from `trade_log["open"]`, so
QHM holds (tracked in `data/state/quarterly_holds.json` with a real `stop_price`) render "—" for a
stop that EXISTS (NVDA 211.06, GOOGL 338.62, etc. — verified live at Alpaca). Fix: for a QHM symbol,
read `stop_price` from `quarterly_holds.json` (already loaded as `_qh` at L548). Target/score stay
"—" until this feature ships (tag them "n/a" so "—" reads as not-applicable, not missing). Small
patch-sequence fix on a gated 982-line file — separable from the strategy build.
