# Static-to-Dynamic Audit — alpaca-mtf-bot
**Generated:** 2026-05-11 | **Session:** S18
**Scope:** config.py (464L), main.py (866L), trade_engine.py (4014L), risk_manager.py

## North Star
Every trading parameter that currently answers "what number feels right?" must instead
answer "what does current market data say?" — per symbol, per regime, per moment.
The edge comes from the bot being more right about risk/reward conditions than a static
rulebook can be, and staying right as conditions shift without human intervention.

---

## Classification Key
- 🔴 MUST STAY STATIC — regulatory, safety, or architecture invariant
- 🟡 HIGH PRIORITY — dynamic replacement has clear formula + high edge impact
- 🟢 MEDIUM PRIORITY — dynamic replacement needs formula design, meaningful impact
- 🔵 ARCHITECTURE UPGRADE — requires design work + board vote, transformative

---

## 🔴 MUST STAY STATIC (10 items)

| Parameter | File | Value | Reason |
|---|---|---|---|
| `DAY_TRADE_MAX_ROLLING` | config.py | 3 | PDT regulatory hard cap |
| `DAY_TRADE_ROLLING_DAYS` | config.py | 5 | PDT regulatory window |
| `paper=True` | broker.py | True | Safety invariant — board vote to change |
| `MAX_DAILY_LOSS_PCT` (paper kill switch) | main.py | 15% | Safety floor — board voted |
| `TOD_MARKET_OPEN_BUFFER_MINS` | config.py | 30 min | Market structure invariant — 9:30 opening noise |
| `TOD_MARKET_CLOSE` | config.py | 16:00 ET | Regulatory close time |
| Shorting min equity | trade_engine.py ~514 | $2,000 | Alpaca regulatory requirement |
| Min notional order | trade_engine.py ~1343 | $50 | Commission drag floor — practical minimum |
| Min shares per order | trade_engine.py ~1373 | 1 share | Lot size minimum |
| Max overnight positions | trade_engine.py ~3853 | 1 | Marcus Thorne cap — board voted |

---

## 🟡 HIGH PRIORITY — DYNAMIC (12 items, clear formula, high edge impact)

### H1 — Overnight Buffer Multiplier
**Current:** `_be_buf = ATR × vix_scalar` (vix_scalar = 0.25/0.40/0.50 at VIX <20/20-30/>30)
**Problem:** TSLA (60% rv) and AAPL (25% rv) get identical buffers. Causes premature exits on routine wicks.
**Formula:** `_be_buf = ATR × vix_scalar × rv_scalar` where `rv_scalar = clip(rv_20d / 0.20, 1.0, 2.0)`
**Status:** IN FLIGHT — board voted S18. Awaiting patch after DPE module design.
**Data input:** 20-day realized vol from daily bars (fetcher.py, no new API)
**Files:** trade_engine.py lines 2399-2404

---

### H2 — Stop ATR Multiplier (Intraday + Overnight)
**Current:** `INTRADAY_STOP_ATR_MULT = 1.25` (config), then stepped VOL_TIER override (1.25/1.75/2.5)
**Problem:** The tier thresholds (50%/80% rv) are static and the multipliers (1.25/1.75/2.5) are static.
A stock with 55% rv and one with 79% rv get the same stop — one threshold away from a cliff.
**Formula:** Continuous function of rv_20d:
```
stop_mult = base_stop × (1.0 + (rv_20d - rv_baseline) / rv_baseline)
         = 1.25 × (rv_20d / 0.20)    ← scales linearly with realized vol
         clipped to [1.0, 3.0]
```
This replaces the three discrete tier buckets with a continuous curve.
**Data input:** rv_20d per symbol (same cache as H1)
**Files:** config.py VOL_TIER_* constants, risk_manager.py lines 193-224

---

### H3 — Target ATR Multiplier
**Current:** `INTRADAY_TARGET_ATR_MULT = 2.5` (fixed 2:1 R:R from stop)
**Problem:** Target is fixed regardless of trend quality. A score-12 signal in a strong trend deserves
a wider target than a score-9 in choppy conditions. Fixed target caps winning trades.
**Formula:**
```
target_mult = base_target × score_factor × regime_factor
score_factor = 1.0 + (score - MIN_SCORE) * 0.1    ← score 12 = 1.3x, score 9 = 1.0x
regime_factor = {NORMAL: 1.0, ELEVATED: 0.9, STRESSED: 0.8, HIGH: 0.7}
```
**Data inputs:** entry score, MRI level
**Files:** config.py INTRADAY_TARGET_ATR_MULT, risk_manager.py

---

### H4 — Overnight Breach Scan Count (9-scan gate)
**Current:** 9 consecutive scans at/below buffer = exit (hardcoded in trade_engine.py line 2444)
**Problem:** 9 scans = 45 minutes. In high-vol regimes, that's 9 normal wicks. In low-vol, that's
a genuine trend break. The count should compress in high-vol (exit sooner) and expand in low-vol
(require more confirmation before exiting a slow melt).
**Formula:**
```
breach_count_required = round(9 × (0.20 / vix_implied_daily_vol))
clipped to [5, 15]
where vix_implied_daily_vol = VIX / sqrt(252) / 100
At VIX=18: vix_daily = 1.13%, threshold → round(9 × 0.20/0.0113) = 9 (baseline)
At VIX=30: vix_daily = 1.89%, threshold → round(9 × 0.20/0.0189) = 5 (faster exit)
At VIX=12: vix_daily = 0.76%, threshold → round(9 × 0.20/0.0076) = 13 (slower exit)
```
**Data input:** VIX (already read every cycle as `last_vix`)
**Files:** trade_engine.py line 2444

---

### H5 — MIN_SCORE Threshold (base level)
**Current:** Base MIN_SCORE = 9 (paper profile: 10). Already has layer adjustments:
+1 for FTD correction, +1 for ATH proximity, MRI can raise floor.
**Problem:** The base 9-10 threshold doesn't adapt to market character.
In a broad trend (NORMAL MRI, high breadth), 9 is appropriate. In choppy/sideways markets,
even 11/12 signals fail. The base should shift with regime, not just apply additive layers.
**Formula:**
```
min_score_base = 9 + regime_delta + breadth_delta
regime_delta: NORMAL=0, ELEVATED=+1, STRESSED=+1, HIGH=+2, CRISIS=block
breadth_delta: breadth_score > 70 → -1 (quality confirms trend), < 40 → +1
```
The existing layer system (FTD, ATH, MRI) remains on top of this dynamic base.
**Data inputs:** MRI level, breadth_score (both already computed)
**Files:** config.py PROFILES["paper"]["MIN_LONG_SCORE"], run_cycle.py

---

### H6 — VIX Regime Thresholds (20/30)
**Current:** `VIX_BE_WIDEN_THRESHOLD_1 = 20.0`, `VIX_STOP_WIDEN_THRESHOLD_1 = 25.0`
**Problem:** VIX 20 is not always a meaningful threshold. In 2020-2023, VIX=20 was "calm."
In 2017, VIX=20 was a stress event. The thresholds should be percentile-based
relative to VIX's own recent history.
**Formula:**
```
vix_percentile = percentile_rank(vix_current, vix_history_252d)
CALM:    vix_percentile < 33rd    → vix_scalar = 0.25×ATR
NORMAL:  vix_percentile 33-66th  → vix_scalar = 0.40×ATR
STRESS:  vix_percentile > 66th   → vix_scalar = 0.50×ATR
```
Requires: 252-day VIX history in cache (fetched pre-market from yfinance ^VIX — already used)
**Data input:** VIX + its 252-day history (pre-market fetch, cache to data/cache/)
**Files:** config.py VIX_BE_WIDEN_THRESHOLD_1/2, VIX_STOP_WIDEN_THRESHOLD_1/2

---

### H7 — Pre-market Mover Threshold (2%)
**Current:** `PREMARKET_MOVER_THRESHOLD_PCT = 2.0` — static
**Problem:** In high-vol periods (VIX>25), 2% pre-market moves are noise. Every stock qualifies.
In low-vol (VIX<15), 2% is genuinely significant. The threshold should be VIX-proportional.
**Formula:**
```
mover_threshold = 2.0 × (VIX / 18.0)    ← VIX=18 baseline = 2.0%
clipped to [1.0%, 5.0%]
At VIX=18: threshold = 2.0% (baseline)
At VIX=30: threshold = 3.3% (noisier market = higher bar)
At VIX=12: threshold = 1.3% (calmer market = lower bar)
```
**Data input:** VIX (already available)
**Files:** config.py PREMARKET_MOVER_THRESHOLD_PCT, trade_engine.py ~550

---

### H8 — Gap-down Entry Block (-3%)
**Current:** Gap-down > -3% blocks new long entries for 30 min (trade_engine.py ~620)
**Problem:** -3% is a major gap in VIX=12 but routine in VIX=30. The block threshold should
scale with realized market volatility so it doesn't over-block in high-vol regimes.
**Formula:**
```
gap_block_threshold = -3.0% × (VIX / 20.0)
clipped to [-2.0%, -6.0%]
```
**Data input:** VIX (already available)
**Files:** trade_engine.py ~620

---

### H9 — Trailing Stop Multiplier
**Current:** `TRAIL_STOP_ATR_MULT = 0.5` — same for all symbols
**Problem:** 0.5×ATR trailing stop is too tight for TSLA (clips winning trades on normal vol)
and appropriately sized for AAPL. Same pattern as H1/H2.
**Formula:** Same rv_scalar pattern as H1: `trail_mult = 0.5 × rv_scalar`
clipped to [0.3, 1.0]
**Data input:** rv_20d per symbol (same cache as H1)
**Files:** config.py TRAIL_STOP_ATR_MULT, trade_engine.py partial exit logic

---

### H10 — Midday Size Multiplier (0.75×)
**Current:** `TOD_MIDDAY_SIZE_MULT = 0.75` applied 12:00-2:00 PM ET by clock
**Problem:** Midday low-volume is a market structure phenomenon, not a clock event. On FOMC days,
high-vol midday sessions don't need size reduction. On summer Fridays, 0.75× may still be too
aggressive. Use actual volume data.
**Formula:**
```
if current_volume_vs_30d_avg < 0.5:    # genuinely thin tape
    size_mult = 0.60
elif current_volume_vs_30d_avg < 0.75:
    size_mult = 0.80
else:                                   # volume is normal — no reduction
    size_mult = 1.00
```
**Data input:** intraday volume vs 30-day average (derivable from existing bar data)
**Files:** config.py TOD_MIDDAY_SIZE_MULT, trade_engine.py midday logic

---

### H11 — Red Ratio Lockout Threshold (75%)
**Current:** If >75% of pre-market movers are red → block new longs (trade_engine.py ~554)
**Problem:** 75% is arbitrary. In a genuine bear session, even 60% red is a signal.
In a whipsaw session, 80% red may recover intraday. Should use rolling evidence.
**Formula:**
```
lockout_threshold = 0.70 + (0.10 × regime_factor)
regime_factor = 0 in STRESSED/HIGH, 1 in NORMAL/ELEVATED
= 0.70 in stress regimes, 0.80 in calm (requires stronger consensus to lock out)
```
**Data input:** MRI level (already computed)
**Files:** trade_engine.py ~554

---

### H12 — RTH Reversal Scan Count (6 scans)
**Current:** `RTH_REVERSAL_SCAN_MIN = 6` (30 min sustained reversal before exit)
**Problem:** Same as H4. In high-vol, 6 scans = 6 genuine reversal signals. In choppy, 6 scans
= noise. Should be vol-adjusted.
**Formula:** Same as H4 — VIX-implied vol scales the count.
```
rth_reversal_required = round(6 × (0.20 / vix_implied_daily_vol))
clipped to [3, 12]
```
**Files:** config.py RTH_REVERSAL_SCAN_MIN, trade_engine.py ~3028

---

## 🟢 MEDIUM PRIORITY — DYNAMIC (8 items)

### M1 — ATR Scan Filter (1.5%)
**Current:** `ATR_MIN_PCT = 1.5` — symbols with ATR < 1.5% of price are excluded from scanning
**Formula:** Scale with VIX regime — in high-vol, raise to avoid noise entries:
`atr_min_pct = 1.5 × (VIX / 20.0)`, clipped to [1.0%, 3.0%]

### M2 — Partial Exit ATR Multiplier (0.8×)
**Current:** `PARTIAL_EXIT_ATR_MULT = 0.8` — first partial exit trigger
**Formula:** rv_scalar pattern: `partial_exit_mult = 0.8 × rv_scalar`, clipped to [0.5, 1.5]
Works with trailing stop (H9) to maintain R:R geometry across vol regimes.

### M3 — R:R Minimum Gate (2.0)
**Current:** R:R minimum = 2.0 hardcoded in trade_engine.py ~1137
**Formula:** Score-weighted R:R floor:
`rr_min = 1.8 + (score - 9) × 0.1` → score 9 = 1.8, score 12 = 2.1
Higher conviction deserves a tighter R:R floor (willing to accept 1.8:1 on a 12/12).

### M4 — Tranche Exit Fractions ([0.20, 0.40, 0.60])
**Current:** Tranche 1 at 20% of target, Tranche 2 at 40%, Tranche 3 at 60% (trade_engine.py ~1693)
**Formula:** Compress tranches in stressed regimes (take profit faster):
```
STRESSED/HIGH: [0.15, 0.30, 0.50]    ← bank it sooner
NORMAL/ELEVATED: [0.20, 0.40, 0.60]  ← current behavior
```

### M5 — HTF RSI Thresholds (52/48)
**Current:** RSI > 52 = bullish, RSI < 48 = bearish (trade_engine.py ~1056-1057)
**Formula:** Expand thresholds in trending markets (breadth > 65):
```
if breadth_score > 65:    rsi_bull = 50, rsi_bear = 50   ← looser in confirmed trend
else:                     rsi_bull = 52, rsi_bear = 48   ← current
```

### M6 — FVG Zone Width (3%) and Approach (0.75%)
**Current:** Max FVG zone width = 3%, approach distance = 0.75% (trade_engine.py ~284-285)
**Formula:** Scale with rv_20d of the symbol:
`fvg_width = 3.0 × rv_scalar` clipped to [2.0%, 6.0%]
`fvg_approach = 0.75 × rv_scalar` clipped to [0.5%, 1.5%]

### M7 — Score Drop Gate (7/12)
**Current:** If signal score drops below 7, potential reversal flag (trade_engine.py ~3117)
**Formula:** Regime-adjusted:
`score_drop_gate = 7 + regime_delta` (same delta as H5 — STRESSED/HIGH = 8, NORMAL = 7)

### M8 — AH Spread Gate (2%)
**Current:** After-hours spread > 2% = reject overnight entry (trade_engine.py ~3952)
**Formula:** Loosen in high-vol periods:
`ah_spread_gate = 2.0 × (VIX / 20.0)` clipped to [1.5%, 4.0%]

---

## 🔵 ARCHITECTURE UPGRADES (3 items, board vote required each)

### A1 — SCORE_WEIGHTS (regime-conditional signal weights)
**Current:** Fixed integer weights per signal component (e.g., momentum_12_1 = 2 always)
**Vision:** Different signals have different predictive power in different market regimes.
In trending markets (NORMAL MRI, breadth > 65): momentum and EMA signals dominate.
In mean-reverting markets (choppy, mid-vol): RSI and VWAP proximity signals dominate.
**Requires:** 90+ days of signal-outcome data to estimate regime-conditional weights.
**Gate:** CPCV validation before activation (same as TSMOM scoring gate)
**Status:** Gated — data not yet available. Queue for Q3 2026.

### A2 — Kelly Fraction Adaptation
**Current:** `KELLY_FRACTION = 0.25` — fixed quarter-Kelly
**Vision:** Kelly fraction adapts to rolling Sharpe ratio of the last 30 closed trades:
`kelly_fraction = base_fraction × clip(rolling_sharpe / 1.0, 0.5, 1.5)`
When Sharpe > 1.0: scale up toward 0.375 (but never > 0.40)
When Sharpe < 0.5: scale down toward 0.125
**Requires:** 30+ closed trades (warmup already tracked via KELLY_MIN_SAMPLE_SIZE)
**Board vote required:** Touches position sizing directly.
**Status:** Pre-conditions nearly met (approaching 30 closed trades). Board vote S19.

### A3 — EMA/SMA Adaptive Periods
**Current:** EMA_FAST=13, EMA_SLOW=30, SMA_20=20 — fixed forever
**Vision:** In trending regimes (ADX > 25, breadth > 65): favor faster EMAs (8/21).
In choppy regimes: favor slower EMAs (21/55) to reduce whipsaw signals.
**Requires:** Regime detection is already built (MRI + breadth). Adaptation logic is new.
**Board vote required:** Touches signal generation directly (Architecture Invariant #1 adjacent).
**Status:** Queue for S20 after A1/A2 board votes.

---

## Dynamic Parameter Engine (DPE) — Initial Design

### Module: `execution/param_engine.py`

The DPE is the single source of truth for all dynamic parameters.
It replaces direct `config.CONSTANT` lookups in trade_engine.py with clean function calls.

```
Market Primitives (inputs):
  ├── vix_current         (already read every cycle → _last_vix)
  ├── vix_percentile      (pre-market: vix vs 252d history → data/cache/vix_history.json)
  ├── rv_per_symbol       (pre-market: stdev(log_returns_20d) × √252 → data/cache/rv_cache.json)
  ├── mri_level           (already computed → MacroRiskIndex.level())
  ├── breadth_score       (already computed → _last_breadth["score"])
  ├── rolling_sharpe      (already tracked → KellySizer stats)
  └── spy_trend_quality   (already computed → SPY bar-over-bar gate)

Parameter Functions (outputs):
  ├── get_be_buffer_mult(symbol)      → H1 (overnight buffer)
  ├── get_stop_mult(symbol)           → H2 (entry stop)
  ├── get_target_mult(symbol, score)  → H3 (entry target)
  ├── get_breach_count()              → H4 (overnight scan gate)
  ├── get_min_score(mri, breadth)     → H5 (entry filter)
  ├── get_vix_scalar()                → H6 (regime threshold, percentile-based)
  ├── get_mover_threshold()           → H7 (pre-market filter)
  ├── get_gap_block_threshold()       → H8 (gap-down gate)
  ├── get_trail_mult(symbol)          → H9 (trailing stop)
  ├── get_size_mult(symbol, tod)      → H10 (midday + vol + breadth)
  ├── get_lockout_threshold()         → H11 (red ratio)
  └── get_reversal_count()            → H12 (RTH reversal gate)
```

### Refresh Schedule
```
Pre-market (8:45 AM ET): compute rv_per_symbol, vix_percentile → cache to data/cache/
Each cycle: update vix_current, mri_level → recompute vix_scalar, breach_count
Midday (12:00 PM ET): refresh rv_per_symbol if cache > 4h old
```

### Logging
Every parameter computation logged to trade_events.jsonl:
```json
{"ts": "...", "event": "param_refresh", "symbol": "TSLA",
 "rv_20d": 0.62, "rv_scalar": 2.0, "stop_mult": 2.5, "be_mult_adj": 0.50,
 "vix": 18.2, "vix_pct": 45, "mri": "ELEVATED", "breadth": 58}
```
This makes every parameter decision auditable in post-mortem.

---

## Implementation Sequence (Priority Order)

| Phase | Items | Prerequisite | Est. Sessions |
|---|---|---|---|
| **Phase 1** | H1 (buffer) + DPE skeleton + rv_cache | Board vote ✅ | S18 |
| **Phase 2** | H2 (stop mult) + H9 (trail) + H4 (breach count) | Phase 1 done | S19 |
| **Phase 3** | H5 (min_score) + H6 (VIX percentile) + H7 (movers) | Phase 2 done | S19-S20 |
| **Phase 4** | H3 (target) + H8 (gap block) + H10 (midday vol) | Phase 3 done | S20 |
| **Phase 5** | M1-M8 (medium priority items) | Phase 4 done | S21 |
| **Phase 6** | A2 (Kelly adapt) | 30+ trades, board vote | S22 |
| **Phase 7** | A1 (score weights) | 90+ days data, CPCV, board vote | Q3 2026 |
| **Phase 8** | A3 (EMA adapt) | A1 validated, board vote | Q4 2026 |

---

## Parameters With Zero Dynamic Opportunity (Intentionally Static)

These look static but are static BY DESIGN — their value comes from consistency:
- `EMA_FAST = 13 / EMA_SLOW = 30` — signals are compared across symbols/days; changing periods mid-session invalidates comparisons (until A3 is validated)
- `MOMENTUM_LONG_LOOKBACK = 252` — Jegadeesh-Titman formulation; changing the period changes the factor entirely
- `KELLY_MAX_RISK_PCT = 0.04` — hard guardrail; must stay as an absolute cap even if Kelly adapts
- `BUCKET_A_ALLOCATION_PCT = 5%` — architectural allocation; board voted
- `CONVICTION_PDT_FULL_MIN = 12` — PDT=3 overnight requires max conviction; this is a qualitative gate not a statistical one
