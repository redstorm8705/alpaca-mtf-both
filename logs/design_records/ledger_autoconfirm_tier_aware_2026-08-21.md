# TIER-AWARE LEDGER AUTO-CONFIRM + REAL-TIME FIFO — root build (Rafael mandate 2026-08-21)
**Rafael:** "I don't want them silenced, I want to fix WHY the slack messages are being paranoid and BUILD, not
just fix. Tracker glitch? Unacceptable and needs to not just get a bandaid patch." → root fix, not silence.

## THE TWO FAILURES (one shared root), grounded at source
Trigger case: a QHM protected hold (LLY) whose GTC protective stop fires — a LEGITIMATE close (+$40.88 booked,
Alpaca-verified). It produces two wrong behaviours:

### Problem 1 — guard paranoia (`execution/ownership_guard.py` sync_ledger L847-969)
- `sync_ledger` REFUSES any protected-floor shrink (`new_q < old_q - EPS`, L941) unless operator-confirmed;
  all-or-nothing (one refused symbol blocks the whole write). Emits "protected-floor shrink — awaiting operator
  confirmation" (L952-953).
- **KEY:** the LLY stop-sell fill is ALREADY coid-tagged `QH-LLY-s-…` (qhm), and sync_ledger ALREADY receives
  `fills` + the coid map (L685-687). The replay ALREADY attributes the −1 to qhm → rebuild qhm=0. **The only
  gap:** nothing LOWERS the baseline for a plain stop-fire (only the earnings-trim path calls
  `apply_authorized_tier_reduction`). So baseline qhm=1 vs rebuild qhm=0 → REFUSED, and it re-refuses every
  cycle forever. It is NOT missing tier-awareness — it is missing an AUTONOMOUS auto-confirm of a
  positively-verified tier-owned stop-close.
- **Spam:** `run_ledger_sync.py` L197-200 — the ":rotating_light: ledger STALE" alert has NO dedup/backoff;
  fires every ~15-30 min once streak≥3, forever, until a clean heal. (The detailed msg IS deduped to count==1.)

### Problem 2 — phantom short (`execution/fifo_pnl.py` _fifo_reconstruct L295-314)
- The real-time FIFO EXCLUDES QHM (purges lots L179-180, skips fills L188-189). When LLY closes, the QHM
  manager transitions it OUT of `get_quarterly_hold_symbols()` → the intraday FIFO stops skipping LLY → but LLY
  has no intraday long lots → the closing `sell` hits the "no open long lots" branch → **synthetic short +
  false `FIFO CRITICAL` Slack + mis-signed P&L** (L298-314). The nightly ledger (#151) re-attributes this
  correctly; the real-time FIFO never got the same tier-awareness.

**Shared root:** a legitimate tier-owned protective close is not RECOGNIZED as such in the REAL-TIME paths,
even though the closing fill already carries the tier tag (`QH-`/`F6-` client_order_id) and the order is a
filled protective STOP. Both fixes key off that same signal.

## DESIGN (risk-path — fail-closed, positive-verification-only)
**1. sync_ledger auto-confirm (ownership_guard.py).** On a detected protected-floor shrink, BEFORE recording it
as REFUSE-worthy, positively verify the shrink is EXACTLY covered by tier-owned protective-stop sells, then
synthesize a `confirmed_by="gtc_stop_auto"` confirmation consumed by the EXISTING L880-926 override machinery
(inherits the JOINT-PROTECTED-SUM co-held guard). **Auto-confirm ONLY iff ALL hold; absence of match →
existing REFUSE+escalate (never default-heal):**
   (a) `positions_settled` (the L880 gate); (b) there exist SELL fills on `sym` whose order coid `tier_of_coid
   == pt` (the shrinking protected tier sold its OWN shares); (c) those orders are filled protective STOPs
   (order `type` contains "stop") — requires passing a new `order_meta_by_id` map built from the `orders` list
   `run_ledger_sync` already fetches (no new network I/O); (d) summed tier-owned stop-sell qty == `(old_q-new_q)`
   within EPS (no residual unexplained reduction); (e) post-heal `net == ledger_sum` + JOINT-PROTECTED-SUM
   holds; (f) co-held symbol → REFUSE→operator fallback preserved (the pure-tier guard). Distinguishes: (A)
   tier-owned stop SELL covers shrink → auto-confirm; (B) aged-out BUY artifact, no matching sell → REFUSE;
   (C) non-owner/untagged sell of protected shares (BREACH) → no tier-owned-stop match → REFUSE+escalate.
   Template: `apply_authorized_tier_reduction` (the earnings-trim path; broker-verified net, pure-tier guard,
   atomic write). NOTE: `record_system_heal_confirmation` does NOT exist — superseded by that direct path.

**2. fifo_pnl tier-aware plain-sell (fifo_pnl.py).** In the plain-`sell`-no-long-lots branch (L295-314), before
recording a synthetic short, check the fill's order coid tier-tag; if it is a protected-tier (`QH-`/`F6-`)
close, attribute it as a tier close (no synthetic short, no false CRITICAL, correct P&L sign). Only a truly
untagged/unattributable long-close fires the CRITICAL (the genuine corruption case it exists for).

**3. run_ledger_sync STALE-alert dedup/backoff (secondary).** Add exponential backoff / already-alerted stamp
so the STALE repeat can't spam every cycle. The auto-confirm makes it fire far less; this bounds the rest.

## RISK / GATE
RISK-PATH: **YES, squarely** — never-sell-floor chokepoint; the heal-down direction is irreversible and could
launder a breach / erase a floor if wrong (two prior fully-auto designs were board-rejected). Requires the
MANDATORY cold board + masked-loss/risk-asymmetry seat + Gro + GAI on BOTH the design AND the diff. NOT eligible
for the same-day all-zero path. RC-1/3/5 all PASS in the current module — new code must match (tz-aware, log
every except, atomic tmp→fsync→replace).

## OPEN DESIGN QUESTION (for the BGG risk-gate)
Is requiring the STOP order-type (c) NECESSARY, or does the coid-tier-tag (b) alone suffice safely? Tag alone is
simpler (no order_meta map) but would also auto-confirm a MANUAL tier-tagged sell; requiring "filled stop"
narrows auto-confirm to the bot's own protective mechanism only. Lean: require the stop-type (safer; a manual
close should still route to operator). BGG to confirm.

## BGG RISK-GATE CONSENSUS (2026-08-21) — REVISE-then-ship (hardening, not rejection)
Gro + GAI + masked-loss board seat (Taleb/Thorp). Data-integrity seat died on a usage limit; GAI covered its
scope (pagination/skew/collision/coercion). All voices converge; the auto-confirm is fail-closed in STRUCTURE
but MUST incorporate these before ship:

1. **REVERSIBILITY (keystone — masked-loss seat).** The auto-heal must NOT be irreversible+silent. A
   false-REFUSE costs ~30s of operator time; a false-AUTO-CONFIRM permanently erases a protected floor + masks
   a loss (unbounded, absorbing). A stronger conjunction lowers the ODDS, not the COST. Therefore: on
   auto-confirm, (a) append an immutable JOURNAL entry {sym, tier, old_q, new_q, matched fill_ids+qtys, coid
   decode, order type/status, reason="gtc_stop_auto"}; (b) fire ONE mandatory operator notification per
   auto-heal (this REPLACES the 20-min paranoid spam — accurate, one-time, actionable); (c) keep a bounded
   reversal window so a wrong auto-confirm is recoverable. Absent reversibility, the heal-down stays
   operator-gated. [Taleb: buy reversibility on an irreversible act; Thorp: never automate an absorbing-state
   loss to save a trivial manual step. NOT a profitable>perfect case — never-mask-a-loss carve-out.]
2. **CONSUMED-ONCE, DAY/SETTLEMENT-BOUNDED fill matching (GAI + masked-loss seat).** Match SPECIFIC fill_ids,
   mark each consumed, never re-sum; bound the fills window to fills after the last reconciled sync
   (`fill.timestamp > last_reconciled_sync_ts`). Kills the RC-4 class (`submitted_after=None` — a stale prior
   stop-fill re-matched against a NEW shrink whose true cause was a non-stop sell → floor laundered).
3. **STOP-TYPE + TERMINAL FILLED mandatory (Gro + GAI).** Clause (c) confirmed ESSENTIAL: require order
   `type ∈ {stop, stop_limit, stop_loss}` AND status strictly `FILLED` (not cancelled-with-partial). The
   coid-tier-tag ALONE would auto-confirm a manual tier-tagged sell — resolves the open design question: the
   answer is NO, tag alone does not suffice.
4. **ALL AMBIGUITY → REFUSE, pinned (masked-loss seat).** fetch-empty/error → REFUSE (empty ≠ clean book, the
   `{}`-conflation bug); coid decode collision / low-confidence → REFUSE (positive tier proof, never inference
   — realistic given the documented UNTAGGED shared QHM/intraday lots, the Movers-collision root); co-held
   UNKNOWN → REFUSE (require positive sole-ownership, not absence of a found co-holder); EPS soft-band → REFUSE.
5. **EXACT SHARE MATCH (Gro + GAI).** Only filled-STOP tier sells count toward the shrink; their qty must EQUAL
   (old_q − new_q) EXACTLY at instrument share granularity (`Decimal`, not float `math.isclose`). A mixed
   reduction (stop-close + a non-stop sell) → stop-fills < shrink → REFUSE.

The REVERSIBILITY change also upgrades P3: the "STALE" spam is REPLACED by the one-time reversible-auto-heal
notification, not merely deduped.

## PHASING
P1 (P&L-path, lower-risk): fifo_pnl tier-aware plain-sell — stops the phantom short + mis-signed P&L + false
CRITICAL. P2 (risk-path core): sync_ledger auto-confirm. P3 (alerting): STALE→one-time reversible notification.
Each its own gated diff; P2 carries the full masked-loss board gate.

### P1 IMPLEMENTATION PLAN (scoped at source)
ROOT: `fifo_pnl._fifo_reconstruct` skips QHM fills by CURRENT symbol membership (`get_quarterly_hold_symbols()`,
L176-189). LLY drops from that list the moment its stop closes it → the closing sell is no longer skipped →
"no long lots" branch → synthetic short (L295-314). Fix: skip protected-tier fills by their COID TAG, not by
current list membership — a fill whose order is tagged `QH-`/`F6-` is a protected-tier fill regardless of
current membership, so the timing race disappears.
- Alpaca FILL activities carry only `order_id` (NOT client_order_id — pnl_ledger L188). Join
  `fill.order_id → order.client_order_id → tier_of_coid()` (helpers exist: `build_coid_map` pnl_ledger L244,
  `tier_of_coid` ownership_guard L102).
- `_fifo_reconstruct(fills, prior_lots, processed_fill_ids)` gains an optional `coid_by_order_id` param
  (default {} → identical to today's behaviour = safe no-op when absent). In the loop, before the
  symbol-QHM-skip, ALSO skip a fill whose `tier_of_coid(coid_by_order_id.get(fill.order_id))` is protected.
  The plain-sell-no-lots synthetic-short branch (L295-314) is then only reached by a TRULY untagged
  long-close (the genuine-corruption case it exists for).
- Caller `portfolio_tracker.write_eod_summary` (L694) builds the coid map and passes it. **BLOCKING-CALL
  GUARD (cold-2nd class):** `write_eod_summary` runs on SIGTERM/heartbeat/AH/close/flush — must NOT add a
  synchronous paginated `fetch_all_orders()` on the hot path. Option: reuse the orders already fetched
  elsewhere in the cycle, OR bound/timeout the fetch, OR (safest) source the tier tag from the tracker's
  own `gtc_stop_order_id`/tier state it already stores (L1387/1457) rather than a live orders fetch. Decide
  in the P1 cold-2nd; default to NO new blocking Alpaca call on the EOD thread.
- NEVER-MASK-A-LOSS: skipping a protected-tagged fill only MOVES it out of intraday FIFO (QHM is tracked
  separately + reconciled by the authoritative ledger) — it does not zero a real loss. A genuine untagged
  long-close still records + alerts. Gate: full-read portfolio_tracker (hotspot) + fifo_pnl (done) + statics
  + cold-2nd (blocking-call check) + Gro+GAI (RTH-path) + adversarial + design-record.
