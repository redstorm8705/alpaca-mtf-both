# Stop-failure page: once per stop per day (2026-10-16, Claude)

**Trigger:** the owner's Slack review request (2026-10-16): "we need these alerts to be intentional, and in plain English". In the review, `alerts.alert_gtc_failed` ranked as the worst repeater on the trading side.

## Facts read at source
- 20 call sites in `execution/` (exit_logic 3, entry_logic 1, broker 7, orphan_manager 9) call `alert_gtc_failed`.
- Several of them are on retry paths that re-run every cycle, so the same failure pages Slack and the phone at top priority on every cycle:
  - `orphan_manager.py:845-859` pages on every cycle once a stop has been blocked 5 cycles (`_p1_defer >= 5`, re-checked every cycle);
  - `orphan_manager.py:936-948` (no stop price or qty) pages every cycle.
- The text always said "Position is UNPROTECTED overnight", including for DAY stops (`broker.py:1565`, `:1717`).
- The reason string carries raw exception text that changes between attempts, so it cannot be the dedupe key.

## Design (no open fork)
- **Dedupe:** one page per (symbol, side, stop price) per ET calendar day.
  - The first failure pages Slack and the phone, as today.
  - Later failures of the same stop that day are logged at CRITICAL and not paged.
  - A different symbol, side or stop price pages at once.
  - A failure that persists pages again on the next ET day.
- **Stamp each transport separately, only after it confirms delivery** (the phone push and Slack each). A failed send is retried on the next failure, so a phone outage cannot leave only Slack paged while callers keep retrying (cold-2nd 2026-10-16). Same stamp-on-ACK pattern as `alert_floor_blind` and `alert_crash`.
- **The page-state file fails open.** It is `data/state/gtc_failed_pages.json`, written atomically and keeping only today's date. An unreadable file means "not paged yet", so a page is never lost to a bad file, and the next confirmed send rewrites it clean.
- **Plain-English text, no "overnight" claim.** It reads: "The bot could not place the SELL protective stop @ $X for SYM… Until this is fixed, SYM has no stop at the broker. The bot keeps retrying; check the stop in the Alpaca app. This page is sent once a day per stop; every retry is in the log."
- The "no stop price" case (orphan_manager passes side "unknown", price 0) reads "SYM has no stop price or share count in the trade log…" instead of "UNKNOWN stop @ $0.00".
- **Never raises:** a formatting or transport error is logged, and the caller's stop path is untouched.
- **Not changed:** every caller, the retry logic, and order paths.

## Risk path (Rule B screen)
Size, frequency and concurrency are all zero: no order, sizing or entry path changes.

Masked-loss check: nothing is hidden on the first failure, or the first failure of each day. The remaining risk is a stop that is fixed and then fails again later the same day at the same price. That repeat is logged but not paged; it was already paged once that day. It is accepted because each repeat stays in the log and the next day pages again.

## Anti-silo
The signal is "a stop could not be placed". A per-symbol "currently unprotected" flag that other paths read would be a separate design, not wired here.

## Gate
- Full read of alerts.py (645 lines); cold second-agent review (round 1 PASS with three improvements, all applied; round 2 PASS); tests in `tests/test_alert_gtc_failed_dedupe.py`.
- Gro/GAI audit the exact diff in the PR's preship CI.
- No `.env` keys in this session.
