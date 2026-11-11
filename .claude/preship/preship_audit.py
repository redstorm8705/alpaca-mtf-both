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

--waive-gro : waive reviewer #2 (Codex, or its NVIDIA-pair / Gro fallback) — record gro=WAIVED (Rafael-authorized
              only). GAI must still APPROVE. Reviewer #2 is Codex since REVIEWER-SWAP-1025 (2026-10-25); when Codex
              is unavailable the NVIDIA pair (Mistral Large 4 + MiniMax M3.1) reviews, Gro only if NVIDIA is down
              and the diff fits one request (NV-REVIEW-1110).
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
import math
import statistics
import uuid
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preship_gate as _pg  # noqa: E402 — the cold-2nd freshness rule is reused from the gate, not copied
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

def _scrub(text, key=""):
    """_redact() (every .env value) plus the call's own key, for any error text that leaves a provider call."""
    text = _redact(str(text))
    return text.replace(key, "***") if key else text


class ProviderError(RuntimeError):
    """A provider's own error reply, summarized (GAI-RETRY-1111). retry_after = the seconds the provider asked to wait
    (Google's RetryInfo.retryDelay), None when it gave none; per_day = every exhausted quota is a daily one;
    last_reason = the LAST ladder model's own reason (the text the retry and paid-key decisions classify, exactly as
    before GAI-RETRY-1111 — the joined message only adds information, it never changes those decisions)."""

    def __init__(self, msg, retry_after=None, per_day=False, last_reason=None, raw=None):
        super().__init__(msg)
        self.retry_after = retry_after
        self.per_day = per_day
        self.last_reason = msg if last_reason is None else last_reason
        # raw = the exact text origin/main raised for a provider error (scrubbed str(error), first 200 chars). The
        # retry and paid-key decisions classify THIS string, so they stay identical to before (cold-2nd round 3).
        self.raw = raw


def _error_summary(err, key=""):
    """Provider error object -> (summary text, retry_after seconds or None, per_day). Keeps what the old 200-character
    cut of str(dict) dropped: the code, status, message, each exhausted quota id and limit, and Google's retryDelay.
    Every piece is scrubbed (all .env values + `key`) BEFORE it is cut, so a cut can never leave part of a key."""
    if not isinstance(err, dict):
        return _scrub(err, key)[:300], None, False
    # Order: code, status, each quota, retryDelay, THEN the (long, generic) message, so a later cut drops the
    # message first and never the quota id or the wait (adversarial review round 1).
    parts = [_scrub(err.get("code", ""), key), _scrub(err.get("status", ""), key)]
    retry, _qids = None, []
    details = err.get("details")
    for d in details if isinstance(details, list) else []:
        if not isinstance(d, dict):
            continue
        viols = d.get("violations")
        for v in viols if isinstance(viols, list) else []:
            if isinstance(v, dict):
                qid = _scrub(v.get("quotaId") or v.get("quotaMetric") or "", key)[:120]
                parts.append(f"quota={qid} limit={_scrub(v.get('quotaValue', '?'), key)[:20]}")
                _qids.append(qid.lower())
        rd = d.get("retryDelay")
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)s\s*", rd) if isinstance(rd, str) else None
        if m:
            _r = float(m.group(1))
            retry = _r if retry is None else min(retry, _r)
            parts.append(f"retryDelay={_r:g}s")
    parts.append(_scrub(err.get("message", ""), key)[:240])
    per_day = bool(_qids) and all("perday" in q for q in _qids)   # daily only when EVERY listed quota is daily
    text = " | ".join(x for x in parts if x)
    return (text or _scrub(err, key)[:300]), retry, per_day


def _curl(url, headers, body_dict, key, timeout=120):
    # curl transport (macOS urllib hits SSL CERTIFICATE_VERIFY_FAILED). `timeout` bounds a SINGLE call.
    # KEY SAFETY (NV-REVIEW-1110 item 6): no secret is ever on curl's argv or in the URL. Headers (the API key among
    # them) go through a header file read with `-H @file`; the request body through `--data-binary @file`. Both files
    # are created by mkstemp (O_EXCL, mode 600, a per-process prefix plus a random suffix, so parallel pair calls
    # never share one) and deleted in the same try/finally. A TimeoutExpired is re-raised WITHOUT its argv, and every
    # error string is scrubbed (_redact masks every .env value), because on 2026-11-09 a TimeoutExpired message
    # printed the NVIDIA key that used to sit on argv.
    import tempfile
    host = url.split("/")[2] if url.count("/") >= 2 else "?"
    tag = f"preship_{os.getpid()}_"
    paths = []
    try:
        hfd, hpath = tempfile.mkstemp(prefix=tag, suffix=".hdr")
        paths.append(hpath)
        os.fchmod(hfd, 0o600)
        with os.fdopen(hfd, "w") as f:
            f.write("".join(h + "\n" for h in headers))
        bfd, bpath = tempfile.mkstemp(prefix=tag, suffix=".json")
        paths.append(bpath)
        with os.fdopen(bfd, "w") as f:
            json.dump(body_dict, f)
        cmd = ["curl", "-s", "-H", f"@{hpath}", "--data-binary", f"@{bpath}", url]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"curl to {host} timed out after {timeout}s") from None
        except OSError as e:
            raise RuntimeError(_scrub(f"curl to {host} could not start ({type(e).__name__}: {e})", key)) from None
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass
    try:
        r = json.loads(out)
    except ValueError:
        raise RuntimeError(_scrub(f"non-JSON reply from {host}: {(out or '')[:160]}", key)) from None
    if isinstance(r, dict) and r.get("error"):
        _msg, _retry, _per_day = _error_summary(r["error"], key)
        raise ProviderError(_scrub(_msg, key)[:600], _retry, _per_day, raw=_scrub(str(r["error"]), key)[:200])
    return r

# PAPER-ACCOUNT FRAME (Rafael mandate 2026-09-26, CLAUDE.md "THIS IS A PAPER TRADING ACCOUNT").
# Prepended to EVERY Gro / GAI / NVIDIA-substitute prompt so no reviewer reasons from generic
# capital-preservation practice instead of this account's purpose. Facts only (no verdict), so it
# does not trip the bias gate. Mechanism, not a wish: every BGG call routes through _framed().
PAPER_FRAME = (
    "FRAME (applies to every judgement below): this is a PAPER trading account; the goal is "
    "$2.5K -> $25K. Until real money, data collection and edge evaluation are the #1 objective "
    "and the bot trades every day. The only daily risk brake is the 7% daily kill switch (it "
    "halts new entries only). The safety envelope is unchanged: stops on every position, never "
    "mask a loss, code correctness. Buy-and-hold tiers (QHM, Forever-6) sit outside swing "
    "limits.\n\n"
)


def _framed(prompt):
    """Prefix PAPER_FRAME once (idempotent)."""
    return prompt if prompt.startswith(PAPER_FRAME) else PAPER_FRAME + prompt


def _gro(prompt, key):
    prompt = _framed(prompt)
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
        raise RuntimeError(_scrub(str(r), key)[:200])
    return r["choices"][0]["message"]["content"]


# REVIEWER #2 = CODEX (BGC gate: Board + GAI + Codex — REVIEWER-SWAP-1025, owner 2026-10-25; design record
# logs/design_records/reviewer_swap_codex_2026-10-25.md). Bake-off: Codex 7/8 bugs and 1/12 false rejects; Gro high
# 4-5/12 false rejects, and Gro's 8k-token free cap cannot take a large diff. Gro (low) is the fallback when Codex is
# unavailable. Every Codex prompt starts with REVIEW_SENTINEL: approval capture scans Codex session files, and
# owner_approval_capture._NOT_HUMAN rejects any message carrying it, so a review prompt can never count as an approval.
REVIEW_SENTINEL = "[AUTOMATED-REVIEW-PROMPT: code-review prompt sent by preship_audit.py, not a message from the owner]"
CODEX_PERSONA = ("You are an expert engineer at a quantitative / algorithmic trading fund, auditing a diff before it "
                 "ships. Concrete and technical, no hedging.")
_CODEX_TIMEOUT = 600
# Every Codex signature form in this repo's history (git log, 2026-10-25): "(OpenAI Codex)", "Signed: OpenAI Codex",
# "Signed-off-by: ChatGPT/Codex <noreply@openai.com>", "Agent-Signature: OpenAI Codex (...)". A Claude commit that only
# mentions Codex ("... for Claude, Codex and every worktree") does not match.
_CODEX_SIGNED = re.compile(r"(?i)\bopenai\s+codex\b|\bchatgpt\s*/\s*codex\b|\bsigned(?:-off-by)?:\s*codex\b"
                           r"|\bagent-signature:\s*(?:openai\s+)?codex\b|noreply@openai\.com")
# Every reviewer verdict (GAI, Codex, each NVIDIA pair model, Gro), one JSON row each, in the git COMMON dir (shared by
# every worktree of this repository, so a re-run from another worktree sees it). It is the open-REJECT ledger that
# blocks reviewer shopping across runs (_open_rejects) and the record the reversal criterion is counted from.
_VERDICT_LOG_NAME = "reviewer-verdicts.jsonl"
# GAI verdicts go to a SEPARATE append-only ledger beside it, same row format (NV-REVIEW-1110 item 5: GAI verdicts are
# logged so a GAI REJECT blocks every fallback). reviewer-verdicts.jsonl keeps its reviewer-#2-and-pair content, which
# its existing readers and tests rely on; the open-REJECT check reads BOTH ledgers, so the no-shopping rule is the same.
_GAI_LOG_NAME = "gai-verdicts.jsonl"
_COMMON_DIR_CACHE: list = []   # the resolved git common dir, resolved ONCE per process (no extra git call per ledger read)


def _git_common_dir():
    if not _COMMON_DIR_CACHE:
        ok, out, _ = _git(["rev-parse", "--git-common-dir"])
        d = out.strip() if ok and out.strip() else os.path.join(REPO, ".git")
        _COMMON_DIR_CACHE.append(d if os.path.isabs(d) else os.path.join(REPO, d))
    return _COMMON_DIR_CACHE[0]


try:   # resolve once at import, so a ledger read inside an audit run adds no git call of its own
    _git_common_dir()
except Exception:  # noqa: BLE001 — no git here: resolved lazily (falling back to REPO/.git) on first use
    _COMMON_DIR_CACHE.clear()


def _verdict_log_path():
    return os.path.join(_git_common_dir(), _VERDICT_LOG_NAME)


def _gai_log_path():
    # beside the reviewer ledger, wherever that resolves (a test that points _verdict_log_path at a temp dir moves both)
    return os.path.join(os.path.dirname(_verdict_log_path()), _GAI_LOG_NAME)


def _log_verdict(relpath, sha, reviewer, verdict, reason="", note="", evidence=False, run_id="", auto_counter=False):
    """Append one reviewer verdict row (append-only; a write failure is reported, never fatal). `evidence` = the run
    carried the AUTHOR's --evidence (an APPROVE on the same content then clears that reviewer's open REJECTs on it).
    `run_id` = the audit run that wrote the row; `auto_counter` = the row is the result of the automatic name-premise
    counter-prompt, which clears ONLY the REJECT that the same reviewer logged in the same run (the one it answered).
    Reason and note are scrubbed of every .env value before they are written. GAI rows go to the separate GAI ledger."""
    row = {"ts": int(time.time()), "file": relpath, "sha256": sha, "reviewer": reviewer, "verdict": verdict,
           "reason": _redact(reason or "")[-300:], "note": _redact(note or ""), "evidence": bool(evidence),
           "run": run_id, "auto_counter": bool(auto_counter), "adjudicated": None}
    try:
        with open(_gai_log_path() if reviewer == "gai" else _verdict_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        sys.stderr.write(f"[preship] reviewer verdict log write failed ({e}) — verdict still enforced this run.\n")


def _ledger_rows(relpath):
    """Rows for one file from BOTH ledgers (reviewer + GAI), oldest first by ts (stable within a ledger). An unreadable
    ledger contributes nothing."""
    rows = []
    for path in (_verdict_log_path(), _gai_log_path()):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and r.get("file") == relpath:
                        rows.append(r)
        except OSError:
            continue
    rows.sort(key=lambda r: r.get("ts") if isinstance(r.get("ts"), (int, float)) else 0)
    return rows


def _open_rejects(relpath, sha):
    """NO REVIEWER SHOPPING (NV-REVIEW-1110 item 5): the reviewers whose REJECT is still open for this content.
    A REJECT by reviewer R stays open until R itself APPROVEs later content (a different sha) or APPROVEs the same
    content on a run that carried --evidence (the author answered it and R cleared it). An INDETERMINATE, a WAIVED row,
    or another reviewer's APPROVE never clears it. Scope: a reject on THIS sha; Codex's reject is file-wide (any sha —
    the REVIEWER-SWAP-1025 rule, kept), so a trivial edit plus a Codex outage still cannot route the file elsewhere.
    Every fallback (the NVIDIA pair in either slot, Gro) is refused while any reviewer other than itself is listed.
    ONLY FALLBACKS are gated by this ledger (design item 5: "blocks every fallback"): a primary reviewer (Codex, GAI)
    re-run on the same content is not refused by it — that reviewer's own new verdict decides its slot.
    Open REJECTs are tracked per (reviewer, sha): a REJECT on content A is never cleared by the same reviewer's later
    REJECT on content B; only that reviewer's later APPROVE of a different sha, or its APPROVE of A on an AUTHOR
    --evidence run, clears every (reviewer, A) entry. An APPROVE from the automatic name-premise counter-prompt
    (auto_counter) clears ONLY the (reviewer, A) REJECT logged in its own run — never an earlier run's genuine REJECT
    and never a REJECT on any other sha."""
    return sorted({rv for rv, s, _run in _open_reject_entries(relpath) if s == sha or rv == "codex"})


def _open_reject_entries(relpath):
    """The open REJECT entries for a file as (reviewer, sha, run id) — the ledger state _open_rejects reads."""
    opened = set()
    for r in _ledger_rows(relpath):
        rv, v, s, run = r.get("reviewer"), r.get("verdict"), r.get("sha256"), r.get("run") or None
        if not rv:
            continue
        if v == "REJECT":
            opened.add((rv, s, run))
        elif v == "APPROVE":
            auto = r.get("auto_counter") is True
            author_ev = r.get("evidence") is True

            def _clears(e, rv=rv, s=s, run=run, auto=auto, author_ev=author_ev):
                orv, osha, orun = e
                if orv != rv:
                    return False
                if auto:   # answers ONE reject: the same reviewer's, same content, same run — nothing else
                    return run is not None and orun == run and osha == s
                return osha != s or author_ev
            opened = {e for e in opened if not _clears(e)}
    return opened


def _codex_rejected(relpath):
    """True when Codex has an open REJECT on this file (any content) — kept as the file-wide special case of
    _open_rejects (unknown sha → only the file-wide Codex entry can match)."""
    return "codex" in _open_rejects(relpath, None)


def _fallback_refusal(relpath, sha, own_ids, evidence, ignore=frozenset()):
    """'' when a fallback reviewer whose verdict-log ids are `own_ids` may stand in; else the reason it may not.
    Another reviewer's open REJECT blocks it outright; its own open REJECT on this content needs --evidence.
    `ignore` = reviewers that are NOT being replaced because they review in this same run (Codex, whose own verdict
    then decides its slot); their entries are left out of both checks."""
    open_r = [r for r in _open_rejects(relpath, sha) if r not in ignore]
    others = [r for r in open_r if r not in own_ids]
    if others:
        return (f"open REJECT from {', '.join(others)} on this file — no fallback may stand in until that reviewer "
                f"approves later content, or approves this content on a run with --evidence")
    if open_r and not (evidence or "").strip():
        return (f"its own open REJECT ({', '.join(open_r)}) on this exact content — answer it with --evidence; a "
                f"blind re-run does not clear it")
    return ""


class CodexUnavailable(RuntimeError):
    """Codex could not give a verdict (not installed, usage limit, timeout, error, empty output) — never a REJECT."""


_CODEX_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "CODEX_HOME")


def _redact(text):
    """Mask every .env value (6+ characters) in reviewer output before it is printed, logged or stored."""
    for v in sorted({v.strip().strip('"') for v in _load_env().values()}, key=len, reverse=True):
        if len(v) >= 6:
            text = text.replace(v, "***")
    return text


def _codex(prompt):
    """Codex CLI on the owner's ChatGPT plan: read-only sandbox (it can still READ files by absolute path — it cannot
    write), started in an empty temporary directory with a minimal environment (none of the .env keys), prompt on
    stdin. Returns its reply with .env values masked; raises CodexUnavailable on any failure to produce one."""
    import shutil
    import tempfile
    exe = shutil.which("codex")
    if not exe:
        raise CodexUnavailable("codex command line not installed")
    text = REVIEW_SENTINEL + "\n\n" + CODEX_PERSONA + "\n\n" + _framed(prompt)
    env = {k: os.environ[k] for k in _CODEX_ENV_KEEP if k in os.environ}
    with tempfile.TemporaryDirectory() as work:
        try:
            r = subprocess.run([exe, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--cd", work, "-"],
                               input=text, capture_output=True, text=True, timeout=_CODEX_TIMEOUT, env=env)
        except subprocess.TimeoutExpired:
            raise CodexUnavailable(f"timed out after {_CODEX_TIMEOUT}s") from None
        except OSError as e:
            raise CodexUnavailable(f"could not start ({type(e).__name__})") from None
    out = _redact((r.stdout or "").strip())
    if r.returncode != 0 or not out:
        tail = _redact(((r.stderr or "") + (r.stdout or ""))[-200:]).replace("\n", " ")
        kind = "usage limit" if re.search(r"(?i)usage limit|rate limit|quota", tail) else f"exit {r.returncode}"
        raise CodexUnavailable(f"{kind}: {tail}")
    return out


def _codex_signed(relpath):
    """True when a commit touching `relpath` since origin/main is signed by Codex. Then Codex AND Gro must both
    approve (Codex alone would be reviewing its own work; Gro alone would be the weaker reviewer, and a forged
    signature could buy it). Both agents commit under the owner's name, so the message text is the signal; a forged
    signature only ADDS a reviewer. If the history cannot be read (no origin/main, git error) this returns True.
    Uncommitted Codex edits cannot be seen (documented limit)."""
    ok, log, _ = _git(["log", "--format=%B%n%an <%ae>", "origin/main..HEAD", "--", relpath])
    return (not ok) or bool(_CODEX_SIGNED.search(log))


def _codex_review(prompt, reminder, names_ok, relpath):
    """Reviewer #2 via Codex: (verdict, text) or (None, reason) when Codex is unavailable before giving any verdict.
    Once Codex has given a verdict, a later failure keeps that verdict — Gro can never overturn a Codex REJECT."""
    try:
        txt = _codex(prompt)
    except CodexUnavailable as e:
        return None, str(e)
    v = _verdict(txt)
    try:
        if v == "INDETERMINATE":
            txt = _codex(prompt + reminder)
            v = _verdict(txt)
        if v == "REJECT" and _name_premise_reject(txt, names_ok):
            sys.stderr.write("[preship] Codex rejected on an undefined-name premise the static check contradicts — "
                             "one automatic counter-prompt with that evidence.\n")
            txt = _codex(prompt + _name_counter(txt) + reminder)
            v = _verdict(txt)
    except CodexUnavailable as e:
        sys.stderr.write(f"[preship] Codex follow-up failed ({str(e)[:80]}) — keeping its verdict {v}.\n")
    return v, txt


# NO CHUNKED REVIEW (NV-REVIEW-1110 item 1): audit_file no longer chunks. A reviewer that sees one chunk cannot see a
# defect, or its guard, in another — 13 refuted Gro REJECTs on 2026-11-09/10. Gro now reviews only a diff that fits ONE
# request; an oversized diff with the NVIDIA endpoint down fails closed. _split_pure_add_hunk and _gro_chunked below are
# RETAINED ONLY as pure helpers for the existing verdict tests (test_verdict.py::_chunker_checks); nothing on the live
# audit path calls them.
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
    """RETAINED for test_verdict.py only (not called by audit_file since NV-REVIEW-1110). Splits the diff on hunk
    boundaries into ~18000-char chunks (pure-add hunks split at line boundaries), asks _gro once per chunk (plus one
    format-reminder retry on INDETERMINATE; there is NO 429 / rate-limit wait-and-retry any more — any _gro exception
    propagates), and combines worst-verdict-wins: any chunk REJECT → that rejection; all APPROVE → APPROVE; a chunk
    still INDETERMINATE after the reminder raises."""
    BUDGET = 18000  # chars for head + one chunk + ctx (~4.5k tokens; safely under 8k TPM w/ 2048 reserve)
    room = max(3000, BUDGET - len(head) - len(ctx_suffix))
    lines = diff_body.splitlines(keepends=True)
    header, hunks, cur_hunk = [], [], None
    for ln in lines:
        if ln.startswith("@@"):
            if cur_hunk is not None:
                hunks.append(cur_hunk)
            cur_hunk = ln
        elif cur_hunk is None:
            header.append(ln)
        else:
            cur_hunk += ln
    if cur_hunk is not None:
        hunks.append(cur_hunk)
    hdr = "".join(header)
    if not hunks:
        chunks = [diff_body]
    else:
        hroom = max(2000, room - len(hdr))
        hunks = [p for h in hunks for p in (_split_pure_add_hunk(h, hroom) if len(h) > hroom else [h])]
        chunks, cur = [], ""
        for h in hunks:
            if cur and len(cur) + len(h) > hroom:
                chunks.append(hdr + cur)
                cur = ""
            cur += h
        if cur:
            chunks.append(hdr + cur)
        if not chunks:
            chunks = [hdr + diff_body]
    n = len(chunks)
    for i, ch in enumerate(chunks, 1):
        marker = (f"(CHUNK {i} of {n} of a larger staged diff — audit ONLY this chunk's changed "
                  f"lines for a defect INTRODUCED here; the other chunks are audited separately. "
                  f"APPROVE this chunk if IT introduces no defect.)\n")
        cprompt = head + marker + ch + ctx_suffix
        txt = _gro(cprompt, key)
        v = _verdict(txt)
        if v == "INDETERMINATE":
            txt = _gro(cprompt + "\n\nREMINDER: end with exactly ONE line beginning "
                       "`VERDICT: APPROVE` or `VERDICT: REJECT — <defect>`.", key)
            v = _verdict(txt)
        if v == "REJECT":
            return f"Gro chunked audit ({n} chunks): chunk {i} REJECTED.\n{txt}"
        if v != "APPROVE":
            raise RuntimeError(f"Gro chunk {i}/{n} INDETERMINATE after reminder — cannot combine")
    return f"Gro chunked audit: all {n} chunk(s) APPROVE.\nVERDICT: APPROVE"



# Groq's own size refusal ("Request too large for model ... Requested 8985 > Limit 8000", 2026-08-24) or an HTTP 413 /
# context-length error. A per-minute 429 rate limit is NOT a size refusal (it also fails closed, with its own message).
_TOO_LARGE_RE = re.compile(r"(?i)request too large|too large|\b413\b|context length|maximum context")


# GAI-RETRY-1111 retry wait. Without a retryDelay the old fixed 6 s stays. With one, Google's own figure is used, up
# to one per-minute quota window (60 s) plus 5 s margin; a longer asked wait means a quota that will not clear within
# this run, so the caller's fallback runs at once instead of sleeping.
_GAI_DEFAULT_WAIT_S = 6
_GAI_MAX_WAIT_S = 65


def _gai(prompt, key, paid_key=""):
    prompt = _framed(prompt)
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
    _GAI_LADDER = ("gemini-3.1-flash-lite", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest")  # 3.1-flash-lite FIRST: proven clean-VERDICT for the gate. Canonical: gai_client.GAI_MODEL_LADDER; test_ladder_parity.py fails on drift

    def _one(k):
        _last_err = None
        _errs, _retries, _days = [], [], []
        for _model in _GAI_LADDER:
            try:
                # The key travels in the x-goog-api-key header (via _curl's header file), never in the URL
                # (NV-REVIEW-1110 item 6: a URL ?key= can surface in an error or a timeout message).
                r = _curl(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{_model}:generateContent",
                    [f"x-goog-api-key: {k}", "Content-Type: application/json"],
                    {"contents": [{"parts": [{"text": prompt}]}],
                     "generationConfig": {"maxOutputTokens": 2048,
                                          "thinkingConfig": {"thinkingBudget": 0}}},
                    k)
                if "candidates" in r:
                    return r["candidates"][0]["content"]["parts"][0]["text"]
                _last_err = RuntimeError(_scrub(str(r), k)[:200])
            except Exception as _e:
                _last_err = _e
            # this model failed (retired 404 / quota 429 / overload 503 / error) — try the next one
            _errs.append(f"{_model}: {_scrub(_last_err, k)[:600]}")
            _ra = getattr(_last_err, "retry_after", None)
            if _ra is not None:
                _retries.append(_ra)
            _days.append(bool(getattr(_last_err, "per_day", False)))
        # GAI-RETRY-1111: every model's reason, not only the last one's, so a 429 shows WHICH quota ran out and how
        # long Google asked to wait. The soonest retryDelay any model gave is the wait; per_day only when every model
        # in the ladder reported a daily quota.
        raise ProviderError("; ".join(_errs)[:4000] or "all GAI ladder models failed",
                            min(_retries) if _retries else None, bool(_days) and all(_days),
                            # classification text = what origin/main raised for the LAST model: a provider error's
                            # old 200-char text, otherwise the exception text itself.
                            (getattr(_last_err, "raw", None) or _scrub(_last_err, k)) if _last_err is not None
                            else "all GAI ladder models failed")
    # FREE-FIRST discipline (Rafael): the free tier is a ROLLING quota — a brief backoff often
    # clears a per-minute spike without spending paid. Retry free once with a short wait BEFORE
    # any paid attempt; paid is a genuine last resort, not an inertia default.
    last = None
    for attempt in range(2):
        try:
            return _one(key)
        except Exception as e:
            last = e
            msg = str(getattr(e, "last_reason", e)).lower()   # the last model's reason, as before GAI-RETRY-1111
            transient = ("429" in msg or "resource_exhausted" in msg or "quota" in msg
                         or "rate limit" in msg or "rate-limit" in msg or "503" in msg
                         or "unavailable" in msg)
            if transient and attempt == 0:
                # GAI-RETRY-1111: wait the time Google asked for (RetryInfo.retryDelay, +1 s) instead of a fixed
                # 6 s; no wait when every model's quota is a daily one (it will not reset in a retry window) or
                # when the asked wait exceeds one per-minute quota window plus margin (_GAI_MAX_WAIT_S).
                _ra = getattr(e, "retry_after", None)
                if getattr(e, "per_day", False):
                    sys.stderr.write("[preship] GAI daily quota exhausted on every model — no retry wait.\n")
                    break
                _wait = _GAI_DEFAULT_WAIT_S if _ra is None else _ra + 1
                if _wait > _GAI_MAX_WAIT_S:
                    sys.stderr.write(f"[preship] GAI asked to wait {_ra:.0f}s (> {_GAI_MAX_WAIT_S}s) — no retry.\n")
                    break
                sys.stderr.write(f"[preship] GAI busy — waiting {_wait:.0f}s before one retry.\n")
                time.sleep(_wait)
                continue
            break
    msg = str(getattr(last, "last_reason", last)).lower()   # unchanged paid-key classification (last model only)
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

# NVIDIA REVIEWER PAIR (NV-REVIEW-1110, owner 2026-11-10: "I'd prefer we use mistral vs groq for reviews"; design
# logs/design_records/nv_review_2026-11-10.md). Replaces the old one-model-at-a-time NVIDIA ladder. The pair is
# reviewer #2 whenever Codex is unavailable (every diff size), and the GAI stand-in whenever Gemini is down.
# Chosen by the 2026-11-09 bake-off on the BAKEOFF-1025 cases (4 planted bugs, 6 clean, x2): Mistral Large 4 caught
# 7/8 bugs with 0/12 false rejects, MiniMax M3.1 6/8 and 0/12; the old ladder head llama-3.2-90b caught 2/4 with 3/6
# false rejects. Both pair models often return EMPTY content (no verdict 7/20 and 9/20) — hence two models, a retry
# per model, and fail-closed when neither lands a verdict.
_NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
_NVIDIA_PAIR = ("mistralai/mistral-large-4-1t-a52b", "minimaxai/minimax-m3.1-flash")
_PAIR_LABEL = "nvidia-pair"
_NVIDIA_SYSTEM = ("You are a Senior Staff engineer auditing a diff before it ships to a live trading bot. Concrete, "
                  "no hedging. REJECT only for a concrete failing input (input -> wrong output/crash) in the CHANGED "
                  "lines, and QUOTE the exact offending line; a theoretical 'could' is a NIT, not a REJECT.")
# BAKE-OFF MEASUREMENTS the per-model limits are DERIVED from (design item 4). Seconds of every SUCCESSFUL run (a
# parsed APPROVE or REJECT) per model, in file order, from the 2026-11-09 result file nv_results_1794292058.json
# (rows [model, case_id, expected, verdict, seconds, tail]; that run used max_tokens 6000, temperature 0.2, a 420 s
# cap, the _NVIDIA_SYSTEM message above and the _framed() prompt).
_BAKEOFF_OK_SECONDS = {
    "mistralai/mistral-large-4-1t-a52b": (217, 211, 277, 83, 264, 204, 185, 190, 195, 238, 224, 37, 20),
    "minimaxai/minimax-m3.1-flash": (224, 103, 51, 58, 136, 103, 115, 80, 22, 30, 13),
}
# The token budget the bake-off accuracy was measured at. The result file stores only the last 400 characters of
# each reply, so output length cannot be measured from it; a smaller budget would be a guess the data does not
# support (and the models that went silent were the ones that ran long), so the measured budget is kept.
_BAKEOFF_MAX_TOKENS = 6000


def _derived_timeout(samples):
    """Per-model timeout = p90 of the successful bake-off latencies plus headroom equal to the upper spread
    (p90 - median). Mistral: p90 258.8 s, median 204 s -> 314 s; MiniMax: p90 136 s, median 80 s -> 192 s. Both models
    run in parallel, so a slot is bounded by the slower model's own serial calls: the first ask, one no-verdict retry,
    and at most one name-premise counter-prompt — worst case about 3 serial calls of that model's timeout per slot."""
    p90 = statistics.quantiles(samples, n=10, method="inclusive")[-1]
    return int(math.ceil(p90 + (p90 - statistics.median(samples))))


_NVIDIA_TIMEOUT = {m: _derived_timeout(s) for m, s in _BAKEOFF_OK_SECONDS.items()}


class NvidiaTransportError(RuntimeError):
    """The NVIDIA call itself failed (timeout, HTTP error such as 404, no `choices`) — distinct from a reply that
    carries no verdict. Only when BOTH pair models fail this way is the endpoint treated as down (then Gro may run)."""


def _nv_label(model):
    return f"NVIDIA_{model}"


def _nv_call(model, prompt, key):
    """One NVIDIA chat call → the model's final `message.content` ('' when empty or missing). `reasoning_content` is
    never read: an APPROVE that exists only in hidden reasoning is not a verdict (design item 4)."""
    try:
        r = _curl(_NVIDIA_URL, [f"Authorization: Bearer {key}", "Content-Type: application/json"],
                  {"model": model, "temperature": 0.2, "max_tokens": _BAKEOFF_MAX_TOKENS, "messages": [
                      {"role": "system", "content": _NVIDIA_SYSTEM},
                      {"role": "user", "content": _framed(prompt)}]},
                  key, timeout=_NVIDIA_TIMEOUT.get(model, max(_NVIDIA_TIMEOUT.values())))
    except Exception as e:  # noqa: BLE001 — every transport failure becomes one typed, scrubbed error
        raise NvidiaTransportError(_scrub(f"{model}: {e}", key)[:300]) from None
    try:
        msg = r["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise NvidiaTransportError(_scrub(f"{model}: no choices in reply: {str(r)[:160]}", key)[:300]) from None
    content = msg.get("content") if isinstance(msg, dict) else None
    return content if isinstance(content, str) else ""


def _nv_model_verdict(model, prompt, key, reminder="", names_ok=None):
    """One pair member: (status, text, pre_counter), status APPROVE / REJECT / NO_VERDICT / TRANSPORT. A model with no
    verdict (empty content, no single VERDICT line, or a transport failure) is retried ONCE with the format reminder.
    TRANSPORT only when every attempt failed at transport. A REJECT on an undefined-name premise the static check
    contradicts gets one counter-prompt (as for every other reviewer); if that yields no verdict the REJECT stands.
    pre_counter = the original REJECT text when a counter-prompt landed a verdict (the caller logs that REJECT first,
    then the counter-prompt result as an auto_counter row of the same run), else None."""
    pre = None
    status, txt, reached = "TRANSPORT", "", False
    for attempt in range(2):
        try:
            txt = _nv_call(model, prompt if attempt == 0 else prompt + reminder, key)
        except NvidiaTransportError as e:
            txt = str(e)
            continue
        reached = True
        v = _verdict(txt) if txt.strip() else "INDETERMINATE"
        if v in ("APPROVE", "REJECT"):
            status = v
            break
    else:
        status = "NO_VERDICT" if reached else "TRANSPORT"
    if status == "REJECT" and _name_premise_reject(txt, names_ok):
        try:
            t2 = _nv_call(model, prompt + _name_counter(txt) + reminder, key)
            v2 = _verdict(t2) if t2.strip() else "INDETERMINATE"
            if v2 in ("APPROVE", "REJECT"):
                pre = txt
                status, txt = v2, t2
        except NvidiaTransportError as e:
            sys.stderr.write(f"[preship] {model} counter-prompt failed ({str(e)[:80]}) — its REJECT stands.\n")
    return status, txt, pre


def _nvidia_pair(prompt, key, reminder="", names_ok=None, models=_NVIDIA_PAIR):
    """Ask both pair models IN PARALLEL with the same prompt and combine (design item 3):
      any REJECT → REJECT; at least one APPROVE and no REJECT → APPROVE ('2-of-2' or '1-of-2');
      neither lands a verdict → verdict None (the caller fails closed, no marker).
    Returns {"verdict", "agreement", "models": [{"id", "verdict"}], "texts": {id: text}, "endpoint_down", "text"};
    endpoint_down is True only when every model failed at transport on every attempt. The model ids travel in this
    return value (no module global)."""
    def _one(m):
        try:
            return _nv_model_verdict(m, prompt, key, reminder, names_ok)
        except Exception as e:  # noqa: BLE001 — an unexpected error is no verdict (fail closed), never endpoint-down
            return "NO_VERDICT", _scrub(f"{m}: {type(e).__name__}: {e}", key)[:300], None
    with ThreadPoolExecutor(max_workers=len(models)) as ex:
        results = list(ex.map(_one, models))
    statuses = [r[0] for r in results]
    pre_counter = {m: r[2] for m, r in zip(models, results, strict=True) if r[2] is not None}
    if "REJECT" in statuses:
        verdict = "REJECT"
    elif "APPROVE" in statuses:
        verdict = "APPROVE"
    else:
        verdict = None
    n_ok = statuses.count("APPROVE")
    agreement = f"{n_ok}-of-{len(models)}" if verdict == "APPROVE" else ""
    pairs = list(zip(models, statuses, strict=True))
    texts = {m: r[1] for m, r in zip(models, results, strict=True)}
    shown = [m for m, s in pairs if s == "REJECT"] or list(models)
    text = "\n\n".join(f"[{m}: {s}]\n{(texts[m] or '')[-600:]}" for m, s in pairs if m in shown)
    return {"verdict": verdict, "agreement": agreement,
            "models": [{"id": m, "verdict": s} for m, s in pairs],
            "texts": texts, "pre_counter": pre_counter,
            "endpoint_down": all(s == "TRANSPORT" for s in statuses), "text": text}


def _nvidia(prompt, key):
    """COMPATIBILITY WRAPPER (the pre-NV-REVIEW-1110 `_nvidia(prompt, key) -> text` signature, kept for callers and test
    patch points). Runs the NVIDIA pair and returns its combined reply ending in exactly one VERDICT line; raises
    RuntimeError when neither model gave a verdict. audit_file uses _nvidia_pair directly (it needs both model ids and
    verdicts for the ledger and the marker)."""
    res = _nvidia_pair(prompt, key)
    if res["verdict"] is None:
        raise RuntimeError(f"NVIDIA pair gave no verdict: {res['models']}")
    body = "\n".join(ln for ln in res["text"].splitlines()
                     if not ln.strip().lstrip("*#-+>• ").strip().upper().startswith("VERDICT:"))
    tail = "APPROVE" if res["verdict"] == "APPROVE" else "REJECT — " + ", ".join(
        m["id"] for m in res["models"] if m["verdict"] == "REJECT")
    return f"{body}\nVERDICT: {tail}"


def _log_pair(relpath, sha, res, note, evidence, run_id=""):
    """One row per pair model. A model whose REJECT went through the name-premise counter-prompt gets its original
    REJECT row first, then the counter-prompt result as an auto_counter row of this run (it clears only that REJECT)."""
    pre = res.get("pre_counter") or {}
    for row in res["models"]:
        rid = row["id"]
        if rid in pre:
            _log_verdict(relpath, sha, _nv_label(rid), "REJECT", pre[rid], note=note, evidence=evidence, run_id=run_id)
            _log_verdict(relpath, sha, _nv_label(rid), row["verdict"], res["texts"].get(rid, ""),
                         note=note + "; automatic name-premise counter-prompt", evidence=evidence, run_id=run_id,
                         auto_counter=True)
        else:
            _log_verdict(relpath, sha, _nv_label(rid), row["verdict"], res["texts"].get(rid, ""),
                         note=note, evidence=evidence, run_id=run_id)


def _pair_refusal(relpath, sha, evidence, ignore=frozenset()):
    """'' when the NVIDIA pair may stand in for this exact staged content, else why not (design items 5 and 8):
    the file's cold-2nd PASS must be recorded and fresh for this content — the gate's own check
    (preship_gate._cold2_ok: same marker, same MARKER_MAX_AGE_SEC, PASS verdict, sha of the staged blob) — and no
    other reviewer may hold an open REJECT on it."""
    ok, why = _pg._cold2_ok(relpath, ":" + relpath)
    if not ok:
        return f"cold-2nd precondition not met ({why})"
    return _fallback_refusal(relpath, sha, {_nv_label(m) for m in _NVIDIA_PAIR}, evidence, ignore)


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


# STATIC-FACTS block (Rafael 2026-10-08, "why are there still so many false premise claims?"). Three
# Gro REJECTs in one session (2026-10-07/08: `e` undefined in broker.py, `pos` undefined in
# day_trade_manager.py, plus a config claim) were all on CHUNKED diffs: a chunk showed a USE of a name
# whose definition sat in another chunk or in unchanged code. The --context rule asks the author to
# pre-load such facts by hand, which cannot cover every function body — a request, not a control. The
# tool now RUNS the undefined-name check itself on the exact staged blob and states the result in every
# prompt (and every chunk), and an undefined-name REJECT contradicted by it gets one automatic counter-
# prompt carrying that evidence (the DISAGREEMENT PROTOCOL, mechanized — never a blind re-roll).
# Only the "name does not exist" class. NOT "possibly unbound / referenced before assignment on some path":
# ruff F821 proves a name is defined somewhere in scope, not that every path assigns it — those claims stay
# with the reviewer (never auto-countered).
_NAME_REJECT_RE = re.compile(
    r"nameerror|undefined (name|variable|identifier)|is not defined|not defined anywhere|is undefined|"
    r"never (defined|declared|imported)", re.IGNORECASE)
_PATH_CLAIM_RE = re.compile(r"unbound|before assignment|not assigned (on|in)|some (path|branch)", re.IGNORECASE)


def _static_facts(relpath, blob):
    """(facts_text, names_ok) for a staged .py blob: ruff F821/F822/F823 (undefined / undefined-export /
    used-before-assignment names) and py_compile, run on the EXACT staged bytes. names_ok is True only
    when the name check RAN and found nothing; ("", None) when not a .py file or the check could not run
    (no claim is made then). Never raises."""
    if not relpath.endswith(".py"):
        return "", None
    import shutil
    import tempfile
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="preship_static_")
        path = os.path.join(tmpdir, os.path.basename(relpath))
        with open(path, "wb") as f:
            f.write(blob)
        ruff = shutil.which("ruff")
        cmd = ([ruff] if ruff else [sys.executable, "-m", "ruff"]) + [
            "check", "--no-cache", "--isolated", "--select", "F821,F822,F823", "--output-format", "concise", path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        if r.returncode not in (0, 1) or ("All checks passed" not in out and "F82" not in out):
            return "", None   # ruff unavailable / crashed -> make no claim
        names_ok = r.returncode == 0
        c = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True, timeout=60)
        comp = "PASS" if c.returncode == 0 else "FAIL: " + (c.stderr.strip().splitlines() or ["?"])[-1][:200]
        names = ("PASS — every BARE name used in the file is defined/imported/a parameter somewhere in its "
                 "scope. NOT checked: attribute access (x.y), whether an imported member exists in its module, "
                 "and whether a name is assigned on every code path"
                 if names_ok else "FINDINGS:\n" + out.replace(path, relpath)[:1500])
        return (f"\n\n--- STATIC ANALYSIS (machine-run by this tool on the EXACT staged file {relpath}; "
                f"authoritative for bare-name resolution only) ---\n"
                f"ruff F821/F822/F823 undefined-name check: {names}\n"
                f"py_compile: {comp}\n"
                f"A bare variable/function/import name used in the changed lines may be DEFINED outside the "
                f"lines shown (earlier in the same function, in another chunk, or in unchanged code); an "
                f"undefined BARE name would appear above as a finding. Attribute and imported-member claims "
                f"are not covered by this check."), names_ok
    except Exception:  # noqa: BLE001 — a tooling hiccup makes no claim; the audit proceeds unchanged
        return "", None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# A claim about an ATTRIBUTE (x.y), an imported member, or a module is NOT covered by ruff F821 (bare names
# only) — such a reject is never auto-countered (cold-2nd 2026-10-08: `config.X is not defined` must stay a
# real REJECT).
_NOT_BARE_RE = re.compile(r"\b\w+\.\w+|\bimport|\battribute|\bmodule\b|\bmember\b", re.IGNORECASE)


def _reject_reason(text):
    """The reason on the single anchored `VERDICT: REJECT ...` line (same anchoring as _verdict), else ''.
    Only the stated reason is matched — never the rest of the reply (a reviewer echoing the static block or a
    NIT about a name must not trigger the counter-prompt)."""
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip().lstrip("*#-+>• ").strip().upper().startswith("VERDICT:")]
    if len(lines) != 1:
        return ""
    after = lines[0].strip().split(":", 1)[1].strip() if ":" in lines[0] else ""
    return after if after.upper().startswith("REJECT") else ""


def _name_premise_reject(text, names_ok):
    """True when the REJECT's stated reason is a BARE-name 'does not exist' claim that the static check
    contradicts (never an attribute/import/module claim, never a path-dependent unbound claim)."""
    r = _reject_reason(text)
    return (names_ok is True and bool(r) and bool(_NAME_REJECT_RE.search(r))
            and not _PATH_CLAIM_RE.search(r) and not _NOT_BARE_RE.search(r))


_NAME_COUNTER = ("\n\n--- AUTOMATIC COUNTER-PROMPT EVIDENCE (machine-generated) ---\nYour previous verdict "
                 "REJECTED on an undefined bare-name premise. The STATIC ANALYSIS block above was run on the "
                 "exact staged file and found NO undefined bare name (ruff F821/F822/F823 clean): that name is "
                 "defined outside the lines you were shown. Re-verdict on the changed lines; you may still "
                 "REJECT for a different concrete failing input (including a name left unassigned on a "
                 "specific path — quote that path).")


def _name_counter(reject_text):
    """_NAME_COUNTER plus the reviewer's own quoted reason, so any OTHER defect it named must be addressed."""
    return (_NAME_COUNTER + "\nYour previous reason, quoted: \"" + _reject_reason(reject_text)[:600] + "\"\n"
            "If that reason also named any OTHER defect, it still stands unless you show it is not a defect.")


def audit_file(relpath, waive_gro, keys, evidence="", context=""):
    """(ok, message). The message is scrubbed of every .env value before it leaves (NV-REVIEW-1110 item 6)."""
    ok, msg = _audit_file(relpath, waive_gro, keys, evidence, context)
    return ok, _redact(msg)


def _audit_file(relpath, waive_gro, keys, evidence="", context=""):
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
    _static_txt, _names_ok = _static_facts(relpath, blob)
    _ctx_suffix = _static_txt
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

    _ev = bool(evidence.strip())
    _run = uuid.uuid4().hex   # this audit run's id, stored on every ledger row it writes (see _open_rejects)
    nk = keys.get("NVIDIA_API_KEY", "")
    # GAI (required) — with the NVIDIA pair standing in on a genuine GAI outage (option C, Rafael 2026-08-24; the pair
    # replaced the llama ladder, NV-REVIEW-1110 item 2).
    gai_substituted = ""
    gai_pair = None
    # Codex reviews this run unless reviewer #2 is waived, and its own verdict then decides: a Codex REJECT (or Codex
    # being unavailable while its old REJECT is open) still blocks the ship in the reviewer-#2 step below. So Codex's
    # file-wide ledger entry for EARLIER content must not block the GAI-slot pair BEFORE Codex has re-reviewed the fixed
    # content. A Codex REJECT on THIS content is never exempt (a same-content Codex re-run without --evidence does not
    # clear it), so it still blocks the GAI-slot pair.
    _codex_on_this = any(rv == "codex" and s == sha for rv, s, _r in _open_reject_entries(relpath))
    _gai_ignore = set() if (waive_gro or _codex_on_this) else {"codex"}
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
        # GEMINI_ALLOW_PAID default-deny gate is explicitly opened by Rafael). Reached ONLY on a GAI *outage*
        # exception — never on a GAI *REJECT* (a reject returns a verdict, not an exception). No NVIDIA key, a missing
        # or stale cold-2nd, an open REJECT from another reviewer, or a pair with no verdict => fail-closed.
        _ge = _scrub(e)
        if not nk:
            return False, f"{relpath}: GAI audit failed ({_ge}) and no NVIDIA substitute key — fail-closed, no marker"
        _why = _pair_refusal(relpath, sha, evidence, _gai_ignore)
        if _why:
            return False, (f"{relpath}: GAI down ({_ge}) and the NVIDIA pair may not stand in: {_why} — "
                           f"fail-closed, no marker")
        sys.stderr.write(f"[preship] GAI down ({_ge}) — the NVIDIA pair stands in for GAI.\n")
        gai_pair = _nvidia_pair(prompt + _reminder, nk, _reminder, _names_ok)
        _log_pair(relpath, sha, gai_pair, "gai-slot substitute", _ev, _run)
        if gai_pair["verdict"] is None:
            return False, (f"{relpath}: GAI down ({_ge}) and neither NVIDIA pair model gave a verdict "
                           f"({gai_pair['models']}) — fail-closed, no marker")
        gai_v, gai_txt, gai_substituted = gai_pair["verdict"], gai_pair["text"], _PAIR_LABEL
    if gai_pair is None:
        # The verdict is logged BEFORE any counter-prompt, so a REJECT is on record even if the counter-prompt fails.
        _log_verdict(relpath, sha, "gai", gai_v, gai_txt, evidence=_ev, run_id=_run)
        if gai_v == "REJECT" and _name_premise_reject(gai_txt, _names_ok):
            sys.stderr.write("[preship] GAI rejected on an undefined-name premise the static check contradicts — "
                             "one automatic counter-prompt with that evidence.\n")
            try:
                gai_txt = _gai(prompt + _name_counter(gai_txt) + _reminder, keys.get("GEMINI_API_KEY", ""),
                               keys.get("GEMINI_PAID_API_KEY", ""))
                gai_v = _verdict(gai_txt)
            except Exception as _gce:
                return False, (f"{relpath}: GAI counter-prompt failed ({_scrub(_gce)}) after a name-premise REJECT "
                               f"— no marker")
            # its own row, marked auto_counter with this run's id: it clears ONLY the REJECT logged just above (the one
            # it answered), never an earlier run's REJECT; `evidence` stays the author's --evidence flag
            _log_verdict(relpath, sha, "gai", gai_v, gai_txt, note="automatic name-premise counter-prompt",
                         evidence=_ev, run_id=_run, auto_counter=True)
    elif gai_v == "APPROVE" and [r for r in _open_rejects(relpath, sha) if r not in _gai_ignore]:
        return False, (f"{relpath}: the NVIDIA pair approved for GAI, but an open REJECT on this content is not cleared "
                       f"({', '.join(r for r in _open_rejects(relpath, sha) if r not in _gai_ignore)}) — "
                       f"fail-closed, no marker")
    _gai_label = (f"substitute {_PAIR_LABEL} ({gai_pair['models']})" if gai_pair else "GAI")
    if gai_v == "INDETERMINATE":
        return False, (f"{relpath}: {_gai_label} INDETERMINATE — no parseable VERDICT line after a retry "
                       f"(NOT a content reject; re-run the audit). Last 300 chars:\n{gai_txt[-300:]}")
    if gai_v != "APPROVE":
        return False, f"{relpath}: {_gai_label} REJECT — no marker.\n{gai_txt[-600:]}"

    # Reviewer #2 (required unless waived): Codex first (BGC gate, REVIEWER-SWAP-1025). When Codex is unavailable — or
    # as the required SECOND reviewer on a Codex-signed file Codex approved — the NVIDIA pair reviews (NV-REVIEW-1110
    # item 1, every diff size); Gro only when the NVIDIA endpoint is down (both pair models transport-failed, or no
    # NVIDIA key) AND the diff fits one Gro request. The marker keeps the `gro` field name the ship gate reads;
    # `reviewer_b` records who gave it.
    gro_substituted = ""
    reviewer_b = "gro"
    b_pair = None
    signed = (not waive_gro) and _codex_signed(relpath)
    codex_v, codex_txt = (None, "") if waive_gro else _codex_review(prompt, _reminder, _names_ok, relpath)
    if codex_v is not None:
        _log_verdict(relpath, sha, "codex", codex_v, codex_txt, note="codex-signed file" if signed else "",
                     evidence=_ev, run_id=_run)
    if waive_gro:
        gro_v = "WAIVED"
        reviewer_b = "waived"
        _log_verdict(relpath, sha, "waived", "WAIVED", run_id=_run)
    elif codex_v == "INDETERMINATE":
        return False, (f"{relpath}: Codex INDETERMINATE — no parseable VERDICT line after a retry "
                       f"(NOT a content reject; re-run). Last 300 chars:\n{codex_txt[-300:]}")
    elif codex_v is not None and codex_v != "APPROVE":
        return False, f"{relpath}: Codex REJECT — no marker.\n{codex_txt[-600:]}"
    elif codex_v == "APPROVE" and not signed:
        gro_v, reviewer_b = "APPROVE", "codex"
    elif codex_v is None and signed:
        # A Codex-signed file (or unreadable history) needs Codex AND a second reviewer; with Codex unavailable it
        # cannot ship.
        return False, (f"{relpath}: Codex-signed file (or unreadable history) needs Codex AND a second reviewer (the "
                       f"NVIDIA pair; it needs Codex AND Gro only when NVIDIA is down), and Codex is "
                       f"unavailable ({codex_txt[:80]}) — no marker. Re-run when Codex is back.")
    else:
        _second = codex_v == "APPROVE"
        # On a Codex-signed file Codex has just approved, Codex is not being replaced, so its own ledger entry does not
        # block the second reviewer (a second reviewer is not a fallback for Codex).
        _ignore = {"codex"} if _second else set()
        _note = "second reviewer on a codex-signed file" if _second else f"codex unavailable: {codex_txt[:120]}"
        if _second:
            sys.stderr.write("[preship] Codex-signed file: Codex APPROVE recorded; a second reviewer must also approve.\n")
        else:
            sys.stderr.write(f"[preship] Codex unavailable ({codex_txt[:120]}) — the NVIDIA pair reviews as reviewer #2.\n")
        use_gro = not nk
        if nk:
            _why = _pair_refusal(relpath, sha, evidence, _ignore)
            if _why:
                return False, (f"{relpath}: NVIDIA pair fallback refused: {_why} — no marker.")
            b_pair = _nvidia_pair(prompt, nk, _reminder, _names_ok)
            _log_pair(relpath, sha, b_pair, _note, _ev, _run)
            if b_pair["endpoint_down"]:
                use_gro = True
                sys.stderr.write(f"[preship] NVIDIA endpoint down for both pair models ({b_pair['text'][:160]}) — "
                                 "Gro may review only if the diff fits one request.\n")
            elif b_pair["verdict"] is None:
                return False, (f"{relpath}: neither NVIDIA pair model gave a verdict ({b_pair['models']}) — "
                               f"fail-closed, no marker; re-run.")
            else:
                gro_v, gro_txt, gro_substituted = b_pair["verdict"], b_pair["text"], _PAIR_LABEL
        if use_gro:
            b_pair = None
            _why = _fallback_refusal(relpath, sha, {"gro"}, evidence, _ignore)
            if _why:
                return False, f"{relpath}: Gro fallback refused: {_why} — no marker."
            try:
                gro_txt = _gro(prompt, keys.get("GROQ_API_KEY", ""))
                gro_v = _verdict(gro_txt)
                if gro_v == "INDETERMINATE":
                    gro_txt = _gro(prompt + _reminder, keys.get("GROQ_API_KEY", ""))
                    gro_v = _verdict(gro_txt)
            except Exception as e:
                _em = _scrub(e)
                if _TOO_LARGE_RE.search(_em):
                    return False, (f"{relpath}: the NVIDIA pair is unreachable and this diff does not fit one Gro "
                                   f"request ({_em[:160]}) — no chunked review; fail-closed, no marker. Re-run when "
                                   "NVIDIA or Codex is back, or --waive-gro only if Rafael authorizes.")
                return False, (f"{relpath}: the NVIDIA pair is unreachable and Gro failed ({_em[:160]}) — "
                               "fail-closed, no marker. Re-run with --waive-gro only if Rafael authorizes.")
            # Logged BEFORE any counter-prompt, so a REJECT is on record even if the counter-prompt fails.
            _log_verdict(relpath, sha, "gro", gro_v, gro_txt, note=_note, evidence=_ev, run_id=_run)
            if gro_v == "REJECT" and _name_premise_reject(gro_txt, _names_ok):
                sys.stderr.write("[preship] Gro rejected on an undefined-name premise the static check contradicts — "
                                 "one automatic counter-prompt with that evidence.\n")
                try:
                    gro_txt = _gro(prompt + _name_counter(gro_txt) + _reminder, keys.get("GROQ_API_KEY", ""))
                    gro_v = _verdict(gro_txt)
                except Exception as _xe:
                    return False, (f"{relpath}: Gro counter-prompt failed ({_scrub(_xe)}) after a name-premise "
                                   "REJECT — no marker; re-run (or --evidence).")
                # its own row, auto_counter with this run's id: clears ONLY the REJECT logged just above in this run
                _log_verdict(relpath, sha, "gro", gro_v, gro_txt, note=_note + "; automatic name-premise counter-prompt",
                             evidence=_ev, run_id=_run, auto_counter=True)
        _gro_lbl = f"{_PAIR_LABEL} ({b_pair['models']})" if b_pair else "Gro"
        if _second:
            reviewer_b = "codex+" + (gro_substituted or "gro")
        if gro_v == "INDETERMINATE":
            return False, (f"{relpath}: {_gro_lbl} INDETERMINATE — no parseable VERDICT line after a "
                           f"retry (NOT a content reject; re-run). Last 300 chars:\n{gro_txt[-300:]}")
        if gro_v != "APPROVE":
            return False, f"{relpath}: {_gro_lbl} REJECT — no marker.\n{gro_txt[-600:]}"
        _left = [r for r in _open_rejects(relpath, sha) if r not in _ignore]
        if _left:
            return False, (f"{relpath}: {_gro_lbl} approved, but an open REJECT on this content is not cleared "
                           f"({', '.join(_left)}) — fail-closed, no marker")
    # FINAL NO-EXEMPTION RE-CHECK (NV-REVIEW-1110 rev 4, adversarial claims check 2026-11-10): whenever a fallback (the
    # NVIDIA pair in either slot, or Gro) filled a slot this run, re-read the ledger with NO exemptions and refuse the
    # marker while any REJECT is still open on this content. A Codex APPROVE of this content in this run has already
    # cleared Codex's entries on earlier content (a later-sha APPROVE clears them), so a legitimate run is not blocked.
    if gai_substituted or reviewer_b not in ("codex", "waived"):
        _still = _open_rejects(relpath, sha)
        if _still:
            return False, (f"{relpath}: a fallback reviewer filled a slot, but an open REJECT on this content is not "
                           f"cleared ({', '.join(_still)}) — fail-closed, no marker")
    # DIVERSITY NOTE: if the pair filled BOTH slots, the 2-voice review is the same two models twice — still better
    # than blocking, recorded in the marker, and surfaced (Rafael-visible on stderr).
    if gro_substituted and gai_substituted:
        sys.stderr.write(f"[preship] NOTE: the {_PAIR_LABEL} filled both the GAI and the reviewer-#2 slot — degraded "
                         "2-voice (same models twice); re-run when GAI or Codex recovers for an independent voice.\n")

    os.makedirs(MARKER_DIR, exist_ok=True)
    # marker["gai"] MUST stay the literal "APPROVE": the ship gate (preship_gate._marker_ok)
    # exact-matches `gai == "APPROVE"` — a decorated value like "APPROVE (SUBST:...)" fails that
    # check and BLOCKS the very ship the substitute just approved (cold-2nd 2026-08-24, self-
    # defeating fail-closed bug). The substitution is recorded ONLY in the `gai_substituted`
    # key (which the gate ignores) — that fully satisfies auditability without touching `gai`.
    if gro_substituted and not reviewer_b.startswith("codex+"):
        reviewer_b = gro_substituted
    # `gro` = reviewer #2's verdict (field name kept: preship_gate._marker_ok reads it); `reviewer_b` = who gave it.
    # _marker_ok reads only ts / gro / gai / sha256, never reviewer_b, so audit-trail labels such as "nvidia-pair" or
    # "codex+nvidia-pair" cannot change whether the gate opens.
    marker = {"sha256": sha, "gro": gro_v, "gai": "APPROVE", "ts": int(time.time()),
              "file": relpath, "reviewer_b": reviewer_b}
    if gai_pair:
        # audit trail (design item 7): GAI was down; the pair stood in — both model ids, both verdicts, 2-of-2 / 1-of-2
        marker["gai_substituted"] = _PAIR_LABEL
        marker["gai_models"] = gai_pair["models"]
        marker["gai_agreement"] = gai_pair["agreement"]
    if b_pair:
        marker["gro_substituted"] = _PAIR_LABEL
        marker["reviewer_b_models"] = b_pair["models"]
        marker["reviewer_b_agreement"] = b_pair["agreement"]
    with open(os.path.join(MARKER_DIR, relpath.replace("/", "__") + ".json"), "w") as f:
        json.dump(marker, f, indent=2)
    _gai_disp = f"APPROVE (SUBST:{_PAIR_LABEL} {gai_pair['agreement']})" if gai_pair else "APPROVE"
    _gro_disp = f"{gro_v} ({b_pair['agreement']})" if b_pair else gro_v
    if reviewer_b == "codex":
        _b_name = "codex"
    elif reviewer_b.startswith("codex+"):
        _b_name = "codex=APPROVE " + reviewer_b.split("+", 1)[1]
    else:
        _b_name = reviewer_b if reviewer_b in (_PAIR_LABEL, "waived") else "gro"
    return True, (f"{relpath}: APPROVED ({_b_name}={_gro_disp} gai={_gai_disp}) — marker "
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
