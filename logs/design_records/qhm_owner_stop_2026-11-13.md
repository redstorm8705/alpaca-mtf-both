# QHM-OWNERSTOP-1113 — the owner can set a QHM stop price, and the bot keeps it until a date

Approval-ID: QHM-OWNERSTOP-1113
Covers: execution/quarterly_hold_manager.py, scripts/qhm_owner_stop.py, tests/test_qhm_owner_stop_1113.py
Not changed: scripts/qhm_owner_trim.py, strategy/run_cycle.py, execution/broker.py, execution/ownership_guard.py, config.py, data/fetcher.py
Status: rev 3 (signed: Claude, 2026-11-13): reviewer changes folded in; for confirmation.

## Owner direction
2026-11-12, after the NVDA morning: "There likely isn't any code around executive, ceo overrides but I'd like to try and
understand what options are available or that can be built" — then, offered an owner stop override: "yes, agreed."

## Problem (measured)
- QHM sets its own stop price (entry/add design stop, trail raises, QHM-RESTOP-1112 after adds) and the per-cycle
  reconcile moves the broker stop back to QHM's recorded price. An owner change in the Alpaca app is undone within a
  cycle (11-12: the owner cancelled NVDA's $163.07 stop; QHM re-placed it at 01:41 PT; it would have sold at the open).
- The only way to set NVDA's stop to the owner's level on 11-12 was a hand edit of data/state/quarterly_holds.json with
  the bot stopped (Claude, at the owner's direction, 05:2x-06:13 PT). That is not repeatable or auditable.

## Change
1. `scripts/qhm_owner_stop.py set <SYM> <PRICE> --until <YYYY-MM-DD>` / `clear <SYM>` / `show` writes
   data/state/qhm_owner_stops.json (atomic, locked, same pattern as scripts/qhm_owner_trim.py). It never talks to the
   broker. It refuses a symbol QHM does not hold, a non-positive price, and a date in the past.
2. QHM reads the file each cycle. While an override is active (today ET <= until):
   - `pos.stop_price` = the owner's price (logged once: "[change=qhm-ownerstop-1113] QHM <SYM> stop set by owner $X until
     <date> (was $Y)"), and the existing per-cycle reconcile moves the broker stop to it in place.
   - QHM's own stop writers do not change it. Measured on main (e5b6b291), 12 places assign `pos.stop_price`:
     - raise writers, skipped while an override is active (each logs "owner stop active"): `_maybe_trailing_lock`
       (two sites), `_stop_safe_add`'s raise for a new average, `_maybe_restop_after_add` (QHM-RESTOP-1112);
     - fresh-stop submitters, which use the owner's price instead of their own when it is below the live price:
       `_compute_and_submit_stop`, `_resubmit_post_earnings_stop`, `_earnings_trim_restore_stop`;
     - broker-adoption sites (owner-add stage x4, the reconcile's adopt-lower-broker-stop): unchanged; the
       per-cycle pass sets the record back to the owner's price and the reconcile moves the broker stop in place.
     One helper `_owner_stop(pos, live)` returns the active, below-market override price or None; every site above
     reads it, so the rule lives in one place.
   - Never through the market: if the owner's price is at or above the live price, QHM keeps its current stop, does not
     apply the override, and pages once per day ("ACTION <SYM>: your stop $X is above the price $P; not applied").
     The QHM-RESTOP-1112 reconcile guard (never move a QHM stop up through the market) stays in force.
3. On expiry (today ET > until) or `clear`: the override is removed, QHM recalculates its own stop once from the
   current average (its existing design-stop rule, kept below the market by the same gap margin as QHM-RESTOP-1112),
   and pages one HEADS-UP line with the old and new stop.
4. File unreadable: no override is applied that cycle (QHM's own stop stays) and one CRITICAL log line.
5. Every set, apply, refusal and expiry writes a trade_events.jsonl row (event qhm_owner_stop).

## Rev 2 — changes asked for by GAI and Gro (2026-11-13)
- File reads: the writer uses tmp + os.replace, so a reader sees the old or the new file, never a partial one; the
  read is lock-free as in `_maybe_owner_trim` (4599-4604). The unreadable-file CRITICAL line carries the exception.
- `show` prints, per symbol: the owner's price and date, QHM's recorded stop, and the broker's open QH stop (a
  read-only open-orders read through execution.broker's existing getter; mocked in tests). Run on the server
  before the first live set.
- Clear/expiry: QHM writes its recalculated stop to `pos.stop_price`, and the existing reconcile replaces the broker
  stop in place (8496-8506). No separate cancel is needed; the recalculated stop is held below the live price by the
  QHM-RESTOP-1112 gap margin.
- Broker rejection (price moved between the check and the replace): the reconcile's existing failure path keeps the
  current broker stop and marks the stop pending (8507-8512). The shares stay covered; the next cycle re-checks.
- Every trade_events row carries the broker stop order id. Tests cover expiry at the ET date boundary (23:59 vs 00:00).

## Rev 3 — changes asked for by the board seats Harris and Taleb (2026-11-13)
These supersede items 2-4 of "Change" where they differ.
- Gap margin on the owner's price (Harris 2): m = the QHM-RESTOP-1112 margin (median overnight gap x live, from
  config.MOMENTUM_LONG_LOOKBACK daily bars). The override applies only while owner price < live - m; otherwise it is
  not applied, the current stop stays, and one ACTION page per day says so with both prices.
- One live read per cycle (Harris 3): the owner pass and the reconcile use the same live price. When an override is
  active, the reconcile's adopt-lower-broker-stop HEADS-UP is worded "owner stop: the broker stop is $X".
- Expiry and clear never raise through the market (Harris 1, same rule as QHM-RESTOP-1112): QHM's design stop from
  the current average is used only if it is below live - m; otherwise the owner's last price stays as QHM's stop and
  QHM's own trail and restop rules take over from there. A HEADS-UP page the day before expiry, and one on expiry
  with the old and new stop.
- Unreadable or missing file (Harris 4): every symbol whose override was applied (persisted as
  `pos.owner_stop_until`) freezes: no recompute, the raise writers stay skipped, the record and broker stop are
  unchanged, plus one CRITICAL line with the exception and one ACTION page per day. The script never deletes the file
  (`clear` rewrites it), so a missing file is a fault, not a clear. The file is the source of truth; `pos` only
  marks that an override was applied.
- Clock (Harris 5): expiry uses QHM's `self._now_et()` date; the override survives restarts (file + `pos` marker).
- Horizon (Taleb 1): `--until` may not be later than the hold's own QHM review/exit date from its hold record (the
  build confirms the field name; if the record has none, the script refuses dates past the current quarter's end).
- Risk shown, not hidden (Taleb 4, 5): `set` prints the loss at the owner's stop in dollars and as a percent of
  equity (read-only account read). When that loss is larger than config.MAX_DAILY_LOSS_PCT x equity, `set` requires
  `--confirm-risk` and still accepts the stop (owner's bounded choice; no floor). The apply log, the page and the
  trade_events row state "owner stop: $X at risk (Y% of equity); QHM losses are outside the daily kill switch".
- Average changes after a set (Taleb 2): the override keeps applying (the owner sets an absolute price; averaging
  down under his stop is the owner's stated use), and QHM pages once with the new loss at the stop. `avg_at_set` is
  stored in the file for the audit trail.
- Fresh-stop submitters with the live price unreadable (Taleb 3): they place QHM's own stop as today; the owner pass
  re-applies the override on the next cycle with a readable price.
- Raise writers that skip because of an override also write a trade_events row (Harris nit). A dashboard "owner stop"
  flag is a follow-up (outside these files).
- Corrected answers: Q11 the stop width can widen risk per share by owner direction, so this was reviewed as
  risk-path with the masked-loss seat (Taleb). Q38 adds "an override still applied after its date". Q40's machine
  check: every cycle the broker QH stop equals the active override and is below live - m; a mismatch pages.

## Required questions
Q1: Lets the owner run QHM positions the way he chooses (average down, keep a wider or tighter stop) without the bot
undoing it; no change to what the bot buys.
Q2: No change in the number of trades.
Q3: Should exist (owner direction). Simplest form: the owner-trim pattern (a request file the bot reads).
Q4: Builds on scripts/qhm_owner_trim.py (same file pattern), QHM-RESTOP-1112 and QHM-OPENSTOP-1112; no open PR touches
these paths.
Q5: Not static: the stop number is the owner's own choice per symbol with an expiry date; the bot adds no threshold.
Q6: Static assumption exposed: "only the bot sets QHM stops". Forward build: the same override for Swing and F6 lots.
Q7: Feeds the QHM stop and the unmanaged-position alarm (a stop covering the shares keeps it quiet).
Q8: 11-12 NVDA orders and logs (01:41 PT re-placed stop, 06:13 PT hand edit, 07:02 PT QHM stop at $147.48).
Q9: Replay of 11-12: with an override of $147.48 set at 05:20 PT, the 01:41 stop would have been replaced by the
owner's price at the next cycle with no hand edit. Wrong if any QHM path moves an active override.
Q10: Not applicable because this is an owner control, not a signal.
Q11: No size or frequency change; the stop level of an existing hold is the owner's choice (risk per share can widen by
owner direction, bounded by the never-through-the-market rule).
Q12: A stop stays on every QHM share; the override never removes a stop and never places one at or above the market.
Q13: File unreadable -> no override that cycle; price above market -> not applied + page.
Q14: No: it acts from the first cycle after a set.
Q15: The [change=qhm-ownerstop-1113] log line and the trade_events row carry the owner's price, the old stop and the date.
Q16: Proof line: "QHM <SYM> stop set by owner $X until <date>" then the broker stop at $X.
Q17: The first set after deploy (read-only run of the script first; the bot applies it in its next cycle).
Q18: The 11-12 NVDA hand edit is the anomaly this replaces.
Q19: Changes how QHM exits are managed: owner approves this exact design.
Q20: Reviewer prompts carry the code and facts only.
Q21: Tests mock every broker call; the script never talks to the broker.
Q22: Read-only run of `show` on the server before the first live set.
Q23: Unit tests of set/apply/refuse/expire plus the read-only run.
Q24: Not applicable because no availability claim.
Q25: Not applicable because no market statistic is used beyond the existing gap margin.
Q26: T1 Alpaca positions/orders through QHM's existing calls; no new data source.
Q27: data/state/qhm_owner_stops.json (atomic tmp+replace with a lock) and QHM's existing state save.
Q28: In QHM's per-cycle pass on the trading thread, before the stop reconcile; one small file read per cycle.
Q29: P1: removes the need for hand edits of bot state, the riskiest manual step used on 11-12.
Q30: Blind spot: no sanctioned way for the owner to direct a stop the bot otherwise reverts.
Q31: Not applicable because nothing is fitted.
Q32: Not applicable because no returns replay.
Q33: Not applicable because no trades are added.
Q34: Not applicable because no new exposure.
Q35: Not applicable because no new exposure.
Q36: Not applicable because no sizing.
Q37: Not applicable because no statistic.
Q38: Reversal: any QHM path moving an active override, or an override placed through the market.
Q39: Not applicable because it sizes nothing.
Q40: Protects "the owner's stop decision holds": machine check = the broker's QH stop equals the active override each cycle.
Q41: Extend to Swing and F6 lots after this proves out.
Q42: Alpaca orders and positions; the owner's request file.
Q43: A discretionary override layer with an expiry, as portfolio managers use for systematic books.
Q44: The broker's order record after the first live set, read independently.
