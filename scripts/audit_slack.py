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
import math
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

def _ledger_and_account() -> Optional[dict]:
    """One consistent read of the authoritative inputs (ACCOUNT-PNL-1020): full-history Alpaca fills + orders
    (strict — a short history raises and the card falls back), the FIFO ledger split as the 8:30 PM heal splits it,
    and ONE account + ONE positions read so equity, prior-close equity and open P&L come from the same moment.
    Returns None on any failure. Never raises."""
    try:
        import reporting.pnl_ledger as _pl
        fills = _pl.fetch_all_fills(strict=True) or []      # a short history must fall back, never mislabel
        orders = _pl.fetch_all_orders(strict=True) or []
        led = _pl.compute_realized(fills, qhm_symbols=_pl._get_qhm_symbols(),
                                   coid_map=_pl.build_coid_map(orders))
        acct = _pl.fetch_account()
        positions = _pl.fetch_positions()
        deposits = _pl.fetch_net_deposits()
        try:   # account-view inputs only: a failure here withholds the account lines, never the realized line
            hist = _pl._get_json(f"{_pl._PAPER_BASE}/v2/account/portfolio/history?period=1W&timeframe=1D")
            fees = _fees_total(_pl)
        except Exception as e:  # noqa: BLE001
            logger.warning("audit card: fee/history read failed (%s) — account lines withheld", e)
            hist, fees = None, None
        return {"led": led, "acct": acct, "positions": positions, "deposits": deposits, "fees": fees,
                "history": hist, "tolerance": float(_pl._INVARIANT_TOLERANCE),
                "check_invariant": _pl.check_invariant}
    except Exception as e:  # noqa: BLE001 — a card must never break on the P&L read
        logger.warning("audit card: Alpaca ledger/account read failed (%s)", e)
        return None


def _fees_total(_pl) -> float:
    """Sum of every posted FEE activity (regulatory/CAT fees; negative). Pages with page_token (Alpaca ignores
    after_id). Raises on a failed or non-list page so the account check is withheld rather than wrong."""
    total, token = 0.0, None
    for _ in range(200):
        url = f"{_pl._PAPER_BASE}/v2/account/activities/FEE?page_size=100" + (f"&page_token={token}" if token else "")
        batch = _pl._get_json(url)
        if not isinstance(batch, list):
            raise RuntimeError("fee activities returned a non-list page")
        total += sum(float(a.get("net_amount") or 0.0) for a in batch)
        if len(batch) < 100:
            return round(total, 2)
        token = batch[-1].get("id")
        if not token:
            raise RuntimeError("fee activities page has no id to continue from")
    raise RuntimeError("fee activities exceeded the page limit")


def _history_prior_close(hist: dict, day: str) -> Optional[float]:
    """Equity at the last completed session before ET date `day`, from Alpaca's portfolio-history endpoint (each
    daily point is stamped 20:00 ET on its own session date, so converted to ET it lands on that date). An independent check on the account's last_equity, which
    has gone stale before (2026-09-16 kill-switch false trip). None when unreadable."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        pts = [(datetime.fromtimestamp(int(t), tz=et).strftime("%Y-%m-%d"), e)
               for t, e in zip(hist.get("timestamp") or [], hist.get("equity") or [], strict=False)]
        prior = [float(e) for d, e in pts if d < day and e is not None]
        return prior[-1] if prior else None
    except Exception:  # noqa: BLE001
        return None


def _account_pnl(day: str, src: Optional[dict]) -> Optional[dict]:
    """Whole-account P&L for the post-market card (ACCOUNT-PNL-1020), each number cross-checked against an
    independent source before it is shown:
      today  = equity - prior-close equity, where the prior close from the account (last_equity) must agree with
               the prior close in Alpaca's portfolio history (a stale last_equity is the known failure);
      since  = equity - net deposits (Alpaca cash activities; when the account has no deposit records, the
               ledger's assumed $2,500 start, labelled), checked against lifetime FIFO realized + posted fees + open
               unrealized (pnl_ledger.check_invariant, the ledger's own tolerance);
      open   = the open positions' unrealized P&L.
    None when any input is unreadable. Never raises."""
    try:
        if not src:
            return None
        acct, positions, led = src["acct"], src["positions"], src["led"]
        equity = float(acct["equity"])
        last_equity = float(acct["last_equity"])
        if not (math.isfinite(equity) and math.isfinite(last_equity) and equity > 0 and last_equity > 0):
            return None
        unreal = round(sum(float(p.get("unrealized_pl") or 0.0) for p in positions), 2)
        tol = src["tolerance"]
        today = round(equity - last_equity, 2)
        hist_prev = _history_prior_close(src.get("history") or {}, day)
        if hist_prev is None or src.get("fees") is None:
            return None
        today_alt = round(equity - hist_prev, 2)
        deposits = float(src["deposits"] or 0.0)
        assumed = deposits == 0   # no deposit records on this account (portfolio history base 2,500 on 04-02)
        start = 2500.0 if assumed else deposits   # pnl_ledger.build_ledger's documented assumed start capital
        fees = float(src["fees"])   # posted fees are cash out of equity, not in the fills FIFO
        inv = src["check_invariant"](float(led.get("lifetime", 0.0) or 0.0) + fees, unreal, equity, start)
        return {"today": today, "today_alt": today_alt, "today_ok": abs(today - today_alt) <= tol,
                "since": inv["expected_total_pnl"], "since_alt": inv["computed_total_pnl"], "since_ok": bool(inv["ok"]),
                "open": unreal, "start": start, "start_assumed": assumed, "equity": equity}
    except Exception as e:  # noqa: BLE001
        logger.warning("audit card: account P&L unavailable (%s)", e)
        return None


def _fifo_day(day: str, src: Optional[dict] = None) -> Optional[tuple[float, float, int]]:
    """(trading-tier realized, quarterly-hold realized, closing orders) for PT date `day`, from Alpaca
    fills by FIFO — the authoritative P&L source — split exactly as the 8:30 PM ET heal splits it
    (pnl_ledger.build_ledger / heal_history: `pnl_today` = per_day_intraday, `pnl_today_qhm` =
    per_day_qhm), so the card means what the EOD file means. A partial close counts once per closing
    order. None when the fills or orders cannot be read in full (callers fall back and label it).
    Never raises."""
    try:
        src = src if src is not None else _ledger_and_account()
        if not src:
            return None
        led = src["led"]
        trading = round(float(led.get("per_day_intraday", {}).get(day, 0.0) or 0.0), 2)
        holds = round(float(led.get("per_day_qhm", {}).get(day, 0.0) or 0.0), 2)
        closes = {r.get("exit_order_id") or r.get("exit_fill_id") or r.get("id")
                  for r in led.get("round_trips", []) if r.get("exit_date") == day}
        return trading, holds, len(closes)
    except Exception as e:  # noqa: BLE001 — a card must never break on the P&L read
        logger.warning("audit card: Alpaca FIFO read failed (%s)", e)
        return None


def build_pnl_fields(mode: str, eod: dict, positions: Optional[list] = None,
                     day: Optional[str] = None) -> dict:
    """
    Return {"today": [...fields...], "lifetime": [...fields...], "source_note": str,
            "injected_numbers": [str,...], "mismatch": bool}.

    mode="nightly": trading-tier realized P&L (the heal's `pnl_today` definition), any quarterly-hold
                    realized P&L as its own line, and the closed-order count for `day`, straight from
                    Alpaca fills (FIFO, SLACK-EOD-1020); if the fills cannot be read in full, the EOD
                    snapshot's `pnl_today` / `pnl_today_qhm`, labeled. Reconciliation alarm from `pnl_unreconciled`, not drift.
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
        src = _ledger_and_account() if day else None
        fifo = _fifo_day(day, src) if day else None   # src None -> _fifo_day retries its own read once
        acct_pnl = _account_pnl(day, src) if (day and src) else None
        if fifo is not None:
            realized, holds, closed_n = fifo
            closed = str(closed_n)
        else:
            realized = float(eod.get("pnl_today", eod.get("alpaca_pnl", 0.0)) or 0.0)
            holds = float(eod.get("pnl_today_qhm", 0.0) or 0.0)
            closed = "—"   # the EOD trades_today count misses Day-tier closes; never show a wrong 0
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
        today_fields = [
            {"type": "mrkdwn", "text": f"*Realized P&L*\n{realized_str}"},
            {"type": "mrkdwn", "text": f"*Closed today*\n{closed}"},
        ]
        if holds:   # a quarterly-hold sale is shown on its own line, never folded in or hidden
            injected.append(_dollar(holds))
            today_fields.append({"type": "mrkdwn", "text": f"*Quarterly holds (separate)*\n{_dollar(holds)}"})
        if acct_pnl is not None:   # whole-account view (ACCOUNT-PNL-1020) — shown only when it cross-checks
            for label, val, ok, alt, src_name in (
                    ("Account today", acct_pnl["today"], acct_pnl["today_ok"], acct_pnl["today_alt"],
                     "portfolio history says"),
                    ("Open positions (unrealized)", acct_pnl["open"], True, acct_pnl["open"], ""),
                    ("Account since start", acct_pnl["since"], acct_pnl["since_ok"], acct_pnl["since_alt"],
                     "fills + fees + open say")):
                injected.append(_dollar(val))
                text = _dollar(val)
                if not ok:
                    injected.append(_dollar(alt))
                    text = f"⚠️ {_dollar(val)} — {src_name} {_dollar(alt)}; does not reconcile, verify"
                today_fields.append({"type": "mrkdwn", "text": f"*{label}*\n{text}"})
            injected.append(_dollar(acct_pnl["start"]))
            injected.append(_dollar(acct_pnl["equity"]))
            today_fields.append({"type": "mrkdwn", "text": (
                f"*Account value*\n{_dollar(acct_pnl['equity'])} vs {_dollar(acct_pnl['start'])} start"
                + (" _(assumed — no deposit records)_" if acct_pnl["start_assumed"] else ""))})
        if fifo is not None:
            source = "Alpaca FIFO · computed from today's fills"
        elif healed:
            source = "EOD snapshot · reconciled"
        else:
            source = "EOD snapshot · _Alpaca fills unreadable — provisional until the 5:30 PM PT reconcile_"
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
_PROSE_FINDING_LEAD = re.compile(
    r"^(?:(?:real|immediate)\s+)?(?:failure|bug|error)\s*:", re.IGNORECASE
)
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
    in_findings_table = False

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

    def _table_cells(value: str) -> list[str] | None:
        """Parse either Markdown table form while preserving code/escaped pipes."""
        if "|" not in value:
            return None
        row = value.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|") and not row.endswith(r"\|"):
            row = row[:-1]
        cells: list[str] = []
        cell: list[str] = []
        in_code = False
        escaped = False
        for char in row:
            if escaped:
                cell.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "`":
                in_code = not in_code
                cell.append(char)
            elif char == "|" and not in_code:
                cells.append(_clean_md("".join(cell).strip()))
                cell = []
            else:
                cell.append(char)
        if escaped:
            cell.append("\\")
        cells.append(_clean_md("".join(cell).strip()))
        return cells

    def _separator_row(cells: list[str]) -> bool:
        return bool(cells) and all(
            re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))
            for cell in cells
        )

    for raw in report.splitlines():
        line = raw.strip()
        s = line.lstrip("#* ").strip()
        if s.upper().startswith("CATASTROPHIC ALERT"):
            _flush()
            section = "cat"
            in_findings_table = False
            continue
        if s.upper().startswith("NEW BUGS"):
            _flush()
            section = "bugs"
            in_findings_table = False
            continue
        if s.upper().startswith(_STOP_HEADERS):
            _flush()
            section = None
            in_findings_table = False
            continue
        if not section:
            continue
        if not line:                               # blank line closes the current finding
            _flush()
            in_findings_table = False
            continue
        cells = _table_cells(line)
        is_header = bool(
            cells and len(cells) >= 5
            and [cell.upper() for cell in cells[:4]]
            == ["CATEGORY", "SEVERITY", "FILE", "DESCRIPTION"]
        )
        if is_header:
            _flush()
            in_findings_table = True
            continue
        if in_findings_table and (
            cells is None or len(cells) < 5 or bool(_ITEM_MARKER.match(raw))
            or bool(_PROSE_FINDING_LEAD.match(line))
        ):
            # Markdown tables are contiguous. A prose/item line ends the table
            # even when that prose itself contains several pipe characters.
            in_findings_table = False
        if in_findings_table and cells is not None and len(cells) >= 5:
            _flush()
            # Gemini sometimes emits the NEW BUGS contract as a Markdown table.
            # Its header/separator are structure, never findings. Each data row is
            # rendered as one phone-readable finding with its source file attached.
            if _separator_row(cells):
                continue
            valid_category = cells[0].upper() in _TAGS
            valid_severity = cells[1].upper() in {
                "CRITICAL", "HIGH", "MEDIUM", "LOW",
            }
            if not (valid_category and valid_severity):
                in_findings_table = False
            else:
                _category, severity, file_name, title = cells[:4]
                file_name = file_name.strip("`")
                # The title is wrapped in Slack bold later; remove inline-code markers
                # as a pair. strip("`") broke mixed titles such as ``_lead` newline``
                # by deleting only the opening marker and leaving a dangling backtick.
                title = title.replace("`", "")
                detail = " — ".join(cell for cell in cells[4:] if cell)
                sev_text = severity.upper()
                sev = ("critical" if section == "cat" or "CRITICAL" in sev_text
                       else "high" if "HIGH" in sev_text
                       else "low")
                if title and not title.lower().startswith("none"):
                    findings.append({
                        "severity": sev,
                        "title": _word_trunc(title, 120),
                        "detail": _word_trunc(detail, 240),
                        "file": file_name,
                    })
                continue
        stripped = line.lstrip("0123456789.)•*->— \t").strip()
        if not stripped:
            continue
        new_item = bool(_ITEM_MARKER.match(raw)) or (
            bool(_BOLD_LEAD.match(line)) and not _CONT_FIELD.match(line)
        ) or bool(_PROSE_FINDING_LEAD.match(stripped))
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
    """Render a compact finding with a separate detail line for mobile Slack."""
    t = str(f.get("title", "")).strip()
    d = str(f.get("detail", "")).strip()
    file_name = str(f.get("file", "")).strip()
    head = f"*{t}*"
    if file_name:
        head += f" · `{file_name}`"
    return f"{head}\n{d}" if d and d != t else head


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
