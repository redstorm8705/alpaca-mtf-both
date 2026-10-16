# Extended-hours stop-watch + earnings-pop add — design record (Claude, 2026-10-16)

**Owner decision (2026-10-16):** "I just want our stops honored whenever the market is available to execute.
Approved." Scope confirmed in chat: pre-market (4:00-9:30 ET) and after-hours (16:00-20:00 ET) per the Alpaca
calendar; overnight session (20:00-04:00) OFF until verified for paper; QHM + Swing; earnings-pop add at the next open.

## Evidence (replays, 1 year, real Alpaca SIP 5-min bars incl. extended hours, split-adjusted)
- Pre-market / after-hours dip-ADDS (10 names): after-hours triggers bought median 0.97% worse than the next open
  (-0.34% 20-session median); pre-market matched the open (+0.05%). Board Harris/Thorp + Gro + GAI 4/4: no
  after-hours dip-buys; then 3-1 against pre-market dip-adds (D) in the earnings fork. -> pre-market dip-add code is
  SHELVED (WIP commit `4f0deb2`, not for merge).
- Earnings-sized after-hours moves (each name's 4 biggest after-hours moves/yr, 36 names): drops >= 5% opened
  LOWER the next morning 18/18 (median 2.46%); pops >= 5% kept rising (+0.42% to open, +1.93% to next close).
- Live-faithful exit replay (`xh_exit.py`): trigger = after-hours price seen with the 15-min SIP delay at
  15-min checks below the close by more than the stock's own 2.5th-pct 1-session return; sell limit = seen price
  x (1 - own median 5-min after-hours bar range). 36 triggers / 19 names / 33 filled. vs holding to the next open:
  median +1.22% better (61%); vs next close +0.13%; vs 20 sessions +0.05% median, mean -1.28% (holding often
  recovers). => value is ONLY where the stop would fire at the open anyway -> trigger = price <= the existing stop.
- Board/Gro/GAI on the earnings fork: A (after-hours drop exit) 4/4; B add at the next OPEN 3-1 (Taleb: small
  after-hours add with own stop); C (buy AH drop / sell AH pop) rejected 4/4.

## Build spec (not yet built — next session)
1. `broker.extended_session_now(now=None)` — in branch `claude/qhm-ext-stop-watch` (from the WIP; calendar
   session_open/open/close/session_close; None on failure). Tests exist in WIP `tests/test_qhm_extended_hours.py`.
2. `dip_zones`: store `median_move` + `typical_move_for()` — in the same branch (from the WIP).
3. `QuarterlyHoldManager.run_extended_stop_watch()` (both sessions): per ACTIVE long hold with stop_price > 0 and no
   recovery flags: price = freshest of IEX `data.live_price.live_price(sym, max_age_s=DELAYED_FEED_AGE_S)` and the
   newest SIP 1-min bar (`fetch_bars_window(feed="sip")`, end = now - SIP delay, age <= DELAYED_FEED_AGE_S +
   _BAR_WIDTH_S). If price <= stop_price -> `_execute_extended_stop_exit` mirroring `_execute_owner_trim`:
   sole-holder check (broker qty == QHM qty) -> cancel QH stop (fail -> abort, stop intact) -> held re-read ->
   extended_hours=True limit SELL all at round(price x (1 - typical_move), 2) (typical_move None -> no sale, keep
   stop) -> poll `_OWNER_TRIM_FILL_POLL_TIMEOUT_SECONDS` -> cancel remainder + confirm terminal -> ledger
   `apply_authorized_tier_fill_reduction(sym, "qhm", filled, broker_net, f"qhm_ext_stop:{order.id}")` -> remaining
   >= 1: `_earnings_trim_restore_stop(remaining, stop_price)`; 0: state CLOSED + `_deregister_symbol` -> trade_event
   exit reason `extended_hours_stop` + plain-English Slack. Unfilled -> stop restored, retried next cycle.
4. Call sites: `run_cycle` closed-market branch AND the 8:45-9:30 premarket branch (it returns before the closed
   branch). NOT kill-switch gated (it is an exit).
5. Cadence: `main.py` sleeps 30 min outside 08:45-16:00 -> sleep 15 min while `extended_session_now()` is True
   (the replay assumed 15-min checks).
6. Swing: legacy 12-pt tracker positions already have `_check_exits_extended_hours` (but read IEX prices only —
   sparse in extended hours: NVDA 5 IEX vs 60,000 SIP trades 04:00-08:45 on 10/15) -> switch it to the same
   freshest-price helper. Swing breakout lots (`swing_breakout_manager`) need the same stop-watch (2nd increment).
7. Earnings-pop add at the open (QHM): after-hours move above the stock's own top tail -> add at the next regular
   open inside every QHM cap; risk-path (size) -> board gate on the diff.

**Size/frequency/concurrency:** stop-watch = exit only (reduces exposure); pop-add = size up (risk-path).
**Reversal criterion:** stop-watch fills worse than that day's open on average over the first 10 triggers -> off.
