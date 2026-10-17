# QHM owner-sale auto-heal (Part B of manual_sell_alerts_2026-10-16.md) — 2026-10-17, Claude

**Owner directive (2026-10-17):** "When I sell, the bot needs safeguards to detect and auto heal." Build it dynamic:
the bot must PROVE the sale was the owner's from broker data, then heal itself, instead of paging a confirm command.

## The new signal (measured 2026-10-17, all 3,140 account orders since inception, raw `/v2/orders`)
Alpaca's raw order object carries `source`. Every order sent through the API key (the bot, every tier, every
script) has `source = "access_key"`: 3,092 of 3,092. Every order placed in the Alpaca app/web has `source = null`
and a random-UUID client_order_id: 48 of 48 (LLY 10/14 and 10/16 sales, the 8/26 LLY buy, the UBER stop filled
10/13, ...). Zero API orders with a null source. The alpaca-py SDK model (0.43.3) DROPS this field, so it must be read
from the raw REST JSON (as `reporting/pnl_ledger.py` already does).

So "a human logged into the account placed this sell" is provable from two independent signals: `source != access_key`
AND the client_order_id carries no bot tier tag (`ownership_guard.tier_of_coid` is None, no `mtf-` prefix). The
never-shrink guard's premise (2026-08-08: "a real manual close and a breach are automation-indistinguishable") no
longer holds for this case: a bot breach always arrives through the API key, so it can never satisfy the proof.

## Root cause being fixed (verified 2026-10-16 by running the real code with mocks)
After a sale that leaves a QHM hold flat, `_reconcile_stop_coverage` calls `_strict_resync_qty` → None (broker 404)
→ pages "stop coverage unresolved (ownership proof unavailable)", returns (True, True); the main loop then skips the
symbol, so `_detect_external_close` never runs. Result: an endless page every cycle, the hold never closes, and the
QHM own-stop-out auto-heal (`qhm_stop_fill`) is also unreachable (since #596, 2026-10-11).

## Design
1. **Flat → let the close detector run.** In `_reconcile_stop_coverage`, before `_strict_resync_qty`: if
   `broker.get_position` returns None (it returns None ONLY on a genuine 404; any API error raises) and
   `pos.qty_filled > 0`, return (True, False) without paging. A raised read keeps today's fail-closed page.
2. **Owner-sale proof** (new pure module `execution/manual_sale_proof.py`): given the symbol's closed orders filled
   after `pos.qty_proven_at` (new persisted field, set only when `_strict_resync_qty` succeeds — the last moment the
   broker provably held the hold's shares), the ledger's QHM qty N and the broker's qty M (0 ≤ M < N): proven iff
   every order with filled_qty > 0 in that window is a SELL, carries a `source` key whose value is not
   `"access_key"`, has no bot tier tag, and the filled quantities sum to exactly N − M. Anything else — a buy in the
   window, any API-sourced fill, a missing `source` key, a qty mismatch, an unreadable order list, no
   `qty_proven_at` — is NOT proven (fail-closed to today's behaviour + the confirm command).
   The order read is one bounded REST call (`/v2/orders?status=closed&symbols=X&after=T`, 2 tries, short timeout).
3. **Flat + proven** (`_detect_external_close`, non-stop branch): close the hold, heal the ledger through the
   existing `apply_authorized_tier_reduction(sym, "qhm", 0, "owner_app_sale")` (pure-QHM guarded; co-held → refused →
   confirm command remains), cancel QHM's own stop by exact id if it is still open, one plain-English message.
4. **Partial sale + proven** (`_reconcile_stop_coverage`, when the strict resync fails with a long broker position
   smaller than the ledger's QHM qty): heal the ledger to M through the same channel, re-prove, then let the existing
   stop logic resize or re-place the QHM stop for the remaining M shares. One plain-English message.
5. **Not proven:** unchanged safety — the hold is closed (flat) or held in PENDING_STOP_REPLACE (partial), and the
   message names the exact `confirm_ledger_heal.py` command and says "if you did NOT sell, do not confirm".
6. **Dedupe:** `_stop_coverage_pending` pages once per (symbol, reason) per ET day (persisted `coverage_page_key`);
   the state change and the log line stay every cycle. Plain-English text.

## Rule B/E screen (size / frequency / concurrency)
Zero on all three: no new entry, no size change, no exposure change. It changes when the ledger's protected floor
is lowered (only to the broker-verified quantity after a proven owner sale) and stops a page loop. The never-shrink
guard still refuses every unproven reduction. Board seats: reliability + masked-loss.

## Replay (front-loaded)
- LLY 2026-10-16: 1 QHM share, sold 11:07 PT by app (`source` null, UUID coid, qty 1) → proven → would have closed
  the hold and healed the ledger with no page.
- LLY 2026-10-14 after-hours app sale (2 → 1): proven partial → heal to 1 + stop resize.
- Counter-cases: the GE 2026-09-15 QHM stop-out (QH- coid, access_key) → not an owner sale → existing stop-out heal;
  the 2026-07-02 run_movers.py flatten of NVDA/GOOGL (API key) → NOT proven → stays frozen + confirm command.

## Reversal criterion
Any auto-heal whose triggering sell turns out not to be the owner's, or any API-sourced order observed with a null
`source`, reverts this to confirm-only.

## Review round 1 (2026-10-17) — changes adopted
Board: Taleb seat (masked loss) APPROVE-WITH-CHANGES; Peterffy seat (reliability) APPROVE-WITH-CHANGES. Gro
APPROVE-WITH-CHANGES; GAI REJECT (asked for a fresh broker read right before the ledger write and a restamped window).
Adopted:
- Proof accepts ONLY the exact app pattern: `source` key present with JSON null AND a random-UUID client_order_id.
  Any other source value (API key, a broker liquidation, another app, an unknown value) is not proven. Tripwire: a
  bot-tagged order with a null source disables the proof for that check (CRITICAL log).
- Window on FILL time: orders are read from the hold's creation time (`status=all`, one page) and only fills after
  `qty_proven_at` count; a full page is not proven. `qty_proven_at` is stamped BEFORE the broker read.
- Fresh broker read immediately before every ledger write; a moved position aborts (next cycle re-evaluates).
- Every flat close (proven or not) cancels QHM's own open sell orders on the symbol; a failed cancel or unreadable
  order book pages. The flat hand-off returns (False, False) so the stray-sell guard still runs on the symbol.
- Startup: an ACTIVE hold found flat at startup is closed at startup (parity with the RTH loop).
- Page dedupe = a per-ET-day SET of reasons; cleared whenever stop coverage is restored, so a later loss pages again.
- The order read is one 5-second request with no retry; a not-proven result is cached per (symbol, record qty, broker
  qty, window) so the trading thread does not re-query every cycle.
- Rollback safety: `HoldPosition.from_dict` ignores unknown keys; reversal is the `_OWNER_SALE_AUTOHEAL_ENABLED`
  constant, not a revert.
- Wording: "sold in the Alpaca app/website, not by the bot" (the bot cannot tell the owner from someone else logged
  into his account).
Not adopted (with reason):
- Gro "a client can send an order with source omitted": `source` is assigned by Alpaca server-side; the bot's SDK
  request object has no `source` field and 3,092/3,092 API orders carry `access_key`. The tripwire covers drift.
- Gro "sync_ledger rolls the heal back": sync compares the rebuild to the PERSISTED ledger, which the heal already
  lowered; a lower baseline is never raised by the never-shrink check, and the QHM overlay is clamped to broker net.
- GAI "atomic ledger+stop transaction": not possible across broker and ledger; the design converges instead — each
  cycle re-derives from broker truth, the write is guarded by a fresh read, and the stop logic resizes to the proven
  quantity on the same cycle.
Deferred: placing a QH stop for broker qty M when a partial sale is NOT proven (Taleb rec, needs its own vote);
ledger_sync auto-accepting a proven owner sale so its own confirm page never fires (follow-up, same proof module).

## Review round 2 (2026-10-17) — GAI changes adopted
- **Stop first, then the record.** Partial sale: if QHM's single resting stop covers more shares than the broker now
  holds, it is resized in place to the held quantity (Alpaca PATCH; the old stop stays live until swapped) and the
  resize is confirmed BEFORE the ledger is lowered; an unconfirmed resize or more than one QHM sell order leaves the
  ledger untouched. Flat: QHM's own sell orders are cancelled before the ledger heal. A crash between the two steps
  leaves a stop no larger than the position and a ledger no lower than the broker — the next cycle converges.
- **Live-signal check in the same response.** The proof also requires at least one bot-tagged order in the fetched
  window (the hold's own entry buy or stop is always there) to show `source == "access_key"`. If Alpaca ever stops
  stamping API orders, the proof fails closed on that very check, before any bad heal — not only after one.
- (Already in round 1) a null-source order must also carry a random-UUID client_order_id and no bot tag.
