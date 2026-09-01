# DAY-TRADE TIER v2 — Design Record (BGGN-aligned)

**Date:** 2026-08-29 | **Status:** DESIGN ALIGNED (Board 4 seats + Gro + GAI/NVIDIA-substitute) — build LIVE next (no shadow, per Rafael mandate). Code build runs the full mandatory patch sequence + preship gate.
**Owner:** Rafael (Chairman/CEO). North-Star lens applied (grow $2.5k→$25k; apply edge, bounded/survivable risk OK — not a large-fund preservation posture).

---

## 0. WHY THIS TIER (context)
The day-trade tier is the **growth engine** (agreed with Rafael). Naive raw-5-min strategies (ORB / fade / gap / VWAP) all backtested **breakeven** since April → the edge, if any, is not in raw 5-min price. The BGGN was convened on a structured design; this record is the aligned output. Tier taxonomy: **DAY-TRADE** (new, same-day, flat by close) · **INTRADAY** (the current confluence signal, holds 2-3 days — renamed from "swing" per Rafael) · **QHM** (quarterly holds) · **FOREVER-6** (never-sell, dark, paused as lower priority).

## 1. BGGN VERDICT — CONDITIONAL GO, BUILD LIVE
Convened per Rafael's explicit instruction ("involve the bggn and follow those protocols. It shouldn't just be the board"). Board seats: Simons (signal/regime/overfit), Harris (microstructure/dealer-hedging), Sosnoff/Sinclair/Nathan (options/gamma/vol), López de Prado (validation). External: Gro (gpt-oss-120b), GAI voice via NVIDIA option-C substitute (GAI 429 quota-blocked).

**Unanimous / strong-majority findings:**
1. **Kill the strict AND-gate → weighted SCORE / meta-label** (Simons + LdP). Three nested "all-must-agree" gates across 7-9 correlated names ≈ zero trades ≈ never reach significance. Reframed as **meta-labeling**: Layer A = SIDE (primary model); Layers B+C = whether-to-act + size (meta model). LdP: this is a sound primary/secondary split.
2. **Timeframe (Rafael's explicit audit): 30-MIN PRIMARY + 15-MIN TRIGGER — UNANIMOUS 3/3 (+ board 4/4).** 5m failed = noise dominates; 15m still fires on head-fakes 30m filters; **30m catches the genuine dealer re-hedge impulse** (dealers re-hedge on material moves + clock points, not every 5m). 15m = entry-timing trigger once 30m confirms.
3. **Confirm set is the weakest element.** 15m/30m/1H MACD are three lags of the SAME price series (collinear) — "three confirms" ≈ one confirm. Replace ≥1 with an **orthogonal** signal: relative volume / VWAP distance / the GEX read itself.
4. **Trade the UNDERLYING, not the leveraged ETF, where BP allows** (Harris + Sinclair). NVDL is pinned by NVDL's own thin option dealers, NOT NVDA's — the gamma edge lives in the underlying's chain. Use TSLL/NVDL/TQQQ/SOXL only when buying power is the binding constraint (cheaper per share on a $2.5k acct). Compute GEX on the underlying; express in whichever instrument BP allows; know the ETF costs wider spread + tracking error.
5. **GEX layer needs DTE/OpEx + time-of-day conditioning** (Sosnoff/Sinclair). Pin strength ∝ days-to-expiry (strong near monthly OpEx/0DTE, weak mid-cycle) and intraday clock (pins tighten after ~2pm ET; −gamma breaks are morning open-drive). Do NOT fire the same GEX logic every day regardless of the calendar. This conditioner — not a 3rd MACD — is what's missing.
6. **Single-name GEX SIGN risk** (Sinclair). "+GEX = pinned" is reliable for INDEX gamma; in single names, customer call-buying can leave dealers short gamma even at a nominal +GEX strike → sign can invert. Validate sign; lean on QQQ/SPY index gamma where the sign is dependable.
7. **Payoff-profile mismatch.** Pin/fade = high-win-rate/small-R; −gamma ride = low-win-rate/large-R. A flat ~0.7R for BOTH is wrong — two different bets, each must be individually profitable. Track fade-win-rate and ride-win-rate SEPARATELY.
8. **Allocation: START 10% of equity (~$245), scale to 15% after positive net-of-cost expectancy** (Board + GAI agree; Gro's ~28%/$688 is the absolute ceiling, not the start). The tier must sit INSIDE the 7% account budget (~$172/day) because intraday + QHM marks also count toward the account net kill. Binding constraint is the ~$650 maintenance cushion, NOT the kill switch — cap tier GROSS notional so a leveraged-name halt-gap can't force a liquidation.
9. **The ONE thing that must be true:** net of realized spread+slippage on the ACTUAL instruments, the GEX layer must add positive ORTHOGONAL expectancy beyond structural bias + entry — and survive a **shuffled-GEX-label control test** (shuffle GEX labels, re-run; if expectancy doesn't collapse, the "edge" is just long-Mag-7-beta-on-up-days = exposure, not alpha).
10. **First metric to watch:** realized cost-per-trade in R (arrival-price slippage vs assumed fill) — paper can't show it, hits leveraged ETFs hardest. Then GEX-split hit-rate (fade vs ride, separately) vs a **pre-registered DSR/expectancy kill threshold** (LdP).

**Sample-uniqueness warning (LdP):** 7-9 correlated Mag-7 names flat-by-close → on any up-day, outcomes are near-duplicates; "hundreds of trades" = a few dozen INDEPENDENT events. Deflate. 15m will show higher in-sample Sharpe partly because finer bars manufacture overlapping non-unique labels (artifact, not edge).

**Resolved divergence (no deadlock):** GAI wanted the quarterly horizon DROPPED (overfit); board wanted it KEPT as a coarse side-lean. **Resolution:** keep the full MA stack for SIDE selection (Rafael's explicit request), give the newest/least-tested horizon the LOWEST weight, prune it first if it shows no measured edge. Satisfies all three voices.

## 2. THE ALIGNED v2 ARCHITECTURE
**Meta-label, 3 jobs:**
- **SIDE (Layer A — structural bias, per name, daily):** full project MA stack — 13/30-EMA, 20/150/200/325-SMA, 10-week SMA, Faber 10-month SMA, + NEW quarterly horizon (lowest weight, prune-if-no-edge). Weighted score → LONG / SHORT / TWO-SIDED lean. NOT an intraday trigger (Simons: slow MAs are constant within a month; they set direction, not timing). Measured 2026-08-29 biases: MSFT/AAPL/AMZN structural-long; META/TSLA structural-short; NVDA/GOOGL two-sided.
- **WHETHER-TO-ACT (Layer B — GEX regime):** compute GEX on the UNDERLYING's chain (map to ETF by leverage×NAV only for expression). +gamma → FADE a failed wall-sweep back to pin/VWAP; −gamma → RIDE a wall close-through. Conditioned on DTE/OpEx + time-of-day. Sign-validated for single names.
- **ENTRY/SIZE (Layer C):** 30m primary confirm (13/30-EMA + one MACD) + 15m trigger + ONE orthogonal confirm (RVOL / VWAP-distance) + delta-of-signal. Size per meta-label conviction, bounded by tier allocation + gross-notional cap.

**Universe:** Mag-7 underlyings (AAPL/AMZN/GOOGL/META/MSFT/NVDA/TSLA) + leveraged trackers (TSLL/NVDL/TQQQ/SOXL) as a BP expedient only.
**Per-stock R:** separate fade-profile (high-win/small-R) vs ride-profile (low-win/large-R); per-name % targets (e.g. TSLA wider than AAPL). Flat by close.
**MRI does NOT apply to this tier** (Rafael). **Dynamic net-portfolio hedging:** day-tier shorts offset long-tier down days in the account 7% NET kill (not static gross).

## 3. RISK
- **Tier kill: 25% of day-tier allocated capital** (room to gather data, Rafael).
- **Account kill: 7%/day on NET equity** — excludes only locked QHM/Forever-6 unrealized marks; intraday + day-trade count fully. Fail-safe reverts to full equity on fetch failure (already in `execution/risk_manager.py:319`). CORRECTION from this session: do NOT exclude the intraday tier from the kill measure — intraday is liquidatable, so excluding it would MASK a real loss (Gro masked-loss seat).
- **Allocation: 10% start → 15% target.** At 10% ($245): tier stop $61 = 2.5% of account; 4 straight max-loss days ≈ 10% of account — bounded twice, does NOT blow up in 4 days. Gross-notional cap protects the ~$650 maintenance cushion.
- **6am daily per-tier available-capital snapshot** (Rafael) — off buying power (account trades on ~4x margin), not cash.

## 4. HEDGE-FUND-GRADE TRACKING (Rafael)
Per-trade decision-stack to `trade_events.jsonl` (Decision-Explainability doctrine): side + each layer score, GEX regime + sign, DTE/TOD, every sizing multiplier in order. TCA: arrival-price slippage in R, fill latency, realized vs assumed. Separate fade/ride hit-rate + expectancy. Shuffled-GEX-label control. Pre-registered DSR/expectancy kill threshold before go-live.

## 5. OPEN / BUILD SEQUENCE (each step = full patch sequence + preship gate; LIVE not shadow)
1. **`data/gex.py` universe** — populate GEX for the Track-A eligible set (routing gate §7) + resolve leveraged trackers to the underlying's regime. A staged 3-edit diff (add Mag-7 to the universe + `_ETF_UNDERLYING_MAP`) passed statics + cold-2nd PASS + Gro APPROVE + board APPROVE-WITH-CHANGES. **CLAIM CORRECTION (verified at source, board+cold-2nd):** the two live equity consumers `kelly.py:352` and `run_cycle.py:1590` BOTH hardcode `get_gex_regime("SPY")` — they do NOT pass the entry symbol, so computing Mag-7 GEX changes ONLY the advisory options overlay (`options_scanner.py:1139`, never places an order), NOT equity sizing or Layer-8 MIN_SCORE. The earlier "fresh Mag-7 entries get real GEX multipliers" framing was WRONG. The staged diff is HELD (not shipped) pending the §7 routing-gate universe (dynamic, not a hardcoded Mag-7 list) so it isn't re-touched. **The REAL risk-path change** = a future diff wiring the entry symbol into kelly.py:352 / run_cycle.py:1590 (that is when NEGATIVE⇒×1.30 first reaches fresh single-name entries) — full masked-loss gate at THAT time. **STATUS CORRECTION (2026-09-01, verified at source):** this gex.py universe work is NO LONGER "held" — it SHIPPED as **#203 (`fb8b42f`)** ("per-symbol GEX universe (Mag-7) + tracker resolution — day-tier foundation"): `_ETF_UNDERLYING_MAP` + Mag-7 in `data/gex.py` (`_DAYTRADE_UNDERLYINGS`, `_raw_universe`), `get_gex_regime(symbol)` per-name live. The day-tier's per-symbol GEX foundation is DONE.
2. **Dynamic 4-tier −7% net kill** — corrected design above. RISK-PATH → full gate + masked-loss seat.
3. **Day-tier module (Track A GEX-core FIRST)** — meta-label engine (A side / B GEX-whether / C entry), 30m-primary/15m-trigger, orthogonal confirm, DTE/TOD conditioning, separate fade/ride R. Per-day TP target (§ below).
4. **Track B (dynamic movers) — logging-only first**, then live-small once guarded (§7).
5. **6am per-tier capital snapshot** + **hedge-fund TCA tracking** + **shuffled-GEX control** + **pre-registered kill threshold.**
6. Resolve the **21-EMA question** (Rafael 2026-08-29: ADD the 21-EMA — distinct from 20-SMA) and formally add the **quarterly horizon** (lowest weight, prune-if-no-edge).

**TAKE-PROFIT (Rafael 2026-08-29):** per-day tier target ~+20%/DAY built from a SERIES of small full-25%-size wins, NOT +20%/trade. TP is STRUCTURAL — the gamma pin centroid / VWAP magnet for a fade; a trailing runner for a −gamma ride. Stop stays TIGHT relative to pin distance so R≈1:1 (small-win math needs it). 25% tier stop = the daily circuit-breaker after a bad streak. Fade profile (high-win/small-R) is the bread-and-butter; ride only when GEX flips −gamma.

## 7. TWO-TRACK DYNAMIC UNIVERSE — BGGN ALIGNED 2026-08-29 (Board 4 seats + Gro + GAI/NVIDIA)
**Fork:** fixed Mag-7 "may miss the day's momentum movers" (Rafael). **Verdict: two-track = APPROVE; movers INGESTED-AND-ROUTED, never blindly traded.** The split is mechanical: pin-fade is mean-reversion, a mover is trend — opposite-sign, can't blend into one score (Simons/LdP).
- **Track A — GEX-core** (Mag-7 + liquid large-caps): the pin-fade/ride strategy (§2). Trades LIVE first.
- **Track B — dynamic movers** (any pre-market gapper): PURE-MOMENTUM (MA stack + VWAP + opening-range + rel-vol, NO gamma). Logging-only first → live-small once guarded.
- **ROUTING GATE = OPTIONS LIQUIDITY, not market cap** (Harris): Track A iff optionable-with-weeklies AND near-ATM front-weekly OI ≥ ~2,000–5,000 contracts (anchor) AND ATM option spread ≤ ~8–10% of mid (anchor) AND underlying $-ADV ≥ ~$500M–$1B (anchor floor). Fail any → Track B IF it clears the pre-registered mover screen (gap %, rel-vol ≥ ~3–5×, price ≥ ~$3–5, non-micro-float, spread ≤ ~0.3–0.5%). Fail the exclusion floor → traded by NEITHER. Anchors calibrate live. (Gro: OI≥10k/spread≤0.05Δ/IVR≥40; GAI: OI>1k/spread<1% — same shape, different anchors.)
- **SEQUENCE (unanimous): GEX-core LIVE now / mover-screen LOGGING now / Track-B live-SMALL once (a) guards built AND (b) pooled-setup expectancy positive.** NOT shadow-benching — Track B is genuinely unvalidated + its ruin guard is unbuilt (doctrine carve-out); short instrumented ramp, not a months bench.
- **Track B entry = FIRST-PULLBACK-ABOVE-VWAP after a confirmed opening drive** (Asness — highest expectancy; raw ORB pays the fakeout tax). VWAP = hard trend filter (no Track-B long below VWAP, ever). Rel-vol floor ~3–5×. Time-of-day cutoff ~first 60–90 min.
- **Track B #1 killer = LULD HALT** (stop can't fire mid-halt; reopens ~10% lower). #1 guard = **HALT-SURVIVABLE SIZING** — size so a halt-reopen gap-through stays inside the 25% tier kill (a sizing guard, not an entry guard — you can't guard the halt, only its damage). Plus: marketable limits never market orders; hard spread cap; price/float exclusion floor.
- **Daily-changing universe = selection-bias machine (LdP, the single biggest risk):** the SAMPLE IS THE SETUP, not the name — pool every "gap that held VWAP" across all movers, measure the MECHANIC. Pre-register the screen as a ≤3-param rule; NEVER tune on live P&L. Shadow full-screen-output vs traded-subset (the gap = cherry-picking, quantified). Honest limit: Track B can't be DSR-validated for a long time on a $2.5k account → bounded exploration (ruin guard substitutes for statistical proof while n small).
- **Track B's ONE must-be-true:** buy CONFIRMED CONTINUATION (VWAP-held first-pullback), never the raw gap. Raw gap-continuation has a NEGATIVE base rate after spread+slippage+halt; conditioning on "the gap that held VWAP" flips it positive.
- **CADENCE:** fast 2–3 min day-tier loop for EXECUTION only (fill/re-peg, halt-detect-before-sizing, arm the mover trigger); the SIGNAL stays on 15m/30m bar-close (Simons: re-deciding a 30m signal every 2 min = trading noise). Loop firewalled from re-firing entries mid-bar. (GAI leaned 5–10 min; resolved by the execution-vs-signal split.)
- **FIRST METRICS:** Track A = hit-rate of the gamma-regime CLASSIFICATION (did +gamma actually fade to pin? the whole edge rests on the call being right). Track B = per-trade implementation shortfall (fill vs signal-bar mid) + pooled-setup expectancy with same-day clustering flagged + traded-vs-full-screen gap.

## 7b. RAFAEL DIRECTIVES 2026-08-29 PM (supersede where noted)
1. **PER-SYMBOL GEX is the point (corrects my SPY-only framing).** The day-tier NEEDS GEX levels for EACH Mag-7 name + each pre-market mover it may trade — not SPY's. The NEW day-tier module reads `get_gex_regime(symbol)` PER NAME (which the gex.py universe expansion enables); it does NOT rely on the existing `kelly.py:352`/`run_cycle.py:1590` SPY-hardcoded book-wide edge mult (that stays as-is for the legacy intraday tier — a separate future question whether to make THAT per-symbol). So the gex.py universe work is Track A's foundation, NOT "held/inert" — it feeds the day-tier's per-symbol reads. Scope its universe to the §7 options-liquidity routing gate.
2. **Track B is BIDIRECTIONAL.** Long a gap-UP that holds VWAP (continuation up) AND SHORT a gap-DOWN that fails at resistance / loses VWAP (continuation down) — the exact inverse mechanic. Symmetric by design.
3. **Track B goes LIVE from day 1 — OVERRIDES the board's logging-first sequencing (§7).** Rafael accepts Track B's risks ("top of the profit spear — front-line volatility"). CARVE-OUT (unchanged): the board's SAFETY guards are NOT overridden — halt-survivable sizing, spread cap, price/float floor, marketable-limits-never-market MUST be built in from day 1. What's overridden is the *validation-caution* (logging-first), not the *ruin guard*. Because there's no backtest, the performance-monitoring + red-day audit (below) must be built BEFORE/WITH go-live.
4. **HEDGE-FUND-GRADE red-day audit — 100% accuracy, nothing less.** On any red day we must reconstruct EVERY aspect of what went wrong: every entry/exit/skip decision-stack, per-track P&L attribution, per-setup expectancy, slippage/TCA, halt events, the GEX/price-action state at each decision. This is the Decision-Explainability doctrine taken to full fidelity for the most volatile tier. Build it into Track B (and the whole day-tier) as a first-class system, not an afterthought.
5. **PRICE ACTION is North Star 1A.** The bot must be dynamically built to ALWAYS refer back to price action — "the only thing that matters at the end of the day." Every day-tier signal/gate resolves against live price action first; indicators inform, price action decides.
6. **25% split — RESOLVED (Board-only: Thorp/Taleb/Dalio, unanimous DIVIDE).** Structure: **Track A ~65% / Track B ~35%** of the day-tier allocation. Sub-kills: A = 25% of A; **B = 20% of B** (tighter — unvalidated + halt tail; "2 bad movers = stop B"). Nesting: sub-kills sum < tier kill (25%) < account kill (7%). **CRITICAL GUARD — Track B is CASH-ONLY, NO MARGIN**, per-name notional ≤ its cash sub-budget (stress: $215 B-budget, −40% halt-reopen = −$86 contained; on 4× margin = −$344 breaches account kill 2×). **One-way fungibility:** A may borrow B's idle budget on a strong-A/no-mover day; B may NEVER borrow A's. **A/B are naturally uncorrelated (mean-reversion vs momentum) — B is a diversifier (Dalio "Holy Grail"), NOT just tolerated;** watch-flag: high-VIX trend days collapse A(−gamma ride)+B(momentum) to correlation +1 → MONITOR realized A/B P&L correlation + B's isolated win-rate/payoff (validate/cut after ~20-30 movers). **ALLOCATION SIZE (separate dial):** start day-tier at 15% of equity (~$368 → A $239 / B $129 cash-only), scale toward 25% as B validates. Encoding any of this into config = risk-path → full board + Gro + GAI + cold-2nd before ship.
7c. **Entry-mechanic — RESOLVED (Full BGGN: board + Gro + GAI, unanimous RIGHT-WITH-REFINEMENT).** Direction kept (blind raw-gap = negative-EV; confirmation flips it positive). Three refinements: (a) **VWAP alone is too weak** — early on a gap-up "above VWAP" is trivially true; the real signal is a **HIGHER-LOW that holds the STRUCTURAL level** (prior-day / pre-market high, opening-range high) on **sustained relative volume** — VWAP is confluence, not the gate. (b) **TWO entry modes, not pullback-only** (pullback-only forfeits the runaway right-tail winners + adverse-selects the choppy middle): **DRIVE mode** = opening-range hold + prior-level reclaim in first 1-5 min; **PULLBACK mode** = first higher-low holding the structural level. (c) **The single best continuation-vs-fade tell = THE RETEST:** higher-low(long)/lower-high(short) that HOLDS the broken level on volume = continuation (take it); round-trip back through the level = fade (reject/short). (d) **SHORT SIDE ASYMMETRIC** — borrow/locate limits, squeeze, LULD halt-UP (reopens past stop) → run shorts SMALLER + TIGHTER, exclude low-float halt-up candidates. Gate universe on RVOL>>1 + catalyst before any entry fires.
8. **NO MORE RESUME CRONS** (Rafael 2026-08-29): Anthropic auto-resumes on usage limits; stop arming +5h cron backstops. Prior usage-limit-resume-cron memory is SUPERSEDED. (Cron da23a015 deleted.)

## 5b. TRACK-A BUILD FINDING (2026-08-30, gate-surfaced — reshapes the build order)
Built `execution/day_trade_manager.py` (full Track-A engine, 642 lines) + `data/gex.py get_gex_levels`.
Four cold-2nd rounds + a masked-loss board pass. **The DECISION engine is gate-VALIDATED** — structural
MA-side bias (13/21/30-EMA + 20/150/200/325-SMA + 10wk + 10mo + quarterly), per-symbol GEX regime +
`get_gex_levels`, fade-to-pin/ride-the-break direction logic, 30m/15m + orthogonal-RVOL confirm, and the
budget-bounded min()-only sizing all PASSED. Fixed en route: a decorative sub-kill (now real: Alpaca-marked
P&L accrual + trip + flatten), unprotected-entry-on-stop-failure, reconcile no-op, missing orthogonal confirm.

**THE BLOCKER (real, not fixable in the module alone):** the Track-A ORDER-EXECUTION layer (close / flatten /
emergency-undo) cannot be tier-safe with the shared broker's tools. `close_position(symbol)` does a FULL-symbol
close (tier ignored under `OWNERSHIP_GUARD_ENFORCE=False`, and `"daytrade"` isn't a registered tier), and the
close/partial-close machinery does a **symbol-blanket `cancel_open_orders_for_symbol` on a 40310000** — so a
day-tier close of a Mag-7 name the INTRADAY or QHM tier co-holds would **clobber the co-resident tier's shares
and/or cancel its live protective stop**. This is the exact Movers/QHM cross-strategy collision the project
retired the Movers bot over ([MOVERS-RETIRED], July): a same-symbol second strategy sharing the fungible Alpaca
lot with no tier-safe close. A same-day tier trading Mag-7 (which the intraday tier also trades) hits it head-on.

**NEXT BUILD (the real prerequisite — retires the Movers/QHM debt):** tier-safe order infra —
(1) register `"daytrade"` in `execution/ownership_guard.py`; (2) **qty-bounded partial closes** (close only
the day-tier's own qty, via `partial_close_position` + fill-qty confirmation via `broker.get_order`);
(3) **tier-aware blanket-cancel** (the 40310000 retry must cancel only the CLOSING tier's own blocking orders,
never another tier's protective stop) — a hotspot `broker.py` change touching ALL tiers → Feature Design
Protocol + full BGGN + masked-loss seat before code. Then the validated Track-A engine's entry/close/flatten
sit safely on top. ~~The decision-engine code is preserved (session scratchpad + working tree, UNCOMMITTED).~~
**LOST — CORRECTION 2026-09-01 (verified: `git fsck`/stash/tree/scratchpad all searched, not found):** the
642-line validated `day_trade_manager.py` was NEVER committed to git; the prior session's ephemeral scratchpad
that held it has been cleaned, so the code is GONE and must be **REBUILT from this design spec** (§1-§7 fully
preserve the aligned architecture; the tier-safe infra Diff A #206 + Diff B #209 + the pre-close sweep #216 +
the gex.py per-symbol universe #203 are all SHIPPED, so the rebuild has a safe foundation). **HARD LESSON
(commit-inert rule):** a gate-VALIDATED build must be COMMITTED — inert/unwired if needed, exactly as Diff A/B
shipped inert — never left in an ephemeral scratchpad. Validation without a commit evaporates at session end.

## 5c. TIER-SAFE ORDER INFRA — BGGN-ALIGNED DESIGN (2026-08-30, board Peterffy/Harris/Taleb/Kim-Beck + Gro + GAI)
Resolves the §5b blocker so the day-tier (and any co-holding tier) can close/cancel WITHOUT clobbering
a co-resident tier on a shared Mag-7 lot. **Verified mechanism:** orders already carry their owning tier
in the client_order_id (`ownership_guard.make_coid` → `tier_of_coid`).

**ALIGNED DESIGN (all three voices):**
1. **Tier-aware cancel** — `cancel_open_orders_for_symbol(symbol, only_tier=None)`: when only_tier set, cancel
   ONLY orders whose tier_of_coid matches. **FAIL-TOWARD-INACTION (board hard rule):** an unattributable /
   untagged / legacy coid (tier_of_coid→None) is NEVER cancelled by the filtered path. Default None =
   legacy blanket, byte-for-byte unchanged.
2. **Qty-bounded close** — the day-tier closes only its OWN qty (`partial_close_position`), never the whole lot.
3. **SCOPED ownership check on the day-tier close path (Rafael YES 2026-08-30)** — Alpaca's held-for-orders is
   a MAGNITUDE check, not an ownership check (Harris/Taleb), so a qty-close can silently eat another tier's
   shares if the day-tier's own qty bookkeeping drifts (the RC-6/RC-8/Movers drift class, ZERO error surface).
   A scoped free-shares/ownership check on the day-tier's close closes it — WITHOUT flipping the global
   OWNERSHIP_GUARD_ENFORCE (board+GAI: disproportionate blast radius; **Gro's minority "enable the global flag"
   is OVERRULED — the scoped check addresses the concern without touching the other 3 tiers**).
4. **Blocked-close policy (Rafael YES 2026-08-30):** a correctly-BLOCKED close fires a CRITICAL alert + auto-
   retries closing only the FREE shares — the day-tier must NEVER silently ride naked overnight (Taleb; its
   whole premise is flat-by-close).

**SEQUENCING (Kim/Beck tidy-first — small reversible diffs):**
- **Diff A (in progress):** additive `only_tier=None` param on cancel_open_orders_for_symbol + register
  "daytrade" in ownership_guard (Tier/_TIERS/_TIER_CODE="DT", NOT _PROTECTED). INERT — nothing passes a tier
  yet. Call-site inventory done: all 5 callers pass None (blanket, unchanged); circuit-breaker stays blanket
  (Architecture Invariant #7). Ships on cold-2nd + statics + final Gro/GAI preship.
- **Diff B:** wire ONLY the day-tier's close/partial-close/entry-undo to pass tier="daytrade" (+ qty-bounded
  partial_close + fill-qty via broker.get_order). Full gate; leaves intraday/qhm/forever6 blanket calls untouched.
- **Diff C:** the scoped ownership check (#3) + blocked-close alert+retry (#4) + the desynced-ledger cross-tier
  test the board wants BEFORE the day-tier trades real size.
- **NOT in scope:** flipping global OWNERSHIP_GUARD_ENFORCE (separate, larger, all-tier decision).
- **The one test (board):** desynced-ledger — two tiers on one symbol, protected tier's stop holds its qty,
  day-tier requests a qty-close for a WRONG amount (drift) still within aggregate free-qty → must be caught by
  the scoped check, not silently succeed.

Then the §5b-held Track-A engine's entry/close/flatten sit safely on top → diff #2 (the module) ships → config/
wiring/enable → Track B → audit.

## 5d. OVERNIGHT-SAFETY REQUIREMENT — Diff B is necessary, NOT sufficient (2026-08-31, Rafael Q + Gro + GAI converged)
Rafael's question ("can an overnight gap-down trigger this? does the day-tier force flat-by-close?") surfaced a
candidate naked-overnight path Diff B alone does NOT close (DESIGN-ANALYSIS finding — Gro+GAI design-review
reasoning, NOT yet observed in a production log). Gro (gpt-oss-120b) and GAI (gemini-3.5-flash) INDEPENDENTLY
reasoned to the same sequence ("deadlock-grip"):
  1. ~15:55 ET the day-tier calls flatten_all(eod) on a Mag-7 name a CO-HELD tier (intraday/qhm) also holds.
  2. Alpaca returns 40310000 because the CO-HELD tier's protective stop reserves the shares.
  3. Diff B (correctly) cancels ONLY the day-tier's own orders → the co-held stop is left intact → the flatten
     RETRY re-fails → partial_close_position returns False (close does NOT execute).
  4. 16:00 ET: the day-tier's DAY stop expires (day-tier uses DAY stops, not GTC).
  5. Result: the day-tier position rides OVERNIGHT with NO protective stop → exposed to an overnight gap-down.
Diff B is the right FIRST layer (prevents clobbering the co-held stop — the Movers/QHM collision) but is
necessary, NOT sufficient, alone.

RECOMMENDED GATE (from this analysis; surfaced to Rafael 2026-08-31, awaiting his explicit confirmation) — the
day-tier should NOT trade real size until ALL of these are live + gated, folded into Diff C + the module (diff #2):
  (a) BLOCKED-CLOSE CRITICAL alert the moment an EOD flatten is blocked (already Diff C §5c point 4);
  (b) GTC PROTECTIVE-STOP FALLBACK — if a day-tier position cannot be flattened by the close, attach a GTC stop
      BEFORE the DAY stop expires, so a stuck position is NEVER naked overnight;
  (c) FORCE-LIQUIDATE-AT-OPEN — any day-tier position surviving past the close is force-closed at the next open
      (a recovery-registry state transition).
REJECTED (masked-loss / collision re-introduction): GAI's "order-dominance / let the day-tier cancel the co-held
tier's stop to force its own close" — that RE-INTRODUCES the exact cross-strategy stop-clobber Diff B exists to
prevent. The safe answer is a FALLBACK STOP on the stuck share, never overriding another tier's protection.
Full board + Gro + GAI + masked-loss seat design the (b)/(c) mechanics when Diff C / the module is built.

## 6. STILL OWED / BOOKMARKED
- Forever-6 LIVE + hybrid-margin (Rafael override of board cash-only) — PAUSED, lower priority than this growth engine.
- Monday allocation simulation (computed 2026-08-29): current book QHM $4,516 (LLY/GEV/GE) + intraday $1,503 (META/GOOGL); equity ~$2,455, BP ~$2,598, maintenance cushion ~$650. Day-tier 10-25% = $245-614; 25% tier stop = $61-153/day = 2.5-6.2% of account; 4 max-loss days = 10-17% of account (bounded).

## 7. PRE-CLOSE STOP-COVERAGE SWEEP — SHIPPED 2026-09-01 (PR #216, OCI `2b10e5c`)
Rafael-approved (one-page package) + board 3-0 (Reliability/Exec-risk-masked-loss/Quant) + Gro + GAI
APPROVE-WITH-CHANGES. Turns on the board-hardened but previously-INERT `reconcile_protection`
(execution/stop_protection.py) as a pre-close safety net: in the final `config.PRECLOSE_SWEEP_MINUTES`
(15) before the REAL close (Alpaca `get_clock().next_close` — half-day aware), `run_cycle` fires it
once/cycle to guarantee every open intraday/daytrade position has a live full-qty DAY stop before the
overnight GTC window (the 4:05 PM AH block). Verified LIVE on OCI: heartbeat `PRECLOSE-SWEEP: not-yet
(232.2 min to real close)` at 12:08 ET — hook exercising clean; the sweep itself first fires today ~15:45 ET.
Board-required upgrades folded in: (a) forever6 excluded (fails CLOSED, `_forever6_symbols`) — reconcile
reads full net not net−F6, so it would otherwise stop the never-sell book; (b) account-wide
`get_open_positions` (ONE fetch, was per-symbol) so N serial socket timeouts can't stall the 12-min
watchdog near the close. Safety verified at source: runs AFTER check_exits (never delays an exit);
`allow_cancel_blocking=False`; DAY stops expire 16:00 → no 40310000 collision with the 16:05 AH GTC block;
7% kill switch is equity-derived → a cover cannot mask a loss. 46 tests pass (+ new `test_forever6_excluded`
+ positions N→1 assertion); cold-2nd PASS; FINAL Gro+GAI preship APPROVE on the exact diff.

### 7a. Phase-B tracked (NOT blockers — do before the daytrade tier ARMS)
- **Reverse-fill residual (Exec-risk seat):** a resting profit-limit invisible in one `get_open_orders` read →
  reconcile places/covers, `_raw_close_position` (Alpaca DELETE-position) leaves the limit resting, and the
  next cycle short-circuits at `already_protected` BEFORE the `other_cov` check → on a reversal the limit
  over-sells. Fix: re-check `other_cov` even on the `already_protected` branch, OR cancel the resting limit
  before `_cover`. Pre-existing to the hardened `_cover`; low-prob (lag + reversal); does NOT mask a loss.
- **Daytrade `tier=` plumbing (Quant seat):** reconcile submits with the default `tier="intraday"`, so a future
  `cancel_open_orders_for_symbol(only_tier="daytrade")` won't recognize a reconcile-placed daytrade stop. Plumb
  `tier=` from the trade into the submit BEFORE the daytrade tier arms. Nit today (DAY stop expires 16:00).
- **Mixed-tier trade-off (Quant seat):** excluding a mixed F6 ticker drops reconcile coverage of its INTRADAY
  leg too (conservative v1 — an unmanaged intraday leg beats an F6 oversell). Tier-aware net (`net − F6_qty`)
  is the Phase-B refinement; do NOT "fix" the exclusion into a full-net stop.

### 7b. DAY-TIER ENGINE — Rafael's binding requirements (2026-09-01, flagged at pre-close approval)
The day-tier ENGINE (separate build after this) runs its scan **every 2-3 min**, NOT the 5-min main cycle
(smaller name universe → faster). Two HARD requirements for that engine's design session (Feature Design
Protocol + board + Gro/GAI gate BEFORE code):
1. **ANTI-SILO (bidirectional confluence):** the 2-3 min day-tier must UPDATE the broader shared confluence
   indicators the 5-min scan consumes (and read them) — overlapping indicators feed each other, not siloed.
   Per the ANTI-SILO MANDATE: adds-signal + fails-safe + stays-testable at every cross-use.
2. **API-BUDGET ISOLATION:** the faster loop's added API volume must NOT starve the 5-min scan's ability to
   make its calls (rate-limit headroom + no thread contention). Design must bound/schedule day-tier API calls
   so the main cycle's T1 fetches are never crowded out. (The pre-close sweep already respects this — one
   account-wide orders fetch + one positions fetch per in-window cycle, no per-symbol calls.)
