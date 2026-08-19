# Tier-1.2 (QHM earnings-trim → ownership-ledger auto-confirm) — GATE RESULT: REJECTED 3-0

**Date:** 2026-08-16 (autonomous AWP build, unattended — STOPPED at package, nothing shipped)
**Status:** ⛔ **DO NOT rebuild the net-keyed auto-confirm design.** Awaiting Rafael's fork decision (below).
**Related:** the original pre-scoped package `qhm_earnings_trim_ledger_autoconfirm_2026-08-12.md` (merged #141)
describes the design that FAILED this gate.

---

## What was built + gated
`record_system_heal_confirmation()` in `execution/ownership_guard.py` (+85 lines) + two hooks in
`execution/quarterly_hold_manager.py` (tier-1 after poll-confirm; tier-2 after full-exit submit).
Intent: auto-write the operator-format heal-confirmation when a QHM earnings-trim's reduction is
verified settled, so a legit trim no longer needs a manual `confirm_ledger_heal`.

## Gate result
- Statics ✅ · Gro ✅ APPROVE · GAI ✅ APPROVE · Cold-2nd ✅ PASS · Adversarial ❌ FAIL (efficacy overclaim)
- **Board: 3 REJECT / 0 APPROVE** — three distinct, SOURCE-VERIFIED, concrete failing scenarios that
  Gro/GAI/cold-2nd all MISSED (the cold board doing exactly its mandated job):

1. **MASKED-LOSS seat — REJECT (stale-confirmation race; reachable on ANY symbol).**
   Tier-1 writes a net-keyed confirmation (2h TTL). A later tier-2 full exit deregisters the symbol
   (its own write no-ops if the close hasn't settled — the design's own "no tier-2 poll" behavior).
   The symbol, now deregistered, becomes eligible for a fresh **unrelated intraday** entry. If that new
   position's net coincidentally equals the stale tier-1 target within 2h, `sync_ledger` MATCHES the
   stale confirmation, OVERRIDES the correct fill-replay, and mislabels the fresh intraday shares as
   qhm-protected → `check_never_sell_floor` then BLOCKS that intraday position's own stop/target exit
   (the "keystone catastrophe" — blocking a legit exit). Root: aggregate-net + TTL is an insufficient
   proxy for "same shares/tier"; automating a rare human-timed confirm makes this routinely reachable.
   Fix rec: tier-2/deregister must INVALIDATE any prior tier-1 confirmation for the symbol; bind the
   match to the close order-id / position identity (not aggregate net); shorten TTL.

2. **RELIABILITY seat — REJECT (blocking call on the trading thread).** `record_system_heal_confirmation`
   calls `reporting.pnl_ledger.fetch_positions()` → `_get_json` (tries=8 × timeout=30s + backoff) ≈
   **282s worst-case synchronous block on the single-threaded run_cycle thread**, and the tier-1 hook
   sits INSIDE the naked-stop window (between stop-cancel and stop-restore) → starves `check_exits()`
   for all other positions. Same class as the `scan_to_html.py` cold-2nd ship-blocker.
   Fix rec (easy): caller passes a broker-verified `live_net` — tier-1 = `_confirmed_qty` (already
   poll-verified, zero new I/O); tier-2 = one fail-fast `_earnings_trim_broker_qty(pos)` read.

3. **DATA-INTEGRITY seat — REJECT (tier-purity / laundering).** `_confirmed_qty` = RAW broker net, not
   qhm-isolated. VERIFIED: `{GOOGL, NVDA}` are in BOTH `FOREVER6_UNIVERSE` (config.py:401) AND the QHM
   picks; `forever_hold_manager` tags 0 orders; `sync_ledger` has a qhm overlay but NO forever6 overlay
   → forever6 shares are invisible to the replay. For a co-held symbol the auto-confirm can launder
   forever6 shares into the qhm floor — the EXACT class the 2026-08-08 board rejected TWO prior auto
   designs for. (Currently lower-liveness: forever6_holds.json is test data; live NVDA/GOOGL are likely
   pure-qhm today. But structurally unsafe — forever6 can buy them anytime.)
   Fix rec: derive target from a qhm-ISOLATED count, OR add the forever6 overlay to sync_ledger, OR
   skip auto-confirm for any symbol with co-held forever6.

## Root cause (shared by findings 1 & 3)
**Aggregate broker net is an insufficient proxy for tier identity AND for temporal continuity.** The
net-keyed heal-confirmation cannot tell (a) which tier the shares belong to, nor (b) whether the state
it attests to still holds when consumed. This is the third auto-heal design to hit this class.

## Pre-existing latent bug surfaced (independent of this feature)
forever6 never tags its orders → if forever6 is EVER live on a name, its shares mis-attribute to the
`intraday` tier in the ledger with no compensating overlay. Verify whether forever6 runs in production;
if so, ledger tier-attribution is already wrong for any forever6 holding. (Today: forever6_holds.json
appears to be test data — verify.)

## FORK FOR RAFAEL (Open Question Protocol — board rec brought with it)
- **A) Redesign auto-confirm safe + re-gate:** non-blocking live_net + tier-purity guard (excludes
  NVDA/GOOGL) + race-hardening (invalidate-on-supersede, order-id binding, short TTL). ~1-2 sessions.
- **B) Fix the ROOT first (board-leaning):** forever6 order-tagging + sync_ledger forever6 overlay +
  identity-bound confirmations → closes tier-purity system-wide, fixes the latent bug, AND enables safe
  auto-confirm for ALL symbols incl NVDA/GOOGL. ~2-3 sessions.
- **C) Keep manual confirm, reduce friction:** Slack alert + one-command confirm when a trim needs it.
  Safest/cheapest; manual step stays but low-friction.
- **D) Minimal subset:** ship reliability+race-hardened auto-confirm for NON-overlap picks (GE/GEV/LLY)
  only; defer NVDA/GOOGL to manual. (Still needs the race-hardening from #1.)

**Board lean:** given the SAFETY-ENVELOPE carve-out (masked-loss risk = no velocity relaxation) and that
this is the 3rd design to hit the laundering class, **B (root fix)** is the durable answer; **C** if the
auto-confirm value doesn't justify B's cost. Do NOT ship any net-keyed variant (A/D) without the
identity-binding + invalidation from finding #1.

---

## ✅ RAFAEL'S DECISION (2026-08-16): **B — Fix the root first.**

### Scoped plan for B (tidy-first / Beck golden-diff — 3 SEPARATE gated increments)
- **B1 (tidy, lower-risk): forever6 order-tagging.** Make `forever_hold_manager` tag its buy orders so
  fills are tier-attributable. The util already exists: `ownership_guard.make_coid(tier, symbol, side,
  epoch_ms, uniq)` + `tier_of_coid()`. B1 = route forever6 buys through a `make_coid("forever6", …)`
  client_order_id. Precondition for B2. Gate: touches RTH order submission → full gate (low-risk additive).
- **B2 (HIGH-risk, ledger authority): sync_ledger forever6 attribution.** Make `sync_ledger` see forever6
  shares so `_other_prot` is no longer blind. Full cold board + Gro + GAI (masked-loss critical).
- **B3 (rebuild the auto-confirm, SAFE): identity-bound confirmations.** Bind the heal-confirmation to
  the close order-id / position identity (not aggregate net) + invalidate-on-supersede; then rebuild the
  QHM earnings-trim auto-confirm on top of B1+B2. Full gate.

### ⛳ THE DESIGN FORK THAT GATES B1/B2 (Open Question Protocol — decide FIRST)
**How should forever6 shares become attributable in the ledger?**
- **Approach 1 — order-tagging (fill-level):** forever6 tags buys via `make_coid`; replay attributes via
  `tier_of_coid`. Robust (fill-level truth); no `sync_ledger` signature change. Con: only tags NEW buys →
  existing untagged forever6 shares need a one-time backfill.
- **Approach 2 — ledger claim-overlay:** add `forever6_holdings` param + overlay to `sync_ledger`
  (mirror `qhm_holdings`). Works for existing shares; but it's a *claim* overlay (same weaker class the
  data-integrity seat flagged for qhm) and changes the highest-risk file.
- **Approach 3 — both** (tag new + overlay as backstop).
Data-integrity seat leaned overlay; tagging is arguably more robust. **NEEDS board + Gro + GAI on the
DESIGN before B1 code.**

### Also confirm (Feature Design Protocol open items)
1. **Is forever6 actually LIVE in production** (a buyer cron), or dormant? `forever6_holds.json` looks like
   TEST data (prices all 100.0, fake OID-*). If dormant, B is preventive/architectural (lower urgency),
   and the immediate NVDA/GOOGL live positions are effectively pure-qhm today.
2. **Backfill** of any existing untagged forever6 shares (if forever6 is live).
3. **Identity-binding scheme** for B3 confirmations (close order-id vs a QHM position uuid).

### ⏭️ IMMEDIATE NEXT STEP: run the Open Question Protocol design pass on the attribution fork
(board + Gro + GAI), then present the recommended approach → build B1.

---

## ⚠️ CORRECTION (2026-08-16, verified at source) — THE "ROOT" IS NOT BROKEN; B IS LARGELY UNNECESSARY

**A false premise I introduced invalidated finding #3 and the entire "root fix B" framing.** During the
design pass, the data-integrity seat re-verified at source (per the verify-at-source rule) and found:

- **forever6 DOES tier-tag its orders.** `forever_hold_manager.py:274` `submit_market_order(sym,1,"buy",
  tier="forever6")` and `:726`→`submit_f6_trim`→`partial_close_position(...,tier="forever6")` both thread
  `tier` → `broker.py:_make_idem_id` → `make_coid` → an `F6-…` client_order_id, parsed back by
  `tier_of_coid`. **Approach 1 (order-tagging) is ALREADY SHIPPED and wired into the live sync**
  (`run_ledger_sync` builds `coid_map` and passes `coid_by_order_id`).
- **My earlier grep was mis-scoped** to `forever_hold_manager.py` alone (0 `make_coid` hits there) — but
  the tagging is one indirection down in `broker.py`. I put that false "forever6 never tags" claim into
  BOTH review briefs, contaminating finding #3 and the B framing.
- **`FOREVER6_ENABLED = False`** (config.py:400, gated run_cycle.py:876) — forever6 has NEVER fired. Zero
  forever6 positions exist (tagged or untagged). `ownership_ledger.json` has zero qhm/forever6 tier qty.
- **`_f6` in the qhm clamp (ownership_guard.py:721)** is live-wired to the fill-replay, reading 0 only
  because no forever6 fill exists — NOT hardcoded/structurally-blind.

**Re-evaluated the three Tier-1.2 rejects with corrected facts:**
- **#3 (tier-purity laundering) — INVALID / moot.** Rested on my false "forever6 untagged" premise.
  forever6 is tagged (visible to `_f6`) AND disabled. Residual (auto-confirm's raw-net target isn't
  qhm-isolated) is real-but-conditional and cheaply fixed by subtracting the tagged forever6 qty; moot
  while forever6 is dark.
- **#1 (masked-loss stale-confirmation race) — STILL VALID.** Independent of forever6: a net-keyed tier-1
  confirmation (2h TTL) can be coincidentally re-matched by a later unrelated intraday position after
  tier-2 deregisters → mislabels intraday as qhm → blocks its exit.
- **#2 (reliability ~282s block) — STILL VALID.** Independent of forever6.

**Corrected design-pass verdict on the attribution fork:** Approach 1 (already shipped) is correct; a
`sync_ledger` claim-overlay (Approach 2/3) is NOT needed and would add standing risk to the highest-risk
file for a legacy population that doesn't exist. Board seats (masked-loss, data-integrity) both → Approach
1; Gro/GAI → Approach 3 but took the false-premise brief at face value.

**REVISED PATH (supersedes B):** "root fix B" is unnecessary — the root (forever6 attribution) is already
sound + dark. The real remaining work is a **v3 of the Tier-1.2 auto-confirm** fixing ONLY the two valid
rejects (Option A from the original fork):
- Fix #2: parameterize `live_net` (fail-fast, non-blocking) — tier-1 = `_confirmed_qty`, tier-2 =
  `_earnings_trim_broker_qty`.
- Fix #1: identity-bind the confirmation (close order-id) + tier-2/deregister invalidates any prior
  tier-1 confirmation + short TTL (touches the consumer `_matching_heal_confirmation`/`sync_ledger`).
- Fix #3-residual (cheap, future-proof): isolate the qhm target by subtracting tagged forever6 qty.
This is ONE gated increment, far smaller than B. **Rafael's B choice was made on my bad info → re-decide.**

---

## v3 GATE RESULT (2026-08-16) — BOARD REJECTS AGAIN (1 APPROVE / 2 REJECT + adversarial FAIL)

Rafael chose **A** (build v3). Built + full-gated. Result:
- Statics ✅ · cold-2nd ✅ PASS · Gro ✅ / GAI ✅ (both APPROVE after a counter-prompt refuted a false-
  premise round: GAI hallucinated a missing time/os import — refuted by imports L35/37/38 + clean F821).
- **Reliability seat: APPROVE** — verified the ~282s block is GONE (helper does zero network I/O; tier-1
  reuses the already-polled `_confirmed_qty`; tier-2 = one fail-fast `_earnings_trim_broker_qty`).
- **Masked-loss seat: REJECT** — the race guard reads `qhm_holdings` (`get_quarterly_hold_quantities`),
  but `PENDING_EXIT` ∈ `_QHM_ACTIVE_STATES` and tier-2 sets PENDING_EXIT WITHOUT zeroing `qty_filled`
  (no resync, unlike tier-1); `PENDING_EXIT→CLOSED` only runs on RESTART (`_reconcile_pending_exit` ←
  `reconcile_on_startup`). So an exited symbol reports stale-positive qty all session → the guard fails
  to skip a stale confirm → an unrelated later intraday position of coincidental net gets relabeled qhm →
  its exit is frozen. VERIFIED at source.
- **Data-integrity seat: REJECT** — `_resync_from_alpaca` (L3095) sets `pos.qty_filled = live_qty` = RAW
  tier-blind Alpaca net; so tier-1's `target=_confirmed_qty` (raw net) can relabel **intraday** shares
  into qhm on a qhm↔intraday co-held symbol. The joint protected-sum guard only covers qhm+forever6, NOT
  qhm+intraday → no backstop. Live TODAY, independent of FOREVER6_ENABLED. VERIFIED at source.
- **Adversarial: FAIL** — Claim 2 ("race closed") overstated; 3rd PENDING_EXIT vector via `_initiate_exit`
  (91-day max-hold exit) which also sets PENDING_EXIT and never drains except on restart.

**UNIFYING ROOT (all 4 attempts — 2026-08-08 ×2, v1, v3):** the auto-confirm relies on TIER-BLIND
raw-net signals (`_confirmed_qty`, `qty_filled` contaminated by `_resync`, `get_quarterly_hold_quantities`
stale through PENDING_EXIT). None answers "qhm's TRUE tier-isolated claim on this symbol RIGHT NOW." The
only tier-accurate source is the fill-tag replay in the ownership ledger — which is what the human
operator verifies by eye. Each patch closes one vector; the tier-blind signal spawns another.

### FORK FOR RAFAEL (re-decide — board consensus: current approach not safe to ship)
- **C — keep manual confirm, cut friction (BOARD-RECOMMENDED):** drop the auto-confirm; add a Slack alert
  + one-command confirm when a QHM trim needs a ledger heal. Sidesteps the ENTIRE race/contamination
  class (a human verifies tier ownership + timing). Cheapest, safest. Per SAFETY-ENVELOPE + "change the
  approach when it keeps failing" doctrine, this is the recommendation.
- **v4 — deeper redesign (if auto-confirm is still wanted):** (1) derive the tier-1 target from the
  TAG-ACCURATE ledger (`tier_qty(ledger,"qhm") - trim_qty`), not raw net → closes contamination; (2) a
  PENDING_EXIT-aware / identity-bound race signal (not `get_quarterly_hold_quantities`) → closes the race.
  Both on the highest-risk file + the QHM manager, with a 4-reject track record on this class. Higher risk.

STATUS: v3 NOT shipped (unattended fire + gate rejected). Awaiting Rafael's C-vs-v4 decision.

---

## v4-B DESIGN PASS (2026-08-16) — MECHANISM UNANIMOUSLY ENDORSED; SCOPE decision needed

Rafael chose **v4-B** (direct authorized ledger decrement, replace the confirm file). Ran a DESIGN gate
(Gro + GAI + board: masked-loss, data-integrity, reliability) on the mechanism BEFORE coding.

**Mechanism verdict: SOUND / endorsed by ALL voices.** The synchronous, tier-isolated, no-persisted-
artifact, broker-poll-gated direct decrement eliminates the stale-confirmation/net-coincidence race class
the board rejected 4×. "It should not go back to a confirmation-file design" (masked-loss + data-integrity).

**Required change-set (converging across seats):**
1. `alpaca_net_qty` from a FRESH post-action broker read — NEVER `net -= delta` (masked-loss: the tier-2
   co-held "two-wrongs-cancel, drift reads 0" masked-loss-adjacent trap). Keystone fix.
2. `delta` = the OBSERVED post-sell reduction (order_id→filled_qty), not the pre-computed target (Gap A).
3. Internal exception handling (try/except LedgerError + around save_ledger, CRITICAL log, return False —
   don't lean on the caller's catch-all); shorter lock timeout ~1.5s for the run_cycle caller
   (Reliability). Concurrency traced to fail-safe self-healing freeze (not masked-loss). Lock split
   resolved toward Reliability (best-effort + shorter; atomic save is the durability guarantee; a missed
   interleave self-heals next sync) over GAI's strict-or-abort.
4. Page `cur < delta` refusal into `page_floor_blind` (operator visibility).

**DEEPER FINDING — the co-held case is where ALL residual danger lives (data-integrity + masked-loss):**
`_resync_from_alpaca` sets `pos.qty_filled = raw tier-blind Alpaca net`; that (a) mis-sizes `trim_qty`,
(b) feeds `get_quarterly_hold_quantities` → the sync qhm-overlay `max(claim, replay)` SILENTLY re-inflates
qhm + zeros intraday on the next sync (reverting the decrement, no alarm), and (c) tier-2's whole-symbol
`close_position` (unguarded while OWNERSHIP_GUARD_ENFORCE=False) sweeps co-held intraday/forever6 shares.
This is the SAME tier-blind-ownership architecture that RETIRED the Movers bot (CLAUDE.md: "per-strategy
client_order_id ownership tags + qty-bounded partial close" — the deferred real fix). Fixing it fully is
a big multi-part build.

**MITIGANT (verified):** all current QHM holds (NVDA, GOOGL, GE, GEV, LLY) are PURE-QHM today (no
co-holding), and OWNERSHIP_GUARD_ENFORCE=False → the co-held danger is LATENT, not live.

### SCOPE FORK FOR RAFAEL (board rec: pure-qhm-guarded v4-B)
- **v4-B-guarded (RECOMMENDED):** implement the decrement + change-set 1-4, GUARDED to pure-qhm symbols
  (auto only when the symbol has zero other-tier shares; co-held → fall back to manual confirm). For a
  pure-qhm symbol net==qhm, so the decrement is exactly tier-accurate and every co-held finding is
  sidestepped. Ships auto-confirm for ALL current holds, safely. Defers the Movers-class architecture.
- **v4-B-full:** also fix the tier-blind ownership root (tier-accurate trim-sizing from ledger qhm; fix
  the overlay trust model; qty-bounded tier-2 partial close). Complete + handles co-held, but a big
  multi-part build on the highest-risk files (the deferred Movers-class work).
- **C:** abandon auto; manual + reduced friction (still available).

STATUS: v4-B design pass complete; mechanism endorsed. Rafael chose v4-B GUARDED to pure-qhm.

---

## v4-B BUILD (implemented pure-qhm-guarded) — BUILD GATE RESULT (2026-08-16/17)

Implemented `apply_authorized_tier_reduction(symbol, tier, fresh_net, source)` in ownership_guard.py
(one new function, NO sync_ledger/consumer change) + tier-1 & tier-2 hooks in quarterly_hold_manager.py.
Files (scratchpad, NOT applied): og_v4b.py, qhm_v4b.py; diff tier12_v4b.diff (+88 og / +28 qhm).

**Build gate:** statics ✅ (py_compile/ruff/mypy) · Gro ✅ (after counter refuting 3 false-premise findings)
· GAI ✅ · cold-2nd ✅ PASS · **masked-loss APPROVE** ("could not construct a blocking scenario"; verified
all 6 change-set items; fail-safe under every race/stale/bad-input path via check_never_sell_floor's
independent live-net re-verify) · **reliability APPROVE** (both impl reqs met: internal exception handling
+ 1.5s lock; concurrency fail-safe) · **data-integrity REJECT (NARROW, tier-2 only)** · adversarial
INCOMPLETE (hit the session limit mid-run, reset 9:50pm PT).

**CLEARED: the core helper + the tier-1 hook.** All seats approve them. Tier-1 auto-reduces the qhm floor
on a partial trim, pure-qhm-guarded, fresh-net, non-blocking, never-raises — verified.

**OPEN (data-integrity REJECT): the tier-2 (full-exit) hook has no fill-confirmation poll.** tier-2's
`close()` returns on SUBMISSION not fill (verified in broker._raw_close_position + this file's own
comment); the hook reads `_earnings_trim_broker_qty` immediately, so a pre-fill read = pre-exit qty →
`apply_authorized_tier_reduction` no-ops (net==cur) → ledger stays stale → the next sync sees the eventual
real shrink as unconfirmed → never-shrink refuses the whole reconciliation pass until a manual
confirm_ledger_heal. NOT a regression (tier-2 full exits need a manual confirm today too) — but tier-2
doesn't get the auto-benefit, and the brief's "tier-2 settled net" was an overclaim. data-integrity:
"tier-1 + the core helper need no changes; once tier-2 gets a bounded poll I'd flip to APPROVE."

**Polish noted for final (cosmetic, log accuracy):** the save-failure CRITICAL log says "ledger
understated" — on a failed save the on-disk ledger is UNCHANGED (stale/over-protective), so reword to
"ledger NOT updated (stale/over-protective) until next sync" (cold-2nd + masked-loss + data-integrity all
flagged the wording). "self-heals on next sync" in the brief is also imprecise (reliability): the
sync-race residual resolves via operator confirm OR a later authorized-reduction, not automatically.

### RECOMMENDATION (awaiting Rafael + capacity/session reset)
- **Ship tier-1-only now (Rec):** the core helper + tier-1 hook are fully gate-cleared. Drop the tier-2
  hook from this diff (trivial deletion) → ship the tier-1 auto-reduce (delivers the benefit for partial
  earnings-trims). Add tier-2 (with a bounded fill-poll mirroring tier-1's) as a separate follow-up diff.
  Tidy-first; tier-2 stays at status-quo manual until then (no regression).
- **OR add the tier-2 bounded poll now** + re-gate (data-integrity → expected APPROVE) + complete the
  adversarial pass, then ship both tiers together. Bigger, adds bounded blocking to the tier-2 path.
- Apply the log-wording polish either way before ship.

STATUS: v4-B build gate done; tier-1+core CLEARED, tier-2 hook REJECTED (needs poll or drop). Session
limit hit (adversarial incomplete) + unattended → STOPPED at package, nothing shipped. Awaiting Rafael's
tier-1-only-vs-add-poll decision + a fresh session (session resets 9:50pm PT 2026-08-16; now 08-17).
