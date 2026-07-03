#!/usr/bin/env python3
# ruff: noqa: E501  — long docstring / mrkdwn string literals (project convention, see main.py)
"""
scripts/audit_slack.py — reusable Slack Block Kit renderer for audit reports.

Design locked 2026-07-01 (board + Rafael approval):
  - PERFORMANCE-FIRST ordering, with a Critical-banner exception: if verdict==FAIL
    AND a Critical finding exists, a one-line "🔴 CRITICAL: <name>" banner is
    prepended above the performance block (act-now beats look-at-numbers only when
    something is actually broken). [Wroblewski + Majors]
  - 3-level severity only (🔴 Critical / 🟡 High / ✓ Low). No 🚨-spam.
  - Low items shown inline only if <=2; otherwise collapsed to a count line. [N=2]
  - P&L is COMPUTED BY CODE from the authoritative Alpaca-FIFO source and INJECTED
    directly into the card. The audit LLM never restates a number; a deterministic
    validator (validate_no_pnl_rewrite) confirms no rogue dollar figure slipped in.
    [McKinney single-source-of-truth; Derman provenance; Thorp reconciliation]
  - Midday card shows UNREALIZED (mark-to-market) P&L labeled "settles tonight";
    nightly card shows REALIZED FIFO P&L. Paper-account settlement lag forces this.
  - Reconciliation: nightly uses the EOD snapshot's pnl_drift (Alpaca FIFO vs
    tracker). Non-zero beyond tolerance → "⚠️ reconciliation mismatch" instead of a
    number we can't stand behind. (equity−last_equity cross-check dropped per Thorp:
    the paper balance lags same-day fills too, so it is not an independent source.)
  - Footer: per-destination distribution confirmation (git / directives / Master
    Brain), ✅ on success, ❌ on failure. [Majors confirm-delivery; Kim system-of-
    record; Schneier durable audit trail]

This module renders + posts only. Distribution wiring (git push / directives append
/ Master Brain) is provided by build_distribution_footer() consumers.
"""
from __future__ import annotations

import json
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

_PNL_DRIFT_TOLERANCE = 0.01   # dollars; above this, show reconciliation mismatch
_LOW_INLINE_MAX = 2           # show Low items inline only if <= this many (board N=2)

_SEV = {"critical": "🔴", "high": "🟡", "low": "✓"}


# ── P&L block (authoritative, code-computed) ─────────────────────────────────

def build_pnl_fields(mode: str, eod: dict, positions: Optional[list] = None) -> dict:
    """
    Return {"today": [...fields...], "lifetime": [...fields...], "source_note": str,
            "injected_numbers": [str,...], "mismatch": bool}.

    mode="nightly": realized P&L from EOD snapshot alpaca_pnl (FIFO, authoritative);
                    reconciliation via pnl_drift.
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
        realized = float(eod.get("alpaca_pnl", eod.get("pnl_today", 0.0)) or 0.0)
        drift = float(eod.get("pnl_drift", 0.0) or 0.0)
        mismatch = abs(drift) > _PNL_DRIFT_TOLERANCE
        realized_str = "⚠️ reconciliation mismatch — verify" if mismatch else _dollar(realized)
        if not mismatch:
            injected.append(_dollar(realized))
        injected.append(_dollar(drift))  # drift appears in source_note — allowlist it
        closed = int(eod.get("trades_today", 0) or 0)
        today_fields = [
            {"type": "mrkdwn", "text": f"*Realized P&L*\n{realized_str}"},
            {"type": "mrkdwn", "text": f"*Closed today*\n{closed}"},
        ]
        source = ("Source: *Alpaca FIFO* (settled) · reconciled vs tracker "
                  f"(drift {_dollar(drift)})")
    else:  # midday — unrealized mark-to-market
        upl = 0.0
        n_open = 0
        for p in (positions or []):
            try:
                upl += float(p.get("unrealized_pl", 0.0))
                n_open += 1
            except Exception:
                continue
        injected.append(_dollar(upl))
        today_fields = [
            {"type": "mrkdwn", "text": f"*Unrealized P&L*\n{_dollar(upl)}"},
            {"type": "mrkdwn", "text": f"*Open positions*\n{n_open}"},
        ]
        source = ("Source: *Alpaca mark-to-market* · _provisional — realized P&L "
                  "finalizes in tonight's post-market run (paper fills settle overnight)_")

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

def findings_from_report(report: str) -> list[dict]:
    """Map a Gemini audit report (### CATASTROPHIC ALERT / ### NEW BUGS sections)
    onto the card's 3-severity model. CATASTROPHIC lines → critical; NEW BUGS
    lines → high (CRITICAL/HIGH tag) or low (MEDIUM/LOW tag). Lenient by design:
    unparseable lines are skipped, never raised — the audit must post regardless."""
    _stop_headers = ("VERDICT", "LOG ANOMALIES", "PERFORMANCE AUDIT",
                     "TRADE INTEGRITY", "MODIFIED FILE", "P5 STATUS",
                     "FIX VALIDATION", "RECOMMENDED", "ENTRY QUALITY",
                     "EXIT QUALITY", "MRI ALIGNMENT")
    _tags = {"EXECUTION BUG", "ALPHA ISSUE", "INFRASTRUCTURE",
             "CRITICAL", "HIGH", "MEDIUM", "LOW"}
    findings: list[dict] = []
    section = None
    for raw in report.splitlines():
        line = raw.strip()
        s = line.lstrip("#* ").strip()
        if s.upper().startswith("CATASTROPHIC ALERT"):
            section = "cat"
            continue
        if s.upper().startswith("NEW BUGS"):
            section = "bugs"
            continue
        if s.upper().startswith(_stop_headers):
            section = None
            continue
        if not section or not line:
            continue
        body = line.lstrip("0123456789.•*->— ").strip().strip("*").strip()
        if not body or body.lower().startswith("none"):
            continue
        # Mask any dollar figure the LLM wrote — the card's only authoritative
        # numbers are the code-injected P&L fields (validate_no_pnl_rewrite
        # enforces this); full figures live in the report file.
        body = re.sub(r"−?-?\$[0-9][0-9,]*(?:\.[0-9]{1,2})?", "$…", body)
        parts = [p.strip().strip("*").strip("`") for p in body.split("|")]

        def _is_tag(seg: str) -> bool:
            u = seg.upper()
            return (u.startswith(("CATEGORY:", "SEVERITY:")) or u in _tags)

        if section == "cat":
            title = next((p for p in parts if p and not _is_tag(p)), parts[0])
            findings.append({
                "severity": "critical",
                "title": title[:120],
                "detail": (" — ".join(p for p in parts if p and p != title)
                           or title)[:300],
            })
        else:
            up = body.upper()
            if "CATASTROPHIC" in up:
                sev = "critical"
            elif "CRITICAL" in up or "HIGH" in up:
                sev = "high"
            else:
                sev = "low"
            title = next((p for p in parts if p and not _is_tag(p)), parts[0])
            findings.append({"severity": sev, "title": title[:120], "detail": body[:300]})
    return findings[:12]  # card stays scannable; full narrative lives in the report file


# ── Card renderer ────────────────────────────────────────────────────────────

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
                   "text": {"type": "mrkdwn", "text": f"*📊 Today — {date_str}*"},
                   "fields": pnl["today"]})
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": pnl["source_note"]}]})
    if pnl["lifetime"]:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "*Lifetime*"},
                       "fields": pnl["lifetime"]})
    blocks.append({"type": "divider"})

    # VERDICT
    v_emoji = {"PASS": "✅", "WARN": "🟡", "FAIL": "🔴"}.get(verdict.upper(), "•")
    n_act = len(crits) + len(highs)
    tail = f"{n_act} item(s) to act on" if n_act else "no action needed"
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": f"*Verdict: {v_emoji} {verdict.upper()}* — {tail}"}})
    if context_line:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": context_line}]})

    # FINDINGS — Critical, High, then Low (collapse >2)
    for f in crits:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🔴 Critical*\n*{f['title']}* — {f['detail']}"}})
    for f in highs:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*🟡 High*\n*{f['title']}* — {f['detail']}"}})
    if lows:
        if len(lows) <= _LOW_INLINE_MAX:
            body = "\n".join(f"• {f['detail']}" for f in lows)
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": f"*✓ Low*\n{body}"}})
        else:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": f"*✓ Low*\n{len(lows)} low-severity housekeeping items — see full report"}})

    # DISTRIBUTION FOOTER
    if dist_footer:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": "*📌 Logged & distributed*\n" + "\n".join(dist_footer)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "P&L computed by code from Alpaca FIFO (LLM never restates numbers) · "
                "full narrative in the linked report"}]})

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
