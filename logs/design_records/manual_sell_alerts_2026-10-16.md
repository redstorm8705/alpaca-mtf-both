# Owner manual sale of a protected hold — alert chain and fixes (2026-10-16, Claude)

**Status:**
- **Part A** (run_ledger_sync.py messages + cadence) ships in this PR.
- **Part B** (long-term hold tier, `execution/quarterly_hold_manager.py`) is a PROPOSAL. **Needs Gro/GAI vote.** It touches the protective-stop path, and this session may not edit that file.

## What happened (LLY, 2026-10-16)
The owner sold 1 LLY share (a long-term hold, QHM) in the Alpaca app at 11:07 PT. Slack then showed:

| # | Message | Source | Verdict |
|---|---------|--------|---------|
| 1 | "Sold LLY · Manual …" | `execution/fill_notifications.py` (untagged fill → "Manual") | Correct and useful. Keep. |
| 2 | ":rotating_light: QHM LLY stop coverage unresolved (ownership proof unavailable) — PENDING_STOP_REPLACE; no blind stop mutation." — repeated 11:07, 11:19, 12:57, 13:03 … | `quarterly_hold_manager.py:4880` `_stop_coverage_pending`, called from `_reconcile_stop_coverage:5145-5147` | **Bug (Part B).** Jargon, no action given, repeats forever. |
| 3 | "ledger_sync OPERATOR CONFIRMATION … Sells stay frozen" + a raw dict | `run_ledger_sync.py` | Fixed in Part A. "Sells stay frozen" was false (`OWNERSHIP_GUARD_ENFORCE = False`, config.py:753). |
| 4 | "ledger_sync FAILING: N consecutive non-heals … STALE" on every pass from N=3 | `run_ledger_sync.py` | Fixed in Part A. Now pages once at 3, then at most once per ET day. |
| 5 | "Session so far" booked the LLY sale under Swing, not QHM | `scripts/pnl_snapshot.py` `_realized_by_tier` | Open; see Part C. |

## Root cause of #2 — verified by running the real code
`run_weekly_check()` (every RTH cycle) runs `_reconcile_stop_coverage` for each ACTIVE / PENDING_STOP_REPLACE hold BEFORE anything else.

1. The owner's sale leaves the broker position flat, so `_strict_resync_qty` returns None (`quarterly_hold_manager.py:4794-4796`, `alpaca_pos is None → None`).
2. `_reconcile_stop_coverage` then pages "ownership proof unavailable" and returns `(True, True)` (5145-5147).
3. The symbol goes into `_coverage_blocked` (~893), and the main loop skips it (`if symbol in _coverage_blocked: continue`, ~912).
4. So `_detect_external_close` (~917 / ~946), the code that recognises "closed outside the bot", is never reached. Startup has the same order (`_startup_blocked`, ~636).

Probe (scratch, mocks only):
- Setup: LLY ACTIVE, broker `get_position → None`, an empty order book, the real `run_weekly_check` run 3 times.
- Result: 3 identical pages, `_detect_external_close` called **0** times, and the hold left in PENDING_STOP_REPLACE.

The order came in with #596 (2026-10-11, "reconcile protective stops to tier ownership").

**Same trace, second consequence:** when a hold's OWN GTC stop fills, the broker position is also flat. So the stop-out branch of `_detect_external_close` is never reached either. That branch is the one that auto-heals the ledger through `apply_authorized_tier_reduction(..., "qhm_stop_fill")`. Since 2026-10-11, a QHM stop-out therefore produces the same endless page plus a ledger freeze, instead of "stopped out — ledger auto-healed". [by code trace and the probe above; not yet observed live]

## Part A — shipped in this PR (run_ledger_sync.py)
- The confirmation page lists each symbol in plain English: "the bot's long-term hold (QHM) record says 1 share, the broker now shows 0. If you sold them yourself, confirm with: `python3 confirm_ledger_heal.py LLY qhm 0`".
- It says where to run the command, and warns: "If you did NOT sell these shares, do not confirm".
- The stale reminder pages once when it reaches the threshold, then at most once per ET day. It repeats the per-symbol command and the warning.
- Plain-English slow-run and failure text.
- Review: cold second agent PASS, adversarial PASS (round 2; round 1 FAIL fixed: the stale reminder lacked the do-not-confirm warning).

## Part B — proposal for the QHM-owning session (needs Gro/GAI vote)
1. **Order fix (the bug).** In `_reconcile_stop_coverage`, before `_strict_resync_qty`:
   - If `broker.get_position(sym)` is None and `pos.qty_filled > 0`, return `(True, False)` without paging. This keeps the stray-sell guard off the symbol but lets the main loop reach `_detect_external_close`.
   - That function then closes the hold. It either auto-heals (own stop filled) or asks for the owner's confirmation (any other sale).
   - A broker read error must stay fail-closed (page as today). Only a confirmed flat position takes the new branch.
2. **Leftover stop.** When `_detect_external_close` closes a hold whose broker position is flat, cancel QHM's own stop by exact order id (`pos.stop_order_id`, `QH-` coid, still open).
   - Otherwise a resting sell-stop on a flat symbol could open a short when it triggers. [hypothesis — unverified: depends on account shorting settings and whether Alpaca kept the stop after the app sale]
   - Owner fact (2026-10-16): on a manual sale the owner always picks "cancel all open orders", so after his own sales no stop is left. This step still covers sales by anything else (e.g. a mystery seller).
3. **Dedupe** `_stop_coverage_pending` pages: once per (symbol, reason) per ET day, persisted in hold state. The state change and the log line stay every cycle.
4. **Plain English** for the QHM pages, e.g.:
   - "LLY (long-term hold): the bot can't confirm how many shares it owns, so it is not touching the protective stop. If you sold LLY yourself, nothing to do here — a separate message has the one confirm command."
   - External close: "LLY (long-term hold) was sold outside the bot (for example in the Alpaca app). The bot closed this hold and will not buy it back. To update the bot's share record, run: `python3 confirm_ledger_heal.py LLY qhm 0`."
5. **One message per manual sale.** Once 1–4 land, a manual sale of a hold should produce exactly:
   - the fill notice;
   - one QHM "sold outside the bot" message with the command;
   - the ledger-sync confirmation (Part A).

   Merging the last two into one is a follow-up.
6. **Not proposed:** auto-confirming an untagged sale as the owner's. The never-shrink guard exists to catch a sale by another part of the bot; only the owner can tell the two apart. Auto-confirm would need a board vote of its own.

Tests to add with Part B:
- Flat broker + ACTIVE hold → `_detect_external_close` runs and the hold is CLOSED (the probe above, asserted).
- Flat broker + own stop filled → ledger auto-heal is called.
- Broker read error → still fail-closed and paging.
- A second cycle on the same day → no second page.

Risk path: (1) and (2) change when a protective-stop path acts. No size, frequency or concurrency change. Board seats: reliability + masked-loss.

## Part C — LLY P&L booked under Swing (open, not diagnosed)
`_realized_by_tier` (`scripts/pnl_snapshot.py`) books a close to the tier of the OLDEST open lot in a single FIFO per symbol since account inception. The tier comes from the opening order's client_order_id; untagged → "intraday" (Swing).

Candidate causes [hypothesis — unverified; needs the Alpaca LLY fills + orders]:
- (a) an older untagged or IN- LLY lot still at the front of the FIFO;
- (b) the QHM buy's order missing from `fetch_all_orders`, so the coid lookup failed.

Check:
- the LLY fills and order coids since inception;
- `_realized_by_tier` on them.

Fix only after that check.
