# Design Record — Self-QA Gate #4: "BGG design-record"

**Feature slug:** `self_qa_gate4`
**Status:** Layer 1 (local PreToolUse hook) — APPROVED FOR BUILD, Rafael 2026-08-12 07:38 PT.
Layer 2 (GitHub branch-protection status check) — DEFERRED, needs Rafael's own GitHub admin
action, not built this pass.

---

## 1. What problem this closes

Self-QA gate checks #1 (BGG prompt-bias) and #2 (adversarial claims review) are enforced.
Check #3 (log-evidence) is enforced. Check #4 — "am I thinking ahead? What does BGG say?" — a
required BGG design pass (Open Question Protocol) BEFORE a new feature/fork's code is written —
was still mandatory-manual: a rule in CLAUDE.md, not a mechanism. Per this project's own
DOCUMENTATION-IS-NOT-ENFORCEMENT rule, a rule that has been stated and can still be silently
skipped is a request, not a control.

## 2. Open Question Protocol run (2026-08-11 AWP session, same-day, re-affirmed unchanged today)

Ran Feature Design Protocol + Open Question Protocol across Gro, GAI, and two cold board seats
(Beck/Kim — QA-process lens; Peterffy — infra lens), neutral prompts (the bias gate — PR #124 —
was itself active and would have blocked anything else).

| Q | Question | Consensus |
|---|---|---|
| Q1 | What triggers the gate? | **4/4 converge:** independent reviewer/cold-agent classifies "new capability vs. fix," default-to-gated on ambiguity — mirrors the existing risk-path routing screen (Rule B). Self-declaration is an input the reviewer can overrule, never sufficient alone. |
| Q2 | How do you prove design came BEFORE code? | **Majority (GAI + both board seats) over Gro:** local git-ancestry alone is a known gaming vector (independently found by GAI via stash-then-backdate and by the Peterffy seat via `rebase -i`) — insufficient alone. Two-layer design: (1) a PreToolUse hook blocking writes to implementation paths until a matching design-record already exists in the working tree (fast, catches accidental violations) + (2) a required GitHub branch-protection status check verifying the design-record commit's *server-received* timestamp precedes the implementing commit's (the actual forgery-resistant wall — same two-layer pattern this project already uses for `preship_gate.py`: "hook = speed bump, branch protection = the real wall"). |
| Q3 | What should the marker contain? | **Majority (3/4) — hash/reference** to a design-record file (`logs/design_records/<feature>.md`), not the verbatim payload inline (avoids transcript-duplication/drift, mirrors the existing cold-2nd/adversarial markers). Peterffy-seat's addition, adopted as genuinely new: an explicit waiver escape valve requiring **Rafael's own sign-off** (not any Claude session) for when the reviewer misclassifies a small change as "new capability" — without an escape valve this becomes exactly the kind of friction that gets `--waive`d into meaninglessness. |

Split disclosed: Gro's Q2 position (ancestry alone) is the minority view — 3 of 4 voices
independently converged on "insufficient," two arriving at the same gaming vector from different
angles without seeing each other's answer. Clean majority per the Gro/GAI Tie-Breaker Protocol,
not escalated as an unresolved split.

## 3. Scope decided TODAY (2026-08-12, Rafael: "let's start with the self qa gate")

Building **Layer 1 only** this pass — the local PreToolUse hook. Layer 2 (GitHub branch
protection required status check) needs a new GitHub Actions workflow AND Rafael adding it as a
required check in the repo's branch-protection settings — an account/settings change outside
what Claude may do unilaterally. Flagged as a separate follow-on, its own go-ahead.

## 4. Layer 1 mechanism — exact spec

**Trigger (the mechanical proxy for "new capability vs. fix," honestly scoped — see §6):** a
`Write` tool call targeting a path that does **not yet exist at git HEAD** (`git cat-file -e
HEAD:<path>` fails), whose extension is `.py` or `.sh`, and which is "gated" per
`preship_gate._is_gated()` — the SAME source of truth already used to decide which files need a
Gro/GAI ship marker, imported directly so the two gates cannot drift apart.

**What satisfies the gate:** a design-record marker
(`.claude/preship/markers/design__<slug>.json`, written by `record_design.py`) whose `covers`
list includes the target path (exact match or a directory-prefix entry ending in `/`), AND whose
recorded `doc_sha256` still matches the CURRENT on-disk content of the referenced design-record
file — an edited-away design doc does not keep validating stale coverage.

**Escape valve:** `record_design_waiver.py <path> --reason "<why this isn't new capability>"` —
mirrors `record_exemption.py`'s pattern exactly (an explicit, git-committed attestation, never a
silent skip). Per Q3's consensus this requires **Rafael's own sign-off**: Claude does not
self-invoke this script without his explicit go-ahead in the current chat message, the same
procedural bar as every other "explicit permission required" action — the git-based trust model
here has no cryptographic way to verify who ran a command, so the enforcement is procedural
(same honest limitation every other marker script in this repo already has).

**Fail-safe direction:** on any INTERNAL error (git call fails, marker unreadable, unexpected
exception) the hook **fails OPEN** (allows the write), not closed. Blocking every file write in
the project on an internal bug is a worse outcome than the hole it closes — same reasoning
`preship_gate.py` itself documents for its heredoc-stripping and rsync-scope choices. The
BLOCKING path only fires via the specific, tested "new + gated + no marker + no waiver" branch,
never via a bare exception fallthrough.

## 5. Bootstrap

This design record + its `record_design.py` marker are created and registered BEFORE the three
new implementation files (`record_design.py`, `record_design_waiver.py`,
`design_record_gate.py`) are written, so the mechanism's own birth is covered by itself —
dogfooding the exact flow a future feature would use: write the design doc → register the
marker → write the code. No special-case bootstrap exemption needed; the marker-writer scripts
themselves impose no precondition to run (same "no bootstrap problem" property `preship_gate.py`
already documents for its own marker writers).

## 6. Honest limitations (no overclaim)

- **New-file-only trigger.** A "new capability" implemented entirely as new functions inside an
  EXISTING file is not caught by this mechanical proxy. This is a real, known gap — per Q1's own
  consensus, self-declaration/mechanical proxies are inputs, never sufficient alone. Layer 2 (a
  human/CI-level check) is the intended backstop for judgment calls this proxy misses; it is not
  built yet.
- **`.py`/`.sh` extension only.** New JSON/YAML/config files are not gated by this trigger — a
  deliberate scope narrowing (most new config/state files are not "new capability," and gating
  them broadly risked exactly the over-block-teaches-bypass failure mode `record_exemption.py`'s
  own docstring warns about).
- **Content-level fabrication is not caught.** `record_design.py` verifies A design-record file
  exists and precedes the code (locally); it does not verify the file's CONTENT reflects a real
  board/Gro/GAI pass. A three-line placeholder registers exactly as well as a genuine one. This
  is the same class of gap Q2 already names (ancestry/ordering can be gamed) — the content
  dimension is a distinct, additional gap, not previously called out this precisely.
- **Procedural, not cryptographic, waiver authority.** Nothing in this hook can verify that a
  waiver was actually authorized by Rafael versus self-issued by a Claude session — the same
  limitation every existing marker script in this repo already has. The control is procedural
  discipline + git-history auditability, not a technical wall. The technical wall is Layer 2,
  deferred.
- **Local hook = speed bump, not unbypassable** (same honest framing `preship_gate.py` uses for
  itself): it lives in a directory an agent can edit. It catches the accidental/no-registered-
  design case cheaply; it does not stop a determined bypass. That is what Layer 2 is for.

## 7. Reversal criterion

If this hook produces enough false-positive friction (e.g., routine new test files/scratch
scripts repeatedly needing waivers) that it starts training bypass behavior rather than
preventing skipped design passes, that is grounds to narrow the trigger scope (e.g., exempt
`tests/`) or loosen it — a board/Gro/GAI review of the friction data, not a unilateral rollback.
Kim-lens risk flagged in review (§8): `scripts/` in particular is full of small, low-risk utility
files that will hit this gate on every new one — watch this specific directory first.

## 8. Post-design review pass (2026-08-12, same session) — 5 independent reviews, fixes applied

Per this project's own patch sequence, the BUILD (not just the design) went through its own
fresh board (Beck/Kim QA-process seat, Peterffy infra seat, Schneier security seat — all cold,
independent Explore agents) + Gro + GAI, neutral prompts. All five returned APPROVE-WITH-CHANGES,
independently converging on several real findings (not disclosed in the original Q1-Q3 table
above, since those covered the DESIGN, not the as-built code). Each is fixed in the shipped diff:

- **Bash-channel bypass (Schneier + Beck/Kim, independently found and confirmed real):** the
  PreToolUse hook only fires on the Write|Edit matcher — a new gated file created via Bash
  (heredoc, `echo >`) never reached it at all. FIXED via a ship-time mirror
  (`preship_gate._design_record_ok`, wired into the existing `_changed_gated`) — the identical
  check now runs at git commit/push time, tool-agnostically, exact parity with how checks #1-3
  already work. Not a Bash-matcher on the PreToolUse hook itself (more complex, would need the
  same heredoc/redirect parsing `preship_gate.py` already carries) — the ship-time boundary is
  the one this project already trusts for tool-agnostic enforcement.
- **Waiver deadlock (GAI, verified real against source):** the original `record_design_waiver.py`
  required a STAGED git hash — but the gate's primary use case is waiving a file that doesn't
  exist ANYWHERE yet (blocked before creation), so it could never be staged. FIXED: the sha is
  now best-effort (staged, else working-tree, else omitted) and never blocks recording the
  waiver — the gate itself never checked this hash anyway, so requiring it served no purpose.
- **Path-traversal-via-prefix-match (GAI, verified real):** `"a/../b".startswith("a/")` is True
  even though the real target resolves elsewhere. FIXED via `os.path.normpath` applied before
  any coverage matching.
- **Naive REPO-containment check (Peterffy + Beck/Kim, independently found):** a plain
  `path.startswith(REPO)` string check could false-match a sibling directory sharing a prefix.
  FIXED using the `realpath` + `commonpath` idiom `preship_gate._handle_rsync` already
  establishes for the identical question.
- **Unguarded relpath-extraction (Peterffy, verified real):** part of `main()` sat outside the
  fail-open try/except, so a malformed payload could throw uncaught. FIXED — the whole decision
  path now shares one fail-open boundary.
- **Fail-open silence (Groq + GAI):** an internal error failed open with zero operator signal.
  FIXED — every fail-open path now writes a stderr WARNING breadcrumb.
- **`logs/design_records/` was gitignored** (`logs/*` whitelist model, no negation existed for
  a new subdirectory) — the design-record `.md` itself, the actual audit-trail artifact, would
  never have reached git history. FIXED in `.gitignore` (the marker JSONs staying gitignored is
  unchanged and intentional — consistent with every other marker type in this repo).
- **No test coverage** (Peterffy + Beck/Kim, independently): the "tested" claim in this doc and
  the gate's own docstring was unbacked by any actual test file — the same documentation-vs-
  reality gap Self-QA gates #2/#3 exist to catch. FIXED: `test_design_record_gate.py` added,
  mirroring `test_gate.py`'s staged-blob discipline, 20 cases, all passing on the staged blob.
- **Non-atomic marker writes** (Peterffy): plain `open(path,"w")` risked a torn read mid-write.
  FIXED — atomic temp-file + `os.replace`, matching this project's RC-5 convention.
- **Unrestricted `--doc`** (Schneier): `record_design.py` accepted any readable path, undermining
  the "git-history auditability" backstop if it pointed at an untracked scratch file. FIXED —
  restricted to `logs/design_records/`.
- **15s git timeout** (Peterffy): too long for a hook firing on every edit; a degraded git state
  would impose a repeated 15s tax. FIXED — lowered to 5s.

**Verified NOT bugs, despite initial confident claims (Verify-Agent-Claims-At-Source discipline):**
absolute-path handling (the code already normalized repo-absolute paths; the reviewing model's
prompt just hadn't been told that detail) and a supposed "corrupted marker disables the entire
gate repo-wide" claim (the per-marker try/except already isolates this — again a prompt-
completeness gap, not a code defect). Both counter-verified against the actual source before
being dismissed, per this project's Disagreement Protocol — never a blind re-roll.
