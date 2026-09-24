REFUTING EVIDENCE for the Gro REJECT on reporting/broker_ground_truth.py ("`re` not imported, `CLEARED_CLASSES` undefined
→ NameError"). The reviewed chunk was chunk 5 of 5 of a NEW file, so the file header was not visible in it. Verified at
source on the exact STAGED blob (`git show :reporting/broker_ground_truth.py`):
- line 37: `import re`
- line 81: `CLEARED_CLASSES = (CLASS_COVERED, CLASS_DESIGN)` (module level, defined before any use)
- `ruff check --select F821,F401` on the staged blob: "All checks passed!" (F821 = undefined name)
- The module is executed by 64 unit tests on the production host (Python 3.10), including
  naked_claims_on_cleared (uses `re` and CLEARED_CLASSES) — all pass; a NameError would fail them.
Please re-evaluate the chunk with these module-level definitions in scope and REJECT only on a concrete failing input.

REFUTING EVIDENCE for the Gro REJECT on nightly_audit.py ("_build_prompt called with a 5th argument gt_block; if the
definition was not updated → TypeError"). The definition IS updated in the same diff (a different chunk). Verified on
the exact STAGED blob (`git show :nightly_audit.py`):
- line 315-316: `def _build_prompt(log_lines: str, trade_events: str, eod: str,
                   modified_files: dict[str, str], ground_truth: str = "") -> str:`
- the only call: `prompt = _build_prompt(log_lines, trade_log, eod, modified_files, gt_block)` — 5 args, matches.
- tests/test_nightly_audit_ground_truth.py calls `na._build_prompt("", "", "", {}, "BROKER GROUND TRUTH ...")` (5 args)
  and `na._build_prompt("", "", "", {})` (4 args, default) — both pass on the production host (Python 3.10).
