# Day tier — dynamic all-session momentum entry + dollar risk budget (2026-10-04, Claude-signed) — DESIGN, not yet built

## Owner direction (Rafael 2026-10-04)
- No sector or correlation limits in the day tier ("we can go all one sector if that's what's moving").
- Momentum names don't pull back on buy days: capture the move while it happens; "this can't be static".
- No mid-day time stop; keep the end-of-day flatten only.
- Remove the 3-trade concurrency cap; measure on the day's dollar loss only.
- Kill switches UNCHANGED (Rafael 2026-10-04, final): keep the -7% account-wide kill (all trading tiers, QHM/F6
  excluded) and the -5% day-tier-only kill. No Invariant #6 change.

## Facts verified at source (2026-10-04)
- No sector/correlation gate exists in the day tier today (grep day_trade_manager / day_tier_* / run_day_tier).
- Only multi-position limit: DAYTRADE_MAX_CONCURRENT_POSITIONS = 3 (place_entry ~L1263), sized so 3 x 1.5% risk <= the
  5% tier kill (DAYTRADE_TIER_KILL_EQUITY_PCT, tier_kill_check: day-tier realized + open P&L, force-flat).
- Account kill (risk_manager.check_kill_switch, MAX_DAILY_LOSS_PCT 7% paper): intraday P&L of ALL trading tiers
  except QHM/F6 buy-and-hold; trips -> halts new entries in run_cycle (swing) AND run_day_tier.py:529 (day tier).
- Track A enters only at GEX levels; Track B (momentum) only ~09:50-11:05 ET, one entry/symbol/day, opening-range
  break only, FIXED volume multiple.

## Aligned design (board Simons/Shaw + Harris/PTJ, Gro, GAI — converged; owner overrides applied)
Signals (5m completed bars; every threshold = a percentile of the symbol's OWN last 20 sessions at the same time-of-day
slot (+-2), or a cross-sectional rank; one rank parameter q, PROV 0.70, chosen by walk-forward replay only):
- S1 participation (mandatory): cumulative volume to bar k AND last-3-bar volume, each >= q-percentile of own history.
- S2 relative strength vs SPY: ln-return from today's open minus beta x SPY's, rank >= q vs own history.
- S3 trigger (mandatory): close above the running session high (mirror for shorts) with true range >= own q-percentile
  and close-location above own median — the "while it is happening" event; the opening-range break is its first case.
- S4 VWAP slope (6 bars, ATR-normalized) rank >= q; anti-chase: extension from VWAP above own p95 -> WAIT.
- S5 cross-sectional rank of sector-residualized relative strength: top third of the universe on the trade side.
- Sector confirmation (sector ETF strong vs SPY on the trade side) = a CONFIRMER, never a limit.
- ENTER = existing gates (daily side, 2m/5m gate, fresh data) + S3 + S1 + >= 2 of {S2, S4, S5, sector} + stop-distance gate.
Stop: last 3-bar structural pivot (or launch bar) minus 0.5 x own typical bar; floor 1.0 x typical bar; if wider than own
p90 -> WAIT; ratchet-only trail at the entry risk distance; no take-profit; end-of-day flatten; no time stop.
Re-entry: max 2 momentum entries per symbol per day; the second needs a fresh close above the session high at the prior
exit and full re-qualification; blocked after a >1R symbol-day loss.
Window: replace 09:50-11:05 with all-session; no entries during the opening-range bars; last-3-bars execution floor.
Fix: build_session_frame must not read a quiet IEX minute as a halt (slot-aware missing-bar frequency).

## Concurrency -> dollar budget (owner direction; replaces the 3-trade cap)
A new entry is allowed iff today's realized day-tier loss + the loss-if-stopped of every open day-tier trade (incl. the
candidate, with a slippage allowance) stays within the day-tier daily dollar limit = DAYTRADE_TIER_KILL_EQUITY_PCT (5%)
x day-start equity (Rafael 2026-10-04). The -7% account kill stays above it unchanged. Rationale: the board's concrete
correlated-gap scenario (3 semis stopped together = 5.4% > 5% kill) is bounded by budgeting open stop-risk in dollars,
not by a position count or a sector cap.

## Rejected reviewer suggestions (conflict with owner direction)
Gro: sector cap (max 2 per sector); 14:45 forced flat. GAI: liquidate the weakest position for a new signal; cut per-trade
risk to 0.75%. Gro/GAI fixed-number thresholds (z > 1.5, P85, etc.) -> replaced by the board's own-history percentiles.

## Next (Rule C, before build ships)
Replay 15 names x ~1 year IEX 5m, walk-forward (thresholds use only prior 20 sessions), last 60 sessions held out; regimes
tagged (trend/chop/VIX>25/gap/earnings/FOMC-CPI); baselines: current Track B, random-time entry at equal frequency, 09:35
entry. Pre-ship reversal: held-out net expectancy <= random-time null or bootstrap 90% CI lower bound < 0 (deflated for q
values tried). Live reversal: after 30 momentum entries, mean R below the replay's 5th-percentile bound -> kill flag off.
Risk-path (frequency + concurrency up) -> board gate incl. masked-loss seat.
