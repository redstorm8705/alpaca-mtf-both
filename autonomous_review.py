#!/usr/bin/env python3
"""autonomous_review.py — Stage 2 of the autonomous patch pipeline.

Runs on OCI at 11 PM ET (weeknights) after the CCR's 10 PM run.
Reads pending_ds_gai_*.json files written by the CCR, calls DeepSeek
and Gemini with the stored DS/GAI prompt, writes raw responses into
pending_approvals_YYYY-MM-DD.md, and pushes to GitHub.

No autonomous summary is written — raw DS and GAI responses are shown
verbatim so the user reads them directly at session start.

Author: autonomous pipeline (S44)
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Config ────────────────────────────────────────────────────────────────────
_REPO_DIR          = Path("/home/ubuntu/mtf-bot")
_LOGS_DIR          = _REPO_DIR / "logs"
_LOCKFILE          = "/tmp/mtf_autonomous_review.lock"
_GIT_LOCKFILE      = "/tmp/mtf_git.lock"   # shared with auto_deploy.sh
_SLACK_URL         = None  # loaded from .env
_MAX_RETRIES       = 3
_API_TIMEOUT       = 180   # seconds, matches auto_ai_audit.py
_DS_BASE_URL       = None  # loaded from .env
_DS_MODEL          = "deepseek-chat"
_GEMINI_MODEL      = "gemini-2.5-flash"
# explicit cap — flash default 8192 causes mid-response truncation
_GEMINI_MAX_TOKENS = 16384

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# ── Logging ───────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    ts = datetime.now(PT).strftime("%Y-%m-%d %H:%M:%S PT")
    print(f"[{ts}] {msg}", flush=True)

# ── Environment ───────────────────────────────────────────────────────────────
def _load_env() -> dict:
    global _SLACK_URL, _DS_BASE_URL
    env: dict = {}
    env_path = _REPO_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
                os.environ.setdefault(k.strip(), v.strip())
    # S58: .env defines SLACK_WEBHOOK_URL (not SLACK_WEBHOOK) — accept both
    _SLACK_URL = (
        env.get("SLACK_WEBHOOK") or env.get("SLACK_WEBHOOK_URL")
        or os.environ.get("SLACK_WEBHOOK", "")
        or os.environ.get("SLACK_WEBHOOK_URL", "")
    )
    _DS_BASE_URL = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    return env

# ── Slack ─────────────────────────────────────────────────────────────────────
def _slack(msg: str) -> None:
    if not _SLACK_URL:
        _log(f"Slack (no webhook): {msg}")
        return
    import urllib.request
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
        _log(f"Undelivered message: {msg}")

# ── DS call ───────────────────────────────────────────────────────────────────
def _call_deepseek(prompt: str) -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {"text": None, "error": "DEEPSEEK_API_KEY not set", "model": _DS_MODEL}
    import requests  # type: ignore[import-untyped]
    t0 = time.monotonic()
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
                },
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "text": data["choices"][0]["message"]["content"],
                "model": _DS_MODEL,
                "tokens": data.get("usage", {}).get("total_tokens"),
                "elapsed_s": round(time.monotonic() - t0, 2),
                "error": None,
            }
        except Exception as exc:
            _log(f"DeepSeek attempt {attempt+1}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 5)
    return {
        "text": None,
        "model": _DS_MODEL,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "error": f"All {_MAX_RETRIES} attempts failed",
    }

# ── Gemini call ───────────────────────────────────────────────────────────────
def _call_gemini(prompt: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {
            "text": None, "error": "GEMINI_API_KEY not set", "model": _GEMINI_MODEL,
        }
    t0 = time.monotonic()
    for attempt in range(_MAX_RETRIES):
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=_GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=_GEMINI_MAX_TOKENS,
                ),
            )
            text = response.text if hasattr(response, "text") else str(response)
            usage = getattr(response, "usage_metadata", None)
            return {
                "text": text,
                "model": _GEMINI_MODEL,
                "tokens": (
                    getattr(usage, "total_token_count", None) if usage else None
                ),
                "elapsed_s": round(time.monotonic() - t0, 2),
                "error": None,
            }
        except ImportError:
            return _call_gemini_rest(prompt, api_key, t0)
        except Exception as exc:
            _log(f"Gemini attempt {attempt+1}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 5)
    return {
        "text": None,
        "model": _GEMINI_MODEL,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "error": f"All {_MAX_RETRIES} attempts failed",
    }

def _call_gemini_rest(prompt: str, api_key: str, t0: float) -> dict:
    import requests  # type: ignore[import-untyped]
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models"
            f"/{_GEMINI_MODEL}:generateContent?key={api_key}"
        )
        resp = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": _GEMINI_MAX_TOKENS,
                },
            },
            timeout=_API_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return {
            "text": text,
            "model": f"{_GEMINI_MODEL}-rest",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except Exception as exc:
        return {
            "text": None,
            "model": f"{_GEMINI_MODEL}-rest",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }

# ── Git helpers ───────────────────────────────────────────────────────────────
def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=_REPO_DIR,
        capture_output=True, text=True, check=check,
    )

def _git_push_with_retry() -> bool:
    for attempt in range(_MAX_RETRIES):
        result = _git("push", "origin", "main", check=False)
        if result.returncode == 0:
            return True
        _log(
            f"Push attempt {attempt+1}/{_MAX_RETRIES} failed:"
            f" {result.stderr.strip()}"
        )
        _git("pull", "--rebase", "origin", "main", check=False)
        time.sleep(3)
    return False

# ── Atomic write (RC-5 compliance) ────────────────────────────────────────────
def _write_atomic(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

# ── Core processing ───────────────────────────────────────────────────────────
def _process_pending_item(json_path: Path) -> bool:
    """Process one pending_ds_gai JSON. Returns True on success."""
    try:
        data = json.loads(json_path.read_text())
    except Exception as exc:
        _log(f"ERROR: Cannot parse {json_path}: {exc}")
        return False

    target_file = data.get("file", "unknown")
    finding     = data.get("finding", "")
    rc_class    = data.get("rc_class", "")
    prompt      = data.get("ds_gai_prompt", "")
    status      = data.get("status", "")

    if status != "awaiting_ds_gai":
        _log(f"SKIP {target_file}: status={status} (not awaiting_ds_gai)")
        return True  # not an error, just already processed

    if not prompt:
        _log(f"ERROR {target_file}: ds_gai_prompt is empty — cannot call APIs")
        return False

    _log(f"Processing: {target_file} | {rc_class} | {finding[:60]}...")

    # ── Call DeepSeek ─────────────────────────────────────────────────────────
    _log("Calling DeepSeek...")
    ds_result = _call_deepseek(prompt)
    if ds_result["error"]:
        _log(f"DeepSeek failed: {ds_result['error']}")
        _slack(
            f"⚠️ autonomous_review.py: DeepSeek API failed for {target_file}."
            " Will retry next night."
        )
        return False  # leave status as awaiting_ds_gai, retry tomorrow

    _log(
        f"DeepSeek OK — {ds_result.get('tokens', '?')} tokens,"
        f" {ds_result.get('elapsed_s', '?')}s"
    )

    # ── Call Gemini ───────────────────────────────────────────────────────────
    _log("Calling Gemini...")
    gai_result = _call_gemini(prompt)
    if gai_result["error"]:
        _log(f"Gemini failed: {gai_result['error']}")
        _slack(
            f"⚠️ autonomous_review.py: Gemini API failed for {target_file}."
            " Will retry next night."
        )
        return False  # leave status as awaiting_ds_gai, retry tomorrow

    _log(
        f"Gemini OK — {gai_result.get('tokens', '?')} tokens,"
        f" {gai_result.get('elapsed_s', '?')}s"
    )

    # ── DS/GAI conflict check ─────────────────────────────────────────────────
    ds_text  = ds_result["text"] or ""
    gai_text = gai_result["text"] or ""
    # scan 500 chars — 200 was too narrow for concise model outputs
    ds_head  = ds_text.upper()[:500]
    gai_head = gai_text.upper()[:500]
    ds_verdict = (
        "APPROVE" if "APPROVE" in ds_head
        else "REJECT" if "REJECT" in ds_head
        else "UNCLEAR"
    )
    gai_verdict = (
        "APPROVE" if "APPROVE" in gai_head
        else "REJECT" if "REJECT" in gai_head
        else "UNCLEAR"
    )

    if ds_verdict == "REJECT" or gai_verdict == "REJECT":
        _log(
            f"REJECT detected: DS={ds_verdict}, GAI={gai_verdict}"
            " — routing to queued_for_review"
        )
        # Write to queue file instead of approvals
        date_str   = datetime.now(PT).strftime("%Y-%m-%d")
        time_str   = datetime.now(PT).strftime("%Y-%m-%d %H:%M PT")
        queue_path = _LOGS_DIR / f"queued_for_review_{date_str}.md"
        queue_entry = (
            f"\n## {target_file} — DS/GAI REJECT — {time_str}\n"
            f"REASON: DS verdict={ds_verdict}, GAI verdict={gai_verdict}\n"
            f"FINDING: {finding}\n"
            "ACTION: User review required — see raw responses below\n\n"
            f"### DeepSeek Response\n{ds_text}\n\n"
            f"### Gemini Response\n{gai_text}\n"
        )
        with open(queue_path, "a", encoding="utf-8") as f:
            f.write(queue_entry)
        _slack(
            f"⚠️ DS/GAI REJECT: {target_file} — {ds_verdict}/{gai_verdict}."
            f" See queued_for_review_{date_str}.md"
        )
        data["ds_response"]  = ds_text
        data["gai_response"] = gai_text
        data["status"]       = "rejected_ds_gai"
        _write_atomic(json_path, json.dumps(data, indent=2))
        return True

    # ── Write pending_approvals_*.md ──────────────────────────────────────────
    date_str       = datetime.now(PT).strftime("%Y-%m-%d")
    approvals_path = _LOGS_DIR / f"pending_approvals_{date_str}.md"
    patch_content  = ""
    patch_file_path = _REPO_DIR / data.get("patch_file", "")
    if patch_file_path.exists():
        patch_content = patch_file_path.read_text()

    board  = data.get("board", {})
    static = data.get("static_analysis", {})

    # pre-compute board verdict strings to keep f-string lines ≤88 chars
    board_a   = f"{board.get('A_strict_parser', '?')} — {board.get('A_notes', '')}"
    board_b   = f"{board.get('B_red_teamer', '?')} — {board.get('B_notes', '')}"
    board_c   = f"{board.get('C_quant_risk', '?')} — {board.get('C_notes', '')}"
    patch_ref = data.get("patch_file", "")

    approval_entry = f"""
## {target_file} — READY FOR APPROVAL
**Date drafted:** {data.get('created_at', 'unknown')}
**Finding:** {finding}
**RC class:** {rc_class}

### Board Verdicts
- Agent A (Strict Parser): {board_a}
- Agent B (Red Teamer): {board_b}
- Agent C (Quant Risk): {board_c}

### Static Analysis
- py_compile: {static.get('py_compile', '?')}
- mypy: {static.get('mypy', '?')}
- ruff: {static.get('ruff', '?')}
- Second-agent: {data.get('second_agent', '?')}

### Integrity Anchors
- SHA256 at draft: `{data.get('sha256_at_draft', 'unknown')}`
- Base commit: `{data.get('base_commit_sha', 'unknown')}`
- Patch file: `{patch_ref}`

### DS/GAI Verdicts
- DeepSeek: **{ds_verdict}**
- Gemini: **{gai_verdict}**

---

### DeepSeek Full Response

{ds_text}

---

### Gemini Full Response

{gai_text}

---

### Diff
```diff
{patch_content}
```

**STATUS: ready_for_approval**
**To apply:** verify SHA256 matches, then run:
`git apply {patch_ref}` (NOT the Edit tool)

---
"""
    with open(approvals_path, "a", encoding="utf-8") as f:
        f.write(approval_entry)

    _log(f"Wrote approval entry to {approvals_path}")

    # ── Update JSON status ────────────────────────────────────────────────────
    data["ds_response"]  = ds_text
    data["gai_response"] = gai_text
    data["status"]       = "ready_for_approval"
    _write_atomic(json_path, json.dumps(data, indent=2))

    return True


def main() -> None:
    # ── Own-process lockfile (prevent duplicate runs) ─────────────────────────
    lock_fd = open(_LOCKFILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("SKIP: Another autonomous_review.py is already running")
        sys.exit(0)

    _log("=== autonomous_review.py starting ===")
    _load_env()

    os.chdir(_REPO_DIR)

    # ── Git lock (shared with auto_deploy.sh) ─────────────────────────────────
    git_lock_fd = open(_GIT_LOCKFILE, "w")
    try:
        fcntl.flock(git_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("Git lock held by another process (auto_deploy.sh?). Waiting 60s...")
        time.sleep(60)
        try:
            fcntl.flock(git_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _log("ERROR: Cannot acquire git lock after 60s wait. Aborting.")
            _slack(
                "⚠️ autonomous_review.py: Cannot acquire git lock"
                " — skipping tonight. Will retry tomorrow."
            )
            sys.exit(1)

    # ── Pull latest ───────────────────────────────────────────────────────────
    _log("git pull...")
    result = _git("pull", "--rebase", "origin", "main", check=False)
    if result.returncode != 0:
        _log(f"git pull failed: {result.stderr.strip()}")

    # ── Find pending items ────────────────────────────────────────────────────
    pending_files = sorted(glob.glob(str(_LOGS_DIR / "pending_ds_gai_*.json")))
    awaiting = [
        p for p in pending_files
        if json.loads(Path(p).read_text()).get("status") == "awaiting_ds_gai"
    ]

    if not awaiting:
        _log("No items awaiting DS/GAI review. Exiting.")
        fcntl.flock(git_lock_fd, fcntl.LOCK_UN)
        sys.exit(0)

    _log(f"Found {len(awaiting)} item(s) awaiting DS/GAI review")

    # ── Process each pending item ─────────────────────────────────────────────
    processed: list[str] = []
    failed: list[str] = []
    for path_str in awaiting:
        path = Path(path_str)
        _log(f"--- Processing {path.name} ---")
        success = _process_pending_item(path)
        (processed if success else failed).append(path.name)

    if not processed:
        _log("No items successfully processed tonight.")
        _slack(
            "⚠️ autonomous_review.py: All DS/GAI API calls failed tonight."
            " Will retry tomorrow."
        )
        fcntl.flock(git_lock_fd, fcntl.LOCK_UN)
        sys.exit(1)

    # ── Commit + push processed files ─────────────────────────────────────────
    _log("Committing DS/GAI results...")
    date_str = datetime.now(PT).strftime("%Y-%m-%d")
    files_to_add = (
        list(glob.glob(str(_LOGS_DIR / "pending_ds_gai_*.json")))
        + list(glob.glob(str(_LOGS_DIR / f"pending_approvals_{date_str}.md")))
        + list(glob.glob(str(_LOGS_DIR / f"queued_for_review_{date_str}.md")))
    )
    # Add each file individually (no wildcards — per DS/GAI audit recommendation)
    for f in files_to_add:
        if Path(f).exists():
            _git("add", str(Path(f).relative_to(_REPO_DIR)))

    failed_note = ("\n\nFailed: " + ", ".join(failed)) if failed else ""
    commit_msg = (
        f"DS/GAI responses received: {len(processed)} item(s) ready for approval\n\n"
        + "\n".join(f"- {p}" for p in processed)
        + failed_note
        + "\n\nCo-Authored-By: autonomous_review.py <noreply@anthropic.com>"
    )
    _git("commit", "-m", commit_msg)

    if not _git_push_with_retry():
        _log("ERROR: git push failed after 3 retries")
        _slack(
            "⚠️ autonomous_review.py: git push failed"
            " — DS/GAI results saved locally but not pushed. Check OCI."
        )
        fcntl.flock(git_lock_fd, fcntl.LOCK_UN)
        sys.exit(1)

    fcntl.flock(git_lock_fd, fcntl.LOCK_UN)
    _log("git push OK")

    # ── Final Slack summary ───────────────────────────────────────────────────
    ts_pt = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    items_summary = "\n".join(
        f"• {p.replace('pending_ds_gai_', '').replace('.json', '')}"
        for p in processed
    )
    msg = (
        f"🎯 *Patches ready for approval — {ts_pt}*\n\n"
        f"*READY (DS+GAI reviewed):*\n{items_summary}\n\n"
        "Start a session and the pending approvals will be presented automatically.\n"
        "auto_deploy.sh deploys at 11:30 PM ET after your approval."
    )
    _slack(msg)

    _log(
        f"=== autonomous_review.py complete:"
        f" {len(processed)} processed, {len(failed)} failed ==="
    )


if __name__ == "__main__":
    main()
