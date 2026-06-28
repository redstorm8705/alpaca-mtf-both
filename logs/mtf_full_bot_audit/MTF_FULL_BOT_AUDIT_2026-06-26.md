# MTF FULL BOT AUDIT — JUNE 26

**Status:** ACTIVE — standing initiative, tracked persistently across sessions (not a one-off).
**Started:** 2026-06-27 (during a Saturday/market-closed window, S68 continuation).
**Mandate (Rafael, 2026-06-27):** Methodically audit the entire bot, file by file, starting
from `execution/portfolio_tracker.py` (today's P0 fix site) outward through its full
dependency graph, eventually covering every file in the bot. Per-file requirement:

1. **Line-by-line read** — every line classified as **STATIC** (fixed value/behavior,
   never changes at runtime) or **DYNAMIC** (computed, config-driven, state-dependent,
   or varies by branch/input). Not a compile check — a behavioral classification.
2. **Gro + GAI review the ENTIRE file** — chunked only for context-window reasons, never
   reviewed as isolated unrelated sections. Explicit mandate: hunt for cross-file impact
   and FUTURE issues not yet identified — not just re-confirm fixes already found.
3. **Board domain review** (cold, independent subagents per CLAUDE.md protocol).
4. **Cross-file impact** — for every file audited, identify what calls it and what it
   calls, and whether today's other files' findings change anything about it.

**Why now:** Rafael pushed back on the "wait for P0 stabilization" deferral from S68 —
correctly noting that market-closed weekend time is exactly the window to do this
properly via static analysis, rather than passively waiting for live trading days to
either confirm or fail to confirm the fix.

---

## Scope — File Inventory (Phase 0 + Phase 1, sized 2026-06-27)

Phase 0 = the file itself. Phase 1 = its direct callers (everything that imports it).
`portfolio_tracker.py` imports only stdlib — zero local-module dependencies upstream,
so the graph only grows in the caller direction from here.

| Phase | File | Lines | STATIC/DYNAMIC pass | Board | Gro | GAI | Cross-file impact noted | Status |
|---|---|---|---|---|---|---|---|---|
| 0 | `execution/portfolio_tracker.py` | 2002 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `execution/entry_logic.py` | 1678 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `execution/exit_logic.py` | 2182 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `execution/kelly.py` | 450 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `execution/orphan_manager.py` | 1442 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `execution/trade_engine.py` | 286 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `main.py` | 1068 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `run_movers.py` | 242 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `state/persistence.py` | 132 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `strategy/run_cycle.py` | 1669 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |
| 1 | `trade_logger.py` | 88 | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | NOT STARTED | QUEUED |

**Phase 0+1 total: 11,239 lines across 11 files.**

**Phase 2+ (not yet mapped):** every file imported BY the Phase 1 files, recursively,
until the entire bot is covered. Will be mapped once Phase 1 file list is confirmed
complete (need to grep each Phase 1 file's own import graph next).

---

## Findings Log

(Populated as each file's audit completes. Format: file | line(s) | STATIC/DYNAMIC |
finding | severity | board/Gro/GAI alignment.)

### `execution/portfolio_tracker.py` — Block 1 (lines 1-439): module setup + FIFO helpers

| Lines | Block | Classification | Notes |
|---|---|---|---|
| 10-23 | stdlib imports | STATIC | No local-module imports — confirmed leaf node upstream. |
| 27-31 | SSL context (certifi fallback) | STATIC at import time, but DYNAMIC in effect — `_SSL_CTX` is computed once at module load and reused for every HTTP call for the life of the process. If certifi's CA bundle is ever rotated/updated on disk, this process won't pick it up without a restart. Low risk, intentional. |
| 33-34 | `_ET`, `_PT` ZoneInfo constants | STATIC |
| 45-61 | `_BotEncoder` | STATIC class definition; `default()` method is DYNAMIC (branches on runtime type). Raises on unknown type — correct fail-loud design (`# raises — surfaces unknown types immediately`). |
| 65-72 | `_ROOT`, `sys.path` mutation, `trade_logger` import w/ fallback | STATIC paths computed once; the `except ImportError` fallback (`_log_event = lambda *a,**kw: pass`) is a **silent no-op fallback** — RC-3 adjacent. If `trade_logger.py` ever fails to import for a real reason (not just absence), logging silently goes nowhere with zero warning. **FINDING (LOW-MEDIUM):** add a `logger.warning` inside the except block so a broken import doesn't fail completely silently. |
| 75-77 | `_ALPACA_PAPER_BASE`, `_LOTS_STATE_FILE`, `_DRIFT_ALERT_FILE` | STATIC paths/URL. |
| **80-91** | `_load_drift_alert_date()` + `_last_eod_drift_alert_date` module global | **DYNAMIC, and this is the load-bearing pattern from today's bug class.** The global is initialized ONCE at import time (line 91 executes at module load) but then **mutated via `global` inside `write_eod_summary()`** later in the file. This is a "looks static (single assignment visible here), is actually dynamic shared mutable state across every call in the process" pattern — exactly the category that caused the lot-duplication bug. **FLAGGED FOR GRO/GAI:** confirm there is no second place in the file (or in any of the 10 Phase-1 caller files) that reads or mutates `_last_eod_drift_alert_date` directly via module access (`portfolio_tracker._last_eod_drift_alert_date`) bypassing the intended single mutation point. |
| **93-95** | `_eod_fifo_in_progress` module global | **DYNAMIC**, same category as above — today's reentrancy-guard fix. Same flag for Gro/GAI: confirm no other file reaches into this module-level name directly. |
| 100-138 | `_atomic_write()` | DYNAMIC (file I/O, branches on exceptions). Reviewed today as part of the P0 fix audit — RC-5 compliant (tmp+fsync+replace+.bak). No new finding. |
| 143-152 | `_fill_et_date()` | DYNAMIC (timestamp parsing). Fallback to raw string slice on parse failure (line 152) — silent-ish degradation (logs a warning first, not fully silent — acceptable). |
| **155-228** | `_fetch_alpaca_fills_for_date()` | DYNAMIC. **Known bug already logged today:** `after_id` pagination param is silently ignored by Alpaca when combined with `after`/`until` timestamp params — confirmed via bounded live test (page 2 identical to page 1). Loop would never terminate if any single day has ≥100 fills (currently impossible at this account's volume, but this is a real landmine for the future). **Also newly noted this pass:** lines 196-209, the retry loop sleeps `2**attempt` seconds (1s, 2s, 4s) **even on the final (3rd) failed attempt**, wasting 4 seconds before raising — cosmetic inefficiency, not a correctness bug, not worth a fix on its own. |
| **231-370** | `_fifo_reconstruct()` | DYNAMIC, the core matching algorithm. **Architectural asymmetry flagged for board awareness (not a new bug — a deliberate, already-approved design choice surfaced for explicit re-confirmation):** when a `buy_to_cover` exceeds available short lots (line 311-323), the code explicitly does NOT create a synthetic lot (S49 board decision, prevents phantom accumulation). But when a `sell`/`sell_short` exceeds available long lots (line 348-367), it DOES create a synthetic short lot. These are asymmetric responses to structurally the same failure mode (insufficient prior-lot history). The board's S49 rationale for the buy-side is documented in-code; there is no equivalent documented rationale in this file for why the sell-side behavior differs. **FLAGGED FOR BOARD:** re-confirm this asymmetry is still intentional, or consider whether the sell-side should also suppress the synthetic lot now that fill-ID dedup exists (today's fix already removes the *duplication* mechanism; this asymmetry is a separate, pre-existing design question about *missing-history* handling, not duplication). |
| 373-417 | `_load_prior_day_lots()` | DYNAMIC. Reviewed today as part of the P0 fix. No new finding this pass beyond what's already fixed. |
| 420-437 | `_save_open_lots_state()` | DYNAMIC. No new finding. |

**Block 1 status: STATIC/DYNAMIC pass complete, reviewed independently by Gro + GAI, every AI-sourced finding individually verified against the actual source before being logged below (not rubber-stamped).**

### Block 1 — Gro/GAI independent review results

**Verified REAL findings (confirmed against actual source, not just AI assertion):**
| # | Finding | Lines | Severity | Source |
|---|---|---|---|---|
| 1 | Silent no-op fallback if `trade_logger` import fails — no warning logged, just a pass-through stub | 69-71 | LOW-MEDIUM | Claude (verified), confirmed independently by both Gro and GAI |
| 2 | Two module-level mutable globals (`_last_eod_drift_alert_date`, `_eod_fifo_in_progress`) initialized once at import but mutated later via `global` — "looks static, is dynamic shared state," the exact category that caused today's bug | 91, 95 | INFORMATIONAL (already correctly handled by today's fix; flagged for awareness across the rest of the codebase) | Claude, confirmed by both Gro and GAI |
| 3 | Retry loop sleeps `2**attempt` seconds even after the final failed attempt, before raising — wastes ~4s | 196-209 | COSMETIC | Claude, confirmed by both Gro and GAI |
| 4 | Asymmetric synthetic-lot handling: `buy_to_cover` overflow suppresses synthetic lot creation (documented S49 board decision), but `sell` overflow creates a synthetic short lot with no equivalent documented rationale for the difference | 311-323 vs 348-367 | MEDIUM — flagged for board re-confirmation, not a confirmed bug (pre-existing, deliberate-looking design, but undocumented asymmetry) | Claude, confirmed and extended by both Gro and GAI — GAI specifically noted this could reproduce the same "phantom accumulation across restarts" problem the S49 decision was meant to prevent, just on the short side instead of the long side |
| 5 | `_atomic_write()`: if the post-write `tmp_path.unlink()` cleanup itself fails after an earlier exception, the orphaned `.tmp` file persists on disk | 130-138 | LOW (disk clutter only — `os.replace()` is still atomic, so this cannot corrupt the live data file; confirmed by re-reading lines 112-129: the replace either fully succeeds or never runs) | Gro (new), verified accurate by Claude re-read |
| 6 | `_BotEncoder.default()` imports `uuid` inside the method body (re-attempted on every call for an unknown type) rather than at module level | 55-59 | LOW (trivial overhead, not a correctness issue) | GAI (new), verified accurate by Claude re-read |
| 7 | **Date-string timezone ambiguity:** `write_eod_summary()`'s `today` variable (lines 787, 1873) is computed in PT (`datetime.now(_PT).strftime("%Y-%m-%d")`), then passed into `_fetch_alpaca_fills_for_date(date_str)`, which interprets that same YYYY-MM-DD string as an **ET** calendar day internally (`et_start = ...replace(tzinfo=_ET)`). A PT calendar day and an ET calendar day with the same date string do NOT cover the same absolute time window (PT is 3h behind ET) — a fill between 9pm-midnight PT would be in ET's *next* calendar day but would still carry today's PT date string, so it could be queried against the wrong ET window and silently missed for that day's reconstruction. | 787 + 167-169 (cross-reference) | **MEDIUM, not CRITICAL as GAI initially rated it** — verified the bot's own AH cutoff is 10:00 PM ET (7:00 PM PT, confirmed elsewhere in codebase), which is BEFORE the 9pm-midnight PT danger window opens, so this bot's own trading activity cannot currently fall into the affected window. Real structural inconsistency, not an active production bug at current operating hours — but would become one if AH trading hours were ever extended later, or for any external/manual fill timing. | GAI (new, originally rated CRITICAL) — downgraded to MEDIUM by Claude after verifying actual AH cutoff times against the danger window; GAI's underlying structural observation is accurate, its severity assessment was not grounded in the bot's actual operating hours |

**REFUTED findings — claimed by an AI reviewer but factually contradicted by the actual source (logged here as evidence the audit's verification discipline is working, not omitted):**
| Claimed finding (Gro) | Verification | Result |
|---|---|---|
| "`_load_drift_alert_date` does not handle potential exceptions" | Re-read lines 80-88: the function has a `try/except Exception as _e: logger.warning(...)` wrapping the entire read+parse | **FALSE** — exception handling is present and correct |
| "Potential Division by Zero in `_fifo_reconstruct`" | `grep` for `/` operators within lines 231-370: zero division operations exist anywhere in the function (only multiplication, e.g. `(price - lot["price"]) * sell`) | **FALSE** — no division exists in this function at all |
| "`_fill_et_date` lacks input validation" (implied unsafe) | Re-read lines 143-152: function already wraps the parse in try/except with a length-checked string-slice fallback on failure | **OVERSTATED** — already has graceful degradation; not a real gap |

**This refutation step is itself a finding worth recording: 2 of Gro's 4 "new findings" this round were hallucinated against code that directly contradicts them, and 1 of GAI's findings had its severity overstated relative to the bot's actual operating constraints. Every AI-sourced claim in this audit will continue to be independently verified against the literal source before being logged as a confirmed finding — consistent with the hallucination pattern already documented twice elsewhere this session (the Gemini-prompt audit work).**

**Remaining in `portfolio_tracker.py`:** Block 2 (lines 440-776, class init + trade log I/O + unverified-exit handling), Block 3 (lines 777-1367, `write_eod_summary` — already heavily audited today but due for a fresh non-bug-hunting pass per the mandate's "whole file" requirement), Block 4 (lines 1368-1872, core trade lifecycle API — record_entry/record_exit/etc., NOT YET touched by today's fix, highest-value remaining target since it's the most-called public surface), Block 5 (lines 1872-2002, stats/reporting). **NOT YET STARTED.**

---

## Session Log

**2026-06-27 (S68 continuation):** Initiative created. Scope confirmed with Rafael, board,
Gro, and GAI — all four voices explicitly confirmed understanding with zero ambiguity
flagged. Dependency graph mapped for Phase 0/1 (11 files, 11,239 lines). Block 1 of
`portfolio_tracker.py` (lines 1-439) completed this session — see Findings Log above.
Blocks 2-5 of this same file, plus all 10 Phase-1 files, remain. This is a multi-session
effort by design — flagging explicitly rather than implying false completeness.
