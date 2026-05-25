# Board Audit — main.py Exit Strategy Full Read
**Date:** 2026-04-21 PT | **Session:** 5d04580f | **Auditors:** TB + AB + BoD (all 26 members)
**Alpaca live snapshot:** portfolio=$2,902.11 | equity=$2,902.11 | long_mkt_val=$2,640.22 | cash=$261.89 | PDT=2/3 | daytrade_count=2 | shorting_enabled=True

---

## Scope
Full sequential read of main.py (lines 1–6200+). Exit strategy, partial exit, trail stop, overnight logic, AH cycle, startup reconciliation. Trail_stop floor bug fixed this session (3 sites).

---

## Exit Architecture (8 Paths, Priority Order)

| # | Path | Trigger | Gate | PDT |
|---|------|---------|------|-----|
| 0A | overnight_breakeven | price ≤ entry−0.25×ATR for 9 consecutive scans after 10 AM ET | prior-session only; 10 AM grace | Not PDT-gated (next-day close) |
| 0B | thesis_invalidation | SPY BROAD_*/EXTREME conflicts with direction | Excludes SECTOR | PDT=3/3 same-day: log only |
| 0C | be_stop_promoted | price ≥ 0.5R profit before T1 | once per trade | N/A |
| 1 | hard_stop | 3 consecutive 15-min bar closes beyond stop/trail | U-3: bar timestamp gates counter | PDT=3/3: GTC stop placed |
| 2 | target | price ≥ target | immediate | PDT=3/3: target_hit_pending deferred to AH |
| 3 | reversal (overnight) | 10 confirmed / 15 force-exit; PDT-forced: 6 | score-drop gate (<7 for 2 scans); counter decays 1/scan | PDT=3/3 opened-today: blocked |
| 4 | reversal (RTH) | 6 scans (30 min); OR 2×entry_touch outside 0.5×ATR noise band | score-drop gate same | PDT=3/3 opened-today: blocked |
| 5 | opposite_signal | ≥11/12 conviction opposite direction during RTH | #12c | Normal PDT |

## Partial Exit System
- Tranches: T1=20%, T2=40%, T3=60% of target distance. Each closes 25% original qty.
- Trail activates after T1, floored by promoted stop (fixed this session).
- Stressed (BROAD_*/VIX>30): T2→0.85R, T3→50%.
- PDT-dynamic: 3/3=no same-day (GTC limit placed); 2/3=T2 only; 1/3=T1+T2; 0=all.

## Trail Stop Floor Fix (this session)
**Before:** Initial trail set = current_price − trail_dist, no floor. Could initialize below entry_price even after T1 promoted stop to breakeven.
**After:** All 3 sites: `max(trail_val, trade.get("stop",0.0))` for longs. Stop promotion executed BEFORE trail set at Site 3.

---

## Board Findings — Ranked by Severity

### CRITICAL

**TALEB (tail risk):** No mechanism to widen GTC stops on existing overnight positions when VIX crosses 30 during AH. Stops were set at entry-time VIX; overnight gap risk unmitigated against them. The VIX stop-widening multiplier applies at entry only — existing GTC stops are never updated post-submission.

**KATSUYAMA (execution integrity):** Hard stop 3-breach confirmation (U-3) uses bar timestamp for gating but `current_price` is overridden by `get_latest_trade()`. A live-price breach on an unchanged bar timestamp is silently discarded. Gap-down open past stop confirmed via live price but stale bar timestamp = up to 15 min before counter increments.

### HIGH

**MAJORS (observability):** `_fill_fallback_count` and `_rth_day_stop_failure_counts` are declared as daily counters but have no midnight reset. Stale values accumulate across bot restarts within the same calendar day (or across days if bot runs continuously). ANOMALY-1/5 alerts may fire on session open reflecting prior-day failures.

**HARRIS (microstructure):** At VIX > 30, the fixed 3-scan hard stop confirmation allows 45+ min of confirmed breach while position continues to bleed. Recommend: reduce to 1-scan immediate at VIX > 30 for positions already at ≤0R.

### MEDIUM

**SHAW (overnight/macro):** Overnight breakeven buffer (entry−0.25×ATR) is ATR-fixed, not VIX-adjusted. In low-vol (VIX<15), 0.25×ATR is very tight → fires frequently on noise. In high-vol (VIX>30), 0.25×ATR is more tolerant but that's exactly when overnight risk is highest. Recommend: 0.25×ATR at VIX<20, 0.40×ATR at VIX 20–30, 0.50×ATR at VIX>30.

**DERMAN (model risk):** `overnight_breakeven` name is operationally misleading — fires at entry−0.25×ATR (a loss), not at cost. Weekly review TQI lookup hardcoded to string "overnight_breakeven" — if renamed, lookup breaks silently.

**DALIO (regime):** Macro regime detector refreshed weekly via cron. If bot runs continuously Mon→Fri without hitting pre-market path, macro_regime_tier could be 7-day stale by Friday. Confirm cron is running.

### LOW

**GENE KIM (observability):** `_rth_day_stop_failure_counts` never cleared between restarts. Symbol that had 2 failures in prior week would fire ANOMALY-1 CRITICAL on next session open before any new failure.

**PETERFFY (infrastructure):** RTH mid-session restart: DAY stop IDs from prior session remain in tracker (Patch 3 only runs at pre-market). `_submit_rth_day_stops()` sees them as protected and skips resubmission — but orders expired at 4 PM prior day. Mitigation: restarts are rare and watchdog fires Slack alert before execv.

---

## Live Account Context (Alpaca MCP — 2026-04-21)
- portfolio_value: $2,902.11 (started ~$2,500 → +$402.11 unrealized+realized)
- equity: $2,902.11 | long_market_value: $2,640.22 | cash: $261.89
- PDT daytrade_count: 2/3 (rolls Mon 2026-04-28)
- shorting_enabled: True | buying_power: $3,164
- daytrading_buying_power: $0 (pattern_day_trader=False, margined)

---

## Exit Strategy: What Changed This Session vs Prior
- **Trail floor fix:** Eliminated the primary post-T1 profit erosion path. Previously trail could initialize below breakeven; now floored by promoted stop at all 3 sites.
- **Overnight breakeven exit:** Was already correctly coded (9-scan gate, 10 AM grace, counter decay). NOT the cause of SPY -$7.70 loss — that was correct behavior: position was at entry−0.25×ATR for 9 scans.
- **Nightly audit:** Now reads trade_events.jsonl (real data) not trade_log.json (never existed).
- **Midday audit:** Gemini prompt now includes stop/T1/T2/T3 levels per trade.

---

## Open Items From This Audit (Board-flagged, not yet addressed)
- [ ] VOTE: VIX-adjust GTC stop widening for existing overnight positions (Taleb CRITICAL)
- [ ] VOTE: Reduce hard stop confirmation to 1-scan at VIX>30 (Harris HIGH)
- [ ] FIX: Reset `_fill_fallback_count` and `_rth_day_stop_failure_counts` at midnight (Majors HIGH)
- [ ] VOTE: VIX-adjust overnight breakeven buffer (Shaw MEDIUM)
- [ ] RENAME: `overnight_breakeven` → `overnight_be_buffer_exit` (Derman MEDIUM, requires TQI map update)
- [ ] VERIFY: Macro regime cron is running and refreshing weekly

