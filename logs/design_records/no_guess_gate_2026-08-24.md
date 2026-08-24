# NO-GUESS GATE — mechanical enforcement of the NO-GUESS mandate on CONVERSATIONAL claims

**Status:** DESIGN + BUILD (Rafael 2026-08-24). Rafael: *"How do we mechanically gate you from
guessing in the future? That's unacceptable."* — after I asserted "llama is capable but not
identical to GAI" with ZERO test behind it (a NO-GUESS violation), which drove a real
recommendation (the risk-path substitute guard). Per DOCUMENTATION-IS-NOT-ENFORCEMENT: the answer
is a mechanism that can REJECT the guess, not another rule.

## THE HOLE THIS CLOSES
The existing NO-GUESS enforcement (CLAUDE.md §NO-GUESS MANDATE) only fires at **commit time** — the
adversarial gate checks a *diff's* evidence for causal/perf claims (`record_evidence.py`). A
**conversational** claim in a chat message / a recommendation is never inspected — there is no hook
on the agent's prose. So "capable but not identical" sailed through un-gated. That is the gap.

## THE MECHANISM
`.claude/preship/no_guess_gate.py` — a **Stop hook** (same family as `bias_gate.py` +
`execute_dont_ask_gate.py`; identical stdin contract: reads `transcript_path`, guarded by
`stop_hook_active` so it fires ONCE per turn and can never loop; exit 2 blocks the turn-end and
forces a redo). It reads the agent's turn-ending message and:
- **Detects a GUESS**: a comparative / causal / superlative CLAIM — patterns like "better/worse/
  stronger/weaker/faster/more(reliable|capable) than", "not as good as", "compares (un)favorably",
  "capable but", "(not) identical", "the root cause is", "this fixes/causes X", "caused by".
- **ALLOWS it only if the same message carries** EITHER (a) **evidence** — measured/tested/eval/
  verified/profiled/traced, a `file:line`, a results table, or an `N/M`-style score — OR (b) an
  explicit **`[hypothesis — unverified]`** / "I haven't tested" / "assumption" label near it.
- Otherwise **exit 2** with feedback: *"You made a comparative/causal claim with no cited evidence
  and no hypothesis label: '<phrase>'. Cite a measurement/tool-result THIS turn, or tag it
  '[hypothesis — unverified]', then retry."*

This would have fired on "capable but not identical" (comparative claim, zero evidence, no label)
and forced the exact behavior Rafael had to demand manually (test it, or label it a hypothesis).

## FEATURE DESIGN PROTOCOL
1. **Data source:** the session transcript JSONL (via `transcript_path` on the Stop-hook stdin) —
   no external data.
2. **Output:** exit 0 (allow) / exit 2 + stderr feedback (block). No files written.
3. **Integration:** registered as a `Stop` hook in `.claude/settings.json` alongside the existing
   `execute_dont_ask_gate`.
4. **Failure mode:** FAIL-OPEN on any parse error / missing transcript / empty message (never block a
   turn it cannot read) — a false block is worse than a missed catch for a speed-bump control, and
   `stop_hook_active` guarantees at most one block per turn.
5. **Board vote:** not risk-path (a Self-QA agent-discipline gate; touches no trading path). GATED_SELF
   tooling → statics + cold-2nd + preship before it ships.

## HONEST LIMIT (no overclaim, per the bias-gate precedent)
Pattern-based → **necessary-not-sufficient**. It catches explicit comparative/causal *language*
without evidence; it cannot catch a guess phrased so as to dodge the patterns, nor a subtle
presupposition. It also can pass a guess if the message cites *unrelated* evidence elsewhere. It is a
first-line speed bump that makes an unsupported claim VISIBLE and forces the label — the structural
backstops remain the commit-time evidence gate, the adversarial self-review, the cold-2nd, and Rafael.
Tuning (patterns, evidence markers) will iterate against real false-block / false-pass observations.

## ALSO (commit-side extension, same session)
Extend the adversarial gate so a commit/PR asserting a COMPARATIVE claim ("X better/worse than Y")
requires a cited eval, exactly as causal/perf claims already do. (Follow-up.)
