# CORE RULES — always-loaded, every session (the tiny non-negotiable block)
<!-- CONSOLIDATION (Rafael mandate 2026-08-22, Hybrid C+). This is the ONLY block the agent must hold
     in-context every session: the safety envelope, the patch-sequence skeleton, and the behaviour rules that
     have NO gate backstop (the agent must self-govern them BEFORE any gate fires). Each line is a POINTER to
     the full rule (RULE-ID → rules/<file>.md via rules/manifest.yaml, being built). The full rule text and all
     on-demand rules live in rules/ topic files. CLAUDE.md is being converted to a lean index that loads this
     file + rules/OPERATING.md every session. Governance rules (26-33) live in rules/OPERATING.md (also
     always-loaded). SOURCE of every line below = the audited CLAUDE.md section cited in (§...). Nothing here is
     invented; it is extracted from the existing rulebook. -->

## A. SAFETY ENVELOPE — never widen (only more aggressive WITHIN it)
- S01 `paper=True` hardcoded in broker.py — change only at live launch after a full board vote. (§Arch-Inv #8)
- S02 Kill switch = 7% for paper; each tier upgrade ($10K→10% / $20K→12% / $25K→15%) needs a board vote. (§Arch-Inv #6)
- S03 Never-mask-a-loss — a risk-path guard overrides the raw reading ONLY on a positively-confirmed fault; on any ambiguity, return raw so the kill switch stays sensitive. (§Safety-control-never-masks-a-loss)
- S04 Data tiers: T1 Alpaca / T2 FMP / T3 breadth / T4 yfinance (^VIX,^VIX3M,JPY=X only). Never yfinance for equities/ETFs; never raw `requests` to market data. (§Guardrail 1)
- S05 Execution isolation: `TradingClient` only in execution/broker.py; `StockHistoricalDataClient` only in data/fetcher.py. (§Guardrail 2)
- S06 RISK-PATH diff → mandatory cold board + masked-loss seat + Gro + GAI. Risk-path = can raise per-trade/aggregate SIZE, FREQUENCY, or CONCURRENCY/exposure, INCLUDING via any multiplier upstream of the Kelly/gross caps. (§Build-Don't-Fix E)
- S07 `.env` secrets (GROQ/GEMINI/ALPACA/FMP keys) never hardcoded in any tracked file. (§Gro/GAI API)

## B. MANDATORY PATCH SEQUENCE — the orchestration the agent runs (zero exceptions)
- P01 Full-read gate before ANY patch: read every line; declare "Full read complete: N lines"; file >1000 L → Explore subagent returning VERBATIM content (no summaries). NO grep/search as file exploration — ever. (§Full-Read Gate, §Explore Subagent)
- P02 10-point audit + RC-1…RC-8 grep-checks, written to logs/tb_audit_log.md, before a patch is proposed. (§10-Point, §Recurring Bug Classes)
- P03 Board vote = independent COLD parallel Explore subagents, never inline roleplay. (§Board Execution Model)
- P04 Gro+GAI external audit on any RTH-impacting non-read-only code; on ANY fail/reject from ANY reviewer → counter-prompt with the specific refuting evidence, NEVER a blind re-roll (≤3 rounds). (§Step 4, §Disagreement Protocol)
- P05 Statics clean, all three: `py_compile` + `mypy --warn-unreachable` + `ruff check --select E,W,F,B`. (§5a)
- P06 Cold second-agent logic review PASS required; EVERY revision requires a FRESH review (marker binds to content sha). (§Cold-2nd)
- P07 FINAL Gro+GAI pre-ship audit on the EXACT final diff (not the concept) — both APPROVE before ship. (§Final Pre-Ship)
- P08 Deploy = git single channel: commit → push → PR → CI → merge → OCI `git pull origin main --ff-only` + restart; literal `DEPLOY_OK` required; NEVER rsync tracked files; deploy is NOT gated on market hours. (§Deploy Mechanism, §Deploy Timing)
- P09 Session boundary: every gate resets to zero each session; compaction summaries satisfy ZERO steps; resuming in-progress work = restart from Step 1. (§Session Boundary C-1…C-8)
- P10 Self-QA pre-ship: (1) no BGG-leading prompts, (2) adversarial self-review vs source, (3) cite a real current log line, (4) BGG design pass on new features. (§Self-QA Gate)

## C. BEHAVIOUR / RESPONSE — pure self-governance, NO gate backstop
- B01 NO-GUESS: never state a hypothesis as fact; every causal/perf/root-cause claim cites its confirming measurement/trace FIRST; a perf claim needs a profile + before/after numbers; unverified cause = say "hypothesis, unverified" and verify before acting. (§No-Guess Mandate)
- B02 Verify agent/board/Gro/GAI claims at source before relaying — execute > static rule > read state > read the cited line. (§Verify At Source)
- B03 Documentation-is-not-enforcement: a rule violated twice → stop rewording it, BUILD the gate that rejects the violation. (§Doc-Not-Enforcement)
- B04 Execute-don't-ask / no menu: pick the highest-priority open item and execute; pause only for a genuinely irreversible/authority-required decision, phrased as a SINGLE decision WITH a board-backed recommendation. (§Authority, §Open Question, user_preferences)
- B05 Never suggest stopping/pausing/checkpointing; keep driving; ship real gated work. (§Response Style)
- B06 Don't narrate the plumbing — run the git/CI/marker mechanics silently; report the OUTCOME + a one-line gate confirmation. (§Response Style)
- B07 Kill filler; match length to the task; admit uncertainty explicitly before any uncertain fact. (§Response Style)
- B08 HONESTY: never label code LIVE/proven/working/verified until it has ACTUALLY executed in production — otherwise "deployed, unexercised". (§Self-QA honesty clause)
- B09 No irreversible/external action (send/post/deploy/migrate) without an explicit in-session "yes"; always show what changed after a coding task. (§Behavior Rules, §Approval Requirement)
- B10 ALWAYS-DYNAMIC / NEVER-STATIC — the is-it-static gate: before ANY diff, line of code, or proposed solution, ask "is this answer STATIC or DYNAMIC?" Static is thrown out ~9/10 times — a DYNAMIC, self-calibrating-from-data derivation IS the recommendation. The 1-in-10 static exception applies ONLY when no historical data exists, and must be flagged explicitly + logged as a roadmap item to dynamize later. No static thresholds (Kelly, sizing, scoring, calendar, news bonuses, regime cutoffs, stops) — ever. BGG is involved in the static-vs-dynamic call on any fork. (memory: feedback_no_static_regimes — PERMANENT 2026-06-19; NOTE: this was NOT in CLAUDE.md — consolidation must add it to the rulebook.)
- B11 ANTI-BIAS + CITATION (kill false premises): BGG/reviewer prompts carry ONLY facts + the raw diff — NEVER verdict-leading language ("board approved", "obviously safe", "should approve") — enforced by `bgg_prompt_bias_gate`. Reviewers must cite a VERBATIM quote per finding, run a mandatory self-check before the verdict, and REJECT only on a CONCRETE failing input (input→wrong output/crash); theoretical "could"s go in a NITS section, never a REJECT. Pre-load ground-truth `--context` to pre-empt false-premise rejects; on any reject → counter-prompt with the specific refuting evidence, NEVER a blind re-roll. (§Self-QA #1, §Reviewer-Context, §Disagreement Protocol, §No-Leading-Prompts)

<!-- Governance rules 26-33 (Rafael sole authority; Profitable>Perfect; Build-Don't-Just-Fix; Feature Design
     Protocol; Open Question Protocol; Durable Sync; graph-before-grep; session-start reads) live in
     rules/OPERATING.md, also always-loaded. Full text of every rule above + all on-demand rules → rules/ topic
     files (migration in progress). This CORE file is authoritative for the pointers; the topic files are
     authoritative for the full text. -->
