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

MODEL = "gemini-flash-latest"
ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
            f"{MODEL}:generateContent")

# OPTION-C SUBSTITUTE (2026-08-24) — a free NVIDIA-hosted reviewer (NVIDIA_MODEL below). It uses the
# SAME free model as the LOCAL gate's option-C substitute (preship_audit._nvidia, added in the
# preship-gate PR #169 — that function lands on main only when that PR merges; it is NOT imported
# here, this CI path is self-contained). Engaged ONLY when Gemini returns an OUTAGE http code
# (429 / 5xx) so a Gemini quota/rate outage cannot fail the server-side wall CLOSED on infra alone
# (2026-08-24: Gemini free tier 429'd for a full day; every gated PR was unmergeable). It is NEVER
# engaged on a Gemini REJECT (a reject is a real verdict, not an outage) or a parse failure. The
# key is a SERVER-SIDE GitHub secret the coding agent cannot read — the "audit the agent cannot
# fake" property is fully preserved; only the reviewer MODEL changes during a Gemini outage. Absent
# NVIDIA_API_KEY the behaviour is UNCHANGED (Gemini-only, fail-closed) — the substitute is additive.
NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_MODEL = "meta/llama-3.1-70b-instruct"
_OUTAGE_CODES = (429, 500, 502, 503, 504)   # Gemini infra/quota outage → substitute may stand in
MAX_DIFF_CHARS = 120_000        # keep the request well inside the model's input budget
MAX_CONTEXT_CHARS = 300_000     # full-file bodies (helpers referenced but not in the diff);
                                #   gemini-2.5-flash has a very large input window, so this is safe
TIMEOUT_SEC = 120
N_SAMPLES = 3           # majority-vote the stochastic reviewer: ship only if >=2 of 3 samples
                        # APPROVE (2026-07-28). A lone stochastic false-reject must not block a
                        # correct ship; INDETERMINATE (error/unclean) never counts toward a pass.

PERSONA = (
    "You are Head of Quant Engineering at a systematic hedge fund, reviewing a "
    "change to a live algorithmic trading bot. Your audit is the last gate before "
    "code reaches a "
    "running system that holds real positions. Be concrete and technical. No hedging."
)

PROMPT = """Audit the change below. It targets a live Alpaca trading bot. Some of
the files run DURING regular trading hours, synchronously on the same thread that
evaluates live
stop losses — a blocking call or an unhandled exception there can leave positions
unmanaged.

You are given TWO sections: first the unified DIFF (what changed), then the FULL CURRENT
CONTENT of each changed file (the post-change file in its entirety). The full content
exists so you are NOT judging through a keyhole: whenever the diff references an
identifier — a helper, wrapper, constant, or default — that it does not itself define,
you MUST look it up in the full file content before drawing any conclusion about it.

Judge the change for DEFECTS only. Do not reject for style, naming, comment wording, or a
redundant-but-harmless operation. A DEFECT is something that produces wrong behaviour, an
unhandled failure, a blocking call on a latency-sensitive path, a silent data-integrity
loss, or a security problem.

Check specifically:
1. Can any line affect order submission, position sizing, stop placement, or exit logic
   in a way the change does not intend?
2. Any genuinely unbounded/retrying network call. BEFORE flagging one: find the call's
   wrapper/helper in the FULL FILE CONTENT. If the wrapper supplies a timeout, retry
   bound, or try/except (e.g. a `_git(...)`/`_api(...)` helper with a `timeout=` default,
   or `urlopen(timeout=...)`), the call IS bounded — that is NOT a defect. Never claim a
   call "lacks a timeout" or "can block indefinitely" without first confirming, in the
   full content, that no wrapper or default provides one.
3. Any new exception path that could abort a render or a cycle unhandled. BEFORE flagging a
   `raise` or an exception as "unhandled" or "aborts the cycle": locate that raise in the FULL
   FILE CONTENT and confirm it is NOT enclosed by a `try/except` that would catch it — a bare
   `except Exception`, or a matching type — INCLUDING an `except:` and its fallback that appear
   many lines BELOW the raise inside the same `try:` block. A `raise RuntimeError(...)` sitting
   inside a `try:` whose `except Exception:` logs a warning and falls through to a fallback path
   is the INTENDED graceful-degradation pattern, NOT an unhandled abort — do not flag it. A call
   that returns None on failure (a fail-safe fetch) reaching a downstream consumer that itself
   handles None is likewise handled, not a new error.
4. Any logic inversion, off-by-one, or boundary error.
5. Any value that can be None/NaN/0 reaching arithmetic or a comparison that assumes
   otherwise? (A NaN written into a stop price makes every `price <= stop` test
   False and the stop can never fire — that class of bug has shipped here before.)
6. Any credential, absolute machine path, or secret introduced?

MANDATORY SELF-CHECK before any REJECT: quote the offending line's VERBATIM TEXT (copy it
character-for-character from the diff or full content), NOT a line number — line numbers in
the diff and context may not correspond, and citing a line whose quoted text does not match
the concern is itself disqualifying. Then trace it using the FULL FILE CONTENT (not just the
diff) — name the wrapper/default/caller you checked and why it does NOT resolve the concern.
For any "unhandled exception / aborts a render or cycle" concern specifically, you must ALSO
quote the nearest enclosing `try:` and its `except ...:` line from the full content — or, to
claim it is truly unhandled, quote the surrounding lines that PROVE no `try/except` encloses
the raise. If you can neither quote an enclosing `except` nor quote the surrounding lines
showing its absence, you may NOT flag it. A concern is a DEFECT only with a concrete failing
input in the changed lines; a theoretical "could", or a concern that the full content already
resolves (including an enclosing try/except or a None-safe consumer), is NOT a defect. If your
only evidence is the diff hunk in isolation, look at the full content before deciding. Answer in at most 220 words. Exactly
ONE line may BEGIN with `VERDICT:` — your final decision:
VERDICT: APPROVE
or
VERDICT: REJECT - <the specific defect, the line, and the wrapper/default you confirmed does not guard it>
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


def _one_audit(prompt_text: str, key: str) -> "tuple[str, str, int]":
    """Run ONE audit sample against Gemini. Returns (verdict, raw_text, http_code).

    http_code lets main() distinguish a Gemini OUTAGE (429/5xx) — where the option-C substitute
    may stand in — from a REJECT verdict or a parse failure (neither of which is an outage). It is
    the HTTP status on an HTTPError, 200 on a delivered-but-unparseable response, and 0 on a
    non-HTTP failure (DNS/timeout/etc.).

    NEVER sys.exit — a transient API/parse failure is a NON-approve INDETERMINATE for THIS
    sample only, so the majority vote in main() can still decide from the other samples rather
    than aborting the whole gate on the first blip. Fail-closed is preserved at the aggregate:
    an INDETERMINATE never counts toward the APPROVE majority a ship requires. Temperature is
    left at the model default (non-zero) ON PURPOSE — the samples must vary for a vote to mean
    anything (the run-to-run APPROVE/REJECT split on an identical diff is that variance).
    """
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"maxOutputTokens": 8192},
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
        return "INDETERMINATE", f"audit API returned HTTP {e.code}", e.code
    except Exception as e:
        return "INDETERMINATE", f"audit API call failed ({type(e).__name__})", 0
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return "INDETERMINATE", "could not parse a verdict from the audit response", 200
    return _verdict(text), text, 200


def _one_audit_nvidia(prompt_text: str, key: str) -> "tuple[str, str]":
    """One audit sample via the OPTION-C NVIDIA substitute. SAME (verdict, raw_text) contract as
    _one_audit, SAME _verdict() parser. Engaged ONLY on a Gemini OUTAGE (see main). Never sys.exit —
    a substitute failure is a NON-approve INDETERMINATE for this sample, so fail-closed is preserved
    at the aggregate. The key is a server-side GitHub secret the coding agent cannot read, so the
    'audit the agent cannot fake' property holds exactly as it does for Gemini."""
    body = json.dumps({
        "model": NVIDIA_MODEL,
        "temperature": 0.3,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": PERSONA},
            {"role": "user", "content": prompt_text},
        ],
    }).encode()
    req = urllib.request.Request(
        NVIDIA_ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            payload = json.load(r)
    except Exception as e:
        return "INDETERMINATE", f"substitute API call failed ({type(e).__name__})"
    try:
        text = payload["choices"][0]["message"]["content"]
    except Exception:
        return "INDETERMINATE", "could not parse a verdict from the substitute response"
    return _verdict(text), text


def main() -> None:
    # <diff-file> is required; <context-file> (full post-change content of the changed files)
    # is optional and backward-compatible — older callers that pass only the diff still work,
    # they just audit through the narrower keyhole. The workflow passes both so the reviewer
    # can resolve a helper/default referenced by the diff but defined outside the hunks (the
    # 2026-07-25 false-reject class: "_git(...) lacks a timeout" when _git defaults timeout=120).
    if len(sys.argv) not in (2, 3):
        _fail("usage: ci_audit.py <diff-file> [<context-file>]")
    try:
        diff = open(sys.argv[1], encoding="utf-8", errors="replace").read()
    except OSError as e:
        _fail(f"cannot read diff file: {e}")
    context = ""
    if len(sys.argv) == 3:
        try:
            context = open(sys.argv[2], encoding="utf-8", errors="replace").read()
        except OSError as e:
            # Context is an ENHANCEMENT, never a gate requirement: if it cannot be read, audit
            # the diff alone rather than fail — a missing context file must not block a ship.
            print(f"::warning::could not read context file ({e}) — auditing diff only.")
            context = ""

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

    # Full post-change file bodies go AFTER the diff so the reviewer can resolve helpers/
    # defaults the diff references but does not define. The diff is primary and never yielded
    # to context; the context is bounded separately and truncation is announced (a truncated
    # helper section must not silently hide the very definition that refutes a false concern).
    ctx_truncated = len(context) > MAX_CONTEXT_CHARS
    if ctx_truncated:
        context = context[:MAX_CONTEXT_CHARS]
    prompt_text = PERSONA + "\n\n" + PROMPT + diff
    if context.strip():
        prompt_text += "\n\n=== FULL CURRENT CONTENT OF CHANGED FILES ===\n" + context

    # Majority-vote the stochastic reviewer. The SAME full diff has drawn both APPROVE and
    # REJECT on different samples (2026-07-28: run 30330994285 REJECT vs 30331270279 APPROVE on
    # an identical gated diff — the reject wandered into pre-existing full-file CONTEXT). A lone
    # stochastic false-reject must not block a correct ship, so require a MAJORITY: ship only if
    # >= (N//2 + 1) samples APPROVE; otherwise fail CLOSED. INDETERMINATE (API/parse failure or an
    # unclean verdict) is NON-approve and never counts toward a pass — "an audit that did not run
    # is not an audit that passed" still holds at the aggregate. _verdict() is UNCHANGED (parity
    # with preship_audit._verdict preserved; the vote wraps it, it does not alter the parser).
    # OPTION-C substitute key (server-side secret; absent => unchanged Gemini-only behaviour).
    nvidia_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    approvals = rejects = indets = 0
    substituted = 0
    for i in range(N_SAMPLES):
        v, text, http_code = _one_audit(prompt_text, key)
        # A Gemini OUTAGE (429/5xx) — NOT a REJECT verdict and NOT a delivered-but-unparseable
        # response (http_code 200) — retries THIS sample against the free substitute so a Gemini
        # quota/rate outage cannot fail the wall CLOSED on infrastructure alone. A REJECT is a real
        # verdict and is never overridden; without NVIDIA_API_KEY the substitute is skipped entirely.
        if v == "INDETERMINATE" and http_code in _OUTAGE_CODES and nvidia_key:
            sv, stext = _one_audit_nvidia(prompt_text, nvidia_key)
            print(f"----- audit sample {i + 1}/{N_SAMPLES}: Gemini outage (HTTP {http_code}) "
                  f"-> option-C substitute (NVIDIA {NVIDIA_MODEL}): {sv} -----")
            v, text = sv, stext
            if sv in ("APPROVE", "REJECT"):
                substituted += 1
        else:
            print(f"----- audit sample {i + 1}/{N_SAMPLES}: {v} -----")
        print(text)
        print("--------------------------")
        if v == "APPROVE":
            approvals += 1
        elif v == "REJECT":
            rejects += 1
        else:
            indets += 1

    if truncated:
        print("::warning::diff exceeded the size cap and was truncated for the audit.")
    if ctx_truncated:
        print("::warning::full-file context exceeded the size cap and was truncated — a helper "
              "definition may be missing from the reviewer's view.")

    if substituted:
        print(f"::warning::{substituted}/{N_SAMPLES} sample(s) used the option-C NVIDIA substitute "
              f"because Gemini was in a quota/rate outage. The server-side wall HELD via the free "
              f"fallback (no weakening — the substitute key is a secret the agent cannot read). "
              f"Re-run once Gemini recovers if a Gemini-primary verdict is preferred.")
    need = N_SAMPLES // 2 + 1        # simple majority: 2 of 3
    print(f"preship vote: APPROVE={approvals} REJECT={rejects} INDETERMINATE={indets} "
          f"(need >= {need} APPROVE to ship; else fail closed)")
    if approvals >= need:
        print(f"preship: external audit APPROVED by majority ({approvals}/{N_SAMPLES})")
        sys.exit(0)
    _fail(f"external audit did NOT reach a {need}/{N_SAMPLES} APPROVE majority "
          f"(APPROVE={approvals} REJECT={rejects} INDETERMINATE={indets}) — failing closed. "
          "See the per-sample responses above for any reject reason.")


if __name__ == "__main__":
    main()
