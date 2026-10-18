#!/usr/bin/env python3
# ruff: noqa: E501
"""owner_approval_gate.py — PreToolUse hook (all tools): no trading-behaviour change without the OWNER's approval.

Design record: logs/design_records/owner_approval_gates_2026-10-17.md (Approval-ID GATES-1017; board Schneier seat,
Gro, GAI). Blocks (exit 2):
  1. any write-capable tool (Write, Edit, MultiEdit, NotebookEdit, MCP edit/refactor tools, or a Bash command that writes)
     touching a BEHAVIOUR path, unless an owner approval record covers that path and its approval text is a USER entry in
     the conversation transcript (written only by owner_approval_capture.py from the owner's typed message);
  2. any agent write to the shared approval store (<git common dir>/owner-approvals), the legacy .claude/approvals/,
     or a Claude/Codex transcript (only the owner_approval_capture code writes approvals);
  3. git hook-skipping and git-config/GIT_* override commands, always;
  4. a Groq/Gemini call that does not go through the allowlisted, bias-scanned wrappers;
  5. an `ssh mtf-bot` command that changes the crontab or writes into the bot directory, unless "server-ops" is approved.
Behaviour paths reuse preship_gate's own scope (_in_risk_scope + its accounting exclusions + GATED_SELF) plus
settings.local.json. FAIL-CLOSED for behaviour paths: an internal error on a write-capable call blocks.
"""
import glob
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
# Tools and commands that start a new session with agent-written text as its USER message (scheduled tasks, headless
# claude/codex, session-management messages, remote triggers; in-session SendMessage text arrives wrapped and is
# never read as owner text): that text would be recorded as the owner's own words.
_INJECT_TOOL = re.compile(r"(?i)scheduled|send_?message|remote_?trigger|cron(create|update)|run_task|spawn_task"
                          r"|run_in_terminal|open_terminal|session_mgmt|move_to_cloud"
                          r"|computer|app_type|type_text|clipboard|browser|chrome|form_input|teach")   # GUI typing too
# a claude/codex launch given a prompt (headless, exec, piped/redirected stdin, or a quoted first prompt)
# Where a shell runs a program: line start, after ; & | ( { ` $( then/do/else, after a wrapper (env, nohup, time, exec,
# sudo, nice, timeout N, xargs, npx, ...), inside `sh -c '...'`, or an osascript `do script "..."`.
_LAUNCH_POS = re.compile(
    r"(?m)(?=(?:\bscript(?:\s+-\S+)*\s+\S+\s|do\s+script\s+\\?['\"]|-exec(?:dir)?\s|\bunbuffer\s|-\w*c\s+['\"]"
    r"|\b(?:then|do|else)\s|[;&|){}`]|(?<![\w$])\(|\$\(|^)"
    r"\s*(?:[{(!]\s*)*"
    r"(?:(?:\S*/)?(?:env|command|builtin|nohup|time|exec|eval|sudo|nice|timeout|caffeinate|xargs|npx|bunx|pnpx"
    r"|stdbuf|watch|parallel|screen|unbuffer|tmux\s+new(?:-session)?|tmux\s+new-window)"
    r"(?:\s+-[angust]\s+(?![-'\"])\S+|\s+-\S+|\s+\d\S*)*\s+['\"]?|\w+=\S*\s+)*(\S+)(.*))")   # zero-width: every position
_LAUNCHERS = ("claude", "claude-code", "codex")
_NON_PROMPT = re.compile(r"^\s+(?:mcp|config|update|doctor|install|login|logout|--version|--help|-v|-h)\b")


def _ansi_c(body: str) -> str:
    try:
        return body.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return body


def _launches_agent(cmd: str) -> bool:
    """True if the command starts a claude/codex session (any form that can take a prompt). Message text and heredoc
    bodies are ignored; quote/backslash splicing ('cla""ude', 'cl\\aude') and globs ('claud?') are normalised."""
    import fnmatch
    c = _strip_data(cmd)
    c = re.sub(r"\$'((?:[^'\\]|\\.)*)'", lambda m: _ansi_c(m.group(1)), c)     # $'\x63laude'
    c = re.sub(r"\{([^{}\s]*,[^{}\s]*)\}", lambda m: " ".join(x for x in m.group(1).split(",") if x) or " ", c)
    c = re.sub(r"\\(?=\w)", "", c)
    c = re.sub(r"(?<=\w)(['\"])\1?(?=\w)", "", c)
    for m in _LAUNCH_POS.finditer(c):
        word = m.group(1).strip("'\"()`")
        base = word.rsplit("/", 1)[-1].lower()
        hit = any(fnmatch.fnmatchcase(name, base) for name in _LAUNCHERS) if re.search(r"[*?\[]", base) \
            else base in _LAUNCHERS
        if hit and not _NON_PROMPT.match(m.group(2)):
            return True
    return False


_INJECT_PATH = re.compile(r"/\.claude/scheduled-tasks/|/\.codex/")
_APPROVAL_TEXT = re.compile(r"(?im)\b(approved?|reject(?:ed)?)\b[^\n]{0,80}?\d{3,}"
                            r"|\b(?:approved?|reject)\s+[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+"
                            r"|^[\s>*_-]*(?:(?:yes|ok|okay)[\s,.!]+)?(approved?|proceed|go\s+ahead|ship\s+it|continue)\W*$")
_MSG_ARGS = re.compile(r"(-m|--message|--body|--title|-t|--notes)(=|\s+)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|\S+)")


def _strip_data(cmd: str) -> str:
    """The command minus commit/PR message values and heredoc bodies (text, not commands)."""
    out = _MSG_ARGS.sub(" ", cmd)
    return re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\1", " ", out, flags=re.S)


def _strings(obj) -> list:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _strings(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _strings(v)]
    return []


def _injects_approval(tool: str, ti: dict) -> bool:
    """True if this call would put approval-shaped text into a new session's user message."""
    texts = _strings(ti)
    if tool == "Bash":   # any prompt-carrying claude/codex launch: its text cannot be checked when read from a file
        return _launches_agent(ti.get("command") or "")
    if (_INJECT_TOOL.search(tool) and tool != "SendMessage") \
            or any(_INJECT_PATH.search(os.path.expanduser(p)) for p in _paths_in(ti)):
        return any(_APPROVAL_TEXT.search(t) for t in texts)
    return False


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


def _transcript_user_text(entry: dict) -> str:
    """A complete, authentic human user message from Claude or Codex JSONL, else '' (the shared parser's rule)."""
    import owner_approval_capture as cap
    return cap.transcript_user_text(entry)


def approval_valid(rec: dict, root: "str | None" = None) -> bool:
    """The approval's text is an authentic USER entry in its transcript (hash + message identity) that approves THIS
    id (the shared parser's rule)."""
    try:
        import owner_approval_capture as cap
        return cap.approval_valid(rec, root or _root())
    except Exception:
        return False


def approvals(root: str) -> list:
    """Valid owner approvals from the SHARED store (<git common dir>/owner-approvals), shared by every agent and
    worktree; legacy per-worktree .claude/approvals records are migrated in first."""
    try:
        import owner_approval_capture as cap
        cap.migrate_legacy(root)
        cap.register_designs(root)
        adir = cap.approvals_dir(root)
    except Exception:
        return []
    out = []
    for p in glob.glob(os.path.join(adir, "*.json")):
        try:
            rec = json.load(open(p))
        except Exception:
            continue
        if rec.get("status") == "APPROVED" and os.path.basename(p) == f"{rec.get('id')}.json" \
                and cap.approval_valid_cached(rec, root):
            # covers come from the design record on disk, not from the (writable) approval file
            found = cap.find_design(root, rec["id"])
            if found and cap.covers_sha(found[1]) == rec.get("covers_sha256"):
                out.append(dict(rec, covers=found[1]))
    return out


def _transcript_path(p: str, root: str) -> bool:
    """True if `p` is a Codex session file or a Claude transcript (.jsonl) — the source of every owner approval.
    Claude's memory files under ~/.claude/projects/*/memory/ stay writable."""
    try:
        import owner_approval_capture as cap
        codex, claude = [os.path.realpath(d) for d in cap.transcript_dirs()]
    except Exception:
        return False
    q = os.path.realpath(os.path.expanduser(p if os.path.isabs(os.path.expanduser(p)) else os.path.join(root, p)))
    if q == codex or q.startswith(codex + os.sep):
        return True
    return q.startswith(claude + os.sep) and q.endswith(".jsonl")


def _store_path(p: str, root: str) -> bool:
    """True if `p` is inside the shared approval store (or a legacy approvals dir)."""
    try:
        import owner_approval_capture as cap
        store = os.path.realpath(cap.approvals_dir(root))
    except Exception:
        store = ""
    q = os.path.realpath(os.path.expanduser(p if os.path.isabs(os.path.expanduser(p)) else os.path.join(root, p)))
    legacy = os.sep + os.path.join(".claude", "approvals") + os.sep      # any checkout's legacy folder, not just ours
    return (bool(store) and (q == store or q.startswith(store + os.sep))) or legacy in q + os.sep


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
    try:   # note every design (and its Covers) the moment it exists, so an owner approval typed later can count
        import owner_approval_capture as cap
        cap.register_designs(root)
    except Exception:
        pass
    if _injects_approval(tool, ti):
        _block("this would start a session whose user message is written by an agent (a headless or launched "
               "claude/codex run, or approval wording in a scheduled task / session message); only the owner types "
               "approvals.")
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
        if (".claude/projects" in cmd or ".codex/sessions" in cmd) and _WRITE_HINT.search(cmd):
            _block("the conversation transcripts (~/.claude/projects/, ~/.codex/sessions/) are not writable by the agent.")
        if _USER_SETTINGS.search(cmd) and (_WRITE_HINT.search(cmd) or _REDIRECT.search(cmd)):
            _block("Claude Code settings files (~/.claude/settings*.json) set hook behaviour and environment; only the "
                   "owner edits them.")
        if (".claude/approvals" in cmd or ".claude/quarantine" in cmd or "owner-approvals" in cmd) \
                and _WRITE_HINT.search(cmd):
            _block("only the owner-approval and integrity hooks write the approval store (.git/owner-approvals), "
                   ".claude/approvals/ and .claude/quarantine/.")
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
    if any(_transcript_path(str(p), root) for p in _paths_in(ti)):
        _block("the conversation transcripts (~/.claude/projects/, ~/.codex/sessions/) are not writable by the agent.")
    rels = [_rel(p, root) for p in _paths_in(ti)]
    if any(r.startswith((".claude/approvals/", ".claude/quarantine/")) for r in rels) \
            or any(_store_path(str(p), root) for p in _paths_in(ti)):
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
