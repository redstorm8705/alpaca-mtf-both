# Intraday audit #1 — GEX whether-to-act → core intraday MTF entry (ANTI-SILO interconnect)

**Date:** 2026-09-03 · **Queue:** item #2 (strategy-by-strategy intraday audit through the new mechanical gates), strategy #1 = core intraday MTF entry.
**Status:** AUDIT COMPLETE + DESIGN DRAFTED → board + Gro/GAI review (Open Question Protocol) → Rafael sign-off → front-loaded sim (Rule C) → build.

## The finding (audit step 1)

`execution/entry_logic.py::execute_entries` (L275–1796, full read done 2026-09-03) runs a **42-gate entry gauntlet** (premarket-red, gap-down settle, SPY-direction, ORB breakout confirm, counter-trend/falling-knife, earnings-HTF, analyst, R:R floor, sector + pairwise-correlation caps, Kelly clamps, gross-exposure, min-lot, etc.) — mature and rigorous.

**It consults ZERO of the day-tier's new mechanical gates.** Grep-verified at source: `entry_logic.py` has **no functional GEX / `day_tier_gex_action` / `day_tier_side` reference** (only a comment at L955). The bot computes a dealer-positioning "whether-to-act" read every cycle and the core intraday strategy never looks at it → a textbook ANTI-SILO violation (accurate signal, zero cross-value).

## Verified interface facts (no-guess)

- `strategy/day_tier_gex_action.compute_gex_action(symbol, side_bias=None, now_et=None) -> dict` with `action` (FADE|RIDE|STAND_DOWN), `gex_label` (POSITIVE|NEGATIVE|UNKNOWN), `act_ok` (True only for a resolved, sign-reliable, strong read), `strength`, `sign_reliable`, `targets`. **Never raises** (read-only signal). Fail-safe: any missing/stale/unresolved GEX → STAND_DOWN, act_ok=False, strength 0.
- It reads `data.gex.get_gex_levels/get_gex_regime`, which are **local reads of `logs/gex_snapshot.json`** (`_SNAP_PATH.read_text()`, data/gex.py L981) — **NO network I/O**, recomputes nothing, 30-min stale guard (`GEX_STALE_MINUTES`). **Trading-thread-safe** to call in the per-symbol entry loop. (Snapshot refreshed by a separate GEX cron.)
- Thresholds in day_tier_gex_action are **PROVISIONAL** (`PROV:daytier-gex-action`: `_MIN_STRENGTH=0.20`, `_SINGLE_NAME_CONF_FLOOR=0.35`, DTE/TOD factors) — to be derived from outcomes.

## The mapping (corrected during the audit)

The MTF entry is a **momentum/breakout** strategy. Standard GEX interpretation:
- **+gamma / FADE regime** = dealers dampen → breakouts fail / mean-revert → **headwind for a momentum entry**.
- **−gamma / RIDE regime** = dealers amplify → breakouts run → tailwind.

So the correct filter is NOT "block on STAND_DOWN" (STAND_DOWN just means no clean read — would kill most entries). It is: **down-weight a momentum entry when a RELIABLE FADE/+gamma regime opposes it.**

## Proposed design (v1) — for the board

Insert in the sizing chain (~L1298, beside the FVG `_compute_fvg_mult` multiplier), NOT a hard block:
- Compute `ga = compute_gex_action(symbol, ...)` once the candidate reaches sizing.
- If `ga["act_ok"]` AND `ga["gex_label"] == "POSITIVE"` (reliable FADE/+gamma headwind) → `dollar_cap *= GEX_MTF_HEADWIND_MULT` (a **down-only** de-weight, e.g. 0.85 — PROV, tunable).
- Everything else (RIDE, STAND_DOWN, UNKNOWN, stale, act_ok=False, error) → ×1.0 (neutral, no effect).
- **No up-multiplier** (RIDE does not raise size) → pure protective/selectivity.

## Risk-path classification (Rule E)

Down-only de-weight → REDUCES size, never raises it → does not increase per-trade size, frequency, or concurrency → **NOT risk-path** per the Rule E definition (a pure selectivity/quality change bounded by existing caps). Same-day-eligible IF the Rule B independent trace confirms all-zero on size/freq/concurrency. STILL requires: statics + cold-2nd + board seat (the trace) + Gro/GAI preship. (An UP-weight variant would be risk-path → separate board + masked-loss seat; explicitly NOT in v1.)

## Front-loaded simulation plan (Rule C — before build)

1. Cases/regimes: replay `logs/day_tier_shadow.jsonl` + `trade_events.jsonl` entries against the GEX snapshot history — for MTF momentum entries taken in a reliable-FADE/+gamma regime, did they underperform entries in RIDE/neutral?
2. Expected effect: momentum entries in +gamma have lower hit-rate / worse R-multiple → de-weighting them improves per-trade expectancy (directional; magnitude TBD from the data).
3. Reversal criterion: if +gamma-regime momentum entries do NOT underperform in the sample, the headwind hypothesis is wrong → do not ship.
4. size/freq/concurrency delta: size DOWN-only; freq 0; concurrency 0.

## Open questions for the board (Open Question Protocol)

1. **De-weight vs hard-block vs shadow-first?** (recommend: down-only de-weight, LIVE if the Rule C sim confirms, since it's protective/non-risk-path; else shadow-log first.)
2. **Trigger on `gex_label==POSITIVE & act_ok` (regime) vs `action==FADE`?** (recommend regime+act_ok — cleaner than the FADE mode which also needs a price-action trigger meant for the day-tier's own strategy.)
3. **Multiplier magnitude** (PROV 0.85) — derive from the Rule C sim's underperformance size, or start PROV and refine in flight (Profitable > Perfect)?
4. **Per-symbol call vs once-per-cycle snapshot load** (catalyst-cache pattern at P5) — v1 per-symbol file read is thread-safe but repeats json.loads ~30×/cycle; the once-per-cycle load is the optimization.

**ANTI-SILO gate check:** ADDS SIGNAL (GEX regime is a real momentum headwind/tailwind, currently unused by MTF) ✓ · FAILS SAFE (module returns STAND_DOWN/neutral on any fault → ×1.0) ✓ · STAYS TESTABLE (isolated multiplier, kill-flag via a config gate, independently loggable) ✓.

---

## UPDATE 2026-09-03 — board review REFRAMED the design; v1 SHIPPED as a fix, not a new bolt-on

The board+Gro+GAI review (4 voices, all APPROVE-WITH-CHANGES) + source verification changed the plan:

**KEY DISCOVERY (board seat 2, verified at source):** GEX is NOT in a silo — it is ALREADY live in
sizing via `execution/kelly.py` L339-373 (`GEX_ENABLED=True`, re-armed 2026-07-26, confirmed on OCID):
`kelly_risk *= _gex_edge_mult`, STRATEGY-BLIND — POSITIVE→1.15 / NEGATIVE→1.30 for ALL trades. So a
momentum trade in +gamma was getting a 1.15× UP-weight — the OPPOSITE of the headwind the board
identified. A naive standalone per-symbol de-weight would have been ~98% cancelled (1.15×0.85). My
entry_logic-only audit missed it (GEX enters sizing through kelly.py, not entry_logic.py).

**SHIPPED (Rafael-approved "proceed with 1", PR #239, OCI `83e321b`, verified live):** made the kelly.py
GEX multiplier STRATEGY-AWARE. POSITIVE branch: `GEX_EDGE_MULT_MR (1.15) if strategy=="mean_reversion"
else GEX_EDGE_MULT_MOMENTUM_POS (1.00, new PROV const)`. Momentum/trend in +gamma 1.15→1.00 (neutral,
down-only); MR keeps 1.15; NEGATIVE unchanged; UNKNOWN/stale fail-safe 1.0. Provably down-only
(cold-2nd enumerated all 5×2 cases) → non-risk-path. Gates: statics + no_static_scan + functional +
cold-2nd PASS + Gro/GAI preship APPROVE both files.

**Sim reality (board seat 2, verified):** the Rule C HISTORICAL replay is impossible — `trade_events.jsonl`
has 37 entries ending 2026-06-05; all GEX history (`logs/gex_daily_audit_*.json`, 42 files) starts
2026-07-03 → ZERO overlap. `logs/day_tier_shadow.jsonl` does not exist. Validation MUST be forward
paired-sample collection drawing from `gex_daily_audit_*.json`.

**OPEN FOLLOW-UPS (not shipped):**
1. **Forward GEX-vs-breakout-outcome log** — ✅ SHIPPED (PR #243, OCI live). `execute_entries` now
   captures `gex_spy_regime` + `gex_spy_raw` + `gex_sym_regime` at each fill into the trade_events.jsonl
   entry event (logging-only, fail-safe, non-risk). A closed trade's outcome (R-multiple from entry/
   exit/stop) pairs with its entry regime → the calibration input to tune `GEX_EDGE_MULT_MOMENTUM_POS`
   and `GEX_EDGE_MULT_MR_NEG` from 1.00 toward optimal de-weights. **DATA ACCRUES OVER WEEKS** — the
   calibration itself (an offline script reading the paired rows once enough accumulate, per LdP
   min-sample/purging from board seat 2) is the future step; nothing more to build now, just let it run.
2. **MR+NEGATIVE mirror mis-sign** — ✅ SHIPPED (PR #241, OCI `e3d7364`). NEGATIVE branch made
   strategy-aware: MR in −gamma → `GEX_EDGE_MULT_MR_NEG` (1.00 neutral, PROV); momentum keeps 1.30.
   Full reconciliation now complete — each strategy up-weighted only in its favorable regime:
   momentum{NEG 1.30, POS 1.00}, MR{POS 1.15, NEG 1.00}, UNKNOWN/stale 1.0. Same down-only gate as #239.
3. **Continuous-in-strength + distance-to-pin** refinements (seat 1 / Gro / GAI) — relevant IF a
   per-symbol day_tier_gex_action layer is added later; the shipped fix uses SPY index-level regime
   (kelly.py), where the per-symbol pin-distance caveat does not apply.
