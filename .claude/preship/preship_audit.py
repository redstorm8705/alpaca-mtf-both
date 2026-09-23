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
import re
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
    "live trading bot. FIRST judge the changed file by its ACTUAL role: gate/audit/CI/cron "
    "TOOLING (.claude/preship/*, .github/scripts/*, *_audit.py, autonomous_*.py, weekly_*.py, "
    "gai_client.py, and helpers they import) does NOT run in the live trade loop — an unhandled "
    "exception there FAILS THE SHIP-GATE (fail-closed, the intended behaviour) or SKIPS A REPORT, "
    "and can NEVER leave a live position unmanaged, so do NOT reject a tooling file on a trade-path "
    "or 'positions-unmanaged' basis. Only main.py / strategy/run_cycle.py / execution/*.py actually "
    "run during RTH. Audit ONLY the CHANGED (+/-) lines for a defect "
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
    "other claim to. A stated ABSENCE ('X does not exist' / 'no wiring' / 'not built') that "
    "carries a VERIFY COMMAND the reader can run (e.g. \"verify: `grep -rn X` -> 0 hits\") IS "
    "earned — do NOT reject it demanding a separate 'proof of absence'; the runnable command IS "
    "the proof. This is a real gate, not a rubber stamp: if in doubt, REJECT "
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

def _curl(url, headers, body_dict, key, timeout=120):
    # curl transport (macOS urllib hits SSL CERTIFICATE_VERIFY_FAILED). `timeout` bounds a SINGLE
    # call so one mute/slow model cannot hang indefinitely; the NVIDIA ladder passes a shorter 70s
    # per-model timeout so a bad model is abandoned quickly. NOTE the aggregate is NOT 120s: the
    # ladder can run up to ~70s x N models and _sub/_gsub retry the whole ladder once, so the
    # substitute path is bounded but can take a few minutes — acceptable for a ship-gate tool that
    # never runs in the live trade loop (an unhandled hang there only delays/fails a ship, closed).
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(body_dict, f)
        path = f.name
    try:
        cmd = ["curl", "-s", url]
        for h in headers:
            cmd += ["-H", h]
        cmd += ["--data-binary", f"@{path}"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
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
        # gpt-oss-120b (was llama-3.3-70b-versatile — DEAD, Groq 404). Reasoning model:
        # reasoning_effort:"low" + max_completion_tokens (not max_tokens) or reasoning eats
        # the budget → empty content. max_completion_tokens 2048 (was 4096): the reservation
        # counts toward Groq's 8k-TPM free cap; at 4096 a diff+context near ~5k input tripped
        # "Request too large ... Requested 8985 > Limit 8000" (2026-08-24). 2048 is ample for a
        # verdict + a cited-defect rationale and keeps input+completion under 8k.
        {"model": "openai/gpt-oss-120b", "reasoning_effort": "low", "messages": [
            {"role": "system",
             "content": "You are a Senior Staff HFT engineer auditing a diff "
                        "before it ships. Concrete, no hedging."},
            {"role": "user", "content": prompt}], "max_completion_tokens": 2048},
        key)
    if "choices" not in r:
        raise RuntimeError(str(r).replace(key, "***")[:200])
    return r["choices"][0]["message"]["content"]

def _split_pure_add_hunk(hunk, limit):
    """Split ONE git hunk that contains NO removed ('-') lines into line-boundary pieces of at most
    ~`limit` chars, each prefixed with the hunk's own '@@' header line. Returns [hunk] unchanged if
    the hunk has any '-' line (a remove/re-add pair must never be separated), has no body, or
    already fits. A single line longer than `limit` becomes its own piece (never dropped)."""
    lines = hunk.splitlines(keepends=True)
    if len(hunk) <= limit or len(lines) < 2 or any(ln.startswith("-") for ln in lines[1:]):
        return [hunk]
    at, body = lines[0], lines[1:]
    pieces, cur = [], ""
    for ln in body:
        if cur and len(at) + len(cur) + len(ln) > limit:
            pieces.append(at + cur)
            cur = ""
        cur += ln
    if cur:
        pieces.append(at + cur)
    return pieces or [hunk]


def _gro_chunked(head, diff_body, ctx_suffix, key):
    # Gro TPM-RESILIENCE (Rafael 2026-09-20 — "stop settling for the first rejection; find a
    # solution"). Groq's free tier caps a SINGLE request at 8k TPM, so a large diff overflows
    # ("Request too large") outright and the old path fell straight to the substitute. Instead,
    # SPLIT the diff into token-bounded chunks, audit EACH with a real Gro call, and COMBINE
    # worst-verdict-wins: any chunk REJECT => the change REJECTs (return that chunk's rejection so
    # the caller surfaces the real defect); all chunks APPROVE => APPROVE. A chunk that stays
    # INDETERMINATE after a reminder RAISES, so the caller falls to the substitute/waive rather than
    # silently passing. Yields a genuine Gro verdict on a diff of ANY size. The returned text carries
    # exactly one `VERDICT:` line so the caller's _verdict() parses it unchanged.
    # KNOWN DEGRADED MODE (cold-2nd 2026-09-20): a defect whose two halves land in SEPARATE chunks
    # can look benign in each chunk -> all-APPROVE (chunking fails toward APPROVE only in this narrow
    # cross-chunk case). MITIGATED: GAI (the required second voice) audits the WHOLE diff single-shot
    # (Gemini's large input budget does not overflow where Gro's 8k TPM does), so a cross-chunk defect
    # is still caught by GAI. Only if GAI is ALSO on its substitute does this mitigation weaken.
    BUDGET = 18000  # chars for head + one chunk + ctx (~4.5k tokens; safely under 8k TPM w/ 2048 reserve)
    room = max(3000, BUDGET - len(head) - len(ctx_suffix))
    # Split on git HUNK boundaries ("@@ ..."), NEVER mid-hunk, so a coherent change stays whole. A
    # function rewritten in place emits its removal (-) and re-addition (+) as ONE hunk; the earlier
    # line-based split cut that hunk across chunks, so a chunk saw the removal WITHOUT the re-addition
    # and FALSE-REJECTED ("_nvidia removed, no replacement" — dogfood 2026-09-20). Keeping each hunk
    # intact eliminates that class. The pre-hunk file header (diff --git / index / --- / +++) is
    # prepended to EVERY chunk so each chunk is a self-describing diff.
    lines = diff_body.splitlines(keepends=True)
    header, hunks, cur_hunk = [], [], None
    for ln in lines:
        if ln.startswith("@@"):
            if cur_hunk is not None:
                hunks.append(cur_hunk)
            cur_hunk = ln
        elif cur_hunk is None:
            header.append(ln)          # pre-hunk file header lines
        else:
            cur_hunk += ln
    if cur_hunk is not None:
        hunks.append(cur_hunk)
    hdr = "".join(header)
    if not hunks:                      # no @@ markers (e.g. the "(no change ...)" placeholder)
        chunks = [diff_body]
    else:
        hroom = max(2000, room - len(hdr))
        # NEW-FILE / PURE-INSERTION HUNKS (2026-09-23): a brand-new file is ONE hunk, and the
        # never-split-a-hunk rule above left any new file larger than ~one chunk permanently
        # un-auditable by Gro ("Request too large" on the single chunk -> no real Gro verdict).
        # The never-split rule exists for hunks that REMOVE and RE-ADD lines (a split could show
        # the removal without its re-addition); a hunk with NO '-' lines has no such pairing, so
        # it is split at LINE boundaries into pieces that each fit, each carrying the hunk's own
        # '@@' header. Hunks with any '-' line stay intact (unchanged behavior). Same known
        # degraded mode as any chunking: a defect spanning two pieces can look benign in each —
        # mitigated because GAI audits the WHOLE diff single-shot.
        hunks = [p for h in hunks for p in (_split_pure_add_hunk(h, hroom) if len(h) > hroom else [h])]
        chunks, cur = [], ""
        for h in hunks:
            if cur and len(cur) + len(h) > hroom:   # never split a hunk; flush the group first
                chunks.append(hdr + cur)
                cur = ""
            cur += h
        if cur:
            chunks.append(hdr + cur)
        if not chunks:
            chunks = [hdr + diff_body]
    n = len(chunks)

    def _gro_rl(p):
        # Groq's 8k TPM is PER-MINUTE, so consecutive chunk calls exhaust the budget and get a 429
        # "rate limit ... tokens per minute". That is NOT a per-request overflow and NOT a reject —
        # wait for the minute to roll and retry (up to 2×). This is what makes multi-chunk auditing
        # actually work within the free cap (Rafael 2026-09-20: "find a solution", not settle).
        for attempt in range(3):
            try:
                return _gro(p, key)
            except Exception as e:
                m = str(e).lower()
                rl = ("rate limit" in m or "rate-limit" in m or ("tokens per minute" in m and "used" in m)
                      or "429" in m)
                if rl and attempt < 2:
                    sys.stderr.write(f"[preship] Gro per-minute TPM hit on a chunk — waiting 62s "
                                     f"(attempt {attempt + 1}/2).\n")
                    time.sleep(62)
                    continue
                raise

    for i, ch in enumerate(chunks, 1):
        marker = (f"(CHUNK {i} of {n} of a larger staged diff — audit ONLY this chunk's changed "
                  f"lines for a defect INTRODUCED here; the other chunks are audited separately. "
                  f"APPROVE this chunk if IT introduces no defect.)\n")
        cprompt = head + marker + ch + ctx_suffix
        txt = _gro_rl(cprompt)
        v = _verdict(txt)
        if v == "INDETERMINATE":
            txt = _gro_rl(cprompt + "\n\nREMINDER: end with exactly ONE line beginning "
                          "`VERDICT: APPROVE` or `VERDICT: REJECT — <defect>`.")
            v = _verdict(txt)
        if v == "REJECT":
            return f"Gro chunked audit ({n} chunks): chunk {i} REJECTED.\n{txt}"
        if v != "APPROVE":
            raise RuntimeError(f"Gro chunk {i}/{n} INDETERMINATE after reminder — cannot combine")
    return f"Gro chunked audit: all {n} chunk(s) APPROVE.\nVERDICT: APPROVE"

def _gai(prompt, key, paid_key=""):
    # Free key is used BY DEFAULT (the free tier is a DAILY quota that RESETS — do not permanently
    # switch to paid after one 429). PAID IS DEFAULT-OFF (Rafael mandate 2026-08-24): paid_key is
    # spent ONLY when Rafael has explicitly opted in via env GEMINI_ALLOW_PAID (1/true/yes/on).
    # Without that opt-in a free-tier 429 raises here and the CALLER engages the FREE option-C
    # substitute (NVIDIA) — paid is NEVER auto-spent. This closes the silent-paid-default: the prior
    # "seamless auto-failover / genuine last resort" was a COMMENT with no enforcement, so paid spend
    # scaled with every free 429 (2 paid top-ups in one month, Aug 2026). Now it is a real gate —
    # least-privilege / default-deny on the ONLY paid path in the repo.
    # MODEL-SELECTION LADDER (2026-08-31 — the tracked follow-up, finally built). A SINGLE pinned
    # Gemini model churns fast: gemini-3.5-flash went 200 -> 503 -> 429 -> 404 within hours on
    # 2026-08-31, which repeatedly false-flagged "GAI down" and dropped ships to the slow NVIDIA
    # substitute. Try a LADDER of family models; a 404 (retired) or 429/503 (quota/overload) on ONE
    # model skips to the next — different models carry SEPARATE free-tier quotas, so a 429 on one is
    # often 200 on the next (verified 2026-08-31 same key: 3.5-flash 404 while 3.7-flash +
    # 3.1-flash-lite returned 200). First model returning candidates wins; only if the WHOLE ladder
    # fails does the caller escalate (free retry -> paid-if-allowed -> NVIDIA substitute).
    # thinkingBudget:0 keeps a thinking model from spending the whole maxOutputTokens on hidden
    # reasoning (INDETERMINATE otherwise). maxOutputTokens 2048 stays under the free per-minute cap.
    _GAI_LADDER = ("gemini-3.1-flash-lite", "gemini-3.7-flash", "gemini-3-flash-preview", "gemini-flash-latest")  # 3.1-flash-lite FIRST: proven clean-VERDICT for the gate. Canonical: gai_client.GAI_MODEL_LADDER

    def _one(k):
        _last_err = None
        for _model in _GAI_LADDER:
            try:
                r = _curl(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{_model}:generateContent?key={k}",
                    ["Content-Type: application/json"],
                    {"contents": [{"parts": [{"text": prompt}]}],
                     "generationConfig": {"maxOutputTokens": 2048,
                                          "thinkingConfig": {"thinkingBudget": 0}}},
                    k)
                if "candidates" in r:
                    return r["candidates"][0]["content"]["parts"][0]["text"]
                _last_err = RuntimeError(str(r).replace(k, "***")[:200])
            except Exception as _e:
                _last_err = _e
            # this model failed (retired 404 / quota 429 / overload 503 / error) — try the next one
        raise _last_err if _last_err else RuntimeError("all GAI ladder models failed")
    # FREE-FIRST discipline (Rafael): the free tier is a ROLLING quota — a brief backoff often
    # clears a per-minute spike without spending paid. Retry free once with a short wait BEFORE
    # any paid attempt; paid is a genuine last resort, not an inertia default.
    last = None
    for attempt in range(2):
        try:
            return _one(key)
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = ("429" in msg or "resource_exhausted" in msg or "quota" in msg
                         or "rate limit" in msg or "rate-limit" in msg or "503" in msg
                         or "unavailable" in msg)
            if transient and attempt == 0:
                time.sleep(6)
                continue
            break
    msg = str(last).lower()
    is_quota = ("429" in msg or "resource_exhausted" in msg or "quota" in msg
                or "rate limit" in msg or "rate-limit" in msg)
    # DEFAULT-DENY paid gate (Rafael mandate 2026-08-24): the paid key is spent ONLY when Rafael has
    # explicitly set env GEMINI_ALLOW_PAID (1/true/yes/on). Unset/false => skip paid entirely and
    # raise, so the caller engages the FREE option-C substitute. Paid can never be reached out of
    # inertia — an owner-controlled grant is required, per the least-privilege principle.
    _allow_paid = os.getenv("GEMINI_ALLOW_PAID", "").strip().lower() in ("1", "true", "yes", "on")
    if _allow_paid and paid_key and paid_key != key and is_quota:
        try:
            sys.stderr.write("[preship] GEMINI_ALLOW_PAID set + GAI free exhausted — one paid last-resort attempt.\n")
            return _one(paid_key)
        except Exception:
            pass
    raise last

# OPTION-C SUBSTITUTE reviewer MODEL LADDER (Rafael-authorized 2026-08-24; laddered 2026-09-20).
# Stands in for GAI or Gro ONLY when that voice is genuinely DOWN (outage/quota/TPM-overflow) so a
# single-provider failure never blocks EVERY ship. Members VERIFIED LIVE via the
# integrate.api.nvidia.com/v1/models probe (2026-09-20) — diverse lineages (Meta / Mistral /
# NVIDIA-tuned-Llama), each a strong INSTRUCT (not a reasoning) model that lands a single clean
# `VERDICT:` line. RE-PINNED history: meta/llama-3.1-70b-instruct RETIRED (410, 2026-08-26);
# nemotron-3-super is live but a REASONING model → INDETERMINATE (excluded). llama-3.2-90b-vision-
# instruct stays FIRST (proven). LADDERED per Rafael 2026-09-20 ("when the fallback doesn't work,
# try another model — it offers more than one; stop settling for the first rejection"): a single
# pinned model that timed out or went mute forced a hard fail + manual --waive-gro (2026-09-20).
_NVIDIA_LADDER = (
    "meta/llama-3.2-90b-vision-instruct",      # proven clean-VERDICT (the prior single pin)
    "mistralai/mistral-large-2-instruct",      # strong general instruct, diverse (Mistral) lineage
    "nvidia/llama-3.1-nemotron-70b-instruct",  # 70b INSTRUCT (NOT the reasoning nemotron-3-super)
)
_NVIDIA_PER_MODEL_TIMEOUT = 70  # bounded per model so trying the whole ladder stays reasonable
_LAST_NVIDIA_MODEL = ""         # which ladder model actually answered (recorded in the marker)


def _nvidia(prompt, key):
    # Try each ladder model in turn. A transport failure (timeout / 4xx / 5xx / no-choices) OR an
    # INDETERMINATE response (no single clean VERDICT line — e.g. a mute/reasoning model) advances
    # to the NEXT model. A model that returns a USABLE verdict (APPROVE or REJECT) is HONORED and
    # returned — the ladder shops past a BROKEN or MUTE model, NEVER past a genuine REJECT
    # (reviewer-shopping for an APPROVE is forbidden by the anti-bias rules). Raises only if the
    # WHOLE ladder yields no usable verdict — the caller then fails closed / --waive path.
    global _LAST_NVIDIA_MODEL
    last_err = None
    for model in _NVIDIA_LADDER:
        try:
            r = _curl(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                [f"Authorization: Bearer {key}", "Content-Type: application/json"],
                {"model": model, "temperature": 0.2, "max_tokens": 2048, "messages": [
                    {"role": "system",
                     "content": "You are a Senior Staff engineer auditing a diff before it ships "
                                "to a live trading bot. Concrete, no hedging. REJECT only for a "
                                "concrete failing input (input -> wrong output/crash) in the CHANGED "
                                "lines, and QUOTE the exact offending line; a theoretical 'could' is a "
                                "NIT, not a REJECT."},
                    {"role": "user", "content": prompt}]},
                key, timeout=_NVIDIA_PER_MODEL_TIMEOUT)
            if "choices" not in r:
                last_err = RuntimeError(f"{model}: {str(r).replace(key, '***')[:160]}")
                continue
            txt = r["choices"][0]["message"]["content"]
            if _verdict(txt) == "INDETERMINATE":
                last_err = RuntimeError(f"{model}: no clean single VERDICT line")
                continue  # mute / reasoning model — try the next
            _LAST_NVIDIA_MODEL = model
            return txt  # a usable verdict (APPROVE or REJECT) — honor it, never shop past it
        except Exception as e:
            last_err = e  # timeout / transport error — advance to the next model
            continue
    raise last_err if last_err else RuntimeError("all NVIDIA ladder models failed")


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

# SELF-QA gate (Rafael mandate 2026-08-09): "am I giving BGG lazy/biased prompts?" — made
# MECHANICAL. The author-supplied --context/--evidence must carry FACTS, never a pre-baked
# VERDICT that leads the reviewer toward APPROVE. A prompt containing verdict-leading language
# is REFUSED (fail-closed) — the author must restate it as a neutral fact and let the reviewer
# reach its own conclusion. This enforces the No-Leading-Prompts rule instead of merely asking.
_BIAS_PATTERNS = [
    r"already approved", r"\bboard (has |already )?(approved|converged|agreed|signed[- ]off)",
    r"\b(gro|gai|board)s?\b[^.]{0,40}\bapprove", r"should (approve|pass|be approved)",
    r"please approve", r"safe to (approve|ship|merge)", r"obviously (safe|correct|fine|right)",
    r"\b(no|zero) risk\b", r"\bharmless\b", r"nothing (to worry|dangerous|risky)",
    r"trivial (change|diff|fix|patch|edit)",
    r"just a (small|minor|tiny|one[- ]?line|1[- ]?line) (change|diff|fix|edit|patch)",
    r"confirmed (safe|correct|dormant|fine|benign)", r"this (change|diff|patch|code) is (safe|correct|fine|benign)",
]


def _check_prompt_bias(*texts):
    """Return a list of verdict-leading phrases found in author-supplied reviewer text.
    Non-empty => the prompt biases the reviewer toward APPROVE and must be neutralized."""
    hits = []
    blob = " ".join(t for t in texts if t)
    low = blob.lower()
    for pat in _BIAS_PATTERNS:
        m = re.search(pat, low)
        if m:
            hits.append(m.group(0).strip())
    return hits


def audit_file(relpath, waive_gro, keys, evidence="", context=""):
    _bias = _check_prompt_bias(context, evidence)
    if _bias:
        return False, (
            f"{relpath}: SELF-QA REFUSE — reviewer context/evidence contains VERDICT-LEADING "
            f"language that biases BGG toward APPROVE: {_bias}. Restate as a neutral FACT (what the "
            f"code does), not a conclusion (that it's safe/approved). See No-Leading-Prompts rule."
        )
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
    # -U15 (was -U30): -U30 on a large diff (e.g. preship_gate.py +148) pushed the prompt over
    # Groq's 8k-TPM free cap ("Requested 8748 > 8000", 2026-08-24). -U15 still shows guards within
    # 15 lines of each hunk (enough to avoid the missing-guard false-reject); the --context flag
    # backfills any guard further away, per the REVIEWER-CONTEXT discipline.
    # CLAIM gate (handoff.md prose) uses -U0: in a factual-claims audit the "context" lines are
    # OTHER, already-shipped claims — showing them 15-deep made the reviewer re-litigate and
    # false-REJECT UNCHANGED bullets (2026-08-31: rejected #203/#209 which this edit never touched)
    # DESPITE the prompt forbidding a reject on a space-prefixed context line. -U0 shows the reviewer
    # ONLY the new/removed claim lines. Code files keep -U15 (nearby guards must stay visible).
    _uctx = "-U0" if relpath in GATED_CLAIM_FILES else "-U15"
    ok_d, diff, _ = _git(["diff", "--cached", base, _uctx, "--", relpath])
    _head = CLAIM_PROMPT_HEAD if relpath in GATED_CLAIM_FILES else PROMPT_HEAD
    _diff_body = diff if diff.strip() else f"(no change vs {base} for {relpath})"
    prompt = _head + _diff_body
    # REVIEWER-CONTEXT block (Rafael mandate 2026-08-02): PROACTIVELY pre-load the
    # diff-specific facts a diff-only view CANNOT show — what a referenced constant
    # MEANS, the threading model, a cross-file helper/caller/guard, the runtime
    # profile — plus refutations of the likely false premises. The tool builds its
    # prompt from the diff alone and so keeps guessing cross-file semantics wrong
    # (e.g. GAI 2026-08-02 assumed MAX_PORTFOLIO_RISK_PCT was an aggregate ceiling and
    # false-REJECTed a correct config diff). A few hundred tokens of facts up front is
    # far cheaper than a counter-prompt round after. Injected as GROUND TRUTH so the
    # reviewer cannot REJECT on an assumption these facts contradict.
    # Build the context/evidence SUFFIX separately so the Gro TPM-overflow chunker can re-attach
    # the SAME ground-truth to every chunk's prompt (each chunk must carry full context).
    _ctx_suffix = ""
    if context.strip():
        _ctx_suffix += ("\n\n--- REVIEWER CONTEXT (author-supplied FACTS about code OUTSIDE "
                        "this diff — treat as GROUND TRUTH; do NOT REJECT on an assumption "
                        "that contradicts a fact stated here) ---\n" + context.strip())
    # DISAGREEMENT PROTOCOL counter-prompt path (Rafael mandate 2026-07-19): a reject
    # on a false premise (e.g. a defect claim about a helper the -U30 diff can't
    # show) is resolved by SHOWING the reviewer the refuting evidence, never by a
    # blind re-roll. Pass it via --evidence.
    if evidence.strip():
        _ctx_suffix += ("\n\n--- COUNTER-PROMPT EVIDENCE (a prior reject rested on a "
                        "premise this refutes; weigh it before re-verdicting; if it "
                        "resolves your stated concern, APPROVE) ---\n"
                        + evidence.strip())
    prompt += _ctx_suffix

    _reminder = ("\n\nREMINDER: exactly ONE line may BEGIN with `VERDICT:` — your single final "
                 "decision `VERDICT: APPROVE` or `VERDICT: REJECT — <defect>`. Do NOT start any "
                 "other line with `VERDICT:` and do NOT restate the format.")

    # GAI (required) — with the OPTION-C substitute on a genuine GAI outage (Rafael 2026-08-24).
    gai_substituted = ""
    try:
        gai_txt = _gai(prompt, keys.get("GEMINI_API_KEY", ""), keys.get("GEMINI_PAID_API_KEY", ""))
        gai_v = _verdict(gai_txt)
        if gai_v == "INDETERMINATE":
            # A parse failure is NOT a content reject (Rafael 2026-07-25) — re-request ONCE
            # with a format reminder before deciding anything.
            gai_txt = _gai(prompt + _reminder, keys.get("GEMINI_API_KEY", ""), keys.get("GEMINI_PAID_API_KEY", ""))
            gai_v = _verdict(gai_txt)
    except Exception as e:
        # OPTION C: GAI genuinely DOWN (free quota exhausted; paid NOT attempted unless the
        # GEMINI_ALLOW_PAID default-deny gate is explicitly opened by Rafael). Engage the
        # substitute so a single-provider outage never blocks EVERY ship. Reached ONLY on a
        # GAI *outage* exception — never on a GAI *REJECT* (a reject returns a verdict, not an
        # exception, and is handled below with full Gro+GAI rigor). No NVIDIA key => fail-closed.
        nk = keys.get("NVIDIA_API_KEY", "")
        if not nk:
            return False, f"{relpath}: GAI audit failed ({e}) and no NVIDIA substitute key — fail-closed, no marker"
        def _sub(p):
            # one retry — the NVIDIA free tier occasionally times out on first hit (2026-08-24).
            try:
                return _nvidia(p, nk)
            except Exception:
                time.sleep(3)
                return _nvidia(p, nk)
        try:
            sys.stderr.write(f"[preship] GAI down ({str(e)[:60]}) — engaging option-C substitute (NVIDIA model ladder).\n")
            gai_txt = _sub(prompt + _reminder)
            gai_v = _verdict(gai_txt)
            if gai_v == "INDETERMINATE":
                gai_txt = _sub(prompt + _reminder)
                gai_v = _verdict(gai_txt)
            gai_substituted = f"NVIDIA_{_LAST_NVIDIA_MODEL}"
        except Exception as e2:
            return False, f"{relpath}: GAI down ({e}) AND option-C substitute failed after retry ({e2}) — fail-closed, no marker"
    _gai_label = f"substitute {gai_substituted}" if gai_substituted else "GAI"
    if gai_v == "INDETERMINATE":
        return False, (f"{relpath}: {_gai_label} INDETERMINATE — no parseable VERDICT line after a retry "
                       f"(NOT a content reject; re-run the audit). Last 300 chars:\n{gai_txt[-300:]}")
    if gai_v != "APPROVE":
        return False, f"{relpath}: {_gai_label} REJECT — no marker.\n{gai_txt[-600:]}"

    # Gro (required unless waived)
    gro_substituted = ""
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
            # Gro's free tier has a HARD 8k-TPM PER-REQUEST cap a large diff can exceed outright
            # ("Request too large"), plus a per-minute budget that rate-limits under load — a Gro
            # OUTAGE, not a REJECT (a reject returns a verdict, not an exception).
            # STEP 1 (Rafael 2026-09-20 — "stop settling"): if this is a TPM/size overflow, get a
            # REAL Gro verdict by CHUNKING the diff, BEFORE falling to any substitute.
            _em = str(e).lower()
            _tpm = ("request too large" in _em or "tokens per minute" in _em or " tpm" in _em
                    or "8000" in _em or "413" in _em or "too large" in _em)
            gro_v = None
            if _tpm:
                try:
                    sys.stderr.write("[preship] Gro TPM-overflow — chunking the diff for a REAL Gro "
                                     "verdict (not settling for the substitute).\n")
                    gro_txt = _gro_chunked(_head, _diff_body, _ctx_suffix, keys.get("GROQ_API_KEY", ""))
                    gro_v = _verdict(gro_txt)
                    if gro_v not in ("APPROVE", "REJECT"):
                        gro_v = None  # indeterminate chunk -> fall to substitute below
                except Exception as _ce:
                    sys.stderr.write(f"[preship] Gro chunked audit failed ({str(_ce)[:80]}) — "
                                     "falling to option-C substitute.\n")
                    gro_v = None
            if gro_v is None:
                # STEP 2 — OPTION-C SYMMETRY (Rafael 2026-08-28): engage the NVIDIA model-ladder
                # substitute the GAI path uses, instead of hard-failing to a manual --waive-gro.
                # Reached ONLY on a Gro *outage* (or a chunked audit that could not land a verdict),
                # never a *REJECT*. No NVIDIA key => the old --waive-gro path.
                nk = keys.get("NVIDIA_API_KEY", "")
                if not nk:
                    return False, (f"{relpath}: Gro audit failed ({e}) and no NVIDIA substitute "
                                   "key. Re-run with --waive-gro only if Rafael authorizes.")
                def _gsub(p):
                    try:
                        return _nvidia(p, nk)
                    except Exception:
                        time.sleep(3)
                        return _nvidia(p, nk)
                try:
                    sys.stderr.write(f"[preship] Gro down ({str(e)[:60]}) — engaging option-C substitute (NVIDIA model ladder).\n")
                    gro_txt = _gsub(prompt + _reminder)
                    gro_v = _verdict(gro_txt)
                    if gro_v == "INDETERMINATE":
                        gro_txt = _gsub(prompt + _reminder)
                        gro_v = _verdict(gro_txt)
                    gro_substituted = f"NVIDIA_{_LAST_NVIDIA_MODEL}"
                except Exception as e2:
                    return False, (f"{relpath}: Gro down ({e}), chunked audit did not land a verdict, "
                                   f"AND option-C substitute failed ({e2}). Re-run with --waive-gro "
                                   "only if Rafael authorizes.")
        _gro_lbl = f"substitute {gro_substituted}" if gro_substituted else "Gro"
        if gro_v == "INDETERMINATE":
            return False, (f"{relpath}: {_gro_lbl} INDETERMINATE — no parseable VERDICT line after a "
                           f"retry (NOT a content reject; re-run). Last 300 chars:\n{gro_txt[-300:]}")
        if gro_v != "APPROVE":
            return False, f"{relpath}: {_gro_lbl} REJECT — no marker.\n{gro_txt[-600:]}"
    # DIVERSITY NOTE: if BOTH voices fell back to the same NVIDIA model, the 2-voice review is
    # degraded to one model reviewing twice — still better than blocking, and the marker records
    # both substitutions so a true 2-voice re-run is available once a primary recovers. Surfaced,
    # never silently passed (Rafael-visible on stderr).
    if gro_substituted and gai_substituted and gro_substituted == gai_substituted:
        sys.stderr.write(f"[preship] NOTE: both Gro and GAI fell back to {gro_substituted} — "
                         "degraded 2-voice (same model twice); re-run when a primary provider "
                         "recovers for an independent second voice.\n")

    os.makedirs(MARKER_DIR, exist_ok=True)
    # marker["gai"] MUST stay the literal "APPROVE": the ship gate (preship_gate._marker_ok)
    # exact-matches `gai == "APPROVE"` — a decorated value like "APPROVE (SUBST:...)" fails that
    # check and BLOCKS the very ship the substitute just approved (cold-2nd 2026-08-24, self-
    # defeating fail-closed bug). The substitution is recorded ONLY in the `gai_substituted`
    # key (which the gate ignores) — that fully satisfies auditability without touching `gai`.
    marker = {"sha256": sha, "gro": gro_v, "gai": "APPROVE", "ts": int(time.time()),
              "file": relpath}
    if gai_substituted:
        marker["gai_substituted"] = gai_substituted  # audit trail: GAI was down; this voice stood in
    if gro_substituted:
        marker["gro_substituted"] = gro_substituted  # audit trail: Gro was down; this voice stood in
    with open(os.path.join(MARKER_DIR, relpath.replace("/", "__") + ".json"), "w") as f:
        json.dump(marker, f, indent=2)
    _gai_disp = f"APPROVE (SUBST:{gai_substituted})" if gai_substituted else "APPROVE"
    _gro_disp = f"{gro_v} (SUBST:{gro_substituted})" if gro_substituted else gro_v
    return True, (f"{relpath}: APPROVED (gro={_gro_disp} gai={_gai_disp}) — marker "
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
