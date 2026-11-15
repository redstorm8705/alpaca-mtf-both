#!/usr/bin/env python3
# ruff: noqa: E501  — dense pattern strings run long (project convention)
"""
investigate_propose_gate.py — Stop hook: an investigation is never delivered as a report alone (Rafael mandate
2026-11-13).

Owner, 2026-11-13, after the NVDA CEO-exit miss was reported as "what happened" only: "Stop just auditing and diagnosing
the issue. You need to be dynamic and investigative. You need to bookmark it and revisit with a dynamic and optimized
plan to prevent something like this from happening again. This needs to be the case whenever I ask you to investigate
something. Do not ever just report what happened without a purpose solution again. Create a mechanical gate or hook to
enforce this."

RULE: when the owner's latest message asks for an investigation (did the bot catch / why did / what happened /
investigate / look into / post mortem / audit ...) OR the reply itself reports a failure or root cause, the final
reply of the turn must carry all three:
  1. a FIX — the proposed solution (a fix, design, build, change, proposal);
  2. PREVENTION — how it is stopped from happening again (prevent / never again / guard / gate / check / alarm ...);
  3. a BOOKMARK — a durable tracked reference the work returns to: an Approval-ID (WORD-WORD-1113 style), a
     logs/design_records/ path, or a GitHub issue/PR number (#123).
Missing any of them blocks the stop once per turn (stop_hook_active guard; same pattern as execute_dont_ask_gate.py),
naming what is missing, so the agent must continue and supply it. Never blocks on unreadable input.
"""
import json
import os
import re
import sys

_TAIL = 4000          # characters of the final reply inspected (a long report keeps its plan near the end)

_ASK = re.compile(
    r"(investigat|look into|dig into|did (the bot|it|we|you) (catch|miss|see|review|notice|act)"
    r"|why (did|does|is|was|were|didn'?t|isn'?t)|what happened|root[- ]cause|post[- ]?mortem|\bpm\b"
    r"|audit|diagnos|figure out (why|what)|how did (this|that|it) happen)",
    re.I,
)
_FAILURE = re.compile(
    r"(root cause|the cause (is|was)|\bmissed\b|did not (catch|act|review|see|place|fire)|didn'?t (catch|act|review"
    r"|see|fire)|was blind|went unreviewed|never (reviewed|fired|acted|caught)|failure|failed to|the bug (is|was)"
    r"|what happened)",
    re.I,
)
_FIX = re.compile(
    r"(\bfix\b|\bfixes\b|\bfixed\b|propos|design|\bbuild|\bbuilt|\bship|\bchange\b|solution|the plan|recommend)",
    re.I,
)
_PREVENT = re.compile(
    r"(prevent|never again|won'?t (happen|recur)|so (it|this) (can'?t|cannot|never)|recur|\bguard\b|\bgate\b"
    r"|\balarm\b|\bcheck\b|\bhook\b|automatic|catch (it|this) next time|every time)",
    re.I,
)
_REMINDER = re.compile(r"\A(?:\s*<system-reminder\b[^>]*>.*?</system-reminder>)+", re.S)
# user-role entries the owner did not type (cold review 11-14, round 2): sub-agent hand-backs, messages from other
# sessions, background-task notices
_NOT_OWNER = re.compile(r"SYSTEM NOTIFICATION|\AAnother Claude session sent a message|<agent-message\b"
                        r"|<task-notification\b|\[Subagent hand-back\]"
                        r"|\AI hit my usage limit while you were working")   # the app's resume note (round 4)
# a leading approval clause ("Approve investigate gate 1113") is not an ask; whatever follows it still is (round 3)
_APPROVAL = re.compile(r"\A(?:\s*(?:approve[ds]?|reject(?:ed|s)?)\b[^.!?\n,;]*[.!?\n,;]?)+", re.I)
_BOOKMARK = re.compile(r"([A-Z][A-Z0-9]+(?:-[A-Z0-9]+)*-\d{4}\b|logs/design_records/\S+|#\d{2,}\b)")


def _events(transcript_path: str) -> list:
    try:
        p = os.path.expanduser(transcript_path)
        with open(p, encoding="utf-8", errors="ignore") as fh:
            lines = fh.read().splitlines()
    except Exception:  # noqa: BLE001 — unreadable transcript never blocks
        return []
    out = []
    for ln in lines:
        try:
            ev = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _text(ev: dict) -> str:
    msg = ev.get("message", ev)
    if not isinstance(msg, dict):
        return msg if isinstance(msg, str) else ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _role(ev: dict) -> str:
    msg = ev.get("message")
    role = ev.get("type") or ev.get("role") or (msg.get("role") if isinstance(msg, dict) else "")
    return str(role or "")


def last_texts(events: list) -> tuple:
    """(last owner text, last assistant text). Owner text = the newest user event that carries typed text (tool
    results and hook/system notices are skipped: they are not the owner's words)."""
    owner, reply = "", ""
    for ev in reversed(events):
        role, txt = _role(ev), _text(ev).strip()
        if role == "user" and (ev.get("isMeta") or ev.get("isCompactSummary")):
            continue         # app-written entries (skill bodies, compaction summaries, hook feedback): not the owner
        if role == "user":   # cold review 11-14: the app can put reminder blocks ahead of the owner's typed words
            txt = _REMINDER.sub("", txt).strip()
        if not txt:
            continue
        if not reply and role == "assistant":
            reply = txt
        elif role == "user" and not owner and not txt.startswith("<") and not _NOT_OWNER.search(txt):
            owner = txt
        if owner and reply:
            break
    return owner, reply


def missing(owner: str, reply: str) -> list:
    """What the reply lacks; empty = compliant or not an investigation."""
    tail = reply[-_TAIL:]
    asked = bool(_ASK.search(_APPROVAL.sub("", owner or "")))
    if not (asked or _FAILURE.search(tail)):
        return []
    out = []
    if not _FIX.search(tail):
        out.append("a FIX (the proposed solution)")
    if not _PREVENT.search(tail):
        out.append("PREVENTION (how it is stopped from happening again)")
    if not _BOOKMARK.search(tail):
        out.append("a BOOKMARK (Approval-ID, logs/design_records/ path, or #issue to revisit)")
    return out


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(data, dict) or data.get("stop_hook_active"):
        return 0
    owner, reply = last_texts(_events(data.get("transcript_path", "")))
    if not reply:
        return 0
    miss = missing(owner, reply)
    if miss:
        sys.stderr.write(
            "INVESTIGATE->PROPOSE GATE (blocking, owner mandate 2026-11-13): this reply reports an investigation or "
            "a failure without " + "; ".join(miss) + ". Never report only what happened. Add the dynamic fix, how "
            "it is prevented next time, and the tracked item (design record / Approval-ID / issue) you are "
            "returning to — then continue the work.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
