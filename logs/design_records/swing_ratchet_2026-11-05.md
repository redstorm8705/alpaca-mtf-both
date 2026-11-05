# SWING-RATCHET-1105 — a Swing lot well in profit never keeps a stop below entry (signed: Claude, 2026-11-05)

Approval-ID: SWING-RATCHET-1105
Status: rev 9 — confirming pass pending. Awaiting owner approval. No code written.
Covers: execution/swing_breakout_manager.py, generate_dashboard.py, logs/research/swing_pm/ratchet_params.json (new), logs/research/swing_pm/breakeven_ratchet_replay.py, tests/test_swing_ratchet.py (new)

## Owner direction (2026-11-05, verbatim)
"I want the stop raised to lock in some profit if brkb reverses." / "That's actually below entry which absolutely should
not happen this far into profit."

## The problem (live)
BRK.B Swing lot: entry $544.60 (10-27), 1R $16.39, peak close $579.09 (+2.10R), price ~$581 on 11-05. The broker stop is
still $527.81 — the entry stop, never moved. A reversal can turn a +2R winner into a −1R loser (−$33.58 on 2 shares
after being +$73). The Swing tier sets its GTC stop once at entry and never moves it (execution/swing_breakout_manager.py:
stop placed at entry; exits are only the 20-session-low close rule, the 30-session time exit and that stop).

## Replay (logs/research/swing_pm/breakeven_ratchet_out_2026-11-05.txt; 48 names, discovery 2020-23 / test 2024-26)
| Rule | Disc mean R | Test mean R | Winners cut short (test) |
|---|---|---|---|
| base (stop never moves) | +0.374 | +0.592 | — |
| stop → entry at +2R (R k=2) | +0.373 | +0.567 | 3 (−9.1R) |
| stop → entry + 0.25R at +2R (R+ k=2) | +0.377 | +0.572 | 3 |
| close below entry after +2R → exit at the close (C k=2) | +0.376 | +0.582 | 1 |
| stop → entry + 0.25R at +3R (R+ k=3) | +0.378 | +0.592 | 0 |
Trial count: 46 looks at the 2024-26 data across three replays; no variant beats base after that many looks. The owner's
rule is a risk-shape choice (no +2R winner becomes a loss), not an edge claim. Its measured price is ~0.01-0.02R per
trade (~$0.15-0.33 per share at BRK.B's 1R).

## Proposed rule
Once a Swing breakout lot's daily close reaches entry + 2R (R = the lot's own entry stop distance), its broker GTC stop is
replaced at max(current stop, entry + 0.25R) — the R+ k=2 variant: it honours "never below entry" with a resting
intraday stop and locks a small profit. Ratchet only up, never down; one replace per trigger; the new stop keeps the
tier's IN- client order tag (the bot already replaces stops this way).
For BRK.B today: R = entry − the lot's own entry stop = $544.60 − $527.81 = $16.79 (state file); $544.60 + 0.25 × $16.79 = **$548.80** (rev 1 used the replay's $16.39, from its own ATR — corrected).

## Rev 2 — GAI + Codex round 1 folded (2026-11-05, signed: Claude)
Both approve-with-changes. Folded (all implementation rules; the rule itself is unchanged):
1. **R is immutable per lot:** `initial_risk = entry_px − stop_px` from the lot's state at fill (BRK.B: $16.79). Never
   recomputed from the current broker stop or volatility. Missing / non-positive → no ratchet for that lot.
2. **Trigger on a completed daily bar only:** the newest COMPLETED T1 Alpaca daily close (session date before today, or
   today after the close), once per session per lot (`ratchet_checked` = session date, persisted). Partial, stale or
   missing bars → no change.
3. **Exact order, exact tier:** the order replaced is the lot's own `stop_order_id`, confirmed live, a SELL stop with
   the `IN-` tag and quantity equal to the lot's quantity. Zero or several candidates, a non-IN tag (e.g. an owner
   manual stop), a quantity mismatch, or a non-open status → no replace; logged; the existing stop is kept.
4. **Idempotent from broker truth:** no-op when the confirmed live IN- stop is already ≥ the target. The target is
   rounded UP to the tick so it never sits below entry + 0.25R. A hard check refuses any target below the lot's entry
   price (and below the current stop).
5. **After every attempt** (success, rejection incl. 40310000, timeout): re-read the position and the lot's orders;
   success → persist the new order id and `stop_px` in state at once (so `_reprotect` never re-places the old level);
   rejected / unclear → the old stop stays, one page, retry next session.
6. **Precedence:** runs inside `run_exit_check` AFTER the lot's trend/time exit decision for the session; a lot with an
   exit chosen, `exit_unverified`, or an in-flight close is skipped. The Swing manager is single-threaded on the
   run_cycle thread (one mutation path per lot).
7. **Price already through the target** (the completed close is above +2R but the live price is at/below the target):
   no stop is placed above the market; the lot stays on its current stop and the normal exits.
8. **Decision record** in trade_events.jsonl per ratchet decision: lot id, symbol, entry, initial stop, R, completed
   close + its session date, trigger, target before/after rounding, old order id/price/qty/status, new order id/status,
   position qty, action (replaced / no-op / skipped) and reason, and the post-replace reconciliation result.
9. **Tests:** 40310000 rejection keeps the old stop; double cycle does not replace twice; owner manual (non-IN) stop is
   left alone; target never below entry; partial bar ignored; state stop_px updated after success.

## Rev 3 — board (Brandt, Harris) folded (2026-11-05, signed: Claude)
Both seats approve-with-changes on the shape (R+ k=2: lock entry + 0.25R once a completed daily close reaches +2R;
k=3 never arms on BRK.B, C k=2 leaves the intraday stop below entry). Folded:
1. **Own exit reason (risk path, C4):** a fill at a ratcheted stop is booked `exit_reason="ratchet_stop"` (stop_px >
   init_stop_px) and is excluded from the protective-stop loss multiple in `kelly_risk`; without this, small ratchet
   wins would shrink the loss multiple and up-size new lots (~30% in the board's example). The masked-loss seat is
   required on the code diff.
2. **Placement and data:** a new `_ratchet(state)` pass in `run_exit_check` right after `_reconcile`, before the
   near-close early return, regular hours only (`_market_open()`), armed from `fetch_closed_bars` (completed daily
   bars, max close since entry_date) — never from a near-close last trade.
3. **Immutable risk per lot:** new `init_stop_px` field (set at entry; for existing lots = today's `stop_px`, logged);
   R = `risk_usd / qty` when present, else `entry_px − init_stop_px`. `entry_px_estimated` true → no ratchet, paged.
   BRK.B check before go-live: state `stop_px` equals the broker stop ($527.81) — read-only on the server.
4. **Order actually in force:** `broker.resolve_live_order(stop_id)` follows a replaced chain; untagged (owner) stop →
   never touched, paged once; already ≥ lock → no-op; never lowered. Ratchet only when the lot is open, protected, no
   close order or pending exit reason, no partial stop fill, and the stop reads non-terminal with 0 filled.
5. **Race with a fill:** keep `stop_order_id_prev`; the terminal branch and `_book_gone` check the previous id's fill
   before calling a close unverified; the previous id is cleared once the new order reads live. New id, `stop_px` and
   `ratchet_stop_px` are saved in one state write.
6. **Price already at/below the lock** in regular hours: close the lot (`_close_lot(reason="ratchet_breach")`) instead
   of keeping a stop below entry. Stop-market (not stop-limit), so a gap still fills.
7. **No fixed thresholds in code (no-static rule):** the trigger (2R) and lock (0.25R) are read from a replay-produced
   parameter file `logs/research/swing_pm/ratchet_params.json` (values, the replay that produced them, its date and
   trial count); the replay is re-run each quarter on the tier's universe and the file re-derived. Missing or invalid
   file → no ratchet (the current behaviour).
8. **Other IN- stop movers:** confirm in the build that `exit_logic.py:210` and `lifecycle.py:410` never act on Swing
   breakout lots (Swing and core intraday share the IN- tag).
9. **Machine check (Q40, corrected):** no lot whose completed close reached +2R has a resting broker stop below entry
   in any regular-hours cycle after the next session's open; realised R reported separately.
10. Tests added: replace rejected after a fill, a "replaced" stop id, an untagged owner stop, price already below the
    lock, crash between PATCH and save, estimated entry, after-hours no-op, ratchet_stop excluded from kelly_risk.

11. **Empty stop id in state (found on the server 2026-11-05):** BRK.B's lot has `stop_order_id = ""` (the stop rests
    at $527.81, found by `_find_resting_stop`). The ratchet resolves the order with the same tag-based lookup
    (`IN-` SELL stop, qty = lot qty, exactly one candidate), writes the resolved id into state before replacing, and
    skips with a page on zero or several candidates.

## Rev 4 — Codex + GAI round 2 folded (2026-11-05, signed: Claude)
Both found concrete failing inputs; all folded. Rev 4 overrides any earlier item it contradicts.
1. **Booking by provenance, not price (Codex 1):** `exit_reason="ratchet_stop"` only when the filled order id equals the
   lot's persisted `ratchet.new_order_id` (or a replaced descendant per `resolve_live_order`). A stop raised by any other
   path keeps its existing exit reason and stays in the loss multiple (input 100/90 → 95 fill = −0.5R stays counted).
   A ratchet fill below entry (gap) is booked `ratchet_stop` but its realised R still enters the loss multiple as a
   loss; only fills at or above entry are excluded (never-mask-a-loss).
2. **Immutable risk per share (Codex 3, 6):** new `init_risk_ps` field = entry_px − the protective stop submitted at
   entry, written once at fill. Existing lots: reconstructed from the entry event in trade_events.jsonl (entry price,
   stop, qty); if that record is missing or disagrees with state → no ratchet for that lot, paged. Never derived from
   today's stop or current qty. BRK.B: entry 544.60, entry stop 527.81, qty 2 → 16.79 — confirmed against its entry
   event before go-live.
3. **One state machine per lot (Codex 4, GAI 1, 3):** `ratchet = {status: idle|pending|done|blocked, armed_session,
   target_px, old_order_id, new_order_id, attempt_ts, reason}`. `pending` (with old id + target) is saved BEFORE the
   PATCH. Every regular-hours cycle starts by reconciling a pending lot from broker truth: the stop in force (resolve the
   old id's chain + the tag lookup) at ≥ target → done (save new id, stop_px); old stop live at the old price with no
   other IN- stop → idle (retry allowed this cycle); anything else (two stops, untagged stop, unknown) → blocked, paged.
   No second PATCH while pending. A crash between PATCH and save is resolved by this reconcile, never by a new PATCH.
4. **Breach close is fill-aware (Codex 2):** price at/below the lock → cancel the IN- stop and read it back terminal;
   if it filled (fully or partly) book that fill first; re-read the live position; sell only the remaining qty (never
   more than held, never opening a short). Tested with the stop filling during the close. The close is fill-aware because a triggered
   stop can be pending or part-filled in a fast market (Rev 5 item 1).
5. **Bounded escalation, so the guarantee holds (Codex 5):** a lot that is armed but not `done` by the Swing tier's
   existing near-close check that session is closed at that check (same fill-aware close as item 4), paged. Q40 is
   enforced, with the close as the fallback, not a "keep the old stop" exception.
6. **Mismatch never lowers (GAI 5):** if the broker stop differs from state, the broker is truth for reading; the bot
   never lowers a stop and pages on any difference.
7. **Tick size (GAI 4):** target rounded UP to $0.01 for prices ≥ $1 (SEC Rule 612 sub-penny rule); lots under $1 are
   skipped.
8. **Arming timing (Codex nit):** a completed daily close arms the lot; it acts at the next regular-hours cycle (the
   ratchet never runs outside regular hours). The parameter file is schema-checked (version, finite positive trigger,
   non-negative lock < trigger), read atomically; any failure → no ratchet, paged.
9. Decision records include the broker response id. Tests add: a raised stop that is not a ratchet keeps its loss;
   qty reduced after entry; crash between PATCH and save; stop fills during the breach close; unresolved at near-close
   → closed; malformed parameter file.

10. **Masked-loss seat (Taleb + Thorp lens) folded:** (a) a ratchet-family exit (`ratchet_stop`, `ratchet_breach`,
    partials) with R < 0 stays in the loss multiple `stop_rs` (`swing_breakout_manager.py:295`); only R ≥ 0 is left out
    (test: a $520 gap fill at the $548.80 stop counts at −1.47R). (b) The exit label is set once, by one helper used by
    all six booking sites (lines 1325, 1328, 1421, 1469, 1474, 1497); `init_risk_ps` and `ratchet` are added to
    `_archive_closed`'s key list (line 283). (c) `lot_r` (line 251) and `_lot_risk` (line 427) fall back to the entry
    risk, never the current `stop_px`; `init_risk_ps` is written at the entry booking (line 1096). (d) `kelly_risk`'s
    decision record lists each excluded lot and its R. (e) Dashboard labels for the two new exit reasons.

## Rev 5 — Codex + GAI round 3 folded (2026-11-05, signed: Claude)
1. **Breach close never overlaps the stop (Codex 1, GAI 1-2):** state `ratchet.status = breach_cancel_pending` is saved
   before the cancel. No sell is sent until the stop reads terminal. A cancel error (404/422 "already filled", timeout)
   is never taken as success or failure by itself: the order is read back. Filled → book that fill, done, no page.
   Partly filled then canceled → book the fill, re-read the live position, sell only what is still held. Still
   new/accepted/pending_cancel → stay pending, reconcile each cycle; at the near-close check keep reconciling and never
   send an overlapping sell; if still non-terminal there, the stop stays in force and the owner is paged.
   Tests: cancel timeout; pending_cancel → late fill; 422 already filled; partial fill then cancel.
2. **Q40 covers every lot whose qualification is known (Codex 2):** qualification needs R and the trigger. R missing or
   contradicting its entry record → qualification unknown → no ratchet, paged once per lot (Q40 is stated for lots with
   an established R). Parameter file unreadable or invalid → the last valid version (kept beside it, written atomically)
   is used; with no valid version ever, no ratchet and paged. A lot with known R and trigger that reached +2R and cannot be
   protected for any reason enters the bounded state of Rev 4 item 5 (fill-aware close at the near-close check).
3. **Reconstruction is quantity-independent (Codex nit):** `init_risk_ps` is checked against the entry record's price and
   stop only, never against the current quantity (partial reductions are legitimate). A disagreement disables the
   ratchet for that lot until the owner resolves it (GAI 3).

## Rev 6 — one mechanism, no cancel (Codex round 4 folded, 2026-11-05, signed: Claude)
Rev 6 overrides Rev 3 item 6, Rev 4 items 4-5 and Rev 5 item 1 (the cancel-then-sell breach close and the near-close
forced close are REMOVED). Reason: cancel-then-sell has a window with no stop and a cancel that can miss any deadline.
1. **Only ever a replace of the lot's own stop.** Target = min(entry + lock×R rounded up to $0.01, the newest regular-hours
   bid − $0.01), and never below the current stop. If the price is already at/below the lock, the stop is raised to just
   under the market (it may fill at once — that is the intended exit; if the price is below entry the exit is below
   entry because the market is). The order is never canceled, so the lot is never without a stop and a sell never
   overlaps it.
2. **State machine unchanged (Rev 4 item 3):** pending saved before the PATCH; every cycle reconciles from broker truth
   (resolve the chain + tag lookup). Old id 404 + no tagged stop + shares still held → `blocked`, paged CRITICAL, and the
   existing `_reconcile` re-protection (which re-places a stop at state `stop_px`) runs as today; no sell is sent by the
   ratchet. A rejected replace (e.g. stop above market after a fast move) → idle, retried next cycle with a fresh bid.
3. **Q40, stated honestly:** for every lot with an established R whose completed close reached +2R, by the next
   session's near-close check its resting stop is ≥ min(lock, bid − $0.01) — or the lot is closed — except when the
   broker has not confirmed the replace (pending/blocked), which is paged each session it persists. This is the owner
   exception Codex asked to be made explicit; it is in the approval package.
4. Tests: replace rejected as above market → retried with a fresh bid; pending_replace through near-close → paged, stop
   still in force; old id 404 with no tagged stop → blocked + existing re-protection, no sell; breach raise fills at once
   → booked by provenance.

## Rev 7 — replace outcomes reconciled; both owner exceptions explicit (Codex round 5 folded, 2026-11-05, signed: Claude)
Rev 7 overrides Rev 6 item 2.
1. **Target formula:** `target = max(current_stop, min(lock_px, bid − $0.01))`, lock_px = entry + lock×R rounded up to
   $0.01. Bid valid only if positive, finite, not crossed, for this symbol, and within the quote age the Swing tier
   already uses for its own quotes (no new number).
2. **New id saved at once:** the PATCH response id is written to `ratchet.new_order_id` in the save right after the
   call. A crash before that save leaves `pending` with no new id; reconcile finds it from the broker (below).
3. **Re-protection waits on the ratchet:** while a lot's ratchet is `pending` or `blocked`, `_reconcile`'s re-protection
   (`_reprotect`) does NOT place a stop for that lot; the ratchet reconcile owns it.
4. **Reconcile from broker links, not one empty lookup:** read the old order by id (closed orders stay readable) and
   follow `replaced_by`; if the new id is unknown, list the symbol's orders since `attempt_ts` (status all) and match
   `replaces == old id`. Outcomes:
   - new order live (new/accepted/held) → `done`, save its id and stop_px;
   - old order still live, not replaced → `idle`, retry allowed;
   - old `replaced` and new rejected/canceled/expired, or old canceled with no successor, shares still held and no live
     sell stop → re-protect at once at the last confirmed stop level (state `stop_px`, unchanged), then `idle`;
   - new order filled → booked by provenance;
   - anything not proven (old unreadable, links missing, two live stops) → stays `pending`, no new order of any kind,
     paged CRITICAL each cycle it persists (deduped).
5. **Q40 exceptions, both explicit for owner approval:** (a) a replace the broker has not confirmed (pending/blocked),
   paged; (b) the market already below entry: the stop is raised to just under the bid, which is below entry because
   the market is — an exit near market, not a protected profit.
6. Tests add: PATCH ok then old=replaced/new=rejected → re-protected at old level; new order visible only on the second
   lookup → no duplicate stop; crash before saving the new id → found by `replaces`; `_reprotect` skipped while pending;
   stale/crossed bid → no replace.

## Rev 8 — re-protection reconciled the same way (Codex round 6 folded, 2026-11-05, signed: Claude)
Rev 8 overrides the re-protect bullet of Rev 7 item 4.
1. **Re-protection is its own pending state:** `ratchet.status = reprotect_pending` with a deterministic
   `client_order_id` (lot coid + a sequence number) is saved BEFORE the POST. Alpaca refuses a second order with the same
   client_order_id, so a retry after a timeout cannot create a duplicate stop. Each cycle reads that id
   (`get order by client_order_id`): live → `done` at that level; rejected/canceled/expired and still no live sell stop →
   sequence + 1, retry; not found after the call returned → retry with the SAME id (the broker refuses if it exists).
   Never `idle` until exactly one live sell stop for the held quantity is proven.
2. **Level when the old level is above the market:** stop at `min(last confirmed stop, bid − $0.01)` — an executable
   stop near market. This is owner exception (c): with no live stop, an executable stop below the last confirmed level
   beats no stop.
3. **States:** idle | pending | reprotect_pending | done (`blocked` removed; every unproven outcome stays pending or
   reprotect_pending). Pages are deduplicated per (lot, status), re-sent once per session while the status persists,
   reset on any transition.
4. Tests add: re-protect timeout after acceptance → no second stop; re-protect rejected → retried; old level above
   bid → stop at bid − $0.01; delayed visibility by client_order_id.

## Rev 9 — re-protection id format (Codex round 7 folded, 2026-11-05, signed: Claude)
1. Re-protection `client_order_id` = `IN-rp-` + the first 24 hex chars of sha256(lot coid) + `-` + sequence: 33 chars at
   sequence 9999, inside Alpaca's 48-char limit, only [A-Za-z0-9-], keeps the tier's `IN-` tag, deterministic per
   (lot, sequence), and distinct lots differ in the hash. Tests: a 48-char lot coid; two long lot coids sharing a
   prefix; the same id reused on retry; successive sequences distinct.
2. Every stop price (raise or re-protection) is rounded to $0.01 (down for a level set under the bid) and must be
   > $0; otherwise no order and paged.
3. Decision records add the sequence, client id, submitted qty and the live position qty.

## Required questions
Q1: Risk shape for data collection: no +2R winner turns into a loss; the cost is measured (≈0.02R/trade on test) and small against the account goal; it keeps winners running (no profit target added).
Q2: No new entry rule. Earlier exits free names, so the replay shows a few more trades (+5 / −4 on test); it ends a few winners early (3 in 2024-26).
Q3: Yes per owner direction; simplest form is one stop replace at +2R. A closing-basis variant (C k=2) is cheaper but leaves the intraday stop below entry, which the owner ruled out.
Q4: GAP-EXIT-1029 (gap-down handling, approved, not built) and EARN-GAP-REVIEW-1104 touch the same lot's stop on gap days; the ratchet only raises a stop and runs after their decisions. SWING-OWNER-1025 sizing is untouched.
Q5: Trigger and lock are in R units of each lot's own stop distance and are read from a replay-produced parameter file re-derived each quarter (Rev 3 item 7); no threshold literal in the code.
Q6: Exposed assumption: an entry stop is good for the whole hold. Next: a stop that follows the lot's own volatility (the D family failed; revisit once the tier has its own fills).
Q7: Interacts with exits (stop), the dashboard (shows the ratcheted stop and its basis), the kill switch (a ratcheted stop fill is a normal Swing fill). Each fails safe: a failed replace keeps the old stop.
Q8: Measured: BRK.B live stop $527.81 vs entry $544.60 (dashboard and broker 11-05); replay numbers above, with three cut winners re-priced from raw bars.
Q9: Replay as above. Expected live effect: ~0.01-0.02R lower mean per trade, no +2R winner closing below entry. Wrong if live winners cut at the ratchet stop later run past +3R more often than the replay's 3 per 120.
Q10: No edge is being held back; this trades a little mean R for no-loss-after-+2R by owner choice.
Q11: Risk-path through the Kelly loss multiple unless ratchet fills get their own exit reason (Rev 3 item 1 does that); masked-loss seat on the code diff. Size, entries and exposure are otherwise unchanged.
Q12: Tightens the stop (inside the envelope); a stop on every position is kept at all times (replace, not cancel-then-place).
Q13: Replace fails or the order is unreadable → the per-lot state machine (Rev 4 item 3) reconciles from broker truth each cycle, retries only when the old stop is proven still in force, never sends a second replace while pending, and pages each session it stays unconfirmed (Rev 6); it never cancels the stop or sends a sell.
Q14: No shadow: it acts live after approval and the gate.
Q15: Each ratchet logs symbol, close, R multiple, old stop, new stop, order ids, to trade_events.jsonl.
Q16: Proof line `swing ratchet ... [change=swing-ratchet-1105]`; audited on the first lot that reaches +2R (BRK.B qualifies on day one).
Q17: Read-only dry run on the server first: compute the ratchet for each open lot without placing; then the first live replace is checked against the broker order.
Q18: Yes — "the stop never moves" was by design; the owner's BRK.B case re-examined it.
Q19: Yes (exit change) — owner approves this exact rule.
Q20: Replay run by an independent agent from raw bars; reviewers next.
Q21: Tests mock every broker call (replace_order / get_order).
Q22: Not a fail-closed control; dry run on the server before live.
Q23: Full-path test: lot reaches +2R → replace called once with the IN- tag → state updated → a second cycle does not replace again.
Q24: N/A — the design makes no existence or tradability claim.
Q25: Measured from Alpaca daily bars in the replay, not memory.
Q26: T1 Alpaca daily bars (closes) and the Alpaca Trading API (order replace); no new source.
Q27: Swing state file data/state/swing_breakout.json (existing atomic writes); events append to trade_events.jsonl.
Q28: A `_ratchet` pass in run_exit_check after `_reconcile`, before the near-close return, regular hours only, on the run_cycle thread; armed from completed daily bars; one replace per lot per arming.
Q29: High: it protects real open profit today (BRK.B +$73) at a measured small cost.
Q30: Standard frameworks trail or ratchet stops; the bot never moved a Swing stop.
Q31: 46 looks at the test years; no variant beat base; the rule is adopted as a risk-shape choice, not an edge.
Q32: Daily bars closed before each decision; universe survivorship stated in the replay.
Q33: Stop fills at min(open, stop) in the replay; live gap slippage not modelled (stated).
Q34: N/A — no return forecast; it changes the shape of exits only.
Q35: The replay covers 2020-26 incl. the 2022 bear market; worst cases are the 3 cut winners listed.
Q36: Works at any size; one order replace per lot.
Q37: Effect size ~0.02R with t ≈ −1.9 on test; not an edge claim.
Q38: Review after 30 ratchet events: if cut winners exceed the replay rate, revisit the trigger.
Q39: Sizing is untouched (SWING-OWNER-1025 quarter-Kelly from the tier's own fills).
Q40: Protects "no +2R winner keeps a stop below entry past its next session" for lots with an established R. Machine check: by the next session's near-close check the resting stop is ≥ max(current stop, min(lock, bid − $0.01)) or the lot is closed. Three explicit exceptions (Rev 7 item 5, Rev 8 item 2): a broker-unconfirmed replace or re-protection (paged), a market already below entry (stop raised to just under the bid), and a lost replace re-protected at bid − $0.01 when the old level is above the market.
Q41: Make the trigger and the lock level follow the tier's own fill history once it has 30 closed lots.
Q42: Alpaca daily bars and the broker order records.
Q43: Break-even / profit-lock stop after a set gain is a standard trend-following risk control.
Q44: Independent replay agent; cold second review; GAI + Codex; first live ratchet reconciled with the broker order.
