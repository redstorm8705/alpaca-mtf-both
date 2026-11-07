#!/usr/bin/env python3
# ruff: noqa: E501
"""approval_ready.py — APPROVAL-READY-1106 (logs/design_records/approval_ready_2026-11-06.md).

No approval ask reaches the owner until his first reply would record. Two modes, one set of checks:

  CLI   python3 .claude/preship/approval_ready.py <ID> [<ID> ...] [--ask <file>]
        prints READY or every missing item for each ID (exit 0 when all READY, 1 otherwise).
  Stop  python3 .claude/preship/approval_ready.py --hook    (Stop hook; JSON on stdin)
        when the turn-ending assistant message asks the owner to approve an Approval-ID, runs the checks for every
        ID it asks about and exits 2 (blocking) on any failure. A message that asks for no approval always passes.
        stop_hook_active is ignored on purpose: the checks are deterministic, so an unfixed ask fails again.

Every check reuses owner_approval_capture / required_questions / preship_gate so the pre-ask check and the recorder
cannot drift apart. Read-only except register_designs (the capture hook's own first-seen registry).
"""
import glob
import json
import os
import re
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import owner_approval_capture as oac  # noqa: E402
import required_questions as rq  # noqa: E402

_PATH = re.compile(r"(?<![\w.\-/])(/?(?:[\w.\-]+/)*[\w\-][\w.\-]*\.(?:py|sh|json|yaml|yml|toml|ini|cfg|md))(:\d+)?")
_NEW = re.compile(r"\s*\(new\)\s*$")
_NOT_CHANGED = re.compile(r"(?im)^Not changed:[ \t]*(.+)$")
_CHOICES = re.compile(r"(?im)^Owner choices:[ \t]*(.+)$")
_REPLY = re.compile(r"\bAPPROVE\s+(" + oac._ID + r")")       # an instructed reply names an ID-shaped token


def _repo_rel(p: str, root: str) -> str:
    """An absolute path inside ANY worktree of this repo (main checkout included), or './x', as the repo-relative
    path; an absolute path outside every worktree → ''."""
    if p.startswith("/"):
        rp = os.path.realpath(p)
        best = ""
        for wt in [root] + oac.worktree_roots(root):    # longest match: worktrees sit inside the main checkout
            real = os.path.realpath(wt)
            if rp.startswith(real + os.sep) and len(real) > len(best):
                best = real
        return os.path.relpath(rp, best) if best else ""
    return p[2:] if p.startswith("./") else p


def _worktree_of(path: str, root: str) -> str:
    """The worktree that holds `path` (longest matching root), else `root`."""
    rp = os.path.realpath(path)
    best = ""
    for wt in [root] + oac.worktree_roots(root):
        real = os.path.realpath(wt)
        if rp.startswith(real + os.sep) and len(real) > len(best):
            best = real
    return best or root


def _norm(entry: str) -> str:
    return _NEW.sub("", entry.strip()).strip().strip("`")


def _git_lines(root: str, *args) -> list:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []


def _is_gated(path: str) -> bool:
    import preship_gate  # local import: preship_gate is heavy and only check 4 needs it
    return preship_gate._is_gated(path)


def covers_gaps(text: str, covers: list, root: str, diff_root: "str | None" = None) -> list:
    """Check 4: gated repo files the record names that are neither in Covers nor on a verified 'Not changed:' line.
    A `path:NNN` citation is evidence, not a file the design changes, and is exempt. `diff_root` is the worktree that
    holds the record; its own branch diff verifies the 'Not changed:' line."""
    diff_root = diff_root or root
    tracked_set = set(_git_lines(root, "ls-files"))
    cov = {_norm(c) for c in covers}
    not_changed: set = set()
    for line in _NOT_CHANGED.findall(text):
        not_changed |= {_norm(x) for x in line.split(",") if x.strip()}
    problems = []
    base = (_git_lines(diff_root, "merge-base", "origin/main", "HEAD") or ["HEAD"])[0]   # no origin → uncommitted only
    changed = set(_git_lines(diff_root, "diff", "--name-only", base))   # this branch's commits + working tree
    changed |= set(_git_lines(diff_root, "ls-files", "--others", "--exclude-standard"))   # + new untracked files
    for nc in sorted(not_changed):
        if nc in changed:
            problems.append(f"'{nc}' is listed under 'Not changed:' but this worktree changes it")
    named = set()
    for m in _PATH.finditer(text):
        if m.group(2):                                  # file:line citation → evidence
            continue
        p = _repo_rel(m.group(1), root)
        if not p or p.startswith("logs/"):
            continue
        is_new = re.match(r"\s*\(new\)", text[m.end():], re.I) is not None
        if p in tracked_set or is_new:                  # a repository path: tracked, or marked (new)
            named.add(p)
    for p in sorted(named):
        if p in cov or p in not_changed or not _is_gated(p):
            continue
        problems.append(f"names gated file '{p}' that is not in Covers (add it, or list it on a 'Not changed:' line)")
    return problems


def check(root: str, aid: str, ask_text: str = "") -> list:
    """Every reason `aid` would not record on the owner's first reply. [] means READY."""
    problems = []
    decls = oac._declarations(root).get(aid) or []
    if not decls:
        return [f"no design record declares 'Approval-ID: {aid}'"]
    shas = {oac.covers_sha(c) for _, c, _ in decls}
    bases = {os.path.basename(p) for p, _, _ in decls}
    if len(shas) != 1 or len(bases) != 1:
        ref = oac.covers_sha(decls[0][1])
        bad = [p for p, c, _ in decls if oac.covers_sha(c) != ref]
        problems.append(f"worktree copies disagree (Covers or file name); differing copies: {bad or [p for p, _, _ in decls]}")
    for path, _c, _b in decls:                          # checks 2 and owner numbers on EVERY copy
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as exc:
            problems.append(f"copy unreadable: {path} ({exc})")
            continue
        gaps = rq.unanswered(text)
        if gaps:
            problems.append(f"required questions unanswered {gaps} in {path}")
        if oac.parse_owner_numbers(text) is None:
            problems.append(f"malformed 'Owner numbers:' line in {path}")
    found = oac.find_design(root, aid)
    if not found:
        return problems or [f"find_design cannot resolve {aid}"]
    path, covers = found
    real_root = os.path.realpath(root)
    here = [p for p, _c, _b in decls if os.path.realpath(p).startswith(real_root + os.sep)]
    path = here[0] if here else path                    # prefer this worktree's copy (copies agree per check 1)
    if not covers:
        problems.append("Covers list is missing or empty")
    text = open(path, encoding="utf-8").read()
    problems += covers_gaps(text, covers, root, _worktree_of(path, root))
    for line in _CHOICES.findall(text):                 # check 5
        for item in (x.strip() for x in line.split(";") if x.strip()):
            name, _, opts = item.partition("=")
            words = [name.strip()] + [o.strip() for o in opts.split("|") if o.strip()]
            missing = [w for w in words if w and w.lower() not in (ask_text or "").lower()]
            if missing:
                problems.append(f"owner choice '{item}' is not put to the owner in the ask (missing {missing})")
    rec_path = os.path.join(oac.approvals_dir(root), f"{aid}.json")     # check 7
    try:
        with open(rec_path) as fh:
            rec = json.load(fh)
        if isinstance(rec, dict) and rec.get("status") == "APPROVED" \
                and rec.get("covers_sha256") == oac.covers_sha(covers) and oac.approval_valid_cached(rec, root):
            problems.append("an approval is already on record for these exact files; asking again is a double approval")
    except (OSError, ValueError):                       # missing or unreadable store file: no valid approval on record
        pass
    return problems


def simulate_replies(root: str, asked: list, ask_text: str) -> dict:
    """Check 6: the replies the ask tells the owner to send — and a bare 'approved' when one ID is asked — must
    record exactly the asked IDs through the capture hook's own resolve(). Returns {asked ID: [problems]}."""
    out: dict = {a: [] for a in asked}
    pend = oac.pending_ids(root)
    replies = {m.group(0): m.group(1) for m in _REPLY.finditer(ask_text)} or {f"APPROVE {a}": a for a in asked}
    got: set = set()
    for r, target in replies.items():
        ok, _rej, note = oac.resolve(r, ask_text, root, pend)
        got |= set(ok)
        owner = target if target in out else asked[0]
        if not ok or [x for x in ok if x not in asked]:
            out[owner].append(f"the reply '{r}' would record {ok or 'nothing'}{' (' + note + ')' if note else ''}")
    for a in asked:
        if a not in got:
            out[a].append(f"no reply the ask gives would record {a}")
    instructed = {m.group(1) for m in _REPLY.finditer(ask_text)}
    for a in asked:
        if a not in pend and a not in instructed:       # an earlier approval exists (now stale): only the explicit
            out[a].append("an earlier approval of this ID is on record, so a bare 'approved' will not record; the ask "
                          f"must tell the owner to send 'APPROVE {a}'")
    if len(asked) == 1 and asked[0] in pend:
        ok, _rej, note = oac.resolve("approved", ask_text, root, pend)
        if ok != asked:
            out[asked[0]].append(f"a bare 'approved' would record {ok or 'nothing'}{' (' + note + ')' if note else ''}")
    return out


def _on_record(root: str, aid: str) -> bool:
    """True when a valid approval for the design's CURRENT Covers is in the store."""
    found = oac.find_design(root, aid)
    if found is None:
        return False
    try:
        with open(os.path.join(oac.approvals_dir(root), f"{aid}.json")) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(rec, dict) and rec.get("status") == "APPROVED" \
        and rec.get("covers_sha256") == oac.covers_sha(found[1]) and oac.approval_valid_cached(rec, root)


def _declared_ids(root: str) -> list:
    """Every declared natural Approval-ID, including ones whose worktree copies disagree (check 1 reports those)."""
    return sorted(i for i in oac._declarations(root) if oac._NATURAL_ID.match(i))


def asked_ids(root: str, ask_text: str) -> list:
    """IDs the message asks the owner to approve. Pending IDs: the capture hook's own ask detection. An ID already
    approved counts only when the message tells the owner to send 'APPROVE <ID>' for it (a real re-ask), so a report
    such as 'your decision on X is recorded' is never treated as an ask."""
    declared = _declared_ids(root)
    have = {os.path.basename(p)[:-len(".json")] for p in glob.glob(os.path.join(oac.approvals_dir(root), "*.json"))}
    named = [i for i in declared if i in have and oac._mentions(ask_text or "", [i])]
    stale = [i for i in named if not _on_record(root, i)]      # store file exists but no longer valid (e.g. Covers changed)
    pending = [i for i in declared if i not in have] + stale
    asked = list(oac.ids_requested(ask_text, pending))
    asked += [i for i in named if i not in stale and i not in asked and _reasked(ask_text or "", i)]
    return asked


_INSTRUCT = re.compile(r"(?i)^\W*(?:please\s+)?(?:reply|send|type|say|respond)\b(?:\s+with)?\s*[`'\"]?\s*APPROVE\s+(\S+?)[`'\".!]*\s*$")
_ASK_Q = re.compile(r"(?i)^\W*do\s+you\s+approve\b[^?\n]*\?")   # the sentence itself is the question, not a quote


def _reasked(text: str, aid: str) -> bool:
    """A real re-ask of an ID already on record: a sentence that is only an instruction to send 'APPROVE <ID>'
    ('Reply APPROVE X.'), or a direct question 'Do you approve X?'. A report that quotes a past reply ('your reply
    APPROVE X was recorded') is neither."""
    for s in re.split(r"(?<=[.!?])\s+|\n+", text):
        m = _INSTRUCT.match(s.strip())
        if m and m.group(1).strip("`'\".!") == aid:
            return True
        q = _ASK_Q.search(s.strip())
        if q and aid in q.group(0) and not oac._NEGATED_ASK.search(s):
            return True
    return False


def evaluate(root: str, ask_text: str, ids: "list | None" = None) -> dict:
    ids = ids if ids is not None else asked_ids(root, ask_text)
    out = {a: check(root, a, ask_text) for a in ids}
    pending_asked = [a for a in ids if not any("already on record" in p for p in out[a])]
    if ask_text and pending_asked:
        for a, probs in simulate_replies(root, pending_asked, ask_text).items():
            out[a] += probs
    return out


def _report(result: dict) -> str:
    lines = []
    for aid, probs in result.items():
        if probs:
            lines.append(f"APPROVAL-READY {aid}: NOT READY")
            lines += [f"  - {p}" for p in probs]
        else:
            lines.append(f"APPROVAL-READY {aid}: READY")
    return "\n".join(lines)


def _hook() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0                                        # no input → nothing to judge
    sys.path.insert(0, HERE)
    import execute_dont_ask_gate as edg
    last = edg._last_assistant_text(data.get("transcript_path", ""))
    if not last or not oac._AGENT_ASKS.search(last):
        return 0                                        # no approval wording → never an ask
    root = data.get("cwd") or oac.project_dir()
    try:
        asked = asked_ids(root, last)
    except Exception:
        sys.stderr.write("APPROVAL-READY: could not tell whether this message asks for an approval (not blocking):\n"
                         + traceback.format_exc())
        return 0                                        # a message with no detected ask always passes
    if not asked:
        return 0
    try:
        result = evaluate(root, last, asked)
        if register := getattr(oac, "register_designs", None):
            if all(not p for p in result.values()):
                register(root)
    except Exception:
        sys.stderr.write("APPROVAL-READY (blocking): the readiness check raised while this message asks for an "
                         "approval — remove the approval ask, or fix the cause:\n" + traceback.format_exc())
        return 2
    if any(result.values()):
        sys.stderr.write("APPROVAL-READY GATE (blocking): this message asks the owner to approve, but his first "
                         "reply would not record. Remove the approval ask, or fix the record:\n" + _report(result) + "\n")
        return 2
    sys.stderr.write(_report(result) + "\n")
    return 0


def main(argv: list) -> int:
    if "--hook" in argv:
        return _hook()
    ask = ""
    if "--ask" in argv:
        i = argv.index("--ask")
        ask = open(argv[i + 1], encoding="utf-8").read()
        argv = argv[:i] + argv[i + 2:]
    ids = [a for a in argv if not a.startswith("-")]
    root = oac.project_dir()
    result = evaluate(root, ask, ids or None)
    if not result:
        print("no Approval-ID asked or named")
        return 1
    print(_report(result))
    return 0 if not any(result.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
