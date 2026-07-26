#!/usr/bin/env python3
# ruff: noqa: E501  (long audit-prompt strings + the Gemini API URL exceed 88; E501 is cosmetic).
"""
Server-side external audit of a PR diff. Called by .github/workflows/preship-verify.yml.

WHY THIS RUNS IN CI RATHER THAN LOCALLY
The local hook (.claude/preship/preship_gate.py) checks marker FILES that the coding
agent can write. A forged marker defeats it. This script does not read markers at all —
it re-derives the verdict from the diff itself, on a GitHub runner, using a key held in
repository secrets that the agent cannot read. That is the whole point: the audit the
agent cannot fake.

Exit 0 = APPROVE. Exit 1 = REJECT or any failure to obtain a verdict (fail CLOSED —
an audit that did not run is not an audit that passed).

Usage: python3 .github/scripts/ci_audit.py <diff-file>
Env:   GEMINI_API_KEY (required)
"""
import json
import os
import sys
import urllib.error
import urllib.request

MODEL = "gemini-2.5-flash"
ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            f"{MODEL}:generateContent")
MAX_DIFF_CHARS = 120_000        # keep the request well inside the model's input budget
TIMEOUT_SEC = 120

PERSONA = (
    "You are Head of Quant Engineering at a systematic hedge fund, reviewing a "
    "change to a live algorithmic trading bot. Your audit is the last gate before "
    "code reaches a "
    "running system that holds real positions. Be concrete and technical. No hedging."
)

PROMPT = """Audit the diff below. It targets a live Alpaca trading bot. Some of
the files run DURING regular trading hours, synchronously on the same thread that
evaluates live
stop losses — a blocking call or an unhandled exception there can leave positions
unmanaged.

Judge ONLY the diff. Do not require context you were not given, and do not reject for
style, naming, comment wording, or a redundant-but-harmless operation. Reject only for a
DEFECT: something that produces wrong behaviour, an unhandled failure, a blocking
call on a latency-sensitive path, a silent data-integrity loss, or a security
problem.

Check specifically:
1. Can any line affect order submission, position sizing, stop placement, or exit logic
   in a way the diff does not intend?
2. Any new unbounded/retrying network call, or any call without a timeout?
3. Any new exception path that could abort a render or a cycle?
4. Any logic inversion, off-by-one, or boundary error?
5. Any value that can be None/NaN/0 reaching arithmetic or a comparison that assumes
   otherwise? (A NaN written into a stop price makes every `price <= stop` test
   False and the stop can never fire — that class of bug has shipped here before.)
6. Any credential, absolute machine path, or secret introduced?

For any concern, self-check it against the diff first; a concern is a DEFECT only with a
concrete failing input in the changed lines (a theoretical "could" is not a defect). Answer
in at most 200 words. Exactly ONE line may BEGIN with `VERDICT:` — your final decision:
VERDICT: APPROVE
or
VERDICT: REJECT - <one sentence naming the specific defect and the line>
Do not BEGIN any other line with `VERDICT:`, and do not restate this format.

=== DIFF ===
"""


def _fail(msg: str) -> "None":
    print(f"::error::{msg}")
    sys.exit(1)


def _verdict(text: str) -> str:
    """APPROVE / REJECT / INDETERMINATE from a reviewer's text output.

    BYTE-FOR-BYTE IDENTICAL logic to .claude/preship/preship_audit.py::_verdict — the local
    pre-commit gate and this server-side CI gate MUST agree on the same reviewer output.
    .claude/preship/test_verdict.py imports BOTH and asserts they never diverge. Anchor to lines
    that BEGIN with 'VERDICT:' after stripping EVERY leading markdown list/bold/heading/blockquote
    char (`.lstrip("*#-+>• ")` — a "- VERDICT: REJECT" bullet the strip misses would un-anchor and
    leave a lone APPROVE → FAIL-OPEN), require EXACTLY ONE (0 or 2+ → INDETERMINATE, fail-closed),
    then be reject-biased within that one line: any reject token anywhere beats a leading APPROVE.
    A LAST-line heuristic here was a FAIL-OPEN (2026-07-25, cold-2nd): a genuine reject whose
    trailing line restated "VERDICT: APPROVE for an approval" read as APPROVE — an APPROVE for a
    REJECTED diff. main() maps REJECT/INDETERMINATE → exit 1 (CI cannot re-request → fail closed).
    """
    lines = [ln for ln in text.splitlines() if ln.strip().lstrip("*#-+>• ").strip().upper().startswith("VERDICT:")]
    if len(lines) != 1:
        return "INDETERMINATE"
    after = lines[0].strip().upper().split("VERDICT:", 1)[1].strip()
    if any(t in after for t in ("REJECT", "NOT APPROV", "DISAPPROV", "DENY", "DENIED")):
        return "REJECT"
    if after.startswith("APPROVE"):
        return "APPROVE"
    return "INDETERMINATE"


def main() -> None:
    if len(sys.argv) != 2:
        _fail("usage: ci_audit.py <diff-file>")
    try:
        diff = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    except OSError as e:
        _fail(f"cannot read diff file: {e}")

    # Order matters: decide "is there anything to audit" BEFORE requiring the key, so a
    # PR that touches no gated file passes trivially instead of failing on a secret it
    # never needed. The reverse order made every docs-only PR depend on the secret.
    # This is NOT the "empty diff reads as APPROVE" bug — the workflow only reaches this
    # script when it has already computed a non-empty gated-file list, and an empty diff
    # here means those files' contents did not change, not that the audit was skipped.
    if not diff.strip():
        print("No gated changes in this diff — nothing to audit.")
        sys.exit(0)

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        _fail("GEMINI_API_KEY secret is not set — the server-side audit cannot run. "
              "Add it under Settings > Secrets and variables > Actions.")

    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS]

    body = json.dumps({
        "contents": [{"parts": [{"text": PERSONA + "\n\n" + PROMPT + diff}]}],
        "generationConfig": {"maxOutputTokens": 2048,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }).encode()

    req = urllib.request.Request(
        f"{ENDPOINT}?key={key}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        # Never interpolate the request object — it carries the key in the URL.
        _fail(f"audit API returned HTTP {e.code} — failing closed.")
    except Exception as e:
        _fail(f"audit API call failed ({type(e).__name__}) — failing closed.")

    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        _fail("could not parse a verdict from the audit response — failing closed.")

    print("----- audit response -----")
    print(text)
    print("--------------------------")
    if truncated:
        print("::warning::diff exceeded the size cap and was truncated for the audit.")

    # Parse via the shared _verdict() (identical to preship_audit._verdict). Server-side CI cannot
    # re-request, so REJECT and INDETERMINATE both fail CLOSED (exit 1) — the full reviewer text is
    # already printed above, so a reject's reason is visible in the run log.
    v = _verdict(text)
    if v == "REJECT":
        _fail("external audit REJECTED — see the audit response above; failing closed.")
    if v == "APPROVE":
        print("preship: external audit APPROVED")
        sys.exit(0)
    _fail("external audit did not emit exactly one clean 'VERDICT:' line (INDETERMINATE) "
          "— failing closed.")


if __name__ == "__main__":
    main()
