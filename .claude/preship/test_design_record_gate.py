#!/usr/bin/env python3
"""Regression suite for design_record_gate.py (Self-QA gate #4).

TESTS THE STAGED BLOB, NOT THE WORKING TREE — same discipline as test_gate.py
(2026-07-22: an earlier gate reported 19/19 while the STAGED blob still contained
two verified bypasses). Loads its module from
`git cat-file blob :.claude/preship/design_record_gate.py` so a result can never be
mistaken for validation of unstaged edits.

Added 2026-08-12 in response to the Peterffy and Beck/Kim board-seat reviews of this
same gate, both of which independently flagged that its "tested" claim (in its own
docstring and in logs/design_records/self_qa_gate4_design_record.md) was unbacked by
any actual test file — exactly the documentation-vs-reality gap this project's
Self-QA gates #2/#3 exist to catch. Every case below maps to a specific reviewer
finding, named in its comment.

Run:  python3 .claude/preship/test_design_record_gate.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
REL = ".claude/preship/design_record_gate.py"
PG_REL = ".claude/preship/preship_gate.py"


def _staged_source(relpath: str) -> bytes:
    r = subprocess.run(["git", "-C", REPO, "cat-file", "blob", f":{relpath}"],
                        capture_output=True, timeout=15)
    if r.returncode != 0:
        sys.exit(f"FAIL: {relpath} is NOT STAGED — the shipping artifact cannot be "
                  f"tested.\n      git add {relpath}  and re-run.")
    return r.stdout


STAGED = _staged_source(REL)
with open(os.path.join(HERE, "design_record_gate.py"), "rb") as fh:
    WORKTREE = fh.read()
DRIFT = STAGED != WORKTREE

dg = types.ModuleType("dg_staged")
dg.__file__ = os.path.join(HERE, "design_record_gate.py")   # keeps REPO derivation ok
exec(compile(STAGED, f"{REL}<staged>", "exec"), dg.__dict__)

# preship_gate.py's own copy of the design-record check (_design_record_ok, the
# ship-time mirror) previously had ZERO direct test coverage — flagged by cold-2nd
# review round 3/4, which is exactly how round 4 found a real, unfixed instance of
# the "ts": null bug at a line no test ever exercised. Loaded the same staged-blob
# way test_gate.py already loads this file, so this suite can test both halves of
# gate #4 without a second file.
PG_STAGED = _staged_source(PG_REL)
with open(os.path.join(HERE, "preship_gate.py"), "rb") as fh:
    PG_WORKTREE = fh.read()
PG_DRIFT = PG_STAGED != PG_WORKTREE

pg = types.ModuleType("pg_staged")
pg.__file__ = os.path.join(HERE, "preship_gate.py")
exec(compile(PG_STAGED, f"{PG_REL}<staged>", "exec"), pg.__dict__)

bad = 0
total = 0


def check(name: str, got, expected) -> None:
    global bad, total
    total += 1
    ok = got == expected
    if not ok:
        bad += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: got={got!r} expected={expected!r}")


def run_hook(payload: dict):
    """Feed payload to the STAGED file as an actual subprocess (end-to-end),
    returning (exit_code, stderr). Slower than in-process calls but exercises the
    real entry point, including stdin parsing — the part unit-testing dg's
    functions directly cannot cover."""
    r = subprocess.run(
        [sys.executable, os.path.join(HERE, "design_record_gate.py")],
        input=json.dumps(payload).encode(), capture_output=True, timeout=15)
    return r.returncode, r.stderr.decode()


def main():
    print(f"module under test: STAGED blob :{REL} ({len(STAGED)} bytes)")

    # --- _to_repo_relpath: path normalization / traversal / containment -----------
    # GAI preship finding 3B: a crafted ".." segment must resolve BEFORE prefix
    # matching, not survive as a literal string a naive startswith() could be
    # fooled by.
    check("normpath collapses ..",
          dg._to_repo_relpath("execution/../data/evil.py"), "data/evil.py")
    check("clean relative path is a no-op",
          dg._to_repo_relpath("execution/foo.py"), "execution/foo.py")
    check("leading ./ stripped",
          dg._to_repo_relpath("./execution/foo.py"), "execution/foo.py")
    # Peterffy + Beck/Kim board seats (independently): naive `startswith(REPO)` false-
    # matches a sibling directory sharing a string prefix. realpath+commonpath must not.
    _sibling = REPO + "_backup"
    check("sibling dir sharing a string prefix is NOT treated as inside the repo",
          dg._to_repo_relpath(os.path.join(_sibling, "execution", "foo.py")),
          os.path.join(_sibling, "execution", "foo.py"))
    check("a genuine absolute repo path normalizes to repo-relative",
          dg._to_repo_relpath(os.path.join(REPO, "execution", "foo.py")),
          "execution/foo.py")

    # --- _exists_at_head: tri-state (True / False / None), cold-2nd fix 2026-08-12 ---
    # An earlier version collapsed a genuine "does not exist" AND an unrelated git
    # error to the same falsy result, which could reach _block() on a legitimate
    # existing-file edit. Confirms the tri-state contract directly, not just via the
    # e2e cases below (which only exercise the True/False paths, not None).
    check("existing file at HEAD -> True",
          dg._exists_at_head("main.py"), True)
    check("genuinely-absent path -> False (confirmed by git's own message)",
          dg._exists_at_head("this_file_does_not_exist_anywhere_xyz.py"), False)

    # --- _design_marker_covers / _waiver_covers, isolated via a temp MARKER_DIR ---
    with tempfile.TemporaryDirectory() as tmp:
        orig_marker_dir = dg.MARKER_DIR
        dg.MARKER_DIR = tmp
        try:
            doc_path = os.path.join(tmp, "doc.md")
            with open(doc_path, "w") as f:
                f.write("design record contents")
            import hashlib
            doc_sha = hashlib.sha256(open(doc_path, "rb").read()).hexdigest()

            # Exact-path coverage
            with open(os.path.join(tmp, "design__feat_a.json"), "w") as f:
                json.dump({"feature": "feat_a", "doc_path": doc_path,
                           "doc_sha256": doc_sha, "covers": ["strategy/new_thing.py"],
                           "ts": time.time()}, f)
            check("exact-path marker covers its exact path",
                  dg._design_marker_covers("strategy/new_thing.py")[0], True)
            check("exact-path marker does NOT cover a different path",
                  dg._design_marker_covers("strategy/other.py")[0], False)

            # Directory-prefix coverage
            with open(os.path.join(tmp, "design__feat_b.json"), "w") as f:
                json.dump({"feature": "feat_b", "doc_path": doc_path,
                           "doc_sha256": doc_sha, "covers": ["events/"],
                           "ts": time.time()}, f)
            check("directory-prefix marker covers a file under it",
                  dg._design_marker_covers("events/new_signal.py")[0], True)
            # This is the GAI 3B scenario, end-to-end through the real matcher: a
            # traversal-crafted path must resolve to its REAL target before this
            # function ever sees it (main() does that via _to_repo_relpath) — this
            # confirms the matcher itself does plain, honest prefix comparison on
            # whatever string it's given, i.e. the fix belongs upstream, which it is.
            check("prefix marker does not cover an unrelated sibling directory",
                  dg._design_marker_covers("events_other/x.py")[0], False)

            # Non-string element in "covers" (cold-2nd 2026-08-23): a marker whose
            # "covers" holds a non-string (`[null]`/`[12345]`, the hand-edit threat
            # class) must NOT crash `c.endswith`. Without the isinstance(c,str) guard
            # the HOOK fails OPEN (waves through every new gated file); the ship-time
            # MIRROR DoS-blocks every commit until the poison marker is removed.
            with open(os.path.join(tmp, "design__nullcover.json"), "w") as f:
                json.dump({"feature": "nullcover", "doc_path": doc_path,
                           "doc_sha256": doc_sha, "covers": [None, 12345],
                           "ts": time.time()}, f)
            check("a non-string covers element does not crash the scan",
                  dg._design_marker_covers("strategy/nullcover_target.py")[0], False)
            # A valid string entry ALONGSIDE the non-string one still matches (the bad
            # element is skipped, not fatal, and does not break a legitimate cover).
            with open(os.path.join(tmp, "design__mixedcover.json"), "w") as f:
                json.dump({"feature": "mixedcover", "doc_path": doc_path,
                           "doc_sha256": doc_sha,
                           "covers": [None, "strategy/mixed_target.py"],
                           "ts": time.time()}, f)
            check("a valid covers entry beside a non-string one still matches",
                  dg._design_marker_covers("strategy/mixed_target.py")[0], True)

            # Stale doc hash invalidates the marker
            with open(doc_path, "w") as f:
                f.write("EDITED — the design doc changed after registration")
            check("edited design doc invalidates its marker (stale sha)",
                  dg._design_marker_covers("strategy/new_thing.py")[0], False)

            # Corrupt marker file is skipped, not fatal (RC-3 fix)
            with open(os.path.join(tmp, "design__corrupt.json"), "w") as f:
                f.write("{not valid json")
            covered, _ = dg._design_marker_covers("strategy/new_thing.py")
            check("a corrupt sibling marker does not crash the scan", covered, False)

            # Syntactically-valid but non-dict marker (cold-2nd fix, 2026-08-12): an
            # earlier version only wrapped json.load() in try/except, so a `[]` or
            # `null` marker raised an UNCAUGHT AttributeError on the .get() calls
            # right after — this must now be skipped exactly like a corrupt one.
            with open(os.path.join(tmp, "design__nondict.json"), "w") as f:
                json.dump([1, 2, 3], f)
            covered, _ = dg._design_marker_covers("strategy/new_thing.py")
            check("a non-dict (list) marker does not crash the scan", covered, False)
            with open(os.path.join(tmp, "design__nulldict.json"), "w") as f:
                json.dump(None, f)
            covered, _ = dg._design_marker_covers("strategy/new_thing.py")
            check("a non-dict (null) marker does not crash the scan", covered, False)

            # doc_path present but null (GAI preship finding, 2026-08-12, verified
            # real): `.get("doc_path", "")`'s default only fires when the KEY is
            # ABSENT, not when it's present-but-None — a dict.get default is not the
            # same thing as `or`. A marker with a covering "covers" entry but
            # "doc_path": null must still be treated as "cannot validate, skip,"
            # never crash the whole scan (an uncaught TypeError from
            # os.path.join(REPO, None) was the actual bug — this exercises the exact
            # covers-hit path that reaches the doc-open code, unlike the two cases
            # above which never get that far).
            with open(os.path.join(tmp, "design__nulldocpath.json"), "w") as f:
                json.dump({"feature": "nulldoc", "doc_path": None,
                           "doc_sha256": "irrelevant",
                           "covers": ["strategy/nulldoc_target.py"],
                           "ts": time.time()}, f)
            covered, _ = dg._design_marker_covers("strategy/nulldoc_target.py")
            check("a marker with doc_path=null does not crash the scan", covered, False)

            # --- waivers ---
            waived_p = os.path.join(tmp, "strategy__waived.py.designwaiver.json")
            with open(waived_p, "w") as f:
                json.dump({"file": "strategy/waived.py", "sha256": None,
                           "reason": "test fixture reason", "ts": time.time()}, f)
            check("fresh waiver with a real reason covers its path",
                  dg._waiver_covers("strategy/waived.py"), True)
            check("waiver does not cover an unrelated path",
                  dg._waiver_covers("strategy/unwaived.py"), False)

            stale_p = os.path.join(tmp, "strategy__stale.py.designwaiver.json")
            with open(stale_p, "w") as f:
                json.dump({"file": "strategy/stale.py", "sha256": None,
                           "reason": "old",
                           "ts": time.time() - dg.MARKER_MAX_AGE_SEC - 1}, f)
            check("a waiver older than MARKER_MAX_AGE_SEC does not cover",
                  dg._waiver_covers("strategy/stale.py"), False)

            empty_p = os.path.join(tmp, "strategy__emptyreason.py.designwaiver.json")
            with open(empty_p, "w") as f:
                json.dump({"file": "strategy/emptyreason.py", "sha256": None,
                           "reason": "   ", "ts": time.time()}, f)
            check("a waiver with a blank/whitespace-only reason does not cover",
                  dg._waiver_covers("strategy/emptyreason.py"), False)
        finally:
            dg.MARKER_DIR = orig_marker_dir

    # --- preship_gate.py's _design_record_ok (the ship-time mirror) — had ZERO ----
    # direct test coverage before this (cold-2nd round 3/4 finding); round 4 found a
    # real, unfixed "ts": null bug at a line exactly this gap let slip through.
    with tempfile.TemporaryDirectory() as tmp2:
        orig_pg_marker_dir = pg.MARKER_DIR
        pg.MARKER_DIR = tmp2
        try:
            # A null timestamp cannot be verified fresh, so `(m.get("ts") or 0)`
            # correctly treats it as epoch-0 — "infinitely stale" — the SAME
            # fail-safe direction as every sibling fix (an invalid/unverifiable
            # value must never be read as "still valid"). The bug this fix actually
            # closes is narrower than "wrong answer": before the fix, `time.time() -
            # None` raised a TypeError that the local `except Exception: pass`
            # already caught, landing on the SAME False result via exception-driven
            # control flow instead of a clean falsy evaluation — this asserts the
            # call completes cleanly (no uncaught exception reaches the test
            # process) and lands on the one CORRECT answer either path must produce.
            waiver_ts_null_p = os.path.join(
                tmp2, "execution__ts_null_target.py.designwaiver.json")
            with open(waiver_ts_null_p, "w") as f:
                json.dump({"file": "execution/ts_null_target.py", "sha256": None,
                           "reason": "cold-2nd round 4 regression fixture",
                           "ts": None}, f)
            covered, why = pg._design_record_ok("execution/ts_null_target.py")
            check("preship_gate: a ts=null waiver completes cleanly and is "
                  "correctly treated as stale/unverifiable, not granted",
                  covered, False)
            check("preship_gate: ts=null waiver's fallback message is the "
                  "no-coverage message, not a crash traceback",
                  "no design-record marker or waiver covering it" in why, True)

            waiver_fresh_p = os.path.join(
                tmp2, "execution__fresh_target.py.designwaiver.json")
            with open(waiver_fresh_p, "w") as f:
                json.dump({"file": "execution/fresh_target.py", "sha256": None,
                           "reason": "fresh fixture", "ts": time.time()}, f)
            covered, _ = pg._design_record_ok("execution/fresh_target.py")
            check("preship_gate: a normal fresh waiver still covers", covered, True)

            covered, _ = pg._design_record_ok("execution/nothing_covers_this.py")
            check("preship_gate: no marker/waiver at all -> not covered",
                  covered, False)

            # Mirror layer non-string "covers" crash-safety (cold-2nd 2026-08-23):
            # SAME bug as the hook, but here it fails CLOSED: a non-string covers
            # element (`[null]`/`[12345]`) raises AttributeError on c.endswith,
            # DoS-blocking EVERY commit until the poison marker is removed. Exercise
            # a design marker (not a waiver) directly against _design_record_ok.
            import hashlib as _hl
            pg_doc = os.path.join(tmp2, "pg_doc.md")
            with open(pg_doc, "w") as f:
                f.write("mirror design record contents")
            pg_doc_sha = _hl.sha256(open(pg_doc, "rb").read()).hexdigest()
            with open(os.path.join(tmp2, "design__pg_nullcover.json"), "w") as f:
                json.dump({"feature": "pg_nullcover", "doc_path": pg_doc,
                           "doc_sha256": pg_doc_sha, "covers": [None, 12345],
                           "ts": time.time()}, f)
            covered, _ = pg._design_record_ok("execution/pg_nullcover_target.py")
            check("preship_gate: a non-string covers element does not crash "
                  "(fails closed cleanly, not a traceback)", covered, False)
        finally:
            pg.MARKER_DIR = orig_pg_marker_dir

    # --- record_design.py --doc containment (cold-2nd fix, 2026-08-12) -------------
    # A HIGH-severity bug the cold-2nd review found and this suite must never regress
    # on: an earlier version checked containment with a naive string prefix
    # (`doc.startswith("logs/design_records/")`), which
    # "logs/design_records/../../<any file>" satisfies at the STRING level while
    # actually resolving OUTSIDE that directory — fully defeating the audit-trail
    # guarantee this flag exists for. Run against the REAL script (not the staged-blob
    # exec trick — record_design.py has its own separate regression identity) via
    # subprocess so the test exercises the actual CLI argument path.
    _rd = os.path.join(HERE, "record_design.py")
    _escape_doc = "logs/design_records/../../README.md"
    r = subprocess.run(
        [sys.executable, _rd, "test_traversal_feature", "--doc", _escape_doc,
         "--covers", "execution/irrelevant_test_path.py"],
        capture_output=True, timeout=15)
    check("record_design.py rejects a '..'-escaping --doc (exit 2, not registered)",
          r.returncode, 2)
    check("rejection stderr names the containment failure, not a generic error",
          b"resolve inside" in r.stderr, True)

    # --- end-to-end via the real entry point (subprocess) --------------------------
    # Uses the REAL, currently-registered markers on disk (this feature's own
    # bootstrap marker covers the three new files) — these three assertions are the
    # same scenarios verified manually during the review pass, now committed as a
    # repeatable regression.
    rc, _ = run_hook({"tool_input": {"file_path": "execution/e2e_test_new_thing.py"}})
    check("e2e: new gated .py with no marker -> BLOCK (exit 2)", rc, 2)

    rc, _ = run_hook({"tool_input": {"file_path": "main.py"}})
    check("e2e: existing file (exists at HEAD) -> ALLOW (exit 0)", rc, 0)

    rc, _ = run_hook({"tool_input": {"file_path": "data/state/new_thing.json"}})
    check("e2e: new non-.py/.sh file -> ALLOW (exit 0)", rc, 0)

    rc, _ = run_hook({"tool_input": {"file_path": 12345}})
    check("e2e: non-string file_path (Peterffy finding) -> ALLOW, no crash", rc, 0)

    rc, _ = run_hook({"not": "the expected shape"})
    check("e2e: malformed tool_input -> ALLOW, no crash", rc, 0)

    print()
    if DRIFT:
        print(f"FAIL: DRIFT — working tree {REL} ({len(WORKTREE)} bytes) differs from "
              f"the staged blob ({len(STAGED)} bytes). `git add` the file and re-run.")
        sys.exit(1)
    if PG_DRIFT:
        print(f"FAIL: DRIFT — working tree {PG_REL} ({len(PG_WORKTREE)} bytes) "
              f"differs from the staged blob ({len(PG_STAGED)} bytes). `git add` "
              f"the file and re-run.")
        sys.exit(1)
    if bad:
        print(f"RESULT: {bad}/{total} MISMATCHES")
        sys.exit(1)
    print(f"RESULT: all {total} correct (staged blob, no drift)")


if __name__ == "__main__":
    main()
