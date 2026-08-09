#!/usr/bin/env python3
"""
execute_dont_ask_gate.py — a Stop hook that ENFORCES the "no menu /
execute-don't-ask / bring-a-recommendation" doctrine, because documentation
alone repeatedly failed to.

Rules being enforced (all documented, all previously violated in-conversation
with no mechanism to catch it — see CLAUDE.md DOCUMENTATION-IS-NOT-ENFORCEMENT):
  - user_preferences: "When asked 'what's next', identify the highest-priority
    open item and execute immediately. Presenting a menu is a rule violation."
  - feedback_execute_dont_ask: once a problem is identified, execute in
    board-priority order; never ask which item to tackle.
  - feedback_board_rec_with_questions / BRING BOARD REC: never a bare
    question/menu — bring a recommendation.

MECHANISM: a Stop hook fires when the main agent finishes a turn. Exiting 2
(with a reason on stderr) BLOCKS the stop, so the agent must continue — it
cannot end a turn on a bare menu/which-do-you-want question. It fires ONCE per
turn (via stop_hook_active) so it can never infinite-loop; the single block is
enough to force a redo as a recommendation + execution.

It deliberately does NOT block a legitimate single decision that carries a
recommendation the agent is proceeding on, nor an irreversible/authority-gated
approval ask — only the "pick-which-work-item / A-or-B menu with no rec you're
executing" pattern.
"""
import json
import os
import re
import sys


def _last_assistant_text(transcript_path: str) -> str:
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
            continue  # a valid-JSON non-dict line (number/string/null) has no .get()
        if ev.get("type") == "assistant" or ev.get("role") == "assistant":
            msg = ev.get("message", ev)
            if not isinstance(msg, dict):
                continue  # "message" may be a bare string in some transcript shapes
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


# A MENU / "which-work-item do you want me to do" ending — the violation.
_MENU = re.compile(
    r"(which (one|do you|would you|of these)"
    r"|what (do you want|would you like|should i (do|build|tackle)) (me to|next)?"
    r"|\ba or b\b|option a\b.{0,80}option b"
    r"|do you want me to .{0,80}\bor\b"
    r"|should i .{0,60}\bor\b .{0,60}\?"
    r"|would you (rather|prefer)\b"
    r"|\byour call\b|let me know which|pick (one|which)"
    r"|do you want (me to )?(build|start|do|ship|tackle|pivot|prioriti)"
    r"|which do you want)",
    re.I,
)
# Markers that the agent is RECOMMENDING + PROCEEDING (allowed even if a "?").
_PROCEED = re.compile(
    r"(i'?ll proceed|i'?m proceeding|proceeding with"
    r"|i'?ll (build|start|ship|do|execute|take|draft|run)"
    r"|executing (now|it)|starting (now|on)|unless you (say|tell)"
    r"|i'?m going to (build|start|ship|do)"
    r"|i recommend .{0,160}(so i'?ll|proceeding|executing"
    r"|i'?m (going|starting)|i'?ll))",
    re.I,
)
_Q_CHOICE = re.compile(r"(do you want|should i|which|prefer|your call)", re.I)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # cannot parse input → never block
    if data.get("stop_hook_active"):
        return 0  # already fired once this turn → do not loop
    last = _last_assistant_text(data.get("transcript_path", ""))
    if not last:
        return 0
    tail = last[-1000:]
    has_menu = bool(_MENU.search(tail))
    ends_q = tail.rstrip().endswith("?")
    q_choice = bool(_Q_CHOICE.search(tail))
    violating = (has_menu or (ends_q and q_choice)) and not _PROCEED.search(tail)
    if violating:
        sys.stderr.write(
            "EXECUTE-DON'T-ASK GATE (blocking): your turn ends by asking Rafael "
            "to choose the next action / a menu, without a recommendation you "
            "are proceeding on. Per user_preferences (no menu), "
            "feedback_execute_dont_ask, and BRING-BOARD-REC: pick the "
            "HIGHEST-PRIORITY open item, state your recommendation, and EXECUTE "
            "it now. Only pause for a genuinely irreversible / authority-required "
            "decision (approval to ship, a user-only design call) — and phrase "
            "THAT as a single decision WITH your board-backed recommendation, "
            "never a 'which do you want' menu. Redo the turn: proceed."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
