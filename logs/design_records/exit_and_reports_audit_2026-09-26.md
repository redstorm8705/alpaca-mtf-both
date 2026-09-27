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
