# Five standing rules from "How to be more ambitious" (Felix Rieseberg) — 2026-10-18 (Claude)

Approval-ID: RULES-1018
Covers: .claude/preship/required_questions.json, weekly_postmortem.py, scripts/belief_tests.py, tests/test_belief_tests.py,
tests/test_weekly_postmortem_rules.py

## Why (owner, 2026-10-17)
The owner asked for the article's 4-5 most useful ideas turned into hard-coded rules. The article's core: give the model an
OUTCOME to own (not a task), work from raw material with expert methods, never let the model grade its own work, and set
specific hard goals. This is process and reporting only — no entry, exit, sizing or routing change.

## The five rules (each enforced by code, not prose)
1. **Own outcomes, not fixes.** Every design record names the outcome it protects and the check that proves it holds
   (e.g. "Day tier has a fresh GEX label every RTH scan"). Enforced: new required question Q40; the existing post-live
   audit (post_live_due.py) evaluates that check after the first session. No new alert or page (owner 2026-10-17: no
   unrequested processes) — results appear in the existing weekly report.
2. **Raw data, counted twice.** The weekly report's realized P&L is computed from raw Alpaca fills (the existing
   canonical FIFO) AND independently from Alpaca account activity cash movement for the same week; if they differ by
   more than the fees/rounding the fills themselves imply, the report prints MISMATCH with both numbers instead of a
   single figure. (Prior work: weekly_postmortem.py already reconciles the tier split to the account total — this adds
   the second, independent source.)
3. **Test one belief a day.** `scripts/belief_tests.py` holds a registry of the bot's built-in assumptions, each as a
   small function evaluated on that day's raw data (first set: GEX stand-down saves money on UNKNOWN days; trading with
   the SPY 5-min trend beats trading against it; the score floor separates winners from losers). One rotates per trading
   day after the close; the result (held / failed, with numbers) is written to logs/belief_tests.jsonl and shown in the
   weekly report. A failed belief becomes a design item for the owner — it never changes trading by itself.
4. **Expert-depth questions.** Four questions join the required list (RQ-4): what would the best expert do beyond the
   request; what is the rawest data available; what is the professional method; what independent evidence proves it.
   Enforced by the existing required-question gate (record_design / owner approval refuse unanswered designs).
5. **Specific weekly goals per tier, set from the bot's own trailing results.** Each tier's goal for the week is to beat
   its own trailing 4-week win rate and average P&L per trade (derived, never a fixed number). The weekly report shows
   goal vs actual; a miss opens an investigation item — never a cut in trading.

## Review (board López de Prado + Majors seats, Gro, GAI — all APPROVE-WITH-CHANGES, aligned) — adopted
1. Rule 2: categorise non-trade cash (dividends, interest, fees, margin charges) before comparing; tolerance derived
   from those categories; a stale or missing source prints SOURCE MISSING, never a single unverified figure; the
   report write becomes atomic (today it uses write_text). Run on the last 4 weeks of real fills read-only first.
2. Rule 3: test only assumptions that drive live decisions today (start: GEX stand-down, SPY trend direction, score
   floor). The daily line records raw inputs only; a belief is called held/failed only from the cumulative tally,
   with a by-day clustered interval and a multiple-testing correction that counts every belief and regime split;
   until then it reads "inconclusive". Beta-adjust against SPY; the GEX stand-down belief is labelled a counterfactual
   estimate. A look-ahead fixture and a shuffled-label false-positive test are in the test suite. Every verdict names
   an owner item.
3. Rule 5: show goal vs trailing with the trade count and interval; a miss opens a DATA audit (postmortem first, no
   code change) only when the week falls outside the trailing range; too few trades prints "insufficient trades".
4. Rule 1: a design names one machine-evaluated outcome check (or "none"), e.g. "two-source P&L reconciles within the
   derived tolerance" — a result check, not a "block is present" check. Rules 1 and 4 are required questions only.
5. Gro's statistics in its review (trades/day, beta, Sharpe) were not from this account's data and were disregarded.

## Required questions
Q1: Moves edge evaluation: every week shows which built-in beliefs hold and whether each tier beat its own trailing results; it costs no trades.
Q2: No change to the number of trades; nothing here can block, delay or size a trade.
Q3: Simplest form: reuse the weekly report and post-live audit already built; no new alert, page, service or cron beyond one after-close script run.
Q4: Prior work: weekly_postmortem.py canonical FIFO + tier reconciliation (2026-10-11), post_live_due.py, required_questions.json RQ-3, gex_daily_audit.py, day_setup_scorecard design 2026-10-17; this extends them, it does not duplicate them.
Q5: No static thresholds: goals derive from each tier's trailing 4 weeks; the P&L mismatch tolerance derives from the fills' own fees and rounding.
Q6: Static assumption exposed: the bot's beliefs (GEX stand-down, trend filter, score floor) were never re-tested; the forward pass is the rotating belief registry.
Q7: Interconnects: belief results feed the design queue for scoring, gates and sizing; outcome checks feed the post-live audit; nothing is siloed.
Q8: Evidence: this week's report needed manual digging to separate the UBER accident and LLY owner sales (session 2026-10-17); the earlier "45% stale GEX" claim was wrong (it counted after-close readings) — exactly the self-graded error rule 2 prevents.
Q9: Front-loaded check: run the belief tests and the two-source P&L on the last 4 weeks of real fills before shipping; expected: beliefs report numbers, the two P&L sources agree to the cent except fees; reversal: a systematic mismatch means one source is wrong and is fixed before publishing.
Q10: N/A — no edge is being held back; this is measurement.
Q11: N/A — no change to size, frequency or exposure; reporting and design-record questions only.
Q12: N/A — the safety envelope is untouched; nothing reaches orders.
Q13: Missing data: a belief test with no data for the day reports "no data" and rotates on; a missing activity source prints SOURCE MISSING rather than a single unverified number.
Q14: Not a shadow: no trading feature is computed-but-unused; these are reports of what live code did.
Q15: Each belief result logs its inputs, numbers and verdict to logs/belief_tests.jsonl.
Q16: Proof it ran: a belief_tests.jsonl line per trading day and the weekly report's two-source P&L block; audited after the first full week.
Q17: Runtime proof: run both on OCI against the real logs and fills read-only before calling it live.
Q18: The 45% stale claim and the LLY/UBER attribution were anomalies explained only after manual work; rule 2 and 3 make them automatic.
Q19: N/A — no entry, exit, sizing or routing change; the owner still approves this design because it changes required questions.
Q20: Review prompts are neutral facts; the claims are checked by the cold and adversarial reviews.
Q21: No broker calls: reads fills and activities through the existing read-only reporting path; tests use fixtures only.
Q22: N/A — not a fail-closed control on trading.
Q23: Full-path run on OCI read-only over the last 4 weeks before calling it ready.
Q24: N/A — no claims about instruments.
Q25: Statistics come from the real fills and bars, never from memory.
Q26: T1 Alpaca fills and account activities via the existing reporting module; no new data source; no fallback that publishes an unverified number.
Q27: logs/belief_tests.jsonl (append, one JSON line per day) and the existing weekly report output; atomic writes as the report already does.
Q28: Belief tests run after the close as a script; the weekly report runs as today; nothing runs on the run_cycle thread.
Q29: Rank: owner put it first in his queue (2026-10-17); it improves every later design's evidence at low cost.
Q30: Whitespace: an outside check on the bot's own numbers (two independent P&L sources) and systematic assumption testing, both standard in a quant shop and missing here.
Q31: Belief tests report per-day results and a running tally with sample size; no single day decides; no variant search.
Q32: Belief tests use only data available at each decision time (logged decision stacks), not later bars.
Q33: Costs: P&L is from actual fills, so spread and slippage are included.
Q34: Belief tests compare against the SPY move of the same window to separate market beta from the belief's effect.
Q35: Results are tagged with the day's VIX and trend regime so a belief can hold in one regime and fail in another.
Q36: N/A — no position sizing or liquidity effect.
Q37: Each belief shows its count and a confidence interval; "held" is only claimed once the interval excludes zero.
Q38: A belief that fails over enough samples becomes an owner design item; the owner decides any trading change.
Q39: N/A — no position sizing is touched; this design only reports results and adds design-record questions.
Q40: Protected outcome: "every weekly report states P&L that reconciles across two sources within the derived tolerance, and each live-decision belief's cumulative verdict"; machine check: the report's reconciliation result is PASS, or it prints MISMATCH/SOURCE MISSING with both numbers.

Q41: An expert quant shop reconciles P&L to an independent source daily and keeps a ledger of every modelling assumption with its out-of-sample record; this design does both on the weekly report.
Q42: Rawest data: Alpaca FILL activities (re-fetched and re-matched independently of the canonical ledger), Alpaca non-trade cash activities, and the logged entry decision stacks in trade_events.jsonl.
Q43: Professional method: two independent computations of the same figure, and hypothesis tests with day-clustered bootstrap intervals and a Holm multiple-testing correction.
Q44: Independent evidence: the second computation is separate code over a separate fetch; the test suite includes a shuffled-label check that the belief tests stay inconclusive on noise.

## Build notes (2026-10-18)
- Rule 2's second count is a separately written FIFO over a fresh fetch of the raw Alpaca FILL activities (the same
  account-activity data, different code and fetch), plus the week's non-trade cash activities (dividends, interest,
  fees) listed by category. The lifetime tie of realized + unrealized to broker equity already runs in the canonical
  figures and still fails the report closed.
- Rule 3 runs inside the weekly report (no new cron or process, per the owner's no-unrequested-processes rule); each
  week it evaluates every live-decision belief on the cumulative history and appends one line per belief to
  logs/belief_tests.jsonl.

- Entry decisions join to lifecycles by symbol + direction + the nearest entry event on the same New York trading day
  (the bot logs the entry event just after the fill confirms); the event carries decision-time fields only.
- Front-loaded replay on OCI, read-only, real fills (2026-10-18): P&L check OK to the cent in the weeks of 2026-10-05
  (+7.36, 19 legs) and 2026-09-21 (+16.31, 34 legs); 81 of 244 lifecycles joined to an entry event (87 entry events
  exist). Beliefs, cumulative: score_floor −2.24% (interval −5.66% to +0.94%), gex_negative −2.40% (−7.69% to
  +3.58%), trend_aligned −5.66% (−11.35% to −0.33%, Holm p 0.099) — all inconclusive after correction. The weeks of
  2026-10-12 and 2026-09-28 fail closed BEFORE these sections because an owner manual sale has tier 'unattributed'
  (LLY, GEV) — the owner's queued item "count owner/accidental trades with an asterisk" fixes that.
- Cold + adversarial review (FAIL, fixed): an entry record could be joined to a trade it was logged after (look-ahead)
  or reused across trades — now a record counts only if logged between the lifecycle's first fill and its exit, same
  symbol, direction and tier, used once; QHM/F6 lifecycles are skipped. The market adjustment is SPY's close-to-close
  move over the hold's trading days (previous close to exit-day close, New York dates), not minute bars. Days group on
  New York dates; a missing SPY series reports "unavailable"; a Sunday rerun does not duplicate belief lines; a tier
  "beats" only when both average P&L per trade and win rate beat its trailing values. Replay after the fix: same
  numbers (all 81 joined records fell inside their trades).
- Recorded deviations: the trend belief uses the logged 12-month time-series momentum sign (tsmom_direction 1/-1),
  because the SPY 5-minute trend at entry is not logged per entry; VIX/trend regime tags on belief results (Q35) are
  not built yet (no per-entry VIX field) — next step; RQ-4 adds FIVE questions (Q40 outcome + Q41-Q44 expert depth);
  non-trade cash is listed (first 100 rows) but does not enter the fills recount, so it does not set its tolerance.
- Tests: 15 (tests/test_belief_tests.py 9, tests/test_weekly_postmortem_rules.py 6), passing on OCI Python 3.10.

## Prior work
- weekly_postmortem.py tier/lifecycle accounting (logs/design_records/weekly_postmortem_tier_split_2026-10-11.md)
- post_live_due.py / record_post_live.py (post-live audit), required_questions.json RQ-3 (owner gates, GATES-1017)
- gex_daily_audit.py (GEX coverage checks)
