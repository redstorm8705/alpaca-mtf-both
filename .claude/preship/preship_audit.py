#!/usr/bin/env python3
# ruff: noqa: E501  (long audit-rationale comments + the Gemini API URL exceed 88; E501 is
# cosmetic — F/B/E-real correctness checks still run. Matches scripts/sync_reports.py.)
"""
preship_audit.py <file> [<file>...]  [--waive-gro] [--context <file|facts>] [--evidence <file>]

Runs the FINAL PRE-SHIP Gro+GAI audit on the STAGED content of each file
(audit-what-you-commit) and, only if both APPROVE, writes the marker the
ship gate checks. Fail-closed: any REJECT / API error / unstaged file => no
marker => gate stays closed.

For files in preship_gate.GATED_CLAIM_FILES (currently: handoff.md), a DIFFERENT
prompt (CLAIM_PROMPT_HEAD) is used — an evidentiary-rigor audit for factual claims,
not a code-defect audit. Added 2026-08-04 after an unverified diagnostic claim shipped
straight into handoff.md with zero review.

--waive-gro : record gro=WAIVED (Rafael-authorized only, e.g. Groq TPD limit).
              GAI must still APPROVE.
--context   : PROACTIVE ground-truth facts (a file path OR inline string) about code
              OUTSIDE the diff — what a referenced constant means, threading model, a
              cross-file helper/guard, the runtime profile — plus refutations of the
              likely false premises. Prepended on the FIRST pass so the reviewer never
              forms the cross-file false premise (Rafael mandate 2026-08-02). MANDATORY
              per CLAUDE.md for any diff whose safety depends on code the diff doesn't show.
--evidence  : REACTIVE — refuting evidence AFTER a false-premise reject (counter-prompt).
"""
import sys
import os
import json
import hashlib
import subprocess
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from preship_gate import GATED_CLAIM_FILES  # noqa: E402 — single source of truth, avoids drift

# Derived from __file__, NOT hardcoded (2026-07-22). A hardcoded absolute path made the
# gate silently inert on any machine but its author's — which is exactly the failure the
# versioning of this file is meant to prevent (second Claude account, OCI, fresh clone).
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MARKER_DIR = os.path.join(REPO, ".claude", "preship", "markers")
ENV = os.path.join(REPO, ".env")

PROMPT_HEAD = (
    "FINAL PRE-SHIP AUDIT of the EXACT staged diff below, before it ships to a "
    "live trading bot. Audit ONLY the CHANGED (+/-) lines for a defect "
    "INTRODUCED BY this change: logic inversion, off-by-one, missing guards, "
    "unsafe failure modes. SCOPE: unchanged surrounding code and pre-existing "
    "helper functions are OUT OF SCOPE — do not REJECT for concerns about code "
    "this diff does not modify, and do not ask for a broader codebase review. "
    "If the change's safety depends on a guard that IS visible in the provided "
    "context (e.g. an `if ... is None:` check), treat that precondition as "
    "satisfied. In the diff, lines starting with '+' or '-' are the change "
    "under audit; lines starting with a space are UNCHANGED CONTEXT shown only "
    "for reference — NEVER base a REJECT on a space-prefixed context line. "
    "PROCESS (do this before deciding): for any concern, QUOTE the exact changed "
    "line and SELF-CHECK it — re-read the changed lines and any visible guard/"
    "import/caller; if the concern is already handled or is merely theoretical, "
    "do NOT raise it. REJECT ONLY for a CONCRETE failing input (input -> wrong "
    "output/crash) in the CHANGED lines; a theoretical 'could' is a NIT, not a "
    "defect, and must not drive a REJECT. Do not restate the diff. "
    "END your reply with its own line BEGINNING exactly `VERDICT: APPROVE` or "
    "`VERDICT: REJECT — <specific defect + the quoted changed line>` — your FINAL "
    "decision. Exactly ONE line may BEGIN with `VERDICT:` (you may mention the word "
    "mid-sentence, but never START another line with it, and do not restate the "
    "format).\n\nDIFF:\n"
)

# CLAIM_PROMPT_HEAD (added 2026-08-04): used ONLY for GATED_CLAIM_FILES (handoff.md).
# WHY THIS EXISTS: a session asserted "the live trading bot has been down for two
# weeks" as settled fact in handoff.md, based on checking systemd/crontab on ONE host,
# without checking git history for an obvious alternative (a completed migration). The
# claim was false — the investigated host was a deliberately decommissioned rollback
# box; real production was fine the whole time. It shipped with ZERO independent
# review and was only caught because the user personally distrusted it and demanded a
# board re-check. PROMPT_HEAD's checklist (logic inversion, off-by-one, missing
# guards) is a CODE-defect lens and does not transfer to prose — this is a distinct,
# evidentiary-rigor lens for factual claims.
CLAIM_PROMPT_HEAD = (
    "FINAL PRE-COMMIT AUDIT of a change to this project's CROSS-ACCOUNT TRUST LOG "
    "(handoff.md) — the durable record other sessions/accounts read as settled fact "
    "with zero further verification. Your job is NOT to check code correctness; it is "
    "to catch an ASSERTED FINDING that is not actually earned by the evidence behind "
    "it. This exists because a session asserted 'the live trading bot has been down "
    "for two weeks' as settled fact, based only on checking systemd/crontab on one "
    "host, without checking git history for an obvious alternative explanation (a "
    "planned migration) — the claim was false and was only caught because the user "
    "personally distrusted it and demanded an independent re-check. That must never "
    "again depend on the user's suspicion; this audit is the mechanical replacement.\n\n"
    "SCOPE: in the diff, lines starting with '+' are NEW claims — audit these. Lines "
    "starting with '-' are being REMOVED — ignore them (a removed claim is not a new "
    "risk). Lines starting with a space are UNCHANGED CONTEXT shown only so you can "
    "read the surrounding paragraph for sense — NEVER base a REJECT on a space-"
    "prefixed line; that claim already shipped in an earlier, separately-audited "
    "commit and re-litigating it here is out of scope and will make this gate "
    "impossible to pass on unrelated small edits (which teaches people to bypass it "
    "— a worse outcome than the risk it closes). If a diff has no '+' lines with a "
    "new claim, APPROVE.\n\n"
    "For each NEW ('+') factual claim, finding, or diagnosis in the diff below "
    "(a state description, a root-cause claim, a 'confirmed'/'verified'/'zero risk' "
    "assertion, a description of what is or isn't running somewhere), ask:\n"
    "  1. Is this claim's evidentiary basis actually shown or clearly implied by "
    "surrounding text — a specific command, a specific file, a specific quoted "
    "output? A claim with no visible evidence trail is a RED FLAG, not something to "
    "wave through because it 'sounds plausible' or is asserted confidently.\n"
    "  2. Does the claim jump to ONE explanation for an observation when an obvious "
    "alternative was not visibly ruled out (e.g. 'stopped' -> 'broken', when 'stopped "
    "on purpose' / 'migrated' / 'superseded' are just as plausible and cheap to "
    "check)? Confident language is not evidence of rigor — treat hedged, sourced "
    "uncertainty as MORE trustworthy than an unqualified assertion with no shown "
    "verification.\n"
    "  3. Would a reasonable reader trust this claim as settled fact, or does it read "
    "as inference/assumption dressed up as a conclusion?\n"
    "  4. Is the claim internally consistent with other facts shown in the same "
    "diff/file (timestamps, other confirmed state)?\n"
    "PROCESS: for each claim you are unsure about, QUOTE it exactly, then state what "
    "verification it is missing. REJECT if ANY claim lacks a visible evidentiary "
    "basis or fails to rule out an obvious alternative explanation. Do NOT reject "
    "over wording or formatting, and do NOT reject a claim that DOES show its "
    "evidence (e.g. 'confirmed via `gh pr view 84 --json mergeable`', or a quoted "
    "command's literal output) — those are fine and are the standard to hold every "
    "other claim to. This is a real gate, not a rubber stamp: if in doubt, REJECT "
    "and name exactly which claim and what check is missing.\n"
    "END your reply with its own line BEGINNING exactly `VERDICT: APPROVE` or "
    "`VERDICT: REJECT — <specific claim + what verification is missing>` — your "
    "FINAL decision. Exactly ONE line may BEGIN with `VERDICT:` (you may mention the "
    "word mid-sentence, but never START another line with it, and do not restate "
    "the format).\n\nDIFF:\n"
)


def _git(args, want_bytes=False):
    r = subprocess.run(["git", "-C", REPO] + args, capture_output=True, timeout=20)
    out = r.stdout if want_bytes else r.stdout.decode("utf-8", "replace")
    return (r.returncode == 0, out, r.stderr.decode("utf-8", "replace"))

def _load_env():
    keys = {}
    try:
        for ln in open(ENV):
            if "=" in ln and not ln.strip().startswith("#"):
                k, _, v = ln.strip().partition("=")
                keys[k] = v
    except OSError:
        pass
    return keys

def _curl(url, headers, body_dict, key):
    # curl transport (macOS urllib hits SSL CERTIFICATE_VERIFY_FAILED).
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body_dict, f)
        path = f.name
    try:
        cmd = ["curl", "-s", url]
        for h in headers:
            cmd += ["-H", h]
        cmd += ["--data-binary", f"@{path}"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout
    finally:
        os.unlink(path)
    r = json.loads(out)
    if isinstance(r, dict) and r.get("error"):
        raise RuntimeError(str(r["error"]).replace(key, "***")[:200])
    return r

def _gro(prompt, key):
    r = _curl(
        "https://api.groq.com/openai/v1/chat/completions",
        [f"Authorization: Bearer {key}", "Content-Type: application/json"],
        {"model": "llama-3.3-70b-versatile", "messages": [
            {"role": "system",
             "content": "You are a Senior Staff HFT engineer auditing a diff "
                        "before it ships. Concrete, no hedging."},
            {"role": "user", "content": prompt}], "max_tokens": 1500},
        key)
    if "choices" not in r:
        raise RuntimeError(str(r).replace(key, "***")[:200])
    return r["choices"][0]["message"]["content"]

def _gai(prompt, key, paid_key=""):
    # Free key is used BY DEFAULT (the free tier is a DAILY quota that RESETS — do not permanently
    # switch to paid after one 429). paid_key is a SEAMLESS AUTO-FAILOVER used ONLY on a real quota/
    # rate error (429 / RESOURCE_EXHAUSTED), so the paid quota is spent only when free is genuinely
    # exhausted — never out of inertia. (2026-08-09: this replaces a manual .env free→paid swap that
    # was wasting paid quota for calls the free tier would have served.)
    def _one(k):
        r = _curl(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={k}",
            ["Content-Type: application/json"],
            {"contents": [{"parts": [{"text": prompt}]}],
             "generationConfig": {"maxOutputTokens": 8192}},
            k)
        if "candidates" not in r:
            raise RuntimeError(str(r).replace(k, "***")[:200])
        return r["candidates"][0]["content"]["parts"][0]["text"]
    try:
        return _one(key)
    except Exception as e:
        msg = str(e).lower()
        is_quota = ("429" in msg or "resource_exhausted" in msg or "quota" in msg
                    or "rate limit" in msg or "rate-limit" in msg)
        if paid_key and paid_key != key and is_quota:
            sys.stderr.write("[preship] GAI free-tier quota hit — auto-failing over to paid key.\n")
            return _one(paid_key)
        raise

def _verdict(text):
    # Require EXACTLY ONE 'VERDICT:' line, then parse THAT line reject-biased. Rationale
    # (2026-07-25, two cold-2nd fail-opens): any single-line-SELECTION heuristic is fragile when
    # the reviewer emits multiple 'VERDICT:' mentions — FIRST-line catches an INTERMEDIATE/
    # hypothetical verdict ("I thought VERDICT: REJECT ... VERDICT: APPROVE" → false REJECT);
    # LAST-line catches a trailing FORMAT-RESTATEMENT ("...end with VERDICT: APPROVE for an
    # approval" → FAIL-OPEN: a marker written for a genuinely REJECTED diff). Both are unsafe.
    # So we do NOT guess which mention is the real decision: 0 or 2+ VERDICT lines → INDETERMINATE,
    # and the caller RE-REQUESTS exactly one. Fail-CLOSED against both classes; the prompt already
    # instructs "emit VERDICT exactly once", so a clean reviewer hits the single-line path directly.
    # Anchor to lines that START with 'VERDICT:' after stripping leading markdown list/bold/heading/
    # blockquote chars (`.lstrip("*#-+>• ")`). This excludes prose/code mentions ("the VERDICT:
    # parser", "if 'VERDICT:' in line") and mid-sentence hypotheticals ("I thought VERDICT: REJECT
    # ...") — so a diff that is itself ABOUT verdict parsing does not drown the real decision in
    # incidental mentions, and the intermediate-hypothetical-reject flake is excluded outright. A
    # standalone restatement line ("VERDICT: APPROVE for an approval.") still counts, so 2+ anchored
    # lines → INDETERMINATE (fail-CLOSED), never a guessed pick. The strip must cover EVERY leading
    # decoration a reviewer might emit: a "- VERDICT: REJECT" (bullet the strip missed) would silently
    # un-anchor, leaving a lone APPROVE line → FAIL-OPEN (2026-07-25 test probe caught exactly this).
    # Over-stripping is fail-CLOSED-safe (more anchored lines → more likely 2+ → INDETERMINATE); only
    # UNDER-stripping is dangerous. Kept BYTE-FOR-BYTE in lockstep with ci_audit.py::_verdict.
    lines = [ln for ln in text.splitlines() if ln.strip().lstrip("*#-+>• ").strip().upper().startswith("VERDICT:")]
    if len(lines) != 1:
        return "INDETERMINATE"
    after = lines[0].strip().upper().split("VERDICT:", 1)[1].strip()
    # REJECT-biased within the one line (fail-CLOSED): a reject token anywhere wins over a
    # leading APPROVE; only a clean APPROVE-open with no reject token approves; else INDETERMINATE.
    if any(t in after for t in ("REJECT", "NOT APPROV", "DISAPPROV", "DENY", "DENIED")):
        return "REJECT"
    if after.startswith("APPROVE"):
        return "APPROVE"
    return "INDETERMINATE"

def audit_file(relpath, waive_gro, keys, evidence="", context=""):
    ok_sha, blob, err = _git(["cat-file", "blob", f":{relpath}"], want_bytes=True)
    if not ok_sha:
        return False, f"{relpath}: not staged (git add it first)"
    sha = hashlib.sha256(blob).hexdigest()
    # Audit the file's net change vs the DEPLOY BASELINE (origin/main), computed
    # from the INDEX (the staged/committed blob whose sha the gate checks).
    # The prior version diffed only `--cached`; once a file was committed the
    # cached diff emptied and it fell back to sending the ENTIRE file, so the
    # flash auditor hallucinated a defect in unchanged code ~78 lines from the
    # real change (false REJECT on a correct line, 2026-07-06). Diffing against
    # origin/main shows committed AND staged changes as a focused diff, never
    # the whole file. -U30 keeps nearby guards visible without over-exposing
    # distant unrelated functions (the old -U90 widened the blast radius).
    base_ok, _b, _ = _git(["rev-parse", "--verify", "--quiet", "origin/main"])
    base = "origin/main" if base_ok else "HEAD"
    ok_d, diff, _ = _git(["diff", "--cached", base, "-U30", "--", relpath])
    _head = CLAIM_PROMPT_HEAD if relpath in GATED_CLAIM_FILES else PROMPT_HEAD
    prompt = _head + (diff if diff.strip()
                      else f"(no change vs {base} for {relpath})")
    # REVIEWER-CONTEXT block (Rafael mandate 2026-08-02): PROACTIVELY pre-load the
    # diff-specific facts a diff-only view CANNOT show — what a referenced constant
    # MEANS, the threading model, a cross-file helper/caller/guard, the runtime
    # profile — plus refutations of the likely false premises. The tool builds its
    # prompt from the diff alone and so keeps guessing cross-file semantics wrong
    # (e.g. GAI 2026-08-02 assumed MAX_PORTFOLIO_RISK_PCT was an aggregate ceiling and
    # false-REJECTed a correct config diff). A few hundred tokens of facts up front is
    # far cheaper than a counter-prompt round after. Injected as GROUND TRUTH so the
    # reviewer cannot REJECT on an assumption these facts contradict.
    if context.strip():
        prompt += ("\n\n--- REVIEWER CONTEXT (author-supplied FACTS about code OUTSIDE "
                   "this diff — treat as GROUND TRUTH; do NOT REJECT on an assumption "
                   "that contradicts a fact stated here) ---\n" + context.strip())
    # DISAGREEMENT PROTOCOL counter-prompt path (Rafael mandate 2026-07-19): a reject
    # on a false premise (e.g. a defect claim about a helper the -U30 diff can't
    # show) is resolved by SHOWING the reviewer the refuting evidence, never by a
    # blind re-roll. Pass it via --evidence.
    if evidence.strip():
        prompt += ("\n\n--- COUNTER-PROMPT EVIDENCE (a prior reject rested on a "
                   "premise this refutes; weigh it before re-verdicting; if it "
                   "resolves your stated concern, APPROVE) ---\n"
                   + evidence.strip())

    _reminder = ("\n\nREMINDER: exactly ONE line may BEGIN with `VERDICT:` — your single final "
                 "decision `VERDICT: APPROVE` or `VERDICT: REJECT — <defect>`. Do NOT start any "
                 "other line with `VERDICT:` and do NOT restate the format.")

    # GAI (required)
    try:
        gai_txt = _gai(prompt, keys.get("GEMINI_API_KEY", ""), keys.get("GEMINI_PAID_API_KEY", ""))
        gai_v = _verdict(gai_txt)
        if gai_v == "INDETERMINATE":
            # A parse failure is NOT a content reject (Rafael 2026-07-25) — re-request ONCE
            # with a format reminder before deciding anything.
            gai_txt = _gai(prompt + _reminder, keys.get("GEMINI_API_KEY", ""), keys.get("GEMINI_PAID_API_KEY", ""))
            gai_v = _verdict(gai_txt)
    except Exception as e:
        return False, f"{relpath}: GAI audit failed ({e}) — fail-closed, no marker"
    if gai_v == "INDETERMINATE":
        return False, (f"{relpath}: GAI INDETERMINATE — no parseable VERDICT line after a retry "
                       f"(NOT a content reject; re-run the audit). Last 300 chars:\n{gai_txt[-300:]}")
    if gai_v != "APPROVE":
        return False, f"{relpath}: GAI REJECT — no marker.\n{gai_txt[-600:]}"

    # Gro (required unless waived)
    if waive_gro:
        gro_v = "WAIVED"
    else:
        try:
            gro_txt = _gro(prompt, keys.get("GROQ_API_KEY", ""))
            gro_v = _verdict(gro_txt)
            if gro_v == "INDETERMINATE":
                gro_txt = _gro(prompt + _reminder, keys.get("GROQ_API_KEY", ""))
                gro_v = _verdict(gro_txt)
        except Exception as e:
            return False, (f"{relpath}: Gro audit failed ({e}). Re-run with "
                           "--waive-gro only if Rafael authorizes.")
        if gro_v == "INDETERMINATE":
            return False, (f"{relpath}: Gro INDETERMINATE — no parseable VERDICT line after a "
                           f"retry (NOT a content reject; re-run). Last 300 chars:\n{gro_txt[-300:]}")
        if gro_v != "APPROVE":
            return False, f"{relpath}: Gro REJECT — no marker.\n{gro_txt[-600:]}"

    os.makedirs(MARKER_DIR, exist_ok=True)
    marker = {"sha256": sha, "gro": gro_v, "gai": "APPROVE", "ts": int(time.time()),
              "file": relpath}
    with open(os.path.join(MARKER_DIR, relpath.replace("/", "__") + ".json"), "w") as f:
        json.dump(marker, f, indent=2)
    return True, (f"{relpath}: APPROVED (gro={gro_v} gai=APPROVE) — marker "
                  f"written, sha {sha[:12]}")

def main():
    argv = sys.argv[1:]
    waive = "--waive-gro" in argv
    evidence = ""
    ev_path = None
    if "--evidence" in argv:
        _i = argv.index("--evidence")
        if _i + 1 < len(argv):
            ev_path = argv[_i + 1]
            try:
                evidence = open(ev_path).read()
            except OSError as _e:
                print(f"--evidence file unreadable ({_e}) — proceeding without it")
    # --context: PROACTIVE facts (a file path OR an inline string) pre-loaded into the
    # first-pass prompt so the reviewer never forms a cross-file false premise.
    context = ""
    ctx_val = None
    if "--context" in argv:
        _j = argv.index("--context")
        if _j + 1 < len(argv):
            ctx_val = argv[_j + 1]
            if os.path.isfile(ctx_val):
                try:
                    context = open(ctx_val).read()
                except OSError as _e:
                    print(f"--context file unreadable ({_e}) — proceeding without it")
            else:
                context = ctx_val  # treat as an inline facts string
    args = [a for a in argv if not a.startswith("--") and a != ev_path and a != ctx_val]
    if not args:
        print("usage: preship_audit.py <file> [<file>...] [--waive-gro] "
              "[--evidence <file>] [--context <file|inline-facts>]")
        sys.exit(1)
    keys = _load_env()
    allok = True
    for f in args:
        # NOT .lstrip("./") — lstrip strips CHARACTERS: ".claude/x" -> "claude/x", which
        # locked every dot-leading (self-gated) path out of its own audit.
        # Prefix-strip only.
        while f.startswith("./"):
            f = f[2:]
        ok, msg = audit_file(f, waive, keys, evidence, context)
        print(("PASS " if ok else "FAIL ") + msg)
        allok = allok and ok
    sys.exit(0 if allok else 1)

if __name__ == "__main__":
    main()
