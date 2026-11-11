# KILL-PRIOR-CLOSE-1111 — the kill-switch prices a fully sold overnight lot from yesterday's daily bar

Approval-ID: KILL-PRIOR-CLOSE-1111
Covers: execution/risk_manager.py, tests/test_kill_prior_close_1111.py
Not changed: strategy/run_cycle.py, execution/ownership_guard.py, config.py, data/fetcher.py
Status: rev 1, drafted (signed: Claude, 2026-11-11). Owner 2026-11-11: "Yes" (build it; P0).

## Problem (measured 2026-11-11)
- 06:33:32 PT the Swing trend-night lot (1 QQQM, bought 11-10 at the close, $277.90) sold at $283.59. From 06:35 to the
  close every cycle logged "kill-switch evaluation failed (RuntimeError('kill-switch exact tier attribution is
  unresolved'))" and no new entry was taken all session (midday + post-market audits, mtf_bot.log 14:35:11Z onward).
- Cause, reproduced on OCI against the day's real fills (read-only): `_intraday_trading_pnl` returned (0.0, False).
  Today's only fill was ('QQQM', 'sell', 1, 283.59, 'intraday'); QQQM is flat, so the broker sends no position and no
  `lastday_price`; the branch "flat now with non-zero net today qty => held-over lot fully closed" returns (0, False);
  with OWNERSHIP_GUARD_ENFORCE on, check_kill_switch raises and run_cycle blocks entries (exits keep running).
- It recurs every morning the trend-night lot fills and sells.

## Change
In that branch only, read the prior session's close from the T1 SIP daily bars (`data/fetcher.fetch_bars_window`,
adjustment="split", so on a split's ex-date yesterday's close is on today's share basis, matching today's fills; on any
other day it equals the raw close): the last bar dated before today (ET). A non-finite or non-positive close logs a
warning and degrades. Board round 1 (Thorp): raw prices would mask a loss on a reverse-split long or a forward-split
short (e.g. 1:10 reverse split, 10 shares held at $1.00, 1 share sold at $9.00: raw gives +$8, true -$1); split
adjustment fixes it. Board round 1 (risk-asymmetry seat), verified at source: (a) today's fills are keyed by the PT
date (`_today_fills`, `pt_today = datetime.now(_PT)`), so "today" for the prior-close lookup and its cache key is the PT
date too; with an ET date, a check between 21:00 and 24:00 PT would take today's own close as "prior" and report a
-$10 gap-down sale as $0. (b) `fetch_bars_window` retries a rate limit for up to ~61 s on the trading thread; a miss is
remembered per (symbol, PT date) and not re-fetched for one scan interval (config.SCAN_INTERVAL_INTRADAY), so a
failing symbol costs at most one fetch per interval; each miss still degrades (fail closed). Tests: long and short sold-out lots, split day both signs, only-today bar -> None, fetch error ->
None, the cache keyed by ET date, and the bar index convention (ET-midnight stamps). Price the fills from it
exactly as for a held symbol. Cached per (symbol, ET date) once found (one fetch per symbol per day). Any miss or error
returns None and the branch degrades exactly as today (fail closed, never masks). Lookback window = the bot's own daily
lookback (config.BARS_TO_FETCH[TF_DAILY] calendar days).

## Replay (OCI, 2026-11-11 after the close, read-only)
Patched module: prior close QQQM 277.87 (2026-11-10 bar); result (+10.04, True), attribution exact. Unpatched: (0.0,
False). Hand check: QQQM fill term 283.59 - 277.87 = +5.72; the rest is the open leg of the other held names.

## Required questions
Q1: Restores trading on every day the Swing overnight lot sells (data collection; a full day of entries was lost 11-11).
Q2: More trades: it removes a false all-day entry block; no new limit.
Q3: Should exist; simplest form: one lookup in the one branch that lacked a price.
Q4: Inventory: KILL-JOIN-1107 (rebuild path, unchanged), KILL-ATTR-1023 (attribution, unchanged); no open PR touches this branch.
Q5: No new decision number; the lookback reuses config.BARS_TO_FETCH[TF_DAILY].
Q6: Static assumption exposed: "a sold-out symbol has no prior close". Forward pass: a startup self-test that replays the kill measure on the morning's first fills.
Q7: Feeds the kill switch only; the dashboard gauge reads the same exact snapshot.
Q8: Measured: mtf_bot.log 2026-11-11 14:35:11Z onward; replay output above.
Q9: Replay on 11-11 data: expected (+10.04, True); wrong if a day with a sold overnight lot still logs the RuntimeError.
Q10: Not applicable because this is a measurement repair, not a signal.
Q11: No size, frequency or exposure multiplier; the kill limit (7%) is unchanged.
Q12: The 7% kill switch stays fully armed and now measures every symbol; never-mask holds (a missing bar still blocks).
Q13: No daily bar or any error -> None -> the existing fail-closed entry block.
Q14: No: live from the first cycle.
Q15: Each lookup logs the close used and its bar date.
Q16: Proof line: "kill-switch prior close for <SYM> (fully sold held-over lot)" with no "exact tier attribution is unresolved" error after it.
Q17: The first session after deploy in which the trend-night lot sells.
Q18: Investigated from the log and the real fills; not explained away.
Q19: Not entry/exit/sizing logic; it repairs the kill measurement that gates entries.
Q20: Reviewer prompts carry the code, the measured facts and the replay only; no verdict is suggested to any reviewer.
Q21: Read-only data call (T1 bars); tests mock it.
Q22: Fails closed exactly as before on any miss.
Q23: Real replay before ready (above).
Q24: The SIP daily close can differ from the official closing auction by cents; the error is bounded by qty times that difference.
Q25: Prior close measured from the bar, not assumed.
Q26: T1 (data/fetcher.py), the approved bar source.
Q27: No state file; an in-process cache only.
Q28: Runs on the trading thread inside the kill check, only for a fully sold held-over symbol, at most one fetch per symbol per day.
Q29: P0: 11-11 lost every entry from 06:35 PT to the close.
Q30: Blind spot: a fail-closed branch that fires on a routine event (the morning Swing sale) and stays shut all day.
Q31: One form tried; the alternative (persisting lastday_price at the open) needs a new state file.
Q32: Not applicable because nothing is fitted.
Q33: Not applicable because no trades are simulated; cost is one bar fetch.
Q34: Not applicable because the change adds no market exposure.
Q35: Not applicable because the change adds no market exposure.
Q36: Not applicable because no orders are placed.
Q37: Not applicable because no statistic is estimated.
Q38: Reversal: a measured kill P&L that differs from the fills-based P&L by more than the bar-vs-auction gap.
Q39: Not applicable because it sizes nothing.
Q40: Protects "the 7% kill switch never masks a loss": a short or long sold-out lot is priced against its prior close; tests cover both signs and the miss.
Q41: Log each prior-close lookup to the kill snapshot.
Q42: Real fills and T1 bars.
Q43: Price every leg from a measured prior close; fail closed when none.
Q44: The 11-11 replay plus the first live session with a morning Swing sale.
