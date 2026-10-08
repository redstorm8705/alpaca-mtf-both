#!/usr/bin/env python3
# ruff: noqa: E501  — dense pattern strings run long (project convention)
"""
no_assume_gate.py — a Stop hook that mechanically blocks an ASSUMED-ABSENCE claim: asserting the
codebase/bot LACKS a capability, tool, data feed, or piece of work WITHOUT having searched for it.

WHY (Rafael 2026-08-29, DOCUMENTATION-IS-NOT-ENFORCEMENT): the agent repeatedly stated as FACT that
work did not exist — "we don't ingest order-flow/gamma" (false: data/gex.py is 905 lines of gamma
exposure), and earlier missed the edge-tracker and monitoring/watchdog the same way — each time
WITHOUT grepping the repo first. A memory note failed to stop it across multiple sessions, so per the
project's own rule the fix is a GATE, not another note. This is the sibling of no_guess_gate.py: that
one gates comparative/causal claims; this one gates existence/absence claims about the system.

MECHANISM: identical stdin/transcript contract to no_guess_gate.py and execute_dont_ask_gate.py — a
Stop hook reads the turn-ending message; exit 2 (reason on stderr) BLOCKS the stop and forces a redo;
`stop_hook_active` fires it at most once per turn. It BLOCKS when the message asserts the system LACKS
a named capability/tool/data (an ABSENCE claim about a CAPABILITY term) AND carries NEITHER
(a) SEARCH evidence (grep/find/glob/semantic-search ran, "no matches", a file:line/path cited) NOR
(b) an honest HEDGE ("I haven't searched", "let me check", "may already exist"). The fix is always
in-turn: grep the repo, then cite what you found (or found nothing).

FAIL-OPEN on any parse error / missing transcript / empty message. HONEST LIMIT (per bias_gate/
no_guess_gate precedent): pattern-based => necessary-not-sufficient; it catches explicit absence
LANGUAGE about capabilities, not an assumption phrased to dodge the patterns. The structural backstops
remain (adversarial self-review, cold-2nd, the board, Rafael).
"""
import json
import os
import re
import sys


def _last_assistant_text(transcript_path: str) -> str:
    # Same extraction shape as no_guess_gate / execute_dont_ask_gate (single source of shape).
    try:
        p = os.path.expanduser(transcript_path)
        if not os.path.exists(p):
            return ""
        lines = open(p, encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        return ""
    for ln in reversed(lines):
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant" or ev.get("role") == "assistant":
            msg = ev.get("message", ev)
            if not isinstance(msg, dict):
                continue
            content = msg.get("content", msg)
            txt = ""
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                txt = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            if txt and txt.strip():
                return txt
    return ""


# A CAPABILITY / TOOL / DATA / WORK term — the kind whose ABSENCE must be searched before asserted.
_CAP = (r"(order[- ]?flow|imbalance|tape|level[- ]?2|\bl2\b|cvd|footprint|bid[- ]?ask|microstructure|"
        r"gamma|gex|greeks?|dealer|vanna|charm|option\w*|\biv\b|implied[- ]vol\w*|0dte|open[- ]interest|"
        r"data|feed|signal|indicator|tool\w*|module|script|infra\w*|pipeline|logic|support|"
        r"track\w*|monitor\w*|ingest\w*|integration|capabilit\w*|framework|handler|backtest\w*|"
        r"harness|report\w*|ledger|metric\w*|snapshot|history|coverage|detector|engine|gate|"
        r"watchdog|telemetry|tca|scanner|multi[- ]?time\w*|timeframe|regime|analytics?|"
        r"heartbeat|sizing|scoring|confluence|kelly|hedge|attribution)")

# Absence via a system-capability VERB (negating these verbs is almost always a capability claim).
_ABSENCE_VERB = re.compile(
    r"\b(we|it|the bot|the system|this)\s+(do(es)? not|don'?t|doesn'?t|never|aren'?t|are not|isn'?t|is not|"
    r"haven'?t|have not|hasn'?t|has not|won'?t|cannot|can'?t)"
    r"(\s+(currently|yet|today|really|even))?\s+"
    r"(ingest\w*|compute\w*|track\w*|wire\w*|instrument\w*|expose\w*|collect\w*|capture\w*|"
    r"log\w*|store\w*|support\w*|implement\w*|maintain\w*|provide\w*|handle\w*|record\w*|"
    r"surface\w*|pull\w*)\b",
    re.I,
)
# Absence via existence phrasing, requiring a CAPABILITY term nearby (so "there's no rush" is ignored).
_ABSENCE_EXIST = re.compile(
    r"\b(there(?:'?s| is| are)? no|we (have no|lack|'?re lacking|are lacking)|"
    r"the bot (has no|doesn'?t have|lacks)|the system (has no|doesn'?t have|lacks))"
    r"\b[\s\w,'\"()-]{0,40}\b" + _CAP + r"\b"
    r"|\b" + _CAP + r"\b[\s\w,'\"()-]{0,30}\b(doesn'?t exist|does not exist|do not exist|don'?t exist|"
    r"isn'?t (built|implemented|wired|available|present|a thing)|"
    r"is not (built|implemented|wired|available|present)|"
    r"(was|has) never been (built|implemented|wired|added)|"
    r"hasn'?t been (built|implemented|wired|added))\b",
    re.I,
)

# SEARCH evidence in the SAME message => the absence was verified; allow.
_SEARCHED = re.compile(
    r"\b(grep\w*|ripgrep|\brg\b|glob\w*|\bfind\b|searched|search of|semantic[- ]?search|"
    r"no (match\w*|results?|hits?|files?)|(returned|found|got) (no|0|zero|nothing)|"
    r"did ?n'?t find|could ?n'?t find|couldn'?t locate|nothing (matched|found|turned up)|"
    r"after (searching|grepping|checking|looking through|scanning)|scanned the (repo|codebase|code)|"
    r"(no|zero) (such )?(file|module|function|def|reference)s?)\b"
    r"|\b[\w./-]+\.(py|sh|md|json|txt|ya?ml)(:\d+)?\b",   # a file/path citation = I looked at the tree
    re.I,
)
# Honest HEDGE / not-yet-searched label => allow (it's flagged, not asserted).
_HEDGE = re.compile(
    r"\bi (haven'?t|have not|did not|didn'?t|need to|should|will|'?ll) (search\w*|grep\w*|check\w*|look\w*|verif\w*|confirm\w*)"
    r"|\blet me (search|grep|check|look|verify|confirm|find out)"
    r"|\bmay (already )?(exist|be (built|there|present))|\bmight (already )?exist"
    r"|\bif (it|this|that|one) (already )?exists?\b"
    r"|\bnot sure (if|whether) (we|it|the bot)\b"
    r"|\bbefore (i|we) (assume|claim|conclude)\b"
    r"|\b(unverified|unchecked|assum(e|ing|ption)|presum(e|ing|ption))\b"
    r"|\bwithout (searching|checking|grepping|verifying)\b",
    re.I,
)


# MARKET-FACT ABSENCE (Rafael 2026-10-09): "AMD has no liquid inverse ETF" was stated as fact — false (DAMD, 2x
# short AMD, traded 322,212 IEX shares that day); the claim came from the bot's own hardcoded map. The capability
# patterns above never fired: the subject was a STOCK, and the message cited unrelated file paths elsewhere. So a
# claim that an instrument / stock LACKS a market thing (an ETF, history, liquidity, borrow) or "is not tradable"
# needs its evidence in the SAME sentence or the next one — evidence elsewhere in the message does not count.
_ETF = (r"(?:etfs?|inverse|bear|bull|leveraged|levered|2x|3x)(?:\s+(?:etfs?|funds?|products?))?"
        r"(?!\s+(?:market|run|case|trend|thesis|flag|trap|leg|phase)s?\b)")   # "bear market" / "bull run" are prose
_MKT_ALL = r"(?:" + _ETF + r"|price history|history|bars|liquidity|volume|borrow|options?|data)"
# An upper-case ticker subject (NVDA, SNDK, DRAM) — not a common acronym / indicator name (cold-2nd 2026-10-09).
_NOT_TICKER = ("ET|PT|UTC|OK|AH|RTH|API|CI|OCO|GTC|DAY|ETF|ETFS|RSI|MACD|ATR|EMA|SMA|VWAP|VIX|MRI|GEX|FMP|IEX|SIP|"
               "OCI|PR|CEO|QHM|F6|DT|IN|QH|TP|SL|PNL|KPI|SPY|AND|THE|IT|WE|NOT|NO")
_TICKER = r"(?-i:\b(?!(?:" + _NOT_TICKER + r")\b)[A-Z]{2,5}\b)(?:'s)?"
_MKT_ABSENCE = re.compile(
    # <TICKER> has no / lacks <market thing>
    _TICKER + r"\s+(?:(?:has|have|had)\s+no|lacks?|is lacking)\s+(?:(?:\w+)\s+){0,3}?" + _MKT_ALL + r"\b"
    # any subject, but only an ETF-type object: "has no liquid inverse ETF", "with no 2x bear", "without an inverse"
    r"|\b(?:(?:has|have|had|with|there(?:'?s| is| are)?)\s+no|without(?:\s+an?)?)\s+"
    r"(?:(?:liquid|usable|tradable|tradeable|real|available|listed|such|other)\s+){0,2}" + _ETF + r"\b"
    r"|\b(?:never|not|isn'?t|aren'?t|is not|are not|can'?t be|cannot be)\s+(?:\w+\s+)?"
    r"(?:tradable|tradeable|shortable|borrowable)\b"
    r"|\b(?:no|none of the|not an?)\s+(?:liquid\s+|usable\s+)?(?:inverse|bear|bull|leveraged|2x|3x)\s+(?:etf|fund|exists?)\b"
    r"|\b(?:doesn'?t|does not|don'?t|do not)\s+have an? (?:inverse|bear|bull|leveraged)\b",
    re.I,
)
# Evidence a market fact was CHECKED at its source (asset list / quote / bars / a cited number).
_MKT_EVIDENCE = re.compile(
    r"\b(alpaca|/v2/assets|assets api|snapshot|get_asset|iex|sip|checked|verified|confirmed|queried|"
    r"per the (api|data|asset)|traded [\d,]+|[\d,]{4,} (iex )?shares|volume [\d,]+|"
    r"\d+ (daily |trading )?(bars|days|months))\b"
    r"|\b[\w./-]+\.(py|sh|md|json)(:\d+)?\b",
    re.I,
)
_CONDITIONAL = re.compile(r"\b(if|when|whenever|where|unless|until|once|any|an? ([\w-]+ )?(stock|name|symbol|lot|ticker|long|short))\b[^.!?]*$", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _market_absence_violation(text: str):
    """A market-fact absence claim whose own sentence (or the next) carries neither source evidence nor a hedge."""
    for para in re.split(r"\n\s*\n", text):   # the "next sentence" never crosses a blank line
        sents = [x for x in _SENT_SPLIT.split(para) if x and x.strip()]
        for i, sent in enumerate(sents):
            m = _MKT_ABSENCE.search(sent)
            if not m:
                continue
            if _CONDITIONAL.search(sent[max(0, m.start() - 40):m.start()]):
                continue   # a rule/condition ("when there's no 2x bear", "a stock with no ETF"), not a fact claim
            window = sent + " " + (sents[i + 1] if i + 1 < len(sents) else "")
            if _MKT_EVIDENCE.search(window) or _HEDGE.search(window):
                continue
            return m.group(0).strip()[:80]
    return None


def _violation(text: str):
    """Return the offending absence phrase if the message asserts a capability is missing with
    NEITHER search evidence NOR a hedge, or a market-fact absence without same-sentence evidence; else None."""
    m = _ABSENCE_VERB.search(text) or _ABSENCE_EXIST.search(text)
    if m and not (_SEARCHED.search(text) or _HEDGE.search(text)):
        return m.group(0).strip()[:80]
    return _market_absence_violation(text)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    if data.get("stop_hook_active"):
        return 0
    last = _last_assistant_text(data.get("transcript_path", ""))
    if not last:
        return 0
    hit = _violation(last)
    if hit:
        sys.stderr.write(
            "NO-ASSUME GATE (blocking): your turn asserts the codebase/bot LACKS a capability, tool, "
            f"data, or work — '{hit}' — with NO evidence you searched for it. This is the recurring "
            "thoroughness failure (claiming order-flow/gamma/edge-tracker/watchdog didn't exist when "
            "it did). Per the NO-GUESS + full-thorough-read mandate: SEARCH FIRST (grep/find/glob/"
            "semantic_search the repo), then cite what you found — or found nothing. For a MARKET fact "
            "(an ETF, history, liquidity, tradability) check the source (Alpaca assets / snapshot / bars) "
            "and cite it in the SAME sentence — a gap in the bot's own map/config is not a market fact. "
            "Do not state absence as fact from assumption. Redo the turn: check, then claim."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
