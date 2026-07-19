# alpaca-mtf-bot — Project State (current, overwritten each session)
**As of 2026-07-19 (Sun, interactive — Rafael present)**

## Bot
All 4 OCI services active. HEAD `858c7df` (local = GitHub = OCI, in sync). Market CLOSED (next open Mon
2026-07-20). Paper. 5 open positions (Alpaca=tracker=5, verified at startup). `OWNERSHIP_GUARD_ENFORCE=False`
(never-sell floor still DORMANT — arming is gated behind F6 prereq #2). PDT abolished | Profile paper |
MIN_SCORE=9/12 | KELLY_FRACTION=0.25.

## This session (2026-07-18) — SHIPPED + LIVE
- **`d93be65` — F6 D-obs + OBS-A (DARK/inert, zero live change).** Never-sell-guard hardening, prereq-2
  arming condition (b). OBS-A: a type-corrupt ownership-ledger qty now fails CLOSED for a cached-protected
  symbol (and pages), fails OPEN (keystone) otherwise — never crashes the exit path (function-boundary
  never-raises wrappers on close/partial/stop; Exec-risk's PLTR −$16k blocked-stop scenario averted,
  runtime-proven 13/13). D-obs: `page_floor_blind` — throttled (kind,symbol / 30-min) phone+Slack page on
  fail-closed AMBIGUITY (ledger/Alpaca unreadable, drift-freeze, type-corrupt); deterministic floor-binding
  rejects stay log-only. Files: alerts.py, execution/ownership_guard.py, execution/broker.py. Gate: 5-voice
  board (Reliability+Exec-risk+Observability+Gro+GAI) all APPROVE-WITH-CHANGES → cold-2nd PASS → statics →
  self-test 13/13 → FINAL preship (broker/alerts real Gro+GAI APPROVE; ownership_guard gai=APPROVE
  gro=WAIVED on Groq TPM). Design `logs/f6_dobs_obsa_design_2026-07-18.md`.
- **Verified this session:** F6 prereq #3 was already DONE+LIVE (`b5d519c`/`4c3ced6`) — the prior handoff
  checkpoint's "WIP/uncommitted" was STALE. Reports durability (Option B) confirmed cross-account: 269
  routine reports in git + OCI (Gemini midday/nightly, meta-audit, wtp). Gemini routine reports are NOT in
  Master Brain (only project-state is) — optional future add.
- **RAM diagnosed (Rafael: no spend).** Box is 956MB + 4GB swap — undersized; bot RSS 550–600MB during RTH →
  free RAM 58–95MB is NORMAL → the two redundant watchdogs (memory_watchdog <200MB */30, ram_watch <80MB
  */6) alarm on steady state → Slack spam; and swap-thrash slows scan cycles → the cycle-hang watchdog
  restarted the bot twice mid-RTH on 7/17. Fix path = software levers only (box relocation from Phoenix is
  locked; no spend): recalibrate/collapse the alerts + trim the pandas working set. DEFERRED this session.

## 2026-07-19 (later, autonomous) — also shipped + a halted item
- ✅ **Slack Gemini-report format fix — SHIPPED + LIVE** (`9341516`): audit→Slack renderer now one clean
  grouped entry per finding (no `**` leak, no dup, no 3–4× repeats, word-boundary truncation).
- ⏸️ **RC-4 datetime-parse P&L fix — PREPPED, NOT SHIPPED (halted on session limit, resets 5:40am PT).**
  #1 open RTH bug (CATASTROPHIC): raw `datetime.fromisoformat()` (portfolio_tracker.py:166/290/404 +
  fill_reconciler.py:156) fails on Alpaca `Z`/variable-fraction timestamps → SMCI never reconciles
  (permanent P&L corruption) + re-queue loop. Design + full-read gate DONE; BGG gate NOT run. Resume per
  handoff ⏩ / `logs/datetime_parse_pnl_fix_design_2026-07-19.md`. Fix = tolerant `_iso_to_dt` in state_io.

## Open Items (priority)
1. **F6 v2 alert-polish** (before arming): cycle-rollup + recovery/all-clear + heartbeat; `load_ledger`
   qty-type validation at source. → then live-verify a rejected sell (paper canary) → **prereq #2 = arm
   `OWNERSHIP_GUARD_ENFORCE=True`** (LAST; `logs/f6_activation_BLOCKED_2026-07-17.md`). NO SEED until #2.
2. ✅ **RAM alert-spam recalibration — SHIPPED + LIVE** (`5050b6e`, 2026-07-19): single `*/6`
   memory_watchdog (ram_watch retired); RTH crit `<15MB`/warn `<30MB` available (below the 58MB floor →
   spam gone); relabel "free"→"available"; off-hours restart unchanged + ping 1/day + 24h count +
   escalation + 20-min cooldown. v2 deferred: "Online"-ping suppression (broken-as-designed), swap-pressure
   alert, dynamic threshold. Does NOT fix the box tightness (size-up refused) — LIVE item.
3. **Slack Gemini-report format fix**: audit→Slack renderer emits broken/duplicated fragments (literal `**`,
   truncated sentences, same finding 3–4×). Renderer bug in nightly_audit.py / midday_audit.py block builder.
4. **Checkpoint automation "B"** (dedicated `session-checkpoint` branch) — Rafael wanted it; other account's
   push-to-main version abandoned (stranded uncommitted: scripts/checkpoint_hook.py, logs/session_checkpoint.md).
5. Options/0DTE program (design stage): 0DTE intraday-capture signal design (board+Gro+GAI); SPX source
   BLOCKER (Alpaca has no SPX); two-column options_scanner build after signals.
6. UX total redesign of 5 HTML pages (queued behind bugs). Evolution mandate (learning loops).

## Hard Invariants
- paper=True hardcoded in execution/broker.py — never change without full board vote.
- SPY 5-min bar-over-bar is the SOLE entry gate. All P&L from Alpaca fills API only (tracker = cross-check).
- T1 (Alpaca) for all equity/ETF data — yfinance only for ^VIX, ^VIX3M, JPY=X.
- Never-sell floor: fail OPEN for a NON-protected symbol (keystone — blocking a legit exit is the worse error);
  fail CLOSED only for a genuinely-protected symbol on ambiguity. Never mask a loss.
- Gro/GAI lean prompts (no leading conclusions); ship only after BOTH APPROVE the exact final diff
  (Gro-WAIVE allowed only on a confirmed Groq rate/TPD limit, GAI still required).
- Board + Gro + GAI POV on EVERY fork BEFORE it reaches Rafael (Open Question Protocol, no exemptions).

## Way of Working
- Cross-account durability: every session's work lands in git + handoff.md + Master Brain; chat-only artifacts
  are not an acceptable record. Resume = `git pull` → read handoff.md ⏩ block → query Master Brain.
- Execute don't ask which item next; surface genuine external blockers. No spend without Rafael's OK.
