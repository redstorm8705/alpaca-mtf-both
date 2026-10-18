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
import subprocess
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


# the user-level Claude Code settings (outside the repo): they can disable hooks or set the hooks' environment
_USER_SETTINGS = re.compile(r"(?:~|\$HOME|/Users/[^/\s]+|/home/[^/\s]+)/\.claude/settings(?:\.local)?\.json")


def settings_files(root: "str | None" = None) -> list:
    """The Claude Code settings outside this checkout that configure hooks or their environment: the user-level files
    and the MAIN checkout's project files (found from git's common dir; a worktree session must not edit them)."""
    home = os.path.expanduser("~")
    out = [os.path.join(home, ".claude", "settings.json"), os.path.join(home, ".claude", "settings.local.json")]
    try:
        r = subprocess.run(["git", "-C", root or os.getcwd(), "rev-parse", "--path-format=absolute",
                            "--git-common-dir"], capture_output=True, text=True, timeout=5)
        common = r.stdout.strip()
        if r.returncode == 0 and common:
            main = os.path.join(os.path.dirname(common), ".claude")
            out += [os.path.join(main, "settings.json"), os.path.join(main, "settings.local.json")]
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted({os.path.realpath(p) for p in out})


def _is_user_settings(p: str, root: str) -> bool:
    """Resolved-path comparison, so '//', '/./', '../' and relative spellings all count."""
    q = os.path.realpath(os.path.expanduser(p if os.path.isabs(os.path.expanduser(p)) else os.path.join(root, p)))
    return q in settings_files(root)


_EXTRA_SCOPE = (".claude/approvals/", ".claude/quarantine/", "scripts/managed_gates/")
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


def _transcript_user_text(entry: dict) -> str:
    """Return a human user message from Claude or Codex JSONL, else empty.

    Claude records user messages at the top level. Codex records the same human
    input as a ``response_item`` whose payload is a user ``message``.  Requiring
    the retained-source role as well as the payload role keeps tool echoes and
    assistant-authored text out of the approval path.
    """
    role = (entry.get("message") or {}).get("role") if isinstance(entry.get("message"), dict) else entry.get("role")
    if entry.get("type") == "user" and role == "user" and entry.get("isMeta") is not True:
        return _user_text(entry)
    payload = entry.get("payload")
    if entry.get("type") != "response_item" or not isinstance(payload, dict):
        return ""
    retained = (entry.get("metadata") or {}).get("retained_source")
    if not isinstance(retained, dict):
        return ""
    retained_id = retained.get("id")
    passthrough = payload.get("internal_chat_message_metadata_passthrough")
    if (
        payload.get("type") != "message"
        or payload.get("role") != "user"
        or not isinstance(retained_id, dict)
        or retained.get("complete") is not True
        or retained_id.get("role") != "user"
        or not payload.get("id")
        or payload.get("id") != retained_id.get("message_id")
        or not isinstance(passthrough, dict)
        or not passthrough.get("turn_id")
        or passthrough.get("turn_id") != retained_id.get("turn_id")
    ):
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "input_text"
    )


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
                text = _transcript_user_text(e)
                if text and hashlib.sha256(text.encode("utf-8")).hexdigest() == want and aid in cap.ids_in(text, root)[0]:
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
        if _USER_SETTINGS.search(cmd) and (_WRITE_HINT.search(cmd) or _REDIRECT.search(cmd)):
            _block("Claude Code settings files (~/.claude/settings*.json) set hook behaviour and environment; only the "
                   "owner edits them.")
        if (".claude/approvals" in cmd or ".claude/quarantine" in cmd) and _WRITE_HINT.search(cmd):
            _block("only the owner-approval and integrity hooks write .claude/approvals/ and .claude/quarantine/.")
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
    if any(_USER_SETTINGS.search(str(p)) or _is_user_settings(str(p), root) for p in _paths_in(ti)):
        _block("Claude Code settings files (~/.claude/settings*.json) set hook behaviour and environment; only the owner "
               "edits them.")
    rels = [_rel(p, root) for p in _paths_in(ti)]
    if any(r.startswith((".claude/approvals/", ".claude/quarantine/")) for r in rels):
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
