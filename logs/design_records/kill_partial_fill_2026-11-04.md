# KILL-PARTIAL-1104 — kill switch caches a partly-filled order as final (signed: Claude, 2026-11-04)

Approval-ID: KILL-PARTIAL-1104
Status: rev 2.2 — BGC aligned (board 3/3, GAI, Codex approve-with-changes, all folded). Awaiting owner approval. No code written.
Covers: execution/risk_manager.py, tests/test_kill_switch_partial_fill.py (new)

## What happened (server logs, 2026-11-04)
- XOM Swing stop order `be404bfa` (sell 3, IN- tag) filled in two pieces: 1 @ 172.18 at 14:32:37 UTC
  (`partial_fill`, leaves 2) and 2 @ 171.70 at 14:33:58 UTC (`fill`, cum 3).
- The kill switch looked up the order between the two fills; `_tier_for_fill_order` stores the order facts
  (`qty 3, filled_qty 1`) as "immutable for the session" (`execution/risk_manager.py` ~L593-596).
- From the next check on, the 2-share activity fails `activity_qty > order_facts["filled_qty"]` (L470-486):
  "kill-switch fill/order facts mismatch for be404bfa… — degrading the intraday measure" — 87 times on 11-04
  (first 14:37:59 UTC, still logging at 23:53 UTC).
- Effect: `_today_fills` returns ok=False all day → the kill switch used its fallback whole-account equity measure
  (more sensitive, never masks a loss) and the operator snapshot carried `attribution_exact: false`, so the dashboard
  showed "Daily brake status unavailable" for the rest of the session.

## Root cause
A partly-filled order's `filled_qty` is not immutable; the cache treats any successful read as final.

## Proposed fix (rev 2 — GAI round 1 folded: no re-read, no new network call)
The order facts the kill switch caches are split by whether they can change:
- **Immutable** (cached for the session as today): order id, symbol, side, total order `qty`, tier from the client
  order id (Alpaca replaces an order under a NEW id, so these never change for one id).
- **Mutable** `filled_qty`: no longer used to judge a fill.

Each fill activity is checked against the immutable facts and its OWN running total, which Alpaca puts on every FILL
activity (`cum_qty`; on 11-04: 1 on the partial, 3 on the final):
`symbol == order symbol`, side matches, `0 < activity qty <= activity cum_qty <= order qty`, integers.
Anything else — missing or non-numeric `cum_qty`, `cum_qty` above the order size, a symbol/side/tier mismatch —
still returns the unresolved sentinel and degrades to the more sensitive equity measure (unchanged).

No re-read and no extra broker call on the run_cycle thread (GAI round 1). Never-mask-a-loss: a fill is only ever
attributed to the tier of the exact order id it belongs to; a quantity can never be accepted above the order's total.

## Rev 2.1 — board (Taleb + Thorp masked-loss, Peterffy reliability) folded

Board on rev 1 (re-read design): 3/3 approve-with-changes; no input found where the fix under-counts a Day/Swing loss.
Their extra-broker-call concerns (Peterffy P3/P4: one unbounded `get_order` per activity per refresh, no timeout)
do not apply to rev 2, which adds no broker call. Folded:
1. **Sum check (Thorp T3):** within one `_today_fills` pass, the activities for one order id must sum to at most the
   order `qty`, and the largest activity `cum_qty` must equal that sum (they are the same fills); otherwise →
   unresolved sentinel → degrade.
2. **Tier stays bound to the immutable lookup (Taleb T1):** unchanged first lookup per order id with the id check,
   field validity and full QH/F6 tag-envelope check; nothing in rev 2 re-reads or changes the tier.
3. **Tests (Taleb T2):** a protected QHM order filling in two pieces across two kill checks is excluded only on proven
   facts with no window where an active fill drops out; an active Swing order filling in two pieces (the 11-04 XOM
   case: 1 then 2, cum 1 then 3) stays exact; a cum_qty above the order qty, a missing cum_qty, and activities
   summing above the order qty each degrade.
4. Mismatch warnings carry the order qty, activity qty and cum_qty, rate-limited like `_log_kill_attr` (Peterffy P5).

## Rev 2.2 — reviewer round 2 folded (2026-11-05, signed: Claude)

| Voice | Rev 1 (re-read) | Rev 2 / 2.1 (cum_qty, no broker call) |
|---|---|---|
| Board (Taleb + Thorp masked-loss, Peterffy) | 3/3 approve-with-changes | extra-call concerns removed by rev 2 |
| GAI | reject (re-read race) | approve-with-changes |
| Codex (reviewer #2) | — (usage limit) | approve-with-changes: "no valid input where this design measures an active Day/Swing loss smaller than the activities supplied" |

Folded:
1. **Sequence validation, not just sum (Codex failing inputs `1/cum2, 2/cum3` and `2/cum3, 1/cum3`):** group the
   pass's activities by order id; reject duplicate activity ids; sort by transaction time, activity id as tie-break;
   require `cum_qty[i] == cum_qty[i-1] + qty[i]` and every `cum_qty <= order qty`. The first activity of the session may
   start above its own qty only for an order that began filling on an earlier session (a GTC stop): the derived
   starting base `cum_qty[0] - qty[0]` must be >= 0 and is logged; that case has its own test.
2. **Whole pass or nothing (GAI 3):** any invalid activity anywhere → the pass returns ok=False and the kill switch
   uses the equity fallback for that check; nothing is partly attributed (today's behaviour, kept).
3. **Pagination complete (Codex):** the fills fetch (`reporting.pnl_ledger.fetch_all_fills`) must have exhausted its
   pages; an unconfirmed last page → ok=False. Verified in the build by reading `fetch_all_fills` in full.
4. **Mismatch log (Codex 15, GAI 2):** order id, symbol, side, order qty, activity id, activity qty, cum_qty, position
   in the sequence, reason, and that the equity fallback was selected; rate-limited like `_log_kill_attr`.
5. **Tests:** the 11-04 XOM sequence (exact, no degradation); both Codex malformed sequences; cum_qty above the order
   qty (GAI 1); a duplicate activity id; a missing / non-numeric cum_qty; a cross-session GTC order; a protected QHM
   order filling in two pieces (Taleb T2). All broker and activity calls mocked.

**Alignment reached:** board, GAI and Codex on rev 2.2. Owner approval next; then build through the full gate.

## Required questions
Q1: Keeps the 7% kill switch on its exact Day + Swing measure instead of a day-long fallback after any multi-piece fill; restores the owner-visible brake status. Not conservative: it removes a false degradation.
Q2: No direct change. The fallback measure (whole account incl. QHM/F6) is different and broader than the exact Day + Swing measure: it can trip on QHM/F6 moves, or hide a Day/Swing loss when QHM/F6 is up. Removing a false degradation keeps the brake on the measure the owner approved.
Q3: Yes — a correctness fix in the approved KILL-ATTR-1023 design; simplest form is cache-only-when-terminal.
Q4: KILL-ATTR-1023 (`logs/design_records/kill_switch_tier_attribution_2026-10-23.md`, #753) introduced the cache; post-live #753 is overdue; no open PR touches execution/risk_manager.py (origin/main 11-04).
Q5: The terminal-status list comes from Alpaca's order states; no numeric threshold added.
Q6: Exposed assumption: one read of an order is final. Next: event-driven order state (trade updates stream) for every component that caches broker order facts.
Q7: Same cache pattern should be checked wherever order facts are memoized (fill notifier, dashboard trade log); listed as a follow-up search, not coupled here.
Q8: Measured (OCI logs/mtf_bot.log, 2026-11-04 14:37:59 UTC onward, 'kill-switch fill/order facts mismatch for be404bfa'; Alpaca FILL activities): two FILL activities for be404bfa (partial_fill qty 1 14:32:37Z, fill qty 2 14:33:58Z); 87 mismatch warnings on 11-04; snapshot `attribution_exact: false` at 20:55 UTC.
Q9: Replay 11-04's fills through the fixed function: expected exact attribution after 14:33:58 and `attribution_exact: true` snapshots; wrong if any multi-fill order still degrades the measure.
Q10: No — this is a correctness fix; no validated edge is involved.
Q11: No size/frequency/concurrency change by design; it touches the kill-switch measure → safety-envelope review: board + masked-loss seat.
Q12: Touches the kill switch's input. Bounded: failure modes still degrade to the more sensitive measure; nothing widens the envelope.
Q13: A failed re-read returns the unresolved sentinel → degrade (unchanged).
Q14: No shadow — the fix acts live on the kill-switch measure; logging only records what live code did.
Q15: The existing attribution log (`kill-switch attribution exact=…`) and snapshot record the result; each mismatch warning carries order id, symbol, side, order qty, activity id, qty, cum_qty, sequence position, reason and the fallback selected.
Q16: Proof: no mismatch warning for a multi-fill order and `attribution_exact: true` snapshots through the session; audit the first session with a partial fill.
Q17: Read-only replay of 11-04 on the server against the real fills before ship.
Q18: Yes — the "Daily brake status unavailable" banner had been read as a display issue; it was a real degradation.
Q19: Not entry/exit/sizing routing; owner approves because it is a kill-switch input.
Q20: Root cause traced at source (code + fills + order); review prompts will be neutral.
Q21: get_order is mocked in tests; no broker call.
Q22: Not a new fail-closed control; read-only replay on the server before live.
Q23: Full-path test: partial then full fill across two kill checks → exact measure, no degradation.
Q24: N/A — the design makes no 'X doesn't exist / isn't tradable' claim.
Q25: N/A — the design quotes no stock or market statistics; the facts are the logged fills and order.
Q26: T1 Alpaca Trading API (orders, FILL activities); fallback unchanged (equity measure).
Q27: No new output; existing snapshot (atomic) and logs.
Q28: `check_kill_switch` → `_today_fills` → `_tier_for_fill_order`, on the run_cycle / Day loop threads; no new broker call (the existing first order read per order id is unchanged).
Q29: High: it affects the only daily brake's accuracy every time a stop fills in pieces.
Q30: Missing: order-state events instead of polling; underweighted: caches of mutable broker state.
Q31: N/A — correctness fix to the kill-switch input; no edge or strategy is tested out of sample.
Q32: N/A — correctness fix to the kill-switch input; no trading edge, replay or market statistic is involved.
Q33: N/A — correctness fix to the kill-switch input; no trading edge, replay or market statistic is involved.
Q34: N/A — correctness fix to the kill-switch input; no trading edge, replay or market statistic is involved.
Q35: N/A — correctness fix to the kill-switch input; no trading edge, replay or market statistic is involved.
Q36: N/A — correctness fix to the kill-switch input; no trading edge, replay or market statistic is involved.
Q37: N/A — correctness fix to the kill-switch input; no trading edge, replay or market statistic is involved.
Q38: N/A — correctness fix to the kill-switch input; no trading edge, replay or market statistic is involved.
Q39: N/A — the change does not size positions; Kelly sizing from Alpaca fills is untouched.
Q40: Protects the exact Day + Swing kill measure. Machine check: zero "facts mismatch" lines for orders whose activities sum to their filled_qty.
Q41: Move every broker-order cache to the trade-updates stream with order version numbers.
Q42: Raw FILL activities and the order record (used).
Q43: Cache only immutable order facts; judge each fill by its own running total (cum_qty) against the order size.
Q44: Cold second review, GAI + reviewer #2, and the server replay of 11-04's fills.
