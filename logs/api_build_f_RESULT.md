# API Build Result — Build F (HALT/mass-liquidation redesign)

**Date:** 2026-07-09
**Branch:** `claude/build-f-2026-07-08` (pushed — new branch, NOT merged to main)
**Commit:** `69025fc` — "Build F: news HALT → display-only tier + Alpaca venue-state halt observability"

## STATUS: READY FOR REVIEW+MERGE

## What changed (5 files)

1. **`events/news_monitor.py`**
   - `get_news_size_multiplier()` now always returns `1.0` (the `0.0`-on-HALT branch is retired); dropped
     the unused `price_change_pct` parameter (no caller ever passed it).
   - `_classify()`'s `HALT` branch now returns `1.0` instead of `0.0` — HALT is a display tier, same as
     CAUTION/MONITOR. Keyword sets and `get_active_event_type()` labeling are unchanged.
   - Retired the dead `PRICE_CONFIRM_THRESHOLD` constant (zero references anywhere else in the repo).
   - Added `has_active_halt_keyword()` — the new signal `run_cycle` uses to drive the entries-only block
     (replaces the retired `news_size_mult==0.0` trigger).

2. **`strategy/run_cycle.py`**
   - Reconciled the F-INTERIM entry-block: `_news_halt_block_entries` is now driven by
     `news.has_active_halt_keyword()` instead of the now-permanently-`1.0` `news_size_mult`. Same
     behavior (per-cycle, self-clearing, never sets the session-wide halt latch), new source.
   - Added **Halt Observability**: `get_clock().is_open` + `get_asset_tradable("SPY")` + SPY's
     session-cumulative % vs. prior close checked against real MWCB `-7/-13/-20` bands. Emits a
     `halt_eval` event to `trade_events.jsonl` every RTH cycle (`keyword_hit`, `spy_5m_pct`, `qqq_5m_pct`,
     `venue_status`, `verdict`). On a confirmed real halt: blocks new entries for the cycle, fires one
     debounced CRITICAL Slack alert. When SPY's tradable status can't be confirmed (API gap): blocks
     entries out of caution but does **not** fire the CRITICAL alert or claim a confirmed halt (avoids
     manufacturing false alarms from routine API noise — see gate history below).
   - **Never liquidates.** `safe_close_all()` is untouched; still called only from `main.py`'s
     user-shutdown path.

3. **`execution/broker.py`** — added `get_asset_tradable(symbol)`, the only new `TradingClient` call site
   (execution-isolation rule respected — instantiated nowhere else).

4. **`alerts.py`** — added `alert_venue_halt()`, modeled on the existing `alert_crash`/`alert_kill_switch`
   CRITICAL-alert pattern.

5. **`trade_logger.py`** — documentation-only: added `halt_eval` to the Event types docstring list (the
   existing generic `**extra` kwargs mechanism already supports the new fields with zero code change).

## Files intentionally NOT touched (in scope per design doc, deliberately out of scope for this build)

- Bucket-A-on-circuit-breaker reversal — explicitly deferred to a separate board vote (design doc).
- Forever-Hold accumulation protocol — a separate design; its own doc says it is not yet cleared for
  API build ("STILL OWED before API... final board+Gro+GAI on the fully-mapped combined proposal").
- `execution/risk_manager.py` and `strategy/signal_generator.py` — both have a parameter also named
  `news_size_mult`, but it's fed by `_spy_risk_mult` (SPY/MRI-derived), not by the news module's
  multiplier. Confirmed via full trace; correctly left untouched.
- `events/handlers.py` (`safe_close_all`, `_halt_entries_for_session`) — read in full for context, zero
  lines changed, per the locked design ("no automated mass-liquidation reflex anywhere").

## Gate verdicts

- **Full read gate:** `events/news_monitor.py` (1828L), `strategy/run_cycle.py` (1893L),
  `events/handlers.py` (131L), `execution/broker.py` (763L), `alerts.py` (407L) — all read in full via
  direct Read tool (≤300-line chunks), not Explore subagent (Explore is documented to read excerpts
  rather than full files, which would violate the project's anti-summary full-read rule more than a
  direct chunked read would).
- **10-point audit + RC-1 through RC-8:** written to `logs/tb_audit_log.md` (2026-07-09 entry). All RC
  classes PASS or N/A for this change set.
- **Static analysis:** `py_compile` / `mypy --warn-unreachable` / `ruff check --select E,W,F,B` — all
  clean on all 5 changed files (mypy installed into `/home/ubuntu/mtf-bot/venv` for this session, since
  it wasn't present; ruff was already there).
- **Cold board** (3 independent Explore subagents, no shared context): Thorp/Taleb masked-loss seat —
  APPROVE-WITH-CHANGES (one finding, addressed — see below); Harris cross-strategy seat — APPROVE;
  Kim/Peterffy reliability seat — APPROVE.
- **Cold second-agent** (CLAUDE.md Step 5b, 4-point mandate: logic inversion / off-by-one / missing
  conditions / branch completeness): PASS on all 4.
- **Gro + GAI:** 3 rounds via direct API (Groq `llama-3.3-70b-versatile`, Gemini `gemini-2.5-flash`).
  - Round 1: both raised concerns; GAI's most serious claim (a false -100% SPY session-decline from
    `_main._spy_last_close` defaulting to `0.0`) turned out to be a misread of Python truthiness.
  - Round 2 (counter-prompt with the board's rebuttal): **both explicitly conceded** the -100% claim was
    wrong. Both held a real, substantive point — `get_asset_tradable()` returning `None` on failure was
    silently treated as "no signal," which under-weights a genuinely degraded data leg on a risk-path
    check (Thorp/Taleb's board seat independently raised the identical concern).
  - **Synthesis applied to the diff:** added `_venue_uncertain` — fails closed for entries only when the
    tradable status can't be confirmed (cheap, self-clearing, matches this file's own pre-existing
    ORB-gate precedent of "any feed error → fail closed"), but does **not** claim a confirmed halt or
    fire the CRITICAL alert for it (preserves alert integrity — the entire reason Build F exists is to
    stop manufacturing false-positive-driven reactions).
  - Round 3 (final pass on the exact updated diff): **Gro APPROVE. GAI APPROVE.**
- **code-review-graph MCP impact analysis:** not available in this headless session (not found via
  ToolSearch) — substituted with exhaustive manual repo-wide grep tracing of every touched symbol's
  callers (documented in the audit log).

## Forward-looking findings logged, not fixed (out of scope for this diff)

- `strategy/run_cycle.py`'s pre-existing ANOMALY-4 check references MRI levels `"HALT"`/`"STRESSED+"`
  that `MacroRiskIndex.level()` never actually returns (real levels: NORMAL/ELEVATED/STRESSED/HIGH/
  CRITICAL) — dead code since before this session, unrelated to and unaffected by this diff.
- Gro's suggestion of an alternate/fallback data source for `get_asset_tradable()` failures — not
  adopted this session; the `_venue_uncertain` fail-closed-on-entries fix covers the practical risk.

Both logged in `logs/tb_audit_log.md`'s 3-Point AI Summary, Point 3.

## Next step

Human review + merge of `claude/build-f-2026-07-08` into `main`. No restart performed, no deploy
triggered — this is a worktree-isolated branch push only, per task scope.
