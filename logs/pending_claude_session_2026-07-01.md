# Pending Claude Session — Approval Queue (2026-07-01, AWP autonomous)

**Context:** Rafael away; autonomous AWP run. Per mandate, NOTHING here is applied to code —
each item is diagnosed + board + Gro/GAI-prepped and waits for Rafael's one-word approval.
Read-only analysis (P0 status checks, directive re-triage, cross-strategy audit) is complete.

Already SHIPPED earlier this session (with Rafael present, full sequence + live-verified):
- `0ab73d7` gc.freeze() — killed the 3–12.8s/cycle gc degradation (101,299 objects frozen).
- `701eb47` P0-STARTUP symmetric QHM exclusion — killed the false "externally closed" CRITICAL.
- `9d0942a` Package 1 — EH QHM guard in `_check_exits_extended_hours()` (see below, DONE).

Packages 3 and 4 are fully gated (board + Gro + GAI + static + cold-agent, all APPROVE/PASS) and
**staged as uncommitted working-tree diffs** in `execution/orphan_manager.py` and
`strategy/run_cycle.py` — nothing committed/pushed/deployed. Awaiting "approve package 3" /
"approve package 4".

---

## STATUS OF STANDING P0s

### P0 #1 — Rebuild `open_lots_prior_day.json` → **CLOSED (no action needed)**
The **live OCI** file is healthy: `date=2026-07-01`, clean lots matching the 6 real intraday
positions (SNOW/RIVN/HOOD/TSLA/MARA/PANW), QHM symbols correctly absent (daily regen +
QHM-purge from `79b8f14` working). The corrupt/26-day-stale copy is only my **local dev copy**,
which the bot never reads. The Master Brain "P0 rebuild" note is stale — recommend closing it.
(Local copy refreshed for hygiene; non-production.)

---

## DIRECTIVE RE-TRIAGE (audit_directives.jsonl on OCI — 79 rows)

Current: failed_permanent=43, pending_review=9, context_only=26, processed=1.
Re-triage of the 43 failed_permanent + 9 pending_review:

### A. RESOLVED THIS SESSION — reclassify `superseded` (≈12 rows)
- **main.py gc.collect() memory leak** — ~10 rows (2085ms/2586/2775/2891/3252/3884/3954/4424/4576ms,
  "heap may be growing", VOLSHADOW/dataframe retention theories). ALL resolved by `gc.freeze()`
  (`0ab73d7`) — verified per-cycle gc now <200ms. These were misattributed as a leak; root cause
  was permanent-heap traversal, not accumulation.
- **run_cycle.py RC-4 "GTC buy stop 42210000 rejected for shorts"** — resolved by cover-on-breach
  (`cf10c9f`/`a53ce92`, RTH + AH paths). Directive's own recommended_fix ("if stop<=market, market
  buy to cover") is exactly what shipped.
- **risk_manager/orphan_manager "position count drift risk=0 vs tracker=2"** (≥4 rows) — root-caused
  and fixed via Option B step-1/step-2 + P0-STARTUP Alpaca-authoritative override (this session +
  `2933a35`). P0-STARTUP now hard-syncs to Alpaca count on startup.

### B. ALREADY CLOSED per CLAUDE.md live RC table — reclassify `superseded` (≈4 rows)
- **risk_manager RC-3 "STOP_HIT/EXIT log expected not fill price"** → RC-4, CLAUDE.md marks CLOSED
  (record_exit uses filled_avg_price / fetch_actual_fill_price). *Verify one site to be safe.*
- **risk_manager RC-4 "shares truncated to zero for fractional"** → RC-7, CLAUDE.md marks CLOSED
  (guard at entry_logic.py L1127). *Verify.*
- **portfolio_tracker "misinterprets short entries as closing long / short-lot FIFO"** → known
  routine item, 0 occurrences currently. Keep as watch, not failed_permanent.
- **confluence "chart context None/UNKNOWN for all entries"** → known routine item, 0 occurrences
  currently. Watch.

### C. NOISE / audit-artifact — reclassify `context_only` (≈5 rows)
- "Truncated source code prevents full audit" (explicitly "not a bot bug").
- "Co-Authored-By attribution wrong" (cosmetic).
- "MARKET_HOLIDAY semantically misleading for Columbus/Veterans Day" (cosmetic).
- "macd_bullish_cross misleading name for short signals" (cosmetic naming).

### D. REAL, STILL-OPEN — candidates for their own packages (verifying now)
- **orphan_manager CRITICAL** — GTC stops stuck in PENDING_CANCEL never clear gtc_stop_order_id →
  can't resubmit protective stop. → **VERIFYING (agent a87a4b8f)**.
- **orphan_manager HIGH** — cancel_and_reconcile_gtc_stops fail-OPEN if quarterly_holds.json
  unreadable → cancels QHM stops. → **VERIFYING (agent a87a4b8f)**.
- **run_cycle HIGH** — ATH proximity gate disabled by `float("inf")` sentinel on fetch_bars
  failure (fail-open gate). → verify + likely fix package.
- **config.py MEDIUM** — BUCKET_B_MAX_POSITIONS=999 placeholder vs MAX_OPEN_POSITIONS; and
  INTRADAY_STOP_ATR_MULT / VOL_TIER_HIGH_STOP_INTRADAY mismatch with CONFIG CONSTANTS. Config
  coherence — verify against live profile before touching (profile overrides may make it moot).
- **confluence RC-3 "VOLSHADOW computed but not enforced as a gate"** — verify: is volshadow a
  shadow-only research signal (intended) or an intended gate that's silently not firing?
- **run_cycle RC-2 "sizing uses MAX_RISK as notional not stop-distance risk"** — significant
  sizing-philosophy finding; needs board (touches sizing). Flag for a dedicated session, not a
  quick patch.
- **portfolio_tracker RC-3 "ISO8601 'Z'/+00:00 parse failure"** — small, real, verify.
- **scan_to_html.py RC-1 "NaN→int in options fetch"** — small, non-RTH, verify.
- **entry_logic RC-3 "cluster entries / correlation-aware sizing"** and **risk_manager RC-4
  "breakeven push timing"** — these are strategy-design roadmap items, not bugs; route to
  Future Roadmap, not directives.

---

## CROSS-STRATEGY PHASE 3 AUDIT (roadmap P0) — COMPLETE

Two cold domain agents across exit_logic.py, handlers.py, quarterly_hold_manager.py,
movers/strategy.py.

**Guards confirmed COMPLETE (Option B working):**
- check_exits() QHM guard at exit_logic.py L1112 (loop-top, before all 13 exit paths). ✅
- check_partial_exits() QHM guard at exit_logic.py L212 (loop-top). ✅
- safe_close_all() QHM exclusion at handlers.py L77 (both routine + circuit-breaker). ✅
- movers/strategy.py both close_position() sites guarded (L413/L506). ✅

**Open findings → packages below.**

---

## PACKAGE 1 (flagship) — EH QHM guard in `_check_exits_extended_hours()`  ✅✅ SHIPPED (`9d0942a`)
**Gap (verified, verbatim read):** exit_logic.py `_check_exits_extended_hours()` per-symbol loop at
**L2075** has NO QHM guard; its three EH exit paths (trail-stop L2180 / hard-stop L2216 / partial
L2247, plus record_exit L2138) can `submit_limit_order` on a QHM symbol during pre-market
(4:00–9:30 ET) and after-hours (16:00–20:00 ET) — the exact windows QHM actively manages its own
position + GTC stop. This is the live ingress through which a partial close of a QHM position could
still occur (Package 2 is its downstream corruption). `_get_qhm_syms` already imported at L53.

**EXACT DIFF** (insert as first statements in the L2075 loop, mirrors approved L212/L1112 guards):
```python
     for symbol, trade in list(tracker.open_trades.items()):
+        # QHM ownership guard (Option B, 2026-07-01): the main bot must not manage
+        # exits for a QHM-held symbol during extended hours — QHM owns its Alpaca
+        # position + its own GTC stop, and the EH windows overlap QHM's active
+        # window. Quiet skip (RTH collision alert fires once via check_exits).
+        if symbol in _get_qhm_syms():
+            logger.debug("[%s] QHM-held — skipped by extended-hours exit management.", symbol)
+            continue
         if trade.get("status") != "open":
             continue
```

**GATES:**
- Full read: exit_logic.py 2258 lines (verbatim EH function + guard pattern + imports). ✅
- 10-pt + RC-1..8: PASS (no datetime/path/except/exit-price/write/API-field/sizing/buffer change).
- Board cross-strategy audit: identified this as the sole remaining exit-path gap. ✅
- Static (scratch copy): py_compile PASS, ruff clean. ✅
- Gro: **APPROVE** (after counter-prompt — initial NEEDS-CHANGES on alert-vs-quiet + pm_exit_order_id
  edge both resolved: L212 quiet-skip precedent + Option-B ownership boundary). GAI: **APPROVE**.
- Cold second-agent logic review: **PASS** (all 5 checks; independently confirmed `pm_exit_order_id`
  is only ever set inside `_check_exits_extended_hours` itself — never by the RTH path — so a QHM
  symbol cannot carry one from the intraday bot; the edge is moot).
**TO APPROVE:** say "approve package 1" → I apply, static, commit→push→git-pull, restart, verify.

## PACKAGE 2 — QHM partial-external-close hardening (defense-in-depth)  [VERIFIED, needs board]
`quarterly_hold_manager.py`: `_detect_external_close()` (~L830) checks only `alpaca_pos is None`,
never `alpaca_pos.qty < pos.qty_filled` → a PARTIAL external close is missed (position stays ACTIVE
with stale qty). `_check_fill_and_advance()` (~L1266) computes `new_total = qty_filled + live_qty`
assuming ALL live Alpaca qty is new fills → a partial external close corrupts tranche state
(qty_filled / tranches_filled / tranche advance). Only reachable THROUGH Package-1's gap, so
Package 1 closes the ingress; Package 2 is defense-in-depth. **Needs board** (touches QHM state
machine) + my own verbatim verification of the exact arithmetic before a diff. Recommend: approve
Package 1 first (closes the hole), then schedule Package 2 as a board session.

## PACKAGE 3 — orphan_manager QHM fail-open  ✅✅ FULLY APPROVAL-READY
**Bug (verified against current code):** `cancel_and_reconcile_gtc_stops()` loads QHM-protected
symbols from `data/state/quarterly_holds.json` in a try/except (L131-149). On ANY parse/read error
(file present but corrupt/partial-write) it falls back to an EMPTY set with only a WARNING, then
every QHM symbol fails `if symbol in _qhm_protected` → **all QHM GTC protective stops are cancelled
before RTH → QHM positions naked.** CRITICAL severity — confirmed live-code, not stale directive.
(File simply ABSENT = empty is correct; the danger is present-but-unreadable.)

**Fix posture:** fail-CLOSED — distinguish file-absent (empty OK, normal cancellation) from
file-present-but-unreadable (retain ALL GTC stops that cycle + CRITICAL log + Slack). Rationale:
naked-QHM capital risk ≫ double-stop transactional risk (a retained protective stop is always safe;
worst case is a benign Alpaca 40310000 on the RTH DAY-stop submission).

**EXACT DIFF (applied to execution/orphan_manager.py, currently uncommitted in working tree):**
```python
# Site 1 — load block: distinguish absent from unreadable, flag + escalate
     _qhm_protected: frozenset[str] = frozenset()
+    _qhm_load_failed = False
     try:
         ... (unchanged load) ...
     except Exception as _qhm_e:
-        logger.warning(
-            "QHM state file read failed — treating all symbols as unprotected: %s",
-            _qhm_e,
-        )
+        # FAIL-CLOSED (2026-07-01, board + Gro + GAI): the file EXISTS but is
+        # unreadable/corrupt — we KNOW there may be QHM holds but cannot enumerate
+        # them. Cancelling GTC stops now would strip protection from a QHM position
+        # (naked-QHM capital risk >> double-stop transactional risk). Retain ALL
+        # stops this cycle and escalate. A simply-ABSENT file does not raise, so this
+        # branch fires only on a present-but-corrupt file.
+        _qhm_load_failed = True
+        logger.critical(
+            "QHM state file UNREADABLE (%s) — GTC reconciliation FAIL-CLOSED: "
+            "retaining ALL overnight GTC stops this cycle to avoid stripping a "
+            "QHM position's protection. Manual review required.", _qhm_e,
+        )
+        try:
+            send_slack(   # already module-imported (L34)
+                f":rotating_light: QHM state file unreadable in GTC reconcile — "
+                f"FAIL-CLOSED, all GTC stops retained this cycle ({_qhm_e})."
+            )
+        except Exception as _ofc_e:
+            logger.warning("fail-closed Slack alert failed: %s", _ofc_e)

# Site 2 — consume block: retain ALL stops when load failed
     # QHM symbols retain their protective GTC stops — do not cancel.
-    if symbol in _qhm_protected:
+    # FAIL-CLOSED: if the QHM state file was unreadable, we cannot classify any
+    # symbol, so retain ALL stops this cycle (board + Gro + GAI 2026-07-01).
+    if symbol in _qhm_protected or _qhm_load_failed:
+        _retain_reason = (
+            "QHM symbol" if symbol in _qhm_protected
+            else "QHM-load-failed fail-closed"
+        )
         logger.info(
-            "[%s] QHM symbol — retaining GTC stop %s"
-            " (not cancelling before RTH).",
-            symbol, order_id,
+            "[%s] %s — retaining GTC stop %s (not cancelling before RTH).",
+            symbol, _retain_reason, order_id,
         )
         continue
```

**GATES (all cleared on this exact final diff):**
- Full read: orphan_manager.py 1459 lines. 10-pt + RC-1..8: PASS.
- Board (2 domain agents): Katsuyama/Schneier fail-safe-defaults lens **APPROVE**; Peterffy
  infrastructure-doctrine lens **APPROVE** (both cite naked-position risk ≫ retained-stop risk).
- Static: py_compile PASS, ruff clean (incl. RUF100), mypy clean.
- Gro: **APPROVE**. GAI: **APPROVE**. (Clean on the exact final diff — no counter-prompt needed.)
- Cold second-agent logic review: **PASS** — all 5 checks (flag scoping/no-leak, both branches
  correct, Slack-failure isolation confirmed via nested try/except).
**TO APPROVE:** say "approve package 3" → I apply, commit→push→git-pull, restart, verify.

## PACKAGE 4 — ATH SPY-52w fail-open (run_cycle.py)  ✅✅ FULLY APPROVAL-READY
**Bug (verified):** when `fetch_bars("SPY", daily, 252)` fails (data outage), `_spy_52w_high` stays
`0.0` → `_spy_ath_dist_pct` defaults to 99.0 → BOTH the ATH MIN_SCORE floor-raise AND the
risk_manager 0.90x ATH stop/target scalar silently DON'T apply — entries proceed as if SPY is far
from its high even though proximity is unknown. Inconsistent with the ORB gate, which fails CLOSED.
HIGH severity, RTH-scoring-impacting.

**Fix (minimal — corrects only the SOURCE value; zero changes to either downstream consumer, both
already correctly gate on `_spy_52w_high > 0`):** persist last-known SPY 52w-high to
`data/state/spy_52w_high.json` (atomic write) on every successful fetch; on failure, reuse the
cached value with staleness logging (a 52w high moves negligibly day-to-day); on true bootstrap (no
cache ever + fetch fails) do a cheap 2-bar secondary fetch and set `_spy_52w_high = last_close ×
1.005` (forces <1% ATH distance → full protection applies) — board-mandated conservative default.

**EXACT DIFF (applied to strategy/run_cycle.py, currently uncommitted in working tree):**
- New module-level helpers `_save_spy_52w_high()` / `_load_spy_52w_high()` (atomic tmp→replace
  write, RC-5 compliant) added after `_PROJECT_ROOT = ...`, before `def run_cycle(`.
- The existing SPY 52w-high fetch block: on success now also calls `_save_spy_52w_high(...)`; on
  failure now tries cache-reuse, then bootstrap-conservative, before leaving `0.0` unchanged only
  on a full double-failure (identical to today's behavior in that one rare case — self-heals next
  pre-market cycle).
- Removed a now-stale `# noqa: F401` on `import json` (json became genuinely used).
Full diff: `git diff strategy/run_cycle.py` (34 lines net added).

**GATES (all cleared on this exact final diff):**
- Full read + trace: confirmed both downstream consumers (run_cycle Layer-5 MIN_SCORE floor,
  risk_manager.py 0.90x scalar) are UNTOUCHED and already correctly gate on `_spy_52w_high > 0`.
- Board (2 domain agents): Derman/Taleb model-risk lens **APPROVE** (persist-last-known = explicit,
  honest degradation; bootstrap-conservative = antifragile, not fragile). Thorp/Harris sizing-
  discipline lens **APPROVE** (uncertain state → conservative, never looser; mirrors quarter-Kelly
  precedent already in config.py).
- Static: py_compile PASS, ruff clean (incl. RUF100 unused-noqa), mypy clean.
- Gro: **APPROVE** (after counter-prompt resolved two false-positive concerns — `.iloc[-1]` is
  already guarded by the preceding `.empty` check; the date-only subtraction has no RC-1 tz issue).
  GAI: **APPROVE**.
- Cold second-agent logic review: **PASS** — all 4 branches (success / cache-hit / bootstrap-success
  / double-failure) verified sound, no exception can abort `run_cycle()`, no shadowing/circular-import.
**TO APPROVE:** say "approve package 4" → I apply, commit→push→git-pull, restart, verify.

## PACKAGE 5 — orphan_manager reconcile_positions() unguarded QHM adoption  ✅✅ FULLY APPROVAL-READY
**Found live, during Package 3/4 deploy verification (2026-07-02 03:14 ET boot log):** `reconcile_positions()`
(startup-only, main.py L747) adopts any Alpaca position missing from the intraday tracker as an
"orphan" — with NO QHM exclusion. Confirmed live: NVDA + GOOGL (QHM's own ACTIVE holds) were adopted
into `tracker.open_trades` with a computed intraday ATR stop, on this very restart.

**Verified downstream effects (full-function verbatim read):**
- Exit management SAFE — check_exits/check_partial_exits/EH all independently guard on
  `if symbol in _get_qhm_syms(): continue` regardless of how the symbol entered the tracker.
- EOD P&L SAFE — portfolio_tracker.py FIFO already filters QHM symbols.
- P0-STARTUP observability SAFE — main.py already filters QHM symbols (this session's earlier fix).
- The computed ATR stop is inert — never submitted to Alpaca (existing `gtc_stop_order_id` guard
  blocks re-submission), never checked (QHM guard skips check_exits for the symbol).
- **The real effect:** `risk.open_positions = max(risk.open_positions, len(tracker.open_trades))`
  counts the adopted QHM symbols, inflating intraday position count → **blocks real intraday entries
  for up to N phantom slots per restart** (N=2 tonight, NVDA+GOOGL). Opportunity-cost bug, not
  capital-risk — no naked positions, no P&L corruption.

**EXACT DIFF (applied to execution/orphan_manager.py, currently uncommitted in working tree):**
```python
     orphans = alpaca_symbols - tracker_symbols
+    # QHM ownership guard (Option B, 2026-07-02): a QHM-held symbol looking "orphaned"
+    # here is expected, not a bug — QHM owns it via its own state machine. Adoption was
+    # harmless for exit management (downstream QHM guards already skip it) but inflated
+    # risk.open_positions, blocking real intraday entries. _get_qhm_syms() is already
+    # called later in this same function at this identical call time, proven populated
+    # by production logs before this fix.
+    orphans = (alpaca_symbols - tracker_symbols) - _get_qhm_syms()
```
(Diff shown reordered for clarity — actual patch replaces the single assignment line with the
guarded version, comment included; `git diff execution/orphan_manager.py` has the literal form.)

**GATES (all cleared):**
- Full read: reconcile_positions() verbatim (Explore agent, verbatim rule) + confirmatory direct
  read of the exact patch site. 10-pt + RC-1..8: PASS.
- Board (2 domain agents): Katsuyama/Majors reliability lens **APPROVE** (honest boundaries >
  adopt-then-guard; non-blocking follow-up below). Thorp/Peterffy execution-risk lens **APPROVE**
  (P2 capacity-leak framing — quantified ~20% intraday capacity loss per restart with 2 QHM holds
  and MAX_OPEN_POSITIONS=10-scale reasoning; not a capital-loss class).
- Static: py_compile PASS, ruff clean (incl. RUF100), mypy clean.
- Gro: **APPROVE** (P1). GAI: **APPROVE** (P1). (Severity label P1 vs board's P2 is a minor
  classification variance, not a disagreement — all four voices unanimous APPROVE, zero REJECTs.)
- Cold second-agent logic review: **PASS** — confirmed the fix is complete (the other two branches
  in the same function — "externally closed" and "size mismatch" — structurally can never see a QHM
  symbol under Option B, so no additional guard needed there); confirmed symmetric with `701eb47`
  (same session's main.py P0-STARTUP fix — that was the *read*-side filter, this is the *write*-side).
**Non-blocking follow-up (logged, NOT part of this fix):** board reliability lens suggested mirroring
Package 3's fail-closed pattern here too, for the rare case where `quarterly_holds.json` is corrupt
at this exact call (today: silently reverts to pre-fix behavior, i.e. no worse than before — a
CRITICAL-escalated retry would be strictly better but is a separate, low-urgency session).
**TO APPROVE:** say "approve package 5" → I apply, commit→push→git-pull, restart, verify.

## FINDING (orphan_manager, NOT a bug) — PENDING_CANCEL retention
Directive claimed "GTC stops stuck in PENDING_CANCEL never clear gtc_stop_order_id." Verified: the
ID retention is INTENTIONAL (board 27-0 2026-04-30; GAI-fix 2026-05-11 comment — clearing it evicts
the trade from GTC protection permanently), with CRITICAL+Slack escalation at ≥3 cycles (L239-259).
Working as designed. No patch. (Optional future: auto-recovery after N cycles — board question.)

## DECISION FORK (for Rafael, board-required) — circuit-breaker vs QHM
`safe_close_all(circuit_breaker=True)` currently STILL excludes QHM (handlers.py L77), but
Architecture Invariant 7 / the docstring says a true circuit-breaker "closes everything
unconditionally." GAI raised this earlier too. Options: (a) keep QHM protected even on
circuit-breaker — current behavior; QHM has its own wide GTC stops (Option-B-consistent), or
(b) flatten QHM on circuit-breaker only. Needs board + your call. NOT resolved autonomously.
Recommend updating the handlers.py docstring either way so code and stated policy agree.
