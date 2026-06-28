# Handoff — S68: P0 FIFO lot-duplication fix + data remediation (2026-06-27)

## LATEST CHANGES (S68)

| Commit | File | Fix |
|--------|------|-----|
| `d74726d` | `execution/portfolio_tracker.py` | **P0 CRITICAL**: `write_eod_summary()` had no idempotency across its 6 daily call sites — every call re-ran FIFO lot reconstruction over the full day's fills with no fill-ID dedup, so any same-day-unclosed position accumulated a duplicate lot on every call. Confirmed live: AMD 36 dup lots, PANW 77, SMCI 60, NVDA had a 6-week-stale lot that caused a real EOD P&L drift. Fixed with fill-ID dedup + same-thread reentrancy guard (GAI caught a real gap Gro missed in round 1; round 2 approved by both). |
| (data remediation, no code commit) | `data/state/open_lots_prior_day.json` (OCI) + `logs/eod_2026-06-27.json` (OCI) | Rebuilt the corrupted lot file from a clean 83-day historical FIFO pass (307 fills, 0 remaining lots — matches live account's confirmed-flat 0 positions). Cross-checked against Alpaca's own equity ledger (base_value=$2,500.00, equity=$2,801.55 → true P&L=$301.55) — rebuild's $302.08 within $0.53. Corrected the stale cumulative baseline ($292.22 → $301.55) so future days don't inherit the old error. GAI rejected round 1 (demanded independent verification), approved round 2 after the equity cross-check + a pagination-defect investigation. Both originals backed up on OCI before any edit. |
| `dff0704` | `execution/quarterly_hold_manager.py` | QHM external-close detection gap fixed — NVDA/GOOGL self-healed CLOSED→PENDING_ENTRY automatically, verified live. |
| `6457394` | `CLAUDE.md` | DS/DeepSeek→Gro/Groq migration — live pipeline already used Groq, docs were stale. |
| `967b550` | `logs/` | Deleted 8 confirmed-resolved stale to-do files. |

| `2f479bb` | `nightly_audit.py`, `midday_audit.py` | **Gemini audit prompt redesign** — root-caused today's PDT hallucination: both scripts' system prompt literally said "PDT-constrained" though PDT was deleted weeks ago. Also found the hand-typed CONFIG CONSTANTS block had drifted from real config.py for months (wrong names AND wrong values — e.g. claimed MIN_SCORE=9/KELLY_FRACTION=0.25, real paper-profile values are MIN_LONG_SCORE/MIN_SHORT_SCORE=10/KELLY_FRACTION=0.35). Added `_build_config_constants_block()` to both — resolves constants live from config.py at report-build time instead of a stale snapshot. Added hallucination-prevention rules (cite exact source, never infer state/function names, never mix fields across different trade records) and a `CATASTROPHIC ALERT` section that comes first in the output so severe findings can't be buried. Also fixed ~45 pre-existing lint/type errors in midday_audit.py (file had never been linted) per RULE C-4. Deployed + verified on OCI. |
| (no code commit — plan only) | `execution/portfolio_tracker.py` decomposition | Rafael requested decomposing the 1993-line hotspot file. Board+Gro+GAI reviewed a 2-phase plan: Phase 1 (low-risk) extracts FIFO/fills helpers → `eod_fifo.py` and trade-log I/O → `trade_log_io.py`; Phase 2 (higher-risk) extracts the 590-line `write_eod_summary()` body into `eod_summary.py`. Both reviewers independently recommended waiting 3-5 trading days for today's P0 fix to prove stable before starting Phase 1, and never combining phases into one deployment. **Plan ready, execution deferred — needs Rafael's go-ahead on timing.** |

**NEW finding, not yet fixed (P2, logged):** `_fetch_alpaca_fills_for_date()` in `portfolio_tracker.py` has a latent pagination bug — Alpaca's `/v2/account/activities/FILL` endpoint ignores `after_id` when combined with `after`/`until` params, so any single day with >100 fills would loop forever (confirmed via bounded test: page 2 was byte-for-byte identical to page 1). Did not affect this remediation (max daily fill count in this account's history was 18). Not urgent at current trading volume, but should be fixed before volume could ever approach 100 fills/day.

**Non-blocking follow-up:** `midday_audit.py:332` has a harmless dead `pdt_at_entry` field in `run_signal_postmortem()` — always 0, never reaches the Gemini prompt, not removed pending verification of an external postmortem-skill consumer's schema.

**Still open from S67:** `resubmit_stop_if_needed()` in `quarterly_hold_manager.py` is dead code — never called from `main.py` or `run_cycle.py`. No mechanism resubmits a missing QHM GTC stop.

**NotebookLM Master Brain:** RESOLVED — Rafael completed `notebooklm login` this session. Stale `project-state.md` duplicates (6 of them) cleaned up; fresh state pushed.

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
