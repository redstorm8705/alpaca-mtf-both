#!/usr/bin/env python3
# ruff: noqa: E501
"""required_questions.py — the owner's required design questions (one source: required_questions.json).

    python3 .claude/preship/required_questions.py --block            # the block to paste into a board/design prompt
    python3 .claude/preship/required_questions.py --check <design.md>  # exit 1 listing unanswered questions

A design record answers them in a '## Required questions' section, one line per question: `Q5: <answer>` (an answer of
'N/A' must give a reason, e.g. 'Q11: N/A — no trading code changes'). Used by bgg_call.py, bgg_prompt_bias_gate.py,
record_design.py and owner_approval_capture.py.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MARKER = "REQUIRED QUESTIONS"


def load() -> dict:
    with open(os.path.join(HERE, "required_questions.json"), encoding="utf-8") as fh:
        return json.load(fh)


def block() -> str:
    d = load()
    lines = [f"{MARKER} ({d['version']}) — answer every one, each with evidence:"]
    lines += [f"{q['id']}. {q['q']}" for q in d["questions"]]
    return "\n".join(lines)


_DESIGN_TRIGGER = re.compile(
    r"\bboard\b[\s:\-]{0,3}(\w+[\s\-]){0,2}(seats?|reviews?|votes?)\b|\bdesign\s+(review|record|fork|pass|proposal)s?\b"
    r"|\bdecision\s+forks?\b|\b(design|strategy|change|build)\s+proposals?\b|\bpropose\s+a\s+(design|change|build|fix)\b"
    r"|\bopen question protocol\b", re.I)


def is_design_prompt(prompt: str) -> bool:
    """One trigger for every gate: a board / design / proposal prompt must carry the required questions."""
    return bool(_DESIGN_TRIGGER.search(prompt or ""))


def missing_in_prompt(prompt: str) -> list:
    """Question ids whose text is absent from a prompt (the whole block must be present)."""
    if MARKER not in prompt:
        return [q["id"] for q in load()["questions"]]
    return [q["id"] for q in load()["questions"] if q["q"][:60] not in prompt]


def unanswered(text: str) -> list:
    """Question ids not answered in a design record's '## Required questions' section."""
    m = re.search(r"(?im)^#{2,3}\s*required questions\b.*$", text)
    sec = text[m.end():] if m else ""
    sec = re.split(r"(?m)^#{1,3}\s", sec, maxsplit=1)[0]
    out = []
    for q in load()["questions"]:
        # the answer must be on the SAME line as its id ([ \t]*, never across a newline)
        m2 = re.search(r"(?m)^[ \t]*[-*]?[ \t]*\**" + re.escape(q["id"]) + r"\**[ \t]*[:.\-—][ \t]*\**[ \t]*(\S[^\n]*)$", sec)
        ans = (m2.group(1).strip() if m2 else "")
        if re.match(r"(?i)n/?a\b", ans):
            ans = re.sub(r"(?i)^n/?a\W*", "", ans)       # N/A must carry a real reason
        if not _substantive(ans):
            out.append(q["id"])
    return out


_FILLER = re.compile(r"(?i)\b(tbd|todo|tbc|see above|same as above|as above|n/?a|lorem|placeholder|xxx+)\b")


def _substantive(ans: str) -> bool:
    """At least three distinct real words (3+ letters), not filler. A word count cannot verify an answer; this only
    rejects the obvious placeholders — the owner reads the answers in the approval package."""
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", ans)]
    if len(set(words)) < 3:
        return False
    remaining = _FILLER.sub(" ", ans)
    return len(set(w.lower() for w in re.findall(r"[A-Za-z]{3,}", remaining))) >= 3


def main() -> int:
    if "--block" in sys.argv:
        print(block())
        return 0
    if "--check" in sys.argv:
        i = sys.argv.index("--check") + 1
        if i >= len(sys.argv):
            print("usage: required_questions.py --check <design.md>")
            return 2
        path = sys.argv[i]
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"cannot read {path}: {exc}")
            return 2
        miss = unanswered(text)
        if miss:
            print(f"unanswered required questions in {path}: {miss}")
            return 1
        print("all required questions answered")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
