# QUEUE (major architectural build) — BGG Pipeline v2: multi-agent personas + async + sandbox loop
**Rafael 2026-08-22:** "Add this to the queue. Whenever the BGG is called, these are the prompts that will need
to be included. I will be adding NVIDIA Build to capture DeepSeek's reasoning." This UPGRADES how BGG (Board +
Gro + GAI) reviews every diff. NOT built yet — queued. When built, the personas below become the STANDARD BGG
invocation.

## FACT CHECK (verified at source 2026-08-22)
DeepSeek is NOT currently connected: `DEEPSEEK_API_KEY` is a dormant leftover in `.env`; `api.deepseek.com`
appears in ZERO code lines; pipeline migrated DeepSeek→Groq 2026-06-30 (unfunded). DeepSeek-R1 must be wired
FRESH — via **NVIDIA Build** (`integrate.api.nvidia.com`, model `deepseek-ai/deepseek-r1`, OpenAI-compatible,
free credits) [Rafael's plan] or OpenRouter fallback. Current live reviewers: Gro = Groq `openai/gpt-oss-120b`;
GAI = Gemini `gemini-flash-latest` (free key + `GEMINI_PAID_API_KEY` paid failover).

## THE THREE UPGRADES (Rafael's spec, verbatim intent)
### 1. SMARTER — multi-agent role assignment (strict, opposing, domain-specific personas)
- **THE AUDITOR (Gemini, large context):** hold the ENTIRE trading framework codebase + broker (Alpaca) API
  docs + the new diff in the big context window. Persona: *"You are a MALICIOUS code reviewer. Your only goal
  is to find race conditions, hidden API rate-limit breaches, or look-ahead biases in this diff."*
  IMPL NOTE: `gemini-flash-latest` may not hold the whole codebase on the free tier — the "hold entire
  codebase" auditor likely needs the PAID Gemini tier (GEMINI_PAID_API_KEY) or curated context packing.
- **THE TESTER (DeepSeek-R1 via NVIDIA Build / OpenRouter — a reasoning/CoT model, "smarter at logic than
  Groq base"):** Persona: *"Do NOT read the code for syntax. Write a standalone Python pytest script that
  specifically tries to BREAK the logic introduced in this diff."*
- (Gro keeps its HFT-execution persona; the board keeps its seats.) These personas are INCLUDED EVERY BGG call
  once the pipeline supports them.

### 2. FASTER — asynchronous parallel execution
- Use `asyncio` to BROADCAST Claude's diff to Gemini + Groq + NVIDIA(DeepSeek-R1) at the SAME time (not
  sequential Claude→Gemini→Groq). Collect all critiques simultaneously. Respect free-tier RPM limits.
- If ANY model flags a HIGH-SEVERITY error, the pipeline HALTS and feeds the error back to Claude for a
  rewrite. (Composes with the existing DISAGREEMENT PROTOCOL: counter-prompt with evidence, never blind
  re-roll; and the `preship_audit.py` counter-prompt tooling gap.)

### 3. BETTER — the "Execution Sandbox" loop (remove human-in-the-loop code execution)
- Claude generates the diff → an automated script applies it to a GIT SANDBOX BRANCH → DeepSeek-R1 / Gemini
  generate a test suite (MOCKING the broker API data) → the local pipeline EXECUTES the code → the raw terminal
  output (success or stack trace) is fed back to the AI. "AI should never just look at code; it should see the
  code run."
- OVERLAP: this AUTOMATES Self-QA gate #2 (adversarial pytest, currently mandatory-MANUAL) and complements the
  cold-2nd. Must run in an isolated worktree/sandbox, never against live state; broker calls MOCKED (paper=True
  invariant untouched).

## FREE-TIER CONTEXT STRATEGY (Rafael 2026-08-22 — "keep using free tier as much as possible")
DO NOT have the auditor hold the ENTIRE codebase — it is not just a cost/paid-tier issue, it DILUTES the
reviewer's attention (worse reviews) AND blows free limits. Alternatives, all free-tier, best→simplest:
1. **Surgical impact-radius packing (the winner):** send the reviewer the diff + ONLY the code it touches
   (specific callers/callees/dependents). We ALREADY have this FREE: `code-review-graph` (get_impact_radius,
   get_review_context, get_minimal_context) returns the exact blast radius token-efficiently; `token-savior` /
   `context-mode` do retrieval. This is what the cold-2nd agents already do (read specific files, not 500).
   Cheaper AND better (focused).
2. **Retrieval/RAG on demand:** index the codebase once (embeddings), retrieve only relevant chunks per diff.
3. **Chunk large diffs file-by-file:** each changed file + its context fits free-tier; never one giant dump.
4. **The knowledge graph IS the codebase memory:** reviewer needs the structural map (callers/dependents),
   not raw source.
5. **Reserve PAID for the rare whole-codebase-reasoning case ONLY (absolutely necessary).**
PROOF IT'S NEEDED (today): a SINGLE-FILE diff hit BOTH free walls — Gro `openai/gpt-oss-120b` 8k-TPM
("Requested 10056": input + the reserved 4096 max_completion_tokens); GAI `gemini-flash-latest` 503-flapping.
Minimal targeted context + a smaller Gro output budget keeps it under 8k free.

## ANTI-FALSE-REJECT PROMPT CONTRACT (Rafael 2026-08-22 — "we're wasting tokens on false-premise rejects")
The bias gate (bgg_prompt_bias_gate.py) enforces MY side (no verdict-LEADING prompts). But the anti-FALSE-
REJECT standard is only DOCUMENTED, not baked into the reviewer PROMPT → false rejects recur (today: Gro
claimed a KeyError, missing the `stats = compute_period_stats(...)` base assignment ABOVE the diff; reversed
in ONE counter-prompt). Per DOCUMENTATION-IS-NOT-ENFORCEMENT, BUILD the contract into the preship_audit.py
reviewer prompt template as the enforced default: (a) MANDATORY verbatim quote of the exact flagged line — a
REJECT with no quote is DISCARDED; (b) a self-check before verdict: "did I trace the real execution path
INCLUDING code outside the diff — base assignments, cross-file helpers, callers?"; (c) REJECT ONLY on a
concrete failing input (input→wrong output/crash) that is line-traced — theoretical concerns go in a NITS
section, never a REJECT; (d) make `--context` (ground-truth facts) MANDATORY, not optional. A reviewer that
cannot reject without a citation cannot make the base-assignment mistake → no counter-prompt, no wasted tokens.

## FREE-KEY DISCIPLINE + INFRA GAPS (Rafael 2026-08-22 — "don't use the GAI paid key unless absolutely necessary")
- FREE-FIRST always. On 503 (transient high-demand), RETRY the free key with backoff. Paid key is a LAST
  RESORT for genuine persistent unavailability only.
- INFRA GAP #1: `preship_audit.py` has NO retry/backoff on 503 — it fails-closed with no marker. ADD
  retry-with-backoff on the free key (this is the primary reliability fix; keep paid as last resort).
- INFRA GAP #2: the Gro request exceeds the 8k free TPM (reserved 4096 output). REDUCE the Gro
  max_completion_tokens (~1800) and/or trim the diff-context so Gro fits the free tier — removes the recurring
  "Request too large" and the need to --waive-gro.
- INFRA GAP #3: `_gai` fails over to paid only on 429, not 503 — but per Rafael, do NOT auto-failover to paid on
  503; retry free instead. (Only escalate to paid when free is persistently down AND a ship is blocked.)

## BUILD NOTES / GATE
- Wire the NVIDIA Build endpoint (new client, key in .env — never hardcoded). Add DeepSeek-R1 as the 3rd
  external reasoning voice alongside Gro + GAI.
- Files: `.claude/preship/preship_audit.py` (personas + async broadcast + 3rd voice + counter-prompt path),
  `auto_ai_audit.py` (meta-audit), a new sandbox-runner (git worktree + mocked pytest executor).
- This is GATED_SELF tooling (touches the review gate) — build via Feature Design Protocol + full BGG on the
  design AND diff. Non-trivial: async RPM handling, sandbox isolation, mocked-broker fixtures, halt/feedback loop.
- Rafael adds NVIDIA Build access (his action); Claude wires the client once the key is in .env.
