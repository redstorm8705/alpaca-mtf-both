# Design Record — Small-Account Aggressive Growth Regime (DAY + INTRADAY tiers)
**Date:** 2026-09-19 · **Status:** BGGN-ALIGNED (5/5 APPROVE the concept, conditional) — awaiting Rafael's decision to build · **Trigger:** Rafael North-Star direction (account $2.45k → $25k; day+intraday = growth engines, QHM/F6 = ballast)

## BGGN PANEL (all reasoning North-Star-First)
Board cold seats: Thorp (growth-sizing), Taleb (risk-asymmetry / masked-loss seat), Harris+LdP (execution/validation). External: Gro, GAI (same brief). Brief: `scratchpad/growth_engine_design.md`.

## UNANIMOUS VERDICT: APPROVE a TEMPORARY bucket-notional + bounded-conviction-upsize regime — CONDITIONAL on the tail being capped. It is growth-optimal AND survivable inside the min-stop gate + −5% day-tier kill ($122.50) + 7% account kill ($171.50).

### The core reframe (all 5)
- Current risk-based day-tier sizing keeps positions at ~1 share ($500 MSFT) → captures ~0% of achievable growth. That is **underbetting** (Thorp: quarter-Kelly captures only 44% of growth), the direct cause of the 1-share problem and the missed SNDK-type movers. Fixing it is North-Star-correct.
- BUT "buy as many shares as possible" (pure notional-max) is **VETOED** (Taleb, GAI, Thorp) on envelope grounds: a wide-stop name (TSLA/NVDA on a volatile day) deploying a full $2k bucket at an 8–10% stop = $160–$200 loss in ONE fill → breaches the 7% account kill before the fills-based kill can act. Notional ≠ risk.
- **HONEST TRUTH (Thorp, LdP):** the day tier is 0/6 (−$11.05) = a *negative measured edge* on a tiny sample. No sizing makes a negative edge profitable. So this regime is legitimate as **BOUNDED EXPLORATION to build a ≥30-trade sample and MEASURE the edge** — size up to LEARN, not to bet a proven winner. North-Star-consistent (cheap paper lesson), but requires instrumentation + a pre-registered pull-back rule.

## DECISIONS (reconciled consensus)
- **D1 — Sizing = HYBRID (option c).** Notional bucket drives shares (escapes the 1-share trap) + a per-trade $-risk cap (`shares × stop_distance ≤ cap`) bounds the tail. Keep the min-stop gate as the no-pennies backstop. Cap range voted: 1.5% (Taleb/Gro), 2% (Thorp), 3% (GAI/Harris-LdP). **Reconciled: 1.5% ($37)** so 3 concurrent fits under the tier kill.
- **D2 — Bucket = TOTAL day-tier gross cap (concurrency shares it), + aggregate open $-risk ≤ tier kill.** Bucket $1k–$2k (Gro/LdP $1k for tighter experiment variance; Thorp/Taleb/GAI $2k). **Binding constraint (all 5): `MAX_CONCURRENT × per-trade-cap ≤ 5%` ($122.50), target ≤4.5% with slippage headroom.** Already partially enforced at `config.py:1114`. **Reconciled: bucket $1.5k, 3 concurrent × 1.5% = 4.5% ≤ 5%** (Taleb: 3 independent convex shots > 1 big bet).
- **D3 — Intraday high-conviction upsize = YES, BOUNDED, snowball via the EXIT not the entry.** Conviction multiplier 10→1.0×, 11→1.25×, 12→1.5× (hard cap 1.5–2×). Raise the FLOOR so an 11–12 signal is never 1 share (~$300–500 min notional). **KEEP 0.25 Kelly — do NOT override blind on n<30** (Taleb/LdP: >2× Kelly makes log-growth negative even with a real edge). Snowball = let winners run / trail the stop / pyramid ONLY on green (add ≤50% initial, aggregate risk ≤ initial 1R) — never fatten the entry stake.
- **D4 — Reversion = GLIDE (not cliff) + a STATISTIC gate.** Glide the aggressive fraction from 1.0 at ~$10k → 0 by $15–25k (aligns with $10k kill-tier upgrade). The fixed-$ bucket self-tempers (82% of equity now → 20% at $10k). **Independent early-revert: at n≥30 aggressive trades, if expectancy ≤ 0 / profit factor < ~1.2 (lower-CI ≤ 0) → revert regardless of equity.** Anti-martingale: size up only after strength, never after losses.
- **D5 — QHM/F6 = ballast, no aggression — but this is where the REAL uncapped tail lives.** Taleb: LLY −$138 is an un-stopped OVERNIGHT concentrated gap the intraday kill/stops cannot protect. **Cap single-name overnight ≤ ~15% equity (~$370); bound aggregate overnight gross so a 20% gap stays survivable.** Segregate P&L reporting by tier so ballast swings don't contaminate the experiment read.

## HARD-ENVELOPE FIXES REQUIRED BEFORE SHIP (retained vetoes)
1. **Per-trade $-risk cap mandatory** (no pure notional-max). 
2. **`MAX_CONCURRENT × per-trade-cap ≤ tier kill`** (≤4.5–5%) — reconcile before enabling; the current 3×2% ceiling path must not be raised without honoring this.
3. **Broker-NATIVE stop orders (not bot-managed "mental" stops)** — this week's ~2-day kill-switch outage + a full-bucket position = an unprotected tail; bigger notional amplifies outage loss linearly. THE latent masked/weakened-kill vector.
4. **Day tier must stay intraday-FLAT** (no overnight) — `eod_flat` exit confirms; condition approval on it.
5. **Ship WITH instrumentation:** per-trade R-multiple log {tier, symbol, trigger, score, slippage bps, shares, notional, stop_dist, $-risk, exit reason, P&L in $ and R, hold time, spread/ATR, regime} + per-tier {n, win-rate+CI, PF, expectancy in R/$} + a **pre-registered n≥30 success criterion written before the experiment starts.**

## MICROSTRUCTURE (Harris): a NON-issue at this size — 2–12 shares of mega-caps is a rounding error vs displayed depth; impact ≈ 0 all the way to $25k. Do NOT stay small on slippage grounds.

## NOTED (not part of the vote)
- SNDK (Rafael's cited +$175 mover) was SCANNED (delta_shadow, score only reached 2) but NOT traded; a day-tier daily-bar fetch for SNDK returned null → possible data gap that prevents the tier from acting on it. Worth a separate check.

## NEXT: Rafael decides → Feature Design Protocol → implement HYBRID sizing (`execution/day_trade_manager.py` + `config.py`) + intraday conviction floor/multiplier + instrumentation → full gate (board + Gro + GAI on the diff, masked-loss seat mandatory — risk-path) → ship staged.

---
## ADDENDUM (2026-09-19) — Rafael's 3 notes, BGGN-resolved (Gro, GAI, Risk seat [Taleb+Thorp+LdP], Execution seat [Harris+Brandt+Sosnoff/Sinclair+reliability])

**Grounded facts (grep-verified):** day tier sizes WHOLE shares (`day_tier_sizing.py:135` floor) via `qty=` orders (`broker.py`), already caps single-name notional (~$1,000 thin-name + 0.60×equity, `config.py:916/:923`) and enforces `MAX_CONCURRENT × basis ≤ tier-kill` (`config.py:904`, asserted `:1113`). v1 exit = confirm-fill → separate naked DAY stop, pin computed but never harvested (`day_trade_manager.py:42-47`). Alpaca: fractional = Market/Limit/Stop TIF=DAY, NO bracket/OCO, NO shorts; bracket=`order_class="bracket"` OTOCO, DNR/DNC, sibling auto-cancel, TP.limit must be beyond stop, partial-TP auto-resizes stop, fast-market both-fill possible.

### A1 — EXIT TARGETS (the real build; tier currently harvests $0):
Broker-native **OTOCO bracket** (entry + TP + protective **market**-stop, one grouped order, TIF=day). FADE TP = GEX pin (1:1), skip if pin < 1.5×ATR from entry. RIDE TP = next GEX wall above the broken wall, ≥2:1. **Default ALL-OUT until n≥30** (clean R-multiple for validation — LdP); scale-out (50% core at target + 1.5×ATR-trailed runner) only AFTER validation AND only on names with ≥4 whole shares. Stop leg = plain market-stop (NOT stop-limit — guaranteed exit, no gap-through naked risk). **eod_force_flat MUST cancel resting bracket legs before flattening** (else self-inflicted double-fill — non-negotiable). Exit legs are reducing orders → no BP draw, correctly excluded from pending-notional aggregate. Bonus: bracket entry closes the naked-fill window B2's retry logic papers over.

### A2 — PRICE-ADAPTIVE STOP (Rafael's note #1) — RESOLVED: REJECT the price scalar (4 voices reject; Gro's 0.3-0.5×ATR proposal is self-contradicting — tighter than the min-stop gate's 1.5×ATR floor → re-arms the hairpin). Rafael's economic intent (size in expensive names at bounded risk) is a share-GRANULARITY problem risk-based sizing already solves for tradeable names; SNDK ($1,791/5.74% ATR) & MU ($1,015/4.23%) wire to 0 shares (notional cap + a safe 1.5×ATR stop = $154/$64 >> $40 cap) and correctly stay EXCLUDED. Fractional VETOED (no broker stop, no shorts). Add pre-submit assert `qty==floor(qty) and price ≤ $1000`. Expensive names re-enter as equity grows (Rafael's own tiering).

### A3 — PER-TRADE CAP (Rafael's note #3) — RESOLVED: $40/trade = the CEILING (2% of $2k, margin-inclusive bucket); **aggregate day-tier open risk ~$110 (4.5%) = the MASTER cap**, concurrency floats (~2 now, auto-ramps to 3 as equity grows). Do NOT run literal $40×3 ($120, only $2.50 under the $122.50 tier kill, no slippage headroom). Brackets don't change the risk math (stop_price identical).

### HARD-ENVELOPE / VETOES (all seats): (1) fractional as a sizing fix = breach (no broker-native stop); (2) $40×3 with no slippage buffer = soft breach → use $110 aggregate master; (3) eod_force_flat must cancel bracket legs first; (4) bot-managed targets vetoed → broker-native OTOCO only; (5) stop leg market not stop-limit.

## STAGED BUILD (Tidy-First, highest-leverage first; each stage full-gated, masked-loss seat mandatory — risk-path):
1. **A1 OTOCO exit targets + A3 sizing ($40 ceiling / $110 aggregate master / whole-share+$1000 assert)** — the day tier finally harvests wins AND deploys size. [v1 all-out; scale-out gated to post-n≥30]
2. Intraday high-conviction floor + bounded ≤1.5× upsize (score 11-12), keep Kelly.
3. Reversion glide ($10k→$15-25k) + statistic early-revert gate + per-trade R-multiple instrumentation.
4. QHM/F6 single-name overnight cap (~15% equity) + per-tier P&L segregation.

## OPEN FOR RAFAEL: note #1 (price-scalar) was overridden by the board — confirm he accepts "no price scalar; SNDK re-enters as the account grows" or wants the scalar anyway (he is the authority).

## QUEUED (Rafael 2026-09-19, after the addendum — high priority, do NOT lose):
- **Q1 — Confluence scanner sharpness / dynamic indicator weighting (Rafael: "confluence isn't working; feels static; not overweighting the right indicators; re-evaluate what triggers buy long/short").** The signal-quality initiative. Rafael's framing: if entries were near-100% correct, risking 6% on a name like SNDK becomes palatable. Connects to the board's 0/6 negative-edge caveat — Stage 1's clean per-trade R-multiple instrumentation is the DATA this needs. MODE-2 architecture re-eval of the 12-pt confluence weights (static → dynamic/regime-aware). Queued until aligned on the Stage-1 path. Prereq: Stage 1 instrumentation shipping.
- **Q2 — Leveraged 2x/3x bull/bear ETF counterparts for expensive names (Rafael temporary fix).** Get expensive-name exposure (e.g. semis via SOXL/SOXS ~$25 instead of SNDK $1,791) with many shares + bounded per-share risk. Additive DAYTRADE_UNIVERSE expansion; infra partly exists (config LEVERAGED_TICKERS={NVDL,TQQQ,TSLL,SQQQ} + 2.5× stop/target). Needs: (a) GEX-coverage check — does the day-tier GEX pipeline produce a regime/pin for a leveraged ETF? (b) board pass on leveraged-ETF decay + higher vol + the 2.5× mult interaction with the new $40 cap. Universe-agnostic to Stage 1 mechanics, so queued separate.
