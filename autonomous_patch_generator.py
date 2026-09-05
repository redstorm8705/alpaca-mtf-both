#!/usr/bin/env python3
# ruff: noqa: E501  — long LLM-prompt strings + rationale comments are intentionally long (matches the sibling audit scripts)
"""autonomous_patch_generator.py — Stage 1.5 of the autonomous patch pipeline.

Runs on OCI at 6 PM ET (23:00 UTC) weeknights, between:
  - Stage 1 (1:35 PM PT): auto_ai_audit.py → logs/audit_directives.jsonl
  - Stage 2 (11 PM ET):   autonomous_review.py → logs/pending_approvals_*.md

For each unprocessed directive in audit_directives.jsonl:
  1. Full read of target file
  2. 3-agent board vote (Strict Parser / Red Teamer / Quant Risk) via LLM (Gemini→Groq)
  3. Diff generation via LLM (Gemini→Groq)
  4. Static analysis (py_compile + mypy + ruff) on target file
  5. Cold second-agent logic review via LLM (Gemini→Groq)
  6. Write pending_ds_gai_*.json + .patch file (for autonomous_review.py to consume)
  7. Mark directive as processed (atomic write)
  8. Slack summary

AWP audit fix (2026-06-30): migrated DeepSeek -> Gro (Groq). DeepSeek's
account had gone unfunded (every call returned 402 Payment Required),
silently producing zero processed directives every night for an unknown
number of sessions before this was caught. The live interactive Claude
pipeline migrated to Gro/Groq on 2026-06-27 (commit 6457394) but this
standalone OCI cron script was never updated to match -- confirmed via the
nightly cron log showing dozens of consecutive 402 errors with "0
processed, 17 left for retry" every run.

Author: autonomous pipeline (S56)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gai_client import call_gai  # single source of truth for the live Gemini model ladder

# ── Config ────────────────────────────────────────────────────────────────────
_REPO_DIR     = Path("/home/ubuntu/mtf-bot")
_LOGS_DIR     = _REPO_DIR / "logs"
_LOCKFILE     = "/tmp/mtf_autonomous_patch_generator.lock"
_MAX_RETRIES  = 3
_API_TIMEOUT  = 180
# 2026-08-05 hardening (Rafael-directed audit): _board_vote / _generate_diff /
# _build_ds_gai_prompt used to silently slice file_content to [:6000]/[:8000]
# chars before ever asking an LLM to validate a finding or write a diff. On
# strategy/run_cycle.py (116,586 chars / 2,064 lines) that is under 7% of the
# file — the model never saw the real control flow and fabricated a plausible-
# sounding but nonexistent `for signal in signals:` loop and variable names,
# one third of which targeted a gate (PDT limit) the project deliberately
# deleted months earlier. This violates the same Full-Read-Gate principle the
# project already enforces for human/AI-assisted patches, just automated.
# _MAX_FILE_CHARS_FOR_LLM covers every file in this repo as of 2026-08-05 (the
# largest, scan_to_html.py, is ~148K chars) with headroom; a file that ever
# exceeds it is a hard, LOUD skip (never a silent partial read) — see
# _process_directive's use of this constant.
_MAX_FILE_CHARS_FOR_LLM = 300_000
# AWP audit fix (2026-06-30): migrated DeepSeek -> Gro (Groq). See module
# docstring for why. Matches CLAUDE.md's Gro/GAI DIRECT API PROTOCOL.
_GRO_BASE_URL = "https://api.groq.com/openai/v1"
_GRO_MODEL    = "openai/gpt-oss-120b"
# AWP audit fix (2026-07-01): Groq free-tier TPD (100K tokens/day) is exhausted
# after ~3 directives (5 LLM calls each), leaving the rest 429'd → 0 processed.
# Route drafting through Gemini (no TPD wall in our usage); keep Groq as fallback.
# The independent DS/GAI cross-check remains a separate stage (autonomous_review.py),
# so model-diversity is preserved where it matters. Rafael approved 2026-07-01.
# GAI model + endpoint now come from gai_client.call_gai (single source of truth: the
# GAI_MODEL_LADDER + thinking_budget=0) — no per-file model pin can go stale here again.
_SLACK_URL    = None  # loaded from .env

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# AWP audit fix (2026-06-30): RTH Block removed (Rafael mandate). This script
# writes only its own dedicated pipeline files (audit_directives.jsonl,
# pending_ds_gai_*.json, .patch files) -- no write-contention risk with the
# live bot's shared state. It also never applies patches itself (only writes
# them for autonomous_review.py / a human to act on), matching CLAUDE.md's
# "scheduled sessions never apply patches" rule.

# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    ts = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S PT")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    log_path = _LOGS_DIR / "autonomous_patch_generator.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        # RC-3 fix (S58): non-fatal but never silent — stdout remains primary log
        print(f"[autonomous_patch_generator] log file write failed: {exc}",
              file=sys.stderr)

# ── Environment ───────────────────────────────────────────────────────────────
def _load_env() -> None:
    global _SLACK_URL
    env_path = _REPO_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    # S58: .env defines SLACK_WEBHOOK_URL (not SLACK_WEBHOOK) — accept both
    _SLACK_URL = (
        os.environ.get("SLACK_WEBHOOK", "")
        or os.environ.get("SLACK_WEBHOOK_URL", "")
    )

# ── Slack ─────────────────────────────────────────────────────────────────────
def _slack(msg: str) -> None:
    if not _SLACK_URL:
        _log(f"Slack (no webhook): {msg}")
        return
    import urllib.request  # noqa: PLC0415
    try:
        req = urllib.request.Request(
            _SLACK_URL,
            data=json.dumps({"text": msg}).encode(),
            headers={"Content-type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        _log(f"Slack delivery failed: {exc}")

# ── Atomic write (RC-5) ───────────────────────────────────────────────────────
def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

# ── Gro (Groq) API ────────────────────────────────────────────────────────────
def _call_gro(prompt: str) -> str | None:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        _log("ERROR: GROQ_API_KEY not set")
        return None
    import requests  # type: ignore[import-untyped]
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                f"{_GRO_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _GRO_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            _log(f"Gro attempt {attempt+1}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 5)
    return None

# ── Gemini API (primary drafting model — no TPD wall) ─────────────────────────
def _call_gai(prompt: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        _log("ERROR: GEMINI_API_KEY not set")
        return None
    # Shared laddered client (single source: gai_client.GAI_MODEL_LADDER + thinking_budget=0); a
    # churned/retired model auto-skips to the next live one. Outer retry handles a transient
    # whole-ladder failure. Returns None on total failure (caller falls back to Groq).
    for attempt in range(_MAX_RETRIES):
        try:
            return call_gai(prompt, api_key, max_output_tokens=4096, timeout=_API_TIMEOUT)
        except Exception as exc:
            _log(f"Gai attempt {attempt+1}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 5)
    return None

# ── LLM router — Gemini primary, Groq fallback ────────────────────────────────
def _call_llm(prompt: str) -> str | None:
    """Drafting calls route to Gemini (no daily-token wall); fall back to Groq
    only if Gemini is unavailable. Returns None if both fail (caller → retry)."""
    out = _call_gai(prompt)
    if out is not None:
        return out
    _log("Gemini unavailable — falling back to Groq")
    return _call_gro(prompt)

# ── Board vote (3 cold independent agents via LLM: Gemini→Groq) ────────────────
def _board_vote(
    file_content: str,
    finding: str,
    recommended_fix: str,
    rc_class: str,
) -> dict:
    """Run 3 independent board agents. Returns dict with verdicts + notes."""

    # 2026-08-05: full file content, never a silent slice — see
    # _MAX_FILE_CHARS_FOR_LLM. A board vote formed on a partial view of the
    # file cannot actually check "does this cover all edge cases" or "does
    # this match real code" — the exact gap that let a fabricated finding
    # about strategy/run_cycle.py reach REJECT-worthy review at all.
    base_context = (
        f"RC class: {rc_class}\n"
        f"Finding: {finding}\n"
        f"Recommended fix: {recommended_fix}\n\n"
        f"Full file content ({len(file_content)} chars):\n"
        f"{file_content}"
    )

    intro_a = (
        "You are a Strict Parser code reviewer. Analyze this finding and"
        " proposed fix for correctness. Check: (1) logic inversion — does"
        " the fix do the opposite of intended? (2) off-by-one/boundary"
        " errors. (3) missing conditions — does it cover all edge cases?"
        " Return APPROVE or REJECT as the first word, then explain."
    )
    intro_b = (
        "You are a Red Teamer code reviewer. Actively try to find ways the"
        " proposed fix could break things, introduce regressions, or miss"
        " edge cases. Assume the fix is wrong until proven otherwise."
        " Return APPROVE or REJECT as the first word, then explain your"
        " attack surface analysis."
    )
    intro_c = (
        "You are a Quant Risk reviewer at a systematic hedge fund. Assess"
        " operational risk of this fix — P&L impact, execution safety,"
        " counter-state invariants. Focus on what could go wrong in"
        " production. Return APPROVE or REJECT as the first word, then"
        " list risks by priority (P0/P1/P2)."
    )
    prompts = {
        "A_strict_parser": f"{intro_a}\n\n{base_context}",
        "B_red_teamer":    f"{intro_b}\n\n{base_context}",
        "C_quant_risk":    f"{intro_c}\n\n{base_context}",
    }

    results: dict = {}
    for agent_key, prompt in prompts.items():
        _log(f"Board vote: calling agent {agent_key}...")
        response = _call_llm(prompt)
        if response is None:
            results[agent_key] = "UNCLEAR"
            results[f"{agent_key}_notes"] = "API call failed"
            continue
        verdict = (
            "APPROVE" if response.upper().startswith("APPROVE")
            else "REJECT" if response.upper().startswith("REJECT")
            else "UNCLEAR"
        )
        results[agent_key] = verdict
        results[f"{agent_key}_notes"] = response[:500]

    return {
        "A_strict_parser": results.get("A_strict_parser", "UNCLEAR"),
        "A_notes": results.get("A_strict_parser_notes", ""),
        "B_red_teamer": results.get("B_red_teamer", "UNCLEAR"),
        "B_notes": results.get("B_red_teamer_notes", ""),
        "C_quant_risk": results.get("C_quant_risk", "UNCLEAR"),
        "C_notes": results.get("C_quant_risk_notes", ""),
    }

# ── Diff generation via LLM (Gemini-primary, Groq fallback) ──────────────────
def _generate_diff(
    file_path: Path,
    file_content: str,
    finding: str,
    recommended_fix: str,
) -> str | None:
    # 2026-08-05: full file content — see _MAX_FILE_CHARS_FOR_LLM. This is the
    # single most consequential of the three sites: a diff generated from a
    # truncated view can only ever reference what it can see, so anything
    # past the old 8000-char cutoff was invisible and got filled in with
    # invented-but-plausible-sounding names. The "must apply cleanly" line
    # below is now backed by an actual git apply --check in _process_directive
    # (2026-08-05) instead of being an unverified instruction to the model.
    prompt = (
        "You are a senior engineer generating a minimal unified diff patch.\n"
        "Return ONLY the unified diff — no explanation, no markdown fences.\n"
        "The diff must apply cleanly with: git apply <patch>\n\n"
        f"File: {file_path}\n"
        f"Finding: {finding}\n"
        f"Recommended fix: {recommended_fix}\n\n"
        f"Full file content ({len(file_content)} chars):\n{file_content}"
    )
    # 2026-08-05 board catch: this is the highest-token-volume call in the whole
    # pipeline (up to _MAX_FILE_CHARS_FOR_LLM=300_000 chars, ~75-100K tokens) —
    # it MUST route through _call_llm (Gemini-primary, ~1M-token context) rather
    # than call Groq directly. Groq's llama-3.3-70b-versatile has a 128K-token
    # window and — per this file's own 2026-07-01 fix note above — a 100K-
    # token/DAY free-tier budget that a single large-file call could now exhaust
    # outright, reintroducing that exact "0 processed" failure for a new reason.
    _log("Generating diff via LLM (Gemini-primary)...")
    response = _call_llm(prompt)
    if response is None:
        return None
    # Strip any accidental markdown fences
    lines = response.strip().splitlines()
    clean: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            clean.append(line)
    return "\n".join(clean)

# ── Mechanical apply-check (2026-08-05) ───────────────────────────────────────
# WHY THIS EXISTS: nothing in this pipeline ever verified a generated diff
# actually applies to the real file — the prompt just TELLS the model "the
# diff must apply cleanly" and hopes. A fabricated diff (referencing code
# that doesn't exist) is definitionally invalid and this is a free, instant,
# zero-LLM-cost way to catch that BEFORE spending two API calls reviewing it
# and writing it to the approval queue. This is the same "mechanism beats
# documentation" principle the project already applies everywhere else.
def _diff_applies_cleanly(diff: str) -> tuple[bool, str]:
    """Return (applies, error_detail). Runs `git apply --check` against the
    real repo state — never mutates anything (--check only validates).
    NEVER RAISES: any unexpected error is treated as "does not apply"
    (fail-closed — a diff we can't verify is not a diff we should ship)."""
    import tempfile
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".patch", dir=_LOGS_DIR, delete=False, encoding="utf-8",
        ) as f:
            # 2026-08-05 board catch: tmp_path MUST be captured before the write,
            # not after — `diff` is raw, unsanitized LLM output that could contain
            # malformed Unicode; if f.write() itself raises (e.g. a lone surrogate
            # under this utf-8 writer), the file already exists on disk via
            # delete=False, but the OLD assignment order left tmp_path as None,
            # so the finally block's cleanup never ran. Reproduced empirically.
            tmp_path = f.name
            f.write(diff)
        result = subprocess.run(
            ["git", "apply", "--check", tmp_path],
            cwd=_REPO_DIR, capture_output=True, text=True, timeout=30,
        )
        return (result.returncode == 0, result.stderr.strip())
    except Exception as exc:  # RC-3: logged by caller, never silent
        return (False, f"apply-check errored: {exc}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── Static analysis on target file ───────────────────────────────────────────
def _run_static_analysis(file_path: Path) -> dict:
    results: dict = {}

    # py_compile
    r = subprocess.run(
        [sys.executable, "-m", "py_compile", str(file_path)],
        capture_output=True, text=True,
    )
    results["py_compile"] = "PASS" if r.returncode == 0 else f"FAIL: {r.stderr.strip()}"

    # mypy
    r = subprocess.run(
        [sys.executable, "-m", "mypy", "--warn-unreachable", str(file_path)],
        capture_output=True, text=True,
    )
    results["mypy"] = (
        "PASS" if r.returncode == 0
        else f"FAIL: {r.stdout.strip()[:300]}"
    )

    # ruff
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "E,W,F,B", str(file_path)],
        capture_output=True, text=True,
    )
    results["ruff"] = "PASS" if r.returncode == 0 else f"FAIL: {r.stdout.strip()[:300]}"

    return results

# ── Cold second-agent logic review ────────────────────────────────────────────
def _second_agent_review(diff: str, finding: str, recommended_fix: str) -> str:
    prompt = (
        "You are a cold second-agent logic reviewer. You have no prior context.\n"
        "Original intent: fix the finding described below.\n\n"
        f"Finding: {finding}\n"
        f"Recommended fix: {recommended_fix}\n\n"
        f"Diff to review:\n{diff}\n\n"
        "Check explicitly:\n"
        "1. Logic inversion — does any condition check the OPPOSITE of intended?\n"
        "2. Off-by-one / boundary errors — incorrect guards (> vs >=, < vs <=)?\n"
        "3. Missing conditions — does the fix cover all edge cases stated?\n"
        "4. Branch completeness — both TRUE and FALSE paths verified?\n\n"
        "Return PASS or FAIL as the first word, then a brief threat list."
    )
    # 2026-08-05: route through _call_llm for the same reason as _generate_diff —
    # consistency with the rest of the pipeline's Gemini-primary routing, even
    # though this payload (diff + finding only) is smaller and lower-risk.
    _log("Running cold second-agent review (Gemini-primary)...")
    response = _call_llm(prompt)
    if response is None:
        return "UNCLEAR: API call failed"
    return response[:200]

# ── DS/GAI prompt builder ─────────────────────────────────────────────────────
def _build_ds_gai_prompt(
    file_path: Path,
    file_content: str,
    finding: str,
    recommended_fix: str,
    rc_class: str,
    diff: str,
) -> str:
    # 2026-08-05: full file content — see _MAX_FILE_CHARS_FOR_LLM. Gro/GAI are
    # this pipeline's LAST gate before a diff reaches Rafael's approval queue;
    # a truncated view here means the final reviewers are exactly as blind as
    # the drafting step, which is how a diff referencing nonexistent variables
    # and a deleted PDT gate got all the way to a rejected-but-still-queued
    # finding instead of being caught as fabricated at the source.
    return (
        f"AUDIT REQUEST — {file_path.name} | {rc_class}\n\n"
        f"FINDING: {finding}\n"
        f"RECOMMENDED FIX: {recommended_fix}\n\n"
        f"PROPOSED DIFF:\n{diff}\n\n"
        f"FULL FILE CONTENT ({len(file_content)} chars):\n{file_content}\n\n"
        "AUDIT SCOPE — check all 8 RC classes:\n"
        "RC-1: Naive datetime (tz-unaware)\n"
        "RC-2: CWD-relative path (not anchored to __file__)\n"
        "RC-3: Silent exception (bare except pass)\n"
        "RC-4: Estimated exit price (non-fill price to record_exit)\n"
        "RC-5: Non-atomic write (no tmp→replace pattern)\n"
        "RC-6: Wrong API field name (Alpaca field assumed not confirmed)\n"
        "RC-7: Zero-share sizing (int truncation before floor guard)\n"
        "RC-8: Unbounded scan buffer (confirm_gate not cleared on block)\n\n"
        "Return APPROVE or REJECT as the first word of your response.\n"
        "Then provide your full audit findings."
    )

# ── Risk-path scope gate (Rafael 2026-09-04) ──────────────────────────────────
# The board correctly REJECTED every risk-path directive (execution/strategy files) — a red-teamer
# + risk reviewer will almost always decline a recommended fix to a 2000-line execution file, and
# should. So autonomous auto-patching is now RESCOPED to NON-risk-path files only; everything else
# is routed to human review (never auto-patched). DEFAULT-DENY / fail-safe: a file that does not
# match this narrow allowlist is treated as risk-path and skipped. Allowlist = files that CANNOT
# affect RTH execution / sizing / orders (docs + tests). Widen deliberately, never by default.
def _is_non_risk_path(file_rel: str) -> bool:
    p = file_rel.replace("\\", "/").lower().lstrip("./")
    parts = [seg for seg in p.split("/") if seg]
    # 1. A documentation / design-record / audit-log text file, anywhere in the tree.
    if p.endswith((".md", ".rst", ".txt")):
        return True
    # 2. The test suite — a genuine "tests" path SEGMENT, not the substring "tests"
    #    inside another name (e.g. "tests_helper.py" is NOT a test dir → risk-path).
    if "tests" in parts:
        return True
    # NB: a bare "docs/" directory rule was REMOVED (cold-2nd 2026-09-04): docs/ held a
    # launchd .plist that controls bot launch — an execution-affecting artifact the pipeline
    # must never auto-patch. Real doc targets are already covered by the .md/.rst/.txt rule
    # above; anything else under docs/ is default-denied to human review, as it should be.
    return False                                  # everything else → risk-path → human review


# ── Process one directive ─────────────────────────────────────────────────────
# Status outcomes (S58 state machine + 2026-09-04 rescope):
#   "processed"         — pipeline succeeded, pending_ds_gai JSON + patch written
#   "skipped_risk_path" — target is a risk-path file; routed to human, never auto-patched (NORMAL)
#   "board_rejected"    — the board declined the finding/fix (NORMAL — the gate working)
#   "failed_permanent"  — structural failure (missing file, bad directive, non-diff output)
#   "retry"             — transient failure (API down); status left pending_review
def _process_directive(directive: dict) -> str:
    """Process one directive → 'processed'|'skipped_risk_path'|'board_rejected'|'failed_permanent'|'retry'."""
    file_rel  = directive.get("file", "")
    finding   = directive.get("finding", "")
    rec_fix   = directive.get("recommended_fix", "")
    rc_class  = directive.get("rc_class", "RC-?")

    if not file_rel or not finding:
        _log(f"SKIP (permanent): directive missing file or finding: {directive}")
        return "failed_permanent"

    # 2026-08-05 board catch (masked-loss seat): file_rel comes from an upstream
    # finding (ultimately an LLM audit output) with NO containment check before
    # this diff — a hallucinated or malformed directive naming ".env" or an
    # absolute path (Path envelopes an absolute RHS, discarding _REPO_DIR
    # entirely — confirmed: Path("/x") / "/etc/passwd" == Path("/etc/passwd"))
    # would have its full content read and sent to Groq/Gemini. Pre-existing in
    # both the old and new version, but THIS diff removes the [:6000]/[:8000]
    # caps that used to bound how much of such a file could leak per call —
    # closing it here rather than shipping a widened version of a known gap.
    file_path = (_REPO_DIR / file_rel).resolve()
    if not file_path.is_relative_to(_REPO_DIR.resolve()):
        _log(f"SKIP (permanent): {file_rel!r} resolves outside the "
             "repo — refusing (path containment).")
        return "failed_permanent"
    if any(part.startswith(".") for part in Path(file_rel).parts):
        _log(f"SKIP (permanent): {file_rel!r} is a dotfile/dotdir "
             "path — refusing (secrets-exposure guard).")
        return "failed_permanent"
    if not file_path.exists():
        _log(f"SKIP (permanent): target file not found: {file_path}")
        return "failed_permanent"

    # ── Risk-path scope gate (2026-09-04): auto-patch NON-risk-path files only ──
    if not _is_non_risk_path(file_rel):
        _log(f"SKIP (risk-path): {file_rel} is a risk-path file — autonomous auto-patching is "
             "restricted to non-risk-path files (docs/tests); routed to human review, not auto-patched.")
        return "skipped_risk_path"

    _log(f"Processing: {file_rel} | {rc_class} | {finding[:60]}...")

    file_content = file_path.read_text(encoding="utf-8")

    # 2026-08-05: fail LOUD and PERMANENT rather than silently truncate. A file
    # this large getting a partial view is exactly the prior failure mode —
    # better to skip it visibly (and let a human decide) than feed the LLM a
    # fragment and let it fill the gap with fabricated code.
    if len(file_content) > _MAX_FILE_CHARS_FOR_LLM:
        _log(
            f"SKIP (permanent): {file_rel} is {len(file_content)} chars, exceeds "
            f"_MAX_FILE_CHARS_FOR_LLM={_MAX_FILE_CHARS_FOR_LLM} — full-context "
            "review not possible, refusing rather than truncate. Human review "
            "required if this file needs a patch."
        )
        return "failed_permanent"

    # ── Board vote ────────────────────────────────────────────────────────────
    _log("Running board vote...")
    board = _board_vote(file_content, finding, rec_fix, rc_class)
    board_verdicts = [
        board["A_strict_parser"],
        board["B_red_teamer"],
        board["C_quant_risk"],
    ]
    if board_verdicts.count("UNCLEAR") == 3:
        # all 3 agents UNCLEAR = API failure (each agent returns UNCLEAR on
        # _call_gro None) — transient, retry tomorrow
        _log(f"Board vote inconclusive (3x UNCLEAR / API down) on {file_rel} — retry")
        return "retry"
    reject_count = board_verdicts.count("REJECT")
    if reject_count >= 2:
        _log(f"Board declined {file_rel}: {board_verdicts} — no auto-patch (normal; gate working)")
        return "board_rejected"

    # ── Diff generation ───────────────────────────────────────────────────────
    diff = _generate_diff(file_path, file_content, finding, rec_fix)
    if diff is None:
        _log(f"Diff generation API failure on {file_rel} — will retry")
        return "retry"
    if not diff.strip().startswith("---"):
        # AWP fix (2026-07-01): model format variance (prose instead of a diff) is
        # TRANSIENT, not a structural failure — retry next run rather than burning
        # the directive permanently. Groq's llama-3.3 frequently returned prose;
        # Gemini produces cleaner diffs, so this path should now rarely fire.
        _log(f"{file_rel}: model returned non-diff output — retry")
        return "retry"

    # ── Mechanical apply-check (2026-08-05) ───────────────────────────────────
    # A diff that looks like a diff (starts with "---") but doesn't actually
    # apply is exactly what a fabricated patch produces — the format-only check
    # above would have waved this straight through to two paid LLM review calls
    # and the approval queue. This is free and instant; run it first.
    applies, apply_err = _diff_applies_cleanly(diff)
    if not applies:
        _log(
            f"{file_rel}: generated diff does NOT apply cleanly to the real "
            f"file — {apply_err[:300]} — retry (now that the full file is "
            "sent, a fresh attempt should be properly grounded)"
        )
        return "retry"

    # ── Static analysis on target file ───────────────────────────────────────
    static = _run_static_analysis(file_path)
    static_pass = all(v == "PASS" for v in static.values())
    if not static_pass:
        _log(f"Static analysis failures on {file_rel}: {static}")
        # do not skip — write the JSON so the user can decide; flag it

    # ── Cold second-agent review ──────────────────────────────────────────────
    second_agent_raw = _second_agent_review(diff, finding, rec_fix)
    second_agent = (
        "PASS" if second_agent_raw.upper().startswith("PASS")
        else "FAIL" if second_agent_raw.upper().startswith("FAIL")
        else "UNCLEAR"
    )

    # ── SHA256 + base commit ──────────────────────────────────────────────────
    sha256 = hashlib.sha256(file_content.encode()).hexdigest()
    git_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPO_DIR, capture_output=True, text=True,
    )
    base_commit = git_result.stdout.strip() if git_result.returncode == 0 else "unknown"

    # ── DS/GAI prompt ─────────────────────────────────────────────────────────
    ds_gai_prompt = _build_ds_gai_prompt(
        file_path, file_content, finding, rec_fix, rc_class, diff,
    )

    # ── Write .patch file ─────────────────────────────────────────────────────
    date_str = datetime.now(PT).strftime("%Y-%m-%d")
    # Sanitize to a filesystem-safe basename. rc_class can be a raw category
    # string like "**ALPHA ISSUE/LOW" or "CATEGORY: EXECUTION BUG/MEDIUM" — the
    # embedded '/' was silently treated as a subdirectory, crashing _write_atomic
    # with FileNotFoundError and killing the entire run (2026-07-02 fix). Collapse
    # every char outside [a-z0-9._] to '_'.
    _raw_name = (
        rc_class.lower().replace("-", "")
        + "_"
        + file_path.stem.replace("_", "")[:20]
    )
    safe_name = re.sub(r"[^a-z0-9._]+", "_", _raw_name).strip("_")[:60] or "patch"
    patch_rel  = f"logs/pending_patch_{date_str}_{safe_name}.patch"
    patch_path = _REPO_DIR / patch_rel
    _write_atomic(patch_path, diff)
    _log(f"Wrote patch: {patch_path.name}")

    # ── Write pending_ds_gai JSON ─────────────────────────────────────────────
    json_path = _LOGS_DIR / f"pending_ds_gai_{date_str}_{safe_name}.json"
    payload = {
        "file": file_rel,
        "finding": finding,
        "rc_class": rc_class,
        "ds_gai_prompt": ds_gai_prompt,
        "status": "awaiting_ds_gai",
        "board": board,
        "static_analysis": static,
        "second_agent": second_agent,
        "sha256_at_draft": sha256,
        "base_commit_sha": base_commit,
        "patch_file": patch_rel,
        "created_at": datetime.now(PT).strftime("%Y-%m-%d %H:%M PT"),
    }
    _write_atomic(json_path, json.dumps(payload, indent=2))
    _log(f"Wrote pending JSON: {json_path.name}")

    return "processed"

# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # ── Lockfile ──────────────────────────────────────────────────────────────
    lock_fd = open(_LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("SKIP: Another autonomous_patch_generator.py is already running")
        sys.exit(0)

    _log("=== autonomous_patch_generator.py starting ===")
    _load_env()

    # ── Read audit_directives.jsonl ───────────────────────────────────────────
    directives_path = _LOGS_DIR / "audit_directives.jsonl"
    if not directives_path.exists():
        _log("No audit_directives.jsonl found. Exiting.")
        sys.exit(0)

    # S58: only actionable directives — status pending_review with file+finding.
    # context_only (weekly compliance blobs) and terminal states are never queued.
    directives: list[dict] = []
    for line in directives_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if (d.get("status") == "pending_review"
                    and d.get("file") and d.get("finding")):
                directives.append(d)
        except json.JSONDecodeError as exc:
            _log(f"WARN: Cannot parse directive line: {exc}")

    if not directives:
        # No Slack on a clean/no-op run (Rafael 2026-08-26: "clean run, no work
        # required" is pure channel noise). Logged for the operator; a genuine
        # SILENT FAILURE or a processed run still Slacks below.
        _log("No pending_review directives found. Exiting.")
        sys.exit(0)

    _log(f"Found {len(directives)} pending directive(s)")

    # ── Process each directive ────────────────────────────────────────────────
    outcomes: dict[str, str] = {}  # key(file+finding) → new status
    processed_count = 0
    permanent_count = 0
    retry_count     = 0
    skipped_count   = 0   # risk-path files routed to human review (NORMAL under the rescope)
    rejected_count  = 0   # board declined the finding/fix (NORMAL — the gate working)
    for directive in directives:
        try:
            result = _process_directive(directive)
        except Exception as _dir_exc:
            # One malformed directive must never abort the whole run (a bad
            # filename used to raise FileNotFoundError and skip every remaining
            # directive). Log, mark failed_permanent, continue. (2026-07-02 fix)
            _log(
                f"ERROR processing directive {directive.get('file', '?')}: "
                f"{type(_dir_exc).__name__}: {_dir_exc} — skipping"
            )
            result = "failed_permanent"
        key = directive.get("file", "") + "\x1f" + directive.get("finding", "")
        if result == "processed":
            outcomes[key] = "processed"
            processed_count += 1
        elif result == "skipped_risk_path":
            outcomes[key] = "skipped_risk_path"   # terminal: routed to human, not re-picked
            skipped_count += 1
        elif result == "board_rejected":
            outcomes[key] = "board_rejected"       # terminal: board declined
            rejected_count += 1
        elif result == "failed_permanent":
            outcomes[key] = "failed_permanent"
            permanent_count += 1
        else:  # retry — leave status pending_review
            retry_count += 1

    # ── Write updated audit_directives.jsonl (atomic) ────────────────────────
    all_lines: list[str] = []
    raw_lines = directives_path.read_text().splitlines()
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            key = d.get("file", "") + "\x1f" + d.get("finding", "")
            if key in outcomes:
                d["status"] = outcomes[key]
            all_lines.append(json.dumps(d, ensure_ascii=False))
        except json.JSONDecodeError:
            all_lines.append(line)

    _write_atomic(directives_path, "\n".join(all_lines) + "\n")
    _log(
        f"Updated audit_directives.jsonl "
        f"({processed_count} processed, {skipped_count} risk-path→human, "
        f"{rejected_count} board-declined, {permanent_count} structural-skip, "
        f"{retry_count} left for retry)"
    )

    # ── Slack summary (2026-09-04 rescope) — NO false 🚨. A board-decline or a risk-path skip is
    # the pipeline WORKING, not a failure (→ ℹ️). But a STRUCTURAL failure (missing file, malformed
    # directive, oversized file) or a transient LLM-unreachable backlog IS worth a human glance
    # (→ ⚠️) and must NOT be laundered into the calm ℹ️ bucket. A run that auto-patched is 🔧. ──
    ts_pt = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    _tail = (f"{skipped_count} risk-path→human · {rejected_count} board-declined · "
             f"{permanent_count} structural-skip · {retry_count} retry next run")
    if processed_count > 0:
        _slack(
            f"🔧 *autonomous_patch_generator.py — {ts_pt}*\n\n"
            f"Auto-patched {processed_count} non-risk-path item(s) → pending_ds_gai_*.json.\n"
            f"{_tail}\n\nautonomous_review.py runs at 7:30 PM ET to call Gro/GAI on these."
        )
    elif permanent_count > 0 or retry_count > 0:
        _slack(
            f"⚠️ *autonomous_patch_generator.py — {ts_pt}*\n\n"
            f"{permanent_count} structural failure(s) need review; {retry_count} could not reach "
            f"the review LLM (transient, will retry next run).\n{_tail}"
        )
    else:
        _slack(
            f"ℹ️ *autonomous_patch_generator.py — {ts_pt}*\n\n"
            f"{len(directives)} directive(s) reviewed, 0 auto-patched — expected: auto-patching is "
            f"scoped to non-risk-path files (docs/tests).\n{_tail}\nNo action needed."
        )

    _log(
        f"=== autonomous_patch_generator.py complete:"
        f" {processed_count} processed, {permanent_count} permanent-failed,"
        f" {retry_count} retry ==="
    )
    fcntl.flock(lock_fd, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
