# weekly_perf_audit.py — Full Design Specification v1
**Status:** DESIGN COMPLETE — Pending DS/GAI design review + user approval before build begins  
**Last updated:** 2026-05-25 S39  
**Board vote:** Analytics Board COMPLETE (8 categories, RTH block, atomic write, Friday 4:15 PM ET cron)  
**DS/GAI gate:** NOT required for this file (not in RTH import chain per board vote)  
**Build gate:** User must review Board Q&A + DS/GAI design feedback before any code is written

---

## 1. PURPOSE & DESIGN PHILOSOPHY

This script is the bot's learning engine. Its job is not just to bucket losses — it is to:

1. **Reconstruct** the full context of every trade (entry signal, market conditions, MRI level, score breakdown)
2. **Classify** losing trades into 8 failure categories using rule-based deterministic logic
3. **Quantify** the pattern: how often does each category fire, how much does it cost, is it statistically meaningful?
4. **Generate hypotheses**: if we changed parameter X, how many of this week's losses would have been avoided?
5. **Feed the board**: present findings with enough rigor that board members can vote on what to actually change
6. **Feed DS/GAI**: send the same prompt to both external AIs for independent review of the hypotheses
7. **Gate all changes**: NO parameter is changed without board vote → DS/GAI review → user approval

The critical principle (López de Prado, *Advances in Financial Machine Learning*): **audit ALL trades (winners + losers) for failure-category conditions.** Winners who survived failure conditions are "lucky wins" — they bias the hypothesis if ignored.

---

## 2. FAILURE TAXONOMY — 8 CATEGORIES

### Deterministic Classification Rules

| ID | Category | Primary Trigger Rule | Candidate Parameter Response |
|----|----------|---------------------|------------------------------|
| **1a** | Directional Macro Headwind | SPY 5-min bar-over-bar was bearish in the 30 min before entry AND position is long (or vice versa), OR SPY daily EMA(20) was declining at entry | ORB window, SPY lookback, directional gate weight |
| **1b** | Volatility Regime Sizing Error | VIX z-score > 1.0 (1 std above 20-day mean) at entry AND stop_distance_atr_mult < 1.75 (stop undersized for regime) | VIX_STOP_WIDEN_THRESHOLD_1/2, Kelly fraction |
| **2** | Marginal Score Low-Momentum | Score was MIN_SCORE or MIN_SCORE+1 AND momentum_summary had fewer than 2 bullish signals at entry | MIN_SCORE increase, momentum confirmation weight |
| **3** | Leveraged Sizing Error PDT-triggered | Symbol in BUCKET_A (TQQQ/SQQQ/TSLL) AND (a) size > Kelly-adjusted budget OR (b) PDT slot used on a loss that could have been held overnight | BUCKET_A Kelly cap, leverage multiplier |
| **4** | Time-of-Day Bleed | Entry time was 9:30–9:45 AM ET (first 15 min) OR 3:30–4:00 PM ET (last 30 min) — historically higher adverse move rate | TOD entry gate (new config), expand ORB_WINDOW_MINUTES |
| **5** | Earnings Risk | Earnings announcement within 5 trading days of entry (before or after) | Earnings proximity gate (extend to 7 days?) |
| **6** | VIX Stop Crush | VIX daily high was >1.5x ATR-equivalent AND stop was set at standard ATR_MULT without VIX widening applied | INTRADAY_STOP_ATR_MULT increase, VIX_STOP_WIDEN_THRESHOLD lowering |
| **7** | Holding Period Mismatch | Holding period was <5 minutes (scalped on noise) OR >3 trading days for an intraday signal type | Exit signal thresholds, target multiplier |
| **8** | Unknown / Doesn't Fit | None of categories 1a–7 applies | Canary — see §5 |

### Multi-Category Assignment
- Every losing trade receives a **primary** category (deepest causal root) and up to 3 **secondary** categories
- Correlated categories (e.g., 1b + 6, both VIX-driven): weight hypothesis at 0.7× — same root cause appearing twice
- Orthogonal categories (e.g., 1a + 4): weight hypothesis at 1.3× — independent confirmation
- "Lucky wins" (winners with failure-category conditions at entry) are tracked and used for hit-rate computation

---

## 3. STRATEGY FEEDBACK LOOP — EXPLICIT MECHANISM

This is the key gap identified by the user. Here is exactly how a failure observation becomes a strategy change.

### 3.1 The Five-Gate Process

```
GATE 1 — DETECTION (automated, Friday 4:15 PM ET)
  weekly_perf_audit.py classifies all trades, computes category hit rates
  Hypothesis generated ONLY when:
    - Category fires ≥2× in the week OR fires in 2 consecutive weeks
    - Category hit rate is significantly worse than portfolio baseline (p < 0.20)
    - Primary assignments across trades are orthogonal (not all same VIX spike)

GATE 2 — BOARD VOTE (Monday AM, same session)
  4 independent domain agents review the hypothesis
  Questions: Is this statistically real? Which parameter to change? Expected impact?
  Board vote required before DS/GAI step
  Defensive changes (TOD gate, earnings block, stop widening): board-only vote, 1-week data minimum
  Offensive changes (MIN_SCORE, Kelly, scoring weights): full board vote, 3-week data minimum

GATE 3 — DS/GAI EXTERNAL REVIEW (Monday, after board vote)
  Same comprehensive prompt sent to both DeepSeek and Google AI Studio
  They answer 8 category-specific questions (see §7)
  Claude compiles 3-Point AI Summary (see §8)

GATE 4 — USER APPROVAL (user is SOLE mandate authority)
  User reviews board findings + DS/GAI 3-Point Summary
  User approves, rejects, defers, or modifies
  No change is implemented without explicit user approval

GATE 5 — VALIDATION (following week)
  If implemented: track whether the category fires less frequently
  Board votes to make permanent or revert at next audit
  Rollback requires user approval
```

### 3.2 Category → Parameter Adjustment Trigger Table

| Category | Trigger Threshold | Evidence Required | Parameter(s) | Action Latency |
|----------|-------------------|-------------------|--------------|----------------|
| 1a | 4+ directional losses + correlation ≥0.65 with SPY direction | 12+ trades, 3+ weeks | ORB window, SPY lookback | Board vote Monday |
| **1b** | 2+ high-VIX losses + Kelly drawdown >15% | 10+ trades, 2+ weeks | VIX_STOP_WIDEN_THRESHOLD, Kelly cap | **AUTO-DEPLOY** pre-approved range at VIX>25 |
| 2 | 60%+ losers at score=10-11/12 + momentum rank <35th pctl | 15+ trades, 3+ weeks | MIN_SCORE 10→11 | Board vote Monday |
| 3 | 3+ PDT violations + BUCKET_A avg loss >1.5× BUCKET_B | 8+ trades | BUCKET_A Kelly 0.25→0.20 | Board vote Monday |
| **4** | 65%+ losses in 9:30–9:45 or 3:30–4:00 ET window | 12+ trades, 2+ weeks | TOD gate activation | **AUTO-DEPLOY** pre-approved gate |
| 5 | 4+ earnings-proximity losses + vol spike >1.5σ | 6+ trades, 2+ weeks | Earnings lookback 5→7 days | Board vote within 24h |
| **6** | 3+ ATR stops with 1.5× intraday recovery | 8+ trades, 2+ weeks | INTRADAY_STOP_ATR_MULT 1.25→1.5+ | **AUTO-DEPLOY** pre-approved at VIX>20 |
| 7 | Win/loss ratio >2.5 + bimodal holding period histogram | 20+ trades | Target multiplier, exit thresholds | Board vote Monday |

**AUTO-DEPLOY** means the parameter is in a pre-approved range that can be activated within 4 hours of the Friday audit without waiting for full Monday board vote. The board still reviews and votes Monday to make it permanent or revert.

### 3.3 What Counts as "Statistically Confirmed"

From López de Prado + Asness + Jegadeesh-Titman:
- **Category hit rate must be significantly worse than portfolio baseline**: use 2-proportion z-test at p < 0.20 (not p < 0.05 — too stringent for 5-20 trades/week)
- **Minimum 3 consecutive weeks** for offensive parameter changes (MIN_SCORE, Kelly)
- **Minimum 2 weeks** for defensive changes (gates, stop widening, earnings block)
- **Lucky wins >20% of category total** → defer proposal 2 more weeks (Asness survivorship bias rule)

### 3.4 The "Unknown" Category as Fragility Canary

From Taleb (*Antifragile*):
- **Threshold: >10% of weekly losers fall in category 8 (Unknown)**
- Action: Do NOT propose parameter changes. Trigger a separate session for zero-based forensic review of those trades
- Signal: The bot's risk model has an unmodeled gap. Something systematic is failing that isn't captured by categories 1a-7

---

## 4. DATA ARCHITECTURE

### 4.1 Complete Trade Record Schema

Every reconstructed trade record carries:

```json
{
  "trade_id": "AMZN_20260522_103045_abc123",
  "symbol": "AMZN",
  "direction": "long",
  "status": "closed|incomplete|orphan",
  
  "entry": {
    "ts": "2026-05-22T10:30:45.123-07:00",
    "price": 183.45,
    "qty": 10,
    "score_12pt": 11,
    "score_breakdown": {"spy_bar": 1, "vwap": 1, "rsi": 1, "macd": 1, ...},
    "mri_level": "NORMAL",
    "spy_event_type": "uptrend",
    "spy_bar_over_bar_bullish": true,
    "vix_at_entry": 19.2,
    "vix_20d_mean": 18.5,
    "vix_zscore": 0.38,
    "symbol_atr_14d": 2.15,
    "symbol_rvol_20d": 0.42,
    "pdt_used_at_entry": 1,
    "event_exists": true,
    "fill_id": "activity_abc123",
    "data_source": "alpaca_fill"
  },
  
  "exit": {
    "ts": "2026-05-22T14:15:22.456-07:00",
    "price": 185.20,
    "qty": 10,
    "reason": "target|stop_hit|signal_exit|manual|overnight_atr_buffer_exit",
    "fill_id": "activity_def456"
  },
  
  "partial_exits": [
    {"ts": "...", "price": 184.33, "qty": 3, "tranch": "T1", "fill_id": "..."}
  ],
  
  "market_context": {
    "symbol_ohlc_entry_day": {"o": 182.00, "h": 186.50, "l": 181.50, "c": 185.20, "v": 12500000},
    "spy_ohlc_entry_day": {"o": 505.10, "h": 506.80, "l": 503.90, "c": 506.20, "v": 85000000},
    "vix_daily_high": 20.1,
    "vix_daily_low": 18.8,
    "earnings_days_away": 12,
    "entry_hour_et": 10,
    "holding_period_minutes": 225,
    "holding_period_days": 0
  },
  
  "pnl": {
    "gross": 17.50,
    "pct": 0.957,
    "source": "alpaca_fill|tracker_estimate",
    "r_multiple": 0.81
  },
  
  "classification": {
    "primary": "1b_vol_regime_sizing",
    "secondary": ["6_vix_stop_crush"],
    "confidence": 0.92,
    "weight_multiplier": 0.7,
    "lucky_win": false,
    "trace": [
      {"rule": "1b_vol_regime", "fired": true, "vix_zscore": 1.4, "stop_mult": 1.25, "min_required": 1.75},
      {"rule": "6_vix_stop_crush", "fired": true, "reason": "correlated with 1b — same VIX spike"}
    ]
  }
}
```

### 4.2 Join Strategy: Fills (authoritative P&L) + trade_events.jsonl (signal/MRI context)

Per McKinney (*Python for Data Analysis*, Ch. 8):
- **Fills = root table** (indexed by Alpaca activity_id)
- **trade_events.jsonl = enrichment** (joined by symbol + qty + ±30s timestamp window)
- Paper gap handling: if Alpaca fill missing (next-day settlement), use tracker entry from trade_events + flag `_pnl_source="tracker_estimate"`
- Orphan fills (fill with no event match): flag `_event_status="orphan_fill"` + log error

### 4.3 VIX Normalization

**NOT absolute threshold (VIX > 25).** Use z-score relative to trailing 20-day mean:
```python
vix_zscore = (vix_today - mean_20d) / stdev_20d
# Category 1b fires when: vix_zscore > 1.0 AND stop_mult < 1.75
```
Self-calibrating across vol regimes without config changes. (McKinney recommendation)

### 4.4 API Call Plan (typical week, 10 losing trades, 6 symbols)

```
T+0s   Alpaca fills API: GET /v2/account/activities?activity_type=FILL&after=...
T+1s   Load trade_events.jsonl (local disk)
T+2s   Reconstruct trades: join fills to events
T+3s   Alpaca daily bars: GET /v1/bars?symbols=AAPL,AMZN,SPY,...&timeframe=1Day (batched)
T+4s   yfinance ^VIX daily history (T4 — acceptable for EOD audit, not RTH)
T+5s   FMP earnings calendar (if any symbol had earnings-adjacent trades)
T+7s   Classify all losing trades
T+8s   Generate HTML tearsheet
T+9s   Atomic write: tmp → replace (scan_results pattern)
T+10s  Slack summary post (async, non-blocking)
T+12s  Done — total 12s nominal (SLA: <90s)
```

Cache: `logs/weekly_audit_cache_YYYY-WNN.json` — checked before any API call, expires after 2h.

---

## 5. OUTPUT SPECIFICATION

### 5.1 HTML Tearsheet

File: `logs/weekly_perf_audit_YYYY-WNN.html`  
Write: atomic tmp→replace pattern  
Archive: keep 52 weeks rolling (compress older to `logs/archive/`)

**Sections:**
1. **Executive Summary Card**: equity, week P&L, win rate, avg win/avg loss, realized R:R
2. **Week-over-Week Trend**: P&L delta, win rate delta, failure category trend arrows
3. **All Trades Table** (wins + losses): symbol, direction, entry/exit price, hold time, score, MRI level, P&L, failure category
4. **Market Context per Trade**: entry-day OHLCV, SPY bar direction at entry, VIX at entry, earnings flag, TOD flag
5. **Failure Category Breakdown**: count + total loss per category, hit rate vs. portfolio baseline
6. **Board Hypotheses**: which parameter changes are being proposed and what triggered them
7. **Lucky Wins Section**: winners that had failure-category conditions — shows if our categories would have filtered good trades
8. **Statistical Confidence**: per-category z-score and p-value
9. **Rolling 4-Week Trend**: category-by-category sparklines
10. **Parameter Change Tracker**: what changed when, what was the pre/post result

### 5.2 Slack Summary

Posted to: existing Slack webhook (same as alerts.py)  
Format (concise, action-oriented):
```
📊 WEEKLY AUDIT — 2026-W21 (May 19–23)
P&L: -$XXX (-X.X%) | Win Rate: X/XX (XX.X%)
Avg Win: $XX.XX | Avg Loss: -$XX.XX | R:R: X.XX

TOP FAILURE CATEGORIES:
  1. Category 6 (VIX Stop Crush): 3× | -$XXX | 35% of losses
  2. Category 1b (Vol Regime Sizing): 2× | -$XXX | 24% of losses

BOARD HYPOTHESES:
  H1: Widen INTRADAY_STOP_ATR_MULT 1.25→1.5 at VIX>22 → estimated +$XXX recovery
  H2: Requires 3-week confirmation before MIN_SCORE change

STATUS: 🟢 DS/GAI REVIEW REQUIRED — forwarding prompt below
[DS/GAI PROMPT — plain text — copy and send to DS/GAI]
```

### 5.3 DS/GAI Prompt Delivery

The DS/GAI prompt is **plain text, delivered via Slack only, never saved to .md files.**  
The Slack message includes the prompt block at the bottom of the weekly summary.  
Operator copies it and forwards to DeepSeek + Google AI Studio (same prompt to both).

---

## 6. GOVERNANCE GUARDRAILS (3 — from Analytics Board vote)

1. **3-week minimum for offensive parameter changes**: MIN_SCORE, Kelly fraction, scoring weights — never changed on a single week of data
2. **40% impact minimum**: A hypothesis must show that applying the proposed fix would have avoided ≥40% of the categorized losses last week before it warrants a board vote
3. **Track 3 weeks post-implementation**: Every change is on probation for 3 audit cycles before being considered permanent. Rollback is always available.

**Douglas's discipline rule**: Maximum 1 parameter change per week. 5-trading-day cooldown after any change before the next one is considered. "Traders who change parameters frequently create random-reward expectations." (*Trading in the Zone*)

---

## 7. DS/GAI WEEKLY REPORT PROMPT TEMPLATE

This is the template that `weekly_perf_audit.py` will generate and post to Slack every Friday.  
**Same prompt sent to both DS and GAI — no split questions.**

```
WEEKLY PERFORMANCE AUDIT — alpaca-mtf-bot
Week: [YYYY-WNN] ([START_DATE] — [END_DATE])
Audit generated: [TIMESTAMP PT]

═══════ ACCOUNT SUMMARY ═══════
Equity: $[EQUITY] | Week P&L: [+/-]$[PNL] ([PCT]%)
Total trades: [N] ([W] wins, [L] losses, [BE] breakeven)
Win rate: [WR]% | Avg win: $[AVG_WIN] | Avg loss: -$[AVG_LOSS]
Realized R:R: [RR] | Max intraday drawdown: [DD]%

Week-over-week: Last week P&L [+/-]$[LAST_PNL] → This week [+/-]$[THIS_PNL]
Win rate trend: [LAST_WR]% → [THIS_WR]%

═══════ CURRENT PARAMETERS ═══════
MIN_SCORE: [X]/12 | KELLY_FRACTION: [K] | KELLY_MAX_RISK_PCT: [R]%
INTRADAY_STOP_ATR_MULT: [M] | TARGET_MULT: [T]
VIX_STOP_WIDEN_THRESHOLD_1: [V1] (mult: [M1]) | THRESHOLD_2: [V2] (mult: [M2])

═══════ WINNING TRADES ([W] trades, +$[TOTAL_WIN]) ═══════
[For each winning trade:]
[SYMBOL] [LONG/SHORT] [ENTRY_DATE] [ENTRY_TIME PT] → [EXIT_TIME PT]
  Entry: $[ENTRY_PRICE] | Exit: $[EXIT_PRICE] | Qty: [QTY] | Hold: [DURATION]
  P&L: +$[PNL] ([PCT]%) | Score: [S]/12 | MRI: [MRI_LEVEL]
  SPY at entry: [UP/DOWN] bar | VIX at entry: [VIX] (z=[ZSCORE])
  Exit reason: [REASON]
  Failure conditions present at entry: [NONE / Category X because Y]

═══════ LOSING TRADES ([L] trades, -$[TOTAL_LOSS]) ═══════
[For each losing trade:]
[SYMBOL] [LONG/SHORT] [ENTRY_DATE] [ENTRY_TIME PT] → [EXIT_TIME PT]
  Entry: $[ENTRY_PRICE] | Exit: $[EXIT_PRICE] | Qty: [QTY] | Hold: [DURATION]
  P&L: -$[PNL] ([PCT]%) | Score: [S]/12 | MRI: [MRI_LEVEL]
  SPY at entry: [UP/DOWN] bar | VIX at entry: [VIX] (z=[ZSCORE])
  Entry OHLC that day: O=[O] H=[H] L=[L] C=[C] Vol=[V]
  SPY OHLC that day: O=[O] H=[H] L=[L] C=[C]
  Exit reason: [REASON]
  PRIMARY failure category: [CATEGORY_ID] — [CATEGORY_NAME]
    Rule fired: [SPECIFIC RULE THAT TRIGGERED]
    Data: [KEY METRICS THAT CAUSED CLASSIFICATION]
  SECONDARY categories: [LIST or NONE]
  Confidence: [PCT]%

═══════ FAILURE CATEGORY BREAKDOWN ═══════
[For each of categories 1a, 1b, 2, 3, 4, 5, 6, 7, 8:]
Category [ID] ([NAME]): [COUNT] primary + [SEC_COUNT] secondary | Total loss: -$[LOSS]
  Hit rate (wins in this category / total trades in category): [WR]%
  Portfolio baseline hit rate: [BASELINE_WR]%
  Z-test: category WR vs baseline: p=[PVALUE] ([SIGNIFICANT/NOT SIGNIFICANT])
  4-week trend: [W-3]× → [W-2]× → [W-1]× → [THIS_WEEK]×

═══════ BOARD HYPOTHESES (for your evaluation) ═══════
[List hypotheses generated by the audit, with the evidence that triggered each:]

H1: [SPECIFIC_PARAMETER] should be changed from [OLD_VALUE] to [NEW_VALUE]
  Evidence: [CATEGORY] fired [N]× with [DESCRIPTION OF WHAT THE TRADES SHOWED]
  Estimated impact: If applied last week, would have avoided ~$[AMT] ([PCT]%) of losses
  Statistical confidence: [PCT]% (based on N=[N] occurrences)
  Board vote required: [YES/NO] | Timeline: [IMMEDIATE/3-WEEK CONFIRMATION REQUIRED]

[Repeat for each active hypothesis]

═══════ YOUR ANALYSIS QUESTIONS ═══════
Please answer all 8 questions. Same prompt has been sent to DeepSeek and Google AI Studio
independently. Your answers will be compared for consensus/divergence.

Q1 [CATEGORY 1a — DIRECTIONAL MACRO HEADWIND]:
[This week: N trades in this category | P&L impact: -$X]
Did the SPY direction at these entry times meaningfully predict the loss, or were there
confounding factors (VIX spike, earnings, time-of-day)? If the ORB gate had been applied
more strictly, which of these losses would have been avoided?

Q2 [CATEGORY 1b — VOLATILITY REGIME SIZING]:
[This week: N trades in this category | VIX range: X-Y | Avg VIX z-score at entry: Z]
Were these trades over-sized given the VIX regime at entry? If INTRADAY_STOP_ATR_MULT
had been [PROPOSED_VALUE] instead of [CURRENT_VALUE], which stops would NOT have been
triggered? Estimate % of loss recovery.

Q3 [CATEGORY 2 — MARGINAL SCORE]:
[This week: N trades in this category | Score distribution: X% at MIN_SCORE, Y% at MIN_SCORE+1]
Is there a meaningful quality drop at score=10 vs. score=12? Does the data support
raising MIN_SCORE to 11? What would the estimated win-rate impact be?

Q4 [CATEGORY 4 — TIME-OF-DAY BLEED]:
[This week: N trades entered in 9:30-9:45 AM window or 3:30-4:00 PM window]
Are losses in these windows systematic (occurring across different days/symbols) or
concentrated in a single event? Would blocking entry in these windows materially harm
the win rate on the remaining days?

Q5 [CATEGORY 6 — VIX STOP CRUSH]:
[This week: N trades where stop was hit intraday then reversed]
Given the VIX daily range on the loss days, what ATR multiplier would have preserved
the position past the intraday noise? Estimate the optimal INTRADAY_STOP_ATR_MULT.

Q6 [CATEGORIES 1b + 6 INTERACTION]:
If categories 1b and 6 fired on the same trades, is the root cause a single VIX-regime
issue, or are they genuinely independent failure modes? Should we treat them as one fix
(VIX-regime stop widening) or two separate parameter changes?

Q7 [LUCKY WINS]:
[N] winning trades this week also had failure-category conditions at entry.
Does the number of lucky wins in any category suggest we would over-filter profitable
setups if we blocked that category? Which proposed fixes have the highest risk of
reducing wins alongside losses?

Q8 [PRIORITY SYNTHESIS]:
Given all categories this week, rank your top 3 parameter changes by:
(a) estimated loss recovery, (b) risk of harming winning trades, (c) confidence.
Which single change would you implement first? What threshold of evidence do you need
before recommending a MIN_SCORE change (the highest-stakes parameter in the system)?

═══════ CONSTRAINTS ═══════
- Do not fabricate data. Work with what is provided.
- All recommendations must be quantified (estimated % loss recovery, expected win-rate impact).
- DS and GAI have advisory-only status. User is sole mandate authority.
- Flag any contradiction between your analysis and the board hypotheses listed above.
- Note: Changes to MIN_SCORE, Kelly fraction, or scoring weights require 3-week confirmation
  per our governance rules — please respect this in your recommendations.
```

---

## 8. DS/GAI 3-POINT AI SUMMARY FORMAT (Weekly Audit Version)

When DS/GAI responses return, Claude produces this summary before any board vote on implementation:

```
=== 3-POINT AI SUMMARY — WEEKLY AUDIT [YYYY-WNN] ===

POINT 1 — ALIGNMENT (DS + GAI consensus)
  [Category/Finding]: [X]/2 — DS [✓/✗] GAI [✓/✗]
  - Consensus finding: [description]
  - Both recommend: [specific action]
  ...

POINT 2 — WHAT DS/GAI BOTH AGREE ON THAT BOARD MISSED
  [Finding]: [description] — Action: [what this means for the hypothesis]
  Note: If DS+GAI both flag something the board didn't surface, treat as confirmed bug/gap.

POINT 3 — FORWARD-LOOKING (new risks DS/GAI identified)
  [Finding] ([DS only / GAI only / both]): [description]
  Priority: [P0-P3] | Board vote required? [Y/N]
  Note: These are novel risks that may require a separate board vote before acting.

CONFLICTS (DS ≠ GAI):
  [Finding]: DS says [X], GAI says [Y]
  Tiebreak: [reasoning] → Net recommendation: [Z]

IMPLEMENTATION GATE:
  Ready for board vote: [YES/NO]
  Blocked by: [REASON if NO]
```

---

## 9. CRON INTEGRATION

```cron
# OCI crontab — runs Friday 4:15 PM ET = 8:15 PM UTC
15 20 * * 5 /home/ubuntu/mtf-bot/venv/bin/python3 /home/ubuntu/mtf-bot/weekly_perf_audit.py >> /home/ubuntu/mtf-bot/logs/weekly_audit_cron.log 2>&1
```

Script must have:
- **RTH block**: refuses to run Mon-Fri 9:30 AM–4:00 PM ET
- **Atomic write**: tmp→replace on all output files
- **Timeout**: 90s SLA; each API call has 3s timeout, 2 retries, exponential backoff
- **Graceful degradation**: if Alpaca data API fails, use tracker P&L; if VIX unavailable, use cached; if FMP fails, skip earnings context
- **Circuit breaker**: if fills API AND trade_events.jsonl both unavailable, exit(1) with CRITICAL log

---

## 10. TEST HARNESS (pre-build requirement)

Per Kent Beck (*TDD*): **write tests before writing the classifier**.

Minimum: 15 synthetic trades (2 per category + boundary cases) + 5 observed trades from production logs.  
Each test: known-outcome trade → assert classifier returns correct primary category.  
Accuracy gate: classifier must hit ≥80% on observed trades before script is deployed.

---

## 11. BOARD Q&A — KEY FINDINGS

### Q: Minimum sample before proposing a change?
**Simons + López de Prado**: 3-week accumulation minimum (12-20 losers) for offensive changes. 2-week for defensive gates. Single-week patterns are statistical noise on 5-20 trades/week.

### Q: Should Kelly auto-adjust to category patterns?
**Thorp**: No. Kelly auto-responds to win-rate reduction via R-multiples — that IS the feedback. No manual weekly Kelly tweak. Recalibrate Kelly only if failure categories are corrupting the R-multiple measurement itself.

### Q: Unknown category threshold?
**Taleb**: >10% of weekly losers in category 8 → zero-based forensic review, NOT a parameter change. This is a signal the risk model has an unmodeled gap.

### Q: Multi-category — how to handle?
**López de Prado**: Correlated categories (e.g., 1b+6, both VIX) → ONE root cause, ONE fix. Hypothesis weight 0.7×. Orthogonal categories (e.g., 1a+4) → genuinely independent. Hypothesis weight 1.3×.

### Q: Track winners too?
**Asness + Jegadeesh-Titman**: YES. If "Category 4" wins 40% of the time in the TOD window, blocking that window would also kill 40% of profitable setups. Must compute hit-rate comparison before any proposal.

### Q: How fast to implement?
**Simons**: Defensive changes (stop widening, TOD gate, earnings block) in 1 week if firing 4×. Offensive changes (score, sizing) require 3 weeks minimum. Pre-approve parameter ranges so fixes deploy same day without waiting for Monday board vote.

### Q: PTJ's entry vs. exit split?
**Tudor Jones**: Categories 4, 5 → pre-flight GATES (block the entry). Categories 1b, 6, 7 → mid-trade EXIT ADJUSTMENTS (let entry happen, adjust how you manage it). Categories 1a, 2, 3 → ENTRY PARAMETER changes.

### Q: Douglas's discipline rule?
**Douglas**: Max 1 parameter change per week. 5-day cooldown after any change. "Traders who change parameters frequently create random-reward expectations." (*Trading in the Zone*)

### Q: PDT constraint and vol compounding?
**Sosnoff**: In a PDT-constrained account, you can't cut losers without burning a slot. Reduce BUCKET_A sizing at VIX>25 because you may be FORCED to hold overnight. The sizing fix is asymmetric — smaller on vol spikes, not smaller always.

---

## 12. PENDING BEFORE BUILD

**User must review and approve before any code is written:**

1. ✅ Analytics Board vote on feature: COMPLETE
2. ✅ 8 failure categories: APPROVED  
3. ✅ DS/GAI design feedback received (per prior session)
4. 🔲 **User reviews this design doc** — approve or modify
5. 🔲 **DS/GAI design review** — send DS/GAI design prompt (see §13)
6. 🔲 **User approves build start** → then full 9-step patch sequence begins (Step 1: write new file, not a patch)

Note: weekly_perf_audit.py is a NEW FILE — it is NOT in the RTH import chain, so no DS/GAI code review gate. DS/GAI review is for the DESIGN only (see §13).

---

## 13. DS/GAI DESIGN REVIEW PROMPT

**Send this same prompt to both DS and GAI before any code is written.**  
This reviews the DESIGN, not a code patch.

```
DESIGN REVIEW REQUEST — weekly_perf_audit.py

We are designing a new standalone script (not in RTH import chain) that runs
every Friday at 4:15 PM ET via OCI cron. It audits the prior week's trades,
classifies losses into 8 failure categories, and generates strategy adjustment
hypotheses for board + operator review. No parameter changes are auto-applied —
everything requires board vote + operator approval.

FULL DESIGN SPEC:
[paste §2 Failure Taxonomy, §3 Strategy Feedback Loop, §5 Output Spec from this document]

CURRENT PARAMETERS:
MIN_SCORE: 10/12 | KELLY_FRACTION: 0.25 | KELLY_MAX_RISK_PCT: 6%
INTRADAY_STOP_ATR_MULT: 1.25 | VIX_STOP_WIDEN_THRESHOLD_1: 25

GOVERNANCE GUARDRAILS:
1. 3-week minimum for offensive parameter changes
2. 40% estimated impact minimum to trigger a board vote
3. 3-week post-implementation tracking before permanent
4. Max 1 parameter change per week
5. User is sole mandate authority — DS/GAI advisory only

DESIGN REVIEW QUESTIONS:

Q1: Are the 8 failure categories well-defined and non-overlapping enough for
deterministic rule-based classification? For a system with 5-20 trades/week,
are there any categories that will almost never fire (too rare to be useful)?
Are there common failure modes in confluence-scoring momentum strategies that
we haven't included?

Q2: The strategy feedback loop requires 3-week confirmation for offensive changes.
Given the account size ($2,500-$3,000) and trade frequency (5-20/week), is this
too slow (burning money while waiting) or too fast (risk of overfitting)? What
is the optimal confirmation window for this account profile?

Q3: The VIX z-score normalization (20-day rolling mean) is used instead of absolute
thresholds. Is this appropriate? What edge cases break the z-score approach
(e.g., regime change in the middle of the 20-day window, VIX stuck below 12)?

Q4: The "lucky wins" tracking (winners with failure-category conditions) is designed
to prevent overfitting. Is the 2-proportion z-test at p < 0.20 an appropriate
threshold for this sample size? Could we use a simpler heuristic (e.g., "if
>20% of category total are wins, defer proposal")?

Q5: The DS/GAI weekly report prompt (§7 above) is sent via Slack and forwarded
by the operator. Is there any critical context missing from the prompt that
would prevent you from giving useful analysis? What additional data fields
would make your analysis significantly more accurate?

Q6: Are there any risks in the closed-loop design (detection → board → DS/GAI →
user → implementation → validation) that we haven't accounted for? Specifically:
(a) feedback loop poisoning (mislabeling corrupts strategy), (b) parameter instability
from too-frequent changes, (c) selection bias in the failure classifier itself.

Q7: Given our 3 governance guardrails, what failure mode in the audit system
is most likely to cause harm to the account in the first 4-8 weeks of operation?
What safeguard would you add?

Please respond with specific, quantifiable recommendations where possible.
Flag any design assumptions that seem incorrect or risky.
```

---

---

## 14. DS/GAI DESIGN REVIEW ADDITIONS (approved 2026-05-26 S40 — incorporated S41)

Four additions from DS/GAI design review, approved by user. These are now part of the canonical spec.

### Addition 1 — Emergency Escalation Clause (>25% weekly drawdown)

**Source:** DS + GAI consensus (both flagged independently)

If the account equity drops >25% in a single week (calculated as week-close equity vs. week-open equity), `weekly_perf_audit.py` must:
1. Escalate immediately — do not wait for Friday 4:15 PM cron. Trigger is checked intraday via a lightweight check in the nightly audit.
2. Post a CRITICAL Slack alert with all failure categories from partial-week data.
3. Block new entries via `_set_halt_entries(True)` equivalent — post a manual halt recommendation to Slack (the script cannot call the bot directly, but it posts the halt payload to Slack for operator action).
4. Generate an emergency audit report (`logs/weekly_perf_audit_EMERGENCY_YYYY-MM-DD.html`) with the partial-week data.
5. DS/GAI prompt is sent immediately in the Slack alert body.

**Threshold:** `(week_close_equity - week_open_equity) / week_open_equity <= -0.25`

**Note:** The 25% threshold is aggressive for paper trading ($2,852 × 25% = $713 drawdown). This is appropriate — at paper, we want to catch runaway scenarios quickly. Revisit at live deployment.

### Addition 2 — VIX Floor LOW_VOL Regime (VIX < 15)

**Source:** DS + GAI consensus

Add a 6th MRI-equivalent regime for the audit: `LOW_VOL` when VIX spot < 15 at close of any trading day in the week.

**Why:** VIX < 15 is the complacency regime. Momentum bots that perform well in mid-vol (VIX 15–25) often OVER-size in low-vol because ATR is compressed — giving artificially tight stops that get hit on normal noise. This is a distinct failure mode not covered by categories 1b or 6 (both require high-vol).

**Classification change:**
- Add `LOW_VOL` flag to every trade's `market_context` dict
- In Category 1b (Volatility Regime Sizing), add sub-category: `1b_low_vol_tight_stop` — fires when VIX < 15 AND stop_distance < 0.5 × symbol_30d_avg_atr
- VIX z-score still computed relative to 20d mean — but also flag absolute threshold separately

**VIX regime table (for report):**
| VIX Level | Regime | Label | Risk Posture |
|-----------|--------|-------|-------------|
| < 15 | LOW_VOL | Complacency | Watch for size creep; ATR may be misleadingly tight |
| 15–20 | NORMAL | Standard | No adjustment |
| 20–25 | ELEVATED | Caution | VIX_STOP_WIDEN_THRESHOLD_1 approaching |
| 25–30 | HIGH | Stressed | VIX_STOP_WIDEN_MULT_1 active |
| > 30 | EXTREME | Crisis | VIX_STOP_WIDEN_MULT_2 active |

### Addition 3 — Monthly Mislabeling Cascade Prevention Gate

**Source:** DS + GAI consensus

**Risk flagged:** The classifier's rules could become systematically wrong as market regimes shift. If VIX normalization parameters drift (e.g., a 6-month low-vol period shifts the 20d mean down), the z-score thresholds silently re-target different vol levels. The feedback loop then learns on mislabeled data → proposes parameter changes based on phantom categories.

**Monthly gate (runs first Friday of each month, in addition to weekly):**
1. Pull last 4 weeks of classified trades (minimum 10 trades required — `MIN_TRADES_MONTHLY_GATE = 10`)
2. Sample 5 random trades from each firing category and manually re-classify them using raw data
3. If sampled re-classification disagrees with stored label on ≥30% of sampled trades → **MISLABELING ALERT**
4. On mislabeling alert: pause hypothesis generation for 2 weeks, post CRITICAL Slack with the discrepancy, request operator review
5. If fewer than 10 total trades in the month → mark `INSUFFICIENT_DATA`, skip hypothesis generation

**Implementation:** The monthly gate is a separate function `_monthly_mislabeling_check()` that runs only on first-Friday of month. Weekly audit function calls it as a post-step.

### Addition 4 — MIN_TRADES Thresholds (per change type)

**Source:** DS + GAI consensus. Replaces the simpler "3-week minimum" rule in §6.

The prior governance guardrails used time (weeks) as the gate. DS and GAI both recommended sample-size gates instead — time-based gates can pass with 3 trades across 3 weeks (useless) or force a 3-week wait when 30 trades fire in 1 week (over-cautious).

**New thresholds (replace §6 Governance Guardrails item 1):**

| Change Type | MIN_TRADES | Description |
|-------------|-----------|-------------|
| **Offensive** | ≥20 | MIN_SCORE, Kelly fraction, scoring weights, target multiplier |
| **Defensive** | ≥12 | TOD gate, earnings block, stop widening, VIX threshold |
| **Emergency** | ≥5 | Only for 25%+ weekly drawdown escalation (Addition 1) |

These thresholds REPLACE the week-based minimums. The time-based rule is now a SUPPLEMENT only: if MIN_TRADES is met but the trades all came from a single week (concentrated event), add a 2-week confirmation wait. If trades span ≥2 distinct market weeks, no time-based wait required.

**Implementation:** `_check_hypothesis_gate(category, trade_count, change_type)` → returns `READY`, `INSUFFICIENT_DATA (N/MIN_TRADES)`, or `CONCENTRATED_NEEDS_CONFIRMATION`.

---

*Design spec v1.1 — 2026-05-27 S41 — DS/GAI additions incorporated. READY TO BUILD.*
