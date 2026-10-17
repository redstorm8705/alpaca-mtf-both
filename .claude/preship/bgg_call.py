#!/usr/bin/env python3
# ruff: noqa: E501
"""bgg_call.py — the ONE allowlisted way to send a design/review prompt to Gro (Groq) and GAI (Gemini) (Claude, 2026-10-17).

Design record: logs/design_records/owner_approval_gates_2026-10-17.md. owner_approval_gate.py blocks any other route to
the Groq/Gemini APIs, so every prompt passes the scan below — on the text actually sent, not the command line.

    python3 .claude/preship/bgg_call.py <prompt_file> [--add-questions] [--gro-only | --gai-only]

Refuses (exit 2, nothing sent) when the prompt:
  * contains verdict-leading language (bgg_prompt_bias_gate's patterns — one source of truth);
  * is a design/board review without the paper-account frame (paper + the $25K goal);
  * is a design review without the owner's full required-question block (required_questions.json);
    `--add-questions` appends it.
"""
import json
import os
import re
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import bgg_prompt_bias_gate as bias  # noqa: E402

_DESIGN_REVIEW = re.compile(r"\bdesign (review|record|fork)\b|\bboard (seat|review)\b|\bproposal\b", re.I)
GRO = ("You are a Senior Staff Engineer at an HFT firm with direct ownership of execution engines and P&L attribution "
       "systems. Treat this as a P0 incident review. Be concrete and technical — no hedging.")
GAI = ("You are Head of Quant Engineering at a systematic hedge fund. Responsible for correctness of all P&L attribution, "
       "risk accounting, and counter-state invariants. Your audit is the last gate before code goes live. Find what others missed.")


def problems(prompt: str) -> list:
    out = []
    hits = bias.find_bias(prompt, list(bias._SHIP_PATTERNS) + bias._SOLICIT_PATTERNS)
    if hits:
        out.append(f"verdict-leading language: {hits}")
    import required_questions as rq
    if _DESIGN_REVIEW.search(prompt) or rq.is_design_prompt(prompt):
        low = prompt.lower()
        if not all(re.search(t, low) for t in bias._FRAME_TOKENS):
            out.append("design/board prompt lacks the paper-account frame (paper + the $25K goal)")
        import required_questions as rq
        if rq.missing_in_prompt(prompt):
            out.append("design review is missing the owner's required questions — run with --add-questions or paste "
                       "the block from required_questions.py --block")
    return out


def _env() -> None:
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path = os.path.join(root, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main() -> int:
    if len(sys.argv) < 2 or not os.path.isfile(sys.argv[1]):
        print("usage: bgg_call.py <prompt_file> [--gro-only|--gai-only]")
        return 2
    prompt = open(sys.argv[1], encoding="utf-8").read()
    import required_questions as rq
    if (_DESIGN_REVIEW.search(prompt) or rq.is_design_prompt(prompt)) and "--add-questions" in sys.argv:
        if rq.MARKER not in prompt:
            prompt = prompt.rstrip() + "\n\n" + rq.block() + "\n"
    issues = problems(prompt)
    if issues:
        sys.stderr.write("bgg_call REFUSED (nothing sent): " + "; ".join(issues) + "\n")
        return 2
    _env()
    sys.path.insert(0, os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if "--gai-only" not in sys.argv:
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps({"model": "openai/gpt-oss-120b", "max_tokens": 6000,
                             "messages": [{"role": "system", "content": GRO}, {"role": "user", "content": prompt}]}).encode(),
            headers={"Authorization": "Bearer " + os.environ.get("GROQ_API_KEY", ""), "Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0"})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=180))
            print("=== GRO ===\n" + r["choices"][0]["message"]["content"])
        except Exception as exc:
            print(f"GRO ERR {exc}")
    if "--gro-only" not in sys.argv:
        try:
            import gai_client
            print("=== GAI ===\n" + str(gai_client.call_gai(GAI + "\n\n" + prompt, os.environ.get("GEMINI_API_KEY", ""),
                                                           max_output_tokens=8192)))
        except Exception as exc:
            print(f"GAI ERR {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
