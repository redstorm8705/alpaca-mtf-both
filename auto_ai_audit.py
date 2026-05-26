#!/usr/bin/env python3
"""
auto_ai_audit.py — Automated DS/GAI external audit gate
(Step 4 of mandatory patch sequence) + autonomous meta-audit mode.

MODES
─────
Patch-gate mode (default):
    Submits an IDENTICAL prompt to both DeepSeek (DS) and Google Gemini (GAI),
    returning structured JSON + printing both raw responses so Claude can
    generate the 3-Point AI Summary inline.

    python3 auto_ai_audit.py --prompt "Your full audit prompt here"
    python3 auto_ai_audit.py --prompt-file /path/to/prompt.txt
    echo "Your prompt" | python3 auto_ai_audit.py

Meta-audit mode (--meta-audit):
    Auto-reads today's midday + nightly Gemini audit reports, recent trade
    events (Slack notification proxy), and recent bot log. Constructs a
    cross-review prompt and submits to both DS + Gemini independently.
    DS is the primary cross-reviewer (genuinely independent model).
    Gemini performs a self-consistency check on its own prior output.
    Posts a Slack summary of findings on completion.

    python3 auto_ai_audit.py --meta-audit
    python3 auto_ai_audit.py --meta-audit --no-slack   # suppress Slack post

OUTPUT
──────
    logs/ai_audit_YYYYMMDD_HHMMSS_PT.json       patch-gate mode
    logs/ai_audit_meta_YYYYMMDD_HHMMSS_PT.json  meta-audit mode
    (atomic write via tmp→replace in both cases)

EXIT CODES
──────────
    0 — both APIs succeeded
    1 — partial (one API failed; partial results written)
    2 — both APIs failed; no usable output

BLOCKED during RTH: 9:30 AM–4:00 PM ET weekdays.
Use --no-rth-block for testing only — never during live trading.

ENVIRONMENT VARIABLES
─────────────────────
    DEEPSEEK_API_KEY    — DeepSeek API key (sk-...)
    GEMINI_API_KEY      — Google Gemini API key
    SLACK_WEBHOOK_URL   — Slack incoming webhook (meta-audit Slack post)
    DEEPSEEK_BASE_URL   — optional override (default: https://api.deepseek.com)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Absolute path anchors (RC-2 prevention) ───────────────────────────────────
_HERE = Path(__file__).resolve().parent
_LOGS_DIR = _HERE / "logs"

# ── Timezone constants (RC-1 prevention) ─────────────────────────────────────
_ET = ZoneInfo("America/New_York")
_PT = ZoneInfo("America/Los_Angeles")

# ── API constants ─────────────────────────────────────────────────────────────
_DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
_DEEPSEEK_MODEL = "deepseek-chat"
_GEMINI_MODEL = "gemini-3.1-pro-preview"  # matches Google AI Studio selection
_API_TIMEOUT_S = 180  # 3-minute wall-clock limit per API call

# ── Meta-audit constants ──────────────────────────────────────────────────────
_TRADE_EVENTS_TAIL = 100   # last N trade events to include as Slack proxy
_BOT_LOG_TAIL_LINES = 150  # last N bot log lines to include

# ── GitHub Gist endpoint (board CCR reads from here — raw IP is allowlisted) ──
_GIST_ID = "1574ea556d06e7a1db45d00097f9c069"
_GIST_RAW_URL = (
    f"https://gist.githubusercontent.com/redstorm8705/{_GIST_ID}"
    "/raw/meta_audit_latest.json"
)


# ── RTH block ────────────────────────────────────────────────────────────────
def _check_rth_block() -> None:
    """Exit if called during Regular Trading Hours (9:30 AM–4:00 PM ET, Mon–Fri)."""
    now_et = datetime.now(_ET)
    if now_et.weekday() < 5:
        mins = now_et.hour * 60 + now_et.minute
        if (9 * 60 + 30) <= mins < (16 * 60):
            print(
                "BLOCKED: auto_ai_audit.py cannot run during RTH "
                "(9:30 AM–4:00 PM ET / 6:30 AM–1:00 PM PT weekdays). "
                "Use --no-rth-block for testing only.",
                file=sys.stderr,
            )
            sys.exit(1)


# ── Prompt resolution (patch-gate mode) ──────────────────────────────────────
def _resolve_prompt(args: argparse.Namespace) -> str:
    """Return the audit prompt from CLI arg, file, or stdin."""
    if args.prompt:
        return args.prompt.strip()
    if args.prompt_file:
        p = Path(args.prompt_file)
        if not p.exists():
            print(f"ERROR: prompt file not found: {p}", file=sys.stderr)
            sys.exit(1)
        return p.read_text(encoding="utf-8").strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print(
        "ERROR: No prompt supplied. Use --prompt, --prompt-file, or stdin.",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Meta-audit helpers ────────────────────────────────────────────────────────
def _find_latest_audit_file(glob_pattern: str) -> Path | None:
    """Find today's audit file; fall back to the most recent available."""
    today = datetime.now(_PT).strftime("%Y-%m-%d")
    # Try today's file first (pattern must contain {date} placeholder)
    today_path = _LOGS_DIR / glob_pattern.replace("{date}", today)
    if today_path.exists() and today_path.stat().st_size > 0:
        return today_path
    # Fall back: find most recent matching file
    star_pattern = glob_pattern.replace("{date}", "*")
    candidates = sorted(_LOGS_DIR.glob(star_pattern), reverse=True)
    return next((c for c in candidates if c.stat().st_size > 0), None)


def _read_tail(path: Path, n_lines: int) -> str:
    """Return the last n_lines of a file. Empty string if missing or unreadable."""
    if not path.exists():
        return ""
    try:
        lines = path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        return "\n".join(lines[-n_lines:])
    except OSError:
        return ""


def _build_meta_audit_prompt() -> tuple[str, dict]:
    """Build the meta-audit cross-review prompt.

    Reads today's Gemini midday + nightly audit files, recent trade events
    (Slack notification proxy), and recent bot log lines.

    Returns (prompt_text, sources_info_dict).
    """
    midday_file = _find_latest_audit_file("midday_gemini_{date}.txt")
    nightly_file = _find_latest_audit_file("gemini_audit_{date}.txt")
    trade_events_path = _LOGS_DIR / "trade_events.jsonl"
    bot_log_path = _LOGS_DIR / "mtf_bot.log"

    sources: dict = {
        "midday_gemini": (
            str(midday_file) if midday_file else "NOT FOUND"
        ),
        "nightly_gemini": (
            str(nightly_file) if nightly_file else "NOT FOUND"
        ),
        "trade_events": (
            str(trade_events_path)
            if trade_events_path.exists()
            else "NOT FOUND"
        ),
        "bot_log": (
            str(bot_log_path) if bot_log_path.exists() else "NOT FOUND"
        ),
    }

    parts: list[str] = [
        "META-AUDIT CROSS-REVIEW — alpaca-mtf-bot trading system",
        "",
        "You are an independent auditor reviewing Gemini's automated audit "
        "reports for an Alpaca paper trading bot.",
        "The nightly and midday reports below were produced by Gemini "
        "(gemini-2.5-flash). Your job:",
        "",
        "1. CONFIRM findings you agree are critical — assign P0/P1/P2/P3",
        "2. CHALLENGE findings you disagree with or that lack evidence",
        "3. SURFACE issues Gemini missed based on the raw data below",
        "4. Final verdict: PASS (no action) / WARN (monitor) / "
        "FAIL (immediate fix required)",
        "",
        "IMPORTANT — Slack notifications proxy:",
        "  Every entry, exit, stop-hit, and MRI change in trade_events.jsonl "
        "below corresponds to a Slack alert that was sent to the operator. "
        "Evaluate whether those alerts represent appropriate bot behavior.",
        "",
    ]

    # ── Midday Gemini report ──────────────────────────────────────────────
    if midday_file:
        parts += [
            f"=== MIDDAY GEMINI AUDIT ({midday_file.name}) ===",
            midday_file.read_text(encoding="utf-8", errors="replace").strip(),
            "",
        ]
    else:
        parts += ["=== MIDDAY GEMINI AUDIT: NOT AVAILABLE ===", ""]

    # ── Nightly Gemini report ─────────────────────────────────────────────
    if nightly_file:
        parts += [
            f"=== NIGHTLY GEMINI AUDIT ({nightly_file.name}) ===",
            nightly_file.read_text(
                encoding="utf-8", errors="replace"
            ).strip(),
            "",
        ]
    else:
        parts += ["=== NIGHTLY GEMINI AUDIT: NOT AVAILABLE ===", ""]

    # ── Trade events (Slack notification proxy) ───────────────────────────
    trade_tail = _read_tail(trade_events_path, _TRADE_EVENTS_TAIL)
    if trade_tail:
        parts += [
            f"=== RECENT TRADE EVENTS / SLACK NOTIFICATION PROXY"
            f" (last {_TRADE_EVENTS_TAIL} from trade_events.jsonl) ===",
            trade_tail,
            "",
        ]
    else:
        parts += [
            "=== TRADE EVENTS: NOT AVAILABLE ===",
            "",
        ]

    # ── Bot log tail ──────────────────────────────────────────────────────
    bot_tail = _read_tail(bot_log_path, _BOT_LOG_TAIL_LINES)
    if bot_tail:
        parts += [
            f"=== RECENT BOT LOG (last {_BOT_LOG_TAIL_LINES} lines"
            f" from mtf_bot.log) ===",
            bot_tail,
            "",
        ]
    else:
        parts += ["=== BOT LOG: NOT AVAILABLE ===", ""]

    return "\n".join(parts), sources


# ── Slack post (meta-audit results) ──────────────────────────────────────────
def _post_slack_summary(
    ds_result: dict,
    gai_result: dict,
    out_path: Path,
    mode_label: str = "meta-audit",
) -> None:
    """Post audit verdict summary to Slack via incoming webhook."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        print(
            "[auto_ai_audit] No SLACK_WEBHOOK_URL — skipping Slack post",
            file=sys.stderr,
        )
        return

    import requests  # type: ignore[import-untyped]

    ds_ok = ds_result["error"] is None
    gai_ok = gai_result["error"] is None
    now_pt = datetime.now(_PT)
    ts = now_pt.strftime("%Y-%m-%d %I:%M %p PT")

    # Build short excerpts (first 350 chars of each response)
    def _excerpt(result: dict, label: str) -> str:
        if not result["text"]:
            return f"*{label}:* ❌ FAILED — {result['error']}"
        preview = result["text"][:350].replace("\n", " ").strip()
        return f"*{label} (preview):* {preview}…"

    text = (
        f":robot_face: *Auto AI {mode_label.title()} — {ts}*\n"
        f"DS: {'✅' if ds_ok else '❌'}  |  "
        f"GAI: {'✅' if gai_ok else '❌'}\n\n"
        f"{_excerpt(ds_result, 'DeepSeek')}\n\n"
        f"{_excerpt(gai_result, 'Gemini')}\n\n"
        f"Full report: `{out_path.name}`"
    )

    try:
        resp = requests.post(
            webhook,
            json={"text": text},
            timeout=10,
        )
        resp.raise_for_status()
        print("[auto_ai_audit] ✅ Slack summary posted")
    except Exception as exc:  # noqa: BLE001
        print(
            f"[auto_ai_audit] ⚠️  Slack post failed: {exc}",
            file=sys.stderr,
        )


# ── DeepSeek call (OpenAI-compatible REST, no SDK required) ──────────────────
def _call_deepseek(prompt: str) -> dict:
    """Submit prompt to DeepSeek API.

    Returns {text, model, tokens, elapsed_s, error}.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return {
            "text": None,
            "model": _DEEPSEEK_MODEL,
            "tokens": None,
            "elapsed_s": 0,
            "error": "DEEPSEEK_API_KEY not set in environment",
        }

    import requests  # type: ignore[import-untyped]  # always available

    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{_DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": _DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=_API_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "text": text,
            "model": _DEEPSEEK_MODEL,
            "tokens": usage.get("total_tokens"),
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "text": None,
            "model": _DEEPSEEK_MODEL,
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }


# ── Gemini call (google.genai SDK — replaces deprecated google.generativeai) ──
def _call_gemini(prompt: str) -> dict:
    """Submit prompt to Google Gemini API via google.genai SDK.

    Uses gemini-3.1-pro-preview to match the model selected in Google AI
    Studio. google.generativeai is deprecated; google.genai is the current SDK.
    Falls back to REST if the SDK is unavailable.

    Returns {text, model, tokens, elapsed_s, error}.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {
            "text": None,
            "model": _GEMINI_MODEL,
            "tokens": None,
            "elapsed_s": 0,
            "error": "GEMINI_API_KEY not set in environment",
        }

    t0 = time.monotonic()
    try:
        from google import genai  # type: ignore[import-untyped]
        from google.genai import types  # type: ignore[import-untyped]

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1),
        )
        text = response.text if hasattr(response, "text") else str(response)
        usage = getattr(response, "usage_metadata", None)
        tokens = (
            getattr(usage, "total_token_count", None) if usage else None
        )
        return {
            "text": text,
            "model": _GEMINI_MODEL,
            "tokens": tokens,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except ImportError:
        return _call_gemini_rest(prompt, api_key, t0)
    except Exception as exc:  # noqa: BLE001
        return {
            "text": None,
            "model": _GEMINI_MODEL,
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }


def _call_gemini_rest(prompt: str, api_key: str, t0: float) -> dict:
    """Gemini via REST — fallback if google.genai library is unavailable."""
    import requests  # type: ignore[import-untyped]

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{_GEMINI_MODEL}:generateContent?key={api_key}"
        )
        resp = requests.post(
            url,
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1},
            },
            timeout=_API_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return {
            "text": text,
            "model": f"{_GEMINI_MODEL}-rest",
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "text": None,
            "model": f"{_GEMINI_MODEL}-rest",
            "tokens": None,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "error": str(exc),
        }


# ── Atomic write (RC-5 compliance) ───────────────────────────────────────────
def _push_to_gist(data: dict) -> None:
    """Push meta_audit_latest.json to GitHub Gist so board CCR can fetch it."""
    import urllib.request  # stdlib only — no requests dependency here
    token = os.environ.get("GITHUB_GIST_TOKEN", "")
    if not token:
        print(
            "[auto_ai_audit] ⚠️  GITHUB_GIST_TOKEN not set — skipping Gist push",
            file=sys.stderr,
        )
        return
    payload = json.dumps({
        "files": {
            "meta_audit_latest.json": {
                "content": json.dumps(data, indent=2, ensure_ascii=False),
            }
        }
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{_GIST_ID}",
        data=payload,
        method="PATCH",
    )
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                print(f"[auto_ai_audit] 📤 Gist updated: {_GIST_RAW_URL}")
            else:
                print(
                    f"[auto_ai_audit] ⚠️  Gist push returned HTTP {resp.status}",
                    file=sys.stderr,
                )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[auto_ai_audit] ⚠️  Gist push failed: {exc}",
            file=sys.stderr,
        )


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically via tmp→replace (no partial writes on crash)."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


# ── Shared API submission + output logic ──────────────────────────────────────
def _run_audit(
    prompt: str,
    out_path: Path,
    mode_label: str,
    post_slack: bool = False,
) -> tuple[dict, dict]:
    """Submit prompt to DS + Gemini, write JSON, print responses.

    Returns (ds_result, gai_result).
    """
    now_pt = datetime.now(_PT)
    ts_display = now_pt.strftime("%Y-%m-%d %I:%M %p PT")

    print(f"[auto_ai_audit] [{mode_label}] — {ts_display}")
    print(f"[auto_ai_audit] Prompt: {len(prompt):,} chars")

    # ── DeepSeek ─────────────────────────────────────────────────────────
    print("[auto_ai_audit] ⏳ Calling DeepSeek ...")
    ds_result = _call_deepseek(prompt)
    if ds_result["error"]:
        print(
            f"[auto_ai_audit] ⚠️  DeepSeek FAILED "
            f"({ds_result['elapsed_s']}s): {ds_result['error']}",
            file=sys.stderr,
        )
    else:
        print(
            f"[auto_ai_audit] ✅ DeepSeek OK "
            f"({ds_result['elapsed_s']}s, {ds_result['tokens']} tokens)"
        )

    # ── Gemini ───────────────────────────────────────────────────────────
    print("[auto_ai_audit] ⏳ Calling Gemini ...")
    gai_result = _call_gemini(prompt)
    if gai_result["error"]:
        print(
            f"[auto_ai_audit] ⚠️  Gemini FAILED "
            f"({gai_result['elapsed_s']}s): {gai_result['error']}",
            file=sys.stderr,
        )
    else:
        print(
            f"[auto_ai_audit] ✅ Gemini OK "
            f"({gai_result['elapsed_s']}s, {gai_result['tokens']} tokens)"
        )

    # ── Build output dict ─────────────────────────────────────────────────
    ds_ok = ds_result["error"] is None
    gai_ok = gai_result["error"] is None

    output = {
        "schema_version": "1.0",
        "mode": mode_label,
        "ts_pt": ts_display,
        "ts_iso": now_pt.isoformat(),
        "prompt_chars": len(prompt),
        "prompt_preview": prompt[:300] + ("…" if len(prompt) > 300 else ""),
        "deepseek": ds_result,
        "gemini": gai_result,
        "summary": {
            "ds_ok": ds_ok,
            "gai_ok": gai_ok,
            "both_ok": ds_ok and gai_ok,
            "partial": ds_ok != gai_ok,
            "both_failed": not ds_ok and not gai_ok,
        },
    }

    _atomic_write_json(out_path, output)
    print(f"[auto_ai_audit] 📄 JSON written: {out_path.name}")

    # In meta-audit mode, also write latest pointer to /var/www/mtf-bot/
    # so the board CCR can fetch it via nginx at /meta_audit_latest.json
    if mode_label == "meta-audit":
        www_path = Path("/var/www/mtf-bot/meta_audit_latest.json")
        try:
            www_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_www = www_path.with_suffix(".tmp")
            tmp_www.write_text(
                json.dumps(output, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_www.replace(www_path)
            print("[auto_ai_audit] 📄 Board endpoint updated: /meta_audit_latest.json")
        except OSError as exc:
            print(
                f"[auto_ai_audit] ⚠️  Could not write board endpoint: {exc}",
                file=sys.stderr,
            )

        # Push to GitHub Gist so board CCR can fetch without IP allowlist issues
        _push_to_gist(output)

    # ── Print raw responses ───────────────────────────────────────────────
    print()
    print("=" * 72)
    print("DEEPSEEK RESPONSE:")
    print("=" * 72)
    if ds_result["text"]:
        print(ds_result["text"])
    else:
        print(f"[FAILED — {ds_result['error']}]")

    print()
    print("=" * 72)
    print("GEMINI RESPONSE:")
    print("=" * 72)
    if gai_result["text"]:
        print(gai_result["text"])
    else:
        print(f"[FAILED — {gai_result['error']}]")

    print()
    print(f"[auto_ai_audit] Done. Full JSON: {out_path}")

    if post_slack:
        _post_slack_summary(ds_result, gai_result, out_path, mode_label)

    return ds_result, gai_result


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Automated DS/GAI audit gate (patch-gate + meta-audit modes). "
            "Submits identical prompts to DeepSeek + Gemini."
        )
    )
    # ── Patch-gate args ───────────────────────────────────────────────────
    parser.add_argument("--prompt", help="Audit prompt as a string")
    parser.add_argument(
        "--prompt-file", help="Path to a file containing the audit prompt"
    )
    # ── Meta-audit args ───────────────────────────────────────────────────
    parser.add_argument(
        "--meta-audit",
        action="store_true",
        help=(
            "Auto-build cross-review prompt from today's Gemini audit "
            "reports + trade events + bot log. Posts Slack summary."
        ),
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="Suppress Slack post in --meta-audit mode",
    )
    # ── Shared args ───────────────────────────────────────────────────────
    parser.add_argument(
        "--no-rth-block",
        action="store_true",
        help="Bypass RTH block — TESTING ONLY",
    )
    args = parser.parse_args()

    if not args.no_rth_block:
        _check_rth_block()

    now_pt = datetime.now(_PT)
    ts_file = now_pt.strftime("%Y%m%d_%H%M%S")

    # ── Meta-audit mode ───────────────────────────────────────────────────
    if args.meta_audit:
        print("[auto_ai_audit] 📊 Building meta-audit prompt ...")
        prompt, sources = _build_meta_audit_prompt()
        print("[auto_ai_audit] Sources loaded:")
        for k, v in sources.items():
            label = "✅" if "NOT FOUND" not in v else "⚠️ "
            short = Path(v).name if "NOT FOUND" not in v else v
            print(f"  {label} {k}: {short}")
        if not prompt.strip():
            print(
                "ERROR: Meta-audit prompt is empty — no audit files found.",
                file=sys.stderr,
            )
            sys.exit(2)
        out_path = _LOGS_DIR / f"ai_audit_meta_{ts_file}_PT.json"
        ds_result, gai_result = _run_audit(
            prompt,
            out_path,
            mode_label="meta-audit",
            post_slack=not args.no_slack,
        )
    else:
        # ── Patch-gate mode ───────────────────────────────────────────────
        prompt = _resolve_prompt(args)
        if not prompt:
            print("ERROR: Empty prompt.", file=sys.stderr)
            sys.exit(1)
        out_path = _LOGS_DIR / f"ai_audit_{ts_file}_PT.json"
        ds_result, gai_result = _run_audit(
            prompt,
            out_path,
            mode_label="patch-gate",
            post_slack=False,  # patch-gate: Claude reads stdout, no Slack
        )

    # ── Exit code ─────────────────────────────────────────────────────────
    ds_ok = ds_result["error"] is None
    gai_ok = gai_result["error"] is None
    if not ds_ok and not gai_ok:
        sys.exit(2)
    elif not ds_ok or not gai_ok:
        sys.exit(1)
    # exit 0 implied


if __name__ == "__main__":
    main()
