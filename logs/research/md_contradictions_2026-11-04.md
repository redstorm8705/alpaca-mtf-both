# Contradictions in the governing .md files (2026-11-04, Claude signed)

Each item was checked against the code, the mechanical gates and the latest records at `origin/main` `8fcd50f`. Fixing
`CLAUDE.md` / `handoff.md` needs a GAI + reviewer #2 marker, so edits go through a Mac session.

## A. Settled by the sources — the .md text is stale
1. **Wrap-up.** `CLAUDE.md:421` "Run `/wrap-up` automatically" vs `CLAUDE.md:74-78` and `rules/OPERATING.md` G09
   "never auto-trigger". Current: never; offer once.
2. **Reviewer group.** Gro + GAI in `CLAUDE.md` (62 mentions incl. Step 4, Final Pre-Ship, Open Question Protocol,
   Interactive-vs-API "keep Gro + GAI as 2 of 3 voices"), `rules/CORE.md` S06/P04/P07, `rules/OPERATING.md` G01/G05/G10,
   `rules/behaviour.md` B02 vs REVIEWER-SWAP-1025 and `.claude/preship/preship_audit.py` (Codex as reviewer #2).
   Current: BGC (board, GAI, Codex); Gro only when Codex is unavailable.
3. **Gemini model.** `CLAUDE.md` Direct API Protocol pins `gemini-2.5-flash` vs `gai_client.py:30-36`, a 3.x model
   ladder (3.5-flash recorded as retired, line 27). Current: the ladder.
4. **How reviewers are called.** `CLAUDE.md` shows raw `curl` calls to Groq / Gemini vs
   `.claude/preship/owner_approval_gate.py:35,322`, which blocks direct calls. Current: only through
   `.claude/preship/bgg_call.py` or `preship_audit.py`.
5. **Approval wording.** `CLAUDE.md` CEO rule 4 and Step 7 ("Let's do it" or "Approved" = proceed) vs
   `.claude/preship/owner_approval_capture.py:40-44`, which records `APPROVE <ID>` (a bare "approved" only counts when
   exactly one design is pending). Current: name the ID.
6. **Invariant #2 "Keywords are display-only".** The catalyst gate has blocked new entries on keyword matches since
   2026-07-14 (`events/catalyst_engine.py:55-61`, owner go). Current: that narrow exception is live; the invariant text
   was never updated.
7. **Invariant #12** says the after-hours GTC stop in `strategy/run_cycle.py` mirrors the VIX widening. P0-5 (#399)
   removed it: `_vix_mult_gtc` appears 0 times in `strategy/run_cycle.py`. Current: removed.
8. **Invariant #6 kill switch.** Says "config.py paper profile L243"; it is at `config.py:320`. Since KILL-ATTR-1023
   (#753, `execution/risk_manager.py:871`) the 7% counts Day + Swing losses only; the invariant doesn't say so.
9. **Project Context.** "MIN_SCORE=9/12, INTRADAY_STOP_ATR_MULT=1.25, 12-point live / 16-point log-only, ~$2.5K" vs the
   paper profile the bot runs with (`--profile paper`, `handoff.md:1563`): MIN_LONG/SHORT_SCORE 8, stop 1.20
   (`config.py:321-328`). The Day / Swing / QHM / F6 / trend_night tiers are not described.
10. **main.py size.** "866 lines — Read tool, no Explore subagent needed" vs 1,201 lines today, which is over the
    1,000-line threshold where the Full Read Gate requires an Explore subagent.
11. **Bug-count tables** ("Live RC Counts", hotspot table) are dated 2026-06-15 and were never refreshed.
12. **Approved data sources** list Alpaca and FMP only. Shipped since: Finnhub earnings dates (#808), SEC EDGAR + Alpha
    Vantage transcripts (#836), Schwab quotes (#837/#838), each under an owner-approved design.
13. **handoff.md header** points new sessions at July designs as "active"; several old blocks are still titled LATEST.
14. **Shadow features.** The paper-account frame says each shadow goes live after its replay or is removed;
    `config.py:202` `DELTA_SCORING_ENABLED = False` and `config.py:536` `VOLUME_CONFIRMATION_ENABLED = False` ("False =
    shadow") are still there. The rule is current; the code clean-up is owed.

## B. Not settled by any source — needs the owner
1. **Regime up-size multiplier.** Rule E says `REGIME_LOW_VOL_SIZE_MULT` is pinned to 1.0 until staged through the board.
   `config.py:468` sets 1.25, and `strategy/run_cycle.py:1150,1827` multiplies it into core-MTF size
   (`execution/entry_logic.py:1371`, `int(raw_shares * size_multiplier)`). No staging record found in
   `logs/design_records/`, `logs/tb_audit_log.md` or `handoff.md`. This is risk-path (size): either the rule text is
   stale or the code drifted from it.
2. **Overnight automated sessions shipping code.** `CLAUDE.md` 5-hour chain: "Scheduled sessions NEVER apply patches" vs
   the 11-03 handoff block titled "owner CEO pre-approval: ship on full BGC alignment" (`handoff.md:42`). No written
   record of that pre-approval's scope was found.

## C. Could not verify from a cloud session
- Step 5c requires the code-review-graph MCP tools; there is no `.mcp.json` in the repo, so whether they exist on the Mac
  is unknown.
- The every-prompt reminder still says "board + Gro + GAI"; it is not in the repo's `.claude/`, so it lives in the Mac's
  user-level settings.
