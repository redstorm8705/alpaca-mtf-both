#!/usr/bin/env python3
# ruff: noqa: E501  (assertion messages + inline stubs exceed 88; E501 is cosmetic here).
"""Offline unit tests for gai_health — all classification branches, no network.

Run: python3 .claude/preship/test_gai_health.py   (exit 0 = all pass)
Also serves as the anti-inert fixture for the canary gate: if the classification logic is broken,
these fail deterministically without touching the network.
"""
import sys
import gai_health as gh

_REAL_PROBE = gh._probe          # capture BEFORE the canary tests replace gh._probe with stubs
_REAL_LIST = gh._list_models

FAILS = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        FAILS.append(name)


def with_stub(serve_map, service_up, names):
    """Install stubs: serve_map maps model -> text|None; _list_models -> (service_up, names)."""
    gh._generate_text = lambda model, key, prompt="x", timeout=30: serve_map.get(model)
    gh._list_models = lambda key, timeout=25: (service_up, set(names))


# 1. pinned serves -> use pinned, no note
with_stub({gh.PINNED_MODEL: "OK"}, True, gh.CANDIDATE_MODELS)
m, note = gh.pick_working_gemini("k", log=lambda _m: None)
check("pinned-serves -> pinned, empty note", m == gh.PINNED_MODEL and note == "")

# 2. pinned fails, model-list unreachable -> GeminiOutage
with_stub({gh.PINNED_MODEL: None}, False, set())
try:
    gh.pick_working_gemini("k", log=lambda _m: None)
    check("model-list-down -> GeminiOutage", False)
except gh.GeminiOutage:
    check("model-list-down -> GeminiOutage", True)
except Exception:
    check("model-list-down -> GeminiOutage", False)

# 3a. pinned congested (present in list, fails), an alternate serves -> self-heal + note
alt = "gemini-3.5-flash-lite"
with_stub({gh.PINNED_MODEL: None, alt: "OK"}, True, [gh.PINNED_MODEL, alt])
m, note = gh.pick_working_gemini("k", log=lambda _m: None)
check("pinned-congested + alt-serves -> alternate", m == alt)
check("congested self-heal emits re-pin note", "RE-PIN NEEDED" in note and "congested" in note)

# 3b. pinned RETIRED (absent from list), an alternate serves -> self-heal + 'RETIRED' note
with_stub({gh.PINNED_MODEL: None, alt: "OK"}, True, [alt])
m, note = gh.pick_working_gemini("k", log=lambda _m: None)
check("pinned-retired + alt-serves -> alternate", m == alt)
check("retired self-heal note says RETIRED", "RETIRED" in note)

# 4. pinned RETIRED, no candidate serves -> GeminiMisconfig (NOT outage, NOT substitute)
with_stub({gh.PINNED_MODEL: None, alt: None}, True, [alt])
try:
    gh.pick_working_gemini("k", log=lambda _m: None)
    check("retired+none-serve -> GeminiMisconfig", False)
except gh.GeminiMisconfig:
    check("retired+none-serve -> GeminiMisconfig", True)
except Exception as e:
    check("retired+none-serve -> GeminiMisconfig (got %s)" % type(e).__name__, False)

# 5. pinned present but nothing serves -> GeminiOutage (Gemini-wide)
with_stub({gh.PINNED_MODEL: None, alt: None}, True, [gh.PINNED_MODEL, alt])
try:
    gh.pick_working_gemini("k", log=lambda _m: None)
    check("present+none-serve -> GeminiOutage", False)
except gh.GeminiOutage:
    check("present+none-serve -> GeminiOutage", True)
except Exception:
    check("present+none-serve -> GeminiOutage", False)

def NOSLEEP(_n):
    return None  # tests must not actually wait through the retry backoff


def stub_probe(probe_fn, service_up=True, names=None):
    """Install a (code, text) _probe stub + a model-list stub for canary tests."""
    gh._probe = probe_fn
    gh._list_models = lambda key, timeout=25: (
        service_up, set(gh.CANDIDATE_MODELS if names is None else names))


# 6. canary HEALTHY: pinned serves 200 + exactly one VERDICT line
stub_probe(lambda m, k, p="x", t=30: (200, "reasoning\nVERDICT: APPROVE") if m == gh.PINNED_MODEL else (503, None))
st, d = gh.canary("k", sleep=NOSLEEP)
check("canary HEALTHY", st == gh.HEALTHY)

# 7. canary CONFIG_BUG: pinned RETIRED (absent from model-list) -> blocks, immediate
stub_probe(lambda m, k, p="x", t=30: (200, "VERDICT: APPROVE"), names=["gemini-3.5-flash-lite"])
st, d = gh.canary("k", sleep=NOSLEEP)
check("canary retired-pin -> CONFIG_BUG", st == gh.CONFIG_BUG and "RETIRED" in d)

# 8. canary CONFIG_BUG: 200 but NO text always (thinking-budget class) -> blocks after retries
stub_probe(lambda m, k, p="x", t=30: (200, None))
st, d = gh.canary("k", sleep=NOSLEEP)
check("canary 200-no-text -> CONFIG_BUG", st == gh.CONFIG_BUG and "thinking-budget" in d)

# 9. canary CONFIG_BUG: 200 with 2 VERDICT lines (unparseable) -> blocks
stub_probe(lambda m, k, p="x", t=30: (200, "VERDICT: A\nVERDICT: B"))
st, d = gh.canary("k", sleep=NOSLEEP)
check("canary 2-verdict -> CONFIG_BUG", st == gh.CONFIG_BUG)

# 10. canary OUTAGE: all models 503, pin still listed -> does NOT block (substitute covers)
stub_probe(lambda m, k, p="x", t=30: (503, None))
st, d = gh.canary("k", sleep=NOSLEEP)
check("canary all-503 -> OUTAGE (not blocked)", st == gh.OUTAGE)

# 11. canary TRANSIENT: 503 on attempt 1, 200+verdict on attempt 2 -> HEALTHY (no false-block)
_n = {"i": 0}
def _trans(m, k, p="x", t=30):
    if m != gh.PINNED_MODEL:
        return (503, None)
    _n["i"] += 1
    return (503, None) if _n["i"] == 1 else (200, "VERDICT: APPROVE")
stub_probe(_trans)
st, d = gh.canary("k", sleep=NOSLEEP)
check("canary transient-then-recover -> HEALTHY (no false-block)", st == gh.HEALTHY)

# 12. canary no-key -> CONFIG_BUG, no network
st, d = gh.canary("", sleep=NOSLEEP)
check("canary no-key -> CONFIG_BUG", st == gh.CONFIG_BUG and "no GEMINI_API_KEY" in d)

# 13. _probe must NOT crash on malformed 200 shapes (a crash would exit non-zero -> false-block CI)
gh._probe = _REAL_PROBE          # restore the real _probe (canary tests above stubbed it)
_MALFORMED = [
    {},                                                # no candidates key
    {"candidates": []},                                # empty candidates array
    {"candidates": [None]},                            # null candidate
    {"candidates": [{"content": None}]},               # null content
    {"candidates": [{"content": {"parts": None}}]},    # null parts
    {"candidates": [{"content": {"parts": []}}]},      # empty parts
    {"candidates": [{"content": {"parts": [None]}}]},  # null part element
]
for bad in _MALFORMED:
    gh._curl_json = lambda url, body=None, timeout=30, _b=bad: (200, _b)
    try:
        code, text = gh._probe("m", "k")
        check(f"_probe malformed {str(bad)[:34]} -> (200,None), no crash", code == 200 and text is None)
    except Exception as e:
        check(f"_probe malformed {str(bad)[:34]} CRASHED: {type(e).__name__}", False)

# 14. _probe happy path still returns the text
gh._curl_json = lambda url, body=None, timeout=30: (200, {"candidates": [{"content": {"parts": [{"text": "VERDICT: APPROVE"}]}}]})
code, text = gh._probe("m", "k")
check("_probe good 200 -> returns text", code == 200 and text == "VERDICT: APPROVE")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("ALL gai_health TESTS PASS")
