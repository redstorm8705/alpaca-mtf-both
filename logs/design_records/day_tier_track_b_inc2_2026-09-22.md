# Day-Tier TRACK B — Increment 2 (live wiring) build design record

**Date:** 2026-09-22 · **Status:** BUILDING · **Parent:**
`logs/design_records/day_tier_track_b_momentum_2026-09-21.md` (Inc 1 shipped) +
`logs/design_records/day_tier_v2_design_2026-08-29.md` §7/§7b/§7c (the authoritative Track B spec).

## WHAT INC 2 DELIVERS
Track B (dynamic-mover momentum) goes from a pure signal (Inc 1 trigger, deployed-unexercised) to
LIVE (paper) order placement. Rafael directive "build it" + "I WANT THINGS LIVE".

## FULL-READ-GATE FINDINGS (2026-09-22, fresh session) — what the existing code already gives Track B
Read verbatim this session: `run_day_tier.py` (309), `execution/day_trade_manager.py` (1826, all 6
chunks), `strategy/day_tier_sizing.py` (184), `day_tier_decision.py` (114), `day_tier_entry_trigger.py`
(185), `day_tier_momentum_trigger.py` (277, Inc 1), `scripts/day_tier_preflight.py` (202),
`data/premarket.py` (449), config day-tier block. Conclusions:
- **Sizing already supports Track B:** `compute_day_tier_size(track="B")` → cash-only budget =
  `equity × DAYTRADE_ALLOC_PCT(0.15) × TRACK_B_SHARE(0.35)` ≈ 5.25% of equity. DONE.
- **The stop maps cleanly:** `_compute_stop_price(trigger, dir, entry)` uses `wall_ref ± buffer` for any
  non-FADE mode with `target=None`. Track B passes `wall_ref = structural_level` (the OR level = the
  natural momentum-invalidation stop). The min-stop ATR gate + risk-based `_bounded_entry_qty` then apply
  unchanged; movers are NON-deep-liquidity so they get the CONSERVATIVE thin-name ceiling + $1000 per-name
  cap + 60%-single-name cap. No new stop code needed.
- **All the risk machinery is tier-scoped by the `daytrade` coid** (reconcile, flatten, EOD force-flat,
  tier-kill −5% equity, gross cap, concurrency cap 3 shared A+B). `place_entry` mints a `daytrade` coid for
  Track B automatically → every existing guard covers Track B lots for free.
- **OCO exit works:** RIDE-style (`target=None`) → OCO take-profit at `DAYTRADE_RIDE_TARGET_R(2.0)` × stop
  distance + the structural stop; falls back to a plain stop; EOD force-flat is the backstop.
- **`place_entry` is generic** on `(decision, trigger, size)` — reads `direction`, `entry_ref`,
  `shares`/`size_ok`/`budget`. Track B only needs to construct compatible dicts (the Part-1 adapter).
- **The ONLY design-specified guard NOT auto-present:** the per-track **B sub-kill**
  (`DAYTRADE_TRACK_B_KILL_PCT=0.20` of B budget) — declared in config but UNWIRED (no track attribution in
  the coid). This is the Part-2 fork below.

## PART 1 (PURE — no live caller → NOT risk-path → gate = statics + cold-2nd + adversarial + Gro/GAI)
`strategy/day_tier_track_b.py` — three pure, fail-safe, unit-tested functions (orders nothing):
1. `screen_mover(symbol, intraday_5m, prior_close, avg_daily_volume, now_et) -> dict` — the PRE-REGISTERED
   mover screen (§7.63): on a fixed liquid candidate list, is this name a real intraday mover TODAY?
   Gates: `|gap%| ≥ _MIN_GAP_PCT` (gap = (last close − prior_close)/prior_close → gives `gap_direction`),
   session `RVOL ≥ _MIN_RVOL` (today RTH vol-so-far ÷ (avg_daily_volume × session-fraction-elapsed)),
   `price ≥ _MIN_PRICE`. The pre-registered liquid list IS the float/micro-cap guard (no float call). Every
   threshold `# PROV:daytier-track-b-screen`. Fail-safe: any bad input/error → not a mover.
2. `build_session_frame(symbol, now_et) -> DataFrame|None` — today's RTH 5m frame FROM the 09:30 ET open
   via `data.fetcher.fetch_bars_window(sym, TF_5M, 09:30-ET-today, now)` (the momentum trigger's INPUT
   CONTRACT: bar 0 == the open, so VWAP resets correctly). **HALT AWARENESS:** verify the 5m sequence is
   CONTIGUOUS from the open; a missing bar = a halt → return None (skip — never momentum-trade a halted
   name; that is exactly the reopen-gap tail §7b worried about). Fail-safe None on any error.
3. `momentum_to_entry(mom, gap_direction) -> (decision_B, trigger_B)` — map the Inc-1 momentum result to
   the `place_entry` contract: `trigger_B = {trigger:ENTER, direction, mode:(DRIVE/PULLBACK), entry_ref,
   target:None, wall_ref:structural_level, vol_confirmed}`; `decision_B = {would_consider:True,
   conviction:track_b_conviction(mode,direction), side, track:"B", ...}`. `track_b_conviction` PROV: DRIVE
   0.5 / PULLBACK 0.6, shorts × 0.7 (§7c-d shorts smaller). Conviction only SHRINKS the (already small)
   cash budget (min-only) — never up-sizes.
Plus a full unit-test suite (mover/non-mover, gap up/down, low-RVOL reject, price-floor reject, halt/
non-contiguous frame → None, adapter mapping incl. the stop-side check, shorts-smaller conviction, fail-safe).
Config: NONE — the pre-registered universe reads `getattr(config,"DAYTRADE_TRACK_B_UNIVERSE",_DEFAULT)` with
a module PROV default, so Part 1 touches no config (Part 2 promotes it to config alongside the flag flip).

## PART 2 (RISK-PATH — full masked-loss board + Gro + GAI + cold-2nd + pre-flight sim)
- Wire a PARALLEL Track-B loop into `run_day_tier.py` behind `DAYTRADE_TRACK_B_ENABLED`. WIRING ORDER
  (frame FIRST — adversarial N1: `screen_mover` MUST receive the VERIFIED frame, never a raw window): for each
  pre-registered Track-B symbol → `build_session_frame(sym)` (verified, halt-guarded) → `screen_mover(sym,
  frame, prior_close, avg_daily_volume)` → (mover) `compute_momentum_trigger(sym, gap_dir, frame)` → (ENTER)
  `momentum_to_entry` → `compute_day_tier_size(track=B)` → `place_entry`. Inside the SHARED per-tick API
  budget + the shared concurrency cap 3.
- **PART-2 HARD CONTRACTS surfaced by the Part-1 adversarial gate (write into the runner before it goes live):**
  - _(SUPERSEDED 2026-09-23 for B2: the daily context is SPLIT-ADJUSTED, not RAW — see BOARD ROUND 1 item 4 below; the RAW text had the split direction backwards.)_
  - **B2 (RAW basis):** `prior_close` + `avg_daily_volume` MUST be fetched on the SAME RAW/unadjusted basis
    as the RAW 5m bars — pin the daily fetch to `adjustment=RAW` (or use `fetch_bars_window` for the prior
    day). An adjusted `prior_close` makes a split read as a −50% false gap. (Backstop: the RAW/from-open
    momentum trigger declines a non-move, so this is a wasted-eval + mis-direction hazard, not a bad trade.)
  - **API budget (adversarial N5/fwd):** `build_session_frame` is the first RTH caller of `fetch_bars_window`
    (no TTL cache, shares the global `_rate_gate`) — each screened mover costs a windowed fetch + a daily
    fetch per tick; the runner's `DAYTRADE_MAX_API_CALLS_PER_RUN` budget must account for the Track-B loop and
    update the `fetch_bars_window` docstring ("offline research only" becomes stale).
  - Half-day RVOL denominator (N2) is a fixed 390 min — understates RVOL on early closes (fewer entries, safe
    direction); tune with the clock's session length when convenient.
- Flip `DAYTRADE_TRACK_B_ENABLED=False→True` + add `DAYTRADE_TRACK_B_UNIVERSE` to config (config full-read).
- Extend `scripts/day_tier_preflight.py` with a Track-B pass (the mandatory pre-flight sim, "deployed ≠ ready").
- **THE B SUB-KILL — the one board fork (Open Question Protocol, convened before Part 2 ships):**
  - **Option A — ship the per-track B sub-kill now:** stamp `track:"B"` into the durable entry/exit log +
    add a SECOND, tighter, ADDITIVE trigger in `tier_kill_check` that sums B-only realized+unrealized loss
    and latches the SHARED halt at −20%×(equity×0.0525). Honors §7b.6 verbatim; tighter, B-independent
    bound; more risk-path surface (touches `day_tier_logger` + `tier_kill_check`).
  - **Option B — rely on the existing outer bounds for B's first outing, B sub-kill as fast-follow:** B is
    ALREADY bounded by 1%-per-trade risk, the shared −5% tier kill, the 3-concurrency cap, and the 7%
    account kill (max B damage before the tier halts ≈ −5% of equity ≈ −$135 on ~$2.7k). Smaller risk-path
    diff; the declared-but-unwired `DAYTRADE_TRACK_B_KILL_PCT` becomes wired next increment.
  - **Recommendation to the board:** Option A. The B sub-kill is a ruin guard (SAFETY, not perfectionism →
    the PROFITABLE>PERFECT / velocity carve-outs do NOT license deferring it), B is genuinely unvalidated
    with halt-tail risk, and the per-track stamp is cheap + additive (only ever kills EARLIER, never loosens
    anything). "Documentation is not enforcement": a declared-but-unwired kill is the exact anti-pattern.

## SAFETY ENVELOPE (unchanged): 7% account kill, −5% tier kill, cash-only Track B, paper=True, data tiers,
code-correctness gate. Part 1 touches none of it (pure). Part 2's B sub-kill only TIGHTENS.

---
## PART 2 — GO-LIVE (RISK-PATH) — RAFAEL DECISION: **OPTION B / SPLIT** (2026-09-22, "ship it live now")

**The fork (B sub-kill) was convened board + Gro + GAI (Open Question Protocol).** Board 2–0 SPLIT (Thorp/
Taleb risk-asymmetry seat + Peterffy/Beck/Kim reliability seat, both after reading the actual kill code);
Gro + GAI both OPTION A. Rafael chose **OPTION B / SPLIT**: ship Track B LIVE now under the EXISTING
survivable envelope + add the observability `track` stamp, and wire the per-track B sub-kill trigger as an
immediate FAST-FOLLOW validated against B's first real labeled P&L (never blind kill code on day one).
Board rationale (decisive): the outer envelope already bounds B (1%/trade + shared −5% tier kill + 3-conc +
7% account) → survivable; you CANNOT validate B-attribution kill code before B has traded; and editing the
safety-critical `tier_kill_check` (which false-tripped ~2 days Sept 2026) with untested attribution on B's
first live day is a worse tail than the ~$80 of bounded paper drawdown the sub-kill would save. Gro/GAI's
valid concern ("don't leave a declared-but-unwired constant / don't let B halt A") is folded in: the config
constant is marked **STAGED (not a lie)**, the stamp STARTS attribution, and the validated trigger is the
next increment.

### THE PART-2 DIFF (files, each full-read this session; RC-checked; risk-path gate)
1. **strategy/day_tier_logger.py** — `log_entry_fill(..., track: str = "A")` writes `rec["track"]=track`
   (ADDITIVE: existing Track-A callers default "A"; readers use .get()). Add `track` to
   `open_trades_from_log()`'s returned dict (from the entry_fill event) for the fast-follow. No control-flow
   change. (Board-confirmed additive.)
2. **execution/day_trade_manager.py** `place_entry` — pass `track=(size.get("track") or "A")` to
   `log_entry_fill`. One-line thread-through; the `size` dict already carries track from compute_day_tier_size.
   Also stamp `state[key]["track"]` for the fast-follow's future B-only unrealized sum. No other change.
3. **run_day_tier.py** — add a PARALLEL Track-B loop AFTER the Track-A loop, behind
   `config.DAYTRADE_TRACK_B_ENABLED` (no-op when False). Order (adversarial N1): per pre-registered Track-B
   symbol → `build_session_frame` → [SUPERSEDED: split-adjusted SIP close / IEX ADV, see BOARD ROUND 1 item 4] daily context (`_track_b_daily_context`, session-cached:
   `fetch_bars_window(TF_DAILY, now−35d, now)` RAW → prior_close = last close before today, avg_daily_volume
   = mean of ~20 prior days) → `screen_mover` → (mover) `compute_momentum_trigger(gap_dir, frame)` → (ENTER)
   `momentum_to_entry` → `compute_day_tier_size(track="B")` → `place_entry`. Track A runs FIRST and gets
   budget priority; Track B uses the SHARED per-tick trading-API `call_budget` + the SHARED 3-concurrency cap
   (place_entry enforces both). Per-symbol try/except (one symbol never aborts the tick). Data fetches are
   data-API (shared _rate_gate), not the trading-API budget.
4. **config.py** — `DAYTRADE_TRACK_B_ENABLED = True`; add `DAYTRADE_TRACK_B_UNIVERSE` (the pre-registered
   liquid list, promoted from the Part-1 module default); annotate `DAYTRADE_TRACK_B_KILL_PCT` as **STAGED —
   declared + design-nested (validate_config line 1041 still checks it) but the LIVE trigger is the
   fast-follow increment** (closes the "declared-but-unwired = a lie" concern per the board). validate_config
   passes unchanged (verified: nothing requires the flag False; the sub-kill nesting assertion still holds).
5. **scripts/day_tier_preflight.py** — add a Track-B read-only pass (mirror the runner loop, stop before
   place_entry) so the mandatory pre-flight sim ("deployed ≠ ready") covers Track B.
6. **data/fetcher.py** — update `fetch_bars_window`'s "offline research only" docstring (the Track-B runner is
   now an RTH caller; it shares the global `_rate_gate`). Doc-only.

### FAST-FOLLOW (next increment, separately gated): the per-track B sub-kill trigger — parametrize
`_realized_loss_today(track="B")` + a B-only unrealized sub-sum (needs `track` in the target state, added in
#2) + a second additive latch in `tier_kill_check` at −20%×B-budget, VALIDATED against B's first real labeled
numbers, behind its own kill flag, fail-closed on untagged/pre-stamp events (reuse the existing L1406 branch).

### PART-2 SAFETY: envelope UNCHANGED. Track B rides the existing daytrade-tagged machinery (reconcile,
flatten, EOD force-flat, −5% tier kill, gross cap, 3-conc, OCO stop+2R, min-stop ATR gate, cash-only 5.25%
budget). The stamp cannot regress any kill (never touches tier_kill_check). Gate: full-read (done) + 10-pt +
RC + masked-loss board + Gro + GAI + cold-2nd + pre-flight sim + FINAL preship on the diff.

---
## PART 2 — 2026-09-23 CORRECTIONS (fresh account; every gate re-run from Step 1 per C-1/C-7)

The prior account's staged Part-2 draft was carried over as a DRAFT only. The Step-1 full read of every
touched/called file + at-source probes found TWO ship-blocking defects the draft (and the Part-1 gate) missed:

### DEFECT 1 — Track B could never see a live bar (would ship dead) — VERIFIED AT SOURCE
`build_session_frame` + the daily context call `fetch_bars_window`, which pinned `feed=SIP`, with `end=now`.
Live probe on the OCI box (2026-09-23 02:28 ET, deployed venv): `fetch_bars_window(NVDA, 5Min, now-1d, now)`
→ **0 rows**, APIError `{"message":"subscription does not permit querying recent SIP data"}`; the same call with
`end=now-16m` → 192 rows. The data plan serves SIP only for SETTLED history; IEX is the only real-time feed.
Every RTH tick would have returned `None` → Track B silently never trades ("deployed ≠ ready").
**FIX:** `fetch_bars_window(..., feed="sip")` gains an explicit feed (default SIP — research callers
unchanged; unknown feed → honest-empty). `build_session_frame` requests `feed="iex"` (real time).
**RVOL basis (measured, same probe session):** IEX 5m bars had **0 missing bars** across 2 full sessions for
all 15 names (the halt/contiguity guard will not false-trip); IEX volume = **1.9%–5.0%** of consolidated per
name, day-to-day CV 0.13–0.27; IEX-vs-SIP daily close diff ≤0.14% (MU one day 0.69%). Therefore the
daily context uses **prior_close = settled SIP close** (official, window ends today 00:00 ET — allowed) and
**ADV = mean of the last 20 IEX daily volumes** (same feed basis as the IEX frame numerator; a SIP ADV would
read every name as ~0.03× RVOL and nothing would ever qualify). Cached once per trading day in
`data/cache/day_tier_track_b_daily_ctx.json` (atomic; approved cache dir; a replay `--asof` never writes it).

### DEFECT 2 — Track B would have been risk-sized ONTO MARGIN (a safety defect) — VERIFIED AT SOURCE
`day_trade_manager._bounded_entry_qty` (hairpin fix 2026-09-18) sizes from RISK:
`safe_qty = min(risk_qty, notional_qty)` (lines ~341-344) — `requested_qty` is a validity check only, NOT a cap.
So a Track-B entry whose cash budget affords 1 share would wire at the 1.5%-risk size up to the $1,000
thin-name cap, on margin. `config.DAYTRADE_TRACK_B_CASH_ONLY = True  # HARD` was referenced by NO code
(grep-verified). Tail it guards: a halted mover reopening 20% through its stop on $1,000 notional = −$200 ≈
−8% of equity — past the 7% account kill in one trade. Under the North-Star carve-out this is a WIDENED
DOWNSIDE (safety defect), not calibration perfectionism.
**FIX:** `_bounded_entry_qty(..., track="A")`: when `track == "B"` and `DAYTRADE_TRACK_B_CASH_ONLY` (default
True; a non-bool reads truthy = cap ON), `safe_qty = min(safe_qty, requested_qty)` — min-only, never up-sizes.
`place_entry` computes `_track` from the size dict BEFORE the wire-cap call and passes it. The preflight
passes `track="B"` so the sim cannot drift. (Honest consequence, stated to Rafael: at ~$2.5K equity the
Track-B budget is ≈ $132/entry → Track B trades ~1 share and only names priced ≤ ~$132 — today NFLX, UBER,
SMCI of the 15. Raising B's budget/allowing margin is a SEPARATE board-gated risk-path decision.)

### ALSO IN THIS DIFF
- `track_b_in_window(now)` — the runner evaluates Track B only ~09:50–11:05 ET, DERIVED from the momentum
  trigger's own bar bounds (`_MIN_FRAME_BARS`, `_SESSION_CUTOFF_BARS`; one bar of slack each side so neither
  bar-publication convention loses a tick). Outside it every symbol WAITs anyway; skipping saves ~15 data
  calls/tick. (Correction to the prior record: `_rate_gate` is PER-PROCESS — the runner does not share the main
  bot's gate. Measured: 0 bar-fetch 429 lines in `day_tier_runner_cron.log`; 1 option-fetch 429 in
  `mtf_bot.log` since 09-09 — current load is fine.)
- Preflight: pre-existing sim-drift fixed (Track-A wire cap omitted `symbol=sym`, so the sim treated every
  name as thin while production passes the symbol); Track-B pass mirrors the window gate; `--asof` replays a
  past session for Track B (the mandatory pre-flight sim can then exercise the FULL B path pre-market).
- Config comments corrected (risk basis is 1.5%, not "~1%"; Track B per-entry bound = the cash cap).

### RULE-B ROUTING (size / frequency / concurrency) — declared input, reviewer must verify independently
Frequency: + (new Track-B entries, bounded by the SHARED 3-concurrency cap and the window). Size: per-entry
notional ≤ cash budget (≈5.25% equity); the cash cap is a NEW min()-only constraint. Concurrency: shares the
existing cap. → NON-ZERO (frequency) → RISK-PATH → masked-loss board seat + Gro + GAI + cold-2nd (Rule E).

### RULE-C FRONT-LOADED SIMULATION (2026-09-23) — the real pipeline code, replayed
Script: `logs/track_b_sim_2026-09-23.py` · output: `logs/track_b_sim_2026-09-23.json` (run on the OCI py3.10 venv
against the Part-2 working tree). Method: every 5-min tick inside `track_b_in_window` for 40 sessions
(2026-07-28 → 2026-09-22) × the 15 pre-registered names; `fetch_bars_window` served from a prefetched cache
returning COMPLETED bars only (no lookahead); fill = next bar OPEN only if within the marketable limit;
exits on SIP 5m bars (stop-first on a both-touch bar, gap-through fills at the open), OCO target at 2R, else
EOD force-flat at 15:40 ET; min-stop gate ATR leg applied (spread leg not reproducible historically); one
Track-B entry per symbol per day; concurrency cap 3; equity $2,512.36.
(1) CASES: 40 sessions spanning the late-July tape, the Aug drawdown days and Sept; 15 names; longs + shorts.
(2) RESULT: 23 signals → 17 trades (6 blocked by the min-stop-room gate; 0 no-fill; 0 concurrency blocks);
    0.43 trades/session, 14/40 sessions traded; win 52.9%; mean +0.19R, median +0.03R, sum +3.23R;
    sd 1.21R → t = 0.65 (NOT significant — an honest "mildly positive, unproven" signal); exits 6 stop /
    4 target / 7 EOD; worst −1.0R (no gap-through worse than the stop in-sample), best +2.0R. Longs 11
    (mean ≈ +0.65R), shorts 6 (mean ≈ −0.65R) — noted, NOT tuned on (n=6; LdP selection-bias guard).
    AT THE CASH CAP: only 4/17 were affordable (price ≤ ~$132) → 1 share each → +$1.39 over 40 sessions.
    EXPECTED LIVE EFFECT: ~0.58 Track-B signals/session (~0.43 trades/session), ~0.1 affordable 1-share trades/session, P&L ≈ $0 ±
    $2/day — Track B is live for DATA (labeled R-multiples for the sub-kill fast-follow + the per-feature
    (p,b) measurement), not yet for dollars.
(3) REVERSAL CRITERIA (any one → flip DAYTRADE_TRACK_B_ENABLED=False and review): (a) any single B trade
    realizes worse than −1.5R (a gap-through the cash cap is meant to contain — confirms the tail is live);
    (b) after 20 live B signals, mean R < −0.25 (R-multiples are size-invariant, so the tiny cash size does
    not blind this); (c) live signals/session > 2× the sim's 0.575 (a feed/basis bug — e.g. RVOL inflated),
    or 0 signals across 15 consecutive sessions while the day-tier runner is healthy (a dead-path bug).
(4) SIZE / FREQUENCY / CONCURRENCY DELTA: frequency + (~0.4 signals, ~0.1 affordable trades per session);
    per-entry notional ≤ the cash budget (≈5.25% of equity, ≈$132); concurrency = the shared cap 3.
    → risk-path (frequency) → masked-loss board seat + Gro + GAI + cold-2nd (Rule E).

### BOARD ROUND 1 (2026-09-23) — 3 cold seats, all APPROVE-WITH-CHANGES; every required change APPLIED
Seats: risk-asymmetry/masked-loss (Thorp/Taleb) · execution-reliability (Peterffy/Harris/Beck/Kim) · data-integrity
(McKinney/Katsuyama/Harris). No REJECT. Consolidated required changes → applied:
1. ONE Track-B entry per symbol per ET day (risk + exec seats: live re-entry after a stop was allowed; the sim assumed
   one) → runner `_track_b_symbols_today` (any state, track B, today's bar_id) skips the symbol; an attempt counts.
2. CASH-ONLY claim was overstated (risk + exec): it caps EXPOSURE, not funding — B shorts / a negative-cash account are
   margin-financed. → wording corrected (config + sizing); cap strengthened to an AGGREGATE Track-B budget cap: open B
   notional + this entry (at the ORDER price — closes the slippage overshoot) ≤ the B budget; missing/invalid budget →
   0 (fail-closed); only an explicit False disables it (None/0/""/"False" keep it ON); place_entry routes to the B cap
   if EITHER the size or the decision dict says "B".
3. Tick wall-clock budget (exec F1): the Track-B loop stops after 75 s; fresh `now` per symbol (freshness + entry_ref
   reflect real time).
4. Data basis (data F1-F3): daily context fetched SPLIT-ADJUSTED (prices + volumes on today's share basis — the prior
   B2 contract text had the split direction backwards; corrected); both SIP and IEX series must END on the previous
   trading session per the Alpaca calendar (unreadable calendar → fail closed, no cache); ADV requires ≥15 prior days.
5. Completed bars only in build_session_frame (data F4 + risk nit): a forming bar is dropped; freshness measured from
   the newest bar's END. This also removes the preflight --asof lookahead and makes live == the replay convention.
6. Preflight parity (exec F4): live mode runs the real `_min_stop_room_ok`; passes track_budget; an empty
   DAYTRADE_UNIVERSE no longer skips the Track-B pass. Track-B imports guarded (exec F5) — an import error disables
   Track B for that tick, never escapes run_tick.
Verification: statics clean (local + OCI py3.10 compile); tests/test_day_tier_track_b_live.py 31/31, test_day_tier_track_b
32/32; discover = only the pre-existing 6 fail + 9 error (baseline identical). Live at-source probe: previous session for
2026-09-22 → 2026-09-21 (correct); UBER ctx (70.84, 723,354 IEX sh/day). Rule-C sim RE-RUN on the final code: IDENTICAL
(17 trades, +0.19R mean, 4 affordable → +$1.39). Not adopted (noted): sizing reads module constants rather than
config.DAYTRADE_ALLOC_PCT/TRACK_B_PCT (values equal; a pre-existing silo — follow-up).

### BOARD ROUND 2 (2026-09-23) — all 3 seats APPROVE-WITH-CHANGES; every change APPLIED
- Risk seat: a Track-B lot known ONLY to the state file (failed log write / submit→log crash window) was invisible to
  the budget cap → place_entry passes same-day non-terminal state-only lots as `extra_b_lots` (Track-B sum only; Track A
  unchanged); "never under-counts" comment removed; remaining "cash-only / settled cash" wording fixed (config
  allocation block, sizing module docstring).
- Execution seat: (1) the per-day rule now counts at the ENTER SIGNAL (before sizing/min-stop/caps) and is PERSISTED
  in state (`_track_b_signal_day`) — identical to the replay's rule; (2) the previous session is resolved ONCE per tick
  and a calendar failure is remembered for the tick ("calendar_unavailable" → Track B skips, fail closed); deadline
  re-checked before the daily context and before place_entry (needs ≥35 s of the 120 s tick left, else deferred).
  Residual (pre-existing, noted as follow-up): the alpaca data client has no per-request HTTP timeout, so a single
  stalled socket can still overrun a tick — the broker-side stops keep protecting positions meanwhile.
- Data seat: freshness bound tightened to one bar + 120 s publication lag from the newest completed bar's END (the
  end-based 2-bar bound had let a name halted ~11 min through) with accept/reject boundary tests; cache carries a
  `basis` tag (a file written on any other basis is a miss); stale "RAW daily context" text fixed; B2 RAW contract
  marked superseded. Preflight: never writes the runner cache; mirrors the per-day rule.
Verification (OCI py3.10): compile clean; new module 35/35; Track-B helpers 33/33; discover = only the pre-existing
6 fail + 9 error; Rule-C sim re-run IDENTICAL (23 signals / 17 trades / +0.19R / 4 affordable → +$1.39).

### BOARD ROUND 3 (2026-09-23) — data seat APPROVE; risk + execution seats APPROVE-WITH-CHANGES → applied
- Risk: remaining "cash-only" labels (incl. the runtime sizing reason string → "B exposure-capped") reworded; a
  place_entry-path test for a state-only Track-B lot (budget used → 0 wired) + a counted-once test (log + state).
- Execution: the per-tick API-call budget is checked BEFORE a symbol is evaluated, so a capped tick never burns a
  symbol's daily shot ("api_budget" note); the calendar call is skipped when the tick budget is already spent; tests
  assert the marker is persisted BEFORE place_entry is called, that a failed marker write does not abort the tick,
  and that an api-budget cap leaves the shot unused.
- Data (nit): the Part-2 plan text's "RAW daily context" marked SUPERSEDED inline.
Verification (OCI py3.10): compile clean; new module 39/39; discover = only the pre-existing 6 fail + 9 error; Rule-C
sim identical (23 / 17 / +0.19R / +$1.39).

### FINAL GATE STATUS + ERRATA (2026-09-23, before commit)
ERRATA (adversarial pass found these overstatements in this record; the code is authoritative):
- "cash cap / cash budget / AT THE CASH CAP" above = the Track-B EXPOSURE (budget) cap — it bounds notional at ENTRY
  time (open B notional + the new entry at the order price ≤ equity × 15% × 35%); it does not act on mark-to-market
  drift of an already-open lot and does not change funding (a B short / negative-cash buy is margin-financed).
- Board round 1 item 5 "live == the replay convention" = the FRAME convention only; RVOL elapsed-time and fill
  timing still differ (the replay fills at the next bar open with zero latency and ticks every 5 min).
- Test counts above (31/31, 35/35, 39/39) were interim; FINAL = new module 49/49 alone, Track-B helpers 33/33
  (production py3.10); the rest of the day-tier suite = the same 6 failures + 9 errors as unchanged main.
- Track A: its CODE PATH is unchanged except that place_entry now also reads the durable log (for the Track-B
  state-only count) on every entry; its BEHAVIOR can change because Track B shares the 3-slot concurrency cap, the
  day-tier gross/BP room, the same-symbol/opposite-side guards on the 7 overlap names, the shared −5% tier kill,
  and tick time. This is by design (board 2026-09-22), not "Track A unchanged".
- A Track-B shot is used at the ENTER signal even when no order results (unaffordable size, min-stop, concurrency,
  an open day-tier lot on the symbol, tier killed, wire cap 0, time deferral, same-bucket Track-A key).
GATES (final staged bytes): statics clean (local + OCI py3.10 compile) · board: data seat APPROVE (R3); risk +
execution seats R4 items applied and MUTATION-VERIFIED · cold-2nd PASS (full diff; then fresh PASS on each later
revision) · 14 mutation runs each failed exactly the targeted test · adversarial PASS (no claim refuted, no blocker) ·
Rule-C replay re-run on the FINAL code (logs/track_b_sim_2026-09-23.json run_at 2026-09-23T11:07 ET): identical
23 / 17 / +0.19R / 4 affordable → +$1.39. Impact: behavior reaches only run_day_tier.py, the preflight,
place_entry/_bounded_entry_qty (gated on track B + the log read) and the logger's additive field; the main bot
(main.py / run_cycle / portfolio_tracker) calls none of the changed functions; research/trade_record_reducer keeps
fetch_bars_window's SIP/RAW defaults.
FOLLOW-UPS (not blockers): (1) the alpaca data client has no per-request HTTP timeout (a stalled socket can overrun a
tick); (2) count/reconcile "submitting" state records; (3) sizing reads module constants, not config ALLOC/TRACK_B
(the roadmap "scale alloc" must change the module); (4) the per-track B sub-kill (STAGED); (5) log shadow R for
unaffordable Track-B ENTERs (faster labeled data); (6) time-of-day RVOL profile; (7) the 6 stale + 9 polluted
day-tier tests (task spawned); (8) strategy/day_tier_momentum_trigger.py:68 stale "runner enforces the precise
clock-time cutoff" comment; (9) tighter reserve/ADV test brackets (cold-2nd nits).
