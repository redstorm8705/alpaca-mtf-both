# WTP-SLACK-1108 — weekly post-mortem Slack post, phone-first layout (design record)

Approval-ID: WTP-SLACK-1108
Covers: weekly_postmortem.py, tests/test_weekly_postmortem_slack.py (new), logs/post_live/expect/wtp-slack-1108.json (new)
Not changed: reporting/report_figures.py, tier_names.py, scripts/belief_tests.py

Status: rev 1 for owner approval (signed: Claude, 2026-11-08).

Owner request (2026-11-07), on the re-run post for the week of 11-02: "I don't understand what this means and the UX of
the post mortem needs to be audited. All the info is there and correct, it just looks terrible."

## What is wrong today (the 11-07 post, read from `_post_slack` in weekly_postmortem.py)

- The post opens with jargon: "Realized cash (Alpaca-FIFO)", "completed W/L", "Fri-positive diagnostics".
- Tier results sit on one line of code-formatted numbers with no win/loss signal.
- All 27 trades follow as a monospace table, worst to best. On a phone this table wraps into unreadable soup.
- Nothing tells the owner what to look at.

## The change (Slack message only)

The numbers, the report file and the calculations are unchanged. Only `_post_slack` builds a different message, from
the same `stats` and trade rows it already receives:

1. **Headline:** "📊 Week of Nov 2–6: +$101.98 ✅" (🔻 when negative). Then one plain line on the P&L check, for
   example "Checked against Alpaca twice; both counts match." When the two counts disagree, it says so in words.
2. **One line per tier:** ✅ (made money), 🔻 (lost) or ➖ (no trades), then the dollars, then "won N of M". Day,
   Swing, QHM and F6, then a "✋ Your trades" line only when the owner's manual bucket is non-empty.
   - The tier's 4-week comparison (`_tier_goals`) is added in words only when it says "beat" or "missed", for example
     "best per-trade result in 4 weeks".
3. **Best 3 and worst 3 completed trades:** symbol, long/short, tier (if not Day), dollars. These are the same
   lifecycle rows the table holds today, sorted.
4. **One "👀 Watch" line:** built by rule from that week's own rows, never by the AI:
   - (a) the tier with the largest loss and the symbol behind most of it;
   - (b) otherwise the largest single loss.
   - It states facts only (what happened), never a cause.
5. **Footer:** "Full table and analysis: logs/wtp_<date>.md". The 16-column table, the Gemini analysis, the belief
   tests and the Friday diagnostics stay in the report file, unchanged.
6. **Warnings stay first:** an incomplete-fills warning, an UNAVAILABLE run or a P&L-check failure still leads the
   message, in plain words.

## Evidence

The mock below uses the real 11-02 week from `logs/wtp_2026-11-06.md` on OCI. Its tier totals sum to the
canonical +$101.98: 156.74 − 49.11 − 63.92 + 0 + 58.27 = 101.98. Win counts per tier come from the rows: Day 14/19,
Swing 1/3, QHM 0/3, 15/25 in total.

    📊 Week of Nov 2–6: +$101.98 ✅
    Checked against Alpaca twice; both counts match.
    ✅ Day +$156.74 · won 14 of 19 · best per-trade result in 4 weeks
    🔻 Swing −$49.11 · won 1 of 3
    🔻 QHM −$63.92 · won 0 of 3
    ➖ F6 no trades
    ✋ Your GE trades +$58.27 (yours, not the bot's)
    🏆 Best: MU short +$41.13 · NVDA short +$32.62 · SNDK short +$24.34
    💸 Worst: XOM (Swing) −$34.98 · NVDA (QHM) −$33.97 · NVDA (QHM) −$23.40
    👀 Watch: QHM lost $63.92; NVDA was $57.37 of it.
    Full table and analysis: logs/wtp_2026-11-06.md

## Required questions

Q1: Indirectly. A report the owner can read in ten seconds lets him act on edge evidence each week. It changes no trading.
Q2: No change to the number of trades: this is the Slack message of a weekly report.
Q3: Yes: the owner asked for it. The simpler option, only trimming the table, still leaves jargon. A rebuilt message from the same stats is the smallest complete fix.
Q4: Builds on weekly_postmortem.py `_post_slack`, `_manual_summary`, `_tier_goals` and the canonical report figures (PR 904 manual bucket). No open PR touches this.
Q5: No new decision numbers. Best and worst count 3: a display count, not a trading threshold. The ✅/🔻 sign comes from each tier's own P&L; the 4-week words come from the existing `_tier_goals` result.
Q6: It exposes that a fixed text layout serves every week alike. Next, the watch line could rank findings by the belief tests once they turn conclusive (forward pass, separate design).
Q7: Not siloed. It reads the canonical figures, tier goals and the manual bucket. It feeds nothing back into trading by design, so it has no fail-safe coupling to add.
Q8: The problem is the owner's own report on the 11-07 post. The mock numbers were read from logs/wtp_2026-11-06.md on OCI on 11-08 and reconcile to +101.98.
Q9: To be tested before ship on the real weeks of 10-12, 10-26 and 11-02 from OCI logs (one with an UNAVAILABLE stub, one with a negative total); the 11-02 mock above is already reconciled by hand. Expected effect: the same facts in about 12 short lines instead of about 40. Wrong if any number differs from the report file.
Q10: No validated edge is involved: this changes how the weekly report is shown, not any signal, so there is nothing benched over calibration.
Q11: No size, frequency or exposure change.
Q12: No effect on the safety envelope. It reads P&L and never changes it; a loss is always shown with 🔻 and its sign.
Q13: A missing tier goal drops only that phrase. A missing figure prints "unknown", never 0. A fills-fetch failure keeps today's loud warning first. A Slack failure is logged and never raises (existing `_slack_raw`).
Q14: No shadow: the new message replaces the old one when it ships.
Q15: It makes no trading decision. The message is built only from the logged report figures, and the report file keeps the full table.
Q16: The log line "change=wtp-slack-1108 Slack layout v2 posted" at the next Saturday 09:00 ET run. The post-live audit compares the posted numbers with logs/wtp_<date>.md.
Q17: The test renders the message from the real 11-02 stats. A one-off TEST post to Slack goes to the owner before the first scheduled run.
Q18: No anomaly is being explained away: the owner's complaint is taken as the finding, and the old layout is replaced rather than defended as by design.
Q19: No change to entry, exit, sizing or routing.
Q20: Review prompts are neutral facts. The claim is limited to the layout: the numbers are unchanged and verified against the file.
Q21: Tests build the message from fixed stats and mock `_slack_raw`. No broker or Alpaca call.
Q22: Not a fail-closed control.
Q23: Before ship, a full-path test runs main() with mocked fills for the 11-02 week and checks the posted text.
Q24: No existence claim is made; the only facts used (the report file and its figures) were read from OCI this turn.
Q25: No market statistics are used; every number in the message is the week's own realized P&L from the report figures.
Q26: Uses the existing canonical figures (T1 Alpaca fills and orders). No new data source.
Q27: Writes nothing new. It posts one Slack message; the report file write is unchanged (atomic).
Q28: The last step of weekly_postmortem.main() (Saturday cron, its own process), after the report file is written. Never on the trading thread.
Q29: The owner asked for it directly, and it is small (one function). It ranks after the shipped kill-switch fix and alongside the Swing ratchet build.
Q30: A standard fund weekly letter leads with the result, attribution and top contributors, then the detail on request. This layout follows that.
Q31: Not a strategy or a fitted model, so there is no walk-forward test or variant count; the layout is checked against the report file, not against returns.
Q32: No replay of prices is involved, so look-ahead and survivorship cannot arise; the message reads the closed week's booked fills only.
Q33: Costs are already inside the realized P&L from Alpaca fills; the message adds no estimate of its own.
Q34: Not a return source, so there is no alpha/beta split to make; it reports what the tiers made.
Q35: N/A. A losing week shows 🔻 in the headline and every losing tier, tested.
Q36: It has no capacity or liquidity limit: a longer week only adds rows to the report file, while the message keeps 3 best and 3 worst.
Q37: No edge is claimed, so no sample size applies; the belief tests that do measure edge stay in the report file unchanged.
Q38: No edge to decay; if the owner finds the layout unhelpful, the old message builder is one revert away.
Q39: N/A. The P&L shown comes from Alpaca fills (canonical ledger), unchanged.
Q40: It protects the owner reading the week correctly. Check: the test asserts every dollar figure in the message equals the report figures, and the tier lines sum to the headline.
Q41: Keep the full detail one tap away and say plainly when the P&L check fails. Both are in the design.
Q42: Canonical fills-based report figures, the same source the report file uses.
Q43: A summary-first performance letter: result, attribution, top and bottom contributors, one watch item.
Q44: A cold review of the diff, GAI plus Codex pre-ship, and the owner's own read of the TEST post.

## Rev 1.1 — review round folded (2026-11-08)

GAI and Gro (reviewer #2; Codex was at its usage limit) both returned APPROVE-WITH-CHANGES. Folded:

1. **Totals check:** the tier lines plus Manual must equal the headline to the cent. The test asserts this. At run
   time a mismatch adds "⚠️ Tier lines don't add up to the total; see the full report" right under the headline.
2. **No hidden loss:** every negative number shows a minus sign and 🔻. A tier whose P&L is missing shows "? unknown"
   with no ✅ or 🔻, never a green check.
3. **Watch line only when something stands out:**
   - When any tier lost money, it names the losing tier with the largest loss and the symbol behind most of it.
   - When every tier made money, it says "👀 Nothing stood out: every tier made money."
   - No fixed threshold is used.
4. **Report failure stays loud:** an UNAVAILABLE or incomplete run keeps today's first-line warning, in plain
   words ("This week's report could not be built: <reason>"), and never posts a partial message.
5. **Goal line (GAI):** "Account $X · goal $25K", read from Alpaca's account endpoint (T1, already used by the bot).
   If the read fails, the line is left out, never shown as 0.

Not taken:
- Max drawdown, volatility, sector attribution and YTD were left out of the Slack post. They need new figures and
  would lengthen the phone view the owner asked to shorten.
- They are logged as candidates for the report file instead.
