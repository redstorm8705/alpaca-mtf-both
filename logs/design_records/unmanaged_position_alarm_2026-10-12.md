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
