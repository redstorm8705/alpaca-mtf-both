#!/usr/bin/env python3
# ruff: noqa: E501  — dense pattern strings run long (project convention)
"""
no_guess_gate.py — a Stop hook that mechanically enforces the NO-GUESS mandate on the agent's
CONVERSATIONAL claims, because the existing NO-GUESS enforcement only fires at COMMIT time (the
adversarial gate checks a diff's evidence) and never inspects a claim made in a chat message /
recommendation. (Rafael 2026-08-24, after "llama is capable but not identical to GAI" — a
comparative claim with ZERO test behind it — drove a real recommendation. Per
DOCUMENTATION-IS-NOT-ENFORCEMENT: build the gate, don't rewrite the rule. Full design in
logs/design_records/no_guess_gate_2026-08-24.md.)

MECHANISM: identical stdin contract to execute_dont_ask_gate.py — a Stop hook reads the agent's
turn-ending message from the transcript; exit 2 (reason on stderr) BLOCKS the stop and forces a
redo; `stop_hook_active` guarantees it fires at most once per turn (never loops). It BLOCKS when the
message makes a COMPARATIVE / CAUSAL / SUPERLATIVE claim AND the same message carries NEITHER
(a) cited evidence (measured/tested/eval/verified/profiled/traced, a file:line, an N/M score, a
results table) NOR (b) an explicit hypothesis label ([hypothesis]/unverified/"I haven't tested"/
assumption). The fix is always available in-turn: cite the evidence, or tag it a hypothesis.

FAIL-OPEN on any parse error / missing transcript / empty message — a false block is worse than a
missed catch for a speed-bump, and stop_hook_active caps it at one block per turn.

HONEST LIMIT (no overclaim, per the bias_gate precedent): pattern-based => necessary-not-sufficient.
It catches explicit comparative/causal LANGUAGE without evidence; it cannot catch a guess phrased to
dodge the patterns, and it can pass a guess whose message cites UNRELATED evidence elsewhere. The
structural backstops remain: the commit-time evidence gate, the adversarial self-review, the
cold-2nd, and Rafael.
"""
import json
import os
import re
import sys


def _last_assistant_text(transcript_path: str) -> str:
    # Byte-for-byte the same extraction execute_dont_ask_gate uses (single source of shape).
    try:
        p = os.path.expanduser(transcript_path)
        if not os.path.exists(p):
            return ""
        lines = open(p, encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        return ""
    for ln in reversed(lines):
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant" or ev.get("role") == "assistant":
            msg = ev.get("message", ev)
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", msg)
            txt = ""
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            if txt and txt.strip():
                return txt
    return ""


# A COMPARATIVE / CAUSAL / SUPERLATIVE claim — the kind that must be earned by evidence, not asserted.
_GUESS = re.compile(
    r"\b(better|worse|stronger|weaker|faster|slower|cheaper|safer|superior|inferior|"
    r"more (reliable|capable|accurate|robust|performant|thorough)|"
    r"less (reliable|capable|accurate|robust)) than\b"
    r"|\bnot as (good|capable|reliable|strong|accurate|fast)\b"
    r"|\bcompares? (un)?favorabl"
    r"|\bcapable but\b|\b(is|are|it'?s) (not )?identical\b"
    r"|\bout[- ]?performs?\b|\bedges out\b"
    r"|\bthe (root )?cause (is|was)\b|\bcaused by\b|\broot[- ]cause\b"
    r"|\bthis (fixes|causes|breaks|is what (caused|fixed|broke))\b"
    r"|\bbecause it (causes|caused|blocks|breaks|prevents|fixes|makes)\b",
    re.I,
)

# EVIDENCE present in the SAME message => the claim is earned; allow.
# NB (Gro preship 2026-08-24, valid REJECT): a bare markdown table row is NOT evidence — any
# formatting table would have let a comparative claim bypass the gate (false-negative). Removed.
# The remaining markers are specific: measurement/test/verify KEYWORDS, an N/M score, or a file:line.
_EVIDENCE = re.compile(
    r"\b(measured|tested|retest|eval(uat(ed|ion))?|verif(ied|y)|profil(ed|e|ing)|traced?|"
    r"benchmark(ed)?|confirmed (by|at|via)|observed|reproduc(ed|e)|source[- ]verified|"
    r"per the (eval|test|probe|run|output|log|profile|data|numbers)|"
    r"the (probe|eval|test|run|log|data|profile) (shows?|showed|returned|found|confirms?|confirmed))\b"
    r"|\b\d+\s*/\s*\d+\b"                       # an N/M score (e.g. 2/4)
    r"|\b[\w./-]+\.(py|sh|md|json|txt|ya?ml):\d+\b",   # a file:line citation
    re.I,
)

# An explicit HYPOTHESIS / not-yet-verified label => the claim is honestly flagged; allow.
_HYPOTHESIS = re.compile(
    r"\[hypothes|\bhypothes(is|es)\b|\bunverified\b|\bunproven\b|\bspeculative\b"
    r"|\bnot (yet )?(verified|tested|measured|confirmed|proven|validated)\b"
    r"|\bi (haven'?t|have not|did not|didn'?t) (tested|verified|measured|confirmed|run|checked)\b"
    r"|\bwould need to (test|verify|measure|check|confirm)\b"
    r"|\bassum(e|ed|es|ing|ption)\b|\bpresum(e|ed|es|ing|ption)\b"
    r"|\bmy guess\b|\bi'?m guessing\b|\bwithout (testing|verifying|measuring|evidence)\b",
    re.I,
)


def _violation(text: str):
    """Return the offending guess phrase if the message makes a comparative/causal claim with
    NEITHER cited evidence NOR a hypothesis label; else None."""
    m = _GUESS.search(text)
    if not m:
        return None
    if _EVIDENCE.search(text) or _HYPOTHESIS.search(text):
        return None
    return m.group(0).strip()


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # unparseable stdin => never block
    if not isinstance(data, dict):
        return 0  # NIT-1 (cold-2nd): valid-JSON non-dict stdin (42/null/"x") -> fail-open, not AttributeError
    if data.get("stop_hook_active"):
        return 0  # already fired once this turn => do not loop
    last = _last_assistant_text(data.get("transcript_path", ""))
    if not last:
        return 0
    hit = _violation(last)
    if hit:
        sys.stderr.write(
            "NO-GUESS GATE (blocking): your turn makes a comparative/causal/superlative claim "
            f"with NO cited evidence and NO hypothesis label: '{hit}'. Per the NO-GUESS mandate, "
            "a causal/comparative/perf claim must cite the measurement, trace, tool-result, or "
            "file:line that confirms it — gathered BEFORE the claim — OR be explicitly tagged "
            "'[hypothesis — unverified]'. Either run the check now and cite it, or label it a "
            "hypothesis and verify before acting on it. Redo the turn."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
