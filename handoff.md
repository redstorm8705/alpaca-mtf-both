# Handoff — S63 T3 Fix + Forbidden Logic Audit (2026-06-24)

## LATEST CHANGES (this session)

| Commit | File | Fix |
|--------|------|-----|
| `be779ba` | `execution/quarterly_hold_manager.py` | pandas `.iloc` fix — `bars[-2]`, `bars[i]`, `if bars and` bugs causing NVDA/GOOGL entry failures |
| `ea9fa0c` | `execution/quarterly_hold_manager.py` + `data/state/quarterly_holds_config.json` | NVDA + GOOGL added as quarterly holds |
| `3cab1db` | `execution/exit_logic.py` + `CLAUDE.md` | T3 explicit close for qty_rem=1; CLAUDE.md Rule 13; Rules 7+9 roadmap |
| `625f751` | `config.py`, `events/handlers.py`, `execution/gtc_manager.py`, `weekly_review.py` | PDT comment cleanup |

**Branch:** `main`
**OCI HEAD:** `be779ba` (full rsync — all Python files in sync, all services active)
**QHM status:** LIVE — NVDA/GOOGL entry gate bug FIXED. Will retry at 10:05 AM ET Jun 25.
**OCI note:** Post-`3cab1db` deploy, OCI drifted (main.py/trade_engine.py still had `_pdt_htf_gate` import; exit_logic.py had it removed). Full rsync at S63 restored sync. All 4 services active.

---

## Prior Session Changes (S62 — QHM Wiring)

| Commit | File | Fix |
|--------|------|-----|
| `002a38f` | `execution/entry_logic.py` | QHM symbol registry gate |
| `c24bcd8` | `execution/quarterly_hold_manager.py` | Earnings protection state machine (PENDING_EARNINGS) |
| `eb316e4` | `main.py` | QHM instantiation, reconcile_on_startup, not_before_date gate |
| `f88caa8` | `main.py` + `strategy/run_cycle.py` | RTH loop wiring — run_weekly_check(), maybe_enter_positions() with 10:05 AM ET gate |

---

## Current Bot State

| Item | Value |
|------|-------|
| Branch | `main` |
| HEAD | `625f751` (local) / `3cab1db` (OCI — exit_logic.py) |
| Mode | Paper trading, PDT enforcement disabled |
| Profile active | paper (MIN_SCORE=9/12, STOP=1.20×ATR, TARGET=2.5×ATR) |
| Kill switch | 7% |
| OCI services | mtf-bot active, mtf-writer active, mtf-http active, nginx active |
| Tranche system | TRANCHE_FRACS=[0.40, 0.60, 1.00], TRANCHE_SHARE=0.33, trail at T1 active |

---

## RC Bug Class Status (all CLOSED)

| RC | Class | Status |
|----|-------|--------|
| RC-1 | Naive datetime | CLOSED — 16 instances fixed 2026-04-28 |
| RC-2 | CWD-relative path | CLOSED |
| RC-3 | Silent exception | CLOSED |
| RC-4 | Estimated exit price | CLOSED |
| RC-5 | Non-atomic write | CLOSED — portfolio_tracker.py flush+fsync S59 |
| RC-6 | Wrong API field | CLOSED |
| RC-7 | Zero-share sizing | CLOSED |
| RC-8 | Unbounded scan buffer | CLOSED |

---

## Hotspot Files

| File | Patch Count | Risk | Open Items |
|------|-------------|------|------------|
| execution/portfolio_tracker.py | 46 | CRITICAL | NONE |
| main.py | 35 | CRITICAL | NONE |
| execution/exit_logic.py | 10 | HIGH | NONE — T3 fix applied 3cab1db |
| execution/entry_logic.py | 4 | HIGH | NONE |
| strategy/run_cycle.py | 11 | MEDIUM | NONE |
| execution/quarterly_hold_manager.py | 7 | MEDIUM | NONE |
| execution/orphan_manager.py | 0 | LOW | NONE |

---

## Open Architecture Items

- **QHM quarterly holds** — FULLY WIRED. NVDA/GOOGL added (not_before Jun 23 — entries attempted at 10:05 AM ET if not yet filled). GEV Jul 22, GE Jul 25, LLY Aug 7. Config: `data/state/quarterly_holds_config.json`.
- **VIX stop widening → continuous curve** — QUEUED (CLAUDE.md roadmap 2026-06-24). Replace static 25/30 thresholds with continuous linear function. Board vote required. File: `execution/risk_manager.py`.
- **Conviction thresholds → linear spline** — QUEUED (CLAUDE.md roadmap 2026-06-24). Replace cliff (10=half, 11=full) with `max(0, (score-9)/3)`. Board vote required. Files: `execution/entry_logic.py`, `execution/kelly.py`.
- **MRI startup staleness** — D5 applied (commit 0e597a8). Blocking refresh at startup.
- **TraderMonty breadth CSV** — data/breadth.py stub exists, not wired. Board vote required.

---

## Open Items / Pending Decisions

1. **VIX stop widening dynamization** — board vote session needed (Rules 7 dynamization).
2. **Conviction linear spline** — board vote session needed (Rule 9 dynamization).
3. **Merge `claude/gracious-keller-j1rvhl` → main** — STALE. All QHM commits are already on main. No separate branch exists locally.
4. **Deferred:** monthly_review.py month-over-month from Alpaca fills (DS flagged, low priority P3).

---

## Prior Session Context (do not re-derive)

- `trade_engine.py` CRITICAL desync: CONFIRMED FIXED
- `exit_logic.py` PDT references: CONFIRMED REMOVED
- `orphan_manager.py` QHM fix: CONFIRMED PRESENT (L125-148/L288-295)
- T3 silent skip (exit_logic.py L553): FIXED in 3cab1db — `_t3_pending` guard now allows qty_rem=1 through when tranche_lvl == len(TRANCHE_FRACS)-1
- Trail activation at T1: NOT FORBIDDEN — CLAUDE.md Rule 13 documents this permanently
- D1 forbidden logic audit: COMPLETE — board+Gro+GAI consensus documented; Rules 7+9 queued for dynamization
