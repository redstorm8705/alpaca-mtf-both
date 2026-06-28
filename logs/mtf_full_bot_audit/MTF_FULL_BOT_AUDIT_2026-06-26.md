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

### `execution/portfolio_tracker.py` — Block 2 (lines 440-776): class init + trade log I/O + unverified-exit handling

| Lines | Block | Classification | Notes |
|---|---|---|---|
| 441-448 | `__init__` | DYNAMIC (per-instance state) | `self._traded_today_date` (line 445) captured once at construction in PT — note this is only re-synced elsewhere (need to check Block 4) since the bot process is long-running and `__init__` runs once at startup, not daily. Not a finding by itself — flagging for cross-check during Block 4/5 review of whatever resets `traded_today`. |
| 452-508 | `_load_log()` | DYNAMIC, read-only (loads from disk, never writes). Verified: the `len(_disk_closed) >= len(self.closed_trades)` guard (line 458) is NOT dead code — confirmed via grep that `_load_log()` is called a second time, inside `write_eod_summary()` (line 791), specifically as a forced reload. The guard protects against a reload regressing a longer in-memory list to a shorter disk one. **This second call site is the origin of the major Block 2/3 finding below.** |
| 510-516 | `_save_log()` | DYNAMIC, atomic write via `_atomic_write`. No new finding. |
| 518-566 | `get_unverified_exits()` | DYNAMIC, time-windowed filtering with a documented eviction fix (T1). Division-free, no new finding. |
| 568-731 | `patch_exit_pnl()` | DYNAMIC. Well-guarded: explicit `is not None` sentinel handling for legitimate-zero vs absent fields (BUG-1 fix, documented), explicit zero/negative entry-price guard before any division (line 653, 683) — **verified no unguarded division exists in this function**, contradicting the pattern of unverified claims seen in Block 1. No new finding. |
| 733-773 | `mark_fill_expired()` | DYNAMIC, time-windowed, consistent with `get_unverified_exits()`'s cutoff logic. No new finding. |

### MAJOR FINDING — `write_eod_summary()` reentrancy guard is incomplete (spans Block 2→3 boundary)

**Severity: DISPUTED — Gro rates HIGH, GAI rates MEDIUM. Both confirm the finding is real; they disagree on severity AND on the correct fix. This is a genuine board-tie-breaker situation, logged as such rather than resolved unilaterally.**

**The finding:** today's P0 fix added a reentrancy guard (`_eod_fifo_in_progress`) around ONLY the Alpaca-FIFO subsection of `write_eod_summary()` (~lines 857-1032 of the ~590-line method). Two other reentrancy-exposed regions exist in the SAME method, untouched by today's guard:

1. **Lines 791-855 (before the guard):** `self._load_log()` force-reloads `self.closed_trades`/`self.open_trades` — shared mutable state on the same `PortfolioTracker` instance across an original call and any SIGTERM-triggered reentrant call (confirmed real reentrancy mechanism, not theoretical: `_fetch_alpaca_fills_for_date()` blocks on network I/O; Python delivers signals by interrupting blocking syscalls and running the handler in the same thread before the original call resumes).
2. **Lines 1034-1337 (after the guard, "Phase 2a.5" reconciliation):** calls `self.record_exit()` and `self._save_log()` multiple times — but verified via the gate condition at line 1206 (`if _alpaca_pnl is not None and not _a4_gap:`) that this block is **unreachable by a reentrant call** (reentrant calls always have `_alpaca_pnl = None` since they skip the guarded FIFO section that's the only place it's set). Both Gro and GAI independently confirmed this gating analysis — this part of the original concern is refuted as non-exploitable.
3. **Line 1351, unconditional:** `_atomic_write(eod_path, summary)` — reached by BOTH the original and any reentrant call. The reentrant call's `summary` contains only tracker-fallback P&L (`_pnl_today = _tracker_pnl`, since `_alpaca_pnl is None`) — a strictly less-authoritative number than what the original call will eventually compute once its blocked network call resolves.

**The scenario:** `main.py`'s SIGTERM handler calls `tracker.write_eod_summary()` then proceeds with other shutdown steps and the process exits shortly after. If the SIGTERM arrives while the original call is blocked inside `_fetch_alpaca_fills_for_date()`, the reentrant call runs to completion first (inside the signal handler) and writes a tracker-fallback-only `eod_{today}.json`. If the process exits before the original (interrupted) call ever resumes and writes its more complete version, **the degraded tracker-fallback summary becomes the final persisted EOD report for that day** — not data corruption (the lot-state file itself remains protected), but a real accuracy/completeness compromise in a daily financial report.

**Where Gro and GAI disagree:**
- **Gro:** severity HIGH; recommends widening the reentrancy guard to wrap the ENTIRE `write_eod_summary()` method body.
- **GAI:** severity MEDIUM (no data corruption, lot-state integrity preserved, only a report-accuracy issue); explicitly identifies that Gro's recommended fix would **make things worse**: a full-function guard means a SIGTERM-triggered reentrant call would hit the guard immediately and skip everything — including the final `_atomic_write` — meaning **no `eod_{today}.json` would be written at all** during that shutdown path if the original call hasn't finished. GAI rates "missing EOD file" as worse than "degraded EOD file" and proposes a more surgical alternative instead (a minimal, disk-load-free "emergency EOD" path for the reentrant case, or a short bounded wait for the original call to finish before falling back).

**Decision: NOT FIXED THIS SESSION.** This needs explicit resolution of the Gro/GAI disagreement (board counter-prompt + majority per CLAUDE.md's tie-breaker protocol) and careful design of whichever fix direction wins, before any patch is drafted — exactly the kind of finding that justifies doing this audit properly rather than assuming today's P0 fix was complete. Logged here as the audit's first concrete vindication of slowing down.

### RESOLVED — 2026-06-28, via Gro/GAI counter-prompt (the disagreement above is moot; the underlying premise was wrong)

Rather than resolving "which fix wins" (widen the guard vs. GAI's emergency-EOD-path alternative), counter-prompted both Gro and GAI with a deeper question while auditing `main.py`: does the original interrupted call to `write_eod_summary()` actually ever "resume" after the SIGTERM handler runs, as the scenario in point 1 above assumes?

**Answer: no.** `main.py`'s `_handle_sigterm()` ends with `sys.exit(0)`, which raises `SystemExit` — a `BaseException` subclass, not caught by any of the `except Exception` clauses anywhere in the call chain (verified via grep: zero bare `except:`/`except BaseException:` in `portfolio_tracker.py`). Under CPython's signal-handling model, a handler that raises (rather than returns normally) does not let the originally-interrupted code resume — the exception unwinds the *entire* stack of whatever was executing when the signal was noticed, terminating the process. **Both Gro and GAI, independently, reversed their original positions after this analysis** and agreed: the "original call resumes and could overwrite the reentrant call's work" scenario this finding (and the original P0 guard) was built around is not reachable via SIGTERM in this codebase. Both recommended removing the guard entirely as solving a non-problem.

**Decision (Rafael, 2026-06-28): keep the guard as harmless defense-in-depth, fix the misleading comment rather than remove protective code on AI reasoning alone.** Applied in commit `1cc7509` — the comment now accurately states that `sys.exit(0)` prevents resumption, rather than asserting the (incorrect) original premise. No functional code change; the boolean check remains in place.

**Net effect on the original dispute:** Gro's "widen to whole function" vs. GAI's "surgical emergency-path" disagreement is no longer relevant — both proposed fixes were solving for a race that doesn't exist via this mechanism. The deterministic (not probabilistic) sub-finding that DOES still stand: if SIGTERM interrupts an original `write_eod_summary()` call mid-FIFO-fetch, the SIGTERM handler's own call to the same function — running to completion in the same signal-handler invocation before `sys.exit(0)` — will *always* persist a tracker-fallback-only (less authoritative) EOD report for that day, since the original call never gets a chance to finish and overwrite it with the more complete Alpaca-FIFO version. This is a known, accepted limitation of process termination mid-report rather than a reentrancy bug — not pursued further this session.

---

### MAJOR FINDING #2 — cumulative P&L lookback loop breaks on file-exists, not on successful-parse (CONSENSUS, no debate — fix not yet applied)

**Severity: Gro rates CRITICAL, GAI rates HIGH. Both independently propose the IDENTICAL fix. No disagreement this time — logged as ready-to-fix, held pending a consolidated batch per Rafael's "audit first, fix once we have the full picture" call.**

**Location:** `write_eod_summary()`, the 7-day cumulative-total lookback loop (~lines 982-1006).

**The bug, verified via exact indentation measurement (not just visual read):** the loop walks backward up to 7 days looking for the most recent prior-day `eod_{date}.json`. The `break` statement sits inside the `if _prev_eod_path.exists():` block but OUTSIDE the `try/except` — meaning the loop exits as soon as it finds a file that **exists on disk**, regardless of whether that file successfully parses. If the most recent existing prior-day file is present but corrupted/unparseable, the `except` only logs a warning (doesn't re-raise, doesn't `continue`), and the unconditional `break` still fires — so `_alpaca_cumulative` stays `None` through loop exit, and the post-loop fallback (`if _alpaca_cumulative is None: _alpaca_cumulative = _alpaca_pnl`) silently resets the cumulative figure to just that single day's P&L, as if it were the first trading day ever — discarding months of history, with only a warning log (no Slack alert, no escalation), and never trying any of the up-to-6 older files still within the lookback budget.

**Why this matters concretely:** this is the exact same `all_time_stats.total_pnl` field that needed a manual $9.33 correction earlier today during the P0 data remediation (the stale $292.22 → $301.55 fix). This loop is the mechanism that propagates that field forward day to day — a single bad file anywhere in the chain silently breaks the chain going forward, with no loud failure signal.

**Fix (Gro and GAI both proposed, independently, identically):** move the `break` to inside the `try` block, immediately after the successful `_alpaca_cumulative = round(...)` assignment, so the loop only stops on a *successful* load — a corrupted file is skipped (loop continues to older files) rather than treated as a terminal "no history exists" signal.

**Decision: finding confirmed by consensus, fix NOT applied this session** — held for a consolidated fix batch once the audit has a fuller picture, per Rafael's explicit instruction.

---

**Remaining in `portfolio_tracker.py`:** Block 3's remaining unreviewed logic (score_16pt_buckets and score_comparison parsing sections, lines ~1107-1193 — reviewed this pass, both confirmed properly guarded against empty-list division by zero, no new finding there), Block 5 (lines 1872-2002, stats/reporting). **Block 3 STATIC/DYNAMIC pass + Gro/GAI review: COMPLETE. Block 4: COMPLETE (below). Block 5: NOT YET STARTED.**

### `execution/portfolio_tracker.py` — Block 4 (lines 1368-1878): core trade lifecycle API

**This is the highest-traffic public surface in the entire codebase — every other file in the dependency graph calls these methods directly on the shared tracker instance.**

**Confirmed REAL findings (independently verified, not just AI-asserted):**

| # | Finding | Lines | Severity | Source |
|---|---|---|---|---|
| 1 | `record_entry()` unconditionally does `self.open_trades[symbol] = {...}` with NO check for whether `symbol` already has an open or pending trade. A duplicate call (bug elsewhere, race) silently clobbers the prior trade dict with zero warning — total loss of that trade's state (GTC order ID, partial-exit history, reversal counters, everything). Inconsistent with `promote_pending_to_active()` a few lines later, which DOES guard against exactly this class of duplicate call. | ~1385 | **HIGH** | Claude, confirmed independently by both Gro and GAI |
| 2 | `update_trail_stop()` mutates `trade["trail_stop"]` but never calls `self._save_log()` — every other state-mutating method in this class does. Traced all 5 call sites in `exit_logic.py`: a `_save_log()` does appear later in each surrounding code block, so this is not a confirmed data-loss bug today, but it is a real persistence-integrity gap — any crash/exception between the trail-stop update and that later save loses the ratchet, reverting to a stale (less protective) stop on restart. | ~1630 | **MEDIUM** | Claude, confirmed by both Gro and GAI — GAI specifically: "If an unhandled exception or system crash occurs between `update_trail_stop()` and the subsequent `_save_log()`... stale stop levels... missed stop exits." Flagged for follow-up verification when `exit_logic.py` gets its own full audit pass (Phase 1). |
| 3 | `record_stop_breach_blocked()`: `active_stop = trade.get("trail_stop") or trade.get("stop")` — if BOTH are `None`, the f-string `f"...${active_stop:.2f}"` raises `TypeError`. GAI correctly did the math and refuted my own over-broad framing: `stop=0.0` would NOT trigger this (`"{:.2f}".format(0.0)` is valid) — only a genuine `None` on both fields would. `stop` is a required, always-populated parameter at entry, so this needs a corrupted/malformed trade object to trigger — low likelihood, trivial fix. | ~1860 | **LOW** | Claude (initial framing partially corrected by GAI's more precise math) |
| 4 | `record_partial_exit()` has NO guard against `entry_price` being `None` or `0.0` before using it in P&L arithmetic (`pnl = (exit_price - entry) * qty_closed`), unlike `record_exit()`, which explicitly guards this exact case (the BUG-5 fix, lines 1671/1732, forces `pnl=0.0` and sets `_fill_unverified=True` rather than letting a None entry crash or a zero entry produce phantom gross-proceeds-as-profit). `record_pending_entry()` confirms `entry_price` starts as `None` until promotion. If a partial-exit signal ever fires on a trade that hasn't been promoted from `pending_overnight` yet (a sequencing bug, not currently known to exist, but not structurally prevented at this layer either), this would either crash (`None` case) or silently misrecord P&L (`0.0` case) — the exact same BUG-5 failure mode `record_exit()` was already patched against, just unpatched in this sibling method. | ~1573-1606 | **MEDIUM** (no known live trigger path, but the missing guard is a genuine asymmetry with an already-fixed sibling bug) | GAI (new), independently verified by Claude — confirmed `record_pending_entry()` sets `entry_price=None` and `record_exit()`'s equivalent guard exists at lines 1671/1732 with no counterpart in `record_partial_exit()` |
| 5 | `is_in_trade()` lazy daily reset of `traded_today`/`_traded_today_date` | 1872-1877 | **NONE — verified clean.** This closes the Block 2 cross-check flag (was `_traded_today_date` ever re-synced after `__init__`? Yes, here, lazily on next call after a PT date rollover. Correctly scoped, only read/written in this one method.) | — |

**REFUTED findings — claimed by an AI reviewer but factually contradicted by the actual source:**

| Claimed finding | Source | Verification | Result |
|---|---|---|---|
| "`record_exit()` does not validate `qty` could be negative/zero before P&L calc" | Gro | Re-read line 1694: `qty = max(0, min(qty, _original_qty))` — explicit clamp exists directly above where Gro claimed it was missing | **FALSE** |
| "`_LOG_STOP_REASONS` is undeclared in `record_exit()` — guaranteed `NameError`, rated CRITICAL" | GAI | `grep` confirms it's imported at module level, line 68 (`from trade_logger import ... _STOP_REASONS as _LOG_STOP_REASONS`), with a fallback `frozenset()` at line 71 if the import fails — already covered and verified clean in Block 1. GAI only saw the Block 4 line range I sent it, not the module-level imports, and asserted a crash with no qualifier. | **FALSE** — artifact of a context slice, not a real bug |
| "Inconsistent logging in `record_exit()` — doesn't validate `reason` parameter" / "lack of validation in `update_trail_stop()` for `new_trail_stop` range" | Gro | Both are generic, no concrete failure mode demonstrated, no actual crash or data-corruption path traced | **NOT LOGGED** — too speculative to count as a finding; noting the pattern (Gro tends toward generic "add more validation" suggestions without a demonstrated trigger) for calibration in later blocks |
| "Possible `TypeError` if `mri_level` is a non-string type" | GAI | Speculative — `mri_level` is always a string constant from the MRI subsystem's own level enum throughout the codebase; no demonstrated call site passes a non-string. Lower confidence than the other findings. | **NOTED, not elevated** — kept at LOW/speculative, not given a severity row above |
| "Redundant `qty_remaining` update after `self.open_trades.pop(symbol)`" | GAI | Accurate observation (the update only affects the popped copy headed to `closed_trades`, not live `open_trades`, since the symbol's already removed) but not a bug — cosmetic/clarity only, explicitly acknowledged as such by GAI itself ("not strictly a bug") | **ACCURATE BUT NOT A FINDING** — no action needed |

**Pattern emerging across 3 blocks now:** Gro tends toward generic "add more validation here" suggestions without demonstrating a concrete trigger, and both reviewers occasionally assert something is missing or broken when it's actually already guarded a few lines away — this audit's per-claim verification step continues to be load-bearing, not ceremonial.

### `execution/portfolio_tracker.py` — Block 5 (lines 1879-2002, FINAL BLOCK): stats/reporting

| Lines | Block | Classification | Notes |
|---|---|---|---|
| 1879-1889 | `get_trade()`, `opened_today()` | DYNAMIC, no findings. |
| 1893-1985 | `get_stats()` | DYNAMIC. All standard divisions (Sharpe, Sortino, win_rate, avg_win/loss, profit_factor, avg_r_multiple) are explicitly guarded against zero-denominator — verified clean. **See MAJOR FINDING #3 below — a different, non-division class of crash risk in this function.** |
| 1987-2000 | `attach_news_summary()`, `get_news_summary()`, `print_stats()` | DYNAMIC, trivial, no findings. |

### MAJOR FINDING #3 (MOST SEVERE OF THE AUDIT SO FAR) — `get_stats()` can crash on a trade with no `"pnl"` key, permanently breaking ALL persistence for the rest of the process

**Severity: CRITICAL. Confirmed by Gro and GAI independently; GAI explicitly ranks this above every other finding logged today, including the reentrancy gap and the cumulative P&L bug.**

**The chain, fully traced and verified at each link:**

1. In `write_eod_summary()`'s Phase 2a.5 reconciliation (already covered under Block 3's reentrancy investigation), two branches mark an externally-closed position `_fifo_reconciled_closed = True` directly on `self.open_trades[_sym_r]` and `continue` — **without** calling `self.record_exit()` and **without** ever setting a `"pnl"` field on that trade dict. The position stays in `open_trades`, just flagged.
2. On the bot's next restart, `_load_log()` reads the persisted state and explicitly routes any trade with `_fifo_reconciled_closed=True` straight into `self.closed_trades` — again **without** going through `record_exit()` (the only code path that ever sets `"pnl"`). The routing code's own log message admits it: *"P&L needs manual verification."* This trade also does **not** get `_fill_unverified=True` set anywhere in this path.
3. `get_stats()` — called from inside **every single `_save_log()` call** (`"stats": self.get_stats()` is part of the atomic-write payload) — filters `self.closed_trades` with `if not t.get("_fill_unverified")` (which the routed trade passes, since that flag was never set on it) and then indexes `t["pnl"]` directly, not `.get("pnl")`. **`KeyError` on the very next `get_stats()` call after such a trade exists.**

**Why this is the worst finding of the day — the cascade, not just the crash:**
- `record_exit()` appends to `self.closed_trades` (line ~1766) **before** calling `self._save_log()` (line ~1812) — so the in-memory mutation always completes even when the subsequent save fails.
- `main.py`'s main loop wraps each cycle in `try: ... except Exception: log + sleep(60)`, so the bot **process does not crash** — confirmed via direct read of `main.py`'s `while True:` loop (lines 931-1064).
- But `self.closed_trades` is append-only — nothing in the codebase ever removes a bad entry from it. Once the malformed trade is in the list, **every subsequent `_save_log()` call, from any state-mutating method, for the rest of that process's life, raises the same `KeyError` and silently fails to persist.** The bot keeps trading correctly in-memory; it simply stops writing `trade_log.json` at all from that point forward, with each individual failure quietly absorbed by main.py's outer handler (logged as "Loop error," nothing louder).
- On the next restart (this codebase restarts itself routinely — nightly heartbeat-triggered `os.execv`), the bot loads `trade_log.json` reflecting whatever was **last successfully saved before the corruption** — silently losing every entry/exit/partial-exit that happened during the broken window, and very plausibly producing exactly the kind of stale-tracker-vs-real-Alpaca-state divergence this project has already been bitten by once today (the QHM external-close gap fixed earlier this session is structurally the same failure shape).

**GAI's explicit re-ranking of today's findings by priority, given this:**
1. **This persistence-cascade bug** (new) — highest priority, guarantees silent data loss once triggered.
2. The `record_entry()` overwrite gap / reentrancy gap (Blocks 2 & 4) — high priority, but more localized (single trade or specific race window).
3. The cumulative P&L lookback bug (Block 3) — medium priority, a reporting/decision-input issue, not a persistence-destroying one.

**Proposed fix (Gro and GAI both converge on the same two-part shape):** (a) make `get_stats()` defensive — `t.get("pnl", 0)` instead of `t["pnl"]`, so a single malformed record can never break every future save; (b) close the root cause — when a trade is marked `_fifo_reconciled_closed` without a recoverable exit price, set `"pnl": 0.0` and `"_fill_unverified": True` on it at that exact point, consistent with how `record_exit()` already handles the entry-price-invalid case (BUG-5 pattern) — so the trade is correctly excluded from stats denominators instead of silently missing a required field.

**Decision: NOT FIXED THIS SESSION** — logged per Rafael's "audit first, consolidated fix once we have the full picture" instruction. Flagging explicitly that this one may warrant priority sequencing ahead of the other logged findings once fixes are scheduled, given GAI's severity ranking — noted for Rafael's decision, not unilaterally escalated out of the queue.

---

## `execution/portfolio_tracker.py` — FULL FILE AUDIT STATUS: **COMPLETE** (all 5 blocks, 2002 lines)

**Summary of confirmed findings, this file only:**
| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | `write_eod_summary()` reentrancy guard incomplete (pre-FIFO reload + final write exposed) | RESOLVED 2026-06-28 — premise was wrong (sys.exit(0) prevents the resumption scenario); both Gro/GAI reversed; comment fixed (`1cc7509`), guard kept as harmless defense-in-depth | **CLOSED** |
| 2 | Cumulative P&L lookback loop breaks on file-exists not successful-parse | CRITICAL (Gro) / HIGH (GAI) — consensus fix | **FIXED `a41e7ce`** |
| 3 | `record_entry()` no overwrite guard for existing open/pending trade | HIGH — consensus | **FIXED `a41e7ce`** |
| 4 | `update_trail_stop()` doesn't self-persist | MEDIUM — consensus, needs exit_logic.py cross-check | **FIXED `a41e7ce`** |
| 5 | `record_partial_exit()` missing entry-price guard `record_exit()` already has | MEDIUM — consensus | **FIXED `a41e7ce`** |
| 6 | `get_stats()` `KeyError` cascade silently breaks all future persistence | **CRITICAL — consensus, GAI's top priority of the day** | **FIXED `a41e7ce`** (root cause: `write_eod_summary()` now sets `_fill_unverified` on all 3 unreconciled-exit paths; `get_stats()` also got a defensive fallback) |
| 7 | Silent `trade_logger` import fallback (no warning) | LOW-MEDIUM — consensus | Logged, not fixed |
| 8 | `_atomic_write()` orphaned `.tmp` file on double failure | LOW | Logged, not fixed |
| 9 | `record_stop_breach_blocked()` possible `TypeError` if both stop fields are `None` | LOW | Logged, not fixed |
| 10 | PT/ET date-string ambiguity in `write_eod_summary`/`_fetch_alpaca_fills_for_date` | MEDIUM (downgraded from GAI's initial CRITICAL after verifying against actual AH cutoff hours) | Logged, not fixed |
| 11 | `_fetch_alpaca_fills_for_date()` pagination ignores `after_id` combined with date params | P2, latent (already logged earlier today during the data remediation work) | Logged, not fixed |
| 12 | FIFO buy/sell synthetic-lot asymmetry (S49 board decision, undocumented on sell side) | MEDIUM, architectural, needs board re-confirmation not necessarily a code fix | Logged, not fixed |

**9 AI-sourced claims refuted** during this file's audit (4 from Gro, 4 from GAI, 1 retracted-after-evidence) — logged with the specific contradicting evidence in each case, demonstrating the verification step is catching real hallucinations, not just adding ceremony.

**Next:** Phase 1 begins — the 10 files that import `portfolio_tracker.py` (`entry_logic.py`, `exit_logic.py`, `kelly.py`, `orphan_manager.py`, `trade_engine.py`, `main.py`, `run_movers.py`, `state/persistence.py`, `run_cycle.py`, `trade_logger.py` — ~9,200 lines). Several cross-file verification items are already queued from this file's findings (the `update_trail_stop()` persistence question needs checking in `exit_logic.py`; `main.py`'s exception-handling structure around the main loop was already partially traced during Finding #6's investigation).

---

# PHASE 1

## `execution/entry_logic.py` (1678 lines) — owns `execute_entries()` (RTH signal→order) and `_overnight_entry_check()` (AH swing scanner). Full read complete.

**Confirmed findings (Gro + GAI both reviewed, both confirmed all 3 of mine plus GAI added one elaboration):**

| # | Finding | Lines | Severity | Source |
|---|---|---|---|---|
| 1 | **Stale comments, 2 instances** — both describe a yfinance data source that was migrated to Alpaca T1/FMP T2, comment never updated: (a) earnings gate comment says "yfinance calendar check" but code uses `data.fmp_client.get_cached_earnings_dates` (FMP T2); (b) FVG multiplier comment says "fetches 1h separately via yfinance" but code uses `fetch_bars(symbol, config.TF_1H, ...)` (Alpaca T1). Same failure pattern as today's Gemini PDT hallucination — a stale comment priming a false data-tier-violation belief in a future reader (human or AI). | ~874, ~1166 | LOW (documentation hygiene), but the *pattern* itself (2 instances in 1 file) is worth a project-wide grep sweep eventually | Claude, confirmed by both Gro and GAI |
| 2 | **"PHANTOM ENTRY" exception handler scope is too broad.** The try block wrapping `tracker.record_entry()` also wraps the overnight-tagging logic, GTC stop submission, and `alert_entry()` — all the way to `entered.append(symbol)`. If `record_entry()` succeeds (trade genuinely tracked) but a LATER step raises (e.g. `submit_gtc_stop_order()` throws instead of returning `None`), the except block fires the full "PHANTOM ENTRY" response: CRITICAL log asserting "Position is LIVE and UNTRACKED" (false — it IS tracked), `alert_crash()`, and `sys.exit(1)` forcing a bot restart — for what may just be a failed Slack alert or a transient GTC submission error. | ~1318-1436 | GAI: HIGH / Gro: MEDIUM | Claude, confirmed by both — GAI additionally traced a concrete sub-scenario: if `submit_gtc_stop_order()` succeeds but `tracker.set_gtc_stop_order_id()`'s own persistence fails, the result is a real position with an orphaned/unrecorded GTC order ID, mischaracterized by the same broad handler as a full phantom position |
| 3 | **Dead/always-False conditional in `_overnight_entry_check()`.** `_use_extended = _mins < _main._OVERNIGHT_ENTRY_START`, comment: "True only if before 8 PM (edge case)". But Gate 1 at the top of the same function already requires `_mins >= _main._OVERNIGHT_ENTRY_START` to reach this line at all — the condition is structurally guaranteed False every time it's evaluated. `submit_limit_order(..., extended_hours=_use_extended, ...)` always receives `False`; the comment describes a scenario the function's own gating makes unreachable. | ~1665 | LOW-MEDIUM | Claude, confirmed by both Gro and GAI |
| 4 (new, GAI) | **Redundant `_save_log()` call.** `tracker._save_log()` is called explicitly at ~line 1349 right before `tracker.set_gtc_stop_order_id()` is called a few lines later — and `set_gtc_stop_order_id()` already calls `self._save_log()` internally (confirmed in Block 4 of the `portfolio_tracker.py` audit). The explicit call is redundant double-saving. | ~1349 | LOW — pure inefficiency, not a bug | GAI (new), verified accurate against the already-audited `portfolio_tracker.py` Block 4 findings |

**Cross-file question raised, queued for `main.py`'s own audit pass (not resolved here):** does `entry_logic.py`'s own control flow adequately protect against `portfolio_tracker.py`'s `record_entry()` overwrite gap (Block 4 Finding #3)? Verified: within a single `execute_entries()` call, `is_in_trade(symbol)` (line 661) gates entry — every path through that check leads to `continue` before `record_entry()` is ever reached for an already-tracked symbol, so normal iteration cannot trigger the overwrite. **Open question:** `write_eod_summary()`'s reentrancy gap (Block 2/3 finding) was exploitable specifically because `main.py`'s SIGTERM handler directly re-invokes that same function while the original call is blocked on network I/O. Does the SIGTERM handler (or any other forced-restart/reentrant path) ever re-invoke `execute_entries()` or `_overnight_entry_check()` while an original call is mid-flight on one of ITS OWN blocking calls (`fetch_bars`, the 3x 1-second fill-price polling sleeps, FMP earnings lookups)? If so, the same reentrancy class of bug could cause a double order submission and a `record_entry()` overwrite for the same symbol. **Not verified either way yet — requires reading `main.py`'s SIGTERM handler and the full calling chain into `execute_entries()`, which is on the Phase 1 list.**

**Status: `execution/entry_logic.py` — STATIC/DYNAMIC pass + Gro/GAI review COMPLETE.**

---

## Methodology note — targeted grep sweeps (Rafael question, 2026-06-27)

After finding the stale-yfinance-comment pattern twice in `entry_logic.py`, Rafael asked why this audit isn't already using grep sweeps to optimize the process. Clarifying the distinction from CLAUDE.md's No-Grep rule: that rule exists to stop grep from substituting for full-file reads when auditing a file's actual logic — the exact failure mode that caused this project's recurring bug cycles historically. It does NOT prohibit a narrowly-scoped pattern sweep used to surface *candidate locations* across files not yet in sequence, provided each candidate still gets fully read and verified when its file comes up for its real audit turn (consistent with the rule's own carve-out: grep is permitted "to verify a specific line number already identified," after a full read of the immediate context around that line).

**Sweep run:** `grep -n "yfinance"` across all remaining Phase 1 files. Results triaged by reading the actual surrounding context at each hit (not just trusting the grep match):

| File:Line | Verdict |
|---|---|
| `exit_logic.py:229` | Historical changelog comment ("DATA-2c: replaced yfinance..."), accurately describes a past migration — not misleading. No finding. |
| `main.py:24,77,243,363,473` | All either historical changelog comments or accurate documentation of the *approved* T4 yfinance fallback (VIX/JPY) — not misleading. No finding. |
| `strategy/run_cycle.py:532-535` | Verified via direct read: `yfinance.Ticker("^VIX")` — approved T4 use (VIX is explicitly whitelisted). Not a violation. |
| `strategy/run_cycle.py:851` | **THIRD instance of the stale-comment pattern.** Comment claims `mri.refresh()` "Fetches TLT, JPY=X, HYG/LQD, USO, GLD, EWJ/EWG... via yfinance" — but this session's own prior history (S67/S68 handoff notes, confirmed earlier today) shows `macro_risk_index.py` was already migrated to Alpaca T1 `fetch_bars()` for those ETFs in an earlier session, with yfinance now reserved only as a T4 fallback for VIX and JPY specifically. The comment in `run_cycle.py` describing `mri.refresh()`'s internals was never updated to match. **Flagged as a candidate finding for `run_cycle.py`'s own full audit turn** — not independently re-verified against `macro_risk_index.py`'s current literal source in this session (that file isn't in the Phase 0/1 dependency list; this is circumstantial-but-strong evidence from documented project history, to be confirmed when `run_cycle.py` is fully read). |
| `strategy/run_cycle.py:906,1056` | Historical changelog / outage-handling comments — not misleading. No finding. |
| `trade_logger.py:66` | Documents a legitimate field value (`"yfinance_fallback"` as a valid `data_source` tag) — accurate, not stale. No finding. |

**Net result of the sweep: 1 new candidate finding (run_cycle.py:851, queued for that file's full turn), 8 locations cleared as non-issues without needing a full audit pass.** This is the intended use of a sweep in this audit — triage, not verdict. Will repeat this kind of targeted sweep opportunistically when a clear pattern emerges (like today's recurring stale-comment issue), not as a blanket substitute for the sequential full-file methodology.

---

## `execution/exit_logic.py` (2182 lines, largest Phase 1 file) — IN PROGRESS

**Block 1: `check_partial_exits()` (lines 185-1054, ~870 lines) — full read complete.** Owns trail-stop ratcheting, trailing-stop-hit closure, and the 3-tranche scaled profit-taking logic.

### RESOLVED — cross-file question queued from `portfolio_tracker.py` Block 4: is `update_trail_stop()`'s lack of self-persistence actually exploitable?

**Answer: yes, precisely — narrower than originally feared, but real and (per GAI) routine rather than rare.**

Traced all 5 call sites of `tracker.update_trail_stop()` across the codebase (2 in `check_partial_exits()`, 1 in `_check_exits_extended_hours()` covered here; 2 more inside the tranche-execution flow):

| Site | Context | Persistence outcome |
|---|---|---|
| ~line 362 | `check_partial_exits()`'s "trail stop not hit — ratchet if price moved" branch | **GAP CONFIRMED.** Multiple early-exit paths (old-stop-cancel failure → immediate `continue`; held-for-orders poll never clearing; GTC/DAY resubmit failure) fall through to the next trade with zero save covering this trail_stop mutation. |
| ~line 648 | Inside the tranche-execution flow, "qty too small for a real partial close" branch | SAFE — an unconditional `tracker._save_log()` follows a few lines later (~line 658) before the loop continues. |
| ~line 742, ~757 | Inside the tranche-execution flow, after an actual partial close fires | SAFE — an unconditional `tracker._save_log()` (~line 941) executes after the stop-resubmission logic regardless of whether that resubmission succeeded or failed, covering both sites. |
| ~line 2125 | `_check_exits_extended_hours()`'s own separate "trail stop not hit — ratchet" branch | **GAP CONFIRMED.** Immediately followed by a bare `continue` — same failure pattern as line 362, in a different function. |

**Refined finding:** the persistence gap is real but confined specifically to the two "ratchet the trail stop without also triggering a tranche or exit this cycle" code paths (one in each of two functions) — NOT throughout every use of `update_trail_stop()` as originally broadly suspected from `portfolio_tracker.py` alone.

**Severity — escalated from the original MEDIUM:** Gro: significant ("any data loss in a trading system can have significant consequences... should be addressed"). **GAI: Moderate-High, and explicitly states this is NOT a narrow edge case — ratcheting happens routinely whenever a trending trade moves favorably without hitting the next tranche threshold, meaning intermediate ratchets are lost on every restart/crash that happens between ratchet events, reverting risk management to a stale, less-protective stop level until the next successful tranche or exit event.** Concrete consequence per GAI: on a restart, the bot resumes with an outdated, wider stop than the market conditions actually warranted, increasing realized loss if price reverses before the next ratchet/save cycle.

**Fix — Gro and GAI both converge, independently, on the same recommendation:** make `update_trail_stop()` self-persistent (call `self._save_log()` internally, matching every other state-mutating method in `PortfolioTracker`) rather than relying on callers to remember to save afterward — exactly the same "encapsulate persistence, don't trust the caller" principle already applied to every sibling method in that class. This also structurally prevents the same class of bug from recurring at any FUTURE call site of `update_trail_stop()`, not just the two found today.

**Decision: NOT FIXED THIS SESSION** — logged per the "audit first, consolidated fix later" instruction. This finding is now considered a candidate fix for the future `portfolio_tracker.py` patch batch (it's a one-line change to that file, not to `exit_logic.py`), alongside the Block 2/3/4/5 findings already logged there.

**Status: `check_partial_exits()` (Block 1 of `exit_logic.py`) — STATIC/DYNAMIC pass + Gro/GAI review on the trail-stop persistence question COMPLETE.**

### Block 2: `check_exits()` (lines 1055-1974, ~920 lines) — full read complete.

Owns: overnight breakeven exit, thesis-invalidation exit, breakeven-stop promotion, hard-stop enforcement (with 3-scan noise filter), target-hit exit, and the reversal-scan-counter exit path (score-drop gate, hard-out noise band, GTC-cancel-defer retries). This is one of the most heavily-iterated functions in the codebase — extensive RC-4 fill-verification fallbacks, external-close detection, and defer-counter retry logic already in place from prior sessions.

**Finding 1 — `_forced_close_pending` dead flag (trivial, not a bug):** Set `True` at the start of the overnight-breakeven forced-close attempt (~line 1237); explicitly cleared in 2 of 3 outcome branches (close succeeds; close fails but position confirmed already gone). In the 3rd branch (close fails AND position confirmed still exists, ~line 1378-1382), it is never cleared — left `True` indefinitely. **Verified via full-codebase grep that this flag is never read anywhere** — only ever written or popped. Gro and GAI both independently confirmed the same grep result. **Verdict: dead state tracking, zero functional impact. Logged for completeness, not actionable.**

**Finding 2 — asymmetric close-failure alerting (real, CONFIRMED by Gro + GAI):** When a HARD STOP close fails and the position is confirmed to still exist on Alpaca (~line 1536-1542), the code logs CRITICAL + calls `alert_stop_breach()`. When a SIGNAL-based exit's close fails under the identical condition (close fails, position confirmed still exists, ~line 1845-1932), there is no equivalent branch at all — the `if not success:` block only acts when the position is confirmed GONE; when it's confirmed to still exist, execution silently falls through to the next trade with zero logging, zero alerting, relying implicitly on the exit signal still being true next cycle.
- Gro: confirmed the asymmetry exists as described.
- GAI: confirmed the asymmetry, rated severity **Medium-High** — explicitly rejects "lower urgency = OK to be silent" reasoning. Cites: (a) opacity — ops has zero visibility into a stuck, intended-to-exit position; (b) accumulation risk — a persistently-failing close (stale data, API issue, broker problem) retries forever with no escalation path; (c) loss of control — failing to execute an exit decision is itself a risk event regardless of which gate triggered the exit.
- **Consensus fix:** add at minimum a `logger.warning()` for every occurrence of "close failed, position still exists" in the signal-exit path; add an escalating alert (mirroring `alert_stop_breach()`) after N consecutive cycles of the same failure, consistent with the defer-counter pattern already used elsewhere in this same function (e.g., GTC-cancel-defer escalates to CRITICAL after 2 or 6 cycles depending on exit type).
- **Decision: NOT FIXED THIS SESSION** — logged per "audit first, consolidated fix later."

**REFUTED — Gro hallucination:** Gro's 4th ("additional finding") claimed the GTC-cancel-defer counters (`_gtc_cancel_defer_count`, `_gtc_sig_defer_count`) are "not reset when the position is closed," risking incorrect deferral counts over time. **Directly contradicted by source**: `grep -n "_gtc_sig_defer_count\|_gtc_cancel_defer_count" execution/exit_logic.py` confirms both counters ARE explicitly popped on every successful-close path — `_gtc_cancel_defer_count` at line 1596 (target-exit success), `_gtc_sig_defer_count` at lines 1837, 1926, AND 1934 (three separate signal-exit success/external-close paths). Logged as a refuted claim, not a finding.

**REFUTED (not logged as confirmed) — GAI speculative "Finding 3":** GAI proposed a "zombie GTC-cancel-defer counter" scenario where a stale non-zero defer count could delay future valid exit attempts after a `close_position()` failure. On inspection this conflates two distinct mechanisms: the defer counters (`_gtc_cancel_defer_count`/`_gtc_sig_defer_count`) gate the **pre-close** "GTC order not yet confirmed cancelled" retry step — they are not read or incremented anywhere in the **post-close-failure** code paths GAI's scenario describes. Not pinned to a concrete line/call site. Not logged as a confirmed finding — flagged as unverified speculation, consistent with this audit's standing practice of not trusting AI claims without source verification.

**Status: `check_exits()` (Block 2) COMPLETE.**

### Block 3: `_check_exits_extended_hours()` (lines 1980-2182, ~200 lines) — full read complete.

24/7 exit monitoring during pre-market (4:00-9:30am ET) and after-hours (4:00-8:00pm ET). Handles: PM-exit order reconciliation, post-partial trail-stop/breakeven-stop hit detection + ratcheting (already covered resolving the cross-file persistence question), pre-partial hard-stop breach, and first-partial-exit-target detection — all via limit orders (`submit_limit_order`) rather than market closes, appropriate for thin EH liquidity.

**Finding — missing kill-switch P&L registration on EH partial-exit fills (real, CONFIRMED HIGH by Gro + GAI independently):** When a pending extended-hours partial-exit limit order reconciles as filled (~line 2045-2060), the code calls `tracker.record_partial_exit()` and `kelly.record_trade()` but never `risk.register_close(pnl or 0.0)`. Every other partial-exit path in this file (3 sites in `check_partial_exits()`, confirmed via grep) and the FULL-exit reconciliation branch immediately below this one in the same function (line 2064) all call `risk.register_close()`. Grep across the whole file confirms exactly 10 `register_close()` calls, none near line 2051 — this is a real, isolated omission, not a different-pattern false alarm.
- Gro: confirmed the gap; rated **High** — kill switch (7% daily loss, paper profile) could undercount a day's realized loss, permitting further entries that should have been blocked.
- GAI: confirmed independently, same **High** rating, same concrete scenario — an EH partial exit taken at a meaningful loss (e.g., an overnight gap-down) is invisible to the daily kill-switch total, so the bot could continue trading later that day believing it's under the 7% threshold when it is not.
- **Fix (both voices converge):** add `risk.register_close(pnl or 0.0)` immediately after the `pnl = tracker.record_partial_exit(...)` line, mirroring every sibling exit/partial-exit path in this file.
- **Decision: NOT FIXED THIS SESSION** — logged per "audit first, consolidated fix later." This is now the highest-severity unfixed finding from `exit_logic.py` (kill-switch integrity, not just a logging/persistence gap) and should be prioritized near the top of the eventual consolidated fix batch.

**`execution/exit_logic.py` (2182 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE.**

### Summary of `exit_logic.py` findings (for the eventual consolidated fix batch)

| # | Finding | Lines | Severity | Source |
|---|---|---|---|---|
| 1 | `update_trail_stop()` not self-persisting — real gap in 2 of 5 call sites (trail-ratchet-only branches in `check_partial_exits()` and `_check_exits_extended_hours()`) | ~362, ~2125 | Moderate-High (GAI escalated) | Gro+GAI consensus; fix targets `portfolio_tracker.py`, not this file |
| 2 | Signal-exit close failure (position confirmed still exists) has no log/alert, unlike hard-stop's CRITICAL+alert for the same condition | ~1845-1932 | Medium-High | Gro+GAI consensus |
| 3 | EH partial-exit fill reconciliation never calls `risk.register_close()` — kill switch can undercount realized loss | ~2051 | **High** | Gro+GAI consensus |
| — | `_forced_close_pending` dead flag, never read anywhere — zero functional impact | ~1237-1382 | None (cosmetic) | Confirmed, not actionable |

**Refuted this file:** 1 Gro hallucination (GTC-defer counters falsely claimed never reset — directly contradicted by 4 separate pop() call sites), 1 GAI speculative claim not pinned to a real code path (conflated pre-close defer counters with post-close-failure scenario).

---

## `execution/kelly.py` (450 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE

Kelly Criterion dynamic position sizing — per-signal-type win/loss R-multiple tracking, fractional Kelly with CV penalty + ATH-drawdown (A2) scaling + GEX edge multiplier, atomic stats persistence.

**Finding — `rebuild_from_trades()` has no guard against missing `exit_price`, creating a direct cascading consequence of the `portfolio_tracker.py` Block 5 finding (real, CONFIRMED HIGH by Gro + GAI independently):**

`rebuild_from_trades()` (lines 388-435) is called by `write_eod_summary()` specifically to rebuild Kelly's stats from `closed_trades` after FIFO reconciliation. It filters out `_fill_unverified` trades and validates `direction`/`entry`/`stop`/`qty`, but reads `exit_p = float(t.get("exit_price") or 0)` — silently defaulting to `0.0` if missing, rather than skipping.

**Cross-file trace confirms this is reachable, not theoretical:** verified directly in `portfolio_tracker.py`'s `write_eod_summary()` (Phase 2a.5, ~lines 1246-1260 and ~1324-1336) — both the "no valid FIFO exit prices" branch and the "no FIFO match" branch set `_fifo_reconciled_closed=True` directly on the trade dict **without ever calling `record_exit()`** (the only place `exit_price`/`pnl` get set) and **without setting `_fill_unverified=True`** either. On the next restart, `_load_log()` routes these trades into `closed_trades` exactly as the earlier portfolio_tracker.py Block 5 finding described — valid `entry_price`/`stop`/`qty`/`direction` (it was a real, legitimately-opened trade), but no `exit_price` and no `pnl` ever set.

**Concrete consequence:**
- LONG trade: `pnl_per_share = 0 - entry = -entry` → an extreme phantom **loss** R-multiple (e.g. -75R on a $150 stock with $2/share risk) injected into that signal type's `losses` list.
- SHORT trade: `pnl_per_share = entry - 0 = +entry` → an extreme phantom **win** R-multiple injected into `wins` — inflating win_rate and avg_win_r for that signal type.

**Both Gro and GAI independently identify the SHORT/phantom-win direction as the more dangerous one:** a fictitious extreme win inflates Kelly into *over-sizing* future real short positions based on statistics that never happened — direct capital risk. The phantom-loss (long) direction skews toward under-sizing/avoidance, which is the safer failure direction by comparison but still a data-integrity violation.

**Severity: High (Gro + GAI both, independently).** GAI: "directly corrupts the core statistical foundation used for position sizing... the silent nature of the data corruption... makes it difficult to detect without careful auditing."

**Fix — both voices converge on doing BOTH, not either/or:**
1. Defensive guard in `rebuild_from_trades()`: skip if `t.get("exit_price") or 0 <= 0` (mirroring the existing `_fill_unverified` skip) — immediate, low-risk patch.
2. Root-cause fix in `portfolio_tracker.py`'s `write_eod_summary()`: the two `_fifo_reconciled_closed=True`-without-`record_exit()` branches should not leave a closed-trade-shaped record with missing exit data — either call `record_exit()` with a defensible price (consistent with the `_fill_unverified` pattern already used elsewhere in that file for unknown-price exits) or explicitly tag these records so `rebuild_from_trades()` (and anything else reading `closed_trades`) can recognize and exclude them deterministically, not via an accidental `or 0` fallback.

**Decision: NOT FIXED THIS SESSION** — logged per "audit first, consolidated fix later." This finding is closely related to the existing portfolio_tracker.py Block 5 finding (`get_stats()` KeyError risk) — both stem from the same two root-cause code paths in `write_eod_summary()`, so the eventual fix batch should address both consequences with a single root-cause patch plus the two respective defensive guards.

**No other findings in `kelly.py`** — the rest of the file (Kelly formula, CV penalty, A2 drawdown multiplier, GEX edge multiplier, atomic persistence, TQI history) was read in full and is consistent, well-guarded (division-by-zero avoided via `avg_win_r`/`avg_loss_r` fallback to 0.001, `stdev()` requires len>=2 already satisfied by the len>=30 gate, `rebuild_from_trades()`'s other field validations are all `.get()`-based and correctly bounded).

**Next Phase-1 file:** `orphan_manager.py` (1442 lines) — proceed?

---

## `execution/orphan_manager.py` (1442 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE

Position reconciliation and GTC stop lifecycle (extracted from main.py Phase 2). Owns: TOD phase classification, pre-market GTC stop reconciliation (`cancel_and_reconcile_gtc_stops()`, ~600 lines — PENDING_CANCEL handling with escalating defer counters, idempotent GTC adoption on restart, Patches 1/2/3 for emergency stops / GTC partial-fill reconciliation / stale DAY-stop cleanup), and startup position reconciliation (`reconcile_positions()`, ~650 lines — orphan adoption, externally-closed detection, size-mismatch and direction-mismatch correction). This is one of the most heavily-iterated, well-hardened files in the codebase — extensive named historical fixes (BUG-1/2/3, OM-RACE-1, OM-BUG-1/2, Patch 1/2/3, P1-2 through P5-H3, SF-01/02/03), each with a detailed comment explaining the specific failure mode it prevents.

**Finding — stale PDT comments (5th instance of the pattern this audit, low severity):** Comments at ~lines 390, 533-539, 583, 742, 757, 841 reference "PDT" checks/guards (e.g. "If PDT=3/3, mark overnight=True", "OM-RACE-1 guard: placed AFTER PDT check intentionally") describing logic from before PDT enforcement was permanently abolished (S63 sweep, per CLAUDE.md). **Verified via grep that every PDT reference in this file is a comment or log-message string only — zero live conditional logic checks a PDT counter anywhere in the file.** Documentation debt only, zero functional impact. Logged for completeness alongside the other 4 instances of this same pattern found earlier in this audit (entry_logic.py ×2, run_cycle.py ×1 via the grep sweep, now this file).

**Investigated and CONFIRMED SAFE — line 1144's `fetch_actual_fill_price()` call in the externally-closed-position branch:** checked whether this could reproduce the `kelly.py` missing-exit-price corruption pattern found earlier in this audit. Verified directly in `execution/fill_helpers.py`: on failure, `fetch_actual_fill_price()` falls back to `entry_price` (never 0.0) and explicitly sets `trade["_fill_unverified"] = True` + CRITICAL log + Slack alert — the standard RC-4 pattern already used everywhere else in the codebase. This IS checked by `kelly.py`'s `rebuild_from_trades()` filter. Not a repeat of the earlier bug — confirmed clean.

**Investigated and REFUTED — GAI's speculative "double-counting P&L on restart" concern (qty-mismatch banking block, ~lines 1356-1419):** GAI raised (explicitly caveated as speculative, "without seeing the actual code") a scenario where a crash between P&L-banking and persistence could cause the same external-close to be banked twice on restart. Checked against the literal code: all banking mutations (`partial_pnl`, `profit_tranche_level`, `qty_remaining`) happen in memory, with a single atomic `tracker._save_log()` call at the very end (line 1419) — critically, `qty_remaining` is set to `alpaca_qty` in that SAME save (line 1418). This makes the operation self-correcting: crash before the save → nothing persists → the same mismatch re-triggers the same (correct, not duplicated) calculation on restart. Save succeeds → `qty_remaining` now equals `alpaca_qty` → the mismatch check (line 1356) is false on every subsequent pass → no re-trigger. **Does not hold up against the literal code — refuted, not logged as a finding.**

**Gro's review** (given a condensed prompt without the full 1442-line source, consistent with this audit's rate-limit-driven approach for Gro): explicitly declined to invent findings without the literal code, stating plainly it had no further findings beyond what was already identified — an honest abstention rather than fabricated confidence, consistent with this audit's standing practice of treating "I don't know" as an acceptable and preferable answer to a guess.

**No other findings.** This is the cleanest Phase-1 file audited so far — the extensive historical hardening appears to have genuinely closed the gaps that earlier sessions found, with no new logic-level bug surfaced by either AI voice or by direct reading.

**Next Phase-1 file:** `trade_engine.py` (286 lines) — proceed?

---

## `execution/trade_engine.py` (286 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE

Phase 2 decomposition shim file — most original logic (`check_partial_exits`, `check_exits`, `execute_entries`, etc.) has been extracted to `exit_logic.py`/`entry_logic.py` and is only re-exported here for caller compatibility. Live logic remaining: hybrid-engine-state persistence (`_save_hybrid_state()`/`_load_hybrid_state()`), a thin `_submit_rth_day_stops()` shim delegating to `gtc_manager`, and `_reconcile_pending_overnight_orders()` (polls Alpaca for pending overnight limit-order status, promotes/cancels accordingly, gates `risk.register_open()` to fire exactly once per genuine transition).

**Investigated and CONFIRMED SAFE — `trade["limit_price"]` direct-dict-indexing (~line 241):** both Gro and GAI flagged this as a stylistic KeyError risk (direct indexing vs `.get()`). Verified against the actual construction site in `portfolio_tracker.py` (~lines 1459, 1476): `limit_price` is a required, non-optional constructor parameter for every `pending_overnight` trade record, set unconditionally at creation. No code path creates a `pending_overnight` trade without it — the risk is theoretical, not reachable. Not logged as a finding.

**No other findings.** GAI raised several minor style/verbosity opinions (debug- vs warning-level logging for hybrid-state save failures; a slightly redundant `float()`-then-`int()` conversion on `filled_qty`) — these are preferences, not bugs, and don't meet this audit's bar for a logged finding. GAI's note on `_main._hybrid_state_loaded = True` being set before the restore guards run is, per the function's own comment ("set regardless — only attempt once"), confirmed intentional design, not a bug.

This is the smallest and cleanest Phase-1 file audited so far — almost entirely re-exports, with the small amount of live logic well-guarded (atomic hybrid-state persistence, idempotent register_open() gating with an explicit undercount warning on unexpected transitions).

**Next Phase-1 file:** `run_movers.py` (242 lines) — proceed?

---

## `run_movers.py` (242 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE

Standalone "Movers Bot" script — a separate momentum strategy distinct from the main MTF confluence bot, with its own scan-evaluate-exit loop, designed to terminate at 9:28 AM ET and hand off to `main.py` for RTH.

**Finding — potential dual-process race on shared `trade_log.json` (real risk pattern, severity disputed pending infra verification, Gro: High / GAI: Critical):** `run_movers.py` instantiates its own independent `PortfolioTracker()` (line 174), and `PortfolioTracker`'s state file is a fixed, module-level absolute path (`TRADE_LOG_FILE = _ROOT / "trade_log.json"`) — identical regardless of which OS process instantiates it. Separately confirmed via `orphan_manager.py`'s own docstring (read earlier in this audit): `main.py`'s `cancel_and_reconcile_gtc_stops()` runs "at startup AND at the start of every premarket cycle" — meaning `main.py` is itself active and writing to the same tracker file during the same premarket window `run_movers.py` operates in, right up until its 9:28 AM ET self-termination check.

**Both Gro and GAI confirm the underlying mechanism is a genuine "last writer wins" race** if the two processes are ever concurrently alive: each holds an independent in-memory snapshot loaded at its own startup, and neither coordinates `_save_log()` calls — a later atomic write from one process would silently overwrite an update the other process made in the interim (lost position records, vanished GTC stop IDs, phantom duplicate entries, or stale P&L). GAI rates this **Critical** (trade_log.json is the single source of truth for position/order state — corruption here cascades everywhere); Gro rates **High**.

**Investigated the deployment-level mitigation:** `launch_bots.sh` (the production launcher) explicitly kills any running `run_movers.py` process (line 46, grouped with other "stale companion processes") every time the launcher itself starts — but this is a **one-time pre-flight cleanup at launch time only**, not an ongoing runtime guard. `main.py` runs as an always-on restart-looped service once launched. If `run_movers.py` is independently triggered later (e.g., via a separate cron entry — `run_ftd.py`'s own docstring references "before run_movers.py at 6:30" AM PT, implying such a schedule exists or once existed), nothing in the codebase itself would prevent it from running concurrently with the already-active `main.py` for the remainder of premarket.

**Asked Rafael directly whether `run_movers.py` is still actively cron-scheduled on the OCI deployment — answer: not sure, needs to be checked directly on the OCI host.** This finding cannot be fully closed from the codebase alone — it depends on infrastructure state outside this repo.

**Decision: OPEN ITEM, not a code fix — needs an OCI crontab check before severity can be finalized.** If `run_movers.py` is confirmed dead/unscheduled in production, this finding downgrades to "legacy code, no live risk, candidate for deletion or explicit deprecation marking." If it's confirmed still scheduled, this becomes one of the higher-priority items in the eventual consolidated fix batch — Gro/GAI's suggested fixes (single shared tracker service via IPC, or strict OS-level mutual exclusion) are both more involved than a simple patch and would need their own design discussion.

**Update from `main.py`'s audit (confirms the gap is real at the code level, doesn't resolve it):** `main.py` has its own process-singleton lockfile (`fcntl.flock(LOCK_EX|LOCK_NB)` on `logs/alpaca_mtf_bot.lock`) — but this only prevents two `main.py` instances from running simultaneously. Verified via grep that `run_movers.py` contains zero references to `flock`/`fcntl`/this lockfile path — it does not participate in main.py's mutual-exclusion mechanism at all. This rules out the possibility that code-level locking already silently protects against the race; if `run_movers.py` is still cron-scheduled, nothing in the codebase would stop it from running concurrently with `main.py`.

**No other findings in `run_movers.py`** — the rest of the file (CLI argument parsing, scan-cycle merge/dedup logic, market-hours/window guards, RiskManager initialization) was read in full and is straightforward, consistent with its role as a relatively simple standalone script.

**Next Phase-1 file:** `state/persistence.py` (132 lines) — proceed?

---

## `state/persistence.py` (132 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE

Atomic JSON write helpers extracted from `main.py` — owns `confirm_gate.json` and `bot_status.json` only (trade_log.json, tqi_history.json, hybrid_state.json, kill_switch_state.json each remain owned by their respective domain classes per this file's own docstring). Standard mkstemp + fsync + os.replace atomic-write pattern.

**Gro: no genuine bugs found** after full-source review — confirmed clean.

**REFUTED — GAI's claimed double-close bug in `_atomic_write()`:** GAI claimed the explicit `os.close(fd)` inside the inner `except` block conflicts with `with os.fdopen(fd, "w") as f:` already owning `fd`'s closure, predicting a `ValueError: I/O operation on closed file`. **Does not hold up against Python's actual context-manager semantics:** when an exception occurs inside the `with` block, `f.__exit__()` (calling `f.close()`, which closes `fd`) runs BEFORE the exception reaches the outer `except` clause — by the time `os.close(fd)` executes, `fd` is already closed, so the call would raise `OSError: Bad file descriptor`, not `ValueError`. **This is exactly the case the code already anticipates and handles**: `except OSError as _fd_e: logger.debug(...)` immediately wraps the `os.close(fd)` call. This is intentional defensive double-close handling, not a bug. Refuted.

**No other findings.** This is the cleanest file audited in this entire initiative so far — a small, focused utility module with no logic beyond atomic file I/O, and both AI reviewers either found nothing or were refuted on inspection.

**Next Phase-1 file:** `trade_logger.py` (88 lines, smallest remaining file) — proceed?

---

## `trade_logger.py` (88 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE

Structured trade-event logging to `logs/trade_events.jsonl` (Guardrail 7). Simple append-only writer, `_STOP_REASONS` frozenset for substring classification.

**Investigated and CONFIRMED SAFE — `_STOP_REASONS` cross-file consistency:** verified `portfolio_tracker.py:68` imports this exact frozenset (aliased `_LOG_STOP_REASONS`) rather than maintaining a separate hardcoded list, with a graceful empty-frozenset fallback on import failure. Single source of truth, no drift risk.

**Confirmed acceptable — non-atomic `open(path, "a")` append write:** intentional per this project's RC-5 carve-out (atomic tmp+replace required for critical state files; non-critical/append-only logs are explicitly exempt). Standard, correct pattern for a JSONL event log.

**No real findings.** Both Gro and GAI suggested adding input/event-type validation to `log_event()` (e.g., enforcing `stop_reason` presence for `stop_hit` events, type-checking parameters) — these are design/robustness preferences, not bugs: the function works correctly as designed for its actual purpose (writing through whatever the caller passes). Doesn't meet this audit's bar for a logged finding, consistent with the standing distinction between real defects and style suggestions.

**`trade_logger.py` — clean, no action needed.**

---

## PHASE 1 STATUS: 8 of 10 files complete

| File | Lines | Status | Key findings |
|---|---|---|---|
| `entry_logic.py` | 1678 | ✅ DONE | Stale yfinance comments ×2, PHANTOM ENTRY exception scope too broad (Gro MEDIUM/GAI HIGH), dead conditional |
| `exit_logic.py` | 2182 | ✅ DONE | `update_trail_stop()` persistence gap (Moderate-High), signal-exit silent-retry asymmetry (Medium-High), **EH partial-exit kill-switch P&L gap (HIGH)** |
| `kelly.py` | 450 | ✅ DONE | **Cascading Kelly-stats corruption from missing exit_price (HIGH)** — traces to portfolio_tracker.py root cause |
| `orphan_manager.py` | 1442 | ✅ DONE | Cleanest file — only a 5th stale-PDT-comment instance |
| `trade_engine.py` | 286 | ✅ DONE | Clean — 1 theoretical (non-reachable) concern investigated and cleared |
| `run_movers.py` | 242 | ✅ DONE | **OPEN ITEM: potential dual-process race on trade_log.json (Gro HIGH/GAI CRITICAL) — pending OCI crontab verification** |
| `state/persistence.py` | 132 | ✅ DONE | Cleanest file in the whole audit — no findings |
| `trade_logger.py` | 88 | ✅ DONE | Clean — no findings |
| `strategy/run_cycle.py` | 1669 | ⏳ QUEUED | (1 candidate flagged from earlier grep sweep — stale MRI yfinance comment at L851) |
| `main.py` | 1068 | ⏳ QUEUED | Also resolves the open SIGTERM-reentrancy cross-file question from `entry_logic.py` |

**Next Phase-1 file:** `strategy/run_cycle.py` (1669 lines) — proceed?

---

## `strategy/run_cycle.py` (1669 lines) — FULL READ AND GRO/GAI REVIEW COMPLETE

The main scan-cycle orchestrator: kill-switch check, premarket phase, AH/overnight phase (GTC stop submission + VIX widening, 24/7 exit monitoring), RTH phase (ORB gate, the 9-layer dynamic MIN_SCORE system, the hybrid SPY/QQQ market-reaction engine), signal scanning/filtering, entry execution, dashboard/HTML writes, periodic EOD flush.

**Stale comments — 3 more instances of the recurring pattern (8th-10th confirmed this audit), all low severity, documentation debt only:**
1. ~Line 851: "Fetches TLT, JPY=X, HYG/LQD, USO, GLD, EWJ/EWG... via yfinance" describing `mri.refresh()`'s internals — already flagged via the earlier grep sweep; confirmed in-context here.
2. ~Lines 1393-1395: comment says "MRI=STRESSED, HIGH, or CRITICAL" blocks entries, but the actual conditional (line 1397) only checks `("HIGH", "CRITICAL")` — confirmed this matches a deliberate, board-approved fix (commit `d81e060`, per S66 handoff: "remove STRESSED from hard-block — restores 2026-06-13 board decision"). Code is correct; comment was never updated after the fix — notable because this one describes risk-gating logic, where a future reader trusting the comment could misjudge the bot's actual behavior.
3. ~Line 1628: header comment says "once after 1:05 PM PST" but the actual gate (line 1638-1641) checks 1:15 PM PST, with its own separate inline comment correctly explaining the 1:15 timing rationale ("after EOD writes"). Minor drift between header and implementation.

**Two candidate findings flagged for board review (design forks, not unilateral bugs — per CLAUDE.md's Open Question Protocol):**

### RESOLVED (scoped as a roadmap item, not a quick patch) — 2026-06-28

Rafael confirmed a floor/redesign is needed, but the right shape is bigger than "add a floor": dials should require **sustained** signals across multiple scans before compounding (not a single momentary snapshot), and sizing logic should split into 3 buckets — A) intraday, B) 1-2 week swing (needs an entirely separate framework, not a filtered version of the intraday dials), C) quarterly QHM holds (already confirmed structurally separate — `qhm.maybe_enter_positions()` never receives `size_multiplier`). Logged as a Future Roadmap item in `CLAUDE.md` rather than rushed — needs its own Feature Design Protocol session, board + Gro/GAI input on the sustain-window threshold, and Bucket B's framework designed from scratch.

**Candidate 1 — multiplicative size-multiplier compounding (Gro + GAI both confirm the mechanism, both stop short of calling it a definitive bug):** `size_multiplier = event_size_mult × regime_size_mult × tod_size_mult × _spy_risk_mult × _pnl_size_mult × _overnight_size_mult` (line 1286) directly multiplies 6 independent stress factors. GAI ran a concrete example: 6 individually-reasonable factors (0.8, 0.75, 0.85, 0.6, 0.7, 0.9) compound to **0.193x** — under 20% of intended size — even though no single factor implied anything close to that severe a reduction. GAI's framing: "the system effectively de-risks more aggressively than any single factor implies... not a bug, but a significant risk of unintended behavior." Candidate fixes proposed (not applied): a floor on the final compounded multiplier, or replacing some subset of independent multiplication with a different combination strategy (e.g., min() of the worst few rather than the product of all). **Needs board input — is this the intended "extra caution when many signals align" behavior, or an unintended interaction nobody designed for?**

**Candidate 2 — hybrid market-reaction engine: a lower-severity event can prematurely override a higher-severity event's persistence countdown (verified directly against the literal code, lines 1010-1054):** the event-detection block is unconditional — any cycle where `_new_event` is truthy (ANY new trigger, regardless of severity vs. the currently active one) overwrites `_main._spy_event_type`, resets `_main._spy_risk_scans_left` to the NEW event type's persistence value, and updates direction/magnitude (lines 1045-1050) — with zero comparison against the currently active event's severity or remaining countdown. Concrete scenario: an EXTREME event is active with 2 scans left (of an original persistence of 3); the very next cycle's SPY/QQQ bar only triggers a SECTOR-level signal (not another EXTREME) — the code unconditionally downgrades the state to SECTOR with SECTOR's own (shorter) persistence count, discarding EXTREME's remaining 2 scans and its associated +3 MIN_SCORE / 0.50x size protections early. **This defeats the apparent design intent of the persistence mechanism** (sticky protection that should outlast a single quiet bar) for the specific case where a NEW, milder trigger fires before the prior, more severe one's countdown naturally expires. **Needs board input — should a new lower-severity event detection be prevented from downgrading an active higher-severity event's state until that event's own countdown reaches zero?** This is the kind of decision-fork CLAUDE.md's Open Question Protocol exists for — not resolved unilaterally here.

### RESOLVED — 2026-06-28, Rafael approved the fix, FIXED in commit `f83152f`

Rafael's decision: yes, fix it — a milder reading should not be allowed to cut short a more severe alert's cooldown early. Added an `_EVENT_SEVERITY_RANK` dict (mirroring the existing MIN_SCORE-bump tiering exactly: EXTREME=3, the 3 hardest macro BROAD types=2, everything else=1). A downgrade attempt (`_new_rank < _active_rank`, different event type, risk currently active) now gets blocked and falls through to the same countdown-decrement path used for "no new event this cycle" — the active event ticks down on its own schedule instead of being reset. A more-severe new event still correctly overrides an active milder one (no behavior change there).

**Verification chain:** cold second-agent PASS (verified the rank dict matches the MIN_SCORE tiering exactly, checked same-event-refresh isn't blocked, checked inactive-state fresh-events aren't blocked, checked no double/skipped decrement) → Gro APPROVE → GAI APPROVE (independently confirmed the more-severe-upgrades-correctly case, flagged a future-roadmap idea — a multi-event "stack" model — as a non-blocking observation) → static analysis clean → applied.

**Note for the record:** an earlier draft of this fix accidentally added a bare `return` inside the new downgrade-check block, which would have caused a blocked downgrade to skip the ENTIRE rest of `run_cycle()` for that scan (all exit checks, signal scanning, dashboard writes) — caught and corrected before any review/commit, by re-reading the code immediately after writing it. Logged here as a reminder that "patch then verify" beats "patch and assume," even mid-session.

**Decision: both candidates logged as OPEN ITEMS requiring board/Gro/GAI alignment before any patch — NOT applied this session,** consistent with "audit first, consolidated fix later."

**Next Phase-1 file:** `main.py` (1068 lines) — the final Phase 1 file, and where the open SIGTERM-reentrancy cross-file question from `entry_logic.py`'s audit gets resolved — proceed?

---

## `main.py` (1068 lines) — FULL READ AND GAI REVIEW COMPLETE (Gro hit its daily 100K TPD limit this file — noted, not blocking)

Final Phase 1 file. Covers: process-singleton lockfile (`fcntl.flock`), profile overrides, startup validation/reconciliation sequence (GTC reconciliation, position reconciliation, pending-order reconciliation, Alpaca-authoritative position-count override), the SIGTERM handler, the main `while True` loop (daily reset, connection heartbeat with `os.execv()` restart on 3 consecutive failures, `run_cycle()` invocation, sleep-interval-by-phase).

### RESOLVED — the open SIGTERM-reentrancy cross-file question from `entry_logic.py`'s audit

**Question:** could `main.py`'s SIGTERM handler re-invoke `execute_entries()` while an original call is blocked mid-execution, the same mechanism that caused the earlier-fixed P0 bug in `write_eod_summary()`?

**Answer: No — confirmed via direct read of `_handle_sigterm()` (lines 602-651).** The handler calls `tracker._save_log()`, `tracker.write_eod_summary()`, `qhm.safe_stop()`, `alert_crash()`, closes the lockfile, then `sys.exit(0)`. It contains zero calls to `execute_entries()`, `run_cycle()`, or anything that transitively reaches entry logic. **Two independent reasons this is safe:** (1) no second call site exists for `execute_entries()` to race against; (2) `sys.exit(0)` raises `SystemExit`, terminating the process rather than returning control to whatever was interrupted — even if SIGTERM fires mid-`execute_entries()`, that call is never "resumed," it simply ceases to exist along with the process. GAI confirmed this reasoning independently.

**Follow-up question GAI raised, also resolved:** does `write_eod_summary()`'s own reentrancy guard (the same-day P0 fix, commit `d74726d`) actually protect the SIGTERM call site at line 626, or could the guard itself deadlock if implemented as a blocking lock? **Verified directly against `portfolio_tracker.py:863-882`: the guard is a plain boolean (`_eod_fifo_in_progress`), explicitly NOT a `threading.Lock`** — its own comment states the reasoning: "Plain bool, not threading.Lock — this is same-thread reentrancy, not cross-thread." Since the SIGTERM handler runs in the *same thread* (Python delivers signals by interrupting the blocking syscall and running the handler in-thread before the original call resumes), by the time the reentrant call reaches the guard check, the flag is already `True` (set before the blocking network call that got interrupted) — so the reentrant call takes the skip-and-fallback branch immediately. No blocking, no possibility of a deadlock — GAI's hypothesized failure mode requires a lock that doesn't exist in this implementation.

**This closes the SIGTERM-reentrancy question definitively, for both the originally-flagged risk (execute_entries) and the deeper follow-up (write_eod_summary's guard correctness at this specific call site).**

**No other new findings in `main.py`.** The file is dense but consistently defensive — every reconciliation step wrapped independently so one failure doesn't block startup, the Alpaca-authoritative position-count override at startup, the heartbeat-triggered full process restart via `os.execv()`, and the deliberate behavioral distinction between `KeyboardInterrupt` (closes all positions — user-intended stop) and `SIGTERM` (preserves positions — possible restart, not a stop) are all consistent, well-reasoned, and matched the actual code on inspection.

---

# PHASE 1 COMPLETE — ALL 10 FILES AUDITED

| File | Lines | Highest severity finding |
|---|---|---|
| `entry_logic.py` | 1678 | PHANTOM ENTRY exception scope too broad (Gro MEDIUM/GAI HIGH) |
| `exit_logic.py` | 2182 | **EH partial-exit kill-switch P&L gap — HIGH** |
| `kelly.py` | 450 | **Cascading Kelly-stats corruption from missing exit_price — HIGH** |
| `orphan_manager.py` | 1442 | None — cleanest hotspot-adjacent file |
| `trade_engine.py` | 286 | None |
| `run_movers.py` | 242 | **Dual-process race on trade_log.json — OPEN, pending OCI verification** |
| `state/persistence.py` | 132 | None — cleanest file in the audit |
| `trade_logger.py` | 88 | None |
| `run_cycle.py` | 1669 | 2 design-fork candidates flagged for board review |
| `main.py` | 1068 | None new — resolved the SIGTERM-reentrancy question definitively |

**Total Phase 0 + Phase 1 lines audited: 2002 (portfolio_tracker.py) + 11,237 (the 10 files above) = 13,239 lines, full-read, Gro+GAI cross-examined, with every AI claim independently verified against literal source before being logged.**

**Phase 2 (the rest of the bot, beyond portfolio_tracker.py's direct dependency graph) is not yet scoped.** This is the natural checkpoint to consolidate findings into a prioritized fix batch before deciding whether to continue mapping the remainder of the codebase or pivot to applying fixes.

---

## CONSOLIDATED FIX BATCH — APPLIED (commit `a41e7ce`, 2026-06-28)

Per Rafael's instruction to pause Phase 2 expansion and address the outstanding ready-to-fix findings, 10 fixes were applied across `portfolio_tracker.py`, `kelly.py`, and `exit_logic.py`. Full sequence run: full read (already complete via this audit) → 10-point audit (already complete) → board vote (4/4 domains APPROVE: Peterffy/Katsuyama reliability, Harris/Brandt execution risk, McKinney data integrity, Thorp quant logic) → Gro+GAI external audit → static analysis (py_compile/mypy/ruff all clean) → cold second-agent (PASS) → applied.

**Fixes closed:**
1. `portfolio_tracker.py` cumulative P&L lookback loop — `break` moved inside `try`, no longer gives up on the first unreadable prior-day file.
2. `portfolio_tracker.py` `record_entry()` — duplicate-open-position guard added (mirrors `promote_pending_to_active()`).
3. `portfolio_tracker.py` `record_partial_exit()` — entry_price validation added (BUG-5 mirror): forces $0.00 + CRITICAL + Slack + `_fill_unverified=True` on invalid entry, instead of phantom P&L.
4. `portfolio_tracker.py` `update_trail_stop()` — now self-persists (`self._save_log()`) in both ratchet branches.
5. `portfolio_tracker.py` `write_eod_summary()` — all 3 `_fifo_reconciled_closed`-without-`record_exit()` paths now also set `_fill_unverified=True` (root-cause fix for the most severe Block 5 finding AND the kelly.py cascading-corruption finding).
6. `portfolio_tracker.py` `get_stats()` — `t["pnl"]` → `t.get("pnl", 0.0)` defensive fallback (belt-and-suspenders alongside fix #5).
7. `kelly.py` `rebuild_from_trades()` — skips trades with missing/zero `exit_price` instead of treating as `0.0` (closes the HIGH-severity phantom-loss/phantom-win Kelly corruption finding).
8. `exit_logic.py` EH partial-exit reconciliation — added `risk.register_close(pnl or 0.0)` (closes the HIGH-severity kill-switch P&L gap finding).
9. `exit_logic.py` signal-exit close-failure path — added the missing `else:` branch with escalating log/alert (closes the Medium-High asymmetric-alerting finding).
10. **Board-discovered during review (not in the original audit findings list):** `record_partial_exit()`'s new entry_price guard (fix #3) was forcing `pnl=0.0` but not setting `_fill_unverified=True`, unlike fix #5's pattern — Thorp/quant-logic domain caught this gap during the board vote itself. Fixed before commit by adding `trade["_fill_unverified"] = True` inside that branch.

**Gro availability note:** Gro (Groq) hit its daily 100K-token-per-day limit partway through this review — it APPROVED the original 9-fix diff, but was rate-limited (full exhaustion, ~70min cooldown) before it could independently re-review fix #10. Per the Authority Rule (Gro/GAI are audit voices, zero blocking authority) and given GAI + all 4 board domains independently confirmed fix #10 correct, the fix was applied without re-querying Gro. Not a Gro/GAI disagreement requiring the tie-breaker protocol — purely a rate-limit availability gap, logged for transparency.

**Remaining open items NOT addressed in this batch (require further discussion, not unilateral fixes):**
- The disputed `write_eod_summary()` reentrancy-guard scope finding from Block 2/3 (Gro HIGH vs GAI MEDIUM disagreement on severity AND fix approach) — still needs a board tie-breaker session.
- The 2 design-fork candidates from `run_cycle.py` (multiplicative size-multiplier compounding; event-severity-downgrade gap in the hybrid market-reaction engine) — both explicitly flagged for board review, not resolved.
- `run_movers.py`'s potential dual-process race on `trade_log.json` — still pending an OCI crontab check to determine if it's even live in production.
- 10 instances of the stale-comment pattern found across files — cosmetic, not yet cleaned up.
- Dead PDT-era code discovered incidentally during impact analysis (`get_rolling_day_trade_count`, `compute_pdt_for_date` in `portfolio_tracker.py` — zero callers anywhere) — not part of this batch's scope, logged as a candidate for a future dead-code sweep.

**Phase 2 (mapping the rest of the bot beyond `portfolio_tracker.py`'s direct dependency graph) remains paused, by Rafael's instruction, in favor of this consolidation pass.**

---

## Session Log

**2026-06-27 (S68 continuation):** Initiative created. Scope confirmed with Rafael, board,
Gro, and GAI — all four voices explicitly confirmed understanding with zero ambiguity
flagged. Dependency graph mapped for Phase 0/1 (11 files, 11,239 lines). Block 1 of
`portfolio_tracker.py` (lines 1-439) completed this session — see Findings Log above.
Blocks 2-5 of this same file, plus all 10 Phase-1 files, remain. This is a multi-session
effort by design — flagging explicitly rather than implying false completeness.

**2026-06-28:** Phase 1 completed (all 10 files). Per Rafael's instruction, paused before
Phase 2 to consolidate and apply the ready-to-fix findings — see "CONSOLIDATED FIX BATCH"
above. 10 fixes applied across 3 files, full sequence run, committed `a41e7ce`.
