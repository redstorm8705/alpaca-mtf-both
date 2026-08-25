#!/usr/bin/env python3
# ruff: noqa: E501  (long rationale comments + Gemini API URLs exceed 88; E501 is cosmetic here).
"""GAI health: a self-healing Gemini model selector + a verdict canary.

WHY THIS EXISTS (2026-08-25 failure class). A dead/congested Gemini MODEL ID (`gemini-flash-latest`
routing to an overloaded pool; `gemini-2.5-flash` silently retired -> 404) was mistaken for a Gemini
OUTAGE and silently routed to the free NVIDIA substitute for hours. Three project rules were broken
at once:
  * NO-GUESS               — a 503 was ASSUMED to mean "GAI is down" instead of measured.
  * VERIFY-AT-SOURCE       — one model-list call proves the service is UP and refutes "GAI is down".
  * NEVER-MASK-A-FAULT     — the substitute (a resilience control) hid a config bug instead of surfacing it.

This module makes "bad model ID" mechanically distinguishable from "Gemini outage", so the substitute
can ONLY ever cover a proven service-wide outage — a dead/congested model self-heals to a working
Gemini model, LOUDLY, or raises a clear, correctly-typed error.

No third-party deps: curl subprocess (macOS urllib hits SSL CERTIFICATE_VERIFY_FAILED), stdlib only.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

# Pinned reviewer model (kept in lockstep with preship_audit.py / ci_audit.py). NOT a `-latest`
# alias — those route to shifting/overloaded pools we do not control (the root cause).
PINNED_MODEL = "gemini-3.5-flash"

# Auto-fall-forward order when the pinned model is retired/congested. Lite variants are the least
# congested (verified 2026-08-25). The list is data, not a moving alias.
CANDIDATE_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]

_BASE = "https://generativelanguage.googleapis.com/v1beta"
# thinkingBudget:0 is MANDATORY — gemini-3.5-flash is a thinking model; without it the hidden
# reasoning eats the whole token budget and NO verdict text is emitted (root cause 2).
_GEN_CFG = {"maxOutputTokens": 2048, "thinkingConfig": {"thinkingBudget": 0}}

CANARY_FIXTURE = (
    "Audit this one-line diff for defects. Reply in at most one sentence, then a final line "
    "exactly 'VERDICT: APPROVE'.\n\n+    _x = 1  # a constant assignment"
)


class GeminiOutage(Exception):
    """Gemini is proven service-wide unavailable (model-list unreachable, or NO candidate model
    serves). This is the ONLY condition under which a caller may engage the substitute."""


class GeminiMisconfig(Exception):
    """The service is UP but the pinned model is retired AND no candidate served — a CONFIG bug,
    never an outage. Must fail LOUD and must NOT route to the substitute."""


def _stderr(msg):
    sys.stderr.write(msg + "\n")


def _curl_json(url, body=None, timeout=30):
    """POST `body` (or GET when body is None); return (http_code|None, parsed_dict|None)."""
    cmd = ["curl", "-s", "-w", "\n__H__%{http_code}", "--max-time", str(timeout), url,
           "-H", "Content-Type: application/json"]
    path = None
    if body is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(body, f)
            path = f.name
        cmd += ["--data-binary", f"@{path}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5).stdout
    finally:
        if path:
            os.unlink(path)
    code = None
    if "__H__" in out:
        head, _, tail = out.rpartition("__H__")
        tail = tail.strip()
        code = int(tail) if tail.isdigit() else None
        out = head
    try:
        return code, json.loads(out)
    except Exception:
        return code, None


def _probe(model, key, prompt="Reply with exactly: OK", timeout=30):
    """Return (http_code|None, text|None) for a generateContent call.

    The (code, text) split is what separates a CONFIG BUG from an OUTAGE:
      * code==200, text is a string  -> served normally.
      * code==200, text is None      -> served but produced NO text (thinking-budget ate the
                                        tokens / MAX_TOKENS on empty) — a CONFIG signal, NOT an outage.
      * code != 200 (503/429/etc.)   -> did not serve — transient congestion / outage.
    """
    code, r = _curl_json(
        f"{_BASE}/models/{model}:generateContent?key={key}",
        {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": _GEN_CFG},
        timeout,
    )
    # Defensive against malformed/edge 200 shapes (empty `candidates: []`, null `content`, null
    # `parts`) — a crash here would exit non-zero and FALSE-BLOCK CI, the exact thing this gate
    # exists to prevent. Any 200 without usable text yields (200, None) -> the CONFIG_BUG bucket.
    if code == 200 and isinstance(r, dict) and r.get("candidates"):
        cand = r["candidates"][0] or {}
        parts = (cand.get("content") or {}).get("parts") or []
        return code, (parts[0].get("text", "") if parts and isinstance(parts[0], dict) else None)
    return code, None


def _generate_text(model, key, prompt="Reply with exactly: OK", timeout=30):
    """Return the response text if `model` serves a 200-with-content call right now, else None."""
    return _probe(model, key, prompt, timeout)[1]


def _list_models(key, timeout=25):
    """Return (service_up, {model names supporting generateContent}). service_up=False => the
    model-list endpoint itself is unreachable (network/auth/service outage)."""
    code, r = _curl_json(f"{_BASE}/models?key={key}&pageSize=200", None, timeout)
    if code == 200 and isinstance(r, dict) and isinstance(r.get("models"), list):
        names = {m["name"].split("/")[-1] for m in r["models"]
                 if isinstance(m, dict) and m.get("name")
                 and "generateContent" in (m.get("supportedGenerationMethods") or [])}
        return True, names
    return False, set()


def pick_working_gemini(key, log=_stderr):
    """Return (model_name, note) for a Gemini model that actually serves NOW — self-healing.

    This is the gate that stops a dead/congested model from masquerading as an outage:

      1. pinned model serves                     -> (PINNED_MODEL, "")            [common path]
      2. pinned fails, model-list UNREACHABLE     -> raise GeminiOutage           [real outage]
      3. pinned fails, service UP, an alternate
         candidate serves                         -> (alternate, LOUD re-pin note) [self-heal]
      4. pinned fails, service UP, pinned ABSENT
         from list, no candidate serves           -> raise GeminiMisconfig        [config bug]
      5. pinned fails, service UP, nothing serves  -> raise GeminiOutage           [Gemini-wide]

    A caller may fall back to the substitute ONLY on GeminiOutage — never on GeminiMisconfig.
    """
    if _generate_text(PINNED_MODEL, key) is not None:
        return PINNED_MODEL, ""

    service_up, names = _list_models(key)
    if not service_up:
        raise GeminiOutage("Gemini model-list unreachable — service/auth/network outage.")

    pinned_present = PINNED_MODEL in names
    for m in CANDIDATE_MODELS:
        if m == PINNED_MODEL:
            continue
        if m in names and _generate_text(m, key) is not None:
            why = "congested" if pinned_present else "RETIRED (absent from model-list)"
            note = (f"RE-PIN NEEDED: pinned '{PINNED_MODEL}' is {why}; this audit served via '{m}'. "
                    f"Update PINNED_MODEL in gai_health.py / preship_audit.py / ci_audit.py.")
            log(f"[gai_health] {note}")
            return m, note

    if not pinned_present:
        raise GeminiMisconfig(
            f"Service UP but pinned '{PINNED_MODEL}' is RETIRED (absent from the model-list) and no "
            f"candidate model served. Fix PINNED_MODEL — this is a CONFIG BUG, not an outage; the "
            f"substitute is refused.")
    raise GeminiOutage(
        "Service UP but no Gemini candidate model served generateContent — treat as a Gemini-wide "
        "outage (the substitute may stand in).")


def _parse_verdict_count(txt):
    """Count anchored 'VERDICT:' lines (mirrors preship_audit._verdict anchoring)."""
    return [ln for ln in txt.splitlines()
            if ln.strip().lstrip("*#-+>• ").upper().startswith("VERDICT:")]


# canary() status values. The GATE blocks ONLY on CONFIG_BUG.
HEALTHY = "HEALTHY"
CONFIG_BUG = "CONFIG_BUG"
OUTAGE = "OUTAGE"


def canary(key, attempts=3, backoff=5, sleep=time.sleep):
    """Anti-inert self-test. Returns (status, detail):

      HEALTHY    - some model returned a PARSEABLE verdict (GAI usable now).
      CONFIG_BUG - a DETERMINISTIC misconfig: the pinned model is RETIRED (absent from the
                   model-list), OR a model served HTTP 200 but produced no parseable verdict across
                   every attempt (thinking-budget ate the tokens / format broken). **The gate BLOCKS
                   on this** — it is the 2026-08-25 failure class, and it never clears on retry.
      OUTAGE     - no model served (all non-200) while the pin is still listed: transient congestion
                   / Gemini-wide outage. The SUBSTITUTE covers this; **the gate does NOT block**, or
                   free-tier congestion would false-block every ship.

    The retired-pin check uses the model-list (metadata — congestion-independent), so a dead pin is
    caught as CONFIG_BUG even while everything is congested. 200-but-no-verdict is retried (to rule
    out a transient empty) and only declared CONFIG_BUG if it never yields a verdict.
    """
    if not key:
        return CONFIG_BUG, "canary: no GEMINI_API_KEY available."
    service_up, names = _list_models(key)
    if service_up and PINNED_MODEL not in names:
        return CONFIG_BUG, (f"canary: pinned '{PINNED_MODEL}' is RETIRED (absent from the model-list). "
                            f"Re-pin PINNED_MODEL — this is a config bug, not an outage.")
    probe_models = [PINNED_MODEL] + [m for m in CANDIDATE_MODELS if m != PINNED_MODEL]
    served_200_no_verdict = False
    for i in range(attempts):
        for m in probe_models:
            if service_up and m not in names:
                continue
            code, text = _probe(m, key, CANARY_FIXTURE)
            if code == 200 and text is not None:
                if len(_parse_verdict_count(text)) == 1:
                    heal = "" if m == PINNED_MODEL else f" (self-healed via '{m}' — RE-PIN recommended)"
                    return HEALTHY, f"canary OK via '{m}'{heal}"
                served_200_no_verdict = True   # 200 with content but not a single parseable verdict
            elif code == 200:
                served_200_no_verdict = True   # 200 with NO text => thinking-budget class
        if i < attempts - 1:
            sleep(backoff)
    if served_200_no_verdict:
        return CONFIG_BUG, ("canary: a Gemini model returned HTTP 200 but no parseable verdict across "
                            f"{attempts} attempts — thinking-budget/format misconfig (deterministic).")
    return OUTAGE, (f"canary: no Gemini model served (all non-200) across {attempts} attempts — "
                    "transient congestion / outage. The substitute covers this; gate not blocked.")


def main(argv):
    """CLI: `gai_health.py canary` -> exit 0 if GAI can return a parseable verdict, else 1 (loud)."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        # Read from a sibling .env without exporting (mirrors preship_audit._load_env intent).
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), ".env")
        try:
            with open(env_path) as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY=") and "=" in line:
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    cmd = argv[1] if len(argv) > 1 else "canary"
    if cmd == "canary":
        status, detail = canary(key)
        print(f"{status}: {detail}")
        # The gate blocks ONLY on a deterministic CONFIG_BUG. OUTAGE (transient congestion / a real
        # Gemini outage) and HEALTHY both pass — the substitute covers a real outage, so free-tier
        # congestion can never false-block a ship.
        return 1 if status == CONFIG_BUG else 0
    if cmd == "pick":
        try:
            model, note = pick_working_gemini(key)
            print(f"model={model} note={note or '(pinned, healthy)'}")
            return 0
        except (GeminiOutage, GeminiMisconfig) as e:
            print(f"{type(e).__name__}: {e}")
            return 1
    print("usage: gai_health.py [canary|pick]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
