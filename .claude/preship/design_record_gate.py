#!/usr/bin/env python3
# ruff: noqa: E501  — dense doc/guidance strings run long (project convention)
"""
design_record_gate.py — PreToolUse hook (Write + Edit): Self-QA gate #4, "BGG design-record"
(Rafael 2026-08-12; board+GAI majority over Gro on Q2 — see logs/design_records/
self_qa_gate4_design_record.md for the full Open Question Protocol alignment this implements).

WHAT IT DOES: blocks a `Write` or `Edit` targeting a NEW file (does not yet exist at git HEAD)
whose extension is .py or .sh and which is "gated" per preship_gate._is_gated() — the SAME source
of truth the ship gate already uses, imported directly so the two gates cannot drift apart —
UNLESS a matching design-record marker (written by record_design.py) already covers that path, OR
an audited waiver (record_design_waiver.py) already covers it.

BASH-CHANNEL COVERAGE (fixed 2026-08-12, Schneier + Beck/Kim board seats, independently
confirmed): this hook is wired ONLY to the Write|Edit PreToolUse matcher (.claude/settings.json),
so a new gated file created via Bash (heredoc, `echo >`, `python3 -c "open(...).write(...)"`)
never reaches this file at all — not a "determined bypass," simply not using the watched tool.
Rather than also wiring this hook to Bash (which would need the same heredoc/redirect/subshell
parsing preship_gate.py already carries — real complexity, not free), the fix is a SHIP-TIME
MIRROR: preship_gate._design_record_ok() enforces the identical check at git commit/push time,
tool-agnostically, exact parity with how checks #1-3 (Gro/GAI marker, cold-2nd, adversarial,
log-evidence) already work. A file that slips past THIS hook via Bash still cannot ship.

THIS IS LAYER 1 ONLY (a local speed bump, same honest framing preship_gate.py uses for itself —
it lives in a directory an agent can edit). Layer 2 (a GitHub branch-protection status check
verifying server-received commit ordering — the actual forgery-resistant wall) is DEFERRED,
needs Rafael's own GitHub admin action, not built this pass. See the design record's §6 for the
full, honest list of what this mechanism does NOT catch (edits to EXISTING files that add new
capability via new functions; new non-.py/.sh files; content-level fabrication of a design doc —
this proves A design-record file exists and precedes the code locally, not that a real design
pass happened).

FAIL-SAFE DIRECTION (asymmetric vs. preship_gate.py's fail-CLOSED on git errors — deliberately,
not an oversight: Peterffy board-seat review confirmed this is the correct call because it tracks
frequency/reversibility, not a fixed rule — preship_gate.py guards a rare, high-blast-radius,
hard-to-reverse action [ship], this guards a frequent, low-blast-radius, fully-reversible one [a
local file write]). Any INTERNAL error (git call fails, marker unreadable, unexpected exception,
even a malformed tool_input payload) fails OPEN — the entire body of main() past stdin-parsing
runs inside one try/except so this is enforced by the code itself, not by an assumption about how
the calling harness treats a non-(0,2) exit code. A WARNING is written to stderr on every fail-open
path so a degraded gate leaves a breadcrumb rather than being silently ineffective (Groq preship
review). The BLOCK path fires only via the specific, unit-tested (test_design_record_gate.py)
"new + gated + .py/.sh + no marker + no waiver" branch, never a bare exception fallthrough.

Exit 0 = allow. Exit 2 = block (stderr shown to Claude).
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

REPO = os.path.dirname(os.path.dirname(_HERE))
_REPO_REAL = os.path.realpath(REPO)
MARKER_DIR = os.path.join(REPO, ".claude", "preship", "markers")
MARKER_MAX_AGE_SEC = 24 * 3600
_GATED_EXTS = (".py", ".sh")
_GIT_TIMEOUT_SEC = 5   # Peterffy review: 15s was too long — a degraded git state (lock
                       # contention, mid-repack) should degrade to fail-open QUICKLY, not impose
                       # a repeated 15s tax on every subsequent edit for the rest of a session.

_is_gated: Optional[Callable[[str], bool]]
try:
    from preship_gate import _is_gated  # single source of truth — see module docstring
except Exception:
    _is_gated = None  # if the import itself fails, main() fails open (see below)


def _to_repo_relpath(raw: str) -> str:
    """Normalize a tool_input.file_path into a repo-relative, traversal-safe path.

    Uses realpath+commonpath containment (Beck/Kim + Peterffy boards, independently: matches the
    idiom preship_gate._handle_rsync already uses for the identical "is this path inside the
    repo" question — a naive `raw.startswith(REPO)` string check would false-match a sibling
    directory sharing a prefix, e.g. 'alpaca-mtf-bot_FINAL_backup/'). Falls back to raw (minus a
    leading './') if raw is not an absolute path under the repo at all — the .py/.sh + _is_gated
    checks downstream correctly no-op on anything that isn't a real repo-relative gated path.
    normpath collapses any '..' segments so a crafted 'a/../b' cannot dodge a prefix match against
    a DIFFERENT resolved location than the one actually written (GAI preship finding, verified)."""
    if os.path.isabs(raw):
        real = os.path.realpath(raw)
        try:
            if os.path.commonpath([real, _REPO_REAL]) == _REPO_REAL:
                raw = os.path.relpath(real, _REPO_REAL)
        except ValueError:
            pass  # different drive / not comparable — leave raw as-is, downstream checks no-op
    while raw.startswith("./"):
        raw = raw[2:]
    return os.path.normpath(raw) if raw else raw


def _exists_at_head(relpath: str) -> Optional[bool]:
    """True = relpath has a blob at HEAD (an EDIT, not new). False = git CONFIRMS it genuinely
    does not exist at HEAD (proceed to marker/waiver checks — this is the normal new-file path).
    None = the check itself was inconclusive (a git error that is NOT the specific "path does not
    exist" message — corrupt repo, unborn HEAD, permission issue, etc.) — the caller MUST treat
    None as "cannot determine, fail open immediately", never as "assume new".

    Cold-2nd review, 2026-08-12 (verified real): an earlier version returned a plain bool
    (`r.returncode == 0`), so ANY non-zero exit — including a genuine git error unrelated to the
    file's existence — collapsed to the same "False" as a confirmed-absent file, and fell through
    to the marker/waiver checks, which could then reach _block() on a legitimate EXISTING file
    edit. That directly contradicted this docstring's own promise. Distinguishing on git's actual
    stderr message ("fatal: path '...' does not exist in 'HEAD'" — verified verbatim against a
    live git call) makes the fail-open guarantee true in code, not just in prose."""
    try:
        r = subprocess.run(["git", "-C", REPO, "cat-file", "-e", f"HEAD:{relpath}"],
                            capture_output=True, timeout=_GIT_TIMEOUT_SEC)
    except Exception:
        return None
    if r.returncode == 0:
        return True
    if b"does not exist" in r.stderr:
        return False
    return None


def _design_marker_covers(relpath: str):
    """A fresh design-record marker whose `covers` list includes relpath (exact match, or a
    directory-prefix entry ending in '/'), with doc_sha256 still matching the CURRENT on-disk
    design-record file. Returns (True, slug) or (False, None)."""
    if not os.path.isdir(MARKER_DIR):
        return False, None
    for name in os.listdir(MARKER_DIR):
        if not (name.startswith("design__") and name.endswith(".json")):
            continue
        try:
            m = json.load(open(os.path.join(MARKER_DIR, name)))
            if not isinstance(m, dict):
                raise ValueError(f"marker content is {type(m).__name__}, not an object")
        except Exception as e:
            # RC-3: a corrupt/unreadable/non-object marker is skipped (not fatal to the
            # scan), but not silently — surfaced on stderr so a bad marker file is
            # discoverable, not invisible. Cold-2nd fix: this previously only wrapped
            # json.load() itself — a syntactically-valid-but-non-dict marker (e.g. `[]`
            # or `null`, plausible from a botched hand-edit) raised AttributeError on
            # the .get() calls below, UNCAUGHT, aborting the whole scan rather than
            # skipping just this one marker as the comment claimed.
            sys.stderr.write(f"design_record_gate: skipping unreadable marker {name}: {e}\n")
            continue
        covers = m.get("covers") or []
        # isinstance(c, str) guard (cold-2nd 2026-08-23): a marker with a non-string
        # covers element (e.g. `"covers":[null]` / `[12345]` — the hand-edit threat class)
        # otherwise raises AttributeError on `c.endswith`, failing the hook OPEN.
        hit = any(
            isinstance(c, str) and ((c == relpath) or (c.endswith("/") and relpath.startswith(c)))
            for c in covers
        )
        if not hit:
            continue
        # GAI preship finding, verified real (2026-08-12): `.get("doc_path", "")`'s
        # default only applies when the KEY IS ABSENT, not when it's present with a
        # None value — a marker containing `"doc_path": null` (a corrupt/hand-edited
        # file, the exact threat class this whole scan already defends against) made
        # `doc` None, and os.path.join(REPO, None) raised an uncaught TypeError the
        # surrounding `except OSError` never caught. `or ""` catches None (and every
        # other falsy value) the same way missing does; os.path.join(REPO, "") is
        # just REPO itself, and open()-ing a directory correctly raises
        # IsADirectoryError — a subclass of OSError — so it falls through to the
        # existing "cannot validate, skip this marker" path below, not a crash.
        doc = m.get("doc_path") or ""
        doc_abs = os.path.join(REPO, doc)
        try:
            with open(doc_abs, "rb") as f:
                cur_sha = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            # RC-3: design doc missing/unreadable — this marker cannot validate; surfaced, not
            # swallowed, since it means a previously-valid marker just went stale silently.
            sys.stderr.write(
                f"design_record_gate: marker {name} covers {relpath!r} but its design doc "
                f"{doc!r} is unreadable ({e}) — cannot validate coverage\n")
            continue
        if cur_sha == m.get("doc_sha256"):
            return True, m.get("feature")
    return False, None


def _waiver_covers(relpath: str) -> bool:
    """A fresh design-record waiver bound to relpath. Presence + freshness + a non-empty reason
    is sufficient — matches the low-friction escape-valve intent (design record §4); the waiver
    script's own content-sha binding (when a sha is obtainable) is for the audit trail, not
    re-verified here."""
    marker_path = os.path.join(MARKER_DIR, relpath.replace("/", "__") + ".designwaiver.json")
    if not os.path.exists(marker_path):
        return False
    try:
        m = json.load(open(marker_path))
        if not isinstance(m, dict):
            return False
    except Exception:
        return False
    # `.get("ts") or 0`, NOT `.get("ts", 0)` — cold-2nd finding, 2026-08-12 (same bug
    # class as the doc_path fix above: a present "ts": null would otherwise raise on
    # `time.time() - None`). Lower severity here than in preship_gate.py's siblings —
    # this line sits OUTSIDE the try above, but main()'s own outer try/except still
    # catches it and fails OPEN, this module's documented direction — fixed anyway for
    # consistency with the pattern now applied everywhere else it appears.
    if time.time() - (m.get("ts") or 0) > MARKER_MAX_AGE_SEC:
        return False
    return bool(str(m.get("reason", "")).strip())


def _block(relpath: str) -> None:
    sys.stderr.write(
        f"DESIGN-RECORD GATE (blocking, Self-QA #4): {relpath!r} is a NEW file under a gated "
        "path with no registered design-record covering it.\n"
        "Either:\n"
        "  (a) it IS new capability — run the Feature Design Protocol / Open Question Protocol "
        "(board + Gro + GAI, neutral prompts — the bias gate is active), write "
        "logs/design_records/<feature>.md, then:\n"
        f"        python3 .claude/preship/record_design.py <feature-slug> --doc "
        f"logs/design_records/<feature>.md --covers {relpath}\n"
        "  (b) it is NOT new capability (a fix, a test file, a scratch script) — this requires "
        "Rafael's own go-ahead in the current chat message, then:\n"
        f"        python3 .claude/preship/record_design_waiver.py {relpath} --reason "
        "\"<why>\"\n"
        "Full spec: logs/design_records/self_qa_gate4_design_record.md\n"
    )
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # unparseable stdin — fail open, we only see this tool's own input

    # Peterffy review (verified real): this block previously sat OUTSIDE the fail-open try/except
    # below — a malformed tool_input (e.g. file_path as a non-string) could raise an uncaught
    # AttributeError, contradicting the module's own "never a bare exception fallthrough" claim
    # and relying on an unstated assumption about how the harness treats a non-(0,2) exit code.
    # Now everything past stdin-parsing shares ONE fail-open boundary.
    try:
        tool_input = payload.get("tool_input", {}) or {}
        relpath = tool_input.get("file_path", "") or ""
        if not relpath or not isinstance(relpath, str):
            sys.exit(0)
        relpath = _to_repo_relpath(relpath)

        if _is_gated is None:
            # Groq preship finding (verified real): the source-of-truth import failing is a MORE
            # suspicious condition than a transient per-call error — surface it, even though we
            # still fail open (blocking every write on a broken import would be worse).
            sys.stderr.write(
                "design_record_gate: WARNING — could not import _is_gated from preship_gate.py; "
                "failing OPEN (this gate is not evaluating any file until this is fixed)\n")
            sys.exit(0)
        _, ext = os.path.splitext(relpath)
        if ext not in _GATED_EXTS:
            sys.exit(0)
        if not _is_gated(relpath):
            sys.exit(0)
        _head_check = _exists_at_head(relpath)
        if _head_check is None:
            # Cold-2nd fix: an INCONCLUSIVE head-check (not a confirmed "doesn't
            # exist") must fail open here, immediately — never fall through to the
            # marker/waiver checks, which could otherwise reach _block() on what may
            # well be a legitimate existing-file edit.
            sys.stderr.write(
                f"design_record_gate: WARNING — could not determine whether "
                f"{relpath!r} exists at HEAD (ambiguous git result); failing OPEN\n")
            sys.exit(0)
        if _head_check:
            sys.exit(0)  # editing an existing file — not this gate's trigger (see §6)
        covered, _feature = _design_marker_covers(relpath)
        if covered:
            sys.exit(0)
        if _waiver_covers(relpath):
            sys.exit(0)
    except SystemExit:
        raise
    except Exception as e:
        # Groq preship finding (verified real): fail open, but leave a breadcrumb — a fully
        # silent fail-open gives the operator zero signal the control degraded.
        sys.stderr.write(
            f"design_record_gate: WARNING — internal error evaluating the pending write, "
            f"failing OPEN: {e}\n")
        sys.exit(0)

    _block(relpath)


if __name__ == "__main__":
    main()
