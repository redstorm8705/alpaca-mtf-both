# Project State — alpaca-mtf-bot
**Snapshot:** 2026-06-07 S52 (post wrap-up) | **Overwrite every session — canonical state only**

## Bot Status
- Running on OCI Phoenix 129.153.208.32 — 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- Paper account: $2,821.25 equity
- Profile: paper | STOP=1.20x ATR | TARGET=2.5x | MIN_SCORE=9/12 | KELLY=0.35 | MAX_RISK=4.5%
- PDT enforcement REMOVED per SEC/FINRA rule amendment (board S50 28-0)

## Open Positions
- MSTR short 1sh @ $118.94
- NFLX short 1sh @ $81.84
- TOST short 2sh @ $24.40
- UBER short 1sh @ $70.46

## Completed This Session (S52)
Tier 1 PDT display removal — all 5 files patched, deployed, committed:
1. generate_dashboard.py (9149354) — PDT tile removed, Win Rate tile added, realized-only P&L
2. alerts.py (df10009) — PDT text removed from alert messages, pdt params defaulted to 0, RC-3 fixed
3. scan_to_html.py + run_cycle.py (bafa28d) — _pdt_reset_display() deleted, PDT status bar removed, 4 call sites cleaned
4. weekly_review.py (77b64d3) — _pdt_for_date() + _rolling_pdt_count() deleted, Gemini prompt corrected ($1K→$2.5K, stop 1.25→1.20, no PDT)
5. monthly_review.py (76574e4) — pdt_day/pdt_badge/PDT Used box removed

## Top Open Items (Priority Order)
1. [P0] portfolio_tracker.py PDT Tier 2 — compute_pdt_for_date + compute_rolling_pdt_count now dead code; 36 patch history, 2122L, dedicated session required
2. [P0] exit_logic.py PDT Tier 2 — 6x DAY_TRADE_MAX_ROLLING blocks RC-3 mypy gate (pending #4)
3. [P0] entry_logic.py PDT Tier 2 — 18 PDT refs
4. [P0] main.py PDT Tier 2 — 12 PDT refs
5. [P0] config.py PDT Tier 2 — 13 PDT refs (incl. DAY_TRADE_MAX_ROLLING constant)
6. [P0] broker.py PDT Tier 2 — 5 PDT refs
7. [P1] Pending approval #1: RC-8 9 sites in entry_logic.py (Board APPROVE, DS/GAI REJECT IO concern — file logs/pending_approvals_2026-06-07.md)
8. [P1] Pending approval #2: RC-4 3 violations in exit_logic.py (strategy decision pending)
9. [P1] Pending approval #3: orphan_manager.py cancels ALL GTC stops without checking QHM
10. [P1] Pending approval #4: exit_logic.py DAY_TRADE_MAX_ROLLING blocks RC-3 mypy gate

## Recurring Bug Counts (as of S52)
| RC | Class | Count | Status |
|----|-------|-------|--------|
| RC-3 | Silent exception | 3 | OPEN — exit_logic.py L1996 blocked by #4 |
| RC-4 | Estimated exit price | 10 | OPEN — 3 in exit_logic.py |
| RC-2 | CWD-relative path | 7 | OPEN |
| RC-7 | Zero-share sizing | 2 | OPEN |
| RC-5 | Non-atomic write | 1 | OPEN |
| RC-6 | Wrong API field | 3 | OPEN |
| RC-8 | Unbounded scan buffer | 1 | OPEN — 9 sites entry_logic.py |
| RC-1 | Naive datetime | 0 | CLOSED |

## Key Decisions (permanent)
- Board is PRIMARY authority; DS/GAI supplement only
- DS/GAI via direct Python API (manual .env parse) — browser automation BROKEN
- PDT removal = remove + SEC/FINRA cite, not disable — accounts <$25K exempt
- All P&L = Alpaca fills API only (FIFO); tracker math is cross-check
- paper=True hardcoded in broker.py — never change without full board vote
- Kill switch: 7% paper (board 25-1, confirmed S50 13-0)

## User Preferences
- No options/menus — identify top priority and execute immediately
- "Remove not disable" on deprecated features
- Full read gate zero tolerance — no grep/partial reads EVER
- DS/GAI via direct API only; same prompt to both
