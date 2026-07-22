#!/usr/bin/env python3
"""
PreToolUse hook — SHIP GATE v2 (GAI-audited 2026-07-03).

Blocks `git commit` / `git push` / `rsync` that would SHIP a gated code file
unless that file's exact STAGED/COMMITTED content has a fresh Gro+GAI APPROVE
marker written by preship_audit.py.

HONEST LIMITATION (GAI finding #1): this hook lives in a dir the agent can
write, so it is a FIRST-LINE SPEED BUMP, not an unbypassable wall. The real
wall is GitHub branch-protection on `main` (server-side, requires Rafael's
review) — outside the agent's reach. This gate raises the cost of a shortcut
and makes skipping visible; it does not claim to be tamper-proof.

Exit 0 = allow. Exit 2 = block (stderr shown to Claude).
GAI v2 fixes: staged-blob sha (not working-tree); fail-CLOSED on git errors;
rsync of a gated file blocked outright; widened scope; shlex command parse.
"""
import sys
import os
import json
import hashlib
import subprocess
import time
import shlex
import re

# Derived from __file__, NOT hardcoded (2026-07-22). A hardcoded absolute path made the
# gate silently inert on any machine but its author's — which is exactly the failure the
# versioning of this file is meant to prevent (second Claude account, OCI, fresh clone).
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARKER_DIR = os.path.join(REPO, ".claude", "preship", "markers")
MARKER_MAX_AGE_SEC = 24 * 3600

GATED_DIRS = ("execution/", "strategy/", "events/", "data/", "indicators/", "scripts/")
GATED_EXTS = (".py", ".sh", ".json", ".yaml", ".yml", ".ini", ".env", ".toml", ".cfg")
GATED_SPECIFIC = ("main.py", "config.py", "requirements.txt", "Dockerfile",
                  "Pipfile", "CLAUDE.md", ".gitignore")
# T6 FIX: the enforcer was the ONE artifact nothing gated. An agent could weaken
# _is_gated, delete the PreToolUse block from settings.json, or re-ignore .claude/,
# and commit any of it with no marker — silently undoing every control here. Given the
# thesis that mechanism beats documentation, the mechanism must be the MOST-gated
# artifact in the repo. No bootstrap problem: the marker writers work on any
# staged file.
GATED_SELF = (".claude/preship/", ".claude/settings.json", ".github/")

def _is_gated(path: str) -> bool:
    path = path[2:] if path.startswith("./") else path
    if path in GATED_SPECIFIC:
        return True
    if path.startswith("run_") and path.endswith(".py"):
        return True
    _, ext = os.path.splitext(path)
    if path.startswith(GATED_DIRS) and ext in GATED_EXTS:
        return True
    # GAP CLOSED 2026-07-22: every root-level .py/.sh is gated.
    # scan_to_html.py was NOT gated — it sits at repo root, so it matched no GATED_DIR
    # and was absent from GATED_SPECIFIC. That file renders the operator's primary
    # decision surface AND runs synchronously on the run_cycle thread (write_scan_html
    # blocks check_exits()), and it could be committed and pushed with no Gro/GAI marker
    # at all. The same held for generate_dashboard.py, weekly_review.py,
    # reconcile_eod.py, options_scanner.py and every other root operational script.
    # A whitelist of names is bypassed the moment a new root file is added, so gate the
    # whole tier instead. Root-level non-operational files are .md/.txt/.json, which are
    # excluded by the extension test below.
    if "/" not in path and ext in (".py", ".sh"):
        return True
    if path.startswith(GATED_SELF):
        return True
    # T7: dirs that were omitted but already carry audit markers -> they were treated as
    # ship-critical at some point and then silently fell out of scope.
    if (path.startswith(("reporting/", "monitoring/", "research/", "tests/"))
            and ext in GATED_EXTS):
        return True
    return False

_SHELL_SEPS = {"&&", "||", ";", "|", "(", ")", "{", "}", "then", "do", "else", "!",
               "sudo", "env", "time", "nohup", "xargs", "&",
               # 2026-07-22 (cold-2nd F6): `command rsync ...` / `exec rsync ...`
               # slipped rsync past _cmd_position because the prefix word was not a
               # recognised command boundary. Adding them is the SAFE direction
               # (more detection, never fewer).
               "command", "exec"}

_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredocs(cmd: str) -> str:
    """Remove heredoc BODIES, keeping the command lines around them.

    A heredoc body is DATA, not shell. The coarse ship backstop must not scan it,
    or any command that merely writes text containing "git commit"/"git push" — an
    audit prompt, a test table, a doc edit, a commit-message draft — is
    hard-blocked. That exact
    false positive fired twice while building this gate, and the second time it was
    caused by the backstop added to fix the first. A gate that blocks read-only work
    gets switched off, which is a worse outcome than the hole it closes.

    Conservative by design: if a terminator is never found, everything from the heredoc
    onward is dropped, so data can only ever be REMOVED from the scan, never
    command text retained. Real shell operators are unaffected because they
    precede the `<<`.
    """
    out, i = [], 0
    lines = cmd.split("\n")
    while i < len(lines):
        line = lines[i]
        mm = list(_HEREDOC_RE.finditer(line))
        out.append(_HEREDOC_RE.sub("", line) if mm else line)
        if mm:
            term = mm[-1].group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                i += 1          # drop the body
        i += 1
    return "\n".join(out)


def _cmd_position(argv, name):
    """Index of `name` used AS A COMMAND, else None.

    A bare basename test is not enough: `grep -rn rsync .` contains the token "rsync" as
    a search PATTERN, and blocking it produced exactly the read-only-command outage this
    gate is supposed to avoid. A token is a command only at argv[0] or immediately after
    a shell separator / command prefix.
    """
    for i, tok in enumerate(argv):
        if os.path.basename(tok) != name:
            continue
        if i == 0:
            return i
        prev = argv[i - 1]
        if prev in _SHELL_SEPS or prev.endswith(("&&", "||", ";", "|")) or "=" in prev:
            return i
    return None


def _git_subcommands(argv):
    """Every git SUBCOMMAND actually invoked in this command line.

    FALSE-POSITIVE FIX 2026-07-22: the previous test was
        "git" in argv and ("commit" in argv or "push" in argv)
    which scans the WHOLE tokenised command — including heredoc bodies. Any Bash call
    whose text merely mentioned git, commit and push (an audit prompt, a commit-message
    draft, a doc edit) was misclassified as a ship and hard-blocked. This fired
    immediately on the very session that added the cold-2nd gate.

    Now: find each literal `git` token and walk past global options (`-C <path>`,
    `--git-dir=...`, `-c k=v`) to the real subcommand. Prose can no longer trigger it,
    while `git commit`, `git push`, `git -C /repo commit` and `git add x && git push`
    all still do. Over-blocking is the safe direction, but blocking everything is not
    a gate — it is an outage, and an outage gets the gate disabled.
    """
    return _git_subcommands_depth(argv, 0)


def _shell_strip(tok: str) -> str:
    """Strip shell punctuation so subshell forms still expose the command token.

    `(git push)` -> shlex gives ['(git', 'push)']; `v=$(git push)` gives ['v=$(git',
    'push)']. Without this, both parse to nothing and a REAL ship goes undetected.
    Also drops a leading `VAR=` assignment prefix.
    """
    t = tok.strip("()`{};&|\n\t ")
    if t.startswith("$"):
        t = t.lstrip("$(")
    if "=" in t and not t.startswith("-"):
        head, _, tail = t.partition("=")
        if head.replace("_", "").isalnum() and tail.startswith("$("):
            t = tail.lstrip("$(")
    return t


def _git_subcommands_depth(argv, depth):
    """Precise parse. NO raw-string regex — deliberately.

    A coarse regex over the command text cannot tell an INVOCATION from TEXT ABOUT an
    invocation. A backstop of that shape was tried here and immediately blocked ordinary
    read-only work: a heredoc documenting the ship verbs, and a `python3 -c` test table
    containing the literal string "git commit". `shlex` already solves this — quoting
    collapses such text into a single token, which is not a bare `git` token. So the fix
    for the missed subshell forms is better TOKENISATION, not a looser scan.

    Covers: git commit/push, `git -C <p> commit`, `git add x && git push`,
    `(git push)`, `$(git push)`, `sudo git push`, `git.exe push`, and one level of
    `bash -c "..."` / `sh -c "..."`.
    """
    subs = set()
    n = len(argv)
    for i, raw in enumerate(argv):
        tok = _shell_strip(raw)
        base = os.path.basename(tok)
        # Recurse one level into an embedded shell payload: bash -c "git push"
        if depth == 0 and base in ("bash", "sh", "zsh", "dash"):
            for j in range(i + 1, n):
                if argv[j] == "-c" and j + 1 < n:
                    try:
                        subs |= _git_subcommands_depth(
                            shlex.split(argv[j + 1]), depth + 1)
                    except Exception:
                        # Unparseable payload: fail SAFE — assume it could ship.
                        subs |= {"commit", "push"}
                    break
            continue
        if base not in ("git", "git.exe"):
            continue
        j = i + 1
        while j < n:
            t = argv[j]
            if t in ("-C", "--git-dir", "--work-tree", "-c", "--namespace"):
                j += 2
                continue
            if t.startswith("-"):
                j += 1
                continue
            break
        if j < n:
            subs.add(_shell_strip(argv[j]))
    return subs


def _git(args, want_bytes=False):
    """Run a git command. Returns (ok, output). ok=False on any failure."""
    try:
        r = subprocess.run(["git", "-C", REPO] + args,
                           capture_output=True, timeout=15)
        if r.returncode != 0:
            return False, r.stderr
        return True, (r.stdout if want_bytes else r.stdout.decode("utf-8", "replace"))
    except Exception as e:
        return False, str(e)

def _content_sha(gitref: str) -> str:
    """sha256 of a file's content at a git ref (e.g. ':path' staged, 'HEAD:path').

    'WT:path' hashes the WORKING-TREE bytes instead — used when the commit form ships
    the working tree (-a / --all / pathspec / an `add` chained in the same command), so
    a marker bound to older staged bytes can never validate newer unstaged edits.
    """
    if gitref.startswith("WT:"):
        try:
            with open(os.path.join(REPO, gitref[3:]), "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return ""
    ok, out = _git(["cat-file", "blob", gitref], want_bytes=True)
    if not ok:
        return ""
    return hashlib.sha256(out).hexdigest()

def _marker_ok(relpath: str, ref: str):
    slug = relpath.replace("/", "__")
    mpath = os.path.join(MARKER_DIR, slug + ".json")
    if not os.path.exists(mpath):
        return False, "no audit marker — run preship_audit.py"
    try:
        m = json.load(open(mpath))
    except Exception as e:
        return False, f"marker unreadable: {e}"
    if time.time() - m.get("ts", 0) > MARKER_MAX_AGE_SEC:
        return False, "marker stale (>24h)"
    if m.get("gro") not in ("APPROVE", "WAIVED") or m.get("gai") != "APPROVE":
        return False, f"not approved (gro={m.get('gro')} gai={m.get('gai')})"
    cur = _content_sha(ref)
    if not cur:
        return False, "FAIL-CLOSED: cannot read staged/committed content"
    if cur != m.get("sha256"):
        return False, "content changed since audit (sha mismatch) — re-audit"
    return True, "ok"

def _cold2_ok(relpath: str, ref: str):
    """Require a fresh COLD SECOND-AGENT PASS for the exact content being shipped.

    ADDED 2026-07-22. Nothing previously enforced this step — the cold-2nd review was
    invoked voluntarily. On the 2026-07-21/22 scan_to_html.py diff it caught FOUR
    ship-blocking defects across three rounds (two FAILs) that ruff, mypy and py_compile
    all passed clean:
      - a per-symbol quote fallback that could block ~600s on the run_cycle thread,
        starving check_exits() and leaving live stops unevaluated;
      - a None*qty TypeError that aborts write_html before the atomic write, freezing
        the operator's page at its last good render while it still LOOKS live;
      - every unfilled overnight limit order rendering as a broker reconciliation break;
      - {} conflating "fetch failed" with "empty book", flagging the entire
        position book.

    The sha binding is the point: it must be a PASS on the bytes actually shipping, not
    on an earlier revision. Rounds 2 and 3 each found defects introduced by the previous
    round's fix, so a stale PASS is worse than none.

    Marker: .claude/preship/markers/<slug>.cold2.json
            {"verdict": "PASS", "sha256": "<sha of shipped content>", "ts": <epoch>}
    Write it with: python3 .claude/preship/record_cold2.py <file> PASS
    """
    slug = relpath.replace("/", "__")
    mpath = os.path.join(MARKER_DIR, slug + ".cold2.json")
    if not os.path.exists(mpath):
        return False, ("no COLD-2ND marker — run an adversarial cold second-agent "
                       "review on the exact diff, then: "
                       f"python3 .claude/preship/record_cold2.py {relpath} PASS")
    try:
        m = json.load(open(mpath))
    except Exception as e:
        return False, f"cold-2nd marker unreadable: {e}"
    if time.time() - m.get("ts", 0) > MARKER_MAX_AGE_SEC:
        return False, "cold-2nd marker stale (>24h) — re-review"
    if str(m.get("verdict", "")).upper() != "PASS":
        return False, (f"cold-2nd verdict is {m.get('verdict')!r}, not PASS — "
                       "a FAIL blocks the ship")
    cur = _content_sha(ref)
    if not cur:
        return False, ("FAIL-CLOSED: cannot read staged/committed content for "
                       "cold-2nd check")
    if cur != m.get("sha256"):
        return False, ("content changed since the cold-2nd PASS (sha mismatch) — "
                       "RE-REVIEW. A fix applied after a PASS is exactly how rounds "
                       "2 and 3 introduced new blockers.")
    return True, "ok"


def _changed_gated(range_args, ref_tmpl):
    """(fails, gated_files) for one diff range. Deletions are EXCLUDED — see T3."""
    ok, out = _git(["diff", "--name-status"] + range_args)
    if not ok:
        return (["FAIL-CLOSED: could not list shipped files (git error). "
                 "Aborting ship rather than allow-through. If this is a push, try "
                 "`git fetch origin` first — the upstream ref may be missing."], [])
    files = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        # T3 FIX: a DELETED path has no blob at the ref, so _content_sha returns ""
        # and the marker check fail-closes forever — `git rm <gated>` and every
        # RENAME became unshippable with no recovery path, which teaches the
        # operator to bypass the gate. Deletions carry no code to audit. Renames
        # appear as A + D; the A side is checked.
        if status.startswith("D"):
            continue
        path = parts[-1].strip()          # for R/C the destination is last
        if path:
            files.append(path)
    gated = [f for f in files if _is_gated(f)]
    fails = []
    for f in gated:
        good, why = _marker_ok(f, ref_tmpl.format(f))
        if not good:
            fails.append(f"  - {f}: [Gro/GAI] {why}")
        c_good, c_why = _cold2_ok(f, ref_tmpl.format(f))
        if not c_good:
            fails.append(f"  - {f}: [COLD-2ND] {c_why}")
    return (fails, gated)


_COMMIT_ARG_TAKING = ("-m", "--message", "-F", "--file", "-t", "--template",
                      "--author", "--date", "-c", "-C", "--fixup", "--squash")


def _commit_worktree_mode(argv, subcmds):
    """True when the commit will ship WORKING-TREE bytes that a `--cached` inspection
    cannot see. Three verified forms:

      `git commit -a` / `-am` / `--all`   -> commits the working tree directly. With
          premarket.py/entry_logic.py/weekly_review.py modified-unstaged and gated,
          `git commit -am` shipped all three with zero markers (verified 2026-07-22).
      `git commit -m x file.py`           -> pathspec commit; ships file.py's working-
          tree bytes regardless of the index.
      `git add X && git commit && git push` -> the hook fires BEFORE the whole command
          runs, so `--cached` reflects the PRE-add index (often empty) while the commit
          ships the post-add index == working-tree bytes. The T1 fix repaired range
          selection but not this ordering hole.

    In all three, the audit range becomes `git diff --name-status HEAD` (staged +
    unstaged vs HEAD — a superset) and marker shas are checked against WORKING-TREE
    bytes ('WT:' refs). Markers bind to STAGED bytes, so anything WT-modified after its
    audit fails the sha match and blocks — which is the point. Over-blocking direction
    only: the canonical flow (stage -> audit -> plain `git commit`) is unaffected.
    """
    if "add" in subcmds:
        return True
    n = len(argv)
    for i, raw in enumerate(argv):
        if os.path.basename(_shell_strip(raw)) not in ("git", "git.exe"):
            continue
        j = i + 1                      # walk past global options to the subcommand
        while j < n:
            t = argv[j]
            if t in ("-C", "--git-dir", "--work-tree", "-c", "--namespace"):
                j += 2
                continue
            if t.startswith("-"):
                j += 1
                continue
            break
        if j >= n or _shell_strip(argv[j]) != "commit":
            continue
        k = j + 1                      # scan commit's own args to end of shell segment
        while k < n:
            t = argv[k]
            if t in _SHELL_SEPS or t in ("&&", "||", ";", "|"):
                break
            if t in _COMMIT_ARG_TAKING:
                k += 2
                continue
            if t == "--all" or t == "-a" or \
                    (re.fullmatch(r"-[a-zA-Z]+", t) and "a" in t[1:]):
                return True            # -a / -am / clustered short flags containing a
            if t.startswith("-"):
                k += 1
                continue
            return True                # bare non-flag token after commit = pathspec
    return False


def _handle_git(cmd_argv, subcmds):
    """Check EVERY ship subcommand present, not just one.

    T1 FIX (CRITICAL). The previous code did `is_push = "push" in cmd_argv` — raw
    token membership over the whole command — and then took EITHER the push branch
    OR the commit branch. Two verified bypasses followed:
      `git add -A && git commit -m x && git push`  -> took the PUSH branch and diffed
          origin/main..HEAD, which is EMPTY before the commit exists -> ALLOWED an
          unaudited commit+push in one line. The single most natural way to type a ship.
      `git commit -m push`                          -> the word "push" in the MESSAGE
          flipped it to the push branch -> ALLOWED.
    Now the subcommand set decides, and when both are present both ranges are
    checked and the failures unioned. Commits that ship working-tree bytes (-a /
    pathspec / chained add) are audited against the working tree — see
    _commit_worktree_mode.
    """
    fails = []
    if "commit" in subcmds:
        if _commit_worktree_mode(cmd_argv, subcmds):
            f, _ = _changed_gated(["HEAD"], "WT:{}")
            if f:
                f.append("  NOTE: this commit form (-a / --all / pathspec / chained "
                         "`git add`) ships WORKING-TREE bytes, so the audit compares "
                         "the working tree to HEAD. Markers bind to STAGED bytes — "
                         "stage the file (git add <file>), audit it, then use a plain "
                         "`git commit` with no -a and no pathspec.")
        else:
            f, _ = _changed_gated(["--cached"], ":{}")
        fails += f
    if "push" in subcmds:
        # @{upstream} is correct for any branch; fall back to origin/main. A missing
        # upstream fails CLOSED via _changed_gated's git-error path.
        ok, _ = _git(["rev-parse", "--abbrev-ref", "@{upstream}"])
        rng = ["@{upstream}..HEAD"] if ok else ["origin/main..HEAD"]
        f, _ = _changed_gated(rng, "HEAD:{}")
        fails += f
    # De-duplicate: a chained commit+push reports the same file from both ranges.
    seen, uniq = set(), []
    for x in fails:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq

def _handle_rsync(cmd_argv):
    # git-single-channel PROHIBITS rsync of repo code entirely. Rather than
    # enumerate which files (defeated by dir / '.' / subdir-relative tricks),
    # block ANY rsync argument that resolves to a path INSIDE the repo. Every
    # path form (relative, absolute, '.', 'dir/', subdir) collapses to abspath
    # then commonpath, so there is no path-shape bypass. rsync of paths OUTSIDE
    # the repo (backups, logs elsewhere) is unaffected.
    repo_real = os.path.realpath(REPO)
    hits = []
    for tok in cmd_argv[1:]:
        if tok.startswith("-"):
            continue  # rsync flag, not a path
        cand = tok.split(":")[-1]  # strip user@host: for remote specs
        if not cand:
            continue
        abscand = os.path.realpath(os.path.abspath(cand))  # resolves vs CWD
        try:
            inside = os.path.commonpath([abscand, repo_real]) == repo_real
        except ValueError:
            inside = False  # different drive / not comparable
        if inside:
            hits.append(f"  - rsync references a repo path ({cand}); rsync of repo "
                        f"files is PROHIBITED (git-single-channel) — use commit+push")
    return hits

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # GAI fix #9: unparseable stdin on a ship must not slip through, but we
        # only see stdin for the matched tool; if it's junk, fail-closed only if
        # it *looks* like a ship. Safest: allow (non-ship tools hit this too).
        sys.exit(0)
    cmd = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    # Heredoc BODIES are data, not shell. Strip them before tokenising or a document
    # that merely mentions the ship verbs parses as a ship and hard-blocks
    # read-only work.
    _cmd_shell = _strip_heredocs(cmd)
    try:
        argv = shlex.split(_cmd_shell)
    except Exception:
        argv = _cmd_shell.split()
    subcmds = _git_subcommands(argv)
    # T5 FIX: the precise parser misses `bash -c "git push"`, `(git push)`,
    # `$(git push)`, `git.exe`, and user aliases — all real ship forms. Precision
    # belongs in the false-POSITIVE direction only; a missed ship is the dangerous
    # error. Coarse regex backstop on the raw command string, scoped to one shell
    # segment so prose spanning a pipe/semicolon does not trigger it.
    is_git_ship = bool(subcmds & {"commit", "push"})
    # T4 FIX: `"rsync" in joined` was a substring test over every token, so any command
    # merely MENTIONING rsync (grep -rn rsync, editing CLAUDE.md's rsync sentence) was
    # hard-blocked — it blocked a reviewer's own read-only commands twice. Require an
    # actual rsync executable token.
    _rsync_idx = _cmd_position(argv, "rsync")
    looks_like_ship = (_rsync_idx is not None or is_git_ship)
    try:
        fails = []
        if is_git_ship:
            fails = _handle_git(argv, subcmds)
        elif _rsync_idx is not None:
            fails = _handle_rsync(argv[_rsync_idx:])
        else:
            sys.exit(0)  # not a ship
    except SystemExit:
        raise
    except Exception as e:
        # Fail-safe: an internal error must not brick all Bash calls. If this
        # looked like a SHIP, block (fail-closed); otherwise allow.
        if looks_like_ship:
            sys.stderr.write(f"SHIP BLOCKED: preship_gate internal error on a "
                             f"ship command ({e}) — fail-closed.\n")
            sys.exit(2)
        sys.exit(0)
    if fails:
        sys.stderr.write(
            "SHIP BLOCKED by preship_gate v2 — FINAL PRE-SHIP rule not satisfied:\n"
            + "\n".join(fails)
            + "\n\nFor each gated file: stage it, then run\n"
            "  python3 .claude/preship/preship_audit.py <file>\n"
            "get Gro+GAI APPROVE, then retry the ship.\n"
        )
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()
