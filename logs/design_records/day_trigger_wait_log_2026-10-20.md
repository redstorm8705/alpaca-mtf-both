# Log every Day-tier entry-trigger WAIT with its numbers — 2026-10-20 (Claude)

Approval-ID: TRIGGER-LOG-1020
Covers: strategy/day_tier_entry_trigger.py, tests/test_day_tier_entry_trigger.py, logs/design_records/day_trigger_wait_log_2026-10-20.md, logs/post_live/expect/trigger-wait-log-1020.json

## Why (owner, 2026-10-20)
On 10/20 the Day tier took no trades by 9:10 AM PT. Ten names qualified (533 would_consider decisions), but the log
only explained META/DRAM/EWY (RIDE "not a fresh cross"). AMZN, GOOGL, TSLA, NVDA and AAPL (FADE) left no line, so the
reason could only be found by replaying the trigger by hand ("FADE: no failed wall-sweep yet"). The owner asked for
the fix ("Build it"). Rule D (decision explainability) says every skip must be reverse-engineerable from the logs.

## Facts (strategy/day_tier_entry_trigger.py, full read 305 lines, 2026-10-20)
- `compute_entry_trigger` logs INFO only on ENTER (L299) and on a stale RIDE cross (L279).
- These WAIT returns are silent: no actionable GEX levels (L227), insufficient bars (L235), no usable close (L239),
  FADE no failed sweep (L258), RIDE no close-through (L271), pattern present but volume not confirmed (L295).
- The runner (run_day_tier.py L1091-1093) calls it once per would_consider name per 2-minute tick and `continue`s on
  WAIT without logging.

## Change (logging only)
1. Each of the six silent WAIT returns above sets its reason through one helper that also logs
   `[SYM] day-tier ENTRY-TRIGGER: <reason>` at INFO, the same format as the existing RIDE line.
2. The FADE / RIDE / volume reasons carry the numbers the decision used: the latest close, the sweep-window high and
   low, the call and put walls, and (volume case) whether the volume check passed. Example:
   `FADE: no failed wall-sweep yet (close 373.99, 3-bar hi 374.79 / lo 373.85; call_wall 385.0, put_wall 360.0) — wait`.
3. The "not would_consider" and "mode not tradeable" returns stay unlogged here: the DECISION line before them
   already records that outcome every tick.
4. No threshold, comparison, direction, target, size, or return value changes. Only `result["reason"]` text gains
   the numbers, and a log line is written.

Volume: at most one line per qualifying name per 2-minute tick (10/20 had 533 qualifying decisions by 9:10 AM PT,
about the same count the existing RIDE line already writes for META).

## Required questions
Q1: It serves edge evaluation: every Day-tier skip becomes explainable from the logs, so non-trading days can be diagnosed.
Q2: No change to the number of trades; only reason text and a log line are added, no trigger logic changes.
Q3: Yes. Simplest form: log inside the existing returns through one helper; no new module or data read.
Q4: Extends the existing RIDE stale-cross log (L279) and the Track-B wait log in run_day_tier.py; no conflict found in open PRs (#725 touches other files).
Q5: No new number: the logged values are the trigger's own measured inputs (close, window high/low, walls, volume check).
Q6: Static assumption exposed: the fixed sweep margin and 3-bar window; logging their inputs gives the distribution needed to derive them later.
Q7: The log feeds post-trade replay and the weekly report review; it changes no scoring, sizing or exit path.
Q8: Measured: 533 would_consider decisions on 10/20 by 09:10 PT and zero FADE trigger lines; a live replay showed "FADE: no failed wall-sweep yet" for AMZN, GOOGL, TSLA, NVDA, AAPL.
Q9: Replay: the 10/20 trigger inputs reproduced by hand; expected effect is one reason line per qualifying name per tick; wrong if any trading decision changes.
Q10: No trading edge is being held back; this is observability for an existing gate.
Q11: Not risk-path: size, frequency and exposure are unchanged; only reason text and logging change.
Q12: The safety envelope is untouched: stops, the 7% kill switch, paper=True and the data tiers are not involved.
Q13: Logging sits inside the existing try/except, so a formatting failure still returns WAIT exactly as today.
Q14: Not a shadow: it logs why the live code skipped, which the no-shadow rule explicitly allows.
Q15: Yes; this change exists to put each skip's decision inputs in the log.
Q16: Proof line: "day-tier ENTRY-TRIGGER: FADE: no failed wall-sweep yet (close" in logs/day_tier_runner_cron.log on the first session.
Q17: Proven at runtime by the proof line appearing from the live runner process during regular trading hours.
Q18: The anomaly (no explained reason for most skips) is being fixed, not explained away.
Q19: Not an entry, exit, sizing or routing change; the owner asked for it and approves this exact design.
Q20: Facts only, from the full read and the 10/20 log counts; reviewers get the exact diff.
Q21: No broker call is added; tests inject bars and levels and never reach the broker.
Q22: Not a new control: nothing blocks or gates trading, so no fail-closed probe applies.
Q23: Unit tests cover each logged WAIT branch; the first live session's lines confirm the full path.
Q24: The record makes no claim that any symbol or instrument does not exist or cannot trade.
Q25: The record quotes no stock or market statistics beyond the logged 10/20 values.
Q26: No new data read; the trigger's existing T1 IEX bars and GEX snapshot are reused.
Q27: Writes only log lines to the existing runner log; no state file, so no atomic write is needed.
Q28: Inside compute_entry_trigger, called by the Day-tier runner (run_day_tier.py L1091) each 2-minute tick; nothing else changes.
Q29: High value per minute of work: it removes the blind spot behind the owner's no-trades question today.
Q30: A professional execution stack logs every rejected signal with its inputs; this closes that gap for Track A.
Q31: No edge claim, so out-of-sample testing does not apply to a logging change.
Q32: No replay result drives a decision here, so look-ahead and survivorship cannot apply.
Q33: No trading cost is involved; the change places no orders.
Q34: The change adds no market exposure and no return to decompose.
Q35: Behaviour is identical in every regime; only the reason text and log line are added.
Q36: Unaffected by account size or liquidity; it logs inputs only.
Q37: No edge statistic is involved, so statistical power does not apply here.
Q38: No signal or edge exists here that could decay; the logged inputs help measure decay of the trigger later.
Q39: Position sizing and the Kelly inputs are unchanged by this logging.
Q40: Outcome protected: every qualifying-but-skipped Day-tier name has a logged reason; check: on the first session each would_consider name has a matching ENTRY-TRIGGER line per tick.
Q41: An expert would also aggregate these reasons into a daily "why no trade" summary; noted as a follow-up.
Q42: It logs the rawest inputs available to the trigger: bar close, high, low, volume check and the GEX walls.
Q43: The professional method is a structured rejection log per signal evaluation; the design follows it.
Q44: Independent evidence: the first live session's log lines, matched against a hand replay of the same ticks.
