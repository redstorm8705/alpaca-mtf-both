# Missed-move exit study + report accuracy audit (2026-09-26)
**Frame:** paper account, $2.5K → $25K goal, data collection first. All work was read-only, sourced from Alpaca fills and bars.

## Exit study (Core swing + day tier; QHM/F6 excluded; qty-weighted; fully-closed lots only; since 2026-07-01)
- Scope: 119 round trips.
- "Money left" is the best price in the trade's direction within +3 trading days after the exit, times qty. It is a hindsight UPPER BOUND, not an achievable amount.
- Totals: $1,200 over +3 days and $426 within the same session.

By exit reason:

| Exit reason | n | Sum left (+3d) | Avg left (+3d) |
|---|---|---|---|
| Unlogged (logging gap) | 54 | $555 | — |
| **Overnight early exit (`overnight_atr_buffer_exit`)** | 32 | $343 | $10.71 |
| Day-tier protective stop | 9 | $216 | $24 |

The overnight early exit looked like the largest leak by the hindsight measure — SEE CORRECTION BELOW (it is net protective):
- Rule: from 10:00 ET, a position 0.25–0.65 ATR below entry for 9 scans (~45 min) is closed. That is before the 1.25×ATR hard stop (`exit_logic.py` ~L1225-1370, `param_engine.get_be_buffer_mult`).
- Average exit was about −0.45R.
- 87.5% of these exits drifted favourably afterwards.
- 0 of 24 checkable trades reached their original target within 3 days.

**Status:** a counterfactual replay (hold to stop/target/10 days) is in progress before any recommendation.

Other findings:
- Top missed moves (+3d, hindsight): META day-tier $91, PANW $84, AMZN day-tier $47, QQQ $46, NET $43.
- **Logging gap:** 84 of 197 exit fragments have no exit reason. Entry logging with the initial stop only starts on 2026-07-29.
- **Weekly AI claims:**
  - "Overnight ATR multiplier 1.20x→1.50x": UNSUPPORTED. No such parameter exists; 1.20 is `INTRADAY_STOP_ATR_MULT`.
  - "15-min calm period before stop-breach closures": UNSUPPORTED, and it contradicts the design.
  - "Overnight trades stop out early": partially true. It is the early-exit rule above.
  - "11–12 scores ≈ 10": consistent with the earlier audit.

## Weekly post-mortem (`weekly_postmortem.py`) — "$76.62 left on table" is wrong
1. It mixes tiers: FIFO by symbol only (L266-343).
2. Qty scaling is used but never shown (L658).
3. Partial exits are counted as missed while the rest stays open.
4. The Stage and "vs wk" columns compare every exit to Friday's close regardless of exit day (L477-486, L650-660).

The P&L of +$16.31 and W/L 9/9 are arithmetically correct but not tier-correct.

## Strategy Edge Report (`monthly_review.py` / `weekly_review.py`) — three unreconciled sources in one card
- **Header** mixes the ledger (34% WR, +$40.71; correct) with `trade_log.json` (179 trades, PF 0.55; corrupted source).
- **Body** uses `fifo_edge.json` LEGS (416 legs, 41% WR, PF 1.51), which inflate win rate.
- **Correct entry-level, all-tier figures:** 309 trades, 34% WR, +$71.05 realized, +$40.71 lifetime.
- **Exit-reason, hold-time and drawdown tiles** use `trade_log.json`:
  - "Other" (131) is uncategorised reasons: `external_close`, `overnight_atr_buffer_exit`, `safe_close_all`.
  - "Intraday hold 22h21m" trusts a stale overnight flag instead of dates.
  - Max DD $918.93 is from corrupted pnl; Alpaca's real max drawdown is $535.34.

## CORRECTION — counterfactual replay of the overnight early exit (2026-09-26)
**Sample:** 31 trades since 2026-06-01, all replayable. The alternative is to hold until the stored hard stop, the target, or 10 trading days (mark at close), with gap-through filled at the open.

| | Actual (rule as-is) | Held instead |
|---|---|---|
| Total P&L | **−$283.81** | **−$457.33** |
| Winners | 0 of 31 | 5 of 31 |
| Hit the full hard stop | — | 17 |
| Reached target | — | 0 |
| 10-day time exit | — | 14 |
| Worst single trade | — | −$56.50 (PANW) |

**The rule is net PROTECTIVE:** it saved $173.52. The earlier hindsight "largest leak" label was WRONG. That +3-day best-price measure ignores that 55% of these trades went on to hit the full stop.

**Sensitivities (not fully replayed; hypothesis only):**
- Delaying the rule to 11:00 ET would affect 11 of 31.
- Doubling the buffer would have suppressed 24 of 31 at the exit moment.

**Implication:** the problem is the ENTRIES (0 of 31 reached target), not this exit. That supports the score-rebuild priority (P1) and not loosening the exit.

**Assumptions:**
- Stop checked before target within a shared bar.
- Targets for RIVN/HOOD on 7/24 estimated at 2:1.
- The TQQQ stop is flagged unreliable.

## Rafael direction (2026-09-26)
1. **Profit lock.** Balance letting runners run against never turning a winner into a loser. Rafael asked: "if a trade is up X% relative to the stock's size, the stop should be there."
   - An MFE / profit-lock study is running. It replays dynamic lock-to-breakeven and trail rules defined in ATR units.
   - It also asks why the existing break-even and trail logic has not prevented winners turning into losers.
2. **Entries (the confirmed root problem).** The swing entry strategy, and to a lesser extent QHM entries, must be rebuilt and optimized dynamically. This is the score rebuild, now the top strategic priority after the safety P0s.

## Profit-lock / MFE study (2026-09-26)
**Sample:** 47 closed core swing round trips since 2026-07-01 (Alpaca fills, IEX 5-min bars).

**Distances:**

| | Median | IQR |
|---|---|---|
| Target distance | 16.1% (2.08R, 3.94×ATR) | 10.7–27.0% |
| Best price reached (MFE) | 1.29% (0.15R, 0.36×ATR) | 0.31–4.49% |

**Actual results:**
- 9 winners and 38 losers (19% win rate), total −$202.6.
- Losers that were up at some point: 12 reached ≥0.25R, 9 reached ≥0.5R, 4 reached ≥0.75R, and 1 reached ≥1R.

**Why the existing protection never arms:**
- Breakeven and the trail are armed ONLY after the T1 partial exit (`exit_logic.py` ~L827-839).
- T1 sits at 40% of a ~2.1R target, about 0.83R or 1.6×ATR (`TRANCHE_FRACS`, ~L441).
- The median MFE is 0.15R, so the protection almost never arms.
- The MRI breakeven push is regime-gated and does not respond to the trade's own MFE.

**Replay of dynamic locks** (stop fills at the stop or at a gap open):

| Rule | Total P&L | Win rate |
|---|---|---|
| Actual | −$202.6 | 19% |
| Breakeven at MFE ≥ 0.5×ATR, then trail 0.5×ATR below the best price | −$37.5 | 43% |
| Same, ≥ 0.75×ATR | −$40.7 | — |
| Same, ≥ 0.3×ATR | −$47.7 | — |
| R-based variant | −$65.5 | — |
| Wide trails (1.0–1.5×ATR) | about −$150 | — |

- ATR-based rules beat R-based ones.
- No rule makes the sample positive. The entries and targets remain the root problem: targets are about 12× the typical MFE.

**Caveats:**
- N=47 with 9 winners, so the 0.5×ATR peak is in-sample. Treat it as a direction, not a tuned constant.
- ATR was recomputed independently. P&L is per share.

**Recommendation (pending BGGN):**
- Add an MFE-armed dynamic profit lock, separate from the tranche ladder. Arm at k×ATR and trail m×ATR, with k and m derived from the rolling MFE distribution. Fallback: k=0.5, m=0.5 in ATR units.
- It only ever tightens a stop. It never loosens one.
- Pair it with the dynamic target (P1 #5) and the entry rebuild.

## BGGN on profit lock + size (2026-09-26) — Gro, GAI and board sizing seat converge
Rafael's intent: take profit sooner AND use more size.
- **ATR is already per stock.** Each stock's own 14 daily bars are measured before entry (`data/premarket.calculate_atr` → % of price × entry price). TSLA's lock distance is therefore wider in $ than AAPL's automatically.

**Design:**

(a) **The lock.**
- Move to breakeven at 0.5×ATR MFE, then trail 0.5×ATR behind the best price.
- It is independent of the tranche ladder and only ever tightens a stop.
- Per-stock adaptation: log each symbol's MFE distribution now. Let the multiplier drift (bounded 0.4–0.6×ATR) only once a symbol has ≥30 of its own trades. The Kelly warm-up gate works the same way.

(b) **Size.**
- Book lock-era trades to their OWN Kelly key, mirroring the mean-reversion key. The lock changes the payoff distribution.
- Size then rises AUTOMATICALLY as the rolling out-of-sample Kelly for that key turns positive.
- At n<30, or while the edge is negative, the existing `KELLY_MIN_RISK_PCT` floor (0.75%) keeps trades and data flowing.
- There is no hand-added "lock bonus".
- Rejected: an up-front size-up now. The median trade reaches only 0.15R, so most losers still hit the full pre-lock stop on the bigger share count.

(c) **Guardrails (unchanged):** `KELLY_MAX_RISK_PCT` 4.5% per-trade re-clamp, and the 7% daily kill switch.

## BGGN on the F6 activation trigger (2026-09-26) — Gro, GAI and board macro seat converge; backtest in progress
**Current rule:** SPY close ≤ −max(2%, 0.15×VIX)%, spot VIX only, flat 20% of cash per event.

**Proposed rule:**
- **Gate:** the SPY drop (existing formula, or an intraday low ≤ −3.5%) AND VIX confirmation. VIX confirmation is VIX ≥ 1.3× its 20-day average OR VIX/VIX3M > 1 (term inversion).
- **Ladder:**

| Tier | Condition | Cash deployed | Names |
|---|---|---|---|
| A | SPY −2% to −4% | 10% | 2 |
| B | SPY −4% to −7%, with inversion | 20% | 3 |
| C | ≤ −7% in a day, or ≤ −10% over 3 days, with VIX > 35 | 30% | all |

- **Name selection:** prefer names down the most relative to SPY, and rotate buckets.
- **Anti-overtrade:** 4 events per month, $200 cash floor, and a 5-day cooldown after a Tier A event only.
- **Status:** historical fire rates are UNVERIFIED. A 2020–2025 backtest is running.

## F6 trigger backtest 2020-01-01 → 2026-09-25 (SPY SIP daily from Alpaca; VIX/VIX3M daily from yfinance; OCI `/tmp/f6_backtest.py`)
**Fires per year:**

| | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | Total |
|---|---|---|---|---|---|---|---|
| Proposed | 10 | 0 | 3 | 0 | 1 | 5 | 19 |
| Current | 0 | 0 | 1 | 0 | 0 | 1 | 2 |

- Calm years: zero fires under both rules.

**The current rule breaks exactly in crashes.** Its threshold, max(2%, 0.15×VIX), rises with VIX:
- It fired **zero** times in Feb–Mar 2020. On 2020-03-16 (−10.8%) its threshold was −12.4%.
- It missed 5 Aug 2024.
- It caught only 1 of 4 April 2025 crash days.

**Forward returns after proposed fires:**
- Mid/late-Mar 2020, Apr 2020, Aug 2024 and Apr 2025 fires mostly returned +12% to +27% over the next 60 days.
- Early Feb–Mar 2020 fires were down 12–31% after 20 days, because the crash was still unfolding. Laddering sends more cash to the deeper tiers later.
- 2022 fires were flat.

**Spec gaps found → fixes:**
1. The 4-events-per-month cap blocked the worst day of the crash (2020-03-16, −10.8%, VIX 82.7). → Tier C bypasses the monthly count cap, bounded by cash only.
2. An intraday-low-only fire on a positive close had no tier (2022-01-24). → Tier by the WORSE of the close return and the intraday low, and require a negative close.
3. The rule misses grinding bear-market drops without a VIX spike (2022-09-13). → Accepted: Rafael wants crash-like only.

## CORRECTION + exit-system redesign (2026-09-26)
**Correction.** An independent break-even move ALREADY exists (`exit_logic` "0C", ~L1484-1520): at ≥0.5R the stop goes to entry and a broker DAY stop is placed. The study's "only after T1" claim was wrong.

**What is actually missing, and why (answers to Rafael's questions):**
- **No trail until the T1 partial (~0.83R).** The design assumed trades reach T1, but the median MFE is 0.15R. This was never measured.
- **Exits share the 5-min scan loop.** The cycle is median 92 s (p90 147 s), so each position is checked about every 6.5 min. Nobody chose this cadence for exits; it is a side effect.
- **The software stop needs 3 scans** on distinct 15-min bars. No recorded rationale for 3 was found. It applies to same-day swing entries, which have no broker stop until the 15:45 sweep. Prod log: 39 breach-monitoring warnings, 8 confirmed stops.

**Realistic replay.**
- Scope: 85 core swing round trips since 2026-07-01, 5-min bars, price observed at bar close only. The broker stop fills at the stop or the gap open. The current rules are modelled with the 3-scan confirm.
- Results:

| Rule | Total P&L | Win rate |
|---|---|---|
| Actual | −$468.93 | 20% |
| Current rules (modelled) | −$415.12 | 31% |
| Arm at 0.5R (existing) + trail 0.5×ATR | −$313.84 | 47% |
| Arm at 0.3×ATR + trail 0.5×ATR | −$249.38 | 40% |
| Arm at 0.5×ATR + trail 0.5×ATR | −$314.07 | 51% |
| Wider trails (0.75–1.0×ATR) | −$354…−$520 | — |

- **Read:** the arm point is roughly fine. The missing piece is the TRAIL, which is worth about $100–165 over 85 trades (~25–40%).
- No variant is profitable. Entries and targets remain the main problem.
- **Caveats:** 9-cell in-sample grid; the VIX≥30 override was not modelled; the earlier 47-trade study used a narrower filter.
- The stale doc was also found: config uses `INTRADAY_STOP_ATR_MULT=1.20`, while CLAUDE.md says 1.25.

**Redesign (Gro, GAI and the board execution seat converge):**
1. **Trail from arm.** At the earlier of 0.5R or 0.5×ATR MFE, the stop goes to entry, then trails 0.5×ATR behind the best observed price. The trail is tighten-only, and a max/min guard stops the partial-exit and trail writers from moving it backwards. The arm point and trail are logged per trade so k and m can adapt per stock later.
2. **A broker stop on every swing position from entry.** It is placed beyond the larger of (a) the recent-noise 90th-percentile true range and (b) the entry stop. It is time-of-day aware and uses the existing VIX curve. This removes the 3-scan and cadence gaps for the stop itself.
3. **A fast exit loop.** A separate cron process following the `run_day_tier.py` pattern: flock singleton, ~30–60 s cadence, one multi-symbol snapshot call, shared rate gate and tracker lock, tighten-only moves, no entries.

**Rollout:** 1 → 2 → 3, each gated. The front-loaded replay above stands in for a post-ship shadow (CLAUDE.md Rule B).

## Rafael decisions (2026-09-26, later)
1. **Structure-aware trail after +0.5×.** Trail below real support (VWAP, fast MAs, swing lows, higher-timeframe flipped levels) with a volatility buffer. Tighten-only, never below entry once armed.
2. **Continuation re-entry.** Trigger: a break above a meaningful level (usually the high of day), a back-test of that level, and a hold. The re-entry stop sits AT THE RE-ENTRY PRICE.
   - Rafael wants the BGGN's analysis, with a senior-engineer recommendation, when the exit work resumes.
   - The re-entry cooldown (`execution/reentry_cooldown.py`) blocks this today and must allow it.
3. **SEQUENCING:** the entry problem is NOT solved. The trade lifecycle is rebuilt starting with ENTRIES, fully shipped. Exits (trail, re-entry, broker-stop-from-entry, fast loop) resume after that.
