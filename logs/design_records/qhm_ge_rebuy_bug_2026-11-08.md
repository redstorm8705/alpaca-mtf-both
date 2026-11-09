# GE-REBUY-1108 — QHM must not buy back a hold someone else sold (bug fix, design record)

Approval-ID: GE-REBUY-1108
Covers: execution/quarterly_hold_manager.py, execution/manual_sale_proof.py, tests/test_qhm_ge_rebuy_1108.py (new)
Not changed: config.py, main.py, data/state/quarterly_holds_config.json

Status: rev 1 for owner approval (signed: Claude, 2026-11-08). The owner's separate re-entry rule ("no re-entry before
earnings unless very oversold or a steep unjustified drop") is a later, separate design; this record is the bug only.

## What happened (verified from the live logs, Alpaca orders and fills, 2026-11-08)

- 11-02: the owner sold 2 GE in the Alpaca app (~$314). Proven as an owner sale (`owner_sale_autoheal`).
- 11-04 18:10:37 UTC: the owner sold the last GE share in the app at $330.50 (order 697a85c3, source null, UUID id).
- 11-04 18:17:06: QHM saw the position flat and ran the owner-sale proof. It returned
  `proven=False reason=order 697a85c3 has fills but no fill time`, so the hold closed as `external_close_detected`.
  Alpaca's order today shows `filled_at 2026-11-04T18:10:37.106777Z`; the value the 18:17 read got was not logged.
- `add_candidate` only refuses to re-add a hold closed as `owner_app_sale` / `owner_confirmed`
  (`_OWNER_EXIT_REASONS`, quarterly_hold_manager.py:342, used only at :1243). `external_close_detected` is not in it.
- 11-05 02:04: GE was re-added as a candidate; 11-06 15:44 QHM bought 1 GE at $322.06 (order 962ae725).

## Root causes

1. **Fail-closed proof, fail-open re-buy.** Any close the proof cannot prove (a slow fill time, a missing field, an
   unreadable page) falls to `external_close_detected`, and that reason allows a buy-back. Nobody but the bot's own stop
   or the owner sells a QHM hold, so an unexplained close must also block a buy-back.
2. **A single read decides for good.** "has fills but no fill time" is a transient read, but unlike "order history
   unreadable" it is not retried.
3. **Latent parser defect.** On the server's Python 3.10, `_parse_ts` returns None for a fractional second that is not
   3 or 6 digits (tested on OCI: `18:10:36.47355Z` → None). Alpaca emits such values (GE order 697a85c3 `submitted_at`
   is `2026-11-04T18:10:36.47355Z`), so a fill time like that would fail the same way.

## The fix

1. `_OWNER_EXIT_REASONS` gains `external_close_detected` for the re-add check only (renamed `_NO_REBUY_EXIT_REASONS`).
   Re-dating the pick's `not_before_date` past the sale still re-enables it, exactly as for owner sales today.
2. `evaluate_owner_sale` marks "has fills but no fill time" as retryable; the QHM caller retries it the same way it
   already retries "order history unreadable" (keep the hold open, re-check next cycle, close unproven next day).
3. `_parse_ts` pads or trims the fractional seconds to 6 digits before parsing, so any Alpaca timestamp parses on 3.10.
4. Tests: re-add refused after `external_close_detected`; re-add allowed after re-dating; a 5-digit fill time proves the
   sale; a missing fill time is retried, not closed.

## Effect on trading

Fewer entries only: QHM no longer buys back a name that left the account without its own stop firing. No size, stop,
or exposure change. Size / frequency / concurrency delta: zero / down / zero.

## Reversal criterion

A QHM pick the owner wants back stays out: re-date its `not_before_date` (unchanged mechanism).

## Also found (not in this diff)

The live GE stop (order 247ba725, client id QH-GE-s-…) shows `source: null` although it carries a bot tag. Under the
existing tripwire this makes every future GE owner-sale proof fail as "signal untrusted". Fix 1 above makes that safe
for buy-backs; why Alpaca left the source empty on that one stop is a separate investigation.

## Required questions

Q1: Yes: QHM data stays clean; a buy-back of a name the owner exited pollutes QHM's edge record and ties up capital.
Q2: Fewer QHM entries only in one case (a hold that left without its own stop). Owner asked for it on 11-08 ("I don't
    think we should've re-entered the trade"); this record is the approval.
Q3: Yes. Simplest form: one tuple entry plus a retry and a parser fix.
Q4: QHM owner-sale auto-heal (design record qhm_owner_sale_autoheal_2026-10-17.md) built the guard this extends.
Q5: Nothing static added: the change adds one exit reason to a list and fixes parsing; no thresholds or numbers.
Q6: Static assumption: an unproven close is not the owner's. Forward pass: the owner's re-entry rule (oversold / steep
    unjustified drop, data-derived) as a separate design.
Q7: The exit reason feeds only the re-add check (`_OWNER_EXIT_REASONS` used only at quarterly_hold_manager.py:1243).
Q8: Yes: OCI log 2026-11-04 18:17:06 "owner-sale check (flat): proven=False reason=order 697a85c3 ... no fill time";
    11-05 02:04:14 "added candidate GE"; fill 962ae725 buy 1 @ 322.06; OCI Python 3.10 `_parse_ts("…36.47355Z")` → None.
Q9: Replay on GE 11-04 → 11-06: with the fix, the 11-05 re-add is refused. Expected: zero QHM buy-backs after an
    external close. Reversal: the owner wants a pick back and re-dating does not re-enable it.
Q10: No validated edge is held back; this blocks a buy-back the owner did not want.
Q11: Size zero, frequency down, concurrency zero.
Q12: No: stops and the kill switch are untouched.
Q13: A missing fill time now retries instead of closing unproven; an unreadable history keeps its existing retry.
Q14: No shadow: the block acts live on the next add_candidate call.
Q15: The existing "was sold by the owner … not re-adding" log line now also fires for external closes, naming the reason.
Q16: Log line "QuarterlyHoldManager: <SYM> was sold ... (external_close_detected, ...) — not re-adding".
Q17: Unit tests plus a read-only OCI run of `add_candidate` against a copy of the live state.
Q18: No: the GE re-buy is treated as a defect.
Q19: Yes (QHM entry): owner approves this exact design.
Q20: Yes: facts and logs only.
Q21: Tests mock every broker and HTTP call.
Q22: The read-only OCI run in Q17.
Q23: A read-only server run of add_candidate against a copy of the live QHM state, plus unit tests, before ready.
Q24: N/A — the design makes no existence or tradability claim.
Q25: N/A — no stock or market statistics are used; the facts are orders and log lines.
Q26: T1 Alpaca orders endpoint (unchanged).
Q27: data/state/quarterly_holds.json via the existing atomic `_save_state` (unchanged).
Q28: `add_candidate` (main.py:846 at startup) and the QHM external-close check on the run_cycle thread (unchanged path).
Q29: Owner-ranked next after the token cuts (11-08).
Q30: Standard practice: an unexplained position change is never auto-reversed.
Q31: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q32: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q33: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q34: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q35: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q36: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q37: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q38: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q39: N/A — bug fix to bookkeeping and re-add eligibility; no strategy signal, replay or sizing is changed.
Q40: Protects: no QHM buy-back after an exit the bot did not make. Check: the test plus the post-live log line.
Q41: Also fix the parser and the retry so owner sales are labelled correctly, not just blocked.
Q42: Raw Alpaca order JSON (source, filled_at) and the OCI logs.
Q43: Idempotent reconciliation: an unexplained exit is never undone automatically.
Q44: Cold second review, GAI + Codex pre-ship, and the first live session's logs.
