#!/usr/bin/env python3
"""
preship_audit.py <file> [<file>...]  [--waive-gro]

Runs the FINAL PRE-SHIP Gro+GAI audit on the STAGED content of each file
(audit-what-you-commit) and, only if both APPROVE, writes the marker the
ship gate checks. Fail-closed: any REJECT / API error / unstaged file => no
marker => gate stays closed.

--waive-gro : record gro=WAIVED (Rafael-authorized only, e.g. Groq TPD limit).
              GAI must still APPROVE.
"""
import sys
import os
import json
import hashlib
import subprocess
import time

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
    "REJECT only for a concrete, specific defect in the changed "
    "lines. Do not restate the diff. Your FIRST line must be exactly "
    "`VERDICT: APPROVE` or `VERDICT: REJECT — <specific defect in the changed "
    "lines>`; put any explanation AFTER that first line.\n\nDIFF:\n"
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

def _gai(prompt, key):
    r = _curl(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
        ["Content-Type: application/json"],
        {"contents": [{"parts": [{"text": prompt}]}],
         "generationConfig": {"maxOutputTokens": 8192,
                              "thinkingConfig": {"thinkingBudget": 0}}},
        key)
    if "candidates" not in r:
        raise RuntimeError(str(r).replace(key, "***")[:200])
    return r["candidates"][0]["content"]["parts"][0]["text"]

def _verdict(text):
    up = text.upper()
    # FIRST VERDICT: line wins — models are told to put it on line 1, so it is
    # emitted before any max-token truncation of verbose trailing reasoning
    # (2026-07-06: a 2000-token cap truncated GAI's approval before its trailing
    # VERDICT line, and the reversed search then fail-closed to REJECT).
    for ln in text.splitlines():
        lu = ln.upper()
        if "VERDICT:" in lu:
            # REJECT-BIASED (2026-07-22, cold-2nd F1). The prior test
            # `"APPROVE" if "APPROVE" in ln` was a FAIL-OPEN: a reject whose
            # reason names an approval concept — "VERDICT: REJECT — I cannot
            # APPROVE this unguarded None" — contains the substring "APPROVE"
            # and was recorded as APPROVE, writing a valid marker the ship gate
            # then trusts. A rejection wins its own verdict line. Mirrors the
            # guard in .github/scripts/ci_audit.py. Read only the text AFTER the
            # label so a stray earlier token cannot flip it.
            after = lu.split("VERDICT:", 1)[1].strip()
            if "REJECT" in after:
                return "REJECT"
            # startswith, NOT substring (2026-07-22, cold-2nd round-2 Finding 1):
            # a substring test made "NOT APPROVED" and "DISAPPROVE" — genuine
            # rejections that omit the token REJECT — return APPROVE and write a
            # trusted marker. Only an APPROVE that OPENS the post-label text counts.
            # Matches .github/scripts/ci_audit.py's verdict.startswith("APPROVE").
            if after.startswith("APPROVE"):
                return "APPROVE"
            return "REJECT"
    return "APPROVE" if ("APPROVE" in up and "REJECT" not in up) else "REJECT"

def audit_file(relpath, waive_gro, keys, evidence=""):
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
    prompt = PROMPT_HEAD + (diff if diff.strip()
                            else f"(no change vs {base} for {relpath})")
    # DISAGREEMENT PROTOCOL counter-prompt path (Rafael mandate 2026-07-19): a reject
    # on a false premise (e.g. a defect claim about a helper the -U30 diff can't
    # show) is resolved by SHOWING the reviewer the refuting evidence, never by a
    # blind re-roll. Pass it via --evidence.
    if evidence.strip():
        prompt += ("\n\n--- COUNTER-PROMPT EVIDENCE (a prior reject rested on a "
                   "premise this refutes; weigh it before re-verdicting; if it "
                   "resolves your stated concern, APPROVE) ---\n"
                   + evidence.strip())

    # GAI (required)
    try:
        gai_txt = _gai(prompt, keys.get("GEMINI_API_KEY", ""))
        gai_v = _verdict(gai_txt)
    except Exception as e:
        return False, f"{relpath}: GAI audit failed ({e}) — fail-closed, no marker"
    if gai_v != "APPROVE":
        return False, f"{relpath}: GAI REJECT — no marker.\n{gai_txt[-600:]}"

    # Gro (required unless waived)
    if waive_gro:
        gro_v = "WAIVED"
    else:
        try:
            gro_txt = _gro(prompt, keys.get("GROQ_API_KEY", ""))
            gro_v = _verdict(gro_txt)
        except Exception as e:
            return False, (f"{relpath}: Gro audit failed ({e}). Re-run with "
                           "--waive-gro only if Rafael authorizes.")
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
    args = [a for a in argv if not a.startswith("--") and a != ev_path]
    if not args:
        print("usage: preship_audit.py <file> [<file>...] [--waive-gro] "
              "[--evidence <file>]")
        sys.exit(1)
    keys = _load_env()
    allok = True
    for f in args:
        # NOT .lstrip("./") — lstrip strips CHARACTERS: ".claude/x" -> "claude/x", which
        # locked every dot-leading (self-gated) path out of its own audit.
        # Prefix-strip only.
        while f.startswith("./"):
            f = f[2:]
        ok, msg = audit_file(f, waive, keys, evidence)
        print(("PASS " if ok else "FAIL ") + msg)
        allok = allok and ok
    sys.exit(0 if allok else 1)

if __name__ == "__main__":
    main()
