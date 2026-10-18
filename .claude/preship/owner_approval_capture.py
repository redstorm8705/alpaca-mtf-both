#!/usr/bin/env python3
# ruff: noqa: E501
"""owner_approval_capture.py — the ONE owner-approval parser and store for every agent (Claude, Codex) and worktree.

Design record: logs/design_records/owner_approval_gates_2026-10-17.md (Approval-ID GATES-1017; "Shared approvals"
section, owner 2026-10-17). Used by the Claude UserPromptSubmit hook (this file's main), the PreToolUse gate
(owner_approval_gate.py), the git pre-commit/pre-push hook (owner_approval_githook.py, which also captures Codex
approvals automatically) and preship_gate.py.

STORE: <git common dir>/owner-approvals/<ID>.json — one store shared by the main checkout, every Claude worktree and
every Codex worktree of this repository. Legacy per-worktree .claude/approvals/*.json records are migrated in on read.

DESIGNS: every `Approval-ID:` declared in logs/design_records/*.md of ANY worktree of this repository. An ID declared
in several places with the same Covers list is one design; with different Covers lists it is ambiguous and never
approvable.

WHAT COUNTS AS THE OWNER'S APPROVAL (only a direct, authentic human user message — see transcript_user_text):
  * an explicit decision naming one design, in any capitalisation / spacing / punctuation / hyphenation:
      'APPROVE OCI-RESET-1017', 'Approved OCI reset-1017', 'approve oci reset 1017';
    a bare number ('Approve 1017') counts when it identifies exactly one PENDING (not yet approved) design;
  * a plain affirmative reply — 'approved', 'approve', 'proceed', 'go ahead', 'ship it', 'continue' (optionally with
    'yes'/'ok' and trailing punctuation) — when the IMMEDIATELY preceding agent message requested approval for exactly
    one pending design. Several pending designs or an unclear target -> nothing recorded, the possible IDs are listed.
  * 'reject <ID>' removes an approval.
Never counted: quoted text, '>' lines, pasted reports, a line after 'Gro:'/'e.g.', questions, negations ('don't approve
1017'), conditionals ('if I approve 1017'), third-party verdicts ('Gro approved 1017'), assistant text, tool output,
task notifications, sub-agent copies, incomplete Codex records, or records whose message/turn identities do not match.
"""
import functools
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+"
_APPROVE = re.compile(r"(?m)^\s*APPROVE\s+((?:" + _ID + r")(?:\s*[,&]\s*(?:and\s+)?" + _ID + r")*)")
_REJECT = re.compile(r"(?m)^\s*REJECT\s+(" + _ID + r")")
_NATURAL_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3,}$")   # e.g. GEX-1017, OCI-RESET-1017
_TOK = re.compile(r"[a-z0-9]+")
_AFFIRM = re.compile(r"(?i)^\s*(?:(?:yes|yep|ok|okay)[\s,.!]+)?(approved?|proceed|go\s+ahead|ship\s+it|continue)"
                     r"(?:[\s,]+(?:please|thanks|thank\s+you))?[\s.!]*$")
_AGENT_ASKS = re.compile(r"(?i)\bapprov")
_NOT_HUMAN = re.compile(r"<task-notification>|<system-reminder>|\[SYSTEM NOTIFICATION|<command-name>|<local-command"
                        r"|<scheduled-task|<cross-session-message")
_PRECONDITION = re.compile(r"(?i)^\W*(?:if|unless|provided|assuming|only\s+if|as\s+long\s+as|so\s+long\s+as|in\s+case"
                           r"|whenever|not\s+sure|wait)\b")
_NEGATED_ASK = re.compile(r"(?i)\b(not|yet|later|rejected|failed|fail|instead|or|rather|from|whether)\b|n't\b")
# Asks addressed to the owner, and nothing else ('approval needed' in a status list, 'requesting approval from Gro'
# and 'ready for approval after the replay' are not asks).
_ASK_TEMPLATE = re.compile(r"(?i)\bdo\s+you\s+approve\b|(?:^|:)\W*approve\b[^.!?\n]*\?|\byour\s+decision\b"
                           r"|\bready\s+for\s+your\s+approval\b|\bneeds?\s+your\s+approval\b|\bplease\s+approve\b"
                           r"|\breply\s+\W?approve")
_COURTESY = r"(?:,?\s*(?:please|thanks|thank\s+you))?"
_LEAD_IN = re.compile(r"(?i)\b(returned|said|says|wrote|verdict|output)\b|^\s*(#|={3,}|-{3,})")
# A decision stands unless the owner RETRACTS it later in the same message. Side notes ('If anything breaks, page me',
# "don't forget the replay", 'but keep Slack quiet') do not cancel it; these do.
_RETRACT = re.compile(r"(?im)\b(never\s*mind|scratch\s+that|on\s+second\s+thought|no\s+wait|wait,?\s+no|just\s+kidding|kidding"
                      r"|cancel\s+(?:that|it|this)|forget\s+(?:it|that)|nope|reject\s+(?:that|it|this)|actually[,\s]+(?:no|not"
                      r"|don'?t|do\s+not|hold|wait|reject|cancel)|hold\s+off|i'?ll\s+decide|decide\s+(?:later|tomorrow)"
                      r"|don'?t\s+approve|do\s+not\s+approve|undo\s+(?:that|it|the\s+approval))\b"
                      r"|\b(?:disregard|ignore\s+(?:that|it|this|my)|withdraw(?:n)?|not\s+really|jk|lol\s+no|on\s+hold"
                      r"|conditional\s+on|pending\s+\w+'?s?\s+(?:sign|ok|review|approval|check)|cancel(?:led)?)\b"
                      r"|(?:^|[.!?]\s+)\W*(?:stop|unless|provided|assuming|pending|actually|maybe|hmm|as\s+long\s+as|so\s+long\s+as|only\s+(?:if|after"
                      r"|once))\b"
                      r"|^\W*no\W*$")
_COURTESY_WORDS = {"thanks", "thank", "you", "ty", "please", "great", "good", "nice", "perfect", "awesome", "cheers",
                   "ship", "it", "go", "ahead", "work", "job", "well", "done", "excellent", "approved", "ok", "okay", "yes"}
_SEP = r"[\s \-‐-―_.,:;/]+"


# ── locations ────────────────────────────────────────────────────────────────────────────────────────────────────

def project_dir() -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _git(root: str, *args) -> str:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@functools.lru_cache(maxsize=None)
def common_dir(root: str) -> str:
    """The repository's common git directory (shared by every worktree)."""
    d = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return d or os.path.join(root, ".git")


def approvals_dir(root: str) -> str:
    """THE shared approval store (one per repository, not per worktree)."""
    return os.path.join(common_dir(root), "owner-approvals")


@functools.lru_cache(maxsize=None)
def _worktree_roots(root: str) -> tuple:
    return tuple(_list_worktrees(root))


def worktree_roots(root: str) -> list:
    return list(_worktree_roots(root))


def _list_worktrees(root: str) -> list:
    """Every worktree of this repository that exists on disk (main checkout first)."""
    out = []
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            p = line[len("worktree "):].strip()
            if os.path.isdir(p) and p not in out:
                out.append(p)
    if os.path.isdir(root) and os.path.realpath(root) not in {os.path.realpath(p) for p in out}:
        out.append(root)
    return out


def covers_sha(covers: list) -> str:
    return hashlib.sha256("\n".join(sorted(covers)).encode("utf-8")).hexdigest()


# ── designs ──────────────────────────────────────────────────────────────────────────────────────────────────────

_DECL_CACHE: dict = {}


def _declarations(root: str) -> dict:
    """{ID: [(abs path, covers, created), ...]} across every worktree (created = file creation time when recorded).
    Cached per process for 5 s (a hook runs briefly; design files do not change during one check)."""
    import time
    hit = _DECL_CACHE.get(root)
    if hit and time.monotonic() - hit[0] < 5:
        return hit[1]
    out = _scan_declarations(root)
    _DECL_CACHE[root] = (time.monotonic(), out)
    return out


def _scan_declarations(root: str) -> dict:
    out: dict = {}
    for wt in worktree_roots(root):
        for path in sorted(glob.glob(os.path.join(wt, "logs", "design_records", "*.md"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                st = os.stat(path)
                born = getattr(st, "st_birthtime", st.st_mtime)   # informational only; never authorizes
            except OSError:
                continue
            m = re.search(r"^Covers:\s*(.+?)(?:\n\s*\n|\n#)", text, re.M | re.S)
            covers = [c.strip() for c in re.split(r"[,\n]", m.group(1)) if c.strip()] if m else []
            for aid in re.findall(r"^Approval-ID:\s*(\S+)\s*$", text, re.M):
                out.setdefault(aid, []).append((path, covers, born))   # every copy (find_design checks they agree)
    return out


def find_design(root: str, approval_id: str) -> "tuple[str, list] | None":
    """(path, covers) of the design declaring this ID in any worktree; None if absent or declared with differing
    Covers lists (ambiguous)."""
    decls = _declarations(root).get(approval_id) or []
    if not decls or len({covers_sha(c) for _, c, _ in decls}) != 1 \
            or len({os.path.basename(p) for p, _, _ in decls}) != 1:   # one design file, copied across worktrees
        return None
    path, covers, _ = decls[0]
    return path, covers


def known_ids(root: str) -> list:
    """Approval-IDs (ending in 3+ digits) that natural wording may name, from every worktree."""
    return sorted(i for i in _declarations(root) if _NATURAL_ID.match(i) and find_design(root, i))


def pending_ids(root: str) -> list:
    """Known IDs with no recorded approval in the shared store."""
    have = {os.path.basename(p)[:-len(".json")] for p in glob.glob(os.path.join(approvals_dir(root), "*.json"))}
    return [i for i in known_ids(root) if i not in have]


# ── parsing ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _tokens(s: str) -> list:
    return _TOK.findall((s or "").lower())


def _segments(aid: str) -> list:
    return _tokens(aid)


def _runs(seg: list) -> set:
    """Every concatenation of consecutive segments ('f6trim', 'trim1017', ...), so a run-together spelling of part of
    the ID ('F6TRIM 1017', 'OCIRESET-1017') reads the same as the spaced one."""
    return {"".join(seg[i:j]) for i in range(len(seg)) for j in range(i + 1, len(seg) + 1)}


def _names(words: list, aid: str) -> bool:
    """The words name `aid`: each word is a run of the ID's segments and the ID's final number is among them."""
    seg = _segments(aid)
    runs = _runs(seg)
    return bool(words) and all(w in runs for w in words) and any(w.endswith(seg[-1]) for w in words)


def _in_text(toks: list, aid: str) -> bool:
    """The ID appears in a token stream as a contiguous run (any spacing / hyphenation / run-together spelling)."""
    want = "".join(_segments(aid))
    for i in range(len(toks)):
        acc = ""
        for j in range(i, len(toks)):
            acc += toks[j]
            if acc == want:
                return True
            if not want.startswith(acc):
                break
    return False


def _match_ids(words: list, known: list, pending: "list | None" = None) -> list:
    """The one ID the words name, or []. Every word must be a segment of the ID and the ID's number must be among
    them ('oci reset 1017', 'gates 1017', 'pr 689'). A number alone names an ID only among PENDING designs."""
    words = [w.lower() for w in words if w]
    if not words:
        return []
    pool = pending if (pending is not None and all(w.isdigit() for w in words)) else known
    cands = [aid for aid in pool if _names(words, aid)]
    return cands if len(cands) == 1 else []


# A decision is its own sentence: approve/approved/reject/rejected first, then only ID words, then the end.
_SENTENCE = re.compile(r"(?im)(?:^|(?<=[.!\n]))[ \t ]*(approved?|reject(?:ed)?)[ \t ]+([A-Za-z0-9][A-Za-z0-9 \t \-‐-―_]*?)[ \t ]*(?=[.!;\r\n]|$|,\s*(?:please|thanks|thank\s+you)\b)")


def _excluded_line(text: str, m) -> bool:
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    line = text[line_start:line_end if line_end != -1 else len(text)]
    if line.lstrip().startswith(">") or re.search(r"[\"“”‘`]|(?<![A-Za-z])'|'(?![A-Za-z])", line):
        return True
    before = text[:m.start()].rstrip()
    if before.endswith(":") or re.search(r"(?i)\b(e\.g|i\.e|etc|vs|cf)\.$", before):
        return True
    if text[:m.start()].count("```") % 2 == 1 or text[:m.start()].count("~~~") % 2 == 1:   # fenced code
        return True
    if line.startswith(("    ", "\t")):                               # indented code block
        return True
    prev = [ln for ln in text[:line_start].splitlines() if ln.strip()]
    if prev and _LEAD_IN.search(prev[-1]):                          # '=== GRO AUDIT ===' / 'Gro returned'
        return True
    head = text[max(text.rfind("\n\n", 0, m.start()), -1) + 1:m.start()].rstrip()   # its paragraph, before it
    if head.endswith((",", ";", "-", "\u2014", "\u2013")) or (prev and not head.endswith((".", "!", "?"))
                                                           and prev[-1].rstrip().endswith((",", ";"))):
        return True                                                 # 'If the replay passes,\nand GAI too,\nAPPROVE X'
    sentences = [x for x in re.split(r"(?<=[.!?])\s+|\n+", head) if x.strip()]
    if sentences and _PRECONDITION.search(sentences[-1]):          # 'Only if Gro agrees. Approve X.' / 'Assuming...\nApprove'
        return True
    after = text[m.end():]
    if _RETRACT.search(after):                                     # 'Approved X.\n\nNever mind.' anywhere after it
        return True
    for sent in re.split(r"(?<=[.!?])\s+|\n+", after):          # a terse follow-up ('Go.', 'Hmm.', 'Wrong one.')
        words = _tokens(sent)
        if 0 < len(words) <= 3 and not all(w in _COURTESY_WORDS for w in words):
            return True
    return text[m.end():].lstrip(" \t")[:1] == "?"


def ids_in(prompt: str, root: "str | None" = None, pending: "list | None" = None) -> "tuple[list, list]":
    """(approved IDs, rejected IDs) explicitly named in the owner's message (strict 'APPROVE <ID>' lines, or natural
    standalone decision sentences resolved against the declared IDs)."""
    text = prompt or ""
    if _NOT_HUMAN.search(text):
        return [], []
    approved: list = []
    for m in _APPROVE.finditer(text):
        if _strict_ok(text, m):
            approved += re.findall(_ID, m.group(1))
    rejected = [m.group(1) for m in _REJECT.finditer(text)]
    if root:
        known = known_ids(root)
        pend = pending if pending is not None else pending_ids(root)
        for m in _SENTENCE.finditer(text):
            if _excluded_line(text, m):
                continue
            words = re.split(_SEP, m.group(2).strip())
            is_reject = m.group(1).lower().startswith("reject")
            hits = _match_ids(words, known, None if is_reject else pend)
            if len(hits) == 1:
                (rejected if is_reject else approved).append(hits[0])
    approved = [a for a in dict.fromkeys(approved) if a not in rejected]
    return approved, list(dict.fromkeys(rejected))


def _strict_ok(text: str, m) -> bool:
    """An 'APPROVE <ID>' line counts only standing alone: nothing but punctuation after the IDs, and none of the
    exclusions (quote, '>' line, after 'X:', inside a code fence, a conditional before it, a question)."""
    rest = text[m.end():].split("\n", 1)[0]
    return not _excluded_line(text, m) and re.fullmatch(_COURTESY + r"[\s.!]*", rest, re.I) is not None


def names_id(text: str, aid: str) -> bool:
    """True if `text` is a direct decision that names `aid` (re-validation; uniqueness was enforced at capture)."""
    if _NOT_HUMAN.search(text or ""):
        return False
    for m in _APPROVE.finditer(text or ""):
        if _strict_ok(text, m) and aid in re.findall(_ID, m.group(1)):
            return True
    for m in _SENTENCE.finditer(text or ""):
        if m.group(1).lower().startswith("reject") or _excluded_line(text, m):
            continue
        if _names([w.lower() for w in re.split(_SEP, m.group(2).strip()) if w], aid):
            return True
    return False


def is_affirmative(text: str) -> bool:
    return bool(_AFFIRM.match(text or "")) and not _NOT_HUMAN.search(text or "")


def ids_requested(agent_text: str, candidates: list) -> list:
    """Pending IDs an agent message asks the owner to approve. A REQUEST is a sentence matching an explicit ask
    ('Approve X?', 'do you approve', 'YOUR DECISION', 'ready for your approval', 'needs your approval', 'please
    approve') with no negating or deferring word ('not', 'yet', 'before', 'first', 'failed', ...); the ID (any spacing
    / hyphenation) must be IN that sentence ('Approve OCI-RESET-1017?'), or — for the standard package ending in
    'YOUR DECISION' — on its 'PROPOSAL (<ID>):' heading or 'Approval-ID: <ID>' line. 'F6 was approved',
    'NOT ready for approval yet' and 'should I seek approval from the board first?' are not requests."""
    if not agent_text or not _AGENT_ASKS.search(agent_text):
        return []
    sents = [s for s in re.split(r"(?<=[.!?])\s+|\n+", agent_text) if s.strip()]
    direct: list = []
    package = False
    for i, s in enumerate(sents):
        if _ASK_TEMPLATE.search(s) and not _NEGATED_ASK.search(s):
            direct += _mentions(s, candidates)
            package = package or (i == len(sents) - 1 and re.search(r"(?i)\byour\s+decision\b", s) is not None)
    if direct:
        return list(dict.fromkeys(direct))
    if not package:
        return []
    heads = []   # the standard package: 'PROPOSAL (<ID>):' / 'PROPOSAL <ID>:' heading, or an 'Approval-ID: <ID>' line
    for ln in agent_text.splitlines():
        m = re.search(r"(?i)\bproposal\b[\s(*]{0,4}([A-Za-z0-9][A-Za-z0-9 _\-]*?)\s*\)?\s*\**\s*:", ln) \
            or re.search(r"(?i)\bapproval-id\b\W*([A-Za-z0-9][A-Za-z0-9_\-]*)", ln)
        if m:
            heads += _mentions(m.group(1), candidates)
    return list(dict.fromkeys(heads))


# ── transcripts (Claude and Codex) ───────────────────────────────────────────────────────────────────────────────

def _claude_text(entry: dict) -> str:
    raw = entry.get("message")
    msg = raw if isinstance(raw, dict) else entry
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in c):
            return ""                                   # tool output, not the owner
        return "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def transcript_user_text(entry: dict) -> str:
    """A complete, authentic human user message from a Claude or Codex JSONL entry, else ''.

    Claude: type=user, message.role=user, origin.kind == "human" (set only for text a person typed into the app), not
    isMeta / sidechain / compaction summary, not a tool result, carries a uuid, and is not a harness notification. Codex: a response_item user message whose retained_source is complete, user-authored, and whose
    message_id and turn_id match the payload (sub-agent copies and incomplete records carry no such source)."""
    msg = entry.get("message")
    role = msg.get("role") if isinstance(msg, dict) else entry.get("role")
    if entry.get("type") == "user" and role == "user" and entry.get("isMeta") is not True:
        origin = entry.get("origin")
        if (not entry.get("uuid") or entry.get("isSidechain") is True or entry.get("isCompactSummary") is True
                or entry.get("isVisibleInTranscriptOnly") is True
                or not isinstance(origin, dict) or origin.get("kind") != "human"):
            return ""   # only text the harness marks as typed by a human: not a headless `claude -p` prompt, a sub-agent
            #             prompt, a compaction summary, hook feedback or a notification (all carry no human origin)
        text = _claude_text(entry)
        return "" if _NOT_HUMAN.search(text) else text
    payload = entry.get("payload")
    if entry.get("type") != "response_item" or not isinstance(payload, dict):
        return ""
    retained = (entry.get("metadata") or {}).get("retained_source")
    if not isinstance(retained, dict):
        return ""
    rid = retained.get("id")
    passthrough = payload.get("internal_chat_message_metadata_passthrough")
    if (payload.get("type") != "message" or payload.get("role") != "user" or not isinstance(rid, dict)
            or retained.get("complete") is not True or rid.get("role") != "user"
            or not payload.get("id") or payload.get("id") != rid.get("message_id")
            or not isinstance(passthrough, dict) or not passthrough.get("turn_id")
            or passthrough.get("turn_id") != rid.get("turn_id")):
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    text = "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "input_text")
    return "" if _NOT_HUMAN.search(text) else text


def transcript_agent_text(entry: dict) -> str:
    """Assistant-visible text of an agent message (Claude assistant text blocks; Codex assistant output_text)."""
    if entry.get("type") == "assistant":
        msg = entry.get("message") or {}
        c = msg.get("content")
        if isinstance(c, list):
            return "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        return c if isinstance(c, str) else ""
    payload = entry.get("payload")
    if entry.get("type") == "response_item" and isinstance(payload, dict) and payload.get("type") == "message" \
            and payload.get("role") == "assistant":
        return "".join(p.get("text", "") for p in payload.get("content") or []
                       if isinstance(p, dict) and p.get("type") == "output_text")
    return ""


def _entry_identity(entry: dict) -> str:
    if entry.get("type") == "user":
        return str(entry.get("uuid") or "")
    p = entry.get("payload") or {}
    return f"{p.get('id')}|{(p.get('internal_chat_message_metadata_passthrough') or {}).get('turn_id')}"


def _entry_ts(entry: dict) -> float:
    """Entry time (epoch seconds) from Claude/Codex 'timestamp' (ISO-8601), or 0.0 when absent/unparseable."""
    ts = entry.get("timestamp")
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() if ts else 0.0
    except ValueError:
        return 0.0


def iter_turns(path: str, with_ts: bool = False):
    """Yield (user_text, user_identity, preceding_agent_text[, ts]) for each authentic user message in a transcript."""
    last_agent = ""
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            a = transcript_agent_text(e)
            if a.strip():
                last_agent = a
                continue
            u = transcript_user_text(e)
            if u:
                yield (u, _entry_identity(e), last_agent, _entry_ts(e)) if with_ts else (u, _entry_identity(e), last_agent)


# ── store ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _write(root: str, rec: dict, name: "str | None" = None) -> None:
    adir = approvals_dir(root)
    os.makedirs(adir, exist_ok=True)
    name = name or f"{rec['id']}.json"
    tmp = os.path.join(adir, f".{name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=2)
    os.replace(tmp, os.path.join(adir, name))


def migrate_legacy(root: str) -> list:
    """Copy legacy per-worktree .claude/approvals/<ID>.json into the shared store (never overwrites, never deletes).
    Only records that validate are copied — a planted file cannot occupy an ID. Returns IDs."""
    adir = approvals_dir(root)
    moved = []
    for wt in worktree_roots(root):
        for p in glob.glob(os.path.join(wt, ".claude", "approvals", "*.json")):
            name = os.path.basename(p)
            if name.startswith(".") or os.path.exists(os.path.join(adir, name)):
                continue
            try:
                with open(p) as fh:
                    rec = json.load(fh)
            except Exception:
                continue
            if rec.get("status") == "APPROVED" and name == f"{rec.get('id')}.json" \
                    and not _rejected_ts(root, rec.get("id") or "") and approval_valid(rec, root):
                rec["migrated_from"] = p
                _write(root, rec)
                moved.append(rec["id"])
    return moved


def resolve(user_text: str, agent_text: str, root: str, pending: "list | None" = None) -> "tuple[list, list, str]":
    """(approved, rejected, note) for one owner message given the immediately preceding agent message. `pending` is
    the set of designs pending AS OF that message (replay); default: pending now."""
    pend = pending if pending is not None else pending_ids(root)
    approved, rejected = ids_in(user_text, root, pend)
    unshown = [a for a in approved if not _named_by_word(user_text, a) and a not in _mentions(agent_text, [a])]
    if unshown:
        approved = [a for a in approved if a not in unshown]
        if not approved and not rejected:
            return [], [], (f"'{user_text.strip()}' names {unshown} only by number, and the last agent message did not "
                            "show that ID — nothing recorded; name it (e.g. 'approve " + unshown[0] + "').")
    if approved or rejected:
        return approved, rejected, ""
    if is_affirmative(user_text):
        asked = ids_requested(agent_text, pend)
        if len(asked) == 1:
            return asked, [], ""
        if asked:
            return [], [], f"'{user_text.strip()}' is ambiguous — the last message asked about {asked}; name one."
        if agent_text and _AGENT_ASKS.search(agent_text) and pend:
            return [], [], f"'{user_text.strip()}' names no design; pending: {pend}."
    return [], [], ""


def record(root: str, aid: str, user_text: str, identity: str, transcript_path: str, session_id, mode: str,
           agent_text: str = "", approved_at: "float | None" = None) -> str:
    found = find_design(root, aid)
    if not found:
        return f"APPROVE {aid}: no single design record declares 'Approval-ID: {aid}' — nothing recorded."
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import required_questions as rq
        gaps = rq.unanswered(open(found[0], encoding="utf-8").read())
    except Exception as exc:
        gaps = [f"(required_questions unavailable: {exc})"]
    if gaps:
        return f"APPROVE {aid}: NOT recorded — its design record does not answer required questions {gaps}."
    rec = {"id": aid, "status": "APPROVED", "design_record": found[0], "covers": found[1],
           "covers_sha256": covers_sha(found[1]),
           "prompt_sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
           "message_identity": identity, "mode": mode,
           "agent_sha256": hashlib.sha256(agent_text.encode("utf-8")).hexdigest(),
           "session_id": session_id, "transcript_path": transcript_path,
           "approved_at": (datetime.fromtimestamp(approved_at, timezone.utc).isoformat() if approved_at
                           else datetime.now(timezone.utc).isoformat()),
           "ts": datetime.now(timezone.utc).isoformat()}
    _write(root, rec)
    return (f"Owner approval recorded: {aid} covers {len(found[1])} path(s); fingerprint {covers_sha(found[1])[:16]}. "
            f"Tell the owner in one line; 'reject {aid}' undoes it.")


def remove(root: str, aid: str, rejected_at: "float | None" = None) -> str:
    for d in [approvals_dir(root)] + [os.path.join(w, ".claude", "approvals") for w in worktree_roots(root)]:
        p = os.path.join(d, f"{aid}.json")
        if os.path.exists(p):
            os.remove(p)
    when = rejected_at or datetime.now(timezone.utc).timestamp()
    if when > _rejected_ts(root, aid):
        _write(root, {"id": aid, "status": "REJECTED",
                      "rejected_at": datetime.fromtimestamp(when, timezone.utc).isoformat()}, f"{aid}.rejected.json")
    return f"Owner REJECTED {aid}: any approval for it is removed."


def _rejected_ts(root: str, aid: str) -> float:
    try:
        with open(os.path.join(approvals_dir(root), f"{aid}.rejected.json")) as fh:
            return datetime.fromisoformat(str(json.load(fh).get("rejected_at"))).timestamp()
    except Exception:
        return 0.0


_TEST_TRANSCRIPT_DIRS: list = []   # set only in-process by the test harness; nothing in production writes it


def transcript_allowed(path: str) -> bool:
    """True if `path` is inside a real Claude/Codex transcript tree (the only place owner text can come from)."""
    q = os.path.realpath(path)
    for d in transcript_dirs() + _TEST_TRANSCRIPT_DIRS:
        d = os.path.realpath(d)
        if q.startswith(d + os.sep):
            return True
    return False


def _named_by_word(text: str, aid: str) -> bool:
    """The decision names `aid` with at least one non-numeric word ('approve gex 1017', 'APPROVE GEX-1017'), so it
    cannot be re-pointed at another design that merely ends in the same number."""
    if _NOT_HUMAN.search(text or ""):
        return False
    for m in _APPROVE.finditer(text or ""):
        if _strict_ok(text, m) and aid in re.findall(_ID, m.group(1)):
            return True
    for m in _SENTENCE.finditer(text or ""):
        if m.group(1).lower().startswith("reject") or _excluded_line(text, m):
            continue
        words = [w.lower() for w in re.split(_SEP, m.group(2).strip()) if w]
        if _names(words, aid) and not all(w.isdigit() for w in words):
            return True
    return False


_VALID_CACHE = ".valid_cache.json"


def approval_valid_cached(rec: dict, root: str) -> bool:
    """approval_valid, remembered per record content while its transcript has only grown (transcripts are
    append-only and agent-unwritable), so a gate call does not re-read a 100 MB transcript per record."""
    key = hashlib.sha256(json.dumps(rec, sort_keys=True).encode("utf-8")).hexdigest()
    path = os.path.join(approvals_dir(root), _VALID_CACHE)
    try:
        with open(path) as fh:
            cache = json.load(fh)
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}
    tp = rec.get("transcript_path") or ""
    try:
        size = os.path.getsize(tp)
    except (OSError, TypeError):
        return False
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("tp") == tp and isinstance(hit.get("size"), int) and size >= hit["size"]:
        return True
    ok = approval_valid(rec, root)
    if ok:
        cache[key] = {"tp": tp, "size": size}
        try:
            _write(root, cache, _VALID_CACHE)
        except OSError:
            pass
    return ok


def approval_valid(rec: dict, root: str) -> bool:
    """The record's message is an authentic user entry in a real transcript (hash + identity match) and it approves
    THIS id — naming it with a word, naming its number after an agent had presented the ID earlier in that
    conversation, or as an affirmative right after an agent message that asked about exactly this id."""
    tp, want, aid = rec.get("transcript_path"), rec.get("prompt_sha256"), rec.get("id") or ""
    if not tp or not want or not aid or not os.path.exists(tp) or not transcript_allowed(tp):
        return False
    ident = rec.get("message_identity")
    for text, identity, agent_text, _ts, _presented in _turns_with_presented(tp, [aid]):
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != want:
            continue
        if ident and identity != ident:
            continue
        if not ident and rec.get("agent_sha256") \
                and hashlib.sha256(agent_text.encode("utf-8")).hexdigest() != rec.get("agent_sha256"):
            continue                       # no message id recorded: pin to the turn by the agent message before it
        if _named_by_word(text, aid) or (names_id(text, aid) and aid in _mentions(agent_text, [aid])):
            return True
        if rec.get("mode") == "affirmative" and is_affirmative(text) \
                and hashlib.sha256(agent_text.encode("utf-8")).hexdigest() == rec.get("agent_sha256") \
                and aid in ids_requested(agent_text, [aid]):
            return True
    return False


# ── capture paths ────────────────────────────────────────────────────────────────────────────────────────────────

def _last_turn(transcript_path: str, prompt: str):
    """(identity, preceding agent text) of the latest transcript turn whose text equals `prompt`, else ('', last
    agent text seen) — the hook can run before the owner's own entry is written."""
    ident, agent, last_agent, agent_after = "", "", "", False
    for text, identity, agent_text in iter_turns(transcript_path) if transcript_path else []:
        ident, agent = (identity, agent_text) if text == prompt else ("", "")
    if ident:   # the matching entry is the CURRENT prompt only if no agent message follows it
        try:
            with open(transcript_path, encoding="utf-8") as fh:
                seen_user = False
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(e, dict):
                        continue
                    if transcript_user_text(e):
                        seen_user = _entry_identity(e) == ident
                        agent_after = False
                    elif seen_user and transcript_agent_text(e).strip():
                        agent_after = True
        except OSError:
            pass
        if agent_after:
            ident = ""
    if not ident:
        try:
            with open(transcript_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        a = transcript_agent_text(json.loads(line))
                    except ValueError:
                        continue
                    if a.strip():
                        last_agent = a
        except (OSError, TypeError):
            pass
        agent = last_agent
    return ident, agent


def capture(payload: dict, root: str) -> list:
    """Claude UserPromptSubmit path: the payload's prompt is the owner's typed text."""
    prompt = payload.get("prompt") or ""
    if _NOT_HUMAN.search(prompt):
        return []
    migrate_legacy(root)
    register_designs(root)
    tp = payload.get("transcript_path") or ""
    ident, agent_text = _last_turn(tp, prompt)
    approved, rejected, note = resolve(prompt, agent_text, root)
    notes = [note] if note else []
    if not approved and not rejected and not note:
        if re.search(r"(?i)\b(approve|reject)\b", prompt) and re.search(r"\d{3,}", prompt):
            notes.append("an approve/reject was written but it names no single pending design — nothing recorded; "
                         f"pending: {pending_ids(root)}.")
        return notes
    mode = "explicit" if ids_in(prompt, root)[0] else "affirmative"
    for aid in approved:
        notes.append(record(root, aid, prompt, ident, tp, payload.get("session_id"), mode, agent_text))
    for aid in rejected:
        notes.append(remove(root, aid))
    return notes


def transcript_home() -> str:
    """The account's real home directory (from the password database, NOT $HOME — a command cannot point the git hook
    at a forged transcript tree by overriding HOME)."""
    import pwd
    return pwd.getpwuid(os.getuid()).pw_dir


def transcript_dirs() -> list:
    home = transcript_home()
    return [os.path.join(home, ".codex", "sessions"), os.path.join(home, ".claude", "projects")]


def _transcript_files() -> list:
    """Top-level Claude transcripts of this project and every Codex session file (sub-agent files excluded)."""
    home = transcript_home()
    out = glob.glob(os.path.join(home, ".codex", "sessions", "**", "*.jsonl"), recursive=True)
    out += glob.glob(os.path.join(home, ".claude", "projects", "*alpaca-mtf-bot*", "*.jsonl"))
    return out


def _record_ts(root: str, aid: str) -> float:
    try:
        rec = json.load(open(os.path.join(approvals_dir(root), f"{aid}.json")))
        return datetime.fromisoformat(str(rec.get("approved_at") or rec.get("ts"))).timestamp()
    except Exception:
        return 0.0


def _mentions(agent_text: str, candidates: list) -> list:
    """Candidate IDs an agent message names (any spacing/hyphenation, as a contiguous run of the ID's segments)."""
    toks = _tokens(agent_text)
    return [aid for aid in candidates if _in_text(toks, aid)]


def _turns_with_presented(path: str, candidates: list):
    """Like iter_turns(with_ts=True), plus the candidate IDs an agent had named EARLIER in this transcript."""
    state = {"offset": 0, "presented": [], "last_agent": ""}
    events, _ = _read_new(path, candidates, state, stop_at_partial=False)
    yield from events


def _read_new(path: str, candidates: list, state: dict, stop_at_partial: bool = True) -> "tuple[list, dict]":
    """Owner turns appended to `path` since `state['offset']`: [(text, identity, preceding agent text, ts,
    frozenset(IDs presented earlier))], plus the new state. A trailing partial line is left for the next read."""
    offset = int(state.get("offset") or 0)
    presented = set(state.get("presented") or [])
    last_agent = state.get("last_agent") or ""
    try:
        size = os.path.getsize(path)
        if size < offset:                     # rewritten/truncated: start over
            offset, presented, last_agent = 0, set(), ""
        fh = open(path, "rb")
    except OSError:
        return [], state
    out = []
    with fh:
        fh.seek(offset)
        for raw in fh:
            if stop_at_partial and not raw.endswith(b"\n"):
                break
            offset += len(raw)
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            a = transcript_agent_text(e)
            if a.strip():
                last_agent = a
                presented.update(_mentions(a, candidates))
                continue
            u = transcript_user_text(e)
            if u:
                out.append((u, _entry_identity(e), last_agent, _entry_ts(e), frozenset(presented)))
    return out, {"offset": offset, "presented": sorted(presented), "last_agent": last_agent}


_CURSOR = ".replay_cursor.json"     # dot-name: never read as an approval record


def _load_cursor(root: str) -> dict:
    try:
        with open(os.path.join(approvals_dir(root), _CURSOR)) as fh:
            cur = json.load(fh)
        return cur if isinstance(cur, dict) else {}
    except Exception:
        return {}


_FIRST_SEEN = ".first_seen.json"   # {ID: epoch first seen by any hook}; dot-name, never read as a record


def _design_signature(root: str) -> str:
    h = hashlib.sha256()
    for wt in worktree_roots(root):
        for path in sorted(glob.glob(os.path.join(wt, "logs", "design_records", "*.md"))):
            try:
                st = os.stat(path)
            except OSError:
                continue
            h.update(f"{path}|{st.st_ino}|{st.st_ctime_ns}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()


def register_designs(root: str) -> dict:
    """Note the first time any hook (every Claude tool call via the gate, the prompt hook, any git hook, or
    `--register`) saw each declared design WITH ITS CURRENT Covers list. A replayed owner message counts only for
    designs seen, with the same Covers, before it was typed — widening Covers after the owner's message voids it."""
    path = os.path.join(approvals_dir(root), _FIRST_SEEN)
    try:
        with open(path) as fh:
            seen = json.load(fh)
        if not isinstance(seen, dict):
            seen = {}
    except Exception:
        seen = {}
    sig = _design_signature(root)
    if seen.get("_sig") == sig:
        return seen
    now = datetime.now(timezone.utc).timestamp()
    changed = seen.get("_sig") != sig
    seen["_sig"] = sig
    for aid in known_ids(root):
        found = find_design(root, aid)
        sha = covers_sha(found[1]) if found else ""
        cur = seen.get(aid)
        if not isinstance(cur, dict) or cur.get("covers") != sha:
            seen[aid] = {"ts": now, "covers": sha}   # new design, or its Covers changed: seen NOW
            changed = True
    if changed:
        try:
            _write(root, seen, _FIRST_SEEN)
        except OSError:
            pass
    return seen


def _first_seen(root: str, aid: str, seen: "dict | None" = None) -> float:
    v = (seen if seen is not None else register_designs(root)).get(aid)
    t = v.get("ts") if isinstance(v, dict) else None
    return float(t) if isinstance(t, (int, float)) else float("inf")


def _pending_at(root: str, ts: float, seen: "dict | None" = None) -> list:
    """Designs pending at time `ts`: already seen by then, and not approved before `ts`."""
    seen = seen if seen is not None else register_designs(root)
    out = []
    for aid in known_ids(root):
        if _first_seen(root, aid, seen) > ts:
            continue
        rec = os.path.join(approvals_dir(root), f"{aid}.json")
        if not os.path.exists(rec) or _record_ts(root, aid) > ts:
            out.append(aid)
    return out


def capture_from_transcripts(root: str, paths: "list | None" = None, use_cursor: "bool | None" = None) -> list:
    """Git-hook path (Codex, or any agent without a UserPromptSubmit hook): read the Claude/Codex transcript lines
    added since the last run (a cursor in the store; `paths` = read those files whole, without the cursor) and apply
    the owner's decisions in time order. Each message is judged ONCE, against the designs that existed and were
    pending when it was read — an old message is never re-applied to a design created later, and a design counts only
    if a hook had already seen it when the message was typed (register_designs). An approval counts only if the owner
    named the design with a word, or by number / affirmative right after an agent message showing that ID, and only if
    typed after the latest rejection of that ID (rejections leave a dated tombstone, even when nothing is pending)."""
    migrate_legacy(root)
    seen = register_designs(root)
    known = known_ids(root)
    use_cursor = paths is None if use_cursor is None else use_cursor
    cursor = _load_cursor(root) if use_cursor else {}
    files = paths if paths is not None else _transcript_files()
    events = []
    new_cursor = dict(cursor)
    for tp in files:
        if not transcript_allowed(tp):
            continue
        st = cursor.get(tp) or {}
        try:
            if use_cursor and os.path.getsize(tp) == st.get("offset"):
                continue
        except OSError:
            continue
        evs, new_cursor[tp] = _read_new(tp, known, st, stop_at_partial=use_cursor)
        events += [(ts, text, ident, agent, tp, pres) for text, ident, agent, ts, pres in evs if ts > 0]
    notes = []
    for ts, text, identity, agent_text, tp, presented in sorted(events, key=lambda e: e[0]):
        pend_then = _pending_at(root, ts, seen)
        approved, rejected, _ = resolve(text, agent_text, root, pend_then)
        for aid in approved:
            if aid in pending_ids(root) and aid not in pend_then and _first_seen(root, aid, seen) > ts:
                notes.append(f"{aid}: an owner approval was typed before this design (with its current Covers) was "
                             "registered — not recorded; register it (--register) and ask the owner again.")
                continue
            if aid in pending_ids(root) and aid in pend_then and ts > _rejected_ts(root, aid) \
                    and (_named_by_word(text, aid) or aid in presented):
                mode = "explicit" if aid in ids_in(text, root, pend_then)[0] else "affirmative"
                notes.append(record(root, aid, text, identity, tp, None, mode, agent_text, approved_at=ts))
        for aid in rejected:
            if ts > _record_ts(root, aid):
                notes.append(remove(root, aid, rejected_at=ts))
    if use_cursor:
        _write(root, new_cursor, _CURSOR)
    return notes


def main() -> None:
    if sys.argv[1:2] == ["--register"]:   # Codex / any agent: run when presenting a design to the owner
        seen = register_designs(project_dir())
        print(json.dumps({k: datetime.fromtimestamp(_first_seen(project_dir(), k, seen), timezone.utc).isoformat()
                          for k in sorted(seen) if not k.startswith("_")}, indent=1))
        sys.exit(0)
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
