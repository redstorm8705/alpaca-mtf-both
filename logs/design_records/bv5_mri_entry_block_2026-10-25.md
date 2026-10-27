# BV-5 market-risk entry block — investigation (2026-10-25, signed: Claude) — DRAFT, not a proposal yet

Owner direction 2026-10-25: "Let's do ... the 10/22 entry block."

## What the code does (source)
`strategy/run_cycle.py:2027-2053` (BV-5): when the Macro Risk Index level is HIGH or CRITICAL, the main MTF scan
returns before `run_scan()` — no signals are computed and no main-scan entries are possible. Its comment cites
"HIGH/CRITICAL = 0% WR per live trade log (23 exits, 2026-05-05)"; STRESSED was removed from the block 2026-06-25.
This contradicts Architecture Invariant #9 ("MRI is background only ... does not gate entries directly").
It runs AFTER the Swing entries (`run_cycle.py:2011-2012`) and is separate from the Day tier process, so Swing and Day
are never blocked by it.

## Measured (server logs, `logs/mtf_bot.log`, which starts 2026-07-19)
- Days BV-5 fired: 2026-08-19 (2 cycles), 10-08 (7), 10-15 (8), 10-22 (46).
- 10/22: first block 10:01 ET, last 15:53 ET — the whole session after the opening half hour.
- Main-scan entries (`trade_events.jsonl`, tier intraday/untagged) since 2026-09-20: about 6; none since 10-15. On
  normal days the scan still finds signals every cycle; they stop at other gates (ORB confirmation, raised dynamic
  MIN_SCORE, symbols already held by Swing).
- Main-scan entries at HIGH/CRITICAL since July: zero (the block prevents them), so there is no live record to test
  the May "0% win rate" claim against.

## Blocker for a front-loaded replay (Rule C)
The existing Core MTF replay tools (`research/core_mtf_*_replay.py`) replay LOGGED candidates; on a blocked day the
scan never ran, so nothing was logged. A real replay needs the scan regenerated point-in-time on those days' bars
(2026-08-19, 10-08, 10-15, 10-22). That regenerator does not exist yet and is the next build step.

## Options to evaluate once the replay exists (no recommendation yet)
- Remove BV-5: HIGH/CRITICAL fall through to the existing raised MIN_SCORE and MRI size floor (Invariant #9 as
  written). Risk-path (frequency): board + masked-loss seat + owner approval.
- Keep BV-5 and amend Invariant #9 to say so.
