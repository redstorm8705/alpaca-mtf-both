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
1. **`data/gex.py` universe** — populate GEX for the Track-A eligible set (routing gate §7) + resolve leveraged trackers to the underlying's regime. A staged 3-edit diff (add Mag-7 to the universe + `_ETF_UNDERLYING_MAP`) passed statics + cold-2nd PASS + Gro APPROVE + board APPROVE-WITH-CHANGES. **CLAIM CORRECTION (verified at source, board+cold-2nd):** the two live equity consumers `kelly.py:352` and `run_cycle.py:1590` BOTH hardcode `get_gex_regime("SPY")` — they do NOT pass the entry symbol, so computing Mag-7 GEX changes ONLY the advisory options overlay (`options_scanner.py:1139`, never places an order), NOT equity sizing or Layer-8 MIN_SCORE. The earlier "fresh Mag-7 entries get real GEX multipliers" framing was WRONG. The staged diff is HELD (not shipped) pending the §7 routing-gate universe (dynamic, not a hardcoded Mag-7 list) so it isn't re-touched. **The REAL risk-path change** = a future diff wiring the entry symbol into kelly.py:352 / run_cycle.py:1590 (that is when NEGATIVE⇒×1.30 first reaches fresh single-name entries) — full masked-loss gate at THAT time.
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

## 6. STILL OWED / BOOKMARKED
- Forever-6 LIVE + hybrid-margin (Rafael override of board cash-only) — PAUSED, lower priority than this growth engine.
- Monday allocation simulation (computed 2026-08-29): current book QHM $4,516 (LLY/GEV/GE) + intraday $1,503 (META/GOOGL); equity ~$2,455, BP ~$2,598, maintenance cushion ~$650. Day-tier 10-25% = $245-614; 25% tier stop = $61-153/day = 2.5-6.2% of account; 4 max-loss days = 10-17% of account (bounded).
