#!/usr/bin/env python3
# ruff: noqa: E501  — dense help/doc strings run long (project convention)
"""
record_design.py — register a DESIGN-RECORD marker proving a design pass happened for a set of
implementation paths (Self-QA gate #4, "BGG design-record" — Rafael 2026-08-12, board+GAI
majority over Gro on Q2; full alignment in
logs/design_records/self_qa_gate4_design_record.md).

    python3 .claude/preship/record_design.py <feature-slug> --doc <design_record.md path>
        --covers <path1> [<path2> ...]

WHAT IT ATTESTS: a design-record file (the Open Question Protocol's output — board/Gro/GAI
positions on the open forks) already exists on disk for a feature, and names WHICH new
implementation paths that design pass covers. `design_record_gate.py` (the PreToolUse hook)
requires a marker like this — with a doc_sha256 still matching the CURRENT design-record file —
before it will allow a `Write` to a NEW file at a covered path.

Binds to the design-record file's content sha256, NOT the implementation file's (there is no
implementation yet when this runs — design precedes code, that is the entire point). If the
design doc is edited after registration, the marker's sha256 stops matching and the gate
re-blocks until re-registered — same "any edit invalidates the marker" property every other
marker in this repo already has (cold-2nd, adversarial, log-evidence).

`--covers` accepts exact paths or a directory prefix ending in "/" (covers every new file under
that directory). Repo-relative paths only.

`--doc` must resolve INSIDE logs/design_records/ (Schneier board-seat review, 2026-08-12: an
unrestricted --doc let a marker validate against ANY readable file, anywhere, undermining the
"git-history auditability" this whole mechanism leans on as its real backstop once you accept
there is no cryptographic proof of who ran this script). Containment is checked via realpath +
commonpath (cold-2nd finding, same pass: an earlier version used a naive string-prefix check —
`doc.startswith("logs/design_records/")` — which a crafted `logs/design_records/../../<any
readable file>` satisfied at the STRING level while `open()` resolved the `..` at the OS level
and read a completely different file outside the intended directory. commonpath resolves the
path FIRST, so it cannot be fooled by traversal segments — same fix already applied correctly in
design_record_gate.py's own _to_repo_relpath for the analogous problem).
"""
import sys
import os
import json
import time
import hashlib

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOC_DIR_REAL = os.path.realpath(os.path.join(REPO, "logs", "design_records"))
MARKER_DIR = os.path.join(REPO, ".claude", "preship", "markers")


def _file_sha256(relpath: str):
    abspath = os.path.join(REPO, relpath)
    try:
        with open(abspath, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _argval(argv, flag):
    """Value after `flag`, or None if absent/trailing — never IndexErrors."""
    if flag not in argv:
        return None
    i = argv.index(flag)
    return argv[i + 1] if i + 1 < len(argv) else None


def _covers_list(argv):
    if "--covers" not in argv:
        return []
    i = argv.index("--covers")
    out = []
    for tok in argv[i + 1:]:
        if tok.startswith("--"):
            break
        out.append(tok)
    return out


def main(argv):
    if not argv or argv[0].startswith("-"):
        sys.stderr.write(__doc__)
        return 2
    slug = argv[0]
    doc = _argval(argv, "--doc")
    covers = _covers_list(argv[1:])
    if not doc:
        sys.stderr.write('record_design: --doc "<path to the design-record .md>" is required\n')
        return 2
    if not covers:
        sys.stderr.write(
            "record_design: --covers <path1> [<path2> ...] is required — name which "
            "implementation path(s) this design pass covers\n")
        return 2
    while doc.startswith("./"):
        doc = doc[2:]
    doc_real = os.path.realpath(os.path.join(REPO, doc))
    try:
        _contained = os.path.commonpath([doc_real, _DOC_DIR_REAL]) == _DOC_DIR_REAL
    except ValueError:
        _contained = False  # different drive / not comparable — treat as NOT contained
    if not _contained:
        sys.stderr.write(
            f"record_design: --doc must resolve inside logs/design_records/ (got {doc!r}, "
            f"resolves to {doc_real!r}) — this keeps every design record in one auditable, "
            "greppable location, immune to a '..' escape\n")
        return 2
    doc_sha = _file_sha256(doc)
    if not doc_sha:
        sys.stderr.write(f"record_design: cannot read design-record file at {doc!r}\n")
        return 1
    norm_covers = []
    for c in covers:
        c = c[2:] if c.startswith("./") else c
        norm_covers.append(c)
    os.makedirs(MARKER_DIR, exist_ok=True)
    marker_path = os.path.join(MARKER_DIR, f"design__{slug}.json")
    # Peterffy board-seat review, 2026-08-12: atomic write (temp file + os.replace) — plain
    # open(path,"w") + json.dump left a window where a concurrent reader could see a
    # partially-written / torn marker file. design_record_gate.py's per-marker try/except
    # already treats an unreadable marker as "skip, not fatal" (a transient, self-healing false
    # BLOCK, never a false ALLOW), so this was never unsafe — this closes the window anyway,
    # matching the atomic-write convention (RC-5) this project already applies to state files.
    tmp_path = marker_path + f".tmp{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump({
            "feature": slug,
            "doc_path": doc,
            "doc_sha256": doc_sha,
            "covers": norm_covers,
            "ts": time.time(),
        }, f, indent=2)
    os.replace(tmp_path, marker_path)
    print(f"design-record registered: {slug}")
    print(f"  doc: {doc}  sha256 {doc_sha[:16]}...")
    print(f"  covers: {norm_covers}")
    print(f"  marker: {os.path.relpath(marker_path, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
