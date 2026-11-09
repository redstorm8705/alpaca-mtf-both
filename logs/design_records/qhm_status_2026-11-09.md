# QHM-STATUS-1109 — QHM reads broker order status as plain text, so NVDA's stop can be put back (design record)

Approval-ID: QHM-STATUS-1109
Covers: execution/quarterly_hold_manager.py, tests/test_qhm_status_1109.py
Not changed: main.py, config.py, execution/broker.py

Status: rev 1 for owner approval (signed: Claude, 2026-11-09). Safety fix: a QHM hold left without a stop.

## The problem (live, 2026-11-09)

- At 07:05 PT QHM bought 1 more NVDA share on a dip. Before the buy it cancelled the old 1-share stop (d98c83ad) so
  it could resize it. The new stop was rejected by Alpaca's wash-trade rule because the buy order was still open. The
  buy filled one second later. NVDA is now 2 shares with no stop at the broker (checked at the broker: no open NVDA
  orders).
- QHM retries every 5-minute cycle. Its retry first checks that the cancelled stop is finished ("canceled",
  "filled", ...). It reads the status with `str(order.status).lower()`. alpaca-py returns an `OrderStatus` enum, and
  on the server's Python 3.10 that gives `"orderstatus.canceled"` (probed on OCI, read-only, 11-09 15:17 UTC), which
  never matches `"canceled"`. Log 15:13:30 UTC: "NVDA canceled stop d98c83ad... is not terminal/readable". So the
  retry can never succeed and NVDA stays without a stop until someone fixes it by hand.
- The same raw read sits at six places in the QHM manager: the cancelled-stop check, the dip-add order checks (two),
  the provisional-stop checks (two), and the live-stop confirm (`status in {"new", "accepted"}`), which also never
  matches. Other QHM reads already unwrap the enum (`.value`).

## The fix

- One helper, `QuarterlyHoldManager._order_status(order)`: the status as plain lowercase text, unwrapping the enum's
  `.value` (and anything after a dot), `""` for a missing status.
- The six raw reads use it. No other logic changes: the same status sets, the same order of checks.
- Test: enum and string statuses read as plain text, a partial fill is not terminal, and no raw
  `str(getattr(x, "status"))` read is left in the file.

## Effect on trading

QHM's stop recovery can finish again, so a QHM hold that lost its stop gets one back on the next cycle. No change to
size, entry rate or exposure. NVDA's stop is expected back on the first cycle after deploy.

## Not in this fix (follow-up, own record)

- The same raw read exists in other files (exit_logic, gtc_manager, trade_engine, run_cycle, orphan_manager, broker);
  each needs checking.
- The dip-add itself: place the stop only after the buy is done (the wash-trade rule), and book the added share to
  QHM, not the Day tier (the ledger showed intraday=1 for the QHM buy).

## Required questions

Q1: Yes. It keeps stops on QHM holds, which the safety envelope requires; it does not slow trading.
Q2: No change to the number of trades.
Q3: Yes, it must exist: a hold without a stop that the bot cannot heal. This is the smallest fix (one helper, six reads).
Q4: QHM-REENTRY-1108 / QHM-REENTRY-NEWS-1109 (open branch) touch the same file in other functions; no overlap. No open PR on these lines.
Q5: No numbers added. The helper only normalises text.
Q6: Static assumption exposed: that str() of a broker enum equals its value. Next build: one shared helper for every tier (the follow-up above).
Q7: Feeds QHM stop recovery and stop confirmation only. Fails safe: an unreadable status is "" which is never terminal, so the lock-in-place behaviour is unchanged.
Q8: Probe on OCI: get_order(d98c83ad) returns Order with status OrderStatus.CANCELED; str().lower() = "orderstatus.canceled". Log line 15:13:30 UTC "not terminal/readable". Broker: no open NVDA orders, position 2 shares.
Q9: Expected: NVDA's QHM stop is placed on the first cycle after deploy. Proven wrong if the log still shows "is not terminal/readable" for a cancelled order.
Q10: No. It does not add a limit or a halt; it removes a stuck state that blocked a protective stop.
Q11: Not risk-path: no size, frequency or concurrency change. It restores a protective stop.
Q12: Stops: it restores them. Kill switch, paper=True and loss booking untouched.
Q13: Unreadable status stays "", which keeps the hold pending (same as today).
Q14: No shadow period. It acts live on the next QHM cycle after deploy, like the code it fixes.
Q15: The existing QHM logs show each recovery step and the stop submit line.
Q16: Log "QuarterlyHoldManager: NVDA GTC stop @ ... submitted" after deploy, and an open QH- stop on NVDA at the broker.
Q17: Read-only OCI probe of get_order on the cancelled stop (done) and a broker check of open orders after deploy.
Q18: Yes. Investigated from the live log, the broker and a probe before this fix.
Q19: Yes. It changes live stop handling, so the owner approves the file scope.
Q20: Facts only; no prior verdicts in the review prompts.
Q21: No. Tests use plain objects; the broker is never called.
Q22: Probed on OCI read-only before the change.
Q23: Unit tests on the helper plus the full QHM test suite (311 pass).
Q24: The enum stringify claim was checked on the server's own Python (3.10.12).
Q25: Not applicable: no market statistics.
Q26: Alpaca trading API order status (the existing source).
Q27: No new writes. QHM saves its state file the same way it already does (atomic write).
Q28: Runs inside QHM's existing cycle on the trading thread; no new calls.
Q29: High: a live position without a stop.
Q30: Standard enum handling. Under-weighted risk: the same pattern in other files (follow-up).
Q31: Not a strategy change.
Q32: Not applicable: no backtest or replay result is claimed; the evidence is the live log and a broker probe.
Q33: No cost change: the same stop order the bot already places, no extra trades.
Q34: Not applicable: it changes no return source; it only lets an existing protective stop be placed.
Q35: Any market: it only changes how status text is read.
Q36: All QHM names.
Q37: Not applicable: no statistical sample; one live incident (NVDA 11-09) plus unit tests.
Q38: Not applicable: no strategy review threshold; the reversal check is in Q9.
Q39: Size unchanged: the stop covers the same QHM quantity the existing recovery computes.
Q40: Protects "every QHM hold has a stop at the broker". Machine check: no QHM hold in PENDING_STOP_REPLACE for more than two cycles with a terminal cancelled stop.
Q41: The shared status helper for every tier (follow-up).
Q42: The broker's order record, read directly.
Q43: Unwrap the enum value before comparing: standard practice. Used.
Q44: Cold review, preship (GAI + Codex), and a broker check after deploy.
