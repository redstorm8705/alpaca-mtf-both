#!/usr/bin/env python3
# ruff: noqa: E501
"""bgg_prompt_bias_gate.py — PreToolUse hook (Bash + Agent): BLOCK a verdict-LEADING BGG prompt on
BOTH first-mile channels — the Groq/Gemini API call (Bash) AND the cold board review (Agent) —
before it leaves the machine. Design stage and ship stage alike.

WHY THIS EXISTS (Rafael 2026-08-10, trust-rupture root cause). `preship_audit._check_prompt_bias`
only scanned the `--context`/`--evidence` flags of ONE opt-in script — the LAST mile. The biased
prompt that shipped the C1 band-aid entered at the DESIGN stage, via a hand-rolled `curl`/`python`
Groq call (and a board Agent prompt) that NO gate ever saw. A control that runs only when I choose
to invoke it is not a control. This hook fires on the TOOL CALL itself: a hand-rolled curl/python
to a BGG endpoint, or a board-review Agent prompt, cannot route around it.

WHAT IT DOES:
  * Bash: if the command targets a BGG endpoint (api.groq.com / generativelanguage.googleapis.com)
    AND contains a verdict-leading phrase -> exit 2 (block).
  * Agent: if the prompt is a REVIEW/board prompt (asks for a verdict/PASS-FAIL/APPROVE-REJECT
    /audit) AND ALSO pre-states a verdict (leading language) -> exit 2 (block). The review-context
    requirement means a plain Explore file-read prompt is never touched (no false positives), and
    asking a board seat for a verdict is legitimate — only PRE-BAKING the answer is blocked.
It reuses preship_audit._BIAS_PATTERNS so there is ONE source of truth for "leading".

HONEST SCOPE (no overclaim — the exact failure mode being fixed): this closes the CHANNEL hole on
both BGG first miles. It is pattern-based, so it is necessary-not-sufficient: it catches explicit
verdict language and verdict-SOLICITATION on a pre-baked design. It cannot catch every subtle
framing-by-presupposition; the structural backstop for THAT is the Open Question Protocol (present
the fork's options neutrally, never a single pre-chosen answer) which fork_reminder.py nudges.

FAIL-OPEN only on unparseable stdin (we see stdin solely for the matched tool). Non-BGG / non-review
prompts pass instantly.
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# Single source of truth: reuse the ship-stage detector's patterns so the two stages can't drift.
try:
    from preship_audit import _BIAS_PATTERNS as _SHIP_PATTERNS
except Exception:
    _SHIP_PATTERNS = [
        r"already approved", r"\bboard (has |already )?(approved|converged|agreed|signed[- ]off)",
        r"\b(gro|gai|board)s?\b[^.]{0,40}\bapprove", r"should (approve|pass|be approved)",
        r"please approve", r"safe to (approve|ship|merge)", r"obviously (safe|correct|fine|right)",
        r"\b(no|zero) risk\b", r"\bharmless\b", r"trivial (change|diff|fix|patch|edit)",
    ]

# Design-stage additions: verdict-SOLICITATION on a pre-baked design (what the C1 frame did — it
# told BGG the answer, then asked them to bless it, rather than presenting the open fork).
_SOLICIT_PATTERNS = [
    r"\bapprove (this|the|my) (design|change|approach|option|plan|patch|diff)",
    r"do you approve", r"please (approve|sign[- ]?off|bless|confirm)",
    r"\bsign[- ]?off on\b", r"\bbless (this|the|my)\b",
    r"confirm (this|the|my) (design|approach|is safe|is correct)",
]

_BGG_ENDPOINTS = ("api.groq.com", "generativelanguage.googleapis.com")

# An Agent prompt is a REVIEW/board prompt if it names a review action. A plain Explore file-read
# matches none of these -> never trips the gate (no false positives on non-review agent work).
_REVIEW_CONTEXT = [
    r"\bverdict\b", r"\bpass or fail\b", r"\bpass/fail\b", r"\bapprove or reject\b",
    r"\bboard (review|seat|vote)\b", r"\bcold (second|2nd)[- ]agent\b", r"\baudit\b",
    r"\breview (this|the) (diff|change|patch|code|design)\b", r"\bfind the (failure|bug)",
]

# NEUTRAL both-sided verdict OFFERS — these present BOTH outcomes, so they are NOT leading and must
# be neutralized before scanning (else "board, approve or reject" false-trips the board..approve
# pattern). A one-sided "please approve" survives; a two-sided "approve or reject" does not.
_NEUTRAL_VERDICT_OFFERS = [
    r"approve[- ]?with[- ]?changes\s+or\s+reject", r"approve\s+or\s+reject",
    r"approve\s*/\s*reject", r"reject\s+or\s+approve", r"approve\s+or\s+deny",
    r"pass\s+or\s+fail", r"pass\s*/\s*fail",
]


def find_bias(text: str, patterns) -> list:
    """Return the leading phrases in a prompt (lowercased scan) for the given pattern set. Neutral
    both-sided verdict offers are blanked first so a fair 'approve or reject' ask is not flagged."""
    low = text.lower()
    for neutral in _NEUTRAL_VERDICT_OFFERS:
        low = re.sub(neutral, " __verdict__ ", low)
    hits = []
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            hits.append(m.group(0).strip())
    return hits


def _block(where: str, hits: list) -> None:
    sys.stderr.write(
        f"BGG PROMPT BIAS GATE (blocking, {where}): this prompt contains VERDICT-LEADING language "
        f"that biases the reviewer toward APPROVE: {hits}. Per the No-Leading-Prompts rule, send "
        "BGG only FACTS (what the code does / the neutral fork options), NEVER a conclusion (that "
        "it is safe / approved / the answer you already chose). Restate neutrally and retry.\n"
    )
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # only the matched tool's stdin is visible; junk => do not block
    ti = payload.get("tool_input", {}) or {}

    # --- Channel 1: Bash -> Groq/Gemini API call ---
    cmd = ti.get("command", "") or ""
    if cmd:
        low = cmd.lower()
        if any(ep in low for ep in _BGG_ENDPOINTS):
            hits = find_bias(cmd, list(_SHIP_PATTERNS) + _SOLICIT_PATTERNS)
            if hits:
                _block("Groq/Gemini call", hits)
        sys.exit(0)

    # --- Channel 2: Agent -> cold board / reviewer prompt ---
    prompt = ti.get("prompt", "") or ""
    if prompt:
        low = prompt.lower()
        # An endorsement-request phrase ("please sign off", "approve this design") is ITSELF proof
        # this is a review/endorsement prompt — a file-read never contains one — so it counts as
        # review-context on its own (closes the "solicit with no review keyword" hole).
        solicit_hit = bool(find_bias(prompt, _SOLICIT_PATTERNS))
        is_review = solicit_hit or any(re.search(p, low) for p in _REVIEW_CONTEXT)
        if is_review:
            # A board seat legitimately RETURNS a verdict, and a neutral "give a verdict PASS/FAIL"
            # does NOT trip these patterns. What IS leading — and what we block — is pre-STATING a
            # conclusion (_SHIP_PATTERNS) OR soliciting sign-off on a pre-chosen design (_SOLICIT_
            # PATTERNS: "approve this design", "please sign off", "bless this"). The C1 failure was
            # exactly a solicitation-on-a-pre-baked-design to the board, so both sets apply here.
            hits = find_bias(prompt, list(_SHIP_PATTERNS) + _SOLICIT_PATTERNS)
            if hits:
                _block("board/reviewer prompt", hits)
    sys.exit(0)


if __name__ == "__main__":
    main()
