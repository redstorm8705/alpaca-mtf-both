# ruff: noqa: E501
"""gai_client.py — THE single source of truth for calling Google Gemini (GAI) across this repo.

WHY THIS EXISTS (permanent fix, Rafael directive 2026-08-31 "fix it permanently, everywhere"):
every GAI caller used to HARDCODE one model string (preship_audit, ci_audit, auto_ai_audit,
nightly_audit, midday_audit, weekly_*, autonomous_*, gai_health, ...). Gemini models churn FAST —
gemini-3.5-flash went 200 -> 503 -> 429 -> 404 within HOURS on 2026-08-31 — so a single pinned model
repeatedly false-flagged "GAI down", dropped ships to the slow NVIDIA substitute, and wasted hours
re-pinning file after file. This module makes that recurrence IMPOSSIBLE: ONE ladder, defined ONCE,
used everywhere. When the roster churns you update `GAI_MODEL_LADDER` in THIS one place and every
caller inherits it; at call time a dead model (404 retired / 429 quota / 503 overload) auto-skips to
the next — and different models carry SEPARATE free-tier quotas, so a 429 on one is often a 200 on
the next.

Stdlib only (no SDK, no `requests`) so it is importable from every surface — the audit scripts at
repo root, the preship gate under .claude/, and the CI gate under .github/ (each inserts the repo
root on sys.path before importing).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

# ── THE ladder — the ONLY place a Gemini model name is chosen. Ordered newest/strongest first.
# Verified live 2026-08-31 (same free key): 3.7-flash, 3.1-flash-lite, 3-flash-preview,
# flash-latest all returned 200; the previously-pinned gemini-3.5-flash returned 404 (retired).
# UPDATE THIS TUPLE (only) when the roster churns — every caller inherits the change.
GAI_MODEL_LADDER: tuple[str, ...] = (
    "gemini-3.1-flash-lite",   # FIRST: proven to emit a clean, terse, parseable VERDICT for the
                               # preship/CI gate (the canary self-healed to it 2026-08-31 when
                               # 3.7-flash returned 200 but no single clean VERDICT line). Live 200.
    "gemini-3.7-flash",        # stronger/newer fallback
    "gemini-3-flash-preview",
    "gemini-flash-latest",
)

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GAIError(RuntimeError):
    """Raised only when the WHOLE ladder fails (every model 404/429/503/parse-miss/error). Callers
    escalate exactly as before (free retry / paid-if-allowed / NVIDIA substitute) — this never
    masks a real outage, it only prevents ONE churned model from looking like a total outage."""


def call_gai(
    prompt: str,
    api_key: str,
    *,
    max_output_tokens: int = 2048,
    timeout: int = 180,
    thinking_budget: int = 0,
    temperature: float = 0.1,
    models: tuple[str, ...] = GAI_MODEL_LADDER,
) -> str:
    """Call Gemini, laddering across `models`; return the FIRST model's response text.

    A 404 (retired) / 429 (quota) / 503 (overload) / parse-miss on one model skips to the next.
    `thinking_budget=0` stops a thinking model from spending the whole token budget on hidden
    reasoning (which yields EMPTY verdict text). Raises `GAIError` only if the WHOLE ladder fails.
    NEVER interpolates the request URL (which carries the key) into any exception message.
    """
    if not api_key:
        raise GAIError("no GEMINI api key provided")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,   # 0.1 default = deterministic audits (restores prior behavior)
            "thinkingConfig": {"thinkingBudget": thinking_budget},
        },
    }).encode()
    last = "no models tried"
    for model in models:
        req = urllib.request.Request(
            f"{_BASE}/{model}:generateContent?key={api_key}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"      # 404 retired / 429 quota / 503 overload — next model
            continue
        except Exception as e:           # noqa: BLE001 — DNS/timeout/etc.; try the next model
            last = type(e).__name__
            continue
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            last = "unparseable response"  # delivered but no candidate text — next model
            continue
        if not (text or "").strip():
            last = "empty response text"   # 200 but empty (thinking ate the budget / MAX_TOKENS) — next model
            continue
        return text
    raise GAIError(f"all Gemini ladder models failed (last: {last})")
