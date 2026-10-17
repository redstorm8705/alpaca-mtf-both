#!/usr/bin/env python3
# ruff: noqa: E501
"""owner_approval_capture.py — UserPromptSubmit hook: record the OWNER's explicit approval (Claude, 2026-10-17).

Design record: logs/design_records/owner_approval_gates_2026-10-17.md (Approval-ID GATES-1017).

When the owner's typed message approves or rejects a design — `APPROVE GATES-1017`, or as he naturally writes it:
'approve gates 1017', 'Approved 1017', 'reject 1017' (resolved against the declared Approval-IDs; ignored when ambiguous,
negated or asked as a question) — this hook finds the design record declaring `Approval-ID: <ID>` under logs/design_records/, and writes
`.claude/approvals/<ID>.json` with the record's path, its `Covers:` list, the session id, the transcript path and the
sha256 of the owner's message. Only this hook writes approvals: it runs on the text the owner typed, which the agent
cannot produce. The approval gate re-checks that the message is in the transcript as a USER entry.

Never blocks the prompt (exit 0 always); prints one line the agent sees so the result is visible.
"""
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+"
_APPROVE = re.compile(r"(?m)^\s*APPROVE\s+((?:" + _ID + r")(?:\s*[,&]\s*(?:and\s+)?" + _ID + r")*)")
_REJECT = re.compile(r"(?m)^\s*REJECT\s+(" + _ID + r")")


def project_dir() -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def approvals_dir(root: str) -> str:
    return os.path.join(root, ".claude", "approvals")


def covers_sha(covers: list) -> str:
    return hashlib.sha256("\n".join(sorted(covers)).encode("utf-8")).hexdigest()


def find_design(root: str, approval_id: str) -> "tuple[str, list] | None":
    """(relative path, covers list) of the ONE design record declaring this Approval-ID, or None (none, or duplicates)."""
    found = []
    for path in sorted(glob.glob(os.path.join(root, "logs", "design_records", "*.md"))):
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        if not re.search(r"^Approval-ID:\s*" + re.escape(approval_id) + r"\s*$", text, re.M):
            continue
        m = re.search(r"^Covers:\s*(.+?)(?:\n\s*\n|\n#)", text, re.M | re.S)
        covers = [c.strip() for c in re.split(r"[,\n]", m.group(1)) if c.strip()] if m else []
        found.append((os.path.relpath(path, root), covers))
    return found[0] if len(found) == 1 else None


_NATURAL_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3,}$")   # e.g. GATES-1017: ends in 3+ digits


def known_ids(root: str) -> list:
    """Approval-IDs declared under logs/design_records/ that natural wording may name (they end in 3+ digits)."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "logs", "design_records", "*.md"))):
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        out += [i for i in re.findall(r"^Approval-ID:\s*(\S+)\s*$", text, re.M) if _NATURAL_ID.match(i)]
    return out


# A natural decision is its own sentence: 'approve(d)' or 'reject(ed)' (any case) FIRST, then only the ID's
# words, then the end of the sentence. Anything else in the sentence (a negation, 'later', 'if', a question, a quote,
# 'Gro approved ...') means it is not a decision.
_SENTENCE = re.compile(r"(?im)(?:^|(?<=[.!;\n]))[ \t\u00a0]*(approved?|reject(?:ed)?)[ \t\u00a0]+([A-Za-z0-9][A-Za-z0-9 \t\u00a0\-\u2010-\u2015]*?)[ \t\u00a0]*(?=[.!;\r\n]|$)")


def _segments(aid: str) -> list:
    return [s.lower() for s in re.split(r"-", aid) if s]


def _match_ids(words: list, known: list) -> list:
    """The one ID the words name, or [] when none or several do. Every typed word must be a segment of the ID and the
    ID's number must be among them: 'gates 1017', '1017', 'PR 689', '689' (owner 2026-10-17: a PR number must work).
    Ambiguity is decided over ALL declared IDs, so two designs ending in the same number record nothing. Because a
    number the agent chose can now approve, every recorded approval pages the owner with what it covers (see
    _page_owner), so an approval he did not mean is visible at once and 'reject <number>' undoes it."""
    words = [w.lower() for w in words]
    cands = []
    for aid in known:
        seg = _segments(aid)
        if words and seg[-1] in words and all(w in seg for w in words):
            cands.append(aid)
    if len(cands) != 1:
        return []
    return cands


def _load_webhook(root: str) -> None:
    """Hooks do not inherit the bot's .env; put SLACK_WEBHOOK_URL in the environment before alerts is imported."""
    if os.environ.get("SLACK_WEBHOOK_URL"):
        return
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8") as fh:
            for line in fh:
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == "SLACK_WEBHOOK_URL":
                    os.environ["SLACK_WEBHOOK_URL"] = v.strip().strip('"').strip("'")
                    return
    except OSError:
        return


def _page_owner(text: str) -> bool:
    """Slack the owner. Returns True only on confirmed delivery; never raises (never blocks his prompt).
    MTF_TEST_MODE=1 sends nothing and returns False, so the agent is told to tell the owner in chat."""
    if os.environ.get("MTF_TEST_MODE") == "1":
        return False
    try:
        root = project_dir()
        _load_webhook(root)
        sys.path.insert(0, root)
        from alerts import send_slack_delivery
        return bool(send_slack_delivery(text))
    except Exception as exc:
        print(f"[owner approval] Slack notice failed: {exc}")
        return False


def ids_in(prompt: str, root: "str | None" = None) -> "tuple[list, list]":
    """(approved IDs, rejected IDs) in the owner's message. The strict line-initial 'APPROVE GATES-1017' always counts.
    With `root`, a sentence that is only 'approve gates 1017' / 'Approve 1017.' / 'reject 1017' also counts, when its
    words name exactly one declared ID. Never inside quotes or a quoted ('>') line."""
    text = prompt or ""
    approved: list = []
    for m in _APPROVE.finditer(text):
        approved += re.findall(_ID, m.group(1))
    rejected = [m.group(1) for m in _REJECT.finditer(text)]
    if root:
        known = known_ids(root)
        for m in _SENTENCE.finditer(text):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.end())
            line = text[line_start:line_end if line_end != -1 else len(text)]
            if line.lstrip().startswith(">") or re.search(r"[\"\u201c\u201d\u2018`]|(?<![A-Za-z])'|'(?![A-Za-z])", line):
                continue
            before = text[:m.start()].rstrip()
            if before.endswith(":") or re.search(r"(?i)\b(e\.g|i\.e|etc|vs|cf)\.$", before):
                continue                          # 'Gro:\napproved 689' or 'e.g. approve 689' reports, not decides
            nxt = text[m.end():m.end() + 1]
            if nxt == "?" or text[m.end():].lstrip(" \t")[:1] == "?":
                continue
            words = re.split(r"[\s\u00a0\-\u2010-\u2015]+", m.group(2).strip())
            is_reject = m.group(1).lower().startswith("reject")
            hits = _match_ids([w for w in words if w], known)
            if len(hits) == 1:
                (rejected if is_reject else approved).append(hits[0])
    approved = [a for a in dict.fromkeys(approved) if a not in rejected]
    return approved, list(dict.fromkeys(rejected))


def capture(payload: dict, root: str) -> list:
    prompt = payload.get("prompt") or ""
    approved, rejected = ids_in(prompt, root)
    notes = []
    if not approved and not rejected:
        if root and re.search(r"(?i)\b(approve|reject)\b", prompt) and re.search(r"\d{3,}", prompt):
            notes.append("an approve/reject was written but it names no single design (none, or more than one "
                         "match) — nothing recorded; say e.g. 'approve gates 1017'.")
        return notes
    adir = approvals_dir(root)
    os.makedirs(adir, exist_ok=True)
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    for aid in approved:
        found = find_design(root, aid)
        if found:
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                import required_questions as rq
                gaps = rq.unanswered(open(os.path.join(root, found[0]), encoding="utf-8").read())
            except Exception as exc:
                gaps = [f"(required_questions unavailable: {exc})"]
            if gaps:
                notes.append(f"APPROVE {aid}: NOT recorded — its design record does not answer required questions {gaps}.")
                continue
        if not found:
            notes.append(f"APPROVE {aid}: no single design record declares 'Approval-ID: {aid}' (none, or more than one) "
                         "— nothing recorded.")
            continue
        rec = {"id": aid, "status": "APPROVED", "design_record": found[0], "covers": found[1],
               "covers_sha256": covers_sha(found[1]),
               "prompt_sha256": digest, "session_id": payload.get("session_id"),
               "transcript_path": payload.get("transcript_path"),
               "ts": datetime.now(timezone.utc).isoformat()}
        tmp = os.path.join(adir, f".{aid}.json.tmp")
        with open(tmp, "w") as f:
            json.dump(rec, f, indent=2)
        os.replace(tmp, os.path.join(adir, f"{aid}.json"))
        delivered = _page_owner(f":white_check_mark: Approval recorded: *{aid}* ({found[0]}) now allows changes to: "
                                f"{', '.join(found[1]) or '(nothing listed)'}. Not what you meant? In Claude Code, "
                                f"type 'reject {aid}' (a Slack reply does not undo it).")
        if not delivered:
            notes.append(f"Slack notice for {aid} was NOT delivered — tell the owner in chat what this approval covers.")
        notes.append(f"Owner approval recorded: {aid} covers {len(found[1])} path(s); covers fingerprint "
                     f"{covers_sha(found[1])[:16]}.")
    for aid in rejected:
        p = os.path.join(adir, f"{aid}.json")
        if os.path.exists(p):
            os.remove(p)
        notes.append(f"Owner REJECTED {aid}: any approval for it is removed.")
    return notes


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    try:
        for n in capture(payload, project_dir()):
            print(f"[owner approval] {n}")
    except Exception as exc:  # never block the owner's prompt
        print(f"[owner approval] capture failed: {exc}")
    sys.exit(0)


if __name__ == "__main__":
    main()
