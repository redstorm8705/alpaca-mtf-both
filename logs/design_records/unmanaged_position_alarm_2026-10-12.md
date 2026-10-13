# Unmanaged-position alarm — design (2026-10-12, Claude)

**Trigger (2026-10-12):** 72 UBER (from 4 test-placed bracket orders, filled 04:00 ET) sat in the account all session
with no owner, no stop after 16:00, and no page; buying power $0 stopped every tier. Also 1 MSFT (Swing 10/05) that
the Swing tracker no longer lists. The only checks: `main.py` P0-STARTUP (Swing tracker only, log-only, startup only)
and `run_ledger_sync.py` (fill ledger; DT-tagged fills count as Day-owned; cron 09:00-17:40 ET).

## BGG consensus (board Majors + Peterffy: C; GAI: B; Gro unavailable — daily cap)
- **B in the main loop (source of truth), every phase incl. the 30-min overnight cycle:** residual = broker net
  minus what each LIVE manager manages (Swing tracker open trades, today's Day-tier log, QHM quantities, F6 holdings,
  promotions). A 04:00 ET fill pages by ~04:30.
- **A as a cross-check in run_ledger_sync** (snapshot residual) — catches a down bot; an A/B disagreement is paged
  with both numbers.
- **Page only. No order is ever placed** (no owner tag, no safe qty, no stop level; never-mask-a-loss). Auto-protect
  is a separate risk-path diff if ever wanted.
- A manager source that cannot be read is paged as UNVERIFIABLE, never treated as zero.
- One structured `unmanaged_position` event per cycle to trade_events.jsonl; Slack once per (symbol, ET date, sign);
  re-page if the residual grows, its notional exceeds 0.5x equity, or it is still unmanaged ~15 min before the open
  or the close. The page states when buying power is <= 0.
- Wrapped so it can never stall check_exits(); the startup P0 check pages too.
- Tests: UBER case; MSFT case (ledger says owned, tracker empty); QHM/F6 not flagged; unreadable source -> page, not
  zero; dedupe + growth re-page + next-day re-arm; 04:05 ET evaluated; zero orders submitted; exception contained.

## Build (2026-10-12, branch claude/unmanaged-alarm)
- `execution/unmanaged_positions.py` `check_unmanaged(tracker)`: residual = Alpaca net minus Swing tracker + Swing
  breakout (holding states) + today's Day-tier log + QHM active quantities + Forever-6 (ledger slice). Pages once per
  (symbol, ET date, sign), re-pages when the residual grows; unreadable sources page UNVERIFIABLE once a day (the
  residual is still reported, never assumed zero); `unmanaged_position` event each pass. Page-only.
- `main.py`: called after run_cycle every loop (5 min RTH, 10 min pre-market, 30 min overnight), wrapped.
- Read-only probe on OCI 2026-10-12 (paging stubbed): flagged only UBER 72; MSFT (breakout), PLTR/EWY (Swing),
  GE/GOOGL/LLY (QHM) counted as managed.
- Deviation from the board's C: the ledger-sync cross-check (A) is deferred — it would double-page with B and adds value
  only when the main bot is down (the watchdog covers that). Re-page at 0.5x equity / pre-close not built (growth
  re-page only).

## Review round 2 (2026-10-13): cold-2nd FAIL + adversarial FAIL, both fixed
- An unreadable source was left out, so its own holdings paged "close them", and the source page said the
  opposite ("may go unflagged"). Each source's LAST GOOD reading is now persisted and used in its place. With no
  last good reading, its symbols page with "verify before acting". Each source failure pages once a day under its
  own key (the broker read is a separate key).
- Day lots in their overnight exit dropped out at midnight ET. Day-tier entries from the previous 3 days that are
  still open now count until 09:45 ET (the Day tier flattens at the regular open). The module reads the Day log
  itself, so `ownership_snapshot._today_daytrade_claims` is unchanged.
- Promotions: Day->Swing hand-offs in status "ready" (booked out of the Day log, not yet adopted) count as managed.
  An unreadable hand-off file is a source error.
- Over-claim: when Alpaca holds less than the tiers record, the page now says "tiers' records are ahead" instead of
  "unmanaged shorts" (key kind "over").
- Breakout entry still settling (qty 0): counts `req_qty`.
- Still deferred and disclosed: the ledger-sync cross-check, re-page at 0.5x equity / pre-open / pre-close, the
  buying-power line in the page, the startup P0 page, a per-tier provider registry, and claims for symbols Alpaca no
  longer holds (the reverse case; pending orders would false-page).

## Review round 3 (2026-10-13): cold-2nd FAIL (weekend carry) + adversarial FAIL (same-day reappearance), fixed
- **Day-lot carry now follows the trading calendar.** A prior-day lot counts until 09:45 ET of the first session
  after its entry day. The session dates come from SPY daily bars (T1, cached 30 min); if that read fails, the first
  weekday after entry is used. Weekends and holidays carry. Hard bound: 6 days.
- **A residual that clears is removed from the dedupe record,** so if it comes back the same day it pages again.
- **A residual must be seen on two consecutive passes before it pages.** Day fill-to-log (~10-20 s) and hand-off
  booking races never page; a real one pages one cycle later.
- **Day-log and hand-off fallback is same-day only.** A prior day's reading is not evidence. Other sources keep
  their last good reading, and their source page repeats daily.
- **OCI read-only probe (2026-10-12, 21:xx PT):**
  - all six sources read cleanly; sessions ended 2026-10-12;
  - UBER 72 is first seen on pass 1 and pages on pass 2;
  - nothing else is flagged.

## Review round 4 fixes (2026-10-13): cold-2nd FAIL (kind flip) + adversarial FAIL (unconfirmed growth), fixed
- **Dedupe is cleared per key** (symbol|kind) when that key is absent this pass. A flip from long to over and back
  pages again.
- **Confirmation memory covers the previous TWO passes, per key.** A residual pages only at the size confirmed on
  an earlier pass: `min(now, max(prev1, prev2))`. Two consequences:
  - growth is confirmed too, so a Day fill-to-log race on a symbol that already paged neither pages nor raises the
    dedupe high-water mark;
  - a residual masked on every other pass still pages.
- **The memory survives the ET midnight reset**, so there is no extra overnight delay.
- **A failed SPY session read is cached for 30 min**, so the main thread never retries fetch_bars every pass.
- **main.py:** the check runs in a `finally` after `run_cycle`, so a raising run_cycle cannot silence it.
- **Pre-existing, outside this diff (adversarial NIT, to verify):**
  - QHM `_check_fill_and_advance` may add the broker's total position as the new tranche's `filled_qty`, which
    would over-count `qty_filled` on a second tranche. If true, this alarm shows it as a "records are ahead" page.
    Separate investigation.
  - QHM tranche fills are booked only after 10:05 ET inside `maybe_enter_positions`, so a QHM fill between the
    open and 10:05 can page "unmanaged" for those shares.

## Review round 5 fixes (2026-10-13): adversarial FAIL (two Day races on one stock; unwritable state), fixed
- **Booking-aware confirmation.** Each pass stores `[|residual|, managed]` per key. An earlier pass's residual
  counts only after subtracting what the tiers booked since (managed moving toward the broker). Two Day fills on one
  stock in back-to-back passes never page. Shares no tier ever books keep `managed` unchanged and page on the next
  pass (UBER).
- **In-process memory copy.** If the state file cannot be written, the confirmation memory and dedupe survive in
  memory, and one page per day reports the unwritable file.
- **Flap guard.** A paged key is cleared only after two consecutive absent passes, so a flapping residual pages once.
- **QHM over-count NIT (round 4) verified at source: no double count on today's paths.**
  - `_check_fill_and_advance` handles tranche 1 only (qty_filled 0). Later tranches use `_add_tranche_active`
    (comment at quarterly_hold_manager.py:1060).
  - Real caveat: the QHM resync (quarterly_hold_manager.py:4385-4397) sets `qty_filled` to the broker's WHOLE
    position, so a QHM hold co-held with another tier claims all the shares. UBER must be registered in QHM only
    after the 65-share sell fills.
- Round-4 cold-2nd (QHM tranche fill unbooked on a kill/halt afternoon or near the close): a residual on a symbol
  with an outstanding QHM entry order (`entry_order_id` in quarterly_holds.json) pages "QHM entry pending — Do NOT
  close; verify the QHM order filled" instead of "close them". Option contracts are ignored (no share tier claims
  them).

## Review round 6 (2026-10-13): adversarial FAIL (shrink then regrow never re-paged), fixed; final cold-2nd PASS
- **Shrink then regrow.** When a residual stays below its paged mark for two consecutive passes (or reappears
  after an absent pass), the mark is lowered to `max(now, previous pass)`. A later regrowth the same day then pages
  again: UBER 72 -> 7 after the sale -> a new lot pages. A one-pass dip never re-pages.
- **Page text and stored mark** both use the confirmed size.
- **QHM "Do NOT close" wording** applies only to a long residual.
- **Malformed memory record:** read with a guard, so it can never stall the alarm.
- **Final read-only OCI probe (2026-10-13 ~10:05 ET):**
  - all six sources readable;
  - PLTR (Swing), MSFT (breakout), AMZN 6 (Day log), GOOGL/GE/LLY (QHM) counted as managed;
  - UBER 7 (no tier until its QHM adoption) pages on pass 2 only.
