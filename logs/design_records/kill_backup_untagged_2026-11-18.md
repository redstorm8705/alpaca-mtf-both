# KILL-BACKUP-UNTAGGED-1118 — the kill-switch backup never counts an owner's app buy in a QHM/F6 name as Day/Swing P&L

Approval-ID: KILL-BACKUP-UNTAGGED-1118
Covers: execution/risk_manager.py, tests/test_kill_backup_untagged_1118.py, tests/test_kill_backup_1111.py
Not changed: execution/ownership_guard.py, execution/quarterly_hold_manager.py, execution/manual_sale_proof.py, run_day_tier.py
Status: rev 10 (signed: Claude, 2026-11-24; the board's rev-10 changes C1-C5 folded in). Rev 10: cold review of the rev-9 build. Reads were recorded only
during backup episodes, so "the last read before today's PT midnight" could be days old or taken before a later QHM/F6
buy (whole-hold owner sale then counted +$25; repro run against the real function). A close-time test for the carried
entry was tried and measured: the ledger stamp is the newest fill, which lands before the 16:00 ET close on 4 of 4
recent sessions, so evidence would be "missing" nearly every day and every untagged fill (the bot's own bracket legs
too) would give no measure. Rev 10 changes only when reads are recorded and what counts as the carried evidence:
- Every kill check (exact path and backup path, in both processes that run it: the main bot's run_cycle and the Day
  tier's 2-minute cron, run_day_tier.py) records one ledger file read into data/state/kill_backup_reads.json: the
  claims, the RAW fills_through_utc stamp, the held QHM/F6 set, and the two taint flags (unkeyed_edit_since_stamp and
  the credited_since_stamp order ids). Never the last-good fallback. The read-modify-write holds an fcntl lock on the
  file, so the two processes never lose each other's entries; the write is atomic (tmp -> replace) and only when the
  entry changed.
- The carried evidence X (board rev-10 C1), recomputed on every check against the current fill list (never frozen in the
  file): the earliest file read of today whose raw stamp is present and before PT midnight, strictly earlier than the
  first fill in today's current fill list, with unkeyed_edit_since_stamp false and no credited order id among today's
  fill order ids. Such a read equals the prior session's end state whenever it was taken.
- X alone decides whether evidence is complete (C3). The last read before PT midnight (Y) only adds symbols to the
  scope. If X is missing, the rev-9 rule applies (every untagged-fill symbol in scope) even when Y exists.
- No calendar read: the rejected close-time test and its /v2/calendar request are removed.
- The same-batch rule for bot legs subtracts same-batch tagged Day/Swing fills before the coverage check.
- One line per PT day: X's stamp, its taint flags and found yes/no.
- Before ship (Thorp): a read-only OCI measurement over the last 20 sessions of how often X would have existed (first
  kill-check time, the ledger stamp and taint flags then, the first fill time). Reversal: X missing on more than half of
  sessions, or any masking test non-zero.
- Tests (C5): an unkeyed owner-sale heal before the first read -> missing, never +$25; a credited today fill -> X
  tainted; a read stamped after the premarket fill -> missing; a 5-day-old Y with no X -> missing; after-hours QHM/F6
  fills seen only in X -> in scope; two writers racing -> no lost held symbol; a corrupt file mid-day -> missing for the
  rest of the day.
- Q28 becomes: check_kill_switch on the trading thread; reads recorded on every check, attribution only in the backup
  path.
Prior: rev 9 (signed: Claude, 2026-11-18). Rev 9: board rev-8 input — missing scope evidence (no reads file, e.g.
ship day, or an unreadable file) must mean unknown, not never held; Codex rev-8 input — an owner sale larger than the
protected holding (sell 10 with QHM 5) opens a short no protected tier can own: quantity cap added; board rev-9
input — a co-hold bracket leg (DT parent) that fills after Day already exited sold QHM shares (+$24): bot legs now
need Day/Swing coverage at the fill.
Prior: rev 8 (signed: Claude, 2026-11-18). Rev 8: board rev-7 input — no VALID read can exist before the account's
first fill of the day, so a whole-hold owner sale that is the day's first fill fell out of scope (+$25); rev 8 decides
scope from every successful ledger read (any stamp age) plus the last read before PT midnight.
Prior: rev 7 (signed: Claude, 2026-11-18). Rev 7: board rev-6 input — an owner app sale of a WHOLE QHM hold before
the checkpoint removed the symbol's QHM claim, so the symbol fell out of scope and the sale counted as a +$25
Day/Swing gain; rev 7 widens the scope to any QHM/F6 holding seen today.
Prior: rev 6 (signed: Claude, 2026-11-18). Rev 6: board rev-5 input — an owner app buy the rebuild booked as
intraday (holds overlay not caught up) was left out of the fill terms while its shares stayed in the Day/Swing claim
(+$12 masked); rev 6 adds a consistency check and stores only valid-checkpoint reads.
Prior: rev 5 (signed: Claude, 2026-11-18). Rev 5: board (Thorp + Taleb) rev-4 input — an owner app sale BEFORE the
checkpoint in a symbol Swing and QHM both hold was judged against a ledger written after it (sale already netted in),
hiding a real -$30 Swing loss; and an untagged bot order must prove a Day/Swing parent.
Prior: rev 4 (signed: Claude, 2026-11-18). Rev 4: GAI round 3 — an owner app SALE in a symbol where Day/Swing also
holds shares cannot be attributed; rev 4 leaves an owner order out only when Day/Swing holds none of that symbol.
Prior: rev 3 (signed: Claude, 2026-11-18). Rev 3: GAI and the masked-loss seat asked for untagged SELLs too (an owner
app sale of QHM shares, +$12.54 masked in the seat's NVDA example); Thorp noted rev 2 also blocks the bot's own
untagged cover legs. Rev 3 decides every untagged fill by Alpaca's own order `source` field instead.
Prior: rev 2 (signed: Claude, 2026-11-18). Design only. Rev 2: board (Thorp + Taleb) rejected Option B as written —
an overnight Day short covered by its untagged OCO stop leg, then a restart and a rebuild with Day/Swing claim 0, would
hide a real -$18 loss. Rev 2 adopts the masked-loss seat's order-identity variant (B-id) and narrows the scope to
untagged BUYS before the checkpoint.

## Problem (verified at source 2026-11-18; GitHub #990, post-live audit of PR #951)
- Server log 2026-11-12 21:03:34 UTC: `[change=kill-backup-1111] kill-switch backup measure B=-10.42` with terms
  `{'symbol': 'NVDA', 'a': 0.0, 'p': 5.0, ...}` and `{'symbol': 'NVDA', 'fills': 28.86}`.
- The $28.86 is the owner's app buy of NVDA (order 1bd07fea, 2 sh @ 159.30, 13:20 UTC, no tier tag). The ledger
  rebuild that set the checkpoint had already placed those shares in QHM (p=5, a=0).
- Code (`_backup_trading_pnl`, risk_manager.py on main ~lines 752-767 and 800-818): the untagged-fill test runs only
  on fills AFTER the checkpoint; the fill-term loop then counts every fill not tagged qhm/forever6 — the untagged
  pre-checkpoint buy included — as Day/Swing, repriced to the prior close.
- Effect: Day+Swing read $28.86 better than it was (about -39.28 vs -10.42). No block was missed that day (7% of
  equity is about $175), but this is a safety control reporting a smaller loss than the real one (never-mask-a-loss).

## Change (rev 3, decided by Alpaca's order `source`)
- Fact used (measured at source): Alpaca stamps every order's raw REST JSON with `source`. Orders sent with the API key
  (the bot, every tier and script, and their bracket/OCO legs) carry `"access_key"`; orders placed in the Alpaca
  app/web carry `null` (execution/manual_sale_proof.py, measured 2026-10-17: 3,092 of 3,092 and 48 of 48). Rechecked
  2026-11-18: order 1bd07fea (the 11-12 NVDA app buy) has `source` null; every filled bracket/OCO leg since 11-10 has
  `"access_key"` under a tagged Day parent; 69 of 72 closed orders since 11-10 are `"access_key"`, 3 null.
- Scope: every untagged fill today (before or after the ledger checkpoint) in a symbol that held QHM/F6 shares at any
  point today: in the checkpoint claim, in the protected journal, or in ANY successful ledger file read of the day or
  the last one before today's PT midnight, whatever its stamp's age (kept in `data/state/kill_backup_reads.json` as a
  per-symbol "held QHM/F6" set; a whole-hold sale cannot remove its own evidence). Validity (today's stamp, no
  unkeyed edit, never the last-good fallback) still applies to the reads used for the Day/Swing position and the
  consistency check below.
  Quantity cap: a source-null SELL is left out only if its filled quantity is no larger than the protected (QHM/F6)
  long position immediately before it (the pre-fill read's protected claim, plus protected-tagged fills, plus every
  source-null fill already left out and booked to p, from the read's stamp up to this fill); a larger sale, or any source-null fill that would leave the protected position negative, gives no measure.
  Partial fills of one order are summed per order and checked against the same pre-fill protected position.
  Ordering: fills of one symbol are processed in broker transaction-time order; fills with the SAME transaction time
  form one batch, judged together against the position before the batch (all source-null sells in the batch summed
  with same-time protected reductions for the cap; all access_key legs summed for coverage). A fill with a missing or
  unparseable transaction time: no measure.
  Missing scope evidence means unknown: if `kill_backup_reads.json` is missing, unreadable or invalid, or has no
  carried pre-midnight entry, every symbol with an untagged fill today is in scope (a source-null fill with no valid
  read before it then gives no measure), with one `[change=kill-backup-untagged-1118]` line per PT day naming the
  fallback. The file is seeded from the current ledger at deploy, so ship day is not a gap. The backup reads those fills' orders once (raw /v2/orders, bounded, the manual_sale_proof
  read pattern; only when such a fill exists).
- `source` null (the owner's app order, buy or sell): left out of the Day/Swing fill terms (and, after the checkpoint,
  booked to the protected journal p) ONLY when the symbol's Day/Swing position is zero at that fill, measured from a
  ledger read stamped BEFORE the fill: each successful ledger read's per-symbol Day/Swing claim and its
  `fills_through_utc` stamp are kept on disk for the PT day (`data/state/kill_backup_reads.json`, atomic write); the
  position at the fill = the newest such read stamped before the fill + every Day/Swing fill of the symbol from Alpaca
  between that stamp and the fill (tagged Day/Swing fills plus untagged `access_key` fills proven Day/Swing as below).
  No read stamped before the fill (for example the first read of the day came after it): no measure.
  For a source-null fill BEFORE the checkpoint, also: the checkpoint's Day/Swing claim for the symbol must equal the
  Day/Swing position from that pre-fill read plus every Day/Swing fill between the read's stamp and the checkpoint
  stamp. If they differ, the ledger has booked a non-Day/Swing fill into Day/Swing (its shares sit in `a`): no measure.
  Only reads `_ledger_checkpoint` accepts as a valid checkpoint are stored (stamp from today,
  `unkeyed_edit_since_stamp` not set, never the last-good fallback). If Day/Swing holds any shares of that symbol (long or short), the
  owner order cannot be attributed (it may have sold Day/Swing shares): no measure (fail closed: new entries wait),
  term `{"symbol", "order_id", "owner_order_with_day_swing_position": true}`.
- `source` "access_key" with no tier tag: Day/Swing (as today: before the checkpoint fill terms, after it the active
  journal) ONLY when its parent order is tagged Day/Swing — read from the nested order list (the parent's `legs`
  carry the fill's order id), the same read `_tier_for_fill_order` already makes. A bot order with no tag and no
  tagged Day/Swing parent (for example a remainder stop, or an untagged QHM/F6 path): no measure.
  Coverage: such a leg counts as Day/Swing only if Day/Swing held the shares at the fill — a sell leg needs a Day/Swing
  long of at least its quantity, a buy leg a Day/Swing short of at least its quantity — measured like the owner rule
  (newest valid read before the fill plus Day/Swing fills up to it). No valid read before the fill, or a leg larger than
  the Day/Swing position: no measure. This replaces today's after-checkpoint
  "untagged fill on protected -> no measure", so the bot's own cover legs no longer stop the measure.
- `source` missing, unreadable, or the order read fails or times out: no measure (fail closed: new entries wait), with
  the term `{"symbol", "order_id", "untagged_source_unknown": true}`.
- Log: `[change=kill-backup-untagged-1118]` with symbol, order id, side, source and the rule that decided.
- Effect on 11-12 (checked against Alpaca fills): the NVDA app buy at 13:20:59 UTC was the account's first fill of the
  day, so no valid read existed before it: no measure (new entries wait until the exact measure works) instead of the
  masked -10.42. On a day with a valid read before the owner order, the order is left out and the reading is exact.
  An owner buy placed with the owner-buy command (tagged qhm) never reaches this rule.
- Out of scope (follow-up): an owner app order in a symbol with no QHM/F6 shares (board Q30).

## Required questions
Q1: Removes masked losses (owner app buys and sales) from the only daily risk brake and keeps the measure working.
Q2: Entries also wait when an owner app order in a QHM/F6 name is the account's first fill of the day (no valid read
yet); otherwise no reduction in normal operation; it removes today's after-checkpoint block on the bot's own untagged legs.
Entries wait only when Alpaca's order read fails for such a fill, or the owner trades from the app a symbol the
Day/Swing tier holds at that moment.
Q3: Should exist: a safety control currently under-reports a loss. B is the get-to-yes form.
Q4: Follow-up to KILL-BACKUP-1111 (PR 951); KILL-EXIT-JOIN-1117 (PR 988) touched the exact join, not this function.
Q5: Not applicable because no threshold is introduced.
Q6: Forward: tag owner app orders at the source (an owner-buy record) so the measure never has to infer them.
Q7: Kill switch only.
Q8: Measured: the 11-12 log line and order 1bd07fea above.
Q9: Replay 11-12 from the server log terms, fills and the real order `source` values: no measure (the app buy was the
day's first fill; no valid read before it), never -10.42. Unit tests: the board's overnight-short / OCO-cover / restart case (bot leg, counted, -18.00); the
seat's NVDA app sale with no Day/Swing shares (left out, no +12.54); GAI's app sale of a symbol Day/Swing holds (no
measure); unreadable source (no measure); the board's pre-checkpoint app sale of shared NVDA (Swing 3 + QHM 5; read before the
sale shows Swing 3 -> no measure); no read stamped before the fill (no measure); an untagged access_key order with no
tagged parent (no measure); the board's rev-5 input (owner buy booked intraday before the holds overlay caught up:
checkpoint Day/Swing 2 vs expected 0 -> no measure); the board's rev-6 input (owner app sale of all 5 QHM NVDA
before the checkpoint, read shows QHM 5 -> in scope, left out, reading unchanged); the board's rev-7 input (whole-hold owner sale
as the day's first fill: yesterday's last read shows QHM 5 -> in scope; no valid read before it -> no measure); the
board's rev-8 input (no reads file, pre-market whole-hold owner sale: every untagged-fill symbol in scope -> no measure); Codex's rev-8 input (owner app sale of 10 NVDA with QHM 5 ->
no measure); a partial-fill owner sale split across fills; a multi-fill owner sale within the holding (left out);
the board's co-hold bracket race (TP leg fills after the tagged Day exit, before and after the checkpoint: no measure,
never +$24); two after-checkpoint owner app sales of 5 each with QHM 5 (the second gives no measure, never 0 on an
unowned short); Codex's rev-9 input (two owner sales of 5 with the same fill timestamp and QHM 5 -> batch of 10 > 5
-> no measure).
Q10: Not applicable because this is a safety-control correction, not a trading edge.
Q11: No size or exposure change.
Q12: Touches the 7% kill switch: both options remove a masked loss; neither widens the envelope.
Q13: Ledger, journal, read history or order read unavailable: no measure (fail closed); the read history is on disk.
Q14: Not a shadow: it changes the live kill-switch backup reading.
Q15: Each decision logs symbol, order id and rule.
Q16: Proof: the next day with an untagged buy in a QHM/F6 name logs the rule line and no `fills` term for it.
Q17: The next owner app add to a QHM hold.
Q18: The anomaly closed: a backup reading better than the real Day+Swing loss.
Q19: Kill switch: owner approves the exact design; board with the masked-loss seat, GAI and reviewer #2.
Q20: Neutral prompts; tests included.
Q21: Tests mock the broker and ledger.
Q22: The replay of 11-12 runs read-only on the server's real log terms before it ships.
Q23: The 11-12 replay plus unit tests for each condition of B and the SELL case.
Q24: Checked at source this turn: the Alpaca `source` field on order 1bd07fea (null) and on every filled leg since 11-10.
Q25: Not applicable because no market statistic is used.
Q26: T1 Alpaca fills, positions and raw /v2/orders (source field); the ownership ledger file.
Q27: data/state/kill_backup_reads.json (atomic tmp->replace), one entry per ledger read per PT day; reset each PT day.
Q28: check_kill_switch on the trading thread; a ledger read is recorded on every check (local file), attribution runs only in the backup path.
Q29: Safety control defect; ranks high.
Q30: Blind spot: untagged orders inferred by time window instead of by source.
Q31: Not applicable because nothing is fitted.
Q32: The 11-12 replay uses the real logged terms and fills; no look-ahead (each fill is judged only on reads before it).
Q33: Not applicable because it places no trades.
Q34: Not applicable because it has no return.
Q35: Regime-independent: attribution by order identity and ledger reads, not by market state.
Q36: Not applicable because it trades nothing.
Q37: Not applicable because it is not a trading edge.
Q38: Not applicable because it is not a trading edge.
Q39: No sizing change; the P&L it measures comes from Alpaca fills.
Q40: Protects the 7% check's reading; check: no fill whose order `source` is null enters Day/Swing (unit tests over every
branch), and the 11-12 replay never reports -10.42.
Q41: Tag owner orders at the source (Q6).
Q42: Alpaca fills and the ledger claims.
Q43: Attribution by order identity (the broker's own source stamp), falling back to fail-closed when unknown.
Q44: The replay and the next live owner add.
