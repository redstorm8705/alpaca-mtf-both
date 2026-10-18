# Owner-approval gates — build-out of the existing hook system (2026-10-17, Claude)

Approval-ID: GATES-1017
Covers: .claude/preship/owner_approval_capture.py, .claude/preship/owner_approval_gate.py, .claude/preship/bgg_call.py,
.claude/preship/preship_gate.py, .claude/preship/record_design.py, .claude/preship/test_owner_approval.py,
.claude/settings.json, scripts/install_managed_gates.sh, scripts/managed_gates/run_gate.py,
.claude/preship/behaviour_integrity.py, .claude/preship/owner_approval_githook.py,
.claude/preship/required_questions.json, .claude/preship/required_questions.py, .claude/preship/bgg_prompt_bias_gate.py

## Required questions
Q1: Not a trading change. It protects the North Star by stopping unapproved changes like #687, which cut Day-tier trades (data collection) without the owner's approval.
Q2: No direct change to trade count; it prevents any future change that would cut trades from shipping without the owner's approval.
Q3: Should exist (owner ordered it after #687). Simpler alternatives considered and rejected with reasons: documentation-only rules (violated repeatedly), a GitHub server wall (owner declined). Gets to yes by keeping every capability, gated on his APPROVE line.
Q4: Extends existing gates (listed under Prior work): bgg_prompt_bias_gate, preship_gate, design_record_gate, record_design, execute_dont_ask_gate; no duplicate. No open PR or Codex branch touches .claude/preship (checked git log origin/main 2026-10-17).
Q5: Gate logic has no trading thresholds. The one count (_PRIOR_TOP = 5 related records to cite) is a tooling setting outside the trading scope; the prior-work ranking itself is computed from the records.
Q6: Static assumption exposed: that rules in .md files steer behaviour. Built instead: mechanical gates that reject the work; next forward step is the server-side wall, which the owner declined for now.
Q7: Connected to every gate: one behaviour scope reused from preship_gate, one bias-pattern source (bgg_prompt_bias_gate), one question list (required_questions.json) used by bgg_call, the Agent bias gate, record_design and approval capture.
Q8: Evidence: #687 shipped without approval (PR #687, reverted in #689); the bias gate missed file-based prompts (bgg_prompt_bias_gate scans only the command text — verified in code); hooks were skipped with core.hooksPath on 2026-10-17 (this session's commands).
Q9: Replayed against this session's real breach routes (file-based Groq prompt, core.hooksPath commit, script indirection, cd + sed, git apply, forged approval, merge flows) in 40 tests; reversal: any unapproved behaviour change reaching origin/main.
Q10: N/A — not a trading edge; no signal is being held back.
Q11: N/A — no change to size, frequency or concurrency; no trading code changes.
Q12: Does not touch stops, the kill switch, never-mask-a-loss, paper=True or data tiers; it adds a process control on code changes only.
Q13: Missing approvals or unreadable state block behaviour edits (fail closed); the integrity check warns instead of blocking every tool on its own internal error; Slack paging failure does not stop the undo.
Q14: No shadow: every gate acts live from the moment it is installed.
Q15: Every block names the files and the reason; every undo is paged to the owner on Slack and the undone version is kept in .claude/quarantine/.
Q16: Proof it ran: a blocked edit message, an undo page, or a refused commit; first-session audit = attempt a covered and an uncovered edit after install and confirm both outcomes.
Q17: Tests run the real hook scripts as subprocesses with real stdin payloads and real git repos (40 tests); after install the owner-run launcher check is verified by editing a gate file in a scratch copy.
Q18: The #687 breach was investigated to root cause (no approval gate, file-based prompts unscanned, hook skipping possible), not explained away.
Q19: Changes the gates themselves, so owner approval is required: Approval-ID GATES-1017.
Q20: Prompts to Gro/GAI were neutral (one leading phrase was refused by the bias gate and removed); four cold/adversarial rounds; logs and code reviewed; design taken to the board (Schneier seat), Gro and GAI before build.
Q21: Tests never reach the broker: every test runs the hook scripts on temporary git repos with no broker import; MTF_TEST_MODE suppresses the Slack page in tests.
Q22: Before calling it live: after the owner installs, probe on this Mac read-only — attempt one covered and one uncovered edit and one gate-file edit, and confirm block, allow and launcher refusal. (It does not run on the server.)
Q23: Full-path simulation = the 40 tests (most run the hooks as subprocesses) over real git repos, including the merge and one-call commit paths; not called ready until the post-install probe in Q22 passes.
Q24: No absence claims are made; the one external claim (managed settings support allowManagedHooksOnly) was checked against the Claude Code docs this session.
Q25: N/A — no stock or market statistics are used by this design.
Q26: Data: no market data; the gates read local git state, the Claude Code transcript and the hook payload only. No fallback is needed: on internal error the PostToolUse check warns and the PreToolUse gate blocks behaviour writes.
Q27: Output: approvals as .claude/approvals/<ID>.json written atomically (tmp then os.replace); undone files are copied to .claude/quarantine/<timestamp>/; a Slack page on every undo.
Q28: Integration: Claude Code hooks (UserPromptSubmit capture, PreToolUse gate, PostToolUse integrity) plus git pre-commit and pre-push hooks and preship_gate.py; none of it runs inside the bot or on the trading thread.
Q29: Value rank: first, because every other item (LLY attribution fix, Swing ETF route, the scorecard) changes trading behaviour, and none of them can safely ship until the owner-approval path enforces itself.
Q30: Whitespace: real firms separate who writes code from who approves a release (four-eyes change control); this adds that. Blind spot our audits miss: the agent itself bypassing the process, which no code review of the trading files can catch; the PostToolUse check judges the resulting file state, not the command used.
Q31: N/A — no trading strategy or parameter is tested; the gates are verified by 40 deterministic tests on temporary git repositories, not a statistical fit.
Q32: N/A — no market data replay; the tests use synthetic git repos, so look-ahead and survivorship do not apply.
Q33: N/A — no orders, fills or costs; the gates never place or change an order.
Q34: N/A — no return stream, so no factor or beta exposure; the gates do not touch position selection or sizing.
Q35: N/A — no trading behaviour in any regime; the only tail risk is blocking a needed urgent fix, handled by the owner typing APPROVE for that fix's record.
Q36: N/A — no capacity or liquidity question; the gates are local developer tooling with no market interaction.
Q37: N/A — no edge to measure; correctness is shown by the deterministic test suite and the post-install probe, not by trade counts.
Q38: Not a strategy, so it never decays; it is what makes Q38 enforceable for strategies, because a change moving a strategy to zero trades now needs the owner's APPROVE before it can be committed.
Q39: N/A — no sizing and no P&L; nothing here reads fills or sets a position size.

## Why (owner, 2026-10-17)
#687 changed Day-tier entry behaviour without the owner's approval, duplicated the Confluence 2.0 admission registry, and
was reviewed with framing-biased prompts sent through a script the bias gate never saw. Owner: build out the existing
hooks (which work: the Stop "execute, don't ask" gate, the pre-ship bias gate) so these unilateral decisions are stopped.

## Prior work (existing gates this extends — read before designing)
- `.claude/preship/bgg_prompt_bias_gate.py` (PreToolUse Bash + Agent): scans the Bash COMMAND TEXT for verdict-leading
  phrases when it names api.groq.com / generativelanguage.googleapis.com; Agent review prompts; paper-account frame.
  HOLE: a script that reads the prompt from a file (my `bggn.py prompt.txt` on 2026-10-17) is never scanned.
- `.claude/preship/preship_gate.py` (PreToolUse Bash on git commit/push): cold-2nd, adversarial, Gro/GAI, log-evidence,
  post-live, design-record and no-static markers bound to staged content. No owner approval check.
- `.claude/preship/design_record_gate.py` + `record_design.py`: a NEW gated file needs a design-record marker. Content is
  not checked; I wrote and registered my own record for #687.
- `.claude/preship/execute_dont_ask_gate.py`, `no_guess_gate.py`, `no_assume_gate.py` (Stop).
- `.claude/preship/fork_reminder.py`, `post_live_due.py` (UserPromptSubmit).
- `bggn_resilience_2026-09-20.md`: Gro/GAI timeouts and dead model IDs; the new bgg_call.py wrapper reuses gai_client's
  model ladder, so it does not reintroduce the single-model failure that record fixed.
- `qhm_earnings_trim_ledger_autoconfirm_GATE_RESULT_2026-08-16.md`: precedent that Gro, GAI and the cold-2nd all
  approved a change the cold board then rejected 3-0 — the reason owner approval must not be replaced by reviewer
  approval in this design.
- `day_tier_v2_design_2026-08-29.md`, `day_tier_internet_strategy_audit_2026-10-05.md`: Day-tier strategy records
  (keyword matches only; no gate or approval mechanism in them) — no overlap with these hooks.
- `self_qa_gate4_design_record.md`: the Self-QA #4 design-record gate (2026-08-12) that created record_design.py and
  design_record_gate.py. This build extends record_design.py (required questions + prior-work citation) rather than
  replacing it; its doc-sha binding is unchanged.
- `alignment_gate_resume_2026-08-30.md`: the paused Self-QA #5 alignment gate (owner, 2026-08-30). Its premise questions
  ('Can this be more dynamic? Is this build accounting for future improvements? How else could this build be better?'
  and 'is this most relevant to $2.5k->$25k') are carried into the required questions as Q1, Q3, Q5, Q6 and Q29; its
  separate cold critic was never shipped and is not rebuilt here.
- `.github/CODEOWNERS` + branch protection (server side): required check "preship"; required reviews = 0 and
  require_code_owner_reviews = false (verified 2026-10-17) — not an owner gate today; out of scope here (owner declined).

## The five additions
1. **Bias gate reads prompt files.** On a Bash call, if the command runs a Python file whose source references a BGG
   endpoint or `gai_client`, every file path in the command (the script and its arguments) is read and scanned with the
   same patterns. A DESIGN review prompt (asks for a design verdict) must also contain three questions: whether the
   change should exist at all, what existing work already covers it, and its effect on trade count / the $25K goal.
   Agent review prompts on a design get the same three-question check.
2. **Owner approval (new).**
   - `owner_approval_capture.py` (UserPromptSubmit): when the owner's message contains `APPROVE <ID>` (or `REJECT
     <ID>`), it finds the design record declaring `Approval-ID: <ID>`, and writes `.claude/approvals/<ID>.json` with the
     record's path, its sha256, its `Covers:` list, the session id and the hash of the owner's message. Only this hook
     writes approvals; it runs on the owner's typed prompt, which the agent cannot produce.
   - `owner_approval_gate.py` (PreToolUse Write|Edit|Bash): a write to a BEHAVIOUR path — `execution/`, `strategy/`,
     `indicators/`, `events/`, `run_*.py`, `main.py`, `config.py`, `data/gex.py`, and the gate files themselves
     (`.claude/preship/*.py`, `.claude/settings*.json`) — is blocked unless an approval record exists whose Covers
     includes the path and whose design-record sha still matches the record on disk, AND the owner's approval text is
     present as a user message in the conversation transcript the hook is given. Bash writes to those paths (redirects,
     sed -i, tee, cp/mv/rm, python open(...,'w')) are checked the same way.
   - `preship_gate.py`: a commit/push staging a behaviour path also requires a matching approval record (the same check
     at ship time, tool-agnostic — the existing mirror pattern).
   - Server: an `ssh mtf-bot` command that edits the crontab or writes inside the bot directory (other than
     `git pull --ff-only origin main` and service restarts/status) needs an approval record covering `server-ops`.
3. **Existing-work check.** `record_design.py` refuses to register a design whose record lacks a "## Prior work" section
   citing every file the tool's own keyword search ranks highest among `logs/design_records/*.md`, the admission
   registries (`strategy/*admissions*.json`) and `handoff.md` (search terms: the design's title words and the covered
   modules' names; the top-ranked files, ties included, must be cited by name).
4. **Gates protect themselves.** Bash commands containing `core.hooksPath`, `--no-verify`, or `git commit -n` are
   blocked outright. Edits to the gate files and settings fall under rule 2 (owner approval).
5. **Stop check.** `execute_dont_ask_gate.py` (existing Stop hook) also blocks a final message that says a behaviour
   change shipped / is live / deployed / merged when the newest commit touching a behaviour path has no
   `Approval-ID:` trailer matching an approval record.

## Effect on trading and work
No change to the bot's trading code or behaviour. Every change to behaviour paths (features AND bug fixes) waits for the
owner's `APPROVE <ID>`. Reports, logs, analysis, docs and tests outside those paths are unaffected.

## Known limits (honest)
These hooks run on this machine under the same user as the agent; they stop the agent's tool calls, which is the
channel every one of today's breaches used. They are not a defence against a person editing files outside the harness.
Pattern-based Bash write detection can miss an obfuscated write; the ship-time mirror (rule 2, preship_gate) is the
backstop for anything that reaches a commit.

## Review round 1 and final build (2026-10-17)
Board (Schneier seat), Gro and GAI: all APPROVE-WITH-CHANGES; all said local hooks the agent can edit are a speed bump.
Built in response:
- **Managed (system-level) hooks** — Claude Code reads `/Library/Application Support/ClaudeCode/managed-settings.json`
  (root-owned); with `allowManagedHooksOnly` project/user settings cannot add or remove hooks (verified in the Claude
  Code docs, 2026-10-17). `scripts/install_managed_gates.sh` (owner runs once with sudo) installs a root-owned launcher
  (`run_gate.py`, run with the root-owned /usr/bin/python3) plus a manifest of every gate file's sha256. Each hook call
  runs the repo's gate ONLY if every gate file still matches the manifest; an edited gate blocks every gated action until
  the owner reinstalls. Until the owner installs it, the same hooks run from the project settings (editable layer).
- Approval gate matcher widened to every tool (MCP edit/refactor tools, NotebookEdit); behaviour scope reuses
  preship_gate's scope (+ its accounting exclusions, gate files, settings.local.json); `.claude/approvals/` is never
  writable by agent tools; an approval must match a USER entry in the transcript by hash (a forged record is refused);
  fail-closed on internal error for write-capable calls.
- Ship-time mirror in preship_gate: every behaviour/gate path in a commit or push (deletions included) must be covered.
- Bias: instead of chasing every route, the ONLY allowed route to Groq/Gemini is `.claude/preship/bgg_call.py` (scans the
  prompt actually sent: leading language, paper frame, the three questions) or `preship_audit.py`; any other command or
  script that reaches those APIs is blocked.
- Git override class blocked: core.hooksPath, --no-verify, commit -n (clustered too), GIT_CONFIG_*/GIT_DIR, git config hooks.
- Existing-work check in record_design.py: prints the related records and refuses until each is cited (it refused this
  very design until four records were read and cited).
- Dropped: the Stop-message check (#5) — all three reviewers: wording-based, easy to avoid; the ship-time mirror replaces it.
Not built (reason): server-side GitHub wall (owner declined); emergency bypass (reviewers suggested; owner decision —
default is none: every behaviour change waits for APPROVE, which he can send from his phone).
Limits: the managed layer covers Claude Code on this Mac only — not cloud sessions or Codex; root-level actions by a
person are outside its scope.

## Review round 2 (combined cold + adversarial, FAIL) — fixed (2026-10-17)
The reviewer proved command-text checks cannot be complete (script written in /tmp then run; `cd` + bare name;
`git apply`; `find -exec`; `$PWD`), found approvals could be backed by ANY earlier owner message, and found false blocks
(`2>&1` in server log reads; `--no-verify` inside a commit message; pushes after merging main).
- NEW `behaviour_integrity.py` (PostToolUse, every tool): checks the RESULT — after any tool call, any behaviour/gate
  file that differs from HEAD without a valid approval is restored from HEAD (new files moved to .claude/quarantine/,
  never deleted), the agent is told, and the owner is paged on Slack. Route-independent.
- An approval is valid only if the owner's transcript message with the recorded hash itself says `APPROVE <that ID>`;
  covers are re-read from the design record, not from the writable approval file.
- Scope adds bare directory names, `scripts/managed_gates/`, the install script, `auto_deploy.sh`, `CLAUDE.md`;
  `$PWD`/`$(pwd)`/`$CLAUDE_PROJECT_DIR` are expanded; writes to transcripts (~/.claude/projects) are blocked.
- False blocks fixed: `2>&1`; commit-message text and heredoc bodies are not scanned for git overrides; the push mirror
  uses `origin/main...HEAD` (the branch's own changes).
- Launcher detects the repo by its git remote as well as the path.
ROLLOUT ORDER: the PostToolUse integrity hook is registered only AFTER the owner's APPROVE GATES-1017 (registered
earlier, it would undo this build itself, which changes gate files).

## Review round 3 (combined, FAIL) — fixed (2026-10-17)
- Covers could be widened after approval by editing the design record: the approval now stores a fingerprint of its
  Covers list and is void if the list changes; an approved design record is itself protected (edits are undone); two
  records declaring the same Approval-ID make the approval impossible.
- Edit + commit + push inside one shell command: real git `pre-commit` / `pre-push` hooks (owner_approval_githook.py),
  which git runs at the moment of the commit/push; behaviour_integrity.py re-installs them after every tool call if
  they are missing or modified. They live in the repository's shared .git/hooks, so they apply to EVERY checkout of
  this repo on this Mac — including Codex's and Rafael's own manual commits of behaviour code.
- `.claude/settings.local.json` (git-ignored): hook-disabling keys (`hooks`, `disableAllHooks`, `allowManagedHooksOnly`)
  are stripped after every tool call.
- Merges/rebases in progress are left alone; the ship-time checks judge their result.
- `APPROVE`/`REJECT` must start a line ("do NOT APPROVE X" does not count). Integrity check is warn-only on its own
  internal error; approvals are read only when a behaviour file actually changed.

## Review round 4 (combined, FAIL) — fixed (2026-10-17)
- The shared git hook stub skips (exit 0) in a checkout that does not have owner_approval_githook.py (older branches,
  main before this merges) instead of blocking every commit there.
- Undoing a change never loses work: the current version is copied to .claude/quarantine/ before the file is restored
  from HEAD. The "approved design record is itself protected" rule was removed (it destroyed approved work when the
  record was untracked); a changed Covers list still voids the approval via its fingerprint.
- pre-commit stands down while a merge/cherry-pick/revert/rebase is completing (main's code is not the branch's
  change); pre-push still checks the branch's own changes (origin/main...<sha>).

## Owner correction (2026-10-17): the required questions
The first version asked only three questions I made up. The owner: the questions are already defined in the md files,
OCI, Master Brain and logs (e.g. "is this dynamic or static", "is this siloed or can it be connected"). Collected from
CLAUDE.md and the owner-rule memories into `.claude/preship/required_questions.json` (RQ-2, 25 questions after the review added five more owner rules, each with its
source). Enforced: bgg_call.py (refuses a design prompt without the block; --add-questions appends it), the Agent bias
gate (board/design prompts must carry the block), record_design.py and owner_approval_capture.py (a design record must
answer every question before it can be registered or approved). Master Brain could not be queried (login expired).

## Owner correction 2 (2026-10-17): Master Brain + hedge-fund questions (RQ-3)
The owner asked that the Master Brain be checked and that the questions an expert hedge fund / algo firm asks when
building a beta model be added. The Master Brain (notebook 0203f312, queried in the signed-in Chrome tab 2026-10-17)
returned the Feature Design Protocol items, the Self-QA checks, the anti-silo and forward-pass questions, the Alignment
Gate headings (dogma / value rank / dynamism) and the MODE-2 whitespace questions. Those not already covered were added
as Q26-Q30. Q31-Q39 are the hedge-fund beta-model questions (out-of-sample and deflated Sharpe, look-ahead/survivorship,
costs, alpha vs beta, regimes and tails, capacity, statistical power, decay and replacement, sizing tied to measured
edge). required_questions.json is now RQ-3 with 39 questions; this record answers all of them.

## Owner correction 3 (2026-10-17): natural approval wording
The owner: "if I say 'approve gates 1017' or 'approve 1017' that needs to be sufficient." A first loose version (any
'approve' followed by an ID within four words) FAILED cold review: it counted 'don’t approve 1017' (curly apostrophe),
'I would not, under any circumstances, approve 1017', 'Gro approved 1017', 'if I approve 1017, what changes' and a
pasted 'reply "approve gates 1017"' as approvals, and let the agent pick IDs like OPTION-2 that a routine reply names.
Final rule in `ids_in(prompt, root)`: besides the strict 'APPROVE <ID>', a decision counts only when it is its own
sentence — 'approve'/'approved'/'reject'/'rejected' first (any case), then only the ID's words ('gates 1017', '1017',
'gates-1017'), then the end of the sentence. It must name exactly one declared Approval-ID, and only IDs ending in
3+ digits (e.g. GATES-1017) can be named this way. Nothing counts on a line with quotes, a quoted ('>') line, or
when a '?' follows. Natural wording can name an ID only when the ID's number is the MMDD date in its own design record's file name and the record is at most 14 days old (e.g. GATES-1017 in owner_approval_gates_2026-10-17.md), whether the owner types 'approve 1017' or 'approve gates 1017'; any other ID (PR-689, CLEANUP-2026) needs the strict 'APPROVE <ID>' line, because the agent chooses ID numbers (round-2/3 reviews: 'approve 689' and 'approve PR 689' named an agent-declared PR-689). Ambiguity is decided over ALL declared IDs before the date check, so an ineligible rival ending in the same number makes the wording record nothing (round-3 review). CRLF line ends and non-breaking spaces are accepted. The gate re-parses the transcript entry with the same function, so the owner's typed message
stays the only source of an approval. Every failing input from both review rounds is a test (44 tests).

## Owner correction 4 (2026-10-17): a PR number must approve
The owner: "Approve 689, approve pr 689 should absolutely be acceptable by me as CEO." The round-3/4 date rule (a
natural ID had to carry its own recent MMDD date) is REMOVED. Natural wording now names any declared ID ending in 3+
digits ('approve 689', 'Approve PR 689.', 'approve gates 1017') when exactly one declared ID matches; two designs
ending in the same number record nothing until a word is added ('approve pr 689'). The risk the reviewers found (the
agent declaring an ID whose number the owner might type for another reason) is answered with visibility rather than
restriction: every recorded approval pages the owner on Slack with the design's ID, record and the exact paths it
now allows, and one reply ('reject 689') removes it. All sentence rules (own sentence, no quotes, no '?', no
negation, no reported past tense) are unchanged.
Review (FAIL, fixed): the Slack notice could not be delivered from the Mac (hooks do not inherit .env, and
alerts.send_slack is a silent no-op without a webhook). Now the hook loads SLACK_WEBHOOK_URL from the project .env,
sends with send_slack_delivery (confirmed delivery), and if delivery fails tells the agent to tell the owner in chat.
The notice says the undo is typed in Claude Code, not a Slack reply. A line after one ending in ':' ('Gro:\napproved
689') and lines after 'e.g.'/'i.e.' do not count. A second review FAILED an env-var test seam that could silence a
real notice; it was removed (tests stub _page_owner in-process). The gate now also refuses agent writes to the
user-level ~/.claude/settings*.json (which can set a hook's environment or disable hooks). A third review FAILED the
path check on odd spellings ('//', '/./', '../', relative) and shell routes. Final: the Edit/Write check compares
RESOLVED paths against the user-level files and the main checkout's .claude/settings*.json (found from git's common
dir); shell routes cannot all be caught from command text, so behaviour_integrity hashes those four files after
every tool call and pages the owner on any change by any route (not undone — the owner edits them too). The hooks'
Slack pages now load the webhook from .env (both hooks). .claude/quarantine/ (which holds the settings baseline) is
write-protected like .claude/approvals/. A fourth review FAILED the watch: an undelivered page was lost (baseline
advanced first; exit 0 hides stderr) and a deleted/corrupted baseline silently re-armed. Final: the baseline advances
only after the Slack page is confirmed; otherwise the hook exits 2 telling the agent to tell the owner in chat, and
repeats every tool call until delivered. A missing or unreadable baseline, or a watch error, is itself reported.
Same-user limit (documented, not fixable here): a script that rewrites the baseline with the new hashes cannot be
detected by a file the agent's user can write — the root-owned managed settings (install_managed_gates.sh) are the
wall for hook removal. 49 tests.

## Owner correction 5 (2026-10-17): no extra alerts
The owner: "Not necessary. We haven't had any issues with this. You're creating unnecessary processes and alerts."
REMOVED: the Slack notice on every recorded approval, and the settings watch (hashing Claude Code settings after
every tool call and paging on change). KEPT: natural approval wording incl. PR numbers (correction 4), and the silent
Edit/Write block on user-level and main-checkout Claude Code settings by resolved path. The undo page in
behaviour_integrity (pre-existing) now reads the webhook from .env so it actually reaches Slack. 46 tests.

## Owner correction 6 (2026-10-17): Codex approvals
The owner approved the F6 trim design from Codex, but the validator only understood Claude Code's JSONL shape, so the
shared pre-push hook rejected an authentic owner approval. `approval_valid` now accepts Codex `response_item` records
only when the payload is a user message, the retained source is complete and identifies the user, the retained message
and turn IDs exactly match the payload, and the exact text hash approves the same declared ID. Assistant messages, tool
echoes, incomplete records, identity mismatches, and user-shaped records without a retained user source remain invalid.
This changes no trading behavior; it lets the existing owner-only control work from both development clients.
