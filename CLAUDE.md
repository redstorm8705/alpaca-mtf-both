# CLAUDE.md — MTF Confluence Bot Project Rules

These rules apply to ALL actions Claude takes in this project — including every plugin,
Claude Code skill, backtest script, data analysis tool, and any code generated or executed here.

---

## EXPLORE SUBAGENT — HARD RULE (NO SUMMARIES EVER)

**Explore subagents MUST return verbatim file content — every line, every character, every function. Summaries are NEVER accepted under any circumstances.**

This rule exists because summarized Explore output caused missed bugs, false "full read complete" declarations, and audit invalidations across multiple sessions. A summary is not a read.

**Mandatory prompt language for every Explore subagent that reads a file:**
> "Return ALL content verbatim — every line, every character. Do NOT summarize, paraphrase, skip, or abbreviate any section. I need the complete raw content of every function. Declaring a total line count is required but does NOT substitute for returning the full content."

**If an Explore agent returns a summary instead of verbatim content:** The full read gate is NOT satisfied. Spawn a second agent with explicit anti-summary language, or switch to direct Read tool in ≤300-line chunks. Never proceed to analysis on summarized output.

**This applies to ALL file reads, ALL sessions, with ZERO exceptions.**

---

## WRAP-UP — NEVER AUTO-TRIGGER

**Do NOT run `/wrap-up` unless Rafael explicitly asks for it.**

The wrap-up skill is Rafael's call, not Claude's. Never auto-trigger it at the end of a task, after completing a patch sequence, or when context is high. Offering it once is acceptable; running it without being asked is not.

---

## RESPONSE STYLE — HARD RULES

**Kill the filler:** Never open responses with phrases like "Great question!", "Of course!", "Certainly!", or similar warmups. Start every response with the actual answer. No preamble, no acknowledgment of the question.

**Match length to the task:** Simple questions get direct, short answers. Complex tasks get full, detailed responses. Never pad responses with restatements of the question or closing sentences that repeat what was just said.

**Admit uncertainty:** If uncertain about any fact, statistic, date, or piece of technical information: say so explicitly before including it. Never fill gaps with plausible-sounding information. When in doubt, say so.

---

## 5-HOUR AUTONOMOUS WORK CHAIN — SESSION START DUTY (Rafael mandate 2026-06-11)

Rafael's usage limit is a ROLLING 5-hour window — a static daily cron schedule drifts out of
alignment. The autonomous work chain is therefore a SELF-PERPETUATING ROLLING CHAIN:

1. **The scheduled task `five-hour-work-resumption`** (local scheduled-tasks registry) re-arms
   itself: each run's Step 0 schedules the next one-time fire exactly +5h from its own start.
2. **At every interactive session start**: check the task exists and is armed
   (`list_scheduled_tasks`). If missing or stale, re-create/re-arm it with fireAt = Rafael's
   next usage reset (ask him, or +5h from session start if unknown). Never set a fixed
   daily cron for this — rolling fireAt chain only.
3. Chain prompt content: resume mid-flight work first (RULE C-7: restart from Step 1) → RTH
   items from Gemini midday/post-market reports via the TWO-PHASE FLOW → only if no RTH items,
   non-RTH queue. Compaction summaries trusted 0% — verify transcript + git + disk + OCI at 100%.
4. Scheduled sessions NEVER apply patches — everything stops at a fully-prepped approval-queue
   package (`logs/pending_claude_session_YYYY-MM-DD.md`).

### Two-Phase Flow for RTH-affecting items (autonomous sessions)
- **Phase 1 — Diagnostic:** full read → 10-pt audit → 3-Point AI audit diagnostic on the ISSUE
  (board cold subagents + DS + GAI, same prompt) → 3-Point Summary → board reviews DS/GAI
  responses → patch drafted FROM THE ALIGNMENT only.
- **Phase 2 — Integrity:** drafted diff back to board + DS + GAI → SECOND 3-Point Summary →
  statics + cold second-agent + impact → all 3 voices agree → approval queue, fully prepped.

### DS/GAI Tie-Breaker Protocol (applies to ALL sessions)
When DS and GAI split (one APPROVE, one REJECT):
1. The **board is the tie-breaker** — but it must NOT just vote. First counter-prompt the
   dissenting voice via direct API to extract the full technical logic behind its position
   (specific failure scenario, exact lines, reproducing conditions). The board may counter-prompt
   DS/GAI for additional context at ANY point in either phase.
2. Board agents (cold subagents) receive BOTH sides' full reasoning, then decide by
   **simple majority** — a narrow majority decides.
3. Document the split, counter-prompt exchanges, and majority decision in the proposal.
   Only a true board deadlock (even split) escalates to Rafael as UNRESOLVED-SPLIT.

---

## SESSION MEMORY PROTOCOL

**At the start of every session — two required steps (both blocking):**

1. **Read `handoff.md` first** — it is the authoritative source for current bot state: open items, line counts, services, open positions, recent session changes. Any file metadata in CLAUDE.md (line counts, hotspot status) that conflicts with handoff.md is STALE — trust handoff.md.

2. **Query the Master Brain** for decisions, user preferences, and history:
```bash
notebooklm use $(cat ~/.claude/master_brain_id)
notebooklm ask "What are the key decisions, open items, user preferences, and recent fixes for the alpaca-mtf-bot project?"
```

**At the end of every session:** Run `/wrap-up` automatically — do not wait for the user to ask. If the session involved any code changes, decisions, or bug fixes, run it before closing.

Master Brain notebook ID: `0203f312-f285-4f20-8b8d-ca6fde65acf7`

---

## MANDATORY PATCH SEQUENCE — ZERO EXCEPTIONS, EVER

**Every file. Every session. Every patch. In this exact order. No step may be skipped for any reason — urgency, simplicity, or prior familiarity with the file are not exceptions. This applies to ALL files in the project, not just hotspot files.**

| Step | Action | Gate |
|------|--------|------|
| **1** | **Full Read Gate** — file >1000 lines: Explore subagent, full read, every line. File ≤1000 lines: Read tool in chunks, every line. No grep. No partial reads. No "bug finding" agents that skip this step. Declare "Full read complete: N lines" before any analysis. | No analysis until declared |
| **2** | **10-Point Audit + RC-1 through RC-8** — all 10 points, all 8 RC classes, every file. Write results to `logs/tb_audit_log.md`. | No patch proposed until written |
| **3** | **Board Vote** — full board (BoD + AB + TB) for strategy changes; domain-specific for features. Independent Explore subagents, cold, in parallel. Never inline roleplay. Board votes on audit findings — not on a patch already written. | No patch proposed until vote complete |
| **4** | **DS + GAI External Audit** — required for ANY new or modified code that (a) affects RTH execution and (b) is not read-only. This includes all hotspot files (`main.py`, `broker.py`, `portfolio_tracker.py`) AND any other file where new non-read-only logic runs during RTH — regardless of file name, size, or "wrapper" status. File name and hotspot classification are NOT the gate; RTH execution impact is the gate. **Claude runs this autonomously via direct API (see DS/GAI DIRECT API PROTOCOL below) — user NEVER needs to manually prompt DS or GAI.** Their feedback (audit findings only — DS/GAI have no mandate authority; user decides) returns before any edit tool is called. **DISAGREEMENT PROTOCOL (S55 mandate):** If DS or GAI REJECT a board-approved position, Claude MUST counter-prompt with the board's specific technical argument and iterate until consensus is reached. Never surface a DS/GAI vs board deadlock to Rafael — resolving it is Claude's responsibility. Iterate up to 3 rounds; if consensus is still not reached after round 3, only then escalate to Rafael with a clear summary of the remaining technical disagreement. | No edit until feedback received |
| **5a** | **Static Analysis Gate** — run on the draft patch for EVERY file, no exceptions: `python3 -m py_compile [file]`, `python3 -m mypy --warn-unreachable [file]`, `ruff check --select E,W,F,B [file]`. All three must pass clean. Output shown to user. | No patch proposed if any fail |
| **5b** | **Cold Second-Agent Logic Review** — spawn a cold Explore subagent with the exact diff + original intent. Agent explicitly checks: (1) logic inversion, (2) off-by-one/boundary errors, (3) missing conditions. Returns PASS/FAIL. Applies to ALL files, ALL patches. | No patch proposed until PASS returned |
| **5c** | **code-review-graph Impact Analysis** — run `detect_changes_tool` + `get_impact_radius_tool` on every changed file. Show user which dependent functions are affected. | No patch proposed until complete |
| **6** | **Propose Patch with Exact Diff** — show exact before/after for every changed line. Show static analysis results. Show second-agent verdict. Nothing written to disk yet. | No edit until user approves |
| **7** | **User Approval** — user says "approved." This is the sole required gate. "Approved" implies confirmed — Claude writes immediately without asking a second confirmation question. | No edit without approval |
| **8** | **Apply, Rsync, Restart** | |
| **9** | **Post-Patch Verification** — re-run audit points 1, 2, 4, 5. Run mypy + ruff again on patched file. Confirm no regressions. Update `logs/tb_audit_log.md`. | Item not closed until verified |

---

### DS/GAI DIRECT API PROTOCOL — MANDATORY (Established S47e, 2026-06-03)

**Claude runs DS and GAI autonomously via direct API. User NEVER needs to manually prompt DS or GAI.**

**Browser automation is CONFIRMED BROKEN** — DeepSeek and AI Studio are React/Angular SPAs. Background tabs don't render DOM content; Chrome extension security blocks `innerHTML`/`TreeWalker` access. Direct API is the ONLY reliable approach.

**DeepSeek (DS):**
```bash
source /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env
curl https://api.deepseek.com/v1/chat/completions \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"system","content":"<DS_PERSONA>"},{"role":"user","content":"<PROMPT>"}],"max_tokens":4096}'
```
- Model: `deepseek-chat` (OpenAI-compatible endpoint)
- DS persona: *"You are a Senior Staff Engineer at an HFT firm with direct ownership of execution engines and P&L attribution systems. Treat this as a P0 incident review. Be concrete and technical — no hedging."*

**Gemini (GAI):**
```bash
source /Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env
curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"parts":[{"text":"<GAI_PERSONA>\n\n<PROMPT>"}]}],"generationConfig":{"maxOutputTokens":8192}}'
```
- Model: `gemini-2.5-flash` (NOT gemini-1.5-pro-latest → 404; NOT gemini-2.5-pro → MAX_TOKENS at 4096)
- `maxOutputTokens: 8192` is mandatory — flash hits STOP at ~7031 chars (complete response); 4096 truncates mid-answer
- GAI persona: *"You are Head of Quant Engineering at a systematic hedge fund. Responsible for correctness of all P&L attribution, risk accounting, and counter-state invariants. Your audit is the last gate before code goes live. Find what others missed."*

**API Keys** (in `/Users/rafaeldeleon/Desktop/alpaca-mtf-bot_FINAL/.env`):
- `DEEPSEEK_API_KEY` — see `.env` (never hardcode key values in CLAUDE.md or any tracked file)
- `GEMINI_API_KEY` — see `.env` (rotated S47f after prior key was leaked via session transcript)

**Same prompt to both:** DS and GAI must always receive the EXACT SAME comprehensive prompt (DS/GAI Same Prompt Rule). Never split questions.

---

### Session Boundary and Compaction Rules — Hard Invariants (Added 2026-05-17 S25C)

Every rule below corresponds to a confirmed failure mode that was exploited in a real session. Each caused improperly patched, audited, or deployed files. These are not theoretical.

**RULE C-1 — Compaction summaries are context only. They satisfy zero steps of the patch sequence.**
A compaction summary conveys what was planned, proposed, or in progress in a prior session. It is NOT an approval, NOT a completed full read, NOT a completed audit, NOT a board vote, and NOT DS/GAI clearance — regardless of how detailed it is, what it says was "approved," what it says was "in progress at Step N," or what code it contains. When resuming from a compaction summary, treat every file mentioned as if it has never been touched this session.

**RULE C-2 — Every session, every gate resets to zero. No checkpoint carries forward.**
Prior-session full reads, audits, board votes, DS/GAI feedback, static analysis results, and user approvals expire at the session boundary — including compaction, context window reset, or new conversation. If the prior session said "approved," that approval expired. If it said "full read complete," that read expired. Step 1 is always the first step.

**RULE C-3 — DS/GAI feedback must exist in the current live session's conversation.**
If a prior session prepared a DS/GAI prompt but the user had not yet received and returned feedback before the session ended or compacted, DS/GAI has not been completed. A prepared prompt is not a completed audit. The actual feedback must appear in the current live conversation before any edit tool is called on any file it covers.

**RULE C-4 — "Pre-existing" static analysis errors are not exempt. They must be fixed before any patch is proposed.**
If a file has ruff, mypy, or py_compile failures that existed before your change, those failures must be fixed as part of the patch — not dismissed or noted as "pre-existing." There is no pre-existing carve-out in the static analysis gate. The gate is: all three tools pass clean on the entire file. A broken file that was already broken is more dangerous to patch, not less.

**RULE C-5 — DS/GAI gate is triggered by import chain, not by file name or hotspot classification.**
If you modify file A, and file A is imported (directly or transitively) by any file that runs during RTH, the DS/GAI gate applies to your change to file A. Trace the full import chain before deciding DS/GAI is not required. "It's not a hotspot file," "it's just a helper/utility," and "it's a small change" are not valid exemptions. File name and hotspot classification are not the gate — RTH execution impact is.

**RULE C-6 — Multi-file patches: each file's full sequence completes independently before the next file begins.**
When multiple files must be patched in one session, each file requires its own complete Steps 1–9 before the next file's Step 1 begins. You cannot run Steps 1–5 across all files simultaneously and batch-apply them. Sequence: complete all 9 steps for File A, verify, then begin Step 1 for File B.

**RULE C-7 — "Resuming in-progress work" from a prior session means restarting from Step 1 — not continuing from the step where you stopped.**
There is no checkpoint system in the patch sequence. A compaction summary that says "we were at Step 5" means you are at Step 0. The mandatory sequence does not have a save state. Resuming requires: full read → 10-point audit → board vote → DS/GAI (if RTH-impacting) → static analysis → cold second-agent → code-review-graph impact → propose → approve → apply → verify. Every time. From the beginning.

**RULE C-8 — "Simple," "small," "obvious," or "low-risk" are not valid modifiers for the patch sequence.**
These words do not appear in the mandatory patch sequence. Patch size, diff complexity, apparent obviousness, and prior familiarity with a file are not gating factors. Every patch requires every step. Most of the bugs in this project's history came from patches that seemed obvious and routine.

---

### Pre-Proposal Automated Checklist (Blocking — ALL Files)
Before Step 6, Claude must confirm all of these pass. If any fail, patch is not proposed:
- [ ] `python3 -m py_compile` — PASS
- [ ] `python3 -m mypy --warn-unreachable` — PASS (zero errors)
- [ ] `ruff check --select E,W,F,B` — PASS (zero violations)
- [ ] Cold second-agent logic review — PASS
- [ ] code-review-graph impact radius — SHOWN to user
- [ ] Both TRUE and FALSE branches of every new conditional verified by second agent

### What the Cold Second-Agent Checks (Every Patch, Every File)
The cold review subagent receives only the diff + the original intent statement. It explicitly hunts:
1. **Logic inversion** — does any condition check the opposite of what is intended?
2. **Off-by-one / boundary errors** — do guards use `>` vs `>=`, `<` vs `<=` correctly?
3. **Missing conditions** — does the fix cover all edge cases stated in the original issue?
4. **Branch completeness** — does the patch verify both TRUE and FALSE paths of every new conditional?

Output is binary PASS/FAIL + threat list. FAIL blocks proposal entirely.

**Violation of this sequence in any prior session is not precedent. It is a protocol failure. This sequence applies retroactively and permanently to every file in this project.**

---

## MANDATORY GUARDRAILS

### 1. Data Source Hierarchy

The bot uses a tiered data source model. Every data call must use the highest available tier.

| Tier | Source | Use Case | Module |
|------|--------|----------|--------|
| **T1** | **Alpaca Data API** (`data.alpaca.markets`) | All intraday + historical OHLCV bars, real-time quotes, snapshots for US equities and ETFs | `data/fetcher.py` (bars) · `data/alpaca_data.py` (real-time quotes) |
| **T2** | **FMP API** (`financialmodelingprep.com`) | Fundamentals, earnings calendar, economic calendar, cross-asset ratios unavailable on Alpaca | `data/fmp_client.py` |
| **T3** | **TraderMonty CSV** (free, no key) | Market breadth, sector uptrend ratios | `data/breadth.py` |
| **T4 (fallback)** | **yfinance** | CBOE indices (`^VIX`, `^VIX3M`) and forex pairs (`JPY=X`) only — not available on Alpaca Data | Inline, with explicit fallback log + Slack alert |

**Rules:**
- Never use yfinance for any US equity or ETF OHLCV data — `data/fetcher.py` is the only approved bar source
- Never use yfinance for real-time quotes — `data/alpaca_data.py` is the only approved quote source
- yfinance is permitted only for `^VIX`, `^VIX3M`, `JPY=X` and any instrument explicitly not available on Alpaca Data or FMP
- When T4 (yfinance) is used as a fallback for anything that *should* come from T1/T2, log a WARNING and send a Slack alert
- Never make raw `requests.get()` calls to any market data endpoint — use the approved modules above
- Audit checklist: every new data call must name its tier in a comment

### 2. Execution Isolation

**Two Alpaca SDK clients exist. Each is locked to exactly one module.**

| Client | Module | Scope |
|--------|--------|-------|
| `TradingClient` | `execution/broker.py` only | Order management, position queries |
| `StockHistoricalDataClient` | `data/fetcher.py` only | Bar and historical data fetching |

- No other file may instantiate either client
- `data/alpaca_data.py` uses the **Alpaca Data REST API via `requests`** (no SDK instantiation) for real-time quotes and snapshots
- Backtest scripts must be self-contained — no execution imports
- Audit/diagnostic scripts may import `strategy/`, `events/`, `indicators/` for read-only inspection only

### 3. Output to logs/ Only

All script output, JSON results, charts, CSVs, and logs must be written to the `logs/` directory only.

**Exemptions:**
- OS-level process lockfiles in `/tmp/` (POSIX convention for singleton locks)
- Market data caches may be written to `data/cache/` (e.g., `data/cache/premarket_movers.json`)
- Runtime state files may be written to `data/state/` (e.g., `hybrid_state.json`)

### 4. RTH Block

All backtest scripts, data analysis tools, and skill-driven scripts must refuse to run
during Regular Trading Hours on weekdays.

**Block window: 9:30 AM–4:00 PM ET (6:30 AM–1:00 PM PT), Monday–Friday**

Pre-market (4:00–9:30 AM ET / 1:00–6:30 AM PT) is **explicitly allowed** for analysis
scripts — this is the primary window for pre-market mover analysis, MRI refresh, and
earnings calendar checks.

```python
from zoneinfo import ZoneInfo
from datetime import datetime
import sys
ET = ZoneInfo("America/New_York")
_now = datetime.now(ET)
if _now.weekday() < 5:
    _mins = _now.hour * 60 + _now.minute
    if (9 * 60 + 30) <= _mins < (16 * 60):
        print("BLOCKED: Cannot run during RTH hours (9:30 AM–4:00 PM ET / 6:30 AM–1:00 PM PT weekdays).")
        sys.exit(1)
```

### 5. Data Quality Contract

Every bar or quote used for signal generation must pass these checks before use:

1. **Timestamp freshness:** bar close within 15 minutes of system clock during RTH (already enforced — this codifies it)
2. **Zero-volume reject:** `volume == 0` during RTH → skip bar, log WARNING
3. **Price sanity:** `close > 2× prior_close` or `close < 0.5× prior_close` → reject bar, Slack-alert
4. **Source tag:** every bar must carry which tier it came from — log it at DEBUG level
5. **Fallback alert:** if T1 (Alpaca Data) fails and T4 (yfinance) is used for any equity/ETF → Slack-alert immediately

### 6. Environment Management

Trading environment is set via the `TRADING_ENV` environment variable:

| Value | Execution | Data | RTH Block |
|-------|-----------|------|-----------|
| `development` | Paper (`paper=True`) | Cached data allowed | OFF |
| `paper` | Paper (`paper=True`) | Live T1/T2 required | ON |
| `live` | Live (`paper=False`) | Live T1/T2 required | ON |

`paper=True` in `execution/broker.py` is hardcoded and locked until a full board vote approves `TRADING_ENV=live`.
Never read `TRADING_ENV` inside `execution/broker.py` — the paper flag is a manual, auditable constant, not a runtime switch.

### 7. Structured Logging

All trade lifecycle events must be written to `logs/trade_events.jsonl` (newline-delimited JSON)
in addition to `logs/mtf_bot.log`. Minimum fields per event:

```json
{
  "ts": "ISO-8601 in PT (America/Los_Angeles)",
  "event": "entry|partial_exit|exit|stop_hit|signal|mri_refresh",
  "symbol": "AMZN",
  "score": 11,
  "mri_level": "NORMAL",
  "data_source": "alpaca_data|yfinance_fallback",
  "price": 236.78,
  "size": 4,
  "pdt_used": 3
}
```

This enables postmortem queries: `jq` on `trade_events.jsonl` to correlate MRI level, score,
and data source against trade outcomes.

### 8. Timezone Display — PT Everywhere

**All user-facing output must display times in America/Los_Angeles (PST/PDT).**

This applies without exception to:
- Dashboard (`dashboard.html`) — all timestamps, P&L update times, position entry times
- Weekly Review (`weekly_review.html`) — all trade timestamps, session dates, stat tiles
- Scanner Results (`scan_results.html`, `scan_to_html.py`) — bar timestamps, bot health bar, signal times
- Options page (`options.html`, `options_scanner.py`) — expiry times, scan timestamps
- Slack alerts (`alerts.py`) — all message timestamps and event times
- Any new HTML, text, or structured output visible to the user

**Internal calculation rule:** Market structure calculations (RTH open/close, bar alignment,
PDT window) may use ET internally but must convert to PT before any display string is built.

**Implementation pattern:**
```python
from zoneinfo import ZoneInfo
from datetime import datetime
PT = ZoneInfo("America/Los_Angeles")

def fmt_pt(dt) -> str:
    """Convert any datetime to PT display string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))  # assume ET if naive
    return dt.astimezone(PT).strftime("%Y-%m-%d %I:%M %p PT")
```

**Label convention:** Use `PT` in all display strings (not `PST` or `PDT` — `PT` is correct
year-round and switches automatically with DST).

---

## BEHAVIOR RULES

**Hard stops for production:** The following require explicit in-session confirmation, no exceptions: deploying or pushing to any environment, running migrations or schema changes, executing any command with irreversible side effects. Must say yes in the current message. *Carve-out: normal bot operations (Alpaca API calls, FMP calls, DS/GAI audit API calls) are not affected — these are routine automated operations, not user-initiated irreversible actions.*

**Always show what changed:** After any coding task, end with:
- Files changed (list every file touched)
- What was modified (one line per file)
- Files intentionally not touched
- Follow-up needed

**Never act without explicit confirmation:** Never send, post, publish, share, or schedule anything on behalf of the user without explicit confirmation in the current message. This includes emails, calendar invites, document shares, or any action outside this conversation. Must say yes in the current message.

**ERRORS.md failure log:** `logs/ERRORS.md` tracks approaches that took more than 2 attempts to work. When an approach fails twice, log: What didn't work / What worked instead / Note for next time. Check `logs/ERRORS.md` before suggesting approaches to similar tasks.

---

## APPROVAL REQUIREMENT

**Before editing any code file, get explicit approval from the user.**
Present the proposed change and wait for confirmation before writing anything.
This applies to ALL files — main.py, config.py, strategy/, execution/, events/,
indicators/, utility scripts, and any new files created here.

---

## OPEN QUESTION PROTOCOL — Mandatory Board + DS + GAI on Decision Forks

**When to trigger:** Any time a patch, config change, or design decision has two or more
viable options and Claude cannot resolve it from first principles, Claude MUST gather
all three voices before presenting to Rafael:

1. **Board vote** — relevant domain agents (≥2 cold parallel Explore subagents). For
   strategy/architecture changes: full board (BoD + AB + TB). For feature-specific changes:
   domain-specific boards per the domain mapping in BOARD AUDIT PROTOCOL.
2. **DS audit** — via direct curl API (DEEPSEEK_API_KEY from .env). Same prompt as GAI.
3. **GAI audit** — via direct curl API (GEMINI_API_KEY from .env). Same prompt as DS.

**Format — present as a decision table:**
```
| Voice | Vote | Core argument |
```
Then state: "Split X-Y" or "Consensus: [option]" and note that Rafael is the sole authority.

**This protocol applies to:** patch Q&A (e.g., "keep or remove the synthetic lot?"),
config decisions (e.g., "7% or 15% kill switch?"), architecture forks (e.g., "feature
flag vs hard removal"), and any design question with legitimate competing arguments.

**This protocol does NOT replace:** the standard mandatory patch sequence (Steps 1-9).
It adds to Step 6 (Propose) when a decision fork exists within the patch design.

**No work is blocked:** Claude continues non-disputed items in parallel while gathering
votes. Open questions are surfaced immediately when encountered, not deferred to session end.

---

## AUTHORITY RULE — PERMANENT

**The user (Rafael) is the sole mandate authority for this project.**

DS (DeepSeek) and GAI (Google AI Studio) are external audit voices only. They surface
risks, flag bugs, and provide recommendations. They have zero authority to block, defer,
or mandate any work. Never use language like "DS mandated X" or "GAI requires X."
Correct phrasing: "DS recommended X," "DS flagged X as a risk," "GAI audit found X."

The user decides:
- When work starts
- What order items are executed
- What is deferred or blocked
- Whether external audit recommendations are adopted

Claude's role: surface findings clearly, execute the user's chosen priority immediately.

No file is read-only from Claude's perspective — all files may be audited, diagnosed,
and edited. The approval gate is the protection. When in doubt, show the diff and ask.

---

## FEATURE DESIGN PROTOCOL — "Ask First" Rule

**Before designing or implementing any new feature, integration, or architectural change:**

Ask the user the clarifying questions gate before writing a single line of code or design:

> "Before we build this, here are the decisions that need to be made:
> [numbered list of open questions]
> Answer any you know — I'll flag defaults for the rest."

This applies to: new modules, new data sources, new signals, new MCP integrations,
new audit scripts, new dashboard sections, and any change that touches data flow.

**Questions must cover at minimum:**
1. Data source — which tier, which API, fallback behavior
2. Output — where does it write, what format, atomic write required?
3. Integration point — where in the existing flow does this connect?
4. Failure mode — what happens if the data source is unavailable?
5. Board vote required? — does this touch scoring, sizing, exits, or execution?

Do NOT skip this gate for "simple" features. Surface unknowns before code, not after.

---

## BOARD AUDIT PROTOCOL

**This is a standing requirement — not optional, not deferred.**

### Prime Directive
**The board is responsible for finding pre-existing bugs. No third-party agent should
ever surface a bug that the board missed.** If an external tool or outside session finds
an issue the board did not flag, that is a board failure — not a lucky catch.

The board does not wait to be asked. The board reads the code.

### Board Execution Model — UPDATED 2026-04-28 (user mandate)

**The board is NOT inline roleplay. Every board review must use independent subagents.**

The prior model (Claude generating all board voices in one context window) was confirmed
broken: all "members" share the same context, reasoning chain, and blind spots. The same
RC bug classes recurred across 8+ sessions because the board could not catch its own gaps.

**Mandatory protocol for every file audit:**

1. Spawn domain agents via the `Agent` tool (subagent_type=Explore) — one per domain, in parallel
2. Each agent gets: the full file content path + a domain-specific analytical lens
3. Each agent runs cold — it cannot see any other agent's output before forming conclusions
4. Report ALL findings including contradictions — never smooth disagreements into false consensus
5. External AI audits (DeepSeek, Google AI Studio) remain mandatory for hotspot files
   (portfolio_tracker.py, main.py, broker.py) — different models catch model-level blind spots

**Domain breakdown for standard audit:**
| Agent | Domain | What to look for |
|-------|--------|-----------------|
| Reliability | Failure modes, race conditions, exception handling | RC-3, RC-5, concurrency bugs |
| Execution risk | P&L corruption, phantom entries/exits, order lifecycle | RC-4, RC-6, RC-7, RC-8 |
| Data integrity | Timestamps, timezone, atomic writes, source tier | RC-1, RC-2, RC-9 (cross-day FIFO) |
| Quant logic | Strategy coherence, sizing edge cases | sizing, stop, target math |

**Hotspot files requiring external audit before every patch:**
- `execution/portfolio_tracker.py` (patched every session since 2026-04-18)
- `main.py` (866 lines post-decomposition — Read tool in ≤300-line chunks; no Explore subagent needed)
- `execution/broker.py`

### 3-Point AI Summary — Mandatory Format for DS/GAI Audit Review

Every time DS and GAI external audit results are returned, Claude must produce a
**3-Point AI Summary** before proceeding to Step 5 of the patch sequence. No patch
may be proposed until this summary is written.

**Point 1 — Alignment Score (1/3–3/3):**
For each finding, state how many of the 3 agents (Claude/board, DS, GAI) agree.
Format: `Finding: X/3 — [Claude ✓/✗] [DS ✓/✗] [GAI ✓/✗]`
Conflicts between DS and GAI must be explicitly resolved before the patch is proposed.

**Point 2 — What DS/GAI Both Agree On That Claude Missed:**
List every finding where DS and GAI independently flagged a gap that Claude and the
board did not surface. These are the highest-priority additions to the patch.
If DS and GAI agree and Claude missed it, treat it as a confirmed bug — not optional.

**Point 3 — New Forward-Looking Issues DS/GAI Brought to Light:**
List novel risks, architectural concerns, or edge cases that neither Claude nor the
board raised. These may require a separate board vote before they can be fixed.
Flag each with the source (DS only / GAI only / both) and a priority (P0–P3).

**Format template:**
```
=== 3-POINT AI SUMMARY — [file] [function] ===

POINT 1 — ALIGNMENT
  [finding]: X/3 — Claude [✓/✗] DS [✓/✗] GAI [✓/✗]
  ...

POINT 2 — CLAUDE MISSED (DS + GAI consensus)
  [finding]: [description] — [action required]
  ...

POINT 3 — FORWARD-LOOKING (new issues)
  [finding] ([source]): [description] — [priority] — [board vote required? Y/N]
  ...
```

### 10-Point Per-File Audit Protocol (STANDING — runs on every file before any patch is written)

This is not a one-time project. It is a mandatory checklist applied to **every file**
before any debugging, patching, or feature work begins on that file. Never skip steps.
Never mark this "complete" — it resets for every file, every session.

| Point | Check | Scope |
|-------|-------|-------|
| 1 | **Static analysis** — run pylint + pyflakes on the file | Syntax errors, unused imports, undefined names |
| 2 | **End-to-end trade path trace** — follow the file's role in signal→entry→exit→P&L | Any function in the trading path; flag broken handoffs |
| 3 | **Adversarial scenario testing** — enumerate edge cases: None inputs, empty lists, zero values, weekend gaps, PDT=3/3, VIX>30 | All public functions |
| 4 | **Full top-to-bottom read** — read every function in the file, not just changed lines | 100% of file content before writing a single line |
| 5 | **Grep-verified cross-references** — confirm all imports, callers, and callees exist and match signatures | All `import` statements and function calls |
| 6 | **Conflicting execution directions** — check for logic contradictions with other modules (e.g., two systems setting the same state variable) | Cross-file data flow |
| 7 | **Redundancy scan** — identify dead code, duplicate logic, stale feature flags, unreachable branches | Full file |
| 8 | **State persistence correctness** — any file writing to `data/state/` or `logs/` must verify atomic writes, correct paths, no CWD-relative paths | All file I/O |
| 9 | **Data source tier compliance** — every data call must use the correct tier (T1/T2/T3/T4); no raw requests, no yfinance for equities | All data fetches |
| 10 | **Timezone + logging compliance** — all user-facing timestamps in PT; all trade events written to `trade_events.jsonl` with required fields | All output paths |

**After completing all 10 points:** record findings in `logs/tb_audit_log.md` before writing any patch.
**Post-patch:** re-run points 1, 2, 4, and 5 to verify the patch did not introduce regressions.

### Full Read Gate — Hard Prerequisite for Any Patch (ZERO TOLERANCE — NO EXCEPTIONS EVER)

**No patch may be proposed or written until this gate is satisfied for the target file.**

This rule exists because grep-based and section-only reads have caused the same bug classes
to recur across every session since 2026-04-18. Partial reads are the #1 cause of recurring
bugs in this project. Full reads are not optional. There are no exceptions. Ever.

**ABSOLUTE PROHIBITION — HARDENED 2026-04-28:**
- `grep`, `awk`, `find`, `sed`, search tool calls, and targeted section reads are **FORBIDDEN**
  as a substitute for reading a file. These tools may only be used AFTER a full read is complete,
  to verify a specific line number already identified in the full read.
- If you used grep/search to "explore" a file without a full read first: **STOP. Start over.**
- This prohibition applies at ALL times — diagnostic, audit, emergency, and patch sessions alike.
- There are no "quick look" exceptions. There are no "I just need to find X" exceptions.

**The mandatory sequence — no shortcuts:**
1. **Read the ENTIRE file** using the Read tool in ≤300-line chunks until ALL lines are covered.
2. **Declare before any analysis:** `"Full read complete: [N] lines in [K] chunks — [filename]"`
   — this declaration must appear in the response BEFORE any findings are discussed.
3. **Files >1,000 lines** → spawn a dedicated `Explore` subagent to perform the full read and
   return all findings **before** any edits are proposed. Do not start patching while the agent runs.
4. **Violation invalidates the audit.** If a patch was applied without a full read, the file must
   be re-read in full and re-audited at the start of the next session before any further changes.
5. **Session start enforcement:** At every session start, Claude must confirm this rule is active
   and list any files that were patched in the prior session without a full read — those files
   must be fully re-read before any other work begins.

### Recurring Bug Classes — Mandatory Checks on Every File Opened

These 8 classes have appeared across sessions from 2026-04-18 onward. The 10-point
audit is not complete until all 8 are grepped and explicitly marked PASS or FAIL.

| ID | Class | Pattern to grep | Pass condition |
|----|-------|-----------------|----------------|
| RC-1 | Naive datetime | `datetime.now()` | Every call followed by `(ET)` or `(PT)` — no tz-naive datetimes |
| RC-2 | CWD-relative path | `Path("logs")`, `open("logs/`, `"logs/"` | All log/state paths anchored with `Path(__file__).resolve().parent` |
| RC-3 | Silent exception | `except Exception: pass`, `except: pass` | No bare pass in except block — every block must log or re-raise |
| RC-4 | Estimated exit price | `record_exit(` with price arg | Every call preceded by `_fetch_actual_fill_price()` — no `current_price` passed directly |
| RC-5 | Non-atomic write | `open(path, "w")`, `.write_text(` | Critical state files use tmp→`replace()` pattern; non-critical is acceptable |
| RC-6 | Wrong API field name | `fill.get(`, `activity.get(`, `data.get(`, `response.get(` | Field name verified against current Alpaca REST API docs or confirmed via live response — never assumed from adjacent docs (e.g. Order vs Activity object) |
| RC-7 | Zero-share sizing (int truncation before floor) | `int(raw_shares`, `int(_raw`, `* mult)` in sizing path | Every sizing result has `max(int(raw × mult), min_shares)` guard before order submit — `int(x)` where `x < 1.0` silently produces 0 |
| RC-8 | Unbounded scan buffer (confirm_gate not cleared on non-fill block) | `_entry_confirm_buffer`, `_conviction_streak` | Both dicts must be cleared when a symbol exits consideration for any reason (fill, exit, sector block, MRI gate) — not only on daily reset or trade close |

**Record RC results in `logs/tb_audit_log.md` before writing any patch** — one line per class per file.

**File hotspot rule:** `execution/portfolio_tracker.py` and `main.py` are the two highest-recurrence files
(patched every session since 2026-04-18). Any session that opens either file must check `logs/bug_counter.json`
`file_hotspot` section before writing a patch — risk rating drives audit depth requirement.

### DEBUG SESSION PROTOCOL — Live RC Counts (updated every time bug_counter.json is updated)

**A "debug session" is root-cause analysis + pattern elimination — not individual patches.**
The goal is to find WHY a class keeps recurring and fix the structural cause, not chase the next instance.

**MANDATORY UPDATE RULE:** Whenever `logs/bug_counter.json` is updated in any session, the counts
table below MUST be updated in the SAME TURN. Updating the JSON file without updating this table is
a protocol violation. This is the authoritative live view for scheduling debug sessions.

#### Live RC Counts (as of 2026-06-11 S58c)

| RC | Class | Count | Status | Top File(s) |
|----|-------|-------|--------|-------------|
| RC-3 | Silent exception (bare `pass` / `debug` swallowing errors) | **0** | CLOSED — last unlocalized instance found+fixed S58: autonomous_patch_generator.py L67 `_log()` (commit 2c4552d) | — |
| RC-4 | Estimated exit price (non-fill price passed to record_exit) | **10** | OPEN — 3 violations in exit_logic.py | exit_logic.py L1345, L1939, L2032 (strategy decision pending #2) |
| RC-2 | CWD-relative path (logs/ not anchored to `__file__`) | **7** | OPEN | run_cycle.py, entry_logic.py |
| RC-1 | Naive datetime (tz-unaware `datetime.now()`) | **4** | CLOSED (all 16 instances fixed 2026-04-28) | — |
| RC-5 | Non-atomic write (no tmp→replace pattern) | **1** | OPEN — manual_audit.jsonl append in portfolio_tracker.py L1796 (low risk — log file) | portfolio_tracker.py |
| RC-6 | Wrong API field name (Alpaca field assumed not confirmed) | **3** | OPEN | portfolio_tracker.py |
| RC-7 | Zero-share sizing (int truncation before floor guard) | **2** | OPEN | main.py |
| RC-8 | Unbounded scan buffer (confirm_gate not cleared on block) | **0** | CLOSED — 9 sites applied b2e61f7 (2026-06-08); DS/GAI IO objection retracted via tie-breaker S58c | — |

#### Top Hotspot Files by Patch Count (as of 2026-06-07 S53)

| File | Patch Count | Risk Rating | Debug Session Priority |
|------|-------------|-------------|----------------------|
| execution/portfolio_tracker.py | **36** | CRITICAL | P0 — schedule dedicated session |
| main.py | **33** | CRITICAL | P0 — schedule dedicated session |
| execution/exit_logic.py | **9** | HIGH | P1 — RC-4 (3 violations, pending strategy decision #2) |
| execution/entry_logic.py | **3** | HIGH | P1 — RC-8 9 sites pending approval #1 |
| strategy/run_cycle.py | **9** | MEDIUM | P2 — _base_min fix deployed S51 |

#### What to Do in a Dedicated Debug Session

1. Pick the highest-count RC class (currently RC-4, count=10)
2. Full read ALL files where that RC class appears (no grep substitutes)
3. Root cause analysis: why does this class keep appearing? Is it a shared pattern? A copy-paste template that propagates the error? A missing linter rule?
4. Fix structurally — not just patch each instance, but add the guard to prevent future instances (linter rule, base class, shared utility)
5. Update RC count to 0 in both `logs/bug_counter.json` AND this table in the same turn
6. Board vote required if the structural fix touches execution paths

### Proactive Audit Rule
The board must audit all files touched in a session AND all adjacent/dependent files
before the session ends. This is not limited to the current session's changes — the
board scans for pre-existing issues in any file it opens.

Specifically:
- Any build that modifies `main.py` triggers a full read of the modified functions
  plus all functions that call them and all functions they call.
- Any build that adds a new file triggers a read of all files that import it.
- Any build that changes data flow (signals, sizing, exits) triggers a cross-check
  of the full path: signal generation → scoring → sizing → entry → exit → P&L recording.
- Any session that opens a file for any reason triggers a pre-existing bug scan
  of that file — not just the lines touched.

### What "Audit" Means
- **TB (Peterffy, Katsuyama, Beck, Gene Kim, McKinney, Minsky, Derman, Schneier, Majors):**
  Flag bugs at the assembly level. Exact failure conditions, edge cases, affected code paths.
  Not "this might be a problem" — exact line numbers and reproducing conditions.
  Scope: ALL code in files opened this session, not just changed lines.
- **AB (Harris, Thorp, Levitt, Tudor Jones, Dalio, Douglas, Sosnoff+Sinclair+Nathan, Brandt, Jegadeesh+Titman, Asness, López de Prado, Tulchinsky):**
  Flag logic gaps that create risk exposure: slippage, P&L corruption, PDT rule violations,
  sizing errors, exit degradation.
  Scope: Any function in the trading path adjacent to what was modified.
- **BoD (Simons, Taleb, Peterffy, Kyle, Shaw):**
  Flag architectural contradictions: dead code from conflicting builds, tail risk exposure,
  invariant violations.
  Scope: Cross-file data flow, order lifecycle, P&L recording integrity.

### Audit Coverage Checklist (run at session end)
- [ ] All functions modified this session — read in full
- [ ] All callers of modified functions
- [ ] All new files — imports, side effects, output paths
- [ ] Pre-existing bug scan of every file opened this session
- [ ] RTH block present in any new analysis/backtest script (9:30 AM–4:00 PM ET)
- [ ] No direct yfinance calls for any US equity or ETF — must use `data/fetcher.py`
- [ ] No raw `requests` calls to any market data endpoint
- [ ] No execution imports in analysis/backtest scripts
- [ ] GTC order handling — any orphan adoption path checked for stranded orders
- [ ] P&L recording paths — fill price source verified, no side ambiguity
- [ ] Data source tier tagged in any new data-fetching code
- [ ] All new user-facing timestamps display in PT (America/Los_Angeles)

---

## BOARD INTELLIGENCE PROTOCOL

**Permanent rule — applies to every board interaction in this project.**

### Intelligence Level
All 27 board members operate at **150 IQ** in their respective domains. This means:
- BoD outputs are graduate-research-level: rigorous, quantitative, no hedging on domain expertise.
- AB outputs lead with specific formulas, thresholds, and citations from published work.
- TB outputs flag bugs at the assembly level — not "this might be a problem" but exact
  failure conditions, edge cases, and tested mitigations.
- No filler. No "it depends." Every member gives a concrete directional answer.

### Persona Sourcing — Mandatory for Every Board Interaction

**Every board member must draw from their full public record before voting or opining.**

Claude must ground each member's position in verifiable public sources before stating it.
Approved source types (exhaust all before abstaining):
- Published books, research papers, whitepapers
- Recorded interviews, lectures, conference talks, podcasts
- Documented fund strategies, known institutional frameworks
- Public statements, blog posts, documented trading decisions

**Reference examples per member (non-exhaustive):**
- Thorp: Kelly Criterion papers; *Beat the Dealer*; *A Man for All Markets*; Princeton-Newport Partners documented strategy
- Taleb: *Incerto* series (*Antifragile*, *The Black Swan*, *Fooled by Randomness*); fragility/convexity framework
- López de Prado: *Advances in Financial Machine Learning*; CPCV/combinatorial purging papers; Quantresearch.io
- Derman: *My Life as a Quant*; local vol model; model risk framework papers
- McKinney: *Python for Data Analysis*; pandas documentation; PyData conference talks
- Kim: *The Phoenix Project*; *Accelerate* (DORA metrics); DevOps Research Association
- Schneier: *Secrets and Lies*; *Applied Cryptography*; Schneier on Security blog
- Simons: RenTec Medallion documented performance; MIT/IAS lectures; congressional testimony
- Brandt: *Diary of a Professional Commodity Trader*; Factor Trading Plan blog
- Douglas: *Trading in the Zone*; *The Disciplined Trader*
- Dalio: *Principles*; All Weather / Risk Parity framework; Bridgewater Daily Observations
- Asness: AQR research papers (value/momentum/carry); *Fact, Fiction and Value Investing*
- Jegadeesh+Titman: 1993 momentum paper (*Returns to Buying Winners and Selling Losers*)
- López de Prado + Tulchinsky: WorldQuant research; *Finding Alphas*
- Majors: Honeycomb.io blog; *Observability Engineering*; SREcon talks
- Minsky: Jane Street OCaml/async architecture; *Real World OCaml*
- Peterffy: IBKR infrastructure; automated market-making history; congressional statements
- Katsuyama: *Flash Boys* (Lewis); IEX exchange design documentation
- Beck: *Test-Driven Development*; *Extreme Programming Explained*; *Tidy First?*
- Gene Kim: *The Phoenix Project*; *The DevOps Handbook*; *The Unicorn Project*

**Citation format — required for every substantive claim or vote:**
`[Member Name]: [position] — grounded in [source title / documented position / recorded statement]`

**If no public record can be found for a specific claim:**
Flag it explicitly: `[inferred from general domain knowledge — not sourced to public record]`
Never present inferred positions as the member's documented view.

**Abstention policy — absolute last resort:**
A member may abstain ONLY after Claude has exhausted books, papers, interviews, lectures,
and documented decisions. Abstention must be declared as:
`[ABSTAIN — exhausted all available public record on this specific question]`
Abstaining without a sourcing attempt is a protocol violation.

### Layman Fallback
If the user says "I don't understand that" or asks for clarification, Claude summarizes
the board output in plain language without losing the directional conclusion.

### Voting Rules
- **Strategy changes** (MIN_SCORE, stops, targets, exit logic): Full board vote — all 3 boards weigh in.
- **Feature-specific reviews**: Call the relevant domain board(s) only.
- **Data source changes**: TB (McKinney, Katsuyama) + AB (Harris) required.
- Board must review actual data/logs before providing analysis — no fabricated facts.

### Domain → Board Mapping (real verified members only)
| Domain | Primary | Support |
|--------|---------|---------|
| Signal construction, regime adaptation | Jim Simons (BoD) | David Shaw (BoD) |
| Position sizing, Kelly, kill switch | Ed Thorp (AB) | Nassim Taleb (BoD) |
| Market microstructure, order flow | Albert Kyle (BoD) | Larry Harris (AB) |
| Trade execution, entry/exit mechanics | Larry Harris (AB) | Peter Brandt (AB) |
| Compliance, PDT, regulatory | Arthur Levitt (AB) | — |
| Macro regime, overnight holds | David Shaw (BoD) | Paul Tudor Jones (AB) |
| Cross-asset correlation, portfolio | Ray Dalio (AB) | Nassim Taleb (BoD) |
| ML/signals, overfitting | Marcos Lopez de Prado (AB) | Jim Simons (BoD) |
| Factor/momentum signals | Cliff Asness (AB) | Jegadeesh+Titman (AB) |
| 0DTE / options / IV | Tom Sosnoff+Sinclair+Nathan (AB) | — |
| Trading psychology, exit discipline | Mark Douglas (AB) | — |
| Alpha validation | Igor Tulchinsky (AB) | — |
| Infrastructure, error handling | Thomas Peterffy (BoD) | Brad Katsuyama (TB) |
| QA, release readiness | Kent Beck (TB) | Gene Kim (TB) |
| Security, secrets, audit | Bruce Schneier (TB) | — |
| Data engineering, bar integrity | Wes McKinney (TB) | — |
| Async, low-latency | Yaron Minsky (TB) | — |
| Observability, alerting | Charity Majors (TB) | — |
| Quant architecture | Emanuel Derman (TB) | — |
| UI/UX, mobile-first, dashboard layout | Luke Wroblewski (TB) | — |

---

## PROJECT CONTEXT

- **Bot type:** Alpaca Markets paper trading bot (MTF confluence scoring)
- **Account:** Paper account (~$2.5K), PDT-constrained (max 3 day trades / 5-day window)
- **Scoring:** 12-point confluence system (live), 16-point system (log only, validation)
- **Profile active:** paper (INTRADAY_STOP_ATR_MULT=1.25, TARGET=2.5x, MIN_SCORE=9/12)
- **MRI active:** MacroRiskIndex — 8-component cross-asset score, 0–100, 5 levels
- **Macro Regime Detector:** 1–2 year structural signal (RSP/SPY, yield curve, HYG/LQD, IWM/SPY,
  XLY/XLP, SPY/TLT). Background-only. Does not gate entries — provides structural context for MRI.

**Approved External APIs:**
| API | Key Env Var | Use | Free Limit |
|-----|-------------|-----|-----------|
| Alpaca Data | `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | T1 bar and quote data | Unlimited (paper plan) |
| FMP | `FMP_API_KEY` | T2 fundamentals, earnings, economic calendar | 250 calls/day free |

**Approved Reference Data (no API key):**
- TraderMonty breadth CSV — market breadth, sector uptrend ratios

**Skills installed:**
- `~/.claude/skills/`: statsmodels, statistical-analysis, exploratory-data-analysis, matplotlib, polars
- `claude-trading-skills-main/`: 54 skills — position-sizer (approved sizing reference),
  macro-regime-detector, market-breadth-analyzer, uptrend-analyzer, earnings-trade-analyzer (FMP),
  economic-calendar-fetcher (FMP), trader-memory-core

---

## ARCHITECTURE INVARIANTS — DO NOT CHANGE WITHOUT BOARD VOTE

1. **SPY 5-min bar-over-bar is the SOLE entry gate.** MRI and Macro Regime Detector only adjust
   quality bar and size floor.
2. **Keywords are display-only.** CAUTION/MONITOR = zero size impact. Only HALT = 0.0x.
3. **PDT enforcement disabled (S50, 2026-06-06, board unanimous).** PDT rule removed for accounts <$25K.
   `PDT_ENFORCEMENT_ENABLED=False` in config.py gates all PDT logic. Feature flag preserves reversibility
   for live accounts. GTC stops submit for ALL overnight positions regardless of PDT state.
4. **Bar staleness uses CLOSE-based age.** `_bar_ts_et + timedelta(minutes=15)` — do not revert to open-based.
5. **Entry price = Alpaca Data real-time last trade + bar close fallback.** Replaces yfinance fast_info.
   Source logged with every entry event in `trade_events.jsonl`.
6. **Kill switch is 7% for paper** (board vote 2026-04-22, 25-1; confirmed S50 board 13-0).
   config.py paper profile L243 is the single source of truth. Tiered upgrade path:
   $10K→10% | $20K→12% | $25K→15% — each requires a board vote.
   BoD-3 main.py override was dead code (condition never fired) and has been removed (S50).
7. **Bucket A (TQQQ/SQQQ/TSLL) exemption from safe_close_all:** Applies ONLY on routine news halts
   (`circuit_breaker=False`). Circuit-breaker halt closes everything unconditionally.
8. **paper=True hardcoded in broker.py.** Change to False ONLY at live launch after full board vote.
   `TRADING_ENV` env var controls analysis behavior — it never overrides the broker hardcode.
9. **MRI is background only.** Sets size floor and MIN_SCORE floor. Does not gate entries directly.
10. **Max correlated exposure:** No more than 2 simultaneous positions with beta correlation >0.7
    to each other. Sector gate already enforces sector-level; this covers cross-sector beta overlap.
11. **Overnight exposure budget:** Max 100% of account equity in overnight positions.
    Breach requires board review before next entry.
12. **VIX-adjusted stop widening:** When VIX spot > 25, ATR stop multiplier widens
    1.5× (25 < VIX ≤ 30) or 2.0× (VIX > 30) automatically. Target scales proportionally
    to preserve R:R. Config: `VIX_STOP_WIDEN_THRESHOLD_1/2`, `VIX_STOP_WIDEN_MULT_1/2`.
    Implemented in `execution/risk_manager.py:get_stop_and_target()`.

---

## FUTURE ROADMAP LOG

**Purpose:** Capture items mentioned in passing — innovations, moonshots, full-session-scope ideas —
so they are never lost between sessions. These are NOT open items (no action this session).
They are logged here so that when we have bandwidth, we know exactly what to build next.

**Add rule:** Any time a future-state idea, novel approach, or multi-session scope item is mentioned
in conversation — even briefly — log it here before the session ends. Format: `[DATE SESSION] Description`.

**Review rule:** At the start of any session where the user asks "what's next" or "what should we build,"
scan this log first alongside the open items list.

---

### Logged Items

**[2026-05-17 S25B] After-close STOD volume normalization for swing pre-filter (DS insight)**
Compute `today's total volume / 20-day avg` at 4:05 PM ET as an after-close computation.
Eliminates the partial bar problem entirely (full bar vs full bar, apples-to-apples).
This is the prerequisite before Option B volume scoring can work at all.
Requires: ≥30 samples of after-close data, dedicated board vote, STOD normalization design.
Target: after 5+ weeks of shadow data accumulation. File: `events/macro_risk_index.py` + new after-close hook.

**[2026-05-17 S25B] Volume additive scoring — Option B (+1pt when vol_ratio ≥ 1.5x, never subtractive)**
Additive only: +1pt when today's STOD-normalized volume clears the 1.5x threshold.
Never gates or blocks entries — never subtractive. Blocked until STOD normalization is built first.
Requires: ≥30 post-close samples, STOD design complete, full board vote.

**[2026-05-17 S25B] GEX levels from Alpaca option chain wired into scoring (GAP-I2)**
Gamma exposure (GEX) levels as a 1pt confluence modifier or MRI input.
`get_option_chain` MCP tool is already available. Need GEX computation formula + board vote on scoring impact.
Could use GEX-neutral zone as a "caution" signal and GEX magnet levels as target confirmation.

**[2026-05-17 S25B] Real-time VIX3M source investigation**
FMP `^VIX3M` = 402 Premium. CBOE free CSVs = end-of-day only. CBOE real-time API = requires investigation.
Finnhub VIX3M = null. yfinance VIX3M = works but subject to hanging (current T4 fallback).
Future: dedicated research session to find a free or low-cost real-time VIX3M source.
Options: CBOE paid tier, Quandl, Tiingo, or derive synthetic VIX3M from option chain data.

**[2026-05-17 S25B] ThreadPoolExecutor blocking-call pattern as reusable template**
The timeout wrapper built for yfinance (#14) — `concurrent.futures.ThreadPoolExecutor(max_workers=1)`
with 8s wall-clock timeout + `shutdown(wait=False)` + stale_since tracking — is a reusable template
for any future external API calls that might hang (Finnhub, Currents, Marketaux, FMP slow endpoints).
Future: extract into `utility/safe_fetch.py` as a shared decorator/wrapper once #14 is validated.

**[2026-05-17 S25B] TraderMonty breadth CSV integration (GAP-V8)**
TraderMonty breadth CSV not yet implemented — `data/breadth.py` stub exists. Free, no API key.
Contains: market breadth, sector uptrend ratios. Could serve as a VIX-alternative breadth component
for MRI scoring (GAP-V7: breadth as ±1pt confluence modifier). Board vote required before wiring into scoring.

**[2026-05-17 S25B] Dedicated debug session — RC-3 root cause elimination (22 instances)**
RC-3 (silent exception, count=22) is the highest recurring bug class. Instead of patching each instance
individually, a full debug session should map all 22 instances, find the structural cause (likely missing
logging standard in exception handlers + no linter rule), and eliminate the pattern permanently.
Candidate fix: add a custom ruff/flake8 rule that rejects bare `except: pass` and bare `except Exception: pass`.

---

## PENDING / DEFERRED

- `C-2`: `paper=True` hardcoded in `execution/broker.py:21` — change to `False` only when going live
- ~~T2~~ ✅ Kelly calibration complete (board vote Apr 2026): `KELLY_FRACTION` → 0.25 (paper), `KELLY_MIN_SAMPLE_SIZE` → 30, `KELLY_MIN_RISK_PCT` → 0.0075, `pstdev` → `stdev` in `kelly.py`. Score-weighted warmup sizing deferred (separate full board vote required).
- ~~T3~~ ✅ MRI STRESSED+ breakeven push — `_apply_mri_breakeven_push()` in `main.py`, wired into `run_cycle()` between `check_partial_exits` and `check_exits` (2026-04-18, full board vote)
- ~~DATA-4 phase 2~~ ✅ Economic calendar live — `get_economic_calendar()` in `data/fmp_client.py`, injected via `calendar.add_event_dynamic()` in `main.py` at startup (45-day lookahead, 6h cache, US high-impact only)
- ~~backtest_12pt~~ ✅ Already T1 compliant — `fetch_bars` throughout, zero `yf.download` calls. PENDING note was stale (confirmed 2026-04-18).

**Completed (Apr 18 2026):**
- ~~T3b~~ ✅ Macro risk window persisted to `data/state/hybrid_state.json` (`events/news_monitor.py` — `_persist_macro_risk_window` / `_restore_macro_risk_window`)
- ~~TB-1~~ ✅ `portfolio_tracker.py:644` — `STATIC_EVENTS` import fix (PDT holiday detection)
- ~~TB-2~~ ✅ `kelly.py:26` — absolute path anchor for `KELLY_STATS_FILE`
- ~~TB-3~~ ✅ `portfolio_tracker.py:91,157,607` — ET/PT timezone-aware datetimes
- ~~TB-5~~ ✅ `volatility_regime.py:70,181` — `.total_seconds()` fix

**Completed (Apr 15 2026):**
- ~~DATA-1~~ ✅ `signal_generator.py` weekly bias + intraday override → `fetch_bars` T1
- ~~DATA-2~~ ✅ All 10 yfinance equity/ETF calls in `main.py` → Alpaca Data T1
- ~~DATA-3~~ ✅ `macro_risk_index.py` ETF fetches (TLT/HYG/LQD/USO/GLD/EWJ/EWG) → `fetch_bars` T1
- ~~DATA-4~~ ✅ `data/fmp_client.py` built; earnings calendar → FMP T2
- ~~DATA-5~~ ✅ `logs/trade_events.jsonl` structured logging live (`trade_logger.py`)
- ~~DATA-6~~ ✅ VIX stop widening already implemented in `risk_manager.py:get_stop_and_target()` — config constants at `config.py:293–296`
- ~~TZ-1~~ ✅ All 9 PT timestamp fixes applied across dashboard, weekly review, scanner, audit scripts

<!-- code-review-graph MCP tools -->
## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

### Key Tools

| Tool | Use when |
|------|----------|
| `detect_changes` | Reviewing code changes — gives risk-scored analysis |
| `get_review_context` | Need source snippets for review — token-efficient |
| `get_impact_radius` | Understanding blast radius of a change |
| `get_affected_flows` | Finding which execution paths are impacted |
| `query_graph` | Tracing callers, callees, imports, tests, dependencies |
| `semantic_search_nodes` | Finding functions/classes by name or keyword |
| `get_architecture_overview` | Understanding high-level codebase structure |
| `refactor_tool` | Planning renames, finding dead code |

### Workflow

1. The graph auto-updates on file changes (via hooks).
2. Use `detect_changes` for code review.
3. Use `get_affected_flows` to understand impact.
4. Use `query_graph` pattern="tests_for" to check coverage.
