# Design record — Day-tier Track-A sizing off BUYING POWER (2026-09-08)

**Directive (Rafael, 2026-09-08, live session):** "size off buying power. We need to maximize the
margin and since it's a day trade the risk is lower." → make the day-tier Track-A size off buying
power instead of the equity slice, maximize intraday margin utilization, bounded by the safety
envelope. This is an APPROVED DIRECTION; the board's job is to BOUND the implementation (North-Star
mandate: use the edge, drop the capital-preservation reflex; a conservative ruling must affirmatively
justify itself against the growth goal), NOT to re-litigate the decision.

## Why this is not a new design (lowers risk)
config.py:781-782 ALREADY declares "Track A sizes off BUYING POWER (account trades ~4x margin); Track
B off settled CASH." The shipped code never implemented it — `day_tier_sizing.py` and
`day_trade_manager._gross_cap_ok` both size off `equity × DAYTRADE_ALLOC_PCT(0.15) × DAYTRADE_TRACK_A_PCT(0.65)`.
On 2026-09-05 the mismatch was papered over with "accept that names > ~$243/sh skip" (config.py:770-774).
Rafael's directive rejects that compromise and enforces the original documented intent.

## Current state (equity-based — the problem)
- Per-trade budget (day_tier_sizing.py): `equity × 0.15 × 0.65` = ~$243 at $2,503 equity.
- Aggregate Track-A gross cap (_gross_cap_ok): SAME `equity × 0.15 × 0.65` = ~$243.
- Tier kill (tier_kill_check:760): `−25% × (equity × 0.15)` = ~−$94.
- Result verified live 2026-09-08 10:26 ET: MSFT produced a real ENTER trigger, blocked ONLY by
  `size_ok=False` (budget $129.49 < 1 share @ $491.51). Any name > ~$243/sh can never size ≥ 1 share.

## Live account (2026-09-08, grounding the numbers)
equity $2,503.28 · buying_power(effective/DTBP) $4,169.77 · regt_buying_power $137.10 ·
cash −$2,366.18 · long_market_value $4,869.46 (MAIN book) · maintenance_margin $1,460.84 ·
multiplier 4 · dtbp_check "exit".
- `buying_power` ($4,169) is the intraday/effective pool the day-tier draws on; it RESETS daily and
  is net of the main book (self-balancing vs main-bot usage). `place_entry` already reads this exact
  field (day_trade_manager.py:560). regt ($137) is the overnight pool — the day-tier is flat-by-close
  so it never touches it (this is Rafael's "day trade = lower risk" made concrete).
- SHARED POOL (ANTI-SILO): the main bot and day-tier draw the same buying_power. Sizing off the LIVE
  field self-throttles the day-tier as the main bot deploys; question for the board is whether an
  explicit main-bot reserve is also needed.

## Proposed change (4 files, all risk-path)
1. **day_tier_sizing.py** — Track-A per-trade budget off buying power (new `buying_power` param):
   `track_budget = buying_power × DAYTRADE_TRACK_A_PER_TRADE_BP_PCT`; `notional = min(track_budget × conviction, track_budget)`;
   `shares = floor(notional / entry_ref)`. Track B stays cash (unchanged; B is OFF day-1).
2. **execution/day_trade_manager.py `_gross_cap_ok`** — aggregate cap `equity×0.15×0.65` →
   `buying_power × DAYTRADE_TRACK_A_BP_GROSS_PCT`. KEEP the cushion check (`BP − new_notional ≥ $650`) —
   the hard backstop, unchanged.
3. **execution/day_trade_manager.py `tier_kill_check`** — RE-BASE so the kill is not hair-trigger
   under BP-sized positions but still fires before the −7% account kill.
4. **run_day_tier.py** — fetch `buying_power` (broker.get_account().buying_power, same field
   place_entry uses) and pass it into compute_day_tier_size.

## Constants (proposed starting values — PROV, no day-tier P&L history to fit; recalibrate from live)
- NEW `DAYTRADE_TRACK_A_PER_TRADE_BP_PCT = 0.35` — per-trade Track-A budget as a fraction of BP
  (conviction scales DOWN). At BP $4,169: full-conviction $1,459 → MSFT($491)=2-3 sh. Bounds any one
  name; allows ~2 concurrent full-conviction names.
- NEW `DAYTRADE_TRACK_A_BP_GROSS_PCT = 0.55` — aggregate Track-A gross ≤ 55% of BP (leaves room for
  the main bot in the shared pool + the cushion). At BP $4,169 → ~$2,293 aggregate.
- KEEP `DAYTRADE_MAINT_CUSHION_USD = 650.0` — hard backstop, unchanged.
- RE-BASE tier kill: `−5% of EQUITY` (−$125 at $2,503), sits below the −7% account kill (−$175).
  Replaces the −$94 tier-budget basis (which is a −1.4% move = noise on a BP-sized book). Track-A
  sub-kill ≈ tier kill day-1 (Track B off).

## Open questions for the board + Gro + GAI (bound the implementation)
- Q1 AGGRESSIVENESS: per-trade 0.35 / aggregate 0.55 of BP — right balance of "maximize margin" vs
  leaving room for the main bot + cushion? Push higher (Rafael says maximize) or is 0.55 aggregate
  already the safe ceiling given the shared pool?
- Q2 KILL RE-BASE: is −5% of equity (−$125) the right tier-kill basis — coherent under the nested
  design (track sub-kill ≤ tier kill < −7% account kill) without hair-triggering on intraday noise?
- Q3 BP FIELD: confirm `buying_power` (effective/DTBP, resets intraday) is correct vs regt ($137);
  the tier is flat-by-close so DTBP is the right pool. Fail-safe if the field reads 0/absent?
- Q4 SHARED POOL: is sizing off LIVE buying_power (self-throttling) sufficient, or add an explicit
  main-bot BP reserve so the day-tier can't starve the main scan (dtbp_check="exit" → main-bot order
  rejects)?
- Q5 MASKED-LOSS / NAKED: does raising position size introduce any new naked-position, masked-loss,
  or cross-tier-netting risk? (All existing guards — structural stop, force-flat, cushion, tier kill,
  opposite-side co-hold guard — remain; only the SIZE magnitude changes.)

## BOARD + Gro + GAI review (2026-09-08) — 3 voices in, masked-loss seat pending
**Gro (APPROVE-WITH-CHANGES), GAI (REJECT→resolved), Sizing seat Thorp/Taleb (BLOCK on Q1/Q5).**
Strong 3-way convergence:
- **Q1 too hot:** all three say 0.35/0.55 is excessive for a ZERO-history edge (Gro 0.15/0.30,
  seat 0.20/0.35, GAI 0.10/0.20). 0.55 aggregate = a ~7.6% correlated gap hits the whole −$175
  account kill from the day-tier ALONE.
- **Q4 main-bot reserve — UNANIMOUS REQUIRED:** aggregate = min(fraction×BP, BP − MAIN_RESERVE −
  cushion); MAIN_RESERVE ≈ $1,000–1,500. Day-tier = residual claimant behind the validated main book
  (dtbp_check="exit" → a starved main-scan order REJECTS).
- **Q5 gap tail (seat) — the added blocker:** kills are TICK-evaluated → gappable; the JOINT
  (main $4,869 + day) gross has no equity-terms ceiling → a −4% gap ≈ −13% of the account in one
  candle. Fix: an **equity-relative aggregate ceiling** (~0.60×equity ≈ $1,500) so 4× BP can't
  balloon the equity bet; it also stabilizes the cap against main-bot BP swings.
- **Q2 kill:** Gro+GAI → −4% equity (−$100) (the −$50 buffer at −$125 is thin); seat OK with −5% at
  the lower aggregate. Basis = equity. Adopt −4% (−$100).
- **Q3 BP field:** all confirm buying_power (DTBP, intraday) vs regt. Fail-safe: BP ≤ 0/missing/
  non-finite → hard SKIP, NEVER fall back to the equity slice or a cached BP.

CORRECTED AT SOURCE (verify-before-relay):
- GAI "death-spiral force-flat": FALSE premise — `_gross_cap_ok` only GATES new entries
  (place_entry:581-584 returns False=skip); it never flattens. A main-bot BP contraction merely
  blocks NEW day-tier entries (desirable auto-throttle). GAI's stabilization point is met by the
  equity ceiling.
- Seat's `dtbp_check="exit"` hard-skip: `dtbp_check` is a STATIC account POLICY (when Alpaca applies
  the DTBP check), not a live over-limit flag — gating on it would permanently disable the tier. NOT
  adopted.

### REFINED RECOMMENDATION (honors "maximize" but folds the unanimous safety additions)
- Per-trade budget = `buying_power × 0.25 × conviction` (affords the whole universe incl. MSFT
  ~$491 at ~$1,042 full-conviction; solves the blocked-name problem, caps single-name tail).
- Aggregate Track-A gross cap = `min(BP × 0.35, equity × 0.60, BP − MAIN_RESERVE − cushion)`,
  MAIN_RESERVE = $1,200, cushion $650. (Three coupled bounds; the equity ceiling binds the gap tail.)
- Fail-safe: BP ≤ (MAIN_RESERVE + cushion) OR non-finite OR ≤ 0 → skip (no equity fallback).
- Tier kill = −4% of EQUITY (−$100); basis equity; all other kills/stops/force-flat UNCHANGED.
- place_entry already re-reads BP + runs _gross_cap_ok BEFORE submit (fresh read) — add the equity
  ceiling + reserve there.
- FAST-FOLLOW (Tidy-First, separate diff — do NOT scope into this sizing change): GAI NITs —
  structural stop computed off entry_ref (place_entry:552) not the FILLED price (slippage widens
  risk); floor() should use ask not last to avoid insufficient-funds rejects.

## Masked-loss / reliability / anti-silo seat — BLOCK (the decisive seat), verified at source
Three cross-component breaks my first proposal missed (2 of the "unchanged" backstops are defeated):
- **S1 kill-nesting guard blinded (VERIFIED config.py:942):** the ONLY code enforcing tier-kill <
  account-kill is `DAYTRADE_TIER_KILL_PCT * DAYTRADE_ALLOC_PCT >= MAX_DAILY_LOSS_PCT` (0.0375 vs
  0.07). A −5% re-base via a NEW constant leaves it validating a phantom 0.0375. AGGRAVATOR (VERIFIED
  config.py:229/289/320): MAX_DAILY_LOSS_PCT=0.03 module default, 0.07 applied only by main.py's
  profile; run_day_tier.py bare-imports config (line 134) in its own process → the day-tier process
  does not get the 7% override. Fix: rewrite :942 to the new constant; run_day_tier asserts the paper
  profile / carries its own account-kill awareness.
- **S2 cushion mathematically DEAD (VERIFIED day_trade_manager.py:214):** `BP − new_notional < $650`
  fires only when one order > $3,519; per-trade cap ($834–1,459) < $3,519 → never trips. Also checks
  DTBP ($4,169), not the real maintenance pool (regt $137 / maint_margin $1,460), and ignores `cur`
  (not cumulative). Fix: make the headroom check cumulative (cur+new) and BIND via the reserve; the
  MAIN_RESERVE ($1,200)+cushion ($650)=$1,850 held-back makes it live again.
- **S3 dtbp_check="exit" + shared-pool stale BP:** Alpaca applies the DTBP check at EXIT not entry, so
  an over-deploying entry off stale-high shared BP is NOT rejected at entry — it surfaces when the
  PROTECTIVE EXIT can't fill (naked risk). Fix: MAIN_RESERVE + (optionally) dtbp_check="both" (account
  config — Rafael's call, don't flip unilaterally; changes MAIN-bot behavior too).
- Confirmed SOUND: partial-fill / naked-remainder / co-hold flatten math is magnitude-INDEPENDENT
  (not broken by bigger fills). Sub-kills DAYTRADE_TRACK_A/B_KILL_PCT are config-only (no runtime
  consumer) — validated at :929-937 but never enforced live (separate note).

## FINAL CONSOLIDATED SAFE DESIGN (folds all 4 voices — this is what ships on Rafael's approve)
1. Per-trade budget = `buying_power × 0.20 × conviction` (affords the whole universe incl. MSFT; caps
   single-name tail). Runner passes 0 (not None) on an unreadable account.
2. Aggregate Track-A gross cap = `min(equity × 0.60, buying_power − MAIN_RESERVE − cushion)`,
   MAIN_RESERVE=$1,200, cushion=$650; CUMULATIVE (cur + new ≤ cap). Equity ceiling (~$1,500) binds the
   gap tail; reserve protects the main book AND revives the cushion.
3. Fail-safe: buying_power ≤ (MAIN_RESERVE+cushion) OR non-finite OR ≤0 → SKIP (no equity fallback).
4. Tier kill → NEW `DAYTRADE_TIER_KILL_EQUITY_PCT = 0.04` (−$100 at $2,503); rewrite tier_kill_check
   to use it; REWRITE config.py:942 to validate the new constant vs MAX_DAILY_LOSS_PCT; run_day_tier
   asserts the paper profile. 0.04 < 0.07 ✓ nesting holds.
5. place_entry already re-reads BP + runs _gross_cap_ok before submit (fresh) — keep; reserve covers
   the stale window.
6. FAST-FOLLOW (separate diffs, NOT this change): stop-on-FILLED-price (place_entry:552 uses
   entry_ref), ask-price floor, dtbp_check="both" (Rafael's account-config call), sub-kill runtime
   enforcement.
BOARD VOTE: Gro APPROVE-WITH-CHANGES · GAI REJECT→resolved · Sizing seat BLOCK(Q1/Q5) · Masked-loss
seat BLOCK — ALL converge on the SAME fixes above; folding them clears every named blocker.

## Safety envelope (UNCHANGED — this widens size within it, never the envelope)
7% account kill · paper=True · never-mask-a-loss · force-flat 20-min pre-close · per-position
structural stop (R≈1:1) · cushion $650 · opposite-side co-hold guard · whole-share floor · per-tick
API budget · flock singleton. Per PROFITABLE>PERFECT + North-Star: more aggressive WITHIN the
envelope, never a wider envelope.

## 2026-09-09 implementation correction after exact-diff cold review

The first staged implementation did not enforce what this record claimed. It counted only
day-tier logged positions, treated `buying_power - $650` as a maintenance cushion, rejected a
high-conviction size at the cap instead of clamping it, priced the cap at `entry_ref` rather than
the submitted limit, and did not read the main process's persisted kill state. Current Alpaca
accounts use real-time intraday margin; the earlier legacy-DTBP / `dtbp_check="exit"` language is
not a valid control for this implementation.

The corrected ship design is mechanical:

1. The pure Track-A budget is `min(20% × current buying_power, 60% × equity) × conviction`.
2. Immediately before submit, requested shares are clamped at the actual marketable-limit price by
   all-tier live gross room (`2.5 × equity - every Alpaca position - pending increasing orders`),
   day-tier room (`60% × equity - open day-tier gross - pending day-tier entries`), current buying
   power after the $1,200 main-book reserve, and actual maintenance room
   (`(equity - maintenance_margin - $650) / symbol maintenance rate`). Missing/non-finite inputs
   produce zero shares.
3. A per-trade stop-loss budget caps `qty × abs(limit_price - stop_price)` at 2% of equity, strictly
   inside the 4% tier kill.
4. The runner evaluates the same persisted, QHM-aware account kill used by the main bot before its
   entry loop; `place_entry` rechecks persisted halt/account-block flags at wire time.
5. The tier kill uses SOD equity and cumulative loss-only realized exit marks plus open unrealized
   P&L. Profitable exits cannot offset the loss floor. An unreadable loss log or unreadable open lot
   halts new entries for the day while existing protected positions remain managed.
6. The read-only preflight calls the same wire-time quantity clamp, so `WOULD_ENTER` means at least
   one share clears the live book, maintenance, pending-order, and stop-risk guards.
7. Exit accounting is broker-confirmed. Day-tier forced closes retain the submitted order object and
   do not write an `exit_fill` until Alpaca reports the requested fill quantity and average price.
   Reconcile recovers a completed protective stop by its durable stop order id, including when a
   same-side position remains for another tier. Unknown or partial stop outcomes halt new entries
   instead of flattening an unattributable cross-tier quantity.
8. An account-data/entry-halt failure never bypasses management: reconcile and the EOD force-flat
   decision run first. Tier-loss evaluation also runs before account-level entry permission, and a
   latched tier kill retries residual liquidation every tick until flat.
9. Confirmed partial forced or protective exits are written to the durable day-tier log and reduce
   the state-owned quantity before any retry. An unreadable newer stop remains unresolved even if an
   older canceled stop is readable, preventing fallback liquidation from consuming co-held shares.

The main and day-tier processes still cannot make two independent broker snapshots transactional.
Pending-order accounting closes the ordinary overlap window; Alpaca buying-power enforcement and
the all-tier gross re-read remain the final broker-side backstops. A shared account-entry reservation
primitive is tracked as separate cross-strategy hardening because it changes every entry path.
