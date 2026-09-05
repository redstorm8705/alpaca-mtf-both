#!/usr/bin/env python3
"""
scripts/session_audit_digest.py

Aggregate the autonomous audit routines' outputs into ONE prioritized digest for
session start. Read-only, prints to stdout, never raises on missing/malformed files.

Wired into the session-start skill (Step 3d) so a session-start prompt surfaces
action items from ALL routines — not just the pending_approvals/queued tail:

  - gemini_audit_YYYY-MM-DD.txt   post-market / nightly Gemini audit (verdict + alerts)
  - midday_audit_YYYY-MM-DD.json  midday structured audit (pnl, postmortem, flagged)
  - meta_audit_latest.json        DS/GAI meta cross-review daily summary
  - audit_directives.jsonl        aggregated open action-item queue

Data tier: n/a (reads logs/ only). Output: stdout only.
Usage: python3 scripts/session_audit_digest.py
"""
import os
import re
import json
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOGS = os.path.join(_HERE, "..", "logs")

# _DONE = terminal directive statuses that must NOT show as OPEN in the digest.
# The last three are autonomous_patch_generator's terminal outcomes (2026-09-04):
# a board-declined, risk-path-skipped, or structurally-failed directive is done
# (never re-picked). failed_permanent was a pre-existing terminal status too.
_DONE = {"processed", "applied", "done", "closed", "resolved", "context_only",
         "board_rejected", "skipped_risk_path", "failed_permanent"}


def _newest(pattern: str) -> str | None:
    files = sorted(glob.glob(os.path.join(_LOGS, pattern)), reverse=True)
    return files[0] if files else None


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _hdr(title: str) -> None:
    print(f"\n=== {title} ===")


def nightly() -> None:
    _hdr("POST-MARKET / NIGHTLY GEMINI AUDIT")
    p = _newest("gemini_audit_*.txt")
    if not p:
        print("  (none found)")
        return
    txt = _read(p)
    print(f"  file: {os.path.basename(p)}")
    m = re.search(r"[Vv]erdict:\s*([A-Z]+)", txt)
    if m:
        print(f"  VERDICT: {m.group(1)}")
    for label in ("CATASTROPHIC ALERT", "CRITICAL", "TRADE INTEGRITY"):
        seg = re.search(rf"###\s*{label}.*?(?=\n###\s|\Z)", txt, re.S)
        if not seg:
            continue
        bullets = [ln.strip() for ln in seg.group(0).splitlines()
                   if ln.strip().startswith("*")]
        if bullets:
            print(f"  {label}:")
            for b in bullets[:5]:
                print(f"    - {b.lstrip('* ').strip()[:190]}")


def midday() -> None:
    _hdr("MIDDAY GEMINI AUDIT")
    p = _newest("midday_audit_*.json")
    if not p:
        print("  (none found)")
        return
    print(f"  file: {os.path.basename(p)}")
    try:
        d = json.loads(_read(p))
    except (json.JSONDecodeError, ValueError):
        print("  (unparseable)")
        return
    for key in ("pnl_analysis", "entry_analysis", "postmortem", "log_analysis"):
        if key in d and d[key]:
            print(f"  {key}: {str(d[key])[:200]}")


def meta() -> None:
    _hdr("META-AUDIT DAILY SUMMARY (DS/GAI cross-review)")
    p = os.path.join(_LOGS, "meta_audit_latest.json")
    if not os.path.exists(p):
        p = _newest("ai_audit_meta_*.json") or ""
    if not p or not os.path.exists(p):
        print("  (none found)")
        return
    print(f"  file: {os.path.basename(p)}")
    try:
        d = json.loads(_read(p))
    except (json.JSONDecodeError, ValueError):
        print("  (unparseable)")
        return
    print(f"  ts: {d.get('ts_pt', '?')}")
    summ = d.get("summary")
    if isinstance(summ, dict):
        for k, v in summ.items():
            print(f"  {k}: {str(v)[:200]}")
    elif summ:
        print(f"  summary: {str(summ)[:300]}")


def directives() -> None:
    _hdr("AUTONOMOUS ACTION-ITEM QUEUE (audit_directives.jsonl)")
    p = os.path.join(_LOGS, "audit_directives.jsonl")
    if not os.path.exists(p):
        print("  (none found)")
        return
    rows = []
    for line in _read(p).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    # collapse to latest per (file, finding-prefix) or per id
    by_key = {}
    for d in rows:
        key = d.get("id") or (d.get("file", ""),
                              (d.get("finding") or d.get("description") or "")[:40])
        by_key[key] = d
    open_items = [d for d in by_key.values()
                  if str(d.get("status", "")).lower() not in _DONE
                  and (d.get("file") or d.get("finding"))]
    from collections import Counter
    counts = Counter(str(d.get("status", "?")) for d in by_key.values())
    print(f"  total unique: {len(by_key)} | status: {dict(counts)}")
    print(f"  OPEN action items: {len(open_items)} (showing up to 20)")
    for d in open_items[:20]:
        st = d.get("status", "?")
        f = d.get("file", "?")
        find = (d.get("finding") or d.get("description") or "")[:150]
        src = d.get("source", "")
        print(f"    - [{st}] {f} :: {find}  ({src})")


def pending() -> None:
    _hdr("PIPELINE-COMPLETE PENDING (pending_ds_gai / pending_approvals)")
    found = False
    for pat in ("pending_ds_gai_*.json", "pending_approvals_*.md"):
        for p in sorted(glob.glob(os.path.join(_LOGS, pat)), reverse=True):
            found = True
            base = os.path.basename(p)
            if p.endswith(".json"):
                try:
                    d = json.loads(_read(p))
                    print(f"  {base}: target={d.get('target_file', '?')} "
                          f"status={d.get('status', '?')} "
                          f"ds={d.get('ds_verdict', '?')} "
                          f"gai={d.get('gai_verdict', '?')}")
                except (json.JSONDecodeError, ValueError):
                    print(f"  {base}: (unparseable)")
            else:
                print(f"  {base}")
    if not found:
        print("  (none)")


def main() -> None:
    print("########## SESSION AUDIT DIGEST — routine action items ##########")
    nightly()
    midday()
    meta()
    directives()
    pending()
    print("\n########## end digest ##########")


if __name__ == "__main__":
    main()
