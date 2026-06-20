# Handoff — S62 QHM Wiring Complete (2026-06-20)

## LATEST CHANGES (this session — QHM 4-file wiring)

| Commit | File | Fix |
|--------|------|-----|
| `002a38f` | `execution/entry_logic.py` | FILE 1: QHM symbol registry gate — blocks intraday entries on QHM symbols |
| `c24bcd8` | `execution/quarterly_hold_manager.py` | FILE 2: Full earnings protection state machine (PENDING_EARNINGS), DataFrame access fix (bars.iloc), FMP earnings gate |
| `eb316e4` | `main.py` | FILE 3: QHM instantiation, reconcile_on_startup, not_before_date candidate gate, SIGTERM/KB safe_stop |
| `f88caa8` | `main.py` + `strategy/run_cycle.py` | FILE 4: RTH loop wiring — run_weekly_check() each cycle, maybe_enter_positions() with 10:05 AM ET gate |

**Branch:** `claude/gracious-keller-j1rvhl`
**OCI HEAD:** `f88caa8` — all 4 files rsynced and services restarted (confirmed active)
**QHM status:** LIVE — loaded 4 picks from `data/state/quarterly_holds_config.json`, 0 positions (all not_before_date future-dated, first entries start Jul 22)

### QHM startup log (confirmed working):
```
QuarterlyHoldManager: loaded config from ...quarterly_holds_config.json (4 picks)
QuarterlyHoldManager init: 0 positions loaded, dry_run=False
QHM reconcile: 0 symbol(s), 0 order(s) verified, ...
```

### What QHM does each RTH cycle now:
- `run_weekly_check()` after check_partial_exits — monitors earnings gate for ACTIVE positions
- `maybe_enter_positions()` at 10:05 AM ET gate, before EXTREME block — places Day limit tranches for due candidates (GEV Jul 22, GE Jul 25, LLY Aug 7)

---

## Prior Session Changes (S61 autonomous)

| Commit | File | Fix |
|--------|------|-----|
| `eb6a5ac` | `generate_dashboard.py` | Dashboard lifetime P&L uses equity-based sourcing (equity − $2,500 = **+$307.38**) |

---

## Current Bot State

| Item | Value |
|------|-------|
| Branch | `claude/gracious-keller-j1rvhl` |
| HEAD | `f88caa8` |
| Mode | Paper trading, PDT enforcement disabled |
| Profile active | paper (MIN_SCORE=9/12, STOP=1.25×ATR, TARGET=2.5×ATR) |
| Kill switch | 7% |
| OCI services | mtf-bot active, mtf-writer active, mtf-http active, nginx active |

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
| main.py | 35 | CRITICAL | NONE — QHM wiring complete |
| execution/exit_logic.py | 9 | HIGH | NONE |
| execution/entry_logic.py | 4 | HIGH | NONE — QHM gate added |
| strategy/run_cycle.py | 11 | MEDIUM | NONE — QHM wired |
| execution/quarterly_hold_manager.py | 7 | MEDIUM | NONE — earnings state machine complete |
| execution/orphan_manager.py | 0 | LOW | NONE |

---

## Open Architecture Items

- **QHM quarterly holds** — FULLY WIRED as of f88caa8. Next entries: GEV Jul 22, GE Jul 25, LLY Aug 7. config at data/state/quarterly_holds_config.json (4 picks — Rafael already created). Short-direction support deferred (only long in _resubmit_post_earnings_stop).
- **MRI startup staleness** — D5 applied (commit 0e597a8). Blocking refresh at startup.
- **TraderMonty breadth CSV** — data/breadth.py stub exists, not wired into scoring. Board vote required.

---

## Open Items / Pending Decisions

1. **T1 tranche restructure — QUEUED** (`logs/queued_for_review_2026-06-16.md`):
   - A: Is enabling trail activation at new T1 (TRANCHE_FRACS[0]=0.40) a forbidden "stop-loss calculation logic" change?
   - B: T3 silent skip for qty_orig=3 positions: acceptable or fix L587?
   - C: GAI sign-off still required per RULE C-2

2. **Merge branch** — `claude/gracious-keller-j1rvhl` has all QHM commits. Merge to main when ready.

3. **Deferred:** monthly_review.py month-over-month from Alpaca fills (DS flagged, low priority).

---

## Prior Session Context (do not re-derive)

- `trade_engine.py` CRITICAL desync: CONFIRMED FIXED
- `exit_logic.py` PDT references: CONFIRMED REMOVED
- `orphan_manager.py` QHM fix: CONFIRMED PRESENT (L125-148/L288-295)
- All pending_approvals_2026-06-07.md items: STALE — no action required
