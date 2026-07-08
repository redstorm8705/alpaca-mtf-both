# Handoff — alpaca-mtf-bot
**Updated:** 2026-07-08 (autonomous scheduled session, evening) | NOT /wrap-up

> **READ FIRST:** Build F design is DONE, awaiting Rafael's confirmation —
> `logs/pending_claude_session_2026-07-08.md` (5 plain-English yes/no questions). Build map:
> `logs/api_build_packages_2026-07-08.md` (the F/A/B/E slate).

## Bot Status
- **Running:** 4 services active on OCI (129.153.208.32). Git HEAD `9d03be1` (local = GitHub = OCI).
- Account healed (~$2,782). Kill switch CLEAR. Holding QHM NVDA/GOOGL (both stopped) + intraday.
- **Interactive-vs-API cost protocol is LIVE** (CLAUDE.md): interactive scopes/designs + runs board/Gro/GAI
  on the DESIGN; API implements the pre-scoped diff + gates the DIFF + ships. Only Rafael changes it. Budget $20/mo.

## Resolved this session (2026-07-08)
- **INCIDENT #2 — false news-HALT mass-liquidation → INTERIM FIX SHIPPED (commit `9d03be1`, LIVE).**
  A benign tariff QUESTION headline ("Can Trump cut off all trade with Spain?") matched the KEYWORDS_HALT
  substring "national emergency" → `_safe_close_all(circuit_breaker=True)` sold the whole non-QHM book
  (6 pos, ~-$26, 11s). Root: news HALT is a raw substring match + the price-confirmation gate was stripped
  from the HALT path (`get_news_size_multiplier` price param is "Unused"). Interim: news HALT now blocks
  NEW ENTRIES only, never liquidates. Full gate passed (board 3/3 + Gro + GAI; both final-pre-ship REJECTs
  were misreads, withdrawn on counter-prompt). Logged: `logs/tb_audit_log.md`.
- **INCIDENT #1 (2026-07-07 Alpaca desync)** — self-healed; account recovered. Its roadmap = Build A below.
- **Cost:** morning health report retuned to **$0.044/run** (was $0.86) — OCI cron 6 AM PT →
  `scripts/oci_report_runner.sh` (Haiku, clean context, pre-gathered facts) + `scripts/collect_health_facts.py`.
  Zero local/Mac dependency.

## NEXT SESSION ENTRY POINT — Build slate (Rafael 2026-07-08): F → A → B → E
Full scope: `logs/api_build_packages_2026-07-08.md`.
- **F (full HALT/mass-liquidation ARCHITECTURE REDESIGN) — DESIGN DONE, AWAITING RAFAEL.** Full read gate
  satisfied this session: `events/news_monitor.py` (1828L, Explore subagent verbatim), `events/handlers.py`
  (132L), `strategy/run_cycle.py` L980-1619 (re-confirmed). 10-pt audit + RC-1..8 in `tb_audit_log.md`. MODE 2
  board (4 cold seats: Taleb, Harris, Simons, Peterffy+Kim) + Gro + GAI ALL independently reviewed the 4 design
  forks — **6/6 UNANIMOUS, no disagreement**: (1) news signals must NEVER trigger mass liquidation, entries-only,
  permanently; (2) any real "close everything" trigger must be built FRESH on real SPY-price-threshold and/or
  Alpaca exchange-halt signal, never news — dead `PRICE_CONFIRM_THRESHOLD`/`price_change_pct` to be REMOVED not
  resurrected; (3) retire keyword-driven HALT entirely (not fixable via better phrase-matching — this week's
  false-positive AND false-negative are the same structural defect); keywords stay for CAUTION/MONITOR only;
  (4) no NEW cross-strategy collision found, but flag: QHM guard in `safe_close_all()` is QHM-specific, not a
  general ownership system — retired Movers' dormant untagged lots will be swept by any future real
  circuit-breaker call same as main-bot lots; must be an explicit tested/documented decision before ship, not
  silent inheritance. **Next action: Rafael answers the 5 questions in
  `logs/pending_claude_session_2026-07-08.md`, then this becomes a pre-scoped API patch package.**
- **A (Data-Integrity Safe Mode)** — scoped to the LINE this session (full reads done: run_cycle 1865,
  orphan_manager 1624, risk_manager 802). Explained-P&L glitch validator → safe-mode → glitch-vs-real kill
  tagging → orphan/trade gating. A4 = the explained-P&L guard the existing Guard A/B structurally miss.
- **B (Orphan-stop root)** — scoped to the LINE (orphan_manager L714 + L1320; one confirming read left:
  `portfolio_tracker.py:~965`). Cancel stops BEFORE record_exit in the external-close paths, fail-closed, + sweep.
- **E (QHM accumulation)** — decisions LOCKED (never-sell; buy-more-on-dips; fixed-$ slice
  `max(1, floor(0.03·equity ÷ price))`; 20% per-name ceiling). Full read of `quarterly_hold_manager.py`
  (1954L) still owed — it's a rewrite of the entry/exit core.

## Process (Rafael directive 2026-07-08 — binding)
The board + Gro + GAI audit of the FULLY-MAPPED proposal is the ABSOLUTE LAST STEP before anything goes to
the API — scope 100% mapped, accounting for cross-strategy implications, existing bugs, and hotspot files.
The 2026-07-08 F-INTERIM ship validated this: the gate caught two misread-REJECTs and held the ship until
resolved. Cold board is MANDATORY on every risk-path diff (Gro/GAI can miss what the board catches).

## Also still open (pre-incident, lower priority than the slate)
- Options two-column page (board-designed 0DTE volatility/reversal column, NOT premium selling). Mockup:
  `logs/mockups/options_scanner_mockup_2026-07-06.html`. SPX-source blocker unresolved (Alpaca has no SPX).
- OPT-2 event-sourced replay (after M1, shipped `1952bef`). CLAUDE.md Open-Question loophole removal. RBLX phantom lot.
- UX total redesign of the 5 HTML pages (Rafael directive, queued behind critical bugs).

## Hard Invariants (unchanged)
- `paper=True` hardcoded in broker.py. SPY 5-min = sole entry gate. P&L from Alpaca fills only.
- Board + Gro + GAI on EVERY fork BEFORE Rafael; final Gro+GAI pre-ship on the exact diff.
- Risk-path safety: **a safety control must never mask a real loss** — keep the cold board on every risk-path diff.

## References
- Build map / slate: `logs/api_build_packages_2026-07-08.md`
- Audit log: `logs/tb_audit_log.md`
- Incident #1 forensics: `logs/incident_2026-07-07_alpaca_desync.json`; safe-mode spec: `logs/safe_mode_spec_2026-07-07.md`
