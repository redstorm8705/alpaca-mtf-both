# Handoff — S68: QHM external-close fix + DS→Gro migration (2026-06-27)

## LATEST CHANGES (S68)

| Commit | File | Fix |
|--------|------|-----|
| `dff0704` | `execution/quarterly_hold_manager.py` | QHM external-close detection gap: `_detect_external_close()` supported PENDING_STOP_REPLACE/PENDING_EARNINGS but was never called for them by `reconcile_on_startup()` or `run_weekly_check()`. NVDA/GOOGL were stranded at stale PENDING_STOP_REPLACE (qty_filled=1) while Alpaca showed 0 shares. Fix verified live 19:11 ET — both self-healed CLOSED→PENDING_ENTRY automatically on restart, no manual edit needed. |
| `6457394` | `CLAUDE.md` | DS/DeepSeek→Gro/Groq migration. Live audit pipeline (`auto_ai_audit.py`, `autonomous_review.py`) already moved to Groq (`llama-3.3-70b-versatile`); CLAUDE.md was stale and caused a failed DeepSeek curl call this session ("Insufficient Balance" — deprecated/unfunded key). Renamed throughout protocol sections (lines 1–928); historical roadmap log entries left untouched as accurate history. |
| `967b550` | `logs/` | Triaged and deleted 8 confirmed-resolved `queued_for_review_*`/`pending_approvals_*` files (cross-checked against current code, not file age). Kept 1 genuinely open item: `scan_to_html.py` RC-9 yfinance-news violation, still present at L1226-1281. |

**Also found, not yet fixed (next priority):** `resubmit_stop_if_needed()` in `quarterly_hold_manager.py` is dead code — defined, never called from `main.py` or `run_cycle.py`. No mechanism currently resubmits a missing QHM GTC stop. Needs its own scope decision (which AH window, what cadence) before patching.

**NotebookLM Master Brain:** still unauthenticated — `notebooklm login` requires Rafael to complete Google OAuth in the CLI's own Playwright browser profile (`~/.notebooklm/browser_profile`, separate from regular Chrome) and press Enter in terminal. Cannot be done by Claude.

## Prior Session (S67): MRI yfinance T4 fallbacks for VIX + JPY (2026-06-25)

| Commit | File | Fix |
|--------|------|-----|
| `98f704e` | `events/macro_risk_index.py` | yfinance T4 fallbacks: VIX (when FMP None + cache stale >30min) and JPY (when FMP USDJPY fails). VIX scored but _vix_confirmed=False → news still capped 20pts. JPY scores 0pts, stores snapshot for observability. |

## Prior Session Changes (S66 Autonomous)

| Commit | File | Fix |
|--------|------|-----|
| `ee7496a` | `events/macro_risk_index.py` | VIX 30-min fallback cache + _vix_confirmed flag + news_alerts cap 20pts when VIX absent + TOCTOU fix (gate inside lock) |
| `d81e060` | `strategy/run_cycle.py` | BV-5: remove STRESSED from hard-block — restores 2026-06-13 board decision accidentally reverted in f88caa8 |

**Root cause fixed (S66):** Jun 25 GEO_ENERGY event — FMP VIX=None → oil5+gold5+news35=45=STRESSED → BV-5 blocked ALL entries including shorts. Both patches together prevent recurrence.

## Prior Session Changes (S65 — Layer 9: 16pt confirmation gate)

| Commit | File | Fix |
|--------|------|-----|
| `73b2bc0` | `strategy/run_cycle.py` | Layer 9: 16pt confirmation gate — score-10 signals require score_16pt >= 11; score-11/12 unaffected |

**Branch:** `main`
**OCI HEAD:** `dff0704`
**QHM status:** NVDA + GOOGL both CLOSED (external close, pre-dated the Jun 27 fix) → re-added fresh as PENDING_ENTRY (0 shares, no thesis state) as of 2026-06-27 19:11 ET. Both eligible for a normal Day-1 gate entry attempt on the next qualifying day; tranche history reset to 0/3.
**OCI note:** All 4 services active post-restart (mtf-bot, mtf-writer, mtf-http, nginx). quarterly_hold_manager.py deployed and byte-verified identical to repo.

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

- **QHM quarterly holds** — FULLY WIRED. NVDA/GOOGL added. GEV Jul 22, GE Jul 25, LLY Aug 7. Config: `data/state/quarterly_holds_config.json`.
- **VWAP SD bands** — LIVE as of S64 (00b216d). Extended entries now score 0pt VWAP → drop below 10pt threshold → blocked. Shadow score_comparison data will show impact going forward.
- **16pt confirmation gate (Layer 9)** — LIVE as of S65 (73b2bc0). Score-10 signals require score_16pt >= 11 to proceed. Score-11/12 unaffected. Data: score-10 WR=0% (0W/4L), 61% rejected by 16pt. 30-day P1 review: if score-10+16pt signals show WR < 35%, re-evaluate threshold.
- **Volume confirmation (Fix A)** — ON HOLD until ~Jul 11 (60-session CPCV condition: 27/60 sessions met). Board 4/4 REJECT premature activation.
- **GEX greeks/OI** — URLs fixed (S64). Greeks still absent from v1beta1 snapshots (OPRA agreement required). GEX computation returns zero/neutral until resolved. GEX_ENABLED=False unchanged.
- **VIX stop widening → continuous curve** — LIVE as of S64 (commit `7e5c983`). Continuous `mult = 1.0 + max(0, vix-20) * 0.1`, capped at 2.0x.
- **Conviction thresholds → linear spline** — REJECTED by board 4-0 + GAI (S64). Board: Kelly double-count; score measures confluence not edge. Archived from roadmap.
- **MRI startup staleness** — D5 applied (commit 0e597a8).
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
