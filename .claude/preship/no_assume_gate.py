#!/usr/bin/env python3
# ruff: noqa: E501  — dense pattern strings run long (project convention)
"""
no_assume_gate.py — a Stop hook that mechanically blocks an ASSUMED-ABSENCE claim: asserting the
codebase/bot LACKS a capability, tool, data feed, or piece of work WITHOUT having searched for it.

WHY (Rafael 2026-08-29, DOCUMENTATION-IS-NOT-ENFORCEMENT): the agent repeatedly stated as FACT that
work did not exist — "we don't ingest order-flow/gamma" (false: data/gex.py is 905 lines of gamma
exposure), and earlier missed the edge-tracker and monitoring/watchdog the same way — each time
WITHOUT grepping the repo first. A memory note failed to stop it across multiple sessions, so per the
project's own rule the fix is a GATE, not another note. This is the sibling of no_guess_gate.py: that
one gates comparative/causal claims; this one gates existence/absence claims about the system.

MECHANISM: identical stdin/transcript contract to no_guess_gate.py and execute_dont_ask_gate.py — a
Stop hook reads the turn-ending message; exit 2 (reason on stderr) BLOCKS the stop and forces a redo;
`stop_hook_active` fires it at most once per turn. It BLOCKS when the message asserts the system LACKS
a named capability/tool/data (an ABSENCE claim about a CAPABILITY term) AND carries NEITHER
(a) SEARCH evidence (grep/find/glob/semantic-search ran, "no matches", a file:line/path cited) NOR
(b) an honest HEDGE ("I haven't searched", "let me check", "may already exist"). The fix is always
in-turn: grep the repo, then cite what you found (or found nothing).

FAIL-OPEN on any parse error / missing transcript / empty message. HONEST LIMIT (per bias_gate/
no_guess_gate precedent): pattern-based => necessary-not-sufficient; it catches explicit absence
LANGUAGE about capabilities, not an assumption phrased to dodge the patterns. The structural backstops
remain (adversarial self-review, cold-2nd, the board, Rafael).
"""
import json
import os
import re
import sys


def _last_assistant_text(transcript_path: str) -> str:
    # Same extraction shape as no_guess_gate / execute_dont_ask_gate (single source of shape).
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


# A CAPABILITY / TOOL / DATA / WORK term — the kind whose ABSENCE must be searched before asserted.
_CAP = (r"(order[- ]?flow|imbalance|tape|level[- ]?2|\bl2\b|cvd|footprint|bid[- ]?ask|microstructure|"
        r"gamma|gex|greeks?|dealer|vanna|charm|option\w*|\biv\b|implied[- ]vol\w*|0dte|open[- ]interest|"
        r"data|feed|signal|indicator|tool\w*|module|script|infra\w*|pipeline|logic|support|"
        r"track\w*|monitor\w*|ingest\w*|integration|capabilit\w*|framework|handler|backtest\w*|"
        r"harness|report\w*|ledger|metric\w*|snapshot|history|coverage|detector|engine|gate|"
        r"watchdog|telemetry|tca|scanner|multi[- ]?time\w*|timeframe|regime|analytics?|"
        r"heartbeat|sizing|scoring|confluence|kelly|hedge|attribution)")

# Absence via a system-capability VERB (negating these verbs is almost always a capability claim).
_ABSENCE_VERB = re.compile(
    r"\b(we|it|the bot|the system|this)\s+(do(es)? not|don'?t|doesn'?t|never|aren'?t|are not|isn'?t|is not|"
    r"haven'?t|have not|hasn'?t|has not|won'?t|cannot|can'?t)"
    r"(\s+(currently|yet|today|really|even))?\s+"
    r"(ingest\w*|compute\w*|track\w*|wire\w*|instrument\w*|expose\w*|collect\w*|capture\w*|"
    r"log\w*|store\w*|support\w*|implement\w*|maintain\w*|provide\w*|handle\w*|record\w*|"
    r"surface\w*|pull\w*)\b",
    re.I,
)
# Absence via existence phrasing, requiring a CAPABILITY term nearby (so "there's no rush" is ignored).
_ABSENCE_EXIST = re.compile(
    r"\b(there(?:'?s| is| are)? no|we (have no|lack|'?re lacking|are lacking)|"
    r"the bot (has no|doesn'?t have|lacks)|the system (has no|doesn'?t have|lacks))"
    r"\b[\s\w,'\"()-]{0,40}\b" + _CAP + r"\b"
    r"|\b" + _CAP + r"\b[\s\w,'\"()-]{0,30}\b(doesn'?t exist|does not exist|do not exist|don'?t exist|"
    r"isn'?t (built|implemented|wired|available|present|a thing)|"
    r"is not (built|implemented|wired|available|present)|"
    r"(was|has) never been (built|implemented|wired|added)|"
    r"hasn'?t been (built|implemented|wired|added))\b",
    re.I,
)

# SEARCH evidence in the SAME message => the absence was verified; allow.
_SEARCHED = re.compile(
    r"\b(grep\w*|ripgrep|\brg\b|glob\w*|\bfind\b|searched|search of|semantic[- ]?search|"
    r"no (match\w*|results?|hits?|files?)|(returned|found|got) (no|0|zero|nothing)|"
    r"did ?n'?t find|could ?n'?t find|couldn'?t locate|nothing (matched|found|turned up)|"
    r"after (searching|grepping|checking|looking through|scanning)|scanned the (repo|codebase|code)|"
    r"(no|zero) (such )?(file|module|function|def|reference)s?)\b"
    r"|\b[\w./-]+\.(py|sh|md|json|txt|ya?ml)(:\d+)?\b",   # a file/path citation = I looked at the tree
    re.I,
)
# Honest HEDGE / not-yet-searched label => allow (it's flagged, not asserted).
_HEDGE = re.compile(
    r"\bi (haven'?t|have not|did not|didn'?t|need to|should|will|'?ll) (search\w*|grep\w*|check\w*|look\w*|verif\w*|confirm\w*)"
    r"|\blet me (search|grep|check|look|verify|confirm|find out)"
    r"|\bmay (already )?(exist|be (built|there|present))|\bmight (already )?exist"
    r"|\bif (it|this|that|one) (already )?exists?\b"
    r"|\bnot sure (if|whether) (we|it|the bot)\b"
    r"|\bbefore (i|we) (assume|claim|conclude)\b"
    r"|\b(unverified|unchecked|assum(e|ing|ption)|presum(e|ing|ption))\b"
    r"|\bwithout (searching|checking|grepping|verifying)\b",
    re.I,
)


def _violation(text: str):
    """Return the offending absence phrase if the message asserts a capability is missing with
    NEITHER search evidence NOR a hedge; else None."""
    m = _ABSENCE_VERB.search(text) or _ABSENCE_EXIST.search(text)
    if not m:
        return None
    if _SEARCHED.search(text) or _HEDGE.search(text):
        return None
    return m.group(0).strip()[:80]


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    if data.get("stop_hook_active"):
        return 0
    last = _last_assistant_text(data.get("transcript_path", ""))
    if not last:
        return 0
    hit = _violation(last)
    if hit:
        sys.stderr.write(
            "NO-ASSUME GATE (blocking): your turn asserts the codebase/bot LACKS a capability, tool, "
            f"data, or work — '{hit}' — with NO evidence you searched for it. This is the recurring "
            "thoroughness failure (claiming order-flow/gamma/edge-tracker/watchdog didn't exist when "
            "it did). Per the NO-GUESS + full-thorough-read mandate: SEARCH FIRST (grep/find/glob/"
            "semantic_search the repo), then cite what you found — or found nothing. Do not state "
            "absence as fact from assumption. Redo the turn: search, then claim."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
