# BGC gate — Codex replaces Gro as reviewer #2 (Board + GAI + Codex), plus the Gemini model-list fix — 2026-10-25 (signed: Claude)

Approval-ID: REVIEWER-SWAP-1025
Covers: gai_client.py, .claude/preship/preship_audit.py, .github/scripts/ci_audit.py, .claude/preship/gai_health.py, .claude/preship/test_ladder_parity.py, .claude/preship/test_reviewer_swap.py, .github/workflows/preship-verify.yml, .claude/preship/bgg_call.py, .claude/preship/owner_approval_capture.py, .claude/preship/test_static_facts.py, CLAUDE.md

Owner direction 2026-10-25: "I'm fine with transitioning Codex to #2 behind Gro and using Gro as the back up if usage
limits are hit", then "build and ship codex as the number two in the new BGC (board, GAI, Codex)" and "Let's do the
Gemini model fix".

## Problem (measured)
1. Reviewer #2 (Gro, Groq gpt-oss-120b) costs counter-prompt rounds and cannot review large diffs. BAKEOFF-1025, same 10
   cases, frame and review standard (`logs/bakeoff/results_*.json`):
   - Codex (owner's ChatGPT plan, read-only): 7/8 planted bugs caught, 1/12 false rejects, about 9 s.
   - Gro, high effort: 4-5/12 false rejects (it invents premises).
   - Gro, low effort (today's setting): 5/8 bugs, 0/12 false rejects.
   On 2026-10-25 Gro could not review the Swing build at all: 18,552 tokens requested against Groq's 8,000-token
   per-minute free cap, chunking returned INDETERMINATE, and the NVIDIA substitute returned empty
   (`pa_sbm7.txt`), so it shipped on the owner's 2026-07-07 waive rule.
2. Gemini model list: gemini-3.6-flash answered (3/4 bugs, 0/6 false rejects) while every model in the gate's list
   returned 429/503, so the gate reported "Gemini down". The list is copied in three places (shared client, local gate,
   CI gate) and the copies could drift.

## Design
1. **Reviewer #2 = Codex** in `preship_audit.py`: `codex exec --sandbox read-only --skip-git-repo-check`, started in an
   empty temporary directory with a minimal environment (PATH/HOME/locale/CODEX_HOME only — none of the `.env` keys),
   prompt on stdin. The read-only sandbox blocks writes, not reads: Codex can still read a file by absolute path, so
   every `.env` value is masked in its output before it is printed or logged. Same frame, prompt head, context and
   evidence blocks as GAI. A missing verdict line gets one format-reminder retry, and an undefined-name reject gets
   the existing automatic counter-prompt, as for GAI.
2. **Fallback = Gro (low effort, unchanged)** only when Codex is unavailable: not installed (CI, the server), usage
   limit, timeout (600 s), non-zero exit or empty output. Gro's existing chunking and NVIDIA substitute stay behind
   it, and `--waive-gro` still waives reviewer #2.
3. **No reviewer shopping.** Within a run, once Codex has returned a verdict a later Codex failure keeps it. Across
   runs, every reviewer-#2 verdict is appended to `reviewer-verdicts.jsonl` in the git common directory (shared by
   every worktree); while Codex's latest verdict on a file is REJECT, the Gro fallback is refused for that file (any
   content) until Codex itself approves a later version.
4. **Codex-signed files need Codex AND Gro.** If any commit on the file since `origin/main` carries a Codex signature
   (every form in this repo's history: "(OpenAI Codex)", "Signed: OpenAI Codex", "Signed-off-by: ChatGPT/Codex",
   "Agent-Signature: OpenAI Codex"), or the history cannot be read, Codex reviews and Gro must also approve. Both
   agents commit under the owner's name, so the message text is the only signal; a forged signature only adds a
   reviewer. Limit: uncommitted Codex edits cannot be detected.
5. **Marker:** the ship gate keeps reading the `gro` field (`preship_gate._marker_ok`), which now holds reviewer #2's
   verdict; a new `reviewer_b` field records who gave it (codex / gro / codex+gro / NVIDIA_<model> / waived). PASS
   lines print the reviewer's name.
6. **Approval safety, two layers.** (a) Channel: approval capture skips any Codex session whose first line says
   `originator: codex_exec` (all 50 bake-off sessions did; the owner's Codex Desktop sessions say "Codex Desktop" and
   are unaffected). (b) Content: every review prompt starts with `[AUTOMATED-REVIEW-PROMPT: ...]`, which
   `owner_approval_capture._NOT_HUMAN` rejects anywhere in a message.
7. **Design reviews (`bgg_call.py`):** reviewer #2 = Codex with the same fallback; `--gro-only` becomes
   reviewer-#2-only. Design reviews are advisory (they write no marker), so the per-file rules in 3-4 do not apply.
8. **CI** stays Gemini, three samples, two APPROVEs to merge (no ChatGPT login on GitHub runners). It now also runs
   `test_reviewer_swap.py`, `test_ladder_parity.py` and `test_owner_approval.py`.
9. **Gemini model fix:** add gemini-3.6-flash (2nd) and gemini-3.5-flash (4th) to all four copies (shared client,
   local gate, CI gate, CI canary `gai_health.CANDIDATE_MODELS`); `test_ladder_parity.py` fails if any copy differs.
   Shipped as its own commit so either change can be reverted alone.
10. **CLAUDE.md:** the Gro/GAI protocol section gains a short "BGC" paragraph describing the above; no other rule changes.
11. **Known gap (follow-up):** the approval gate's launch check reads the Bash command text only, so a Python script
    could start `codex exec` itself; `preship_audit.py` and `bgg_call.py` should become the only allowed callers.

Board review (cold seats, 2026-10-25): Beck (QA) and Schneier (security) APPROVE-WITH-CHANGES; every change is in
items 1-9 above. Cold second review FAIL on the Codex-signature patterns, fixed in item 4 and tested on real history.

## Required questions
Q1: Moves the goal by cutting gate time and false-reject rounds on every ship (bake-off: Codex 1/12 false rejects vs Gro high 4-5/12), so more validated changes reach the paper account per session. Not conservative.
Q2: No change to trades taken; gate tooling only.
Q3: Yes: the owner asked for it. Simpler alternatives (keep Gro low) cannot review diffs above 8,000 tokens. The risk (a review prompt counted as an approval) is closed by the sentinel, not by restricting Codex.
Q4: BAKEOFF-1025 (measurement, `logs/design_records/reviewer_bakeoff_2026-10-25.md`); its tool and wording-parser work ship separately. No open PR touches these files (`gh pr list` checked 2026-10-25). Codex owns QHM work, not the gate.
Q5: The 600 s Codex timeout is plumbing (a timeout name). No trading thresholds.
Q6: Static assumption exposed: one fixed second reviewer. Forward pass: score each reviewer's live false-reject and catch rate per ship from the markers and re-rank automatically.
Q7: Connects to preship markers, CI and design reviews only. Each fails safe to Gro / the existing substitute and is unit-tested.
Q8: Bake-off numbers from `logs/bakeoff/results_*.json`; Gro 8k-cap failure from the 2026-10-25 preship output; Gemini 3.6-flash result from `results_20261026T004759Z.json`.
Q9: Not applicable because this is gate tooling with no trading replay; the bake-off's 10 cases are its front-loaded test. Reversal: Codex false-rejects more than 2 of the next 10 real ships, or misses a bug a later reviewer finds.
Q10: Not applicable because no trading edge is involved.
Q11: No size, frequency or concurrency change.
Q12: No safety-envelope change; the gate stays two external approvals plus the cold second review.
Q13: Codex missing / limited / timed out -> Gro; Gro down -> existing NVIDIA substitute; everything down -> fail closed, no marker.
Q14: No shadow; live on merge.
Q15: The marker and the PASS/FAIL line record which reviewer gave each verdict and why a fallback happened (stderr).
Q16: Proof it ran: the next preship PASS line reads `codex=APPROVE` and the marker has `reviewer_b: codex`. Post-live: the first 10 ships are checked for reviewer and false-reject count.
Q17: Run preship_audit on a real staged file after merge and check the marker.
Q18: The Gro waive on 2026-10-25 is treated as a design failure, not "by design".
Q19: No entry, exit, sizing or routing change.
Q20: Prompts unchanged except the reviewer; facts only. Bake-off result files are the evidence.
Q21: No broker calls. Tests mock subprocess; no Codex or network call in tests.
Q22: Not applicable because this is not a trading control; it is checked on a real staged file before being called live.
Q23: Deployed is not called ready until a real preship run shows `reviewer_b: codex`.
Q24: No absence claims about markets.
Q25: Not applicable because no stock or market statistics are quoted; the only numbers are the bake-off results.
Q26: Not applicable because no market data is used.
Q27: Writes only the existing marker JSON under `.claude/preship/markers/` (same write path as today).
Q28: Runs inside preship_audit after GAI approves, before the marker write; never in the trading process.
Q29: Highest-value gate item: every ship this week waits on reviewer #2.
Q30: Whitespace: no per-reviewer accuracy tracking (the Q6 forward pass). Blind spot: the same Codex that builds could review its own uncommitted work (documented limit).
Q31: Not applicable because this is gate tooling with no trading replay; the bake-off ran 3 settings x 10 cases.
Q32: Not applicable because no trading replay.
Q33: Not applicable because no trading replay.
Q34: Not applicable because no trading replay.
Q35: Not applicable because no trading replay.
Q36: Not applicable because no trading replay.
Q37: 10 cases is small; the reversal criterion (Q9) runs on live ships.
Q38: Non-working = the Q9 reversal criterion; then revert to Gro low.
Q39: Not applicable because no sizing.
Q40: Protects: a review verdict always comes from a working second reviewer, and no review prompt can count as an approval. Check: `test_reviewer_swap.py` and `test_ladder_parity.py` in CI.
Q41: An expert would also track reviewer accuracy over time (Q6), queued as the forward pass.
Q42: Raw data: the bake-off result files and the markers.
Q43: Professional method: measured reviewer selection with an automatic fallback and recorded provenance; this design uses it.
Q44: Independent evidence: the CI Gemini audit of this diff and the cold second review; the post-live check of the first 10 ships.
