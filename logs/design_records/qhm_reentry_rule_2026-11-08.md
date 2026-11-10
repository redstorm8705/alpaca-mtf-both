# QHM-REENTRY-1108 — QHM does not buy back a sold hold unless the price has earned it (design draft)

Approval-ID: QHM-REENTRY-1108
Covers: execution/quarterly_hold_manager.py, tests/test_qhm_reentry_1108.py (new)
Not changed: main.py, config.py, execution/dip_zones.py, events/catalyst_engine.py, run_dip_zones.py
Owner choices: Release=Rung A|Rung B; Review=10 locked cases
Status: rev 2. Design review: GAI, Codex, board Thorp and board Taleb all APPROVE-WITH-CHANGES and all chose Rung A; their changes are folded in below. Owner approval package next (signed: Claude, 2026-11-08).

## Owner direction (2026-11-08)

"This is about a stock being bought for QHM, profit being taken manually or otherwise, and the bot then re-entering
the same trade at a higher price." "The only way we should be re-entering before earnings is if the stock is very
oversold and or sees a steep, unjustified decline in price with fundamentals remaining strong."

## The gap (verified in code, 2026-11-08)

- At every bot restart, `main.py:838-849` re-adds every pick whose `not_before_date` has passed. `add_candidate`
  (quarterly_hold_manager.py:1230) refuses only owner sales and (GE-REBUY-1108) unexplained closes.
- Every other way a hold ends leaves `closed_reason` None or `qhm_stop_out`, so the hold is re-added on the next
  restart and bought at market. That covers the stop filling (line 1904), a pending exit filling (line 2062), and the
  trailing profit-lock stop, which counts as a stop-out.
- The re-buy passes the same Day-1 gate as a new pick: the 30-min close must be above 0.85 × the prior close
  (`_passes_entry_gate`, line 2576), so it almost always passes. Nothing compares the price with the sale price or
  with the next earnings date.

## How often it happened (Alpaca orders since 2026-04-01, read-only from OCI, 2026-11-08)

A QHM buy was counted when the whole account was flat in the name first. Sale sources: QHM = the bot's own QHM
order, app = the owner, bot = another tier (Day/Swing). There were 12 re-buys, 7 of them above the last sale price:

| Symbol | Sold | Sold by | Re-bought | Change |
|---|---|---|---|---|
| GE | 09-15 @ 307.24 | QHM | 09-29 @ 320.83 | +4.4% |
| GE | 11-04 @ 330.50 | app | 11-06 @ 322.06 | -2.6% (owner also sold 2 @ ~314 on 11-02) |
| GEV | 08-18 @ 1042.01 | QHM | 08-19 @ 967.00 | -7.2% |
| GOOGL | 08-20 @ 338.61 | QHM | 08-21 @ 342.22 | +1.1% |
| GOOGL | 09-24 @ 339.33 | bot | 09-29 @ 339.97 | +0.2% |
| GOOGL | 10-30 @ 278.72 | bot | 11-02 @ 283.18 | +1.6% |
| LLY | 08-21 @ 1231.00 | QHM | 08-24 @ 1244.86 | +1.1% |
| NVDA | 08-17 @ 227.89 | bot | 08-18 @ 219.11 | -3.9% |
| NVDA | 08-28 @ 218.73 | QHM | 08-31 @ 219.20 | +0.2% |
| NVDA | 09-29 @ 230.30 | bot | 09-29 @ 229.62 | -0.3% |
| NVDA | 10-13 sell 10-08 @ 234.07 | QHM | 10-13 @ 226.36 | -3.3% |
| NVDA | 11-05 @ 186.46 | bot | 11-06 @ 191.85 | +2.9% |

Caveat: "bot" sales belong to other tiers trading the same name, so those rows are not QHM exits. The replay must
key on QHM's own exits and the owner's sales.

## Proposed rule (to be tested, not yet decided)

After ANY QHM exit (own stop, profit-lock stop, exit order, earnings trim to zero, owner sale, unexplained close), the
pick goes into a re-entry lock that remembers the exit price and date. Until the stock's next earnings report has
passed, QHM re-buys only when at least one holds:
1. **Very oversold, data-derived:** price at or below that stock's own Rung B dip zone
   (`execution/dip_zones.py`, refreshed daily by `run_dip_zones.py`, already used by DIP-ADD), measured from the exit
   price. This reuses an existing per-stock signal (anti-silo); no new fixed threshold.
2. **Steep drop, with fundamentals intact:** a fall from the exit price larger than the stock's own historical
   N-day drawdown quantile, with no classified negative catalyst for the name (`events/catalyst_engine.py`) and no
   earnings guide-down. A drop with a negative catalyst counts as justified, so no re-buy. The quantile and N come from
   the replay, chosen on discovery years only.

After earnings pass, the lock clears and the normal pick flow applies. The owner can still release a pick by
re-dating its `not_before_date`.


## Replay result (read-only, OCI, 2026-11-08; signed: Claude)

**Method:**
- **Cases:** 7. Each is a sale by QHM or the owner that left the account flat, followed by a QHM re-buy.
- **Bars:** Alpaca SIP daily bars, split-adjusted (T1).
- **Zones:** `execution/dip_zones.compute_zones`, with data up to each sale date and the live horizon of 2 sessions.
- **Earnings dates:** FMP. GEV and LLY were blocked by FMP (402), so the Finnhub fallback supplied them.
- **Lock re-buy:** the next session's open after a dip trigger, or the first open after the earnings date.
- **Scoring:** both policies buy the same share count (1 share in every case). A case still locked at 2026-11-06 is
  scored as the actual re-buy's open P&L at that close.
- **Spot-check:** LLY was re-priced independently from raw MCP bars. Its close fell 6.0% below the $1,231 sale on
  08-31, so the lock re-buys at the 09-01 open, $1,188.00. This matches the replay.

| Case | Actual re-buy | Lock + Rung A (shallow dip) | Lock + Rung B (deep dip) | Lock until earnings only |
|---|---|---|---|---|
| GE sold 09-15 @ 307.24 | 09-29 @ 320.83 | 10-21 @ 302.48 (+18.35) | same (+18.35) | same (+18.35) |
| GE sold 11-04 @ 330.50 (owner) | 11-06 @ 322.06 | still locked (-1.18) | (-1.18) | (-1.18) |
| GEV sold 08-18 @ 1042.01 | 08-19 @ 967.00 | 08-21 @ 974.73 (-7.73) | 08-25 @ 960.01 (+6.99) | 10-29 @ 860.00 (+107.00) |
| GOOGL sold 08-20 @ 338.61 | 08-21 @ 342.22 | 10-26 @ 320.89 (+21.33) | 10-29 @ 286.88 (+55.35) | 10-29 @ 286.88 (+55.35) |
| LLY sold 08-21 @ 1231.00 | 08-24 @ 1244.86 | 09-01 @ 1188.00 (+56.86) | 10-30 @ 1428.83 (-183.97) | 10-30 @ 1428.83 (-183.97) |
| NVDA sold 08-28 @ 218.73 | 08-31 @ 219.20 | 11-02 @ 207.53 (+11.67) | 11-06 @ 191.10 (+28.10) | locked (+32.56) |
| NVDA sold 10-08 @ 234.07 | 10-13 @ 226.36 | 10-15 @ 215.30 (+11.06) | 10-26 @ 213.10 (+13.26) | locked (+39.72) |
| **Total, 1 share each** | | **+$110.36, better in 5 of 7** | **-$63.11, 5 of 7** | **+$67.83, 5 of 7** |

**Reading:**
- Every lock variant beats today's behaviour in 5 of the 7 cases.
- Rung A has the best total and no large miss. Its worst case is GEV at -$7.73.
- Rung B and lock-only lose most of their gain on one case. LLY never fell 9.5% and ran +15% through earnings, so
  waiting for a deep dip missed that move.

**Honest limits:**
- 7 cases and 3 variants, all tried on the same data. There was no discovery/test split, because the sample is too
  small to divide.
- The ranking is therefore not statistically established. Confirmation has to come from data never consulted: the
  live sessions after it ships.
- The fundamentals leg (no negative catalyst) was not replayed. Historical catalyst classifications are not stored.
- Replay buys 1 share at the open, while live QHM buys in tranches toward a target equity percentage.

**Recommended variant (pending review):** lock until the next earnings report. Inside the lock, re-buy only when the
close is at or below the stock's own Rung A dip zone measured from the exit price, and no classified negative catalyst
is open. Rung B ("very oversold" in the owner's words) tested worse, because it can miss a strong name that never
dips deeply. That trade-off is the owner's call.

## Still owed before this goes to the owner

- Replay: every QHM exit and owner sale since April, the no-lock path vs the lock path. Measure money and days held.
  Choose the variant on discovery data only, and report how many variants were tried.
- Board (Thorp, Asness, Taleb) + GAI + Codex on this design.
- The 44 required questions.
- A size / frequency / concurrency delta: frequency goes down only, so it is expected to be non-risk-path; reviewers
  must confirm.
- An interaction check with DIP-ADD and the tranche schedule: after a partial trim, does a tranche top-up re-buy
  higher? Trace it in the replay.

## Rev 1 — changes from the GAI + Codex design review (2026-11-08)

1. **Scope: full exits only, stated precisely.** A lock starts whenever QHM's holding of a name goes to zero, by any
   route: its own stop, the profit-lock stop, an exit order, an earnings trim to zero, an owner sale, or an
   unexplained close. Partial trims are not locked. Code trace (quarterly_hold_manager.py) shows that after a partial
   trim nothing re-buys higher:
   - tranches 2-3 run only during the first 5 days of a new hold (`_TRANCHE_DAYS = [1, 3, 5]`, lines 1088-1096);
   - DIP-ADD (line 2642) buys only below the hold's cost average.
   Other tiers' sales (Day/Swing) never start a lock. Exits are attributed by QHM's own state transition, not by
   account flatness.
2. **The trigger is a price.** `trigger = exit_price × (1 + rung_a)`, where `rung_a` is the stock's Rung A (a negative
   2-session return) on the day of the check. Re-entry needs a completed SIP daily close at or below the trigger.
3. **Pre-order ceiling (Codex failure 1).** Immediately before the first tranche order, the live ask must still be at
   or below the trigger. A gap above it keeps the lock. The order is a limit at the trigger, never a market order.
4. **Fundamentals leg, fail-closed (owner's words).** Re-entry needs the catalyst state for the name to be CLEAR.
   NEGATIVE or UNKNOWN (no read, stale, error) keeps the lock.
   - GAI recommended striking this leg because historical catalysts are not stored. Codex recommended keeping it with
     UNKNOWN treated as locked.
   - The owner's rule requires "fundamentals remaining strong", so it stays, fail-closed. This can only block
     re-entries, never add them.
5. **Missing data keeps the lock.** A missing or stale dip zone, earnings date, exit price or exit date means lock-only
   (no re-entry until earnings pass). This covers the GAI RQ-13 nit. Earnings date order: FMP, then Finnhub (as in
   the replay), then the pick's config date. With none of them, the lock stays and the owner is paged once.
6. **"Earnings passed"** means the first regular session that opens after the report:
   - for a before-the-open (bmo) report, the open of the report day itself;
   - for an after-the-close (amc) report or an unknown time, the next session.
   After that the lock clears and the normal pick flow applies. GAI's "earnings-day gap-down" scenario is therefore
   the normal post-earnings flow, which is outside the owner's "before earnings" rule. The ordinary entry gates still
   apply then.
7. **Persistence and order of checks.**
   - Storage: the lock (exit price, exit date, exit route, earnings date) is stored atomically on the CLOSED hold in
     quarterly_holds.json.
   - Where it is checked: in `add_candidate` before anything else, and again right before the first tranche order, so
     a restart cannot skip it.
   - Logging: each decision logs `QHM reentry <SYM>: locked|released reason=... trigger=... close=... ask=...`.
8. **Owner release:** re-dating the pick's `not_before_date` past the exit date still releases it (the existing
   mechanism), and the release is logged.
9. **Variants tried:** exactly the three in the table (Rung A, Rung B, lock-only). No other settings were tried.

Size / frequency / concurrency:
- **Size:** unchanged.
- **Entry rate:** can only go down or be delayed. No catch-up or accelerated tranches are allowed after a release.
- **Concurrency:** caps unchanged.
- **Risk path:** Codex says non-risk-path as a pure veto. GAI says risk-path because it touches the entry gate.
  Default-to-risk-path on ambiguity applies, so the board includes the masked-loss seat.
10. **Catalyst read (verified, events/catalyst_engine.py):** the state comes from `read_cached_state()`.
    - **UNKNOWN:** `ok=False`, meaning the cache is missing, malformed, or older than its stale limit.
    - **NEGATIVE:** any active catalyst for the name with `polarity == "negative"`, of ANY type. This covers
      guidance_cut, downgrade, dilution, legal, solvency, leadership and recall. It is stricter than the entry gate's
      four blocking types, because a re-entry release needs fundamentals affirmatively intact.
    - **CLEAR:** otherwise.
    - The entry gate never blocks on a cache fault. Here a fault only keeps the default lock: missing data never
      creates a trade, in line with never-mask.

## Rev 2 — board changes (Thorp + Taleb, cold seats, 2026-11-08)

11. **Owner sales are not loosened (Taleb, a concrete failing input).**
    - **Today:** an owner sale (`owner_app_sale`, `owner_confirmed`) or an unexplained close (`external_close_detected`)
      blocks a re-add indefinitely, until the owner re-dates `not_before_date` (quarterly_hold_manager.py:1245-1264).
    - **The defect in rev 1:** letting the Rung A release apply to those holds would let a dip buy back a stock the
      owner chose to sell. Example: the owner sells GE at $330.50, GE dips to the trigger, and QHM re-buys it. That is
      MORE buying than today.
    - **The fix:** those exits keep the existing re-date-only release. The Rung A / earnings release applies only to
      QHM's own exits: its stop, its profit-lock stop, its exit order, and an earnings trim to zero. With this change
      the design is a pure veto: it can only remove or delay buys relative to current code.
12. **The trigger is pinned at exit time (Thorp).** `trigger = exit_price × (1 + rung_a at the exit date)`. It is stored
    with the lock and does not drift as the zones refresh daily. If no zone exists at exit, the hold is lock-only.
13. **Pre-order check uses a fresh live quote (Taleb).** A missing, zero, one-sided or stale quote keeps the lock.
14. **Catalyst lag (Thorp).** The release also requires the catalyst cache timestamp to be later than the close that
    crossed the trigger. A cache refreshed before the drop is treated as UNKNOWN, so the lock stays.
15. **The lock is written atomically with the close (Taleb).** It is written in the same `_save_state` as the CLOSED
    transition, on every QHM close path (lines 1904, 2062, 2155, 5716). If that save fails, the hold must not end up
    CLOSED-and-unlocked: an unlocked CLOSED hold from a QHM close path is read as locked (lock-only). A lock never
    touches `closed_reason` or the booked P&L.
16. **No catch-up after release (Thorp, Taleb).** A released hold starts the ordinary tranche schedule from tranche 1.
    There are no accelerated or caught-up tranches. A test asserts this.
17. **Structured event.** Every lock and release evaluation also writes a `qhm_reentry` event to `trade_events.jsonl`
    with: exit price, trigger, close, ask, zone date, catalyst state and cache age, earnings date and source, and the
    decision.
18. **Reversal criterion (Thorp, Taleb; owner to confirm).** Each locked case is scored at its earnings release
    against the counterfactual immediate re-buy. If the cumulative score over the first 10 locked cases is negative,
    the rule comes back to the owner for removal or redesign. [The number 10 is the owner's choice, not derived;
    alternatives in the package.]
19. **Open positions are never touched.** The lock gates only `add_candidate` and the first tranche order. It never
    affects stops, exits or `check_exits`.

**Risk-path ruling:** non-risk-path. Taleb and Thorp both trace a pure veto: size unchanged, entry rate down or
delayed, caps unchanged, given changes 11 and 16. Codex agrees. GAI said risk-path because it touches the entry gate;
the board's trace is that nothing in it can raise size, rate or exposure. The board and masked-loss seat reviewed it
regardless.

## Required questions

Q1: Yes. It stops QHM buying back a stock it just sold, which undoes its own exit and muddies QHM's exit record. It is justified on growth: in the replay the buy-backs lost money in 5 of 7 cases.
Q2: Fewer or later QHM re-buys only, after QHM's own full exits. There were 12 re-buys since August, 7 above the sale. The owner asked for this on 11-08 ("I don't think we should've re entered the trade"); this package is the approval.
Q3: Yes, it should exist. The simplest form tried, lock-until-earnings, tested worse (+$68, with a -$184 LLY miss). Rung A reuses an existing signal, so no new machinery is built for the release.
Q4: It builds on GE-REBUY-1108 (staged; blocks re-adds after unexplained closes), the owner-sale auto-heal (10-17), and DIP-ADD's zones (PR #658). No conflict: owner-sale holds keep their current rule (change 11).
Q5: Nothing static in the release. The trigger is each stock's own rolling Rung A, and the lock end is its earnings date. The only number is the review count of 10, which is an owner choice and flagged as one.
Q6: Static assumption exposed: a pick that was just sold is as good a buy as a new pick. Next build: regime-conditioned dip zones, and scoring every exit plus re-entry at a common horizon.
Q7: It uses dip_zones (release), the catalyst engine (fundamentals) and the earnings calendar (lock end). Each fails to the lock, and each is tested on its own.
Q8: The defect is measured from Alpaca orders (12 re-buys, 7 higher), the code trace (main.py:838-849, add_candidate:1230) and the 7-case replay. The size of the benefit is a hypothesis.
Q9: 7 cases from Aug to Nov 2026, covering rising and falling names. Expected: re-entry below the actual re-buy in about 5 of 7. It is wrong if the cumulative locked-case score is negative at the review count (change 18).
Q10: No. Rung A ships without waiting for a better catalyst model.
Q11: Size unchanged, entry rate only down or delayed, caps unchanged. Board trace (Thorp, Taleb) and Codex: non-risk-path. GAI said risk-path; the masked-loss seat reviewed it either way.
Q12: No. Stops, the 7% kill switch, paper=True and loss booking are untouched. A re-entry gets the normal QHM stop.
Q13: A missing or stale zone, quote, catalyst cache, earnings date or exit record keeps the lock (changes 5, 13, 14). A failed save is read as locked (change 15).
Q14: No shadow. The lock acts live from the first QHM close after deploy.
Q15: Yes. Every evaluation writes a structured qhm_reentry event with its full inputs to trade_events.jsonl (change 17).
Q16: The log line "QHM reentry <SYM>: locked|released reason=..." plus the qhm_reentry event. The post-live audit replays the first session's QHM closes and re-adds against Alpaca orders.
Q17: A read-only run on OCI against a copy of the live quarterly_holds.json, and the restart path: a locked case must create no order.
Q18: No. The "by design" restart re-add was re-examined against actual fills, which is how this was found.
Q19: Yes. It changes QHM entry routing, so the owner approves this exact design before any commit.
Q20: The prompts gave facts and the replay table. Codex flagged two framing phrases, now scoped (changes 11 and 16). The board, GAI and Codex all reviewed it.
Q21: No. Every broker call in the new tests is mocked, and the broker refuses under test.
Q22: Not yet. It runs read-only on OCI against the real holds file before it is called live.
Q23: Not yet. A full-path test (close, restart, re-add refused, trigger close, quote check, limit order) is part of the build.
Q24: There are no absence claims. Earnings dates were checked at source (FMP 402 for GEV and LLY, Finnhub 200).
Q25: Yes. Zones come from a year of Alpaca SIP bars, and replay moves from Alpaca daily bars.
Q26: Bars and quotes are T1 Alpaca. Earnings: T2 FMP, then Finnhub (an approved API), then the pick's config date. Catalysts: Alpaca news via the existing cache. Every fallback ends in the lock.
Q27: Lock fields go on the CLOSED hold in data/state/quarterly_holds.json via the existing atomic _save_state. Events go to logs/trade_events.jsonl.
Q28: add_candidate (the startup re-add, main.py:838) and the first-tranche order path in QHM's entry cycle. Nothing on the check_exits path.
Q29: High. It is a measured leak in a live tier, small to build, and the owner asked for it today.
Q30: A standard framework has a re-entry ledger scored at a common horizon. Under-weighted risks: earnings-date revisions and corporate actions between exit and re-entry. The earnings date is re-read on every check.
Q31: No walk-forward. 3 variants on 7 cases, so significance is not established. It ships as a correctness gate at unchanged size, with the review criterion.
Q32: Zones used data up to the exit date only, so nothing after the decision. There is selection bias: only cases with an actual re-buy were replayed. The symbols are today's QHM names.
Q33: Not modelled. The replay buys one share at the open. A limit at the trigger can miss a fill; a miss keeps the lock and costs nothing.
Q34: Not measured. 7 points are too few for a beta split.
Q35: Not regime-tested. The worst observed miss is LLY's run: -$184 for lock-only and Rung B, not for Rung A.
Q36: QHM names are large caps and the orders are small. The ask check uses the approved quote path.
Q37: 7 cases give no usable confidence interval, which is why there is a review after the first locked cases.
Q38: Non-working means a negative cumulative locked-case score at the review count. It then goes back to the owner for removal or redesign.
Q39: Size is unchanged and is not part of this design.
Q40: It protects "no QHM re-buy above an unearned price before earnings". Machine check: every QHM buy fill after a QHM close carries a released qhm_reentry event, with the close and ask at or below the trigger.
Q41: Score every exit plus re-entry at a common horizon, so exit quality itself can be measured. This is listed as the forward build.
Q42: Alpaca orders and fills, Alpaca SIP daily bars and the bot's own logs, not summaries.
Q43: An event study for the replay, a state machine for the lock, and fail-closed defaults. All are used.
Q44: Independent board seats, GAI and Codex reviewed it. After ship, the post-live audit compares the logged decisions with Alpaca orders.
