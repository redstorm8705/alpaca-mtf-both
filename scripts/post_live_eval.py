#!/usr/bin/env python3
# ruff: noqa: E501
"""
scripts/post_live_eval.py — post-live audit EVALUATOR (increment 1; design record
logs/design_records/post_live_engine_2026-10-11.md; board Kim/Majors/López de Prado + Gro + GAI).
Diff A of POST-LIVE-WEEKLY-1106 (logs/design_records/post_live_weekly_2026-11-06.md, rev 7): per-signature error
identity, every signature stored per session, one issue per signature, classified UNEXERCISED_EXPIRED, durable
pending outputs.

Runs on OCI after the close (cron, weekdays). For every committed expectation record in logs/post_live/expect/ whose
watch window covers the session that just ended, it checks:
  * RAN?  each proof pattern's matches on lines of that session's date in its log. Zero for all patterns ->
          NOT_EXERCISED (never a pass); still zero once the window is spent -> finding UNEXERCISED_EXPIRED, with the
          cause classified (log missing / log undated / bad regex / never matches anywhere / matched before the ship).
  * ERROR SIGNATURES: every ERROR/CRITICAL line and every traceback in the session's mtf_bot.log and
          day_tier_runner_cron.log becomes a versioned signature
          (normaliser version, exception type or logger module, deepest in-repo frame, canonical message).
          A signature absent from the prior 5 sessions -> one issue per signature
          "[post-live] error <id>: <text>"; a signature that has an open issue is commented on when it recurs.
Metric evaluation is increment 2.

OUTPUTS (no code, no commits, no config, no tracked files on OCI):
  * logs/post_live/results/<session>.json (untracked; atomic; schema-versioned; EVERY signature with its line count);
  * logs/post_live/pending_outputs.json — issues / comments / Slack cards not delivered, retried by every run; each
    carries a delivery key that is searched for before anything is created (idempotent);
  * a GitHub issue per finding, label "post-live"; one Slack card for the session (at-least-once).
READ-ONLY on the market: Alpaca is read only for the trading calendar (T1). Never raises past main (exit 0/1).

  python3 scripts/post_live_eval.py [--session YYYY-MM-DD] [--dry-run]
  python3 scripts/post_live_eval.py --close-legacy [--dry-run]   # map the dated NEW_ERRORS_<date> issues, then close them
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
EXPECT_DIR = ROOT / "logs" / "post_live" / "expect"
RESULTS_DIR = ROOT / "logs" / "post_live" / "results"
PENDING_FILE = ROOT / "logs" / "post_live" / "pending_outputs.json"
TRADE_EVENTS = "logs/trade_events.jsonl"
ERROR_LOGS = ("logs/mtf_bot.log", "logs/day_tier_runner_cron.log")
REPO = "/repos/redstorm8705/alpaca-mtf-both"
NORMALISER_VERSION = 2
RESULTS_SCHEMA = 2
PENDING_SCHEMA = 1
_PRIOR_SESSIONS = 5
_EXPIRY_GRACE = 3          # sessions after the window in which a missed expiry is still raised (issues are deduped)
_ERR_RE = re.compile(r"\| (ERROR|CRITICAL) +\| ([\w.]+) \| (.*)$")
_LEGACY_RE = re.compile(r"^\[post-live\] session: NEW_ERRORS_(\d{4}-\d{2}-\d{2})$")


def sessions(until: date, n_back: int = 40) -> list:
    """Trading session dates up to and including `until` (Alpaca calendar, T1). Weekdays if the calendar is
    unreadable (holidays then count as sessions — the strict direction for the window)."""
    try:
        from alpaca.trading.requests import GetCalendarRequest

        from execution.broker import get_trading_client
        cal = get_trading_client().get_calendar(GetCalendarRequest(start=until - timedelta(days=n_back), end=until))
        out = sorted({c.date if isinstance(c.date, date) else date.fromisoformat(str(c.date)[:10]) for c in cal})
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        print(f"[post-live] calendar unreadable ({e}); using weekdays", file=sys.stderr)
    d, out = until - timedelta(days=n_back), []
    while d <= until:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def shipped_at(path: Path) -> "datetime | None":
    """When the record reached main: the FIRST-PARENT commit (the PR merge) that added it, ET (cold-2nd: the branch
    commit's date can be days before the merge/deploy)."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "log", "--first-parent", "-m", "--diff-filter=A",
                              "--format=%cI", "HEAD", "--", str(path.relative_to(ROOT))],
                             capture_output=True, text=True, timeout=20).stdout
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return datetime.fromisoformat(lines[-1]).astimezone(ET) if lines else None
    except Exception:  # noqa: BLE001
        return None


def window_position(shipped: datetime, session: date, cal: list) -> "int | None":
    """1-based index of `session` among the sessions that OPENED (09:30 ET) after `shipped`; None if before."""
    live = [d for d in cal if datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) >= shipped]
    return live.index(session) + 1 if session in live else None


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DAY_CACHE: dict = {}


def _log_files(log: Path) -> list:
    """The log plus its rotated siblings, oldest first (`x.log.1.gz`, `x.log.1`, `x.log`). OCI does not rotate these
    logs today (checked 2026-11-07: no logrotate entry); reading the siblings keeps the baseline if that changes."""
    out = [p for p in (log.with_name(log.name + ".1.gz"), log.with_name(log.name + ".1")) if p.exists()]
    return out + ([log] if log.exists() else [])


def _open(p: Path):
    return gzip.open(p, "rt", encoding="utf-8", errors="replace") if p.suffix == ".gz" \
        else p.open("r", encoding="utf-8", errors="replace")


def _lines_on(log: Path, day: date) -> list:
    """Lines of `log` dated `day` (the 'YYYY-MM-DD ' text-log and '"ts": "YYYY-MM-DD' JSONL forms, date within the first
    60 characters). An undated line (a raw traceback, a print) inherits the date of the last dated line before it
    (cold-2nd: an uncaught crash dumped into a cron log has no date). Missing log -> []. Memoised per run."""
    key = (str(log), day)
    if key in _DAY_CACHE:
        return _DAY_CACHE[key]
    tag = day.isoformat()
    out: list = []
    for p in _log_files(log):
        cur = ""
        with _open(p) as fh:
            for ln in fh:
                m = _DATE_RE.search(ln[:60])
                if m:
                    cur = m.group(1)
                if cur == tag:
                    out.append(ln.rstrip("\n"))
    _DAY_CACHE[key] = out
    return out


def partial_data(day: date) -> list:
    """Error logs whose rotated sibling was written on `day`: the session's lines are split across a rotation."""
    out = []
    for name in ERROR_LOGS:
        log = ROOT / name
        for p in _log_files(log):
            if p != log and datetime.fromtimestamp(p.stat().st_mtime, ET).date() == day:
                out.append(name)
                break
    return out


def proof_count(rec: dict, day: date) -> int:
    total = 0
    for p in rec.get("proof") or []:
        try:
            rx = re.compile(p["pattern"])
        except (re.error, KeyError, TypeError):
            continue                                    # classified by proof_diagnosis when the window expires
        total += sum(1 for ln in _lines_on(ROOT / p["log"], day) if rx.search(ln))
    return total


def proof_diagnosis(rec: dict, shipped: datetime) -> list:
    """Why an expired record never matched, per proof pattern: bad_regex / log_missing / log_undated (no dated lines) /
    never_matches (no line anywhere in the log — likely a pattern bug) / matched_before_ship (the pattern is good: the
    code really did not run after the ship). Reads the whole log once per pattern; only called for expired records."""
    out = []
    for p in rec.get("proof") or []:
        log = p.get("log") if isinstance(p, dict) else None
        try:
            rx = re.compile(p["pattern"])
        except (re.error, KeyError, TypeError):
            out.append({"log": log, "cause": "bad_regex"})
            continue
        files = _log_files(ROOT / str(log))
        if not files:
            out.append({"log": log, "cause": "log_missing"})
            continue
        dated, before, last = 0, None, None
        for f in files:
            cur = ""
            with _open(f) as fh:
                for ln in fh:
                    m = _DATE_RE.search(ln[:60])
                    if m:
                        cur, dated = m.group(1), dated + 1
                    if rx.search(ln):
                        last = cur or last
                        if cur and cur < shipped.date().isoformat():
                            before = cur
        if not dated:
            cause = "log_undated"
        elif last is None:
            cause = "never_matches"
        elif last >= shipped.date().isoformat():
            cause = "ran_after_window"                 # matched after the ship, but outside the window counted
        elif before is not None:
            cause = "matched_before_ship"
        else:
            cause = "unexplained"                      # a match exists on/after the ship day but not in the window
        out.append({"log": log, "cause": cause, "last_match": last})
    return out


# ── signature normalisation (version 2) ──────────────────────────────────────────────────────────────────────────
_SYM_TOKEN = r"[A-Z][A-Z0-9.\-]{0,11}"
# Numbers kept verbatim because they ARE the identity: HTTP / API status codes (429 != 500; Alpaca "code":40310000).
_HTTP_KEEP = re.compile(r"\b(?:status(?:_code)?|HTTP|code)\"?\s*[:=]?\s*\"?(\d{3,8})(?![\d.])"
                        r"|\b([1-5]\d\d)(?= (?:Server Error|Client Error|Too Many|Not Found|Forbidden|Unauthorized))")
_NAMED_NUM = re.compile(r"\b([a-z][a-z_]{1,24})[ =:]+\$?(-?\d+(?:\.\d+)?)(?![\w.])")
# Two named numbers are compared only within one family (rev 6: requested vs available, price vs limit).
_FAMILIES = ({"qty", "quantity", "requested", "available", "held", "shares", "need", "needed", "have", "filled",
              "remaining", "free"},
             {"price", "limit", "stop", "target", "entry", "last", "bid", "ask", "fill", "trigger"})


def _sign(num: str) -> str:
    v = float(num)
    return "<neg>" if v < 0 else "<zero>" if v == 0 else "<pos>"


def canonical(msg: str, universe: "set | frozenset" = frozenset()) -> str:
    """Mask by ROLE (rev 5-6): ids, timestamps, durations, URL query values and symbols are masked; a file path keeps
    its basename; HTTP / API status codes and enum words stay; any other number becomes its sign class, and two named
    numbers in one message keep their relation (requested<=available). Symbols are masked only where the line marks
    them as symbols ([SYM], ['A', 'B'], symbol=, (SYM), and 'for/on/in/of SYM' with SYM in the historical universe)."""
    s = msg
    s = re.sub(r"\[(?:'[A-Z0-9.\-]{1,12}',? ?)+\]", "<SYMS>", s)                     # ['MSFT', 'PLTU']
    s = re.sub(r"\[[A-Z0-9.\-]{1,12}\]", "[SYM]", s)                                 # [NVDA]
    s = re.sub(r"\b(symbol\"?\s*[:=]\s*['\"]?)" + _SYM_TOKEN, r"\1<SYM>", s)
    if universe:
        s = re.sub(r"\((" + _SYM_TOKEN + r")\)", lambda m: "(<SYM>)" if m.group(1) in universe else m.group(0), s)
        s = re.sub(r"\b(for|on|in|of) (" + _SYM_TOKEN + r")\b",
                   lambda m: f"{m.group(1)} <SYM>" if m.group(2) in universe else m.group(0), s)
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", s)    # uuids
    s = re.sub(r"\b[A-Z]{2}-[A-Z0-9.]+-[bs]-\d+-[0-9a-f]+\b", "<coid>", s)             # bot client order ids
    s = re.sub(r"\b[0-9a-f]{16,}\b", "<hex>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", "<ts>", s)
    s = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<date>", s)
    s = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?: ?[EP]T)?\b", "<time>", s)
    s = re.sub(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds|min|mins|minutes|h|hrs|hours)\b", "<dur>", s)
    s = re.sub(r"([?&][\w.\-]+=)[^&\s\"']+", r"\1<v>", s)                            # URL query values
    s = re.sub(r"(?<![\w:/])(?:~|\.{1,2})?/(?:[\w.\-]+/)*([\w.\-]+)", r"\1", s)              # path -> basename
    kept: list = []

    def _keep(m: re.Match) -> str:
        num = next(g for g in m.groups() if g)
        kept.append(num)
        start = m.start()
        g = next(i for i in range(1, 3) if m.group(i))
        a, b = m.start(g) - start, m.end(g) - start
        txt = m.group(0)
        return txt[:a] + f"\x00{len(kept) - 1}\x00" + txt[b:]
    s = _HTTP_KEEP.sub(_keep, s)
    pairs = [(m.group(1), float(m.group(2))) for m in _NAMED_NUM.finditer(s)]
    rel = [f"{a}{'<' if x < y else '=' if x == y else '>'}{b}" for (a, x), (b, y) in zip(pairs, pairs[1:], strict=False)
           if any(a in fam and b in fam for fam in _FAMILIES)]
    # A number keeps any unit letters glued to it (1.9x -> <pos>x, 3.5bps -> <pos>bps); never a partial decimal.
    s = re.sub(r"(?<![\w.\x00])(-?\d+(?:\.\d+)?)([A-Za-z%]*)(?![\w\x00]|\.\d)",
               lambda m: _sign(m.group(1)) + m.group(2), s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], s)
    s = re.sub(r"\s+", " ", s).strip()
    return s + (f" {{{', '.join(rel)}}}" if rel else "")


def _signature(module: str, msg: str, universe: "set | frozenset" = frozenset()) -> str:
    """Display form of an ERROR/CRITICAL line's signature (identity is sig_id over the full tuple)."""
    return f"{module}: {canonical(msg, universe)[:160]}"


def sig_id(kind: str, frame: str, canon: str) -> str:
    raw = json.dumps([NORMALISER_VERSION, kind, frame, canon], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


_FRAME_RE = re.compile(r'^\s+File "([^"]+)", line \d+, in (\S+)')
_EXC_RE = re.compile(r"^([A-Za-z_][\w.]*)(?::\s?(.*))?$")
_CHAIN = ("During handling of the above exception", "The above exception was the direct cause")
# A traceback header at the start of a line, optionally after a dated prefix ("2026-11-07 15:00:00 " or a
# "... | LEVEL | module | " log prefix); never an indented source line or a message that merely quotes the phrase.
_TB_HEAD = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[^|\n]*?(?:\|[^|\n]*)*?\| |\d{4}-\d{2}-\d{2}[ T][\d:,.]+ )?"
                      r"Traceback \(most recent call last\)")


def _in_repo(path: str) -> "str | None":
    if path.startswith("<"):
        return None                                     # <frozen importlib._bootstrap>, <string>
    p = path.replace("\\", "/")
    root = str(ROOT).replace("\\", "/").rstrip("/") + "/"
    if "/site-packages/" in p or "/venv/" in p or "/lib/python" in p:
        return None
    rel = p[len(root):] if p.startswith(root) else (p if not p.startswith("/") else None)
    if not rel:
        return None
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")


def _tracebacks(lines: list) -> list:
    """(exception type, message, in-repo frames) per traceback; a chained traceback reports its final exception.
    A block that ends without an exception line is ("Traceback", "<incomplete>", frames)."""
    out: list = []
    frames: "list | None" = None
    pending = None
    chained = False
    for ln in lines:
        if _TB_HEAD.match(ln):          # at column 0, or right after a date / log prefix (Codex preship; cold-2nd r6)
            if pending is not None and not chained:
                out.append(pending)
            pending, chained, frames = None, False, []
            continue
        if frames is None:
            if pending is not None and ln.strip():
                if ln.startswith(_CHAIN):
                    chained = True
                    continue
                out.append(pending)
                pending, chained = None, False
            continue
        m = _FRAME_RE.match(ln)
        if m:
            mod = _in_repo(m.group(1))
            if mod:
                frames.append(f"{mod}:{m.group(2)}")
            continue
        if ln.startswith((" ", "\t")) or not ln.strip():
            continue                                        # source line / caret line
        e = _EXC_RE.match(ln) if not _DATE_RE.match(ln[:10]) else None
        if e:
            pending, frames = (e.group(1), e.group(2) or "", frames), None
        else:
            out.append(("Traceback", "<incomplete>", frames))
            frames = None
    if frames is not None:
        out.append(("Traceback", "<incomplete>", frames))
    if pending is not None:
        out.append(pending)
    return out


def error_signatures(day: date, universe: "set | frozenset" = frozenset()) -> dict:
    """{sig_id: {kind, frame, caller, canonical, cluster, display, count, examples}} for the session's error logs."""
    sigs: dict = {}

    def _add(kind: str, frame: str, caller: str, canon: str, raw: str) -> None:
        sid = sig_id(kind, frame, canon)
        s = sigs.setdefault(sid, {"kind": kind, "frame": frame, "caller": caller, "canonical": canon,
                                  "cluster": hashlib.sha256(f"{kind}|{canon}".encode()).hexdigest()[:12],
                                  "display": (f"{kind} at {frame}: " if frame else f"{kind}: ") + canon[:160],
                                  "count": 0, "examples": []})
        s["count"] += 1
        if len(s["examples"]) < 2 and raw[:400] not in s["examples"]:
            s["examples"].append(raw[:400])
    for name in ERROR_LOGS:
        lines = _lines_on(ROOT / name, day)
        for ln in lines:
            m = _ERR_RE.search(ln)
            if m:
                _add(m.group(2), "", "", canonical(m.group(3), universe), ln)
        for kind, msg, frames in _tracebacks(lines):
            frame = frames[-1] if frames else ""
            caller = frames[-2] if len(frames) > 1 else ""
            _add(kind, frame, caller, canonical(msg, universe) if msg != "<incomplete>" else msg,
                 f"{kind}: {msg}" + (f" (at {frame})" if frame else ""))
    return sigs


def symbol_snapshot(days: list) -> set:
    """Symbols the bot handled on `days` (the `symbol` field of logs/trade_events.jsonl) — one session's universe
    snapshot; the union over the window is the stable masking universe (no hindsight from today's alone)."""
    tags = {d.isoformat() for d in days}
    out: set = set()
    p = ROOT / TRADE_EVENTS
    if not p.exists():
        return out
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            m = _DATE_RE.search(ln[:60])
            if m and m.group(1) in tags:
                s = re.search(r'"symbol": "([A-Z][A-Z0-9.\-]{0,11})"', ln)
                if s:
                    out.add(s.group(1))
    return out


# ── state files (atomic, schema-versioned; an unreadable file is backed up, never silently replaced) ─────────────
def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_state(path: Path, health: list, backup: bool = True) -> "dict | None":
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("not an object")
        return obj
    except (OSError, ValueError) as e:
        bak = path.with_name(path.name + f".corrupt.{datetime.now(ET):%Y%m%dT%H%M%S}")
        try:
            if not backup:
                raise OSError("dry run")
            path.replace(bak)
        except OSError:
            bak = path
        health.append(f"{path.name} unreadable ({e}); kept as {bak.name}")
        return None


def _stored(day: date, health: list, backup: bool = True) -> "dict | None":
    """A prior session's stored signatures when written by this normaliser version, else None (recompute)."""
    obj = _read_state(RESULTS_DIR / f"{day.isoformat()}.json", health, backup)
    if obj and obj.get("schema_version") == RESULTS_SCHEMA and obj.get("normaliser_version") == NORMALISER_VERSION \
            and isinstance(obj.get("signatures"), dict):
        return obj
    return None


def evaluate(session: date, cal: list, dry: bool = False) -> dict:
    _DAY_CACHE.clear()
    health: list = []
    prior = [d for d in cal if d < session][-_PRIOR_SESSIONS:]
    stored = {d: _stored(d, health, not dry) for d in prior}
    universe: set = symbol_snapshot(prior + [session])
    for obj in stored.values():
        if obj:
            su = obj.get("symbol_universe")
            universe |= {str(x) for x in su} if isinstance(su, list) else set()
    old_ids: set = set()
    for d in prior:
        obj = stored[d]
        su = obj.get("symbol_universe") if obj else None
        # Stored ids are reused only when they were masked with this same universe; a universe that has grown since
        # (a symbol first traded later) would mask the same failure differently, so the session is re-derived from its
        # raw log lines (Codex preship 2026-11-07).
        if obj and isinstance(su, list) and {str(x) for x in su} == universe:
            old_ids |= set(obj.get("signatures") or {})
        else:
            old_ids |= set(error_signatures(d, universe))
    sigs = error_signatures(session, universe)
    new_ids = sorted(sid for sid in sigs if sid not in old_ids)
    changes: list = []
    findings: list = []
    skipped: list = []
    for path in sorted(EXPECT_DIR.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            findings.append({"change": path.stem, "finding": "UNREADABLE_RECORD", "detail": str(e)})
            continue
        shipped = shipped_at(path)
        if shipped is None:
            skipped.append({"change": rec.get("change", path.stem), "reason": "ship date unreadable (not on main?)"})
            continue
        pos = window_position(shipped, session, cal)
        live = [d for d in cal if datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) >= shipped and d <= session]
        try:
            window = int(rec.get("window_sessions") or 5)
            # window_days (calendar days after the ship): for a job that runs outside trading sessions, e.g. a
            # weekend report. The window then ends at the first session on/after ship date + window_days; until
            # that session exists it has not ended (2026-10-13: a Saturday-only job expired after two weekdays).
            window_days = int(rec["window_days"]) if rec.get("window_days") is not None else None
        except (TypeError, ValueError):
            findings.append({"change": rec.get("change", path.stem), "finding": "BAD_WINDOW",
                             "detail": f"window unreadable: sessions={rec.get('window_sessions')!r} "
                                       f"days={rec.get('window_days')!r}"})
            continue
        if pos is None:
            continue
        if window_days is not None:
            deadline = shipped.date() + timedelta(days=window_days)
            ends = [i + 1 for i, d in enumerate(live) if d >= deadline]
            window = ends[0] if ends else pos + 1
        if pos > window + _EXPIRY_GRACE:
            continue
        # Cumulative over the window so far (cold-2nd F1), counted on EVERY calendar day from the ship date — a proof
        # line written in the evening or on a weekend is dated between sessions (2026-10-13: the fill-timestamp
        # canonicalisation ran once at 17:46 ET and was never counted).
        last_day = session if pos <= window else live[window - 1]
        days = [shipped.date() + timedelta(days=i) for i in range((last_day - shipped.date()).days + 1)]
        n_today = proof_count(rec, session) if pos <= window else 0
        n_total = sum(proof_count(rec, d) for d in days)
        if pos > window and n_total > 0:
            continue                         # ran within its window; nothing more to report
        verdict = "RAN" if n_total > 0 else "NOT_EXERCISED"
        changes.append({"change": rec.get("change", path.stem), "tier": rec.get("tier"), "session_in_window": pos,
                        "window": window, "proof_matches_session": n_today, "proof_matches_window": n_total,
                        "verdict": verdict})
        if n_total == 0 and pos >= window:   # the window's last session, or a missed run inside the grace
            diag = proof_diagnosis(rec, shipped)
            causes = ", ".join(f"{d['log']}: {d['cause']}" for d in diag) or "no proof patterns"
            findings.append({"change": rec.get("change", path.stem), "finding": "UNEXERCISED_EXPIRED",
                             "causes": diag,
                             "detail": f"no proof match since it shipped ({window} session(s) checked); expected rate: "
                                       f"{rec.get('expected_rate')}. Cause: {causes}. Needs a historical replay "
                                       f"proving the code fires, or a remove / kill-flag decision."})
    live_changes = [c["change"] for c in changes] or "none"
    for sid in new_ids:
        s = sigs[sid]
        ex = "\n".join(f"    {e}" for e in s["examples"])
        findings.append({"change": f"error {sid}", "finding": s["display"][:90], "kind": "NEW_ERROR", "sig": sid,
                         "detail": f"New error signature (absent in the prior {_PRIOR_SESSIONS} sessions), "
                                   f"{s['count']} line(s) this session.\nType / logger: {s['kind']}\n"
                                   f"Deepest in-repo frame: {s['frame'] or 'n/a'} (caller {s['caller'] or 'n/a'})\n"
                                   f"Canonical: {s['canonical']}\nRelated cluster: {s['cluster']}\n"
                                   f"Examples:\n{ex}\nChanges live in their window: {live_changes}"})
    return {"schema_version": RESULTS_SCHEMA, "normaliser_version": NORMALISER_VERSION, "session": session.isoformat(),
            "changes": changes, "new_error_signatures": [sigs[s]["display"] for s in new_ids], "new_ids": new_ids,
            "signatures": sigs, "symbol_universe": sorted(universe), "partial_data": partial_data(session),
            "skipped": skipped, "health": health, "findings": findings}


# ── delivery: one issue per finding, idempotent by delivery key; undelivered outputs retried every run ───────────
def _key(finding: dict) -> str:
    return f"err-{finding['sig']}" if finding.get("sig") else f"chg-{finding['change']}-{finding['finding']}"


def _key_tag(key: str) -> str:
    return f"<!-- post-live-key: {key} -->"


def _open_issues(tok: str) -> "list | None":
    from scripts.sync_reports import _api
    out: list = []
    for page in range(1, 21):
        st, items = _api("GET", f"{REPO}/issues?state=open&per_page=100&page={page}", tok)
        if st != 200 or not isinstance(items, list):
            return None
        out += [it for it in items if "pull_request" not in it]
        if len(items) < 100:
            return out
    return out


def _find(index: list, key: str, title: str) -> "dict | None":
    tag = _key_tag(key)
    for it in index:
        if tag in (it.get("body") or "") or it.get("title") == title:
            return it
    return None


def _deliver(item: dict, tok: str, index: list) -> str:
    """Comment on the open issue carrying the item's key (or exact title); else create it. Returns a status line."""
    from scripts.sync_reports import _api
    it = _find(index, item.get("find_key") or item["key"], item["title"])
    if it is not None:
        cst, _ = _api("POST", f"{REPO}/issues/{it['number']}/comments", tok, {"body": item["comment"]})
        url = it.get("html_url") or "#" + str(it["number"])
        return f"updated {url}" if cst in (200, 201) else f"comment failed ({cst}) on {url}"
    if item.get("comment_only"):
        return "no open issue — recurrence noted in results only"
    st, made = _api("POST", f"{REPO}/issues", tok, {"title": item["title"], "body": item["body"],
                                                    "labels": ["post-live"]})
    if st in (200, 201) and isinstance(made, dict):
        index.append({**made, "body": item["body"]})
        return f"opened {made.get('html_url') or '#' + str(made.get('number'))}"
    return f"issue create failed ({st})"


def _item(finding: dict, session: date) -> dict:
    key = _key(finding)
    head = f"Session {session.isoformat()} (post_live_eval.py)"
    return {"kind": "issue", "key": key, "title": f"[post-live] {finding['change']}: {finding['finding']}",
            "body": f"{head}\n\n{finding['detail']}\n\n{_key_tag(key)}",
            "comment": f"{head}\n\n{finding['detail']}", "queued": datetime.now(ET).isoformat()}


def _upsert_issue(finding: dict, session: date, dry: bool, index: "list | None" = None,
                  tok: "str | None" = None) -> str:
    item = _item(finding, session)
    if dry:
        return f"DRY {item['title']}"
    if not tok:
        from scripts.sync_reports import _github_token
        tok = _github_token()
    if not tok:
        return f"NO_TOKEN {item['title']}"
    if index is None:
        index = _open_issues(tok)
    # A failed lookup never falls through to a create (cold-2nd: that duplicates); the item is queued and retried.
    if index is None:
        return f"issue lookup failed — not created: {item['title']}"
    return _deliver(item, tok, index)


def _not_delivered(status: str) -> bool:
    return status.startswith(("issue lookup failed", "NO_TOKEN", "issue create failed", "comment failed"))


def load_pending(health: list) -> list:
    """Undelivered outputs. A file in another schema is never dropped silently (Codex preship 2026-11-07): it is kept
    as `<name>.unsupported.<ts>` and reported in the health section, and this run starts a fresh queue."""
    obj = _read_state(PENDING_FILE, health)
    if obj is None:
        return []
    if obj.get("schema_version") != PENDING_SCHEMA or not isinstance(obj.get("items"), list):
        bak = PENDING_FILE.with_name(PENDING_FILE.name + f".unsupported.{datetime.now(ET):%Y%m%dT%H%M%S}")
        try:
            PENDING_FILE.replace(bak)
            health.append(f"{PENDING_FILE.name} has schema {obj.get('schema_version')!r} (expected {PENDING_SCHEMA}); "
                          f"kept as {bak.name}, not delivered")
        except OSError as e:
            health.append(f"{PENDING_FILE.name} has an unsupported schema and could not be moved aside ({e})")
            raise
        return []
    return [i for i in obj["items"] if isinstance(i, dict) and i.get("key")]


def save_pending(items: list) -> None:
    _atomic_write(PENDING_FILE, {"schema_version": PENDING_SCHEMA, "items": items})


def _attempts(p: dict) -> int:
    try:
        return int(p.get("attempts", 0))
    except (TypeError, ValueError):
        return 0


def _merge_pending(pending: list, item: dict, status: str) -> None:
    item = {**item, "last_status": status, "attempts": 1}
    for i, p in enumerate(pending):
        if p.get("key") == item["key"] and p.get("kind") == item["kind"]:
            pending[i] = {**item, "queued": p.get("queued", item["queued"]), "attempts": _attempts(p) + 1}
            return
    pending.append(item)


def retry_pending(pending: list, tok: "str | None", index: "list | None") -> list:
    """Re-deliver queued outputs. Issues: the key is searched before anything is created. Slack: at-least-once."""
    left = []
    for p in pending:
        try:
            if p.get("kind") == "slack":
                from alerts import send_slack_blocks
                if send_slack_blocks(p["blocks"], p["fallback"]):
                    continue
            elif tok and index is not None and not _not_delivered(_deliver(p, tok, index)):
                continue
        except Exception as e:  # noqa: BLE001
            p = {**p, "last_status": f"retry failed: {e!r}"}
        left.append({**p, "attempts": _attempts(p) + 1})
    return left


def close_legacy(cal: list, dry: bool) -> int:
    """One-time (rev 3 item 5): map each open dated `NEW_ERRORS_<date>` issue to per-signature issues (recomputed for
    that session with this normaliser), link them, close the dated issue, and verify the closure through the API."""
    from scripts.sync_reports import _api, _github_token
    tok = _github_token()
    index = _open_issues(tok) if tok else None
    if index is None:
        print("close-legacy: GitHub unreadable — nothing done")
        return 1
    bad = 0
    for it in sorted((i for i in index if _LEGACY_RE.match(i.get("title") or "")), key=lambda i: i["number"]):
        lm = _LEGACY_RE.match(it["title"])
        if lm is None:
            continue
        day = date.fromisoformat(lm.group(1))
        res = evaluate(day, [d for d in cal if d <= day] if day in cal else sessions(day), dry)
        links = []
        for f in (x for x in res["findings"] if x.get("kind") == "NEW_ERROR"):
            st = _upsert_issue(f, day, dry, index, tok)
            links.append((f["finding"], st))
        note = (f"Superseded by per-signature issues (POST-LIVE-WEEKLY-1106 Diff A, normaliser v{NORMALISER_VERSION}). "
                f"Signatures new on {day} under the new normaliser:\n" + ("\n".join(f"- {a} → {b}" for a, b in links) or "- none (all were seen "
                f"in the prior {_PRIOR_SESSIONS} sessions once ids, numbers and symbols are masked by role)"))
        if dry:
            print(f"DRY close #{it['number']} ({day}):\n{note}")
            continue
        if any(_not_delivered(b) for _a, b in links):
            print(f"close-legacy: #{it['number']} kept open — a signature issue was not delivered")
            bad += 1
            continue
        cst, _ = _api("POST", f"{REPO}/issues/{it['number']}/comments", tok, {"body": note})
        if cst not in (200, 201):
            print(f"close-legacy: #{it['number']} kept open — the link comment was not posted ({cst})")
            bad += 1
            continue
        _api("PATCH", f"{REPO}/issues/{it['number']}", tok, {"state": "closed", "state_reason": "completed"})
        st, now = _api("GET", f"{REPO}/issues/{it['number']}", tok)
        ok = st == 200 and isinstance(now, dict) and now.get("state") == "closed"
        print(f"close-legacy: #{it['number']} ({day}) {'closed (verified)' if ok else 'NOT closed'}; "
              f"{len(links)} signature issue(s)")
        bad += 0 if ok else 1
    return 1 if bad else 0


def main(argv: "list | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    dry = "--dry-run" in argv
    today = datetime.now(ET).date()
    if "--close-legacy" in argv:
        return close_legacy(sessions(today), dry)
    session = date.fromisoformat(argv[argv.index("--session") + 1]) if "--session" in argv else today
    cal = sessions(session)
    if session not in cal:
        print(f"post-live eval: {session} is not a trading session — nothing to do")
        return 0
    res = evaluate(session, cal, dry)
    tok, index = None, None
    if not dry:
        from scripts.sync_reports import _github_token
        tok = _github_token()
        index = _open_issues(tok) if tok else None
    pending = load_pending(res["health"]) if not dry else []
    pending = retry_pending(pending, tok, index) if not dry else []
    res["issues"] = [_upsert_issue(f, session, dry, index, tok) for f in res["findings"]]
    for f, st in zip(res["findings"], res["issues"], strict=True):
        if _not_delivered(st):
            _merge_pending(pending, _item(f, session), st)
    # Recurrence of a known signature that has an open issue: comment (never create — it was not new).
    recurring = []
    for sid, s in res["signatures"].items():
        if sid in res["new_ids"] or dry:
            continue
        # Its own pending key (cold-2nd r2): a queued comment must never replace a queued create for the same signature.
        item = {"kind": "comment", "key": f"err-{sid}-recur", "find_key": f"err-{sid}", "title": "", "comment_only": True,
                "queued": datetime.now(ET).isoformat(),
                "comment": f"Recurred on {session.isoformat()}: {s['count']} line(s).\n"
                           f"Example: {s['examples'][0] if s['examples'] else 'n/a'}"}
        if index is None or tok is None:
            _merge_pending(pending, item, "issue lookup failed — recurrence comment queued")
            continue
        hit = _find(index, f"err-{sid}", "")
        if hit is not None:
            st = _deliver(item, tok, index)
            recurring.append({"sig": sid, "issue": hit.get("html_url"), "status": st})
            if _not_delivered(st):
                _merge_pending(pending, item, st)
    res["recurring"] = recurring
    ran = [c["change"] for c in res["changes"] if c["verdict"] == "RAN"]
    expired = {f["change"] for f in res["findings"] if f["finding"] == "UNEXERCISED_EXPIRED"}
    idle = [c["change"] for c in res["changes"] if c["verdict"] != "RAN" and c["change"] not in expired]
    # PLAIN ENGLISH (owner 2026-10-16): what worked, what is still waiting, what needs a look — names in the results file.
    def _plain(f: dict, link: str) -> str:
        url = next((w for w in str(link).split() if w.startswith("http")), "issue not filed")
        if f["finding"] == "UNEXERCISED_EXPIRED":
            return f"'{f['change']}' never ran in its check window — needs a replay or a decision ({url})"
        if f.get("kind") == "NEW_ERROR":
            return f"a new kind of error showed up in today's logs ({url})"
        return f"{f['change']}: {f['finding']} ({url})"
    msg = (f"New-code check for {session:%a %b %d}: {len(ran)} recent change(s) have run since they shipped"
           + (f"; {len(idle)} are still waiting for the situation they handle (normal)" if idle else "")
           + (". Needs a look: " + "; ".join(_plain(f, i) for f, i in zip(res["findings"], res["issues"], strict=True))
              if res["findings"] else ". Nothing needs attention."))
    if not dry:
        try:
            from alerts import send_slack_blocks
            blocks, fallback = _slack_digest(session, len(ran), len(idle), res["findings"], res["issues"],
                                             n_recurring=len(recurring))
            if not send_slack_blocks(blocks, fallback):
                print("post-live eval: Slack not delivered — queued", file=sys.stderr)
                _merge_pending(pending, {"kind": "slack", "key": f"slack-{session.isoformat()}", "blocks": blocks,
                                         "fallback": fallback, "queued": datetime.now(ET).isoformat()}, "not delivered")
        except Exception as e:  # noqa: BLE001
            print(f"post-live eval: Slack failed: {e}", file=sys.stderr)
        res["pending_outputs"] = len(pending)
        res["pending_oldest"] = min((p.get("queued", "") for p in pending), default=None)
    pending_lost = False
    if not dry:
        try:
            save_pending(pending)
        except OSError as e:
            # Undelivered outputs would be lost: say so loudly and fail the run (Codex preship 2026-11-07); the results
            # file still records every finding and its delivery status.
            pending_lost = True
            res["health"].append(f"pending outputs NOT saved ({e}); {len(pending)} undelivered output(s) at risk")
            print(f"post-live eval FAILED: pending outputs not saved ({e})", file=sys.stderr)
        _atomic_write(RESULTS_DIR / f"{session.isoformat()}.json", res)
    # Dated + tagged with this change's slug: the evaluator's own unique proof line (cold-2nd F2; board change_id tag).
    print(f"{session.isoformat()} post-live eval [post-live-evaluator]: {msg}")
    print(f"{session.isoformat()} post-live signatures [post-live-sig-v2]: normaliser={NORMALISER_VERSION} "
          f"signatures={len(res['signatures'])} new={len(res['new_ids'])} recurring={len(recurring)} "
          f"pending={res.get('pending_outputs', 0)} partial={','.join(res['partial_data']) or 'none'} "
          f"status={'degraded' if res['health'] else 'complete'}")
    return 1 if pending_lost else 0


def _slack_digest(session: date, n_ran: int, n_idle: int, findings: list, issues: list,
                  n_recurring: int = 0) -> "tuple[list, str]":
    """The owner's phone digest (rules/slack_format.md SLK01-SLK12; owner 2026-10-27: the one-paragraph version was
    "horrible and hard to read"). Header, a three-line count summary, then one short line per finding with a tappable
    issue link. Pure: builds blocks only."""
    def _link(raw: str) -> str:
        url = next((w for w in str(raw).split() if w.startswith("http")), "")
        num = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        return f"<{url}|Issue #{num}>" if url and num.isdigit() else "issue not filed"

    stale = [(f, i) for f, i in zip(findings, issues, strict=True) if f["finding"] == "UNEXERCISED_EXPIRED"]
    other = [(f, i) for f, i in zip(findings, issues, strict=True) if f["finding"] != "UNEXERCISED_EXPIRED"]
    summary = [f"✅ *{n_ran}* ran as expected"]
    if n_idle:
        summary.append(f"⏳ {n_idle} still waiting (normal)")
    summary.append(f"⚠️ *{len(findings)}* need a look" if findings else "Nothing needs attention")
    if n_recurring:
        summary.append(f"🔁 {n_recurring} known error(s) happened again")
    blocks: list = [
        {"type": "header", "text": {"type": "plain_text", "text": f"New-code check · {session:%a %b %d}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(summary)}},
    ]
    def _sections(rows: list) -> list:
        # Slack caps a section's text at 3,000 characters: pack whole lines into sections under that cap (a single
        # oversized line is cut to fit rather than dropping the message).
        out, cur = [], ""
        for row in (r[:2900] for r in rows):
            if cur and len(cur) + 1 + len(row) > 2900:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n{row}" if cur else row
        if cur:
            out.append(cur)
        return [{"type": "section", "text": {"type": "mrkdwn", "text": t}} for t in out]

    if other:
        blocks.append({"type": "divider"})
        lines = []
        for f, i in other:
            if f.get("kind") == "NEW_ERROR":
                where = str(f["finding"]).split(":", 1)[0].split(" at ")[-1].split(":")[0].rsplit(".", 1)[-1][:16]
                what = f"New error in {where}"
            else:
                what = f"{f['change']}: {f['finding']}"
            lines.append(f"🔴 {what} · {_link(i)}")
        blocks.extend(_sections(lines))
    if stale:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            "*Never ran in its check window*\nNeeds a replay or a decision")}})
        blocks.extend(_sections([f"⚠️ {f['change']} · {_link(i)}" for f, i in stale]))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": (
        "Post-live evaluator · details in each issue · a retried card may arrive twice")}]})
    fallback = (f"New-code check {session:%a %b %d}: {n_ran} ran, {n_idle} waiting, "
                + (f"⚠️ {len(findings)} need a look" if findings else "nothing needs attention"))
    return blocks, fallback


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"post-live eval FAILED: {e!r}", file=sys.stderr)
        sys.exit(1)
