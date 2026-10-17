#!/usr/bin/env python3
# ruff: noqa: E501
"""owner_approval_gate.py — PreToolUse hook (all tools): no trading-behaviour change without the OWNER's approval.

Design record: logs/design_records/owner_approval_gates_2026-10-17.md (Approval-ID GATES-1017; board Schneier seat,
Gro, GAI). Blocks (exit 2):
  1. any write-capable tool (Write, Edit, MultiEdit, NotebookEdit, MCP edit/refactor tools, or a Bash command that writes)
     touching a BEHAVIOUR path, unless an owner approval record covers that path and its approval text is a USER entry in
     the conversation transcript (written only by owner_approval_capture.py from the owner's typed message);
  2. any agent write to .claude/approvals/ (only the capture hook writes there);
  3. git hook-skipping and git-config/GIT_* override commands, always;
  4. a Groq/Gemini call that does not go through the allowlisted, bias-scanned wrappers;
  5. an `ssh mtf-bot` command that changes the crontab or writes into the bot directory, unless "server-ops" is approved.
Behaviour paths reuse preship_gate's own scope (_in_risk_scope + its accounting exclusions + GATED_SELF) plus
settings.local.json. FAIL-CLOSED for behaviour paths: an internal error on a write-capable call blocks.
"""
import glob
import hashlib
import json
import os
import re
import shlex
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_MCP_WRITE = re.compile(r"(replace|edit|insert|move|apply|add|create|write|delete|rename|refactor|update|set)_", re.I)
_PATH_KEYS = ("file_path", "notebook_path", "path", "paths", "target_file", "filename", "file", "files", "target")
_GIT_OVERRIDE = re.compile(
    r"core\.hookspath|--no-verify|\bgit\b[^|;&]*\bcommit\b[^|;&]*\s-[a-z]*n[a-z]*\b|\bGIT_CONFIG_(COUNT|KEY|VALUE|GLOBAL|SYSTEM|NOSYSTEM)\b"
    r"|\bGIT_DIR=|\bgit\s+config\b[^|;&]*\bhooks", re.I)
_BGG = re.compile(r"api\.groq\.com|generativelanguage\.googleapis\.com|\bgai_client\b|\bcall_gai\b", re.I)
_BGG_ALLOWED = re.compile(r"^\s*(?:cd\s+\S+\s*&&\s*)?python3?\s+\S*\.claude/preship/(preship_audit|bgg_call)\.py\b")
_WRITE_HINT = re.compile(
    r"(?<![<>])>>?(?!&)|\bsed\s+-i|\bperl\s+-[a-z]*i|\btee\b|\b(cp|mv|rm|ln|install|truncate|touch|unlink)\b"
    r"|\bopen\([^)]*['\"][wax+]|write_text|write_bytes|\.write\(|os\.replace|shutil\.|os\.remove"
    r"|\bgit\s+(apply|restore|cherry-pick|revert|am)\b|\bgit\s+checkout\b[^|;&]*\s--\s|\bgit\s+reset\s+--hard\b"
    r"|\bgit\s+stash\s+(pop|apply)\b|\bpatch\s+(-|<)", re.I)
_REDIRECT = re.compile(r"(?<![<>0-9&])>>?\s*([^\s;&|]+)|(?<=[0-9])>>?\s*([^\s&;|][^\s;&|]*)")
_SERVER_WRITE = re.compile(
    r"\bcrontab\s+(-[er]|-\s|[^-l\s|]\S*)|\|\s*crontab\b|\bsed\s+-i|\btee\b|\bgit\s+(checkout|reset|commit|apply|stash|restore|revert|cherry-pick)\b"
    r"|\bsystemctl\s+(stop|disable|edit|mask)\b|(?<![<>0-9&])>>?\s*(?!/tmp/|/dev/null|&)\S|\b(rm|mv|cp)\s+[^|;&]*mtf-bot/", re.I)
_SERVER_OK_PULL = re.compile(r"\bgit\s+pull\b")


def _root() -> str:
    return os.path.realpath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _rel(path: str, root: str) -> str:
    p = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    if p == root or not p.startswith(root + os.sep):
        return ""
    return os.path.relpath(p, root)


_EXTRA_SCOPE = (".claude/approvals/", "scripts/managed_gates/")
_EXTRA_FILES = (".claude/settings.json", ".claude/settings.local.json", "scripts/install_managed_gates.sh",
                "auto_deploy.sh", "CLAUDE.md")
_SCOPE_DIRS = ("execution", "strategy", "indicators", "events", ".claude/preship", ".claude/approvals", ".github",
               "scripts/managed_gates", ".claude")


def is_behaviour(rel: str) -> bool:
    if not rel:
        return False
    rel = rel.rstrip("/")
    if rel in _SCOPE_DIRS or rel.startswith(_EXTRA_SCOPE) or rel in _EXTRA_FILES:
        return True
    try:
        import preship_gate as pg
        if pg._in_risk_scope(rel) or rel in getattr(pg, "_RISK_SCOPE_EXCLUDED", ()) or rel.startswith(pg.GATED_SELF):
            return True
    except Exception:
        pass
    return (rel.startswith(("execution/", "strategy/", "indicators/", "events/", ".claude/preship/", ".github/"))
            or (rel.startswith("run_") and rel.endswith(".py")) or rel in ("main.py", "config.py", "data/gex.py",
                                                                              "data/market_breadth.py"))


def _user_text(entry: dict) -> str:
    raw = entry.get("message")
    msg: dict = raw if isinstance(raw, dict) else entry
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def approval_valid(rec: dict, root: "str | None" = None) -> bool:
    """The approval's text is a USER entry in its transcript with the recorded hash AND that text approves THIS id."""
    tp = rec.get("transcript_path")
    want = rec.get("prompt_sha256")
    aid = rec.get("id") or ""
    if not tp or not want or not aid or not os.path.exists(tp):
        return False
    try:
        import owner_approval_capture as cap
    except Exception:
        return False
    try:
        with open(tp, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                role = (e.get("message") or {}).get("role") if isinstance(e.get("message"), dict) else e.get("role")
                if e.get("type") == "user" and role == "user" and e.get("isMeta") is not True:
                    text = _user_text(e)
                    if hashlib.sha256(text.encode("utf-8")).hexdigest() == want and aid in cap.ids_in(text, root)[0]:
                        return True
    except OSError:
        return False
    return False


def approvals(root: str) -> list:
    out = []
    for p in glob.glob(os.path.join(root, ".claude", "approvals", "*.json")):
        try:
            rec = json.load(open(p))
        except Exception:
            continue
        if rec.get("status") == "APPROVED" and os.path.basename(p) == f"{rec.get('id')}.json" and approval_valid(rec, root):
            try:   # covers come from the design record on disk, not from the (writable) approval file
                import owner_approval_capture as cap
                found = cap.find_design(root, rec["id"])
                if found and cap.covers_sha(found[1]) == rec.get("covers_sha256"):
                    rec = dict(rec, covers=found[1])
                    out.append(rec)
            except Exception:
                continue
    return out


def covered(rel: str, recs: list) -> bool:
    for r in recs:
        for c in r.get("covers") or []:
            c = c.strip().rstrip(",")
            if rel == c or (c.endswith("/") and rel.startswith(c)) or (c.endswith("*") and rel.startswith(c[:-1])):
                return True
    return False


def _paths_in(obj) -> list:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _PATH_KEYS and isinstance(v, str):
                out.append(v)
            elif k in _PATH_KEYS and isinstance(v, list):
                out += [x for x in v if isinstance(x, str)]
            elif isinstance(v, (dict, list)):
                out += _paths_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _paths_in(v)
    return out


def _expand(cmd: str, root: str) -> str:
    return (cmd.replace("$(pwd)", root).replace("${PWD}", root).replace("$PWD", root)
            .replace("$CLAUDE_PROJECT_DIR", root).replace("${CLAUDE_PROJECT_DIR}", root))


def _bash_paths(cmd: str, root: str) -> list:
    cmd = _expand(cmd, root)
    try:
        toks = shlex.split(cmd, posix=True)
    except ValueError:
        toks = cmd.split()
    cands = set(toks) | set(re.findall(r"[\w./-]+\.(?:py|json|sh|yml|yaml|md)", cmd))
    return [t for t in cands if is_behaviour(_rel(t, root))]


def _block(msg: str) -> None:
    sys.stderr.write("OWNER APPROVAL GATE (blocking): " + msg + "\n")
    sys.exit(2)


def check(payload: dict) -> None:
    root = _root()
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    if tool == "Bash":
        cmd = ti.get("command") or ""
        cmd_nomsg = re.sub(r"(-m|--message)(=|\s+)(\"[^\"]*\"|'[^']*'|\S+)", " ", cmd)
        cmd_nomsg = re.sub(r"<<-?\s*'?(\w+)'?.*?\n\1", " ", cmd_nomsg, flags=re.S)   # heredoc bodies are data
        if _GIT_OVERRIDE.search(cmd_nomsg):
            _block("commands that skip or redirect git hooks are not allowed (core.hooksPath, --no-verify, commit -n, "
                   "GIT_CONFIG_*/GIT_DIR overrides, git config hooks).")
        if _BGG.search(cmd) and not _BGG_ALLOWED.search(cmd):
            _block("Groq/Gemini calls must go through .claude/preship/bgg_call.py or preship_audit.py, which scan the "
                   "prompt actually sent.")
        for tok in re.findall(r"(?:python3?|bash|sh)\s+(\S+)", cmd):
            p = os.path.join(root, tok) if not os.path.isabs(tok) else tok
            if os.path.isfile(p) and not _BGG_ALLOWED.search(f"python3 {tok}"):
                try:
                    if _BGG.search(open(p, encoding="utf-8", errors="ignore").read()):
                        _block(f"{tok} calls Groq/Gemini directly; use .claude/preship/bgg_call.py.")
                except OSError:
                    pass
        if re.search(r"\bssh\b[^|;&]*\bmtf-bot\b", cmd):
            remote = cmd.split("mtf-bot", 1)[1]
            if _SERVER_WRITE.search(remote) and not covered("server-ops", approvals(root)):
                _block("this server command changes the crontab or writes into the bot directory; it needs the owner's "
                       "approval of an ID whose Covers includes 'server-ops'.")
            return
        if ".claude/projects" in cmd and _WRITE_HINT.search(cmd):
            _block("the conversation transcripts under ~/.claude/projects/ are not writable by the agent.")
        if ".claude/approvals" in cmd and _WRITE_HINT.search(cmd):
            _block("only the owner-approval hook writes .claude/approvals/.")
        if _WRITE_HINT.search(cmd):
            no_redirects = _REDIRECT.sub(" ", cmd)
            if _WRITE_HINT.search(no_redirects):
                hits = _bash_paths(cmd, root)            # a real write verb: every behaviour path named counts
            else:                                         # only redirects: just their targets count
                targets = [a or b for a, b in _REDIRECT.findall(cmd)]
                hits = [t for t in targets if is_behaviour(_rel(t, root))]
            if hits:
                recs = approvals(root)
                missing = [h for h in hits if not covered(_rel(h, root), recs)]
                if missing:
                    _block(f"this command may write trading-behaviour or gate files {sorted(set(missing))} with no owner "
                           "approval covering them. Bring the design to the owner; he approves with 'APPROVE <ID>'.")
        return
    write_tool = tool in _WRITE_TOOLS or (tool.startswith("mcp__") and _MCP_WRITE.search(tool.split("__")[-1] + "_"))
    if not write_tool:
        return
    rels = [_rel(p, root) for p in _paths_in(ti)]
    if any(r.startswith(".claude/approvals/") for r in rels):
        _block("only the owner-approval hook writes .claude/approvals/.")
    hits = [r for r in rels if is_behaviour(r)]
    if not hits:
        return
    recs = approvals(root)
    missing = [h for h in hits if not covered(h, recs)]
    if missing:
        _block(f"{missing} is trading-behaviour or gate code. No owner approval covers it. Bring the exact design to the "
               "owner; he approves with 'APPROVE <ID>' (the ID declared as 'Approval-ID:' in its design record, whose "
               "'Covers:' lists these paths).")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    try:
        check(payload)
    except SystemExit:
        raise
    except Exception as exc:  # fail closed on a write-capable call
        tool = (payload or {}).get("tool_name") or ""
        if tool in _WRITE_TOOLS or tool == "Bash" or tool.startswith("mcp__"):
            _block(f"gate error ({exc}); failing closed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
