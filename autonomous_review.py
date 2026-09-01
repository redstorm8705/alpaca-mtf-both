#!/usr/bin/env python3
# ruff: noqa: E501  — long LLM-prompt strings + rationale comments are intentionally long (matches the sibling audit scripts)
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

from gai_client import GAI_MODEL_LADDER, call_gai  # single source of truth for the live Gemini model ladder

# ── Config ────────────────────────────────────────────────────────────────────
_REPO_DIR          = Path("/home/ubuntu/mtf-bot")
_LOGS_DIR          = _REPO_DIR / "logs"
_LOCKFILE          = "/tmp/mtf_autonomous_review.lock"
_GIT_LOCKFILE      = "/tmp/mtf_git.lock"   # shared with auto_deploy.sh
_SLACK_URL         = None  # loaded from .env
_MAX_RETRIES       = 3
_API_TIMEOUT       = 180   # seconds, matches auto_ai_audit.py
_GRO_BASE_URL      = "https://api.groq.com/openai/v1"
_GRO_MODEL         = "openai/gpt-oss-120b"
_GEMINI_MODEL      = GAI_MODEL_LADDER[0]   # display only; call_gai ladders the full gai_client.GAI_MODEL_LADDER
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
    global _SLACK_URL
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

# ── Slack dedup — surface a recurring failure ONCE, not nightly ───────────────
# Rafael 2026-08-26: the pipeline can fail for the SAME reason (e.g. free-tier
# Gemini/Groq exhausted) every night; nagging the channel about a known ongoing
# degradation is noise. Send a given failure `key`, then stay quiet for a cooldown.
# A genuinely NEW failure key still pages immediately. Never raises — a dedup-state
# error must not swallow the alert (it falls through to a normal send).
_DEDUP_FILE       = _REPO_DIR / "data" / "state" / "pipeline_alert_dedup.json"
_DEDUP_COOLDOWN_H = 72  # re-surface the same failure at most once per 3 days

def _slack_dedup(msg: str, key: str, cooldown_h: int = _DEDUP_COOLDOWN_H) -> None:
    # Epoch-float timestamps (never ISO strings) — version-proof and immune to any
    # datetime.fromisoformat() parsing quirk across Python versions.
    now_ts = datetime.now(PT).timestamp()
    state: dict = {}
    try:
        if _DEDUP_FILE.exists():
            state = json.loads(_DEDUP_FILE.read_text())
        last = state.get(key)
        if last is not None:
            elapsed_h = (now_ts - float(last)) / 3600.0
            if elapsed_h < cooldown_h:
                _log(f"Slack deduped: key={key} {elapsed_h:.0f}h < {cooldown_h}h")
                return
    except Exception as exc:
        _log(f"_slack_dedup state read failed ({exc}) — sending anyway")
    _slack(msg)
    try:
        state[key] = now_ts
        _DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEDUP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(_DEDUP_FILE)
    except Exception as exc:
        _log(f"_slack_dedup state write failed ({exc})")

# ── Groq call ─────────────────────────────────────────────────────────────────
def _call_groq(prompt: str) -> dict:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return {"text": None, "error": "GROQ_API_KEY not set", "model": _GRO_MODEL}
    import requests  # type: ignore[import-untyped]
    t0 = time.monotonic()
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
                },
                timeout=_API_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "text": data["choices"][0]["message"]["content"],
                "model": _GRO_MODEL,
                "tokens": data.get("usage", {}).get("total_tokens"),
                "elapsed_s": round(time.monotonic() - t0, 2),
                "error": None,
            }
        except Exception as exc:
            _log(f"Groq attempt {attempt+1}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 5)
    return {
        "text": None,
        "model": _GRO_MODEL,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "error": f"All {_MAX_RETRIES} attempts failed",
    }

# ── Gemini call ───────────────────────────────────────────────────────────────
def _call_gemini(prompt: str) -> dict:
    # Shared laddered client — the SINGLE source of truth for the model list
    # (gai_client.GAI_MODEL_LADDER) + thinking_budget=0. A churned / quota'd / retired model
    # auto-skips to the next LIVE one, so one dead model can never false-flag "GAI down". The
    # outer retry loop handles a transient whole-ladder failure. Replaces the old SDK-pinned
    # call + _call_gemini_rest fallback (call_gai IS the laddered REST path now).
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {
            "text": None, "error": "GEMINI_API_KEY not set", "model": _GEMINI_MODEL,
        }
    t0 = time.monotonic()
    last = None
    for attempt in range(_MAX_RETRIES):
        try:
            text = call_gai(prompt, api_key, max_output_tokens=_GEMINI_MAX_TOKENS, timeout=_API_TIMEOUT)
            return {
                "text": text,
                "model": _GEMINI_MODEL,
                "tokens": None,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "error": None,
            }
        except Exception as exc:
            last = exc
            _log(f"Gemini attempt {attempt+1}/{_MAX_RETRIES} failed: {exc}")
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 5)
    return {
        "text": None,
        "model": _GEMINI_MODEL,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "error": f"All {_MAX_RETRIES} attempts failed (last: {last})",
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

    if status not in ("awaiting_ds_gai", "awaiting_gro_gai"):
        _log(f"SKIP {target_file}: status={status} (not awaiting Gro/GAI review)")
        return True  # not an error, just already processed

    if not prompt:
        _log(f"ERROR {target_file}: ds_gai_prompt is empty — cannot call APIs")
        return False

    _log(f"Processing: {target_file} | {rc_class} | {finding[:60]}...")

    # ── Call Groq ─────────────────────────────────────────────────────────────
    _log("Calling Groq...")
    gro_result = _call_groq(prompt)
    if gro_result["error"]:
        # Per-item failure is redundant with the run-level report (main() surfaces
        # the `failed` bucket in its digest, or the deduped all-failed notice). Log
        # only; no Slack, kills the nightly per-item "will retry" noise (2026-08-26).
        _log(f"Groq failed: {gro_result['error']}")
        return False  # leave status, retry tomorrow

    _log(
        f"Groq OK — {gro_result.get('tokens', '?')} tokens,"
        f" {gro_result.get('elapsed_s', '?')}s"
    )

    # ── Call Gemini ───────────────────────────────────────────────────────────
    _log("Calling Gemini...")
    gai_result = _call_gemini(prompt)
    if gai_result["error"]:
        # Redundant with the run-level report — log only (see Groq branch above).
        _log(f"Gemini failed: {gai_result['error']}")
        return False  # leave status, retry tomorrow

    _log(
        f"Gemini OK — {gai_result.get('tokens', '?')} tokens,"
        f" {gai_result.get('elapsed_s', '?')}s"
    )

    # ── Gro/GAI conflict check ────────────────────────────────────────────────
    gro_text = gro_result["text"] or ""
    gai_text = gai_result["text"] or ""
    # scan 500 chars — 200 was too narrow for concise model outputs
    gro_head  = gro_text.upper()[:500]
    gai_head  = gai_text.upper()[:500]
    gro_verdict = (
        "APPROVE" if "APPROVE" in gro_head
        else "REJECT" if "REJECT" in gro_head
        else "UNCLEAR"
    )
    gai_verdict = (
        "APPROVE" if "APPROVE" in gai_head
        else "REJECT" if "REJECT" in gai_head
        else "UNCLEAR"
    )

    if gro_verdict == "REJECT" or gai_verdict == "REJECT":
        _log(
            f"REJECT detected: Gro={gro_verdict}, GAI={gai_verdict}"
            " — routing to queued_for_review"
        )
        # Write to queue file instead of approvals
        date_str   = datetime.now(PT).strftime("%Y-%m-%d")
        time_str   = datetime.now(PT).strftime("%Y-%m-%d %H:%M PT")
        queue_path = _LOGS_DIR / f"queued_for_review_{date_str}.md"
        queue_entry = (
            f"\n## {target_file} — Gro/GAI REJECT — {time_str}\n"
            f"REASON: Gro verdict={gro_verdict}, GAI verdict={gai_verdict}\n"
            f"FINDING: {finding}\n"
            "ACTION: User review required — see raw responses below\n\n"
            f"### Groq Response\n{gro_text}\n\n"
            f"### Gemini Response\n{gai_text}\n"
        )
        with open(queue_path, "a", encoding="utf-8") as f:
            f.write(queue_entry)
        # No per-item Slack here (2026-07-02 format-lock): 7 rejects used to fire
        # 7 separate messages. Rejects are rolled into ONE digest line in main().
        data["gro_response"] = gro_text
        data["gai_response"] = gai_text
        data["status"]       = "rejected_gro_gai"
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

### Gro/GAI Verdicts
- Groq (Gro): **{gro_verdict}**
- Gemini: **{gai_verdict}**

---

### Groq Full Response

{gro_text}

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
    data["gro_response"] = gro_text
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

    # ── Find pending items (accept both old ds_gai and new gro_gai filenames) ──
    pending_files = sorted(
        glob.glob(str(_LOGS_DIR / "pending_ds_gai_*.json"))
        + glob.glob(str(_LOGS_DIR / "pending_gro_gai_*.json"))
    )
    _pending_statuses = {"awaiting_ds_gai", "awaiting_gro_gai"}
    awaiting = [
        p for p in pending_files
        if json.loads(Path(p).read_text()).get("status") in _pending_statuses
    ]

    if not awaiting:
        _log("No items awaiting Gro/GAI review. Exiting.")
        fcntl.flock(git_lock_fd, fcntl.LOCK_UN)
        sys.exit(0)

    _log(f"Found {len(awaiting)} item(s) awaiting Gro/GAI review")

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
        _slack_dedup(
            "⚠️ autonomous_review.py: All DS/GAI API calls failed tonight."
            " Will retry tomorrow. (Deduped: silenced for 72h unless it recovers"
            " then fails again, or a different failure appears.)",
            key="all_dsgai_failed",
        )
        fcntl.flock(git_lock_fd, fcntl.LOCK_UN)
        sys.exit(1)

    # ── Commit + push processed files ─────────────────────────────────────────
    _log("Committing DS/GAI results...")
    date_str = datetime.now(PT).strftime("%Y-%m-%d")
    files_to_add = (
        list(glob.glob(str(_LOGS_DIR / "pending_ds_gai_*.json")))
        + list(glob.glob(str(_LOGS_DIR / "pending_gro_gai_*.json")))
        + list(glob.glob(str(_LOGS_DIR / f"pending_approvals_{date_str}.md")))
        + list(glob.glob(str(_LOGS_DIR / f"queued_for_review_{date_str}.md")))
    )
    # Add each file individually (no wildcards — per DS/GAI audit recommendation)
    for f in files_to_add:
        if Path(f).exists():
            _git("add", str(Path(f).relative_to(_REPO_DIR)))

    # Bucket by the ACTUAL post-review status (2026-07-02 fix): `processed` only
    # means "handled without API failure" — it INCLUDES rejects. The old commit
    # message and Slack summary called everything "ready for approval", which on
    # 2026-07-02 announced 7 Gro/GAI-REJECTED patches as READY.
    ready: list[str] = []
    rejected: list[str] = []
    for p in processed:
        try:
            st = json.loads((_LOGS_DIR / p).read_text()).get("status", "")
        except Exception:
            st = ""
        if st == "ready_for_approval":
            ready.append(p)
        elif st == "rejected_gro_gai":
            rejected.append(p)

    failed_note = ("\n\nFailed: " + ", ".join(failed)) if failed else ""
    commit_msg = (
        f"Gro/GAI review: {len(ready)} ready, {len(rejected)} rejected,"
        f" {len(failed)} failed\n\n"
        + "\n".join(f"- READY: {p}" for p in ready)
        + ("\n" if ready and rejected else "")
        + "\n".join(f"- REJECTED: {p}" for p in rejected)
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

    # ── Final Slack summary — ONE digest message, honest buckets ─────────────
    ts_pt = datetime.now(PT).strftime("%Y-%m-%d %I:%M %p PT")
    def _strip_prefix(name: str) -> str:
        return name.replace("pending_gro_gai_", "").replace(
            "pending_ds_gai_", "").replace(".json", "")
    parts = [f"🤖 *Autonomous review — {ts_pt}*"]
    if ready:
        parts.append(
            f"\n✅ *READY for your approval ({len(ready)})* — Gro+GAI both APPROVE:\n"
            + "\n".join(f"• {_strip_prefix(p)}" for p in ready)
            + "\nStart a session to review; auto_deploy.sh deploys after approval."
        )
    if rejected:
        parts.append(
            f"\n🛑 *REJECTED by Gro/GAI ({len(rejected)})* — NOT shippable; "
            f"queued for your review in queued_for_review_"
            f"{datetime.now(PT).strftime('%Y-%m-%d')}.md:\n"
            + "\n".join(f"• {_strip_prefix(p)}" for p in rejected)
        )
    if failed:
        parts.append(f"\n⚠️ API-failed, retrying next run ({len(failed)}): "
                     + ", ".join(_strip_prefix(p) for p in failed))
    if not ready and not rejected and not failed:
        parts.append("\nNothing to review tonight.")
    _slack("\n".join(parts))

    _log(
        f"=== autonomous_review.py complete:"
        f" {len(processed)} processed, {len(failed)} failed ==="
    )


if __name__ == "__main__":
    main()
