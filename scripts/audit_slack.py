#!/usr/bin/env python3
# ruff: noqa: E501  — long docstring / mrkdwn string literals (project convention, see main.py)
"""
scripts/audit_slack.py — reusable Slack Block Kit renderer for audit reports.

Design locked 2026-07-01 (board + Rafael approval):
  - PERFORMANCE-FIRST ordering, with a Critical-banner exception: if verdict==FAIL
    AND a Critical finding exists, a one-line "🔴 CRITICAL: <name>" banner is
    prepended above the performance block (act-now beats look-at-numbers only when
    something is actually broken). [Wroblewski + Majors]
  - 3-level severity only (🔴 Critical / 🟡 High / ✓ Low). No 🚨-spam. Low items are NOT shown on
    the card (not actionable; logged + kept in the report files — Rafael 2026-09-25).
  - P&L is COMPUTED BY CODE from the authoritative Alpaca-FIFO source and INJECTED
    directly into the card. The audit LLM never restates a number; a deterministic
    validator (validate_no_pnl_rewrite) confirms no rogue dollar figure slipped in.
    [McKinney single-source-of-truth; Derman provenance; Thorp reconciliation]
  - Midday card shows REALIZED P&L SO FAR THIS SESSION (Alpaca FIFO; Rafael 2026-09-14 — "see what
    has happened so far this session"); unrealized MTM moved to the weekly recap. Nightly card shows
    REALIZED FIFO P&L for the closed day. Fail-safe: if the realized-so-far compute is unavailable the
    midday card falls back to labeled unrealized MTM rather than going blank.
  - Reconciliation (BGG 2026-07-27): the nightly card runs 4:05pm ET but the
    authoritative ledger heal runs 8:30pm ET, so pnl_drift/alpaca_pnl/tracker_pnl are
    PRE-HEAL dual-compute TELEMETRY — a raw drift here is EXPECTED, not a failure. The
    card keys off the `_healed_by` provenance stamp and `pnl_unreconciled`, NEVER off
    drift magnitude. Pre-heal → the number is labeled PROVISIONAL (never silently
    "clean"); a genuine ledger-flagged pnl_unreconciled → "reconciliation unresolved".
    (masked-loss + reliability seats: a self-check delta must never read as a real loss,
    and an unhealed file must never render as reconciled.)
  - ACTIONABLE-ONLY (Rafael 2026-09-25: "actionable info only"; the report path is not a Slack
    link, so it is not shown). The card carries the P&L block, the verdict and the critical/high
    findings. Footer lines from callers are shown ONLY when they flag something to act on
    (prefix ⚠️ / 🚨 / ❌ / 🔴); every other footer line and all low-severity findings are logged
    (this module's logger → the cron log) and remain in the saved report files, not on the card.

This module renders + posts only. Distribution wiring (git push / directives append
/ Master Brain) is provided by build_distribution_footer() consumers.
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.request
from typing import Optional

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()

logger = logging.getLogger(__name__)

# Footer lines shown on the card only if they start with one of these (they flag something to act
# on). Everything else is logged, not displayed (Rafael 2026-09-25). The bare U+26A0 "⚠" matches
# with or without the U+FE0F variation selector, so a warning line can never be hidden by encoding.
_ACTIONABLE_PREFIXES = ("\u26a0", "🚨", "❌", "🔴")

_SEV = {"critical": "🔴", "high": "🟡", "low": "✓"}


# ── P&L block (authoritative, code-computed) ─────────────────────────────────

def build_pnl_fields(mode: str, eod: dict, positions: Optional[list] = None) -> dict:
    """
    Return {"today": [...fields...], "lifetime": [...fields...], "source_note": str,
            "injected_numbers": [str,...], "mismatch": bool}.

    mode="nightly": realized P&L from EOD snapshot `pnl_today` (ledger-authoritative);
                    reconciliation status from `_healed_by`/`pnl_unreconciled`, not drift.
    mode="midday" : unrealized mark-to-market from live positions; labeled provisional.
    Numbers here are the ONLY source of truth for the card — the LLM never restates them.
    """
    injected: list[str] = []
    mismatch = False
    stats = eod.get("all_time_stats", {}) or {}

    def _dollar(v: float) -> str:
        s = f"${abs(v):,.2f}"
        return f"−{s}" if v < 0 else s

    if mode == "nightly":
        # Authoritative day P&L is the ledger-healed `pnl_today`. `alpaca_pnl`/`pnl_drift`/
        # `tracker_pnl` are PRE-HEAL dual-compute telemetry: the heal runs 8:30pm ET, AFTER
        # this 4:05pm audit, so a raw drift here is EXPECTED and is NOT a reconciliation
        # failure. Key off the `_healed_by` provenance stamp + `pnl_unreconciled`, never off
        # drift magnitude (BGG 2026-07-27, masked-loss + reliability seats).
        realized = float(eod.get("pnl_today", eod.get("alpaca_pnl", 0.0)) or 0.0)
        healed = bool(eod.get("_healed_by"))
        unreconciled = bool(eod.get("pnl_unreconciled"))
        # The ONLY thing that shows an alarm instead of a number is a genuine ledger-flagged
        # unreconciled state — never a raw pre-heal drift.
        mismatch = unreconciled
        if mismatch:
            realized_str = "⚠️ reconciliation unresolved — verify"
        else:
            realized_str = _dollar(realized)
            injected.append(_dollar(realized))
        closed = int(eod.get("trades_today", 0) or 0)
        today_fields = [
            {"type": "mrkdwn", "text": f"*Realized P&L*\n{realized_str}"},
            {"type": "mrkdwn", "text": f"*Closed today*\n{closed}"},
        ]
        if healed:
            source = "Alpaca FIFO · reconciled"
        else:
            source = "Alpaca FIFO · _provisional until the 5:30 PM PT reconcile_"
    else:  # midday — REALIZED P&L SO FAR THIS SESSION (Rafael 2026-09-14: "see what has happened
           # so far this session" — not unrealized MTM, which now lives in the weekly recap).
        n_open = len(positions or [])
        realized_today = None
        try:                                             # authoritative realized-so-far from Alpaca FIFO
            import reporting.pnl_ledger as _pl
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            _today = _dt.now(_ZI("America/Los_Angeles")).strftime("%Y-%m-%d")
            _fills = _pl.fetch_all_fills() or []
            realized_today = round(float(_pl.compute_realized(_fills).get("per_day", {}).get(_today, 0.0) or 0.0), 2)
        except Exception:
            realized_today = None                        # fail-safe below — never a blank/broken card
        if realized_today is not None:
            injected.append(_dollar(realized_today))
            today_fields = [
                {"type": "mrkdwn", "text": f"*Realized P&L (so far)*\n{_dollar(realized_today)}"},
                {"type": "mrkdwn", "text": f"*Open positions*\n{n_open}"},
            ]
            source = "Alpaca FIFO · realized so far today"
        else:                                            # realized compute unavailable → fall back to MTM, labeled
            upl = 0.0
            for p in (positions or []):
                try:
                    upl += float(p.get("unrealized_pl", 0.0))
                except Exception:
                    continue
            injected.append(_dollar(upl))
            today_fields = [
                {"type": "mrkdwn", "text": f"*Unrealized P&L* _(realized-so-far unavailable)_\n{_dollar(upl)}"},
                {"type": "mrkdwn", "text": f"*Open positions*\n{n_open}"},
            ]
            source = "Alpaca mark-to-market · _realized unavailable this run_"

    lifetime_fields = []
    if stats:
        wr = stats.get("win_rate")
        pf = stats.get("profit_factor")
        ar = stats.get("avg_r_multiple")
        if wr is not None:
            lifetime_fields.append({"type": "mrkdwn", "text": f"*Win rate*\n{float(wr):.1f}%"})
        if pf is not None:
            lifetime_fields.append({"type": "mrkdwn", "text": f"*Profit factor*\n{float(pf):.2f}"})
        if ar is not None:
            lifetime_fields.append({"type": "mrkdwn", "text": f"*Avg R-multiple*\n{float(ar):+.3f}"})

    return {"today": today_fields, "lifetime": lifetime_fields,
            "source_note": source, "injected_numbers": injected, "mismatch": mismatch}


# ── Gemini-report → findings mapper ──────────────────────────────────────────

# A line STARTS a new finding if it carries an explicit list marker...
_ITEM_MARKER = re.compile(r"^\s{0,3}(?:[-•]|\d+[.)]|\*(?!\*))\s+")
# ...or leads with a bolded title (`**Foo**`) that is NOT one of these continuation
# sub-field labels. Gemini writes each finding as a title line + **Why**/**SEVERITY**/…
# detail lines; those detail lines must fold INTO the finding, not become their own.
# The trailing `...:` requirement keeps a real bug TITLE that merely starts with one of
# these words (e.g. "**Fix validation bypassed**") from being mistaken for a sub-field
# label ("**Why ...**:", "**SEVERITY**:") and wrongly folded into the previous finding.
_CONT_FIELD = re.compile(
    r"^\**\s*(why|severity|exact failure|impact|recommendation|root cause|"
    r"fix|evidence|explanation|reason|mitigation|detail)\b[^:\n]{0,60}:", re.I)
_BOLD_LEAD = re.compile(r"^\*\*[^*]")
_TAGS = {"EXECUTION BUG", "ALPHA ISSUE", "INFRASTRUCTURE",
         "CRITICAL", "HIGH", "MEDIUM", "LOW"}
_STOP_HEADERS = ("VERDICT", "LOG ANOMALIES", "PERFORMANCE AUDIT",
                 "TRADE INTEGRITY", "MODIFIED FILE", "P5 STATUS",
                 "FIX VALIDATION", "RECOMMENDED", "ENTRY QUALITY",
                 "EXIT QUALITY", "MRI ALIGNMENT")


def _clean_md(s: str) -> str:
    """Convert Gemini's GitHub-style emphasis (`**bold**` / `*em*`) to plain text so it does
    not leak as literal asterisks — render_card owns the single-`*` Slack bolding. PRESERVES
    inline-code backticks (Slack renders them) and arithmetic/globs like `2*ATR` (only
    whitespace-bounded stray `*` markers are dropped, never a `*` between non-space chars).
    Collapses whitespace. Never raises."""
    s = re.sub(r"\*{1,2}([^*]+?)\*{1,2}", r"\1", s)   # paired *x* / **x** → x
    s = s.replace("**", "")                            # any leaked double-asterisk marker
    s = re.sub(r"(?<!\S)\*|\*(?!\S)", "", s)           # stray whitespace-bounded * (keep 2*ATR)
    return re.sub(r"\s+", " ", s).strip()


def _word_trunc(s: str, n: int) -> str:
    """Truncate with an ellipsis, preferring a word boundary. Falls back to a hard cut only
    for a single token longer than the limit (a URL/hash with no interior space, where no
    word boundary exists). Never raises."""
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0].rstrip(" .,;:—-")
    return (cut or s[:n]).rstrip() + "…"


def _is_tag(seg: str) -> bool:
    u = seg.upper()
    return u.startswith(("CATEGORY:", "SEVERITY:")) or u in _TAGS


def findings_from_report(report: str) -> list[dict]:
    """Map a Gemini audit report (### CATASTROPHIC ALERT / ### NEW BUGS sections) onto the
    card's 3-severity model — ONE clean entry per real finding.

    Gemini writes each finding as a title line followed by **Why**/**SEVERITY**/… detail
    lines. The prior version made EACH line its own finding (3–4× repeats, mis-severitied
    fragments, literal `**` leaks, title==detail duplication). This groups continuation
    lines into their finding, strips markdown, dedups near-identical titles, and truncates
    on word boundaries. Lenient by design: unparseable input is skipped, never raised — the
    audit must post regardless."""
    findings: list[dict] = []
    section: Optional[str] = None
    cur: Optional[dict] = None

    def _flush() -> None:
        nonlocal cur
        if not cur or not cur["raw"]:
            cur = None
            return
        text = _clean_md(" ".join(cur["raw"]))
        # Mask any dollar figure the LLM wrote — authoritative numbers are code-injected
        # P&L fields (validate_no_pnl_rewrite enforces this); full figures live in the file.
        text = re.sub(r"−?-?\$[0-9][0-9,]*(?:\.[0-9]{1,2})?", "$…", text)
        if not text or text.lower().startswith("none"):
            cur = None
            return
        up = text.upper()
        if cur["section"] == "cat" or "CATASTROPHIC" in up:
            sev = "critical"
        elif "CRITICAL" in up or "HIGH" in up:
            sev = "high"
        else:
            sev = "low"
        parts = [p.strip() for p in text.split("|") if p.strip()]
        core = [p for p in parts if not _is_tag(p)] or parts
        title = core[0] if core else text          # pipe-only text → fall back, never IndexError
        detail = " — ".join(core[1:]) if len(core) > 1 else ""
        if not detail and ". " in title:          # no |-structure → split off first sentence
            title, detail = title.split(". ", 1)
        title = title.strip()
        if not title:                              # degenerate (e.g. text began with ". ") — skip
            cur = None
            return
        findings.append({
            "severity": sev,
            "title": _word_trunc(title, 120),
            "detail": _word_trunc(detail.strip(), 240),
        })
        cur = None

    for raw in report.splitlines():
        line = raw.strip()
        s = line.lstrip("#* ").strip()
        if s.upper().startswith("CATASTROPHIC ALERT"):
            _flush()
            section = "cat"
            continue
        if s.upper().startswith("NEW BUGS"):
            _flush()
            section = "bugs"
            continue
        if s.upper().startswith(_STOP_HEADERS):
            _flush()
            section = None
            continue
        if not section:
            continue
        if not line:                               # blank line closes the current finding
            _flush()
            continue
        stripped = line.lstrip("0123456789.)•*->— \t").strip()
        if not stripped:
            continue
        new_item = bool(_ITEM_MARKER.match(raw)) or (
            bool(_BOLD_LEAD.match(line)) and not _CONT_FIELD.match(line))
        if cur is not None and not new_item:
            cur["raw"].append(stripped)            # continuation → fold into current finding
            continue
        _flush()
        cur = {"section": section, "raw": [stripped]}
    _flush()

    # Dedup near-identical titles (belt-and-suspenders vs any residual repeats).
    out: list[dict] = []
    seen: list[str] = []
    for f in findings:
        k = f["title"].lower().strip()
        if not k or k in seen:                     # exact-title dedup — never drop a distinct substring title
            continue
        seen.append(k)
        out.append(f)
    return out[:12]  # card stays scannable; full narrative lives in the report file


# ── Card renderer ────────────────────────────────────────────────────────────

def _finding_line(f: dict) -> str:
    """`*Title* — detail`, but drop the ` — detail` when detail is empty or identical to
    the title (the old renderer printed `*title* — title` for prose findings)."""
    t = str(f.get("title", "")).strip()
    d = str(f.get("detail", "")).strip()
    return f"*{t}* — {d}" if d and d != t else f"*{t}*"


def render_card(mode: str, date_str: str, verdict: str, pnl: dict,
                findings: list[dict], dist_footer: Optional[list] = None,
                sample: bool = False, context_line: Optional[str] = None) -> dict:
    """
    findings: list of {"severity": "critical|high|low", "title": str, "detail": str}
              (detail already one-liner for low). Rendered per approved design.
    Returns a Slack webhook payload {"blocks":[...], "text": fallback}.
    """
    label = "🧪 SAMPLE — " if sample else ""
    mode_word = "Post-Market" if mode == "nightly" else "Midday"
    crits = [f for f in findings if f.get("severity") == "critical"]
    highs = [f for f in findings if f.get("severity") == "high"]
    lows = [f for f in findings if f.get("severity") == "low"]

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{label}{mode_word} Audit · {date_str}", "emoji": True}},
    ]
    if sample:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
            "text": "_Format preview — not a live alert._"}]})

    # Critical-banner exception: FAIL + a critical finding jumps above performance.
    if verdict.upper() == "FAIL" and crits:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🔴 CRITICAL: {crits[0]['title']}*"}})

    # PERFORMANCE FIRST
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn", "text": "*📊 Today*"},
                   "fields": pnl["today"]})
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": pnl["source_note"]}]})
    if pnl["lifetime"]:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "*Lifetime*"},
                       "fields": pnl["lifetime"]})
    blocks.append({"type": "divider"})

    # VERDICT — one line, stated as what to do. A FAIL with nothing parsed to act on still says
    # FAIL (never softened) and points at the log.
    n_act = len(crits) + len(highs)
    if n_act:
        v_line = f"{'🔴' if crits else '🟡'} {n_act} to act on"
    elif verdict.upper() == "FAIL":
        v_line = "🔴 FAIL — check the audit log"
    else:
        v_line = "✅ Nothing to act on"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{v_line}*"}})
    if context_line:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": context_line}]})

    # FINDINGS — Critical, High, then Low (collapse >2)
    for f in crits:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🔴 Critical*\n{_finding_line(f)}"}})
    for f in highs:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🟡 High*\n{_finding_line(f)}"}})
    for f in lows:                                  # not actionable → logged only
        logger.info("audit card (%s %s) low finding not shown: %s", mode, date_str,
                    f.get("detail") or f.get("title"))

    # FOOTER — actionable lines only; the rest is logged.
    shown = [str(x) for x in (dist_footer or []) if str(x).lstrip().startswith(_ACTIONABLE_PREFIXES)]
    for x in dist_footer or []:
        if str(x) not in shown:
            logger.info("audit card (%s %s) footer line not shown: %s", mode, date_str, x)
    if shown:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(shown)}})

    fallback = (f"{label}{mode_word} Audit {date_str} — {verdict.upper()} — "
                f"{len(crits)} critical, {len(highs)} high, {len(lows)} low")
    return {"blocks": blocks, "text": fallback}


# ── Deterministic P&L-rewrite guard ──────────────────────────────────────────

def validate_no_pnl_rewrite(payload: dict, injected_numbers: list[str]) -> tuple[bool, str]:
    """
    Belt-and-suspenders (board Q1): scan the finished card for dollar figures and
    confirm every $-amount present is one we injected. Any dollar amount NOT in the
    injected set means the LLM narrative introduced a number — block the post.
    Returns (ok, reason).
    """
    # ensure_ascii=False so the U+2212 minus survives as a literal character and
    # negative amounts match their allowlisted form (default dumps escapes to −).
    text = json.dumps(payload, ensure_ascii=False)
    found = set(re.findall(r"−?\$[0-9][0-9,]*\.[0-9]{2}", text))
    allowed = set(injected_numbers)
    rogue = found - allowed
    if rogue:
        return False, f"unexpected dollar figure(s) in card not from authoritative source: {sorted(rogue)}"
    return True, "ok"


# ── Slack post ───────────────────────────────────────────────────────────────

def post_to_slack(payload: dict, webhook: Optional[str] = None) -> int:
    webhook = webhook or os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL not set")
    data = json.dumps(payload).encode()
    req = urllib.request.Request(webhook, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
        return resp.status
