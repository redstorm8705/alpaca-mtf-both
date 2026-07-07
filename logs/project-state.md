# alpaca-mtf-bot — Project State (current, overwritten each session)
**As of 2026-07-06 10:18 PM PT**

## Bot
All 4 OCI services active, HEAD `1952bef` (local = GitHub = OCI, in sync). Paper equity **$2,798.53**.
9 open positions: AVGO long 1 · GOOGL long 1 (QHM) · HOOD long 2 · MARA long 16 · MS long 1 ·
MSTR short 1 · NVDA long 1 (QHM) · RIVN long 17 · SNOW long 1.
PDT abolished | Profile paper | MIN_SCORE=9/12 | KELLY_FRACTION=0.25.

## Active Program — Options page redesign + 0DTE strategy reframe (DESIGN STAGE, NOT SHIPPED)
Rafael reframed 0DTE: it is NOT premium-selling; it is intraday-capture (breakouts / mean-reversion
exploiting IV·delta·volatility — i.e. buying 0DTE directionally / for vol). 0DTE universe = 10 names
(MAG7 + SPY/SPX/QQQ); Weekly directional = full universe. Sequencing = STRATEGY FIRST, then a
pixel-exact page ("mockup = exactly what ships, no exceptions"). Three parts, in order:
1. **Design the 0DTE intraday-capture signals** (Feature Design + board + Gro + GAI). **NEXT ACTION.**
2. **SPX data source — BLOCKER:** Alpaca has no SPX index options. Decision pending from Rafael
   (provider+key / SPX-less-now / SPY-proxy).
3. **Build two-column `options_scanner.py`** to match the saved mockup exactly (Weekly | 0DTE, VRP+Δ,
   no words, live clock + last-scanned + timers, mobile stack). Only after Part 1.
Mockup (chat-only, standalone) saved: `logs/mockups/options_scanner_mockup_2026-07-06.html` — its 0DTE
column still shows the OLD "premium selling" placeholder; that is a stub, not the spec.

## Open Items (priority)
1. Options/0DTE Part 1 — 0DTE intraday-capture signal design (board + Gro + GAI).
2. SPX source decision (Part 2 blocker).
3. Options page build (Part 3) — after Part 1.
4. OPT-2 event-sourced replay (after M1, which shipped `1952bef`) — spec `logs/M1_decomp_spec.md`.
5. CLAUDE.md §OPEN QUESTION PROTOCOL loophole removal (gated edit).
6. Groq UA / Cloudflare-1010 fix (un-stalls autonomous pipeline) — memory `reference_groq_ua_cloudflare_block`.
7. RBLX phantom short-lot cleanup in `open_lots_prior_day.json`.

## Recent Ships (live on OCI)
- `1952bef` M1 mechanical extract → fifo_pnl.py + state_io.py (zero logic change).
- `654d507` portfolio_tracker repeat-run FIFO attribution fix (persist+accumulate day P&L).
- `504bd8f` GEX: compute on this-week expiry only + standalone dashboard card.

## Hard Invariants
- paper=True hardcoded in execution/broker.py — never change without full board vote.
- SPY 5-min bar-over-bar is the SOLE entry gate.
- All P&L from Alpaca fills API only — tracker math is cross-check only.
- T1 (Alpaca) for all equity/ETF data — yfinance only for ^VIX, ^VIX3M, JPY=X.
- Gro/GAI lean prompts (no leading conclusions); ship only after BOTH APPROVE the exact final diff.
- Board + Gro + GAI POV on EVERY fork BEFORE it reaches Rafael (Open Question Protocol, no exemptions).

## Way of Working
- Cross-account durability: every session's work lands in git + Master Brain; chat-only artifacts are
  not an acceptable record. Mockup = exactly what ships. Kill words (dense, number-first UI).
- Execute don't ask which item next; surface genuine external blockers (e.g. SPX key).
