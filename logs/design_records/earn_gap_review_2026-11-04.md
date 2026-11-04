# Earnings gap-down review — EARN-GAP-REVIEW-1104 (signed: Claude, 2026-11-04) — DESIGN + REPLAY, not approved for build

Approval-ID: EARN-GAP-REVIEW-1104
Covers: config.py, execution/gap_exit.py (new), execution/earnings_gap_review.py (new), data/earnings_documents.py (new), execution/tier_transfers.py, execution/quarterly_hold_manager.py, execution/swing_breakout_manager.py, tests/test_earnings_gap_review.py (new)

Owner direction 2026-11-04 (verbatim): "Before selling after earnings, the bot needs to review the transcript of the
earnings and similar to QHM holds, decide 1) if it's a dip buy candidate and should be promoted from swing to QHM and 2)
if not, where the dead cat bounce level target is to update the stop and sell target. No trades should happen on earnings
gap downs until these two things are confirmed and verified." Earlier the same day: "No I want to at least wait for the
dead cat bounce. AAPL is now at 353 since Monday which is exactly my point."

This REPLACES the earnings branch of GAP-EXIT-1029 (rev 3.2 §3c item 5: "earnings or unknown → exit at the open").
Everything else in GAP-EXIT-1029 (non-earnings wait branch, budget floor, residual-only exit sequence, confirmed
coverage, tick clamp) is unchanged and is reused here.

## 1. The case
AAPL reported 2026-11-02 after the close (Finnhub `amc`, FMP 2026-11-02): EPS $2.05 vs $1.99 estimate, revenue $114.48B
vs $113.40B (FMP /stable/earnings, read 2026-11-04). It opened 11-03 at $325.58 against a $338.32 close and a $328.70
stop; the stop converted to market and sold at $327.34 (06:33 PT). 11-03 high $334.48, close $331.70; 11-04 open $341.88,
$353.92 at 07:30 PT (Alpaca daily bars, IEX). A beat that gapped down and recovered past the prior close in two sessions.

## 2. Replay (logs/research/gap_exit/; panel `gx_events_nd.pkl` = 122 FMP-tagged earnings gaps with 5 following sessions)
Earnings gap = event day is the report date or the next session (timing unknown → both); FMP-402 names are untagged
(a blind spot: their earnings gaps sit in the non-earnings set). Discovery 2020-23, test 2024-26.

**A. Waiting for a bounce without a review loses (54 variants, `sim7_earn` … `sim11_earn`).** Sell at the first bounce,
half at the bounce, hold 1/3/5 days, tighter/looser floors, opening-range-low exits, time caps, small-gap only. Best
on discovery, then test once: −51 to −69 bps vs the open, date-clustered 95% CI fully below zero (e.g. [−120, −12]).
The deterministic target/stop alone (`sim11_earn`, target at the 25/50/75th pct of earlier gaps' 5-day best bounce,
stop at the 90th pct of the drawdown before the high): disc −4 / −33 / −46, test −49 / −60 / −27.

**B. How far the bounce goes (`bounce_levels.py`).** 5-session best bounce, as a fraction of the gap: median 0.45
(25th 0.18, 75th 1.12); half the gap is regained within 5 sessions in 48% of cases, all of it in 30%. Drawdown before
the day-1 high: median 0.37 ATR, 90th pct 0.94 ATR. Typical earnings gap 6.1%.

**C. The report itself separates them (`sim12_beat`, one pre-specified split, not a grid).** BEAT = EPS and revenue both
above estimate (FMP), else MISS. Hold 5 sessions vs selling at the open:
| | Disc mean | Test mean | All mean (median) | All, 95% CI date-clustered | P(mean > 0) |
|---|---|---|---|---|---|
| BEAT (n 22 / 45) | +67.7 | +135.7 | +113.4 (+17.6) | [−28, +271] | 0.93 |
| MISS (n 31 / 24) | −65.1 | −125.9 | −91.6 (−227.5) | | |
BEAT minus MISS, all: CI [−37, +449]. Same sign in both periods; not yet significant at 95%. The bounce-target rule on
BEAT gaps loses (−40): selling at the bounce caps the recovery that pays for the losers — beat gaps should be HELD
(QHM), not sold at the bounce. The transcript read's own value is NOT measured: historical transcripts read by an LLM
today carry look-ahead (the model may know what happened next), so only live events are a clean test.

## 3. Policy
**Trigger:** a held long lot (any tier) whose company reported since the prior close (Finnhub date, FMP fallback, or an
EDGAR 8-K Item 2.02 filed since the prior close), and whose pre-market price is at least 1 ATR below the prior close or
below the lot's stop.

**Step 0 — no sale before the review.** Before 09:30 the lot's stop is cancelled and replaced (read back) by the
GAP-EXIT budget-floor backstop (rev 3.2 §3e items 1-4). In the first cycle after the open it is raised by PATCH to
max(O − F·ATR, budget floor), O = the first regular-session print, ATR = the 14-session ATR known before the day, F as in
path 2 (§8b.9). The backstop, then that raised stop, are the only automatic exits until the review is confirmed: the stop is never removed, and the 7% kill switch is untouched. No marketable-limit sale, no bounce sale,
no add, until Step 1 is confirmed and verified.

**Step 1 — the review (runs pre-market as soon as the documents exist; retried every cycle until confirmed).**
- *Facts, verified at source:* EPS and revenue actual vs estimate (FMP /stable/earnings, Finnhub fallback); the
  press release (EDGAR 8-K Exhibit 99.1: CIK matches the ticker, filed after the prior close, period matches the
  report); the call transcript (Alpha Vantage EARNINGS_CALL_TRANSCRIPT for that quarter; if not yet published, the
  review waits for it — the press release alone does not confirm Step 1).
- *Read:* GAI and reviewer #2 (Gro on the server) get the same prompt with the full documents and return JSON:
  thesis intact (yes/no), guidance (raised / held / cut / none), the three reasons, each with a VERBATIM quote.
- *Verified:* every quote is found verbatim in the fetched text; both models return a parsable verdict; both agree on
  "thesis intact". Anything else = not confirmed.

**Step 2 — the decision.**
1. *QHM dip-buy (promote Swing → QHM):* BEAT (both) AND both models say thesis intact with guidance not cut AND quotes
   verified AND the QHM caps (40% aggregate / 20% per name, enforced code caps) have room. The lot is transferred to
   QHM by `tier_transfers.record_transfer` only after the QHM stop handover is confirmed (§8b.4), at the SIP last trade captured immediately before `record_transfer` (Swing books its realized leg there and QHM's basis starts there, §8b.1; the first print O is only the bounce-formula anchor, never a retroactive transfer price), keeps QHM's stop and dip-zone rules, and is held
   to the day before the next report like any QHM hold. QHM cap room short for the whole reconciled residual → nothing moves; the entire residual takes path 2. Partial promotion and split tier ownership are prohibited.
2. *Bounce path (everything else):* sell target = O + x·(C − O) and protective stop = max(O − F·ATR, budget floor),
   horizon 5 sessions (sell the rest at the 5th close). x = walk-forward quantile of earlier earnings gaps' 5-session
   best bounce — the 50th pct (today ≈ 0.45) when BEAT but the read did not confirm thesis intact, the 25th pct
   (≈ 0.18) when MISS or guidance cut; F = walk-forward 90th pct of the drawdown before the day-1 high (today ≈ 0.94).
   Exits use the GAP-EXIT residual-only sequence (reconcile, resize the stop, marketable limit for the sold shares).
   Measured cost of this path vs selling at the open: about −50 to −60 bps on MISS gaps. This is the owner's
   decision ("at least wait for the dead cat bounce"), stated here so it is not mistaken for an edge.

**Facts rule:** BEAT requires FMP and Finnhub (when both return the report) to agree that EPS and revenue both beat;
facts that are missing, stale (not for this report), conflicting between sources or revised to a miss → MISS treatment.
**Not confirmed by the close of the gap day** (documents missing, models split, quotes fail; Q-RQ1, board Harris rule,
the owner confirms it with the approval): a MISS takes path 2 at the close (25th pct target); a BEAT keeps holding on
its raised stop (no promotion, no add) and the review retries every cycle for up to 5 sessions, then takes path 2 with
the 50th pct target. The owner is paged at the gap-day close for every unconfirmed lot.

## 4. Safety envelope
Stop on every lot at all times (backstop, then path stops or QHM stops); the backstop sits inside the 7% kill switch
budget; never-mask-a-loss: the decision stack logs the loss against the original stop and the SIP open; paper=True.
LLM output never sizes a position and never places an order; it can only choose between two pre-defined paths, and
only when its quotes verify.

## 5. Logging
`earnings_gap_review` events in trade_events.jsonl: documents fetched (source, URL/accession, timestamps, hashes), the
facts, each model's raw JSON, quote-verification results, the verdict, x/F with their sample sizes, the path, every
order id, and P&L against the open, the original stop and the prior close.

## 6. Build steps and checks before "ready"
1. Data adapters (EDGAR, Alpha Vantage, FMP/Finnhub facts) with mocked tests and a read-only server run on the 11-02
   AAPL report. 2. Review harness with quote verification; dry run on AAPL 11-02, NOW 10-28, GOOGL 10-28 (expected: AAPL
   BEAT/intact). 3. QHM transfer path with the caps, a co-held Swing+QHM case, and a short-cap case asserting zero shares transferred, the full residual on path 2, one owner and one confirmed stop covering the full residual. 4. Full-path
   simulation: pre-open backstop → review → each path → restart mid-review. 5. Validation that only live events can
   provide: every review's verdict and outcome is logged; the post-live audit compares BEAT-promoted lots vs the open.

## 7. Static numbers (flagged)
1 ATR trigger (the panel's event definition), 5-session horizon (data length used), 25/50/90 quantile choices
(estimator inputs), QHM caps (existing enforced code caps), close-of-gap-day deadline (owner question).

## 8. Rev 2 (2026-11-04) — changes from board seats Taleb and Harris and Codex round 1 (8 points); all folded
GAI returned HTTP 503 this round.

### 8a. Evidence, corrected
- **One event per report** (Codex 6): the panel keeps the first gapped session per report (121 of 122 events); FMP
  actuals are read today and may have been revised since publication (a point-in-time limit, stated).
- **5-session BEAT arm, full picture** (Taleb E2/E3, Codex 7): discovery mean +67.7 but median −49.3, 59% worse than the
  open, P(mean>0) 0.68, CI [−175, +346]; test mean +135.7, median +74.8, CI [−34, +333]; all: CI by date [−28, +271],
  by ticker [−30, +309]. INCONCLUSIVE. The BEAT split was chosen after the test years had already been looked at (AAPL
  prompted it); only live events are a clean confirmation. The "about 150 events needed" line is withdrawn.
- **The treatment actually proposed** (Codex 5, Taleb E1; `sim13_qhm.py`, `sim14_beta.py`): BEAT lots held to the
  session before the next report (median 61 sessions):
| BEAT, hold to next report | Raw mean | Minus SPY over the same days: mean / median | 95% CI (minus SPY) |
|---|---|---|---|
| No stop | +526 bps | +161 / −59 | [−244, +605] |
| Stop 3 ATR below the open | +372 | +146 / −306 | [−219, +545] |
| MISS, no stop (control) | +142 | −10 / −129 | [−523, +515] |
  Most of the raw quarter-long gain is the market's own rise. After SPY is removed, BEAT vs MISS is about +170 bps, not
  significant. Tight stops (0.94 ATR) cut the median badly (84% worse than the open): a promoted lot needs QHM's wide
  stops, not the bounce-path stop. QHM's real dip-zone stop was not replayed (proxies above); flagged.
- **What carries the decision:** the earnings read the owner asked for. Its value cannot be measured on past events
  (an LLM reading old transcripts knows the outcome), so every live review logs the facts-only decision beside it
  (Codex 8) and the post-live audit compares both against the SIP open and SPY.

### 8b. Policy changes
1. **Book the loss at promotion** (Taleb 1). A Swing → QHM promotion books a realized Swing leg at the transfer price
   against Swing's FIFO basis (into Swing P&L, R and Kelly, and the decision stack); QHM opens at the transfer price with
   its own stop. `tier_transfers._valid` (today only daytrade↔intraday) and `ownership_guard._apply_tier_transfer`
   (today moves lots with no realized leg) are extended for this. Test: 5 AAPL, basis $338.32, review confirms at 10:15
   with SIP last $331.00 → Swing realized −$36.60 at $331.00, QHM basis $331.00, ledger time 10:15; plus a restart between
   the stop handover and the ledger write recovers idempotently with no double attribution.
2. **QHM ownership path for a name QHM does not already hold** (Harris 1): the promotion creates a QHM hold record (today
   QHM skips symbols missing from quarterly_holds_config) and QHM manages its stop. Test that QHM manages that lot.
3. **All-or-nothing promotion** (Codex 4, Harris 3): if QHM's caps cannot take the whole lot, nothing moves; the lot takes
   path 2. One tier owns the symbol's lot at any time — no split ownership.
4. **Stop handover** (Harris 2, Taleb 4): during the session the existing stop is PATCHed (status `new`) to QHM's price and
   QHM's client_order_id, read back, then the transfer is recorded with the reconciled residual. PATCH failure → the lot
   stays Swing, retried next cycle. Invariant asserted every cycle: a confirmed resting stop covers every share.
5. **Reconcile first** (Harris 7): every decision starts by reading fills, holdings and the stop's state; a backstop that
   filled during the review → nothing is sent or transferred, logged as stop-filled-before-decision.
6. **Already below the budget floor before the open** (Codex 2): holding would breach the 7% budget, so the kill-switch
   envelope overrides the review — exit at the first regular print by marketable limit, logged as budget override.
7. **Decision after a level was crossed** (Codex 3, Harris 4): at the decision cycle, last trade ≥ target → sell the
   residual now (marketable limit, collar from the live quote); last trade ≤ the path stop → sell now; a stop is never
   placed through the market.
8. **Pre-open coverage** (Codex 1): a resting stop cannot trigger before 09:30, and Alpaca refuses PATCH on an `accepted`
   order, so the pre-open move is cancel + new + read-back with GAP-EXIT §3e item 4's fallback (no confirmed stop at the
   last pre-open cycle → resubmit at the backstop level, which is below the market; page). This is the same protocol
   Codex closed on GAP-EXIT-1029 round 2.
9. **First-day stop** (Harris 6, Codex round 2): the post-open raise to max(O − F·ATR, budget floor) applies before the review confirms; section 3 Step 0 is the single normative statement.
10. **Unconfirmed by the close** (Harris 5; now the normative rule in section 3): MISS takes the bounce path at the close;
    an unconfirmed BEAT keeps holding on its backstop (no promotion, no add) and the review retries each cycle up to 5
    sessions, then takes the bounce path.
11. **Document timing** (Harris 8): EDGAR's acceptance time for AAPL's 11-02 Item 2.02 8-K was 2026-11-02 16:30 ET
    (data.sec.gov submissions, read 2026-11-04); a BMO report's call is usually after 09:30 ET so its transcript cannot
    exist at the open [hypothesis — the Alpha Vantage lag is unmeasured until the owner's key exists]. Build step 1
    measures both lags over the last 4 quarters of the held names.
12. **Quote check** (Harris 9): whitespace, curly quotes and dashes are normalized before the verbatim match; words are
    never fuzzy-matched. Transcript and news text is data in the prompt, never instructions (Taleb N1).
13. **Facts-only counterfactual** (Codex 8): logged on every live review.
14. **Idempotent transfer** (GAI round 5): the transfer `ref` is the review id; `record_transfer` is already idempotent
    by `ref` under its file lock. On restart, an existing ledger entry for that ref wins: its price is the Swing exit and
    the QHM basis, never a fresh SIP read.
15. **Stop filled during the review** (GAI round 5): the review ends with status STOP_FILLED as soon as reconciliation
    shows the backstop or raised stop filled; nothing further is sent, transferred or promoted for that lot.
16. **Review status:** Codex APPROVE (round 5, after four rounds of changes); GAI round 5 changes 14-15 adopted (its confirmation of them is pending: Gemini quota, HTTP 429); board
    seats Taleb and Harris changes adopted (section 8b).

## Required questions (RQ-4)
Q1: Edge evaluation: replaces a measured −40 to −65 bps exit with a review-gated hold whose BEAT arm measured +113 bps; data collection: every earnings gap produces a logged review and outcome.
Q2: Trade count unchanged; a gapped lot is held longer or moved to QHM instead of sold at the open.
Q3: Simpler: facts-only (BEAT/MISS) without the transcript; kept as the fallback. The transcript read adds the guidance check the owner asked for.
Q4: GAP-EXIT-1029 (rev 3.2, owner-approved; this replaces its earnings branch), qhm_thesis_routine_2026-09-05 (LLM read, transcripts unavailable on FMP), qhm_dynamic_dip_zones_2026-10-15, earnings_source_finnhub_2026-10-29, tier_transfers.py; no open PR on this.
Q5: x and F are walk-forward quantiles; static items listed in section 7.
Q6: Static assumption: one earnings response for every report. Forward pass: score the review's verdicts against outcomes each quarter and refit which read features (guidance, beat size, gap size) predict recovery.
Q7: Feeds QHM entry (dip-buy promotion), Swing exits, the kill-switch budget; consumes the earnings date sources and the GAP-EXIT backstop; can later feed QHM's weekly thesis memo with real transcripts.
Q8: Every number in section 2 comes from the named replay files; the transcript's added value is labelled unmeasured.
Q9: 121 one-per-report earnings gaps 2020-26 across 26 names; BEAT hold to next report +161 bps vs SPY (inconclusive), MISS bounce path about −50 bps vs the open; reversal: reviewed lots averaging below SPY-adjusted open-exit after 15 live events, measured to their actual exit.
Q10: No; the BEAT effect is not yet significant and the transcript value is unmeasured, stated as such.
Q11: Yes, risk-path: holds a gapped lot longer (exposure held) and moves lots into QHM; board + masked-loss seat required.
Q12: Stop never removed (backstop, then path or QHM stop); bounded by the 7% budget; never-mask-a-loss logging; paper=True.
Q13: Missing documents or split models → no confirmation → the section 3 close rule; missing, stale, conflicting or revised facts → MISS treatment; tests cover each.
Q14: No shadow: every verdict acts live.
Q15: Yes, section 5 lists the decision stack.
Q16: `earnings_gap_review` events and the transfer/order ids; post-live audit on the first earnings gap after deploy.
Q17: First live earnings gap, audited from the event log, the broker orders and the tier ledger.
Q18: AAPL 11-03 re-examined: the prior design called it "by design"; this redesign is the outcome.
Q19: Yes — owner approves this exact design before any commit.
Q20: Prompts neutral; all losing variants reported; the AAPL earlier misclassification corrected in GAP-EXIT §3f.
Q21: All broker, EDGAR, Alpha Vantage, FMP and model calls mocked in tests; broker refuses under test.
Q22: Data adapters and the review run read-only on the server against the AAPL 11-02 report before going live.
Q23: Full-path simulation in section 6 must pass first.
Q24: Transcript availability on FMP checked (premium-blocked per qhm_thesis.py); Alpha Vantage free transcript is from third-party pages, to be verified with a live call.
Q25: Bounce and drawdown statistics measured from SIP bars (section 2B).
Q26: T1 Alpaca bars and quotes; T2 FMP / Finnhub facts; EDGAR and Alpha Vantage are new free document sources (owner to approve as tiers); fallback = facts-only.
Q27: trade_events.jsonl, the tier state files, the tier transfer ledger; atomic writes as those modules already do.
Q28: Pre-market in each tier's own cycle (Swing on the run_cycle thread: one non-blocking step per cycle; document fetch and model calls run in a worker with a deadline, never blocking check_exits).
Q29: High: every held name reports quarterly; one gap costs 2-6% of a lot; it is the owner's direct request.
Q30: Event-driven earnings handling (guidance vs consensus) is standard; the bot lacked any post-report read.
Q31: Walk-forward; 54 bounce variants tried and reported; the BEAT split was chosen after the test years had been seen; inconclusive (section 8a).
Q32: Earnings tag misses FMP-402 names; LLM transcript reads of past events carry look-ahead, so they are not used as evidence.
Q33: Same-slippage comparisons; paper market-at-open fills were 0.55-4% below the SIP open (n = 3).
Q34: Measured vs SPY (section 8a): most of the raw quarter-long BEAT gain is market beta; +161 bps left, not significant; sector residuals at build.
Q35: Worst BEAT hold-5 p05 −513 bps of a lot; bounded by the backstop and the 7% budget.
Q36: 1-20 share lots in liquid large caps; capacity not binding at $25K.
Q37: 67 BEAT events; SPY-adjusted CI [−244, +605]; inconclusive; live events (with the facts-only counterfactual logged) are the evidence.
Q38: Reversal criterion in Q9; owner sets the zero-trade definition.
Q39: No sizing change; QHM-promoted lots follow QHM's existing Kelly and caps from Alpaca fills.
Q40: Protects exit value on earnings gaps; check = post-live audit of each review's path vs the SIP open and the prior close.
Q41: Add guidance-vs-consensus numbers (FMP estimates) to the facts and score the read quarterly.
Q42: SIP bars, the company's own filing, the call transcript, broker fills.
Q43: Post-earnings announcement handling (beat vs miss, guidance), event-driven; the design follows it.
Q44: Live outcomes vs the SIP open in the post-live audit; board, GAI and reviewer #2.

## Owner questions
Q-RQ1 (written into section 3 as the rule; the owner confirms or picks the alternative): if the review cannot be confirmed by the close of the gap day: recommended (board Harris) — a MISS takes the bounce path; an unconfirmed BEAT keeps holding on its backstop and retries for up to 5 sessions. Alternative: every unconfirmed lot takes the bounce path at the close.
Q-RQ2: Approve SEC EDGAR and Alpha Vantage as document sources. EDGAR requires a User-Agent with a contact address. Alpha Vantage needs a free key, which the owner creates.
