# Project State — alpaca-mtf-bot
**Updated:** 2026-06-08 S55 (wrap-up)

## Bot Status
- **Running:** YES — OCI Phoenix `129.153.208.32` | all 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- **SSH:** `ssh -i ~/.ssh/mtf_bot_oracle ubuntu@129.153.208.32`
- **Account:** Paper ~$2,500 start → **$2,813.82** equity | $2,932.64 cash
- **PDT:** REMOVED — all PDT code eliminated from all 8 files (Tier 2 complete S55)
- **OCI git:** latest commit `2a1c4dc` deployed and running
- **RAM:** 251MB used / 559MB avail (healthy)
- **daytrade_count:** 0

## Open Positions (live as of S55 end)
| Symbol | Side | Shares | Entry | Unrealized |
|--------|------|--------|-------|------------|
| TOST | short | 2 | $24.40 | +$0.32 |
| UBER | short | 1 | $70.46 | +$0.12 |

## Tier 2 PDT Removal — ALL FILES COMPLETE
| File | Commit | Session |
|------|--------|---------|
| exit_logic.py | 8fc0cd0 | S53 |
| portfolio_tracker.py | bd8827b | S52+S54 |
| handlers.py / lifecycle.py / trade_engine.py / entry_logic.py | S54 commits | S54 |
| config.py | 9768e43 | S55 |
| broker.py | 2a1c4dc | S55 |

## Open Items

### P1 — CRITICAL Bug
- trade_engine.py L252-254: Direct risk.open_positions = len(...) bypasses CYCLE-SYNC-GUARD. Needs DS/GAI.

### P1 — Pending Approvals
- #1 RC-8: 9 missing _rc8_clear_buffers() in entry_logic.py (Board APPROVE, DS/GAI deadlocked)
- #2 RC-4: exit_logic.py L1345/L1939/L2032 — strategy decision on fallback price source

### P1 — RC Bugs
- RC-4: 7 violations unlocalized
- RC-3: 1 violation unlocalized
- RC-2: 7 violations in run_cycle.py + entry_logic.py
- RC-7: 2 violations in main.py

### P2
- alerts.py: Remove pdt params from alert_entry + alert_stop_breach
- Post-market pipeline: auto_ai_audit.py findings not wired to autonomous_review.py queue

## RC Bug Counts
RC-4: 7 | RC-2: 7 | RC-3: 1 | RC-7: 2 | RC-5: 1 | RC-6: 3 | RC-8: 1 (deadlocked) | RC-1: 0 (CLOSED)

## Key Rules
- paper=True hardcoded in broker.py
- All P&L from Alpaca fills API only
- PDT REMOVED from all files
- Full Read Gate ZERO TOLERANCE
- Board = independent cold Explore subagents only
- DS/GAI disagreement with board: counter-prompt up to 3 rounds, then escalate
- User authority is final
