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

The overnight early exit is the **largest identified leak**:
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
