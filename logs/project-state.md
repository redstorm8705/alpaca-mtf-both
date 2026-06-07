# Project State — alpaca-mtf-bot
**Snapshot:** 2026-06-07 S53 (post wrap-up) | **Overwrite every session — canonical state only**

## Bot Status
- Running on OCI Phoenix 129.153.208.32 — 4 services active (mtf-bot, mtf-writer, mtf-http, nginx)
- Paper account: $2,821.25 equity | Cash: $3,143.86
- Profile: paper | STOP=1.20x ATR | TARGET=2.5x | MIN_SCORE=9/12 | KELLY=0.35 | MAX_RISK=4.5%
- PDT enforcement REMOVED per SEC/FINRA rule amendment (board S50 28-0)
- Latest commit on OCI: 1483498 (all changes deployed)

## Open Positions (live from Alpaca as of S53 end)
| Symbol | Side | Shares | Entry | Unrealized |
|--------|------|--------|-------|------------|
| MSTR | short | 1 | $118.94 | -$1.50 |
| NFLX | short | 1 | $81.84 | -$0.34 |
| TOST | short | 2 | $24.40 | -$0.48 |
| UBER | short | 1 | $70.46 | -$0.25 |

## Completed This Session (S53)
**exit_logic.py Tier 2 PDT Removal — COMPLETE**
- Commit `8fc0cd0`: 20 PDT items removed from exit_logic.py (-312 lines, 2435→2123)
- RC-3 fix at L1996: bare except → logger.warning
- 5 unused imports removed (macd/ema/submit_gtc_stop_close — only used in PDT code)
- `_pdt_htf_gate()` replaced with 3-line pass-through stub (import compat for main.py + trade_engine.py)
- All exit paths now close unconditionally — no PDT=3/3 gating, no deferred GTC paths
- Commit `daabe80`: portfolio_tracker.py Tier 2 partial (prior session carried forward)
- Commit `1483498`: Audit logs + CLAUDE.md RC counts updated

## Top Open Items (Priority Order)

### P0 — Tier 2 PDT Removal (each file needs full Steps 1-9 + DS/GAI, Rule C-6)
**SEQUENCE MATTERS**: main.py must be patched BEFORE portfolio_tracker.py final pass
1. **main.py** (~980L, 36 patches): `tracker._day_trades` direct write at L193-202 + ~10 other PDT refs. DS/GAI required (hotspot). Must come BEFORE portfolio_tracker.py final pass.
2. **portfolio_tracker.py** (P0 CRITICAL, 44 patches, ~2125L): Stubs to delete after callers cleaned:
   - `record_day_trade()` — exit_logic.py callers DONE; main.py caller remains at L193-202
   - `get_rolling_day_trade_count()` — all callers now removed
   - `set_pdt_gtc_stop_order_id()` — exit_logic.py caller DONE
   - `_load_day_trades()` / `_save_day_trades()` / `self._day_trades` — main.py L193-202 writes directly
   - `pdt_slots_used` in `write_eod_summary` — reporting/metrics.py L110 reads via `.get()` fallback
3. **entry_logic.py** (~1747L): ~18 PDT refs
4. **config.py**: ~13 PDT refs incl. `DAY_TRADE_MAX_ROLLING` constant (delete last, after all comparisons removed)
5. **broker.py**: ~5 PDT refs

### P1 — Pending Approvals (logs/pending_approvals_2026-06-07.md)
6. **#1** — RC-8: 9 missing `_rc8_clear_buffers()` in entry_logic.py (Board APPROVE, DS/GAI REJECT — deadlocked, Rafael decides)
7. **#2** — RC-4: exit_logic.py L1345/L1939/L2032 (strategy decision pending)
8. **#3** — orphan_manager.py cancels ALL GTC stops without checking quarterly_hold_manager

### P1 — Known CRITICAL Bug (deferred)
9. **trade_engine.py L252-254**: Direct `risk.open_positions` assignment instead of `risk.register_open()` — bypasses CYCLE-SYNC-GUARD. DS/GAI required.

### P2 — Cleanup (after Tier 2 complete)
10. **alerts.py**: Remove `pdt: int = 0` params from `alert_entry` + `alert_stop_breach`
11. **weekly_review.py L379**: `("Pdt", "PDT")` in `_fmt_reason()` — dead code

## RC Bug Counts (as of S53)
| RC | Class | Count | Status |
|----|-------|-------|--------|
| RC-3 | Silent exception | **2** | OPEN — 2 unknown-other-files |
| RC-4 | Estimated exit price | **10** | OPEN — 3 in exit_logic.py |
| RC-2 | CWD-relative path | **7** | OPEN |
| RC-7 | Zero-share sizing | **2** | OPEN |
| RC-5 | Non-atomic write | **1** | OPEN |
| RC-6 | Wrong API field | **3** | OPEN |
| RC-8 | Unbounded scan buffer | **1** | OPEN — 9 sites entry_logic.py |
| RC-1 | Naive datetime | **0** | CLOSED |

## Key Decisions (permanent)
- Board is PRIMARY authority; DS/GAI supplement only
- DS/GAI via direct Python API (manual .env parse) — browser automation BROKEN
- PDT removal = remove + SEC/FINRA cite, not disable — accounts <$25K exempt
- All P&L = Alpaca fills API only (FIFO); tracker math is cross-check
- paper=True hardcoded in broker.py — never change without full board vote
- Kill switch: 7% paper (board 25-1, confirmed S50 13-0)
- SEQUENCE FOR REMAINING PDT CLEANUP: main.py → portfolio_tracker.py → entry_logic.py → config.py → broker.py

## Architecture Invariants
- SPY 5-min bar-over-bar is the SOLE entry gate
- MRI background only — sets size floor and MIN_SCORE floor, does not gate entries
- GTC stops submit for ALL overnight positions
- VIX-adjusted stop widening: >25 = 1.5x, >30 = 2.0x multiplier
- Max 2 simultaneous positions with beta correlation >0.7
- BUCKET_B_MAX_POSITIONS = 999 (unlimited — S52)
- INTRADAY_STOP_ATR_MULT = 1.20, TARGET = 2.5x, KELLY_FRACTION = 0.35, KELLY_MAX_RISK_PCT = 4.5%

## User Preferences
- No options/menus — identify top priority and execute immediately
- "Remove not disable" on deprecated features
- Full read gate zero tolerance — no grep/partial reads EVER
- DS/GAI via direct API only; same prompt to both; board is primary
- Mid-session context limits: update all logs, push, continue in new session without re-briefing
