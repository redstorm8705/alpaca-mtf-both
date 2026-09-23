#!/usr/bin/env python3
# ruff: noqa: E501  (long reviewer-output fixtures)
"""Regression suite for the review-gate verdict parser — BOTH copies of it.

There are two gates that must agree on the same reviewer output: the local pre-commit gate
(.claude/preship/preship_audit.py::_verdict) and the server-side CI gate
(.github/scripts/ci_audit.py::_verdict). Their logic is byte-for-byte identical BY DESIGN;
this suite imports both and asserts they never diverge (a divergence means the local gate and
the CI gate could disagree — the exact class of bug cold-2nd flagged as T1 on 2026-07-25).

Every case is a real failure mode the gate must handle. The dangerous class is a FAIL-OPEN:
reviewer text that is a genuine REJECT but which a naive parser reads as APPROVE — a marker/exit
that ships a REJECTED diff. A false REJECT merely re-runs and costs nothing but tokens, so the
parser is fail-CLOSED biased: 0 or 2+ anchored 'VERDICT:' lines → INDETERMINATE.

Run:  python3 .claude/preship/test_verdict.py     (exit 0 = pass, non-zero = a regression)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(modname: str, relpath: str):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(HERE, relpath))
    assert spec is not None and spec.loader is not None, f"cannot load {relpath}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Local pre-commit gate and server-side CI gate. Importing ci_audit is side-effect-free
# (its network/main path is guarded by `if __name__ == "__main__"`).
_pa = _load("preship_audit", "preship_audit.py")
_verdict = _pa._verdict
_ci_verdict = _load("ci_audit_mod", os.path.join("..", "..", ".github", "scripts", "ci_audit.py"))._verdict

CASES = [
    ("clean approve", "VERDICT: APPROVE", "APPROVE"),
    ("clean reject", "VERDICT: REJECT — off-by-one on line 5", "REJECT"),
    ("NOT APPROVED phrasing", "VERDICT: NOT APPROVED — missing guard", "REJECT"),
    ("verbose single verdict", "...long analysis, all guards present...\nVERDICT: APPROVE", "APPROVE"),
    ("no verdict line", "analysis only, no verdict line here", "INDETERMINATE"),
    ("empty", "", "INDETERMINATE"),
    ("verdict line, empty remainder", "VERDICT:", "INDETERMINATE"),
    # markdown (T1): Gemini bolds/bullets routinely — both parsers must strip `*# ` and agree.
    ("markdown bold approve", "**VERDICT: APPROVE**", "APPROVE"),
    ("markdown bold reject", "**VERDICT: REJECT** — line 5 inverted", "REJECT"),
    ("markdown heading reject", "# VERDICT: REJECT — missing guard", "REJECT"),
    # T1 fail-open now closed: a bulleted 2nd verdict must anchor → 2 lines → INDETERMINATE,
    # NOT silently un-anchor (which pre-fix left only the APPROVE line → fail-open).
    ("bulleted second verdict (T1 lockstep)",
     "VERDICT: APPROVE\n* VERDICT: REJECT — actual defect", "INDETERMINATE"),
    # false-reject flake: a mid-sentence hypothetical reject; the real verdict is APPROVE
    ("flake: mid-sentence hypothetical reject",
     "I first thought VERDICT: REJECT but that was a red herring.\nVERDICT: APPROVE", "APPROVE"),
    # self-referential: prose/code mentions of VERDICT: must be excluded (this file reviews such code)
    ("self-referential prose",
     "The VERDICT: parser reads lines.\n  if 'VERDICT:' in line: pass\nVERDICT: APPROVE", "APPROVE"),
    # FAIL-OPEN 1 (cold-2nd): genuine reject + single-line format restatement (mid-line → excluded)
    ("fail-open 1: reject + inline restatement",
     "Line 40 inverts the fill guard.\nVERDICT: REJECT — logic inversion\n(format: VERDICT: APPROVE or VERDICT: REJECT.)", "REJECT"),
    # FAIL-OPEN 2 (cold-2nd): genuine reject + multi-line restatement whose last anchored line is APPROVE
    ("fail-open 2: reject + trailing APPROVE restatement",
     "VERDICT: REJECT — inverts the fill guard\nend with VERDICT: REJECT for a reject, or\nVERDICT: APPROVE for an approval.", "INDETERMINATE"),
]


def main() -> None:
    fails = []
    for name, text, want in CASES:
        got = _verdict(text)
        got_ci = _ci_verdict(text)
        ok = got == want and got_ci == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: preship={got} ci={got_ci} want={want}")
        if got != want:
            fails.append(f"{name}: preship={got} want={want}")
        if got_ci != want:
            fails.append(f"{name}: ci={got_ci} want={want}")
        if got != got_ci:
            fails.append(f"{name}: LOCKSTEP DIVERGE preship={got} ci={got_ci}")
    # Exhaustive fail-open invariant: NO input containing an anchored 'VERDICT: REJECT' line may
    # ever return APPROVE, in EITHER parser. A false REJECT just re-runs; a false APPROVE ships a bug.
    probes = [
        "VERDICT: REJECT — x",
        "VERDICT: REJECT — x\nVERDICT: APPROVE (example)",
        "a\nVERDICT: REJECT — bug\nb\nVERDICT: APPROVE for approvals",
        "   VERDICT: REJECT — leading whitespace",
        "**VERDICT: REJECT** — bold reject",
        "VERDICT: APPROVE\n- VERDICT: REJECT — bulleted real defect",
    ]
    for p in probes:
        for label, fn in (("preship", _verdict), ("ci", _ci_verdict)):
            if fn(p) == "APPROVE":
                fails.append(f"FAIL-OPEN PROBE ({label}) returned APPROVE for {p!r}")
    fails += _chunker_checks()
    if fails:
        sys.exit("FAIL: " + "; ".join(fails))
    print(f"\nOK — {len(CASES)} cases + {len(probes)} fail-open probes pass on BOTH parsers; "
          f"lockstep + fail-closed hold; Gro chunker checks pass.")


def _chunker_checks() -> list:
    """Gro TPM chunker (2026-09-23): a brand-new file is ONE git hunk; before the fix the
    never-split-a-hunk rule sent it as one oversized chunk -> 'Request too large' -> no real Gro
    verdict for any new file larger than ~one chunk. Pure-insertion hunks now split at line
    boundaries; hunks with a removal ('-') line must still NEVER be split."""
    out = []
    at = "@@ -0,0 +1,600 @@\n"
    big_add = at + "".join(f"+line {i} " + "x" * 40 + "\n" for i in range(600))
    pieces = _pa._split_pure_add_hunk(big_add, 5000)
    if len(pieces) < 2:
        out.append(f"chunker: pure-add hunk not split ({len(pieces)} piece)")
    if any(len(p) > 5000 + 200 for p in pieces):
        out.append("chunker: a pure-add piece exceeds the limit")
    if any(not p.startswith(at) for p in pieces):
        out.append("chunker: a piece lost its @@ header")
    if "".join(p[len(at):] for p in pieces) != big_add[len(at):]:
        out.append("chunker: pieces do not reassemble to the original body (line lost/duplicated)")
    mixed = "@@ -1,300 +1,300 @@\n" + "".join(f"-old {i} " + "y" * 40 + "\n+new {i} " + "y" * 40 + "\n" for i in range(300))
    if _pa._split_pure_add_hunk(mixed, 5000) != [mixed]:
        out.append("chunker: a hunk WITH removals was split (must stay intact)")
    small = at + "+one\n"
    if _pa._split_pure_add_hunk(small, 5000) != [small]:
        out.append("chunker: a hunk that already fits was altered")
    # End-to-end: a 600-line new-file diff through _gro_chunked yields chunks that each fit, and all
    # APPROVE -> combined APPROVE (Groq call stubbed; records each prompt's size).
    sizes = []

    def _fake_gro(prompt, key):
        sizes.append(len(prompt))
        return "VERDICT: APPROVE"
    real_gro = _pa._gro
    _pa._gro = _fake_gro
    try:
        diff = "diff --git a/t.py b/t.py\nnew file mode 100644\n--- /dev/null\n+++ b/t.py\n" + big_add
        res = _pa._gro_chunked("HEAD\n", diff, "", "k")
    finally:
        _pa._gro = real_gro
    if len(sizes) < 2 or max(sizes) > 18000 + 400:
        out.append(f"chunker e2e: {len(sizes)} Gro calls, max prompt {max(sizes) if sizes else 0} chars")
    if _pa._verdict(res) != "APPROVE":
        out.append("chunker e2e: all-APPROVE chunks did not combine to APPROVE")
    for f in out:
        print(f"  [FAIL] {f}")
    if not out:
        print(f"  [PASS] Gro chunker: new-file hunk split into {len(sizes)} fitting chunks; removal hunks intact")
    return out


if __name__ == "__main__":
    main()
