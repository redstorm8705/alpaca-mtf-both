# Session Summary — 2026-07-06 10:18 PM PT
**Project:** alpaca-mtf-bot
**Duration:** ~long (multi-topic; continued after a context compaction)
**Purpose of this doc:** cross-account handoff. Rafael is switching Claude accounts (weekly usage
limit). ANY account picking up must be able to continue the options/0DTE program from here without
re-deriving context. Read this + `handoff.md` + `logs/M1_decomp_spec.md`.

---

## HEADLINE STATE (verified this session)
- **git in sync:** local = `origin/main` = OCI HEAD = **`1952bef`**. All 4 services active on OCI.
- **Account:** paper equity **$2,798.53**. 9 open positions (see handoff.md).
- **Master Brain:** `project-state.md` re-uploaded this session (NotebookLM `0203f312-f285-4f20-8b8d-ca6fde65acf7`).
- Nothing in the options/0DTE program is shipped yet — it is a **design-stage program**, deliberately gated.

---

## THE MAIN WORK — Options page redesign + 0DTE strategy reframe (DESIGN STAGE, NOT SHIPPED)

### What Rafael decided (locked)
1. **0DTE is NOT premium-selling.** The existing `options_scanner.py` computes premium-selling
   (short-put/short-call) 0DTE recs — that framing is **WRONG and gets replaced**. 0DTE is meant to
   **capture intraday moves**: breakouts, mean-reversion, and other intraday signals that exploit
   **implied IV / delta / volatility** (i.e. BUY 0DTE options directionally / for vol, not sell premium).
2. **0DTE universe = 10 liquid names only:** MAG7 (AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA) + **SPY, SPX, QQQ**.
3. **Weekly directional = the full scan universe** (unchanged breadth).
4. **Sequencing = STRATEGY FIRST, then the exact page.** Rafael's rule: "the mockup/render must be
   EXACTLY what's pushed, no exceptions." The page therefore CANNOT be built until the 0DTE
   intraday-capture signals are defined — the 0DTE column's contents depend entirely on that new logic.
5. Per Feature Design Protocol, the 0DTE signal rework is a **strategy change** → needs a design +
   **board + Gro + GAI** gate BEFORE any code.

### The three-part program (in order)
- **Part 1 — 0DTE intraday-capture strategy design.** Define the signals: what triggers a breakout
  0DTE rec vs a mean-reversion 0DTE rec, which indicators (VWAP±SD bands, prior-day H/L, MACD, RSI,
  EMA), long-call vs long-put selection, strike/delta selection (~ATM 0.50Δ discussed), IV-rank gate,
  no-entry-after-1PM, hard 3:45 close. Feature Design + board (Sosnoff/Sinclair/Nathan options seat +
  Simons/Shaw signal seat) + Gro + GAI. **THIS IS THE NEXT ACTION for the program.**
- **Part 2 — SPX data source.** **BLOCKER / OPEN QUESTION.** Alpaca provides equity/ETF options
  (SPY, QQQ, MAG7) but **NOT SPX index options.** Rafael said "Keep SPX — I have/want an SPX source"
  but then DEFERRED the provider decision (was switching accounts). Options presented, still unresolved:
  (a) Rafael supplies provider + key (Polygon / Tradier / CBOE / ThetaData); (b) build SPX-less on the
  9 non-SPX names now and slot SPX in later; (c) use SPY as the SPX proxy (SPY≈SPX/10, deep-liquid 0DTE).
  **An account picking this up must get the SPX decision from Rafael before Part 1 can fully close.**
- **Part 3 — Two-column page + timers.** Build the approved layout into `options_scanner.py`:
  Weekly (full universe, green) | 0DTE (10 names, amber) as two 50/50 columns; VRP + Δ restored as
  columns; NO disclaimer/explainer paragraphs (Rafael: "there are SO MANY WORDS. You made it worse.");
  live clock + last-scanned + next-scan/entry-window countdown timers; mobile stack < 768px; use the
  Signal-badge COLOR as the buy/sell scent (NO separate BUY/SELL pill column — redundant in a 2-col split).

### The mockup (chat-only render — was NEVER deployed; that was the confusion this session)
- A `show_widget` render is **chat-only** (draws in chat, writes no file, touches no server). It was a
  visual PROPOSAL, not code. `options.html` at `http://129.153.208.32:8080/options.html` still serves
  the OLD separated page. **A widget ≠ a deploy** — flagged and owned.
- The exact mockup is saved as a standalone, self-contained HTML file so any account can view/continue it:
  **`logs/mockups/options_scanner_mockup_2026-07-06.html`** (opens in a browser; live PT clock ticks;
  two 50/50 columns; VRP/Δ columns; mobile stack). **NOTE: the mockup's right column still says "0DTE
  premium selling / Short put" — that is the PRE-REWORK placeholder. It is NOT the spec. Treat the 0DTE
  column as a stub to be redefined by Part 1.**

---

## Decisions Made
- 0DTE reframed premium-selling → intraday-capture (breakouts/mean-reversion on IV·delta·volatility). *Rafael.*
- 0DTE universe fixed at 10 names (MAG7 + SPY/SPX/QQQ); Weekly = full universe. *Rafael.*
- Sequencing: strategy-first, then pixel-exact page. *Rafael.*
- Keep SPX in-scope; source TBD (Rafael to provide, or SPY-proxy fallback). *Rafael, deferred.*
- Commit the loose files below so the record matches what's running (this wrap-up). *Rafael: "everything
  from this session needs to be logged in a place other accounts can reference and pick up the work."*

## Corrections (Claude was wrong / owned)
- Presented the `show_widget` options mockup in a way that implied it was live. It was chat-only, never
  built or deployed. Owned: "a widget ≠ a deploy." The live page is still the old version.
- Claimed the prior session had "pushed everything." TRUE for code + committed .md + Master Brain, but
  **3 files were left uncommitted** (below) — most importantly the M1 audit-log entry. Not fully true.

## Bugs Fixed
- None this session (design + handoff session). Prior-session code already live: FIFO repeat-run fix
  `654d507`, GEX weekly card `504bd8f`, M1 mechanical extract `1952bef`.

## Loose files committed in THIS wrap-up (were uncommitted, now durable)
| File | Was | Why it matters |
|------|-----|----------------|
| `logs/tb_audit_log.md` | modified, uncommitted | 59-line M1 extract audit entry — the CODE was live (`1952bef`) but its audit record was never committed (violated same-turn logging rule). Now committed. |
| `weekly_field_gate.py` | untracked | UX weekly-review field gate helper (62L), built earlier, never committed. Now tracked. |
| `logs/mockups/options_scanner_mockup_2026-07-06.html` | new | The exact options mockup, standalone, for cross-account reference. |
| `logs/session_summary_2026-07-06_2218.md` | new | This file. |
| `test_ui_tokens.py` | modified | Pre-existing test tweak (6+/3−); committed to clear the tree. |

## Open Items (carry forward — START-HERE for the next account)
- [ ] **OPTIONS/0DTE PROGRAM (Part 1)** — design the 0DTE intraday-capture signals via Feature Design +
      board (options + signal seats) + Gro + GAI. Blocked-adjacent by the SPX-source decision (Part 2).
- [ ] **SPX SOURCE DECISION (Part 2)** — get from Rafael: provider+key, OR SPX-less-now, OR SPY-proxy.
- [ ] **OPTIONS PAGE (Part 3)** — build two-column `options_scanner.py` to match the mockup EXACTLY,
      only after Part 1 defines the 0DTE column. Ship through Gro/GAI pre-ship gate.
- [ ] **M1 follow-on — OPT-2 event-sourced replay** — separate ship after M1 (already shipped `1952bef`);
      design against the mid-day-restart duplicate-lot hazard (2026-06-27 class). Spec: `logs/M1_decomp_spec.md`.
- [ ] **CLAUDE.md loophole removal** — §OPEN QUESTION PROTOCOL: strip "cannot resolve from first
      principles" so board+Gro+GAI is required on EVERY fork. Gated edit (needs Gro TPD reset/waiver).
- [ ] **Groq UA fix** — add browser `User-Agent` header to autonomous-pipeline urllib Groq callers
      (Cloudflare-1010 root cause; likely un-stalls the pipeline). Memory: `reference_groq_ua_cloudflare_block`.
- [ ] **RBLX phantom short-lot cleanup** — one-time removal from `open_lots_prior_day.json`.
- [ ] Autonomous pipeline stalled (50 directives failed_permanent; Groq TPM ceiling) — needs prompt-chunking/paid tier.

## User Preferences Observed / Reinforced
- **Cross-account durability:** everything from a session must land in git + Master Brain so a different
  account can pick up the exact work. Chat-only artifacts (widgets) are not acceptable as the record.
- **"Mockup = exactly what ships, no exceptions."** No divergence between render and deployed page.
- **Kill the words.** Dense, number-first UI; no explainer paragraphs/disclaimers. "SO MANY WORDS" = failure.
- **Execute, don't ask** which item to work on; but genuine external blockers (SPX key) are surfaced.
- **Board + Gro + GAI POV on every fork BEFORE it reaches Rafael** (Open Question Protocol; no exemptions).
