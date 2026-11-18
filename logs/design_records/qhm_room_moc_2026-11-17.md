# QHM-ROOM-MOC-1117 — a market-on-close buy no longer blocks every QHM buy for 20 minutes

Approval-ID: QHM-ROOM-MOC-1117
Covers: execution/quarterly_hold_manager.py, run_moc_cushion.py (new), scripts/install_moc_cushion_cron.sh (new), scripts/state_freshness_check.py, server-ops, tests/test_qhm_room_moc_1117.py (new)
Not changed: execution/swing_overnight.py, execution/overnight_capacity.py, execution/broker.py, config.py, data/fetcher.py, run_overnight_k.py
Status: rev 1 (signed: Claude, 2026-11-17). Design only.

## Problem (measured 2026-11-17; issue 940, reported by the 11-10 Gemini audit)
- QHM's overnight-room check (`_overnight_room_dollars`, `execution/quarterly_hold_manager.py:9428-9446`) adds every
  working buy order's notional, priced by its limit or stop price. The trend-night tier's entry is a market-on-close
  order (`IN-QQQM-b-...-tnecls1`), which has neither, so the check raises "working buy without a price" and returns
  None, and QHM blocks every buy until that order fills at the close.
- Log count of "working buy without a price" (each one a blocked QHM buy attempt): 11-09: 6, 11-10: 12, 11-11: 4,
  11-13: 4, 11-16: 3. The order is placed about 20 minutes before the close, so QHM cannot add or re-enter in that
  window on any trend-night day.

## Change
For a working BUY with no limit and no stop price, price its unfilled quantity at the live two-sided ask from the
same quote source QHM already uses for re-entry (`_reentry_ask`), raised by the existing add-path slippage cap (the
same constant `_stop_safe_add` uses), so the estimate is not below what a marketable buy would pay. If no fresh
two-sided ask exists, the check stays fail-closed exactly as today. Orders with a limit or stop price are unchanged.
One log line per order id per day with `[change=qhm-room-moc-1117]` states the price used.

## Rev 2 (2026-11-17) — after GAI's design review (REJECT: an auction fill can land well above the ask, so ask +
slippage cap can under-count). Supersedes the price rule in "Change".
The priceless working buy is priced at the larger of (a) the live two-sided ask raised by the add-path slippage cap and
(b) the ask raised by the symbol's own 99th-percentile UPWARD move from the same time of day to the close, measured on
its 5-minute T1 bars over the last 250 sessions (the percentile and the window are arguments to the estimator; no fixed
cushion). (b) is computed once per symbol per PT day and cached; it is read from `data/fetcher.py` only on the first
need that day, never per cycle. No bars, too few sessions for the estimator, or no fresh ask: fail-closed as today.
Added tests: a fill at the measured 99th-percentile move stays inside the overnight K limb; a fill above it is the
1-in-100 case and is stated, not hidden, in the log line; missing bars or ask blocks.

## Rev 3 (2026-11-17) — from the board seats (Thorp, Taleb; Peterffy, Kim). Supersedes Rev 2's computation timing.
- Line citations corrected: `_overnight_room_dollars` is at quarterly_hold_manager.py:9476-9533, the raise at
  9520-9521 (main a8c71726 + later merges). The add-path slippage cap is `_LIMIT_PRICE_TOLERANCE` (0.1%), so the
  measured move decides the price in practice.
- The move is precomputed pre-market by a new job `run_moc_cushion.py` (same pattern as run_overnight_k.py: cron,
  atomic JSON write to `data/state/moc_cushion.json`, a timestamp). Per symbol with a trend-night or other
  close-auction entry: the distribution of (daily close / price at the order's time of day) - 1 over the prior sessions
  in the cache, using only sessions before today, the endpoint from the DAILY bar's close (the 5-minute feed may miss
  the closing-auction print). QHM reads the file: missing, unreadable, or not stamped today -> fail-closed as today.
  No bar fetch on the QHM cycle.
- The percentile is not a fixed 99: it starts at the highest observed value in the window (the sample maximum) and
  every close-auction fill logs realized-vs-estimate with `[change=qhm-room-moc-1117]`, so the estimate is checked
  against outcomes (Taleb, Thorp).
- Get-to-yes alternative noted for the owner, not in this change: the trend-night tier could place limit-on-close at
  this estimate, bounding the auction price by construction (an entry change; separate design).
- Covers adds `run_moc_cushion.py` (new), its test, and server-ops (one cron line, installed by the same job's install
  script).
- Round-2 conditions (board Peterffy/Kim and Thorp/Taleb; GAI): the window is the bar cache's sessions before today,
  and too few sessions for the estimator -> fail-closed (Rev 2 rule kept); the time of day comes from the trend-night
  placement schedule in execution/swing_overnight.py, not a written constant; `moc_cushion.json` is written with
  tmp + os.replace and carries its PT date, and QHM treats any date other than today as missing; the file is added to
  the existing state-freshness check (scripts/state_freshness_check.py) so a failed job pages at once; the change-tag
  log line carries order id, estimated price and, once known, the actual fill price.

## Required questions
Q1: Lets QHM add or re-enter in the last 20 minutes on trend-night days; more trades, same caps.
Q2: Increases QHM's possible trades slightly (removes a false block); size still bounded by the overnight K limb.
Q3: Should exist: the block is caused by a missing field, not by a lack of room. Simplest form: price it from a quote.
Q4: Touches only the room function; trend-night order placement is unchanged; no open PR touches it.
Q5: No new number: the live ask and the existing slippage-cap constant.
Q6: Forward: the same pricing for any future priceless working order type (e.g. market-on-open).
Q7: Not applicable because no signal changes.
Q8: Cases: the five trend-night days above.
Q9: Replay: the 11-10 order list (MOC buy for 4 QQQM) priced at that minute's ask; room compared with the cap.
Q10: Not applicable because no signal threshold.
Q11: Frequency: QHM may act where it was falsely blocked; size and exposure stay inside the overnight K limb.
Q12: The overnight K limb (Invariant 11) still binds; under-count risk is the gap between the ask (+ slippage cap) and
     the closing price for one order, for at most about 20 minutes.
Q13: No fresh two-sided ask: fail-closed as today.
Q14: Not a shadow: the priced estimate is used live by QHM's room check from the first day.
Q15: One log line per priced order per day.
Q16: Proof: no "working buy without a price" line on the next trend-night day; a priced line instead.
Q17: The next trend-night entry day.
Q18: The anomaly closed: daily 20-minute QHM block (issue 940).
Q19: Exposure path: board seats (Thorp), GAI and Codex review, owner approval.
Q20: Prompts were neutral (bias filter passed); GAI's first-round reject was answered with evidence, not re-rolled.
Q21: Tests mock all broker and quote calls.
Q22: Masked-exposure question: the estimate uses the ask plus the slippage cap, and any missing quote blocks.
Q23: Unit tests plus the 11-10 replay.
Q24: Not applicable because no availability claim.
Q25: The quote is a live T1 Alpaca quote, not a statistic.
Q26: T1 Alpaca quote, the same source as `_reentry_ask`.
Q27: No new state; one log line.
Q28: Inside QHM's existing room check, on the QHM cycle.
Q29: Removes a daily false block.
Q30: Blind spot: order types without a price.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because no market replay beyond the 11-10 pricing check.
Q33: The slippage cap is the existing one.
Q34: Not applicable because no return stream.
Q35: Not applicable because no regime.
Q36: Not applicable because no sizing change.
Q37: Not applicable because no edge claimed.
Q38: Reversal: QHM over-commits overnight because an MOC buy filled well above the estimate.
Q39: Not applicable because it sizes nothing.
Q40: Outcome: zero "working buy without a price" blocks; machine check = the tests.
Q41: Next: the same treatment for any other priceless order type found.
Q42: The broker's open-orders list.
Q43: Exposure practice: price pending orders conservatively at a marketable price, never skip them.
Q44: The next trend-night day's log.
