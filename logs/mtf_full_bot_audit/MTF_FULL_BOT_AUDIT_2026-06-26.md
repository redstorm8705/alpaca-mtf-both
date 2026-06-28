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
| 1 | `write_eod_summary()` reentrancy guard incomplete (pre-FIFO reload + final write exposed) | DISPUTED (Gro: HIGH / GAI: MEDIUM) — fix approach also disputed | Logged, not fixed |
| 2 | Cumulative P&L lookback loop breaks on file-exists not successful-parse | CRITICAL (Gro) / HIGH (GAI) — consensus fix | Logged, not fixed |
| 3 | `record_entry()` no overwrite guard for existing open/pending trade | HIGH — consensus | Logged, not fixed |
| 4 | `update_trail_stop()` doesn't self-persist | MEDIUM — consensus, needs exit_logic.py cross-check | Logged, not fixed |
| 5 | `record_partial_exit()` missing entry-price guard `record_exit()` already has | MEDIUM — consensus | Logged, not fixed |
| 6 | `get_stats()` `KeyError` cascade silently breaks all future persistence | **CRITICAL — consensus, GAI's top priority of the day** | Logged, not fixed |
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

## Session Log

**2026-06-27 (S68 continuation):** Initiative created. Scope confirmed with Rafael, board,
Gro, and GAI — all four voices explicitly confirmed understanding with zero ambiguity
flagged. Dependency graph mapped for Phase 0/1 (11 files, 11,239 lines). Block 1 of
`portfolio_tracker.py` (lines 1-439) completed this session — see Findings Log above.
Blocks 2-5 of this same file, plus all 10 Phase-1 files, remain. This is a multi-session
effort by design — flagging explicitly rather than implying false completeness.
