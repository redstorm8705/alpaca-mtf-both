#!/usr/bin/env python3
"""autonomous_patch_generator.py — Stage 1.5 of the autonomous patch pipeline.

Runs on OCI at 6 PM ET (23:00 UTC) weeknights, between:
  - Stage 1 (1:35 PM PT): auto_ai_audit.py → logs/audit_directives.jsonl
  - Stage 2 (11 PM ET):   autonomous_review.py → logs/pending_approvals_*.md

For each unprocessed directive in audit_directives.jsonl:
  1. Full read of target file
  2. 3-agent board vote (Strict Parser / Red Teamer / Quant Risk) via DeepSeek API
  3. Diff generation via DeepSeek API
  4. Static analysis (py_compile + mypy + ruff) on target file
  5. Cold second-agent logic review via DeepSeek API
  6. Write pending_ds_gai_*.json + .patch file (for autonomous_review.py to consume)
  7. Mark directive as processed (atomic write)
  8. Slack summary

Author: autonomous pipeline (S56)
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
_REPO_DIR     = Path("/home/ubuntu/mtf-bot")
_LOGS_DIR     = _REPO_DIR / "logs"
_LOCKFILE     = "/tmp/mtf_autonomous_patch_generator.lock"
_MAX_RETRIES  = 3
_API_TIMEOUT  = 180
_DS_BASE_URL  = "https://api.deepseek.com"
_DS_MODEL     = "deepseek-chat"
_SLACK_URL    = None  # loaded from .env

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# ── RTH block (9:30 AM–4:00 PM ET weekdays) ──────────────────────────────────
_now_et = datetime.now(ET)
if _now_et.weekday() < 5:
    _mins = _now_et.hour * 60 + _now_et.minute
    if (9 * 60 + 30) <= _mins < (16 * 60):
        print(
            "BLOCKED: Cannot run during RTH hours"
            " (9:30 AM–4:00 PM ET weekdays)."
        )
        sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    ts = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S PT")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    log_path = _LOGS_DIR / "autonomous_patch_generator.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # log file write failure is non-fatal; stdout is the primary log

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
    _SLACK_URL = os.environ.get("SLACK_WEBHOOK", "")

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

# ── DeepSeek API ──────────────────────────────────────────────────────────────
def _call_deepseek(prompt: str) -> str | None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        _log("ERROR: DEEPSEEK_API_KEY not set")
        return None
    import requests  # type: ignore[import-untyped]
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.post(
                f"{_DS_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _DS_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            _log(f"DeepSeek attempt {attempt+1}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 5)
    return None

# ── Board vote (3 cold independent agents via DS API) ────────────────────────
def _board_vote(
    file_content: str,
    finding: str,
    recommended_fix: str,
    rc_class: str,
) -> dict:
    """Run 3 independent board agents. Returns dict with verdicts + notes."""

    base_context = (
        f"RC class: {rc_class}\n"
        f"Finding: {finding}\n"
        f"Recommended fix: {recommended_fix}\n\n"
        f"File content (truncated to first 6000 chars):\n"
        f"{file_content[:6000]}"
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
        response = _call_deepseek(prompt)
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

# ── Diff generation via DS API ────────────────────────────────────────────────
def _generate_diff(
    file_path: Path,
    file_content: str,
    finding: str,
    recommended_fix: str,
) -> str | None:
    prompt = (
        "You are a senior engineer generating a minimal unified diff patch.\n"
        "Return ONLY the unified diff — no explanation, no markdown fences.\n"
        "The diff must apply cleanly with: git apply <patch>\n\n"
        f"File: {file_path}\n"
        f"Finding: {finding}\n"
        f"Recommended fix: {recommended_fix}\n\n"
        f"File content:\n{file_content[:8000]}"
    )
    _log("Generating diff via DeepSeek...")
    response = _call_deepseek(prompt)
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
    _log("Running cold second-agent review...")
    response = _call_deepseek(prompt)
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
    return (
        f"AUDIT REQUEST — {file_path.name} | {rc_class}\n\n"
        f"FINDING: {finding}\n"
        f"RECOMMENDED FIX: {recommended_fix}\n\n"
        f"PROPOSED DIFF:\n{diff}\n\n"
        f"FILE CONTENT (first 6000 chars):\n{file_content[:6000]}\n\n"
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

# ── Process one directive ─────────────────────────────────────────────────────
def _process_directive(directive: dict) -> bool:
    """Process one audit directive. Returns True on success."""
    file_rel  = directive.get("file", "")
    finding   = directive.get("finding", "")
    rec_fix   = directive.get("recommended_fix", "")
    rc_class  = directive.get("rc_class", "RC-?")

    if not file_rel or not finding:
        _log(f"SKIP: directive missing file or finding: {directive}")
        return False

    file_path = _REPO_DIR / file_rel
    if not file_path.exists():
        _log(f"SKIP: target file not found: {file_path}")
        return False

    _log(f"Processing: {file_rel} | {rc_class} | {finding[:60]}...")

    file_content = file_path.read_text(encoding="utf-8")

    # ── Board vote ────────────────────────────────────────────────────────────
    _log("Running board vote...")
    board = _board_vote(file_content, finding, rec_fix, rc_class)
    board_verdicts = [
        board["A_strict_parser"],
        board["B_red_teamer"],
        board["C_quant_risk"],
    ]
    reject_count = board_verdicts.count("REJECT")
    if reject_count >= 2:
        _log(f"Board rejected {file_rel}: {board_verdicts} — skipping diff generation")
        return False

    # ── Diff generation ───────────────────────────────────────────────────────
    diff = _generate_diff(file_path, file_content, finding, rec_fix)
    if not diff or not diff.strip().startswith("---"):
        _log(f"SKIP {file_rel}: diff generation failed or returned non-diff output")
        return False

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
    safe_name = (
        rc_class.lower().replace("-", "")
        + "_"
        + file_path.stem.replace("_", "")[:20]
    )
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

    return True

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

    directives: list[dict] = []
    for line in directives_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("status") != "processed":
                directives.append(d)
        except json.JSONDecodeError as exc:
            _log(f"WARN: Cannot parse directive line: {exc}")

    if not directives:
        _log("No unprocessed directives found. Exiting.")
        sys.exit(0)

    _log(f"Found {len(directives)} unprocessed directive(s)")

    # ── Process each directive ────────────────────────────────────────────────
    processed_count = 0
    failed_count    = 0
    for directive in directives:
        success = _process_directive(directive)
        if success:
            directive["status"] = "processed"
            processed_count += 1
        else:
            failed_count += 1

    # ── Write updated audit_directives.jsonl (atomic) ────────────────────────
    all_lines: list[str] = []
    raw_lines = directives_path.read_text().splitlines()
    processed_findings = {
        d["finding"] for d in directives if d.get("status") == "processed"
    }
    for line in raw_lines:
        line = line.strip()
        if not line:
            all_lines.append(line)
            continue
        try:
            d = json.loads(line)
            if d.get("finding") in processed_findings:
                d["status"] = "processed"
            all_lines.append(json.dumps(d))
        except json.JSONDecodeError:
            all_lines.append(line)

    _write_atomic(directives_path, "\n".join(all_lines) + "\n")
    _log(f"Updated audit_directives.jsonl ({processed_count} marked processed)")

    # ── Slack summary ─────────────────────────────────────────────────────────
    ts_pt = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    _slack(
        f"🔧 *autonomous_patch_generator.py — {ts_pt}*\n\n"
        f"Processed: {processed_count} directive(s) → pending_ds_gai_*.json written\n"
        f"Failed/skipped: {failed_count}\n\n"
        "autonomous_review.py runs at 11 PM ET to call DS/GAI on these items."
    )

    _log(
        f"=== autonomous_patch_generator.py complete:"
        f" {processed_count} processed, {failed_count} failed ==="
    )
    fcntl.flock(lock_fd, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
