# Unreadable Day share count on a co-held stock — retry all session (design, 2026-10-11, Claude)

**CEO (2026-10-11):** "Why are we not continuously retrying throughout the session and flagging EOD if the issue
persists?"

## Today (verified at source)
- `execution/broker.py` `_cohold_bounded_close(symbol, tier)`: before another tier (Swing, breakout, QHM, F6) fully
  closes a stock, it reads the Day tier's share count on that stock. The count comes from today's DT- orders at Alpaca,
  with the Day log as a fallback.
  - If both are unreadable, it pages and returns None. The caller then does a normal FULL close, which can sell the
    Day tier's shares too. The Day lot's own OCO stop/target would then sell shares that are gone and open an
    accidental short. The page asks for a manual check.
  - There is no retry: one failed read on one cycle decides the close.
- **Callers** (all full closes through `close_position`):
  - `execution/exit_logic.py`: overnight breakeven ~L1405, ~L1568, hard stop ~L1667, target ~L1750, signal exit
    ~L2008.
  - `execution/stop_protection.py` ~L733 (safety net), `execution/entry_logic.py` ~L311,
    `execution/gtc_manager.py` ~L190, `execution/quarterly_hold_manager.py` ~L427 (QHM).
- **Production:** no unreadable claim has been logged yet. Checked 2026-10-11: OCI `logs/*.log` contains no
  "without knowing the Day tier" or "share count changed while closing" line. The co-hold went live the same day.

## Proposed design
- **R1 — urgent exits (hard stop, safety-net stop, breakeven):** retry the claim read 3 times within about 3 s. If it
  is still unreadable, close only the tier's OWN recorded quantity (its book qty), as a bounded partial close, instead
  of the full net. This never sells more than the tier itself bought, so a Day lot cannot be sold by mistake. If the
  tier's own quantity is also unknown, keep today's full close plus a page (a stop exit is never refused).
- **R2 — non-urgent exits (target, signal exit, QHM/F6 planned exits):** defer the exit for that cycle, log the reason,
  and retry on each later cycle. Page once on the first deferral. If still unreadable at 3:50 PM ET, fall back to the
  R1 bounded close of the tier's own quantity.
- **R3 — end-of-day flag:** a short EOD summary line (Slack) lists every symbol whose claim stayed unreadable on any
  cycle that day, with first/last time and retry count.

## Open forks for BGG
1. R1: is "own recorded quantity" (the tier's book) safe enough as the bound? Its own book can be wrong (e.g. the
   2026-07 false-drop class). Or should R1 keep the full close and only add the retry?
2. R2: is deferring a target/signal exit acceptable on a paper account, given that a target can be missed? Or should
   R2 also take the R1 bounded close after the quick retry?
3. Which exit kinds count as urgent? Proposed: hard stop, safety-net stop, breakeven, kill switch, circuit-breaker
   halt.
4. Where should the retry state live: in memory per process (the 5-minute cycle is one long-running process) or in a
   state file?

## BGG consensus (2026-10-11; board Harris+Taleb, GAI; Gro unavailable — daily token cap, 2026-07-07 rule)
- **Fork 1 — bound:** each caller passes its own share count and side into `close_position`. If the Day count is still
  unreadable after the retry, sell `min(own_qty, |net|)` on the lot's side only, via `partial_close_position(...,
  position_side=)`. Then re-read the net; any leftover shares page CRITICAL with whether a stop covers them. If own_qty
  is missing or below 1, keep today's full close plus a page (a stop exit is never refused). 3-0.
- **Fork 2 — defer target/signal exits:** NO (Harris, GAI; Taleb wanted a capped deferral while the own stop rests).
  Majority 2-1: one path for every exit. A skipped target corrupts the edge data; the bound already protects the Day lot.
- **Fork 3 — urgency list:** not needed (single path). Kill switch closes nothing; a circuit-breaker halt bypasses the
  bound (Invariant #7).
- **Fork 4 — state:** in memory; at most 3 reads in about 3 s (it runs on the trading thread). Each event is written
  to `trade_events.jsonl` (`cohold_claim_unreadable`: symbol, tier, attempts, outcome, shares sold); the end-of-day
  Slack summary is built from that log.
- **Risk path:** not risk-path (sells shrink only, no new entries); still exit-path, so the full gate applies.
- **Build note:** add a test that the bounded close never sells more than `min(own_qty, |net|)` and that the path
  always ends in an order or a page.
