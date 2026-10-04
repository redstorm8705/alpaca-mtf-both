# Confluence 2.0 swing tier — megacap breakout (design record, 2026-09-29, Claude-signed)

## Evidence
- Live since 2026-04-06 (fills, FIFO): 12-point swing -$126.00 / 154 trades (PF 0.89; shorts -$123.98, PF 0.66);
  QHM +$196.98 inferred (PF 3.83). Record: logs/design_records/entry_rebuild_2026-09-26.md (labs 3-9).
- Lab 8c (corrected, true beta): point-in-time top-10 dollar volume, 55-day closing-high breakout above the 200-day SMA,
  exit on a close below the prior 20-day low or 30 sessions, 2.5 ATR stop: +2.6%/trade absolute, +1.26% beta-adjusted,
  t 2.18 (daily-portfolio t 0.94). Mostly high-beta megacap exposure in rising markets plus positive skew.
- Lab 9 (post-hoc top-20, review pending): heavy-volume gap-up through the 60-day high, 20-session hold: +2.6%/trade,
  +1.23% beta-adjusted, t 3.2, 10/11 years. Breakdown shorts lose in every slice.

## Build (BGGN: board risk + execution seats APPROVE-WITH-CHANGES; GAI APPROVE-WITH-CHANGES; Gro no valid objection)
- New self-contained tier `execution/swing_breakout_manager.py` (template: quarterly_hold_manager.py): own state file
  data/state/swing_breakout.json (atomic), own client_order_id tag, GTC protective stop per lot, quantity-bounded exits
  (never close_position(symbol)); symbols held by QHM or F6 are skipped.
- Universe: top-10 (breakout) / top-20 (gap, if its review passes) by 60-day median dollar volume among current
  S&P 500 + Nasdaq-100 members, from Alpaca T1 daily bars (closed bars only).
- Entry: once per day on the closed daily bar; order at the next session open; long only; no swing shorts.
- Exit: close below the prior 20-day low (checked ~15:50 ET on the live price; logged vs the official close) or 30 sessions;
  GTC stop at entry - 2.5 x ATR(14).
- Sizing (risk seat, corrected for the unenforced 40% constant): 4 fixed slots x 20% of equity, whole shares; the 2%
  risk-at-stop rule stays as a ceiling; tier budget = min(80%, 100% - QHM - F6 - other swing overnight notional)
  (Architecture Invariant #11); skip + log when no room; bypass the run_cycle overnight size multiplier; keep gross 2.5x
  and the buying-power pre-flight.
- Stop NEW 12-point swing entries (both sides); open positions keep their exits.
- Every entry/skip/exit emits its decision stack to trade_events.jsonl.
- Rule E: risk-path (new entries) -> full gate: cold board + masked-loss seat, cold-2nd, Gro + GAI, Rafael approval.
- Follow-up (separate diff): remove or wire the dead MAX_OVERNIGHT_EXPOSURE_PCT and the stale entry_logic.py:1497 comment.

## As built (2026-09-29, Claude-signed) — supersedes the Build section where they differ
- Ownership: lots carry the swing owner tag "intraday" (allocator lease IN- client_order_id), NOT a new tag. A new
  ledger tier would break tier_capital_allocator._ledger_tier_gross (the four ledger tiers must sum to the broker
  net) and block every tier's entries. This tier's lots are identified by data/state/swing_breakout.json;
  orphan_manager and run_cycle's drift checks exclude get_breakout_symbols().
- Capital: every entry is admitted by live_admit("swing", "intraday", ...) — the same budget the 12-point swing
  path used. With TIER_CAPITAL_SWING_TARGET_PCT=0.20 × gross 1.25 the swing budget is ~25% of equity, so only ONE
  20% slot fits today; slots 2-4 are denied "tier capital cap" (logged skip). Raising that budget is a separate
  allocator decision (targets must sum to 1.0) — Rafael's.
- overnight=False on admit (same as entry_logic's RTH swing path): the allocator's overnight flag caps TOTAL
  account gross (QHM + F6 included) at 40% of equity, and Rafael excluded the buy-and-hold tiers from swing
  exposure limits (2026-09-26). The tier's own overnight bound is its sizing cap min(80%, 100% − other notional).
- Universe: a fixed 30-name megacap pool (SWING_BREAKOUT_POOL / module default), ranked daily to the top 10 by
  60-day median dollar volume. Not the full S&P 500 + Nasdaq-100 (too many daily-bar calls per cycle).
- Entry timing: once per ET day from 10:05 ET (module gate; after run_cycle's 09:30-10:00 opening return and
  QHM's own >= 10:05 entry hook), on the prior closed daily bar; marketable DAY limit at ask + 0.2%, 15 s fill wait,
  remainder cancelled and SETTLED (polled to a final status, so a fill during the cancel is counted). A total
  daily-bar outage un-latches the day so the next cycle retries. QHM's configured picks (incl. pending entries)
  are skipped, not only its held names.
- 12-point: SWING12_NEW_ENTRIES_ENABLED=False blocks NEW entries in execute_entries (after the #12c opposite-signal
  exit and the position checks, mean-reversion exempt) and in _overnight_entry_check. Open 12-point positions
  keep every exit path.
- Protection invariant (after BGGN round 1 — risk seat, execution seat and cold-2nd all found recovery gaps):
  every cycle `_reconcile` drives each held lot to "open with a resting stop for min(lot qty, broker long qty)".
  * A stop cancelled/expired/rejected by ANYONE (a corporate action, QHM's stray-sell sweep, another tier's
    40310000 blanket cancel), never placed, or lost to a restart is re-placed and paged.
  * Re-stops use allow_cancel_blocking=False (never blanket-cancel another tier's orders; no 63 s poll) and
    route the broker sentinels by identity. A failed re-stop flags the lot `unprotected`, pages (throttled to
    30 min) and retries every cycle; if the price is already through the stop, the lot is closed instead.
  * A stop is NEVER placed on shares the broker does not show (no accidental short). Broker flat → the lot's
    resting stop is cancelled first, then the lot is booked as an unverified external close (P&L None).
  * In-flight entries (submitting / submitted / submit_unknown / entry_unverified, incl. after a restart) are
    resolved by order id or client order id: settle → promote the filled qty to "open" + stop, or mark unfilled.
    They count as HELD (slots, notional, orphan/drift exclusion) until resolved.
  * Unreadable orders are never read as zero-filled; an exit whose stop or close fill is unreadable becomes
    `exit_unverified` (never sells blind) and is resolved when readable.
  * Partial fills (stop or close) are booked as partial_exit events with realized P&L, and the final exit's
    realized_pnl sums every leg (None if any leg is unknown). Estimated entry prices are flagged on every event.
  * get_breakout_symbols() returns the last good set on an unreadable state file (never empty-on-error).
  * Near-close exits latch per lot only once decided; a deferred exit retries on the next cycle.
- Per-lot risk cap: SWING_BREAKOUT_MAX_RISK_PCT = 2% (own constant; the paper profile's MAX_PORTFOLIO_RISK_PCT is 4%).
- Disclosed residual risks (not fixed in this diff):
  * No earnings filter: a 30-session hold crosses an earnings date about half the time; a gap can jump the stop.
    Worst case ~ slot 20% × a 25% gap ≈ 5% of equity per lot (bounded; the 7% kill only stops new entries).
  * Trading-thread time: the entry scan (30 daily-bar fetches + ~20 s per entry) and near-close exits run on the
    run_cycle thread; realistically 35-50 s on an entry day with one slot. Main-bot lots keep their exchange stops.
  * The tier's own close (partial_close_position tier "intraday") can, on a 40310000, blanket-cancel orders on the
    symbol, including a co-holding day-tier stop (existing broker behaviour for non-daytrade tiers).
- Kill switch: SWING_BREAKOUT_ENABLED=False stops NEW entries only; held lots keep their GTC stops and managed exits.
- Hook placement (strategy/run_cycle.py): exits run after the QHM weekly check and before the kill/halt return, so
  they run every RTH cycle up to the 16:00 EOD return (the 15:50/15:55 cycles land in the 10-minute window). Not on
  a calendar BLACKOUT day (that return comes first) — the GTC stop still protects the lot. Entries run after the
  QHM entry hook: after the kill/halt return, but BEFORE the SPY "EXTREME" block, like QHM (the swing tier does
  not use the SPY 5-minute gate — Rafael 2026-09-27).

## Budget amendment (2026-10-02, Claude-signed; Rafael approved)
As built, the tier took ZERO entries: its room counted the QHM book (~130% of equity) against Invariant #11's 100%,
while SWING12_NEW_ENTRIES_ENABLED=False removed the legacy swing entries. BGG 4/4 chose a margin-anchored ceiling:
room = min( min(80%, 100% − other swing/day notional [QHM/F6 excluded]) − tier, 1.75 × equity − ALL notional ).
QHM/F6 notional comes from the ownership ledger's protected tiers (`_protected_notional`); an unreadable ledger
counts them in the 100% limb (fail-safe → fewer entries). Invariant #11 amended in CLAUDE.md. Today: room ≈ $841.
Worst case −10% overnight gap at full K: equity ≈ $2,014, margin call needs ≈ 39% gap. The allocator is OFF
(PR #449), so live_admit approves without a lease; this sizing cap is the binding bound.
