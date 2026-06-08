# Project State — alpaca-mtf-bot
**Snapshot:** 2026-06-08 S54 (post wrap-up) | **Overwrite every session — canonical state only**

## Bot Status
- OCI Phoenix 129.153.208.32 — all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- Equity: $2,813.62 | Cash: $3,015.47 | PDT count: 0
- Latest commit: `4b35638` (broker.py Tier 2 PDT removal)
- **RAM WARNING:** 713MB used / 96MB avail — monitor

## Open Positions
- NFLX short 1sh @ $81.84 | unrealized -$0.45
- TOST short 2sh @ $24.40 | unrealized -$0.13
- UBER short 1sh @ $70.46 | unrealized -$0.17

## P0 — COMPLETE ✅ Tier 2 PDT Removal
All 8 files done: exit_logic.py (S53), portfolio_tracker.py (S52+S54), handlers.py/lifecycle.py/trade_engine.py/entry_logic.py/config.py/broker.py (S54).

## P1 — Open Items
1. **trade_engine.py L252-254 CRITICAL**: `risk.open_positions = len(...)` direct assignment bypasses CYCLE-SYNC-GUARD — needs DS/GAI + full Steps 1-9
2. **Pending approval #1**: RC-8 entry_logic.py — 9 missing `_rc8_clear_buffers()` (Board APPROVE, DS/GAI REJECT — deadlocked, Rafael decides)
3. **Pending approval #2**: RC-4 exit_logic.py L1345/L1939/L2032 — strategy decision on fallback price source

## P2 — Cleanup
- alerts.py: remove `pdt: int = 0` params from `alert_entry` + `alert_stop_breach`
- weekly_review.py L379: `("Pdt", "PDT")` in `_fmt_reason()` — dead code

## Key Decisions This Session (S54)
- Broker PDT error code handling (40310100 + text fallback) KEPT per board + DS/GAI consensus — Alpaca platform may still return 40310100 regardless of internal PDT removal
- Per-line `# type: ignore` over file-level mypy suppress in broker.py (DS/GAI rejected broad suppress)
- RULE C-5: config.py comment-only changes = no DS/GAI required

## RC Bug Counts
RC-3: 2 | RC-4: 10 | RC-2: 7 | RC-7: 2 | RC-5: 1 | RC-6: 3 | RC-8: 1 | RC-1: 0 (CLOSED)

## Hard Rules (user preferences — enforce every session)
- Full Read Gate: ZERO TOLERANCE — full file before any patch, declare line count, NO grep
- DS/GAI: direct API only (browser automation broken), same prompt to both, AUTONOMOUS
- Board: independent cold Explore subagents ONLY — never inline roleplay
- paper=True hardcoded in broker.py — board vote required for live
- All P&L: Alpaca fills API only (FIFO)
- No options trading EVER
- "Always approve crons" — standing user instruction
- Never present options/menus — execute highest priority immediately

## Next Session Priority
1. trade_engine.py L252-254 CRITICAL risk desync (P1, full Steps 1-9 + DS/GAI)
2. Resolve pending approval #1 (RC-8 deadlock — Rafael decides)
3. alerts.py P2 cleanup (if time)
